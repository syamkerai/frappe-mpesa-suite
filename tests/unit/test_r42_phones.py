"""Safaricom's newer lines (0118 / 0119, 0140-0143, 0180-0182) and mistyped Kenyan numbers: one rule
for what B2C may pay (utils.phone.payable_msisdn) on claim payee lines, pasted payees, payee accounts
and the B2C row; an Employee's Cell Number checked when it is typed; Pay employee (phone) refusing a
Cell Number B2C cannot pay before any payout is made; an approved phone found however its number
is written. (No STK number guessed from its last 9 digits: mpsa_suite/money_in/test_phones.py, on a bench.)"""

import importlib

import fake_frappe
import pytest
from test_claim_payees import PHONE, payee
from test_claim_payees import env as claims_env  # noqa: F401 (the fixture)
from test_employee_documents import EC, PREPARER, claim, env  # noqa: F401 (env: the fixture)
from test_payee_accounts import env as accounts_env  # noqa: F401 (the fixture)
from test_payee_accounts import new
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture env extends)

_dict = fake_frappe._dict


# ---------------------------------------------------------------- claim payee lines and pasted payees
@pytest.mark.parametrize("phone", ["0118 444 514", "0119444514", "0142 000 280", "254182000280"])
def test_a_new_safaricom_line_is_taken_without_a_warning(claims_env, phone):
    row, warnings = payee(payee_type=PHONE, phone=phone), []
    claims_env.cp.clean_line(row, set(), warnings)
    assert row.phone.startswith("2541") and len(row.phone) == 12 and warnings == []


@pytest.mark.parametrize("phone", ["0152 000 280", "0122000280", "25416055453"])
def test_a_number_b2c_cannot_pay_is_refused_on_the_claim_line(claims_env, phone):
    with pytest.raises(claims_env.frappe.ValidationError, match="not a Kenyan mobile number M-Pesa can pay"):
        claims_env.cp.clean_line(payee(payee_type=PHONE, phone=phone), set(), [])


def test_pasted_new_safaricom_lines_are_taken_and_other_networks_still_refused(claims_env):
    cp = claims_env.cp
    rows = cp.table_rows(cp.parse_text("\n".join([
        "New 0118, 0118 444 514, 100",
        "New 0142, 0142 000 280, 100",
        "New 0182, 254182000281, 100",
        "Airtel, 0733111222, 100",
        "Airtel 010, 0101 111 222, 100",
        "Not In Use, 0152000280, 100",
        "Short, 25416055453, 100",
    ])))
    checked = cp.check_pasted(rows, "EMP-1", [], cp.employee_destinations())
    problems = {r["payee_name"]: "; ".join(r["errors"]) for r in checked}
    assert problems["New 0118"] == problems["New 0142"] == problems["New 0182"] == ""
    assert "does not start like a Safaricom number" in problems["Airtel"]
    assert "does not start like a Safaricom number" in problems["Airtel 010"]
    assert "not a Kenyan mobile number M-Pesa can pay" in problems["Not In Use"]
    assert "not a Kenyan mobile number M-Pesa can pay" in problems["Short"]


# ---------------------------------------------------------------- payee accounts
@pytest.mark.parametrize("doctype", ["Supplier M-Pesa Account", "Employee M-Pesa Account"])
def test_a_payee_account_on_a_new_safaricom_line_saves_without_a_warning(accounts_env, doctype):
    doc = new(accounts_env, doctype, account_type="Phone (B2C)", phone="0142 000 280", expected_name="Jane Wanjiru")
    accounts_env.pa.validate_account(doc)
    assert doc.phone == "254142000280" and not accounts_env.frappe.messages


@pytest.mark.parametrize("doctype", ["Supplier M-Pesa Account", "Employee M-Pesa Account"])
def test_a_payee_account_b2c_cannot_pay_is_refused(accounts_env, doctype):
    doc = new(accounts_env, doctype, account_type="Phone (B2C)", phone="0152 000 280", expected_name="Jane Wanjiru")
    with pytest.raises(accounts_env.frappe.ValidationError, match="01XX XXX XXX"):
        accounts_env.pa.validate_account(doc)


# ---------------------------------------------------------------- an Employee's Cell Number
class Employee(_dict):
    def __init__(self, cell_number, before=None, new=False):
        super().__init__(cell_number=cell_number)
        self._before, self._new = before, new

    def is_new(self):
        return self._new

    def has_value_changed(self, field):
        return self.get(field) != self._before


@pytest.fixture
def ea(claims_env):
    return importlib.import_module("mpsa_suite.money_out.employee_accounts")


@pytest.mark.parametrize("typed", ["25416055453", "0727 123 4567", "072712345", "+254 7271234567", "716055453 1",
                                   "0121 234 567", "0152000280", "0144 123 456", "+254 152 000 280"])
def test_a_mistyped_kenyan_cell_number_is_refused_when_it_is_typed(claims_env, ea, typed, masked_numbers):
    with pytest.raises(claims_env.frappe.ValidationError, match="is not a Kenyan mobile number") as caught:
        ea.check_cell_number(Employee(typed, before="0716 055 453"))
    assert "".join(c for c in typed if c.isdigit()) not in str(caught.value)  # the number itself is not shown
    assert "254111234567" in str(caught.value)  # how to write it with 254


@pytest.mark.parametrize("typed", ["0111 444 514", "254118444514", "+254 142 000 280", "0722000111", "", "+44 7911 123456",
                                   "12025550123", "00447911123456", "020 2345678", "+254 20 2345678", "0803 123 4567",
                                   "0", "N/A"])
def test_a_kenyan_mobile_a_landline_or_a_foreign_number_is_left_alone(claims_env, ea, typed):
    doc = Employee(typed, new=True)
    ea.check_cell_number(doc)
    assert doc.cell_number == typed and not claims_env.frappe.messages


@pytest.mark.parametrize("typed", ["+254 0712 345 678", "2540712345678", "00254 712 345 678", "0254712345678",
                                   "+00254 111 444 514"])
def test_a_cell_number_with_a_prefix_too_many_is_stored_in_254_form(claims_env, ea, typed):
    doc = Employee(typed, before="")
    ea.check_cell_number(doc)
    assert doc.cell_number in ("254712345678", "254111444514")
    assert "saved as 254" in claims_env.frappe.messages[-1]


def test_a_new_employee_copied_with_another_ones_mistyped_number_is_saved_with_a_warning(claims_env, ea):
    # HRMS Employee Transfer with a new Employee ID (and Duplicate) inserts a copy of the employee
    claims_env.frappe.db.table("Employee")["EMP-OLD"] = {"name": "EMP-OLD", "cell_number": "25416055453"}
    doc = Employee("25416055453", new=True)
    doc.name = "EMP-NEW"
    ea.check_cell_number(doc)
    assert "copied from another Employee" in claims_env.frappe.messages[-1]
    with pytest.raises(claims_env.frappe.ValidationError):  # typed fresh: refused
        ea.check_cell_number(Employee("25418000851", new=True))


def test_an_employee_saved_without_changing_its_cell_number_is_not_refused(ea):
    ea.check_cell_number(Employee("25416055453", before="25416055453"))  # HR's other edits still save


def test_the_cell_number_check_is_an_employee_validate_hook(claims_env):
    hooks = importlib.import_module("mpsa_suite.hooks")
    assert hooks.doc_events["Employee"]["validate"] == "mpsa_suite.money_out.employee_accounts.check_cell_number"


# ---------------------------------------------------------------- Pay employee (phone) to a Cell Number
def test_pay_employee_by_phone_pays_a_cell_number_in_254_form(env):
    env.frappe.db.table("Employee")["EMP-3"]["cell_number"] = "0118 444 514"
    assert env.ed.b2c_phone("EMP-3") == ("254118444514", None)


def test_pay_employee_by_phone_refuses_a_cell_number_b2c_cannot_pay_before_any_payout(env, masked_numbers):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    env.frappe.db.table("Employee")["EMP-3"]["cell_number"] = "25416055453"
    claim(env, "HR-EXP-3", employee="EMP-3")
    assert env.ed.b2c_phone("EMP-3") == ("25416055453", None)  # as typed: the row refuses it too
    problem = env.ed.cell_number_problem("EMP-3", "25416055453")
    assert problem.startswith("The Cell Number on EMP-3 (ending 453 (11 digits)) is not a Kenyan mobile number")
    with pytest.raises(env.frappe.ValidationError, match="The Cell Number on EMP-3"):
        env.ed.prepare_b2c(EC, "HR-EXP-3")
    assert env.ed.cell_number_problem("EMP-3", "254118444514") is None
    assert env.ed.cell_number_problem("EMP-3", "") is None  # no number: _no_phone says why
    # an account approved before r4.2 with a number outside the blocks in use: the account is named
    assert env.ed.cell_number_problem("EMP-3", "254122345678", "MEA-7").startswith(
        "The approved phone on MEA-7 (2541****5678) is not a number M-Pesa can pay")


def test_the_form_says_why_a_cell_number_cannot_be_paid(env, masked_numbers):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    env.frappe.db.table("Employee")["EMP-3"]["cell_number"] = "25416055453"
    claim(env, "HR-EXP-3", employee="EMP-3")
    status = env.ed.document_status(EC, "HR-EXP-3")
    assert status["phone_problem"].startswith("The Cell Number on EMP-3 (ending 453 (11 digits))")
    env.frappe.db.table("Employee")["EMP-3"]["cell_number"] = "0711 000 333"
    assert env.ed.document_status(EC, "HR-EXP-3")["phone_problem"] is None


def test_pay_by_mpesa_on_the_list_skips_a_cell_number_b2c_cannot_pay(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User", "HR Manager"}
    env.frappe.db.table("Employee")["EMP-3"]["cell_number"] = "25416055453"
    claim(env, "HR-EXP-3", employee="EMP-3")
    out = env.ed.prepare_bulk(EC, ["HR-EXP-3"])
    assert not out.get("b2c") and out["skipped"][0]["name"] == "HR-EXP-3"
    assert out["skipped"][0]["reason"].startswith("The Cell Number on EMP-3")


# ---------------------------------------------------------------- an approved phone however it is written
def test_an_approved_phone_is_found_however_its_number_is_written(env, monkeypatch):
    accounts = [_dict(name="MEA-9", phone="254111444514", is_default=1)]
    monkeypatch.setattr(env.ed.party_accounts, "accounts_of", lambda doctype, party, types: accounts)
    monkeypatch.setattr(env.ed.party_accounts, "usable_problem", lambda acc, party, doctype: None)
    for typed in ("0111 444 514", "+254111444514", "254111444514", "111444514"):
        assert env.ed._approved_phone("EMP-9", typed).name == "MEA-9", typed
    assert env.ed._approved_phone("EMP-9", "0110444514") is None
    assert env.ed._approved_phone("EMP-9", "") is None
