"""Role gates for whitelisted M-Pesa actions that talk to Safaricom.

Upstream left these open to any logged-in user (including website/portal users).
The lists include the standard ERPNext roles Astral's staff already hold, so
adding the gates does not take access away from anyone who uses them today.
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

# Change registrations at Safaricom (C2B URLs, Pull), rotate secrets, release payouts.
ADMIN_ROLES = (
    "System Manager",
    "M-Pesa Manager",
    "Accounts Manager",
)


def require(roles) -> None:
    """Throw PermissionError unless the session user has at least one of ``roles``."""
    if frappe.session.user == "Administrator":
        return
    frappe.only_for(roles)


def is_direct_call(function_name: str) -> bool:
    """True when this request is an HTTP call to ``…<module>.<function_name>``.

    Lets a function gate direct API calls while still running when reached through a
    permission-checked document flow (e.g. Payment Request submit -> STK push).
    """
    request = getattr(frappe.local, "request", None)
    if request is None:
        return False
    path = (getattr(request, "path", "") or "").rstrip("/")
    cmd = frappe.local.form_dict.get("cmd") or ""
    return path.endswith("." + function_name) or cmd.endswith("." + function_name)


def require_for_direct_call(function_name: str, roles) -> None:
    if is_direct_call(function_name):
        require(roles)
