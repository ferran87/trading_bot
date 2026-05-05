' Runs run_bot_auto.bat silently — no console window, no prompts.
' Used by Windows Task Scheduler for the daily auto-run.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Users\ferra\trading bot\run_bot_auto.bat""", 0, True
Set shell = Nothing
