"""Configuration via environment variables.

All settings are env-var based so JupyterHub can inject them via extraEnv
without needing to modify the server config file.

In normal mode (JSU_CLEANUP_MODE=0):
  - Monitors home directory storage usage
  - Reports to hub via /report endpoint
  - Shows status bar widget in JupyterLab

In cleanup mode (JSU_CLEANUP_MODE=1):
  - Takes over the Jupyter server with a file manager portal
  - Allows browse, delete, download (zip/tar.gz) — no upload/edit
  - Reports fresh usage to hub after each delete
  - Blocks all non-cleanup routes (no /lab/ access)
"""

import os


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "1" if default else "0")
    return val.lower() in ("1", "true", "yes", "on")


def _env_quantity(key: str, default: int) -> int:
    """Parse an env var as a K8s-style quantity or plain bytes.

    Accepts: "10Gi", "500Mi", "1Ti", "21474836480", etc.
    Falls back to plain int, then default.
    """
    raw = os.environ.get(key, "")
    if not raw:
        return default
    # Try plain int first (fast path)
    try:
        return int(raw)
    except ValueError:
        pass
    # Try K8s quantity parsing
    try:
        from .quantity import parse_quantity
        return parse_quantity(raw)
    except (ValueError, ImportError):
        return default


# Directory to monitor
HOME_DIR = _env_str("JSU_HOME_DIR", "/home/jovyan")

# Soft limit — show warning in UI
# Default: 10 GiB (matches hub-side default)
WARN_LIMIT_BYTES = _env_quantity("JSU_WARN_LIMIT_BYTES", 10 * 1024**3)

# Hard limit — hub will cull + block spawn; server goes read-only.
# Default: 0 = NO LIMIT (unlimited).
#   - On a JupyterHub, the hub's pre-spawn hook ALWAYS injects the resolved
#     per-user/group limit (hub-side default 20Gi, or a per-user/group
#     override like ccha23) into JSU_HARD_LIMIT_BYTES, so the pod never sees
#     this code default on the hub — hubs keep their configured limit.
#   - Only a standalone `docker run` (no hub, env var unset) falls to this
#     default -> unlimited. The widget renders "no limit" when the value is 0.
#   - Set explicitly for a standalone box: JSU_HARD_LIMIT_BYTES=20Gi (or
#     "unlimited" to force no limit; see quantity.parse_quantity).
HARD_LIMIT_BYTES = _env_quantity("JSU_HARD_LIMIT_BYTES", 0)

# How often to check usage locally (seconds)
# Default: 1800 (30 min). Persistent cache on NFS means staleness is acceptable.
# The du runs with nice -n 19 to avoid competing with user workloads.
CHECK_INTERVAL_SEC = _env_int("JSU_CHECK_INTERVAL_SEC", 1800)

# ── Cleanup portal du cache ──────────────────────────────────────────────
# The cleanup portal used to run `du` over the home dir AND over every
# top-level directory, synchronously, on every page load. On a 0.5 CPU
# cleanup pod with a large NFS-backed home that blocks the first (and
# often every) page render for minutes — the portal appeared stuck
# loading forever. Instead, a single `du --max-depth=1` now runs in the
# background (started at server boot, refreshed periodically) and its
# result is cached in-process. Portal pages render immediately and show
# cached sizes or "computing…" — the du NEVER blocks a request.
#
# Refresh interval for the background du (seconds).
USAGE_CACHE_REFRESH_SEC = _env_int("JSU_USAGE_CACHE_REFRESH_SEC", 60)
# Hard ceiling for one background du (seconds). The task is cancelled
# past this and the cache is left for the next cycle, so a pathological
# home cannot even pin a worker thread indefinitely.
CLEANUP_DU_MAX_SECONDS = _env_int("JSU_CLEANUP_DU_MAX_SECONDS", 300)

# ── Cleanup mode ────────────────────────────────────────────────────────
# When JSU_CLEANUP_MODE=1, the server extension takes over the root URL
# and serves the cleanup portal (restricted file manager) instead of
# the normal JupyterLab interface. This is set by the pre-spawn hook
# when a user is over their storage limit.
CLEANUP_MODE = _env_bool("JSU_CLEANUP_MODE", False)

# ── Hub report URL ──────────────────────────────────────────────────────
# The cleanup pod reports its usage to the hub storage-usage service.
# Default: constructed from the hub service DNS + port + service prefix.
# In z2jh, the service prefix is /{baseUrl}/services/storage-usage/.
# We read JSU_SERVICE_PREFIX (set in hub.extraEnv) or fall back to
# JUPYTERHUB_SERVICE_PREFIX (set by JupyterHub for the service subprocess).
# The pre-spawn hook injects JSU_HUB_REPORT_URL from the hub-side config,
# so this default is only used when the env var is not set.
_service_prefix = (
    os.environ.get("JSU_SERVICE_PREFIX")
    or os.environ.get("JUPYTERHUB_SERVICE_PREFIX")
    or "/"
)
_report_default = f"http://hub:9099{_service_prefix.rstrip('/')}/report"
HUB_REPORT_URL = _env_str("JSU_HUB_REPORT_URL", _report_default)
# Service API token injected by the pre-spawn hook — used to authenticate
# report requests to the hub's ReportHandler.
HUB_API_TOKEN = _env_str("JSU_HUB_API_TOKEN", "")
