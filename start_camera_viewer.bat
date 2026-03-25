@echo off

REM =========================
REM バッチの場所を基準にする
REM =========================
cd /d "%~dp0"

REM =========================
REM 不具合の原因（記録）
REM =========================
REM - python コマンドを直接呼んでいた
REM - PATH環境変数に依存していた
REM - embedded Python が参照されていなかった

REM =========================
REM Python（embedded）
REM =========================
set "PYTHON=%~dp0runtime\python\python.exe"
REM set "PYTHON=%~dp0runtime\python\pythonw.exe"

REM =========================
REM PATH補完（必要ライブラリ対策）
REM =========================
set "PATH=%~dp0runtime\python;%~dp0runtime\python\Scripts;%PATH%"

REM =========================
REM 事前チェック
REM =========================
if not exist "%PYTHON%" (
    echo Python not found.
    pause
    exit /b
)

if not exist "app\ip_camera_viewer\main.py" (
    echo main.py not found.
    pause
    exit /b
)

REM =========================
REM 起動
REM =========================
"%PYTHON%" "app\ip_camera_viewer\main.py"

pause
