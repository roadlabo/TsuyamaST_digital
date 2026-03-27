@echo off
cd /d "C:\_TsuyamaSignage\app\SignageController"
call "C:\_PythonEnvs\venv_signage_controller\Scripts\activate.bat"
python "C:\_TsuyamaSignage\app\SignageController\analysisPCTsuyamaST_SuperAI_Signage_Controller.py"
