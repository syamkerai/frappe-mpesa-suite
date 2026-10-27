"""Round 4 verify pass, group sends: the fixes, on the in-memory sites of the tests they extend
(test_payee_accounts, test_payroll_schedule, test_claim_payees, test_send_confirm).

Covers: a bank paybill coming back never unblocks an account approved without a second named
person, even one migrate skipped because the paybill blocked it; a reversal of M-Pesa's own salary
entry, and any row that credits the payroll payable, passes the bank-entry guard; the send and Send
via M-Pesa count a claim's casual payees against the day's cap by one rule (the supplier account
approved now); a hand-made B2C request to an employee gets no token for who is no manager; the
Payment Request form asks the server which paid prompts are being booked.
"""

import importlib

import fake_frappe
import pytest
from test_claim_payees import claim, disbursement, phone_line, raises, settings
from test_claim_payees import env as claims  # noqa: F401
from test_payee_accounts import BANK, EMP, SUP, stored
from test_payee_accounts import env as payee  # noqa: F401
from test_payroll_schedule import bank_line, schedule_row
from test_payroll_schedule import env as site  # noqa: F401
from test_r4_final_b import BANK_ROW, PAYABLE, journal
from test_send_confirm import B2C as SEND_B2C
from test_send_confirm import (
    B2C_REQUEST,
    CLERK,
    MANAGER,
    MODIFIED,
    PAYER,
    claim_payees,
    prepared,
)
from test_send_confirm import disbursement as send_disbursement
from test_send_confirm import env as send  # noqa: F401


# ================================================================ K4: a paybill coming back
def _paybill_blocked(payee, name, **kw):
    reason = ("Bank paybill KCB changed from 522522 to 522599: cancel and amend this account so a manager approves "
              "the new number.")
    return stored(payee, EMP, name, account_type=BANK, bank_paybill="KCB", shortcode="522522",
                  expected_name="KCB Paybill AC", needs_reapproval=1, reapproval_reason=reason, **kw)


def _columns(payee, refuse_administrator=None):
    db = payee.frappe.db
    db.columns = db.columns | {(dt, "entered_by") for dt in (SUP, EMP)}
    if refuse_administrator is not None:
        db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["refuse_administrator_payouts"] = refuse_administrator


def test_a_paybill_coming_back_keeps_an_account_migrate_skipped_blocked_for_its_approval(payee):
    bank_paybills = importlib.import_module("mpsa_suite.money_out.bank_paybills")
    _columns(payee)
    # blocked by the paybill change first, so migrate's check left it as it was
    _paybill_blocked(payee, "MEA-ADMIN", account_number="1234567890123", owner="hr@example.com",
                     approved_by="Administrator")
    _paybill_blocked(payee, "MEA-SELF", account_number="1234567890124", owner="hr@example.com",
                     entered_by="Manager@Example.com", approved_by="manager@example.com")
    _paybill_blocked(payee, "MEA-OK", account_number="1234567890125", owner="hr@example.com",
                     entered_by="hr@example.com", approved_by="manager@example.com")
    _paybill_blocked(payee, "MEA-OLD", account_number="1234567890126")  # approved before approvals were recorded
    bank_paybills.flag_accounts(fake_frappe._dict(name="KCB", paybill="522522"), "522599")
    table = payee.frappe.db.table(EMP)
    assert (table["MEA-OK"]["needs_reapproval"], table["MEA-OLD"]["needs_reapproval"]) == (0, 0)
    assert table["MEA-ADMIN"]["needs_reapproval"] == 1
    assert table["MEA-ADMIN"]["reapproval_reason"] == payee.pa.ADMIN_APPROVED
    assert table["MEA-SELF"]["needs_reapproval"] == 1
    assert table["MEA-SELF"]["reapproval_reason"] == payee.pa.SELF_APPROVED  # entered_by, any case
    notes = {c[1]: c[2] for c in payee.frappe.comments}
    assert notes["MEA-ADMIN"].startswith("Bank paybill KCB is 522522 again, but the account stays blocked: Approved "
                                         "with the shared Administrator login")
    assert "can be paid again" in notes["MEA-OK"]
    problem = payee.pa.usable_problem(payee.pa.get_account(EMP, "MEA-ADMIN"), None, EMP)
    assert "is blocked: Approved with the shared Administrator login" in problem


def test_where_the_site_allows_administrator_its_approval_is_unblocked_with_the_paybill(payee):
    bank_paybills = importlib.import_module("mpsa_suite.money_out.bank_paybills")
    _columns(payee, refuse_administrator=0)
    _paybill_blocked(payee, "MEA-ADMIN", account_number="1234567890123", owner="hr@example.com",
                     approved_by="Administrator")
    bank_paybills.flag_accounts(fake_frappe._dict(name="KCB", paybill="522522"), "522599")
    assert payee.frappe.db.table(EMP)["MEA-ADMIN"]["needs_reapproval"] == 0


# ================================================================ K6: a reversal pays nobody
def test_a_reversal_of_mpesas_own_salary_entry_is_left_alone(site):
    schedule_row(site)
    bank_line(site)  # M-Pesa plans a salary of PAY-1
    # ERPNext's Reverse Journal Entry: debit and credit swapped, the no_copy tag of the B2B salary dropped
    reversal = journal(dict(PAYABLE, debit_in_account_currency=0, credit_in_account_currency=115000.40),
                       {"account": "Bank - AIL", "debit_in_account_currency": 115000.40})
    reversal.reversal_of = "ACC-JV-1"
    site.payroll.guard_bank_entry(reversal)
    reversal.reversal_of = None  # made by hand: the row credits the payable, so it still pays nobody
    site.payroll.guard_bank_entry(reversal)
    with pytest.raises(site.frappe.ValidationError, match="pay a payroll either through M-Pesa"):
        site.payroll.guard_bank_entry(journal(PAYABLE, BANK_ROW))  # HRMS's bank entry: still refused


def test_only_the_rows_that_debit_the_payable_name_the_employees_paid(site):
    schedule_row(site)
    bank_line(site)  # EMP-1, planned
    rows = [dict(PAYABLE, party_type="Employee", party="EMP-1", debit_in_account_currency=0,
                 credit_in_account_currency=1000),
            dict(PAYABLE, party_type="Employee", party="EMP-2", debit_in_account_currency=1000)]
    site.payroll.guard_bank_entry(journal(*rows, BANK_ROW))  # EMP-1's row pays nobody
    rows[0].update(debit_in_account_currency=1000, credit_in_account_currency=0)
    with pytest.raises(site.frappe.ValidationError, match="M-Pesa pays the salaries of EMP-1 on PAY-1"):
        site.payroll.guard_bank_entry(journal(*rows, BANK_ROW))


# ================================================================ K7: one rule for the day's cap
def test_the_send_counts_a_row_whose_supplier_account_was_disabled_since_the_approval(claims):
    lines = [phone_line(amount=3000), phone_line("LN-2", 2, phone="0711000444", amount=3000)]
    claim(claims, lines)
    claims.frappe.db.table("M-Pesa Claim Payee")["LN-1"]["supplier_account"] = "MSA-OLD"  # approved then, not now
    settings(claims, claim_payee_daily_cap=5000)
    parent, refs = disbursement(claims, [(lines[0], {}), (lines[1], {})], docstatus=1)
    parent.flags = fake_frappe._dict()
    with raises(claims, "with this KES 6,000 it would pass the day's cap of KES 5,000"):
        claims.cp.check_b2c_row(parent, refs[1])


def test_the_send_leaves_out_a_row_whose_supplier_account_is_approved_now(claims, monkeypatch):
    lines = [phone_line(amount=3000), phone_line("LN-2", 2, phone="0711000444", amount=3000)]
    claim(claims, lines)
    monkeypatch.setattr(claims.cp, "approved_supplier_account", lambda line: "MSA-1" if line.name == "LN-1" else None)
    settings(claims, claim_payee_daily_cap=5000)
    parent, refs = disbursement(claims, [(lines[0], {}), (lines[1], {})], docstatus=1)
    parent.flags = fake_frappe._dict()
    for ref in refs:
        claims.cp.check_b2c_row(parent, ref)
    assert claims.cp._capped_rows_total(parent) == 3000


def _payee_rows():
    return [fake_frappe._dict(name=f"R{i}", party_type="Employee", party="EMP-1", partyb=phone, allocated_amount=amount,
                              reference_doctype="Expense Claim", reference_name="EC-9", claim_payee=f"CP-{i}")
            for i, (phone, amount) in enumerate((("254711000111", 800.0), ("254711000222", 700.0)), start=1)]


def test_send_via_mpesa_counts_the_payees_by_the_sends_own_rule(send, monkeypatch):
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    monkeypatch.setattr(cp, "sent_today", lambda *a, **kw: 4200.0)
    claim_payees(send, dict(payee_type="Phone (B2C)", payee_name="Peter Otieno", phone="254711000111", amount=800,
                            supplier_account="MSA-OLD"),  # approved when the claim was, not now
                 dict(payee_type="Phone (B2C)", payee_name="Mary Akinyi", phone="254711000222", amount=700),
                 daily_cap=5000)
    send_disbursement(send, references=_payee_rows(), company="Test Co")
    r = prepared(send, SEND_B2C, "B2C-1")
    assert r["token"] is None and "with this KES 1,500 it would pass the day's cap of KES 5,000" in r["problems"][0]
    monkeypatch.setattr(cp, "approved_supplier_account", lambda line: "MSA-1" if line.name == "CP-1" else None)
    assert prepared(send, SEND_B2C, "B2C-1")["token"]  # 4,200 + 700


# ================================================================ K8: a hand-made B2C request to an employee
def _b2c_request(send, **values):
    send.frappe.db.table(B2C_REQUEST)["B2CR-1"] = dict(dict(
        name="B2CR-1", docstatus=0, owner=CLERK, modified_by=CLERK, modified=MODIFIED, party_type="Employee",
        party="EMP-1", phone_number="254712345678", amount=500.0, mpesa_settings="WORKING"), **values)


def test_a_hand_made_b2c_request_to_an_employee_is_for_a_manager_only(send):
    _b2c_request(send)
    r = prepared(send, B2C_REQUEST, "B2CR-1")
    assert send.frappe.session.user == PAYER
    assert r["token"] is None and r["problems"] == [
        "A payment to an employee with no Salary Slip, Expense Claim or Employee Advance is paid by an M-Pesa Manager."]
    send.frappe.session.user, send.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    assert prepared(send, B2C_REQUEST, "B2CR-1")["token"]


def test_the_payer_rule_comes_before_the_manager_rule(send):
    _b2c_request(send)
    send.frappe.session.user = "jane@x"  # EMP-1's user, an M-Pesa Payer
    problems = prepared(send, B2C_REQUEST, "B2CR-1")["problems"]
    assert problems[0] == "You are EMP-1: another person must pay you." and "M-Pesa Manager" in problems[1]


def test_a_b2c_request_to_a_supplier_needs_no_manager(send):
    _b2c_request(send, party_type="Supplier", party="ACME")
    assert prepared(send, B2C_REQUEST, "B2CR-1")["token"]


# ================================================================ K11: which paid prompts are being booked
@pytest.fixture
def prompts():
    frappe, saved = fake_frappe.install()
    try:
        asked = []

        def has_permission(doctype, ptype="read", doc=None, throw=False, **kw):
            asked.append((doctype, ptype, doc))
            if doc == "PR-SECRET":
                raise frappe.PermissionError("no read")
            return True

        frappe.has_permission = has_permission
        frappe.user_roles = {"M-Pesa Collector"}
        frappe.db.table("Mpesa Express Request").update({
            "MEXP-1": {"name": "MEXP-1", "reference_doctype": "Payment Request", "reference_name": "PR-1",
                       "docstatus": 1, "status": "Completed", "transaction_id": "RX1", "amount": 2000.0,
                       "currency": "KES", "is_reconciled": 0, "creation": "2026-10-27 10:00:00"},
            "MEXP-2": {"name": "MEXP-2", "reference_doctype": "Payment Request", "reference_name": "PR-1",
                       "docstatus": 1, "status": "Completed", "transaction_id": "RX2", "amount": 2000.0,
                       "currency": "KES", "is_reconciled": 0, "creation": "2026-10-27 11:00:00"}})
        module = importlib.import_module("mpsa_suite.money_in.payment_request")
        yield fake_frappe._dict(frappe=frappe, pr=module, asked=asked)
    finally:
        fake_frappe.uninstall(saved)


def test_the_form_is_told_only_the_prompts_the_server_holds_as_being_booked(prompts):
    db = prompts.frappe.db
    # RX1 went back to the customer (a reversal), RX2 was booked through its C2B register: neither is reconciled
    db.table("Mpesa Reversal Request")["MRV-1"] = {"name": "MRV-1", "transaction_id": "RX1", "docstatus": 1,
                                                   "status": "Reversed"}
    db.table("Payment Entry")["ACC-PAY-1"] = {"name": "ACC-PAY-1", "reference_no": "RX2", "docstatus": 1}
    assert prompts.pr.prompts_being_booked("PR-1") == []
    db.table("Payment Entry")["ACC-PAY-1"]["docstatus"] = 2
    assert prompts.pr.prompts_being_booked("PR-1") == [{"name": "MEXP-2", "transaction_id": "RX2"}]
    assert prompts.asked == [("Payment Request", "read", "PR-1")] * 2


def test_only_a_collector_who_reads_the_payment_request_is_told(prompts):
    with pytest.raises(prompts.frappe.PermissionError):
        prompts.pr.prompts_being_booked("PR-SECRET")
    prompts.frappe.user_roles = {"Accounts Viewer"}
    with pytest.raises(prompts.frappe.PermissionError, match="Not permitted"):
        prompts.pr.prompts_being_booked("PR-1")
