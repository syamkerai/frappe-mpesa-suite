# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MPesaPaymentScheduleLine(Document):
    """One planned payment. Its checks run in money_out.schedule.validate_schedule."""
