# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MPesaPettyCashTopUp(Document):
    """One top-up of a petty cash phone by M-Pesa B2C (money_out.petty_cash): written by the system only,
    from Top Up by M-Pesa and the payout's Result."""
