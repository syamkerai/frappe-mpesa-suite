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
    """Frappe's RedisWrapper, as far as the token cache goes. ``values`` is Redis, shared by
    every worker; ``frappe.local.cache`` is this request's or job's own copy. ``get_value``
    serves the local copy first and keeps what it read there (a miss too) unless ``expires``;
    v15's ``set_value`` with an expiry leaves the local copy alone, v16's always writes it."""

    def __init__(self, frappe, version=15, lock_fails=False):
        self.frappe, self.version = frappe, version
        self.values, self.events, self.lock_fails = {}, [], lock_fails

    @property
    def local(self):
        return self.frappe.local.cache

    def get_value(self, key, generator=None, user=None, expires=False, shared=False):
        made = self.make_key(key)
        if made in self.local:
            return self.local[made]
        value = self.values.get(key)
        if not expires:
            self.local[made] = value
        return value

    def set_value(self, key, value, user=None, expires_in_sec=None, shared=False):
        if self.version >= 16 or not expires_in_sec:
            self.local[self.make_key(key)] = value
        self.values[key] = value

    def delete_value(self, key):
        self.local.pop(self.make_key(key), None)
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


@pytest.fixture(params=[15, 16], ids=["v15", "v16"])
def client(monkeypatch, request):
    frappe, saved = fake_frappe.install()
    try:
        frappe.local.cache = {}
        mod = importlib.import_module("mpsa_suite.daraja.client")
        cache = FakeCache(frappe, version=request.param)
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


def test_token_fetched_after_a_miss_is_reused_for_the_rest_of_the_job(client):
    # v15: the first read keeps None in frappe.local.cache and set_value with an expiry does
    # not replace it, so every later call in the same job used to mint (and revoke) again.
    mod, cache, calls = client
    assert [mod.fetch_token("key", "secret", URL) for _ in range(3)] == ["tok1"] * 3
    assert len(calls) == 1


def test_worker_that_waited_does_not_trust_its_own_earlier_miss(client, monkeypatch):
    mod, cache, calls = client
    key = mod._token_key("key", URL)
    cache.get_value(key)  # an earlier plain read in this job kept the miss locally
    real_lock = cache.lock

    def lock_then_other_worker_fetches(name, **kw):
        cache.values[key] = "fresh-from-other-worker"  # Redis only: not in our local copy
        return real_lock(name, **kw)

    monkeypatch.setattr(cache, "lock", lock_then_other_worker_fetches)
    assert mod.fetch_token("key", "secret", URL) == "fresh-from-other-worker"
    assert calls == []


def test_invalidate_compares_the_refused_token_with_redis_not_a_local_copy(client):
    mod, cache, calls = client
    settings = fake_frappe._dict(consumer_key="key", sandbox=1)
    key = mod._token_key("key", URL)
    cache.local[cache.make_key(key)] = "mine"  # what this job sent (v16 set_value keeps it locally)
    cache.values[key] = "newer"  # another worker refreshed meanwhile

    mod.invalidate_token(settings, rejected="mine")
    assert cache.values[key] == "newer"  # kept: dropping it would make us mint and revoke it
    assert mod.fetch_token("key", "secret", URL) == "newer"  # the retry uses it, not "mine"
    assert calls == []


def test_invalidate_never_deletes_a_token_cached_after_the_refused_one_was_dropped(client, monkeypatch):
    # Worker 1 already dropped T0 and is fetching Ta; its set_value lands between our read and
    # our delete. Redis held nothing to drop, so a delete could only have removed Ta.
    mod, cache, calls = client
    settings = fake_frappe._dict(consumer_key="key", sandbox=1)
    key = mod._token_key("key", URL)
    real_get = cache.get_value

    def read_then_other_worker_stores(k, **kw):
        value = real_get(k, **kw)
        cache.values[key] = "Ta"
        return value

    monkeypatch.setattr(cache, "get_value", read_then_other_worker_stores)
    mod.invalidate_token(settings, rejected="T0")
    monkeypatch.setattr(cache, "get_value", real_get)
    assert cache.values[key] == "Ta"
    assert mod.fetch_token("key", "secret", URL) == "Ta" and calls == []


def test_fetch_still_works_when_redis_cannot_lock(client, monkeypatch):
    mod, cache, calls = client
    cache.lock_fails = True
    assert mod.fetch_token("key", "secret", URL) == "tok1"
    assert not any(e.startswith("unlock") for e in cache.events)


def test_invalidate_drops_only_the_refused_token():
    frappe, saved = fake_frappe.install()
    try:
        frappe.local.cache = {}
        mod = importlib.import_module("mpsa_suite.daraja.client")
        cache = FakeCache(frappe)
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
        mod = importlib.import_module("mpsa_suite.daraja.client")
        assert mod.bearer_token(headers) == expected
    finally:
        fake_frappe.uninstall(saved)
