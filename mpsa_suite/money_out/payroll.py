"""Salaries from a Payroll Entry (HRMS) through an M-Pesa Payment Schedule.

"Create M-Pesa Payment Schedule" on a submitted Payroll Entry builds ONE schedule for it: a line
per submitted, not withheld Salary Slip of an active employee of the same company, for what the
slip still has to pay in whole shillings, routed by the employee's default approved Employee
M-Pesa Account (a bank account, paybill or till by B2B, a phone by B2C). Employees that cannot
be paid that way are listed on the schedule under Not Included, with the reason. The schedule shows the Payroll
Entry and each line its Salary Slip; a Payroll Entry has at most one live (not closed) schedule.
From there the schedule's rules apply: Accounts prepare, an M-Pesa Payer pays on the day with
Pay Selected after the 5-second confirm, and never before the line's Pay On.

Nothing is paid by M-Pesa while the payroll has a bank or cash entry not made by M-Pesa (HRMS's
Make Bank Entry, draft or submitted): checked when the schedule is made and again whenever one of
its salaries is paid (owed.slip_problems). The other way round, no such entry is saved while
M-Pesa pays salaries of the payroll, Paid (booked or not), on their way or planned
(guard_bank_entry, a Journal Entry validate hook): a payroll is paid either through M-Pesa or
through Make Bank Entry, or split into separate Payroll Entries; when Payroll Settings books
payroll per employee, a bank entry made by hand may pay the employees M-Pesa does not.

A Payroll Entry cannot be cancelled while M-Pesa salaries of it are paid or on their way: HRMS
cancels every Journal Entry referencing it and deletes its salary slips. Nor can one of its
Salary Slips: an amended slip would show its whole net pay as unpaid again (what was paid is
found by the slip's name).

Salary Slips (round 4): "Pay by M-Pesa" on a submitted slip builds a one-line schedule (a one-off
or off-cycle salary) to any of the employee's approved accounts, and the list's "Pay Selected
Slips by M-Pesa" one schedule for the ticked slips, each to its employee's default account. Both
are purpose Payroll (only who may see salaries sees them) and are paid like the Payroll Entry's
schedule. Who prepares them: PREPARE_ROLES who may see salaries (money_out.salary_access), read
the slip and may make a payment schedule (its DocPerms: Accounts and M-Pesa Manager).

HRMS is a required app (hooks.required_apps).
"""

import contextlib

import frappe
from frappe.utils import cint, cstr, flt, getdate, nowdate

from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..money_in import common as money_in_common
from ..permissions import PREPARE_ROLES, require
from ..utils.phone import mask
from . import owed, party_accounts, salary_access

PAYROLL_ENTRY = "Payroll Entry"
SCHEDULE = "M-Pesa Payment Schedule"
LINE = "M-Pesa Payment Schedule Line"
B2B = "Mpesa B2B Payment Request"
B2C_REQUEST = "B2C Disbursement Request"
B2C_ROW = "B2C Payment Disbursement Reference"
EMPLOYEE_ACCOUNT = party_accounts.EMPLOYEE_ACCOUNT
# Payouts still on their way, or Paid
B2B_LIVE = ("Not Initiated", "Initiated", "Unknown")
B2C_LIVE = ("Not Initiated", "Initiated", "Status Unknown")
LINE_SENT = ("Sending", "Sent", "Unknown", "Paid")


class _Row(frappe._dict):
    """A line as the schedule's checks see it (they call row.set)."""

    def set(self, key, value):
        self[key] = value


def _money(value) -> str:
    return owed.money(value)


def whole_shillings(left) -> int:
    """What M-Pesa can pay of ``left``: whole shillings, never more than is left."""
    return int(flt(left, 2) + 0.001)


def live_schedule(payroll_entry: str, for_update: bool = False) -> str | None:
    """The Payroll Entry's schedule that is not closed, if any. ``for_update``: a locking read, for
    the builders under the payroll's schedule lock (a plain read would not see a schedule another
    worker committed since this request began)."""
    if not frappe.get_meta(SCHEDULE).has_field("payroll_entry"):
        return None
    return frappe.db.get_value(SCHEDULE, {"payroll_entry": payroll_entry, "closed": 0}, "name",
                               for_update=for_update)


def bank_entries(payroll_entry: str) -> list:
    """Live Bank or Cash Journal Entries for the Payroll Entry not made by an M-Pesa payout (HRMS's
    Make Bank Entry, or one made by hand): paying through M-Pesa as well could pay twice. Checked
    again whenever one of its salaries is paid (owed.slip_problems)."""
    return owed.payroll_bank_entries(payroll_entry)


def _slips(payroll_entry: str) -> list:
    meta = frappe.get_meta(owed.SLIP)
    fields = [f for f in owed.SLIP_READ if f in ("name", "docstatus") or meta.has_field(f)]
    return frappe.get_all(owed.SLIP, filters={"payroll_entry": payroll_entry, "docstatus": 1}, fields=fields,
                          order_by="employee asc, name asc")


@frappe.whitelist(methods=["POST"])
def from_payroll_entry(payroll_entry, b2b_settings=None, b2c_settings=None, planned_date=None) -> dict:
    """Build the Payroll Entry's M-Pesa Payment Schedule. Who prepares payouts (PREPARE_ROLES, not
    System Manager) with create on the schedule prepares it, so they are its lines' preparer:
    someone else pays them (maker-checker)."""
    require(PREPARE_ROLES)
    require_salary_reader()  # the schedule shows every salary of the payroll (D4)
    if not owed.hrms_installed():
        frappe.throw("Paying a payroll needs HRMS.")
    frappe.has_permission(PAYROLL_ENTRY, "read", doc=payroll_entry, throw=True)
    frappe.has_permission(SCHEDULE, "create", throw=True)
    payroll = frappe.db.get_value(PAYROLL_ENTRY, payroll_entry,
                                  ["name", "docstatus", "company", "start_date", "end_date"], as_dict=True)
    if not payroll:
        frappe.throw(f"Payroll Entry {payroll_entry} not found")
    if cint(payroll.docstatus) != 1:
        frappe.throw(f"Submit {payroll_entry} first.")
    day = getdate(planned_date) if planned_date else getdate(nowdate())
    if day < getdate(nowdate()):
        frappe.throw("Pay On cannot be before today.")
    slips = _slips(payroll_entry)
    # the payroll's schedule lock, then each slip's own (as the Salary Slip builders), and locking
    # reads under them: a schedule, one-off or payout another worker committed since this request
    # began is counted
    with _locks([payroll_entry], [slip.name for slip in slips]), _bank_entries_once():
        live = live_schedule(payroll_entry, for_update=True)
        if live:
            frappe.throw(f"{payroll_entry} already has M-Pesa Payment Schedule {live}. Open it, or close it first.")
        bank = bank_entries(payroll_entry)
        if bank:
            frappe.throw(f"{payroll_entry} already has a bank or cash entry not made by M-Pesa ({', '.join(bank)}). "
                         "Paying it through M-Pesa as well could pay salaries twice: cancel that entry first.")
        header = frappe._dict(name=None, company=payroll.company, b2b_settings=b2b_settings, b2c_settings=b2c_settings)
        lines, excluded = [], []
        for slip in slips:
            left = owed.slip_left_to_pay(slip.name, for_update=True).left
            line, reason = _line_for(header, payroll, slip, day, left=left)
            if reason:
                excluded.append(f"{slip.employee} {slip.get('employee_name') or ''} ({slip.name}): {reason}".replace(
                    "  ", " "))
            else:
                lines.append(line)
        if not lines:
            frappe.throw(f"No salary of {payroll_entry} can be paid by M-Pesa. " + "; ".join(excluded))
        doc = frappe.new_doc(SCHEDULE)
        doc.update({
            "title": f"Salaries {payroll_entry} ({_period(payroll)})"[:140],
            "company": payroll.company,
            "b2b_settings": b2b_settings or None,
            "b2c_settings": b2c_settings or None,
            "purpose": "Payroll",
            "payroll_doctype": PAYROLL_ENTRY,
            "payroll_entry": payroll_entry,
            "excluded": _not_included(excluded),
        })
        for line in lines:
            doc.append("lines", line)
        doc.flags.mpesa_from_payroll = True
        doc.insert()
    return {"name": doc.name, "lines": len(lines), "total": flt(sum(flt(line["amount"]) for line in lines), 2),
            "excluded": excluded}


def _period(payroll) -> str:
    if payroll.get("start_date") and payroll.get("end_date"):
        start, end = getdate(payroll.start_date), getdate(payroll.end_date)
        return f"{start.day} {start:%b} to {end.day} {end:%b %Y}"
    return cstr(payroll.name)


def _line_for(header, payroll, slip, day, account=None, left=None) -> tuple:
    """(line, None), or (None, why this salary is not on the schedule). ``account``: the approved
    Employee M-Pesa Account to pay (a one-off salary), else the employee's default. ``left``: what
    the slip has left, as the caller read it (a locking read under the slip's lock), else read here."""
    from . import schedule

    if slip.get("status") == "Withheld":
        return None, "the salary is withheld"
    employee = frappe.db.get_value("Employee", slip.employee, ["status", "company", "employee_name"], as_dict=True)
    if not employee:
        return None, "employee not found"
    if employee.status != "Active":
        return None, f"the employee is {employee.status}"
    if (slip.get("company") or employee.company) != payroll.company:
        return None, f"the salary slip is of {slip.get('company')}, not {payroll.company}"
    problems = owed.slip_problems(slip)
    if problems:
        return None, problems[0].replace(f"{slip.name} ", "", 1)
    amount = whole_shillings(owed.slip_left_to_pay(slip.name).left if left is None else left)
    if amount < 1:
        return None, "nothing is left to pay"
    account = account or party_accounts.default_account(EMPLOYEE_ACCOUNT, slip.employee)
    if not account:
        if party_accounts.accounts_of(EMPLOYEE_ACCOUNT, slip.employee):
            return None, "several approved M-Pesa accounts and none is the default: press Make Default on one"
        return None, "no approved M-Pesa account: add one under Employee > M-Pesa Accounts and have a manager approve it"
    acc = party_accounts.get_account(EMPLOYEE_ACCOUNT, account)
    route = party_accounts.ROUTE_FOR.get(acc.account_type)
    if route == party_accounts.B2C_PHONE:
        if amount > B2C_MAX_AMOUNT:
            return None, (f"KES {_money(amount)} is more than M-Pesa B2C pays at once (KES {B2C_MAX_AMOUNT:,}): pay it to "
                          "a bank, paybill or till account")
        if amount < B2C_MIN_AMOUNT:
            return None, f"KES {_money(amount)} is less than M-Pesa B2C pays (KES {B2C_MIN_AMOUNT})"
        if not header.b2c_settings:
            return None, "paid to a phone: pick Pay From (B2C)"
    elif not header.b2b_settings:
        return None, "paid to a bank, paybill or till account: pick Pay From (B2B)"
    line = {"planned_date": day, "party_type": "Employee", "party": slip.employee,
            "payee_account_doctype": EMPLOYEE_ACCOUNT, "payee_account": account, "amount": amount,
            "reference_doctype": owed.SLIP, "reference_name": slip.name, "entry_mode": "Book automatically",
            "remarks": f"Salary {_period(payroll)}"[:100]}
    # the schedule's own checks on save, here, so one employee's problem leaves only that salary out
    row = _Row(line, idx=1)
    try:
        schedule._check_line(header, row)
    except (frappe.ValidationError, frappe.PermissionError) as e:
        schedule._forget_message()
        return None, schedule._plain(e).replace("Row 1: ", "", 1)
    return line, None


# ---------------------------------------------------------------- the Payroll Entry's cancel
def before_cancel(doc, method=None) -> None:
    """Payroll Entry before_cancel: refused while M-Pesa salaries of it are paid or on their way
    (HRMS would cancel their entries and delete the slips they paid), or while a payment schedule
    still plans its salaries."""
    if not owed.hrms_installed():
        return
    paying = salaries_paid_or_on_their_way(doc.name)
    if paying:
        frappe.throw(f"{doc.name} cannot be cancelled while M-Pesa salaries of it are paid or on their way: "
                     f"{', '.join(paying[:10])}. Cancelling it would cancel their salary entries and delete the salary "
                     "slips they paid.")
    planned = _schedules(doc.name)
    if planned:
        frappe.throw(f"{doc.name} has M-Pesa Payment Schedule {', '.join(planned)}, which pays its salary slips. Nothing "
                     "was paid from it: delete it first, then cancel.")


def slip_before_cancel(doc, method=None) -> None:
    """Salary Slip before_cancel: refused while an M-Pesa payment of it is Paid (also on a
    cancelled payout: the money left) or on its way. Paid payouts are found by the slip's name, so
    an amended slip would show its whole net pay as unpaid and could be paid again."""
    paying = paid_or_on_their_way([doc.name])
    if paying:
        frappe.throw(f"{doc.name} cannot be cancelled while M-Pesa payments of it are paid or on their way: "
                     f"{', '.join(paying[:10])}. An amended slip would show its whole net pay as unpaid again.")


def _slip_names(payroll_entry: str) -> list:
    return frappe.get_all(owed.SLIP, filters={"payroll_entry": payroll_entry}, pluck="name")


def salaries_paid_or_on_their_way(payroll_entry: str) -> list:
    """M-Pesa payouts of the Payroll Entry's salary slips that are Paid or still on their way."""
    return paid_or_on_their_way(_slip_names(payroll_entry), payroll_entry)


def paid_or_on_their_way(slips: list, payroll_entry: str | None = None) -> list:
    """M-Pesa payouts of these salary slips (and B2B salaries naming the Payroll Entry) that are
    Paid or still on their way."""
    return list(dict.fromkeys(label for label, _slip, _employee in _paying(slips, payroll_entry)))


def _paying(slips: list, payroll_entry: str | None = None, planned: bool = False) -> list:
    """(label, salary slip, employee) of each M-Pesa payout of these salary slips (and B2B salaries
    naming the Payroll Entry) Paid or on its way; with ``planned``, schedule lines still planned too.
    The employee is the B2B request's own; for the rest the caller maps the slip."""
    found = []
    if frappe.db.has_column(B2B, "salary_slip") and (slips or payroll_entry):
        named = " OR ".join((["salary_slip IN %(slips)s"] if slips else [])
                            + (["payroll_entry = %(payroll)s"] if payroll_entry else []))
        found += [(r[0], r[1], r[2]) for r in frappe.db.sql(
            f"""
            SELECT name, salary_slip, employee FROM `tab{B2B}`
            WHERE ({named})
              AND ((docstatus < 2 AND COALESCE(status, '') IN %(live)s) OR (docstatus IN (1, 2) AND status = 'Paid'))
            ORDER BY creation
            """,
            {"payroll": payroll_entry, "slips": tuple(slips) or ("",), "live": ("", *B2B_LIVE)},
        )]
    if slips:
        found += [(r[0], r[1], None) for r in frappe.db.sql(
            f"""
            SELECT name, reference_name FROM `tab{B2C_REQUEST}`
            WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s
              AND ((docstatus = 1 AND COALESCE(status, '') IN %(live)s) OR (docstatus IN (1, 2) AND status = 'Paid'))
            ORDER BY creation
            """,
            {"slips": tuple(slips), "live": ("", *B2C_LIVE)},
        )]
        # rows of a submitted disbursement whose payout is not made yet
        found += [(r[0], r[1], None) for r in frappe.db.sql(
            f"""
            SELECT DISTINCT ref.parent, ref.reference_name FROM `tab{B2C_ROW}` ref
            JOIN `tabB2C Payment Disbursement` d ON d.name = ref.parent
            WHERE ref.reference_doctype = 'Salary Slip' AND ref.reference_name IN %(slips)s AND d.docstatus = 1
              AND COALESCE(ref.b2c_disbursement_request, '') = ''
              AND COALESCE(ref.payment_status, '') NOT IN ('Failed', 'Paid')
            """,
            {"slips": tuple(slips)},
        )]
        found += [(f"{r[0]} row {r[1]}", r[2], None) for r in frappe.db.sql(
            f"""
            SELECT parent, idx, reference_name FROM `tab{LINE}`
            WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s AND status IN %(sent)s
            ORDER BY parent, idx
            """,
            {"slips": tuple(slips), "sent": LINE_SENT},
        )]
    if slips and planned:
        # lines still to pay, on a schedule not closed (closing one cancels its planned lines)
        found += [(f"{r[0]} row {r[1]}", r[2], None) for r in frappe.db.sql(
            f"""
            SELECT l.parent, l.idx, l.reference_name FROM `tab{LINE}` l
            JOIN `tab{SCHEDULE}` s ON s.name = l.parent
            WHERE l.parenttype = %(schedule)s AND l.reference_doctype = 'Salary Slip'
              AND l.reference_name IN %(slips)s AND l.status = 'Planned' AND COALESCE(s.closed, 0) = 0
            ORDER BY l.parent, l.idx
            """,
            {"slips": tuple(slips), "schedule": SCHEDULE},
        )]
    return found


# ---------------------------------------------------------------- HRMS's bank entry for a payroll
BANK_VOUCHERS = ("Bank Entry", "Cash Entry")
# The Journal Entry Account fields M-Pesa tags its own salary entries with (salary_entries, journal_service)
MPESA_TAGS = ("b2c_payment_disbursement", "mpesa_b2b_payment_request")


def guard_bank_entry(doc, method=None) -> None:
    """Journal Entry validate: no bank or cash entry for a Payroll Entry (HRMS's Make Bank Entry, or
    one made by hand) while M-Pesa pays salaries of it: payouts Paid (booked or not) or on their
    way, and schedule lines still planned. HRMS's entry pays the net pay of every slip of the
    payroll and knows nothing of M-Pesa; the suite's own salary entry, which hides HRMS's button,
    exists only once a payout is Paid and booked. The other direction is owed.slip_problems: no
    M-Pesa salary while the payroll has such an entry.

    So a payroll is paid either through M-Pesa or through Make Bank Entry (or split into separate
    Payroll Entries). When Payroll Settings books payroll per employee, the entry's payable rows
    name each employee: only the rows of employees M-Pesa pays or paid are refused, so a bank entry
    made by hand for the others goes through. M-Pesa's own entries (a row tagged with its payout)
    are left alone."""
    if doc.get("voucher_type") not in BANK_VOUCHERS:
        return
    accounts = doc.get("accounts") or []
    rows = [r for r in accounts if r.get("reference_type") == PAYROLL_ENTRY and cstr(r.get("reference_name")).strip()]
    if not rows or any(cstr(r.get(tag)).strip() for r in accounts for tag in MPESA_TAGS):
        return
    if not owed.hrms_installed():
        return
    for payroll_entry in sorted({cstr(r.get("reference_name")).strip() for r in rows}):
        mine = [r for r in rows if cstr(r.get("reference_name")).strip() == payroll_entry]
        per_employee = all(r.get("party_type") == "Employee" and cstr(r.get("party")).strip() for r in mine)
        employees = {cstr(r.get("party")).strip() for r in mine} if per_employee else None
        paying = mpesa_salaries(payroll_entry, employees)
        if paying:
            frappe.throw(_bank_entry_refusal(payroll_entry, paying, per_employee), title="M-Pesa pays this payroll")


def mpesa_salaries(payroll_entry: str, employees=None) -> dict:
    """{employee: [payouts and planned lines]} of the Payroll Entry's salaries M-Pesa pays: Paid or
    on their way, or planned on a schedule. ``employees``: only theirs (None: every employee)."""
    filters = {"payroll_entry": payroll_entry}
    if employees is not None:
        if not employees:
            return {}
        filters["employee"] = ["in", sorted(employees)]
    employee_of = {s.name: s.employee for s in frappe.get_all(owed.SLIP, filters=filters, fields=["name", "employee"])}
    found = {}
    for label, slip, employee in _paying(sorted(employee_of), payroll_entry, planned=True):
        employee = cstr(employee or employee_of.get(slip)).strip()
        if employees is not None and employee not in employees:
            continue
        labels = found.setdefault(employee, [])
        if label not in labels:
            labels.append(label)
    return found


def _bank_entry_refusal(payroll_entry: str, paying: dict, per_employee: bool) -> str:
    """The refusal; the payouts are named only to who may see salaries."""
    labels = list(dict.fromkeys(label for names in paying.values() for label in names))
    many = len(labels) > 1
    count = f"{len(labels)} M-Pesa payout{'s' if many else ''} or planned line{'s' if many else ''}"
    if salary_access.can_see_salaries():
        count += ": " + ", ".join(labels[:10]) + (f" and {len(labels) - 10} more" if len(labels) > 10 else "")
    if per_employee:
        employees = sorted(e for e in paying if e)
        more = f" and {len(employees) - 10} more" if len(employees) > 10 else ""
        return (f"M-Pesa pays the salaries of {', '.join(employees[:10])}{more} on {payroll_entry} ({count}). Leave "
                "them out of this bank entry, or it would pay them twice.")
    return (f"M-Pesa pays salaries of {payroll_entry} ({count}). A bank or cash entry for the whole payroll would pay "
            "them twice: pay a payroll either through M-Pesa or through Make Bank Entry, or split it into separate "
            "Payroll Entries. (When Payroll Settings books payroll per employee, a bank entry made by hand may pay "
            "the employees M-Pesa does not.)")


def _schedules(payroll_entry: str) -> list:
    """Payment schedules whose lines (planned or cancelled) name the Payroll Entry's slips: HRMS
    could not delete those slips."""
    slips = _slip_names(payroll_entry)
    found = []
    if frappe.get_meta(SCHEDULE).has_field("payroll_entry"):
        found += frappe.get_all(SCHEDULE, filters={"payroll_entry": payroll_entry}, pluck="name", order_by="name asc")
    if slips:
        found += [r[0] for r in frappe.db.sql(
            f"""SELECT DISTINCT parent FROM `tab{LINE}`
            WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s ORDER BY parent""",
            {"slips": tuple(slips)},
        )]
    return sorted(set(found))


# ---------------------------------------------------------------- the forms' Connections
def get_dashboard_data(data=None):
    """Payroll Entry Connections: its M-Pesa payment schedule, B2B salary requests and B2C salary
    payouts (their rows' payroll_entry). Salary data: no counts for who may not see salaries."""
    from .connections import B2C, add_group

    return add_group(
        PAYROLL_ENTRY, data,
        links={SCHEDULE: "payroll_entry", B2B: "payroll_entry", B2C: "payroll_entry"},
        dynamic={"payroll_entry": "payroll_doctype"}, fieldname="payroll_entry",
    )


def get_employee_dashboard_data(data=None):
    """Employee Connections: the employee's M-Pesa accounts, B2B and B2C payments and payment
    schedule lines. Salary data: no counts for who may not see salaries."""
    from .connections import B2C, add_group

    return add_group(
        "Employee", data,
        links={EMPLOYEE_ACCOUNT: "employee", B2B: "employee", B2C_REQUEST: "party", B2C: "party", SCHEDULE: "party"},
        dynamic={"party": "party_type"}, fieldname="employee",
    )


@frappe.whitelist()
def payroll_status(payroll_entry: str) -> dict:
    """What the Payroll Entry form's M-Pesa buttons need."""
    frappe.has_permission(PAYROLL_ENTRY, "read", doc=payroll_entry, throw=True)
    return {"schedule": live_schedule(payroll_entry), "bank_entries": bank_entries(payroll_entry),
            "schedules": _schedules(payroll_entry)}


# ---------------------------------------------------------------- Salary Slips: one-off and selected
# Round 4. Who may prepare: require_salary_preparer. Who may read the figures: require_salary_reader.
SALARIES_HIDDEN = ("Not permitted: salaries are shown only to who may read Salary Slips (HR User, HR Manager) and "
                   "to who pays them (M-Pesa Payer, M-Pesa Manager).")
NO_SCHEDULE = ("Not permitted: preparing a salary payment makes an M-Pesa Payment Schedule, which needs create on it "
               "(Accounts Manager, Accounts User or M-Pesa Manager).")
NOTHING_LEFT = "nothing is left to pay"  # _line_for's reason, explained by _slip_line
MAX_SLIPS = 500  # per Pay Selected Slips
# The schedule's Not Included is a Small Text: a MariaDB text column, at most 65,535 bytes
NOT_INCLUDED_BYTES = 60000
NO_ACCOUNT_READ = ("Not permitted: you may not read {0} {1}. Pay the employee's default account, or ask HR or the "
                   "M-Pesa Manager.")


def require_salary_reader() -> None:
    """Salary data (decision D4): only for who money_out.salary_access lets see salaries (not an
    Accounts role alone, nor System Manager once round 4's role sets apply)."""
    if not salary_access.can_see_salaries():
        frappe.throw(SALARIES_HIDDEN, frappe.PermissionError)


def require_salary_preparer() -> None:
    """Who may prepare a salary payment from Salary Slips: PREPARE_ROLES (so not the M-Pesa Payer
    who pays it, nor System Manager) who may see salaries and make a payment schedule (its
    DocPerms: Accounts Manager, Accounts User, M-Pesa Manager; an HR-only user is refused here,
    unless a site gives HR create on the schedule). The caller checks read on each slip."""
    require(PREPARE_ROLES)
    require_salary_reader()
    if not owed.hrms_installed():
        frappe.throw("Paying a salary needs HRMS.")
    if not frappe.has_permission(SCHEDULE, "create"):
        frappe.throw(NO_SCHEDULE, frappe.PermissionError)


def _may_prepare() -> bool:
    """require_salary_preparer, without throwing: whether the form offers Pay by M-Pesa."""
    user = frappe.session.user
    return ((user == "Administrator" or not set(PREPARE_ROLES).isdisjoint(frappe.get_roles()))
            and salary_access.can_see_salaries() and owed.hrms_installed()
            and bool(frappe.has_permission(SCHEDULE, "create")))


def _pay_on(planned_date):
    day = getdate(planned_date) if planned_date else getdate(nowdate())
    if day < getdate(nowdate()):
        frappe.throw("Pay On cannot be before today.")
    return day


def _who(slip) -> str:
    return f"{slip.employee} {slip.get('employee_name') or ''} ({slip.name})".replace("  ", " ")


def _payroll_of(slip, payrolls: dict) -> tuple:
    """(the slip's submitted Payroll Entry, None), or (None, why not). ``payrolls`` caches them."""
    name = cstr(slip.get("payroll_entry")).strip()
    if not name:
        return None, ("it is not part of a Payroll Entry, so it has no payroll payable to clear (for an off-cycle "
                      "salary, run a Payroll Entry for this employee)")
    if name not in payrolls:
        payrolls[name] = frappe.db.get_value(PAYROLL_ENTRY, name, ["name", "docstatus", "company", "start_date",
                                                                   "end_date"], as_dict=True)
    payroll = payrolls[name]
    if not payroll:
        return None, f"its Payroll Entry {name} was not found"
    if cint(payroll.docstatus) != 1:
        return None, f"its Payroll Entry {name} is not submitted"
    return payroll, None


def _slip_line(header, slip, day, payrolls: dict, account=None, partly_planned: bool = True) -> tuple:
    """(line, None, None), or (None, why not, the breakdown behind it or None), for a Salary Slip
    paid from its own schedule: submitted, in a submitted Payroll Entry, then the Payroll Entry
    builder's rules (_line_for) with ``account`` or the employee's default. What is left is read
    once, by a locking read under the slip's lock: a schedule or payout another worker committed
    since this request began is counted too. A slip planned on another schedule names it: refused
    when nothing is left, and at all when ``partly_planned`` is False (Pay Selected Slips)."""
    if cint(slip.docstatus) != 1:
        return None, "it is cancelled" if cint(slip.docstatus) == 2 else "it is not submitted", None
    payroll, reason = _payroll_of(slip, payrolls)
    if reason:
        return None, reason, None
    result = owed.slip_left_to_pay(slip.name, for_update=True)
    line, reason = _line_for(header, payroll, slip, day, account, left=result.left)
    planned = sorted({p.name for p in result.parts if p.kind == "planned"})
    if planned and (reason == NOTHING_LEFT or (not reason and not partly_planned)):
        return None, f"already planned on M-Pesa Payment Schedule {', '.join(planned)}", owed.explain(slip.name, result)
    if reason == NOTHING_LEFT:
        return None, reason, owed.explain(slip.name, result)
    return line, reason, None


@contextlib.contextmanager
def _locks(payroll_entries, slips):
    """The payroll's schedule lock, then each slip's own lock (the one its payouts take: R4), in
    name order; held until commit. All three builders take them."""
    with contextlib.ExitStack() as stack:
        for name in sorted(set(payroll_entries) - {None, ""}):
            stack.enter_context(money_in_common.named_lock(
                f"payroll-schedule:{name}", busy=f"A schedule for {name} is being made. Try again in a minute."))
        for name in sorted(set(slips)):
            stack.enter_context(owed.slip_lock(name))
        yield


@contextlib.contextmanager
def _bank_entries_once():
    """While a schedule is built, each Payroll Entry's bank or cash entries are read once
    (owed.payroll_bank_entries), not again for every slip and every check of it: the request reads
    one snapshot of the database anyway. Paying a line reads them again (owed.slip_problems)."""
    previous = getattr(frappe.local, "mpesa_bank_entries", None)
    frappe.local.mpesa_bank_entries = {} if previous is None else previous
    try:
        yield
    finally:
        frappe.local.mpesa_bank_entries = previous


def _not_included(excluded) -> str | None:
    """The schedule's Not Included: a line per salary left out, cut to fit its column when a large
    selection leaves very many out (the answer to the browser lists them all)."""
    kept, size = [], 0
    for i, entry in enumerate(excluded):
        size += len(entry.encode()) + 1
        if size > NOT_INCLUDED_BYTES:
            kept.append(f"... and {len(excluded) - i} more (listed when the schedule was made)")
            break
        kept.append(entry)
    return "\n".join(kept) or None


def _insert_schedule(title, company, b2b_settings, b2c_settings, lines, excluded, payroll_entry=None):
    """A purpose Payroll schedule of ``lines`` (only who may see salaries sees it), naming the
    Payroll Entry only when it is that payroll's own schedule."""
    doc = frappe.new_doc(SCHEDULE)
    doc.update({
        "title": title[:140],
        "company": company,
        "b2b_settings": b2b_settings or None,
        "b2c_settings": b2c_settings or None,
        "purpose": "Payroll",
        "payroll_doctype": PAYROLL_ENTRY if payroll_entry else None,
        "payroll_entry": payroll_entry or None,
        "excluded": _not_included(excluded),
    })
    for line in lines:
        doc.append("lines", line)
    doc.flags.mpesa_from_payroll = True  # keeps purpose, payroll and Not Included (schedule._keep_payroll_fields)
    doc.insert()
    return doc


@frappe.whitelist(methods=["POST"])
def from_salary_slip(slip, payee_account=None, b2b_settings=None, b2c_settings=None, planned_date=None) -> dict:
    """Salary Slip, Pay by M-Pesa: a one-off or off-cycle salary as a ONE-line payment schedule to
    ``payee_account``, any of the employee's approved accounts (the default when none is given): a
    phone by B2C from ``b2c_settings``, a bank account, paybill or till by B2B from
    ``b2b_settings``, for what the slip has left in whole shillings.

    Double pay: a line planned elsewhere (the Payroll Entry's schedule) for the whole salary leaves
    nothing, so this is refused with that schedule's name; for part of it, this plans the rest. The
    Payroll Entry's builder leaves the slip out in turn ("nothing is left to pay") and its cancel is
    refused while this line exists (_schedules). The schedule names no Payroll Entry, so it never
    takes the payroll's one live schedule. Sending stays Pay Selected in M-Pesa Payments Due, by
    someone else (maker-checker). Who may not read Employee M-Pesa Accounts pays the default only."""
    require_salary_preparer()
    slip = cstr(slip).strip()
    values = owed.read_slip(slip) if slip else None
    if not values:
        frappe.throw(f"Salary Slip {slip} not found")
    frappe.has_permission(owed.SLIP, "read", doc=slip, throw=True)
    day = _pay_on(planned_date)
    payee_account = cstr(payee_account).strip() or None
    if payee_account:
        acc = party_accounts.get_account(EMPLOYEE_ACCOUNT, payee_account)
        if not acc or cint(acc.docstatus) != 1 or acc.get("employee") != values.employee:
            frappe.throw(f"{payee_account} is not an approved M-Pesa account of {values.employee}.")
        if payee_account not in _readable_accounts(values.employee) and (
                payee_account != party_accounts.default_account(EMPLOYEE_ACCOUNT, values.employee)):
            frappe.throw(NO_ACCOUNT_READ.format(EMPLOYEE_ACCOUNT, payee_account), frappe.PermissionError)
    header = frappe._dict(name=None, company=values.company, b2b_settings=b2b_settings, b2c_settings=b2c_settings)
    payrolls = {}
    with _locks([values.get("payroll_entry")], [slip]), _bank_entries_once():
        line, reason, detail = _slip_line(header, values, day, payrolls, payee_account)
        if reason:
            frappe.throw(f"{slip} cannot be paid by M-Pesa: {reason}" + (f": {detail}" if detail else ""))
        payroll = payrolls[values.payroll_entry]
        title = f"Salary {slip} {values.get('employee_name') or values.employee} ({_period(payroll)})"
        doc = _insert_schedule(title, values.company, b2b_settings, b2c_settings, [line], [])
    row = doc.lines[0]
    return {"name": doc.name, "amount": flt(row.amount, 2), "route": row.route, "destination": row.destination,
            "payee_account": row.payee_account, "planned_date": cstr(day)}


@frappe.whitelist(methods=["POST"])
def from_salary_slips(slips, b2b_settings=None, b2c_settings=None, planned_date=None) -> dict:
    """Salary Slip list, Pay Selected Slips by M-Pesa: ONE payment schedule with a line per slip,
    each to its employee's default approved account of any type (a phone by B2C, a bank account,
    paybill or till by B2B), for what the slip has left in whole shillings. A slip that cannot be
    paid (not readable, not submitted, withheld, no approved default account, nothing left, already
    on another live schedule, for all or part of it...) is listed with the reason, in the answer
    (with what is left, where that is the reason) and under the schedule's Not Included, never
    dropped. The same guards as the Payroll Entry's schedule.

    The schedule names the Payroll Entry only when the slips are all the submitted slips of one
    Payroll Entry that has no live schedule: it is then that payroll's own schedule, as its button
    would make it. Otherwise it names none, so a few slips never take a payroll's one live
    schedule; its cancel is refused either way while lines name its slips (_schedules).

    Speed: the slips, and which of them the user may read, are two queries for the whole
    selection; what each slip has left is read once; each Payroll Entry's bank entries once."""
    from . import schedule

    require_salary_preparer()
    names = schedule._names(slips)
    if not names:
        frappe.throw("Tick the salary slips to pay.")
    if len(names) > MAX_SLIPS:
        frappe.throw(f"Pick at most {MAX_SLIPS} salary slips at a time.")
    day = _pay_on(planned_date)
    rows = {v.name: v for v in _read_slips(names)}
    # the user's read with one list query (role, user permissions, own and shared slips; HRMS and
    # ERPNext add no has_permission hook for Salary Slip), not a full document load per slip
    readable = set(frappe.get_list(owed.SLIP, filters={"name": ["in", names]}, pluck="name", limit_page_length=0)
                   ) if frappe.has_permission(owed.SLIP, "read") else set()
    found, excluded = [], []
    for name in names:
        values = rows.get(name)
        if not values:
            excluded.append(f"{name}: not found")
        elif name not in readable:
            excluded.append(f"{name}: you may not read it")
        else:
            found.append(values)
    noted = list(excluded)  # Not Included: the reasons without the breakdown of what is left
    companies = sorted({cstr(v.company) for v in found})
    if len(companies) > 1:
        frappe.throw(f"These salary slips are of {', '.join(companies)}: pick the slips of one company.")
    if not found:
        frappe.throw("None of these salary slips can be paid by M-Pesa. " + "; ".join(excluded))
    company = companies[0]
    found.sort(key=lambda v: (cstr(v.employee), v.name))
    payroll_entries = {v.get("payroll_entry") for v in found} - {None, ""}
    header = frappe._dict(name=None, company=company, b2b_settings=b2b_settings, b2c_settings=b2c_settings)
    payrolls, lines = {}, []
    with _locks(payroll_entries, [v.name for v in found]), _bank_entries_once():
        for values in found:
            line, reason, detail = _slip_line(header, values, day, payrolls, partly_planned=False)
            if reason:
                excluded.append(f"{_who(values)}: {reason}" + (f": {detail}" if detail else ""))
                noted.append(f"{_who(values)}: {reason}")
            else:
                lines.append(line)
        if not lines:
            frappe.throw("None of these salary slips can be paid by M-Pesa. " + "; ".join(excluded))
        own = _whole_payroll(names, payroll_entries)
        if own:
            title = f"Salaries {own} ({_period(payrolls[own])})"
        else:
            count = f"{len(lines)} salar{'ies' if len(lines) != 1 else 'y'}"
            title = f"Salaries: {count} of {', '.join(sorted(payroll_entries))}"
        doc = _insert_schedule(title, company, b2b_settings, b2c_settings, lines, noted, payroll_entry=own)
    return {"name": doc.name, "lines": len(lines), "total": flt(sum(flt(line["amount"]) for line in lines), 2),
            "excluded": excluded, "payroll_entry": own}


def _read_slips(names) -> list:
    """The Salary Slips ``names`` (owed.SLIP_READ), in one query."""
    meta = frappe.get_meta(owed.SLIP)
    fields = [f for f in owed.SLIP_READ if f in ("name", "docstatus") or meta.has_field(f)]
    return frappe.get_all(owed.SLIP, filters={"name": ["in", list(names)]}, fields=fields)


def _whole_payroll(names, payroll_entries) -> str | None:
    """The Payroll Entry, when the slips picked are all its submitted slips and it has no live
    schedule (a locking read: under the payroll's schedule lock)."""
    if len(payroll_entries) != 1:
        return None
    (payroll_entry,) = payroll_entries
    if live_schedule(payroll_entry, for_update=True):
        return None
    submitted = set(frappe.get_all(owed.SLIP, filters={"payroll_entry": payroll_entry, "docstatus": 1}, pluck="name"))
    return payroll_entry if submitted and submitted <= set(names) else None


# ---------------------------------------------------------------- the forms' M-Pesa headlines
def _shortcodes(company, api: str) -> list:
    """The shortcodes that pay by ``api`` (use_b2b / use_b2c) for the company, with an initiator."""
    filters = {api: 1}
    if frappe.get_meta("Mpesa Settings").has_field("company"):
        filters["company"] = company
    return [s.name for s in frappe.get_all("Mpesa Settings", filters=filters, fields=["name", "initiator_name"],
                                           order_by="creation asc") if s.initiator_name]


def _readable_accounts(employee) -> set:
    """The employee's approved accounts the user may read (Employee M-Pesa Account's DocPerms: HR
    and M-Pesa Manager), with one list query."""
    if not frappe.has_permission(EMPLOYEE_ACCOUNT, "read"):
        return set()
    return set(frappe.get_list(EMPLOYEE_ACCOUNT, filters={"employee": employee, "docstatus": 1}, pluck="name",
                               limit_page_length=0))


def _masked(destination) -> str:
    """'247247 / ******6789', '2547****0111': an account's number for who may not read the account."""
    shortcode, _, account = cstr(destination).partition(" / ")
    if account:
        return f"{shortcode} / {'*' * max(len(account) - 4, 2)}{account[-4:]}"
    return mask(shortcode) if len(shortcode) > 7 else shortcode  # a phone; a till number is public


def _pay_choices(slip) -> dict:
    """Pay by M-Pesa's choices: the employee's approved accounts of every type (the default
    first), whether each can be paid now, and the shortcodes for each route. Who may not read
    Employee M-Pesa Accounts is offered the default only, its number masked (from_salary_slip
    refuses them any other)."""
    readable = _readable_accounts(slip.employee)
    default = party_accounts.default_account(EMPLOYEE_ACCOUNT, slip.employee)
    accounts = []
    for acc in party_accounts.accounts_of(EMPLOYEE_ACCOUNT, slip.employee):
        route, destination = party_accounts.destination(acc)
        problem = party_accounts.usable_problem(acc, slip.employee, EMPLOYEE_ACCOUNT)
        if acc.name in readable:
            accounts.append({"name": acc.name, "account_type": acc.account_type, "route": route,
                             "destination": destination, "is_default": cint(acc.is_default),
                             "expected_name": acc.get("expected_name"), "problem": problem})
        elif acc.name == default:
            accounts.append({"name": acc.name, "account_type": acc.account_type, "route": route,
                             "destination": _masked(destination), "is_default": cint(acc.is_default),
                             "expected_name": None,
                             "problem": f"{EMPLOYEE_ACCOUNT} {acc.name} cannot be paid now" if problem else None})
    return {"accounts": accounts, "default_account": default,
            "b2b_settings": _shortcodes(slip.company, "use_b2b"), "b2c_settings": _shortcodes(slip.company, "use_b2c")}


@frappe.whitelist()
def salary_slip_status(salary_slip: str) -> dict:
    """The Salary Slip form's headline: what M-Pesa paid of it, what is on its way and planned, and
    what is left (owed.slip_left_to_pay); for who may prepare a salary payment, Pay by M-Pesa's
    choices too. Salary data: for who may see salaries only."""
    from ..mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement import salary_slip_paid

    require_salary_reader()
    frappe.has_permission(owed.SLIP, "read", doc=salary_slip, throw=True)
    values = owed.read_slip(salary_slip)
    if not values:
        frappe.throw(f"Salary Slip {salary_slip} not found")
    result = owed.slip_left_to_pay(salary_slip)

    def total(*kinds):
        return flt(sum(flt(p.amount) for p in result.parts if p.kind in kinds), 2)

    net = flt(values.get("rounded_total")) or flt(values.get("net_pay"))
    paid = flt(salary_slip_paid(salary_slip), 2)  # no employee given: M-Pesa payouts only, not bank entries
    # what the slip's owed leaves out besides M-Pesa: bank or cash entries (a slip not submitted owes nothing)
    other = max(0.0, flt(net - result.owed - paid, 2)) if cint(values.docstatus) == 1 else 0.0
    answer = {
        "net_pay": net, "paid": paid, "other_paid": other,
        "in_flight": total("b2b", "b2c"), "planned": total("planned"), "left": result.left,
        "payable": whole_shillings(result.left), "parts": [p.label for p in result.parts],
        "schedules": sorted({p.name for p in result.parts if p.kind == "planned"}),
        "problems": owed.slip_problems(values), "can_prepare": _may_prepare(),
        "accounts": [], "default_account": None, "b2b_settings": [], "b2c_settings": [],
    }
    if answer["can_prepare"]:
        answer.update(_pay_choices(values))
    return answer


def slip_figures(slips) -> dict:
    """{slip: _dict(net_pay, paid, other_paid, in_flight, planned, left)} for Salary Slip rows
    (name, employee, payroll_entry, rounded_total, net_pay), in a few grouped queries whatever the
    number of slips. Slip by slip the reading of owed.slip_left_to_pay: what M-Pesa paid (Paid B2C
    and B2B payouts, also cancelled ones), bank or cash entries for the Payroll Entry not made by
    M-Pesa, payouts on their way and lines planned on a schedule."""
    slips = [s for s in slips or [] if s.get("name")]
    if not slips:
        return {}
    names = tuple(s.name for s in slips)
    paid, on_way, planned = {}, {}, {}

    def add(target, rows):
        for key, amount in rows:
            target[key] = flt(flt(target.get(key)) + flt(amount), 2)

    add(paid, frappe.db.sql(
        f"""
        SELECT reference_name, SUM(amount) FROM `tab{B2C_REQUEST}`
        WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s
          AND docstatus IN (1, 2) AND status = 'Paid'
        GROUP BY reference_name
        """,
        {"slips": names},
    ))
    if frappe.db.has_column(B2B, "salary_slip"):  # a site with HRMS
        rows = frappe.db.sql(
            f"""
            SELECT salary_slip,
                   SUM(CASE WHEN docstatus IN (1, 2) AND status = 'Paid' THEN amount ELSE 0 END),
                   SUM(CASE WHEN docstatus < 2 AND COALESCE(status, '') IN %(open)s THEN amount ELSE 0 END)
            FROM `tab{B2B}` WHERE salary_slip IN %(slips)s
            GROUP BY salary_slip
            """,
            {"slips": names, "open": ("", *owed.OPEN)},
        )
        add(paid, [(r[0], r[1]) for r in rows])
        add(on_way, [(r[0], r[2]) for r in rows])
    # B2C payouts on their way: open_payouts' rows that are not Paid (owed.b2c_slip_open)
    add(on_way, frappe.db.sql(
        f"""
        SELECT ref.reference_name, SUM(ref.allocated_amount)
        FROM `tab{B2C_ROW}` ref
        JOIN `tabB2C Payment Disbursement` d ON d.name = ref.parent
        LEFT JOIN `tab{B2C_REQUEST}` req ON req.name = ref.b2c_disbursement_request
        WHERE ref.reference_doctype = 'Salary Slip' AND ref.reference_name IN %(slips)s
          AND (d.docstatus = 1 OR (req.name IS NOT NULL AND req.docstatus = 1))
          AND COALESCE(req.status, ref.payment_status, '') != 'Failed' AND COALESCE(req.status, '') != 'Paid'
        GROUP BY ref.reference_name
        """,
        {"slips": names},
    ))
    add(planned, frappe.db.sql(
        f"""
        SELECT reference_name, SUM(amount) FROM `tab{LINE}`
        WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s AND parenttype = %(schedule)s
          AND status = 'Planned' AND COALESCE(request, '') = '' AND COALESCE(b2c_disbursement, '') = ''
        GROUP BY reference_name
        """,
        {"slips": names, "schedule": SCHEDULE},
    ))
    bank = _bank_paid({s.get("payroll_entry") for s in slips} - {None, ""})
    found = {}
    for s in slips:
        net = flt(s.get("rounded_total")) or flt(s.get("net_pay"))
        other = flt(bank.get((s.get("payroll_entry"), s.get("employee")))) if s.get("employee") else 0.0
        owes = max(0.0, flt(net - flt(paid.get(s.name)) - other, 2))
        left = max(0.0, flt(owes - flt(on_way.get(s.name)) - flt(planned.get(s.name)), 2))
        found[s.name] = frappe._dict(net_pay=net, paid=flt(paid.get(s.name)), other_paid=other,
                                     in_flight=flt(on_way.get(s.name)), planned=flt(planned.get(s.name)), left=left)
    return found


def _bank_paid(payroll_entries) -> dict:
    """{(Payroll Entry, employee): what submitted bank or cash entries not made by M-Pesa paid the
    employee against the Payroll Entry} (salary_slip_paid's reading, for many slips at once)."""
    if not payroll_entries:
        return {}
    meta = frappe.get_meta("Journal Entry Account")
    not_mpesa = " ".join(f"AND COALESCE(jea.{field}, '') = ''"
                         for field in ("b2c_payment_disbursement", "mpesa_b2b_payment_request") if meta.has_field(field))
    rows = frappe.db.sql(
        f"""
        SELECT jea.reference_name, jea.party, SUM(jea.debit_in_account_currency)
        FROM `tabJournal Entry Account` jea
        JOIN `tabJournal Entry` je ON je.name = jea.parent
        WHERE je.docstatus = 1 AND je.voucher_type IN ('Bank Entry', 'Cash Entry')
          AND jea.reference_type = 'Payroll Entry' AND jea.reference_name IN %(payrolls)s
          AND jea.party_type = 'Employee' AND jea.debit_in_account_currency > 0
          {not_mpesa}
        GROUP BY jea.reference_name, jea.party
        """,
        {"payrolls": tuple(sorted(payroll_entries))},
    )
    return {(r[0], r[1]): flt(r[2], 2) for r in rows}


@frappe.whitelist()
def payroll_salary_totals(payroll_entry: str) -> dict:
    """The Payroll Entry form's headline: what M-Pesa paid of its submitted salary slips, what is
    on its way and planned, and what is left (slip_figures), and the schedules naming its slips.
    Salary data: for who may see salaries only."""
    require_salary_reader()
    frappe.has_permission(PAYROLL_ENTRY, "read", doc=payroll_entry, throw=True)
    slips = _slips(payroll_entry)
    figures = list(slip_figures(slips).values())

    def total(key):
        return flt(sum(flt(f[key]) for f in figures), 2)

    return {"slips": len(slips), "net_pay": total("net_pay"), "paid": total("paid"), "other_paid": total("other_paid"),
            "in_flight": total("in_flight"), "planned": total("planned"), "left": total("left"),
            "slips_left": sum(1 for f in figures if f.left >= 1), "schedules": _schedules(payroll_entry)}
