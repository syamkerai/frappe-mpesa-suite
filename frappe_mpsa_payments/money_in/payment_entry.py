"""Payment Entry / Journal Entry controls for M-Pesa money.

One ``before_insert`` runs the steps in a fixed order. As separate Server Scripts they ran
alphabetically, so the duplicate guard could compare the register name instead of the
receipt and let a duplicate through.
"""

import frappe
from frappe import _
from frappe.utils import cstr

from .common import REGISTER, enabled, mpesa_gl_accounts, named_lock


def _touches_mpesa(doc) -> bool:
    gls = mpesa_gl_accounts()
    return bool(gls & {doc.get("paid_from"), doc.get("paid_to")})


def before_insert(doc, method=None) -> None:
    if enabled("clear_clearance_on_amend") and doc.get("amended_from") and doc.get("clearance_date"):
        doc.clearance_date = None

    if enabled("receipt_as_reference"):
        receipt_reference(doc)

    if enabled("block_duplicate_payment_entries"):
        block_duplicate_receipt(doc)


def receipt_reference(doc) -> None:
    """Register name (MPC2B-…) -> real M-Pesa receipt, keeping the register in the remarks."""
    ref = cstr(doc.get("reference_no")).strip()
    if not ref or not frappe.db.exists(REGISTER, ref):
        return
    receipt = frappe.db.get_value(REGISTER, ref, "transid")
    if receipt:
        doc.reference_no = receipt
        note = f"M-Pesa register: {ref}"
        if note not in cstr(doc.get("remarks")):
            doc.remarks = f"{doc.remarks}\n{note}" if doc.get("remarks") else note


def block_duplicate_receipt(doc) -> None:
    """One live Payment Entry per receipt per M-Pesa account (Safaricom re-sends callbacks)."""
    ref = cstr(doc.get("reference_no")).strip()
    if not ref or not _touches_mpesa(doc):
        return
    # Same key as the gateway's direct booking, held until commit, so two workers booking
    # one receipt cannot both pass the check. The read is a locking read on the indexed
    # reference_no so it sees an entry committed while we waited for the lock.
    with named_lock(f"book:{ref}"):
        for account in {doc.get("paid_from"), doc.get("paid_to")} & mpesa_gl_accounts():
            existing = frappe.db.sql(
                """
                SELECT name FROM `tabPayment Entry`
                WHERE reference_no = %s AND docstatus < 2 AND name != %s
                  AND (paid_from = %s OR paid_to = %s)
                LIMIT 1
                FOR UPDATE
                """,
                (ref, doc.name or "", account, account),
            )
            if existing:
                frappe.throw(
                    _("M-Pesa receipt {0} is already recorded on Payment Entry {1}.").format(ref, existing[0][0]),
                    title=_("Duplicate M-Pesa Receipt"),
                )


def before_validate(doc, method=None) -> None:
    """Payment Request -> Payment Entry never sets a cost center; sites that make it
    mandatory then fail every STK callback."""
    if doc.get("cost_center") or not enabled("default_cost_center") or not _touches_mpesa(doc):
        return
    company = doc.company
    default = frappe.get_cached_value("Company", company, "cost_center")
    if default and not frappe.get_cached_value("Cost Center", default, "is_group"):
        doc.cost_center = default
        return
    doc.cost_center = frappe.db.get_value(
        "Cost Center", {"company": company, "is_group": 0, "disabled": 0}, "name", order_by="lft asc"
    )


def journal_before_insert(doc, method=None) -> None:
    if enabled("clear_clearance_on_amend") and doc.get("amended_from") and doc.get("clearance_date"):
        doc.clearance_date = None
