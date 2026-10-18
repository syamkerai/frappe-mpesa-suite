"""Signed outbound webhooks for payment intents, with retries.

POST <webhook_url>
Content-Type: application/json
X-Mpesa-Event: payment.succeeded
X-Mpesa-Delivery: <delivery id>          (dedupe on this: retries reuse it)
X-Mpesa-Signature: t=<unix>,v1=<hmac>    (see signing.py)
"""

import json

import frappe
import requests
from frappe.utils import add_to_date, cint, now_datetime

from .signing import HEADER, sign

DELIVERY = "Mpesa Webhook Delivery"
EVENTS = {
    "Paid": "payment.succeeded",
    "Failed": "payment.failed",
    "Expired": "payment.expired",
    "Cancelled": "payment.cancelled",
}
BACKOFF_MINUTES = (1, 5, 15, 60, 180, 720, 1440)  # then give up
TIMEOUT = (5, 15)


def queue(intent) -> str | None:
    from .intents import serialize

    event = EVENTS.get(intent.status)
    client = frappe.get_cached_doc("Mpesa API Client", intent.client)
    url = intent.callback_url or client.webhook_url
    if not event or not url:
        return None
    delivery = frappe.get_doc(
        {
            "doctype": DELIVERY,
            "intent": intent.name,
            "client": intent.client,
            "event": event,
            "status": "Pending",
            "url": url,
            # the enqueued job sends it now; the retry scheduler only steps in if that job didn't
            "next_attempt_at": add_to_date(now_datetime(), minutes=2),
        }
    )
    delivery.flags.ignore_permissions = True
    delivery.insert()
    payload = {"id": delivery.name, "event": event, "created": int(now_datetime().timestamp()), "data": serialize(intent)}
    delivery.db_set("payload", json.dumps(payload, separators=(",", ":"), default=str), update_modified=False)
    frappe.enqueue(
        "frappe_mpsa_payments.gateway.webhooks.deliver",
        queue="short",
        delivery_name=delivery.name,
        enqueue_after_commit=True,
    )
    return delivery.name


def deliver(delivery_name: str, force: bool = False) -> str:
    delivery = frappe.get_doc(DELIVERY, delivery_name)
    if delivery.status == "Delivered" or (delivery.status == "Failed" and not force):
        return delivery.status
    client = frappe.get_doc("Mpesa API Client", delivery.client)
    secret = client.get_password("webhook_secret", raise_exception=False) or ""
    body = delivery.payload or "{}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "M-Pesa-Suite-Webhooks/1",
        "X-Mpesa-Event": delivery.event,
        "X-Mpesa-Delivery": delivery.name,
        HEADER: sign(secret, body),
    }
    attempts = cint(delivery.attempts) + 1
    values = {"attempts": attempts}
    try:
        response = requests.post(delivery.url, data=body.encode(), headers=headers, timeout=TIMEOUT, allow_redirects=False)
        values.update(response_code=response.status_code, response_body=(response.text or "")[:1000], last_error=None)
        ok = 200 <= response.status_code < 300
    except requests.RequestException as e:
        values.update(last_error=f"{type(e).__name__}: {str(e)[:500]}")
        ok = False

    if ok:
        values.update(status="Delivered", next_attempt_at=None)
    elif attempts > len(BACKOFF_MINUTES):
        values.update(status="Failed", next_attempt_at=None)
    else:
        values.update(status="Pending", next_attempt_at=add_to_date(now_datetime(), minutes=BACKOFF_MINUTES[attempts - 1]))
    frappe.db.set_value(DELIVERY, delivery.name, values, update_modified=False)
    return values["status"]


def retry_due(limit: int = 50) -> None:
    names = frappe.get_all(
        DELIVERY,
        filters={"status": "Pending", "next_attempt_at": ["<=", now_datetime()]},
        pluck="name",
        order_by="next_attempt_at asc",
        limit=limit,
    )
    for name in names:
        try:
            deliver(name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa webhook delivery failed for {name}")
