"""Balance snapshots checked against the ERP's payout records when no statement is imported
(handover F12), against the real database (v15 and v16).

The shortcode of test_wallet (600888). Two Account Balance results around a B2C payout agree;
100 more leaving Utility (a payment made on the M-Pesa portal, as on Trident on 27 Oct 2026) is a
gap, alerted to Notify Role; a reserved amount with no ERP payout in flight is a warning. Payouts
are written as their Result leaves them (sending has its own tests). Each test is rolled back.
Unit coverage: tests/unit/test_balance_movement.py.
"""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.wallet import balances
from mpsa_suite.wallet.test_wallet import SETTINGS, SUITE_VALUES, _fixtures

SNAPSHOT, B2C = "Mpesa Balance Snapshot", "B2C Disbursement Request"


class TestBalanceMovement(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.f = _fixtures()
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        # a time no other test's balances are near: a week ago
        self.now = now_datetime().replace(microsecond=0) - timedelta(days=7, hours=3)
        frappe.db.delete(SNAPSHOT, {"mpesa_settings": SETTINGS, "taken_at": [">=", self.now - timedelta(days=8)]})

    def tearDown(self):
        frappe.db.rollback()

    def balance(self, minutes, utility, available=None, reserved=0.0) -> str:
        text = f"Utility Account|KES|{utility:.2f}|{(utility if available is None else available):.2f}|{reserved:.2f}|0.00"
        at = (self.now + timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S")
        (name,) = balances.from_account_balance(SETTINGS, text, at)
        return name

    def b2c(self, minutes, amount) -> str:
        name = f"_T-B2C-{frappe.generate_hash(length=8)}"
        frappe.get_doc({
            "doctype": B2C, "name": name, "docstatus": 1, "status": "Paid", "mpesa_settings": SETTINGS,
            "amount": amount, "transaction_id": "W" + frappe.generate_hash(length=9).upper(),
            "transaction_completed_datetime": self.now + timedelta(minutes=minutes), "result_code": "0",
        }).db_insert()
        return name

    def test_two_balances_around_a_b2c_payout_agree(self):
        self.balance(0, 5000)
        self.b2c(10, 1000)  # B2C Charge Bands: 1,000 costs 5
        later = self.balance(60, 3995)
        self.assertEqual(balances.check(later), "Agrees")
        row = frappe.db.get_value(SNAPSHOT, later, ["erp_movement", "previous_snapshot", "difference"], as_dict=True)
        self.assertEqual((flt(row.erp_movement), flt(row.difference)), (-1005, 0))
        self.assertTrue(row.previous_snapshot)

    def test_money_that_left_outside_the_erp_is_a_gap_and_alerted(self):
        self.balance(0, 4002.91)
        self.b2c(10, 100)  # band "100 = 0": no charge
        later = self.balance(60, 3802.91)
        with patch("mpsa_suite.money_out.party_accounts.alert") as alert:
            self.assertEqual(balances.check(later), "Gap")
        row = frappe.db.get_value(SNAPSHOT, later, ["erp_movement", "difference", "note"], as_dict=True)
        self.assertEqual((flt(row.erp_movement), flt(row.difference)), (-100, -100))
        self.assertIn("-100.00 was not sent or received by the ERP", row.note)
        self.assertEqual(alert.call_count, 1)

    def test_reserved_money_with_nothing_in_flight_is_a_warning(self):
        with patch("mpsa_suite.money_out.party_accounts.alert") as alert:
            name = self.balance(0, 4002.91, available=3902.91, reserved=100)
        row = frappe.db.get_value(SNAPSHOT, name, ["available", "reserved", "warning"], as_dict=True)
        self.assertEqual((flt(row.available), flt(row.reserved)), (3902.91, 100))
        self.assertIn("100.00 of Utility is reserved at Safaricom with no ERP payout in flight", row.warning)
        self.assertEqual(alert.call_count, 1)
