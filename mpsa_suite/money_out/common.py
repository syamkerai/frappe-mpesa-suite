"""Sending Daraja initiator requests (Reversal, B2B, Transaction Status) and logging them.

Safety rules shared by every sender here, taken from the B2C payout flow:
- send only from a submitted document whose status is ``Not Initiated``, under a row lock;
- never raise after the HTTP call has gone out (a rollback would undo the record that
  money was asked for, and the user could submit again);
- a transport error after the request may have left is ``Unknown``, never ``Failed``,
  so nothing is retried until someone checks;
- the queue timeout goes to its own URL and never sets a final status;
- tokens come from ``daraja.client``; bodies are scrubbed before logging.
"""

from copy import deepcopy
from urllib.parse import urlencode

import frappe
import requests
from frappe.integrations.utils import create_request_log
from frappe.utils import get_url

from ..mpsa_suite.connectors.connectors import TOKEN_ERROR_CODES
from ..daraja.callbacks import with_token
from ..daraja.client import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    base_url,
    bearer_headers,
    bearer_token,
    invalidate_token,
)
from ..daraja.guard import redact_headers, scrub_payload
from ..utils.doctype_names import MPESA_SETTINGS_DOCTYPE

API_MODULE = "mpsa_suite.money_out.api"

# Request status values shared by the Reversal and B2B doctypes.
NOT_INITIATED = "Not Initiated"
INITIATED = "Initiated"
UNKNOWN = "Unknown"
FAILED = "Failed"


def callback_url(method: str, **params) -> str:
    """Public URL of ``money_out.api.<method>`` with query params and the callback token."""
    url = f"{get_url()}/api/method/{API_MODULE}.{method}"
    if params:
        url += "?" + urlencode(params)
    return with_token(url)


def initiator_settings(settings_name: str):
    """Mpesa Settings with an initiator and a security credential, or throw."""
    settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, settings_name)
    if not settings.initiator_name or not settings.security_credential:
        frappe.throw(
            f"Mpesa Settings {settings_name} needs an Initiator Name and a Security Credential "
            "(set the initiator password and press Regenerate Security Credential)."
        )
    if not settings.business_shortcode:
        frappe.throw(f"Mpesa Settings {settings_name} has no Business Shortcode")
    return settings


def lock_for_send(doctype: str, name: str):
    """Row-lock and reload; only a submitted, unsent document may be sent. The reload is a locking
    read too, so a cancel or an answer committed after this transaction's snapshot is seen."""
    frappe.db.get_value(doctype, name, "name", for_update=True)
    doc = frappe.get_doc(doctype, name, for_update=True)
    if doc.docstatus != 1:
        frappe.throw(f"{doctype} {name} must be submitted before it is sent")
    if doc.status not in (NOT_INITIATED, "", None):
        frappe.throw(f"{doctype} {name} is {doc.status}; only unsent requests are sent")
    return doc


def lock_by_name(doctype: str, name: str | None):
    if not name or not frappe.db.get_value(doctype, name, "name", for_update=True):
        return None
    return frappe.get_doc(doctype, name)


def post(settings, endpoint: str, payload: dict, *, doctype: str, name: str, description: str):
    """POST to Daraja. Returns ``(data, error)``.

    ``error`` is ``None`` when Safaricom answered (any HTTP status), ``"not_sent"`` when
    the connection never opened, and ``"unknown"`` when the request may have arrived.
    """
    url = f"{base_url(settings.sandbox)}/{endpoint.lstrip('/')}"
    log = create_request_log(
        data=scrub_payload(deepcopy(payload)),
        request_description=description,
        is_remote_request=True,
        service_name="Mpesa",
        request_headers=redact_headers({"Authorization": "Bearer ***", "Content-Type": "application/json"}),
        url=url,
        reference_doctype=doctype,
        reference_docname=name,
    )

    def _send():
        headers = bearer_headers(settings)
        response = requests.post(url, json=payload, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        return bearer_token(headers), response

    try:
        token, response = _send()
        data = _json(response)
        if str(data.get("errorCode") or "") in TOKEN_ERROR_CODES:
            # Safaricom refused the token, so nothing was processed: refresh once and resend.
            # Drop only that token; a fresher one another worker cached is used as is.
            invalidate_token(settings, rejected=token)
            _, response = _send()
            data = _json(response)
    except requests.ConnectTimeout as e:
        _close_log(log, "Failed", error=f"Not sent: {type(e).__name__}")
        return None, "not_sent"
    except requests.RequestException as e:
        _close_log(log, "Failed", error=f"Outcome unknown: {type(e).__name__}")
        return None, "unknown"

    ok = response.status_code < 300
    _close_log(log, "Completed" if ok else "Failed", output=frappe.as_json(data))
    return data, None


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {"_raw": (response.text or "")[:2000], "_status": response.status_code}
    return data if isinstance(data, dict) else {"_raw": str(data)[:2000]}


def _close_log(log, status, output=None, error=None):
    try:
        values = {"status": status}
        if output:
            values["output"] = output
        if error:
            values["error"] = error
        frappe.db.set_value("Integration Request", log.name, values, update_modified=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "M-Pesa request log update failed")


def apply_ack(doc, data, error, *, settings_ids=True) -> None:
    """Record the synchronous answer on ``doc`` with db writes (never raises)."""
    from ..daraja.money_out import ack_accepted

    if error == "not_sent":
        values = {"status": FAILED, "error_message": "Could not connect to M-Pesa; nothing was sent."}
    elif error:
        values = {
            "status": UNKNOWN,
            "error_message": "No answer from M-Pesa. The request may have gone through: "
            "wait for the result before sending again.",
        }
    elif ack_accepted(data):
        values = {
            "status": INITIATED,
            "response_code": str(data.get("ResponseCode")),
            "response_description": data.get("ResponseDescription"),
        }
        if settings_ids:
            values["conversation_id"] = data.get("ConversationID")
            values["originator_conversation_id"] = data.get("OriginatorConversationID")
    else:
        values = {
            "status": FAILED,
            "response_code": str(data.get("ResponseCode") or ""),
            "error_code": str(data.get("errorCode") or ""),
            "error_message": data.get("errorMessage") or data.get("ResponseDescription") or str(data)[:500],
        }
    values = {k: v for k, v in values.items() if v is not None}
    try:
        doc.db_set(values, notify=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"{doc.doctype} {doc.name}: could not record M-Pesa answer")
    if doc.doctype == "Mpesa B2B Payment Request":
        from .schedule import sync_line_later

        sync_line_later(doc)  # a payment schedule's line follows its request (never raises)
        from . import claim_payees

        try:
            claim_payees.on_payout_result(doc.doctype, doc.name)  # an Expense Claim's payee line: refused at once
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"{doc.doctype} {doc.name}: payee line not updated")


def log_callback(payload: dict, description: str, doctype: str, name: str | None, status: str) -> None:
    """Keep the raw (scrubbed) callback on an Integration Request for audit."""
    create_request_log(
        data=scrub_payload(deepcopy(payload)),
        request_description=description,
        is_remote_request=True,
        service_name="Mpesa",
        reference_doctype=doctype,
        reference_docname=name,
        status=status,
    )
