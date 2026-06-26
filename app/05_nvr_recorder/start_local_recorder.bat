@echo off
setlocal

cd /d "%~dp0"

python start_local_recorder.py

echo.
echo ===============================
echo Recorder has stopped.
echo Press any key to close...
pause >nul
