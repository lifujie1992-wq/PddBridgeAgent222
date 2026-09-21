@echo off
cd /d "%~dp0"
if not exist "PddAdsorbWindow.exe" exit /b 0
"%~dp0PddAdsorbWindow.exe" --stop
