# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MpesaWebhookDelivery(Document):
    @frappe.whitelist()
    def retry_now(self):
        frappe.only_for(("System Manager", "M-Pesa Manager"))
        from ....gateway.webhooks import deliver

        return deliver(self.name, force=True)
