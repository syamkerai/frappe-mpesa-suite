# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from ....money_out import maker_checker, send_confirm
from ....money_out.b2b import send_b2b, validate_request
from ....money_out.schedule import INTERRUPTED, assert_line_due, check_request
from ....permissions import ADMIN_ROLES, PAYOUT_ROLES, require, require_named_user

# Written only by the server (the send, the ack, the Result, the booking), with db writes. The
# money checks rely on them, so a save after submit never changes them.
SERVER_FIELDS = (
    "status", "conversation_id", "originator_conversation_id", "response_code", "response_description",
    "error_code", "error_message", "queue_timed_out", "result_code", "result_desc", "transaction_id",
    "trans_completed_time", "receiver_public_name", "receiver_name_mismatch", "debit_party_charges",
    "debit_account_balance", "payment_entry", "journal_entry", "entry_status", "entry_note",
    "entry_attempted_on", "entry_alerted_on", "send_started_on", "completed_time_estimated",
)
# A request in one of these states may have reached M-Pesa.
MAY_HAVE_PAID = ("Initiated", "Paid", "Unknown")


class MpesaB2BPaymentRequest(Document):
    """Pay a supplier's paybill or till from an M-Pesa shortcode. Submitting sends it."""

    def validate(self):
        if self.docstatus == 0:
            check_request(self)
            validate_request(self)

    def before_submit(self):
        # Every check again, under the document's lock: validate() above runs only on drafts,
        # and Frappe also submits an insert that carries docstatus 1 (F1).
        require_named_user()
        send_confirm.assert_deliberate(self)  # Send via M-Pesa or Pay Selected, never a plain submit
        require(PAYOUT_ROLES)
        if not self.get("schedule_line"):
            maker_checker.check(self)  # a schedule line was checked at Pay Selected
        check_request(self)  # a schedule line only from Pay Selected
        assert_line_due(self)  # never before the line's planned day, whatever the route
        validate_request(self, at_submit=True)
        self.status = "Not Initiated"

    def on_submit(self):
        if self.flags.get("mpesa_send_later") or self.get("schedule_line"):
            return  # the payment schedule's job sends it after this commits (schedule.send_batch)
        send_b2b(self.name)  # sending checks everything once more (check_ready_to_send)

    def before_update_after_submit(self):
        """A save after submit (a form opened before the Result, the API, a script) writes back
        what the database holds for the server's fields. Frappe does not enforce read only on
        the server, and a changed status, receipt or entry would fool the double-payment checks."""
        self._server_fields_from_db()

    def _server_fields_from_db(self):
        """Locking read, so a value committed after this transaction's snapshot is seen too."""
        fields = [f for f in SERVER_FIELDS if self.meta.has_field(f)]
        current = frappe.db.get_value(self.doctype, self.name, fields, as_dict=True, for_update=True)
        if current:
            self.update({f: current.get(f) for f in fields})

    def before_cancel(self):
        # Decide on what the database holds, and keep it on the cancelled request: a form opened
        # before the send (the marker does not change modified) or a typed status cannot pass.
        self._server_fields_from_db()
        if (self.status or "Not Initiated") == "Not Initiated" and self.get("send_started_on"):
            # The schedule's send began and never recorded an answer: M-Pesa may have paid it
            # (R1). Only an admin who has checked the statement cancels it, and it is kept as
            # Unknown, so a late Result still matches it and the sweep's rule holds.
            require_named_user()  # a named manager: the shared login checked no statement
            require(ADMIN_ROLES)
            self.status = "Unknown"
            self.error_message = INTERRUPTED
            return
        if self.status in ("Not Initiated", "Failed"):
            return
        if self.status in ("Unknown", "Initiated"):
            # The payment may have gone through (an Initiated one whose Result never came): only
            # an admin who has checked the statement may cancel, and so allow a new request. A
            # Result that comes later is still recorded, and counts against the document.
            require_named_user()
            require(ADMIN_ROLES)
            return
        frappe.throw(f"A B2B payment that is {self.status} cannot be cancelled")

    def on_trash(self):
        """A cancelled request that may have reached M-Pesa is the only record of that money:
        a Paid one keeps counting against its document, and a late Result needs it to match."""
        if (self.status or "Not Initiated") == "Not Initiated" and self.get("send_started_on"):
            frappe.throw(
                f"{self.name} was being sent to M-Pesa when it stopped, so it may have been paid. It is the record "
                "of that payment and cannot be deleted."
            )
        if self.status in MAY_HAVE_PAID or self.get("conversation_id") or self.get("transaction_id"):
            frappe.throw(
                f"{self.name} was sent to M-Pesa ({self.status}). It is the record of that payment and cannot "
                "be deleted."
            )
