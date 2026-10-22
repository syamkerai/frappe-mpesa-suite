"""Kenyan mobile number helpers shared by every M-Pesa flow.

One rule for all of C2B, STK, B2C and the external API, replacing the three
slightly different normalisers that existed before (STK wanted 07..., B2C
2547..., casual payouts 25410/25411...). Pure Python: no Frappe import, so it
is unit-testable outside a bench.
"""

import hashlib
import re

_NON_DIGITS = re.compile(r"\D")

# Safaricom, Airtel, Telkom, Equitel, Faiba prefixes (after the leading 0 / 254).
# Source: Communications Authority of Kenya numbering plan.
_NETWORK_PREFIXES = {
    "Safaricom": (
        "70", "710", "711", "712", "713", "714", "715", "716", "717", "718", "719",
        "720", "721", "722", "723", "724", "725", "726", "727", "728", "729",
        "740", "741", "742", "743", "745", "746", "748", "757", "758", "759",
        "768", "769", "79", "110", "111", "112", "113", "114", "115",
    ),
    "Airtel": (
        "730", "731", "732", "733", "734", "735", "736", "737", "738", "739",
        "750", "751", "752", "753", "754", "755", "756", "762", "780", "781",
        "782", "783", "784", "785", "786", "787", "788", "789", "100", "101", "102",
    ),
    "Telkom": ("770", "771", "772", "773", "774", "775", "776", "777", "778", "779"),
    "Equitel": ("763", "764", "765", "766"),
    "Faiba": ("747",),
}


def digits(value) -> str:
    return _NON_DIGITS.sub("", str(value or ""))


def normalize_msisdn(value) -> str | None:
    """Return the number as 2547XXXXXXXX / 2541XXXXXXXX, or None if not a Kenyan mobile.

    Accepts 07.., 01.., 7.., 1.., 2547.., +254 7.., with any spaces/dashes.
    Excel scientific notation ("2.54712E+11") is rejected, not guessed.
    """
    raw = str(value or "").strip()
    if "e+" in raw.lower():
        return None
    num = digits(raw)
    if num.startswith("254") and len(num) == 12:
        local = num[3:]
    elif num.startswith("0") and len(num) == 10:
        local = num[1:]
    elif len(num) == 9:
        local = num
    else:
        return None
    if local[0] not in "71":
        return None
    return "254" + local


def to_local(value) -> str | None:
    """07XXXXXXXX form (what some Daraja products and SMS templates expect)."""
    msisdn = normalize_msisdn(value)
    return "0" + msisdn[3:] if msisdn else None


def msisdn_sha256(value) -> str | None:
    """Safaricom sends C2B MSISDN as SHA-256 of the 2547XXXXXXXX string."""
    msisdn = normalize_msisdn(value)
    return hashlib.sha256(msisdn.encode()).hexdigest() if msisdn else None


def looks_like_sha256(value) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "")))


def network_of(value) -> str | None:
    """Mobile network for the number, longest prefix wins; None if unknown/invalid."""
    msisdn = normalize_msisdn(value)
    if not msisdn:
        return None
    local = msisdn[3:]
    best = None
    for network, prefixes in _NETWORK_PREFIXES.items():
        for p in prefixes:
            if local.startswith(p) and (best is None or len(p) > best[1]):
                best = (network, len(p))
    return best[0] if best else None


def is_safaricom(value) -> bool:
    return network_of(value) == "Safaricom"


def mask(value) -> str:
    """2547****5678 — for messages, comments and logs."""
    msisdn = normalize_msisdn(value)
    if not msisdn:
        return "***"
    return msisdn[:4] + "****" + msisdn[-4:]
