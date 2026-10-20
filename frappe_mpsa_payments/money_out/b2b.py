"""Daraja B2B BusinessPayBill / BusinessBuyGoods supplier payments (docs/daraja_api_reference.md §9).

Same safety rules as B2C payouts: sent only from a submitted document under a row lock,
whole shillings, the ack sets ``Initiated`` (never ``Paid``), only the Result sets
Paid/Failed, a re-sent Result is ignored, and the queue timeout has its own URL.
"""

import frappe
from frappe.utils import flt, getdate, nowdate

from ..daraja import apis
from ..daraja import money_out as daraja
from ..utils.phone import normalize_msisdn
from . import supplier_accounts
from .common import apply_ack, callback_url, initiator_settings, lock_for_send, log_callback, post
from .results import find_request

B2B_DOCTYPE = "Mpesa B2B Payment Request"
FINAL = ("Paid", "Failed")
# Requests that still count against the invoice's outstanding amount.
LIVE = ("Not Initiated", "Initiated", "Unknown", "Paid")


def send_b2b(name: str) -> None:
    doc = lock_for_send(B2B_DOCTYPE, name)
    supplier_accounts.check_before_send(doc)
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


def handle_result(result: dict, ref: str | None) -> None:
    doc = find_request(B2B_DOCTYPE, result, ref)
    if not doc:
        log_callback({"Result": result}, "Mpesa B2B Result (unmatched)", B2B_DOCTYPE, ref, "Failed")
        return
    if doc.status in FINAL and doc.result_code not in (None, ""):
        return  # Safaricom re-sent a result we already recorded; never book twice

    values = daraja.result_values(result)
    outcome = daraja.b2b_outcome(result)
    doc.db_set(
        {
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
            "debit_account_balance": values.get("DebitAccountBalance"),
        },
        notify=True,
    )
    log_callback({"Result": result}, "Mpesa B2B Result", B2B_DOCTYPE, doc.name, "Completed")

    if outcome == "Paid":
        try:
            supplier_accounts.record_receiver_name(doc, values.get("ReceiverPartyPublicName"))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"M-Pesa B2B {doc.name}: receiver name check failed")
        frappe.enqueue(
            "frappe_mpsa_payments.money_out.b2b.book_payment_entry",
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
def validate_request(doc) -> None:
    if doc.purchase_invoice and not doc.supplier:
        doc.supplier = frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "supplier")
    supplier_accounts.apply_destination(doc)
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

    settings = doc.mpesa_settings and frappe.db.get_value(
        "Mpesa Settings",
        doc.mpesa_settings,
        ["name", "business_shortcode", "till_number", "sandbox", "transaction_limit", *apis.ALL],
        as_dict=True,
    )
    if settings:
        apis.require(settings, apis.B2B)
    if settings and doc.receiver_shortcode in {
        str(settings.business_shortcode or "").strip(),
        apis.initiator_party_a(settings),
    }:
        frappe.throw("Receiver is this business's own shortcode")
    if settings and flt(settings.transaction_limit) and flt(doc.amount) > flt(settings.transaction_limit):
        frappe.throw(f"Amount is above this shortcode's transaction limit of {settings.transaction_limit}")

    if doc.purchase_invoice:
        validate_against_invoice(doc)


def validate_against_invoice(doc) -> None:
    pi = frappe.db.get_value(
        "Purchase Invoice",
        doc.purchase_invoice,
        ["docstatus", "supplier", "company", "outstanding_amount", "currency"],
        as_dict=True,
    )
    if not pi or pi.docstatus != 1:
        frappe.throw(f"Purchase Invoice {doc.purchase_invoice} must be submitted")
    if pi.currency and pi.currency != "KES":
        frappe.throw(f"Purchase Invoice {doc.purchase_invoice} is in {pi.currency}; M-Pesa pays KES only")
    if doc.supplier and doc.supplier != pi.supplier:
        frappe.throw(f"Purchase Invoice {doc.purchase_invoice} belongs to {pi.supplier}, not {doc.supplier}")
    doc.supplier = pi.supplier
    doc.company = doc.company or pi.company

    # Other requests not yet booked still count against the outstanding amount, so two
    # requests (or a request and a manual payment) cannot pay the same invoice twice.
    pending = frappe.get_all(
        B2B_DOCTYPE,
        filters={
            "purchase_invoice": doc.purchase_invoice,
            "name": ["!=", doc.name or ""],
            "docstatus": ["<", 2],
            "status": ["in", LIVE],
            "payment_entry": ["is", "not set"],
        },
        pluck="amount",
        order_by="creation asc",
    )
    available = flt(pi.outstanding_amount) - sum(flt(a) for a in pending)
    if flt(doc.amount) > available + 0.001:
        frappe.throw(
            f"Amount {doc.amount} is more than what is left to pay on {doc.purchase_invoice} ({available})"
        )


# ---------------------------------------------------------------- accounting
def book_payment_entry(name: str) -> str | None:
    """Payment Entry for a Paid request. Idempotent; errors are logged, not raised."""
    frappe.db.get_value(B2B_DOCTYPE, name, "name", for_update=True)
    doc = frappe.get_doc(B2B_DOCTYPE, name)
    if doc.status != "Paid" or not doc.transaction_id:
        return None
    if doc.payment_entry and frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus") in (0, 1):
        return doc.payment_entry
    existing = frappe.db.get_value(
        "Payment Entry",
        {"reference_no": doc.transaction_id, "docstatus": ["<", 2], "payment_type": "Pay"},
        "name",
        order_by="creation asc",
    )
    if existing:
        doc.db_set("payment_entry", existing)
        return existing
    if not doc.supplier:
        doc.add_comment("Comment", "Paid, but no Supplier is set: book the Payment Entry by hand.")
        return None

    savepoint = "mpesa_b2b_pe"
    frappe.db.savepoint(savepoint)
    try:
        pe = _build_payment_entry(doc)
        pe.insert(ignore_permissions=True)
        if frappe.db.get_value("Mpesa Settings", doc.mpesa_settings, "submit_b2c_accounting_entries"):
            pe.submit()
        doc.db_set("payment_entry", pe.name)
        return pe.name
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        frappe.log_error(frappe.get_traceback(), f"M-Pesa B2B {name}: Payment Entry failed")
        doc.add_comment("Comment", "Paid, but the Payment Entry could not be created. See Error Log.")
        return None


def _build_payment_entry(doc):
    from erpnext.accounts.party import get_party_account

    from ..money_in.common import bank_account_for, mode_of_payment_for

    bank_account = bank_account_for(doc.mpesa_settings)
    if not bank_account:
        frappe.throw(f"Map a Bank Account on Mpesa Settings {doc.mpesa_settings} (Accounting Mapping)")
    bank = frappe.db.get_value("Bank Account", bank_account, ["account", "company"], as_dict=True)
    company = doc.company or bank.company
    amount = flt(doc.amount)
    reference_date = getdate(doc.trans_completed_time) if doc.trans_completed_time else getdate(nowdate())

    if doc.purchase_invoice:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        pe = get_payment_entry(
            "Purchase Invoice", doc.purchase_invoice, party_amount=amount, bank_account=bank.account
        )
        for ref in pe.references:
            if ref.reference_doctype == "Purchase Invoice" and ref.reference_name == doc.purchase_invoice:
                ref.allocated_amount = min(amount, flt(ref.outstanding_amount))
    else:
        pe = frappe.new_doc("Payment Entry")
        pe.payment_type = "Pay"
        pe.company = company
        pe.party_type = "Supplier"
        pe.party = doc.supplier
        pe.paid_from = bank.account
        pe.paid_to = get_party_account("Supplier", doc.supplier, company)
        pe.source_exchange_rate = 1
        pe.target_exchange_rate = 1

    pe.paid_amount = amount
    pe.received_amount = amount
    pe.bank_account = bank_account
    pe.mode_of_payment = mode_of_payment_for(doc.mpesa_settings) or pe.get("mode_of_payment")
    pe.reference_no = doc.transaction_id
    pe.reference_date = reference_date
    pe.posting_date = reference_date
    pe.remarks = f"M-Pesa B2B {doc.command_id} to {doc.receiver_shortcode} ({doc.name}), receipt {doc.transaction_id}"
    pe.flags.ignore_permissions = True
    return pe
