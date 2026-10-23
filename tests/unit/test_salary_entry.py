"""The salary Journal Entry of an M-Pesa salary payout (money_out.salary_entries), for both routes,
on the in-memory site of b2b_fixture (SQL on SQLite).

Covers: the payroll payable cleared once (the slip's net pay less what was paid before), the
round-off row for whole shillings (debit or credit), party Employee only when Payroll Settings
books payroll per employee, the payable account and cost center taken from the Payroll Entry,
the receipt as Cheque No on the completion date, the remark fields per version, the payout tag;
salary_slip_paid counting Paid B2B salaries and not counting their tagged entries; the B2B
booking (automatic, never twice, Make Entry) and the B2C booking finding an entry with no party.
"""

import datetime
import importlib
import sys
import types

import pytest

import b2b_fixture

B2B, JE, JEA = b2b_fixture.B2B, b2b_fixture.JE, b2b_fixture.JEA
SLIP, PAYROLL = "Salary Slip", "Payroll Entry"
GL, BANK = b2b_fixture.GL, b2b_fixture.BANK_ACCOUNT
COMPLETED = datetime.datetime(2026, 10, 17, 9, 0, 5)


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        db = env.frappe.db
        db.columns = db.columns | {(B2B, "salary_slip")}
        db.table("Company")["Astral"] = {"name": "Astral", "round_off_account": "Round Off - AIL",
                                         "round_off_cost_center": "Main - AIL"}
        db.table(PAYROLL)["PAY-1"] = {"name": "PAY-1", "company": "Astral", "docstatus": 1,
                                      "payroll_payable_account": "Payroll Payable - AIL", "cost_center": "HQ - AIL"}
        db.table("Payroll Settings")["Payroll Settings"] = {"process_payroll_accounting_entry_based_on_employee": 0}
        env.slip = lambda name="SAL-1", **kw: b2b_fixture._row(env, SLIP, name, {
            "docstatus": 1, "status": "Submitted", "employee": "EMP-1", "employee_name": "Jane Wanjiru",
            "company": "Astral", "currency": "KES", "payroll_entry": "PAY-1", "net_pay": 50000.40,
            "rounded_total": 50000}, kw)
        env.salary = importlib.import_module("mpsa_suite.money_out.salary_entries")
        env.meta_fields[JE] = None  # every field (v16: custom_remark)
        yield env


def build(env, amount=50000, before=0.0, tag=("mpesa_b2b_payment_request", "MB2B-1")):
    return env.salary.build("SAL-1", amount, "QRS1", COMPLETED.date(), GL, BANK, "M-Pesa salary", before, *tag)


def rows(je):
    return [dict(r) for r in je._row["accounts"]]


# ---------------------------------------------------------------- how a payout clears a salary
@pytest.mark.parametrize("net, before, amount, expected", [
    (50000.40, 0, 50000, (50000.40, -0.40, None)),  # whole shillings: 0.40 to round off (credit)
    (49999.60, 0, 50000, (49999.60, 0.40, None)),  # rounded up: 0.40 to round off (debit)
    (50000.40, 20000, 30000, (30000.40, -0.40, None)),  # the rest after an earlier payout
    (50000.40, 0, 20000, (20000, 0.0, None)),  # a part: debits what was paid
])
def test_split(env, net, before, amount, expected):
    assert env.salary.split(net, before, amount) == expected


def test_split_flags_a_salary_with_nothing_left_or_less_than_paid(env):
    assert "nothing was left of its net pay" in env.salary.split(100, 100, 50)[2]
    payable, round_off, problem = env.salary.split(100, 0, 150)
    assert (payable, round_off) == (150, 0.0) and "more than the KES 100 it had left" in problem


# ---------------------------------------------------------------- the entry
def test_the_entry_clears_the_payroll_payable_with_a_round_off_row(env):
    env.slip()
    je, problem = build(env)
    assert problem is None
    assert (je.voucher_type, je.company, je.cheque_no, je.posting_date, je.cheque_date) == (
        "Bank Entry", "Astral", "QRS1", COMPLETED.date(), COMPLETED.date())
    payable, round_off, credit = rows(je)
    assert (payable["account"], payable["debit_in_account_currency"], payable["reference_type"],
            payable["reference_name"], payable["cost_center"]) == ("Payroll Payable - AIL", 50000.40, PAYROLL, "PAY-1",
                                                                  "HQ - AIL")
    assert "party" not in payable and payable["mpesa_b2b_payment_request"] == "MB2B-1"
    assert (round_off["account"], round_off["credit_in_account_currency"], round_off["cost_center"]) == (
        "Round Off - AIL", 0.40, "Main - AIL")
    assert (credit["account"], credit["credit_in_account_currency"], credit["bank_account"]) == (GL, 50000, BANK)
    debit = payable["debit_in_account_currency"]
    assert round(debit - round_off["credit_in_account_currency"] - credit["credit_in_account_currency"], 2) == 0


def test_party_employee_only_when_payroll_is_booked_per_employee(env):
    env.slip()
    env.frappe.db.table("Payroll Settings")["Payroll Settings"]["process_payroll_accounting_entry_based_on_employee"] = 1
    je, _ = build(env)
    assert (rows(je)[0]["party_type"], rows(je)[0]["party"], je.party_not_required) == ("Employee", "EMP-1", False)


def test_a_rounded_up_salary_debits_round_off(env):
    env.slip(net_pay=49999.60)
    _, round_off, _ = rows(build(env)[0])
    assert (round_off["debit_in_account_currency"], round_off.get("credit_in_account_currency")) == (0.40, None)


def test_no_round_off_row_when_the_amount_is_exact_or_a_part(env):
    env.slip(net_pay=50000)
    assert len(rows(build(env)[0])) == 2
    env.slip(net_pay=50000.40)
    assert [r["debit_in_account_currency"] for r in rows(build(env, amount=20000)[0])[:1]] == [20000]


def test_remark_fields_per_version(env):
    env.slip()
    je, _ = build(env)
    assert (je.user_remark, je.custom_remark, je.remark) == ("M-Pesa salary", 1, "M-Pesa salary")
    env.meta_fields[JE] = {"user_remark"}  # v15: no custom_remark
    je, _ = build(env)
    assert je.user_remark == "M-Pesa salary" and "custom_remark" not in je._row


def test_the_tag_needs_its_column(env):
    env.slip()
    env.meta_fields[JEA] = {"b2c_payment_disbursement"}  # not migrated yet
    assert "mpesa_b2b_payment_request" not in rows(build(env)[0])[0]


def test_round_off_needs_the_company_account(env):
    env.slip()
    env.frappe.db.table("Company")["Astral"]["round_off_account"] = None
    with pytest.raises(env.frappe.ValidationError, match="Set the Round Off Account on Company Astral"):
        build(env)


def test_a_slip_outside_a_payroll_entry_has_no_payable_to_clear(env):
    env.slip(payroll_entry=None)
    with pytest.raises(env.frappe.ValidationError, match="not part of a Payroll Entry"):
        build(env)


def test_a_salary_paid_already_is_flagged(env):
    env.slip()
    _, problem = build(env, before=50000.40)
    assert problem.startswith("SAL-1: nothing was left") and "Check that EMP-1 was not paid twice." in problem


# ---------------------------------------------------------------- what a slip has had paid
def test_salary_slip_paid_counts_paid_b2b_salaries_not_their_entries(env):
    disb = importlib.import_module("mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement")
    env.request("MB2B-1", status="Paid", amount=30000, salary_slip="SAL-1", employee="EMP-1", supplier=None)
    env.request("MB2B-2", status="Initiated", amount=5000, salary_slip="SAL-1", transaction_id=None)
    env.request("MB2B-3", status="Paid", docstatus=2, amount=1000, salary_slip="SAL-1")  # paid after its cancel
    # its salary entry (tagged) and a bank entry made by hand for the same employee and payroll
    env.je("ACC-JV-1", "RMB2B-1X", [
        {"account": "Payroll Payable - AIL", "debit_in_account_currency": 30000, "party_type": "Employee",
         "party": "EMP-1", "reference_type": PAYROLL, "reference_name": "PAY-1", "mpesa_b2b_payment_request": "MB2B-1"},
        {"account": GL, "credit_in_account_currency": 30000}], voucher_type="Bank Entry")
    env.je("ACC-JV-2", "CHQ-9", [
        {"account": "Payroll Payable - AIL", "debit_in_account_currency": 2000, "party_type": "Employee",
         "party": "EMP-1", "reference_type": PAYROLL, "reference_name": "PAY-1"},
        {"account": "Bank - AIL", "credit_in_account_currency": 2000}], voucher_type="Bank Entry")
    assert disb.salary_slip_paid("SAL-1", "EMP-1", "PAY-1") == 33000
    assert disb.salary_slip_paid("SAL-1", "EMP-1", "PAY-1", exclude_b2b="MB2B-1") == 3000


def test_salary_slip_paid_without_hrms_columns_counts_no_b2b(env):
    disb = importlib.import_module("mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement")
    env.frappe.db.columns = env.frappe.db.columns - {(B2B, "salary_slip")}
    env.request("MB2B-1", status="Paid", amount=30000, salary_slip="SAL-1")
    assert disb.salary_slip_paid("SAL-1") == 0


# ---------------------------------------------------------------- B2B: booked when M-Pesa confirms
def salary_request(env, name="MB2B-1", **kw):
    values = {"employee": "EMP-1", "employee_mpesa_account": "MEA-1", "supplier": None, "supplier_mpesa_account": None,
              "salary_slip": "SAL-1", "payroll_entry": "PAY-1", "amount": 50000, "receiver_shortcode": "247247",
              "account_reference": "0123456789", "entry_attempted_on": None, "transaction_id": "QSAL1", **kw}
    return env.request(name, **values)


def test_a_paid_salary_request_books_the_salary_entry_once(env):
    env.slip()
    salary_request(env)
    entry = env.entries.book_automatically("MB2B-1")
    request = env.frappe.db.table(B2B)["MB2B-1"]
    assert entry == request["journal_entry"] and request["entry_status"] == "Entry Made"
    lines = [r for r in env.frappe.db.table(JEA).values() if r["parent"] == entry]
    assert {r["account"] for r in lines} == {"Payroll Payable - AIL", "Round Off - AIL", GL}
    assert next(r for r in lines if r["account"] == "Payroll Payable - AIL")["mpesa_b2b_payment_request"] == "MB2B-1"
    assert env.frappe.db.table(JE)[entry]["docstatus"] == 1
    assert "b2c-pay:Salary Slip:SAL-1" in env.locks  # the slip's lock, B2C's key
    inserted = len(env.inserted)
    env.entries.book_automatically("MB2B-1")
    assert len(env.inserted) == inserted  # never booked twice: the receipt is on it


def test_a_salary_paid_twice_is_left_needing_a_check_and_alerted(env):
    env.slip()
    env.request("MB2B-0", status="Paid", amount=50000, salary_slip="SAL-1", employee="EMP-1", supplier=None,
                transaction_id="R0")  # the slip was paid by another request
    salary_request(env)
    env.entries.book_automatically("MB2B-1")
    request = env.frappe.db.table(B2B)["MB2B-1"]
    assert request["entry_status"] == "Needs Checking" and "was not paid twice" in request["entry_note"]
    assert env.alerts
    env.entries.refresh_entry(env.frappe.get_doc(B2B, "MB2B-1"))  # the sweep keeps it
    assert env.frappe.db.table(B2B)["MB2B-1"]["entry_status"] == "Needs Checking"


def test_a_payment_to_an_employee_with_no_slip_waits_for_make_entry(env):
    salary_request(env, salary_slip=None, payroll_entry=None, entry_mode="Make the entry later")
    assert env.entries.book_automatically("MB2B-1") is None
    assert env.frappe.db.table(B2B)["MB2B-1"]["entry_status"] == "Entry Not Made"
    env.frappe.db.table(B2B)["MB2B-1"]["entry_mode"] = "Book automatically"
    env.entries.book_automatically("MB2B-1")
    assert env.frappe.db.table(B2B)["MB2B-1"]["entry_note"] == "Paid to EMP-1 with no salary slip: press Make Entry."


def test_an_employee_receipt_on_a_payment_entry_is_off_target(env):
    env.slip()
    salary_request(env)
    env.pe("ACC-PAY-9", "QSAL1", party_type="Employee", party="EMP-1", paid_amount=50000)
    env.entries.book_automatically("MB2B-1")
    request = env.frappe.db.table(B2B)["MB2B-1"]
    assert request["entry_status"] == "Needs Checking" and "a salary is booked on the payroll payable of PAY-1" in (
        request["entry_note"])


def test_make_entry_books_a_salary_and_supplier_kinds_are_refused(env):
    env.slip()
    salary_request(env, salary_slip=None, payroll_entry=None, entry_mode="Make the entry later")
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.ValidationError, match="went to an employee"):
        env.entries.make_entry("MB2B-1", "Supplier Advance")
    result = env.entries.make_entry("MB2B-1", "Salary Slip", salary_slip="SAL-1")
    request = env.frappe.db.table(B2B)["MB2B-1"]
    assert (result["doctype"], request["salary_slip"], request["payroll_entry"], request["entry_status"]) == (
        JE, "SAL-1", "PAY-1", "Entry Made")
    with pytest.raises(env.frappe.ValidationError, match="is already on Journal Entry"):
        env.entries.make_entry("MB2B-1", "Salary Slip", salary_slip="SAL-1")


# ---------------------------------------------------------------- B2C: the schedule's phone salaries
def test_a_b2c_salary_entry_with_no_party_is_found_by_its_receipt_and_tag(env, monkeypatch):
    for name in ("mpsa_suite.accounting.payment_entry_service", "mpsa_suite.accounting.journal_service"):
        stub = types.ModuleType(name)
        stub.create_payment_entry_for_doc = stub.create_journal_entry = lambda *a, **kw: None
        monkeypatch.setitem(sys.modules, name, stub)
    post = importlib.import_module("mpsa_suite.services.b2c_postprocess_service")
    env.slip()
    env.frappe.db.table("B2C Disbursement Request")["B2C-1"] = {
        "name": "B2C-1", "docstatus": 1, "status": "Paid", "amount": 50000, "transaction_id": "QB2C1",
        "mpesa_settings": b2b_fixture.SETTINGS, "transaction_completed_datetime": COMPLETED, "creation": COMPLETED}
    ref = types.SimpleNamespace(name="ROW-1", reference_doctype=SLIP, reference_name="SAL-1", party="EMP-1",
                                party_type="Employee", b2c_disbursement_request="B2C-1", reference_no="QB2C1",
                                allocated_amount=50000, parenttype="B2C Payment Disbursement",
                                get=lambda key, default=None: "L1" if key == "mpesa_schedule_line" else default)
    disbursement = types.SimpleNamespace(name="MPESA-B2C-1")
    name = env.salary.for_b2c(disbursement, ref, submit=True)
    lines = [r for r in env.frappe.db.table(JEA).values() if r["parent"] == name]
    payable = next(r for r in lines if r["account"] == "Payroll Payable - AIL")
    assert (payable["b2c_payment_disbursement"], payable.get("party")) == ("MPESA-B2C-1", None)
    assert env.frappe.db.table(JE)[name]["cheque_no"] == "QB2C1"
    assert post.booked_docstatus("MPESA-B2C-1", ref) == 1  # so it is never booked again
