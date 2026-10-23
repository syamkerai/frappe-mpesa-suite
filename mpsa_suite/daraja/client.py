"""Single Safaricom Daraja HTTP client: base URLs, OAuth token cache, safe requests.

Every connector in the app gets its token from here, so there is one cache, one
expiry margin and no credentials written to the database or Integration Request.
"""

import hashlib
from contextlib import contextmanager

import frappe
import requests
from frappe import _
from frappe.utils import cint

from ..compat import get_cache

SANDBOX_URL = "https://sandbox.safaricom.co.ke"
LIVE_URL = "https://api.safaricom.co.ke"

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
# Refresh the token this many seconds before Safaricom says it expires (tokens last 3599s).
TOKEN_SAFETY_MARGIN = 120
# Longest a worker waits for another worker's token fetch (connect + read timeout).
TOKEN_LOCK_SECONDS = CONNECT_TIMEOUT + READ_TIMEOUT


def base_url(sandbox) -> str:
    return SANDBOX_URL if cint(sandbox) else LIVE_URL


def _token_key(consumer_key: str, url: str) -> str:
    digest = hashlib.sha256(f"{consumer_key}|{url.rstrip('/')}".encode()).hexdigest()[:24]
    return f"mpesa_suite:daraja_token:{digest}"


def _cached_token(cache, key: str) -> str | None:
    """The token Redis holds now, never a stale per-request copy.

    ``get_value`` serves ``frappe.local.cache`` first and keeps what it read there (a miss
    too) for the rest of the request or job, and v15's ``set_value`` with an expiry does not
    update it. Without this, a token another worker stored (or that we just stored) stays
    invisible, so the lock re-check and ``invalidate_token(rejected=)`` see the wrong one.
    """
    local = getattr(frappe.local, "cache", None)
    make_key = getattr(cache, "make_key", None)
    if isinstance(local, dict) and make_key:
        local.pop(make_key(key), None)
    return cache.get_value(key, expires=True)  # expires=True: read Redis, keep no local copy


@contextmanager
def _fetch_lock(cache, key: str):
    """Hold a Redis lock while fetching a token; carry on without one if Redis can't lock.

    Each token request invalidates the previous token (Daraja Authorization FAQ 3), so two
    workers that miss the cache together would otherwise revoke each other's tokens.
    """
    lock, acquired = None, False
    try:
        make_key = getattr(cache, "make_key", None)
        name = make_key(f"{key}:lock") if make_key else f"{key}:lock"
        lock = cache.lock(name, timeout=TOKEN_LOCK_SECONDS, blocking_timeout=TOKEN_LOCK_SECONDS)
        acquired = bool(lock.acquire())
    except Exception:
        acquired = False
    try:
        yield
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass  # expired while fetching; the next fetch takes a fresh lock


def fetch_token(consumer_key: str, consumer_secret: str, url: str, force: bool = False) -> str:
    """Return a cached OAuth token for these credentials, fetching a new one when needed."""
    if not consumer_key or not consumer_secret:
        frappe.throw(_("M-Pesa Consumer Key and Consumer Secret are required."))

    cache = get_cache()
    key = _token_key(consumer_key, url)
    if not force:
        cached = _cached_token(cache, key)
        if cached:
            return cached

    with _fetch_lock(cache, key):
        if not force:
            cached = _cached_token(cache, key)  # another worker fetched while we waited
            if cached:
                return cached
        return _request_token(cache, key, consumer_key, consumer_secret, url)


def _request_token(cache, key: str, consumer_key: str, consumer_secret: str, url: str) -> str:
    endpoint = f"{url.rstrip('/')}/oauth/v1/generate?grant_type=client_credentials"
    try:
        response = requests.get(
            endpoint,
            auth=(consumer_key, consumer_secret),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.RequestException as e:
        frappe.throw(_("Could not reach M-Pesa to authenticate: {0}").format(type(e).__name__))

    try:
        data = response.json()
    except ValueError:
        data = {}

    token = data.get("access_token")
    if not response.ok or not token:
        frappe.throw(
            _("M-Pesa authentication failed (HTTP {0}): {1}").format(
                response.status_code,
                data.get("errorMessage") or data.get("error_description") or response.text[:200],
            )
        )

    ttl = max(60, cint(data.get("expires_in") or 3599) - TOKEN_SAFETY_MARGIN)
    cache.set_value(key, token, expires_in_sec=ttl)
    return token


def get_access_token(settings, force: bool = False) -> str:
    """Token for an ``Mpesa Settings`` document or name."""
    if isinstance(settings, str):
        settings = frappe.get_doc("Mpesa Settings", settings)
    return fetch_token(
        settings.consumer_key,
        settings.get_password("consumer_secret", raise_exception=False),
        base_url(settings.sandbox),
        force=force,
    )


def invalidate_token(settings, rejected: str | None = None) -> None:
    """Drop the cached token. With ``rejected`` (the token Safaricom refused), only drop it if
    the cache still holds exactly that token: another worker may already have cached a fresh
    one, or dropped ours and be caching one now, and fetching again would revoke it."""
    if isinstance(settings, str):
        settings = frappe.get_doc("Mpesa Settings", settings)
    if not settings.consumer_key:
        return
    cache = get_cache()
    key = _token_key(settings.consumer_key, base_url(settings.sandbox))
    if rejected and _cached_token(cache, key) != rejected:
        return  # nothing cached is nothing to drop; a delete could hit a token stored meanwhile
    cache.delete_value(key)


def bearer_token(headers: dict) -> str | None:
    """The token inside a ``bearer_headers`` dict, to pass back as ``rejected``."""
    value = str((headers or {}).get("Authorization") or "")
    return value.split(" ", 1)[1] if value.startswith("Bearer ") else None


def bearer_headers(settings, force: bool = False) -> dict:
    return {
        "Authorization": f"Bearer {get_access_token(settings, force=force)}",
        "Content-Type": "application/json",
    }
