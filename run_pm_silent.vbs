' Runs run_pm_auto.bat silently — no console window, no prompts.
' Used by the \ThesisBot_Daily scheduled task.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Users\ferra\trading bot\run_pm_auto.bat""", 0, True
Set shell = Nothing
