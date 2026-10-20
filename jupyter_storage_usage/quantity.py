"""Kubernetes-style quantity parsing (mirrors k8s.io/apimachinery/pkg/api/resource).

Supports the same suffixes as PVC storage capacity:
    Ki, Mi, Gi, Ti, Pi, Ei  (binary, base-1024)
    k, M, G, T, P, E         (decimal, base-1000)
    m                        (milli — decimal fraction, e.g. "500m" = 0.5)
    (no suffix)              (plain bytes)

Examples:
    parse_quantity("10Gi")   → 10737418240
    parse_quantity("500Mi")  → 524288000
    parse_quantity("1Ti")    → 1099511627776
    parse_quantity("20G")    → 20000000000
    parse_quantity("1024")   → 1024
    parse_quantity("500m")   → 1   (rounded to integer bytes)

Usage in config files:
    {"warn": "10Gi", "hard": "20Gi"}
    {"warn": "500Mi", "hard": "1Gi"}
"""

from __future__ import annotations

import re

# Binary suffixes (base-1024)
_BINARY = {
    "Ki": 1024,
    "Mi": 1024 ** 2,
    "Gi": 1024 ** 3,
    "Ti": 1024 ** 4,
    "Pi": 1024 ** 5,
    "Ei": 1024 ** 6,
}

# Decimal suffixes (base-1000)
_DECIMAL = {
    "k": 1000,
    "M": 1000 ** 2,
    "G": 1000 ** 3,
    "T": 1000 ** 4,
    "P": 1000 ** 5,
    "E": 1000 ** 6,
}

_PATTERN = re.compile(
    r'^([0-9]+(?:\.[0-9]+)?)([KMGTPE]i?|m)?$',
    re.IGNORECASE,
)


def parse_quantity(s: str) -> int:
    """Parse a Kubernetes-style quantity string into bytes (int).

    Raises ValueError if the string is not a valid quantity.

    Special values: "unlimited" / "none" / "infinity" (case-insensitive)
    parse to 0, meaning "no limit".
    """
    if not s or not isinstance(s, str):
        raise ValueError(f"Invalid quantity: {s!r}")

    s = s.strip()
    if s.lower() in ("unlimited", "none", "infinity", "inf", "-"):
        return 0

    match = _PATTERN.match(s)
    if not match:
        raise ValueError(f"Invalid quantity: {s!r}")

    value_str = match.group(1)
    suffix = match.group(2)

    if suffix is None:
        # Plain bytes
        return int(float(value_str))

    # Normalize suffix — only first char case matters for decimal,
    # and 'i' must be lowercase
    suffix_lower = suffix.lower()

    if suffix == 'm':
        # Milli — decimal fraction (lowercase 'm' only, like k8s)
        milli = float(value_str)
        return int(round(milli / 1000))

    # Check binary suffixes (case-insensitive on the letter, 'i' lowercase)
    if suffix_lower.endswith('i'):
        key = suffix[0].upper() + 'i'
        if key in _BINARY:
            return int(float(value_str) * _BINARY[key])
        raise ValueError(f"Unknown binary suffix: {suffix!r}")

    # Decimal suffixes (k=1000, M=1000000, etc.)
    # k8s uses lowercase k but uppercase M/G/T/P/E
    key = suffix  # keep original case
    if key == 'k':
        return int(float(value_str) * 1000)
    key = suffix.upper()
    if key in _DECIMAL and suffix != 'm':
        return int(float(value_str) * _DECIMAL[key])

    raise ValueError(f"Unknown suffix: {suffix!r}")


def format_bytes(bytes: int) -> str:
    """Format bytes as a human-readable string with the most appropriate unit.

    Uses binary units (Ki, Mi, Gi, Ti) consistent with PVC capacity display.

    Examples:
        format_bytes(0)            → "0"
        format_bytes(536870912)    → "512Mi"
        format_bytes(10737418240)  → "10Gi"
        format_bytes(16106127360)  → "15Gi"
    """
    if bytes == 0:
        return "0"

    for unit, factor in [
        ("Pi", 1024 ** 5),
        ("Ti", 1024 ** 4),
        ("Gi", 1024 ** 3),
        ("Mi", 1024 ** 2),
        ("Ki", 1024),
    ]:
        if bytes >= factor:
            value = bytes / factor
            # Show integer if exact, else 1 decimal place
            if value == int(value):
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"

    return str(bytes)


def format_bytes_human(bytes: int) -> str:
    """Format bytes as a human-readable string for UI display.

    Like format_bytes but uses familiar names (KB, MB, GB, TB).

    Examples:
        format_bytes_human(0)           → "0 B"
        format_bytes_human(524288)      → "512 KB"
        format_bytes_human(1073741824)  → "1.0 GB"
        format_bytes_human(16106127360) → "15.0 GB"
    """
    if bytes == 0:
        return "0 B"

    for unit, factor in [
        ("TB", 1024 ** 4),
        ("GB", 1024 ** 3),
        ("MB", 1024 ** 2),
        ("KB", 1024),
    ]:
        if bytes >= factor:
            value = bytes / factor
            if value == int(value):
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"

    return f"{bytes} B"
