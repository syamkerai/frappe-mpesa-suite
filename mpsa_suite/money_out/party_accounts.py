"""Approved M-Pesa destinations of a party: a supplier's paybill or till, an employee's phone or
bank paybill.

A payout goes to whatever number is on it, so a typo or a swapped number sends money to the
wrong person. Destinations therefore live on an account that one person enters (with proof)
and a different manager approves by submitting it: not who created it, not who last entered or
changed its details (entered_by, set on every save that changes them and checked again at
submit), and for an employee's account not the employee. Approved details cannot be edited
(cancel and amend, which needs approval again), become usable only after a hold period, and
payouts copy the destination from the approved account instead of trusting typed values. One
phone or bank account is approved for one employee only. After each payment, the name
Safaricom says it paid is compared with the name on file; a mismatch blocks the account until
a manager confirms it.

One profile per account DocType (Supplier M-Pesa Account, Employee M-Pesa Account) holds what
differs: the party, its account types, the hold setting and the wording. supplier_accounts and
employee_accounts are thin wrappers over this module.

For an employee's bank paybill the expected name is the bank's public name: Safaricom names the
bank, not the account holder, so it confirms the destination only. Who holds the account rests
on the proof, the second approver and the hold period.
"""

import re

import frappe
from frappe.utils import add_to_date, cint, cstr, get_datetime, now_datetime

from ..daraja import money_out as daraja
from ..permissions import ADMIN_ROLES, require
from ..utils.phone import normalize_msisdn

SUITE = "M-Pesa Suite Settings"
SUPPLIER_ACCOUNT = "Supplier M-Pesa Account"
EMPLOYEE_ACCOUNT = "Employee M-Pesa Account"
# Account types
PAYBILL = "Paybill"
TILL = "Till (Buy Goods)"
PHONE = "Phone (B2C)"
BANK = "Bank Paybill (B2B)"
# The Daraja B2B command of each B2B account type (a phone is paid by B2C)
COMMAND_FOR = {PAYBILL: daraja.B2B_PAYBILL, TILL: daraja.B2B_BUY_GOODS, BANK: daraja.B2B_PAYBILL}
# The payment schedule's route of each account type
B2B_PAYBILL, B2B_TILL, B2C_PHONE = "B2B Paybill", "B2B Till", "B2C Phone"
ROUTE_FOR = {PAYBILL: B2B_PAYBILL, TILL: B2B_TILL, BANK: B2B_PAYBILL, PHONE: B2C_PHONE}
B2B_TYPES = (PAYBILL, TILL, BANK)
COMMON_FIELDS = ("name", "docstatus", "account_type", "shortcode", "account_number", "expected_name", "active_from",
                 "is_default", "name_mismatch", "last_receiver_name")


class Profile(frappe._dict):
    """What one account DocType's approval differs in."""


PROFILES = {
    SUPPLIER_ACCOUNT: Profile(
        doctype=SUPPLIER_ACCOUNT, party_type="Supplier", party_field="supplier", label="supplier",
        name_field="supplier_name", types=(PAYBILL, TILL), hold_field="supplier_account_hold_hours",
        fields=("name", "docstatus", "supplier", "account_type", "shortcode", "account_number", "expected_name",
                "active_from", "is_default", "name_mismatch", "last_receiver_name"),
        proof="Attach the supplier's proof of these details (letter, invoice or statement)",
        add_hint="add one under Supplier > M-Pesa Accounts and have a manager approve it.",
        # who entered or changed these may not approve them
        destination_fields=("supplier", "account_type", "shortcode", "account_number", "expected_name"),
        user_link=None, unique_destination=False,
    ),
    EMPLOYEE_ACCOUNT: Profile(
        doctype=EMPLOYEE_ACCOUNT, party_type="Employee", party_field="employee", label="employee",
        name_field="employee_name", types=(PHONE, BANK), hold_field="employee_account_hold_hours",
        fields=("name", "docstatus", "employee", "account_type", "phone", "shortcode", "account_number", "bank",
                "expected_name", "active_from", "is_default", "name_mismatch", "last_receiver_name"),
        proof="Attach the employee's proof of these details (bank letter or statement, or M-Pesa registration)",
        add_hint="add one under Employee > M-Pesa Accounts and have a manager approve it.",
        destination_fields=("employee", "account_type", "phone", "shortcode", "account_number", "bank",
                            "expected_name"),
        # the employee's own user may not approve their account (nor pay themselves)
        user_link=("Employee", "user_id"),
        # a salary goes to one employee only: the same phone or bank account for two is refused
        unique_destination=True,
    ),
}
ACCOUNT_FOR_PARTY = {p.party_type: p.doctype for p in PROFILES.values()}


def profile(doctype) -> Profile:
    doctype = getattr(doctype, "doctype", doctype)
    if doctype not in PROFILES:
        frappe.throw(f"{doctype} is not an M-Pesa account DocType")
    return PROFILES[doctype]


def party_of(acc, doctype) -> str | None:
    return acc.get(profile(doctype).party_field) if acc else None


# ---------------------------------------------------------------- the account itself
def validate_account(doc) -> None:
    """On save of a draft account, and again at submit (the approval): clean and check the
    details, note who entered or changed them, refuse a repeat for the same party, and warn when
    another party has the same number approved (an employee's is refused)."""
    p = profile(doc)
    party = doc.get(p.party_field)
    if doc.account_type not in p.types:
        frappe.throw(f"Account Type must be {' or '.join(p.types)}")
    if doc.account_type == PHONE:
        msisdn = normalize_msisdn(doc.get("phone"))
        if not msisdn:
            frappe.throw("Enter the employee's M-Pesa phone number (07XX XXX XXX or 2547XX XXX XXX)")
        doc.phone = msisdn
        doc.shortcode = None
        doc.account_number = None
        same = {"phone": msisdn}
    else:
        _check_number(doc, p)
        same = {"shortcode": doc.shortcode, "account_number": doc.account_number or ["is", "not set"]}
    same.update({"docstatus": 1, "name": ["!=", doc.name or ""]})
    if frappe.db.exists(p.doctype, dict(same, **{p.party_field: party})):
        frappe.throw(f"This {p.label} already has an approved account with these details")
    others = frappe.get_all(p.doctype, filters=dict(same, **{p.party_field: ["!=", party]}), pluck=p.party_field)
    if others and p.unique_destination:
        what = "phone" if doc.account_type == PHONE else "bank account"
        frappe.throw(f"The same {what} is already approved for {', '.join(sorted(set(others)))}. One {p.label}'s "
                     f"account cannot be another's: check with the {p.label}.")
    if others:
        # Shared bank paybills (e.g. 522522) differ by account number, so an exact repeat is suspicious.
        frappe.msgprint(
            f"The same number is already approved for {', '.join(sorted(set(others)))}. "
            f"Check with the {p.label} before approving.",
            title="Possible duplicate",
            indicator="orange",
        )
    _note_who_entered(doc, p)


def _note_who_entered(doc, p) -> None:
    """entered_by: who entered or last changed the destination, who may not approve it. Read
    back from the database when nothing changed: the form's copy is not trusted."""
    fields = list(p.destination_fields)
    extra = ["owner", "entered_by"] if frappe.db.has_column(p.doctype, "entered_by") else ["owner"]
    stored = frappe.db.get_value(p.doctype, doc.name, [*fields, *extra], as_dict=True) if doc.get("name") else None
    if not stored or any(cstr(doc.get(f)).strip() != cstr(stored.get(f)).strip() for f in fields):
        doc.entered_by = frappe.session.user
    else:
        doc.entered_by = stored.get("entered_by") or stored.get("owner")


def own_user(doctype, party) -> str | None:
    """The party's own user (an employee's User ID), or None."""
    p = profile(doctype)
    if not p.user_link or not party:
        return None
    link_doctype, field = p.user_link
    return cstr(frappe.db.get_value(link_doctype, party, field)).strip() or None


def _check_number(doc, p) -> None:
    doc.shortcode = cstr(doc.shortcode).strip()
    doc.account_number = cstr(doc.account_number).strip() or None
    if p.doctype == EMPLOYEE_ACCOUNT:
        doc.phone = None
    what = "Bank Paybill Number" if doc.account_type == BANK else "Paybill / Till Number"
    if not doc.shortcode.isdigit() or not 5 <= len(doc.shortcode) <= 7:
        frappe.throw(f"{what} must be 5 to 7 digits")
    if doc.account_type == PAYBILL and not doc.account_number:
        frappe.throw("A paybill needs the Account Number the supplier gave you")
    if doc.account_type == BANK and not doc.account_number:
        frappe.throw("A bank paybill needs the employee's bank account number")
    if doc.account_type == TILL:
        doc.account_number = None
    if doc.account_number and len(doc.account_number) > 20:
        frappe.throw("Account Number is longer than M-Pesa allows (20 characters)")
    own = frappe.get_all("Mpesa Settings", pluck="business_shortcode") + frappe.get_all(
        "Mpesa Settings", pluck="till_number"
    )
    if doc.shortcode in {cstr(s).strip() for s in own if s}:
        frappe.throw(f"{doc.shortcode} is one of this company's own M-Pesa numbers")


def approve(doc) -> None:
    """Submitting is the approval: a manager who did not create the account or enter (or change)
    its details, and for an employee's account not the employee. validate_account runs just
    before (before_submit), so entered_by also names who changed the details in this submit."""
    p = profile(doc)
    require(ADMIN_ROLES)
    user = frappe.session.user
    if user != "Administrator":
        if user in {doc.owner, doc.get("entered_by")} - {None, ""}:
            frappe.throw("A different person must approve: you entered or changed the details of this M-Pesa "
                         "account.")
        if user == own_user(p.doctype, doc.get(p.party_field)):
            frappe.throw("This is your own M-Pesa account: another manager must approve it.")
    if not doc.proof:
        frappe.throw(p.proof)
    now = now_datetime()
    hold = cint(frappe.db.get_single_value(SUITE, p.hold_field))
    doc.approved_by = user
    doc.approved_on = now
    doc.active_from = add_to_date(now, hours=hold) if hold > 0 else now


def after_approve(doc) -> None:
    if doc.is_default:
        set_default(doc)


def set_default(doc) -> None:
    p = profile(doc)
    for other in frappe.get_all(
        p.doctype,
        filters={p.party_field: doc.get(p.party_field), "docstatus": 1, "is_default": 1, "name": ["!=", doc.name]},
        pluck="name",
    ):
        frappe.db.set_value(p.doctype, other, "is_default", 0)


# ---------------------------------------------------------------- using it on a payout
def usable_problem(acc, party=None, doctype=SUPPLIER_ACCOUNT) -> str | None:
    """Why an account cannot be paid right now, or None."""
    p = profile(doctype)
    if not acc:
        return f"{p.doctype} not found"
    if acc.docstatus != 1:
        return f"{p.doctype} {acc.name} is not approved" + (" (cancelled)" if acc.docstatus == 2 else "")
    owner = acc.get(p.party_field)
    if party and owner != party:
        return f"{p.doctype} {acc.name} belongs to {owner}, not {party}"
    if acc.active_from and get_datetime(acc.active_from) > now_datetime():
        return f"{p.doctype} {acc.name} is on hold until {acc.active_from} (new accounts wait before first use)"
    if cint(acc.get("name_mismatch")):
        # A paybill whose holder is not the party is how invoice-redirection fraud looks.
        return (
            f"{p.doctype} {acc.name} is blocked: M-Pesa last paid {acc.get('last_receiver_name')!r}, "
            f"not {acc.expected_name!r}. A manager must check with the {p.label} and press Receiver Name "
            "Confirmed on the account before it is paid again."
        )
    return None


def get_account(doctype, name):
    return frappe.db.get_value(doctype, name, list(profile(doctype).fields), as_dict=True) if name else None


def accounts_of(doctype, party, types=None) -> list:
    """The party's approved accounts (of ``types``), the default first, then by creation."""
    p = profile(doctype)
    rows = frappe.get_all(p.doctype, filters={p.party_field: party, "docstatus": 1}, fields=list(p.fields),
                          order_by="is_default desc, creation asc")
    return [r for r in rows if not types or r.account_type in types]


def pick_account(doctype, party, types=None) -> str | None:
    """The party's default approved account that can be paid now, or its only one."""
    rows = [r for r in accounts_of(doctype, party, types) if not usable_problem(r, party, doctype)]
    rows.sort(key=lambda r: 0 if r.is_default else 1)  # stable: keeps creation order otherwise
    if not rows:
        return None
    if rows[0].is_default or len(rows) == 1:
        return rows[0].name
    return None


def default_account(doctype, party, types=None) -> str | None:
    """The party's default approved account, or its only one; one still in its hold period
    counts (a payment planned for later may be due after it)."""
    rows = accounts_of(doctype, party, types)
    rows.sort(key=lambda r: 0 if r.is_default else 1)
    if rows and (rows[0].is_default or len(rows) == 1):
        return rows[0].name
    return None


def destination(acc) -> tuple:
    """(route, destination) of an approved account: ('B2B Paybill', '888880 / ACME-1'), ('B2C Phone', '2547...')."""
    route = ROUTE_FOR.get(acc.get("account_type"))
    if route == B2C_PHONE:
        return route, cstr(acc.get("phone")).strip()
    shortcode = cstr(acc.get("shortcode")).strip()
    account = cstr(acc.get("account_number")).strip()
    return route, f"{shortcode} / {account}" if account else shortcode


def unusable_reasons(doctype, party, types=None) -> list:
    """Why each of the party's approved accounts (of ``types``) cannot be paid now."""
    return [usable_problem(r, party, doctype) for r in accounts_of(doctype, party, types)]


# ---------------------------------------------------------------- after payment
def _tokens(name) -> set:
    text = re.sub(r"^\s*\d+\s*-\s*", "", str(name or ""))  # "888880 - KPLC PREPAID", "254722000000 - JOHN DOE"
    words = re.findall(r"[A-Z0-9]+", text.upper())
    return {w for w in words if w not in {"LTD", "LIMITED", "CO", "COMPANY", "THE", "AND", "KENYA", "KE"}}


def names_match(expected, received) -> bool:
    """Loose match: every word of the shorter name appears in the longer one."""
    a, b = _tokens(expected), _tokens(received)
    if not a or not b:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return small <= big


def record_receiver_name(doctype, account, received_name, payout, notify=None) -> bool | None:
    """After a Paid result: remember who M-Pesa paid and raise the alarm (the account is blocked)
    if it is not the name on file. Returns whether the names differed (None: nothing checked)."""
    if not account or not received_name:
        return None
    p = profile(doctype)
    acc = get_account(doctype, account)
    if not acc:
        return None
    mismatch = not names_match(acc.expected_name, received_name)
    frappe.db.set_value(
        p.doctype,
        acc.name,
        {"last_receiver_name": received_name, "last_paid_on": now_datetime(), "name_mismatch": 1 if mismatch else 0},
    )
    if not mismatch:
        return False
    if payout.doctype == "Mpesa B2B Payment Request":
        payout.db_set("receiver_name_mismatch", 1)
    message = (
        f"M-Pesa paid {received_name!r}, but {p.doctype} {acc.name} expects "
        f"{acc.expected_name!r}. Check with the {p.label} before paying this account again."
    )
    payout.add_comment("Comment", message)
    frappe.log_error(title="M-Pesa receiver name mismatch", message=f"{payout.name}: {message}",
                     reference_doctype=payout.doctype, reference_name=payout.name)
    (notify or alert)(message, payout)
    return True


def confirm_receiver_name(doc, adopt_name: bool = False) -> None:
    """A manager checked with the party: unblock the account, optionally taking M-Pesa's name."""
    p = profile(doc)
    require(ADMIN_ROLES)
    if not cint(doc.get("name_mismatch")):
        return
    values = {"name_mismatch": 0}
    note = f"Receiver name {doc.get('last_receiver_name')!r} confirmed with the {p.label} by {frappe.session.user}"
    if adopt_name and doc.get("last_receiver_name"):
        values["expected_name"] = re.sub(r"^\s*\d+\s*-\s*", "", doc.last_receiver_name).strip()
        note += f"; expected name changed from {doc.expected_name!r} to {values['expected_name']!r}"
    frappe.db.set_value(p.doctype, doc.name, values)
    doc.add_comment("Comment", note + ".")


def alert(message, doc) -> None:
    """A notification to the Notify Role (M-Pesa Suite Settings). Never raises."""
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
        frappe.log_error(frappe.get_traceback(), "M-Pesa alert failed")


def accounts_for(doctype, party) -> list:
    """Approved and draft accounts of a party for its form, with whether each can be paid now."""
    p = profile(doctype)
    frappe.has_permission(p.doctype, "read", throw=True)
    rows = frappe.get_list(
        p.doctype,
        filters={p.party_field: party, "docstatus": ["<", 2]},
        fields=list(p.fields),
        order_by="is_default desc, creation asc",
    )
    for row in rows:
        row.problem = usable_problem(row, party, p.doctype)
    return rows
