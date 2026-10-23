# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaPaymentIntent(Document):
    """One payment asked for by an external client (STK prompt or pay-page link)."""

    def on_payment_authorized(self, status=None):
        # Called by utils.handle_successful_transaction when money_in.stk_verify (or a manager's
        # Reconcile click) books a prompt we sent, just before the prompt is marked reconciled.
        # The receipt is already committed by then, so raising only rolls back and retries the
        # booking (a manager's click shows the error instead).
        import frappe

        from ....gateway.intents import INTENT, STK_BOOKING_FLAG, book_extra_receipts, refresh

        was_paid = frappe.db.get_value(INTENT, self.name, "status") == "Paid"
        frappe.db.savepoint("mpesa_intent_callback")
        # This booking may have removed the receipt's C2B register already, so a failure to book
        # the receipt that marks the intent Paid is raised too (mark_paid would only log it).
        frappe.flags[STK_BOOKING_FLAG] = True
        try:
            refresh(self.name)
        except Exception:
            if not was_paid:
                # Raise: stk_verify rolls back (the Paid status and the register clean-up too),
                # leaves the prompt unreconciled and retries. One cause is another booking
                # marking the intent Paid after this transaction began (the save sees a newer
                # version); the retry then finds it Paid and books this prompt's receipt as an
                # extra payment below. Another is the booking of the receipt failing.
                raise
            try:
                frappe.db.rollback(save_point="mpesa_intent_callback")
            except Exception:
                pass
            frappe.log_error(frappe.get_traceback(), f"M-Pesa intent update failed in STK callback: {self.name}")
        finally:
            frappe.flags[STK_BOOKING_FLAG] = False

        # This prompt may be a second payment on an intent another receipt already paid. If the
        # intent was paid before this prompt, nothing else changed here, so failing to book the
        # prompt is raised: stk_verify rolls back, leaves it unreconciled and retries. Otherwise
        # the Paid status just set must stand, so the failure is only logged to Mpesa Match Log
        # (intents.sync_open retries it), unless this booking cancelled the receipt's C2B register
        # and its Payment Entry, which only the rollback brings back.
        book_extra_receipts(frappe.get_doc(INTENT, self.name), raise_errors=was_paid, stk_booking=True)
