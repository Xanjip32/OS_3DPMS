@echo off
rem Stop OpenPrintDeck (and the older "printer-dashboard" version if it is running).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
if /i not "%~1"=="quiet" pause
