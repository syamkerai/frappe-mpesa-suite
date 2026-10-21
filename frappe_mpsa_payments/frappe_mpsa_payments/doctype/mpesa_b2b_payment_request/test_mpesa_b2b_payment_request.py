# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt
# State-machine coverage (send, results, re-sends, timeouts) is in tests/unit/test_money_out_flow.py.

import frappe

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.money_out.b2b import validate_request


class TestMpesaB2BPaymentRequest(IntegrationTestCase):
    # These check the request's own rules on a one-off (typed) destination, which needs the
    # one-off switch and a manager; approved Supplier M-Pesa Accounts have their own tests.
    def setUp(self):
        self._one_off = frappe.db.get_single_value("M-Pesa Suite Settings", "allow_one_off_b2b")
        frappe.db.set_single_value("M-Pesa Suite Settings", "allow_one_off_b2b", 1)
        frappe.set_user("Administrator")

    def tearDown(self):
        frappe.db.set_single_value("M-Pesa Suite Settings", "allow_one_off_b2b", self._one_off or 0)

    def _doc(self, **kw):
        values = {
            "doctype": "Mpesa B2B Payment Request",
            "command_id": "BusinessPayBill",
            "receiver_shortcode": "888880",
            "account_reference": "INV-1",
            "amount": 100,
            "remarks": "Test",
        }
        values.update(kw)
        return frappe.get_doc(values)

    def test_whole_shillings_only(self):
        self.assertRaises(frappe.ValidationError, validate_request, self._doc(amount=100.5))

    def test_paybill_needs_account_number(self):
        self.assertRaises(frappe.ValidationError, validate_request, self._doc(account_reference=""))

    def test_buy_goods_drops_account_number(self):
        doc = self._doc(command_id="BusinessBuyGoods")
        validate_request(doc)
        self.assertFalse(doc.account_reference)

    def test_requester_is_normalised(self):
        doc = self._doc(requester="0712 345 678")
        validate_request(doc)
        self.assertEqual(doc.requester, "254712345678")
