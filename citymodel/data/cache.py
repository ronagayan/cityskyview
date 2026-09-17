"""A small on-disk cache: one file per key, optional time-to-live.

Keys are arbitrary strings (URLs, query text); the file name is their SHA-1 so
anything is safe on Windows. Writes go through a temp file + rename so a crash
or a cancelled download can never leave a half-written entry behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from ..paths import cache_dir

DAY = 86400.0


class DiskCache:
    def __init__(self, namespace: str, ttl_s: Optional[float] = None,
                 root: Optional[Path] = None):
        self.dir = Path(root or cache_dir()) / namespace
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_s = ttl_s

    def path(self, key: str, suffix: str = "") -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.dir / h[:2] / (h + suffix)

    def has(self, key: str, suffix: str = "") -> bool:
        p = self.path(key, suffix)
        if not p.is_file():
            return False
        if self.ttl_s is not None and time.time() - p.stat().st_mtime > self.ttl_s:
            return False
        return True

    def get_bytes(self, key: str, suffix: str = "") -> Optional[bytes]:
        if not self.has(key, suffix):
            return None
        try:
            return self.path(key, suffix).read_bytes()
        except OSError:
            return None

    def put_bytes(self, key: str, data: bytes, suffix: str = "") -> Path:
        p = self.path(key, suffix)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return p

    def get_json(self, key: str):
        raw = self.get_bytes(key, ".json")
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def put_json(self, key: str, obj) -> Path:
        return self.put_bytes(key, json.dumps(obj).encode("utf-8"), ".json")

    def get_stale_json(self, key: str):
        """Ignore the TTL -- used as a fallback when the network is down."""
        p = self.path(key, ".json")
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            return None


def cache_size_bytes(root: Optional[Path] = None) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root or cache_dir()):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
    return total


def clear_cache(root: Optional[Path] = None) -> int:
    """Delete every cached file. Returns the number of bytes freed."""
    freed = 0
    for dirpath, _dirs, files in os.walk(root or cache_dir()):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                freed += os.path.getsize(p)
                os.remove(p)
            except OSError:
                pass
    return freed
