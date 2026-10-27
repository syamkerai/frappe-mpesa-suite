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

    def test_send_via_mpesa_sends_a_reversal_another_payer_made(self):
        """Round 4: Make Reversal's user gets no Send via M-Pesa token (maker-checker); another payer
        presses it and confirms, and the submit sends the reversal (send_reversal, replaced here)."""
        from unittest.mock import patch

        from mpsa_suite.money_in import testing as t
        from mpsa_suite.money_out import send_confirm
        from mpsa_suite.mpsa_suite.doctype.mpesa_reversal_request import (
            mpesa_reversal_request as module,
        )

        t.bootstrap()  # the shortcode TESTC2B, which reverses (use_reversal)
        maker, payer = t.named_user("_t-rev-maker@example.com", "M-Pesa Payer"), t.payer()
        register = frappe.get_doc({"doctype": "Mpesa C2B Payment Register", "transid": "TREV" + frappe.generate_hash(
            length=6).upper(), "transamount": 700, "businessshortcode": t.SHORTCODE, "full_name": "TEST PAYER"})
        register.name = f"_T-C2B-REV-{frappe.generate_hash(length=6)}"
        register.db_insert()
        frappe.db.set_single_value("M-Pesa Suite Settings", "schedule_maker_checker", 1)
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
        try:
            frappe.set_user(maker)  # as Make Reversal makes it (money_out.api.make_reversal), from TESTC2B
            name = frappe.get_doc({"doctype": "Mpesa Reversal Request", "c2b_register": register.name,
                                   "mpesa_settings": t.SETTINGS, "reason": "Paid twice",
                                   "remarks": "Reversal"}).insert().name
            found = send_confirm.prepare_send("Mpesa Reversal Request", name)
            self.assertIsNone(found["token"])
            self.assertIn("another person must send it", found["problems"][0])
            frappe.set_user(payer)
            found = send_confirm.prepare_send("Mpesa Reversal Request", name)
            self.assertTrue(found["token"])
            self.assertEqual((found["rows"][0]["receiver"], found["total"]), ("TEST PAYER", 700.0))
            frappe.set_user("Administrator")
            with patch.object(module, "send_reversal") as send:
                t.send_as(payer, frappe.get_doc("Mpesa Reversal Request", name))
            send.assert_called_once_with(name)
            self.assertEqual(frappe.db.get_value("Mpesa Reversal Request", name, ["docstatus", "status"]),
                             (1, "Not Initiated"))
        finally:
            frappe.set_user("Administrator")
            frappe.db.rollback()  # the users, the register, the reversal and the switch
            frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")
