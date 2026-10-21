"""MpesaConnector's token-error retry (STK push, STK query, balance, pull) drops only the token
Safaricom refused, so two workers retrying together never revoke each other's fresh token
(Daraja Authorization FAQ 3: "each request invalidates the previous token")."""

import importlib

import pytest

import fake_frappe
from test_daraja_client import FakeCache, Resp

URL = "https://sandbox.safaricom.co.ke"
TOKEN_ERROR = {"requestId": "r1", "errorCode": "404.001.03", "errorMessage": "Invalid Access Token"}
ACK = {"MerchantRequestID": "m1", "CheckoutRequestID": "ws_CO_1", "ResponseCode": "0"}


@pytest.fixture(params=[15, 16], ids=["v15", "v16"])
def env(monkeypatch, request):
    frappe, saved = fake_frappe.install()
    try:
        frappe.local.cache = {}
        client = importlib.import_module("frappe_mpsa_payments.daraja.client")
        connectors = importlib.import_module("frappe_mpsa_payments.frappe_mpsa_payments.connectors.connectors")
        cache = FakeCache(frappe, version=request.param)
        monkeypatch.setattr(client, "get_cache", lambda: cache)
        monkeypatch.setattr(connectors, "update_integration_request", lambda *a, **kw: None)
        frappe.db.table("Mpesa Settings")["S1"] = {
            "name": "S1", "consumer_key": "key", "sandbox": 1,
            "get_password": lambda field, raise_exception=True: "secret",
        }
        state = fake_frappe._dict(minted=[], posts=[], answers=[], cache=cache, frappe=frappe,
                                  key=client._token_key("key", URL), connectors=connectors)

        def fake_oauth(url, auth=None, timeout=None):
            state.minted.append(f"tok{len(state.minted) + 1}")
            return Resp({"access_token": state.minted[-1], "expires_in": "3599"})

        def fake_post(url, json=None, headers=None, timeout=None):
            state.posts.append(headers["Authorization"])
            answer = state.answers.pop(0)
            return answer() if callable(answer) else answer

        monkeypatch.setattr(client.requests, "get", fake_oauth)
        monkeypatch.setattr(connectors.requests, "post", fake_post)
        yield state
    finally:
        fake_frappe.uninstall(saved)


def stk_push(env):
    done = []
    connector = (
        env.connectors.MpesaConnector("S1")
        .set_endpoint("mpesa/stkpush/v1/processrequest")
        .set_method("POST")
        .set_payload({"Amount": 1})
        .on_success(lambda **kw: done.append("ok"))
        .on_error(lambda **kw: done.append("error"))
    )
    connector.make_remote_call(doctype="Mpesa Express Request", document_name="MER-1")
    return done


def test_retry_keeps_the_token_another_worker_just_cached(env):
    env.cache.values[env.key] = "T0"

    def refused_while_other_worker_refreshed():
        env.cache.values[env.key] = "Ta"  # worker 1 already dropped T0 and cached Ta
        return Resp(TOKEN_ERROR, 404)

    env.answers += [refused_while_other_worker_refreshed, Resp(ACK)]
    assert stk_push(env) == ["ok"]
    assert env.posts == ["Bearer T0", "Bearer Ta"]
    assert env.minted == []  # minting Tb would have revoked worker 1's Ta mid-retry
    assert env.cache.values[env.key] == "Ta"


def test_late_retry_never_deletes_the_token_another_worker_is_caching(env):
    env.cache.values[env.key] = "T0"
    real_get = env.cache.get_value

    def store_right_after_the_next_read(key, **kw):
        env.cache.get_value = real_get
        value = real_get(key, **kw)
        env.cache.values[env.key] = "Ta"  # worker 1's set_value lands between our read and delete
        return value

    def refused_while_other_worker_fetches():
        env.cache.values.pop(env.key)  # worker 1 already dropped T0 and is minting Ta
        env.cache.get_value = store_right_after_the_next_read
        return Resp(TOKEN_ERROR, 404)

    env.answers += [refused_while_other_worker_fetches, Resp(ACK)]
    assert stk_push(env) == ["ok"]
    assert env.posts == ["Bearer T0", "Bearer Ta"]
    assert env.minted == []  # deleting Ta would have made us mint Tb and revoke Ta
    assert env.cache.values[env.key] == "Ta"


def test_retry_refreshes_when_the_refused_token_is_still_cached(env):
    env.cache.values[env.key] = "T0"
    env.answers += [Resp(TOKEN_ERROR, 404), Resp(ACK)]
    assert stk_push(env) == ["ok"]
    assert env.minted == ["tok1"] and env.posts == ["Bearer T0", "Bearer tok1"]
    assert env.cache.values[env.key] == "tok1"
