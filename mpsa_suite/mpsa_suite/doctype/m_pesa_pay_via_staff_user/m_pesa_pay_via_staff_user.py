# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MPesaPayviaStaffUser(Document):
    """One user who may pay a supplier's till through a staff member (Pay via Staff, round 4.5):
    a row of M-Pesa Suite Settings' Who May Pay via Staff. No row means nobody may."""
