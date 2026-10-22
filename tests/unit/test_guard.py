import ast
from pathlib import Path

import pytest

from mpsa_suite.daraja.guard import (
    DEFAULT_SAFARICOM_IPS,
    callback_url_problems,
    daraja_id,
    ip_allowed,
    is_zero_code,
    names_id,
    parse_allowlist,
    redact_headers,
    stk_amount,
    token_matches,
    truncate_reference,
)

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"

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


@pytest.mark.parametrize(
    "value,expected",
    [("0", True), (0, True), ("00000000", True), (" 0 ", True),  # sandbox registerurl answers "00000000"
     ("", False), (None, False), ("1", False), ("C2B00011", False), ("500.003.1001", False), ("10", False)],
)
def test_is_zero_code(value, expected):
    assert is_zero_code(value) is expected


def test_stk_amount_daraja_maximum():
    assert stk_amount(250000) == 250000
    with pytest.raises(ValueError):
        stk_amount(250001)


@pytest.mark.parametrize(
    "url,sandbox,expected",
    [
        ("https://erp.example.com/api/method/mpsa_suite.money_out.api.b2b_result?ref=MB2B-1&t=ab12", False, []),
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



@pytest.mark.parametrize(
    "value",
    ["AG_20191219_00005797af5d7d75f652", "16740-34861180-1", "ws_CO_191220191020363925",
     "c1a2b3d4-e5f6-7890-abcd-ef1234567890", " AG_1 "],
)
def test_daraja_id_accepts_safaricom_ids(value):
    assert daraja_id(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    ["%", "%%", "AG_1%", "attempts%", "AG\\_1", "AG' OR '1'='1", 'AG"1', "_", "-1", "", None,
     "A" * 65, "AG 1", {"$like": "%"}, ["AG_1"]],
)
def test_daraja_id_refuses_like_wildcards_and_junk(value):
    # a ConversationID of "%" made ``output like '%%%'`` match every Integration Request
    assert daraja_id(value) == ""


def test_names_id_matches_exactly_in_json_and_logged_dict():
    import json

    ack = {"OriginatorConversationID": "OC_9", "ConversationID": "AG_1", "ResponseCode": "0"}
    # JSON as we save it, and the str(dict) lines the connector appends to an IR's output
    for stored in (json.dumps(ack), json.dumps(ack, indent=4), str(ack), "null\n" + str(ack)):
        assert names_id(stored, "ConversationID", "AG_1")
        assert not names_id(stored, "ConversationID", "AG_")  # a prefix is not the ID
        assert not names_id(stored, "ConversationID", "OC_9")  # nor the other key's value
        assert names_id(stored, "OriginatorConversationID", "OC_9")
    assert not names_id('{"attempts": 0}', "ConversationID", "attempts")
    assert not names_id(None, "ConversationID", "AG_1")
    assert not names_id(json.dumps(ack), "ConversationID", "")


# Upstream endpoint names the app no longer sends; kept so results for requests (or Pull
# registrations) made with the old URL still land.
_SETTINGS_MODULE = "mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings"
LEGACY_CALLBACKS = {
    "mpsa_suite.mpsa_suite.api.m_pesa_api.balance_query_callback",
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
