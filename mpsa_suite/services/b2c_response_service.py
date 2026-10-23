import frappe
from frappe.utils import getdate

from mpsa_suite.connectors.factory import get_b2c_connector
from mpsa_suite.utils.doctype_names import B2C_REQUEST_DOCTYPE


def process_b2c_result(provider: str, payload: dict) -> None:
    """
    Handle the raw B2C callback payload:
    1) Instantiate connector via factory.
    2) Delegate to its handle_callback(), which updates the request doc
       and enqueues downstream work.
    """
    originator_id = payload.get("OriginatorConversationID")
    req = frappe.db.get_value(
        B2C_REQUEST_DOCTYPE,
        {"originator_conversation_id": originator_id},
        ["name", "mpesa_settings"],
        as_dict=True,
    )
    if not req:
        frappe.throw(f"No B2C request for OriginatorConversationID {originator_id}")

    if not req.mpesa_settings:
        frappe.throw(f"B2C request {req.name} has no Mpesa Settings")
    provider, settings_name = "Mpesa", req.mpesa_settings

    # Now load the connector with a real settings_name
    connector = get_b2c_connector(provider, settings_name)
    connector.handle_callback(payload)


def update_b2c_reference_status(b2c_request_name: str, enqueue_next: bool = False):
    """
    After a B2C Request finishes, update the matching Reference row,
    then (optionally) enqueue the post-processing job.
    """
    req = frappe.get_doc(B2C_REQUEST_DOCTYPE, b2c_request_name)
    ref_name = req.b2c_payment_disbursement_reference
    line = _schedule_line(ref_name)
    was_paid = bool(line) and frappe.db.get_value(
        "B2C Payment Disbursement Reference", ref_name, "payment_status") == "Paid"

    if req.status in ("Paid", "Failed") and ref_name:
        update = {
            "Paid": {
                "payment_status": "Paid",
                "reference_no": req.transaction_id,
                "reference_date": getdate(req.transaction_completed_datetime),
            },
            "Failed": {"payment_status": "Failed"},
        }[req.status]
        if req.docstatus == 1 or _link_is_free(ref_name):
            # Also (re)write the row's link to its request: an older full save of the
            # disbursement could wipe it, and booking (_book, _mark_booked) reads the request
            # through it. A cancelled request's late result never takes the row over.
            update["b2c_disbursement_request"] = req.name

        frappe.db.set_value("B2C Payment Disbursement Reference", ref_name, update)
        if line and req.status == "Paid" and not was_paid and req.docstatus == 1:
            _check_receiver_name(req, line)  # once, when the row first shows Paid
        frappe.db.commit()

    if enqueue_next and req.status == "Paid":
        from mpsa_suite.money_out.statement import after_payout
        from mpsa_suite.services.b2c_postprocess_service import (
            handle_successful_payment,
        )

        handle_successful_payment(req.b2c_payment_disbursement, ref_name)
        # the payout's statement lines, reconciled with the entry just booked
        after_payout(B2C_REQUEST_DOCTYPE, req.name)

    if line:
        from mpsa_suite.money_out.schedule import sync_b2c_line_later

        sync_b2c_line_later(ref_name)  # the payment schedule's line shows the result and its entry


def _schedule_line(ref_name: str | None) -> str | None:
    """The M-Pesa Payment Schedule line a disbursement row pays, or None."""
    if not ref_name or not frappe.get_meta("B2C Payment Disbursement Reference").has_field("mpesa_schedule_line"):
        return None
    return frappe.db.get_value("B2C Payment Disbursement Reference", ref_name, "mpesa_schedule_line") or None


def _check_receiver_name(req, line: str) -> None:
    """The name M-Pesa paid against the employee's approved phone account: a mismatch blocks the
    account until a manager confirms it. Never raises."""
    try:
        from mpsa_suite.money_out import employee_accounts

        account = frappe.db.get_value("M-Pesa Payment Schedule Line", line, "payee_account")
        employee_accounts.record_receiver_name(req, account, req.receiver_public_name)
    except Exception:
        frappe.log_error(title="M-Pesa B2C receiver name check failed", message=f"{req.name}\n{frappe.get_traceback()}")


def _link_is_free(ref_name: str) -> bool:
    """A cancelled request may still fill its row's empty link, so the guards that read a
    payout through the row (open_payouts, the retry check, an amend's copy of the row) find
    it: only while no request holds the row, and none submitted names it."""
    return not frappe.db.get_value(
        "B2C Payment Disbursement Reference", ref_name, "b2c_disbursement_request"
    ) and not frappe.db.exists(
        B2C_REQUEST_DOCTYPE, {"b2c_payment_disbursement_reference": ref_name, "docstatus": 1}
    )
