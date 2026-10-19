# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaB2CStatusQuery(Document):
    """One Daraja Transaction Status query for a B2C payout and what it found.

    Created by ``money_out.api.check_b2c_status``; filled by the result callback.
    """
