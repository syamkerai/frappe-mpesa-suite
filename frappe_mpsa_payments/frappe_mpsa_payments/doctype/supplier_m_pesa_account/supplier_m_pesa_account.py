# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out import supplier_accounts


class SupplierMPesaAccount(Document):
    """A supplier's paybill / till. Submitting is the approval; edits need cancel + amend."""

    def validate(self):
        if self.docstatus == 0:
            supplier_accounts.validate_account(self)

    def before_submit(self):
        supplier_accounts.approve(self)

    def on_submit(self):
        supplier_accounts.after_approve(self)

    def on_update_after_submit(self):
        if self.is_default and self.has_value_changed("is_default"):
            supplier_accounts.set_default(self)

    def before_cancel(self):
        self.is_default = 0

    @frappe.whitelist()
    def confirm_receiver_name(self, adopt_name: int = 0):
        """Unblock after a name mismatch, once a manager has checked with the supplier."""
        supplier_accounts.confirm_receiver_name(self, bool(int(adopt_name or 0)))


@frappe.whitelist()
def accounts_for_supplier(supplier: str) -> list:
    """Approved accounts for the Supplier form, with whether each can be paid now."""
    frappe.has_permission("Supplier M-Pesa Account", "read", throw=True)
    rows = frappe.get_list(
        "Supplier M-Pesa Account",
        filters={"supplier": supplier, "docstatus": ["<", 2]},
        fields=supplier_accounts.ACCOUNT_FIELDS + ["name_mismatch"],
        order_by="is_default desc, creation asc",
    )
    for row in rows:
        row.problem = supplier_accounts.usable_problem(row, supplier)
    return rows
