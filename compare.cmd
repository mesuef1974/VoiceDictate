@echo off
chcp 65001 > nul
taskkill /f /fi "WINDOWTITLE eq إملاء" > nul 2>&1
for /f "tokens=2 delims=," %%p in ('wmic process where "name='pythonw.exe' and commandline like '%%dictate.pyw%%'" get processid^,name /format:csv 2^>nul ^| findstr /r "[0-9]"') do taskkill /f /pid %%p > nul 2>&1
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0compare.py"
start "" "%~dp0start.cmd"
