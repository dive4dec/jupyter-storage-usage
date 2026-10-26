"""Tests for jupyter-storage-usage cleanup portal (server extension)."""

import asyncio
import importlib
import os
import tempfile
import time

import pytest

os.environ["JSU_HOME_DIR"] = tempfile.mkdtemp()
os.environ["JSU_WARN_LIMIT_BYTES"] = "100"
os.environ["JSU_HARD_LIMIT_BYTES"] = "200"

from jupyter_storage_usage.cleanup import (
    _UsageCache,
    _compute_usage,
    _ensure_usage_cached,
    _resolve_safe,
    _run_du_maxdepth1,
    _USAGE_CACHE,
    _fmt_bytes,
    CleanupHandler,
)
from jupyter_storage_usage.config import CLEANUP_MODE


@pytest.fixture
def home_with_files():
    """Create test files in the fake home directory.

    Sets BOTH the JSU_HOME_DIR env var and the package-level HOME_DIR that
    the du code targets, so the test is correct regardless of test-file
    ordering (test_functional.py mutates the env var at collection time).
    """
    import jupyter_storage_usage.cleanup as cl

    home = tempfile.mkdtemp()
    os.environ["JSU_HOME_DIR"] = home
    cl.HOME_DIR = home
    os.makedirs(os.path.join(home, "homework"), exist_ok=True)
    os.makedirs(os.path.join(home, ".cache"), exist_ok=True)
    with open(os.path.join(home, "notebook.ipynb"), "w") as f:
        f.write("content")
    with open(os.path.join(home, "homework", "hw1.py"), "w") as f:
        f.write("print('hello')")
    with open(os.path.join(home, ".cache", "big.bin"), "wb") as f:
        f.write(b"\x00" * 10240)
    return home


# ── Path safety ─────────────────────────────────────────────────────────

def test_resolve_safe_normal():
    path = _resolve_safe(os.environ["JSU_HOME_DIR"], "homework/hw1.py")
    assert path.endswith("homework/hw1.py")


def test_resolve_safe_root():
    path = _resolve_safe(os.environ["JSU_HOME_DIR"], "")
    assert path == os.path.realpath(os.environ["JSU_HOME_DIR"])


def test_resolve_safe_traversal_blocked():
    with pytest.raises(ValueError):
        _resolve_safe(os.environ["JSU_HOME_DIR"], "../../etc/passwd")


def test_resolve_safe_url_encoded():
    """URL-encoded paths are decoded correctly."""
    path = _resolve_safe(os.environ["JSU_HOME_DIR"], "homework%2Fhw1.py")
    assert path.endswith("homework/hw1.py")


# ── Size formatting ─────────────────────────────────────────────────────

def test_fmt_bytes():
    assert _fmt_bytes(0) == "0 B"
    assert _fmt_bytes(500) == "500 B"
    assert _fmt_bytes(1024) == "1.0 KiB"
    assert _fmt_bytes(1024**2) == "1.0 MiB"
    assert _fmt_bytes(1024**3) == "1.0 GiB"


# ── du cache (single pass, non-blocking) ────────────────────────────────

def test_run_du_maxdepth1(home_with_files):
    """One pass yields the home total + each top-level entry's size."""
    total, sizes = _run_du_maxdepth1()
    # .cache contains a 10 KiB file; total must include it
    assert total >= 10240
    assert ".cache" in sizes
    assert sizes[".cache"] >= 10240
    # The total is the max of the individual top-level sizes (approx:
    # du max-depth-1 total == sum of children + dir overhead)
    assert total >= max(sizes.values())


def test_cache_fresh_lifecycle(home_with_files):
    cache = _UsageCache()
    assert not cache.fresh
    cache.total = 123
    cache.computed_at = time.monotonic()
    assert cache.fresh
    cache.invalidate()
    assert not cache.fresh
    assert cache.total is None


async def test_compute_usage_populates_cache(home_with_files):
    """_compute_usage runs the du in the background and populates the cache."""
    _USAGE_CACHE.invalidate()
    assert _USAGE_CACHE.total is None
    await _compute_usage()
    # Wait for the in-flight task to land
    task = _USAGE_CACHE._inflight
    if task is not None:
        await task
    assert _USAGE_CACHE.total is not None
    assert _USAGE_CACHE.total > 0
    assert ".cache" in _USAGE_CACHE.dir_sizes


async def test_ensure_usage_cached_waits(tmp_path):
    """wait=True returns only after the du has produced a value."""
    import jupyter_storage_usage.cleanup as cl

    home = str(tmp_path / "home")
    big = str(tmp_path / "home" / "bigdir")
    os.makedirs(big)
    with open(os.path.join(big, "blob.bin"), "wb") as f:
        f.write(b"\x00" * 1024)

    orig_home = cl.HOME_DIR
    cache = cl._UsageCache()
    cl._USAGE_CACHE = cache
    cl.HOME_DIR = home
    try:
        assert not cache.fresh
        t0 = time.monotonic()
        await cl._ensure_usage_cached(wait=True)
        elapsed = time.monotonic() - t0
        assert cache.total is not None and cache.total >= 1024
        # du --max-depth=1 reports each top-level DIRECTORY (not files)
        assert "bigdir" in cache.dir_sizes
        assert cache.dir_sizes["bigdir"] >= 1024
        # wait=True returned after the du landed, well under the ceiling
        assert elapsed < 30
    finally:
        cl._USAGE_CACHE = _USAGE_CACHE
        cl.HOME_DIR = orig_home


async def test_render_dir_never_blocks_on_slow_du(home_with_files):
    """The portal renders immediately even when the du is still running.

    This is the regression test for the original bug: the old code did a
    full recursive `du` (plus one per top-level dir) synchronously in
    get() before rendering, so a large/NFS home froze the portal forever.
    """
    import jupyter_storage_usage.cleanup as cl

    home_with_files  # populate fake home
    _USAGE_CACHE.invalidate()

    # Make the du artificially slow (simulate a huge NFS home).
    real = cl._run_du_maxdepth1
    slow_calls = []

    def slow_du():
        slow_calls.append(1)
        time.sleep(1.5)  # would have blocked the request for 1.5s+ before
        return real()

    cl._run_du_maxdepth1 = slow_du
    try:
        await _compute_usage()  # kick the slow background du; must NOT block

        # Minimal stand-in for the handler: _render_dir only uses
        # base_url, set_header, write, _cleanup_url, and _list_dir_sync.
        class _FakeHandler:
            base_url = "/"

            def __init__(self):
                self._captured = []

            def _cleanup_url(self, sub_path=""):
                base = self.base_url.rstrip("/")
                p = sub_path.lstrip("/")
                return f"{base}/cleanup/{p}"

            def set_header(self, k, v):
                pass

            def write(self, chunk, *a, **k2):
                self._captured.append(chunk)

            _list_dir_sync = staticmethod(CleanupHandler._list_dir_sync)
            _breadcrumbs = CleanupHandler._breadcrumbs

        h = _FakeHandler()

        t0 = time.monotonic()
        await CleanupHandler._render_dir(h, "", cl.HOME_DIR, _USAGE_CACHE,
                                         computing=not _USAGE_CACHE.fresh)
        elapsed = time.monotonic() - t0

        # Must render fast, NOT wait for the 1.5s du.
        assert elapsed < 1.0, f"render blocked {elapsed:.2f}s on the du"
        out = "".join(str(c) for c in h._captured)
        # The directory rows render (names present), size shows "…" while
        # computing, and the page auto-refreshes.
        assert "homework" in out
        assert "Computing storage sizes" in out
        assert 'http-equiv="refresh"' in out
        assert "…" in out
    finally:
        cl._run_du_maxdepth1 = real


def test_list_dir_sync_is_shallow(home_with_files):
    """_list_dir_sync uses os.stat only — it must not recurse into subdirs."""
    import jupyter_storage_usage.cleanup as cl
    entries = CleanupHandler._list_dir_sync(cl.HOME_DIR)
    names = {e["name"] for e in entries}
    assert "homework" in names
    assert "notebook.ipynb" in names
    by_name = {e["name"]: e for e in entries}
    # The file has its real apparent size; the dir size is left for the
    # du cache to fill (stat.st_size of a dir is small/meaningless).
    assert by_name["notebook.ipynb"]["size"] == 7
    assert by_name["homework"]["is_dir"] is True
    assert by_name["notebook.ipynb"]["is_dir"] is False


# ── Cleanup mode config ─────────────────────────────────────────────────

def test_cleanup_mode_default_off():
    """CLEANUP_MODE follows the env var (off unless explicitly set)."""
    import os as _os
    import jupyter_storage_usage.config as cfg
    saved = _os.environ.pop("JSU_CLEANUP_MODE", None)
    try:
        # Reload with the var unset: fresh() default is False
        importlib.reload(cfg)
        assert cfg.CLEANUP_MODE is False
        _os.environ["JSU_CLEANUP_MODE"] = "1"
        importlib.reload(cfg)
        assert cfg.CLEANUP_MODE is True
    finally:
        if saved is not None:
            _os.environ["JSU_CLEANUP_MODE"] = saved
        else:
            _os.environ.pop("JSU_CLEANUP_MODE", None)
        importlib.reload(cfg)
