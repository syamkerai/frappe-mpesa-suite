# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaMatchLog(Document):
    """Audit trail of how each M-Pesa receipt was (or could not be) matched and booked."""
