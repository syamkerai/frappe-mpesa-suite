"""Bench tests (handover F1): money received is booked even when the callback's Integration User may
not see the document it pays.

Trident gives users a User Permission on Project (applied to all doctypes) and its orders and
invoices carry a project, so the Integration User cannot read them; ERPNext's get_payment_entry
and an invoice's submit check that user's permission. STK booking and the C2B register's
auto-submit now run as Administrator (as the B2B booking and the 2-minute matcher already did):
they book on the first attempt, with no Error Log and no retry. The Integration User needs no
document access at all. Each test is rolled back.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import stk_verify
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_in.common import EXPRESS, REGISTER

GATEWAY = f"Mpesa-{t.SETTINGS}"


def _project(name: str) -> str:
    existing = frappe.db.get_value("Project", {"project_name": name, "company": t.COMPANY}, "name")
    return existing or frappe.get_doc({"doctype": "Project", "project_name": name, "company": t.COMPANY}).insert().name


class TestIntegrationUserBookings(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in()
        cls.customer = t.customer("_Test M-Pesa Project Customer")
        cls.gateway_account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": GATEWAY}, "name")
        cls.project = _project("_T Integration P1")
        cls.other_project = _project("_T Integration Other")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.tag = frappe.generate_hash(length=8)
        self.since = now_datetime()
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None

    def _integration_user(self, *roles) -> str:
        """A callback user who may not see the project the documents carry."""
        user = frappe.get_doc({"doctype": "User", "email": f"_t_callbacks_{self.tag}@example.com",
                               "first_name": "Callbacks", "send_welcome_email": 0,
                               "roles": [{"role": r} for r in roles]}).insert(ignore_permissions=True)
        frappe.get_doc({"doctype": "User Permission", "user": user.name, "allow": "Project",
                        "for_value": self.other_project, "apply_to_all_doctypes": 1}).insert(ignore_permissions=True)
        return user.name

    def _errors(self, title: str) -> list:
        return frappe.get_all("Error Log", filters={"method": ["like", f"{title}%"], "creation": [">=", self.since]},
                              pluck="name")

    def test_stk_payment_on_an_order_the_callback_user_cannot_see_books_at_once(self):
        from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request

        user = self._integration_user("Accounts User", "M-Pesa Manager")
        so = frappe.get_doc({
            "doctype": "Sales Order", "company": t.COMPANY, "customer": self.customer, "transaction_date": nowdate(),
            "delivery_date": add_days(nowdate(), 2), "currency": "KES", "selling_price_list": t.PRICE_LIST,
            "project": self.project,
            "items": [{"item_code": t.ITEM, "qty": 1, "rate": 1500, "delivery_date": add_days(nowdate(), 2)}]})
        so.insert()
        so.submit()
        self.assertFalse(frappe.has_permission("Sales Order", "read", doc=so, user=user))
        with patch("erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.request_phone_payment"):
            pr = make_payment_request(dt="Sales Order", dn=so.name, payment_gateway_account=self.gateway_account,
                                      phone_number="0708374149", recipient_id="payer@example.com", mute_email=1,
                                      submit_doc=1, return_doc=1)
        prompt = frappe.get_doc({
            "doctype": EXPRESS, "settings": t.SETTINGS, "payment_gateway": GATEWAY, "phone_number": "254708374149",
            "currency": "KES", "base_amount": 1500, "status": "In Progress", "reference_doctype": "Payment Request",
            "reference_name": pr.name}).insert(ignore_permissions=True)
        receipt = "U" + frappe.generate_hash(length=9).upper()
        frappe.db.set_value(EXPRESS, prompt.name, {"docstatus": 1, "status": "Completed", "transaction_id": receipt,
                                                   "verification_status": "Verified", "transaction_date": now_datetime()})

        frappe.set_user(user)  # what the callback's job runs as
        booked = stk_verify.book(prompt.name)
        self.assertEqual(frappe.session.user, user)
        frappe.set_user("Administrator")

        self.assertTrue(booked)
        self.assertEqual(frappe.db.get_value(EXPRESS, prompt.name, "booking_attempts"), 1)
        (entry,) = frappe.get_all("Payment Entry", filters={"reference_no": receipt, "docstatus": 1},
                                  fields=["name", "paid_amount"])
        self.assertEqual(flt(entry.paid_amount), 1500)
        self.assertEqual(self._errors("M-Pesa STK booking failed"), [])

    def test_c2b_receipt_is_auto_submitted_for_a_callback_user_with_no_document_rights(self):
        user = self._integration_user("M-Pesa Viewer")  # may not submit registers, nor see the project
        si = frappe.get_doc({
            "doctype": "Sales Invoice", "company": t.COMPANY, "customer": self.customer, "posting_date": nowdate(),
            "due_date": nowdate(), "currency": "KES", "selling_price_list": t.PRICE_LIST, "project": self.project,
            "items": [{"item_code": t.ITEM, "qty": 1, "rate": 700}]})
        si.insert()
        si.submit()
        self.assertFalse(frappe.has_permission("Sales Invoice", "read", doc=si, user=user))
        before = frappe.db.get_value("Mpesa Settings", t.SETTINGS, "auto_reconcile_c2b")
        frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_reconcile_c2b", 1)
        try:
            frappe.set_user(user)  # delayed_insert_c2b runs as the Integration User
            doc = frappe.get_doc({"doctype": REGISTER, **t.confirmation_payload(700, billref=self.customer)})
            doc.insert(ignore_permissions=True)
            self.assertEqual(frappe.session.user, user)
        finally:
            frappe.set_user("Administrator")
            frappe.db.set_value("Mpesa Settings", t.SETTINGS, "auto_reconcile_c2b", before)
        self.assertEqual(frappe.db.get_value(REGISTER, doc.name, "docstatus"), 1, self._errors("C2B Auto-submit Error"))
        self.assertTrue(frappe.db.get_value(REGISTER, doc.name, "payment_entry"))
        self.assertEqual(self._errors("C2B Auto-submit Error"), [])
