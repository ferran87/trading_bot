@echo off
cd /d "C:\Users\ferra\trading bot"

:loop
".venv\Scripts\streamlit.exe" run dashboard\app.py ^
    --server.port 8511 ^
    --server.headless true ^
    --server.runOnSave false ^
    >> data\logs\dashboard.log 2>&1

:: If Streamlit exits for any reason, wait 10 seconds and restart
timeout /t 10 /nobreak >nul
goto loop
