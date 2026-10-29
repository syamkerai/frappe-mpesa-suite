from __future__ import unicode_literals

import base64
import datetime
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, now_datetime

from ...daraja import apis
from ...daraja.callbacks import KIND_C2B, KIND_PULL, KIND_RESULT, safaricom_callback
from ...daraja.guard import daraja_id, daraja_timestamp, names_id, stk_amount, truncate_reference
from ...permissions import COLLECT_ROLES, FINANCE_ROLES, is_direct_call, require, require_for_direct_call

from ...utils.doctype_names import MPESA_EXPRESS_REQUEST_DOCTYPE, MPESA_SETTINGS_DOCTYPE
from ...utils.encoding_initiator_password import (
    generate_security_credential,
)
from ...utils.utils import (
    build_callback_url,
    log_and_throw_error,
    update_mpesa_request_status,
)
from .mpesa_response_handler import (
    BULK_PULL_FLAG,
    BULK_PULL_RESULTS_FLAG,
    balance_query_on_success,
    publish_pull_result,
    pull_transaction_on_error,
    pull_transaction_on_success,
    record_pull_outcome,
    stk_push_on_error,
    stk_push_on_success,
    transaction_status_on_success,
)
from .process_request import process_request


def _result_parameters(result_data: dict) -> list:
    """Result.ResultParameters.ResultParameter as a list of {Key, Value} pairs.

    Daraja sends a bare dict instead of a one-item list when there is a single parameter.
    """
    params = (result_data.get("ResultParameters") or {}).get("ResultParameter") or []
    if isinstance(params, dict):
        params = [params]
    return [p for p in params if isinstance(p, dict)]


ACCOUNT_BALANCE_RESULT_METHOD = (
    "mpsa_suite.mpsa_suite.api.m_pesa_api.account_balance_result"
)


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def account_balance_result(**kwargs) -> None:
    """Account Balance ResultURL. Daraja bans the word "query" in callback URLs."""
    _handle_balance_result(**kwargs)


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def balance_query_callback(**kwargs) -> None:
    """Upstream ResultURL name, kept so results for requests sent before the rename land."""
    _handle_balance_result(**kwargs)


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_PULL)
def pull_registration_result(**kwargs) -> dict:
    """Pull Transactions register CallBackURL. Daraja bans "mpesa" in callback URLs, which the
    older ``doctype.mpesa_settings...pull_transaction_callback`` path contains."""
    frappe.log_error(title="Mpesa Pull Transaction Callback", message=frappe.as_json(kwargs))
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


def _handle_balance_result(**kwargs) -> None:
    args = frappe._dict(kwargs)
    result_data = args.get("Result")
    if not result_data:
        frappe.log_error(
            "M-Pesa Balance Query Error", "Missing 'Result' in callback response"
        )
        return

    result_code = result_data.get("ResultCode")
    result_desc = result_data.get("ResultDesc", "No description provided")

    if result_code is None:
        frappe.log_error(
            "M-Pesa Balance Query Error", "Missing 'ResultCode' in callback response"
        )
        return

    conversation_id = daraja_id(result_data.get("ConversationID"))
    if not conversation_id:
        frappe.log_error(
            "M-Pesa Balance Query Error", "ConversationID missing or malformed in callback response"
        )
        return

    # Unsigned guest callback: the LIKE only narrows the search; the request is ours only if
    # it is a balance query whose stored ack has exactly this ConversationID.
    integration_request = [
        row
        for row in frappe.get_all(
            "Integration Request",
            filters={
                "request_description": "Mpesa Balance Query",
                "reference_doctype": MPESA_SETTINGS_DOCTYPE,
                "output": ["like", f"%{conversation_id}%"],
            },
            fields=["name", "output", "reference_docname"],
            order_by="creation desc",
            limit=20,
        )
        if names_id(row.output, "ConversationID", conversation_id)
    ]

    if not integration_request:
        frappe.log_error(
            "M-Pesa Balance Query Error",
            f"No matching Integration Request found for ConversationID: {conversation_id}",
        )
        return

    request_doc = frappe.get_doc("Integration Request", integration_request[0].name)
    request_doc.flags.ignore_permissions = True

    if str(result_code) != "0":
        request_doc.status = "Failed"
        request_doc.error = json.dumps(result_data, indent=4)
        request_doc.save(ignore_permissions=True)
        frappe.log_error(
            title="M-Pesa Balance Query Error",
            message=f"ResultCode: {result_code}, ResultDesc: {result_desc}, Data: {json.dumps(result_data, indent=4)}",
        )
        return

    account_balance = None
    result_params = _result_parameters(result_data)
    for param in result_params:
        if param.get("Key") == "AccountBalance":
            account_balance = param.get("Value")
            break

    if not account_balance:
        # checked before saving: an incomplete (or forged) result never changes the request
        frappe.log_error(
            "M-Pesa Balance Query Error", "AccountBalance missing in callback response"
        )
        return

    request_doc.output = json.dumps(result_data, indent=4)
    request_doc.status = "Completed"
    request_doc.save(ignore_permissions=True)

    settings_docname = integration_request[0].get("reference_docname")
    if not settings_docname:
        frappe.log_error(
            "M-Pesa Balance Query Error",
            "Reference document name missing in Integration Request",
        )
        return

    settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, settings_docname)
    settings.flags.ignore_permissions = True

    if account_balance:
        frappe.db.set_value(
            MPESA_SETTINGS_DOCTYPE, settings.name, "account_balance", account_balance
        )
        # each sub-account's balance, checked against the lines (wallet.balances)
        from ...wallet.balances import from_account_balance

        completed = next(
            (p.get("Value") for p in result_params if p.get("Key") == "BOCompletedTime"), None
        )
        from_account_balance(settings.name, account_balance, completed, request_doc.name)

    frappe.publish_realtime(
        event="refresh_form", doctype=MPESA_SETTINGS_DOCTYPE, docname=settings_docname
    )


@frappe.whitelist()
def get_account_balance(name: str) -> Any:
    """Call account balance API to send the request to the Mpesa Servers."""
    require_for_direct_call("get_account_balance", FINANCE_ROLES)
    try:
        settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, name)
        certs = frappe.get_single("Mpesa Public Key Certificate")
        cert_url = ""

        if settings.sandbox:
            cert_url = certs.sandbox_certificate
        else:
            cert_url = certs.production_certificate

        security_credential = generate_security_credential(
            (
                settings.get_password("initiator_password", "")
                if settings.initiator_password
                else ""
            ),
            cert_url,
            settings.sandbox,
        )

        endpoint = "/mpesa/accountbalance/v1/query"

        callback_url = build_callback_url(ACCOUNT_BALANCE_RESULT_METHOD)
        timeout_url = build_callback_url(
            "mpsa_suite.mpsa_suite.api.m_pesa_api.handle_queue_timeout"
        )

        payload = {
            "Initiator": settings.initiator_name,
            "SecurityCredential": settings.get_password("security_credential")
            or security_credential,
            "CommandID": "AccountBalance",
            "PartyA": apis.initiator_party_a(settings),
            "IdentifierType": "4",
            "Remarks": "Balance",
            "QueueTimeOutURL": timeout_url,
            "ResultURL": callback_url,
        }

        response = process_request(
            endpoint=endpoint,
            method="POST",
            payload=payload,
            success_callback=balance_query_on_success,
            request_description="Mpesa Balance Query",
            doctype=MPESA_SETTINGS_DOCTYPE,
            document_name=name,
            settings_name=name,
        )
        return response

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Balance Query Error")
        frappe.log_error(
            _("Failed to check mpesa balance. Please check the error logs.")
        )


@frappe.whitelist()
def check_transaction_status(name: str) -> Any:
    """Check the status of a transaction by its name."""
    if is_direct_call("check_transaction_status"):
        require(COLLECT_ROLES)
        frappe.has_permission(MPESA_EXPRESS_REQUEST_DOCTYPE, "read", doc=name, throw=True)
    try:
        express_request = frappe.get_doc(MPESA_EXPRESS_REQUEST_DOCTYPE, name)
        settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, express_request.settings)

        endpoint = "/mpesa/stkpushquery/v1/query"
        timestamp = daraja_timestamp()
        payload = {
            "BusinessShortCode": settings.business_shortcode,
            "Password": generate_request_password(settings, timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": express_request.checkout_request_id,
        }

        response = process_request(
            endpoint=endpoint,
            method="POST",
            payload=payload,
            success_callback=transaction_status_on_success,
            error_callback=transaction_status_error_callback,
            request_description="Mpesa Transaction Status Query",
            doctype=MPESA_EXPRESS_REQUEST_DOCTYPE,
            document_name=express_request.name,
            settings_name=express_request.settings,
            reuse_existing_request=True,
        )
        return response

    except Exception:
        frappe.log_error(frappe.get_traceback(), "STK Push Query Error")
        frappe.log_error(
            _("Failed to check transaction status. Please check the error logs.")
        )


def generate_request_password(settings: Document, timestamp: str) -> str:
    """Generate the password for making a request to the M-Pesa API."""
    shortcode = str(settings.business_shortcode).strip()
    passkey = str(settings.get_password("online_passkey")).strip()
    data_to_encode = f"{shortcode}{passkey}{timestamp}"
    return base64.b64encode(data_to_encode.encode("utf-8")).decode("utf-8")


# STK Query errorCodes meaning "still being processed - ask again", not "failed".
STK_QUERY_PENDING_ERROR_CODES = {"500.001.1001", "500.003.02"}


def transaction_status_error_callback(
    response: dict, payload: dict, document_name: str, **kwargs
) -> None:
    """Mark the prompt Failed only when Safaricom says it is finished (mirrors money_in.stk_poll)."""
    frappe.log_error(
        message=f"Transaction {document_name} query failed.\nResponse: {frappe.as_json(response)}",
        title="Mpesa Transaction Status Query Error",
    )

    error_code = str(response.get("errorCode") or "") if isinstance(response, dict) else ""
    if error_code in STK_QUERY_PENDING_ERROR_CODES:
        return  # the callback or the STK poller settles it later
    if (
        frappe.db.get_value(MPESA_EXPRESS_REQUEST_DOCTYPE, document_name, "status")
        == "Completed"
    ):
        return  # never downgrade a paid prompt on a late query error

    frappe.db.set_value(
        MPESA_EXPRESS_REQUEST_DOCTYPE,
        document_name,
        {
            "status": "Failed",
            "result_desc": (
                response.get("errorMessage")
                if isinstance(response, dict)
                else "Unknown error"
            ),
        },
    )


@frappe.whitelist()
def initiate_stk_push(**args) -> any:
    """Generate STK push by making an API call to the STK push API."""
    require_for_direct_call("initiate_stk_push", COLLECT_ROLES)

    # If args is a single key "args" containing a JSON string, parse it
    if len(args) == 1 and "args" in args:
        try:
            parsed_args = json.loads(args.get("args"))
            if isinstance(parsed_args, dict):
                args = frappe._dict(parsed_args)
            else:
                frappe.log_error(_("Invalid input format. Expected JSON object."))
        except json.JSONDecodeError:
            frappe.log_error(_("Failed to decode JSON arguments."))
    else:
        args = frappe._dict(args)

    required_fields = ["payment_gateway", "phone_number", "request_amount"]
    missing_fields = [field for field in required_fields if not args.get(field)]
    if missing_fields:
        frappe.log_error(
            title="Mpesa STK request invalid",
            message=_("Missing required fields: {0}").format(", ".join(missing_fields)),
        )
    if args.get("payment_gateway"):
        apis.require(args.payment_gateway[6:], apis.STK)  # outside the try: the caller must see it
    if args.get("doctype") == MPESA_EXPRESS_REQUEST_DOCTYPE and args.get("document_name"):
        _check_can_prompt_again(args.document_name)  # desk button and both retry_stkpush
    mobile_number = sanitize_mobile_number(args.phone_number or args.sender)  # outside the try: a bad number is said

    try:
        callback_url = build_callback_url(
            "mpsa_suite.mpsa_suite.api.m_pesa_api.stk_push_callback"
        )
        mpesa_settings = frappe.get_doc(
            MPESA_SETTINGS_DOCTYPE, args.payment_gateway[6:]
        )
        amount = stk_amount(args.request_amount)
        business_shortcode = mpesa_settings.business_shortcode
        timestamp = daraja_timestamp()
        reference_name = args.get("reference_name", "Online Payment")

        payload = {
            "BusinessShortCode": business_shortcode,
            "Password": generate_request_password(mpesa_settings, timestamp),
            "Timestamp": timestamp,
            "Amount": amount,
            "PartyA": int(mobile_number),
            "PartyB": (
                business_shortcode
                if mpesa_settings.paybill_type == "Pay Bill"
                else mpesa_settings.till_number
            ),
            "PhoneNumber": int(mobile_number),
            "CallBackURL": callback_url,
            # Documented max is 12 chars, but C2B matching needs the full document
            # name back as BillRefNumber, so AccountReference is not truncated.
            "AccountReference": reference_name,
            "TransactionDesc": truncate_reference(reference_name, 13),
            "TransactionType": (
                "CustomerPayBillOnline"
                if mpesa_settings.paybill_type == "Pay Bill"
                else "CustomerBuyGoodsOnline"
            ),
        }

        endpoint = "/mpesa/stkpush/v1/processrequest"

        response = process_request(
            endpoint=endpoint,
            method="POST",
            payload=payload,
            success_callback=_stk_push_accepted,
            error_callback=stk_push_on_error,
            request_description="Mpesa STK Push",
            doctype=args.get("doctype", MPESA_SETTINGS_DOCTYPE),
            document_name=args.get("document_name", mpesa_settings.name),
            settings_name=mpesa_settings.name,
        )
        return response

    except Exception:
        frappe.log_error(frappe.get_traceback(), "STK Push Generation Error")
        frappe.log_error(_("Failed to generate STK push. Please check the error logs."))


STK_RESEND_AFTER_SECONDS = 90  # as gateway.intents.RESEND_AFTER_SECONDS


def _check_can_prompt_again(request_name: str) -> None:
    """Refuse a prompt on a paid Mpesa Express Request, on one whose last callback is still
    being settled or waits for a person (Pending / Mismatch / Unverified), or on one whose
    last prompt may still be on the customer's phone."""
    from frappe.utils import get_datetime, now_datetime

    row = frappe.db.get_value(
        MPESA_EXPRESS_REQUEST_DOCTYPE,
        request_name,
        ["status", "transaction_id", "verification_status", "checkout_request_id", "timestamp", "creation"],
        as_dict=True,
    )
    if not row:
        return
    reason = _prompt_settled_or_undecided(request_name, row)
    if reason:
        frappe.throw(reason)
    # Accepted moments ago and nothing back yet: it is still on the customer's phone, and a
    # new prompt would replace its CheckoutRequestID before its callback arrives.
    sent = (row.timestamp or row.creation) if row.checkout_request_id and not row.verification_status else None
    if sent and (now_datetime() - get_datetime(sent)).total_seconds() < STK_RESEND_AFTER_SECONDS:
        frappe.throw(
            _("A prompt was just sent on {0}; wait for it to finish before sending another.").format(request_name)
        )


def _prompt_settled_or_undecided(request_name: str, row) -> str | None:
    """Why the last prompt on ``row`` must be left as it is: paid, or its callback is still
    being settled or waits for a person. None when another prompt may replace it."""
    from ...daraja.verify import MISMATCH, PENDING

    if row.status == "Completed" or row.transaction_id:
        return _("{0} is already paid; it cannot be prompted again.").format(request_name)
    if row.verification_status in (PENDING, MISMATCH, "Unverified"):
        return _("The last prompt on {0} is {1}. Settle it before sending another prompt.").format(
            request_name, row.verification_status
        )
    return None


def _stk_push_accepted(response: dict, payload: dict, document_name: str, **kwargs) -> None:
    """stk_push_on_success, then clear what an earlier prompt on the same request settled.

    A re-sent prompt reuses the Mpesa Express Request with a new CheckoutRequestID; left as
    Failed / Verified, the new prompt's callback was dropped and the poller skipped it.
    The guard read the row a Safaricom round trip ago, so it is read again under the lock:
    if the earlier prompt's callback was stored or settled meanwhile, the request keeps that
    result untouched (never downgrade a paid prompt) and a person is told about the new one.
    """
    resets = kwargs.get("doctype") == MPESA_EXPRESS_REQUEST_DOCTYPE and (response or {}).get("CheckoutRequestID")
    if resets:
        row = frappe.db.get_value(
            MPESA_EXPRESS_REQUEST_DOCTYPE,
            document_name,
            ["status", "transaction_id", "verification_status", "checkout_request_id"],
            as_dict=True,
            for_update=True,
        )
        reason = _prompt_settled_or_undecided(document_name, row) if row else None
        if reason:
            frappe.log_error(
                title=f"M-Pesa STK prompt not tracked for {document_name}"[:140],
                message=(
                    f"Safaricom accepted a new prompt ({response.get('CheckoutRequestID')}) while the earlier one "
                    f"({row.checkout_request_id}) was being settled: {reason}\n"
                    f"{document_name} keeps the earlier prompt's result. If the customer pays the new prompt "
                    "as well, its callback will not find this request: check it on the M-Pesa portal."
                ),
            )
            return
    stk_push_on_success(response=response, payload=payload, document_name=document_name, **kwargs)
    if resets:
        frappe.db.set_value(
            MPESA_EXPRESS_REQUEST_DOCTYPE,
            document_name,
            {
                "status": "In Progress",
                "verification_status": None,
                "callback_payload": None,
                "result_code": None,
                "result_desc": None,
                "booking_attempts": 0,
            },
        )


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def stk_push_callback(**kwargs) -> None:
    """Store Safaricom's STK result and queue its settlement; nothing is booked here.

    money_in.stk_verify.settle confirms the callback with an STK Query (callbacks are not
    signed), records status and receipt, commits, and only then books the payment, so a
    forged callback books nothing and a booking failure cannot lose the receipt.
    """
    from ...daraja.verify import PENDING, VERIFIED
    from ...money_in.stk_verify import queue

    checkout_request_id = None
    try:
        transaction_response = frappe._dict(kwargs["Body"]["stkCallback"])
        checkout_request_id = transaction_response.get("CheckoutRequestID")
        if not isinstance(checkout_request_id, str):
            log_and_throw_error("Invalid Checkout Request ID")

        frappe.db.get_value(
            MPESA_EXPRESS_REQUEST_DOCTYPE,
            {"checkout_request_id": checkout_request_id},
            "name",
            for_update=True,
        )
        request_doc = frappe.get_doc(
            MPESA_EXPRESS_REQUEST_DOCTYPE, {"checkout_request_id": checkout_request_id}
        )
        if request_doc.get("verification_status") == VERIFIED and _settled_prompt(request_doc, checkout_request_id):
            return  # Safaricom re-sent a callback we already settled

        update_mpesa_request_status(
            request_doc.name,
            {
                "callback_payload": json.dumps(dict(transaction_response)),
                "verification_status": PENDING,
            },
        )

        integration_req = frappe.db.get_value(
            "Integration Request", {"output": ["like", f"%{checkout_request_id}%"]}, ["name", "output"], as_dict=True
        )
        if integration_req:
            frappe.db.set_value(
                "Integration Request",
                integration_req.name,
                "output",
                json.dumps(
                    {
                        "stkpush_response": integration_req.output or "{}",
                        "callback_result": transaction_response,
                    },
                    indent=4,
                ),
            )

        queue(request_doc.name)

    except Exception:
        log_and_throw_error("STK Push Callback Error", checkout_request_id)


def _settled_prompt(request_doc, checkout_request_id: str) -> bool:
    """True if a Verified request's settlement covers this callback's prompt.

    A paid request is final. A failed one may have been prompted again on the same document
    (new CheckoutRequestID); that prompt's callback must be settled, not dropped.
    """
    if request_doc.get("status") == "Completed" or request_doc.get("transaction_id"):
        return True
    try:
        settled = (json.loads(request_doc.get("callback_payload") or "{}") or {}).get("CheckoutRequestID")
    except (TypeError, ValueError, AttributeError):
        return True
    return not settled or settled == checkout_request_id


def publish_stk_status(status: str, payment_request):
    expected_token = frappe.db.get_value(
        "Payment Request", payment_request, "payment_token"
    )

    if not expected_token:
        return

    room = "website"

    frappe.publish_realtime(
        "stk_payment_complete",
        {
            "status": status,
            "expected_token": expected_token,
        },
        room=room,
    )


def sanitize_mobile_number(number: str) -> str:
    """2547XXXXXXXX / 2541XXXXXXXX via utils.phone (also '+254 0712...', '00254 712...' and
    '0254712...': utils.phone.repaired_msisdn); any other number is refused, never guessed from its
    last 9 digits, which can be another subscriber's."""
    from ...utils.phone import FORMATS, digits, repaired_msisdn, shown

    msisdn = repaired_msisdn(number)
    num = digits(number)
    if not msisdn and num:
        frappe.throw(_("{0} is not a Kenyan mobile number: enter {1}.").format(shown(number), FORMATS))
    if not msisdn:
        frappe.throw(_("Enter the M-Pesa phone number: {0}.").format(FORMATS))
    return msisdn


def get_token(app_key, app_secret, base_url):
    """Cached OAuth token (see daraja.client); kept for existing callers."""
    from ...daraja.client import fetch_token

    return fetch_token(app_key, app_secret, base_url)


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_C2B)
def confirmation(**kwargs):
    try:
        args = frappe._dict(kwargs)

        # Runs as the integration user (see safaricom_callback), so the job does too.
        frappe.enqueue(
            "mpsa_suite.mpsa_suite.api.m_pesa_api.delayed_insert_c2b",
            queue="short",
            timeout=300,
            is_async=True,
            c2b_data={
                "transactiontype": args.get("TransactionType"),
                "transid": args.get("TransID"),
                "transtime": args.get("TransTime"),
                "transamount": flt(args.get("TransAmount")),
                "businessshortcode": args.get("BusinessShortCode"),
                "billrefnumber": args.get("BillRefNumber"),
                "invoicenumber": args.get("InvoiceNumber"),
                "orgaccountbalance": args.get("OrgAccountBalance"),
                "thirdpartytransid": args.get("ThirdPartyTransID"),
                "msisdn": args.get("MSISDN"),
                "firstname": args.get("FirstName"),
                "middlename": args.get("MiddleName"),
                "lastname": args.get("LastName"),
            },
        )

        frappe.db.commit()
        context = {"ResultCode": 0, "ResultDesc": "Accepted"}
        return dict(context)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), str(e)[:140])
        context = {"ResultCode": 1, "ResultDesc": "Rejected"}
        return dict(context)


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def c2b_verification_result(**kwargs):
    """Transaction Status result for a held C2B confirmation (money_in.c2b_verify).

    The held IR is named in the ResultURL (``?ir=``), but Frappe builds the arguments of a
    JSON POST (Safaricom's) from the body only, so it is read from the request itself, and
    failing that found by the result's IDs. apply_result still checks those IDs.
    """
    from mpsa_suite.money_in.c2b_verify import SERVICE, find_held

    result = kwargs.get("Result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if not isinstance(result, dict):
        return {"ResultCode": 1, "ResultDesc": "Rejected"}
    ir = _query_arg("ir") or kwargs.get("ir")
    if not isinstance(ir, str) or not ir:
        ir = find_held(result)
    if not ir or not frappe.db.exists("Integration Request", {"name": ir, "integration_request_service": SERVICE}):
        return {"ResultCode": 1, "ResultDesc": "Rejected"}
    frappe.enqueue(
        "mpsa_suite.mpsa_suite.api.m_pesa_api.apply_c2b_verification",
        queue="short",
        timeout=300,
        enqueue_after_commit=True,
        ir=ir,
        result=result,
    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


def _query_arg(name: str) -> str | None:
    """A query-string parameter of the current request (see callbacks._request_token)."""
    request = getattr(frappe.local, "request", None)
    args = getattr(request, "args", None)
    return args.get(name) if args is not None else None


def apply_c2b_verification(ir: str, result: dict) -> None:
    from mpsa_suite.money_in.c2b_verify import apply_result

    try:
        apply_result(ir, result)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), f"M-Pesa C2B verification failed for {ir}"[:140])


# Daraja C2B "Response Codes": accept with the string "0"; reject only with C2B00011-C2B00016.
C2B_VALIDATION_ACCEPTED = {"ResultCode": "0", "ResultDesc": "Accepted"}
C2B_VALIDATION_REJECTED = {"ResultCode": "C2B00016", "ResultDesc": "Rejected"}  # "Other Error"


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_C2B, rejected=C2B_VALIDATION_REJECTED)
def validation(**kwargs):
    return dict(C2B_VALIDATION_ACCEPTED)


def delayed_insert_c2b(c2b_data: dict) -> None:
    """
    Insert C2B Payment Register after a small delay.
    This ensures that any Express Request with the same transaction_id
    is already created and can be prioritized.
    """
    from mpsa_suite.money_in import c2b_verify
    from mpsa_suite.money_in.common import enabled, settings_for_shortcode
    from mpsa_suite.money_in.receipts import STK_OWNED, insert_register

    try:
        time.sleep(1)

        if not settings_for_shortcode(c2b_data.get("businessshortcode")):
            # Not one of our paybills/tills: forged, or a shortcode nobody configured.
            frappe.log_error(
                title="M-Pesa C2B confirmation ignored",
                message=f"C2B confirmation for unknown BusinessShortCode: {frappe.as_json(c2b_data)}",
            )
            return

        if enabled("verify_c2b_confirmations"):
            shortcode = c2b_data.get("businessshortcode")
            if c2b_verify.can_verify(shortcode):
                # Unsigned callback: the register is created only once Transaction Status
                # confirms the receipt, amount and shortcode (money_in.c2b_verify).
                c2b_verify.hold(c2b_data)
                return
            # No initiator to ask with: register it as it arrives, as before the suite.
            c2b_verify.note_unverified(shortcode)

        # Safaricom re-sends confirmations; keep exactly one live register per receipt.
        outcome, _name = insert_register(c2b_data)
        if outcome == STK_OWNED:
            frappe.log_error(
                title="C2B Insert Skipped",
                message=f"C2B {c2b_data['transid']} blocked due to existing Express Request",
            )
        frappe.db.commit()

    except Exception:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), "Delayed C2B Insert Error")


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def handle_transaction_status_result():
    """Handle the transaction status response from Mpesa"""
    try:
        response = frappe.request.data
        response_data = json.loads(response)

        integration_request = frappe.get_doc(
            {
                "doctype": "Integration Request",
                "is_remote_request": 1,
                "integration_request_service": "Mpesa Transaction Status Result Callback",
                "reference_doctype": "Mpesa C2B Payment Register",
                "status": "Queued",
                "data": json.dumps(response_data),
                "url": frappe.request.url,
                "method": "POST",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.enqueue(
            "mpsa_suite.mpsa_suite.api.m_pesa_api.process_mpesa_integration_request",
            queue="short",
            timeout=300,
            job_id=f"mpesa_process_{integration_request.name}",
            integration_request_name=integration_request.name,
            deduplicate=True,
        )

        return {"status": "queued", "message": "Transaction queued for processing"}

    except json.JSONDecodeError as e:
        frappe.log_error(
            title="Mpesa API Error", message=f"Failed to decode JSON from Mpesa response: {str(e)}"
        )
        return {"status": "error", "message": "Invalid JSON data"}
    except Exception as e:
        frappe.log_error(title="Mpesa API Error", message=f"Error in Mpesa webhook: {str(e)}")
        return {"status": "error", "message": f"Webhook error: {str(e)}"}


STATUS_EVENT = "mpesa_transaction_status_update"  # what Mpesa Settings and the C2B register list listen for
STATUS_QUERY = "Mpesa Transaction Status"
STATUS_RESULT = "Mpesa Transaction Status Result Callback"
# A result whose query's ack is not recorded yet (Safaricom has answered 0.4 s after the ack) is
# kept Queued and tried again every two minutes (retry_waiting_status_results), this long at most.
STATUS_WAIT_MINUTES = 10
PAYOUT_DOCTYPES = ("B2C Disbursement Request", "Mpesa B2B Payment Request")


def publish_transaction_status(user, status: str, message: str, **extra) -> None:
    """Tell the person who asked: their "Waiting for M-Pesa callback..." dialog closes on any
    status but "queued". Nobody is told about a result nobody asked for."""
    if not user or user == "Guest":
        return
    frappe.publish_realtime(
        event=STATUS_EVENT,
        message={"status": status, "message": message, "title": _("M-Pesa Transaction Status"), **extra},
        user=user,
    )


def _find_status_query(conversation_id: str, ref: str | None = None):
    """The "Mpesa Transaction Status" request a result answers, _dict(name, owner, data, output,
    status), or None.

    By the ``ref`` on its ResultURL: a locking read, which waits while the sender is recording the
    ack (one still Queued has no ack yet, and the caller waits for it); the ack must have exactly
    this ConversationID. Else (a query sent before the ref) by that ID; the LIKE only narrows the
    search (an ID of "%" used to match the newest query)."""
    fields = ["name", "owner", "data", "output", "status"]
    if ref:
        row = frappe.db.get_value(
            "Integration Request", {"name": ref, "integration_request_service": STATUS_QUERY}, fields,
            as_dict=True, for_update=True,
        )
        if row:
            return row if row.status == "Queued" or names_id(row.output, "ConversationID", conversation_id) else None
    return next(
        (
            row
            for row in frappe.get_all(
                "Integration Request",
                filters={
                    "integration_request_service": STATUS_QUERY,
                    "status": "Completed",
                    "output": ["like", f"%{conversation_id}%"],
                },
                fields=fields,
                order_by="creation desc",
                limit=20,
            )
            if names_id(row.output, "ConversationID", conversation_id)
        ),
        None,
    )


def _asked(row) -> dict:
    try:
        return json.loads(row.data or "{}") or {}
    except ValueError:
        return {}


def _answer_problem(asked: dict, receipt_no: str) -> str | None:
    """Why a result is not the answer to the query (its stored data ``asked``), or None."""
    if str(asked.get("transaction_id") or "").strip().upper() != str(receipt_no).strip().upper():
        return f"Result is for {receipt_no}, but we asked about {asked.get('transaction_id')}"
    return None


def _c2b_receipt_problem(asked: dict, result_params: dict, receipt_no: str, credit_shortcode: str) -> str | None:
    """Why the receipt is not completed money paid to us (so no C2B receipt is made), or None."""
    from ...money_in.c2b_verify import own_shortcodes

    # ResultCode 0 only means the query worked; the receipt itself may be Declined or Reversed
    state = str(result_params.get("TransactionStatus") or "").strip()
    if state.lower() != "completed":
        return f"Receipt {receipt_no} has status {state or '(none)'}, not Completed"
    if not credit_shortcode or credit_shortcode not in own_shortcodes(asked.get("mpesa_settings")):
        return (
            f"Receipt {receipt_no} was not paid to {asked.get('mpesa_settings')} "
            f"(CreditPartyName {credit_shortcode or 'missing'})"
        )
    return None


def _unsolicited_status_result(
    result_data: dict, result_params: dict, receipt_no: str, credit_shortcode: str
) -> str | None:
    """Reason to reject a Transaction Status result that answers no query of ours, or does not
    show completed money paid to us; None when it may be registered."""
    conversation_id = daraja_id(result_data.get("ConversationID"))
    if not conversation_id or not receipt_no:
        return "Result has no valid ConversationID or no receipt"
    sent = _find_status_query(conversation_id)
    if not sent:
        return f"No Transaction Status query of ours has ConversationID {conversation_id}"
    asked = _asked(sent)
    return _answer_problem(asked, receipt_no) or _c2b_receipt_problem(asked, result_params, receipt_no, credit_shortcode)


def _url_param(url, name: str) -> str | None:
    values = parse_qs(urlparse(str(url or "")).query).get(name)
    return values[0] if values else None


def _close(integration_request, status: str, output: str) -> None:
    integration_request.status = status
    integration_request.output = output
    integration_request.save(ignore_permissions=True)
    frappe.db.commit()


def _keep_waiting(integration_request, sent) -> None:
    """Leave the result Queued for retry_waiting_status_results (its query ``sent`` has no ack
    recorded yet), counting the tries."""
    try:
        tries = cint((json.loads(integration_request.output or "{}") or {}).get("tries"))
    except (TypeError, ValueError, AttributeError):
        tries = 0
    integration_request.output = json.dumps({
        "waiting_for": sent.name,
        "tries": tries + 1,
    })
    integration_request.save(ignore_permissions=True)
    frappe.db.commit()


def _payout_for(receipt_no: str, values: dict):
    """(doctype, name) of the ERP payout the receipt belongs to (by receipt, else by the answer's
    own ConversationID or OriginatorConversationID), or None."""
    receipt = daraja_id(receipt_no)
    ids = [("conversation_id", daraja_id(values.get("ConversationID"))),
           ("originator_conversation_id", daraja_id(values.get("OriginatorConversationID")))]
    for lookups in ([("transaction_id", receipt)], ids):
        for doctype in PAYOUT_DOCTYPES:
            for field, value in lookups:
                name = value and frappe.db.get_value(doctype, {field: value}, "name", order_by="creation desc")
                if name:
                    return doctype, name
    return None


def _hidden_from(doctype: str, name: str, asker: str) -> str | None:
    """Why the asker may not see who the payout paid, how much and where, or None: the rule its own
    form follows (money_out.salary_access.has_permission). A salary (a B2C payout to an employee
    for no claim or advance, or for a Salary Slip; a B2B payout to an employee for no claim or
    advance) is for who may see salaries; a payout of an Expense Claim or Employee Advance is not a
    salary, but stays hidden from an asker who cannot read that DocType. Its creator sees it."""
    from ...money_out import salary_access

    if doctype == "Mpesa B2B Payment Request":
        links = [f for f in salary_access.B2B_DOCUMENT_FIELDS if frappe.db.has_column(doctype, f)]
        fields = ["employee", "owner", *links]
    else:
        fields = ["party_type", "reference_doctype", "owner"]
    row = frappe.db.get_value(doctype, name, fields, as_dict=True)
    if not row:
        return None
    payout = frappe._dict(row, doctype=doctype)
    if salary_access.has_permission(payout, "read", user=asker):
        return None
    return "salary" if salary_access.has_salaries(payout) else "document"


def _answer_payout(integration_request, asker, payout, result_data: dict, values: dict) -> None:
    """A status answer for one of our payouts' receipts (handover F11): no C2B receipt is made, and
    no error logged. The person who asked sees the payout's status, receiver, charge and time; a
    B2B payout gets what its Result left out (money_out.b2b_status)."""
    from ...daraja.money_out import parse_daraja_datetime
    from ...money_out import b2b_status
    from ...money_out.statement import parse_charges

    doctype, name = payout
    kind = "B2B" if doctype == b2b_status.B2B_DOCTYPE else "B2C"
    filled, note = [], ""
    if kind == "B2B":
        outcome, reason, filled = b2b_status.apply_answer(
            name, result_data, f"Transaction Status result {integration_request.name}"
        )
        if outcome != "Paid":
            note = f" Not applied to {name}: {reason}."
        elif not filled and frappe.db.get_value(doctype, name, "status") != "Paid":
            note = f" Press Check Status on {name} to settle it."
    state = cstr(values.get("TransactionStatus")).strip() or "(no status)"
    hidden = _hidden_from(doctype, name, asker)
    if hidden:
        # salary data (decision D4), or a claim's or advance's payout the asker cannot read: the asker
        # learns only that the receipt is the ERP's own payout
        message = ("This receipt is an ERP salary payout." if hidden == "salary" else
                   "This receipt is an ERP payout of an Expense Claim or Employee Advance, which you cannot read.")
        _close(integration_request, "Completed", json.dumps({"kind": kind, "salary": 1, "filled": filled}))
        publish_transaction_status(asker, "success" if state.lower() == "completed" else "warning", message)
        return
    finalised = parse_daraja_datetime(values.get("FinalisedTime"))
    parts = [f"KES {flt(values.get('Amount')):,.2f} to {values.get('CreditPartyName') or 'an unnamed receiver'}"]
    if cstr(values.get("DebitPartyCharges")).strip():
        parts.append(f"charge KES {parse_charges(values.get('DebitPartyCharges')):,.2f}")
    if finalised:
        parts.append(finalised.strftime("%d-%m-%Y %H:%M"))
    message = f"Payout {name} {state}: {', '.join(parts)}. In the ERP it is {frappe.db.get_value(doctype, name, 'status')}."
    if filled:
        message += f" Filled on {name}: {', '.join(b2b_status.LABELS[f] for f in filled)}."
    message += note
    integration_request.reference_doctype = doctype
    integration_request.reference_docname = name
    _close(integration_request, "Completed", json.dumps({
        "kind": kind,
        "payout": name,
        "ReceiptNo": values.get("ReceiptNo"),
        "TransactionStatus": values.get("TransactionStatus"),
        "Amount": values.get("Amount"),
        "CreditPartyName": values.get("CreditPartyName"),
        "DebitPartyCharges": values.get("DebitPartyCharges"),
        "FinalisedTime": values.get("FinalisedTime"),
        "filled": filled,
    }, default=str))
    publish_transaction_status(asker, "success" if state.lower() == "completed" else "warning", message,
                               document_name=name, doctype=doctype)


def _paid_by_us(asked: dict, values: dict) -> str | None:
    """Our shortcode when the receipt was paid from it (DebitPartyName "985401 - ..."), else None."""
    from ...money_in.c2b_verify import own_shortcodes

    debit = str(values.get("DebitPartyName") or "").split("-")[0].strip()
    return debit if debit and debit in own_shortcodes(asked.get("mpesa_settings")) else None


def process_mpesa_integration_request(integration_request_name, final: bool = False):
    """Process a Transaction Status result (Mpesa Settings → Check Transaction Status) and tell
    whoever asked, on every outcome.

    Only the answer to a query we sent counts, for the receipt we asked about. A payout's receipt
    (B2C or B2B) is answered with the payout's status, receiver, charge and time; a receipt
    completed and paid to one of our shortcodes is registered as a C2B receipt; one paid from our
    shortcode with no ERP payout is flagged (money that left outside the ERP). ``final``: the last
    try for a result still waiting for its query's ack (retry_waiting_status_results)."""
    # locking read: the result's job and the retry sweep never handle one result twice
    if frappe.db.get_value("Integration Request", integration_request_name, "status", for_update=True) != "Queued":
        return
    integration_request = frappe.get_doc("Integration Request", integration_request_name)
    asker = None
    try:
        # Parse the stored data
        response_data = json.loads(integration_request.data)
        result_data = response_data.get("Result", {}) or {}
        result_params = {
            param.get("Key", ""): param.get("Value", "")
            for param in _result_parameters(result_data)
            if "Key" in param
        }
        from ...daraja.money_out import result_values

        values = result_values(result_data)  # the first of a repeated key (our own DebitPartyName)

        result_code = result_data.get("ResultCode", None)
        receipt_no = result_params.get("ReceiptNo", "")
        business_shortcode = str(result_params.get("CreditPartyName") or "").split("-")

        # Unsigned guest callback: only the answer to a query we actually sent counts.
        conversation_id = daraja_id(result_data.get("ConversationID"))
        sent = _find_status_query(conversation_id, _url_param(integration_request.url, "ref")) if conversation_id else None
        if not sent or sent.status == "Queued":
            if sent and not final:
                _keep_waiting(integration_request, sent)  # the query's ack is not recorded yet
                return
            if not conversation_id:
                rejection = "Result has no valid ConversationID or no receipt"
            elif sent:
                rejection = f"The ack of Transaction Status query {sent.name} was never recorded"
            else:
                rejection = f"No Transaction Status query of ours has ConversationID {conversation_id}"
            _close(integration_request, "Failed", rejection)
            frappe.log_error(f"{rejection}\n\n{integration_request.data}", "M-Pesa Transaction Status result rejected")
            publish_transaction_status(sent and sent.owner, "error", rejection)
            return
        asker = sent.owner
        asked = _asked(sent)

        if str(result_code) != "0":
            error_msg = (f"Safaricom could not answer for {asked.get('transaction_id')}: "
                         f"{result_data.get('ResultDesc') or 'no reason given'} ({result_code})")
            _close(integration_request, "Failed", error_msg)
            publish_transaction_status(asker, "error", error_msg)
            return

        problem = "Result has no valid ConversationID or no receipt" if not receipt_no else _answer_problem(asked, receipt_no)
        if problem:
            _close(integration_request, "Failed", problem)
            publish_transaction_status(asker, "error", problem)
            return

        payout = _payout_for(receipt_no, values)
        if payout:
            _answer_payout(integration_request, asker, payout, result_data, values)
            return

        rejection = _c2b_receipt_problem(asked, result_params, receipt_no, business_shortcode[0].strip())
        if rejection:
            ours = _paid_by_us(asked, values)
            if ours:
                from ...money_in.common import log_match

                message = (
                    f"Receipt {receipt_no} paid KES {flt(values.get('Amount')):,.2f} from {ours} to "
                    f"{values.get('CreditPartyName') or 'an unnamed receiver'}, but no B2C or B2B payout of the ERP has "
                    "it: money left the shortcode outside the ERP (a payment on the M-Pesa portal?)."
                )
                log_match("Ambiguous", "Bank Reconciliation", receipt=receipt_no, amount=flt(values.get("Amount")),
                          details=message)
                _close(integration_request, "Completed", message)
                publish_transaction_status(asker, "warning", message)
                return
            # an answer, just not money paid to us: told to the asker, not an error
            _close(integration_request, "Failed", rejection)
            publish_transaction_status(asker, "error", rejection)
            return

        from ...money_in.receipts import INSERTED, insert_register

        debit_party = str(result_params.get("DebitPartyName") or "").split(" - ")
        name_parts = (
            debit_party[1].split(" ") if len(debit_party) > 1 else ["", "", ""]
        )
        outcome, register_name = insert_register(
            {
                "full_name": result_params.get("DebitPartyName", ""),
                "transactiontype": result_params.get("ReasonType", ""),
                "transid": receipt_no,
                "transtime": result_params.get("InitiatedTime", ""),
                "transamount": flt(result_params.get("Amount", 0.0)),
                "businessshortcode": business_shortcode[0].strip(),
                # Transaction Status does not return the customer's account reference;
                # leave billrefnumber empty rather than copy the receipt (it broke matching).
                "invoicenumber": result_params.get("TransactionID", ""),
                "orgaccountbalance": result_params.get("DebitAccountType", ""),
                "thirdpartytransid": result_params.get("OriginatorConversationID", ""),
                "msisdn": debit_party[0] if len(debit_party) > 0 else "",
                "firstname": name_parts[0],
                "middlename": name_parts[1] if len(name_parts) > 1 else "",
                "lastname": name_parts[-1] if len(name_parts) > 2 else "",
            }
        )
        if outcome != INSERTED:
            error_msg = f"Receipt {receipt_no} is already recorded" + (f" ({register_name})" if register_name else "")
            _close(integration_request, "Failed", error_msg)
            publish_transaction_status(asker, "warning", error_msg, document_name=register_name)
            return
        frappe.db.commit()

        success_msg = f"Receipt {receipt_no} recorded as {register_name}"
        integration_request.reference_document = register_name
        _close(integration_request, "Completed", success_msg)
        publish_transaction_status(asker, "success", success_msg, document_name=register_name)

    except Exception as e:
        error_message = f"Mpesa Processing Error: {str(e)}"
        integration_request.status = "Failed"
        integration_request.output = error_message
        integration_request.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.log_error(
            f"{error_message}\nData: {integration_request.data}",
            "Mpesa Integration Error",
        )
        publish_transaction_status(asker, "error", error_message)


def retry_waiting_status_results(limit: int = 50) -> None:
    """Every two minutes: Transaction Status results still Queued a minute after they came (their
    query's ack was not recorded yet, or their job was lost) are tried again; after
    STATUS_WAIT_MINUTES, for the last time (rejected if the ack never came)."""
    now = now_datetime()
    rows = frappe.get_all(
        "Integration Request",
        filters={"integration_request_service": STATUS_RESULT, "status": "Queued",
                 "creation": [">=", add_to_date(now, hours=-24)]},
        fields=["name", "creation"],
        order_by="creation asc",
        limit=limit,
    )
    for row in rows:
        created = get_datetime(row.creation)
        if created > add_to_date(now, minutes=-1):
            continue  # its own job is still on its way
        try:
            process_mpesa_integration_request(row.name, final=created <= add_to_date(now, minutes=-STATUS_WAIT_MINUTES))
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa Transaction Status result {row.name} failed"[:140])


@frappe.whitelist(allow_guest=True)
@safaricom_callback(KIND_RESULT)
def handle_queue_timeout():
    """Handle the timeout response from Mpesa."""
    try:
        response = frappe.request.data
        response_data = json.loads(response)

        frappe.log_error(
            title="Mpesa Queue Timeout",
            message=f"Timeout response received: {frappe.as_json(response_data)}",
        )

        return {"status": "timeout", "message": "Timeout response logged successfully."}

    except json.JSONDecodeError:
        frappe.log_error(
            title="Mpesa Timeout Error",
            message="Failed to decode JSON from timeout response.",
        )
        return {"status": "error", "message": "Invalid JSON received."}

    except Exception as e:
        error_message = f"Mpesa Timeout Error: {str(e)}"
        frappe.log_error(title="Mpesa Timeout Error", message=error_message)
        return {"status": "error", "message": str(e)}


def build_pull_payload(
    mpesa_settings: str, start_date: str, end_date: str, offset: int = 0
) -> dict:
    def _fmt(dt_str: str) -> str:
        return datetime.datetime.strptime(dt_str.strip(), "%Y-%m-%d %H:%M:%S").strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    settings = frappe.get_doc(MPESA_SETTINGS_DOCTYPE, mpesa_settings)
    shortcode = (
        settings.till_number if settings.sandbox else settings.business_shortcode
    )

    return {
        "ShortCode": shortcode,
        "StartDate": _fmt(start_date),
        "EndDate": _fmt(end_date),
        "OffSetValue": str(int(offset)),
    }


@frappe.whitelist()
def pull_transactions(
    mpesa_settings: str,
    start_date: str,
    end_date: str,
    offset: int = 0,
) -> dict:
    require_for_direct_call("pull_transactions", FINANCE_ROLES)
    try:
        payload = build_pull_payload(mpesa_settings, start_date, end_date, offset)

    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Pull Transaction Error")
        return {
            "status": "error",
            "message": "Failed to pull transactions. Check Error Logs.",
        }

    try:
        frappe.enqueue(
            "mpsa_suite.mpsa_suite.api.m_pesa_api.execute_pull_transactions",
            queue="long",
            timeout=600,
            mpesa_settings=mpesa_settings,
            payload=payload,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Pull Transaction Error")
        return {
            "status": "error",
            "message": "Failed to pull transactions. Check Error Logs.",
        }

    return {
        "status": "success",
        "message": "Pull request queued. Importing transactions in the background...",
    }


# Connect timeouts to api.safaricom.co.ke are routine when the hourly job fans
# out one request per Mpesa Settings doc. Retry rather than lose the window.
PULL_MAX_ATTEMPTS = 3
PULL_RETRY_BACKOFF_SECONDS = 2

# Safety stop for the pagination walk, in case TotalPages is missing or wrong.
PULL_MAX_PAGES = 50


def execute_pull_transactions(mpesa_settings: str, payload: dict) -> None:
    """Pull every page Safaricom has for the window, not just the first one.

    Safaricom paginates: a 48h window on a busy shortcode came back as
    TotalRecords 318 across TotalPages 4. We used to send OffSetValue once and
    import whatever single page came back, silently dropping the rest unless
    someone manually re-ran with offset 1, 2, 3. Now we walk the pages.

    Runs in the background (enqueued by `pull_transactions`). Connection-level
    failures are retried per page: the hourly job fans out one request per
    Mpesa Settings doc against a single Safaricom host, so transient connect
    timeouts are expected and shouldn't be treated as a lost pull.
    """
    # A multi-page pull would otherwise fire one alert per page. Collect them
    # and report once, unless a bulk run is already collecting.
    outer_bulk = bool(frappe.flags.get(BULK_PULL_FLAG))
    if not outer_bulk:
        frappe.flags[BULK_PULL_FLAG] = True
        frappe.flags[BULK_PULL_RESULTS_FLAG] = []

    results = frappe.flags.get(BULK_PULL_RESULTS_FLAG)
    first_result = len(results or [])

    page = frappe.utils.cint(payload.get("OffSetValue") or 0)
    last_error = None

    try:
        while page < PULL_MAX_PAGES:
            payload["OffSetValue"] = str(page)
            data = _pull_one_page(mpesa_settings, payload)

            if isinstance(data, Exception):
                last_error = data
                break
            if data is None:
                break

            total_pages = frappe.utils.cint(data.get("TotalPages") or 0)
            page += 1
            if page >= total_pages:
                break
    finally:
        if not outer_bulk:
            frappe.flags[BULK_PULL_FLAG] = False

    if last_error is None:
        if not outer_bulk:
            _publish_pull_summary(mpesa_settings, (results or [])[first_result:])
        return

    _handle_pull_failure(mpesa_settings, last_error)


def _pull_one_page(mpesa_settings: str, payload: dict):
    """One page, with retries. Returns the response, None, or the exception."""
    for attempt in range(PULL_MAX_ATTEMPTS):
        try:
            return process_request(
                endpoint="/pulltransactions/v1/query",
                settings_name=mpesa_settings,
                method="POST",
                payload=payload,
                success_callback=pull_transaction_on_success,
                error_callback=pull_transaction_on_error,
                request_description="Mpesa Pull Transaction",
                doctype=MPESA_SETTINGS_DOCTYPE,
                document_name=mpesa_settings,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            if attempt < PULL_MAX_ATTEMPTS - 1:
                time.sleep(PULL_RETRY_BACKOFF_SECONDS * (2**attempt))
                continue
            return e
        except Exception as e:
            # Not a network blip - don't burn retries on a real bug.
            return e

    return None


def _publish_pull_summary(mpesa_settings: str, page_results: list) -> None:
    """One alert for the whole pull, however many pages it took."""
    if not page_results:
        return

    if len(page_results) == 1:
        publish_pull_result(page_results[0])
        return

    created = sum(r.get("created") or 0 for r in page_results)
    skipped = sum(r.get("skipped") or 0 for r in page_results)
    failed = sum(r.get("failed") or 0 for r in page_results)

    publish_pull_result(
        {
            "status": "success" if created or skipped else "warning",
            "title": "Pull Transaction Complete",
            "message": (
                f"{mpesa_settings}: {created} record(s) imported, {skipped} skipped, "
                f"{failed} failed across {len(page_results)} page(s)."
            ),
            "count": created,
            "settings": mpesa_settings,
            "created": created,
            "skipped": skipped,
            "failed": failed,
        }
    )


def _handle_pull_failure(mpesa_settings: str, error) -> None:
    is_network = isinstance(
        error,
        (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ),
    )

    if is_network:
        frappe.log_error(
            title=f"Mpesa Pull Transaction Error [{mpesa_settings}]"[:140],
            message=f"Gave up after {PULL_MAX_ATTEMPTS} attempts: {error}",
        )
        detail = f"connection failed after {PULL_MAX_ATTEMPTS} attempts"
    else:
        frappe.log_error(title=f"Mpesa Pull Transaction Error [{mpesa_settings}]"[:140], message=str(error))
        detail = "Check Error Logs"

    record_pull_outcome(mpesa_settings, status="Error", message=str(error or detail))

    publish_pull_result(
        {
            "status": "error",
            "title": "Pull Transaction Failed",
            "message": f"{mpesa_settings}: Failed to pull transactions - {detail}.",
            "count": 0,
            "settings": mpesa_settings,
        }
    )


@frappe.whitelist()
def bulk_pull_transactions(
    settings_names: str | list | None = None,
    start_date: str = "",
    end_date: str = "",
    offset: int = 0,
) -> dict:
    """Queue one pull covering many shortcodes, reporting a single summary."""
    require(("System Manager",))

    if isinstance(settings_names, str):
        settings_names = frappe.parse_json(settings_names)

    if not settings_names:
        settings_names = [
            row.name
            for row in frappe.get_all(
                MPESA_SETTINGS_DOCTYPE,
                filters={"enable_hourly_pull_transactions": 1},
                fields=["name"],
            )
        ]

    if not settings_names:
        return {"status": "error", "message": _("No Mpesa Settings selected.")}

    if not start_date or not end_date:
        return {"status": "error", "message": _("Start and end date are required.")}

    frappe.enqueue(
        "mpsa_suite.mpsa_suite.api.m_pesa_api"
        ".execute_bulk_pull_transactions",
        queue="long",
        timeout=3600,
        settings_names=settings_names,
        start_date=start_date,
        end_date=end_date,
        offset=offset,
    )

    return {
        "status": "success",
        "message": _(
            "Pull queued for {0} shortcode(s). One summary will follow."
        ).format(len(settings_names)),
        "count": len(settings_names),
    }


def execute_bulk_pull_transactions(
    settings_names: list, start_date: str, end_date: str, offset: int = 0
) -> None:
    """Pull each shortcode in turn, collecting results into one summary.

    Runs the pulls inline rather than enqueuing one job each, so the per-pull
    toasts can be suppressed via BULK_PULL_FLAG and replaced by a single message.
    """
    frappe.flags[BULK_PULL_FLAG] = True
    frappe.flags[BULK_PULL_RESULTS_FLAG] = []

    skipped_settings = []

    try:
        for name in settings_names:
            try:
                payload = build_pull_payload(name, start_date, end_date, offset)
            except Exception as e:
                skipped_settings.append(f"{name}: {e}")
                continue

            try:
                execute_pull_transactions(mpesa_settings=name, payload=payload)
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Mpesa Bulk Pull Error [{name}]",
                )
            frappe.db.commit()

        results = frappe.flags.get(BULK_PULL_RESULTS_FLAG) or []
    finally:
        frappe.flags[BULK_PULL_FLAG] = False

    imported = sum(r.get("created") or 0 for r in results)
    no_data = [r for r in results if r.get("response_code") == "1001"]
    errored = [r for r in results if r.get("status") == "error"]

    summary_lines = [
        f"Window: {start_date} -> {end_date}",
        f"Shortcodes attempted: {len(settings_names)}",
        f"Records imported: {imported}",
        f"Returned no data (1001): {len(no_data)}",
        f"Errored: {len(errored)}",
        "",
        "No data: " + ", ".join(str(r.get("settings")) for r in no_data),
        "",
        "Errors:",
    ]
    summary_lines += [f"  {r.get('message')}" for r in errored]
    if skipped_settings:
        summary_lines += ["", "Skipped (bad config):"] + [
            f"  {s}" for s in skipped_settings
        ]

    frappe.log_error(
        title="Mpesa Bulk Pull Transactions Complete",
        message="\n".join(summary_lines),
    )

    frappe.publish_realtime(
        event="mpesa_bulk_pull_complete",
        message={
            "status": "success" if not errored else "warning",
            "title": "Bulk Pull Complete",
            "message": (
                f"{imported} record(s) imported across {len(settings_names)} shortcode(s). "
                f"{len(no_data)} returned no data, {len(errored)} errored."
            ),
        },
        user=frappe.session.user,
    )
