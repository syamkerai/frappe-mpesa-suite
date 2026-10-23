"""Decide whether a Safaricom callback is real money (no Frappe import; unit-tested).

Callbacks are not signed, so a C2B confirmation is only booked after a Transaction Status
query names the same receipt, amount and shortcode, and an STK callback only after an STK
Query for the same CheckoutRequestID agrees with it (docs/daraja_api_reference.md §2, §11).
"""

from .money_out import result_code, result_ok, result_values, to_number

VERIFIED = "Verified"  # callback confirmed: book it
FAILED = "Failed"  # STK: callback and query both say the prompt failed
PENDING = "Pending"  # no final answer yet: ask again later
MISMATCH = "Mismatch"  # Safaricom contradicts the callback: never book, tell a person

# STK Query ResultCode meaning "still being processed"
STK_PENDING_CODES = {"4999"}


def _code(value) -> str:
    text = str(value if value is not None else "").strip()
    return str(int(text)) if text.isdigit() else text


def _amount_differs(got, expected, tolerance: float) -> bool:
    return got is None or abs(float(got) - float(expected)) > float(tolerance)


# ---------------------------------------------------------------- STK
def parse_stk_callback(stk_callback: dict) -> dict:
    """Body.stkCallback -> flat dict. CallbackMetadata exists only on success; items may lack Value."""
    cb = stk_callback or {}
    items = (cb.get("CallbackMetadata") or {}).get("Item") or []
    if isinstance(items, dict):
        items = [items]
    meta = {i.get("Name"): i.get("Value") for i in items if isinstance(i, dict) and "Value" in i}
    code = _code(cb.get("ResultCode"))
    return {
        "merchant_request_id": cb.get("MerchantRequestID"),
        "checkout_request_id": cb.get("CheckoutRequestID"),
        "result_code": code,
        "result_desc": cb.get("ResultDesc"),
        "ok": code == "0",
        "receipt": str(meta.get("MpesaReceiptNumber") or "").strip() or None,
        "amount": to_number(meta.get("Amount")) if meta.get("Amount") is not None else None,
        "transaction_date": str(meta.get("TransactionDate") or "") or None,
        "phone": str(meta.get("PhoneNumber") or "") or None,
    }


def stk_verification(parsed: dict, query: dict | None, expected_amount, tolerance: float = 1.0) -> tuple:
    """Compare a parsed STK callback with an STK Query response. Returns (outcome, reason)."""
    if not isinstance(query, dict) or query.get("ResultCode") in (None, ""):
        reason = (query or {}).get("errorMessage") if isinstance(query, dict) else None
        return PENDING, f"STK Query has no final answer yet{': ' + reason if reason else ''}"
    qcode = _code(query.get("ResultCode"))
    if qcode in STK_PENDING_CODES:
        return PENDING, f"STK Query: {qcode} {query.get('ResultDesc') or ''}".strip()

    if parsed.get("ok"):
        if qcode != "0":
            return MISMATCH, f"Callback says paid but STK Query says {qcode} {query.get('ResultDesc') or ''}".strip()
        if not parsed.get("receipt"):
            return MISMATCH, "Callback says paid but carries no M-Pesa receipt"
        if expected_amount not in (None, "") and _amount_differs(parsed.get("amount"), expected_amount, tolerance):
            return MISMATCH, f"Callback amount {parsed.get('amount')} does not match the prompt ({expected_amount})"
        return VERIFIED, "Confirmed by STK Query"

    if qcode == "0":
        return MISMATCH, f"Callback says failed ({parsed.get('result_code')}) but STK Query says paid"
    return FAILED, f"Confirmed by STK Query ({qcode})"


# ---------------------------------------------------------------- C2B
def credit_shortcode(credit_party_name) -> str:
    """Transaction Status CreditPartyName "600000 - Company Ltd" -> "600000"."""
    return str(credit_party_name or "").split("-", 1)[0].strip()


def c2b_verification(confirmation: dict, result: dict, own_shortcodes, tolerance: float = 0.5) -> tuple:
    """Compare a C2B confirmation with a Transaction Status Result. Returns (outcome, reason).

    ``own_shortcodes``: the paybill/till/store numbers of the Mpesa Settings the confirmation
    claims to be for. A failed query is PENDING (asked again, then held for a person);
    a successful query that disagrees is MISMATCH.
    """
    own = {str(s).strip() for s in (own_shortcodes or []) if str(s or "").strip()}
    claimed = str(confirmation.get("businessshortcode") or "").strip()
    if not claimed or claimed not in own:
        return MISMATCH, f"BusinessShortCode {claimed or '(none)'} is not one of ours"

    if not result_ok(result):
        return PENDING, f"Transaction Status failed: {result_code(result)} {(result or {}).get('ResultDesc') or ''}".strip()

    values = result_values(result)
    receipt = str(confirmation.get("transid") or "").strip().upper()
    got_receipt = str(values.get("ReceiptNo") or "").strip().upper()
    if got_receipt != receipt:
        return MISMATCH, f"Transaction Status is for receipt {got_receipt or '(none)'}, not {receipt}"

    state = str(values.get("TransactionStatus") or "").strip()
    if state.lower() != "completed":
        return MISMATCH, f"Receipt {receipt} has status {state or '(none)'}"

    if _amount_differs(to_number(values.get("Amount")), confirmation.get("transamount") or 0, tolerance):
        return MISMATCH, f"Safaricom amount {values.get('Amount')} != confirmation {confirmation.get('transamount')}"

    # fail closed: a result that does not name the shortcode credited proves nothing about it
    credited = credit_shortcode(values.get("CreditPartyName"))
    if not credited or credited not in own:
        return MISMATCH, f"Receipt {receipt} was paid to {credited or '(no CreditPartyName)'}, not {claimed}"

    return VERIFIED, "Confirmed by Transaction Status"
