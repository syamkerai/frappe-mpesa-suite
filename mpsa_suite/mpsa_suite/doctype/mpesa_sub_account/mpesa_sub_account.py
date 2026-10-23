# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaSubAccount(Document):
    """One wallet of a shortcode (Utility, Working / MMF...) mapped to a Bank Account of its own."""
