@echo off
rem LAN Video Server - double click to start
cd /d "%~dp0"
set PYTHONUTF8=1
python --version >nul 2>nul
if %errorlevel%==0 (
  python video_server.py %*
) else (
  py -3 video_server.py %*
)
pause
