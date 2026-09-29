# Ignition & Coil — WEEX momentum scanner

Scans WEEX USDT perpetual futures for coins that are **just starting a strong up move** (Ignition) or **coiled and about to break out** (Coil), with room to run. It sends a complete trade plan to Telegram, paper-trades every signal, and gives you a journal for the trades you place yourself.

> **This app never places, changes or cancels orders.** It only reads public WEEX market data, so it needs **no exchange API keys**. You place every trade on WEEX yourself and record it in the dashboard.

Contents:
1. [Setup on Windows](#1-setup-on-windows-step-by-step)
2. [Telegram alerts](#2-telegram-alerts)
3. [Keeping it running](#3-keeping-it-running-sleep-windows-update-auto-start)
4. [Daily use](#4-daily-use)
5. [Your trading model](#5-your-trading-model-20x--policy-s)
6. [Backtesting and tuning](#6-backtesting-and-tuning)
7. [TradingView companion](#7-tradingview-companion)
8. [The .exe version](#8-the-exe-version)
9. [Settings and config.yaml](#9-settings-and-configyaml)
10. [Running on a VPS](#10-running-on-a-vps-hostinger)
11. [Troubleshooting](#11-troubleshooting)
12. [Files and folders](#12-files-and-folders)

---

## 1. Setup on Windows (step by step)

**You need:** Windows 10 or 11, an internet connection, and about 10 minutes.

1. **Install Python 3.12.** Download it from https://www.python.org/downloads/, run the installer and **tick "Add python.exe to PATH"** on the first screen. (Already done on this PC.)
2. **Put this folder somewhere permanent,** e.g. `E:\Ignition & Coil`. The database and logs will live inside it, in `var\`.
3. **Double-click `run.bat`.** The first time, it creates a private Python environment (`.venv`) and installs everything, which takes a few minutes. It also creates a `.env` file for your secrets.
4. **Your browser opens the dashboard** at http://localhost:8000. For about 30 seconds it shows "Starting…" while it builds the list of coins and loads history. The console window then prints a scan every 5 minutes.
5. **Set up Telegram** (section 2) so alerts reach your phone. Until then they print in the console.

**To stop:** close the console window, or press Ctrl+C in it.
**To start again:** double-click `run.bat` (or use the `.exe`, section 8).

> Only run **one** copy at a time. If the dashboard says port 8000 is already in use, an older copy is still running: close it first.

---

## 2. Telegram alerts

1. In Telegram, open **@BotFather**, send `/newbot`, choose a name and a username, and copy the **token** it gives you.
2. Open the `.env` file in this folder with Notepad and paste the token after `TELEGRAM_BOT_TOKEN=`. Save.
3. Open your new bot in Telegram and send it any message (e.g. `hi`).
4. Double-click `run.bat` once to make sure everything is installed, close it, then open a terminal in this folder (in Explorer, type `cmd` in the address bar and press Enter) and run:
   ```bash
   .venv\Scripts\python.exe tools\get_chat_id.py
   ```
   Put the number it prints into `.env` after `TELEGRAM_CHAT_ID=`. Save.
5. Start the scanner again. You should receive **"🟢 Back online"**.

The bot only talks to your chat ID and ignores everyone else. It has no buttons and cannot trade.

**What you'll receive**
- **ENTRY:** the setup, coin, score, regime and signal ID, followed by:
  - the entry, a "don't enter above" price and the liquidation price
  - **your stop (Policy S) first**, marked ◀, then the Policy L stop, each with the $ loss and break-even win rate
  - TP1/TP2 and the runner rule
  - key numbers, time to the next funding, and links to TradingView and the dashboard
- **Action alerts** for trades you've logged, as replies under the signal: set your SL, TP1 reached (close 40%, move SL), move SL, close the rest, price hit your SL.
- **WATCH:** a coin started coiling.
- **Paper results:** one line per closed paper trade.
- **Daily summary** at 23:00 London.
- **Bedtime reminder** at 23:30 while you have open trades.
- **System messages:** back online, data stale/recovered, coin delisted, clock drift.

**Commands:**
- `/status`, `/top`, `/open` (your open trades), `/stats`
- `/pause` and `/resume`: ENTRY alerts off or on. Signals are still recorded.
- `/mute` and `/unmute`: WATCH alerts off or on.
- `/mutepaper`: paper-result lines off or on.
- `/snooze`: silence tonight's bedtime reminder.

**When an ENTRY is recorded but *not* sent:**
- The regime is RISK_OFF.
- It falls inside a news blackout window.
- `/pause` is on.
- The limit of 6 alerts per hour is reached.
- Your stop (Policy S) doesn't fit the signal.

All of these still appear on the dashboard with the reason, and are paper-traded. A second signal for the same coin within 2 hours isn't created at all.

---

## 3. Keeping it running (sleep, Windows Update, auto-start)

While the PC is asleep, off or offline, **no alerts or trade tracking happen**. The stop you set on WEEX is then your only protection. That's why "Set your SL on WEEX now" is the first message after you log a trade. The app shows a warning at startup if Windows is set to sleep.

**Stop Windows from sleeping (when plugged in)**
- Go to Settings → System → Power (& battery) → Screen, sleep & hibernate timeouts, and set *"When plugged in, put my device to sleep after"* to **Never**. The screen may still turn off; that's fine.
- Or run this once in a terminal:
  ```bash
  powercfg /change standby-timeout-ac 0
  ```
  and this:
  ```bash
  powercfg /change hibernate-timeout-ac 0
  ```
- **Laptop:** in Control Panel → Power Options → *Choose what closing the lid does*, set "Plugged in" to **Do nothing**.
- **Wi-Fi/Ethernet:** in Device Manager → Network adapters → your adapter → Properties → Power Management, **untick** "Allow the computer to turn off this device to save power".

**Stop Windows Update restarting the PC while you're out**
- In Settings → Windows Update → Advanced options → **Active hours**, choose *Manually* and cover your trading day, e.g. 07:00 to 01:00 (18 hours maximum). Windows won't restart automatically inside them.
- In the same place, turn **"Get me up to date"** off and **"Notify me when a restart is required"** on.
- Before a trip, use **Pause updates** (Settings → Windows Update) for a week.
- Install updates when *you* choose, with no trades open.

**Start automatically when Windows starts (optional)**
1. Press Win+R, type `shell:startup` and press Enter.
2. Right-click → New → Shortcut, and point it at `run.bat` (or `MomentumScanner.exe`).

**Keep the clock right:** in Settings → Time & language → Date & time, turn on *Set time automatically* and click *Sync now*. The app warns if your clock is more than 1 s off WEEX's. It corrects for the difference internally.

**What happens after a restart**
- The app resumes tracking your open trades and replays the candles it missed.
- If your stop or a target was crossed while it was off, the trade is flagged **NEEDS CONFIRMATION** and you get an alert.
- You also get a **"Back online"** summary.
- `run.bat` restarts the app automatically after a crash.

---

## 4. Daily use

### The dashboard (http://localhost:8000)
- **Header:** regime, BTC price and 1h change, breadth, data status, last scan time, your open trades, and anything waiting for confirmation (red).
- **Scanner:** every coin being watched, with state (ENTRY / WATCH / SKIP), score, conditions met, returns, volume, open interest, funding, order flow, headroom and wick risk. Click a column to sort; hover a row to see what failed; click it for the coin's page.
- **Coin page:** a live 15-minute chart (VWAP, EMAs, resistance levels, coil box) and the ✓/✗ checklist for both setups.
- **Signal page** (📋 link in every alert):
  - the full plan, with **your stop marked**, and a live chart with every level drawn
  - why it fired, and whether the alert was sent (and if not, why)
  - its baseline paper trades
  - the **I took this** form
- **My trades:** open trades with live, unconfirmed P&L, your stop vs the suggested stop, the next action, and **Close / Partial close / Moved stop / Edit** buttons. There's also **Close all open trades** and **+ Add own trade**.
- **Stats:**
  - the system's paper results (Policy L vs S, broken down by setup, regime, score, session, hour and alert status)
  - your own results
  - whether your selection beats the system, your entry gap, and your plan adherence
- **Settings:** default margin, leverage and notional (linked), fees and the bedtime time. They're saved into `config.yaml` and recorded in the history.

It also works on a phone screen, but only on this PC's network address (it listens on this computer only). See section 10 for remote access.

### Logging a trade you placed on WEEX
1. Open the signal page (the 📋 link in the alert) and fill in **I took this**. Or use **My trades → + Add own trade** for a trade without a signal.
2. Everything is pre-filled and editable: current price, the time now, **$50 margin × 20x = $1,000**, **Policy S stop** recalculated from your actual entry, and TP1/TP2.
3. On save you see your liquidation price, plus warnings if your stop is beyond it or you entered above "don't enter above". The trade is saved either way.
4. Telegram replies: **"Tracking S-0412 … Set your SL on WEEX at … now."** Do that on WEEX straight away.

**Logging late is fine.** Set the real entry time; the plan is replayed from then and shows what you still need to do. Every price and time can be corrected later. Nothing is ever deleted: corrections are kept in an audit trail.

### Following the plan
When Telegram says **TP1 reached / Move SL to … / Close remaining**, do it on WEEX, then log it with **Partial close / Moved stop / Close**. After you log a stop move or exit, alerts use *your* numbers.

If price crosses your stop and you haven't logged an exit, the trade turns **red (NEEDS CONFIRMATION)** until you log what happened, or press **Still open** if it really is.

### Before bed
1. Close everything on WEEX.
2. Go to **My trades → Close all open trades**, check the pre-filled prices, and confirm once.

The 23:30 reminder repeats every 30 minutes while trades are open; `/snooze` silences it for the night.

### Records and tax
```bash
.venv\Scripts\python.exe tools\export_trades.py --from 2026-10-01 --to 2026-10-31
```
This writes CSV files to `var\exports\` (`--what manual|paper|both`). The database is also copied every day to `var\backups\` (the last 30 are kept).

---

## 5. Your trading model: 20x · Policy S

Chosen on 2026-09-28 from a one-year walk-forward study (full details in `docs\DECISIONS.md`):

| Setting | Value |
|---|---|
| Position | **$50 margin × 20x = $1,000** (isolated) |
| Stop | **Policy S**: the structural level where the setup is invalid (below the breakout candle / inside the coil box) |
| Why 20x | Liquidation sits ~4.8% away, so the structural stop fits on almost every signal. At 50x (~1.8%) it rarely fits. |
| Risk per trade | ~$18–19 on average (depends on the chart) |
| Targets | TP1 +3% (close 40%, SL to breakeven + fees), TP2 up to +6% (30%), runner on a trailing stop; time stop 45 min, max hold 8h |

**What the backtests say, honestly.** Out-of-sample, the model averaged roughly **+0.13 to +0.23R per trade**. There's about a 5–20% chance the true edge is zero or negative, depending on the window. Results come in bursts: over a year, 6 of 12 months were negative, and the profit came from a few strong months. Live results will also differ:
- Open-interest data isn't available historically, so the backtests couldn't check it.
- You enter by hand after an alert, so your entries will be later than the backtest's.

Start small, let the Stats page (which compares paper trades and your trades) confirm or reject the edge, and re-check with the backtester every few months. **This is not financial advice.**

---

## 6. Backtesting and tuning

```bash
.venv\Scripts\python.exe -m backtest --days 90 --open
```
Replays history through **the same code the live scanner uses**, under both stop policies. It writes an HTML report and a trades CSV to `var\backtests\`.
- **First run:** downloads history, about 25 minutes per 40 coins × 30 days. The download shares WEEX's rate limit safely with a running scanner.
- **Later runs:** reuse the cache in `var\bt_cache\`.

**Read the "Data & limitations" box first:**
- the open-interest conditions are off
- the spread and depth filters can't be applied
- only coins listed today are included
- entries are instant, and stops are checked pessimistically inside a candle

Useful variants:
- `--symbols SOLUSDT,WIFUSDT` tests specific coins.
- `--set score.min_entry=60 --tag score60` tries a setting for one run without changing `config.yaml`.
- `python -m backtest.sweep --days 90` searches 1,620 threshold combinations. It picks on the older part and checks on the newer part.
- `python -m backtest.study --days 365` is the walk-forward comparison of leverage × stop policy that chose your model. It takes about 2 hours the first time.

Always confirm a change with a normal backtest before using it live.

---

## 7. TradingView companion

`tradingview\ignition_coil.pine` is a Pine Script v6 indicator for checking signals **visually**. TradingView is never used by the app itself.
1. In TradingView, open a **5-minute** chart of a WEEX perpetual, e.g. `WEEX:SOLUSDT.P`.
2. Open the **Pine Editor**, paste the whole file, click **Add to chart**.
3. The indicator shows:
   - **IGN** and **COIL** markers where the price/volume conditions fire
   - the 24h VWAP and the 15m EMAs
   - the coil box, shaded while a coil is being watched
   - a ✓/✗ table of the conditions

It replicates the breakout, volume, VWAP, EMAs, returns, BTC strength, candle shape and Bollinger squeeze, with the same defaults as `config.yaml`. It **cannot** check open interest, order flow (taker ratio, CVD), funding or headroom (the header of the file says so). So it shows **more markers than the app sends**. Alert links for coins TradingView doesn't list under WEEX fall back to Binance: run `tools\check_tv_symbols.py` now and then to refresh that list.

---

## 8. The .exe version

Double-click **`build_exe.bat`**. It builds `dist\MomentumScanner\MomentumScanner.exe` and copies `config.yaml`, `.env` and the README next to it. You can then move the whole `dist\MomentumScanner` folder anywhere and start the exe directly; no Python is needed on that PC.
- **Everything next to the exe:** it keeps its own `config.yaml`, `.env` and `var\` (database, logs) in its folder.
- **Keeping your history:** to carry over your trades and records, stop the scanner and copy your `var` folder into `dist\MomentumScanner`.
- **Console window:** it stays open, showing the live scan table and warnings. Closing it stops the scanner.
- **Antivirus:** some antivirus tools are suspicious of new, unsigned exes built with PyInstaller. If yours blocks it, allow it or use `run.bat`.

---

## 9. Settings and config.yaml

- **Dashboard → Settings** edits the everyday values: margin, leverage, notional, fees and the bedtime time. Changes apply to new signals immediately.
- **Everything else is in `config.yaml`**, with a comment on each line: thresholds, score weights, the universe filters, the exit rules, and so on. Edit it with Notepad and restart the app. Every version is recorded (config history on the Settings page).
- **Secrets** live only in `.env`: the Telegram token and chat ID, and the dashboard password. Never share that file.
- **News blackouts** (e.g. CPI or FOMC) go under `signals: blackout_windows:`. Times are London time:
  ```yaml
  - {start: "2026-10-14 13:20", end: "2026-10-14 13:50", label: "US CPI"}
  ```

---

## 10. Running on a VPS (Hostinger)

The scanner runs 24/7 in Docker on the VPS, with HTTPS and a login. GitHub builds the app image automatically on every push; Hostinger's **Docker Manager** downloads and runs it. No files to copy, nothing to install by hand.

**Before you start**
- The VPS must use Hostinger's **Docker** OS template (hPanel → VPS → *OS & Panel*). Changing the OS template **wipes the VPS**, so do it only on a VPS with nothing else on it.
- Nothing else on the VPS may use ports 80/443 (e.g. an n8n or Traefik template).
- The repository and its image (GitHub → your profile → *Packages* → `ignitionandcoil`) must be **public**, so the VPS can download the image without a login.

**You supply 4 values**

| Variable | What it is |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (section 2) |
| `TELEGRAM_CHAT_ID` | your chat id (section 2: `tools\get_chat_id.py`) |
| `DASHBOARD_PASSWORD` | the dashboard login, make up a long one |
| `DOMAIN` | the VPS **hostname** from hPanel's VPS overview, e.g. `srv123456.hstgr.cloud` (or your own domain pointed at the VPS) |

**Steps**
1. hPanel → **VPS** → **Docker Manager** → **Compose** → **Compose from URL**.
2. URL: `https://raw.githubusercontent.com/sudeep-sasikumar/IgnitionAndCoil/master/docker-compose.yaml`. Project name: `ignition-coil`.
3. Enter the 4 variables above in the environment variables section.
4. Click **Deploy**. The first build takes a few minutes.
5. Open `https://<DOMAIN>` and sign in with `DASHBOARD_PASSWORD`. Telegram sends **"🟢 Back online"**.
6. **Stop the scanner on your PC** and don't start `run.bat` again: two scanners send double alerts and split the journal.

**Afterwards**
- It restarts by itself after crashes and VPS reboots.
- **New code:** after a push to `master`, wait for the green tick under the repository's *Actions* tab (about 2 minutes), then press **Redeploy** in Docker Manager.
- **Settings page** changes (margin, leverage, fees, bedtime reminder) survive redeploys. Thresholds come from the repository's `config.yaml`.
- **Logs:** Docker Manager → the project → *Logs* (service `scanner`).
- Data (database, logs, daily backups) lives in the Docker volume `scanner_data`, which survives redeploys. Deleting the project in Docker Manager may delete it.

How it fits together: `.github/workflows/docker-image.yml` builds the `Dockerfile` into `ghcr.io/sudeep-sasikumar/ignitionandcoil:latest` on every push. Hostinger downloads only `docker-compose.yaml`, pulls that image and runs Caddy in front for automatic HTTPS. `deploy/docker_start.py` keeps the live `config.yaml` in the data volume. Moving to PostgreSQL later needs no code changes: install a Postgres driver and set `database.url`.

*Not yet run in Docker: Docker isn't installed on this PC. The config handling and the login/host settings were tested here.*

---

## 11. Troubleshooting

| Problem | What to do |
|---|---|
| "port 8000 is already in use" | Another copy is running. Close it (check the taskbar for a console window). |
| No Telegram messages | Check `.env` (token + chat ID, no spaces) and run `tools\get_chat_id.py` again. The dashboard header shows "Telegram off" if it isn't configured. |
| "DATA STALE" | Internet or WEEX problem. Signals pause and resume automatically when data is healthy again. |
| Windows sleep warning at start | See section 3. |
| Dashboard blank or "disconnected" | The scanner isn't running, or is still starting (~30 s). Look at the console. |
| A trade is red (NEEDS CONFIRMATION) | Price crossed your logged stop or a plan exit. Log the close, or press **Still open** if it really is. |
| Something else | Look in `var\logs\scanner.log` (newest at the bottom). |

Health check (runs one scan and exits):
```bash
.venv\Scripts\python.exe main.py --once
```

Tests:
```bash
.venv\Scripts\python.exe -m pytest -q
```

---

## 12. Files and folders

| Path | What it is |
|---|---|
| `run.bat` | Start the scanner + dashboard (first run installs everything) |
| `build_exe.bat`, `MomentumScanner.spec` | Build the Windows `.exe` |
| `config.yaml` | Every threshold and setting (commented) |
| `.env` | Your secrets (from `.env.example`) |
| `var\scanner.db` | Database: signals, paper trades, your trades and events, alerts, OI history, incidents, config versions. Nothing is ever deleted. |
| `var\logs\` | Rotating log files |
| `var\backups\` | Daily database copies (last 30) |
| `var\backtests\` | Backtest, sweep and study reports |
| `var\exports\` | CSV exports |
| `tradingview\ignition_coil.pine` | TradingView companion indicator |
| `tools\` | `get_chat_id.py`, `export_trades.py`, `check_tv_symbols.py` |
| `docs\M0_capability_report.md` | What WEEX's public data offers (and its quirks) |
| `docs\DECISIONS.md` | Every design choice and threshold change, with the evidence |
| `Dockerfile`, `docker-compose.yaml`, `deploy\` | VPS deployment (Hostinger Docker Manager) |

**Milestones:** M0–M6 complete.
