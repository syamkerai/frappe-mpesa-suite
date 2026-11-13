# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from ....money_out import petty_cash


class MPesaPettyCashPhone(Document):
    """A company M-Pesa phone that pays supplier tills (money_out.petty_cash). Submitting is the
    approval (Approve, by a named M-Pesa Manager or Accounts Manager who did not enter it and does not
    hold it); its number, account and expected name then change only by cancel and amend."""

    def validate(self):
        if self.docstatus == 0:
            petty_cash.validate_phone(self)
            petty_cash.note_who_entered(self)

    def before_submit(self):
        petty_cash.check_approval(self)

    def before_update_after_submit(self):
        petty_cash.check_update(self)

    def before_cancel(self):
        petty_cash.before_cancel(self)
