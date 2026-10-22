"""Prepaid Sales Orders paid by M-Pesa (STK prompts and/or direct paybill payments).

A draft order cannot take an advance in ERPNext, so receipts wait (matched, unbooked)
until they cover the order. Then the order is released through its Workflow as the
configured approver user, and every receipt is booked as an advance against it.
"""

import frappe
from frappe.utils import add_to_date, cstr, flt, get_datetime, now_datetime, strip_html

from ..compat import get_setting
from .common import MATCH_LOG, REGISTER, as_user, enabled, log_match, reversed_registers, suite, tolerance

REFUSED = "could not release it"  # in the Match Log details of a refused release


def payment_requests_for(so_name: str) -> list:
    return frappe.get_all(
        "Payment Request",
        filters={"reference_doctype": "Sales Order", "reference_name": so_name},
        pluck="name",
        order_by="creation asc",
    )


def receipts_for(so_name: str) -> list:
    """Live (not cancelled, not reversed) registers that belong to this order.

    A reversed receipt's money is back with the payer: it must not count toward releasing
    the order, nor be booked as an advance.
    """
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
    held = set(reversed_registers(names))
    return [frappe.get_doc(REGISTER, n) for n in names if n not in held]


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
        log_match("Error", "Sales Order", on_change=True, reference_doctype="Sales Order", reference_name=so_name,
                  details="Order is fully paid but no 'Approve Paid Orders As' user is set in M-Pesa Suite Settings.")
        return
    if _release_backing_off(so_name):
        return

    user = approver or frappe.session.user
    frappe.db.savepoint("mpesa_so_release")
    messages = getattr(frappe.local, "message_log", None)
    shown = len(messages or [])
    flagged = frappe.flags.get("error_message")  # Frappe raises PermissionError bare: its text goes here
    frappe.flags.error_message = None  # so only this attempt's text is read below
    try:
        with as_user(user):
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
    except Exception as e:
        # ERPNext refused the order (credit limit, a mandatory field, a workflow condition, a
        # missing permission), or something failed on the way (a bug in a hook). Undo the
        # half-applied steps and keep it a draft; raising would make the caller roll back the
        # receipt match too and log the same refusal every 2 minutes. The order is retried with
        # backoff (see _release_backing_off) until the cause is fixed.
        frappe.db.rollback(save_point="mpesa_so_release")
        if messages:
            del messages[shown:]  # handled here, not an error dialog for whoever triggered it
        reason = strip_html(cstr(e) or cstr(frappe.flags.get("error_message"))).strip()[:300]
        hint = _refusal_hint(e, user)
        if hint is None:  # not one of ERPNext's refusals: likely a bug (in a hook ...), keep its traceback
            _log_error_once(f"M-Pesa paid order release failed for {so_name}", so_name, frappe.get_traceback())
            reason = f"{type(e).__name__}: {reason}"
            hint = (" The traceback is in the Error Log." if isinstance(e, frappe.ValidationError)
                    else " This is an error, not a refusal: the traceback is in the Error Log.")
        # a repeat of the same refusal only marks its row as tried again: the backoff clock
        log_match("Error", "Sales Order", on_change=True, reference_doctype="Sales Order", reference_name=so_name,
                  details=f"Fully paid but {user} {REFUSED}: {reason}"
                  f"{hint} Retried less often each time (up to every 6 hours); saving "
                  "the order or its customer retries it at once.")
        return
    finally:
        frappe.flags.error_message = flagged  # handled here: not shown to whoever triggered it

    doc = frappe.get_doc("Sales Order", so_name)
    if doc.docstatus == 0:
        log_match(
            "Error",
            "Sales Order",
            on_change=True,
            reference_doctype="Sales Order",
            reference_name=so_name,
            details=f"Fully paid but still in '{doc.get('workflow_state')}'. Check the workflow actions and that "
            f"{approver} holds the roles each step needs.",
        )


def _release_backing_off(so_name: str) -> bool:
    """After a refusal, wait 2 minutes, then 4, 8 ... up to 6 hours between attempts.

    Each attempt runs workflow steps that are then rolled back, but a savepoint rollback keeps
    the after-commit jobs they queued (Workflow Action approval emails). The refusal's Match
    Log row holds the clock: created at the first refusal, ``modified`` at the latest one. A
    saved order or customer (a raised credit limit) is retried at once.
    """
    row = frappe.db.get_value(
        MATCH_LOG,
        {"reference_doctype": "Sales Order", "reference_name": so_name, "result": "Error"},
        ["details", "creation", "modified"],
        as_dict=True,
        order_by="creation desc",
    )
    if not row or REFUSED not in cstr(row.details):
        return False
    last = get_datetime(row.modified)
    wait = min(max((last - get_datetime(row.creation)).total_seconds() / 60, 2), 360)
    if now_datetime() >= add_to_date(last, minutes=wait):
        return False
    so = frappe.db.get_value("Sales Order", so_name, ["modified", "customer"], as_dict=True) or frappe._dict()
    changed = (so.modified, so.customer and frappe.db.get_value("Customer", so.customer, "modified"))
    return not any(t and get_datetime(t) > last for t in changed)


def _refusal_hint(e: Exception, user: str) -> str | None:
    """What may fix a refused release, or None when the failure is not one of the refusals Frappe
    and ERPNext raise for a setting, a role or the order's data (it may then be a bug)."""
    if isinstance(e, frappe.PermissionError):  # raised bare: its text is in frappe.flags.error_message
        return (f" Check that {user} has a role that may submit Sales Orders, and any User Permission "
                "the order needs.")
    if not isinstance(e, frappe.ValidationError):
        return None
    if type(e) is frappe.ValidationError:  # frappe.throw / msgprint(raise_exception): the credit check ...
        return _credit_controller_hint(user)
    from frappe.model.workflow import WorkflowPermissionError, WorkflowStateError, WorkflowTransitionError

    if isinstance(e, (WorkflowPermissionError, WorkflowStateError, WorkflowTransitionError)):
        return f" Check the workflow actions and that {user} holds the roles each step needs."
    if isinstance(e, frappe.MandatoryError):
        return " Fill in the missing fields on the order."
    return None  # DoesNotExistError, LinkValidationError, DataError ...: usually a bug in a hook


def _credit_controller_hint(user: str) -> str:
    """Receipts are booked after the release, so ERPNext's credit check counts the whole paid
    order as owed; only the Accounts Settings credit controller role lets it through."""
    role = get_setting("Accounts Settings", "credit_controller")
    if role and role in frappe.get_roles(user):
        return ""
    if not role:
        return f" If the credit limit stopped it: set a Credit Controller role in Accounts Settings and give it to {user}."
    return f" If the credit limit stopped it: give {user} the '{role}' role (Accounts Settings > Credit Controller)."


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
    filters = {"docstatus": 0, "matched_reference_doctype": "Sales Order", "payment_entry": ["is", "not set"]}
    held = reversed_registers()
    if held:
        filters["name"] = ["not in", held]  # refunded: they would only take places in the batch
    orders = set(
        frappe.get_all(
            REGISTER,
            filters=filters,
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
            traceback = frappe.get_traceback()
            _log_error_once(f"M-Pesa paid order processing failed for {so_name}", so_name, traceback)
            log_match("Error", "Sales Order", on_change=True, reference_doctype="Sales Order", reference_name=so_name,
                      details=traceback[-500:])


def _log_error_once(title: str, so_name: str, traceback: str) -> None:
    """Error Log for an order's failure: once per order and error, not every run while it stays stuck."""
    last = frappe.db.get_value(
        "Error Log",
        {"reference_doctype": "Sales Order", "reference_name": so_name, "method": title},
        "error",
        order_by="creation desc",
    )
    if last is None or _last_line(last) != _last_line(traceback):
        frappe.log_error(title=title, message=traceback, reference_doctype="Sales Order", reference_name=so_name)


def _last_line(text) -> str:
    """The exception line of a traceback."""
    lines = cstr(text).strip().splitlines()
    return lines[-1] if lines else ""

