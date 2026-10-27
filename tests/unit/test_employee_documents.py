"""Expense Claims and Employee Advances paid by M-Pesa (round 4, WP3: money_out.employee_documents,
owed, b2b, b2b_entries, the B2C disbursement and its row, salary_access), on the in-memory site of
test_payroll_schedule (SQL on SQLite) with the real B2B request and B2C disbursement controllers.

Covers: what a claim or advance owes (HRMS's own reading: an advance-covered claim owes nothing,
taxes count, v16's Partially Paid) and what is left with every payout of it counted under its
lock; the payable gate (submitted, approved, not paid at entry, its account, KES, the workflow
states M-Pesa Suite Settings lists, something owed); a B2B request for a claim or advance (the
employee from the document, one document only, the amount capped, the approved bank / paybill /
till account unless the request pays a payee of the claim, a Payer may send it, nobody pays
themselves); its Payment Entry (the claim's own Payable Account, the advance's Advance Account,
the document's employee and project, the rest unallocated and flagged) and Make Entry; the B2C
row and the direct B2C request (the claim may be paid, the approved-phone lock and its payee
skip, the account named for the name check, nobody pays themselves); the B2C cross-guard and
booking account; the buttons' drafts, the headline and the list's bulk prepare; the cancel guard;
claims and advances are not salaries; a new site turns the phone lock on.
"""

import datetime
import importlib
import sys
import types

import pytest

import b2b_fixture
import fake_frappe
from test_payroll_schedule import B2B, DISB, EMPACC, LINE, PAYER, PHONE, REQ, ROW, SETTINGS, SLIP, Row
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture this one extends)

EC, EA = "Expense Claim", "Employee Advance"
PAYABLE, ADVANCES, COMPANY_PAYABLE = "Claims Payable - AIL", "Employee Advances - AIL", "Creditors - AIL"
PREPARER = "accounts@example.com"
NOW = b2b_fixture.NOW


@pytest.fixture
def env(payroll_env, monkeypatch):  # noqa: F811
    env = payroll_env
    frappe = env.frappe
    columns = b2b_fixture.COLUMNS
    monkeypatch.setitem(columns, B2B, columns[B2B] + ("expense_claim", "employee_advance", "claim_payee"))
    monkeypatch.setitem(columns, DISB, columns[DISB] + ("party_type", "transaction_to_pay_against", "owner"))
    monkeypatch.setitem(columns, ROW, columns[ROW] + ("payee_account_doctype", "payee_account", "claim_payee"))
    frappe.db.columns = frappe.db.columns | {(B2B, "expense_claim"), (B2B, "employee_advance")}
    env.ed = importlib.import_module("mpsa_suite.money_out.employee_documents")
    env.settings = fake_frappe._dict()
    monkeypatch.setattr(env.ed, "_settings", lambda: env.settings)
    # WP7's one payout cost-center rule and the document's dimensions (money_out.common)
    common = importlib.import_module("mpsa_suite.money_out.common")
    monkeypatch.setattr(common, "payout_cost_center", lambda settings, company, dt=None, dn=None: f"CC of {dn}",
                        raising=False)
    monkeypatch.setattr(common, "document_dimensions", lambda dt, dn: {"project": f"PROJ of {dn}"} if dt else {},
                        raising=False)
    db = frappe.db
    db.table("Account")[PAYABLE] = {"name": PAYABLE, "company": "Astral", "account_currency": "KES",
                                    "account_type": "Payable"}
    db.table("Account")[ADVANCES] = {"name": ADVANCES, "company": "Astral", "account_currency": "KES",
                                     "account_type": "Receivable"}
    db.table("Company")["Astral"]["default_currency"] = "KES"
    db.table("Employee")["EMP-3"]["cell_number"] = "0711000333"
    claim(env)
    yield env


def claim(env, name="HR-EXP-1", **kw):
    row = {"name": name, "docstatus": 1, "employee": "EMP-1", "employee_name": "Jane Wanjiru", "company": "Astral",
           "approval_status": "Approved", "status": "Unpaid", "is_paid": 0, "payable_account": PAYABLE,
           "project": "PROJ-1", "cost_center": "HQ - AIL", "total_sanctioned_amount": 5000,
           "total_taxes_and_charges": 0, "total_advance_amount": 0, "grand_total": 5000, "total_amount_reimbursed": 0,
           "currency": None, **kw}
    env.frappe.db.table(EC)[name] = row
    return row


def advance(env, name="HR-EAD-1", **kw):
    row = {"name": name, "docstatus": 1, "employee": "EMP-1", "employee_name": "Jane Wanjiru", "company": "Astral",
           "currency": "KES", "exchange_rate": 1, "status": "Unpaid", "advance_account": ADVANCES,
           "advance_amount": 8000, "paid_amount": 0, **kw}
    env.frappe.db.table(EA)[name] = row
    return row


def request(env, name="MB2B-E1", **kw):
    """A B2B request to EMP-1's approved bank account for HR-EXP-1."""
    return env.B2BDoc({"name": name, "mpesa_settings": SETTINGS, "company": "Astral", "amount": 5000,
                       "remarks": "Claim", "expense_claim": "HR-EXP-1", "docstatus": 0, "status": "Not Initiated",
                       "command_id": "BusinessPayBill", **kw})


def paid_request(env, name="MB2B-P1", **kw):
    values = {"supplier": None, "supplier_mpesa_account": None, "employee": "EMP-1", "employee_mpesa_account": "MEA-1",
              "expense_claim": "HR-EXP-1", "receiver_shortcode": "247247", "account_reference": "0123456789",
              "amount": 5000, "transaction_id": "QEXP1", "entry_mode": "Book automatically",
              "entry_attempted_on": NOW, **kw}
    return env.request(name, **values)


def lock_on(env, states=None):
    env.settings.update(b2c_approved_phones_only=1, claim_payable_workflow_states=states)


def row_of(env, **kw):
    controller = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement_reference.b2c_payment_disbursement_reference")
    values = {"idx": 1, "party_type": "Employee", "party": "EMP-2", "reference_doctype": EC,
              "reference_name": "HR-EXP-2", "allocated_amount": 3000, "outstanding_amount": None,
              "partyb": "0722000111", "b2c_disbursement_request": None, **kw}
    return Row(controller.B2CPaymentDisbursementReference, **values)


# ---------------------------------------------------------------- what a claim or advance owes
@pytest.mark.parametrize("values, owes", [
    ({"grand_total": 5000, "total_sanctioned_amount": 5000, "total_amount_reimbursed": 2000}, 3000),
    # covered by an advance: HRMS's grand_total is 0 and nothing is owed (it used to read 800)
    ({"grand_total": 0, "total_sanctioned_amount": 800, "total_advance_amount": 800}, 0),
    # taxes are owed too; part covered by an advance
    ({"grand_total": 1060, "total_sanctioned_amount": 1000, "total_taxes_and_charges": 160,
      "total_advance_amount": 100, "total_amount_reimbursed": 60}, 1000),
    ({"grand_total": 800, "total_amount_reimbursed": 300}, 500),  # read without the sanctioned amount
    ({"total_sanctioned_amount": 800}, 800),
])
def test_a_claim_owes_what_hrms_says(values, owes):
    from mpsa_suite.utils.payouts import owed

    assert owed(EC, values) == owes


def test_an_advance_owes_what_is_not_paid_whatever_its_status():
    from mpsa_suite.utils.payouts import owed

    assert owed(EA, {"advance_amount": 8000, "paid_amount": 3000}) == 5000  # v16 Partially Paid
    assert owed(EA, {"advance_amount": 8000, "paid_amount": 8000}) == 0


def test_get_references_offers_what_hrms_says_a_claim_owes_and_partly_paid_ones(env):
    from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.config import DOCTYPE_CONFIGS

    config = DOCTYPE_CONFIGS[EC]
    assert config.additional_filters == {"approval_status": "Approved", "status": ["in", ["Unpaid", "Partially Paid"]]}
    covered = fake_frappe._dict(grand_total=0, total_sanctioned_amount=800, total_taxes_and_charges=0,
                                total_advance_amount=800, total_amount_reimbursed=0)
    assert config.payable_amount_calc(covered) == 0
    assert "total_claimed_amount" not in config.fields and config.invoice_amount_field == "grand_total"


def test_left_to_pay_counts_every_payout_of_the_claim_under_its_lock(env):
    env.b2c.append({"dt": EC, "dn": "HR-EXP-1", "parent": "MPESA-B2C-X", "allocated_amount": 1000,
                    "request_status": "Initiated"})
    paid_request(env, "MB2B-OPEN", status="Initiated", transaction_id=None, amount=1500)
    env.pe("ACC-PAY-9", None, [(EC, "HR-EXP-1", 500)], docstatus=0, party_type="Employee", party="EMP-1")
    result = env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1", for_update=True)
    assert [(p.kind, p.name, p.amount) for p in result.parts] == [
        ("b2b", "MB2B-OPEN", 1500), ("b2c", "MPESA-B2C-X", 1000), ("draft", "ACC-PAY-9", 500)]
    assert (result.owed, result.left) == (5000, 2000)
    assert "b2c-pay:Expense Claim:HR-EXP-1" in env.locks
    assert env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1", exclude_request="MB2B-OPEN").left == 3500
    with pytest.raises(env.frappe.ValidationError, match="not an Expense Claim or Employee Advance"):
        env.owed.employee_doc_left_to_pay("Purchase Invoice", "PINV-1")


def test_a_paid_b2b_stops_counting_once_its_entry_is_allocated_to_the_claim(env):
    paid_request(env, transaction_id="QEXP1")
    assert env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1").left == 0
    env.frappe.db.table(EC)["HR-EXP-1"].update(total_amount_reimbursed=5000, status="Paid")
    env.pe("ACC-PAY-1", "QEXP1", [(EC, "HR-EXP-1", 5000)], party_type="Employee", party="EMP-1", paid_amount=5000)
    result = env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1")
    assert (result.parts, result.left) == ([], 0)


def test_b2b_open_needs_the_hrms_link_and_the_old_lookup_no_longer_fails(env):
    env.frappe.db.columns = env.frappe.db.columns - {(B2B, "expense_claim")}
    assert env.owed.b2b_open(EC, "HR-EXP-1") == []  # before the migrate makes the field (was a KeyError)


# ---------------------------------------------------------------- the payable gate
@pytest.mark.parametrize("values, problem", [
    ({"docstatus": 0}, "HR-EXP-1 is not submitted."),
    ({"docstatus": 2}, "HR-EXP-1 is cancelled."),
    ({"approval_status": "Rejected"}, "HR-EXP-1 is not approved (Approval Status: Rejected)."),
    ({"is_paid": 1}, "HR-EXP-1 was paid when it was entered (Is Paid)."),
    ({"payable_account": None}, "HR-EXP-1 has no Payable Account."),
    ({"currency": "USD"}, "HR-EXP-1 is in USD. M-Pesa pays KES only."),
    ({"total_amount_reimbursed": 5000, "status": "Paid"}, "HR-EXP-1 has nothing left to pay."),
    ({"grand_total": 0, "total_advance_amount": 5000, "status": "Paid"}, "HR-EXP-1 has nothing left to pay."),
])
def test_what_stops_a_claim_being_paid(env, values, problem):
    claim(env, **values)
    assert env.ed.payable_problem(env.frappe.get_doc(EC, "HR-EXP-1")) == problem


def test_a_v15_claim_takes_its_companys_currency(env):
    env.frappe.db.table("Company")["Astral"]["default_currency"] = "UGX"
    assert env.ed.payable_problem(fake_frappe._dict(doctype=EC, name="HR-EXP-1")) == "HR-EXP-1 is in UGX. M-Pesa pays KES only."


def test_an_unpaid_or_partly_paid_advance_may_be_paid(env):
    advance(env, status="Partially Paid", paid_amount=3000)
    assert env.ed.payable_problem(fake_frappe._dict(doctype=EA, name="HR-EAD-1")) is None
    advance(env, exchange_rate=130)
    assert "exchange rate of 130" in env.ed.payable_problem(fake_frappe._dict(doctype=EA, name="HR-EAD-1"))
    advance(env, advance_account=None)
    assert env.ed.payable_problem(fake_frappe._dict(doctype=EA, name="HR-EAD-1")) == "HR-EAD-1 has no Advance Account."
    env.frappe.db.table("Account")[ADVANCES]["account_currency"] = "USD"
    advance(env)
    assert env.ed.payable_problem(fake_frappe._dict(doctype=EA, name="HR-EAD-1")) == (
        f"{ADVANCES} is in USD. M-Pesa pays KES only.")


def test_the_workflow_states_listed_in_the_settings_may_be_paid(env):
    claim(env, workflow_state="Pending Attachment")
    doc = fake_frappe._dict(doctype=EC, name="HR-EXP-1")
    lock_on(env, "Approved\nPending Attachment\n")
    assert env.ed.payable_problem(doc) is None  # no Workflow on the DocType: the list does not apply
    env.frappe.db.table("Workflow")["Claims"] = {"name": "Claims", "document_type": EC, "is_active": 1,
                                                 "workflow_state_field": "workflow_state"}
    assert env.ed.payable_problem(doc) is None
    env.settings.claim_payable_workflow_states = "Approved"
    assert env.ed.payable_problem(doc) == ("HR-EXP-1 is in workflow state Pending Attachment, which M-Pesa Suite "
                                           "Settings does not list among the workflow states that may be paid.")
    env.settings.claim_payable_workflow_states = ""
    assert env.ed.payable_problem(doc) is None  # the list is blank: any submitted, approved claim


# ---------------------------------------------------------------- B2B for a claim or advance
def test_a_b2b_request_for_a_claim_pays_its_employee_to_an_approved_account(env):
    doc = request(env)
    env.b2b.validate_request(doc)
    assert (doc.employee, doc.employee_mpesa_account, doc.receiver_shortcode, doc.account_reference, doc.entry_mode,
            doc.company) == ("EMP-1", "MEA-1", "247247", "0123456789", "Book automatically", "Astral")


def test_a_b2b_request_for_an_advance(env):
    advance(env)
    doc = request(env, expense_claim=None, employee_advance="HR-EAD-1", amount=8000)
    env.b2b.validate_request(doc)
    assert doc.employee == "EMP-1"
    doc.amount = 8001
    with pytest.raises(env.frappe.ValidationError, match="KES 8,001 is more than is left to pay"):
        env.b2b.validate_request(doc)


@pytest.mark.parametrize("values, msg", [
    ({"salary_slip": "SAL-1", "employee": "EMP-1"}, "one of them, not two"),
    ({"employee_advance": "HR-EAD-1"}, "Link an Expense Claim or an Employee Advance, not both"),
    ({"supplier": "ACME"}, "An Expense Claim or Employee Advance is paid to its employee"),
    ({"employee": "EMP-2", "employee_mpesa_account": None}, "HR-EXP-1 belongs to EMP-1, not EMP-2"),
    ({"amount": 5001}, "KES 5,001 is more than is left to pay. HR-EXP-1 owes 5,000: 5,000 is left to pay."),
])
def test_b2b_request_checks_for_a_claim(env, values, msg):
    advance(env)
    env.frappe.db.table(EMPACC)["MEA-2B"] = dict(env.frappe.db.table(EMPACC)["MEA-1"], name="MEA-2B", employee="EMP-2",
                                                 account_number="9999")
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.b2b.validate_request(request(env, **values))


def test_b2b_counts_b2c_payouts_of_the_claim_and_refuses_one_not_payable(env):
    env.b2c.append({"dt": EC, "dn": "HR-EXP-1", "parent": "MPESA-B2C-X", "allocated_amount": 2000,
                    "request_status": "Initiated"})
    with pytest.raises(env.frappe.ValidationError, match="HR-EXP-1 owes 5,000, less B2C MPESA-B2C-X Initiated 2,000: "
                                                         "3,000 is left to pay"):
        env.b2b.validate_request(request(env, amount=4000))
    claim(env, approval_status="Draft")
    with pytest.raises(env.frappe.ValidationError, match="is not approved"):
        env.b2b.validate_request(request(env, amount=1000))


def test_an_employee_phone_account_is_never_paid_by_b2b_for_a_claim(env):
    claim(env, employee="EMP-2")
    with pytest.raises(env.frappe.ValidationError, match="phone account"):
        env.b2b.validate_request(request(env, employee_mpesa_account="MEA-2"))


def test_a_payer_sends_a_request_its_claim_caps_and_a_manager_one_with_no_document(env):
    env.frappe.session.user, env.frappe.user_roles = PAYER, {"M-Pesa Payer"}
    doc = request(env)
    env.b2b.validate_request(doc, at_submit=True)
    assert "b2c-pay:Expense Claim:HR-EXP-1" in env.locks  # checked again under the claim's lock
    with pytest.raises(env.frappe.PermissionError, match="no Salary Slip, Expense Claim or Employee Advance is paid "
                                                         "by an M-Pesa Manager"):
        env.b2b.validate_request(request(env, name="MB2B-E2", expense_claim=None, employee="EMP-1"), at_submit=True)


def test_nobody_pays_their_own_claim_by_b2b(env):
    env.frappe.db.table("Employee")["EMP-1"]["user_id"] = PAYER
    env.frappe.session.user = PAYER
    with pytest.raises(env.frappe.PermissionError, match="You are EMP-1"):
        env.b2b.validate_request(request(env), at_submit=True)


def test_a_request_for_a_payee_of_the_claim_is_not_held_to_the_employees_accounts(env, monkeypatch):
    ea = importlib.import_module("mpsa_suite.money_out.employee_accounts")
    calls, apply_destination = [], ea.apply_destination
    monkeypatch.setattr(ea, "apply_destination", lambda doc: calls.append("apply") or apply_destination(doc))
    monkeypatch.setattr(ea, "check_before_send", lambda doc: calls.append("check"))
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")  # WP4: the line's own checks
    monkeypatch.setattr(cp, "check_b2b", lambda doc: calls.append("payee"))
    doc = request(env, claim_payee="CP-1", receiver_shortcode="835811", command_id="BusinessBuyGoods", amount=1000)
    env.b2b.validate_request(doc)
    assert calls == ["payee"] and doc.employee == "EMP-1" and not doc.employee_mpesa_account
    calls.clear()
    env.b2b._check_employee_ready(doc)  # the claim's own checks still run, under its lock
    assert calls == ["payee"] and "b2c-pay:Expense Claim:HR-EXP-1" in env.locks
    calls.clear()
    env.b2b.validate_request(request(env, name="MB2B-E3", amount=1000))
    assert calls == ["apply"]


def test_the_send_checks_the_claim_again_under_its_lock(env):
    doc = request(env, employee="EMP-1", employee_mpesa_account="MEA-1", receiver_shortcode="247247",
                  account_reference="0123456789", docstatus=1)
    claim(env, total_amount_reimbursed=5000, status="Paid")  # paid by bank meanwhile
    with pytest.raises(env.frappe.ValidationError, match="HR-EXP-1 has nothing left to pay"):
        env.b2b.check_ready_to_send(doc)
    assert env.b2b.lock_document("MB2B-NONE") == (None, None)
    paid_request(env, "MB2B-L", status="Not Initiated")
    assert env.b2b.lock_document("MB2B-L") == (EC, "HR-EXP-1")


# ---------------------------------------------------------------- its Payment Entry
def booked(env, name="MB2B-P1"):
    pe = env.frappe.db.table(b2b_fixture.PE)[env.frappe.db.table(B2B)[name]["payment_entry"]]
    refs = [r for r in env.frappe.db.table(b2b_fixture.PER).values() if r["parent"] == pe["name"]]
    return pe, refs


def test_a_paid_claim_payout_is_booked_on_the_claims_own_payable_account(env):
    paid_request(env)
    name = env.entries.book_automatically("MB2B-P1")
    pe, refs = booked(env)
    assert pe["name"] == name and pe["docstatus"] == 1
    assert (pe["party_type"], pe["party"], pe["paid_from"], pe["paid_to"], pe["paid_amount"], pe["reference_no"]) == (
        "Employee", "EMP-1", b2b_fixture.GL, PAYABLE, 5000, "QEXP1")
    assert pe["paid_to"] != COMPANY_PAYABLE  # not the company default: ERPNext checks the claim's own
    assert (pe["cost_center"], pe["project"], pe["posting_date"]) == ("CC of HR-EXP-1", "PROJ of HR-EXP-1",
                                                                      datetime.date(2026, 10, 17))
    assert [(r["reference_doctype"], r["reference_name"], r["allocated_amount"]) for r in refs] == [
        (EC, "HR-EXP-1", 5000)]
    assert env.frappe.db.table(B2B)["MB2B-P1"]["entry_status"] == "Entry Made"
    assert "b2c-pay:Expense Claim:HR-EXP-1" in env.locks
    assert env.users[:1] == ["Administrator"]  # booked as Administrator (R9)


def test_a_paid_advance_payout_is_booked_on_its_advance_account(env):
    advance(env)
    paid_request(env, expense_claim=None, employee_advance="HR-EAD-1", amount=8000)
    env.entries.book_automatically("MB2B-P1")
    pe, refs = booked(env)
    assert (pe["paid_to"], refs[0]["reference_doctype"], refs[0]["allocated_amount"]) == (ADVANCES, EA, 8000)


def test_what_the_claim_could_not_take_stays_unallocated_and_is_flagged(env):
    claim(env, total_amount_reimbursed=2000)  # 2,000 reimbursed by bank while M-Pesa paid 5,000
    paid_request(env)
    env.entries.book_automatically("MB2B-P1")
    pe, refs = booked(env)
    assert refs[0]["allocated_amount"] == 3000 and pe["unallocated_amount"] == 2000
    request_row = env.frappe.db.table(B2B)["MB2B-P1"]
    assert request_row["entry_status"] == "Needs Checking"
    assert request_row["entry_note"] == ("HR-EXP-1 could take only KES 3,000 of this payment, so KES 2,000 is unallocated "
                                         "on EMP-1's Payment Entry. Check that the employee was not paid twice.")
    assert env.alerts and "MB2B-P1" in env.alerts[-1][1]


def test_a_cancelled_claim_takes_nothing(env):
    claim(env, docstatus=2)
    paid_request(env)
    env.entries.book_automatically("MB2B-P1")
    pe, refs = booked(env)
    assert refs == [] and pe["party"] == "EMP-1"
    assert "could no longer take this payment (it is cancelled)" in env.frappe.db.table(B2B)["MB2B-P1"]["entry_note"]


def test_classify_a_claim_payout(env):
    paid_request(env, entry_status="Entry Not Made")
    doc = env.frappe.get_doc(B2B, "MB2B-P1")
    on_claim = env.pe("ACC-PAY-1", "QEXP1", [(EC, "HR-EXP-1", 5000)], party_type="Employee", party="EMP-1",
                      paid_amount=5000)
    assert env.entries.classify(doc, env.entries.receipt_entries("QEXP1"))[0] == "Entry Made"
    on_claim["docstatus"] = 0
    assert env.entries.classify(doc, env.entries.receipt_entries("QEXP1"))[0] == "Draft Entry"
    env.frappe.db.table(b2b_fixture.PE).clear()
    env.frappe.db.table(b2b_fixture.PER).clear()
    env.pe("ACC-PAY-2", "QEXP1", [(EC, "HR-EXP-9", 5000)], party_type="Employee", party="EMP-1", paid_amount=5000)
    state, _, note = env.entries.classify(doc, env.entries.receipt_entries("QEXP1"))
    assert state == "Needs Checking" and "allocate it to HR-EXP-1" in note
    env.frappe.db.table(b2b_fixture.PE).clear()
    env.frappe.db.table(b2b_fixture.PER).clear()
    # HRMS's Make Bank Entry: a Journal Entry debiting the claim's payable against the claim
    env.je("ACC-JV-1", "QEXP1", [
        {"account": PAYABLE, "party_type": "Employee", "party": "EMP-1", "reference_type": EC,
         "reference_name": "HR-EXP-1", "debit_in_account_currency": 5000},
        {"account": b2b_fixture.GL, "credit_in_account_currency": 5000}])
    assert env.entries.classify(doc, env.entries.receipt_entries("QEXP1"))[0] == "Entry Made"


def test_make_entry_books_a_claim_and_only_a_manager_moves_it(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    claim(env, "HR-EXP-2")
    paid_request(env, expense_claim=None, entry_mode="Make the entry later")
    with pytest.raises(env.frappe.ValidationError, match="make a Salary Slip, Expense Claim, Employee Advance or Journal"):
        env.entries.make_entry("MB2B-P1", "Purchase Invoice", purchase_invoice="PINV-1")
    result = env.entries.make_entry("MB2B-P1", EC, expense_claim="HR-EXP-1")
    assert result["doctype"] == b2b_fixture.PE and env.frappe.db.table(B2B)["MB2B-P1"]["expense_claim"] == "HR-EXP-1"
    assert env.frappe.db.table(B2B)["MB2B-P1"]["entry_status"] == "Entry Made"
    paid_request(env, "MB2B-P2", transaction_id="QEXP2", entry_mode="Make the entry later")
    with pytest.raises(env.frappe.PermissionError):
        env.entries.make_entry("MB2B-P2", EC, expense_claim="HR-EXP-2")  # moves it off HR-EXP-1
    claim(env, "HR-EXP-3", employee="EMP-2")
    paid_request(env, "MB2B-P3", transaction_id="QEXP3", expense_claim=None)
    with pytest.raises(env.frappe.ValidationError, match="HR-EXP-3 belongs to EMP-2, not EMP-1"):
        env.entries.make_entry("MB2B-P3", EC, expense_claim="HR-EXP-3")
    assert env.entries.entry_facts("MB2B-P1")["expense_claim"] == "HR-EXP-1"


# ---------------------------------------------------------------- B2C: booking, cross-guard, rows, requests
def test_a_b2c_claim_payout_is_booked_to_the_claims_employee_and_payable(env, monkeypatch):
    made = []
    api = types.ModuleType("mpsa_suite.mpsa_suite.api.payment_entry")
    api.create_payment_entry = lambda *a, **kw: made.append((a, kw))
    monkeypatch.setitem(sys.modules, api.__name__, api)
    monkeypatch.delitem(sys.modules, "mpsa_suite.accounting.payment_entry_service", raising=False)
    service = importlib.import_module("mpsa_suite.accounting.payment_entry_service")
    advance(env)
    disbursement = fake_frappe._dict(paid_to=COMPANY_PAYABLE, company="Astral", mode_of_payment="Mpesa-X", name="D-1")
    for dt, dn, account in ((EC, "HR-EXP-1", PAYABLE), (EA, "HR-EAD-1", ADVANCES)):
        ref = fake_frappe._dict(party_type="Employee", party="EMP-9", allocated_amount=500, reference_doctype=dt,
                                reference_name=dn, reference_date=None, reference_no="QB2C1")
        service.create_payment_entry_for_doc(disbursement, ref)
        args, kw = made[-1]
        assert (args[1], kw["party_type"], kw["party_account"]) == ("EMP-1", "Employee", account)
    ref = fake_frappe._dict(party_type="Supplier", party="ACME", allocated_amount=500, reference_doctype="Purchase Invoice",
                            reference_name="PINV-1", reference_date=None, reference_no="QB2C2")
    service.create_payment_entry_for_doc(disbursement, ref)
    assert made[-1][1]["party_account"] == COMPANY_PAYABLE  # other documents keep the disbursement's


def test_the_b2c_guard_counts_b2b_requests_for_the_claim_and_its_payable_gate(env):
    paid_request(env, "MB2B-OPEN", status="Initiated", transaction_id=None)
    disb = env.disb.B2CPaymentDisbursement.__new__(env.disb.B2CPaymentDisbursement)
    disb.__dict__.update(name="D-NEW", doctype=DISB, references=[fake_frappe._dict(
        reference_doctype=EC, reference_name="HR-EXP-1", allocated_amount=1000, b2c_disbursement_request=None)])
    with pytest.raises(env.frappe.ValidationError, match="already has a payout in progress or awaiting accounting: "
                                                         "MB2B-OPEN"):
        disb.validate_no_double_payout()
    env.frappe.db.table(B2B).clear()
    claim(env, approval_status="Rejected")
    with pytest.raises(env.frappe.ValidationError, match="HR-EXP-1 is not approved"):
        disb.validate_no_double_payout()


def test_the_b2c_row_pays_a_payable_claim_of_its_employee(env):
    claim(env, "HR-EXP-2", employee="EMP-2", approval_status="Draft")
    with pytest.raises(env.frappe.ValidationError, match="Row #1: HR-EXP-2 is not approved"):
        row_of(env).validate()
    claim(env, "HR-EXP-2", employee="EMP-1")
    with pytest.raises(env.frappe.ValidationError, match="Row #1: HR-EXP-2 belongs to EMP-1, not EMP-2"):
        row_of(env).validate()


def test_the_b2c_row_names_the_approved_phone_it_pays(env):
    claim(env, "HR-EXP-2", employee="EMP-2")
    row = row_of(env)
    row.validate()
    assert (row.partyb, row.payee_account_doctype, row.payee_account) == (PHONE, EMPACC, "MEA-2")
    row = row_of(env, partyb="0733000000")
    row.validate()  # the setting is off: a typed number is still paid, and names no account
    assert (row.partyb, row.payee_account) == ("254733000000", None)


def test_with_the_lock_on_an_employee_is_paid_by_b2c_only_to_an_approved_phone(env):
    lock_on(env)
    claim(env, "HR-EXP-2", employee="EMP-2")
    with pytest.raises(env.frappe.ValidationError, match="254733000000 is not a phone approved for EMP-2"):
        row_of(env, partyb="0733000000").validate()
    row_of(env).validate()
    env.frappe.db.table(EMPACC)["MEA-2"]["active_from"] = NOW + datetime.timedelta(hours=5)
    with pytest.raises(env.frappe.ValidationError, match="MEA-2 is on hold"):
        row_of(env).validate()
    # a salary row made by hand is held to it too; a payment schedule's row is its line's
    with pytest.raises(env.frappe.ValidationError, match="is not a phone approved"):
        row_of(env, reference_doctype=SLIP, reference_name="SAL-2", partyb="0733000000").validate()


def test_a_row_paying_a_payee_of_the_claim_is_left_to_claim_payees(env, monkeypatch):
    lock_on(env)
    claim(env, "HR-EXP-2", employee="EMP-2", approval_status="Draft")
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")  # WP4: the line's own checks
    seen = []
    monkeypatch.setattr(cp, "check_b2c_row", lambda parent, row: seen.append(row.claim_payee))
    row_of(env, claim_payee="CP-1", partyb="0733000000").validate()  # neither the lock nor the gate here
    assert seen == ["CP-1"]


def test_a_b2c_request_made_by_hand_is_held_to_the_same_rules(env):
    lock_on(env)
    claim(env, "HR-EXP-2", employee="EMP-2")
    req = fake_frappe._dict(reference_doctype=EC, reference_name="HR-EXP-2", party_type="Employee", party="EMP-2",
                            phone_number="254733000000", docstatus=0, flags=fake_frappe._dict())
    with pytest.raises(env.frappe.ValidationError, match="is not a phone approved for EMP-2"):
        env.ed.check_b2c_request(req)
    req.phone_number = PHONE
    env.ed.check_b2c_request(req)
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = PAYER
    req.docstatus = 1  # being sent, by the employee
    with pytest.raises(env.frappe.PermissionError, match="You are EMP-2"):
        env.ed.check_b2c_request(req)
    req.flags.mpesa_from_disbursement = True  # its disbursement's rows were checked
    env.ed.check_b2c_request(req)


def test_nobody_sends_a_b2c_payout_of_their_own_claim(env):
    claim(env, "HR-EXP-2", employee="EMP-2")
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = PAYER
    doc = fake_frappe._dict(flags=fake_frappe._dict(), references=[row_of(env)])
    with pytest.raises(env.frappe.PermissionError, match="Row #1: You are EMP-2"):
        env.ed.check_not_own_b2c(doc)
    doc.flags.mpesa_from_schedule = True  # Pay Selected checked it
    env.ed.check_not_own_b2c(doc)


def test_get_references_takes_the_approved_phone(env):
    identify = env.disb.B2CPaymentDisbursement.get_party_identifier
    assert identify(None, fake_frappe._dict(employee="EMP-2"), "Employee", "Mpesa Disbursement") == {
        "account_number": PHONE}
    assert identify(None, fake_frappe._dict(employee="EMP-3"), "Employee", "Mpesa Disbursement") == {
        "account_number": "0711000333"}  # no approved phone: the Cell Number while the setting is off
    lock_on(env)
    assert identify(None, fake_frappe._dict(employee="EMP-3"), "Employee", "Mpesa Disbursement") == {
        "account_number": ""}
    claim(env, "HR-EXP-2", approval_status="Draft")
    assert [e["name"] for e in env.ed.payable_entries(EC, [{"name": "HR-EXP-1"}, {"name": "HR-EXP-2"}])] == ["HR-EXP-1"]


def test_the_name_check_runs_for_a_row_locked_to_an_approved_phone(env, monkeypatch):
    service = importlib.import_module("mpsa_suite.services.b2c_response_service")
    ea = importlib.import_module("mpsa_suite.money_out.employee_accounts")
    checked = []
    monkeypatch.setattr(ea, "record_receiver_name", lambda payout, account, name: checked.append((account, name)))
    env.frappe.db.table(ROW)["R1"] = {"name": "R1", "payment_status": "Initiated", "payee_account": "MEA-2",
                                      "mpesa_schedule_line": None, "b2c_disbursement_request": "B2C-1"}
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Paid", "transaction_id": "QB2C9",
                                         "transaction_completed_datetime": NOW, "b2c_payment_disbursement_reference": "R1",
                                         "receiver_public_name": "254722****111 - OTIENO OUMA"}
    service.update_b2c_reference_status("B2C-1")
    assert checked == [("MEA-2", "254722****111 - OTIENO OUMA")]
    service.update_b2c_reference_status("B2C-1")  # once, when the row first shows Paid
    assert len(checked) == 1


def test_maker_checker_watches_the_approved_account_of_a_b2c_row(env):
    from mpsa_suite.money_out.maker_checker import PAID

    assert "payee_account" in PAID[DISB][1][1]


# ---------------------------------------------------------------- the buttons, the headline and the list
def test_the_headline_and_what_the_buttons_offer(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    env.b2c.append({"dt": EC, "dn": "HR-EXP-1", "parent": "MPESA-B2C-X", "allocated_amount": 1000,
                    "request_status": "Initiated"})
    status = env.ed.document_status(EC, "HR-EXP-1")
    assert (status["owed"], status["in_flight"], status["left"], status["amount"], status["problem"]) == (
        5000, 1000, 4000, 4000, None)
    assert status["can_prepare"] and status["b2b_default"] == "MEA-1" and status["b2c_settings"] == [SETTINGS]
    assert status["b2b_accounts"][0]["label"].endswith(", account ****6789 (default)")
    env.frappe.user_roles = {"M-Pesa Payer"}  # sees the headline, prepares nothing
    assert "b2b_accounts" not in env.ed.document_status(EC, "HR-EXP-1")
    env.denied.add((EC, "read"))
    with pytest.raises(env.frappe.PermissionError):
        env.ed.document_status(EC, "HR-EXP-1")


def test_pay_employee_by_phone_prepares_a_draft_disbursement_only(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000.6, grand_total=3000.6)
    out = env.ed.prepare_b2c(EC, "HR-EXP-2")
    disbursement = env.frappe.db.table(DISB)[out["name"]]
    (row,) = [r for r in env.frappe.db.table(ROW).values() if r["parent"] == out["name"]]
    assert (out["amount"], disbursement["docstatus"], disbursement["paid_to"], disbursement["mode_of_payment"]) == (
        3000, 0, PAYABLE, f"Mpesa-{SETTINGS}")
    assert (row["reference_name"], row["party"], row["partyb"], row["payee_account"], row["allocated_amount"]) == (
        "HR-EXP-2", "EMP-2", PHONE, "MEA-2", 3000)
    with pytest.raises(env.frappe.ValidationError, match=f"Draft B2C Payment Disbursement {out['name']} already pays"):
        env.ed.prepare_b2c(EC, "HR-EXP-2")


def test_pay_employee_by_phone_refusals(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    lock_on(env)
    with pytest.raises(env.frappe.ValidationError, match="EMP-1 has no approved phone that can be paid now"):
        env.ed.prepare_b2c(EC, "HR-EXP-1")
    env.frappe.user_roles = {"M-Pesa Payer"}  # sends, does not prepare
    with pytest.raises(env.frappe.PermissionError, match="needs one of the roles"):
        env.ed.prepare_b2c(EC, "HR-EXP-1")
    env.frappe.user_roles = {"HR Manager"}
    env.denied.add((DISB, "create"))
    with pytest.raises(env.frappe.PermissionError):
        env.ed.prepare_b2c(EC, "HR-EXP-1")
    with pytest.raises(env.frappe.ValidationError, match="Pick an Expense Claim or an Employee Advance"):
        env.ed.prepare_b2c(SLIP, "SAL-1")


def test_pay_employee_to_a_bank_account_prepares_a_draft_b2b_request(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    advance(env, paid_amount=3000, status="Partially Paid")
    out = env.ed.prepare_b2b(EA, "HR-EAD-1")
    doc = env.frappe.db.table(B2B)[out["name"]]
    assert (doc["docstatus"], doc["employee"], doc["employee_mpesa_account"], doc["employee_advance"], doc["amount"],
            doc["entry_mode"]) == (0, "EMP-1", "MEA-1", "HR-EAD-1", 5000, "Book automatically")
    with pytest.raises(env.frappe.ValidationError, match="Nothing is left to pay by M-Pesa. HR-EAD-1 owes 5,000, less "
                                                         f"{out['name']} Not Initiated 5,000"):
        env.ed.prepare_b2b(EA, "HR-EAD-1")
    with pytest.raises(env.frappe.ValidationError, match="MEA-2 is not a bank, paybill or till account of EMP-1"):
        env.ed.prepare_b2b(EC, "HR-EXP-1", employee_mpesa_account="MEA-2")
    with pytest.raises(env.frappe.ValidationError, match="does not pay Astral by B2B"):
        env.ed.prepare_b2b(EC, "HR-EXP-1", mpesa_settings="Other 1")


def test_pay_by_mpesa_on_the_list_prepares_one_b2c_batch_and_b2b_requests(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User", "HR Manager"}
    lock_on(env)
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000)
    claim(env, "HR-EXP-5", employee="EMP-2", total_sanctioned_amount=700, grand_total=700)
    claim(env, "HR-EXP-3", employee="EMP-3")
    claim(env, "HR-EXP-4", approval_status="Draft")
    claim(env, "HR-EXP-6", company="Other Ltd")
    out = env.ed.prepare_bulk(EC, ["HR-EXP-1", "HR-EXP-2", "HR-EXP-3", "HR-EXP-4", "HR-EXP-5", "HR-EXP-6"])
    assert out["b2c_rows"] == 2 and len(out["b2b"]) == 1
    rows = sorted((r["reference_name"], r["allocated_amount"], r["partyb"]) for r in env.frappe.db.table(ROW).values()
                  if r["parent"] == out["b2c"])
    assert rows == [("HR-EXP-2", 3000, PHONE), ("HR-EXP-5", 700, PHONE)]
    assert env.frappe.db.table(DISB)[out["b2c"]]["docstatus"] == 0
    assert env.frappe.db.table(B2B)[out["b2b"][0]]["expense_claim"] == "HR-EXP-1"
    assert {s["name"]: s["reason"] for s in out["skipped"]} == {
        "HR-EXP-3": "EMP-3 has no approved phone that can be paid now. Add one under Employee > M-Pesa Accounts and "
                    "have a manager approve it.",
        "HR-EXP-4": "HR-EXP-4 is not approved (Approval Status: Draft).",
        "HR-EXP-6": "It is Other Ltd's: tick one company's at a time.",
    }
    assert not env.frappe.local.message_log  # the refusals are listed, not shown as errors


# ---------------------------------------------------------------- cancelling a claim or advance
def test_a_claim_is_not_cancelled_while_a_payout_of_it_is_paid_or_on_its_way(env):
    doc = fake_frappe._dict(doctype=EC, name="HR-EXP-1")
    env.ed.before_cancel(doc)  # nothing paid
    paid_request(env, "MB2B-F", status="Failed")
    env.frappe.db.table(REQ)["B2C-F"] = {"name": "B2C-F", "docstatus": 1, "status": "Failed",
                                         "reference_doctype": EC, "reference_name": "HR-EXP-1"}
    env.ed.before_cancel(doc)  # failed payouts do not hold it
    paid_request(env, "MB2B-C", docstatus=2, status="Paid")  # paid after the request was cancelled: the money left
    env.frappe.db.table(REQ)["B2C-I"] = {"name": "B2C-I", "docstatus": 1, "status": "Initiated",
                                         "reference_doctype": EC, "reference_name": "HR-EXP-1"}
    env.frappe.db.table(DISB)["D-1"] = {"name": "D-1", "docstatus": 1}
    env.frappe.db.table(ROW)["R1"] = {"name": "R1", "parent": "D-1", "parenttype": DISB, "reference_doctype": EC,
                                      "reference_name": "HR-EXP-1", "payment_status": "Initiated",
                                      "b2c_disbursement_request": None}
    with pytest.raises(env.frappe.ValidationError, match="HR-EXP-1 cannot be cancelled while M-Pesa payouts of it are "
                                                         "paid or on their way: MB2B-C, B2C-I, D-1"):
        env.ed.before_cancel(doc)
    assert env.ed.before_cancel(fake_frappe._dict(doctype=EC)) is None  # nothing named: nothing to find


# ---------------------------------------------------------------- not salaries
def test_claim_and_advance_payouts_are_not_salaries(env):
    salary = importlib.import_module("mpsa_suite.money_out.salary_access")
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    env.request("MB2B-1")  # a supplier payment
    env.request("MB2B-SAL", supplier=None, employee="EMP-1", salary_slip="SAL-1")
    env.request("MB2B-EXP", supplier=None, employee="EMP-1", expense_claim="HR-EXP-1")
    env.request("MB2B-NODOC", supplier=None, employee="EMP-1")
    condition = salary.query_conditions("other@example.com", B2B)
    shown = [r[0] for r in env.frappe.db.sql(f"SELECT name FROM `tab{B2B}` WHERE {condition} ORDER BY name")]
    assert shown == ["MB2B-1", "MB2B-EXP"]
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, employee="EMP-1", expense_claim="HR-EXP-1")) is True
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, employee="EMP-1", salary_slip="SAL-1")) is False
    db = env.frappe.db
    db.table(DISB)["D-EXP"] = {"name": "D-EXP", "docstatus": 1, "party_type": "Employee",
                               "transaction_to_pay_against": EC}
    db.table(DISB)["D-SAL"] = {"name": "D-SAL", "docstatus": 1, "party_type": "Employee",
                               "transaction_to_pay_against": SLIP}
    db.table(ROW)["R-EXP"] = {"name": "R-EXP", "parent": "D-EXP", "parenttype": DISB, "party_type": "Employee",
                              "reference_doctype": EC}
    db.table(ROW)["R-SAL"] = {"name": "R-SAL", "parent": "D-SAL", "parenttype": DISB, "party_type": "Employee",
                              "reference_doctype": SLIP}
    condition = salary.query_conditions("other@example.com", DISB)
    assert [r[0] for r in env.frappe.db.sql(f"SELECT name FROM `tab{DISB}` WHERE {condition} ORDER BY name")] == [
        "D-EXP"]
    assert salary.is_salary_line({"party_type": "Employee", "reference_doctype": EA}) is False
    assert salary.is_salary_line({"party_type": "Employee", "reference_doctype": None}) is True
    assert salary.has_salaries(fake_frappe._dict(doctype=DISB, party_type="Employee", transaction_to_pay_against=EC,
                                                 references=[{"party_type": "Employee", "reference_doctype": EC}])) is False


# ---------------------------------------------------------------- a new site
def test_a_new_site_pays_employees_by_b2c_only_to_approved_phones(env):
    install = importlib.import_module("mpsa_suite.install")
    written = []
    env.frappe.db.table("DocType")["M-Pesa Suite Settings"] = {"name": "M-Pesa Suite Settings"}
    env.frappe.db.set_single_value = lambda doctype, field, value: written.append((doctype, field, value))
    install.approved_phones_only_on_new_sites()
    assert written == [("M-Pesa Suite Settings", "b2c_approved_phones_only", 1)]


def test_a_claim_payout_is_not_booked_as_a_salary(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    paid_request(env, entry_mode="Make the entry later")
    with pytest.raises(env.frappe.ValidationError, match="This payment is for Expense Claim HR-EXP-1: make its "
                                                         "Expense Claim entry"):
        env.entries.make_entry("MB2B-P1", SLIP, salary_slip="SAL-1")


def test_the_send_and_the_rows_call_the_checks():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "mpsa_suite" / "mpsa_suite" / "doctype"

    def calls_in(path, cls, method):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls)
        body = next(n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == method)
        return {ast.unparse(c.func) for c in ast.walk(body) if isinstance(c, ast.Call)}

    disbursement = root / "b2c_payment_disbursement" / "b2c_payment_disbursement.py"
    assert "check_not_own_b2c" in calls_in(disbursement, "B2CPaymentDisbursement", "before_submit")
    row = root / "b2c_payment_disbursement_reference" / "b2c_payment_disbursement_reference.py"
    assert "employee_documents.check_b2c_row" in calls_in(row, "B2CPaymentDisbursementReference", "validate")
    request_path = root / "b2c_disbursement_request" / "b2c_disbursement_request.py"
    assert "employee_documents.check_b2c_request" in calls_in(request_path, "B2CDisbursementRequest", "validate")
