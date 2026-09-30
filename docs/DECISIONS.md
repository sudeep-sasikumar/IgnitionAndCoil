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

## Short side (2026-09-30, backtest only)

Your spec says "Long-only for now". You chose: build shorts **backtest-first** (live stays long-only until they show an edge), mirror **both** setups, and test **both** regime rules.

| Topic | Decision | Why |
|---|---|---|
| Switch | `short.enabled: false` in config.yaml. Off = the signal engine, levels and score behave exactly as before (a 90-day replay of the same window reproduced the long results). The live scanner never sees `true` until you decide. | Live stays long-only. |
| Setups | `IGNITION_SHORT`: close < 12h low, rvol as long, close < VWAP24h, EMA20 < EMA50 (15m), −8% ≤ ret_1h ≤ −1%, ret_24h ≥ −25%, rs_1h ≤ −1%, OI up ≥ 2% (OI rises on new shorts too), taker SELL ratio ≥ 0.55, CVD slope < 0, funding ≥ −0.03% (very negative = crowded shorts), room down to support ≥ 3%, close in the bottom 30% of the candle, candle ≤ 2.5 ATR. `COIL_SHORT`: squeeze with 1h close < EMA50 and EMA50 falling, OI up, funding ≥ −0.01%; entry on a 15m close below the coil low. Same numbers as the long thresholds (no separate short tuning). | Exact mirror, so the only new variable is direction. |
| Stops / targets | Policy S: breakdown candle high + 0.1 ATR15 (Ignition), coil low + 0.25 ATR15 (Coil). TP1 −3%, TP2 = max(−6%, support + 0.2%). Supports: confirmed swing lows (1h 14d, 4h 60d), 7d/30d lows, the same volume nodes. | Mirror of resistance/headroom. |
| Score | Same weights; RS, flow (sell ratio, falling CVD), funding sign and the regime score mirrored; the wick penalty counts **upper** wicks (squeeze risk). A short scores exactly what the long mirror image scores (test). | |
| Liquidation | `L = (Q·E + M + cum) / (Q·(1 + MMR))` - the same balance equation as the calibrated long formula, solved for a short. **Not yet checked against a real WEEX short.** | No short screenshot yet; ask before going live. |
| Exits | The exit engine is unchanged: a short runs as a long on the negated price (every exit rule is linear in price, ATR is sign-free, EMA(−P) = −EMA(P)). Breakeven and the time-stop level are built in the short's direction. A test shows a short on a mirrored path produces exactly the long's events. | One exit implementation, no second copy to drift. |
| Costs | Slippage against you both ways (sell lower, buy back higher); fees as for longs; a short **receives** positive funding. | |
| Regime gate | Longs: unchanged (held back in RISK_OFF). Shorts: `short.regime_rule` = `risk_off` (only RISK_OFF) or `not_risk_on` (RISK_OFF or NEUTRAL). Separate cooldown and Coil watches per side. | Your "test both". |
| Universe | Unchanged, including the lower-wick filter (a short-specific wick filter would change the long universe). | |
| Backtest tooling | `--end 'YYYY-MM-DD HH:MM'` replays an earlier window exactly; cache files are written atomically so backtests can run side by side. | |

**Result (2026-09-30), 20x · Policy S, $1,000, $5M+ coins, alerts that would have been sent:**

| Period | Longs | Shorts, RISK_OFF only | Shorts, RISK_OFF + NEUTRAL |
|---|---|---|---|
| 90 days (54 coins) | 65 trades, −0.02R, +$33 | 17 trades, +0.06R, −$2 | 24 trades, −0.03R, −$32 |
| 365 days (top 40 coins) | 111 trades, +0.13R, +$228 | 77 trades, −0.17R, −$198 | 89 trades, −0.18R, −$244 |

Shorts lose under both regime rules (Ignition short −0.24R over 74 trades; Coil short +0.09R over 17). **Kept off.** Side finding: longs held back in RISK_OFF made +0.46R over 16 trades in the year (+$176) - the RISK_OFF block cost money in both windows; small sample, not changed.

## Highs tab (2026-09-30)

| Topic | Decision | Why |
|---|---|---|
| Data (live) | CoinGecko free public API (keyless; optional free Demo key `COINGECKO_DEMO_API_KEY`, header `x-cg-demo-api-key`). `/coins/markets` (250/page, 8 pages for 2,000) every 10 min; `/coins/{id}/ohlc?days=365` (4-day candles) once per coin for the 52-week high. Endpoints checked in the docs and live. Paced to `highs.calls_per_min` (5; keyless allowed ~4 quick calls before a 429 in testing), Retry-After honoured. | You asked for CoinGecko top 2,000; free, no paid plan. |
| ATH | CoinGecko's own `ath` / `ath_date` (full history). Break = a higher ATH than last seen. | CoinGecko computes it from full history, which the free API can't give us. |
| 52-week high | Highest high of the last 365 days from the OHLC candles plus our own daily observations (max of price and `high_24h`). Break = price or 24h high above it. Needs 300 days of history (younger coins: ATH only). | Candle highs, not daily snapshots, so wicks count as the high. |
| Fresh-break rule | The old high must be ≥ 7 days old (`min_high_age_days`); an ATH break is reported only as ATH. First sighting of a coin never alerts. | One alert per breakout, not one per scan while it trends. |
| Exclusions | Stablecoins (the universe list) and names containing wrapped / bridged / staked / restaked / liquid staking / tokenized. | Copies of other coins. |
| Alerts | One Telegram message per scan with all new breaks (split after 20 lines), deduplicated per event, outside the ENTRY hourly cap. `alert_min_volume_usd: 0` (all coins) - raise it to skip illiquid ones. | Top 2,000 includes very thin coins; left to you. |
| Study | `python -m highs.study`: Binance spot daily candles (free public market data, `data-api.binance.vision`), because CoinGecko's free API only serves 365 days (error 10012 beyond). Close-based breaks, no lookahead, same 7-day rule; ATH events only for coins whose Binance history holds their real ATH (CoinGecko ATH within 10%). A snapshot ships in `highs/study_snapshot.json` for machines that haven't run it. | Needs years of history before and after each break. |

**Study result (2026-09-30, 327 coins, 142 ATH + 341 52W breaks since 2017):** after 30 days the median ATH break was +1.1% (vs −4.8% for any day, same coins) and the median 52W break −5.8%; averages are far higher (+39.8% / +6.5%) because a few coins ran hundreds of percent, mostly in 2021. Breaks worked in bull years (2021) and failed otherwise (2024–2026 medians −10% to −22% at 30 days); top-100 coins did better than small caps; breaks while BTC was below its 200-day average did badly. About 40% closed back below the old high within 3 days.

## RISK_OFF longs test (2026-09-30)

`signals.suppress_longs_in_risk_off` (default **true** = unchanged). 20x · Policy S, $1,000, same 40 coins; OI conditions off (no history); the older year also has the funding filter off (WEEX serves ~1 year of funding) - the recent year replayed without it changed little (+0.13R → +0.11R), so the comparison holds.

| Year | Held back (now) | Allowed | RISK_OFF longs alone |
|---|---|---|---|
| Oct 2025 – Sep 2026 (found here) | 111 trades, +0.13R, +$228 | 127 trades, +0.17R, +$404 | 16 trades, +0.46R |
| Oct 2024 – Sep 2025 (out of sample) | 207 trades, −0.12R, −$263 | 219 trades, −0.09R, −$204 | 12 trades, +0.43R |

Both years: RISK_OFF longs +0.45R over 28 trades (bootstrap P(edge ≤ 0) ≈ 4%). **The whole model lost money in the older year** (−0.12R over 207 trades) - its edge depends on the period.

## Filter combinations (2026-09-30)

`python -m backtest.ablation harvest|search|report`: one replay per year through the live code records every bar where Ignition's trigger holds (close > 12h high) with each other condition's pass/fail, the score and the simulated trade; then every on/off combination is replayed with the live rules (cooldown set even for suppressed alerts, Ignition before Coil, Coil WATCH expiry/consumption, hourly cap, Policy S must fit). Checked: the current system reproduces the full backtests exactly (one same-bar/same-score tie under the hourly cap is broken by symbol instead of volume: same R, $2 different). 8,192 Ignition combinations × score 0/40/55/70, 128 Coil combinations × 4, both years (40 coins; OI untestable; funding only in the recent year). Every condition now has a key (e.g. `ignition.taker`) usable in `signals.disabled_conditions`; `coil.enabled` switches Coil off.

| System (20x · Policy S, $1,000, alerts sent) | Oct 2024 – Sep 2025 | Oct 2025 – Sep 2026 |
|---|---|---|
| Current (all filters, Coil on) | 219 trades, −18.7R, −$204, PF 0.90, max DD 29.5R | 127 trades, +21.5R, +$404, PF 1.36, DD 12.8R |
| **Best: Coil off; Ignition without rvol_15m, ema, ret_1h, ret_24h, taker, candle_size** | 345 trades, **+11.2R, +$701**, PF 1.23, DD 14.0R | 242 trades, **+55.1R, +$1,200**, PF 1.57, DD 13.9R |

Evidence it is not luck: removing them one at a time improves both years at every step (taker, ret_24h, candle_size, ret_1h, rvol_15m, ema); single-condition neighbours of the pick stay positive in both years (only removing rvol_5m or headroom breaks it); picked on either year alone, the choice is top-1% on the other and beats the current filters there; losing months 17 → 11 of 24, monthly paired bootstrap P(no improvement) ≈ 0.5%. No Coil combination makes money in the recent year. Kept: rvol_5m, vwap, rs_1h, cvd, funding, headroom, close_pos, score ≥ 55. The pick used both years, so real results should be expected to be lower.

## Threshold changes (your decisions)

| Date | Change | Why |
|---|---|---|
| 2026-09-30 (later) | **Best system from the filter study:** `coil.enabled` → false; `signals.disabled_conditions` → [ignition.rvol_15m, ignition.ema, ignition.ret_1h, ignition.ret_24h, ignition.taker, ignition.candle_size] | Your decision. Both test years: current −$204 / +$404 → new +$701 / +$1,200 (see "Filter combinations"). |
| 2026-09-30 | `signals.suppress_longs_in_risk_off` true → **false**: long ENTRY alerts are sent in RISK_OFF too | Your decision after the two-year test (RISK_OFF longs +0.45R over 28 trades; allowing them improved both years). |
| 2026-09-28 (later) | **Model switched to 20x · Policy S.** `trade.leverage` 50 → **20**, `trade.margin_usd` 20 → **50** (same $1,000 position); new `trade.default_stop_policy: S` and `trade.require_default_policy: true` (no Telegram ENTRY when the structural stop doesn't fit; still recorded + paper-traded; reason `default_policy_na`). Thresholds from the latest walk-forward window for this model: `ignition.min_rvol_5m` **3.0**, `ignition.min_rvol_15m` **1.2**, `ignition.min_ret_1h` 1.0, `ignition.max_ret_1h` **8.0**, `coil.min_rvol_15m` **2.5**, `score.min_entry` **55**. This supersedes the 90-day sweet spot below. | One-year walk-forward study (`python -m backtest.study --days 365`, 8 models = 50/25/20/10x × Policy L/S, 3 × 60-day test windows, 435 candidates). Out-of-sample: **20x-S +0.23R/trade over 98 trades (+22.3R, PF 1.65, bootstrap P(edge ≤ 0) ≈ 5%)**, 10x-S essentially identical (+0.24R) but needs twice the margin; the previous 50x-L model +0.10R (P ≈ 20%); 50x-S worst (−0.08R: the structural stop rarely fits inside 50x liquidation). Test windows: +14.5R, −3.8R, +11.5R. At 20x, liquidation is ~4.8% away, so the structural stop fits on almost every signal. Ignition RVOL 15m 1.2 is the loosest grid value (chosen in all 3 windows) - worth widening the grid next time. |
| 2026-09-28 | `ignition.min_rvol_5m` 3.0 → **1.5**; `ignition.max_ret_1h` 5.0 → **6.5**; `coil.min_rvol_15m` 2.5 → **1.5**; `score.min_entry` 70 → **50**. Unchanged: `ignition.min_rvol_15m` 2.0, `ignition.min_ret_1h` 1.0. | Request: more signals. 90-day sweep (`python -m backtest.sweep`, 1,620 combinations, 40 coins, OI conditions off): picked on the first 60 days with a neighbourhood-smoothed objective (≥ 2× signals, PF ≥ 1), then checked on the last 30 days it never saw. Where the tuning period couldn't tell settings apart, the smaller change was kept. Exact 90-day confirmation: **24 → 67 signals; Policy L −0.09R → +0.17R (−$37 → +$176); Policy S −0.37R → +0.32R (7 → 30 trades)**. RVOL 15m 1.5 was rejected: it added trades that lost out-of-sample. Caveats: ~67 trades is still a small sample (± ~0.15R); live also requires the OI conditions, so it fires less often than the backtest. |
| 2026-09-27 | `ignition.min_headroom_pct` and `coil.min_headroom_pct`: 4.0 → **3.0** | The 30-day backtest showed headroom was the most frequent sole blocker (75 Ignition near-misses; it passed only 5% of evaluations). 3.0% = TP1, so the first target still sits at or before the nearest resistance. Between 3% and ~4.2% of headroom, TP2 is dropped by the existing rule and the runner takes 60%. Compared on 60 days against 4.0 (reports tagged `headroom3` / `headroom4`). |
