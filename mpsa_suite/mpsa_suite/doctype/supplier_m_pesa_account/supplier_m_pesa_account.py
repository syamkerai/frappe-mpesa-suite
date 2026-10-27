# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out import supplier_accounts


class SupplierMPesaAccount(Document):
    """A supplier's paybill, till, bank account or phone. Submitting is the approval; edits need
    cancel + amend; the default changes with Make Default."""

    def onload(self):
        self.set_onload("mpsa", supplier_accounts.form_state(self))

    def validate(self):
        if self.docstatus == 0:
            supplier_accounts.validate_account(self)

    def before_submit(self):
        # validate() above runs only on drafts: the details as submitted are cleaned and checked
        # again, and a change made in this submit makes its author unable to approve it
        supplier_accounts.validate_account(self)
        supplier_accounts.approve(self)

    def on_submit(self):
        supplier_accounts.after_approve(self)

    def before_update_after_submit(self):
        supplier_accounts.refuse_default_edit(self)

    def before_cancel(self):
        supplier_accounts.before_cancel(self)

    def on_cancel(self):
        supplier_accounts.after_cancel(self)

    @frappe.whitelist()
    def confirm_receiver_name(self, adopt_name: int = 0):
        """Unblock after a name mismatch, once a manager has checked with the supplier."""
        supplier_accounts.confirm_receiver_name(self, bool(int(adopt_name or 0)))

    @frappe.whitelist()
    def make_default(self):
        """Make this approved account the supplier's default (where payments that name no account go)."""
        supplier_accounts.make_default(self)


@frappe.whitelist()
def accounts_for_supplier(supplier: str) -> list:
    """Approved and draft accounts for the Supplier form and the payment schedule, with whether
    each can be paid now."""
    return supplier_accounts.accounts_for(supplier)
