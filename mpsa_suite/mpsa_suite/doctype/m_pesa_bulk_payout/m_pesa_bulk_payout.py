# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from ....money_out import bulk_payout


class MPesaBulkPayout(Document):
    """Casual wages and overtime paid by M-Pesa B2C in bulk (money_out.bulk_payout): a preparer adds
    the rows, a named M-Pesa Manager or Accounts Manager submits (approves) it, Pay by M-Pesa makes
    the draft payout an M-Pesa Payer sends, and one Journal Entry books the paid rows."""

    def validate(self):
        bulk_payout.validate_sheet(self)

    def before_submit(self):
        bulk_payout.before_submit(self)

    def before_update_after_submit(self):
        bulk_payout.keep_results(self)

    def before_cancel(self):
        bulk_payout.before_cancel(self)

    def on_cancel(self):
        bulk_payout.on_cancel(self)
