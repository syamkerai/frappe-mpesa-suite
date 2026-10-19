"""Settle STK callbacks in a background job: verify, record, commit, then book.

The callback endpoint only stores Safaricom's payload on the Mpesa Express Request
(``callback_payload``, ``verification_status = Pending``) and queues ``settle``. Here:

1. STK Query for the same CheckoutRequestID must agree with the callback, and the paid
   amount must match the prompt (switch: Verify STK Callbacks with STK Query). Callbacks are
   unsigned, so this is what stops a forged "paid" callback.
2. Status, receipt and amount are written and committed before any booking, so a failure
   while booking can never lose the receipt.
3. Booking (Payment Request / invoice / Payment Entry) runs under a savepoint; on failure
   it is rolled back, logged, and retried by ``sweep`` (up to ``MAX_BOOKING_ATTEMPTS``).
"""

import json

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from ..daraja import verify
from .common import EXPRESS, enabled, log_match, parse_transtime, tolerance

MAX_BOOKING_ATTEMPTS = 3
GIVE_UP_AFTER_HOURS = 6  # STK Query answers for a while only; then a person decides


def queue(request_name: str) -> None:
    frappe.enqueue(
        "frappe_mpsa_payments.money_in.stk_verify.settle",
        queue="short",
        timeout=300,
        job_id=f"mpesa_stk_settle_{request_name}",
        deduplicate=True,
        enqueue_after_commit=True,
        request_name=request_name,
    )


def settle(request_name: str) -> str | None:
    """Verify and apply the stored callback; returns the outcome."""
    frappe.db.get_value(EXPRESS, request_name, "name", for_update=True)
    req = frappe.get_doc(EXPRESS, request_name)
    if req.get("verification_status") != verify.PENDING or not req.get("callback_payload"):
        return None

    parsed = verify.parse_stk_callback(json.loads(req.callback_payload))
    if enabled("verify_stk_callbacks"):
        try:
            from .stk_poll import stk_query

            query = stk_query(req)
        except Exception:
            query = None  # network trouble: ask again on the next sweep
        outcome, reason = verify.stk_verification(parsed, query, req.amount, max(tolerance(), 1.0))
    else:
        outcome = verify.VERIFIED if parsed["ok"] else verify.FAILED
        reason = "STK callback verification is off"

    if outcome == verify.PENDING:
        if get_datetime(req.creation) < add_to_date(now_datetime(), hours=-GIVE_UP_AFTER_HOURS):
            _set(req.name, {"verification_status": "Unverified", "result_desc": reason[:140]})
            log_match("Error", "STK callback", express=req, once=True,
                      details=f"Could not confirm the STK callback with Safaricom: {reason}. Check it by hand.")
        return outcome

    if outcome == verify.MISMATCH:
        _set(req.name, {"verification_status": verify.MISMATCH, "result_desc": reason[:140]})
        frappe.log_error(
            title=f"M-Pesa STK callback rejected for {req.name}"[:140],
            message=f"{reason}\n\nCallback:\n{req.callback_payload}",
        )
        log_match("Error", "STK callback", express=req, once=True, details=f"Not booked: {reason}")
        return outcome

    values = {
        "result_code": parsed["result_code"],
        "result_desc": (parsed["result_desc"] or "")[:140],
        "status": "Completed" if outcome == verify.VERIFIED else "Failed",
        "verification_status": verify.VERIFIED,
    }
    if outcome == verify.VERIFIED:
        values["transaction_id"] = parsed["receipt"]
        when = parse_transtime(parsed["transaction_date"])
        if when:
            values["transaction_date"] = when
        if parsed["amount"] is not None:
            values["amount"] = parsed["amount"]  # book what the customer actually paid
    _set(req.name, values)
    frappe.db.commit()  # the receipt is safe before any booking starts

    req.reload()
    _publish(req)
    if outcome == verify.VERIFIED:
        book(req.name)
    return outcome


def book(request_name: str) -> bool:
    """Book a verified, completed prompt; rolled back and retried by ``sweep`` on failure."""
    req = frappe.get_doc(EXPRESS, request_name)
    if req.status != "Completed" or not req.transaction_id or req.is_reconciled:
        return True
    frappe.db.set_value(
        EXPRESS, req.name, "booking_attempts", cint(req.get("booking_attempts")) + 1, update_modified=False
    )
    frappe.db.commit()

    frappe.db.savepoint("mpesa_stk_booking")
    try:
        req.validate_duplicate_c2b_records()  # the prompt wins over a C2B register for the receipt
        req.book_completed_payment()
    except Exception:
        frappe.db.rollback(save_point="mpesa_stk_booking")
        frappe.log_error(frappe.get_traceback(), f"M-Pesa STK booking failed for {req.name}"[:140])
        return False
    frappe.db.commit()
    return True


def sweep(limit: int = 50) -> None:
    """Scheduler: settle callbacks whose job was lost or whose query had no answer yet, and
    retry bookings that failed."""
    meta = frappe.get_meta(EXPRESS)
    if not meta.has_field("verification_status"):
        return
    since = add_to_date(now_datetime(), hours=-GIVE_UP_AFTER_HOURS - 1)
    pending = frappe.get_all(
        EXPRESS,
        filters={"verification_status": verify.PENDING, "modified": ["<", add_to_date(now_datetime(), minutes=-2)]},
        pluck="name",
        order_by="modified asc",
        limit=limit,
    )
    for name in pending:
        _safely(settle, name)

    unbooked = frappe.get_all(
        EXPRESS,
        filters={
            "verification_status": verify.VERIFIED,
            "status": "Completed",
            "is_reconciled": 0,
            "transaction_id": ["is", "set"],
            "booking_attempts": ["<", MAX_BOOKING_ATTEMPTS],
            "creation": [">=", since],
        },
        pluck="name",
        order_by="modified asc",
        limit=limit,
    )
    for name in unbooked:
        _safely(book, name)


def _safely(fn, name: str) -> None:
    try:
        fn(name)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"M-Pesa STK settle failed for {name}"[:140])


def _set(name: str, values: dict) -> None:
    # db.set_value, not save: saving runs the duplicate-C2B check outside the booking savepoint
    frappe.db.set_value(EXPRESS, name, values)
    frappe.publish_realtime(event="refresh_form", doctype=EXPRESS, docname=name)


def _publish(req) -> None:
    payload = {
        "status": req.status,
        "request_name": req.name,
        "reference_doctype": req.reference_doctype,
        "reference_name": req.reference_name,
        "transaction_id": req.transaction_id,
        "amount": req.amount,
        "result_desc": req.result_desc,
    }
    frappe.publish_realtime(event="mpesa_stk_payment_completed", message=payload, user=req.owner)
    frappe.publish_realtime(event="mpesa_stk_payment_completed", message=payload)
    if req.reference_doctype == "Payment Request" and "erpnext" in frappe.get_installed_apps():
        from ..frappe_mpsa_payments.api.m_pesa_api import publish_stk_status

        publish_stk_status(req.status, req.reference_name)
