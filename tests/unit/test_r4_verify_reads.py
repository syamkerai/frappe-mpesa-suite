"""Round 4 verification, reads group (K3, K5, K9, K12, K15), on the in-memory sites of
test_employee_documents, test_fix_payouts and b2b_fixture.

Covers: an employee with approved phones is paid by B2C only to the one picked now, never their
Cell Number while the default is on hold or blocked (the reason is shown); a B2C disbursement
asks who may see salaries before a slip's outstanding is read; a B2B draft names an invoice,
order, claim or advance only for who may read it, when the link is new or changed (an Accounts
role reads orders by its Purchase Invoice read); Get References lists only the documents the user
may read, of a DocType they read; a supplier with approved phones is paid by a B2C request made by
hand, and by a retry, only to one of them that can be paid now.
"""

import datetime
import importlib
import sys
import types

import pytest

import b2b_fixture
import fake_frappe
from test_employee_documents import EC, EA, PREPARER, claim, lock_on, request, supplier_phone
from test_employee_documents import env  # noqa: F401 (the claims and advances site)
from test_fix_payouts import env as payouts_env  # noqa: F401 (the B2C disbursement site)
from test_payroll_schedule import EMPACC, PHONE, ROW, SLIP
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture test_employee_documents extends)

NOW = b2b_fixture.NOW
SUPPLIER_ACCOUNT = "Supplier M-Pesa Account"


def reads(frappe, monkeypatch, doctypes=(), docs=(), denied=()):
    """frappe.has_permission(..., "read"): the DocTypes in ``doctypes`` and the documents in ``docs``
    (``denied`` refused, whatever the DocType); every other right is given."""
    asked = []

    def has_permission(doctype, ptype="read", doc=None, throw=False, **kw):
        asked.append((doctype, ptype, doc))
        if ptype != "read":
            allowed = True
        elif doc is None:
            allowed = doctype in doctypes
        else:
            allowed = doc in docs and doc not in denied
        if throw and not allowed:
            raise frappe.PermissionError(f"No {ptype} on {doctype} {doc or ''}".strip())
        return allowed

    monkeypatch.setattr(frappe, "has_permission", has_permission, raising=False)
    return asked


# ---------------------------------------------------------------- K3: an approved phone that cannot be paid now
def on_hold(env, account="MEA-2"):
    env.frappe.db.table(EMPACC)[account]["active_from"] = NOW + datetime.timedelta(hours=5)


def other_phone(env, name="MEA-7", phone="254722000777", **kw):
    """A second approved phone of EMP-2, not the default, that can be paid now."""
    env.frappe.db.table(EMPACC)[name] = dict(env.frappe.db.table(EMPACC)["MEA-2"], name=name, phone=phone,
                                             is_default=0, active_from=NOW - datetime.timedelta(days=2), **kw)


def test_an_on_hold_default_phone_is_never_passed_over_for_the_cell_number(env):
    # the approved-phone lock is off (as on Trident and Astral after the upgrade): the default phone is on
    # hold, and the Cell Number is the new number nobody approved yet
    env.frappe.db.table("Employee")["EMP-2"]["cell_number"] = "0733000222"
    on_hold(env)
    assert env.ed.b2c_phone("EMP-2") == ("", None)
    other_phone(env)  # another approved phone is not picked silently either
    assert env.ed.b2c_phone("EMP-2") == ("", None)
    env.frappe.db.table(EMPACC)["MEA-2"]["active_from"] = NOW - datetime.timedelta(days=2)
    assert env.ed.b2c_phone("EMP-2") == (PHONE, "MEA-2")
    # no approved phone at all: the Cell Number while the lock is off, nothing once it is on
    assert env.ed.b2c_phone("EMP-3") == ("0711000333", None)
    lock_on(env)
    assert env.ed.b2c_phone("EMP-3") == ("", None)


def test_pay_employee_by_phone_says_why_the_approved_phone_cannot_be_paid(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    env.frappe.db.table("Employee")["EMP-2"]["cell_number"] = "0733000222"
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000)
    on_hold(env)
    with pytest.raises(env.frappe.ValidationError, match="MEA-2 is on hold until .* It is the employee's default: "
                                                         "pick another approved account"):
        env.ed.prepare_b2c(EC, "HR-EXP-2")
    other_phone(env)
    env.frappe.db.table(EMPACC)["MEA-2"].update(is_default=0, active_from=NOW - datetime.timedelta(days=2))
    with pytest.raises(env.frappe.ValidationError, match="EMP-2 has several approved phones and none is the default"):
        env.ed.prepare_b2c(EC, "HR-EXP-2")
    env.frappe.db.table(EMPACC)["MEA-7"]["is_default"] = 1
    out = env.ed.prepare_b2c(EC, "HR-EXP-2")
    (row,) = [r for r in env.frappe.db.table(ROW).values() if r["parent"] == out["name"]]
    assert (row["partyb"], row["payee_account"]) == ("254722000777", "MEA-7")


def test_pay_by_mpesa_on_the_list_lists_the_phone_on_hold(env):
    env.frappe.session.user, env.frappe.user_roles = PREPARER, {"HR Manager"}
    env.frappe.db.table("Employee")["EMP-2"]["cell_number"] = "0733000222"
    claim(env, "HR-EXP-2", employee="EMP-2", total_sanctioned_amount=3000, grand_total=3000)
    claim(env, "HR-EXP-3", employee="EMP-3")
    on_hold(env)
    out = env.ed.prepare_bulk(EC, ["HR-EXP-2", "HR-EXP-3"])
    (reason,) = [s["reason"] for s in out["skipped"]]
    assert reason.startswith("Employee M-Pesa Account MEA-2 is on hold until")
    rows = [(r["reference_name"], r["partyb"]) for r in env.frappe.db.table(ROW).values() if r["parent"] == out["b2c"]]
    assert rows == [("HR-EXP-3", "254711000333")]  # no approved phone: the Cell Number while the lock is off


def test_get_references_leaves_the_number_empty_while_the_default_phone_is_on_hold(env):
    identify = env.disb.B2CPaymentDisbursement.get_party_identifier
    env.frappe.db.table("Employee")["EMP-2"]["cell_number"] = "0733000222"
    on_hold(env)
    assert identify(None, fake_frappe._dict(employee="EMP-2"), "Employee", "Mpesa Disbursement") == {
        "account_number": ""}


# ---------------------------------------------------------------- K15: a supplier's approved phones
def test_a_b2c_request_made_by_hand_pays_a_supplier_only_its_approved_phone(env):
    req = fake_frappe._dict(reference_doctype="Purchase Invoice", reference_name="PINV-1", party_type="Supplier",
                            party="ACME", phone_number="0733999888", docstatus=0, flags=fake_frappe._dict())
    env.ed.check_b2c_request(req)  # no approved phone account: any number, as before
    supplier_phone(env)
    with pytest.raises(env.frappe.ValidationError, match="0733999888 is not a phone approved for ACME. ACME has "
                                                         "approved M-Pesa phone accounts"):
        env.ed.check_b2c_request(req)
    req.docstatus = 1  # and at the send
    with pytest.raises(env.frappe.ValidationError, match="is not a phone approved for ACME"):
        env.ed.check_b2c_request(req)
    req.phone_number = "0711222333"
    env.ed.check_b2c_request(req)
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-P1"]["active_from"] = NOW + datetime.timedelta(days=1)
    with pytest.raises(env.frappe.ValidationError, match="MSA-P1 is on hold"):
        env.ed.check_b2c_request(req)
    req.flags.mpesa_from_disbursement = True  # its disbursement's row was checked
    env.ed.check_b2c_request(req)


def failed_supplier_b2c(env, payee_account=None, phone="254711222333"):
    """A failed B2C payout to ACME from disbursement row R-S1 (naming ``payee_account``)."""
    env.frappe.db.table(ROW)["R-S1"] = {
        "name": "R-S1", "parent": "MPESA-B2C-S", "mpesa_schedule_line": None, "claim_payee": None,
        "payee_account_doctype": SUPPLIER_ACCOUNT if payee_account else None, "payee_account": payee_account}
    return fake_frappe._dict(name="B2C-S1", reference_doctype="Purchase Invoice", reference_name="PINV-1",
                             party_type="Supplier", party="ACME", phone_number=phone,
                             b2c_payment_disbursement_reference="R-S1")


def test_a_retry_to_a_supplier_checks_the_approved_account_its_row_named(env):
    supplier_phone(env)
    env.ed.check_b2c_retry(failed_supplier_b2c(env, "MSA-P1"))
    # the SIM was stolen and a manager cancelled the supplier's only approved phone since the failure
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-P1"]["docstatus"] = 2
    with pytest.raises(env.frappe.ValidationError, match="MSA-P1 is not approved \\(cancelled\\)"):
        env.ed.check_b2c_retry(failed_supplier_b2c(env, "MSA-P1"))
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-P1"].update(docstatus=1, needs_reapproval=1)
    with pytest.raises(env.frappe.ValidationError, match="MSA-P1 is blocked"):
        env.ed.check_b2c_retry(failed_supplier_b2c(env, "MSA-P1"))
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-P1"].update(needs_reapproval=0,
                                                         active_from=NOW + datetime.timedelta(days=1))
    with pytest.raises(env.frappe.ValidationError, match="MSA-P1 is on hold"):
        env.ed.check_b2c_retry(failed_supplier_b2c(env, "MSA-P1"))
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-P1"]["active_from"] = NOW - datetime.timedelta(days=1)
    with pytest.raises(env.frappe.ValidationError, match="254733999888 is not the phone of MSA-P1"):
        env.ed.check_b2c_retry(failed_supplier_b2c(env, "MSA-P1", phone="254733999888"))


def test_a_retry_to_a_supplier_whose_row_named_no_account_takes_its_approved_phones_since(env):
    env.ed.check_b2c_retry(failed_supplier_b2c(env, phone="254733999888"))  # no approved phone: as before
    supplier_phone(env)  # approved since the row was made
    with pytest.raises(env.frappe.ValidationError, match="254733999888 is not a phone approved for ACME"):
        env.ed.check_b2c_retry(failed_supplier_b2c(env, phone="254733999888"))
    env.ed.check_b2c_retry(failed_supplier_b2c(env))


# ---------------------------------------------------------------- K9: a B2B draft reads only what its maker reads
@pytest.fixture
def b2b_env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        yield env


def b2b_draft(env, **values):
    row = env.request("MB2B-NEW", **{"docstatus": 0, "status": "Not Initiated", "transaction_id": None, "amount": 3000,
                                     "purchase_invoice": "PINV-1", "supplier": None, "company": None,
                                     "entry_mode": None, **values})
    return env.frappe.get_doc(b2b_fixture.B2B, row["name"])


def saved_before(doc, **values):
    """The draft as it was before this save (Frappe's get_doc_before_save)."""
    object.__setattr__(doc, "get_doc_before_save", lambda: fake_frappe._dict(values))


def test_a_b2b_draft_names_an_invoice_only_for_who_reads_it(b2b_env, monkeypatch):
    env = b2b_env
    env.pi()
    env.pi("PINV-2")
    reads(env.frappe, monkeypatch)  # an Expense Approver: no read on Purchase Invoices
    doc = b2b_draft(env, amount=9999999)
    with pytest.raises(env.frappe.PermissionError, match="No read on Purchase Invoice PINV-1"):
        env.b2b.validate_request(doc)
    # refused before the invoice was read: neither its supplier nor what it owes is shown
    assert doc.supplier is None and not [e for e in env.log if e[:2] == ("row", "Purchase Invoice")]
    # a payer saving a colleague's draft, its invoice unchanged, needs no read on it
    doc = b2b_draft(env)
    saved_before(doc, purchase_invoice="PINV-1")
    env.b2b.validate_request(doc)
    assert doc.supplier == "ACME"
    saved_before(doc, purchase_invoice="PINV-2")  # the invoice changed: read it is
    with pytest.raises(env.frappe.PermissionError, match="No read on Purchase Invoice PINV-1"):
        env.b2b.validate_request(doc)
    doc.flags = fake_frappe._dict(mpesa_from_schedule=True)  # Pay Selected checked its preparer
    env.b2b.validate_request(doc)
    reads(env.frappe, monkeypatch, docs={"PINV-1"})
    env.b2b.validate_request(b2b_draft(env))


def test_an_accounts_role_names_an_order_by_its_purchase_invoice_read(b2b_env, monkeypatch):
    # ERPNext gives Purchase Order read only to the Purchase and Stock roles: an Accounts User pays its
    # advance as before; a role that reads no invoice (HR, Expense Approver) does not learn the order's supplier
    env = b2b_env
    env.po()
    asked = reads(env.frappe, monkeypatch, doctypes={"Purchase Invoice"})
    doc = b2b_draft(env, purchase_invoice=None, purchase_order="PO-1")
    env.b2b.validate_request(doc)
    assert doc.supplier == "ACME" and ("Purchase Invoice", "read", None) in asked
    reads(env.frappe, monkeypatch)
    with pytest.raises(env.frappe.PermissionError, match="No read on Purchase Order PO-1"):
        env.b2b.validate_request(b2b_draft(env, purchase_invoice=None, purchase_order="PO-1"))
    reads(env.frappe, monkeypatch, docs={"PO-1"})  # a Purchase User
    env.b2b.validate_request(b2b_draft(env, purchase_invoice=None, purchase_order="PO-1"))


def test_a_b2b_draft_names_a_claim_or_advance_only_for_who_reads_it(env, monkeypatch):
    # an Expense Approver whose User Permissions leave out HR-EXP-1
    reads(env.frappe, monkeypatch, doctypes={EC, EA}, docs={"HR-EXP-2"})
    doc = request(env)
    with pytest.raises(env.frappe.PermissionError, match="No read on Expense Claim HR-EXP-1"):
        env.b2b.validate_request(doc)
    assert doc.employee is None  # the claim's employee was not filled in
    saved_before(doc, expense_claim="HR-EXP-1")  # a payer saving it again
    env.b2b.validate_request(doc)
    assert doc.employee == "EMP-1"


# ---------------------------------------------------------------- K12: Get References reads by the user's rights
def test_get_references_needs_read_on_what_it_lists(payouts_env, monkeypatch):
    env = payouts_env
    fetched = []
    monkeypatch.setattr(env.Doc, "_check_can_prepare", lambda self: None)
    monkeypatch.setattr(env.Doc, "_fetch_entries", lambda self, *a: fetched.append(a[0]) or [])
    doc = env.Doc(name="D2")
    for doctype in (EC, EA, "Purchase Invoice", "Purchase Order"):
        reads(env.frappe, monkeypatch)  # an HR User has no Employee Advance read, nor invoices or orders
        with pytest.raises(env.frappe.PermissionError, match=f"No read on {doctype}"):
            doc.get_outstanding_reference_documents({"transaction_to_pay_against": doctype, "company": "Astral"})
        reads(env.frappe, monkeypatch, doctypes={doctype})
        doc.get_outstanding_reference_documents({"transaction_to_pay_against": doctype, "company": "Astral"})
    assert fetched == [EC, EA, "Purchase Invoice", "Purchase Order"]


def test_get_references_lists_only_the_claims_the_user_may_read(payouts_env, monkeypatch):
    # an Expense Approver with an Employee User Permission and one claim shared with them
    env = payouts_env
    documents = importlib.import_module("mpsa_suite.money_out.employee_documents")
    monkeypatch.setattr(documents, "payable_entries", lambda doctype, entries: entries)
    claims = [fake_frappe._dict(name=n, employee="EMP-1") for n in ("HR-EXP-1", "HR-EXP-2", "HR-EXP-3")]
    monkeypatch.setattr(env.frappe.db, "get_all", lambda *a, **kw: list(claims), raising=False)
    asked = reads(env.frappe, monkeypatch, doctypes={EC}, docs={"HR-EXP-2"})
    from mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.config import DOCTYPE_CONFIGS

    found = env.Doc(name="D2")._fetch_entries(EC, DOCTYPE_CONFIGS[EC], {"company": "Astral"}, {})
    assert [e.name for e in found] == ["HR-EXP-2"]
    assert (EC, "read", "HR-EXP-1") in asked


def test_get_references_lists_only_the_invoices_and_orders_the_user_may_read(payouts_env, monkeypatch):
    env = payouts_env
    payment_entry = types.ModuleType("erpnext.accounts.doctype.payment_entry.payment_entry")
    payment_entry.get_outstanding_reference_documents = lambda args: [
        {"voucher_type": t, "voucher_no": n, "outstanding_amount": 1000}
        for t, n in (("Purchase Order", "PO-1"), ("Purchase Order", "PO-2"), ("Purchase Invoice", "PINV-1"))]
    party = types.ModuleType("erpnext.accounts.party")
    party.get_party_account = lambda *a, **kw: "Creditors - AIL"
    for name in ("erpnext", "erpnext.accounts", "erpnext.accounts.doctype", "erpnext.accounts.doctype.payment_entry"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, payment_entry.__name__, payment_entry)
    monkeypatch.setitem(sys.modules, party.__name__, party)
    args = {"party_type": "Supplier", "party": "ACME", "company": "Astral"}
    reads(env.frappe, monkeypatch, docs={"PO-2", "PINV-1"})  # a Purchase User with a User Permission
    assert [e["voucher_no"] for e in env.Doc(name="D2")._fetch_erpnext_entries("Purchase Order", args)] == ["PO-2"]
    reads(env.frappe, monkeypatch, doctypes={"Purchase Invoice"})  # an Accounts User reads orders as invoices
    assert [e["voucher_no"] for e in env.Doc(name="D2")._fetch_erpnext_entries("Purchase Order", args)] == [
        "PO-1", "PO-2"]
    reads(env.frappe, monkeypatch, doctypes={"Purchase Invoice"}, docs={"PO-1"})
    assert env.Doc(name="D2")._fetch_erpnext_entries("Purchase Invoice", args) == []


# ---------------------------------------------------------------- K5: salaries asked before a slip is read
def test_a_salary_row_is_refused_before_its_slips_outstanding_is_read(payouts_env, monkeypatch):
    # an Accounts Manager (create on the disbursement, no HR role) types a slip and an amount: whatever
    # the slip owes, the answer is the same refusal, so its net pay cannot be probed
    env = payouts_env
    payroll = importlib.import_module("mpsa_suite.money_out.payroll")
    seen = {"salaries": False}

    def require_salary_reader():
        if not seen["salaries"]:
            raise env.frappe.PermissionError(payroll.SALARIES_HIDDEN)

    monkeypatch.setattr(payroll, "require_salary_reader", require_salary_reader)
    reads(env.frappe, monkeypatch, doctypes={SLIP}, docs={"SS-1", "SS-9"})
    env.frappe.db.table(SLIP)["SS-1"] = {"name": "SS-1", "docstatus": 1, "rounded_total": 45123, "net_pay": 45123.40,
                                         "employee": "EMP-1", "payroll_entry": "PE-1"}

    def disbursement(slip, amount):
        row = fake_frappe._dict(idx=1, reference_doctype=SLIP, reference_name=slip, allocated_amount=amount,
                                b2c_disbursement_request=None)
        return env.Doc(name="D9", docstatus=0, flags=fake_frappe._dict(), company="Astral", posting_date="2026-10-18",
                       party_type="Employee", paid_from="Mpesa - AIL", paid_to="Payroll Payable - AIL",
                       mode_of_payment="Mpesa", transaction_to_pay_against=SLIP, references=[row])

    for slip, amount in (("SS-1", 50000), ("SS-9", 1000)):  # more than it owes; settled (or not submitted)
        with pytest.raises(env.frappe.PermissionError, match="salaries are shown only"):
            disbursement(slip, amount).validate()
    seen["salaries"] = True  # who may see salaries is told what the slip has left
    with pytest.raises(env.frappe.ValidationError, match="Row #1: Salary Slip SS-9 has nothing left to pay"):
        disbursement("SS-9", 1000).validate()
