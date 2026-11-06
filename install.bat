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
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo [1/4] Python not found.
  where winget >nul 2>nul || goto :no_winget_python
  echo       Installing Python with winget...
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  echo.
  echo       Python installed. Close this window and double-click install.bat again.
  pause
  exit /b 1
)
echo [1/4] Python found.

rem ---- 2. Tailscale -----------------------------------------------------------
set "HAVE_TS="
if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "HAVE_TS=1"
where tailscale >nul 2>nul && set "HAVE_TS=1"
if defined HAVE_TS (
  echo [2/4] Tailscale found.
) else (
  echo [2/4] Tailscale not found. Installing it now.
  echo       Downloading the official installer and checking its signature.
  echo       Windows asks for permission once: click Yes.
  %PY% "%~dp0run.py" --install-tailscale || (
    echo       Tailscale was not installed. You can retry from the setup page.
  )
)

rem ---- 3. Stop old copies, remove the old open firewall rule -------------------
echo [3/4] Cleaning up older versions...
call "%~dp0stop.bat" quiet
netsh advfirewall firewall delete rule name="Printer Dashboard" >nul 2>nul
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
del "%STARTUP%\printer-dashboard.vbs" >nul 2>nul

rem ---- 4. Auto-start at login and start now -----------------------------------
echo [4/4] Setting up auto-start and starting OS_3DPMS...
> "%STARTUP%\os3dpms.vbs" echo CreateObject("WScript.Shell").Run "wscript.exe ""%~dp0run-hidden.vbs""", 0
wscript.exe "%~dp0run-hidden.vbs"
timeout /t 3 /nobreak >nul
start "" "http://127.0.0.1:8079/setup"

echo.
echo  Done. The setup page is open in your browser:  http://127.0.0.1:8079/setup
echo  Follow the steps there (Tailscale login, printers, publish).
echo.
pause
exit /b 0

:no_winget_python
echo       Install Python from https://www.python.org/downloads/ then run install.bat again.
pause
exit /b 1
