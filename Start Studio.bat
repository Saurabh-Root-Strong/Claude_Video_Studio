@echo off
title CVE Studio (close this window to stop)
cd /d "%~dp0_engines\studio"
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
echo Starting CVE Studio at http://127.0.0.1:8070 ...
".venv\Scripts\python.exe" app.py
pause
