import json
from datetime import datetime
from enum import Enum

import frappe
import requests
from frappe.model.document import Document
from frappe.utils import get_datetime

from ...daraja.apis import initiator_party_a
from ...daraja.guard import is_zero_code
from ...daraja.money_out import result_receipt
from ...utils.doctype_names import B2C_REQUEST_DOCTYPE, MPESA_SETTINGS_DOCTYPE
from ..abstract import B2CConnector
from ..base_connector import (
    BaseAPIConnector,
    create_request_log,
    update_integration_request,
)


def queue_timeout_url() -> str:
    from frappe.utils import get_url

    from ...daraja.callbacks import with_token

    return with_token(
        get_url()
        + "/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.handle_queue_timeout"
    )


def parse_completed_datetime(value):
    """Daraja sends DD.MM.YYYY HH:MM:SS; dateutil would read 05.03.2024 as 3 May."""
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y %H:%M:%S")
    except ValueError:
        return get_datetime(value)


class BaseUrl(Enum):
    SANDBOX_URL = "https://sandbox.safaricom.co.ke"
    LIVE_URL = "https://api.safaricom.co.ke"


class MpesaB2CConnector(BaseAPIConnector, B2CConnector):
    """
    Handles OAuth token + B2C endpoint for Mpesa using BaseAPIConnector hooks.
    """

    provider_name = "Mpesa"

    def __init__(self, settings_name: str):
        super().__init__(
            provider="Mpesa",
            settings_doctype=MPESA_SETTINGS_DOCTYPE,
            settings_name=settings_name,
        )
        self._load_settings()

    def _load_settings(self):
        s = frappe.get_doc(
            self.settings_doctype, self.settings_name, ignore_permissions=True
        )
        self.consumer_key = s.consumer_key
        self.consumer_secret = s.get_password("consumer_secret")
        self.initiator_name = s.initiator_name
        self.security_credential = s.security_credential
        self.business_shortcode = s.business_shortcode
        self.party_a = initiator_party_a(s)  # the 600xxx test shortcode in sandbox
        self._token = s.access_token
        self._expiry = s.token_expiry
        self._base_url = (
            BaseUrl.SANDBOX_URL.value if s.sandbox else BaseUrl.LIVE_URL.value
        )

    def _get_valid_token(self) -> str:
        from ...daraja.client import fetch_token

        return fetch_token(self.consumer_key, self.consumer_secret, self._base_url)

    def send_b2c_request(self, b2c_request_doc: Document, callback_url: str) -> dict:
        """
        Dispatch a B2C payment request. Returns the raw JSON response.
        """

        token = self._get_valid_token()

        self.on_success(self._handle_success).on_error(self._handle_error)

        payload = {
            "InitiatorName": self.initiator_name,
            "SecurityCredential": self.security_credential,
            "CommandID": b2c_request_doc.commandid,
            "Amount": int(b2c_request_doc.amount),  # whole shillings, checked before sending
            "PartyA": self.party_a,
            "PartyB": b2c_request_doc.phone_number,
            "Remarks": b2c_request_doc.remarks,
            # A queue timeout is not a result: the payout may still complete, so it must
            # not reach the Result handler (which would mark it Failed and allow a retry).
            "QueueTimeOutURL": queue_timeout_url(),
            "ResultURL": callback_url,
            "Occasion": b2c_request_doc.occassion,
            "OriginatorConversationID": b2c_request_doc.originator_conversation_id,
        }

        return (
            self.describe("Mpesa B2C")
            .set_endpoint("/mpesa/b2c/v3/paymentrequest")
            .set_method("POST")
            .set_headers(
                {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            )
            .set_payload(payload)
            .make_remote_call(
                self._base_url,
                doctype=b2c_request_doc.doctype,
                document_name=b2c_request_doc.name,
            )
        )

    def _handle_success(
        self,
        response=None,
        data=None,
        doctype=None,
        document_name=None,
        integration_request=None,
        settings_name=None,
        **kwargs,
    ):
        try:
            response_code = data.get("ResponseCode")

            # ResponseCode 0 only means Safaricom accepted the request; money moves (or not)
            # when the Result callback arrives, which is the only place Paid is set.
            status = "Initiated" if is_zero_code(response_code) else "Failed"

            fields = {
                "conversation_id": data.get("ConversationID", ""),
                "originator_conversation_id": data.get("OriginatorConversationID", ""),
                "response_code": response_code,
                "response_description": data.get("ResponseDescription", ""),
                "status": status,
            }

            if frappe.db.exists(B2C_REQUEST_DOCTYPE, document_name):
                for key, value in fields.items():
                    frappe.db.set_value(B2C_REQUEST_DOCTYPE, document_name, key, value)
                frappe.log(f"B2C Request updated for {document_name}")

            update_integration_request(
                integration_request.name, status="Completed", output=str(data)
            )
            # no commit: runs inside B2C Disbursement Request.on_submit; the job commits
            frappe.publish_realtime(
                event="refresh_form", doctype=B2C_REQUEST_DOCTYPE, docname=document_name
            )

        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"B2C Request Success Error for {document_name}"
            )
            raise

    def _handle_error(
        self,
        response=None,
        data=None,
        error=None,
        doctype=None,
        document_name=None,
        integration_request=None,
        settings_name=None,
        **kwargs,
    ):
        if error is not None:
            frappe.log_error(
                title="Mpesa B2C Transport Error",
                message=frappe.get_traceback() or str(error),
            )
            update_integration_request(
                integration_request.name, status="Failed", output=str(error)
            )
            # Only a connect timeout proves the request never reached Safaricom. Any other
            # transport error (read timeout, reset) may have sent the money: the payout is
            # "Status Unknown" until a Transaction Status query settles it, and nothing can
            # resend it meanwhile (create_b2c_request and retry need Not Initiated / Failed).
            if isinstance(error, requests.exceptions.ConnectTimeout):
                fields = {"status": "Failed", "error_message": f"Not sent: {type(error).__name__}"}
            else:
                fields = {
                    "status": "Status Unknown",
                    "error_message": f"No answer from Safaricom ({type(error).__name__}); "
                    "a Transaction Status check runs before anything else happens.",
                }
            if frappe.db.exists(B2C_REQUEST_DOCTYPE, document_name):
                frappe.db.set_value(B2C_REQUEST_DOCTYPE, document_name, fields)
            # no commit: runs inside B2C Disbursement Request.on_submit; the job commits
            if fields["status"] == "Failed":
                frappe.enqueue(
                    "mpsa_suite.services.b2c_response_service.update_b2c_reference_status",
                    queue="short",
                    timeout=300,
                    enqueue_after_commit=True,
                    b2c_request_name=document_name,
                    enqueue_next=True,
                )
            return

        if response is not None and response.status_code >= 500 and not data.get("errorCode"):
            # A gateway error page, not a Daraja error: the request may have been processed.
            update_integration_request(integration_request.name, status="Failed", output=str(data))
            if frappe.db.exists(B2C_REQUEST_DOCTYPE, document_name):
                frappe.db.set_value(
                    B2C_REQUEST_DOCTYPE,
                    document_name,
                    {"status": "Status Unknown", "error_message": f"HTTP {response.status_code} without a Daraja error"},
                )
            return

        fields = {
            "status": "Failed",
            "error_code": data.get("errorCode", ""),
            "error_message": data.get("errorMessage", ""),
        }

        if frappe.db.exists(B2C_REQUEST_DOCTYPE, document_name):
            for key, val in fields.items():
                frappe.db.set_value(B2C_REQUEST_DOCTYPE, document_name, key, val)

        update_integration_request(
            integration_request.name, status="Failed", output=str(data)
        )
        # no commit: runs inside B2C Disbursement Request.on_submit; the job commits. Queued
        # after that commit: a worker starting earlier would read the request as it was
        # before Failed and leave the disbursement row Initiated for good.
        frappe.enqueue(
            "mpsa_suite.services.b2c_response_service.update_b2c_reference_status",
            queue="short",
            timeout=300,
            enqueue_after_commit=True,
            b2c_request_name=document_name,
            enqueue_next=True,
        )
        frappe.publish_realtime(
            event="refresh_form", doctype=B2C_REQUEST_DOCTYPE, docname=document_name
        )

    def handle_callback(self, payload: dict) -> None:
        """
        Process the full B2C callback payload, update the request doc,
        log to Integration Request, and enqueue downstream jobs.
        """

        result = frappe._dict(payload)
        occ_id = result.get("OriginatorConversationID")
        # Row lock serialises duplicate Result callbacks (Safaricom re-sends).
        name = frappe.db.get_value(
            B2C_REQUEST_DOCTYPE, {"originator_conversation_id": occ_id}, "name", for_update=True
        )
        if not name:
            frappe.throw(f"No B2C request for OriginatorConversationID {occ_id}")
        req = frappe.get_doc(B2C_REQUEST_DOCTYPE, name)
        if req.status in ("Paid", "Failed") and req.result_code not in (None, ""):
            # Final result already recorded; a re-send must not book the payout again.
            return

        self.integration_request = create_request_log(
            data=payload,
            request_description="Mpesa B2C Callback",
            is_remote_request=True,
            service_name=self.provider,
            request_headers={},
            url=f"{self._base_url}/b2c/callback",
            reference_doctype=req.doctype,
            reference_docname=req.name,
        )

        # ResultParameters is absent on many failures and ResultParameter is a single dict
        # (not a list) when Safaricom returns one item.
        params = (result.get("ResultParameters") or {}).get("ResultParameter") or []
        if isinstance(params, dict):
            params = [params]
        result_dict = {p.get("Key"): p.get("Value") for p in params if isinstance(p, dict)}

        paid = str(result.get("ResultCode")) == "0"
        update_map = {
            "status": "Paid" if paid else "Failed",
            "result_code": result.get("ResultCode"),
            "result_desc": result.get("ResultDesc"),
            "conversation_id": result.get("ConversationID"),
            # a failed attempt's TransactionID is no receipt (it stays in the callback's log)
            "transaction_id": result_receipt(result, paid),
            "recipient_is_registered_customer": result_dict.get(
                "B2CRecipientIsRegisteredCustomer"
            ),
            "charges_paid_acct_avlbl_funds": result_dict.get(
                "B2CChargesPaidAccountAvailableFunds"
            ),
            "utility_acct_avlbl_funds": result_dict.get(
                "B2CUtilityAccountAvailableFunds"
            ),
            "working_acct_avlbl_funds": result_dict.get(
                "B2CWorkingAccountAvailableFunds"
            ),
            "receiver_public_name": result_dict.get("ReceiverPartyPublicName"),
        }

        tcd = result_dict.get("TransactionCompletedDateTime")
        if tcd:
            update_map["transaction_completed_datetime"] = parse_completed_datetime(tcd)

        for k, v in update_map.items():
            if v is not None:
                setattr(req, k, v)
        req.save(ignore_permissions=True)

        if self.integration_request:
            # The callback log takes Integration Request statuses, not the payout's: upstream's
            # status=req.status made every Paid result fail validation and roll back (15.3.1,
            # which Astral runs, used "Completed").
            update_integration_request(
                self.integration_request.name,
                status="Completed" if req.status == "Paid" else "Failed",
                output=json.dumps(payload),
            )
        else:
            frappe.log_error(title="B2C callback", message=f"No integration_request on connector for {req.name}")

        # After the callback's commit (create_request_log above committed the old status):
        # a worker starting earlier would read Initiated, and a Failed result would never
        # reach the disbursement row.
        frappe.enqueue(
            "mpsa_suite.services.b2c_response_service.update_b2c_reference_status",
            queue="short",
            timeout=300,
            enqueue_after_commit=True,
            b2c_request_name=req.name,
            enqueue_next=True,
        )
