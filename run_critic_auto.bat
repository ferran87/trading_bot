@echo off
cd /d "C:\Users\ferra\trading bot"
".venv\Scripts\python.exe" scripts\run_strategy_critic.py >> data\logs\strategy_critic.log 2>&1
