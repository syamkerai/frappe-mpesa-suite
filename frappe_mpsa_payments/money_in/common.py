"""Shared helpers for the money-in pipeline."""

import datetime
import hashlib
import html
import re
from contextlib import contextmanager

import frappe
from frappe.utils import cint, cstr, flt, getdate, now_datetime

from ..daraja import apis

SUITE = "M-Pesa Suite Settings"
REGISTER = "Mpesa C2B Payment Register"
EXPRESS = "Mpesa Express Request"
MATCH_LOG = "Mpesa Match Log"
REVERSAL = "Mpesa Reversal Request"
# A submitted reversal in one of these states means the money is, or may be, back with the
# payer (money_out.reversal.LIVE). Failed or cancelled ones leave the receipt bookable.
REVERSAL_HOLDS = ("Not Initiated", "Initiated", "Unknown", "Reversed", "Already Reversed")
# frappe.flags key naming the M-Pesa receipt an STK booking is booking right now (set by
# utils.handle_successful_transaction, read by gateway.intents)
BOOKING_RECEIPT_FLAG = "mpesa_booking_receipt"


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
    """Mpesa Settings name for a paybill/till number (business shortcode or till).

    One shortcode may do everything (Safaricom "Business One Account": Paybill, STK, B2C and
    B2B on one number), ideally as one record. When several records share the number, money
    received is booked with a record that collects (C2B or STK ticked) before one that only
    pays out, then the oldest, so the choice never depends on database order.
    """
    code = cstr(shortcode).strip()
    if not code:
        return None
    rows = frappe.get_all(
        "Mpesa Settings",
        fields=["name", "business_shortcode", "till_number", *apis.COLLECTING],
        order_by="creation asc",
    )
    # strip(): tolerate stray spaces stored on the till (seen in production data)
    matches = [r for r in rows if code in (cstr(r.business_shortcode).strip(), cstr(r.till_number).strip())]
    if not matches:
        return None
    matches.sort(key=lambda r: 0 if apis.collects(r) else 1)  # stable: oldest first otherwise
    return matches[0].name


def records_sharing_shortcode(name: str, shortcodes) -> list:
    """Other Mpesa Settings using any of ``shortcodes`` (for the save-time warning)."""
    codes = {cstr(c).strip() for c in shortcodes if cstr(c).strip()}
    if not codes:
        return []
    rows = frappe.get_all("Mpesa Settings", fields=["name", "business_shortcode", "till_number"])
    return [
        r.name
        for r in rows
        if r.name != name and codes & {cstr(r.business_shortcode).strip(), cstr(r.till_number).strip()}
    ]


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


# ---------------------------------------------------------------- reversals
# A reversal is raised on one register and carries its receipt (transaction_id). Both keys
# count: a register cancelled and amended after its reversal is a new name for the same money,
# and an STK receipt may have no live register at all (upstream cancels or deletes it).
def _live(**filters) -> dict:
    return {**filters, "docstatus": 1, "status": ["in", REVERSAL_HOLDS]}


def _live_reversal(filters: dict, for_update: bool = False):
    return frappe.db.get_value(
        REVERSAL,
        _live(**filters),
        ["name", "status"],
        as_dict=True,
        order_by="creation asc",
        for_update=for_update,
    )


def receipt_reversal(receipt, for_update: bool = False):
    """The reversal that keeps this M-Pesa receipt (by its code) from being used (name, status), or None."""
    receipt = cstr(receipt).strip()
    return _live_reversal({"transaction_id": receipt}, for_update) if receipt else None


def reversal_holding(register_name: str | None, for_update: bool = False, receipt: str | None = None):
    """The reversal that keeps this receipt from being booked (name, status), or None.

    Booking a receipt whose money went back to the payer would credit the customer (or pay
    an order) with refunded money. Found by the register it was raised on or by the receipt
    (``receipt``: the register's transid when the caller has it; otherwise it is read).
    """
    held = _live_reversal({"c2b_register": register_name}, for_update) if register_name else None
    if held:
        return held
    if receipt is None and register_name:
        receipt = frappe.db.get_value(REGISTER, register_name, "transid")
    return receipt_reversal(receipt, for_update)


def reversed_registers(names=None) -> list:
    """Registers held back by a reversal: all of them, or those among ``names``. Held: the
    register a reversal was raised on, and any register carrying its receipt.

    Never contains None: callers filter with ``name not in``, and on v16 a NULL in that list
    makes the condition match no row at all (the matcher would pick up nothing).
    """
    if names is None:
        rows = frappe.get_all(REVERSAL, filters=_live(), fields=["c2b_register", "transaction_id"],
                              order_by="creation asc")
        held = [r.c2b_register for r in rows]
        receipts = list(dict.fromkeys(cstr(r.transaction_id).strip() for r in rows if cstr(r.transaction_id).strip()))
        if receipts:
            held += frappe.get_all(REGISTER, filters={"transid": ["in", receipts]}, pluck="name", order_by="creation asc")
        return list(dict.fromkeys(n for n in held if n))

    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return []
    by_register = set(frappe.get_all(REVERSAL, filters=_live(c2b_register=["in", names]),
                                     pluck="c2b_register", order_by="creation asc"))
    receipt_of = {
        r.name: cstr(r.transid).strip()
        for r in frappe.get_all(REGISTER, filters={"name": ["in", names]}, fields=["name", "transid"], order_by="creation asc")
    }
    receipts = sorted({c for c in receipt_of.values() if c})
    by_receipt = {
        cstr(c).strip()
        for c in frappe.get_all(REVERSAL, filters=_live(transaction_id=["in", receipts]), pluck="transaction_id",
                                order_by="creation asc")
    } if receipts else set()
    return [n for n in names if n in by_register or receipt_of.get(n) in by_receipt]


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
    """Cross-worker mutex (MariaDB GET_LOCK) for idempotent inserts keyed by receipt.

    Held until this transaction commits or rolls back, not just to the end of the block:
    released earlier, a second worker could take it, miss our uncommitted insert and
    insert a duplicate. Re-entering the same key in one transaction is a no-op.
    """
    if frappe.db.db_type != "mariadb":
        yield
        return
    lock = "mpesa:" + hashlib.sha1(key.encode()).hexdigest()  # GET_LOCK names max 64 chars
    held = _held_locks()
    if lock not in held:
        got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock, timeout))[0][0]
        if not got:
            frappe.throw(f"Could not acquire lock {lock}")
        if not held:
            # commit resets the rollback callbacks and vice versa, so exactly one runs
            frappe.db.after_commit.add(release_named_locks)
            frappe.db.after_rollback.add(release_named_locks)
        held.add(lock)
    yield


def _held_locks() -> set:
    if getattr(frappe.local, "mpesa_named_locks", None) is None:
        frappe.local.mpesa_named_locks = set()
    return frappe.local.mpesa_named_locks


def release_named_locks() -> None:
    held = getattr(frappe.local, "mpesa_named_locks", None) or set()
    frappe.local.mpesa_named_locks = set()
    for lock in held:
        try:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock,))
        except Exception:
            pass  # connection gone: MariaDB released it with the session


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
def log_match(
    result: str, method: str = None, register=None, express=None, once: bool = False, on_change: bool = False, **fields
) -> None:
    """Write a Mpesa Match Log row; never let logging break the money flow.

    ``once``: skip if this receipt already has a row with the same result (used by the
    2-minute matcher so an unplaced receipt is logged once, not every pass).
    ``on_change``: skip only if the latest row with the same result says the same thing, and
    mark that row as seen again (its ``modified``); a different reason still gets its row.
    """
    if not frappe.db.exists("DocType", MATCH_LOG):
        return
    key = {"register": register.name} if register is not None else (
        {"reference_name": fields.get("reference_name")} if fields.get("reference_name") else None
    )
    if once and key and frappe.db.exists(MATCH_LOG, {**key, "result": result}):
        return
    try:
        if on_change and key:
            last = frappe.db.get_value(
                MATCH_LOG, {**key, "result": result}, ["name", "details"], as_dict=True, order_by="creation desc"
            )
            if last and _plain(last.details) == _plain(fields.get("details")):
                frappe.db.set_value(MATCH_LOG, last.name, "modified", now_datetime(), update_modified=False)
                return
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


def _plain(text) -> str:
    """Details as compared by ``on_change``: the stored copy may have had its HTML sanitised."""
    from frappe.utils import strip_html

    return " ".join(strip_html(html.unescape(cstr(text))).split())
