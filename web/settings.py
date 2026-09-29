"""Editable settings (spec §12): default margin/leverage/notional, fee rates, bedtime reminder.

Saved into config.yaml with ruamel.yaml (keeps comments and layout), applied to the running
config immediately, and recorded in config_versions.
"""
from __future__ import annotations

import re

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from plan.liquidation import link_size

# form field -> (config section, key)
FIELDS = {
    "margin_usd": ("trade", "margin_usd"),
    "leverage": ("trade", "leverage"),
    "notional_usd": ("trade", "notional_usd"),
    "maker_fee": ("trade", "maker_fee"),
    "taker_fee": ("trade", "taker_fee"),
    "bedtime_reminder": ("reminders", "bedtime_reminder"),
}
MAX_LEVERAGE = 400   # WEEX's highest max leverage (BTC); per-symbol limits are checked per trade


class SettingsError(ValueError):
    pass


def current(cfg) -> dict:
    d = cfg.to_dict()
    return {k: d[sec][key] for k, (sec, key) in FIELDS.items()}


def validate(values: dict, edited: str = "margin") -> dict:
    """Validate and normalise. Margin/leverage/notional are linked: the notional is always
    margin x leverage (editing notional recalculates margin at the current leverage)."""
    try:
        margin = float(values["margin_usd"])
        lev = float(values["leverage"])
        notional = float(values["notional_usd"])
        maker = float(values["maker_fee"])
        taker = float(values["taker_fee"])
    except (KeyError, TypeError, ValueError) as e:
        raise SettingsError(f"invalid number: {e}") from None
    if not (1 <= lev <= MAX_LEVERAGE):
        raise SettingsError(f"leverage must be between 1 and {MAX_LEVERAGE}")
    if edited not in ("margin", "leverage", "notional"):
        edited = "margin"
    margin, lev, notional = link_size(margin, lev, notional, edited)
    if not (0 < margin <= 1_000_000):
        raise SettingsError("margin must be > 0")
    for name, fee in (("maker fee", maker), ("taker fee", taker)):
        if not (0 <= fee <= 0.005):
            raise SettingsError(f"{name} must be between 0 and 0.005 (0.5%)")
    bed = str(values.get("bedtime_reminder", "")).strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", bed):
        raise SettingsError("bedtime reminder must be HH:MM (24h, London time)")
    lev_out: float | int = int(lev) if lev == int(lev) else lev
    return {"margin_usd": round(margin, 4), "leverage": lev_out, "notional_usd": round(notional, 4),
            "maker_fee": maker, "taker_fee": taker, "bedtime_reminder": bed}


def save(cfg, values: dict) -> dict:
    """Write validated values to config.yaml and the live config. Returns {field: [old, new]}."""
    yaml = YAML()
    yaml.preserve_quotes = True
    with open(cfg.path, encoding="utf-8") as f:
        doc = yaml.load(f)
    live = cfg.to_dict()
    changes = {}
    for k, (sec, key) in FIELDS.items():
        old = live[sec][key]
        new = values[k]
        if old != new:
            changes[f"{sec}.{key}"] = [old, new]
        # Strings must stay quoted: PyYAML (YAML 1.1) reads a bare 23:30 as the number 1410.
        doc[sec][key] = DoubleQuotedScalarString(new) if isinstance(new, str) else new
        live[sec][key] = new
    with open(cfg.path, "w", encoding="utf-8") as f:
        yaml.dump(doc, f)
    return changes
