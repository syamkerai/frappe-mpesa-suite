"""M-Pesa Payment Schedule: B2B payments planned ahead and paid on their day after a confirm step.

Accounts prepare the lines (who, how much, which invoice or order, which day) and may move a
line's day until it is sent. On the day, an M-Pesa Payer or Manager ticks lines in the "M-Pesa
Payments Due" report and presses Pay Selected. prepare_payment shows a summary per payee and per
shortcode with the balances; confirm_payment pays only with the token it issued, at least 5
seconds later, once, for the same user (the browser keeps Confirm disabled for those 5 seconds
too). Nothing is ever paid automatically.

Rules on top of the B2B request's own (money_out.b2b):
- no line is sent before its planned day (site time zone), whatever the route: confirm_payment,
  the request's submit (before_submit), send_b2b and send_deferred all refuse (assert_line_due);
- the payer must not be who prepared or last changed the line (maker-checker), unless the Suite
  setting is off;
- a planned line counts in owed.left_to_pay, so its document cannot also be paid past what is left;
- confirm_payment submits the B2B requests but sends none: send_batch sends them one at a time
  from a job, each with a send_started_on marker committed before the call to M-Pesa (R1). The
  5-minute sweep makes a marked request still Not Initiated after 10 minutes Unknown (never sent
  again); an unmarked one is sent again within the hour, then cancelled and its line planned again.

Lock order: the document's lock, the schedule row, its line rows, then the payout rows and the
document row (R4). A Result touches only its line; the schedule's totals follow in a job.
"""

import contextlib
import re
import time

import frappe
from frappe.utils import add_days, add_to_date, cint, cstr, flt, get_datetime, getdate, now_datetime, nowdate

from ..compat import get_cache
from ..daraja import money_out as daraja
from ..money_in import common as money_in_common
from ..permissions import ADMIN_ROLES, FINANCE_ROLES, PAYOUT_ROLES, require
from ..wallet import accounts as wallet_accounts
from ..wallet import balances
from . import owed, supplier_accounts

SCHEDULE = "M-Pesa Payment Schedule"
LINE = "M-Pesa Payment Schedule Line"
B2B = "Mpesa B2B Payment Request"
PI, PO = "Purchase Invoice", "Purchase Order"
PLANNED, SENDING, SENT, PAID, FAILED, UNKNOWN, CANCELLED = (
    "Planned", "Sending", "Sent", "Paid", "Failed", "Unknown", "Cancelled")
IN_PROGRESS = (SENDING, SENT, UNKNOWN)
AUTO, LATER = "Book automatically", "Make the entry later"
# The approved-account DocType of each party type (Employee M-Pesa Account comes with payroll).
PARTY_ACCOUNT = {"Supplier": supplier_accounts.ACCOUNT_DOCTYPE}
B2B_PAYBILL, B2B_TILL = "B2B Paybill", "B2B Till"
ROUTE_FOR = {supplier_accounts.PAYBILL: B2B_PAYBILL, supplier_accounts.TILL: B2B_TILL}
# The sub-account each route pays from (B2C phone lines would pay from Utility).
SUB_ACCOUNT = {B2B_PAYBILL: wallet_accounts.WORKING, B2B_TILL: wallet_accounts.WORKING}
# Confirm works this many seconds after the summary was made, for this long.
CONFIRM_DELAY = 5
TOKEN_SECONDS = 600
# A request whose send started this long ago and is still Not Initiated may have reached M-Pesa.
INTERRUPTED_MINUTES = 10
# A submitted request never started is sent again from this age until RESEND_WITHIN, then given up.
RESEND_AFTER_MINUTES, RESEND_WITHIN_MINUTES = 5, 60
INTERRUPTED = "Sending was interrupted after it may have reached M-Pesa. Check the statement before paying again."
CHANGED = "It changed after you pressed Pay Selected. Press Pay Selected again."
# Fields staff type; a line that left Planned keeps them as they were.
EDITABLE = ("planned_date", "party_type", "party", "payee_name", "payee_account", "one_off_type",
            "one_off_shortcode", "one_off_account", "amount", "reference_doctype", "reference_name",
            "entry_mode", "remarks")
# Written only by the server; always restored from the database on save.
SYSTEM = ("status", "request", "receipt", "paid_on", "entry_status", "last_problem", "prepared_by", "changed_by",
          "approved_by", "approved_on", "batch", "attempts")
# Copied from the approved account (or the one-off number) on save.
DERIVED = ("payee_account_doctype", "route", "destination")


def _clock() -> float:
    """Seconds, for the confirm delay (replaced in tests)."""
    return time.time()


def _today():
    return getdate(nowdate())


def _day(value) -> str:
    """'23 Oct 2026'."""
    day = getdate(value)
    return f"{day.day} {day:%b %Y}"


def _plain(text) -> str:
    return re.sub(r"<[^>]+>", "", cstr(text)).strip()


def _forget_message() -> None:
    """A message frappe.throw queued for the browser, for an error handled here instead."""
    log = getattr(frappe.local, "message_log", None)
    if log:
        frappe.local.message_log = log[:-1]


def _norm(field, value):
    if field == "planned_date":
        return cstr(getdate(value)) if value else ""
    if field in ("amount", "attempts"):
        return flt(value, 2)
    return cstr(value).strip()


def _document(line) -> tuple:
    dt = cstr(line.get("reference_doctype")).strip()
    dn = cstr(line.get("reference_name")).strip()
    return (dt, dn) if dt and dn else (None, None)


def maker_checker_on() -> bool:
    """The Suite setting, on unless switched off (a site not yet migrated reads it as on)."""
    try:
        value = money_in_common.suite().get("schedule_maker_checker")
    except frappe.DoesNotExistError:
        return True
    return True if value in (None, "") else bool(cint(value))


def _names(lines) -> list:
    if isinstance(lines, str):
        lines = frappe.parse_json(lines) if lines.strip().startswith("[") else [lines]
    return list(dict.fromkeys(cstr(n).strip() for n in lines or [] if cstr(n).strip()))


# ---------------------------------------------------------------- reading
LINE_FIELDS = ("name", "parent", "idx", "modified", *EDITABLE, *SYSTEM, *DERIVED)


def _line(name: str, for_update: bool = False):
    return frappe.db.get_value(LINE, name, list(LINE_FIELDS), as_dict=True, for_update=for_update)


def _header(name: str, for_update: bool = False):
    return frappe.db.get_value(SCHEDULE, name, ["name", "title", "company", "b2b_settings", "closed"], as_dict=True,
                               for_update=for_update)


def _stored_lines(schedule: str) -> dict:
    """The schedule's lines as saved, under lock (the schedule row is already locked by the save)."""
    rows = frappe.db.sql(
        f"""
        SELECT {", ".join(f"`{f}`" for f in LINE_FIELDS)}
        FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s ORDER BY idx
        {owed._locking(True)}
        """,
        (schedule, SCHEDULE),
        as_dict=True,
    )
    return {r.name: r for r in rows}


def _payee(line) -> str:
    return cstr(line.get("payee_name")).strip() or cstr(line.get("party")).strip() or cstr(line.get("destination"))


# ---------------------------------------------------------------- the schedule form
def validate_schedule(doc) -> None:
    """On every save: lines that left Planned stay exactly as they are, the server's fields come
    from the database, a new or moved Pay On is today or later, destinations come from the
    approved account, and no document gets more planned than it has left to pay."""
    stored = {} if doc.is_new() else _stored_lines(doc.name)
    doc.closed = 0 if doc.is_new() else cint(frappe.db.get_value(SCHEDULE, doc.name, "closed"))
    doc.purpose = doc.purpose or "General"
    _check_pay_from(doc)
    user = frappe.session.user
    today = _today()
    kept = set()
    for row in doc.lines:
        old = stored.get(row.name)
        if old:
            kept.add(row.name)
        changed = not old or any(_norm(f, row.get(f)) != _norm(f, old.get(f)) for f in EDITABLE)
        if old and (old.status != PLANNED or old.request):
            if changed:
                frappe.throw(_left_planned(old, "changed"))
            row.update({f: old.get(f) for f in (*SYSTEM, *DERIVED)})
            continue
        if cint(doc.closed) and changed:
            frappe.throw(f"{doc.name} is closed: its lines cannot be added or changed.")
        if old:
            row.update({f: old.get(f) for f in SYSTEM})
        else:
            row.update({f: None for f in SYSTEM})
            row.status, row.attempts, row.prepared_by = PLANNED, 0, user
        if changed:
            row.changed_by = user  # who pays it must be someone else (maker-checker)
        if (not old or _norm("planned_date", row.planned_date) != _norm("planned_date", old.planned_date)) and (
            getdate(row.planned_date) < today
        ):
            frappe.throw(f"Row {row.idx}: Pay On cannot be before today.")
        row.last_problem = _check_line(doc, row, changed) or None
    for name, old in stored.items():
        if name not in kept and (old.status != PLANNED or old.request):
            frappe.throw(_left_planned(old, "deleted"))
    _check_planned_amounts(doc)
    doc.update(totals(doc.lines, doc.closed))


def _left_planned(old, what: str) -> str:
    if old.status == CANCELLED:
        return f"Row {old.idx} was cancelled when the schedule was closed; it cannot be {what}."
    when = f" on {_day(old.approved_on)}" if old.approved_on else ""
    text = f"Row {old.idx} was sent to M-Pesa{when}; it cannot be {what}."
    if old.status == FAILED:
        text += " Press Plan Again to pay it again."
    return text


def _check_pay_from(doc) -> None:
    if not doc.b2b_settings or not frappe.get_meta("Mpesa Settings").has_field("company"):
        return
    company = frappe.db.get_value("Mpesa Settings", doc.b2b_settings, "company")
    if company and company != doc.company:
        frappe.throw(f"Mpesa Settings {doc.b2b_settings} pays for {company}, not {doc.company}.")


def _check_line(doc, row, changed: bool = True) -> str | None:
    """Throws what makes the line unpayable whatever the day; returns a problem that may clear by
    its Pay On (an account still in its hold period, a hold, a switch), shown on the line."""
    try:
        daraja.whole_shillings(row.amount)
    except ValueError as e:
        frappe.throw(f"Row {row.idx}: {e}")
    if not doc.b2b_settings:
        frappe.throw("Pick Pay From (B2B): the shortcode these lines are paid from.")
    if row.party:
        soft = _check_party(row)
    else:
        soft = _check_one_off(doc, row, changed)
    row.entry_mode = row.entry_mode or AUTO
    if not row.party:
        row.entry_mode = LATER  # nothing to book it on until someone says what it paid for
    dt, dn = _document(row)
    if row.get("reference_doctype") or row.get("reference_name"):
        if not dt or dt not in owed.DOC_FIELDS:
            frappe.throw(f"Row {row.idx}: pick the Purchase Invoice or Purchase Order, or leave both empty.")
        if not row.party:
            frappe.throw(f"Row {row.idx}: a payment of {dn} needs its supplier.")
        values = owed.read_document(dt, dn)
        if not values:
            frappe.throw(f"Row {row.idx}: {dt} {dn} not found.")
        if values.supplier != row.party:
            frappe.throw(f"Row {row.idx}: {dn} belongs to {values.supplier}, not {row.party}.")
        if values.get("company") and values.company != doc.company:
            frappe.throw(f"Row {row.idx}: {dn} belongs to {values.company}, not {doc.company}.")
        problems = owed.document_problems(dt, values)
        soft = soft or (problems[0] if problems else None)
    settings = _settings_problem(doc, row)
    return soft or settings


def _check_party(row) -> str | None:
    account_doctype = PARTY_ACCOUNT.get(row.party_type or "")
    if not account_doctype:
        frappe.throw(f"Row {row.idx}: {row.party_type or 'this party type'} is not paid by the schedule yet.")
    if not frappe.db.exists(row.party_type, row.party):
        frappe.throw(f"Row {row.idx}: {row.party_type} {row.party} not found.")
    row.payee_account_doctype = account_doctype
    row.payee_account = row.payee_account or _default_account(row.party)
    if not row.payee_account:
        frappe.throw(f"Row {row.idx}: {row.party} has no approved M-Pesa account. Add one under Supplier > M-Pesa "
                     "Accounts and have a manager approve it.")
    acc = supplier_accounts.get_account(row.payee_account)
    if not acc or acc.docstatus != 1 or acc.supplier != row.party:
        frappe.throw(f"Row {row.idx}: {row.payee_account} is not an approved M-Pesa account of {row.party}.")
    row.route, row.destination = destination_of(acc)
    row.payee_name = frappe.db.get_value("Supplier", row.party, "supplier_name") or row.party
    for field in ("one_off_type", "one_off_shortcode", "one_off_account"):
        row.set(field, None)
    return supplier_accounts.usable_problem(acc, row.party) or owed.supplier_hold(row.party)


def _default_account(supplier) -> str | None:
    """The supplier's default approved account, or its only one; one still in its hold period
    counts (the line may be due after it)."""
    rows = frappe.get_all(supplier_accounts.ACCOUNT_DOCTYPE, filters={"supplier": supplier, "docstatus": 1},
                          fields=["name", "is_default"], order_by="is_default desc, creation asc")
    if rows and (rows[0].is_default or len(rows) == 1):
        return rows[0].name
    return None


def destination_of(acc) -> tuple:
    """(route, destination) of an approved account: ('B2B Paybill', '888880 / ACME-1')."""
    route = ROUTE_FOR.get(acc.account_type)
    shortcode = cstr(acc.shortcode).strip()
    account = cstr(acc.get("account_number")).strip()
    return route, f"{shortcode} / {account}" if account else shortcode


def _check_one_off(doc, row, changed: bool) -> str | None:
    """A number not saved on a supplier (a utility's paybill): only with the switch on, and only a
    manager enters or changes one (R7). An unchanged one is kept, with a problem if the switch
    went off since."""
    off = not cint(frappe.db.get_single_value(supplier_accounts.SUITE, "allow_one_off_b2b"))
    if changed:
        if off:
            frappe.throw(f"Row {row.idx}: pick the supplier. Paying a number not saved on a supplier needs 'Allow B2B "
                         "to Numbers Not Saved on a Supplier' in M-Pesa Suite Settings.")
        require(ADMIN_ROLES)
    row.party_type = None
    shortcode = cstr(row.one_off_shortcode).strip()
    account = cstr(row.one_off_account).strip()
    if row.one_off_type not in supplier_accounts.COMMAND_FOR:
        frappe.throw(f"Row {row.idx}: pick Paybill or Till for the number not saved on a supplier.")
    if not shortcode.isdigit() or not 5 <= len(shortcode) <= 7:
        frappe.throw(f"Row {row.idx}: the paybill or till number must be 5 to 7 digits.")
    if row.one_off_type == supplier_accounts.PAYBILL and not account:
        frappe.throw(f"Row {row.idx}: a paybill needs its Account Number.")
    if row.one_off_type == supplier_accounts.TILL:
        account = ""
    if not cstr(row.payee_name).strip():
        frappe.throw(f"Row {row.idx}: enter who is being paid (Payee).")
    row.one_off_shortcode, row.one_off_account = shortcode, account or None
    row.payee_account_doctype = row.payee_account = None
    row.route, row.destination = destination_of(
        frappe._dict(account_type=row.one_off_type, shortcode=shortcode, account_number=account))
    return "Paying a number not saved on a supplier is switched off (M-Pesa Suite Settings)." if off else None


def _settings_problem(doc, row) -> str | None:
    """The shortcode: B2B switched on, not paying itself, within its limit, for this company."""
    request = _request_like(doc, row)
    return _caught(_check_settings, request)


def _check_settings(request) -> None:
    from . import b2b

    b2b._check_settings(request, fill=False)


def _request_like(schedule, line):
    shortcode = cstr(line.get("destination")).split("/")[0].strip()
    return frappe._dict(mpesa_settings=schedule.b2b_settings, receiver_shortcode=shortcode,
                        amount=flt(line.get("amount")), company=schedule.company)


def _caught(fn, *args, **kwargs) -> str | None:
    """What a check throws, as a message, without showing it to the user as an error."""
    try:
        fn(*args, **kwargs)
    except (frappe.ValidationError, frappe.PermissionError) as e:
        _forget_message()
        return _plain(e) or type(e).__name__
    return None


def _check_planned_amounts(doc) -> None:
    """No document gets more planned than it has left to pay, counting the planned lines of every
    other schedule (owed.left_to_pay) and this schedule's lines as on the form."""
    planned = {}
    for row in doc.lines:
        dt, dn = _document(row)
        if dt and row.status == PLANNED and not row.request:
            planned.setdefault((dt, dn), []).append(row)
    for (dt, dn), rows in sorted(planned.items()):
        total = flt(sum(flt(r.amount) for r in rows), 2)
        result = owed.left_to_pay(dt, dn, exclude_schedule=doc.name)
        if total > result.left + 0.001:
            idx = ", ".join(str(r.idx) for r in rows)
            frappe.throw(f"Row {idx} plans KES {owed.money(total)} for {dn}, but KES {owed.money(result.left)} is "
                         f"left to pay. {owed.explain(dn, result)}")


def totals(lines, closed=0) -> dict:
    """The schedule's totals and status from its lines."""
    def total(*states):
        return flt(sum(flt(r.get("amount")) for r in lines if r.get("status") in states), 2)

    live = [r for r in lines if r.get("status") != CANCELLED]
    if cint(closed):
        status = "Closed"
    elif live and all(r.get("status") == PAID for r in live):
        status = "Paid"
    elif any(r.get("status") == PAID for r in live):
        status = "Partly Paid"
    else:
        status = "Open"
    return {"total_planned": total(PLANNED), "total_in_progress": total(*IN_PROGRESS), "total_paid": total(PAID),
            "status": status}


def update_totals(schedule: str, bump: bool = False) -> None:
    """Recount the schedule's totals from its lines (after a Result, a payment, a move). ``bump``:
    also change its modified time, so a form opened before is refused on save and reloaded."""
    if not frappe.db.get_value(SCHEDULE, schedule, "name", for_update=True):
        return
    rows = frappe.db.sql(f"SELECT status, amount FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s",
                         (schedule, SCHEDULE), as_dict=True)
    closed = frappe.db.get_value(SCHEDULE, schedule, "closed")
    frappe.db.set_value(SCHEDULE, schedule, totals(rows, closed), update_modified=bump)


def check_delete(doc) -> None:
    """A schedule with any line sent (or once sent) is the record of those payments."""
    rows = frappe.db.sql(
        f"SELECT idx, status, request FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s ORDER BY idx",
        (doc.name, SCHEDULE), as_dict=True)
    sent = [r for r in rows if r.status not in (PLANNED, CANCELLED) or r.request]
    if sent or frappe.db.exists(B2B, {"payment_schedule": doc.name}):
        frappe.throw(f"{doc.name} has payments sent to M-Pesa. It is their record and cannot be deleted: close it "
                     "instead.")


# ---------------------------------------------------------------- is a line payable now?
def line_problem(line, schedule=None, *, for_update: bool = False, payer=None, snapshot=None):
    """_dict(problem, warnings, left): why the line cannot be paid now by ``payer`` (None when it
    can), notes worth reading before paying it, and what its document has left to pay.
    ``snapshot``: the line as prepare_payment showed it; any change since refuses it."""
    schedule = schedule or _header(line.parent)
    found = frappe._dict(problem=None, warnings=[], left=None)
    payer = payer or frappe.session.user

    def refuse(problem):
        found.problem = problem
        return found

    if line.status != PLANNED or line.request:
        return refuse(f"It is {line.status}" + (f" ({line.request})." if line.request else "."))
    if getdate(line.planned_date) > _today():
        return refuse(f"Not due until {_day(line.planned_date)}.")
    if cint(schedule.closed):
        return refuse(f"{schedule.name} is closed.")
    if maker_checker_on() and payer != "Administrator" and payer in (line.prepared_by, line.changed_by):
        return refuse("You prepared or last changed this line: another person must pay it.")
    if snapshot is not None and _changed(line, snapshot):
        return refuse(CHANGED)
    problem = _send_problem(line, schedule, payer) or _document_problem(line, schedule, found, for_update)
    if problem:
        return refuse(problem)
    found.warnings = _warnings(line)
    return found


def _changed(line, snapshot) -> bool:
    return (
        flt(line.amount, 2) != flt(snapshot.get("amount"), 2)
        or _norm("planned_date", line.planned_date) != cstr(snapshot.get("planned_date"))
        or cstr(line.destination) != cstr(snapshot.get("destination"))
        or cstr(line.modified) != cstr(snapshot.get("modified"))
    )


def _send_problem(line, schedule, payer) -> str | None:
    if line.route not in SUB_ACCOUNT:
        return f"{line.route or 'This route'} is not paid by the schedule yet."
    if not schedule.b2b_settings:
        return f"Pick Pay From (B2B) on {schedule.name}."
    try:
        daraja.whole_shillings(line.amount)
    except ValueError as e:
        return str(e)
    if line.party:
        hold = owed.supplier_hold(line.party)
        if hold:
            return hold
        acc = supplier_accounts.get_account(line.payee_account)
        problem = supplier_accounts.usable_problem(acc, line.party)
        if problem:
            return problem
        if destination_of(acc) != (line.route, line.destination):
            return f"Its approved account changed since the line was saved: open {line.parent} and save it again."
    else:
        if not cint(frappe.db.get_single_value(supplier_accounts.SUITE, "allow_one_off_b2b")):
            return "Paying a number not saved on a supplier is switched off (M-Pesa Suite Settings)."
        if payer != "Administrator" and not set(ADMIN_ROLES) & set(frappe.get_roles(payer)):
            return "A number not saved on a supplier is paid by an M-Pesa Manager only."
    return _caught(_check_settings, _request_like(schedule, line))


def _document_problem(line, schedule, found, for_update: bool) -> str | None:
    """The document can be paid and has at least the line's amount left, its other planned lines
    and payouts counted (payouts first, the document row last: R4)."""
    dt, dn = _document(line)
    if not dt:
        return None
    result = owed.left_to_pay(dt, dn, exclude_line=line.name, for_update=for_update)
    found.left = result.left
    values = owed.read_document(dt, dn, for_update=for_update)
    problems = owed.document_problems(dt, values)
    if problems:
        return problems[0]
    if values.supplier != line.party:
        return f"{dn} belongs to {values.supplier}, not {line.party}."
    if values.get("company") and values.company != schedule.company:
        return f"{dn} belongs to {values.company}, not {schedule.company}."
    if flt(line.amount) > result.left + 0.001:
        return f"KES {owed.money(line.amount)} is more than is left to pay. {owed.explain(dn, result)}"
    return None


def _warnings(line) -> list:
    """Worth reading before paying: M-Pesa money the supplier got outside this schedule."""
    if not line.party:
        return []
    dt, _ = _document(line)
    if dt == PI:
        earlier = owed.earlier_payouts(line.party)
        if earlier:
            listed = "; ".join(e.label for e in earlier[:5])
            return [f"{line.party} already got M-Pesa payments that are not on any invoice: {listed}. Confirming says "
                    "none of them paid this invoice."]
        return []
    if dt:
        return []
    since = add_days(_today(), -30)
    rows = frappe.db.sql(
        f"""
        SELECT name, amount, trans_completed_time FROM `tab{B2B}`
        WHERE supplier = %(supplier)s AND status = 'Paid' AND docstatus IN (1, 2)
          AND trans_completed_time >= %(since)s AND COALESCE(payment_schedule, '') != %(schedule)s
        ORDER BY trans_completed_time DESC LIMIT 3
        """,
        {"supplier": line.party, "since": since, "schedule": line.parent},
        as_dict=True,
    )
    return [f"{line.party} was paid KES {owed.money(r.amount)} by M-Pesa on {_day(r.trans_completed_time)} outside "
            f"this schedule ({r.name})." for r in rows]


def assert_line_due(doc) -> None:
    """No route sends a scheduled payment before its planned day (site time zone). Called at the
    request's submit, by send_b2b and by send_deferred, under a lock on the line."""
    name = cstr(doc.get("schedule_line")).strip()
    if not name:
        return
    line = frappe.db.get_value(LINE, name, ["name", "parent", "planned_date", "request"], as_dict=True,
                               for_update=True)
    if not line:
        frappe.throw(f"Payment schedule line {name} not found.")
    if doc.get("payment_schedule") and line.parent != doc.payment_schedule:
        frappe.throw(f"Payment schedule line {name} belongs to {line.parent}, not {doc.payment_schedule}.")
    if getdate(line.planned_date) > _today():
        frappe.throw(f"Not due until {_day(line.planned_date)}. A scheduled payment cannot be sent before its "
                     "planned day.")
    if line.request != doc.name:
        frappe.throw(f"{doc.name} is not the payment of its schedule line: pay it with Pay Selected in M-Pesa "
                     "Payments Due.")


def check_request(doc) -> None:
    """A B2B request carries a schedule line only when confirm_payment made it. Flags cannot come
    through the API, so an insert with docstatus 1 and a typed line is refused (F1)."""
    if not (cstr(doc.get("schedule_line")).strip() or cstr(doc.get("payment_schedule")).strip()):
        return
    flags = getattr(doc, "flags", None) or {}
    if not flags.get("mpesa_from_schedule"):
        frappe.throw("A scheduled payment is paid with Pay Selected in M-Pesa Payments Due, not by hand.")


# ---------------------------------------------------------------- Pay Selected
def _token_key(token: str) -> str:
    return f"mpesa_suite:pay_token:{token}"


def _age(as_of) -> str | None:
    if not as_of:
        return None
    minutes = max(0, int((now_datetime() - get_datetime(as_of)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if minutes < 48 * 60:
        return f"{minutes // 60} hour{'s' if minutes // 60 != 1 else ''}"
    return f"{minutes // 1440} days"


def _shown(line, schedule) -> dict:
    """A line as the summary shows it."""
    dt, dn = _document(line)
    return {"line": line.name, "schedule": line.parent, "row": line.idx, "payee": _payee(line),
            "party_type": line.party_type, "party": line.party, "amount": flt(line.amount, 2),
            "planned_date": _norm("planned_date", line.planned_date), "route": line.route,
            "destination": line.destination, "document": dn, "document_type": dt,
            "pay_from": schedule.b2b_settings}


@frappe.whitelist(methods=["POST"])
def prepare_payment(lines) -> dict:
    """The confirm step's summary for the ticked lines, and the token Confirm needs.

    Per payee and per shortcode and route, the grand total, each shortcode's balance with its age
    and any shortfall, the lines left out and why. The token is bound to this user and the lines
    as shown, used once, and works from 5 seconds to 10 minutes after this call."""
    require(PAYOUT_ROLES)
    names = _names(lines)
    if not names:
        frappe.throw("Tick the lines to pay.")
    user = frappe.session.user
    headers, included, left_out, warnings, snapshot = {}, [], [], [], []
    for name in names:
        line = _line(name)
        if not line:
            left_out.append({"line": name, "payee": name, "amount": 0, "reason": "This line was deleted."})
            continue
        if line.parent not in headers:
            frappe.has_permission(SCHEDULE, "read", doc=line.parent, throw=True)
            headers[line.parent] = _header(line.parent)
        schedule = headers[line.parent]
        item = _shown(line, schedule)
        check = line_problem(line, schedule, payer=user)
        if check.problem:
            left_out.append({**item, "reason": check.problem})
            continue
        warnings += [f"{item['payee']}: {w}" for w in check.warnings]
        earlier = sorted(e.name for e in owed.earlier_payouts(line.party)) if item["document_type"] == PI else []
        included.append(item)
        snapshot.append({"line": line.name, "amount": item["amount"], "planned_date": item["planned_date"],
                         "destination": cstr(line.destination), "modified": cstr(line.modified), "earlier": earlier})
    parties, routes = {}, {}
    for item in included:
        key = (item["party_type"] or "", item["party"] or item["destination"])
        p = parties.setdefault(key, {"payee": item["payee"], "party": item["party"], "lines": 0, "amount": 0.0})
        p["lines"] += 1
        p["amount"] = flt(p["amount"] + item["amount"], 2)
        key = (item["pay_from"], item["route"])
        r = routes.setdefault(key, {"pay_from": item["pay_from"], "route": item["route"], "lines": 0, "amount": 0.0,
                                    "shortcode": frappe.db.get_value("Mpesa Settings", item["pay_from"],
                                                                     "business_shortcode")})
        r["lines"] += 1
        r["amount"] = flt(r["amount"] + item["amount"], 2)
    total = flt(sum(i["amount"] for i in included), 2)
    found_balances = _balances(routes.values(), warnings)
    token = None
    if included:
        token = frappe.generate_hash(length=24)
        get_cache().set_value(_token_key(token), {"user": user, "issued_at": _clock(), "lines": snapshot},
                              expires_in_sec=TOKEN_SECONDS)
    return {
        "token": token,
        "delay": CONFIRM_DELAY,
        "expires_in": TOKEN_SECONDS,
        "count": len(included),
        "total": total,
        "parties": sorted(parties.values(), key=lambda p: (cstr(p["payee"]).lower(), p["amount"])),
        "routes": sorted(routes.values(), key=lambda r: (cstr(r["pay_from"]), cstr(r["route"]))),
        "balances": found_balances,
        "warnings": warnings,
        "not_included": left_out,
    }


def _balances(routes, warnings) -> list:
    """Each shortcode's sub-account balance (approximate), with its age, against the selected total."""
    selected = {}
    for r in routes:
        key = (r["pay_from"], SUB_ACCOUNT.get(r["route"]))
        selected[key] = flt(selected.get(key, 0) + r["amount"], 2)
    found = []
    for (pay_from, sub), amount in sorted(selected.items(), key=lambda kv: (cstr(kv[0][0]), cstr(kv[0][1]))):
        b = balances.available(pay_from, sub)
        age = _age(b.as_of)
        short = flt(amount - b.after, 2) if b.after is not None else None
        found.append({"pay_from": pay_from, "sub_account": sub, "amount": b.amount, "as_of": b.as_of, "age": age,
                      "pending": b.pending, "after": b.after, "selected": amount, "source": b.source,
                      "short": short if short is not None and short > 0 else 0, "approximate": 1})
        if b.amount is None:
            warnings.append(f"No {sub} balance is known for {pay_from}: press Refresh Balances.")
            continue
        if short and short > 0:
            warnings.append(
                f"{pay_from} {sub}: about KES {owed.money(b.after)} available"
                + (f" after KES {owed.money(b.pending)} still on its way" if b.pending else "")
                + f", less than the KES {owed.money(amount)} selected (short by KES {owed.money(short)}). M-Pesa fails "
                "the payments past the balance."
            )
        if age and b.as_of and get_datetime(b.as_of) < add_to_date(now_datetime(), hours=-1):
            warnings.append(f"The {sub} balance of {pay_from} is {age} old: press Refresh Balances for a fresh one.")
    return found


class _Refused(Exception):
    """A line confirm_payment does not pay, with the reason."""


@frappe.whitelist(methods=["POST"])
def confirm_payment(token) -> dict:
    """Pay the lines of a summary: once per token, by its user, no sooner than 5 seconds after it
    was made. Each line is checked again under its locks and gets a submitted B2B request; the
    requests are sent by send_batch after this commits."""
    require(PAYOUT_ROLES)
    token = cstr(token).strip()
    if not token:
        frappe.throw("Press Pay Selected first.")
    user = frappe.session.user
    with money_in_common.named_lock(f"pay-token:{token}", busy="These payments are being confirmed already."):
        data = get_cache().get_value(_token_key(token))
        if not data:
            frappe.throw("This summary has expired or was used already. Press Pay Selected again.")
        if data.get("user") != user:
            frappe.throw("This summary was made for another user. Press Pay Selected yourself.")
        if _clock() - flt(data.get("issued_at")) < CONFIRM_DELAY:
            frappe.throw(f"Read the summary first: Confirm works {CONFIRM_DELAY} seconds after it opens.")
        get_cache().delete_value(_token_key(token))  # used once, whatever happens next
    batch = f"{now_datetime():%y%m%d%H%M%S}-{frappe.generate_hash(length=4)}"
    sent, refused, touched = [], [], set()
    for item in _in_lock_order(data.get("lines") or []):
        frappe.db.savepoint("mpsa_pay_line")
        try:
            request, line = _pay_line(item, batch, user)
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            raise  # the whole transaction is gone: nothing here was paid
        except Exception as e:
            frappe.db.rollback(save_point="mpsa_pay_line")
            reason = _reason(e, item)
            if frappe.db.exists(LINE, item["line"]):
                frappe.db.set_value(LINE, item["line"], "last_problem", reason[:1000], update_modified=False)
            refused.append({"line": item["line"], "reason": reason, **_brief(item["line"])})
            continue
        touched.add(line.parent)
        sent.append({"line": line.name, "request": request, "payee": _payee(line), "amount": flt(line.amount, 2)})
    for schedule in sorted(touched):
        update_totals(schedule, bump=True)  # a schedule form opened before must be reloaded
    if sent:
        frappe.enqueue(f"{__name__}.send_batch", queue="long", timeout=3600, enqueue_after_commit=True,
                       requests=[s["request"] for s in sent], batch=batch)
    return {"batch": batch, "sent": sent, "refused": refused,
            "total": flt(sum(s["amount"] for s in sent), 2)}


def _reason(e, item) -> str:
    if isinstance(e, _Refused):
        return cstr(e)
    if isinstance(e, (frappe.ValidationError, frappe.PermissionError)):
        _forget_message()
        return _plain(e) or "Not allowed."
    frappe.log_error(title="M-Pesa scheduled payment not confirmed", message=f"{item}\n{frappe.get_traceback()}")
    return f"Could not pay it: {type(e).__name__}. It is in the Error Log."


def _brief(name) -> dict:
    line = frappe.db.get_value(LINE, name, ["parent", "payee_name", "party", "destination", "amount"], as_dict=True)
    if not line:
        return {"payee": name, "amount": 0}
    return {"payee": _payee(line), "amount": flt(line.amount, 2), "schedule": line.parent}


def _in_lock_order(items) -> list:
    """Lines by schedule and row, so two confirms lock schedule rows in the same order."""
    def key(item):
        peek = frappe.db.get_value(LINE, item.get("line"), ["parent", "idx"], as_dict=True) or {}
        return (cstr(peek.get("parent")), cint(peek.get("idx")), cstr(item.get("line")))

    return sorted(items, key=key)


def _pay_line(item, batch, payer) -> tuple:
    """Check the line again under its locks and submit its B2B request (sent later)."""
    peek = frappe.db.get_value(LINE, item["line"], ["parent", "reference_doctype", "reference_name"], as_dict=True)
    if not peek:
        raise _Refused("This line was deleted.")
    dt, dn = _document(peek)
    with contextlib.ExitStack() as stack:
        if dt:
            stack.enter_context(owed.doc_lock(dt, dn))  # R4: the document's lock first
        schedule = _header(peek.parent, for_update=True)
        line = _line(item["line"], for_update=True)
        if not schedule or not line or _document(line) != (dt, dn):
            raise _Refused(CHANGED)
        check = line_problem(line, schedule, for_update=True, payer=payer, snapshot=item)
        if check.problem:
            raise _Refused(check.problem)
        earlier = sorted(e.name for e in owed.earlier_payouts(line.party)) if dt == PI else []
        if set(earlier) - set(item.get("earlier") or []):
            raise _Refused(f"{line.party} got M-Pesa payments with no invoice after you pressed Pay Selected. Press it "
                           "again and check them.")
        doc = _new_request(line, schedule, checked=bool(earlier))
        doc.insert()
        frappe.db.set_value(LINE, line.name, {
            "status": SENDING, "request": doc.name, "approved_by": payer, "approved_on": now_datetime(),
            "batch": batch, "attempts": cint(line.attempts) + 1, "last_problem": None,
        }, update_modified=False)
        doc.submit()  # every B2B check again under the document's lock; sent later (flags)
    return doc.name, line


def _new_request(line, schedule, checked: bool):
    doc = frappe.new_doc(B2B)
    doc.update({
        "mpesa_settings": schedule.b2b_settings,
        "company": schedule.company,
        "amount": flt(line.amount),
        "remarks": (cstr(line.remarks).strip() or cstr(schedule.title).strip() or "Scheduled payment")[:100],
        "entry_mode": line.entry_mode or AUTO,
        "payment_schedule": schedule.name,
        "schedule_line": line.name,
        "checked_earlier_payouts": 1 if checked else 0,
    })
    if line.party:
        doc.supplier = line.party
        doc.supplier_mpesa_account = line.payee_account
    else:
        doc.command_id = supplier_accounts.COMMAND_FOR[line.one_off_type]
        doc.receiver_shortcode = line.one_off_shortcode
        doc.account_reference = line.one_off_account
    dt, dn = _document(line)
    if dt:
        doc.set(owed.DOC_FIELDS[dt], dn)
    doc.flags.mpesa_from_schedule = True
    doc.flags.mpesa_send_later = True
    return doc


def send_batch(requests, batch=None) -> None:
    """The job confirm_payment queues: each request sent once, a few seconds apart, committed one
    by one (b2b.send_deferred)."""
    from . import b2b

    interval = _interval()
    for i, name in enumerate(requests or []):
        if i and interval:
            time.sleep(interval)
        try:
            b2b.send_deferred(name)
        except Exception:
            frappe.db.rollback()
            frappe.log_error(title="M-Pesa scheduled payment not sent",
                             message=f"{name} ({batch})\n{frappe.get_traceback()}",
                             reference_doctype=B2B, reference_name=name)


def _interval() -> int:
    try:
        value = money_in_common.suite().get("send_interval_seconds")
    except frappe.DoesNotExistError:
        value = None
    return 2 if value in (None, "") else max(0, cint(value))


# ---------------------------------------------------------------- the line follows its request
def sync_line(request) -> None:
    """Mirror a schedule request on its line: status, receipt, paid time and entry state. Never
    raises, except a deadlock or lock wait: swallowing one would hide that the caller's own
    writes were rolled back, so the caller's retry (a Result is recorded again) handles it."""
    name = request if isinstance(request, str) else getattr(request, "name", None)
    try:
        if not name or not frappe.get_meta(B2B).has_field("schedule_line"):
            return
        values = frappe.db.get_value(
            B2B, name, ["name", "docstatus", "status", "schedule_line", "transaction_id", "trans_completed_time",
                        "entry_status", "error_message", "result_desc"], as_dict=True)
        if not values or not cstr(values.schedule_line).strip():
            return
        line = frappe.db.get_value(LINE, values.schedule_line, ["name", "parent", "request", *SYSTEM], as_dict=True)
        if not line or line.request != name:
            return  # planned again since: the line belongs to another request now
        mirror = _mirror(values)
        changed = {k: v for k, v in mirror.items() if _norm(k, line.get(k)) != _norm(k, v)}
        if not changed:
            return
        frappe.db.set_value(LINE, line.name, changed, update_modified=False)
        frappe.enqueue(f"{__name__}.update_totals", queue="short", timeout=300, enqueue_after_commit=True,
                       schedule=line.parent)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise
    except Exception:
        frappe.log_error(title="M-Pesa schedule line not updated", message=f"{name}\n{frappe.get_traceback()}")


def sync_line_later(doc) -> None:
    """sync_line from a job once the caller commits: a Result, an ack or an entry check never
    waits on, or deadlocks with, a schedule's rows. Only for a request made by a schedule."""
    if not cstr(doc.get("schedule_line")).strip():
        return
    try:
        frappe.enqueue(f"{__name__}.sync_line", queue="short", timeout=300, enqueue_after_commit=True,
                       request=doc.name)
    except Exception:
        # the 5-minute sweep brings the line up to date
        frappe.log_error(title="M-Pesa schedule line update not queued",
                         message=f"{doc.name}\n{frappe.get_traceback()}")


def _mirror(values) -> dict:
    status = values.status or "Not Initiated"
    problem = None
    if status == "Paid":
        line_status = PAID
    elif cint(values.docstatus) == 2:
        line_status = FAILED
        problem = f"{values.name} was cancelled ({status}). Plan it again if it was not paid."
    elif status == "Initiated":
        line_status = SENT
    elif status == "Unknown":
        line_status = UNKNOWN
        problem = (f"{_plain(values.error_message) or INTERRUPTED} A manager checks the statement and cancels "
                   f"{values.name} if it was not paid; then plan it again.")
    elif status == "Failed":
        line_status = FAILED
        problem = _plain(values.error_message or values.result_desc) or "M-Pesa refused it."
    else:
        line_status = SENDING
    paid = line_status == PAID
    return {"status": line_status, "receipt": cstr(values.transaction_id).strip() or None,
            "paid_on": values.trans_completed_time if paid else None,
            "entry_status": values.entry_status if paid else None, "last_problem": problem}


# ---------------------------------------------------------------- staff actions on lines
def _lines_by_schedule(names) -> dict:
    found = {}
    for name in names:
        parent = frappe.db.get_value(LINE, name, "parent")
        if not parent:
            frappe.throw(f"Payment schedule line {name} not found.")
        found.setdefault(parent, []).append(name)
    return found


@frappe.whitelist(methods=["POST"])
def move_date(lines, date) -> dict:
    """Move planned lines to another day, today or later (Accounts). Each move is noted on the
    schedule with the old and new day, and makes the mover the line's last changer."""
    require(FINANCE_ROLES)
    names = _names(lines)
    if not names:
        frappe.throw("Tick the lines to move.")
    if not date:
        frappe.throw("Pick the new Pay On.")
    new = getdate(date)
    if new < _today():
        frappe.throw(f"Pick today or a later day: {_day(new)} has passed.")
    user = frappe.session.user
    moved = 0
    for parent, members in sorted(_lines_by_schedule(names).items()):
        frappe.has_permission(SCHEDULE, "write", doc=parent, throw=True)
        schedule = _header(parent, for_update=True)
        if cint(schedule.closed):
            frappe.throw(f"{parent} is closed.")
        notes = []
        for line in sorted((_line(n, for_update=True) for n in members), key=lambda r: cint(r.idx)):
            if line.status != PLANNED or line.request:
                frappe.throw(f"Row {line.idx} of {parent} is {line.status}: only a planned line can be moved.")
            if getdate(line.planned_date) == new:
                continue
            frappe.db.set_value(LINE, line.name, {"planned_date": new, "changed_by": user})
            notes.append(f"row {line.idx} ({_payee(line)}, KES {owed.money(line.amount)}) from "
                         f"{_day(line.planned_date)} to {_day(new)}")
            moved += 1
        if notes:
            frappe.get_doc(SCHEDULE, parent).add_comment("Comment", f"{user} moved Pay On of " + "; ".join(notes) + ".")
            update_totals(parent, bump=True)
    return {"moved": moved}


@frappe.whitelist(methods=["POST"])
def plan_again(line, date=None) -> dict:
    """Plan a failed line again, to be paid with Pay Selected like a new one. An Unknown line may
    have been paid: a manager checks the statement and cancels its request first."""
    require(FINANCE_ROLES)
    parent = frappe.db.get_value(LINE, line, "parent")
    if not parent:
        frappe.throw(f"Payment schedule line {line} not found.")
    frappe.has_permission(SCHEDULE, "write", doc=parent, throw=True)
    schedule = _header(parent, for_update=True)
    if cint(schedule.closed):
        frappe.throw(f"{parent} is closed.")
    row = _line(line, for_update=True)
    if row.status == UNKNOWN:
        frappe.throw(f"M-Pesa may have paid row {row.idx} ({row.request}). A manager checks the statement and cancels "
                     f"{row.request} if it was not paid; then plan it again.")
    if row.status != FAILED:
        frappe.throw(f"Only a failed line is planned again; row {row.idx} is {row.status}.")
    if row.request:
        request = frappe.db.get_value(B2B, row.request, ["status", "docstatus"], as_dict=True, for_update=True)
        if request and (request.status == "Paid" or (cint(request.docstatus) == 1 and request.status != "Failed")):
            frappe.throw(f"{row.request} is {request.status}: row {row.idx} is not planned again.")
    values = {"status": PLANNED, "request": None, "receipt": None, "paid_on": None, "entry_status": None,
              "approved_by": None, "approved_on": None, "batch": None, "last_problem": None,
              "changed_by": frappe.session.user}
    if date:
        if getdate(date) < _today():
            frappe.throw(f"Pick today or a later day: {_day(date)} has passed.")
        values["planned_date"] = getdate(date)
    frappe.db.set_value(LINE, line, values)
    frappe.get_doc(SCHEDULE, parent).add_comment(
        "Comment", f"{frappe.session.user} planned row {row.idx} ({_payee(row)}, KES {owed.money(row.amount)}) again "
        f"after {row.request or 'it'} failed: {row.last_problem or 'no reason given'}.")
    update_totals(parent, bump=True)
    return {"line": line, "status": PLANNED}


@frappe.whitelist(methods=["POST"])
def close_schedule(name) -> dict:
    """Close the schedule: its planned lines are cancelled; lines already sent carry on."""
    require(FINANCE_ROLES)
    frappe.has_permission(SCHEDULE, "write", doc=name, throw=True)
    schedule = _header(name, for_update=True)
    if not schedule:
        frappe.throw(f"{SCHEDULE} {name} not found.")
    if cint(schedule.closed):
        return {"cancelled": 0}
    user = frappe.session.user
    cancelled = 0
    for line in _stored_lines(name).values():
        if line.status == PLANNED and not line.request:
            frappe.db.set_value(LINE, line.name, {"status": CANCELLED,
                                                  "last_problem": f"Cancelled when {user} closed the schedule."})
            cancelled += 1
    frappe.db.set_value(SCHEDULE, name, "closed", 1, update_modified=False)
    frappe.get_doc(SCHEDULE, name).add_comment("Comment", f"{user} closed the schedule; {cancelled} planned line(s) "
                                               "cancelled.")
    update_totals(name, bump=True)
    return {"cancelled": cancelled}


# ---------------------------------------------------------------- the 5-minute sweep
def sweep(limit: int = 200) -> None:
    """Every 5 minutes (money_in.events): sends that were interrupted, requests never started,
    and lines that missed their request's news."""
    if not frappe.get_meta(B2B).has_field("send_started_on") or not frappe.db.table_exists(LINE):
        return  # before migrate
    now = now_datetime()
    for name in frappe.get_all(B2B, filters={"docstatus": 1, "status": "Not Initiated", "send_started_on": [
            "<", add_to_date(now, minutes=-INTERRUPTED_MINUTES)]}, pluck="name", order_by="creation asc", limit=limit):
        _each(name, _interrupted)
    resend = []
    for row in frappe.get_all(B2B, filters={"docstatus": 1, "status": "Not Initiated",
                                            "send_started_on": ["is", "not set"], "schedule_line": ["is", "set"],
                                            "creation": ["<", add_to_date(now, minutes=-RESEND_AFTER_MINUTES)]},
                              fields=["name", "creation"], order_by="creation asc", limit=limit):
        if get_datetime(row.creation) >= add_to_date(now, minutes=-RESEND_WITHIN_MINUTES):
            resend.append(row.name)
        else:
            _each(row.name, _give_up)
    if resend:
        frappe.enqueue(f"{__name__}.send_batch", queue="long", timeout=3600, requests=resend, batch="sweep")
    for (request,) in frappe.db.sql(
        f"""
        SELECT request FROM `tab{LINE}`
        WHERE parenttype = %(parenttype)s AND status IN %(states)s AND COALESCE(request, '') != ''
        ORDER BY modified LIMIT {cint(limit)}
        """,
        {"parenttype": SCHEDULE, "states": IN_PROGRESS},
    ):
        _each(request, sync_line)


def _each(name, fn) -> None:
    frappe.db.savepoint("mpsa_schedule_sweep")
    try:
        fn(name)
        frappe.db.commit()
    except Exception:
        frappe.db.rollback(save_point="mpsa_schedule_sweep")
        frappe.log_error(title="M-Pesa schedule sweep failed", message=f"{name}\n{frappe.get_traceback()}",
                         reference_doctype=B2B, reference_name=name)
        frappe.db.commit()


def _interrupted(name: str) -> None:
    """The send began (its marker was committed) and never recorded an answer: M-Pesa may have
    paid it, so it is Unknown and never sent again (R1)."""
    values = frappe.db.get_value(B2B, name, ["docstatus", "status", "send_started_on"], as_dict=True, for_update=True)
    if not values or cint(values.docstatus) != 1 or values.status != "Not Initiated" or not values.send_started_on:
        return
    frappe.db.set_value(B2B, name, {"status": "Unknown", "error_message": INTERRUPTED}, update_modified=False)
    sync_line(name)
    frappe.log_error(title="M-Pesa scheduled payment interrupted", message=f"{name}: {INTERRUPTED}",
                     reference_doctype=B2B, reference_name=name)
    supplier_accounts._alert(f"{name}: {INTERRUPTED}", frappe._dict(doctype=B2B, name=name))


def _give_up(name: str) -> None:
    """Submitted by confirm_payment over an hour ago and never started: it cannot have reached
    M-Pesa (the marker comes first), so it is cancelled and its line is planned again."""
    values = frappe.db.get_value(B2B, name, ["docstatus", "status", "send_started_on", "schedule_line"], as_dict=True,
                                 for_update=True)
    if not values or cint(values.docstatus) != 1 or values.status != "Not Initiated" or values.send_started_on:
        return
    doc = frappe.get_doc(B2B, name)
    doc.flags.ignore_permissions = True
    doc.cancel()
    line = frappe.db.get_value(LINE, values.schedule_line, ["name", "parent", "idx", "request"], as_dict=True,
                               for_update=True)
    if not line or line.request != name:
        return
    frappe.db.set_value(LINE, line.name, {
        "status": PLANNED, "request": None, "approved_by": None, "approved_on": None, "batch": None,
        "last_problem": f"{name} was not sent within an hour and was cancelled: nothing was paid. Pay it again with "
                        "Pay Selected.",
    }, update_modified=False)
    frappe.get_doc(SCHEDULE, line.parent).add_comment(
        "Comment",
        f"Row {line.idx}: {name} was not sent within an hour, so it was cancelled and the line planned again.")
    update_totals(line.parent)
