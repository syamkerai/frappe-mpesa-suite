"""Per-API switches on Mpesa Settings and the Party A each initiator API sends."""

import pytest

from mpsa_suite.daraja import apis

EXPRESS, C2B, B2C = apis.LEGACY_EXPRESS, apis.LEGACY_C2B, apis.LEGACY_B2C


def on(flags):
    return sorted(api for api, value in flags.items() if value)


@pytest.mark.parametrize(
    "api_type,expected",
    [
        (EXPRESS, [apis.STK]),
        (C2B, [apis.C2B]),
        (B2C, [apis.B2C]),
        (None, [apis.STK]),  # upstream's default was MPesa Express
        ("", [apis.STK]),
    ],
)
def test_legacy_api_type_maps_to_one_switch(api_type, expected):
    assert on(apis.switches_from_legacy(api_type)) == sorted(expected)


def test_legacy_keeps_every_api_the_record_was_used_for():
    # Upstream let any record do anything: an Express record that also had C2B URLs registered
    # and paid B2C from its Mode of Payment keeps all three.
    flags = apis.switches_from_legacy(EXPRESS, registered_c2b=True, sent_b2c=True)
    assert on(flags) == sorted([apis.STK, apis.C2B, apis.B2C])
    assert flags[apis.B2B] == 0 and flags[apis.REVERSAL] == 0


def test_b2c_record_that_sent_stk_keeps_stk():
    # Astral's 4134019: API Type "MPesa B2C", yet it sent STK prompts and has C2B URLs.
    flags = apis.switches_from_legacy(B2C, registered_c2b=True, sent_stk=True, sent_b2c=True)
    assert on(flags) == sorted([apis.B2C, apis.C2B, apis.STK])


def test_legacy_money_out_only_when_already_used():
    flags = apis.switches_from_legacy(C2B, sent_b2b=True, sent_reversal=True)
    assert on(flags) == sorted([apis.C2B, apis.B2B, apis.REVERSAL])


def test_every_switch_starts_from_a_value():
    assert set(apis.switches_from_legacy(B2C)) == set(apis.ALL)


@pytest.mark.parametrize("value,expected", [(1, True), ("1", True), (0, False), (None, False), ("", False), ("x", False)])
def test_enabled_reads_check_values(value, expected):
    assert apis.enabled({apis.B2C: value}, apis.B2C) is expected


def test_collects_means_c2b_or_stk():
    assert apis.collects({apis.C2B: 1})
    assert apis.collects({apis.STK: 1})
    assert not apis.collects({apis.B2C: 1, apis.B2B: 1, apis.REVERSAL: 1})


def test_refusal_names_the_record_and_switch():
    assert apis.refusal({apis.B2B: 1}, apis.B2B) is None
    reason = apis.refusal({"name": "Paybill 208911"}, apis.REVERSAL)
    assert "Paybill 208911" in reason and "Reversals" in reason
    assert "Till 1" in apis.refusal({}, apis.STK, "Till 1")


def test_describe():
    assert apis.describe({apis.C2B: 1, apis.B2C: 1}) == "Receive C2B (Paybill / Till), B2C Payouts"
    assert apis.describe({}) == "no APIs"


class Doc:
    """Stands in for a Frappe Document: values through .get()."""

    def __init__(self, **values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({"business_shortcode": "208911", "till_number": "5123456", "sandbox": 0}, "208911"),  # live: never the till
        ({"business_shortcode": "174379", "till_number": " 600999 ", "sandbox": 1}, "600999"),  # sandbox: test shortcode
        ({"business_shortcode": "600999", "till_number": "", "sandbox": 1}, "600999"),  # sandbox without till
        (Doc(business_shortcode="174379", till_number="600984", sandbox="1"), "600984"),
        ({}, ""),
    ],
)
def test_initiator_party_a(settings, expected):
    assert apis.initiator_party_a(settings) == expected
