@echo off
setlocal

cd /d "C:\_TsuyamaSignage\app\03_ip_camera_viewer"

echo [INFO] Starting IP Camera Viewer...

"C:\_PythonEnvs\venv_ip_camera_viewer\Scripts\python.exe" ^
"C:\_TsuyamaSignage\app\03_ip_camera_viewer\ip_camera_viewer.py" ^
>> "C:\_TsuyamaSignage\logs\ip_camera_viewer.log" 2>&1

echo.
echo [INFO] Process finished.
pause
