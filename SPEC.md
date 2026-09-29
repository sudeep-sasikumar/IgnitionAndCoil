# SPEC: Crypto Momentum Scanner — "Ignition & Coil" (WEEX perps, signals + manual trade journal)

You are building a local Windows application for me. Read this whole spec before writing any code. Follow the milestones at the end in order and STOP after each one so I can verify it.

## 0. Working rules (non-negotiable)

1. **This app never places, modifies, or cancels orders.** It uses **public WEEX market data only**, so no exchange API keys are needed. I place all trades manually in WEEX and log them in the dashboard.
2. **Never invent API endpoints, fields, or library functions.** Work from the official WEEX contract/futures API docs, and check `ccxt` support. Third-party SDKs (e.g. `weex-sdk` on PyPI) may be used only if they are maintained and match the official docs. If something in this spec is not available, tell me and propose an alternative. Do not fake it.
3. **No lookahead.** Signals use closed candles only. The live engine and the backtester must call the same signal code.
4. **Every threshold lives in `config.yaml`.** No magic numbers in code.
5. Secrets (Telegram token, dashboard password) go in `.env` only. `.env` must be in `.gitignore`.
6. UTC internally; display times in Europe/London.
7. Do not add paid services or paid data APIs without asking me.
8. When unsure about a design choice, ask. Don't assume.

## 1. What the system does

It scans USDT-margined perpetual futures on **WEEX** continuously. It finds coins that are **just starting a strong up move** (Setup A: Ignition) or **coiled and about to break upward** (Setup B: Coil), and that have **room to run 5–8%+** before hitting resistance. It then:

- sends **signal-only** Telegram alerts (no buttons) with a complete trade plan,
- paper-trades **every** signal automatically under two stop policies, as the system's baseline,
- lets me **log trades I placed manually** on WEEX in the dashboard,
- tracks my logged trades against live prices and sends **action alerts** (TP hit, move stop, exit) as Telegram replies to the original signal,
- logs everything to a database, shows stats, and can backtest the same logic,
- produces a TradingView Pine Script companion indicator for visual checking.

Long-only for now. Holding time: minutes to a few hours.

## 2. Trade parameters

- My defaults: margin **$20**, leverage **50x**, notional **$1,000**, **isolated margin**. These defaults live in config and are editable per trade in the dashboard.
- Fees: `maker_fee: 0.0002`, `taker_fee: 0.0008` (WEEX's published standard futures rates; I will set my actual tier). All P&L figures must include fees and a slippage allowance (`slippage_pct: 0.03`). Funding settles every 8h. Include funding in P&L for any position held through a settlement.
- **Liquidation price:** compute from WEEX's maintenance-margin tiers for the symbol, for the actual leverage and entry of each trade. Include any liquidation fee. Liquidation is normally triggered on **mark price**. Confirm this at M0.

### Stop policies (both are computed for every signal)

- **Policy S (structural):** the stop goes where the setup is invalidated (see §7). It is valid only if it sits at least `liq_stop_buffer_pct` inside the liquidation price.
- **Policy L (liquidation-buffered):** the stop is placed `liq_stop_buffer_pct: 0.15` (% of price) before the estimated liquidation price.
- The baseline paper engine runs **both policies on every signal**, and the stats page compares them side by side.

## 3. Universe filter (refresh every 30 min)

At $1,000 notional my own fill won't move the book. The liquidity filter protects against spread cost, liquidation-triggering wicks, and fake volume signals on thin books.

- Active WEEX USDT linear perps. Exclude stablecoin pairs.
- `min_quote_volume_24h: 5_000_000` (USD, WEEX volume)
- `max_spread_pct: 0.08`
- `min_book_depth_usd_0_5pct: 20_000` (bid-side depth within 0.5% of mid)
- `min_listing_age_days: 7`
- `min_atr_1h_pct: 1.0` (ATR14 on 1h ÷ price)
- **Wick-risk filter:** exclude a symbol if, over the last 24h, more than `max_deep_wick_bars: 3` closed 5m bars had a lower wick ≥ `deep_wick_pct: 1.2%`.
- `max_symbols: 80`, ranked by volume.
- BTC and ETH are regime references. Exclude them from signals by default (`include_majors: false`).

## 4. Market regime (every closed 5m bar)

- BTC 1h close vs EMA50(1h) and EMA50 slope.
- BTC return over the last 3 × 15m bars. A drop below `-0.6%` is flagged `BTC_DUMP`.
- Breadth: % of the universe with 1h close > EMA20(1h).
- States:
  - `RISK_ON`: BTC above EMA50(1h), no dump, breadth ≥ 55%
  - `RISK_OFF`: BTC below EMA50(1h) and breadth < 40%, or `BTC_DUMP`
  - `NEUTRAL`: everything else
- In `RISK_OFF`, ENTRY alerts are **suppressed** (no Telegram) but still logged, shown on the dashboard, and paper-traded.

## 5. Features per symbol

Computed on each closed 5m bar unless stated otherwise:

- Returns: `ret_1h`, `ret_4h`, `ret_24h`
- Relative strength: `rs_1h = ret_1h(coin) - ret_1h(BTC)`
- Relative volume: `rvol_5m` = last closed 5m volume ÷ mean of the prior 48 bars; `rvol_15m` = last 15m ÷ mean of the prior 32 bars
- VWAP: rolling 24h VWAP from 5m bars
- EMA20/EMA50 on 15m and 1h; ATR14 on 15m and 1h
- Bollinger Band width (20, 2) on 1h and its percentile rank over the last 30 days
- **Open interest:** poll current OI every 60s and store the snapshots in SQLite, so the app builds its own OI history. Derive `oi_chg_15m`, `oi_chg_1h`, `oi_chg_4h` (%).
- **Funding:** current rate, normalised to an 8h-equivalent.
- **Order flow / CVD:** from the public trades feed (websocket preferred), aggregate taker-buy minus taker-sell volume into 5m buckets. Derive `taker_buy_ratio_15m` and `cvd_slope_1h`. If WEEX does not expose trade side, tell me at M0.
- **Resistance map and headroom:**
  - Swing highs (pivot, 3 bars each side) on 1h over the last 14 days and on 4h over the last 60 days
  - 7-day and 30-day highs
  - Volume-profile high-volume nodes from 1h bars over the last 14 days (bins of 0.25%)
  - `headroom_pct` = distance from entry to the nearest level above, ignoring levels within 0.3%.
  - If there is no level within the lookback, set `headroom_pct = 3 × ATR1h%` and flag `PRICE_DISCOVERY`.

## 6. Setups

### Setup A: IGNITION (the move has just started)

All conditions must be true on a closed 5m bar:

1. Close > highest high of the prior 144 5m bars (12h range breakout)
2. `rvol_5m ≥ 3.0` and `rvol_15m ≥ 2.0`
3. Close > 24h VWAP, and EMA20(15m) > EMA50(15m)
4. `1.0% ≤ ret_1h ≤ 5.0%` and `ret_24h ≤ 25%`
5. `rs_1h ≥ 1.0%`
6. `oi_chg_1h ≥ 2.0%`
7. `taker_buy_ratio_15m ≥ 0.55` and `cvd_slope_1h > 0`
8. `funding_8h ≤ 0.03%`
9. `headroom_pct ≥ 4.0%`
10. Breakout candle closes in the top 30% of its range, and its size is ≤ 2.5 × ATR15m

### Setup B: COIL → BREAKOUT (about to move)

**WATCH state.** All of:

- 1h BB-width percentile ≤ 15
- 1h close > EMA50(1h), with EMA50 rising over 12 bars
- `oi_chg_4h ≥ 3%` while `|ret_4h| ≤ 1.5%`
- `funding_8h ≤ 0.01%`

Coil box = high/low of the last 12 1h bars.

**ENTRY trigger** (from WATCH, within 24h). All of:

- 15m close > coil high
- `rvol_15m ≥ 2.5`
- `taker_buy_ratio_15m ≥ 0.55`
- `headroom_pct ≥ 4.0%`

### Score (0–100)

Weighted components: RVOL, RS, OI change, CVD/taker ratio, headroom, regime, funding, and wick-risk (a penalty). Weights go in config. An ENTRY alert fires only if all hard conditions pass **and** `score ≥ 70`. Every alert shows the score breakdown.

### Throttling

One ENTRY per symbol per 2h. Maximum `max_alerts_per_hour: 6`.

## 7. Trade plan (generated for every ENTRY)

- **Reference entry:** last price at signal time.
- **Structural stop (Policy S):**
  - Ignition: low of the breakout 5m candle − 0.1 × ATR15m
  - Coil: coil high − 0.25 × ATR15m
  - If this is beyond the Policy L stop, Policy S = "n/a".
- **Liquidation-buffered stop (Policy L):** estimated liquidation price (at default margin/leverage) + `liq_stop_buffer_pct`.
- **Chase limit:** `max_chase_pct: 0.5`. The alert shows the "don't enter above" price = reference × (1 + 0.5%).
- **Exit engine: hybrid.** Fixed targets first, then a market-structure trailing stop. Used for baseline paper trades and for the action alerts on my logged trades.
  - **Phase 1 (entry → TP1).** The stop stays fixed at the initial stop. Time stop: if +1.0% is not reached within `time_stop_min: 45`, exit.
  - **TP1** at +3.0%: close 40%. The stop moves to breakeven + round-trip fees.
  - **Phase 2 (TP1 → TP2).** Stop = max(breakeven + fees, chandelier). Chandelier = highest high since entry − 2.5 × ATR15m, recomputed on each closed 15m bar.
  - **TP2** = min(+6.0%, nearest resistance above entry − 0.2%), fixed at entry time: close 30%. If TP2 would be ≤ TP1 + 1.0%, drop TP2 and give that 30% to the runner.
  - **Phase 3 (runner, remaining 30%).** Stop = max(chandelier, last confirmed 15m swing low − 0.1 × ATR15m). Also exit on a 15m close below EMA20(15m).
  - **The stop only ever moves up.** Max hold `max_hold_h: 8`.
- Show $ P&L at each stop, TP1, TP2 (fees included), R-multiples, and the **break-even win rate** for each policy.

## 8. Signals, manual trades, and paper trading

### 8.1 Baseline paper trades (system performance)

- Every ENTRY signal gets a unique `signal_id` (e.g. `S-0412`). It immediately opens **baseline paper trades** at default size, one per stop policy, whatever I do. Suppressed signals (RISK_OFF, blackout) are paper-traded too, tagged as such.
- Fill model: reference entry + slippage, fees, and funding. Stops trigger on the configured price type (mark/last).
- Records entry, every exit leg, MFE, MAE, time to each target, exit reason, regime, setup, score, and policy.

### 8.2 My manual trades (journal + live tracking)

- In the dashboard I mark a signal as **"I took this"**, or add a trade **without a signal** (`signal_id` = null, source = `manual_own`).
- **Entry form fields:**
  - symbol (pre-filled from the signal)
  - entry price (pre-filled with the current price, editable)
  - entry time (defaults to now, editable)
  - **margin ($20), leverage (50x), notional ($1,000)**: config defaults, editable. The three are linked: editing margin or leverage recalculates notional; editing notional recalculates margin at the current leverage.
  - stop policy: L / S / custom. The stop price is pre-filled from the chosen policy, **recalculated from my actual entry and leverage**, and editable.
  - TP1/TP2 pre-filled from the plan, editable
  - notes
- On save, show my **estimated liquidation price** and warn if the stop is beyond it or within the buffer. Also warn if my entry is above the signal's chase limit (allowed, but flagged).
- **Closing and exit logging:** these buttons **record** what I did on WEEX. They never touch WEEX; I close positions there myself.
  - Each open trade has:
    - **Close** (price pre-filled with the current price, time defaults to now, both editable)
    - **Partial close** (price, % or qty, time)
    - **Moved stop** (new price, time)
    - **Edit** (with an audit trail)
  - **Close all open trades:** one screen listing every open logged trade with the current price pre-filled, editable per trade, confirmed with one click. This is for closing everything before I sleep.
  - P&L is computed from what I log, including fees and funding.
- **Late logging (trades placed while I'm away from the PC):**
  - Entry time, exit times, and stop-move times are all editable, so I can log trades up to hours afterwards.
  - When a trade is logged with a past entry time, the exit engine **replays from that entry time** over historical 5m candles. The dashboard then shows:
    - what the plan would have done so far (e.g. "TP1 reached at 14:22, SL should now be at breakeven 142.63")
    - which events I still need to confirm
    - the current suggested stop
  - Live tracking and action alerts continue from there.
  - Each logged price is validated against the market's high/low for that minute. Warn, but don't block.
- **Live tracking:** the engine follows each open manual trade on live WEEX prices using the exit engine (§7), with my logged entry, size, and stop. It sends action alerts (§10.2).
- If price crosses my logged stop (on mark or last, per config) and I haven't logged an exit, mark the trade `NEEDS_CONFIRMATION`, send an alert, and highlight it on the dashboard until I log the actual exit.
- Before I log exits, P&L shown is **mark-to-market (unconfirmed)**. Confirmed P&L uses only exits I have logged.

### 8.3 Stats

- **System baseline:** win rate, average R, expectancy ($ and R), profit factor, max consecutive losses, max drawdown. Break down by policy (S vs L), setup, regime, score bucket, session, and hour of day (London time).
- **My trades:** the same metrics, plus:
  - taken vs not-taken signals (does my selection beat the system?)
  - **entry gap** (my entry vs the signal reference price)
  - **plan adherence** (my actual exits vs what the exit engine would have done on my entry)
  - signal trades vs own trades

### 8.4 Database (everything is logged; nothing is ever deleted)

- SQLite through SQLAlchemy, so it can move to Postgres on the VPS later without code changes.
- **Tables:**
  - `signals`: all features, score breakdown, plan, config hash, whether an alert was sent or suppressed
  - `paper_trades`: baseline, per policy
  - `manual_trades`: entry form data, linked `signal_id` (nullable)
  - `manual_trade_events`: append-only log of partial closes, stop moves, edits (old → new), exits, NEEDS_CONFIRMATION
  - `trade_events`: exit-engine events for paper and manual trades
  - `alerts_log`: every Telegram message, with its message ID for threading
  - `oi_snapshots`
  - `incidents`: errors, disconnects, stale data
  - `config_versions`
- Automatic daily backup copy of the DB file (keep 30).
- `tools/export_trades.py`: CSV export of manual trades and/or paper trades by date range, for my records and tax.

## 9. Backtester

- Download historical 5m/15m/1h klines (plus any OI and funding history the API offers) for the universe over a configurable number of days.
- Replay through the **same** signal, plan, and exit-engine code, with both stop policies.
- If OI or CVD history is unavailable, run with those filters disabled and state that clearly in the report.
- Output an HTML report with the same stats as the baseline paper engine.
- Include a no-lookahead unit test.

## 10. Telegram (signal and information only; no buttons, no trading)

- Bot token and my chat ID in `.env`. Include a helper command (`python tools/get_chat_id.py`) that prints my chat ID after I message the bot.
- Private chat only. Respond only to my chat ID.

### 10.1 ENTRY signal message

Contents:

- setup, symbol, score, regime, `signal_id`
- reference entry and "don't enter above" price
- both stops with $ loss (at default size) and break-even win rate
- estimated liquidation price at default leverage
- TP1, TP2, and the trailing rule in one line
- key metrics
- time to the next funding settlement
- TradingView link
- dashboard link to that signal

Example:

```
🚀 IGNITION — SOLUSDT | score 82 | RISK_ON | #S-0412
Entry ~142.30 | Don't enter above 143.01 | Liq ~139.90 (50x, mark)
Stop L 140.11 (-1.54%) → -$17.0 | BE win rate 27%
Stop S 141.05 (-0.88%) → -$10.4 | BE win rate 18%
TP1 146.57 (+3%, close 40%) | TP2 150.80 (+6%, close 30%)
Runner 30%: chandelier / 15m swing-low trail, stop only moves up
RVOL 4.2x | RS +2.1% | OI +3.4% 1h | Taker 0.61 | Fund 0.008% (next in 2h14m)
📈 TradingView: <link> | 📋 Log trade: <dashboard link>
```

(The numbers above are illustrative only.)

### 10.2 Action alerts for my logged trades

These are sent **as replies to the original signal message** (or as a new message for own trades). Each one tells me exactly what to do on WEEX:

- **On logging:** "Tracking S-0412: entry 142.35, 50x, liq ~139.95. Set your SL on WEEX at 140.16 now."
- **TP1 reached:** "Close 40% at ~146.6. Move SL to breakeven + fees: 142.63."
- **Trail update:** "Move SL to X." Sent only when the suggested stop rises ≥ `trail_notify_min_pct: 0.5` above my last logged stop, at most once per 15 min per trade.
- **TP2 reached**, **EMA exit**, **time stop**, **max hold**: "Close remaining at market."
- **Stop level crossed:** "Price hit your SL level 140.16. Log your exit on the dashboard."
- **Chase warning:** my logged entry is above the chase limit.
- After I log a stop move or exit, alerts use my logged values, not the plan's.
- Action alerts exist only for **logged** trades. For trades I placed while away and haven't logged yet, the original signal message (stops, TP1/TP2, trailing rule) is my plan until I log them.

### 10.3 Other messages

- Baseline paper results: `paper_updates: summary | off`. Default `summary` = one line per closed paper trade, threaded to its signal.
- **Bedtime reminder:** at `bedtime_reminder: "23:30"` (London, editable in Settings), if any logged trades are open, send the list with current P&L and suggested exit, plus "Close on WEEX, then log it via Close all on the dashboard". Repeat every 30 min (`bedtime_repeat_min: 30`) until no logged trades are open, or `/snooze` is sent. `/snooze` silences the reminder for the night.
- **Unconfirmed trades:** in the same reminder, list trades flagged NEEDS_CONFIRMATION.
- Other alerts: WATCH (can be muted), and a daily summary at 23:00 London with the system baseline and my trades shown separately.
- Commands (text only):
  - `/status`, `/open` (my open manual trades), `/top`, `/stats`
  - `/mute`, `/unmute`, `/mutepaper`
  - `/pause` and `/resume` (signals)
  - `/snooze` (bedtime reminder, tonight only)

## 11. TradingView companion

TradingView is **not** a data source or a runtime dependency. It is used only for visual verification.

- **Chart links:** each alert links to TradingView for that symbol, using `tv_symbol_template` in config. Check at M0 whether WEEX perps are listed on TradingView. If not, fall back to a configurable major-exchange perp symbol, and verify the correct symbol format rather than guessing.
- **Pine Script v6 indicator** (`tradingview/ignition_coil.pine`): replicates the price/volume parts of Ignition and Coil (range breakout, RVOL, VWAP, EMAs, BB-width squeeze, coil box) and plots markers where they fire. State clearly in the script header which conditions (OI, CVD, funding, headroom) are **not** replicated.

## 12. Dashboard (local web UI)

- FastAPI backend, single-page frontend with a websocket for live updates, dark theme. I use it on my PC; the layout should still be usable on a phone. Bundle all JS/CSS locally (no CDN) so it works inside the .exe.
- Served at `http://localhost:8000`, auto-opened in the browser on start.
- Config option `bind_host`: `127.0.0.1` (default, localhost only). If it is ever set to anything else (e.g. on the VPS later), **a login is required** (password from `.env`, session cookie).
- **Header:** regime, BTC price and 1h change, breadth, data connection status, last scan time.
- **Scanner table:** symbol, price, state (WATCH/ENTRY/SKIP), score, ret_1h, rvol, rs_1h, OI Δ1h, funding, taker ratio, headroom, wick-risk. Sortable, colour-coded.
- **Signal page** (the target of the Telegram link): full plan, a live 15m chart (TradingView Lightweight Charts library, bundled) with VWAP, EMAs, resistance levels, coil box, both stops, liquidation price, and TPs. Includes the **"I took this"** entry form (§8.2).
- **My trades:** open trades with live mark-to-market P&L, current suggested stop vs my logged stop, next action, and the Close / Partial close / Moved stop buttons, plus a prominent **Close all** button. Plus closed trades and anything flagged NEEDS_CONFIRMATION.
- **Add own trade** (no signal).
- **Stats page:** system baseline (S vs L), my trades, the taken/not-taken comparison, entry gap, plan adherence.
- **Settings:** default margin, leverage, and notional (editable, saved to config), bedtime reminder time, fee rates, and a read-only view of other config.

## 13. Architecture and packaging

- Python 3.12, asyncio. Suggested layout:
  `exchange/ data/ features/ levels/ signals/ plan/ exits/ paper/ journal/ backtest/ notify/ web/ tradingview/ tools/ tests/`
- In-memory ring buffers for candles; SQLite for everything persistent.
- Respect WEEX public rate limits. Reconnect with exponential backoff; clock-drift check against the WEEX server time endpoint; rotating log files; watchdog that alerts on Telegram if data goes stale for more than 2 minutes.
- One entry point: `python main.py` runs the engine and the web server.
  - `run.bat` for Windows.
  - PyInstaller spec to build `MomentumScanner.exe`.
- `Dockerfile` + `docker-compose.yml` for later deployment to a Linux VPS (with the dashboard login enforced and HTTPS via a reverse proxy).
- `README.md` with step-by-step Windows setup written for me.
- Unit tests for indicators (checked against known values), liquidation-price calculation at various leverages, the margin/leverage/notional linking, the exit engine, signal logic (synthetic data), and no-lookahead.

## 14. Other scenarios to handle (and log in `incidents` where relevant)

- **Away from the PC (short trips, PC left running):** signals and action alerts for already-logged trades keep coming to Telegram. Trades I place while away are untracked until I log them. Late logging (§8.2) replays them on return.
- **PC sleep / Windows Update restarts / internet loss:** alerts and tracking stop while the app is down. My SL on WEEX is my only protection, which is why the "set your SL now" alert matters.
  - The README must explain how to stop Windows sleeping and how to set active hours so Windows Update doesn't restart the PC while I'm out.
  - Show a startup warning if the Windows power plan allows sleep.
  - On restart, resume tracking open manual trades and send a "back online" summary. If a stop or target was crossed while offline, flag the trade NEEDS_CONFIRMATION.
- **Clock drift:** check against WEEX server time at startup and hourly. Warn if drift > 1s.
- **Stale data or exchange maintenance:** pause signals, alert, and resume automatically when data is healthy again.
- **Symbol suspended or delisted:** drop it from the universe. If I have a logged open trade in it, alert immediately.
- **Several signals at once:** send them in score order.
- **New signal on a symbol where I have an open logged trade:** still send it, labelled "you already hold this".
- **News blackout:** `blackout_windows` in config (e.g. CPI/FOMC times, entered manually). Signals during a window are labelled `BLACKOUT` and suppressed from Telegram by default (`blackout_alerts: false`).
- **Weekend / low-liquidity hours:** tag signals with the session, and break down stats by session.
- **Data-entry mistakes:** validate the entry price against the market range at the given time. Warn (don't block) if it's outside that minute's high/low. All edits are kept in the audit trail.
- **Restart:** never re-send an alert that was already sent (dedupe by `signal_id` and event).

## 15. Milestones — stop after each and wait for my go-ahead

- **M0 — WEEX public-data capability report.** A table covering:
  - kline history depth per timeframe
  - current and historical OI
  - funding (current and history)
  - trades feed with taker side (REST and WebSocket)
  - order book depth
  - maintenance-margin tiers and liquidation fee
  - whether liquidation uses mark price
  - mark price availability
  - min order / contract sizes
  - public rate limits
  - `ccxt` support status
  - whether WEEX perps are on TradingView

  Include proposed fallbacks for any gaps. Write no app code yet.
- **M1** — Data layer, universe filter (including wick-risk), indicators, regime. Console output.
- **M2** — Levels/headroom, setups, scoring, trade plan with both stop policies, liquidation calculation, Telegram signal alerts.
- **M3** — Dashboard (scanner, signal pages, settings; login only if bound beyond localhost).
- **M4** — Database (full schema §8.4), exit engine, baseline paper trading, manual trade journal with linked margin/leverage/notional, late logging with replay, Close / Close all, live tracking with threaded action alerts, bedtime reminder, stats, CSV export.
- **M5** — Backtester and HTML report.
- **M6** — Pine Script companion, `.exe` packaging, `run.bat`, Dockerfile, README.
