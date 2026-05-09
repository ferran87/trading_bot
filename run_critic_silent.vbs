' Runs run_critic_auto.bat silently — no console window, no prompts.
' Used by the \StrategyCritic_Weekly scheduled task.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run """C:\Users\ferra\trading bot\run_critic_auto.bat""", 0, True
Set shell = Nothing
