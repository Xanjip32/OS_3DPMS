@echo off
rem OS_3DPMS installer for Windows. Double-click it on the PC that can reach the printers.
setlocal
cd /d "%~dp0"
title OS_3DPMS installer
echo.
echo  OS_3DPMS installer
echo  ==================
echo.

rem ---- 1. Python 3.8+ ---------------------------------------------------------
set "PY="
if exist "%~dp0runtime\python.exe" set "PY=1"
if not defined PY py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=1"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo [1/3] Python not found.
  where winget >nul 2>nul || goto :no_winget_python
  echo       Installing Python with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  echo.
  echo       After Python is installed, double-click install.bat again.
  pause
  exit /b 1
)
echo [1/3] Python ready.

rem ---- 2. Stop any copy already running from this folder -----------------------
echo [2/3] Checking for a running copy...
call "%~dp0stop.bat" quiet
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

rem ---- 3. Auto-start at login and start now -----------------------------------
echo [3/3] Setting up auto-start and starting OS_3DPMS...
if not exist "%STARTUP%" mkdir "%STARTUP%"
> "%STARTUP%\os3dpms.vbs" echo CreateObject("WScript.Shell").Run "wscript.exe ""%~dp0run-hidden.vbs""", 0
wscript.exe "%~dp0run-hidden.vbs" --open-setup

echo.
echo  Setup opens at http://127.0.0.1:8079/setup when the app is ready.
echo  Add a printer and save it to preview its camera.
echo.
pause
exit /b 0

:no_winget_python
echo       Install Python from https://www.python.org/downloads/ then run install.bat again.
pause
exit /b 1
