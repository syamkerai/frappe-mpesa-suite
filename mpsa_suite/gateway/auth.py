"""Authentication, rate limiting and the JSON envelope for the external Payments API.

Clients send two headers (Frappe reserves ``Authorization`` for its own users and OAuth):

    X-Mpesa-Client-Key:    mpk_...
    X-Mpesa-Client-Secret: mps_...

Every response is ``{"message": {"ok": true, ...}}`` or, with an HTTP error status,
``{"message": {"ok": false, "error": {"code": "...", "message": "..."}}}``.
"""

import functools
import inspect
import re

import frappe
from frappe.utils import cint, now_datetime

from ..compat import get_cache
from ..daraja.callbacks import as_integration_user
from ..daraja.guard import ip_allowed, token_matches

KEY_HEADER = "X-Mpesa-Client-Key"
SECRET_HEADER = "X-Mpesa-Client-Secret"
CLIENT = "Mpesa API Client"


class ApiError(Exception):
    """``rollback=False``: keep this call's writes (e.g. a failed prompt that upstream already
    committed, or an intent we just marked Expired) and still answer with the error."""

    def __init__(self, status: int, code: str, message: str, rollback: bool = True):
        super().__init__(message)
        self.status, self.code, self.message, self.rollback = status, code, message, rollback


def undo(save_point: str = "mpesa_api") -> None:
    """Roll back to our savepoint. Upstream M-Pesa code commits mid-request (which drops
    savepoints); then only work done since that commit can be undone."""
    try:
        frappe.db.rollback(save_point=save_point)
    except Exception:
        frappe.db.rollback()


def _header(name: str) -> str | None:
    request = getattr(frappe.local, "request", None)
    return request.headers.get(name) if request is not None else None


def authenticate():
    key, secret = _header(KEY_HEADER), _header(SECRET_HEADER)
    if not key or not secret:
        raise ApiError(401, "unauthenticated", f"Send {KEY_HEADER} and {SECRET_HEADER} headers.")
    name = frappe.db.get_value(CLIENT, {"api_key": key, "enabled": 1}, "name")
    if not name:
        raise ApiError(401, "unauthenticated", "Unknown or disabled API key.")
    client = frappe.get_doc(CLIENT, name)
    if not token_matches(secret, client.get_password("api_secret", raise_exception=False)):
        raise ApiError(401, "unauthenticated", "Wrong API secret.")
    if client.allowed_ips and not ip_allowed(getattr(frappe.local, "request_ip", None), client.allowed_ips):
        raise ApiError(403, "ip_not_allowed", "This source address is not allowed for this client.")
    rate_limit(f"client:{client.name}", cint(client.rate_limit_per_minute) or 60)
    _touch(client.name)
    return client


def rate_limit(bucket: str, per_minute: int) -> None:
    """Fixed one-minute window in Redis (site-prefixed key)."""
    cache = get_cache()
    key = cache.make_key(f"mpesa_suite:rl:{bucket}:{now_datetime().strftime('%Y%m%d%H%M')}")
    count = cache.incr(key)
    if count == 1:
        cache.expire(key, 70)
    if count > per_minute:
        raise ApiError(429, "rate_limited", "Too many requests; slow down and retry in a minute.")


def _touch(client_name: str) -> None:
    cache = get_cache()
    if cache.get_value(f"mpesa_suite:client_seen:{client_name}"):
        return
    cache.set_value(f"mpesa_suite:client_seen:{client_name}", 1, expires_in_sec=60)
    frappe.db.set_value(CLIENT, client_name, "last_used_on", now_datetime(), update_modified=False)


def require(client, flag: str, label: str) -> None:
    if not cint(client.get(flag)):
        raise ApiError(403, "not_allowed", f"This client is not allowed to {label}.")


def _clean(message) -> str:
    return re.sub(r"<[^>]+>", "", str(message or "")).strip() or "Request failed."


def endpoint(methods=("POST",), authenticated: bool = True):
    """Wrap a guest-whitelisted function as an API endpoint (put under @frappe.whitelist(allow_guest=True)).

    Authenticated endpoints receive the client as their first argument; a ``client`` sent by
    the caller is ignored.
    """

    def decorator(fn):
        params = list(inspect.signature(fn).parameters.values())
        if authenticated:
            params = params[1:]  # first parameter is the authenticated client
        accepts = {p.name for p in params}
        varkw = any(p.kind == p.VAR_KEYWORD for p in params)

        def fail(status: int, code: str, message: str) -> dict:
            frappe.clear_messages()
            frappe.local.response["http_status_code"] = status
            return {"ok": False, "error": {"code": code, "message": message}}

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for reserved in ("cmd", "client"):
                kwargs.pop(reserved, None)
            request = getattr(frappe.local, "request", None)
            frappe.db.savepoint("mpesa_api")  # undo only this call's writes on error
            try:
                if request is not None and request.method not in methods:
                    raise ApiError(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
                with as_integration_user():
                    call_kwargs = kwargs if varkw else {k: v for k, v in kwargs.items() if k in accepts}
                    if authenticated:
                        client = authenticate()
                        result = fn(client, *args, **call_kwargs)
                    else:
                        result = fn(*args, **call_kwargs)
                # GET requests are rolled back unless flagged; status checks may settle a payment
                frappe.local.flags.commit = True
                frappe.clear_messages()
                return {"ok": True, **(result or {})}
            except ApiError as e:
                if e.rollback:
                    undo()
                else:
                    frappe.local.flags.commit = True
                return fail(e.status, e.code, e.message)
            except frappe.PermissionError as e:
                undo()
                return fail(403, "not_allowed", _clean(e))
            except frappe.DoesNotExistError as e:
                undo()
                return fail(404, "not_found", _clean(e))
            except frappe.ValidationError as e:
                undo()
                return fail(422, "invalid_request", _clean(e))
            except Exception:
                undo()
                frappe.log_error(frappe.get_traceback(), f"M-Pesa Payments API: {fn.__name__}")
                return fail(500, "server_error", "Unexpected error; it has been logged.")

        return wrapper

    return decorator
