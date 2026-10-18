import pytest

from frappe_mpsa_payments.daraja.guard import (
    DEFAULT_SAFARICOM_IPS,
    ip_allowed,
    parse_allowlist,
    redact_headers,
    stk_amount,
    token_matches,
    truncate_reference,
)


@pytest.mark.parametrize(
    "ip",
    ["196.201.214.200", "196.201.214.206", "196.201.213.114", "196.201.213.44", "196.201.212.127", "196.201.212.69"],
)
def test_published_safaricom_ips_allowed(ip):
    assert ip_allowed(ip, DEFAULT_SAFARICOM_IPS)


@pytest.mark.parametrize("ip", ["8.8.8.8", "196.201.215.1", "", None, "not-an-ip", "::1"])
def test_other_ips_rejected(ip):
    assert not ip_allowed(ip, DEFAULT_SAFARICOM_IPS)


def test_allowlist_parsing_ignores_junk_and_comments():
    nets = parse_allowlist("10.0.0.1 # office\n\nbogus\n192.168.0.0/16, 172.16.0.1")
    assert len(nets) == 3
    assert ip_allowed("192.168.4.4", "192.168.0.0/16")
    assert not ip_allowed("10.0.0.1", "")


def test_token_matches_is_strict():
    assert token_matches("abc123", "abc123")
    assert not token_matches("abc124", "abc123")
    assert not token_matches("", "abc123")
    assert not token_matches("abc123", None)
    assert not token_matches(None, None)


def test_redact_headers():
    out = redact_headers({"Authorization": "Bearer secret-token", "Content-Type": "application/json", "cookie": "sid=1"})
    assert out["Authorization"] == "Bearer ***"
    assert out["Content-Type"] == "application/json"
    assert "secret" not in str(out) and "sid=1" not in str(out)
    assert redact_headers(None) == {}


def test_truncate_reference():
    assert truncate_reference("ACC-PRQ-2026-00123") == "ACC-PRQ-2026"
    assert len(truncate_reference("x" * 40, 13)) == 13
    assert truncate_reference("") == "Payment"


@pytest.mark.parametrize("value,expected", [(100, 100), ("99.5", 100), (10.49, 10), ("1", 1)])
def test_stk_amount_whole_numbers(value, expected):
    assert stk_amount(value) == expected


@pytest.mark.parametrize("value", [0, "0.4", -5, None])
def test_stk_amount_rejects_below_one(value):
    with pytest.raises(ValueError):
        stk_amount(value)
