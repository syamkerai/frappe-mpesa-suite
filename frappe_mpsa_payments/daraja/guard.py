"""Pure helpers for authenticating inbound Safaricom callbacks (no Frappe import)."""

import hmac
import ipaddress
import re

# Safaricom Daraja callback sources. The 12 IPs Daraja publishes (GettingStarted, "Callback
# and IP Whitelisting": 196.201.214.200/206/207/208, 196.201.213.44/114,
# 196.201.212.69/74/127/129/136/138) all sit in these /24s. Editable in M-Pesa Suite Settings.
DEFAULT_SAFARICOM_IPS = """196.201.212.0/24
196.201.213.0/24
196.201.214.0/24"""

# Daraja per-transaction limits (MpesaExpressSimulate FAQ 5, BusinessToCustomer FAQ 9).
STK_MIN_AMOUNT = 1
STK_MAX_AMOUNT = 250000
B2C_MIN_AMOUNT = 10
B2C_MAX_AMOUNT = 250000

# Daraja rejects callback URLs containing these words in any case (FAQ 19, C2B "URL
# Requirements"). Upstream endpoint names already in use are listed in daraja_api_reference.md.
BANNED_URL_WORDS = ("m-pesa", "mpesa", "safaricom", "exe", "exec", "cmd", "sql", "query")


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


def is_zero_code(value) -> bool:
    """True for Safaricom's "accepted" codes: 0, "0" and "00000000".

    The docs show ResponseCode "0", but the sandbox's C2B v2 registerurl answers
    "00000000" ("Success"), so an exact == "0" check calls a success a failure.
    """
    text = str(value if value is not None else "").strip()
    return bool(text) and set(text) == {"0"}


def callback_url_problems(url: str | None, sandbox=False) -> list:
    """Reasons Safaricom would refuse this callback URL; empty when it looks acceptable.

    Production URLs must be HTTPS (sandbox may use HTTP) and no URL may contain a banned
    word in any case (FAQ 19 and C2B "URL Requirements").
    """
    text = str(url or "").strip()
    if not text:
        return ["the callback URL is empty"]
    scheme, _, rest = text.partition("://")
    problems = []
    if not sandbox and scheme.lower() != "https":
        problems.append("production callback URLs must use HTTPS")
    found = [w for w in BANNED_URL_WORDS if w in rest.lower()]
    if found:
        problems.append("it contains words Safaricom refuses: " + ", ".join(found))
    return problems


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
    """STK Amount must be a whole number from 1 to 250,000; round half up, never send decimals."""
    from decimal import ROUND_HALF_UP, Decimal

    amount = int(Decimal(str(value or 0)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if amount < STK_MIN_AMOUNT:
        raise ValueError(f"STK amount must be at least {STK_MIN_AMOUNT}")
    if amount > STK_MAX_AMOUNT:
        raise ValueError(f"STK amount must be at most {STK_MAX_AMOUNT:,}")
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
