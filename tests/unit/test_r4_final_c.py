"""Round 4 final review, area C: who prepares a claim's or advance's payout by default, the B2C
phone lock on renamed sites, a re-prompt never superseding a Payment Request whose prompt was
paid, and who settles a reversal whose result never came. On an in-memory frappe."""

import importlib
import json
import types
from pathlib import Path

import fake_frappe
import pytest

from mpsa_suite import permission_matrix as M

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
REVERSAL = "Mpesa Reversal Request"
REGISTER = "Mpesa C2B Payment Register"


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        yield frappe
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- who prepares a claim's or advance's payout
# HRMS's own DocPerms (v15 and v16): who reads the document's DocType, the employee's own role aside
HRMS_READERS = {"Expense Claim": (M.HM, M.HU, M.EA), "Employee Advance": (M.EA,)}
PAYOUTS = ("B2C Payment Disbursement", "Mpesa B2B Payment Request")


def _preparers():
    import ast

    tree = ast.parse((APP / "permissions.py").read_text(encoding="utf-8"))
    return next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and n.targets[0].id == "PREPARE_ROLES")


@pytest.mark.parametrize("doctype", sorted(HRMS_READERS))
def test_every_default_reader_of_a_claim_or_advance_may_prepare_both_routes(doctype):
    # plan section 1: no default role both read an Expense Claim and could create a B2B; nobody could prepare an
    # advance. Fixed in the suite's own rights, never by DocPerms on the HRMS DocTypes.
    for role in HRMS_READERS[doctype]:
        assert role in _preparers(), role
        for payout in PAYOUTS:
            rights = M.rows(payout).get((role, 0), set())
            assert {"read", "write", "create"} <= rights, (role, payout)
            assert not rights & {"submit", "cancel"}, f"{role} may send {payout}"


def test_the_suite_ships_no_docperm_on_hrms_doctypes():
    for doctype in HRMS_READERS:
        assert doctype not in M.DOCTYPES
    shipped = {json.loads(p.read_text(encoding="utf-8")).get("name") for p in APP.glob("**/doctype/*/*.json")}
    assert not shipped & set(HRMS_READERS)
    custom = [p for p in APP.glob("**/custom/*.json") if "expense_claim" in p.name or "employee_advance" in p.name]
    for path in custom:  # Customize Form exports may add fields, never permissions
        assert not json.loads(path.read_text(encoding="utf-8")).get("custom_perms"), path


def test_an_expense_approver_sees_no_salary():
    import ast

    tree = ast.parse((APP / "permissions.py").read_text(encoding="utf-8"))
    salary = next(ast.literal_eval(n.value) for n in tree.body
                  if isinstance(n, ast.Assign) and n.targets[0].id == "SALARY_ROLES")
    assert M.EA not in salary


# ---------------------------------------------------------------- the B2C phone lock on a new site only
@pytest.fixture
def install(env):
    module = importlib.import_module("mpsa_suite.install")
    written = []
    env.db.table("DocType")["M-Pesa Suite Settings"] = {"name": "M-Pesa Suite Settings"}
    env.db.set_single_value = lambda doctype, field, value: written.append((doctype, field, value))
    return module, written


def test_a_new_site_locks_b2c_to_approved_phones(env, install):
    module, written = install
    module.approved_phones_only_on_new_sites()
    assert written == [("M-Pesa Suite Settings", "b2c_approved_phones_only", 1)]


def test_the_rename_keeps_the_lock_off(env, install):
    # Trident and Astral become mpsa_suite by install-app next to the old app (docs/RENAME_SPIKE.md): after_install
    # runs on a site that pays its employees by B2C today
    module, written = install
    env.get_installed_apps = lambda: ["frappe", "erpnext", "hrms", "frappe_mpsa_payments", "mpsa_suite"]
    module.approved_phones_only_on_new_sites()
    assert written == []


@pytest.mark.parametrize("doctype", ["B2C Disbursement Request", "B2C Payment Disbursement"])
def test_a_site_with_b2c_history_keeps_the_lock_off(env, install, doctype):
    module, written = install
    env.db.table(doctype)["OLD-1"] = {"name": "OLD-1"}
    module.approved_phones_only_on_new_sites()
    assert written == [] and module.has_b2c_history()


# ---------------------------------------------------------------- a re-prompt never supersedes a paid one
@pytest.fixture
def requests(env):
    module = importlib.import_module("mpsa_suite.money_in.payment_request")
    cancelled, deleted = [], []

    class PR(fake_frappe.FakeDoc):
        flags = fake_frappe._dict()

        def cancel(self):
            cancelled.append(self.name)

        def delete(self):
            deleted.append(self.name)

    plain = env.get_doc
    env.get_doc = lambda dt, name=None, **kw: (PR(env, dt, env.db.table(dt)[name]) if dt == "Payment Request"
                                              else plain(dt, name, **kw))
    for name in ("PR-PAID", "PR-UNPAID"):
        env.db.table("Payment Request")[name] = {
            "name": name, "reference_doctype": "Sales Invoice", "reference_name": "SINV-1", "docstatus": 1,
            "status": "Requested", "payment_request_type": "Inward", "payment_gateway": "Mpesa-TILL"}
    env.db.table("Mpesa Express Request")["MEXP-1"] = {
        "name": "MEXP-1", "reference_doctype": "Payment Request", "reference_name": "PR-PAID", "docstatus": 1,
        "status": "Completed", "transaction_id": "TPAID00001", "amount": 2500.0, "currency": "KES", "is_reconciled": 0}
    return module, cancelled


def test_supersede_keeps_a_request_whose_prompt_was_paid(env, requests):
    module, cancelled = requests
    module.supersede_unpaid(fake_frappe._dict(reference_doctype="Sales Invoice", reference_name="SINV-1", name=None))
    assert cancelled == ["PR-UNPAID"]
    assert [p.name for p in module.unbooked_prompts("PR-PAID")] == ["MEXP-1"]
    assert "MEXP-1 (receipt TPAID00001)" in module.being_booked("PR-PAID", module.unbooked_prompts("PR-PAID"))


@pytest.mark.parametrize("status,holds", [("Reversed", False), ("Already Reversed", False), ("Unknown", True),
                                          ("Initiated", True), ("Failed", True)])
def test_a_prompt_whose_money_went_back_holds_nothing(env, requests, status, holds):
    module, _cancelled = requests
    env.db.table(REVERSAL)["MRV-1"] = {"name": "MRV-1", "transaction_id": "TPAID00001", "docstatus": 1,
                                       "status": status}
    assert bool(module.unbooked_prompts("PR-PAID")) is holds


def test_once_booked_the_request_is_superseded_as_before(env, requests):
    module, cancelled = requests
    env.db.table("Mpesa Express Request")["MEXP-1"]["is_reconciled"] = 1
    module.supersede_unpaid(fake_frappe._dict(reference_doctype="Sales Invoice", reference_name="SINV-1", name=None))
    assert sorted(cancelled) == ["PR-PAID", "PR-UNPAID"]


# ---------------------------------------------------------------- a reversal whose result never came
@pytest.fixture
def reversal(env, monkeypatch):
    env.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
    env.logger = lambda *a, **kw: None
    env.get_cached_doc = lambda *a, **kw: fake_frappe._dict(refuse_administrator_payouts=1)
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.mpesa_reversal_request.mpesa_reversal_request")
    flows = importlib.import_module("mpsa_suite.money_out.reversal")
    env.db.table(REGISTER)["MPC2B-1"] = {"name": "MPC2B-1", "transid": "OEI2AK4Q16", "transamount": 500.0,
                                         "businessshortcode": "600999", "docstatus": 1}
    env.db.table("Mpesa Settings")["S1"] = {"name": "S1", "business_shortcode": "600999", "use_reversal": 1,
                                            "initiator_name": "op", "security_credential": "x"}
    monkeypatch.setattr(flows.apis, "require", lambda settings, api: None)

    def saved(status, name="MRV-1", docstatus=1):
        env.db.table(REVERSAL)[name] = {"name": name, "docstatus": docstatus, "status": status,
                                        "c2b_register": "MPC2B-1", "creation": len(env.db.table(REVERSAL))}
        doc = module.MpesaReversalRequest()
        doc.doctype, doc.name, doc.docstatus, doc.status = REVERSAL, name, docstatus, "Not Initiated"  # a stale form
        return doc

    return types.SimpleNamespace(module=module, flows=flows, saved=saved)


def test_only_a_named_manager_cancels_a_reversal_that_may_have_returned_the_money(env, reversal):
    doc = reversal.saved("Unknown")
    env.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.PermissionError):
        doc.before_cancel()
    assert doc.status == "Unknown", "decided on what the database holds"
    env.session.user, env.user_roles = "Administrator", {"System Manager", "M-Pesa Manager"}
    with pytest.raises(env.PermissionError, match="Use your own login"):
        reversal.saved("Unknown").before_cancel()
    env.session.user, env.user_roles = "boss@example.com", {"M-Pesa Manager"}
    doc = reversal.saved("Unknown")
    doc.before_cancel()
    assert doc.status == "Unknown", "kept on the cancelled reversal: a late Result still matches it"


def test_a_failed_reversal_is_still_cancelled_by_a_payer(env, reversal):
    env.user_roles = {"M-Pesa Payer"}
    reversal.saved("Failed").before_cancel()
    with pytest.raises(env.ValidationError, match="A reversal that is Reversed cannot be cancelled"):
        reversal.saved("Reversed").before_cancel()


def _new(env):
    return env.get_doc({"doctype": REVERSAL, "name": "MRV-2", "c2b_register": "MPC2B-1", "mpesa_settings": "S1"})


def test_after_an_unknown_reversal_was_cancelled_only_a_manager_reverses_the_receipt_again(env, reversal):
    reversal.saved("Unknown", docstatus=2)
    env.user_roles = {"M-Pesa Payer"}  # Make Reversal again, without checking the statement
    with pytest.raises(env.PermissionError, match="Only an M-Pesa Manager"):
        reversal.flows.validate_request(_new(env))
    env.session.user, env.user_roles = "Administrator", {"System Manager", "M-Pesa Manager"}
    with pytest.raises(env.PermissionError, match="Only an M-Pesa Manager"):
        reversal.flows.validate_request(_new(env))
    env.session.user, env.user_roles = "boss@example.com", {"M-Pesa Manager"}
    reversal.flows.validate_request(_new(env))


def test_a_cancelled_reversal_that_m_pesa_reversed_later_refuses_another(env, reversal):
    reversal.saved("Reversed", docstatus=2)  # its Result came after the cancel
    env.session.user, env.user_roles = "boss@example.com", {"M-Pesa Manager"}
    with pytest.raises(env.ValidationError, match="back with the customer already"):
        reversal.flows.validate_request(_new(env))


def test_a_cancelled_failed_reversal_holds_nothing(env, reversal):
    reversal.saved("Failed", docstatus=2)
    env.user_roles = {"M-Pesa Payer"}
    reversal.flows.validate_request(_new(env))
