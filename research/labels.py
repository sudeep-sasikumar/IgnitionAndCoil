"""Outcome labels: what happened AFTER each bar (never available to the scanner at the time).

For a 5m close c0 at bar i (bars must be contiguous; any gap -> NaN):
  fwd_1h / fwd_4h / fwd_24h  close-to-close return after 12 / 48 / 288 bars, %
  mfe_4h / mae_4h            highest high / lowest low of the next 48 bars vs c0, %
  tp3_first                  1 if +tp% trades before -sl% within 4h, 0 if -sl% first (or both in the
                             same bar - counted as a loss), NaN if neither: a stop-independent win proxy
"""
from __future__ import annotations

import numpy as np

M5 = 300_000


def label_arrays(t: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, tp_pct: float, sl_pct: float) -> dict:
    """Labels for bars t (open times, ascending). Gaps in t make the affected labels NaN."""
    n = len(t)
    out = {k: np.full(n, np.nan) for k in ("fwd_1h", "fwd_4h", "fwd_24h", "mfe_4h", "mae_4h", "tp3_first")}
    if n < 2:
        return out
    # place on a full 5m grid so gaps become NaN
    grid0 = int(t[0])
    idx = ((t - grid0) // M5).astype(np.int64)
    size = int(idx[-1]) + 1
    H, L, C = (np.full(size, np.nan) for _ in range(3))
    H[idx], L[idx], C[idx] = h, l, c
    for k, bars in (("fwd_1h", 12), ("fwd_4h", 48), ("fwd_24h", 288)):
        fut = np.full(size, np.nan)
        fut[:-bars] = C[bars:]
        out[k] = ((fut / C - 1) * 100)[idx]
    w = 48
    if size > w:
        hw = np.lib.stride_tricks.sliding_window_view(np.r_[H[1:], np.full(1, np.nan)], w)   # bars i+1 .. i+48
        lw = np.lib.stride_tricks.sliding_window_view(np.r_[L[1:], np.full(1, np.nan)], w)
        m = len(hw)
        valid = ~np.isnan(hw).any(axis=1) & ~np.isnan(lw).any(axis=1)
        c0 = C[:m]
        mfe = np.where(valid, (hw.max(axis=1) / c0 - 1) * 100, np.nan)
        mae = np.where(valid, (lw.min(axis=1) / c0 - 1) * 100, np.nan)
        up = hw >= (c0 * (1 + tp_pct / 100))[:, None]
        dn = lw <= (c0 * (1 - sl_pct / 100))[:, None]
        first_up = np.where(up.any(axis=1), up.argmax(axis=1), w)
        first_dn = np.where(dn.any(axis=1), dn.argmax(axis=1), w)
        tp = np.where(first_up < first_dn, 1.0, np.where(first_dn <= first_up, 0.0, np.nan))
        tp[(first_up == w) & (first_dn == w)] = np.nan
        tp[~valid] = np.nan
        full = {"mfe_4h": mfe, "mae_4h": mae, "tp3_first": tp}
        for k, v in full.items():
            arr = np.full(size, np.nan)
            arr[:m] = v
            out[k] = arr[idx]
    return out
