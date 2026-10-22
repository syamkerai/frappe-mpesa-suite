# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt
# State-machine coverage (send, results, re-sends, timeouts) is in tests/unit/test_money_out_flow.py.

import frappe

from mpsa_suite.compat import IntegrationTestCase


class TestMpesaReversalRequest(IntegrationTestCase):
    def test_submit_is_gated_to_payout_and_admin_roles(self):
        meta = frappe.get_meta("Mpesa Reversal Request")
        submitters = {p.role for p in meta.permissions if p.submit}
        self.assertTrue(submitters <= {"System Manager", "M-Pesa Manager", "Accounts Manager", "M-Pesa Payer"})
        self.assertNotIn("Accounts User", submitters)
