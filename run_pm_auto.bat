@echo off
cd /d "C:\Users\ferra\trading bot"
".venv\Scripts\python.exe" scripts\run_portfolio_manager.py >> data\logs\portfolio_manager.log 2>&1
