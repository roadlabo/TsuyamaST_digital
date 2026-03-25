@echo off

REM =========================
REM バッチの場所（repo ルート）を基準にする
REM =========================
cd /d "%~dp0"

REM =========================
REM Python（embedded）
REM =========================
set "PYTHON=%~dp0runtime\python\python.exe"

REM =========================
REM 事前チェック
REM =========================
if not exist "%PYTHON%" (
    echo Python not found: %PYTHON%
    pause
    exit /b 1
)

if not exist "%~dp0app\ip_camera_viewer\ip_camera_viewer.py" (
    echo ip_camera_viewer.py not found.
    pause
    exit /b 1
)

REM =========================
REM ip_camera_viewer.py のディレクトリで起動（相対 import 対策）
REM =========================
cd /d "%~dp0app\ip_camera_viewer"
"%PYTHON%" ip_camera_viewer.py

pause
