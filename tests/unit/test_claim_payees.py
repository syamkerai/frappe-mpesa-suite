"""Merchants and casual payees paid straight from an Expense Claim (money_out.claim_payees), on the
in-memory site of b2b_fixture: the SQL the module sends runs on SQLite.

Covers: the destination key (the payee accounts' format); each line's details by type (phone, till,
paybill, bank account at an enabled bank paybill, a bank paybill typed as a plain paybill, the
company's own numbers, whole shillings); the claim's checks (the Suite switch, the Expense Claim
Type's switch per expense line, the expense line picked or taken, what each expense line and the
claim owe, the caps at 0 and above 0, an approved supplier destination not capped, the claimant's
or any employee's phone, M-Pesa account or bank account refused, duplicates warned, a rejected
claim); the B2B and B2C payouts of a line (check_b2b, check_b2c_row: the line's destination,
amount and claim, another payout of the line, the caps and the day's cap at the send); the line's
status from its payouts and the name M-Pesa paid (a bank's name, a till's name filled later, a
mismatch commented and alerted once); the supplier suggestion; the drafts Pay merchants and payees,
Retry failed payees and the list's batch make; the paste parser; a save after submit; the cancel;
the reimbursed-total repair. Payable checks and what the claim has left to pay are
money_out.employee_documents' and owed's (stubbed here).
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
# the faked double-payout guard of a disbursement and the faked retry of its payouts (fixtures)
from test_fix3 import payout  # noqa: F401
from test_fix_b2creq import retry  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
NOW = b2b_fixture.NOW
CLAIM, LINE, TYPE = "Expense Claim", "M-Pesa Claim Payee", "Expense Claim Type"
B2B, B2C, ROW, REQ = ("Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Payment Disbursement Reference",
                      "B2C Disbursement Request")
SUITE, BANK_PAYBILL = "M-Pesa Suite Settings", "M-Pesa Bank Paybill"
TILL, PAYBILL, BANK, PHONE = "Till (Buy Goods)", "Paybill", "Bank Account (via Bank Paybill)", "Phone (B2C)"
_dict = fake_frappe._dict

LINE_COLUMNS = ("name", "idx", "parent", "parenttype", "parentfield", "payee_type", "payee_name", "destination",
                "expense_detail", "expense_type", "bank_paybill", "shortcode", "account_number", "phone", "amount",
                "destination_key", "status", "payout_doctype", "payout_name", "receipt", "paid_on", "sent_on",
                "error_message", "receiver_name", "name_matches", "name_note", "supplier_account", "name_verdict",
                "sent_by", "staff_check", "staff_note", "staff_involved")
EXTRA_COLUMNS = {
    B2B: ("claim_payee", "expense_claim", "receiver_public_name", "error_message", "result_desc"),
    ROW: ("claim_payee", "creation"),
    REQ: ("receiver_public_name", "error_message", "result_desc"),
    B2C: ("name", "docstatus", "company"),
    LINE: LINE_COLUMNS,
    CLAIM: ("name", "docstatus", "employee", "company"),
}


class Draft(fake_frappe.FakeDoc):
    """frappe.new_doc for the payouts make_payouts prepares: stored on insert, rows as their own table."""

    def __init__(self, env, doctype):
        super().__init__(env.frappe, doctype, {"doctype": doctype, "docstatus": 0})
        object.__setattr__(self, "flags", _dict())
        object.__setattr__(self, "_env", env)

    def update(self, values):
        self._row.update(values)

    def append(self, table, row):
        self._row.setdefault(table, []).append(_dict(row))

    def insert(self, ignore_permissions=False):
        env = self._env
        env.drafts.append((self.doctype, env.frappe.session.user, ignore_permissions))
        name = f"{'MB2B' if self.doctype == B2B else 'MPESA-B2C'}-{len(env.drafts)}"
        self._row.update(name=name, creation=NOW)
        db = env.frappe.db
        db.table(self.doctype)[name] = {k: v for k, v in self._row.items() if k != "references"}
        for i, row in enumerate(self._row.get("references") or [], 1):
            row.update(name=f"{name}-R{i}", parent=name, parenttype=B2C, idx=i, creation=NOW)
            db.table(ROW)[row.name] = row
        return self


@pytest.fixture
def env(monkeypatch):
    for doctype, extra in EXTRA_COLUMNS.items():
        monkeypatch.setitem(b2b_fixture.COLUMNS, doctype,
                            tuple(dict.fromkeys(b2b_fixture.COLUMNS.get(doctype, ("name",)) + extra)))
    with b2b_fixture.site(monkeypatch) as env:
        frappe = env.frappe
        frappe.db.columns |= {(B2B, "claim_payee"), (ROW, "claim_payee"), (TYPE, "allow_mpesa_payees")}
        frappe.session.user = "accounts@example.com"
        frappe.user_roles = {"Accounts User"}  # a preparer
        env.meta_fields["Employee"] = {"cell_number", "bank_ac_no"}
        env.drafts, env.problem, env.left = [], None, 1_000_000
        ed = importlib.import_module("mpsa_suite.money_out.employee_documents")
        monkeypatch.setattr(ed, "payable_problem", lambda doc, skip_gap=False, **kw: env.problem, raising=False)
        env.left_asked = []

        def left_to_pay(dt, dn, exclude_payees=()):
            env.left_asked.append((dt, dn, sorted(exclude_payees)))
            return _dict(left=env.left)

        monkeypatch.setattr(env.owed, "employee_doc_left_to_pay", left_to_pay, raising=False)
        cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
        monkeypatch.setattr(cp.pa, "alert", lambda message, doc, *a, **kw: env.alerts.append((doc.name, message)))
        schedule = importlib.import_module("mpsa_suite.money_out.schedule")
        env.b2c_checked = []
        monkeypatch.setattr(schedule, "check_b2c_settings", lambda *a: env.b2c_checked.append(a))
        wallet = importlib.import_module("mpsa_suite.wallet.accounts")
        monkeypatch.setattr(wallet, "bank_account", lambda settings, sub=None: b2b_fixture.BANK_ACCOUNT)
        frappe.new_doc = lambda doctype: Draft(env, doctype)
        env.cp = cp
        db = frappe.db
        db.table(SUITE)[SUITE].update(allow_claim_payees=1, claim_payee_max_amount=0, claim_payee_daily_cap=0,
                                      suggest_supplier_after=3, notify_role="Accounts Manager")
        db.table("Mpesa Settings")["Astral B2C"] = {"name": "Astral B2C", "business_shortcode": "600111",
                                                    "use_b2c": 1, "company": "Astral", "initiator_name": "apiop"}
        db.table(TYPE).update({"Fuel": {"name": "Fuel", "allow_mpesa_payees": 1},
                               "Casuals": {"name": "Casuals", "allow_mpesa_payees": 1},
                               "Travel": {"name": "Travel", "allow_mpesa_payees": 0}})
        db.table("Employee").update({
            "EMP-1": {"name": "EMP-1", "cell_number": "0722 000 111", "bank_ac_no": "01-0099-12345"},
            "EMP-2": {"name": "EMP-2", "cell_number": "+254 733 000 222", "bank_ac_no": None}})
        db.table("Employee M-Pesa Account")["MEA-9"] = {
            "name": "MEA-9", "docstatus": 1, "employee": "EMP-2", "account_type": TILL, "shortcode": "565656",
            "phone": None, "account_number": None}
        db.table("DocType")[BANK_PAYBILL] = {"name": BANK_PAYBILL}
        db.table(BANK_PAYBILL).update({
            "KCB": {"name": "KCB", "institution_name": "KCB Bank", "paybill": "522522", "enabled": 1,
                    "receiver_name": "KCB Paybill AC", "account_number_pattern": r"\d{10,13}",
                    "account_number_rule": "10 to 13 digits"},
            "OLD": {"name": "OLD", "institution_name": "Old Bank", "paybill": "400400", "enabled": 0,
                    "receiver_name": None},
            "NEW": {"name": "NEW", "institution_name": "New Bank", "paybill": "300300", "enabled": 1,
                    "receiver_name": None}})
        yield env


# ---------------------------------------------------------------- helpers
def expense(name="EXP-1", idx=1, expense_type="Fuel", amount=10000, sanctioned=10000):
    return _dict(name=name, idx=idx, expense_type=expense_type, amount=amount, sanctioned_amount=sanctioned)


def payee(name="LN-1", idx=1, payee_type=TILL, **kw):
    row = _dict(name=name, idx=idx, parent="HR-EXP-1", parenttype=CLAIM, parentfield="mpesa_payees",
                payee_type=payee_type, payee_name="Shell Kilimani", shortcode="835811", account_number=None,
                phone=None, bank_paybill=None, amount=2000, expense_detail="EXP-1", expense_type="Fuel",
                status="Draft", payout_doctype=None, payout_name=None, receipt=None, paid_on=None, sent_on=None,
                error_message=None, receiver_name=None, name_matches=0, name_note=None, supplier_account=None,
                destination=None, destination_key=None)
    if payee_type == PHONE:
        row.update(payee_name="Jane Wanjiku", shortcode=None, phone="0711 000 333")
    elif payee_type == PAYBILL:
        row.update(payee_name="Nairobi County", shortcode="647647", account_number="PARK-77")
    elif payee_type == BANK:
        row.update(payee_name="Mama Mboga Ltd", shortcode=None, bank_paybill="KCB", account_number="1234567890")
    row.update(kw)
    return row


def claim(env, lines=(), name="HR-EXP-1", expenses=None, store=True, **kw):
    """An Expense Claim (submitted and approved unless said otherwise) and its payee lines, stored
    as the site holds them (the lines as cleaned by a save) or only built."""
    doc = _dict(name=name, doctype=CLAIM, docstatus=1, employee="EMP-1", company="Astral", approval_status="Approved",
                grand_total=10000, total_amount_reimbursed=0, payable_account="Payable - AIL", currency="KES",
                is_paid=0, expenses=expenses or [expense()], mpesa_payees=list(lines), total_taxes_and_charges=0,
                total_advance_amount=0)
    doc.update(kw)
    for row in lines:
        row.parent = name
    if store:
        own = env.cp.own_numbers()
        for row in lines:
            env.cp.clean_line(row, own, [])
            env.frappe.db.table(LINE)[row.name] = row
        env.frappe.db.table(CLAIM)[name] = doc
    return doc


def request(env, line, name="MB2B-9", **kw):
    row = {"name": name, "docstatus": 0, "status": "Not Initiated", "claim_payee": line.name,
           "expense_claim": line.parent, "employee": "EMP-1", "company": "Astral", "amount": line.amount,
           "command_id": "BusinessBuyGoods", "receiver_shortcode": line.shortcode, "account_reference": None,
           "mpesa_settings": b2b_fixture.SETTINGS, "creation": NOW, **kw}
    env.frappe.db.table(B2B)[name] = row
    return _dict(row)


def disbursement(env, rows, name="MPESA-B2C-9", docstatus=0, **kw):
    parent = {"name": name, "docstatus": docstatus, "company": "Astral", **kw}
    env.frappe.db.table(B2C)[name] = parent
    refs = []
    for i, (line, extra) in enumerate(rows, 1):
        ref = _dict(name=f"{name}-R{i}", parent=name, parenttype=B2C, idx=i, reference_doctype=CLAIM,
                    reference_name=line.parent, party_type="Employee", party="EMP-1", allocated_amount=line.amount,
                    partyb=line.phone, payment_status="Not Initiated", b2c_disbursement_request=None,
                    claim_payee=line.name, creation=NOW)
        ref.update(extra)
        env.frappe.db.table(ROW)[ref.name] = ref
        refs.append(ref)
    return _dict(parent, references=refs), refs


def stored(env, name="LN-1"):
    return env.frappe.db.table(LINE)[name]


def settings(env, **values):
    env.frappe.db.table(SUITE)[SUITE].update(values)


def raises(env, match):
    return pytest.raises(env.frappe.ValidationError, match=match)


# ---------------------------------------------------------------- where a line pays
def test_destination_keys_are_the_payee_accounts_format(env):
    key = env.cp.destination_key
    assert key(PHONE, phone="0722-000 111") == "B2C:254722000111"
    assert key(TILL, "835811") == "TILL:835811"
    assert key(PAYBILL, "647647", "park 77-a") == "PB:647647:PARK77A"
    assert key(BANK, "522522", "01-0099/12345") == "PB:522522:01009912345"
    assert key("Bank Paybill (B2B)", "522522", "0123") == "PB:522522:0123"  # an employee account's old type
    assert key(PHONE, phone="12345") is None and key(TILL, "") is None


@pytest.mark.parametrize("kw, shown", [
    ({"payee_type": PHONE}, "0711 000 333"),
    ({"payee_type": TILL}, "835811"),
    ({"payee_type": PAYBILL}, "647647 / PARK-77"),
])
def test_a_line_reads_as_its_destination(env, kw, shown):
    row = payee(**kw)
    env.cp.clean_line(row, set(), [])
    assert row.destination == shown


# ---------------------------------------------------------------- one line's details
def test_a_phone_is_kept_as_2547_and_drops_what_is_not_a_phone(env):
    row = payee(payee_type=PHONE, shortcode="1234", account_number="X", bank_paybill="KCB")
    env.cp.clean_line(row, set(), [])
    assert (row.phone, row.shortcode, row.account_number, row.bank_paybill) == ("254711000333", None, None, None)
    assert row.destination_key == "B2C:254711000333"


def test_a_phone_that_is_not_safaricom_is_warned_not_refused(env):
    warnings = []
    env.cp.clean_line(payee(payee_type=PHONE, phone="0733 000 999"), set(), warnings)  # Airtel
    assert "does not start like a Safaricom number" in warnings[0]


@pytest.mark.parametrize("kw, msg", [
    ({"payee_type": PHONE, "phone": "12345"}, "not a Kenyan mobile number"),
    ({"payee_type": TILL, "shortcode": "83A811"}, "must be 5 to 7 digits"),
    ({"payee_type": PAYBILL, "account_number": " "}, "needs the Account Number"),
    ({"payee_type": PAYBILL, "account_number": "1" * 21}, "longer than M-Pesa allows"),
    ({"payee_type": TILL, "shortcode": "600999"}, "own M-Pesa numbers"),
    ({"payee_type": TILL, "amount": 100.5}, "whole shillings"),
    ({"payee_type": PHONE, "amount": 5}, "B2C pays KES 10 to 250,000"),
    ({"payee_type": TILL, "payee_name": " "}, "enter the Payee Name"),
    ({"payee_type": "Cash"}, "Pay To must be"),
    ({"payee_type": PAYBILL, "shortcode": "522522", "account_number": "0123"}, "KCB Bank's bank paybill: pick Pay To Bank"),
    # final review: a bank paybill shipped disabled (not checked yet) is no merchant's paybill either
    ({"payee_type": PAYBILL, "shortcode": "400400", "account_number": "0123"},
     "400400 is Old Bank's bank paybill, switched off in M-Pesa Bank Paybills: an M-Pesa Manager checks"),
])
def test_bad_line_details_are_refused(env, kw, msg):
    with raises(env, msg):
        env.cp.clean_line(payee(**kw), env.cp.own_numbers(), [])


def test_a_till_drops_an_account_number(env):
    row = payee(payee_type=TILL, account_number="IGNORED")
    env.cp.clean_line(row, set(), [])
    assert (row.account_number, row.destination_key) == (None, "TILL:835811")


def test_a_bank_account_takes_the_paybill_from_its_bank_never_the_typed_one(env):
    row = payee(payee_type=BANK, shortcode="999999", account_number="12-3456 7890")
    env.cp.clean_line(row, set(), [])
    assert (row.shortcode, row.account_number, row.destination_key) == ("522522", "1234567890", "PB:522522:1234567890")


@pytest.mark.parametrize("kw, msg", [
    ({"bank_paybill": None}, "pick the Bank"),
    ({"bank_paybill": "OLD"}, "Old Bank is switched off"),
    ({"account_number": "12345"}, "does not look like a KCB Bank account number \\(10 to 13 digits\\)"),
    ({"account_number": "12345678#0"}, "letters and digits only"),
])
def test_bad_bank_accounts_are_refused(env, kw, msg):
    with raises(env, msg):
        env.cp.clean_line(payee(payee_type=BANK, **kw), set(), [])


# ---------------------------------------------------------------- the claim's checks
def validate(env, doc, docstatus=None):
    if docstatus is not None:
        doc.docstatus = docstatus
    env.frappe.messages.clear()
    env.cp.validate_claim(doc)
    return env.frappe.messages


def test_a_claim_without_payees_is_left_alone_even_when_switched_off(env):
    settings(env, allow_claim_payees=0)
    assert validate(env, claim(env, store=False)) == []


def test_payees_are_refused_while_the_suite_switch_is_off(env):
    settings(env, allow_claim_payees=0)
    with raises(env, "switched off \\(M-Pesa Suite Settings\\)"):
        validate(env, claim(env, [payee()], store=False))


@pytest.mark.parametrize("kw, msg", [
    ({"is_paid": 1}, "marked Is Paid"),
    ({"currency": "USD"}, "M-Pesa pays KES only"),
])
def test_a_paid_or_foreign_currency_claim_pays_no_payee(env, kw, msg):
    with raises(env, msg):
        validate(env, claim(env, [payee()], store=False, **kw))


def test_the_expense_line_type_must_allow_payees(env):
    doc = claim(env, [payee()], store=False, expenses=[expense(expense_type="Travel")])
    with raises(env, "Travel is not paid to merchants or payees"):
        validate(env, doc)
    doc = claim(env, [payee()], store=False)
    validate(env, doc)
    assert doc.mpesa_payees[0].expense_type == "Fuel"


def test_the_only_expense_line_is_taken_and_one_of_several_must_be_picked_by_the_approval(env):
    line = payee(expense_detail=None)
    validate(env, claim(env, [line], store=False))
    assert line.expense_detail == "EXP-1"
    two = [expense(), expense("EXP-2", 2, "Casuals")]
    line = payee(expense_detail=None)
    messages = validate(env, claim(env, [line], store=False, expenses=two), docstatus=0)
    assert "pick the Expense Line this payee is paid for before the claim is approved" in messages[0]
    with raises(env, "Row #1: pick the Expense Line"):
        validate(env, claim(env, [payee(expense_detail=None)], store=False, expenses=two), docstatus=1)
    with raises(env, "no longer on the claim"):
        validate(env, claim(env, [payee(expense_detail="GONE")], store=False, expenses=two))


def test_payees_of_an_expense_line_stay_within_it(env):
    lines = [payee(amount=6000), payee("LN-2", 2, PHONE, amount=5000)]
    with raises(env, "expense line #1 \\(Fuel\\) add up to KES 11,000, more than the line's KES 10,000"):
        validate(env, claim(env, lines, store=False, grand_total=20000))
    # a draft's line not sanctioned yet counts its claimed amount; the approval its sanctioned one
    draft = claim(env, [payee(amount=6000)], store=False, expenses=[expense(sanctioned=0)], grand_total=0, docstatus=0)
    validate(env, draft)
    with raises(env, "more than the line's KES 0"):
        validate(env, claim(env, [payee(amount=6000)], store=False, expenses=[expense(sanctioned=0)]))


def test_payees_stay_within_what_the_claim_owes_and_the_rest_goes_to_the_employee(env):
    doc = claim(env, [payee(amount=4000)], store=False, grand_total=10000, total_amount_reimbursed=7000)
    with raises(env, "add up to KES 4,000, more than the claim owes \\(KES 3,000\\)"):
        validate(env, doc)
    validate(env, claim(env, [payee(amount=3000)], store=False, grand_total=10000, total_amount_reimbursed=7000))


def test_no_cap_by_default(env):
    validate(env, claim(env, [payee(amount=9000)], store=False))


def test_the_per_payment_cap_applies_when_set_but_not_to_an_approved_supplier_destination(env):
    settings(env, claim_payee_max_amount=5000)
    with raises(env, "Row #1: KES 9,000 is more than may be paid at once to a payee that is not an approved account"):
        validate(env, claim(env, [payee(amount=9000)], store=False))
    supplier = payee(payee_type=PAYBILL, shortcode="888880", account_number="acme-1", amount=9000)
    validate(env, claim(env, [supplier], store=False))
    assert supplier.supplier_account == "MSA-1"  # noted, and not capped


def test_the_daily_cap_refuses_a_phone_batch_above_it_and_warns_for_merchants(env):
    settings(env, claim_payee_daily_cap=5000)
    phones = [payee("LN-1", 1, PHONE, amount=3000), payee("LN-2", 2, PHONE, phone="0711000444", amount=3000)]
    with raises(env, "phone payees add up to KES 6,000.*paid in one batch"):
        validate(env, claim(env, phones, store=False))
    tills = [payee("LN-1", 1, amount=3000), payee("LN-2", 2, shortcode="835812", amount=3000)]
    messages = validate(env, claim(env, tills, store=False))
    assert "some will be paid on a later day" in messages[0]


@pytest.mark.parametrize("line, whose", [
    (payee(payee_type=PHONE, phone="0722000111"), "the claimant's own phone"),
    (payee(payee_type=PHONE, phone="0733000222"), "an employee's phone"),
    (payee(payee_type=TILL, shortcode="565656"), "an employee's till"),  # an Employee M-Pesa Account
    (payee(payee_type=BANK, bank_paybill="NEW", account_number="0100 9912345"), "the claimant's own bank account"),
])
def test_an_employees_destination_is_never_a_payee(env, line, whose):
    with raises(env, f"is {whose}. An employee is paid by reimbursing the claim"):
        validate(env, claim(env, [line], store=False))


def test_an_employees_bank_account_is_never_paid_as_a_merchants_paybill(env):
    # final review: a bank or Sacco paybill not on M-Pesa Bank Paybills (or the account number of a bank
    # account) credits the employee's bank account all the same: compared with every employee's
    line = payee(payee_type=PAYBILL, shortcode="777777", account_number="01-0099-12345")  # EMP-1's Bank A/C No.
    with raises(env, "Row #1: the account number 01-0099-12345 is the claimant's own bank account number. An "
                     "employee is paid by reimbursing the claim"):
        validate(env, claim(env, [line], store=False))
    env.frappe.db.table("Employee")["EMP-2"]["bank_ac_no"] = "PARK77"
    with raises(env, "is an employee's bank account number"):
        validate(env, claim(env, [payee(payee_type=PAYBILL, shortcode="647647", account_number="park-77")],
                            store=False))
    validate(env, claim(env, [payee(payee_type=PAYBILL, shortcode="647647", account_number="PARK-78")], store=False))


def test_an_employee_is_not_named_when_refused(env):
    with pytest.raises(env.frappe.ValidationError) as refused:
        validate(env, claim(env, [payee(payee_type=PHONE, phone="0733000222")], store=False))
    assert "EMP-2" not in str(refused.value)


def test_the_same_destination_twice_is_warned(env):
    messages = validate(env, claim(env, [payee(amount=1000), payee("LN-2", 2, amount=1000)], store=False))
    assert "Rows #1, #2 pay the same destination" in messages[0]


def test_a_rejected_claim_pays_no_payee_and_its_lines_are_cancelled(env):
    settings(env, allow_claim_payees=0)
    lines = [payee(payee_type=PHONE, phone="0722000111", amount=99999)]
    validate(env, claim(env, lines, store=False, approval_status="Rejected"))
    assert lines[0].status == "Cancelled"


def test_the_approval_of_payees_is_a_named_users_never_the_shared_administrators(env):
    # final review (decision 1): approving the claim approves its payees' unverified destinations
    doc = claim(env, [payee()], store=False)
    doc._action = "submit"
    env.frappe.session.user = "Administrator"
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        validate(env, doc, docstatus=1)
    validate(env, claim(env, [payee()], store=False), docstatus=0)  # a draft's save by anyone
    rejected = claim(env, [payee()], store=False, approval_status="Rejected")
    rejected._action = "submit"
    validate(env, rejected, docstatus=1)  # a rejection approves nobody
    env.frappe.session.user = "approver@example.com"
    validate(env, doc, docstatus=1)


def test_a_draft_or_its_approval_starts_its_lines_afresh_whatever_was_copied_or_typed(env):
    # an amend copies every field of the cancelled claim's lines; the API could set them on a draft
    copied = dict(status="Cancelled", payout_doctype=B2B, payout_name="MB2B-1", receipt="RCPT1", sent_on=NOW,
                  paid_on=NOW, error_message="Failed", receiver_name="X", name_matches=1, name_note="n")
    line = payee(**copied)
    validate(env, claim(env, [line], store=False), docstatus=0)
    assert (line.status, line.payout_name, line.receipt, line.sent_on, line.error_message, line.name_matches) == (
        "Draft", None, None, None, None, 0)
    line = payee(**copied)
    validate(env, claim(env, [line], store=False, _action="submit"))  # the approval: nothing was paid before it
    assert (line.status, line.payout_doctype) == ("Draft", None)
    line = payee(**copied)
    validate(env, claim(env, [line], store=False))  # a submitted claim's lines are the server's (keep_results)
    assert line.status == "Cancelled"


def test_an_amended_claims_lines_take_the_same_expense_line_of_the_new_claim(env):
    env.frappe.db.table("Expense Claim Detail").update({
        "OLD-1": {"name": "OLD-1", "parent": "HR-EXP-1", "idx": 1, "expense_type": "Fuel"},
        "OLD-2": {"name": "OLD-2", "parent": "HR-EXP-1", "idx": 2, "expense_type": "Casuals"}})
    two = [expense("NEW-1", 1, "Fuel"), expense("NEW-2", 2, "Casuals")]
    lines = [payee(payee_type=PHONE, expense_detail="OLD-2"), payee("LN-2", 2, expense_detail="OLD-1")]
    doc = claim(env, lines, name="HR-EXP-1-1", store=False, expenses=two, amended_from="HR-EXP-1", grand_total=20000)
    assert validate(env, doc, docstatus=0) == []
    assert [line.expense_detail for line in lines] == ["NEW-2", "NEW-1"]
    # one that is gone: picked again before the approval (a one-line claim's line is taken)
    line = payee(expense_detail="GONE")
    messages = validate(env, claim(env, [line], store=False, expenses=two, grand_total=20000), docstatus=0)
    assert "its expense line is no longer on the claim: pick its Expense Line again before the claim is approved" in (
        messages[0])
    assert line.expense_detail is None
    line = payee(expense_detail="GONE")
    validate(env, claim(env, [line], store=False), docstatus=0)
    assert line.expense_detail == "EXP-1"


def test_a_claim_without_a_currency_is_in_its_companys(env):
    env.frappe.db.table("Company")["Astral"] = {"name": "Astral", "default_currency": "USD"}
    doc = claim(env, [payee()], store=False)
    doc.pop("currency")  # v15's Expense Claim has no currency field (v16's has)
    with raises(env, "This claim is in USD. M-Pesa pays KES only"):
        validate(env, doc)
    env.frappe.db.table("Company")["Astral"]["default_currency"] = "KES"
    validate(env, doc)


@pytest.mark.parametrize("line, refused", [
    (payee(payee_type=PAYBILL, account_number="0733 000 222"), "account number 0733 000 222 is an employee's phone"),
    (payee(payee_type=BANK, account_number="0722000111"), "the account number 0722000111 is the claimant's own phone"),
    (payee(payee_type=PAYBILL, account_number="0711000999"), None),  # nobody's phone on file
])
def test_an_employees_phone_as_the_account_number_is_refused(env, line, refused):
    doc = claim(env, [line], store=False)
    if refused:
        with raises(env, refused):
            validate(env, doc)
    else:
        validate(env, doc)


# ---------------------------------------------------------------- a B2B request for a merchant line
# ---------------------------------------------------------------- what the payees hold of the claim
def test_the_lines_no_counted_payout_represents_are_held_back_from_the_claim(env):
    # final review: the claim's approved merchants and casual payees not paid yet are theirs: what the
    # claim has left for the employee (owed.left_to_pay) takes them off, unless a payout owed counts
    # already represents them (a B2B request, drafts too, or a sent B2C row)
    lines = [payee("LN-1", 1), payee("LN-2", 2, PAYBILL, amount=1500), payee("LN-3", 3, PHONE, amount=1000),
             payee("LN-4", 4, PHONE, phone="0711 000 334", amount=700), payee("LN-5", 5, BANK, amount=1200),
             payee("LN-6", 6, shortcode="835812", amount=300), payee("LN-7", 7, shortcode="835813", amount=400),
             payee("LN-8", 8, shortcode="835814", amount=600)]
    claim(env, lines)
    env.frappe.db.table(LINE)["LN-8"]["status"] = "Cancelled"
    request(env, lines[1], "MB2B-2")  # a draft B2B: counted by owed.b2b_open
    disbursement(env, [(lines[2], {})], "MPESA-B2C-3")  # a draft B2C: owed counts it once it is sent
    disbursement(env, [(lines[3], {})], "MPESA-B2C-4", docstatus=1)  # sent: counted
    request(env, lines[4], "MB2B-5", docstatus=1, status="Failed")  # failed: counted no more
    request(env, lines[5], "MB2B-6", docstatus=2, status="Unknown")  # cancelled by a manager: counted no more
    request(env, lines[6], "MB2B-7", docstatus=1, status="Paid")  # paid: counted (or booked on the claim)
    parts = env.cp.unpaid_lines("HR-EXP-1")
    assert [(p.kind, p.line, p.amount) for p in parts] == [
        ("payee", "LN-1", 2000), ("payee", "LN-3", 1000), ("payee", "LN-5", 1200), ("payee", "LN-6", 300)]
    assert parts[0].label == "payee row #1 (Shell Kilimani) 2,000 not paid yet"
    # the lines a payout pays are its own share
    assert [p.line for p in env.cp.unpaid_lines("HR-EXP-1", exclude_lines=["LN-1", "LN-3"])] == ["LN-5", "LN-6"]
    env.frappe.db.table(CLAIM)["HR-EXP-1"]["docstatus"] = 2
    assert env.cp.unpaid_lines("HR-EXP-1") == []  # a cancelled claim pays nobody
    assert env.cp.unpaid_lines("HR-EXP-2") == []  # a claim with no payee lines: nothing read


@pytest.mark.parametrize("payout, counted", [
    (dict(doctype=B2B, state="Queued", docstatus=0, status="Not Initiated"), True),
    (dict(doctype=B2B, state="Queued", docstatus=1, status=None), True),
    (dict(doctype=B2B, state="In Flight", docstatus=1, status="Unknown"), True),
    (dict(doctype=B2B, state="Paid", docstatus=2, status="Paid"), True),
    (dict(doctype=B2B, state="Failed", docstatus=1, status="Failed"), False),
    (dict(doctype=B2B, state="In Flight", docstatus=2, status="Initiated"), False),
    (dict(doctype=B2C, state="Queued", parent_docstatus=0, request=None, request_docstatus=0), False),
    (dict(doctype=B2C, state="In Flight", parent_docstatus=1, request=None, request_docstatus=0), True),
    (dict(doctype=B2C, state="Failed", parent_docstatus=1, request="R", request_docstatus=1), False),
    (dict(doctype=B2C, state="In Flight", parent_docstatus=2, request="R", request_docstatus=1), True),
    (dict(doctype=B2C, state="In Flight", parent_docstatus=2, request="R", request_docstatus=2), False),
    (dict(doctype=B2C, state="Paid", parent_docstatus=2, request="R", request_docstatus=2), True),
])
def test_a_payout_holds_its_line_when_owed_counts_it(env, payout, counted):
    assert env.cp.counted_in_left_to_pay(_dict(payout)) is counted


def test_the_drafts_fit_in_what_the_claim_leaves_besides_its_other_payees(env):
    lines = [payee("LN-1", 1), payee("LN-2", 2, PAYBILL, amount=1500)]
    claim(env, lines)
    env.cp.pay_payees("HR-EXP-1")
    # the lines being paid are their own share of what the claim has left
    assert env.left_asked == [(CLAIM, "HR-EXP-1", ["LN-1", "LN-2"])]


def b2b_doc(env, line, **kw):
    return _dict(request(env, line, **kw))


def test_a_draft_takes_the_lines_destination_and_the_claims_employee_and_company(env):
    line = payee(payee_type=PAYBILL)
    claim(env, [line])
    doc = b2b_doc(env, line, command_id=None, receiver_shortcode="999", account_reference="TYPED", employee=None,
                  company="Other Co")
    env.cp.check_b2b(doc)
    assert (doc.command_id, doc.receiver_shortcode, doc.account_reference, doc.employee, doc.company) == (
        "BusinessPayBill", "647647", "PARK-77", "EMP-1", "Astral")


@pytest.mark.parametrize("kw, msg", [
    ({"expense_claim": "HR-EXP-2"}, "is a payee of HR-EXP-1, not of HR-EXP-2"),
    ({"supplier": "ACME"}, "clear Supplier"),
    ({"employee_mpesa_account": "MEA-1"}, "clear Employee M-Pesa Account"),
    ({"salary_slip": "SAL-1"}, "clear Salary Slip"),
    ({"amount": 2500}, "KES 2,500 is not what payee row #1 of HR-EXP-1 pays \\(KES 2,000\\)"),
    # final review: never part of it either (the line would show Paid, its rest reimbursed to the employee)
    ({"amount": 1000}, "KES 1,000 is not what payee row #1 of HR-EXP-1 pays \\(KES 2,000\\): a payee is paid its "
                       "whole line"),
])
def test_a_request_pays_its_line_only(env, kw, msg):
    line = payee()
    claim(env, [line])
    with raises(env, msg):
        env.cp.check_b2b(b2b_doc(env, line, **kw))


def test_a_phone_line_is_never_paid_by_b2b(env):
    line = payee(payee_type=PHONE)
    claim(env, [line])
    with raises(env, "is a phone: it is paid by B2C"):
        env.cp.check_b2b(b2b_doc(env, line))


@pytest.mark.parametrize("kw", [{"receiver_shortcode": "835812"}, {"command_id": "BusinessPayBill"},
                                {"employee": "EMP-2"}])
def test_a_request_changed_from_its_line_is_not_sent(env, kw):
    line = payee()
    claim(env, [line])
    with raises(env, "is not payee row #1's|is EMP-1's claim"):
        env.cp.check_b2b(b2b_doc(env, line, docstatus=1, **kw))


@pytest.mark.parametrize("other, refused", [
    ({"docstatus": 0, "status": "Not Initiated"}, False),  # another draft: whichever is sent first takes the line
    ({"docstatus": 1, "status": "Failed"}, False),
    ({"docstatus": 2, "status": "Not Initiated"}, False),
    ({"docstatus": 1, "status": "Not Initiated"}, True),  # submitted, about to be sent
    ({"docstatus": 1, "status": "Initiated"}, True),
    ({"docstatus": 1, "status": "Unknown"}, True),
    ({"docstatus": 1, "status": "Paid"}, True),
    ({"docstatus": 2, "status": "Paid"}, True),
])
def test_a_line_is_never_paid_twice(env, other, refused):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", **other)
    doc = b2b_doc(env, line)
    if refused:
        with raises(env, "is (being )?paid by Mpesa B2B Payment Request MB2B-1: it is never paid twice"):
            env.cp.check_b2b(doc)
    else:
        env.cp.check_b2b(doc)


def test_the_send_checks_the_claim_and_notes_the_payout_on_the_line(env):
    line = payee()
    claim(env, [line])
    doc = b2b_doc(env, line, docstatus=1)
    env.problem = "HR-EXP-1 is not approved."
    with raises(env, "is not approved"):
        env.cp.check_b2b(doc)
    env.problem = None
    env.cp.check_b2b(doc)
    assert ("lock", "b2c-pay:Expense Claim:HR-EXP-1") in env.log
    row = stored(env)
    assert (row["status"], row["payout_doctype"], row["payout_name"], row["sent_on"]) == ("In Flight", B2B, "MB2B-9", NOW)
    assert any(entry[0] == "sql" and B2B in entry[1] and entry[2] for entry in env.log)  # a locking read


def test_the_send_refuses_a_destination_an_employee_registered_since(env):
    line = payee(shortcode="565657")
    claim(env, [line])
    env.frappe.db.table("Employee M-Pesa Account")["MEA-10"] = {
        "name": "MEA-10", "docstatus": 0, "employee": "EMP-2", "account_type": TILL, "shortcode": "565657"}
    with raises(env, "HR-EXP-1: Row #1: 565657 is an employee's till"):
        env.cp.check_b2b(b2b_doc(env, line, docstatus=1))


def test_the_send_applies_the_caps_when_set(env):
    line = payee(amount=4000)
    claim(env, [line])
    settings(env, claim_payee_max_amount=3000)
    with raises(env, "KES 4,000 is more than may be paid at once"):
        env.cp.check_b2b(b2b_doc(env, line, docstatus=1))
    settings(env, claim_payee_max_amount=0, claim_payee_daily_cap=5000)
    # 2,000 sent today by another claim's payee (a submitted request that did not fail)
    earlier = payee("LN-7", amount=2000, shortcode="835899")
    claim(env, [earlier], name="HR-EXP-7")
    request(env, earlier, name="MB2B-7", docstatus=1, status="Initiated")
    env.frappe.db.table(LINE)["LN-7"].update(sent_on=NOW, payout_doctype=B2B, payout_name="MB2B-7", status="In Flight")
    with raises(env, "KES 2,000 was sent today .* with this KES 4,000 it would pass the day's cap of KES 5,000"):
        env.cp.check_b2b(b2b_doc(env, line, docstatus=1))
    env.frappe.db.table(B2B)["MB2B-7"]["status"] = "Failed"  # a failed one does not count
    env.cp.check_b2b(b2b_doc(env, line, docstatus=1))


def test_the_day_counts_other_companies_and_approved_suppliers_out(env):
    settings(env, claim_payee_daily_cap=5000)
    other = payee("LN-7", amount=4000, shortcode="835899")
    claim(env, [other], name="HR-EXP-7", company="Other Co")
    request(env, other, name="MB2B-7", docstatus=1, status="Paid")
    env.frappe.db.table(LINE)["LN-7"].update(sent_on=NOW, payout_doctype=B2B, payout_name="MB2B-7")
    assert env.cp.sent_today("Astral") == 0
    assert env.cp.sent_today("Other Co") == 4000
    env.frappe.db.table(LINE)["LN-7"].update(supplier_account="MSA-1")
    assert env.cp.sent_today("Other Co") == 0
    env.frappe.db.table(LINE)["LN-7"].update(supplier_account=None, sent_on=NOW - datetime.timedelta(days=1))
    assert env.cp.sent_today("Other Co") == 0  # yesterday's


def test_the_days_cap_is_counted_under_the_companys_lock_by_a_locking_read(env):
    line = payee(amount=1000)
    claim(env, [line])
    settings(env, claim_payee_daily_cap=5000)
    env.cp.check_b2b(b2b_doc(env, line, docstatus=1))
    # the company's lock first, then the claim's: two claims sent together never both pass the cap
    assert [e[1] for e in env.log if e[0] == "lock"] == ["claim-payee-day:Astral", "b2c-pay:Expense Claim:HR-EXP-1"]
    after = env.log.index(("lock", "claim-payee-day:Astral"))
    assert any(e[0] == "sql" and e[1][0] == LINE and e[2] for e in env.log[after:]), "sent_today, a locking read"
    # no cap, no company lock
    settings(env, claim_payee_daily_cap=0)
    env.locks.clear()
    env.log.clear()
    env.cp.check_b2b(b2b_doc(env, line, docstatus=1))
    assert [e[1] for e in env.log if e[0] == "lock"] == ["b2c-pay:Expense Claim:HR-EXP-1"]


def test_a_supplier_request_never_pays_a_payee_line(env):
    line = payee()
    claim(env, [line])
    doc = _dict(name="MB2B-9", docstatus=0, supplier="ACME", employee=None, claim_payee="LN-1", amount=2000)
    with raises(env, "names Expense Claim payee LN-1: a claim's payee is paid on the claim"):
        env.b2b.validate_request(doc)
    env.frappe.user_roles = {"M-Pesa Payer"}
    with raises(env, "names Expense Claim payee LN-1"):
        env.b2b.check_ready_to_send(_dict(doc, docstatus=1))
    env.cp.check_supplier_request(_dict(supplier="ACME"))  # no payee line: nothing to say


def test_an_approved_supplier_destination_is_not_capped_at_the_send(env):
    line = payee(payee_type=PAYBILL, shortcode="888880", account_number="ACME-1", amount=9000)
    claim(env, [line])
    settings(env, claim_payee_max_amount=1000, claim_payee_daily_cap=1000)
    env.cp.check_b2b(b2b_doc(env, line, docstatus=1, command_id="BusinessPayBill", account_reference="ACME-1"))
    assert stored(env)["supplier_account"] == "MSA-1"


# ---------------------------------------------------------------- a B2C row for a phone line
def phone_line(name="LN-1", idx=1, **kw):
    return payee(name, idx, PHONE, **kw)


@pytest.mark.parametrize("extra, msg", [
    ({"reference_name": "HR-EXP-2"}, "its document must be Expense Claim HR-EXP-1"),
    ({"party": "EMP-2"}, "the party is the claimant, EMP-1"),
    ({"partyb": "254711000999"}, "the phone is not payee row #1's"),
    ({"allocated_amount": 2500}, "KES 2,500 is not what payee row #1"),
    ({"allocated_amount": 500}, "KES 500 is not what payee row #1 .* a payee is paid its whole line"),
])
def test_a_row_pays_its_line_only(env, extra, msg):
    line = phone_line()
    claim(env, [line])
    parent, refs = disbursement(env, [(line, extra)])
    with raises(env, msg):
        env.cp.check_b2c_row(parent, refs[0])


def test_a_row_pays_from_its_claims_company_only(env):
    line = phone_line()
    claim(env, [line])
    parent, refs = disbursement(env, [(line, {})], company="Other Co")
    with raises(env, "Row #1: HR-EXP-1 belongs to Astral, not Other Co"):
        env.cp.check_b2c_row(parent, refs[0])


def test_a_merchant_line_is_never_paid_to_a_phone(env):
    line = payee()
    claim(env, [line])
    parent, refs = disbursement(env, [(line, {"partyb": "254711000333"})])
    with raises(env, "is a till: it is paid by B2B"):
        env.cp.check_b2c_row(parent, refs[0])


def test_two_rows_never_pay_one_line(env):
    line = phone_line()
    claim(env, [line])
    parent, refs = disbursement(env, [(line, {}), (line, {"name": "X-2"})])
    with raises(env, "Rows #1, #2 pay the same payee row"):
        env.cp.check_b2c_row(parent, refs[0])


def test_a_row_of_a_line_paid_by_another_disbursement_is_refused(env):
    line = phone_line()
    claim(env, [line])
    disbursement(env, [(line, {"payment_status": "Paid"})], name="MPESA-B2C-1", docstatus=1)
    parent, refs = disbursement(env, [(line, {})])
    with raises(env, "is paid by B2C Payment Disbursement MPESA-B2C-1"):
        env.cp.check_b2c_row(parent, refs[0])


def test_a_row_already_sent_from_the_original_is_not_checked_again(env):
    line = phone_line()
    claim(env, [line])
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Paid"}
    parent, refs = disbursement(env, [(line, {"b2c_disbursement_request": "B2C-1"})], docstatus=1)
    env.problem = "not payable any more"
    env.cp.check_b2c_row(parent, refs[0])  # nothing refused: it is not sent again


def test_a_failed_payout_is_not_retried_once_the_line_is_paid_again(env):
    line = phone_line()
    claim(env, [line])
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Failed",
                                         "b2c_payment_disbursement": "MPESA-B2C-1",
                                         "b2c_payment_disbursement_reference": "MPESA-B2C-1-R1"}
    disbursement(env, [(line, {"b2c_disbursement_request": "B2C-1", "payment_status": "Failed"})],
                 name="MPESA-B2C-1", docstatus=1)
    assert env.cp.retry_problem("B2C-1") is None
    disbursement(env, [(line, {})], name="MPESA-B2C-2", docstatus=0)  # Retry Failed Payees' draft
    assert env.cp.retry_problem("B2C-1") is None  # whichever is sent first takes the line
    env.frappe.db.table(B2C)["MPESA-B2C-2"]["docstatus"] = 1
    assert "B2C Payment Disbursement MPESA-B2C-2 pays now" in env.cp.retry_problem("B2C-1")
    env.frappe.db.table(REQ)["B2C-5"] = {"name": "B2C-5", "b2c_payment_disbursement_reference": None}
    assert env.cp.retry_problem("B2C-5") is None  # no payee line


def test_a_payees_own_retry_is_checked_like_a_send(env):
    line = phone_line()
    claim(env, [line])
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Failed", "amount": 2000,
                                         "b2c_payment_disbursement": "MPESA-B2C-1",
                                         "b2c_payment_disbursement_reference": "MPESA-B2C-1-R1"}
    disbursement(env, [(line, {"b2c_disbursement_request": "B2C-1", "payment_status": "Failed"})],
                 name="MPESA-B2C-1", docstatus=1)
    env.problem = "HR-EXP-1 is not approved."
    with raises(env, "is not approved"):
        env.cp.check_retry("B2C-1")
    env.problem = None
    settings(env, claim_payee_daily_cap=1500)
    with raises(env, "with this KES 2,000 it would pass the day's cap of KES 1,500"):
        env.cp.check_retry("B2C-1")
    settings(env, claim_payee_daily_cap=0)
    env.cp.check_retry("B2C-1")
    assert "b2c-pay:Expense Claim:HR-EXP-1" in env.locks
    row = stored(env)
    assert (row["status"], row["payout_doctype"], row["payout_name"], row["sent_on"]) == (
        "In Flight", B2C, "MPESA-B2C-1", NOW)
    # Retry Failed Payees paid the line since: the old payout is never sent again
    disbursement(env, [(line, {"payment_status": "Paid"})], name="MPESA-B2C-2", docstatus=1)
    with raises(env, "B2C-1 pays an Expense Claim payee that B2C Payment Disbursement MPESA-B2C-2 pays now"):
        env.cp.check_retry("B2C-1")
    assert any(e[0] == "sql" and e[1][0] == ROW and e[2] for e in env.log), "a locking read under the claim's lock"
    env.frappe.db.table(REQ)["B2C-5"] = {"name": "B2C-5", "b2c_payment_disbursement_reference": None}
    env.cp.check_retry("B2C-5")  # pays no payee line: nothing to check


def test_both_retry_buttons_check_a_payee_line_before_anything_is_sent(retry, monkeypatch):  # noqa: F811
    # the claim's and the employee's own retry checks (WP3) are tested in test_employee_documents.py
    monkeypatch.setattr(importlib.import_module("mpsa_suite.money_out.employee_documents"), "check_b2c_retry",
                        lambda doc: None)
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    seen = []

    def check_retry(name):
        seen.append((name, list(retry.frappe.locks_held)))
        raise retry.frappe.ValidationError("paid since by MPESA-B2C-2")

    monkeypatch.setattr(cp, "check_retry", check_retry)
    request = retry.failed(amount=1000, doctype=CLAIM, reference="HR-EXP-1")
    with pytest.raises(fake_frappe.ValidationError, match="paid since"):
        request.retry_failed_payment()
    assert seen == [("B2C-1", ["b2c-pay:Expense Claim:HR-EXP-1"])]  # under the claim's lock
    assert retry.sends == [] and retry.frappe.db.table(REQ)["B2C-1"]["status"] == "Failed"
    # after what the claim owes is checked (that refusal writes nothing on the line)
    assert [c[0] for c in retry.frappe.calls][-1] == "current_outstanding"
    retry.owed = 0.0
    seen.clear()
    with pytest.raises(fake_frappe.ValidationError, match="owes 0.0"):
        request.retry_failed_payment()
    assert seen == []


def test_the_send_counts_the_whole_batch_against_the_days_cap(env):
    lines = [phone_line(amount=3000), phone_line("LN-2", 2, phone="0711000444", amount=3000)]
    claim(env, lines)
    settings(env, claim_payee_daily_cap=5000)
    parent, refs = disbursement(env, [(lines[0], {}), (lines[1], {})], docstatus=1)
    parent.flags = _dict()
    with raises(env, "with this KES 6,000 it would pass the day's cap of KES 5,000"):
        env.cp.check_b2c_row(parent, refs[0])
    settings(env, claim_payee_daily_cap=6000)
    parent.flags = _dict()
    for ref in refs:
        env.cp.check_b2c_row(parent, ref)
    assert [stored(env, n)["status"] for n in ("LN-1", "LN-2")] == ["In Flight", "In Flight"]
    assert stored(env, "LN-2")["payout_name"] == "MPESA-B2C-9"


# ---------------------------------------------------------------- a line's status from its payouts
@pytest.mark.parametrize("docstatus, status, state", [
    (0, "Not Initiated", "Queued"), (1, "Not Initiated", "Queued"), (1, "Initiated", "In Flight"),
    (1, "Unknown", "In Flight"), (1, "Paid", "Paid"), (1, "Failed", "Failed"), (2, "Paid", "Paid"),
    (2, "Unknown", "In Flight"), (2, "Not Initiated", None), (2, "Failed", None),
])
def test_what_a_b2b_request_says_about_its_line(env, docstatus, status, state):
    assert env.cp.b2b_state(docstatus, status) == state


@pytest.mark.parametrize("args, state", [
    ((0, "Not Initiated"), "Queued"),
    ((1, "Initiated"), "In Flight"),
    ((1, "Failed"), "Failed"),  # never sent (an amount the job refused)
    ((1, "Initiated", "B2C-1", 1, "Not Initiated"), "In Flight"),
    ((1, "Initiated", "B2C-1", 1, "Status Unknown"), "In Flight"),
    ((1, "Paid", "B2C-1", 1, "Paid"), "Paid"),
    ((1, "Failed", "B2C-1", 1, "Failed"), "Failed"),
    ((2, "Initiated"), None),
    ((2, "Paid", "B2C-1", 2, "Paid"), "Paid"),
])
def test_what_a_b2c_row_says_about_its_line(env, args, state):
    assert env.cp.b2c_state(*args) == state


def test_the_line_follows_its_furthest_payout_and_flags_a_double_payment(env):
    cp = env.cp

    def p(state, name, creation=NOW):
        return _dict(state=state, doctype=B2B, name=name, creation=creation, receipt=f"R-{name}", receiver_name="X",
                     paid_on=NOW, error="no float")

    assert cp.line_values(_dict(), [], 1)["status"] == "Draft"
    values = cp.line_values(_dict(), [p("Failed", "A"), p("Queued", "B")], 1)
    assert (values["status"], values["payout_name"], values["error_message"]) == ("Queued", "B", None)
    values = cp.line_values(_dict(), [p("Failed", "A")], 1)
    assert (values["status"], values["error_message"]) == ("Failed", "no float")
    values = cp.line_values(_dict(), [p("Paid", "A"), p("Paid", "B")], 1)
    assert values["error_message"].startswith("Paid 2 times: A, B")
    assert cp.line_values(_dict(), [p("Queued", "A")], 2)["status"] == "Cancelled"  # the claim was cancelled
    assert cp.line_values(_dict(), [p("Paid", "A")], 2)["status"] == "Paid"


def paid_result(env, name, receiver="835811 - SHELL KILIMANI", **kw):
    env.frappe.db.table(B2B)[name].update(status="Paid", docstatus=1, transaction_id="RCPT1", receiver_public_name=receiver,
                                          trans_completed_time=NOW, **kw)


def test_a_paid_result_reaches_the_line_with_the_name_checked(env):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated")
    paid_result(env, "MB2B-1")
    env.cp.on_payout_result(B2B, "MB2B-1")
    row = stored(env)
    assert (row["status"], row["receipt"], row["receiver_name"], row["name_matches"]) == (
        "Paid", "RCPT1", "835811 - SHELL KILIMANI", 1)
    assert env.alerts == [] and env.frappe.comments == []


def test_a_name_that_does_not_match_is_commented_and_alerted_once(env):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated")
    paid_result(env, "MB2B-1", receiver="835811 - JOHN DOE ENTERPRISES")
    env.cp.on_payout_result(B2B, "MB2B-1")
    env.cp.on_payout_result(B2B, "MB2B-1")  # the result again: nothing new
    row = stored(env)
    assert (row["name_matches"], "but the claim gives 'Shell Kilimani'" in row["name_note"]) == (0, True)
    assert len(env.alerts) == 1 and env.alerts[0][0] == "HR-EXP-1"
    assert len(env.frappe.comments) == 1 and env.frappe.comments[0][:2] == (CLAIM, "HR-EXP-1")


def test_a_number_that_is_not_the_lines_is_a_mismatch(env):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated")
    paid_result(env, "MB2B-1", receiver="835812 – SHELL KILIMANI")
    env.cp.on_payout_result(B2B, "MB2B-1")
    assert "number 835812, not 835811" in stored(env)["name_note"] and len(env.alerts) == 1


def test_a_tills_name_comes_later_and_is_checked_then(env):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated")
    paid_result(env, "MB2B-1", receiver=None)
    env.cp.on_payout_result(B2B, "MB2B-1")
    assert stored(env)["name_note"].startswith("Name not checked (till)") and env.alerts == []
    # Safaricom's Transaction Status answer fills the receiver (money_out.b2b_status)
    env.frappe.db.table(B2B)["MB2B-1"]["receiver_public_name"] = "835811 - OTHER BUSINESS"
    env.cp.on_payout_result(B2B, "MB2B-1")
    assert stored(env)["name_matches"] == 0 and len(env.alerts) == 1


def test_a_bank_account_is_checked_against_its_banks_name(env):
    line = payee(payee_type=BANK)
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated", command_id="BusinessPayBill",
            receiver_shortcode="522522", account_reference="1234567890")
    paid_result(env, "MB2B-1", receiver="522522 - KCB Paybill AC")
    env.cp.on_payout_result(B2B, "MB2B-1")
    # R4.1: the bank's own name is all M-Pesa shows, never who holds the account: not verified, not a match,
    # and no alarm (a wrong bank still is: test_a_bank_without_its_safaricom_name_is_checked_by_number_only)
    line = stored(env)
    assert (line["name_matches"], line["name_verdict"], env.alerts) == (0, "Not Verified", [])
    assert line["name_note"].startswith("Not verified: M-Pesa paid '522522 - KCB Paybill AC', the bank's own name")


def test_a_bank_without_its_safaricom_name_is_checked_by_number_only(env):
    line = payee(payee_type=BANK, bank_paybill="NEW", account_number="77")
    claim(env, [line])
    assert env.cp.name_check(stored(env), "300300 - NEW BANK COLLECTIONS", None)[0] is None
    assert env.cp.name_check(stored(env), "300301 - NEW BANK COLLECTIONS", None)[0] is False


@pytest.mark.parametrize("received, verdict", [
    ("254711000333 - JANE WANJIKU", True),
    ("2547****0333 - JANE WANJIKU MUTHONI", True),
    ("0711***333 - JANE WANJIKU", True),
    ("2547****0999 - JANE WANJIKU", False),
    ("JOHN DOE", False),
])
def test_a_phones_name_and_masked_number(env, received, verdict):
    line = phone_line()
    env.cp.clean_line(line, set(), [])
    assert env.cp.name_check(line, received)[0] is verdict


def test_a_b2c_result_reaches_its_line(env):
    line = phone_line()
    claim(env, [line])
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Failed",
                                         "result_desc": "The initiator information is invalid.",
                                         "b2c_payment_disbursement_reference": "MPESA-B2C-9-R1"}
    disbursement(env, [(line, {"b2c_disbursement_request": "B2C-1", "payment_status": "Failed"})], docstatus=1)
    env.cp.on_payout_result(REQ, "B2C-1")
    row = stored(env)
    assert (row["status"], row["payout_doctype"], row["payout_name"], row["error_message"]) == (
        "Failed", B2C, "MPESA-B2C-9", "The initiator information is invalid.")


def test_a_payout_that_pays_no_line_is_left_alone_and_errors_are_only_logged(env, monkeypatch):
    env.frappe.db.table(B2B)["MB2B-5"] = {"name": "MB2B-5", "claim_payee": None}
    env.cp.on_payout_result(B2B, "MB2B-5")
    monkeypatch.setattr(env.cp, "_line_of", lambda *a: 1 / 0)
    env.cp.on_payout_result(B2B, "MB2B-5")
    assert env.frappe.errors[-1][1]["title"] == "M-Pesa claim payee result not recorded"
    monkeypatch.setattr(env.cp, "_line_of", lambda *a: (_ for _ in ()).throw(env.frappe.QueryDeadlockError()))
    with pytest.raises(env.frappe.QueryDeadlockError):  # the Result is recorded again from a job
        env.cp.on_payout_result(B2B, "MB2B-5")


def test_a_merchant_request_refused_at_once_fails_its_line(env, monkeypatch):
    line = payee()
    claim(env, [line])
    request(env, line, docstatus=1, status="Not Initiated")
    env.frappe.db.table(LINE)["LN-1"].update(status="In Flight", payout_doctype=B2B, payout_name="MB2B-9")
    common = importlib.import_module("mpsa_suite.money_out.common")
    doc = fake_frappe.FakeDoc(env.frappe, B2B, env.frappe.db.table(B2B)["MB2B-9"])
    common.apply_ack(doc, {"ResponseCode": "1", "errorMessage": "The balance is insufficient"}, None)
    assert (stored(env)["status"], stored(env)["error_message"]) == ("Failed", "The balance is insufficient")
    # the ack never raises: a line that cannot follow is only logged (claim_state brings it up to date)
    monkeypatch.setattr(env.cp, "on_payout_result",
                        lambda *a: (_ for _ in ()).throw(env.frappe.QueryDeadlockError("deadlock")))
    common.apply_ack(doc, None, "not_sent")
    assert env.frappe.errors[-1][0][1] == "Mpesa B2B Payment Request MB2B-9: payee line not updated"


# ---------------------------------------------------------------- suggest a supplier account
def test_a_till_paid_often_is_suggested_as_a_supplier_account(env):
    lines = [payee("LN-1", 1), payee("LN-2", 2, PAYBILL), payee("LN-3", 3, PHONE)]
    claim(env, lines)
    for i in range(3):  # paid three times from other claims
        old = payee(f"OLD-{i}", status="Paid")
        claim(env, [old], name=f"HR-EXP-OLD{i}")
        env.frappe.db.table(LINE)[old.name]["status"] = "Paid"
    found = env.cp.suggestions([stored(env, n) for n in ("LN-1", "LN-2", "LN-3")], 3)
    assert [(s["line"], s["count"]) for s in found] == [("LN-1", 3)]
    assert found[0]["account"] == {"account_type": TILL, "expected_name": "Shell Kilimani", "shortcode": "835811",
                                   "account_number": None}
    assert "was paid 3 times from Expense Claims: make it an approved Supplier M-Pesa Account" in found[0]["message"]
    assert env.cp.suggestions([stored(env)], 4) == [] and env.cp.suggestions([stored(env)], 0) == []


def test_no_suggestion_once_a_supplier_account_is_approved(env):
    lines = [payee(payee_type=PAYBILL, shortcode="888880", account_number="ACME-1", status="Paid")]
    claim(env, lines)
    env.frappe.db.table(LINE)["LN-1"]["status"] = "Paid"
    assert env.cp.suggestions([stored(env)], 1) == []


# ---------------------------------------------------------------- the drafts
def test_pay_merchants_and_payees_makes_one_request_per_merchant_and_one_batch_for_the_phones(env):
    lines = [payee("LN-1", 1, amount=1000), payee("LN-2", 2, PAYBILL, amount=1500),
             phone_line("LN-3", 3, amount=2000), phone_line("LN-4", 4, phone="0711000444", amount=500)]
    claim(env, lines)
    made = env.cp.pay_payees("HR-EXP-1", b2c_settings="Astral B2C")
    assert (made.b2b, made.b2c, made.lines, made.total) == (["MB2B-1", "MB2B-2"], "MPESA-B2C-3", 4, 5000)
    req = env.frappe.db.table(B2B)["MB2B-2"]
    assert {k: req[k] for k in ("employee", "expense_claim", "claim_payee", "command_id", "receiver_shortcode",
                                "account_reference", "amount", "company", "mpesa_settings")} == {
        "employee": "EMP-1", "expense_claim": "HR-EXP-1", "claim_payee": "LN-2", "command_id": "BusinessPayBill",
        "receiver_shortcode": "647647", "account_reference": "PARK-77", "amount": 1500, "company": "Astral",
        "mpesa_settings": b2b_fixture.SETTINGS}
    assert env.frappe.db.table(B2B)["MB2B-1"]["account_reference"] is None  # a till
    batch = env.frappe.db.table(B2C)["MPESA-B2C-3"]
    assert (batch["transaction_to_pay_against"], batch["party_type"], batch["paid_to"], batch["paid_from"],
            batch["paid_amount"], batch["mode_of_payment"]) == (
        CLAIM, "Employee", "Payable - AIL", b2b_fixture.GL, 2500, None)
    rows = [r for r in env.frappe.db.table(ROW).values() if r.parent == "MPESA-B2C-3"]
    assert [(r.claim_payee, r.partyb, r.allocated_amount, r.party, r.reference_name) for r in rows] == [
        ("LN-3", "254711000333", 2000, "EMP-1", "HR-EXP-1"), ("LN-4", "254711000444", 500, "EMP-1", "HR-EXP-1")]
    assert [stored(env, f"LN-{i}")["status"] for i in range(1, 5)] == ["Queued"] * 4
    assert all(user == "accounts@example.com" and ignored for _dt, user, ignored in env.drafts)  # the preparer's
    assert "prepared M-Pesa payouts for its payees" in env.frappe.comments[-1][2]
    assert env.b2c_checked == [("Astral B2C", 2000, "Astral")]
    # pressed again: nothing left to pay (the drafts are the lines')
    with raises(env, "Nothing to pay"):
        env.cp.pay_payees("HR-EXP-1", b2c_settings="Astral B2C")


def test_only_preparers_make_drafts_of_a_payable_claim_that_has_enough_left(env):
    claim(env, [payee()])
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        env.cp.pay_payees("HR-EXP-1")
    env.frappe.user_roles = {"HR User"}
    env.problem = "HR-EXP-1 is not approved."
    with raises(env, "^HR-EXP-1 is not approved.$"):
        env.cp.pay_payees("HR-EXP-1")
    env.problem, env.left = None, 1500
    with raises(env, "HR-EXP-1 has KES 1,500 left to pay, less than its payees' KES 2,000"):
        env.cp.pay_payees("HR-EXP-1")
    settings(env, allow_claim_payees=0)
    with raises(env, "switched off"):
        env.cp.pay_payees("HR-EXP-1")


def test_retry_makes_new_drafts_for_the_failed_lines_only(env):
    lines = [payee("LN-1", 1), payee("LN-2", 2, shortcode="835812")]
    claim(env, lines)
    request(env, lines[0], name="MB2B-1", docstatus=1, status="Failed", error_message="no float")
    request(env, lines[1], name="MB2B-2", docstatus=1, status="Paid")
    with raises(env, "Nothing to pay"):
        env.cp.pay_payees("HR-EXP-1")
    made = env.cp.retry_payees("HR-EXP-1")
    assert [env.frappe.db.table(B2B)[n]["claim_payee"] for n in made.b2b] == ["LN-1"]
    assert (stored(env, "LN-1")["status"], stored(env, "LN-1")["payout_name"]) == ("Queued", made.b2b[0])
    with raises(env, "No payee line has failed"):
        env.cp.retry_payees("HR-EXP-1")


def test_the_list_pays_the_phone_payees_of_several_claims_in_one_batch(env):
    claim(env, [phone_line("LN-1"), payee("LN-2", 2)], name="HR-EXP-1")
    claim(env, [phone_line("LN-3", phone="0711000555")], name="HR-EXP-2", employee="EMP-2")
    made = env.cp.pay_claims(json.dumps(["HR-EXP-2", "HR-EXP-1"]), "Astral B2C")
    rows = sorted((r.claim_payee, r.party, r.reference_name) for r in env.frappe.db.table(ROW).values())
    assert (made.b2b, made.lines, rows) == ([], 2, [("LN-1", "EMP-1", "HR-EXP-1"), ("LN-3", "EMP-2", "HR-EXP-2")])
    assert stored(env, "LN-2")["status"] == "Draft"  # merchants are paid from the claim's form


@pytest.mark.parametrize("kw, msg", [
    ({"company": "Other Co"}, "one company at a time"),
    ({"payable_account": "Other Payable - AIL"}, "one Payable Account to be paid in one batch"),
])
def test_one_batch_is_one_company_and_one_payable_account(env, kw, msg):
    claim(env, [phone_line("LN-1")], name="HR-EXP-1")
    claim(env, [phone_line("LN-3", phone="0711000555")], name="HR-EXP-2", **kw)
    with raises(env, msg):
        env.cp.pay_claims(["HR-EXP-1", "HR-EXP-2"], "Astral B2C")


def test_the_shortcode_is_the_picked_one_or_the_companys_only_one(env):
    cp = env.cp
    assert cp.pick_settings(None, "use_b2b", "Astral") == b2b_fixture.SETTINGS
    with raises(env, "does not have B2C Payouts ticked"):
        cp.pick_settings(b2b_fixture.SETTINGS, "use_b2c", "Astral")
    with raises(env, "pays for Astral, not Other Co"):
        cp.pick_settings("Astral B2C", "use_b2c", "Other Co")
    with raises(env, "Pick Pay From \\(B2C\\): Other Co has no shortcode"):
        cp.pick_settings(None, "use_b2c", "Other Co")


# ---------------------------------------------------------------- B2C's guards for a claim paid in parts
def test_a_payee_claims_batch_counts_its_other_payouts_in_the_amount(payout, monkeypatch):  # noqa: F811
    # a merchant's request (a draft, merged with money_out.owed's B2B count), or another batch, is
    # open for the claim: its casual payees are paid all the same, within what it owes
    employee_documents = importlib.import_module("mpsa_suite.money_out.employee_documents")
    monkeypatch.setattr(employee_documents, "payable_problem", lambda doc, doctype=None: None, raising=False)
    ec = {"reference_doctype": CLAIM, "reference_name": "HR-EXP-1"}
    payout.open = [{"parent": "MB2B-1", "allocated_amount": 2000, "request_status": "Not Initiated"}]
    payout.owed = 4300
    with pytest.raises(payout.frappe.ValidationError, match="in progress or awaiting accounting: MB2B-1"):
        payout.disbursement("D-NEW", {"allocated_amount": 2300, **ec}).validate_no_double_payout()  # no payees
    payout.frappe.db.table(LINE)["LN-1"] = {"name": "LN-1", "parent": "HR-EXP-1", "parenttype": CLAIM}
    payout.disbursement("D-NEW", {"allocated_amount": 2300, **ec}).validate_no_double_payout()
    with pytest.raises(payout.frappe.ValidationError, match="HR-EXP-1 owes 4300.0, less MB2B-1 Not Initiated 2,000; "
                                                             "this disbursement allocates 2400.0"):
        payout.disbursement("D-NEW", {"allocated_amount": 2400, **ec}).validate_no_double_payout()
    # an invoice is not paid in parts: refused as before
    with pytest.raises(payout.frappe.ValidationError, match="Purchase Invoice PI-1 already has a payout in progress"):
        payout.disbursement("D-NEW", {"allocated_amount": 500}).validate_no_double_payout()


def test_a_payee_claims_retry_counts_its_other_payouts_in_the_amount(retry, monkeypatch):  # noqa: F811
    # the claim's and the employee's own retry checks (WP3) are tested in test_employee_documents.py
    monkeypatch.setattr(importlib.import_module("mpsa_suite.money_out.employee_documents"), "check_b2c_retry",
                        lambda doc: None)
    request = retry.failed(amount=1000, doctype=CLAIM, reference="HR-EXP-1")
    retry.others = [{"parent": "MPESA-B2C-2", "allocated_amount": 800, "request_status": "Paid"}]
    retry.owed = 1500
    with pytest.raises(fake_frappe.ValidationError, match="awaiting accounting: MPESA-B2C-2"):
        request.retry_failed_payment()  # no payee lines
    retry.frappe.db.table(LINE)["LN-1"] = {"name": "LN-1", "parent": "HR-EXP-1", "parenttype": CLAIM}
    with pytest.raises(fake_frappe.ValidationError,
                       match="HR-EXP-1 owes 1500.0; retrying would pay 1000.0, and 800.0 is still being paid"):
        request.retry_failed_payment()
    retry.owed = 1800
    request.retry_failed_payment()
    assert [s[0] for s in retry.sends] == ["B2C-1"]


# ---------------------------------------------------------------- casual payees: paste or upload
def test_pasted_rows_by_tab_or_comma_with_or_without_a_header(env):
    cp = env.cp
    rows = cp.table_rows(cp.parse_text("Jane Wanjiku\t0711000333\t1,500\n\nJohn Otieno, 711000444, \"1,200\"\n"))
    # round 4.5 review: the blank line keeps its number; a comma-split amount is quoted (else never guessed)
    assert [(r.row, r.payee_name, r.phone, r.amount_text) for r in rows] == [
        (1, "Jane Wanjiku", "0711000333", "1,500"), (3, "John Otieno", "711000444", "1,200")]
    rows = cp.table_rows([["Amount", "Phone Number", "Payee Name"], [1500.0, 711000333, "Jane"]])
    # a spreadsheet's whole number comes without its ".0" (round 4.4)
    assert [(r.row, r.payee_name, r.phone, r.amount_text) for r in rows] == [(2, "Jane", "711000333", "1500")]
    assert cp.parse_amount("KES 1,500") == 1500 and cp.parse_amount("Ksh. 20") == 20 and cp.parse_amount("x") is None


def test_each_pasted_row_is_checked(env):
    cp = env.cp
    rows = cp.table_rows(cp.parse_text("\n".join([
        "Jane, 0711000333, 1500",
        "No Phone, 12345, 100",
        "Airtel, 0733111222, 100",
        "Zero, 0711000555, 0",
        "Cents, 0711000556, 10.50",
        ", 0711000557, 100",
        "Claimant, 0722000111, 100",
        "Colleague, 0733000222, 100",
        "Again, 0711000333, 100",
        "On The Claim, 0711000999, 100",
    ])))
    checked = cp.check_pasted(rows, "EMP-1", ["254711000999"], cp.employee_destinations())
    problems = {r["payee_name"] or "(no name)": "; ".join(r["errors"]) for r in checked}
    assert problems["Jane"] == "" and checked[0]["phone"] == "254711000333" and checked[0]["amount"] == 1500
    assert "not a Kenyan mobile number" in problems["No Phone"]
    assert "does not start like a Safaricom number" in problems["Airtel"]
    assert "more than 0" in problems["Zero"] and "whole shillings" in problems["Cents"]
    assert problems["(no name)"] == "no name"
    assert "the claimant's own phone" in problems["Claimant"]
    assert "an employee's phone" in problems["Colleague"]
    assert "earlier in the list" in problems["Again"] and "on the claim already" in problems["On The Claim"]


def test_parse_payees_needs_write_on_the_claim_or_create_before_its_first_save(env):
    claim(env, [])
    answer = env.cp.parse_payees(text="Jane, 0711000333, 1500\nBad, 1, 1", claim="HR-EXP-1")
    assert (answer["good"], answer["bad"]) == (1, 1)
    env.denied.add((CLAIM, "write"))
    with pytest.raises(env.frappe.PermissionError):
        env.cp.parse_payees(text="Jane, 0711000333, 1500", claim="HR-EXP-1")
    assert env.cp.parse_payees(text="Jane, 0711000333, 1500", claim="new-expense-claim-1")["good"] == 1
    env.denied.add((CLAIM, "create"))
    with pytest.raises(env.frappe.PermissionError):
        env.cp.parse_payees(text="Jane, 0711000333, 1500")


# ---------------------------------------------------------------- after submit, cancel, the form
def test_a_save_after_submit_keeps_the_servers_status_and_result(env):
    line = payee()
    doc = claim(env, [line])
    env.frappe.db.table(LINE)["LN-1"].update(status="Paid", receipt="RCPT1")
    edited = _dict(stored(env), status="Draft", receipt=None)
    env.cp.keep_results(_dict(doc, mpesa_payees=[edited]))
    assert (edited.status, edited.receipt) == ("Paid", "RCPT1")


def test_a_cancelled_claims_unpaid_lines_are_cancelled(env):
    lines = [payee("LN-1", 1), payee("LN-2", 2, status="Queued")]
    doc = claim(env, lines)
    env.cp.on_cancel_claim(doc)
    assert [stored(env, n)["status"] for n in ("LN-1", "LN-2")] == ["Cancelled", "Cancelled"]


def test_the_form_state_brings_the_lines_up_to_date(env, hrms_modules):
    line = payee()
    claim(env, [line])
    request(env, line, name="MB2B-1", docstatus=1, status="Initiated")
    env.frappe.get_doc = lambda doctype, name=None, **kw: fake_frappe.FakeDoc(env.frappe, doctype, _dict(
        env.frappe.db.table(doctype)[name], mpesa_payees=[_dict(r) for r in env.frappe.db.table(LINE).values()
                                                          if r.parent == name]))
    hrms_stub(env, booked=0)
    state = env.cp.claim_state("HR-EXP-1")
    assert state["changed"] and state["enabled"] and state["totals"]["In Flight"] == 2000
    assert state["lines"][0]["status"] == "In Flight" and state["reimbursed"] is None
    assert env.cp.claim_state("new-expense-claim-1") == {"enabled": True, "changed": False, "totals": {}, "lines": [],
                                                          "suggestions": []}


# ---------------------------------------------------------------- the reimbursed total
def hrms_stub(env, booked):
    module = types.ModuleType("hrms.hr.doctype.expense_claim.expense_claim")
    module.get_total_reimbursed_amount = lambda doc: booked
    module.update_reimbursed_amount = lambda doc: env.frappe.db.table(CLAIM)[doc.name].update(
        total_amount_reimbursed=booked)
    for name in ("hrms", "hrms.hr", "hrms.hr.doctype", "hrms.hr.doctype.expense_claim"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules[module.__name__] = module
    return module


@pytest.fixture
def hrms_modules():
    saved = {k: v for k, v in sys.modules.items() if k == "hrms" or k.startswith("hrms.")}
    yield
    for k in [k for k in sys.modules if k == "hrms" or k.startswith("hrms.")]:
        sys.modules.pop(k)
    sys.modules.update(saved)


def test_repair_sets_the_reimbursed_total_from_the_booked_entries(env, hrms_modules):
    claim(env, [], total_amount_reimbursed=401)
    hrms_stub(env, booked=501)
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.cp.repair_reimbursed("HR-EXP-1")
    env.frappe.user_roles = {"M-Pesa Manager"}
    assert env.cp.repair_reimbursed("HR-EXP-1") == {"repaired": True, "from": 401, "to": 501}
    assert env.frappe.db.table(CLAIM)["HR-EXP-1"]["total_amount_reimbursed"] == 501
    assert "repaired from 401 to 501" in env.frappe.comments[-1][2]
    assert env.cp.repair_reimbursed("HR-EXP-1") == {"repaired": False, "total": 501}


# ---------------------------------------------------------------- wiring
def _hooks() -> dict:
    import ast

    tree = ast.parse((APP / "hooks.py").read_text(encoding="utf-8"))
    return {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id in (
                "doctype_js", "doctype_list_js", "doc_events")}


def test_the_claims_scripts_and_events_are_wired():
    hooks = _hooks()
    assert hooks["doctype_js"]["Expense Claim"] == ["public/js/expense_claim.js", "public/js/expense_claim_payees.js"]
    assert hooks["doctype_list_js"]["Expense Claim"] == ["public/js/expense_claim_list.js",
                                                         "public/js/expense_claim_payees_list.js"]
    events = hooks["doc_events"]["Expense Claim"]
    assert events["validate"] == "mpsa_suite.money_out.claim_payees.validate_claim"
    assert events["before_update_after_submit"] == "mpsa_suite.money_out.claim_payees.keep_results"
    assert events["on_cancel"] == "mpsa_suite.money_out.claim_payees.on_cancel_claim"
    assert events["before_cancel"] == "mpsa_suite.money_out.employee_documents.before_cancel"


def _doctype(folder):
    return json.loads((APP / "mpsa_suite" / "doctype" / folder / f"{folder}.json").read_text(encoding="utf-8"))


def test_the_payee_line_doctype_freezes_its_details_and_lets_the_server_write_its_result(env):
    data = _doctype("m_pesa_claim_payee")
    assert (data["name"], data["istable"], data["module"], data["permissions"]) == (LINE, 1, "Mpsa Suite", [])
    fields = {f["fieldname"]: f for f in data["fields"]}
    assert set(data["field_order"]) == set(fields)
    assert fields["payee_type"]["options"].split("\n") == [TILL, PAYBILL, BANK, PHONE]
    assert fields["status"]["options"].split("\n") == ["Draft", "Queued", "In Flight", "Paid", "Failed", "Cancelled"]
    server = set(env.cp.RESULT_FIELDS)
    for name, field in fields.items():
        if field["fieldtype"] in ("Section Break", "Column Break", "HTML"):
            continue
        if name in server:
            assert field.get("allow_on_submit") and field.get("read_only") and field.get("no_copy"), name
        else:
            assert not field.get("allow_on_submit"), name
    assert fields["bank_paybill"]["options"] == BANK_PAYBILL
    assert fields["destination_key"]["search_index"] == 1
    assert set(env.cp.LINE_FIELDS) - {"parent", "parenttype"} <= set(fields) | {"name", "idx"}


def test_the_line_names_its_payout_and_supplier_account_without_linking_them():
    # A submitted claim's line is docstatus 1: a Link or Dynamic Link from it would stop its payout (and the
    # supplier account) being cancelled or deleted (Frappe's back-link checks), and an amend copying a stale
    # value would fail "Could not find" / "Cannot link cancelled document" before validate clears it.
    data = _doctype("m_pesa_claim_payee")
    fields = {f["fieldname"]: f for f in data["fields"]}
    for name in ("payout_name", "supplier_account"):
        assert fields[name]["fieldtype"] == "Data" and "options" not in fields[name], name
        assert fields[name]["read_only"] and fields[name]["no_copy"], name
    linked = {f["options"] for f in data["fields"] if f["fieldtype"] in ("Link", "Dynamic Link")}
    assert linked == {"DocType", TYPE, BANK_PAYBILL}  # none is submittable
    assert data["modified"] > "2026-10-27 21:00:00"  # newer than WP4's: the migrate changes the field types
    js = (APP / "public" / "js" / "expense_claim_payees.js").read_text(encoding="utf-8")
    assert "get_form_link(line.payout_doctype, line.payout_name, true)" in js
    assert 'get_form_link("Supplier M-Pesa Account", line.supplier_account, true)' in js


@pytest.mark.parametrize("folder", ["mpesa_b2b_payment_request", "b2c_payment_disbursement_reference"])
def test_the_payouts_name_the_line_they_pay_in_a_last_field(folder):
    data = _doctype(folder)
    assert data["field_order"][-1] == "claim_payee" and data["fields"][-1]["fieldname"] == "claim_payee"
    field = data["fields"][-1]
    assert (field["fieldtype"], field["read_only"], field["search_index"]) == ("Data", 1, 1)
    assert data["modified"] >= "2026-10-27 21:00:00"  # newer than the site's: the migrate syncs it


def test_the_claim_and_type_fields_are_made_on_every_migrate(env, monkeypatch):
    custom_field = types.ModuleType("frappe.custom.doctype.custom_field.custom_field")
    custom_field.create_custom_fields = lambda fields, update=False: None
    for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.custom_field"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, custom_field.__name__, custom_field)
    migrate = importlib.import_module("mpsa_suite.mpsa_suite.migrate")
    made = {}
    monkeypatch.setattr(migrate, "create_fields", lambda fields: made.update(fields))
    migrate.create_claim_payee_fields()
    claim_fields = {f["fieldname"]: f for f in made["Expense Claim"]}
    assert claim_fields["mpesa_payees_section"]["insert_after"] == "expenses"  # a section follows it in v15 and v16
    assert claim_fields["mpesa_payees_section"]["hidden"] == 1  # shown by the form while the switch is on
    assert claim_fields["mpesa_payees"]["options"] == LINE
    assert made["Expense Claim Type"][0]["fieldname"] == "allow_mpesa_payees"
    source = (APP / "mpsa_suite" / "migrate.py").read_text(encoding="utf-8")
    assert "create_custom_hrms_fields()\n        create_claim_payee_fields()" in source
