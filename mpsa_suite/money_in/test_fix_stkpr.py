"""Bench tests: STK prompts on a Payment Request ask, and book, the right amount.

A Payment Request above the shortcode's transaction limit is refused before any prompt goes
out; prompts made for parts of a request each keep their own amount and book what they
collected, against the request (Partially Paid, then Paid). A prompt for more than the request still owes books all of it. The cents a whole-shilling
prompt cannot ask are written off; a receipt from C2B of another amount is never re-booked.
"""

from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import matching, stk_verify
from mpsa_suite.money_in import payment_request as payment_requests
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import EXPRESS, REGISTER

GATEWAY = f"Mpesa-{t.SETTINGS}"
SETTINGS_MODULE = "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings"
EXPRESS_CLASS = (
    "mpsa_suite.mpsa_suite.doctype.mpesa_express_request."
    "mpesa_express_request.MpesaExpressRequest"
)


class TestStkPaymentRequest(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa PR Customer")
        cls.gateway_account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY}, "name")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    def _payment_request(self, amount, so=None):
        from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

        so = so or t.sales_order(self.customer, amount, submit=True)
        # Prompting loads the gateway through the payments app, which the test site does not
        # install; the tests create (or split) the prompts themselves.
        with patch("erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.request_phone_payment"):
            pr = make_payment_request(
                dt="Sales Order", dn=so.name, payment_gateway_account=self.gateway_account, phone_number="0708374149",
                recipient_id="payer@example.com", mute_email=1, submit_doc=1, return_doc=1,
            )
        self.assertEqual(flt(pr.grand_total), amount)
        return so, pr

    def _prompt(self, pr, amount):
        return frappe.get_doc(
            {
                "doctype": EXPRESS, "settings": t.SETTINGS, "payment_gateway": GATEWAY, "phone_number": "254708374149",
                "currency": "KES", "base_amount": amount, "status": "In Progress",
                "reference_doctype": "Payment Request", "reference_name": pr.name,
            }
        ).insert(ignore_permissions=True)

    def _receipt(self):
        return "U" + frappe.generate_hash(length=9).upper()

    def _paid(self, name, receipt, amount=None):
        """What stk_verify.settle records (submitted prompt, Completed, verified receipt)."""
        values = {"docstatus": 1, "status": "Completed", "transaction_id": receipt,
                  "verification_status": "Verified", "transaction_date": now_datetime()}
        if amount is not None:
            values["amount"] = amount
        frappe.db.set_value(EXPRESS, name, values)

    def _entry(self, receipt):
        names = frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": 1}, pluck="name",
                               order_by="creation asc")
        self.assertEqual(len(names), 1, f"one Payment Entry for {receipt}")
        return frappe.get_doc("Payment Entry", names[0])

    def _pr(self, pr):
        return frappe.db.get_value("Payment Request", pr.name, ["status", "outstanding_amount"], as_dict=True)

    # ------------------------------------------------------------ tests
    def test_request_over_the_transaction_limit_is_refused_and_parts_book_in_parts(self):
        so, pr = self._payment_request(200000)
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", 150000)
        try:
            settings = frappe.get_doc("Mpesa Settings", t.SETTINGS)
            with patch(f"{SETTINGS_MODULE}.in_test", return_value=False), \
                    patch(f"{EXPRESS_CLASS}.initiate_request") as sent:
                with self.assertRaisesRegex(frappe.ValidationError, "Transaction Limit"):
                    settings.request_for_payment(
                        reference_doctype="Payment Request", reference_docname=pr.name, request_amount=200000,
                        currency="KES", payment_gateway=GATEWAY, phone_number="0708374149",
                    )
        finally:
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", 250000)
        sent.assert_not_called()
        self.assertFalse(frappe.get_all(EXPRESS, filters={"reference_name": pr.name}))

        # Prompts made for parts of the request each keep, and book, their own amount
        parts = [self._prompt(pr, 150000), self._prompt(pr, 50000)]
        self.assertEqual([(flt(p.base_amount), flt(p.amount)) for p in parts], [(150000, 150000), (50000, 50000)])

        first, second = self._receipt(), self._receipt()
        self._paid(parts[0].name, first)
        self.assertTrue(stk_verify.book(parts[0].name))
        pe = self._entry(first)
        self.assertEqual(flt(pe.paid_amount), 150000)
        self.assertEqual([(r.reference_name, flt(r.allocated_amount), r.payment_request) for r in pe.references],
                         [(so.name, 150000, pr.name)])
        self.assertEqual(self._pr(pr), {"status": "Partially Paid", "outstanding_amount": 50000})
        self.assertEqual(frappe.db.get_value(EXPRESS, parts[0].name, "is_reconciled"), 1)

        self._paid(parts[1].name, second)
        self.assertTrue(stk_verify.book(parts[1].name))
        self.assertEqual(flt(self._entry(second).paid_amount), 50000)
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})
        self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "advance_paid")), 200000)

    def test_new_prompt_asks_at_most_what_the_request_owes(self):
        so, pr = self._payment_request(30000)
        self.assertEqual(flt(self._prompt(pr, 10000).amount), 10000)
        self.assertEqual(flt(self._prompt(pr, 45000).amount), 30000)

    def test_a_prompt_paid_but_not_booked_yet_is_never_asked_again(self):
        """Round 4 final review: the customer paid MEXP-1, its booking has not run (or failed): the Payment
        Request still owes it in ERPNext, so neither a new prompt nor the resend of another one goes out."""
        from mpsa_suite.www.mpesa import stkpush

        so, pr = self._payment_request(5000)
        paid, failed = self._prompt(pr, 5000), self._prompt(pr, 5000)
        frappe.db.set_value(EXPRESS, failed.name, {"docstatus": 1, "status": "Failed"})
        receipt = self._receipt()
        self._paid(paid.name, receipt)
        with self.assertRaisesRegex(frappe.ValidationError, f"paid by M-Pesa prompt {paid.name} .receipt {receipt}."):
            self._prompt(pr, 5000)
        with self.assertRaisesRegex(frappe.ValidationError, "being booked"):
            stkpush._check_can_push(frappe.get_doc(EXPRESS, failed.name))
        self.assertTrue(stk_verify.book(paid.name))  # booked: the request is paid, nothing is asked again
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})
        with self.assertRaisesRegex(frappe.ValidationError, "already paid"):
            self._prompt(pr, 5000)

    def test_prompt_for_more_than_is_left_books_everything_received(self):
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        so, pr = self._payment_request(100000)
        req = self._prompt(pr, 100000)
        # 40,000 by bank, from the order: ERPNext links the open request (outstanding 60,000)
        gl = frappe.db.get_value("Mode of Payment Account", {"parent": GATEWAY, "company": t.COMPANY}, "default_account")
        bank = get_payment_entry("Sales Order", so.name, party_amount=40000, bank_account=gl)
        bank.reference_no, bank.reference_date = "BANK-" + frappe.generate_hash(length=8), frappe.utils.nowdate()
        bank.insert().submit()
        self.assertEqual(self._pr(pr), {"status": "Partially Paid", "outstanding_amount": 60000})

        receipt = self._receipt()
        self._paid(req.name, receipt, amount=100000)  # the earlier prompt, re-sent for 100,000
        frappe.get_doc(EXPRESS, req.name).book_completed_payment()
        pe = self._entry(receipt)
        self.assertEqual(flt(pe.paid_amount), 100000, "the whole receipt is in the ledger")
        self.assertEqual([(flt(r.allocated_amount), r.payment_request) for r in pe.references], [(60000, pr.name)])
        self.assertEqual(flt(pe.unallocated_amount), 40000)
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})

    def test_manual_reconcile_replaces_the_c2b_booking_of_the_receipt(self):
        so, pr = self._payment_request(5000)
        req = self._prompt(pr, 5000)
        receipt = self._receipt()
        register = t.receipt(5000, billref=so.name, transid=receipt)  # the C2B copy came first
        matching.match_register(register)
        c2b_entry = frappe.db.get_value(REGISTER, register, "payment_entry")
        self.assertTrue(c2b_entry)

        self._paid(req.name, receipt)
        frappe.get_doc(EXPRESS, req.name).book_completed_payment()
        pe = self._entry(receipt)
        self.assertNotEqual(pe.name, c2b_entry)
        self.assertEqual([r.payment_request for r in pe.references], [pr.name])
        self.assertEqual(frappe.db.get_value("Payment Entry", c2b_entry, "docstatus"), 2)
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})

    def test_prompt_over_the_stk_maximum_is_refused_not_silently_dropped(self):
        so, pr = self._payment_request(300000)
        req = self._prompt(pr, 300000)
        with patch(f"{EXPRESS_CLASS}.initiate_request") as sent:
            self.assertRaises(frappe.ValidationError, req.submit)
        sent.assert_not_called()

    def test_manual_reconcile_refuses_a_c2b_receipt_of_another_amount(self):
        so, pr = self._payment_request(5000)
        req = self._prompt(pr, 5000)
        frappe.db.set_value(EXPRESS, req.name, "docstatus", 1)  # sent; no callback ever came
        receipt = self._receipt()
        register = t.receipt(6000, billref=pr.name, transid=receipt)  # paybill, account = the request
        matching.match_register(register)
        c2b_entry = frappe.db.get_value(REGISTER, register, "payment_entry")
        self.assertEqual(flt(frappe.db.get_value("Payment Entry", c2b_entry, "paid_amount")), 6000)
        with patch.object(payment_requests, "enabled", return_value=True):
            payment_requests.sync_statuses()  # order settled: request Paid, prompt given the receipt
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, ["status", "transaction_id"]), ("Completed", receipt))

        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(EXPRESS, req.name).book_completed_payment()  # a manager's Reconcile click
        self.assertEqual(frappe.db.get_value(REGISTER, register, "docstatus"), 1)
        self.assertEqual(
            frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": 1}, pluck="name"),
            [c2b_entry], "the 6,000 entry stays the only booking of the receipt",
        )
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, "is_reconciled"), 0)

    def test_cents_the_prompt_could_not_ask_are_written_off(self):
        so = t.sales_order(self.customer, 1000.40)
        so.disable_rounded_total = 1  # the request keeps its cents
        so.save()
        so.submit()
        so, pr = self._payment_request(1000.40, so=so)
        req = self._prompt(pr, 1000.40)
        receipt = self._receipt()
        self._paid(req.name, receipt, amount=1000)  # STK asks, and the customer pays, whole shillings
        before = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "auto_create_sales_invoice")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_create_sales_invoice", 1)
        try:
            frappe.get_doc(EXPRESS, req.name).book_completed_payment()
        finally:
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_create_sales_invoice", before)

        pe = self._entry(receipt)
        round_off = frappe.get_cached_value("Company", t.COMPANY, "round_off_account")
        self.assertEqual(flt(pe.paid_amount), 1000, "only what M-Pesa received")
        self.assertEqual([(d.account, flt(d.amount)) for d in pe.deductions], [(round_off, 0.40)])
        self.assertEqual(flt(sum(flt(r.allocated_amount) for r in pe.references), 2), 1000.40)
        self.assertEqual(flt(pe.unallocated_amount), 0)
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})
        # billed once paid; the invoice takes the advance (ERPNext moves the row to the invoice)
        invoice = frappe.db.get_value("Sales Invoice Item", {"sales_order": so.name, "docstatus": 1}, "parent")
        self.assertTrue(invoice, "the paid order is billed")
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")), 0)
