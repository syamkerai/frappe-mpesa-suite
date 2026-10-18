"""Pure helpers for authenticating inbound Safaricom callbacks (no Frappe import)."""

import hmac
import ipaddress

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
