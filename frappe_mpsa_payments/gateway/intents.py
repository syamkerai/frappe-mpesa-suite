"""Payment Intent lifecycle: create -> prompt -> paid / failed / expired / cancelled.

The intent's name (e.g. MPI-26-10-000123) is used as the M-Pesa AccountReference, so the
money finds its way back whichever Safaricom message arrives: the STK callback, the
C2B confirmation, a Pull sweep, or a receipt code the customer types into a chat.
"""

import json
from urllib.parse import urlparse

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, get_url, now_datetime

from ..money_in.common import EXPRESS, REGISTER, parse_transtime, tolerance
from ..utils.phone import mask, normalize_msisdn
from .auth import ApiError

INTENT = "Mpesa Payment Intent"
OPEN = ("Created", "Prompted", "Failed")
TERMINAL = ("Paid", "Cancelled", "Expired")
MAX_PROMPTS = 5
LINKABLE = ("Sales Order", "Sales Invoice")


# ---------------------------------------------------------------- create
def create(client, *, amount, order_ref, phone=None, idempotency_key=None, description=None,
           reference_doctype=None, reference_name=None, callback_url=None, return_url=None,
           metadata=None, expires_in_minutes=None):
    order_ref = cstr(order_ref).strip()
    if not order_ref:
        raise ApiError(422, "invalid_request", "order_ref is required.")
    if len(order_ref) > 140:
        raise ApiError(422, "invalid_request", "order_ref is longer than 140 characters.")

    key = cstr(idempotency_key).strip() or None
    if key:
        existing = frappe.db.get_value(INTENT, {"client": client.name, "idempotency_key": key}, "name")
        if existing:
            doc = frappe.get_doc(INTENT, existing)
            if flt(doc.amount) != flt(amount) or doc.order_ref != order_ref:
                raise ApiError(409, "idempotency_conflict", "This idempotency_key was used for a different payment.")
            return doc, False

    amount = _valid_amount(client, amount)
    msisdn = None
    if phone:
        msisdn = normalize_msisdn(phone)
        if not msisdn:
            raise ApiError(422, "invalid_phone", "phone must be a Kenyan mobile number, e.g. 0712345678.")

    customer = None
    if reference_doctype or reference_name:
        if not cint(client.can_link_documents):
            raise ApiError(403, "not_allowed", "This client may not link payments to documents.")
        customer = _check_reference(client, reference_doctype, reference_name)

    for url, label in ((callback_url, "callback_url"), (return_url, "return_url")):
        if url:
            _check_url(client, url, label)

    doc = frappe.get_doc(
        {
            "doctype": INTENT,
            "client": client.name,
            "order_ref": order_ref,
            "idempotency_key": key,
            "status": "Created",
            "amount": amount,
            "phone": msisdn,
            "description": cstr(description)[:500] or None,
            "reference_doctype": reference_doctype if reference_name else None,
            "reference_name": reference_name or None,
            "customer": customer,
            "pay_token": frappe.generate_hash(length=32),
            "expires_at": add_to_date(now_datetime(), minutes=cint(expires_in_minutes) or cint(client.prompt_expiry_minutes) or 15),
            "callback_url": callback_url or None,
            "return_url": return_url or None,
            "metadata": json.dumps(metadata) if isinstance(metadata, (dict, list)) else (cstr(metadata) or None),
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc, True


def _valid_amount(client, amount) -> int:
    value = flt(amount)
    if value < 1 or value != int(value):
        raise ApiError(422, "invalid_amount", "amount must be a whole number of shillings, at least 1.")
    limits = [flt(client.max_amount)]
    settings_limit = flt(frappe.db.get_value("Mpesa Settings", client.mpesa_settings, "transaction_limit"))
    suite_limit = flt(frappe.db.get_single_value("M-Pesa Suite Settings", "stk_max_amount"))
    limits += [settings_limit, suite_limit]
    ceiling = min([x for x in limits if x > 0] or [250000])
    if value > ceiling:
        raise ApiError(422, "amount_too_large", f"amount exceeds the per-payment limit of KES {ceiling:,.0f}.")
    return int(value)


def _check_reference(client, doctype, name) -> str:
    if doctype not in LINKABLE or not name:
        raise ApiError(422, "invalid_reference", "reference_doctype must be Sales Order or Sales Invoice, with reference_name.")
    row = frappe.db.get_value(doctype, name, ["docstatus", "customer", "company"], as_dict=True)
    if not row or row.docstatus == 2:
        raise ApiError(404, "reference_not_found", f"{doctype} {name} not found.")
    company = frappe.db.get_value("Mpesa Settings", client.mpesa_settings, "company") if frappe.get_meta(
        "Mpesa Settings").has_field("company") else None
    if company and row.company != company:
        raise ApiError(404, "reference_not_found", f"{doctype} {name} not found.")
    return row.customer


def _check_url(client, url, label) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise ApiError(422, "invalid_url", f"{label} must be an absolute URL.")
    if parsed.scheme != "https" and not frappe.conf.developer_mode:
        raise ApiError(422, "invalid_url", f"{label} must use https.")
    if parsed.hostname.lower() not in client.allowed_hosts():
        raise ApiError(422, "invalid_url", f"{label} host is not in this client's allowed hosts.")


# ---------------------------------------------------------------- prompt
def send_prompt(intent, phone=None):
    """Send (or re-send) the STK prompt for an intent."""
    refresh(intent.name)
    intent.reload()
    if intent.status in TERMINAL:
        raise ApiError(409, "not_payable", f"This payment is already {intent.status.lower()}.")
    if get_datetime(intent.expires_at) < now_datetime():
        _set_status(intent, "Expired")
        raise ApiError(409, "not_payable", "This payment has expired.")
    if cint(intent.prompts_sent) >= MAX_PROMPTS:
        raise ApiError(429, "too_many_prompts", "Too many prompts for this payment; ask the customer to pay via paybill.")
    msisdn = normalize_msisdn(phone or intent.phone)
    if not msisdn:
        raise ApiError(422, "invalid_phone", "A Kenyan mobile number is required to send the prompt.")

    client = frappe.get_cached_doc("Mpesa API Client", intent.client)
    express = frappe.get_doc(
        {
            "doctype": EXPRESS,
            "settings": client.mpesa_settings,
            "phone_number": msisdn,
            "base_amount": flt(intent.amount),
            "amount": flt(intent.amount),
            "currency": "KES",
            "reference_doctype": INTENT,
            "reference_name": intent.name,  # becomes the AccountReference the customer sees
            "status": "In Progress",
            "transaction_title": cstr(intent.order_ref)[:140],
            "transaction_description": cstr(intent.description)[:500] or None,
        }
    )
    express.flags.ignore_permissions = True
    express.insert()
    express.submit()  # on_submit sends the STK push
    express.reload()

    intent.phone = msisdn
    intent.express_request = express.name
    intent.prompts_sent = cint(intent.prompts_sent) + 1
    if not express.checkout_request_id:
        reason = express.get("response_description") or express.get("result_desc") or "Safaricom did not accept the prompt."
        intent.failure_reason = reason
        _save(intent)
        raise ApiError(502, "prompt_failed", reason)
    intent.status = "Prompted"
    intent.failure_reason = None
    _save(intent)
    return intent


# ---------------------------------------------------------------- status
def refresh(name: str):
    """Bring an open intent up to date from STK results, C2B receipts and expiry."""
    intent = frappe.get_doc(INTENT, name)
    if intent.status in TERMINAL:
        return intent

    express = frappe.get_doc(EXPRESS, intent.express_request) if intent.express_request else None
    if (
        express and express.status == "Completed" and express.transaction_id
        and not _receipt_taken(express.transaction_id, intent.name)
    ):
        mark_paid(intent, express.transaction_id, express.amount,
                  parse_transtime(express.transaction_date) if express.transaction_date else now_datetime(),
                  None, register=frappe.db.get_value(REGISTER, {"transid": express.transaction_id, "docstatus": ["<", 2]}, "name"),
                  via="STK callback", express=express)
        return intent

    register = _register_for(intent, express)
    if register:
        reg = frappe.get_doc(REGISTER, register)
        mark_paid(intent, reg.transid, reg.transamount, parse_transtime(reg.transtime), reg.full_name, register=reg.name)
        return intent

    if express and express.status == "Failed" and intent.status == "Prompted":
        intent.failure_reason = express.result_desc or express.response_description or "Prompt failed"
        _set_status(intent, "Failed")
        return intent

    if intent.expires_at and get_datetime(intent.expires_at) < now_datetime():
        _set_status(intent, "Expired")
    return intent


def _register_for(intent, express):
    """A C2B receipt that pays this intent: account reference = intent name, or the prompt's receipt."""
    tol = tolerance()
    rows = frappe.get_all(
        REGISTER,
        filters={"billrefnumber": intent.name, "docstatus": ["<", 2]},
        fields=["name", "transamount", "transid"],
        order_by="creation asc",
    )
    for row in rows:
        if flt(row.transamount) + tol >= flt(intent.amount) and not _receipt_taken(row.transid, intent.name):
            return row.name
    return None


def _receipt_taken(receipt: str, intent_name: str) -> bool:
    return bool(frappe.db.exists(INTENT, {"receipt": receipt, "name": ["!=", intent_name]}))


def mark_paid(intent, receipt, amount, paid_at, payer_name, register=None, via=None, express=None):
    if _receipt_taken(receipt, intent.name):
        raise ApiError(409, "receipt_already_used", f"Receipt {receipt} already paid another order.")
    intent.receipt = receipt
    intent.paid_amount = flt(amount)
    intent.paid_at = paid_at or now_datetime()
    intent.payer_name = payer_name
    intent.register = register
    intent.failure_reason = None
    _set_status(intent, "Paid")
    if register:
        _hand_to_accounting(intent, register, via)
    elif express is not None:
        _book_from_express(intent, express)
    return intent


def _book_from_express(intent, express) -> None:
    """STK paid and the callback arrived: upstream drops the duplicate C2B register, so book
    the receipt directly against the intent's document (same rules as register booking)."""
    from ..frappe_mpsa_payments.api.payment_entry import create_payment_entry
    from ..money_in.common import enabled, log_match, mode_of_payment_for

    if not (enabled("enable_matching") and intent.customer and intent.reference_doctype in LINKABLE):
        return
    if frappe.db.exists("Payment Entry", {"reference_no": express.transaction_id, "docstatus": ["<", 2]}):
        return
    ref = frappe.db.get_value(
        intent.reference_doctype, intent.reference_name,
        ["docstatus", "company", "outstanding_amount" if intent.reference_doctype == "Sales Invoice" else "advance_paid",
         "rounded_total", "grand_total"],
        as_dict=True,
    )
    if not ref or ref.docstatus != 1:
        log_match("Matched", "Reference", express=express, once=False, reference_doctype=intent.reference_doctype,
                  reference_name=intent.reference_name, customer=intent.customer,
                  details=f"Paid by STK for intent {intent.name}; document not submitted, book manually.")
        return
    owed = flt(ref.outstanding_amount) if intent.reference_doctype == "Sales Invoice" else (
        flt(ref.rounded_total or ref.grand_total) - flt(ref.advance_paid))
    allocated = min(flt(express.amount), owed)
    refs = [{"reference_doctype": intent.reference_doctype, "reference_name": intent.reference_name,
             "allocated_amount": allocated}] if allocated > 0 else None
    paid_on = (parse_transtime(express.transaction_date) or now_datetime()).date()
    cost_center = frappe.db.get_value("Mpesa Settings", express.settings, "cost_center") if frappe.get_meta(
        "Mpesa Settings").has_field("cost_center") else None
    pe = create_payment_entry(ref.company, intent.customer, flt(express.amount), "KES", mode_of_payment_for(express.settings),
                              "Customer", paid_on, express.transaction_id, paid_on, cost_center, 1, references=refs)
    log_match("Booked", "Reference", express=express, customer=intent.customer, payment_entry=pe.name,
              reference_doctype=intent.reference_doctype, reference_name=intent.reference_name,
              details=f"Intent {intent.name}")


def _hand_to_accounting(intent, register_name, via=None) -> None:
    """Point the receipt at the intent's document so the money-in pipeline books it there."""
    from ..money_in import booking
    from ..money_in.common import enabled, log_match
    from ..money_in.matching import Match

    reg = frappe.get_doc(REGISTER, register_name)
    if reg.docstatus != 0 or reg.get("payment_entry"):
        return
    match = Match(method=via or "Reference", customer=intent.customer or reg.get("customer"),
                  details=f"Payment intent {intent.name} ({intent.order_ref})")
    if intent.reference_doctype in LINKABLE and intent.reference_name:
        match.update(doctype=intent.reference_doctype, name=intent.reference_name)
    if not match.customer:
        log_match("Matched", "Reference", register=reg, once=True, details=match.details + "; no customer, left for staff")
        return
    if enabled("enable_matching"):
        booking.apply_match(reg, match)
    else:
        booking.stamp(reg, match)


def cancel(intent):
    refresh(intent.name)
    intent.reload()
    if intent.status == "Paid":
        raise ApiError(409, "already_paid", "This payment is already paid and cannot be cancelled.")
    if intent.status in ("Cancelled", "Expired"):
        return intent
    _set_status(intent, "Cancelled")
    return intent


def _set_status(intent, status: str) -> None:
    from .webhooks import queue

    changed = intent.status != status
    intent.status = status
    _save(intent)
    if changed and status in ("Paid", "Failed", "Expired", "Cancelled"):
        queue(intent)


def _save(intent) -> None:
    intent.flags.ignore_permissions = True
    intent.save()


# ---------------------------------------------------------------- output
def serialize(intent) -> dict:
    client = frappe.get_cached_doc("Mpesa API Client", intent.client)
    shortcode = frappe.db.get_value("Mpesa Settings", client.mpesa_settings, "business_shortcode")
    return {
        "id": intent.name,
        "order_ref": intent.order_ref,
        "status": intent.status.lower(),
        "amount": flt(intent.amount),
        "currency": "KES",
        "phone": mask(intent.phone) if intent.phone else None,
        "receipt": intent.receipt,
        "paid_amount": flt(intent.paid_amount) or None,
        "paid_at": intent.paid_at and get_datetime(intent.paid_at).isoformat(),
        "payer_name": intent.payer_name,
        "failure_reason": intent.failure_reason,
        "expires_at": intent.expires_at and get_datetime(intent.expires_at).isoformat(),
        "pay_url": get_url(f"/pay/{intent.pay_token}"),
        "paybill": {"business_number": shortcode, "account_number": intent.name},
        "reference": {"doctype": intent.reference_doctype, "name": intent.reference_name} if intent.reference_name else None,
        "metadata": _json(intent.metadata),
    }


def _json(value):
    try:
        return json.loads(value) if value else None
    except ValueError:
        return value


def sync_open(limit: int = 200) -> None:
    """Scheduler: settle prompts whose callback never came, expire stale intents."""
    names = frappe.get_all(
        INTENT, filters={"status": ["in", list(OPEN)]}, pluck="name", order_by="creation desc", limit=limit
    )
    for name in names:
        frappe.db.savepoint("mpesa_intent")
        try:
            refresh(name)
        except Exception:
            frappe.db.rollback(save_point="mpesa_intent")
            frappe.log_error(frappe.get_traceback(), f"M-Pesa intent refresh failed for {name}")
