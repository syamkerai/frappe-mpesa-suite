"""The suite's own entries are posted at once (the user's rule, 2026-10-28): submitted, and in a
Payment Entry workflow its first submitted state (Approved), never drafts left for review.

Covers: every migrate switches "Submit B2C Accounting Entries" (each Mpesa Settings) and "Submit
Transfer and Funding Entries" (M-Pesa Suite Settings) on, without touching their modified; saving
either settings form keeps them on; Make Entry posts the entry it makes, so it is refused to a user
who may not submit it, and its button is hidden from them. Every entry the suite posts is inserted
already submitted, in one save (accounting.posting.insert_entry), after the draft's save methods,
so a workflow's Draft state never runs (no Open Workflow Action), and no suite code lets ERPNext
insert then submit one (a Payment Request's create_payment_entry is built with submit=False); a
switch still off inserts a draft as before. A payout whose day ERPNext refuses (a closed Accounting
Period, books closed by a Period Closing Voucher, Accounts Frozen Till) says what to do: Make Entry
with a later Posting Date (B2B; the entry says both days), an entry by hand carrying the receipt,
which the booking counts (B2C, alerted once per reason), the charge's Journal Entry by hand (a
charge entry fails under a savepoint of its own: the payout's line and its confirmed charge stay,
one Match Log per reason, and a later sweep books the charge). That a submit under a workflow lands in its
approved state with no Open Workflow Action is Frappe's (set_workflow_state_on_action), checked on
the bench (test_b2b_documents).
"""

import ast
import datetime
import importlib
import importlib.util
import inspect
import pathlib
import shutil
import sys
import types

import fake_frappe
import pytest

import b2b_fixture
from test_b2b_entries import COMPLETED, make, row
from test_b2b_entries import env as b2b_env  # noqa: F401 (the B2B booking site)
from test_employee_pay_js import B2B_JS, run
from test_fix2_stk2 import env as stk_env  # noqa: F401 (a site that loads the Mpesa Settings controller)
from test_fix_b2creq import PE, PER, REQ, _paid_row
from test_fix_b2creq import book as b2c_env  # noqa: F401 (the B2C booking site)
from test_payout_statement import B2C as STATEMENT_B2C
from test_payout_statement import b2c as statement_b2c
from test_payout_statement import env as statement_env  # noqa: F401 (the payout statement lines)

ROOT = pathlib.Path(__file__).resolve().parents[2]
PERIOD = "erpnext.accounts.doctype.accounting_period.accounting_period"
CLOSED = "You cannot create a Payment Entry within the closed Accounting Period <strong>Oct 2026 - AIL</strong>"


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.logger = lambda *a, **kw: None
        custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")  # migrate's
        custom_field.create_custom_fields = lambda fields, update=False: None
        for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
        yield types.SimpleNamespace(frappe=frappe, monkeypatch=monkeypatch)
    finally:
        fake_frappe.uninstall(saved)


def test_every_migrate_switches_both_settings_on(env):
    frappe = env.frappe
    migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
    queries, singles = [], []
    env.monkeypatch.setattr(frappe.db, "sql", lambda query, *a, **kw: queries.append(query), raising=False)
    env.monkeypatch.setattr(frappe.db, "set_single_value", lambda dt, field, value=None, **kw:
                            singles.append((dt, field, value, kw)), raising=False)
    env.monkeypatch.setattr(frappe.db, "columns", {("Mpesa Settings", "submit_b2c_accounting_entries")})
    frappe.db.table("DocType")["M-Pesa Suite Settings"] = {"name": "M-Pesa Suite Settings"}
    migrate.post_entries_at_once()
    assert queries == ["UPDATE `tabMpesa Settings` SET submit_b2c_accounting_entries = 1 "
                       "WHERE IFNULL(submit_b2c_accounting_entries, 0) = 0"]
    # a migrate does not make the settings look edited (modified)
    assert singles == [("M-Pesa Suite Settings", "submit_statement_entries", 1, {"update_modified": False})]
    assert "post_entries_at_once()" in inspect.getsource(migrate.after_migrate)


def test_an_mpesa_settings_save_keeps_its_entries_posted_at_once(stk_env, monkeypatch):  # noqa: F811
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.mpesa_settings.mpesa_settings")
    doc = module.MpesaSettings()
    for method in ("refresh_security_credential", "validate_api_switches", "warn_shared_shortcode"):
        setattr(doc, method, lambda: None)
    doc.__dict__.update(exchange_rates=[], submit_b2c_accounting_entries=0)
    monkeypatch.setattr(module.MpesaSettings, "meta", types.SimpleNamespace(has_field=lambda field: False),
                        raising=False)
    doc.validate()
    assert doc.submit_b2c_accounting_entries == 1


def test_a_suite_settings_save_keeps_statement_entries_posted_at_once(env):
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.m_pesa_suite_settings.m_pesa_suite_settings")
    doc = module.MPesaSuiteSettings()
    doc.get_password = lambda *a, **kw: "token"
    doc.enforce_ip_allowlist = 0
    doc.get = lambda field, default=None: getattr(doc, field, default)
    doc.submit_statement_entries = 0
    doc.validate()
    assert doc.submit_statement_entries == 1


def test_make_entry_is_refused_to_a_user_who_may_not_submit_it(b2b_env):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1")
    env.denied.add(("Payment Entry", "submit"))
    with pytest.raises(env.frappe.PermissionError, match="Make Entry posts the Payment Entry at once"):
        make(env, "Supplier Advance")
    assert env.inserted == [] and row(env).get("entry_status") != "Draft Entry"


def test_make_entry_posts_the_entry_it_makes(b2b_env):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1")
    answer = make(env, "Supplier Advance")
    assert answer["docstatus"] == 1 and row(env)["entry_status"] == "Entry Made"
    assert b2b_fixture.SETTINGS  # the Mpesa Settings of the request, whatever its old switch said


# ---------------------------------------------------------------- one save, never a draft for review
class Entry:
    """A new Payment Entry, as Frappe's Document runs it: what ran, and its docstatus each time."""

    def __init__(self):
        self.doctype, self.docstatus, self.steps = "Payment Entry", 0, []

    def run_method(self, method, *args, **kwargs):
        self.steps.append((method, self.docstatus))

    def insert(self, ignore_permissions=None):
        self.steps.append(("insert", self.docstatus, ignore_permissions))
        return self

    def submit(self):
        self.steps.append(("submit", self.docstatus))


def posting():
    return importlib.import_module("mpsa_suite.accounting.posting")


def test_an_entry_posted_at_once_is_inserted_submitted_in_one_save(env):
    # Frappe runs an insert with docstatus 1 as a submit and puts it straight in the workflow's approved
    # state: the Draft state (its Open Workflow Action, its email) never runs. ERPNext's draft-only steps
    # (the advance account, a Journal Entry's against accounts) run first, on the draft, as before.
    entry = Entry()
    assert posting().insert_entry(entry, 1, ignore_permissions=True) is entry
    assert entry.steps == [("before_validate", 0), ("validate", 0), ("before_save", 0), ("insert", 1, True)]


@pytest.mark.parametrize("switch", [0, None, "0"])
def test_a_switch_still_off_inserts_a_draft_as_before(env, switch):
    # a site before its migrate (post_entries_at_once), or a test
    entry = Entry()
    posting().insert_entry(entry, switch)
    assert entry.steps == [("insert", 0, None)]


SITES = [  # every suite insert-and-submit of a Payment or Journal Entry
    ("mpsa_suite/money_out/b2b_entries.py", "_book"),
    ("mpsa_suite/money_out/b2b_entries.py", "make_entry"),
    ("mpsa_suite/mpsa_suite/api/payment_entry.py", "create_payment_entry"),
    ("mpsa_suite/accounting/journal_service.py", "create_journal_entry"),
    ("mpsa_suite/money_out/salary_entries.py", "for_b2c"),
    ("mpsa_suite/money_out/statement.py", "charge_entry"),
    ("mpsa_suite/wallet/statement_import.py", "transfer_entry"),
    ("mpsa_suite/utils/utils.py", "_book_payment_request_payment"),
    ("mpsa_suite/utils/utils.py", "_book_on_billed_order"),
    # round 4.5: a petty cash phone's top-up journal and spend entry, a staff advance's settlement
    ("mpsa_suite/money_out/petty_cash.py", "book"),
    ("mpsa_suite/money_out/petty_cash.py", "record_spend"),
    ("mpsa_suite/money_out/pay_via_staff.py", "settle_staff_advances"),
]


def _function(path, name):
    source = (ROOT / path).read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


@pytest.mark.parametrize("path, name", SITES)
def test_every_suite_entry_is_posted_through_the_one_helper(path, name):
    code = _function(path, name)
    assert "insert_entry(" in code
    assert ".submit()" not in code and ".insert(" not in code


# ERPNext's own helpers that insert, then submit, a Payment or Journal Entry: under a workflow the insert runs its
# Draft state (an Open Workflow Action, the Draft email). PaymentRequest.create_payment_entry(submit=True, its
# default) and set_as_paid, which calls it; the Bank Reconciliation Tool's entries.
SUBMITTING_HELPERS = ("set_as_paid", "create_payment_entry_bts", "create_journal_entry_bts")


def _suite_calls():
    for path in sorted((ROOT / "mpsa_suite").rglob("*.py")):
        if path.name.startswith("test_") or path.name == "testing.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                yield path.relative_to(ROOT).as_posix(), node, func, name


def test_no_suite_code_lets_erpnext_insert_then_submit_an_entry():
    for path, node, func, name in _suite_calls():
        assert name not in SUBMITTING_HELPERS, f"{path}:{node.lineno} {name}"
        if name == "create_payment_entry" and isinstance(func, ast.Attribute):
            # a Payment Request's: built as a draft, posted by accounting.posting.insert_entry
            submit = next((k.value for k in node.keywords if k.arg == "submit"), None)
            assert isinstance(submit, ast.Constant) and submit.value is False, f"{path}:{node.lineno}"


def test_a_suite_create_payment_entry_is_the_suites_own():
    # create_payment_entry(...) called by name is api.payment_entry's, which posts through insert_entry (SITES)
    for path, node, func, name in _suite_calls():
        if name == "create_payment_entry" and isinstance(func, ast.Name):
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            assert any(isinstance(n, ast.ImportFrom) and (n.module or "").endswith("api.payment_entry")
                       and any(a.name == "create_payment_entry" for a in n.names)
                       for n in ast.walk(tree)), f"{path}:{node.lineno}"


def test_a_paid_payment_request_entry_is_posted_in_one_save():
    # utils.handle_successful_transaction: ERPNext's create_payment_entry() inserts, then submits; nothing it does
    # after the submit (v15, v16) is left out by building the draft and posting it
    code = _function("mpsa_suite/utils/utils.py", "handle_successful_transaction")
    assert "insert_entry(payment_request.create_payment_entry(submit=False), ignore_permissions=True)" in code


def test_a_draft_a_prompt_paid_is_submitted_in_one_save():
    # utils.handle_successful_transaction: a draft Payment Entry an STK prompt paid gets its receipt and is
    # submitted with it (not saved, then submitted)
    code = _function("mpsa_suite/utils/utils.py", "handle_successful_transaction")
    assert "payment_entry.submit()" in code and "payment_entry.save(" not in code


# ---------------------------------------------------------------- a closed or frozen day
@pytest.fixture
def closed_period(monkeypatch):
    """ERPNext's ClosedAccountingPeriod, on the fake site the test's own fixture installed."""

    def install(frappe):
        module = types.ModuleType(PERIOD)
        module.ClosedAccountingPeriod = type("ClosedAccountingPeriod", (frappe.ValidationError,), {})
        monkeypatch.setitem(sys.modules, PERIOD, module)
        return module.ClosedAccountingPeriod

    return install


def test_a_closed_or_frozen_day_is_told_from_other_refusals(env, closed_period):
    module = posting()
    assert module.period_closed(env.frappe.ValidationError(CLOSED)) is False  # no ERPNext: never
    error = closed_period(env.frappe)
    assert module.period_closed(error(CLOSED))
    # general_ledger.check_freezing_date (binds Administrator too)
    frozen = "You are not authorized to add or update entries before 31-10-2026"
    assert module.period_closed(env.frappe.ValidationError(frozen))
    assert not module.period_closed(env.frappe.ValidationError("Difference Amount must be zero"))
    assert not module.period_closed(RuntimeError(frozen))


@pytest.mark.parametrize("message", [
    # general_ledger.validate_against_pcv: v15 ends its first sentence with ".", v16 does not
    "Books have been closed till the period ending on 30-09-2026.</br >You cannot create/amend any accounting "
    "entries till this date.",
    "Books have been closed till the period ending on 30-09-2026</br >You cannot create/amend any accounting "
    "entries till this date.",
])
def test_books_closed_by_a_period_closing_voucher_are_a_closed_day(env, closed_period, message):
    module = posting()
    closed_period(env.frappe)
    assert module.period_closed(env.frappe.ValidationError(message))  # a plain ValidationError, for every user
    assert not module.period_closed(RuntimeError(message))


def test_a_translation_that_starts_with_the_date_never_matches_every_refusal(env, closed_period, monkeypatch):
    module = posting()
    closed_period(env.frappe)
    monkeypatch.setattr(env.frappe, "_", lambda text: "{0}: " + text if text.startswith("Books") else text)
    assert not module.period_closed(env.frappe.ValidationError("Difference Amount must be zero"))


def test_a_b2b_paid_on_a_closed_day_says_to_make_its_entry_with_a_later_posting_date(b2b_env, closed_period):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1")
    env.fail_insert = closed_period(env.frappe)(CLOSED)
    for _ in range(2):  # the sweep tries again: one log, one alert
        assert env.entries.book_automatically("MB2B-1") is None
    closed = ("The day it was paid is in a closed Accounting Period, in books closed by a Period Closing Voucher or "
              "on or before Accounts Frozen Till: press Make Entry and give it a Posting Date in an open period.")
    assert row(env)["entry_note"] == (
        f"Could not book automatically: traceback. {closed} Do not make a Payment Entry for it by hand.")
    assert env.alerts == [("MB2B-1", f"MB2B-1 was paid but could not be booked automatically: traceback. {closed}")]
    assert len([e for e in env.frappe.errors if e[1].get("title") == "M-Pesa B2B booking failed"]) == 1


def test_make_entry_posts_on_a_later_posting_date_and_says_the_day_it_was_paid(b2b_env):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1", entry_mode="Make the entry later")
    with pytest.raises(env.frappe.ValidationError, match=r"the day M-Pesa paid it \(2026-10-17\) or a later day"):
        make(env, "Supplier Advance", posting_date="2026-10-16")
    assert env.inserted == []
    answer = make(env, "Supplier Advance", posting_date="2026-11-02")
    pe = env.frappe.db.table("Payment Entry")[answer["name"]]
    assert (pe["posting_date"], pe["reference_date"], pe["docstatus"]) == (datetime.date(2026, 11, 2), COMPLETED, 1)
    assert pe["remarks"].endswith("receipt R1. Paid on 2026-10-17, posted on 2026-11-02.")


def test_make_entry_on_the_day_it_was_paid_keeps_its_remark(b2b_env):  # noqa: F811
    env = b2b_env
    env.request(transaction_id="R1", entry_mode="Make the entry later")
    pe = env.frappe.db.table("Payment Entry")[make(env, "Supplier Advance", posting_date="2026-10-17")["name"]]
    assert pe["posting_date"] == COMPLETED and "posted on" not in pe["remarks"]


@pytest.mark.parametrize("fields, custom_remark", [(None, True), ({"posting_date"}, False)])
def test_a_journal_entry_made_on_a_later_day_says_both_days(b2b_env, fields, custom_remark):  # noqa: F811
    env = b2b_env
    env.meta_fields["Journal Entry"] = fields  # v16 has custom_remark; v15 does not
    env.frappe.db.table("Account")["Rent - AIL"] = {"name": "Rent - AIL", "company": "Astral",
                                                   "report_type": "Profit and Loss", "account_currency": "KES"}
    env.request(transaction_id="R1", entry_mode="Make the entry later")
    result = make(env, "Journal Entry", account="Rent - AIL", cost_center="Main - AIL", posting_date="2026-11-02")
    je = env.frappe.db.table("Journal Entry")[result["name"]]
    text = "M-Pesa B2B BusinessPayBill to 888880 (MB2B-1), receipt R1. Paid on 2026-10-17, posted on 2026-11-02."
    assert (je["posting_date"], je["cheque_date"], je["user_remark"]) == (datetime.date(2026, 11, 2), COMPLETED, text)
    assert je.get("remark") == (text if custom_remark else None)


# ---------------------------------------------------------------- B2C: alerted, and what staff do
def _alerts(monkeypatch):
    alerts = []
    party_accounts = types.ModuleType("mpsa_suite.money_out.party_accounts")
    party_accounts.alert = lambda message, doc, roles=None: alerts.append((doc.doctype, doc.name, message))
    monkeypatch.setitem(sys.modules, party_accounts.__name__, party_accounts)
    return alerts


def test_a_b2c_booking_failure_is_alerted_once_per_payout_and_reason(b2c_env, monkeypatch):  # noqa: F811
    env = b2c_env
    alerts = _alerts(monkeypatch)
    _paid_row(env, 1, "SKT1AAAAAA")

    def fail(disb, ref, submit=False):
        raise env.frappe.ValidationError("Difference Amount must be zero")

    monkeypatch.setattr(env.mod, "create_payment_entry_for_doc", fail)
    for _ in range(3):  # the sweep retries every run
        assert env.mod.handle_successful_payment("D-1", "R1") is False
    env.mod._record_booking_error("B2C-1", "another reason")
    assert alerts == [(REQ, "B2C-1", "B2C-1 was paid but could not be booked: traceback"),
                      (REQ, "B2C-1", "B2C-1 was paid but could not be booked: another reason")]
    assert len([e for e in env.frappe.errors if "B2C accounting failed" in e[1].get("title", "")]) == 1


def test_a_b2c_paid_on_a_closed_day_is_booked_by_hand_with_its_receipt(b2c_env, monkeypatch, closed_period):  # noqa: F811
    env = b2c_env
    alerts = _alerts(monkeypatch)
    error = closed_period(env.frappe)
    _paid_row(env, 1, "SKT1AAAAAA")
    real = env.mod.create_payment_entry_for_doc

    def fail(disb, ref, submit=False):
        raise error(CLOSED)

    monkeypatch.setattr(env.mod, "create_payment_entry_for_doc", fail)
    assert env.mod.handle_successful_payment("D-1", "R1") is False
    hint = ("Its date is in a closed Accounting Period, in books closed by a Period Closing Voucher or on or before "
            "Accounts Frozen Till: make a Payment Entry with Reference No SKT1AAAAAA, allocated to Purchase Invoice "
            "PI-1, dated in an open period; the suite counts it as this payout's entry.")
    assert env.frappe.db.table(REQ)["B2C-1"]["accounting_error"] == f"traceback. {hint}"
    assert alerts == [(REQ, "B2C-1", f"B2C-1 was paid but could not be booked: traceback. {hint}")]
    # what the hint says: the entry staff make, dated later, carrying the receipt, books the payout (booked_docstatus)
    env.add_entry(PE, PER, {"docstatus": 1, "payment_type": "Pay", "reference_no": "SKT1AAAAAA",
                            "posting_date": datetime.date(2026, 11, 2)},
                  [{"reference_doctype": "Purchase Invoice", "reference_name": "PI-1", "allocated_amount": 100}])
    monkeypatch.setattr(env.mod, "create_payment_entry_for_doc", real)
    assert env.mod.handle_successful_payment("D-1", "R1") is True
    request = env.frappe.db.table(REQ)["B2C-1"]
    assert (request["accounting_booked"], request["accounting_error"]) == (1, None)
    assert len(env.frappe.db.table(PE)) == 1  # nothing booked twice


def test_a_b2c_salary_paid_on_a_closed_day_names_its_journal_entry(b2c_env):  # noqa: F811
    ref = fake_frappe._dict(reference_doctype="Salary Slip", reference_name="SS-1", party="EMP-1")
    assert b2c_env.mod.closed_period_hint(ref, "SKT1AAAAAA").startswith(
        "Its date is in a closed Accounting Period, in books closed by a Period Closing Voucher or on or before "
        "Accounts Frozen Till: make a Journal Entry with Reference Number SKT1AAAAAA and EMP-1 as party on the "
        "payroll payable row, dated in an open period")


# ---------------------------------------------------------------- a charge entry that fails waits on its own
def _savepoints(env, monkeypatch):
    """A rollback to a savepoint undoes the payout row's changes made after it, as the database does."""
    saved = {}
    table = env.frappe.db.table(STATEMENT_B2C)
    monkeypatch.setattr(env.frappe.db, "savepoint",
                        lambda name: saved.__setitem__(name, {k: dict(v) for k, v in table.items()}))

    def rollback(save_point=None):
        for name, values in saved[save_point].items():
            table[name].clear()
            table[name].update(values)

    monkeypatch.setattr(env.frappe.db, "rollback", rollback)


@pytest.mark.parametrize("closed", [False, True])
def test_a_charge_entry_that_fails_keeps_the_payouts_line_and_is_booked_later(
        statement_env, monkeypatch, closed_period, closed):  # noqa: F811
    env = statement_env
    _savepoints(env, monkeypatch)
    error = closed_period(env.frappe) if closed else env.frappe.ValidationError
    statement_b2c(env)

    def refused(p, bank_account, line, charge):
        raise error("You are not authorized to add or update entries before 31-10-2026" if closed else "No cost center")

    monkeypatch.setattr(env.statement, "charge_entry", refused)
    for _ in range(2):  # the result job, then the sweep: one Match Log (once), no Error Log
        env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    payout = env.frappe.db.table(STATEMENT_B2C)["B2C-1"]
    # the payout's line kept; the charge line waits, unlinked, for the sweep; the confirmed charge is kept
    assert (payout["bank_transaction"], payout["charge_bank_transaction"], payout["mpesa_charge"]) == (
        "BT-UJ2NI8UJ2A", None, 11)
    assert env.frappe.errors == []
    args, kw = env.calls.logs[0]
    assert (args, kw["on_change"], kw["reference_name"]) == (("Error", "Payout Charge"), True, "B2C-1")
    assert "once" not in kw  # a later, different reason is logged too
    why = ("its day is in a closed Accounting Period, in books closed by a Period Closing Voucher or on or before "
           "Accounts Frozen Till: make its Journal Entry by hand in an open period with Reference Number "
           "CHG-UJ2NI8UJ2A, which the suite uses" if closed else "it is tried again every 5 minutes")
    assert kw["details"].endswith(f"The payout's own line is kept and the charge waits: {why}.")
    assert env.calls.reconciled == [("BT-UJ2NI8UJ2A", "Mpesa-4139683 - AIL")] * 2  # never the charge line alone
    # the period opens (or the setting is fixed): the next sweep books the charge
    entries = []
    monkeypatch.setattr(env.statement, "charge_entry", lambda p, ba, line, charge: entries.append(line) or "JE-1")
    env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    assert entries == ["BT-CHG-UJ2NI8UJ2A"]
    assert (payout["charge_bank_transaction"], payout["mpesa_charge"]) == ("BT-CHG-UJ2NI8UJ2A", 11)


def _match_logs(env, monkeypatch):
    """Mpesa Match Log rows as money_in.common.log_match writes them for ``on_change``: a repeat of the
    latest reason refreshes that row, a different reason gets its own (test_fix_register covers it)."""
    rows = []

    def log(result, method=None, on_change=False, **kw):
        if on_change and rows and rows[-1][:2] == (result, method) and rows[-1][2] == kw["details"]:
            return
        rows.append((result, method, kw["details"]))

    monkeypatch.setattr(env.statement, "log_match", log)
    return rows


def test_a_later_different_refusal_of_the_charge_entry_is_logged_too(
        statement_env, monkeypatch, closed_period):  # noqa: F811
    env = statement_env
    _savepoints(env, monkeypatch)
    rows = _match_logs(env, monkeypatch)
    error = closed_period(env.frappe)
    statement_b2c(env)
    reasons = iter([env.frappe.ValidationError("No cost center")] * 2 + [error(CLOSED)] * 2)

    def refused(p, bank_account, line, charge):
        raise next(reasons)

    monkeypatch.setattr(env.statement, "charge_entry", refused)
    for _ in range(4):  # the result job, then the sweep: a setting first, then a closed day
        env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    assert [(r[0], r[1]) for r in rows] == [("Error", "Payout Charge")] * 2
    assert "tried again every 5 minutes" in rows[0][2]
    assert CLOSED in rows[1][2] and "make its Journal Entry by hand" in rows[1][2]


def test_a_retry_the_balances_no_longer_confirm_books_the_charge_the_line_was_made_for(
        statement_env, monkeypatch):  # noqa: F811
    # a B2C charge seen only in the balances (no B2C Charge Bands): its entry refused, the balances cannot show
    # it on the retry (a payout sent since is still in flight); the kept line is booked with the charge it was made
    # for, never linked bare
    env = statement_env
    _savepoints(env, monkeypatch)
    env.settings.b2c_charge_bands = ""
    seen = iter([11.0])
    monkeypatch.setattr(env.statement, "balance_charge", lambda p: next(seen, None))
    lines = env.frappe.db.table("Bank Transaction")
    monkeypatch.setattr(env.statement, "find_existing",
                        lambda ba, key, withdrawal=None: f"BT-{key}" if f"BT-{key}" in lines else None)
    statement_b2c(env)

    def refused(p, bank_account, line, charge):
        raise env.frappe.ValidationError("No cost center")

    monkeypatch.setattr(env.statement, "charge_entry", refused)
    env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    payout = env.frappe.db.table(STATEMENT_B2C)["B2C-1"]
    assert (payout["charge_bank_transaction"], payout["mpesa_charge"]) == (None, 11)
    entries = []
    monkeypatch.setattr(env.statement, "charge_entry",
                        lambda p, ba, line, charge: entries.append((line, charge)) or "JE-1")
    env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    assert entries == [("BT-CHG-UJ2NI8UJ2A", 11)]
    assert (payout["charge_bank_transaction"], payout["mpesa_charge"]) == ("BT-CHG-UJ2NI8UJ2A", 11)


def test_a_retry_never_books_a_charge_the_bands_and_balances_now_disagree_on(statement_env, monkeypatch):  # noqa: F811
    # the line was made for the tariff (11) and its entry refused; on the retry the balances show 13: charge_for
    # logs it for staff to book by hand, so the stored 11 is not booked behind that log
    env = statement_env
    _savepoints(env, monkeypatch)
    env.settings.b2c_charge_bands = "20000 = 11"  # 11 on the 5,300 payout
    seen = iter([None, 13.0, 13.0])
    monkeypatch.setattr(env.statement, "balance_charge", lambda p: next(seen, 13.0))
    lines = env.frappe.db.table("Bank Transaction")
    monkeypatch.setattr(env.statement, "find_existing",
                        lambda ba, key, withdrawal=None: f"BT-{key}" if f"BT-{key}" in lines else None)
    statement_b2c(env)

    def refused(p, bank_account, line, charge):
        raise env.frappe.ValidationError("No cost center")

    monkeypatch.setattr(env.statement, "charge_entry", refused)
    env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    payout = env.frappe.db.table(STATEMENT_B2C)["B2C-1"]
    assert (payout["charge_bank_transaction"], payout["mpesa_charge"]) == (None, 11)
    entries = []
    monkeypatch.setattr(env.statement, "charge_entry",
                        lambda p, ba, line, charge: entries.append((line, charge)) or "JE-1")
    env.statement.after_payout(STATEMENT_B2C, "B2C-1")
    assert entries == [("BT-CHG-UJ2NI8UJ2A", None)]  # never the stored 11: charge_entry books nothing for None
    disputed = [kw for a, kw in env.calls.logs if kw.get("details", "").startswith("B2C Charge Bands give 11")]
    assert disputed and "Reference Number CHG-UJ2NI8UJ2A" in disputed[0]["details"]


def test_a_charge_line_the_suite_cannot_book_leaves_a_match_log(statement_env, monkeypatch):  # noqa: F811
    # charge_entry itself: a line found for a charge the suite cannot confirm, with no entry, is never linked quietly
    env = statement_env
    spec = importlib.util.find_spec(env.statement.__name__)
    statement = importlib.util.module_from_spec(spec)  # its own charge_entry (the fixture's is a stub)
    spec.loader.exec_module(statement)
    logs = []
    monkeypatch.setattr(statement, "log_match", lambda *a, **kw: logs.append((a, kw)))
    env.frappe.db.table("Bank Transaction")["ACC-BTN-1"] = {"name": "ACC-BTN-1", "withdrawal": 11.0, "docstatus": 1}
    p = fake_frappe._dict(doctype=STATEMENT_B2C, name="B2C-1", receipt="UJ2NI8UJ2A")
    assert statement.charge_entry(p, "Mpesa B2C 4139683", "ACC-BTN-1", None) is None
    ((args, kw),) = logs
    assert (args, kw["on_change"], kw["amount"]) == (("Ambiguous", "Payout Charge"), True, 11.0)
    assert kw["details"] == ("Charge line ACC-BTN-1 has no Journal Entry and the suite cannot confirm the charge on "
                             "UJ2NI8UJ2A: check the line and book its Journal Entry by hand with Reference Number "
                             "CHG-UJ2NI8UJ2A.")


# ---------------------------------------------------------------- the Make Entry button and dialog
ENTRY_ACTIONS = r"""
global.format_number = (v) => String(v);
frappe.datetime = { str_to_user: (d) => d };
frappe.boot.versions = {};
frappe.user = { has_role: () => true };
(0, eval)(fs.readFileSync(process.env.MPSA_B2B_JS, "utf8") + "\nglobalThis.mpsa_b2b = mpsa_b2b;");
const frm = mk_frm("Mpesa B2B Payment Request", { status: "Paid", entry_status: "Entry Not Made", entry_note: "x" });
mpsa_b2b.entry_actions(frm);
const out = { buttons: frm.buttons.filter((b) => !b.startsWith("headline")) };
if (frm.actions["Make Entry"]) {
	await mpsa_b2b.make_entry({ doc: { name: "MB2B-1" }, reload_doc: () => {} });
	const field = (name) => last_dialog.opts.fields.find((f) => f.fieldname === name);
	out.kinds = field("kind").options.split("\n");
	out.kind = field("kind").default;
	out.posting_date = [field("posting_date").fieldtype, field("posting_date").default || null];
	last_dialog.opts.primary_action({ kind: out.kind, posting_date: "2026-11-02" });
	await tick();
	out.sent = calls.find((c) => c[1] === "make_entry")[2];
}
"""
FACTS = {"amount": 4000, "date": "2026-10-17", "receipt": "R1", "paid_to": "ACME", "gl": "M-Pesa - AIL",
         "supplier": "ACME", "employee": None, "company": "Astral"}


def entry_actions(monkeypatch, no_submit=()):
    monkeypatch.setenv("MPSA_B2B_JS", str(B2B_JS))
    return run(ENTRY_ACTIONS, {"no_submit": list(no_submit), "replies": {
        "entry_facts": FACTS, "make_entry": {"doctype": "Payment Entry", "name": "PE-1"}}})


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_make_entry_is_offered_with_a_posting_date_to_who_may_submit_the_entry(monkeypatch):
    out = entry_actions(monkeypatch)
    assert out["buttons"] == ["Accounting > Make Entry", "Accounting > Check Entry Again"]
    assert out["kinds"] == ["Purchase Invoice", "Purchase Order", "Supplier Advance", "Journal Entry"]
    assert (out["kind"], out["posting_date"]) == ("Supplier Advance", ["Date", None])  # empty: the day it was paid
    assert out["sent"] == {"name": "MB2B-1", "kind": "Supplier Advance", "posting_date": "2026-11-02"}


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_make_entry_offers_only_the_entries_the_user_may_submit(monkeypatch):
    out = entry_actions(monkeypatch, no_submit=["Payment Entry"])
    assert out["kinds"] == ["Journal Entry"] and out["kind"] == "Journal Entry"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_make_entry_is_hidden_from_who_may_submit_neither_entry(monkeypatch):
    # the server refuses them (make_entry needs submit permission); Check Entry Again stays
    out = entry_actions(monkeypatch, no_submit=["Payment Entry", "Journal Entry"])
    assert out["buttons"] == ["Accounting > Check Entry Again"]
