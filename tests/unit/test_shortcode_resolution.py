"""One shortcode can collect and pay out (Safaricom Business One Account). When several Mpesa
Settings records share a number, money received is booked with a collecting record, never a
B2C-only one, and the choice never depends on database order."""

import importlib

import pytest

import fake_frappe

B2C = "MPesa B2C (Business to Customer)"
C2B = "MPesa C2B (Customer to Business)"


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        yield fake_frappe._dict(frappe=frappe, common=importlib.import_module("frappe_mpsa_payments.money_in.common"))
    finally:
        fake_frappe.uninstall(saved)


def add(env, name, api_type, shortcode="", till=""):
    env.frappe.db.table("Mpesa Settings")[name] = {"name": name, "api_type": api_type,
                                                  "business_shortcode": shortcode, "till_number": till}


def test_collecting_record_wins_over_older_b2c_record(env):
    add(env, "Payouts 208911", B2C, "208911")
    add(env, "Paybill 208911", C2B, "208911")
    assert env.common.settings_for_shortcode("208911") == "Paybill 208911"


def test_single_record_for_everything(env):
    add(env, "Business One 208911", "MPesa Express", "208911")
    assert env.common.settings_for_shortcode(" 208911 ") == "Business One 208911"


def test_till_and_stray_spaces(env):
    add(env, "Till", C2B, "4134019", " 5123456 ")
    assert env.common.settings_for_shortcode("5123456") == "Till"
    assert env.common.settings_for_shortcode("999999") is None
    assert env.common.settings_for_shortcode("") is None


def test_only_b2c_record_is_still_found(env):
    add(env, "Payouts", B2C, "600000")
    assert env.common.settings_for_shortcode("600000") == "Payouts"


def test_records_sharing_shortcode(env):
    add(env, "A", C2B, "208911")
    add(env, "B", B2C, "", "208911")
    add(env, "C", C2B, "4139683")
    assert env.common.records_sharing_shortcode("A", ["208911", ""]) == ["B"]
    assert env.common.records_sharing_shortcode("C", ["4139683"]) == []
