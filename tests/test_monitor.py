"""Tests for jupyter-storage-usage monitor."""

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest

# Import after setting up env
os.environ["JSU_HOME_DIR"] = tempfile.mkdtemp()
os.environ["JSU_CHECK_INTERVAL_SEC"] = "1"
os.environ["JSU_WARN_LIMIT_BYTES"] = "100"
os.environ["JSU_HARD_LIMIT_BYTES"] = "200"

from jupyter_storage_usage.monitor import StorageMonitor, UsageData, _du_bytes


def test_usage_data_status():
    """Test UsageData status calculation."""
    d = UsageData(used_bytes=50, total_bytes=1000, warn_limit=100, hard_limit=200, timestamp=0)
    assert d.status == "ok"
    assert d.percent == 25.0

    d = UsageData(used_bytes=150, total_bytes=1000, warn_limit=100, hard_limit=200, timestamp=0)
    assert d.status == "warning"
    assert d.percent == 75.0

    d = UsageData(used_bytes=250, total_bytes=1000, warn_limit=100, hard_limit=200, timestamp=0)
    assert d.status == "over_limit"
    assert d.percent == 125.0


def test_usage_data_to_dict():
    d = UsageData(used_bytes=50, total_bytes=1000, warn_limit=100, hard_limit=200, timestamp=12345)
    result = d.to_dict()
    assert result["used_bytes"] == 50
    assert result["status"] == "ok"
    assert result["timestamp"] == 12345
    assert "used_display" in result
    assert "hard_limit_display" in result


def test_usage_data_unlimited():
    """hard_limit == 0 means NO limit (standalone / unconfigured):
    never over_limit, percent 0, display 'unlimited'."""
    # Well over the warn limit but no hard limit -> warning, not over_limit.
    d = UsageData(used_bytes=500, total_bytes=1000, warn_limit=100, hard_limit=0, timestamp=0)
    assert d.status == "warning"
    assert d.percent == 0.0
    assert d.to_dict()["hard_limit_display"] == "unlimited"

    # Under the warn limit too -> ok.
    d = UsageData(used_bytes=10, total_bytes=1000, warn_limit=100, hard_limit=0, timestamp=0)
    assert d.status == "ok"
    assert d.percent == 0.0


def test_du_bytes():
    """Test du on a temp directory with known content."""
    tmpdir = tempfile.mkdtemp()
    # Create a file with known content
    with open(os.path.join(tmpdir, "test.txt"), "w") as f:
        f.write("x" * 4096)
    size = _du_bytes(tmpdir)
    assert size > 0  # du reports block size, should be >= 4096


@pytest.mark.asyncio
async def test_storage_monitor_caches():
    """Test that the monitor returns cached results."""
    # Reset singleton
    StorageMonitor._instance = None
    monitor = StorageMonitor()

    # No cache yet — should return None
    data1 = await monitor.get_usage()
    assert data1 is None
