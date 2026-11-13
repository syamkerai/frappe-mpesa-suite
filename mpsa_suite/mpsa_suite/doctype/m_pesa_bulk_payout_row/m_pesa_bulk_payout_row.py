# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

ON_ITS_OWN = "Change a bulk payout's rows on their sheet."


class MPesaBulkPayoutRow(Document):
    """One payee of an M-Pesa Bulk Payout. Its checks run when the sheet is saved
    (money_out.bulk_payout.validate_sheet). Frappe runs a row's own validate and on_trash only when
    the row is saved or deleted by itself (the REST API), which would skip them: that is refused.
    Saving or deleting the sheet never calls them."""

    def validate(self):
        frappe.throw(ON_ITS_OWN)

    def on_trash(self):
        frappe.throw(ON_ITS_OWN)
