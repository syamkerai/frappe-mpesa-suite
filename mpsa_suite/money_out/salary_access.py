"""Who sees salaries on the M-Pesa side: what each employee is paid, to which phone or bank account,
and who was left out of a payroll and why.

HRMS shows Salary Slips to HR and to the employee only. Here the same data sits on payment
schedule lines, on B2B requests to employees and in the M-Pesa Payments Due report, so they are
shown only to who may read Salary Slips (HR Manager, HR User, or any role given read on Salary
Slip, the employee's own role aside), who pays them (PAYOUT_ROLES) and the managers
(ADMIN_ROLES). Everyone else (an M-Pesa Viewer, an Accounts User without an HR role) sees the
supplier payments only:
- a payment schedule for a Payroll Entry, or with any employee or Salary Slip line
  (has_permission, permission_query_conditions);
- a B2B request to an employee (the same hooks);
- the report's employee and salary lines.
"""

import frappe
from frappe.utils import cint, cstr

from ..permissions import ADMIN_ROLES, PAYOUT_ROLES

SCHEDULE = "M-Pesa Payment Schedule"
LINE = "M-Pesa Payment Schedule Line"
B2B = "Mpesa B2B Payment Request"
# Who reads Salary Slips in HRMS, besides the employee (their own, by user permission)
SALARY_ROLES = ("HR Manager", "HR User")
NOT_READERS = ("Employee", "Employee Self Service", "All", "Guest", "Desk User")
# A line that names a salary: an employee paid, or a Salary Slip
SALARY_LINE_SQL = "(COALESCE({t}party_type, '') = 'Employee' OR COALESCE({t}reference_doctype, '') = 'Salary Slip')"


def can_see_salaries(user: str | None = None) -> bool:
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    roles = set(frappe.get_roles(user))
    if roles & {*ADMIN_ROLES, *PAYOUT_ROLES, *SALARY_ROLES}:
        return True
    return _reads_salary_slips(user)


def _reads_salary_slips(user: str) -> bool:
    """A role given read on Salary Slip (Role Permissions Manager), other than the employee's own."""
    try:
        from frappe.permissions import get_valid_perms

        perms = get_valid_perms("Salary Slip", user)
    except Exception:
        return False
    return any(cint(p.get("read")) and not cint(p.get("permlevel")) and not cint(p.get("if_owner"))
               and p.get("role") not in NOT_READERS for p in perms)


def is_salary_line(line) -> bool:
    return line.get("party_type") == "Employee" or line.get("reference_doctype") == "Salary Slip"


def has_salaries(doc) -> bool:
    """A payment schedule that pays salaries: made from a Payroll Entry, or with an employee or
    Salary Slip line (as on the form, or as saved)."""
    if doc.get("purpose") == "Payroll" or cstr(doc.get("payroll_entry")).strip():
        return True
    if any(is_salary_line(line) for line in doc.get("lines") or []):
        return True
    name = cstr(doc.get("name")).strip()
    return bool(name) and bool(frappe.db.sql(
        f"""SELECT 1 FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s AND {SALARY_LINE_SQL.format(t='')}
        LIMIT 1""", (name, SCHEDULE)))


# ---------------------------------------------------------------- hooks
def schedule_has_permission(doc, ptype=None, user=None, debug=False) -> bool:
    """has_permission of M-Pesa Payment Schedule: a schedule that pays salaries is for who may see
    them. Returns True to leave the role permissions as they are (v16 needs True, not None)."""
    if can_see_salaries(user):
        return True
    return not has_salaries(doc)


def schedule_query_conditions(user=None, doctype=None) -> str:
    """permission_query_conditions of M-Pesa Payment Schedule (lists, reports, links)."""
    if can_see_salaries(user):
        return ""
    table = f"`tab{SCHEDULE}`"
    return (f"(COALESCE({table}.purpose, '') != 'Payroll' AND COALESCE({table}.payroll_entry, '') = '' "
            f"AND NOT EXISTS (SELECT 1 FROM `tab{LINE}` sal_line WHERE sal_line.parent = {table}.name "
            f"AND sal_line.parenttype = '{SCHEDULE}' AND {SALARY_LINE_SQL.format(t='sal_line.')}))")


def b2b_has_permission(doc, ptype=None, user=None, debug=False) -> bool:
    """has_permission of Mpesa B2B Payment Request: a payment to an employee is for who may see
    salaries."""
    if not cstr(doc.get("employee")).strip():
        return True
    return can_see_salaries(user)


def b2b_query_conditions(user=None, doctype=None) -> str:
    if can_see_salaries(user):
        return ""
    return f"COALESCE(`tab{B2B}`.employee, '') = ''"


def report_condition(alias: str = "l.") -> str:
    """SQL to add to a query of schedule lines (``alias`` the line table) for who may not see
    salaries, or "" for who may."""
    return "" if can_see_salaries() else f"NOT {SALARY_LINE_SQL.format(t=alias)}"
