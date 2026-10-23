# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out.b2b import send_b2b, validate_request
from ....permissions import ADMIN_ROLES, PAYOUT_ROLES, require


class MpesaB2BPaymentRequest(Document):
    """Pay a supplier's paybill or till from an M-Pesa shortcode. Submitting sends it."""

    def validate(self):
        if self.docstatus == 0:
            validate_request(self)

    def before_submit(self):
        # Every check again, under the document's lock: validate() above runs only on drafts,
        # and Frappe also submits an insert that carries docstatus 1 (F1).
        require(PAYOUT_ROLES)
        validate_request(self, at_submit=True)
        self.status = "Not Initiated"

    def on_submit(self):
        send_b2b(self.name)  # sending checks everything once more (check_ready_to_send)

    def before_cancel(self):
        if self.status in ("Not Initiated", "Failed"):
            return
        if self.status == "Unknown":
            # The payment may have gone through: only an admin who has checked the
            # statement may cancel (and so allow a new request).
            require(ADMIN_ROLES)
            return
        frappe.throw(f"A B2B payment that is {self.status} cannot be cancelled")
