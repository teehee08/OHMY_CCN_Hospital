@echo off
cd /d "%~dp0"
python scenario_lab.py --open
if errorlevel 1 pause
