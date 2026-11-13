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
- ``confirm_send(doctype, name, token, acknowledged)``: checks everything again under the document's
  row lock, takes the token, compares what it is bound to, and submits (or approves) the document
  inside ``deliberate``.
- Round 4.5, the M-Pesa names (money_out.known_names): the summary's ``known_names`` gives each row the
  name M-Pesa gave its phone, till or paybill before, the typed or expected name, and the verdict
  (match, differs, new), with the counts in a header. A payout with a row that differs or is new is
  sent only when the sender ticked the acknowledgement it states: the dialog keeps Confirm off until
  then, and ``confirm_send`` refuses without ``acknowledged`` (the names read again at the confirm).
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
from ..utils.privacy import phone as shown_phone

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
# An Expense Claim's payee line (money_out.claim_payees): a merchant or casual payee paid straight from the claim
CLAIM_PAYEE = "M-Pesa Claim Payee"
PAYEE_FIELDS = ("name", "idx", "parent", "parenttype", "payee_type", "payee_name", "bank_paybill", "shortcode",
                "account_number", "phone", "amount", "destination_key", "typed_name")
# Round 4.5: the payouts whose rows' M-Pesa names must be acknowledged when one differs or is new
# (a payee account's Approve shows the names but asks nothing)
NAMED = (B2B, B2C, B2C_REQUEST)
# A row's (destination key, typed or expected name) while the summary is built; never shown
KNOWN = "_known"


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
def confirm_send(doctype: str, name: str, token: str, acknowledged=None) -> dict:
    """Send (or approve) ``name`` with the token ``prepare_send`` gave this user: once, no sooner
    than 5 seconds after it was made, and only if nothing it is bound to has changed. A payout with
    a row whose M-Pesa name differs or is new needs ``acknowledged`` (round 4.5: the summary's
    ``known_names.acknowledge``, ticked)."""
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
    _check_acknowledged(doc, acknowledged)
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
    # the M-Pesa names' box ticked, as a sender does (round 4.5)
    confirm_send(doc.doctype, doc.name, summary["token"], acknowledged=1)
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


def _row(receiver, destination, amount=None, charge=None, document=None, party=None, expected_name=None,
         account_holder=None) -> dict:
    return {"receiver": receiver, "party": party, "expected_name": expected_name, "destination": destination,
            "account_holder": cstr(account_holder).strip() or None,
            "amount": flt(amount, 2) if amount is not None else None,
            "charge": flt(charge, 2) if charge is not None else None, "document": document}


def _bank_destination(acc, shortcode, number) -> str | None:
    """A bank account paid through its bank's paybill, named by its bank (the M-Pesa Bank Paybill
    row's name, else the ERPNext Bank it links); None for any other destination. Supplier and
    Employee M-Pesa Accounts both hold this type (round 4)."""
    from . import party_accounts as pa

    if not acc or not (acc.get("account_type") == pa.BANK or acc.get("bank") or acc.get("bank_paybill")):
        return None
    paybill = cstr(acc.get("bank_paybill")).strip()
    bank = frappe.db.get_value(pa.BANK_PAYBILL, paybill, "institution_name") if paybill else None
    return _("{0} (bank paybill {1}), account {2}").format(cstr(bank or acc.get("bank")).strip() or _("Bank"),
                                                          shortcode, number)


def _payee_lines(names) -> dict:
    """{name: line} of the Expense Claim payee lines (M-Pesa Claim Payee) ``names``, each with
    ``claimant`` (the claim's employee name) and ``approved_account`` (a supplier's approved Supplier
    M-Pesa Account with the line's destination, which the payee caps skip; claim_payees)."""
    from . import claim_payees

    wanted = sorted({cstr(n).strip() for n in names if cstr(n).strip()})
    if not wanted:
        return {}
    lines = {r.name: r for r in frappe.get_all(CLAIM_PAYEE, filters={"name": ["in", wanted]},
                                               fields=list(PAYEE_FIELDS))}
    claimants = {}
    for line in lines.values():
        if line.parent not in claimants:
            found = frappe.db.get_value("Expense Claim", line.parent, ["employee", "employee_name"], as_dict=True)
            claimants[line.parent] = cstr((found or {}).get("employee_name") or (found or {}).get("employee")).strip()
        line.claimant = claimants[line.parent]
        line.approved_account = claim_payees.approved_supplier_account(line)
    return lines


def _payee_row(line, destination, amount, charge=None, key=None) -> dict:
    """A payout of an Expense Claim's payee line: the receiver is the payee the claim names (the
    claimant is only who is reimbursed in the books), and the destination says it is no approved
    account unless it is a supplier's. ``key``: where the payout pays (known_names), named against
    the name the claimant gave (Name Given, else the Payee Name)."""
    expected = None
    if line.approved_account:
        expected = frappe.db.get_value(SUPPLIER_ACCOUNT, line.approved_account, "expected_name")
        destination += " " + _("(a supplier's approved account, {0})").format(line.approved_account)
    else:
        destination += " " + _("(payee of Expense Claim {0}, not an approved account)").format(line.parent)
    party = _("payee on {0}, claimant {1}").format(line.parent, line.claimant or _("not named"))
    document = _("Expense Claim {0}, payee row #{1}").format(line.parent, line.idx)
    row = _row(cstr(line.payee_name).strip() or _("Not named"), destination, amount, charge, document, party,
               expected)
    return _named(row, key, expected or line.get("typed_name") or line.payee_name)


def _named(row: dict, key, typed) -> dict:
    """The row with where it pays and the name it is paid under, for the M-Pesa names (round 4.5);
    ``_summary`` takes them off the row."""
    row[KNOWN] = (key, cstr(typed).strip() or None) if key else None
    return row


def _phone_key(phone) -> str | None:
    from . import known_names

    return known_names.key_of(known_names.PHONE, phone)


def _b2b_key(doc) -> str | None:
    from . import known_names

    return known_names.key_of(known_names.b2b_kind(doc.get("command_id")), doc.get("receiver_shortcode"))


def _b2b_payee_destination(doc, line) -> str:
    """Where a B2B request for a payee line pays, as sent (claim_payees.check_b2b keeps it the line's)."""
    from ..daraja import money_out as daraja
    from . import party_accounts as pa

    shortcode, number = cstr(doc.get("receiver_shortcode")).strip(), cstr(doc.get("account_reference")).strip()
    if doc.get("command_id") == daraja.B2B_BUY_GOODS:
        return _("Till {0}").format(shortcode)
    bank = _bank_destination(frappe._dict(account_type=pa.BANK, bank_paybill=line.get("bank_paybill")),
                             shortcode, number) if line.get("payee_type") == pa.BANK else None
    return bank or _("Paybill {0}, account {1}").format(shortcode, number)


def _b2b_rows(doc) -> list:
    from ..daraja import money_out as daraja

    payee = cstr(doc.get("claim_payee")).strip()
    line = _payee_lines([payee]).get(payee) if payee else None
    if line:
        return [_payee_row(line, _b2b_payee_destination(doc, line), doc.get("amount"), key=_b2b_key(doc))]
    if cstr(doc.get("employee")).strip():
        party_type, party, account_doctype = "Employee", doc.employee, EMPLOYEE_ACCOUNT
        account = doc.get("employee_mpesa_account")
    elif cstr(doc.get("supplier")).strip():
        party_type, party, account_doctype = "Supplier", doc.supplier, SUPPLIER_ACCOUNT
        account = doc.get("supplier_mpesa_account")
    else:
        party_type = party = account_doctype = account = None
    fields = ["expected_name", "account_type", "bank_paybill", "bank", "account_holder_name"]
    acc = frappe.db.get_value(account_doctype, account, fields, as_dict=True) if account else None
    shortcode, number = cstr(doc.get("receiver_shortcode")).strip(), cstr(doc.get("account_reference")).strip()
    if doc.get("command_id") == daraja.B2B_BUY_GOODS:
        destination = _("Till {0}").format(shortcode)
    else:
        destination = _bank_destination(acc, shortcode, number) or _("Paybill {0}, account {1}").format(shortcode,
                                                                                                     number)
    if not account:
        destination += " " + _("(a number not saved on a supplier)")
    document = next((f"{_(dt)} {doc.get(f)}" for f, dt in B2B_DOCUMENTS if cstr(doc.get(f)).strip()), None)
    receiver = _party_name(party_type, party) if party else _("Not saved on a supplier")
    expected = acc.get("expected_name") if acc else None
    row = _row(receiver, destination, doc.get("amount"), None, document, party, expected,
               acc.get("account_holder_name") if acc else None)
    # the name M-Pesa should show: the approved account's, else the party's (a number on no supplier: none)
    return [_named(row, _b2b_key(doc), expected or (receiver if party else None))]


def _b2c_send_rows(doc) -> list:
    return doc._rows_to_send() if callable(getattr(doc, "_rows_to_send", None)) else doc.get("references") or []


def _b2c_rows(doc) -> list:
    from . import partyless

    rows = _b2c_send_rows(doc)
    payees = _payee_lines(row.get("claim_payee") for row in rows)
    out = []
    for row in rows:
        party_type = row.get("party_type") or doc.get("party_type")
        document = f"{_(row.reference_doctype)} {row.reference_name}" if row.get("reference_name") else None
        amount = row.get("allocated_amount")
        phone = _("M-Pesa phone {0}").format(shown_phone(row.get("partyb")))
        key = _phone_key(row.get("partyb"))
        line = payees.get(cstr(row.get("claim_payee")).strip())
        if line:  # a casual payee of a claim: the row's party is the claimant, who is not who is paid
            out.append(_payee_row(line, phone, amount, _b2c_charge(amount), key=key))
            continue
        if partyless.handler_for(row.get("reference_doctype")):
            # a row that pays a person no party names: who is paid is what its document says
            found = partyless.describe(row)
            receiver = cstr(found.get("receiver")).strip()
            out.append(_named(_row(receiver or _("Not named"), phone, amount, _b2c_charge(amount),
                                   found.get("document") or document, found.get("party"), found.get("expected_name")),
                              key, found.get("expected_name") or receiver))
            continue
        receiver = _party_name(party_type, row.get("party"))
        out.append(_named(_row(receiver, phone, amount, _b2c_charge(amount), document, row.get("party")), key,
                          _account_name(row.get("payee_account_doctype"), row.get("payee_account")) or receiver))
    return out


def _account_name(doctype, account) -> str | None:
    """The name M-Pesa shows for an approved payee account a row is locked to (its Expected Name)."""
    if doctype not in ACCOUNTS or not cstr(account).strip():
        return None
    return cstr(frappe.db.get_value(doctype, account, "expected_name")).strip() or None


def _b2c_request_rows(doc) -> list:
    document = f"{_(doc.reference_doctype)} {doc.reference_name}" if doc.get("reference_name") else None
    receiver = _party_name(doc.get("party_type"), doc.get("party"))
    row = _row(receiver or _("Not named"), _("M-Pesa phone {0}").format(shown_phone(doc.get("phone_number"))),
               doc.get("amount"), _b2c_charge(doc.get("amount")), document, doc.get("party"))
    return [_named(row, _phone_key(doc.get("phone_number")), receiver)]


def _reversal_rows(doc) -> list:
    register = doc.get("c2b_register")
    payer = cstr(frappe.db.get_value("Mpesa C2B Payment Register", register, "full_name") if register else "")
    destination = _("Back to whoever paid receipt {0}").format(doc.get("transaction_id"))
    return [_row(payer or _("The customer who paid"), destination, doc.get("amount"), None,
                 f"{_('Mpesa C2B Payment Register')} {register}" if register else None)]


def _account_rows(doc) -> list:
    from . import party_accounts as pa

    from . import known_names

    p = pa.profile(doc)
    party = doc.get(p.party_field)
    if doc.get("account_type") == pa.PHONE:
        destination = _("M-Pesa phone {0}").format(shown_phone(doc.get("phone")))
    elif doc.get("account_type") == pa.TILL:
        destination = _("Till {0}").format(doc.get("shortcode"))
    else:
        destination = _bank_destination(doc, doc.get("shortcode"), doc.get("account_number")) or _(
            "Paybill {0}, account {1}").format(doc.get("shortcode"), doc.get("account_number"))
    row = _row(_party_name(p.party_type, party), destination, party=party, expected_name=doc.get("expected_name"),
               document=_(doc.get("account_type") or ""), account_holder=doc.get("account_holder_name"))
    phone = doc.get("account_type") == pa.PHONE
    key = known_names.key_of(doc.get("account_type"), doc.get("phone") if phone else doc.get("shortcode"))
    return [_named(row, key, doc.get("expected_name"))]


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
    problems = ([problem] if problem else []) + _payer_problems(doc, user)
    if doc.doctype == B2C:
        from . import partyless

        # rows that pay people no party names: never the sender's phone or name (money_out.self_pay)
        own = partyless.self_pay_problem(doc, user, "send")
        problems += [own] if own else []
    return problems + _payee_problems(doc)


def _paid_employees(doc) -> set:
    """The employees a payout pays, or pays for (the claim's or advance's employee): who may not
    send it (employee_accounts.own_payment_problem), as its submit checks (b2b._check_payer,
    employee_documents.check_not_own_b2c and check_b2c_request)."""
    from . import employee_documents

    employees = set()
    if doc.doctype == B2B:
        employees.add(cstr(doc.get("employee")).strip())
    elif doc.doctype == B2C_REQUEST:
        if doc.get("party_type") == "Employee":
            employees.add(cstr(doc.get("party")).strip())
    elif doc.doctype == B2C:
        for row in _b2c_send_rows(doc):
            if row.get("party_type") == "Employee":
                employees.add(cstr(row.get("party")).strip())
            dt, dn = row.get("reference_doctype"), cstr(row.get("reference_name")).strip()
            if dt in employee_documents.DOCTYPES and dn:
                employees.add(cstr(frappe.db.get_value(dt, dn, "employee")).strip())
    return {e for e in employees if e}


def _payer_problems(doc, user) -> list:
    """What the send would refuse about the payer: their own payment, and a payment to an employee
    for no Salary Slip, Expense Claim or Employee Advance by someone who is no manager (a B2B
    request without one, or any B2C Disbursement Request made by hand: employee_documents)."""
    from . import b2b, employee_accounts, owed

    problems = [p for p in (employee_accounts.own_payment_problem(e, user) for e in sorted(_paid_employees(doc))) if p]
    if (doc.doctype == B2B and b2b.is_employee(doc) and not b2b.salary_slip(doc)
            and not owed.employee_document_of(doc)[0] and not employee_accounts.is_manager(user)):
        problems.append(_(b2b.NO_DOCUMENT))
    if (doc.doctype == B2C_REQUEST and doc.get("party_type") == "Employee" and cstr(doc.get("party")).strip()
            and not employee_accounts.is_manager(user)):
        problems.append(_(b2b.NO_DOCUMENT))
    return problems


def _payee_problems(doc) -> list:
    """What the send would refuse about the Expense Claim payee lines a payout pays (claim_payees,
    at the send): the switch off, and the payee caps on destinations that are no approved account
    (M-Pesa Suite Settings, 0 = no cap): the per-payment cap and the company's day."""
    from . import claim_payees

    if doc.doctype == B2B:
        paid = [(doc.get("claim_payee"), doc.get("amount"))]
    elif doc.doctype == B2C:
        paid = [(row.get("claim_payee"), row.get("allocated_amount")) for row in _b2c_send_rows(doc)]
    else:
        return []
    lines = _payee_lines(name for name, _amount in paid)
    if not lines:
        return []
    s = claim_payees.settings()
    if not s.allow:
        return [_("Paying merchants and payees from an Expense Claim is switched off (M-Pesa Suite Settings).")]
    problems, capped = [], 0.0
    for name, amount in paid:
        line = lines.get(cstr(name).strip())
        if not line or line.approved_account:
            continue
        problem = claim_payees.amount_cap_problem(frappe._dict(line, amount=amount), s)
        if problem:
            problems.append(f"{line.parent}: {problem}")
        capped += flt(amount)
    if doc.doctype == B2C:
        capped = claim_payees._capped_rows_total(doc)  # the send's own batch total (check_b2c_row)
    if s.daily_cap and capped:
        sent = claim_payees.sent_today(doc.get("company"), (doc.doctype, doc.name))
        if sent + capped > s.daily_cap + 0.001:
            problems.append(_("KES {0} was sent today to Expense Claim payees that are not approved accounts; with "
                              "this KES {1} it would pass the day's cap of KES {2} (M-Pesa Suite Settings).").format(
                claim_payees.money(sent), claim_payees.money(capped), claim_payees.money(s.daily_cap)))
    return problems


def _warnings(doc) -> list:
    """What the person approving should check, which does not stop them: a payee account that pays a
    third party's bank account, or an employee paid to a business paybill or till (the warnings
    party_accounts.validate_account shows when the account is saved); and a B2B payment, or a paybill
    or till account, to a number M-Pesa refused as no receiver and nothing has paid since
    (b2b.refused_warning: sending to it unchanged fails the same way)."""
    from . import b2b
    from . import party_accounts as pa

    if doc.doctype == B2B:
        refused = b2b.refused_warning(doc.get("receiver_shortcode"), doc.get("command_id"), exclude=doc.name)
        return [refused] if refused else []
    if doc.doctype == B2C:
        from . import partyless

        return partyless.send_warnings(doc)  # rows no party names: their document's (a short balance)
    if doc.doctype not in ACCOUNTS:
        return []
    p = pa.profile(doc)
    command = {pa.PAYBILL: "BusinessPayBill", pa.TILL: "BusinessBuyGoods"}.get(doc.get("account_type"))
    refused = b2b.refused_warning(doc.get("shortcode"), command) if command else None
    out = [refused] if refused else []
    if pa.is_bank(doc):
        name = pa.party_name(p, doc.get(p.party_field))
        holder = cstr(doc.get("account_holder_name")).strip()
        if name and holder and not pa.names_match(name, holder):
            out.append(_("The account holder {0} is not {1}: this pays a third party's account. Check the proof "
                         "before approving.").format(holder, name))
    elif doc.doctype == EMPLOYEE_ACCOUNT and doc.get("account_type") in (pa.PAYBILL, pa.TILL):
        out.append(_("This pays an employee to a business paybill or till: check that the proof shows the number "
                     "is the employee's."))
    return out


def _rows_and_names(doc) -> tuple:
    """(rows, known_names): the summary's rows and their M-Pesa names (known_names.summary: None when
    no row pays a phone, till or paybill). Only a payout's names ask for the acknowledgement."""
    from . import known_names

    rows = ROWS[doc.doctype](doc)
    targets = [row.pop(KNOWN, None) for row in rows]
    return rows, known_names.summary(targets, acknowledge=doc.doctype in NAMED)


def _ticked(value) -> bool:
    return cstr(value).strip().lower() in ("1", "true", "yes", "on")


def _check_acknowledged(doc, acknowledged) -> None:
    """Round 4.5: a payout with a row whose number M-Pesa knows under another name, or never paid, is
    sent only when the sender ticked that they checked them (the names read again now: a Result that
    came since counts)."""
    if doc.doctype not in NAMED or _ticked(acknowledged):
        return
    names = _rows_and_names(doc)[1]
    if names and names.get("acknowledge"):
        frappe.throw(_("{0} pays numbers whose M-Pesa names need checking ({1}). Tick \"{2}\" and press {3} again.")
                     .format(doc.name, names["header"], names["acknowledge"], _(action(doc.doctype))),
                     title=_("Check the M-Pesa names"))


def _summary(doc) -> dict:
    rows, names = _rows_and_names(doc)
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
        "problems": _problems(doc), "warnings": _warnings(doc),
        # round 4.5: each row's M-Pesa name, the counts, and what the sender acknowledges (rows by position)
        "known_names": names,
    }
