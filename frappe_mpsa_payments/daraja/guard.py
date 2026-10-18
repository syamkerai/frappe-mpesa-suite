"""Pure helpers for authenticating inbound Safaricom callbacks (no Frappe import)."""

import hmac
import ipaddress
import re

# Safaricom Daraja callback sources. The published callback IPs (196.201.214.200/206/207/208,
# 196.201.213.44/114, 196.201.212.69/74/127/129/136/138) all sit in these /24s. Editable in
# M-Pesa Suite Settings; confirm against your go-live pack before enforcing.
DEFAULT_SAFARICOM_IPS = """196.201.212.0/24
196.201.213.0/24
196.201.214.0/24"""


def parse_allowlist(text: str | None) -> list:
    nets = []
    for line in (text or "").replace(",", "\n").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            nets.append(ipaddress.ip_network(line, strict=False))
        except ValueError:
            continue
    return nets


def ip_allowed(ip: str | None, allowlist_text: str | None) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr in net for net in parse_allowlist(allowlist_text))


def token_matches(given: str | None, expected: str | None) -> bool:
    if not given or not expected:
        return False
    return hmac.compare_digest(str(given).encode(), str(expected).encode())


def redact_headers(headers: dict | None) -> dict:
    """Never persist credentials into Integration Request logs."""
    out = {}
    for k, v in (headers or {}).items():
        if k.lower() in ("authorization", "proxy-authorization", "x-api-key", "cookie"):
            scheme = str(v).split(" ", 1)[0] if " " in str(v) else ""
            out[k] = f"{scheme} ***".strip()
        else:
            out[k] = v
    return out


def truncate_reference(value, limit: int = 12) -> str:
    """Daraja documents AccountReference <= 12 chars and TransactionDesc <= 13.

    Production accepts longer AccountReference values in practice, and Astral's order
    matching relies on the full "ACC-PRQ-…" name coming back in C2B, so only apply this
    to TransactionDesc unless a shortcode is known to enforce the limit.
    """
    text = str(value or "").strip() or "Payment"
    return text[:limit]


def stk_amount(value) -> int:
    """STK Amount must be a whole number >= 1; round half up, never send decimals."""
    from decimal import ROUND_HALF_UP, Decimal

    amount = int(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if amount < 1:
        raise ValueError("STK amount must be at least 1")
    return amount


def daraja_timestamp(now=None) -> str:
    """Daraja ``Timestamp`` (YYYYMMDDHHmmss) in Africa/Nairobi, whatever the server's TZ.

    Safaricom validates the STK password against this value in EAT; a UTC server clock
    shifts it by three hours. Falls back to a fixed UTC+3 offset when tzdata is missing.
    """
    from datetime import datetime, timedelta, timezone

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Africa/Nairobi")
    except Exception:
        tz = timezone(timedelta(hours=3))
    return (now or datetime.now(tz)).astimezone(tz).strftime("%Y%m%d%H%M%S")


_SECRET_KEYS = ("password", "securitycredential", "secret", "authorization", "passkey")
_URL_TOKEN = re.compile(r"([?&]t=)[^&#]*")


def scrub_payload(payload):
    """Copy of a Daraja request body safe to store in an Integration Request.

    Masks Password / SecurityCredential / secrets / Authorization and the ``t=`` callback
    token in any URL (CallBackURL, ResultURL, QueueTimeOutURL, ConfirmationURL, ...).
    """
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if any(s in str(key).lower().replace("_", "") for s in _SECRET_KEYS):
                out[key] = "****" if value not in (None, "") else value
            else:
                out[key] = scrub_payload(value)
        return out
    if isinstance(payload, (list, tuple)):
        return [scrub_payload(v) for v in payload]
    if isinstance(payload, str) and "t=" in payload:
        return _URL_TOKEN.sub(r"\1***", payload)
    return payload
