# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....sandbox_lab.guard import SETTINGS_FIELDS, lab_enabled, require_sandbox_settings
from ....sandbox_lab.runner import load_scenarios


class MpesaSandboxTestRun(Document):
    """A sandbox test session: its setup, test documents and one row per scenario."""

    def validate(self):
        if not lab_enabled():
            frappe.throw("The M-Pesa Sandbox Test Lab is off on this site (site config mpesa_sandbox_lab).")
        for field in SETTINGS_FIELDS:
            require_sandbox_settings(self.get(field))
        if self.c2b_settings and not self.c2b_shortcode:
            row = frappe.db.get_value(
                "Mpesa Settings", self.c2b_settings, ["till_number", "business_shortcode"], as_dict=True
            )
            self.c2b_shortcode = row.till_number or row.business_shortcode
        if self.amount is not None and int(self.amount) <= 0:
            frappe.throw("Test Amount must be a positive whole number of shillings")

    def before_insert(self):
        load_scenarios(self)
