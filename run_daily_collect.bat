@echo off
rem Daily end-of-day data collection for Iran Market AI.
rem Run after the market closes (e.g. 16:00 Tehran, Saturday-Wednesday). Output is appended to data\logs.
cd /d "%~dp0"
if not exist data\logs mkdir data\logs
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i
".venv\Scripts\python.exe" collector.py daily >> "data\logs\daily-%TODAY%.log" 2>&1
