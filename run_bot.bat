@echo off
cd /d "C:\Users\ferra\trading bot"
".venv\Scripts\python.exe" main.py --once >> data\logs\scheduler.log 2>&1
