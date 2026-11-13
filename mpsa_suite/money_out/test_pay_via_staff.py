"""Pay via Staff, strict (money_out.pay_via_staff), against the real database (ERPNext and HRMS, v15 and v16).

Two M-Pesa Managers are listed under M-Pesa Suite Settings' Who May Pay via Staff; a third M-Pesa Manager and an
M-Pesa Payer are not. A staff member has an approved Employee M-Pesa Account phone (written as approved: the
approval has its own tests). Pay via Staff on a submitted Purchase Order makes a submitted Employee Advance and its
draft B2C Payment Disbursement; the payout is sent with Send via M-Pesa (send_confirm.send_for_test), its job run
with the B2C connector replaced and the Result written as the B2C callback writes it, so the advance is booked
paid by money_out.employee_documents. The supplier's Purchase Invoice made from the order then settles the advance
with one Journal Entry on its submit; an advance made on an unpaid invoice is settled with Settle Staff Advances.
Same rules as test_b2b_documents: commits made by the code under test replaced, each test rolled back. Unit
coverage: tests/unit/test_r45_pay_via_staff.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import pay_via_staff, send_confirm
from mpsa_suite.money_out.test_b2b_documents import _buying_price_list, _settings
from mpsa_suite.money_out.test_employee_documents import _account

SETTINGS = "TESTB2BDOCS"
SUITE = "M-Pesa Suite Settings"
EA, PO, PI, JE = "Employee Advance", "Purchase Order", "Purchase Invoice", "Journal Entry"
DISB, ROW, REQ = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
EMPACC = "Employee M-Pesa Account"
OWNER, JANKI = "_t_pvs_owner@example.com", "_t_pvs_janki@example.com"
OUTSIDER, PAYER = "_t_pvs_outsider@example.com", "_t_pvs_payer@example.com"
PHONE = "254722000929"
SUPPLIER = "_T Pay via Staff Supplier"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1, employee_account_hold_hours=0)


def hrms() -> bool:
    return "hrms" in frappe.get_installed_apps()


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


def _employee(first_name: str) -> str:
    name = frappe.db.get_value("Employee", {"first_name": first_name, "company": t.COMPANY}, "name")
    if not name:
        if not frappe.db.exists("Gender", "Female"):
            frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert()
        name = frappe.get_doc({"doctype": "Employee", "first_name": first_name, "gender": "Female",
                               "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "company": t.COMPANY,
                               "status": "Active"}).insert().name
    return name


def _approved_phone(employee: str, name: str) -> str:
    """The staff member's Employee M-Pesa Account phone, written as approved (its approval has its own tests)."""
    if frappe.db.exists(EMPACC, name):
        return name
    doc = frappe.get_doc({
        "doctype": EMPACC, "employee": employee, "account_type": "Phone (B2C)", "phone": PHONE,
        "expected_name": "PVS STAFF", "is_default": 1, "docstatus": 1, "proof": "/private/files/_t_pvs_proof.pdf",
        "approved_by": "Administrator", "approved_on": now_datetime(), "active_from": add_days(now_datetime(), -1),
        "creation": now_datetime(), "modified": now_datetime(), "owner": "Administrator",
        "destination_key": f"B2C:{PHONE}"})
    doc.name = name
    doc.db_insert()
    return name


def _supplier() -> str:
    name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER}, "name")
    if not name:
        group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        name = frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER, "supplier_group": group}).insert().name
    return name


class TestPayViaStaff(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not hrms():
            return
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        _buying_price_list()
        frappe.db.set_value("Item", t.ITEM, "is_purchase_item", 1)
        for user in (OWNER, JANKI, OUTSIDER):
            t.named_user(user, "M-Pesa Manager", "Purchase User", "Accounts User")
        t.named_user(PAYER, "M-Pesa Payer")
        cls.advances = _account("_T PVS Employee Advances", "Asset", "Balance Sheet", "Receivable")
        # HRMS v15 fetches an advance's account from the company on every save (test_employee_documents)
        frappe.db.set_value("Company", t.COMPANY, "default_employee_advance_account", cls.advances)
        cls.staff = _employee("_T PVS Staff")
        cls.account = _approved_phone(cls.staff, "_T-MEA-PVS-1")
        cls.supplier = _supplier()
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        if not hrms():
            self.skipTest("HRMS is not installed")
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()
        suite = frappe.get_single(SUITE)
        suite.set("pay_via_staff_users", [{"user": OWNER}, {"user": JANKI}])
        suite.flags.ignore_permissions = True
        suite.save()
        frappe.clear_document_cache(SUITE, SUITE)

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache(SUITE, SUITE)
        t.forget_roles(OWNER, JANKI, OUTSIDER, PAYER)

    # ------------------------------------------------------------ helpers
    def _items(self, amount, **row):
        return [{"item_code": t.ITEM, "qty": 1, "rate": amount, "uom": "Nos", "conversion_factor": 1, **row}]

    def _order(self, amount) -> str:
        schedule = add_days(nowdate(), 3)
        po = frappe.get_doc({
            "doctype": PO, "supplier": self.supplier, "company": t.COMPANY, "currency": "KES", "conversion_rate": 1,
            "buying_price_list": "_Test M-Pesa KES Buying", "plc_conversion_rate": 1, "transaction_date": nowdate(),
            "schedule_date": schedule, "items": self._items(amount, schedule_date=schedule)})
        po.insert()
        po.submit()
        return po.name

    def _invoice(self, amount, submit=True):
        pi = frappe.get_doc({
            "doctype": PI, "supplier": self.supplier, "company": t.COMPANY, "posting_date": nowdate(),
            "due_date": nowdate(), "currency": "KES", "conversion_rate": 1,
            "buying_price_list": "_Test M-Pesa KES Buying",
            "plc_conversion_rate": 1, "bill_no": f"PVS-{frappe.generate_hash(length=8)}", "items": self._items(amount)})
        pi.insert()
        if submit:
            pi.submit()
        return pi

    def _invoice_from(self, po: str):
        from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice

        pi = make_purchase_invoice(po)
        pi.bill_no = f"PVS-{frappe.generate_hash(length=8)}"
        pi.posting_date = pi.due_date = nowdate()
        pi.set_posting_time = 1
        pi.insert()
        pi.submit()
        return pi

    def _send(self, name, user) -> list:
        """Send via M-Pesa as ``user``; the jobs it queues, kept."""
        with _as(user), patch("frappe.enqueue") as enqueue:
            send_confirm.send_for_test(frappe.get_doc(DISB, name))
        return [c for c in enqueue.call_args_list if c.args and c.args[0].endswith("create_b2c_request")]

    def _paid(self, jobs, user) -> list:
        """The send's jobs run (Daraja's ack replaced) and each payout's Result written and recorded."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb
        from mpsa_suite.services import b2c_response_service

        def ack(doc, callback_url):
            frappe.db.set_value(REQ, doc.name, {"status": "Initiated", "conversation_id": f"AG_{doc.name}"})
            return {"ResponseCode": "0"}

        connector = type("Connector", (), {"send_b2c_request": staticmethod(ack)})()
        requests = []
        with _as(user), patch("mpsa_suite.services.b2c_request_service.get_b2c_connector", return_value=connector), \
                patch("frappe.enqueue"):
            for job in jobs:
                disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
                requests.append(frappe.db.get_value(ROW, job.kwargs["ref"].name, "b2c_disbursement_request"))
        for request in requests:
            frappe.db.set_value(REQ, request, {
                "status": "Paid", "result_code": "0", "result_desc": "ok",
                "transaction_id": "T" + frappe.generate_hash(length=9).upper(),
                "transaction_completed_datetime": now_datetime(), "receiver_public_name": f"{PHONE} - PVS STAFF",
                "utility_acct_avlbl_funds": 400000})
            with patch("frappe.enqueue"):
                b2c_response_service.update_b2c_reference_status(request, enqueue_next=True)
        return requests

    def _advance_paid(self, doctype, name, amount) -> dict:
        """Pay via Staff as the owner, sent by Janki (maker-checker: who prepared does not send), paid."""
        with _as(OWNER):
            out = pay_via_staff.make_advance(doctype, name, self.staff, amount, mpesa_settings=SETTINGS)
        self._paid(self._send(out["disbursement"], JANKI), JANKI)
        return out

    def _journal(self, invoice) -> str | None:
        return frappe.db.get_value("Journal Entry Account", {"reference_type": PI, "reference_name": invoice,
                                                             "docstatus": 1}, "parent")

    # ------------------------------------------------------------ who may
    def test_pay_via_staff_is_refused_for_a_user_not_on_the_list_and_allowed_for_a_listed_one(self):
        po = self._order(5000)
        with _as(OUTSIDER):
            self.assertFalse(pay_via_staff.document_state(PO, po)["allowed"])  # the button shows to the listed only
            self.assertRaisesRegex(frappe.PermissionError, "Who May Pay via Staff", pay_via_staff.make_advance, PO, po,
                                   self.staff, 2000, mpesa_settings=SETTINGS)
        self.assertFalse(frappe.db.exists(EA, {"mpesa_purchase_order": po}))
        with _as(OWNER):
            self.assertEqual(pay_via_staff.document_state(PO, po)["allowed"], True)
            out = pay_via_staff.make_advance(PO, po, self.staff, 2000, mpesa_settings=SETTINGS)
        advance = frappe.db.get_value(EA, out["employee_advance"], ["docstatus", "employee", "advance_amount",
                                                                    "mpesa_supplier", "mpesa_purchase_order"],
                                      as_dict=True)
        self.assertEqual((advance.docstatus, advance.employee, flt(advance.advance_amount), advance.mpesa_supplier,
                          advance.mpesa_purchase_order), (1, self.staff, 2000, self.supplier, po))
        row = frappe.db.get_value(ROW, {"parent": out["disbursement"]}, ["reference_doctype", "reference_name",
                                                                         "partyb", "payee_account"], as_dict=True)
        self.assertEqual((row.reference_doctype, row.reference_name, row.partyb, row.payee_account),
                         (EA, out["employee_advance"], PHONE, self.account))
        # the send: refused for a payer not on the list, made by a listed one
        self.assertRaisesRegex(frappe.PermissionError, "may send a Pay via Staff advance|may pay a Pay via Staff",
                               self._send, out["disbursement"], PAYER)
        self.assertEqual(frappe.db.get_value(DISB, out["disbursement"], "docstatus"), 0)
        self._paid(self._send(out["disbursement"], JANKI), JANKI)
        self.assertEqual(flt(frappe.db.get_value(EA, out["employee_advance"], "paid_amount")), 2000)

    def test_even_a_listed_user_never_pays_an_advance_to_themselves(self):
        po = self._order(3000)
        frappe.db.set_value("Employee", self.staff, "user_id", OWNER)
        with _as(OWNER):
            self.assertRaisesRegex(frappe.PermissionError, "You may not pay an advance to yourself",
                                   pay_via_staff.make_advance, PO, po, self.staff, 1000, mpesa_settings=SETTINGS)
        frappe.db.set_value("Employee", self.staff, "user_id", None)
        with _as(JANKI):
            out = pay_via_staff.make_advance(PO, po, self.staff, 1000, mpesa_settings=SETTINGS)
        frappe.db.set_value("Employee", self.staff, "user_id", OWNER)
        with _as(OWNER), patch("frappe.enqueue"):  # nor sends one to themselves
            self.assertRaises((frappe.PermissionError, frappe.ValidationError), send_confirm.send_for_test,
                              frappe.get_doc(DISB, out["disbursement"]))

    # ------------------------------------------------------------ every other payout sees the advance
    def test_a_staff_advance_is_taken_off_what_the_order_owes_for_every_other_payout(self):
        from mpsa_suite.money_out import b2b, owed

        po = self._order(5000)
        with _as(OWNER):
            out = pay_via_staff.make_advance(PO, po, self.staff, 3000, mpesa_settings=SETTINGS)
        found = owed.left_to_pay(PO, po)  # B2B, the schedule and a petty cash spend read this
        self.assertEqual((flt(found.owed), flt(found.left)), (5000, 2000))
        self.assertIn(f"Pay via Staff advance {out['employee_advance']}", owed.explain(po, found))
        # B2C's own guards (the send, a retry) take off the same planned lines, as a locking read
        self.assertEqual([p.name for p in owed.planned_lines(PO, po, for_update=True)], [out["employee_advance"]])
        request = frappe._dict(name="_T-PVS-B2B", amount=2500, supplier=self.supplier, company=t.COMPANY)
        self.assertRaisesRegex(frappe.ValidationError, "more than is left to pay", b2b._check_document, request, PO,
                               po)
        # a second staff advance: only what is left
        with _as(JANKI):
            self.assertRaisesRegex(frappe.ValidationError, r"has left for a staff advance \(KES 2,000\)",
                                   pay_via_staff.make_advance, PO, po, self.staff, 2500, mpesa_settings=SETTINGS)

    def test_no_staff_advance_for_a_supplier_on_hold(self):
        po = self._order(2000)
        frappe.db.set_value("Supplier", self.supplier, {"on_hold": 1, "hold_type": "All", "release_date": None})
        with _as(OWNER):
            self.assertFalse(pay_via_staff.document_state(PO, po)["allowed"])
            self.assertRaisesRegex(frappe.ValidationError, "is on hold for payments", pay_via_staff.make_advance, PO,
                                   po, self.staff, 1000, mpesa_settings=SETTINGS)

    # ------------------------------------------------------------ the settlement
    def test_the_invoice_from_the_order_settles_the_staff_advance_in_one_journal(self):
        po = self._order(5000)
        out = self._advance_paid(PO, po, 3000)
        pi = self._invoice_from(po)
        journal = self._journal(pi.name)
        self.assertTrue(journal, "no settlement Journal Entry on the invoice")
        lines = sorted((r.account, r.party_type, r.party, r.reference_type, r.reference_name,
                        flt(r.debit_in_account_currency), flt(r.credit_in_account_currency))
                       for r in frappe.get_all("Journal Entry Account", filters={"parent": journal},
                                               fields=["account", "party_type", "party", "reference_type",
                                                       "reference_name", "debit_in_account_currency",
                                                       "credit_in_account_currency"]))
        self.assertEqual(lines, sorted([
            (pi.credit_to, "Supplier", self.supplier, PI, pi.name, 3000, 0),
            (self.advances, "Employee", self.staff, EA, out["employee_advance"], 0, 3000)]))
        self.assertEqual(frappe.db.get_value(JE, journal, "docstatus"), 1)
        self.assertEqual(flt(frappe.db.get_value(PI, pi.name, "outstanding_amount")), 2000)
        # settled once: nothing more to settle
        with _as(OWNER):
            self.assertRaisesRegex(frappe.ValidationError, "no staff advance to settle", pay_via_staff.settle_now,
                                   pi.name)

    def test_settle_staff_advances_by_hand_settles_an_invoices_own_advance(self):
        pi = self._invoice(2000)
        out = self._advance_paid(PI, pi.name, 2000)
        with _as(OUTSIDER):  # an M-Pesa Manager not listed and no Accounts Manager
            self.assertRaisesRegex(frappe.PermissionError, "needs one of the roles", pay_via_staff.settle_now, pi.name)
        with _as(OWNER):
            state = pay_via_staff.document_state(PI, pi.name)
            self.assertEqual((state["can_settle"], state["to_settle"]), (True, 2000))
            settled = pay_via_staff.settle_now(pi.name)
        self.assertEqual(settled["settled"], [(out["employee_advance"], 2000)])
        self.assertEqual(flt(frappe.db.get_value(PI, pi.name, "outstanding_amount")), 0)
        self.assertEqual(self._journal(pi.name), settled["journal_entry"])
