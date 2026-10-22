"""mpsa_suite refuses every uninstall of itself unless site_config allows it (RENAME_PLAN.md R2).

The old app's own guard (R3: refuse unless mpsa_suite has taken the module over) stays in the
old app only. Under the new name its premise would invert, so mpsa_suite has its own rule:
refuse whatever the row counts, installed apps or module owners, unless mpsa_allow_uninstall.
"""

import ast
import importlib
from pathlib import Path

import fake_frappe
import pytest

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"


class NoDB:
    """The guard must decide without looking at the site: any query is a failure."""

    def __getattr__(self, name):
        raise AssertionError(f"the R2 guard must not read the database (frappe.db.{name})")


@pytest.fixture
def guard():
    frappe, saved = fake_frappe.install()
    try:
        module = importlib.import_module("mpsa_suite.uninstall_guard")
        frappe.conf = fake_frappe._dict()
        frappe.db = NoDB()
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_mpsa_payments", "mpsa_suite"]
        yield module, frappe
    finally:
        fake_frappe.uninstall(saved)


def test_uninstall_is_refused(guard):
    module, _ = guard
    with pytest.raises(Exception, match="Uninstalling mpsa_suite would drop every M-Pesa table"):
        module.before_uninstall()


@pytest.mark.parametrize(
    "apps",
    [
        ["frappe", "erpnext", "mpsa_suite"],  # after the hand-over
        ["frappe", "erpnext"],  # mpsa_suite missing from installed_apps (half-failed install, restore)
        ["frappe", "erpnext", "frappe_mpsa_payments"],
    ],
)
def test_refused_whatever_the_installed_apps(guard, apps):
    module, frappe = guard
    frappe.get_installed_apps = lambda: apps
    with pytest.raises(Exception, match="would drop every M-Pesa table"):
        module.before_uninstall()


@pytest.mark.parametrize("value", [None, 0, "0", "", "no", "yes please", 2])
def test_only_an_explicit_flag_unlocks_it(guard, value):
    module, frappe = guard
    frappe.conf = fake_frappe._dict(mpsa_allow_uninstall=value)
    with pytest.raises(Exception, match="mpsa_allow_uninstall"):
        module.before_uninstall()


@pytest.mark.parametrize("value", [1, "1", True, "true"])
def test_site_config_flag_allows_a_deliberate_uninstall(guard, value):
    module, frappe = guard
    frappe.conf = fake_frappe._dict(mpsa_allow_uninstall=value)
    module.before_uninstall()


def test_the_old_apps_flag_does_not_unlock_mpsa_suite(guard):
    module, frappe = guard
    frappe.conf = fake_frappe._dict(mpsa_allow_old_uninstall=1)
    with pytest.raises(Exception, match="would drop every M-Pesa table"):
        module.before_uninstall()


def test_hand_over_hook_is_the_second_check(guard):
    _, frappe = guard
    handover = importlib.import_module("mpsa_suite.handover")
    with pytest.raises(Exception, match="would drop every M-Pesa table"):
        handover.before_app_uninstall("mpsa_suite")
    frappe.conf = fake_frappe._dict(mpsa_allow_uninstall=1)
    handover.before_app_uninstall("mpsa_suite")  # allowed: returns without touching the site
    handover.before_app_uninstall("erpnext")  # other apps: nothing to do


def _hooks():
    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in ("before_uninstall", "before_app_uninstall", "after_sync")
    }


def test_hooks_wire_the_guard_as_the_apps_own_before_uninstall():
    hooks = _hooks()
    # an app's own before_uninstall runs even when it is missing from installed_apps
    assert hooks["before_uninstall"] == "mpsa_suite.uninstall_guard.before_uninstall"
    assert hooks["before_app_uninstall"] == "mpsa_suite.handover.before_app_uninstall"


def test_the_old_apps_guard_is_not_shipped_under_the_new_name():
    source = (APP / "uninstall_guard.py").read_text(encoding="utf-8")
    assert "mpsa_allow_old_uninstall" not in source
    assert "get_installed_apps" not in source
