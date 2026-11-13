"""Round 4.5: the petty cash phone (money_out.petty_cash), on the in-memory site of test_payroll_schedule (SQL on
SQLite) with the real B2C Payment Disbursement, row and request controllers and the party-less branch
(money_out.partyless) the phone's top-ups go through.

Covers: the SMS reader against Safaricom's formats (Buy Goods, Paybill with its account, send money with the
person's phone, the time of day, a masked phone, text that is no payment SMS, a received-money SMS); the fields
typed by hand agreeing with the SMS; the phone's checks (a Safaricom number, one phone per number and per
account, a Cash account, an active holder with a User, the limits) and its approval (Approve only, a manager who
did not enter it and does not hold it); top-ups (the default amount, the top-up limit and the ceiling with what is
on its way, one at a time, never by the holder, nor sent by them), their party-less disbursement and send, the
Result on the top-up row, one Journal Entry per paid top-up (petty cash debited, the Utility GL credited, the
receipt as Cheque No) found by the statement's own rule, a failed top-up retried, the M-Pesa name checked on the
first top-up; spends recorded from the SMS (the holder or Accounts roles, the receipt once, at most what the order
or invoice owes and the phone's book balance, the Payment Entry's accounts, references and charge), the name on
the SMS kept; the book balance against the last SMS and the daily section; the hooks, fields and DocTypes.
"""

import datetime
import importlib
import json
import sys
import types
from pathlib import Path

import b2b_fixture
import fake_frappe
import pytest
from test_payroll_schedule import DISB, PAYER, REQ, ROW, SETTINGS
from test_payroll_schedule import env as payroll_env  # noqa: F401 (the fixture this one extends)

APP = Path(__file__).resolve().parents[2] / "mpsa_suite"
PHONE, TOP_UP = "M-Pesa Petty Cash Phone", "M-Pesa Petty Cash Top Up"
PE, PER, JE, JEA = b2b_fixture.PE, b2b_fixture.PER, b2b_fixture.JE, b2b_fixture.JEA
GL = b2b_fixture.GL
GLE, BT, BTP = "GL Entry", "Bank Transaction", "Bank Transaction Payments"
CASH, CHARGES = "Petty Cash M-Pesa 0722 - AIL", "M-Pesa Charges - AIL"
NUMBER = "254722000999"
HOLDER, ACCOUNTS, MANAGER = "holder@example.com", "accounts@example.com", "manager@example.com"
LATER = datetime.datetime(2026, 11, 13, 18, 0, 0)
_dict = fake_frappe._dict

TILL_SMS = ("UKD1ABC2DE Confirmed. Ksh1,500.00 paid to SHELL KILIMANI. on 13/11/26 at 10:15 AM. New M-PESA balance "
            "is Ksh8,500.00. Transaction cost, Ksh0.00. Amount you can transact within the day is 498,500.00. Save "
            "frequent Tills for quick payment on M-PESA app https://bit.ly/mpesalnk")
PAYBILL_SMS = ("UKD1ABC2DF Confirmed. Ksh2,000.00 sent to KPLC PREPAID for account 12345 on 13/11/26 at 11:02 AM New "
               "M-PESA balance is Ksh6,477.00. Transaction cost, Ksh23.00.Amount you can transact within the day is "
               "496,500.00. Save frequent paybills for quick payment on M-PESA app https://bit.ly/mpesalnk")
SEND_SMS = ("UKD1ABC2DG Confirmed. Ksh500.00 sent to JOHN DOE 0712345678 on 13/11/26 at 12:30 PM. New M-PESA balance "
            "is Ksh5,970.00. Transaction cost, Ksh7.00. Amount you can transact within the day is 496,000.00. Earn "
            "interest daily on Ziidi MMF, Dial *334#")


# ---------------------------------------------------------------- the SMS reader (pure)
@pytest.fixture
def reader():
    frappe, saved = fake_frappe.install()
    try:
        yield importlib.import_module("mpsa_suite.money_out.petty_cash"), frappe
    finally:
        fake_frappe.uninstall(saved)


def test_a_buy_goods_sms_is_read(reader):
    pc, _ = reader
    read = pc.parse_sms(TILL_SMS)
    assert (read.receipt, read.amount, read.kind, read.payee, read.account, read.phone) == (
        "UKD1ABC2DE", 1500.0, "Till", "SHELL KILIMANI", None, None)
    assert (read.date, read.time, read.when, read.balance, read.charge) == (
        "2026-11-13", "10:15", datetime.datetime(2026, 11, 13, 10, 15), 8500.0, 0.0)


def test_a_paybill_sms_is_read_with_its_account(reader):
    pc, _ = reader
    read = pc.parse_sms(PAYBILL_SMS)
    assert (read.receipt, read.amount, read.kind, read.payee, read.account) == (
        "UKD1ABC2DF", 2000.0, "Paybill", "KPLC PREPAID", "12345")
    assert (read.time, read.balance, read.charge) == ("11:02", 6477.0, 23.0)


def test_a_send_money_sms_is_read_with_the_persons_phone(reader):
    pc, _ = reader
    read = pc.parse_sms(SEND_SMS)
    assert (read.kind, read.payee, read.phone, read.amount, read.time, read.balance, read.charge) == (
        "Phone", "JOHN DOE", "254712345678", 500.0, "12:30", 5970.0, 7.0)
    masked = pc.parse_sms(SEND_SMS.replace("0712345678", "0712***678"))
    assert (masked.kind, masked.payee, masked.phone) == ("Phone", "JOHN DOE", "0712***678")


@pytest.mark.parametrize("clock, expected", [("12:30 PM", "12:30"), ("1:05 PM", "13:05"), ("12:10 AM", "00:10"),
                                             ("9:07 am", "09:07"), ("11:59PM", "23:59")])
def test_the_time_of_day_is_read(reader, clock, expected):
    pc, _ = reader
    assert pc.parse_sms(TILL_SMS.replace("10:15 AM", clock)).time == expected


def test_an_sms_pasted_over_several_lines_or_without_a_balance_is_read(reader):
    pc, _ = reader
    read = pc.parse_sms("UKD1ABC2DE Confirmed.\nKsh1,500.00 paid to\nSHELL KILIMANI. on 13/11/2026\nat 10:15 AM.")
    assert (read.payee, read.date, read.balance, read.charge) == ("SHELL KILIMANI", "2026-11-13", None, None)
    assert pc.parse_sms("  " + TILL_SMS + "  ").receipt == "UKD1ABC2DE"


@pytest.mark.parametrize("text, message", [
    ("", "Paste the M-Pesa SMS"),
    ("Hello, please pay the till", "This is not an M-Pesa payment SMS"),
    ("UKD1ABC2DE Confirmed.You have received Ksh10,000.00 from ASTRAL INDUSTRIES LTD 600999 on 13/11/26 at 9:00 AM "
     "New M-PESA balance is Ksh18,500.00.", "says the phone received money"),
    ("Failed. You do not have enough money in your M-PESA account to send Ksh1,500.00.",
     "This is not an M-Pesa payment SMS"),
    (TILL_SMS.replace("13/11/26", "31/02/26"), "cannot be read"),
    (TILL_SMS.replace("Ksh1,500.00", "Ksh0.00"), "is not a payment"),
])
def test_text_that_is_no_payment_sms_is_refused(reader, text, message):
    pc, frappe = reader
    with pytest.raises(frappe.ValidationError, match=message):
        pc.parse_sms(text)


def test_typed_fields_must_agree_with_the_sms_and_fill_it(reader):
    pc, frappe = reader
    spend = pc.spend_values(TILL_SMS, receipt="ukd1abc2de", amount="1500", sms_date="2026-11-13", sms_time="10:15:00",
                            charge=5, payee="Shell Kilimani station")
    assert (spend.receipt, spend.amount, spend.charge, spend.payee, spend.text) == (
        "UKD1ABC2DE", 1500.0, 5.0, "Shell Kilimani station", " ".join(TILL_SMS.split()))
    for field, value, label in (("amount", 1600, "amount"), ("receipt", "UKD1ABC2DX", "receipt"),
                                ("sms_date", "2026-11-12", "date"), ("sms_time", "10:16", "time"),
                                ("balance", 8000, "new balance")):
        with pytest.raises(frappe.ValidationError, match=f"The {label} typed"):
            pc.spend_values(TILL_SMS, **{field: value})


def test_without_an_sms_the_typed_fields_are_needed(reader):
    pc, frappe = reader
    spend = pc.spend_values(receipt="ukd1abc2dz", amount=700, sms_date="2026-11-13", sms_time="08:05", balance=100)
    assert (spend.receipt, spend.amount, spend.when, spend.balance, spend.text) == (
        "UKD1ABC2DZ", 700.0, datetime.datetime(2026, 11, 13, 8, 5), 100.0, None)
    with pytest.raises(frappe.ValidationError, match="type its date, time"):
        pc.spend_values(receipt="UKD1ABC2DZ", amount=700)
    with pytest.raises(frappe.ValidationError, match="is not an M-Pesa receipt"):
        pc.spend_values(receipt="12345", amount=700, sms_date="2026-11-13", sms_time="08:05")


# ---------------------------------------------------------------- the site
@pytest.fixture
def env(payroll_env, monkeypatch):  # noqa: F811
    env = payroll_env
    frappe = env.frappe
    columns = b2b_fixture.COLUMNS
    monkeypatch.setitem(columns, ROW, columns[ROW] + ("reference_row", "payee_account_doctype", "payee_account",
                                                      "claim_payee"))
    monkeypatch.setitem(columns, DISB, columns[DISB] + ("party_type", "transaction_to_pay_against", "owner",
                                                        "paid_from"))
    monkeypatch.setitem(columns, REQ, columns[REQ] + ("phone_number", "receiver_public_name", "accounting_booked",
                                                      "accounting_error", "party_type", "party", "result_desc"))
    monkeypatch.setitem(columns, JE, columns[JE] + ("clearance_date",))
    monkeypatch.setitem(columns, PE, columns[PE] + ("clearance_date", "mpesa_petty_cash_phone", "mpesa_sms_time",
                                                    "mpesa_sms_balance", "mpesa_sms_payee", "mpesa_sms_text"))
    monkeypatch.setitem(columns, TOP_UP, ("name", "idx", "parent", "parenttype", "amount", "status", "payout",
                                          "request", "receipt", "paid_on", "journal_entry"))
    monkeypatch.setitem(columns, GLE, ("name", "account", "debit", "credit", "is_cancelled"))
    monkeypatch.setitem(columns, BTP, ("name", "parent", "payment_document", "payment_entry", "allocated_amount"))
    monkeypatch.setitem(columns, BT, ("name", "docstatus"))
    frappe.db.columns = frappe.db.columns | {(PE, "mpesa_petty_cash_phone")}
    # the payout's own accounting services (ERPNext's modules): never reached for a top-up
    for name in ("mpsa_suite.accounting.payment_entry_service", "mpsa_suite.accounting.journal_service"):
        stub = types.ModuleType(name)
        stub.create_payment_entry_for_doc = stub.create_journal_entry = lambda *a, **kw: None
        monkeypatch.setitem(sys.modules, name, stub)
    maker_checker = importlib.import_module("mpsa_suite.money_out.maker_checker")
    monkeypatch.setattr(maker_checker, "check", lambda doc: None)  # tested on its own
    env.partyless = importlib.import_module("mpsa_suite.money_out.partyless")
    env.pc = importlib.import_module("mpsa_suite.money_out.petty_cash")
    env.partyless.register(PHONE, env.pc.HANDLER)
    monkeypatch.setattr(env.pc, "now_datetime", lambda: LATER)
    env.send_confirm = importlib.import_module("mpsa_suite.money_out.send_confirm")
    env.postprocess = importlib.import_module("mpsa_suite.services.b2c_postprocess_service")
    env.response = importlib.import_module("mpsa_suite.services.b2c_response_service")
    env.statement = importlib.import_module("mpsa_suite.money_out.statement")
    seq = iter(range(1, 1000))

    def db_insert(self):
        if not self._row.get("name"):
            self._row["name"] = f"TU-{next(seq)}"
        self._frappe.db.table(self.doctype)[self._row["name"]] = self._row

    monkeypatch.setattr(fake_frappe.FakeDoc, "db_insert", db_insert, raising=False)
    plain_new_doc = frappe.new_doc

    def new_doc(doctype):
        doc = plain_new_doc(doctype)
        if doctype == DISB:  # the rows know their disbursement, as Frappe's do
            doc.meta = types.SimpleNamespace(get_label=lambda field: field.replace("_", " ").title())
            append = doc.append

            def appended(table, row):
                child = append(table, row)
                child.parent_doc = doc
                return child

            doc.append = appended
        return doc

    frappe.new_doc = new_doc
    db = frappe.db
    db.table("Employee")["EMP-3"].update(user_id=HOLDER, employee_name="Achieng Mary")
    for user, name in ((HOLDER, "Achieng Mary"), (ACCOUNTS, "Books Keeper"), (MANAGER, "Mary Manager"),
                       (PAYER, "Payer Person")):
        db.table("User")[user] = {"name": user, "full_name": name, "mobile_no": None, "phone": None}
    db.table("Account")[CASH] = {"name": CASH, "company": "Astral", "is_group": 0, "account_type": "Cash",
                                 "account_currency": "KES"}
    db.table("Account")[CHARGES] = {"name": CHARGES, "company": "Astral", "is_group": 0, "account_type": None,
                                    "account_currency": "KES"}
    db.table("Cost Center")["HQ - AIL"] = {"name": "HQ - AIL", "company": "Astral"}
    db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"].update(petty_cash_alert_days=7)
    phone(env)
    env.gl = lambda debit=0, credit=0, account=CASH: gl(env, account, debit, credit)
    yield env


def phone(env, name="PCP-00001", **kw):
    row = {"name": name, "docstatus": 1, "company": "Astral", "phone": NUMBER, "phone_label": "Isinya petty cash",
           "expected_name": "ACHIENG MARY", "enabled": 1, "holder": "EMP-3", "holder_name": "Achieng Mary",
           "holder_user": HOLDER, "petty_cash_account": CASH, "default_cost_center": "HQ - AIL",
           "charges_account": CHARGES, "top_up_limit": 10000, "float_ceiling": 20000, "owner": ACCOUNTS,
           "entered_by": ACCOUNTS, "approved_by": MANAGER, "mpesa_name": None, "name_mismatch": 0,
           "last_receiver_name": None, **kw}
    env.frappe.db.table(PHONE)[name] = row
    return row


def gl(env, account, debit, credit):
    table = env.frappe.db.table(GLE)
    name = f"GLE-{len(table) + 1}"
    table[name] = {"name": name, "account": account, "debit": debit, "credit": credit, "is_cancelled": 0}


def as_user(env, user, *roles):
    env.frappe.session.user, env.frappe.user_roles = user, set(roles)


def raises(env, match, exc=None):
    return pytest.raises(exc or env.frappe.ValidationError, match=match)


def top_up(env, amount=None, **kw):
    as_user(env, ACCOUNTS, "Accounts Manager")
    return env.pc.top_up("PCP-00001", amount, **kw)


def disbursement(env, name):
    """The draft disbursement as Frappe loads it, with its real controller."""
    stored = env.frappe.db.table(DISB)[name]
    doc = env.frappe.new_doc(DISB)
    rows = [r for r in env.frappe.db.table(ROW).values() if r["parent"] == name]
    doc.update({"name": name, "docstatus": stored["docstatus"], "company": stored["company"],
                "naming_series": "MPESA-B2C-.YYYY.-", "posting_date": "2026-11-13",
                "payment_type": "Mpesa Disbursement", "party_type": None, "transaction_to_pay_against": PHONE,
                "mode_of_payment": stored["mode_of_payment"], "paid_from": stored["paid_from"], "paid_to": None,
                "paid_from_account_currency": "KES", "paid_to_account_currency": "KES", "source_exchange_rate": 1,
                "target_exchange_rate": 1})
    for row in rows:
        keep = {k: v for k, v in row.items() if k not in ("idx", "parent_doc", "doctype", "b2c_disbursement_request",
                                                          "payment_status")}
        child = doc.append("references", keep)
        child.b2c_disbursement_request, child.payment_status = row.get("b2c_disbursement_request"), row.get(
            "payment_status")
        child.name, child.parent, child.parenttype = row["name"], name, DISB
    return doc


def send(env, name, user=PAYER, roles=("M-Pesa Payer",)):
    """Send via M-Pesa: the row's checks at the submit (validate), then before_submit."""
    as_user(env, user, *roles)
    doc = disbursement(env, name)
    doc.docstatus = 1
    doc.validate()
    with env.send_confirm.deliberate("test", doc):
        doc.submit()
    return doc


def result(env, name, status="Paid", receiver=f"{NUMBER} - ACHIENG MARY", receipt="UKT1TOPUP1",
           completed=datetime.datetime(2026, 11, 13, 9, 0, 0)):
    """The payout's Result, written as the B2C callback writes it, and its job run."""
    db = env.frappe.db
    (row,) = [r for r in db.table(ROW).values() if r["parent"] == name]
    request = f"B2C-{name}"
    row.update(b2c_disbursement_request=request, payment_status="Initiated")
    db.table(REQ)[request] = {
        "name": request, "docstatus": 1, "status": status, "amount": row["allocated_amount"],
        "transaction_id": receipt if status == "Paid" else None, "b2c_payment_disbursement": name,
        "b2c_payment_disbursement_reference": row["name"], "mpesa_settings": SETTINGS, "reference_doctype": PHONE,
        "reference_name": row["reference_name"], "party_type": None, "party": None, "phone_number": row["partyb"],
        "receiver_public_name": receiver if status == "Paid" else None, "transaction_completed_datetime": completed,
        "creation": completed, "accounting_booked": 0, "accounting_error": None,
        "result_desc": "ok" if status == "Paid" else "The initiator information is invalid."}
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    return request


def top_up_row(env, name=None):
    rows = list(env.frappe.db.table(TOP_UP).values())
    return next(r for r in rows if r["name"] == name) if name else rows[-1]


# ---------------------------------------------------------------- the phone
def draft(env, **kw):
    doc = fake_frappe.FakeDoc(env.frappe, PHONE, {
        "name": "PCP-00002", "docstatus": 0, "company": "Astral", "phone": "0711 000 777", "phone_label": None,
        "holder": "EMP-3", "petty_cash_account": "Petty Cash 2 - AIL", "default_cost_center": None,
        "charges_account": None, "top_up_limit": 5000, "float_ceiling": 15000, "owner": ACCOUNTS,
        "entered_by": ACCOUNTS, **kw})
    env.frappe.db.table("Account")["Petty Cash 2 - AIL"] = {"name": "Petty Cash 2 - AIL", "company": "Astral",
                                                            "is_group": 0, "account_type": "Cash",
                                                            "account_currency": "KES"}
    return doc


def test_a_phone_is_checked_and_its_number_and_holder_set(env):
    doc = draft(env)
    env.pc.validate_phone(doc)
    assert (doc.phone, doc.holder_name, doc.holder_user, doc.phone_label) == (
        "254711000777", "Achieng Mary", HOLDER, "Petty cash Achieng Mary")


@pytest.mark.parametrize("change, message", [
    ({"phone": "0712 34"}, "is not a Safaricom number"),
    ({"phone": "0733 000 111"}, "is not a Safaricom number"),  # Airtel: M-Pesa B2C cannot top it up
    ({"phone": "0722 000 999"}, "is petty cash phone PCP-00001 already"),
    ({"petty_cash_account": CASH}, "is the account of petty cash phone PCP-00001 already"),
    ({"petty_cash_account": CHARGES}, "is not a Cash account"),
    ({"holder": "EMP-1"}, "Give EMP-1 a User ID"),
    ({"holder": "EMP-4"}, "EMP-4 is Left"),
    ({"top_up_limit": 5}, "at least KES 10"),
    ({"top_up_limit": 300000, "float_ceiling": 400000}, "at most KES 250,000"),
    ({"float_ceiling": 4000}, "must be at least the Top-Up Limit"),
    ({"charges_account": "Other Co Charges"}, "is not a Account of Astral"),
])
def test_a_phone_is_refused(env, change, message):
    env.frappe.db.table("Account")["Other Co Charges"] = {"name": "Other Co Charges", "company": "Other"}
    doc = draft(env, **change)
    with raises(env, message):
        env.pc.validate_phone(doc)


def test_the_approval_is_a_named_manager_who_did_not_enter_it_and_does_not_hold_it(env):
    doc = draft(env)
    as_user(env, MANAGER, "M-Pesa Manager")
    with raises(env, "approved with the Approve button", env.frappe.PermissionError):
        env.pc.check_approval(doc)  # a plain Submit
    doc.flags = _dict({env.pc.APPROVE_FLAG: True})
    for user, roles, message in ((ACCOUNTS, ("Accounts Manager",), "you entered or changed"),
                                 (HOLDER, ("Accounts Manager",), "You hold this phone"),
                                 ("clerk@example.com", ("Accounts User",), "needs one of the roles")):
        as_user(env, user, *roles)
        with raises(env, message, env.frappe.PermissionError):
            env.pc.check_approval(doc)
    as_user(env, MANAGER, "Accounts Manager")
    env.pc.check_approval(doc)
    assert (doc.approved_by, doc.approved_on, doc.phone) == (MANAGER, LATER, "254711000777")


def test_approve_submits_through_the_one_flag(env, monkeypatch):
    submitted = []

    class Phone(fake_frappe.FakeDoc):
        def check_permission(self, ptype="read"):
            submitted.append(("perm", ptype))

        def submit(self):
            submitted.append(("submit", dict(self.flags)))

    doc = Phone(env.frappe, PHONE, {"name": "PCP-00002", "docstatus": 0})
    object.__setattr__(doc, "flags", _dict())
    monkeypatch.setattr(env.frappe, "get_doc", lambda doctype, name=None, **kw: doc)
    as_user(env, "clerk@example.com", "Accounts User")
    with raises(env, "needs one of the roles", env.frappe.PermissionError):
        env.pc.approve("PCP-00002")
    as_user(env, MANAGER, "M-Pesa Manager")
    env.pc.approve("PCP-00002")
    assert submitted == [("perm", "submit"), ("submit", {env.pc.APPROVE_FLAG: True})]


class Saved(fake_frappe.FakeDoc):
    """The approved phone as its form saves it (Update): a copy, maybe older than the database."""

    def set(self, field, value):
        self._row[field] = value

    def append(self, table, row):
        self._row.setdefault(table, []).append(row)


def test_the_holder_and_limits_change_but_not_by_the_holder(env):
    before = _dict(env.frappe.db.table(PHONE)["PCP-00001"])
    doc = Saved(env.frappe, PHONE, dict(before, float_ceiling=30000))
    object.__setattr__(doc, "get_doc_before_save", lambda: before)
    as_user(env, MANAGER, "M-Pesa Manager")
    env.pc.check_update(doc)
    assert env.frappe.comments[-1][2] == f"{MANAGER} changed float_ceiling: 20000 to 30000."
    as_user(env, HOLDER, "Accounts Manager")
    with raises(env, "You hold this phone", env.frappe.PermissionError):
        env.pc.check_update(doc)


def test_a_stale_form_neither_drops_a_top_up_nor_lifts_a_name_block(env):
    out = top_up(env, 2000)
    env.frappe.db.table(PHONE)["PCP-00001"].update(name_mismatch=1, last_receiver_name=f"{NUMBER} - JOHN KAMAU")
    stale = _dict(env.frappe.db.table(PHONE)["PCP-00001"], name_mismatch=0, last_receiver_name=None, top_ups=[])
    doc = Saved(env.frappe, PHONE, dict(stale))
    object.__setattr__(doc, "get_doc_before_save", lambda: stale)
    as_user(env, MANAGER, "M-Pesa Manager")
    env.pc.check_update(doc)
    assert (doc.name_mismatch, doc.last_receiver_name) == (1, f"{NUMBER} - JOHN KAMAU")
    assert [(r["name"], r["status"], r["payout"]) for r in doc.top_ups] == [(out["top_up"], "Draft", out["name"])]


def test_a_phone_with_a_top_up_on_its_way_is_not_cancelled_and_its_history_never_holds_the_cancel(env):
    out = top_up(env, 5000)
    doc = _dict(name="PCP-00001", ignore_linked_doctypes=("Version",))
    with raises(env, "cannot be cancelled while top-up #1 is Draft"):
        env.pc.before_cancel(doc)
    del env.frappe.db.table(DISB)[out["name"]]  # the draft deleted: the top-up is Cancelled
    env.pc.before_cancel(doc)
    assert top_up_row(env)["status"] == "Cancelled"
    assert doc.ignore_linked_doctypes == ("Version", DISB, ROW, REQ, PE, JE)


# ---------------------------------------------------------------- top-ups
def test_a_top_up_defaults_to_the_ceiling_less_the_book_balance_and_is_a_party_less_draft(env):
    env.gl(debit=12000)
    out = top_up(env)
    assert (out["amount"], out["doctype"]) == (8000, DISB)
    stored = env.frappe.db.table(DISB)[out["name"]]
    (row,) = [r for r in env.frappe.db.table(ROW).values() if r["parent"] == out["name"]]
    assert (stored["docstatus"], stored["paid_from"], stored["paid_to"], stored["mode_of_payment"]) == (
        0, GL, None, f"Mpesa-{SETTINGS}")
    assert (row["reference_doctype"], row["reference_name"], row["reference_row"], row["partyb"], row["party"],
            row["allocated_amount"]) == (PHONE, "PCP-00001", out["top_up"], NUMBER, None, 8000)
    assert {k: top_up_row(env)[k] for k in ("idx", "amount", "status", "payout", "requested_by")} == {
        "idx": 1, "amount": 8000, "status": "Draft", "payout": out["name"], "requested_by": ACCOUNTS}
    assert "b2c-pay:M-Pesa Petty Cash Phone:PCP-00001" in env.locks


@pytest.mark.parametrize("book, amount, message", [
    (0, 12000, "more than PCP-00001's top-up limit, KES 10,000"),
    (15000, 6000, r"would hold KES 21,000, more than its float ceiling of KES 20,000 \(book balance KES 15,000\): "
                  "top it up by at most KES 5,000"),
    (0, 1500.5, "whole shillings only"),
    (0, 5, "less than M-Pesa B2C pays"),
    (20000, None, "nothing to top up"),
])
def test_a_top_up_is_refused_above_the_limit_and_the_ceiling(env, book, amount, message):
    env.gl(debit=book)
    with raises(env, message):
        top_up(env, amount)


def test_one_top_up_at_a_time_and_the_ceiling_counts_what_is_on_its_way(env):
    first = top_up(env, 9000)
    with raises(env, r"Top-up #1 of PCP-00001 is Draft \(MPESA-B2C-1\): send it, or delete its draft"):
        top_up(env, 1000)
    send(env, first["name"])
    assert env.pc.refresh_top_ups("PCP-00001")[0].status == "Sending"
    with raises(env, "is Sending"):
        top_up(env, 1000)
    # sent, not answered yet: on its way, so the ceiling counts it
    assert env.pc.in_flight("PCP-00001") == 9000
    # Paid and not booked yet (a closed period, say): still counted, until its journal is in
    env.frappe.db.table(TOP_UP)[first["top_up"]].update(status="Paid", journal_entry=None)
    assert env.pc.in_flight("PCP-00001") == 9000
    env.je("ACC-JV-T1", "UKT1TOPUP1", [{"account": CASH, "debit_in_account_currency": 9000}])
    env.frappe.db.table(TOP_UP)[first["top_up"]]["journal_entry"] = "ACC-JV-T1"
    assert env.pc.in_flight("PCP-00001") == 0
    env.frappe.db.table(TOP_UP)[first["top_up"]].update(status="Sending", journal_entry=None)
    assert env.pc.amount_problem(_dict(env.frappe.db.table(PHONE)["PCP-00001"]), 10000, 5000, 9000).startswith(
        "PCP-00001 would hold KES 24,000")


@pytest.mark.parametrize("change, message", [
    ({"enabled": 0}, "PCP-00001 is disabled"),
    ({"docstatus": 0}, "PCP-00001 is not approved"),
    ({"name_mismatch": 1, "last_receiver_name": "254722000999 - JOHN OTHER"}, "is blocked: M-Pesa paid its number"),
])
def test_a_phone_that_may_not_be_used_is_not_topped_up(env, change, message):
    env.frappe.db.table(PHONE)["PCP-00001"].update(change)
    with raises(env, message):
        top_up(env, 1000)


def test_the_holder_neither_prepares_nor_sends_a_top_up_of_their_phone(env):
    as_user(env, HOLDER, "Accounts Manager")
    with raises(env, "You hold PCP-00001: another person must prepare its top-up", env.frappe.PermissionError):
        env.pc.top_up("PCP-00001", 1000)
    out = top_up(env, 1000)
    with raises(env, "You hold PCP-00001: another person must send its top-up", env.frappe.PermissionError):
        send(env, out["name"], user=HOLDER, roles=("M-Pesa Payer",))
    # nor one whose phone or name is the sender's own (money_out.self_pay)
    env.frappe.db.table("User")[PAYER].update(mobile_no="0722 000 999")
    with raises(env, "You may not send a payment to yourself", env.frappe.PermissionError):
        send(env, out["name"])


def test_the_preparer_needs_a_preparers_role_and_create_on_the_disbursement(env):
    as_user(env, PAYER, "M-Pesa Payer")
    with raises(env, "needs one of the roles", env.frappe.PermissionError):
        env.pc.top_up("PCP-00001", 1000)
    env.denied.add((DISB, "create"))
    with raises(env, "no create", env.frappe.PermissionError):
        top_up(env, 1000)


def test_the_row_must_be_the_top_ups_own(env):
    out = top_up(env, 4000)
    doc = disbursement(env, out["name"])
    row = doc.references[0]
    for change, message in (({"partyb": "0711000111"}, "not the number of PCP-00001"),
                            ({"allocated_amount": 5000}, "not top-up #1's KES 4,000"),
                            ({"reference_row": "NOPE"}, "names no top-up of PCP-00001")):
        saved = {k: getattr(row, k) for k in change}
        row.__dict__.update(change)
        with raises(env, message):
            env.pc.HANDLER.check_row(row, doc)
        row.__dict__.update(saved)
    other = disbursement(env, out["name"])
    other.name = "MPESA-B2C-OTHER"
    with raises(env, r"top-up #1 of PCP-00001 is paid by MPESA-B2C-1"):
        env.pc.HANDLER.check_row(other.references[0], other)


def test_the_send_summary_names_the_phone_and_its_holder(env):
    out = top_up(env, 3000)
    summary = env.send_confirm._summary(disbursement(env, out["name"]))
    (row,) = summary["rows"]
    assert (row["receiver"], row["party"], row["document"], row["amount"]) == (
        "Petty cash phone Isinya petty cash", "held by Achieng Mary", f"{PHONE} PCP-00001, top-up #1", 3000)


def test_a_paid_top_up_is_booked_by_one_journal_that_the_statement_finds(env):
    out = top_up(env, 6000)
    send(env, out["name"])
    request = result(env, out["name"])
    row = top_up_row(env)
    assert (row["status"], row["receipt"], row["receiver_name"], row["request"]) == (
        "Paid", "UKT1TOPUP1", f"{NUMBER} - ACHIENG MARY", request)
    journal = env.frappe.db.table(JE)[row["journal_entry"]]
    assert (journal["docstatus"], journal["voucher_type"], journal["cheque_no"], str(journal["posting_date"])) == (
        1, "Bank Entry", "UKT1TOPUP1", "2026-11-13")
    lines = sorted((r["account"], r["debit_in_account_currency"], r["credit_in_account_currency"], r["cost_center"])
                   for r in env.frappe.db.table(JEA).values() if r["parent"] == row["journal_entry"])
    assert lines == [(GL, 0, 6000, "HQ - AIL"), (CASH, 6000, 0, "HQ - AIL")]
    stored = env.frappe.db.table(REQ)[request]
    assert (stored["accounting_booked"], stored["accounting_error"]) == (1, None)
    # the statement line of the payout reconciles one to one, by the Journal Entry's Cheque No
    assert env.statement.entries_for("UKT1TOPUP1", GL) == [(JE, row["journal_entry"], 6000)]
    # the sweep again: booked once
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    assert len([j for j in env.frappe.db.table(JE).values() if j["cheque_no"] == "UKT1TOPUP1"]) == 1
    # the phone's M-Pesa name, checked on its first top-up
    stored_phone = env.frappe.db.table(PHONE)["PCP-00001"]
    assert (stored_phone["mpesa_name"], stored_phone["name_mismatch"]) == ("ACHIENG MARY", 0)


def test_a_journal_made_by_hand_for_the_receipt_is_the_top_ups(env):
    out = top_up(env, 2000)
    send(env, out["name"])
    env.je("ACC-JV-HAND", "UKT1TOPUP1", [{"account": CASH, "debit_in_account_currency": 2000},
                                         {"account": GL, "credit_in_account_currency": 2000}], clearance_date=None)
    result(env, out["name"])
    assert top_up_row(env)["journal_entry"] == "ACC-JV-HAND"
    assert len([j for j in env.frappe.db.table(JE).values() if j["cheque_no"] == "UKT1TOPUP1"]) == 1
    ref = _dict(reference_doctype=PHONE, reference_name="PCP-00001", reference_row="NOPE", reference_no="RCPT")
    assert env.pc.HANDLER.closed_period_hint(ref, "RCPT").startswith(
        "Its date is in a closed Accounting Period")
    assert f"make a Journal Entry with Cheque No RCPT, debit {CASH}" in env.pc.HANDLER.closed_period_hint(ref, "RCPT")


def test_a_failed_top_up_is_told_and_retried(env):
    out = top_up(env, 3000)
    send(env, out["name"])
    request = result(env, out["name"], status="Failed")
    row = top_up_row(env)
    assert (row["status"], row["note"], row.get("journal_entry")) == (
        "Failed", "The initiator information is invalid.", None)
    # Retry on the request: the row checked again (its ceiling and holder), not the phone's other top-ups
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")
    doc = object.__new__(module.B2CDisbursementRequest)
    doc.__dict__.update(env.frappe.db.table(REQ)[request], doctype=REQ, flags=_dict())
    as_user(env, PAYER, "M-Pesa Payer")
    doc.validate_still_owed()
    as_user(env, HOLDER, "M-Pesa Payer")  # the holder: their own name on the phone (self_pay), or the holder rule
    with raises(env, "You may not resend a payment to yourself: B2C-MPESA-B2C-1 pays 'ACHIENG MARY', your name",
                env.frappe.PermissionError):
        doc.validate_still_owed()
    env.frappe.db.table(PHONE)["PCP-00001"].update(mpesa_name="ACHIENG M. W.", holder_name="A. Mary")
    with raises(env, "You hold PCP-00001: another person must send its top-up", env.frappe.PermissionError):
        doc.validate_still_owed()
    env.frappe.db.table(PHONE)["PCP-00001"].update(mpesa_name="ACHIENG MARY", holder_name="Achieng Mary")
    # paid on the retry: the row follows and it is booked
    env.frappe.db.table(REQ)[request].update(status="Paid", transaction_id="UKT1RETRY1",
                                             receiver_public_name=f"{NUMBER} - ACHIENG MARY")
    env.response.update_b2c_reference_status(request, enqueue_next=True)
    row = top_up_row(env)
    assert (row["status"], row["receipt"], row["note"]) == ("Paid", "UKT1RETRY1", None)
    assert env.frappe.db.table(JE)[row["journal_entry"]]["cheque_no"] == "UKT1RETRY1"


def test_a_name_that_does_not_match_on_the_first_top_up_holds_the_phone_until_confirmed(env):
    out = top_up(env, 3000)
    send(env, out["name"])
    result(env, out["name"], receiver=f"{NUMBER} - JOHN KAMAU")
    stored = env.frappe.db.table(PHONE)["PCP-00001"]
    assert (stored["name_mismatch"], stored["mpesa_name"], stored["last_receiver_name"]) == (
        1, None, f"{NUMBER} - JOHN KAMAU")
    assert "M-Pesa paid it as '254722000999 - JOHN KAMAU', not 'ACHIENG MARY'" in env.alerts[-1][1]
    with raises(env, "is blocked"):
        top_up(env, 1000)
    as_user(env, HOLDER, "M-Pesa Manager")
    with raises(env, "another manager confirms its name", env.frappe.PermissionError):
        env.pc.confirm_name("PCP-00001", 1)
    as_user(env, MANAGER, "M-Pesa Manager")
    assert env.pc.confirm_name("PCP-00001", 1)["mpesa_name"] == "JOHN KAMAU"
    assert (stored["name_mismatch"], stored["mpesa_name"]) == (0, "JOHN KAMAU")


def test_another_number_in_front_of_the_name_is_a_mismatch(env):
    env.pc.check_name("PCP-00001", _dict(name="B2C-X", receiver_public_name="254711999888 - ACHIENG MARY"))
    assert env.frappe.db.table(PHONE)["PCP-00001"]["name_mismatch"] == 1


# ---------------------------------------------------------------- spends
@pytest.fixture
def spends(env):
    env.pi("PINV-1", outstanding_amount=10000)
    env.po("PO-1", rounded_total=4000, grand_total=4000)
    env.frappe.db.table("Supplier")["ACME"].update(supplier_name="Shell Kilimani Ltd")
    env.gl(debit=10000)
    as_user(env, HOLDER, "Purchase User")
    return env


def test_a_spend_from_the_sms_books_a_payment_entry_from_the_petty_cash_account(spends):
    out = spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=PAYBILL_SMS)
    assert (out["amount"], out["charge"], out["receipt"], out["book_balance"], out["sms_balance"]) == (
        2000, 23, "UKD1ABC2DF", 7977, 6477)
    pe = spends.frappe.db.table(PE)[out["payment_entry"]]
    assert (pe["docstatus"], pe["payment_type"], pe["party_type"], pe["party"], pe["paid_from"], pe["paid_to"]) == (
        1, "Pay", "Supplier", "ACME", CASH, "Creditors - AIL")
    assert (pe["paid_amount"], pe["received_amount"], pe["reference_no"], pe["reference_date"], pe["posting_date"]) == (
        2023, 2000, "UKD1ABC2DF", "2026-11-13", "2026-11-13")
    assert pe["deductions"] == [{"account": CHARGES, "cost_center": "HQ - AIL", "amount": 23,
                                 "description": "M-Pesa transaction cost, UKD1ABC2DF"}]
    assert (pe["mpesa_petty_cash_phone"], pe["mpesa_sms_time"], pe["mpesa_sms_balance"], pe["mpesa_sms_payee"]) == (
        "PCP-00001", datetime.datetime(2026, 11, 13, 11, 2), 6477, "KPLC PREPAID")
    assert pe["remarks"].startswith("Paid from petty cash phone PCP-00001 Isinya petty cash")
    refs = [(r["reference_doctype"], r["reference_name"], r["allocated_amount"])
            for r in spends.frappe.db.table(PER).values() if r["parent"] == out["payment_entry"]]
    assert refs == [("Purchase Invoice", "PINV-1", 2000)]
    assert ("lock", "mpesa-receipt:UKD1ABC2DF") in spends.log


def test_a_spend_on_an_order_is_its_advance(spends):
    out = spends.pc.record_spend("Purchase Order", "PO-1", sms=TILL_SMS)
    pe = spends.frappe.db.table(PE)[out["payment_entry"]]
    assert (pe["paid_to"], pe["paid_amount"], pe["received_amount"], pe.get("deductions")) == (
        "Creditors - AIL", 1500, 1500, None)
    assert [(r["reference_doctype"], r["reference_name"], r["allocated_amount"])
            for r in spends.frappe.db.table(PER).values() if r["parent"] == out["payment_entry"]] == [
        ("Purchase Order", "PO-1", 1500)]
    state = spends.pc.document_state("Purchase Order", "PO-1")
    assert [(s["payment_entry"], s["receipt"], s["phone"], s["amount"]) for s in state["spends"]] == [
        (out["payment_entry"], "UKD1ABC2DE", "PCP-00001", 1500)]


def test_a_receipt_is_recorded_once(spends):
    spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS)
    with raises(spends, "M-Pesa receipt UKD1ABC2DE is on Payment Entry ACC-PAY-0001 already"):
        spends.pc.record_spend("Purchase Order", "PO-1", sms=TILL_SMS)
    spends.je("ACC-JV-9", "UKD1ABC2DF", [{"account": CASH, "credit_in_account_currency": 1}])
    with raises(spends, "is on Journal Entry ACC-JV-9 already"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=PAYBILL_SMS)


def test_a_spend_above_what_is_owed_or_the_book_balance_is_refused(spends):
    with raises(spends, "KES 4,500 is more than PO-1 still owes. PO-1 owes 4,000: 4,000 is left to pay"):
        spends.pc.record_spend("Purchase Order", "PO-1", sms=TILL_SMS.replace("1,500.00 paid", "4,500.00 paid"))
    spends.frappe.db.table(GLE).clear()
    spends.gl(debit=2000)
    with raises(spends, r"book balance is KES 2,000, less than KES 2,023: record its missing top-ups first"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=PAYBILL_SMS)
    assert not spends.frappe.db.table(PE)


def test_only_the_holder_or_accounts_roles_record(spends):
    as_user(spends, "buyer@example.com", "Purchase User")
    with raises(spends, "needs one of the roles", spends.frappe.PermissionError):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", phone="PCP-00001", sms=TILL_SMS)
    as_user(spends, ACCOUNTS, "Accounts User")
    assert spends.pc.record_spend("Purchase Invoice", "PINV-1", phone="PCP-00001", sms=TILL_SMS)["payment_entry"]
    spends.denied.add(("Purchase Invoice", "read"))
    with raises(spends, "no read", spends.frappe.PermissionError):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=PAYBILL_SMS)


def test_the_phone_is_the_holders_or_the_companys_only_one(spends):
    phone(spends, "PCP-00002", phone="254711000888", petty_cash_account="Other Cash", holder="EMP-2",
          holder_user="otieno@example.com")
    as_user(spends, ACCOUNTS, "Accounts User")
    with raises(spends, "Pick the petty cash phone that paid: Astral has 2 approved phones"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS)
    as_user(spends, HOLDER, "Purchase User")  # holds one of them
    out = spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS)
    assert spends.frappe.db.table(PE)[out["payment_entry"]]["mpesa_petty_cash_phone"] == "PCP-00001"
    with raises(spends, "PCP-00009 is not a petty cash phone of Astral"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", phone="PCP-00009", sms=PAYBILL_SMS)


def test_a_charge_needs_the_charges_account_and_a_future_sms_is_refused(spends):
    spends.frappe.db.table(PHONE)["PCP-00001"]["charges_account"] = None
    with raises(spends, "Set the Charges Account on PCP-00001"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=PAYBILL_SMS)
    with raises(spends, "are in the future"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS.replace("13/11/26", "14/11/26"))


def test_the_name_on_the_sms_is_kept_and_shown_next_to_the_suppliers(spends, monkeypatch):
    known = importlib.import_module("mpsa_suite.money_out.known_names")
    seen = []
    monkeypatch.setattr(known, "observe", lambda *a, **kw: seen.append((a, kw)))
    out = spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=SEND_SMS)
    assert seen == [(("Phone", "254712345678", "254712345678 - JOHN DOE"),
                     {"when": datetime.datetime(2026, 11, 13, 12, 30), "payout_doctype": PE,
                      "payout": out["payment_entry"], "source": "SMS"})]
    # a till's name against the supplier's one approved till
    spends.frappe.db.table("Supplier M-Pesa Account")["MSA-T"] = {
        "name": "MSA-T", "docstatus": 1, "supplier": "ACME", "account_type": "Till (Buy Goods)", "shortcode": "835811",
        "account_number": None, "is_default": 0}
    spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS)
    assert seen[-1][0] == ("Till (Buy Goods)", "835811", "SHELL KILIMANI")
    read = spends.pc.read_sms("Purchase Invoice", "PINV-1", TILL_SMS.replace("UKD1ABC2DE", "UKD1ABC2DQ"))
    assert (read["payee"], read["supplier_name"], read["name_matches"], read["used"]) == (
        "SHELL KILIMANI", "Shell Kilimani Ltd", True, None)
    till = next(name for name, pe in spends.frappe.db.table(PE).items() if pe["reference_no"] == "UKD1ABC2DE")
    assert spends.pc.read_sms("Purchase Invoice", "PINV-1", TILL_SMS)["used"] == f"Payment Entry {till}"


def test_the_screenshot_is_a_file_of_the_site(spends):
    with raises(spends, "Attach the screenshot as a file of this site"):
        spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS, screenshot="https://evil.example.com/x.png")
    out = spends.pc.record_spend("Purchase Invoice", "PINV-1", sms=TILL_SMS, screenshot="/private/files/sms.png")
    (attached,) = spends.frappe.db.table("File").values()
    assert (attached["file_url"], attached["attached_to_doctype"], attached["attached_to_name"]) == (
        "/private/files/sms.png", PE, out["payment_entry"])


def test_what_the_order_or_invoice_shows(spends):
    state = spends.pc.document_state("Purchase Invoice", "PINV-1")
    assert (state["can_record"], state["default_phone"], state["problem"]) == (True, "PCP-00001", None)
    assert [(p["name"], p["book_balance"]) for p in state["phones"]] == [("PCP-00001", 10000)]
    as_user(spends, "buyer@example.com", "Purchase User")  # neither holder nor Accounts: no phone to record with
    assert spends.pc.document_state("Purchase Invoice", "PINV-1")["can_record"] is False
    spends.pi("PINV-2", outstanding_amount=0)
    as_user(spends, HOLDER, "Purchase User")
    state = spends.pc.document_state("Purchase Invoice", "PINV-2")
    assert (state["can_record"], state["problem"]) == (False, "PINV-2 has nothing left to pay.")


# ---------------------------------------------------------------- the book against the last SMS, the daily section
def spend_entry(env, name, amount, when, balance, docstatus=1):
    env.frappe.db.table(PE)[name] = {"name": name, "docstatus": docstatus, "paid_amount": amount, "posting_date":
                                     when.date(), "mpesa_petty_cash_phone": "PCP-00001", "mpesa_sms_time": when,
                                     "mpesa_sms_balance": balance, "creation": when, "reference_no": name}


def paid_top_up(env, name, amount, when):
    env.frappe.db.table(TOP_UP)[name] = {"name": name, "idx": 1, "parent": "PCP-00001", "parenttype": PHONE,
                                         "amount": amount, "status": "Paid", "paid_on": when}


def test_the_book_balance_is_compared_with_the_last_sms(env):
    paid_top_up(env, "TU-A", 10000, datetime.datetime(2026, 11, 10, 9, 0))
    env.gl(debit=10000)
    spend_entry(env, "PE-1", 1500, datetime.datetime(2026, 11, 11, 10, 15), 8500)
    env.gl(credit=1500)
    status = env.pc.sms_status(_dict(env.frappe.db.table(PHONE)["PCP-00001"]))
    assert (status.book, status.last_sms.balance, status.expected, status.difference) == (8500, 8500, 8500, 0)
    # a top-up paid after the SMS and a spend recorded without a balance after it: still in step
    paid_top_up(env, "TU-B", 5000, datetime.datetime(2026, 11, 12, 9, 0))
    env.gl(debit=5000)
    spend_entry(env, "PE-2", 700, datetime.datetime(2026, 11, 12, 15, 0), None)
    env.gl(credit=700)
    status = env.pc.sms_status(_dict(env.frappe.db.table(PHONE)["PCP-00001"]))
    assert (status.book, status.expected, status.difference) == (12800, 12800, 0)
    assert (status.last_top_up, status.last_spend) == (datetime.datetime(2026, 11, 12, 9, 0),
                                                       datetime.datetime(2026, 11, 12, 15, 0))
    # an SMS whose balance shows less than the books: a spend not recorded, or a typo
    spend_entry(env, "PE-3", 800, datetime.datetime(2026, 11, 13, 8, 0), 11000)
    env.gl(credit=800)
    status = env.pc.sms_status(_dict(env.frappe.db.table(PHONE)["PCP-00001"]))
    assert (status.book, status.expected, status.difference) == (12000, 11000, 1000)
    title, lines, link = env.pc.attention_section()
    assert (title, link) == ("Petty cash phones", "/app/m-pesa-petty-cash-phone")
    assert lines == ["PCP-00001 Isinya petty cash (254722000999), Achieng Mary: the books hold KES 1,000 more than its "
                     "last SMS says (KES 11,000 on 13 Nov 08:00): a spend not recorded, or a typo (book KES 12,000, "
                     "last top-up 1 days ago, last spend 0 days ago)"]


def test_money_left_unspent_for_the_set_days_is_flagged(env):
    paid_top_up(env, "TU-A", 6000, datetime.datetime(2026, 11, 1, 9, 0))
    env.gl(debit=6000)
    (line,) = env.pc.attention_section()[1]
    assert "KES 6,000 with no spend since its top-up for 12 days" in line and "no spend recorded" in line
    env.frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["petty_cash_alert_days"] = 14
    assert env.pc.attention_section()[1] == []
    env.frappe.db.table(PHONE)["PCP-00001"]["name_mismatch"] = 1
    assert "its M-Pesa name did not match: top-ups held" in env.pc.attention_section()[1][0]
    env.frappe.db.table(PHONE)["PCP-00001"]["enabled"] = 0  # a disabled phone is not watched
    assert env.pc.attention_section()[1] == []


def test_the_phones_form_says_what_this_user_may_do(env):
    env.gl(debit=5000)
    as_user(env, ACCOUNTS, "Accounts Manager")
    state = env.pc.phone_state("PCP-00001")
    assert (state["book_balance"], state["suggested_amount"], state["can_top_up"], state["can_approve"],
            state["can_confirm_name"], state["can_record"]) == (5000, 10000, True, False, False, True)
    as_user(env, HOLDER, "Accounts Manager")
    state = env.pc.phone_state("PCP-00001")
    assert (state["can_top_up"], state["can_record"]) == (False, True)
    env.frappe.db.table(PHONE)["PCP-00001"].update(docstatus=0)
    as_user(env, MANAGER, "M-Pesa Manager")
    assert env.pc.phone_state("PCP-00001")["can_approve"] is True
    as_user(env, ACCOUNTS, "M-Pesa Manager")  # entered it
    assert env.pc.phone_state("PCP-00001")["can_approve"] is False


# ---------------------------------------------------------------- the daily alert links each section to its list
def test_the_alert_links_each_section_to_its_own_list(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        attention = importlib.import_module("mpsa_suite.money_out.attention")
        frappe.utils.get_url = lambda uri=None, *a, **kw: f"https://erp.example.com{uri or ''}"
        attention.register(lambda: ("Petty cash phones", ["PCP-1: a spend not recorded"],
                                    "/app/m-pesa-petty-cash-phone"))
        attention.register(lambda: ("Plain", ["x"]))
        collected = attention.collect()
        assert collected == [("Petty cash phones", ["PCP-1: a spend not recorded"], "/app/m-pesa-petty-cash-phone"),
                             ("Plain", ["x"])]
        assert attention.subject(collected) == "M-Pesa needs attention: Petty cash phones (1); Plain (1)"
        assert attention.details(collected) == (
            '<p><b><a href="https://erp.example.com/app/m-pesa-petty-cash-phone">Petty cash phones</a></b></p>'
            "<ul><li>PCP-1: a spend not recorded</li></ul><p><b>Plain</b></p><ul><li>x</li></ul>")
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- the DocTypes, fields and hooks
def _doctype(folder):
    return json.loads((APP / "mpsa_suite" / "doctype" / folder / f"{folder}.json").read_text(encoding="utf-8"))


def test_the_phone_doctype_locks_what_its_approval_approved():
    data = _doctype("m_pesa_petty_cash_phone")
    fields = {f["fieldname"]: f for f in data["fields"]}
    assert (data["name"], data["is_submittable"], data["track_changes"], data["module"]) == (PHONE, 1, 1, "Mpsa Suite")
    assert sorted(fields) == sorted(data["field_order"])
    for name in ("phone", "petty_cash_account", "expected_name", "company"):
        assert not fields[name].get("allow_on_submit"), name
    for name in ("holder", "holder_name", "holder_user", "top_up_limit", "float_ceiling", "enabled", "top_ups",
                 "mpesa_name", "name_mismatch"):
        assert fields[name].get("allow_on_submit"), name
    assert fields["top_ups"]["options"] == TOP_UP and fields["top_ups"]["no_copy"] == 1
    child = _doctype("m_pesa_petty_cash_top_up")
    assert (child["istable"], child["permissions"]) == (1, [])
    child_fields = {f["fieldname"]: f for f in child["fields"]}
    # the payout and its entry are named, not linked: cancelling them is never held by the phone
    assert {child_fields[f]["fieldtype"] for f in ("payout", "request", "journal_entry")} == {"Data"}
    assert child_fields["status"]["options"].split("\n") == ["Draft", "Sending", "Paid", "Failed", "Cancelled"]
    py = (APP / "mpsa_suite" / "doctype" / "m_pesa_petty_cash_phone" / "m_pesa_petty_cash_phone.py").read_text(
        encoding="utf-8")
    assert "petty_cash.check_approval(self)" in py and "petty_cash.before_cancel(self)" in py


def test_the_payment_entry_and_purchase_fields(monkeypatch):
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    frappe, saved = fake_frappe.install()
    try:
        for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
        migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
        made = []
        monkeypatch.setattr(migrate, "create_fields", lambda fields: made.append(fields))
        frappe.get_installed_apps = lambda: ["frappe", "erpnext", "hrms"]
        migrate.create_till_payment_fields()
        (fields,) = made
        pe = {f["fieldname"]: f for f in fields["Payment Entry"]}
        assert set(pe) == {"mpesa_petty_cash_phone", "mpesa_sms_time", "mpesa_sms_balance", "mpesa_sms_payee",
                           "mpesa_sms_text"}
        assert pe["mpesa_petty_cash_phone"]["options"] == PHONE
        assert all(f["read_only"] and f["no_copy"] for f in pe.values())
        for doctype in ("Purchase Order", "Purchase Invoice"):
            assert [f["fieldname"] for f in fields[doctype]] == ["mpesa_tills_html"]
        source = (APP / "mpsa_suite" / "migrate.py").read_text(encoding="utf-8")
        assert "create_till_payment_fields()" in source.split("def post_entries_at_once")[0]  # after_migrate runs it
    finally:
        fake_frappe.uninstall(saved)


def test_the_purchase_forms_load_the_tills_script():
    import ast

    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    hooks = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "doctype_js"}
    for doctype, own in (("Purchase Order", "public/js/purchase_order.js"),
                         ("Purchase Invoice", "public/js/purchase_invoice.js")):
        assert hooks["doctype_js"][doctype] == [own, "public/js/purchase_mpesa_tills.js"]
    assert (APP / "public" / "js" / "purchase_mpesa_tills.js").is_file()
