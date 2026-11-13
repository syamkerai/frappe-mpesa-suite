"""Transaction Status queries for B2C payouts (docs/daraja_api_reference.md §11).

A payout whose Result never arrived (transport timeout, queue timeout, lost callback)
must not be re-sent blindly: the money may already have moved. ``check_b2c_status``
asks Safaricom; the result settles the payout only when it names a final state that
matches our amount, and only while the payout is still unsettled.

A query for a B2B payout (``b2b_request``) goes through the same doctype and endpoints;
its result is handled by money_out.b2b_status.

Safaricom has answered 0.4 s after the ack, before the sender recorded the ack's IDs: such a
result is kept (an Integration Request, Queued) and tried again every two minutes once the ack is
recorded (``retry_waiting_results``); after ACK_WAIT_MINUTES it is rejected like any result that
answers no query of ours.
"""

import json

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from ..daraja import apis
from ..daraja import money_out as daraja
from ..daraja.guard import daraja_id
from ..utils.doctype_names import B2C_REQUEST_DOCTYPE
from .common import callback_url, initiator_settings, log_callback, post
from .results import find_request

QUERY_DOCTYPE = "Mpesa B2C Status Query"
# "Status Unknown": the send hit a transport error, so the payout may or may not exist at Safaricom.
UNSETTLED = ("Not Initiated", "Initiated", "Status Unknown", "", None)
# Don't let people hammer Daraja: one open query per payout within this window.
QUERY_COOLDOWN_MINUTES = 2
# How long a result waits for its query's ack (as m_pesa_api.STATUS_WAIT_MINUTES), and how it is kept.
ACK_WAIT_MINUTES = 10
WAITING = "Mpesa Status Result (waiting for its ack)"


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

    return send_query(
        {"b2c_request": b2c_request},
        b2c.mpesa_settings,
        b2c.transaction_id,
        b2c.originator_conversation_id,
        remarks=f"Status {b2c_request}",
        description="Mpesa B2C Transaction Status",
    )


def send_query(link: dict, mpesa_settings: str, receipt, originator_id, remarks: str, description: str) -> str:
    """Create the query (``link``: the payout it asks about), send it and record the ack; returns
    the query's name. Queried by receipt when there is one, else by OriginatorConversationID.

    Committed before the call and again right after the ack: Safaricom has answered 0.4 s after
    the ack, and its result finds the query by the ack's ConversationID (results.find_request).
    """
    settings = initiator_settings(mpesa_settings)
    query = frappe.get_doc(
        {
            "doctype": QUERY_DOCTYPE,
            **link,
            "mpesa_settings": mpesa_settings,
            "query_by": "TransactionID" if receipt else "OriginalConversationID",
            "query_value": receipt or originator_id,
            "status": "Queued",
        }
    ).insert(ignore_permissions=True)

    payload = daraja.transaction_status_payload(
        initiator=settings.initiator_name,
        security_credential=settings.security_credential,
        party_a=apis.initiator_party_a(settings),
        transaction_id=receipt or None,
        original_conversation_id=None if receipt else originator_id,
        result_url=callback_url("b2c_status_result", ref=query.name),
        queue_timeout_url=callback_url("status_queue_timeout", ref=query.name),
        remarks=remarks,
    )
    frappe.db.commit()  # the query exists before Safaricom can answer it
    data, error = post(
        settings,
        daraja.TRANSACTION_STATUS_ENDPOINT,
        payload,
        doctype=QUERY_DOCTYPE,
        name=query.name,
        description=description,
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
    frappe.db.commit()  # the ack's IDs, before the result looks for them
    return query.name


def handle_result(result: dict, ref: str | None, final: bool = False) -> None:
    """A query's result (the callback, or ``retry_waiting_results`` with ``final``: no more waiting)."""
    if not final and awaiting_ack(result, ref):
        log_callback({"Result": result}, WAITING, QUERY_DOCTYPE, ref, "Queued")
        return
    query = find_request(QUERY_DOCTYPE, result, ref)
    if not query:
        log_callback({"Result": result}, "Mpesa B2C Status Result (unmatched)", QUERY_DOCTYPE, ref, "Failed")
        return
    if query.status == "Result Received":
        return  # re-sent result
    if query.get("b2b_request"):
        from . import b2b_status

        b2b_status.handle_result(query, result)
        return

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
    """Settle the payout from a status query, exactly as its own Result callback would. Round 4.5
    review: a Paid answer's CreditPartyName (docs/daraja_api_reference.md §11: whom M-Pesa paid,
    "2547... - NAME") fills an empty ``receiver_public_name``, and the name is kept in the M-Pesa
    Known Name list as the Result's would be (known_names.record_b2c: never raises but a deadlock)."""
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
        credited = str(values.get("CreditPartyName") or "").strip()
        if credited and not str(b2c.get("receiver_public_name") or "").strip():
            update["receiver_public_name"] = credited[:140]
    else:
        update = {
            "status": "Failed",
            "result_code": f"TS:{values.get('TransactionStatus') or 'Failed'}"[:140],
            "result_desc": f"Transaction Status query {query.name}: {values.get('TransactionStatus')}",
        }
    b2c.db_set(update, notify=True)
    query.db_set("applied", 1)
    b2c.add_comment("Comment", f"Settled as {outcome} by Transaction Status query {query.name}.")
    if outcome == "Paid":
        from . import known_names

        known_names.record_b2c(b2c)  # the name M-Pesa paid the phone under (none kept when it names no one)

    # Same downstream work as a real Result: update the disbursement row, book on Paid.
    frappe.enqueue(
        "mpsa_suite.services.b2c_response_service.update_b2c_reference_status",
        queue="short",
        timeout=300,
        enqueue_after_commit=True,
        b2c_request_name=b2c.name,
        enqueue_next=True,
    )


def awaiting_ack(result: dict, ref: str | None) -> bool:
    """The result's URL names a query of ours (``ref``) whose ack the sender has not recorded yet:
    still Queued, with no IDs. A locking read, so an ack being written now is waited for."""
    result = result or {}
    if not ref or not any(daraja_id(result.get(k)) for k in ("ConversationID", "OriginatorConversationID")):
        return False
    row = frappe.db.get_value(QUERY_DOCTYPE, ref, ["status", "conversation_id", "originator_conversation_id"],
                              as_dict=True, for_update=True)
    return bool(row) and row.status == "Queued" and not (row.conversation_id or row.originator_conversation_id)


def retry_waiting_results(limit: int = 50) -> None:
    """Every two minutes: results kept while their query's ack was on its way are handled once the
    ack is recorded, and for the last time after ACK_WAIT_MINUTES (rejected if it never came)."""
    now = now_datetime()
    rows = frappe.get_all(
        "Integration Request",
        filters={"request_description": WAITING, "status": "Queued", "creation": [">=", add_to_date(now, hours=-24)]},
        fields=["name", "creation"],
        order_by="creation asc",
        limit=limit,
    )
    for row in rows:
        # a locking read: one result is never handled twice
        kept = frappe.db.get_value("Integration Request", row.name, ["status", "data", "reference_docname"],
                                   as_dict=True, for_update=True)
        if not kept or kept.status != "Queued":
            continue
        data = json.loads(kept.data) if isinstance(kept.data, str) else (kept.data or {})
        result = data.get("Result") or {}
        final = get_datetime(row.creation) <= add_to_date(now, minutes=-ACK_WAIT_MINUTES)
        if not final and awaiting_ack(result, kept.reference_docname):
            continue  # its ack is still on its way
        try:
            frappe.db.set_value("Integration Request", row.name, "status", "Completed", update_modified=False)
            handle_result(result, kept.reference_docname, final=True)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            # kept, with the result, to be replayed by hand
            frappe.db.set_value("Integration Request", row.name, {"status": "Failed", "error": frappe.get_traceback()},
                                update_modified=False)
            frappe.db.commit()
            frappe.log_error(title=f"M-Pesa status result {row.name} not recorded"[:140], message=frappe.get_traceback())


def handle_queue_timeout(payload: dict, ref: str | None) -> None:
    if ref and frappe.db.exists(QUERY_DOCTYPE, ref):
        frappe.db.set_value(QUERY_DOCTYPE, ref, "status", "Timed Out")
    log_callback(payload, "Mpesa B2C Status Queue Timeout", QUERY_DOCTYPE, ref, "Failed")
