"""Ask Safaricom what happened to STK prompts whose callback never arrived.

Only updates the prompt's status (Completed / Failed with Safaricom's reason). Money is
never booked from a status query: the receipt number and amount come from the C2B
confirmation, which the matching pipeline books.
"""

import frappe
import requests
from frappe.utils import add_to_date, cint, now_datetime

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
    names = frappe.get_all(
        EXPRESS,
        filters={
            "docstatus": 1,
            "status": ["in", ["In Progress", "Pending", "Initiated"]],
            "checkout_request_id": ["is", "set"],
            "creation": ["between", [add_to_date(now, minutes=-60), add_to_date(now, minutes=-2)]],
        },
        pluck="name",
        order_by="creation asc",
        limit=limit,
    )
    for name in names:
        try:
            poll(name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"STK status poll failed for {name}")


def poll(name: str) -> str | None:
    from ..frappe_mpsa_payments.api.m_pesa_api import generate_request_password

    req = frappe.get_doc(EXPRESS, name)
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
