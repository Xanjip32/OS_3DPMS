@echo off
rem Run OS_3DPMS in this window (useful for troubleshooting). Close the window to stop it.
cd /d "%~dp0"
call "%~dp0stop.bat" quiet
py -3 run.py --open-setup 2>nul || python run.py --open-setup
pause
