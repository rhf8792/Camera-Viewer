"""Camera Viewer - live view and synchronized recording of several USB cameras.

Tick the cameras you want in the side panel. They are laid out in a grid that
fits the count (1 -> 1x1, 2 -> 2x1, 3 -> 3x1, 4 -> 2x2, 5-6 -> 3x2, 7-9 -> 3x3 ...),
either inside one window (drag the dividers to resize tiles) or as separate
windows tiled across the screen.

Each camera has its own resolution / FPS picker and an adjustments panel
(hardware camera settings + software filters). Recording writes one MJPEG .avi
per camera plus a CSV of per-frame timestamps on a clock shared by every camera
in the same recording, so several cameras can be aligned afterwards. Recordings
can start immediately or on a TTL edge from an Arduino (serial) or a Harp board.
"""

import csv
import math
import os
import queue
import re
import sys
import threading
import time
import shutil
from dataclasses import dataclass, field
from datetime import datetime

import cv2
import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

APP_NAME = "Camera Viewer"
PREFERRED_FOURCC = "MJPG"  # compressed, so several cameras fit on one USB bus
OPEN_TIMEOUT_S = 4.0
_OPEN_LOCK = threading.Lock()


def settings():
    return QtCore.QSettings("CameraViewer", "CameraViewer")


def fmt_hms(seconds):
    s = int(max(seconds, 0))
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def safe_filename(text):
    text = re.sub(r'[<>:"/\\|?*#]+', "", text).strip()
    return re.sub(r"\s+", "_", text) or "camera"


# --------------------------------------------------------------------------- #
# Camera discovery
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    fourcc: str
    max_fps: float

    @property
    def label(self):
        return f"{self.width}×{self.height} {self.fourcc}"


# Used when the driver won't tell us which modes it supports.
GENERIC_MODES = [
    Mode(1920, 1080, "MJPG", 30), Mode(1280, 720, "MJPG", 30),
    Mode(800, 600, "MJPG", 30), Mode(640, 480, "MJPG", 30), Mode(320, 240, "MJPG", 30),
]


@dataclass
class CameraInfo:
    index: int        # DirectShow / OpenCV index
    name: str         # display name (made unique)
    modes: list


def _modes_from_formats(formats):
    best = {}
    for f in formats:
        fourcc = str(f.get("media_type_str", "")).strip() or "?"
        w, h = int(f["width"]), int(f["height"])
        fps = max(float(f.get("min_framerate") or 0), float(f.get("max_framerate") or 0))
        key = (w, h, fourcc)
        if fps > best.get(key, 0):
            best[key] = fps
    modes = [Mode(w, h, cc, fps or 30) for (w, h, cc), fps in best.items()]
    # Preferred encoding first, then biggest resolution first.
    modes.sort(key=lambda m: (m.fourcc != PREFERRED_FOURCC, -m.width * m.height, -m.max_fps))
    return modes


def list_cameras():
    """Return every video capture device, in the same order OpenCV's DirectShow backend uses."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
    except Exception:
        if "--diag" in sys.argv:
            import traceback
            traceback.print_exc()
        names = None

    if names is None:  # fall back to probing indices
        names = []
        for i in range(10):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if not cap.isOpened():
                cap.release()
                break
            names.append(f"Camera {i}")
            cap.release()

    cams, seen = [], {}
    for i, name in enumerate(names):
        modes = []
        try:
            from pygrabber.dshow_graph import FilterGraph
            graph = FilterGraph()
            graph.add_video_input_device(i)
            modes = _modes_from_formats(graph.get_input_device().get_formats())
        except Exception:
            pass
        seen[name] = seen.get(name, 0) + 1
        cams.append(CameraInfo(i, name, modes or list(GENERIC_MODES)))

    # Number duplicate names: "HD USB Camera #1", "HD USB Camera #2" ...
    counts = {}
    for cam in cams:
        if seen[cam.name] > 1:
            counts[cam.name] = counts.get(cam.name, 0) + 1
            cam.name = f"{cam.name} #{counts[cam.name]}"
    return cams


def fps_options(max_fps):
    top = max(1, round(max_fps))
    return [top] + [v for v in (60, 30, 25, 20, 15, 10, 5) if v < top]


def grid_shape(n):
    """(rows, cols) for n tiles: up to 3 in a row, then the squarest grid."""
    if n <= 3:
        return 1, max(n, 1)
    cols = math.ceil(math.sqrt(n))
    return math.ceil(n / cols), cols


# --------------------------------------------------------------------------- #
# Hardware camera controls (DirectShow IAMVideoProcAmp / IAMCameraControl)
# --------------------------------------------------------------------------- #

def _make_range_iface(name, iid):
    from ctypes import HRESULT, POINTER, c_long
    from comtypes import COMMETHOD, GUID, IUnknown
    return type(name, (IUnknown,), {"_iid_": GUID(iid), "_methods_": [
        COMMETHOD([], HRESULT, "GetRange", (["in"], c_long, "Property"),
                  (["out"], POINTER(c_long), "pMin"), (["out"], POINTER(c_long), "pMax"),
                  (["out"], POINTER(c_long), "pStep"), (["out"], POINTER(c_long), "pDefault"),
                  (["out"], POINTER(c_long), "pCaps")),
        COMMETHOD([], HRESULT, "Set", (["in"], c_long, "Property"),
                  (["in"], c_long, "lValue"), (["in"], c_long, "Flags")),
        COMMETHOD([], HRESULT, "Get", (["in"], c_long, "Property"),
                  (["out"], POINTER(c_long), "lValue"), (["out"], POINTER(c_long), "Flags")),
    ]})


FLAG_AUTO, FLAG_MANUAL = 1, 2

# (key, label, interface, DirectShow property id)
HW_PROPS = [
    ("exposure", "Exposure", "cam", 4),
    ("gain", "Gain", "amp", 9),
    ("brightness", "Brightness", "amp", 0),
    ("contrast", "Contrast", "amp", 1),
    ("gamma", "Gamma", "amp", 5),
    ("saturation", "Saturation", "amp", 3),
    ("hue", "Hue", "amp", 2),
    ("sharpness", "Sharpness", "amp", 4),
    ("white_balance", "White balance", "amp", 7),
    ("backlight", "Backlight compensation", "amp", 8),
    ("focus", "Focus", "cam", 6),
    ("zoom", "Zoom", "cam", 3),
]


@dataclass
class HwProp:
    key: str
    label: str
    iface: object
    pid: int
    lo: int
    hi: int
    step: int
    default: int
    caps: int

    @property
    def can_auto(self):
        return bool(self.caps & FLAG_AUTO)


class HardwareControls:
    """Reads and sets a camera's built-in image settings. Works with any UVC camera;
    only the settings the camera reports are exposed. Use from the GUI thread only."""

    def __init__(self, index):
        self.props = {}
        try:
            from pygrabber.dshow_graph import SystemDeviceEnum
            from pygrabber.dshow_ids import DeviceCategories
            flt, _ = SystemDeviceEnum().get_filter_by_index(DeviceCategories.VideoInputDevice, index)
        except Exception:
            return
        self._filter = flt  # keep the COM object alive
        ifaces = {}
        for key, (name, iid) in {"amp": ("IAMVideoProcAmp", "{C6E13360-30AC-11D0-A18C-00A0C9118956}"),
                                 "cam": ("IAMCameraControl", "{C6E13370-30AC-11D0-A18C-00A0C9118956}")}.items():
            try:
                ifaces[key] = flt.QueryInterface(_make_range_iface(name, iid))
            except Exception:
                pass
        for key, label, iname, pid in HW_PROPS:
            itf = ifaces.get(iname)
            if itf is None:
                continue
            try:
                lo, hi, step, default, caps = itf.GetRange(pid)
            except Exception:
                continue
            if hi > lo:
                self.props[key] = HwProp(key, label, itf, pid, lo, hi, max(step, 1), default, caps)

    def get(self, key):
        p = self.props[key]
        value, flags = p.iface.Get(p.pid)
        return value, bool(flags & FLAG_AUTO)

    def set(self, key, value, auto=False):
        p = self.props[key]
        value = int(min(max(value, p.lo), p.hi))
        try:
            p.iface.Set(p.pid, value, FLAG_AUTO if auto and p.can_auto else FLAG_MANUAL)
            return True
        except Exception:
            return False


def format_hw_value(key, value):
    if key == "exposure":  # DirectShow exposure is log2(seconds)
        secs = 2.0 ** value
        return f"1/{round(1 / secs)} s" if secs < 1 else f"{secs:g} s"
    if key == "white_balance":
        return f"{value} K"
    return str(value)


# --------------------------------------------------------------------------- #
# Software filters
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SoftFilter:
    brightness: int = 0    # -100..100
    contrast: int = 0      # -100..100
    gamma: int = 100       # 20..300 (= 0.20..3.00)
    saturation: int = 0    # -100..100
    vibrance: int = 0      # -100..100

    @staticmethod
    def specs():
        # field, label, min, max, default
        return [("brightness", "Brightness", -100, 100, 0), ("contrast", "Contrast", -100, 100, 0),
                ("gamma", "Gamma", 20, 300, 100), ("saturation", "Saturation", -100, 100, 0),
                ("vibrance", "Vibrance", -100, 100, 0)]


class FilterProcessor:
    """Applies a SoftFilter to BGR frames. Immutable, so the capture thread can use it safely."""

    def __init__(self, f: SoftFilter):
        self.f = f
        self.lut = None
        if (f.brightness, f.contrast, f.gamma) != (0, 0, 100):
            x = np.arange(256, dtype=np.float32) / 255.0
            x = x ** (100.0 / f.gamma)                          # gamma > 1 brightens mid-tones
            k = 1 + f.contrast / 100 * (2 if f.contrast > 0 else 1)  # 0x .. 3x
            x = (x - 0.5) * k + 0.5 + f.brightness / 200.0
            self.lut = np.clip(x * 255 + 0.5, 0, 255).astype(np.uint8)
        self.color = bool(f.saturation or f.vibrance)

    @property
    def identity(self):
        return self.lut is None and not self.color

    def subset(self, include):
        """Processor applying only the filters named in `include`; the rest stay neutral."""
        defaults = {name: default for name, _l, _lo, _hi, default in SoftFilter.specs()}
        return FilterProcessor(SoftFilter(**{name: getattr(self.f, name) if name in include else default
                                             for name, default in defaults.items()}))

    def apply(self, frame):
        if self.lut is not None:
            frame = cv2.LUT(frame, self.lut)
        if self.color and frame.ndim == 3:
            fl = frame.astype(np.float32)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)[..., None]
            k = 1 + self.f.saturation / 100.0
            if self.f.vibrance:
                mx, mn = fl.max(axis=2), fl.min(axis=2)
                sat = (mx - mn) / (mx + 1.0)            # 0 = grey, 1 = fully saturated
                k = k + (self.f.vibrance / 100.0) * (1.0 - sat)[..., None]
            frame = np.clip(gray + (fl - gray) * k, 0, 255).astype(np.uint8)
        return frame


ALL_FILTERS = frozenset(name for name, *_rest in SoftFilter.specs())


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #

# Video formats offered in the Record window: key -> (label, one-line explanation).
FORMATS = {
    "h264": ("H.264 – small files (recommended)",
             "Stores a full frame every few seconds and only what changed in between, so mostly-still "
             "scenes like an arena shrink 20–40× versus MJPEG. Fine for tracking and scoring "
             "(DeepLabCut, SLEAP…)."),
    "mpeg4": ("MPEG-4 – Bonsai-style",
              "The same codec Bonsai writes (FMP4). Very fast and light on CPU; files are a little "
              "larger than H.264 at the same quality."),
    "mjpeg": ("MJPEG – every frame kept whole",
              "Each frame is stored as its own JPEG. Best for frame-by-frame image analysis, but files "
              "are 20–40× bigger. Use for short recordings."),
}
PRESETS = {
    "high": ("High", "hard to tell from the live image"),
    "standard": ("Standard", "good for tracking and scoring"),
    "compact": ("Compact", "smallest files, some softness"),
}
_X264_CRF = {"high": 18, "standard": 23, "compact": 28}
_NVENC_CQ = {"high": 21, "standard": 33, "compact": 41}    # matched to the x264 levels by PSNR and size
_MPEG4_BPP = {"high": 0.15, "standard": 0.06, "compact": 0.025}   # bits per pixel per frame
_MJPEG_QUALITY = {"high": 95, "standard": 85, "compact": 70}
KEYFRAME_SECONDS = 10      # H.264 / MPEG-4: one full frame this often (seeking granularity)


def x264_speed_preset(width, height, fps):
    """Faster (slightly larger) x264 presets when the pixel rate is high, so encoding keeps up."""
    load = width * height * fps / (1920 * 1080 * 30)
    return "veryfast" if load <= 1.2 else "superfast" if load <= 2.5 else "ultrafast"


_nvenc_ok = None
_nvenc_lock = threading.Lock()


def nvenc_available():
    """True if an NVIDIA GPU H.264 encoder can be opened (needs a recent driver). Cached."""
    global _nvenc_ok
    with _nvenc_lock:
        if _nvenc_ok is None:
            try:
                import av
                from fractions import Fraction
                cc = av.CodecContext.create("h264_nvenc", "w")
                cc.width, cc.height, cc.pix_fmt = 256, 256, "yuv420p"
                cc.time_base = Fraction(1, 30)
                cc.open()
                _nvenc_ok = True
            except Exception:
                _nvenc_ok = False
        return _nvenc_ok


class CameraRecorder:
    """Encodes one camera's frames into an .avi (plus a timestamps .csv) on its own thread,
    so slow encoding or disk writes never stall capture. `video_path` may also be a
    BytesIO, which the Record window uses to measure H.264 sizes."""

    def __init__(self, session, camera_name, video_path, csv_path, fps,
                 rec_fps=0, fmt="h264", preset="standard", include=ALL_FILTERS):
        self.session = session
        self.camera_name = camera_name
        self.video_path = video_path
        self.csv_path = csv_path
        self.fps = rec_fps or fps        # written in the file header
        self.interval = 1.0 / rec_fps if rec_fps else 0.0
        self.fmt = fmt
        self.preset = preset
        self.include = include
        self.frames = 0
        self.dropped = 0
        self.bytes_out = 0               # encoded bytes so far (approximate until closed)
        self.first_packet_bytes = 0      # size of the opening keyframe
        self.encoder = fmt               # actual codec used (h264_nvenc / libx264 / ...)
        self._force_cpu = False
        self.error = None
        self._next_due = None
        self._subset = (None, None)      # (display processor, recording processor)
        self._q = queue.Queue(maxsize=240)
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def push(self, raw, shown, proc, t_perf):
        """Called from the capture thread for every frame (raw and as displayed)."""
        if self._closed or t_perf < self.session.t0_perf:
            return
        if self.interval:  # recording below the camera rate: keep evenly spaced frames
            if self._next_due is None:
                self._next_due = t_perf
            if t_perf < self._next_due - self.interval * 0.25:
                return
            self._next_due += self.interval
            if self._next_due < t_perf:  # after a stall, resync instead of bursting
                self._next_due = t_perf + self.interval
        try:
            self._q.put_nowait((self._frame_for(raw, shown, proc), t_perf))
        except queue.Full:
            self.dropped += 1

    def _frame_for(self, raw, shown, proc):
        if proc is None or proc.identity or self.include >= ALL_FILTERS:
            return shown
        if not self.include:
            return raw
        if self._subset[0] is not proc:
            self._subset = (proc, proc.subset(self.include))
        sub = self._subset[1]
        return raw if sub.identity else sub.apply(raw)

    def close(self):
        if not self._closed:
            self._closed = True
            self._q.put(None)

    def join(self, timeout=None):
        self._thread.join(timeout)

    def _open_stream(self, container, size):
        from fractions import Fraction
        rate = Fraction(self.fps).limit_denominator(1001)
        w, h = size
        if self.fmt == "mjpeg":
            stream = container.add_stream("mjpeg", rate=rate)
            stream.pix_fmt = "yuvj420p"
        elif self.fmt == "mpeg4":
            stream = container.add_stream("mpeg4", rate=rate)
            stream.pix_fmt = "yuv420p"
            stream.bit_rate = int(w * h * float(rate) * _MPEG4_BPP[self.preset])
            stream.codec_context.gop_size = max(1, round(float(rate) * KEYFRAME_SECONDS))
        else:
            gop = str(max(1, round(float(rate) * KEYFRAME_SECONDS)))
            if not self._force_cpu and nvenc_available():
                self.encoder = "h264_nvenc"
                stream = container.add_stream("h264_nvenc", rate=rate, options={
                    "preset": "p5", "tune": "hq", "rc": "vbr", "cq": str(_NVENC_CQ[self.preset]),
                    "b": "0", "g": gop, "bf": "3", "spatial-aq": "1", "temporal-aq": "1",
                })
            else:
                self.encoder = "libx264"
                stream = container.add_stream("libx264", rate=rate, options={
                    "preset": x264_speed_preset(w, h, float(rate)),
                    "crf": str(_X264_CRF[self.preset]),
                    "g": gop,
                })
            stream.pix_fmt = "yuv420p"
        stream.width, stream.height = w, h
        stream.time_base = 1 / rate
        return stream

    def _mux(self, container, packets):
        for packet in packets:
            container.mux(packet)
            if not self.first_packet_bytes:
                self.first_packet_bytes = packet.size
            self.bytes_out += packet.size

    def _run(self):
        # MJPEG frames are compressed with libjpeg (which honours quality, unlike OpenCV's
        # own AVI writers); H.264 / MPEG-4 go through FFmpeg. PyAV muxes everything into an
        # OpenDML AVI, so files can grow past 2 GB.
        import contextlib
        import av
        container = stream = size = None
        jpeg_params = [cv2.IMWRITE_JPEG_QUALITY, _MJPEG_QUALITY[self.preset]]
        csv_file = open(self.csv_path, "w", newline="") if self.csv_path else contextlib.nullcontext()
        try:
            with csv_file as fh:
                out = csv.writer(fh) if fh else None
                if out:
                    out.writerow(["frame", "timestamp_unix", "elapsed_s"])
                while True:
                    item = self._q.get()
                    if item is None:
                        break
                    frame, t_perf = item
                    if frame.ndim == 2:
                        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                    if container is None:
                        size = (frame.shape[1], frame.shape[0])
                        container = av.open(self.video_path, "w", format="avi")
                        stream = self._open_stream(container, size)
                    if (frame.shape[1], frame.shape[0]) != size:
                        frame = cv2.resize(frame, size)
                    if self.fmt == "mjpeg":
                        ok, jpeg = cv2.imencode(".jpg", frame, jpeg_params)
                        if not ok:
                            raise OSError("JPEG encoding failed")
                        packet = av.Packet(jpeg.tobytes())
                        packet.stream = stream
                        packet.pts = packet.dts = self.frames
                        packet.time_base = stream.time_base
                        self._mux(container, [packet])
                    else:
                        vf = av.VideoFrame.from_ndarray(frame, format="bgr24")
                        vf.pts = self.frames
                        vf.time_base = stream.time_base
                        try:
                            self._mux(container, stream.encode(vf))
                        except Exception:
                            if self.encoder != "h264_nvenc" or self.frames:
                                raise
                            # The GPU refused this stream (e.g. its limit on simultaneous
                            # encodes was reached): start this file over on the CPU encoder.
                            try:
                                container.close()
                            except Exception:
                                pass
                            self._force_cpu = True
                            self.bytes_out = self.first_packet_bytes = 0
                            if not isinstance(self.video_path, str):
                                self.video_path.seek(0)
                                self.video_path.truncate()
                            container = av.open(self.video_path, "w", format="avi")
                            stream = self._open_stream(container, size)
                            vf.time_base = stream.time_base
                            self._mux(container, stream.encode(vf))
                    if out:
                        elapsed = t_perf - self.session.t0_perf
                        out.writerow([self.frames, f"{self.session.t0_unix + elapsed:.6f}", f"{elapsed:.6f}"])
                    self.frames += 1
                    if out and self.frames % 30 == 0:
                        fh.flush()
                if container is not None and self.fmt != "mjpeg":
                    self._mux(container, stream.encode(None))  # flush frames held by the encoder
        except Exception as exc:
            self.error = str(exc)
            self._closed = True
            while True:  # drain so push() never blocks
                try:
                    if self._q.get_nowait() is None:
                        break
                except queue.Empty:
                    break
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception as exc:
                    self.error = self.error or f"Could not finish {self.video_path}: {exc}"


@dataclass
class RecordConfig:
    camera_indices: list
    folder: str
    basename: str
    limit_s: int = 0              # 0 = no limit
    start_on_ttl: bool = False
    stop_on_ttl: bool = False
    fmt: str = "h264"                                  # key of FORMATS
    preset: str = "standard"                           # key of PRESETS
    include: frozenset = ALL_FILTERS                   # software filters burned into the video
    rec_fps: dict = field(default_factory=dict)        # camera index -> fps (0/missing = full rate)

    def paths_for(self, cam_name):
        """(video, csv) paths. One camera -> exactly the chosen name; several -> name_camera."""
        stem = self.basename if len(self.camera_indices) == 1 else \
            f"{self.basename}_{safe_filename(cam_name)}"
        base = os.path.join(self.folder, stem)
        return base + ".avi", base + "_timestamps.csv"

    @property
    def events_path(self):
        return os.path.join(self.folder, f"{self.basename}_ttl_events.csv")


class RecordingSession:
    """A group of cameras that start and stop together on one shared clock."""

    WAITING, ARMED, RECORDING, DONE = "waiting", "armed", "recording", "done"

    def __init__(self, config: RecordConfig, tiles):
        self.config = config
        self.tiles = list(tiles)
        self.recorders = {}          # tile -> CameraRecorder
        self.t0_perf = float("inf")
        self.t0_unix = 0.0
        self.state = self.ARMED if config.start_on_ttl else self.WAITING
        self.wait_deadline = time.perf_counter() + 8.0
        self.saved = []
        self.errors = []
        self._events = None
        if config.start_on_ttl or config.stop_on_ttl:
            self._events = open(config.events_path, "w", newline="")
            csv.writer(self._events).writerow(["edge", "timestamp_unix", "elapsed_s"])
        for t in self.tiles:
            t.session = self

    def start(self, t_perf=None):
        if self.state not in (self.WAITING, self.ARMED):
            return
        now = time.perf_counter()
        self.t0_perf = t_perf if t_perf is not None else now
        self.t0_unix = time.time() - (now - self.t0_perf)
        self.state = self.RECORDING
        for t in self.tiles:
            video, csv_path = self.config.paths_for(t.cam.name)
            # Header rate: the camera's set rate, unless it is clearly delivering a different one.
            # (Players use this for playback speed; the CSV holds the true frame times.)
            fps = t.fps_box.currentData()
            measured = t.worker.measured_fps if t.worker else 0
            if measured > 1 and abs(measured - fps) > 0.15 * fps:
                fps = measured
            rec = CameraRecorder(self, t.cam.name, video, csv_path, round(float(fps), 2),
                                 self.config.rec_fps.get(t.cam.index, 0),
                                 self.config.fmt, self.config.preset, self.config.include)
            self.recorders[t] = rec
            if t.worker:
                t.worker.recorder = rec

    def elapsed(self):
        return time.perf_counter() - self.t0_perf if self.state == self.RECORDING else 0.0

    def log_edge(self, rising, t_perf):
        if self._events is None:
            return
        elapsed = t_perf - self.t0_perf if self.t0_perf != float("inf") else ""
        unix = time.time() - (time.perf_counter() - t_perf)
        csv.writer(self._events).writerow(["rising" if rising else "falling", f"{unix:.6f}",
                                           f"{elapsed:.6f}" if elapsed != "" else ""])
        self._events.flush()

    def stop_tile(self, tile):
        if tile not in self.tiles:
            return
        self.tiles.remove(tile)
        tile.session = None
        if tile.worker:
            tile.worker.recorder = None
        rec = self.recorders.pop(tile, None)
        if rec:
            rec.close()
            rec.join(10)
            if rec.error:
                self.errors.append(f"{rec.camera_name}: {rec.error}")
            elif rec.frames:
                self.saved.append((rec.video_path, rec.frames, rec.dropped))
        if not self.tiles:
            self._finish()

    def stop(self):
        for t in list(self.tiles):
            self.stop_tile(t)
        self._finish()

    def _finish(self):
        self.state = self.DONE
        if self._events:
            self._events.close()
            self._events = None


# --------------------------------------------------------------------------- #
# TTL trigger input
# --------------------------------------------------------------------------- #

class TriggerSource(QtCore.QObject):
    """Turns TTL edges arriving over a serial port into Qt signals.

    Arduino mode: the board prints a line per edge ("H"/"L", also accepts 1/0,
    HIGH/LOW, START/STOP) at 115200 baud - see ttl_trigger.ino.
    Harp mode: listens for Harp event messages on one register and watches one bit.
    """

    edge = QtCore.Signal(bool, float)      # rising, perf_counter time
    status = QtCore.Signal(str)

    ARDUINO, HARP = "arduino", "harp"
    _HIGH = {"H", "HIGH", "1", "START", "ON", "R", "RISE"}
    _LOW = {"L", "LOW", "0", "STOP", "OFF", "F", "FALL"}

    def __init__(self):
        super().__init__()
        self._serial = None
        self._thread = None
        self._halt = threading.Event()
        self.level = None
        self.connected = False

    def open(self, port, kind, harp_register=32, harp_bit=0):
        import serial
        self.close()
        baud = 1000000 if kind == self.HARP else 115200
        self._serial = serial.Serial(port, baud, timeout=0.05)
        self._halt.clear()
        self.level = None
        self.connected = True
        target = self._run_harp if kind == self.HARP else self._run_text
        args = (harp_register, harp_bit) if kind == self.HARP else ()
        self._thread = threading.Thread(target=target, args=args, daemon=True)
        self._thread.start()
        self.status.emit(f"Connected to {port}")

    def close(self):
        self._halt.set()
        if self._thread:
            self._thread.join(1)
            self._thread = None
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        if self.connected:
            self.connected = False
            self.status.emit("Not connected")

    def simulate(self, rising):
        self._set_level(rising, time.perf_counter())

    def _set_level(self, high, t):
        if high == self.level:
            return
        previous, self.level = self.level, high
        if previous is None and not high:
            return  # first report of a low line is not an edge
        self.edge.emit(high, t)

    def _fail(self, exc):
        self.connected = False
        self.status.emit(f"Disconnected: {exc}")

    def _run_text(self):
        buf = b""
        while not self._halt.is_set():
            try:
                chunk = self._serial.read(64)
            except Exception as exc:
                self._fail(exc)
                return
            t = time.perf_counter()
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                word = line.strip().decode("ascii", "ignore").upper()
                if word in self._HIGH:
                    self._set_level(True, t)
                elif word in self._LOW:
                    self._set_level(False, t)

    def _run_harp(self, register, bit):
        # Put the device in Active mode so it streams events:
        # write OperationControl (address 10, U8) = Active | visual indicators | operation LED.
        msg = [2, 5, 10, 255, 1, 0x61]
        try:
            self._serial.write(bytes(msg + [sum(msg) & 0xFF]))
        except Exception as exc:
            self._fail(exc)
            return
        buf = bytearray()
        while not self._halt.is_set():
            try:
                chunk = self._serial.read(256)
            except Exception as exc:
                self._fail(exc)
                return
            t = time.perf_counter()
            buf += chunk
            while len(buf) >= 2:
                total = buf[1] + 2
                if buf[0] & 0x07 not in (1, 2, 3) or total < 7:
                    del buf[0]
                    continue
                if len(buf) < total:
                    break
                msg = bytes(buf[:total])
                if sum(msg[:-1]) & 0xFF != msg[-1]:
                    del buf[0]  # lost sync, slide forward
                    continue
                del buf[:total]
                mtype, address, ptype = msg[0] & 0x07, msg[2], msg[4]
                if mtype != 3 or address != register:
                    continue
                offset = 5 + (6 if ptype & 0x10 else 0)
                size = max(ptype & 0x0F, 1)
                value = int.from_bytes(msg[offset:offset + size], "little")
                self._set_level(bool(value >> bit & 1), t)


def list_serial_ports():
    try:
        from serial.tools import list_ports
        return [(p.device, f"{p.device} – {p.description}") for p in sorted(list_ports.comports())]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Capture thread
# --------------------------------------------------------------------------- #

class CameraWorker(threading.Thread):
    """Grabs frames from one camera; the GUI polls `frame` / `frame_id`."""

    def __init__(self, index, mode, fps, fallback_mode, processor):
        super().__init__(daemon=True)
        self.index = index
        self.mode = mode
        self.fps = fps
        self.fallback_mode = fallback_mode
        self.processor = processor   # FilterProcessor, swapped by the GUI
        self.recorder = None         # CameraRecorder while recording
        self.raw_frame = None        # latest frame before software filters
        self.probe = None            # CameraRecorder measuring sizes for the Record window
        self.frame = None
        self.frame_id = 0
        self.status = "Opening…"
        self.ok = False
        self.measured_fps = 0.0
        self.active_mode = None
        self.fell_back = False
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def _open(self, mode, fps):
        # The format must be passed at open time: DirectShow ignores a FOURCC
        # set afterwards and silently stays on slow uncompressed YUY2.
        params = [cv2.CAP_PROP_FRAME_WIDTH, mode.width,
                  cv2.CAP_PROP_FRAME_HEIGHT, mode.height,
                  cv2.CAP_PROP_FPS, fps]
        if len(mode.fourcc) == 4:
            params = [cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc)] + params
        with _OPEN_LOCK:  # opening devices concurrently makes DirectShow fail at random
            cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW, params)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _wait_first_frame(self, cap):
        deadline = time.time() + OPEN_TIMEOUT_S
        while time.time() < deadline and not self._halt.is_set():
            ok, frame = cap.read()
            if ok and frame is not None:
                return frame
            time.sleep(0.05)
        return None

    def _publish(self, raw, t_perf):
        proc = self.processor
        frame = raw if proc is None or proc.identity else proc.apply(raw)
        self.raw_frame = raw
        self.frame = frame
        self.frame_id += 1
        for rec in (self.recorder, self.probe):
            if rec is not None:
                rec.push(raw, frame, proc, t_perf)

    def run(self):
        attempts = [(self.mode, self.fps)]
        if self.fallback_mode and self.fallback_mode != self.mode:
            attempts.append((self.fallback_mode, min(self.fps, round(self.fallback_mode.max_fps))))

        cap = None
        for n, (mode, fps) in enumerate(attempts):
            if self._halt.is_set():
                return
            if n:
                self.status = f"Retrying at {mode.width}×{mode.height}…"
            cap = self._open(mode, fps)
            if cap is None:
                continue
            frame = self._wait_first_frame(cap)
            if frame is not None:
                self.active_mode = mode
                self.fell_back = n > 0
                self._publish(frame, time.perf_counter())
                break
            cap.release()
            cap = None

        if cap is None:
            if not self._halt.is_set():
                self.status = ("Could not open camera\n"
                               "(in use by another app, or USB bandwidth is full)")
            return

        self.ok = True
        self.status = ""
        count, t0, fails = 0, time.time(), 0
        while not self._halt.is_set():
            ok, frame = cap.read()
            t_frame = time.perf_counter()
            if not ok or frame is None:
                fails += 1
                if fails > 40:
                    self.ok = False
                    self.status = "Signal lost"
                time.sleep(0.02)
                continue
            fails = 0
            self.ok = True
            self.status = ""
            self._publish(frame, t_frame)
            count += 1
            now = time.time()
            if now - t0 >= 1.0:
                self.measured_fps = count / (now - t0)
                count, t0 = 0, now
        cap.release()


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #

class VideoView(QtWidgets.QWidget):
    """Paints the latest frame scaled to fit, keeping its aspect ratio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._array = None
        self._image = None
        self.overlay = ""
        self.message = "Opening…"
        self.badge = ""                 # top-right recording badge
        self.badge_color = QtGui.QColor(200, 30, 30)
        self.setMinimumSize(160, 90)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent)

    def sizeHint(self):
        return QtCore.QSize(480, 300)

    def set_frame(self, arr):
        arr = np.ascontiguousarray(arr)
        h, w = arr.shape[:2]
        if arr.ndim == 2:
            img = QtGui.QImage(arr.data, w, h, arr.strides[0], QtGui.QImage.Format_Grayscale8)
        else:
            img = QtGui.QImage(arr.data, w, h, arr.strides[0], QtGui.QImage.Format_BGR888)
        self._array, self._image = arr, img  # keep the buffer alive alongside the image
        self.update()

    def clear(self):
        self._array = self._image = None
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(12, 12, 12))
        if self._image is not None:
            size = self._image.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
            target = QtCore.QRect(QtCore.QPoint(0, 0), size)
            target.moveCenter(self.rect().center())
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
            p.drawImage(target, self._image)
        if self.message:
            p.setPen(QtGui.QColor(220, 220, 220))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, self.message)
        fm = p.fontMetrics()
        if self.overlay:
            r = fm.boundingRect(self.overlay).adjusted(-6, -3, 6, 3)
            r.moveTopLeft(QtCore.QPoint(6, 6))
            p.fillRect(r, QtGui.QColor(0, 0, 0, 150))
            p.setPen(QtGui.QColor(235, 235, 235))
            p.drawText(r, QtCore.Qt.AlignCenter, self.overlay)
        if self.badge:
            font = p.font()
            font.setBold(True)
            p.setFont(font)
            r = p.fontMetrics().boundingRect(self.badge).adjusted(-8, -4, 8, 4)
            r.moveTopRight(QtCore.QPoint(self.width() - 6, 6))
            p.fillRect(r, self.badge_color)
            p.setPen(QtCore.Qt.white)
            p.drawText(r, QtCore.Qt.AlignCenter, self.badge)
        p.end()


class CameraTile(QtWidgets.QFrame):
    """One camera: header (name, resolution, FPS, adjust, record) above the video."""

    record_clicked = QtCore.Signal(object)
    adjust_clicked = QtCore.Signal(object)

    def __init__(self, cam: CameraInfo, parent=None):
        super().__init__(parent)
        self.cam = cam
        self.worker = None
        self.session = None
        self._hw = None
        self._hw_applied = False
        self._last_id = -1
        self._synced_fallback = False
        self.soft = load_soft_filter(cam.name)
        self.processor = FilterProcessor(self.soft)

        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        name = QtWidgets.QLabel(cam.name)
        name.setStyleSheet("font-weight: 600;")
        name.setToolTip(f"{cam.name} (device {cam.index})")
        name.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        name.setMinimumWidth(90)

        self.mode_box = QtWidgets.QComboBox()
        for m in cam.modes:
            self.mode_box.addItem(m.label, m)
        self.mode_box.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.mode_box.setMinimumContentsLength(7)
        self.mode_box.setToolTip("Resolution and format")
        self.fps_box = QtWidgets.QComboBox()
        self.fps_box.setToolTip("Requested frame rate")
        self._restore_mode()
        self._fill_fps(select=settings().value(f"camera/{cam.name}/fps", None, int))

        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        self.fps_box.currentIndexChanged.connect(self._on_fps_changed)

        self.adjust_btn = QtWidgets.QToolButton()
        self.adjust_btn.setText("⚙")
        self.adjust_btn.setToolTip("Image adjustments")
        self.adjust_btn.clicked.connect(lambda: self.adjust_clicked.emit(self))
        self.rec_btn = QtWidgets.QToolButton()
        self.rec_btn.clicked.connect(lambda: self.record_clicked.emit(self))
        self._style_rec_button()

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(6, 4, 6, 4)
        header.addWidget(name, 1)
        header.addWidget(self.mode_box)
        header.addWidget(self.fps_box)
        header.addWidget(self.adjust_btn)
        header.addWidget(self.rec_btn)

        self.view = VideoView()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(header)
        lay.addWidget(self.view, 1)

    # -- settings ---------------------------------------------------------- #

    def _restore_mode(self):
        saved = settings().value(f"camera/{self.cam.name}/mode", "")
        for i in range(self.mode_box.count()):
            if self.mode_box.itemData(i).label == saved:
                self.mode_box.setCurrentIndex(i)
                return

    def _fill_fps(self, select=None):
        mode = self.mode_box.currentData()
        self.fps_box.blockSignals(True)
        self.fps_box.clear()
        for v in fps_options(mode.max_fps):
            self.fps_box.addItem(f"{v} fps", v)
        i = self.fps_box.findData(select) if select is not None else 0
        self.fps_box.setCurrentIndex(max(i, 0))
        self.fps_box.blockSignals(False)

    def _on_mode_changed(self, _i):
        self._fill_fps()
        settings().setValue(f"camera/{self.cam.name}/mode", self.mode_box.currentData().label)
        self.restart()

    def _on_fps_changed(self, _i):
        settings().setValue(f"camera/{self.cam.name}/fps", self.fps_box.currentData())
        self.restart()

    def _fallback_mode(self):
        same = [m for m in self.cam.modes if m.fourcc == PREFERRED_FOURCC] or self.cam.modes
        near_vga = [m for m in same if m.width * m.height <= 640 * 480]
        return max(near_vga, key=lambda m: m.width * m.height) if near_vga else \
            min(same, key=lambda m: m.width * m.height)

    @property
    def hw(self):
        if self._hw is None:
            self._hw = HardwareControls(self.cam.index)
        return self._hw

    def set_soft_filter(self, f: SoftFilter):
        self.soft = f
        self.processor = FilterProcessor(f)
        if self.worker:
            self.worker.processor = self.processor
        save_soft_filter(self.cam.name, f)

    def _apply_saved_hw(self):
        """Re-apply hardware settings the user changed in an earlier run."""
        s = settings()
        s.beginGroup(f"camera/{self.cam.name}/hw")
        for key in s.childKeys():
            if key in self.hw.props:
                value, auto = str(s.value(key)).split(":")
                self.hw.set(key, int(value), auto == "1")
        s.endGroup()

    # -- recording state --------------------------------------------------- #

    @property
    def recording(self):
        return self.session is not None

    def _style_rec_button(self):
        busy = self.recording
        self.rec_btn.setText("■ Stop" if busy else "● Rec")
        self.rec_btn.setToolTip("Stop this camera's recording" if busy else "Record this camera")
        self.rec_btn.setStyleSheet("color: white; background: #b3261e; font-weight: 600;"
                                   if busy else "color: #ff5a4f; font-weight: 600;")
        self.mode_box.setEnabled(not busy)
        self.fps_box.setEnabled(not busy)

    # -- lifecycle --------------------------------------------------------- #

    def start(self):
        self.stop()
        mode, fps = self.mode_box.currentData(), self.fps_box.currentData()
        self.worker = CameraWorker(self.cam.index, mode, fps, self._fallback_mode(), self.processor)
        self._last_id = -1
        self._synced_fallback = False
        self._hw_applied = False
        self.view.clear()
        self.view.overlay = ""
        self.view.message = "Opening…"
        self.worker.start()

    def restart(self):
        if not self.recording:
            self.start()

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.worker.join(timeout=5)
            self.worker = None

    def tick(self):
        w = self.worker
        if not w:
            return
        if w.frame_id != self._last_id and w.frame is not None:
            self._last_id = w.frame_id
            self.view.set_frame(w.frame)
        if w.ok and not self._hw_applied:
            self._hw_applied = True
            self._apply_saved_hw()
        if w.fell_back and not self._synced_fallback:
            self._synced_fallback = True
            i = self.mode_box.findData(w.active_mode)
            if i >= 0:
                self.mode_box.blockSignals(True)
                self.mode_box.setCurrentIndex(i)
                self.mode_box.blockSignals(False)
                self._fill_fps()

        message = w.status
        overlay = ""
        if w.ok and w.frame is not None:
            h, wd = w.frame.shape[:2]
            overlay = f"{wd}×{h} · {w.measured_fps:.1f} fps"
            if w.fell_back:
                overlay += " · lowered resolution (bandwidth)"

        badge, color = "", self.view.badge_color
        s = self.session
        if s is not None:
            if s.state == RecordingSession.RECORDING:
                badge = f"● REC {fmt_hms(s.elapsed())}"
                if s.config.limit_s:
                    badge += f" / {fmt_hms(s.config.limit_s)}"
                color = QtGui.QColor(200, 30, 30)
            elif s.state == RecordingSession.ARMED:
                badge, color = "◌ ARMED – waiting for TTL", QtGui.QColor(200, 120, 0)
            elif s.state == RecordingSession.WAITING:
                badge, color = "Starting…", QtGui.QColor(200, 120, 0)
        if self.rec_btn.text().startswith("■") != self.recording:
            self._style_rec_button()

        if (message, overlay, badge) != (self.view.message, self.view.overlay, self.view.badge):
            self.view.message, self.view.overlay, self.view.badge = message, overlay, badge
            self.view.badge_color = color
            self.view.update()


def load_soft_filter(cam_name):
    s = settings()
    values = {}
    for name, *_rest in SoftFilter.specs():
        v = s.value(f"camera/{cam_name}/soft/{name}", None)
        if v is not None:
            values[name] = int(v)
    return SoftFilter(**values)


def save_soft_filter(cam_name, f: SoftFilter):
    s = settings()
    for name, *_rest in SoftFilter.specs():
        s.setValue(f"camera/{cam_name}/soft/{name}", getattr(f, name))


class AdjustmentsDialog(QtWidgets.QDialog):
    """Hardware camera settings + software filters for one camera."""

    AUTO_BY_DEFAULT = {"exposure", "white_balance", "focus"}

    def __init__(self, tile: CameraTile, parent=None):
        super().__init__(parent)
        self.tile = tile
        self.setWindowTitle(f"Adjustments – {tile.cam.name}")
        self.setMinimumWidth(460)
        lay = QtWidgets.QVBoxLayout(self)

        # Hardware
        hw_box = QtWidgets.QGroupBox("Camera settings (applied inside the camera)")
        grid = QtWidgets.QGridLayout(hw_box)
        self.hw_rows = {}
        hw = tile.hw
        if not hw.props:
            grid.addWidget(QtWidgets.QLabel("This camera doesn't expose any adjustable settings."), 0, 0)
        for row, p in enumerate(hw.props.values()):
            try:
                value, auto = hw.get(p.key)
            except Exception:
                value, auto = p.default, False
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(p.lo, p.hi)
            slider.setSingleStep(p.step)
            slider.setValue(value)
            label = QtWidgets.QLabel(format_hw_value(p.key, value))
            label.setMinimumWidth(64)
            auto_box = QtWidgets.QCheckBox("Auto")
            auto_box.setVisible(p.can_auto)
            auto_box.setChecked(auto and p.can_auto)
            slider.setEnabled(not auto_box.isChecked())
            grid.addWidget(QtWidgets.QLabel(p.label), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(label, row, 2)
            grid.addWidget(auto_box, row, 3)
            self.hw_rows[p.key] = (slider, label, auto_box)
            slider.valueChanged.connect(lambda _v, k=p.key: self._hw_changed(k))
            auto_box.toggled.connect(lambda _v, k=p.key: self._hw_changed(k))
        grid.setColumnStretch(1, 1)
        hw_reset = QtWidgets.QPushButton("Reset camera settings")
        hw_reset.clicked.connect(self._reset_hw)
        hw_reset.setEnabled(bool(hw.props))
        grid.addWidget(hw_reset, len(hw.props) + 1, 0, 1, 4)
        lay.addWidget(hw_box)

        # Software
        sw_box = QtWidgets.QGroupBox("Image filters (software – also saved in recordings)")
        grid = QtWidgets.QGridLayout(sw_box)
        self.sw_rows = {}
        for row, (name, label_text, lo, hi, _default) in enumerate(SoftFilter.specs()):
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(lo, hi)
            slider.setValue(getattr(tile.soft, name))
            label = QtWidgets.QLabel()
            label.setMinimumWidth(64)
            grid.addWidget(QtWidgets.QLabel(label_text), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(label, row, 2)
            self.sw_rows[name] = (slider, label)
            slider.valueChanged.connect(self._sw_changed)
        grid.setColumnStretch(1, 1)
        note = QtWidgets.QLabel("Saturation and vibrance have no effect on black-and-white cameras.")
        note.setStyleSheet("color: #8a8a8a;")
        grid.addWidget(note, len(self.sw_rows), 0, 1, 3)
        sw_reset = QtWidgets.QPushButton("Reset filters")
        sw_reset.clicked.connect(self._reset_sw)
        grid.addWidget(sw_reset, len(self.sw_rows) + 1, 0, 1, 3)
        lay.addWidget(sw_box)
        self._update_sw_labels()

        close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        close.rejected.connect(self.close)
        lay.addWidget(close)

    def _hw_changed(self, key):
        slider, label, auto_box = self.hw_rows[key]
        auto = auto_box.isChecked()
        slider.setEnabled(not auto)
        self.tile.hw.set(key, slider.value(), auto)
        label.setText(format_hw_value(key, slider.value()))
        settings().setValue(f"camera/{self.tile.cam.name}/hw/{key}",
                            f"{slider.value()}:{1 if auto else 0}")

    def _reset_hw(self):
        s = settings()
        for key, (slider, _label, auto_box) in self.hw_rows.items():
            p = self.tile.hw.props[key]
            for w in (slider, auto_box):
                w.blockSignals(True)
            slider.setValue(p.default)
            auto_box.setChecked(p.can_auto and key in self.AUTO_BY_DEFAULT)
            for w in (slider, auto_box):
                w.blockSignals(False)
            self._hw_changed(key)
            s.remove(f"camera/{self.tile.cam.name}/hw/{key}")

    def _current_soft(self):
        return SoftFilter(**{k: s.value() for k, (s, _l) in self.sw_rows.items()})

    def _update_sw_labels(self):
        for name, (slider, label) in self.sw_rows.items():
            v = slider.value()
            label.setText(f"{v / 100:.2f}" if name == "gamma" else f"{v:+d}" if v else "0")

    def _sw_changed(self, _v):
        self._update_sw_labels()
        self.tile.set_soft_filter(self._current_soft())

    def _reset_sw(self):
        for name, _label, _lo, _hi, default in SoftFilter.specs():
            slider = self.sw_rows[name][0]
            slider.blockSignals(True)
            slider.setValue(default)
            slider.blockSignals(False)
        self._sw_changed(0)


AVI_FRAME_OVERHEAD = 600    # bytes per frame for AVI chunk headers and indexes
_ROUGH_BPP = {             # bytes per pixel per frame, typical lab arena (used before measuring)
    ("mjpeg", "high"): 0.16, ("mjpeg", "standard"): 0.09, ("mjpeg", "compact"): 0.06,
    ("h264", "high"): 0.016, ("h264", "standard"): 0.004, ("h264", "compact"): 0.0013,
}


def camera_rate(tile, cam):
    """Nominal frame rate the camera is set to deliver."""
    if tile is not None:
        return tile.fps_box.currentData()
    return round(cam.modes[0].max_fps)


def recording_rate(tile, cam, rec_fps):
    fps = camera_rate(tile, cam)
    if tile is not None and tile.worker and tile.worker.measured_fps > 1:
        fps = min(fps, tile.worker.measured_fps)
    return min(fps, rec_fps) if rec_fps else fps


def estimate_bytes_per_second(tile, cam, fmt, preset, include, rec_fps, h264_bytes_per_frame=None):
    """-> (bytes per second, measured?). MJPEG: encodes the live frame. MPEG-4: exact, it is
    bitrate-controlled. H.264: needs a SizeProbe measurement, otherwise a rough guess."""
    fps = recording_rate(tile, cam, rec_fps)
    mode = tile.mode_box.currentData() if tile is not None else cam.modes[0]
    pixels = mode.width * mode.height
    if fmt == "mpeg4":
        return pixels * fps * _MPEG4_BPP[preset] / 8 + AVI_FRAME_OVERHEAD * fps, True
    if fmt == "h264":
        if h264_bytes_per_frame is not None:
            return (h264_bytes_per_frame + AVI_FRAME_OVERHEAD) * fps, True
        return pixels * fps * _ROUGH_BPP[("h264", preset)], False
    w = tile.worker if tile is not None else None
    if w is not None and w.raw_frame is not None:
        proc = tile.processor.subset(include)
        frame = w.raw_frame if proc.identity else proc.apply(w.raw_frame)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, _MJPEG_QUALITY[preset]])
        if ok:
            return (len(buf) + AVI_FRAME_OVERHEAD) * fps, True
    return pixels * fps * _ROUGH_BPP[("mjpeg", preset)], False


class SizeProbe:
    """Encodes a few seconds of a camera's live view to memory to measure real H.264 size
    (which depends heavily on how much moves in the scene)."""

    DURATION = 4.0

    def __init__(self, tile, preset, include, rec_fps):
        import io
        from types import SimpleNamespace
        self.tile = tile
        self.started = time.perf_counter()
        session = SimpleNamespace(t0_perf=self.started, t0_unix=time.time())
        fps = recording_rate(tile, tile.cam, rec_fps)
        self.rec = CameraRecorder(session, tile.cam.name, io.BytesIO(), None, fps,
                                  rec_fps, "h264", preset, include)
        self.result = None
        tile.worker.probe = self.rec

    def poll(self):
        """Returns bytes per frame once measured, else None."""
        if self.result is None and time.perf_counter() - self.started >= self.DURATION:
            self.cancel()
            self.rec.join(5)
            r = self.rec
            if r.frames > 2 and not r.error:
                # Spread the opening keyframe as it would be over a long recording.
                key = r.first_packet_bytes
                per_frame = (r.bytes_out - key) / (r.frames - 1)
                self.result = per_frame + key / max(1.0, r.fps * KEYFRAME_SECONDS)
        return self.result

    def cancel(self):
        w = self.tile.worker
        if w is not None and w.probe is self.rec:
            w.probe = None
        self.rec.close()


def fmt_gb(n_bytes):
    gb = n_bytes / 1e9
    return f"{gb:.1f} GB" if gb < 100 else f"{gb:.0f} GB"


class RecordDialog(QtWidgets.QDialog):
    """Pick cameras and per-camera frame rate, save folder, file name, time limit,
    quality, which filters go into the video, and start mode. Shows a live size estimate."""

    COL_CAM, COL_RES, COL_FPS, COL_SIZE = range(4)

    def __init__(self, cameras, tiles, preselect, busy, trigger_ready, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record")
        self.setMinimumWidth(640)
        self.cameras = {c.index: c for c in cameras}
        self.tiles = tiles
        s = settings()
        lay = QtWidgets.QVBoxLayout(self)

        # Cameras ---------------------------------------------------------- #
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Camera", "Resolution", "Record at", "≈ per hour"])
        self.tree.setRootIsDecorated(False)
        self.tree.setMaximumHeight(170)
        self.fps_boxes = {}
        for cam in cameras:
            tile = tiles.get(cam.index)
            mode = tile.mode_box.currentData() if tile else cam.modes[0]
            item = QtWidgets.QTreeWidgetItem([cam.name, f"{mode.width}×{mode.height}", "", ""])
            item.setData(self.COL_CAM, QtCore.Qt.UserRole, cam.index)
            if cam.index in busy:
                item.setText(self.COL_CAM, cam.name + "  (recording)")
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEnabled)
                item.setCheckState(self.COL_CAM, QtCore.Qt.Unchecked)
            else:
                item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
                item.setCheckState(self.COL_CAM, QtCore.Qt.Checked if cam.index in preselect
                                   else QtCore.Qt.Unchecked)
            self.tree.addTopLevelItem(item)
            box = QtWidgets.QComboBox()
            rate = camera_rate(tile, cam)
            box.addItem(f"Full rate ({rate} fps)", 0)
            for v in (60, 30, 25, 20, 15, 10, 5, 2, 1):
                if v < rate:
                    box.addItem(f"{v} fps", v)
            i = box.findData(s.value(f"camera/{cam.name}/rec_fps", 0, int))
            box.setCurrentIndex(max(i, 0))
            box.setEnabled(cam.index not in busy)
            box.currentIndexChanged.connect(self._refresh)
            self.tree.setItemWidget(item, self.COL_FPS, box)
            self.fps_boxes[cam.index] = box
        for col in range(4):
            self.tree.resizeColumnToContents(col)
        self.tree.setColumnWidth(self.COL_CAM, max(self.tree.columnWidth(self.COL_CAM), 220))
        self.tree.itemChanged.connect(self._refresh)
        lay.addWidget(QtWidgets.QLabel("<b>Cameras</b> – ticked cameras start and stop together"))
        lay.addWidget(self.tree)

        # Files ------------------------------------------------------------ #
        form = QtWidgets.QFormLayout()
        lay.addLayout(form)
        folder_row = QtWidgets.QHBoxLayout()
        default_dir = os.path.join(os.path.expanduser("~"), "Videos")
        self.folder = QtWidgets.QLineEdit(s.value("record/folder", default_dir))
        self.folder.editingFinished.connect(self._refresh)
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder, 1)
        folder_row.addWidget(browse)
        form.addRow("Save to:", folder_row)

        self.name = QtWidgets.QLineEdit(datetime.now().strftime("recording_%Y-%m-%d_%H-%M-%S"))
        self.name.textChanged.connect(self._refresh)
        form.addRow("File name:", self.name)

        limit_row = QtWidgets.QHBoxLayout()
        self.limit_on = QtWidgets.QCheckBox("Stop automatically after")
        self.limit_on.setChecked(s.value("record/limit_on", False, bool))
        self.limit = QtWidgets.QTimeEdit(QtCore.QTime.fromString(s.value("record/limit", "01:00:00"), "HH:mm:ss"))
        self.limit.setDisplayFormat("HH:mm:ss")
        self.limit.setEnabled(self.limit_on.isChecked())
        self.limit_on.toggled.connect(self.limit.setEnabled)
        self.limit_on.toggled.connect(self._refresh)
        self.limit.timeChanged.connect(self._refresh)
        limit_row.addWidget(self.limit_on)
        limit_row.addWidget(self.limit)
        limit_row.addWidget(QtWidgets.QLabel("(h:m:s)"))
        limit_row.addStretch(1)
        form.addRow("Time limit:", limit_row)

        start_box = QtWidgets.QVBoxLayout()
        self.start_now = QtWidgets.QRadioButton("Start now")
        self.start_ttl = QtWidgets.QRadioButton("Wait for TTL trigger (starts on rising edge)")
        self.stop_ttl = QtWidgets.QCheckBox("Also stop on TTL falling edge")
        self.start_now.setChecked(True)
        self.start_ttl.setEnabled(trigger_ready)
        if not trigger_ready:
            self.start_ttl.setText(self.start_ttl.text() + " – connect a trigger first")
        elif s.value("record/ttl", False, bool):
            self.start_ttl.setChecked(True)
        self.stop_ttl.setEnabled(trigger_ready)
        self.stop_ttl.setChecked(trigger_ready and s.value("record/stop_ttl", False, bool))
        self.start_ttl.toggled.connect(self._refresh)
        for w in (self.start_now, self.start_ttl, self.stop_ttl):
            start_box.addWidget(w)
        form.addRow("Start:", start_box)

        # Quality & filters ------------------------------------------------ #
        qbox = QtWidgets.QGroupBox("Video format, quality and filters")
        ql = QtWidgets.QGridLayout(qbox)
        self.fmt = QtWidgets.QComboBox()
        for key, (label, _info) in FORMATS.items():
            self.fmt.addItem(label, key)
        self.fmt.setCurrentIndex(max(self.fmt.findData(s.value("record/format", "h264")), 0))
        self.fmt_info = QtWidgets.QLabel()
        self.fmt_info.setWordWrap(True)
        self.fmt_info.setStyleSheet("color: #9a9a9a;")
        self.preset = QtWidgets.QComboBox()
        for key, (label, hint) in PRESETS.items():
            self.preset.addItem(f"{label} \u2013 {hint}", key)
        self.preset.setCurrentIndex(max(self.preset.findData(s.value("record/preset", "standard")), 0))
        self.fmt.currentIndexChanged.connect(self._refresh)
        self.preset.currentIndexChanged.connect(self._refresh)
        ql.addWidget(QtWidgets.QLabel("Format:"), 0, 0)
        ql.addWidget(self.fmt, 0, 1, 1, 2)
        ql.addWidget(self.fmt_info, 1, 1, 1, 2)
        ql.addWidget(QtWidgets.QLabel("Quality:"), 2, 0)
        ql.addWidget(self.preset, 2, 1, 1, 2)
        saved_include = s.value("record/include", None)
        include = ALL_FILTERS if saved_include is None else \
            frozenset(x for x in str(saved_include).split(":") if x)
        inc_row = QtWidgets.QHBoxLayout()
        self.include_boxes = {}
        for name, label, *_rest in SoftFilter.specs():
            cb = QtWidgets.QCheckBox(label)
            cb.setChecked(name in include)
            cb.toggled.connect(self._refresh)
            inc_row.addWidget(cb)
            self.include_boxes[name] = cb
        inc_row.addStretch(1)
        ql.addWidget(QtWidgets.QLabel("Filters in video:"), 3, 0)
        ql.addLayout(inc_row, 3, 1, 1, 2)
        note = QtWidgets.QLabel("Unticked filters still show on screen but are left out of the file. "
                                "Camera settings (exposure, gain…) are always in the video.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #8a8a8a;")
        ql.addWidget(note, 4, 0, 1, 3)
        ql.setColumnStretch(1, 1)
        lay.addWidget(qbox)

        # Estimate --------------------------------------------------------- #
        self.preview = QtWidgets.QLabel()
        self.preview.setStyleSheet("color: #9a9a9a;")
        self.preview.setWordWrap(True)
        lay.addWidget(self.preview)
        self.size_bar = QtWidgets.QProgressBar()
        self.size_bar.setTextVisible(True)
        lay.addWidget(self.size_bar)
        self.size_label = QtWidgets.QLabel()
        self.size_label.setWordWrap(True)
        lay.addWidget(self.size_label)

        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.ok_btn = buttons.button(QtWidgets.QDialogButtonBox.Ok)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.config = None
        self.probes = {}          # camera index -> (settings key, SizeProbe)
        self.probe_results = {}   # (camera index, settings key) -> bytes per frame

        # Live frames change, so keep the estimate current while the dialog is open.
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(700)
        self._refresh()

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Save recordings to", self.folder.text())
        if d:
            self.folder.setText(os.path.normpath(d))
            self._refresh()

    def _items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def _selected(self):
        return [it.data(self.COL_CAM, QtCore.Qt.UserRole) for it in self._items()
                if it.checkState(self.COL_CAM) == QtCore.Qt.Checked]

    def _include(self):
        return frozenset(n for n, cb in self.include_boxes.items() if cb.isChecked())

    def _build_config(self):
        sel = self._selected()
        limit = QtCore.QTime(0, 0).secsTo(self.limit.time()) if self.limit_on.isChecked() else 0
        name = safe_filename(self.name.text()) if self.name.text().strip() else ""
        return RecordConfig(
            sel, self.folder.text().strip(), name, limit, self.start_ttl.isChecked(),
            self.stop_ttl.isChecked() and self.stop_ttl.isEnabled(), self.fmt.currentData(),
            self.preset.currentData(), self._include(), {i: self.fps_boxes[i].currentData() for i in sel})

    def _h264_measure(self, idx, cfg):
        """-> (bytes per frame or None, still measuring?). Starts a SizeProbe when needed."""
        tile = self.tiles.get(idx)
        if tile is None or tile.worker is None or not tile.worker.ok:
            return None, False
        key = (cfg.preset, cfg.include, cfg.rec_fps.get(idx, 0), tile.mode_box.currentData(),
               tile.fps_box.currentData(), tile.soft)
        if (idx, key) in self.probe_results:
            return self.probe_results[(idx, key)], False
        current = self.probes.get(idx)
        if current is None or current[0] != key:
            if current:
                current[1].cancel()
            if tile.worker.probe is not None:   # another probe is still running on this camera
                return None, True
            current = (key, SizeProbe(tile, cfg.preset, cfg.include, cfg.rec_fps.get(idx, 0)))
            self.probes[idx] = current
        result = current[1].poll()
        if result is None:
            return None, current[1].rec.error is None
        self.probe_results[(idx, key)] = result
        del self.probes[idx]
        return result, False

    def done(self, r):
        for _key, probe in self.probes.values():
            probe.cancel()
        self.probes.clear()
        self._timer.stop()
        super().done(r)

    def _free_space(self, folder):
        path = os.path.abspath(folder or ".")
        while path and not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                return None, ""
            path = parent
        try:
            return shutil.disk_usage(path).free, os.path.splitdrive(path)[0] or path
        except OSError:
            return None, ""

    def _refresh(self, *_):
        cfg = self._build_config()
        info = FORMATS[cfg.fmt][1]
        if cfg.fmt == "h264":
            info += ("  Encoding on: NVIDIA graphics card (light on CPU)." if nvenc_available()
                     else "  Encoding on: CPU (no compatible NVIDIA graphics card found).")
        self.fmt_info.setText(info)

        total, rough, measuring = 0.0, False, False
        for it in self._items():
            idx = it.data(self.COL_CAM, QtCore.Qt.UserRole)
            checked = idx in cfg.camera_indices
            if not checked:
                it.setText(self.COL_SIZE, "")
                continue
            per_frame, busy = self._h264_measure(idx, cfg) if cfg.fmt == "h264" else (None, False)
            bps, measured = estimate_bytes_per_second(self.tiles.get(idx), self.cameras[idx], cfg.fmt,
                                                      cfg.preset, cfg.include, cfg.rec_fps.get(idx, 0),
                                                      per_frame)
            total += bps
            measuring |= busy
            rough |= not measured and not busy
            it.setText(self.COL_SIZE, ("\u2248 " if measured else "~ ") + fmt_gb(bps * 3600)
                       + (" (measuring\u2026)" if busy else "" if measured else " (rough)"))

        self.ok_btn.setText("Arm (wait for TTL)" if cfg.start_on_ttl else "Start recording")
        self.ok_btn.setEnabled(bool(cfg.camera_indices and cfg.basename))
        if not cfg.camera_indices:
            self.preview.setText("Tick at least one camera.")
        else:
            names = [os.path.basename(cfg.paths_for(self.cameras[i].name)[0]) for i in cfg.camera_indices]
            self.preview.setText("Will save: " + ", ".join(names) + "  (+ a _timestamps.csv for each)")

        per_hour = total * 3600
        free, drive = self._free_space(cfg.folder)
        planned = per_hour * (cfg.limit_s / 3600 if cfg.limit_s else 1.0)
        note = " (measuring\u2026)" if measuring else \
            " (rough \u2013 some cameras aren't open)" if rough else ""
        parts = [f"<b>\u2248 {fmt_gb(per_hour)} per hour</b>{note}"]
        if cfg.limit_s:
            parts.append(f"this recording ({fmt_hms(cfg.limit_s)}) ≈ <b>{fmt_gb(planned)}</b>")
        if free is not None:
            hours = free / per_hour if per_hour else float("inf")
            lasts = "" if not per_hour else \
                f" – enough for ≈ {hours:.1f} h" if hours < 100 else " – enough for 100+ h"
            parts.append(f"{fmt_gb(free)} free on {drive}{lasts}")
        self.size_label.setText(" · ".join(parts))

        if free:
            frac = min(planned / free, 1.0) if total else 0.0
            self.size_bar.setValue(int(frac * 100))
            self.size_bar.setFormat(f"{'This recording' if cfg.limit_s else '1 hour'} uses %p% of free space")
            color = "#3fa34d" if frac < 0.5 else "#d08a1e" if frac < 0.9 else "#c0392b"
            self.size_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")
        else:
            self.size_bar.setValue(0)
            self.size_bar.setFormat("Free space unknown")

    def _accept(self):
        cfg = self._build_config()
        if not cfg.folder or not cfg.camera_indices:
            return
        try:
            os.makedirs(cfg.folder, exist_ok=True)
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Record", f"Can't use that folder:\n{exc}")
            return
        targets = [p for i in cfg.camera_indices for p in cfg.paths_for(self.cameras[i].name)]
        if cfg.start_on_ttl or cfg.stop_on_ttl:
            targets.append(cfg.events_path)
        existing = [os.path.basename(p) for p in targets if os.path.exists(p)]
        if existing and QtWidgets.QMessageBox.question(
                self, "Overwrite?", "These files already exist and will be overwritten:\n\n"
                + "\n".join(existing)) != QtWidgets.QMessageBox.Yes:
            return
        s = settings()
        s.setValue("record/folder", cfg.folder)
        s.setValue("record/limit_on", self.limit_on.isChecked())
        s.setValue("record/limit", self.limit.time().toString("HH:mm:ss"))
        s.setValue("record/ttl", cfg.start_on_ttl)
        s.setValue("record/stop_ttl", self.stop_ttl.isChecked())
        s.setValue("record/format", cfg.fmt)
        s.setValue("record/preset", cfg.preset)
        s.setValue("record/include", ":".join(sorted(cfg.include)))
        for i in cfg.camera_indices:
            s.setValue(f"camera/{self.cameras[i].name}/rec_fps", cfg.rec_fps.get(i, 0))
        self.config = cfg
        self.accept()


class FloatingWindow(QtWidgets.QWidget):
    """Top-level window that holds one CameraTile in 'separate windows' mode."""

    closed_by_user = QtCore.Signal(int)

    def __init__(self, tile: CameraTile):
        super().__init__(None, QtCore.Qt.Window)
        self.tile = tile
        self._app_closing = False
        self.setWindowTitle(f"{tile.cam.name} – {APP_NAME}")
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(tile)
        tile.show()

    def release_tile(self):
        self.tile.setParent(None)
        self._app_closing = True
        self.close()
        self.deleteLater()

    def closeEvent(self, event):
        if not self._app_closing:
            self.closed_by_user.emit(self.tile.cam.index)
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #

class MainWindow(QtWidgets.QMainWindow):
    MODE_GRID, MODE_SEPARATE = "grid", "separate"

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 800)
        self.cameras = []
        self.tiles = {}       # camera index -> CameraTile
        self.floating = {}    # camera index -> FloatingWindow
        self.sessions = []    # active RecordingSessions
        self.adjust_dialogs = {}
        self.row_splitters = []
        self.grid_root = None
        self.layout_mode = self.MODE_GRID
        self.trigger = TriggerSource()
        self.trigger.edge.connect(self.on_trigger_edge)
        self.trigger.status.connect(self._on_trigger_status)

        # Central area: either the camera grid or a hint label.
        self.central = QtWidgets.QWidget()
        self.central_lay = QtWidgets.QVBoxLayout(self.central)
        self.central_lay.setContentsMargins(4, 4, 4, 4)
        self.hint = QtWidgets.QLabel()
        self.hint.setAlignment(QtCore.Qt.AlignCenter)
        self.hint.setStyleSheet("color: #9a9a9a; font-size: 15px;")
        self.central_lay.addWidget(self.hint)
        self.setCentralWidget(self.central)

        self._build_sidebar()
        self._build_menu()
        self.statusBar()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(15)

        self.refresh_cameras()
        self.relayout()

    # -- UI construction --------------------------------------------------- #

    def _build_sidebar(self):
        panel = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(panel)

        lay.addWidget(QtWidgets.QLabel("<b>Cameras</b> – tick to show"))
        self.cam_list = QtWidgets.QListWidget()
        self.cam_list.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.cam_list, 1)

        row = QtWidgets.QHBoxLayout()
        for text, slot in (("All", self.select_all), ("None", self.select_none),
                           ("Refresh", self.refresh_cameras)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        box = QtWidgets.QGroupBox("Layout")
        bl = QtWidgets.QVBoxLayout(box)
        self.rb_grid = QtWidgets.QRadioButton("One window (grid)")
        self.rb_sep = QtWidgets.QRadioButton("Separate windows")
        self.rb_grid.setChecked(True)
        self.rb_grid.toggled.connect(self._on_layout_toggled)
        bl.addWidget(self.rb_grid)
        bl.addWidget(self.rb_sep)
        retile = QtWidgets.QPushButton("Re-tile")
        retile.setToolTip("Snap everything back into an even grid (Ctrl+T)")
        retile.clicked.connect(self.retile)
        bl.addWidget(retile)
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("Recording")
        bl = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        self.record_btn = QtWidgets.QPushButton("● Record…")
        self.record_btn.setStyleSheet("color: #ff5a4f; font-weight: 600;")
        self.record_btn.setToolTip("Record one or several cameras together (Ctrl+R)")
        self.record_btn.clicked.connect(lambda: self.open_record_dialog())
        self.stop_all_btn = QtWidgets.QPushButton("■ Stop all")
        self.stop_all_btn.clicked.connect(self.stop_all_recordings)
        row.addWidget(self.record_btn)
        row.addWidget(self.stop_all_btn)
        bl.addLayout(row)
        self.rec_status = QtWidgets.QLabel("Not recording")
        self.rec_status.setWordWrap(True)
        bl.addWidget(self.rec_status)
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("TTL trigger")
        bl = QtWidgets.QGridLayout(box)
        self.trig_kind = QtWidgets.QComboBox()
        self.trig_kind.addItem("Arduino (serial)", TriggerSource.ARDUINO)
        self.trig_kind.addItem("Harp device", TriggerSource.HARP)
        self.trig_port = QtWidgets.QComboBox()
        self.trig_port.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.trig_port.setMinimumContentsLength(10)
        refresh_ports = QtWidgets.QToolButton()
        refresh_ports.setText("↻")
        refresh_ports.setToolTip("Refresh port list")
        refresh_ports.clicked.connect(self._fill_ports)
        self.harp_reg = QtWidgets.QSpinBox()
        self.harp_reg.setRange(0, 255)
        self.harp_bit = QtWidgets.QSpinBox()
        self.harp_bit.setRange(0, 7)
        self.trig_connect = QtWidgets.QPushButton("Connect")
        self.trig_connect.clicked.connect(self._toggle_trigger)
        self.trig_status = QtWidgets.QLabel("Not connected")
        self.trig_status.setWordWrap(True)
        test_hi = QtWidgets.QPushButton("Test ↑")
        test_hi.setToolTip("Simulate a rising edge")
        test_hi.clicked.connect(lambda: self.trigger.simulate(True))
        test_lo = QtWidgets.QPushButton("Test ↓")
        test_lo.setToolTip("Simulate a falling edge")
        test_lo.clicked.connect(lambda: self.trigger.simulate(False))
        self.harp_label = QtWidgets.QLabel("Register / bit:")
        harp_row = QtWidgets.QHBoxLayout()
        harp_row.addWidget(self.harp_reg)
        harp_row.addWidget(self.harp_bit)
        bl.addWidget(QtWidgets.QLabel("Device:"), 0, 0)
        bl.addWidget(self.trig_kind, 0, 1, 1, 2)
        bl.addWidget(QtWidgets.QLabel("Port:"), 1, 0)
        bl.addWidget(self.trig_port, 1, 1)
        bl.addWidget(refresh_ports, 1, 2)
        bl.addWidget(self.harp_label, 2, 0)
        bl.addLayout(harp_row, 2, 1, 1, 2)
        bl.addWidget(self.trig_connect, 3, 0, 1, 3)
        test_row = QtWidgets.QHBoxLayout()
        test_row.addWidget(test_hi)
        test_row.addWidget(test_lo)
        bl.addLayout(test_row, 4, 0, 1, 3)
        bl.addWidget(self.trig_status, 5, 0, 1, 3)
        bl.setColumnStretch(1, 1)
        lay.addWidget(box)

        s = settings()
        self.trig_kind.setCurrentIndex(max(self.trig_kind.findData(s.value("trigger/kind", "arduino")), 0))
        self.harp_reg.setValue(s.value("trigger/harp_register", 32, int))
        self.harp_bit.setValue(s.value("trigger/harp_bit", 0, int))
        self.trig_kind.currentIndexChanged.connect(self._update_trigger_fields)
        self._fill_ports()
        self._update_trigger_fields()

        tips = QtWidgets.QLabel(
            "Drag the dividers between cameras to resize them.\n"
            "F11: fullscreen · Ctrl+H: hide this panel")
        tips.setWordWrap(True)
        tips.setStyleSheet("color: #8a8a8a;")
        lay.addWidget(tips)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self.dock = QtWidgets.QDockWidget("Controls", self)
        self.dock.setObjectName("controls")
        self.dock.setWidget(scroll)
        self.dock.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                              QtWidgets.QDockWidget.DockWidgetFloatable |
                              QtWidgets.QDockWidget.DockWidgetClosable)
        self.dock.setMinimumWidth(270)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, self.dock)

    def _build_menu(self):
        view = self.menuBar().addMenu("&View")
        toggle = self.dock.toggleViewAction()
        toggle.setText("Show controls panel")
        toggle.setShortcut("Ctrl+H")
        view.addAction(toggle)
        retile = view.addAction("Re-tile")
        retile.setShortcut("Ctrl+T")
        retile.triggered.connect(self.retile)
        full = view.addAction("Fullscreen")
        full.setShortcut("F11")
        full.triggered.connect(self.toggle_fullscreen)
        rec = self.menuBar().addMenu("&Record")
        a = rec.addAction("Record…")
        a.setShortcut("Ctrl+R")
        a.triggered.connect(lambda: self.open_record_dialog())
        a = rec.addAction("Stop all recordings")
        a.triggered.connect(self.stop_all_recordings)

    # -- camera selection -------------------------------------------------- #

    def refresh_cameras(self):
        if self.sessions:
            QtWidgets.QMessageBox.information(self, APP_NAME, "Stop recording before refreshing the camera list.")
            return
        for idx in list(self.tiles):
            self._remove_tile(idx)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            self.cameras = list_cameras()
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self.cam_list.blockSignals(True)
        self.cam_list.clear()
        for cam in self.cameras:
            item = QtWidgets.QListWidgetItem(cam.name)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            item.setData(QtCore.Qt.UserRole, cam.index)
            item.setToolTip(f"Device {cam.index}")
            self.cam_list.addItem(item)
        self.cam_list.blockSignals(False)
        self.relayout()

    def _item_for(self, index):
        for i in range(self.cam_list.count()):
            it = self.cam_list.item(i)
            if it.data(QtCore.Qt.UserRole) == index:
                return it
        return None

    def _set_checked(self, states):
        """Apply many check changes, then lay out once."""
        self.cam_list.blockSignals(True)
        for i in range(self.cam_list.count()):
            it = self.cam_list.item(i)
            idx = it.data(QtCore.Qt.UserRole)
            want = states(idx)
            it.setCheckState(QtCore.Qt.Checked if want else QtCore.Qt.Unchecked)
            if want and idx not in self.tiles:
                self._add_tile(idx)
            elif not want and idx in self.tiles:
                self._remove_tile(idx)
        self.cam_list.blockSignals(False)
        self.relayout()

    def select_all(self):
        self._set_checked(lambda _idx: True)

    def select_none(self):
        self._set_checked(lambda _idx: False)

    def _on_item_changed(self, item):
        idx = item.data(QtCore.Qt.UserRole)
        checked = item.checkState() == QtCore.Qt.Checked
        if checked and idx not in self.tiles:
            self._add_tile(idx)
        elif not checked and idx in self.tiles:
            self._remove_tile(idx)
        else:
            return
        self.relayout()

    def _add_tile(self, idx):
        cam = next(c for c in self.cameras if c.index == idx)
        tile = CameraTile(cam)
        tile.record_clicked.connect(self._on_tile_record)
        tile.adjust_clicked.connect(self._on_tile_adjust)
        self.tiles[idx] = tile
        tile.start()

    def _remove_tile(self, idx):
        tile = self.tiles.pop(idx)
        if tile.session:
            tile.session.stop_tile(tile)
            self._collect_finished_sessions()
        dlg = self.adjust_dialogs.pop(idx, None)
        if dlg:
            dlg.close()
        tile.stop()
        win = self.floating.pop(idx, None)
        if win:
            win.release_tile()
        tile.setParent(None)
        tile.deleteLater()

    def _on_floating_closed(self, idx):
        self.floating.pop(idx, None)
        item = self._item_for(idx)
        if item:
            item.setCheckState(QtCore.Qt.Unchecked)  # triggers _on_item_changed

    def _on_tile_adjust(self, tile):
        dlg = self.adjust_dialogs.get(tile.cam.index)
        if dlg is None:
            dlg = AdjustmentsDialog(tile, self)
            dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose)
            dlg.destroyed.connect(lambda *_a, i=tile.cam.index: self.adjust_dialogs.pop(i, None))
            self.adjust_dialogs[tile.cam.index] = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # -- recording --------------------------------------------------------- #

    def _on_tile_record(self, tile):
        if tile.session:
            tile.session.stop_tile(tile)
            self._collect_finished_sessions()
        else:
            self.open_record_dialog([tile.cam.index])

    def open_record_dialog(self, preselect=None):
        if not self.cameras:
            return
        busy = {t.cam.index for t in self.tiles.values() if t.session}
        if preselect is None:
            preselect = [i for i in self.tiles if i not in busy]
        dlg = RecordDialog(self.cameras, self.tiles, set(preselect), busy, self.trigger.connected, self)
        if dlg.exec() != QtWidgets.QDialog.Accepted or not dlg.config:
            return
        self.start_recording(dlg.config)

    def start_recording(self, cfg: RecordConfig):
        wanted = set(cfg.camera_indices)
        if not wanted.issubset(self.tiles):  # open any camera that isn't shown yet
            self._set_checked(lambda i: i in self.tiles or i in wanted)
        try:
            session = RecordingSession(cfg, [self.tiles[i] for i in cfg.camera_indices])
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, "Record", f"Could not create files:\n{exc}")
            return None
        self.sessions.append(session)
        self._update_rec_status()
        return session

    def stop_all_recordings(self):
        for s in list(self.sessions):
            s.stop()
        self._collect_finished_sessions()

    def on_trigger_edge(self, rising, t_perf):
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        level = "HIGH ↑" if rising else "LOW ↓"
        self.trig_status.setText(f"{level} at {stamp}" + ("" if self.trigger.connected else "  (test)"))
        for s in list(self.sessions):
            if rising and s.state == RecordingSession.ARMED and s.config.start_on_ttl:
                s.start(t_perf)
            s.log_edge(rising, t_perf)
            if not rising and s.state == RecordingSession.RECORDING and s.config.stop_on_ttl:
                s.stop()
        self._collect_finished_sessions()

    def _collect_finished_sessions(self):
        for s in [s for s in self.sessions if s.state == RecordingSession.DONE]:
            self.sessions.remove(s)
            for t in s.tiles:
                t.session = None
            if s.errors:
                QtWidgets.QMessageBox.warning(self, "Recording problem", "\n".join(s.errors))
            if s.saved:
                frames = ", ".join(f"{os.path.basename(p)} ({n} frames"
                                   + (f", {d} dropped" if d else "") + ")" for p, n, d in s.saved)
                self.statusBar().showMessage(f"Saved to {s.config.folder}: {frames}", 30000)
        self._update_rec_status()

    def _update_rec_status(self):
        if not self.sessions:
            self.rec_status.setText("Not recording")
            return
        lines = []
        for s in self.sessions:
            names = ", ".join(t.cam.name for t in s.tiles)
            if s.state == RecordingSession.RECORDING:
                lines.append(f"● {fmt_hms(s.elapsed())} – {names}")
            elif s.state == RecordingSession.ARMED:
                lines.append(f"◌ Armed (waiting for TTL) – {names}")
            else:
                lines.append(f"Starting – {names}")
        self.rec_status.setText("\n".join(lines))

    # -- trigger ----------------------------------------------------------- #

    def _fill_ports(self):
        current = self.trig_port.currentData() or settings().value("trigger/port", "")
        self.trig_port.clear()
        for device, label in list_serial_ports():
            self.trig_port.addItem(label, device)
        i = self.trig_port.findData(current)
        if i >= 0:
            self.trig_port.setCurrentIndex(i)

    def _update_trigger_fields(self):
        harp = self.trig_kind.currentData() == TriggerSource.HARP
        for w in (self.harp_label, self.harp_reg, self.harp_bit):
            w.setVisible(harp)
        self.harp_reg.setToolTip("Harp register that reports the digital input (32 on a Harp Behavior board)")
        self.harp_bit.setToolTip("Bit within that register, i.e. which input line (0 = first)")

    def _toggle_trigger(self):
        if self.trigger.connected:
            self.trigger.close()
            return
        port = self.trig_port.currentData()
        if not port:
            self.trig_status.setText("Pick a serial port first.")
            return
        kind = self.trig_kind.currentData()
        try:
            self.trigger.open(port, kind, self.harp_reg.value(), self.harp_bit.value())
        except Exception as exc:
            self.trig_status.setText(f"Could not open {port}: {exc}")
            return
        s = settings()
        s.setValue("trigger/kind", kind)
        s.setValue("trigger/port", port)
        s.setValue("trigger/harp_register", self.harp_reg.value())
        s.setValue("trigger/harp_bit", self.harp_bit.value())

    def _on_trigger_status(self, text):
        self.trig_status.setText(text)
        connected = self.trigger.connected
        self.trig_connect.setText("Disconnect" if connected else "Connect")
        for w in (self.trig_kind, self.trig_port, self.harp_reg, self.harp_bit):
            w.setEnabled(not connected)

    # -- layout ------------------------------------------------------------ #

    def _on_layout_toggled(self, grid_checked):
        self.layout_mode = self.MODE_GRID if grid_checked else self.MODE_SEPARATE
        self.relayout()

    def _ordered_tiles(self):
        return [self.tiles[i] for i in sorted(self.tiles)]

    def _clear_grid(self):
        for t in self.tiles.values():
            if t.parent() is not None and not isinstance(t.parent(), FloatingWindow):
                t.setParent(None)
        if self.grid_root is not None:
            self.central_lay.removeWidget(self.grid_root)
            self.grid_root.deleteLater()
            self.grid_root = None
        self.row_splitters = []

    def relayout(self):
        tiles = self._ordered_tiles()
        self._clear_grid()

        if self.layout_mode == self.MODE_GRID:
            for idx in list(self.floating):
                self.floating.pop(idx).release_tile()
            if not tiles:
                self.hint.setText("Tick one or more cameras in the panel on the left."
                                  if self.cameras else "No cameras found. Plug one in and press Refresh.")
                self.hint.show()
                return
            self.hint.hide()
            rows, cols = grid_shape(len(tiles))
            root = QtWidgets.QSplitter(QtCore.Qt.Vertical)
            root.setChildrenCollapsible(False)
            for r in range(rows):
                row = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
                row.setChildrenCollapsible(False)
                for t in tiles[r * cols:(r + 1) * cols]:
                    row.addWidget(t)
                    t.show()
                root.addWidget(row)
                self.row_splitters.append(row)
            self.grid_root = root
            self.central_lay.addWidget(root)
            self.retile()
        else:
            self.hint.setText("Cameras are open in separate windows.\n"
                              "Press Re-tile (Ctrl+T) to arrange them in a grid."
                              if tiles else "Tick one or more cameras in the panel on the left.")
            self.hint.show()
            for idx in list(self.floating):
                if idx not in self.tiles:
                    self.floating.pop(idx).release_tile()
            for t in tiles:
                if t.cam.index not in self.floating:
                    win = FloatingWindow(t)
                    win.closed_by_user.connect(self._on_floating_closed)
                    self.floating[t.cam.index] = win
                    win.show()
            QtCore.QTimer.singleShot(0, self.retile)

    def retile(self):
        if self.layout_mode == self.MODE_GRID:
            if self.grid_root is None:
                return
            big = 100000
            self.grid_root.setSizes([big] * self.grid_root.count())
            for row in self.row_splitters:
                row.setSizes([big] * row.count())
            return

        wins = [self.floating[i] for i in sorted(self.floating)]
        if not wins:
            return
        screen = (self.screen() or QtGui.QGuiApplication.primaryScreen()).availableGeometry()
        rows, cols = grid_shape(len(wins))
        cw, ch = screen.width() // cols, screen.height() // rows
        for n, win in enumerate(wins):
            r, c = divmod(n, cols)
            if win.isMinimized() or win.isMaximized():
                win.showNormal()
            # frame = title bar + borders; client size = cell minus frame
            fw = win.frameGeometry().width() - win.geometry().width()
            fh = win.frameGeometry().height() - win.geometry().height()
            win.resize(max(cw - fw, 160), max(ch - fh, 120))
            win.move(screen.x() + c * cw, screen.y() + r * ch)
            win.raise_()

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # -- loop / shutdown --------------------------------------------------- #

    def _tick(self):
        now = time.perf_counter()
        for s in list(self.sessions):
            if s.state == RecordingSession.WAITING:
                # Start together once every camera is delivering frames (or after a timeout).
                if all(t.worker and t.worker.ok for t in s.tiles) or now > s.wait_deadline:
                    s.start()
            elif s.state == RecordingSession.RECORDING and s.config.limit_s \
                    and s.elapsed() >= s.config.limit_s:
                s.stop()
            for rec in s.recorders.values():
                if rec.error and s.state != RecordingSession.DONE:
                    s.stop()
                    break
        if any(s.state == RecordingSession.DONE for s in self.sessions):
            self._collect_finished_sessions()
        if self.sessions:
            self._update_rec_status()
        for t in self.tiles.values():
            t.tick()

    def closeEvent(self, event):
        if self.sessions and QtWidgets.QMessageBox.question(
                self, APP_NAME, "Recording is in progress. Stop it and quit?") != QtWidgets.QMessageBox.Yes:
            event.ignore()
            return
        self.timer.stop()
        for s in list(self.sessions):
            s.stop()
        self.sessions.clear()
        self.trigger.close()
        for t in self.tiles.values():
            if t.worker:
                t.worker.stop()
        for idx in list(self.tiles):
            self._remove_tile(idx)
        super().closeEvent(event)


# --------------------------------------------------------------------------- #

def apply_dark_theme(app):
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    base, alt, text = QtGui.QColor(30, 30, 30), QtGui.QColor(45, 45, 45), QtGui.QColor(225, 225, 225)
    pal.setColor(QtGui.QPalette.Window, alt)
    pal.setColor(QtGui.QPalette.WindowText, text)
    pal.setColor(QtGui.QPalette.Base, base)
    pal.setColor(QtGui.QPalette.AlternateBase, alt)
    pal.setColor(QtGui.QPalette.Text, text)
    pal.setColor(QtGui.QPalette.Button, alt)
    pal.setColor(QtGui.QPalette.ButtonText, text)
    pal.setColor(QtGui.QPalette.ToolTipBase, base)
    pal.setColor(QtGui.QPalette.ToolTipText, text)
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(42, 130, 218))
    pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.white)
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor(110, 110, 110))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.ButtonText, QtGui.QColor(110, 110, 110))
    pal.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.WindowText, QtGui.QColor(110, 110, 110))
    app.setPalette(pal)


def _self_test(win, out_dir):
    """Developer check of every format: cameras 0+1 in H.264 started/stopped by simulated
    TTL edges (camera 0 thinned to 15 fps), camera 3 in MPEG-4 without filters, camera 2
    in MJPEG; compares size estimates (incl. live H.264 probes) with the real files."""
    os.makedirs(out_dir, exist_ok=True)
    log = []
    est = {}
    probes = {}
    dialog = []

    def say(msg):
        log.append(msg)
        if sys.stdout:  # None in the windowed .exe
            print(msg, flush=True)

    def step1():
        win.tiles[1].set_soft_filter(SoftFilter(brightness=30, contrast=20, gamma=120))
        for i, fps in ((0, 15), (1, 0)):
            probes[i] = (SizeProbe(win.tiles[i], "standard", ALL_FILTERS, fps), fps)
        cfg = RecordConfig([0, 1], out_dir, "pair", 0, start_on_ttl=True, stop_on_ttl=True,
                           fmt="h264", preset="standard", rec_fps={0: 15})
        win.start_recording(cfg)
        win._on_tile_adjust(win.tiles[1])

    def step2():
        win.grab().save(os.path.join(out_dir, "armed.png"))
        dlg = win.adjust_dialogs.get(1)
        if dlg:
            dlg.grab().save(os.path.join(out_dir, "adjust.png"))
            dlg.close()
        rd = RecordDialog(win.cameras, win.tiles, {2, 3}, {0, 1}, False, win)
        rd.fmt.setCurrentIndex(rd.fmt.findData("h264"))
        rd.limit_on.setChecked(True)
        rd.show()
        dialog.append(rd)
        win.trigger.simulate(True)

    def step3():
        win.grab().save(os.path.join(out_dir, "recording.png"))
        rd = dialog.pop()
        rd._refresh()
        rd.grab().save(os.path.join(out_dir, "record_dialog.png"))
        rd.reject()
        for i, (probe, fps) in probes.items():
            per_frame = probe.poll()
            est[i] = estimate_bytes_per_second(win.tiles[i], win.tiles[i].cam, "h264", "standard",
                                               ALL_FILTERS, fps, per_frame)
        win.trigger.simulate(False)

    def step4():
        win.tiles[3].set_soft_filter(SoftFilter(brightness=60))
        est[3] = estimate_bytes_per_second(win.tiles[3], win.tiles[3].cam, "mpeg4", "compact", frozenset(), 0)
        win.start_recording(RecordConfig([3], out_dir, "mpeg4_raw", limit_s=2, fmt="mpeg4",
                                         preset="compact", include=frozenset()))

    def step4b():
        est[2] = estimate_bytes_per_second(win.tiles[2], win.tiles[2].cam, "mjpeg", "high", ALL_FILTERS, 0)
        win.start_recording(RecordConfig([2], out_dir, "mjpeg_high", limit_s=2, fmt="mjpeg", preset="high"))

    def step5():
        win.tiles[3].set_soft_filter(SoftFilter())
        win.tiles[1].set_soft_filter(SoftFilter())
        say(f"sessions left: {len(win.sessions)}")
        for name in sorted(os.listdir(out_dir)):
            say(f"{name} {os.path.getsize(os.path.join(out_dir, name))}")
        for i, stem in {0: "pair_HD_USB_Camera_1", 1: "pair_Arducam_OV9281_USB_Camera",
                        3: "mpeg4_raw", 2: "mjpeg_high"}.items():
            with open(os.path.join(out_dir, stem + "_timestamps.csv")) as fh:
                rows = list(csv.DictReader(fh))
            secs = float(rows[-1]["elapsed_s"]) - float(rows[0]["elapsed_s"])
            actual = os.path.getsize(os.path.join(out_dir, stem + ".avi")) / secs
            e, measured = est[i]
            say(f"{stem}: est {e * 3.6e-6:.2f} GB/h ({'measured' if measured else 'rough'}), "
                f"actual {actual * 3.6e-6:.2f} GB/h")
        with open(os.path.join(out_dir, "selftest.log"), "w") as fh:
            fh.write("\n".join(log))
        win.close()

    for delay, fn in ((6000, step1), (8000, step2), (13000, step3), (14000, step4),
                      (17000, step4b), (21000, step5)):
        QtCore.QTimer.singleShot(delay, fn)
    win.select_all()


def resource_path(name):
    """Path to a file shipped with the app (inside the .exe when frozen)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def main():
    if sys.platform == "win32":
        # Own taskbar identity, so Windows shows our icon rather than Python's.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CameraViewer.App")
    QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
        QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = resource_path("app_icon.ico")
    if os.path.exists(icon):
        app.setWindowIcon(QtGui.QIcon(icon))
    apply_dark_theme(app)
    win = MainWindow()
    win.show()
    if "--selftest" in sys.argv:
        _self_test(win, sys.argv[sys.argv.index("--selftest") + 1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
