import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from frappe_mpsa_payments.compat import IntegrationTestCase
from frappe_mpsa_payments.gateway import intents, v1, webhooks
from frappe_mpsa_payments.gateway.auth import KEY_HEADER, SECRET_HEADER, ApiError
from frappe_mpsa_payments.gateway.signing import HEADER, verify
from frappe_mpsa_payments.money_in import testing as t

EXPRESS_CLASS = (
    "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_express_request."
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
        t.bootstrap()
        name = "_Test Chatbot"
        if not frappe.db.exists("Mpesa API Client", name):
            frappe.get_doc({
                "doctype": "Mpesa API Client", "client_name": name, "mpesa_settings": t.SETTINGS,
                "webhook_url": "https://hooks.example.com/mpesa", "max_amount": 100000,
                "can_link_documents": 1,
            }).insert()
        cls.client = frappe.get_doc("Mpesa API Client", name)

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

        with patch("frappe_mpsa_payments.gateway.webhooks.requests.post", fake_post):
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
        from frappe_mpsa_payments.money_in import matching

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
