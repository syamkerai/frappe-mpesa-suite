"""Daraja B2B BusinessPayBill / BusinessBuyGoods supplier payments (docs/daraja_api_reference.md §9).

Same safety rules as B2C payouts: sent only from a submitted document under a row lock,
whole shillings, the ack sets ``Initiated`` (never ``Paid``), only the Result sets
Paid/Failed, a re-sent Result is ignored, and the queue timeout has its own URL.

A request may pay a Purchase Invoice, a Purchase Order (an advance its invoice picks up under
Get Advances) or neither. Every check runs on save, again at submit under the document's lock
and again right before sending: the approved destination, the shortcode, the supplier's hold,
the document, and what is left to pay with B2B, B2C, draft entries and order advances counted
(money_out.owed). The entry is booked when M-Pesa confirms, or later (money_out.b2b_entries).

A request may instead pay an employee, never both: to the employee's approved bank paybill
account (Employee M-Pesa Account), optionally for a Salary Slip (HRMS), which it may not pay past
what the slip has left (B2B, B2C and planned lines counted, under the slip's lock). A salary is
booked with a salary Journal Entry that clears the Payroll Entry's payroll payable
(money_out.salary_entries); a payment to an employee with no slip waits for Make Entry, and only
a manager sends it (nothing caps its amount). Nobody sends a payment to themselves.

A request made by the payment schedule (money_out.schedule) is submitted by Pay Selected but not
sent then: send_deferred sends it from a job, after committing its send_started_on marker, and
never before its line's planned day.
"""

import contextlib
import re

import frappe
from frappe.utils import cint, cstr, flt, now_datetime

from ..daraja import apis
from ..daraja import money_out as daraja
from ..permissions import PAYOUT_ROLES, require
from ..utils.phone import normalize_msisdn
from . import employee_accounts, owed, supplier_accounts
from .common import NOT_INITIATED, apply_ack, callback_url, initiator_settings, lock_for_send, log_callback, post
from .results import find_request

B2B_DOCTYPE = "Mpesa B2B Payment Request"
FINAL = ("Paid", "Failed")
# When Paid: book the Payment Entry at once, or wait for Make Entry (b2b_entries).
AUTO, LATER = "Book automatically", "Make the entry later"
# A Result that meets a deadlock or a row lock held past MariaDB's lock wait is recorded again
# from a job, this many tries in all: Safaricom may never send it again.
RESULT_TRIES = 5


def send_b2b(name: str) -> None:
    from . import schedule

    doc = lock_for_send(B2B_DOCTYPE, name)
    schedule.assert_line_due(doc)
    if doc.get("schedule_line"):
        # sent only by send_deferred, which commits its marker before the call (R1)
        frappe.throw(f"{doc.name} is paid from payment schedule {doc.payment_schedule}: it is sent by the schedule.")
    check_ready_to_send(doc)
    settings, payload = _payload(doc)
    # Nothing below may raise: the request has (maybe) left the building.
    data, error = post(
        settings, daraja.B2B_ENDPOINT, payload, doctype=B2B_DOCTYPE, name=doc.name, description="Mpesa B2B"
    )
    apply_ack(doc, data, error)


def send_deferred(name: str) -> bool:
    """Send a request the payment schedule submitted (send_batch's job); True when it was posted.

    The claim is a locking read, so a marker another worker committed after this job's snapshot
    is seen. The send_started_on marker is committed first: a worker that dies after it leaves a
    request the 5-minute sweep makes Unknown, never one that is sent again (R1). Then the locks
    in R4 order: the document's lock (held through the call, as at a direct submit), the request
    row, the line row. A check that fails now marks the request Failed, "Not sent: <reason>",
    while it is still this send's own, and its line may be planned again. Nothing after the
    call raises."""
    from . import schedule

    claim = frappe.db.get_value(B2B_DOCTYPE, name, ["docstatus", "status", "send_started_on"], as_dict=True,
                                for_update=True)
    if (not claim or cint(claim.docstatus) != 1 or (claim.status or NOT_INITIATED) != NOT_INITIATED
            or claim.send_started_on):
        return False  # cancelled, sent, or being sent by another worker
    marker = now_datetime()
    frappe.db.set_value(B2B_DOCTYPE, name, "send_started_on", marker, update_modified=False)
    frappe.db.commit()  # the marker stays whatever happens next
    # the document, read without a lock, to take its lock before any row's (R4)
    dt, dn = lock_document(name)
    frappe.db.savepoint("mpsa_b2b_send_checks")
    try:
        with contextlib.ExitStack() as stack:
            if dt:
                stack.enter_context(owed.doc_lock(dt, dn))
            doc = lock_for_send(B2B_DOCTYPE, name)
            schedule.assert_line_due(doc)
            check_ready_to_send(doc)  # re-enters the document's lock
            settings, payload = _payload(doc)
    except Exception as e:
        try:
            frappe.db.rollback(save_point="mpsa_b2b_send_checks")  # the checks write nothing
        except Exception:
            frappe.db.rollback()  # a deadlock ended the transaction, savepoint and all
        reason = re.sub(r"<[^>]+>", "", cstr(e)).strip() or type(e).__name__
        _fail_unsent(name, marker, f"Not sent: {reason}")
        frappe.db.commit()
        schedule.sync_line(name)  # nothing else is pending: the sweep repeats it if this fails
        frappe.db.commit()
        return False
    data, error = post(
        settings, daraja.B2B_ENDPOINT, payload, doctype=B2B_DOCTYPE, name=doc.name, description="Mpesa B2B"
    )
    apply_ack(doc, data, error)  # never raises; queues the line's update
    frappe.db.commit()
    return True


def lock_document(name: str) -> tuple:
    """(doctype, name) whose per-document lock a request's send or booking takes first (R4): its
    invoice or order, or the Salary Slip it pays. Read without a lock."""
    fields = ["purchase_invoice", "purchase_order"]
    if frappe.get_meta(B2B_DOCTYPE).has_field("salary_slip"):
        fields.append("salary_slip")
    values = frappe.db.get_value(B2B_DOCTYPE, name, fields, as_dict=True) or frappe._dict()
    dt, dn = owed.document_of(values)
    if not dt and cstr(values.get("salary_slip")).strip():
        return owed.SLIP, values.salary_slip
    return dt, dn


def _fail_unsent(name: str, marker, message: str) -> None:
    """Failed, only while the request is still Not Initiated with this send's marker (a locking
    read): an answer or a cancel committed meanwhile is kept."""
    current = frappe.db.get_value(B2B_DOCTYPE, name, ["status", "send_started_on"], as_dict=True, for_update=True)
    if (current and (current.status or NOT_INITIATED) == NOT_INITIATED
            and cstr(current.send_started_on)[:19] == cstr(marker)[:19]):
        frappe.db.set_value(B2B_DOCTYPE, name, {"status": "Failed", "error_message": message[:1000]},
                            update_modified=False)


def _payload(doc) -> tuple:
    """(settings, Daraja body) of the request."""
    settings = initiator_settings(doc.mpesa_settings)
    apis.require(settings, apis.B2B)
    payload = daraja.b2b_payload(
        initiator=settings.initiator_name,
        security_credential=settings.security_credential,
        command_id=doc.command_id,
        amount=doc.amount,
        party_a=apis.initiator_party_a(settings),
        party_b=doc.receiver_shortcode,
        account_reference=doc.account_reference,
        fallback_reference=doc.name,
        requester=doc.requester,
        remarks=doc.remarks,
        occasion=doc.occasion,
        result_url=callback_url("b2b_result", ref=doc.name),
        queue_timeout_url=callback_url("b2b_queue_timeout", ref=doc.name),
    )
    return settings, payload


def handle_result(result: dict, ref: str | None, attempt: int = 1) -> None:
    """Safaricom's Result (the callback, or a job trying it again). Recording it is idempotent:
    a Result already recorded changes nothing."""
    try:
        _record_result(result, ref)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        frappe.db.rollback()  # met before the Result's log commits: nothing of it was kept
        if attempt >= RESULT_TRIES:
            frappe.log_error(title="M-Pesa B2B Result not recorded",
                             message=f"ref {ref}\n{frappe.as_json(result)}\n{frappe.get_traceback()}")
            raise
        frappe.enqueue("mpsa_suite.money_out.b2b.handle_result", queue="short", timeout=300, result=result,
                       ref=ref, attempt=attempt + 1)


def _record_result(result: dict, ref: str | None) -> None:
    doc = find_request(B2B_DOCTYPE, result, ref)
    if not doc:
        log_callback({"Result": result}, "Mpesa B2B Result (unmatched)", B2B_DOCTYPE, ref, "Failed")
        return
    if doc.status in FINAL and doc.result_code not in (None, ""):
        return  # Safaricom re-sent a result we already recorded; never book twice

    values = daraja.result_values(result)
    outcome = daraja.b2b_outcome(result)
    entry = {}
    if outcome == "Paid":
        # entry_attempted_on marks a request Paid since this release: the sweep books it if the job
        # below is lost or fails before it commits (only rows Paid before the release have none)
        entry = {"entry_status": "Entry Not Made", "entry_note": "Booking the entry…",
                 "entry_attempted_on": now_datetime()}
    completed = daraja.parse_daraja_datetime(values.get("TransCompletedTime"))
    doc.db_set(
        {
            **entry,
            "status": outcome,
            "result_code": daraja.result_code(result),
            "result_desc": result.get("ResultDesc"),
            "conversation_id": doc.conversation_id or result.get("ConversationID"),
            "originator_conversation_id": doc.originator_conversation_id
            or result.get("OriginatorConversationID"),
            "transaction_id": result.get("TransactionID"),
            # a Buy Goods (till) Result states no time: when it arrived, flagged as estimated
            # until a Transaction Status query gives Safaricom's own (money_out.b2b_status)
            "trans_completed_time": completed or (now_datetime() if outcome == "Paid" else None),
            "completed_time_estimated": 1 if outcome == "Paid" and not completed else 0,
            "receiver_public_name": values.get("ReceiverPartyPublicName"),
            "debit_party_charges": values.get("DebitPartyCharges"),
            # live results name the account ("Working Account|KES|1476.00|..."); Daraja's sample
            # sends DebitAccountBalance instead, and a till's Result only
            # "{Amount={CurrencyCode=KES, ..., BasicAmount=1894.00}}", stored as Working's balance
            "debit_account_balance": daraja.b2b_balance_text(
                values.get("DebitPartyAffectedAccountBalance")
                or values.get("DebitAccountBalance")
                or values.get("DebitAccountCurrentBalance")
            ),
        },
        notify=True,
    )
    log_callback({"Result": result}, "Mpesa B2B Result", B2B_DOCTYPE, doc.name, "Completed")
    from . import schedule

    schedule.sync_line_later(doc)  # a payment schedule's line, from a job after this commits

    if outcome == "Paid":
        try:
            if is_employee(doc):
                employee_accounts.record_receiver_name(doc, doc.get("employee_mpesa_account"),
                                                       values.get("ReceiverPartyPublicName"))
            else:
                supplier_accounts.record_receiver_name(doc, values.get("ReceiverPartyPublicName"))
        except Exception:
            frappe.log_error(title="M-Pesa B2B receiver name check failed",
                             message=f"{doc.name}\n{frappe.get_traceback()}")
        frappe.enqueue(
            "mpsa_suite.money_out.b2b.book_payment_entry",
            queue="short",
            timeout=300,
            enqueue_after_commit=True,
            name=doc.name,
        )

    if outcome == "Paid" and not (cstr(values.get("ReceiverPartyPublicName")).strip()
                                  and cstr(values.get("DebitPartyCharges")).strip()):
        # a till's Result names no receiver and states no charge: Safaricom's status answer
        # for the receipt does (handover F2.4)
        from . import b2b_status

        b2b_status.ask_later(doc.name)


def handle_queue_timeout(payload: dict, ref: str | None) -> None:
    """Timed out in Safaricom's queue: the payment may still go through. Flag, never settle."""
    if ref and frappe.db.exists(B2B_DOCTYPE, ref):
        frappe.db.set_value(B2B_DOCTYPE, ref, "queue_timed_out", 1)
    log_callback(payload, "Mpesa B2B Queue Timeout", B2B_DOCTYPE, ref, "Failed")


# ---------------------------------------------------------------- validation
def is_employee(doc) -> bool:
    return bool(cstr(doc.get("employee")).strip())


def salary_slip(doc) -> str | None:
    return cstr(doc.get("salary_slip")).strip() or None


def validate_request(doc, at_submit: bool = False) -> None:
    """Every check a request must pass: on save, again at submit (under the document's lock) and,
    through check_ready_to_send, again right before the money leaves."""
    if is_employee(doc):
        _validate_employee_request(doc, at_submit)
        return
    if salary_slip(doc):
        frappe.throw("A Salary Slip is paid to its employee: pick the Employee.")
    if doc.get("employee_mpesa_account"):
        frappe.throw("An Employee M-Pesa Account is paid to its employee: pick the Employee.")
    dt, dn = owed.linked_document(doc)
    if dt:
        _take_from_document(doc, dt, dn)
    supplier_accounts.apply_destination(doc)
    _check_fields(doc)
    _check_settings(doc, fill=True)
    if not doc.supplier:
        doc.entry_mode = LATER  # nothing to book it on until someone says what it paid for
    elif not doc.get("entry_mode"):
        doc.entry_mode = AUTO
    hold = owed.supplier_hold(doc.supplier)
    if hold:
        frappe.throw(hold)
    if dt:
        if at_submit:
            with owed.doc_lock(dt, dn):
                _check_document(doc, dt, dn, for_update=True)
        else:
            _check_document(doc, dt, dn)
    if dt == "Purchase Invoice":
        _check_earlier_payouts(doc, at_submit)


def _validate_employee_request(doc, at_submit: bool) -> None:
    """A payment to an employee's approved bank paybill account, optionally for a Salary Slip."""
    if cstr(doc.get("supplier")).strip() or doc.get("supplier_mpesa_account"):
        frappe.throw("Pay a supplier or an employee, not both.")
    if cstr(doc.get("purchase_invoice")).strip() or cstr(doc.get("purchase_order")).strip():
        frappe.throw("A payment to an employee cannot pay a Purchase Invoice or Purchase Order.")
    _check_payer(doc, at_submit)
    slip = salary_slip(doc)
    if slip:
        _take_from_slip(doc, slip)
    employee_accounts.apply_destination(doc)
    _check_fields(doc)
    _check_settings(doc, fill=True)
    if not slip:
        doc.entry_mode = LATER  # nothing says what it paid for until someone makes the entry
    elif not doc.get("entry_mode"):
        doc.entry_mode = AUTO
    if slip:
        if at_submit:
            with owed.slip_lock(slip):
                _check_slip(doc, slip, for_update=True)
        else:
            _check_slip(doc, slip)


def _check_payer(doc, sending: bool) -> None:
    """Nobody pays themselves; and a payment with no Salary Slip, which nothing caps, is submitted
    and sent by a manager (ADMIN_ROLES) only."""
    problem = employee_accounts.own_payment_problem(doc.employee)
    if problem:
        frappe.throw(problem, frappe.PermissionError)
    if sending and not salary_slip(doc) and not employee_accounts.is_manager():
        frappe.throw(employee_accounts.NO_SLIP, frappe.PermissionError)


def _take_from_slip(doc, slip: str) -> None:
    """Company and Payroll Entry come from the slip, whose employee must be the request's."""
    values = owed.read_slip(slip)
    if not values:
        frappe.throw(f"Salary Slip {slip} not found")
    if values.employee != doc.employee:
        frappe.throw(f"{slip} belongs to {values.employee}, not {doc.employee}.")
    doc.company = values.company or doc.company
    if frappe.get_meta(B2B_DOCTYPE).has_field("payroll_entry"):
        doc.payroll_entry = values.get("payroll_entry")


def _check_slip(doc, slip: str, for_update: bool = False) -> None:
    """The slip can be paid and has at least this amount left, its payouts on their way and its
    planned lines counted (under the slip's lock at submit and send: payouts first, R4)."""
    result = owed.slip_left_to_pay(slip, exclude_request=doc.name, exclude_line=doc.get("schedule_line") or None,
                                   for_update=for_update)
    values = owed.read_slip(slip, for_update=for_update)
    problems = owed.slip_problems(values)
    if problems:
        frappe.throw(problems[0])
    if values.employee != doc.employee:
        frappe.throw(f"{slip} belongs to {values.employee}, not {doc.employee}.")
    if values.get("company") and doc.company != values.company:
        frappe.throw(f"{slip} belongs to {values.company}, not {doc.company or 'no company'}.")
    if flt(doc.amount) > result.left + 0.001:
        frappe.throw(f"KES {owed.money(doc.amount)} is more than is left to pay. {owed.explain(slip, result)}")


def _take_from_document(doc, dt: str, dn: str) -> None:
    """Supplier and company come from the invoice or order. A typed supplier must agree. The
    company always follows the document: Frappe fills a new request's company from the user's
    default, so a different one says nothing about the pick. The shortcode must then pay for
    the document's company (_check_settings)."""
    values = owed.read_document(dt, dn)
    if not values:
        frappe.throw(f"{dt} {dn} not found")
    if doc.supplier and doc.supplier != values.supplier:
        frappe.throw(f"{dn} belongs to {values.supplier}, not {doc.supplier}.")
    doc.supplier = values.supplier
    doc.company = values.company or doc.company


def _check_fields(doc) -> None:
    try:
        daraja.whole_shillings(doc.amount)
    except ValueError as e:
        frappe.throw(str(e))
    if doc.command_id not in daraja.B2B_COMMANDS:
        frappe.throw(f"Command must be one of {', '.join(daraja.B2B_COMMANDS)}")
    doc.receiver_shortcode = str(doc.receiver_shortcode or "").strip()
    if not doc.receiver_shortcode.isdigit():
        frappe.throw("Receiver must be the supplier's paybill or till number (digits only)")
    if doc.command_id == daraja.B2B_PAYBILL and not str(doc.account_reference or "").strip():
        frappe.throw("Paying a paybill needs the supplier's Account Number")
    if doc.command_id == daraja.B2B_BUY_GOODS:
        doc.account_reference = None
    if doc.requester:
        msisdn = normalize_msisdn(doc.requester)
        if not msisdn:
            frappe.throw(f"Invalid requester phone number {doc.requester!r}")
        doc.requester = msisdn


def _check_settings(doc, fill: bool = True) -> None:
    """The shortcode may send B2B, is not paying itself, is within its limit and pays for the
    request's company. ``fill``: take the company from Mpesa Settings when the request has none."""
    if not doc.mpesa_settings:
        return  # a mandatory field: the form asks for it, and sending refuses without it
    fields = ["name", "business_shortcode", "till_number", "sandbox", "transaction_limit", *apis.ALL]
    if frappe.get_meta("Mpesa Settings").has_field("company"):
        fields.append("company")
    settings = frappe.db.get_value("Mpesa Settings", doc.mpesa_settings, fields, as_dict=True)
    if not settings:
        frappe.throw(f"Mpesa Settings {doc.mpesa_settings} not found")
    apis.require(settings, apis.B2B)
    if doc.receiver_shortcode in {str(settings.business_shortcode or "").strip(), apis.initiator_party_a(settings)}:
        frappe.throw("Receiver is this business's own shortcode")
    if flt(settings.transaction_limit) and flt(doc.amount) > flt(settings.transaction_limit):
        frappe.throw(f"Amount is above this shortcode's transaction limit of {settings.transaction_limit}")
    company = settings.get("company")
    if company:
        if not doc.company and fill:
            doc.company = company
        elif doc.company != company:
            frappe.throw(f"Mpesa Settings {doc.mpesa_settings} pays for {company}, not {doc.company or 'no company'}.")


def _check_document(doc, dt: str, dn: str, for_update: bool = False) -> None:
    """The document can be paid and still needs at least this amount. ``for_update``: under the
    document's lock, payouts first and the document row last (R3, R4)."""
    # a schedule's request does not count its own line as planned
    result = owed.left_to_pay(dt, dn, exclude_request=doc.name, exclude_line=doc.get("schedule_line") or None,
                              for_update=for_update)
    values = owed.read_document(dt, dn, for_update=for_update)
    problems = owed.document_problems(dt, values)
    if problems:
        frappe.throw(problems[0])
    if doc.supplier and values.supplier != doc.supplier:
        frappe.throw(f"{dn} belongs to {values.supplier}, not {doc.supplier}.")
    if values.get("company") and doc.company != values.company:  # only server code can get here
        frappe.throw(f"{dn} belongs to {values.company}, not {doc.company or 'no company'}.")
    if flt(doc.amount) > result.left + 0.001:
        frappe.throw(f"KES {owed.money(doc.amount)} is more than is left to pay. {owed.explain(dn, result)}")


def validate_against_invoice(doc) -> None:
    """Kept for callers of the old name: the document check above."""
    dt, dn = owed.linked_document(doc)
    if dt:
        _check_document(doc, dt, dn)


def _check_earlier_payouts(doc, at_submit: bool) -> None:
    """M-Pesa money the supplier already got with no document may have paid this invoice."""
    earlier = owed.earlier_payouts(doc.supplier)
    if not earlier:
        return
    listed = "; ".join(e.label for e in earlier[:10])
    if at_submit and not cint(doc.get("checked_earlier_payouts")):
        frappe.throw(
            f"{doc.supplier} already got M-Pesa payments that are not on any invoice: {listed}. Check that none "
            "of them paid this invoice, then tick the box under Purchase Invoice."
        )
    if not at_submit and not (getattr(doc, "flags", None) or {}).get("mpesa_from_schedule"):
        frappe.msgprint(
            f"{doc.supplier} already got M-Pesa payments that are not on any invoice: {listed}. Check that none "
            "of them paid this invoice before you send this one.",
            title="Earlier M-Pesa payments",
            indicator="orange",
        )


def check_ready_to_send(doc) -> None:
    """Every check once more, right before the money leaves. Changes nothing and only throws,
    always before post(): server code may submit with flags.ignore_validate."""
    require(PAYOUT_ROLES)
    if is_employee(doc):
        _check_employee_ready(doc)
        return
    supplier_accounts.check_before_send(doc)
    if not doc.mpesa_settings:
        frappe.throw(f"{doc.name} has no Pay From (Mpesa Settings)")
    _check_settings(doc, fill=False)
    hold = owed.supplier_hold(doc.supplier)
    if hold:
        frappe.throw(hold)
    dt, dn = owed.linked_document(doc)
    if dt:
        with owed.doc_lock(dt, dn):
            _check_document(doc, dt, dn, for_update=True)


def _check_employee_ready(doc) -> None:
    if cstr(doc.get("supplier")).strip() or owed.document_of(doc) != (None, None):
        frappe.throw("Pay a supplier or an employee, not both.")
    _check_payer(doc, sending=True)
    employee_accounts.check_before_send(doc)
    if not doc.mpesa_settings:
        frappe.throw(f"{doc.name} has no Pay From (Mpesa Settings)")
    _check_settings(doc, fill=False)
    slip = salary_slip(doc)
    if slip:
        with owed.slip_lock(slip):
            _check_slip(doc, slip, for_update=True)


@frappe.whitelist()
def salary_left(salary_slip: str, request: str | None = None) -> dict:
    """What the request form shows for a Salary Slip: the employee, the company, what is left to
    pay and why, and anything that stops it being paid."""
    frappe.has_permission(owed.SLIP, "read", doc=salary_slip, throw=True)
    values = owed.read_slip(salary_slip)
    if not values:
        frappe.throw(f"Salary Slip {salary_slip} not found")
    result = owed.slip_left_to_pay(salary_slip, exclude_request=request)
    return {
        "employee": values.employee,
        "company": values.company,
        "payroll_entry": values.get("payroll_entry"),
        "owed": result.owed,
        "left": result.left,
        "parts": [p.label for p in result.parts],
        "problems": owed.slip_problems(values),
        "employee_mpesa_account": employee_accounts.pa.pick_account(
            employee_accounts.ACCOUNT_DOCTYPE, values.employee, (employee_accounts.BANK,)),
    }


@frappe.whitelist()
def amount_left(doctype: str, name: str, request: str | None = None) -> dict:
    """What the request form shows for an invoice or order: who and what to pay, how much is left
    and why, and anything that stops it being paid."""
    if doctype not in owed.DOC_FIELDS:
        frappe.throw("Pick a Purchase Invoice or a Purchase Order")
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    values = owed.read_document(doctype, name)
    if not values:
        frappe.throw(f"{doctype} {name} not found")
    result = owed.left_to_pay(doctype, name, exclude_request=request)
    filters = {"use_b2b": 1}
    if frappe.get_meta("Mpesa Settings").has_field("company"):
        filters["company"] = values.company
    shortcodes = [
        s.name
        for s in frappe.get_all("Mpesa Settings", filters=filters, fields=["name", "initiator_name"],
                                order_by="creation asc")
        if s.initiator_name
    ]
    earlier = owed.earlier_payouts(values.supplier) if doctype == "Purchase Invoice" else []
    return {
        "supplier": values.supplier,
        "company": values.company,
        "mpesa_settings": shortcodes,
        "owed": result.owed,
        "left": result.left,
        "parts": [p.label for p in result.parts],
        "summary": owed.summary(result),
        "problems": owed.document_problems(doctype, values),
        "supplier_hold": owed.supplier_hold(values.supplier),
        "earlier_payouts": [e.label for e in earlier],
        "supplier_mpesa_account": supplier_accounts.pick_account(values.supplier) if values.supplier else None,
    }


@frappe.whitelist()
def order_advances(purchase_orders) -> list:
    """M-Pesa or other advances still on these orders, for the headline of a draft invoice made
    from them ("press Get Advances Paid to use it")."""
    if isinstance(purchase_orders, str):
        purchase_orders = frappe.parse_json(purchase_orders)
    found = []
    for po in sorted({cstr(p).strip() for p in purchase_orders or []} - {""})[:20]:
        if not frappe.has_permission("Purchase Order", "read", doc=po):
            continue
        amount = owed.order_advance(po)
        if amount > owed.MIN:
            found.append({"purchase_order": po, "amount": amount})
    return found


# ---------------------------------------------------------------- accounting
def book_payment_entry(name: str) -> str | None:
    """The Result's job: the Payment Entry of a Paid request, then its statement lines. Idempotent."""
    from . import b2b_entries
    from .statement import after_payout

    entry = b2b_entries.book_automatically(name)
    after_payout(B2B_DOCTYPE, name)  # reconciled with the entry just booked
    return entry
