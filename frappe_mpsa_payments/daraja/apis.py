"""Which Daraja APIs an Mpesa Settings record may use, and its Party A (no Frappe import).

One shortcode can collect (Paybill / Till, STK) and pay out (B2C, B2B) when Safaricom set it up
as an M-PESA Business One Account, so a record has one switch per API instead of the single
upstream ``api_type``. Switches gate what the app *sends*; money Safaricom reports as received
is always recorded, whatever the switches say.

Transaction Status and Account Balance are read-only and have no switch: C2B and B2C
verification depend on Transaction Status.
"""

C2B = "use_c2b"
STK = "use_stk"
B2C = "use_b2c"
B2B = "use_b2b"
REVERSAL = "use_reversal"

ALL = (C2B, STK, B2C, B2B, REVERSAL)
COLLECTING = (C2B, STK)
PAYING = (B2C, B2B, REVERSAL)  # need an API operator (Initiator Name / Password)

LABELS = {
    C2B: "Receive C2B (Paybill / Till)",
    STK: "Send STK Prompts (M-Pesa Express)",
    B2C: "B2C Payouts",
    B2B: "B2B Supplier Payments",
    REVERSAL: "Reversals",
}

# Upstream single-purpose values of the old ``api_type`` Select.
LEGACY_EXPRESS = "MPesa Express"
LEGACY_C2B = "MPesa C2B (Customer to Business)"
LEGACY_B2C = "MPesa B2C (Business to Customer)"
LEGACY_API_TYPES = {LEGACY_EXPRESS: STK, LEGACY_C2B: C2B, LEGACY_B2C: B2C}


def _get(settings, field):
    if settings is None:
        return None
    if isinstance(settings, dict):
        return settings.get(field)
    getter = getattr(settings, "get", None)
    return getter(field) if callable(getter) else getattr(settings, field, None)


def _flag(value) -> bool:
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return False


def enabled(settings, api: str) -> bool:
    return _flag(_get(settings, api))


def enabled_apis(settings) -> list:
    return [api for api in ALL if enabled(settings, api)]


def collects(settings) -> bool:
    """True when the record receives money (C2B or STK); such records book receipts first."""
    return any(enabled(settings, api) for api in COLLECTING)


def describe(settings) -> str:
    names = [LABELS[api] for api in enabled_apis(settings)]
    return ", ".join(names) if names else "no APIs"


def refusal(settings, api: str, record: str | None = None) -> str | None:
    """Why ``api`` may not be used from this record, or None when it may."""
    if enabled(settings, api):
        return None
    name = record or _get(settings, "name") or "this record"
    return f"Mpesa Settings {name} does not have {LABELS[api]} ticked under APIs."


def require(settings, api: str) -> None:
    """Throw unless ``api`` is ticked. ``settings`` is an Mpesa Settings document, dict or name."""
    import frappe

    record = None
    if isinstance(settings, str):
        record = settings
        settings = frappe.db.get_value("Mpesa Settings", settings, list(ALL), as_dict=True)
        if not settings:
            frappe.throw(f"Mpesa Settings {record} not found")
    reason = refusal(settings, api, record)
    if reason:
        frappe.throw(reason)


def switches_from_legacy(
    api_type: str | None,
    *,
    registered_c2b: bool = False,
    sent_b2c: bool = False,
    sent_b2b: bool = False,
    sent_reversal: bool = False,
) -> dict:
    """Switch values for an existing record, from its old ``api_type`` and what it was used for.

    Upstream let any record do anything, so each API a record was already used for stays on:
    C2B URLs registered on an Express record keep C2B, a B2C payout from an Express record's
    Mode of Payment keeps B2C. Everything else starts off.
    """
    flags = {api: 0 for api in ALL}
    legacy = LEGACY_API_TYPES.get(str(api_type or "").strip())
    if legacy:
        flags[legacy] = 1
    if registered_c2b:
        flags[C2B] = 1
    if sent_b2c:
        flags[B2C] = 1
    if sent_b2b:
        flags[B2B] = 1
    if sent_reversal:
        flags[REVERSAL] = 1
    if not any(flags.values()):
        flags[STK] = 1  # blank api_type: upstream's default was MPesa Express
    return flags


def initiator_party_a(settings) -> str:
    """PartyA for the initiator APIs (B2C, B2B, Reversal, Transaction Status, Account Balance).

    Live: the Business Shortcode. Sandbox: the test shortcode (600xxx) kept in Till Number,
    because 174379 is for M-Pesa Express only (Daraja FAQ 1); with Till Number empty it falls
    back to the Business Shortcode. So one sandbox record can hold 174379 for STK and 600xxx
    for everything else.
    """
    shortcode = str(_get(settings, "business_shortcode") or "").strip()
    if _flag(_get(settings, "sandbox")):
        till = str(_get(settings, "till_number") or "").strip()
        if till:
            return till
    return shortcode
