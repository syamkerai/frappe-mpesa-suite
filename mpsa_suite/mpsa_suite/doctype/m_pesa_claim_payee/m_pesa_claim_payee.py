# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

ON_ITS_OWN = "Change the payees of an Expense Claim on the claim."


class MPesaClaimPayee(Document):
    """One merchant or casual payee an Expense Claim pays by M-Pesa (money_out.claim_payees). Its
    checks run when the claim is saved and submitted; its status and result are written by the
    server from its payout. Frappe runs a row's own validate and on_trash only when the row is
    saved or deleted by itself (the REST API, frappe.client.save), which would skip those checks,
    so that is refused. Saving or deleting the claim never calls them."""

    def validate(self):
        frappe.throw(ON_ITS_OWN)

    def on_trash(self):
        frappe.throw(ON_ITS_OWN)
