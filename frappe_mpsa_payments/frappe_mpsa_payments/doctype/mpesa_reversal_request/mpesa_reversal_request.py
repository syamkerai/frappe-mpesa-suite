# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out.api import PAYOUT_OR_ADMIN
from ....money_out.reversal import send_reversal, validate_request
from ....permissions import require


class MpesaReversalRequest(Document):
    """Reverse a received C2B payment through Daraja. Submitting sends it."""

    def validate(self):
        if self.docstatus == 0:
            validate_request(self)

    def before_submit(self):
        require(PAYOUT_OR_ADMIN)
        self.status = "Not Initiated"

    def on_submit(self):
        send_reversal(self.name)

    def before_cancel(self):
        if self.status not in ("Not Initiated", "Failed", "Unknown"):
            frappe.throw(f"A reversal that is {self.status} cannot be cancelled")
        require(PAYOUT_OR_ADMIN)
