@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 or 3.12 is required. See README.md.
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg is required on PATH. See README.md.
  pause
  exit /b 1
)
where ffprobe >nul 2>nul
if errorlevel 1 (
  echo FFprobe is required on PATH. See README.md.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  py -3.12 -m venv .venv
  if errorlevel 1 py -3.11 -m venv .venv
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Open http://127.0.0.1:8765 in your browser after the server starts.
.venv\Scripts\python.exe -m uvicorn agent.server:app --host 127.0.0.1 --port 8765
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup or startup failed. See the error above.
pause
exit /b 1
