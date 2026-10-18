# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class MpesaPaymentIntent(Document):
    """One payment asked for by an external client (STK prompt or pay-page link)."""

    def on_payment_authorized(self, status=None):
        # Called by the STK callback (utils.handle_successful_transaction) for prompts we sent.
        from ....gateway.intents import refresh

        refresh(self.name)
