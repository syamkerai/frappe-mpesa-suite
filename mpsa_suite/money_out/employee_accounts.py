"""Approved employee M-Pesa destinations: a phone (B2C) or a bank paybill plus the employee's
bank account number (B2B). The approval and checks are money_out.party_accounts'; this module
applies them to payouts.

A B2B request pays only a bank paybill account. A phone account is paid by B2C, from a payment
schedule line (B2C Payment Disbursement), with the row's number locked to the approved phone.

Nobody pays themselves: a payment to an employee is refused for the employee's own user (their
User ID on the Employee), on a B2B request and at Pay Selected. A payment to an employee with no
Salary Slip has no amount it is checked against, so a manager (ADMIN_ROLES) sends it.
"""

import frappe
from frappe.utils import cstr

from ..permissions import ADMIN_ROLES
from . import party_accounts as pa

ACCOUNT_DOCTYPE = pa.EMPLOYEE_ACCOUNT
PHONE, BANK = pa.PHONE, pa.BANK
NO_SLIP = "A payment to an employee with no Salary Slip is paid by an M-Pesa or Accounts Manager."


def own_payment_problem(employee, user=None) -> str | None:
    """Why ``user`` may not pay ``employee``: it is their own salary or payment."""
    user = user or frappe.session.user
    if not employee or user == "Administrator":
        return None
    if user == pa.own_user(ACCOUNT_DOCTYPE, employee):
        return f"You are {employee}: another person must pay you."
    return None


def is_manager(user=None) -> bool:
    user = user or frappe.session.user
    return user == "Administrator" or bool(set(ADMIN_ROLES) & set(frappe.get_roles(user)))


def usable_problem(acc, employee=None) -> str | None:
    return pa.usable_problem(acc, employee, ACCOUNT_DOCTYPE)


def get_account(name):
    return pa.get_account(ACCOUNT_DOCTYPE, name)


def apply_destination(doc) -> None:
    """A B2B request to an employee: the destination is copied from the employee's approved bank
    paybill account (the default one when none is picked); typed numbers are not trusted, and
    an employee is never paid to a number not saved on an account."""
    if not doc.employee_mpesa_account:
        doc.employee_mpesa_account = pa.pick_account(ACCOUNT_DOCTYPE, doc.employee, (BANK,))
        if not doc.employee_mpesa_account:
            problems = pa.unusable_reasons(ACCOUNT_DOCTYPE, doc.employee, (BANK,))
            if problems and all(problems):
                frappe.throw(problems[0])
            frappe.throw(
                f"Pick the employee's approved bank paybill account. {doc.employee} has none that can be paid by B2B "
                f"yet: {pa.PROFILES[ACCOUNT_DOCTYPE].add_hint}"
            )
    acc = get_account(doc.employee_mpesa_account)
    problem = usable_problem(acc, doc.employee)
    if problem:
        frappe.throw(problem)
    if acc.account_type != BANK:
        frappe.throw(f"{acc.name} is a phone account: B2B pays bank paybills only. Pay a phone from a payment "
                     "schedule (B2C).")
    doc.employee = acc.employee
    doc.command_id = pa.COMMAND_FOR[BANK]
    doc.receiver_shortcode = acc.shortcode
    doc.account_reference = acc.account_number


def check_before_send(doc) -> None:
    """The account may have been cancelled or blocked since the request was saved, or the request's
    destination changed: refused before the money leaves."""
    if not doc.get("employee_mpesa_account"):
        frappe.throw("Pick the employee's approved bank paybill account")
    acc = get_account(doc.employee_mpesa_account)
    problem = usable_problem(acc, doc.employee)
    if problem:
        frappe.throw(problem)
    if (
        acc.account_type != BANK
        or doc.receiver_shortcode != acc.shortcode
        or (doc.account_reference or None) != (acc.account_number or None)
        or doc.command_id != pa.COMMAND_FOR[BANK]
    ):
        frappe.throw(f"Destination on {doc.name} does not match Employee M-Pesa Account {acc.name}")


def phone_problem(account, employee, phone) -> str | None:
    """Why a B2C payout to ``phone`` may not use the employee's approved phone ``account`` now."""
    acc = get_account(account)
    problem = usable_problem(acc, employee)
    if problem:
        return problem
    if acc.account_type != PHONE:
        return f"{acc.name} is a bank paybill account: it is paid by B2B, not to a phone."
    if cstr(phone).strip() != cstr(acc.phone).strip():
        return f"The phone number does not match Employee M-Pesa Account {acc.name}."
    return None


def record_receiver_name(payout, account, received_name) -> bool | None:
    """After a Paid result to an employee's account: check the name M-Pesa paid (a mismatch
    blocks the account until a manager confirms it)."""
    return pa.record_receiver_name(ACCOUNT_DOCTYPE, account, received_name, payout)
