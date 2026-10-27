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
from test_payroll_schedule import B2B, DISB, EMPACC, PAYER, PHONE, REQ, ROW, SETTINGS, SLIP, Row
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
    # a request made by hand pays exactly its disbursement row (row_mismatch: tests/unit/test_r4_residual.py)
    env.row_mismatch = env.ed.row_mismatch
    monkeypatch.setattr(env.ed, "row_mismatch", lambda doc: None)
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


def test_left_to_pay_takes_the_claims_lock_by_default_and_the_headline_does_not(env):
    env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1")  # as a preparation calls it (WP4's payees too)
    assert env.locks == ["b2c-pay:Expense Claim:HR-EXP-1"]
    assert ("row", EC, "HR-EXP-1", True) in env.log  # the document row last, a locking read (R4)
    env.locks.clear()
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    env.ed.document_status(EC, "HR-EXP-1")  # the form's headline decides nothing
    assert env.locks == []
    env.ed.prepare_b2b(EC, "HR-EXP-1")  # a preparation reads under it
    assert env.locks == ["b2c-pay:Expense Claim:HR-EXP-1"]


def test_a_paid_b2b_stops_counting_once_its_entry_is_allocated_to_the_claim(env):
    paid_request(env, transaction_id="QEXP1")
    assert env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1").left == 0
    env.frappe.db.table(EC)["HR-EXP-1"].update(total_amount_reimbursed=5000, status="Paid")
    env.pe("ACC-PAY-1", "QEXP1", [(EC, "HR-EXP-1", 5000)], party_type="Employee", party="EMP-1", paid_amount=5000)
    result = env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1")
    assert (result.parts, result.left) == ([], 0)


def payees_held(env, monkeypatch, amount=3000):
    """claim_payees.unpaid_lines for HR-EXP-1: one approved payee line of ``amount`` not paid yet."""
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    asked = []

    def unpaid_lines(claim, exclude_lines=(), for_update=False):
        asked.append((claim, sorted(exclude_lines or ()), for_update, list(env.locks)))
        if claim != "HR-EXP-1" or "LN-1" in (exclude_lines or ()):
            return []
        return [fake_frappe._dict(kind="payee", name="LN-1", line="LN-1", amount=amount,
                                  label=f"payee row #1 (Shell) {amount:,} not paid yet")]

    monkeypatch.setattr(cp, "unpaid_lines", unpaid_lines)
    return asked


def test_a_claims_unpaid_payees_are_not_offered_to_its_employee(env, monkeypatch):
    # final review: a claim of 5,000 approved with a 3,000 till line not paid yet offers its claimant 2,000
    asked = payees_held(env, monkeypatch)
    result = env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1")
    assert [(p.kind, p.name, p.amount) for p in result.parts] == [("payee", "LN-1", 3000)]
    assert (result.owed, result.left) == (5000, 2000)
    assert asked[-1] == ("HR-EXP-1", [], True, ["b2c-pay:Expense Claim:HR-EXP-1"])  # under the claim's lock
    calls = [entry for entry in env.log if entry[0] == "row"]
    assert calls[-1][1:3] == (EC, "HR-EXP-1")  # the claim's row read last (R4)
    assert env.owed.employee_doc_left_to_pay(EC, "HR-EXP-1", exclude_payees=["LN-1"]).left == 5000  # its own share
    advance(env)
    assert env.owed.employee_doc_left_to_pay(EA, "HR-EAD-1").parts == []  # an advance has no payees
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    status = env.ed.document_status(EC, "HR-EXP-1")
    assert (status["left"], status["amount"], status["payees"]) == (2000, 2000, 3000)
    out = env.ed.prepare_b2b(EC, "HR-EXP-1")  # Pay employee (bank / paybill / till): the rest only
    assert (out["amount"], env.frappe.db.table(B2B)[out["name"]]["amount"]) == (2000, 2000)


def test_a_b2b_request_to_the_employee_leaves_the_payees_share_and_a_payees_request_its_own(env, monkeypatch):
    asked = payees_held(env, monkeypatch)
    with pytest.raises(env.frappe.ValidationError, match="KES 2,500 is more than is left to pay. HR-EXP-1 owes "
                                                         "5,000, less payee row #1 .Shell. 3,000 not paid yet: 2,000"):
        env.b2b.validate_request(request(env, amount=2500))
    env.b2b.validate_request(request(env, amount=2000))
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    monkeypatch.setattr(cp, "check_b2b", lambda doc: None)  # the line's own checks (test_claim_payees)
    asked.clear()
    env.b2b.validate_request(request(env, amount=3000, claim_payee="LN-1", employee_mpesa_account=None,
                                     command_id="BusinessBuyGoods", receiver_shortcode="835811"))
    assert asked == [("HR-EXP-1", ["LN-1"], False, [])]  # a draft's save; its send reads under the lock


def test_the_b2c_guards_take_off_draft_entries_and_the_claims_unpaid_payees(env, monkeypatch):
    # owed.employee_doc_taken_off (B2C's submit and retry guards): what left_to_pay takes off that they
    # do not read themselves. A draft entry carrying a receipt a B2B request or a Paid B2C payout counts
    # already is skipped.
    asked = payees_held(env, monkeypatch, 1000)
    env.pe("ACC-PAY-9", None, [(EC, "HR-EXP-1", 500)], docstatus=0, party_type="Employee", party="EMP-1")
    env.pe("ACC-PAY-8", "QB2B8", [(EC, "HR-EXP-1", 700)], docstatus=0, party_type="Employee", party="EMP-1")
    parts = env.owed.employee_doc_taken_off(EC, "HR-EXP-1", [fake_frappe._dict(parent="MB2B-8", receipt="QB2B8")],
                                            ["LN-2"])
    assert [(p.kind, p.name, p.amount) for p in parts] == [("draft", "ACC-PAY-9", 500), ("payee", "LN-1", 1000)]
    assert asked[-1][:3] == ("HR-EXP-1", ["LN-2"], True)
    assert [(p.kind, p.name) for p in env.owed.employee_doc_taken_off(EC, "HR-EXP-1", [], ["LN-1"])] == [
        ("draft", "ACC-PAY-8"), ("draft", "ACC-PAY-9")]
    assert env.owed.employee_doc_taken_off("Purchase Invoice", "PINV-1") == []


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


def test_an_advance_under_its_own_workflow_takes_only_the_listed_states_its_workflow_has(env):
    # final review: the list is the Expense Claim's (its label says so). A site running workflows on both,
    # listing its claim states, refused every advance in its own final state.
    advance(env, workflow_state="Approved by HOD")
    doc = fake_frappe._dict(doctype=EA, name="HR-EAD-1")
    lock_on(env, "Approved\nPending Attachment")
    env.frappe.db.table("Workflow")["Advances"] = {"name": "Advances", "document_type": EA, "is_active": 1,
                                                   "workflow_state_field": "workflow_state"}
    for state in ("Draft", "Approved by HOD"):
        env.frappe.db.table("Workflow Document State")[f"WDS-{state}"] = {
            "name": f"WDS-{state}", "parent": "Advances", "parenttype": "Workflow", "state": state}
    assert env.ed.payable_problem(doc) is None  # none of the listed states is the advance workflow's
    env.settings.claim_payable_workflow_states = "Approved\nApproved by HOD"
    assert env.ed.payable_problem(doc) is None
    env.frappe.db.table(EA)["HR-EAD-1"]["workflow_state"] = "Draft"
    assert env.ed.payable_problem(doc) == ("HR-EAD-1 is in workflow state Draft, which M-Pesa Suite Settings does not "
                                           "list among the workflow states that may be paid.")
    assert env.ed.payable_states_of(EC) == ["Approved", "Approved by HOD"]  # a claim: the whole list


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


def test_a_b2b_to_an_employee_for_a_slip_or_no_claim_is_for_who_may_see_salaries(env, monkeypatch):
    # final review (decision D4): its save would show the slip's net pay and copy the employee's approved
    # bank account, paybill or till into a draft its maker reads
    sa = importlib.import_module("mpsa_suite.money_out.salary_access")
    seen = {"salaries": False}
    monkeypatch.setattr(sa, "can_see_salaries", lambda user=None: seen["salaries"])
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User"}
    doc = request(env, expense_claim=None, employee="EMP-1", employee_mpesa_account=None)
    with pytest.raises(env.frappe.PermissionError, match="salaries are shown only to who may read Salary Slips"):
        env.b2b.validate_request(doc)
    assert not doc.get("employee_mpesa_account") and not doc.get("receiver_shortcode")  # nothing read
    slip = request(env, expense_claim=None, employee="EMP-1", salary_slip="SAL-1", employee_mpesa_account=None)
    with pytest.raises(env.frappe.PermissionError, match="salaries are shown only"):
        env.b2b.validate_request(slip)
    env.b2b.validate_request(request(env))  # a claim's payout is not salary data
    seen["salaries"] = True
    env.denied.add(("Salary Slip", "read"))
    with pytest.raises(env.frappe.PermissionError):
        env.b2b.validate_request(request(env, expense_claim=None, employee="EMP-1", salary_slip="SAL-1"))
    env.b2b.validate_request(request(env, expense_claim=None, employee="EMP-1", amount=1500))
    seen["salaries"] = False
    sent = request(env, expense_claim=None, employee="EMP-1", amount=1500, docstatus=1)
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.b2b.validate_request(sent, at_submit=True)  # the send: the payer is not asked again
    scheduled = request(env, expense_claim=None, employee="EMP-1", amount=1500)
    scheduled.flags.mpesa_from_schedule = True
    env.frappe.user_roles = {"Accounts User"}
    env.b2b.validate_request(scheduled)  # Pay Selected checked its preparer


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


def test_only_a_named_manager_moves_a_paid_payout_and_never_a_payees_or_a_schedule_lines(env):
    # final review: the shared Administrator login (it holds M-Pesa Manager) could move a paid payout off
    # the claim it paid, freeing the claim to be paid again; a payee's payout would leave its line Paid
    claim(env, "HR-EXP-2")
    paid_request(env, "MB2B-P2", transaction_id="QEXP2", entry_mode="Make the entry later")
    env.frappe.session.user = "Administrator"
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        env.entries.make_entry("MB2B-P2", EC, expense_claim="HR-EXP-2")
    env.frappe.session.user, env.frappe.user_roles = "manager@example.com", {"M-Pesa Manager", "Accounts Manager"}
    paid_request(env, "MB2B-P3", transaction_id="QEXP3", entry_mode="Make the entry later", claim_payee="LN-1")
    with pytest.raises(env.frappe.ValidationError, match="paid a payee of HR-EXP-1 .Expense Claim payee LN-1.: its "
                                                         "entry is made on HR-EXP-1, never moved"):
        env.entries.make_entry("MB2B-P3", EC, expense_claim="HR-EXP-2")
    paid_request(env, "MB2B-P4", transaction_id="QEXP4", entry_mode="Make the entry later", schedule_line="MPSL-1",
                 payment_schedule="MPS-1")
    with pytest.raises(env.frappe.ValidationError, match="paid a line of M-Pesa Payment Schedule MPS-1 for HR-EXP-1"):
        env.entries.make_entry("MB2B-P4", EC, expense_claim="HR-EXP-2")
    env.entries.make_entry("MB2B-P2", EC, expense_claim="HR-EXP-2")  # a named manager moves a plain one
    assert env.frappe.db.table(B2B)["MB2B-P2"]["expense_claim"] == "HR-EXP-2"
    # money paid after its request was cancelled: booked by a named manager only
    paid_request(env, "MB2B-P5", transaction_id="QEXP5", docstatus=2, entry_mode="Make the entry later")
    env.frappe.session.user = "Administrator"
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        env.entries.make_entry("MB2B-P5", EC, expense_claim="HR-EXP-1")


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


def test_a_b2c_claim_payout_takes_only_what_the_claim_still_owes_and_alerts_the_rest(env, monkeypatch):
    # final review: paid in part another way since the send (an HRMS bank entry), the claim takes what it
    # still owes; the rest stays unallocated on the employee's entry and staff are told, once, as for B2B.
    # Before, ERPNext refused the entry on every sweep for 30 days, with no alert.
    made = []
    api = types.ModuleType("mpsa_suite.mpsa_suite.api.payment_entry")
    api.create_payment_entry = lambda *a, **kw: made.append((a, kw))
    monkeypatch.setitem(sys.modules, api.__name__, api)
    monkeypatch.delitem(sys.modules, "mpsa_suite.accounting.payment_entry_service", raising=False)
    service = importlib.import_module("mpsa_suite.accounting.payment_entry_service")
    pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
    alerts = []
    monkeypatch.setattr(pa, "alert", lambda message, doc, roles=None: alerts.append((doc.name, message)))
    env.frappe.db.table(EC)["HR-EXP-1"].update(total_amount_reimbursed=3000)  # 2,000 left of 5,000
    disbursement = fake_frappe._dict(paid_to=COMPANY_PAYABLE, company="Astral", mode_of_payment="Mpesa-X", name="D-1")
    ref = fake_frappe._dict(party_type="Employee", party="EMP-1", allocated_amount=5000, reference_doctype=EC,
                            reference_name="HR-EXP-1", reference_date=None, reference_no="QB2C1",
                            b2c_disbursement_request="B2C-9")
    service.create_payment_entry_for_doc(disbursement, ref)
    args, kw = made[-1]
    assert args[2] == 5000  # the whole payout is on the entry
    assert kw["references"] == [{"reference_doctype": EC, "reference_name": "HR-EXP-1", "allocated_amount": 2000,
                                 "b2c_payment_disbursement": "D-1"}]
    (alert,) = alerts
    assert alert[0] == "B2C-9"
    assert "HR-EXP-1 could take only KES 2,000 of this payment, so KES 3,000 is unallocated" in alert[1]
    assert "Check that the employee was not paid twice." in alert[1]
    env.frappe.db.table(EC)["HR-EXP-1"].update(total_amount_reimbursed=5000, status="Paid")  # nothing left
    service.create_payment_entry_for_doc(disbursement, ref)
    assert made[-1][1]["references"] == [] and len(alerts) == 2  # all of it unallocated, said so
    alerts.clear()
    env.frappe.db.table(EC)["HR-EXP-1"].update(total_amount_reimbursed=0, status="Unpaid")
    service.create_payment_entry_for_doc(disbursement, fake_frappe._dict(ref, allocated_amount=500))
    assert made[-1][1]["references"][0]["allocated_amount"] == 500 and alerts == []  # the usual case


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


@pytest.mark.parametrize("party_type, party", [("Supplier", "ACME"), ("Customer", "CUST-1"), (None, "EMP-1")])
def test_a_b2c_row_paying_a_claim_pays_its_employee_whatever_party_type_it_was_given(env, party_type, party):
    # booked to the claim's employee as their reimbursement (payment_entry_service), so paid to them:
    # a supplier row would pay an unapproved number and mark the claim paid
    claim(env, "HR-EXP-2", employee="EMP-1")
    with pytest.raises(env.frappe.ValidationError, match="Row #1: HR-EXP-2 belongs to EMP-1, not .*: an Expense Claim "
                                                         "is paid to its own employee. Pay a merchant or another "
                                                         "person as a payee of the claim"):
        row_of(env, party_type=party_type, party=party, partyb="0733000000").validate()
    advance(env, "HR-EAD-2", employee="EMP-1", advance_amount=3000)
    with pytest.raises(env.frappe.ValidationError, match="Row #1: HR-EAD-2 belongs to EMP-1, not Supplier ACME: an "
                                                         "Employee Advance is paid to its own employee.$"):
        row_of(env, party_type="Supplier", party="ACME", reference_doctype=EA, reference_name="HR-EAD-2").validate()
    row_of(env, party="EMP-1", partyb="0733000000").validate()  # its own employee (the lock is off)


def test_the_b2c_row_names_the_approved_phone_it_pays(env):
    claim(env, "HR-EXP-2", employee="EMP-2")
    row = row_of(env)
    row.validate()
    assert (row.partyb, row.payee_account_doctype, row.payee_account) == (PHONE, EMPACC, "MEA-2")
    row = row_of(env, partyb="0733000000")
    row.validate()  # the setting is off: a typed number is still paid, and names no account
    assert (row.partyb, row.payee_account) == ("254733000000", None)


def test_the_rows_approved_account_is_a_plain_name_set_again_on_every_save():
    import json
    from pathlib import Path

    path = (Path(__file__).resolve().parents[2] / "mpsa_suite" / "mpsa_suite" / "doctype"
            / "b2c_payment_disbursement_reference" / "b2c_payment_disbursement_reference.json")
    fields = {f["fieldname"]: f for f in json.loads(path.read_text(encoding="utf-8"))["fields"]}
    # not a link: Frappe refuses a link to a cancelled (submittable) account before any validate
    # runs, so a stale account would block the save, amend and Send via M-Pesa of the row
    assert fields["payee_account"]["fieldtype"] == "Data" and "options" not in fields["payee_account"]
    assert fields["payee_account"]["no_copy"] == 1 and fields["payee_account_doctype"]["no_copy"] == 1


def test_a_stale_approved_account_on_a_row_is_replaced_or_refused_plainly(env):
    claim(env, "HR-EXP-2", employee="EMP-2")
    row = row_of(env, partyb=PHONE, payee_account_doctype=EMPACC, payee_account="MEA-OLD")  # amended copy
    row.validate()
    assert row.payee_account == "MEA-2"
    lock_on(env)
    env.frappe.db.table(EMPACC)["MEA-2"]["docstatus"] = 2  # cancelled since: the number is not approved now
    row = row_of(env, partyb=PHONE, payee_account_doctype=EMPACC, payee_account="MEA-2")
    with pytest.raises(env.frappe.ValidationError, match=f"Row #1: {PHONE} is not a phone approved for EMP-2"):
        row.validate()
    assert row.payee_account is None


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
    req = fake_frappe._dict(reference_doctype=None, reference_name=None, party_type="Employee", party="EMP-2",
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


def test_a_b2c_request_made_by_hand_never_pays_a_salary_and_no_document_is_a_managers(env):
    # final review: nothing caps a hand-made request by what the slip owes, or counts it while on its way
    req = fake_frappe._dict(reference_doctype=SLIP, reference_name="SAL-2", party_type="Employee", party="EMP-2",
                            phone_number=PHONE, amount=30000, docstatus=0, flags=fake_frappe._dict())
    with pytest.raises(env.frappe.ValidationError, match="A Salary Slip is paid from an M-Pesa Payment Schedule line"):
        env.ed.check_b2c_request(req)
    req.flags.mpesa_from_schedule = True  # the schedule's own payout of the slip's line
    env.ed.check_b2c_request(req)
    # an employee paid for no document: sent by an M-Pesa Manager only, as a B2B request with none
    req = fake_frappe._dict(reference_doctype=None, reference_name=None, party_type="Employee", party="EMP-2",
                            phone_number=PHONE, amount=1500, docstatus=0, flags=fake_frappe._dict())
    env.frappe.session.user, env.frappe.user_roles = "payer@example.com", {"M-Pesa Payer"}
    env.ed.check_b2c_request(req)  # a draft
    req.docstatus = 1
    with pytest.raises(env.frappe.PermissionError, match="paid by an M-Pesa Manager"):
        env.ed.check_b2c_request(req)
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.ed.check_b2c_request(req)


@pytest.mark.parametrize("dt, dn, employee", [(EC, "HR-EXP-2", "EMP-2"), (EC, "HR-EXP-2", "EMP-1"),
                                              (EA, "HR-EAD-1", "EMP-1")])
def test_a_b2c_request_made_by_hand_never_pays_a_claim_or_advance(env, dt, dn, employee):
    # nothing would count it against the claim while on its way, nor cap it: another payout of the
    # claim could pass, or it could pay more than the claim owes, or pay another employee
    claim(env, "HR-EXP-2", employee="EMP-1")
    advance(env)
    req = fake_frappe._dict(reference_doctype=dt, reference_name=dn, party_type="Employee", party=employee,
                            phone_number=PHONE, amount=100000, docstatus=0, flags=fake_frappe._dict())
    with pytest.raises(env.frappe.ValidationError, match=f"An {dt} is paid by a B2C Payment Disbursement \\(Pay "
                                                         "employee \\(phone\\)"):
        env.ed.check_b2c_request(req)
    req.flags.mpesa_from_disbursement = True  # a disbursement row's payout: its row was checked
    env.ed.check_b2c_request(req)


def test_nobody_sends_a_b2c_payout_of_their_own_claim(env):
    claim(env, "HR-EXP-2", employee="EMP-2")
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = PAYER
    doc = fake_frappe._dict(flags=fake_frappe._dict(), references=[row_of(env)])
    with pytest.raises(env.frappe.PermissionError, match="Row #1: You are EMP-2"):
        env.ed.check_not_own_b2c(doc)
    doc.flags.mpesa_from_schedule = True  # Pay Selected checked it
    env.ed.check_not_own_b2c(doc)


def failed_b2c(env, monkeypatch, claim_payee=None, **kw):
    """A failed B2C payout of HR-EXP-2 to EMP-2's approved phone, from row R-R1; what it sends lands in env.resent."""
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    env.setdefault("resent", [])
    monkeypatch.setattr(module, "make_b2c_payment_request", lambda name: env.resent.append(name))
    values = {"name": "B2C-R1", "docstatus": 1, "status": "Failed", "amount": 3000, "reference_doctype": EC,
              "reference_name": "HR-EXP-2", "party_type": "Employee", "party": "EMP-2", "phone_number": PHONE,
              "b2c_payment_disbursement": "MPESA-B2C-R", "b2c_payment_disbursement_reference": "R-R1", **kw}
    env.frappe.db.table(REQ)["B2C-R1"] = dict(values)
    env.frappe.db.table(ROW)["R-R1"] = {"name": "R-R1", "parent": "MPESA-B2C-R", "parenttype": DISB,
                                        "mpesa_schedule_line": None, "claim_payee": claim_payee}
    request = object.__new__(module.B2CDisbursementRequest)
    request.__dict__.update(values, flags=fake_frappe._dict())
    request.reload = lambda: None
    request.db_set = lambda changes, *a, **k: request.__dict__.update(changes)
    return request


def test_a_retry_checks_the_claim_the_approved_phone_and_the_payer_again(env, monkeypatch):
    lock_on(env)
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000)
    # the claimant is an M-Pesa Payer and presses Retry on the payout of their own claim
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = PAYER
    with pytest.raises(env.frappe.PermissionError, match="You are EMP-2"):
        failed_b2c(env, monkeypatch).retry_failed_payment()
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = None
    # a manager cancelled the phone account since the failure (the number was given to someone else)
    env.frappe.db.table(EMPACC)["MEA-2"]["docstatus"] = 2
    with pytest.raises(env.frappe.ValidationError, match=f"{PHONE} is not a phone approved for EMP-2"):
        failed_b2c(env, monkeypatch).retry_failed_payment()
    env.frappe.db.table(EMPACC)["MEA-2"]["docstatus"] = 1
    # the claim is no longer approved
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000,
          approval_status="Rejected")
    with pytest.raises(env.frappe.ValidationError, match="HR-EXP-2 is not approved"):
        failed_b2c(env, monkeypatch).retry_failed_payment()
    assert env.resent == [] and env.frappe.db.table(REQ)["B2C-R1"]["status"] == "Failed"
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000)
    request = failed_b2c(env, monkeypatch)
    request.retry_failed_payment()
    assert env.resent == ["B2C-R1"] and request.status == "Not Initiated"
    assert "b2c-pay:Expense Claim:HR-EXP-2" in env.locks


def test_a_retry_of_a_claim_payee_leaves_its_phone_to_claim_payees_and_any_payout_keeps_the_rules(env, monkeypatch):
    lock_on(env)
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000)
    with pytest.raises(env.frappe.ValidationError, match="is not a phone approved for EMP-2"):
        failed_b2c(env, monkeypatch, phone_number="254733000000").retry_failed_payment()
    failed_b2c(env, monkeypatch, claim_payee="CP-1", phone_number="254733000000").retry_failed_payment()
    assert env.resent == ["B2C-R1"]
    # a salary payout by hand is held to the phone lock and self-pay too
    env.frappe.db.table("Employee")["EMP-2"]["user_id"] = PAYER
    with pytest.raises(env.frappe.PermissionError, match="You are EMP-2"):
        failed_b2c(env, monkeypatch, reference_doctype=SLIP, reference_name="SAL-2",
                   amount=30000).retry_failed_payment()


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


def supplier_phone(env, name="MSA-P1", phone="254711222333", **kw):
    """An approved Phone (B2C) Supplier M-Pesa Account of ACME."""
    env.frappe.db.table("Supplier M-Pesa Account")[name] = {
        "name": name, "docstatus": 1, "supplier": "ACME", "account_type": PHONE_TYPE, "phone": phone,
        "shortcode": None, "account_number": None, "expected_name": "ACME", "active_from": NOW - datetime.timedelta(days=1),
        "is_default": 1, "name_mismatch": 0, "needs_reapproval": 0, "creation": NOW, **kw}
    return name


PHONE_TYPE = "Phone (B2C)"


def test_a_supplier_row_pays_its_approved_phone_once_it_has_one(env):
    # final review: a supplier's approved Phone (B2C) account (second approver, proof, hold) was never used:
    # the row took the Contact's number, which anyone could edit. With an approved phone it pays only that.
    row = row_of(env, party_type="Supplier", party="ACME", reference_doctype="Purchase Invoice",
                 reference_name="PINV-1", partyb="0733000999")
    row.validate()  # no approved phone account: the Contact's phone, as before
    assert row.payee_account is None
    supplier_phone(env)
    with pytest.raises(env.frappe.ValidationError, match="Row #1: 254733000999 is not a phone approved for ACME. "
                                                         "ACME has approved M-Pesa phone accounts"):
        row.validate()
    row.partyb = "0711222333"
    row.validate()
    assert (row.partyb, row.payee_account_doctype, row.payee_account) == (
        "254711222333", "Supplier M-Pesa Account", "MSA-P1")
    env.frappe.db.table("Supplier M-Pesa Account")["MSA-P1"]["needs_reapproval"] = 1  # e.g. a name mismatch
    with pytest.raises(env.frappe.ValidationError, match="Row #1: .*MSA-P1"):
        row.validate()


def test_get_references_takes_the_suppliers_approved_phone(env):
    identify = env.disb.B2CPaymentDisbursement.get_party_identifier
    env.frappe.db.table("Contact")["C-1"] = {"name": "C-1", "link_name": "ACME", "phone": "0733000999",
                                             "mobile_no": None}
    entry = fake_frappe._dict(party="ACME")
    assert identify(None, entry, "Supplier", "Mpesa Disbursement") == {"account_number": "0733000999"}
    supplier_phone(env)
    assert identify(None, entry, "Supplier", "Mpesa Disbursement") == {"account_number": "254711222333"}
    env.frappe.db.table("Supplier M-Pesa Account")["MSA-P1"]["active_from"] = NOW + datetime.timedelta(days=1)
    assert identify(None, entry, "Supplier", "Mpesa Disbursement") == {"account_number": ""}  # on hold: none


def test_the_name_check_runs_for_a_suppliers_approved_phone(env, monkeypatch):
    service = importlib.import_module("mpsa_suite.services.b2c_response_service")
    pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
    checked = []
    monkeypatch.setattr(pa, "record_receiver_name",
                        lambda doctype, account, name, payout, notify=None: checked.append((doctype, account, name)))
    env.frappe.db.table(ROW)["R1"] = {"name": "R1", "payment_status": "Initiated", "payee_account": "MSA-P1",
                                      "payee_account_doctype": "Supplier M-Pesa Account",
                                      "mpesa_schedule_line": None, "b2c_disbursement_request": "B2C-1"}
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Paid", "transaction_id": "QB2C9",
                                         "transaction_completed_datetime": NOW, "b2c_payment_disbursement_reference": "R1",
                                         "receiver_public_name": "254711***333 - ACME LTD"}
    service.update_b2c_reference_status("B2C-1")
    assert checked == [("Supplier M-Pesa Account", "MSA-P1", "254711***333 - ACME LTD")]


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


def test_a_draft_b2b_request_does_not_hold_the_cancel_but_a_submitted_one_does(env):
    doc = fake_frappe._dict(doctype=EC, name="HR-EXP-1")
    paid_request(env, "MB2B-D", docstatus=0, status="Not Initiated")
    env.ed.before_cancel(doc)  # a draft is never sent once the claim is cancelled (payable_problem)
    paid_request(env, "MB2B-S", docstatus=1, status="Not Initiated")  # submitted, its send not made yet
    with pytest.raises(env.frappe.ValidationError, match="on their way: MB2B-S"):
        env.ed.before_cancel(doc)


@pytest.mark.parametrize("doctype", [EC, EA])
def test_failed_payouts_do_not_hold_the_cancel_once_nothing_is_paid_or_on_its_way(env, doctype):
    # HRMS's on_cancel sets the ledger DocTypes it ignores; the payouts are added after it (Frappe's back-link
    # check runs after on_cancel and would refuse the cancel for a failed submitted payout naming the document)
    hrms = ("GL Entry", "Stock Ledger Entry", "Payment Ledger Entry")
    doc = fake_frappe._dict(doctype=doctype, name="DOC-1", ignore_linked_doctypes=hrms, mpesa_payees=[])
    if doctype == EC:
        importlib.import_module("mpsa_suite.money_out.claim_payees").on_cancel_claim(doc)
    else:
        env.ed.on_cancel(doc)
    assert doc.ignore_linked_doctypes == (*hrms, B2B, DISB, ROW, REQ)
    env.ed.on_cancel(doc)  # twice: nothing added twice
    assert doc.ignore_linked_doctypes == (*hrms, B2B, DISB, ROW, REQ)
    blank = fake_frappe._dict(doctype=doctype, name="DOC-2")
    env.ed.ignore_payout_links(blank)
    assert blank.ignore_linked_doctypes == (B2B, DISB, ROW, REQ)


def test_the_advance_cancel_hook_is_wired_after_hrms():
    import ast
    from pathlib import Path

    hooks = ast.literal_eval(ast.unparse(next(
        node.value for node in ast.parse((Path(__file__).resolve().parents[2] / "mpsa_suite" / "hooks.py")
                                         .read_text(encoding="utf-8")).body
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "doc_events")))
    assert hooks["Employee Advance"]["on_cancel"] == "mpsa_suite.money_out.employee_documents.on_cancel"
    assert hooks["Expense Claim"]["on_cancel"] == "mpsa_suite.money_out.claim_payees.on_cancel_claim"


# ---------------------------------------------------------------- not salaries
def readers(env, monkeypatch, perms):
    """frappe.permissions.get_valid_perms: ``perms`` {doctype: roles given read (level 0)}."""
    permissions = types.ModuleType("frappe.permissions")
    permissions.get_valid_perms = lambda doctype, user: [
        {"role": role, "read": 1, "permlevel": 0, "if_owner": 0}
        for role in perms.get(doctype, ()) if role in env.frappe.user_roles]
    monkeypatch.setitem(sys.modules, "frappe.permissions", permissions)


# HRMS's own: HR Manager, HR User and Expense Approver read claims; advances only Expense Approver
# (and every Employee their own, which does not count)
HRMS_READERS = {EC: ("HR Manager", "HR User", "Expense Approver", "Employee"), EA: ("Expense Approver", "Employee")}


def claim_payouts(env, monkeypatch):
    columns = b2b_fixture.COLUMNS
    monkeypatch.setitem(columns, REQ, columns[REQ] + ("party_type", "party", "owner"))
    env.request("MB2B-1")  # a supplier payment
    env.request("MB2B-SAL", supplier=None, employee="EMP-1", salary_slip="SAL-1")
    env.request("MB2B-EXP", supplier=None, employee="EMP-1", expense_claim="HR-EXP-1")
    env.request("MB2B-ADV", supplier=None, employee="EMP-1", employee_advance="HR-EAD-1")
    env.request("MB2B-NODOC", supplier=None, employee="EMP-1")
    db = env.frappe.db
    db.table(DISB)["D-EXP"] = {"name": "D-EXP", "docstatus": 1, "party_type": "Employee",
                               "transaction_to_pay_against": EC}
    db.table(DISB)["D-SAL"] = {"name": "D-SAL", "docstatus": 1, "party_type": "Employee",
                               "transaction_to_pay_against": SLIP}
    db.table(ROW)["R-EXP"] = {"name": "R-EXP", "parent": "D-EXP", "parenttype": DISB, "party_type": "Employee",
                              "reference_doctype": EC}
    db.table(ROW)["R-SAL"] = {"name": "R-SAL", "parent": "D-SAL", "parenttype": DISB, "party_type": "Employee",
                              "reference_doctype": SLIP}
    for name, dt in (("B2C-EXP", EC), ("B2C-SAL", SLIP)):
        db.table(REQ)[name] = {"name": name, "docstatus": 1, "status": "Paid", "party_type": "Employee",
                               "party": "EMP-1", "reference_doctype": dt, "reference_name": "X"}


def shown(env, salary, doctype):
    condition = salary.query_conditions("other@example.com", doctype)
    return [r[0] for r in env.frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE {condition} ORDER BY name")]


def test_claim_and_advance_payouts_are_shown_to_accounts_roles_who_read_them(env, monkeypatch):
    salary = importlib.import_module("mpsa_suite.money_out.salary_access")
    claim_payouts(env, monkeypatch)
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"Accounts User", "Expense Approver"}
    readers(env, monkeypatch, HRMS_READERS)
    assert salary.reads_documents() == (EC, EA) and not salary.can_see_salaries()
    assert shown(env, salary, B2B) == ["MB2B-1", "MB2B-ADV", "MB2B-EXP"]
    assert shown(env, salary, DISB) == ["D-EXP"]
    assert shown(env, salary, REQ) == ["B2C-EXP"]
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, employee="EMP-1", expense_claim="HR-EXP-1")) is True
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, employee="EMP-1", salary_slip="SAL-1")) is False
    assert salary.is_salary_line({"party_type": "Employee", "reference_doctype": EA}) is False
    assert salary.is_salary_line({"party_type": "Employee", "reference_doctype": None}) is True
    assert salary.has_salaries(fake_frappe._dict(doctype=DISB, party_type="Employee", transaction_to_pay_against=EC,
                                                 references=[{"party_type": "Employee", "reference_doctype": EC}])) is False
    # the site gave Accounts User read on claims only: advances stay hidden like salaries
    env.frappe.user_roles = {"Accounts User"}
    readers(env, monkeypatch, {**HRMS_READERS, EC: (*HRMS_READERS[EC], "Accounts User")})
    assert salary.reads_documents() == (EC,)
    assert shown(env, salary, B2B) == ["MB2B-1", "MB2B-EXP"]
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, employee="EMP-1", employee_advance="HR-EAD-1")) is False


@pytest.mark.parametrize("roles", [{"Accounts User"}, {"M-Pesa Viewer"}, {"Accounts Manager", "Employee"}])
def test_claim_payouts_stay_hidden_from_who_cannot_read_claims(env, monkeypatch, roles):
    # HRMS gives Accounts roles and M-Pesa Viewer no read on claims; an Employee reads only their own
    salary = importlib.import_module("mpsa_suite.money_out.salary_access")
    claim_payouts(env, monkeypatch)
    env.frappe.session.user, env.frappe.user_roles = PREPARER, roles
    readers(env, monkeypatch, HRMS_READERS)
    assert salary.reads_documents() == ()
    assert shown(env, salary, B2B) == ["MB2B-1"]
    assert shown(env, salary, DISB) == []
    assert shown(env, salary, REQ) == []
    for doc in (fake_frappe._dict(doctype=B2B, employee="EMP-1", expense_claim="HR-EXP-1", owner="hr@example.com"),
                fake_frappe._dict(doctype=REQ, party_type="Employee", reference_doctype=EC, owner="hr@example.com"),
                fake_frappe._dict(doctype=DISB, party_type="Employee", transaction_to_pay_against=EA,
                                  references=[{"party_type": "Employee", "reference_doctype": EA}],
                                  owner="hr@example.com")):
        assert salary.has_permission(doc) is False
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, supplier="ACME", owner="hr@example.com")) is True
    # its creator still sees it
    assert salary.has_permission(fake_frappe._dict(doctype=B2B, employee="EMP-1", expense_claim="HR-EXP-1",
                                                   owner=PREPARER)) is True
    hidden = salary.report_condition("l.")
    assert "COALESCE(l.reference_doctype, '') IN ('Expense Claim', 'Employee Advance')" in hidden


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
