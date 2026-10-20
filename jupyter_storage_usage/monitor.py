"""Core storage monitor — runs inside the user pod, checks local disk usage.

Efficiency strategy:
  1. Persistent cache file on NFS (.storage-usage.json) — survives pod
     restarts, so the widget shows the last known value instantly.
  2. du runs in a background thread with nice +19 (lowest priority) so it
     doesn't compete with the user's kernels/workloads.
  3. Check interval defaults to 1800s (30 min) — the persistent cache
     makes staleness acceptable; users see their last measured value
     immediately on pod start.
  4. get_usage() never triggers du — it only reads the in-memory cache
     (populated from the persistent file on startup, then refreshed by
     the background task).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import (
    CHECK_INTERVAL_SEC,
    HARD_LIMIT_BYTES,
    HOME_DIR,
    HUB_REPORT_URL,
    WARN_LIMIT_BYTES,
)

logger = logging.getLogger("jupyter_storage_usage")

_GIB = 1024**3
_CACHE_FILE = os.path.join(HOME_DIR, ".storage-usage.json")


@dataclass
class UsageData:
    """Snapshot of storage usage at a point in time."""

    used_bytes: int
    total_bytes: int  # filesystem capacity (from statvfs)
    warn_limit: int
    hard_limit: int
    timestamp: float  # epoch seconds
    status: str = field(init=False)  # "ok" | "warning" | "over_limit"
    percent: float = field(init=False)

    def __post_init__(self):
        # hard_limit == 0 means "no limit" (standalone / unconfigured): never
        # over_limit, percent 0. The hub always injects a real limit, so this
        # branch only affects standalone runs.
        if self.hard_limit <= 0:
            self.status = (
                "warning" if self.used_bytes > self.warn_limit else "ok"
            )
        elif self.used_bytes > self.hard_limit:
            self.status = "over_limit"
        elif self.used_bytes > self.warn_limit:
            self.status = "warning"
        else:
            self.status = "ok"
        self.percent = (
            round(self.used_bytes / self.hard_limit * 100, 1)
            if self.hard_limit > 0
            else 0.0
        )

    def to_dict(self) -> dict:
        from .quantity import format_bytes
        # hard_limit 0 = no limit; render "unlimited" so the widget/tooltips
        # never show a confusing "0" or "0Gi".
        if self.hard_limit > 0:
            hard_display = format_bytes(self.hard_limit)
        else:
            hard_display = "unlimited"
        return {
            "used_bytes": self.used_bytes,
            "used_display": format_bytes(self.used_bytes),
            "total_bytes": self.total_bytes,
            "warn_limit_bytes": self.warn_limit,
            "warn_limit_display": format_bytes(self.warn_limit),
            "hard_limit_bytes": self.hard_limit,
            "hard_limit_display": hard_display,
            "percent": self.percent,
            "status": self.status,
            "timestamp": self.timestamp,
            "home_dir": HOME_DIR,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _du_bytes(path: str) -> Optional[int]:
    """Get disk usage of a directory in bytes.

    Uses `du -sb` (apparent size in bytes) with nice +19 (lowest priority) so it
    doesn't compete with the user's workloads.
    Returns None on failure — the caller keeps the previous cached value.
    """
    try:
        result = subprocess.run(
            ["nice", "-n", "19", "du", "-sb", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            # du -sb outputs: "<bytes>\t<path>"
            return int(result.stdout.split()[0])
        logger.warning("du returned code %d for %s: %s", result.returncode, path, result.stderr[:200])
    except subprocess.TimeoutExpired:
        logger.warning("du timed out for %s (120s)", path)
    except (ValueError, FileNotFoundError) as e:
        logger.warning("du failed for %s: %s", path, e)
    return None


def _statvfs_total(path: str) -> int:
    """Get total filesystem capacity from statvfs."""
    try:
        sv = os.statvfs(path)
        return sv.f_blocks * sv.f_frsize
    except OSError:
        return 0


def _load_persistent_cache() -> Optional[UsageData]:
    """Load cached usage from the NFS-backed cache file.

    This survives pod restarts so the widget can show the last known
    value immediately instead of "loading…" while du runs.
    """
    try:
        with open(_CACHE_FILE, "r") as f:
            data = json.load(f)
        return UsageData(
            used_bytes=data["used_bytes"],
            total_bytes=data.get("total_bytes", 0),
            warn_limit=data.get("warn_limit", WARN_LIMIT_BYTES),
            hard_limit=data.get("hard_limit", HARD_LIMIT_BYTES),
            timestamp=data["timestamp"],
        )
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.debug("No persistent cache: %s", e)
        return None


def _save_persistent_cache(usage: UsageData):
    """Save usage to the NFS-backed cache file."""
    try:
        with open(_CACHE_FILE, "w") as f:
            f.write(usage.to_json())
    except OSError as e:
        logger.debug("Failed to save persistent cache: %s", e)


class StorageMonitor:
    """Singleton monitor that caches usage data and refreshes periodically."""

    _instance: Optional["StorageMonitor"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._cached: Optional[UsageData] = None
        self._task: Optional[asyncio.Task] = None

    async def get_usage(self) -> Optional[UsageData]:
        """Return cached usage only. Never triggers du on-demand."""
        return self._cached

    async def start_periodic(self):
        """Start background task that refreshes usage periodically.

        On startup, loads the persistent cache (from NFS) so the widget
        shows the last known value immediately. Then runs du in a
        low-priority background thread to get fresh data.

        Also reports usage to the hub storage-usage service.
        """
        if self._task and not self._task.done():
            return

        # Load persistent cache immediately (instant — reads one small file)
        cached = await asyncio.to_thread(_load_persistent_cache)
        if cached:
            self._cached = cached
            age = time.time() - cached.timestamp
            logger.info(
                "Loaded persistent cache: %.1f GiB (measured %.0fs ago)",
                cached.used_bytes / _GIB, age,
            )

        async def _loop():
            while True:
                try:
                    # Run du in a background thread with low priority
                    used = await asyncio.to_thread(_du_bytes, HOME_DIR)
                    if used is None:
                        # du failed — keep previous cached value, don't overwrite
                        logger.info("du failed, keeping cached value")
                        if self._cached:
                            await self._report_to_hub(self._cached)
                    else:
                        total = await asyncio.to_thread(_statvfs_total, HOME_DIR)
                        self._cached = UsageData(
                            used_bytes=used,
                            total_bytes=total,
                            warn_limit=WARN_LIMIT_BYTES,
                            hard_limit=HARD_LIMIT_BYTES,
                            timestamp=time.time(),
                        )
                        # Save to persistent cache on NFS
                        await asyncio.to_thread(_save_persistent_cache, self._cached)

                        if self._cached.status == "over_limit":
                            logger.warning(
                                "Storage over hard limit: %.1f GiB > %.1f GiB",
                                used / _GIB, HARD_LIMIT_BYTES / _GIB,
                            )
                        elif self._cached.status == "warning":
                            logger.info(
                                "Storage over warn limit: %.1f GiB > %.1f GiB",
                                used / _GIB, WARN_LIMIT_BYTES / _GIB,
                            )

                        await self._report_to_hub(self._cached)
                except Exception as e:
                    logger.error("Periodic storage check failed: %s", e)
                await asyncio.sleep(CHECK_INTERVAL_SEC)

        self._task = asyncio.create_task(_loop())
        logger.info(
            "Storage monitor started (interval=%ds, home=%s, cache=%s)",
            CHECK_INTERVAL_SEC, HOME_DIR, _CACHE_FILE,
        )

    async def _report_to_hub(self, usage: UsageData):
        """Report current usage to the hub storage-usage service."""
        username = os.environ.get("JUPYTERHUB_USER", "")
        if not username:
            return  # Not running in JupyterHub

        from .config import HUB_API_TOKEN
        hub_report_url = HUB_REPORT_URL

        payload = {
            "username": username,
            "used_bytes": usage.used_bytes,
            "server_url": os.environ.get("JUPYTERHUB_SERVICE_PREFIX", ""),
        }

        # Send auth header so the ReportHandler accepts the request
        headers = {}
        if HUB_API_TOKEN:
            headers["Authorization"] = f"token {HUB_API_TOKEN}"

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    hub_report_url,
                    params=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                    allow_redirects=False,
                ) as resp:
                    if resp.status != 200:
                        logger.warning("Hub report: HTTP %d from %s", resp.status, hub_report_url)
                    else:
                        logger.info("Hub report: OK (used=%d bytes)", usage.used_bytes)
        except Exception as e:
            logger.warning("Hub report failed: %s (url=%s)", e, hub_report_url)

    async def stop_periodic(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
