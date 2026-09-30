@echo off
rem 録音文字起こしツール launcher. Run setup.bat first.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo .venv not found. Run setup.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0whisper_gui_meeting.py"
