@echo off
chcp 65001 > nul
rem wmic is gone from Windows 11: stop only the tool and the Silma server via PowerShell to free GPU memory
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match 'VoiceDictate.+dictate\.pyw|SilmaTTS.+server\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0compare.py"
start "" "%~dp0start.cmd"
