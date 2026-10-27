"""Approved supplier destinations: a paybill, till or bank account for B2B payments, or a phone.

A B2B payment goes to whatever number is on the request, so a typo or a swapped number
sends money to the wrong business. Destinations therefore live on a Supplier M-Pesa
Account that one person enters (with the supplier's proof) and a different manager
approves by submitting it. Approved details cannot be edited (cancel and amend, which
needs approval again), become usable only after a hold period, and B2B requests copy
the destination from the approved account instead of trusting typed values.
After each payment, the name Safaricom says it paid is compared with the name on file.
A supplier's phone account is not paid by B2B: a B2C Payment Disbursement pays it.

The approval, the checks and the name check are shared with Employee M-Pesa Accounts
(money_out.party_accounts); this module keeps the supplier's names for them.
"""

import frappe
from frappe.utils import cint

from ..permissions import ADMIN_ROLES, require
from . import party_accounts as pa

ACCOUNT_DOCTYPE = pa.SUPPLIER_ACCOUNT
SUITE = pa.SUITE
PAYBILL = pa.PAYBILL
TILL = pa.TILL
B2B_TYPES = pa.B2B_TYPES
# A number not saved on a supplier (a one-off, M-Pesa Suite Settings) is a paybill or a till.
COMMAND_FOR = {PAYBILL: pa.COMMAND_FOR[PAYBILL], TILL: pa.COMMAND_FOR[TILL]}
ACCOUNT_FIELDS = list(pa.PROFILES[ACCOUNT_DOCTYPE].fields)
_tokens = pa._tokens
names_match = pa.names_match


# ---------------------------------------------------------------- the account itself
def validate_account(doc) -> None:
    pa.validate_account(doc)


def approve(doc) -> None:
    """Submitting is the approval: a manager who did not enter the details."""
    pa.approve(doc)


def after_approve(doc) -> None:
    pa.after_approve(doc)


def set_default(doc) -> None:
    pa.set_default(doc)


def before_cancel(doc) -> None:
    pa.before_cancel(doc)


def after_cancel(doc) -> None:
    pa.after_cancel(doc)


def refuse_default_edit(doc) -> None:
    pa.refuse_default_edit(doc)


def make_default(doc) -> None:
    """Make an approved account the supplier's default (M-Pesa Manager, Accounts Manager or User,
    Purchase Manager: not System Manager), as a named user."""
    pa.make_default(doc)


def form_state(doc) -> dict:
    return pa.form_state(doc)


def accounts_for(supplier) -> list:
    """Approved and draft accounts of the supplier, with whether each can be paid now."""
    return pa.accounts_for(ACCOUNT_DOCTYPE, supplier)


# ---------------------------------------------------------------- using it on a B2B request
def usable_problem(acc, supplier=None) -> str | None:
    """Why an account cannot be paid right now, or None."""
    return pa.usable_problem(acc, supplier, ACCOUNT_DOCTYPE)


def get_account(name):
    return pa.get_account(ACCOUNT_DOCTYPE, name)


def pick_account(supplier, types=B2B_TYPES) -> str | None:
    """The supplier's default approved account that B2B pays (a paybill, till or bank account), or
    its only one."""
    return pa.pick_account(ACCOUNT_DOCTYPE, supplier, types)


def _b2b_problem(acc) -> str | None:
    if acc.account_type not in B2B_TYPES:
        return (f"{acc.name} is a phone account: B2B pays a paybill, till or bank account. Pay a supplier's phone "
                "with a B2C Payment Disbursement.")
    return None


def apply_destination(doc) -> None:
    """Copy the destination from the approved account; typed numbers are not trusted."""
    if not doc.supplier_mpesa_account and doc.supplier:
        doc.supplier_mpesa_account = pick_account(doc.supplier)
        if not doc.supplier_mpesa_account:
            # Say why the supplier's default (or only) account can't be used (blocked, on hold, ...).
            problem = pa.pick_problem(ACCOUNT_DOCTYPE, doc.supplier, B2B_TYPES)
            if problem:
                frappe.throw(problem)

    if doc.supplier_mpesa_account:
        acc = get_account(doc.supplier_mpesa_account)
        problem = usable_problem(acc, doc.supplier) or _b2b_problem(acc)
        if problem:
            frappe.throw(problem)
        doc.supplier = acc.supplier
        doc.command_id = pa.COMMAND_FOR[acc.account_type]
        doc.receiver_shortcode = acc.shortcode
        doc.account_reference = acc.account_number
        return

    if not cint(frappe.db.get_single_value(SUITE, "allow_one_off_b2b")):
        frappe.throw(
            "Pick the supplier's approved M-Pesa account. "
            + (f"{doc.supplier} has none that can be paid yet: " if doc.supplier else "")
            + pa.PROFILES[ACCOUNT_DOCTYPE].add_hint
        )
    # One-off destination (switched on in M-Pesa Suite Settings): managers only.
    require(ADMIN_ROLES)


def check_before_send(doc) -> None:
    """The account may have been cancelled between saving the request and sending it. A request
    without one is a one-off destination: the switch and a manager are checked again now (R7)."""
    if not doc.supplier_mpesa_account:
        if not cint(frappe.db.get_single_value(SUITE, "allow_one_off_b2b")):
            frappe.throw("Pick the supplier's approved M-Pesa account")
        require(ADMIN_ROLES)
        return
    acc = get_account(doc.supplier_mpesa_account)
    problem = usable_problem(acc, doc.supplier) or _b2b_problem(acc)
    if problem:
        frappe.throw(problem)
    if (
        doc.receiver_shortcode != acc.shortcode
        or (doc.account_reference or None) != (acc.account_number or None)
        or doc.command_id != pa.COMMAND_FOR[acc.account_type]
    ):
        frappe.throw(f"Destination on {doc.name} does not match Supplier M-Pesa Account {acc.name}")


# ---------------------------------------------------------------- after payment
def record_receiver_name(doc, received_name) -> None:
    """After a Paid result: remember who M-Pesa paid and raise the alarm if it isn't the supplier."""
    if not doc.supplier_mpesa_account:
        return
    pa.record_receiver_name(ACCOUNT_DOCTYPE, doc.supplier_mpesa_account, received_name, doc, notify=_alert)


def confirm_receiver_name(doc, adopt_name: bool = False) -> None:
    """A manager checked with the supplier: unblock the account, optionally taking M-Pesa's name."""
    pa.confirm_receiver_name(doc, adopt_name)


def _alert(message, doc) -> None:
    pa.alert(message, doc)
