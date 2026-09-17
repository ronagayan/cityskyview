@echo off
rem Launch CityModel. python.exe (not pythonw) so a start-up error stays visible.
cd /d "%~dp0"
python -m citymodel
if errorlevel 1 (
  echo.
  echo CityModel exited with an error. If packages are missing run:
  echo     python -m pip install -r requirements.txt
  pause
)
