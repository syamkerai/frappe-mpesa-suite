"""Receipt matching fixes on a real database.

C05: the STK booking's duplicate-C2B neutralising cancels a booked register with its Payment
Entry, raises (never skips) when that fails, and leaves a draft that Match Log rows link, which
its own on_submit then refuses; on two connections, the register's and the prompt's locking
reads make the other booking wait. C06: the orphan-STK matcher never adopts a prompt Safaricom
answered or one sent again after the payment, and a callback for another receipt never
overwrites an adopted prompt nor books over an adoption left on a draft order. C24: a part
payment on a prompted intent is booked unless paid to another company's number; a paybill
payment long after the intent closed pays it. C27: STK review rows (Unverified / Mismatch) are
written to the Match Log.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, flt, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import booking, matching, stk_verify
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import EXPRESS, REGISTER

MATCH_LOG = "Mpesa Match Log"
REGISTER_CLASS = (
    "mpsa_suite.mpsa_suite.doctype.mpesa_c2b_payment_register."
    "mpesa_c2b_payment_register.MpesaC2BPaymentRegister"
)
EXPRESS_CLASS = (
    "mpsa_suite.mpsa_suite.doctype.mpesa_express_request."
    "mpesa_express_request.MpesaExpressRequest.initiate_request"
)


def fake_stk_accepted(self):
    frappe.db.set_value(self.doctype, self.name, {"checkout_request_id": "ws_CO_" + frappe.generate_hash(length=12),
                                                  "response_code": "0"})


def _receipt_code() -> str:
    return "T" + frappe.generate_hash(length=9).upper()


class TestFixMatching(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # CI cannot resolve the example webhook host; the SSRF guard has its own unit tests.
        cls._public = patch("mpsa_suite.gateway.netguard.is_public_url", return_value=True)
        cls._public.start()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa Customer")
        name = "_Test Matching Bot"
        if not frappe.db.exists("Mpesa API Client", name):
            frappe.get_doc({
                "doctype": "Mpesa API Client", "client_name": name, "mpesa_settings": t.SETTINGS,
                "webhook_url": "https://hooks.example.com/mpesa", "max_amount": 100000,
                "can_link_documents": 1,
            }).insert()
        cls.client = frappe.get_doc("Mpesa API Client", name)

    @classmethod
    def tearDownClass(cls):
        cls._public.stop()
        super().tearDownClass()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    # ------------------------------------------------------------ helpers
    def _prompt(self, amount, reference_doctype="Sales Invoice", reference_name=None, **values):
        """A submitted STK prompt, without sending anything to Safaricom."""
        doc = frappe.get_doc(
            {
                "doctype": EXPRESS,
                "settings": t.SETTINGS,
                "phone_number": "254708374149",
                "currency": "KES",
                "base_amount": amount,
                "status": "In Progress",
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "checkout_request_id": "ws_CO_" + frappe.generate_hash(length=12),
            }
        ).insert(ignore_permissions=True)
        frappe.db.set_value(EXPRESS, doc.name, {"docstatus": 1, "creation": add_to_date(now_datetime(), minutes=-2),
                                                **values}, update_modified=False)
        return frappe.get_doc(EXPRESS, doc.name)

    def _callback(self, req, receipt, paid=True):
        from mpsa_suite.mpsa_suite.api.m_pesa_api import stk_push_callback

        cb = {"MerchantRequestID": "1", "CheckoutRequestID": req.checkout_request_id,
              "ResultCode": 0 if paid else 1032, "ResultDesc": "ok" if paid else "Request cancelled by user"}
        if paid:
            cb["CallbackMetadata"] = {"Item": [
                {"Name": "Amount", "Value": flt(req.amount)},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": int(now_datetime().strftime("%Y%m%d%H%M%S"))},
                {"Name": "PhoneNumber", "Value": 254708374149},
            ]}
        with patch("mpsa_suite.money_in.stk_verify.queue"):
            stk_push_callback(Body={"stkCallback": cb})

    def _logs(self, **filters):
        return frappe.get_all(MATCH_LOG, filters=filters, fields=["name", "result", "method", "details", "receipt"])

    def _live_entries(self, receipt):
        return frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": 1}, pluck="name")

    # ------------------------------------------------------------ C27
    def test_stk_review_rows_are_written_to_the_match_log(self):
        t.enable_money_in(verify_stk_callbacks=1)
        try:
            unconfirmed = self._prompt(100, reference_doctype=None)
            self._callback(unconfirmed, _receipt_code())
            frappe.db.set_value(EXPRESS, unconfirmed.name, "creation", add_to_date(now_datetime(), hours=-7),
                                update_modified=False)
            contradicted = self._prompt(100, reference_doctype=None)
            self._callback(contradicted, _receipt_code())
            with patch("mpsa_suite.money_in.stk_poll.stk_query", side_effect=ConnectionError("down")):
                self.assertEqual(stk_verify.settle(unconfirmed.name), "Pending")
            with patch("mpsa_suite.money_in.stk_poll.stk_query", return_value={"ResultCode": "1032"}):
                self.assertEqual(stk_verify.settle(contradicted.name), "Mismatch")
        finally:
            t.enable_money_in()
        self.assertEqual(frappe.db.get_value(EXPRESS, unconfirmed.name, "verification_status"), "Unverified")
        for req in (unconfirmed, contradicted):
            rows = self._logs(express_request=req.name)
            self.assertEqual([(r.result, r.method) for r in rows], [("Error", "STK Callback")], req.name)

    # ------------------------------------------------------------ C06
    def test_orphan_matcher_skips_prompts_safaricom_answered(self):
        si = t.sales_invoice(self.customer, 1373)
        failed = self._prompt(1373, reference_name=si.name, status="Failed", verification_status="Verified")
        paid_unverified = self._prompt(1373, reference_name=si.name)
        self._callback(paid_unverified, _receipt_code())  # stored as Pending until Safaricom confirms it
        reg = t.receipt(1373)  # no account reference: only the orphan matcher could place it
        self.assertIsNone(matching.adopt_orphan_stk(frappe.get_doc(REGISTER, reg)))
        self.assertIsNone(matching.match_register(reg))
        self.assertEqual(frappe.db.get_value(REGISTER, reg, "docstatus"), 0)
        for req in (failed, paid_unverified):
            self.assertFalse(frappe.db.get_value(EXPRESS, req.name, "transaction_id"))
        self.assertEqual(frappe.db.get_value(EXPRESS, failed.name, "status"), "Failed")

    def test_callback_for_another_receipt_never_replaces_an_adopted_one(self):
        si = t.sales_invoice(self.customer, 1381)
        req = self._prompt(1381, reference_name=si.name)
        reg = t.receipt(1381)
        r2 = frappe.db.get_value(REGISTER, reg, "transid")
        match = matching.match_register(reg)  # unanswered prompt: adopted and booked
        self.assertEqual((match.method, match.name), ("Orphan STK Prompt", si.name))
        self.assertEqual(frappe.db.get_value(EXPRESS, req.name, ["transaction_id", "status"]), (r2, "Completed"))
        self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount"), 0)

        r1 = _receipt_code()
        self._callback(req, r1)  # the prompt's own callback, for another receipt, arrives late
        with patch("mpsa_suite.money_in.stk_verify.book") as book:
            self.assertEqual(stk_verify.settle(req.name), "Mismatch")
        book.assert_not_called()
        row = frappe.db.get_value(EXPRESS, req.name, ["transaction_id", "status", "verification_status"], as_dict=True)
        self.assertEqual((row.transaction_id, row.status, row.verification_status), (r2, "Completed", "Mismatch"))
        self.assertEqual(len(self._live_entries(r2)), 1)
        self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": ["in", [r1, req.reference_name]]}))
        rows = self._logs(express_request=req.name, result="Error")
        self.assertEqual([(r.method, r.receipt) for r in rows], [("STK Callback", r2)])

    def test_callback_after_an_adoption_left_on_a_draft_order_is_flagged(self):
        so = t.sales_order(self.customer, 2894)  # draft prepaid order, prompted for a first half
        req = self._prompt(1447, reference_doctype="Sales Order", reference_name=so.name)
        reg = t.receipt(1447)
        r2 = frappe.db.get_value(REGISTER, reg, "transid")
        match = matching.match_register(reg)  # adopted, but a part payment only stamps the draft order
        self.assertEqual((match.method, match.name), ("Orphan STK Prompt", so.name))
        self.assertEqual(frappe.db.get_value(REGISTER, reg, ["docstatus", "matched_reference_name"]), (0, so.name))
        self.assertFalse(frappe.db.get_value(EXPRESS, req.name, "transaction_id"))

        r1 = _receipt_code()
        self._callback(req, r1)  # the prompt's own callback, for another receipt
        with patch("mpsa_suite.money_in.stk_verify.book") as book:
            self.assertEqual(stk_verify.settle(req.name), "Mismatch")
        book.assert_not_called()  # booked as well, the order would hold r1 and r2 once submitted
        row = frappe.db.get_value(EXPRESS, req.name, ["transaction_id", "verification_status"], as_dict=True)
        self.assertEqual((row.transaction_id, row.verification_status), (None, "Mismatch"))
        rows = self._logs(express_request=req.name, result="Error")
        self.assertEqual([(r.method, r.receipt) for r in rows], [("STK Callback", r2)])

    def test_prompt_sent_again_is_adopted_only_for_money_paid_after_it(self):
        si = t.sales_invoice(self.customer, 1439)
        # created hours ago and sent again two minutes before the payment (same row, new send)
        req = self._prompt(1439, reference_name=si.name, creation=add_to_date(now_datetime(), hours=-3),
                           timestamp=add_to_date(now_datetime(), minutes=-2))
        reg = frappe.get_doc(REGISTER, t.receipt(1439))
        self.assertEqual(matching.adopt_orphan_stk(reg).express, req.name)

        frappe.db.set_value(EXPRESS, req.name, "timestamp", add_to_date(now_datetime(), minutes=1),
                            update_modified=False)  # sent again after the payment instead
        self.assertIsNone(matching.adopt_orphan_stk(reg))

    # ------------------------------------------------------------ C05
    def _booked_register(self, amount):
        si = t.sales_invoice(self.customer, amount)
        reg = t.receipt(amount, billref=si.name)
        matching.match_register(reg)
        row = frappe.db.get_value(REGISTER, reg, ["transid", "docstatus", "payment_entry"], as_dict=True)
        self.assertEqual(row.docstatus, 1)
        return reg, row

    def test_prompt_cancels_the_booked_register_for_its_receipt(self):
        reg, row = self._booked_register(1409)
        req = self._prompt(1409, transaction_id=row.transid, status="Completed")
        req.validate_duplicate_c2b_records()
        self.assertEqual(frappe.db.get_value(REGISTER, reg, "docstatus"), 2)
        self.assertEqual(frappe.db.get_value("Payment Entry", row.payment_entry, "docstatus"), 2)

    def test_register_that_cannot_be_cancelled_stops_the_prompt_booking(self):
        reg, row = self._booked_register(1417)
        req = self._prompt(1417, transaction_id=row.transid, status="Completed")
        frappe.db.savepoint("c05_cancel")  # what stk_verify.book rolls back to
        with patch(f"{REGISTER_CLASS}.on_cancel", side_effect=frappe.ValidationError("reconciled")):
            with self.assertRaises(frappe.ValidationError):
                req.validate_duplicate_c2b_records()
        frappe.db.rollback(save_point="c05_cancel")
        self.assertEqual(frappe.db.get_value(REGISTER, reg, "docstatus"), 1)
        self.assertEqual(self._live_entries(row.transid), [row.payment_entry])

    def test_draft_register_is_deleted_or_left_unbookable(self):
        clean = t.receipt(1423, billref="nobody")
        frappe.db.delete(MATCH_LOG, {"register": clean})
        logged = t.receipt(1423, billref="nobody")
        matching.match_register(logged)  # leaves an Unmatched row linking it
        self.assertTrue(self._logs(register=logged))

        for reg in (clean, logged):
            receipt = frappe.db.get_value(REGISTER, reg, "transid")
            self._prompt(1423, transaction_id=receipt, status="Completed").validate_duplicate_c2b_records()
        self.assertFalse(frappe.db.exists(REGISTER, clean))
        self.assertEqual(frappe.db.get_value(REGISTER, logged, "docstatus"), 0)

        # the draft left in place can never be booked: the prompt carries its receipt
        frappe.db.set_value(REGISTER, logged, "customer", self.customer)
        frappe.db.savepoint("c05_draft")
        with self.assertRaises(frappe.ValidationError):
            booking.book_register(logged)
        frappe.db.rollback(save_point="c05_draft")
        self.assertFalse(self._live_entries(frappe.db.get_value(REGISTER, logged, "transid")))

    # ------------------------------------------------------------ C24
    def _prompted_intent(self, amount, si):
        from mpsa_suite.gateway import intents

        intent = intents.create(self.client, amount=amount, order_ref="WA-" + frappe.generate_hash(length=6),
                                phone="0712345678", reference_doctype="Sales Invoice", reference_name=si.name)[0]
        with patch(EXPRESS_CLASS, fake_stk_accepted):
            intents.send_prompt(intent)
        return intent.name

    def test_part_payments_on_a_prompted_intent_are_booked(self):
        si = t.sales_invoice(self.customer, 1500)
        intent = self._prompted_intent(1500, si)
        first = t.receipt(1000, billref=intent)
        match = matching.match_register(first)
        self.assertEqual((match.method, match.doctype, match.name), ("Reference", "Sales Invoice", si.name))
        self.assertEqual(frappe.db.get_value(REGISTER, first, "docstatus"), 1)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 500)
        self.assertEqual(frappe.db.get_value("Mpesa Payment Intent", intent, "status"), "Prompted")

        second = t.receipt(500, billref=intent)
        matching.match_register(second)
        self.assertEqual(frappe.db.get_value(REGISTER, second, "docstatus"), 1)
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)

    def test_paybill_payment_long_after_the_intent_closed_pays_it(self):
        si = t.sales_invoice(self.customer, 1500)
        intent = self._prompted_intent(1500, si)
        frappe.db.set_value("Mpesa Payment Intent", intent,
                            {"status": "Expired", "expires_at": add_days(now_datetime(), -3),
                             "modified": add_days(now_datetime(), -3)}, update_modified=False)
        reg = t.receipt(1500, billref=intent)  # beyond sync_open's one-day look-back
        receipt = frappe.db.get_value(REGISTER, reg, "transid")
        self.assertIsNone(matching.match_register(reg))
        self.assertEqual(frappe.db.get_value("Mpesa Payment Intent", intent, ["status", "receipt"]), ("Paid", receipt))
        self.assertEqual(len(self._live_entries(receipt)), 1, "booked straight from the receipt")
        self.assertEqual(frappe.db.get_value(REGISTER, reg, "docstatus"), 0, "kept a draft for the gateway")
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 0)
        rows = self._logs(register=reg, result="Matched")
        self.assertTrue(rows and f"booked by payment intent {intent}" in rows[0].details)

    def test_part_payment_paid_to_another_company_is_held(self):
        si = t.sales_invoice(self.customer, 1500)
        intent = self._prompted_intent(1500, si)
        reg = t.receipt(1000, billref=intent)
        frappe.db.set_value(REGISTER, reg, "businessshortcode", "555555", update_modified=False)  # not the client's
        self.assertIsNone(matching.match_register(reg))
        self.assertEqual(frappe.db.get_value(REGISTER, reg, ["docstatus", "matched_reference_name"]), (0, None))
        self.assertEqual(flt(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount")), 1500)
        self.assertEqual(frappe.db.get_value("Mpesa Payment Intent", intent, "status"), "Prompted")
        rows = self._logs(register=reg, result="Matched")
        self.assertTrue(rows and "555555, outside the client's company" in rows[0].details)
        self.assertTrue(self._logs(register=reg, result="Unmatched"), "the gateway tells staff to book it by hand")


class TestFixMatchingRace(IntegrationTestCase):
    """C05 on two real connections: each booking's locking read makes the other one wait, where a
    plain read would let both book the receipt. Nothing is committed: the first connection's
    writes stay in its open transaction, and both connections are rolled back after the test.
    """

    def setUp(self):
        # secondary_connection() leaves its own connection current after first use: every step
        # below picks its connection, and the first one is current again after the test
        self.addCleanup(setattr, frappe.local, "db", self._primary_connection)
        with self.secondary_connection():
            frappe.db.sql("SET SESSION innodb_lock_wait_timeout = 2")

    def _second_connection_waits(self, write):
        with self.secondary_connection():
            try:
                with self.assertRaises(frappe.QueryTimeoutError):
                    write()
            finally:
                frappe.db.rollback()

    def test_settle_and_the_matcher_wait_for_each_other(self):
        # The matcher submits a register for a receipt no prompt carries yet: its duplicate check
        # locks that receipt in the Express Request index until the booking commits...
        receipt = _receipt_code()
        with self.primary_connection(), patch(f"{REGISTER_CLASS}._reconcile_payment"):
            register = frappe.new_doc(REGISTER)
            register.transid = receipt
            register.on_submit()
        # ...so a prompt settling that receipt meanwhile waits, and then finds the register booked
        self._second_connection_waits(lambda: frappe.db.sql(
            "INSERT INTO `tabMpesa Express Request` (name, transaction_id) VALUES (%s, %s)",
            ("_T-" + frappe.generate_hash(length=10), receipt),
        ))

        # A register the matcher is booking (its row locked, here a fresh insert): the prompt's
        # duplicate check waits for it, where its snapshot would show no register at all.
        receipt = _receipt_code()
        with self.primary_connection():
            frappe.db.sql(
                "INSERT INTO `tabMpesa C2B Payment Register` (name, transid, docstatus) VALUES (%s, %s, 0)",
                ("_T-" + frappe.generate_hash(length=10), receipt),
            )
        self._second_connection_waits(
            lambda: frappe.get_doc(
                {"doctype": EXPRESS, "name": "_T-STK", "transaction_id": receipt}
            ).validate_duplicate_c2b_records()
        )
