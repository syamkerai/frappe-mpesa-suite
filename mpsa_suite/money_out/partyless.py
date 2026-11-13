"""Party-less B2C payouts: B2C Payment Disbursement rows that pay a person no ERPNext party names.

Every B2C row used to name an Employee or a Supplier, and the payout machinery reads that party
to check the row, book it, reconcile its statement line and show it before the send. Round 4.5
pays people that are no party: a bulk payout sheet's casuals and overtime, a petty cash phone's
top-ups. A DocType that pays such rows registers a handler here and owns those parts for its own
rows. A row of any other DocType goes through the code exactly as before: every hook below
returns at once when the row's DocType has no handler.

Registration, in hooks.py (read with ``frappe.get_hooks``; the last app's path wins):

    mpsa_partyless_payouts = {"M-Pesa Bulk Payout": "mpsa_suite.money_out.bulk_payout.HANDLER"}

The path names a handler object (or a class, made with no arguments). ``register(doctype,
handler)`` does the same in-process (tests, a console). Subclass ``Handler``: it has the optional
methods' defaults.

What a party-less payout is
- The B2C Payment Disbursement (the parent) pays ``transaction_to_pay_against`` = the registered
  DocType. It still needs ``company``, ``posting_date``, ``mode_of_payment`` ("Mpesa-<settings>")
  and ``paid_from`` (the shortcode's Utility GL), and ``paid_from_account_currency`` /
  ``paid_to_account_currency`` ("KES"). ``party_type`` and ``paid_to`` stay empty: the rows name
  no party, and the handler books each row on its own accounts.
- Each row: ``reference_doctype`` the registered DocType, ``reference_name`` the paying document,
  ``reference_row`` the paying document's child row (a sheet's row, a top-up), ``partyb`` the
  phone, ``allocated_amount``. No ``party`` or ``party_type``, no claim payee, no schedule line.
  ``outstanding_amount`` may stay empty.
- The suite checks what is generic: the row names its paying row and no party; two rows of one
  disbursement never pay one paying row; a paying row is never paid while another payout of it
  is paid or on its way (any status but Failed), at the send under the paying document's lock,
  and again at a retry; the sender is not paid (money_out.self_pay). A B2C Disbursement Request
  made by hand never pays such a row.

The handler (``h`` below; ``row`` is the B2C Payment Disbursement Reference: the child document in
``check_row``, ``book`` and ``booked``, a frappe._dict read from the database in ``on_result`` and
``check_retry``; ``parent`` its B2C Payment Disbursement; ``request`` its B2C Disbursement Request):
- ``h.check_row(row, parent)`` (required): raise (frappe.throw) when the row is wrong for the
  paying document: it exists and may be paid, the phone and amount are the paying row's, the
  company is the parent's, the paying row is not paid or on its way by the DocType's own record.
  Runs on every save of the disbursement and at the send (the row's validate), after the suite's
  own checks, never for an amended copy of a row its original sent.
- ``h.on_result(row, request)`` (optional): the payout has a result (``row.payment_status`` Paid
  or Failed, ``row.reference_no`` the receipt; ``request.receiver_public_name``,
  ``request.transaction_completed_datetime``, ``request.result_desc`` / ``error_message``). Copy
  them to the paying row. Runs in the result's job, before booking, and again whenever the job
  runs again (the 15-minute sweeps): it must be safe to repeat. An error is rolled back to a
  savepoint and logged; it never stops the booking.
- ``h.book(row)`` (required): book the Paid payout. Post the entry and return None, or return
  ``DEFERRED`` / ``deferred(note)`` when the DocType books it later with others (a sheet's one
  journal). A deferred payout stays unbooked (``accounting_booked`` 0, the note on
  ``accounting_error`` after "Waiting: "), and ``retry_unbooked_payouts`` calls ``book`` again
  every 15 minutes for 30 days. Raise to refuse: the error is recorded on the payout, logged and
  sent to the Notify Role, and the sweep tries again. Runs under the paying document's lock
  (owed.booking_lock), at most once at a time per document. An entry that books other rows' payouts
  too (a sheet's journal) marks them booked when the sweep next asks ``booked`` for each.
- ``h.booked(row)`` (required): the docstatus of the live entry that books this payout (1
  submitted, 0 draft), or None when there is none yet. A payout is marked booked when it is 1.
- ``h.statement_entry(request)`` (optional): ``(voucher_type, voucher_name, amount)``: the
  submitted entry that books this payout on the shortcode's bank GL and the part of it this
  payout's statement line takes (its amount), or None (not booked yet, or the entry carries the
  receipt itself and the usual rule finds it: a Payment Entry with Reference No, a Journal Entry
  with Cheque No). ERPNext v15 and v16 allocate one voucher in parts to several Bank
  Transactions. ``request`` carries ``reference_row``.
- ``h.describe(row)`` (optional): what the Send via M-Pesa summary shows for the row, a dict:
  ``receiver`` (who is paid), ``party`` (a second line, in brackets: the project, the purpose),
  ``document`` (e.g. "M-Pesa Bulk Payout MBP-26-11-0001, row #3"), ``expected_name`` (shown as
  the name on the approved account: leave it out unless the row has one). Prints use
  ``receiver`` for a row M-Pesa paid under no name.
- ``h.self_pay_rows(parent)`` (optional): the people the parent pays, for the self-pay check at
  the send: dicts of ``phone``, ``payee_name``, ``mpesa_name`` (the name M-Pesa paid the phone
  under before) and ``label``. Default: each row's phone, its ``describe`` receiver and expected
  name.
- ``h.check_retry(row, request)`` (optional): a failed payout is sent again (Retry on the request
  or its disbursement), after the suite checked that no other payout of the paying row is paid or
  on its way and that the person retrying is not paid. Default: ``h.check_row(row, parent)``.
- ``h.closed_period_hint(row, receipt)`` (optional): what staff do when ERPNext refuses the
  entry's date (a closed period). Default: a generic sentence.
- ``h.send_warnings(parent)`` (optional): what the Send via M-Pesa summary warns of, which does
  not stop the send (a short Utility balance): a list of sentences. Default: none.
"""

import importlib

import frappe
from frappe.utils import cint, cstr, flt

HOOK = "mpsa_partyless_payouts"
DISB = "B2C Payment Disbursement"
ROW = "B2C Payment Disbursement Reference"
REQUEST = "B2C Disbursement Request"
# What a deferred booking writes on the payout before its note (accounting_error): a waiting
# payout is not an error, and the error log treats such a note as none (b2c_postprocess_service)
WAITING = "Waiting: "
ROW_FIELDS = ("name", "idx", "parent", "parenttype", "reference_doctype", "reference_name", "reference_row",
              "party_type", "party", "partyb", "allocated_amount", "payment_status", "reference_no", "reference_date",
              "b2c_disbursement_request")
SAVEPOINT = "mpesa_partyless_result"

# in-process registrations (register): {doctype: handler}; hooks are read on every call
_REGISTERED = {}
# handlers made from hook paths, by path
_LOADED = {}


class Deferred:
    """What ``Handler.book`` returns when the DocType books the payout later, with others."""

    def __init__(self, note: str = ""):
        self.note = cstr(note).strip()

    def __repr__(self) -> str:
        return f"Deferred({self.note!r})"


DEFERRED = Deferred()


def deferred(note: str) -> Deferred:
    """``Handler.book``'s answer for a payout booked later, with the reason shown on the payout."""
    return Deferred(note)


def is_deferred(result) -> bool:
    return isinstance(result, Deferred)


class Handler:
    """Base of a party-less payout handler: the optional methods' defaults (module docstring)."""

    def check_row(self, row, parent) -> None:
        raise NotImplementedError

    def on_result(self, row, request) -> None:
        return None

    def book(self, row):
        raise NotImplementedError

    def booked(self, row) -> int | None:
        raise NotImplementedError

    def statement_entry(self, request):
        return None

    def describe(self, row) -> dict:
        return {}

    def self_pay_rows(self, parent):
        return None  # the registry's default (module docstring)

    def check_retry(self, row, request) -> None:
        parent = frappe.get_doc(DISB, row.parent)
        child = next((r for r in parent.get("references") or [] if r.get("name") == row.name), row)
        self.check_row(child, parent)

    def closed_period_hint(self, row, receipt: str) -> str | None:
        return None

    def send_warnings(self, parent) -> list:
        return []


# ---------------------------------------------------------------- the registry
def register(doctype: str, handler) -> None:
    """Register ``handler`` for ``doctype`` in this process (hooks.py is the usual way)."""
    _REGISTERED[doctype] = handler


def unregister(doctype: str) -> None:
    _REGISTERED.pop(doctype, None)


def _hooked() -> dict:
    """{doctype: path} from the apps' hooks (the last app's path wins)."""
    get_hooks = getattr(frappe, "get_hooks", None)
    if not get_hooks:
        return {}
    try:
        found = get_hooks(HOOK)
    except Exception:
        frappe.log_error(title="M-Pesa party-less payouts: hooks unreadable", message=frappe.get_traceback())
        return {}
    if not isinstance(found, dict):
        return {}
    out = {}
    for doctype, paths in found.items():
        paths = paths if isinstance(paths, (list, tuple)) else [paths]
        paths = [cstr(p).strip() for p in paths if cstr(p).strip()]
        if doctype and paths:
            out[doctype] = paths[-1]
    return out


def _load(path: str):
    """The handler a hook path names, made once per process; None when it cannot be (logged once
    per process: every row's save asks again)."""
    if path in _LOADED:
        return _LOADED[path]
    try:
        module, _, attr = path.rpartition(".")
        handler = getattr(importlib.import_module(module), attr)
        if isinstance(handler, type):
            handler = handler()
    except Exception:
        frappe.log_error(title=f"M-Pesa party-less payout handler not loaded: {path}"[:140],
                         message=frappe.get_traceback())
        handler = None
    _LOADED[path] = handler
    return handler


def handlers() -> dict:
    """{doctype: handler} of every DocType whose B2C rows are party-less. A handler that cannot be
    loaded is left out (logged): its rows are then refused as rows with no party, never paid
    without their checks."""
    out = {}
    for doctype, path in _hooked().items():
        handler = _load(path)
        if handler is not None:
            out[doctype] = handler
    out.update(_REGISTERED)
    return out


def handler_for(doctype):
    """The handler of ``doctype``'s party-less rows, or None (a party is needed, as always)."""
    if not doctype:
        return None
    return handlers().get(doctype)


def doctypes() -> list:
    return sorted(handlers())


def _call(handler, method: str, *args, default=None):
    fn = getattr(handler, method, None)
    return fn(*args) if callable(fn) else default


# ---------------------------------------------------------------- the rows (B2C Payment Disbursement Reference)
def _value(row, field):
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(field)
    return getattr(row, field, None)


def _set(row, field, value) -> None:
    if isinstance(row, dict):
        row[field] = value
    else:
        setattr(row, field, value)


def _sent_already(row) -> bool:
    """A row an amend copied from a disbursement that sent it: it is not sent again."""
    request = cstr(_value(row, "b2c_disbursement_request")).strip()
    status = frappe.db.get_value(REQUEST, request, "status") if request else None
    return bool(status) and status != "Failed"


def _parent_of(row, parent=None):
    if parent is not None:
        return parent
    name = cstr(_value(row, "parent")).strip()
    return frappe.get_doc(DISB, name) if name else None


def _paying(row) -> str:
    """How a row names what it pays: 'M-Pesa Bulk Payout MBP-1 row abc12'."""
    return (f"{_value(row, 'reference_doctype')} {_value(row, 'reference_name')} row "
            f"{cstr(_value(row, 'reference_row')).strip() or '(none)'}")


def check_row(row, parent=None) -> None:
    """A party-less row's validate (B2C Payment Disbursement Reference): the suite's checks, then
    its handler's ``check_row``. On a save, also that no other payout pays its paying row; at the
    send that is checked under the paying document's lock (check_rows_not_paid)."""
    handler = handler_for(_value(row, "reference_doctype"))
    if not handler:
        return
    idx = _value(row, "idx")
    if not cstr(_value(row, "reference_name")).strip() or not cstr(_value(row, "reference_row")).strip():
        frappe.throw(f"Row #{idx} pays {_value(row, 'reference_doctype')}: it must name the document and its row "
                     "it pays (Paying Row). Make the payout from that document.")
    if cstr(_value(row, "party")).strip() or cstr(_value(row, "party_type")).strip():
        frappe.throw(f"Row #{idx} pays {_paying(row)}: it names no Party or Party Type. The document says who is "
                     "paid.")
    if cstr(_value(row, "claim_payee")).strip() or cstr(_value(row, "mpesa_schedule_line")).strip():
        frappe.throw(f"Row #{idx} pays {_paying(row)}: it is not an Expense Claim payee or a payment schedule line.")
    # no approved party account is named: the name M-Pesa paid is the paying document's to check
    for field in ("payee_account_doctype", "payee_account"):
        if _value(row, field):
            _set(row, field, None)
    if _sent_already(row):
        return  # an amended copy of a row its original sent: not sent again
    parent = _parent_of(row, parent)
    if parent is not None:
        key = _key(row)
        twice = [r for r in parent.get("references") or [] if _key(r) == key]
        if len(twice) > 1:
            rows = ", ".join("#" + cstr(_value(r, "idx")) for r in twice)
            frappe.throw(f"Rows {rows} pay the same {_paying(row)}: a row is paid once.")
        if cint(parent.get("docstatus")) != 1:
            problem = _paid_elsewhere(row, exclude_parent=parent.get("name"))
            if problem:
                frappe.throw(problem)
    handler.check_row(row, parent)


def _key(row) -> tuple:
    return (cstr(_value(row, "reference_doctype")), cstr(_value(row, "reference_name")),
            cstr(_value(row, "reference_row")).strip())


def _others(row, exclude_parent=None, exclude_row=None, for_update: bool = False) -> list:
    """Other payouts of the row's paying row that are paid or on their way: rows of a submitted
    disbursement, or with a submitted or Paid payout, whose payout did not fail. A draft is not one
    (whichever is sent first takes the row); a Paid payout counts however it was cancelled since:
    the money left. ``for_update``: a locking read (what is committed now)."""
    doctype, name, paying = _key(row)
    locking = "FOR UPDATE" if for_update and frappe.db.db_type == "mariadb" else ""
    return frappe.db.sql(
        f"""
        SELECT ref.name, ref.idx, ref.parent, ref.payment_status, req.name AS request, req.status AS request_status
        FROM `tab{ROW}` ref
        JOIN `tab{DISB}` d ON d.name = ref.parent
        LEFT JOIN `tab{REQUEST}` req ON req.name = ref.b2c_disbursement_request
        WHERE ref.reference_name = %(name)s AND ref.reference_doctype = %(doctype)s
          AND ref.reference_row = %(paying)s
          AND ref.parent != %(parent)s AND ref.name != %(row)s
          AND (d.docstatus = 1 OR req.docstatus = 1 OR COALESCE(req.status, '') = 'Paid')
          AND COALESCE(req.status, ref.payment_status, '') != 'Failed'
        {locking}
        """,
        {"name": name, "doctype": doctype, "paying": paying, "parent": exclude_parent or "",
         "row": exclude_row or ""},
        as_dict=True,
    )


def _status(other) -> str:
    """Another payout's state: its request's, else its row's (a row whose request is not made yet)."""
    return other.request_status or other.payment_status or "Initiated"


def _paid_elsewhere(row, exclude_parent=None, exclude_row=None, for_update: bool = False) -> str | None:
    others = _others(row, exclude_parent, exclude_row, for_update)
    if not others:
        return None
    listed = ", ".join(f"{o.parent} row #{o.idx} ({_status(o)})" for o in others[:3])
    paid = any(_status(o) == "Paid" for o in others)
    return (f"Row #{_value(row, 'idx')} pays {_paying(row)}, which is {'paid' if paid else 'being paid'} by "
            f"{listed}: a row is never paid twice.")


def check_rows_not_paid(parent, rows, for_update: bool = True) -> None:
    """The send's check (B2C Payment Disbursement.validate_no_double_payout), under the paying
    document's lock: each row pays its own paying row, never the document's total, so the other
    rows' payouts of the document are not this one's; only another payout of the same paying row
    refuses it."""
    for row in rows:
        problem = _paid_elsewhere(row, exclude_parent=parent.get("name"), for_update=for_update)
        if problem:
            frappe.throw(problem, title="Duplicate Payout")


def _send_rows(parent) -> list:
    rows = parent._rows_to_send() if callable(getattr(parent, "_rows_to_send", None)) else None
    return list(rows if rows is not None else parent.get("references") or [])


def _partyless_rows(parent) -> list:
    """The parent's party-less rows this send pays (an amended copy of a row sent already is not)."""
    if not handlers():
        return []
    rows = [r for r in parent.get("references") or [] if handler_for(_value(r, "reference_doctype"))]
    if not rows:
        return rows
    sending = {id(r) for r in _send_rows(parent)}
    return [r for r in rows if id(r) in sending]


# ---------------------------------------------------------------- who is paid (send summary, self-pay, prints)
def describe(row) -> dict:
    """What the handler says about a party-less row (module docstring); {} for any other row, or
    when the handler fails (logged). Never raises."""
    handler = handler_for(_value(row, "reference_doctype"))
    if not handler:
        return {}
    try:
        found = _call(handler, "describe", row, default={}) or {}
    except Exception:
        frappe.log_error(title="M-Pesa party-less payout not described", message=frappe.get_traceback())
        return {}
    return dict(found)


def receiver_of(row) -> str:
    """The person a party-less row pays, as its handler names them ('' for any other row)."""
    return cstr(describe(row).get("receiver")).strip()


def self_pay_rows(parent) -> list:
    """The people the parent's party-less rows (to send) pay: its handler's ``self_pay_rows``, else
    each row's phone, receiver and expected name."""
    rows = _partyless_rows(parent)
    if not rows:
        return []
    handler = handler_for(_value(rows[0], "reference_doctype"))
    own = _call(handler, "self_pay_rows", parent)
    if own is not None:
        return list(own)
    out = []
    for row in rows:
        found = describe(row)
        out.append({"phone": _value(row, "partyb"), "payee_name": found.get("receiver"),
                    "mpesa_name": found.get("expected_name"), "label": f"Row #{_value(row, 'idx')}"})
    return out


def self_pay_problem(parent, user: str | None = None, action: str = "send") -> str | None:
    """Why ``user`` may not send the parent: one of its party-less rows pays their own phone or
    name (money_out.self_pay). None when it pays none, or has no party-less row."""
    rows = self_pay_rows(parent)
    if not rows:
        return None
    from .self_pay import self_pay_problem as problem

    return problem(user or frappe.session.user, rows, action)


def send_warnings(parent) -> list:
    """What the Send via M-Pesa summary warns of for the parent's party-less rows (its handler's
    ``send_warnings``); [] for any other payout, or when the handler fails (logged)."""
    rows = _partyless_rows(parent)
    if not rows:
        return []
    try:
        found = _call(handler_for(_value(rows[0], "reference_doctype")), "send_warnings", parent, default=[]) or []
    except Exception:
        frappe.log_error(title="M-Pesa party-less payout warnings failed", message=frappe.get_traceback())
        return []
    return [cstr(w).strip() for w in found if cstr(w).strip()]


def check_not_own(parent) -> None:
    """The send (B2C Payment Disbursement.before_submit): nobody pays themselves."""
    problem = self_pay_problem(parent, frappe.session.user, "send")
    if problem:
        frappe.throw(problem, frappe.PermissionError)


# ---------------------------------------------------------------- the result and booking
def on_result(ref_name: str | None, request) -> None:
    """After a payout's result is recorded on its row (b2c_response_service): the paying
    document follows it (``h.on_result``). Under a savepoint: an error rolls back only the
    handler's own writes and is logged. A deadlock or a lock wait past MariaDB's limit is raised:
    the transaction is gone, and the job runs again from the sweep."""
    if not ref_name or not handlers():
        return
    row = frappe.db.get_value(ROW, ref_name, list(ROW_FIELDS), as_dict=True)
    handler = handler_for(row.reference_doctype) if row else None
    if not handler or not callable(getattr(handler, "on_result", None)):
        return
    frappe.db.savepoint(SAVEPOINT)
    try:
        handler.on_result(row, request)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
        raise
    except Exception:
        frappe.db.rollback(save_point=SAVEPOINT)
        frappe.log_error(title=f"M-Pesa payout result not copied to {row.reference_doctype}"[:140],
                         message=f"{_value(request, 'name')} ({ref_name})\n{frappe.get_traceback()}")


def book(handler, row):
    """``h.book(row)`` (b2c_postprocess_service._book): None once booked, a Deferred when later."""
    return handler.book(row)


def booked(handler, row) -> int | None:
    value = handler.booked(row)
    return None if value is None else cint(value)


def waiting_note(row, result) -> str:
    note = result.note if is_deferred(result) else ""
    note = note or f"booked later by {_value(row, 'reference_doctype')} {_value(row, 'reference_name')}"
    return f"{WAITING}{note}"


def is_waiting_note(value) -> bool:
    return cstr(value).startswith(WAITING)


def closed_period_hint(handler, row, receipt: str) -> str:
    hint = _call(handler, "closed_period_hint", row, receipt)
    if hint:
        return cstr(hint)
    return (f"Its date is in a closed Accounting Period, in books closed by a Period Closing Voucher or on or before "
            f"Accounts Frozen Till: book it from {_value(row, 'reference_doctype')} "
            f"{_value(row, 'reference_name')} once the period is open, or with a date in an open period.")


# ---------------------------------------------------------------- the retry
def check_retry(request) -> None:
    """A failed party-less payout sent again (B2C Disbursement Request.retry_failed_payment, under
    the paying document's lock), in place of the document-wide checks of what it owes: no other
    payout of its paying row is paid or on its way, the person retrying is not paid by it, and
    the handler's ``check_retry``."""
    ref = cstr(_value(request, "b2c_payment_disbursement_reference")).strip()
    row = frappe.db.get_value(ROW, ref, list(ROW_FIELDS), as_dict=True) if ref else None
    if not row:
        frappe.throw(f"{_value(request, 'name')} names no B2C Payment Disbursement row: it is not sent again.")
    handler = handler_for(row.reference_doctype)
    if not handler:
        frappe.throw(f"{_value(request, 'name')}: its row {row.name} pays {row.reference_doctype}, not "
                     f"{_value(request, 'reference_doctype')}. It is not sent again.")
    problem = _paid_elsewhere(row, exclude_row=row.name, for_update=True)
    if problem:
        frappe.throw(problem, title="Duplicate Payout")
    found = describe(row)
    from .self_pay import self_pay_problem as own_problem

    problem = own_problem(frappe.session.user, [{
        "phone": _value(request, "phone_number") or row.partyb, "payee_name": found.get("receiver"),
        "mpesa_name": found.get("expected_name"), "label": cstr(_value(request, "name"))}], "resend")
    if problem:
        frappe.throw(problem, frappe.PermissionError)
    if callable(getattr(handler, "check_retry", None)):
        handler.check_retry(row, request)
    else:  # a handler that is no Handler subclass: the default
        Handler.check_retry(handler, row, request)


# ---------------------------------------------------------------- the statement (money_out.statement)
def statement_entries(key: str, gl: str) -> list:
    """[(voucher_type, voucher_name, amount)]: the entries that book the party-less payout whose
    receipt is ``key`` on ``gl``, for the part its statement line takes (``h.statement_entry``).
    The amount is what is left of the entry to allocate when that is less (the line is then left
    for staff). [] for any other key, and at once on a site with no handler."""
    registered = doctypes()
    key = cstr(key).strip()
    if not registered or not key:
        return []
    requests = frappe.get_all(REQUEST, filters={"transaction_id": key, "status": "Paid", "docstatus": 1,
                                                "reference_doctype": ["in", registered]},
                              fields=["name", "amount", "transaction_id", "reference_doctype", "reference_name",
                                      "b2c_payment_disbursement", "b2c_payment_disbursement_reference",
                                      "mpesa_settings", "phone_number"])
    out = []
    for request in requests:
        handler = handler_for(request.reference_doctype)
        ref = request.b2c_payment_disbursement_reference
        request.reference_row = frappe.db.get_value(ROW, ref, "reference_row") if ref else None
        try:
            entry = _call(handler, "statement_entry", request)
        except Exception:
            frappe.log_error(title=f"M-Pesa party-less payout entry not found: {request.name}"[:140],
                             message=frappe.get_traceback())
            continue
        if not entry:
            continue
        doctype, name, amount = entry
        left = left_to_allocate(doctype, name, gl)
        if left is None:
            continue
        out.append((doctype, name, flt(min(flt(amount), left), 2)))
    return out


def left_to_allocate(doctype: str, name: str, gl: str) -> float | None:
    """What a submitted, uncleared entry pays out of ``gl`` that no submitted Bank Transaction has
    taken yet; None when it is not such an entry or nothing is left."""
    if doctype == "Journal Entry":
        rows = frappe.db.sql(
            """
            SELECT je.docstatus, je.clearance_date, SUM(jea.credit_in_account_currency - jea.debit_in_account_currency)
            FROM `tabJournal Entry` je
            JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
            WHERE je.name = %s AND jea.account = %s
            GROUP BY je.name, je.docstatus, je.clearance_date
            """,
            (name, gl),
        )
    elif doctype == "Payment Entry":
        rows = frappe.db.sql(
            "SELECT docstatus, clearance_date, paid_amount FROM `tabPayment Entry` WHERE name = %s AND paid_from = %s",
            (name, gl),
        )
    else:
        return None
    if not rows:
        return None
    docstatus, cleared, total = rows[0]
    if cint(docstatus) != 1 or cleared or flt(total) <= 0.005:
        return None
    taken = frappe.db.sql(
        """
        SELECT SUM(btp.allocated_amount) FROM `tabBank Transaction Payments` btp
        JOIN `tabBank Transaction` bt ON bt.name = btp.parent
        WHERE btp.payment_document = %s AND btp.payment_entry = %s AND bt.docstatus = 1
        """,
        (doctype, name),
    )
    left = flt(flt(total) - flt(taken[0][0] if taken else 0), 2)
    return left if left > 0.005 else None
