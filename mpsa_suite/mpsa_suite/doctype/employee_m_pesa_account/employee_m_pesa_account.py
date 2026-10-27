# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out import party_accounts

ACCOUNT_DOCTYPE = party_accounts.EMPLOYEE_ACCOUNT


class EmployeeMPesaAccount(Document):
    """An employee's M-Pesa phone, bank account, paybill or till. Submitting is the approval; edits
    need cancel + amend; the default changes with Make Default."""

    def onload(self):
        self.set_onload("mpsa", party_accounts.form_state(self))

    def validate(self):
        if self.docstatus == 0:
            party_accounts.validate_account(self)

    def before_submit(self):
        # validate() above runs only on drafts: the details as submitted are cleaned and checked
        # again, and a change made in this submit makes its author unable to approve it
        party_accounts.validate_account(self)
        party_accounts.approve(self)

    def on_submit(self):
        party_accounts.after_approve(self)

    def before_update_after_submit(self):
        party_accounts.refuse_default_edit(self)

    def before_cancel(self):
        self.is_default = 0

    def on_cancel(self):
        party_accounts.after_cancel(self)

    @frappe.whitelist()
    def confirm_receiver_name(self, adopt_name: int = 0):
        """Unblock after a name mismatch, once a manager has checked with the employee."""
        party_accounts.confirm_receiver_name(self, bool(int(adopt_name or 0)))

    @frappe.whitelist()
    def make_default(self):
        """Make this approved account the employee's default (where salaries go)."""
        party_accounts.make_default(self)


@frappe.whitelist()
def accounts_for_employee(employee: str) -> list:
    """Approved and draft accounts for the Employee form and the payment schedule, with whether
    each can be paid now."""
    return party_accounts.accounts_for(ACCOUNT_DOCTYPE, employee)
