import ast
from pathlib import Path

import pytest

from frappe_mpsa_payments.daraja.guard import (
    DEFAULT_SAFARICOM_IPS,
    callback_url_problems,
    ip_allowed,
    parse_allowlist,
    redact_headers,
    stk_amount,
    token_matches,
    truncate_reference,
)

APP = Path(__file__).resolve().parents[2] / "frappe_mpsa_payments"

# The 12 IPs Daraja publishes (GettingStarted, "Callback and IP Whitelisting").
PUBLISHED_IPS = [
    "196.201.214.200", "196.201.214.206", "196.201.214.207", "196.201.214.208",
    "196.201.213.114", "196.201.213.44",
    "196.201.212.127", "196.201.212.138", "196.201.212.129", "196.201.212.136", "196.201.212.74", "196.201.212.69",
]


@pytest.mark.parametrize("ip", PUBLISHED_IPS)
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


def test_stk_amount_daraja_maximum():
    assert stk_amount(250000) == 250000
    with pytest.raises(ValueError):
        stk_amount(250001)


@pytest.mark.parametrize(
    "url,sandbox,expected",
    [
        ("https://erp.example.com/api/method/frappe_mpsa_payments.money_out.api.b2b_result?ref=MB2B-1&t=ab12", False, []),
        ("http://erp.example.com/api/method/x.confirmation", True, []),  # sandbox may use HTTP
        ("http://erp.example.com/api/method/x.confirmation", False, ["production callback URLs must use HTTPS"]),
        ("https://erp.example.com/api/method/x.balance_query_callback", False, ["query"]),
        ("https://erp.example.com/api/method/x.b2c_results_callback?provider=Mpesa", False, ["mpesa"]),
        ("https://erp.example.com/api/method/x.SQL_result", False, ["sql"]),
        ("", False, ["the callback URL is empty"]),
    ],
)
def test_callback_url_problems(url, sandbox, expected):
    problems = callback_url_problems(url, sandbox=sandbox)
    if not expected:
        assert problems == []
    else:
        assert all(any(e in p for p in problems) for e in expected)


# Upstream endpoint names the app no longer sends; kept so results for requests (or Pull
# registrations) made with the old URL still land.
_SETTINGS_MODULE = "frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings"
LEGACY_CALLBACKS = {
    "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.balance_query_callback",
    f"{_SETTINGS_MODULE}.process_balance_info",
    f"{_SETTINGS_MODULE}.pull_transaction_callback",
}


def _safaricom_callbacks():
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = ".".join(path.relative_to(APP.parent).with_suffix("").parts)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(
                "safaricom_callback" in ast.unparse(d) for d in node.decorator_list
            ):
                yield f"{module}.{node.name}"


def test_app_no_longer_sends_legacy_callback_urls():
    literals = {
        node.value
        for path in APP.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not any(legacy in text for legacy in LEGACY_CALLBACKS for text in literals)


def test_every_safaricom_callback_url_avoids_banned_words():
    callbacks = sorted(set(_safaricom_callbacks()))
    assert len(callbacks) >= 10  # the scan found the endpoints
    bad = {
        method: problems
        for method in callbacks
        if method not in LEGACY_CALLBACKS
        and (problems := callback_url_problems(f"https://erp.example.com/api/method/{method}"))
    }
    assert bad == {}
