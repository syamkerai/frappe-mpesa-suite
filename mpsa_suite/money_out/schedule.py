"""M-Pesa Payment Schedule: payments planned ahead and paid on their day after a confirm step.

Accounts prepare the lines (who, how much, which invoice, order or salary slip, which day) and
may move a line's day until it is sent. On the day, an M-Pesa Payer or Manager ticks lines in
the "M-Pesa Payments Due" report and presses Pay Selected. prepare_payment shows a summary per
payee and per shortcode with the balances; confirm_payment pays only with the token it issued,
at least 5 seconds later, once, for the same user (the browser keeps Confirm disabled for those
5 seconds too). Nothing is ever paid automatically.

Suppliers are paid by B2B (paybill, till or bank account; a supplier's phone is not paid from a
schedule yet). Employees are paid to their approved Employee M-Pesa Account: a bank account,
paybill or till by B2B, like a supplier; a phone by B2C, through one B2C Payment
Disbursement per schedule, B2C shortcode, party type, document type and Payroll Entry, whose
rows carry their line and the approved phone (the row refuses any other number). B2C's own
guard and jobs send those. A Payroll Entry's "Create M-Pesa Payment Schedule" builds one
schedule for its salary slips (money_out.payroll).

Rules on top of the B2B request's and the B2C disbursement's own (money_out.b2b, B2C Payment
Disbursement):
- no line is sent before its planned day (site time zone), whatever the route: confirm_payment,
  the request's submit (before_submit), send_b2b and send_deferred all refuse (assert_line_due);
  on the B2C route the job that makes the payout and the send refuse too (b2c_problem,
  assert_b2c_line_due);
- the payer must not be who prepared or last changed the line (maker-checker), unless the Suite
  setting is off;
- a planned line counts in owed.left_to_pay, so its document cannot also be paid past what is left;
- confirm_payment submits the B2B requests but sends none: send_batch sends them one at a time
  from a job, each with a send_started_on marker committed before the call to M-Pesa (R1). The
  5-minute sweep makes a marked request still Not Initiated after 10 minutes Unknown (never sent
  again); an unmarked one is sent again within the hour once its batch's job is gone, then
  cancelled and its line planned again;
- a line is changed or removed only through its schedule (its own save or delete is refused).

Lock order: the document's lock, the schedule row, its line rows, then the payout rows and the
document row (R4); send_deferred takes the document's lock before the request and line rows.
A Result touches only its line; the schedule's totals follow in a job.
"""

import contextlib
import re
import time

import frappe
from frappe.utils import add_days, add_to_date, cint, cstr, flt, get_datetime, getdate, now_datetime, nowdate

from ..compat import get_cache
from ..daraja import apis
from ..daraja import money_out as daraja
from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..money_in import common as money_in_common
from ..permissions import ADMIN_ROLES, PAYOUT_ROLES, PREPARE_ROLES, require, require_named_user
from ..wallet import accounts as wallet_accounts
from ..wallet import balances
from . import employee_accounts, owed, party_accounts, send_confirm, supplier_accounts

SCHEDULE = "M-Pesa Payment Schedule"
LINE = "M-Pesa Payment Schedule Line"
B2B = "Mpesa B2B Payment Request"
B2C_DOCTYPE = "B2C Payment Disbursement"
B2C_ROW = "B2C Payment Disbursement Reference"
B2C_REQUEST = "B2C Disbursement Request"
PI, PO, SLIP = "Purchase Invoice", "Purchase Order", owed.SLIP
DOCUMENTS = (PI, PO, SLIP)
PLANNED, SENDING, SENT, PAID, FAILED, UNKNOWN, CANCELLED = (
    "Planned", "Sending", "Sent", "Paid", "Failed", "Unknown", "Cancelled")
IN_PROGRESS = (SENDING, SENT, UNKNOWN)
AUTO, LATER = "Book automatically", "Make the entry later"
# The approved-account DocType of each party type.
PARTY_ACCOUNT = {"Supplier": supplier_accounts.ACCOUNT_DOCTYPE, "Employee": employee_accounts.ACCOUNT_DOCTYPE}
# The account types a line may pay, per party type (any for an employee: a phone by B2C, the rest by B2B)
LINE_TYPES = {"Supplier": party_accounts.B2B_TYPES}
SUPPLIER_PHONE = "is a phone account: pay a supplier's phone with a B2C Payment Disbursement."
B2B_PAYBILL, B2B_TILL, B2C_PHONE = party_accounts.B2B_PAYBILL, party_accounts.B2B_TILL, party_accounts.B2C_PHONE
ROUTE_FOR = party_accounts.ROUTE_FOR
B2C_ROUTES = (B2C_PHONE,)
# The sub-account each route pays from.
SUB_ACCOUNT = {B2B_PAYBILL: wallet_accounts.WORKING, B2B_TILL: wallet_accounts.WORKING,
               B2C_PHONE: wallet_accounts.UTILITY}
# Confirm works this many seconds after the summary was made, for this long.
CONFIRM_DELAY = 5
TOKEN_SECONDS = 600
# A request whose send started this long ago and is still Not Initiated may have reached M-Pesa.
INTERRUPTED_MINUTES = 10
# A submitted request never started is sent again from this age until RESEND_WITHIN, then given up.
RESEND_AFTER_MINUTES, RESEND_WITHIN_MINUTES = 5, 60
INTERRUPTED = "Sending was interrupted after it may have reached M-Pesa. Check the statement before paying again."
B2C_INTERRUPTED = ("Sending was interrupted after it may have reached M-Pesa. A Transaction Status check settles it; "
                   "never pay it again before.")
NOT_INITIATED = "Not Initiated"
CHANGED = "It changed after you pressed Pay Selected. Press Pay Selected again."
# Fields staff type; a line that left Planned keeps them as they were.
EDITABLE = ("planned_date", "party_type", "party", "payee_name", "payee_account", "one_off_type",
            "one_off_shortcode", "one_off_account", "amount", "reference_doctype", "reference_name",
            "entry_mode", "remarks")
# Written only by the server; always restored from the database on save.
SYSTEM = ("status", "request", "receipt", "paid_on", "entry_status", "last_problem", "prepared_by", "changed_by",
          "approved_by", "approved_on", "batch", "attempts", "b2c_disbursement", "b2c_row", "b2c_request")
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


def is_b2c(line) -> bool:
    """A phone line, paid by B2C."""
    return line.get("route") in B2C_ROUTES


def _sent(line) -> bool:
    """The line has (or had) a payout: a B2B request, or a row of a B2C disbursement."""
    return bool(cstr(line.get("request")).strip() or cstr(line.get("b2c_disbursement")).strip())


def _payout(line) -> str:
    """The name of what pays the line, for messages."""
    return cstr(line.get("request") or line.get("b2c_request") or line.get("b2c_disbursement"))


def pay_from(schedule, line):
    """The shortcode a line is paid from: Pay From (B2C) for a phone line, else Pay From (B2B)."""
    return schedule.get("b2c_settings") if is_b2c(line) else schedule.get("b2b_settings")


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


HEADER_FIELDS = ("name", "title", "company", "b2b_settings", "b2c_settings", "purpose", "payroll_entry", "closed")


def _header(name: str, for_update: bool = False):
    return frappe.db.get_value(SCHEDULE, name, list(HEADER_FIELDS), as_dict=True, for_update=for_update)


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
    _keep_payroll_fields(doc)
    doc.purpose = doc.purpose or "General"
    _check_pay_from(doc)
    user = frappe.session.user
    today = _today()
    kept = set()
    for row in doc.lines:
        old = stored.get(row.name)
        if old:
            kept.add(row.name)
        elif row.name:
            _check_not_elsewhere(doc, row)
        changed = not old or any(_norm(f, row.get(f)) != _norm(f, old.get(f)) for f in EDITABLE)
        if old and (old.status != PLANNED or _sent(old)):
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
        if name not in kept and (old.status != PLANNED or _sent(old)):
            frappe.throw(_left_planned(old, "deleted"))
    _check_planned_amounts(doc)
    doc.update(totals(doc.lines, doc.closed))


PAYROLL_FIELDS = ("purpose", "payroll_doctype", "payroll_entry", "excluded")


def _keep_payroll_fields(doc) -> None:
    """The Payroll Entry a schedule pays, and who it left out, are set only when the Payroll
    Entry's button makes it (money_out.payroll), and kept as they are on every save: "at most one
    live schedule per payroll" relies on them."""
    fields = [f for f in PAYROLL_FIELDS if frappe.get_meta(SCHEDULE).has_field(f)]
    if not doc.is_new():
        stored = frappe.db.get_value(SCHEDULE, doc.name, fields, as_dict=True) or {}
        doc.update({field: stored.get(field) for field in fields})
        return
    flags = getattr(doc, "flags", None) or {}
    if not flags.get("mpesa_from_payroll"):
        doc.update({field: None for field in fields})


def _check_not_elsewhere(doc, row) -> None:
    """A row carrying the name of another schedule's line. Frappe updates rows by name only, so it
    would move that line here and its record of a payment would be lost."""
    parent = frappe.db.get_value(LINE, row.name, "parent")
    if parent:
        frappe.throw(f"Row {row.idx} is a line of {parent}: it cannot be moved to {doc.name}.")


def _left_planned(old, what: str) -> str:
    if old.status == CANCELLED:
        return f"Row {old.idx} was cancelled when the schedule was closed; it cannot be {what}."
    when = f" on {_day(old.approved_on)}" if old.approved_on else ""
    text = f"Row {old.idx} was sent to M-Pesa{when}; it cannot be {what}."
    if old.status == FAILED:
        text += " Press Plan Again to pay it again."
    return text


def _check_pay_from(doc) -> None:
    if not frappe.get_meta("Mpesa Settings").has_field("company"):
        return
    for settings in {doc.get("b2b_settings"), doc.get("b2c_settings")} - {None, ""}:
        company = frappe.db.get_value("Mpesa Settings", settings, "company")
        if company and company != doc.company:
            frappe.throw(f"Mpesa Settings {settings} pays for {company}, not {doc.company}.")


def _check_line(doc, row, changed: bool = True) -> str | None:
    """Throws what makes the line unpayable whatever the day; returns a problem that may clear by
    its Pay On (an account still in its hold period, a hold, a switch), shown on the line."""
    try:
        daraja.whole_shillings(row.amount)
    except ValueError as e:
        frappe.throw(f"Row {row.idx}: {e}")
    if row.party:
        soft = _check_party(row)
    else:
        soft = _check_one_off(doc, row, changed)
    b2c = is_b2c(row)
    if b2c:
        if not doc.get("b2c_settings"):
            frappe.throw("Pick Pay From (B2C): the shortcode the phone lines are paid from.")
        if not B2C_MIN_AMOUNT <= flt(row.amount) <= B2C_MAX_AMOUNT:
            frappe.throw(f"Row {row.idx}: M-Pesa B2C pays KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,} per payment.")
    elif not doc.b2b_settings:
        frappe.throw("Pick Pay From (B2B): the shortcode these lines are paid from.")
    row.entry_mode = row.entry_mode or AUTO
    if not row.party:
        row.entry_mode = LATER  # nothing to book it on until someone says what it paid for
    dt, dn = _document(row)
    if row.get("reference_doctype") or row.get("reference_name"):
        if not dt or dt not in DOCUMENTS:
            frappe.throw(f"Row {row.idx}: pick the Purchase Invoice, Purchase Order or Salary Slip, or leave both "
                         "empty.")
        if dt == SLIP:
            soft = soft or _check_slip_line(doc, row, dn)
        else:
            soft = soft or _check_document_line(doc, row, dt, dn)
    elif row.party_type == "Employee":
        if b2c:
            frappe.throw(f"Row {row.idx}: a payment to a phone (B2C) pays a Salary Slip: pick it.")
        row.entry_mode = LATER  # no salary slip: nothing to book it on until someone makes the entry
    if b2c:
        row.entry_mode = AUTO  # B2C books every salary it pays (b2c_postprocess_service)
    settings = _settings_problem(doc, row)
    return soft or settings


def _check_document_line(doc, row, dt, dn) -> str | None:
    """A Purchase Invoice or Order: of the line's supplier and company. Returns its first problem
    (one that may clear by the Pay On, shown on the line)."""
    if not row.party:
        frappe.throw(f"Row {row.idx}: a payment of {dn} needs its supplier.")
    if row.party_type != "Supplier":
        frappe.throw(f"Row {row.idx}: {dn} is paid to its supplier: pick Supplier.")
    values = owed.read_document(dt, dn)
    if not values:
        frappe.throw(f"Row {row.idx}: {dt} {dn} not found.")
    if values.supplier != row.party:
        frappe.throw(f"Row {row.idx}: {dn} belongs to {values.supplier}, not {row.party}.")
    if values.get("company") and values.company != doc.company:
        frappe.throw(f"Row {row.idx}: {dn} belongs to {values.company}, not {doc.company}.")
    problems = owed.document_problems(dt, values)
    return problems[0] if problems else None


def _check_slip_line(doc, row, slip) -> str | None:
    """A Salary Slip (HRMS): of the line's employee and company, in a Payroll Entry (its payroll
    payable is what the salary entry clears). Returns its first other problem."""
    if not owed.hrms_installed():
        frappe.throw(f"Row {row.idx}: Salary Slips are paid when HRMS is installed.")
    if row.party_type != "Employee" or not row.party:
        frappe.throw(f"Row {row.idx}: {slip} is paid to its employee: pick Employee.")
    values = owed.read_slip(slip)
    if not values:
        frappe.throw(f"Row {row.idx}: Salary Slip {slip} not found.")
    if values.employee != row.party:
        frappe.throw(f"Row {row.idx}: {slip} belongs to {values.employee}, not {row.party}.")
    if values.get("company") and values.company != doc.company:
        frappe.throw(f"Row {row.idx}: {slip} belongs to {values.company}, not {doc.company}.")
    if not values.get("payroll_entry"):
        frappe.throw(f"Row {row.idx}: {slip} is not part of a Payroll Entry, so it has no payroll payable to clear.")
    problems = owed.slip_problems(values)
    return problems[0] if problems else None


def _check_party(row) -> str | None:
    account_doctype = PARTY_ACCOUNT.get(row.party_type or "")
    if not account_doctype:
        frappe.throw(f"Row {row.idx}: {row.party_type or 'this party type'} is not paid by the schedule.")
    if not frappe.db.exists(row.party_type, row.party):
        frappe.throw(f"Row {row.idx}: {row.party_type} {row.party} not found.")
    profile = party_accounts.profile(account_doctype)
    types = LINE_TYPES.get(row.party_type)
    row.payee_account_doctype = account_doctype
    row.payee_account = row.payee_account or party_accounts.default_account(account_doctype, row.party, types)
    if not row.payee_account:
        hint = profile.add_hint
        frappe.throw(f"Row {row.idx}: {row.party} has no approved M-Pesa account. {hint[0].upper()}{hint[1:]}")
    acc = party_accounts.get_account(account_doctype, row.payee_account)
    if not acc or acc.docstatus != 1 or party_accounts.party_of(acc, account_doctype) != row.party:
        frappe.throw(f"Row {row.idx}: {row.payee_account} is not an approved M-Pesa account of {row.party}.")
    if types and acc.account_type not in types:
        frappe.throw(f"Row {row.idx}: {acc.name} {SUPPLIER_PHONE}")
    row.route, row.destination = destination_of(acc)
    row.payee_name = frappe.db.get_value(row.party_type, row.party, profile.name_field) or row.party
    for field in ("one_off_type", "one_off_shortcode", "one_off_account"):
        row.set(field, None)
    problem = party_accounts.usable_problem(acc, row.party, account_doctype)
    if row.party_type == "Supplier":
        problem = problem or owed.supplier_hold(row.party)
    return problem


def destination_of(acc) -> tuple:
    """(route, destination) of an approved account: ('B2B Paybill', '888880 / ACME-1'), ('B2C Phone', '2547...')."""
    return party_accounts.destination(acc)


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
    """The shortcode: B2B (or B2C for a phone line) switched on, not paying itself, within its
    limit, for this company."""
    if is_b2c(row):
        return _caught(check_b2c_settings, doc.get("b2c_settings"), flt(row.get("amount")), doc.company)
    request = _request_like(doc, row)
    return _caught(_check_settings, request)


def check_b2c_settings(settings: str, amount: float, company: str) -> None:
    """The B2C shortcode: B2C switched on, an initiator, within its limit, for this company, and
    its Mode of Payment (the B2C disbursement finds the shortcode by it)."""
    meta = frappe.get_meta("Mpesa Settings")
    fields = ["name", "initiator_name", "transaction_limit", *apis.ALL]
    if meta.has_field("company"):
        fields.append("company")
    values = frappe.db.get_value("Mpesa Settings", settings, fields, as_dict=True)
    if not values:
        frappe.throw(f"Mpesa Settings {settings} not found")
    apis.require(values, apis.B2C)
    if not values.initiator_name:
        frappe.throw(f"Mpesa Settings {settings} needs an Initiator Name to pay B2C.")
    if flt(values.transaction_limit) and flt(amount) > flt(values.transaction_limit):
        frappe.throw(f"Amount is above this shortcode's transaction limit of {values.transaction_limit}")
    if values.get("company") and values.company != company:
        frappe.throw(f"Mpesa Settings {settings} pays for {values.company}, not {company}.")
    if not money_in_common.mode_of_payment_for(settings):
        frappe.throw(f"Mode of Payment Mpesa-{settings} not found: save Mpesa Settings {settings} with its Company.")


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
        if dt and row.status == PLANNED and not _sent(row):
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


def _b2c_made(schedule: str) -> bool:
    """A B2C disbursement Pay Selected made for the schedule's phone lines."""
    meta = frappe.get_meta(B2C_DOCTYPE)
    return meta.has_field("payment_schedule") and bool(frappe.db.exists(B2C_DOCTYPE, {"payment_schedule": schedule}))


def check_delete(doc) -> None:
    """A schedule with any line sent (or once sent) is the record of those payments."""
    rows = frappe.db.sql(
        f"""SELECT idx, status, request, b2c_disbursement FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s
        ORDER BY idx""",
        (doc.name, SCHEDULE), as_dict=True)
    sent = [r for r in rows if r.status not in (PLANNED, CANCELLED) or _sent(r)]
    if sent or frappe.db.exists(B2B, {"payment_schedule": doc.name}) or _b2c_made(doc.name):
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

    if line.status != PLANNED or _sent(line):
        return refuse(f"It is {line.status}" + (f" ({_payout(line)})." if _sent(line) else "."))
    if getdate(line.planned_date) > _today():
        return refuse(f"Not due until {_day(line.planned_date)}.")
    if cint(schedule.closed):
        return refuse(f"{schedule.name} is closed.")
    if maker_checker_on() and payer in (line.prepared_by, line.changed_by):
        return refuse("You prepared or last changed this line: another person must pay it.")
    if line.party_type == "Employee" and line.party:
        own = employee_accounts.own_payment_problem(line.party, payer)
        if own:
            return refuse(own)
        if not _document(line)[0] and not employee_accounts.is_manager(payer):
            return refuse(employee_accounts.NO_SLIP)  # nothing caps a payment with no slip
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
        return f"{line.route or 'This route'} is not paid by the schedule."
    b2c = is_b2c(line)
    if b2c and not schedule.get("b2c_settings"):
        return f"Pick Pay From (B2C) on {schedule.name}."
    if not b2c and not schedule.b2b_settings:
        return f"Pick Pay From (B2B) on {schedule.name}."
    try:
        daraja.whole_shillings(line.amount)
    except ValueError as e:
        return str(e)
    if b2c and not B2C_MIN_AMOUNT <= flt(line.amount) <= B2C_MAX_AMOUNT:
        return f"M-Pesa B2C pays KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,} per payment."
    if line.party:
        account_doctype = PARTY_ACCOUNT.get(line.party_type or "")
        if not account_doctype or line.payee_account_doctype not in (None, "", account_doctype):
            return f"{line.party_type or 'This party type'} is not paid by the schedule."
        if line.party_type == "Supplier":
            hold = owed.supplier_hold(line.party)
            if hold:
                return hold
        acc = party_accounts.get_account(account_doctype, line.payee_account)
        problem = party_accounts.usable_problem(acc, line.party, account_doctype)
        if problem:
            return problem
        types = LINE_TYPES.get(line.party_type)
        if types and acc.account_type not in types:
            return f"{acc.name} {SUPPLIER_PHONE}"
        if ROUTE_FOR.get(acc.account_type) != line.route:
            return f"{acc.name} is a {acc.account_type} account, not paid by {line.route or 'this route'}."
        if destination_of(acc) != (line.route, line.destination):
            return f"Its approved account changed since the line was saved: open {line.parent} and save it again."
    else:
        if b2c:
            return "A phone (B2C) line pays an employee's approved phone: pick the employee."
        if not cint(frappe.db.get_single_value(supplier_accounts.SUITE, "allow_one_off_b2b")):
            return "Paying a number not saved on a supplier is switched off (M-Pesa Suite Settings)."
        if not set(ADMIN_ROLES) & set(frappe.get_roles(payer)):
            return "A number not saved on a supplier is paid by an M-Pesa Manager only."
    if b2c:
        return _caught(check_b2c_settings, schedule.b2c_settings, flt(line.amount), schedule.company)
    return _caught(_check_settings, _request_like(schedule, line))


def _document_problem(line, schedule, found, for_update: bool) -> str | None:
    """The document can be paid and has at least the line's amount left, its other planned lines
    and payouts counted (payouts first, the document row last: R4)."""
    dt, dn = _document(line)
    if not dt:
        return None
    result = owed.left_to_pay(dt, dn, exclude_line=line.name, for_update=for_update)
    found.left = result.left
    if dt == SLIP:
        values = owed.read_slip(dn, for_update=for_update)
        problems = owed.slip_problems(values)
        owner = values.employee if values else None
    else:
        values = owed.read_document(dt, dn, for_update=for_update)
        problems = owed.document_problems(dt, values)
        owner = values.supplier if values else None
    if problems:
        return problems[0]
    if owner != line.party:
        return f"{dn} belongs to {owner}, not {line.party}."
    if values.get("company") and values.company != schedule.company:
        return f"{dn} belongs to {values.company}, not {schedule.company}."
    if flt(line.amount) > result.left + 0.001:
        return f"KES {owed.money(line.amount)} is more than is left to pay. {owed.explain(dn, result)}"
    return None


def _warnings(line) -> list:
    """Worth reading before paying: M-Pesa money the supplier got outside this schedule."""
    if not line.party or line.party_type != "Supplier":
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


B2C_LINE_FIELDS = ("name", "parent", "idx", "planned_date", "status", "b2c_disbursement", "b2c_row", "party",
                   "party_type", "payee_account", "payee_account_doctype", "route", "destination", "amount",
                   "reference_doctype", "reference_name", "last_problem")


def b2c_problem(row, disbursement: str, phone=None, request: str | None = None) -> str | None:
    """Why a B2C row made by Pay Selected may not be paid now (None: it may): its disbursement is
    still submitted, it is its line's own, the line is waiting to be sent and due (site time
    zone), amount, payee and slip are the line's, no other payout names the row (``request``: the
    payout being sent, None while it is not made yet), the slip may still be paid (not cancelled
    or withheld, no bank entry for its payroll), and the number is the employee's approved phone,
    still usable. Under a lock on the line.

    The disbursement is read without a lock: its rows' jobs run side by side, and a cancel of it
    must cancel its payouts first, which wait on the payout row the send holds (send_b2c)."""
    if cint(frappe.db.get_value(B2C_DOCTYPE, disbursement, "docstatus")) != 1:
        return f"{disbursement} is not submitted (cancelled?): nothing is sent for it."
    name = cstr(row.get("mpesa_schedule_line")).strip()
    line = frappe.db.get_value(LINE, name, list(B2C_LINE_FIELDS), as_dict=True, for_update=True)
    if not line:
        return f"Payment schedule line {name} not found."
    if line.b2c_row != row.get("name") or line.b2c_disbursement != disbursement:
        return f"{disbursement} is not the payment of its schedule line: pay it with Pay Selected in M-Pesa Payments Due."
    if line.status != SENDING:
        return f"Its schedule line is {line.status}, not waiting to be sent."
    if getdate(line.planned_date) > _today():
        return f"Not due until {_day(line.planned_date)}. A scheduled payment cannot be sent before its planned day."
    if (flt(row.get("allocated_amount"), 2) != flt(line.amount, 2) or row.get("party") != line.party
            or cstr(row.get("reference_name")) != cstr(line.reference_name)):
        return "It does not match its schedule line."
    other = frappe.db.get_value(B2C_REQUEST, {"b2c_payment_disbursement_reference": row.get("name"),
                                              "name": ["!=", request or ""]}, "name")
    if other:
        return f"{other} already pays this row: a row is paid once."
    if line.reference_doctype == SLIP and line.reference_name:
        problems = owed.slip_problems(owed.read_slip(line.reference_name))
        if problems:
            return problems[0]
    phone = phone or row.get("partyb")
    return employee_accounts.phone_problem(line.payee_account, line.party, phone)


def assert_b2c_line_due(request) -> None:
    """The B2C send of a payout made by Pay Selected (services.b2c_request_service): refused before
    the planned day, when it is not its row's and line's own, or not to the approved phone."""
    ref = cstr(request.get("b2c_payment_disbursement_reference")).strip()
    if not ref or not frappe.get_meta(B2C_ROW).has_field("mpesa_schedule_line"):
        return
    row = frappe.db.get_value(B2C_ROW, ref, ["name", "mpesa_schedule_line", "allocated_amount", "party",
                                             "reference_name", "partyb", "b2c_disbursement_request"], as_dict=True)
    if not row or not cstr(row.mpesa_schedule_line).strip():
        return
    if cstr(row.b2c_disbursement_request) != cstr(request.get("name")):
        # the job that makes a scheduled payout links it to its row before anything is sent
        frappe.throw(f"{request.get('name')} is not the payout of its row: pay it with Pay Selected in M-Pesa "
                     "Payments Due.")
    problem = b2c_problem(row, request.get("b2c_payment_disbursement"), phone=request.get("phone_number"),
                          request=request.get("name"))
    if problem:
        frappe.throw(problem)


def check_b2c_request(doc) -> None:
    """A B2C Disbursement Request names a row of a scheduled payout only when the job that makes
    that payout made it (flags cannot come through the API)."""
    ref = cstr(doc.get("b2c_payment_disbursement_reference")).strip()
    if not ref or not frappe.get_meta(B2C_ROW).has_field("mpesa_schedule_line"):
        return
    if not cstr(frappe.db.get_value(B2C_ROW, ref, "mpesa_schedule_line")).strip():
        return
    flags = getattr(doc, "flags", None) or {}
    if not flags.get("mpesa_from_schedule"):
        frappe.throw("A scheduled payment is paid with Pay Selected in M-Pesa Payments Due, not by hand.")


def check_b2c_disbursement(doc) -> None:
    """A B2C disbursement carries schedule lines only when Pay Selected made it (flags cannot come
    through the API); each such row pays its line's employee, to the approved phone."""
    rows = [r for r in doc.get("references") or [] if cstr(r.get("mpesa_schedule_line")).strip()]
    if not rows and not cstr(doc.get("payment_schedule")).strip():
        return
    flags = getattr(doc, "flags", None) or {}
    if not flags.get("mpesa_from_schedule"):
        frappe.throw("A scheduled payment is paid with Pay Selected in M-Pesa Payments Due, not by hand.")


def check_b2c_row(row) -> None:
    """The row's number is its line's approved phone (the row's validate): any other is refused."""
    line = frappe.db.get_value(LINE, row.mpesa_schedule_line, list(B2C_LINE_FIELDS), as_dict=True)
    if not line or line.route not in B2C_ROUTES:
        frappe.throw(f"Row #{row.idx}: payment schedule line {row.mpesa_schedule_line} is not a phone line.")
    if row.party != line.party or cstr(row.reference_name) != cstr(line.reference_name):
        frappe.throw(f"Row #{row.idx} does not match its payment schedule line.")
    if flt(row.allocated_amount, 2) != flt(line.amount, 2):
        frappe.throw(f"Row #{row.idx}: the amount must be the schedule line's, KES {owed.money(line.amount)}.")
    acc = employee_accounts.get_account(line.payee_account)
    if not acc or cstr(row.partyb).strip() != cstr(acc.phone).strip():
        frappe.throw(f"Row #{row.idx}: the number must be the employee's approved phone ({line.payee_account}).")


def send_b2c(name: str) -> bool:
    """Send a scheduled B2C payout; True when it was posted. The job that makes it calls this once
    the payout and its row's link are committed, so a row with no payout was never sent.

    As b2b.send_deferred (R1): the claim is a locking read, and the send_started_on marker is
    committed before anything else, so a worker that dies after it leaves a payout the 5-minute
    sweep makes Status Unknown (a Transaction Status check settles it), never one sent again.
    The checks (b2c_request_service.ready_to_send: the date, the row's and line's own payout, the
    approved phone) run once more; one that fails marks it Failed, "Not sent: <reason>", while it
    is still this send's own. Nothing after the call raises."""
    from ..services import b2c_request_service as service

    claim = frappe.db.get_value(B2C_REQUEST, name, ["docstatus", "status", "send_started_on"], as_dict=True,
                                for_update=True)
    if (not claim or cint(claim.docstatus) != 1 or (claim.status or NOT_INITIATED) != NOT_INITIATED
            or claim.send_started_on):
        return False  # cancelled, sent, failed, or being sent by another worker
    marker = now_datetime()
    frappe.db.set_value(B2C_REQUEST, name, "send_started_on", marker, update_modified=False)
    frappe.db.commit()  # the marker stays whatever happens next
    frappe.db.savepoint("mpsa_b2c_send_checks")
    try:
        doc = service.ready_to_send(name)
    except Exception as e:
        try:
            frappe.db.rollback(save_point="mpsa_b2c_send_checks")  # the checks write nothing that matters
        except Exception:
            frappe.db.rollback()  # a deadlock ended the transaction, savepoint and all
        if isinstance(e, (frappe.ValidationError, frappe.PermissionError)):
            _forget_message()
        _fail_unsent_b2c(name, marker, f"Not sent: {_plain(e) or type(e).__name__}")
        frappe.db.commit()
        return False
    try:
        service.send(doc)  # the connector records the ack: Initiated, Failed or Status Unknown
    except Exception:
        # it may have reached M-Pesa: the marker stays, and the sweep makes it Status Unknown (R1)
        frappe.db.rollback()
        frappe.log_error(title="M-Pesa scheduled B2C payout send failed", message=f"{name}\n{frappe.get_traceback()}",
                         reference_doctype=B2C_REQUEST, reference_name=name)
    frappe.db.commit()
    return True


def _fail_unsent_b2c(name: str, marker, message: str) -> None:
    """Failed with its row and line, only while the payout is still Not Initiated with this send's
    marker (a locking read): an answer or a cancel committed meanwhile is kept."""
    current = frappe.db.get_value(B2C_REQUEST, name, ["status", "send_started_on", "b2c_payment_disbursement_reference"],
                                  as_dict=True, for_update=True)
    if (not current or (current.status or NOT_INITIATED) != NOT_INITIATED
            or cstr(current.send_started_on)[:19] != cstr(marker)[:19]):
        return
    frappe.db.set_value(B2C_REQUEST, name, {"status": "Failed", "error_message": message[:1000]}, update_modified=False)
    row = current.b2c_payment_disbursement_reference
    if row:
        frappe.db.set_value(B2C_ROW, row, "payment_status", "Failed", update_modified=False)
        sync_b2c_line(row)


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
            "pay_from": pay_from(schedule, line)}


@frappe.whitelist(methods=["POST"])
def prepare_payment(lines) -> dict:
    """The confirm step's summary for the ticked lines, and the token Confirm needs.

    Per payee and per shortcode and route, the grand total, each shortcode's balance with its age
    and any shortfall, the lines left out and why. The token is bound to this user and the lines
    as shown, used once, and works from 5 seconds to 10 minutes after this call."""
    require_named_user()
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
        # one row per payee and paybill or till, so the payer reads where each amount goes
        key = (item["party_type"] or "", item["party"] or item["payee"], cstr(item["destination"]))
        p = parties.setdefault(key, {"payee": item["payee"], "party_type": item["party_type"], "party": item["party"],
                                     "route": item["route"], "destination": item["destination"], "lines": 0,
                                     "amount": 0.0})
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
        "parties": sorted(parties.values(), key=lambda p: (cstr(p["payee"]).lower(), cstr(p["party"]),
                                                            cstr(p["destination"]))),
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
    require_named_user()
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
    sent, refused, touched, requests, groups = [], [], set(), [], {}

    def refuse(item, e):
        reason = _reason(e, item)
        if frappe.db.exists(LINE, item["line"]):
            frappe.db.set_value(LINE, item["line"], "last_problem", reason[:1000], update_modified=False)
        refused.append({"line": item["line"], "reason": reason, **_brief(item["line"])})

    for item in _in_lock_order(data.get("lines") or []):
        frappe.db.savepoint("mpsa_pay_line")
        try:
            request, line, schedule = _pay_line(item, batch, user)
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            raise  # the whole transaction is gone: nothing here was paid
        except Exception as e:
            frappe.db.rollback(save_point="mpsa_pay_line")
            refuse(item, e)
            continue
        if request is None:
            # a phone line: paid below by its group's B2C disbursement (its locks are held to commit)
            groups.setdefault(_group_key(line, schedule), []).append((item, line, schedule))
            continue
        touched.add(line.parent)
        requests.append(request)
        sent.append({"line": line.name, "request": request, "payee": _payee(line), "amount": flt(line.amount, 2)})
    for _key, members in sorted(groups.items(), key=lambda kv: tuple(cstr(k) for k in kv[0])):
        frappe.db.savepoint("mpsa_pay_b2c")
        try:
            disbursement = _pay_b2c_group(members, batch, user)
        except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
            raise
        except Exception as e:
            frappe.db.rollback(save_point="mpsa_pay_b2c")
            for item, _line, _schedule in members:
                refuse(item, e)
            continue
        for _item, line, _schedule in members:
            touched.add(line.parent)
            sent.append({"line": line.name, "request": disbursement, "payee": _payee(line),
                         "amount": flt(line.amount, 2)})
    for schedule in sorted(touched):
        update_totals(schedule, bump=True)  # a schedule form opened before must be reloaded
    if requests:
        _queue_send(requests, batch, enqueue_after_commit=True)
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
    """Check the line again under its locks and submit its B2B request (sent later). A phone line
    is only checked here: (None, line, schedule), and its group's disbursement pays it."""
    peek = frappe.db.get_value(LINE, item["line"], ["parent", "reference_doctype", "reference_name", "route"],
                               as_dict=True)
    if not peek:
        raise _Refused("This line was deleted.")
    dt, dn = _document(peek)
    with contextlib.ExitStack() as stack:
        if dt:
            stack.enter_context(owed.doc_lock(dt, dn))  # R4: the document's lock first
        schedule = _header(peek.parent, for_update=True)
        line = _line(item["line"], for_update=True)
        if not schedule or not line or _document(line) != (dt, dn) or line.route != peek.route:
            raise _Refused(CHANGED)
        check = line_problem(line, schedule, for_update=True, payer=payer, snapshot=item)
        if check.problem:
            raise _Refused(check.problem)
        if is_b2c(line):
            return None, line, schedule
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
        with send_confirm.deliberate("Pay Selected", doc):  # this confirm is the payer's deliberate step
            doc.submit()  # every B2B check again under the document's lock; sent later (flags)
    return doc.name, line, schedule


def _group_key(line, schedule) -> tuple:
    """One B2C disbursement per schedule, B2C shortcode, party type, document type and Payroll Entry."""
    dt, dn = _document(line)
    payroll = owed.read_slip(dn).get("payroll_entry") if dt == SLIP else None
    return (schedule.name, schedule.b2c_settings, line.party_type, dt, payroll)


def _pay_b2c_group(members, batch, payer) -> str:
    """One B2C Payment Disbursement for a group of checked phone lines, submitted: B2C's own guard
    checks each salary slip again under its lock, and its jobs make and send each payout after
    this commits. Each row carries its line and the approved phone (the row refuses another).
    Made with ignore_permissions: the payer passed the payout roles and the line checks."""
    _, first, schedule = members[0]
    settings = schedule.b2c_settings
    check_b2c_settings(settings, max(flt(m[1].amount) for m in members), schedule.company)
    bank_account = wallet_accounts.bank_account(settings, wallet_accounts.UTILITY)
    paid_from = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
    if not paid_from:
        raise _Refused(f"Map a Bank Account on Mpesa Settings {settings} (Accounting Mapping).")
    dt, _dn = _document(first)
    if dt != SLIP:
        raise _Refused("A phone (B2C) line pays a Salary Slip.")
    payroll = owed.read_slip(first.reference_name).payroll_entry
    paid_to = frappe.db.get_value("Payroll Entry", payroll, "payroll_payable_account") if payroll else None
    if not paid_to:
        raise _Refused(f"Payroll Entry {payroll} has no Payroll Payable Account.")
    has_payroll_column = frappe.get_meta(B2C_ROW).has_field("payroll_entry")
    has_payee_columns = frappe.get_meta(B2C_ROW).has_field("payee_account")
    doc = frappe.new_doc(B2C_DOCTYPE)
    doc.update({
        "naming_series": "MPESA-B2C-.YYYY.-",
        "company": schedule.company,
        "posting_date": _today(),
        "payment_type": "Mpesa Disbursement",
        "party_type": first.party_type,
        "transaction_to_pay_against": SLIP,
        "mode_of_payment": money_in_common.mode_of_payment_for(settings),
        "paid_from": paid_from,
        "paid_from_account_currency": "KES",
        "paid_to": paid_to,
        "paid_to_account_currency": "KES",
        "paid_amount": flt(sum(flt(m[1].amount) for m in members), 2),
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
        "payment_schedule": schedule.name,
    })
    for _item, line, _schedule in members:
        row = {"reference_doctype": SLIP, "reference_name": line.reference_name, "party_type": line.party_type,
               "party": line.party, "allocated_amount": flt(line.amount), "partyb": line.destination,
               "currency": "KES", "exchange_rate": 1, "mpesa_schedule_line": line.name}
        if has_payroll_column:
            row["payroll_entry"] = payroll
        if has_payee_columns:  # the approved phone account the row pays (its own lock checks it)
            row.update(payee_account_doctype=line.payee_account_doctype, payee_account=line.payee_account)
        doc.append("references", row)
    doc.flags.ignore_permissions = True
    doc.flags.mpesa_from_schedule = True
    doc.insert(ignore_permissions=True)
    found = frappe.db.get_value("Mpesa Settings", {"payment_gateway_name": cstr(doc.mode_of_payment)[6:]}, "name")
    if found != settings:
        # B2C finds the shortcode by its Mode of Payment
        raise _Refused(f"Mode of Payment {doc.mode_of_payment} belongs to Mpesa Settings {found or 'none'}, not "
                       f"{settings}.")
    now = now_datetime()
    for row in doc.references:
        line = next(m[1] for m in members if m[1].name == row.mpesa_schedule_line)
        frappe.db.set_value(LINE, line.name, {
            "status": SENDING, "b2c_disbursement": doc.name, "b2c_row": row.name, "approved_by": payer,
            "approved_on": now, "batch": batch, "attempts": cint(line.attempts) + 1, "last_problem": None,
        }, update_modified=False)
    with send_confirm.deliberate("Pay Selected", doc):  # this confirm is the payer's deliberate step
        doc.submit()  # B2C's guard under each slip's lock; its jobs make and send the payouts after commit
    return doc.name


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
    dt, dn = _document(line)
    if line.party and line.party_type == "Employee":
        doc.employee = line.party
        doc.employee_mpesa_account = line.payee_account
        if dt == SLIP:
            doc.salary_slip = dn
    elif line.party:
        doc.supplier = line.party
        doc.supplier_mpesa_account = line.payee_account
    else:
        doc.command_id = supplier_accounts.COMMAND_FOR[line.one_off_type]
        doc.receiver_shortcode = line.one_off_shortcode
        doc.account_reference = line.one_off_account
    if dt in owed.DOC_FIELDS:
        doc.set(owed.DOC_FIELDS[dt], dn)
    doc.flags.mpesa_from_schedule = True
    doc.flags.mpesa_send_later = True
    return doc


def _queue_send(requests, batch, **kwargs) -> None:
    """send_batch on the long queue, under one job id per batch: the sweep's deduplicate then
    queues a batch again only once its job is no longer queued or running."""
    frappe.enqueue(f"{__name__}.send_batch", queue="long", timeout=3600, job_id=f"mpesa_schedule_send_{batch}",
                   requests=requests, batch=batch, **kwargs)


def send_batch(requests, batch=None) -> None:
    """The job confirm_payment queues (and the sweep, for requests still not started): each
    request sent once, committed one by one (b2b.send_deferred). It waits the interval only after
    a request that was posted, not after one already sent or cancelled."""
    from . import b2b

    interval = _interval()
    posted = False
    for name in requests or []:
        if posted and interval:
            time.sleep(interval)
        try:
            posted = bool(b2b.send_deferred(name))
        except Exception:
            posted = True  # it may have been posted: keep the interval
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
                        "entry_status", "error_message", "result_desc", "send_started_on"], as_dict=True)
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
        if cancelled_mid_send(values):
            problem = (f"{values.name} was cancelled after its send began, so M-Pesa may have paid it. A manager "
                       "checks the statement, then plans it again.")
        else:
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


# ---------------------------------------------------------------- a phone line follows its B2C payout
B2C_REQUEST_FIELDS = ("name", "docstatus", "status", "transaction_id", "transaction_completed_datetime",
                      "accounting_booked", "accounting_error", "error_message", "result_desc")


def _b2c_request_fields() -> list:
    """B2C_REQUEST_FIELDS, and the send marker once migrate has made it."""
    marker = ["send_started_on"] if frappe.get_meta(B2C_REQUEST).has_field("send_started_on") else []
    return [*B2C_REQUEST_FIELDS, *marker]


def b2c_never_sent(request) -> bool:
    """A scheduled B2C payout that provably never reached M-Pesa: Failed, or Not Initiated with no
    send marker (send_b2c commits the marker before the call)."""
    status = request.get("status") or NOT_INITIATED
    return status == "Failed" or (status == NOT_INITIATED and not request.get("send_started_on"))


def sync_b2c_line(row_name) -> None:
    """Mirror a B2C row made by Pay Selected, and its B2C Disbursement Request, on its line. Never
    raises, except a deadlock or lock wait (as sync_line)."""
    try:
        if not row_name or not frappe.get_meta(B2C_ROW).has_field("mpesa_schedule_line"):
            return
        row = frappe.db.get_value(B2C_ROW, row_name, ["name", "parent", "payment_status", "b2c_disbursement_request",
                                                     "mpesa_schedule_line"], as_dict=True)
        if not row or not cstr(row.mpesa_schedule_line).strip():
            return
        line = frappe.db.get_value(LINE, row.mpesa_schedule_line, ["name", "parent", "b2c_row", *SYSTEM], as_dict=True)
        if not line or line.b2c_row != row.name:
            return  # planned again since: the line belongs to another payout now
        request = row.b2c_disbursement_request or frappe.db.get_value(
            B2C_REQUEST, {"b2c_payment_disbursement_reference": row.name, "docstatus": ["<", 2]}, "name")
        values = frappe.db.get_value(B2C_REQUEST, request, _b2c_request_fields(), as_dict=True) if request else None
        mirror = _mirror_b2c(row, values, line)
        changed = {k: v for k, v in mirror.items() if _norm(k, line.get(k)) != _norm(k, v)}
        if not changed:
            return
        frappe.db.set_value(LINE, line.name, changed, update_modified=False)
        frappe.enqueue(f"{__name__}.update_totals", queue="short", timeout=300, enqueue_after_commit=True,
                       schedule=line.parent)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise
    except Exception:
        frappe.log_error(title="M-Pesa schedule line not updated", message=f"{row_name}\n{frappe.get_traceback()}")


def sync_b2c_line_later(row_name) -> None:
    """sync_b2c_line from a job once the caller commits (the B2C Result, its booking, the send)."""
    if not row_name:
        return
    try:
        frappe.enqueue(f"{__name__}.sync_b2c_line", queue="short", timeout=300, enqueue_after_commit=True,
                       row_name=row_name)
    except Exception:
        frappe.log_error(title="M-Pesa schedule line update not queued", message=f"{row_name}\n{frappe.get_traceback()}")


def _mirror_b2c(row, request, line) -> dict:
    if not request:
        if row.payment_status == "Failed":
            problem = cstr(line.get("last_problem")).strip() or "Not sent: no B2C payout was made for it (see the Error Log)."
            return {"status": FAILED, "b2c_request": None, "receipt": None, "paid_on": None, "entry_status": None,
                    "last_problem": problem}
        return {"status": SENDING, "b2c_request": None, "receipt": None, "paid_on": None, "entry_status": None,
                "last_problem": None}
    status = request.status or "Not Initiated"
    problem, entry = None, None
    if status == "Paid":
        line_status = PAID
        booked = bool(cint(request.accounting_booked))
        entry = "Entry Made" if booked else "Entry Not Made"
        problem = None if booked else (_plain(request.accounting_error) or None)
    elif cint(request.docstatus) == 2 and b2c_never_sent(request):
        line_status = FAILED
        problem = f"{request.name} was cancelled ({status}): nothing was paid. Plan it again."
    elif cint(request.docstatus) == 2:
        # cancelled while M-Pesa may have paid it (only a manager may: its before_cancel)
        line_status = UNKNOWN
        problem = (f"{request.name} was cancelled while M-Pesa may have paid it ({status}). An M-Pesa or Accounts "
                   "Manager checks the statement, then plans it again.")
    elif status == "Failed":
        line_status = FAILED
        problem = _plain(request.error_message or request.result_desc) or "M-Pesa refused it."
    elif status == "Status Unknown":
        line_status = UNKNOWN
        problem = (f"{_plain(request.error_message) or 'No answer from M-Pesa.'} A Transaction Status check settles "
                   f"{request.name}; plan the line again only once it shows Failed.")
    elif status == "Initiated":
        line_status = SENT
    else:
        line_status = SENDING
    paid = line_status == PAID
    return {"status": line_status, "b2c_request": request.name,
            "receipt": (cstr(request.transaction_id).strip() or None) if paid else None,
            "paid_on": request.transaction_completed_datetime if paid else None,
            "entry_status": entry, "last_problem": problem}


def fail_b2c_line(row_name, message: str) -> None:
    """A B2C row made by Pay Selected that its job did not pay (b2c_problem): its line Failed
    with the reason, to be planned again."""
    row = frappe.db.get_value(B2C_ROW, row_name, ["name", "mpesa_schedule_line"], as_dict=True)
    if not row or not row.mpesa_schedule_line:
        return
    line = frappe.db.get_value(LINE, row.mpesa_schedule_line, ["name", "parent", "b2c_row"], as_dict=True)
    if not line or line.b2c_row != row.name:
        return
    frappe.db.set_value(LINE, line.name, {"status": FAILED, "last_problem": message[:1000]}, update_modified=False)
    frappe.enqueue(f"{__name__}.update_totals", queue="short", timeout=300, enqueue_after_commit=True,
                   schedule=line.parent)


def cancelled_mid_send(request) -> bool:
    """Cancelled while still Not Initiated after its send began (send_started_on): M-Pesa may have
    paid it (R1). The request's before_cancel keeps such a cancel Unknown, so only a cancel that
    went around it shows this."""
    return (cint(request.get("docstatus")) == 2 and (request.get("status") or "Not Initiated") == "Not Initiated"
            and bool(request.get("send_started_on")))


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
    schedule with the old and new day, and makes the mover the line's last changer. Who prepares
    payouts (PREPARE_ROLES), not System Manager: a moved line is payable on its new day."""
    require(PREPARE_ROLES)
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
            if line.status != PLANNED or _sent(line):
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
    have been paid: a manager checks the statement and cancels its request first. A line whose
    request was cancelled after its send began is planned again by a manager only, by name (the
    shared Administrator login holds every role but checked no statement: decision 1)."""
    require(PREPARE_ROLES)
    parent = frappe.db.get_value(LINE, line, "parent")
    if not parent:
        frappe.throw(f"Payment schedule line {line} not found.")
    frappe.has_permission(SCHEDULE, "write", doc=parent, throw=True)
    schedule = _header(parent, for_update=True)
    if cint(schedule.closed):
        frappe.throw(f"{parent} is closed.")
    row = _line(line, for_update=True)
    if row.b2c_disbursement:
        if row.status not in (FAILED, UNKNOWN):
            frappe.throw(f"Only a failed line is planned again; row {row.idx} is {row.status}.")
        _check_b2c_not_paid(row)  # an Unknown one only once a manager settled its payout
    elif row.status == UNKNOWN:
        frappe.throw(f"M-Pesa may have paid row {row.idx} ({row.request}). A manager checks the statement and cancels "
                     f"{row.request} if it was not paid; then plan it again.")
    elif row.status != FAILED:
        frappe.throw(f"Only a failed line is planned again; row {row.idx} is {row.status}.")
    if row.request:
        request = frappe.db.get_value(B2B, row.request, ["status", "docstatus", "send_started_on"], as_dict=True,
                                      for_update=True)
        if request and (request.status == "Paid" or (cint(request.docstatus) == 1 and request.status != "Failed")):
            frappe.throw(f"{row.request} is {request.status}: row {row.idx} is not planned again.")
        if request and cancelled_mid_send(request) and not _named_manager():
            frappe.throw(f"M-Pesa may have paid row {row.idx}: {row.request} was cancelled after its send began. A "
                         "manager checks the statement, then plans it again.")
    values = {"status": PLANNED, "request": None, "receipt": None, "paid_on": None, "entry_status": None,
              "approved_by": None, "approved_on": None, "batch": None, "last_problem": None,
              "b2c_disbursement": None, "b2c_row": None, "b2c_request": None,
              "changed_by": frappe.session.user}
    if date:
        if getdate(date) < _today():
            frappe.throw(f"Pick today or a later day: {_day(date)} has passed.")
        values["planned_date"] = getdate(date)
    frappe.db.set_value(LINE, line, values)
    frappe.get_doc(SCHEDULE, parent).add_comment(
        "Comment", f"{frappe.session.user} planned row {row.idx} ({_payee(row)}, KES {owed.money(row.amount)}) again "
        f"after {_payout(row) or 'it'} failed: {row.last_problem or 'no reason given'}.")
    update_totals(parent, bump=True)
    return {"line": line, "status": PLANNED}


def _named_manager() -> bool:
    """True for an M-Pesa Manager (ADMIN_ROLES), who has checked the statement before planning a
    line whose payout may have been paid. The shared Administrator login holds every role but names
    nobody: refused ("Use your own login") while M-Pesa Suite Settings refuses it (decision 1)."""
    require_named_user()
    return bool(set(ADMIN_ROLES) & set(frappe.get_roles(frappe.session.user)))


def _check_b2c_not_paid(row) -> None:
    """A phone line is planned again only when its B2C payout provably was not paid: it is Failed,
    or it never started (cancelled before its send, or its row got none). One cancelled while
    M-Pesa may have paid it (Initiated, Status Unknown, or its send began) is planned again by a
    manager only, who checked the statement first; a live one waits for its answer or for a
    Transaction Status check."""
    payout = row.b2c_request or frappe.db.get_value(B2C_ROW, row.b2c_row, "b2c_disbursement_request")
    if not payout:
        payout = frappe.db.get_value(B2C_REQUEST, {"b2c_payment_disbursement_reference": row.b2c_row}, "name")
    if not payout:
        status = frappe.db.get_value(B2C_ROW, row.b2c_row, "payment_status", for_update=True)
        if status != "Failed" or row.status != FAILED:
            frappe.throw(f"Row {row.idx} of {row.b2c_disbursement} is {status or row.status}: row {row.idx} is not "
                         "planned again.")
        return
    request = frappe.db.get_value(B2C_REQUEST, payout, ["status", "docstatus", *(
        ["send_started_on"] if frappe.get_meta(B2C_REQUEST).has_field("send_started_on") else [])], as_dict=True,
        for_update=True)
    if not request:
        frappe.throw(f"{payout} not found: row {row.idx} is not planned again.")
    status = request.status or NOT_INITIATED
    if status == "Paid":
        frappe.throw(f"{payout} is Paid: row {row.idx} is not planned again.")
    if status == "Failed" or (cint(request.docstatus) == 2 and b2c_never_sent(request)):
        return
    if cint(request.docstatus) == 2:
        if _named_manager():
            return  # a manager cancelled it as not paid, having checked the statement
        frappe.throw(f"M-Pesa may have paid row {row.idx}: {payout} was cancelled while it was {status}. An M-Pesa "
                     "Manager checks the statement, then plans it again.")
    if status == "Status Unknown" or row.status == UNKNOWN:
        frappe.throw(f"M-Pesa may have paid row {row.idx} ({payout}). A Transaction Status check settles it; plan it "
                     "again only once it shows Failed.")
    frappe.throw(f"{payout} is {status}: row {row.idx} is not planned again.")


@frappe.whitelist(methods=["POST"])
def close_schedule(name) -> dict:
    """Close the schedule: its planned lines are cancelled; lines already sent carry on."""
    require(PREPARE_ROLES)
    frappe.has_permission(SCHEDULE, "write", doc=name, throw=True)
    schedule = _header(name, for_update=True)
    if not schedule:
        frappe.throw(f"{SCHEDULE} {name} not found.")
    if cint(schedule.closed):
        return {"cancelled": 0}
    user = frappe.session.user
    cancelled = 0
    for line in _stored_lines(name).values():
        if line.status == PLANNED and not _sent(line):
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
    resend = {}
    for row in frappe.get_all(B2B, filters={"docstatus": 1, "status": "Not Initiated",
                                            "send_started_on": ["is", "not set"], "schedule_line": ["is", "set"],
                                            "creation": ["<", add_to_date(now, minutes=-RESEND_AFTER_MINUTES)]},
                              fields=["name", "creation", "schedule_line"], order_by="creation asc", limit=limit):
        if get_datetime(row.creation) >= add_to_date(now, minutes=-RESEND_WITHIN_MINUTES):
            resend.setdefault(_batch_of(row), []).append(row.name)
        else:
            _each(row.name, _give_up)
    for batch, names in sorted(resend.items()):
        # not while the batch's own job (confirm's, or an earlier sweep's) is queued or running:
        # a large batch takes longer than 5 minutes
        _queue_send(names, batch, deduplicate=True)
    for request, row in frappe.db.sql(
        f"""
        SELECT request, b2c_row FROM `tab{LINE}`
        WHERE parenttype = %(parenttype)s AND status IN %(states)s
          AND (COALESCE(request, '') != '' OR COALESCE(b2c_row, '') != '')
        ORDER BY modified LIMIT {cint(limit)}
        """,
        {"parenttype": SCHEDULE, "states": IN_PROGRESS},
    ):
        if request:
            _each(request, sync_line)
        else:
            _each(row, sync_b2c_line)
    if frappe.get_meta(B2C_REQUEST).has_field("send_started_on"):
        for (name,) in frappe.db.sql(
            f"""
            SELECT name FROM `tab{B2C_REQUEST}`
            WHERE docstatus = 1 AND COALESCE(status, '') IN ('', %(not_initiated)s)
              AND send_started_on IS NOT NULL AND send_started_on < %(cutoff)s
            ORDER BY send_started_on LIMIT {cint(limit)}
            """,
            {"not_initiated": NOT_INITIATED, "cutoff": add_to_date(now, minutes=-INTERRUPTED_MINUTES)},
        ):
            _each(name, _interrupted_b2c)
    for (row,) in frappe.db.sql(
        f"""
        SELECT b2c_row FROM `tab{LINE}`
        WHERE parenttype = %(parenttype)s AND status = %(sending)s AND COALESCE(b2c_row, '') != ''
          AND approved_on < %(cutoff)s
        ORDER BY approved_on LIMIT {cint(limit)}
        """,
        {"parenttype": SCHEDULE, "sending": SENDING, "cutoff": add_to_date(now, minutes=-RESEND_WITHIN_MINUTES)},
    ):
        _each(row, _give_up_b2c)


def _batch_of(request) -> str:
    """The Pay Selected batch a request was confirmed in (its line's), else the request itself."""
    line = frappe.db.get_value(LINE, request.schedule_line, ["request", "batch"], as_dict=True)
    return line.batch if line and line.request == request.name and line.batch else request.name


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


def _interrupted_b2c(name: str) -> None:
    """A scheduled B2C payout whose send began (its marker was committed) and never recorded an
    answer: M-Pesa may have paid it, so it is Status Unknown, never sent again (R1), and its line
    Unknown until a Transaction Status check settles it."""
    values = frappe.db.get_value(B2C_REQUEST, name, ["docstatus", "status", "send_started_on",
                                                     "b2c_payment_disbursement_reference"], as_dict=True, for_update=True)
    if (not values or cint(values.docstatus) != 1 or (values.status or NOT_INITIATED) != NOT_INITIATED
            or not values.send_started_on):
        return
    frappe.db.set_value(B2C_REQUEST, name, {"status": "Status Unknown", "error_message": B2C_INTERRUPTED},
                        update_modified=False)
    if values.b2c_payment_disbursement_reference:
        sync_b2c_line(values.b2c_payment_disbursement_reference)
    frappe.log_error(title="M-Pesa scheduled B2C payout interrupted", message=f"{name}: {B2C_INTERRUPTED}",
                     reference_doctype=B2C_REQUEST, reference_name=name)
    supplier_accounts._alert(f"{name}: {B2C_INTERRUPTED}", frappe._dict(doctype=B2C_REQUEST, name=name))


def _give_up_b2c(row_name: str) -> None:
    """A phone line's B2C row still Sending an hour after Pay Selected: its job was lost.

    The job commits the payout, linked to its row, before anything is sent, and the send commits
    its marker first (send_b2c). So a row with no payout was never sent: it is Failed and its
    line may be planned again. A payout that never started (no marker) is Failed, "Not sent", the
    same way. One whose send began is left to the interrupted rule (Status Unknown, never
    Failed). Locks: the payout, the line, then the row, as the job and the send take them; a late
    job refuses the row (b2c_problem: the line is no longer Sending)."""
    peek = frappe.db.get_value(B2C_ROW, row_name, ["name", "mpesa_schedule_line", "b2c_disbursement_request"],
                               as_dict=True)
    if not peek:
        return
    payout = peek.b2c_disbursement_request or frappe.db.get_value(
        B2C_REQUEST, {"b2c_payment_disbursement_reference": row_name}, "name")
    request = frappe.db.get_value(B2C_REQUEST, payout, _b2c_request_fields(), as_dict=True,
                                  for_update=True) if payout else None
    line = frappe.db.get_value(LINE, peek.mpesa_schedule_line, ["name", "parent", "idx", "status", "b2c_row"],
                               as_dict=True, for_update=True)
    row = frappe.db.get_value(B2C_ROW, row_name, ["name", "parent", "idx", "payment_status", "b2c_disbursement_request",
                                                 "mpesa_schedule_line"], as_dict=True, for_update=True)
    if not row or not line or line.b2c_row != row_name or line.status != SENDING:
        return
    if not request and (row.b2c_disbursement_request or frappe.db.exists(
            B2C_REQUEST, {"b2c_payment_disbursement_reference": row_name})):
        # its job committed a payout since the first read: never failed here unlocked; the next
        # sweep looks again with that payout locked first
        sync_b2c_line(row_name)
        return
    if request:
        if (cint(request.docstatus) == 1 and (request.status or NOT_INITIATED) == NOT_INITIATED
                and not request.get("send_started_on")):
            frappe.db.set_value(B2C_REQUEST, request.name, {
                "status": "Failed",
                "error_message": "Not sent: the job that sends it was lost before it started. Plan the line again."},
                update_modified=False)
            frappe.db.set_value(B2C_ROW, row_name, "payment_status", "Failed", update_modified=False)
            _note_given_up(row, line, f"{request.name} was never sent (its job was lost): nothing was paid. Plan it "
                                      "again.")
        sync_b2c_line(row_name)
        return
    if row.payment_status in ("Paid", "Failed"):
        sync_b2c_line(row_name)
        return
    frappe.db.set_value(B2C_ROW, row_name, "payment_status", "Failed", update_modified=False)
    message = (f"No B2C payout was made for row {row.idx} of {row.parent} within an hour, so nothing was sent: "
               "plan it again.")
    fail_b2c_line(row_name, message)
    _note_given_up(row, line, message)


def _note_given_up(row, line, message: str) -> None:
    frappe.get_doc(SCHEDULE, line.parent).add_comment("Comment", f"Row {line.idx}: {message}")
    frappe.log_error(title="M-Pesa scheduled B2C payout not made", message=f"{row.name}: {message}",
                     reference_doctype=B2C_DOCTYPE, reference_name=row.parent)


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


# ---------------------------------------------------------------- from a Payroll Entry (HRMS)
@frappe.whitelist(methods=["POST"])
def from_payroll_entry(payroll_entry, b2b_settings=None, b2c_settings=None, planned_date=None) -> dict:
    """The Payroll Entry's "Create M-Pesa Payment Schedule" button (money_out.payroll)."""
    from . import payroll

    return payroll.from_payroll_entry(payroll_entry, b2b_settings=b2b_settings, b2c_settings=b2c_settings,
                                      planned_date=planned_date)
