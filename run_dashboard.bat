@echo off
title Iran Market AI Dashboard
cd /d "%~dp0"
echo Starting Iran Market AI Dashboard on http://127.0.0.1:8085 ...
start "" http://127.0.0.1:8085
".venv\Scripts\python.exe" dashboard.py
pause
