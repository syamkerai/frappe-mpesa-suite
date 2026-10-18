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
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


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

    Authenticated endpoints receive the client as their first argument.
    """

    def decorator(fn):
        params = inspect.signature(fn).parameters
        accepts = {p.name for p in params.values()}
        varkw = any(p.kind == p.VAR_KEYWORD for p in params.values())

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            kwargs.pop("cmd", None)
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
                return {"ok": True, **(result or {})}
            except ApiError as e:
                frappe.db.rollback(save_point="mpesa_api")
                frappe.local.response["http_status_code"] = e.status
                return {"ok": False, "error": {"code": e.code, "message": e.message}}
            except (frappe.ValidationError, frappe.DoesNotExistError, frappe.PermissionError) as e:
                frappe.db.rollback(save_point="mpesa_api")
                frappe.local.response["http_status_code"] = 404 if isinstance(e, frappe.DoesNotExistError) else 422
                frappe.clear_messages()
                return {"ok": False, "error": {"code": "invalid_request", "message": _clean(e)}}
            except Exception:
                frappe.db.rollback(save_point="mpesa_api")
                frappe.log_error(frappe.get_traceback(), f"M-Pesa Payments API: {fn.__name__}")
                frappe.local.response["http_status_code"] = 500
                return {"ok": False, "error": {"code": "server_error", "message": "Unexpected error; it has been logged."}}

        return wrapper

    return decorator
