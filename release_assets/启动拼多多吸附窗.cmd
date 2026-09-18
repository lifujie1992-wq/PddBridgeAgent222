@echo off
cd /d "%~dp0"
if not exist "PddAdsorbWindow.exe" (
  echo PddAdsorbWindow.exe not found
  pause
  exit /b 1
)
start "" "%~dp0PddAdsorbWindow.exe"
