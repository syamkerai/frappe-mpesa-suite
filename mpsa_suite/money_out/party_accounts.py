"""Approved M-Pesa destinations of a party: a supplier's or an employee's phone, paybill, till or
bank account (paid through the bank's paybill).

A payout goes to whatever number is on it, so a typo or a swapped number sends money to the
wrong person. Destinations therefore live on an account that one person enters (with proof)
and a different manager approves by submitting it: not who created it, not who last entered or
changed its details (entered_by, set on every save that changes them and checked again at
submit), and for an employee's account not the employee. Approved details cannot be edited
(cancel and amend, which needs approval again), become usable only after a hold period, and
payouts copy the destination from the approved account instead of trusting typed values. After
each payment, the name Safaricom says it paid is compared with the name on file; a mismatch
blocks the account until a manager confirms it.

A party may have many approved accounts, one record per destination, each with its own proof,
approval and hold; one is the party's default (``make_default``), where salaries and payments
that name no account go. The same destination (``destination_key``) is never approved twice for
one party; an employee's phone or bank account is never another employee's, nor a supplier's
(a supplier paid to an employee's own account is how a fake supplier looks).

One profile per account DocType (Supplier M-Pesa Account, Employee M-Pesa Account) holds what
differs: the party, the hold setting, who may change the default and the wording. Both take the
same four account types. supplier_accounts and employee_accounts are thin wrappers over this
module.

A bank account is paid by B2B to the bank's paybill (an M-Pesa Bank Paybill row, money_out.
bank_paybills), with the account number as the reference. Safaricom names the bank, not the
account holder ("522522 - KCB Paybill AC"), so the name check confirms the bank only; who holds
the account rests on the proof, the second approver and the hold period.
"""

import re

import frappe
from frappe.utils import add_to_date, cint, cstr, get_datetime, now_datetime

from ..daraja import money_out as daraja
from ..permissions import ADMIN_ROLES, CONFIG_ROLES, administrator_refused, require, require_named_user
from ..utils.phone import FORMATS, is_safaricom, normalize_msisdn, payable_msisdn
from . import send_confirm

SUITE = "M-Pesa Suite Settings"
SUPPLIER_ACCOUNT = "Supplier M-Pesa Account"
EMPLOYEE_ACCOUNT = "Employee M-Pesa Account"
BANK_PAYBILL = "M-Pesa Bank Paybill"
# Account types (both DocTypes take all four)
PHONE = "Phone (B2C)"
PAYBILL = "Paybill"
TILL = "Till (Buy Goods)"
BANK = "Bank Account (via Bank Paybill)"
OLD_BANK = "Bank Paybill (B2B)"  # the Employee type's label before round 4 (renamed on migrate)
ALL_TYPES = (PHONE, PAYBILL, TILL, BANK)
# The Daraja B2B command of each B2B account type (a phone is paid by B2C)
COMMAND_FOR = {PAYBILL: daraja.B2B_PAYBILL, TILL: daraja.B2B_BUY_GOODS, BANK: daraja.B2B_PAYBILL}
# The payment schedule's route of each account type
B2B_PAYBILL, B2B_TILL, B2C_PHONE = "B2B Paybill", "B2B Till", "B2C Phone"
ROUTE_FOR = {PAYBILL: B2B_PAYBILL, TILL: B2B_TILL, BANK: B2B_PAYBILL, PHONE: B2C_PHONE}
B2B_TYPES = (PAYBILL, TILL, BANK)
COMMON_FIELDS = ("name", "docstatus", "account_type", "phone", "shortcode", "account_number", "bank_paybill", "bank",
                 "account_holder_name", "expected_name", "active_from", "is_default", "name_mismatch",
                 "last_receiver_name", "needs_reapproval", "reapproval_reason", "destination_key")
# Who entered or changed these may not approve them
DESTINATION_FIELDS = ("account_type", "phone", "shortcode", "account_number", "bank_paybill", "bank",
                      "account_holder_name", "expected_name")
# Copied onto a bank account from its bank's row on every save, never typed
FROM_THE_BANK = ("shortcode", "bank", "expected_name")
# Written by M-Pesa Suite only (frappe.db.set_value), never by a save after approval; with their labels
SET_BY_THE_SUITE = {"name_mismatch": "Name Did Not Match", "needs_reapproval": "Blocked: Approve Again",
                    "reapproval_reason": "Why It Is Blocked", "last_receiver_name": "Name M-Pesa Paid Last Time",
                    "last_paid_on": "Last Paid On"}
BANK_FIELDS = ["name", "institution_name", "paybill", "bank", "enabled", "receiver_name", "account_number_rule",
               "account_number_pattern", "observed_receiver_name"]
# The number M-Pesa puts before a name: "522522 - KCB Paybill AC", "2547****0111 - JANE", and with an
# en-dash or em-dash as in one of Safaricom's own samples
_NUMBER_PREFIX = re.compile(r"^\s*([0-9][0-9*xX]*)\s*[-\u2013\u2014]\s*")


class Profile(frappe._dict):
    """What one account DocType's approval differs in."""


PROFILES = {
    SUPPLIER_ACCOUNT: Profile(
        doctype=SUPPLIER_ACCOUNT, party_type="Supplier", party_field="supplier", label="supplier",
        name_field="supplier_name", types=ALL_TYPES, hold_field="supplier_account_hold_hours",
        fields=("supplier", *COMMON_FIELDS),
        proof="Attach the supplier's proof of these details (letter, invoice or statement)",
        add_hint="add one under Supplier > M-Pesa Accounts and have a manager approve it.",
        destination_fields=("supplier", *DESTINATION_FIELDS),
        user_link=None, unique_destination=False,
        # who may change the supplier's default account (make_default): where payments that name no
        # account go is payout routing, so not System Manager, which only configures (decision 1)
        default_roles=("M-Pesa Manager", "Accounts Manager", "Accounts User", "Purchase Manager"),
    ),
    EMPLOYEE_ACCOUNT: Profile(
        doctype=EMPLOYEE_ACCOUNT, party_type="Employee", party_field="employee", label="employee",
        name_field="employee_name", types=ALL_TYPES, hold_field="employee_account_hold_hours",
        fields=("employee", *COMMON_FIELDS),
        proof="Attach the employee's proof of these details (bank letter or statement, or M-Pesa registration)",
        add_hint="add one under Employee > M-Pesa Accounts and have a manager approve it.",
        destination_fields=("employee", *DESTINATION_FIELDS),
        # the employee's own user may not approve their account (nor pay themselves)
        user_link=("Employee", "user_id"),
        # a salary goes to one employee only: the same destination for two is refused
        unique_destination=True,
        default_roles=(*ADMIN_ROLES, "HR Manager"),
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


def party_name(p, party) -> str:
    return cstr(frappe.db.get_value(p.party_type, party, p.name_field) if party else "").strip()


def is_bank(acc) -> bool:
    return acc.get("account_type") in (BANK, OLD_BANK)


# ---------------------------------------------------------------- the account itself
def validate_account(doc) -> None:
    """On save of a draft account, and again at submit (the approval): clean and check the
    details for the account type, note who entered or changed them, and refuse or warn when the
    same destination is approved already (``_check_unique``)."""
    p = profile(doc)
    party = doc.get(p.party_field)
    _forget_the_last_approval(doc)
    if doc.account_type == OLD_BANK:
        doc.account_type = BANK  # a draft saved before the rename
    if doc.account_type not in p.types:
        frappe.throw(f"Account Type must be {', '.join(p.types[:-1])} or {p.types[-1]}")
    if doc.account_type == PHONE:
        _check_phone(doc, p)
    elif doc.account_type == BANK:
        _check_bank_account(doc, p, party)
        _refuse_unseen_bank_paybill(doc, p)
    else:
        _check_number(doc, p)
    if doc.account_type != BANK and not cstr(doc.get("expected_name")).strip():
        frappe.throw("Enter the name M-Pesa shows for this number (Name M-Pesa Shows)")
    if p.doctype == EMPLOYEE_ACCOUNT and doc.account_type in (PAYBILL, TILL):
        frappe.msgprint(
            "This pays an employee to a business paybill or till: check that the proof shows the number is the "
            "employee's.",
            title="A business number for an employee",
            indicator="orange",
        )
    doc.destination_key = destination_key(doc)
    _check_unique(doc, p, party)
    _note_who_entered(doc, p)


def _forget_the_last_approval(doc) -> None:
    """A draft starts unblocked and unpaid: Amend copies every field of the cancelled account, its
    no-copy ones too (frappe.model.copy_doc with from_amend, v15 and v16), so without this the
    account a manager approves again would stay blocked by the old account's bank paybill change,
    name mismatch or unchecked approval, which cancel, amend and approve again are there to clear."""
    for field in SET_BY_THE_SUITE:
        if doc.get(field):
            setattr(doc, field, 0 if field in ("name_mismatch", "needs_reapproval") else None)


def _clear(doc, *fields) -> None:
    for field in fields:
        setattr(doc, field, None)


def _check_phone(doc, p) -> None:
    msisdn = payable_msisdn(doc.get("phone"))  # the B2C row's rule: an account B2C can pay
    if not msisdn:
        frappe.throw(f"Enter the {p.label}'s M-Pesa phone number ({FORMATS})")
    doc.phone = msisdn
    _clear(doc, "shortcode", "account_number", "bank_paybill", "bank", "account_holder_name")
    if not is_safaricom(msisdn):
        # not refused: a number moved from another network keeps its old prefix
        frappe.msgprint(
            f"0{msisdn[3:]} does not start like a Safaricom number, and B2C pays M-Pesa lines only. Check with the "
            f"{p.label} that it is an M-Pesa line.",
            title="Not a Safaricom number?",
            indicator="orange",
        )


def _check_number(doc, p) -> None:
    """A paybill (with the account number the party gave) or a till."""
    doc.shortcode = cstr(doc.shortcode).strip()
    doc.account_number = cstr(doc.account_number).strip() or None
    _clear(doc, "phone", "bank_paybill", "bank", "account_holder_name")
    if not doc.shortcode.isdigit() or not 5 <= len(doc.shortcode) <= 7:
        frappe.throw("Paybill / Till Number must be 5 to 7 digits")
    if doc.account_type == PAYBILL and not doc.account_number:
        frappe.throw(f"A paybill needs the Account Number the {p.label} gave you")
    if doc.account_type == TILL:
        doc.account_number = None
    _check_length(doc.account_number)
    check_not_own(doc.shortcode)
    if doc.account_type == PAYBILL:
        row = frappe.db.get_value(BANK_PAYBILL, {"paybill": doc.shortcode, "enabled": 1},
                                  ["name", "institution_name"], as_dict=True)
        if row and "#" in cstr(doc.account_number):
            # a business's collection code at the bank, not an account there: "Lipa na I&M" is
            # 542542 with 888888#house10 (code#reference), and stays a paybill
            frappe.msgprint(
                f"{doc.shortcode} is {row.institution_name}'s bank paybill, and {doc.account_number} is a business's "
                "collection reference there (code#reference), so this stays a Paybill. M-Pesa shows the bank's name "
                f"for this paybill: enter it in Name M-Pesa Shows. For an account at {row.institution_name}, pick "
                "Account Type Bank Account (via Bank Paybill).",
                title="A collection reference at a bank",
                indicator="orange",
            )
        elif row:
            # the bank's account number rules and Safaricom's name for the bank apply to it
            frappe.throw(f"{doc.shortcode} is {row.institution_name}'s bank paybill: pick Account Type Bank Account "
                         f"(via Bank Paybill) and {row.institution_name}. (A business's collection reference at the "
                         "bank, written code#reference such as 888888#INV7, stays a Paybill.)")


def _check_bank_account(doc, p, party) -> None:
    """A bank account: the paybill and the name M-Pesa shows come from the bank's row, the account
    number is the party's at that bank."""
    if not cstr(doc.get("bank_paybill")).strip():
        frappe.throw("Pick the bank the account is at (Bank)")
    row = frappe.db.get_value(BANK_PAYBILL, doc.bank_paybill, BANK_FIELDS, as_dict=True)
    if not row:
        frappe.throw(f"M-Pesa Bank Paybill {doc.bank_paybill} not found")
    if not cint(row.enabled):
        frappe.throw(f"{row.institution_name} is switched off in M-Pesa Bank Paybills: an M-Pesa Manager checks its "
                     "paybill and enables it first.")
    doc.shortcode = cstr(row.paybill).strip()
    doc.bank = row.bank or None
    doc.expected_name = cstr(row.receiver_name).strip() or row.institution_name
    doc.phone = None
    raw = re.sub(r"[\s-]", "", cstr(doc.get("account_number"))).upper()
    if not raw:
        frappe.throw(f"A bank account needs the {p.label}'s account number at {row.institution_name}")
    if not raw.isalnum():
        typed = cstr(doc.get("account_number")).strip()
        frappe.throw("A bank account number has letters and digits only"
                     + (f". {typed} is a business's collection reference (code#reference): pick Account Type Paybill, "
                        f"with Paybill {doc.shortcode} and {typed} as the Account Number." if "#" in raw else ""))
    doc.account_number = raw
    _check_length(raw)
    pattern = cstr(row.account_number_pattern).strip()
    if pattern and not _fits(pattern, raw):
        rule = cstr(row.account_number_rule).strip()
        frappe.throw(f"{raw} does not look like a {row.institution_name} account number"
                     + (f" ({rule})" if rule else "")
                     + f". Check it with the {p.label}, or ask an M-Pesa Manager to correct the bank's Account "
                     "Number Pattern.")
    check_not_own(doc.shortcode)
    name = party_name(p, party)
    holder = cstr(doc.get("account_holder_name")).strip() or name
    if not holder:
        frappe.throw("Enter the Account Holder Name")
    doc.account_holder_name = holder
    if name and not names_match(name, holder):
        frappe.msgprint(
            f"The account holder {holder!r} is not {name!r}: this pays a third party's account. Check the proof "
            "before approving.",
            title="Third-party account",
            indicator="orange",
        )


def _refuse_unseen_bank_paybill(doc, p) -> None:
    """At the approval (before_submit) a bank account's paybill is copied again from its bank's row,
    after Approve showed, and its token was bound to, the draft as saved. A row whose paybill changed
    since then would make the approved number one nobody approved (a draft is never flagged, and
    copying it is nobody's change): refused until the draft is saved again and shown again."""
    if cint(doc.docstatus) != 1 or not doc.get("name"):
        return
    saved = frappe.db.get_value(p.doctype, doc.name, ["name", "shortcode"], as_dict=True)
    if not saved:
        return  # not saved yet: nothing was shown
    before = cstr(saved.shortcode).strip()
    if before != doc.shortcode:
        frappe.throw(f"The bank's paybill changed from {before or 'none'} to {doc.shortcode} since this account was "
                     "saved: save it again and press Approve again, so the approval shows the new number.")


def _fits(pattern, value) -> bool:
    try:
        return bool(re.fullmatch(pattern, value))
    except re.error:
        return True  # a broken pattern refuses nothing (the bank row refuses one on save)


def _check_length(account_number) -> None:
    if account_number and len(account_number) > 20:
        frappe.throw("Account Number is longer than M-Pesa allows (20 characters)")


def check_not_own(shortcode) -> None:
    own = frappe.get_all("Mpesa Settings", pluck="business_shortcode") + frappe.get_all(
        "Mpesa Settings", pluck="till_number"
    )
    if cstr(shortcode).strip() in {cstr(s).strip() for s in own if s}:
        frappe.throw(f"{shortcode} is one of this company's own M-Pesa numbers")


def destination_key(acc) -> str | None:
    """Where an account pays, one value per destination: 'B2C:2547...', 'PB:522522:0123456789',
    'TILL:835811'. The account number is upper-cased without spaces and dashes (a bank's, letters
    and digits only), so the same destination typed two ways is one key."""
    account_type = acc.get("account_type")
    if account_type == PHONE:
        phone = cstr(acc.get("phone")).strip()
        return f"B2C:{phone}" if phone else None
    shortcode = cstr(acc.get("shortcode")).strip()
    if not shortcode:
        return None
    if account_type == TILL:
        return f"TILL:{shortcode}"
    if account_type in (PAYBILL, BANK, OLD_BANK):
        strip = r"[^A-Za-z0-9]" if account_type != PAYBILL else r"[\s-]"
        return f"PB:{shortcode}:{re.sub(strip, '', cstr(acc.get('account_number'))).upper()}"
    return None


def _what(account_type) -> str:
    return {PHONE: "phone", TILL: "till", PAYBILL: "paybill and account number"}.get(account_type, "bank account")


def _check_unique(doc, p, party) -> None:
    """The same destination approved already: for this party, refused; for another employee (this
    is an employee's account), refused: a salary goes to one person; for another supplier, a
    warning (shared tills and paybills are real). Between a supplier and an employee, a phone or a
    bank account is refused (the ghost-vendor pattern) and a paybill or till warned. An employee
    is named only to who may see salaries."""
    key = doc.destination_key
    if not key:
        return
    from .salary_access import can_see_salaries

    refused, warned = [], []
    for other in PROFILES.values():
        rows = frappe.get_all(other.doctype, filters={"destination_key": key, "docstatus": 1,
                                                      "name": ["!=", doc.name or ""]},
                              fields=["name", "account_type", other.party_field])
        for row in rows:
            other_party = row.get(other.party_field)
            if other.doctype == p.doctype and other_party == party:
                frappe.throw(f"This {p.label} already has an approved account with these details")
            if other.doctype == p.doctype:
                hit = p.unique_destination
            else:
                hit = bool({doc.account_type, row.account_type} & {PHONE, BANK, OLD_BANK})
            (refused if hit else warned).append((other, other_party))
    if not (refused or warned):
        return
    salaries = can_see_salaries()

    def who(found) -> str:
        names = sorted({party_ if o.doctype == SUPPLIER_ACCOUNT or salaries else "an employee" for o, party_ in found})
        return ", ".join(names)

    what = _what(doc.account_type)
    if refused:
        if all(o.doctype == p.doctype for o, _ in refused):
            frappe.throw(f"The same {what} is already approved for {who(refused)}. One {p.label}'s account cannot "
                         f"be another's: check with the {p.label}.")
        frappe.throw(f"The same {what} is already approved for {who(refused)}. A supplier and an employee cannot "
                     f"share a {what} (a supplier paid to an employee's own account is how a fake supplier looks): "
                     "check with both.")
    frappe.msgprint(
        f"The same {what} is already approved for {who(warned)}. Check with the {p.label} before approving.",
        title="Possible duplicate",
        indicator="orange",
    )


def _note_who_entered(doc, p) -> None:
    """entered_by: who entered or last changed the destination, who may not approve it. Read
    back from the database when nothing changed: the form's copy is not trusted. A bank account's
    paybill and names come from its bank's row, so copying them again is nobody's change."""
    fields = list(p.destination_fields)
    extra = ["owner", "entered_by"] if frappe.db.has_column(p.doctype, "entered_by") else ["owner"]
    stored = frappe.db.get_value(p.doctype, doc.name, [*fields, *extra], as_dict=True) if doc.get("name") else None
    typed = [f for f in fields if not (doc.account_type == BANK and f in FROM_THE_BANK)]
    if not stored or any(cstr(doc.get(f)).strip() != cstr(stored.get(f)).strip() for f in typed):
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


def approve(doc) -> None:
    """Submitting is the approval: a named manager (never the shared Administrator login while
    M-Pesa Suite Settings refuses it) who did not create the account or enter (or change) its
    details, and for an employee's account not the employee, through the deliberate Approve step
    (send_confirm). validate_account runs just before (before_submit), so entered_by also names
    who changed the details in this submit."""
    p = profile(doc)
    require(ADMIN_ROLES)
    require_named_user()
    send_confirm.assert_deliberate(doc)
    user = frappe.session.user
    if user in {doc.owner, doc.get("entered_by")} - {None, ""}:
        frappe.throw("A different person must approve: you entered or changed the details of this M-Pesa account.")
    if user == own_user(p.doctype, doc.get(p.party_field)):
        frappe.throw("This is your own M-Pesa account: another manager must approve it.")
    if not doc.proof:
        frappe.throw(p.proof)
    now = now_datetime()
    hold = cint(frappe.db.get_single_value(SUITE, p.hold_field))
    doc.approved_by = user
    doc.approved_on = now
    doc.active_from = add_to_date(now, hours=hold) if hold > 0 else now


# ---------------------------------------------------------------- the party's default (D1-D5)
def blocked(acc) -> bool:
    """Blocked until a manager acts: a name mismatch, or a bank paybill changed since approval."""
    return bool(cint(acc.get("name_mismatch")) or cint(acc.get("needs_reapproval")))


def note(doctype, name, text) -> None:
    """A Comment on an account the code changed without saving it."""
    frappe.get_doc(doctype, name).add_comment("Comment", text)


def _set_default_flag(doctype, name, value) -> None:
    """is_default written directly (the field is read-only once approved), with a Version for the
    change, as a save would leave."""
    old = cint(frappe.db.get_value(doctype, name, "is_default"))
    if old == cint(value):
        return
    frappe.db.set_value(doctype, name, "is_default", cint(value))
    frappe.get_doc({
        "doctype": "Version",
        "ref_doctype": doctype,
        "docname": name,
        "data": frappe.as_json({"added": [], "changed": [["is_default", old, cint(value)]], "removed": [],
                                "row_changed": []}),
    }).insert(ignore_permissions=True)


def after_approve(doc) -> None:
    """on_submit. D1: one default per party (an account approved as the default takes over). D2:
    when a second account is approved and the first was the default only because it was the
    only one, the first is now ticked, so the new one does not become where salaries go unasked."""
    p = profile(doc)
    if cint(doc.is_default):
        set_default(doc)
        return
    others = [r for r in accounts_of(p.doctype, doc.get(p.party_field)) if r.name != doc.name]
    if len(others) == 1 and not cint(others[0].is_default) and not blocked(others[0]):
        _set_default_flag(p.doctype, others[0].name, 1)
        note(p.doctype, others[0].name, f"Ticked as the default of this {p.label}: it was the only approved account "
                                        f"until {doc.name} was approved.")
        doc.add_comment("Comment", f"{others[0].name} stays the default of this {p.label}. Make Default changes it.")


def set_default(doc) -> None:
    """D1: ``doc`` is the party's default, no other account is."""
    p = profile(doc)
    for other in frappe.get_all(
        p.doctype,
        filters={p.party_field: doc.get(p.party_field), "docstatus": 1, "is_default": 1, "name": ["!=", doc.name]},
        pluck="name",
    ):
        _set_default_flag(p.doctype, other, 0)
        note(p.doctype, other, f"No longer the default of this {p.label}: {doc.name} is.")


# Submitted records that keep an account's name after it pays: a B2B request (Paid, Failed or Unknown
# stays submitted) and an Expense Claim's M-Pesa Payees row. Each copied the destination when it was
# made, and every send checks the account again (cancelled: refused), so they never stop the account
# being cancelled (Frappe's back-link check reads doc.ignore_linked_doctypes from the instance).
CANCEL_IGNORES = ("Mpesa B2B Payment Request", "Expense Claim")


def before_cancel(doc) -> None:
    """before_cancel. Cancelling is never refused (a compromised number must go at once, and
    "cancel and amend" is how a blocked account is approved again): the payouts and claims that
    name the account do not stop it."""
    doc.ignore_linked_doctypes = CANCEL_IGNORES
    doc.is_default = 0


def after_cancel(doc) -> None:
    """on_cancel. Cancelling is never refused (a compromised number must go at once). The one
    account left becomes the default; with two or more left and no default, none is guessed: the
    party's form asks for one and payroll leaves the party out."""
    p = profile(doc)
    left = [r for r in accounts_of(p.doctype, doc.get(p.party_field)) if r.name != doc.name]
    if len(left) == 1 and not cint(left[0].is_default) and not blocked(left[0]):
        _set_default_flag(p.doctype, left[0].name, 1)
        note(p.doctype, left[0].name, f"Now the default of this {p.label}: {doc.name} was cancelled and this is the "
                                      "only approved account left.")


def refuse_default_edit(doc) -> None:
    """before_update_after_submit: the default of an approved account changes only through Make
    Default, which checks who may (D4); the block (a name mismatch, a bank paybill change) and the
    last payment only through the code that sets them (frappe.db.set_value), so a save after
    approval never unblocks an account. Frappe enforces neither read only nor, once a site sets
    Allow on Submit on a field, anything else on such a save."""
    if _changed(doc, "is_default"):
        frappe.throw("Use Make Default on the account to change the default.")
    changed = [label for field, label in SET_BY_THE_SUITE.items() if _changed(doc, field)]
    if changed:
        frappe.throw(f"{', '.join(changed)}: set by M-Pesa Suite only on an approved M-Pesa account. A manager "
                     "presses Receiver Name Confirmed after a name mismatch, and an account whose bank paybill "
                     "changed is cancelled, amended and approved again.")


def _changed(doc, field) -> bool:
    """Whether this save changes ``field`` (0, "" and None alike; dates as Frappe compares them)."""
    before = doc.get_doc_before_save()
    if before is None:
        return True
    if field in ("is_default", "name_mismatch", "needs_reapproval"):
        return cint(before.get(field)) != cint(doc.get(field))
    if not (before.get(field) or doc.get(field)):
        return False
    return doc.has_value_changed(field)


def can_make_default(doc, user=None) -> bool:
    p = profile(doc)
    return (doc.docstatus == 1 and not cint(doc.is_default) and not blocked(doc)
            and not set(p.default_roles).isdisjoint(frappe.get_roles(user)) and not administrator_refused(user))


def make_default(doc) -> None:
    """D4: make an approved account the party's default. Not maker-checker: no new destination is
    introduced (every candidate was approved by a second person). D3: an account blocked by a
    name mismatch or a bank paybill change cannot be the default; one still on hold can. D5: lines
    and draft requests already made keep the account they name. Leaves a Version and a Comment;
    an employee's change also alerts the Notify Role (no names: it is salary data). Where payments
    go is routing: a named login (never the shared Administrator one), and not System Manager.

    The form calls it by name (run_doc_method with dt and dn): sent with the form's copy of the
    document, Frappe would check it as a save after submit, which needs submit permission that HR
    and Accounts do not hold."""
    p = profile(doc)
    require(p.default_roles)
    require_named_user()
    doc.check_permission("read")
    stored = get_account(p.doctype, doc.name)
    if not stored or stored.docstatus != 1:
        frappe.throw("Only an approved account can be the default: approve it first.")
    if blocked(stored):
        frappe.throw(f"{doc.name} is blocked and cannot be the default: "
                     f"{usable_problem(stored, None, p.doctype)}")
    if cint(stored.is_default):
        return
    user = frappe.session.user
    # one change of the party's default at a time (D1)
    frappe.db.get_value(p.party_type, stored.get(p.party_field), "name", for_update=True)
    previous = frappe.get_all(p.doctype, filters={p.party_field: stored.get(p.party_field), "docstatus": 1,
                                                  "is_default": 1, "name": ["!=", doc.name]}, pluck="name")
    _set_default_flag(p.doctype, doc.name, 1)
    for other in previous:
        _set_default_flag(p.doctype, other, 0)
        note(p.doctype, other, f"No longer the default of this {p.label}: {user} made {doc.name} the default.")
    doc.is_default = 1
    doc.add_comment("Comment", f"Made the default of this {p.label} by {user}"
                               + (f", in place of {', '.join(previous)}." if previous else "."))
    if p.doctype == EMPLOYEE_ACCOUNT:
        alert(f"{user} changed the default M-Pesa account of an employee to {doc.name}.", doc)


def form_state(doc) -> dict:
    """What an account's form shows: whether this user may press Make Default, and how many
    approved accounts the party has with which default (the "pick a default" headline)."""
    p = profile(doc)
    party = doc.get(p.party_field)
    approved = accounts_of(p.doctype, party) if party else []
    return {
        "can_make_default": can_make_default(doc),
        "approved": len(approved),
        "default": next((r.name for r in approved if cint(r.is_default)), None),
    }


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
    if cint(acc.get("needs_reapproval")):
        return (f"{p.doctype} {acc.name} is blocked: "
                + (cstr(acc.get("reapproval_reason")).strip() or "its bank paybill changed since it was approved: "
                   "cancel and amend it so a manager approves the new number."))
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
    """The account a payment that names none goes to (plan B.4): the party's default when it is of
    ``types`` and can be paid now; when the default cannot be paid (on hold, blocked), none: the
    payment waits or the payer picks another account, never another one silently. With no default
    of ``types``, the only one of them that can be paid now (a phone default with one bank account:
    B2B picks the bank account)."""
    rows = accounts_of(doctype, party, types)
    default = next((r for r in rows if cint(r.is_default)), None)
    if default:
        return None if usable_problem(default, party, doctype) else default.name
    usable = [r for r in rows if not usable_problem(r, party, doctype)]
    return usable[0].name if len(usable) == 1 else None


def pick_problem(doctype, party, types=None) -> str | None:
    """Why ``pick_account`` picks nothing, when one reason stands: the default (of ``types``) cannot
    be paid now, or no account of ``types`` can (the first one's reason)."""
    rows = accounts_of(doctype, party, types)
    default = next((r for r in rows if cint(r.is_default)), None)
    if default:
        problem = usable_problem(default, party, doctype)
        if problem:
            return (f"{problem.rstrip('.')}. It is the {profile(doctype).label}'s default: pick another approved "
                    "account to pay now.")
        return None
    problems = [usable_problem(r, party, doctype) for r in rows]
    return problems[0] if problems and all(problems) else None


def default_account(doctype, party, types=None) -> str | None:
    """The party's default approved account (of ``types``), or its only one; one still in its
    hold period counts (a payment planned for later may be due after it)."""
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
def number_prefix(name) -> str | None:
    """The number M-Pesa puts before the name ("522522 - KCB Paybill AC" gives 522522; a masked
    phone keeps its stars), or None."""
    found = _NUMBER_PREFIX.match(cstr(name))
    return found.group(1) if found else None


def without_number(name) -> str:
    return _NUMBER_PREFIX.sub("", cstr(name), count=1).strip()


def _tokens(name) -> set:
    words = re.findall(r"[A-Z0-9]+", without_number(name).upper())
    return {w for w in words if w not in {"LTD", "LIMITED", "CO", "COMPANY", "THE", "AND", "KENYA", "KE"}}


def names_match(expected, received, min_words: int = 1) -> bool:
    """Loose match: every word of the shorter name appears in the longer one. ``min_words``: both
    names have at least that many words (a staff check: one first name alone matches no one)."""
    a, b = _tokens(expected), _tokens(received)
    if not a or not b or min(len(a), len(b)) < min_words:
        return False
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return small <= big


def number_problem(acc, received_name) -> str | None:
    """The number in front of the received name is not the account's (its shortcode, or its phone
    when M-Pesa did not mask it)."""
    prefix = number_prefix(received_name)
    if not prefix or not prefix.isdigit():
        return None
    if acc.get("account_type") == PHONE:
        expected = cstr(acc.get("phone")).strip()
        same = normalize_msisdn(prefix) == expected
    else:
        expected = cstr(acc.get("shortcode")).strip()
        same = prefix == expected
    return None if same else f"number {prefix}, not {expected}"


def record_receiver_name(doctype, account, received_name, payout, notify=None) -> bool | None:
    """After a Paid result: remember who M-Pesa paid and raise the alarm (the account is blocked)
    if it is not the number and name on file. Returns whether they differed (None: nothing
    checked, e.g. a till, whose result names no one).

    A bank account is checked against its bank's row: the number and the bank's name as Safaricom
    shows it. While the row has no such name yet, only the number is checked, and the name seen is
    kept on the row for an M-Pesa Manager to confirm."""
    if not account or not received_name:
        return None
    p = profile(doctype)
    acc = get_account(doctype, account)
    if not acc:
        return None
    expected, check_name = acc.expected_name, True
    if is_bank(acc) and acc.get("bank_paybill"):
        row = frappe.db.get_value(BANK_PAYBILL, acc.bank_paybill, BANK_FIELDS, as_dict=True)
        if row:
            expected = cstr(row.receiver_name).strip()
            if not expected:
                check_name = False
                _observe(row, received_name)
    wrong_number = number_problem(acc, received_name)
    mismatch = bool(wrong_number) or (check_name and not names_match(expected, received_name))
    frappe.db.set_value(
        p.doctype,
        acc.name,
        {"last_receiver_name": received_name, "last_paid_on": now_datetime(), "name_mismatch": 1 if mismatch else 0},
    )
    if not mismatch:
        return False
    if payout.doctype == "Mpesa B2B Payment Request":
        payout.db_set("receiver_name_mismatch", 1)
    if wrong_number:
        message = (f"M-Pesa paid {received_name!r} ({wrong_number} on {p.doctype} {acc.name}). "
                   f"Check with the {p.label} before paying this account again.")
    else:
        message = (
            f"M-Pesa paid {received_name!r}, but {p.doctype} {acc.name} expects "
            f"{expected!r}. Check with the {p.label} before paying this account again."
        )
    payout.add_comment("Comment", message)
    frappe.log_error(title="M-Pesa receiver name mismatch", message=f"{payout.name}: {message}",
                     reference_doctype=payout.doctype, reference_name=payout.name)
    (notify or alert)(message, payout)
    return True


def _observe(row, received_name) -> None:
    """A bank row with no Name M-Pesa Shows: keep what M-Pesa showed, and ask CONFIG roles to
    confirm it (once per new name)."""
    seen = without_number(received_name)
    if not seen or seen == cstr(row.observed_receiver_name).strip():
        return
    frappe.db.set_value(BANK_PAYBILL, row.name, "observed_receiver_name", seen)
    alert(f"M-Pesa shows {seen!r} for bank paybill {row.name} ({row.paybill}). If that is the bank, copy it into "
          "Name M-Pesa Shows on the bank paybill so that payments to it are checked against it.",
          frappe._dict(doctype=BANK_PAYBILL, name=row.name), roles=CONFIG_ROLES)


def confirm_receiver_name(doc, adopt_name: bool = False) -> None:
    """A manager checked with the party: unblock the account, optionally taking M-Pesa's name (a
    bank account's name is its bank's, kept on the bank paybill)."""
    p = profile(doc)
    require(ADMIN_ROLES)
    require_named_user()  # the note names who checked
    if not cint(doc.get("name_mismatch")):
        return
    values = {"name_mismatch": 0}
    note_text = f"Receiver name {doc.get('last_receiver_name')!r} confirmed with the {p.label} by {frappe.session.user}"
    if adopt_name and doc.get("last_receiver_name") and is_bank(doc) and doc.get("bank_paybill"):
        note_text += f"; the name M-Pesa shows for a bank is kept on M-Pesa Bank Paybill {doc.bank_paybill}"
    elif adopt_name and doc.get("last_receiver_name"):
        values["expected_name"] = without_number(doc.last_receiver_name)
        note_text += f"; expected name changed from {doc.expected_name!r} to {values['expected_name']!r}"
    frappe.db.set_value(p.doctype, doc.name, values)
    doc.add_comment("Comment", note_text + ".")


def alert(message, doc, roles=None, exclude=(), now: bool = False, rolled_back: bool = False,
          details: str | None = None) -> None:
    """A notification to the Notify Role (M-Pesa Suite Settings), or to the users holding one of
    ``roles``, but ``exclude`` (whoever the alert is about); the Notify Role's users when that
    leaves no one. Once this commits, as Frappe sends them; ``now``: made in this transaction (a
    migrate, whose queued alerts were never delivered); ``rolled_back``: queued straight away,
    for a refusal whose transaction is rolled back; ``details``: the email's body (HTML), when
    there is more to say than the subject (money_out.attention's daily list). Never raises."""
    try:
        from frappe.desk.doctype.notification_log.notification_log import (
            enqueue_create_notification,
            make_notification_logs,
        )

        notify_role = frappe.db.get_single_value(SUITE, "notify_role") or "Accounts Manager"
        skip = {"Administrator", "Guest", *(u for u in exclude or () if u)}
        users = _holders(roles or [notify_role], skip)
        if not users and roles:
            users = _holders([notify_role], skip)
        if not users:
            return
        content = {"type": "Alert", "subject": message, "document_type": doc.doctype, "document_name": doc.name}
        if details:
            content["email_content"] = details
        if now:
            make_notification_logs(frappe._dict(content), users)
        elif rolled_back:
            frappe.enqueue("frappe.desk.doctype.notification_log.notification_log.make_notification_logs",
                           doc=frappe._dict(content), users=users)
        else:
            enqueue_create_notification(users, content)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "M-Pesa alert failed")


def _holders(roles, skip) -> list:
    users = frappe.get_all("Has Role", filters={"role": ["in", list(roles)], "parenttype": "User"}, pluck="parent",
                           distinct=True)
    return sorted({u for u in users if u not in skip})


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


# ---------------------------------------------------------------- migrate
SELF_APPROVED = ("Approved by the person who entered it, before the second-person rule applied to every login: "
                 "cancel and amend it, and have another M-Pesa Manager approve it.")
ADMIN_APPROVED = ("Approved with the shared Administrator login, which may no longer approve accounts (M-Pesa Suite "
                  "Settings): cancel and amend it, and have a named M-Pesa Manager approve it.")


def after_migrate() -> None:
    """Idempotent, every migrate: the Employee type's old label renamed, the bank paybill of bank
    accounts saved before the directory linked by their paybill, the destination key of accounts
    saved before it existed (the uniqueness check reads it), and accounts approved without a second
    named person blocked (``block_unchecked_approvals``)."""
    for p in PROFILES.values():
        if not frappe.db.exists("DocType", p.doctype) or not frappe.db.has_column(p.doctype, "destination_key"):
            continue
        frappe.db.sql(f"UPDATE `tab{p.doctype}` SET account_type = %s WHERE account_type = %s", (BANK, OLD_BANK))
        if frappe.db.exists("DocType", BANK_PAYBILL):
            for acc in frappe.get_all(p.doctype, filters={"account_type": BANK, "bank_paybill": ["is", "not set"]},
                                      fields=["name", "shortcode"]):
                row = frappe.db.get_value(BANK_PAYBILL, {"paybill": cstr(acc.shortcode).strip()}, "name")
                if row:
                    frappe.db.set_value(p.doctype, acc.name, "bank_paybill", row, update_modified=False)
        for acc in frappe.get_all(p.doctype, filters={"destination_key": ["is", "not set"]},
                                  fields=["name", "account_type", "phone", "shortcode", "account_number"]):
            key = destination_key(acc)
            if key:
                frappe.db.set_value(p.doctype, acc.name, "destination_key", key, update_modified=False)
    block_unchecked_approvals()


def _refuses_administrator() -> bool:
    """The Suite setting itself (permissions.administrator_refused is never true while migrating)."""
    try:
        value = frappe.db.get_single_value(SUITE, "refuse_administrator_payouts")
    except Exception:
        return True
    return True if value in (None, "") else bool(cint(value))


def unchecked_approval(acc, refuse_administrator: bool = True) -> str | None:
    """Why an approved account never had a second named person, or None: approved by who created
    it or entered its details (before round 4 the shared Administrator login was exempt from that
    rule), or with the shared Administrator login while M-Pesa Suite Settings refuses it."""
    approver = cstr(acc.get("approved_by")).strip()
    if not approver:
        return None  # approved before approvals were recorded: nothing to go on
    if approver.lower() in {cstr(acc.get(f)).strip().lower() for f in ("owner", "entered_by")} - {""}:
        return SELF_APPROVED
    if approver == "Administrator" and refuse_administrator:
        return ADMIN_APPROVED
    return None


def block_unchecked_approvals() -> dict:
    """after_migrate: an approved account whose approval had no second named person (handover F3:
    entered and approved by Administrator) is blocked (needs_reapproval) until it is cancelled,
    amended and approved again; payouts are refused meanwhile. Once per account: one already
    blocked is left as it is. One alert per DocType with a count (employee accounts: no names).
    Returns {DocType: how many were blocked now}."""
    refuse_administrator = _refuses_administrator()
    blocked_now = {}
    for p in PROFILES.values():
        if not frappe.db.exists("DocType", p.doctype) or not all(
                frappe.db.has_column(p.doctype, f) for f in ("approved_by", "needs_reapproval", "reapproval_reason")):
            continue
        fields = ["name", "owner", "approved_by"] + (["entered_by"] if frappe.db.has_column(p.doctype, "entered_by")
                                                     else [])
        count = 0
        for acc in frappe.get_all(p.doctype, filters={"docstatus": 1, "needs_reapproval": 0}, fields=fields):
            reason = unchecked_approval(acc, refuse_administrator)
            if not reason:
                continue
            frappe.db.set_value(p.doctype, acc.name, {"needs_reapproval": 1, "reapproval_reason": reason},
                                update_modified=False)
            note(p.doctype, acc.name, f"Blocked by the M-Pesa Suite upgrade: {reason}")
            count += 1
        blocked_now[p.doctype] = count
        if count:
            many = count > 1
            # made now (R4.1): one queued after the migrate's commit was never delivered on Trident
            alert(f"M-Pesa Suite upgrade: {count} {p.doctype}{'s' if many else ''} approved without a second named "
                  f"person {'are' if many else 'is'} blocked until cancelled, amended and approved again by an M-Pesa "
                  "Manager.", frappe._dict(doctype=p.doctype, name=None), now=True)
    return blocked_now
