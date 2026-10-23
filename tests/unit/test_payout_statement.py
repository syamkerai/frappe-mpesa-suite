"""Payout statement lines (money_out.statement) on an in-memory frappe.

Charges: B2B results state theirs; a B2C charge comes from the B2C Charge Bands, checked
against the fall in wallet balance since the payout before (the balances below are Astral's
real 4139683 payouts of 2 Oct 2026, eight of them completing in one second). Lines: a
withdrawal line keyed by the receipt and a charge line keyed "CHG-<receipt>" on the
shortcode's Bank Account, an existing line (Astral's feed) found instead of a second one,
both reconciled; and the result jobs make them right after booking the payout.
"""

import datetime
import importlib
import sys
import types

import pytest

import fake_frappe

STATEMENT = "frappe_mpsa_payments.money_out.statement"
B2C, B2B, BT = "B2C Disbursement Request", "Mpesa B2B Payment Request", "Bank Transaction"
BANDS = "100 = 0\n1500 = 5\n5000 = 9\n20000 = 11\n250000 = 13"


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.commit = lambda: None
        frappe.db.db_type = "fake"  # named_lock: no GET_LOCK outside MariaDB
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        statement = importlib.import_module(STATEMENT)
        settings = fake_frappe._dict(create_bank_transactions=1, auto_reconcile_bank_transactions=1,
                                     book_payout_charges=1, reconcile_payout_lines=1, b2c_charge_bands=BANDS, payout_lines_from=None, matching_lookback_days=45)
        frappe.get_cached_doc = lambda doctype, *a, **kw: settings
        calls = fake_frappe._dict(lines=[], entries=[], reconciled=[], logs=[])

        def create_statement_line(**kw):
            calls.lines.append(kw)
            name = f"BT-{kw['receipt']}"
            frappe.db.table(BT).setdefault(name, {"name": name, "withdrawal": kw["amount"], "docstatus": 1})
            return name

        monkeypatch.setattr(statement, "create_statement_line", create_statement_line)
        monkeypatch.setattr(statement, "find_existing", lambda ba, key, withdrawal=None: None)
        monkeypatch.setattr(statement, "line_elsewhere", lambda ba, key: None)
        monkeypatch.setattr(statement, "payout_bank_account", lambda p: "Mpesa B2C 4139683")
        monkeypatch.setattr(statement, "erpnext_installed", lambda: True)
        monkeypatch.setattr(statement, "balance_charge", lambda p: None)
        monkeypatch.setattr(statement, "charge_entry",
                            lambda p, ba, line, charge: calls.entries.append((line, charge)) or f"JE-{line}")
        monkeypatch.setattr(statement, "reconcile_withdrawal", lambda name, gl: calls.reconciled.append((name, gl)))
        monkeypatch.setattr(statement, "log_match", lambda *a, **kw: calls.logs.append((a, kw)))
        frappe.db.table("Bank Account")["Mpesa B2C 4139683"] = {"name": "Mpesa B2C 4139683",
                                                                  "account": "Mpesa-4139683 - AIL"}
        yield fake_frappe._dict(frappe=frappe, statement=statement, settings=settings, calls=calls)
    finally:
        fake_frappe.uninstall(saved)


def b2c(env, name="B2C-1", **values):
    row = {"name": name, "docstatus": 1, "status": "Paid", "transaction_id": "UJ2NI8UJ2A", "amount": 5300.0,
           "mpesa_settings": "4139683", "creation": datetime.datetime(2026, 10, 2, 16, 30, 10),
           "transaction_completed_datetime": datetime.datetime(2026, 10, 2, 16, 30, 15),
           "posting_date": datetime.date(2026, 10, 2), "party_type": "Employee", "party": "HR-EMP-0001",
           "reference_doctype": "Expense Claim", "reference_name": "HR-EXP-2026-0001",
           "receiver_public_name": "254712345678 - JANE DOE", "phone_number": "254712345678",
           "utility_acct_avlbl_funds": 214664.01, "charges_paid_acct_avlbl_funds": 0.0,
           "bank_transaction": None, "mpesa_charge": 0, "charge_bank_transaction": None}
    row.update(values)
    env.frappe.db.table(B2C)[name] = row
    return row


def b2b(env, name="MB2B-26-10-000012", **values):
    row = {"name": name, "docstatus": 1, "status": "Paid", "transaction_id": "UJMTM632YM", "amount": 10.0,
           "mpesa_settings": "Astral 4139683 LIVE", "creation": datetime.datetime(2026, 10, 22, 11, 0),
           "trans_completed_time": datetime.datetime(2026, 10, 22, 11, 0, 5), "supplier": "Astral Industries",
           "purchase_invoice": None, "receiver_shortcode": "208911", "command_id": "BusinessPayBill",
           "receiver_public_name": "208911 - ASTRAL INDUSTRIES LIMITED",
           "debit_party_charges": "Business Pay Bill Charge|KES|2.00",
           "bank_transaction": None, "mpesa_charge": 0, "charge_bank_transaction": None}
    row.update(values)
    env.frappe.db.table(B2B)[name] = row
    return row


# ---------------------------------------------------------------- charges
@pytest.fixture
def pure():
    frappe, saved = fake_frappe.install()
    try:
        yield importlib.import_module(STATEMENT)
    finally:
        fake_frappe.uninstall(saved)


@pytest.mark.parametrize("value, charge", [
    ("Business Pay Bill Charge|KES|2.00", 2.0),
    ("Business Buy Goods Charge|KES|1,000.50&Excise Duty|KES|0.30", 1000.8),
    ("", 0.0),
    (None, 0.0),
    ("Business Pay Bill Charge|KES|", 0.0),
])
def test_b2b_charge_is_read_from_debit_party_charges(pure, value, charge):
    assert pure.parse_charges(value) == charge


def test_charge_bands_follow_safaricoms_tariff(pure):
    bands = pure.parse_bands(BANDS + "\n\n# comments and blank lines are fine")
    assert [pure.band_charge(bands, a) for a in (10, 100, 101, 1500, 1501, 5000, 5006, 20000, 21000, 250000)] == [
        0, 0, 5, 5, 9, 9, 11, 11, 13, 13]
    assert pure.band_charge(bands, 250001) is None  # above the last band: unknown
    assert pure.parse_bands("1500: 5\n5,000 = 9") == [(1500, 5), (5000, 9)]
    assert pure.parse_bands("") == []


@pytest.mark.parametrize("text", ["1500 5", "1500 = five", "5000 = 9\n1500 = 5", "1500 = -5", "1500 = 5 = 6"])
def test_unreadable_charge_bands_are_refused(pure, text):
    with pytest.raises(ValueError):
        pure.parse_bands(text)


def paid(name, at, amount, utility, charges_paid=0.0):
    return fake_frappe._dict(name=name, completed=datetime.datetime(2026, 10, 2, *at), amount=amount,
                             utility_acct_avlbl_funds=utility, charges_paid_acct_avlbl_funds=charges_paid)


# Astral 4139683 on 2 Oct 2026: B2C-26-10-000066..074; 067..074 completed in one second,
# their results arriving in any order.
REAL = [
    paid("066", (11, 37, 48), 3174, 341138.01),
    paid("070", (11, 37, 50), 8316, 325158.01),
    paid("068", (11, 37, 50), 4455, 333485.01),
    paid("072", (11, 37, 50), 3394, 320370.01),
    paid("067", (11, 37, 50), 3180, 337949.01),
    paid("074", (11, 37, 50), 1500, 313141.01),
    paid("069", (11, 37, 50), 1380, 323773.01),
    paid("071", (11, 37, 50), 3456, 316905.01),
    paid("073", (11, 37, 50), 2250, 314646.01),
]


def test_balances_show_each_payouts_charge_in_one_second(pure):
    bands = pure.parse_bands(BANDS)
    for payout in REAL[1:]:
        pred = pure.predecessor([r for r in REAL if r is not payout], payout)
        assert pure.seen_charge(pred, payout) == pure.band_charge(bands, payout.amount), payout.name


def test_a_missing_or_balance_less_payout_hides_the_charge(pure):
    rows = [r for r in REAL if r.name not in ("069", "070")]
    payout = next(r for r in REAL if r.name == "069")
    # 070's result is not in yet: the fall since 068 includes its 8,316 + 11
    assert pure.seen_charge(pure.predecessor(rows, payout), payout) is None
    no_balance = paid("070", (11, 37, 50), 8316, 0.0)
    assert pure.predecessor(rows + [no_balance], payout).name == "068"
    assert pure.predecessor([paid("x", (11, 0, 0), 10, 0.0)], payout) is None


def test_a_top_up_between_hides_the_charge(pure):
    before, after = paid("1", (9, 0, 0), 1000, 1005.0), paid("2", (10, 0, 0), 1000, 50000.0)
    assert pure.seen_charge(before, after) is None


def test_charges_paid_account_counts_when_safaricom_uses_it(pure):
    # Daraja's docs: the charge goes to Charges Paid (negative), Utility falls by the amount only
    before = paid("1", (9, 0, 0), 1000, 10000.0, -1540.0)
    after = paid("2", (9, 0, 5), 1000, 9000.0, -1545.0)
    assert pure.seen_charge(before, after) == 5


def test_b2c_charge_is_the_band_unless_the_balance_shows_another(env, monkeypatch):
    p = env.statement.load(B2C, b2c(env)["name"])
    assert env.statement.charge_for(p) == 11 and env.calls.logs == []  # balances cannot tell

    monkeypatch.setattr(env.statement, "balance_charge", lambda p: 11.0)
    assert env.statement.charge_for(p) == 11 and env.calls.logs == []  # they agree

    monkeypatch.setattr(env.statement, "balance_charge", lambda p: 13.0)
    assert env.statement.charge_for(p) is None  # not certain: left for staff
    (result, method), kw = env.calls.logs[0]
    assert (result, method, kw["reference_name"]) == ("Ambiguous", "Payout Charge", "B2C-1")
    assert "No charge was booked" in kw["details"]


def test_disputed_b2c_charge_gets_no_line(env, monkeypatch):
    b2c(env)
    monkeypatch.setattr(env.statement, "balance_charge", lambda p: 13.0)
    env.statement.after_payout(B2C, "B2C-1")
    assert [line["receipt"] for line in env.calls.lines] == ["UJ2NI8UJ2A"] and env.calls.entries == []


def test_without_bands_the_balance_charge_is_booked_or_waited_for(env, monkeypatch):
    env.settings.b2c_charge_bands = ""
    p = env.statement.load(B2C, b2c(env)["name"])
    assert env.statement.charge_for(p) is None
    monkeypatch.setattr(env.statement, "balance_charge", lambda p: 11.0)
    assert env.statement.charge_for(p) == 11


# ---------------------------------------------------------------- lines
def test_paid_b2c_gets_its_line_and_charge_line_reconciled(env):
    b2c(env)
    env.statement.after_payout(B2C, "B2C-1")

    payout, charge = env.calls.lines
    assert (payout["receipt"], payout["amount"], payout["withdrawal"], payout["date"]) == (
        "UJ2NI8UJ2A", 5300.0, True, datetime.date(2026, 10, 2))
    assert (payout["party_type"], payout["party"], payout["transaction_type"]) == (
        "Employee", "HR-EMP-0001", "M-Pesa B2C")
    assert "HR-EXP-2026-0001" in payout["description"] and "B2C-1" in payout["description"]
    assert (charge["receipt"], charge["amount"], charge["transaction_type"]) == ("CHG-UJ2NI8UJ2A", 11, "M-Pesa Charges")
    assert env.calls.entries == [("BT-CHG-UJ2NI8UJ2A", 11)]
    assert env.calls.reconciled == [("BT-UJ2NI8UJ2A", "Mpesa-4139683 - AIL"),
                                    ("BT-CHG-UJ2NI8UJ2A", "Mpesa-4139683 - AIL")]
    row = env.frappe.db.table(B2C)["B2C-1"]
    assert (row["bank_transaction"], row["charge_bank_transaction"], row["mpesa_charge"]) == (
        "BT-UJ2NI8UJ2A", "BT-CHG-UJ2NI8UJ2A", 11)


def test_paid_b2b_line_and_its_stated_charge(env):
    b2b(env)
    env.statement.after_payout(B2B, "MB2B-26-10-000012")
    payout, charge = env.calls.lines
    assert (payout["receipt"], payout["amount"], payout["party_type"], payout["party"]) == (
        "UJMTM632YM", 10.0, "Supplier", "Astral Industries")
    assert payout["transaction_type"] == "M-Pesa B2B" and "208911 - ASTRAL" in payout["description"]
    assert (charge["receipt"], charge["amount"]) == ("CHG-UJMTM632YM", 2.0)
    assert env.frappe.db.table(B2B)["MB2B-26-10-000012"]["mpesa_charge"] == 2.0


def test_b2b_without_charges_gets_only_its_line(env):
    b2b(env, debit_party_charges="")
    env.statement.after_payout(B2B, "MB2B-26-10-000012")
    assert [line["receipt"] for line in env.calls.lines] == ["UJMTM632YM"]
    assert env.calls.entries == []


@pytest.mark.parametrize("state", [{"status": "Failed"}, {"status": "Initiated"}, {"docstatus": 2},
                                   {"transaction_id": ""}, {"transaction_id": None}])
def test_only_a_paid_payout_with_a_receipt_gets_lines(env, state):
    b2c(env, **state)
    env.statement.after_payout(B2C, "B2C-1")
    assert env.calls.lines == []


def test_switches_off_make_nothing_or_leave_reconciling(env):
    b2c(env)
    env.settings.create_bank_transactions = 0
    env.statement.after_payout(B2C, "B2C-1")
    assert env.calls.lines == []
    env.settings.create_bank_transactions, env.settings.auto_reconcile_bank_transactions = 1, 0
    env.statement.after_payout(B2C, "B2C-1")
    assert len(env.calls.lines) == 2 and env.calls.reconciled == []
    env.settings.auto_reconcile_bank_transactions, env.settings.reconcile_payout_lines = 1, 0
    env.statement.after_payout(B2C, "B2C-1")
    assert env.calls.reconciled == []


def test_charges_switch_off_makes_only_the_payout_line(env):
    b2c(env)
    env.settings.book_payout_charges = 0
    env.statement.after_payout(B2C, "B2C-1")
    assert [line["receipt"] for line in env.calls.lines] == ["UJ2NI8UJ2A"] and env.calls.entries == []
    assert env.calls.reconciled == [("BT-UJ2NI8UJ2A", "Mpesa-4139683 - AIL")]


def test_payout_before_payout_lines_from_is_left_alone(env):
    b2c(env)
    env.settings.payout_lines_from = datetime.date(2026, 10, 3)
    env.statement.after_payout(B2C, "B2C-1")
    assert env.calls.lines == []
    env.settings.payout_lines_from = datetime.date(2026, 10, 2)
    env.statement.after_payout(B2C, "B2C-1")
    assert len(env.calls.lines) == 2


def test_no_mapped_bank_account_is_logged_once(env, monkeypatch):
    b2c(env)
    monkeypatch.setattr(env.statement, "payout_bank_account", lambda p: None)
    env.statement.after_payout(B2C, "B2C-1")
    assert env.calls.lines == []
    (result, method), kw = env.calls.logs[0]
    assert (result, kw["once"], kw["reference_name"]) == ("Error", True, "B2C-1")


def test_line_on_another_mpesa_account_is_not_made_twice(env, monkeypatch):
    b2c(env)
    monkeypatch.setattr(env.statement, "line_elsewhere",
                        lambda ba, key: fake_frappe._dict(name="ACC-BTN-2026-13432", bank_account="Old 4139683"))
    env.statement.after_payout(B2C, "B2C-1")
    assert env.calls.lines == [] and env.calls.reconciled == []
    (result, method), kw = env.calls.logs[0]
    assert result == "Ambiguous" and "ACC-BTN-2026-13432" in kw["details"]


def test_charge_line_astrals_feed_made_is_used_while_the_charge_is_unknown(env, monkeypatch):
    env.settings.b2c_charge_bands = ""
    b2c(env)
    env.frappe.db.table(BT)["ACC-BTN-1"] = {"name": "ACC-BTN-1", "withdrawal": 11.0, "docstatus": 1}
    monkeypatch.setattr(env.statement, "find_existing",
                        lambda ba, key, withdrawal=None: "ACC-BTN-1" if key == "CHG-UJ2NI8UJ2A" else None)
    env.statement.after_payout(B2C, "B2C-1")
    assert [line["receipt"] for line in env.calls.lines] == ["UJ2NI8UJ2A"]
    assert env.calls.entries == [("ACC-BTN-1", None)]
    row = env.frappe.db.table(B2C)["B2C-1"]
    assert (row["charge_bank_transaction"], row["mpesa_charge"]) == ("ACC-BTN-1", 11.0)


def test_a_failing_line_never_breaks_the_result_job(env, monkeypatch):
    b2c(env)

    def boom(**kw):
        raise RuntimeError("Bank Account frozen")

    monkeypatch.setattr(env.statement, "create_statement_line", boom)
    env.statement.after_payout(B2C, "B2C-1")  # must not raise
    assert env.frappe.errors


def test_sweep_skips_a_b2b_that_was_charged_nothing(env, monkeypatch):
    b2b(env, debit_party_charges="", bank_transaction="BT-UJMTM632YM")
    b2b(env, name="MB2B-2", transaction_id="UJMTM6331C")
    fed = []
    monkeypatch.setattr(env.statement, "feed", lambda doctype, name: fed.append(name))
    env.frappe.get_all = lambda doctype, **kw: env.frappe.db.get_all(doctype, filters=kw.get("filters"))
    env.statement.sweep_missing()
    assert fed == ["MB2B-2"]


# ---------------------------------------------------------------- reconciliation
@pytest.fixture
def recon(env, monkeypatch):
    statement = importlib.reload(sys.modules[STATEMENT])  # undo the fixture's stand-ins
    monkeypatch.setattr(statement, "log_match", lambda *a, **kw: env.calls.logs.append((a, kw)))
    tool = types.ModuleType("erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool")
    tool.reconcile_vouchers = lambda name, vouchers: env.calls.reconciled.append(
        (name, vouchers, env.frappe.session.user))
    for module in ("erpnext", "erpnext.accounts", "erpnext.accounts.doctype",
                   "erpnext.accounts.doctype.bank_reconciliation_tool"):
        monkeypatch.setitem(sys.modules, module, types.ModuleType(module))
    monkeypatch.setitem(sys.modules, tool.__name__, tool)
    env.frappe.db.table(BT)["ACC-BTN-9"] = {"name": "ACC-BTN-9", "reference_number": "UJ2NI8UJ2A",
                                             "transaction_id": "UJ2NI8UJ2A", "unallocated_amount": 5300.0,
                                             "docstatus": 1}
    env.frappe.session.user = "Guest"
    return statement


def test_payout_line_is_reconciled_with_its_entry_as_administrator(env, recon, monkeypatch):
    monkeypatch.setattr(recon, "entries_for", lambda key, gl: [("Payment Entry", "ACC-PAY-1", 5300.0)])
    assert recon.reconcile_withdrawal("ACC-BTN-9", "Mpesa-4139683 - AIL") == "ACC-PAY-1"
    name, vouchers, user = env.calls.reconciled[0]
    assert name == "ACC-BTN-9" and '"payment_name": "ACC-PAY-1"' in vouchers and user == "Administrator"
    assert env.frappe.session.user == "Guest"


def test_only_the_one_entry_with_exactly_its_amount_is_reconciled(env, recon, monkeypatch):
    monkeypatch.setattr(recon, "entries_for", lambda key, gl: [("Payment Entry", "ACC-PAY-1", 5000.0),
                                                              ("Journal Entry", "ACC-JV-1", 5300.0)])
    assert recon.reconcile_withdrawal("ACC-BTN-9", "Mpesa-4139683 - AIL") == "ACC-JV-1"
    assert env.calls.logs == []


@pytest.mark.parametrize("found, reason", [
    ([("Payment Entry", "ACC-PAY-1", 5000.0)], "no entry has that amount"),
    ([("Payment Entry", "ACC-PAY-1", 5300.0), ("Payment Entry", "ACC-PAY-2", 5300.0)], "several entries match"),
])
def test_unclear_match_is_left_for_staff(env, recon, monkeypatch, found, reason):
    monkeypatch.setattr(recon, "entries_for", lambda key, gl: found)
    assert recon.reconcile_withdrawal("ACC-BTN-9", "Mpesa-4139683 - AIL") is None
    assert env.calls.reconciled == []
    (result, method), kw = env.calls.logs[0]
    assert (result, method) == ("Ambiguous", "Bank Reconciliation") and reason in kw["details"]


def test_line_with_no_entry_is_left_quietly(env, recon, monkeypatch):
    monkeypatch.setattr(recon, "entries_for", lambda key, gl: [])
    assert recon.reconcile_withdrawal("ACC-BTN-9", "Mpesa-4139683 - AIL") is None
    assert env.calls.logs == [] and env.calls.reconciled == []


def test_reconciled_line_is_left_alone(env, recon, monkeypatch):
    env.frappe.db.table(BT)["ACC-BTN-9"]["unallocated_amount"] = 0
    monkeypatch.setattr(recon, "entries_for", lambda key, gl: pytest.fail("looked for entries"))
    assert recon.reconcile_withdrawal("ACC-BTN-9", "Mpesa-4139683 - AIL") is None


# ---------------------------------------------------------------- result jobs
def test_b2c_result_job_books_then_makes_the_lines(env, monkeypatch):
    calls = []
    post = types.ModuleType("frappe_mpsa_payments.services.b2c_postprocess_service")
    post.handle_successful_payment = lambda disbursement, row: calls.append(("book", disbursement, row))
    monkeypatch.setitem(sys.modules, post.__name__, post)
    monkeypatch.setattr(env.statement, "after_payout", lambda doctype, name: calls.append(("lines", doctype, name)))
    resp = importlib.import_module("frappe_mpsa_payments.services.b2c_response_service")
    b2c(env, b2c_payment_disbursement="D-1", b2c_payment_disbursement_reference=None)

    resp.update_b2c_reference_status("B2C-1", enqueue_next=True)
    assert calls == [("book", "D-1", None), ("lines", B2C, "B2C-1")]

    calls.clear()
    resp.update_b2c_reference_status("B2C-1")  # a status update only: nothing booked, no lines
    assert calls == []


def test_b2b_booking_job_books_then_makes_the_lines(env, monkeypatch):
    b2b_module = importlib.import_module("frappe_mpsa_payments.money_out.b2b")
    calls = []
    monkeypatch.setattr(b2b_module, "_book_payment_entry", lambda name: calls.append(("book", name)) or "ACC-PAY-22")
    monkeypatch.setattr(env.statement, "after_payout", lambda doctype, name: calls.append(("lines", doctype, name)))
    assert b2b_module.book_payment_entry("MB2B-26-10-000013") == "ACC-PAY-22"
    assert calls == [("book", "MB2B-26-10-000013"), ("lines", B2B, "MB2B-26-10-000013")]


# ---------------------------------------------------------------- settings and upgrade
def test_settings_refuse_unreadable_bands(env):
    env.frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
    env.frappe.logger = lambda *a, **kw: None
    module = importlib.import_module(
        "frappe_mpsa_payments.frappe_mpsa_payments.doctype.m_pesa_suite_settings.m_pesa_suite_settings")
    doc = module.MPesaSuiteSettings()
    doc.get_password = lambda *a, **kw: "token"
    doc.enforce_ip_allowlist = 0
    doc.get = lambda field, default=None: getattr(doc, field, default)
    doc.b2c_charge_bands = BANDS
    doc.validate()
    doc.b2c_charge_bands = "5000 = 9\n1500 = 5"
    with pytest.raises(env.frappe.ValidationError):
        doc.validate()


@pytest.mark.parametrize("stored, expected", [
    ([("create_bank_transactions", "1")], "2026-10-22"),                 # upgraded site: starts today
    ([("payout_lines_from", "2026-09-01")], None),                       # already chosen: kept
    ([], None),                                                          # never saved: nothing fed
])
def test_upgrade_starts_payout_lines_at_the_upgrade_date(env, stored, expected):
    patch = importlib.import_module("frappe_mpsa_payments.frappe_mpsa_payments.patches.payout_lines_from_upgrade_date")
    written = []
    env.frappe.db.exists = lambda doctype, name=None: True
    env.frappe.db.sql = lambda query, values=None, **kw: stored
    env.frappe.db.set_single_value = lambda doctype, field, value: written.append((field, value))
    patch.nowdate = lambda: "2026-10-22"
    patch.execute()
    assert written == ([("payout_lines_from", expected)] if expected else [])


def test_a_receipt_on_both_sides_of_one_account_is_two_lines(env):
    # a B2B between two shortcodes mapped to one Bank Account: out of one, into the other
    bank_transaction = importlib.import_module("frappe_mpsa_payments.money_in.bank_transaction")
    env.frappe.db.table(BT)["ACC-BTN-IN"] = {"name": "ACC-BTN-IN", "reference_number": "UJMTM632YM",
                                              "transaction_id": "UJMTM632YM", "bank_account": "Mpesa",
                                              "docstatus": 1, "deposit": 10.0, "withdrawal": 0}
    assert bank_transaction.find_existing("Mpesa", "UJMTM632YM", withdrawal=True) is None
    assert bank_transaction.find_existing("Mpesa", "UJMTM632YM", withdrawal=False) == "ACC-BTN-IN"
    assert bank_transaction.find_existing("Mpesa", "UJMTM632YM") == "ACC-BTN-IN"  # any direction
