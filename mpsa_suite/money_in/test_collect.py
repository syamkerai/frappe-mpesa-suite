"""Request M-Pesa payment on a submitted Sales Order or Sales Invoice (money_in.collect) against a
real site (ERPNext v15 and v16): ERPNext's own Payment Request is made for the amount asked, on the
company's M-Pesa gateway account, and submitted, which prompts the phone; what is owed, the
shortcode's limit and the role and permission checks hold.

Prompting loads the gateway through the payments app, which the test site does not install, so
PaymentRequest.request_phone_payment is replaced (as test_fix_stkpr does): what it is called
with is what Mpesa Settings.request_for_payment gets. Each test is rolled back; the users are named
test users (an Accounts User collects, a Sales User may not create Payment Requests), never
Administrator or System Manager. Unit coverage: tests/unit/test_r4_collect.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import collect
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import EXPRESS

PR = "Payment Request"
PHONE = "payment_request.PaymentRequest.request_phone_payment"
PROMPT = f"erpnext.accounts.doctype.payment_request.{PHONE}"
DOMAIN = "r4-collect.example.com"
COLLECTOR, SALES, BUYER = f"collector@{DOMAIN}", f"sales@{DOMAIN}", f"buyer@{DOMAIN}"
ROLES = {COLLECTOR: "Accounts User", SALES: "Sales User", BUYER: "Purchase User"}


def _user(email: str, role: str) -> str:
    if not frappe.db.exists("User", email):
        doc = frappe.new_doc("User")
        doc.update({"email": email, "first_name": email.split("@")[0], "user_type": "System User",
                    "send_welcome_email": 0, "enabled": 1})
        doc.append("roles", {"role": role})
        doc.insert(ignore_permissions=True)
    return email


class TestCollect(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        # the phone is stored in local form (money_in.payment_request.before_validate)
        t.enable_money_in(supersede_unpaid_prompts=0, normalize_payment_request_phone=1)
        for email, role in ROLES.items():
            _user(email, role)
        cls.customer = t.customer("_T Collect Customer")
        frappe.db.set_value("Customer", cls.customer, "mobile_no", "0712345678")
        cls.gateway_account = frappe.db.get_value(
            "Payment Gateway Account", {"payment_gateway": f"Mpesa-{t.SETTINGS}"}, "name")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, {"transaction_limit": 250000, "use_stk": 1})
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()
        self._no_limit = patch.object(collect, "_rate_limit")  # its own test below
        self._no_limit.start()

    def tearDown(self):
        self._no_limit.stop()
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()

    @contextmanager
    def as_user(self, user):
        frappe.set_user(user)
        try:
            yield
        finally:
            frappe.set_user("Administrator")

    def _ask(self, doc, amount, user=COLLECTOR, phone="0712 345 678"):
        with self.as_user(user), patch(PROMPT, autospec=True) as prompted:
            out = collect.request_payment(doc.doctype, doc.name, phone, amount, self.gateway_account)
        return out, prompted

    def _requests(self, doc):
        return frappe.get_all(PR, filters={"reference_doctype": doc.doctype, "reference_name": doc.name},
                              fields=["name", "docstatus", "grand_total", "phone_number", "payment_channel",
                                      "payment_gateway_account", "payment_request_type", "party", "status"],
                              order_by="creation asc")

    # ------------------------------------------------------------ the prompt is a Payment Request
    def test_a_sales_order_is_asked_for_part_of_what_it_owes(self):
        so = t.sales_order(self.customer, 5000, submit=True)
        with self.as_user(COLLECTOR):
            options = collect.collect_options("Sales Order", so.name)
        self.assertEqual((options["owed"], options["phone"]), (5000, "0712345678"))
        self.assertIn(self.gateway_account, [g["name"] for g in options["gateways"]])

        out, prompted = self._ask(so, 1500)
        (pr,) = self._requests(so)
        self.assertEqual((pr.docstatus, flt(pr.grand_total), pr.phone_number, pr.payment_channel),
                         (1, 1500, "0712345678", "Phone"))
        self.assertEqual((pr.payment_gateway_account, pr.payment_request_type, pr.party),
                         (self.gateway_account, "Inward", self.customer))
        self.assertEqual(out["payment_request"], pr.name)
        prompted.assert_called_once()  # the submit sent the prompt
        self.assertEqual(prompted.call_args.args[0].name, pr.name)

        # the request now holds 1,500: at most 3,500 more may be asked for
        with self.as_user(COLLECTOR):
            self.assertEqual(collect.collect_options("Sales Order", so.name)["owed"], 3500)
        with self.assertRaisesRegex(frappe.ValidationError, "3,500.00 left to ask for"):
            self._ask(so, 3600)
        self._ask(so, 3500)
        self.assertEqual(len(self._requests(so)), 2)

    def test_a_submitted_invoice_is_asked_for_at_most_its_outstanding(self):
        si = t.sales_invoice(self.customer, 2000)
        with self.assertRaisesRegex(frappe.ValidationError, "2,000.00 left to ask for"):
            self._ask(si, 2000.5)
        self.assertEqual(self._requests(si), [])
        self._ask(si, 2000)
        self.assertEqual(flt(self._requests(si)[0].grand_total), 2000)

    def test_at_most_the_shortcodes_transaction_limit(self):
        si = t.sales_invoice(self.customer, 9000)
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "transaction_limit", 5000)
        with self.assertRaisesRegex(frappe.ValidationError, "Transaction Limit of KES 5,000.00"):
            self._ask(si, 6000)
        self.assertEqual(self._requests(si), [])
        self._ask(si, 5000)

    def test_no_second_prompt_while_the_first_may_be_on_the_phone(self):
        si = t.sales_invoice(self.customer, 3000)
        out, _ = self._ask(si, 1000)
        prompt = frappe.get_doc({"doctype": EXPRESS, "settings": t.SETTINGS, "phone_number": "254712345678",
                                 "currency": "KES", "base_amount": 1000, "status": "In Progress",
                                 "reference_doctype": PR, "reference_name": out["payment_request"]})
        prompt.insert(ignore_permissions=True)
        frappe.db.set_value(EXPRESS, prompt.name, {"docstatus": 1, "creation": now_datetime()})
        with self.assertRaisesRegex(frappe.ValidationError, "A prompt was just sent"):
            self._ask(si, 500)

    # ------------------------------------------------------------ who, on what
    def test_only_collectors_who_create_payment_requests(self):
        so = t.sales_order(self.customer, 1000, submit=True)
        with self.assertRaises(frappe.PermissionError):  # Sales User: no Payment Request create by default
            self._ask(so, 100, user=SALES)
        with self.assertRaises(frappe.PermissionError):  # not a collecting role
            self._ask(so, 100, user=BUYER)
        self.assertEqual(self._requests(so), [])

    def test_only_submitted_orders_and_invoices_in_kes(self):
        draft = t.sales_order(self.customer, 1000)
        with self.assertRaisesRegex(frappe.ValidationError, "Submit .* first"):
            self._ask(draft, 100)
        with self.as_user(COLLECTOR), self.assertRaisesRegex(frappe.ValidationError, "Sales Order or a Sales Invoice"):
            collect.request_payment("Quotation", "QTN-1", "0712345678", 100)
        si = t.sales_invoice(self.customer, 1000)
        frappe.db.set_value("Sales Invoice", si.name, "is_pos", 1)
        with self.assertRaisesRegex(frappe.ValidationError, "POS invoice"):
            self._ask(frappe.get_doc("Sales Invoice", si.name), 100)

    def test_at_most_five_prompts_a_minute_per_user(self):
        self._no_limit.stop()
        try:
            user = _user(f"burst-{frappe.generate_hash(length=6)}@{DOMAIN}", "Accounts User")
            si = t.sales_invoice(self.customer, 100)
            with patch.object(collect, "PROMPTS_PER_MINUTE", 2), \
                    patch.object(collect, "prompt_pending", return_value=None):
                self._ask(si, 10, user=user)
                self._ask(si, 10, user=user)
                with self.assertRaisesRegex(frappe.ValidationError, "Too many M-Pesa prompts"):
                    self._ask(si, 10, user=user)
        finally:
            self._no_limit.start()
