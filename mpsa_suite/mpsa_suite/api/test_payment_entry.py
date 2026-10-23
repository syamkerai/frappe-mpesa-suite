from frappe.tests.utils import FrappeTestCase

from mpsa_suite.mpsa_suite.api.payment_entry import (
    get_outstanding_invoices,
    set_paid_amount_and_received_amount,
)


class TestPaymentFunctions(FrappeTestCase):
    def test_get_outstanding_invoices(self):
        invoices = get_outstanding_invoices("Test Company Maniac", "Test Customer")  # no such customer: empty list

        self.assertTrue(isinstance(invoices, list))

    def test_set_paid_amount_and_received_amount(self):
        party_account_currency = "KES"
        bank = {"account_currency": "KES", "bank_currency": "KES", "conversion_rate": 1.0}
        outstanding_amount = 100.00
        payment_type = "Receive"
        bank_amount = None
        conversion_rate = 1.0

        paid_amount, received_amount = set_paid_amount_and_received_amount(
            party_account_currency, bank, outstanding_amount, payment_type, bank_amount, conversion_rate
        )

        self.assertEqual(paid_amount, 100.00)
        self.assertEqual(received_amount, 100.00)
