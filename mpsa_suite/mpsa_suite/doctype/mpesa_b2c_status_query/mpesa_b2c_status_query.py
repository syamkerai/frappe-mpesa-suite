# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaB2CStatusQuery(Document):
    """One Daraja Transaction Status query for a B2C or B2B payout and what it found.

    Created by ``money_out.api.check_b2c_status`` (B2C) or by ``money_out.b2b_status`` (B2B: after
    a till's Result, by the sweep, or ``money_out.api.check_b2b_status``); filled by the result
    callback.
    """
