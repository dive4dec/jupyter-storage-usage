"""Tests for jupyter-storage-usage cleanup portal (server extension)."""

import os
import tempfile
import shutil

import pytest

os.environ["JSU_HOME_DIR"] = tempfile.mkdtemp()
os.environ["JSU_WARN_LIMIT_BYTES"] = "100"
os.environ["JSU_HARD_LIMIT_BYTES"] = "200"

from jupyter_storage_usage.cleanup import (
    _resolve_safe,
    _du_path,
    _fmt_bytes,
)
from jupyter_storage_usage.config import HOME_DIR, CLEANUP_MODE


@pytest.fixture
def home_with_files():
    """Create test files in the fake home directory."""
    home = os.environ["JSU_HOME_DIR"]
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


# ── du ──────────────────────────────────────────────────────────────────

def test_du_path_file(home_with_files):
    size = _du_path(os.path.join(home_with_files, "notebook.ipynb"))
    assert size > 0


def test_du_path_directory(home_with_files):
    size = _du_path(os.path.join(home_with_files, ".cache"))
    assert size >= 10240


def test_du_path_nonexistent():
    assert _du_path("/nonexistent/path") == 0


# ── Cleanup mode config ─────────────────────────────────────────────────

def test_cleanup_mode_default_off():
    """Cleanup mode is off by default."""
    assert CLEANUP_MODE is False
