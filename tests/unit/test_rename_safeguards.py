"""Safeguards of the rename frappe_mpsa_payments -> mpsa_suite (RENAME_PLAN.md section 4.2).

R1 after_sync re-homes at install, R4 the old-path lookup table, R5 Payment Entry STK fields on
every migrate, R6 default_app re-pointed, R8 patches that already ran under the old name do
nothing, R9 CI runs on mpsa-suite. R2 is in test_uninstall_guard.py. Also: before_install
rebuilds a stale module map (Frappe 15), and no bench test sits where Frappe 15 skips it.
"""

import ast
import importlib
import json
import re
import sys
import types
from pathlib import Path

import fake_frappe
import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
OLD_APP, NEW_APP = "frappe_mpsa_payments", "mpsa_suite"
OLD_MODULE, NEW_MODULE = "Frappe Mpsa Payments", "Mpsa Suite"
SNAPSHOT = json.loads((Path(__file__).with_name("rename_whitelist_at_c.json")).read_text(encoding="utf-8"))


def _hooks_assignments() -> dict:
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }


def _hook(name):
    return ast.literal_eval(_hooks_assignments()[name])


# ---------------------------------------------------------------------------------------------
# A small site: tables of rows, Singles values and which DocTypes / columns exist
# ---------------------------------------------------------------------------------------------


class Site:
    UPDATE = re.compile(r"UPDATE `tab(?P<dt>[^`]+)` SET `(?P<f>[^`]+)` = %s WHERE `(?P=f)` = %s$")
    SINGLES_COUNT = "SELECT COUNT(*) FROM `tabSingles` WHERE doctype = %s AND field = %s AND value = %s"
    SINGLES_UPDATE = "UPDATE `tabSingles` SET value = %s WHERE doctype = %s AND field = %s AND value = %s"

    def __init__(self):
        self.rows = {}  # doctype -> [dict]
        self.columns = {}  # doctype -> set of columns (non-single DocTypes with a table)
        self.singles = {}  # (doctype, field) -> value
        self.single_doctypes = {"System Settings"}
        self.modules = {}  # Module Def name -> app_name
        self.docfields = []  # Link-to-Module-Def fields: (parent, fieldname)
        self.patch_log = []  # (patch, skipped)
        self.cleared = 0
        self.committed = 0

    def add_table(self, doctype, *columns, rows=()):
        self.columns[doctype] = set(columns) | {"name"}
        self.rows[doctype] = [dict(r) for r in rows]

    # frappe.db
    def exists(self, doctype, filters):
        if doctype == "Module Def":
            return filters in self.modules
        if doctype == "DocType":
            return filters in self.columns or filters in self.single_doctypes
        if doctype == "Patch Log":
            return (filters["patch"], filters["skipped"]) in self.patch_log
        raise AssertionError(f"exists({doctype!r})")

    def table_exists(self, doctype):
        return doctype in self.columns

    def has_column(self, doctype, column):
        return column in self.columns.get(doctype, ())

    def count(self, doctype, filters):
        ((field, value),) = filters.items()
        return sum(1 for r in self.rows[doctype] if r.get(field) == value)

    def sql(self, query, values=()):
        query = " ".join(query.split())
        if match := self.UPDATE.match(query):
            new, old = values
            for r in self.rows[match["dt"]]:
                if r.get(match["f"]) == old:
                    r[match["f"]] = new
            return ()
        if query == self.SINGLES_COUNT:
            doctype, field, value = values
            return ((int(self.singles.get((doctype, field)) == value),),)
        if query == self.SINGLES_UPDATE:
            new, doctype, field, old = values
            if self.singles.get((doctype, field)) == old:
                self.singles[(doctype, field)] = new
            return ()
        raise AssertionError(f"unexpected SQL: {query}")

    def set_value(self, doctype, name, field, value, update_modified=True):
        assert (doctype, field) == ("Module Def", "app_name")
        self.modules[name] = value

    def commit(self):
        self.committed += 1

    # frappe
    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kw):
        if doctype == "DocField":
            assert filters == {"fieldtype": "Link", "options": "Module Def"}
            return [fake_frappe._dict(parent=p, fieldname=f) for p, f in self.docfields]
        if doctype == "Module Def":
            names = [m for m, app in self.modules.items() if app == filters["app_name"]]
            return names if pluck else [fake_frappe._dict(name=m) for m in names]
        raise AssertionError(f"get_all({doctype!r})")

    def get_meta(self, doctype):
        return fake_frappe._dict(issingle=doctype in self.single_doctypes)


@pytest.fixture
def site():
    frappe, saved = fake_frappe.install()
    try:
        s = Site()
        frappe.db = s
        frappe.get_all = s.get_all
        frappe.get_meta = s.get_meta

        def clear_cache(**kw):
            s.cleared += 1

        frappe.clear_cache = clear_cache
        handover = importlib.import_module("mpsa_suite.handover")
        yield s, handover
    finally:
        fake_frappe.uninstall(saved)


def _astral_like(s: Site):
    """Both apps installed; the old module still holds site-made records."""
    s.modules = {OLD_MODULE: OLD_APP, NEW_MODULE: NEW_APP}
    s.docfields = [("Report", "module"), ("Server Script", "module"), ("Custom Field", "module"),
                   ("Workspace", "module")]
    s.add_table("DocType", "module", rows=[
        {"name": "Mpesa Settings", "module": NEW_MODULE},
        {"name": "Astral Casual Payee", "module": OLD_MODULE},  # custom DocType made on the site
    ])
    s.add_table("Report", "module", rows=[{"name": "Payouts by Branch", "module": OLD_MODULE}])
    s.add_table("Server Script", "module", rows=[{"name": "AIL Feed", "module": OLD_MODULE},
                                                 {"name": "Other", "module": "Accounts"}])
    s.add_table("Custom Field", "module", rows=[{"name": "Bank-x", "module": OLD_MODULE}])
    s.add_table("Workspace", "module", "app", rows=[{"name": "M-Pesa", "module": NEW_MODULE, "app": OLD_APP}])
    s.add_table("User", "default_app", rows=[{"name": "a@x", "default_app": OLD_APP},
                                             {"name": "b@x", "default_app": "erpnext"},
                                             {"name": "c@x", "default_app": None}])
    s.singles[("System Settings", "default_app")] = OLD_APP


# ---------------------------------------------------------------------------------------------
# R1: after_sync re-homes at the end of install-app
# ---------------------------------------------------------------------------------------------


def test_r1_after_sync_hook_is_wired():
    assert _hook("after_sync") == "mpsa_suite.handover.after_sync"


def test_r1_install_leaves_nothing_in_the_old_module(site):
    s, handover = site
    _astral_like(s)
    handover.after_sync()
    assert all(r.get("module") != OLD_MODULE for rows in s.rows.values() for r in rows)
    assert {r["name"]: r["module"] for r in s.rows["DocType"]}["Astral Casual Payee"] == NEW_MODULE
    assert s.rows["Server Script"][1]["module"] == "Accounts"  # other modules untouched
    assert s.cleared == 1
    assert s.modules[OLD_MODULE] == OLD_APP  # Module Defs change hands only at the uninstall


def test_r1_rehome_waits_for_the_new_module_and_is_idempotent(site):
    s, handover = site
    _astral_like(s)
    del s.modules[NEW_MODULE]
    assert handover.rehome() == {}
    assert s.rows["Report"][0]["module"] == OLD_MODULE
    s.modules[NEW_MODULE] = NEW_APP
    first = handover.rehome()
    assert first["DocType.module"] == 1 and first["Report.module"] == 1
    assert handover.rehome() == {}  # second run: nothing left


def test_r1_uninstall_of_the_old_app_keeps_non_empty_modules(site):
    s, handover = site
    _astral_like(s)
    s.modules["Stanbic"] = OLD_APP
    s.add_table("Print Format", "module", rows=[{"name": "Stanbic Slip", "module": "Stanbic"}])
    s.docfields.append(("Print Format", "module"))
    s.modules["Empty Old"] = OLD_APP
    handover.before_app_uninstall(OLD_APP)
    assert s.modules["Stanbic"] == NEW_APP  # kept for mpsa_suite, not deleted
    assert s.modules["Empty Old"] == OLD_APP  # empty: Frappe may delete it
    assert s.modules[OLD_MODULE] == OLD_APP  # emptied, so its deletion removes nothing
    assert s.committed == 1


# ---------------------------------------------------------------------------------------------
# R6: default_app follows the app
# ---------------------------------------------------------------------------------------------


def test_r6_default_app_moves_to_mpsa_suite(site):
    s, handover = site
    _astral_like(s)
    moved = handover.rehome()
    assert [u["default_app"] for u in s.rows["User"]] == [NEW_APP, "erpnext", None]
    assert s.singles[("System Settings", "default_app")] == NEW_APP
    assert moved["User.default_app"] == 1 and moved["System Settings.default_app"] == 1
    assert moved["Workspace.app"] == 1 and s.rows["Workspace"][0]["app"] == NEW_APP


def test_r6_other_default_apps_are_left_alone(site):
    s, handover = site
    _astral_like(s)
    s.singles[("System Settings", "default_app")] = "erpnext"
    handover.rehome()
    assert s.singles[("System Settings", "default_app")] == "erpnext"


def test_r6_site_without_default_app_column(site):
    s, handover = site
    _astral_like(s)
    s.columns["User"].discard("default_app")
    moved = handover.rehome()
    assert "User.default_app" not in moved
    assert s.rows["User"][0]["default_app"] == OLD_APP


# ---------------------------------------------------------------------------------------------
# R8: a patch that already ran under the old name does nothing
# ---------------------------------------------------------------------------------------------


def _registered_patches() -> list:
    lines = (APP / "patches.txt").read_text(encoding="utf-8").splitlines()
    return [line.split()[0] for line in lines if line.strip() and not line.startswith(("#", "["))]


def test_r8_twin_name(site):
    _, handover = site
    assert handover.old_patch_name("mpsa_suite.mpsa_suite.patches.mpesa_settings_api_switches") == (
        "frappe_mpsa_payments.frappe_mpsa_payments.patches.mpesa_settings_api_switches"
    )


def test_r8_twin_ran_under_old_name_for_one_patch_only(site):
    s, handover = site
    s.patch_log = [("frappe_mpsa_payments.frappe_mpsa_payments.patches.relink_b2c_reference_rows", 0)]
    assert handover.ran_under_old_name("mpsa_suite.mpsa_suite.patches.relink_b2c_reference_rows")
    assert not handover.ran_under_old_name("mpsa_suite.mpsa_suite.patches.payout_lines_from_upgrade_date")


@pytest.mark.parametrize(
    "log, expected",
    [
        ([("frappe_mpsa_payments.frappe_mpsa_payments.patches.x", 0)], True),  # ran (or install-app marked it)
        ([("frappe_mpsa_payments.frappe_mpsa_payments.patches.x", 1)], False),  # failed and skipped
        ([("mpsa_suite.mpsa_suite.patches.x", 0)], False),  # only its own name
        ([], False),
    ],
)
def test_r8_ran_under_old_name(site, log, expected):
    s, handover = site
    s.patch_log = log
    assert handover.ran_under_old_name("mpsa_suite.mpsa_suite.patches.x") is expected


def test_r8_registered_patches_are_the_eight_of_release_c_and_round_41s():
    # the eight of Release C, round 4.1's claim_payee_name_verdict, the STK draft-order retry (Nov 2026), then
    # round 4.5's known M-Pesa names from the payouts made before
    assert len(_registered_patches()) == 11
    assert _registered_patches()[-3] == "mpsa_suite.mpsa_suite.patches.claim_payee_name_verdict"
    assert _registered_patches()[-2] == "mpsa_suite.mpsa_suite.patches.retry_stk_prompts_refused_on_draft_orders"
    assert _registered_patches()[-1] == "mpsa_suite.mpsa_suite.patches.known_mpesa_names"
    assert all(p.startswith("mpsa_suite.mpsa_suite.patches.") for p in _registered_patches())


@pytest.mark.parametrize("patch", _registered_patches())
def test_r8_every_registered_patch_returns_first_when_its_twin_ran(patch):
    path = ROOT / (patch.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    (execute,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "execute"]
    first = execute.body[0]
    assert isinstance(first, ast.If) and ast.unparse(first.test) == "ran_under_old_name(__name__)"
    assert isinstance(first.body[0], ast.Return)
    assert any(
        isinstance(n, ast.ImportFrom) and n.module == "mpsa_suite.handover"
        and [a.name for a in n.names] == ["ran_under_old_name"]
        for n in tree.body
    )


def test_r8_twin_ran_so_mpesa_settings_switches_are_not_rewritten(site, monkeypatch):
    s, _ = site
    patch = importlib.import_module("mpsa_suite.mpsa_suite.patches.mpesa_settings_api_switches")

    def touched(*a, **kw):
        raise AssertionError("the patch did work although its twin already ran")

    monkeypatch.setattr(patch.frappe, "get_all", touched)
    monkeypatch.setattr(s, "has_column", touched)
    s.patch_log = [("frappe_mpsa_payments.frappe_mpsa_payments.patches.mpesa_settings_api_switches", 0)]
    patch.execute()  # returns at once
    s.patch_log = []
    with pytest.raises(AssertionError, match="did work"):
        patch.execute()  # without the twin it does its work


# ---------------------------------------------------------------------------------------------
# R4: every old whitelisted path keeps working through override_whitelisted_methods
# ---------------------------------------------------------------------------------------------


def _override_map() -> dict:
    return _hook("override_whitelisted_methods")


def _new_name(old: str) -> str:
    return ".".join(NEW_APP if part == OLD_APP else part for part in old.split("."))


def _whitelist_flags(func) -> tuple:
    whitelisted, guest = False, False
    for dec in func.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
        if name == "whitelist":
            whitelisted = True
            if isinstance(dec, ast.Call):
                guest = guest or any(kw.arg == "allow_guest" and ast.literal_eval(kw.value) for kw in dec.keywords)
    return whitelisted, guest


def _resolve(dotted: str):
    """The top-level function a dotted path names, from source (no import of Frappe needed)."""
    module, func = dotted.rsplit(".", 1)
    base = ROOT / module.replace(".", "/")
    path = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
    assert path.is_file(), f"{dotted}: no module {module}"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func]
    assert found, f"{dotted}: {module} has no top-level function {func}"
    return found[-1]


def test_r4_snapshot_is_release_c():
    assert SNAPSHOT["release"] == "02d94a0"
    assert len(SNAPSHOT["whitelisted"]) == 70 and sum(SNAPSHOT["whitelisted"].values()) == 29


def test_r4_every_whitelisted_function_of_release_c_has_an_entry():
    missing = sorted(set(SNAPSHOT["whitelisted"]) - set(_override_map()))
    assert missing == []


def test_r4_no_entry_without_an_old_function():
    assert sorted(set(_override_map()) - set(SNAPSHOT["whitelisted"])) == []


def test_r4_each_old_path_maps_to_the_same_function_under_the_new_name():
    wrong = {old: new for old, new in _override_map().items() if new != _new_name(old)}
    assert wrong == {}


@pytest.mark.parametrize("old", sorted(SNAPSHOT["whitelisted"]))
def test_r4_target_exists_is_whitelisted_and_keeps_the_guest_setting(old):
    new = _override_map()[old]
    whitelisted, guest = _whitelist_flags(_resolve(new))
    assert whitelisted, f"{new} is not whitelisted: the old path would return 403"
    assert guest == SNAPSHOT["whitelisted"][old], f"{new}: allow_guest differs from {old} at release C"


def test_r4_the_safaricom_paths_are_guest():
    guest_paths = {old for old, guest in SNAPSHOT["whitelisted"].items() if guest}
    for name in ("confirmation", "validation", "stk_push_callback", "pull_registration_result",
                 "balance_query_callback", "handle_queue_timeout", "handle_transaction_status_result"):
        assert f"{OLD_APP}.{OLD_APP}.api.m_pesa_api.{name}" in guest_paths
    assert f"{OLD_APP}.{OLD_APP}.doctype.mpesa_settings.mpesa_settings.pull_transaction_callback" in guest_paths


# ---------------------------------------------------------------------------------------------
# R5: the Payment Entry STK fields come from after_migrate, filed under MODULE
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def migrate_env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        created = []
        leaf = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
        leaf.create_custom_fields = lambda fields, **kw: created.append((fields, kw))
        for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, leaf.__name__, leaf)
        for doctype in ("Payment Entry", "Payment Gateway", "Payment Gateway Account"):
            frappe.db.table("DocType")[doctype] = {"name": doctype}
        yield importlib.import_module("mpsa_suite.mpsa_suite.migrate"), frappe, created
    finally:
        fake_frappe.uninstall(saved)


def test_r5_after_migrate_creates_the_payment_entry_fields():
    tree = ast.parse((APP / "mpsa_suite" / "migrate.py").read_text(encoding="utf-8"))
    (after_migrate,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "after_migrate"]
    (erpnext_block,) = [n for n in after_migrate.body if isinstance(n, ast.If) and "erpnext" in ast.unparse(n.test)]
    calls = {ast.unparse(n.func) for n in ast.walk(erpnext_block) if isinstance(n, ast.Call)}
    assert "create_payment_entry_mpesa_fields" in calls


def test_r5_the_six_fields_are_filed_under_the_module(migrate_env):
    migrate, _, created = migrate_env
    migrate.create_payment_entry_mpesa_fields()
    ((fields, kw),) = created
    assert kw == {"update": True}
    rows = fields["Payment Entry"]
    assert [r["fieldname"] for r in rows] == [
        "custom_mpesa_section", "custom_payment_gateway_account", "custom_payment_gateway",
        "custom_mpesa_column_break", "custom_mpesa_phone_number", "custom_mpesa_receipt_number",
    ]
    assert {r["module"] for r in rows} == {migrate.MODULE} == {NEW_MODULE}


def test_r5_fields_wait_for_the_payments_app(migrate_env):
    migrate, frappe, created = migrate_env
    del frappe.db.table("DocType")["Payment Gateway"]
    migrate.create_payment_entry_mpesa_fields()
    assert created == []


def test_r5_patch_delegates_to_migrate():
    source = (APP / "mpsa_suite" / "patches" / "payment_entry_custom_fields.py").read_text(encoding="utf-8")
    assert "from ..migrate import create_payment_entry_mpesa_fields" in source
    assert '"Mpsa Suite"' not in source and "fieldname" not in source


# ---------------------------------------------------------------------------------------------
# before_install: a module map cached before `bench get-app` (Frappe 15) is rebuilt
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def install_env():
    frappe, saved = fake_frappe.install()
    try:
        calls = []
        frappe.cache = types.SimpleNamespace(delete_value=lambda key: calls.append(("delete_value", key)))

        def setup_module_map(include_all_apps=True):
            calls.append(("setup_module_map", include_all_apps))
            frappe.local.app_modules = {"frappe": ["core"], OLD_APP: [OLD_APP], NEW_APP: [NEW_APP]}

        frappe.setup_module_map = setup_module_map
        yield importlib.import_module("mpsa_suite.install"), frappe, calls
    finally:
        fake_frappe.uninstall(saved)


def test_before_install_hook_is_wired():
    assert _hook("before_install") == "mpsa_suite.install.before_install"


@pytest.mark.parametrize("cached", [{"frappe": ["core"], OLD_APP: [OLD_APP]}, None])
def test_before_install_rebuilds_a_module_map_without_mpsa_suite(install_env, cached):
    install, frappe, calls = install_env
    frappe.local.app_modules = cached
    assert install.before_install() is None  # False would make install-app stop
    assert calls == [("delete_value", "app_modules"), ("setup_module_map", True)]
    assert NEW_APP in frappe.local.app_modules


def test_before_install_leaves_a_current_module_map_alone(install_env):
    install, frappe, calls = install_env
    frappe.local.app_modules = {"frappe": ["core"], NEW_APP: [NEW_APP]}
    install.before_install()
    assert calls == []


def test_no_bench_test_in_the_app_folder_itself():
    # Frappe 15's run-tests --app imports a test file in the app folder itself as the bare
    # package (test_runner._add_test, relative_path == "."), so its tests would never run.
    assert sorted(p.name for p in APP.glob("test_*.py")) == []
    assert (APP / "tests" / "__init__.py").is_file()
    assert (APP / "tests" / "test_rename_lookup.py").is_file()


# ---------------------------------------------------------------------------------------------
# R9 and Frappe Cloud: CI runs on mpsa-suite; the branch ships no frappe_mpsa_payments app
# ---------------------------------------------------------------------------------------------


def test_r9_ci_runs_on_pushes_to_mpsa_suite():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    push = ci.split("push:", 1)[1].split("pull_request:", 1)[0]
    assert "- mpsa-suite" in push
    # The linters run on pull requests only, as on astral-suite: the code did not pass them before
    # the rename, and a red run on every push would hide a failing test run.
    linter = (ROOT / ".github" / "workflows" / "linter.yml").read_text(encoding="utf-8")
    assert "push:" not in linter


def test_only_app_folder_is_mpsa_suite():
    # Frappe Cloud names an app after the first top-level folder with hooks.py; an old-named
    # folder would sort first and be offered as an update of the live app.
    apps = sorted(p.parent.name for p in ROOT.glob("*/hooks.py"))
    assert apps == [NEW_APP]
    assert not (ROOT / OLD_APP).exists()
    assert _hook("app_name") == NEW_APP and _hook("app_title") == "Mpsa Suite"
