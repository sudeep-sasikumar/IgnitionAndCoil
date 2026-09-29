"""Container start (Docker / VPS): keep Settings-page changes across redeploys, then run the scanner.

The image carries config.yaml from the repository: thresholds are the repository's decision.
The live copy lives in the data volume (var/config.yaml), because the Settings page writes to it
and the container's own files are replaced on every redeploy.
- first start: the image's config.yaml is copied there
- the image's config.yaml changed since the last start (new thresholds pushed): the new file is
  used, but the Settings-page values (margin, leverage, notional, fees, bedtime reminder) are kept
- otherwise: the live copy is used as it is
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from ruamel.yaml import YAML

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.config import ROOT, load_config  # noqa: E402
from web.settings import FIELDS  # noqa: E402


def prepare(image_cfg: Path, live_cfg: Path, seed_file: Path) -> str:
    """Bring live_cfg up to date with image_cfg (see module docstring). Returns what was done."""
    image_hash = hashlib.sha256(image_cfg.read_bytes()).hexdigest()
    seed = seed_file.read_text().strip() if seed_file.exists() else ""
    if not live_cfg.exists():
        shutil.copyfile(image_cfg, live_cfg)
        result = "config.yaml: first start - copied from the image"
    elif seed == image_hash:
        return "config.yaml: unchanged"
    else:
        yaml = YAML()
        yaml.preserve_quotes = True
        with open(image_cfg, encoding="utf-8") as f:
            new = yaml.load(f)
        with open(live_cfg, encoding="utf-8") as f:
            old = yaml.load(f)
        kept = []
        for sec, key in FIELDS.values():
            if sec in old and key in old[sec] and old[sec][key] != new[sec][key]:
                new[sec][key] = old[sec][key]
                kept.append(f"{sec}.{key}")
        with open(live_cfg, "w", encoding="utf-8") as f:
            yaml.dump(new, f)
        result = ("config.yaml: updated from the image" +
                  (f", kept your settings: {', '.join(kept)}" if kept else ""))
    seed_file.write_text(image_hash)
    return result


def main() -> None:
    data_dir = load_config(ROOT / "config.yaml").data_dir
    live = data_dir / "config.yaml"
    print(prepare(ROOT / "config.yaml", live, data_dir / "config.image.sha256"), flush=True)
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "main.py", "--no-browser", "--config", str(live)])


if __name__ == "__main__":
    main()
