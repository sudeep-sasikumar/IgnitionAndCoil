"""Config loading. Every threshold comes from config.yaml; secrets from .env."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

def _app_home() -> Path:
    """Folder holding config.yaml, .env and var/.
    IC_HOME overrides; the packaged .exe uses the folder the exe is in; otherwise the project."""
    env = os.environ.get("IC_HOME", "").strip()
    if env:
        return Path(env).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _app_home()


def apply_overrides(d: dict, items: list[str]) -> list[str]:
    """section.key=value overrides (value parsed as YAML). Only existing keys can be set.
    Used by `--set` (backtests) and the IC_OVERRIDES environment variable (Docker/VPS)."""
    done = []
    for item in items:
        item = item.strip()
        if not item:
            continue
        path, sep, raw = item.partition("=")
        if not sep:
            raise SystemExit(f"override needs key=value, got '{item}'")
        keys = path.strip().split(".")
        node = d
        for k in keys[:-1]:
            if not isinstance(node, dict) or k not in node:
                raise SystemExit(f"override: unknown config key '{path}'")
            node = node[k]
        if keys[-1] not in node:
            raise SystemExit(f"override: unknown config key '{path}'")
        node[keys[-1]] = yaml.safe_load(raw)
        done.append(f"{path}={node[keys[-1]]!r}")
    return done


class Section:
    """Read-only attribute access over a nested dict: cfg.universe.max_symbols."""

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            value = self._data[name]
        except KeyError:
            raise AttributeError(f"config key missing: {name}") from None
        return Section(value) if isinstance(value, dict) else value

    def __getitem__(self, name: str) -> Any:
        return self.__getattr__(name)

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def get(self, name: str, default: Any = None) -> Any:
        return self.__getattr__(name) if name in self._data else default

    def to_dict(self) -> dict[str, Any]:
        return self._data


class Config(Section):
    def __init__(self, data: dict[str, Any], path: Path):
        super().__init__(data)
        self.path = path

    @property
    def hash(self) -> str:
        blob = json.dumps(self._data, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    @property
    def data_dir(self) -> Path:
        d = Path(self._data["app"]["data_dir"])
        d = d if d.is_absolute() else ROOT / d
        d.mkdir(parents=True, exist_ok=True)
        return d


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else ROOT / "config.yaml"
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # e.g. IC_OVERRIDES="dashboard.bind_host=0.0.0.0;dashboard.cookie_secure=true" (Docker / VPS)
    env = os.environ.get("IC_OVERRIDES", "")
    if env.strip():
        apply_overrides(data, env.split(";"))
    return Config(data, p)


def load_env() -> dict[str, str]:
    load_dotenv(ROOT / ".env")
    keys = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DASHBOARD_PASSWORD", "COINGECKO_DEMO_API_KEY")
    return {k: os.environ.get(k, "") for k in keys}
