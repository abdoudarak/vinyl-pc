@echo off
cd /d "%~dp0"
python -m pip install winsdk pywebview pyaudiowpatch numpy
python app.py
pause
