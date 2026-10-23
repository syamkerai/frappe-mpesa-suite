import hashlib

import pytest

from frappe_mpsa_payments.utils.phone import (
    is_safaricom,
    looks_like_sha256,
    mask,
    msisdn_sha256,
    network_of,
    normalize_msisdn,
    to_local,
)


@pytest.mark.parametrize(
    "raw",
    ["0712345678", "712345678", "254712345678", "+254 712 345 678", "254-712-345-678", " 0712 345678 "],
)
def test_normalize_7xx_forms(raw):
    assert normalize_msisdn(raw) == "254712345678"


@pytest.mark.parametrize("raw,expected", [("0110123456", "254110123456"), ("254111234567", "254111234567")])
def test_normalize_1xx_forms(raw, expected):
    assert normalize_msisdn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "12345", "0812345678", "2547123456789", "2.54712E+11", "07123456", "abc", "0212345678"],
)
def test_rejects_invalid(raw):
    assert normalize_msisdn(raw) is None


def test_to_local():
    assert to_local("254712345678") == "0712345678"
    assert to_local("bad") is None


def test_sha256_matches_safaricom_c2b_hash():
    expected = hashlib.sha256(b"254712345678").hexdigest()
    assert msisdn_sha256("0712 345 678") == expected
    assert looks_like_sha256(expected)
    assert not looks_like_sha256("254712345678")


@pytest.mark.parametrize(
    "raw,network",
    [
        ("0712345678", "Safaricom"),
        ("0110123456", "Safaricom"),
        ("0733123456", "Airtel"),
        ("0100123456", "Airtel"),
        ("0771123456", "Telkom"),
        ("0763123456", "Equitel"),
        ("0747123456", "Faiba"),
    ],
)
def test_network_of(raw, network):
    assert network_of(raw) == network


def test_is_safaricom():
    assert is_safaricom("0722000000")
    assert not is_safaricom("0733000000")


def test_mask_hides_middle_digits():
    assert mask("0712345678") == "2547****5678"
    assert mask("nonsense") == "***"
