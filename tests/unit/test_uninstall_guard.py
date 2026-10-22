"""The old app refuses an uninstall that would drop the M-Pesa tables (RENAME_PLAN.md R3)."""

import importlib

import fake_frappe
import pytest


class FakeDB:
    def __init__(self, owner=None):
        self.owner = owner

    def get_value(self, doctype, name, field):
        assert (doctype, name, field) == ("Module Def", "Mpsa Suite", "app_name")
        return self.owner


@pytest.fixture
def guard():
    frappe, saved = fake_frappe.install()
    try:
        module = importlib.import_module("frappe_mpsa_payments.uninstall_guard")
        frappe.conf = fake_frappe._dict()
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "frappe_mpsa_payments"]
        frappe.db = FakeDB()
        yield module, frappe
    finally:
        fake_frappe.uninstall(saved)


def test_uninstall_is_refused_on_its_own(guard):
    module, _ = guard
    with pytest.raises(Exception, match="would drop every M-Pesa table"):
        module.before_uninstall()


def test_refused_when_mpsa_suite_is_installed_but_does_not_own_the_module(guard):
    module, frappe = guard
    frappe.get_installed_apps = lambda: ["frappe", "frappe_mpsa_payments", "mpsa_suite"]
    with pytest.raises(Exception, match="would drop every M-Pesa table"):
        module.before_uninstall()


def test_refused_when_the_module_exists_but_mpsa_suite_is_not_installed(guard):
    module, frappe = guard
    frappe.db = FakeDB(owner="mpsa_suite")
    with pytest.raises(Exception, match="would drop every M-Pesa table"):
        module.before_uninstall()


def test_hand_over_is_allowed(guard):
    module, frappe = guard
    frappe.get_installed_apps = lambda: ["frappe", "frappe_mpsa_payments", "mpsa_suite"]
    frappe.db = FakeDB(owner="mpsa_suite")
    module.before_uninstall()


def test_site_config_flag_allows_a_deliberate_uninstall(guard):
    module, frappe = guard
    frappe.conf = fake_frappe._dict(mpsa_allow_old_uninstall=1)
    module.before_uninstall()
