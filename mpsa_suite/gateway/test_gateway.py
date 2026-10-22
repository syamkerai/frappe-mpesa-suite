import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.gateway import intents, v1, webhooks
from mpsa_suite.gateway.auth import KEY_HEADER, SECRET_HEADER, ApiError
from mpsa_suite.gateway.signing import HEADER, verify
from mpsa_suite.money_in import testing as t

EXPRESS_CLASS = (
    "mpsa_suite.mpsa_suite.doctype.mpesa_express_request."
    "mpesa_express_request.MpesaExpressRequest.initiate_request"
)


def fake_stk_accepted(self):
    frappe.db.set_value(self.doctype, self.name, {"checkout_request_id": "ws_CO_" + frappe.generate_hash(length=12),
                                                  "response_code": "0"})


class FakeRequest(SimpleNamespace):
    pass


class TestPaymentsAPI(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # CI cannot resolve the example webhook host; the SSRF guard has its own unit tests.
        cls._public = patch("mpsa_suite.gateway.netguard.is_public_url", return_value=True)
        cls._public.start()
        t.bootstrap()
        name = "_Test Chatbot"
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

    def _intent(self, **kw):
        args = {"amount": 1500, "order_ref": "WA-" + frappe.generate_hash(length=6), "phone": "0712345678"} | kw
        return intents.create(self.client, **args)[0]

    # ------------------------------------------------------------ client
    def test_client_credentials_generated(self):
        self.assertTrue(self.client.api_key.startswith("mpk_"))
        self.assertTrue(self.client.get_password("api_secret").startswith("mps_"))
        self.assertTrue(self.client.get_password("webhook_secret").startswith("whsec_"))

    def test_webhook_must_be_https(self):
        doc = frappe.get_doc({"doctype": "Mpesa API Client", "client_name": "_Test Insecure",
                              "mpesa_settings": t.SETTINGS, "webhook_url": "http://evil.example.com/x"})
        if not frappe.conf.developer_mode:
            self.assertRaises(frappe.ValidationError, doc.insert)

    # ------------------------------------------------------------ create
    def test_idempotency(self):
        first = self._intent(idempotency_key="k-" + frappe.generate_hash(length=6))
        again, created = intents.create(self.client, amount=1500, order_ref=first.order_ref, phone="0712345678",
                                        idempotency_key=first.idempotency_key)
        self.assertFalse(created)
        self.assertEqual(again.name, first.name)
        with self.assertRaises(ApiError) as ctx:
            intents.create(self.client, amount=999, order_ref=first.order_ref, idempotency_key=first.idempotency_key)
        self.assertEqual(ctx.exception.status, 409)

    def test_amount_rules(self):
        for bad in (0, 10.5, 1_000_000):
            with self.assertRaises(ApiError) as ctx:
                self._intent(amount=bad)
            self.assertEqual(ctx.exception.status, 422)

    def test_callback_host_must_be_allowed(self):
        with self.assertRaises(ApiError):
            self._intent(callback_url="https://elsewhere.example.org/hook")
        self.assertTrue(self._intent(callback_url="https://hooks.example.com/other").name)

    # ------------------------------------------------------------ prompt + settle
    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_prompt_then_c2b_receipt_settles_and_signs_webhook(self):
        intent = self._intent()
        intents.send_prompt(intent)
        intent.reload()
        self.assertEqual((intent.status, intent.prompts_sent), ("Prompted", 1))
        self.assertTrue(intent.express_request)
        self.assertEqual(frappe.db.get_value("Mpesa Express Request", intent.express_request, "reference_name"), intent.name)

        # customer paid; only the C2B confirmation reached us, with the intent as account number
        reg = t.receipt(1500, billref=intent.name)
        intents.refresh(intent.name)
        intent.reload()
        self.assertEqual(intent.status, "Paid")
        self.assertEqual(intent.register, reg)
        self.assertEqual(intent.receipt, frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid"))

        delivery = frappe.get_last_doc("Mpesa Webhook Delivery", filters={"intent": intent.name})
        self.assertEqual(delivery.event, "payment.succeeded")
        sent = {}

        def fake_post(url, data=None, headers=None, **kw):
            sent.update(url=url, data=data, headers=headers)
            return SimpleNamespace(status_code=200, text="ok")

        with patch("mpsa_suite.gateway.webhooks.requests.post", fake_post):
            self.assertEqual(webhooks.deliver(delivery.name), "Delivered")
        secret = self.client.get_password("webhook_secret")
        self.assertTrue(verify(secret, sent["data"], sent["headers"][HEADER]))
        self.assertEqual(json.loads(sent["data"])["data"]["status"], "paid")

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_receipt_cannot_pay_two_orders(self):
        a, b = self._intent(amount=200), self._intent(amount=200)
        reg = t.receipt(200, billref=a.name)
        intents.refresh(a.name)
        code = frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid")
        with self.assertRaises(ApiError) as ctx:
            intents.mark_paid(frappe.get_doc(intents.INTENT, b.name), code, 200, now_datetime(), None)
        self.assertEqual(ctx.exception.status, 409)

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_prompted_intent_books_directly_and_keeps_register_draft(self):
        t.enable_money_in()
        cust = t.customer("_Test Gateway Customer")
        si = t.sales_invoice(cust, 1500)
        intent = self._intent(reference_doctype="Sales Invoice", reference_name=si.name)
        intents.send_prompt(intent)
        reg = t.receipt(1500, billref=intent.name)  # C2B confirmation arrived before the STK callback
        intents.refresh(intent.name)
        receipt = frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid")
        pe = frappe.db.get_value("Payment Entry", {"reference_no": receipt, "docstatus": 1}, "name")
        self.assertTrue(pe, "booked straight from the receipt")
        self.assertEqual(frappe.db.get_value("Mpesa C2B Payment Register", reg, "docstatus"), 0,
                         "register must stay draft: upstream cancels it when the STK callback lands")
        self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount"), 0)

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_resend_is_refused_while_prompt_pending(self):
        intent = self._intent()
        intents.send_prompt(intent)
        with self.assertRaises(ApiError) as ctx:
            intents.send_prompt(frappe.get_doc(intents.INTENT, intent.name))
        self.assertEqual(ctx.exception.code, "prompt_pending")

    def test_booked_receipt_cannot_be_applied(self):
        si = t.sales_invoice(t.customer("_Test Gateway Customer"), 650)
        reg = t.receipt(650, billref=si.name)
        from mpsa_suite.money_in import matching

        matching.match_register(reg)  # books it against the invoice
        code = frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid")
        found = v1._find_receipt(self.client, code)
        self.assertEqual(v1._claimed_by(code, found), "booked")

    def test_expiry_and_cancel(self):
        intent = self._intent()
        frappe.db.set_value(intents.INTENT, intent.name, "expires_at", add_to_date(now_datetime(), minutes=-1))
        self.assertEqual(intents.refresh(intent.name).status, "Expired")
        self.assertEqual(frappe.get_last_doc("Mpesa Webhook Delivery", filters={"intent": intent.name}).event, "payment.expired")

        other = self._intent()
        intents.cancel(other)
        self.assertEqual(frappe.db.get_value(intents.INTENT, other.name, "status"), "Cancelled")

    # ------------------------------------------------------------ webhooks never block a status
    def test_long_callback_url_settles_and_queues_its_webhook(self):
        url = "https://hooks.example.com/mpesa?token=" + "a" * 400  # signed URLs run past 140 chars
        intent = self._intent(callback_url=url)
        t.receipt(1500, billref=intent.name)
        intents.refresh(intent.name)
        self.assertEqual(frappe.db.get_value(intents.INTENT, intent.name, "status"), "Paid")
        delivery = frappe.get_last_doc("Mpesa Webhook Delivery", filters={"intent": intent.name})
        self.assertEqual((delivery.event, delivery.url), ("payment.succeeded", url))

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_webhook_failure_keeps_paid_and_books_the_receipt(self):
        t.enable_money_in()
        si = t.sales_invoice(t.customer("_Test Gateway Customer"), 1500)
        intent = self._intent(reference_doctype="Sales Invoice", reference_name=si.name)
        intents.send_prompt(intent)
        reg = t.receipt(1500, billref=intent.name)
        with patch("mpsa_suite.gateway.webhooks.queue", side_effect=frappe.ValidationError("Value too big")):
            intents.refresh(intent.name)
        self.assertEqual(frappe.db.get_value(intents.INTENT, intent.name, "status"), "Paid")
        receipt = frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid")
        self.assertTrue(frappe.db.exists("Payment Entry", {"reference_no": receipt, "docstatus": 1}))
        self.assertFalse(frappe.db.exists("Mpesa Webhook Delivery", {"intent": intent.name}))

    def test_webhook_job_not_enqueued_is_kept_for_the_retry(self):
        intent = self._intent()
        t.receipt(1500, billref=intent.name)
        real = frappe.enqueue

        def enqueue(method, **kw):
            if method == "mpsa_suite.gateway.webhooks.deliver":
                raise ConnectionError("Redis is down")  # v16 reaches Redis inside enqueue, before the commit
            return real(method, **kw)

        with patch("frappe.enqueue", enqueue):
            intents.refresh(intent.name)
        self.assertEqual(frappe.db.get_value(intents.INTENT, intent.name, "status"), "Paid")
        delivery = frappe.get_last_doc("Mpesa Webhook Delivery", filters={"intent": intent.name})
        self.assertEqual((delivery.event, delivery.status), ("payment.succeeded", "Pending"))
        self.assertTrue(delivery.next_attempt_at and delivery.payload)

    # ------------------------------------------------------------ the customer pays twice
    def _complete(self, express_name, receipt):
        """Settled STK callback (what stk_verify.settle commits), then its booking."""
        frappe.db.set_value("Mpesa Express Request", express_name, {
            "status": "Completed", "transaction_id": receipt, "amount": 1500, "transaction_date": now_datetime(),
            "verification_status": "Verified"})
        frappe.get_doc("Mpesa Express Request", express_name).book_completed_payment()

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_second_prompt_paid_on_a_paid_intent_is_booked_on_account(self):
        t.enable_money_in()
        cust = t.customer("_Test Gateway Customer")
        si = t.sales_invoice(cust, 1500)
        intent = self._intent(reference_doctype="Sales Invoice", reference_name=si.name)
        first = intents.send_prompt(intent).express_request
        frappe.db.set_value("Mpesa Express Request", first, "creation", add_to_date(now_datetime(), minutes=-5),
                            update_modified=False)  # the resend wait has passed
        second = intents.send_prompt(frappe.get_doc(intents.INTENT, intent.name)).express_request
        self.assertNotEqual(first, second)

        r1, r2 = ("T" + frappe.generate_hash(length=9).upper() for _ in range(2))
        self._complete(first, r1)
        self.assertEqual(frappe.db.get_value(intents.INTENT, intent.name, ["status", "receipt"]), ("Paid", r1))
        self._complete(second, r2)  # the customer answered the resent prompt too

        pe = frappe.get_doc("Payment Entry", frappe.db.get_value("Payment Entry", {"reference_no": r2, "docstatus": 1}))
        self.assertEqual((pe.party, pe.paid_amount, pe.unallocated_amount), (cust, 1500, 1500))
        self.assertFalse(pe.references, "the invoice is already paid by the first receipt: on account")
        self.assertEqual(frappe.db.get_value("Mpesa Express Request", second, "is_reconciled"), 1)
        log = frappe.get_last_doc("Mpesa Match Log", filters={"reference_name": intent.name, "result": "Booked"})
        self.assertIn("extra payment", log.details)
        self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount"), 0)

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_paybill_payment_on_a_paid_prompted_intent_is_booked_by_sync_open(self):
        from mpsa_suite.money_in import matching

        t.enable_money_in()
        cust = t.customer("_Test Gateway Customer")
        si = t.sales_invoice(cust, 1500)
        intent = self._intent(reference_doctype="Sales Invoice", reference_name=si.name)
        first = intents.send_prompt(intent).express_request
        r1 = "T" + frappe.generate_hash(length=9).upper()
        self._complete(first, r1)
        self.assertEqual(frappe.db.get_value(intents.INTENT, intent.name, ["status", "receipt"]), ("Paid", r1))

        reg = t.receipt(1500, billref=intent.name)  # the customer also paid the paybill the API advertises
        r3 = frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid")
        matching.match_register(reg)  # held for the gateway
        self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": r3}))

        intents.sync_open()
        intents.sync_open()
        rows = frappe.get_all("Payment Entry", filters={"reference_no": r3, "docstatus": 1},
                              fields=["name", "party", "paid_amount", "unallocated_amount"])
        self.assertEqual([(r.party, r.paid_amount, r.unallocated_amount) for r in rows], [(cust, 1500, 1500)])
        log = frappe.get_last_doc("Mpesa Match Log", filters={"reference_name": intent.name, "result": "Booked"})
        self.assertIn(r3, log.details)
        self.assertIn("extra payment", log.details)
        matching.match_register(reg)  # the held register now finds its Payment Entry
        self.assertEqual(frappe.db.get_value("Mpesa C2B Payment Register", reg, ["docstatus", "payment_entry"]),
                         (0, rows[0].name))

    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_resend_refused_while_a_paid_prompt_has_no_receipt_yet(self):
        intent = self._intent()
        first = intents.send_prompt(intent).express_request
        # STK status query said paid (ResultCode 0); the receipt comes with the C2B confirmation
        frappe.db.set_value("Mpesa Express Request", first, {"status": "Completed",
                                                             "creation": add_to_date(now_datetime(), minutes=-5)})
        with self.assertRaises(ApiError) as ctx:
            intents.send_prompt(frappe.get_doc(intents.INTENT, intent.name))
        self.assertEqual(ctx.exception.code, "prompt_pending")

    # ------------------------------------------------------------ paybill money and its shortcode
    @patch(EXPRESS_CLASS, fake_stk_accepted)
    def test_paybill_receipt_is_booked_with_the_shortcode_that_received_it(self):
        t.enable_money_in()
        other = "_Test Gateway Paybill"
        if not frappe.db.exists("Mpesa Settings", other):
            frappe.get_doc({"doctype": "Mpesa Settings", "payment_gateway_name": other, "use_c2b": 1, "use_stk": 0,
                            "paybill_type": "Pay Bill", "sandbox": 1, "company": t.COMPANY, "consumer_key": "key",
                            "consumer_secret": "secret", "online_passkey": "passkey",
                            "business_shortcode": "600998"}).insert()
        cust = t.customer("_Test Gateway Customer")
        intent = self._intent(reference_doctype="Sales Invoice", reference_name=t.sales_invoice(cust, 1500).name)
        intents.send_prompt(intent)  # prompted on the client's own shortcode
        payload = t.confirmation_payload(1500, billref=intent.name) | {"businessshortcode": "600998"}
        frappe.get_doc({"doctype": "Mpesa C2B Payment Register", **payload}).insert(ignore_permissions=True)
        intents.refresh(intent.name)
        mop = frappe.db.get_value("Payment Entry", {"reference_no": payload["transid"], "docstatus": 1}, "mode_of_payment")
        self.assertEqual(mop, f"Mpesa-{other}")

    def test_verify_receipt_finds_company_receipts(self):
        reg = t.receipt(321, billref="whatever")
        code = frappe.db.get_value("Mpesa C2B Payment Register", reg, "transid")
        found = v1._find_receipt(self.client, code)
        self.assertEqual(found["amount"], 321)
        self.assertIsNone(v1._find_receipt(self.client, "NOSUCHCODE1"))

    # ------------------------------------------------------------ HTTP envelope
    def test_endpoint_rejects_bad_secret(self):
        request = FakeRequest(method="POST", headers={KEY_HEADER: self.client.api_key, SECRET_HEADER: "wrong"}, args={})
        with patch.object(frappe.local, "request", request, create=True):
            frappe.local.response = frappe._dict()
            out = v1.create_prompt(amount=100, phone="0712345678", order_ref="x")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "unauthenticated")
        self.assertEqual(frappe.local.response.get("http_status_code"), 401)
