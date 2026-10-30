"""Bench tests (round 4.2): the refused-receiver lookups run on the site's database, the B2B form's
onload sets no warning when M-Pesa refused nothing, and the account forms' synced help says a till is
paid on its Store (or Head Office) number."""

import frappe

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.daraja import money_out as daraja
from mpsa_suite.money_out import b2b

B2B = "Mpesa B2B Payment Request"


class TestReceiverRefused(IntegrationTestCase):
    def test_the_lookups_run_on_the_sites_database(self):
        self.assertIsNone(b2b.refused_before("_no such number", daraja.B2B_BUY_GOODS))
        self.assertIsNone(b2b.refused_before("_no such number", daraja.B2B_PAYBILL, exclude="_no such request"))
        self.assertIsNone(b2b.refused_warning("", daraja.B2B_BUY_GOODS))

    def test_a_refused_request_tells_its_form_what_to_do(self):
        doc = frappe.new_doc(B2B)  # as loaded: submitted and refused (never saved here)
        doc.update({"docstatus": 1, "status": "Failed", "result_code": daraja.RECEIVER_INVALID,
                    "command_id": daraja.B2B_BUY_GOODS, "receiver_shortcode": "813712"})
        doc.run_method("onload")
        self.assertIn("Store Number (or Head Office number)", (doc.get("__onload") or {}).get("receiver_hint") or "")

    def test_a_draft_to_a_number_never_refused_gets_no_warning(self):
        doc = frappe.new_doc(B2B)
        doc.update({"command_id": daraja.B2B_BUY_GOODS, "receiver_shortcode": "_no such number"})
        doc.run_method("onload")
        self.assertNotIn("receiver_hint", doc.get("__onload") or {})

    def test_the_account_forms_say_a_till_is_paid_on_its_store_number(self):
        for doctype, field in (("Supplier M-Pesa Account", "shortcode"), ("Employee M-Pesa Account", "account_type"),
                               ("M-Pesa Claim Payee", "shortcode")):
            self.assertIn("Store Number (or Head Office number)",
                          frappe.get_meta(doctype).get_field(field).description or "", (doctype, field))
