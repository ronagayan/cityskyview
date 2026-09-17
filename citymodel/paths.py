"""Where the app keeps its cache, settings and logs."""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_NAME


def _base(env: str, fallback: Path) -> Path:
    root = os.environ.get(env)
    return Path(root) if root else fallback


def cache_dir() -> Path:
    """Downloaded tiles / API responses. Override with CITYMODEL_CACHE."""
    override = os.environ.get("CITYMODEL_CACHE")
    p = Path(override) if override else (
        _base("LOCALAPPDATA", Path.home() / ".cache") / APP_NAME / "cache")
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    """Settings + log. Override with CITYMODEL_CONFIG."""
    override = os.environ.get("CITYMODEL_CONFIG")
    p = Path(override) if override else _base("APPDATA", Path.home() / ".config") / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return config_dir() / "settings.json"


def log_file() -> Path:
    return config_dir() / "citymodel.log"


def default_output_dir() -> Path:
    return Path.home() / "Documents" / APP_NAME
