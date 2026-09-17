@echo off
rem Launch the City Map Generator GUI.
rem Uses python.exe (not pythonw.exe) so that if the app fails to even start,
rem the error is visible in this window, which is kept open by "pause".
cd /d "%~dp0"
python city_map_gui.py
echo.
echo ------------------------------------------------------------------
echo GUI closed. This window stays open so you can read any messages.
echo ------------------------------------------------------------------
pause
