@echo off
rem LAN Video Server (portable) - double click to start
cd /d "%~dp0"
set PYTHONUTF8=1
"python\python.exe" video_server.py %*
pause
