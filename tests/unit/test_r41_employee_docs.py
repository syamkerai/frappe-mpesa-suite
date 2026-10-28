"""Round 4.1 on the in-memory site of test_employee_documents: a claim whose reimbursed total
disagrees with its entries is paid no more until it is set right, the job that sets it right after
each payment (under the claim's lock, taken first), and a draft batch of the claim's casual payees
that no longer stops the employee's own refund."""

import importlib

import fake_frappe
import pytest
from test_claim_payees import hrms_modules, hrms_stub  # noqa: F401 (hrms_modules: a fixture)
from test_employee_documents import DISB, EC, PREPARER, ROW, claim, env  # noqa: F401 (env: the fixture)
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture env extends)

_dict = fake_frappe._dict
GAP = ("HR-EXP-1's Total Amount Reimbursed shows KES 0, but the entries booked against it add up to KES 2,000: an "
       "M-Pesa Manager repairs it")


def test_a_claim_whose_reimbursed_total_disagrees_with_its_entries_is_not_paid(env, hrms_modules):
    hrms_stub(env, booked=2000)
    values = env.ed.read_document(EC, "HR-EXP-1")
    assert env.ed.payable_problem(values, EC).startswith(GAP)
    assert env.ed.payable_problem(values, EC, skip_gap=True) is None  # the claim form says it in its own words
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    with pytest.raises(env.frappe.ValidationError, match="Total Amount Reimbursed shows KES 0"):
        env.ed.prepare_b2c(EC, "HR-EXP-1")
    hrms_stub(env, booked=0)
    assert env.ed.payable_problem(env.ed.read_document(EC, "HR-EXP-1"), EC) is None


def test_a_draft_batch_of_the_claims_payees_does_not_stop_the_employees_refund(env):
    env.frappe.db.columns = env.frappe.db.columns | {(ROW, "claim_payee")}
    env.frappe.db.table(DISB)["MPESA-B2C-P"] = {"name": "MPESA-B2C-P", "docstatus": 0, "company": "Astral"}
    env.frappe.db.table(ROW)["MPESA-B2C-P-R1"] = {
        "name": "MPESA-B2C-P-R1", "parent": "MPESA-B2C-P", "parenttype": DISB, "parentfield": "references", "idx": 1,
        "reference_doctype": EC, "reference_name": "HR-EXP-1", "claim_payee": "LN-1", "allocated_amount": 500}
    assert env.ed.draft_b2c(EC, "HR-EXP-1") == ["MPESA-B2C-P"]  # the form still lists it as waiting
    assert env.ed.draft_b2c(EC, "HR-EXP-1", payees=False) == []
    env.frappe.db.table(ROW)["MPESA-B2C-P-R1"]["claim_payee"] = None  # a row to the employee
    assert env.ed.draft_b2c(EC, "HR-EXP-1", payees=False) == ["MPESA-B2C-P"]


def test_each_payment_of_a_claim_or_advance_queues_the_paid_total_check(env):
    pe = _dict(doctype="Payment Entry", name="PE-1", references=[
        _dict(reference_doctype=EC, reference_name="HR-EXP-1"), _dict(reference_doctype=EC, reference_name="HR-EXP-1"),
        _dict(reference_doctype="Employee Advance", reference_name="HR-EAD-1"),
        _dict(reference_doctype="Purchase Invoice", reference_name="PINV-1")])
    env.frappe.enqueued.clear()
    env.ed.check_paid_total_later(pe)
    assert [(m.rsplit(".", 1)[1], kw["doctype"], kw["name"], kw["enqueue_after_commit"])
            for m, kw in env.frappe.enqueued] == [("check_paid_total", "Employee Advance", "HR-EAD-1", True),
                                                   ("check_paid_total", EC, "HR-EXP-1", True)]
    je = _dict(doctype="Journal Entry", name="JV-1", accounts=[_dict(reference_type=EC, reference_name="HR-EXP-2")])
    env.frappe.enqueued.clear()
    env.ed.check_paid_total_later(je)
    assert env.frappe.enqueued[0][1]["name"] == "HR-EXP-2"


def test_the_check_sets_a_short_reimbursed_total_right_under_the_claims_lock(env, hrms_modules):
    claim(env, total_amount_reimbursed=70)  # two payouts of 20 and 70 booked 2 s apart (HR-EXP-2026-00712)
    hrms_stub(env, booked=90)
    env.log.clear()
    env.ed.check_paid_total(EC, "HR-EXP-1")
    locks = [e for e in env.log if e[0] == "lock"]
    assert locks and locks[0] == ("lock", "b2c-pay:Expense Claim:HR-EXP-1")
    assert env.frappe.db.table(EC)["HR-EXP-1"]["total_amount_reimbursed"] == 90
    assert env.frappe.comments[-1][2] == ("Total Amount Reimbursed set from KES 70 to KES 90, the entries booked "
                                          "against it: two payments were booked at the same moment.")
    comments = len(env.frappe.comments)
    env.ed.check_paid_total(EC, "HR-EXP-1")  # idempotent
    assert len(env.frappe.comments) == comments


def test_the_check_recounts_an_advances_paid_amount(env, monkeypatch):
    env.frappe.db.table("Employee Advance")["HR-EAD-1"] = {"name": "HR-EAD-1", "docstatus": 1, "paid_amount": 3000}
    doc = fake_frappe.FakeDoc(env.frappe, "Employee Advance", env.frappe.db.table("Employee Advance")["HR-EAD-1"])
    object.__setattr__(doc, "set_total_advance_paid",
                       lambda: env.frappe.db.table("Employee Advance")["HR-EAD-1"].update(paid_amount=5000))
    monkeypatch.setattr(env.frappe, "get_doc", lambda doctype, name=None, **kw: doc)
    env.ed.check_paid_total("Employee Advance", "HR-EAD-1")
    assert "Paid Amount set from KES 3,000 to KES 5,000" in env.frappe.comments[-1][2]


def test_hooks_check_the_paid_total_after_each_payment_entry_and_journal_entry():
    import ast
    from pathlib import Path

    hooks = Path(__file__).resolve().parents[2] / "mpsa_suite" / "hooks.py"
    tree = ast.parse(hooks.read_text(encoding="utf-8"))
    events = next(ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "doc_events")
    check = "mpsa_suite.money_out.employee_documents.check_paid_total_later"
    for doctype in ("Payment Entry", "Journal Entry"):
        assert events[doctype]["on_submit"] == check
        assert check in events[doctype]["on_cancel"]
    importlib.import_module("mpsa_suite.permission_matrix")  # pure; still imports
