"""Round 4 residual fixes, after the review of the verification fixes, on the in-memory sites of
test_employee_documents, test_r4_verify_reads and test_claim_payees.

Covers: a B2C request made by hand pays exactly what its disbursement row says (row_mismatch), so
one that leaves its party out or names another party does not skip the row's checks, at its save
and on a retry; a number typed in that is an approved phone on hold is refused even while the
approved-phone lock is off; an amended B2B request keeps its cancelled request's invoice without
read on it; an M-Pesa Manager does not settle the payees of their own claim.
"""

import datetime
import importlib

import fake_frappe
import pytest

import b2b_fixture
from test_claim_payees import CLAIM, claim, payee
from test_claim_payees import env as claims_env  # noqa: F401 (the claim payees site)
from test_employee_documents import env, supplier_phone  # noqa: F401 (the claims and advances site)
from test_payroll_schedule import EMPACC, PHONE, ROW
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture test_employee_documents extends)
from test_r4_verify_reads import b2b_draft, b2b_env, on_hold, reads  # noqa: F401

NOW = b2b_fixture.NOW


# ---------------------------------------------------------------- a request made by hand pays its row
def hand_made(env, **kw):
    """A B2C request made by hand for disbursement row R-S1 (ACME, PINV-1, KES 1,500)."""
    env.frappe.db.table(ROW)["R-S1"] = {
        "name": "R-S1", "parent": "MPESA-B2C-S", "party_type": "Supplier", "party": "ACME", "partyb": "0711222333",
        "allocated_amount": 1500, "reference_doctype": "Purchase Invoice", "reference_name": "PINV-1",
        "mpesa_schedule_line": None, "claim_payee": None, "payee_account_doctype": None, "payee_account": None}
    return fake_frappe._dict({
        "name": "B2C-S1", "reference_doctype": "Purchase Invoice", "reference_name": "PINV-1", "party_type": "Supplier",
        "party": "ACME", "phone_number": "254711222333", "amount": 1500, "docstatus": 0, "flags": fake_frappe._dict(),
        "b2c_payment_disbursement": "MPESA-B2C-S", "b2c_payment_disbursement_reference": "R-S1", **kw})


def test_a_b2c_request_made_by_hand_pays_exactly_its_disbursement_row(env, monkeypatch):
    monkeypatch.setattr(env.ed, "row_mismatch", env.row_mismatch)  # the fixture stubs it for the other tests
    env.ed.check_b2c_request(hand_made(env))  # as its row says (the number in another format)
    with pytest.raises(env.frappe.ValidationError, match="request's phone number is not that of MPESA-B2C-S row R-S1"):
        env.ed.check_b2c_request(hand_made(env, phone_number="254733999888"))
    supplier_phone(env)  # ACME has an approved phone since: the row's supplier rule may not be skipped
    for change, what in ((dict(party_type=None, party=None), "party type, party"), (dict(party="OTHER"), "party"),
                         (dict(amount=1499), "amount"), (dict(reference_name="PINV-2"), "document")):
        with pytest.raises(env.frappe.ValidationError, match=f"request's {what} is not that of MPESA-B2C-S row R-S1"):
            env.ed.check_b2c_request(hand_made(env, **change))
    with pytest.raises(env.frappe.ValidationError, match="R-S1 is not a row of MPESA-B2C-T"):
        env.ed.check_b2c_request(hand_made(env, b2c_payment_disbursement="MPESA-B2C-T"))
    with pytest.raises(env.frappe.ValidationError, match="No row is not a row of MPESA-B2C-S"):
        env.ed.check_b2c_request(hand_made(env, b2c_payment_disbursement_reference=None))
    env.ed.check_b2c_request(hand_made(env, party=None, flags=fake_frappe._dict(mpesa_from_disbursement=True)))


def test_a_retry_of_a_request_made_by_hand_pays_exactly_its_row(env, monkeypatch):
    # a request made by hand before row_mismatch was checked at its save, with its party left out
    monkeypatch.setattr(env.ed, "row_mismatch", env.row_mismatch)
    supplier_phone(env)
    env.ed.check_b2c_retry(hand_made(env, docstatus=1))
    with pytest.raises(env.frappe.ValidationError, match="request's party type, party, phone number is not that of MPESA-B2C-S"):
        env.ed.check_b2c_retry(hand_made(env, docstatus=1, party_type=None, party=None, phone_number="254733999888"))


# ---------------------------------------------------------------- a typed number on hold, lock off
def test_a_typed_number_of_an_approved_phone_on_hold_is_refused_with_the_lock_off(env):
    assert not env.settings.get("b2c_approved_phones_only")  # as on Trident and Astral after the upgrade
    on_hold(env)  # MEA-2, EMP-2's default phone (PHONE), approved a moment ago
    assert "on hold" in env.ed.phone_lock_problem("EMP-2", PHONE)
    assert env.ed.phone_lock_problem("EMP-2", "254733000999") is None  # no approved account's: not checked, as before
    env.frappe.db.table(EMPACC)["MEA-2"]["active_from"] = NOW - datetime.timedelta(days=2)
    assert env.ed.phone_lock_problem("EMP-2", PHONE) is None


# ---------------------------------------------------------------- an amended B2B keeps its invoice
def test_an_amended_b2b_request_keeps_its_invoice_without_read_on_it(b2b_env, monkeypatch):
    env = b2b_env
    env.pi()
    env.pi("PINV-2")
    monkeypatch.setitem(b2b_fixture.COLUMNS, b2b_fixture.B2B, b2b_fixture.COLUMNS[b2b_fixture.B2B] + ("amended_from",))
    env.request("MB2B-OLD", docstatus=2, status="Failed", purchase_invoice="PINV-1", amount=3000)
    reads(env.frappe, monkeypatch)  # a payer resending a failed request: no read on Purchase Invoices
    doc = b2b_draft(env, amended_from="MB2B-OLD")
    env.b2b.validate_request(doc)
    assert doc.supplier == "ACME"
    with pytest.raises(env.frappe.PermissionError, match="No read on Purchase Invoice PINV-2"):
        env.b2b.validate_request(b2b_draft(env, amended_from="MB2B-OLD", purchase_invoice="PINV-2"))


# ---------------------------------------------------------------- nobody settles their own claim's payees
def test_a_manager_does_not_settle_the_payees_of_their_own_claim(claims_env, monkeypatch):  # noqa: F811
    env = claims_env
    claim(env, [payee("LN-1", 1)])
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.frappe.session.user = "manager@example.com"
    accounts = importlib.import_module("mpsa_suite.money_out.employee_accounts")
    monkeypatch.setattr(accounts, "own_payment_problem",
                        lambda employee, user=None: f"You are {employee}: another person must pay you."
                        if employee == "EMP-1" else None)
    with pytest.raises(env.frappe.PermissionError, match="HR-EXP-1 is your own claim: another M-Pesa Manager settles"):
        env.cp.settle_line("HR-EXP-1", "LN-1", "Bank ACC-PAY-1")
    assert env.frappe.db.table("M-Pesa Claim Payee")["LN-1"]["status"] != "Cancelled"
    assert not [c for c in env.frappe.comments if c[:2] == (CLAIM, "HR-EXP-1")]
