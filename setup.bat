@echo off
rem 録音文字起こしツール setup: installs Python packages and the Whisper model.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup.ps1" %*
pause
