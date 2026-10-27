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
its salaries is paid (owed.slip_problems), since HR may press Make Bank Entry after the schedule.

A Payroll Entry cannot be cancelled while M-Pesa salaries of it are paid or on their way: HRMS
cancels every Journal Entry referencing it and deletes its salary slips. Nor can one of its
Salary Slips: an amended slip would show its whole net pay as unpaid again (what was paid is
found by the slip's name).

HRMS is a required app (hooks.required_apps).
"""

import frappe
from frappe.utils import cint, cstr, flt, getdate, nowdate

from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..money_in import common as money_in_common
from ..permissions import PREPARE_ROLES, require
from . import owed, party_accounts

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


def live_schedule(payroll_entry: str) -> str | None:
    """The Payroll Entry's schedule that is not closed, if any."""
    if not frappe.get_meta(SCHEDULE).has_field("payroll_entry"):
        return None
    return frappe.db.get_value(SCHEDULE, {"payroll_entry": payroll_entry, "closed": 0}, "name")


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
    busy = f"A schedule for {payroll_entry} is being made. Try again in a minute."
    with money_in_common.named_lock(f"payroll-schedule:{payroll_entry}", busy=busy):
        live = live_schedule(payroll_entry)
        if live:
            frappe.throw(f"{payroll_entry} already has M-Pesa Payment Schedule {live}. Open it, or close it first.")
        bank = bank_entries(payroll_entry)
        if bank:
            frappe.throw(f"{payroll_entry} already has a bank or cash entry not made by M-Pesa ({', '.join(bank)}). "
                         "Paying it through M-Pesa as well could pay salaries twice: cancel that entry first.")
        header = frappe._dict(name=None, company=payroll.company, b2b_settings=b2b_settings, b2c_settings=b2c_settings)
        lines, excluded = [], []
        for slip in _slips(payroll_entry):
            line, reason = _line_for(header, payroll, slip, day)
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
            "excluded": "\n".join(excluded) or None,
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


def _line_for(header, payroll, slip, day) -> tuple:
    """(line, None), or (None, why this salary is not on the schedule)."""
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
    amount = whole_shillings(owed.slip_left_to_pay(slip.name).left)
    if amount < 1:
        return None, "nothing is left to pay"
    account = party_accounts.default_account(EMPLOYEE_ACCOUNT, slip.employee)
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
    found = []
    if frappe.db.has_column(B2B, "salary_slip") and (slips or payroll_entry):
        named = " OR ".join((["salary_slip IN %(slips)s"] if slips else [])
                            + (["payroll_entry = %(payroll)s"] if payroll_entry else []))
        found += [r[0] for r in frappe.db.sql(
            f"""
            SELECT name FROM `tab{B2B}`
            WHERE ({named})
              AND ((docstatus < 2 AND COALESCE(status, '') IN %(live)s) OR (docstatus IN (1, 2) AND status = 'Paid'))
            ORDER BY creation
            """,
            {"payroll": payroll_entry, "slips": tuple(slips) or ("",), "live": ("", *B2B_LIVE)},
        )]
    if slips:
        found += [r[0] for r in frappe.db.sql(
            f"""
            SELECT name FROM `tab{B2C_REQUEST}`
            WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s
              AND ((docstatus = 1 AND COALESCE(status, '') IN %(live)s) OR (docstatus IN (1, 2) AND status = 'Paid'))
            ORDER BY creation
            """,
            {"slips": tuple(slips), "live": ("", *B2C_LIVE)},
        )]
        # rows of a submitted disbursement whose payout is not made yet
        found += [r[0] for r in frappe.db.sql(
            f"""
            SELECT DISTINCT ref.parent FROM `tab{B2C_ROW}` ref
            JOIN `tabB2C Payment Disbursement` d ON d.name = ref.parent
            WHERE ref.reference_doctype = 'Salary Slip' AND ref.reference_name IN %(slips)s AND d.docstatus = 1
              AND COALESCE(ref.b2c_disbursement_request, '') = ''
              AND COALESCE(ref.payment_status, '') NOT IN ('Failed', 'Paid')
            """,
            {"slips": tuple(slips)},
        )]
        found += [f"{r[0]} row {r[1]}" for r in frappe.db.sql(
            f"""
            SELECT parent, idx FROM `tab{LINE}`
            WHERE reference_doctype = 'Salary Slip' AND reference_name IN %(slips)s AND status IN %(sent)s
            ORDER BY parent, idx
            """,
            {"slips": tuple(slips), "sent": LINE_SENT},
        )]
    return list(dict.fromkeys(found))


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
