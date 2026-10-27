"""Pure helpers for Daraja Reversal, B2B and Transaction Status (no Frappe import).

Request bodies follow docs/daraja_api_reference.md sections 9, 11 and 13 field for
field, including Safaricom's own spellings (``RecieverIdentifierType``,
``Occassion`` on B2B). Result parsing tolerates the shape differences between
APIs: ResultCode is an int ``0`` (Reversal, Transaction Status), a string ``"0"``
(B2B) or a string like ``"R000002"``; ``ResultParameter`` may be a list, a single
dict, or absent.
"""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .guard import is_zero_code

REVERSAL_ENDPOINT = "/mpesa/reversal/v1/request"
B2B_ENDPOINT = "/mpesa/b2b/v1/paymentrequest"
TRANSACTION_STATUS_ENDPOINT = "/mpesa/transactionstatus/v1/query"

B2B_PAYBILL = "BusinessPayBill"
B2B_BUY_GOODS = "BusinessBuyGoods"
B2B_COMMANDS = (B2B_PAYBILL, B2B_BUY_GOODS)

# Reversal result codes (Daraja: Reversal)
REVERSAL_ALREADY_REVERSED = "R000001"

# Transaction Status ``TransactionStatus`` values that mean the money did not move.
# Anything other than these or "Completed" is left for a person to decide.
STATUS_FINAL_FAILED = ("failed", "declined", "cancelled", "canceled", "expired")


# ---------------------------------------------------------------- inputs
def whole_shillings(amount) -> int:
    """Daraja money APIs take whole shillings; refuse (never round) anything else."""
    try:
        value = Decimal(str(amount if amount not in (None, "") else "0"))
    except InvalidOperation:
        raise ValueError(f"Amount {amount!r} is not a number")
    if value <= 0:
        raise ValueError(f"Amount must be positive, got {amount}")
    if value != value.to_integral_value():
        raise ValueError(f"M-Pesa amounts must be whole shillings, got {amount}")
    return int(value)


def clamp_remarks(text, default: str = "Payment") -> str:
    """Remarks / Occasion: 2-100 characters."""
    value = " ".join(str(text or "").split())
    if len(value) < 2:
        value = default
    return value[:100]


def ack_accepted(data) -> bool:
    """Synchronous ack: ``ResponseCode "0"`` only means Safaricom queued the request."""
    return isinstance(data, dict) and is_zero_code(data.get("ResponseCode"))


# ---------------------------------------------------------------- request bodies
def reversal_payload(
    *,
    initiator: str,
    security_credential: str,
    transaction_id: str,
    amount,
    receiver_party: str,
    remarks: str,
    result_url: str,
    queue_timeout_url: str,
    occasion: str | None = None,
) -> dict:
    if not str(transaction_id or "").strip():
        raise ValueError("Reversal needs the original M-Pesa receipt (TransactionID)")
    payload = {
        "Initiator": initiator,
        "SecurityCredential": security_credential,
        "CommandID": "TransactionReversal",
        "TransactionID": str(transaction_id).strip(),
        "Amount": whole_shillings(amount),
        "ReceiverParty": str(receiver_party).strip(),
        "RecieverIdentifierType": "11",  # (sic) Daraja spelling
        "Remarks": clamp_remarks(remarks, "Reversal"),
        "QueueTimeOutURL": queue_timeout_url,
        "ResultURL": result_url,
    }
    if occasion:
        payload["Occasion"] = clamp_remarks(occasion, "Reversal")
    return payload


def b2b_payload(
    *,
    initiator: str,
    security_credential: str,
    command_id: str,
    amount,
    party_a: str,
    party_b: str,
    remarks: str,
    result_url: str,
    queue_timeout_url: str,
    account_reference: str | None = None,
    requester: str | None = None,
    occasion: str | None = None,
    fallback_reference: str | None = None,
) -> dict:
    """``fallback_reference`` fills AccountReference for BusinessBuyGoods when the till needs no
    account number: Daraja's BuyGoods body lists AccountReference without marking it optional."""
    if command_id not in B2B_COMMANDS:
        raise ValueError(f"B2B CommandID must be one of {B2B_COMMANDS}, got {command_id!r}")
    if not str(party_b or "").strip().isdigit():
        raise ValueError(f"B2B receiver (PartyB) must be a paybill or till number, got {party_b!r}")
    reference = str(account_reference or "").strip()
    if command_id == B2B_PAYBILL and not reference:
        raise ValueError("BusinessPayBill needs an AccountReference (the supplier's account number)")
    payload = {
        "Initiator": initiator,
        "SecurityCredential": security_credential,
        "CommandID": command_id,
        "SenderIdentifierType": "4",
        "RecieverIdentifierType": "4",  # (sic) Daraja spelling
        "Amount": whole_shillings(amount),
        "PartyA": str(party_a).strip(),
        "PartyB": str(party_b).strip(),
        "AccountReference": reference or str(fallback_reference or "").strip() or "Payment",
        "Remarks": clamp_remarks(remarks, "Supplier payment"),
        "QueueTimeOutURL": queue_timeout_url,
        "ResultURL": result_url,
    }
    if requester:
        payload["Requester"] = str(requester).strip()
    if occasion:
        payload["Occassion"] = clamp_remarks(occasion, "Payment")  # (sic) Daraja spelling
    return payload


def transaction_status_payload(
    *,
    initiator: str,
    security_credential: str,
    party_a: str,
    result_url: str,
    queue_timeout_url: str,
    transaction_id: str | None = None,
    original_conversation_id: str | None = None,
    identifier_type: str = "4",
    remarks: str = "Status check",
    occasion: str | None = None,
) -> dict:
    """Query by receipt (``TransactionID``) when known, else by ``OriginalConversationID``."""
    if not (transaction_id or original_conversation_id):
        raise ValueError("Transaction Status needs a TransactionID or OriginalConversationID")
    payload = {
        "Initiator": initiator,
        "SecurityCredential": security_credential,
        "CommandID": "TransactionStatusQuery",
        "PartyA": str(party_a).strip(),
        "IdentifierType": str(identifier_type),
        "ResultURL": result_url,
        "QueueTimeOutURL": queue_timeout_url,
        "Remarks": clamp_remarks(remarks, "Status check"),
        "Occasion": clamp_remarks(occasion or remarks, "Status check"),
    }
    if transaction_id:
        payload["TransactionID"] = str(transaction_id).strip()
    else:
        payload["OriginalConversationID"] = str(original_conversation_id).strip()
    return payload


# ---------------------------------------------------------------- results
def result_code(result: dict) -> str:
    return str((result or {}).get("ResultCode", "")).strip()


def result_ok(result: dict) -> bool:
    return result_code(result) == "0"


def result_items(result: dict) -> list:
    """``Result.ResultParameters.ResultParameter`` as a list of {Key, Value} dicts."""
    holder = (result or {}).get("ResultParameters") if isinstance(result, dict) else None
    params = (holder.get("ResultParameter") if isinstance(holder, dict) else None) or []
    if isinstance(params, dict):
        params = [params]
    if not isinstance(params, list):
        return []
    return [p for p in params if isinstance(p, dict) and isinstance(p.get("Key"), str) and p["Key"]]


def result_values(result: dict) -> dict:
    """Key -> Value. The first occurrence wins (Transaction Status repeats DebitPartyName)."""
    out = {}
    for item in result_items(result):
        out.setdefault(item["Key"], item.get("Value"))
    return out


def parse_daraja_datetime(value):
    """Daraja uses ``DD.MM.YYYY HH:MM:SS`` (B2C/B2B/Reversal) and ``YYYYMMDDHHMMSS``
    (Transaction Status, sometimes as an int). Returns a naive datetime or None."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_number(value):
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def basic_amount(text) -> float | None:
    """The amount of Safaricom's "{Amount={CurrencyCode=KES, MinimumAmount=189400, BasicAmount=1894.00}}"
    (the only balance a Buy Goods Result gives), or None."""
    match = re.search(r"BasicAmount\s*=\s*(-?[\d,]+(?:\.\d+)?)", str(text or ""))
    return float(match.group(1).replace(",", "")) if match else None


def b2b_balance_text(value, account: str = "Working Account"):
    """A B2B Result's balance as "<account>|KES|<current>|<available>", the form the wallet reads.
    The "{Amount=...}" form names no account: B2B always pays from Working (MMF). Anything else,
    and an "{Amount=...}" with no readable amount, is kept as Safaricom sent it."""
    text = str(value or "").strip()
    amount = basic_amount(text) if "{" in text else None
    if amount is None:
        return value
    currency = re.search(r"CurrencyCode\s*=\s*([A-Za-z]{3})", text)
    return f"{account}|{currency.group(1).upper() if currency else 'KES'}|{amount:.2f}|{amount:.2f}"


def reversal_outcome(result: dict) -> str:
    code = result_code(result)
    if code == "0":
        return "Reversed"
    if code == REVERSAL_ALREADY_REVERSED:
        return "Already Reversed"
    return "Failed"


def b2b_outcome(result: dict) -> str:
    return "Paid" if result_ok(result) else "Failed"


def b2c_status_outcome(result: dict, expected_amount=None) -> tuple[str, str]:
    """Decide what a Transaction Status result means for a B2C payout.

    Returns ``(outcome, reason)`` where outcome is ``Paid``, ``Failed`` or ``Unresolved``.
    Only a successful query that names a final state settles the payout; a failed
    query ("not found" right after sending, bad credential, ...) settles nothing.
    """
    if not result_ok(result):
        return "Unresolved", f"Query failed: {result_code(result)} {(result or {}).get('ResultDesc') or ''}".strip()
    values = result_values(result)
    state = str(values.get("TransactionStatus") or "").strip()
    if state.lower() == "completed":
        if expected_amount not in (None, ""):
            got = to_number(values.get("Amount"))
            if got is None or abs(got - float(expected_amount)) > 0.001:
                return "Unresolved", f"Completed but amount {values.get('Amount')} != {expected_amount}"
        if not values.get("ReceiptNo"):
            return "Unresolved", "Completed but no ReceiptNo"
        return "Paid", "Completed"
    if state.lower() in STATUS_FINAL_FAILED:
        return "Failed", state
    return "Unresolved", f"TransactionStatus {state or 'missing'}"
