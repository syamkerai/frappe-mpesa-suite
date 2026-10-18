# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaPaymentIntent(Document):
    """One payment asked for by an external client (STK prompt or pay-page link)."""

    def on_payment_authorized(self, status=None):
        # Called by the STK callback (utils.handle_successful_transaction) for prompts we sent.
        # Must never raise: this runs inside Safaricom's STK callback, and an exception there
        # rolls back the callback (losing the receipt) and Safaricom does not retry.
        import frappe

        from ....gateway.intents import refresh

        frappe.db.savepoint("mpesa_intent_callback")
        try:
            refresh(self.name)
        except Exception:
            try:
                frappe.db.rollback(save_point="mpesa_intent_callback")
            except Exception:
                pass
            frappe.log_error(frappe.get_traceback(), f"M-Pesa intent update failed in STK callback: {self.name}")
