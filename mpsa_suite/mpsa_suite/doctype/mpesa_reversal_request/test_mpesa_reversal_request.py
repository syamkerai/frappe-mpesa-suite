# Copyright (c) 2026, Astral Industries Limited and contributors
# For license information, please see license.txt
# State-machine coverage (send, results, re-sends, timeouts) is in tests/unit/test_money_out_flow.py.

import frappe

from mpsa_suite.compat import IntegrationTestCase


class TestMpesaReversalRequest(IntegrationTestCase):
    def test_submit_is_gated_to_payout_and_admin_roles(self):
        meta = frappe.get_meta("Mpesa Reversal Request")
        submitters = {p.role for p in meta.permissions if p.submit}
        self.assertTrue(submitters <= {"M-Pesa Manager", "M-Pesa Payer"})  # D1; round 4: not System Manager
        self.assertFalse(submitters & {"Accounts User", "Accounts Manager", "System Manager"})

    def test_who_made_a_reversal_cannot_send_it(self):
        """Maker-checker (round 4): Make Reversal's user owns the draft, so another payer sends it, and
        the submitting save cannot change what is reversed."""
        from mpsa_suite.money_in import testing as t
        from mpsa_suite.money_out import maker_checker

        maker, payer = t.named_user("_t-rev-maker@example.com", "M-Pesa Payer"), t.payer()
        doc = frappe.get_doc({"doctype": "Mpesa Reversal Request", "name": "REV-MC-1", "owner": maker,
                              "c2b_register": "C2B-X", "transaction_id": "TJR0000001", "amount": 500,
                              "mpesa_settings": "X"})
        before = frappe.get_doc(doc.as_dict())
        before.modified_by = maker
        doc._doc_before_save = before
        frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 1)
        try:
            frappe.set_user(payer)
            maker_checker.check(doc)
            doc.amount = 400
            self.assertRaisesRegex(frappe.ValidationError, "changed while being sent", maker_checker.check, doc)
            doc.amount = 500
            frappe.set_user(maker)
            self.assertRaisesRegex(frappe.ValidationError, "another person must send it", maker_checker.check, doc)
        finally:
            frappe.set_user("Administrator")
            frappe.db.rollback()  # the users and the switch
