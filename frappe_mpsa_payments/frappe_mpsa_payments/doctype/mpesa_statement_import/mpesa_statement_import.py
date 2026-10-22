# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MpesaStatementImport(Document):
    """One M-PESA Org Portal account statement, imported as statement lines (wallet.statement_import)."""

    def validate(self):
        if self.has_value_changed("statement_file") and not self.is_new():
            self.status = "Not Imported"

    @frappe.whitelist()
    def import_statement(self):
        self.check_permission("write")
        if self.status in ("Queued", "Importing"):
            frappe.throw(_("This statement is already being imported"))
        from ....wallet.statement_import import start

        start(self.name)
        return _("Import queued: the counts below fill in when it is done.")
