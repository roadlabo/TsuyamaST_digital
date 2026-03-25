@echo off
cd /d "%~dp0app\ip_camera_viewer"
"..\..\.venv\Scripts\python.exe" ip_camera_viewer.py
pause
