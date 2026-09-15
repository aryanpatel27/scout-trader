# Moving agentic-trader from the MacBook to the always-on PC

## ⚠️ Rule zero: only ONE machine may be LIVE at a time
Two live engines = duplicate real orders. Disarm the Mac BEFORE arming the PC.

## 1. Disarm the Mac (on the Mac)
    cd ~/Desktop/"Agentic Trading" && sed -i '' 's/^mode = "live"/mode = "paper"/' config.toml \
      && launchctl bootout gui/$(id -u)/com.aryanpatel.agentic-trader && echo "Mac DISARMED + stopped"
(do this after the 15:55 flatten so no position is orphaned, or confirm positions = 0 on the dashboard)

## 2. Copy the project folder to the PC
- Copy the whole `Agentic Trading` folder (USB / AirDrop→OneDrive / network share) to e.g. `C:\AgenticTrading`
- Do NOT copy `.env` over shared cloud storage — re-type the three keys on the PC instead.
- `trader.db` carries your journal history; keep it.

## 3. On the PC
1. Install Python 3.11 from python.org (tick "Add python.exe to PATH" and "py launcher").
2. Install Chrome + Tampermonkey; import `bridge\ybi_bridge.user.js`; enable "Allow User Scripts".
3. Log into app.youngbullinvestors.com in Chrome; open & PIN the three tabs
   (#premarket-alerts, #intraday-alerts, #live-commentary). Chrome must stay open.
4. PowerShell in the project folder:
       Set-ExecutionPolicy -Scope Process Bypass
       .\deploy\windows\setup.ps1
5. Create `.env` (copy `.env.example`), fill WEBULL_APP_KEY / WEBULL_APP_SECRET /
   WEBULL_ACCOUNT_ID (the long account ID). Leave LIVE_TRADING_ACK commented until step 7.
6. Settings → System → Power → Sleep: **Never** (plugged in). Keep the Windows user logged in.
7. Arm (your decision, same two gates as the Mac): in config.toml set mode = "live";
   in .env uncomment LIVE_TRADING_ACK=I_UNDERSTAND_THE_RISKS.
8. Start: `schtasks /Run /TN AgenticTrader` → open http://localhost:8787 → expect
   MODE LIVE · ARMED and the "LIVE broker ARMED — cash power $…" event.

## Daily reality on the PC
- Dashboard: http://localhost:8787 on the PC (localhost-only by design).
- Reports: `reports\paper-YYYY-MM-DD.md` (name kept; it records live trades too).
- Kill switches: dashboard "Pause entries"; or set mode = "paper" in config.toml and
  `schtasks /End /TN AgenticTrader && schtasks /Run /TN AgenticTrader`.
