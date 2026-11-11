"""Salaries from Salary Slips (round 4, money_out.payroll) against the real database (ERPNext and
HRMS v15 and v16).

A one-off salary by B2C (to the employee's phone) and by B2B (to a bank account that is not the
default) through its one-line M-Pesa Payment Schedule and Pay Selected; the Salary Slip list's
Pay Selected Slips by M-Pesa for three slips with mixed routes (phone, bank account, till), paid
in one Pay Selected; the double-pay refusals both ways (the Payroll Entry's schedule against a
one-off, a one-off against the Payroll Entry's schedule); the cancel guards on the Payroll Entry
and the slip; who may prepare and who may see the figures, and who may not read the employee's
accounts (the default only); a slip partly planned elsewhere left out of selected slips; the
forms' headlines.

Named users only: an Accounts User who is also HR Manager prepares (reads slips and Payroll
Entries, sees salaries, makes schedules), an M-Pesa Payer pays with Pay Selected (the payer's
deliberate step: send_confirm.deliberate inside confirm_payment). Administrator and System
Manager neither prepare nor pay. Fixtures and rules as test_payroll_payouts: the shortcode
TESTB2BDOCS, nothing reaches Safaricom, Results written as the callbacks write them, commits
replaced so each test rolls back, the queued jobs run in the test. Unit coverage:
tests/unit/test_salary_slip_payouts.py.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, flt, now_datetime, nowdate

from mpsa_suite.money_in import testing as t
from mpsa_suite.money_out import owed, party_accounts, payroll, schedule
from mpsa_suite.money_out import test_payroll_payouts as pp
from mpsa_suite.money_out.test_b2b_documents import _account

SETTINGS = pp.SETTINGS
B2B, JE, JEA = pp.B2B, pp.JE, pp.JEA
SCHEDULE, LINE, SLIP, PAYROLL, EMPACC = pp.SCHEDULE, pp.LINE, pp.SLIP, pp.PAYROLL, pp.EMPACC
REQ, ROW = pp.REQ, pp.ROW
PREPARER = "_t_slip_preparer@example.com"
PAYER = "_t_slip_payer@example.com"
ACCOUNTS_ONLY = "_t_slip_accounts@example.com"
HR_ONLY = "_t_slip_hr@example.com"
SYSTEM_MANAGER = "_t_slip_sysmgr@example.com"
ACCOUNTS_PAYER = "_t_slip_accounts_payer@example.com"
BOOKS = "_t_slip_books@example.com"
USERS = {
    PREPARER: ("Accounts User", "HR Manager"),
    PAYER: ("M-Pesa Payer",),
    ACCOUNTS_ONLY: ("Accounts User",),  # makes schedules, may not see salaries
    HR_ONLY: ("HR User",),  # sees salaries, may not make a schedule
    SYSTEM_MANAGER: ("System Manager",),
    # makes schedules and sees salaries, may not read Employee M-Pesa Accounts (their DocPerms: HR, M-Pesa Manager)
    ACCOUNTS_PAYER: ("Accounts User", "M-Pesa Payer"),
    BOOKS: ("Accounts Manager",),  # makes B2C Payment Disbursements, may not see salaries
}
PHONE = "254722000888"
TILL = "835811"
# first name -> (net pay, accounts as (type, default, values))
PEOPLE = {
    "_T Slip Phone": (30000, ((party_accounts.PHONE, 1, {"phone": PHONE, "expected_name": "Test Slip Name"}),
                              (party_accounts.BANK, 0, {"shortcode": "247247", "account_number": "TSLIP0001",
                                                        "bank": None, "expected_name": "Equity Bank"}))),
    "_T Slip Bank": (50000.40, ((party_accounts.BANK, 1, {"shortcode": "247247", "account_number": "TSLIP0002",
                                                          "expected_name": "Equity Bank"}),)),
    "_T Slip Till": (12000, ((party_accounts.TILL, 1, {"shortcode": TILL, "expected_name": "T SLIP TILL"}),)),
    "_T Slip None": (8000, ()),
}


def _approved(employee: str, account_type: str, default: int, values: dict, name: str) -> str:
    """Written as approved (the approval has its own tests)."""
    if frappe.db.exists(EMPACC, name):
        return name
    doc = frappe.get_doc({
        "doctype": EMPACC, "employee": employee, "account_type": account_type, "is_default": default, "docstatus": 1,
        "proof": "/private/files/_t_slip_proof.pdf", "approved_by": pp.MANAGER, "approved_on": now_datetime(),
        "active_from": add_days(now_datetime(), -1), "creation": now_datetime(), "modified": now_datetime(),
        "owner": pp.PREPARER, **values})
    doc.name = name
    doc.db_insert()
    return name


class TestSalarySlipPayouts(pp._Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not pp.hrms():
            return
        for email, roles in USERS.items():
            pp._user(email, roles)
        cls.payable = _account("_T Pay Payroll Payable", "Liability", "Balance Sheet")
        cls.expense = _account("_T Pay Salaries", "Expense", "Profit and Loss")
        if not frappe.db.get_value("Company", t.COMPANY, "round_off_account"):
            frappe.db.set_value("Company", t.COMPANY, "round_off_account",
                                _account("_T Pay Round Off", "Expense", "Profit and Loss"))
        cls.people, cls.slip_accounts = {}, {}
        for i, (first, (_, accounts)) in enumerate(PEOPLE.items()):
            employee = pp._employee(first)
            cls.people[first] = employee
            for j, (account_type, default, values) in enumerate(accounts):
                cls.slip_accounts[(first, account_type)] = _approved(employee, account_type, default, values,
                                                                     f"_T-MEA-SLIP-{i}-{j}")
        frappe.db.commit()  # fixtures stay; each test rolls back to here

    def setUp(self):
        if not pp.hrms():
            self.skipTest("HRMS is not installed")
        super().setUp()
        frappe.db.set_single_value("Payroll Settings", "process_payroll_accounting_entry_based_on_employee", 1)
        frappe.clear_document_cache("Payroll Settings", "Payroll Settings")

    _payable_left = pp.TestPayroll._payable_left  # what the payroll payable still holds (per employee)

    # ------------------------------------------------------------ a payroll written as HRMS leaves it
    def _payroll(self) -> tuple:
        """A submitted Payroll Entry, a submitted Salary Slip per person and the accrual entry
        crediting each net pay to the payroll payable, per employee (as test_payroll_payouts)."""
        tag = frappe.generate_hash(length=6)
        center = frappe.db.get_value("Company", t.COMPANY, "cost_center")
        pe = frappe.get_doc({
            "doctype": PAYROLL, "company": t.COMPANY, "posting_date": nowdate(), "start_date": nowdate(),
            "end_date": nowdate(), "payroll_frequency": "Monthly", "currency": "KES", "exchange_rate": 1,
            "payroll_payable_account": self.payable, "cost_center": center, "docstatus": 1, "status": "Submitted",
            "salary_slips_created": 1, "salary_slips_submitted": 1, "creation": now_datetime(),
            "modified": now_datetime(), "owner": "Administrator"})
        pe.name = f"_T-PAYS-{tag}"
        pe.db_insert()
        slips = {}
        accrual = frappe.get_doc({"doctype": JE, "voucher_type": "Journal Entry", "company": t.COMPANY,
                                  "posting_date": nowdate(), "user_remark": "Accrual", "accounts": []})
        for first, (net, _) in PEOPLE.items():
            slip = frappe.get_doc({
                "doctype": SLIP, "employee": self.people[first], "employee_name": first, "company": t.COMPANY,
                "posting_date": nowdate(), "start_date": nowdate(), "end_date": nowdate(), "currency": "KES",
                "exchange_rate": 1, "payroll_entry": pe.name, "net_pay": net, "rounded_total": round(net),
                "base_net_pay": net, "base_rounded_total": round(net), "gross_pay": net, "status": "Submitted",
                "docstatus": 1, "creation": now_datetime(), "modified": now_datetime(), "owner": "Administrator"})
            slip.name = f"_T-SALS-{tag}-{len(slips)}"
            slip.db_insert()
            slips[first] = slip.name
            accrual.append("accounts", {"account": self.payable, "credit_in_account_currency": net,
                                        "reference_type": PAYROLL, "reference_name": pe.name, "cost_center": center,
                                        "party_type": "Employee", "party": self.people[first]})
        accrual.append("accounts", {"account": self.expense, "cost_center": center,
                                    "debit_in_account_currency": flt(sum(n for n, _ in PEOPLE.values()), 2)})
        accrual.insert()
        accrual.submit()
        return pe.name, slips

    def _run_b2c(self, answer, phone=PHONE, public="TEST SLIP NAME") -> list:
        """The create_b2c_request jobs Pay Selected queued, sent with Daraja's ack replaced, each
        payout's Result written as the B2C callback writes it, and its job run (test_payroll_payouts)."""
        from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement import b2c_payment_disbursement as disb
        from mpsa_suite.services import b2c_response_service

        def ack(doc, callback_url):
            frappe.db.set_value(REQ, doc.name, {"status": "Initiated", "conversation_id": f"AG_{doc.name}"})
            return {"ResponseCode": "0"}

        connector = type("Connector", (), {"send_b2c_request": staticmethod(ack)})()
        requests = []
        jobs = [c for c in answer["jobs"] if c.args and c.args[0].endswith("create_b2c_request")]
        with pp._as(PAYER), patch("mpsa_suite.services.b2c_request_service.get_b2c_connector", return_value=connector), \
                patch("frappe.enqueue"):
            for job in jobs:
                disb.create_b2c_request(job.kwargs["ref"], job.kwargs["settings"], job.kwargs["b2c_disbursement"])
                name = frappe.db.get_value(ROW, job.kwargs["ref"].name, "b2c_disbursement_request")
                if name:
                    requests.append(name)
        for name in requests:
            frappe.db.set_value(REQ, name, {
                "status": "Paid", "result_code": "0", "result_desc": "ok",
                "transaction_id": "T" + frappe.generate_hash(length=9).upper(),
                "transaction_completed_datetime": now_datetime(), "receiver_public_name": f"{phone} - {public}",
                "utility_acct_avlbl_funds": 400000})
            with patch("frappe.enqueue"):
                b2c_response_service.update_b2c_reference_status(name, enqueue_next=True)
            schedule.sync_b2c_line(frappe.db.get_value(REQ, name, "b2c_payment_disbursement_reference"))
        return requests

    def _lines(self, name) -> list:
        return frappe.get_all(LINE, filters={"parent": name}, order_by="idx asc",
                              fields=["name", "party", "payee_account", "route", "destination", "amount",
                                      "reference_name", "status", "prepared_by"])

    def _one_off(self, slip, account=None, **kw) -> tuple:
        values = {"b2b_settings": SETTINGS, "b2c_settings": SETTINGS, **kw}
        with pp._as(PREPARER):
            answer = payroll.from_salary_slip(slip, payee_account=account, **values)
        return answer, self._lines(answer["name"])

    # ------------------------------------------------------------ one slip
    def test_a_one_off_salary_by_b2c_is_paid_with_pay_selected(self):
        pe, slips = self._payroll()
        slip, employee = slips["_T Slip Phone"], self.people["_T Slip Phone"]
        answer, (line,) = self._one_off(slip)  # the default: the phone
        header = frappe.db.get_value(SCHEDULE, answer["name"], ["purpose", "payroll_entry", "owner"], as_dict=True)
        self.assertEqual((header.purpose, header.payroll_entry or None, header.owner), ("Payroll", None, PREPARER))
        self.assertEqual((line.party, line.route, line.destination, flt(line.amount), line.prepared_by),
                         (employee, "B2C Phone", PHONE, 30000, PREPARER))
        # salary data: hidden from an Accounts User who may not see salaries, shown to the payer
        doc = frappe.get_doc(SCHEDULE, answer["name"])
        self.assertFalse(frappe.has_permission(SCHEDULE, "read", doc=doc, user=ACCOUNTS_ONLY))
        self.assertTrue(frappe.has_permission(SCHEDULE, "read", doc=doc, user=PAYER))
        # who prepared it does not pay it
        frappe.get_doc("User", PREPARER).add_roles("M-Pesa Payer")
        refused = self._pay([line.name], user=PREPARER)
        self.assertIn("another person must pay it", refused["summary"]["not_included"][0]["reason"])
        frappe.get_doc("User", PREPARER).remove_roles("M-Pesa Payer")
        paid = self._pay([line.name], user=PAYER)
        self.assertEqual(paid["refused"], [])
        self.assertEqual(len(self._run_b2c(paid)), 1)
        state = self._line(line.name)
        self.assertEqual((state.status, state.entry_status, state.approved_by), ("Paid", "Entry Made", PAYER))
        self.assertEqual(owed.slip_left_to_pay(slip).left, 0)
        self.assertEqual(self._payable_left(pe, employee), 0)
        # R4.1: the slip's payment advice, its payout masked (who may see salaries: Administrator here)
        html = t.renders("Salary Slip", slip, "Salary Payment", "M-Pesa B2C", "Fully Paid")
        self.assertNotIn(PHONE, html)

    def test_a_one_off_salary_by_b2b_to_a_bank_account_that_is_not_the_default(self):
        pe, slips = self._payroll()
        slip, employee = slips["_T Slip Phone"], self.people["_T Slip Phone"]
        bank = self.slip_accounts[("_T Slip Phone", party_accounts.BANK)]
        answer, (line,) = self._one_off(slip, bank)
        self.assertEqual((line.payee_account, line.route, line.destination), (bank, "B2B Paybill",
                                                                                "247247 / TSLIP0001"))
        self.assertEqual((answer["route"], answer["amount"]), ("B2B Paybill", 30000))
        paid = self._pay([line.name], user=PAYER)
        self._send_b2b(paid, user=PAYER)
        request = self._line(line.name).request
        self.assertEqual(frappe.db.get_value(B2B, request, ["employee", "employee_mpesa_account", "salary_slip",
                                                            "payroll_entry"]), (employee, bank, slip, pe))
        self._b2b_result(request)
        self.assertEqual(self._line(line.name).status, "Paid")
        self.assertEqual(self._payable_left(pe, employee), 0)
        self.assertEqual(owed.slip_left_to_pay(slip).left, 0)

    # ------------------------------------------------------------ the list's selected slips
    def test_three_selected_slips_one_schedule_mixed_routes_one_pay_selected(self):
        if party_accounts.TILL not in party_accounts.PROFILES[EMPACC].types:
            # an employee's till is paid by B2B once the payee accounts work package (r4-wp2) is merged
            self.skipTest("employee tills need WP2's payee account types")
        pe, slips = self._payroll()
        picked = [slips[f] for f in ("_T Slip Phone", "_T Slip Bank", "_T Slip Till")]
        with pp._as(PREPARER):
            answer = payroll.from_salary_slips(picked, b2b_settings=SETTINGS, b2c_settings=SETTINGS)
        self.assertEqual((answer["lines"], answer["excluded"], answer["payroll_entry"]), (3, [], None))
        self.assertIsNone(frappe.db.get_value(SCHEDULE, answer["name"], "payroll_entry"))
        lines = {line.party: line for line in self._lines(answer["name"])}
        routes = {first: (lines[self.people[first]].route, flt(lines[self.people[first]].amount))
                  for first in ("_T Slip Phone", "_T Slip Bank", "_T Slip Till")}
        self.assertEqual(routes, {"_T Slip Phone": ("B2C Phone", 30000), "_T Slip Bank": ("B2B Paybill", 50000),
                                  "_T Slip Till": ("B2B Till", 12000)})
        paid = self._pay([line.name for line in lines.values()], user=PAYER)
        self.assertEqual(paid["refused"], [])
        self._send_b2b(paid, user=PAYER)
        bank = self._line(lines[self.people["_T Slip Bank"]].name).request
        till = self._line(lines[self.people["_T Slip Till"]].name).request
        self.assertEqual(frappe.db.get_value(B2B, till, ["command_id", "receiver_shortcode"]),
                         ("BusinessBuyGoods", TILL))
        self._b2b_result(bank)
        self._b2b_result(till, public=f"{TILL} - T SLIP TILL")
        self.assertEqual(len(self._run_b2c(paid)), 1)
        for line in lines.values():
            self.assertEqual(self._line(line.name).status, "Paid", line.party)
        # the Payroll Entry's button now has only the employee with no account, who cannot be paid
        with pp._as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "No salary of .* can be paid by M-Pesa.*no approved M-Pesa "
                                   "account", schedule.from_payroll_entry, pe, b2b_settings=SETTINGS,
                                   b2c_settings=SETTINGS)
        # the headlines: everything paid but the one with no account
        with pp._as(PREPARER):
            totals = payroll.payroll_salary_totals(pe)
            status = payroll.salary_slip_status(slips["_T Slip Bank"])
        self.assertEqual((totals["paid"], totals["in_flight"], totals["planned"], totals["left"], totals["slips_left"]),
                         (92000, 0, 0, 8000, 1))
        self.assertEqual((status["paid"], status["left"]), (50000, 0))

    def test_a_slip_that_cannot_be_paid_is_listed_and_a_whole_payroll_is_its_schedule(self):
        pe, slips = self._payroll()
        with pp._as(PREPARER):
            answer = payroll.from_salary_slips(list(slips.values()), b2b_settings=SETTINGS, b2c_settings=SETTINGS)
        self.assertEqual(answer["lines"], 3)
        self.assertTrue(any("no approved M-Pesa account" in x for x in answer["excluded"]), answer["excluded"])
        self.assertIn("no approved M-Pesa account", frappe.db.get_value(SCHEDULE, answer["name"], "excluded"))
        # every submitted slip of the payroll was picked: it is the payroll's own schedule
        self.assertEqual((answer["payroll_entry"], payroll.live_schedule(pe)), (pe, answer["name"]))
        with pp._as(PREPARER):
            self.assertRaisesRegex(frappe.ValidationError, "already has M-Pesa Payment Schedule",
                                   schedule.from_payroll_entry, pe, b2b_settings=SETTINGS, b2c_settings=SETTINGS)

    # ------------------------------------------------------------ double pay and cancel
    def test_a_one_off_is_refused_while_the_payroll_schedule_plans_the_slip_and_after(self):
        pe, slips = self._payroll()
        slip = slips["_T Slip Bank"]
        with pp._as(PREPARER):
            planned = schedule.from_payroll_entry(pe, b2b_settings=SETTINGS, b2c_settings=SETTINGS)["name"]
            self.assertRaisesRegex(frappe.ValidationError, f"already planned on M-Pesa Payment Schedule {planned}",
                                   payroll.from_salary_slip, slip, b2b_settings=SETTINGS)
            self.assertRaisesRegex(frappe.ValidationError, "None of these salary slips can be paid.*already planned",
                                   payroll.from_salary_slips, [slip], b2b_settings=SETTINGS)
            schedule.close_schedule(planned)  # its planned lines are cancelled: the slip is free again
        _, (line,) = self._one_off(slip)
        self.assertEqual(flt(line.amount), 50000)
        # and the other way round: a second schedule for the payroll leaves the slip out
        with pp._as(PREPARER):
            again = schedule.from_payroll_entry(pe, b2b_settings=SETTINGS, b2c_settings=SETTINGS)
        self.assertNotIn(slip, [x.reference_name for x in self._lines(again["name"])])
        self.assertTrue(any(f"({slip}): nothing is left to pay" in x for x in again["excluded"]), again["excluded"])

    def test_a_slip_partly_planned_elsewhere_is_left_out_of_selected_slips_a_one_off_pays_the_rest(self):
        pe, slips = self._payroll()
        slip = slips["_T Slip Bank"]
        with pp._as(PREPARER):
            planned = schedule.from_payroll_entry(pe, b2b_settings=SETTINGS, b2c_settings=SETTINGS)["name"]
            doc = frappe.get_doc(SCHEDULE, planned)
            next(r for r in doc.lines if r.reference_name == slip).amount = 20000
            doc.save()
            self.assertRaisesRegex(frappe.ValidationError, f"None of these salary slips can be paid.*{slip}.*already "
                                   f"planned on M-Pesa Payment Schedule {planned}", payroll.from_salary_slips,
                                   [slip], b2b_settings=SETTINGS, b2c_settings=SETTINGS)
        _, (line,) = self._one_off(slip)
        self.assertEqual(flt(line.amount), 30000)

    def test_the_payroll_and_the_slip_cannot_be_cancelled_under_a_one_off(self):
        pe, slips = self._payroll()
        slip = slips["_T Slip Bank"]
        answer, (line,) = self._one_off(slip)
        entry = frappe.get_doc(PAYROLL, pe)
        self.assertRaisesRegex(frappe.ValidationError, f"{answer['name']}.*delete it first", payroll.before_cancel,
                               entry)
        paid = self._pay([line.name], user=PAYER)
        self.assertRaisesRegex(frappe.ValidationError, "paid or on their way", payroll.before_cancel, entry)
        self._send_b2b(paid, user=PAYER)
        self._b2b_result(self._line(line.name).request)
        self.assertRaisesRegex(frappe.ValidationError, "paid or on their way", payroll.slip_before_cancel,
                               frappe.get_doc(SLIP, slip))
        self.assertRaisesRegex(frappe.ValidationError, "paid or on their way", entry.cancel)
        self.assertEqual(frappe.db.get_value(PAYROLL, pe, "docstatus"), 1)

    # ------------------------------------------------------------ who
    def test_who_may_prepare_and_who_may_see_the_figures(self):
        pe, slips = self._payroll()
        slip = slips["_T Slip Bank"]
        for user, message in ((ACCOUNTS_ONLY, "salaries are shown only"), (HR_ONLY, "needs create on it"),
                              (PAYER, "this needs one of the roles"), (SYSTEM_MANAGER, "this needs one of the roles")):
            with pp._as(user):
                self.assertRaisesRegex(frappe.PermissionError, message, payroll.from_salary_slip, slip,
                                       b2b_settings=SETTINGS)
                self.assertRaises(frappe.PermissionError, payroll.from_salary_slips, [slip], b2b_settings=SETTINGS)
        with pp._as(ACCOUNTS_ONLY):
            self.assertRaises(frappe.PermissionError, payroll.salary_slip_status, slip)
            self.assertRaises(frappe.PermissionError, payroll.payroll_salary_totals, pe)
        with pp._as(HR_ONLY):  # reads the slip and sees salaries, may not make a schedule: no accounts offered
            status = payroll.salary_slip_status(slip)
        self.assertEqual((status["can_prepare"], status["accounts"], status["left"]), (False, [], 50000))
        with pp._as(PREPARER):
            status = payroll.salary_slip_status(slip)
        self.assertTrue(status["can_prepare"])
        self.assertEqual([a["name"] for a in status["accounts"]], [self.slip_accounts[("_T Slip Bank",
                                                                                       party_accounts.BANK)]])
        self.assertEqual(frappe.db.count(SCHEDULE, {"title": ["like", f"%{slip}%"]}), 0)

    def test_a_salary_row_is_refused_before_its_slip_is_read(self):
        # verification K5: an Accounts Manager (create on B2C Payment Disbursement, no HR role) types a slip and an
        # amount. More than the slip owes, or a slip with nothing left, gets the same refusal: its net pay cannot be
        # learned from which message comes back
        _, slips = self._payroll()

        def disbursement(first, amount):
            return frappe.get_doc({
                "doctype": pp.DISB, "naming_series": "MPESA-B2C-.YYYY.-", "company": t.COMPANY,
                "posting_date": nowdate(), "payment_type": "Mpesa Disbursement", "party_type": "Employee",
                "transaction_to_pay_against": SLIP, "mode_of_payment": f"Mpesa-{SETTINGS}", "paid_from": self.gl,
                "paid_from_account_currency": "KES", "paid_to": self.payable, "paid_to_account_currency": "KES",
                "paid_amount": amount, "source_exchange_rate": 1, "target_exchange_rate": 1,
                "references": [{"reference_doctype": SLIP, "reference_name": slips[first], "party_type": "Employee",
                                "party": self.people[first], "allocated_amount": amount, "partyb": PHONE,
                                "currency": "KES", "exchange_rate": 1}]})

        # the slip paid in full by an earlier B2C payout (written as its Result leaves it)
        paid = frappe.get_doc({"doctype": REQ, "amount": 30000, "status": "Paid", "docstatus": 1,
                               "reference_doctype": SLIP, "reference_name": slips["_T Slip Phone"],
                               "party_type": "Employee", "party": self.people["_T Slip Phone"], "phone_number": PHONE,
                               "mpesa_settings": SETTINGS, "transaction_id": "TSLIPK5" + frappe.generate_hash(length=3),
                               "creation": now_datetime(), "modified": now_datetime(), "owner": "Administrator"})
        paid.name = f"_T-B2C-SLIP-{frappe.generate_hash(length=6)}"
        paid.db_insert()
        with pp._as(BOOKS):
            # 90,000 is more than the slip owes (50,000.40); the other slip has nothing left to pay
            self.assertRaisesRegex(frappe.PermissionError, "salaries are shown only",
                                   disbursement("_T Slip Bank", 90000).insert)
            self.assertRaisesRegex(frappe.PermissionError, "salaries are shown only",
                                   disbursement("_T Slip Phone", 1000).insert)
        self.assertEqual(frappe.db.count(pp.DISB, {"owner": BOOKS}), 0)

    def test_who_may_not_read_employee_accounts_pays_the_default_only(self):
        _, slips = self._payroll()
        slip = slips["_T Slip Phone"]  # the phone is the default, a bank account the other
        bank = self.slip_accounts[("_T Slip Phone", party_accounts.BANK)]
        frappe.share.add_docshare(SLIP, slip, ACCOUNTS_PAYER, read=1, flags={"ignore_share_permission": True})
        with pp._as(ACCOUNTS_PAYER):
            self.assertFalse(frappe.has_permission(EMPACC, "read"))
            status = payroll.salary_slip_status(slip)
            self.assertTrue(status["can_prepare"])
            self.assertEqual([(a["destination"], a["expected_name"]) for a in status["accounts"]],
                             [(PHONE, None)])  # round 4.3: numbers in full (the default)
            self.assertRaisesRegex(frappe.PermissionError, f"you may not read Employee M-Pesa Account {bank}",
                                   payroll.from_salary_slip, slip, payee_account=bank, b2b_settings=SETTINGS)
            answer = payroll.from_salary_slip(slip, b2c_settings=SETTINGS)
        self.assertEqual((answer["route"], answer["amount"]), ("B2C Phone", 30000))
        with pp._as(PREPARER):  # HR reads the accounts: both offered
            self.assertEqual(len(payroll.salary_slip_status(slip)["accounts"]), 2)

    def test_the_shared_administrator_login_does_not_pay_a_one_off(self):
        # Administrator is refused at Pay Selected (permissions.require_named_user in prepare_payment: the
        # round 4 send safety work package; this test needs it merged)
        _, slips = self._payroll()
        _, (line,) = self._one_off(slips["_T Slip Bank"])
        self.assertRaises(frappe.PermissionError, schedule.prepare_payment, [line.name])
        self.assertEqual(self._line(line.name).status, "Planned")
