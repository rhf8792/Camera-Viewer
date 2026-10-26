@echo off
rem Builds CameraViewer.exe from camera_viewer.py.
rem On a new PC it first creates .venv and installs requirements.txt (needs Python 3.11).
rem ffi.dll is added by hand because PyInstaller can't find it in a conda-based Python.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Setting up Python environment...
  py -3.11 -m venv .venv || python -m venv .venv || goto :nopython
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt || goto :failed
)
for /f "delims=" %%p in ('.venv\Scripts\python -c "import sys;print(sys.base_prefix)"') do set BASE=%%p
set FFI=
if exist "%BASE%\Library\bin\ffi.dll" set FFI=--add-binary "%BASE%\Library\bin\ffi.dll;."
.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name CameraViewer %FFI% ^
  --icon "%~dp0app_icon.ico" --add-data "%~dp0app_icon.ico;." ^
  --collect-submodules comtypes --collect-submodules pygrabber --collect-submodules serial --collect-submodules av ^
  --distpath . --workpath "%TEMP%\cv_build" --specpath "%TEMP%\cv_build" camera_viewer.py || goto :failed
echo.
echo Done: CameraViewer.exe
goto :eof

:nopython
echo Python 3.11 was not found. Install it from https://www.python.org/downloads/ and run this again.
pause
exit /b 1

:failed
echo Build failed - see the messages above.
pause
exit /b 1
