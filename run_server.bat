@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating Python environment...
  py -m venv .venv
)
".venv\Scripts\python.exe" -c "import flask" >nul 2>nul
if errorlevel 1 (
  echo [2/3] Installing required package for the first time...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
) else (
  echo [2/3] Required package is ready.
)
echo [3/3] Starting MITTARE Exam Coach...
start "" http://127.0.0.1:5000
".venv\Scripts\python.exe" server.py
endlocal
