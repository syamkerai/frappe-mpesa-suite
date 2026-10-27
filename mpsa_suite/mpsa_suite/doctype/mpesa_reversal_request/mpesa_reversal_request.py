# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out import maker_checker, send_confirm
from ....money_out.api import PAYOUT_OR_ADMIN
from ....money_out.reversal import send_reversal, validate_request
from ....permissions import require, require_named_user


class MpesaReversalRequest(Document):
    """Reverse a received C2B payment through Daraja. Submitting sends it."""

    def validate(self):
        if self.docstatus == 0:
            validate_request(self)

    def before_submit(self):
        require_named_user()
        send_confirm.assert_deliberate(self)  # Send via M-Pesa, never a plain submit
        require(PAYOUT_OR_ADMIN)
        maker_checker.check(self)  # who sends it is not who made it (Make Reversal) or last saved it
        self.status = "Not Initiated"

    def on_submit(self):
        send_reversal(self.name)

    def before_cancel(self):
        if self.status not in ("Not Initiated", "Failed", "Unknown"):
            frappe.throw(f"A reversal that is {self.status} cannot be cancelled")
        require(PAYOUT_OR_ADMIN)
