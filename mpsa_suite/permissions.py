"""Role gates for whitelisted M-Pesa actions that talk to Safaricom.

Upstream left these open to any logged-in user (including website/portal users).
The lists include the standard ERPNext roles Astral's staff already hold, so
adding the gates does not take access away from anyone who uses them today.
Who holds what, per DocType and per endpoint: docs/ROLES_AND_PERMISSIONS.md
(kept in step with mpsa_suite.permission_matrix by the unit tests).
"""

import frappe

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

# Send or retry B2C payouts directly (outside the disbursement document's submit flow).
PAYOUT_ROLES = (
    "System Manager",
    "M-Pesa Manager",
    "M-Pesa Payer",
    "Accounts Manager",
)

# Change registrations at Safaricom (C2B URLs, Pull), rotate secrets, release payouts.
ADMIN_ROLES = (
    "System Manager",
    "M-Pesa Manager",
    "Accounts Manager",
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
