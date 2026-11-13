"""Round 4.5: the M-Pesa Bulk Payout sheet (money_out.bulk_payout), on the in-memory site of
test_payroll_schedule (SQL on SQLite) with the real B2C Payment Disbursement, row and request controllers, the
real party-less registry (money_out.partyless), Result recording (b2c_response_service) and booking
(b2c_postprocess_service).

Covers: the paste reader's optional columns (Expense Type, Project, Cost Center; unchanged without them) and
Paste Rows (employees allowed, a phone twice, own phone refused, M-Pesa's name); the save's checks (phone,
whole shillings 10 to 250,000, the expense type's account for the company, the cost center and project, the
defaults, the employee named, the same phone twice warned, self-pay by phone and by name); the approval (a named
M-Pesa Manager or Accounts Manager, the names tick enforced, self-pay); Pay by M-Pesa (one party-less draft
batch, rows Queued, nothing twice) and the send (self-pay of the sender, the summary, a short balance warned);
Results reaching the rows; one Journal Entry grouped by account, cost center, project and a dimension, posted
once nothing waits, the after-hours rule, a late success's own small journal, never twice, Book Now; each
statement line's part of the journal; Retry Failed (failed rows only, never an unknown one), the request's own
retry, Check with M-Pesa Now, Mark as Not Paid, Correct Phone and Retry, Settle Outside M-Pesa, the cancel; the
KES 10 test of a new number (matching: the rest; differing: held, released by a manager; the booking covering
the total); the sheet's status; the daily alert's section and its link; and the wiring (hooks, DocTypes,
settings, workspace).
"""

import ast
import datetime
import importlib
import json
import sys
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest
from test_payroll_schedule import DISB, PAYER, REQ, ROW, SETTINGS
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture this one extends)
from test_schedule import _datetime

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
SHEET, SROW = "M-Pesa Bulk Payout", "M-Pesa Bulk Payout Row"
GL, NOW, TODAY = b2b_fixture.GL, b2b_fixture.NOW, b2b_fixture.TODAY
JE, JEA = b2b_fixture.JE, b2b_fixture.JEA
BT, BTP = "Bank Transaction", "Bank Transaction Payments"
KNOWN = "M-Pesa Known Name"
SUITE = "M-Pesa Suite Settings"
PREPARER, APPROVER = "accounts@example.com", "manager@example.com"
CASUAL, OVERTIME = "Casual Salaries", "Overtime Expense"
WAGES, OT = "Casual Wages - AIL", "Overtime - AIL"
SITE_A, SITE_B = "Site A - AIL", "Site B - AIL"
_dict = fake_frappe._dict

ROWS = [
    dict(payee_name="Jane Wanjiku", phone="0711000331", amount=1500),  # the sheet's defaults: casual, Site A, A
    dict(payee_name="Otieno Ouma", phone="0722 000 111", amount=2000, expense_type=OVERTIME, cost_center=SITE_B,
         project="PROJ-B"),  # an employee (EMP-2's approved phone), overtime
    dict(payee_name="Mary Akinyi", phone="0711000333", amount=800, project="PROJ-B"),
    dict(payee_name="John Kamau", phone="+254 711 000 334", amount=1200, cost_center=SITE_B),
]


class SheetDoc(_dict):
    """An M-Pesa Bulk Payout as the controller sees it (its rows _dicts)."""


@pytest.fixture
def env(payroll_env, monkeypatch):  # noqa: F811
    env = payroll_env
    frappe = env.frappe
    columns = b2b_fixture.COLUMNS
    monkeypatch.setitem(columns, ROW, columns[ROW] + ("reference_row", "payee_account_doctype", "payee_account",
                                                      "claim_payee", "creation"))
    monkeypatch.setitem(columns, DISB, columns[DISB] + ("party_type", "transaction_to_pay_against", "owner",
                                                        "creation", "paid_from"))
    monkeypatch.setitem(columns, REQ, columns[REQ] + (
        "phone_number", "receiver_public_name", "accounting_booked", "accounting_error", "party_type", "party",
        "error_message", "result_desc", "result_code", "conversation_id", "bank_transaction"))
    monkeypatch.setitem(columns, JE, columns[JE] + ("clearance_date",))
    monkeypatch.setitem(columns, BTP, ("name", "parent", "payment_document", "payment_entry", "allocated_amount"))
    monkeypatch.setitem(columns, BT, ("name", "docstatus", "unallocated_amount"))
    monkeypatch.setitem(columns, "Mpesa B2C Status Query", ("name", "b2c_request", "status", "creation"))
    env.partyless = importlib.import_module("mpsa_suite.money_out.partyless")
    env.bulk = importlib.import_module("mpsa_suite.money_out.bulk_payout")
    env.partyless.register(SHEET, env.bulk.HANDLER)
    monkeypatch.setattr(env.bulk, "get_datetime", _datetime)
    maker_checker = importlib.import_module("mpsa_suite.money_out.maker_checker")
    monkeypatch.setattr(maker_checker, "check", lambda doc: None)  # maker-checker has its own tests
    salary_access = importlib.import_module("mpsa_suite.money_out.salary_access")
    monkeypatch.setattr(salary_access, "can_see_salaries", lambda user=None: True)
    env.send_confirm = importlib.import_module("mpsa_suite.money_out.send_confirm")
    for name in ("mpsa_suite.accounting.payment_entry_service", "mpsa_suite.accounting.journal_service"):
        stub = types.ModuleType(name)
        stub.create_payment_entry_for_doc = stub.create_journal_entry = lambda *a, **kw: None
        monkeypatch.setitem(sys.modules, name, stub)
    env.postprocess = importlib.import_module("mpsa_suite.services.b2c_postprocess_service")
    env.response = importlib.import_module("mpsa_suite.services.b2c_response_service")
    env.statement = importlib.import_module("mpsa_suite.money_out.statement")
    env.attention = importlib.import_module("mpsa_suite.money_out.attention")
    monkeypatch.setattr(env.bulk.pa, "alert", lambda message, doc, **kw: env.alerts.append((doc.name, message)))
    # the batches Pay by M-Pesa makes (the real controller), each row knowing its batch as on a bench
    plain_new_doc = frappe.new_doc
    env.batches = []

    def new_doc(doctype):
        doc = plain_new_doc(doctype)
        if doctype == DISB:
            env.batches.append(doc)
            plain_append = doc.append

            def append(table, row):
                made = plain_append(table, row)
                made.parent_doc = doc
                return made

            doc.append = append
        return doc

    frappe.new_doc = new_doc
    db = frappe.db
    db.table("Company")["Astral"].update(default_currency="KES", cost_center="Main - AIL")
    for name, company, group in ((SITE_A, "Astral", 0), (SITE_B, "Astral", 0), ("Main - AIL", "Astral", 0),
                                 ("All - AIL", "Astral", 1), ("Site X - XYZ", "Other", 0)):
        db.table("Cost Center")[name] = {"name": name, "company": company, "is_group": group, "disabled": 0,
                                         "cost_center_name": name.split(" - ")[0]}
    for name, status in (("PROJ-A", "Open"), ("PROJ-B", "Open"), ("PROJ-OLD", "Completed")):
        db.table("Project")[name] = {"name": name, "company": "Astral", "status": status, "is_active": "Yes",
                                     "project_name": f"Project {name[-1]}"}
    for expense_type, account in ((CASUAL, WAGES), (OVERTIME, OT), ("No Account", None)):
        db.table("Expense Claim Type")[expense_type] = {"name": expense_type, "expense_type": expense_type}
        if account:
            db.table("Expense Claim Account")[f"ECA-{expense_type}"] = {
                "name": f"ECA-{expense_type}", "parent": expense_type, "parenttype": "Expense Claim Type",
                "company": "Astral", "default_account": account}
            db.table("Account")[account] = {"name": account, "company": "Astral", "is_group": 0,
                                            "account_currency": "KES", "disabled": 0}
    for user, name in ((PAYER, "Payer Person"), (PREPARER, "Accounts Person"), (APPROVER, "Manager Person")):
        db.table("User")[user] = {"name": user, "full_name": name, "mobile_no": None, "phone": None, "enabled": 1,
                                  "user_type": "System User"}
    # EMP-2's phone was paid before (salary-free): M-Pesa knows it as Otieno Ouma
    known(env, "254722000111", "OTIENO OUMA")
    db.table(SUITE)[SUITE].update(bulk_payout_book_after_hours=2, verify_new_numbers=0, notify_role="Accounts Manager")
    yield env


# ---------------------------------------------------------------- helpers
def known(env, phone, name, times=3):
    env.frappe.db.table(KNOWN)[f"B2C:{phone}"] = {
        "name": f"B2C:{phone}", "destination_type": "Phone", "number": phone, "mpesa_name": name,
        "received": f"{phone} - {name}", "first_seen": NOW, "last_seen": NOW, "times_paid": times,
        "last_payout_doctype": REQ, "last_payout": "B2C-OLD", "source": "B2C Result", "salary_only": 0, "bank": 0}


class as_user:
    def __init__(self, env, user, *roles):
        self.env, self.user, self.roles = env, user, set(roles)

    def __enter__(self):
        frappe = self.env.frappe
        self.saved = frappe.session.user, set(frappe.user_roles)
        frappe.session.user, frappe.user_roles = self.user, self.roles
        return self

    def __exit__(self, *exc):
        self.env.frappe.session.user, self.env.frappe.user_roles = self.saved[0], self.saved[1]


def build(env, rows=ROWS, name="MBP-1", **kw):
    doc = SheetDoc(name=name, doctype=SHEET, docstatus=0, company="Astral", title="Casuals week 45",
                   posting_date=TODAY, expense_type=CASUAL, cost_center=SITE_A, project="PROJ-A",
                   verify_new_numbers=0, mpesa_settings=SETTINGS, owner=PREPARER, rows=[])
    doc.update(kw)
    for i, r in enumerate(rows, 1):
        doc.rows.append(_dict(name=f"{name}-R{i}", idx=i, parent=name, parenttype=SHEET, parentfield="rows", **r))
    return doc


def save(env, doc, user=PREPARER, *roles):
    with as_user(env, user, *(roles or ("Accounts User",))):
        env.bulk.validate_sheet(doc)
    return doc


def approve(env, doc, user=APPROVER, ticked=1, *roles):
    doc.docstatus, doc._action, doc.names_checked = 1, "submit", ticked
    with as_user(env, user, *(roles or ("M-Pesa Manager",))):
        env.bulk.validate_sheet(doc)
        env.bulk.before_submit(doc)
    return doc


def store(env, doc):
    db = env.frappe.db
    db.table(SHEET)[doc.name] = {k: v for k, v in doc.items() if k not in ("rows", "_action")}
    for r in doc.rows:
        db.table(SROW)[r.name] = dict(r)
    return doc


def sheet(env, rows=ROWS, name="MBP-1", **kw):
    """A sheet saved by the preparer and approved by the manager, as stored."""
    return store(env, approve(env, save(env, build(env, rows, name, **kw))))


def stored_rows(env, name="MBP-1"):
    return sorted((r for r in env.frappe.db.table(SROW).values() if r["parent"] == name), key=lambda r: r["idx"])


def header(env, name="MBP-1"):
    return env.frappe.db.table(SHEET)[name]


def pay(env, method="pay", name="MBP-1", user=PREPARER, **kw):
    with as_user(env, user, "Accounts User"):
        return getattr(env.bulk, method)(name, **kw)


def send(env, batch=None, user=PAYER):
    batch = batch or env.batches[-1]
    with as_user(env, user, "M-Pesa Payer"), env.send_confirm.deliberate("test", batch):
        batch.submit()
    return batch


def answer(env, batch, outcomes, when=NOW, record=True):
    """Each row's payout (B2C Disbursement Request) made and answered as Safaricom posts it: ``outcomes`` by
    phone, ("Paid", name), ("Failed", code, text), ("Initiated",) acknowledged and waiting, ("Unknown",) no
    answer to the send."""
    db = env.frappe.db
    requests = {}
    for ref in batch.references:
        outcome = outcomes.get(ref.partyb)
        if not outcome:
            continue
        request = f"B2C-{ref.name}"
        status = {"Paid": "Paid", "Failed": "Failed", "Initiated": "Initiated", "Unknown": "Status Unknown"}[outcome[0]]
        paid = status == "Paid"
        db.table(REQ)[request] = {
            "name": request, "docstatus": 1, "status": status, "amount": ref.allocated_amount,
            "transaction_id": f"R{ref.name[-6:].replace('-', '')}X" if paid else None,
            "receiver_public_name": f"{ref.partyb} - {outcome[1]}" if paid else None,
            "result_code": "0" if paid else (outcome[1] if status == "Failed" else None),
            "result_desc": "ok" if paid else (outcome[2] if status == "Failed" else None),
            "error_message": None, "conversation_id": None if status == "Status Unknown" else f"AG_{request}",
            "mpesa_settings": SETTINGS, "b2c_payment_disbursement": batch.name,
            "b2c_payment_disbursement_reference": ref.name, "reference_doctype": SHEET,
            "reference_name": ref.reference_name, "party_type": None, "party": None, "phone_number": ref.partyb,
            "creation": when, "transaction_completed_datetime": when if paid else None, "accounting_booked": 0,
            "accounting_error": None, "utility_acct_avlbl_funds": 400000 if paid else None}
        db.table(ROW)[ref.name]["b2c_disbursement_request"] = request
        requests[ref.partyb] = request
        if record and status in ("Paid", "Failed"):
            env.response.update_b2c_reference_status(request, enqueue_next=True)
            run_jobs(env)
    return requests


def run_jobs(env):
    """The booking jobs a result queued (bulk_payout.book_later), run as the worker runs them."""
    jobs = [kw for method, kw in env.frappe.enqueued if method.endswith("bulk_payout.book_waiting")]
    env.frappe.enqueued[:] = [(m, kw) for m, kw in env.frappe.enqueued if not m.endswith("bulk_payout.book_waiting")]
    for kw in jobs:
        env.bulk.book_waiting(kw["sheet"])


def journals(env):
    """{journal: (header, [lines])} of the Journal Entries the sheets posted."""
    db = env.frappe.db
    out = {}
    for name, je in db.table(JE).items():
        if je.get("cheque_no", "").startswith("MBP-"):
            out[name] = (je, sorted((r for r in db.table(JEA).values() if r["parent"] == name), key=lambda r: r["idx"]))
    return out


def debit_lines(lines):
    return sorted((r["account"], r["cost_center"], r.get("project"), r["debit_in_account_currency"])
                  for r in lines if r.get("debit_in_account_currency"))


def raises(match, exc=None):
    return pytest.raises(exc or fake_frappe.ValidationError, match=match)


# ---------------------------------------------------------------- Paste Rows
def test_the_paste_reader_takes_optional_columns_and_is_unchanged_without_them(env):
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    table = [["CASUALS WEEK 45"], ["Project Name", "Name", "Phone", "Amount", "Expense Type", "Cost Centre"],
             ["Site A", "Jane Wanjiku", "0711000331", "1,500", "Overtime Expense", "Site B"],
             ["Site B", "John Kamau", "0711000334", "1200", "", ""]]
    rows, left_out = cp.read_rows(table, extra=env.bulk.EXTRA_COLUMNS)
    assert left_out == [1, 2]
    assert [(r.payee_name, r.phone, r.amount_text, r.project, r.expense_type, r.cost_center) for r in rows] == [
        ("Jane Wanjiku", "0711000331", "1,500", "Site A", "Overtime Expense", "Site B"),
        ("John Kamau", "0711000334", "1200", "Site B", "", "")]
    # a claim's paste reads as before: no new fields, the project column passed over as it always was
    plain, plain_left_out = cp.read_rows([table[1][1:4], *[r[1:4] for r in table[2:]]])
    assert plain_left_out == [1] and all(set(r) == {"row", "payee_name", "phone", "amount_text", "problem"}
                                         for r in plain)
    # without column names the optional fields are empty: nothing is guessed
    bare, _ = cp.read_rows([["Jane", "0711000331", "1500"]], extra=env.bulk.EXTRA_COLUMNS)
    assert (bare[0].payee_name, bare[0].expense_type, bare[0].project, bare[0].cost_center) == ("Jane", "", "", "")


def test_paste_rows_pays_employees_and_a_phone_twice_and_refuses_only_the_preparer(env):
    env.frappe.db.table("User")[PREPARER]["mobile_no"] = "0711000999"
    text = ("Name\tPhone\tAmount\tExpense Type\tProject\n"
            "Otieno\t0722000111\t2000\tOvertime Expense\tProject B\n"  # an employee, known to M-Pesa
            "Otieno\t0722000111\t1500\t\t\n"  # the same person's casual wages: no refusal
            "Jane Wanjiku\t0711000331\t1500\tCasual salaries\t\n"
            "Jane Wanjiku\t0711000331\t700\tCasual Salaries\t\n"  # same phone, same type: a warning
            "Me\t0711000999\t500\t\t\n"  # the preparer's own phone
            "Ann\t0711000335\t900\tNo Such Type\tPROJ-A\n")
    with as_user(env, PREPARER, "Accounts User"):
        answer_ = env.bulk.parse_rows(text=text, company="Astral", expense_type=CASUAL, cost_center=SITE_A,
                                      project="PROJ-A", existing=json.dumps([["254711000333", CASUAL]]))
    rows = answer_["rows"]
    assert (answer_["good"], answer_["bad"], answer_["good_total"]) == (4, 2, 5700)
    first = rows[0]
    assert (first["payee_name"], first["mpesa_name"], first["typed_name"], first["expense_type"], first["project"],
            first["cost_center"]) == ("OTIENO OUMA", "OTIENO OUMA", None, OVERTIME, "PROJ-B", SITE_A)
    assert rows[1]["errors"] == [] and rows[1]["expense_type"] == CASUAL and rows[1]["warnings"] == []
    assert rows[2]["expense_type"] == CASUAL  # the type's name in any case
    assert rows[3]["errors"] == [] and "same phone for the same expense type" in rows[3]["warnings"][0]
    assert rows[4]["errors"] == ["your own phone or name: you may not prepare a payment to yourself"]
    assert rows[5]["errors"] == ["no Expense Claim Type 'No Such Type'"]
    env.denied.add((SHEET, "create"))
    with raises("no create on M-Pesa Bulk Payout", env.frappe.PermissionError):
        env.bulk.parse_rows(text=text)


# ---------------------------------------------------------------- the save
def test_rows_are_cleaned_their_defaults_filled_and_the_account_and_employee_found(env):
    doc = save(env, build(env))
    rows = doc.rows
    assert [r.phone for r in rows] == ["254711000331", "254722000111", "254711000333", "254711000334"]
    assert [(r.expense_type, r.expense_account, r.cost_center, r.project) for r in rows] == [
        (CASUAL, WAGES, SITE_A, "PROJ-A"), (OVERTIME, OT, SITE_B, "PROJ-B"), (CASUAL, WAGES, SITE_A, "PROJ-B"),
        (CASUAL, WAGES, SITE_B, "PROJ-A")]
    # the employee's phone: paid like anyone, the Employee named for reporting; M-Pesa's name locked
    assert (rows[1].employee, rows[1].payee_name, rows[1].mpesa_name, rows[1].typed_name, rows[1].known_flag) == (
        "EMP-2", "OTIENO OUMA", "OTIENO OUMA", None, "Known")
    assert [r.known_flag for r in rows] == ["New", "Known", "New", "New"]
    assert all(r.status == "Draft" for r in rows) and (doc.status, doc.total_rows, doc.total_amount) == (
        "Draft", 4, 5500)
    # no cost center anywhere: the company's
    doc = save(env, build(env, [dict(payee_name="Jane", phone="0711000331", amount=100)], cost_center=None))
    assert doc.rows[0].cost_center == "Main - AIL"


@pytest.mark.parametrize("change, message", [
    ({"phone": "0733"}, "Row #1: 0733 is not a Kenyan mobile number M-Pesa can pay"),
    ({"amount": 5}, r"Row #1: M-Pesa B2C pays KES 10 to 250,000 to a phone"),
    ({"amount": 300000}, r"Row #1: M-Pesa B2C pays KES 10 to 250,000 to a phone"),
    ({"amount": 10.5}, "Row #1: M-Pesa amounts must be whole shillings"),
    ({"payee_name": ""}, "Row #1: enter the Payee Name"),
    ({"expense_type": "No Account"}, "Row #1: No Account has no account for Astral"),
    ({"expense_type": "Nowhere"}, "Row #1: Expense Claim Type Nowhere not found"),
    ({"cost_center": "Site X - XYZ"}, "Row #1: Cost Center Site X - XYZ is not Astral's"),
    ({"cost_center": "All - AIL"}, "Row #1: All - AIL is a group Cost Center"),
    ({"project": "PROJ-OLD"}, "Row #1: Project PROJ-OLD is Completed: pick an open one"),
])
def test_a_wrong_row_is_refused(env, change, message):
    with raises(message):
        save(env, build(env, [dict(ROWS[0], **change)]))


def test_no_expense_type_anywhere_is_refused_and_the_same_phone_twice_for_one_type_is_warned(env):
    with raises("Row #1: pick its Expense Type, or the sheet's default Expense Type"):
        save(env, build(env, [ROWS[0]], expense_type=None))
    rows = [ROWS[0], dict(ROWS[0], amount=500), dict(ROWS[0], amount=300, expense_type=OVERTIME)]
    save(env, build(env, rows))
    assert any("Rows #1, #2 pay the same phone for Casual Salaries" in m for m in env.frappe.messages)
    assert not any("#3" in m and "same phone" in m for m in env.frappe.messages)


def test_a_typed_name_m_pesa_knows_otherwise_is_kept_as_name_given(env):
    doc = save(env, build(env, [dict(ROWS[1], payee_name="Peter Otieno")]))
    row = doc.rows[0]
    assert (row.payee_name, row.typed_name, row.known_flag) == ("OTIENO OUMA", "Peter Otieno", "Differs")
    assert any("M-Pesa paid 0722000111 as OTIENO OUMA before, not 'Peter Otieno'" in m for m in env.frappe.messages)
    again = save(env, doc)  # saved again: the name given stays
    assert (again.rows[0].payee_name, again.rows[0].typed_name) == ("OTIENO OUMA", "Peter Otieno")


def test_nobody_prepares_a_payment_to_themselves_by_phone_or_by_name(env):
    env.frappe.db.table("User")[PREPARER]["mobile_no"] = "0711 000 333"
    with raises("You may not prepare a payment to yourself: Row #3 pays your phone", env.frappe.PermissionError):
        save(env, build(env))
    env.frappe.db.table("User")[PREPARER].update(mobile_no=None, full_name="John Kamau")
    with raises("Row #4 pays 'John Kamau', your name", env.frappe.PermissionError):
        save(env, build(env))


def test_a_draft_keeps_no_approval_tick_and_no_results(env):
    doc = build(env, [dict(ROWS[0], status="Paid", receipt="RX", journal_entry="ACC-JV-9", paid_amount=1500)],
                names_checked=1, approved_by=APPROVER)
    save(env, doc)
    row = doc.rows[0]
    assert (doc.names_checked, doc.approved_by, row.status, row.receipt, row.journal_entry, row.paid_amount) == (
        0, None, "Draft", None, None, 0)


# ---------------------------------------------------------------- the approval
def test_the_approval_is_a_named_mpesa_manager_or_accounts_manager(env):
    doc = save(env, build(env))
    with raises("Not permitted: this needs one of the roles", env.frappe.PermissionError):
        approve(env, doc, PREPARER, 1, "Accounts User")
    env.frappe.get_cached_doc = lambda doctype, *a, **kw: _dict(refuse_administrator_payouts=1)
    with raises("Use your own login", env.frappe.PermissionError):
        approve(env, save(env, build(env)), "Administrator", 1, "M-Pesa Manager")
    approve(env, save(env, build(env)), "accounts.manager@example.com", 1, "Accounts Manager")
    done = approve(env, save(env, build(env)))
    assert (done.status, done.approved_by, done.approved_on, done.left_amount) == ("To Pay", APPROVER, NOW, 5500)


def test_the_approver_ticks_that_the_new_and_differing_names_were_checked(env):
    doc = save(env, build(env, [ROWS[0], dict(ROWS[1], payee_name="Peter Otieno")]))
    with raises(r"pays numbers whose M-Pesa names need checking \(1 under another name · 1 new\). Tick \"I "
                r"checked the 1 different name and 1 new number with the people paid.\""):
        approve(env, doc, ticked=0)
    approve(env, doc, ticked=1)
    assert doc.names_summary == "1 under another name · 1 new"
    # every number known under the name given: nothing to tick
    plain = save(env, build(env, [ROWS[1]], name="MBP-2"))
    approve(env, plain, ticked=0)
    assert plain.names_summary == "1 known and matching"


def test_nobody_approves_a_payment_to_themselves(env):
    doc = save(env, build(env))
    env.frappe.db.table("User")[APPROVER]["full_name"] = "Mary Akinyi"
    with raises("You may not approve a payment to yourself: Row #3 pays 'Mary Akinyi', your name",
                env.frappe.PermissionError):
        approve(env, doc)


# ---------------------------------------------------------------- Pay by M-Pesa and the send
def test_pay_makes_one_partyless_draft_batch_of_the_draft_rows(env):
    sheet(env)
    made = pay(env)
    assert (made.rows, made.total, made.tests) == (4, 5500, 0)
    batch = env.batches[-1]
    assert (made.b2c, batch.name, batch.docstatus, batch.party_type, batch.paid_to, batch.paid_from,
            batch.transaction_to_pay_against, batch.mode_of_payment) == (
        batch.name, batch.name, 0, None, None, GL, SHEET, f"Mpesa-{SETTINGS}")
    assert [(r.reference_row, r.partyb, r.allocated_amount, r.party, r.party_type) for r in batch.references] == [
        ("MBP-1-R1", "254711000331", 1500, None, None), ("MBP-1-R2", "254722000111", 2000, None, None),
        ("MBP-1-R3", "254711000333", 800, None, None), ("MBP-1-R4", "254711000334", 1200, None, None)]
    assert [r["status"] for r in stored_rows(env)] == ["Queued"] * 4
    assert header(env)["status"] == "Paying"
    assert any(f"prepared {batch.name} for 4 rows (KES 5,500). An M-Pesa Payer" in c[2] for c in env.frappe.comments)
    with raises("Nothing to pay: every row is paid, on its way"):
        pay(env)


def test_pay_needs_an_approved_sheet_and_never_pays_its_preparer(env):
    store(env, save(env, build(env)))
    with raises("MBP-1 is not approved"):
        pay(env)
    sheet(env, name="MBP-2")
    env.frappe.db.table("User")[PREPARER]["mobile_no"] = "0711000331"
    with raises("You may not prepare a payment to yourself: Row #1 pays your phone",
                env.frappe.PermissionError):
        pay(env, name="MBP-2")
    with as_user(env, "payer@example.com", "M-Pesa Payer"), raises("Not permitted", env.frappe.PermissionError):
        env.bulk.pay("MBP-2")


def test_nobody_sends_a_sheets_batch_to_themselves(env):
    sheet(env)
    pay(env)
    env.frappe.db.table("User")[PAYER]["mobile_no"] = "0711000334"
    with raises("You may not send a payment to yourself: Row #4 of MBP-1 pays your phone",
                env.frappe.PermissionError):
        send(env)
    env.frappe.db.table("User")[PAYER].update(mobile_no=None, full_name="Jane Wanjiku")
    with raises("Row #1 of MBP-1 pays 'Jane Wanjiku', your name", env.frappe.PermissionError):
        send(env)


def test_the_send_summary_shows_the_rows_and_warns_of_a_short_utility_balance(env, monkeypatch):
    sheet(env)
    pay(env)
    batch = env.batches[-1]
    balances = importlib.import_module("mpsa_suite.wallet.balances")
    monkeypatch.setattr(balances, "available", lambda settings, sub: _dict(amount=6000, after=4000))
    with as_user(env, PAYER, "M-Pesa Payer"):
        summary = env.send_confirm._summary(batch)
    assert summary["problems"] == []
    assert [(r["receiver"], r["party"], r["document"], r["amount"]) for r in summary["rows"]] == [
        ("Jane Wanjiku", f"PROJ-A, {CASUAL}", f"{SHEET} MBP-1, row #1", 1500),
        ("OTIENO OUMA", f"PROJ-B, {OVERTIME}", f"{SHEET} MBP-1, row #2", 2000),
        ("Mary Akinyi", f"PROJ-B, {CASUAL}", f"{SHEET} MBP-1, row #3", 800),
        ("John Kamau", f"PROJ-A, {CASUAL}", f"{SHEET} MBP-1, row #4", 1200)]
    assert summary["known_names"]["header"] == "1 known and matching · 3 new"
    assert summary["warnings"] == ["The Utility account had about KES 4,000 (Safaricom's last balance, less the "
                                   "payouts on their way), less than this batch's KES 5,500: top it up first, or the "
                                   "rows past the balance fail with insufficient funds (Retry Failed pays them after "
                                   "a top-up)."]
    monkeypatch.setattr(balances, "available", lambda settings, sub: _dict(amount=9000, after=9000))
    assert env.send_confirm._summary(batch)["warnings"] == []


def test_a_batch_row_is_the_sheet_rows_own(env):
    sheet(env)
    pay(env)
    batch = env.batches[-1]
    batch.references[0].partyb = "254711000999"
    with raises("Row #1: the phone is not row #1's of MBP-1"):
        batch.validate()
    batch.references[0].partyb, batch.references[1].allocated_amount = "254711000331", 1999
    with raises(r"Row #2: KES 1,999 is not what row #2 of MBP-1 pays \(KES 2,000\)"):
        batch.validate()


# ---------------------------------------------------------------- results and the journal
def paid_names(rows=ROWS):
    return {"254711000331": ("Paid", "JANE WANJIKU"), "254722000111": ("Paid", "OTIENO OUMA"),
            "254711000333": ("Paid", "MARY AKINYI"), "254711000334": ("Failed", "2040", "The recipient is not "
                                                                                       "registered.")}


def test_results_reach_the_rows_and_one_journal_books_the_paid_rows_grouped(env):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, paid_names())
    rows = stored_rows(env)
    assert [r["status"] for r in rows] == ["Paid", "Paid", "Paid", "Failed"]
    assert [r["receipt"] for r in rows[:3]] == [env.frappe.db.table(REQ)[requests[r["phone"]]]["transaction_id"]
                                                for r in rows[:3]]
    assert rows[0]["name_verdict"] == "Matches" and rows[0]["paid_amount"] == 1500
    assert rows[3]["error_message"].startswith("The recipient is not registered. M-Pesa does not know this number")
    ((name, (je, lines)),) = journals(env).items()  # one journal for the sheet
    assert (je["voucher_type"], je["cheque_no"], je["docstatus"], je["company"]) == ("Bank Entry", "MBP-1", 1, "Astral")
    assert je["user_remark"] == "M-Pesa Bulk Payout MBP-1 (Casuals week 45): 3 payouts paid by M-Pesa, KES 4,300"
    # grouped by account, cost center and project: rows 1 and 3 differ by project, row 2 is overtime on Site B
    assert debit_lines(lines) == [(WAGES, SITE_A, "PROJ-A", 1500), (WAGES, SITE_A, "PROJ-B", 800),
                                  (OT, SITE_B, "PROJ-B", 2000)]
    credit = [r for r in lines if r.get("credit_in_account_currency")]
    assert [(r["account"], r["credit_in_account_currency"], r["bank_account"]) for r in credit] == [
        (GL, 4300, b2b_fixture.BANK_ACCOUNT)]
    assert [r["journal_entry"] for r in rows] == [name, name, name, None]
    assert all(json.loads(r["bookings"]) == {requests[r["phone"]]: name} for r in rows[:3])
    stored = env.frappe.db.table(REQ)
    assert [(stored[requests[p]]["accounting_booked"], stored[requests[p]]["accounting_error"])
            for p in ("254711000331", "254722000111", "254711000333")] == [(1, None)] * 3
    # each payout's statement line, its part of the journal reconciled now
    assert sorted(set(env.fed)) == sorted(requests[p] for p in ("254711000331", "254722000111", "254711000333"))
    assert (header(env)["status"], header(env)["progress"], header(env)["paid_amount"], header(env)["failed_amount"],
            header(env)["left_amount"]) == ("Partly Paid", "Partly Paid: 3 paid, 1 failed", 4300, 1200, 1200)
    assert any(f"Journal Entry {name} books 3 payouts paid by M-Pesa (KES 4,300)" in c[2] for c in env.frappe.comments)


def test_each_payout_takes_its_own_part_of_the_journal_on_the_statement(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), paid_names())
    (name,) = journals(env)
    db = env.frappe.db
    for phone, amount in (("254711000331", 1500), ("254722000111", 2000), ("254711000333", 800)):
        receipt = db.table(REQ)[requests[phone]]["transaction_id"]
        assert env.statement.entries_for(receipt, GL) == [(JE, name, amount)]
    assert env.statement.entries_for("RNOTHING", GL) == []
    # once 3,800 of it is taken by two lines, the last line takes what is left at most
    db.table(BT)["BT-1"] = {"name": "BT-1", "docstatus": 1, "unallocated_amount": 0}
    db.table(BTP)["P1"] = {"name": "P1", "parent": "BT-1", "payment_document": JE, "payment_entry": name,
                           "allocated_amount": 3800}
    receipt = db.table(REQ)[requests["254711000333"]]["transaction_id"]
    assert env.statement.entries_for(receipt, GL) == [(JE, name, 500)]


def test_the_journal_waits_while_a_row_is_on_its_way(env):
    sheet(env)
    pay(env)
    batch = send(env)
    requests = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU"), "254722000111": ("Initiated",)})
    assert journals(env) == {}
    first = env.frappe.db.table(REQ)[requests["254711000331"]]
    assert first["accounting_booked"] == 0
    assert first["accounting_error"] == ("Waiting: MBP-1's journal books its paid rows once no row waits for M-Pesa "
                                         "(3 waiting; at most 2 hours after a send)")
    assert env.alerts == []  # waiting is no error
    rows = env.bulk.sync_rows("MBP-1")  # as the form's refresh does: the acks since are shown
    assert rows[1]["progress"] == "Payout sent 12:00, waiting for M-Pesa"
    assert rows[2]["progress"] == f"Payout of {batch.name} being sent"
    answer(env, batch, {"254722000111": ("Paid", "OTIENO OUMA"), "254711000333": ("Paid", "MARY AKINYI"),
                        "254711000334": ("Paid", "JOHN KAMAU")})
    ((_name, (_je, lines)),) = journals(env).items()
    assert sum(r["debit_in_account_currency"] for r in lines) == 5500  # the first row's payout too
    assert first["accounting_booked"] == 1 and first["accounting_error"] is None
    assert header(env)["status"] == "Paid"


def test_after_the_set_hours_the_paid_rows_are_booked_while_one_waits(env):
    sheet(env)
    pay(env)
    batch = send(env)
    long_ago = NOW - datetime.timedelta(hours=3)
    answer(env, batch, {"254722000111": ("Unknown",), "254711000333": ("Initiated",),
                        "254711000334": ("Initiated",)}, when=long_ago)
    requests = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")})
    ((name, (_je, lines)),) = journals(env).items()
    assert debit_lines(lines) == [(WAGES, SITE_A, "PROJ-A", 1500)]
    rows = stored_rows(env)
    assert rows[1]["progress"].startswith("Checking with M-Pesa: B2C-") and rows[1]["status"] == "In Flight"
    # the late rows, paid later: each a small journal of its own
    answer(env, batch, {"254722000111": ("Paid", "OTIENO OUMA"), "254711000333": ("Paid", "MARY AKINYI"),
                        "254711000334": ("Paid", "JOHN KAMAU")})
    late = {n: sum(r["debit_in_account_currency"] for r in lines) for n, (_je, lines) in journals(env).items()
            if n != name}
    assert sorted(late.values()) == [800, 1200, 2000]
    assert env.frappe.db.table(REQ)[requests["254711000331"]]["accounting_booked"] == 1


def test_with_no_book_after_hours_every_row_holds_the_journal(env):
    env.frappe.db.table(SUITE)[SUITE]["bulk_payout_book_after_hours"] = 0
    rows = [_dict(status="In Flight", sent_on=str(NOW - datetime.timedelta(days=3)), verification=None),
            _dict(status="Paid", sent_on=str(NOW), verification=None)]
    assert env.bulk.holding_rows(rows) == [rows[0]]
    assert env.bulk.holding_rows(rows, hours=2) == []
    verified = _dict(status="Draft", verification="Verified", sent_on=str(NOW))
    assert env.bulk.holding_rows([verified], hours=2) == [verified]  # its rest is the next payout


def test_a_failed_row_retried_and_paid_gets_a_small_journal_of_its_own(env):
    sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    (first,) = journals(env)
    made = pay(env, "retry_failed")
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [("MBP-1-R4", 1200)]
    assert made.rows == 1
    answer(env, send(env), {"254711000334": ("Paid", "JOHN KAMAU")})
    second = [n for n in journals(env) if n != first]
    assert len(second) == 1
    assert debit_lines(journals(env)[second[0]][1]) == [(WAGES, SITE_B, "PROJ-A", 1200)]
    row = stored_rows(env)[3]
    assert (row["status"], row["attempts"], row["journal_entry"]) == ("Paid", 2, second[0])
    assert header(env)["status"] == "Paid"


def test_the_journal_groups_by_accounting_dimension_too(env, monkeypatch):
    dims = sys.modules["erpnext.accounts.doctype.accounting_dimension.accounting_dimension"]
    monkeypatch.setattr(dims, "get_accounting_dimensions", lambda as_list=True: ["project", "department"])
    rows = [dict(ROWS[0], department="Ops"), dict(ROWS[0], phone="0711000335", department="Admin"),
            dict(ROWS[0], phone="0711000336", department="Ops")]
    sheet(env, rows)
    pay(env)
    answer(env, send(env), {p: ("Paid", "X Y") for p in ("254711000331", "254711000335", "254711000336")})
    ((_name, (_je, lines)),) = journals(env).items()
    assert sorted((r["account"], r.get("department"), r["debit_in_account_currency"]) for r in lines
                  if r.get("debit_in_account_currency")) == [(WAGES, "Admin", 1500), (WAGES, "Ops", 3000)]


def test_a_journal_is_never_posted_twice_and_book_now_books_what_waits(env):
    sheet(env)
    pay(env)
    batch = send(env)
    answer(env, batch, paid_names())
    assert env.bulk.book_sheet("MBP-1") is None  # every paid payout booked already
    assert len(journals(env)) == 1
    # a journal cancelled by staff: Book Now books its payouts again
    (name,) = journals(env)
    env.frappe.db.table(JE)[name]["docstatus"] = 2
    with as_user(env, PREPARER, "Accounts User"), raises("Not permitted", env.frappe.PermissionError):
        env.bulk.book_now("MBP-1")
    with as_user(env, APPROVER, "Accounts Manager"):
        with raises("The Posting Date is the sheet's or a later day"):
            env.bulk.book_now("MBP-1", posting_date=str(TODAY - datetime.timedelta(days=1)))
        again = env.bulk.book_now("MBP-1", posting_date=str(TODAY + datetime.timedelta(days=2)))["journal_entry"]
    assert again and again != name
    assert journals(env)[again][0]["posting_date"] == TODAY + datetime.timedelta(days=2)
    assert sum(r["debit_in_account_currency"] for r in journals(env)[again][1]) == 4300
    with as_user(env, APPROVER, "M-Pesa Manager"):
        assert env.bulk.book_now("MBP-1") == {"journal_entry": None}


def test_a_journal_that_fails_is_recorded_alerted_and_retried_by_the_sweep(env):
    sheet(env)
    pay(env)
    batch = send(env)
    env.fail_insert = env.frappe.ValidationError("Books have been closed till the period ending on 2026-10-31")
    requests = answer(env, batch, paid_names())
    assert journals(env) == {}
    # the last row failed: a job booked the paid ones through the first, which records why it could not
    stored = env.frappe.db.table(REQ)[requests["254711000331"]]
    # the error's last line (this fake site's traceback; on a closed period with the sheet's hint: Book Now)
    assert (stored["accounting_booked"], stored["accounting_error"]) == (0, "traceback")
    assert any("was paid but could not be booked" in a[1] for a in env.alerts)
    with as_user(env, APPROVER, "M-Pesa Manager"):
        state = env.bulk.sheet_state("MBP-1")
    assert state["unbooked"]["rows"] == [1, 2, 3] and state["unbooked"]["amount"] == 4300
    assert state["unbooked"]["problems"] == ["traceback"]  # the waiting notes are no problem
    env.fail_insert = None
    env.postprocess.retry_unbooked_payouts()
    assert len(journals(env)) == 1
    assert all(env.frappe.db.table(REQ)[r]["accounting_booked"] == 1 for p, r in requests.items()
               if p != "254711000334")


def test_a_closed_period_tells_staff_to_book_now(env, monkeypatch):
    posting = importlib.import_module("mpsa_suite.accounting.posting")
    monkeypatch.setattr(posting, "period_closed", lambda e: True)
    sheet(env, ROWS[:1])
    pay(env)
    batch = send(env)
    env.fail_insert = env.frappe.ValidationError("Books have been closed till the period ending on 2026-10-31")
    requests = answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU")})
    error = env.frappe.db.table(REQ)[requests["254711000331"]]["accounting_error"]
    assert error.startswith("traceback. Its date is in a closed Accounting Period")
    assert "press Book Now on MBP-1" in error


def test_a_value_read_back_from_the_database_is_no_change(env):
    from decimal import Decimal

    bulk = env.bulk
    assert bulk._norm("paid_amount", Decimal("1500.000000000")) == bulk._norm("paid_amount", 1500.0)
    assert bulk._norm("sent_on", NOW) == bulk._norm("sent_on", "2026-10-18 12:00:00.000000")
    assert bulk._norm("verification", None) == bulk._norm("verification", "")
    sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    for row in env.frappe.db.table(SROW).values():
        row["paid_amount"] = Decimal(str(row["paid_amount"] or 0))
    with as_user(env, APPROVER, "M-Pesa Manager"):
        assert env.bulk.sheet_state("MBP-1")["changed"] is False  # the form never reloads for nothing


def test_booked_says_which_journal_books_each_payout(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), paid_names())
    (name,) = journals(env)
    db = env.frappe.db
    ref = _dict(db.table(ROW)[db.table(REQ)[requests["254711000331"]]["b2c_payment_disbursement_reference"]])
    assert env.bulk.HANDLER.booked(ref) == 1
    assert env.bulk.HANDLER.booked(_dict(ref, b2c_disbursement_request="B2C-OTHER")) is None
    hint = env.postprocess.closed_period_hint(ref, "RX")
    assert "press Book Now on MBP-1" in hint
    assert env.bulk.HANDLER.describe(ref)["document"] == f"{SHEET} MBP-1, row #1"


# ---------------------------------------------------------------- failures, retries and payouts in doubt
def test_retry_failed_pays_failed_rows_only_never_one_in_doubt(env):
    sheet(env)
    pay(env)
    batch = send(env)
    answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU"), "254722000111": ("Unknown",),
                        "254711000333": ("Failed", "1", "The balance is insufficient for the transaction."),
                        "254711000334": ("Failed", "1", "The balance is insufficient for the transaction.")})
    rows = stored_rows(env)
    assert [r["status"] for r in rows] == ["Paid", "In Flight", "Failed", "Failed"]
    with as_user(env, APPROVER, "M-Pesa Manager"):
        state = env.bulk.sheet_state("MBP-1")
    assert state["failures"] == {"insufficient": {"rows": [3, 4], "amount": 2000, "hint": (
        "The shortcode's Utility account had too little money: top it up, then press Retry Failed.")}}
    made = pay(env, "retry_failed")
    assert [r.reference_row for r in env.batches[-1].references] == ["MBP-1-R3", "MBP-1-R4"]
    assert made.rows == 2
    with raises("No row has failed"):
        pay(env, "retry_failed")


def test_a_payout_in_doubt_is_never_sent_again(env):
    sheet(env)
    pay(env)
    batch = send(env)
    answer(env, batch, {"254722000111": ("Unknown",)})
    # a second batch of the row (made by hand) is refused while the first payout may have paid it
    other = env.frappe.new_doc(DISB)
    other.update({k: getattr(batch, k) for k in ("naming_series", "company", "posting_date", "payment_type",
                                                 "transaction_to_pay_against", "mode_of_payment", "paid_from",
                                                 "paid_from_account_currency", "paid_to_account_currency",
                                                 "source_exchange_rate", "target_exchange_rate")})
    other.update({"party_type": None, "paid_to": None})
    other.append("references", {"reference_doctype": SHEET, "reference_name": "MBP-1", "reference_row": "MBP-1-R2",
                                "partyb": "254722000111", "allocated_amount": 2000, "currency": "KES",
                                "exchange_rate": 1})
    with raises("which is being paid by"):
        other.insert()


def test_a_retry_of_the_request_itself_is_refused_the_sheets_retry_failed_makes_a_new_payout(env, monkeypatch):
    # round 4.5 review: a request sent again gets a new OriginatorConversationID, and its first attempt's late
    # Result could no longer be matched; the sheet's Retry Failed makes a new request instead
    sheet(env)
    pay(env)
    requests = answer(env, send(env), paid_names())
    failed = requests["254711000334"]
    request = env.frappe.get_doc(REQ, failed)
    with as_user(env, PAYER, "M-Pesa Payer"):
        with raises(f"{failed} pays a row of M-Pesa Bulk Payout MBP-1: retry it with Retry Failed on MBP-1, which "
                    "makes a new payout"):
            env.partyless.check_retry(request)
    made = pay(env, "retry_failed")
    assert [r.reference_row for r in env.batches[-1].references] == ["MBP-1-R4"] and made.rows == 1
    env.frappe.db.table(SROW)["MBP-1-R4"]["phone"] = "254711000999"  # corrected since: never to the old number
    with raises("the phone is not row #4's of MBP-1"):
        env.batches[-1].validate()


def test_check_with_mpesa_now_asks_about_the_waiting_payouts(env, monkeypatch):
    sheet(env)
    pay(env)
    answer(env, send(env), {"254711000331": ("Paid", "JANE WANJIKU"), "254722000111": ("Unknown",),
                            "254711000333": ("Initiated",)})
    status = importlib.import_module("mpsa_suite.money_out.b2c_status")
    asked = []

    def start_query(request):
        if request.endswith("R3"):
            raise env.frappe.ValidationError("Status query MBSQ-1 is still waiting for Safaricom's answer")
        asked.append(request)
        return "MBSQ-2"

    monkeypatch.setattr(status, "start_query", start_query)
    with as_user(env, PAYER, "M-Pesa Payer"):
        found = env.bulk.check_now("MBP-1")
    assert found["asked"] == ["B2C-MPESA-B2C-1-R2"] and asked == found["asked"]
    assert found["skipped"] == ["B2C-MPESA-B2C-1-R3: Status query MBSQ-1 is still waiting for Safaricom's answer"]
    with as_user(env, PREPARER, "Accounts User"), raises("Not permitted", env.frappe.PermissionError):
        env.bulk.check_now("MBP-1")


def test_mark_as_not_paid_after_the_status_query_found_nothing(env):
    sheet(env)
    pay(env)
    requests = answer(env, send(env), {"254722000111": ("Unknown",)})
    request = requests["254722000111"]
    with as_user(env, APPROVER, "M-Pesa Manager"):
        with raises("M-Pesa has not answered a Transaction Status query about"):
            env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "statement shows nothing")
        env.frappe.db.table("Mpesa B2C Status Query")["MBSQ-1"] = {"name": "MBSQ-1", "b2c_request": request,
                                                                   "status": "Result Received", "creation": NOW}
        with raises("Say what the M-Pesa statement shows"):
            env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", " ")
        done = env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "the statement of 18 Oct shows no such payment")
    assert done == {"row": "MBP-1-R2", "status": "Failed", "request": request}
    stored = env.frappe.db.table(REQ)[request]
    assert stored["status"] == "Failed" and stored["result_code"] is None  # a late Result still settles it
    assert stored["error_message"].startswith(f"Marked as not paid by {APPROVER}")
    assert stored_rows(env)[1]["status"] == "Failed"
    with as_user(env, PAYER, "M-Pesa Payer"), raises("Not permitted", env.frappe.PermissionError):
        env.bulk.mark_not_paid("MBP-1", "MBP-1-R2", "x")


def test_correct_phone_and_retry(env):
    sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    with as_user(env, APPROVER, "M-Pesa Manager"):
        with raises("only a Failed row's phone is corrected"):
            env.bulk.correct_phone("MBP-1", "MBP-1-R1", "0711000399", "the right number")
        made = env.bulk.correct_phone("MBP-1", "MBP-1-R4", "0711 000 344", "John called with his own number")
    row = stored_rows(env)[3]
    assert (row["phone"], row["payee_name"], row["known_flag"]) == ("254711000344", "John Kamau", "New")
    assert made["row"] == "MBP-1-R4" and made["rows"] == 1
    assert [(r.reference_row, r.partyb) for r in env.batches[-1].references] == [("MBP-1-R4", "254711000344")]
    assert any("phone corrected from 254711000334 to 254711000344" in c[2] for c in env.frappe.comments)


def test_settle_outside_mpesa_leaves_the_row_out_of_the_journal(env):
    sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    with as_user(env, APPROVER, "M-Pesa Manager"):
        with raises("only a Draft, Failed or On Hold row is settled"):
            env.bulk.settle_row("MBP-1", "MBP-1-R1", "cash")
        env.bulk.settle_row("MBP-1", "MBP-1-R4", "paid in cash at the site, voucher PV-12")
    row = stored_rows(env)[3]
    assert row["status"] == "Cancelled" and row["error_message"].startswith(f"Settled outside M-Pesa by {APPROVER}")
    assert header(env)["status"] == "Paid"
    with raises("Nothing to pay"):
        pay(env, "pay")
    with raises("No row has failed"):
        pay(env, "retry_failed")
    with as_user(env, PREPARER, "Accounts User"), raises("Not permitted", env.frappe.PermissionError):
        env.bulk.settle_row("MBP-1", "MBP-1-R4", "x")


def test_the_cancel_is_refused_once_a_row_is_queued_or_paid(env):
    doc = sheet(env)
    env.bulk.before_cancel(doc)  # nothing sent: cancelled, its failed payouts would not hold it
    assert "B2C Payment Disbursement" in doc.ignore_linked_doctypes
    pay(env)
    with raises("MBP-1 cannot be cancelled: rows #1 \\(Queued\\)"):
        env.bulk.before_cancel(doc)
    answer(env, send(env), paid_names())
    with raises("cannot be cancelled: rows #1 \\(Paid\\), #2 \\(Paid\\), #3 \\(Paid\\)"):
        env.bulk.before_cancel(doc)


def test_a_save_after_submit_keeps_the_results(env):
    doc = sheet(env)
    pay(env)
    answer(env, send(env), paid_names())
    doc.rows[0].status, doc.rows[0].receipt, doc.status = "Draft", None, "Draft"
    env.bulk.keep_results(doc)
    assert (doc.rows[0].status, doc.status) == ("Paid", "Partly Paid") and doc.rows[0].receipt


# ---------------------------------------------------------------- KES 10 first for new numbers
def test_new_numbers_get_kes_10_first_when_the_sheet_says_so(env):
    rows = [*ROWS, dict(payee_name="Small Pay", phone="0711000338", amount=15)]
    sheet(env, rows, verify_new_numbers=1)
    made = pay(env)
    assert made.tests == 3
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [
        ("MBP-1-R1/test", 10), ("MBP-1-R2", 2000), ("MBP-1-R3/test", 10), ("MBP-1-R4/test", 10),
        ("MBP-1-R5", 15)]  # the known number paid at once; a row under KES 20 is its own test
    assert any("3 new numbers get KES 10 first" in c[2] for c in env.frappe.comments)


def test_a_matching_test_lets_the_rest_be_paid_and_the_journal_books_the_total(env):
    sheet(env, ROWS[:2], verify_new_numbers=1)
    pay(env)
    tests = answer(env, send(env), {"254711000331": ("Paid", "JANE WANJIKU")})
    row = stored_rows(env)[0]
    assert (row["status"], row["verification"], row["paid_amount"], row["test_request"]) == (
        "Draft", "Verified", 10, tests["254711000331"])
    assert row["progress"].startswith("KES 10 test paid under the name given")
    assert journals(env) == {}  # the rest is still to be paid (and row 2 waits)
    answer(env, env.batches[-1], {"254722000111": ("Paid", "OTIENO OUMA")})
    assert journals(env) == {}  # the verified row's rest holds it
    made = pay(env)
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [("MBP-1-R1", 1490)]
    assert made.tests == 0
    rest = answer(env, send(env), {"254711000331": ("Paid", "JANE WANJIKU")})
    ((name, (_je, lines)),) = journals(env).items()
    assert debit_lines(lines) == [(WAGES, SITE_A, "PROJ-A", 1500), (OT, SITE_B, "PROJ-B", 2000)]
    row = stored_rows(env)[0]
    assert (row["status"], row["paid_amount"], row["verification"]) == ("Paid", 1500, "Verified")
    assert json.loads(row["bookings"]) == {tests["254711000331"]: name, rest["254711000331"]: name}
    db = env.frappe.db
    for request, amount in ((tests["254711000331"], 10), (rest["254711000331"], 1490)):
        assert env.statement.entries_for(db.table(REQ)[request]["transaction_id"], GL) == [(JE, name, amount)]


def test_a_test_paid_under_another_name_holds_the_row_for_a_manager(env):
    sheet(env, ROWS[:1], verify_new_numbers=1)
    pay(env)
    answer(env, send(env), {"254711000331": ("Paid", "PETER MWANGI")})
    row = stored_rows(env)[0]
    assert (row["status"], row["verification"], row["name_verdict"]) == ("On Hold", "Name Differs", "Does Not Match")
    assert any("The rest (KES 1,490) is held" in a[1] for a in env.alerts)
    with raises("Nothing to pay"):
        pay(env)
    # a batch made by hand for the rest is refused while it is held
    batch = env.batches[-1]
    batch.references[0].reference_row, batch.references[0].allocated_amount = "MBP-1-R1", 1490
    batch.docstatus = 0
    with raises("is held: its KES 10 test was paid under another name"):
        batch.validate()
    with as_user(env, APPROVER, "M-Pesa Manager"):
        env.bulk.release_row("MBP-1", "MBP-1-R1", "Jane uses her husband's line; she confirmed on the phone")
    row = stored_rows(env)[0]
    assert (row["status"], row["verification"]) == ("Draft", "Released")
    made = pay(env)
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [("MBP-1-R1", 1490)]
    assert made.rows == 1


def test_a_failed_test_is_retried_as_a_test(env):
    sheet(env, ROWS[:1], verify_new_numbers=1)
    pay(env)
    answer(env, send(env), {"254711000331": ("Failed", "2040", "The recipient is not registered.")})
    assert stored_rows(env)[0]["status"] == "Failed" and stored_rows(env)[0]["verification"] == "Testing"
    pay(env, "retry_failed")
    assert [(r.reference_row, r.allocated_amount) for r in env.batches[-1].references] == [("MBP-1-R1/test", 10)]


def test_the_suite_setting_starts_new_sheets_and_an_upgraded_site_gets_it(env, monkeypatch):
    env.frappe.db.table(SUITE)[SUITE]["verify_new_numbers"] = 1
    with as_user(env, PREPARER, "Accounts User"):
        assert env.bulk.sheet_state(None)["defaults"] == {"verify_new_numbers": True}
    data = json.loads((APP / "mpsa_suite" / "doctype" / "m_pesa_suite_settings" / "m_pesa_suite_settings.json")
                      .read_text(encoding="utf-8"))
    order = data["field_order"]
    fields = {f["fieldname"]: f for f in data["fields"]}
    assert order.index("bulk_payouts_section") < order.index("verify_new_numbers") < order.index("accounts_tab")
    assert (fields["verify_new_numbers"]["fieldtype"], fields["verify_new_numbers"]["default"]) == ("Check", "0")
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.sql = lambda query, values=None, pluck=None, **kw: ["notify_role"]
        frappe.get_meta = lambda doctype: types.SimpleNamespace(fields=[_dict(f) for f in data["fields"]])
        written = {}
        frappe.db.set_single_value = lambda doctype, field, value: written.__setitem__(field, value)
        importlib.import_module("mpsa_suite.install").backfill_single_defaults(SUITE)
        assert written["verify_new_numbers"] == "0"
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- the sheet's status and the daily alert
def test_sheet_values_from_the_rows(env):
    bulk = env.bulk

    def rows(*statuses):
        return [_dict(status=s, amount=1000, paid_amount=1000 if s == "Paid" else 0) for s in statuses]

    assert bulk.sheet_values(0, rows("Draft"))["status"] == "Draft"
    assert bulk.sheet_values(1, rows("Draft", "Draft"))["progress"] == "To Pay: 2 to pay"
    assert bulk.sheet_values(1, rows("Paid", "In Flight", "Queued"))["progress"] == "Paying: 1 paid, 2 waiting"
    values = bulk.sheet_values(1, rows(*["Paid"] * 64, *["Failed"] * 3, "On Hold"))
    assert (values["status"], values["progress"], values["paid_amount"], values["failed_amount"],
            values["left_amount"]) == ("Partly Paid", "Partly Paid: 64 paid, 3 failed, 1 held", 64000, 3000, 4000)
    assert bulk.sheet_values(1, rows("Paid", "Cancelled"))["status"] == "Paid"
    assert bulk.sheet_values(2, rows("Cancelled"))["status"] == "Cancelled"


def test_the_daily_alert_lists_sheets_part_paid_failed_or_waiting_and_links_their_list(env):
    sheet(env)
    pay(env)
    batch = send(env)
    answer(env, batch, {"254711000331": ("Paid", "JANE WANJIKU"),
                        "254722000111": ("Failed", "1", "The balance is insufficient for the transaction.")})
    answer(env, batch, {"254711000333": ("Initiated",)}, when=NOW - datetime.timedelta(hours=30))
    sheet(env, ROWS[:1], name="MBP-2")  # approved, nothing sent: nothing to say
    title, lines, link = env.bulk.attention_section()
    assert title == "Bulk payout sheets" and link == "https://erp.example.com/app/m-pesa-bulk-payout?docstatus=1"
    assert lines == ["MBP-1 (Casuals week 45): 1 paid, 1 failed (KES 2,000), 1 waiting for M-Pesa over 24 hours; "
                     "KES 4,000 left"]
    env.attention.register(env.bulk.attention_section)
    sent = []
    monkeypatch_alert = importlib.import_module("mpsa_suite.money_out.party_accounts")
    original = monkeypatch_alert.alert
    monkeypatch_alert.alert = lambda message, doc, **kw: sent.append((message, doc, kw))
    try:
        env.attention.daily_attention()
    finally:
        monkeypatch_alert.alert = original
    ((message, doc, kw),) = sent
    assert message == "M-Pesa needs attention: Bulk payout sheets (1)"
    assert kw["link"] == link and (doc.doctype, doc.name) == (SUITE, SUITE)
    assert kw["details"].startswith(f'<p><b><a href="{link}">Bulk payout sheets</a></b></p><ul><li>MBP-1')


def test_an_alert_with_a_link_opens_it(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        made = []
        log = types.ModuleType("frappe.desk.doctype.notification_log.notification_log")
        log.enqueue_create_notification = lambda users, content: made.append((users, content))
        log.make_notification_logs = lambda content, users: made.append((users, dict(content)))
        for name in ("frappe.desk", "frappe.desk.doctype", "frappe.desk.doctype.notification_log"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, log.__name__, log)
        frappe.db.table(SUITE)[SUITE] = {"notify_role": "Accounts Manager"}
        frappe.db.table("Has Role")["HR-1"] = {"name": "HR-1", "parent": "acc@example.com", "role": "Accounts Manager",
                                               "parenttype": "User"}
        pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        pa.alert("M-Pesa needs attention: X (1)", _dict(doctype=SUITE, name=SUITE), link="/app/m-pesa-bulk-payout")
        assert made[0][1]["link"] == "/app/m-pesa-bulk-payout"
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- the form
def test_the_forms_state(env):
    with as_user(env, PREPARER, "Accounts User"):
        store(env, save(env, build(env)))
        draft = env.bulk.sheet_state("MBP-1")
    assert draft["names"]["header"] == "1 known and matching · 3 new"
    assert draft["names"]["acknowledge"] == "I checked the 3 new numbers with the people paid."
    sheet(env, name="MBP-2")
    pay(env, name="MBP-2")
    answer(env, send(env), paid_names())
    with as_user(env, APPROVER, "Accounts Manager"):
        state = env.bulk.sheet_state("MBP-2")
    assert state["by_project"] == {"PROJ-A": {"paid": 1500, "failed": 1200, "left": 1200, "rows": 2},
                                   "PROJ-B": {"paid": 2800, "failed": 0.0, "left": 0.0, "rows": 2}}
    assert [j["docstatus"] for j in state["journals"]] == [1]
    assert state["unbooked"] == {"rows": [], "amount": 0.0, "problems": []} and state["can_book"]
    assert state["failures"]["receiver"]["rows"] == [4]
    env.denied.add((SHEET, "read"))
    with raises("no read on M-Pesa Bulk Payout", env.frappe.PermissionError):
        env.bulk.sheet_state("MBP-2")


def test_a_row_saved_on_its_own_is_refused(env):
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.m_pesa_bulk_payout_row.m_pesa_bulk_payout_row")
    with raises("Change a bulk payout's rows on their sheet"):
        module.MPesaBulkPayoutRow.validate(None)
    with raises("Change a bulk payout's rows on their sheet"):
        module.MPesaBulkPayoutRow.on_trash(None)


# ---------------------------------------------------------------- the wiring
def _hooks() -> dict:
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id in ("mpsa_partyless_payouts", "mpsa_attention_sections",
                                    "accounting_dimension_doctypes")}


def test_hooks_register_the_handler_the_daily_section_and_the_rows_dimensions():
    hooks = _hooks()
    assert hooks["mpsa_partyless_payouts"][SHEET] == "mpsa_suite.money_out.bulk_payout.HANDLER"
    assert "mpsa_suite.money_out.bulk_payout.attention_section" in hooks["mpsa_attention_sections"]
    assert SROW in hooks["accounting_dimension_doctypes"]
    source = (APP / "mpsa_suite" / "migrate.py").read_text(encoding="utf-8")
    assert 'create_accounting_dimensions_for_doctype("M-Pesa Bulk Payout Row")' in source


def _doctype(folder):
    return json.loads((APP / "mpsa_suite" / "doctype" / folder / f"{folder}.json").read_text(encoding="utf-8"))


def test_the_doctypes(env):
    parent, row = _doctype("m_pesa_bulk_payout"), _doctype("m_pesa_bulk_payout_row")
    assert (parent["is_submittable"], parent["track_changes"], parent["autoname"], parent["module"]) == (
        1, 1, "MBP-.YY.-.MM.-.####", "Mpsa Suite")
    for data in (parent, row):
        assert sorted(f["fieldname"] for f in data["fields"]) == sorted(data["field_order"])
    fields = {f["fieldname"]: f for f in row["fields"]}
    assert (row["istable"], row["permissions"]) == (1, [])
    # ERPNext puts the dimensions after these (accounting_dimension.make_dimension_in_accounting_doctypes)
    assert fields["accounting_dimensions_section"]["fieldtype"] == "Section Break"
    assert fields["dimension_col_break"]["fieldtype"] == "Column Break"
    for name in env.bulk.RESULT_FIELDS:
        assert (fields[name].get("read_only"), fields[name].get("allow_on_submit"), fields[name].get("no_copy")) == (
            1, 1, 1), name
    assert fields["status"]["options"].split("\n") == ["Draft", "Queued", "In Flight", "Paid", "Failed", "On Hold",
                                                       "Cancelled"]
    head = {f["fieldname"]: f for f in parent["fields"]}
    assert head["status"]["options"].split("\n") == ["Draft", "To Pay", "Paying", "Partly Paid", "Paid", "Cancelled"]
    assert head["rows"]["options"] == SROW and head["verify_new_numbers"]["fieldtype"] == "Check"
    assert (APP / "mpsa_suite" / "doctype" / "m_pesa_bulk_payout" / "m_pesa_bulk_payout_list.js").is_file()


def test_the_workspace_and_its_sidebar_link_the_sheets():
    workspace = json.loads((APP / "mpsa_suite" / "workspace" / "m_pesa" / "m_pesa.json").read_text(encoding="utf-8"))
    links = workspace["links"]
    assert any(link.get("link_to") == SHEET for link in links)
    for i, link in enumerate(links):
        if link["type"] == "Card Break":
            following = next((j for j in range(i + 1, len(links)) if links[j]["type"] == "Card Break"), len(links))
            assert link["link_count"] == following - i - 1, link["label"]
    sidebar = json.loads((APP / "workspace_sidebar" / "m_pesa.json").read_text(encoding="utf-8"))
    assert any(item.get("link_to") == SHEET for item in sidebar["items"])
