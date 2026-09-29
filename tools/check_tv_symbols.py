"""Optional: find WEEX perps that TradingView does NOT list as WEEX:<SYMBOL>.P.

Writes var/tv_missing.json; alerts then link those symbols to the fallback template
(BINANCE:<SYMBOL>.P). Uses TradingView's public symbol-search endpoint, which is
UNOFFICIAL and may change - so this is a manual tool, never used at runtime.

    .venv\\Scripts\\python.exe tools\\check_tv_symbols.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import load_config  # noqa: E402

SEARCH = "https://symbol-search.tradingview.com/symbol_search/v3/"


def listed_on_tv(client: httpx.Client, symbol: str) -> bool:
    r = client.get(SEARCH, params={"text": symbol, "exchange": "WEEX", "search_type": "crypto"},
                   headers={"Origin": "https://www.tradingview.com"})
    r.raise_for_status()
    want = f"{symbol}.P"
    for s in r.json().get("symbols", []):
        name = s.get("symbol", "").replace("<em>", "").replace("</em>", "")
        if name == want and s.get("type") == "swap":
            return True
    return False


def main() -> int:
    cfg = load_config()
    info = httpx.get(f"{cfg.exchange.rest_base}/capi/v3/market/exchangeInfo",
                     params={"contractType": "PERPETUAL"}, timeout=30).json()
    vols = {t["symbol"]: float(t.get("quoteVolume") or 0)
            for t in httpx.get(f"{cfg.exchange.rest_base}/capi/v3/market/ticker/24hr", timeout=30).json()}
    syms = sorted(s["symbol"] for s in info["symbols"]
                  if s.get("quoteAsset") == "USDT" and vols.get(s["symbol"], 0) >= cfg.universe.min_quote_volume_24h)
    missing = []
    with httpx.Client(timeout=20) as c:
        for i, s in enumerate(syms, 1):
            try:
                if not listed_on_tv(c, s):
                    missing.append(s)
            except httpx.HTTPError as e:
                print(f"  {s}: lookup failed ({e}) - skipped")
            time.sleep(0.3)
            print(f"\rchecked {i}/{len(syms)}", end="")
    out = cfg.data_dir / cfg.tradingview.tv_missing_file
    out.write_text(json.dumps(missing, indent=1), encoding="utf-8")
    print(f"\n{len(missing)} of {len(syms)} not on TradingView as WEEX perps -> {out}")
    print(", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
