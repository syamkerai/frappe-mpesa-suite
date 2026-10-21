"""Matching an asynchronous Daraja Result (or queue timeout) to the request that caused it."""

import frappe

from ..daraja.guard import daraja_id

# The only states in which a request may have reached Safaricom while its ack never reached
# us, so a Result can arrive for a document that holds no ack IDs: B2B / Reversal "Unknown"
# (common.UNKNOWN), and a status query whose send hit a transport error after the
# connection opened (b2c_status.start_query writes this reason).
SENT_WITHOUT_ACK_STATUS = "Unknown"
QUERY_SENT_WITHOUT_ACK = ("Failed to Send", "Transport error (unknown)")


def _sent_without_ack(doc) -> bool:
    status = doc.get("status")
    if status == SENT_WITHOUT_ACK_STATUS:
        return True
    return (status, doc.get("outcome_reason")) == QUERY_SENT_WITHOUT_ACK


def find_request(doctype: str, result: dict, ref: str | None):
    """Row-lock and return the ``doctype`` document this result belongs to, or None.

    Matched by the IDs from Safaricom's ack (ConversationID, then OriginatorConversationID).
    The ``ref`` query parameter we put on the ResultURL is used only for a request that
    may have been sent but never got an ack (transport timeout), and only for a result that
    carries IDs (Safaricom's always do). A request that got an ack is matched by its IDs only.
    The body is unauthenticated, so only Daraja-shaped IDs reach a filter: Frappe reads a list
    such as ``["is", "set"]`` as an operator, which would match any acked request.
    """
    result = result or {}
    for field, key in (("conversation_id", "ConversationID"), ("originator_conversation_id", "OriginatorConversationID")):
        value = daraja_id(result.get(key))
        if not value:
            continue
        name = frappe.db.get_value(doctype, {field: value}, "name", order_by="creation desc")
        if name:
            if ref and ref != name:
                frappe.log_error(
                    title="M-Pesa result mismatch",
                    message=f"{doctype}: result {key}={value} belongs to {name} but its URL names {ref}",
                )
                return None
            frappe.db.get_value(doctype, name, "name", for_update=True)
            return frappe.get_doc(doctype, name)

    if not ref or not frappe.db.get_value(doctype, ref, "name", for_update=True):
        return None
    doc = frappe.get_doc(doctype, ref)
    known = {doc.get("conversation_id"), doc.get("originator_conversation_id")} - {None, ""}
    sent = {daraja_id(result.get(k)) for k in ("ConversationID", "OriginatorConversationID")} - {""}
    if known or not sent or not _sent_without_ack(doc):
        frappe.log_error(
            title="M-Pesa result mismatch",
            message=f"{doctype} {ref}: result IDs {sorted(sent)} do not match the ack {sorted(known)} "
            f"(status {doc.get('status')})",
        )
        return None
    return doc


def first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None
