"""Salaries from Salary Slips (round 4, money_out.payroll): Pay by M-Pesa on one slip (a one-line
payment schedule to any approved account of the employee) and the list's Pay Selected Slips by
M-Pesa (one schedule, each slip to its employee's default account of any type), on the in-memory
site of test_payroll_schedule.

Covers: the line per account type (phone by B2C, bank account / paybill by B2B Paybill, till by
B2B Till), also for the Payroll Entry's own builder; the chosen account checked; the double-pay
guards both ways (a line planned elsewhere, the Payroll Entry's builder after a one-off, the
locking re-read); the Payroll Entry's cancel; the list action's reasons for every slip left out,
one company, the schedule naming the Payroll Entry only for a whole payroll; who may prepare and
who may see the figures (salary visibility); the slip's and the payroll's headlines (paid, in
flight, planned, left) and their grouped reading; the form and list scripts.
"""

import datetime
import itertools
import re
from pathlib import Path

import pytest

import fake_frappe
from test_payroll_schedule import (  # noqa: F401  (env is a fixture)
    ACCOUNTS, DISB, EMPACC, LINE, NOW, PAYROLL, PHONE, REQ, ROW, SCHEDULE, SETTINGS, SLIP, RowDict, ScheduleDoc,
    bank_line, env, schedule_row,
)
from test_schedule import day

ROOT = Path(__file__).resolve().parents[2]
PREPARER_ROLES = {"Accounts User", "HR User"}  # prepares (PREPARE_ROLES), sees salaries, creates schedules


class NamedSchedule(ScheduleDoc):
    """A schedule with its own name and line names, so several can be made in one test."""

    def append(self, table, row):
        lines = self.setdefault("lines", [])
        line = RowDict(row, idx=len(lines) + 1, name=f"{self.name}-L{len(lines) + 1}")
        lines.append(line)
        return line


@pytest.fixture
def site(env):
    frappe = env.frappe
    seq = itertools.count(1)
    plain_new_doc = frappe.new_doc

    def new_doc(doctype):
        if doctype == SCHEDULE:
            return NamedSchedule(env=env, flags=fake_frappe._dict(), lines=[], doctype=SCHEDULE,
                                 name=f"MPS-S{next(seq)}")
        return plain_new_doc(doctype)

    frappe.new_doc = new_doc
    frappe.session.user, frappe.user_roles = ACCOUNTS, set(PREPARER_ROLES)
    yield env


def account(s, name, employee, account_type, default=1, **kw):
    active = NOW - datetime.timedelta(days=2)
    row = {"name": name, "docstatus": 1, "employee": employee, "account_type": account_type, "phone": None,
           "shortcode": None, "account_number": None, "bank": None, "expected_name": "Name", "active_from": active,
           "is_default": default, "name_mismatch": 0, **kw}
    s.frappe.db.table(EMPACC)[name] = row
    return row


def destination_of(s, account_type):
    pa = s.pa
    return {pa.PHONE: {"phone": "254722000555"}, pa.PAYBILL: {"shortcode": "208911", "account_number": "ACC1"},
            pa.TILL: {"shortcode": "835811"},
            pa.BANK: {"shortcode": "522522", "account_number": "1100223344", "bank": "KCB"}}[account_type]


def schedule_of(s, name):
    header = s.frappe.db.table(SCHEDULE)[name]
    lines = sorted((r for r in s.frappe.db.table(LINE).values() if r["parent"] == name), key=lambda r: r["idx"])
    return header, lines


# ---------------------------------------------------------------- one slip: Pay by M-Pesa
def test_a_one_off_salary_is_a_one_line_payroll_schedule(site):
    answer = site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS, planned_date=day(1).isoformat())
    header, lines = schedule_of(site, answer["name"])
    assert (header["purpose"], header["payroll_entry"], header["payroll_doctype"], header["company"]) == (
        "Payroll", None, None, "Astral")
    assert header["title"].startswith("Salary SAL-1 Jane Wanjiru (1 Oct to 31 Oct 2026)")
    assert [(r["party"], r["payee_account"], r["route"], r["destination"], r["amount"], r["reference_name"],
             r["planned_date"], r["prepared_by"], r["status"]) for r in lines] == [
        ("EMP-1", "MEA-1", "B2B Paybill", "247247 / 0123456789", 50000, "SAL-1", day(1), ACCOUNTS, "Planned")]
    assert (answer["amount"], answer["route"], answer["payee_account"]) == (50000, "B2B Paybill", "MEA-1")
    # the payroll's schedule lock, then the slip's own (R4)
    assert site.locks.index("payroll-schedule:PAY-1") < site.locks.index("b2c-pay:Salary Slip:SAL-1")


@pytest.mark.parametrize("kind, route", [("PHONE", "B2C Phone"), ("BANK", "B2B Paybill"), ("PAYBILL", "B2B Paybill"),
                                         ("TILL", "B2B Till")])
def test_the_chosen_account_decides_the_route(site, kind, route):
    account_type = getattr(site.pa, kind)
    account(site, "MEA-9", "EMP-2", account_type, default=0, **destination_of(site, account_type))
    answer = site.payroll.from_salary_slip("SAL-2", payee_account="MEA-9", b2b_settings=SETTINGS,
                                           b2c_settings=SETTINGS)
    _, (line,) = schedule_of(site, answer["name"])
    assert (line["payee_account"], line["route"], line["amount"]) == ("MEA-9", route, 30000)


def test_the_pay_from_of_the_route_is_needed(site):
    with pytest.raises(site.frappe.ValidationError, match="SAL-2 cannot be paid by M-Pesa: paid to a phone: pick Pay "
                                                          "From \\(B2C\\)"):
        site.payroll.from_salary_slip("SAL-2", b2b_settings=SETTINGS)
    with pytest.raises(site.frappe.ValidationError, match="pick Pay From \\(B2B\\)"):
        site.payroll.from_salary_slip("SAL-1", b2c_settings=SETTINGS)


@pytest.mark.parametrize("payee", ["MEA-1", "MEA-404", "MEA-DRAFT"])
def test_only_an_approved_account_of_the_employee_is_paid(site, payee):
    account(site, "MEA-DRAFT", "EMP-2", site.pa.PHONE, docstatus=0, phone="254722000666")
    with pytest.raises(site.frappe.ValidationError, match=f"{payee} is not an approved M-Pesa account of EMP-2"):
        site.payroll.from_salary_slip("SAL-2", payee_account=payee, b2c_settings=SETTINGS)
    assert not site.frappe.db.table(SCHEDULE)


@pytest.mark.parametrize("change, message", [
    (("slip", "payroll_entry", None), "it is not part of a Payroll Entry, so it has no payroll payable to clear"),
    (("payroll", "docstatus", 0), "its Payroll Entry PAY-1 is not submitted"),
    (("slip", "docstatus", 2), "it is cancelled"),
    (("slip", "docstatus", 0), "it is not submitted"),
    (("slip", "status", "Withheld"), "the salary is withheld"),
    (("slip", "currency", "USD"), "is in USD. M-Pesa pays KES only"),
])
def test_a_slip_that_cannot_be_paid_is_refused_with_the_reason(site, change, message):
    table, field, value = change
    site.frappe.db.table(SLIP if table == "slip" else PAYROLL)["SAL-1" if table == "slip" else "PAY-1"][field] = value
    with pytest.raises(site.frappe.ValidationError, match=f"SAL-1 cannot be paid by M-Pesa: .*{re.escape(message)}"):
        site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS)


def test_pay_on_cannot_be_before_today(site):
    with pytest.raises(site.frappe.ValidationError, match="Pay On cannot be before today"):
        site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS, planned_date=day(-1).isoformat())


# ---------------------------------------------------------------- double pay
def test_a_slip_planned_on_the_payroll_schedule_is_refused_with_its_name(site):
    schedule_row(site)
    bank_line(site)  # SAL-1, 50,000 planned on MPS-1
    with pytest.raises(site.frappe.ValidationError,
                       match="SAL-1 cannot be paid by M-Pesa: already planned on M-Pesa Payment Schedule MPS-1"):
        site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS)


def test_a_part_planned_elsewhere_leaves_the_rest(site):
    schedule_row(site)
    bank_line(site, amount=20000)
    answer = site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS)
    assert answer["amount"] == 30000


def test_the_payroll_builder_leaves_out_a_slip_paid_by_a_one_off_and_cannot_be_cancelled_under_it(site):
    one_off = site.payroll.from_salary_slip("SAL-2", b2c_settings=SETTINGS)["name"]
    answer = site.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    _, lines = schedule_of(site, answer["name"])
    assert [r["reference_name"] for r in lines] == ["SAL-1"]
    assert any(x.startswith("EMP-2 Otieno Ouma (SAL-2): nothing is left to pay") for x in answer["excluded"])
    pe = fake_frappe._dict(name="PAY-1")
    with pytest.raises(site.frappe.ValidationError, match=f"has M-Pesa Payment Schedule .*{one_off}.*delete it first"):
        site.payroll.before_cancel(pe)


def test_a_payout_committed_meanwhile_is_seen_by_the_locking_read(site, monkeypatch):
    real = site.owed.slip_left_to_pay

    def left(slip, **kw):
        result = real(slip, **kw)
        if kw.get("for_update") and slip == "SAL-1":  # another worker's payout, committed after this snapshot
            result.left, result.parts = 10000, [fake_frappe._dict(kind="b2b", name="MB2B-9", amount=40000,
                                                                   label="MB2B-9 Initiated 40,000")]
        return result

    monkeypatch.setattr(site.owed, "slip_left_to_pay", left)
    with pytest.raises(site.frappe.ValidationError, match="KES 50,000 is more than is left to pay now. SAL-1 owes "
                                                          "50,000, less MB2B-9 Initiated 40,000"):
        site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS)
    answer = site.payroll.from_salary_slips(["SAL-1", "SAL-2"], b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    assert any("(SAL-1): KES 50,000 is more than is left to pay now" in x for x in answer["excluded"])


# ---------------------------------------------------------------- who may prepare, who may see
@pytest.mark.parametrize("roles, denied, message", [
    ({"Accounts User"}, set(), "salaries are shown only to who may read Salary Slips"),
    ({"Accounts Manager"}, set(), "salaries are shown only to who may read Salary Slips"),
    ({"HR User"}, {(SCHEDULE, "create")}, "needs create on it"),
    ({"M-Pesa Payer"}, set(), "this needs one of the roles"),
    ({"System Manager"}, set(), "this needs one of the roles"),
    (PREPARER_ROLES, {(SLIP, "read")}, "no read on Salary Slip"),
])
def test_who_may_prepare_a_salary_payment(site, roles, denied, message):
    site.frappe.user_roles = set(roles)
    site.denied |= denied
    for call in (lambda: site.payroll.from_salary_slip("SAL-1", b2b_settings=SETTINGS),
                 lambda: site.payroll.from_salary_slips(["SAL-1"], b2b_settings=SETTINGS)):
        if (SLIP, "read") in denied:
            with pytest.raises((site.frappe.PermissionError, site.frappe.ValidationError), match=message + "|you may "
                                                                                                   "not read it"):
                call()
        else:
            with pytest.raises(site.frappe.PermissionError, match=message):
                call()
    assert not site.frappe.db.table(SCHEDULE)


def test_the_payroll_entry_button_needs_salary_visibility_too(site):
    site.frappe.user_roles = {"Accounts User"}
    with pytest.raises(site.frappe.PermissionError, match="salaries are shown only"):
        site.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS)


def test_the_figures_are_for_who_may_see_salaries(site):
    site.frappe.user_roles = {"Accounts User"}
    for call in (lambda: site.payroll.salary_slip_status("SAL-1"),
                 lambda: site.payroll.payroll_salary_totals("PAY-1")):
        with pytest.raises(site.frappe.PermissionError, match="salaries are shown only"):
            call()
    for roles, denied in (({"M-Pesa Payer"}, set()), ({"HR User"}, {(SCHEDULE, "create")})):
        site.frappe.user_roles = roles  # sees salaries, does not prepare: no accounts offered
        site.denied |= denied
        status = site.payroll.salary_slip_status("SAL-1")
        assert (status["can_prepare"], status["accounts"], status["b2b_settings"], status["left"]) == (
            False, [], [], 50000)
    site.denied.add((PAYROLL, "read"))
    with pytest.raises(site.frappe.PermissionError):
        site.payroll.payroll_salary_totals("PAY-1")


# ---------------------------------------------------------------- the list: Pay Selected Slips
def _more_slips(s):
    db = s.frappe.db
    db.table(PAYROLL)["PAY-2"] = dict(db.table(PAYROLL)["PAY-1"], name="PAY-2")
    db.table("Employee")["EMP-6"] = {"name": "EMP-6", "employee_name": "Till Person", "status": "Active",
                                     "company": "Astral"}
    db.table(SLIP)["SAL-6"] = dict(db.table(SLIP)["SAL-1"], name="SAL-6", employee="EMP-6", employee_name="Till Person",
                                   payroll_entry="PAY-2", net_pay=12000, rounded_total=12000)
    account(s, "MEA-6", "EMP-6", s.pa.TILL, shortcode="835811")


def test_selected_slips_make_one_schedule_with_mixed_routes_and_every_slip_left_out_listed(site):
    _more_slips(site)
    answer = site.payroll.from_salary_slips(["SAL-6", "SAL-1", "SAL-2", "SAL-3", "SAL-4", "SAL-X", "SAL-1"],
                                            b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    header, lines = schedule_of(site, answer["name"])
    assert [(r["party"], r["route"], r["amount"], r["reference_name"], r["prepared_by"]) for r in lines] == [
        ("EMP-1", "B2B Paybill", 50000, "SAL-1", ACCOUNTS), ("EMP-2", "B2C Phone", 30000, "SAL-2", ACCOUNTS),
        ("EMP-6", "B2B Till", 12000, "SAL-6", ACCOUNTS)]
    assert (answer["lines"], answer["total"], answer["payroll_entry"]) == (3, 92000, None)
    assert (header["purpose"], header["payroll_entry"], header["title"]) == (
        "Payroll", None, "Salaries: 3 salaries of PAY-1, PAY-2")
    excluded = answer["excluded"]
    assert "SAL-X: not found" in excluded
    assert any(x.startswith("EMP-3 Achieng Mary (SAL-3): no approved M-Pesa account") for x in excluded)
    assert any(x.startswith("EMP-4 Left Person (SAL-4): the employee is Left") for x in excluded)
    assert header["excluded"] == "\n".join(excluded)  # under the schedule's Not Included too


@pytest.mark.parametrize("change, reason", [
    (lambda s: s.frappe.db.table(SLIP)["SAL-2"].update(status="Withheld"), "the salary is withheld"),
    (lambda s: s.frappe.db.table(SLIP)["SAL-2"].update(docstatus=2), "it is cancelled"),
    (lambda s: s.frappe.db.table(EMPACC)["MEA-2"].update(docstatus=2), "no approved M-Pesa account"),
    (lambda s: (account(s, "MEA-7", "EMP-2", s.pa.BANK, default=0, **destination_of(s, s.pa.BANK)),
                  s.frappe.db.table(EMPACC)["MEA-2"].update(is_default=0)),
     "several approved M-Pesa accounts and none is the default"),
    (lambda s: (schedule_row(s), bank_line(s, "L2", party="EMP-2", payee_account="MEA-2", route="B2C Phone",
                                               destination=PHONE, amount=30000, reference_name="SAL-2")),
     "already planned on M-Pesa Payment Schedule MPS-1"),
    (lambda s: s.request("MB2B-0", status="Paid", amount=30000, salary_slip="SAL-2", employee="EMP-2",
                             supplier=None), "nothing is left to pay: SAL-2 owes 0"),
])
def test_a_slip_that_cannot_be_paid_is_listed_with_its_reason(site, change, reason):
    change(site)
    answer = site.payroll.from_salary_slips(["SAL-1", "SAL-2"], b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    assert [r["reference_name"] for r in schedule_of(site, answer["name"])[1]] == ["SAL-1"]
    (left_out,) = [x for x in answer["excluded"] if "(SAL-2)" in x]
    assert reason in left_out, left_out


def test_a_whole_payroll_selected_is_that_payrolls_schedule(site):
    answer = site.payroll.from_salary_slips(["SAL-1", "SAL-2", "SAL-3", "SAL-4"], b2b_settings=SETTINGS,
                                            b2c_settings=SETTINGS)
    header, _ = schedule_of(site, answer["name"])
    assert (answer["payroll_entry"], header["payroll_entry"], header["payroll_doctype"], header["title"]) == (
        "PAY-1", "PAY-1", PAYROLL, "Salaries PAY-1 (1 Oct to 31 Oct 2026)")
    with pytest.raises(site.frappe.ValidationError, match=f"PAY-1 already has M-Pesa Payment Schedule {answer['name']}"):
        site.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    # with a live schedule already, a second whole selection names no payroll (and finds nothing left)
    with pytest.raises(site.frappe.ValidationError, match="None of these salary slips can be paid"):
        site.payroll.from_salary_slips(["SAL-1", "SAL-2", "SAL-3", "SAL-4"], b2b_settings=SETTINGS,
                                       b2c_settings=SETTINGS)


def test_a_few_slips_never_take_the_payrolls_one_live_schedule(site):
    answer = site.payroll.from_salary_slips(["SAL-1", "SAL-2"], b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    assert schedule_of(site, answer["name"])[0]["payroll_entry"] is None
    assert site.payroll.live_schedule("PAY-1") is None
    # the Payroll Entry's button still works, for what the few slips left: here nothing
    with pytest.raises(site.frappe.ValidationError, match="No salary of PAY-1 can be paid by M-Pesa.*\\(SAL-1\\): "
                                                          "nothing is left to pay"):
        site.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    with pytest.raises(site.frappe.ValidationError, match="has M-Pesa Payment Schedule"):
        site.payroll.before_cancel(fake_frappe._dict(name="PAY-1"))


def test_the_selection_is_one_company_and_not_too_many(site):
    site.frappe.db.table(SLIP)["SAL-2"]["company"] = "Other Ltd"
    with pytest.raises(site.frappe.ValidationError, match="These salary slips are of Astral, Other Ltd: pick the slips "
                                                          "of one company"):
        site.payroll.from_salary_slips(["SAL-1", "SAL-2"], b2b_settings=SETTINGS)
    with pytest.raises(site.frappe.ValidationError, match="Tick the salary slips to pay"):
        site.payroll.from_salary_slips("[]")
    with pytest.raises(site.frappe.ValidationError, match="at most 500"):
        site.payroll.from_salary_slips([f"SAL-{i}" for i in range(501)])
    with pytest.raises(site.frappe.ValidationError, match="None of these salary slips can be paid by M-Pesa.*SAL-3.*"
                                                          "no approved M-Pesa account"):
        site.payroll.from_salary_slips('["SAL-3"]', b2b_settings=SETTINGS)
    assert not site.frappe.db.table(SCHEDULE)


# ---------------------------------------------------------------- the Payroll Entry's builder, any type
@pytest.mark.parametrize("kind, route", [("PHONE", "B2C Phone"), ("BANK", "B2B Paybill"), ("PAYBILL", "B2B Paybill"),
                                         ("TILL", "B2B Till")])
def test_the_payroll_pays_each_employee_to_the_default_of_any_type(site, kind, route):
    account_type = getattr(site.pa, kind)
    account(site, "MEA-3", "EMP-3", account_type, **destination_of(site, account_type))
    answer = site.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    lines = {r["party"]: r for r in schedule_of(site, answer["name"])[1]}
    assert (lines["EMP-3"]["payee_account"], lines["EMP-3"]["route"]) == ("MEA-3", route)


# ---------------------------------------------------------------- headlines
def _paid_and_planned(s):
    db = s.frappe.db
    s.request("MB2B-0", status="Paid", amount=20000, salary_slip="SAL-1", employee="EMP-1", supplier=None)
    s.request("MB2B-1", status="Initiated", amount=5000, salary_slip="SAL-1", employee="EMP-1", supplier=None)
    s.request("MB2B-2", status="Failed", amount=7000, salary_slip="SAL-1", employee="EMP-1", supplier=None)
    schedule_row(s)
    bank_line(s, amount=10000)
    db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Paid", "amount": 30000,
                              "reference_doctype": SLIP, "reference_name": "SAL-2"}
    # a B2C row on its way (no payout made yet), as open_payouts and the grouped query both see it
    db.table(DISB)["D-1"] = {"name": "D-1", "docstatus": 1}
    db.table(ROW)["D-1-R1"] = {"name": "D-1-R1", "parent": "D-1", "parenttype": DISB, "reference_doctype": SLIP,
                               "reference_name": "SAL-3", "allocated_amount": 20000, "payment_status": "Not Initiated",
                               "b2c_disbursement_request": None}
    s.b2c.append({"dt": SLIP, "dn": "SAL-3", "parent": "D-1", "allocated_amount": 20000,
                    "payment_status": "Not Initiated", "request_status": None})
    s.je("ACC-JV-1", "CHQ-1", [{"account": "Payroll Payable - AIL", "debit_in_account_currency": 15000,
                                  "party_type": "Employee", "party": "EMP-4", "reference_type": PAYROLL,
                                  "reference_name": "PAY-1"}], voucher_type="Bank Entry")


def test_the_slip_headline_shows_paid_in_flight_planned_and_left(site):
    _paid_and_planned(site)
    status = site.payroll.salary_slip_status("SAL-1")
    assert {k: status[k] for k in ("net_pay", "paid", "in_flight", "planned", "left", "payable", "other_paid",
                                   "schedules")} == {
        "net_pay": 50000, "paid": 20000, "in_flight": 5000, "planned": 10000, "left": 15000, "payable": 15000,
        "other_paid": 0, "schedules": ["MPS-1"]}
    # the bank entry paying EMP-4 stops M-Pesa for the whole payroll: the form shows it, offers no button
    assert [p[:42] for p in status["problems"]] == ["PAY-1 has a bank or cash entry not made by"]
    assert status["can_prepare"] is True
    assert [(a["name"], a["route"], a["destination"], a["is_default"], a["problem"]) for a in status["accounts"]] == [
        ("MEA-1", "B2B Paybill", "247247 / 0123456789", 1, None)]
    assert (status["default_account"], status["b2b_settings"], status["b2c_settings"]) == ("MEA-1", [SETTINGS],
                                                                                          [SETTINGS])
    bank = site.payroll.salary_slip_status("SAL-4")  # paid by the bank entry, outside M-Pesa
    assert (bank["paid"], bank["other_paid"], bank["left"]) == (0, 15000, 0)


def test_the_payroll_headline_adds_up_its_slips_in_a_few_queries(site):
    _paid_and_planned(site)
    before = len(site.log)
    totals = site.payroll.payroll_salary_totals("PAY-1")
    assert {k: totals[k] for k in ("slips", "net_pay", "paid", "other_paid", "in_flight", "planned", "left",
                                   "slips_left")} == {
        "slips": 4, "net_pay": 115000, "paid": 50000, "other_paid": 15000, "in_flight": 25000, "planned": 10000,
        "left": 15000, "slips_left": 1}
    assert "MPS-1" in totals["schedules"]
    figures_sql = [e for e in site.log[before:] if e[0] == "sql"]
    assert len(figures_sql) <= 8  # grouped: the same few queries for 4 slips or 400
    # slip by slip, the grouped reading is owed.slip_left_to_pay's
    figures = site.payroll.slip_figures(site.payroll._slips("PAY-1"))
    for slip in ("SAL-1", "SAL-2", "SAL-3", "SAL-4"):
        assert figures[slip].left == site.owed.slip_left_to_pay(slip).left, slip


# ---------------------------------------------------------------- the form and list scripts
def _whitelisted(dotted):
    module, _, func = dotted.rpartition(".")
    source = ROOT.joinpath(*module.split(".")).with_suffix(".py").read_text(encoding="utf-8")
    return re.search(rf"@frappe\.whitelist\([^)]*\)\ndef {func}\(", source) is not None


@pytest.mark.parametrize("path, methods", [
    ("public/js/salary_slip.js", ("salary_slip_status", "from_salary_slip")),
    ("public/js/salary_slip_list.js", ("from_salary_slips",)),
    ("public/js/payroll_entry.js", ("payroll_salary_totals", "payroll_status")),
])
def test_the_scripts_call_whitelisted_endpoints_and_show_only_to_who_may(path, methods):
    code = (ROOT / "mpsa_suite" / path).read_text(encoding="utf-8")
    called = set(re.findall(r'"mpsa_suite\.money_out\.payroll\.(\w+)"', code))
    assert set(methods) <= called
    for method in called:
        assert _whitelisted(f"mpsa_suite.money_out.payroll.{method}"), method
    assert "can_see_salaries" in code
    if "from_salary" in " ".join(methods):
        assert "can_prepare" in code
