@echo off
chcp 65001 > nul
rem wmic حُذف من ويندوز 11، فالإيقاف عبر PowerShell: الأداة وخادم سلمى فقط، لتتسع ذاكرة كرت الشاشة
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'VoiceDictate.+dictate\.pyw|SilmaTTS.+server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0compare.py"
start "" "%~dp0start.cmd"
