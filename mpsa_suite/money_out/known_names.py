"""The names M-Pesa gave every destination it paid: M-Pesa Known Name (round 4.5).

Daraja has no Hakikisha: nothing says whose phone, till or paybill a payout goes to before the
money leaves. Every result does say whom M-Pesa paid ("254705783406 - AMBER DEMAS NAMUSIAH",
"835811 - SHELL KILIMANI"). Each such name is kept, one record per destination, and shown wherever
someone types, approves or sends a payout, so a wrong number is caught by its name.

The list (DocType M-Pesa Known Name, named by its destination key):
- ``B2C:2547...`` a phone, ``TILL:835811`` a till, ``PAYBILL:400200`` a paybill. The phone and till
  keys are the payee accounts' own (claim_payees.destination_key, party_accounts.destination_key); a
  paybill has one name whatever the account number, so their ``PB:400200:ACC`` reads as
  ``PAYBILL:400200`` here (``known_key``);
- the name without the number in front (``mpesa_name``), the text as M-Pesa sent it (``received``),
  when it was first and last seen, on how many payouts, the last payout, and where the name last came
  from (``source``: B2C Result, B2B Result, SMS, History);
- ``salary_only``: every payout seen to it was a salary (a Salary Slip, or an employee paid for no
  Expense Claim or Employee Advance: salary_access). Such a record is shown only to who may see
  salaries; the others see "Paid before" and no name. Once a payout that is no salary pays the
  number, the record holds payouts that are no salary only (its count starts again from that payout):
  what the others see never comes from a salary;
- ``bank``: the paybill is a bank's (M-Pesa Bank Paybill). M-Pesa shows the bank's name, never who
  holds the account, so its name is no check of the account holder. Round 4.5 review: what is shown
  reads the current M-Pesa Bank Paybill list (``lookup``), so a bank added after its record counts;
- ``employee_payee`` (round 4.5 review): a payout to the phone paid an Employee party (a claim, an
  advance or a salary). Such a phone's record is shown only to who may read Expense Claims or see
  salaries (``sees_employee_phones``); tills and paybills are never hidden so.

Names are plain text: whatever HTML a result or an SMS carries is taken out before it is kept
(``plain``), and every screen escapes them too (the v15 list view shows a Data field as it is).

Where a name comes from matters (round 4.5 review): a B2C or B2B Result's name (or one from the
payouts before: History) is M-Pesa's own. An SMS's (a petty cash phone's) is not confirmed by any
payout: it only starts a record where there is none, never changes one, is shown as "From SMS, not
confirmed by a payout", is never a match (it reads as new), and never locks a claim payee's name
(``confirmed``). The first Result that names the number replaces it.

Written by the system only (no role creates or changes a record: ``check_system_write``):
- going forward, where a payout becomes Paid with a name: ``record_b2c`` (the B2C Result callback,
  and a Transaction Status answer that settles a B2C payout: b2c_status.apply_to_payout),
  ``record_b2b`` (the B2B Result, and a Transaction Status answer that names a B2B receiver). Each
  runs once per payout, under a savepoint, and never stops the payout's own processing: an error,
  a lock wait past MariaDB's limit too (the transaction stays), rolls back only its own writes and
  is logged; a deadlock is raised (the transaction is gone, as partyless.on_result). A name whose
  number in front is another number's is not kept;
- once, from history, by the migrate patch ``known_mpesa_names`` (``backfill``, idempotent: it
  rebuilds each destination's record from the Paid payouts);
- by later features through ``observe`` (e.g. a petty cash phone's SMS: source SMS, the name the
  SMS gave only).

Read by:
- ``lookup(keys)`` and ``verdict(typed_name, known)`` (pure): the claim payees' names
  (claim_payees.mpesa_names) and any server check. A single typed word against a longer M-Pesa
  name ("Jane" for JANE WANJIKU) is "partial", not a match: shown amber, acknowledged with the
  names that differ;
- ``summary(targets)``: the Send via M-Pesa confirm screen (money_out.send_confirm) and Pay Selected
  (money_out.schedule): each row's known name, the typed or expected name and the verdict, the
  counts, and what the sender acknowledges;
- ``for_destinations(destinations)``: the desk badges (public/js/mpsa_known_names.js), for who may read
  the list, a salary-only name hidden from who may not see salaries.
"""

import datetime
import json
import re

import frappe
from frappe.utils import cint, cstr, now_datetime

from ..utils.phone import normalize_msisdn
from . import party_accounts as pa
from . import salary_access

KNOWN = "M-Pesa Known Name"
B2C_REQUEST = "B2C Disbursement Request"
B2B = "Mpesa B2B Payment Request"
BANK_PAYBILL = "M-Pesa Bank Paybill"
CLAIM = "Expense Claim"
CLAIM_PAYEE = "M-Pesa Claim Payee"
ACCOUNTS = ("Supplier M-Pesa Account", "Employee M-Pesa Account")
PARTY_NAME = {"Supplier": "supplier_name", "Employee": "employee_name", "Customer": "customer_name"}
# Daraja's B2B commands (daraja.money_out.B2B_BUY_GOODS / B2B_PAYBILL)
BUY_GOODS, PAY_BILL = "BusinessBuyGoods", "BusinessPayBill"

# The destination's type, and its key's prefix
PHONE, TILL, PAYBILL = "Phone", "Till", "Paybill"
PREFIX = {PHONE: "B2C", TILL: "TILL", PAYBILL: "PAYBILL"}
# Every way the suite names a destination's type: the payee types, the schedule's routes, Daraja's commands
_TYPES = {
    "phone": PHONE, "b2c": PHONE, "phone (b2c)": PHONE, "b2c phone": PHONE,
    "till": TILL, "till (buy goods)": TILL, "b2b till": TILL, "buy goods": TILL, BUY_GOODS.lower(): TILL,
    "paybill": PAYBILL, "b2b paybill": PAYBILL, PAY_BILL.lower(): PAYBILL, "bank": PAYBILL,
    "bank account (via bank paybill)": PAYBILL, "bank paybill (b2b)": PAYBILL,
}
# Where the name came from (source); an SMS's is confirmed by no payout
B2C_RESULT, B2B_RESULT, SMS, HISTORY = "B2C Result", "B2B Result", "SMS", "History"
# verdict(): the name typed (or expected) against M-Pesa's; "partial": one typed word in a longer name
MATCH, DIFFERS, NEW, PARTIAL = "match", "differs", "new", "partial"
# for_destinations' status of a destination
KNOWN_STATUS, NEW_STATUS, HIDDEN, INVALID = "known", "new", "hidden", "invalid"
# The record's fields, its name first; the values a write sets are the rest
FIELDS = ("name", "destination_type", "number", "mpesa_name", "received", "first_seen", "last_seen", "times_paid",
          "last_payout_doctype", "last_payout", "source", "salary_only", "bank", "employee_payee")
VALUES = FIELDS[1:]
# The party type whose phone is hidden from who may read neither Expense Claims nor salaries
EMPLOYEE = "Employee"
# What a kept name may not hold (plain): tags, and the characters that would make one or leave an attribute
_TAG = re.compile(r"<[^>]*>")
_UNSAFE = re.compile(r"[<>\"`]")
# Data fields hold at most 140 characters
DATA_LENGTH = 140
# doc.flags key of a system write (check_system_write)
FLAG = "mpsa_known_names"
SAVEPOINT, INSERT_SAVEPOINT = "mpesa_known_name", "mpesa_known_name_insert"
# Destinations for_destinations answers in one call
LIMIT = 500


# ---------------------------------------------------------------- keys
def destination_type(value) -> str | None:
    """Phone, Till or Paybill from any of the suite's names for it (a payee type, a schedule route, a
    B2B command); None for anything else."""
    value = cstr(value).strip()
    if value in PREFIX:
        return value
    return _TYPES.get(value.lower())


def key_of(kind, number) -> str | None:
    """The list's key of a destination: 'B2C:2547...' for a phone in any form, 'TILL:835811',
    'PAYBILL:400200'; None for a number that is no such destination."""
    kind = destination_type(kind)
    if kind == PHONE:
        msisdn = normalize_msisdn(number)
        return f"B2C:{msisdn}" if msisdn else None
    if kind in (TILL, PAYBILL):
        code = re.sub(r"\s", "", cstr(number))
        return f"{PREFIX[kind]}:{code}" if code.isdigit() else None
    return None


def known_key(value) -> str | None:
    """The list's key of any destination key the suite uses: 'B2C:...' and 'TILL:...' as they are, a
    paybill's 'PB:<paybill>:<account>' (claim payees, payee accounts) or 'PAYBILL:<paybill>' as
    'PAYBILL:<paybill>'."""
    head, _, rest = cstr(value).strip().partition(":")
    head = head.upper()
    if head == "B2C":
        return key_of(PHONE, rest)
    if head == "TILL":
        return key_of(TILL, rest)
    if head in ("PB", "PAYBILL"):
        return key_of(PAYBILL, rest.split(":")[0])
    return None


def split_key(key) -> tuple:
    """(destination_type, number) of a key of the list; (None, None) for anything else."""
    key = known_key(key)
    if not key:
        return None, None
    head, _, number = key.partition(":")
    return {v: k for k, v in PREFIX.items()}[head], number


def ready() -> bool:
    """The list exists on this site (a site not migrated yet has no table: nothing is read or kept)."""
    try:
        return bool(frappe.db.table_exists(KNOWN))
    except Exception:
        return False


def bank_paybills() -> set:
    """Every paybill on M-Pesa Bank Paybill (enabled or not: a retired row is still the bank's)."""
    try:
        return {cstr(p).strip() for p in frappe.get_all(BANK_PAYBILL, pluck="paybill") if cstr(p).strip()}
    except Exception:
        return set()


def is_bank_paybill(number, banks=None) -> bool:
    number = cstr(number).strip()
    return bool(number) and number in (bank_paybills() if banks is None else banks)


def plain(text) -> str:
    """A name or a received text as plain text (round 4.5 review): HTML tags taken out, then any
    character that could start one or end an attribute (< > " `), and the spaces made single. What
    M-Pesa sends ("254705783406 - AMBER DEMAS NAMUSIAH", "O'BRIEN & SONS") is unchanged."""
    return " ".join(_UNSAFE.sub(" ", _TAG.sub(" ", cstr(text))).split())


def confirmed(known) -> bool:
    """A record whose name a payout's result gave (a B2C or B2B Result, or History): not an SMS's
    alone, which only says what was typed or texted."""
    return bool(known) and cstr(known.get("source")).strip() != SMS


def sees_employee_phones(user: str | None = None) -> bool:
    """Who may see the name on a phone paid to an Employee (a claim, an advance or a salary): who may
    see salaries, or read Expense Claims by a role other than the employee's own (salary_access)."""
    user = user or frappe.session.user
    return salary_access.can_see_salaries(user) or CLAIM in salary_access.reads_documents(user)


# ---------------------------------------------------------------- what one payout says (pure, but the bank flag)
def _when(value):
    """A datetime from a datetime, a date or their text; None for nothing."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time())
    try:
        return datetime.datetime.fromisoformat(cstr(value).strip()[:26])
    except ValueError:
        return None


def _get(doc, field):
    getter = getattr(doc, "get", None)
    return getter(field) if callable(getter) else getattr(doc, field, None)


def _line(kind, number):
    """A claim payee line of the destination, for claim_payees._same_number."""
    from . import claim_payees as cp

    if kind == PHONE:
        return frappe._dict(payee_type=cp.PHONE, phone=number)
    return frappe._dict(payee_type=cp.TILL if kind == TILL else cp.PAYBILL, shortcode=number)


def observation(kind, number, received, when=None, payout_doctype=None, payout=None, source=SMS, salary=False,
                bank=None, employee=False):
    """What one payout (or SMS) says about a destination, for ``merge``: _dict(key, destination_type,
    number, mpesa_name, received, when, payout_doctype, payout, source, salary, bank, employee). None
    when it names no one, or names another number (the number in front of M-Pesa's name is not this
    one's: claim_payees._same_number, a masked phone by the digits shown). The text is kept as plain
    text (``plain``: a name of HTML only names no one). ``bank``: None to read it from M-Pesa Bank
    Paybill (a paybill). ``employee``: the payout paid an Employee party (a phone only)."""
    from . import claim_payees as cp

    key = key_of(kind, number)
    received = plain(received)
    if not key or not received:
        return None
    kind, number = split_key(key)
    name = plain(cp._without_number(received))
    if not name:
        return None
    prefix = cp.number_prefix(received)
    if prefix and not cp._same_number(prefix, _line(kind, number)):
        return None  # M-Pesa named another number: not this destination's name
    if bank is None:
        bank = kind == PAYBILL and is_bank_paybill(number)
    return frappe._dict(key=key, destination_type=kind, number=number, mpesa_name=name[:DATA_LENGTH],
                        received=received[:DATA_LENGTH], when=_when(when) or now_datetime(),
                        payout_doctype=cstr(payout_doctype).strip() or None, payout=cstr(payout).strip() or None,
                        source=source or SMS, salary=bool(salary), bank=bool(bank),
                        employee=bool(employee) and kind == PHONE)


def b2c_observation(request, source=B2C_RESULT):
    """What a Paid B2C Disbursement Request says (``receiver_public_name`` for its phone), or None.
    A payout whose party is an Employee marks the phone ``employee_payee``; so does a party-less one
    whose row names the Employee it pays (a bulk sheet's: partyless.payee_employee)."""
    if _get(request, "status") != "Paid":
        return None
    employee = cstr(_get(request, "party_type")).strip() == EMPLOYEE
    if not employee and not cstr(_get(request, "party_type")).strip() and _get(request, "reference_doctype"):
        from . import partyless

        employee = bool(partyless.payee_employee(request))
    return observation(PHONE, _get(request, "phone_number"), _get(request, "receiver_public_name"),
                       when=_get(request, "transaction_completed_datetime") or _get(request, "creation"),
                       payout_doctype=B2C_REQUEST, payout=_get(request, "name"), source=source,
                       salary=salary_access.is_salary_line(request), employee=employee)


def b2b_kind(command_id) -> str | None:
    return {BUY_GOODS: TILL, PAY_BILL: PAYBILL}.get(cstr(command_id).strip())


def b2b_observation(request, received=None, source=B2B_RESULT, banks=None):
    """What a Paid B2B request says about its till or paybill (``received``, else its
    ``receiver_public_name``), or None. A payout to an employee for no Expense Claim or Employee
    Advance is a salary (salary_access.has_salaries)."""
    if _get(request, "status") != "Paid":
        return None
    kind = b2b_kind(_get(request, "command_id"))
    number = cstr(_get(request, "receiver_shortcode")).strip()
    links = {f: _get(request, f) for f in ("employee", *salary_access.B2B_DOCUMENT_FIELDS)}
    bank = None if banks is None else (kind == PAYBILL and number in banks)
    return observation(kind, number, received or _get(request, "receiver_public_name"),
                       when=_get(request, "trans_completed_time") or _get(request, "creation"), payout_doctype=B2B,
                       payout=_get(request, "name"), source=source,
                       salary=salary_access.has_salaries(frappe._dict(links, doctype=B2B)), bank=bank)


def _fresh(seen, record=None) -> dict:
    """A record started from ``seen``; a phone once paid to an Employee stays marked so (``record``)."""
    employee = bool(seen.get("employee")) or bool(record and cint(record.get("employee_payee")))
    return {"destination_type": seen.destination_type, "number": seen.number, "mpesa_name": plain(seen.mpesa_name),
            "received": plain(seen.received), "first_seen": seen.when, "last_seen": seen.when, "times_paid": 1,
            "last_payout_doctype": seen.payout_doctype, "last_payout": seen.payout, "source": seen.source,
            "salary_only": 1 if seen.salary else 0, "bank": 1 if seen.bank else 0,
            "employee_payee": 1 if employee else 0}


def merge(record, seen) -> dict | None:
    """The record's values after ``seen`` (pure), or None when ``seen`` changes nothing:
    - a new destination starts with it;
    - an SMS's name (round 4.5 review) only starts a record: it never changes one, and a Result's
      name replaces it (the record starts again from the Result);
    - its payout is the record's last payout already (the same result again; each Result is recorded
      once by its caller, after the guard that ignores a re-sent one: this only catches a repeat of
      the latest);
    - a salary on a record that holds payouts that are no salary is left out (what others see never
      comes from a salary; the phone is marked paid to an Employee all the same); the first payout
      that is no salary on a salary-only record starts it again;
    - otherwise it counts once more; the name, the text, the last payout and the source are the
      latest payout's (one recorded late, older than the last seen, only counts and may be the first).
    A phone once paid to an Employee party stays ``employee_payee``. Names are kept as plain text."""
    if not record:
        return _fresh(seen)
    if seen.source == SMS:
        return None  # an SMS names a number no payout named yet, or nothing
    if not confirmed(record):
        return _fresh(seen, record)  # M-Pesa's own name now, for the SMS's
    employee = 1 if cint(record.get("employee_payee")) or seen.get("employee") else 0
    last_payout = (cstr(record.get("last_payout_doctype")), cstr(record.get("last_payout")))
    if seen.payout and last_payout == (cstr(seen.payout_doctype), cstr(seen.payout)):
        return None
    salary_only = bool(cint(record.get("salary_only")))
    if seen.salary and not salary_only:
        if employee == cint(record.get("employee_payee")):
            return None
        return dict({f: record.get(f) for f in VALUES}, employee_payee=employee)
    if salary_only and not seen.salary:
        return _fresh(seen, record)
    out = {f: record.get(f) for f in VALUES}
    out.update(destination_type=seen.destination_type, number=seen.number,
               times_paid=cint(record.get("times_paid")) + 1, bank=1 if seen.bank else 0, employee_payee=employee,
               mpesa_name=plain(out.get("mpesa_name")), received=plain(out.get("received")))
    first, last = _when(record.get("first_seen")), _when(record.get("last_seen"))
    if not first or seen.when < first:
        out["first_seen"] = seen.when
    if not last or seen.when >= last:
        out.update(mpesa_name=seen.mpesa_name, received=seen.received, last_seen=seen.when,
                   last_payout_doctype=seen.payout_doctype, last_payout=seen.payout, source=seen.source)
    return out


# ---------------------------------------------------------------- writing (the system only)
def check_system_write(doc) -> None:
    """M-Pesa Known Name's validate: only the system writes the list (``record``, the migrate)."""
    flags = getattr(doc, "flags", None) or {}
    if flags.get(FLAG) or frappe.flags.in_migrate or frappe.flags.in_patch or frappe.flags.in_install:
        return
    frappe.throw("M-Pesa Known Names are written by the system from M-Pesa's results: nobody adds or changes one "
                 "by hand.", frappe.PermissionError)


def _insert(key, values) -> None:
    doc = frappe.get_doc({"doctype": KNOWN, "name": key, "destination_key": key, **values})
    flags = getattr(doc, "flags", None)
    if flags is not None:
        flags[FLAG] = True
        flags.ignore_links = True  # the last payout may be cancelled since it paid
    doc.insert(ignore_permissions=True)


def _same(a, b) -> bool:
    if isinstance(a, (datetime.date, datetime.datetime)) or isinstance(b, (datetime.date, datetime.datetime)):
        return _when(a) == _when(b)
    if isinstance(a, (int, float, bool)) or isinstance(b, (int, float, bool)):
        return cint(a) == cint(b)
    return cstr(a) == cstr(b)


def _update(seen) -> bool:
    # a locking read: the latest committed record, held until this transaction commits
    current = frappe.db.get_value(KNOWN, seen.key, list(FIELDS), as_dict=True, for_update=True)
    if not current:  # deleted meanwhile (by an Administrator): it starts again
        _insert(seen.key, _fresh(seen))
        return True
    values = merge(current, seen)
    if not values:
        return False
    changed = {k: v for k, v in values.items() if not _same(current.get(k), v)}
    if changed:
        frappe.db.set_value(KNOWN, seen.key, changed)
    return bool(changed)


def record(seen) -> bool:
    """Keep ``seen`` (an ``observation``) in the list, under its record's row lock. True when the
    list changed. A record another job inserts meanwhile is merged into. Raises on an error: callers
    use ``record_b2c``, ``record_b2b`` or ``observe``, which never do."""
    if not seen or not ready():
        return False
    if frappe.db.exists(KNOWN, seen.key):
        return _update(seen)
    frappe.db.savepoint(INSERT_SAVEPOINT)
    try:
        _insert(seen.key, _fresh(seen))
        return True
    except frappe.QueryDeadlockError:
        raise
    except Exception:
        frappe.db.rollback(save_point=INSERT_SAVEPOINT)
        # another job inserted it meanwhile (a locking read sees what it committed)
        if not frappe.db.get_value(KNOWN, seen.key, "name", for_update=True):
            raise
        return _update(seen)


def _safely(what: str, make) -> bool:
    """``record(make())`` under a savepoint: an error rolls back only the list's writes and is logged,
    so the payout's own processing goes on. A lock wait past MariaDB's limit too (round 4.5 review):
    MariaDB undoes only the statement and keeps the transaction, so the B2C Result that waited is
    never lost for a name. A deadlock is raised (the transaction is gone; the payout's job runs
    again)."""
    frappe.db.savepoint(SAVEPOINT)
    try:
        return record(make())
    except frappe.QueryDeadlockError:
        raise
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        frappe.log_error(title="M-Pesa known name not kept", message=f"{what}\n{frappe.get_traceback()}")
        return False


def record_b2c(request) -> bool:
    """A B2C payout just became Paid (its Result: connectors.mpesa.b2c_connector.handle_callback): the
    name M-Pesa paid its phone under is kept. Once per payout; never raises (but a deadlock)."""
    return _safely(f"{B2C_REQUEST} {_get(request, 'name')}", lambda: b2c_observation(request))


def record_b2b(request, received=None, source=B2B_RESULT) -> bool:
    """A B2B payout just became Paid with its receiver's name, or a Transaction Status answer filled
    the name its Result left out (b2b._record_result, b2b_status.fill and settle): the name M-Pesa
    paid its till or paybill under is kept. Once per payout; never raises (but a deadlock)."""
    return _safely(f"{B2B} {_get(request, 'name')}", lambda: b2b_observation(request, received, source))


def observe(kind, number, received, when=None, payout_doctype=None, payout=None, source=SMS,
            salary=False) -> bool:
    """For later features: keep a name M-Pesa gave ``number`` (``kind`` Phone, Till or Paybill, or
    any of the suite's names for it), e.g. from a petty cash phone's M-Pesa SMS ("Confirmed. Ksh500
    sent to JANE DOE 0712345678"): only the name the SMS gave, never one typed. ``received`` may
    carry the number in front ("0712345678 - JANE DOE"): another number's name is not kept. Name the
    payout (or the record the SMS was pasted on) in ``payout_doctype`` and ``payout``: the same one
    again is not counted twice. An SMS's name (source SMS) only starts a record where none exists,
    unconfirmed (``merge``). Never raises (but a deadlock)."""
    return _safely(f"{kind} {number}", lambda: observation(kind, number, received, when, payout_doctype, payout,
                                                            source, salary))


# ---------------------------------------------------------------- the backfill (migrate patch)
def _history(banks) -> list:
    """Every Paid payout with a name, as observations (source History)."""
    seen = []
    b2c = frappe.get_all(B2C_REQUEST, filters={"status": "Paid", "receiver_public_name": ["is", "set"]},
                         fields=["name", "status", "phone_number", "receiver_public_name",
                                 "transaction_completed_datetime", "creation", "reference_doctype", "party_type"])
    seen += [b2c_observation(r, source=HISTORY) for r in b2c]
    links = [f for f in salary_access.B2B_DOCUMENT_FIELDS if frappe.db.has_column(B2B, f)]
    b2b = frappe.get_all(B2B, filters={"status": "Paid", "receiver_public_name": ["is", "set"]},
                         fields=["name", "status", "command_id", "receiver_shortcode", "receiver_public_name",
                                 "trans_completed_time", "creation", "employee", *links])
    seen += [b2b_observation(r, source=HISTORY, banks=banks) for r in b2b]
    return [s for s in seen if s]


def rebuild(observations) -> dict:
    """{key: values}: each destination's record from its observations alone (pure), oldest first."""
    out = {}
    for seen in sorted(observations, key=lambda s: (s.when, cstr(s.payout_doctype), cstr(s.payout))):
        values = merge(out.get(seen.key), seen)
        if values:
            out[seen.key] = values
    return out


def backfill() -> dict:
    """The migrate patch: every destination's record rebuilt from the Paid B2C Disbursement Requests
    and Mpesa B2B Payment Requests that name whom M-Pesa paid (the latest name, first and last seen,
    the count), source History. Idempotent: running it again writes the same values (a record that
    already holds them is not written, whatever its source says). A record of a destination no payout
    names (an SMS's) is left alone. Two reads and one write per destination: a few seconds for thousands
    of payouts. Returns the counts."""
    if not ready():
        return {"inserted": 0, "updated": 0, "unchanged": 0}
    built = rebuild(_history(bank_paybills()))
    existing = {r.name: r for r in frappe.get_all(KNOWN, fields=list(FIELDS))}
    counts = {"inserted": 0, "updated": 0, "unchanged": 0}
    for key in sorted(built):
        values, current = built[key], existing.get(key)
        if current is None:
            _insert(key, values)
            counts["inserted"] += 1
            continue
        changed = {k: v for k, v in values.items() if not _same(current.get(k), v)}
        if set(changed) - {"source"}:
            frappe.db.set_value(KNOWN, key, changed, update_modified=False)
            counts["updated"] += 1
        else:  # the same name and counts (a Result recorded them already: its source is kept)
            counts["unchanged"] += 1
    return counts


# ---------------------------------------------------------------- reading
def _known(row, banks) -> frappe._dict:
    bank = row.destination_type == PAYBILL and cstr(row.number).strip() in banks
    return frappe._dict(key=row.name, name=plain(row.mpesa_name), times_paid=cint(row.times_paid),
                        first_seen=row.first_seen, last_seen=row.last_seen, last_payout=row.last_payout,
                        last_payout_doctype=row.last_payout_doctype, salary_only=cint(row.salary_only),
                        bank=1 if bank else 0, received=plain(row.received), source=row.source,
                        destination_type=row.destination_type, number=row.number,
                        employee_payee=cint(row.get("employee_payee")), confirmed=1 if confirmed(row) else 0)


def lookup(keys, banks=None) -> dict:
    """{key: _dict(name, times_paid, first_seen, last_seen, last_payout, last_payout_doctype,
    salary_only, bank, received, source, destination_type, number, employee_payee, confirmed)} of the
    destinations M-Pesa paid before, by the list's key (``known_key`` of each of ``keys``:
    'PB:400200:ACC' answers as 'PAYBILL:400200'). A destination never paid is not in it. ``bank`` is
    read from the current M-Pesa Bank Paybill list (``banks``: that list, read once by the caller),
    not the record's, so a bank added since counts. No permission check: callers show a salary-only
    record only to who may see salaries, and an employee's phone only to who may read claims
    (``visible``)."""
    wanted = sorted({k for k in (known_key(x) for x in keys or []) if k})
    if not wanted or not ready():
        return {}
    rows = frappe.get_all(KNOWN, filters={"name": ["in", wanted]}, fields=list(FIELDS))
    if banks is None:
        banks = bank_paybills() if any(r.destination_type == PAYBILL for r in rows) else set()
    return {r.name: _known(r, banks) for r in rows}


def _words(name) -> set:
    return pa._tokens(name)


def verdict(typed_name, known) -> str:
    """The name typed (or expected) against M-Pesa's (pure): "new" when M-Pesa never paid the
    destination (``known`` None or nameless) or only an SMS named it (``confirmed``: no payout
    confirmed the name, so it is never a match); "match" for a bank's paybill (M-Pesa shows the
    bank's name, so there is nothing to compare), when nothing was typed, or when the names match
    (party_accounts.names_match: every word of the shorter in the longer); "partial" when one word
    was typed and M-Pesa's name has more ("Jane" for JANE WANJIKU: the first name of anyone); else
    "differs"."""
    if not known:
        return NEW
    if cint(known.get("bank")):
        return MATCH
    name = cstr(known.get("name")).strip()
    if not name or not confirmed(known):
        return NEW
    typed = cstr(typed_name).strip()
    if not typed:
        return MATCH
    if pa.names_match(typed, name):
        return PARTIAL if len(_words(typed)) == 1 and len(_words(name)) > 1 else MATCH
    return DIFFERS


def visible(known, can_see_salaries: bool, can_see_employees: bool = True) -> bool:
    """Whether a reader may see this record's name: a salary-only one only who may see salaries, a
    phone paid to an Employee only who may read Expense Claims or see salaries (``can_see_employees``)."""
    if not known:
        return True
    if cint(known.get("salary_only")) and not can_see_salaries:
        return False
    return not (cint(known.get("employee_payee")) and not can_see_employees
                and cstr(known.get("destination_type") or PHONE) == PHONE)


def _day(value) -> str:
    when = _when(value)
    if not when:
        return ""
    text = f"{when.day} {when.strftime('%b')}"
    return text if when.year == now_datetime().year else f"{text} {when.year}"


def _paid(info) -> str:
    times = cint(info.get("times_paid"))
    text = f"paid {times} time{'' if times == 1 else 's'}"
    day = _day(info.get("last_seen"))
    return f"{text}, last {day}" if day else text


def badge(info) -> dict | None:
    """What the badge says, and its colour (green, amber, orange, blue, gray), for ``describe``'s
    answer:
    - green "M-Pesa: NAME · paid N times, last 28 Oct" (the name matches, or nothing was typed);
    - amber "M-Pesa: NAME · only one name given: check it is this person" (partial);
    - orange "M-Pesa knows this number as NAME" (the typed name differs);
    - blue "New number: never paid; check it with the person", or "From SMS, not confirmed by a
      payout: NAME" (only an SMS named it);
    - gray "M-Pesa shows the bank's name only" (a bank's paybill), or "Paid before" (a salary-only
      name, or an employee's phone, to who may not see it).
    None for a destination that is no phone, till or paybill. The text is plain: screens escape it."""
    status, name = info.get("status"), plain(info.get("name"))
    if status == INVALID:
        return None
    if cint(info.get("bank")):
        text = "M-Pesa shows the bank's name only"
        if name:
            text += f": {name}"
        if cint(info.get("times_paid")):
            text += f" · {_paid(info)}"
        return {"colour": "gray", "text": text}
    if status == HIDDEN:
        return {"colour": "gray", "text": "Paid before"}
    if status == NEW_STATUS:
        return {"colour": "blue", "text": "New number: never paid; check it with the person"}
    if status == KNOWN_STATUS and not cint(info.get("confirmed", 1)):
        return {"colour": "blue", "text": f"From SMS, not confirmed by a payout: {name}"}
    if info.get("verdict") == DIFFERS:
        return {"colour": "orange", "text": f"M-Pesa knows this number as {name}"}
    if info.get("verdict") == PARTIAL:
        return {"colour": "amber", "text": f"M-Pesa: {name} · only one name given: check it is this person"}
    return {"colour": "green", "text": f"M-Pesa: {name} · {_paid(info)}"}


def describe(key, known=None, typed_name=None, can_see_salaries: bool = True, banks=None,
             can_see_employees: bool = True) -> dict:
    """What a reader is shown about one destination: key, destination_type, number, typed_name, bank,
    status (known, new, hidden, invalid), verdict, and for a known one they may see (``visible``):
    name, times_paid, first_seen, last_seen, last_payout_doctype, last_payout, salary_only, confirmed
    (0: only an SMS named it); and the badge."""
    kind, number = split_key(key)
    typed = cstr(typed_name).strip() or None
    out = {"key": known_key(key), "destination_type": kind, "number": number, "typed_name": typed, "bank": 0}
    if not out["key"]:
        out.update(status=INVALID, verdict=None)
    elif known and not visible(known, can_see_salaries, can_see_employees):
        out.update(status=HIDDEN, verdict=None, bank=cint(known.get("bank")))
    elif known:
        out.update(status=KNOWN_STATUS, name=plain(known.get("name")), times_paid=cint(known.get("times_paid")),
                   first_seen=cstr(known.get("first_seen")) or None, last_seen=cstr(known.get("last_seen")) or None,
                   last_payout_doctype=known.get("last_payout_doctype"), last_payout=known.get("last_payout"),
                   salary_only=cint(known.get("salary_only")), bank=cint(known.get("bank")),
                   confirmed=1 if confirmed(known) else 0, verdict=verdict(typed, known))
    else:
        bank = kind == PAYBILL and is_bank_paybill(number, banks)
        out.update(status=NEW_STATUS, bank=1 if bank else 0, verdict=verdict(typed, {"bank": 1} if bank else None))
    out["badge"] = badge(out)
    return out


# ---------------------------------------------------------------- the Send via M-Pesa summary
def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary(targets, acknowledge: bool = True, can_see_salaries: bool | None = None,
            can_see_employees: bool | None = None) -> dict | None:
    """The names on a confirm screen (money_out.send_confirm, and Pay Selected: money_out.schedule).
    ``targets``: per row of the screen, (key, typed or expected name), or None for a row that pays no
    phone, till or paybill. Returns None when no row has one; else:
    - ``rows``: per row, ``describe``'s answer (None for a row without a destination);
    - ``counts``: {match, partial, differs, new, bank, hidden} (an SMS's name alone counts as new);
    - ``header``: "60 known and matching · 2 matching one name only · 4 under another name · 5 new";
    - ``acknowledge``: when ``acknowledge`` and a row differs (in part too), is new or hides its
      name, what the sender ticks before Confirm works ("I checked the 4 different names and 5 new
      numbers"), which confirm_send and confirm_payment require too; else None."""
    targets = list(targets or [])
    if not any(t and t[0] for t in targets):
        return None
    if can_see_salaries is None:
        can_see_salaries = salary_access.can_see_salaries()
    if can_see_employees is None:
        can_see_employees = can_see_salaries or sees_employee_phones()
    banks = bank_paybills() if any(t and cstr(t[0]).upper().startswith(("PAYBILL", "PB")) for t in targets) else set()
    found = lookup((t[0] for t in targets if t and t[0]), banks)
    rows, counts = [], {"match": 0, "partial": 0, "differs": 0, "new": 0, "bank": 0, "hidden": 0}
    for target in targets:
        if not target or not target[0]:
            rows.append(None)
            continue
        key, typed = target[0], target[1] if len(target) > 1 else None
        info = describe(key, found.get(known_key(key)), typed, can_see_salaries, banks, can_see_employees)
        rows.append(info)
        if info["status"] == INVALID:
            counts["new"] += 1  # a number no destination reads as: nobody can vouch for it
        elif info["bank"]:
            counts["bank"] += 1
        elif info["status"] == HIDDEN:
            counts["hidden"] += 1
        else:
            counts[info["verdict"]] += 1
    parts = []
    if counts["match"]:
        parts.append(f"{counts['match']} known and matching")
    if counts["partial"]:
        parts.append(f"{counts['partial']} matching one name only")
    if counts["differs"]:
        parts.append(f"{counts['differs']} under another name")
    if counts["new"]:
        parts.append(f"{counts['new']} new")
    if counts["bank"]:
        parts.append(_plural(counts["bank"], "bank account (M-Pesa shows the bank's name only)",
                             "bank accounts (M-Pesa shows the bank's name only)"))
    if counts["hidden"]:
        parts.append(f"{counts['hidden']} paid before (name not shown to you)")
    checked = []
    differ = counts["differs"] + counts["partial"]  # one name given is checked like a name that differs
    if differ:
        checked.append(_plural(differ, "different name", "different names") if not counts["partial"] else
                       _plural(differ, "name that differs or matches in part", "names that differ or match in part"))
    if counts["new"]:
        checked.append(_plural(counts["new"], "new number", "new numbers"))
    if counts["hidden"]:
        checked.append(_plural(counts["hidden"], "number whose name I may not see",
                               "numbers whose names I may not see"))
    ask = None
    if acknowledge and checked:
        ask = "I checked the " + (", ".join(checked[:-1]) + " and " + checked[-1] if len(checked) > 1 else checked[0])
        ask += " with the people paid."
    return {"rows": rows, "counts": counts, "header": " · ".join(parts), "acknowledge": ask}


# ---------------------------------------------------------------- the desk badges
def _items(destinations) -> list:
    if isinstance(destinations, str):
        destinations = json.loads(destinations or "[]")
    if isinstance(destinations, dict):
        destinations = [dict(v, id=k) if isinstance(v, dict) else {"id": k, "key": v} for k, v in destinations.items()]
    out = []
    for i, item in enumerate(destinations or []):
        if isinstance(item, str):
            item = {"id": item, "key": item}
        if not isinstance(item, dict):
            continue
        item = frappe._dict(item)
        item.id = cstr(item.get("id")).strip() or str(i)
        out.append(item)
    return out


def _readable(doctype, name, cache) -> bool:
    key = ("perm", doctype, name)
    if key not in cache:
        try:
            cache[key] = bool(frappe.has_permission(doctype, "read", doc=name))
        except Exception:
            cache[key] = False
    return cache[key]


def _account(doctype, name, cache):
    """An approved Supplier or Employee M-Pesa Account the reader may read (its key and expected name)."""
    doctype, name = cstr(doctype).strip(), cstr(name).strip()
    if doctype not in ACCOUNTS or not name or not _readable(doctype, name, cache):
        return None
    return frappe.db.get_value(doctype, name, ["destination_key", "account_type", "phone", "shortcode",
                                               "expected_name"], as_dict=True)


def _payee_name(line, cache) -> str | None:
    """The name a claim payee line's claimant gave (Name Given, else the Payee Name), if the reader may
    read its claim."""
    row = frappe.db.get_value(CLAIM_PAYEE, cstr(line).strip(), ["parent", "payee_name", "typed_name"],
                              as_dict=True) if cstr(line).strip() else None
    if not row or not _readable(CLAIM, row.parent, cache):
        return None
    return cstr(row.typed_name or row.payee_name).strip() or None


def _party_name(party_type, party, cache) -> str | None:
    field = PARTY_NAME.get(cstr(party_type))
    party = cstr(party).strip()
    if not field or not party or not _readable(party_type, party, cache):
        return None
    return cstr(frappe.db.get_value(party_type, party, field) or party).strip() or None


def _number(item) -> str:
    for field in ("number", "phone", "shortcode"):
        if cstr(item.get(field)).strip():
            return cstr(item.get(field)).strip()
    # a schedule line's destination: '888880 / ACME-1' or '2547...'
    return cstr(item.get("destination")).split("/")[0].strip()


def resolve(item, cache=None) -> tuple:
    """(key, typed or expected name) of one destination the desk asks about: ``key`` (any destination
    key), or ``type`` (or ``route``, ``command_id``) with ``number`` (or ``phone``, ``shortcode``, a
    schedule line's ``destination``); the name ``typed_name``, else an approved account's expected
    name (``account_doctype`` and ``account``: also its destination when none is given), a claim
    payee line's (``claim_payee``) or the party's (``party_type`` and ``party``). Records the reader
    may not read give nothing."""
    cache = {} if cache is None else cache
    key = known_key(item.get("key")) if item.get("key") else None
    if not key:
        kind = item.get("type") or item.get("route") or item.get("command_id")
        key = key_of(kind, _number(item))
    typed = cstr(item.get("typed_name")).strip() or None
    acc = None
    if item.get("account") and (not typed or not key):
        acc = _account(item.get("account_doctype"), item.get("account"), cache)
    if acc:
        key = key or known_key(acc.destination_key) or key_of(acc.account_type, acc.phone or acc.shortcode)
        typed = typed or cstr(acc.expected_name).strip() or None
    if not typed and item.get("claim_payee"):
        typed = _payee_name(item.get("claim_payee"), cache)
    if not typed and item.get("party_type") and item.get("party"):
        typed = _party_name(item.get("party_type"), item.get("party"), cache)
    return key, typed


@frappe.whitelist()
def for_destinations(destinations=None) -> dict:
    """The desk badges (public/js/mpsa_known_names.js): {id: ``describe``'s answer} for each of
    ``destinations`` (a list, or its JSON: key strings, or dicts with an ``id`` and what ``resolve``
    reads). For who may read M-Pesa Known Names: the roles that prepare, approve or send payouts. A
    salary-only name is shown only to who may see salaries (salary_access), an employee's phone only
    to who may read Expense Claims or see salaries (``sees_employee_phones``): the others get "Paid
    before" and no name."""
    if not frappe.has_permission(KNOWN, "read"):
        frappe.throw("You may not read M-Pesa Known Names.", frappe.PermissionError)
    items = _items(destinations)
    if len(items) > LIMIT:
        frappe.throw(f"Ask for at most {LIMIT} numbers at a time.")
    cache = {}
    resolved = [(item.id, *resolve(item, cache)) for item in items]
    banks = bank_paybills() if any(key and key.startswith("PAYBILL:") for _id, key, _typed in resolved) else set()
    found = lookup((key for _id, key, _typed in resolved if key), banks)
    see = salary_access.can_see_salaries()
    employees = see or sees_employee_phones()
    return {item_id: describe(key, found.get(key), typed, see, banks, employees) for item_id, key, typed in resolved}


# ---------------------------------------------------------------- permission hooks (hooks.py)
def query_conditions(user=None, doctype=None) -> str:
    """permission_query_conditions of M-Pesa Known Name: a salary-only record is listed only for who
    may see salaries; a phone paid to an Employee (round 4.5 review) only for who may read Expense
    Claims or see salaries. Tills and paybills are listed to all who read the list."""
    user = user or frappe.session.user
    if salary_access.can_see_salaries(user):
        return ""
    conditions = [f"COALESCE(`tab{KNOWN}`.salary_only, 0) = 0"]
    if not sees_employee_phones(user):
        conditions.append(f"(COALESCE(`tab{KNOWN}`.employee_payee, 0) = 0 "
                          f"OR COALESCE(`tab{KNOWN}`.destination_type, '') != '{PHONE}')")
    return " AND ".join(conditions)


def has_permission(doc, ptype=None, user=None, debug=False) -> bool:
    """has_permission of M-Pesa Known Name: a salary-only record only for who may see salaries, a
    phone paid to an Employee only for who may read Expense Claims or see salaries. True leaves the
    role permissions as they are (v16 needs True, not None)."""
    user = user or frappe.session.user
    if cint(doc.get("salary_only")) and not salary_access.can_see_salaries(user):
        return False
    if cint(doc.get("employee_payee")) and cstr(doc.get("destination_type") or PHONE) == PHONE:
        return sees_employee_phones(user)
    return True
