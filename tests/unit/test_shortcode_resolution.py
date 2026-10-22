"""One shortcode can collect and pay out (Safaricom Business One Account). When several Mpesa
Settings records share a number, money received is booked with a collecting record, never a
payout-only one, and the choice never depends on database order."""

import importlib

import pytest

import fake_frappe

# Per-API switches on Mpesa Settings (daraja.apis)
B2C = {"use_b2c": 1}
C2B = {"use_c2b": 1}
ALL = {"use_c2b": 1, "use_stk": 1, "use_b2c": 1, "use_b2b": 1}


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        yield fake_frappe._dict(frappe=frappe, common=importlib.import_module("mpsa_suite.money_in.common"))
    finally:
        fake_frappe.uninstall(saved)


def add(env, name, switches, shortcode="", till=""):
    env.frappe.db.table("Mpesa Settings")[name] = {"name": name, "business_shortcode": shortcode,
                                                  "till_number": till, **switches}


def test_collecting_record_wins_over_older_b2c_record(env):
    add(env, "Payouts 208911", B2C, "208911")
    add(env, "Paybill 208911", C2B, "208911")
    assert env.common.settings_for_shortcode("208911") == "Paybill 208911"


def test_single_record_for_everything(env):
    add(env, "Business One 208911", ALL, "208911")
    assert env.common.settings_for_shortcode(" 208911 ") == "Business One 208911"


def test_till_and_stray_spaces(env):
    add(env, "Till", C2B, "4134019", " 5123456 ")
    assert env.common.settings_for_shortcode("5123456") == "Till"
    assert env.common.settings_for_shortcode("999999") is None
    assert env.common.settings_for_shortcode("") is None


def test_stk_only_record_counts_as_collecting(env):
    add(env, "Payouts 208911", B2C, "208911")
    add(env, "Express 208911", {"use_stk": 1}, "208911")
    assert env.common.settings_for_shortcode("208911") == "Express 208911"


def test_only_b2c_record_is_still_found(env):
    add(env, "Payouts", B2C, "600000")
    assert env.common.settings_for_shortcode("600000") == "Payouts"


def test_records_sharing_shortcode(env):
    add(env, "A", C2B, "208911")
    add(env, "B", B2C, "", "208911")
    add(env, "C", C2B, "4139683")
    assert env.common.records_sharing_shortcode("A", ["208911", ""]) == ["B"]
    assert env.common.records_sharing_shortcode("C", ["4139683"]) == []
