"""Round 4.5: one daily "needs attention" alert to the Notify Role (money_out.attention), on the in-memory
site of b2b_fixture.

Covers: the sections from the apps' hooks and those registered in-process, in order and once each; a
section that raises (logged, rolled back to its savepoint, the others still run); a section with
nothing to say; the one alert (its subject names each section and how many lines it has, its email
lists every line, escaped) to the Notify Role, and none on a quiet day; the alert's email body
(party_accounts.alert's details); the daily job in hooks.py and the empty sections hook the features
fill.
"""

import ast
import importlib
import sys
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
SUITE = "M-Pesa Suite Settings"


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        env.hooks = {}
        env.frappe.get_hooks = lambda hook=None, default=None, app_name=None: env.hooks.get(hook, [])
        env.attention = importlib.import_module("mpsa_suite.money_out.attention")
        env.pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        env.sent = []
        monkeypatch.setattr(env.pa, "alert", lambda message, doc, **kw: env.sent.append((message, doc, kw)))
        # sections the hooks name, in a module of their own
        sections = types.ModuleType("r45_sections")
        sections.sheets = lambda: ("Bulk payout sheets", ["MBP-26-11-0003: 64 paid, 3 failed", "  ", None])
        sections.phones = lambda: ("Petty cash phones", [])
        sections.advances = lambda: None

        def broken():
            raise ValueError("no such column")

        sections.broken = broken
        monkeypatch.setitem(sys.modules, "r45_sections", sections)
        yield env


def test_every_section_runs_once_in_order_hooks_first(env):
    env.hooks["mpsa_attention_sections"] = ["r45_sections.sheets", "r45_sections.phones", "r45_sections.sheets"]

    def advances():
        return "Staff advances", ["HR-EAD-1: paid 5 days ago, not settled"]

    env.attention.register(advances)
    env.attention.register(advances)
    assert env.attention.sections() == ["r45_sections.sheets", "r45_sections.phones", advances]
    assert env.attention.collect() == [("Bulk payout sheets", ["MBP-26-11-0003: 64 paid, 3 failed"]),
                                       ("Staff advances", ["HR-EAD-1: paid 5 days ago, not settled"])]


def test_a_section_that_raises_is_logged_and_never_stops_the_others(env):
    env.hooks["mpsa_attention_sections"] = ["r45_sections.broken", "r45_sections.sheets", "r45_sections.nowhere",
                                            "no_such_module.section"]
    assert env.attention.collect() == [("Bulk payout sheets", ["MBP-26-11-0003: 64 paid, 3 failed"])]
    titles = [kw["title"] for _args, kw in env.frappe.errors]
    assert titles == ["M-Pesa daily attention: r45_sections.broken failed",
                      "M-Pesa daily attention: r45_sections.nowhere failed",
                      "M-Pesa daily attention: no_such_module.section failed"]
    # each under a savepoint of its own, rolled back to it when it fails
    savepoints = [e for e in env.log if e[0] == "savepoint"]
    rollbacks = [e for e in env.log if e[0] == "rollback"]
    assert len(savepoints) == 4 and rollbacks == [("rollback", "mpesa_attention_section")] * 3
    env.attention.daily_attention()
    assert len(env.sent) == 1  # the good section still goes out


def test_one_alert_to_the_notify_role_names_each_section_and_lists_every_line(env):
    env.hooks["mpsa_attention_sections"] = ["r45_sections.sheets", "r45_sections.phones"]
    env.attention.register(lambda: ("Staff <advances>", ["HR-EAD-1 & HR-EAD-2: not settled", "HR-EAD-3"]))
    env.attention.daily_attention()
    ((message, doc, kw),) = env.sent
    assert message == "M-Pesa needs attention: Bulk payout sheets (1); Staff <advances> (2)"
    assert (doc.doctype, doc.name) == (SUITE, SUITE)
    assert kw["details"] == ("<p><b>Bulk payout sheets</b></p><ul><li>MBP-26-11-0003: 64 paid, 3 failed</li></ul>"
                             "<p><b>Staff &lt;advances&gt;</b></p><ul><li>HR-EAD-1 &amp; HR-EAD-2: not settled</li>"
                             "<li>HR-EAD-3</li></ul>")


def test_a_quiet_day_sends_nothing(env):
    env.hooks["mpsa_attention_sections"] = ["r45_sections.phones", "r45_sections.advances"]
    env.attention.daily_attention()
    assert env.sent == [] and env.frappe.errors == []
    env.hooks = {}  # no section at all (the features not installed yet)
    env.attention.daily_attention()
    assert env.sent == []


def test_hooks_that_cannot_be_read_leave_the_registered_sections(env):
    def unreadable(hook=None, default=None, app_name=None):
        raise RuntimeError("redis down")

    env.frappe.get_hooks = unreadable
    env.attention.register("r45_sections.sheets")
    assert env.attention.sections() == ["r45_sections.sheets"]
    assert env.frappe.errors[0][1]["title"] == "M-Pesa daily attention: hooks unreadable"
    del env.frappe.get_hooks  # a Frappe without hooks (the unit tests' own)
    assert env.attention.sections() == ["r45_sections.sheets"]


def test_the_alert_carries_the_details_as_its_email(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        made = []
        log = types.ModuleType("frappe.desk.doctype.notification_log.notification_log")
        log.enqueue_create_notification = lambda users, content: made.append((users, content))
        log.make_notification_logs = lambda content, users: made.append((users, dict(content)))
        for name in ("frappe.desk", "frappe.desk.doctype", "frappe.desk.doctype.notification_log"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, log.__name__, log)
        frappe.db.table(SUITE)[SUITE] = {"notify_role": "Accounts Manager"}
        frappe.db.table("Has Role")["HR-1"] = {"name": "HR-1", "parent": "acc@example.com", "role": "Accounts Manager",
                                               "parenttype": "User"}
        pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        doc = fake_frappe._dict(doctype=SUITE, name=SUITE)
        pa.alert("M-Pesa needs attention: X (1)", doc, details="<p>X</p>")
        pa.alert("A plain alert", doc)
        assert made == [
            (["acc@example.com"], {"type": "Alert", "subject": "M-Pesa needs attention: X (1)", "document_type": SUITE,
                                   "document_name": SUITE, "email_content": "<p>X</p>"}),
            (["acc@example.com"], {"type": "Alert", "subject": "A plain alert", "document_type": SUITE,
                                   "document_name": SUITE}),  # as before: no email body of its own
        ]
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- hooks.py
def _hooks() -> dict:
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {n.targets[0].id: n.value for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)}


def test_the_daily_job_and_the_sections_hook_are_in_hooks_py():
    hooks = _hooks()
    scheduler = ast.literal_eval(hooks["scheduler_events"])
    assert scheduler["daily"] == ["mpsa_suite.money_out.attention.daily_attention"]
    sections = ast.literal_eval(hooks["mpsa_attention_sections"])  # each feature adds its own
    assert all(isinstance(path, str) and path.startswith("mpsa_suite.money_out.") for path in sections)
    module, _, function = scheduler["daily"][0].rpartition(".")
    assert (APP / "money_out" / "attention.py").exists() and module.endswith("attention") and function
