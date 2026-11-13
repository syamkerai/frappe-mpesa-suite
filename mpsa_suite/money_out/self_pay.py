"""Nobody pays themselves: a user's own phones and names, and whether a payout's rows pay them.

Round 4.5's payouts to people no ERPNext party names (a bulk payout sheet's casuals and overtime,
a petty cash phone's top-ups) have no employee to compare with the sender, as
employee_accounts.own_payment_problem does. They are compared by what the row pays instead:
- the phone: the User's Mobile No and Phone, the Cell Number of each Employee whose User ID they
  are, and the phones of those employees' Employee M-Pesa Accounts (approved or not, as
  claim_payees.employee_destinations reads them), all as 2547... (utils.phone);
- the name: the User's full name and those employees' names (claim_payees.own_names), matched with
  the name the row gives and the name M-Pesa paid its phone under before, as the claim payees'
  staff check does (claim_payees.staff_matches: two words at least on both sides, so one first
  name alone matches no one).

The phones are read for this user only, not from employee_destinations: that keeps one owner per
phone, so a phone two people share would be found for one of them only.

Used by money_out.partyless at the send and a retry; the bulk payout sheet calls it for who
prepares, approves and sends it, the Petty Cash Phone for its holder.
"""

import frappe
from frappe.utils import cstr

from ..utils import privacy
from ..utils.phone import normalize_msisdn

EMPLOYEE_ACCOUNT = "Employee M-Pesa Account"
# How many rows a refusal names before it says how many more
LISTED = 5


def employees_of(user: str) -> list:
    """The Employees whose User ID is ``user`` (several when a person was employed twice)."""
    if not user or not frappe.get_meta("Employee").has_field("user_id"):
        return []
    return frappe.get_all("Employee", filters={"user_id": user}, pluck="name")


def own_phones(user: str | None = None) -> set:
    """Every phone of ``user`` (default: the session user), as 2547...: the User's Mobile No and
    Phone, their Employees' Cell Number and the phone of each of those employees' Employee M-Pesa
    Accounts that is not cancelled."""
    user = cstr(user or frappe.session.user).strip()
    if not user or user == "Guest":
        return set()
    numbers = []
    user_meta = frappe.get_meta("User")
    fields = [f for f in ("mobile_no", "phone") if user_meta.has_field(f)]
    if fields:
        values = frappe.db.get_value("User", user, fields, as_dict=True) or {}
        numbers += [values.get(f) for f in fields]
    employees = employees_of(user)
    if employees:
        if frappe.get_meta("Employee").has_field("cell_number"):
            numbers += frappe.get_all("Employee", filters={"name": ["in", employees]}, pluck="cell_number")
        numbers += frappe.get_all(EMPLOYEE_ACCOUNT, filters={"employee": ["in", employees], "docstatus": ["<", 2]},
                                  pluck="phone")
    return {m for m in (normalize_msisdn(n) for n in numbers) if m}


def own_names(user: str | None = None) -> list:
    """The names ``user`` goes by: their User's full name and each of their Employees' name."""
    from .claim_payees import own_names as names

    user = cstr(user or frappe.session.user).strip()
    if not user or user == "Guest":
        return []
    return names(user)


def matches(user: str, rows) -> list:
    """[(label, why)] for each of ``rows`` that pays ``user``: their phone, or their name."""
    from .claim_payees import staff_matches

    phones = own_phones(user)
    people = [("you", name, user) for name in own_names(user)]
    found = []
    for i, row in enumerate(rows or [], 1):
        label = cstr(row.get("label")).strip() or f"Row #{row.get('idx') or i}"
        msisdn = normalize_msisdn(row.get("phone"))
        if msisdn and msisdn in phones:
            found.append((label, f"your phone {privacy.phone(msisdn)}"))
            continue
        for field in ("payee_name", "mpesa_name"):
            name = cstr(row.get(field)).strip()
            if name and people and staff_matches(name, people):
                found.append((label, f"{name!r}, your name"))
                break
    return found


def self_pay_problem(user: str | None, rows, action: str = "send") -> str | None:
    """Why ``user`` may not ``action`` (prepare, approve, send...) a payout of ``rows``: one pays
    their phone, or a name that is theirs. ``rows``: dicts of ``phone``, ``payee_name``, an
    optional ``mpesa_name`` (the name M-Pesa paid the phone under before) and an optional
    ``label`` (else "Row #<idx>"). None when it pays none of theirs."""
    user = cstr(user).strip()
    if not user or user == "Guest" or not rows:
        return None
    found = matches(user, rows)
    if not found:
        return None
    shown = "; ".join(f"{label} pays {why}" for label, why in found[:LISTED])
    if len(found) > LISTED:
        shown += f"; and {len(found) - LISTED} more"
    return f"You may not {action} a payment to yourself: {shown}. Another person must {action} it."
