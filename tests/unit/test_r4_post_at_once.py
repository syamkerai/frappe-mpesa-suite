"""The suite's own entries are posted at once (the user's rule, 2026-10-28): submitted, and in a
Payment Entry workflow its first submitted state (Approved), never drafts left for review.

Covers: every migrate switches "Submit B2C Accounting Entries" (each Mpesa Settings) and "Submit
Transfer and Funding Entries" (M-Pesa Suite Settings) on; saving either settings form keeps them
on; Make Entry posts the entry it makes, so it is refused to a user who may not submit it. That a
submit under a workflow lands in its approved state is Frappe's (set_workflow_state_on_action),
checked on the bench (test_b2b_documents).
"""

import importlib
import inspect
import sys
import types

import fake_frappe
import pytest

import b2b_fixture
from test_b2b_entries import make, row
from test_b2b_entries import env as b2b_env  # noqa: F401 (the B2B booking site)
from test_fix2_stk2 import env as stk_env  # noqa: F401 (a site that loads the Mpesa Settings controller)


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.logger = lambda *a, **kw: None
        custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")  # migrate's
        custom_field.create_custom_fields = lambda fields, update=False: None
        for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
        yield types.SimpleNamespace(frappe=frappe, monkeypatch=monkeypatch)
    finally:
        fake_frappe.uninstall(saved)


def test_every_migrate_switches_both_settings_on(env):
    frappe = env.frappe
    migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
    queries, singles = [], []
    env.monkeypatch.setattr(frappe.db, "sql", lambda query, *a, **kw: queries.append(query), raising=False)
    env.monkeypatch.setattr(frappe.db, "set_single_value", lambda dt, field, value=None, **kw:
                            singles.append((dt, field, value)), raising=False)
    env.monkeypatch.setattr(frappe.db, "columns", {("Mpesa Settings", "submit_b2c_accounting_entries")})
    frappe.db.table("DocType")["M-Pesa Suite Settings"] = {"name": "M-Pesa Suite Settings"}
    migrate.post_entries_at_once()
    assert queries == ["UPDATE `tabMpesa Settings` SET submit_b2c_accounting_entries = 1 "
                       "WHERE IFNULL(submit_b2c_accounting_entries, 0) = 0"]
    assert singles == [("M-Pesa Suite Settings", "submit_statement_entries", 1)]
    assert "post_entries_at_once()" in inspect.getsource(migrate.after_migrate)


def test_an_mpesa_settings_save_keeps_its_entries_posted_at_once(stk_env, monkeypatch):  # noqa: F811
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings")
    doc = module.MpesaSettings()
    for method in ("refresh_security_credential", "validate_api_switches", "warn_shared_shortcode"):
        setattr(doc, method, lambda: None)
    doc.__dict__.update(exchange_rates=[], submit_b2c_accounting_entries=0)
    monkeypatch.setattr(module.MpesaSettings, "meta", types.SimpleNamespace(has_field=lambda field: False),
                        raising=False)
    doc.validate()
    assert doc.submit_b2c_accounting_entries == 1


def test_a_suite_settings_save_keeps_statement_entries_posted_at_once(env):
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.m_pesa_suite_settings.m_pesa_suite_settings")
    doc = module.MPesaSuiteSettings()
    doc.get_password = lambda *a, **kw: "token"
    doc.enforce_ip_allowlist = 0
    doc.get = lambda field, default=None: getattr(doc, field, default)
    doc.submit_statement_entries = 0
    doc.validate()
    assert doc.submit_statement_entries == 1


def test_make_entry_is_refused_to_a_user_who_may_not_submit_it(b2b_env):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1")
    env.denied.add(("Payment Entry", "submit"))
    with pytest.raises(env.frappe.PermissionError, match="Make Entry posts the Payment Entry at once"):
        make(env, "Supplier Advance")
    assert env.inserted == [] and row(env).get("entry_status") != "Draft Entry"


def test_make_entry_posts_the_entry_it_makes(b2b_env):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1")
    answer = make(env, "Supplier Advance")
    assert answer["docstatus"] == 1 and row(env)["entry_status"] == "Entry Made"
    assert b2b_fixture.SETTINGS  # the Mpesa Settings of the request, whatever its old switch said
