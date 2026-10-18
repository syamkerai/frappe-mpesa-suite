"""Authentication and execution context for inbound Safaricom callbacks.

Every ``allow_guest`` endpoint that Safaricom posts to calls ``accept()`` first:

    if not accept(KIND_RESULT):
        return rejected_response()

and runs its work inside ``as_integration_user()`` instead of switching to
Administrator for the rest of the request.
"""

import functools
import inspect
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import frappe
from frappe.utils import cint, now_datetime

from ..compat import get_cache
from .guard import ip_allowed, token_matches

SETTINGS = "M-Pesa Suite Settings"
KIND_C2B = "c2b"  # C2B validation / confirmation (URLs registered once at Safaricom)
KIND_RESULT = "result"  # STK, B2C, B2B, status, balance, reversal (URL sent per request)
KIND_PULL = "pull"  # Pull Transactions: registered once, permanently -> IP check only, no token
TOKEN_PARAM = "t"


def _settings():
    try:
        return frappe.get_cached_doc(SETTINGS)
    except frappe.DoesNotExistError:
        return frappe._dict()


def callback_token() -> str | None:
    if not frappe.db.exists("DocType", SETTINGS):
        return None
    settings = _settings()
    if not settings or not getattr(settings, "get_password", None):
        return None
    return settings.get_password("callback_token", raise_exception=False) or None


def with_token(url: str) -> str:
    """Append ``?t=<callback token>`` to a callback URL (idempotent)."""
    token = callback_token()
    if not token:
        return url
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != TOKEN_PARAM]
    query.append((TOKEN_PARAM, token))
    return urlunparse(parts._replace(query=urlencode(query)))


def _request_token() -> str | None:
    request = getattr(frappe.local, "request", None)
    if request is not None and request.args.get(TOKEN_PARAM):
        return request.args.get(TOKEN_PARAM)
    return frappe.local.form_dict.get(TOKEN_PARAM)


def accept(kind: str = KIND_RESULT) -> bool:
    """True if this inbound request may be processed; records the rejection otherwise."""
    settings = _settings()
    if not settings:
        return True  # app not migrated yet: behave as before

    ip = getattr(frappe.local, "request_ip", None)
    if cint(settings.get("enforce_ip_allowlist")) and not ip_allowed(
        ip, settings.get("allowed_callback_ips")
    ):
        _record_rejection(f"{kind}: source IP {ip} not allowed")
        return False

    if kind == KIND_PULL:
        return True
    enforce = settings.get("enforce_token_c2b") if kind == KIND_C2B else settings.get("enforce_token_results")
    if cint(enforce) and not token_matches(_request_token(), callback_token()):
        _record_rejection(f"{kind}: missing or wrong callback token from {ip}")
        return False

    return True


def rejected_response() -> dict:
    return {"ResultCode": 1, "ResultDesc": "Rejected"}


def _record_rejection(reason: str) -> None:
    """Count rejections; write at most one row per minute so an attacker can't flood the DB."""
    cache = get_cache()
    key = "mpesa_suite:callback_rejected_recently"
    if cache.get_value(key):
        return
    cache.set_value(key, 1, expires_in_sec=60)
    try:
        frappe.db.set_single_value(
            SETTINGS,
            {
                "rejected_callbacks": cint(frappe.db.get_single_value(SETTINGS, "rejected_callbacks")) + 1,
                "last_rejected_callback_on": now_datetime(),
                "last_rejected_callback_reason": reason[:500],
            },
        )
        frappe.db.commit()  # guest request; nothing else will be committed
    except Exception:
        frappe.db.rollback()


def integration_user() -> str:
    return _settings().get("integration_user") or "Administrator"


@contextmanager
def as_integration_user():
    """Run callback work as the configured integration user, then restore the caller."""
    previous = frappe.session.user if getattr(frappe.local, "session", None) else "Guest"
    frappe.set_user(integration_user())
    try:
        yield
    finally:
        frappe.set_user(previous or "Guest")


def _call_with_accepted_args(fn, args, kwargs):
    """Pass only the keyword arguments ``fn`` declares (unless it takes **kwargs)."""
    params = inspect.signature(fn).parameters.values()
    if any(p.kind == p.VAR_KEYWORD for p in params):
        return fn(*args, **kwargs)
    names = {p.name for p in params}
    return fn(*args, **{k: v for k, v in kwargs.items() if k in names})


def safaricom_callback(kind: str = KIND_RESULT, rejected=None):
    """Decorator for Safaricom-facing endpoints. Put it *under* ``@frappe.whitelist(allow_guest=True)``.

    Rejects unauthenticated requests (when enforcement is on), strips the URL token from the
    arguments and runs the handler as the integration user.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            kwargs.pop(TOKEN_PARAM, None)
            kwargs.pop("cmd", None)
            if not accept(kind):
                return rejected_response() if rejected is None else rejected
            with as_integration_user():
                return _call_with_accepted_args(fn, args, kwargs)

        return wrapper

    return decorator
