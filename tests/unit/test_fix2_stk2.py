"""Round 2, STK prompts (group stk2), on an in-memory frappe.

C03 windows: a re-sent prompt reuses its Mpesa Express Request, so the poller's 60-minute
window, the 6-hour give-up and the booking retries key on the latest prompt (``timestamp``,
``creation`` only when it is empty), not on when the request was created.
Reversals: a paid prompt whose receipt has a live reversal is never booked; the refusal is
logged once, and a reversal that ends Failed lets the receipt book. The check is repeated with
a locking read in the transaction that books, so a reversal in flight is waited for.
C01 split parts: a Payment Request is prompted once (refused above the transaction limit, at
most 250,000); other references are still split, never into a part under 1 shilling, and
every part is checked before the first prompt goes out.
C02 leftovers: party_account_currency is read only where ERPNext has it; a prompt whose
request's order was billed meanwhile books against the order's invoices instead of failing,
into the request's own account, with the cents an invoice still owes written off.
"""

import datetime
import importlib
import json
import math
import sys
import types
from pathlib import Path

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)  # fake_frappe.utils.now_datetime
EXPRESS = "Mpesa Express Request"
REVERSAL = "Mpesa Reversal Request"
SUITE = "M-Pesa Suite Settings"
PR = "ACC-PRQ-9"
SO = "SO-9"
REPO = Path(__file__).resolve().parents[2]


def ago(**kw):
    return NOW - datetime.timedelta(**kw)


def _cond(value, cond) -> bool:
    if not isinstance(cond, (list, tuple)):
        return value == cond
    op, arg = cond[0], cond[1]
    if op == "is":
        return (value not in (None, "")) if arg == "set" else (value in (None, ""))
    if op == "in":
        return value in arg
    if op == "not in":
        return value not in arg
    if op == "!=":
        return value != arg
    if value in (None, ""):
        return False
    if op == "between":
        return arg[0] <= value <= arg[1]
    return {"<": value < arg if op == "<" else None, ">": value > arg if op == ">" else None,
            "<=": value <= arg if op == "<=" else None, ">=": value >= arg if op == ">=" else None}[op]


class DB(fake_frappe.FakeDB):
    """FakeDB with the filter operators, ``fields`` / ``limit`` and ``get_values`` used here."""

    def __init__(self, frappe):
        super().__init__(frappe)
        self.db_type = "fake"  # named_lock is a no-op off MariaDB
        self.queries = []

    def _rows(self, doctype, filters):
        rows = self.table(doctype)
        if isinstance(filters, str):
            return [rows[filters]] if filters in rows else []
        return [r for r in rows.values() if all(_cond(r.get(k), c) for k, c in (filters or {}).items())]

    def get_all(self, doctype, filters=None, pluck=None, fields=None, order_by=None, limit=None, **kw):
        self.queries.append((doctype, dict(filters or {})))
        rows = self._rows(doctype, filters)
        for term in reversed([t.split() for t in (order_by or "").split(",") if t.strip()]):
            field, desc = term[0], len(term) > 1 and term[1].lower() == "desc"
            # as SQL: NULL first ascending (stable sort, so the later keys break ties)
            rows = sorted(rows, key=lambda r: (r.get(field) is not None, 0 if r.get(field) is None else r.get(field)),
                          reverse=desc)
        if limit:
            rows = rows[:limit]
        return [r.get(pluck) for r in rows] if pluck else [fake_frappe._dict(r) for r in rows]

    def get_values(self, doctype, filters, fieldname="name", order_by=None, pluck=False, **kw):
        rows = self._rows(doctype, filters)
        return [r.get(fieldname) for r in rows] if pluck else [(r.get(fieldname),) for r in rows]

    def exists(self, doctype, filters):
        rows = self._rows(doctype, filters)
        return rows[0]["name"] if rows else None

    def commit(self):
        pass


class Meta:
    def __init__(self, fields):
        self.fields = set(fields)

    def has_field(self, fieldname):
        return fieldname in self.fields


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.db = DB(frappe)
        frappe.utils.add_to_date = lambda d, **kw: d + datetime.timedelta(**kw)
        frappe.utils.strip_html = lambda s: s
        frappe.utils.now = lambda: str(NOW)
        frappe.utils.ceil = math.ceil
        frappe.utils.flt = lambda value, precision=None: (
            round(float(value or 0), precision) if precision is not None else float(value or 0)
        )
        frappe.utils.fmt_money = lambda value, *a, **kw: str(value)
        frappe.utils.get_request_site_address = lambda *a, **kw: "https://erp.example.com"
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.logger = lambda *a, **kw: types.SimpleNamespace(debug=print, info=print, error=print)
        frappe.metas = {EXPRESS: Meta({"verification_status", "timestamp"}),
                        "Payment Request": Meta({"outstanding_amount", "party_account_currency", "grand_total",
                                                 "currency"})}
        frappe.get_meta = lambda doctype: frappe.metas.get(doctype, Meta(()))
        frappe.db.table("DocType")["Mpesa Match Log"] = {"name": "Mpesa Match Log"}

        def get_cached_doc(doctype, name=None, **kw):
            row = frappe.db.table(doctype).get(name or doctype)
            if row is None:
                raise frappe.DoesNotExistError(doctype)
            return fake_frappe.FakeDoc(frappe, doctype, row)

        frappe.get_cached_doc = get_cached_doc
        frappe.db.table(SUITE)[SUITE] = {"name": SUITE, "poll_stk_status": 1, "verify_stk_callbacks": 1,
                                         "amount_tolerance": 1}
        # The security-credential helper needs the cryptography package, not under test here.
        enc = types.ModuleType("mpsa_suite.utils.encoding_initiator_password")
        enc.generate_security_credential = lambda *a, **kw: ""
        monkeypatch.setitem(sys.modules, enc.__name__, enc)
        yield fake_frappe._dict(frappe=frappe, monkeypatch=monkeypatch)
    finally:
        fake_frappe.uninstall(saved)


def prompt(env, name, **kw):
    row = {"name": name, "docstatus": 1, "status": "In Progress", "checkout_request_id": f"ws_CO_{name}",
           "verification_status": None, "transaction_id": None, "is_reconciled": 0, "booking_attempts": 0,
           "amount": 1000, "owner": "cashier@example.com", "creation": ago(minutes=30), "timestamp": None,
           "reference_doctype": "Sales Invoice", "reference_name": "SINV-1", "settings": "TEST"}
    row.update(kw)
    env.frappe.db.table(EXPRESS)[name] = row
    return row


# ================================================================ C03: windows on the latest prompt
def test_poller_keys_its_window_on_the_latest_prompt(env):
    stk_poll = importlib.import_module("mpsa_suite.money_in.stk_poll")
    polled = []
    env.monkeypatch.setattr(stk_poll, "poll", polled.append)
    prompt(env, "OLD-REPROMPT", creation=ago(hours=3), timestamp=ago(minutes=10))  # lost callback, re-prompt
    prompt(env, "JUST-SENT", creation=ago(minutes=30), timestamp=ago(minutes=1))  # still on the phone
    prompt(env, "NO-TIMESTAMP", creation=ago(minutes=20))  # falls back to creation
    prompt(env, "STALE", creation=ago(hours=2), timestamp=ago(hours=2))
    prompt(env, "ANSWERED", creation=ago(hours=3), timestamp=ago(minutes=10), verification_status="Pending")
    stk_poll.run()
    assert polled == ["NO-TIMESTAMP", "OLD-REPROMPT"]


@pytest.mark.parametrize("limit,expected", [
    (1, ["CR-5"]),
    (3, ["CR-5", "TS-5", "CR-4"]),  # 55, 51, 45 minutes ago: from both queries, oldest first
    (4, ["CR-5", "TS-5", "CR-4", "TS-4"]),
    (20, ["CR-5", "TS-5", "CR-4", "TS-4", "CR-3", "TS-3", "CR-2", "TS-2", "CR-1", "TS-1"]),
])
def test_prompted_between_keeps_the_oldest_prompts_across_both_queries(env, limit, expected):
    stk_poll = importlib.import_module("mpsa_suite.money_in.stk_poll")
    for i in range(1, 6):  # each query alone has more rows than the smaller limits
        prompt(env, f"TS-{i}", creation=ago(hours=5), timestamp=ago(minutes=10 * i + 1))  # re-prompted
        prompt(env, f"CR-{i}", creation=ago(minutes=10 * i + 5))  # no timestamp: its creation counts
    prompt(env, "RE-PROMPTED-LATE", creation=ago(minutes=50), timestamp=ago(minutes=1))  # created in, sent after
    prompt(env, "SENT-LONG-AGO", creation=ago(minutes=40), timestamp=ago(hours=3))  # never by its creation
    prompt(env, "OTHER", creation=ago(minutes=59), docstatus=0)  # outside the filters
    assert stk_poll.prompted_between({"docstatus": 1}, ago(minutes=60), ago(minutes=2), limit=limit) == expected


def _callback(receipt="SJK1ABC2DE", amount=1000):
    return json.dumps({"MerchantRequestID": "1", "CheckoutRequestID": "ws_CO_1", "ResultCode": 0,
                       "ResultDesc": "The service request is processed successfully.",
                       "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": amount},
                                                     {"Name": "MpesaReceiptNumber", "Value": receipt},
                                                     {"Name": "TransactionDate", "Value": 20261018115900}]}})


@pytest.mark.parametrize("sent,expected", [
    (ago(hours=1), "Pending"),  # a re-prompt on a request created 8 hours ago: sweep asks again
    (ago(hours=7), "Unverified"),
    (None, "Unverified"),  # no timestamp: from creation
])
def test_give_up_counts_from_the_latest_prompt(env, sent, expected):
    stk_verify = importlib.import_module("mpsa_suite.money_in.stk_verify")
    stk_poll = importlib.import_module("mpsa_suite.money_in.stk_poll")
    env.monkeypatch.setattr(stk_poll, "stk_query", lambda req: None)  # no final answer (spike arrest)
    env.monkeypatch.setattr(stk_verify, "log_match", lambda *a, **kw: None)
    prompt(env, "MEXP-1", creation=ago(hours=8), timestamp=sent, verification_status="Pending",
           callback_payload=_callback())
    assert stk_verify.settle("MEXP-1") == "Pending"
    assert env.frappe.db.table(EXPRESS)["MEXP-1"]["verification_status"] == expected


def test_sweep_retries_a_failed_booking_of_a_late_reprompt(env):
    stk_verify = importlib.import_module("mpsa_suite.money_in.stk_verify")
    booked = []
    env.monkeypatch.setattr(stk_verify, "book", booked.append)
    paid = {"status": "Completed", "verification_status": "Verified", "transaction_id": "SJK1ABC2DE"}
    prompt(env, "REPROMPT", creation=ago(hours=30), timestamp=ago(hours=1), **paid)
    prompt(env, "OLD", creation=ago(hours=30), timestamp=ago(hours=30), **paid)
    prompt(env, "NO-TIMESTAMP", creation=ago(hours=2), **paid)
    prompt(env, "GAVE-UP", creation=ago(hours=1), timestamp=ago(hours=1), booking_attempts=3, **paid)
    stk_verify.sweep()
    assert sorted(booked) == ["NO-TIMESTAMP", "REPROMPT"]


# ================================================================ reversals
class ExpressDoc(fake_frappe.FakeDoc):
    def validate_duplicate_c2b_records(self):
        self._frappe.calls.append(("neutralise", self.name))

    def book_completed_payment(self):
        self._frappe.calls.append(("book", self.name))


def reversal_env(env):
    stk_verify = importlib.import_module("mpsa_suite.money_in.stk_verify")
    env.frappe.calls, env.logged = [], []
    plain = env.frappe.get_doc
    env.frappe.get_doc = lambda dt, name=None, **kw: (
        ExpressDoc(env.frappe, dt, env.frappe.db.table(dt)[name]) if dt == EXPRESS else plain(dt, name, **kw)
    )
    env.monkeypatch.setattr(stk_verify, "log_match", lambda *a, **kw: env.logged.append((a, kw)))
    prompt(env, "MEXP-R", status="Completed", verification_status="Verified", transaction_id="SJK1ABC2DE")
    return stk_verify


def reversal(env, status="Reversed", docstatus=1, receipt="SJK1ABC2DE", name="MRV-1"):
    env.frappe.db.table(REVERSAL)[name] = {"name": name, "transaction_id": receipt, "c2b_register": "MPC2B-1",
                                           "docstatus": docstatus, "status": status, "creation": ago(minutes=5)}


@pytest.mark.parametrize("status", ["Not Initiated", "Initiated", "Unknown", "Reversed", "Already Reversed"])
def test_receipt_with_a_live_reversal_is_never_booked(env, status):
    stk_verify = reversal_env(env)
    reversal(env, status)
    assert stk_verify.book("MEXP-R") is False
    assert env.frappe.calls == [], "the register is not touched and nothing is booked"
    assert env.frappe.db.table(EXPRESS)["MEXP-R"]["booking_attempts"] == 0, "not an attempt: sweep asks again"
    [(args, kw)] = env.logged
    assert args[:2] == ("Error", "STK Callback")
    assert kw["on_change"] and kw["reference_name"] == "MEXP-R" and kw["reference_doctype"] == EXPRESS
    assert "MRV-1" in kw["details"] and status in kw["details"]


@pytest.mark.parametrize("status,docstatus", [("Failed", 1), ("Reversed", 2), ("Not Initiated", 0)])
def test_failed_cancelled_or_unsent_reversal_leaves_the_receipt_bookable(env, status, docstatus):
    stk_verify = reversal_env(env)
    reads = _count_reads(env, stk_verify)
    reversal(env, status, docstatus)
    reversal(env, "Reversed", receipt="OTHER12345", name="MRV-2")  # another receipt's reversal
    assert stk_verify.book("MEXP-R") is True
    assert env.frappe.calls == [("neutralise", "MEXP-R"), ("book", "MEXP-R")]
    assert env.logged == []
    assert reads == [False, True], "a plain read, then a locking one in the transaction that books"


def _count_reads(env, stk_verify):
    reads, real = [], stk_verify.receipt_reversal

    def receipt_reversal(receipt, for_update=False):
        reads.append(for_update)
        return real(receipt, for_update)

    env.monkeypatch.setattr(stk_verify, "receipt_reversal", receipt_reversal)
    return reads


def test_reversal_submitted_while_the_attempt_was_counted_is_seen_by_the_locking_read(env):
    """A reversal in flight (its submit holds the row while Safaricom answers) is invisible to
    the plain read; the locking read waits for it, then sees it."""
    stk_verify = reversal_env(env)
    reads = []

    def receipt_reversal(receipt, for_update=False):
        reads.append(for_update)
        return fake_frappe._dict(name="MRV-9", status="Initiated") if for_update else None

    env.monkeypatch.setattr(stk_verify, "receipt_reversal", receipt_reversal)
    assert stk_verify.book("MEXP-R") is False
    assert reads == [False, True]
    assert env.frappe.calls == [], "the register is not touched and nothing is booked"
    assert env.frappe.db.table(EXPRESS)["MEXP-R"]["booking_attempts"] == 0, "the attempt is given back"
    [(args, kw)] = env.logged
    assert args[:2] == ("Error", "STK Callback") and "MRV-9" in kw["details"] and kw["on_change"]


# ================================================================ C01: one prompt per Payment Request
class NewExpress:
    """An Mpesa Express Request as request_for_payment creates it: amount = base_amount (KES)."""

    def __init__(self, frappe):
        self._frappe = frappe
        self.flags = fake_frappe._dict()
        self.amount = None

    def update(self, values):
        self.__dict__.update(values)

    def insert(self, ignore_permissions=False):
        self.amount = self._frappe.kes_rate * self.base_amount if self.currency != "KES" else self.base_amount
        self._frappe.inserted.append(self)
        return self

    def submit(self):
        self._frappe.sent.append(self.amount)  # on_submit sends the prompt


def settings(env, limit=150000, **kw):
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings")
    env.frappe.inserted, env.frappe.sent, env.frappe.kes_rate = [], [], 129
    env.frappe.new_doc = lambda doctype: NewExpress(env.frappe)
    env.monkeypatch.setattr(module, "convert_amount_to_kes", lambda currency, amount, *a, **kw: amount * 129)
    doc = module.MpesaSettings()
    doc.__dict__.update(name="TEST", transaction_limit=limit, **kw)
    return doc


def ask(doc, amount, reference_doctype="Payment Request", currency="KES"):
    doc.request_for_payment(reference_doctype=reference_doctype, reference_docname=PR, request_amount=amount,
                            currency=currency, payment_gateway="Mpesa-TEST", phone_number="0708374149")


@pytest.mark.parametrize("limit,amount", [(150000, 172000), (150000, 150000.6), (0, 260000), (300000, 260000)])
def test_payment_request_above_its_one_prompt_is_refused_before_anything_is_made(env, limit, amount):
    doc = settings(env, limit)
    with pytest.raises(env.frappe.ValidationError, match="Transaction Limit"):
        ask(doc, amount)
    assert (env.frappe.inserted, env.frappe.sent) == ([], [])


def test_payment_request_within_the_limit_is_prompted_once_for_all_of_it(env):
    doc = settings(env, 150000)
    ask(doc, 150000)
    assert env.frappe.sent == [150000]
    doc = settings(env, 0)  # blank limit: Safaricom's 250,000
    ask(doc, 250000)
    assert env.frappe.sent == [250000]


def test_payment_request_in_another_currency_is_compared_in_kes(env):
    doc = settings(env, 150000)
    with pytest.raises(env.frappe.ValidationError, match="Transaction Limit"):
        ask(doc, 1500, currency="USD")  # KES 193,500
    ask(doc, 1000, currency="USD")  # KES 129,000
    assert env.frappe.sent == [129000]


@pytest.mark.parametrize("limit,amount,parts", [
    (150, 480, [150, 150, 150, 30]),
    (150000, 150000.30, [150000.30]),  # a 0.30 part is folded into the one before it
    (150000, 300000.75, [150000, 150000.75]),
    (150000, 300001, [150000, 150000, 1]),
    (0, 300000, [250000, 50000]),  # no limit set: Safaricom's 250,000
    (400000, 300000, [250000, 50000]),
])
def test_other_references_are_split_into_parts_of_at_least_one_shilling(env, limit, amount, parts):
    doc = settings(env, limit)
    assert doc.split_request_amount_according_to_transaction_limit(fake_frappe._dict(request_amount=amount)) == parts
    ask(doc, amount, reference_doctype="Sales Invoice")
    assert env.frappe.sent == parts


def test_no_part_is_sent_unless_every_part_can_be_sent(env):
    doc = settings(env, 250000)
    with pytest.raises(env.frappe.ValidationError, match="No prompt was sent"):
        ask(doc, 500000.6, reference_doctype="Sales Invoice")  # 250,000.6 asks 250,001: above the maximum
    assert env.frappe.sent == [], "the first part's prompt did not go out either"


# ================================================================ C02 leftovers: Payment Request booking
class FakePaymentRequest(fake_frappe.FakeDoc):
    def run_method(self, method, *args):
        pass

    def create_payment_entry(self, submit=True):
        frappe = self._frappe
        order = frappe.db.table("Sales Order")[self.reference_name]
        if order["per_billed"] >= 100:
            raise frappe.ValidationError("Can only make payment against unbilled Sales Order")
        frappe.erpnext_entries.append(self.name)
        return ErpnextEntry()


class ErpnextEntry:
    """What ERPNext's create_payment_entry(submit=False) hands back (no rows: not under test)."""

    def __init__(self):
        self.paid_from_account_currency = self.paid_to_account_currency = "KES"
        self.deductions, self.references, self.company = [], [], "_Test Co"

    def get(self, key, default=None):
        return getattr(self, key, default)

    def set(self, key, value):
        setattr(self, key, value)

    def insert(self, **kw):
        return self

    def submit(self):
        return self


class Express:
    def __init__(self, **kw):
        self.name, self.reference_doctype, self.reference_name = "MEXP-P", "Payment Request", PR
        self.owner, self.phone_number, self.payment_gateway = "cashier@example.com", "254708374149", "Mpesa-TEST"
        self.transaction_date, self.is_reconciled, self.settings = None, 0, "TEST"
        self.__dict__.update(kw)

    def get(self, key, default=None):
        return getattr(self, key, default)

    def reload(self):
        pass

    def save(self, ignore_permissions=False):
        pass


class FakePaymentEntry:
    """A Payment Entry as _book_on_billed_order builds it (ERPNext's own methods are not under test)."""

    def __init__(self, frappe):
        self._frappe = frappe
        self.references, self.deductions, self.submitted = [], [], False

    def update(self, values):
        self.__dict__.update(values)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)

    def append(self, table, row):
        getattr(self, table).append(dict(row))

    def setup_party_account_field(self):
        pass

    def set_missing_values(self):
        pass

    def set_exchange_rate(self):
        self.source_exchange_rate = 1  # a KES company

    def insert(self, ignore_permissions=False):
        self._frappe.entries.append(self)
        return self

    def submit(self):
        self.submitted = True


def booking_env(env, per_billed=100, outstanding=50000, meta=None, invoice_owes=30000, round_off=False,
                payment_account="Mpesa Gateway - TC"):
    frappe = env.frappe
    if meta is not None:
        frappe.metas["Payment Request"] = Meta(meta)
    frappe.erpnext_entries, frappe.entries, frappe.invoices_made = [], [], []
    frappe.currencies = {}
    frappe.get_cached_value = lambda doctype, *a, **kw: (
        ("Round Off - TC", None, "Main - TC") if round_off and doctype == "Company" else None
    )
    frappe.new_doc = lambda doctype: FakePaymentEntry(frappe) if doctype == "Payment Entry" else None
    frappe.db.table("Payment Request")[PR] = {
        "name": PR, "docstatus": 1, "status": "Partially Paid", "currency": "KES", "party_account_currency": "KES",
        "grand_total": 100000, "outstanding_amount": outstanding, "reference_doctype": "Sales Order",
        "reference_name": SO, "mode_of_payment": "Mpesa-TEST", "payment_account": payment_account}
    frappe.db.table("Sales Order")[SO] = {"name": SO, "per_billed": per_billed, "company": "_Test Co",
                                          "customer": "Customer A", "cost_center": "Main - TC"}
    frappe.db.table("Accounts Settings")["Accounts Settings"] = {"over_billing_allowance": 0}
    frappe.db.table("Sales Invoice Item").update({
        "SII-1": {"name": "SII-1", "parent": "SINV-9", "sales_order": SO, "docstatus": 1},
        "SII-2": {"name": "SII-2", "parent": "SINV-9", "sales_order": SO, "docstatus": 1},
        "SII-3": {"name": "SII-3", "parent": "SINV-8", "sales_order": "SO-OTHER", "docstatus": 1},
    })
    frappe.db.table("Sales Invoice").update({
        "SINV-9": {"name": "SINV-9", "docstatus": 1, "outstanding_amount": invoice_owes, "customer": "Customer A",
                   "debit_to": "Debtors - TC"},
        "SINV-8": {"name": "SINV-8", "docstatus": 1, "outstanding_amount": 9000, "customer": "Customer A",
                   "debit_to": "Debtors - TC"},
    })

    real_get_value = frappe.db.get_value

    def get_value(doctype, filters, fieldname="name", *a, **kw):
        if doctype == "Payment Request" and isinstance(fieldname, (list, tuple)):
            unknown = set(fieldname) - frappe.metas["Payment Request"].fields
            if unknown:  # MariaDB 1054, re-raised by Frappe without ignore=
                raise frappe.ValidationError(f"(1054, \"Unknown column '{sorted(unknown)[0]}' in 'field list'\")")
        return real_get_value(doctype, filters, fieldname, *a, **kw)

    frappe.db.get_value = get_value
    plain_get_doc = frappe.get_doc
    frappe.get_doc = lambda dt, name=None, **kw: (
        FakePaymentRequest(frappe, dt, frappe.db.table(dt)[name]) if dt == "Payment Request" else plain_get_doc(dt, name, **kw)
    )

    def create_payment_entry(company, customer, amount, currency, mode_of_payment, party_type="Customer",
                             reference_date=None, reference_no=None, posting_date=None, cost_center=None, submit=0,
                             party_account=None, references=None):
        frappe.entries.append(dict(company=company, customer=customer, amount=amount, currency=currency,
                                   mode_of_payment=mode_of_payment, reference_no=reference_no, submit=submit,
                                   party_account=party_account, references=references))

    pe_module = types.ModuleType("mpsa_suite.mpsa_suite.api.payment_entry")
    pe_module.create_payment_entry = create_payment_entry
    pe_module.get_bank_cash_account = lambda company, mode_of_payment, bank_account=None: (
        fake_frappe._dict(account="Mpesa MoP - TC", account_currency="KES") if mode_of_payment == "Mpesa-TEST" else None
    )
    party = types.ModuleType("erpnext.accounts.party")
    party.get_party_account = lambda party_type, party_name, company: "Debtors - TC"
    accounts_utils = types.ModuleType("erpnext.accounts.utils")
    accounts_utils.get_account_currency = lambda account: frappe.currencies.get(account, "KES")

    def make_sales_invoice(source_name, ignore_permissions=False):
        frappe.invoices_made.append(source_name)
        return types.SimpleNamespace(insert=lambda **kw: types.SimpleNamespace(submit=lambda: None))

    so_module = types.ModuleType("erpnext.selling.doctype.sales_order.sales_order")
    so_module.make_sales_invoice = make_sales_invoice
    for name in ("erpnext", "erpnext.accounts", "erpnext.selling", "erpnext.selling.doctype",
                 "erpnext.selling.doctype.sales_order"):
        env.monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for module in (pe_module, party, accounts_utils, so_module):
        env.monkeypatch.setitem(sys.modules, module.__name__, module)
    return importlib.import_module("mpsa_suite.utils.utils")


def test_prompt_on_a_request_whose_order_was_billed_books_against_its_invoice(env):
    utils = booking_env(env, per_billed=100, outstanding=50000)
    req = Express(amount=50000, transaction_id="SJK1ABC2DF")
    utils.handle_successful_transaction(req, types.SimpleNamespace(auto_create_sales_invoice=1))
    assert env.frappe.erpnext_entries == [], "ERPNext's builder refuses a billed order"
    [pe] = env.frappe.entries
    assert (pe.payment_type, pe.party, pe.paid_amount, pe.received_amount, pe.reference_no, pe.submitted) == (
        "Receive", "Customer A", 50000, 50000, "SJK1ABC2DF", True)
    assert (pe.paid_from, pe.paid_to, pe.mode_of_payment) == ("Debtors - TC", "Mpesa Gateway - TC", "Mpesa-TEST"), \
        "into the request's own (gateway) account, as ERPNext books it, not its mode of payment's"
    assert pe.references == [
        {"reference_doctype": "Sales Invoice", "reference_name": "SINV-9", "allocated_amount": 30000}
    ], "the order's own invoice, up to what it owes; the rest stays on the customer"
    assert pe.deductions == []
    pr = env.frappe.db.table("Payment Request")[PR]
    assert (pr["status"], pr["outstanding_amount"]) == ("Paid", 0)
    assert env.frappe.invoices_made == [], "a billed order is not billed again"


def test_part_paid_on_a_billed_order_leaves_the_request_partly_open(env):
    utils = booking_env(env, per_billed=100, outstanding=50000)
    utils.handle_successful_transaction(Express(amount=20000, transaction_id="SJK1ABC2DG"),
                                        types.SimpleNamespace(auto_create_sales_invoice=0))
    [pe] = env.frappe.entries
    assert pe.references[0]["allocated_amount"] == 20000
    pr = env.frappe.db.table("Payment Request")[PR]
    assert (pr["status"], pr["outstanding_amount"]) == ("Partially Paid", 30000)


def _book_billed(env, amount, **kw):
    utils = booking_env(env, per_billed=100, **kw)
    utils.handle_successful_transaction(Express(amount=amount, transaction_id="SJK1ABC2DK"),
                                        types.SimpleNamespace(auto_create_sales_invoice=0))
    pr = env.frappe.db.table("Payment Request")[PR]
    return env.frappe.entries, (pr["status"], pr["outstanding_amount"])


def test_cents_a_billed_order_still_owes_are_written_off_on_its_invoice(env):
    # request and invoice owe 10,000.40 (rounded totals off); STK asks, and is paid, 10,000
    [pe], pr = _book_billed(env, 10000, outstanding=10000.40, invoice_owes=10000.40, round_off=True)
    assert pe.paid_amount == 10000, "only what M-Pesa received"
    assert [(r["reference_name"], r["allocated_amount"]) for r in pe.references] == [("SINV-9", pytest.approx(10000.40))]
    assert [(d["account"], d["cost_center"], d["amount"]) for d in pe.deductions] == [
        ("Round Off - TC", "Main - TC", pytest.approx(0.40))], "the invoice is closed, not left owing 0.40"
    assert pr == ("Paid", 0)


def test_without_a_round_off_account_the_cents_stay_open_on_invoice_and_request(env):
    [pe], pr = _book_billed(env, 10000, outstanding=10000.40, invoice_owes=10000.40, round_off=False)
    assert [r["allocated_amount"] for r in pe.references] == [10000]
    assert pe.deductions == []
    assert pr == ("Partially Paid", pytest.approx(0.40)), "not Paid while its invoice still owes the cents"


def test_cents_no_invoice_owes_leave_the_request_paid_with_nothing_written_off(env):
    # the invoice was partly paid elsewhere: it owes 9,000 of the request's 10,000.40
    [pe], pr = _book_billed(env, 10000, outstanding=10000.40, invoice_owes=9000, round_off=True)
    assert [r["allocated_amount"] for r in pe.references] == [9000], "1,000 stays unallocated on the customer"
    assert pe.deductions == []
    assert pr == ("Paid", 0)


def test_request_without_a_payment_account_books_to_its_mode_of_payment_account(env):
    [pe], pr = _book_billed(env, 50000, payment_account=None)
    assert pe.paid_to == "Mpesa MoP - TC"
    assert pr == ("Paid", 0)


def test_payment_account_in_another_currency_is_left_for_manual_booking(env):
    utils = booking_env(env, per_billed=100)
    env.frappe.currencies["Mpesa Gateway - TC"] = "USD"
    payment_request = env.frappe.get_doc("Payment Request", PR)
    with pytest.raises(env.frappe.ValidationError, match="another currency"):
        utils._book_payment_request_payment(Express(amount=50000, transaction_id="SJK1ABC2DL"), payment_request)
    assert env.frappe.entries == []
    assert env.frappe.db.table("Payment Request")[PR]["status"] == "Partially Paid"


def test_unbilled_order_still_books_through_erpnext(env):
    utils = booking_env(env, per_billed=40)
    utils.handle_successful_transaction(Express(amount=50000, transaction_id="SJK1ABC2DH"),
                                        types.SimpleNamespace(auto_create_sales_invoice=0))
    assert env.frappe.erpnext_entries == [PR]
    assert env.frappe.entries == []


def test_old_erpnext_without_party_account_currency_still_books(env):
    # ERPNext before partial Payment Requests has neither outstanding_amount nor its currency
    utils = booking_env(env, per_billed=0, meta={"grand_total", "currency"})
    utils.handle_successful_transaction(Express(amount=100000, transaction_id="SJK1ABC2DJ"),
                                        types.SimpleNamespace(auto_create_sales_invoice=0))
    assert env.frappe.erpnext_entries == [PR]
    assert env.frappe.db.table("Payment Request")[PR]["status"] == "Paid"


# ================================================================ desk: Initiate STK Push button
JS = REPO / "mpsa_suite/mpsa_suite/doctype/mpesa_express_request/mpesa_express_request.js"
JS_HARNESS = r"""
const src = require("fs").readFileSync(process.argv[1], "utf8");
let handlers;
global.__ = (s) => s;
global.frappe = {boot: {app_data: []}, ui: {form: {on: (dt, h) => { handlers = h; }}},
                 realtime: {on: () => {}}, db: {get_value: () => Promise.resolve({})}, call: () => {}};
eval(src);
const out = JSON.parse(process.argv[2]).map((doc) => {
  const added = [];
  handlers.refresh({doc: Object.assign({docstatus: 1, currency: "KES", amount: 10}, doc),
                    add_custom_button: (label) => added.push(label), add_web_link: () => {}, set_value: () => {}});
  return added.includes("Initiate STK Push");
});
console.log(JSON.stringify(out));
"""


def test_initiate_button_is_hidden_where_the_server_refuses_a_prompt():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cases = [
        ({"status": "In Progress"}, True),
        ({"status": "Failed", "verification_status": "Verified"}, True),  # settled Failed: re-prompt
        ({"status": "Completed"}, False),
        ({"status": "Failed", "transaction_id": "SJK1ABC2DE"}, False),
        ({"status": "In Progress", "verification_status": "Pending"}, False),
        ({"status": "Failed", "verification_status": "Mismatch"}, False),
        ({"status": "In Progress", "verification_status": "Unverified"}, False),
        ({"status": "In Progress", "docstatus": 0}, False),
    ]
    out = subprocess.run([node, "-e", JS_HARNESS, str(JS), json.dumps([c for c, _ in cases])],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert json.loads(out) == [want for _, want in cases]
