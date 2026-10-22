"""Ask Safaricom what happened to STK prompts whose callback never arrived.

Only updates the prompt's status (Completed / Failed with Safaricom's reason). Money is
never booked from a status query: the receipt number and amount come from the C2B
confirmation, which the matching pipeline books.
"""

import frappe
import requests
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from ..daraja.client import CONNECT_TIMEOUT, READ_TIMEOUT, base_url, bearer_headers
from ..daraja.guard import daraja_timestamp
from .common import EXPRESS, enabled

# ResultCode -> prompt is finished and failed. 0 = paid. Anything else / errorCode = still pending.
FAILED_CODES = {
    "1": "Insufficient M-Pesa balance",
    "1001": "Customer has another transaction in progress",
    "1019": "Prompt expired before the customer responded",
    "1025": "Safaricom could not send the prompt",
    "1032": "Customer cancelled the prompt",
    "1037": "Customer's phone could not be reached",
    "2001": "Wrong M-Pesa PIN entered",
    "9999": "Safaricom could not send the prompt",
}


def run(limit: int = 20) -> None:
    if not enabled("poll_stk_status"):
        return
    now = now_datetime()
    filters = {
        "docstatus": 1,
        "status": ["in", ["In Progress", "Pending", "Initiated"]],
        "checkout_request_id": ["is", "set"],
    }
    if frappe.get_meta(EXPRESS).has_field("verification_status"):
        # a callback arrived: money_in.stk_verify settles it (and books it), not the poller
        filters["verification_status"] = ["is", "not set"]
    names = prompted_between(filters, add_to_date(now, minutes=-60), add_to_date(now, minutes=-2), limit=limit)
    for name in names:
        try:
            poll(name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"STK status poll failed for {name}")


def prompted_between(filters: dict, after, before=None, limit: int = 20) -> list:
    """Mpesa Express Requests matching ``filters`` whose latest prompt was sent in the window
    (``before`` None: up to now), oldest prompt first.

    A re-sent prompt reuses its request (m_pesa_api._stk_push_accepted), so the window keys on
    ``timestamp``, which stk_push_on_success sets on every prompt Safaricom accepts; ``creation``
    only for a request without one. Keyed on creation, a re-prompt on a request older than the
    window was never polled, given up on at once, or never retried.
    """
    window = ["between", [after, before]] if before is not None else [">=", after]
    rows = frappe.get_all(
        EXPRESS,
        filters={**filters, "timestamp": window},
        fields=["name", "timestamp"],
        order_by="timestamp asc",
        limit=limit,
    )
    rows += [
        frappe._dict(name=row.name, timestamp=row.creation)
        for row in frappe.get_all(
            EXPRESS,
            filters={**filters, "timestamp": ["is", "not set"], "creation": window},
            fields=["name", "creation"],
            order_by="creation asc",
            limit=limit,
        )
    ]
    rows.sort(key=lambda row: get_datetime(row.timestamp))
    return [row.name for row in rows[:limit]]


def stk_query(req) -> dict | None:
    """STK Query for an Mpesa Express Request; the response body, or None if unreadable."""
    from ..mpsa_suite.api.m_pesa_api import generate_request_password

    settings = frappe.get_doc("Mpesa Settings", req.settings)
    timestamp = daraja_timestamp()
    payload = {
        "BusinessShortCode": settings.business_shortcode,
        "Password": generate_request_password(settings, timestamp),
        "Timestamp": timestamp,
        "CheckoutRequestID": req.checkout_request_id,
    }
    response = requests.post(
        f"{base_url(settings.sandbox)}/mpesa/stkpushquery/v1/query",
        json=payload,
        headers=bearer_headers(settings),
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    try:
        data = response.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def poll(name: str) -> str | None:
    req = frappe.get_doc(EXPRESS, name)
    data = stk_query(req)
    if data is None:
        return None

    code = data.get("ResultCode")
    if code is None:
        return None  # e.g. errorCode 500.001.1001 "The transaction is being processed"
    code = str(cint(code)) if str(code).isdigit() else str(code)

    if code == "0":
        values = {"status": "Completed", "result_code": "0", "result_desc": data.get("ResultDesc")}
    elif code in FAILED_CODES:
        values = {"status": "Failed", "result_code": code, "result_desc": FAILED_CODES[code]}
    else:
        return None  # e.g. 4999 "still under processing": ask again next run
    # db.set_value: saving would run the request's duplicate-C2B cancellation
    frappe.db.set_value(EXPRESS, name, values, update_modified=False)
    frappe.publish_realtime("mpesa_stk_status", {"name": name, **values}, user=req.owner)
    return values["status"]
