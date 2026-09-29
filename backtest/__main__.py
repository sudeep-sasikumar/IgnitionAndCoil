"""Run a backtest and write an HTML report.

    .venv\\Scripts\\python.exe -m backtest                 # last 30 days (config backtest.days)
    .venv\\Scripts\\python.exe -m backtest --days 14 --open
    .venv\\Scripts\\python.exe -m backtest --symbols SOLUSDT,WIFUSDT --days 60

The first run downloads history (a few minutes per 10 symbols for 30 days); later runs reuse
the cache in var/bt_cache and only fetch what's new.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import html
import sys
import time
import webbrowser
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.data import HistoryCache, aggregate, warmup_ms  # noqa: E402
from backtest.engine import Backtester, BTInputs  # noqa: E402
from backtest.report import build  # noqa: E402
from core.clock import TF_MS, Clock, floor_tf  # noqa: E402
from core.config import Config, apply_overrides, load_config  # noqa: E402
from core.logs import setup_logging  # noqa: E402
from data.db import Database  # noqa: E402
from data.universe import base_filters  # noqa: E402
from exchange.weex_rest import WeexRest  # noqa: E402
from plan.liquidation import parse_risk_limits  # noqa: E402

DAY = 86_400_000


def _say(msg: str) -> None:
    print(msg, flush=True)


async def pick_symbols(cfg, rest, explicit: list[str], n: int) -> tuple[list[str], dict]:
    info = await rest.exchange_info()
    meta = {s["symbol"]: s for s in info["symbols"]}
    if explicit:
        bad = [s for s in explicit if s not in meta]
        if bad:
            raise SystemExit(f"not listed on WEEX: {', '.join(bad)}")
        syms = explicit
    else:
        vols = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in await rest.ticker_24h_all()}
        cands, _ = base_filters(info["symbols"], vols, cfg)
        majors = set(cfg.universe.majors)
        syms = [c.symbol for c in cands if c.symbol not in majors][:n]
    return [cfg.universe.regime_reference] + [s for s in syms if s != cfg.universe.regime_reference], meta


async def prepare(cfg, rest, db, clock, days: int | None, symbols: str | None, max_symbols: int | None,
                  overrides: list[str], concurrency: int = 6) -> dict:
    """Download/cache history and build the backtest inputs + the report's data notes."""
    server, b, af = await rest.server_time()
    clock.set_offset(server, b, af)
    now = clock.now_ms()
    days = days or cfg.backtest.days
    end = floor_tf(now, "5m")
    start = end - days * DAY
    explicit = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()] or list(cfg.backtest.symbols or [])
    syms, meta = await pick_symbols(cfg, rest, explicit, max_symbols or cfg.backtest.max_symbols)
    _say(f"Backtest {days} days, {len(syms) - 1} symbols + {syms[0]} (regime). Downloading history "
         f"(cached in {cfg.data_dir / cfg.backtest.cache_dir})...")

    cache = HistoryCache(cfg, rest, now)
    inp = BTInputs(bars={}, funding={})
    inp.brackets = parse_risk_limits(await rest.risk_limits_all())
    t0 = time.time()
    done = 0
    sem = asyncio.Semaphore(max(1, int(concurrency)))

    async def load(sym: str) -> None:
        """One symbol's history. Several run at once: downloads are latency-bound, and the shared
        rate limiter still caps the total API weight."""
        nonlocal done
        async with sem:
            bars = {}
            # 5m covers the 15m warm-up too; 15m is built from 5m (identical to WEEX's 15m - see aggregate())
            w5 = max(warmup_ms(cfg, "5m"), warmup_ms(cfg, "15m"))
            b5 = await cache.bars(sym, "5m", start - w5, end)
            bars["5m"] = b5
            b15 = aggregate(b5, "15m")
            bars["15m"] = b15.upto_tail(end + TF_MS["5m"], len(b15))   # closed 15m bars only
            for tf in ("1h", "4h"):
                bars[tf] = await cache.bars(sym, tf, start - warmup_ms(cfg, tf), end)
            inp.bars[sym] = bars
            inp.funding[sym] = await cache.funding(sym, start - DAY, end)
            m = meta.get(sym) or {}
            inp.precision[sym] = int(m["pricePrecision"]) if m.get("pricePrecision") is not None else None
            first = db.first_bar_ts(sym)
            if first is None:
                daily = await rest.klines(sym, "1d", 1000)
                if daily:
                    first = daily[0].t
                    if len(daily) >= 30:
                        db.set_first_bar_ts(sym, first)
            if first is not None:
                inp.first_bar[sym] = first
            done += 1
            el = time.time() - t0
            _say(f"  [{done}/{len(syms)}] {sym}: {len(b5):,} 5m bars"
                 f"  (elapsed {el:.0f}s, ETA {el / done * (len(syms) - done):.0f}s)")

    await asyncio.gather(*(load(s) for s in syms))
    inp.bars = {s: inp.bars[s] for s in syms}   # keep the original order (BTC first)

    # OI: WEEX has no OI history - use our own snapshots if they cover the period
    disabled: set[str] = set()
    notes: list[str] = []
    oi_rows = db.load_oi_since(start - 5 * 3_600_000) if cfg.backtest.oi_mode != "disable" else []
    by_sym: dict[str, tuple[list, list]] = {}
    for s, ts, v in oi_rows:
        if ts <= end:
            by_sym.setdefault(s, ([], []))
            by_sym[s][0].append(ts)
            by_sym[s][1].append(v)
    period = end - start
    cov = [min(1.0, len(by_sym.get(s, ([], []))[0]) * cfg.open_interest.poll_s * 1000 / period) for s in syms[1:]]
    coverage = sum(cov) / len(cov) if cov else 0.0
    if cfg.backtest.oi_mode != "disable" and coverage >= cfg.backtest.oi_min_coverage:
        inp.oi = by_sym
        notes.append(f"<b>Open interest: ENABLED</b> from this app's own 60 s snapshots "
                     f"({coverage * 100:.0f}% coverage of the period).")
    else:
        if cfg.backtest.oi_mode == "require":
            raise SystemExit(f"OI coverage only {coverage * 100:.0f}% - run with oi_mode auto/disable")
        disabled.add("oi")
        notes.append(f"<b>Open-interest conditions DISABLED.</b> WEEX provides no OI history and this app's own "
                     f"snapshots cover only {coverage * 100:.0f}% of the period. Ignition's oi_chg_1h ≥ "
                     f"{cfg.ignition.min_oi_chg_1h}% and Coil's oi_chg_4h ≥ {cfg.coil.min_oi_chg_4h}% were not "
                     f"applied, and the OI score component was dropped (other components rescaled to 100). "
                     f"<b>Live signals require these, so the backtest fires more often than live would.</b>")
    tot = miss = 0
    for s in syms[1:]:
        b5 = inp.bars[s]["5m"]
        sel = (b5.t >= start) & (b5.t <= end)
        tot += int(sel.sum())
        miss += int(np.isnan(b5.tbqv[sel]).sum())
    if miss:
        notes.append(f"<b>Taker-volume history missing on {miss / max(tot, 1) * 100:.1f}% of 5m bars</b> (empty in WEEX's "
                     f"older klines). Order-flow conditions (taker ratio, CVD) are 'n/a' there, so no Ignition/Coil "
                     f"ENTRY can fire in those stretches.")
    if overrides:
        notes.insert(0, "<b>Settings overridden for this run</b> (config.yaml unchanged): "
                     + html.escape(", ".join(overrides)))
    notes += [
        "<b>CVD / taker-buy ratio: ENABLED</b> - from kline taker volume (WEEX's mislabelled field corrected; "
        "verified equal to the live trade tape in M0/M1).",
        "<b>Funding filter:</b> uses the last <i>settled</i> rate at each bar (live uses the forecast for the next "
        "settlement, which has no history). Funding P&amp;L uses the actual settlements.",
        "<b>Universe:</b> rebuilt every 30 simulated minutes from klines (24h volume, ATR(1h) ≥ "
        f"{cfg.universe.min_atr_1h_pct}%, wick risk, listing age). <b>Spread and order-book depth filters could not "
        "be applied</b> (no order-book history). Only symbols listed on WEEX today were tested "
        "(<b>survivorship bias</b>): delisted coins are missing.",
        "<b>Fills:</b> reference entry = the signal bar's close (live uses the last trade), + "
        f"{cfg.trade.slippage_pct}% slippage. Stops trigger on <b>last-price</b> 5m bars; inside a bar the stop is "
        "checked before targets (conservative). A gap through the stop fills at the bar open.",
        "<b>Liquidation / precision:</b> today's WEEX risk brackets and price ticks were used for the whole period.",
        f"<b>Same code as live:</b> features, regime, setups, score, trade plan and exit engine are the live "
        f"modules; config {html.escape(cfg.hash)}.",
    ]

    return {"inp": inp, "syms": syms, "notes": notes, "disabled": disabled, "start": start, "end": end,
            "now": now, "days": days}


async def main_async(a) -> int:
    base = load_config(a.config)
    d = copy.deepcopy(base.to_dict())
    d["exchange"]["rate_limit_weight"] = d["backtest"]["rate_limit_weight"]   # share the IP with a live scanner
    overrides = apply_overrides(d, a.set or [])
    cfg = Config(d, base.path)
    setup_logging(cfg.data_dir / "logs", cfg.app.log_level)
    t_run = time.time()
    clock = Clock(cfg.app.display_tz)
    rest = WeexRest(cfg, clock)
    db = Database(cfg.database.url.format(data_dir=cfg.data_dir.as_posix()))
    try:
        prep = await prepare(cfg, rest, db, clock, a.days, a.symbols, a.max_symbols, overrides)
        inp, syms, notes, disabled = prep["inp"], prep["syms"], prep["notes"], prep["disabled"]
        start, end, now, days = prep["start"], prep["end"], prep["now"], prep["days"]

        _say(f"Replaying {days * 288:,} five-minute bars through the live signal and exit code...")

        def progress(T, n_sig, n_uni):
            _say(f"  {clock.fmt(T, with_date=True)[:10]}: {n_sig} signals so far, universe {n_uni}")
        result = Backtester(cfg, inp, disabled).run(start, end, progress)

        out_dir = cfg.data_dir / cfg.backtest.report_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = clock.fmt(now, with_date=True).replace(":", "").replace(" ", "_").replace("-", "")[:13]
        tag = ("_" + "".join(ch for ch in a.tag if ch.isalnum() or ch in "-_")) if a.tag else ""
        html_path = out_dir / f"backtest_{stamp}_{days}d{tag}.html"
        csv_path = out_dir / f"backtest_{stamp}_{days}d{tag}_trades.csv"
        meta_out = {"symbols": syms, "config_hash": cfg.hash, "generated_ms": now, "notes": notes,
                    "runtime_s": time.time() - t_run}
        html_path.write_text(build(result, meta_out, cfg), encoding="utf-8")
        cols = ["signal_id", "policy", "symbol", "setup", "score", "regime", "session", "suppressed_reason", "status",
                "entry_ms", "exit_ms", "exit_reason", "entry_ref", "stop", "tp1", "tp2", "risk_usd", "net", "r",
                "funding_usd", "mfe_pct", "mae_pct"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for t in result["trades"]:
                w.writerow([t.get(c, "") if t.get(c) is not None else "" for c in cols])

        from stats.metrics import metrics
        for p in ("L", "S"):
            m = metrics([t for t in result["trades"] if t["policy"] == p and t["status"] == "CLOSED"])
            if m.get("n"):
                _say(f"Policy {p}: {m['n']} trades, win {m['win_rate'] * 100:.0f}%, expectancy "
                     f"{m['expectancy_usd']:+.2f}$ / {m['expectancy_r']:+.2f}R, net {m['net']:+.2f}$")
            else:
                _say(f"Policy {p}: no closed trades")
        _say(f"{len(result['signals'])} signals. Report: {html_path}")
        _say(f"Trades CSV: {csv_path}")
        if a.open:
            webbrowser.open(html_path.as_uri())
        return 0
    finally:
        await rest.close()


def main() -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Ignition & Coil backtester (same code as live)")
    ap.add_argument("--days", type=int, default=None, help="days to replay (default: config backtest.days)")
    ap.add_argument("--symbols", default=None, help="comma-separated symbols (default: top by volume)")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--open", action="store_true", help="open the report in the browser")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="override a config value for this run only, e.g. --set ignition.min_headroom_pct=4")
    ap.add_argument("--tag", default="", help="label added to the report file name")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
