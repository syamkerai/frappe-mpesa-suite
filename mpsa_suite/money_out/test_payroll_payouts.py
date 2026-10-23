"""Employees and payroll (payouts Phase 3) against the real database (ERPNext v15 and v16).

TestEmployeePayouts: Employee M-Pesa Account approval, B2B to an employee's bank paybill with no
salary slip (Make Entry), the date rule on an employee line. TestPayroll (HRMS is a required
app): a Payroll Entry of four employees (one paid to a bank paybill by B2B, one to a phone by
B2C, one with no account, one who left) through the Payroll Entry's button, Pay Selected, the
sends and the Results, the salary Journal Entries clearing the payroll payable (employee-wise
accounting on and off, round-off), never booked twice, a partial failure planned again, the date
rule on the B2C path, maker-checker and the cancel guard.

The Payroll Entry, its Salary Slips and its accrual entry are written directly (db_insert, and a
Journal Entry like HRMS's accrual), not computed by HRMS: what is tested is the M-Pesa side. Same
rules as test_b2b_documents and test_payment_schedule: the shortcode TESTB2BDOCS (B2B and B2C),
nothing reaches Safaricom (B2B post() and the B2C connector are replaced, Results are written as
the callbacks write them), commits made by the code under test are replaced so each test rolls
back, and the jobs the code queues are run in the test. Unit coverage: tests/unit/
test_employee_accounts.py, test_salary_entry.py and test_payroll_schedule.py.
"""

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from mpsa_suite.compat import IntegrationTestCase
from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import b2b, b2b_entries, owed, payroll, schedule
from mpsa_suite.money_out.test_b2b_documents import _account, _settings

SETTINGS = "TESTB2BDOCS"
B2B, JE, JEA = b2b.B2B_DOCTYPE, "Journal Entry", "Journal Entry Account"
SCHEDULE, LINE = schedule.SCHEDULE, schedule.LINE
DISB, ROW, REQ = "B2C Payment Disbursement", "B2C Payment Disbursement Reference", "B2C Disbursement Request"
EMPACC, SLIP, PAYROLL = "Employee M-Pesa Account", "Salary Slip", "Payroll Entry"
PREPARER, PAYER, MANAGER = "_t_pay_accounts@example.com", "_t_pay_payer@example.com", "_t_pay_manager@example.com"
PHONE = "254722000999"
SUITE_VALUES = dict(book_payout_charges=1, reconcile_payout_lines=1, payout_lines_from=None, allow_one_off_b2b=0,
                    entry_reminder_days=3, schedule_maker_checker=1, send_interval_seconds=0,
                    employee_account_hold_hours=0)
EMPLOYEES = (("_T Pay Bank", "Bank Paybill (B2B)"), ("_T Pay Phone", "Phone (B2C)"), ("_T Pay None", None),
             ("_T Pay Left", "Bank Paybill (B2B)"))


def hrms() -> bool:
    return "hrms" in frappe.get_installed_apps()


def _user(email: str, roles: tuple) -> None:
    if frappe.db.exists("User", email):
        return
    frappe.get_doc({"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0,
                    "roles": [{"role": r} for r in roles]}).insert(ignore_permissions=True)


def _employee(first_name: str, status: str = "Active") -> str:
    name = frappe.db.get_value("Employee", {"first_name": first_name, "company": t.COMPANY}, "name")
    if name:
        return name
    if not frappe.db.exists("Gender", "Female"):
        frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert()
    doc = frappe.get_doc({"doctype": "Employee", "first_name": first_name, "gender": "Female",
                          "date_of_birth": "1990-01-01", "date_of_joining": "2020-01-01", "company": t.COMPANY,
                          "status": "Active"})
    doc.insert()
    if status != "Active":
        frappe.db.set_value("Employee", doc.name, {"status": status, "relieving_date": nowdate()})
    return doc.name


def _approved_account(employee: str, account_type: str, name: str) -> str:
    """Written as approved (the approval has its own test)."""
    if frappe.db.exists(EMPACC, name):
        return name
    values = ({"phone": PHONE, "expected_name": "Test Name"} if account_type == "Phone (B2C)" else
              {"shortcode": "247247", "account_number": f"T{name[-4:]}", "expected_name": "Equity Bank"})
    doc = frappe.get_doc({
        "doctype": EMPACC, "employee": employee, "account_type": account_type,
        "is_default": 1, "docstatus": 1, "proof": "/private/files/_t_pay_proof.pdf", "approved_by": "Administrator",
        "approved_on": now_datetime(), "active_from": add_days(now_datetime(), -1), "creation": now_datetime(),
        "modified": now_datetime(), "owner": "Administrator", **values})
    doc.name = name
    doc.db_insert()
    return name


@contextmanager
def _as(user):
    frappe.set_user(user)
    try:
        yield
    finally:
        frappe.set_user("Administrator")


class _Base(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        t.bootstrap()
        t.enable_money_in(**SUITE_VALUES)
        cls.bank_account, cls.gl = _settings()
        _user(PREPARER, ("Accounts User",))
        _user(PAYER, ("M-Pesa Payer",))
        _user(MANAGER, ("M-Pesa Manager", "Accounts Manager"))
        cls.employees = {first: _employee(first, "Left" if first.endswith("Left") else "Active")
                         for first, _ in EMPLOYEES}
        cls.accounts = {}
        for i, (first, account_type) in enumerate(EMPLOYEES):
            if account_type:
                cls.accounts[first] = _approved_account(cls.employees[first], account_type, f"_T-MEA-PAY-{i}")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        frappe.set_user("Administrator")
        frappe.local.mpesa_suite_bank_accounts = None
        self.clock = [1000.0]
        self._patches = [patch.object(frappe.db, "commit"),
                         patch.object(schedule, "_clock", side_effect=lambda: self.clock[0])]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        frappe.set_user("Administrator")
        frappe.db.rollback()
        frappe.local.mpesa_suite_bank_accounts = None
        frappe.clear_document_cache("M-Pesa Suite Settings", "M-Pesa Suite Settings")

    def _pay(self, lines, user=PAYER) -> dict:
        """Pay Selected as ``user``: the summary, 6 seconds, Confirm; the queued jobs are kept."""
        with _as(user):
            summary = schedule.prepare_payment(lines)
            self.clock[0] += 6
            with patch("frappe.enqueue") as enqueue:
                answer = schedule.confirm_payment(summary["token"]) if summary["token"] else {"sent": [], "refused": []}
        answer["summary"], answer["jobs"] = summary, list(enqueue.call_args_list) if summary["token"] else []
        return answer

    def _ack(self, settings, endpoint, payload, **kw):
        name = kw["name"]
        return {"ResponseCode": "0", "ResponseDescription": "Accepted", "ConversationID": f"AG_{name}",
                "OriginatorConversationID": f"OC_{name}"}, None

    def _send_b2b(self, answer, user=PAYER):
        jobs = [c for c in answer["jobs"] if c.args and c.args[0].endswith("schedule.send_batch")]
        with _as(user), patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack) as post, patch("frappe.enqueue"):
            for job in jobs:
                schedule.send_batch(job.kwargs["requests"], job.kwargs["batch"])
        for job in jobs:
            for name in job.kwargs["requests"]:
                schedule.sync_line(name)
        return post

    def _b2b_result(self, name, public="247247 - Equity Bank Limited") -> str:
        receipt = "T" + frappe.generate_hash(length=9).upper()
        result = {
            "ResultType": 0, "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
            "ConversationID": f"AG_{name}", "OriginatorConversationID": f"OC_{name}", "TransactionID": receipt,
            "ResultParameters": {"ResultParameter": [
                {"Key": "DebitAccountBalance", "Value": "Working Account|KES|500000.00|500000.00|0.00|0.00"},
                {"Key": "TransCompletedTime", "Value": now_datetime().strftime("%Y%m%d%H%M%S")},
                {"Key": "DebitPartyCharges", "Value": "Business Pay Bill Charge|KES|2.00"},
                {"Key": "ReceiverPartyPublicName", "Value": public}]},
        }
        with patch("frappe.enqueue"):
            b2b.handle_result(result, name)
            b2b.book_payment_entry(name)
        schedule.sync_line(name)
        return receipt

    def _line(self, name):
        return frappe.db.get_value(LINE, name, ["status", "request", "receipt", "entry_status", "last_problem",
                                                "b2c_disbursement", "b2c_row", "b2c_request", "approved_by"],
                                   as_dict=True)


class TestEmployeePayouts(_Base):
    def test_an_employee_account_is_approved_by_someone_else_with_proof(self):
        doc = frappe.get_doc({"doctype": EMPACC, "employee": self.employees["_T Pay None"], "account_type": "Phone (B2C)",
                              "phone": "0722 000 998", "expected_name": "Test Name",
                              "proof": "/private/files/_t_pay_proof.pdf"})
        frappe.get_doc("User", PREPARER).add_roles("M-Pesa Manager")
        with _as(PREPARER):
            doc.insert()
            self.assertEqual(doc.phone, "254722000998")
            self.assertRaisesRegex(frappe.ValidationError, "different person", doc.submit)
        with _as(MANAGER):
            doc = frappe.get_doc(EMPACC, doc.name)
            doc.submit()
        self.assertEqual((doc.docstatus, doc.approved_by), (1, MANAGER))

    def test_b2b_to_an_employee_bank_paybill_without_a_slip_waits_for_make_entry(self):
        employee = self.employees["_T Pay Bank"]
        with _as(MANAGER), patch("mpsa_suite.money_out.b2b.post", side_effect=self._ack):
            req = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "amount": 1500,
                                  "remarks": "Allowance", "employee": employee})
            req.insert()
            self.assertEqual((req.employee_mpesa_account, req.receiver_shortcode, req.entry_mode, req.supplier),
                             (self.accounts["_T Pay Bank"], "247247", "Make the entry later", None))
            req.submit()
        self._b2b_result(req.name)
        self.assertEqual(frappe.db.get_value(B2B, req.name, "entry_status"), "Entry Not Made")
        expense = _account("_T Pay Allowances", "Expense", "Profit and Loss")
        center = frappe.db.get_value("Company", t.COMPANY, "cost_center")
        result = b2b_entries.make_entry(req.name, "Journal Entry", account=expense, cost_center=center)
        self.assertEqual(frappe.db.get_value(B2B, req.name, "entry_status"), "Entry Made")
        self.assertEqual(frappe.db.get_value(JE, result["name"], "cheque_no"), frappe.db.get_value(B2B, req.name,
                                                                                                 "transaction_id"))

    def test_an_employee_phone_account_is_never_paid_by_b2b(self):
        with _as(MANAGER):
            req = frappe.get_doc({"doctype": B2B, "mpesa_settings": SETTINGS, "company": t.COMPANY, "amount": 1500,
                                  "remarks": "x", "employee": self.employees["_T Pay Phone"],
                                  "employee_mpesa_account": self.accounts["_T Pay Phone"]})
            self.assertRaisesRegex(frappe.ValidationError, "B2B pays bank paybills only", req.insert)

    def test_an_employee_line_is_not_paid_before_its_day(self):
        with _as(PREPARER):
            doc = frappe.get_doc({"doctype": SCHEDULE, "title": "T employee", "company": t.COMPANY,
                                  "b2b_settings": SETTINGS, "lines": [{
                                      "planned_date": add_days(nowdate(), 1), "party_type": "Employee",
                                      "party": self.employees["_T Pay Bank"], "amount": 700}]}).insert()
        line = doc.lines[0]
        self.assertEqual((line.route, line.entry_mode), ("B2B Paybill", "Make the entry later"))
        answer = self._pay([line.name])
        self.assertIn("Not due until", answer["summary"]["not_included"][0]["reason"])


class TestPayroll(_Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not hrms():
            return
        # the Payroll Entry's button needs read on it (HRMS: HR Manager) and create on the schedule
        frappe.get_doc("User", PREPARER).add_roles("HR Manager")
        cls.payable = _account("_T Pay Payroll Payable", "Liability", "Balance Sheet")
        cls.expense = _account("_T Pay Salaries", "Expense", "Profit and Loss")
        if not frappe.db.get_value("Company", t.COMPANY, "round_off_account"):
            frappe.db.set_value("Company", t.COMPANY, "round_off_account",
                                _account("_T Pay Round Off", "Expense", "Profit and Loss"))
        frappe.db.commit()

    def setUp(self):
        if not hrms():
            self.skipTest("HRMS is not installed")
        super().setUp()
        self.employee_wise(0)

    def employee_wise(self, on: int):
        frappe.db.set_single_value("Payroll Settings", "process_payroll_accounting_entry_based_on_employee", on)
        frappe.clear_document_cache("Payroll Settings", "Payroll Settings")

    # ------------------------------------------------------------ a payroll written as HRMS leaves it
    def _payroll(self, nets=(50000.40, 30000, 20000, 15000)) -> tuple:
        """A submitted Payroll Entry, a submitted Salary Slip per employee and the accrual entry
        crediting each net pay to the payroll payable."""
        tag = frappe.generate_hash(length=6)
        center = frappe.db.get_value("Company", t.COMPANY, "cost_center")
        pe = frappe.get_doc({
            "doctype": PAYROLL, "company": t.COMPANY, "posting_date": nowdate(), "start_date": nowdate(),
            "end_date": nowdate(), "payroll_frequency": "Monthly", "currency": "KES", "exchange_rate": 1,
            "payroll_payable_account": self.payable, "cost_center": center, "docstatus": 1, "status": "Submitted",
            "salary_slips_created": 1, "salary_slips_submitted": 1, "creation": now_datetime(),
            "modified": now_datetime(), "owner": "Administrator"})
        pe.name = f"_T-PAY-{tag}"
        pe.db_insert()
        slips = []
        per_employee = frappe.db.get_single_value("Payroll Settings", "process_payroll_accounting_entry_based_on_employee")
        accrual = frappe.get_doc({"doctype": JE, "voucher_type": "Journal Entry", "company": t.COMPANY,
                                  "posting_date": nowdate(), "user_remark": "Accrual", "accounts": []})
        for (first, _), net in zip(EMPLOYEES, nets):
            slip = frappe.get_doc({
                "doctype": SLIP, "employee": self.employees[first], "employee_name": first, "company": t.COMPANY,
                "posting_date": nowdate(), "start_date": nowdate(), "end_date": nowdate(), "currency": "KES",
                "exchange_rate": 1, "payroll_entry": pe.name, "net_pay": net, "rounded_total": round(net),
                "base_net_pay": net, "base_rounded_total": round(net), "gross_pay": net, "status": "Submitted",
                "docstatus": 1, "creation": now_datetime(), "modified": now_datetime(), "owner": "Administrator"})
            slip.name = f"_T-SAL-{tag}-{len(slips)}"
            slip.db_insert()
            slips.append(slip.name)
            row = {"account": self.payable, "credit_in_account_currency": net, "reference_type": PAYROLL,
                   "reference_name": pe.name, "cost_center": center}
            if per_employee:
                row.update(party_type="Employee", party=self.employees[first])
            accrual.append("accounts", row)
        accrual.append("accounts", {"account": self.expense, "debit_in_account_currency": flt(sum(nets), 2),
                                    "cost_center": center})
        accrual.insert()
        accrual.submit()
        return pe.name, slips

    def _payable_left(self, pe, employee=None) -> float:
        """What the payroll payable still holds for the Payroll Entry (or one employee)."""
        party = "AND jea.party = %(employee)s" if employee else ""
        return flt(frappe.db.sql(
            f"""SELECT SUM(jea.credit_in_account_currency - jea.debit_in_account_currency)
            FROM `tabJournal Entry Account` jea JOIN `tabJournal Entry` je ON je.name = jea.parent
            WHERE je.docstatus = 1 AND jea.account = %(account)s AND jea.reference_type = 'Payroll Entry'
              AND jea.reference_name = %(pe)s {party}""",
            {"account": self.payable, "pe": pe, "employee": employee})[0][0], 2)

    def _make_schedule(self, pe, **kw):
        values = {"b2b_settings": SETTINGS, "b2c_settings": SETTINGS, **kw}
        with _as(PREPARER):
            answer = schedule.from_payroll_entry(pe, **values)
        doc = frappe.get_doc(SCHEDULE, answer["name"])
        return answer, {line.party: line for line in doc.lines}

    def _run_b2c(self, answer, result="Paid", user=PAYER) -> list:
        """The create_b2c_request jobs Pay Selected queued, sent with Daraja's ack replaced, then each
        payout's Result written as the B2C callback writes it, and its job run."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb
        from mpsa_suite.services import b2c_response_service

        def ack(doc, callback_url):
            frappe.db.set_value(REQ, doc.name, {"status": "Initiated", "conversation_id": f"AG_{doc.name}"})
            return {"ResponseCode": "0"}

        connector = type("Connector", (), {"send_b2c_request": staticmethod(ack)})()
        requests = []
        jobs = [c for c in answer["jobs"] if c.args and c.args[0].endswith("create_b2c_request")]
        with _as(user), patch("mpsa_suite.services.b2c_request_service.get_b2c_connector", return_value=connector), \
                patch("frappe.enqueue"):
            for job in jobs:
                disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
                name = frappe.db.get_value(ROW, job.kwargs["ref"].name, "b2c_disbursement_request")
                if name:
                    requests.append(name)
        for name in requests:
            values = {"status": result, "result_code": "0" if result == "Paid" else "2001",
                      "result_desc": "ok" if result == "Paid" else "The initiator information is invalid."}
            if result == "Paid":
                values.update(transaction_id="T" + frappe.generate_hash(length=9).upper(),
                              transaction_completed_datetime=now_datetime(), receiver_public_name=f"{PHONE} - TEST NAME",
                              utility_acct_avlbl_funds=400000)
            frappe.db.set_value(REQ, name, values)
            with patch("frappe.enqueue"):
                b2c_response_service.update_b2c_reference_status(name, enqueue_next=True)
            schedule.sync_b2c_line(frappe.db.get_value(REQ, name, "b2c_payment_disbursement_reference"))
        return requests

    # ------------------------------------------------------------ the whole payroll
    def _whole_payroll(self, employee_wise: int):
        self.employee_wise(employee_wise)
        pe, slips = self._payroll()
        answer, lines = self._make_schedule(pe)
        bank, phone = self.employees["_T Pay Bank"], self.employees["_T Pay Phone"]
        self.assertEqual(set(lines), {bank, phone})
        self.assertEqual((lines[bank].route, flt(lines[bank].amount)), ("B2B Paybill", 50000))
        self.assertEqual((lines[phone].route, flt(lines[phone].amount), lines[phone].destination),
                         ("B2C Phone", 30000, PHONE))
        excluded = frappe.db.get_value(SCHEDULE, answer["name"], "excluded")
        self.assertIn("no approved M-Pesa account", excluded)
        self.assertIn("the employee is Left", excluded)
        self.assertEqual(frappe.db.get_value(SCHEDULE, answer["name"], "payroll_entry"), pe)

        paid = self._pay([lines[bank].name, lines[phone].name])
        self.assertEqual(paid["refused"], [])
        self._send_b2b(paid)
        request = self._line(lines[bank].name).request
        self.assertEqual(frappe.db.get_value(B2B, request, ["employee", "salary_slip", "payroll_entry"]),
                         (bank, slips[0], pe))
        self._b2b_result(request)
        b2c = self._run_b2c(paid)
        self.assertEqual(len(b2c), 1)
        for party in (bank, phone):
            state = self._line(lines[party].name)
            self.assertEqual((state.status, state.entry_status), ("Paid", "Entry Made"))
        # each paid salary is off the payroll payable exactly (round-off took the 0.40)
        if employee_wise:
            self.assertEqual(self._payable_left(pe, bank), 0)
            self.assertEqual(self._payable_left(pe, phone), 0)
        self.assertEqual(self._payable_left(pe), 20000 + 15000)  # the two not paid by M-Pesa
        entry = frappe.db.get_value(B2B, request, "journal_entry")
        rows = frappe.get_all(JEA, filters={"parent": entry}, fields=["account", "party", "debit_in_account_currency",
                                                                       "credit_in_account_currency"],
                              order_by="idx asc")
        self.assertEqual(rows[0].party or None, bank if employee_wise else None)
        round_off = frappe.db.get_value("Company", t.COMPANY, "round_off_account")
        self.assertEqual(flt(next(r for r in rows if r.account == round_off).credit_in_account_currency), 0.40)
        self.assertEqual(owed.slip_left_to_pay(slips[0]).left, 0)
        self.assertEqual(owed.slip_left_to_pay(slips[1]).left, 0)
        return pe, slips, request, b2c

    def test_a_payroll_paid_by_b2b_and_b2c_clears_the_payroll_payable_per_employee(self):
        self._whole_payroll(employee_wise=1)

    def test_a_payroll_paid_by_b2b_and_b2c_clears_the_payroll_payable_without_parties(self):
        self._whole_payroll(employee_wise=0)

    def test_salaries_are_never_booked_twice(self):
        pe, slips, request, b2c = self._whole_payroll(employee_wise=1)
        receipt = frappe.db.get_value(B2B, request, "transaction_id")
        with patch("frappe.enqueue"):
            b2b.book_payment_entry(request)
        self.assertEqual(frappe.db.count(JE, {"cheque_no": receipt, "docstatus": ["<", 2]}), 1)
        from mpsa_suite.services import b2c_response_service

        b2c_receipt = frappe.db.get_value(REQ, b2c[0], "transaction_id")
        with patch("frappe.enqueue"):
            b2c_response_service.update_b2c_reference_status(b2c[0], enqueue_next=True)
        self.assertEqual(frappe.db.count(JE, {"cheque_no": b2c_receipt, "docstatus": ["<", 2]}), 1)
        self.assertEqual(frappe.db.get_value(REQ, b2c[0], "accounting_booked"), 1)

    # ------------------------------------------------------------ rules on the way
    def test_one_live_schedule_per_payroll_and_none_beside_a_bank_entry(self):
        pe, _ = self._payroll()
        answer, _ = self._make_schedule(pe)
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "already has M-Pesa Payment Schedule",
                                   schedule.from_payroll_entry, pe, b2b_settings=SETTINGS, b2c_settings=SETTINGS)
        frappe.db.set_value(SCHEDULE, answer["name"], "closed", 1)
        bank = frappe.get_doc({"doctype": JE, "voucher_type": "Bank Entry", "company": t.COMPANY, "posting_date": nowdate(),
                               "cheque_no": "CHQ-T1", "cheque_date": nowdate(), "accounts": [
                                   {"account": self.payable, "debit_in_account_currency": 100, "reference_type": PAYROLL,
                                    "reference_name": pe},
                                   {"account": self.gl, "credit_in_account_currency": 100,
                                    "bank_account": self.bank_account}]})
        bank.insert()
        with _as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "bank or cash entry not made by M-Pesa",
                                   schedule.from_payroll_entry, pe, b2b_settings=SETTINGS, b2c_settings=SETTINGS)

    def test_the_date_rule_and_maker_checker_hold_on_the_b2c_path(self):
        pe, slips = self._payroll()
        answer, lines = self._make_schedule(pe, planned_date=add_days(nowdate(), 1))
        phone = lines[self.employees["_T Pay Phone"]]
        self.assertIn("Not due until", self._pay([phone.name])["summary"]["not_included"][0]["reason"])
        frappe.db.set_value(LINE, phone.name, "planned_date", nowdate(), update_modified=False)
        frappe.get_doc("User", PREPARER).add_roles("M-Pesa Payer")
        refused = self._pay([phone.name], user=PREPARER)
        self.assertIn("another person must pay it", refused["summary"]["not_included"][0]["reason"])
        paid = self._pay([phone.name])
        self.assertEqual(len(paid["sent"]), 1)
        # moved to tomorrow under it (by the database): the job that makes the payout refuses it
        frappe.db.set_value(LINE, phone.name, "planned_date", add_days(nowdate(), 1), update_modified=False)
        self.assertEqual(self._run_b2c(paid), [])
        state = self._line(phone.name)
        self.assertEqual(state.status, "Failed")
        self.assertIn("Not sent: Not due until", state.last_problem)
        self.assertEqual(frappe.db.get_value(ROW, state.b2c_row, "payment_status"), "Failed")
        self.assertFalse(frappe.db.exists(REQ, {"b2c_payment_disbursement_reference": state.b2c_row}))

    def test_a_failed_phone_salary_is_planned_again_and_the_bank_one_stays_paid(self):
        pe, slips = self._payroll()
        answer, lines = self._make_schedule(pe)
        bank, phone = lines[self.employees["_T Pay Bank"]], lines[self.employees["_T Pay Phone"]]
        paid = self._pay([bank.name, phone.name])
        self._send_b2b(paid)
        self._b2b_result(self._line(bank.name).request)
        self._run_b2c(paid, result="Failed")
        self.assertEqual(self._line(bank.name).status, "Paid")
        self.assertEqual(self._line(phone.name).status, "Failed")
        self.assertEqual(owed.slip_left_to_pay(slips[1]).left, 30000)  # a failed payout frees the slip
        with _as(PREPARER):
            schedule.plan_again(phone.name)
        state = self._line(phone.name)
        self.assertEqual((state.status, state.b2c_disbursement, state.b2c_row), ("Planned", None, None))
        again = self._pay([phone.name])
        self.assertEqual(len(again["sent"]), 1)

    def test_the_phone_number_is_locked_to_the_approved_account(self):
        pe, slips = self._payroll()
        answer, lines = self._make_schedule(pe)
        phone = lines[self.employees["_T Pay Phone"]]
        paid = self._pay([phone.name])
        row = frappe.get_doc(ROW, self._line(phone.name).b2c_row)
        row.partyb = "254733000000"
        self.assertRaisesRegex(frappe.ValidationError, "approved phone", row.validate)
        self.assertEqual(frappe.db.get_value(DISB, paid["sent"][0]["request"], "payment_schedule"), answer["name"])

    def test_a_payroll_with_mpesa_salaries_cannot_be_cancelled(self):
        pe, slips = self._payroll()
        answer, lines = self._make_schedule(pe)
        doc = frappe.get_doc(PAYROLL, pe)
        self.assertRaisesRegex(frappe.ValidationError, "delete it first", payroll.before_cancel, doc)
        bank = lines[self.employees["_T Pay Bank"]]
        paid = self._pay([bank.name])
        self.assertRaisesRegex(frappe.ValidationError, "paid or on their way", payroll.before_cancel, doc)
        self._send_b2b(paid)
        self._b2b_result(self._line(bank.name).request)
        self.assertRaisesRegex(frappe.ValidationError, "paid or on their way", doc.cancel)
        self.assertEqual(frappe.db.get_value(PAYROLL, pe, "docstatus"), 1)

    def test_withheld_and_left_employees_are_excluded(self):
        pe, slips = self._payroll()
        frappe.db.set_value(SLIP, slips[0], "status", "Withheld")
        answer, lines = self._make_schedule(pe)
        self.assertNotIn(self.employees["_T Pay Bank"], lines)
        excluded = frappe.db.get_value(SCHEDULE, answer["name"], "excluded")
        self.assertIn("the salary is withheld", excluded)
        self.assertIn("the employee is Left", excluded)

    def test_the_due_report_shows_the_utility_balance_for_phone_lines(self):
        from mpsa_suite.mpsa_suite.report.m_pesa_payments_due.m_pesa_payments_due import execute

        pe, slips = self._payroll()
        answer, lines = self._make_schedule(pe)
        _, data, _, _, cards = execute({"company": t.COMPANY, "schedule": answer["name"]})
        self.assertEqual({d["route"] for d in data}, {"B2B Paybill", "B2C Phone"})
        labels = [c["label"] for c in cards]
        self.assertTrue(any(label.startswith("Utility") for label in labels), labels)
        self.assertTrue(any(label.startswith("Working") for label in labels), labels)

