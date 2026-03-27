@echo off
cd /d "%~dp0"
call "..\.venv\Scripts\activate.bat"
python ip_camera_viewer.py
