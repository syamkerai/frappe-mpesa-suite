"""Approved employee M-Pesa destinations: a phone (B2C), or a bank account (through the bank's
paybill, with the employee's account number), a paybill or a till (B2B). The approval and checks
are money_out.party_accounts'; this module applies them to payouts.

A B2B request pays a bank, paybill or till account, never a phone. A phone account is paid by
B2C, from a payment schedule line (B2C Payment Disbursement), with the row's number locked to
the approved phone.

Nobody pays themselves: a payment to an employee is refused for the employee's own user (their
User ID on the Employee), on a B2B request and at Pay Selected. A payment to an employee with no
Salary Slip has no amount it is checked against, so a manager (ADMIN_ROLES) sends it.
"""

import frappe
from frappe.utils import cstr

from ..permissions import ADMIN_ROLES
from ..utils.phone import FORMATS, digits, normalize_msisdn, shown
from . import party_accounts as pa

ACCOUNT_DOCTYPE = pa.EMPLOYEE_ACCOUNT
PHONE, BANK = pa.PHONE, pa.BANK
B2B_TYPES = pa.B2B_TYPES
NO_SLIP = "A payment to an employee with no Salary Slip is paid by an M-Pesa Manager."


def check_cell_number(doc, method=None) -> None:
    """Employee validate (hooks.py doc_events): a Cell Number typed or changed that reads as a Kenyan
    number (0..., 254..., or 7... / 1... of up to 10 digits) but is no Kenyan mobile number is
    refused. Pay employee (phone), HR's SMS and the claim payees' 'never an employee's phone' check
    all read it, and a digit lost there (25416... for 0716...) only showed later. Another country's
    number, and an Employee saved without changing it, are left alone."""
    value = cstr(doc.get("cell_number")).strip()
    if not value or not (doc.is_new() or doc.has_value_changed("cell_number")) or normalize_msisdn(value):
        return
    num = digits(value)
    kenyan = (num.startswith("254") or (num.startswith("0") and not num.startswith("00"))
              or (num[:1] in ("7", "1") and len(num) <= 10))
    if not kenyan or (value.startswith("+") and not num.startswith("254")):
        return
    frappe.throw(f"Cell Number {shown(value)} is not a Kenyan mobile number. Enter {FORMATS}. To write it with 254, "
                 "drop only the leading 0: 0111 234 567 is 254111234567.", title="Check the Cell Number")


def own_payment_problem(employee, user=None) -> str | None:
    """Why ``user`` may not pay ``employee``: it is their own salary or payment."""
    user = user or frappe.session.user
    if not employee:
        return None
    if user == pa.own_user(ACCOUNT_DOCTYPE, employee):
        return f"You are {employee}: another person must pay you."
    return None


def is_manager(user=None) -> bool:
    user = user or frappe.session.user
    return bool(set(ADMIN_ROLES) & set(frappe.get_roles(user)))


def usable_problem(acc, employee=None) -> str | None:
    return pa.usable_problem(acc, employee, ACCOUNT_DOCTYPE)


def get_account(name):
    return pa.get_account(ACCOUNT_DOCTYPE, name)


def _b2b_problem(acc) -> str | None:
    if acc.account_type not in B2B_TYPES:
        return (f"{acc.name} is a phone account: B2B pays a bank, paybill or till account. Pay a phone from a "
                "payment schedule (B2C).")
    return None


def apply_destination(doc) -> None:
    """A B2B request to an employee: the destination is copied from the employee's approved bank,
    paybill or till account (the default one when none is picked, or the only one of them);
    typed numbers are not trusted, and an employee is never paid to a number not saved on an
    account."""
    if not doc.employee_mpesa_account:
        doc.employee_mpesa_account = pa.pick_account(ACCOUNT_DOCTYPE, doc.employee, B2B_TYPES)
        if not doc.employee_mpesa_account:
            problem = pa.pick_problem(ACCOUNT_DOCTYPE, doc.employee, B2B_TYPES)
            if problem:
                frappe.throw(problem)
            frappe.throw(
                f"Pick the employee's approved bank, paybill or till account. {doc.employee} has none that can be "
                f"paid by B2B yet: {pa.PROFILES[ACCOUNT_DOCTYPE].add_hint}"
            )
    acc = get_account(doc.employee_mpesa_account)
    problem = usable_problem(acc, doc.employee) or _b2b_problem(acc)
    if problem:
        frappe.throw(problem)
    doc.employee = acc.employee
    doc.command_id = pa.COMMAND_FOR[acc.account_type]
    doc.receiver_shortcode = acc.shortcode
    doc.account_reference = acc.account_number


def check_before_send(doc) -> None:
    """The account may have been cancelled or blocked since the request was saved, or the request's
    destination changed: refused before the money leaves."""
    if not doc.get("employee_mpesa_account"):
        frappe.throw("Pick the employee's approved bank, paybill or till account")
    acc = get_account(doc.employee_mpesa_account)
    problem = usable_problem(acc, doc.employee) or _b2b_problem(acc)
    if problem:
        frappe.throw(problem)
    if (
        doc.receiver_shortcode != acc.shortcode
        or (doc.account_reference or None) != (acc.account_number or None)
        or doc.command_id != pa.COMMAND_FOR[acc.account_type]
    ):
        frappe.throw(f"Destination on {doc.name} does not match Employee M-Pesa Account {acc.name}")


def phone_problem(account, employee, phone) -> str | None:
    """Why a B2C payout to ``phone`` may not use the employee's approved phone ``account`` now."""
    acc = get_account(account)
    problem = usable_problem(acc, employee)
    if problem:
        return problem
    if acc.account_type != PHONE:
        return f"{acc.name} is a bank, paybill or till account: it is paid by B2B, not to a phone."
    if cstr(phone).strip() != cstr(acc.phone).strip():
        return f"The phone number does not match Employee M-Pesa Account {acc.name}."
    return None


def record_receiver_name(payout, account, received_name) -> bool | None:
    """After a Paid result to an employee's account: check the number and name M-Pesa paid (a
    mismatch blocks the account until a manager confirms it)."""
    return pa.record_receiver_name(ACCOUNT_DOCTYPE, account, received_name, payout)
