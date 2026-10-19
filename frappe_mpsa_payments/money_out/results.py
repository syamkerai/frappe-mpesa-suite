"""Matching an asynchronous Daraja Result (or queue timeout) to the request that caused it."""

import frappe


def find_request(doctype: str, result: dict, ref: str | None):
    """Row-lock and return the ``doctype`` document this result belongs to, or None.

    Matched by the IDs from Safaricom's ack (ConversationID, then OriginatorConversationID).
    The ``ref`` query parameter we put on the ResultURL is used only for a request that
    never got an ack (transport timeout), and only if it holds no IDs that disagree.
    """
    result = result or {}
    for field, key in (("conversation_id", "ConversationID"), ("originator_conversation_id", "OriginatorConversationID")):
        value = result.get(key)
        if not value:
            continue
        name = frappe.db.get_value(doctype, {field: value}, "name", order_by="creation desc")
        if name:
            if ref and ref != name:
                frappe.log_error(
                    f"{doctype}: result {key}={value} belongs to {name} but its URL names {ref}",
                    "M-Pesa result mismatch",
                )
                return None
            frappe.db.get_value(doctype, name, "name", for_update=True)
            return frappe.get_doc(doctype, name)

    if not ref or not frappe.db.get_value(doctype, ref, "name", for_update=True):
        return None
    doc = frappe.get_doc(doctype, ref)
    known = {doc.get("conversation_id"), doc.get("originator_conversation_id")} - {None, ""}
    sent = {result.get("ConversationID"), result.get("OriginatorConversationID")} - {None, ""}
    if known and sent and not (known & sent):
        frappe.log_error(
            f"{doctype} {ref}: result IDs {sorted(sent)} do not match the ack {sorted(known)}",
            "M-Pesa result mismatch",
        )
        return None
    return doc


def first_value(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None
