# M0 — WEEX Public-Data Capability Report

Date: 2026-09-27. Sources: the official WEEX Futures API docs (`weex.com/api-doc/contract/...`), WEEX Help Centre / Futures Terms of Use, ccxt source, and **live probes of the public endpoints from this PC** (no API keys). Anything marked *verified live* was observed in real responses, not just read in the docs.

Base REST: `https://api-contract.weex.com` (API **V3**, `/capi/v3/market/...`).
Public WS: `wss://ws-contract.weex.com/v3/ws/public`.
**V2 is sunset on 30 Sep 2026**, so the app uses V3 only.

## Capability table

| # | Capability | Available? | Details (endpoint · weight) | Gap / fallback |
|---|---|---|---|---|
| 1 | **Kline history depth** | ✅ Deep | `GET /capi/v3/market/klines` (w1): up to **1000** most recent bars; intervals 1m,5m,15m,30m,1h,4h,12h,1d,1w. `GET /capi/v3/market/historyKlines` (w5): **100 bars/request, window ≤ 90 days per request, `startTime`+`endTime` both required**, paged backwards. *Verified live*: 5m, 1h and 4h bars exist at least **540 days** back (SOLUSDT). `priceType` LAST / INDEX / MARK. | Bars come back **newest-first** (with the forming bar included) → sort, de-dupe, and drop any bar whose close time is after server time. |
| 2 | **Open interest, current** | ✅ | `GET /capi/v3/market/openInterest?symbol=` (w2). Returns `openInterest` + engine `time`. | The unit is unclear: docs say "contracts", but live values don't fit either contracts or base cleanly (BTC 139,060; SOL 171.6M). **Harmless**: the spec only uses % changes. The dashboard will show OI Δ% only. |
| 2b | **Open interest, historical** | ❌ | No endpoint. ccxt `fetchOpenInterestHistory` is also unsupported. | As the spec already plans: poll every 60s → `oi_snapshots`. 80 symbols × w2 = 160 weight/min (budget is ~3,000/min). **Backtests run with OI filters disabled** until enough of our own snapshots exist, and the report says so. |
| 3 | **Funding, current** | ✅ | `GET /capi/v3/market/premiumIndex` (w1, all symbols in one call): `markPrice`, `indexPrice`, `lastFundingRate`, `forecastFundingRate`, `nextFundingTime`, `collectCycle`. | ⚠ **Funding interval varies by symbol**. Live: 504 symbols at 8h, 493 at 4h, 3 at 1h. The code will normalise to 8h-equivalent (`rate × 480 / collectCycle`) and charge funding at each symbol's real settlement times. |
| 3b | **Funding, history** | ✅ | `GET /capi/v3/market/fundingRate` (w5): ≤1000 rows, window ≤ 7 days per request, **start must be within the last 365 days**. | Enough for backtests of up to a year. |
| 4 | **Trades feed with taker side, REST** | ✅ | `GET /capi/v3/market/trades` (w5), ≤1000 recent trades (≈3.5 min for SOL). Field `isBuyerMaker`. No historical-trades endpoint. | Used only for gap-filling after reconnects. |
| 4b | **Trades feed with taker side, WS** | ✅ *verified live* | `<SYMBOL>@trade`, field `m`. **Verified:** `m=true` = aggressive **sell**, `m=false` = aggressive **buy**. 706 of 747 trades matched the live book, and the rest were book-timing noise. | The docs describe `m` ambiguously. The live test decides it. |
| 4c | **CVD history (for backtests)** | ✅ via klines, *with a caveat* | Klines carry a "taker buy volume" field (REST index 9/10, WS `V`/`Q`). | ⚠ **Mislabelled.** Live check against the trade tape: kline `V` equals **taker-SELL** volume exactly (e.g. SOL 1m: v 1845.7, V 501.3, trade-tape taker-buy 1344.4 = v − V). Plan: **taker_buy = volume − V**. The live engine uses the WS trade tape as primary and runs a startup self-check that compares the two. If WEEX ever "fixes" the field, the check catches it and raises an incident. **Upside: CVD/taker-ratio history is available for backtests** from klines, so the CVD filters don't need to be disabled. |
| 5 | **Order-book depth** | ✅ | REST `GET /capi/v3/market/depth` (w1), `limit` 15 or 200. WS `<SYMBOL>@depth15` / `@depth200`: snapshot, then incremental with `U`/`u` sequence IDs. | For the 0.5%-depth filter, a REST `limit=200` pull per symbol every 30 min is enough. No WS depth is needed. |
| 6 | **Maintenance-margin tiers** | ✅ *verified live* | `GET /capi/v3/market/riskLimits` (w1, all symbols in one call, added 2026-09-25): `bracket`, `notionalFloor`, `notionalCap`, `initialLeverage`, `maintMarginRatio`. SOL bracket 1: 0–$2M, 300x, MMR 0.2%. | There's **no cumulative "maintAmount" field**, so the code computes it from the brackets. At $1,000 notional we're always in bracket 1. |
| 6b | **Liquidation fee** | ⚠ Not published | Terms of Use: *"a liquidation clearance fee (such amount to be determined by WEEX)"*. No rate is given anywhere public. WEEX's help-centre formula includes an estimated closing fee at the **taker rate**. | **Calibrated (see below).** WEEX's displayed liq price **excludes** any closing/liquidation fee. The fee only affects P&L if liquidated. On isolated margin that loss is capped at the whole margin, and paper trades will book it as −margin − fees. |
| 7 | **Liquidation uses mark price?** | ✅ Yes | Futures Terms of Use: *"the Mark Price … hits the Liquidation Price"* triggers liquidation. | Default `stop_trigger_price: mark` for liquidation; configurable for stops. |
| 8 | **Mark price availability** | ✅ | `premiumIndex` (all symbols, w1), `markPriceKlines` (w1, ≤1000), `historyKlines?priceType=MARK`, WS ticker field `m`, WS `@kline_<i>_MARK_PRICE`. | Mark-price history is available too, so backtests can trigger stops on mark. |
| 9 | **Min order / contract sizes** | ✅ | `GET /capi/v3/market/exchangeInfo` (w1): `contractVal`, `minOrderSize`, `maxOrderSize`, `pricePrecision`, `quantityPrecision`, `minLeverage`, `maxLeverage`, `makerFeeRate`/`takerFeeRate` (0.0002/0.0008 confirmed), `contractType`. | **No `status` and no listing date.** Listing age = earliest `1d` kline (`/klines?interval=1d&limit=1000`). Delisting = the symbol disappears from `exchangeInfo`. |
| 10 | **Public rate limits** | ✅ *verified live* | **500 weight per rolling 10 s per IP** (the `x-used-weight-10s` header reset after 10 s idle). `exchangeInfo` reports it confusingly as "MINUTE × 10". WS: ≤20 connections/IP, 300 connects/IP/5 min, **≤100 channels per connection, 240 sub operations/hour/connection**. The server sends `{"event":"ping"}`, and we must reply `{"method":"PONG","id":1}`. | 80 symbols × (trade + kline_5m) = 160 channels → 2–3 WS connections. 15m/1h/4h bars are built locally from 5m and re-synced from REST each hour. Watch out for `ticker/24hr` without a symbol: weight **40**. |
| 11 | **Server time** | ✅ | `GET /capi/v3/market/time` (w1) → `serverTime` ms. | For clock-drift checks. |
| 12 | **WS closed-candle flag** | ❌ | The kline WS payload has **no "closed" flag**. | A bar is treated as closed when a newer bar's open time arrives or the bar's close time passes the server-synced clock. It is then **confirmed against REST** before signals run (no-lookahead safe). |
| 13 | **`ccxt` support** | ✅ Partial | ccxt `weex` uses V3 and supports OHLCV, OI (current), funding rate + history, trades, order book, mark price, time, and `watchTrades`/`watchOHLCV`. Not supported: OI history, leverage tiers. Not "certified". | **Recommendation: call the REST/WS endpoints directly** with `httpx` + `websockets`. It's fewer layers, and we need raw fields ccxt doesn't expose (the taker-volume quirk, `collectCycle`, `riskLimits`). ccxt isn't needed. |
| 13b | **`weex-sdk` (PyPI)** | ❌ Don't use | v1.0.10, all releases on 22–23 Dec 2025, none since. That predates V3 (Mar 2026), so it targets the V2 API, which is being sunset. | Fails the spec's "maintained and matches the docs" rule. |
| 14 | **WEEX perps on TradingView** | ✅ Mostly | Format **`WEEX:<SYMBOL>.P`**, e.g. `WEEX:SOLUSDT.P`, `WEEX:WIFUSDT.P`, `WEEX:TAOUSDT.P` (found in TradingView's symbol search). Coverage is **not complete**: `BPUSDT` (listed 21 Sep) and 1000PEPE had no WEEX swap on TradingView. | `tv_symbol_template: "WEEX:{symbol}.P"`, fallback `"BINANCE:{symbol}.P"`, plus a per-symbol override map in config. The coverage check uses TradingView's *unofficial* symbol-search endpoint, so I'd only use it in an optional `tools/check_tv_symbols.py`, not at runtime. |

## Universe notes

- `exchangeInfo` returns 1,000 symbols: **576 `PERPETUAL`** (crypto) and **424 `TRADIFI_PERPETUAL`** (stocks/commodities/FX). The universe uses `contractType=PERPETUAL`, `quoteAsset=USDT` only, minus stablecoin bases.
- The 24h quote volume for all symbols in one call (`ticker/24hr`, w40) is fine every 30 min.

## Other things you need to know

1. **Python isn't installed on this PC.** Only the Microsoft Store stub exists. M1 needs **Python 3.12** from python.org (tick "Add to PATH"). The README will cover it.
2. **Access from your location works.** All public endpoints responded from this PC.
3. Fees in `exchangeInfo` match the spec defaults (0.02% maker / 0.08% taker). Your personal tier still goes in config.

## Liquidation-price calibration (from a real WEEX position)

Screenshot from the user, 2026-09-27: INXUSDT long, isolated, 3x. Position value 298.0416 USDT at mark 0.006536 → qty 45,600 INX (456 contracts × `contractVal` 100). Entry 0.006566, margin 99.8076, **WEEX liq price 0.004421**. INX bracket 1 has MMR = 0.01.

| Model | Result | Match? |
|---|---|---|
| **A: margin + Q·(L − E) = MMR · Q · L** → `L = (Q·E − M) / (Q·(1 − MMR))` | **0.0044215** | ✅ within rounding, matches the displayed 0.004421 |
| B: A plus the taker closing fee at L | 0.0044250 | ❌ 4 ticks off |
| C: maintenance margin on entry value | 0.0044429 | ❌ |

**Adopted: model A**, with a cumulative maintenance amount for brackets above 1: `L = (Q·E − M − cum) / (Q·(1 − MMR))`. The margin M is the actual position margin. WEEX's margin was 99.8076 vs 99.8032 = notional/3, which implies a true average entry of ≈0.0065663, shown rounded. This case becomes a unit test.

At the default $20 × 50x, this means: SOL (MMR 0.2%) liq ≈ −1.80%; INX-type coins (MMR 1%) liq ≈ **−1.01%**. On high-MMR coins, the Policy L stop at −0.15% before liq is therefore very tight. That is exactly why Policy S vs L is being compared.

## Decisions (answered 2026-09-27)

1. Liquidation: model A, calibrated above. ✅
2. TradFi perps: **excluded**. ✅
3. TradingView: `WEEX:{symbol}.P`, fallback `BINANCE:{symbol}.P`. ✅
4. Funding: filters use the 8h-equivalent; P&L uses real per-symbol settlements. ✅
5. Direct REST/WS (`httpx` + `websockets`), no ccxt. ✅
