"""The petty cash phone (money_out.petty_cash) against the real database (ERPNext and HRMS, v15 and v16).

A company phone of the tests' own, held by an employee with a login (who records spends) and approved by an
Accounts Manager who did not enter it. It is topped up from the shortcode TESTB2BDOCS by a party-less B2C payout
(money_out.partyless): an Accounts Manager prepares it with Top Up by M-Pesa, a named M-Pesa Payer sends it with
Send via M-Pesa (send_confirm.send_for_test), its job is run with the B2C connector replaced, and the Result is
written the way the B2C callback writes it. The paid top-up is booked as one Journal Entry (petty cash debited,
the shortcode's GL credited, the receipt as Cheque No) and its statement line is reconciled with it. Spends are
recorded on a Purchase Order and a Purchase Invoice from SMS texts in Safaricom's format, dated a little before
now. Same rules as test_b2b_documents: commits made by the code under test replaced, each test rolled back.
Unit coverage: tests/unit/test_r45_petty_cash.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, add_to_date, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import attention, petty_cash, send_confirm
from mpsa_suite.money_out.test_b2b_documents import _buying_price_list, _settings
from mpsa_suite.money_out.test_employee_documents import _account

SETTINGS = "TESTB2BDOCS"
PHONE, TOP_UP = petty_cash.PHONE, petty_cash.TOP_UP
DISB, ROW, REQ = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
PE, JE, BT, PO, PI = "Payment Entry", "Journal Entry", "Bank Transaction", "Purchase Order", "Purchase Invoice"
NUMBER = "254722000919"  # a Safaricom number of the tests' own
HOLDER, PREPARER, APPROVER = "_t_pc_holder@example.com", "_t_pc_preparer@example.com", "_t_pc_approver@example.com"
PAYER, ACCOUNTS, BUYER = "_t_pc_payer@example.com", "_t_pc_accounts@example.com", "_t_pc_buyer@example.com"
SUPPLIER = "_T Petty Cash Supplier"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1)


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


def _employee(first_name: str, user: str) -> str:
    name = frappe.db.get_value("Employee", {"first_name": first_name, "company": t.COMPANY}, "name")
    if not name:
        if not frappe.db.exists("Gender", "Female"):
            frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert()
        name = frappe.get_doc({"doctype": "Employee", "first_name": first_name, "gender": "Female",
                               "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "company": t.COMPANY,
                               "status": "Active"}).insert().name
    frappe.db.set_value("Employee", name, "user_id", user)
    return name


def _supplier() -> str:
    name = frappe.db.get_value("Supplier", {"supplier_name": SUPPLIER}, "name")
    if not name:
        group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
        name = frappe.get_doc({"doctype": "Supplier", "supplier_name": SUPPLIER, "supplier_group": group}).insert().name
    return name


def _sms(receipt, amount, to, balance, cost=0.0, minutes_ago=30) -> str:
    """An M-Pesa confirmation as Safaricom words it, dated ``minutes_ago`` before now."""
    when = add_to_date(now_datetime(), minutes=-minutes_ago)
    verb = "paid to" if to.endswith(".") else "sent to"
    return (f"{receipt} Confirmed. Ksh{amount:,.2f} {verb} {to} on {when:%d/%m/%y} at {when:%I:%M %p}. New M-PESA "
            f"balance is Ksh{balance:,.2f}. Transaction cost, Ksh{cost:,.2f}. Amount you can transact within the day "
            "is 498,500.00.")


class TestPettyCash(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        _buying_price_list()
        frappe.db.set_value("Item", t.ITEM, "is_purchase_item", 1)
        t.named_user(HOLDER, "Purchase User")
        t.named_user(PREPARER, "Accounts Manager")
        t.named_user(APPROVER, "Accounts Manager")
        t.named_user(PAYER, "M-Pesa Payer")
        t.named_user(ACCOUNTS, "Accounts User")
        t.named_user(BUYER, "Purchase User")
        cls.holder = _employee("_T Petty Holder", HOLDER)
        cls.cash = _account("_T Petty Cash Phone 0722", "Asset", "Balance Sheet", "Cash")
        cls.charges = _account("_T Petty Cash Charges", "Expense", "Profit and Loss")
        cls.cost_center = frappe.db.get_value("Company", t.COMPANY, "cost_center")
        cls.supplier = _supplier()
        # phones an interrupted run committed would hold the number or the account
        for name in set(frappe.get_all(PHONE, filters={"phone": NUMBER}, pluck="name")
                        + frappe.get_all(PHONE, filters={"petty_cash_account": cls.cash}, pluck="name")):
            frappe.db.delete(TOP_UP, {"parent": name, "parenttype": PHONE})
            frappe.db.delete(PHONE, {"name": name})
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.tag = frappe.generate_hash(length=6).upper()
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        t.forget_roles(HOLDER, PAYER, BUYER)
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")

    # ------------------------------------------------------------ helpers
    def _receipt(self, n=1) -> str:
        return f"U{self.tag}{n:03d}"[:10]

    def _draft(self, **values):
        with _as(PREPARER):
            doc = frappe.get_doc({
                "doctype": PHONE, "company": t.COMPANY, "phone": "0722 000 919", "expected_name": "PETTY HOLDER",
                "holder": self.holder, "petty_cash_account": self.cash, "default_cost_center": self.cost_center,
                "charges_account": self.charges, "top_up_limit": 10000, "float_ceiling": 15000, **values})
            doc.insert()
        return doc

    def _phone(self, **values) -> str:
        doc = self._draft(**values)
        with _as(APPROVER):
            petty_cash.approve(doc.name)
        return doc.name

    def _send(self, name, results, user=PAYER) -> list:
        """Send via M-Pesa on the payout as ``user``, its jobs run (the connector replaced), and each
        Result written as the B2C callback writes it: ``results`` [(status, receipt, receiver)]."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb

        with patch("frappe.enqueue") as enqueue, _as(user):
            send_confirm.send_for_test(frappe.get_doc(DISB, name))
        jobs = [c for c in enqueue.call_args_list if c.args and c.args[0].endswith("create_b2c_request")]

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
        for request, (status, receipt, receiver) in zip(requests, results):
            self._result(request, status, receipt, receiver)
        return requests

    def _result(self, request, status, receipt=None, receiver=None):
        """The Result as the B2C callback writes it. A top-up is paid two hours ago: before the
        spends' SMS texts, which are dated within the last hour."""
        from mpsa_suite.services import b2c_response_service

        values = {"status": status, "result_code": "0" if status == "Paid" else "2001",
                  "result_desc": "ok" if status == "Paid" else "The initiator information is invalid."}
        if status == "Paid":
            completed = add_to_date(now_datetime(), hours=-2)
            values.update(transaction_id=receipt, transaction_completed_datetime=completed,
                          receiver_public_name=receiver or f"{NUMBER} - PETTY HOLDER", utility_acct_avlbl_funds=400000)
        frappe.db.set_value(REQ, request, values)
        with patch("frappe.enqueue"):
            b2c_response_service.update_b2c_reference_status(request, enqueue_next=True)

    def _topped_up(self, phone, amount, receipt, receiver=None) -> str:
        """A top-up prepared, sent and Paid: its Journal Entry."""
        with _as(PREPARER):
            out = petty_cash.top_up(phone, amount, mpesa_settings=SETTINGS)
        self._send(out["name"], [("Paid", receipt, receiver)])
        return frappe.db.get_value(TOP_UP, out["top_up"], "journal_entry")

    def _items(self, amount, **row):
        return [{"item_code": t.ITEM, "qty": 1, "rate": amount, "uom": "Nos", "conversion_factor": 1, **row}]

    def _order(self, amount):
        schedule = add_days(nowdate(), 3)
        po = frappe.get_doc({
            "doctype": PO, "supplier": self.supplier, "company": t.COMPANY, "currency": "KES", "conversion_rate": 1,
            "buying_price_list": "_Test M-Pesa KES Buying", "plc_conversion_rate": 1, "transaction_date": nowdate(),
            "schedule_date": schedule, "items": self._items(amount, schedule_date=schedule)})
        po.insert()
        po.submit()
        return po.name

    def _invoice(self, amount):
        pi = frappe.get_doc({
            "doctype": PI, "supplier": self.supplier, "company": t.COMPANY, "posting_date": nowdate(),
            "due_date": nowdate(), "currency": "KES", "conversion_rate": 1,
            "buying_price_list": "_Test M-Pesa KES Buying",
            "plc_conversion_rate": 1, "bill_no": f"PC-{self.tag}-{frappe.generate_hash(length=4)}",
            "items": self._items(amount)})
        pi.insert()
        pi.submit()
        return pi.name

    def _lines(self, journal) -> list:
        return sorted((r.account, flt(r.debit_in_account_currency), flt(r.credit_in_account_currency))
                      for r in frappe.get_all("Journal Entry Account", filters={"parent": journal},
                                              fields=["account", "debit_in_account_currency",
                                                      "credit_in_account_currency"]))

    # ------------------------------------------------------------ set up and approve
    def test_a_phone_is_set_up_and_approved_by_another_manager_then_its_number_is_locked(self):
        # the Name on M-Pesa (what M-Pesa shows for the number) is needed: the first top-up is checked against it
        self.assertRaises(frappe.ValidationError, self._draft, expected_name=None)
        doc = self._draft()
        self.assertEqual((doc.phone, doc.holder_user, doc.entered_by, doc.docstatus), (NUMBER, HOLDER, PREPARER, 0))
        with _as(PREPARER):  # entered it
            self.assertRaisesRegex(frappe.PermissionError, "A different person must approve", petty_cash.approve,
                                   doc.name)
        with _as(APPROVER):  # a plain Submit
            self.assertRaisesRegex(frappe.PermissionError, "approved with the Approve button",
                                   frappe.get_doc(PHONE, doc.name).submit)
            petty_cash.approve(doc.name)
        approved = frappe.get_doc(PHONE, doc.name)
        self.assertEqual((approved.docstatus, approved.approved_by), (1, APPROVER))
        approved.phone = "254722000918"
        with _as(APPROVER):
            self.assertRaises(frappe.ValidationError, approved.save)  # locked: a cancel and amend, approved again
        # one live phone per number
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "already: one phone per number", frappe.get_doc({
                "doctype": PHONE, "company": t.COMPANY, "phone": NUMBER, "expected_name": "PETTY HOLDER",
                "holder": self.holder, "petty_cash_account": self.cash, "top_up_limit": 1000,
                "float_ceiling": 2000}).insert)

    # ------------------------------------------------------------ top up
    def test_a_top_up_is_sent_paid_booked_and_its_statement_line_reconciled(self):
        phone = self._phone()
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "more than PCP-|top-up limit", petty_cash.top_up, phone,
                                   12000, mpesa_settings=SETTINGS)
            out = petty_cash.top_up(phone, 6000, mpesa_settings=SETTINGS)
        row = frappe.db.get_value(ROW, {"parent": out["name"]}, ["reference_doctype", "reference_name", "reference_row",
                                                                 "partyb", "party", "allocated_amount"], as_dict=True)
        self.assertEqual((row.reference_doctype, row.reference_name, row.reference_row, row.partyb, row.party,
                          flt(row.allocated_amount)), (PHONE, phone, out["top_up"], NUMBER, None, 6000))
        # the holder never sends a top-up of their own phone
        t.named_user(HOLDER, "M-Pesa Payer")
        with _as(HOLDER), patch("frappe.enqueue"):
            self.assertRaisesRegex((frappe.ValidationError, frappe.PermissionError), "yourself|You hold",
                                   send_confirm.send_for_test, frappe.get_doc(DISB, out["name"]))
        self.assertEqual(frappe.db.get_value(DISB, out["name"], "docstatus"), 0)
        receipt = self._receipt()
        (request,) = self._send(out["name"], [("Paid", receipt, None)])
        top_up = frappe.db.get_value(TOP_UP, out["top_up"], ["status", "receipt", "journal_entry"], as_dict=True)
        self.assertEqual((top_up.status, top_up.receipt), ("Paid", receipt))
        journal = frappe.db.get_value(JE, top_up.journal_entry, ["docstatus", "cheque_no", "voucher_type"],
                                      as_dict=True)
        self.assertEqual((journal.docstatus, journal.cheque_no, journal.voucher_type), (1, receipt, "Bank Entry"))
        self.assertEqual(self._lines(top_up.journal_entry), sorted([(self.cash, 6000, 0), (self.gl, 0, 6000)]))
        self.assertEqual(frappe.db.get_value(REQ, request, "accounting_booked"), 1)
        self.assertEqual(petty_cash.book_balance(self.cash), 6000)
        # the statement line of the payout reconciles one to one with the top-up's journal
        line = frappe.db.get_value(BT, {"reference_number": receipt, "docstatus": 1}, ["name", "status"], as_dict=True)
        self.assertEqual(line.status, "Reconciled")
        self.assertEqual(frappe.get_all("Bank Transaction Payments", filters={"parent": line.name},
                                        pluck="payment_entry"), [top_up.journal_entry])
        # the name M-Pesa gave the number, checked on the first top-up
        self.assertEqual(frappe.db.get_value(PHONE, phone, ["mpesa_name", "name_mismatch"]), ("PETTY HOLDER", 0))
        # the ceiling: 6,000 held of 15,000, at most 9,000 more
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "top it up by at most KES 9,000", petty_cash.top_up, phone,
                                   10000, mpesa_settings=SETTINGS)

    def test_a_failed_top_up_then_retry(self):
        from mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request import b2c_disbursement_request as req_module

        phone = self._phone()
        with _as(PREPARER):
            out = petty_cash.top_up(phone, 3000, mpesa_settings=SETTINGS)
        (request,) = self._send(out["name"], [("Failed", None, None)])
        self.assertEqual(frappe.db.get_value(TOP_UP, out["top_up"], "status"), "Failed")
        self.assertEqual(petty_cash.in_flight(phone), 0)  # failed: nothing on its way
        with _as(PAYER), patch.object(req_module, "make_b2c_payment_request") as sent:
            frappe.get_doc(REQ, request).retry_failed_payment()
        sent.assert_called_once_with(request)
        # sent again: on its way by its request's state, so the ceiling counts it and no other top-up is prepared
        self.assertEqual(petty_cash.in_flight(phone), 3000)
        self.assertEqual(frappe.db.get_value(TOP_UP, out["top_up"], "status"), "Sending")
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "is Sending .*wait for M-Pesa's answer", petty_cash.top_up,
                                   phone, 1000, mpesa_settings=SETTINGS)
        receipt = self._receipt(2)
        self._result(request, "Paid", receipt)
        top_up = frappe.db.get_value(TOP_UP, out["top_up"], ["status", "receipt", "journal_entry"], as_dict=True)
        self.assertEqual((top_up.status, top_up.receipt), ("Paid", receipt))
        self.assertEqual(frappe.db.get_value(JE, top_up.journal_entry, ["docstatus", "cheque_no"]), (1, receipt))

    def test_a_name_that_does_not_match_holds_the_phone_until_a_manager_confirms_it(self):
        phone = self._phone()
        self._topped_up(phone, 2000, self._receipt(), receiver=f"{NUMBER} - SOMEONE ELSE")
        self.assertEqual(frappe.db.get_value(PHONE, phone, "name_mismatch"), 1)
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "is blocked", petty_cash.top_up, phone, 1000,
                                   mpesa_settings=SETTINGS)
        with _as(APPROVER):
            petty_cash.confirm_name(phone, 1)
        self.assertEqual(frappe.db.get_value(PHONE, phone, ["name_mismatch", "mpesa_name"]), (0, "SOMEONE ELSE"))

    # ------------------------------------------------------------ the holder
    def test_the_holder_changes_only_by_another_manager_and_its_user_follows_the_employee(self):
        phone = self._phone()
        doc = frappe.get_doc(PHONE, phone)
        doc.holder_user = ACCOUNTS  # typed: never kept, it is the holder's Employee's User ID
        with _as(APPROVER):
            doc.save()
        self.assertEqual(frappe.db.get_value(PHONE, phone, "holder_user"), HOLDER)
        other = _employee("_T Petty Second Holder", BUYER)
        # the holder never changes the holder, even as a manager
        t.named_user(HOLDER, "Accounts Manager")
        doc = frappe.get_doc(PHONE, phone)
        doc.holder = other
        with _as(HOLDER):
            self.assertRaisesRegex(frappe.PermissionError, "You hold this phone", doc.save)
        # another manager does, with a Comment
        doc = frappe.get_doc(PHONE, phone)
        doc.holder = other
        with _as(APPROVER):
            doc.save()
        self.assertEqual(frappe.db.get_value(PHONE, phone, ["holder", "holder_user"]), (other, BUYER))
        comments = frappe.get_all("Comment", filters={"reference_doctype": PHONE, "reference_name": phone,
                                                      "comment_type": "Comment"}, pluck="content")
        self.assertTrue(any(f"{APPROVER} changed Holder" in c for c in comments), comments)
        # nor does the new holder change it back
        t.named_user(BUYER, "Accounts Manager")
        doc = frappe.get_doc(PHONE, phone)
        doc.holder = self.holder
        with _as(BUYER):
            self.assertRaisesRegex(frappe.PermissionError, "You hold this phone", doc.save)

    # ------------------------------------------------------------ spends
    def test_spends_on_an_order_and_an_invoice_from_their_sms(self):
        phone = self._phone()
        self._topped_up(phone, 5000, self._receipt())
        po, pi = self._order(3000), self._invoice(2000)
        till = _sms(self._receipt(2), 1500, "_T PETTY CASH SUPPLIER.", 3500, minutes_ago=40)
        with _as(HOLDER):  # the holder records it from the order
            out = petty_cash.record_spend(PO, po, sms=till)
        entry = frappe.get_doc(PE, out["payment_entry"])
        self.assertEqual((entry.docstatus, entry.payment_type, entry.party, entry.paid_from, flt(entry.paid_amount),
                          entry.reference_no, entry.mpesa_petty_cash_phone, flt(entry.mpesa_sms_balance)),
                         (1, "Pay", self.supplier, self.cash, 1500, self._receipt(2), phone, 3500))
        self.assertEqual(entry.mpesa_sms_has_balance, 1)  # its SMS gave the balance (Frappe stores none as 0)
        self.assertEqual([(r.reference_doctype, r.reference_name, flt(r.allocated_amount)) for r in entry.references],
                         [(PO, po, 1500)])
        self.assertEqual(flt(frappe.db.get_value(PO, po, "advance_paid")), 1500)  # the order's advance
        paybill = _sms(self._receipt(3), 2000, "KPLC PREPAID for account 12345", 1477, cost=23, minutes_ago=20)
        with _as(ACCOUNTS):  # an Accounts User records the invoice's
            out = petty_cash.record_spend(PI, pi, phone=phone, sms=paybill)
        entry = frappe.get_doc(PE, out["payment_entry"])
        self.assertEqual((flt(entry.paid_amount), [(d.account, flt(d.amount)) for d in entry.deductions]),
                         (2023, [(self.charges, 23)]))
        self.assertEqual([(r.reference_doctype, r.reference_name, flt(r.allocated_amount)) for r in entry.references],
                         [(PI, pi, 2000)])
        # the phone's account pays the amount and its charge; the charge is booked on the charges account
        ledger = {(g.account, flt(g.debit), flt(g.credit)) for g in frappe.get_all(
            "GL Entry", filters={"voucher_type": PE, "voucher_no": entry.name, "is_cancelled": 0},
            fields=["account", "debit", "credit"])}
        self.assertTrue({(self.cash, 0, 2023), (self.charges, 23, 0)} <= ledger, ledger)
        self.assertEqual(frappe.db.get_value(PI, pi, ["outstanding_amount", "status"]), (0, "Paid"))
        # the book balance after the spends: 5,000 less 1,500 and 2,023, what the last SMS says
        self.assertEqual(petty_cash.book_balance(self.cash), 1477)
        status = petty_cash.sms_status(frappe.db.get_value(PHONE, phone, list(petty_cash.FIELDS), as_dict=True))
        self.assertEqual((status.book, status.last_sms.balance, status.difference), (1477, 1477, 0))
        with _as(HOLDER):
            listed = petty_cash.document_state(PO, po)["spends"]
        self.assertEqual([(s["receipt"], s["phone"], s["amount"]) for s in listed], [(self._receipt(2), phone, 1500)])
        # an SMS whose balance shows less than the books: a spend not recorded (or a typo), flagged
        po2 = self._order(1000)
        missed = _sms(self._receipt(4), 400, "_T PETTY CASH SUPPLIER.", 577, minutes_ago=5)  # 500 more was spent
        with _as(HOLDER):
            petty_cash.record_spend(PO, po2, sms=missed)
        status = petty_cash.sms_status(frappe.db.get_value(PHONE, phone, list(petty_cash.FIELDS), as_dict=True))
        self.assertEqual((status.book, status.expected, status.difference), (1077, 577, 500))
        sections = dict((entry[0], entry[1]) for entry in attention.collect())
        self.assertTrue(any(line.startswith(phone) and "KES 500 more than its last SMS says" in line
                            for line in sections.get("Petty cash phones", [])), sections)

    def test_a_spend_is_refused_twice_above_what_is_owed_or_held_and_for_others(self):
        phone = self._phone()
        self._topped_up(phone, 2000, self._receipt())
        po = self._order(3000)
        with _as(BUYER):  # neither the holder nor an Accounts role
            self.assertRaisesRegex(frappe.PermissionError, "needs one of the roles", petty_cash.record_spend, PO, po,
                                   phone=phone, sms=_sms(self._receipt(2), 500, "SHELL KILIMANI.", 1500))
        with _as(HOLDER):
            self.assertRaisesRegex(frappe.ValidationError, "book balance is KES 2,000, less than KES 2,500",
                                   petty_cash.record_spend, PO, po, sms=_sms(self._receipt(2), 2500, "SHELL KILIMANI.",
                                                                             0))
            self.assertRaisesRegex(frappe.ValidationError, "is more than .* still owes", petty_cash.record_spend, PO,
                                   po, sms=_sms(self._receipt(2), 3500, "SHELL KILIMANI.", 0))
            sms = _sms(self._receipt(2), 500, "SHELL KILIMANI.", 1500)
            self.assertRaisesRegex(frappe.ValidationError, r"transaction charge typed \(50.0\) is not the SMS's",
                                   petty_cash.record_spend, PO, po, sms=sms, charge=50)  # the SMS says 0.00
            petty_cash.record_spend(PO, po, sms=sms)
            self.assertRaisesRegex(frappe.ValidationError, "is recorded once", petty_cash.record_spend, PO, po, sms=sms)
            self.assertRaisesRegex(frappe.ValidationError, "This is not an M-Pesa payment SMS", petty_cash.record_spend,
                                   PO, po, sms="Please pay Shell at the till")
