@echo off
cd /d "%~dp0app\ip_camera_viewer"
start "" "..\..\.venv\Scripts\pythonw.exe" ip_camera_viewer.py
exit
