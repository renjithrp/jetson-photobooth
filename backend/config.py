"""Settings persistence: load/merge/save settings.json atomically and thread-safely."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .models import Settings

DATA_DIR = Path(os.environ.get("BOOTH_DATA", Path(__file__).resolve().parent.parent / "data"))
SETTINGS_PATH = DATA_DIR / "settings.json"

_lock = threading.RLock()
_cache: Settings | None = None


def data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def captures_dir(settings: Settings | None = None) -> Path:
    settings = settings or load()
    p = Path(settings.storage.local_dir)
    if not p.is_absolute():
        p = data_dir() / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively (override wins)."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> Settings:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        defaults = Settings().model_dump()
        if SETTINGS_PATH.exists():
            try:
                saved = json.loads(SETTINGS_PATH.read_text())
                merged = _deep_merge(defaults, saved)
                _cache = Settings.model_validate(merged)
            except Exception as e:  # corrupt file -> fall back to defaults
                print(f"[config] failed to read {SETTINGS_PATH}: {e}; using defaults")
                _cache = Settings()
        else:
            _cache = Settings()
            _write(_cache)
        return _cache


def _write(settings: Settings) -> None:
    data_dir()
    fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings.model_dump(), f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save(settings: Settings) -> Settings:
    global _cache
    with _lock:
        _write(settings)
        _cache = settings
        return _cache


def update(partial: dict) -> Settings:
    """Deep-merge a partial dict from the admin UI into current settings."""
    with _lock:
        current = load().model_dump()
        merged = _deep_merge(current, partial)
        settings = Settings.model_validate(merged)  # validates types
        return save(settings)
