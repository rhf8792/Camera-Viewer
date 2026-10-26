"""Draws the Camera Viewer icon and saves app_icon.png + app_icon.ico (all Windows sizes).

Run:  .venv\Scripts\python make_icon.py
"""

import os
import sys

from PIL import Image
from PySide6 import QtCore, QtGui

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024  # draw large, scale down for each icon size


def draw():
    img = QtGui.QImage(S, S, QtGui.QImage.Format_ARGB32)
    img.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.Antialiasing)

    # Rounded-square body with a deep blue gradient.
    body = QtCore.QRectF(40, 40, S - 80, S - 80)
    g = QtGui.QLinearGradient(body.topLeft(), body.bottomRight())
    g.setColorAt(0, QtGui.QColor("#2b3a55"))
    g.setColorAt(1, QtGui.QColor("#0d1320"))
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(g)
    p.drawRoundedRect(body, 210, 210)

    c = QtCore.QPointF(S / 2, S / 2 + 20)

    # Lens barrel: metallic ring.
    ring = QtGui.QConicalGradient(c, 45)
    for pos, col in ((0, "#d9dde4"), (0.25, "#5c6573"), (0.5, "#c3c8d0"), (0.75, "#4a525e"), (1, "#d9dde4")):
        ring.setColorAt(pos, QtGui.QColor(col))
    p.setBrush(ring)
    p.drawEllipse(c, 330, 330)
    p.setBrush(QtGui.QColor("#0a0e16"))
    p.drawEllipse(c, 285, 285)

    # Glass: teal-blue radial gradient.
    glass = QtGui.QRadialGradient(c + QtCore.QPointF(-60, -70), 300)
    glass.setColorAt(0, QtGui.QColor("#5fd3ff"))
    glass.setColorAt(0.45, QtGui.QColor("#1677c9"))
    glass.setColorAt(1, QtGui.QColor("#061a3a"))
    p.setBrush(glass)
    p.drawEllipse(c, 250, 250)

    # Iris rings and pupil.
    p.setBrush(QtCore.Qt.NoBrush)
    p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 45), 10))
    p.drawEllipse(c, 175, 175)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(QtGui.QColor("#04101f"))
    p.drawEllipse(c, 105, 105)

    # Glints.
    p.setBrush(QtGui.QColor(255, 255, 255, 220))
    p.drawEllipse(c + QtCore.QPointF(-115, -120), 62, 46)
    p.setBrush(QtGui.QColor(255, 255, 255, 120))
    p.drawEllipse(c + QtCore.QPointF(95, 105), 26, 20)

    # Red record dot, top right, with a dark outline so it reads at 16 px.
    dot = QtCore.QPointF(S - 205, 205)
    p.setBrush(QtGui.QColor("#0d1320"))
    p.drawEllipse(dot, 132, 132)
    red = QtGui.QRadialGradient(dot + QtCore.QPointF(-30, -30), 130)
    red.setColorAt(0, QtGui.QColor("#ff7a6e"))
    red.setColorAt(1, QtGui.QColor("#d3221a"))
    p.setBrush(red)
    p.drawEllipse(dot, 104, 104)
    p.end()
    return img


def main():
    app = QtGui.QGuiApplication(sys.argv)  # noqa: F841 - needed for QPainter
    png = os.path.join(HERE, "app_icon.png")
    draw().save(png)
    im = Image.open(png).convert("RGBA")
    big = im.resize((256, 256), Image.LANCZOS)
    big.save(os.path.join(HERE, "app_icon.ico"),
             sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote app_icon.png and app_icon.ico")


if __name__ == "__main__":
    main()
