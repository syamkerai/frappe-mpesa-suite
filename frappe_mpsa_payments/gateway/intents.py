"""Payment Intent lifecycle: create -> prompt -> paid / failed / expired / cancelled.

The intent's name (e.g. MPI-26-10-000123) is used as the M-Pesa AccountReference, so the
money finds its way back whichever Safaricom message arrives: the STK callback, the
C2B confirmation, a Pull sweep, or a receipt code the customer types into a chat.

Rules that keep money safe:
- Marking an intent Paid (and its webhook) is saved before any accounting; a booking
  failure is logged to Mpesa Match Log and never undoes or blocks the payment status.
- Intents paid through an STK prompt are booked straight from the receipt (Payment Entry
  with the receipt as reference). Their C2B register is never submitted, because upstream
  cancels/deletes that register when the STK callback lands later.
- A receipt pays at most one intent (named lock + unique index on ``receipt``).
- Money that lands after an intent expired or was cancelled still marks it Paid.
"""

import json
import math
import re
from urllib.parse import urlparse

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, get_url, now_datetime, strip_html

from ..money_in.common import EXPRESS, REGISTER, log_match, named_lock, parse_transtime, tolerance
from ..utils.phone import mask, normalize_msisdn
from .auth import ApiError

INTENT = "Mpesa Payment Intent"
OPEN = ("Created", "Prompted", "Failed")
TERMINAL = ("Paid", "Cancelled", "Expired")
MAX_PROMPTS = 5
RESEND_AFTER_SECONDS = 90
MAX_METADATA_BYTES = 4096
LINKABLE = ("Sales Order", "Sales Invoice")
_BAD_URL_CHARS = re.compile(r"[\s\\\x00-\x1f\x7f]")


# ---------------------------------------------------------------- create
def _text(value, field: str, limit: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ApiError(422, "invalid_request", f"{field} must be text.")
    return strip_html(value).strip()[:limit] or None


def create(client, *, amount, order_ref, phone=None, idempotency_key=None, description=None,
           reference_doctype=None, reference_name=None, callback_url=None, return_url=None,
           metadata=None, expires_in_minutes=None):
    order_ref = _text(order_ref, "order_ref", 140)
    if not order_ref:
        raise ApiError(422, "invalid_request", "order_ref is required.")
    description = _text(description, "description", 500)
    key = _text(idempotency_key, "idempotency_key", 140)

    with named_lock(f"intent-idem:{client.name}:{key or frappe.generate_hash(length=12)}"):
        if key:
            existing = frappe.db.get_value(
                INTENT, {"client": client.name, "idempotency_key": key}, "name", for_update=True
            )
            if existing:
                doc = frappe.get_doc(INTENT, existing)
                if flt(doc.amount) != flt(amount) or doc.order_ref != order_ref:
                    raise ApiError(409, "idempotency_conflict", "This idempotency_key was used for a different payment.")
                return doc, False
        doc = _new_intent(client, amount, order_ref, phone, key, description, reference_doctype, reference_name,
                          callback_url, return_url, metadata, expires_in_minutes)
    return doc, True


def _new_intent(client, amount, order_ref, phone, key, description, reference_doctype, reference_name,
                callback_url, return_url, metadata, expires_in_minutes):
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

    if metadata is not None and not isinstance(metadata, (dict, list, str)):
        raise ApiError(422, "invalid_request", "metadata must be a JSON object, list or string.")
    meta_text = json.dumps(metadata) if isinstance(metadata, (dict, list)) else (metadata or None)
    if meta_text and len(meta_text.encode()) > MAX_METADATA_BYTES:
        raise ApiError(422, "invalid_request", f"metadata is larger than {MAX_METADATA_BYTES} bytes.")

    minutes = max(1, cint(expires_in_minutes) or cint(client.prompt_expiry_minutes) or 15)
    doc = frappe.get_doc(
        {
            "doctype": INTENT,
            "client": client.name,
            "order_ref": order_ref,
            "idempotency_key": key,
            "status": "Created",
            "amount": amount,
            "phone": msisdn,
            "description": description,
            "reference_doctype": reference_doctype if reference_name else None,
            "reference_name": reference_name or None,
            "customer": customer,
            "pay_token": frappe.generate_hash(length=32),
            "expires_at": add_to_date(now_datetime(), minutes=minutes),
            "callback_url": callback_url or None,
            "return_url": return_url or None,
            "metadata": meta_text,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc


def _valid_amount(client, amount) -> int:
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_amount", "amount must be a number.")
    if not math.isfinite(value) or value < 1 or value != int(value):
        raise ApiError(422, "invalid_amount", "amount must be a whole number of shillings, at least 1.")
    limits = [flt(client.max_amount)]
    limits.append(flt(frappe.db.get_value("Mpesa Settings", client.mpesa_settings, "transaction_limit")))
    limits.append(flt(frappe.db.get_single_value("M-Pesa Suite Settings", "stk_max_amount")))
    ceiling = min([x for x in limits if x > 0] or [250000])
    if value > ceiling:
        raise ApiError(422, "amount_too_large", f"amount exceeds the per-payment limit of KES {ceiling:,.0f}.")
    return int(value)


def client_company(client) -> str | None:
    if not frappe.get_meta("Mpesa Settings").has_field("company"):
        return None
    return frappe.db.get_value("Mpesa Settings", client.mpesa_settings, "company")


def _check_reference(client, doctype, name) -> str:
    if doctype not in LINKABLE or not name or not isinstance(name, str):
        raise ApiError(422, "invalid_reference", "reference_doctype must be Sales Order or Sales Invoice, with reference_name.")
    row = frappe.db.get_value(doctype, name, ["docstatus", "customer", "company"], as_dict=True)
    company = client_company(client)
    if not row or row.docstatus == 2 or (company and row.company != company):
        raise ApiError(404, "reference_not_found", f"{doctype} {name} not found.")
    return row.customer


def _check_url(client, url, label) -> None:
    if not isinstance(url, str) or _BAD_URL_CHARS.search(url) or len(url) > 1000:
        raise ApiError(422, "invalid_url", f"{label} is not a valid URL.")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError(422, "invalid_url", f"{label} must be an absolute URL without credentials.")
    if parsed.scheme != "https" and not frappe.conf.developer_mode:
        raise ApiError(422, "invalid_url", f"{label} must use https.")
    if parsed.hostname.lower() not in client.allowed_hosts():
        raise ApiError(422, "invalid_url", f"{label} host is not in this client's allowed hosts.")
    if label == "callback_url" and not frappe.conf.developer_mode:
        from .netguard import is_public_url

        if not is_public_url(url):
            raise ApiError(422, "invalid_url", "callback_url must point to a public internet address.")


# ---------------------------------------------------------------- prompt
def _prompts(intent_name: str) -> list:
    return frappe.get_all(
        EXPRESS,
        filters={"reference_doctype": INTENT, "reference_name": intent_name, "docstatus": 1},
        fields=["name", "status", "transaction_id", "amount", "transaction_date", "creation", "result_desc",
                "response_description", "settings"],
        order_by="creation desc",
    )


def send_prompt(intent, phone=None):
    """Send (or re-send) the STK prompt for an intent."""
    refresh(intent.name)
    intent.reload()
    if intent.status in TERMINAL:
        raise ApiError(409, "not_payable", f"This payment is already {intent.status.lower()}.")
    if get_datetime(intent.expires_at) < now_datetime():
        _set_status(intent, "Expired")
        raise ApiError(409, "not_payable", "This payment has expired.", rollback=False)
    if cint(intent.prompts_sent) >= MAX_PROMPTS:
        raise ApiError(429, "too_many_prompts", "Too many prompts for this payment; ask the customer to pay via paybill.")
    last = _prompts(intent.name)[:1]
    if last and last[0].status == "In Progress" and (
        now_datetime() - get_datetime(last[0].creation)
    ).total_seconds() < RESEND_AFTER_SECONDS:
        raise ApiError(409, "prompt_pending", "A prompt was just sent; wait for it to finish before sending another.")
    msisdn = normalize_msisdn(phone or intent.phone)
    if not msisdn:
        raise ApiError(422, "invalid_phone", "A Kenyan mobile number is required to send the prompt.")

    client = frappe.get_cached_doc("Mpesa API Client", intent.client)
    intent.phone = msisdn
    intent.prompts_sent = cint(intent.prompts_sent) + 1
    _save(intent)  # count the attempt even if Safaricom refuses it

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
    try:
        express.submit()  # on_submit sends the STK push; upstream commits inside
    except Exception as e:
        # the prompt never reached the customer; keep the attempt on record
        intent.reload()
        intent.failure_reason = strip_html(cstr(e))[:500] or "Safaricom did not accept the prompt."
        _save(intent)
        raise ApiError(502, "prompt_failed", intent.failure_reason, rollback=False)
    express.reload()

    intent.reload()
    intent.express_request = express.name
    if not express.checkout_request_id:
        intent.failure_reason = (
            express.get("response_description") or express.get("result_desc") or "Safaricom did not accept the prompt."
        )
        _save(intent)
        raise ApiError(502, "prompt_failed", intent.failure_reason, rollback=False)
    intent.status = "Prompted"
    intent.failure_reason = None
    _save(intent)
    return intent


# ---------------------------------------------------------------- status
def refresh(name: str):
    """Bring an intent up to date from STK results, C2B receipts and expiry.

    Paid is final. Expired/Cancelled intents are still checked for money that arrived late.
    """
    intent = frappe.get_doc(INTENT, name)
    if intent.status == "Paid":
        return intent

    prompts = _prompts(intent.name)
    for p in prompts:  # any prompt may be the one the customer answered, not just the latest
        if p.status == "Completed" and p.transaction_id and not _receipt_taken(p.transaction_id, intent.name):
            paid_at = parse_transtime(p.transaction_date) if p.transaction_date else now_datetime()
            mark_paid(intent, p.transaction_id, p.amount, paid_at, None, via="STK callback", settings=p.settings)
            return intent

    register = _register_for(intent)
    if register:
        reg = frappe.db.get_value(REGISTER, register, ["transid", "transamount", "transtime", "full_name", "businessshortcode"],
                                  as_dict=True)
        mark_paid(intent, reg.transid, reg.transamount, parse_transtime(reg.transtime), reg.full_name, register=register)
        return intent

    if intent.status in TERMINAL:
        return intent

    if prompts and prompts[0].status == "Failed" and intent.status == "Prompted":
        intent.failure_reason = prompts[0].result_desc or prompts[0].response_description or "Prompt failed"
        _set_status(intent, "Failed")
        return intent

    if intent.expires_at and get_datetime(intent.expires_at) < now_datetime():
        _set_status(intent, "Expired")
    return intent


def _register_for(intent):
    """A C2B receipt whose account number is this intent, covering its amount, not used elsewhere."""
    tol = tolerance()
    for row in frappe.get_all(
        REGISTER,
        filters={"billrefnumber": intent.name, "docstatus": ["<", 2]},
        fields=["name", "transamount", "transid"],
        order_by="creation asc",
    ):
        if flt(row.transamount) + tol >= flt(intent.amount) and not _receipt_taken(row.transid, intent.name):
            return row.name
    return None


def _receipt_taken(receipt: str, intent_name: str) -> bool:
    return bool(frappe.db.exists(INTENT, {"receipt": receipt, "name": ["!=", intent_name]}))


def mark_paid(intent, receipt, amount, paid_at, payer_name, register=None, via=None, settings=None):
    """Record the payment (locked per receipt), notify, then hand the money to accounting."""
    with named_lock(f"receipt:{receipt}"):
        if frappe.db.get_value(INTENT, {"receipt": receipt, "name": ["!=", intent.name]}, "name", for_update=True):
            raise ApiError(409, "receipt_already_used", f"Receipt {receipt} already paid another order.")
        intent.reload()
        if intent.status == "Paid":
            return intent
        intent.receipt = receipt
        intent.paid_amount = flt(amount)
        intent.paid_at = paid_at or now_datetime()
        intent.payer_name = payer_name
        intent.register = register  # plain text: upstream may delete the register later
        intent.failure_reason = None
        _set_status(intent, "Paid")

    frappe.db.savepoint("mpesa_intent_booking")
    try:
        _book(intent, receipt, flt(amount), intent.paid_at, register, via, settings)
    except Exception:
        try:
            frappe.db.rollback(save_point="mpesa_intent_booking")
        except Exception:
            pass  # upstream committed mid-way; nothing left to undo
        log_match("Error", via or "Reference", reference_doctype=INTENT, reference_name=intent.name, once=True,
                  details=f"Payment recorded but booking failed: {frappe.get_traceback()[-800:]}")
    return intent


# ---------------------------------------------------------------- accounting
def _book(intent, receipt, amount, paid_at, register, via, settings) -> None:
    from ..money_in import booking
    from ..money_in.common import enabled
    from ..money_in.matching import Match

    if not enabled("enable_matching"):
        return
    stk_flow = bool(_prompts(intent.name))
    if stk_flow or not register:
        # Never submit the register for prompted intents: when the STK callback lands later,
        # upstream cancels (or deletes) any register with the same receipt.
        _book_direct(intent, receipt, amount, paid_at, settings or _settings_for(intent))
        return

    reg = frappe.get_doc(REGISTER, register)
    if reg.docstatus != 0 or reg.get("payment_entry"):
        return
    match = Match(method=via or "Reference", customer=intent.customer or reg.get("customer"),
                  details=f"Payment intent {intent.name} ({intent.order_ref})")
    if intent.reference_doctype in LINKABLE and intent.reference_name:
        match.update(doctype=intent.reference_doctype, name=intent.reference_name)
    if not match.customer:
        log_match("Matched", "Reference", register=reg, once=True, details=match.details + "; no customer, left for staff")
        return
    booking.apply_match(reg, match)


def _settings_for(intent) -> str:
    return frappe.db.get_value("Mpesa API Client", intent.client, "mpesa_settings")


def _book_direct(intent, receipt, amount, paid_at, settings_name) -> None:
    """Payment Entry straight from the receipt, against the intent's document (or on account)."""
    from ..frappe_mpsa_payments.api.payment_entry import create_payment_entry
    from ..money_in.common import mode_of_payment_for

    if not intent.customer:
        log_match("Matched", "Reference", reference_doctype=INTENT, reference_name=intent.name, once=True,
                  details=f"Paid ({receipt}) but the intent has no customer; book it manually.")
        return
    with named_lock(f"book:{receipt}"):
        if frappe.db.get_value("Payment Entry", {"reference_no": receipt, "docstatus": ["<", 2]}, "name", for_update=True):
            return
        refs, company = None, None
        if intent.reference_doctype in LINKABLE and intent.reference_name:
            dt = intent.reference_doctype
            ref = frappe.db.get_value(
                dt, intent.reference_name,
                ["docstatus", "company", "outstanding_amount" if dt == "Sales Invoice" else "advance_paid",
                 "rounded_total", "grand_total"], as_dict=True,
            )
            company = ref and ref.company
            if ref and ref.docstatus == 1:
                owed = flt(ref.outstanding_amount) if dt == "Sales Invoice" else (
                    flt(ref.rounded_total or ref.grand_total) - flt(ref.advance_paid))
                allocated = min(amount, owed)
                if allocated > 0:
                    refs = [{"reference_doctype": dt, "reference_name": intent.reference_name, "allocated_amount": allocated}]
            elif ref:
                log_match("Matched", "Reference", reference_doctype=dt, reference_name=intent.reference_name, once=True,
                          details=f"Paid ({receipt}, intent {intent.name}); booked on account because the document is a draft.")
        company = company or frappe.db.get_value("Mpesa Settings", settings_name, "company")
        cost_center = frappe.db.get_value("Mpesa Settings", settings_name, "cost_center") if frappe.get_meta(
            "Mpesa Settings").has_field("cost_center") else None
        paid_on = get_datetime(paid_at).date()
        pe = create_payment_entry(company, intent.customer, amount, "KES", mode_of_payment_for(settings_name), "Customer",
                                  paid_on, receipt, paid_on, cost_center, 1, references=refs)
    log_match("Booked", "Reference", reference_doctype=INTENT, reference_name=intent.name, customer=intent.customer,
              payment_entry=pe.name, details=f"Receipt {receipt}")


# ---------------------------------------------------------------- cancel / status helpers
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


# ---------------------------------------------------------------- scheduler
def sync_open(limit: int = 200) -> None:
    """Expire overdue intents, settle open ones, and catch late payments on recently closed ones."""
    now = now_datetime()
    jobs = []
    # 1. overdue first, oldest first, so nothing is starved by newer traffic
    jobs += frappe.get_all(INTENT, filters={"status": ["in", list(OPEN)], "expires_at": ["<", now]},
                           pluck="name", order_by="expires_at asc", limit=limit)
    # 2. open and still valid, newest first
    jobs += frappe.get_all(INTENT, filters={"status": ["in", list(OPEN)], "expires_at": [">=", now]},
                           pluck="name", order_by="creation desc", limit=limit)
    # 3. closed in the last day but a receipt names them (late payment)
    recent = frappe.get_all(INTENT, filters={"status": ["in", ["Expired", "Cancelled"]],
                                             "modified": [">=", add_to_date(now, days=-1)]},
                            pluck="name", order_by="modified desc", limit=limit)
    if recent:
        late = set(frappe.get_all(REGISTER, filters={"billrefnumber": ["in", recent], "docstatus": ["<", 2]},
                                  pluck="billrefnumber", order_by="creation desc"))
        late |= set(frappe.get_all(EXPRESS, filters={"reference_doctype": INTENT, "reference_name": ["in", recent],
                                                     "status": "Completed"}, pluck="reference_name", order_by="creation desc"))
        jobs += [n for n in recent if n in late]

    for name in dict.fromkeys(jobs):
        frappe.db.savepoint("mpesa_intent")
        try:
            refresh(name)
        except Exception:
            try:
                frappe.db.rollback(save_point="mpesa_intent")
            except Exception:
                frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa intent refresh failed for {name}")
