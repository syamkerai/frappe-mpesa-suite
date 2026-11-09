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
from .common import EXPRESS, REGISTER, as_user, enabled, log_match, parse_transtime, receipt_reversal, tolerance

MAX_BOOKING_ATTEMPTS = 3
GIVE_UP_AFTER_HOURS = 6  # STK Query answers for a while only; then a person decides


def queue(request_name: str) -> None:
    frappe.enqueue(
        "mpsa_suite.money_in.stk_verify.settle",
        queue="short",
        timeout=300,
        job_id=f"mpesa_stk_settle_{request_name}",
        deduplicate=True,
        enqueue_after_commit=True,
        request_name=request_name,
    )


def settle(request_name: str) -> str | None:
    """Verify and apply the stored callback; returns the outcome."""
    # locking read: the receipt as committed now (the matcher may have just adopted the prompt
    # for a C2B receipt), which this transaction's snapshot could still miss
    receipt_on_file = frappe.db.get_value(EXPRESS, request_name, "transaction_id", for_update=True)
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
        # from the latest prompt: a re-prompt reuses the request (stk_poll.prompted_between)
        sent = get_datetime(req.get("timestamp") or req.creation)
        if sent < add_to_date(now_datetime(), hours=-GIVE_UP_AFTER_HOURS):
            _set(req.name, {"verification_status": "Unverified", "result_desc": reason[:140]})
            log_match("Error", "STK Callback", express=req, once=True,
                      details=f"Could not confirm the STK callback with Safaricom: {reason}. Check it by hand.")
        return outcome

    if outcome == verify.MISMATCH:
        _set(req.name, {"verification_status": verify.MISMATCH, "result_desc": reason[:140]})
        frappe.log_error(
            title=f"M-Pesa STK callback rejected for {req.name}"[:140],
            message=f"{reason}\n\nCallback:\n{req.callback_payload}",
        )
        log_match("Error", "STK Callback", express=req, once=True, details=f"Not booked: {reason}")
        return outcome

    if receipt_on_file and not (outcome == verify.VERIFIED and _same_receipt(parsed["receipt"], receipt_on_file)):
        return _already_matched(req, parsed, outcome, receipt_on_file)
    adopted = outcome == verify.VERIFIED and not receipt_on_file and _adopted_receipt(req, parsed["receipt"])
    if adopted:
        return _already_matched(req, parsed, outcome, adopted)

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


def book(request_name: str, commit: bool = True) -> bool:
    """Book a verified, completed prompt; rolled back and retried by ``sweep`` on failure.

    ``commit=False``: inside a caller's transaction (the prepaid-order flow books a released
    order's prompts in its own run), which commits or rolls back as a whole.
    """
    req = frappe.get_doc(EXPRESS, request_name)
    if req.status != "Completed" or not req.transaction_id or req.is_reconciled:
        return True
    reversal = receipt_reversal(req.transaction_id)
    if reversal:
        # Not an attempt: sweep asks again while the prompt is recent, so a reversal that
        # ends Failed (the money stayed with us) still gets the receipt booked.
        _held_by_reversal(req, reversal)
        return False

    order = draft_order_waiting(req)
    if order:
        # ERPNext books no payment on a draft order. Not an attempt: the prepaid-order flow
        # counts this prompt toward the order, releases the order through its Workflow once it
        # is paid in full (as the approver user), then books the prompt (this function again,
        # the order now submitted). A partial payment waits for the rest.
        from . import sales_order

        sales_order.process_order(order)
        if commit:
            frappe.db.commit()
        return bool(cint(frappe.db.get_value(EXPRESS, req.name, "is_reconciled")))

    repoint_superseded(req)
    attempts = cint(req.get("booking_attempts"))
    frappe.db.set_value(EXPRESS, req.name, "booking_attempts", attempts + 1, update_modified=False)
    if commit:
        frappe.db.commit()

    # Again, with a locking read, in the transaction that books: a reversal being submitted
    # right now holds its row until Safaricom answers and its transaction commits, unseen by
    # the plain read above; this waits for it. A reversal submitted after this read waits for
    # the booking to commit.
    reversal = receipt_reversal(req.transaction_id, for_update=True)
    if reversal:
        frappe.db.set_value(EXPRESS, req.name, "booking_attempts", attempts, update_modified=False)
        _held_by_reversal(req, reversal)
        if commit:
            frappe.db.commit()
        return False

    frappe.db.savepoint("mpesa_stk_booking")
    try:
        # As Administrator: this job runs as the callback's Integration User, and ERPNext's
        # get_payment_entry and an invoice's submit check that user's permission on the order or
        # invoice (a User Permission on Project, Cost Center or Company hides it). The prompt was
        # verified with Safaricom above; staff reconciling by hand keep their own permissions.
        with as_user("Administrator"):
            req.validate_duplicate_c2b_records()  # the prompt wins over a C2B register for the receipt
            req.book_completed_payment()
    except Exception:
        frappe.db.rollback(save_point="mpesa_stk_booking")
        frappe.log_error(frappe.get_traceback(), f"M-Pesa STK booking failed for {req.name}"[:140])
        return False
    if commit:
        frappe.db.commit()
    return True


def draft_order_waiting(req) -> str | None:
    """The draft Sales Order a paid prompt's Payment Request is for, when the prepaid-order flow
    (M-Pesa Suite Settings > Sales Order payments) is on; else None."""
    if req.get("reference_doctype") != "Payment Request" or not enabled("enable_sales_order_payments"):
        return None
    pr = frappe.db.get_value("Payment Request", req.get("reference_name"), ["reference_doctype", "reference_name"],
                             as_dict=True)
    if not pr or pr.reference_doctype != "Sales Order" or not pr.reference_name:
        return None
    return pr.reference_name if cint(frappe.db.get_value("Sales Order", pr.reference_name, "docstatus")) == 0 else None


def repoint_superseded(req) -> None:
    """A prompt whose Payment Request was superseded (cancelled by a re-prompt for the same
    document) while the customer paid it: book it on the request that replaced it.

    Left on the cancelled request, every booking fails (the prompt's save links a cancelled
    document) and the money stays in no ledger. Only a submitted M-Pesa request for the same
    document qualifies (the newest); with none, booking fails and staff settle it by hand.
    """
    from .payment_request import is_mpesa

    if getattr(req, "reference_doctype", None) != "Payment Request" or not getattr(req, "reference_name", None):
        return
    old = frappe.db.get_value("Payment Request", req.reference_name, ["docstatus", "reference_doctype", "reference_name"],
                              as_dict=True)
    if not old or cint(old.docstatus) != 2 or not (old.reference_doctype and old.reference_name):
        return
    live = [
        row.name
        for row in frappe.get_all(
            "Payment Request",
            filters={"reference_doctype": old.reference_doctype, "reference_name": old.reference_name, "docstatus": 1,
                     "payment_request_type": "Inward", "status": ["in", ["Requested", "Initiated", "Partially Paid"]]},
            fields=["name", "payment_gateway", "payment_gateway_account"],
            order_by="creation desc",
        )
        if is_mpesa(row)
    ]
    if not live:
        return
    superseded = req.reference_name
    frappe.db.set_value(EXPRESS, req.name, "reference_name", live[0], update_modified=False)
    req.reference_name = live[0]
    req.add_comment("Info", f"Payment Request {superseded} was superseded while the customer paid this prompt; "
                            f"booked on {live[0]} for {old.reference_doctype} {old.reference_name}.")


def _held_by_reversal(req, reversal) -> None:
    """Log (once per reason) why a paid prompt is not booked: its receipt's money is, or may
    be, back with the payer, and booking it would credit the customer with refunded money."""
    log_match("Error", "STK Callback", express=req, on_change=True, receipt=req.transaction_id,
              reference_doctype=EXPRESS, reference_name=req.name,
              details=f"Not booked: M-Pesa receipt {req.transaction_id} has reversal {reversal.name} "
                      f"({reversal.status}), so its money is, or may be, back with the payer. "
                      "If the reversal ends Failed it is booked on the next sweep; otherwise settle it by hand.")


def _same_receipt(a, b) -> bool:
    return str(a or "").strip().upper() == str(b or "").strip().upper()


def _adopted_receipt(req, receipt) -> str | None:
    """Another C2B receipt the orphan matcher gave this prompt's document, which no prompt
    carries: the matcher writes the receipt on the prompt only when it books the register at
    once, not when the register waits for a draft order (booked when the order is released or
    submitted). Booking the prompt as well would credit the order twice. Another prompt for the
    same document is flagged too: the register does not say which prompt it was adopted for."""
    from .matching import prompt_document

    document = prompt_document(req.reference_doctype, req.reference_name)
    if not document:
        return None
    # A plain read is enough: the matcher adopts only a prompt with no callback, under its lock,
    # so the callback settled here was stored (and seen Pending above) after that commit.
    for transid in frappe.get_all(
        REGISTER,
        filters={"docstatus": ["<", 2], "match_method": "Orphan STK Prompt",
                 "matched_reference_doctype": document[0], "matched_reference_name": document[1]},
        pluck="transid",
        order_by="creation asc",
    ):
        if transid and not _same_receipt(transid, receipt) and not frappe.db.exists(EXPRESS, {"transaction_id": transid}):
            return transid
    return None


def _already_matched(req, parsed: dict, outcome: str, receipt: str) -> str:
    """The prompt already carries a receipt from C2B (orphan adoption, or its request settled
    from a C2B receipt), or the orphan matcher matched one to its document, booked (or to be
    booked) through that register. A callback for another receipt, or one saying the prompt
    failed, means that money may not be this customer's: overwriting the receipt and booking
    the prompt as well would credit the customer twice. Never booked here; a person decides."""
    said = (
        f"paid with receipt {parsed['receipt']}"
        if outcome == verify.VERIFIED
        else f"failed ({parsed['result_desc'] or parsed['result_code']})"
    )
    _set(req.name, {"verification_status": verify.MISMATCH,
                    "result_desc": f"Callback {said}; already matched to C2B receipt {receipt}"[:140]})
    log_match("Error", "STK Callback", express=req, once=True, receipt=receipt,
              details=f"Safaricom says this prompt {said}, but it was already matched to C2B receipt {receipt}, "
                      "which is booked (or waits to be booked) through its register. Not booked from the prompt: "
                      "check both by hand.")
    return verify.MISMATCH


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

    from .stk_poll import prompted_between

    unbooked = prompted_between(
        {
            "verification_status": verify.VERIFIED,
            "status": "Completed",
            "is_reconciled": 0,
            "transaction_id": ["is", "set"],
            "booking_attempts": ["<", MAX_BOOKING_ATTEMPTS],
        },
        since,
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
        from ..mpsa_suite.api.m_pesa_api import publish_stk_status

        publish_stk_status(req.status, req.reference_name)
