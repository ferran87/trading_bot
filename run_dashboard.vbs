' Launches run_dashboard.bat silently (no console window visible)
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Users\ferra\trading bot\run_dashboard.bat""", 0, False
Set shell = Nothing
