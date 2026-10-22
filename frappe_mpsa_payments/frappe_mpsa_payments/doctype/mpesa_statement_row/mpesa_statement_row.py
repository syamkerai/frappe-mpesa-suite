# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaStatementRow(Document):
    """One row of an imported M-PESA statement and what the import did with it."""
