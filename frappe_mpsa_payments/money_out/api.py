"""HTTP endpoints for Reversal, B2B and B2C Transaction Status.

Safaricom-facing endpoints go through ``@safaricom_callback`` (IP allow-list, URL
token, integration user). Each API has its own Result URL and its own QueueTimeOut
URL; ``ref`` on the URL names our document. Desk actions are role-gated.
"""

import frappe

from ..daraja.callbacks import KIND_RESULT, safaricom_callback
from ..permissions import ADMIN_ROLES, PAYOUT_ROLES, require
from . import b2b, b2c_status, reversal

ACCEPTED = {"ResultCode": 0, "ResultDesc": "Accepted"}
PAYOUT_OR_ADMIN = tuple(dict.fromkeys(PAYOUT_ROLES + ADMIN_ROLES))


def _result(kwargs) -> dict:
    result = kwargs.get("Result")
    if isinstance(result, str):
        result = frappe.parse_json(result)
    return result if isinstance(result, dict) else {}


def _run(handler, kwargs, title: str):
    try:
        handler(_result(kwargs), kwargs.get("ref"))
    except Exception:
        frappe.log_error(frappe.get_traceback(), title)
        raise
    return ACCEPTED


def _timeout(handler, kwargs, title: str):
    payload = {k: v for k, v in kwargs.items() if k not in ("ref", "cmd", "t")}
    try:
        handler(payload, kwargs.get("ref"))
    except Exception:
        frappe.log_error(frappe.get_traceback(), title)
        raise
    return ACCEPTED


# ---------------------------------------------------------------- Safaricom callbacks
@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def reversal_result(**kwargs):
    return _run(reversal.handle_result, kwargs, "M-Pesa Reversal Result Error")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def reversal_queue_timeout(**kwargs):
    return _timeout(reversal.handle_queue_timeout, kwargs, "M-Pesa Reversal Timeout Error")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def b2b_result(**kwargs):
    return _run(b2b.handle_result, kwargs, "M-Pesa B2B Result Error")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def b2b_queue_timeout(**kwargs):
    return _timeout(b2b.handle_queue_timeout, kwargs, "M-Pesa B2B Timeout Error")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def b2c_status_result(**kwargs):
    return _run(b2c_status.handle_result, kwargs, "M-Pesa B2C Status Result Error")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def status_queue_timeout(**kwargs):
    return _timeout(b2c_status.handle_queue_timeout, kwargs, "M-Pesa Status Timeout Error")


# ---------------------------------------------------------------- desk actions
@frappe.whitelist(methods=["POST"])
def check_b2c_status(b2c_request: str) -> str:
    """Ask Safaricom what happened to a payout before anyone retries it."""
    require(PAYOUT_OR_ADMIN)
    frappe.get_doc("B2C Disbursement Request", b2c_request).check_permission("read")
    return b2c_status.start_query(b2c_request)


@frappe.whitelist(methods=["POST"])
def make_reversal(c2b_register: str, reason: str) -> str:
    """Draft a reversal for a received payment (submitting it sends it)."""
    require(PAYOUT_OR_ADMIN)
    frappe.get_doc("Mpesa C2B Payment Register", c2b_register).check_permission("read")
    doc = frappe.get_doc(
        {
            "doctype": reversal.REVERSAL_DOCTYPE,
            "c2b_register": c2b_register,
            "reason": reason,
            "remarks": "Reversal",
        }
    )
    doc.insert()
    return doc.name
