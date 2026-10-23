"""Approved supplier paybill / till numbers for B2B payments.

A B2B payment goes to whatever number is on the request, so a typo or a swapped number
sends money to the wrong business. Destinations therefore live on a Supplier M-Pesa
Account that one person enters (with the supplier's proof) and a different manager
approves by submitting it. Approved details cannot be edited (cancel and amend, which
needs approval again), become usable only after a hold period, and B2B requests copy
the destination from the approved account instead of trusting typed values.
After each payment, the name Safaricom says it paid is compared with the name on file.
"""

import re

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from ..daraja import money_out as daraja
from ..permissions import ADMIN_ROLES, require

ACCOUNT_DOCTYPE = "Supplier M-Pesa Account"
SUITE = "M-Pesa Suite Settings"
PAYBILL = "Paybill"
TILL = "Till (Buy Goods)"
COMMAND_FOR = {PAYBILL: daraja.B2B_PAYBILL, TILL: daraja.B2B_BUY_GOODS}
ACCOUNT_FIELDS = [
    "name",
    "docstatus",
    "supplier",
    "account_type",
    "shortcode",
    "account_number",
    "expected_name",
    "active_from",
    "is_default",
    "name_mismatch",
    "last_receiver_name",
]


# ---------------------------------------------------------------- the account itself
def validate_account(doc) -> None:
    doc.shortcode = str(doc.shortcode or "").strip()
    doc.account_number = str(doc.account_number or "").strip() or None
    if not doc.shortcode.isdigit() or not 5 <= len(doc.shortcode) <= 7:
        frappe.throw("Paybill / Till Number must be 5 to 7 digits")
    if doc.account_type not in COMMAND_FOR:
        frappe.throw(f"Account Type must be {PAYBILL} or {TILL}")
    if doc.account_type == PAYBILL and not doc.account_number:
        frappe.throw("A paybill needs the Account Number the supplier gave you")
    if doc.account_type == TILL:
        doc.account_number = None
    if doc.account_number and len(doc.account_number) > 20:
        frappe.throw("Account Number is longer than M-Pesa allows (20 characters)")

    own = frappe.get_all("Mpesa Settings", pluck="business_shortcode") + frappe.get_all(
        "Mpesa Settings", pluck="till_number"
    )
    if doc.shortcode in {str(s).strip() for s in own if s}:
        frappe.throw(f"{doc.shortcode} is one of this company's own M-Pesa numbers")

    same = {
        "shortcode": doc.shortcode,
        "account_number": doc.account_number or ["is", "not set"],
        "docstatus": 1,
        "name": ["!=", doc.name or ""],
    }
    if frappe.db.exists(ACCOUNT_DOCTYPE, dict(same, supplier=doc.supplier)):
        frappe.throw("This supplier already has an approved account with these details")
    others = frappe.get_all(ACCOUNT_DOCTYPE, filters=dict(same, supplier=["!=", doc.supplier]), pluck="supplier")
    if others:
        # Shared bank paybills (e.g. 522522) differ by account number, so an exact repeat is suspicious.
        frappe.msgprint(
            f"The same number is already approved for {', '.join(sorted(set(others)))}. "
            "Check with the supplier before approving.",
            title="Possible duplicate",
            indicator="orange",
        )


def approve(doc) -> None:
    """Submitting is the approval: a manager who did not enter the details."""
    require(ADMIN_ROLES)
    user = frappe.session.user
    if user != "Administrator" and user == doc.owner:
        frappe.throw("A different person must approve the M-Pesa account they did not enter themselves")
    if not doc.proof:
        frappe.throw("Attach the supplier's proof of these details (letter, invoice or statement)")
    now = now_datetime()
    hold = cint(frappe.db.get_single_value(SUITE, "supplier_account_hold_hours"))
    doc.approved_by = user
    doc.approved_on = now
    doc.active_from = add_to_date(now, hours=hold) if hold > 0 else now


def after_approve(doc) -> None:
    if doc.is_default:
        set_default(doc)


def set_default(doc) -> None:
    for other in frappe.get_all(
        ACCOUNT_DOCTYPE,
        filters={"supplier": doc.supplier, "docstatus": 1, "is_default": 1, "name": ["!=", doc.name]},
        pluck="name",
    ):
        frappe.db.set_value(ACCOUNT_DOCTYPE, other, "is_default", 0)


# ---------------------------------------------------------------- using it on a B2B request
def usable_problem(acc, supplier=None) -> str | None:
    """Why an account cannot be paid right now, or None."""
    if not acc:
        return "Supplier M-Pesa Account not found"
    if acc.docstatus != 1:
        return f"Supplier M-Pesa Account {acc.name} is not approved" + (" (cancelled)" if acc.docstatus == 2 else "")
    if supplier and acc.supplier != supplier:
        return f"Supplier M-Pesa Account {acc.name} belongs to {acc.supplier}, not {supplier}"
    if acc.active_from and get_datetime(acc.active_from) > now_datetime():
        return f"Supplier M-Pesa Account {acc.name} is on hold until {acc.active_from} (new accounts wait before first use)"
    if cint(acc.get("name_mismatch")):
        # A paybill whose holder is not the supplier is how invoice-redirection fraud looks.
        return (
            f"Supplier M-Pesa Account {acc.name} is blocked: M-Pesa last paid {acc.get('last_receiver_name')!r}, "
            f"not {acc.expected_name!r}. A manager must check with the supplier and press Receiver Name "
            "Confirmed on the account before it is paid again."
        )
    return None


def get_account(name):
    return frappe.db.get_value(ACCOUNT_DOCTYPE, name, ACCOUNT_FIELDS, as_dict=True) if name else None


def pick_account(supplier) -> str | None:
    """The supplier's default approved account, or its only one."""
    rows = frappe.get_all(
        ACCOUNT_DOCTYPE,
        filters={"supplier": supplier, "docstatus": 1},
        fields=ACCOUNT_FIELDS,
        order_by="is_default desc, creation asc",
    )
    rows = [r for r in rows if not usable_problem(r, supplier)]
    rows.sort(key=lambda r: 0 if r.is_default else 1)  # stable: keeps creation order otherwise
    if not rows:
        return None
    if rows[0].is_default or len(rows) == 1:
        return rows[0].name
    return None


def apply_destination(doc) -> None:
    """Copy the destination from the approved account; typed numbers are not trusted."""
    if not doc.supplier_mpesa_account and doc.supplier:
        doc.supplier_mpesa_account = pick_account(doc.supplier)
        if not doc.supplier_mpesa_account:
            # Say why the supplier's approved account can't be used (blocked, on hold, ...).
            problems = [
                usable_problem(r, doc.supplier)
                for r in frappe.get_all(ACCOUNT_DOCTYPE, filters={"supplier": doc.supplier, "docstatus": 1},
                                        fields=ACCOUNT_FIELDS, order_by="is_default desc, creation asc")
            ]
            if problems and all(problems):
                frappe.throw(problems[0])

    if doc.supplier_mpesa_account:
        acc = get_account(doc.supplier_mpesa_account)
        problem = usable_problem(acc, doc.supplier)
        if problem:
            frappe.throw(problem)
        doc.supplier = acc.supplier
        doc.command_id = COMMAND_FOR[acc.account_type]
        doc.receiver_shortcode = acc.shortcode
        doc.account_reference = acc.account_number
        return

    if not cint(frappe.db.get_single_value(SUITE, "allow_one_off_b2b")):
        frappe.throw(
            "Pick the supplier's approved M-Pesa account. "
            + (f"{doc.supplier} has none that can be paid yet: " if doc.supplier else "")
            + "add one under Supplier > M-Pesa Accounts and have a manager approve it."
        )
    # One-off destination (switched on in M-Pesa Suite Settings): managers only.
    require(ADMIN_ROLES)


def check_before_send(doc) -> None:
    """The account may have been cancelled between saving the request and sending it."""
    if not doc.supplier_mpesa_account:
        return
    acc = get_account(doc.supplier_mpesa_account)
    problem = usable_problem(acc, doc.supplier)
    if problem:
        frappe.throw(problem)
    if (
        doc.receiver_shortcode != acc.shortcode
        or (doc.account_reference or None) != (acc.account_number or None)
        or doc.command_id != COMMAND_FOR[acc.account_type]
    ):
        frappe.throw(f"Destination on {doc.name} does not match Supplier M-Pesa Account {acc.name}")


# ---------------------------------------------------------------- after payment
def _tokens(name) -> set:
    text = re.sub(r"^\s*\d+\s*-\s*", "", str(name or ""))  # "888880 - KPLC PREPAID"
    words = re.findall(r"[A-Z0-9]+", text.upper())
    return {w for w in words if w not in {"LTD", "LIMITED", "CO", "COMPANY", "THE", "AND", "KENYA", "KE"}}


def names_match(expected, received) -> bool:
    """Loose match: every word of the shorter name appears in the longer one."""
    a, b = _tokens(expected), _tokens(received)
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return small <= big


def record_receiver_name(doc, received_name) -> None:
    """After a Paid result: remember who M-Pesa paid and raise the alarm if it isn't the supplier."""
    if not doc.supplier_mpesa_account or not received_name:
        return
    acc = get_account(doc.supplier_mpesa_account)
    if not acc:
        return
    mismatch = not names_match(acc.expected_name, received_name)
    frappe.db.set_value(
        ACCOUNT_DOCTYPE,
        acc.name,
        {"last_receiver_name": received_name, "last_paid_on": now_datetime(), "name_mismatch": 1 if mismatch else 0},
    )
    if not mismatch:
        return
    doc.db_set("receiver_name_mismatch", 1)
    message = (
        f"M-Pesa paid {received_name!r}, but Supplier M-Pesa Account {acc.name} expects "
        f"{acc.expected_name!r}. Check with the supplier before paying this account again."
    )
    doc.add_comment("Comment", message)
    frappe.log_error(title=f"M-Pesa B2B receiver name mismatch {doc.name}", message=message)
    _alert(message, doc)


def confirm_receiver_name(doc, adopt_name: bool = False) -> None:
    """A manager checked with the supplier: unblock the account, optionally taking M-Pesa's name."""
    require(ADMIN_ROLES)
    if not cint(doc.get("name_mismatch")):
        return
    values = {"name_mismatch": 0}
    note = f"Receiver name {doc.get('last_receiver_name')!r} confirmed with the supplier by {frappe.session.user}"
    if adopt_name and doc.get("last_receiver_name"):
        values["expected_name"] = re.sub(r"^\s*\d+\s*-\s*", "", doc.last_receiver_name).strip()
        note += f"; expected name changed from {doc.expected_name!r} to {values['expected_name']!r}"
    frappe.db.set_value(ACCOUNT_DOCTYPE, doc.name, values)
    doc.add_comment("Comment", note + ".")


def _alert(message, doc) -> None:
    try:
        from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

        role = frappe.db.get_single_value(SUITE, "notify_role") or "Accounts Manager"
        users = frappe.get_all(
            "Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent", distinct=True
        )
        users = [u for u in users if u not in ("Administrator", "Guest")]
        if users:
            enqueue_create_notification(
                users,
                {
                    "type": "Alert",
                    "subject": message,
                    "document_type": doc.doctype,
                    "document_name": doc.name,
                },
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "M-Pesa B2B mismatch alert failed")
