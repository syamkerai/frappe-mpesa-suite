# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import uuid

import frappe
from frappe.model.document import Document

from ...api.b2c import make_b2c_payment_request
from ....permissions import PAYOUT_ROLES


class B2CDisbursementRequest(Document):
    """B2C Payments Transactions"""

    def before_insert(self) -> None:
        """Insert Missing Values"""
        self.originator_conversation_id = self._generate_uuid_v4()

    def validate(self) -> None:
        """B2C Payments Transactions validations"""
        if not self.originator_conversation_id:
            self.originator_conversation_id = self._generate_uuid_v4()

    def on_submit(self):
        make_b2c_payment_request(self.name)

    def _generate_uuid_v4(self) -> str:
        return str(uuid.uuid4())

    @frappe.whitelist()
    def retry_failed_payment(self) -> None:
        """Retry only payments Safaricom reported as failed."""
        if frappe.session.user != "Administrator":
            frappe.only_for(PAYOUT_ROLES)
        frappe.db.get_value(self.doctype, self.name, "name", for_update=True)
        self.reload()
        if self.docstatus != 1 or self.status != "Failed":
            frappe.throw(f"Only submitted, failed B2C requests can be retried ({self.status})")
        # Daraja rejects a reused OriginatorConversationID (500.002.1001), and the result
        # callback ignores re-sends for a request that already has a final result.
        self.db_set(
            {
                "originator_conversation_id": self._generate_uuid_v4(),
                "status": "Not Initiated",
                "result_code": None,
                "result_desc": None,
                "transaction_id": None,
            }
        )
        make_b2c_payment_request(self.name)
