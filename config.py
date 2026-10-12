#!/usr/bin/env python3
"""Shared configuration for Duplicates Explorer."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config.json"
DEFAULT_PORT = 8766

# Used only as a soft default if present on this machine (never required).
LEGACY_DEFAULT_ROOT = Path("/Volumes/BackupData/PhotoVideoAudio/Photos")


def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_config(updates: dict) -> dict:
    cfg = load_config()
    cfg.update({k: v for k, v in updates.items() if v is not None})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    return cfg


def get_port(cli_port: int | None = None) -> int:
    if cli_port:
        return int(cli_port)
    cfg = load_config()
    try:
        return int(cfg.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def get_photos_root(
    cli_root: Path | str | None = None,
    *,
    allow_legacy_default: bool = True,
) -> Path | None:
    """Return configured photo library root, or None if not set / missing."""
    if cli_root:
        p = Path(cli_root).expanduser()
        return p if p.is_dir() else p

    cfg = load_config()
    raw = (cfg.get("photos_root") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        return p

    # Fall back to DB meta if an older index already recorded the root
    db = DATA_DIR / "index.db"
    if db.exists():
        try:
            import sqlite3

            conn = sqlite3.connect(str(db))
            row = conn.execute(
                "SELECT value FROM meta WHERE key='photos_root'"
            ).fetchone()
            conn.close()
            if row and row[0]:
                p = Path(row[0])
                if p.is_dir() or allow_legacy_default:
                    return p
        except Exception:
            pass

    if allow_legacy_default and LEGACY_DEFAULT_ROOT.is_dir():
        return LEGACY_DEFAULT_ROOT

    return None


def photos_root_configured() -> bool:
    root = get_photos_root(allow_legacy_default=False)
    return bool(root and Path(root).is_dir()) or bool(
        (load_config().get("photos_root") or "").strip()
    )


def set_photos_root(path: Path | str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"Folder not found: {p}")
    save_config({"photos_root": str(p)})
    return p
