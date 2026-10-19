"""Daraja Transaction Reversal for a received C2B payment (docs/daraja_api_reference.md §13)."""

import frappe
from frappe.utils import flt

from ..daraja import money_out as daraja
from ..utils.doctype_names import MPESA_C2B_PAYMENT_REGISTER_DOCTYPE
from .common import apply_ack, callback_url, initiator_settings, lock_for_send, log_callback, post
from .results import find_request

REVERSAL_DOCTYPE = "Mpesa Reversal Request"
FINAL = ("Reversed", "Already Reversed", "Failed")
# A register may have only one reversal in these states.
LIVE = ("Not Initiated", "Initiated", "Unknown", "Reversed", "Already Reversed")


def send_reversal(name: str) -> None:
    doc = lock_for_send(REVERSAL_DOCTYPE, name)
    settings = initiator_settings(doc.mpesa_settings)
    payload = daraja.reversal_payload(
        initiator=settings.initiator_name,
        security_credential=settings.security_credential,
        transaction_id=doc.transaction_id,
        amount=doc.amount,
        receiver_party=settings.business_shortcode,
        remarks=doc.remarks,
        result_url=callback_url("reversal_result", ref=doc.name),
        queue_timeout_url=callback_url("reversal_queue_timeout", ref=doc.name),
    )
    # Nothing below may raise: the request has (maybe) left the building.
    data, error = post(
        settings,
        daraja.REVERSAL_ENDPOINT,
        payload,
        doctype=REVERSAL_DOCTYPE,
        name=doc.name,
        description="Mpesa Reversal",
    )
    apply_ack(doc, data, error)


def handle_result(result: dict, ref: str | None) -> None:
    doc = find_request(REVERSAL_DOCTYPE, result, ref)
    if not doc:
        log_callback({"Result": result}, "Mpesa Reversal Result (unmatched)", REVERSAL_DOCTYPE, ref, "Failed")
        return
    if doc.status in FINAL and doc.result_code not in (None, ""):
        return  # Safaricom re-sent a result we already recorded

    values = daraja.result_values(result)
    outcome = daraja.reversal_outcome(result)
    doc.db_set(
        {
            "status": outcome,
            "result_code": daraja.result_code(result),
            "result_desc": result.get("ResultDesc"),
            "conversation_id": doc.conversation_id or result.get("ConversationID"),
            "originator_conversation_id": doc.originator_conversation_id
            or result.get("OriginatorConversationID"),
            "reversal_transaction_id": result.get("TransactionID"),
            "trans_completed_time": daraja.parse_daraja_datetime(values.get("TransCompletedTime")),
            "charge": flt(daraja.to_number(values.get("Charge")) or 0),
            "debit_party_public_name": values.get("DebitPartyPublicName"),
            "credit_party_public_name": values.get("CreditPartyPublicName"),
        },
        notify=True,
    )
    log_callback({"Result": result}, "Mpesa Reversal Result", REVERSAL_DOCTYPE, doc.name, "Completed")

    if outcome in ("Reversed", "Already Reversed") and doc.c2b_register:
        register = frappe.get_doc(MPESA_C2B_PAYMENT_REGISTER_DOCTYPE, doc.c2b_register)
        register.add_comment(
            "Comment",
            f"M-Pesa reversal {doc.name}: {outcome} ({result.get('ResultDesc') or ''}). "
            "Any Payment Entry booked from this receipt must be cancelled or reversed by hand.",
        )


def handle_queue_timeout(payload: dict, ref: str | None) -> None:
    """The request timed out in Safaricom's queue: it may still complete. Flag, never settle."""
    if ref and frappe.db.exists(REVERSAL_DOCTYPE, ref):
        frappe.db.set_value(REVERSAL_DOCTYPE, ref, "queue_timed_out", 1)
    log_callback(payload, "Mpesa Reversal Queue Timeout", REVERSAL_DOCTYPE, ref, "Failed")


def validate_request(doc) -> None:
    """Checks for a draft Mpesa Reversal Request (called from the doctype's validate)."""
    from ..money_in.common import settings_for_shortcode

    register = frappe.db.get_value(
        MPESA_C2B_PAYMENT_REGISTER_DOCTYPE,
        doc.c2b_register,
        ["transid", "transamount", "businessshortcode", "docstatus"],
        as_dict=True,
    )
    if not register:
        frappe.throw(f"C2B register {doc.c2b_register} not found")
    if register.docstatus == 2:
        frappe.throw(f"C2B register {doc.c2b_register} is cancelled")
    if not register.transid:
        frappe.throw(f"C2B register {doc.c2b_register} has no M-Pesa receipt to reverse")

    doc.transaction_id = register.transid
    doc.original_amount = flt(register.transamount)
    if not doc.amount:
        doc.amount = doc.original_amount
    try:
        daraja.whole_shillings(doc.amount)
    except ValueError as e:
        frappe.throw(str(e))
    if flt(doc.amount) != flt(doc.original_amount):
        frappe.throw(
            f"A reversal returns the whole payment: amount must be {doc.original_amount}, not {doc.amount}"
        )

    if not doc.mpesa_settings:
        doc.mpesa_settings = settings_for_shortcode(register.businessshortcode)
    if not doc.mpesa_settings:
        frappe.throw(f"No Mpesa Settings found for shortcode {register.businessshortcode}; pick one")

    other = frappe.db.get_value(
        REVERSAL_DOCTYPE,
        {
            "c2b_register": doc.c2b_register,
            "name": ["!=", doc.name or ""],
            "docstatus": ["<", 2],
            "status": ["in", LIVE],
        },
        "name",
        order_by="creation asc",
    )
    if other:
        frappe.throw(f"Reversal {other} already exists for {doc.c2b_register}")
