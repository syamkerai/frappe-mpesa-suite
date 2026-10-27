"""Send via M-Pesa / Approve: the one deliberate step that sends a payout or approves a payee
account, in place of Submit (the user's decision 2, 2026-10-27).

Submitting a payout sends money, and Frappe offers many ways to submit: the form's Submit, the
list's Actions > Submit (bulk), REST, ``frappe.client.submit`` and ``run_doc_method``. So every
payout's before_submit (B2B request, B2C Payment Disbursement, B2C Disbursement Request,
Reversal) and every payee account's approval call ``assert_deliberate``, which refuses all of
them but the two server flows that are the deliberate step:
- ``prepare_send(doctype, name)``: checks that the session user may send (or approve) the
  document now (a named login, the roles, submit permission on the document, a saved draft) and
  returns what the confirmation shows (receiver and the approved account's expected name;
  destination: phone, paybill and account, till, or bank and account; the amount, per row and in
  total; the charge estimate where the app knows it; who prepared it and whether maker-checker
  would refuse this user) with a token. The token is bound to the user, the DocType, the name,
  the document's ``modified``, its amounts and its destination. It can be used from 5 seconds to
  10 minutes after it is issued, and once (the browser keeps Confirm disabled for those 5
  seconds too), like Pay Selected's (money_out.schedule).
- ``confirm_send(doctype, name, token)``: checks everything again under the document's row lock,
  takes the token, compares what it is bound to, and submits (or approves) the document inside
  ``deliberate``.
- ``deliberate(reason, doc)``: for server code that is itself the deliberate step (Pay Selected's
  confirm, the jobs that make a sent disbursement's B2C requests): documents submitted inside
  pass ``assert_deliberate``. With ``doc``, only that one.

Installing, migrating and patches pass. Bench tests send with ``send_for_test(doc)``: prepare and
confirm as the session user, without the 5-second wait, in tests only.
"""

import contextlib
import hashlib
import json
import time

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt

from ..compat import get_cache, in_test
from ..permissions import ADMIN_ROLES, PAYOUT_ROLES, require, require_named_user
from ..utils.phone import mask

B2B = "Mpesa B2B Payment Request"
B2C = "B2C Payment Disbursement"
B2C_REQUEST = "B2C Disbursement Request"
REVERSAL = "Mpesa Reversal Request"
SUPPLIER_ACCOUNT = "Supplier M-Pesa Account"
EMPLOYEE_ACCOUNT = "Employee M-Pesa Account"
PAYOUTS = (B2B, B2C, B2C_REQUEST, REVERSAL)
ACCOUNTS = (SUPPLIER_ACCOUNT, EMPLOYEE_ACCOUNT)
SEND, APPROVE = "Send via M-Pesa", "Approve"
# Confirm works this many seconds after the summary was made, for this long (as Pay Selected).
CONFIRM_DELAY = 5
TOKEN_SECONDS = 600
# frappe.flags key: the deliberate steps running now, as [(reason, (doctype, name) or None)]
FLAG = "mpsa_deliberate"
# The party's name on its own record
PARTY_NAME = {"Supplier": "supplier_name", "Employee": "employee_name", "Customer": "customer_name"}
# The documents a B2B request may pay, in the order the summary names them
B2B_DOCUMENTS = (("purchase_invoice", "Purchase Invoice"), ("purchase_order", "Purchase Order"),
                 ("salary_slip", "Salary Slip"), ("expense_claim", "Expense Claim"),
                 ("employee_advance", "Employee Advance"))
# What a confirm refuses when it changed since the summary
CHANGED = {"modified": "it was saved again", "amount": "the amount", "destination": "who is paid or where"}


def _clock() -> float:
    """Seconds, for the confirm delay (replaced in tests)."""
    return time.time()


def _token_key(token: str) -> str:
    return f"mpesa_suite:send_token:{token}"


def action(doctype: str) -> str:
    """The button's name: Approve for a payee account, Send via M-Pesa for a payout."""
    return APPROVE if doctype in ACCOUNTS else SEND


# ---------------------------------------------------------------- the deliberate step
@contextlib.contextmanager
def deliberate(reason: str, doc=None):
    """Documents submitted inside this block pass ``assert_deliberate``: ``reason`` names the server
    flow that is itself the deliberate step. With ``doc``, only that document passes."""
    previous = frappe.flags.get(FLAG)
    frappe.flags[FLAG] = [*(previous or []), (reason, (doc.doctype, doc.name) if doc is not None else None)]
    try:
        yield
    finally:
        frappe.flags[FLAG] = previous


def assert_deliberate(doc) -> None:
    """Throw unless ``doc`` is being sent (or approved) by ``confirm_send`` or inside
    ``deliberate``: a submit from the list, the API or a script does not send money."""
    if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch:
        return
    key = (doc.doctype, doc.name)
    if any(bound in (None, key) for _reason, bound in frappe.flags.get(FLAG) or ()):
        return
    button = action(doc.doctype)
    if doc.doctype in ACCOUNTS:
        message = _("{0} is approved with the Approve button on its form, which shows the account first. "
                    "Submitting it any other way (the list's Submit, the API, a script) is refused.")
    else:
        message = _("{0} is sent with the Send via M-Pesa button on its form, which shows who is paid, where and "
                    "how much first. Submitting it any other way (the list's Submit, the API, a script) is "
                    "refused and sends nothing.")
    frappe.throw(message.format(doc.name), title=_("Press {0}").format(_(button)))


def _sendable(doctype: str, name: str, for_update: bool = False):
    """The saved draft ``name`` the session user may send (or approve) now: a named login, the
    roles (ADMIN_ROLES approve payee accounts, PAYOUT_ROLES send payouts) and submit permission
    on the document itself."""
    if doctype not in PAYOUTS + ACCOUNTS:
        frappe.throw(_("{0} is not sent with Send via M-Pesa.").format(doctype))
    require_named_user()
    require(ADMIN_ROLES if doctype in ACCOUNTS else PAYOUT_ROLES)
    doc = frappe.get_doc(doctype, name, for_update=True) if for_update else frappe.get_doc(doctype, name)
    doc.check_permission("submit")
    if cint(doc.docstatus) != 0:
        done = _("approved") if doctype in ACCOUNTS else _("sent")
        frappe.throw(_("{0} is {1} already.").format(name, done if cint(doc.docstatus) == 1 else _("cancelled")))
    return doc


@frappe.whitelist(methods=["POST"])
def prepare_send(doctype: str, name: str, modified: str | None = None) -> dict:
    """What the Send via M-Pesa (or Approve) confirmation shows, with its token. ``modified``: the
    version the form shows; a document saved since is refused (reload it first)."""
    doc = _sendable(doctype, name)
    if modified and cstr(modified) != cstr(doc.modified):
        frappe.throw(_("{0} was changed since you opened it. Reload it and press {1} again.")
                     .format(name, _(action(doctype))))
    summary = _summary(doc)
    token = None
    if not summary["problems"]:
        token = frappe.generate_hash(length=24)
        get_cache().set_value(_token_key(token), dict(_binding(doc), user=frappe.session.user, issued_at=_clock()),
                              expires_in_sec=TOKEN_SECONDS)
    return dict(summary, token=token, delay=CONFIRM_DELAY, expires_in=TOKEN_SECONDS)


@frappe.whitelist(methods=["POST"])
def confirm_send(doctype: str, name: str, token: str) -> dict:
    """Send (or approve) ``name`` with the token ``prepare_send`` gave this user: once, no sooner
    than 5 seconds after it was made, and only if nothing it is bound to has changed."""
    token = cstr(token).strip()
    if not token:
        frappe.throw(_("Press {0} first.").format(_(action(doctype))))
    # the row lock first: a second confirm with the same token waits here, then finds it used
    doc = _sendable(doctype, name, for_update=True)
    user, button = frappe.session.user, _(action(doctype))
    data = get_cache().get_value(_token_key(token))
    if not data:
        frappe.throw(_("This confirmation has expired or was used already. Press {0} again.").format(button))
    if data.get("user") != user:
        frappe.throw(_("This confirmation was made for another user. Press {0} yourself.").format(button))
    if (data.get("doctype"), data.get("name")) != (doctype, name):
        frappe.throw(_("This confirmation is for {0}, not {1}. Press {2} again.").format(data.get("name"), name,
                                                                                         button))
    if _clock() - flt(data.get("issued_at")) < CONFIRM_DELAY:
        frappe.throw(_("Read the summary first: Confirm works {0} seconds after it opens.").format(CONFIRM_DELAY))
    get_cache().delete_value(_token_key(token))  # used once, whatever happens next
    now = _binding(doc)
    changed = [_(text) for what, text in CHANGED.items() if cstr(now[what]) != cstr(data.get(what))]
    if changed:
        frappe.throw(_("{0} changed after you pressed {1} ({2}). Reload it and press {1} again.")
                     .format(name, button, ", ".join(changed)))
    with deliberate(action(doctype), doc):
        doc.submit()
    return {"name": doc.name, "docstatus": cint(doc.docstatus), "status": doc.get("status")}


def send_for_test(doc):
    """Bench tests: press Send via M-Pesa (or Approve) and Confirm as the session user, without
    the 5-second wait. ``doc``: a saved draft (what is in the database is sent). Returns the
    document as saved. Refused outside tests."""
    if not in_test():
        frappe.throw("send_for_test is for tests only.")
    summary = prepare_send(doc.doctype, doc.name)
    if not summary["token"]:
        frappe.throw(" ".join(summary["problems"]))
    key = _token_key(summary["token"])
    data = get_cache().get_value(key)
    data["issued_at"] = flt(data.get("issued_at")) - CONFIRM_DELAY  # as if the summary was read
    get_cache().set_value(key, data, expires_in_sec=TOKEN_SECONDS)
    confirm_send(doc.doctype, doc.name, summary["token"])
    return frappe.get_doc(doc.doctype, doc.name)


# ---------------------------------------------------------------- what the token is bound to
def _fingerprint(values) -> str:
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()


def _amounts(doc) -> list:
    if doc.doctype == B2C:
        return [flt(row.get("allocated_amount"), 2) for row in doc.get("references") or []]
    return [flt(doc.get("amount"), 2)] if doc.doctype in PAYOUTS else []


def _destination(doc) -> list:
    """Who is paid, from where and to where, for which document: maker-checker's fields of the
    payout (maker_checker.PAID), the destination fields of a payee account."""
    from . import maker_checker, party_accounts

    if doc.doctype in ACCOUNTS:
        return [cstr(doc.get(f)).strip() for f in party_accounts.profile(doc).destination_fields]
    fields, table = maker_checker.PAID[doc.doctype]
    out = [cstr(doc.get(f)).strip() for f in fields if f != "amount"]
    if table:
        out += [[cstr(row.get(f)).strip() for f in table[1] if f != "allocated_amount"]
                for row in doc.get(table[0]) or []]
    return out


def _binding(doc) -> dict:
    return {"doctype": doc.doctype, "name": doc.name, "modified": cstr(doc.modified),
            "amount": _fingerprint(_amounts(doc)), "destination": _fingerprint(_destination(doc))}


# ---------------------------------------------------------------- the summary
def _party_name(party_type, party) -> str:
    field = PARTY_NAME.get(cstr(party_type))
    found = frappe.db.get_value(party_type, party, field) if field and party else None
    return cstr(found or party or "")


def _shortcode(settings) -> str:
    return cstr(frappe.db.get_value("Mpesa Settings", settings, "business_shortcode")) if settings else ""


def _b2c_charge(amount) -> float | None:
    """The B2C Charge Bands estimate (M-Pesa Suite Settings), or None when none are set."""
    from .statement import band_charge, charge_bands

    try:
        return band_charge(charge_bands(), amount)
    except Exception:
        return None


def _row(receiver, destination, amount=None, charge=None, document=None, party=None, expected_name=None) -> dict:
    return {"receiver": receiver, "party": party, "expected_name": expected_name, "destination": destination,
            "amount": flt(amount, 2) if amount is not None else None,
            "charge": flt(charge, 2) if charge is not None else None, "document": document}


def _b2b_rows(doc) -> list:
    from ..daraja import money_out as daraja

    if cstr(doc.get("employee")).strip():
        party_type, party, account_doctype = "Employee", doc.employee, EMPLOYEE_ACCOUNT
        account = doc.get("employee_mpesa_account")
    elif cstr(doc.get("supplier")).strip():
        party_type, party, account_doctype = "Supplier", doc.supplier, SUPPLIER_ACCOUNT
        account = doc.get("supplier_mpesa_account")
    else:
        party_type = party = account_doctype = account = None
    fields = ["expected_name"] + (["bank"] if account_doctype == EMPLOYEE_ACCOUNT else [])
    acc = frappe.db.get_value(account_doctype, account, fields, as_dict=True) if account else None
    shortcode, number = cstr(doc.get("receiver_shortcode")).strip(), cstr(doc.get("account_reference")).strip()
    if doc.get("command_id") == daraja.B2B_BUY_GOODS:
        destination = _("Till {0}").format(shortcode)
    elif acc and acc.get("bank"):
        destination = _("{0} (bank paybill {1}), account {2}").format(acc.bank, shortcode, number)
    else:
        destination = _("Paybill {0}, account {1}").format(shortcode, number)
    if not account:
        destination += " " + _("(a number not saved on a supplier)")
    document = next((f"{_(dt)} {doc.get(f)}" for f, dt in B2B_DOCUMENTS if cstr(doc.get(f)).strip()), None)
    receiver = _party_name(party_type, party) if party else _("Not saved on a supplier")
    return [_row(receiver, destination, doc.get("amount"), None, document, party,
                 acc.get("expected_name") if acc else None)]


def _b2c_rows(doc) -> list:
    rows = doc._rows_to_send() if callable(getattr(doc, "_rows_to_send", None)) else doc.get("references") or []
    out = []
    for row in rows:
        party_type = row.get("party_type") or doc.get("party_type")
        document = f"{_(row.reference_doctype)} {row.reference_name}" if row.get("reference_name") else None
        amount = row.get("allocated_amount")
        phone = _("M-Pesa phone {0}").format(mask(row.get("partyb")))
        out.append(_row(_party_name(party_type, row.get("party")), phone, amount, _b2c_charge(amount), document,
                        row.get("party")))
    return out


def _b2c_request_rows(doc) -> list:
    document = f"{_(doc.reference_doctype)} {doc.reference_name}" if doc.get("reference_name") else None
    return [_row(_party_name(doc.get("party_type"), doc.get("party")) or _("Not named"),
                 _("M-Pesa phone {0}").format(mask(doc.get("phone_number"))), doc.get("amount"),
                 _b2c_charge(doc.get("amount")), document, doc.get("party"))]


def _reversal_rows(doc) -> list:
    register = doc.get("c2b_register")
    payer = cstr(frappe.db.get_value("Mpesa C2B Payment Register", register, "full_name") if register else "")
    destination = _("Back to whoever paid receipt {0}").format(doc.get("transaction_id"))
    return [_row(payer or _("The customer who paid"), destination, doc.get("amount"), None,
                 f"{_('Mpesa C2B Payment Register')} {register}" if register else None)]


def _account_rows(doc) -> list:
    from . import party_accounts as pa

    p = pa.profile(doc)
    party = doc.get(p.party_field)
    if doc.get("account_type") == pa.PHONE:
        destination = _("M-Pesa phone {0}").format(mask(doc.get("phone")))
    elif doc.get("account_type") == pa.TILL:
        destination = _("Till {0}").format(doc.get("shortcode"))
    elif doc.get("bank"):
        destination = _("{0} (bank paybill {1}), account {2}").format(doc.bank, doc.get("shortcode"),
                                                                      doc.get("account_number"))
    else:
        destination = _("Paybill {0}, account {1}").format(doc.get("shortcode"), doc.get("account_number"))
    return [_row(_party_name(p.party_type, party), destination, party=party, expected_name=doc.get("expected_name"),
                 document=_(doc.get("account_type") or ""))]


ROWS = {B2B: _b2b_rows, B2C: _b2c_rows, B2C_REQUEST: _b2c_request_rows, REVERSAL: _reversal_rows,
        SUPPLIER_ACCOUNT: _account_rows, EMPLOYEE_ACCOUNT: _account_rows}


def _problems(doc) -> list:
    """Why this user cannot send (or approve) the document as saved, said before anything else:
    what its submit would refuse first."""
    from . import maker_checker, party_accounts

    user = frappe.session.user
    if doc.doctype in ACCOUNTS:
        p = party_accounts.profile(doc)
        problems = []
        if user in {doc.owner, doc.get("entered_by")} - {None, ""}:
            problems.append(_("A different person must approve: you entered or changed the details of this M-Pesa "
                              "account."))
        if user == party_accounts.own_user(p.doctype, doc.get(p.party_field)):
            problems.append(_("This is your own M-Pesa account: another manager must approve it."))
        if not doc.get("proof"):
            problems.append(_(p.proof))
        return problems
    if cstr(doc.get("schedule_line")).strip() or cstr(doc.get("payment_schedule")).strip():
        return [_("A scheduled payment is paid with Pay Selected in M-Pesa Payments Due, not by hand.")]
    problem = maker_checker.preview(doc, user)
    return [problem] if problem else []


def _summary(doc) -> dict:
    rows = ROWS[doc.doctype](doc)
    amounts = [r["amount"] for r in rows if r["amount"] is not None]
    charges = [r["charge"] for r in rows if r["amount"] is not None]
    known = bool(charges) and all(c is not None for c in charges)
    return {
        "doctype": doc.doctype, "name": doc.name, "action": action(doc.doctype),
        "rows": rows, "total": flt(sum(amounts), 2) if amounts else None,
        "charge": flt(sum(charges), 2) if known else None,
        "charge_note": None if known or not amounts else (
            _("M-Pesa states the charge after it pays.") if doc.doctype in (B2B, REVERSAL)
            else _("No B2C Charge Bands are set (M-Pesa Suite Settings).")),
        "pay_from": doc.get("mpesa_settings") or doc.get("mode_of_payment"),
        "shortcode": _shortcode(doc.get("mpesa_settings")),
        "prepared_by": doc.owner, "changed_by": doc.get("modified_by"), "entered_by": doc.get("entered_by"),
        "proof": doc.get("proof"), "modified": cstr(doc.modified),
        "problems": _problems(doc),
    }
