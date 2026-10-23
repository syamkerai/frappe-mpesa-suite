# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....permissions import CONFIG_ROLES, require


class MpesaWebhookDelivery(Document):
    @frappe.whitelist()
    def retry_now(self):
        require(CONFIG_ROLES)
        from ....gateway.webhooks import deliver

        return deliver(self.name, force=True)
