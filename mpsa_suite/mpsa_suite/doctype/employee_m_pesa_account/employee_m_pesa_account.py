# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out import party_accounts

ACCOUNT_DOCTYPE = party_accounts.EMPLOYEE_ACCOUNT


class EmployeeMPesaAccount(Document):
    """An employee's M-Pesa phone or bank paybill. Submitting is the approval; edits need cancel + amend."""

    def validate(self):
        if self.docstatus == 0:
            party_accounts.validate_account(self)

    def before_submit(self):
        party_accounts.approve(self)

    def on_submit(self):
        party_accounts.after_approve(self)

    def on_update_after_submit(self):
        if self.is_default and self.has_value_changed("is_default"):
            party_accounts.set_default(self)

    def before_cancel(self):
        self.is_default = 0

    @frappe.whitelist()
    def confirm_receiver_name(self, adopt_name: int = 0):
        """Unblock after a name mismatch, once a manager has checked with the employee."""
        party_accounts.confirm_receiver_name(self, bool(int(adopt_name or 0)))


@frappe.whitelist()
def accounts_for_employee(employee: str) -> list:
    """Approved and draft accounts for the Employee form and the payment schedule, with whether
    each can be paid now."""
    return party_accounts.accounts_for(ACCOUNT_DOCTYPE, employee)
