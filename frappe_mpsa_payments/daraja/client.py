"""Single Safaricom Daraja HTTP client: base URLs, OAuth token cache, safe requests.

Every connector in the app gets its token from here, so there is one cache, one
expiry margin and no credentials written to the database or Integration Request.
"""

import hashlib

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


def base_url(sandbox) -> str:
    return SANDBOX_URL if cint(sandbox) else LIVE_URL


def _token_key(consumer_key: str, url: str) -> str:
    digest = hashlib.sha256(f"{consumer_key}|{url.rstrip('/')}".encode()).hexdigest()[:24]
    return f"mpesa_suite:daraja_token:{digest}"


def fetch_token(consumer_key: str, consumer_secret: str, url: str, force: bool = False) -> str:
    """Return a cached OAuth token for these credentials, fetching a new one when needed."""
    if not consumer_key or not consumer_secret:
        frappe.throw(_("M-Pesa Consumer Key and Consumer Secret are required."))

    cache = get_cache()
    key = _token_key(consumer_key, url)
    if not force:
        cached = cache.get_value(key)
        if cached:
            return cached

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


def invalidate_token(settings) -> None:
    if isinstance(settings, str):
        settings = frappe.get_doc("Mpesa Settings", settings)
    if settings.consumer_key:
        get_cache().delete_value(_token_key(settings.consumer_key, base_url(settings.sandbox)))


def bearer_headers(settings, force: bool = False) -> dict:
    return {
        "Authorization": f"Bearer {get_access_token(settings, force=force)}",
        "Content-Type": "application/json",
    }
