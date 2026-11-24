@echo off
rem Remove OpenPrintDeck auto-start and stop publishing it on Tailscale.
cd /d "%~dp0"
call "%~dp0stop.bat" quiet
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\os3dpms.vbs" >nul 2>nul
set "TS=tailscale"
if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
echo.
echo Removing Tailscale publishing (tailscale serve reset)...
"%TS%" serve reset
echo.
echo OpenPrintDeck is stopped and will not start at login.
echo You can now delete this folder. Tailscale itself stays installed.
pause
