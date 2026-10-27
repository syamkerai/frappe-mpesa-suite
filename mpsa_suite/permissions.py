"""Role gates for whitelisted M-Pesa actions that talk to Safaricom.

Upstream left these open to any logged-in user (including website/portal users).
Collecting and finance work keep the standard ERPNext roles staff already hold. Sending money
and M-Pesa administration are for the suite's own roles only (the user's decisions D1 and D2,
2026-10-23): ERPNext's Accounts Manager and HR Manager prepare payouts but do not send them.
Only named payers send (decision 1, 2026-10-27): System Manager configures the suite but neither
sends, approves payee accounts nor sees salaries, and the shared Administrator login is refused
for sends and approvals (``require_named_user``).
Who holds what, per DocType and per endpoint: docs/ROLES_AND_PERMISSIONS.md
(kept in step with mpsa_suite.permission_matrix by the unit tests).
"""

import frappe
from frappe.utils import cint

SUITE_SETTINGS = "M-Pesa Suite Settings"

# Send STK prompts / check their status.
COLLECT_ROLES = (
    "System Manager",
    "M-Pesa Manager",
    "M-Pesa Collector",
    "Accounts Manager",
    "Accounts User",
    "Sales Manager",
    "Sales User",
)

# Query balances and transaction status, run Pull Transactions.
FINANCE_ROLES = (
    "System Manager",
    "M-Pesa Manager",
    "Accounts Manager",
    "Accounts User",
)

# Prepare a payout (a draft B2B request or B2C payout, a payment schedule line) from a document
# they can read: Accounts for invoices and orders, HR for claims, advances and salaries, and HRMS's
# Expense Approver for the claims and advances it reads (HRMS lets no other default role but the
# employee read an Employee Advance). Only PAYOUT_ROLES send it.
PREPARE_ROLES = (
    "M-Pesa Manager",
    "Accounts Manager",
    "Accounts User",
    "HR Manager",
    "HR User",
    "Expense Approver",
)

# Send money: submit B2C payouts, B2B payments and reversals, retry them, Pay Selected. Named
# payers only (decision 1, 2026-10-27): not System Manager.
PAYOUT_ROLES = (
    "M-Pesa Manager",
    "M-Pesa Payer",
)

# Approve payee accounts and one-off destinations, change registrations at Safaricom (C2B URLs,
# Pull), settle Unknown payouts, reconcile by hand, the Sandbox Lab. M-Pesa Manager only.
ADMIN_ROLES = (
    "M-Pesa Manager",
)

# See salary payouts (decision D4): those who read Salary Slips in HRMS (HR User, HR Manager) and
# those who send or administer payouts. Employee is not here: it reads only its own slip; nor is
# System Manager, which HRMS does not let read Salary Slips either (decision 1, 2026-10-27).
SALARY_ROLES = (
    "M-Pesa Manager",
    "M-Pesa Payer",
    "HR Manager",
    "HR User",
)

# See and change Daraja secrets and suite settings, approve what only an M-Pesa admin may.
CONFIG_ROLES = (
    "System Manager",
    "M-Pesa Manager",
)

# How Frappe runs a whitelisted method of a document over HTTP (v1 ``cmd``).
DOC_METHOD_COMMANDS = ("run_doc_method", "runserverobj", "frappe.handler.run_doc_method")


def require(roles) -> None:
    """Throw PermissionError unless the session user has at least one of ``roles``.

    Not frappe.only_for: on Frappe 15 that returns early whenever flags.in_test is set, so a
    gate built on it never refused anyone in a bench test.
    """
    if frappe.session.user == "Administrator":
        return
    if set(roles).isdisjoint(frappe.get_roles()):
        frappe.throw(
            frappe._("Not permitted: this needs one of the roles {0}.").format(", ".join(roles)),
            frappe.PermissionError,
        )


def administrator_refused(user: str | None = None) -> bool:
    """True when ``user`` is the shared Administrator login and M-Pesa Suite Settings refuses it
    for sending payouts and approving accounts (on by default; a site not yet migrated reads it as
    on). Installing, migrating and patches are never refused. Bench tests are: they send and
    approve as named users holding the right roles."""
    if (user or frappe.session.user) != "Administrator":
        return False
    if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
        return False
    try:
        value = frappe.get_cached_doc(SUITE_SETTINGS).get("refuse_administrator_payouts")
    except frappe.DoesNotExistError:
        return True
    return True if value in (None, "") else bool(cint(value))


def require_named_user() -> None:
    """Throw PermissionError when the shared Administrator login sends a payout or approves a
    payee account (``administrator_refused``), so every payout and approval names a person.

    Call it at the person's own step (the send, Pay Selected and its confirm, a retry, an
    approval), not in the background jobs the scheduler runs as Administrator.
    """
    if administrator_refused():
        frappe.throw(
            frappe._("Use your own login to send M-Pesa payouts or approve accounts: the shared Administrator login is not allowed (M-Pesa Suite Settings)."),
            frappe.PermissionError,
        )


def is_direct_call(function_name: str) -> bool:
    """True when this request is an HTTP call to the function ``function_name``.

    Lets a function gate direct API calls while still running when reached through a
    permission-checked document flow (e.g. Payment Request submit -> STK push). Every route
    Frappe 15 and 16 offer counts: ``/api/method/<module>.<name>`` (``cmd`` on v1),
    ``/api/v2/method/<DocType>/<name>`` (any name its controller module holds, imported ones
    too), ``/api/v2/document/<DocType>/<name>/method/<name>`` and ``run_doc_method``.
    """
    request = getattr(frappe.local, "request", None)
    if request is None:
        return False
    path = (getattr(request, "path", "") or "").rstrip("/")
    form = frappe.local.form_dict
    cmd = form.get("cmd") or ""
    if path.endswith(("." + function_name, "/" + function_name)) or cmd.endswith("." + function_name):
        return True
    doc_method = cmd in DOC_METHOD_COMMANDS or path.endswith(tuple("/" + c for c in DOC_METHOD_COMMANDS))
    return doc_method and form.get("method") == function_name


def require_for_direct_call(function_name: str, roles) -> None:
    if is_direct_call(function_name):
        require(roles)
