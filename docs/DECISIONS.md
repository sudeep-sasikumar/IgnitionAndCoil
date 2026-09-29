# Design decisions log

Choices made where the spec left room. Each can be changed in `config.yaml` unless noted.

## M1

| Topic | Decision | Why |
|---|---|---|
| CVD source | Canonical = kline taker volume (after the WEEX field fix); the WS trade tape cross-checks every closed 5m bar. | The two are identical when the tape is complete (verified live, 37/37 bars). Using klines gives the backtester the exact same input as live. |
| Closed bars | Fetched over REST 3 s after each boundary, retried until present. | WEEX's WS klines have no "closed" flag. |
| Funding "current rate" | `forecastFundingRate` (the next settlement). `funding.rate_field` | `lastFundingRate` is the rate already settled (checked against funding history). |
| Majors | BTC and ETH are tracked as references; only BTC feeds the regime rules. | Per spec §4. |

## M2

| Topic | Decision | Why |
|---|---|---|
| Missing data | A condition whose data is missing (e.g. OI before 1h of history) **fails**. `signals.disabled_conditions` switches whole groups off (for backtests). | Never alert on unverified conditions. |
| Score ramps | Linear ramps start below the hard minimums: a bare-minimum pass scores ~50, a solid setup in RISK_ON ~70. Disabled components are dropped and the rest rescaled to 100. | With ramps starting at the minimums, almost nothing could reach 70. **Provisional; to be calibrated with the M5 backtest.** |
| Policy L stop | `liq × (1 + 0.15%)`, rounded **up** to the tick. | Matches the spec example (139.90 → 140.11). Rounding up keeps the buffer. |
| Policy S validity | n/a if it's below the Policy L stop, or at/above the entry. | Spec §7. |
| Fees per leg | Entry and stops are market (taker + 0.03% slippage); TP1/TP2 are limit (maker, no slippage). | Realistic for how the plan is executed. |
| Break-even win rate | Uses the "all targets" win: 40% at TP1 + 60% at TP2 (runner assumed to exit at TP2; at TP1 if TP2 is dropped). | Reproduces the spec example (−$17.0 → 27%, −$10.4 → 18%). |
| TP2 dropped | If TP2 ≤ TP1 + 1.0% (i.e. ≤ +4%), the 30% goes to the runner (runner 60%). | Spec §7. |
| Coil WATCH window | A watch stays valid for 24h after its conditions **last** held. The box (12 × 1h high/low) is re-measured each time they hold. A Coil ENTRY consumes the watch. | A coil lasting several days should not expire mid-coil. |
| Coil ENTRY timing | Checked only when a 15m bar closes. | The trigger is a 15m close. |
| Cooldown | 2h per symbol across both setups; restored from the DB after a restart. | Spec §6 throttling. |
| Hourly cap | Over the cap, signals are still recorded (and will be paper-traded in M4) but not sent (`hourly_cap`). | Nothing is lost for stats. |
| `/pause` | ENTRY alerts are not sent (recorded as `paused`). `/mute` mutes WATCH alerts only. | The spec lists `/mute` next to "WATCH (can be muted)". |
| Startup | The bar that closed before startup is evaluated for display only (dry run): no alert, no cooldown. | It may be minutes old or already alerted before a restart. |
| Reference entry | Last WS trade if ≤ 20 s old, else the bar close. | Spec: "last price at signal time". |
| TradingView | `WEEX:{symbol}.P`; symbols listed in `var/tv_missing.json` (from `tools/check_tv_symbols.py`) use `BINANCE:{symbol}.P`. | TradingView's search endpoint is unofficial, so it's not used at runtime. |
| Alert status | Recorded as `sent`, `console` (Telegram not configured) or `failed`. | The dashboard must not claim an alert was sent when it wasn't. |

## M3

| Topic | Decision | Why |
|---|---|---|
| Frontend | Vanilla JS single page, no build step; DOM built with `textContent` only. Lightweight Charts 5.2.1 bundled in `web/static/vendor` (Apache-2.0, licence file kept; attribution logo on). | Works offline and inside the future `.exe`; no injection risk from exchange data. |
| Localhost safety | Host-header allowlist (DNS rebinding), same-origin + JSON-only POSTs, same-origin WebSocket, strict CSP, `X-Frame-Options: DENY`. | Even without a login, other websites in your browser cannot drive the dashboard. |
| Login | Only when `bind_host` isn't localhost. Password from `.env`, HMAC-signed HttpOnly SameSite=Strict cookie, 10 failed tries per 15 min per IP. The app refuses to serve if no password is set. | Spec §12. |
| Settings persistence | ruamel.yaml round-trip, so `config.yaml` comments and layout survive. Strings are written quoted. Every save goes into `config_versions`. | A bare `23:30` would be read back by PyYAML as the number 1410. |
| Live candle | The last 15m candle is updated in the browser from the 2 s price push; the chart reloads from closed bars after each scan. | Live look without extra API load; closed bars stay authoritative. |
| Chart levels | Levels within ±15% of price, nearest 12 (6 on phones); levels at the same price are merged into one labelled line. | Readability. |
| Startup | The dashboard starts first and shows "Starting…" while history loads (~30 s). | You see the app immediately. |

## M4

| Topic | Decision | Why |
|---|---|---|
| One exit engine | `exits/engine.py` is a pure, JSON-serialisable state machine. It drives paper trades, the plan "shadow" behind your trades' action alerts, late-logging replay, restart catch-up, and the M5 backtester. | Identical rules everywhere; state survives restarts. |
| Live inputs | Every second: the trade tape's high/low since the last check (so no wick is missed) and the mark price (premiumIndex, now polled every 3 s, weight 1). Chandelier, swing-low trail and EMA exit update on each closed 15m bar. | Timely alerts with little API load. |
| Replay / backtest inputs | Closed 5m bars (mark bars for stop triggers when configured). **Inside a bar the stop is checked before targets** (conservative). A gap through the stop fills at the bar open. | No optimistic fills. |
| Breakeven + fees | Entry × (1 + 2 × taker + slippage). | Reproduces the spec example (142.35 → 142.63). |
| Trail above price | If a 15m close leaves the new trail above the price, the position exits at that close. | No impossible stop above the market. |
| Paper fills | Entry = reference + 0.03% slippage (taker). TPs = limit (maker, no slippage). Stops/market exits = taker + slippage. Funding charged at each real settlement on the quantity still open. | Spec §2 / §8.1. |
| Policy S n/a | No S paper trade is opened when Policy S is n/a for that signal. | Nothing to trade; stats show the count per policy. |
| Your trades vs the plan | The plan shadow uses your entry, initial stop and TPs. Your **logged** stop is watched separately (NEEDS_CONFIRMATION when crossed). Trail alerts compare the plan's stop with your logged stop. | Spec §10.2: "after I log a stop move or exit, alerts use my logged values". |
| Your fees | Per logged leg: market = taker, limit = maker. No slippage on your fills (they're real prices). | You log what you actually got. |
| Partial close % | % of the **original** position (the plan's 40%/30% are of the original). You can enter a quantity instead. | Matches the plan wording. |
| Audit trail | `manual_trade_events` is append-only. Corrections are EDIT rows pointing at their target; withdrawals are VOID rows; the effective state is rebuilt from the whole log each time. | "Nothing is ever deleted" with correct numbers. |
| Late logging window | Replay up to `journal.replay_max_days` (3) back; older entries are recorded with a warning and no replay. | 5m history within reach of one request. |
| Offline crossings | On restart, open trades are replayed over the missed bars. A crossed stop or target flags NEEDS_CONFIRMATION and sends an alert. | Spec §14. |
| Plan adherence | Your confirmed net minus the plan's P&L on your entry and size (same funding). If the plan was still open when you closed, its remainder is valued at your final exit price. | Spec §8.3. |
| Taken vs not taken | A signal counts as "taken" if any of your trades links to it (open or closed). | Spec §8.3. |
| Bedtime window | Reminders run from `bedtime_reminder` until `notifications.bedtime_end` (06:00 London), every 30 min while logged trades are open. `/snooze` covers the current night (or tonight, if sent earlier in the evening). | Spec §10.3. |
| Backups | SQLite online-backup API (safe while running), one file per London day, last 30 kept. Only backup copies are ever removed. | Spec §8.4. |

## M5

| Topic | Decision | Why |
|---|---|---|
| Same code | The backtester calls the live `compute_features`, `compute_regime`, `SignalEngine` (setups, score, plan) and exit engine. Only data access differs. | Spec §9. |
| Window parity | At each simulated close T, every series is cut at T (`upto`) and trimmed to the live ring-buffer length (`bars.keep`); history is downloaded with that warm-up. | Indicators equal what the live engine would have computed. |
| No lookahead | Unit test: running on data physically cut at T reproduces every signal and every trade closed by T from the full-data run. | Spec §9. |
| OI | `backtest.oi_mode: auto`: our own OI snapshots are used if they cover ≥ 80% of the period; otherwise the "oi" condition group and score component are disabled, and the report says so prominently. | WEEX has no OI history (M0). |
| CVD | Enabled (kline taker volume, corrected field). | Verified equal to the live trade tape. |
| Funding feature | Last *settled* rate at T, normalised with the interval between the last two settlements. | The live forecast has no history; using the next settled rate would be lookahead. |
| Universe | Rebuilt every 30 simulated minutes: 24h volume, ATR(1h)%, wick risk, listing age, stablecoins/majors, top N by volume. Spread/depth not applied. Only currently listed symbols. | Those need order-book history; stated in the report. |
| Fills | Reference = signal bar close (+ slippage). Stops on last-price 5m bars (no mark-bar download). | Mark history would double the download; stated in the report. |
| Telegram gating | RISK_OFF / blackout / 6-per-hour suppression applied as live (no `/pause`); every signal is simulated. | The "alert sent vs suppressed" breakdown matches live. |
| Speed | 15m/1h indicators memoised by (symbol, tf, last bar, length) - pure, verified transparent by test. EMA loops over plain floats; swing highs and volume profile vectorised (tested equal to the reference loops). ~1.8 ms per symbol-bar. | A 30-day, 40-symbol run takes minutes, not hours. |
| Rate limit | Backtests use at most 250 weight / 10 s. | Leaves room for a live scanner on the same IP (WEEX: 500). |
| `--set` / `--tag` | Override any existing config key for one run (`--set ignition.min_headroom_pct=4`); the report lists the overrides. | Compare settings without editing config.yaml. |

## M6

| Topic | Decision | Why |
|---|---|---|
| App home | `config.yaml`, `.env` and `var/` are read from: `IC_HOME` if set, else the folder of the packaged exe, else the project folder. | The exe unpacks code to a temp folder; data must live next to it. |
| Env overrides | `IC_OVERRIDES="section.key=value;..."` (same parser as `--set`). | Docker/VPS settings without a second config file. |
| Exe | PyInstaller **one-folder** build (`dist\MomentumScanner\`), console kept, dashboard assets + tzdata bundled, test/analysis libs excluded. Verified: `--once` scan and the dashboard (all pages and the chart library) served from the exe. | Faster start and fewer antivirus false positives than one-file; the console shows the live table. |
| run.bat | Creates `.venv` + installs on first run, creates `.env`, restarts the app after a crash (30 s). Works from a folder whose name contains `&`. | One double-click for daily use. |
| Docker | `python:3.12-slim`, non-root user; compose binds the dashboard to 0.0.0.0 (so login is enforced), secure cookies, base_url = https://DOMAIN; Caddy terminates HTTPS with automatic certificates; port 8000 is never published. Verified the override → login path without Docker; the containers themselves were not run (Docker not installed on the dev PC). | Spec §13. |
| Pine | v6, 5-minute chart, `request.security` for 15m/1h values (update on bar close, like the app), markers on confirmed bars only; header lists what is not replicated (OI, CVD/taker, funding, headroom, score, universe, regime). Not compiled in TradingView from here (needs a TradingView login). | Spec §11. |
| 15m history | Backtests build 15m bars from 5m (verified identical to WEEX's 15m klines on 8,977 bars × 4 coins). | Cuts the download by about a quarter. |
| Rate limiting | The REST client folds the server's `x-used-weight-10s` into its own budget (other processes on the IP count) and backs off a full window on 429. | A backtest running next to the live scanner hit 429s before this. |
| Missing taker data | Empty taker fields in old WEEX klines are kept as unknown (NaN) and reported in backtests; order-flow conditions then fail rather than read fake data. | Found in the one-year download. |
| VPS (2026-09-29) | Hostinger **Docker Manager → Compose from URL** with the GitHub repo link. Hostinger's API docs: a GitHub repo URL "will be automatically resolved to docker-compose.yaml file in master branch" - only that file is fetched. So `docker-compose.yaml` is self-contained: it pulls a prebuilt image `ghcr.io/sudeep-sasikumar/ignitionandcoil:latest` (GitHub Actions builds it on every push to master; `pull_policy: always`). A first attempt that built from the repo's git URL on the VPS failed in Docker Manager ("docker container not found"). Deploy with the raw URL of docker-compose.yaml. Secrets/DOMAIN as `${VAR:-}` entered in Docker Manager (no `:?`, so validation can't fail on them; the app itself refuses to start without DASHBOARD_PASSWORD), named volume for `var/` (owned by the container user, fixing the root-owned bind-mount problem), HTTPS via the VPS's existing Traefik (Docker Manager's: host network, entrypoints web/websecure, resolver `letsencrypt`) using container labels - Caddy was dropped because Traefik already holds ports 80/443; DOMAIN defaults to the VPS hostname srv1479428.hstgr.cloud; the container logs which secrets arrived (set/MISSING, never values); log rotation 10 MB × 3. `deploy/docker_start.py`: the live config.yaml lives in the volume; when the repo's config.yaml changes it is taken over, keeping the Settings-page fields. DOMAIN defaults to the VPS hostname (srvNNN.hstgr.cloud) - no domain purchase. Starts with a fresh DB (the PC's had no trades, only ~2 days of OI snapshots). Replaced the earlier bundle/tunnel/setup-script approach. | The user wants to deploy from a repo link with only secrets to enter. |

## Threshold changes (your decisions)

| Date | Change | Why |
|---|---|---|
| 2026-09-28 (later) | **Model switched to 20x · Policy S.** `trade.leverage` 50 → **20**, `trade.margin_usd` 20 → **50** (same $1,000 position); new `trade.default_stop_policy: S` and `trade.require_default_policy: true` (no Telegram ENTRY when the structural stop doesn't fit; still recorded + paper-traded; reason `default_policy_na`). Thresholds from the latest walk-forward window for this model: `ignition.min_rvol_5m` **3.0**, `ignition.min_rvol_15m` **1.2**, `ignition.min_ret_1h` 1.0, `ignition.max_ret_1h` **8.0**, `coil.min_rvol_15m` **2.5**, `score.min_entry` **55**. This supersedes the 90-day sweet spot below. | One-year walk-forward study (`python -m backtest.study --days 365`, 8 models = 50/25/20/10x × Policy L/S, 3 × 60-day test windows, 435 candidates). Out-of-sample: **20x-S +0.23R/trade over 98 trades (+22.3R, PF 1.65, bootstrap P(edge ≤ 0) ≈ 5%)**, 10x-S essentially identical (+0.24R) but needs twice the margin; the previous 50x-L model +0.10R (P ≈ 20%); 50x-S worst (−0.08R: the structural stop rarely fits inside 50x liquidation). Test windows: +14.5R, −3.8R, +11.5R. At 20x, liquidation is ~4.8% away, so the structural stop fits on almost every signal. Ignition RVOL 15m 1.2 is the loosest grid value (chosen in all 3 windows) - worth widening the grid next time. |
| 2026-09-28 | `ignition.min_rvol_5m` 3.0 → **1.5**; `ignition.max_ret_1h` 5.0 → **6.5**; `coil.min_rvol_15m` 2.5 → **1.5**; `score.min_entry` 70 → **50**. Unchanged: `ignition.min_rvol_15m` 2.0, `ignition.min_ret_1h` 1.0. | Request: more signals. 90-day sweep (`python -m backtest.sweep`, 1,620 combinations, 40 coins, OI conditions off): picked on the first 60 days with a neighbourhood-smoothed objective (≥ 2× signals, PF ≥ 1), then checked on the last 30 days it never saw. Where the tuning period couldn't tell settings apart, the smaller change was kept. Exact 90-day confirmation: **24 → 67 signals; Policy L −0.09R → +0.17R (−$37 → +$176); Policy S −0.37R → +0.32R (7 → 30 trades)**. RVOL 15m 1.5 was rejected: it added trades that lost out-of-sample. Caveats: ~67 trades is still a small sample (± ~0.15R); live also requires the OI conditions, so it fires less often than the backtest. |
| 2026-09-27 | `ignition.min_headroom_pct` and `coil.min_headroom_pct`: 4.0 → **3.0** | The 30-day backtest showed headroom was the most frequent sole blocker (75 Ignition near-misses; it passed only 5% of evaluations). 3.0% = TP1, so the first target still sits at or before the nearest resistance. Between 3% and ~4.2% of headroom, TP2 is dropped by the existing rule and the runner takes 60%. Compared on 60 days against 4.0 (reports tagged `headroom3` / `headroom4`). |
