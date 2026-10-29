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
- Money that names an intent already paid by another receipt (the customer paid twice) is
  booked on account to the intent's customer, never dropped.
- A receipt with a live reversal (refunded, or being refunded, to the payer) never pays an
  intent and is never booked; nor does one a cashier put on an invoice (its C2B register is
  submitted with no Payment Entry: the invoice's payment row holds the money).
- Inside an STK booking a booking failure is raised, not logged: that booking has already
  removed the receipt's C2B register, and only its rollback (and retry) brings it back.
- A webhook problem never undoes or blocks a status change.
"""

import json
import math
import re
from urllib.parse import urlparse

import frappe
from frappe.utils import (
    add_days,
    add_to_date,
    cint,
    cstr,
    flt,
    get_datetime,
    get_url,
    now_datetime,
    nowdate,
    strip_html,
)

from ..daraja import apis
from ..money_in.common import (
    BOOKING_RECEIPT_FLAG,
    EXPRESS,
    MATCH_LOG,
    REGISTER,
    REVERSAL_HOLDS,
    erpnext_installed,
    log_match,
    named_lock,
    parse_transtime,
    receipt_reversal,
    reversal_holding,
    reversed_registers,
    settings_for_shortcode,
    tolerance,
)
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
# frappe.flags set while an STK booking runs the intent's on_payment_authorized: a booking
# failure is raised (see mark_paid). BOOKING_RECEIPT_FLAG (money_in.common), set by the
# booking, names the receipt being booked, so only that prompt counts as pending in
# book_extra_receipts.
STK_BOOKING_FLAG = "mpesa_intent_stk_booking"


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
            raise ApiError(422, "invalid_phone", "phone must be a Kenyan mobile number, e.g. 0712345678 or 0112345678.")

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


def client_shortcodes(client, company=None) -> dict:
    """{shortcode or till: Mpesa Settings name} for the client's company (or its own settings).

    Where records share a number, the one settings_for_shortcode books money with: a record that
    collects before one that only pays out, then the oldest."""
    company = company or client_company(client)
    filters = {"company": company} if company else {"name": client.mpesa_settings}
    rows = frappe.get_all("Mpesa Settings", filters=filters,
                          fields=["name", "business_shortcode", "till_number", *apis.COLLECTING],
                          order_by="creation asc")
    rows.sort(key=lambda r: 0 if apis.collects(r) else 1)  # stable: oldest first otherwise
    out = {}
    for row in rows:
        for code in (row.business_shortcode, row.till_number):
            if cstr(code).strip():
                out.setdefault(cstr(code).strip(), row.name)
    return out


def client_settings(client, company=None) -> set:
    """Every Mpesa Settings record of the client's company (or just its own record)."""
    company = company or client_company(client)
    filters = {"company": company} if company else {"name": client.mpesa_settings}
    return set(frappe.get_all("Mpesa Settings", filters=filters, pluck="name"))


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
                "response_description", "settings", "is_reconciled"],
        order_by="creation desc",
    )


def _paid_prompt_unsettled(intent_name: str) -> bool:
    """The customer paid a prompt whose receipt has not settled the intent yet: Safaricom's
    status query said paid, or a 'paid' callback is still being verified. A resend now would
    charge the customer twice."""
    from ..daraja import verify

    fields = ["status", "transaction_id"]
    if frappe.get_meta(EXPRESS).has_field("verification_status"):
        fields += ["verification_status", "callback_payload"]
    for p in frappe.get_all(EXPRESS, filters={"reference_doctype": INTENT, "reference_name": intent_name,
                                              "docstatus": 1}, fields=fields):
        if p.status == "Completed" and not receipt_reversal(p.transaction_id):  # refunded: may pay again
            return True
        if p.get("verification_status") == verify.PENDING and p.get("callback_payload"):
            try:
                if verify.parse_stk_callback(json.loads(p.callback_payload))["ok"]:
                    return True
            except (TypeError, ValueError, AttributeError):
                pass
    return False


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
    if _paid_prompt_unsettled(intent.name):
        raise ApiError(409, "prompt_pending", "The customer paid an earlier prompt that Safaricom has not confirmed yet; "
                                              "check the status again shortly.")
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
        if (p.status == "Completed" and p.transaction_id and not _receipt_taken(p.transaction_id, intent.name)
                and not receipt_reversal(p.transaction_id)):
            consumed = _consumed_register(p.transaction_id)
            if consumed:
                _log_consumed(intent, p.transaction_id, p.amount, consumed)
                continue
            paid_at = parse_transtime(p.transaction_date) if p.transaction_date else now_datetime()
            mark_paid(intent, p.transaction_id, p.amount, paid_at, None, via="STK callback", settings=p.settings)
            return intent

    register = _register_for(intent)
    if register:
        reg = frappe.db.get_value(REGISTER, register, ["transid", "transamount", "transtime", "full_name", "businessshortcode"],
                                  as_dict=True)
        # booked with the shortcode that received the money, which may not be the client's own
        mark_paid(intent, reg.transid, reg.transamount, parse_transtime(reg.transtime), reg.full_name, register=register,
                  settings=_receiving_settings(intent, reg.businessshortcode))
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
    """A C2B receipt whose account number is this intent, paid to the client's company, covering
    its amount, not used elsewhere (another intent, or an invoice a cashier put it on) and not
    refunded to the payer. A register the matcher booked keeps its Payment Entry and counts."""
    tol = tolerance()
    erpnext = erpnext_installed()  # the register's payment_entry and invoice fields come with ERPNext
    rows = frappe.get_all(
        REGISTER,
        filters={"billrefnumber": intent.name, "docstatus": ["<", 2]},
        fields=["name", "transamount", "transid", "businessshortcode", "docstatus"]
        + (["payment_entry", "pos_invoice", "sales_invoice"] if erpnext else []),
        order_by="creation asc",
    )
    shortcodes = _company_shortcodes(intent) if rows else None
    held = set(reversed_registers([r.name for r in rows]))
    for row in rows:
        if row.name in held:
            continue  # reversed: the money went (or is going) back to the payer
        if not _paid_to(row.businessshortcode, shortcodes):
            _log_other_company(intent, row)
        elif flt(row.transamount) + tol >= flt(intent.amount) and not _receipt_taken(row.transid, intent.name):
            if erpnext and row.docstatus == 1 and not row.payment_entry and (row.pos_invoice or row.sales_invoice):
                _log_consumed(intent, row.transid, row.transamount, row)  # on an invoice already
                continue
            return row.name
    return None


def _consumed_register(receipt, for_update: bool = False):
    """The submitted C2B register of this receipt that has no Payment Entry, or None: a cashier
    put the receipt on a POS or sales invoice (POS quick pay), whose payment row holds the money.
    With ``for_update`` it locks the receipt's registers (see mark_paid)."""
    receipt = cstr(receipt).strip()
    if not receipt or not erpnext_installed():
        return None  # no invoices without ERPNext, and a register is then submitted with no entry
    row = frappe.db.get_value(
        REGISTER, {"transid": receipt, "docstatus": 1, "payment_entry": ["is", "not set"]},
        ["name", "pos_invoice", "sales_invoice"], as_dict=True, for_update=for_update,
    )
    return row if row and (row.pos_invoice or row.sales_invoice) else None


def _invoice_of(row) -> str:
    """The invoice a cashier put a submitted register on, for messages."""
    if row.get("pos_invoice"):
        return f"POS Invoice {row.pos_invoice}"
    if row.get("sales_invoice"):
        return f"Sales Invoice {row.sales_invoice}"
    return "an invoice"


def _log_consumed(intent, receipt, amount, row) -> None:
    """A receipt that would pay this intent was put on an invoice: the intent stays unpaid (the
    money paid that sale), and staff check the order with the customer."""
    _log_extra(intent, receipt, amount, "Error",
               f"Receipt {receipt} names payment intent {intent.name} but C2B register {row.name} was put on "
               f"{_invoice_of(row)}, so it does not pay the intent; check the order with the customer.")


def _company_shortcodes(intent) -> dict | None:
    """The shortcodes and tills of the client's company, or None when its company is unknown."""
    client = frappe.get_cached_doc("Mpesa API Client", intent.client)
    company = client_company(client)
    return client_shortcodes(client, company) if company else None


def _paid_to(shortcode, shortcodes) -> bool:
    """Money paid to another company's paybill or till is not this client's."""
    return shortcodes is None or cstr(shortcode).strip() in shortcodes


def _log_other_company(intent, register) -> None:
    """A receipt naming a prompted intent but paid to another company's shortcode: the matcher
    holds it for the gateway, which never takes it, so tell staff to book it by hand."""
    if register.get("docstatus") or register.get("payment_entry"):
        return  # booked already
    if frappe.db.exists(EXPRESS, {"reference_doctype": INTENT, "reference_name": intent.name}):
        log_match("Unmatched", "Reference", register=register, once=True, reference_doctype=INTENT,
                  reference_name=intent.name,
                  details=f"Names payment intent {intent.name} but was paid to {register.businessshortcode}, "
                          "outside the client's company; book it by hand.")


def _receiving_settings(intent, shortcode, shortcodes=None) -> str | None:
    """Mpesa Settings that received a paybill payment, which money on that number is booked
    with: the client's company's record for it (companies may share a number), else the
    number's own record."""
    shortcodes = shortcodes if shortcodes is not None else _company_shortcodes(intent)
    return (shortcodes or {}).get(cstr(shortcode).strip()) or settings_for_shortcode(shortcode)


def _receipt_taken(receipt: str, intent_name: str) -> bool:
    return bool(frappe.db.exists(INTENT, {"receipt": receipt, "name": ["!=", intent_name]}))


def mark_paid(intent, receipt, amount, paid_at, payer_name, register=None, via=None, settings=None):
    """Record the payment (locked per receipt), notify, then hand the money to accounting."""
    # Locking read of the receipt's C2B registers first, in the order every booking of a receipt
    # takes its locks (the STK booking and POS quick pay too): register rows, then the receipt
    # lock, then the booking lock. A cashier's POS submit still in flight is waited for, and one
    # committed after this transaction's snapshot is seen.
    consumed = _consumed_register(receipt, for_update=True)
    with named_lock(f"receipt:{receipt}"):
        if frappe.db.get_value(INTENT, {"receipt": receipt, "name": ["!=", intent.name]}, "name", for_update=True):
            raise ApiError(409, "receipt_already_used", f"Receipt {receipt} already paid another order.")
        intent.reload()
        if intent.status == "Paid":
            return intent
        if consumed:
            raise ApiError(409, "receipt_already_used",
                           f"Receipt {receipt} was put on {_invoice_of(consumed)} (C2B register {consumed.name}).")
        # locking read: a reversal submitted while this runs is waited for, not missed
        reversal = reversal_holding(register, for_update=True, receipt=receipt)
        if reversal:
            raise ApiError(409, "receipt_already_used",
                           f"Receipt {receipt} was reversed to the payer ({reversal.name}, {reversal.status}).")
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
        if frappe.flags.get(STK_BOOKING_FLAG):
            # The STK booking may have cancelled or deleted this receipt's C2B register already;
            # logged and committed, the receipt would be in no ledger and nothing would retry
            # it (it is the intent's own receipt). Raised, the booking rolls back and retries.
            raise
        log_match("Error", _match_method(via), reference_doctype=INTENT, reference_name=intent.name, once=True,
                  details=f"Payment recorded ({via or 'C2B'}) but booking failed: {frappe.get_traceback()[-800:]}")
    return intent


# ---------------------------------------------------------------- accounting
def _match_method(via) -> str:
    """Mpesa Match Log method for how the money arrived ("STK callback" is not one of its options)."""
    return via if via == "Manual Attach" else "Reference"


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
    match = Match(method=_match_method(via), customer=intent.customer or reg.get("customer"),
                  details=f"Payment intent {intent.name} ({intent.order_ref})")
    if intent.reference_doctype in LINKABLE and intent.reference_name:
        match.update(doctype=intent.reference_doctype, name=intent.reference_name)
    if not match.customer:
        log_match("Matched", "Reference", register=reg, once=True, details=match.details + "; no customer, left for staff")
        return
    booking.apply_match(reg, match)


def _settings_for(intent) -> str:
    return frappe.db.get_value("Mpesa API Client", intent.client, "mpesa_settings")


def _book_direct(intent, receipt, amount, paid_at, settings_name, note=None) -> None:
    """Payment Entry straight from the receipt, against the intent's document (or on account)."""
    from ..mpsa_suite.api.payment_entry import create_payment_entry
    from ..money_in.common import mode_of_payment_for

    if not intent.customer:
        log_match("Matched", "Reference", reference_doctype=INTENT, reference_name=intent.name, once=True,
                  details=f"Paid ({receipt}) but the intent has no customer; book it manually.")
        return
    # Locking reads: they see what another booking committed after this transaction's snapshot.
    # The receipt's registers first, then the lock every booking of a receipt takes (POS quick
    # pay too), in the order they all take them.
    submitted = frappe.db.get_value(REGISTER, {"transid": receipt, "docstatus": 1},
                                    ["name", "payment_entry", "pos_invoice", "sales_invoice"], as_dict=True,
                                    for_update=True)
    with named_lock(f"book:{receipt}"):
        if frappe.db.get_value("Payment Entry", {"reference_no": receipt, "docstatus": ["<", 2]}, "name", for_update=True):
            return
        if submitted:
            # booked by its register: on an invoice a cashier put it on (no Payment Entry), or
            # through a Payment Entry since cancelled by hand while the register stayed submitted
            where = f" on {_invoice_of(submitted)}" if not submitted.payment_entry else ""
            _log_extra(intent, receipt, amount, "Error",
                       f"Receipt {receipt} not booked for payment intent {intent.name}: C2B register "
                       f"{submitted.name} is submitted{where}, and booking it again would count the money "
                       "twice; check the intent.")
            return
        reversal = receipt_reversal(receipt, for_update=True)
        if reversal:
            _log_extra(intent, receipt, amount, "Error",
                       f"Receipt {receipt} not booked: reversal {reversal.name} is {reversal.status}, so the money "
                       "is (or may be) back with the payer.")
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
              payment_entry=pe.name, details=f"Receipt {receipt}" + (f"; {note}" if note else ""))


def book_extra_receipts(intent, raise_errors: bool = False, stk_booking: bool = False) -> None:
    """Book money that names a Paid intent but is not its receipt (the customer paid twice).

    Covers Completed prompts (the STK booking calls this, with ``stk_booking``, just before
    marking its prompt reconciled) and draft C2B registers of prompted intents, which the
    matcher holds for the gateway; sync_open calls it for the ones nothing else retries. Each
    is booked straight from its receipt, like any prompted payment; the document is usually
    paid already, so it lands on account to the customer. A prompt booked here is marked
    reconciled. A failure is logged to Mpesa Match Log. For a prompt not yet reconciled it is
    raised instead, so the STK booking rolls back and leaves the prompt unreconciled for a
    retry, when ``raise_errors`` is set or when the receipt's only Payment Entry is a cancelled
    one (this booking cancelled the receipt's C2B register in the prompt's favour; logged, that
    money would count as booked and never be retried).
    """
    from ..money_in.common import enabled

    if intent.status != "Paid" or not enabled("enable_matching"):
        return
    for receipt, amount, paid_at, settings, prompt, pending in _extra_receipts(intent, stk_booking):
        if not intent.customer:
            _log_extra(intent, receipt, amount, "Matched",
                       f"Extra payment {receipt} on paid intent {intent.name}, which has no customer; book it manually.")
            continue
        frappe.db.savepoint("mpesa_intent_extra")
        try:
            _book_direct(intent, receipt, amount, paid_at, settings or _settings_for(intent),
                         note=f"extra payment on intent {intent.name}, already paid by {intent.receipt}; refund or allocate it")
        except Exception:
            if raise_errors and pending:
                raise
            frappe.db.rollback(save_point="mpesa_intent_extra")
            if pending and _receipt_booked(receipt, pending=False):
                raise
            _log_extra(intent, receipt, amount, "Error",
                       f"Extra payment {receipt} on paid intent {intent.name} not booked: {frappe.get_traceback()[-600:]}")
            continue
        if prompt:
            # Done: not waiting for its own STK booking any more (book() skips a reconciled prompt),
            # so a Payment Entry staff cancel later stays cancelled.
            frappe.db.set_value(EXPRESS, prompt, "is_reconciled", 1, update_modified=False)


def _extra_receipts(intent, stk_booking: bool = False) -> list:
    """(receipt, amount, paid_at, settings, prompt, pending) naming this intent, other than its
    own receipt, that nothing has booked, refunded or given to another intent. ``prompt``: the
    unreconciled prompt carrying the receipt. ``pending``: such a prompt during an STK booking
    (``stk_booking``), which may be the one being booked: the one the booking names in
    frappe.flags (BOOKING_RECEIPT_FLAG), or any of them when it names none."""
    booking = cstr(frappe.flags.get(BOOKING_RECEIPT_FLAG)).strip() if stk_booking else ""
    out = {}
    for p in _prompts(intent.name):
        # reconciled ones too: an earlier booking may have logged a failure and moved on
        if p.status == "Completed" and p.transaction_id:
            paid_at = parse_transtime(p.transaction_date) if p.transaction_date else now_datetime()
            prompt = None if p.is_reconciled else p.name
            pending = bool(stk_booking and prompt and (not booking or p.transaction_id == booking))
            out.setdefault(p.transaction_id, (flt(p.amount), paid_at, p.settings, prompt, pending))
    if frappe.db.exists(EXPRESS, {"reference_doctype": INTENT, "reference_name": intent.name}):
        # The matcher holds a prompted intent's registers for the gateway; others it books itself.
        # A register it matched to an order (paid before the first prompt) is the order's to book.
        shortcodes = _company_shortcodes(intent)
        for r in frappe.get_all(REGISTER, filters={"billrefnumber": intent.name, "docstatus": 0,
                                                   "payment_entry": ["is", "not set"],
                                                   "matched_reference_name": ["is", "not set"]},
                                fields=["name", "transid", "transamount", "transtime", "businessshortcode"],
                                order_by="creation asc"):
            if not r.transid:
                continue
            if not _paid_to(r.businessshortcode, shortcodes):
                _log_other_company(intent, r)
                continue
            out.setdefault(r.transid, (flt(r.transamount), parse_transtime(r.transtime) or now_datetime(),
                                       _receiving_settings(intent, r.businessshortcode, shortcodes), None, False))
    return [
        (receipt, *values) for receipt, values in out.items()
        if receipt != intent.receipt
        and not _receipt_taken(receipt, intent.name)
        and not _receipt_booked(receipt, pending=values[-1])
        and not receipt_reversal(receipt)  # refunded to the payer
    ]


def _receipt_booked(receipt: str, pending: bool) -> bool:
    """A Payment Entry carries the receipt. A cancelled one counts as well (booked, then undone
    by hand), except for a prompt that may be the one being booked now: its STK booking has
    just cancelled the C2B register for this receipt in its favour, and that register's
    Payment Entry with it. Outside an STK booking (sync_open) nothing cancelled it in this
    transaction, so a prompt whose STK booking gave up stays unbooked once staff cancel."""
    filters = {"reference_no": receipt}
    if pending:
        filters["docstatus"] = ["<", 2]
    return bool(frappe.db.exists("Payment Entry", filters))


def _log_extra(intent, receipt, amount, result, details) -> None:
    """One Mpesa Match Log row per extra receipt and result (this runs again on later prompts)."""
    if frappe.db.exists(MATCH_LOG, {"reference_name": intent.name, "receipt": receipt, "result": result}):
        return
    log_match(result, "Reference", reference_doctype=INTENT, reference_name=intent.name, receipt=receipt,
              amount=amount, details=details)


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
        # the status (and, for Paid, the booking after it) must stand even if the webhook can't be queued
        frappe.db.savepoint("mpesa_intent_webhook")
        try:
            queue(intent)
        except Exception:
            frappe.db.rollback(save_point="mpesa_intent_webhook")
            frappe.log_error(frappe.get_traceback(), f"M-Pesa webhook not queued for {intent.name}"[:140])


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
    """Expire overdue intents, settle open ones, catch late payments on recently closed ones, and
    book money paid again on paid ones."""
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

    # 4. paid, then paid again (a paybill payment, or a prompt whose booking logged a failure):
    #    nothing else books that money, since refresh stops at Paid and the matcher holds it
    from ..money_in.common import enabled

    if not enabled("enable_matching"):
        return
    for name in _paid_with_extra_money(limit):
        frappe.db.savepoint("mpesa_intent_extras")
        try:
            book_extra_receipts(frappe.get_doc(INTENT, name))
        except Exception:
            try:
                frappe.db.rollback(save_point="mpesa_intent_extras")
            except Exception:
                frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa extra payment booking failed for {name}"[:140])


def _paid_with_extra_money(limit: int) -> list:
    """Paid, prompted intents that a receipt nothing has booked still names, within the matcher's
    lookback (book_extra_receipts makes the final checks)."""
    from ..money_in.common import suite_int

    since = add_days(nowdate(), -suite_int("matching_lookback_days", 45))
    return frappe.db.sql(
        """
        SELECT reg.billrefnumber FROM `tabMpesa C2B Payment Register` reg
        JOIN `tabMpesa Payment Intent` it ON it.name = reg.billrefnumber AND it.status = 'Paid'
        WHERE reg.docstatus = 0 AND reg.posting_date >= %(since)s
          AND COALESCE(reg.transid, '') != '' AND reg.transid != COALESCE(it.receipt, '')
          AND COALESCE(reg.payment_entry, '') = '' AND COALESCE(reg.matched_reference_name, '') = ''
          AND EXISTS (SELECT 1 FROM `tabMpesa Express Request` er
                      WHERE er.reference_doctype = 'Mpesa Payment Intent' AND er.reference_name = it.name)
          AND NOT EXISTS (SELECT 1 FROM `tabPayment Entry` pe WHERE pe.reference_no = reg.transid)
          AND NOT EXISTS (SELECT 1 FROM `tabMpesa C2B Payment Register` sub WHERE sub.transid = reg.transid
                          AND sub.docstatus = 1)
          AND NOT EXISTS (SELECT 1 FROM `tabMpesa Reversal Request` rv WHERE rv.transaction_id = reg.transid
                          AND rv.docstatus = 1 AND rv.status IN %(holds)s)
        UNION
        SELECT er.reference_name FROM `tabMpesa Express Request` er
        JOIN `tabMpesa Payment Intent` it ON it.name = er.reference_name AND it.status = 'Paid'
        WHERE er.reference_doctype = 'Mpesa Payment Intent' AND er.docstatus = 1 AND er.status = 'Completed'
          AND er.creation >= %(since)s
          AND COALESCE(er.transaction_id, '') != '' AND er.transaction_id != COALESCE(it.receipt, '')
          AND NOT EXISTS (SELECT 1 FROM `tabPayment Entry` pe WHERE pe.reference_no = er.transaction_id)
          AND NOT EXISTS (SELECT 1 FROM `tabMpesa C2B Payment Register` sub WHERE sub.transid = er.transaction_id
                          AND sub.docstatus = 1)
          AND NOT EXISTS (SELECT 1 FROM `tabMpesa Reversal Request` rv WHERE rv.transaction_id = er.transaction_id
                          AND rv.docstatus = 1 AND rv.status IN %(holds)s)
        LIMIT %(limit)s
        """,
        {"since": since, "limit": cint(limit), "holds": REVERSAL_HOLDS},
        pluck=True,
    )
