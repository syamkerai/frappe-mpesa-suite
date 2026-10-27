"""Round 4 final review, area B (salaries, schedules, payee accounts): the fixes, on the in-memory
sites of the tests they extend (test_payee_accounts, test_payroll_schedule, test_employee_documents,
test_salary_entry, test_r4_connections, test_r4_buttons_js).

Covers: an approved Supplier / Employee M-Pesa Account is cancelled whatever payouts and claims name
it (Frappe's back-link check, transcribed, and the Cancel All dialog's list); a payment that names no
account goes to the party's default, never silently to another one while the default is on hold or
blocked; accounts approved without a second named person are blocked by migrate, and a bank paybill
coming back never unblocks them; who may not see salaries cannot plan, keep or remove a salary line on
a schedule, and learns no amount or destination from the refusal; no bank or cash entry for a payroll
(HRMS's Make Bank Entry) while M-Pesa pays salaries of it, per employee when the payroll is booked per
employee; one cost center on both rows of the schedule's salary entry; a claim's or advance's payouts
counted on its Connections for who reads it; a status query about a claim's B2B payout is not salary
data; Versions of salary records and of Employee M-Pesa Accounts hidden like the records; the payee
account list indicators.
"""

import ast
import datetime
import importlib
import json
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest
from test_employee_documents import (
    EA,
    EC,
    HRMS_READERS,
    PREPARER,
    claim_payouts,
    readers,
)
from test_employee_documents import env as claims  # noqa: F401
from test_payee_accounts import BANK, EMP, NOW, SUP, request, stored
from test_payee_accounts import env as payee  # noqa: F401
from test_payroll_schedule import (
    ACCOUNTS,
    PAYROLL,
    REQ,
    SCHEDULE,
    SETTINGS,
    SLIP,
    RowDict,
    ScheduleDoc,
    bank_line,
    new_form,
    schedule_row,
)
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the claims fixture extends it)
from test_payroll_schedule import env as site  # noqa: F401
from test_r4_buttons_js import DT, INDICATOR, run
from test_r4_connections import BASES, _hooked
from test_r4_connections import env as conn  # noqa: F401
from test_salary_entry import COMPLETED, GL
from test_salary_entry import env as salary_env  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
B2B = "Mpesa B2B Payment Request"
STATUS = "Mpesa B2C Status Query"
B2B_TYPES = ("Paybill", "Till (Buy Goods)", "Bank Account (via Bank Paybill)")


# ================================================================ cancelling an account that was paid
class _Document:
    """Frappe's BaseDocument.get reads the instance's __dict__ (v15 and v16), never the class."""

    def get(self, key, default=None):
        return self.__dict__.get(key, default)


def back_links(doc, links):
    """frappe.model.delete_doc.get_linked_docs(doc, method="Cancel"), transcribed (v15 = v16):
    submitted records linking the document, less ``doc.get("ignore_linked_doctypes")``, matched on
    the linking DocType and, for a child row, its parent's DocType."""
    ignored = set(_Document.get(doc, "ignore_linked_doctypes") or ())
    return [link for link in links
            if link["doctype"] not in ignored and (link.get("parenttype") or link["doctype"]) not in ignored
            and link["docstatus"] == 1]


PAID_LINKS = [
    {"doctype": B2B, "name": "MB2B-26-10-000002", "docstatus": 1},  # Paid (or Failed, Unknown): stays submitted
    {"doctype": "M-Pesa Claim Payee", "name": "row-1", "parenttype": EC, "docstatus": 1},
]


@pytest.mark.parametrize("module, cls", [
    ("supplier_m_pesa_account", "SupplierMPesaAccount"), ("employee_m_pesa_account", "EmployeeMPesaAccount"),
])
def test_an_account_a_payout_or_claim_names_is_cancelled_all_the_same(payee, monkeypatch, module, cls):
    # the doctype package sets up its logger on import (b2b_fixture's site stubs it the same way)
    monkeypatch.setattr(payee.frappe.utils, "logger", types.SimpleNamespace(set_log_level=lambda *a, **kw: None),
                        raising=False)
    monkeypatch.setattr(payee.frappe, "logger", lambda *a, **kw: types.SimpleNamespace(), raising=False)
    controller = importlib.import_module(f"mpsa_suite.mpsa_suite.doctype.{module}.{module}")
    doc = getattr(controller, cls)()
    doc.is_default = 1
    assert back_links(doc, PAID_LINKS) == PAID_LINKS  # what Frappe refused before (LinkExistsError)
    doc.before_cancel()
    assert doc.is_default == 0
    assert vars(doc)["ignore_linked_doctypes"] == (B2B, EC)  # on the instance: Frappe's get never sees the class
    assert back_links(doc, PAID_LINKS) == []
    assert payee.pa.CANCEL_IGNORES == (B2B, EC)


def test_every_submitted_record_that_links_an_account_is_ignored_on_cancel(payee):
    linking = {}
    for path in (APP / "mpsa_suite" / "doctype").glob("*/*.json"):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("doctype") != "DocType":
            continue
        for field in meta.get("fields", []):
            if (field.get("fieldtype") == "Link" and field.get("options") in (SUP, EMP)
                    and field.get("fieldname") != "amended_from"):
                linking[meta["name"]] = meta
    assert set(linking) == {B2B, "M-Pesa Claim Payee"}, "a new link to a payee account: add it to CANCEL_IGNORES"
    assert linking[B2B].get("is_submittable") and linking["M-Pesa Claim Payee"].get("istable")
    migrate = (APP / "mpsa_suite" / "migrate.py").read_text(encoding="utf-8")
    claims_table = migrate.split('"Expense Claim": [', 1)[1].split("]", 1)[0]
    assert '"options": "M-Pesa Claim Payee"' in claims_table  # the payees table is the claim's
    assert set(payee.pa.CANCEL_IGNORES) == {B2B, EC}


@pytest.mark.parametrize("folder, doctype", [("supplier_m_pesa_account", SUP), ("employee_m_pesa_account", EMP)])
def test_the_cancel_all_dialog_never_offers_to_cancel_the_payouts_or_claims(tmp_path, folder, doctype):
    got = run(tmp_path, [DT / folder / f"{folder}.js"], f"""
const frm = mk_frm({json.dumps(doctype)}, {{}});
handlers[{json.dumps(doctype)}].setup(frm);
return frm.ignore_doctypes_on_cancel_all;
""")
    assert sorted(got) == sorted([B2B, EC])


@pytest.mark.parametrize("doctype", [SUP, EMP])
def test_an_amended_account_starts_unblocked_so_approving_it_again_clears_the_block(payee, doctype):
    """Amend copies the cancelled account's no-copy fields too (frappe.model.copy_doc with from_amend,
    v15 and v16): the draft forgets the old block and the old payment."""
    stored(payee, doctype, "ACC-1", docstatus=2, needs_reapproval=1, reapproval_reason=payee.pa.SELF_APPROVED,
           name_mismatch=1, last_receiver_name="888880 - SOMEONE ELSE", last_paid_on=NOW)
    row = dict(payee.frappe.db.table(doctype)["ACC-1"], name=None, docstatus=0, amended_from="ACC-1",
               doctype=doctype)
    amended = payee.frappe.get_doc(row)
    payee.pa.validate_account(amended)
    assert (amended.needs_reapproval, amended.name_mismatch) == (0, 0)
    assert (amended.reapproval_reason, amended.last_receiver_name, amended.last_paid_on) == (None, None, None)
    assert not payee.pa.blocked(amended)


# ================================================================ the default, never another one silently
def _on_hold():
    return NOW + datetime.timedelta(hours=20)


@pytest.mark.parametrize("doctype, field, party", [(SUP, "supplier", "Total Kenya"), (EMP, "employee", "EMP-1")])
def test_a_default_on_hold_is_not_skipped_for_an_older_account(payee, doctype, field, party):
    """Plan B.4: the party moved to a new paybill (the default, in its hold period); the old one can
    still be paid. A payment that names no account waits; the payer may name the old one."""
    stored(payee, doctype, "ACC-OLD", shortcode="111111")
    stored(payee, doctype, "ACC-NEW", shortcode="222222", is_default=1, active_from=_on_hold())
    assert payee.pa.pick_account(doctype, party, B2B_TYPES) is None
    problem = payee.pa.pick_problem(doctype, party, B2B_TYPES)
    assert "ACC-NEW is on hold until" in problem and "default: pick another approved account" in problem
    module = payee.sa if doctype == SUP else payee.ea
    with pytest.raises(payee.frappe.ValidationError, match="ACC-NEW is on hold until"):
        module.apply_destination(request(payee, **{field: party}))
    doc = request(payee, **{field: party, f"{field}_mpesa_account": "ACC-OLD"})  # the payer's own choice
    module.apply_destination(doc)
    assert doc.receiver_shortcode == "111111"


def test_a_blocked_default_is_not_skipped_either_and_an_unblocked_one_is_picked(payee):
    stored(payee, SUP, "MSA-OLD", shortcode="111111")
    stored(payee, SUP, "MSA-NEW", shortcode="222222", is_default=1, name_mismatch=1, last_receiver_name="999 - X")
    assert payee.sa.pick_account("Total Kenya") is None
    assert "MSA-NEW is blocked" in payee.pa.pick_problem(SUP, "Total Kenya", B2B_TYPES)
    payee.frappe.db.table(SUP)["MSA-NEW"]["name_mismatch"] = 0
    assert payee.sa.pick_account("Total Kenya") == "MSA-NEW"
    assert payee.pa.pick_problem(SUP, "Total Kenya", B2B_TYPES) is None


def test_with_no_default_the_only_account_that_can_be_paid_is_picked(payee):
    stored(payee, SUP, "MSA-1", shortcode="111111")
    stored(payee, SUP, "MSA-2", shortcode="222222", active_from=_on_hold())
    assert payee.sa.pick_account("Total Kenya") == "MSA-1"
    stored(payee, SUP, "MSA-3", shortcode="333333")
    assert payee.sa.pick_account("Total Kenya") is None  # two can be paid, none is the default
    assert payee.pa.pick_problem(SUP, "Total Kenya", B2B_TYPES) is None  # the request asks to pick one


def test_a_phone_default_leaves_b2b_to_the_only_bank_account(payee):
    stored(payee, EMP, "MEA-1", account_type="Phone (B2C)", phone="254722000111", shortcode=None,
           account_number=None, is_default=1, active_from=_on_hold())
    stored(payee, EMP, "MEA-2", account_type=BANK, bank_paybill="KCB", shortcode="522522",
           account_number="1234567890123")
    assert payee.pa.pick_account(EMP, "EMP-1", B2B_TYPES) == "MEA-2"
    assert payee.pa.pick_account(EMP, "EMP-1", ("Phone (B2C)",)) is None  # its default phone is on hold


# ================================================================ approvals without a second named person
def _migrating(payee, refuse_administrator=None):
    db = payee.frappe.db
    db.table("DocType").update({d: {"name": d} for d in (SUP, EMP)})
    db.columns = db.columns | {(dt, f) for dt in (SUP, EMP)
                               for f in ("approved_by", "needs_reapproval", "reapproval_reason", "entered_by")}
    if refuse_administrator is not None:
        db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["refuse_administrator_payouts"] = refuse_administrator


def test_migrate_blocks_accounts_approved_without_a_second_named_person(payee, monkeypatch):
    _migrating(payee)
    # Trident's MSA-00002 (handover F3): entered and approved with the shared Administrator login
    stored(payee, SUP, "MSA-00002", shortcode="835811", account_type="Till (Buy Goods)", account_number=None,
           owner="Administrator", approved_by="Administrator")
    stored(payee, SUP, "MSA-ADMIN", shortcode="111111", owner="clerk@example.com", approved_by="Administrator")
    stored(payee, SUP, "MSA-OK", shortcode="222222", owner="clerk@example.com", entered_by="clerk@example.com",
           approved_by="manager@example.com")
    stored(payee, SUP, "MSA-OLD", shortcode="333333")  # approved before approvals were recorded
    stored(payee, EMP, "MEA-SELF", owner="hr@example.com", entered_by="Manager@Example.com",
           approved_by="manager@example.com")
    stored(payee, EMP, "MEA-BANK", shortcode="522522", account_type=BANK, bank_paybill="KCB",
           account_number="1234567890123", owner="Administrator", approved_by="Administrator", needs_reapproval=1,
           reapproval_reason="Bank paybill KCB changed from 522522 to 522523: cancel and amend this account so a "
                             "manager approves the new number.")
    assert payee.pa.block_unchecked_approvals() == {SUP: 2, EMP: 1}
    sup, emp = payee.frappe.db.table(SUP), payee.frappe.db.table(EMP)
    assert [n for n, r in sup.items() if r["needs_reapproval"]] == ["MSA-00002", "MSA-ADMIN"]
    assert sup["MSA-00002"]["reapproval_reason"] == payee.pa.SELF_APPROVED
    assert sup["MSA-ADMIN"]["reapproval_reason"] == payee.pa.ADMIN_APPROVED
    assert emp["MEA-SELF"]["reapproval_reason"] == payee.pa.SELF_APPROVED  # entered_by, any case
    assert emp["MEA-BANK"]["reapproval_reason"].startswith("Bank paybill KCB changed")  # already blocked: kept
    problem = payee.sa.usable_problem(payee.sa.get_account("MSA-00002"), "Total Kenya")
    assert "is blocked: Approved by the person who entered it" in problem and "cancel and amend it" in problem
    assert {c[1] for c in payee.frappe.comments} == {"MSA-00002", "MSA-ADMIN", "MEA-SELF"}
    (sup_alert, emp_alert) = payee.alerts
    assert sup_alert[1].startswith("M-Pesa Suite upgrade: 2 Supplier M-Pesa Accounts approved without a second")
    assert emp_alert[1].startswith("M-Pesa Suite upgrade: 1 Employee M-Pesa Account approved without")
    assert "MEA-SELF" not in emp_alert[1] and emp_alert[0] is None  # salary data: a count, no names
    assert payee.pa.block_unchecked_approvals() == {SUP: 0, EMP: 0}  # once per account
    assert len(payee.alerts) == 2


def test_an_administrator_approval_stands_where_the_site_allows_administrator(payee):
    _migrating(payee, refuse_administrator=0)
    stored(payee, SUP, "MSA-ADMIN", owner="clerk@example.com", approved_by="Administrator")
    stored(payee, SUP, "MSA-SELF", shortcode="222222", owner="Administrator", approved_by="Administrator")
    assert payee.pa.block_unchecked_approvals()[SUP] == 1
    assert payee.frappe.db.table(SUP)["MSA-ADMIN"]["needs_reapproval"] == 0
    assert payee.frappe.db.table(SUP)["MSA-SELF"]["needs_reapproval"] == 1  # self-approved all the same


def test_after_migrate_runs_the_check(payee, monkeypatch):
    _migrating(payee)
    payee.frappe.db.columns = payee.frappe.db.columns | {(SUP, "destination_key"), (EMP, "destination_key")}
    monkeypatch.setattr(payee.frappe.db, "sql", lambda *a, **kw: [], raising=False)
    stored(payee, SUP, "MSA-1", owner="Administrator", approved_by="Administrator")
    payee.pa.after_migrate()
    assert payee.frappe.db.table(SUP)["MSA-1"]["needs_reapproval"] == 1


def test_a_bank_paybill_coming_back_never_unblocks_an_account_blocked_for_its_approval(payee):
    bank_paybills = importlib.import_module("mpsa_suite.money_out.bank_paybills")
    common = dict(account_type=BANK, bank_paybill="KCB", shortcode="522522", expected_name="KCB Paybill AC")
    stored(payee, EMP, "MEA-SELF", account_number="1234567890123", needs_reapproval=1,
           reapproval_reason=payee.pa.SELF_APPROVED, **common)
    stored(payee, EMP, "MEA-OK", account_number="1234567890124", **common)
    moved = fake_frappe._dict(name="KCB", paybill="522599")
    assert bank_paybills.flag_accounts(moved, "522522")[EMP] == 1  # MEA-OK only
    table = payee.frappe.db.table(EMP)
    assert table["MEA-SELF"]["reapproval_reason"] == payee.pa.SELF_APPROVED
    assert table["MEA-OK"]["reapproval_reason"].startswith("Bank paybill KCB changed from 522522 to 522599")
    bank_paybills.flag_accounts(fake_frappe._dict(name="KCB", paybill="522522"), "522599")
    assert (table["MEA-OK"]["needs_reapproval"], table["MEA-SELF"]["needs_reapproval"]) == (0, 1)
    assert table["MEA-SELF"]["reapproval_reason"] == payee.pa.SELF_APPROVED


# ================================================================ salary lines on a schedule (D4)
SALARY_LINES = [
    {"party": "EMP-1", "amount": 1000000, "reference_doctype": SLIP, "reference_name": "SAL-1"},
    {"party": "EMP-1", "amount": 1, "reference_doctype": SLIP, "reference_name": "SAL-1"},
    {"party": "EMP-1", "amount": 1, "reference_doctype": None, "reference_name": None},  # no document: a salary too
]


@pytest.mark.parametrize("line", SALARY_LINES)
def test_who_may_not_see_salaries_cannot_plan_one_and_learns_nothing_from_the_refusal(site, line):
    site.frappe.session.user, site.frappe.user_roles = ACCOUNTS, {"Accounts User"}
    doc = new_form(site, [line], purpose="General")
    with pytest.raises(site.frappe.PermissionError) as refused:
        site.schedule.validate_schedule(doc)
    text = str(refused.value)
    assert text.startswith("Row 1: a salary line. Not permitted: salaries are shown only")
    for secret in ("50,000", "50000", "247247", "0123456789", "MEA-1", "Jane"):
        assert secret not in text
    row = doc.lines[0]
    assert not (row.get("destination") or row.get("payee_account") or row.get("payee_name"))  # nothing was read


def test_who_may_see_salaries_plans_them_and_the_payroll_builders_are_not_asked_twice(site):
    site.frappe.session.user, site.frappe.user_roles = ACCOUNTS, {"Accounts User", "HR User"}
    doc = new_form(site, [SALARY_LINES[1]])
    site.schedule.validate_schedule(doc)
    assert doc.lines[0].destination == "247247 / 0123456789"
    site.frappe.user_roles = {"Accounts User"}
    doc = new_form(site, [SALARY_LINES[1]])
    doc.flags.mpesa_from_payroll = True  # payroll.py checked require_salary_preparer itself
    site.schedule.validate_schedule(doc)
    supplier = RowDict(idx=1, party_type="Supplier", party="ACME", reference_doctype=None)
    site.schedule._check_salary_visibility(new_form(site, []), supplier)  # a supplier line: nothing to check


def _saved_schedule(site, lines):
    return ScheduleDoc(env=site, flags=fake_frappe._dict(), _saved=True, name="MPS-1", title="Salaries October",
                       company="Astral", b2b_settings=SETTINGS, b2c_settings=SETTINGS, doctype=SCHEDULE,
                       lines=[RowDict(line) for line in lines])


def test_a_salary_line_already_saved_is_neither_kept_nor_removed_by_who_may_not_see_salaries(site):
    schedule_row(site, purpose="General", payroll_entry=None, payroll_doctype=None)
    line = bank_line(site)
    site.frappe.session.user = ACCOUNTS  # the schedule's creator (salary_access shows it to them)
    site.frappe.user_roles = {"Accounts User"}
    for lines in ([dict(line)], [dict(line, amount=1)], []):
        with pytest.raises(site.frappe.PermissionError, match="a salary line"):
            site.schedule.validate_schedule(_saved_schedule(site, lines))
    site.frappe.user_roles = {"Accounts User", "HR Manager"}
    site.schedule.validate_schedule(_saved_schedule(site, [dict(line)]))


# ================================================================ HRMS's bank entry for a payroll
def journal(*rows, voucher_type="Bank Entry"):
    return fake_frappe._dict(doctype="Journal Entry", voucher_type=voucher_type,
                             accounts=[fake_frappe._dict(r) for r in rows])


PAYABLE = {"account": "Payroll Payable - AIL", "reference_type": PAYROLL, "reference_name": "PAY-1",
           "debit_in_account_currency": 115000.40}
BANK_ROW = {"account": "Bank - AIL", "credit_in_account_currency": 115000.40}


def per_employee(*employees):
    return [dict(PAYABLE, party_type="Employee", party=e, debit_in_account_currency=1000) for e in employees]


def test_hrms_bank_entry_for_the_whole_payroll_is_refused_while_mpesa_plans_a_salary(site):
    schedule_row(site)
    bank_line(site)  # SAL-1 of EMP-1, Planned
    with pytest.raises(site.frappe.ValidationError, match="pay a payroll either through M-Pesa or through Make "
                                                          "Bank Entry, or split it into separate Payroll Entries"):
        site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))
    with pytest.raises(site.frappe.ValidationError, match=r"\(1 M-Pesa payout or planned line: MPS-1 row 1\)"):
        site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))  # a Payer sees salaries: named
    site.frappe.user_roles = {"Accounts User"}
    with pytest.raises(site.frappe.ValidationError) as refused:
        site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))
    assert "(1 M-Pesa payout or planned line)" in str(refused.value) and "MPS-1" not in str(refused.value)


def test_a_closed_schedule_or_a_cancelled_line_does_not_stop_the_bank_entry(site):
    schedule_row(site, closed=1)
    bank_line(site)  # planned on a closed schedule: never paid
    site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))
    schedule_row(site)
    bank_line(site, status="Cancelled")
    site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))


@pytest.mark.parametrize("status, refused", [("Paid", True), ("Initiated", True), ("Failed", False)])
def test_a_salary_paid_or_on_its_way_stops_the_bank_entry_even_once_booked(site, status, refused):
    site.request("MB2B-9", employee="EMP-2", salary_slip="SAL-2", payroll_entry="PAY-1", supplier=None,
                 status=status, journal_entry="ACC-JV-1" if status == "Paid" else None)
    if refused:
        with pytest.raises(site.frappe.ValidationError, match="MB2B-9"):
            site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))
    else:
        site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))


def test_booked_per_employee_only_the_rows_of_employees_mpesa_pays_are_refused(site):
    schedule_row(site)
    bank_line(site)  # EMP-1, planned
    site.frappe.db.table(REQ)["B2C-3"] = {"name": "B2C-3", "docstatus": 1, "status": "Paid",
                                          "reference_doctype": SLIP, "reference_name": "SAL-3"}  # EMP-3, paid
    with pytest.raises(site.frappe.ValidationError) as refused:
        site.payroll.guard_bank_entry(journal(*per_employee("EMP-1", "EMP-2", "EMP-3", "EMP-4"), BANK_ROW))
    text = str(refused.value)
    assert text.startswith("M-Pesa pays the salaries of EMP-1, EMP-3 on PAY-1 (2 M-Pesa payouts or planned lines")
    assert "Leave them out of this bank entry" in text and "EMP-2" not in text
    site.payroll.guard_bank_entry(journal(*per_employee("EMP-2", "EMP-4"), BANK_ROW))  # the others: made by hand


def test_mpesas_own_entries_and_other_vouchers_are_left_alone(site):
    schedule_row(site)
    bank_line(site)
    site.payroll.guard_bank_entry(journal(dict(PAYABLE, mpesa_b2b_payment_request="MB2B-1"), BANK_ROW))
    site.payroll.guard_bank_entry(journal(dict(PAYABLE, b2c_payment_disbursement="MPESA-B2C-1"), BANK_ROW))
    site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW, voucher_type="Journal Entry"))  # the accrual
    site.payroll.guard_bank_entry(journal({"account": "Bank - AIL", "credit_in_account_currency": 1},
                                          {"account": "Expenses - AIL", "debit_in_account_currency": 1}))


def test_the_guard_runs_on_every_journal_entry_save():
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    events = next(ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "doc_events")
    assert events["Journal Entry"]["validate"] == "mpsa_suite.money_out.payroll.guard_bank_entry"


# ================================================================ one cost center on the salary entry
def _rows(je):
    return [dict(r) for r in je._row["accounts"]]


def test_both_rows_of_a_schedule_salary_entry_take_the_payrolls_cost_center(salary_env):
    salary_env.slip()
    je, _ = salary_env.salary.build("SAL-1", 50000, "QRS1", COMPLETED.date(), GL, b2b_fixture.BANK_ACCOUNT, "x", 0,
                                    "mpesa_b2b_payment_request", "MB2B-1", settings=SETTINGS)
    payable, round_off, credit = _rows(je)
    assert (payable["cost_center"], credit["cost_center"]) == ("HQ - AIL", "HQ - AIL")
    assert round_off["cost_center"] == "Main - AIL"  # the company's round-off cost center, as before


def test_without_one_on_the_payroll_both_rows_take_the_shortcodes_then_the_companys(salary_env):
    salary_env.slip()
    salary_env.frappe.db.table(PAYROLL)["PAY-1"]["cost_center"] = None
    salary_env.frappe.db.table("Mpesa Settings")[SETTINGS]["cost_center"] = "Nairobi - AIL"
    doc = fake_frappe._dict(name="MB2B-1", salary_slip="SAL-1", employee="EMP-1", amount=50000, transaction_id="QS1",
                            mpesa_settings=SETTINGS)
    je, _ = salary_env.salary.for_b2b(doc, b2b_fixture.BANK_ACCOUNT, GL, COMPLETED.date())
    assert [r.get("cost_center") for r in _rows(je)] == ["Nairobi - AIL", "Main - AIL", "Nairobi - AIL"]
    salary_env.frappe.db.table("Mpesa Settings")[SETTINGS]["cost_center"] = None
    je, _ = salary_env.salary.for_b2b(doc, b2b_fixture.BANK_ACCOUNT, GL, COMPLETED.date())
    payable, _round_off, credit = _rows(je)
    assert (payable["cost_center"], credit["cost_center"]) == ("Main - AIL", "Main - AIL")  # the company's default


# ================================================================ a claim's payouts on its Connections
def _counting(conn, *, salaries=False, reads=()):
    conn.monkeypatch.setattr(conn.connections, "counting", lambda: True)
    conn.monkeypatch.setattr(conn.salary_access, "can_see_salaries", lambda user=None: salaries)
    conn.monkeypatch.setattr(conn.salary_access, "reads_documents", lambda user=None: tuple(reads))


@pytest.mark.parametrize("doctype, b2b_field", [(EC, "expense_claim"), (EA, "employee_advance")])
def test_who_reads_a_claim_or_advance_gets_its_payouts_counted(conn, doctype, b2b_field):
    """An Accounts User + Expense Approver: no salary, reads claims and advances (plan C.8)."""
    _counting(conn, reads=(EC, EA))
    fields = _hooked(conn, doctype, BASES[doctype])["non_standard_fieldnames"]
    assert fields[B2B] == b2b_field
    assert fields["B2C Disbursement Request"] == fields["B2C Payment Disbursement"] == "reference_name"
    _counting(conn, reads=tuple(d for d in (EC, EA) if d != doctype))  # reads the other one only
    fields = _hooked(conn, doctype, BASES[doctype])["non_standard_fieldnames"]
    assert fields[B2B] == "name" and fields["B2C Disbursement Request"] == "name"
    _counting(conn, salaries=True)
    assert _hooked(conn, doctype, BASES[doctype])["non_standard_fieldnames"][B2B] == b2b_field


def test_a_salary_slip_counts_nothing_for_who_only_reads_claims(conn):
    _counting(conn, reads=(EC, EA))
    fields = _hooked(conn, "Salary Slip", BASES["Salary Slip"])["non_standard_fieldnames"]
    assert fields[B2B] == "name" and fields["B2C Disbursement Request"] == "name"


def test_a_source_the_claim_reader_cannot_read_still_counts_nothing(conn):
    _counting(conn, reads=(EC, EA))
    conn.frappe.denied = {B2B}
    assert _hooked(conn, EC, BASES[EC])["non_standard_fieldnames"][B2B] == "name"


# ================================================================ status queries and Versions (D4)
class Record(fake_frappe._dict):
    def is_new(self):
        return False


def _status_queries(claims, monkeypatch):
    columns = b2b_fixture.COLUMNS
    monkeypatch.setitem(columns, STATUS, ("name", "b2c_request", "b2b_request", "owner"))
    for name, b2b, b2c in (("Q-SUP", "MB2B-1", None), ("Q-SAL", "MB2B-SAL", None), ("Q-EXP", "MB2B-EXP", None),
                           ("Q-ADV", "MB2B-ADV", None), ("Q-NODOC", "MB2B-NODOC", None),
                           ("Q-B2C-EXP", None, "B2C-EXP"), ("Q-B2C-SAL", None, "B2C-SAL")):
        claims.frappe.db.table(STATUS)[name] = {"name": name, "b2b_request": b2b, "b2c_request": b2c,
                                                "owner": "hr@example.com"}


def _shown(claims, salary, doctype, condition=None):
    condition = condition or salary.query_conditions(PREPARER, doctype)
    return [r[0] for r in claims.frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE {condition} ORDER BY name")]


def test_a_status_query_about_a_claims_b2b_payout_is_shown_to_who_reads_the_claim(claims, monkeypatch):
    salary = importlib.import_module("mpsa_suite.money_out.salary_access")
    claim_payouts(claims, monkeypatch)
    _status_queries(claims, monkeypatch)
    claims.frappe.session.user, claims.frappe.user_roles = PREPARER, {"Accounts User", "Expense Approver"}
    readers(claims, monkeypatch, HRMS_READERS)
    assert _shown(claims, salary, STATUS) == ["Q-ADV", "Q-B2C-EXP", "Q-EXP", "Q-SUP"]
    for name, shown in (("Q-EXP", True), ("Q-ADV", True), ("Q-SUP", True), ("Q-SAL", False), ("Q-NODOC", False),
                        ("Q-B2C-EXP", True), ("Q-B2C-SAL", False)):
        query = Record(claims.frappe.db.table(STATUS)[name], doctype=STATUS)
        assert salary.has_permission(query, "read") is shown, name
        assert salary.has_salaries(query) is (name in ("Q-SAL", "Q-NODOC", "Q-B2C-SAL")), name
    # the site gave Accounts User read on claims only: the advance's query stays hidden like a salary
    claims.frappe.user_roles = {"Accounts User"}
    readers(claims, monkeypatch, {**HRMS_READERS, EC: (*HRMS_READERS[EC], "Accounts User")})
    assert _shown(claims, salary, STATUS) == ["Q-B2C-EXP", "Q-EXP", "Q-SUP"]
    assert salary.has_permission(Record(claims.frappe.db.table(STATUS)["Q-ADV"], doctype=STATUS), "read") is False
    # no claim read at all: only the supplier payment's
    readers(claims, monkeypatch, HRMS_READERS)
    assert _shown(claims, salary, STATUS) == ["Q-SUP"]
    assert salary.has_permission(Record(claims.frappe.db.table(STATUS)["Q-EXP"], doctype=STATUS), "read") is False


VERSIONS = (("V-SUP", B2B, "MB2B-1"), ("V-SAL", B2B, "MB2B-SAL"), ("V-EXP", B2B, "MB2B-EXP"),
            ("V-GONE", B2B, "MB2B-DELETED"), ("V-ACC", "Employee M-Pesa Account", "MEA-1"),
            ("V-PINV", "Purchase Invoice", "PINV-1"), ("V-PAYROLL", SCHEDULE, "MPS-1"), ("V-REQ", REQ, "B2C-SAL"),
            ("V-Q", STATUS, "Q-SAL"))


def test_versions_of_salary_records_and_employee_accounts_are_hidden_like_the_records(claims, monkeypatch):
    salary = importlib.import_module("mpsa_suite.money_out.salary_access")
    claim_payouts(claims, monkeypatch)
    _status_queries(claims, monkeypatch)
    schedule_row(claims)  # a payroll schedule: salary data
    for name, ref_doctype, docname in VERSIONS:
        claims.frappe.db.table("Version")[name] = {"name": name, "ref_doctype": ref_doctype, "docname": docname,
                                                   "owner": "hr@example.com"}
    claims.frappe.session.user, claims.frappe.user_roles = PREPARER, {"Accounts User", "Expense Approver"}
    readers(claims, monkeypatch, HRMS_READERS)
    condition = salary.version_query_conditions(PREPARER)
    assert _shown(claims, salary, "Version", condition) == ["V-EXP", "V-PINV", "V-SUP"]
    plain_get_doc = claims.frappe.get_doc
    monkeypatch.setattr(claims.frappe, "get_doc", lambda dt, name=None, **kw: (
        Record(claims.frappe.db.table(dt)[name], doctype=dt) if isinstance(name, str) else plain_get_doc(dt, name, **kw)))
    for name, ref_doctype, docname in VERSIONS:
        version = fake_frappe._dict(ref_doctype=ref_doctype, docname=docname)
        assert salary.version_has_permission(version, "read") is (name in ("V-EXP", "V-PINV", "V-SUP")), name
    claims.frappe.user_roles = {"HR Manager"}  # who may see salaries sees every Version
    assert salary.version_query_conditions(PREPARER) == ""
    assert salary.version_has_permission(fake_frappe._dict(ref_doctype="Employee M-Pesa Account", docname="MEA-1"))


def test_the_version_hooks_are_registered():
    source = (APP / "hooks.py").read_text(encoding="utf-8")
    assert ('permission_query_conditions["Version"] = "mpsa_suite.money_out.salary_access.version_query_conditions"'
            in source)
    assert 'has_permission["Version"] = "mpsa_suite.money_out.salary_access.version_has_permission"' in source


# ================================================================ the payee account lists
@pytest.mark.parametrize("folder, doctype", [("supplier_m_pesa_account", SUP), ("employee_m_pesa_account", EMP)])
def test_payee_account_list_indicators(tmp_path, folder, doctype):
    rows = [({"docstatus": 1, "needs_reapproval": 1, "is_default": 1}, "Blocked: Approve Again"),
            ({"docstatus": 1, "name_mismatch": 1}, "Blocked: Name Did Not Match"),
            ({"docstatus": 1, "is_default": 1}, "Default"), ({"docstatus": 1, "is_default": 0}, "Approved"),
            ({"docstatus": 0}, "Draft"), ({"docstatus": 2}, "Cancelled")]
    got = run(tmp_path, [DT / folder / f"{folder}_list.js"], INDICATOR % (json.dumps(doctype),
                                                                          json.dumps([d for d, _ in rows])))
    assert got == [want for _, want in rows]
    source = (DT / folder / f"{folder}_list.js").read_text(encoding="utf-8")
    for field in ("is_default", "needs_reapproval", "name_mismatch"):
        assert f'"{field}"' in source.split("add_fields:", 1)[1].split("]", 1)[0]
