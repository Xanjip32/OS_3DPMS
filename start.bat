@echo off
rem Run OS_3DPMS in this window (useful for troubleshooting). Close the window to stop it.
cd /d "%~dp0"
call "%~dp0stop.bat" quiet
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" "%~dp0run.py" --open-setup
) else (
  py -3 "%~dp0run.py" --open-setup 2>nul || python "%~dp0run.py" --open-setup
)
pause
