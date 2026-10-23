"""Bench tests, round 2, STK prompts (group stk2).

C03 windows: the poller, the give-up and the booking retries key on the latest prompt
(``timestamp``), so a re-prompt on an older Mpesa Express Request is still recovered.
Reversals: a paid prompt whose receipt has a live reversal is never booked, by the job or by
a manager's Reconcile click, and the refusal is logged once; a Failed reversal lets it book.
C01: a Payment Request is prompted once, refused above the transaction limit before anything
is sent (its own submit fails and rolls back); other references are split into parts of at
least 1 shilling, all checked first.
C02: a prompt whose request's order was billed meanwhile books against the order's invoice,
into the request's own account, with the cents the invoice still owes written off.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, flt, now_datetime

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.money_in import stk_poll, stk_verify
from frappe_mpsa_payments.money_in import testing as t
from frappe_mpsa_payments.money_in.common import EXPRESS, MATCH_LOG, REGISTER, REVERSAL

GATEWAY = f"Mpesa-{t.SETTINGS}"
SETTINGS_MODULE = "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings"
EXPRESS_CLASS = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request."
    "mpesa_express_request.MpesaExpressRequest"
)


class TestStkRound2(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa STK2 Customer")
        cls.gateway_account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY}, "name")

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    def _payment_request(self, amount, so=None):
        from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

        so = so or t.sales_order(self.customer, amount, submit=True)
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

    def _sent_prompt(self, created, sent, **values):
        """A submitted prompt Safaricom accepted (``sent``) on a request made at ``created``."""
        so, pr = self._payment_request(1000)
        name = self._prompt(pr, 1000).name
        frappe.db.set_value(EXPRESS, name, {"docstatus": 1, "checkout_request_id": "ws_CO_" + frappe.generate_hash(length=8),
                                            "timestamp": sent, **values}, update_modified=False)
        frappe.db.set_value(EXPRESS, name, "creation", created, update_modified=False)
        return name

    def _receipt(self):
        return "U" + frappe.generate_hash(length=9).upper()

    def _paid(self, name, receipt, amount=None):
        values = {"docstatus": 1, "status": "Completed", "transaction_id": receipt,
                  "verification_status": "Verified", "transaction_date": now_datetime()}
        if amount is not None:
            values["amount"] = amount
        frappe.db.set_value(EXPRESS, name, values)

    def _entries(self, receipt):
        return frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": 1}, pluck="name",
                              order_by="creation asc")

    def _pr(self, pr):
        return frappe.db.get_value("Payment Request", pr.name, ["status", "outstanding_amount"], as_dict=True)

    def _ask(self, limit, **kwargs):
        before = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "transaction_limit")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", limit)
        try:
            settings = frappe.get_doc("Mpesa Settings", t.SETTINGS)
            with patch(f"{SETTINGS_MODULE}.in_test", return_value=False), \
                    patch(f"{EXPRESS_CLASS}.initiate_request") as sent:
                try:
                    settings.request_for_payment(currency="KES", payment_gateway=GATEWAY, phone_number="0708374149",
                                                 **kwargs)
                    return sent, None
                except frappe.ValidationError as e:
                    return sent, e
        finally:
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", before)

    # ------------------------------------------------------------ C01: one prompt per Payment Request
    def test_payment_request_above_the_limit_is_refused_before_any_prompt(self):
        so, pr = self._payment_request(200000)
        sent, error = self._ask(150000, reference_doctype="Payment Request", reference_docname=pr.name,
                                request_amount=200000)
        self.assertIsNotNone(error)
        self.assertIn("Transaction Limit", str(error))
        sent.assert_not_called()
        self.assertEqual(frappe.get_all(EXPRESS, filters={"reference_name": pr.name}, pluck="name"), [])

    def test_payment_request_submit_above_the_limit_fails_and_leaves_no_prompt(self):
        """Through ERPNext's own submit (request_phone_payment, not patched): the refusal raises
        out of the Payment Request's submit, so the request rolls back with nothing sent."""
        from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

        from frappe_mpsa_payments.utils.utils import get_payment_gateway_controller

        so = t.sales_order(self.customer, 200000, submit=True)
        before = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "transaction_limit")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", 150000)
        frappe.db.savepoint("stk2_pr_submit")
        try:
            # ERPNext finds the gateway's controller through the payments app, not installed on
            # the test site; ours does the same lookup (Payment Gateway -> Mpesa Settings).
            with patch("erpnext.accounts.doctype.payment_request.payment_request._get_payment_gateway_controller",
                       side_effect=get_payment_gateway_controller), \
                    patch(f"{SETTINGS_MODULE}.in_test", return_value=False), \
                    patch(f"{EXPRESS_CLASS}.initiate_request") as sent, \
                    self.assertRaises(frappe.ValidationError) as refused:
                make_payment_request(
                    dt="Sales Order", dn=so.name, payment_gateway_account=self.gateway_account,
                    phone_number="0708374149", recipient_id="payer@example.com", mute_email=1, submit_doc=1,
                    return_doc=1,
                )
            self.assertIn("Transaction Limit", str(refused.exception))
            sent.assert_not_called()
            made = frappe.get_all("Payment Request", filters={"reference_name": so.name}, pluck="name")
            self.assertEqual(
                frappe.get_all(EXPRESS, filters={"reference_doctype": "Payment Request",
                                                 "reference_name": ["in", made or [""]]}, pluck="name"),
                [], "no Express Request was made",
            )
        finally:
            frappe.db.rollback(save_point="stk2_pr_submit")  # what the failed web request does
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", before)
        self.assertEqual(
            frappe.get_all("Payment Request", filters={"reference_name": so.name, "docstatus": 1}, pluck="name"), []
        )

    def test_payment_request_within_the_limit_is_prompted_once_for_all_of_it(self):
        so, pr = self._payment_request(200000)
        sent, error = self._ask(250000, reference_doctype="Payment Request", reference_docname=pr.name,
                                request_amount=200000)
        self.assertIsNone(error)
        self.assertEqual(sent.call_count, 1)
        parts = frappe.get_all(EXPRESS, filters={"reference_name": pr.name, "docstatus": 1}, fields=["amount"],
                               order_by="creation asc")
        self.assertEqual([flt(p.amount) for p in parts], [200000])

    def test_other_reference_with_a_sub_shilling_remainder_gets_no_part_under_one_shilling(self):
        si = t.sales_invoice(self.customer, 150000.30)
        sent, error = self._ask(150000, reference_doctype="Sales Invoice", reference_docname=si.name,
                                request_amount=150000.30)
        self.assertIsNone(error)
        parts = frappe.get_all(EXPRESS, filters={"reference_name": si.name, "docstatus": 1}, fields=["amount"],
                               order_by="creation asc")
        self.assertEqual([flt(p.amount) for p in parts], [150000.30])  # asked as 150,000
        self.assertEqual(sent.call_count, 1)

    def test_no_part_is_sent_unless_every_part_can_be_sent(self):
        si = t.sales_invoice(self.customer, 500000.60)
        sent, error = self._ask(250000, reference_doctype="Sales Invoice", reference_docname=si.name,
                                request_amount=500000.60)  # 250,000 + 250,000.60, which asks 250,001
        self.assertIsNotNone(error)
        sent.assert_not_called()
        self.assertEqual(frappe.get_all(EXPRESS, filters={"reference_name": si.name, "docstatus": 1}, pluck="name"), [])

    # ------------------------------------------------------------ reversals
    def _reversal(self, register, status="Reversed"):
        reg = frappe.db.get_value(REGISTER, register, ["transid", "transamount"], as_dict=True)
        doc = frappe.get_doc({"doctype": REVERSAL, "c2b_register": register, "transaction_id": reg.transid,
                              "amount": reg.transamount, "original_amount": reg.transamount,
                              "mpesa_settings": t.SETTINGS, "remarks": "test", "status": status, "docstatus": 1})
        doc.name = "MRV-T-" + frappe.generate_hash(length=8)
        doc.db_insert()  # no validate, nothing sent: only the reversal's state matters here
        return doc.name

    def test_stk_receipt_with_a_live_reversal_is_never_booked(self):
        so, pr = self._payment_request(5000)
        req = self._prompt(pr, 5000)
        receipt = self._receipt()
        register = t.receipt(5000, billref="refund " + frappe.generate_hash(length=6), transid=receipt)
        reversal = self._reversal(register)  # refunded to the payer before the callback was settled
        self._paid(req.name, receipt)

        with patch.object(frappe.db, "commit"):
            self.assertFalse(stk_verify.book(req.name))
            self.assertFalse(stk_verify.book(req.name))  # the next sweep
        self.assertEqual(self._entries(receipt), [])
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, ["is_reconciled", "booking_attempts"]), (0, 0))
        self.assertEqual(frappe.db.get_value(REGISTER, register, "docstatus"), 0, "the reversed register is left as it is")
        rows = frappe.get_all(MATCH_LOG, filters={"reference_name": req.name, "result": "Error"},
                              fields=["method", "details", "express_request", "receipt"])
        self.assertEqual(len(rows), 1, "logged once, not on every sweep")
        self.assertEqual((rows[0].method, rows[0].express_request, rows[0].receipt), ("STK Callback", req.name, receipt))
        self.assertIn(reversal, rows[0].details)
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(EXPRESS, req.name).book_completed_payment()  # a manager's Reconcile click
        self.assertEqual(self._entries(receipt), [])

        frappe.db.set_value(REVERSAL, reversal, "status", "Failed")  # the money stayed with us
        with patch.object(frappe.db, "commit"):
            self.assertTrue(stk_verify.book(req.name))
        self.assertEqual(len(self._entries(receipt)), 1)
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})

    # ------------------------------------------------------------ C03: windows on the latest prompt
    def test_poller_keys_its_window_on_the_latest_prompt(self):
        now = now_datetime()
        reprompt = self._sent_prompt(add_to_date(now, hours=-3), add_to_date(now, minutes=-10))
        just_sent = self._sent_prompt(add_to_date(now, minutes=-30), add_to_date(now, minutes=-1))
        stale = self._sent_prompt(add_to_date(now, hours=-2), add_to_date(now, hours=-2))
        polled = []
        with patch.object(stk_poll, "enabled", return_value=True), patch.object(stk_poll, "poll", polled.append):
            stk_poll.run(limit=1000)
        self.assertIn(reprompt, polled)
        self.assertNotIn(just_sent, polled)
        self.assertNotIn(stale, polled)

    def test_give_up_counts_from_the_latest_prompt(self):
        import json

        now = now_datetime()
        callback = json.dumps({"MerchantRequestID": "1", "CheckoutRequestID": "ws_CO_1", "ResultCode": 0,
                               "ResultDesc": "ok", "CallbackMetadata": {"Item": [
                                   {"Name": "Amount", "Value": 1000},
                                   {"Name": "MpesaReceiptNumber", "Value": self._receipt()}]}})
        pending = {"verification_status": "Pending", "callback_payload": callback}
        recent = self._sent_prompt(add_to_date(now, hours=-8), add_to_date(now, hours=-1), **pending)
        old = self._sent_prompt(add_to_date(now, hours=-8), add_to_date(now, hours=-7), **pending)
        with patch.object(stk_verify, "enabled", return_value=True), patch.object(stk_poll, "stk_query", return_value=None):
            self.assertEqual(stk_verify.settle(recent), "Pending")
            self.assertEqual(stk_verify.settle(old), "Pending")
        self.assertEqual(frappe.db.get_value(EXPRESS, recent, "verification_status"), "Pending", "asked again later")
        self.assertEqual(frappe.db.get_value(EXPRESS, old, "verification_status"), "Unverified")

    def test_sweep_retries_the_booking_of_a_late_reprompt(self):
        now = now_datetime()
        paid = {"status": "Completed", "verification_status": "Verified", "transaction_id": self._receipt()}
        reprompt = self._sent_prompt(add_to_date(now, hours=-30), add_to_date(now, hours=-1), **paid)
        paid["transaction_id"] = self._receipt()
        old = self._sent_prompt(add_to_date(now, hours=-30), add_to_date(now, hours=-30), **paid)
        booked = []
        with patch.object(stk_verify, "book", booked.append), patch.object(stk_verify, "settle"), \
                patch.object(frappe.db, "commit"):
            stk_verify.sweep(limit=1000)
        self.assertIn(reprompt, booked)
        self.assertNotIn(old, booked)

    # ------------------------------------------------------------ C02: order billed meanwhile
    def _bill(self, so):
        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

        si = make_sales_invoice(so.name)
        si.insert()
        si.submit()
        self.assertEqual(flt(frappe.db.get_value("Sales Order", so.name, "per_billed")), 100)
        return si

    def _book_with_auto_invoice(self, name):
        before = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "auto_create_sales_invoice")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_create_sales_invoice", 1)
        try:
            frappe.get_doc(EXPRESS, name).book_completed_payment()
        finally:
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_create_sales_invoice", before)

    def _other_bank_account(self):
        """A second KES bank account, so a test can tell the request's account from its mode of payment's."""
        name = frappe.db.get_value("Account", {"account_name": "_Test M-Pesa Gateway 2", "company": t.COMPANY}, "name")
        if name:
            return name
        mop_account = frappe.db.get_value(
            "Mode of Payment Account", {"parent": GATEWAY, "company": t.COMPANY}, "default_account"
        )
        return frappe.get_doc({
            "doctype": "Account", "account_name": "_Test M-Pesa Gateway 2", "company": t.COMPANY,
            "parent_account": frappe.db.get_value("Account", mop_account, "parent_account"),
            "account_type": "Bank", "account_currency": "KES",
        }).insert().name

    def test_prompt_on_a_request_whose_order_was_billed_books_against_the_invoice(self):
        so, pr = self._payment_request(10000)
        si = self._bill(so)  # invoiced by hand while the prompt was out
        account = self._other_bank_account()
        frappe.db.set_value("Payment Request", pr.name, "payment_account", account)  # its gateway account's
        req = self._prompt(pr, 10000)
        receipt = self._receipt()
        self._paid(req.name, receipt)
        self._book_with_auto_invoice(req.name)

        [pe] = self._entries(receipt)
        pe = frappe.get_doc("Payment Entry", pe)
        self.assertEqual(flt(pe.paid_amount), 10000)
        self.assertEqual(pe.paid_to, account, "into the request's own account, as ERPNext books a request")
        self.assertEqual([(r.reference_doctype, r.reference_name, flt(r.allocated_amount)) for r in pe.references],
                         [("Sales Invoice", si.name, 10000)])
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, "is_reconciled"), 1)
        invoices = frappe.db.get_values("Sales Invoice Item", {"sales_order": so.name, "docstatus": 1}, "parent",
                                        pluck=True)
        self.assertEqual(set(invoices), {si.name}, "the billed order is not billed again")

    def test_part_paid_on_a_billed_order_leaves_the_request_partly_open(self):
        so, pr = self._payment_request(10000)
        si = self._bill(so)
        req = self._prompt(pr, 6000)
        receipt = self._receipt()
        self._paid(req.name, receipt)
        frappe.get_doc(EXPRESS, req.name).book_completed_payment()

        [pe] = self._entries(receipt)
        self.assertEqual(flt(frappe.db.get_value("Payment Entry", pe, "paid_amount")), 6000)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 4000)
        self.assertEqual(self._pr(pr), {"status": "Partially Paid", "outstanding_amount": 4000})

    def test_cents_a_billed_order_still_owes_are_written_off_on_its_invoice(self):
        so = t.sales_order(self.customer, 10000.40)
        so.disable_rounded_total = 1  # request and invoice keep their cents
        so.save()
        so.submit()
        so, pr = self._payment_request(10000.40, so=so)
        si = self._bill(so)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 10000.40)
        req = self._prompt(pr, 10000.40)
        receipt = self._receipt()
        self._paid(req.name, receipt, amount=10000)  # STK asks, and the customer pays, whole shillings
        frappe.get_doc(EXPRESS, req.name).book_completed_payment()

        [pe] = self._entries(receipt)
        pe = frappe.get_doc("Payment Entry", pe)
        round_off = frappe.get_cached_value("Company", t.COMPANY, "round_off_account")
        self.assertEqual(flt(pe.paid_amount), 10000, "only what M-Pesa received")
        self.assertEqual([(r.reference_name, flt(r.allocated_amount)) for r in pe.references], [(si.name, 10000.40)])
        self.assertEqual([(d.account, flt(d.amount)) for d in pe.deductions], [(round_off, 0.40)])
        self.assertEqual(flt(pe.unallocated_amount), 0)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0,
                         "the invoice is closed, not left owing 0.40 that no prompt can ask")
        self.assertEqual(self._pr(pr), {"status": "Paid", "outstanding_amount": 0})
