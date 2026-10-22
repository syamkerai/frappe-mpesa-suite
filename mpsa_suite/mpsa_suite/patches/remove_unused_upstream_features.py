# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Remove upstream features Astral never used (checked on the live site, 2026-10-19).

Stanbic payouts (no Stanbic Settings, no Stanbic payouts), B2C payouts against Loans (none),
the POS Invoice / POS page STK flow (no POS payments by M-Pesa) and the Mpesa Payment
Reconciliation tool (never installed). Their code is gone; this drops their DocTypes, the
custom fields that only served them, and the POS Settings fields upstream added.

Kept on purpose: POS Invoice ``mpesa_receipt_number`` (it holds data on some sites).
"""

import frappe

from mpsa_suite.handover import ran_under_old_name

DOCTYPES = (
    "Mpesa Payment Reconciliation",
    "Mpesa Draft Payments",
    "Mpesa Payments Invoices",
    "Stanbic Settings",
    "Stanbic Sort Code",
)
CUSTOM_FIELDS = (
    "Bank-pesa_link_bank_code",
    "Loan Disbursement-b2c_payment_disbursement",
    "POS Invoice-request_for_payment",
)
POS_FIELDS = ("request_for_payment",)
MODULE = "Stanbic"


def execute():
    if ran_under_old_name(__name__):
        return  # R8: already ran as frappe_mpsa_payments.*; see handover.py
    for doctype in DOCTYPES:
        if frappe.db.exists("DocType", doctype):
            frappe.delete_doc("DocType", doctype, force=True, ignore_missing=True)
        # custom fields upstream added on these DocTypes
        frappe.db.delete("Custom Field", {"dt": doctype})

    for name in CUSTOM_FIELDS:
        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc("Custom Field", name, force=True, ignore_missing=True)

    if frappe.db.exists("DocType", "POS Field"):
        frappe.db.delete("POS Field", {"parent": "POS Settings", "fieldname": ["in", POS_FIELDS]})

    if frappe.db.exists("Module Def", MODULE) and not frappe.db.exists("DocType", {"module": MODULE}):
        frappe.delete_doc("Module Def", MODULE, force=True, ignore_missing=True)
