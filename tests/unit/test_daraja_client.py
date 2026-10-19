"""OAuth token cache rules from Daraja's Authorization FAQ: "each request invalidates the
previous token", so workers must share one token and never revoke a fresher one."""

import importlib

import pytest

import fake_frappe


class FakeLock:
    def __init__(self, cache, name, fail=False):
        self.cache, self.name, self.fail = cache, name, fail

    def acquire(self):
        if self.fail:
            raise ConnectionError("redis down")
        self.cache.events.append(f"lock {self.name}")
        return True

    def release(self):
        self.cache.events.append(f"unlock {self.name}")


class FakeCache:
    def __init__(self, lock_fails=False):
        self.values, self.events, self.lock_fails = {}, [], lock_fails

    def get_value(self, key):
        return self.values.get(key)

    def set_value(self, key, value, expires_in_sec=None):
        self.values[key] = value

    def delete_value(self, key):
        self.values.pop(key, None)

    def make_key(self, key):
        return f"site1|{key}"

    def lock(self, name, timeout=None, blocking_timeout=None):
        return FakeLock(self, name, fail=self.lock_fails)


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.ok, self.text = data, status, status < 300, str(data)

    def json(self):
        return self._data


@pytest.fixture
def client(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        mod = importlib.import_module("frappe_mpsa_payments.daraja.client")
        cache = FakeCache()
        monkeypatch.setattr(mod, "get_cache", lambda: cache)
        calls = []

        def fake_get(url, auth=None, timeout=None):
            calls.append(url)
            return Resp({"access_token": f"tok{len(calls)}", "expires_in": "3599"})

        monkeypatch.setattr(mod.requests, "get", fake_get)
        yield mod, cache, calls
    finally:
        fake_frappe.uninstall(saved)


URL = "https://sandbox.safaricom.co.ke"


def test_token_is_cached_and_shared(client):
    mod, cache, calls = client
    assert mod.fetch_token("key", "secret", URL) == "tok1"
    assert mod.fetch_token("key", "secret", URL) == "tok1"
    assert len(calls) == 1


def test_fetch_happens_under_a_site_scoped_lock(client):
    mod, cache, calls = client
    mod.fetch_token("key", "secret", URL)
    lock_name = f"site1|{mod._token_key('key', URL)}:lock"
    assert cache.events == [f"lock {lock_name}", f"unlock {lock_name}"]


def test_worker_that_waited_reuses_the_token_the_lock_holder_cached(client, monkeypatch):
    mod, cache, calls = client
    key = mod._token_key("key", URL)
    real_lock = cache.lock

    def lock_then_other_worker_fetches(name, **kw):
        cache.values[key] = "fresh-from-other-worker"
        return real_lock(name, **kw)

    monkeypatch.setattr(cache, "lock", lock_then_other_worker_fetches)
    assert mod.fetch_token("key", "secret", URL) == "fresh-from-other-worker"
    assert calls == []  # no second fetch, so the other worker's token stays valid


def test_fetch_still_works_when_redis_cannot_lock(client, monkeypatch):
    mod, cache, calls = client
    cache.lock_fails = True
    assert mod.fetch_token("key", "secret", URL) == "tok1"
    assert not any(e.startswith("unlock") for e in cache.events)


def test_invalidate_drops_only_the_refused_token():
    frappe, saved = fake_frappe.install()
    try:
        mod = importlib.import_module("frappe_mpsa_payments.daraja.client")
        cache = FakeCache()
        mod.get_cache = lambda: cache
        settings = fake_frappe._dict(consumer_key="key", sandbox=1)
        key = mod._token_key("key", URL)

        cache.values[key] = "newer"
        mod.invalidate_token(settings, rejected="older")
        assert cache.values[key] == "newer"  # another worker already refreshed: keep it

        mod.invalidate_token(settings, rejected="newer")
        assert key not in cache.values

        cache.values[key] = "any"
        mod.invalidate_token(settings)  # no token given: always drop
        assert key not in cache.values
    finally:
        fake_frappe.uninstall(saved)


@pytest.mark.parametrize(
    "headers,expected",
    [({"Authorization": "Bearer abc"}, "abc"), ({"Authorization": "Basic x"}, None), ({}, None), (None, None)],
)
def test_bearer_token(headers, expected):
    frappe, saved = fake_frappe.install()
    try:
        mod = importlib.import_module("frappe_mpsa_payments.daraja.client")
        assert mod.bearer_token(headers) == expected
    finally:
        fake_frappe.uninstall(saved)
