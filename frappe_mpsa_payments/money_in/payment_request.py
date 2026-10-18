"""M-Pesa Payment Requests: phone hygiene, re-prompting, and status once money lands."""

import frappe
from frappe.utils import add_days, cstr, flt, nowdate

from ..utils.phone import to_local
from .common import EXPRESS, REGISTER, enabled, tolerance


def is_mpesa(doc) -> bool:
    return cstr(doc.get("payment_gateway")).lower().startswith("mpesa") or cstr(
        doc.get("payment_gateway_account")
    ).lower().startswith("mpesa")


def before_validate(doc, method=None) -> None:
    if enabled("normalize_payment_request_phone") and is_mpesa(doc) and doc.get("phone_number"):
        local = to_local(doc.phone_number)
        if local:
            doc.phone_number = local


def before_insert(doc, method=None) -> None:
    if enabled("supersede_unpaid_prompts") and is_mpesa(doc):
        supersede_unpaid(doc)


def supersede_unpaid(doc) -> None:
    """Re-prompt: free the amount held by older unpaid M-Pesa requests for the same document.

    Cancelled the proper way (``cancel()``), so ERPNext releases the reserved outstanding
    and a late payment against the old request still resolves to the same order.
    """
    if not (doc.get("reference_doctype") and doc.get("reference_name")):
        return
    older = frappe.get_all(
        "Payment Request",
        filters={
            "reference_doctype": doc.reference_doctype,
            "reference_name": doc.reference_name,
            "payment_request_type": "Inward",
            "docstatus": ["<", 2],
            "status": ["in", ["Draft", "Requested", "Initiated"]],
            "name": ["!=", doc.name or ""],
        },
        fields=["name", "docstatus", "payment_gateway", "payment_gateway_account"],
        order_by="creation asc",
    )
    for row in older:
        if not is_mpesa(row):
            continue
        old = frappe.get_doc("Payment Request", row.name)
        old.flags.ignore_permissions = True
        if old.docstatus == 0:
            old.delete()
            continue
        old.ignore_linked_doctypes = ("Mpesa Express Request", "Integration Request")
        old.cancel()
        old.add_comment("Info", f"Superseded by a new M-Pesa prompt {doc.name or ''}".strip())


def sync_statuses() -> None:
    """Mark M-Pesa requests Paid once their document is settled; complete their STK prompt."""
    if not enabled("sync_payment_request_status"):
        return
    tol = tolerance()
    rows = frappe.get_all(
        "Payment Request",
        filters={
            "docstatus": 1,
            "status": ["in", ["Requested", "Initiated", "Partially Paid"]],
            "creation": [">=", add_days(nowdate(), -60)],
        },
        fields=["name", "reference_doctype", "reference_name", "grand_total", "payment_gateway", "payment_gateway_account"],
        order_by="creation asc",
        limit=500,
    )
    for pr in rows:
        if not is_mpesa(pr):
            continue
        # "settled" = the whole document is paid, not just an amount equal to this request
        settled = False
        if pr.reference_doctype == "Sales Order":
            so = frappe.db.get_value(
                "Sales Order", pr.reference_name, ["docstatus", "advance_paid", "rounded_total", "grand_total"], as_dict=True
            )
            settled = bool(
                so and so.docstatus == 1 and flt(so.advance_paid) > 0
                and flt(so.advance_paid) + tol >= flt(so.rounded_total or so.grand_total)
            )
        elif pr.reference_doctype == "Sales Invoice":
            si = frappe.db.get_value("Sales Invoice", pr.reference_name, ["docstatus", "outstanding_amount"], as_dict=True)
            settled = bool(si and si.docstatus == 1 and flt(si.outstanding_amount) <= tol)
        if not settled:
            continue
        values = {"status": "Paid"}
        if frappe.get_meta("Payment Request").has_field("outstanding_amount"):
            values["outstanding_amount"] = 0  # what ERPNext itself writes when a request is paid
        frappe.db.set_value("Payment Request", pr.name, values)
        _complete_prompt(pr.name)


def _complete_prompt(pr_name: str) -> None:
    # only a booked register: a draft one would cancel itself on submit once a prompt carries its receipt
    receipt = frappe.db.get_value(
        REGISTER, {"billrefnumber": pr_name, "docstatus": 1}, "transid", order_by="creation asc"
    )
    for name in frappe.get_all(
        EXPRESS,
        filters={"reference_doctype": "Payment Request", "reference_name": pr_name, "docstatus": 1,
                 "status": "In Progress"},
        pluck="name",
        order_by="creation asc",
    ):
        values = {"status": "Completed", "result_code": "0",
                  "result_desc": "Settled; confirmed from the C2B receipt (no STK callback received)"}
        if receipt and not frappe.db.get_value(EXPRESS, name, "transaction_id"):
            values["transaction_id"] = receipt
            receipt = None  # one prompt gets the receipt
        # db.set_value: saving the request would trigger its duplicate-C2B cancellation
        frappe.db.set_value(EXPRESS, name, values, update_modified=False)
