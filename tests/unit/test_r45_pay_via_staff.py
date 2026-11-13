"""Round 4.5: Pay via Staff, strict (money_out.pay_via_staff), on the in-memory site of test_employee_documents (SQL
on SQLite, the real B2C disbursement and B2B request controllers, HRMS's Employee Advance as rows).

Covers: who may (M-Pesa Suite Settings' Who May Pay via Staff; empty means nobody; a named login), at the
button (document_state), the advance (make_advance), the payout's row on every save, the send and a retry, a
B2B request never paying such an advance, an advance with the fields made any other way refused; the advance
itself (its fields, account, amount: what the order or invoice still owes less its other staff advances, the
approved phone only, never to oneself) and its draft disbursement; the Employee Advance form and the list's Pay
by M-Pesa for who is not listed; the settlement Journal Entry on the supplier's invoice (its lines, capped at the
invoice's outstanding and at what each advance has open, the orders' advances too), on submit and by hand; the
daily section; the hooks and fields.
"""

import ast
import datetime
import importlib
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest
from test_employee_documents import ADVANCES, EA, advance
from test_employee_documents import env as ed_env  # noqa: F401 (the fixture this one extends)
from test_payroll_schedule import B2B, DISB, PAYER, PHONE, REQ, ROW
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture test_employee_documents' extends)

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
SUITE, LISTED = "M-Pesa Suite Settings", "M-Pesa Pay via Staff User"
PI, PO = "Purchase Invoice", "Purchase Order"
JE, JEA = b2b_fixture.JE, b2b_fixture.JEA
OWNER, JANKI, CLERK = "owner@example.com", "janki@example.com", "clerk@example.com"
_dict = fake_frappe._dict


class AdvanceDoc(fake_frappe.FakeDoc):
    """frappe.new_doc("Employee Advance"): HRMS's advance as the test site keeps it; insert runs the
    suite's validate hook (pay_via_staff.validate_advance), as hooks.py has it."""

    def __init__(self, env):
        super().__init__(env.frappe, EA, {})
        object.__setattr__(self, "flags", _dict())
        object.__setattr__(self, "meta", types.SimpleNamespace(has_field=lambda f: True))
        object.__setattr__(self, "_env", env)

    def update(self, values):
        self._row.update(values)

    def is_new(self):
        return not self._row.get("docstatus") and self._row.get("name") not in self._env.frappe.db.table(EA)

    def insert(self, ignore_permissions=False):
        env = self._env
        env.pvs.validate_advance(self)
        employee = env.frappe.db.table("Employee")[self._row["employee"]]
        self._row.update(name=self._row.get("name") or f"HR-EAD-NEW-{len(env.frappe.db.table(EA)) + 1}", docstatus=0,
                         employee_name=employee["employee_name"], paid_amount=0, claimed_amount=0, return_amount=0,
                         status="Draft", creation=b2b_fixture.NOW)
        env.made.append(("insert", dict(self.flags)))
        env.frappe.db.table(EA)[self._row["name"]] = self._row
        return self

    def submit(self):
        self._row.update(docstatus=1, status="Unpaid")
        self._env.made.append(("submit", self._row["name"]))


@pytest.fixture
def env(ed_env, monkeypatch):  # noqa: F811
    env = ed_env
    frappe = env.frappe
    env.pvs = importlib.import_module("mpsa_suite.money_out.pay_via_staff")
    env.made = []
    plain_new_doc = frappe.new_doc
    frappe.new_doc = lambda doctype: AdvanceDoc(env) if doctype == EA else plain_new_doc(doctype)
    db = frappe.db
    db.table("Company")["Astral"]["default_employee_advance_account"] = ADVANCES
    db.table("Supplier")["ACME"]["supplier_name"] = "Shell Kilimani Ltd"
    for user in (OWNER, JANKI, CLERK):
        db.table("User")[user] = {"name": user, "full_name": user.split("@")[0].title(), "mobile_no": None,
                                  "phone": None}
    listed(env, OWNER, JANKI)
    env.po("PO-1", rounded_total=6000, grand_total=6000)
    env.pi("PINV-1", outstanding_amount=10000)
    as_user(env, OWNER, "M-Pesa Manager")
    yield env


def listed(env, *users):
    table = env.frappe.db.table(LISTED)
    table.clear()
    for i, user in enumerate(users, 1):
        table[f"PVS-{i}"] = {"name": f"PVS-{i}", "parent": SUITE, "parenttype": SUITE, "user": user}


def as_user(env, user, *roles):
    env.frappe.session.user, env.frappe.user_roles = user, set(roles)


def raises(env, match, exc=None):
    return pytest.raises(exc or env.frappe.ValidationError, match=match)


def staff_advance(env, name="HR-EAD-S1", employee="EMP-2", amount=4000, paid=4000, **kw):
    values = {"employee": employee, "employee_name": "Otieno Ouma", "advance_amount": amount, "paid_amount": paid,
              "claimed_amount": 0, "return_amount": 0, "posting_date": datetime.date(2026, 10, 10),
              "creation": b2b_fixture.NOW, "mpesa_supplier": "ACME", "mpesa_purchase_order": kw.pop("order", "PO-1"),
              "mpesa_purchase_invoice": kw.pop("invoice", None)}
    return advance(env, name, **{**values, **kw})


# ---------------------------------------------------------------- who may
def test_only_the_listed_users_may_and_empty_means_nobody(env):
    assert env.pvs.listed_users() == [JANKI, OWNER]
    env.pvs.require_listed()
    as_user(env, CLERK, "M-Pesa Manager")
    with raises(env, r"Only the users M-Pesa Suite Settings lists under Who May Pay via Staff may use Pay via Staff "
                     r"\(janki@example.com, owner@example.com\)", env.frappe.PermissionError):
        env.pvs.require_listed()
    listed(env)
    as_user(env, OWNER, "M-Pesa Manager")
    with raises(env, r"\(nobody is listed\)", env.frappe.PermissionError):
        env.pvs.require_listed()
    # the shared Administrator login is refused even when listed (a named login)
    listed(env, "Administrator")
    as_user(env, "Administrator", "M-Pesa Manager")
    with raises(env, "Use your own login", env.frappe.PermissionError):
        env.pvs.require_listed()


def test_the_button_shows_to_the_listed_users_only(env):
    state = env.pvs.document_state(PO, "PO-1")
    assert (state["allowed"], state["left"], state["advances"], state["can_settle"]) == (True, 6000, [], False)
    as_user(env, CLERK, "M-Pesa Manager", "Accounts Manager")
    assert env.pvs.document_state(PO, "PO-1")["allowed"] is False
    env.denied.add((PO, "read"))
    with raises(env, "no read", env.frappe.PermissionError):
        env.pvs.document_state(PO, "PO-1")


# ---------------------------------------------------------------- the advance
def test_a_listed_user_makes_the_advance_and_its_draft_payout_to_the_approved_phone(env):
    out = env.pvs.make_advance(PO, "PO-1", "EMP-2", 2500)
    adv = env.frappe.db.table(EA)[out["employee_advance"]]
    assert (adv["docstatus"], adv["employee"], adv["company"], adv["advance_amount"], adv["advance_account"]) == (
        1, "EMP-2", "Astral", 2500, ADVANCES)
    assert (adv["mpesa_supplier"], adv["mpesa_purchase_order"], adv.get("mpesa_purchase_invoice")) == (
        "ACME", "PO-1", None)
    assert adv["purpose"] == "Pay Shell Kilimani Ltd for Purchase Order PO-1 (Pay via Staff)"
    assert env.made[0] == ("insert", {"mpsa_pay_via_staff": True, "ignore_permissions": True})
    disb = env.frappe.db.table(DISB)[out["disbursement"]]
    (row,) = [r for r in env.frappe.db.table(ROW).values() if r["parent"] == out["disbursement"]]
    assert (disb["docstatus"], disb["paid_to"]) == (0, ADVANCES)
    assert (row["reference_doctype"], row["reference_name"], row["party"], row["partyb"], row["payee_account"],
            row["allocated_amount"]) == (EA, out["employee_advance"], "EMP-2", PHONE, "MEA-2", 2500)
    assert "Pay via Staff by owner@example.com: KES 2,500 to EMP-2" in env.frappe.comments[-1][2]
    # what is left for another staff advance: the order less this one
    assert env.pvs.document_state(PO, "PO-1")["left"] == 3500
    with raises(env, r"KES 4,000 is more than PO-1 has left for a staff advance \(KES 3,500\)"):
        env.pvs.make_advance(PO, "PO-1", "EMP-2", 4000)
    assert env.pvs.make_advance(PO, "PO-1", "EMP-2")["amount"] == 3500  # the default: all that is left


def test_nobody_else_makes_one(env):
    as_user(env, CLERK, "M-Pesa Manager", "HR Manager")
    with raises(env, "may make a Pay via Staff advance", env.frappe.PermissionError):
        env.pvs.make_advance(PO, "PO-1", "EMP-2", 2500)
    assert not env.made and not env.frappe.db.table(DISB)


@pytest.mark.parametrize("employee, change, message, exc", [
    ("EMP-3", {}, "EMP-3 has no approved phone", None),  # a Cell Number is never enough
    ("EMP-2", {"user_id": OWNER}, "You may not pay an advance to yourself: You are EMP-2", "perm"),
    ("EMP-2", {"company": "Other Ltd"}, "EMP-2 is an employee of Other Ltd, not Astral", None),
    ("EMP-4", {}, "EMP-4 is Left", None),
])
def test_the_staff_member_is_an_active_employee_with_an_approved_phone_and_not_oneself(env, employee, change,
                                                                                         message, exc):
    env.frappe.db.table("Employee")[employee].update(change)
    with raises(env, message, env.frappe.PermissionError if exc else None):
        env.pvs.make_advance(PO, "PO-1", employee, 1000)


def test_an_order_or_invoice_that_may_not_be_paid_gets_no_advance(env):
    env.frappe.db.table(PO)["PO-1"]["per_billed"] = 50
    with raises(env, "PO-1 already has invoices: pay the Purchase Invoice instead"):
        env.pvs.make_advance(PO, "PO-1", "EMP-2", 1000)
    with raises(env, "M-Pesa B2C pays KES 10 to 250,000"):
        env.pvs.make_advance(PI, "PINV-1", "EMP-2", 5)
    with raises(env, "whole shillings only"):
        env.pvs.make_advance(PI, "PINV-1", "EMP-2", 100.5)
    with raises(env, "Pick a Purchase Order or a Purchase Invoice"):
        env.pvs.make_advance("Sales Invoice", "SINV-1", "EMP-2", 100)
    env.frappe.db.table("Company")["Astral"]["default_employee_advance_account"] = None
    with raises(env, "Set the Default Employee Advance Account on Company Astral"):
        env.pvs.make_advance(PI, "PINV-1", "EMP-2", 1000)


def test_an_advance_with_the_fields_is_made_by_pay_via_staff_only(env):
    doc = AdvanceDoc(env)
    doc.update({"employee": "EMP-2", "company": "Astral", "mpesa_supplier": "ACME"})
    with raises(env, "made with Pay via Staff", env.frappe.PermissionError):
        doc.insert()
    plain = AdvanceDoc(env)
    plain.update({"employee": "EMP-2", "company": "Astral"})
    plain.insert()  # any other advance: as before
    saved = staff_advance(env)
    changed = fake_frappe.FakeDoc(env.frappe, EA, dict(saved, mpesa_supplier="OTHER"))
    object.__setattr__(changed, "is_new", lambda: False)
    object.__setattr__(changed, "flags", _dict())
    object.__setattr__(changed, "get_doc_before_save", lambda: _dict(saved))
    with raises(env, "never change", env.frappe.PermissionError):
        env.pvs.validate_advance(changed)


# ---------------------------------------------------------------- its payout: the row, the send, a retry, B2B
def test_the_advances_payout_is_prepared_by_a_listed_user_only_and_to_the_approved_phone_only(env):
    staff_advance(env, paid=0)
    as_user(env, "hr@example.com", "HR Manager")  # a preparer of advances, not listed
    with raises(env, "may pay a Pay via Staff advance", env.frappe.PermissionError):
        env.ed.prepare_b2c(EA, "HR-EAD-S1")
    as_user(env, OWNER, "HR Manager")
    out = env.ed.prepare_b2c(EA, "HR-EAD-S1")
    (row,) = [r for r in env.frappe.db.table(ROW).values() if r["parent"] == out["name"]]
    assert (row["partyb"], row["payee_account"]) == (PHONE, "MEA-2")
    # another number, even with the approved-phone lock off: refused
    from test_employee_documents import row_of

    typed = row_of(env, reference_doctype=EA, reference_name="HR-EAD-S1", party="EMP-2", partyb="0711000555",
                   allocated_amount=1000)
    with raises(env, r"Row #1: A Pay via Staff advance is paid only to EMP-2's approved phone"):
        typed.validate()


def test_the_send_and_a_retry_are_for_listed_users(env):
    staff_advance(env, paid=0)
    doc = _dict(references=[_dict(reference_doctype=EA, reference_name="HR-EAD-S1", idx=1)])
    env.pvs.check_send(doc)
    as_user(env, PAYER, "M-Pesa Payer")
    with raises(env, "may send a Pay via Staff advance", env.frappe.PermissionError):
        env.pvs.check_send(doc)
    with raises(env, "may send a Pay via Staff advance", env.frappe.PermissionError):
        env.ed.check_not_own_b2c(_dict(references=doc.references, flags=_dict()))
    # any other advance: as before
    env.pvs.check_send(_dict(references=[_dict(reference_doctype=EA, reference_name="HR-EAD-1")]))
    env.frappe.db.table(ROW)["R-1"] = {"name": "R-1", "parent": "MPESA-B2C-9", "payee_account": "MEA-2"}
    request = _dict(reference_doctype=EA, reference_name="HR-EAD-S1", b2c_payment_disbursement_reference="R-1",
                    phone_number=PHONE)
    with raises(env, "may send a Pay via Staff advance again", env.frappe.PermissionError):
        env.pvs.check_retry(request)
    as_user(env, JANKI, "M-Pesa Payer")
    env.pvs.check_retry(request)
    env.frappe.db.table("Employee M-Pesa Account")["MEA-2"]["name_mismatch"] = 1  # blocked since
    with raises(env, "MEA-2 is blocked"):
        env.pvs.check_retry(request)


def test_a_b2b_request_never_pays_a_staff_advance(env):
    staff_advance(env, paid=0)
    assert "is paid by B2C to the staff member's approved phone only" in env.pvs.b2b_problem(EA, "HR-EAD-S1")
    assert env.pvs.b2b_problem(EA, "HR-EAD-1") is None and env.pvs.b2b_problem(PI, "PINV-1") is None
    source = (APP / "money_out" / "b2b.py").read_text(encoding="utf-8")
    assert "problem = b2b_problem(dt, dn)" in source.split("def _check_employee_document")[1].split("\ndef ")[0]


def test_the_advance_form_and_the_lists_bulk_pay_leave_it_to_the_listed_users(env):
    staff_advance(env, paid=0)
    as_user(env, "hr@example.com", "HR Manager")
    status = env.ed.document_status(EA, "HR-EAD-S1")
    assert status["can_prepare"] is False
    assert status["problem"].startswith("HR-EAD-S1 is a Pay via Staff advance (for PO-1): only the users")
    made = env.ed.prepare_bulk(EA, ["HR-EAD-S1"])
    assert made["b2c"] is None and made["skipped"][0]["reason"].startswith("HR-EAD-S1 is a Pay via Staff advance")
    as_user(env, OWNER, "HR Manager")
    status = env.ed.document_status(EA, "HR-EAD-S1")
    assert (status["can_prepare"], status["b2b_accounts"], status["b2b_default"]) == (True, [], None)


# ---------------------------------------------------------------- settlement against the supplier's invoice
def journal_lines(env, name):
    return sorted((r["account"], r.get("party_type"), r.get("party"), r.get("reference_type"),
                   r.get("reference_name"), r["debit_in_account_currency"], r["credit_in_account_currency"],
                   r.get("is_advance"))
                  for r in env.frappe.db.table(JEA).values() if r["parent"] == name)


def test_the_invoice_settles_its_staff_advances_in_one_journal(env):
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000, order=None, invoice="PINV-1")
    staff_advance(env, "HR-EAD-S2", amount=3000, paid=3000, claimed_amount=1000)  # on the invoice's order
    env.frappe.db.table("Purchase Invoice Item")["PII-1"] = {"name": "PII-1", "parent": "PINV-1",
                                                             "parenttype": PI, "purchase_order": "PO-1"}
    out = env.pvs.settle_staff_advances("PINV-1")
    assert out["settled"] == [("HR-EAD-S1", 4000), ("HR-EAD-S2", 2000)] and out["left"] == 4000
    journal = env.frappe.db.table(JE)[out["journal_entry"]]
    assert (journal["docstatus"], journal["voucher_type"], journal["company"]) == (1, "Journal Entry", "Astral")
    assert journal_lines(env, out["journal_entry"]) == [
        ("Creditors - AIL", "Supplier", "ACME", PI, "PINV-1", 6000, 0, None),
        (ADVANCES, "Employee", "EMP-2", EA, "HR-EAD-S1", 0, 4000, "Yes"),
        (ADVANCES, "Employee", "EMP-2", EA, "HR-EAD-S2", 0, 2000, "Yes")]
    assert all(r.get("cost_center") == "Branch - AIL" for r in env.frappe.db.table(JEA).values())
    assert [c[:2] for c in env.frappe.comments[-3:]] == [(PI, "PINV-1"), (EA, "HR-EAD-S1"), (EA, "HR-EAD-S2")]
    assert ("lock", "b2c-pay:Purchase Invoice:PINV-1") in env.log


def test_the_settlement_is_capped_at_the_invoices_outstanding(env):
    env.frappe.db.table(PI)["PINV-1"]["outstanding_amount"] = 2500
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000, order=None, invoice="PINV-1")
    out = env.pvs.settle_staff_advances("PINV-1")
    assert (out["settled"], out["left"]) == ([("HR-EAD-S1", 2500)], 0)  # the rest: HRMS's Return or Claim
    lines = journal_lines(env, out["journal_entry"])
    assert [(line[0], line[5], line[6]) for line in lines] == [("Creditors - AIL", 2500, 0), (ADVANCES, 0, 2500)]


@pytest.mark.parametrize("change", [{"paid": 0}, {"paid": 4000, "return_amount": 4000}, {"mpesa_supplier": "OTHER"},
                                    {"docstatus": 2}])
def test_nothing_is_settled_for_an_advance_not_paid_settled_or_of_another_supplier(env, change):
    staff_advance(env, "HR-EAD-S1", order=None, invoice="PINV-1", **change)
    assert env.pvs.settle_staff_advances("PINV-1") == {"journal_entry": None, "settled": [], "left": 10000}
    assert not env.frappe.db.table(JE)


def test_the_invoices_submit_settles_and_an_error_never_stops_it(env, monkeypatch):
    staff_advance(env, "HR-EAD-S1", order=None, invoice="PINV-1")
    invoice = _dict(name="PINV-1", is_return=0, items=[])
    env.pvs.on_submit_purchase_invoice(invoice)
    assert len(env.frappe.db.table(JE)) == 1
    env.frappe.db.table(JE).clear()
    env.frappe.db.table(JEA).clear()
    monkeypatch.setattr(env.pvs, "settle_staff_advances", lambda name: 1 / 0)
    env.pvs.on_submit_purchase_invoice(invoice)
    assert ("rollback", "mpesa_pay_via_staff_settle") in env.log
    assert env.frappe.errors[-1][1]["title"] == "M-Pesa Pay via Staff advances not settled"
    assert env.alerts[-1] == ("PINV-1", "PINV-1's staff advances (Pay via Staff) could not be settled: press Settle "
                                        "Staff Advances on it.")
    calls = []
    monkeypatch.setattr(env.pvs, "settle_staff_advances", lambda name: calls.append(name))
    env.pvs.on_submit_purchase_invoice(_dict(name="PINV-2", is_return=0, items=[]))  # no staff advance: nothing
    env.pvs.on_submit_purchase_invoice(_dict(name="PINV-1", is_return=1, items=[]))  # a debit note: nothing
    assert calls == []


def test_settle_staff_advances_by_hand(env):
    staff_advance(env, "HR-EAD-S1", order=None, invoice="PINV-1")
    as_user(env, CLERK, "Accounts User")
    with raises(env, "needs one of the roles", env.frappe.PermissionError):
        env.pvs.settle_now("PINV-1")
    as_user(env, CLERK, "Accounts Manager")
    state = env.pvs.document_state(PI, "PINV-1")
    assert (state["can_settle"], state["to_settle"], state["allowed"]) == (True, 4000, False)
    assert env.pvs.settle_now("PINV-1")["settled"] == [("HR-EAD-S1", 4000)]
    as_user(env, JANKI, "M-Pesa Manager")
    # settled once, whether or not HRMS wrote the journal back as the advance's Return Amount
    with raises(env, "has no staff advance to settle"):
        env.pvs.settle_now("PINV-1")
    env.frappe.db.table(EA)["HR-EAD-S1"]["return_amount"] = 4000
    with raises(env, "has no staff advance to settle"):
        env.pvs.settle_now("PINV-1")
    assert env.pvs.document_state(PI, "PINV-1")["advances"][0]["open"] == 0


# ---------------------------------------------------------------- the daily section
def test_advances_paid_days_ago_and_not_settled_are_listed(env):
    staff_advance(env, "HR-EAD-S1", amount=4000, paid=4000)
    staff_advance(env, "HR-EAD-S2", amount=3000, paid=3000, return_amount=3000)  # settled
    staff_advance(env, "HR-EAD-S3", amount=2000, paid=2000)
    env.frappe.db.table(REQ)["B2C-S3"] = {"name": "B2C-S3", "reference_doctype": EA, "reference_name": "HR-EAD-S3",
                                          "status": "Paid",
                                          "transaction_completed_datetime": datetime.datetime(2026, 10, 17, 9, 0)}
    title, lines, link = env.pvs.attention_section()
    assert title == "Staff advances for suppliers (Pay via Staff)"
    assert link == "/app/employee-advance?mpesa_supplier=%5B%22is%22%2C%22set%22%5D"
    assert lines == ["HR-EAD-S1: Otieno Ouma was paid KES 4,000 8 days ago to pay ACME (PO-1); KES 4,000 is not "
                     "settled"]  # S3 was paid yesterday: within the 3 days
    env.frappe.db.table(SUITE)[SUITE]["staff_advance_settle_days"] = 10
    assert env.pvs.attention_section()[1] == []


# ---------------------------------------------------------------- hooks and fields
def _hooks() -> dict:
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {n.targets[0].id: n.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}


def test_the_hooks_settle_on_the_invoices_submit_and_guard_the_advance():
    events = ast.literal_eval(_hooks()["doc_events"])
    assert events["Purchase Invoice"]["on_submit"] == "mpsa_suite.money_out.pay_via_staff.on_submit_purchase_invoice"
    assert events["Employee Advance"]["validate"] == "mpsa_suite.money_out.pay_via_staff.validate_advance"
    assert events["Employee Advance"]["before_cancel"] == "mpsa_suite.money_out.employee_documents.before_cancel"


def test_the_employee_advance_fields_are_read_only_and_never_copied(monkeypatch):
    import sys

    frappe, saved = fake_frappe.install()
    try:
        custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
        custom_field.create_custom_fields = lambda fields, update=False: None
        for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
        migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
        fields = {f["fieldname"]: f for f in migrate.till_payment_fields()["Employee Advance"]}
        assert set(fields) == {"mpesa_supplier", "mpesa_purchase_order", "mpesa_purchase_invoice"}
        assert {f["options"] for f in fields.values()} == {"Supplier", PO, PI}
        assert all(f["read_only"] and f["no_copy"] and f["search_index"] for f in fields.values())
    finally:
        fake_frappe.uninstall(saved)


def test_the_b2b_request_check_and_the_known_fields_stay_in_step():
    pvs = (APP / "money_out" / "pay_via_staff.py").read_text(encoding="utf-8")
    assert 'FIELDS = ("mpesa_purchase_order", "mpesa_purchase_invoice", "mpesa_supplier")' in pvs
    assert B2B == "Mpesa B2B Payment Request"
