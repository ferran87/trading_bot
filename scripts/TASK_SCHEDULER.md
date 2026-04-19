# Scheduling the Trading Bot on Windows

This document explains how to wire `scripts/run_once.ps1` into Windows
Task Scheduler so the bot runs twice daily without you touching it.

## Why two runs per day?

- **08:00 CET (pre-market EU)** — Bot 1 (ETF momentum) rebalances on
  Mondays; the others evaluate signals once a day before the EU open.
- **22:30 CET (post-US-close)** — Records EOD equity snapshots using
  fresh US closing prices so the dashboard shows consistent daily bars.

Both runs are idempotent: guardrails reject extra same-day trades, and
the equity snapshot overwrites the day's row if it already exists.

## IB Gateway prerequisite

Gateway must be **running and logged into the paper account** at the
scheduled time. Two ways to handle this:

1. **Easy mode — stay logged in for 24h:** open Gateway → *Configure →
   Settings → Lock and Exit → Auto Logoff* and pick 23:59. It will still
   auto-logoff once a day (IBKR requirement) but otherwise stays up.
2. **Robust — IBC (IB Controller):** install
   [IBC](https://github.com/IbcAlpha/IBC) and wrap Gateway in it. IBC
   supplies credentials, handles the nightly re-login, and restarts
   Gateway if it crashes. Recommended if you plan to leave the bot
   running unattended for weeks.

## Creating the scheduled tasks

Open an **elevated** PowerShell (Run as Administrator) and run the
snippets below. Replace `C:\Users\ferra\trading bot` with your repo path
if it differs.

### Morning run — weekdays 08:00 CET

```powershell
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\ferra\trading bot\scripts\run_once.ps1"'

$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 08:00

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName 'TradingBot_Morning' `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Pre-market run of trading bots'
```

### Evening run — weekdays 22:30 CET

```powershell
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 22:30

Register-ScheduledTask -TaskName 'TradingBot_Evening' `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Post-US-close equity snapshot for trading bots'
```

`$action` and `$settings` are reused from the morning snippet — run them
in the same PowerShell session.

## Verifying

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like 'TradingBot_*' } `
    | Select-Object TaskName, State
Start-ScheduledTask -TaskName 'TradingBot_Morning'   # trigger immediately
Get-ScheduledTaskInfo -TaskName 'TradingBot_Morning' | Select-Object LastRunTime, LastTaskResult
Get-Content "C:\Users\ferra\trading bot\data\logs\run_*.log" | Select-Object -Last 50
```

`LastTaskResult = 0` = success. Any non-zero value means the script
itself crashed (venv missing, Gateway down, etc.) — check the log file.

## Timezone notes

Windows Task Scheduler runs in **local time**. If your machine is set to
Europe/Madrid, the schedule above is already CET/CEST. For a machine in
another zone, either change its timezone or convert the trigger times.

## Removing a task

```powershell
Unregister-ScheduledTask -TaskName 'TradingBot_Morning' -Confirm:$false
Unregister-ScheduledTask -TaskName 'TradingBot_Evening' -Confirm:$false
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `LastTaskResult = 1` and log shows `IBKRBroker: connected but no managed accounts` | Gateway is running but not logged in | Log in, or switch to IBC for auto-login |
| `LastTaskResult = 0` but no trades in DB | Market was closed (weekend/holiday) or no signal met guardrails | Normal — check `data/logs/run_*.log` for rejection reasons |
| Task runs at wrong clock time after DST switch | Trigger saved in UTC | Delete and re-register after DST change, or add two triggers (winter/summer) |
