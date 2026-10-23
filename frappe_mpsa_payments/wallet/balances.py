"""Sub-account balances Safaricom reports, checked against the ERP's statement lines.

A balance is recorded from every Account Balance result (one per sub-account) and from the
last row of every imported statement. The statement's balance is the anchor: from it, the
lines the ERP holds for that sub-account since then (by Safaricom's completion time) say what
the next Account Balance should be. When it differs, money moved that the ERP has no line for
(a funding, a sweep to the bank, a transfer made on the portal, a payout whose result never
came): the balance is marked "Gap" with the amount, within minutes when balances are asked
for on a schedule. Importing the statement that holds the missing rows explains it.
"""

import datetime

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, now_datetime

from ..money_in.common import suite_int, tolerance
from . import accounts

SNAPSHOT = "Mpesa Balance Snapshot"
BT = "Bank Transaction"
# Results of payouts completed just before a balance may still be on their way.
QUIET_MINUTES = 10
# A balance still unchecked after this long (a payout stuck in flight) is checked anyway.
GIVE_UP_HOURS = 24
IN_FLIGHT = {"B2C Disbursement Request": ("Initiated", "Status Unknown"), "Mpesa B2B Payment Request": ("Initiated", "Unknown")}


# ---------------------------------------------------------------- recording
def record(settings, sub_account, balance, taken_at, source, reference_doctype=None, reference_name=None):
    if not sub_account or balance is None or not frappe.db.table_exists(SNAPSHOT):
        return None
    shortcode = cstr(frappe.db.get_value("Mpesa Settings", settings, "business_shortcode")).strip()
    taken_at = get_datetime(taken_at)
    key = {"shortcode": shortcode, "sub_account": sub_account, "taken_at": taken_at, "source": source}
    existing = frappe.db.get_value(SNAPSHOT, key, "name")
    if existing:
        return existing
    doc = frappe.new_doc(SNAPSHOT)
    doc.update({**key, "mpesa_settings": settings, "balance": flt(balance), "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "check_status": "Agrees" if source == "Statement" else "Not Checked",
                "note": "The statement's own balance: later balances are checked from it." if source == "Statement" else None})
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def parse_balances(text) -> dict:
    """{sub-account: current balance} from "Working Account|KES|0.00|0.00|0.00|0.00&Utility Account|KES|..."."""
    out = {}
    for part in cstr(text).split("&"):
        fields = part.split("|")
        sub = accounts.from_balance_text(part)
        if not sub or len(fields) < 3:
            continue
        try:
            out[sub] = float(fields[2].replace(",", ""))
        except ValueError:
            continue
    return out


def parse_time(value) -> datetime.datetime:
    """BOCompletedTime "20261002104324"; now when absent or unreadable."""
    text = cstr(value).strip()
    try:
        return datetime.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return now_datetime()


def from_account_balance(settings, account_balance, completed_time=None, reference_name=None) -> list:
    """Record each sub-account of an Account Balance result. Never raises: called from the callback."""
    try:
        taken_at = parse_time(completed_time)
        return [
            record(settings, sub, balance, taken_at, "Account Balance", "Integration Request", reference_name)
            for sub, balance in parse_balances(account_balance).items()
        ]
    except Exception:
        frappe.log_error(title=f"M-Pesa balance snapshot failed for {settings}", message=frappe.get_traceback())
        return []


# ---------------------------------------------------------------- checking
def check(name: str) -> str:
    """Compare one Account Balance snapshot with the statement anchor plus the lines since."""
    s = frappe.get_doc(SNAPSHOT, name)
    anchor = frappe.db.get_value(
        SNAPSHOT,
        {"shortcode": s.shortcode, "sub_account": s.sub_account, "source": "Statement",
         "taken_at": ["<=", s.taken_at]},
        ["name", "balance", "taken_at"],
        as_dict=True,
        order_by="taken_at desc",
    )
    if not anchor:
        return _set(s, "Cannot Check", note=f"Import a {s.sub_account} statement first: its balance is where the "
                    "lines are counted from.")
    bank_account = accounts.bank_account(s.mpesa_settings, s.sub_account)
    own = accounts.separate(s.mpesa_settings, s.sub_account)
    lines = _lines_sql(own)
    unknown = frappe.db.sql(
        f"""SELECT COUNT(*) FROM `tabBank Transaction` WHERE bank_account = %(ba)s AND docstatus = 1
        AND mpesa_completed_at IS NULL AND date BETWEEN %(from_date)s AND %(to_date)s AND {lines}""",
        {"ba": bank_account, "sub": s.sub_account, "from_date": get_datetime(anchor.taken_at).date(),
         "to_date": get_datetime(s.taken_at).date()},
    )[0][0]
    if unknown:
        return _set(s, "Cannot Check", anchor=anchor.name, note=(
            f"{unknown} line(s) on {bank_account} in that period have no M-Pesa completion time (made by hand or by "
            "an earlier script), so they cannot be placed before or after this balance."))
    moved = flt(frappe.db.sql(
        f"""SELECT SUM(deposit - withdrawal) FROM `tabBank Transaction` WHERE bank_account = %(ba)s AND docstatus = 1
        AND mpesa_completed_at > %(after)s AND mpesa_completed_at <= %(until)s AND {lines}""",
        {"ba": bank_account, "sub": s.sub_account, "after": anchor.taken_at, "until": s.taken_at},
    )[0][0])
    expected = round(flt(anchor.balance) + moved, 2)
    difference = round(flt(s.balance) - expected, 2)
    if abs(difference) <= tolerance():
        status = "Explained" if s.check_status == "Gap" else "Agrees"
        return _set(s, status, expected, difference, anchor.name)
    return _set(s, "Gap", expected, difference, anchor.name, note=(
        f"Safaricom shows {abs(difference):,.2f} {'more' if difference > 0 else 'less'} in {s.sub_account} than the "
        f"statement of {get_datetime(anchor.taken_at)} plus the lines since: money moved that has no line yet (a "
        "funding, a sweep to the bank, a transfer on the portal, a payout whose result never came). Import the "
        f"latest {s.sub_account} statement; this closes when its lines agree."))


def _lines_sql(own: bool) -> str:
    """Which lines on the Bank Account are this sub-account's: all of them on its own account
    (some may predate the sub-account stamp), else only those stamped with it."""
    return "(mpesa_sub_account = %(sub)s OR mpesa_sub_account IS NULL)" if own else "mpesa_sub_account = %(sub)s"


def _set(s, status, expected=None, difference=None, anchor=None, note=None) -> str:
    values = {"check_status": status, "anchor": anchor, "checked_on": now_datetime(), "note": note,
              # Currency columns hold no NULL: a check that compared nothing leaves them as they were
              **{k: v for k, v in (("expected_balance", expected), ("difference", difference)) if v is not None}}
    frappe.db.set_value(SNAPSHOT, s.name, values, update_modified=False)
    return status


def in_flight(settings, before) -> bool:
    """A payout sent before ``before`` whose result is not in: it may have moved the balance."""
    shortcode = cstr(frappe.db.get_value("Mpesa Settings", settings, "business_shortcode")).strip()
    wallet = [r.name for r in frappe.get_all("Mpesa Settings", fields=["name", "business_shortcode"])
              if cstr(r.business_shortcode).strip() == shortcode] or [settings]
    return any(
        frappe.get_all(doctype, filters=[[doctype, "mpesa_settings", "in", wallet], [doctype, "docstatus", "=", 1],
                                         [doctype, "status", "in", statuses], [doctype, "creation", "<=", before]],
                       pluck="name", limit=1)
        for doctype, statuses in IN_FLIGHT.items()
    )


def recheck(settings, sub_account=None) -> None:
    """After an import: look again at balances that showed a gap or could not be checked."""
    if not frappe.db.table_exists(SNAPSHOT):
        return
    filters = {"mpesa_settings": settings, "source": "Account Balance", "check_status": ["in", ("Gap", "Cannot Check")],
               "taken_at": [">=", add_to_date(now_datetime(), days=-30)]}
    if sub_account:
        filters["sub_account"] = sub_account
    for name in frappe.get_all(SNAPSHOT, filters=filters, pluck="name", order_by="taken_at asc", limit=200):
        check(name)


# ---------------------------------------------------------------- scheduler
def run() -> None:
    """Every 5 minutes: ask for balances when due, then check the ones whose payouts have settled."""
    if "erpnext" not in frappe.get_installed_apps() or not frappe.db.table_exists(SNAPSHOT):
        return
    ask_for_balances()
    frappe.db.commit()
    quiet = add_to_date(now_datetime(), minutes=-QUIET_MINUTES)
    for s in frappe.get_all(SNAPSHOT, filters={"source": "Account Balance", "check_status": "Not Checked",
                                               "taken_at": ["<=", quiet]},
                            fields=["name", "mpesa_settings", "taken_at"], order_by="taken_at asc", limit=200):
        stale = get_datetime(s.taken_at) < add_to_date(now_datetime(), hours=-GIVE_UP_HOURS)
        if not stale and in_flight(s.mpesa_settings, s.taken_at):
            continue
        frappe.db.savepoint("mpesa_balance_check")
        try:
            check(s.name)
        except Exception:
            frappe.db.rollback(save_point="mpesa_balance_check")
            frappe.log_error(title=f"M-Pesa balance check failed for {s.name}", message=frappe.get_traceback())
    frappe.db.commit()


def ask_for_balances() -> None:
    """Send an Account Balance query for each shortcode with an initiator, every N minutes
    (Ask Safaricom for Balances Every; 0 = never)."""
    minutes = suite_int("balance_check_minutes", 0)
    if minutes <= 0:
        return
    from ..frappe_mpsa_payments.api.m_pesa_api import get_account_balance

    since = add_to_date(now_datetime(), minutes=-minutes)
    for s in frappe.get_all("Mpesa Settings", fields=["name", "initiator_name", "security_credential"],
                            order_by="creation asc"):
        if not s.initiator_name or not s.security_credential:
            continue
        asked = frappe.db.exists("Integration Request", {"request_description": "Mpesa Balance Query",
                                                         "reference_docname": s.name, "creation": [">=", since]})
        if asked:
            continue
        try:
            get_account_balance(s.name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title=f"M-Pesa balance query for {s.name} failed", message=frappe.get_traceback())
