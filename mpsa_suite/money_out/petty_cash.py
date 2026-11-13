"""The petty cash phone: the company's own M-Pesa phone that pays supplier tills (round 4.5).

Some suppliers are paid only at their till (petrol stations, hardware shops). The normal way is the
company's petty cash phone: it is topped up by M-Pesa from the company's shortcode, and its holder
(the cash handler) pays the tills from it. Each till payment is recorded on its Purchase Order or
Purchase Invoice from the M-Pesa SMS the phone received.

The phone (DocType M-Pesa Petty Cash Phone, submittable):
- company, the number (2547..., one live phone per number), the name M-Pesa shows for it (Expected
  Name), the holder (an Employee and their User), the petty cash account (a Cash account of its own:
  one live phone per account, so the account's balance is the phone's book balance), a default cost
  center and a charges account, the top-up limit (the most one top-up may be) and the float ceiling
  (the most the phone should hold);
- approved with Approve (``approve``: submit) by a named M-Pesa Manager or Accounts Manager who did
  not enter it and does not hold it. Approved, the number, the account and the expected name are
  locked: changing them is a cancel and amend, approved again. Holder, cost center, charges account,
  limits and Enabled may be changed by who may write it (not the holder: ``check_update``);
- the name M-Pesa gives the number is checked on its first top-up (``check_name``): against the
  Expected Name, and later top-ups against that first name. A mismatch blocks top-ups until a
  manager confirms it (``confirm_name``).

Top-ups (``top_up``, preparer roles): one top-up row on the phone (M-Pesa Petty Cash Top Up) and one
draft party-less B2C Payment Disbursement paying it (money_out.partyless, this module's ``HANDLER``):
the amount defaults to the ceiling less the book balance and the top-ups on their way, and is
refused above the top-up limit or when the phone would pass its ceiling (checked again at the send).
An M-Pesa Payer sends it with Send via M-Pesa (maker-checker as in the settings). The phone's holder
neither prepares nor sends a top-up of their own phone. A Paid top-up is booked as one Journal Entry
(``Handler.book``): debit the petty cash account, credit the shortcode's Utility GL, the receipt as
Cheque No, so its bank statement line reconciles one to one (money_out.statement.entries_for).
Late Results, Transaction Status queries, Retry after a failure and the unbooked sweep are the B2C
payouts' own.

Spends (``record_spend``): Paid from Petty Cash Phone on a submitted Purchase Order or an unpaid
Purchase Invoice. The pasted SMS is read (``parse_sms``: receipt, amount, till / paybill / person,
date and time, the phone's new balance, the transaction cost); its fields may also be typed. It books
a submitted Payment Entry: Pay, to the supplier, from the petty cash account, Reference No the
receipt, Reference Date the SMS's date, against the order (an advance) or the invoice, a charge on
the charges account as a deduction. Refused when the receipt is on another entry, when the amount is
more than the document still owes (owed.left_to_pay) or than the phone's book balance, and for anyone
but the phone's holder and the Accounts roles. The name on the SMS is shown next to the supplier's.

Watching (``attention_section``, in the daily "needs attention" alert): for each phone, the book
balance against the balance on its last recorded SMS (``sms_status``: top-ups paid and spends recorded
after that SMS taken into account), and money left with no spend for ``petty_cash_alert_days``.
"""

import datetime
import re

import frappe
from frappe.utils import cint, cstr, flt, getdate, now_datetime, nowdate

from ..daraja.guard import B2C_MAX_AMOUNT, B2C_MIN_AMOUNT
from ..permissions import PREPARE_ROLES, require, require_named_user
from ..utils import privacy
from ..utils.phone import is_safaricom, normalize_msisdn, payable_msisdn
from . import owed, partyless

PHONE = "M-Pesa Petty Cash Phone"
TOP_UP = "M-Pesa Petty Cash Top Up"
TOP_UPS = "top_ups"
DISB, ROW, REQUEST = partyless.DISB, partyless.ROW, partyless.REQUEST
PE, JE, GLE = "Payment Entry", "Journal Entry", "GL Entry"
PO, PI = "Purchase Order", "Purchase Invoice"
DOCUMENTS = (PO, PI)
SUITE = "M-Pesa Suite Settings"
# who approves a phone (and confirms its M-Pesa name); who records spends besides the holder
APPROVERS = ("M-Pesa Manager", "Accounts Manager")
ACCOUNTS = ("Accounts Manager", "Accounts User")
# a top-up's states: a draft payout, sent (on its way), and M-Pesa's answer
DRAFT, SENDING, PAID, FAILED, CANCELLED = "Draft", "Sending", "Paid", "Failed", "Cancelled"
OPEN = (DRAFT, SENDING)
# the Payment Entry fields a spend fills (migrate.create_till_payment_fields)
SPEND_FIELDS = ("mpesa_petty_cash_phone", "mpesa_sms_time", "mpesa_sms_balance", "mpesa_sms_payee", "mpesa_sms_text")
# doc.flags key: the phone is submitted by ``approve`` (no other submit approves it)
APPROVE_FLAG = "mpsa_petty_cash_approve"
# what the form shows when the settings have no petty_cash_alert_days
ALERT_DAYS = 7
# a top-up completed within the SMS's minute may be before or after it: only later ones are "after"
SMS_MINUTE = datetime.timedelta(seconds=59)
# the phone's fields the code reads
FIELDS = ("name", "docstatus", "company", "phone", "phone_label", "expected_name", "enabled", "holder",
          "holder_name", "holder_user", "petty_cash_account", "default_cost_center", "charges_account",
          "top_up_limit", "float_ceiling", "owner", "entered_by", "approved_by", "mpesa_name", "name_mismatch",
          "last_receiver_name")
TOP_UP_FIELDS = ("name", "idx", "parent", "amount", "status", "requested_by", "requested_on", "payout", "request",
                 "receipt", "paid_on", "receiver_name", "journal_entry", "note")
SAVEPOINT = "mpesa_petty_cash"


def money(value) -> str:
    return owed.money(value)


def _settings_value(field: str, default=None):
    try:
        value = frappe.db.get_single_value(SUITE, field)
    except Exception:
        return default
    return default if value in (None, "") else value


# ---------------------------------------------------------------- reading the SMS
# Safaricom's confirmations, e.g.
#   "UKD1ABC2DE Confirmed. Ksh1,500.00 paid to SHELL KILIMANI. on 13/11/26 at 10:15 AM. New M-PESA balance is
#    Ksh8,500.00. Transaction cost, Ksh0.00. Amount you can transact within the day is 498,500.00."   (Buy Goods)
#   "... Ksh2,000.00 sent to KPLC PREPAID for account 12345 on 13/11/26 at 11:02 AM New M-PESA balance is ..."
#   "... Ksh500.00 sent to JOHN DOE 0712345678 on 13/11/26 at 12:30 PM. New M-PESA balance is ..."  (send money)
_AMOUNT = r"\d[\d,]*(?:\.\d{1,2})?"
SMS = re.compile(
    rf"\b(?P<receipt>[A-Z0-9]{{10}})\s+Confirmed\.?\s*(?:Ksh|KES)\s?(?P<amount>{_AMOUNT})\s+"
    r"(?P<verb>paid|sent)\s+to\s+(?P<payee>.+?)\s*\.?\s+on\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\s+at\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?\s?[Mm]\.?)",
    re.S,
)
BALANCE = re.compile(rf"New\s+M-?PESA\s+balance\s+is\s+(?:Ksh|KES)\s?(?P<value>-?{_AMOUNT})", re.I)
COST = re.compile(rf"Transaction\s+cost,?\s*(?:Ksh|KES)\s?(?P<value>{_AMOUNT})", re.I)
FOR_ACCOUNT = re.compile(r"^(?P<name>.+?)\s+for\s+account\s+(?P<account>.+?)\.?$", re.I | re.S)
# a phone at the end of a send-money payee, in full or as M-Pesa masks it (0712***678)
PHONE_AT_END = re.compile(r"^(?P<name>.*?)\s*(?P<phone>(?:\+?254|0)[17][\d*]{8}|(?:\+?254)?\s?[17]\d{8})$")
RECEIVED = re.compile(r"\byou\s+have\s+received\b", re.I)
TILL, PAYBILL, PERSON = "Till", "Paybill", "Phone"


def _amount(text) -> float:
    return flt(cstr(text).replace(",", ""), 2)


def _sms_datetime(date_text: str, time_text: str) -> datetime.datetime:
    day, month, year = (int(p) for p in date_text.split("/"))
    year = year + 2000 if year < 100 else year
    clock = re.sub(r"[\s.]", "", time_text).upper()
    for fmt in ("%I:%M%p", "%I:%M:%S%p"):
        try:
            moment = datetime.datetime.strptime(clock, fmt).time()
            break
        except ValueError:
            continue
    else:
        raise ValueError(time_text)
    return datetime.datetime.combine(datetime.date(year, month, day), moment)


def parse_sms(text) -> frappe._dict:
    """What one M-Pesa confirmation SMS says (pure): _dict(receipt, amount, kind (Till, Paybill or
    Phone), payee, account (a paybill's account), phone (send money: 2547... when in full), date
    (YYYY-MM-DD), time (HH:MM), when (a datetime), balance (the phone's new balance, None when the
    SMS has none), charge (the transaction cost, None when it has none)). Raises ValidationError on
    any other text, saying what is wrong."""
    text = re.sub(r"\s+", " ", cstr(text)).strip()
    if not text:
        frappe.throw("Paste the M-Pesa SMS, or type its receipt, amount, date and time.")
    if RECEIVED.search(text):
        frappe.throw("This SMS says the phone received money, not that it paid: paste the SMS of the payment "
                     "(\"… Confirmed. Ksh… paid to …\" or \"… sent to …\").")
    found = SMS.search(text)
    if not found:
        frappe.throw("This is not an M-Pesa payment SMS: it should read \"<receipt> Confirmed. Ksh<amount> paid to "
                     "<till> on <date> at <time>. New M-PESA balance is Ksh…\" (or \"sent to <paybill> for account "
                     "<account>\", or \"sent to <name> <phone>\"). Paste it whole, or type the fields.")
    try:
        when = _sms_datetime(found.group("date"), found.group("time"))
    except ValueError:
        frappe.throw(f"The SMS's date and time ({found.group('date')} {found.group('time')}) cannot be read: type "
                     "them.")
    amount = _amount(found.group("amount"))
    if amount <= 0:
        frappe.throw("The SMS's amount is not a payment: type the amount.")
    payee = found.group("payee").strip().rstrip(".").strip()
    kind, account, phone = TILL, None, None
    if found.group("verb").lower() == "sent":
        to_account = FOR_ACCOUNT.match(payee)
        at_end = PHONE_AT_END.match(payee)
        if to_account:
            kind, payee, account = PAYBILL, to_account.group("name").strip(), to_account.group("account").strip()
        elif at_end and at_end.group("name").strip():
            kind, payee = PERSON, at_end.group("name").strip()
            phone = normalize_msisdn(at_end.group("phone")) or at_end.group("phone").replace(" ", "")
        else:
            kind = PAYBILL  # a business paid by name only
    balance, cost = BALANCE.search(text), COST.search(text)
    return frappe._dict(receipt=found.group("receipt"), amount=amount, kind=kind, payee=payee, account=account,
                        phone=phone, date=when.date().isoformat(), time=when.strftime("%H:%M"), when=when,
                        balance=_amount(balance.group("value")) if balance else None,
                        charge=_amount(cost.group("value")) if cost else None)


# ---------------------------------------------------------------- the phone
def _phone(name, for_update: bool = False):
    if not cstr(name).strip():
        return None
    values = frappe.db.get_value(PHONE, name, list(FIELDS), as_dict=True, for_update=for_update)
    return values


def label(doc) -> str:
    """'PCP-00001 Isinya petty cash (2547****0111)': how messages name a phone."""
    text = cstr(doc.get("name"))
    if cstr(doc.get("phone_label")).strip():
        text += f" {cstr(doc.get('phone_label')).strip()}"
    return f"{text} ({privacy.phone(doc.get('phone'))})"


def usable_problem(doc) -> str | None:
    """Why the phone may not be topped up or spent from now, or None."""
    if not doc:
        return f"{PHONE} not found."
    if cint(doc.get("docstatus")) != 1:
        return f"{doc.name} is not approved{' (cancelled)' if cint(doc.get('docstatus')) == 2 else ''}."
    if not cint(doc.get("enabled")):
        return f"{doc.name} is disabled."
    if cint(doc.get("name_mismatch")):
        paid_as = cstr(doc.get("last_receiver_name")).strip()
        return (f"{doc.name} is blocked: M-Pesa paid its number as {paid_as!r}, not "
                f"{cstr(doc.get('mpesa_name') or doc.get('expected_name')).strip()!r}. A manager checks the number and "
                "presses Name Confirmed.")
    return None


def _same_account_phones(account: str) -> list:
    """Every phone (cancelled ones too) of the petty cash account: an amended phone keeps the history."""
    return frappe.get_all(PHONE, filters={"petty_cash_account": account}, pluck="name") if account else []


def validate_phone(doc) -> None:
    """The phone's validate (a draft, and again at the approval)."""
    msisdn = payable_msisdn(doc.get("phone"))
    if not msisdn or not is_safaricom(msisdn):
        frappe.throw(f"{privacy.shown(doc.get('phone')) or 'No number'} is not a Safaricom number M-Pesa B2C can top "
                     "up: enter 07XX XXX XXX, 01XX XXX XXX or 2547/2541....")
    doc.phone = msisdn
    others = [n for n in frappe.get_all(PHONE, filters={"phone": msisdn, "docstatus": ["<", 2]}, pluck="name")
              if n != doc.get("name")]
    if others:
        frappe.throw(f"{privacy.phone(msisdn)} is petty cash phone {others[0]} already: one phone per number.")
    _check_holder(doc)
    _check_account(doc)
    limit, ceiling = flt(doc.get("top_up_limit"), 2), flt(doc.get("float_ceiling"), 2)
    if limit < B2C_MIN_AMOUNT:
        frappe.throw(f"Top-Up Limit must be at least KES {B2C_MIN_AMOUNT} (the least M-Pesa B2C pays).")
    if limit > B2C_MAX_AMOUNT:
        frappe.throw(f"Top-Up Limit can be at most KES {B2C_MAX_AMOUNT:,} (the most M-Pesa B2C pays at once).")
    if ceiling < limit:
        frappe.throw(f"Float Ceiling (KES {money(ceiling)}) must be at least the Top-Up Limit (KES {money(limit)}).")
    if not cstr(doc.get("phone_label")).strip():
        doc.phone_label = f"Petty cash {cstr(doc.get('holder_name')).strip()}".strip()


def _check_holder(doc) -> None:
    employee = frappe.db.get_value("Employee", doc.get("holder"), ["name", "company", "status", "employee_name",
                                                                   "user_id"], as_dict=True)
    if not employee:
        frappe.throw("Pick the holder: the Employee who keeps the phone.")
    if employee.company and employee.company != doc.get("company"):
        frappe.throw(f"{employee.name} is an employee of {employee.company}, not {doc.get('company')}.")
    if employee.status and employee.status != "Active":
        frappe.throw(f"{employee.name} is {employee.status}: the holder is an active employee.")
    if not cstr(employee.user_id).strip():
        frappe.throw(f"Give {employee.name} a User ID: the holder records the phone's spends with their own login.")
    doc.holder_name = employee.employee_name
    doc.holder_user = employee.user_id


def _check_account(doc) -> None:
    acc = frappe.db.get_value("Account", doc.get("petty_cash_account"),
                              ["name", "company", "is_group", "account_type", "account_currency"], as_dict=True)
    if not acc:
        frappe.throw("Pick the petty cash account: a Cash account of its own, e.g. \"Petty Cash M-Pesa 0722… - AIL\".")
    if acc.company != doc.get("company"):
        frappe.throw(f"{acc.name} is not an account of {doc.get('company')}.")
    if cint(acc.is_group):
        frappe.throw(f"{acc.name} is a group account: pick an account under it.")
    if acc.account_type != "Cash":
        frappe.throw(f"{acc.name} is not a Cash account (Account Type: {acc.account_type or 'none'}): the phone's "
                     "money is cash in hand.")
    if acc.account_currency and acc.account_currency != "KES":
        frappe.throw(f"{acc.name} is in {acc.account_currency}. M-Pesa holds KES only.")
    others = [n for n in frappe.get_all(PHONE, filters={"petty_cash_account": acc.name, "docstatus": ["<", 2]},
                                        pluck="name") if n != doc.get("name")]
    if others:
        frappe.throw(f"{acc.name} is the account of petty cash phone {others[0]} already: each phone has an account "
                     "of its own, so its balance is the phone's.")
    for field, label_ in (("default_cost_center", "Cost Center"), ("charges_account", "Account")):
        value = doc.get(field)
        if value and frappe.db.get_value(label_, value, "company") != doc.get("company"):
            frappe.throw(f"{value} is not a {label_} of {doc.get('company')}.")


def note_who_entered(doc) -> None:
    """entered_by: who saved the draft last (who may not approve it)."""
    if cint(doc.get("docstatus")) == 0:
        doc.entered_by = frappe.session.user


def check_approval(doc) -> None:
    """before_submit: submitting approves the phone, through ``approve`` only, by a named M-Pesa
    Manager or Accounts Manager who did not create it, did not save it last and does not hold it."""
    flags = getattr(doc, "flags", None) or {}
    if not flags.get(APPROVE_FLAG) and not (frappe.flags.in_install or frappe.flags.in_migrate
                                            or frappe.flags.in_patch):
        frappe.throw(f"{doc.name} is approved with the Approve button on its form: submitting it any other way is "
                     "refused.", frappe.PermissionError)
    require(("M-Pesa Manager", "Accounts Manager"))
    require_named_user()
    user = frappe.session.user
    if user in {doc.get("owner"), doc.get("entered_by")} - {None, ""}:
        frappe.throw("A different person must approve: you entered or changed this petty cash phone.",
                     frappe.PermissionError)
    validate_phone(doc)
    if user == doc.get("holder_user"):
        frappe.throw("You hold this phone: another manager must approve it.", frappe.PermissionError)
    doc.approved_by = user
    doc.approved_on = now_datetime()


# what may change once approved (allow_on_submit) and who may not change it
LIMIT_FIELDS = ("holder", "top_up_limit", "float_ceiling", "enabled")
# what the system writes on an approved phone (the M-Pesa name check): a save keeps the database's
SYSTEM_FIELDS = ("mpesa_name", "name_checked_on", "name_mismatch", "last_receiver_name")


def keep_system_values(doc) -> None:
    """A save of the approved phone (its form's Update, the API) writes every field and row back from
    the copy it holds, which may be older than a top-up or a Result: the top-up rows and the M-Pesa
    name check are kept as the database holds them (a locking read), so a stale form neither drops a
    top-up nor lifts a name block."""
    stored = frappe.db.get_value(PHONE, doc.name, list(SYSTEM_FIELDS), as_dict=True, for_update=True) or {}
    for field in SYSTEM_FIELDS:
        doc.set(field, stored.get(field))
    rows = frappe.get_all(TOP_UP, filters={"parent": doc.name, "parenttype": PHONE}, fields=["*"])
    doc.set(TOP_UPS, [])
    for row in sorted(rows, key=lambda r: cint(r.get("idx"))):
        doc.append(TOP_UPS, row)


def check_update(doc) -> None:
    """before_update_after_submit: the system's values kept (keep_system_values); the holder and
    limits may change, by who may write the phone, but not by its holder (before or after the
    change); every check runs again, and a Comment says what changed."""
    keep_system_values(doc)
    before = doc.get_doc_before_save() if callable(getattr(doc, "get_doc_before_save", None)) else None
    if not before:
        return
    changed = [f for f in LIMIT_FIELDS if cstr(before.get(f)) != cstr(doc.get(f))]
    if not changed:
        return
    user = frappe.session.user
    _check_holder(doc)
    if user in {before.get("holder_user"), doc.get("holder_user")} - {None, ""}:
        frappe.throw("You hold this phone: another person changes its holder or limits.", frappe.PermissionError)
    limit, ceiling = flt(doc.get("top_up_limit"), 2), flt(doc.get("float_ceiling"), 2)
    if not B2C_MIN_AMOUNT <= limit <= B2C_MAX_AMOUNT or ceiling < limit:
        frappe.throw(f"Top-Up Limit is KES {B2C_MIN_AMOUNT} to {B2C_MAX_AMOUNT:,}, and Float Ceiling at least the "
                     "limit.")
    shown = "; ".join(f"{doc.meta.get_label(f) if getattr(doc, 'meta', None) else f}: {before.get(f)} to {doc.get(f)}"
                      for f in changed)
    doc.add_comment("Comment", f"{user} changed {shown}.")


def before_cancel(doc) -> None:
    """Refused while a top-up is prepared or on its way; otherwise its history (payouts, entries) does
    not hold the cancel: an amended phone (a new number) keeps the account and its book balance."""
    open_ = [t for t in refresh_top_ups(doc.name) if t.status in OPEN]
    if open_:
        frappe.throw(f"{doc.name} cannot be cancelled while top-up #{open_[0].idx} is {open_[0].status} "
                     f"({open_[0].payout}).")
    current = tuple(doc.get("ignore_linked_doctypes") or ())
    # the payouts' rows and requests name the phone in a Dynamic Link, the spends in a Link
    links = (DISB, ROW, REQUEST, PE, JE)
    doc.ignore_linked_doctypes = current + tuple(d for d in links if d not in current)


@frappe.whitelist(methods=["POST"])
def approve(name: str) -> dict:
    """Approve: submit the phone (check_approval, the phone's before_submit, says who may)."""
    require(("M-Pesa Manager", "Accounts Manager"))
    require_named_user()
    doc = frappe.get_doc(PHONE, name)
    doc.check_permission("submit")
    if cint(doc.docstatus) != 0:
        frappe.throw(f"{name} is {'approved' if cint(doc.docstatus) == 1 else 'cancelled'} already.")
    doc.flags[APPROVE_FLAG] = True
    doc.submit()
    return {"name": doc.name, "docstatus": cint(doc.docstatus), "approved_by": doc.get("approved_by")}


@frappe.whitelist(methods=["POST"])
def confirm_name(name: str, adopt: int = 0) -> dict:
    """Name Confirmed: a manager checked the number whose M-Pesa name did not match; top-ups go on.
    ``adopt``: the name M-Pesa gave is the phone's from now on."""
    require(("M-Pesa Manager", "Accounts Manager"))
    require_named_user()
    frappe.has_permission(PHONE, "write", doc=name, throw=True)
    doc = _phone(name)
    if not doc or not cint(doc.name_mismatch):
        frappe.throw(f"{name}'s M-Pesa name is not blocked.")
    if frappe.session.user == doc.holder_user:
        frappe.throw("You hold this phone: another manager confirms its name.", frappe.PermissionError)
    values = {"name_mismatch": 0}
    if cint(adopt) and cstr(doc.last_receiver_name).strip():
        values["mpesa_name"] = _without_number(doc.last_receiver_name)
    frappe.db.set_value(PHONE, name, values)
    frappe.get_doc(PHONE, name).add_comment(
        "Comment", f"{frappe.session.user} confirmed the M-Pesa name {cstr(doc.last_receiver_name).strip()!r}"
                   f"{' and made it the phone’s' if cint(adopt) else ''}: top-ups go on.")
    return {"name": name, "mpesa_name": values.get("mpesa_name") or doc.mpesa_name}


# ---------------------------------------------------------------- balances
def book_balance(account: str) -> float:
    """The petty cash account's balance in the books: what the phone should hold."""
    if not account:
        return 0.0
    rows = frappe.db.sql(
        f"SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0) FROM `tab{GLE}` "
        "WHERE account = %s AND is_cancelled = 0",
        (account,),
    )
    return flt(rows[0][0] if rows else 0, 2)


def _spend_columns() -> bool:
    return bool(frappe.db.has_column(PE, "mpesa_petty_cash_phone"))


def sms_status(doc) -> frappe._dict:
    """The phone's book balance against the balance on its last recorded SMS: _dict(book, last_sms
    (name, when, balance) or None, expected (the SMS's balance, plus the top-ups paid and less the
    spends recorded after it), difference (book less expected; None without an SMS balance),
    last_top_up, last_spend)."""
    account = doc.get("petty_cash_account")
    phones = _same_account_phones(account) or [doc.get("name")]
    out = frappe._dict(book=book_balance(account), last_sms=None, expected=None, difference=None,
                       last_top_up=None, last_spend=None)
    paid = frappe.db.sql(
        f"""SELECT amount, paid_on FROM `tab{TOP_UP}` WHERE parent IN %(phones)s AND parenttype = %(parenttype)s
        AND status = %(paid)s""",
        {"phones": tuple(phones), "parenttype": PHONE, "paid": PAID}, as_dict=True)
    times = [_when(t.paid_on) for t in paid if _when(t.paid_on)]
    out.last_top_up = max(times) if times else None
    if not _spend_columns():
        return out
    spends = frappe.db.sql(
        f"""SELECT name, paid_amount, posting_date, mpesa_sms_time, mpesa_sms_balance, creation FROM `tab{PE}`
        WHERE docstatus = 1 AND mpesa_petty_cash_phone IN %(phones)s""",
        {"phones": tuple(phones)}, as_dict=True)
    moments = [_when(s.mpesa_sms_time) or _when(s.posting_date) for s in spends]
    moments = [m for m in moments if m]
    out.last_spend = max(moments) if moments else None
    with_balance = [s for s in spends if s.mpesa_sms_balance is not None and _when(s.mpesa_sms_time)]
    if not with_balance:
        return out
    last = max(with_balance, key=lambda s: (_when(s.mpesa_sms_time), cstr(s.creation)))
    when = _when(last.mpesa_sms_time)
    out.last_sms = frappe._dict(name=last.name, when=when, balance=flt(last.mpesa_sms_balance, 2))
    topped = sum(flt(t.amount) for t in paid if _when(t.paid_on) and _when(t.paid_on) > when + SMS_MINUTE)
    spent = sum(flt(s.paid_amount) for s in spends if s.name != last.name and _when(s.mpesa_sms_time)
                and _when(s.mpesa_sms_time) > when)
    out.expected = flt(out.last_sms.balance + topped - spent, 2)
    out.difference = flt(out.book - out.expected, 2)
    return out


def _when(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    try:
        return datetime.datetime.fromisoformat(cstr(value).strip()[:19])
    except ValueError:
        return None


# ---------------------------------------------------------------- top-ups
def top_ups(phone: str) -> list:
    return sorted(frappe.get_all(TOP_UP, filters={"parent": phone, "parenttype": PHONE}, fields=list(TOP_UP_FIELDS)),
                  key=lambda t: cint(t.idx))


def _live_status(top_up) -> str:
    """A Draft or Sending top-up as its payout stands now: Draft (the payout a draft), Sending (sent),
    Cancelled (its draft deleted, or its batch cancelled before sending it)."""
    if top_up.status not in OPEN:
        return top_up.status
    if not top_up.payout:
        return top_up.status  # its payout is being made (top_up): in the same transaction
    docstatus = frappe.db.get_value(DISB, top_up.payout, "docstatus")
    if docstatus is None:
        return CANCELLED  # its draft was deleted
    if cint(docstatus) == 0:
        return DRAFT
    if cint(docstatus) == 1:
        return SENDING
    row = frappe.db.get_value(ROW, {"parent": top_up.payout, "reference_row": top_up.name}, "payment_status")
    return SENDING if row in ("Initiated", "Paid") else CANCELLED


def refresh_top_ups(phone: str) -> list:
    """The phone's top-ups, a Draft or Sending one's status set to where its payout stands."""
    rows = top_ups(phone)
    for row in rows:
        status = _live_status(row)
        if status != row.status:
            frappe.db.set_value(TOP_UP, row.name, "status", status, update_modified=False)
            row.status = status
    return rows


def _on_its_way(rows, exclude: str | None = None) -> float:
    """What top-ups add that the book balance does not show yet: sent and not answered, or Paid and
    not booked (a booking the sweep retries)."""
    total = 0.0
    for row in rows:
        if row.name == exclude:
            continue
        unbooked = row.status == PAID and cint(frappe.db.get_value(JE, row.journal_entry, "docstatus")
                                                if row.journal_entry else 0) != 1
        if row.status == SENDING or unbooked:
            total += flt(row.amount)
    return flt(total, 2)


def in_flight(phone: str, exclude: str | None = None) -> float:
    """What the phone's top-ups add on top of its book balance (``_on_its_way``)."""
    return _on_its_way(refresh_top_ups(phone), exclude)


def amount_problem(doc, amount, book: float, flying: float) -> str | None:
    """Why ``amount`` may not top the phone up now, or None: the B2C limits, the top-up limit, and
    the ceiling (the book balance and the top-ups on their way counted)."""
    amount = flt(amount, 2)
    if amount != int(amount):
        return f"M-Pesa B2C pays whole shillings only: KES {money(amount)}."
    if amount < B2C_MIN_AMOUNT:
        return f"KES {money(amount)} is less than M-Pesa B2C pays (KES {B2C_MIN_AMOUNT})."
    if amount > flt(doc.get("top_up_limit")):
        return (f"KES {money(amount)} is more than {doc.get('name')}'s top-up limit, KES "
                f"{money(doc.get('top_up_limit'))}.")
    if amount > B2C_MAX_AMOUNT:
        return f"KES {money(amount)} is more than M-Pesa B2C pays at once (KES {B2C_MAX_AMOUNT:,})."
    room = flt(flt(doc.get("float_ceiling")) - book - flying, 2)
    if amount > room + 0.005:
        on_way = f" and KES {money(flying)} on its way" if flying else ""
        return (f"{doc.get('name')} would hold KES {money(book + flying + amount)}, more than its float ceiling of KES "
                f"{money(doc.get('float_ceiling'))} (book balance KES {money(book)}{on_way}): top it up by at most KES "
                f"{money(max(room, 0))}.")
    return None


def suggested_amount(doc, book: float, flying: float) -> int:
    """What a top-up offers: the ceiling less the book balance and what is on its way, at most the
    top-up limit, in whole shillings (0: nothing to top up)."""
    room = min(flt(doc.get("float_ceiling")) - book - flying, flt(doc.get("top_up_limit")), B2C_MAX_AMOUNT)
    return max(int(flt(room, 2) + 0.001), 0)


def _not_holder(doc, action: str) -> None:
    """Nobody tops up the phone they hold, nor a phone that is theirs (money_out.self_pay)."""
    user = frappe.session.user
    if user and user == doc.get("holder_user"):
        frappe.throw(f"You hold {doc.get('name')}: another person must {action} its top-up.", frappe.PermissionError)
    from .self_pay import self_pay_problem

    problem = self_pay_problem(user, [{"phone": doc.get("phone"), "payee_name": doc.get("holder_name"),
                                       "mpesa_name": doc.get("mpesa_name"), "label": cstr(doc.get("name"))}], action)
    if problem:
        frappe.throw(problem, frappe.PermissionError)


def _new_disbursement(doc, settings: str, top_up_row: str, amount: int):
    """A draft party-less B2C Payment Disbursement paying the top-up (money_out.partyless)."""
    from ..money_in import common as money_in_common
    from ..wallet import accounts as wallet_accounts

    bank_account = wallet_accounts.bank_account(settings, wallet_accounts.UTILITY)
    paid_from = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
    if not paid_from:
        frappe.throw(f"Map a Bank Account on Mpesa Settings {settings} (Accounting Mapping).")
    disb = frappe.new_doc(DISB)
    disb.update({
        "naming_series": "MPESA-B2C-.YYYY.-",
        "company": doc.company,
        "posting_date": nowdate(),
        "payment_type": "Mpesa Disbursement",
        "party_type": None,
        "transaction_to_pay_against": PHONE,
        "mode_of_payment": money_in_common.mode_of_payment_for(settings),
        "paid_from": paid_from,
        "paid_from_account_currency": "KES",
        "paid_to": None,
        "paid_to_account_currency": "KES",
        "paid_amount": amount,
        "source_exchange_rate": 1,
        "target_exchange_rate": 1,
    })
    disb.append("references", {"reference_doctype": PHONE, "reference_name": doc.name, "reference_row": top_up_row,
                               "partyb": doc.phone, "allocated_amount": amount, "currency": "KES", "exchange_rate": 1,
                               "party_type": None, "party": None})
    return disb


def _add_top_up(doc, amount: int) -> str:
    """A Draft top-up row on the (submitted) phone, written directly: the phone is not saved."""
    idx = max([cint(t.idx) for t in top_ups(doc.name)] or [0]) + 1
    row = frappe.get_doc({"doctype": TOP_UP, "parent": doc.name, "parenttype": PHONE, "parentfield": TOP_UPS,
                          "idx": idx, "docstatus": 1, "amount": amount, "status": DRAFT,
                          "requested_by": frappe.session.user, "requested_on": now_datetime()})
    row.db_insert()
    return row.name


@frappe.whitelist(methods=["POST"])
def top_up(phone: str, amount=None, mpesa_settings: str | None = None) -> dict:
    """Top Up by M-Pesa: a top-up row and a draft B2C Payment Disbursement paying it, for ``amount``
    (default: the ceiling less the book balance and what is on its way). Nothing is sent: an M-Pesa
    Payer sends it with Send via M-Pesa."""
    require(PREPARE_ROLES)
    frappe.has_permission(PHONE, "read", doc=phone, throw=True)
    frappe.has_permission(DISB, "create", throw=True)
    from .employee_documents import _shortcode

    with owed.doc_lock(PHONE, phone):
        doc = _phone(phone, for_update=True)
        problem = usable_problem(doc)
        if problem:
            frappe.throw(problem)
        _not_holder(doc, "prepare")
        rows = refresh_top_ups(doc.name)
        waiting = [t for t in rows if t.status in OPEN]
        if waiting:
            frappe.throw(f"Top-up #{waiting[0].idx} of {doc.name} is {waiting[0].status} ({waiting[0].payout}): send "
                         "it, or delete its draft, before another.")
        book = book_balance(doc.petty_cash_account)
        flying = in_flight(doc.name)
        amount = flt(amount, 2) if flt(amount) else suggested_amount(doc, book, flying)
        if amount < 1:
            frappe.throw(f"{doc.name} holds KES {money(book)} of its KES {money(doc.float_ceiling)} ceiling: nothing "
                         "to top up.")
        problem = amount_problem(doc, amount, book, flying)
        if problem:
            frappe.throw(problem)
        amount = int(amount)
        settings = _shortcode(doc.company, "use_b2c", mpesa_settings)
        row = _add_top_up(doc, amount)
        disb = _new_disbursement(doc, settings, row, amount)
        disb.insert()
        frappe.db.set_value(TOP_UP, row, "payout", disb.name, update_modified=False)
    return {"doctype": DISB, "name": disb.name, "amount": amount, "top_up": row}


# ---------------------------------------------------------------- the M-Pesa name of the number
def _without_number(received) -> str:
    from .claim_payees import _without_number as strip

    return strip(received)


def check_name(phone: str, request) -> None:
    """A top-up was Paid: the name M-Pesa gave the number, on the first top-up against the Expected
    Name, later against that first name. A mismatch (or another number in front of it) blocks top-ups
    until a manager confirms it, with a Comment and an alert. Safe to repeat."""
    from . import party_accounts as pa

    received = cstr(request.get("receiver_public_name")).strip()
    doc = _phone(phone)
    if not received or not doc:
        return
    name = _without_number(received)
    prefix = pa.number_prefix(received)
    other_number = bool(prefix) and prefix.isdigit() and normalize_msisdn(prefix) != doc.phone
    values = {"last_receiver_name": received[:140]}
    expected = cstr(doc.mpesa_name).strip() or cstr(doc.expected_name).strip()
    mismatch = other_number or bool(expected and name and not pa.names_match(expected, name))
    if not mismatch and not cstr(doc.mpesa_name).strip():
        values.update(mpesa_name=name[:140], name_checked_on=now_datetime())  # the first top-up's name
    # a name a manager confirmed already (Name Confirmed, the last name M-Pesa gave) is not blocked again
    if mismatch and not cint(doc.name_mismatch) and cstr(doc.last_receiver_name).strip() != received[:140]:
        values["name_mismatch"] = 1
        message = (f"Petty cash phone {label(doc)} was topped up by {request.get('name')}, and M-Pesa paid it as "
                   f"{received!r}, not {expected!r}: top-ups are held until a manager checks the number and presses "
                   "Name Confirmed.")
        frappe.get_doc(PHONE, phone).add_comment("Comment", message)
        pa.alert(message, frappe._dict(doctype=PHONE, name=phone))
    frappe.db.set_value(PHONE, phone, values, update_modified=False)


# ---------------------------------------------------------------- the party-less handler (money_out.partyless)
class Handler(partyless.Handler):
    """A petty cash phone's top-ups: the paying row is the phone's top-up row."""

    def _top_up(self, row):
        return frappe.db.get_value(TOP_UP, partyless._value(row, "reference_row"), list(TOP_UP_FIELDS), as_dict=True)

    def check_row(self, row, parent) -> None:
        idx = partyless._value(row, "idx")
        name = partyless._value(row, "reference_name")
        doc = _phone(name)
        problem = usable_problem(doc)
        if problem:
            frappe.throw(f"Row #{idx}: {problem}")
        if normalize_msisdn(partyless._value(row, "partyb")) != doc.phone:
            frappe.throw(f"Row #{idx} pays {privacy.shown(partyless._value(row, 'partyb'))}, not the number of "
                         f"{doc.name} ({privacy.phone(doc.phone)}).")
        if parent is not None and parent.get("company") and parent.get("company") != doc.company:
            frappe.throw(f"Row #{idx}: {doc.name} is {doc.company}'s, not {parent.get('company')}'s.")
        top_up_row = self._top_up(row)
        if not top_up_row or top_up_row.parent != doc.name:
            frappe.throw(f"Row #{idx} names no top-up of {doc.name}: make the top-up with Top Up by M-Pesa on the "
                         "phone.")
        if abs(flt(partyless._value(row, "allocated_amount")) - flt(top_up_row.amount)) >= 0.01:
            frappe.throw(f"Row #{idx} pays KES {money(partyless._value(row, 'allocated_amount'))}, not top-up "
                         f"#{top_up_row.idx}'s KES {money(top_up_row.amount)}.")
        if top_up_row.status in (PAID, CANCELLED):
            frappe.throw(f"Row #{idx}: top-up #{top_up_row.idx} of {doc.name} is {top_up_row.status}.")
        mine = parent.get("name") if parent is not None else None
        if top_up_row.payout and mine and top_up_row.payout != mine:
            other = frappe.db.get_value(DISB, top_up_row.payout, "docstatus")
            if other is not None and cint(other) != 2:
                frappe.throw(f"Row #{idx}: top-up #{top_up_row.idx} of {doc.name} is paid by {top_up_row.payout}.")
        _not_holder(doc, "send")
        book = book_balance(doc.petty_cash_account)
        problem = amount_problem(doc, top_up_row.amount, book, in_flight(doc.name, exclude=top_up_row.name))
        if problem:
            frappe.throw(f"Row #{idx}: {problem}")

    def on_result(self, row, request) -> None:
        top_up_row = self._top_up(row)
        if not top_up_row:
            return
        values = {"payout": row.parent, "request": request.get("name")}
        if row.payment_status == "Paid":
            values.update(status=PAID, receipt=cstr(row.reference_no or request.get("transaction_id")).strip() or None,
                          paid_on=request.get("transaction_completed_datetime") or now_datetime(),
                          receiver_name=cstr(request.get("receiver_public_name")).strip()[:140] or None, note=None)
        elif row.payment_status == "Failed" and top_up_row.status != PAID:
            values.update(status=FAILED,
                          note=cstr(request.get("result_desc") or request.get("error_message")).strip()[:140] or None)
        frappe.db.set_value(TOP_UP, top_up_row.name, values, update_modified=False)
        if row.payment_status == "Paid":
            check_name(row.reference_name, request)

    def book(self, row):
        top_up_row = self._top_up(row)
        doc = _phone(row.reference_name)
        if not top_up_row or not doc:
            frappe.throw(f"{row.reference_name} row {row.reference_row}: the top-up it pays is gone.")
        if self.booked(row) is not None:
            return None
        je, submit = self._journal(row, top_up_row, doc)
        from ..accounting.posting import insert_entry

        insert_entry(je, submit, ignore_permissions=True)  # posted at once (accounting.posting)
        frappe.db.set_value(TOP_UP, top_up_row.name, {"journal_entry": je.name, "status": PAID},
                            update_modified=False)
        return None

    def _journal(self, row, top_up_row, doc) -> tuple:
        """(the top-up's Journal Entry, whether it is posted): debit the petty cash account, credit the
        shortcode's Utility GL, the receipt as Cheque No on M-Pesa's date."""
        from ..money_out.common import payout_cost_center
        from ..wallet import accounts as wallet_accounts

        request = frappe.db.get_value(REQUEST, row.b2c_disbursement_request,
                                      ["name", "mpesa_settings", "transaction_completed_datetime", "transaction_id"],
                                      as_dict=True) or frappe._dict()
        receipt = cstr(row.reference_no or request.get("transaction_id")).strip()
        credit = wallet_accounts.b2c_utility_gl(row) or frappe.db.get_value(DISB, row.parent, "paid_from")
        settings = request.get("mpesa_settings")
        bank_account = wallet_accounts.bank_account(settings, wallet_accounts.UTILITY) if settings else None
        day = getdate(request.get("transaction_completed_datetime") or row.reference_date or nowdate())
        cost_center = doc.default_cost_center or payout_cost_center(settings, doc.company)
        amount = flt(row.allocated_amount, 2)
        remark = (f"Top-up #{top_up_row.idx} of petty cash phone {label(doc)}, held by {doc.holder_name}: M-Pesa "
                  f"{receipt} ({request.get('name') or row.b2c_disbursement_request}).")
        je = frappe.new_doc(JE)
        je.voucher_type = "Bank Entry"
        je.company = doc.company
        je.posting_date = je.cheque_date = day
        je.cheque_no = receipt
        je.user_remark = remark
        if je.meta.has_field("custom_remark"):  # v16 builds the remark itself unless told
            je.custom_remark = 1
            je.remark = remark
        je.append("accounts", {"account": doc.petty_cash_account, "debit_in_account_currency": amount,
                               "cost_center": cost_center, "user_remark": remark})
        je.append("accounts", {"account": credit, "credit_in_account_currency": amount, "cost_center": cost_center,
                               "bank_account": bank_account, "user_remark": remark})
        submit = frappe.db.get_value("Mpesa Settings", settings, "submit_b2c_accounting_entries") if settings else 1
        return je, submit

    def booked(self, row) -> int | None:
        top_up_row = self._top_up(row)
        journal = top_up_row.journal_entry if top_up_row else None
        status = frappe.db.get_value(JE, journal, "docstatus") if journal else None
        if status is not None and cint(status) < 2:
            return cint(status)
        doc = _phone(partyless._value(row, "reference_name"))
        receipt = cstr(partyless._value(row, "reference_no")).strip()
        if not receipt or not doc:
            return None
        found = frappe.db.sql(
            f"""SELECT je.name, je.docstatus FROM `tab{JE}` je
            JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
            WHERE je.cheque_no = %s AND je.docstatus < 2 AND jea.account = %s AND jea.debit_in_account_currency > 0
            ORDER BY je.docstatus DESC""",
            (receipt, doc.petty_cash_account), as_dict=True)
        if not found:
            return None
        if top_up_row:  # an entry made by hand for its receipt (a closed period): the top-up's
            frappe.db.set_value(TOP_UP, top_up_row.name, "journal_entry", found[0].name, update_modified=False)
        return cint(found[0].docstatus)

    def describe(self, row) -> dict:
        doc = _phone(partyless._value(row, "reference_name"))
        if not doc:
            return {}
        top_up_row = self._top_up(row)
        number = f", top-up #{top_up_row.idx}" if top_up_row else ""
        expected = cstr(doc.mpesa_name).strip() or cstr(doc.expected_name).strip() or None
        return {"receiver": f"Petty cash phone {cstr(doc.phone_label).strip() or doc.name}",
                "party": f"held by {doc.holder_name}", "document": f"{PHONE} {doc.name}{number}",
                "expected_name": expected}

    def self_pay_rows(self, parent):
        out = []
        for ref in partyless._partyless_rows(parent):
            doc = _phone(partyless._value(ref, "reference_name")) or frappe._dict()
            out.append({"phone": partyless._value(ref, "partyb"), "payee_name": doc.get("holder_name"),
                        "mpesa_name": doc.get("mpesa_name"), "label": f"Row #{partyless._value(ref, 'idx')}"})
        return out

    def closed_period_hint(self, row, receipt: str) -> str:
        doc = _phone(partyless._value(row, "reference_name")) or frappe._dict()
        account = doc.get("petty_cash_account") or "the petty cash account"
        return (f"Its date is in a closed Accounting Period, in books closed by a Period Closing Voucher or on or "
                f"before Accounts Frozen Till: make a Journal Entry with Cheque No {receipt}, debit {account} and "
                "credit the shortcode's M-Pesa account, dated in an open period; the suite counts it as this "
                "top-up's entry.")


HANDLER = Handler()


# ---------------------------------------------------------------- spends: Paid from Petty Cash Phone
def _check_document(doctype: str) -> None:
    if doctype not in DOCUMENTS:
        frappe.throw("Pick a Purchase Order or a Purchase Invoice.")


def is_holder(doc, user=None) -> bool:
    return bool(doc) and (user or frappe.session.user) == doc.get("holder_user")


def may_record(doc, user=None) -> bool:
    user = user or frappe.session.user
    return is_holder(doc, user) or not set(ACCOUNTS).isdisjoint(frappe.get_roles(user))


def _check_recorder(doc) -> None:
    """Only the phone's holder or the Accounts roles record its spends."""
    if is_holder(doc):
        return
    require(("Accounts Manager", "Accounts User"))


def phones_for(company: str, user=None) -> list:
    """The approved, enabled phones of ``company`` the user may record spends of: the ones they hold,
    or all of them for the Accounts roles."""
    rows = frappe.get_all(PHONE, filters={"company": company, "docstatus": 1, "enabled": 1}, fields=list(FIELDS))
    return sorted((r for r in rows if may_record(r, user)), key=lambda r: (not is_holder(r, user), r.name))


def _pick_phone(company: str, chosen: str | None):
    if cstr(chosen).strip():
        doc = _phone(chosen)
        if not doc or doc.company != company:
            frappe.throw(f"{chosen} is not a petty cash phone of {company}.")
        return doc
    held = [p for p in phones_for(company) if is_holder(p)]
    if len(held) == 1:
        return held[0]
    company_phones = frappe.get_all(PHONE, filters={"company": company, "docstatus": 1, "enabled": 1}, pluck="name")
    if len(company_phones) == 1:
        return _phone(company_phones[0])
    frappe.throw(f"Pick the petty cash phone that paid: {company} has {len(company_phones) or 'no'} approved phones.")


def receipt_used(receipt: str) -> str | None:
    """The live entry that carries ``receipt`` already: 'Payment Entry ACC-PAY-1', or None."""
    for doctype, field in ((PE, "reference_no"), (JE, "cheque_no")):
        name = frappe.db.get_value(doctype, {field: receipt, "docstatus": ["<", 2]}, "name")
        if name:
            return f"{doctype} {name}"
    return None


def spend_values(sms=None, receipt=None, amount=None, sms_date=None, sms_time=None, balance=None, charge=None,
                 payee=None) -> frappe._dict:
    """What a spend records: the SMS read again (``parse_sms``), each field typed by hand agreeing
    with it; or, without an SMS, the typed fields (receipt, amount, date and time needed)."""
    typed = frappe._dict(receipt=cstr(receipt).strip().upper() or None, amount=flt(amount, 2) or None,
                         date=cstr(sms_date).strip() or None, time=cstr(sms_time).strip()[:5] or None,
                         balance=None if balance in (None, "") else flt(balance, 2),
                         charge=None if charge in (None, "") else flt(charge, 2), payee=cstr(payee).strip() or None)
    if cstr(sms).strip():
        read = parse_sms(sms)
        for field, label_ in (("receipt", "receipt"), ("amount", "amount"), ("date", "date"), ("time", "time"),
                              ("balance", "new balance")):
            mine, its = typed.get(field), read.get(field)
            if mine is None or its is None:
                continue
            same = abs(flt(mine) - flt(its)) < 0.005 if field in ("amount", "balance") else (
                cstr(getdate(mine)) == cstr(getdate(its)) if field == "date" else cstr(mine) == cstr(its))
            if not same:
                frappe.throw(f"The {label_} typed ({mine}) is not the SMS's ({its}): correct one of them.")
        if typed.charge is not None:
            read.charge = typed.charge
        read.payee = typed.payee or read.payee
        read.text = re.sub(r"\s+", " ", cstr(sms)).strip()
        return read
    missing = [label_ for field, label_ in (("receipt", "receipt"), ("amount", "amount"), ("date", "date"),
                                            ("time", "time")) if not typed.get(field)]
    if missing:
        frappe.throw(f"Paste the M-Pesa SMS, or type its {', '.join(missing)}.")
    if not re.fullmatch(r"[A-Z0-9]{10}", typed.receipt):
        frappe.throw(f"{typed.receipt} is not an M-Pesa receipt (10 letters and digits).")
    try:
        day = datetime.date.fromisoformat(cstr(getdate(typed.date))[:10])
        when = datetime.datetime.combine(day, datetime.time.fromisoformat(typed.time))
    except ValueError:
        frappe.throw(f"Type the date as a date and the time as HH:MM ({typed.date} {typed.time}).")
    return frappe._dict(receipt=typed.receipt, amount=typed.amount, kind=None, payee=typed.payee, account=None,
                        phone=None, date=when.date().isoformat(), time=when.strftime("%H:%M"), when=when,
                        balance=typed.balance, charge=typed.charge, text=None)


def _supplier_name(supplier) -> str:
    return cstr(frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier).strip()


@frappe.whitelist()
def read_sms(doctype: str, name: str, text: str) -> dict:
    """The dialog's Paste the M-Pesa SMS: what the SMS says, with the supplier's name next to the name
    on the SMS (``name_matches``), so a mismatch is seen."""
    _check_document(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    read = parse_sms(text)
    supplier = frappe.db.get_value(doctype, name, "supplier")
    from .party_accounts import names_match

    supplier_name = _supplier_name(supplier) if supplier else None
    out = dict(read, when=cstr(read.when), supplier=supplier, supplier_name=supplier_name,
               name_matches=bool(supplier_name and read.payee and names_match(supplier_name, read.payee)),
               used=receipt_used(read.receipt))
    return out


def _screenshot(file_url) -> str | None:
    """The screenshot's URL: a file of this site (the dialog's upload), or None."""
    url = cstr(file_url).strip()
    if url and not url.startswith(("/private/files/", "/files/")):
        frappe.throw("Attach the screenshot as a file of this site.")
    return url or None


def _attach(pe_name: str, url: str | None) -> None:
    """The screenshot, attached to the Payment Entry. Under a savepoint: a file Frappe refuses is
    logged, and the spend stands."""
    if not url:
        return
    frappe.db.savepoint(SAVEPOINT)
    try:
        frappe.get_doc({"doctype": "File", "file_url": url, "attached_to_doctype": PE, "attached_to_name": pe_name,
                        "is_private": 1 if url.startswith("/private/") else 0}).insert(ignore_permissions=True)
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        frappe.log_error(title="M-Pesa petty cash screenshot not attached", message=f"{pe_name} {url}\n"
                                                                                    f"{frappe.get_traceback()}")


def build_spend_entry(doctype: str, values, doc, spend, charge: float) -> object:
    """The spend's Payment Entry (built by hand, so nothing replaces the petty cash account): Pay, to
    the supplier, from the petty cash account, the amount plus the charge paid and the charge a
    deduction, against the order or invoice, the receipt and the SMS's date."""
    from erpnext.accounts.party import get_party_account

    from .b2b_entries import reference_rows
    from .common import document_dimensions, payout_cost_center

    amount = flt(spend.amount, 2)
    pe = frappe.new_doc(PE)
    pe.payment_type = "Pay"
    pe.company = doc.company
    pe.party_type = "Supplier"
    pe.party = values.supplier
    pe.paid_from = doc.petty_cash_account
    pe.paid_to = values.get("credit_to") if doctype == PI and values.get("credit_to") else get_party_account(
        "Supplier", values.supplier, doc.company)
    pe.paid_amount = flt(amount + charge, 2)
    pe.received_amount = amount
    pe.source_exchange_rate = pe.target_exchange_rate = 1
    pe.reference_no = spend.receipt
    pe.reference_date = pe.posting_date = spend.date
    pe.cost_center = payout_cost_center(None, doc.company, doctype, values.name)
    for field, value in document_dimensions(doctype, values.name).items():
        if pe.meta.has_field(field):
            pe.set(field, value)
    to = f" to {spend.payee}" if spend.payee else ""
    if spend.get("account"):
        to += f" for account {spend.account}"
    remark = (f"Paid from petty cash phone {label(doc)}, held by {doc.holder_name}: M-Pesa {spend.receipt}, KES "
              f"{money(amount)}{to} on {spend.date} {spend.time}")
    if charge:
        remark += f", charge KES {money(charge)}"
    if spend.balance is not None:
        remark += f"; the phone's new balance KES {money(spend.balance)}"
    pe.custom_remarks = 1
    pe.remarks = f"{remark}. Recorded by {frappe.session.user}."
    rows, problem = reference_rows(doctype, values.name, amount)
    if problem or flt(sum(flt(r["allocated_amount"]) for r in rows), 2) < amount:
        frappe.throw(problem or f"{values.name} cannot take KES {money(amount)} now.")
    for row in rows:
        pe.append("references", row)
    if charge:
        pe.append("deductions", {"account": doc.charges_account,
                                 "cost_center": doc.default_cost_center or pe.cost_center, "amount": charge,
                                 "description": f"M-Pesa transaction cost, {spend.receipt}"})
    extra = {"mpesa_petty_cash_phone": doc.name, "mpesa_sms_time": spend.when, "mpesa_sms_balance": spend.balance,
             "mpesa_sms_payee": cstr(spend.payee)[:140] or None, "mpesa_sms_text": spend.get("text")}
    for field, value in extra.items():
        if pe.meta.has_field(field):
            pe.set(field, value)
    return pe


def _observe(spend, supplier: str, pe_name: str) -> None:
    """The name on the SMS, kept with the known M-Pesa names (source SMS): a person's by their phone;
    a till's or paybill's against the supplier's one approved Till or Paybill account of that kind."""
    from . import known_names
    from . import party_accounts as pa

    if spend.kind == PERSON and normalize_msisdn(spend.phone):
        known_names.observe(known_names.PHONE, spend.phone, f"{spend.phone} - {spend.payee}", when=spend.when,
                            payout_doctype=PE, payout=pe_name, source=known_names.SMS)
        return
    kind = {TILL: pa.TILL, PAYBILL: pa.PAYBILL}.get(spend.kind)
    if not kind or not spend.payee:
        return
    accounts = [a for a in pa.accounts_of(pa.SUPPLIER_ACCOUNT, supplier, (kind,))
                if kind != pa.PAYBILL or not spend.get("account")
                or cstr(a.account_number).strip().upper() == cstr(spend.account).strip().upper()]
    if len(accounts) == 1 and cstr(accounts[0].shortcode).strip():
        known_names.observe(kind, accounts[0].shortcode, spend.payee, when=spend.when, payout_doctype=PE,
                            payout=pe_name, source=known_names.SMS)


@frappe.whitelist(methods=["POST"])
def record_spend(doctype: str, name: str, phone: str | None = None, sms: str | None = None, receipt=None,
                 amount=None, sms_date=None, sms_time=None, balance=None, charge=None, payee=None,
                 screenshot=None) -> dict:
    """Paid from Petty Cash Phone: a submitted Payment Entry from the phone's petty cash account to the
    order's or invoice's supplier, from the pasted SMS (or its fields typed). The phone's holder or the
    Accounts roles only; the receipt once; at most what the document still owes and the phone's book
    balance."""
    _check_document(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    values = owed.read_document(doctype, name)
    if not values:
        frappe.throw(f"{doctype} {name} not found.")
    doc = _pick_phone(values.company, phone)
    _check_recorder(doc)
    spend = spend_values(sms, receipt, amount, sms_date, sms_time, balance, charge, payee)
    screenshot = _screenshot(screenshot)
    charge = flt(spend.charge, 2)
    if charge < 0:
        frappe.throw("A transaction charge is not negative.")
    if charge and not doc.charges_account:
        frappe.throw(f"Set the Charges Account on {doc.name} to record a transaction charge.")
    if spend.when > now_datetime() + datetime.timedelta(minutes=5):
        frappe.throw(f"The SMS's date and time ({spend.date} {spend.time}) are in the future: check them.")
    from ..money_in.common import named_lock

    with owed.doc_lock(PHONE, doc.name), owed.doc_lock(doctype, name), named_lock(f"mpesa-receipt:{spend.receipt}"):
        doc = _phone(doc.name, for_update=True)
        problem = usable_problem(doc)
        if problem:
            frappe.throw(problem)
        values = owed.read_document(doctype, name)
        problems = owed.document_problems(doctype, values)
        if problems:
            frappe.throw(problems[0])
        used = receipt_used(spend.receipt)
        if used:
            frappe.throw(f"M-Pesa receipt {spend.receipt} is on {used} already: a receipt is recorded once.")
        left = owed.left_to_pay(doctype, name, for_update=True)
        if spend.amount > left.left + 0.005:
            frappe.throw(f"KES {money(spend.amount)} is more than {name} still owes. {owed.explain(name, left)}")
        book = book_balance(doc.petty_cash_account)
        if flt(spend.amount + charge, 2) > book + 0.005:
            frappe.throw(f"{doc.name}'s book balance is KES {money(book)}, less than KES "
                         f"{money(spend.amount + charge)}: record its missing top-ups first, so the phone's record is "
                         "complete.")
        pe = build_spend_entry(doctype, values, doc, spend, charge)
        from ..accounting.posting import insert_entry
        from ..money_in.common import as_user

        recorder = frappe.session.user
        # posted at once (accounting.posting), as the system: the holder records a spend without a right on
        # Payment Entry, and ERPNext's own account lookups check that right whatever ignore_permissions says
        # (accounting.payment_entry_service books payouts the same way); who recorded it is in its remarks
        with as_user("Administrator"):
            insert_entry(pe, True, ignore_permissions=True)
            pe.add_comment("Comment", f"Recorded from petty cash phone {doc.name} by {recorder}.")
    _attach(pe.name, screenshot)
    frappe.db.savepoint(SAVEPOINT)
    try:
        _observe(spend, values.supplier, pe.name)
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        frappe.log_error(title="M-Pesa petty cash SMS name not kept", message=frappe.get_traceback())
    return {"payment_entry": pe.name, "amount": spend.amount, "charge": charge, "receipt": spend.receipt,
            "book_balance": flt(book - spend.amount - charge, 2), "sms_balance": spend.balance}


# ---------------------------------------------------------------- what the forms show
def spends_of(doctype: str, name: str) -> list:
    """The petty cash spends of the order or invoice: its Payment Entries from a phone, and (an invoice)
    those it took as advances from its orders."""
    if not _spend_columns():
        return []
    rows = frappe.db.sql(
        f"""SELECT pe.name, pe.reference_no, pe.paid_amount, pe.posting_date, pe.mpesa_petty_cash_phone,
        pe.mpesa_sms_payee, pe.mpesa_sms_balance, per.allocated_amount
        FROM `tabPayment Entry Reference` per JOIN `tab{PE}` pe ON pe.name = per.parent
        WHERE per.reference_doctype = %s AND per.reference_name = %s AND pe.docstatus = 1
          AND COALESCE(pe.mpesa_petty_cash_phone, '') != ''
        ORDER BY pe.posting_date, pe.name""",
        (doctype, name), as_dict=True)
    if doctype == PI and frappe.db.table_exists("Purchase Invoice Advance"):
        taken = frappe.get_all("Purchase Invoice Advance", filters={"parent": name, "reference_type": PE},
                               fields=["reference_name", "allocated_amount"])
        listed = {r.name for r in rows}
        for adv in taken:
            if adv.reference_name in listed:
                continue
            entry = frappe.db.get_value(PE, {"name": adv.reference_name, "docstatus": 1}, [
                "name", "reference_no", "paid_amount", "posting_date", "mpesa_petty_cash_phone", "mpesa_sms_payee",
                "mpesa_sms_balance"], as_dict=True)
            if entry and entry.mpesa_petty_cash_phone:
                rows.append(frappe._dict(entry, allocated_amount=adv.allocated_amount))
    return [{"payment_entry": r.name, "receipt": r.reference_no, "amount": flt(r.allocated_amount or r.paid_amount, 2),
             "paid_amount": flt(r.paid_amount, 2), "date": cstr(r.posting_date), "phone": r.mpesa_petty_cash_phone,
             "payee": r.mpesa_sms_payee} for r in rows]


@frappe.whitelist()
def document_state(doctype: str, name: str) -> dict:
    """What a Purchase Order or Invoice shows: its petty cash spends, and for who may record one the
    phones they may record with and the default."""
    _check_document(doctype)
    frappe.has_permission(doctype, "read", doc=name, throw=True)
    values = owed.read_document(doctype, name)
    if not values:
        frappe.throw(f"{doctype} {name} not found.")
    phones = phones_for(values.company)
    problems = owed.document_problems(doctype, values)
    if not problems and owed.outstanding(doctype, name) <= owed.MIN:
        problems = [f"{name} has nothing left to pay."]
    held = [p.name for p in phones if is_holder(p)]
    return {
        "spends": spends_of(doctype, name),
        "can_record": bool(phones) and not problems,
        "problem": problems[0] if problems else None,
        "phones": [{"name": p.name, "label": label(p), "holder_name": p.holder_name,
                    "book_balance": book_balance(p.petty_cash_account), "has_charges_account": bool(p.charges_account)}
                   for p in phones],
        "default_phone": (held[0] if len(held) == 1 else phones[0].name if len(phones) == 1 else None),
    }


@frappe.whitelist()
def phone_state(name: str) -> dict:
    """What the phone's form shows: the book balance against the last SMS, its top-ups as they stand,
    and what this user may do (Approve, Top Up by M-Pesa, Name Confirmed)."""
    frappe.has_permission(PHONE, "read", doc=name, throw=True)
    doc = _phone(name)
    if not doc:
        frappe.throw(f"{PHONE} {name} not found.")
    user, roles = frappe.session.user, set(frappe.get_roles())
    status = sms_status(doc)
    rows = refresh_top_ups(name) if cint(doc.docstatus) == 1 else top_ups(name)
    flying = _on_its_way(rows)
    approver = not roles.isdisjoint(APPROVERS)
    return {
        "book_balance": status.book,
        "last_sms": status.last_sms and {"payment_entry": status.last_sms.name, "when": cstr(status.last_sms.when),
                                         "balance": status.last_sms.balance},
        "expected": status.expected,
        "difference": status.difference,
        "last_top_up": cstr(status.last_top_up or "") or None,
        "last_spend": cstr(status.last_spend or "") or None,
        "in_flight": flying,
        "top_ups": [dict(t) for t in rows],
        "problem": usable_problem(doc) if cint(doc.docstatus) == 1 else None,
        "suggested_amount": suggested_amount(doc, status.book, flying) if cint(doc.docstatus) == 1 else 0,
        "can_top_up": (cint(doc.docstatus) == 1 and not usable_problem(doc) and not is_holder(doc, user)
                       and not roles.isdisjoint(PREPARE_ROLES) and not any(t.status in OPEN for t in rows)
                       and bool(frappe.has_permission(DISB, "create"))),
        "can_approve": (cint(doc.docstatus) == 0 and approver
                        and user not in {doc.owner, doc.entered_by, doc.holder_user}),
        "can_confirm_name": bool(cint(doc.docstatus) == 1 and cint(doc.name_mismatch) and approver
                                 and not is_holder(doc, user)),
        "can_record": may_record(doc, user),
    }


# ---------------------------------------------------------------- the daily "needs attention" section
def _days(since, now) -> int | None:
    when = _when(since)
    return (now.date() - when.date()).days if when else None


def attention_section():
    """money_out.attention: each approved, enabled phone whose book balance differs from what its last
    SMS says, whose money lies with no spend for petty_cash_alert_days (default 7), or whose M-Pesa
    name is blocked. Linked to the phones' list."""
    days = cint(_settings_value("petty_cash_alert_days", ALERT_DAYS)) or ALERT_DAYS
    now = now_datetime()
    lines = []
    for doc in frappe.get_all(PHONE, filters={"docstatus": 1, "enabled": 1}, fields=list(FIELDS)):
        status = sms_status(doc)
        problems = []
        if status.difference is not None and abs(status.difference) >= 1:
            more = "more" if status.difference > 0 else "less"
            problems.append(f"the books hold KES {money(abs(status.difference))} {more} than its last SMS says (KES "
                            f"{money(status.last_sms.balance)} on {status.last_sms.when:%d %b %H:%M}): a spend not "
                            "recorded, or a typo")
        idle_since = status.last_spend or status.last_top_up
        idle = _days(idle_since, now)
        if status.book > 0.5 and idle is not None and idle >= days:
            what = "spend" if status.last_spend else "spend since its top-up"
            problems.append(f"KES {money(status.book)} with no {what} for {idle} days")
        if cint(doc.name_mismatch):
            problems.append("its M-Pesa name did not match: top-ups held")
        if not problems:
            continue
        top_days, spend_days = _days(status.last_top_up, now), _days(status.last_spend, now)
        facts = [f"book KES {money(status.book)}"]
        facts.append(f"last top-up {top_days} days ago" if top_days is not None else "never topped up")
        facts.append(f"last spend {spend_days} days ago" if spend_days is not None else "no spend recorded")
        lines.append(f"{label(doc)}, {doc.holder_name}: {'; '.join(problems)} ({', '.join(facts)})")
    return "Petty cash phones", lines, "/app/m-pesa-petty-cash-phone"
