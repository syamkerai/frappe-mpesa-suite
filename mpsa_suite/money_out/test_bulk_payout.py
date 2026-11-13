"""The M-Pesa Bulk Payout sheet (money_out.bulk_payout) against the real database (ERPNext and HRMS, v15
and v16).

A sheet of casual wages and an employee's overtime over two projects and two cost centers, with two Expense
Claim Types of their own accounts. Named users only: an Accounts User prepares it, a named M-Pesa Manager
submits it (the approval, ticking the new numbers), the preparer presses Pay by M-Pesa, and a named M-Pesa
Payer sends the batch with Send via M-Pesa (send_confirm.send_for_test). Nothing reaches Safaricom: the B2C
connector is replaced, and each Result is handed over the way Safaricom posts it (process_b2c_result, the B2C
callback's own handler), its queued jobs run as the worker runs them. The paid rows are booked in one Journal
Entry (grouped by expense account, cost center and project, credited to the shortcode's GL) once no row waits,
and each payout's statement line is reconciled with its part of it. Same rules as test_claim_payees: the
shortcode TESTB2BDOCS, commits made by the code under test replaced, each test rolled back. Unit coverage:
tests/unit/test_r45_bulk_payout.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import bulk_payout, send_confirm
from mpsa_suite.money_out.test_b2b_documents import _account, _settings

SETTINGS = "TESTB2BDOCS"
SHEET, SROW = bulk_payout.SHEET, bulk_payout.ROW
DISB, ROW, REQ, JE, JEA, BT = ("B2C Payment Disbursement", "B2C Payment Disbursement Reference",
                               "B2C Disbursement Request", "Journal Entry", "Journal Entry Account", "Bank Transaction")
TYPE = "Expense Claim Type"
CASUAL, OVERTIME = "_T Bulk Casual Wages", "_T Bulk Overtime"
PREPARER, PAYER = "_t_bp_preparer@example.com", "_t_bp_payer@example.com"
EMPLOYEE_PHONE = "0722000781"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1)
BULK_SETTINGS = dict(bulk_payout_book_after_hours=2, verify_new_numbers=0)
# the worker's part of a payout's Result: the row and its booking, and the sheet's booking job
JOBS = ("b2c_response_service.update_b2c_reference_status", "bulk_payout.book_waiting")


def _user(email: str, *roles) -> str:
    if not frappe.db.exists("User", email):
        frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0].strip("_"),
                        "send_welcome_email": 0, "user_type": "System User"}).insert(ignore_permissions=True)
    user = frappe.get_doc("User", email)
    missing = [r for r in roles if r not in {row.role for row in user.roles}]
    if missing:
        user.flags.ignore_permissions = True
        user.add_roles(*missing)
    return email


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


def _employee(first_name: str, phone: str) -> str:
    name = frappe.db.get_value("Employee", {"first_name": first_name, "company": t.COMPANY}, "name")
    if not name:
        if not frappe.db.exists("Gender", "Female"):
            frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert()
        name = frappe.get_doc({"doctype": "Employee", "first_name": first_name, "gender": "Female",
                               "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "company": t.COMPANY,
                               "status": "Active", "cell_number": phone}).insert().name
    frappe.db.set_value("Employee", name, "cell_number", phone)
    return name


def _claim_type(name: str, account: str) -> str:
    if not frappe.db.exists(TYPE, name):
        frappe.get_doc({"doctype": TYPE, "expense_type": name,
                        "accounts": [{"company": t.COMPANY, "default_account": account}]}).insert()
    return name


def _cost_center(name: str) -> str:
    existing = frappe.db.get_value("Cost Center", {"cost_center_name": name, "company": t.COMPANY}, "name")
    if existing:
        return existing
    root = frappe.db.get_value("Cost Center", {"company": t.COMPANY, "is_group": 1}, "name", order_by="lft asc")
    return frappe.get_doc({"doctype": "Cost Center", "cost_center_name": name, "parent_cost_center": root,
                           "company": t.COMPANY, "is_group": 0}).insert().name


def _project(name: str) -> str:
    existing = frappe.db.get_value("Project", {"project_name": name}, "name")
    return existing or frappe.get_doc({"doctype": "Project", "project_name": name, "company": t.COMPANY}).insert().name


class TestBulkPayout(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        _user(PREPARER, "Accounts User")
        _user(PAYER, "M-Pesa Payer")
        cls.manager = t.manager()  # approves (submits) the sheets, settles rows, books at once
        cls.employee = _employee("_T Bulk Overtime Worker", EMPLOYEE_PHONE)
        cls.wages = _account("_T Bulk Casual Wages", "Expense", "Profit and Loss")
        cls.overtime = _account("_T Bulk Overtime Pay", "Expense", "Profit and Loss")
        _claim_type(CASUAL, cls.wages)
        _claim_type(OVERTIME, cls.overtime)
        cls.site_a, cls.site_b = _cost_center("_T Bulk Site A"), _cost_center("_T Bulk Site B")
        cls.project_a, cls.project_b = _project("_T Bulk Project A"), _project("_T Bulk Project B")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        suite = frappe.get_single("M-Pesa Suite Settings")
        suite.update(BULK_SETTINGS)
        suite.flags.ignore_permissions = True
        suite.save()
        self._no_commit = patch.object(frappe.db, "commit")
        self._no_commit.start()

    def tearDown(self):
        self._no_commit.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        t.forget_roles(PREPARER, PAYER, self.manager)
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")

    # ------------------------------------------------------------ helpers
    def _rows(self):
        """Casuals and an employee's overtime, over two projects and two cost centers."""
        return [
            {"payee_name": "_T Jane Wanjiku", "phone": "0711000771", "amount": 1500},  # the sheet's defaults
            {"payee_name": "_T Bulk Overtime Worker", "phone": EMPLOYEE_PHONE, "amount": 2000, "expense_type": OVERTIME,
             "cost_center": self.site_b, "project": self.project_b},
            {"payee_name": "_T Mary Akinyi", "phone": "0711000773", "amount": 800, "project": self.project_b},
            {"payee_name": "_T John Kamau", "phone": "0711000774", "amount": 1200, "cost_center": self.site_b},
        ]

    def _draft(self, rows=None, **values):
        with _as(PREPARER):
            doc = frappe.get_doc({"doctype": SHEET, "title": "_T Casuals week 45", "company": t.COMPANY,
                                  "posting_date": nowdate(), "mpesa_settings": SETTINGS, "expense_type": CASUAL,
                                  "cost_center": self.site_a, "project": self.project_a,
                                  "rows": rows if rows is not None else self._rows(), **values})
            doc.insert()
        return doc

    def _approved(self, rows=None, **values):
        doc = self._draft(rows, **values)
        with _as(self.manager):
            doc = frappe.get_doc(SHEET, doc.name)
            doc.names_checked = 1  # the approver ticked that they checked the new numbers (asked at Submit)
            doc.submit()
        return frappe.get_doc(SHEET, doc.name)

    def _pay(self, sheet, method="pay") -> str:
        with _as(PREPARER):
            return getattr(bulk_payout, method)(sheet, b2c_settings=SETTINGS)["b2c"]

    def _drain(self, calls):
        """Run the queued jobs the worker would (JOBS), and the jobs they queue, as Administrator."""
        pending = list(calls)
        while pending:
            call = pending.pop(0)
            method = call.args[0] if call.args else call.kwargs.get("method", "")
            if not isinstance(method, str) or not method.endswith(JOBS):
                continue
            kwargs = {k: v for k, v in call.kwargs.items()
                      if k not in ("queue", "timeout", "enqueue_after_commit", "now", "job_name", "is_async")}
            with patch("frappe.enqueue") as enqueue:
                frappe.get_attr(method)(**kwargs)
            pending += enqueue.call_args_list

    def _send(self, batch) -> dict:
        """Send via M-Pesa on the batch as the payer; its payouts made and sent (the connector replaced).
        Returns {phone: B2C Disbursement Request}."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb

        with patch("frappe.enqueue") as enqueue, _as(PAYER):
            send_confirm.send_for_test(frappe.get_doc(DISB, batch))
        jobs = [c for c in enqueue.call_args_list if c.args and c.args[0].endswith("create_b2c_request")]

        def ack(doc, callback_url):
            frappe.db.set_value(REQ, doc.name, {"status": "Initiated", "conversation_id": f"AG_{doc.name}"})
            return {"ResponseCode": "0"}

        connector = type("Connector", (), {"send_b2c_request": staticmethod(ack)})()
        requests = {}
        with _as(PAYER), patch("mpsa_suite.services.b2c_request_service.get_b2c_connector", return_value=connector), \
                patch("frappe.enqueue"):
            for job in jobs:
                disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
                request = frappe.db.get_value(ROW, job.kwargs["ref"].name, "b2c_disbursement_request")
                requests[frappe.db.get_value(REQ, request, "phone_number")] = request
        return requests

    def _result(self, request, receiver=None, code=0, text="The service request is processed successfully."):
        """The B2C Result as Safaricom posts it (process_b2c_result, the callback's handler), its jobs run."""
        from mpsa_suite.services.b2c_response_service import process_b2c_result

        values = frappe.db.get_value(REQ, request, ["originator_conversation_id", "conversation_id", "amount",
                                                     "phone_number"], as_dict=True)
        receipt = "T" + frappe.generate_hash(length=9).upper()
        result = {"ResultType": 0, "ResultCode": code, "ResultDesc": text,
                  "OriginatorConversationID": values.originator_conversation_id,
                  "ConversationID": values.conversation_id, "TransactionID": receipt}
        if code == 0:
            result["ResultParameters"] = {"ResultParameter": [
                {"Key": "TransactionAmount", "Value": flt(values.amount)},
                {"Key": "TransactionReceipt", "Value": receipt},
                {"Key": "B2CRecipientIsRegisteredCustomer", "Value": "Y"},
                {"Key": "ReceiverPartyPublicName", "Value": f"{values.phone_number} - {receiver}"},
                {"Key": "TransactionCompletedDateTime", "Value": now_datetime().strftime("%d.%m.%Y %H:%M:%S")},
            ]}
        with patch("frappe.enqueue") as enqueue:
            process_b2c_result("Mpesa", result)
        self._drain(enqueue.call_args_list)
        return receipt if code == 0 else None

    def _rows_of(self, sheet) -> list:
        return frappe.get_all(SROW, filters={"parent": sheet, "parenttype": SHEET}, fields=["*"], order_by="idx asc")

    def _journals(self, sheet) -> dict:
        """{journal: [(account, cost center, project, debit, credit)]} of the sheet's submitted Journal Entries."""
        out = {}
        for name in frappe.get_all(JE, filters={"cheque_no": sheet, "docstatus": 1}, pluck="name",
                                   order_by="creation asc"):
            out[name] = sorted(
                (r.account, r.cost_center, r.project or None, flt(r.debit_in_account_currency),
                 flt(r.credit_in_account_currency))
                for r in frappe.get_all(JEA, filters={"parent": name},
                                        fields=["account", "cost_center", "project", "debit_in_account_currency",
                                                "credit_in_account_currency"], order_by="idx asc"))
        return out

    def _paid_all_but_the_last(self, sheet):
        """Pay by M-Pesa and send; rows 1-3 Paid (the employee's under their own name), row 4 Failed."""
        batch = self._pay(sheet)
        requests = self._send(batch)
        receipts = {}
        for phone, receiver in (("254711000771", "_T JANE WANJIKU"), ("254722000781", "_T BULK OVERTIME WORKER"),
                                ("254711000773", "_T MARY AKINYI")):
            receipts[phone] = self._result(requests[phone], receiver)
        self._result(requests["254711000774"], code=2040,
                     text="Credit Party customer type (Unregistered or Registered Customer) can't be supported "
                          "by the service.")
        return batch, requests, receipts

    # ------------------------------------------------------------ the whole round
    def test_a_sheet_is_approved_paid_booked_in_one_journal_and_reconciled(self):
        doc = self._approved()
        rows = self._rows_of(doc.name)
        self.assertEqual([r.phone for r in rows], ["254711000771", "254722000781", "254711000773", "254711000774"])
        # the employee's phone: paid here like anyone, the Employee named for reporting (no party on the payout)
        self.assertEqual((rows[1].employee, rows[1].expense_account, rows[1].cost_center, rows[1].project),
                         (self.employee, self.overtime, self.site_b, self.project_b))
        self.assertEqual([(r.expense_account, r.cost_center, r.project) for r in rows[::2]],
                         [(self.wages, self.site_a, self.project_a), (self.wages, self.site_a, self.project_b)])
        self.assertEqual((doc.status, doc.approved_by, doc.total_amount), ("To Pay", self.manager, 5500))
        batch, requests, receipts = self._paid_all_but_the_last(doc.name)
        refs = frappe.get_all(ROW, filters={"parent": batch}, fields=["party", "party_type", "reference_row"],
                              order_by="idx asc")
        self.assertEqual([(r.party, r.party_type) for r in refs], [(None, None)] * 4)
        rows = self._rows_of(doc.name)
        self.assertEqual([r.status for r in rows], ["Paid", "Paid", "Paid", "Failed"])
        self.assertEqual([r.receipt for r in rows[:3]],
                         [receipts["254711000771"], receipts["254722000781"], receipts["254711000773"]])
        self.assertEqual([r.name_verdict for r in rows[:3]], ["Matches"] * 3)
        # one journal: the paid rows grouped by account, cost center and project, credited to the shortcode's GL
        journals = self._journals(doc.name)
        self.assertEqual(len(journals), 1)
        ((journal, lines),) = journals.items()
        debits = sorted((a, cc, p, d) for a, cc, p, d, _c in lines if d)
        self.assertEqual(debits, sorted([(self.wages, self.site_a, self.project_a, 1500.0),
                                         (self.wages, self.site_a, self.project_b, 800.0),
                                         (self.overtime, self.site_b, self.project_b, 2000.0)]))
        self.assertEqual([(a, c) for a, _cc, _p, _d, c in lines if c], [(self.gl, 4300.0)])
        self.assertEqual([r.journal_entry for r in rows], [journal, journal, journal, None])
        for phone in ("254711000771", "254722000781", "254711000773"):
            self.assertEqual(frappe.db.get_value(REQ, requests[phone], "accounting_booked"), 1, phone)
        # each payout's statement line takes its own part of the journal (partial allocation)
        for phone, amount in (("254711000771", 1500), ("254722000781", 2000), ("254711000773", 800)):
            line = frappe.db.get_value(REQ, requests[phone], "bank_transaction")
            self.assertTrue(line, phone)
            bt = frappe.get_doc(BT, line)
            self.assertEqual(flt(bt.unallocated_amount), 0, phone)
            allocated = [(p.payment_document, p.payment_entry, flt(p.allocated_amount)) for p in bt.payment_entries]
            self.assertEqual(allocated, [(JE, journal, amount)], phone)
        self.assertEqual(frappe.db.get_value(SHEET, doc.name, ["status", "paid_amount", "failed_amount"]),
                         ("Partly Paid", 4300, 1200))
        # Retry Failed: a new batch for the failed row only, paid, booked in a small journal of its own
        retry = self._pay(doc.name, "retry_failed")
        self.assertEqual(frappe.get_all(ROW, filters={"parent": retry}, pluck="partyb"), ["254711000774"])
        again = self._send(retry)
        receipt = self._result(again["254711000774"], "_T JOHN KAMAU")
        journals = self._journals(doc.name)
        self.assertEqual(len(journals), 2)
        late = journals[[n for n in journals if n != journal][0]]
        self.assertEqual([(a, cc, p, d) for a, cc, p, d, _c in late if d], [(self.wages, self.site_b, self.project_a,
                                                                              1200.0)])
        row = self._rows_of(doc.name)[3]
        self.assertEqual((row.status, row.receipt, row.attempts), ("Paid", receipt, 2))
        self.assertEqual(frappe.db.get_value(SHEET, doc.name, "status"), "Paid")
        # the money left: the sheet is never cancelled
        with _as(self.manager):
            self.assertRaisesRegex(frappe.ValidationError, "cannot be cancelled",
                                   frappe.get_doc(SHEET, doc.name).cancel)

    def test_the_journal_waits_for_a_row_on_its_way_then_books_every_paid_row(self):
        doc = self._approved()
        requests = self._send(self._pay(doc.name))
        self._result(requests["254711000771"], "_T JANE WANJIKU")
        self.assertEqual(self._journals(doc.name), {})
        note = frappe.db.get_value(REQ, requests["254711000771"], "accounting_error")
        self.assertTrue(note.startswith("Waiting: "), note)
        for phone, receiver in (("254722000781", "_T BULK OVERTIME WORKER"), ("254711000773", "_T MARY AKINYI"),
                                ("254711000774", "_T JOHN KAMAU")):
            self._result(requests[phone], receiver)
        journals = self._journals(doc.name)
        self.assertEqual(len(journals), 1)
        self.assertEqual(sum(d for *_x, d, _c in next(iter(journals.values()))), 5500)
        self.assertEqual(frappe.db.get_value(REQ, requests["254711000771"], ["accounting_booked", "accounting_error"]),
                         (1, None))

    def test_a_row_in_doubt_is_asked_about_never_sent_again_and_the_paid_rows_book_after_the_set_hours(self):
        doc = self._approved()
        requests = self._send(self._pay(doc.name))
        # row 2's send got no answer: Checking with M-Pesa; Retry Failed never picks it
        frappe.db.set_value(REQ, requests["254722000781"], {"status": "Status Unknown", "conversation_id": None})
        for phone, receiver in (("254711000771", "_T JANE WANJIKU"), ("254711000773", "_T MARY AKINYI"),
                                ("254711000774", "_T JOHN KAMAU")):
            self._result(requests[phone], receiver)
        self.assertEqual(self._journals(doc.name), {})  # it holds the journal back
        row = self._rows_of(doc.name)[1]
        self.assertEqual(row.status, "In Flight")
        self.assertTrue(row.progress.startswith("Checking with M-Pesa"), row.progress)
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "No row has failed", bulk_payout.retry_failed, doc.name,
                                   SETTINGS)
        # three hours later: the paid rows are booked without it (the 15-minute sweep)
        frappe.db.set_value(SROW, row.name, "sent_on", frappe.utils.add_to_date(now_datetime(), hours=-3))
        frappe.db.sql(f"UPDATE `tab{REQ}` SET creation = %s WHERE name = %s",
                      (frappe.utils.add_to_date(now_datetime(), hours=-3), requests["254722000781"]))
        from mpsa_suite.services.b2c_postprocess_service import retry_unbooked_payouts

        with patch("frappe.enqueue"):
            retry_unbooked_payouts()
        journals = self._journals(doc.name)
        self.assertEqual(len(journals), 1)
        self.assertEqual(sum(d for *_x, d, _c in next(iter(journals.values()))), 3500)

    # ------------------------------------------------------------ self-pay and the approval
    def test_nobody_prepares_approves_or_sends_a_payment_to_themselves(self):
        frappe.db.set_value("User", PREPARER, "mobile_no", "0711000773")
        try:
            with _as(PREPARER):
                doc = frappe.get_doc({"doctype": SHEET, "title": "_T own", "company": t.COMPANY,
                                      "posting_date": nowdate(), "expense_type": CASUAL, "cost_center": self.site_a,
                                      "rows": self._rows()})
                self.assertRaisesRegex(frappe.PermissionError, "You may not prepare a payment to yourself: Row #3",
                                       doc.insert)
        finally:
            frappe.db.set_value("User", PREPARER, "mobile_no", None)
        doc = self._draft()
        full_name = frappe.db.get_value("User", self.manager, "full_name")
        frappe.db.set_value("User", self.manager, "full_name", "_T Mary Akinyi")
        try:
            with _as(self.manager):
                sheet = frappe.get_doc(SHEET, doc.name)
                sheet.names_checked = 1
                self.assertRaisesRegex(frappe.PermissionError, "You may not approve a payment to yourself",
                                       sheet.submit)
        finally:
            frappe.db.set_value("User", self.manager, "full_name", full_name)
        doc = self._approved()
        batch = self._pay(doc.name)
        frappe.db.set_value("User", PAYER, "mobile_no", "0711000774")
        try:
            with patch("frappe.enqueue") as enqueue, _as(PAYER):
                self.assertRaisesRegex(frappe.ValidationError, "You may not send a payment to yourself",
                                       send_confirm.send_for_test, frappe.get_doc(DISB, batch))
            self.assertEqual(frappe.db.get_value(DISB, batch, "docstatus"), 0)
            enqueue.assert_not_called()
        finally:
            frappe.db.set_value("User", PAYER, "mobile_no", None)

    def test_the_approval_needs_the_names_ticked_and_a_named_manager(self):
        doc = self._draft()
        with _as(PREPARER):
            self.assertRaises(frappe.PermissionError, frappe.get_doc(SHEET, doc.name).submit)
        with _as(self.manager):
            self.assertRaisesRegex(frappe.ValidationError, "need checking", frappe.get_doc(SHEET, doc.name).submit)
        self.assertEqual(frappe.db.get_value(SHEET, doc.name, "docstatus"), 0)
        approved = self._approved()
        self.assertEqual((approved.names_checked, approved.names_summary), (1, "4 new"))

    # ------------------------------------------------------------ settled outside M-Pesa, the cancel
    def test_a_failed_row_settled_outside_mpesa_is_left_out_and_a_sheet_never_paid_cancels(self):
        doc = self._approved()
        self._paid_all_but_the_last(doc.name)
        row = self._rows_of(doc.name)[3]
        with _as(self.manager):
            bulk_payout.settle_row(doc.name, row.name, "_T paid in cash at the site, voucher PV-12")
        self.assertEqual(frappe.db.get_value(SROW, row.name, "status"), "Cancelled")
        self.assertEqual(frappe.db.get_value(SHEET, doc.name, "status"), "Paid")
        self.assertEqual(len(self._journals(doc.name)), 1)
        # a sheet nothing was sent for is cancelled
        other = self._approved(self._rows()[:1])
        with _as(self.manager):
            frappe.get_doc(SHEET, other.name).cancel()
        self.assertEqual(frappe.db.get_value(SHEET, other.name, ["docstatus", "status"]), (2, "Cancelled"))
        self.assertEqual(frappe.db.get_value(SROW, {"parent": other.name}, "status"), "Cancelled")
