@echo off
title CVE Studio (close this window to stop)
cd /d "%~dp0"
if not exist "_engines\studio\.venv\Scripts\python.exe" goto needsetup
if not exist "_engines\whisper\models\ggml-large-v3-turbo-q5_0.bin" goto needsetup
cd /d "%~dp0_engines\studio"
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
echo Starting CVE Studio at http://127.0.0.1:8070 ...
".venv\Scripts\python.exe" app.py
pause
exit /b

:needsetup
echo This PC is not set up yet (first run after download).
choice /m "Run Setup now"
if errorlevel 2 exit /b
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if errorlevel 1 (pause & exit /b)
"%~f0"
