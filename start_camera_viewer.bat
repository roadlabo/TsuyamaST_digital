@echo off
setlocal

REM =========================
REM ルートディレクトリ取得
REM =========================
set "ROOT_DIR=%~dp0"

REM =========================
REM Python実行ファイル
REM =========================
set "PYTHON_EXE=%ROOT_DIR%runtime\python\python.exe"
REM set "PYTHON_EXE=%ROOT_DIR%runtime\python\pythonw.exe"

REM =========================
REM Python環境設定
REM =========================
set "PYTHONHOME=%ROOT_DIR%runtime\python"
set "PYTHONPATH=%ROOT_DIR%runtime\pydeps"

REM =========================
REM Qtライブラリパス（PyQt6用）
REM =========================
set "PATH=%ROOT_DIR%runtime\python\Lib\site-packages\PyQt6\Qt6\bin;%PATH%"

REM =========================
REM アプリ起動スクリプト
REM =========================
set "MAIN_SCRIPT=%ROOT_DIR%src\main.py"

REM =========================
REM 事前チェック
REM =========================
if not exist "%PYTHON_EXE%" (
    echo Python runtime not found.
    pause
    exit /b 1
)

if not exist "%MAIN_SCRIPT%" (
    echo main.py not found.
    pause
    exit /b 1
)

REM =========================
REM アプリ起動
REM =========================
echo Starting Camera Viewer...
"%PYTHON_EXE%" "%MAIN_SCRIPT%"

pause
