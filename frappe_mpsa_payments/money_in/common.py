"""Shared helpers for the money-in pipeline."""

import datetime
import hashlib
import re
from contextlib import contextmanager

import frappe
from frappe.utils import cint, cstr, flt, getdate

SUITE = "M-Pesa Suite Settings"
REGISTER = "Mpesa C2B Payment Register"
EXPRESS = "Mpesa Express Request"
MATCH_LOG = "Mpesa Match Log"


# ---------------------------------------------------------------- settings
def suite():
    return frappe.get_cached_doc(SUITE)


def enabled(flag: str) -> bool:
    try:
        return bool(cint(suite().get(flag)))
    except frappe.DoesNotExistError:
        return False


def tolerance() -> float:
    try:
        value = suite().get("amount_tolerance")
    except frappe.DoesNotExistError:
        return 1.0
    return 1.0 if value in (None, "") else flt(value)


def suite_int(field: str, default: int) -> int:
    try:
        return cint(suite().get(field)) or default
    except frappe.DoesNotExistError:
        return default


def erpnext_installed() -> bool:
    return "erpnext" in frappe.get_installed_apps()


# ---------------------------------------------------------------- shortcode -> books
def settings_for_shortcode(shortcode) -> str | None:
    """Mpesa Settings name for a paybill/till number (business shortcode or till)."""
    code = cstr(shortcode).strip()
    if not code:
        return None
    for field in ("business_shortcode", "till_number"):
        name = frappe.db.get_value(
            "Mpesa Settings", {field: code}, "name", order_by="creation asc"
        )
        if name:
            return name
    # tolerate stray spaces stored on the till (seen in production data)
    rows = frappe.get_all(
        "Mpesa Settings", fields=["name", "business_shortcode", "till_number"], order_by="creation asc"
    )
    for row in rows:
        if code in (cstr(row.business_shortcode).strip(), cstr(row.till_number).strip()):
            return row.name
    return None


def mode_of_payment_for(settings_name: str) -> str | None:
    mop = f"Mpesa-{settings_name}"
    return mop if frappe.db.exists("Mode of Payment", mop) else None


def bank_account_for(settings_name: str | None) -> str | None:
    """Company Bank Account that represents this shortcode.

    1. the Bank Account mapped on Mpesa Settings (Accounting Mapping section)
    2. otherwise the company Bank Account whose GL is the Mode of Payment's default account
    """
    if not settings_name:
        return None
    meta = frappe.get_meta("Mpesa Settings")
    values = frappe.db.get_value(
        "Mpesa Settings",
        settings_name,
        [f for f in ("bank_account", "company") if meta.has_field(f)],
        as_dict=True,
    ) or frappe._dict()
    if values.get("bank_account"):
        return values.bank_account

    mop = mode_of_payment_for(settings_name)
    if not mop:
        return None
    filters = {"parent": mop}
    if values.get("company"):
        filters["company"] = values.company
    gl = frappe.db.get_value("Mode of Payment Account", filters, "default_account")
    if not gl:
        return None
    return frappe.db.get_value(
        "Bank Account", {"account": gl, "is_company_account": 1}, "name", order_by="creation asc"
    )


def mpesa_bank_accounts() -> set:
    """All company Bank Accounts that carry M-Pesa money (mapped or bank named like M-Pesa)."""
    cached = getattr(frappe.local, "mpesa_suite_bank_accounts", None)
    if cached is not None:
        return cached
    accounts = set()
    for name in frappe.get_all("Mpesa Settings", pluck="name", order_by="creation asc"):
        ba = bank_account_for(name)
        if ba:
            accounts.add(ba)
    accounts.update(
        frappe.get_all(
            "Bank Account",
            filters={"is_company_account": 1, "bank": ["like", "%pesa%"]},
            pluck="name",
            order_by="creation asc",
        )
    )
    frappe.local.mpesa_suite_bank_accounts = accounts  # per request / job
    return accounts


def mpesa_gl_accounts() -> set:
    gls = set()
    for ba in mpesa_bank_accounts():
        gl = frappe.db.get_value("Bank Account", ba, "account")
        if gl:
            gls.add(gl)
    for mop in frappe.get_all("Mode of Payment", filters={"name": ["like", "Mpesa-%"]}, pluck="name", order_by="name asc"):
        gls.update(
            a for a in frappe.get_all("Mode of Payment Account", filters={"parent": mop}, pluck="default_account", order_by="idx asc") if a
        )
    return gls


# ---------------------------------------------------------------- time / text
def parse_transtime(value) -> datetime.datetime | None:
    """Daraja TransTime / TransactionDate: 20261018143005 (also tolerates ISO strings)."""
    text = cstr(value).strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 14:
        try:
            return datetime.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except ValueError:
            pass
    try:
        return frappe.utils.get_datetime(text) if text else None
    except Exception:
        return None


def receipt_date(register) -> datetime.date:
    when = parse_transtime(register.get("transtime"))
    return when.date() if when else getdate(register.get("posting_date"))


def compact(value) -> str:
    """Upper-case letters and digits only: 'sinv-26 00123' -> 'SINV2600123'."""
    return re.sub(r"[^A-Za-z0-9]", "", cstr(value)).upper()


# ---------------------------------------------------------------- concurrency / users
@contextmanager
def named_lock(key: str, timeout: int = 10):
    """Cross-worker mutex (MariaDB GET_LOCK) for idempotent inserts keyed by receipt."""
    if frappe.db.db_type != "mariadb":
        yield
        return
    lock = "mpesa:" + hashlib.sha1(key.encode()).hexdigest()  # GET_LOCK names max 64 chars
    got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock, timeout))[0][0]
    if not got:
        frappe.throw(f"Could not acquire lock {lock}")
    try:
        yield
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock,))


@contextmanager
def as_user(user: str | None):
    previous = frappe.session.user
    if user and user != previous:
        frappe.set_user(user)
    try:
        yield
    finally:
        if frappe.session.user != previous:
            frappe.set_user(previous)


# ---------------------------------------------------------------- audit
def log_match(result: str, method: str = None, register=None, express=None, once: bool = False, **fields) -> None:
    """Write a Mpesa Match Log row; never let logging break the money flow.

    ``once``: skip if this receipt already has a row with the same result (used by the
    2-minute matcher so an unplaced receipt is logged once, not every pass).
    """
    if not frappe.db.exists("DocType", MATCH_LOG):
        return
    if once:
        key = {"register": register.name} if register is not None else (
            {"reference_name": fields.get("reference_name")} if fields.get("reference_name") else None
        )
        if key and frappe.db.exists(MATCH_LOG, {**key, "result": result}):
            return
    try:
        row = frappe.new_doc(MATCH_LOG)
        row.result = result
        row.method = method
        if register is not None:
            row.register = register.name
            row.receipt = register.get("transid")
            row.amount = flt(register.get("transamount"))
        if express is not None:
            row.express_request = express.name
            row.receipt = row.receipt or express.get("transaction_id")
            row.amount = row.amount or flt(express.get("amount"))
        for key, value in fields.items():
            if value is not None:
                row.set(key, value if not isinstance(value, (list, tuple)) else ", ".join(map(str, value)))
        row.flags.ignore_permissions = True
        row.insert()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Mpesa Match Log write failed")
