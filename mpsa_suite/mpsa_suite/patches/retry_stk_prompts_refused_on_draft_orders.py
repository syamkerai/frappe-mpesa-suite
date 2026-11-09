# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Give paid STK prompts back the booking attempts ERPNext refused on a draft Sales Order.

Before this release a prompt paid on a draft (prepaid) order tried to book three times, was
refused ("Sales Order ... must be submitted") and stopped for good, even after the order was
approved; a prompt whose Payment Request a re-prompt cancelled while the customer paid failed the
same way ("Cannot link cancelled document"). Those were never real attempts: they are reset, so
money_in.sales_order books the prompt (releasing a still-draft order first) and stk_verify books
the superseded one on the request that replaced it.

Only Completed, confirmed, unbooked prompts on a Sales Order's Payment Request whose latest booking
failure (Error Log "M-Pesa STK booking failed for <prompt>") says one of those two things, and
whose receipt no submitted Payment Entry carries. Idempotent: a reset prompt has no attempts left
to reset until it fails again.
"""

import frappe
from frappe.utils import cstr

from mpsa_suite.handover import ran_under_old_name

EXPRESS = "Mpesa Express Request"
REFUSALS = ("must be submitted", "Cannot link cancelled document")


def refused_on_draft(prompt: str) -> bool:
    """Its latest booking failure is one of the two refusals (an older, other failure does not count)."""
    latest = frappe.get_all(
        "Error Log", filters={"method": f"M-Pesa STK booking failed for {prompt}"[:140]}, pluck="error",
        order_by="creation desc", limit=1,
    )
    return bool(latest) and any(r in cstr(latest[0]) for r in REFUSALS)


def execute():
    if ran_under_old_name(__name__):
        return  # R8: the patch convention; never registered under the old name
    if not frappe.db.has_column(EXPRESS, "booking_attempts"):
        return
    filters = {"docstatus": 1, "status": "Completed", "is_reconciled": 0, "reference_doctype": "Payment Request",
               "transaction_id": ["is", "set"], "booking_attempts": [">", 0]}
    if frappe.db.has_column(EXPRESS, "verification_status"):
        filters["verification_status"] = "Verified"
    for row in frappe.get_all(EXPRESS, filters=filters, fields=["name", "reference_name", "transaction_id"]):
        if frappe.db.get_value("Payment Request", row.reference_name, "reference_doctype") != "Sales Order":
            continue
        if frappe.db.exists("Payment Entry", {"reference_no": row.transaction_id, "docstatus": 1}):
            continue  # booked another way
        if refused_on_draft(row.name):
            frappe.db.set_value(EXPRESS, row.name, "booking_attempts", 0, update_modified=False)
