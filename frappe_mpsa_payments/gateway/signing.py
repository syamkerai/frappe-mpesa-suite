"""Webhook signatures (pure Python, no Frappe import; copy into client code freely).

Header:  X-Mpesa-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>

Receivers recompute the HMAC over the raw request body and reject stale timestamps,
which stops both tampering and replay.
"""

import hashlib
import hmac
import time

HEADER = "X-Mpesa-Signature"
DEFAULT_TOLERANCE = 300  # seconds


def sign(secret: str, body: str | bytes, timestamp: int | None = None) -> str:
    ts = int(timestamp if timestamp is not None else time.time())
    raw = body.encode() if isinstance(body, str) else body
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify(secret: str, body: str | bytes, header: str | None, tolerance: int = DEFAULT_TOLERANCE, now: int | None = None) -> bool:
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - ts) > tolerance:
        return False
    expected = sign(secret, body, ts).split("v1=", 1)[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))
