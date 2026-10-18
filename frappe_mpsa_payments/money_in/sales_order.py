"""Prepaid Sales Orders paid by M-Pesa (STK prompts and/or direct paybill payments).

A draft order cannot take an advance in ERPNext, so receipts wait (matched, unbooked)
until they cover the order. Then the order is released through its Workflow as the
configured approver user, and every receipt is booked as an advance against it.
"""

import frappe
from frappe.utils import cstr, flt

from .common import REGISTER, as_user, enabled, log_match, suite, tolerance


def payment_requests_for(so_name: str) -> list:
    return frappe.get_all(
        "Payment Request",
        filters={"reference_doctype": "Sales Order", "reference_name": so_name},
        pluck="name",
        order_by="creation asc",
    )


def receipts_for(so_name: str) -> list:
    """Live (not cancelled) registers that belong to this order."""
    refs = [so_name, *payment_requests_for(so_name)]
    by_ref = frappe.get_all(
        REGISTER,
        filters={"billrefnumber": ["in", refs], "docstatus": ["<", 2]},
        pluck="name",
        order_by="creation asc",
    )
    by_match = frappe.get_all(
        REGISTER,
        filters={"matched_reference_doctype": "Sales Order", "matched_reference_name": so_name, "docstatus": ["<", 2]},
        pluck="name",
        order_by="creation asc",
    )
    names = list(dict.fromkeys(by_ref + by_match))
    return [frappe.get_doc(REGISTER, n) for n in names]


def process_order(so_name: str) -> None:
    from .booking import book_register, stamp

    if not enabled("enable_sales_order_payments"):
        return
    so = frappe.get_doc("Sales Order", so_name)
    receipts = receipts_for(so_name)
    if so.docstatus == 2 or so.status in ("Closed", "Completed"):
        # order is gone: book its waiting receipts as customer advances instead of retrying forever
        for reg in receipts:
            if reg.docstatus == 0 and not reg.get("payment_entry"):
                reg.db_set({"matched_reference_doctype": None, "matched_reference_name": None,
                            "match_method": "Customer advance (order closed)"}, update_modified=False)
                book_register(reg.name, "Sales Order")
        return

    if not receipts:
        return
    for reg in receipts:  # make sure every receipt points at the order and its customer
        if reg.docstatus == 0 and reg.get("matched_reference_name") != so_name:
            stamp(reg, frappe._dict(customer=so.customer, doctype="Sales Order", name=so_name, method="Sales Order"))

    received = sum(flt(r.transamount) for r in receipts)
    due = flt(so.rounded_total or so.grand_total)

    if so.docstatus == 0:
        if received + tolerance() < due:
            return  # partial payment: wait for the rest
        _write_details(so, receipts)
        release_order(so_name)
        so.reload()
        if so.docstatus != 1:
            return

    for reg in receipts:
        if reg.docstatus == 0 and not reg.get("payment_entry"):
            book_register(reg.name, "Sales Order")


def release_order(so_name: str) -> None:
    """Move a fully prepaid draft order to submitted through its Workflow (or submit directly)."""
    from frappe.model.workflow import apply_workflow, get_transitions, get_workflow_name

    settings = suite()
    approver = settings.get("sales_order_approver")
    actions = [a.strip() for a in cstr(settings.get("sales_order_workflow_actions")).splitlines() if a.strip()]
    workflow = get_workflow_name("Sales Order")

    if workflow and not approver:
        log_match("Error", "Sales Order", once=True, reference_doctype="Sales Order", reference_name=so_name,
                  details="Order is fully paid but no 'Approve Paid Orders As' user is set in M-Pesa Suite Settings.")
        return

    with as_user(approver or frappe.session.user):
        if not workflow:
            doc = frappe.get_doc("Sales Order", so_name)
            doc.submit()
            return
        for action in actions:
            doc = frappe.get_doc("Sales Order", so_name)
            if doc.docstatus != 0:
                break
            available = {t.action for t in get_transitions(doc)}
            if action in available:
                apply_workflow(doc.as_dict(), action)

    doc = frappe.get_doc("Sales Order", so_name)
    if doc.docstatus == 0:
        log_match(
            "Error",
            "Sales Order",
            once=True,
            reference_doctype="Sales Order",
            reference_name=so_name,
            details=f"Fully paid but still in '{doc.get('workflow_state')}'. Check the workflow actions and that "
            f"{approver} holds the roles each step needs.",
        )


def _write_details(so, receipts) -> None:
    field = cstr(suite().get("sales_order_details_field")).strip()
    if not field or not so.meta.has_field(field):
        return
    existing = cstr(so.get(field))
    lines = [existing] if existing else []
    for r in receipts:
        if r.transid and r.transid not in existing:
            payer = r.full_name or "M-Pesa"
            lines.append(f"M-Pesa {r.transid} KES {flt(r.transamount):,.2f} {payer}".strip())
    values = {field: "\n".join(lines)}
    if so.meta.has_field("custom_payment_date_time_"):
        from .common import parse_transtime

        times = [parse_transtime(r.transtime) for r in receipts]
        times = [t for t in times if t]
        if times:
            values["custom_payment_date_time_"] = max(times)
    so.db_set(values, update_modified=False)


def process_pending_orders(limit: int = 100) -> None:
    """Scheduler: orders with receipts waiting (partial payments, approval retries, booking)."""
    if not enabled("enable_sales_order_payments"):
        return
    orders = set(
        frappe.get_all(
            REGISTER,
            filters={"docstatus": 0, "matched_reference_doctype": "Sales Order", "payment_entry": ["is", "not set"]},
            pluck="matched_reference_name",
            order_by="creation asc",
            limit=limit,
        )
    )

    for so_name in sorted(o for o in orders if o):
        frappe.db.savepoint("mpesa_so")
        try:
            process_order(so_name)
        except Exception:
            frappe.db.rollback(save_point="mpesa_so")
            frappe.log_error(frappe.get_traceback(), f"M-Pesa paid order processing failed for {so_name}")
            log_match("Error", "Sales Order", once=True, reference_doctype="Sales Order", reference_name=so_name,
                      details=frappe.get_traceback()[-500:])

