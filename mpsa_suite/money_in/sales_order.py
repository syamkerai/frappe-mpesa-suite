"""Prepaid Sales Orders paid by M-Pesa (STK prompts and/or direct paybill payments).

A draft order cannot take an advance in ERPNext, so receipts wait (matched, unbooked)
until they cover the order. Then the order is released through its Workflow as the
configured approver user, and every receipt is booked as an advance against it.

Receipts are C2B registers and paid STK prompts on the order's Payment Requests. A prompt
paid on a draft order cannot be booked either (ERPNext: "Sales Order must be submitted"), so
it waits here like a register and counts toward the order (money_in.stk_verify.book sends it
here instead of failing). When Safaricom also sends a C2B register for a prompt's receipt, the
prompt books it and the register is not counted.
"""

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, now_datetime, strip_html

from ..compat import get_setting
from .common import (
    EXPRESS,
    MATCH_LOG,
    REGISTER,
    as_user,
    enabled,
    is_fully_billed,
    log_match,
    receipt_reversal,
    reversed_registers,
    suite,
    suite_int,
    tolerance,
)

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
    registers = [frappe.get_doc(REGISTER, n) for n in names if n not in held]
    carried = prompt_receipts(r.transid for r in registers)
    # a receipt an STK prompt carries is the prompt's (paid_prompts_for): its register would cancel
    # itself as a duplicate on submit, and counting both would count the money twice
    return [r for r in registers if cstr(r.transid).strip() not in carried]


def prompt_receipts(transids) -> set:
    """The receipts among ``transids`` that a submitted Mpesa Express Request carries."""
    transids = [t for t in dict.fromkeys(cstr(t).strip() for t in transids) if t]
    if not transids:
        return set()
    return set(frappe.get_all(EXPRESS, filters={"transaction_id": ["in", transids], "docstatus": 1},
                              pluck="transaction_id"))


def _verified_only(filters: dict) -> dict:
    """Only prompts Safaricom confirmed (STK Query), where the site has the verification field."""
    if frappe.get_meta(EXPRESS).has_field("verification_status"):
        filters["verification_status"] = "Verified"
    return filters


def paid_prompts_for(so_name: str) -> list:
    """Paid STK prompts on the order's Payment Requests that are not booked yet.

    Cancelled requests count too: a re-prompt supersedes (cancels) the older request, and the
    customer may have paid the older prompt meanwhile (stk_verify books it on the newer one).
    Only Completed, confirmed prompts with a receipt that no submitted Payment Entry carries and
    no reversal holds. Rows: name, transaction_id, amount, transaction_date.
    """
    requests = payment_requests_for(so_name)
    if not requests:
        return []
    rows = frappe.get_all(
        EXPRESS,
        filters=_verified_only({"reference_doctype": "Payment Request", "reference_name": ["in", requests],
                                "docstatus": 1, "status": "Completed", "is_reconciled": 0,
                                "transaction_id": ["is", "set"]}),
        fields=["name", "transaction_id", "amount", "transaction_date"],
        order_by="creation asc",
    )
    out, seen = [], set()
    for row in rows:
        receipt = cstr(row.transaction_id).strip()
        if not receipt or receipt in seen:
            continue
        if frappe.db.exists("Payment Entry", {"reference_no": receipt, "docstatus": 1}):
            continue  # booked another way (its C2B register, by hand)
        if receipt_reversal(receipt):
            continue  # the money is, or may be, back with the payer
        seen.add(receipt)
        out.append(row)
    return out


def _book_prompt(name: str) -> None:
    from .stk_verify import book

    book(name, commit=False)  # inside the caller's transaction; a failure is rolled back and logged there


def process_order(so_name: str) -> None:
    from .booking import book_register, stamp

    if not enabled("enable_sales_order_payments"):
        return
    so = frappe.get_doc("Sales Order", so_name)
    receipts = receipts_for(so_name)
    prompts = paid_prompts_for(so_name)
    if so.docstatus == 1 and is_fully_billed(so_name):
        # invoiced: ERPNext takes no more payments on the order; its receipts pay its invoice
        for reg in receipts:
            if reg.docstatus == 0 and not reg.get("payment_entry"):
                book_on_invoice(reg, so)
        for prompt in prompts:  # booked on the order's open invoices (utils._book_on_billed_order)
            _book_prompt(prompt.name)
        return
    if so.docstatus == 2 or so.status in ("Closed", "Completed"):
        # order is gone: book its waiting receipts as customer advances instead of retrying forever
        for reg in receipts:
            if reg.docstatus == 0 and not reg.get("payment_entry"):
                reg.db_set({"matched_reference_doctype": None, "matched_reference_name": None,
                            "match_method": "Customer advance (order closed)"}, update_modified=False)
                book_register(reg.name, "Sales Order")
        for prompt in prompts:
            log_match("Error", "Sales Order", express=prompt, once=True, reference_doctype="Sales Order",
                      reference_name=so_name,
                      details=f"M-Pesa prompt {prompt.name} (receipt {prompt.transaction_id}) was paid for an order "
                              "that is closed or cancelled: book it by hand.")
        return

    if not receipts and not prompts:
        return
    for reg in receipts:  # make sure every receipt points at the order and its customer
        if reg.docstatus == 0 and reg.get("matched_reference_name") != so_name:
            stamp(reg, frappe._dict(customer=so.customer, doctype="Sales Order", name=so_name, method="Sales Order"))

    received = sum(flt(r.transamount) for r in receipts) + sum(flt(p.amount) for p in prompts)
    due = flt(so.rounded_total or so.grand_total)

    if so.docstatus == 0:
        if received + tolerance() < due:
            return  # partial payment: wait for the rest
        _write_details(so, receipts, prompts)
        release_order(so_name)
        so.reload()
        if so.docstatus != 1:
            return

    for reg in receipts:
        if reg.docstatus == 0 and not reg.get("payment_entry"):
            book_register(reg.name, "Sales Order")
    for prompt in prompts:
        _book_prompt(prompt.name)


def book_on_invoice(reg, so) -> None:
    """Book a receipt for an invoiced order on that order's own open invoice.

    All its invoices paid: the money is the customer's, booked as their advance (as for a
    completed order). Several open invoices and none matching the amount: left for staff.
    """
    from .booking import book_register, stamp
    from .matching import Match, retarget_billed_order

    match = retarget_billed_order(
        reg, Match(method="Sales Order", doctype="Sales Order", name=so.name, customer=so.customer)
    )
    if match.get("ambiguous"):
        log_match("Ambiguous", "Sales Order", register=reg, once=True, candidates=match.candidates,
                  reference_doctype="Sales Order", reference_name=so.name, details=match.details)
        return
    if match.get("doctype") == "Sales Invoice":
        stamp(reg, match)
        book_register(reg.name, "Sales Order")
        return
    reg.db_set({"matched_reference_doctype": None, "matched_reference_name": None,
                "match_method": "Customer advance (order invoiced and paid)"}, update_modified=False)
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


def _write_details(so, receipts, prompts=()) -> None:
    field = cstr(suite().get("sales_order_details_field")).strip()
    if not field or not so.meta.has_field(field):
        return
    existing = cstr(so.get(field))
    lines = [existing] if existing else []
    for r in receipts:
        if r.transid and r.transid not in existing:
            payer = r.full_name or "M-Pesa"
            lines.append(f"M-Pesa {r.transid} KES {flt(r.transamount):,.2f} {payer}".strip())
    for p in prompts:
        if p.transaction_id and p.transaction_id not in existing:
            lines.append(f"M-Pesa {p.transaction_id} KES {flt(p.amount):,.2f} STK prompt {p.name}")
    values = {field: "\n".join(lines)}
    if so.meta.has_field("custom_payment_date_time_"):
        from .common import parse_transtime

        times = [parse_transtime(r.transtime) for r in receipts]
        times += [get_datetime(p.transaction_date) for p in prompts if p.get("transaction_date")]
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
    orders |= set(orders_with_paid_prompts(limit))

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


def orders_with_paid_prompts(limit: int = 100) -> list:
    """Sales Orders whose Payment Requests have paid STK prompts not booked yet.

    A prompt paid on a draft order waits for the order's release; once the order is submitted
    (released here, or approved by hand) its prompt is booked on the next run. A draft order is
    always taken: attempts spent while it was a draft (by a version that tried to book them
    there) were never real ones. A submitted order whose prompt used up its booking attempts is
    left to staff (Error Log, Reconcile Payment on the prompt). Looks back
    ``matching_lookback_days`` (default 45).
    """
    from .stk_verify import MAX_BOOKING_ATTEMPTS

    since = add_to_date(now_datetime(), days=-suite_int("matching_lookback_days", 45))
    rows = frappe.get_all(
        EXPRESS,
        filters=_verified_only({"reference_doctype": "Payment Request", "docstatus": 1, "status": "Completed",
                                "is_reconciled": 0, "transaction_id": ["is", "set"], "creation": [">=", since]}),
        fields=["reference_name", "booking_attempts"],
        order_by="creation asc",
        limit=limit,
    )
    if not rows:
        return []
    order_of = dict(
        (r.name, r.reference_name)
        for r in frappe.get_all(
            "Payment Request",
            filters={"name": ["in", list(dict.fromkeys(r.reference_name for r in rows if r.reference_name))],
                     "reference_doctype": "Sales Order"},
            fields=["name", "reference_name"],
            order_by="creation asc",
        )
    )
    orders = []
    for row in rows:
        so_name = order_of.get(row.reference_name)
        if not so_name or so_name in orders:
            continue
        if cint(row.booking_attempts) < MAX_BOOKING_ATTEMPTS or cint(
                frappe.db.get_value("Sales Order", so_name, "docstatus")) == 0:
            orders.append(so_name)
    return orders


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

