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
# Source: Communications Authority of Kenya numbering plan (the 011X block is Safaricom's, 010X
# Airtel's), and Safaricom's 014X / 018X lines of August 2026 (0140-0143, 0180-0182).
_NETWORK_PREFIXES = {
    "Safaricom": (
        "70", "710", "711", "712", "713", "714", "715", "716", "717", "718", "719",
        "720", "721", "722", "723", "724", "725", "726", "727", "728", "729",
        "740", "741", "742", "743", "745", "746", "748", "757", "758", "759",
        "768", "769", "79", "11", "140", "141", "142", "143", "180", "181", "182",
    ),
    "Airtel": (
        "730", "731", "732", "733", "734", "735", "736", "737", "738", "739",
        "750", "751", "752", "753", "754", "755", "756", "762", "780", "781",
        "782", "783", "784", "785", "786", "787", "788", "789", "10",
    ),
    "Telkom": ("770", "771", "772", "773", "774", "775", "776", "777", "778", "779"),
    "Equitel": ("763", "764", "765", "766"),
    "Faiba": ("747",),
}

# The mobile blocks in use: 07XX, 010X / 011X, 0140-0143 and 0180-0182. Every pay path (claim
# payee lines, pasted payees, payee accounts, B2C rows) and the Employee's Cell Number take the same
# blocks, so a claim never approves a number its payout refuses; a block is added here (and to
# _NETWORK_PREFIXES and FORMATS) when it is opened.
_IN_USE = re.compile(r"254(?:(?:7\d|1[01])\d{7}|(?:14[0-3]|18[0-2])\d{6})")

# What a user is told to type
FORMATS = ("07XX XXX XXX or 01XX XXX XXX (010X, 011X, 0140-0143, 0180-0182), or the 12 digits "
           "2547... / 2541...")


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


def repaired_msisdn(value) -> str | None:
    """normalize_msisdn, or the same for a number written with a prefix too many: the 0 kept after
    254 ('+254 0712...'), or the 00 / 0 of international dialling ('00254 712...', '0254712...').
    For what a person typed (STK, a Cell Number); normalize_msisdn itself stays strict, as the C2B
    hash and matching use it."""
    msisdn = normalize_msisdn(value)
    if msisdn or "e+" in str(value or "").lower():
        return msisdn
    num = digits(value)
    for prefix in ("002540", "00254", "02540", "0254", "2540"):
        if num.startswith(prefix) and len(num) == len(prefix) + 9:
            return normalize_msisdn("254" + num[len(prefix):])
    return None


def payable_msisdn(value) -> str | None:
    """normalize_msisdn, for a number in a mobile block in use (_IN_USE) only: what B2C may pay."""
    msisdn = normalize_msisdn(value)
    return msisdn if msisdn and _IN_USE.fullmatch(msisdn) else None


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


def shown(value) -> str:
    """A number for a message: masked when it is a phone; else its last 3 digits and how many digits
    it has ('ending 453 (11 digits)'), so a mistyped number is told apart without being shown."""
    value = str(value or "").strip()
    if normalize_msisdn(value):
        return mask(value)
    num = digits(value)
    return f"ending {num[-3:]} ({len(num)} digits)" if num else repr(value)
