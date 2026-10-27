"""Who sees salaries on the M-Pesa side: what each employee is paid, to which phone or bank account,
and who was left out of a payroll and why (the user's decision D4, docs/ROLES_AND_PERMISSIONS.md).

HRMS shows Salary Slips to HR and to the employee only. Here the same data sits on payment
schedules and their lines, on B2C payouts and B2B requests to employees, on their status queries
and in two reports, so they are shown only to who may read Salary Slips (HR Manager, HR User, or
any role given read on Salary Slip, the employee's own role aside) and to who pays or administers
them (``permissions.SALARY_ROLES``), and to whoever created the record. Everyone else (an M-Pesa
Viewer, an Accounts User or Manager without an HR role) sees the supplier payments only:
- ``has_permission`` and ``query_conditions`` for ``DOCTYPES`` (registered in hooks.py): a single
  document, lists, report view, link search, exports, charts;
- ``report_condition`` / ``visible`` for the reports that read these tables directly (M-Pesa
  Payments Due, B2C Disbursement Summary).
Employee M-Pesa Account is closed to the other roles by its DocPerms.

A payout of an Expense Claim or Employee Advance is not a salary (plan C.8): a B2C row or request,
a B2B request or a schedule line that pays an employee for one of them is shown by the DocType's
own rights, to the Accounts roles who can read the claim (or advance): a role of theirs other than
the employee's own has read on that DocType. To anyone else it stays hidden like a salary (who it
paid, how much, to which phone or bank account). Salary Slips, payroll schedules and payments to
an employee that name no document stay hidden.
"""

import frappe
from frappe.utils import cint, cstr

from ..permissions import SALARY_ROLES

SCHEDULE = "M-Pesa Payment Schedule"
LINE = "M-Pesa Payment Schedule Line"
B2B = "Mpesa B2B Payment Request"
B2C = "B2C Payment Disbursement"
B2C_ROW = "B2C Payment Disbursement Reference"
B2C_REQUEST = "B2C Disbursement Request"
STATUS = "Mpesa B2C Status Query"
SLIP = "Salary Slip"
DOCTYPES = (SCHEDULE, B2C, B2C_REQUEST, B2B, STATUS)
NOT_READERS = ("Employee", "Employee Self Service", "All", "Guest", "Desk User")
# What an employee is paid for that is not a salary (they reimburse and advance)
EMPLOYEE_DOCUMENTS = ("Expense Claim", "Employee Advance")
_DOCUMENTS_SQL = ", ".join(f"'{dt}'" for dt in EMPLOYEE_DOCUMENTS)
# A line or row that names a salary: a Salary Slip, or an employee paid for no claim or advance
SALARY_LINE_SQL = (
    "(COALESCE({t}reference_doctype, '') = 'Salary Slip' OR (COALESCE({t}party_type, '') = 'Employee' "
    f"AND COALESCE({{t}}reference_doctype, '') NOT IN ({_DOCUMENTS_SQL})))"
)
# The B2B request's links to a claim or advance (HRMS custom fields, migrate)
B2B_DOCUMENT_FIELDS = ("expense_claim", "employee_advance")

# DocType -> SQL true for a row that is NOT salary data
_NOT_SALARY = {
    SCHEDULE: (
        f"(COALESCE(`tab{SCHEDULE}`.purpose, '') != 'Payroll' AND COALESCE(`tab{SCHEDULE}`.payroll_entry, '') = '' "
        f"AND NOT EXISTS (SELECT 1 FROM `tab{LINE}` sal_line WHERE sal_line.parent = `tab{SCHEDULE}`.name "
        f"AND sal_line.parenttype = '{SCHEDULE}' AND {SALARY_LINE_SQL.format(t='sal_line.')}))"
    ),
    B2C: (
        f"((COALESCE(`tab{B2C}`.party_type, '') != 'Employee' "
        f"OR COALESCE(`tab{B2C}`.transaction_to_pay_against, '') IN ({_DOCUMENTS_SQL})) "
        f"AND NOT EXISTS (SELECT 1 FROM `tab{B2C_ROW}` sal_row "
        f"WHERE sal_row.parent = `tab{B2C}`.name AND sal_row.parenttype = '{B2C}' "
        f"AND {SALARY_LINE_SQL.format(t='sal_row.')}))"
    ),
    B2C_REQUEST: f"NOT {SALARY_LINE_SQL.format(t=f'`tab{B2C_REQUEST}`.')}",
    B2B: f"COALESCE(`tab{B2B}`.employee, '') = ''",  # and the claims and advances (_not_salary)
    STATUS: (
        f"NOT EXISTS (SELECT 1 FROM `tab{B2C_REQUEST}` sal_req WHERE sal_req.name = `tab{STATUS}`.b2c_request "
        f"AND {SALARY_LINE_SQL.format(t='sal_req.')})"
    ),
}


def _not_salary(doctype: str, user: str | None = None) -> str:
    """SQL true for a row of ``doctype`` that is not salary data to ``user``: a B2B request for a
    claim or advance too, where the site has those links; but a payout of a claim or advance the
    user cannot read stays hidden like a salary."""
    condition = _NOT_SALARY[doctype]
    if doctype == B2B:
        links = [f"COALESCE(`tab{B2B}`.{f}, '') != ''" for f in B2B_DOCUMENT_FIELDS if frappe.db.has_column(B2B, f)]
        if links:
            condition = f"({condition} OR {' OR '.join(links)})"
    unread = unread_documents(user)
    if unread:
        condition = f"({condition} AND NOT {_pays_documents_sql(doctype, unread)})"
    return condition


def reads_documents(user: str | None = None) -> tuple:
    """The claim DocTypes (``EMPLOYEE_DOCUMENTS``) the user reads: their payouts are not salaries
    to them."""
    user = user or frappe.session.user
    return tuple(dt for dt in EMPLOYEE_DOCUMENTS if _reads(dt, user))


def unread_documents(user: str | None = None) -> tuple:
    """The claim DocTypes the user cannot read: their payouts stay hidden like salaries."""
    read = reads_documents(user)
    return tuple(dt for dt in EMPLOYEE_DOCUMENTS if dt not in read)


def _pays_documents_sql(doctype: str, documents) -> str:
    """SQL true for a row of ``doctype`` that pays an employee for one of ``documents``."""
    listed = ", ".join(f"'{dt}'" for dt in documents)

    def line(t: str) -> str:
        return f"(COALESCE({t}party_type, '') = 'Employee' AND COALESCE({t}reference_doctype, '') IN ({listed}))"

    if doctype == SCHEDULE:
        return (f"EXISTS (SELECT 1 FROM `tab{LINE}` doc_line WHERE doc_line.parent = `tab{SCHEDULE}`.name "
                f"AND doc_line.parenttype = '{SCHEDULE}' AND {line('doc_line.')})")
    if doctype == B2C:
        return (f"((COALESCE(`tab{B2C}`.party_type, '') = 'Employee' "
                f"AND COALESCE(`tab{B2C}`.transaction_to_pay_against, '') IN ({listed})) "
                f"OR EXISTS (SELECT 1 FROM `tab{B2C_ROW}` doc_row WHERE doc_row.parent = `tab{B2C}`.name "
                f"AND doc_row.parenttype = '{B2C}' AND {line('doc_row.')}))")
    if doctype == B2C_REQUEST:
        return line(f"`tab{B2C_REQUEST}`.")
    if doctype == B2B:
        fields = dict(zip(EMPLOYEE_DOCUMENTS, B2B_DOCUMENT_FIELDS))
        links = [f"COALESCE(`tab{B2B}`.{fields[dt]}, '') != ''" for dt in documents
                 if frappe.db.has_column(B2B, fields[dt])]
        return f"(COALESCE(`tab{B2B}`.employee, '') != '' AND ({' OR '.join(links)}))" if links else "1 = 0"
    return (f"EXISTS (SELECT 1 FROM `tab{B2C_REQUEST}` doc_req WHERE doc_req.name = `tab{STATUS}`.b2c_request "
            f"AND {line('doc_req.')})")


def pays_documents(doc, documents) -> bool:
    """Whether ``doc`` (one of ``DOCTYPES``, as on the form or as saved) pays an employee for one
    of ``documents`` (claim DocTypes)."""
    if not documents:
        return False

    def line(values) -> bool:
        return values.get("party_type") == "Employee" and values.get("reference_doctype") in documents

    if doc.get("doctype") == B2B or (not doc.get("doctype") and "employee" in doc):
        fields = dict(zip(EMPLOYEE_DOCUMENTS, B2B_DOCUMENT_FIELDS))
        return bool(cstr(doc.get("employee")).strip()) and any(cstr(doc.get(fields[dt])).strip() for dt in documents)
    if doc.get("doctype") == B2C_REQUEST:
        return line(doc)
    if doc.get("doctype") == STATUS:
        request = doc.get("b2c_request")
        values = frappe.db.get_value(B2C_REQUEST, request, ["party_type", "reference_doctype"], as_dict=True) \
            if request else None
        return bool(values) and line(values)
    if doc.get("doctype") == B2C:
        header = doc.get("party_type") == "Employee" and doc.get("transaction_to_pay_against") in documents
        return header or any(line(r) for r in doc.get("references") or [])
    if any(line(row) for row in doc.get("lines") or []):
        return True
    name = cstr(doc.get("name")).strip()
    return bool(name) and bool(frappe.db.sql(
        f"""SELECT 1 FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s AND party_type = 'Employee'
        AND reference_doctype IN %s LIMIT 1""", (name, SCHEDULE, tuple(documents))))


def can_see_salaries(user: str | None = None) -> bool:
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    if not set(SALARY_ROLES).isdisjoint(frappe.get_roles(user)):
        return True
    return _reads_salary_slips(user)


def _reads_salary_slips(user: str) -> bool:
    """A role given read on Salary Slip (Role Permissions Manager), other than the employee's own."""
    return _reads(SLIP, user)


def _reads(doctype: str, user: str) -> bool:
    """A role of the user's with read on ``doctype`` (level 0, not only their own records), other
    than the employee's own (HRMS lets every Employee read their own claims and slips)."""
    try:
        from frappe.permissions import get_valid_perms

        perms = get_valid_perms(doctype, user)
    except Exception:
        return False
    return any(cint(p.get("read")) and not cint(p.get("permlevel")) and not cint(p.get("if_owner"))
               and p.get("role") not in NOT_READERS for p in perms)


def is_salary_line(line) -> bool:
    if line.get("reference_doctype") == SLIP:
        return True
    return line.get("party_type") == "Employee" and line.get("reference_doctype") not in EMPLOYEE_DOCUMENTS


def has_salaries(doc) -> bool:
    """Whether ``doc`` (one of ``DOCTYPES``) pays a salary, as on the form or as saved."""
    if doc.get("doctype") == B2B or (not doc.get("doctype") and "employee" in doc):
        return bool(cstr(doc.get("employee")).strip()) and not any(
            cstr(doc.get(f)).strip() for f in B2B_DOCUMENT_FIELDS)
    if doc.get("doctype") == B2C_REQUEST:
        return is_salary_line(doc)
    if doc.get("doctype") == STATUS:
        request = doc.get("b2c_request")
        values = frappe.db.get_value(B2C_REQUEST, request, ["party_type", "reference_doctype"], as_dict=True) \
            if request else None
        return bool(values) and is_salary_line(values)
    if doc.get("doctype") == B2C:
        header = doc.get("party_type") == "Employee" and doc.get("transaction_to_pay_against") not in EMPLOYEE_DOCUMENTS
        return header or any(is_salary_line(r) for r in doc.get("references") or [])
    # a payment schedule: made from a Payroll Entry, or with an employee or Salary Slip line
    if doc.get("purpose") == "Payroll" or cstr(doc.get("payroll_entry")).strip():
        return True
    if any(is_salary_line(line) for line in doc.get("lines") or []):
        return True
    name = cstr(doc.get("name")).strip()
    return bool(name) and bool(frappe.db.sql(
        f"""SELECT 1 FROM `tab{LINE}` WHERE parent = %s AND parenttype = %s AND {SALARY_LINE_SQL.format(t='')}
        LIMIT 1""", (name, SCHEDULE)))


def _own(doc, user: str) -> bool:
    return bool(user) and cstr(doc.get("owner")).lower() == user.lower()


# ---------------------------------------------------------------- hooks
def has_permission(doc, ptype=None, user=None, debug=False) -> bool:
    """has_permission of ``DOCTYPES``: a record that pays a salary is for who may see salaries and
    for its creator. Returns True to leave the role permissions as they are (v16 needs True, not None)."""
    user = user or frappe.session.user
    if can_see_salaries(user) or _own(doc, user):
        return True
    if getattr(doc, "is_new", None) and doc.is_new():
        return True  # being created by this user
    # a payout of a claim or advance the user cannot read is hidden like a salary
    return not has_salaries(doc) and not pays_documents(doc, unread_documents(user))


def query_conditions(user=None, doctype=None) -> str:
    """permission_query_conditions of ``DOCTYPES`` (lists, reports, links, exports, charts)."""
    user = user or frappe.session.user
    if doctype not in _NOT_SALARY or can_see_salaries(user):
        return ""
    return f"({_not_salary(doctype, user)} OR `tab{doctype}`.owner = {frappe.db.escape(user)})"


# The Phase 3 names of the two hooks it started with (kept for its tests and callers)
schedule_has_permission = b2b_has_permission = has_permission


def schedule_query_conditions(user=None, doctype=None) -> str:
    return query_conditions(user, SCHEDULE)


def b2b_query_conditions(user=None, doctype=None) -> str:
    return query_conditions(user, B2B)


# ---------------------------------------------------------------- reports
def report_condition(alias: str = "l.", schedule_alias: str | None = None) -> str:
    """SQL to add to a query of schedule lines (``alias`` the line table, ``schedule_alias`` the
    schedule's, for its creator) for who may not see salaries, or "" for who may."""
    if can_see_salaries():
        return ""
    hidden = f"NOT {SALARY_LINE_SQL.format(t=alias)}"
    unread = unread_documents()
    if unread:  # lines paying a claim or advance the user cannot read
        listed = ", ".join(f"'{dt}'" for dt in unread)
        hidden = (f"({hidden} AND NOT (COALESCE({alias}party_type, '') = 'Employee' "
                  f"AND COALESCE({alias}reference_doctype, '') IN ({listed})))")
    if schedule_alias:
        hidden = f"({hidden} OR {schedule_alias}owner = {frappe.db.escape(frappe.session.user)})"
    return hidden


def visible(doctype: str, names, user=None) -> set:
    """Of ``names``, those the user may see (for queries that skip permissions, e.g. reports)."""
    names = list(names or [])
    user = user or frappe.session.user
    if not names or can_see_salaries(user):
        return set(names)
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name IN %(names)s AND {query_conditions(user, doctype)}",
        {"names": names},
    )
    return {r[0] for r in rows}
