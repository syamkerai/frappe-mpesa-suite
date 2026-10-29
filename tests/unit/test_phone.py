import hashlib

import pytest

from mpsa_suite.utils.phone import (
    FORMATS,
    is_safaricom,
    looks_like_sha256,
    mask,
    msisdn_sha256,
    network_of,
    normalize_msisdn,
    payable_msisdn,
    repaired_msisdn,
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
        ("0115123456", "Safaricom"),
        ("0118123456", "Safaricom"),
        ("0119123456", "Safaricom"),
        ("254118123456", "Safaricom"),
        ("0140123456", "Safaricom"),
        ("0143123456", "Safaricom"),
        ("0180123456", "Safaricom"),
        ("0182123456", "Safaricom"),
        ("0733123456", "Airtel"),
        ("0100123456", "Airtel"),
        ("0106123456", "Airtel"),
        ("0771123456", "Telkom"),
        ("0763123456", "Equitel"),
        ("0747123456", "Faiba"),
    ],
)
def test_network_of(raw, network):
    assert network_of(raw) == network


@pytest.mark.parametrize("raw", ["0144123456", "0183123456"])
def test_network_of_an_unopened_block_is_unknown(raw):
    assert network_of(raw) is None


def test_is_safaricom():
    assert is_safaricom("0722000000")
    assert is_safaricom("0119 000 000")
    assert is_safaricom("0142000000")
    assert not is_safaricom("0733000000")
    assert not is_safaricom("0101000000")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0712345678", "254712345678"),
        ("0111 444 514", "254111444514"),
        ("0101444514", "254101444514"),
        ("+254 142 000 280", "254142000280"),
        ("0182000280", "254182000280"),
        ("111444514", "254111444514"),
    ],
)
def test_payable_msisdn_takes_the_blocks_in_use(raw, expected):
    assert payable_msisdn(raw) == expected


@pytest.mark.parametrize("raw", ["0122345678", "0132345678", "0152345678", "0192345678", "0144123456", "0149123456",
                                 "0183123456", "25416055453", "", None])
def test_payable_msisdn_refuses_other_numbers(raw):
    assert payable_msisdn(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("0712 345 678", "254712345678"),
    ("+254 0712 345 678", "254712345678"),
    ("2540111444514", "254111444514"),
    ("00254 712 345 678", "254712345678"),
    ("002540712345678", "254712345678"),
    ("0254712345678", "254712345678"),
    ("02540712345678", "254712345678"),
])
def test_repaired_msisdn_drops_a_prefix_too_many(raw, expected):
    assert repaired_msisdn(raw) == expected


@pytest.mark.parametrize("raw", ["25416055453", "0025471234567", "+44 7911 123456", "2.54712E+11", "07123456789", ""])
def test_repaired_msisdn_guesses_nothing_else(raw):
    assert repaired_msisdn(raw) is None


def test_normalize_msisdn_stays_strict_for_the_c2b_hash():
    assert normalize_msisdn("00254712345678") is None and normalize_msisdn("+254 0712 345 678") is None


def test_formats_name_the_blocks_in_use():
    assert "07XX XXX XXX" in FORMATS and "010X, 011X, 0140-0143, 0180-0182" in FORMATS


def test_mask_hides_middle_digits():
    assert mask("0712345678") == "2547****5678"
    assert mask("nonsense") == "***"
