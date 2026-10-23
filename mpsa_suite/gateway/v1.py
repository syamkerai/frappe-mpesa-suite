"""External Payments API v1 (see docs/PAYMENTS_API.md in the repo).

Base path: /api/method/mpsa_suite.gateway.v1.<endpoint>
Auth: X-Mpesa-Client-Key / X-Mpesa-Client-Secret headers (pay-page endpoints use the page token).
"""

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, now_datetime

from ..money_in.common import EXPRESS, REGISTER, parse_transtime, reversal_holding, tolerance
from . import intents
from .auth import ApiError, endpoint, rate_limit, require

INTENT = intents.INTENT
REVERSED = "reversed"  # _claimed_by: the money went (or is going) back to the payer


def _intent_for(client, intent_id=None, order_ref=None):
    filters = {"client": client.name}
    if intent_id:
        filters["name"] = intent_id
    elif order_ref:
        filters["order_ref"] = order_ref
    else:
        raise ApiError(422, "invalid_request", "Give intent_id or order_ref.")
    name = frappe.db.get_value(INTENT, filters, "name", order_by="creation desc")
    if not name:
        raise ApiError(404, "not_found", "No such payment for this client.")
    return frappe.get_doc(INTENT, name)


# ---------------------------------------------------------------- client endpoints
@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",))
def create_prompt(client, amount=None, phone=None, order_ref=None, idempotency_key=None, description=None,
                  reference_doctype=None, reference_name=None, callback_url=None, metadata=None):
    """Create a payment and send the STK prompt to ``phone`` right away."""
    require(client, "can_prompt", "send STK prompts")
    if not phone:
        raise ApiError(422, "invalid_phone", "phone is required.")
    intent, created = intents.create(
        client, amount=amount, phone=phone, order_ref=order_ref, idempotency_key=idempotency_key,
        description=description, reference_doctype=reference_doctype, reference_name=reference_name,
        callback_url=callback_url, metadata=metadata,
    )
    if created or intent.status in ("Created", "Failed"):
        intents.send_prompt(intent)
    return {"payment": intents.serialize(intent)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",))
def create_payment_link(client, amount=None, order_ref=None, phone=None, idempotency_key=None, description=None,
                        reference_doctype=None, reference_name=None, callback_url=None, return_url=None,
                        metadata=None, expires_in_hours=None):
    """Create a payment the customer completes on the hosted pay page (or by paybill)."""
    require(client, "can_create_links", "create payment links")
    hours = min(max(1, cint(expires_in_hours) or cint(client.link_expiry_hours) or 24), 24 * 7)
    intent, _ = intents.create(
        client, amount=amount, phone=phone, order_ref=order_ref, idempotency_key=idempotency_key,
        description=description, reference_doctype=reference_doctype, reference_name=reference_name,
        callback_url=callback_url, return_url=return_url, metadata=metadata, expires_in_minutes=hours * 60,
    )
    return {"payment": intents.serialize(intent)}


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint(methods=("GET", "POST"))
def get_status(client, intent_id=None, order_ref=None):
    """Current state of a payment (checks Safaricom-side results first)."""
    intent = _intent_for(client, intent_id, order_ref)
    intent = intents.refresh(intent.name)
    return {"payment": intents.serialize(intent)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",))
def resend_prompt(client, intent_id=None, order_ref=None, phone=None):
    require(client, "can_prompt", "send STK prompts")
    intent = _intent_for(client, intent_id, order_ref)
    intents.send_prompt(intent, phone=phone)
    return {"payment": intents.serialize(intent)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",))
def cancel(client, intent_id=None, order_ref=None):
    intent = _intent_for(client, intent_id, order_ref)
    intents.cancel(intent)
    return {"payment": intents.serialize(intent)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",))
def verify_receipt(client, receipt=None, intent_id=None, order_ref=None, amount=None):
    """Check an M-Pesa code a customer sent (e.g. in WhatsApp) and optionally apply it to a payment.

    Only receipts on this client's company paybills/tills are visible. A receipt that is
    already booked or claimed cannot be applied again, and only receipts from the last
    72 hours (and not older than the payment itself) can be applied.
    """
    require(client, "can_verify_receipts", "verify receipts")
    code = cstr(receipt).strip().upper() if isinstance(receipt, str) else ""
    if not code or len(code) < 8 or not code.isalnum():
        raise ApiError(422, "invalid_receipt", "receipt must be an M-Pesa transaction code like UJIB2AMYHE.")

    found = _find_receipt(client, code)
    result = {"receipt": code, "found": bool(found)}
    if not found:
        return result
    claimed_by = _claimed_by(code, found)
    result.update(amount=found["amount"], paid_at=found["paid_at"] and found["paid_at"].isoformat(),
                  payer_name=found.get("payer_name"), claimed=bool(claimed_by))
    if amount is not None:
        result["amount_matches"] = abs(flt(amount) - flt(found["amount"])) <= tolerance()
    if not (intent_id or order_ref):
        return result

    intent = _intent_for(client, intent_id, order_ref)
    if intent.status != "Paid":
        if claimed_by == REVERSED:
            raise ApiError(409, "receipt_already_used", "This receipt was reversed to the payer and cannot be used.")
        if claimed_by and claimed_by != intent.name:
            raise ApiError(409, "receipt_already_used", "This receipt has already been used.")
        paid_at = found["paid_at"]
        if not paid_at or paid_at < add_to_date(now_datetime(), hours=-RECEIPT_MAX_AGE_HOURS):
            raise ApiError(422, "receipt_too_old", f"Only receipts from the last {RECEIPT_MAX_AGE_HOURS} hours can be applied.")
        if paid_at < add_to_date(get_datetime(intent.creation), minutes=-10):
            raise ApiError(422, "receipt_before_order", "This receipt was paid before the order was created.")
        if flt(found["amount"]) + tolerance() < flt(intent.amount):
            raise ApiError(422, "amount_too_low",
                           f"Receipt is KES {flt(found['amount']):,.0f}; this payment needs KES {flt(intent.amount):,.0f}.")
        intents.mark_paid(intent, code, found["amount"], paid_at, found.get("payer_name"),
                          register=found.get("register"), via="Manual Attach", settings=found.get("settings"))
        intent.reload()
    result["payment"] = intents.serialize(intent)
    return result


RECEIPT_MAX_AGE_HOURS = 72


def _find_receipt(client, code):
    shortcodes = intents.client_shortcodes(client)
    reg = frappe.db.get_value(
        REGISTER, {"transid": code, "docstatus": ["<", 2]},
        ["name", "transamount", "transtime", "full_name", "businessshortcode"], as_dict=True,
    )
    if reg and cstr(reg.businessshortcode).strip() in shortcodes:
        return {"register": reg.name, "amount": flt(reg.transamount), "paid_at": parse_transtime(reg.transtime),
                "payer_name": reg.full_name, "settings": shortcodes[cstr(reg.businessshortcode).strip()]}
    exp = frappe.db.get_value(
        EXPRESS, {"transaction_id": code, "status": "Completed"},
        ["name", "amount", "transaction_date", "settings", "reference_doctype", "reference_name", "is_reconciled"],
        as_dict=True,
    )
    # the prompt's own record, which need not be the one client_shortcodes keeps for its number
    if exp and exp.settings in intents.client_settings(client):
        return {"express": exp, "amount": flt(exp.amount), "settings": exp.settings,
                "paid_at": exp.transaction_date and get_datetime(exp.transaction_date)}
    return None


def _claimed_by(code: str, found: dict) -> str | None:
    """Who already owns this receipt: an intent name, or a marker for booked/reconciled money
    or for money refunded to the payer (a live reversal of the receipt or of its register)."""
    owner = frappe.db.get_value(INTENT, {"receipt": code}, "name")
    if owner:
        return owner
    if reversal_holding(found.get("register"), receipt=code):
        return REVERSED
    if frappe.db.exists("Payment Entry", {"reference_no": code, "docstatus": 1}):
        return "booked"
    if found.get("register"):
        reg = frappe.db.get_value(REGISTER, found["register"], ["docstatus", "payment_entry"], as_dict=True)
        if reg.docstatus == 1 or reg.payment_entry:
            return "booked"
    exp = found.get("express")
    if exp and exp.reference_doctype != INTENT and exp.is_reconciled:
        return "booked"
    return None


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@endpoint(methods=("GET", "POST"))
def list_payments(client, since=None, status=None, limit=50):
    filters = {"client": client.name}
    if since:
        try:
            filters["modified"] = [">=", get_datetime(since)]
        except Exception:
            raise ApiError(422, "invalid_request", "since must be a date or datetime, e.g. 2026-10-18T09:00:00.")
    if status:
        filters["status"] = cstr(status).capitalize()
    names = frappe.get_all(INTENT, filters=filters, pluck="name", order_by="modified desc",
                           limit=min(cint(limit) or 50, 200))
    return {"payments": [intents.serialize(frappe.get_doc(INTENT, n)) for n in names]}


# ---------------------------------------------------------------- pay page (token, no client auth)
def _intent_by_token(token):
    token = cstr(token).strip()
    if len(token) != 32:
        raise ApiError(404, "not_found", "Payment not found.")
    name = frappe.db.get_value(INTENT, {"pay_token": token}, "name")
    if not name:
        raise ApiError(404, "not_found", "Payment not found.")
    return frappe.get_doc(INTENT, name)


def page_view(intent) -> dict:
    """What the public page may show: no customer data beyond the masked phone."""
    data = intents.serialize(intent)
    return {k: data[k] for k in ("status", "amount", "currency", "phone", "receipt", "failure_reason", "expires_at", "paybill")} | {
        "description": intent.description,
        "order_ref": intent.order_ref,
        "return_url": intent.return_url if intent.status == "Paid" else None,
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",), authenticated=False)
def page_prompt(token=None, phone=None):
    rate_limit(f"page:{getattr(frappe.local, 'request_ip', '')}", 20)
    intent = _intent_by_token(token)
    rate_limit(f"page-intent:{intent.name}", 3)
    # a phone fixed by the merchant cannot be redirected from the public page
    intents.send_prompt(intent, phone=None if intent.phone else phone)
    return {"payment": page_view(intent)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@endpoint(methods=("POST",), authenticated=False)
def page_status(token=None):
    rate_limit(f"page-status:{getattr(frappe.local, 'request_ip', '')}", 120)
    intent = intents.refresh(_intent_by_token(token).name)
    return {"payment": page_view(intent)}

