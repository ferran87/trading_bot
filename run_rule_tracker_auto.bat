@echo off
cd /d "C:\Users\ferra\trading bot"
".venv\Scripts\python.exe" scripts\measure_rule_changes.py >> data\logs\rule_tracker.log 2>&1
