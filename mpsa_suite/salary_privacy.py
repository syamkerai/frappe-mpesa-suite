"""Who sees salary payouts (the user's decision D4, 2026-10-23).

An employee's net pay, phone and bank account show on payroll schedules and their lines, on B2C
payouts and B2B requests that pay an employee, on their status queries and on Employee M-Pesa
Accounts. HRMS lets only HR User and HR Manager read Salary Slips; the suite lets the same people
see these records, plus those who send or administer payouts (``SALARY_ROLES``) and whoever
created the record. Accounts User, Accounts Manager and M-Pesa Viewer see supplier payouts only.

Employee is not among them: HRMS gives it Salary Slip read restricted by User Permission to the
employee's own slip, which a role check cannot tell apart.

Enforced on every read path: ``has_permission`` (a document) and ``query_condition`` (lists,
report view, link search, exports, charts) are registered in hooks.py for ``CONDITIONS``; the two
Script Reports that read these tables directly filter with ``visible`` / ``can_see_salary``.
Employee M-Pesa Account is closed to the other roles by its DocPerms instead.
"""

import frappe

from .permissions import SALARY_ROLES

SLIP = "Salary Slip"

# DocType -> SQL condition that is true for a row that is NOT salary data.
CONDITIONS = {
    "M-Pesa Payment Schedule": (
        "IFNULL(`tabM-Pesa Payment Schedule`.purpose, '') != 'Payroll' AND NOT EXISTS ("
        "SELECT 1 FROM `tabM-Pesa Payment Schedule Line` l WHERE l.parent = `tabM-Pesa Payment Schedule`.name "
        "AND l.parenttype = 'M-Pesa Payment Schedule' "
        "AND (l.party_type = 'Employee' OR l.reference_doctype = 'Salary Slip'))"
    ),
    "B2C Payment Disbursement": (
        "IFNULL(`tabB2C Payment Disbursement`.party_type, '') != 'Employee' AND NOT EXISTS ("
        "SELECT 1 FROM `tabB2C Payment Disbursement Reference` r WHERE r.parent = `tabB2C Payment Disbursement`.name "
        "AND r.parenttype = 'B2C Payment Disbursement' "
        "AND (r.party_type = 'Employee' OR r.reference_doctype = 'Salary Slip'))"
    ),
    "B2C Disbursement Request": (
        "IFNULL(`tabB2C Disbursement Request`.party_type, '') != 'Employee' "
        "AND IFNULL(`tabB2C Disbursement Request`.reference_doctype, '') != 'Salary Slip'"
    ),
    "Mpesa B2B Payment Request": "IFNULL(`tabMpesa B2B Payment Request`.employee, '') = ''",
    "Mpesa B2C Status Query": (
        "NOT EXISTS (SELECT 1 FROM `tabB2C Disbursement Request` b WHERE b.name = `tabMpesa B2C Status Query`.b2c_request "
        "AND (b.party_type = 'Employee' OR b.reference_doctype = 'Salary Slip'))"
    ),
}


def can_see_salary(user: str | None = None) -> bool:
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    return not set(SALARY_ROLES).isdisjoint(frappe.get_roles(user))


def query_condition(user=None, doctype=None) -> str:
    """permission_query_conditions: hide salary rows from everyone else, but not their own."""
    user = user or frappe.session.user
    if doctype not in CONDITIONS or can_see_salary(user):
        return ""
    return f"(({CONDITIONS[doctype]}) OR `tab{doctype}`.owner = {frappe.db.escape(user)})"


def is_salary(doc) -> bool:
    if doc.doctype == "M-Pesa Payment Schedule":
        return doc.get("purpose") == "Payroll" or any(
            r.get("party_type") == "Employee" or r.get("reference_doctype") == SLIP for r in doc.get("lines") or [])
    if doc.doctype == "B2C Payment Disbursement":
        return doc.get("party_type") == "Employee" or any(
            r.get("party_type") == "Employee" or r.get("reference_doctype") == SLIP for r in doc.get("references") or [])
    if doc.doctype == "B2C Disbursement Request":
        return doc.get("party_type") == "Employee" or doc.get("reference_doctype") == SLIP
    if doc.doctype == "Mpesa B2B Payment Request":
        return bool(doc.get("employee"))
    if doc.doctype == "Mpesa B2C Status Query":
        request = doc.get("b2c_request")
        values = frappe.db.get_value("B2C Disbursement Request", request, ["party_type", "reference_doctype"],
                                     as_dict=True) if request else None
        return bool(values) and (values.party_type == "Employee" or values.reference_doctype == SLIP)
    return False


def has_permission(doc, ptype=None, user=None, debug=False) -> bool:
    """has_permission hook. True means "no objection" (Frappe 16 reads None as a refusal); role
    permissions still decide everything else."""
    user = user or frappe.session.user
    if can_see_salary(user) or (doc.get("owner") or "").lower() == user.lower():
        return True
    if getattr(doc, "is_new", None) and doc.is_new():
        return True  # being created by this user
    return not is_salary(doc)


def visible(doctype: str, names, user=None) -> set:
    """Of ``names``, those the user may see (for queries that skip permissions, e.g. reports)."""
    names = list(names or [])
    user = user or frappe.session.user
    if not names or can_see_salary(user):
        return set(names)
    rows = frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name IN %(names)s AND {query_condition(user, doctype)}",
        {"names": names},
    )
    return {r[0] for r in rows}
