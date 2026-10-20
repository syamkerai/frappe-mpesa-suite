"""Transaction Status queries for B2C payouts (docs/daraja_api_reference.md §11).

A payout whose Result never arrived (transport timeout, queue timeout, lost callback)
must not be re-sent blindly: the money may already have moved. ``check_b2c_status``
asks Safaricom; the result settles the payout only when it names a final state that
matches our amount, and only while the payout is still unsettled.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from ..daraja import apis
from ..daraja import money_out as daraja
from ..utils.doctype_names import B2C_REQUEST_DOCTYPE
from .common import callback_url, initiator_settings, log_callback, post
from .results import find_request

QUERY_DOCTYPE = "Mpesa B2C Status Query"
# "Status Unknown": the send hit a transport error, so the payout may or may not exist at Safaricom.
UNSETTLED = ("Not Initiated", "Initiated", "Status Unknown", "", None)
# Don't let people hammer Daraja: one open query per payout within this window.
QUERY_COOLDOWN_MINUTES = 2


def start_query(b2c_request: str) -> str:
    """Create and send a status query for ``b2c_request``; returns the query's name."""
    b2c = frappe.db.get_value(
        B2C_REQUEST_DOCTYPE,
        b2c_request,
        ["name", "docstatus", "status", "mpesa_settings", "transaction_id", "originator_conversation_id"],
        as_dict=True,
    )
    if not b2c:
        frappe.throw(f"B2C request {b2c_request} not found")
    if b2c.docstatus != 1:
        frappe.throw("Only submitted B2C requests can be checked")
    if not b2c.mpesa_settings:
        frappe.throw("Transaction Status is an M-Pesa API; this request was not sent through M-Pesa")
    if b2c.status not in UNSETTLED:
        frappe.throw(f"B2C request {b2c_request} is already {b2c.status}")
    if not (b2c.transaction_id or b2c.originator_conversation_id):
        frappe.throw("Nothing to query by: the request has no receipt and no OriginatorConversationID")

    recent = frappe.db.get_value(
        QUERY_DOCTYPE,
        {
            "b2c_request": b2c_request,
            "status": "Sent",
            "creation": [">", add_to_date(now_datetime(), minutes=-QUERY_COOLDOWN_MINUTES)],
        },
        "name",
        order_by="creation desc",
    )
    if recent:
        frappe.throw(f"Status query {recent} is still waiting for Safaricom's answer")

    settings = initiator_settings(b2c.mpesa_settings)
    query = frappe.get_doc(
        {
            "doctype": QUERY_DOCTYPE,
            "b2c_request": b2c_request,
            "mpesa_settings": b2c.mpesa_settings,
            "query_by": "TransactionID" if b2c.transaction_id else "OriginalConversationID",
            "query_value": b2c.transaction_id or b2c.originator_conversation_id,
            "status": "Queued",
        }
    ).insert(ignore_permissions=True)

    payload = daraja.transaction_status_payload(
        initiator=settings.initiator_name,
        security_credential=settings.security_credential,
        party_a=apis.initiator_party_a(settings),
        transaction_id=b2c.transaction_id or None,
        original_conversation_id=None if b2c.transaction_id else b2c.originator_conversation_id,
        result_url=callback_url("b2c_status_result", ref=query.name),
        queue_timeout_url=callback_url("status_queue_timeout", ref=query.name),
        remarks=f"Status {b2c_request}",
    )
    data, error = post(
        settings,
        daraja.TRANSACTION_STATUS_ENDPOINT,
        payload,
        doctype=QUERY_DOCTYPE,
        name=query.name,
        description="Mpesa B2C Transaction Status",
    )
    if error:
        query.db_set({"status": "Failed to Send", "outcome_reason": f"Transport error ({error})"})
    elif daraja.ack_accepted(data):
        query.db_set(
            {
                "status": "Sent",
                "conversation_id": data.get("ConversationID"),
                "originator_conversation_id": data.get("OriginatorConversationID"),
            }
        )
    else:
        query.db_set(
            {
                "status": "Failed to Send",
                "outcome_reason": (
                    f"{data.get('errorCode') or data.get('ResponseCode') or ''} "
                    f"{data.get('errorMessage') or data.get('ResponseDescription') or ''}"
                ).strip()[:500],
            }
        )
    return query.name


def handle_result(result: dict, ref: str | None) -> None:
    query = find_request(QUERY_DOCTYPE, result, ref)
    if not query:
        log_callback({"Result": result}, "Mpesa B2C Status Result (unmatched)", QUERY_DOCTYPE, ref, "Failed")
        return
    if query.status == "Result Received":
        return  # re-sent result

    values = daraja.result_values(result)
    # Lock the payout before reading it, so a real B2C Result arriving now is serialised.
    frappe.db.get_value(B2C_REQUEST_DOCTYPE, query.b2c_request, "name", for_update=True)
    b2c = frappe.get_doc(B2C_REQUEST_DOCTYPE, query.b2c_request)

    outcome, reason = daraja.b2c_status_outcome(result, expected_amount=b2c.amount)
    original_id = values.get("OriginatorConversationID")
    if (
        outcome != "Unresolved"
        and query.query_by == "OriginalConversationID"
        and original_id
        and original_id != b2c.originator_conversation_id
    ):
        outcome, reason = "Unresolved", f"Result is for {original_id}, not {b2c.originator_conversation_id}"

    query.db_set(
        {
            "status": "Result Received",
            "result_code": daraja.result_code(result),
            "result_desc": result.get("ResultDesc"),
            "transaction_status": values.get("TransactionStatus"),
            "receipt_no": values.get("ReceiptNo"),
            "amount": daraja.to_number(values.get("Amount")),
            "finalised_time": daraja.parse_daraja_datetime(values.get("FinalisedTime")),
            "debit_party_name": values.get("DebitPartyName"),
            "credit_party_name": values.get("CreditPartyName"),
            "outcome": outcome,
            "outcome_reason": reason[:500],
        },
        notify=True,
    )
    log_callback({"Result": result}, "Mpesa B2C Status Result", QUERY_DOCTYPE, query.name, "Completed")

    if outcome == "Unresolved":
        return
    if b2c.status not in UNSETTLED or b2c.result_code not in (None, ""):
        # The real Result callback got there first; it is the authority.
        query.db_set("outcome_reason", f"{reason}. Payout already {b2c.status}; left unchanged."[:500])
        return

    apply_to_payout(b2c, query, outcome, values)


def apply_to_payout(b2c, query, outcome: str, values: dict) -> None:
    """Settle the payout from a status query, exactly as its own Result callback would."""
    if outcome == "Paid":
        update = {
            "status": "Paid",
            # A non-empty result_code makes a late B2C Result callback a no-op (see
            # MpesaB2CConnector.handle_callback), so the payout cannot be booked twice.
            "result_code": "0",
            "result_desc": f"Confirmed by Transaction Status query {query.name}",
            "transaction_id": values.get("ReceiptNo"),
            "transaction_completed_datetime": daraja.parse_daraja_datetime(values.get("FinalisedTime"))
            or now_datetime(),
        }
        if not b2c.conversation_id and values.get("ConversationID"):
            update["conversation_id"] = values.get("ConversationID")
    else:
        update = {
            "status": "Failed",
            "result_code": f"TS:{values.get('TransactionStatus') or 'Failed'}"[:140],
            "result_desc": f"Transaction Status query {query.name}: {values.get('TransactionStatus')}",
        }
    b2c.db_set(update, notify=True)
    query.db_set("applied", 1)
    b2c.add_comment("Comment", f"Settled as {outcome} by Transaction Status query {query.name}.")

    # Same downstream work as a real Result: update the disbursement row, book on Paid.
    frappe.enqueue(
        "frappe_mpsa_payments.services.b2c_response_service.update_b2c_reference_status",
        queue="short",
        timeout=300,
        enqueue_after_commit=True,
        b2c_request_name=b2c.name,
        enqueue_next=True,
    )


def handle_queue_timeout(payload: dict, ref: str | None) -> None:
    if ref and frappe.db.exists(QUERY_DOCTYPE, ref):
        frappe.db.set_value(QUERY_DOCTYPE, ref, "status", "Timed Out")
    log_callback(payload, "Mpesa B2C Status Queue Timeout", QUERY_DOCTYPE, ref, "Failed")
