"""Bench tests (round 4.2): the payouts a print reads for its retries are read with columns the site
has, and the 80 mm proof renders, on Frappe's own Jinja environment, a failed payout's "Paid Again"
line and a batch's line for the rows paid again later."""

import frappe

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_out import payment_print


class TestPrintRetries(IntegrationTestCase):
    def test_the_payouts_of_a_document_are_read_with_the_sites_columns(self):
        for doctype in ("Purchase Invoice", "Purchase Order", "Expense Claim", "Employee Advance", "Salary Slip"):
            self.assertEqual(payment_print._mpesa_rows(doctype, "_no such document"), [], doctype)

    def test_the_proof_prints_where_a_failure_was_paid_again(self):
        template = frappe.read_file(frappe.get_app_path("mpsa_suite", "templates", "print",
                                                        "mpsa_payout_proof_80mm.html"))
        block = frappe._dict(status="Failed", receipt="", sms="", name="JANE WANJIKU", number="2547****0331",
                             party="", amount=1000.0, charge=None, charge_how="", when_text="", paid_from="",
                             document="Expense Claim HR-EXP-1", note="Insufficient balance",
                             paid_again="REQ-4 · UJS4", hidden=False)
        proof = frappe._dict(title="M-Pesa Payment Proof", doctype="B2C Payment Disbursement", name="D-1",
                             blocks=[block], paid_again_note="1 failed payout was paid again later (D-2): not listed.",
                             prepared_by="", sent_by="", total=0.0, company="", letter_head=frappe._dict(),
                             printed="29-10-2026 21:00")
        html = frappe.render_template(template, {"doc": frappe._dict(docstatus=1, status="Partially Paid"),
                                                 "mpsa_payout_proof": lambda doc: proof})
        for text in ("Paid Again", "REQ-4 · UJS4", "1 failed payout was paid again later (D-2): not listed."):
            self.assertIn(text, html)
        block.paid_again, proof.paid_again_note = "", ""
        html = frappe.render_template(template, {"doc": frappe._dict(docstatus=1, status="Failed"),
                                                 "mpsa_payout_proof": lambda doc: proof})
        self.assertNotIn("Paid Again", html)
        self.assertIn("Insufficient balance", html)
