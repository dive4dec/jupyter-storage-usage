"""Tests for jupyter-storage-usage quantity parsing, incl. the no-limit sentinels."""
import pytest

from jupyter_storage_usage.quantity import parse_quantity


def test_parse_quantity_normal():
    assert parse_quantity("10Gi") == 10 * 1024**3
    assert parse_quantity("500Mi") == 500 * 1024**2
    assert parse_quantity("2000000") == 2000000


def test_parse_quantity_unlimited_sentinels():
    for s in ("unlimited", "none", "infinity", "inf", "-", "UNLIMITED", "Unlimited"):
        assert parse_quantity(s) == 0, f"{s!r} should parse to 0"


def test_parse_quantity_invalid():
    for s in ("", "abc", "10ZZ", "Gi"):
        with pytest.raises(ValueError):
            parse_quantity(s)
