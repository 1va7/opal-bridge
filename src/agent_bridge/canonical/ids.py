"""ID and timestamp utilities.

UUIDv7 generator (Python stdlib doesn't have one yet — pydantic isn't
needed, just a small helper). See RFC 9562 Section 5.7.
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone


def uuid7_str() -> str:
    """Generate a UUIDv7 (millisecond timestamp + random)."""
    ms = int(time.time() * 1000)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    val = (ms & 0xFFFFFFFFFFFF) << 80
    val |= 0x7 << 76
    val |= rand_a << 64
    val |= 0b10 << 62
    val |= rand_b & ((1 << 62) - 1)
    h = f"{val:032x}"
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def iso_now_z() -> str:
    """ISO 8601 with millisecond precision and Z suffix (UTC)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def parse_cc_iso(s: str) -> datetime:
    """Parse CC's ISO 8601 timestamps (always UTC, with Z)."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_iso_z(dt: datetime) -> str:
    """Format a datetime as ISO 8601 with millisecond + Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def add_ms(ts_iso: str, ms: int) -> str:
    """Add ms milliseconds to an ISO 8601 timestamp."""
    from datetime import timedelta
    dt = parse_cc_iso(ts_iso)
    return fmt_iso_z(dt + timedelta(milliseconds=ms))
