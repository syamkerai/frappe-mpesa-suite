"""Sub-account balances Safaricom reports, checked against the ERP's statement lines.

A balance is recorded from every Account Balance result (one per sub-account) and from the
last row of every imported statement. The statement's balance is the anchor: from it, the
lines the ERP holds for that sub-account since then (by Safaricom's completion time) say what
the next Account Balance should be. When it differs, money moved that the ERP has no line for
(a funding, a sweep to the bank, a transfer made on the portal, a payout whose result never
came): the balance is marked "Gap" with the amount, within minutes when balances are asked
for on a schedule. Importing the statement that holds the missing rows explains it.

With no statement imported (or Create Bank Transactions off), each balance is compared with the
sub-account's balance before it instead: what moved in between must be what the ERP's payout
records (B2B from Working, B2C from Utility, each with its charge) and receipts (C2B and STK
into the account that collects) say. Money the ERP did not send (a payment made on the M-Pesa
portal) shows as a "Gap" with its amount, and Notify Role is alerted; the same movement in
opposite directions on two sub-accounts of one result is a transfer between them. A reserved
amount with no ERP payout in flight is a warning that something is pending outside the ERP.

available() says, approximately, how much a sub-account can pay right now, for the payment
schedule's Pay Selected summary and the M-Pesa Payments Due report.
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
# How far back the balance before may be, for the movement check.
MOVEMENT_DAYS = 7
# Which payouts a sub-account pays, and the field of a B2C Result that gives its available funds.
PAID_FROM = {accounts.WORKING: "Mpesa B2B Payment Request", accounts.UTILITY: "B2C Disbursement Request"}
B2C_FUNDS = {accounts.WORKING: "working_acct_avlbl_funds", accounts.UTILITY: "utility_acct_avlbl_funds"}


# ---------------------------------------------------------------- recording
def record(settings, sub_account, balance, taken_at, source, reference_doctype=None, reference_name=None,
           available=None, reserved=None):
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
                **{k: flt(v) for k, v in (("available", available), ("reserved", reserved)) if v is not None},
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


def parse_parts(text) -> dict:
    """{sub-account: (current, available, reserved)} from "Utility Account|KES|4002.91|3902.91|100.00|0.00";
    available is the current balance, and reserved 0, when a result gives no more."""
    out = {}
    for part in cstr(text).split("&"):
        fields = [f.strip() for f in part.split("|")]
        sub = accounts.from_balance_text(part)
        if not sub or len(fields) < 3 or not fields[2]:
            continue
        try:
            current, available, reserved = (float(f.replace(",", "")) if f else None for f in (fields[2:5] + ["", ""])[:3])
        except ValueError:
            continue
        out[sub] = (current, current if available is None else available, reserved or 0.0)
    return out


def parse_available(text) -> dict:
    """{sub-account: available balance}: the fourth field of "Working Account|KES|current|available|reserved|...",
    the current balance when a result gives no more."""
    out = {}
    for part in cstr(text).split("&"):
        fields = part.split("|")
        sub = accounts.from_balance_text(part)
        if not sub or len(fields) < 3:
            continue
        try:
            out[sub] = float((fields[3] if len(fields) > 3 and fields[3].strip() else fields[2]).replace(",", ""))
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
        names = []
        for sub, (current, available, reserved) in parse_parts(account_balance).items():
            name = record(settings, sub, current, taken_at, "Account Balance", "Integration Request", reference_name,
                          available=available, reserved=reserved)
            if name and reserved > tolerance():
                warn_reserved(name)
            names.append(name)
        return names
    except Exception:
        frappe.log_error(title=f"M-Pesa balance snapshot failed for {settings}", message=frappe.get_traceback())
        return []


def warn_reserved(name: str) -> None:
    """A reserved amount while no ERP payout is in flight: something is pending outside the ERP
    (a payment started on the M-Pesa portal). Noted on the snapshot; Notify Role is alerted when
    the balance before had no such warning for the same amount (balances asked for every few
    minutes would repeat it)."""
    s = frappe.get_doc(SNAPSHOT, name)
    if s.get("warning") or in_flight(s.mpesa_settings, s.taken_at):
        return
    warning = (f"KES {flt(s.reserved):,.2f} of {s.sub_account} is reserved at Safaricom with no ERP payout in "
               "flight: something is pending outside the ERP (a payment started on the M-Pesa portal?).")
    frappe.db.set_value(SNAPSHOT, name, "warning", warning, update_modified=False)
    prev = previous(s)
    if prev and frappe.db.get_value(SNAPSHOT, prev.name, "warning") == warning:
        return
    _alert(f"M-Pesa {s.shortcode}: {warning}", s)


def _alert(message: str, s) -> None:
    from ..money_out.party_accounts import alert

    alert(message, s)  # Notify Role on M-Pesa Suite Settings; never raises


# ---------------------------------------------------------------- checking
def check(name: str) -> str:
    """Compare one Account Balance snapshot with the statement anchor plus the lines since, or,
    with no statement imported, with the balance before it plus what the ERP moved since."""
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
        return check_movement(s)
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


def check_movement(s) -> str:
    """With no statement to count from: the movement since the sub-account's balance before this
    one (within MOVEMENT_DAYS), against what the ERP moved it by (erp_movement)."""
    prev = previous(s)
    if not prev:
        return _set(s, "Cannot Check", note=(
            f"Import a {s.sub_account} statement, or ask for balances again: there is no earlier {s.sub_account} "
            f"balance in the last {MOVEMENT_DAYS} days to compare this one with."))
    moved = erp_movement(s.mpesa_settings, s.sub_account, prev.taken_at, s.taken_at)
    stale = get_datetime(s.taken_at) < add_to_date(now_datetime(), hours=-GIVE_UP_HOURS)
    if moved.waiting and not stale:
        return "Not Checked"  # a payout's charge a status query may still give: checked on a later run
    seen = round(flt(s.balance) - flt(prev.balance), 2)
    expected = round(flt(prev.balance) + moved.amount, 2)
    difference = round(flt(s.balance) - expected, 2)
    links = {"previous_snapshot": prev.name, "erp_movement": moved.amount}
    if abs(difference) <= tolerance():
        status = "Explained" if s.check_status == "Gap" else "Agrees"
        return _set(s, status, expected, difference, note=moved.note or None, **links)
    partner = transfer_partner(s, difference)
    if partner:
        return _set(s, "Explained", expected, difference, note=(
            f"{s.sub_account} moved {difference:+,.2f} and {partner} {-difference:+,.2f} more than the ERP recorded, "
            f"in the same result: a transfer between them (on the M-Pesa portal), nothing left the shortcode. If "
            f"they post to different Bank Accounts, book it as an Internal Transfer. {moved.note}").strip(), **links)
    note = (f"{s.sub_account} moved {seen:+,.2f} since {get_datetime(prev.taken_at)}; the ERP recorded "
            f"{moved.amount:+,.2f}; {difference:+,.2f} was not sent or received by the ERP (a transaction on the M-Pesa "
            f"portal, a funding, a sweep to the bank, a reversal). {moved.note}").strip()
    _set(s, "Gap", expected, difference, note=note, **links)
    _alert(f"M-Pesa {s.shortcode}: {note}", s)
    return "Gap"


def previous(s):
    """The sub-account's Account Balance before ``s`` (MOVEMENT_DAYS at most), or None."""
    prev = frappe.db.get_value(
        SNAPSHOT,
        {"shortcode": s.shortcode, "sub_account": s.sub_account, "source": "Account Balance",
         "taken_at": ["<", s.taken_at]},
        ["name", "balance", "taken_at"],
        as_dict=True,
        order_by="taken_at desc",
    )
    if not prev or get_datetime(prev.taken_at) < get_datetime(s.taken_at) - datetime.timedelta(days=MOVEMENT_DAYS):
        return None
    return prev


def transfer_partner(s, difference: float) -> str | None:
    """Another sub-account of the same Account Balance result whose unexplained movement is the
    opposite of this one's: money moved between the two."""
    for other in frappe.get_all(
        SNAPSHOT,
        filters={"shortcode": s.shortcode, "source": "Account Balance", "taken_at": s.taken_at,
                 "sub_account": ["!=", s.sub_account]},
        fields=["name", "mpesa_settings", "sub_account", "balance", "taken_at"],
    ):
        prev = previous(other)
        if not prev:
            continue
        moved = erp_movement(other.mpesa_settings, other.sub_account, prev.taken_at, other.taken_at)
        other_difference = round(flt(other.balance) - flt(prev.balance) - moved.amount, 2)
        if abs(other_difference + difference) <= tolerance():
            return other.sub_account
    return None


def erp_movement(settings, sub_account, after, until):
    """_dict(amount, waiting, note): what the ERP's records moved ``sub_account`` of the shortcode by
    between two balances (after < completed <= until). Paid B2B leave Working and Paid B2C leave
    Utility, each with its charge (stated, booked, else the B2C Charge Bands); C2B receipts and
    STK payments arrive in the sub-account that collects. ``waiting``: a B2B charge is not known
    yet and a status query may still give it."""
    from ..money_out.b2b_status import still_asking
    from ..money_out.statement import B2B, B2C, band_charge, charge_bands, parse_charges

    wallet = _wallet(settings)
    amount, waiting, unknown = 0.0, False, []
    if sub_account in PAID_FROM:
        doctype = PAID_FROM[sub_account]
        completed = "trans_completed_time" if doctype == B2B else "transaction_completed_datetime"
        fields = ["name", "amount", *(["debit_party_charges"] if doctype == B2B else []),
                  *(["mpesa_charge"] if frappe.get_meta(doctype).has_field("mpesa_charge") else [])]
        bands = charge_bands() if doctype == B2C else []
        for r in frappe.get_all(
            doctype,
            filters=[[doctype, "mpesa_settings", "in", wallet], [doctype, "docstatus", "in", [1, 2] if doctype == B2B else [1]],
                     [doctype, "status", "=", "Paid"], [doctype, completed, ">", after], [doctype, completed, "<=", until]],
            fields=fields,
        ):
            if cstr(r.get("debit_party_charges")).strip():
                charge = parse_charges(r.debit_party_charges)
            elif flt(r.get("mpesa_charge")):
                charge = flt(r.mpesa_charge)
            elif doctype == B2C:
                charge = flt(band_charge(bands, r.amount)) if bands else 0.0
            else:
                charge = 0.0
                unknown.append(r.name)
                waiting = waiting or still_asking(r.name)
            amount -= flt(r.amount) + charge
    if sub_account == accounts.receiving(settings):
        amount += _received(settings, wallet, after, until)
    note = f"No charge is known yet for {', '.join(unknown)}: counted as 0." if unknown else ""
    return frappe._dict(amount=round(amount, 2), waiting=waiting, note=note)


def _received(settings, wallet, after, until) -> float:
    """Customer payments the ERP holds for the shortcode between two balances: C2B receipts, and
    STK payments that no C2B receipt carries."""
    from ..money_in.common import EXPRESS, REGISTER

    row = frappe.db.get_value("Mpesa Settings", settings, ["business_shortcode", "till_number"], as_dict=True) or {}
    codes = [cstr(v).strip() for v in row.values() if cstr(v).strip()]
    stamp = "%Y%m%d%H%M%S"
    registers = frappe.get_all(
        REGISTER,
        filters=[[REGISTER, "businessshortcode", "in", codes or [""]], [REGISTER, "docstatus", "<", 2],
                 [REGISTER, "transtime", ">", get_datetime(after).strftime(stamp)],
                 [REGISTER, "transtime", "<=", get_datetime(until).strftime(stamp)]],
        fields=["transid", "transamount"],
    )
    total = sum(flt(r.transamount) for r in registers)
    receipts = {cstr(r.transid).strip().upper() for r in registers}
    for r in frappe.get_all(
        EXPRESS,
        filters=[[EXPRESS, "settings", "in", wallet], [EXPRESS, "status", "=", "Completed"],
                 [EXPRESS, "transaction_date", ">", after], [EXPRESS, "transaction_date", "<=", until]],
        fields=["transaction_id", "amount"],
    ):
        if cstr(r.transaction_id).strip().upper() not in receipts:
            total += flt(r.amount)
    return total


def _set(s, status, expected=None, difference=None, anchor=None, note=None, **links) -> str:
    values = {"check_status": status, "anchor": anchor, "checked_on": now_datetime(), "note": note, **links,
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
    from ..mpsa_suite.api.m_pesa_api import get_account_balance

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


# ---------------------------------------------------------------- what can be paid now (approximate)
def available(settings, sub_account=accounts.WORKING):
    """_dict(amount, as_of, source, pending, after, approximate): about how much the sub-account
    can pay now. The newest balance Safaricom reported (an Account Balance result, a B2B Result or
    a B2C Result), less payouts of this shortcode sent after it. Only approximate: balances arrive
    late, Utility is funded by hand on the portal and charges come off too, so a payment above the
    real balance is failed by Safaricom (no money is lost)."""
    wallet = _wallet(settings)
    found = [b for b in (_from_balance_query(settings, sub_account), _from_b2b(wallet, sub_account),
                         _from_b2c(wallet, sub_account)) if b]
    if not found:
        return frappe._dict(amount=None, as_of=None, source=None, pending=0.0, after=None, approximate=True)
    newest = max(found, key=lambda b: get_datetime(b.as_of) if b.as_of else datetime.datetime.min)
    as_of = get_datetime(newest.as_of) if newest.as_of else None
    pending = _sent_since(wallet, sub_account, as_of)
    return frappe._dict(amount=flt(newest.amount, 2), as_of=as_of, source=newest.source, pending=pending,
                        after=flt(flt(newest.amount) - pending, 2), approximate=True)


def _wallet(settings) -> list:
    """Mpesa Settings records of the same shortcode (a sandbox copy, a till on the same account)."""
    shortcode = cstr(frappe.db.get_value("Mpesa Settings", settings, "business_shortcode")).strip()
    rows = frappe.get_all("Mpesa Settings", fields=["name", "business_shortcode"], order_by="creation asc")
    names = [r.name for r in rows if shortcode and cstr(r.business_shortcode).strip() == shortcode]
    return names or [settings]


def _from_balance_query(settings, sub_account):
    """The last Account Balance result stored on Mpesa Settings; its time is that of the balance
    snapshot recorded from the same result."""
    amount = parse_available(frappe.db.get_value("Mpesa Settings", settings, "account_balance")).get(sub_account)
    if amount is None:
        return None
    as_of = None
    if frappe.db.table_exists(SNAPSHOT):
        shortcode = cstr(frappe.db.get_value("Mpesa Settings", settings, "business_shortcode")).strip()
        as_of = frappe.db.get_value(SNAPSHOT, {"shortcode": shortcode, "sub_account": sub_account,
                                               "source": "Account Balance"}, "taken_at", order_by="taken_at desc")
    return frappe._dict(amount=amount, as_of=as_of, source="Account Balance")


def _from_b2b(wallet, sub_account):
    """The balance the newest B2B Result reported (it names the account it paid from)."""
    if sub_account != accounts.WORKING:
        return None
    for row in frappe.db.sql(
        """
        SELECT debit_account_balance, trans_completed_time FROM `tabMpesa B2B Payment Request`
        WHERE mpesa_settings IN %(wallet)s AND status = 'Paid' AND trans_completed_time IS NOT NULL
          AND COALESCE(debit_account_balance, '') != ''
        ORDER BY trans_completed_time DESC LIMIT 5
        """,
        {"wallet": tuple(wallet)},
        as_dict=True,
    ):
        amount = parse_available(row.debit_account_balance).get(sub_account)
        if amount is not None:
            return frappe._dict(amount=amount, as_of=row.trans_completed_time, source="B2B Result")
    return None


def _from_b2c(wallet, sub_account):
    """The available funds the newest B2C Result reported for the sub-account."""
    field = B2C_FUNDS.get(sub_account)
    if not field:
        return None
    rows = frappe.db.sql(
        f"""
        SELECT `{field}` AS funds, transaction_completed_datetime AS completed FROM `tabB2C Disbursement Request`
        WHERE mpesa_settings IN %(wallet)s AND transaction_completed_datetime IS NOT NULL AND `{field}` IS NOT NULL
        ORDER BY transaction_completed_datetime DESC LIMIT 1
        """,
        {"wallet": tuple(wallet)},
        as_dict=True,
    )
    if not rows:
        return None
    return frappe._dict(amount=flt(rows[0].funds), as_of=rows[0].completed, source="B2C Result")


def _sent_since(wallet, sub_account, as_of) -> float:
    """Payouts from the sub-account still on their way that were made after the balance."""
    doctype = PAID_FROM.get(sub_account)
    if not doctype:
        return 0.0
    since = "AND creation > %(as_of)s" if as_of else ""
    total = frappe.db.sql(
        f"""
        SELECT SUM(amount) FROM `tab{doctype}`
        WHERE mpesa_settings IN %(wallet)s AND docstatus = 1 AND status IN %(statuses)s {since}
        """,
        {"wallet": tuple(wallet), "statuses": ("Not Initiated", *IN_FLIGHT[doctype]), "as_of": as_of},
    )
    return flt(total[0][0] if total else 0, 2)
