# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ....daraja.guard import parse_allowlist


class MPesaSuiteSettings(Document):
    def validate(self):
        if not self.get_password("callback_token", raise_exception=False):
            self.callback_token = frappe.generate_hash(length=40)

        if self.enforce_ip_allowlist and not parse_allowlist(self.allowed_callback_ips):
            frappe.throw(_("Add at least one valid IP or CIDR before enforcing the callback allow-list."))

        from ....money_out.statement import parse_bands

        try:
            parse_bands(self.get("b2c_charge_bands"))
        except ValueError as e:
            frappe.throw(_("B2C Charge Bands: {0}").format(e))

    @frappe.whitelist()
    def regenerate_callback_token(self):
        frappe.only_for(("System Manager", "M-Pesa Manager"))
        self.callback_token = frappe.generate_hash(length=40)
        self.save()
        return _(
            "New callback token saved. STK/B2C callbacks use it immediately; "
            "re-register every C2B URL before enforcing the C2B token."
        )

    @frappe.whitelist()
    def reset_rejection_counter(self):
        frappe.only_for(("System Manager", "M-Pesa Manager"))
        self.rejected_callbacks = 0
        self.last_rejected_callback_on = None
        self.last_rejected_callback_reason = None
        self.save()
