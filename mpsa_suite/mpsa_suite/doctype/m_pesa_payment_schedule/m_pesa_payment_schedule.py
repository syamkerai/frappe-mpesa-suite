# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from ....money_out import schedule


class MPesaPaymentSchedule(Document):
    """B2B payments planned ahead. Accounts prepare the lines; a Payer pays them on their day
    from the M-Pesa Payments Due report (money_out.schedule)."""

    def validate(self):
        schedule.validate_schedule(self)

    def on_trash(self):
        schedule.check_delete(self)
