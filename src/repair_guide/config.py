"""User config: ~/.config/repair-guide/config.json (override with $REPAIR_GUIDE_CONFIG)."""

from __future__ import annotations

import os
from pathlib import Path

from .util import read_json, write_json

DEFAULTS = {
    "output_dir": "~/RepairGuides",
    "image_width": 1280,
    "image_max_kb": 150,
    "max_height": 1080,
    "caption_langs": ["en"],
    "whisper_model": "base.en",
    "joplin_notebook": "Repair Guides",  # top notebook; guides go in a per-vehicle sub-notebook
    "joplin_url": "",  # blank = find the Web Clipper service on 127.0.0.1:41184+
    "joplin_token": "",  # blank = read it from Joplin desktop's settings.json
    "manuals": {},  # name -> {"path": "...", "index": "...", "pages": N, "torque_lines": N}
}


def config_path() -> Path:
    env = os.environ.get("REPAIR_GUIDE_CONFIG")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return base / "repair-guide" / "config.json"


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", "~/.cache")).expanduser()
    return base / "repair-guide"


def load() -> dict:
    cfg = dict(DEFAULTS)
    cfg["manuals"] = {}
    p = config_path()
    if p.exists():
        cfg.update(read_json(p))
    return cfg


def save(cfg: dict) -> None:
    write_json(config_path(), cfg)


def output_dir(cfg: dict) -> Path:
    return Path(cfg["output_dir"]).expanduser()
