"""Daraja B2B BusinessPayBill / BusinessBuyGoods supplier payments (docs/daraja_api_reference.md §9).

Same safety rules as B2C payouts: sent only from a submitted document under a row lock,
whole shillings, the ack sets ``Initiated`` (never ``Paid``), only the Result sets
Paid/Failed, a re-sent Result is ignored, and the queue timeout has its own URL.

A request may pay a Purchase Invoice, a Purchase Order (an advance its invoice picks up under
Get Advances) or neither. Every check runs on save, again at submit under the document's lock
and again right before sending: the approved destination, the shortcode, the supplier's hold,
the document, and what is left to pay with B2B, B2C, draft entries and order advances counted
(money_out.owed). The entry is booked when M-Pesa confirms, or later (money_out.b2b_entries).
"""

import frappe
from frappe.utils import cint, cstr, flt, now_datetime

from ..daraja import apis
from ..daraja import money_out as daraja
from ..permissions import PAYOUT_ROLES, require
from ..utils.phone import normalize_msisdn
from . import owed, supplier_accounts
from .common import apply_ack, callback_url, initiator_settings, lock_for_send, log_callback, post
from .results import find_request

B2B_DOCTYPE = "Mpesa B2B Payment Request"
FINAL = ("Paid", "Failed")
# When Paid: book the Payment Entry at once, or wait for Make Entry (b2b_entries).
AUTO, LATER = "Book automatically", "Make the entry later"
# A Result that meets a deadlock or a row lock held past MariaDB's lock wait is recorded again
# from a job, this many tries in all: Safaricom may never send it again.
RESULT_TRIES = 5


def send_b2b(name: str) -> None:
    doc = lock_for_send(B2B_DOCTYPE, name)
    check_ready_to_send(doc)
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
    # Nothing below may raise: the request has (maybe) left the building.
    data, error = post(
        settings, daraja.B2B_ENDPOINT, payload, doctype=B2B_DOCTYPE, name=doc.name, description="Mpesa B2B"
    )
    apply_ack(doc, data, error)


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
            "trans_completed_time": daraja.parse_daraja_datetime(values.get("TransCompletedTime")),
            "receiver_public_name": values.get("ReceiverPartyPublicName"),
            "debit_party_charges": values.get("DebitPartyCharges"),
            # live results name the account ("Working Account|KES|1476.00|..."); Daraja's sample
            # sends DebitAccountBalance instead
            "debit_account_balance": values.get("DebitPartyAffectedAccountBalance")
            or values.get("DebitAccountBalance")
            or values.get("DebitAccountCurrentBalance"),
        },
        notify=True,
    )
    log_callback({"Result": result}, "Mpesa B2B Result", B2B_DOCTYPE, doc.name, "Completed")

    if outcome == "Paid":
        try:
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


def handle_queue_timeout(payload: dict, ref: str | None) -> None:
    """Timed out in Safaricom's queue: the payment may still go through. Flag, never settle."""
    if ref and frappe.db.exists(B2B_DOCTYPE, ref):
        frappe.db.set_value(B2B_DOCTYPE, ref, "queue_timed_out", 1)
    log_callback(payload, "Mpesa B2B Queue Timeout", B2B_DOCTYPE, ref, "Failed")


# ---------------------------------------------------------------- validation
def validate_request(doc, at_submit: bool = False) -> None:
    """Every check a request must pass: on save, again at submit (under the document's lock) and,
    through check_ready_to_send, again right before the money leaves."""
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
    result = owed.left_to_pay(dt, dn, exclude_request=doc.name, for_update=for_update)
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
    if not at_submit:
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
