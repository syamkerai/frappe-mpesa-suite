"""Round 2 gateway review fixes on a real database.

A receipt refunded to the payer (a live reversal of its register, or of its receipt after the
register was amended) never pays a payment intent through verify_receipt or refresh, and is
never offered to or taken by POS quick pay; neither is a draft the gateway already booked
straight from the receipt or one that paid a payment intent. A register a cashier put on an
invoice never pays an intent (pay page, refresh, mark_paid). sync_open leaves alone a prompt
whose STK booking gave up once staff cancelled its Payment Entry, books one that has none and
marks it reconciled, and never picks a reversed receipt or one a POS invoice holds. An STK
booking whose own receipt fails to book raises; elsewhere the failure is logged.
"""

import inspect
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.mpsa_suite.api import mpesa_quick_pay
from mpsa_suite.mpsa_suite.api.payment_entry import create_payment_entry
from mpsa_suite.gateway import intents, v1
from mpsa_suite.gateway.auth import ApiError
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import EXPRESS, REGISTER, REVERSAL, reversal_holding, reversed_registers

INTENT = intents.INTENT
MOP = f"Mpesa-{t.SETTINGS}"


def verify_receipt(client, **kw):
    """The endpoint's body, past the HTTP envelope and frappe.whitelist's argument checks."""
    return inspect.unwrap(v1.verify_receipt)(client, **kw)


def _code() -> str:
    return "T" + frappe.generate_hash(length=9).upper()


def _reversal(register: str, status: str = "Reversed") -> str:
    reg = frappe.db.get_value(REGISTER, register, ["transid", "transamount"], as_dict=True)
    doc = frappe.get_doc({"doctype": REVERSAL, "c2b_register": register, "transaction_id": reg.transid,
                          "amount": reg.transamount, "original_amount": reg.transamount, "mpesa_settings": t.SETTINGS,
                          "remarks": "test", "status": status, "docstatus": 1})
    doc.name = "MRV-T-" + frappe.generate_hash(length=8)
    doc.db_insert()  # no validate, nothing sent: only the reversal's state matters here
    return doc.name


def _register(amount: float, billref: str, transid: str | None = None, **values) -> str:
    doc = frappe.get_doc({"doctype": REGISTER, **t.confirmation_payload(amount, billref=billref, transid=transid),
                          **values})
    doc.insert(ignore_permissions=True)
    return doc.name


def _amended_after_reversal(amount: float, billref: str) -> tuple[str, str]:
    """A register reversed, then cancelled and amended: a new name for the same refunded money."""
    first = t.receipt(amount, billref=billref)
    _reversal(first)
    frappe.db.set_value(REGISTER, first, "docstatus", 2)
    transid = frappe.db.get_value(REGISTER, first, "transid")
    return _register(amount, billref, transid=transid, amended_from=first), transid


def _prompt(intent_name: str, **values) -> str:
    doc = frappe.get_doc({"doctype": EXPRESS, "settings": t.SETTINGS, "phone_number": "254712345678", "amount": 500,
                          "base_amount": 500, "currency": "KES", "reference_doctype": INTENT,
                          "reference_name": intent_name, "status": "In Progress", "docstatus": 1,
                          "creation": now_datetime(), "modified": now_datetime(), **values})
    doc.name = "MER-T-" + frappe.generate_hash(length=8)
    doc.db_insert()  # nothing sent to Safaricom: only the prompt's state matters here
    return doc.name


def _payment_entry(customer: str, receipt: str, amount: float = 500, cancel: bool = False) -> str:
    pe = create_payment_entry(t.COMPANY, customer, amount, "KES", MOP, "Customer", nowdate(), receipt, nowdate(),
                              None, 1)
    if cancel:
        frappe.get_doc("Payment Entry", pe.name).cancel()
    return pe.name


def _live_entries(receipt: str) -> list:
    return frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": ["<", 2]}, pluck="name")


class TestFix2Gwrev(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._public = patch("mpsa_suite.gateway.netguard.is_public_url", return_value=True)
        cls._public.start()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test Gwrev Customer")
        name = "_Test Gwrev Client"
        if not frappe.db.exists("Mpesa API Client", name):
            frappe.get_doc({"doctype": "Mpesa API Client", "client_name": name, "mpesa_settings": t.SETTINGS,
                            "webhook_url": "https://hooks.example.com/mpesa", "max_amount": 100000,
                            "can_verify_receipts": 1}).insert()
        cls.client = frappe.get_doc("Mpesa API Client", name)

    @classmethod
    def tearDownClass(cls):
        cls._public.stop()
        super().tearDownClass()

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None

    def tearDown(self):
        frappe.local.form_dict = frappe._dict()

    def _intent(self, status: str = "Prompted", **values):
        intent = intents.create(self.client, amount=500, order_ref="GW-" + frappe.generate_hash(length=6),
                                phone="0712345678")[0]
        frappe.db.set_value(INTENT, intent.name, {"status": status, "customer": self.customer, **values})
        return frappe.get_doc(INTENT, intent.name)

    # ------------------------------------------------------------ the gateway refuses refunded money
    def test_reversed_draft_cannot_pay_an_intent_through_verify_receipt(self):
        intent = self._intent()
        _prompt(intent.name, status="Failed")  # prompted: Manual Attach would book straight from the receipt
        reg = t.receipt(500, billref="nobody-" + frappe.generate_hash(length=6))
        _reversal(reg)
        code = frappe.db.get_value(REGISTER, reg, "transid")

        self.assertTrue(verify_receipt(self.client, receipt=code)["claimed"])
        with self.assertRaises(ApiError) as ctx:
            verify_receipt(self.client, receipt=code, intent_id=intent.name)
        self.assertEqual((ctx.exception.status, ctx.exception.code), (409, "receipt_already_used"))
        self.assertEqual(frappe.db.get_value(INTENT, intent.name, ["status", "receipt"]), ("Prompted", None))
        self.assertEqual(_live_entries(code), [])
        self.assertFalse(frappe.db.exists("Mpesa Webhook Delivery", {"intent": intent.name}))

    def test_refresh_never_takes_a_reversed_or_amended_register(self):
        intent = self._intent()
        _prompt(intent.name, status="Failed")
        reversed_reg = t.receipt(500, billref=intent.name)
        _reversal(reversed_reg, status="Unknown")
        amended, transid = _amended_after_reversal(500, intent.name)
        self.assertEqual(reversed_registers([amended, reversed_reg]), [amended, reversed_reg])
        self.assertTrue(reversal_holding(amended))

        intents.refresh(intent.name)
        self.assertEqual(frappe.db.get_value(INTENT, intent.name, ["status", "receipt"]), ("Failed", None))
        self.assertEqual(_live_entries(transid), [])

    def test_book_direct_leaves_a_receipt_its_submitted_register_booked(self):
        intent = self._intent(status="Paid")
        reg = t.receipt(500, billref="pos-" + frappe.generate_hash(length=6))
        frappe.db.set_value(REGISTER, reg, "docstatus", 1)  # put on a POS invoice: no Payment Entry
        code = frappe.db.get_value(REGISTER, reg, "transid")
        intents._book_direct(intent, code, 500, now_datetime(), t.SETTINGS)
        self.assertEqual(_live_entries(code), [])

    def test_stk_receipt_on_a_company_record_is_found(self):
        code = _code()
        _prompt("SINV-NONE", reference_doctype="Sales Invoice", status="Completed", transaction_id=code,
                transaction_date=now_datetime())
        self.assertIn(t.SETTINGS, intents.client_settings(self.client))
        self.assertEqual(v1._find_receipt(self.client, code)["settings"], t.SETTINGS)

    # ------------------------------------------------------------ sync_open and given-up prompts
    def test_sync_open_extra_receipts(self):
        # A: a prompt whose STK booking gave up; sync_open booked it, then staff cancelled the entry
        a = self._intent(status="Paid", receipt=_code())
        a_code = _code()
        _prompt(a.name, status="Completed", transaction_id=a_code, booking_attempts=3)
        _payment_entry(self.customer, a_code, cancel=True)
        # B: paid again by paybill, but that money went back to the payer
        b = self._intent(status="Paid", receipt=_code())
        _prompt(b.name, status="Completed", transaction_id=b.receipt, is_reconciled=1)
        b_reg = t.receipt(500, billref=b.name)
        _reversal(b_reg)
        # C: a prompt whose STK booking gave up with nothing booked
        c = self._intent(status="Paid", receipt=_code())
        c_code = _code()
        c_prompt = _prompt(c.name, status="Completed", transaction_id=c_code, booking_attempts=3)
        # D: a cashier put the receipt's register on a POS invoice before the prompt's callback landed
        d = self._intent(status="Paid", receipt=_code())
        d_code = _code()
        d_reg = _register(500, "pos-" + frappe.generate_hash(length=6), transid=d_code)
        frappe.db.set_value(REGISTER, d_reg, "docstatus", 1)  # submitted, no Payment Entry
        _prompt(d.name, status="Completed", transaction_id=d_code, booking_attempts=3)

        candidates = set(intents._paid_with_extra_money(500))
        self.assertNotIn(a.name, candidates)
        self.assertNotIn(b.name, candidates)
        self.assertIn(c.name, candidates)
        self.assertNotIn(d.name, candidates)

        intents.sync_open()
        self.assertEqual(_live_entries(a_code), [], "a cancelled entry stays cancelled")
        self.assertEqual(_live_entries(frappe.db.get_value(REGISTER, b_reg, "transid")), [])
        self.assertEqual(len(_live_entries(c_code)), 1)
        self.assertEqual(_live_entries(d_code), [], "the POS invoice holds that money")
        self.assertEqual(frappe.db.get_value(EXPRESS, c_prompt, "is_reconciled"), 1)
        self.assertNotIn(c.name, set(intents._paid_with_extra_money(500)))

    # ------------------------------------------------------------ POS quick pay
    def _pos_url(self):
        if mpesa_quick_pay.get_mpesa_shortcode_for_mop_and_company(MOP, t.COMPANY) != t.SHORTCODE:
            url = frappe.get_doc({"doctype": "Mpesa C2B Payment Register URL", "mpesa_settings": t.SETTINGS,
                                  "business_shortcode": t.SHORTCODE, "till_number": t.SHORTCODE,
                                  "register_status": "Success", "company": t.COMPANY, "mode_of_payment": MOP})
            url.name = "_Test Gwrev URL " + frappe.generate_hash(length=6)
            url.db_insert()  # no validate: it registers URLs with Safaricom

    def _invoice(self):
        return frappe.get_doc({"doctype": "Sales Invoice", "company": t.COMPANY, "customer": self.customer,
                               "posting_date": nowdate(), "due_date": nowdate(), "currency": "KES",
                               "selling_price_list": t.PRICE_LIST,
                               "items": [{"item_code": t.ITEM, "qty": 1, "rate": 500}]}).insert()

    def _put_on_invoice(self, register: str) -> str:
        """What a cashier does in POS quick pay: the draft is submitted onto an invoice."""
        si = self._invoice()
        frappe.local.form_dict = frappe._dict(doctype="Sales Invoice", invoice_name=si.name, customer=self.customer,
                                              mpesa_payments=register)
        mpesa_quick_pay.process_mpesa()
        self.assertEqual(frappe.db.get_value(REGISTER, register, ["docstatus", "sales_invoice", "payment_entry"]),
                         (1, si.name, None))
        return si.name

    def _pos_setup(self):
        self._pos_url()
        token = "POS" + frappe.generate_hash(length=8).upper()
        regs = {"free": t.receipt(500, billref=token)}
        regs["linked"] = t.receipt(500, billref=token)
        frappe.db.set_value(REGISTER, regs["linked"], "payment_entry",
                            _payment_entry(self.customer, frappe.db.get_value(REGISTER, regs["linked"], "transid")))
        regs["gateway"] = t.receipt(500, billref=token)  # booked straight from the receipt, kept a draft
        _payment_entry(self.customer, frappe.db.get_value(REGISTER, regs["gateway"], "transid"))
        regs["reversed"] = t.receipt(500, billref=token)
        _reversal(regs["reversed"])
        regs["amended"], _ = _amended_after_reversal(500, token)
        return token, regs

    def test_pos_hides_and_refuses_booked_or_reversed_drafts(self):
        token, regs = self._pos_setup()
        frappe.local.form_dict = frappe._dict(company=t.COMPANY, search=token, mode_of_payment=MOP)
        listed = [p.name for p in mpesa_quick_pay.get_mpesa_payments()["payments"]]
        self.assertEqual(listed, [regs["free"]])

        si = self._invoice()
        for key in ("linked", "gateway", "reversed", "amended"):
            frappe.local.form_dict = frappe._dict(doctype="Sales Invoice", invoice_name=si.name,
                                                  customer=self.customer, mpesa_payments=regs[key])
            with self.assertRaises(frappe.ValidationError, msg=key):
                mpesa_quick_pay.process_mpesa()
            self.assertEqual(frappe.db.get_value(REGISTER, regs[key], "docstatus"), 0, key)

        frappe.local.form_dict = frappe._dict(doctype="Sales Invoice", invoice_name=si.name, customer=self.customer,
                                              mpesa_payments=regs["free"])
        out = mpesa_quick_pay.process_mpesa()
        self.assertEqual([p["reference"] for p in out["payments_added"]], [regs["free"]])
        self.assertEqual(frappe.db.get_value(REGISTER, regs["free"], "docstatus"), 1)

    # ------------------------------------------------------------ POS first, gateway second
    def _assert_unpaid(self, intent_name: str, status: str, receipt: str):
        self.assertEqual(frappe.db.get_value(INTENT, intent_name, ["status", "receipt"]), (status, None))
        self.assertFalse(frappe.db.exists("Mpesa Webhook Delivery",
                                          {"intent": intent_name, "event": "payment.succeeded"}))
        self.assertEqual(_live_entries(receipt), [])

    def test_register_a_pos_invoice_took_never_pays_an_intent(self):
        self._pos_url()
        # an expired pay link without a customer (the matcher holds its register for staff)
        link = self._intent(status="Expired", customer=None)
        link_reg = t.receipt(500, billref=link.name)
        # a prompted intent whose prompt failed; the customer paid by paybill instead
        prompted = self._intent(status="Prompted")
        _prompt(prompted.name, status="Failed")
        prompted_reg = t.receipt(500, billref=prompted.name)
        self._put_on_invoice(link_reg)
        self._put_on_invoice(prompted_reg)

        token = frappe.db.get_value(INTENT, link.name, "pay_token")
        page = inspect.unwrap(v1.page_status)(token=token)  # the payer opens the pay link
        self.assertEqual((page["payment"]["status"], page["payment"]["receipt"]), ("expired", None))
        intents.refresh(link.name)
        intents.refresh(prompted.name)
        self._assert_unpaid(link.name, "Expired", frappe.db.get_value(REGISTER, link_reg, "transid"))
        self._assert_unpaid(prompted.name, "Failed", frappe.db.get_value(REGISTER, prompted_reg, "transid"))
        self.assertTrue(frappe.db.exists("Mpesa Match Log", {"reference_name": link.name, "result": "Error"}))

        with self.assertRaises(ApiError) as ctx:  # refresh read the register before the cashier's submit
            intents.mark_paid(frappe.get_doc(INTENT, link.name), frappe.db.get_value(REGISTER, link_reg, "transid"),
                              500, now_datetime(), "JANE", register=link_reg)
        self.assertEqual(ctx.exception.code, "receipt_already_used")

    def test_pos_refuses_a_receipt_that_paid_an_intent(self):
        self._pos_url()
        token = "PI" + frappe.generate_hash(length=8).upper()
        reg = t.receipt(500, billref=token)
        code = frappe.db.get_value(REGISTER, reg, "transid")
        self._intent(status="Paid", receipt=code, customer=None)  # left a draft for staff: no customer
        frappe.local.form_dict = frappe._dict(company=t.COMPANY, search=token, mode_of_payment=MOP)
        self.assertEqual(mpesa_quick_pay.get_mpesa_payments()["payments"], [])
        frappe.local.form_dict = frappe._dict(doctype="Sales Invoice", invoice_name=self._invoice().name,
                                              customer=self.customer, mpesa_payments=reg)
        with self.assertRaises(frappe.ValidationError):
            mpesa_quick_pay.process_mpesa()
        self.assertEqual(frappe.db.get_value(REGISTER, reg, "docstatus"), 0)

    # ------------------------------------------------------------ STK booking of the intent's own receipt
    def test_stk_booking_raises_when_its_receipt_fails_to_book(self):
        def broken(*args, **kwargs):
            raise frappe.ValidationError("accounting period closed")

        stk = self._intent(status="Prompted")
        _prompt(stk.name, status="Completed", transaction_id=_code(), transaction_date=now_datetime())
        with patch("mpsa_suite.mpsa_suite.api.payment_entry.create_payment_entry", broken):
            with self.assertRaises(frappe.ValidationError):
                frappe.get_doc(INTENT, stk.name).run_method("on_payment_authorized", "Completed")
            self.assertFalse(frappe.flags.get(intents.STK_BOOKING_FLAG))

            # outside an STK booking (pay page, sync_open) the Paid status stands and staff see the error
            other = self._intent(status="Prompted")
            _prompt(other.name, status="Completed", transaction_id=_code(), transaction_date=now_datetime())
            intents.refresh(other.name)
        self.assertEqual(frappe.db.get_value(INTENT, other.name, "status"), "Paid")
        self.assertTrue(frappe.db.exists("Mpesa Match Log", {"reference_name": other.name, "result": "Error"}))
