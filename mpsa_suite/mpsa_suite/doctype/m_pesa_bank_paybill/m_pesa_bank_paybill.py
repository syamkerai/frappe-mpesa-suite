# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document
from frappe.utils import cstr

from ....money_out import bank_paybills


class MPesaBankPaybill(Document):
    """A bank's M-Pesa paybill that payee bank accounts are paid through (money_out.bank_paybills)."""

    def before_insert(self):
        self.code = cstr(self.code).strip().upper()  # the record's name

    def validate(self):
        bank_paybills.validate_row(self)

    def on_update(self):
        bank_paybills.after_change(self)

    def on_trash(self):
        bank_paybills.check_delete(self)
