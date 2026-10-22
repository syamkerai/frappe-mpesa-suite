# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaBalanceSnapshot(Document):
    """A sub-account balance Safaricom reported, and whether the ERP's lines agree with it."""
