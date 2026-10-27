"""Payee accounts, round 4 (money_out.party_accounts): many approved destinations per party.

Covers: the four account types on both DocTypes (cleaning, required and cleared fields, the
Safaricom warning, the company's own numbers, a bank's paybill typed as a plain Paybill, the bank
copy and its account number pattern); the destination key and the uniqueness table across both
DocTypes, with an employee named only to who may see salaries; the defaults D1-D5 (promotion at a
second approval, the cancel rules, Make Default's roles, a blocked account never the default);
picking the account of a channel; B2B to a paybill, till or bank account for both parties and a
phone refused; the number and name check after payment (the KCB string, a wrong number, an
en-dash, a bank with no name yet, a till); a bank paybill change blocking an account; the approval
by a named user only; the migrate step. The schedule and payroll routes are at the end, on the
in-memory site of test_payroll_schedule.
"""

import datetime
import importlib
import json
from pathlib import Path

import pytest

import fake_frappe
from test_payroll_schedule import (  # noqa: F401
    EMPACC,
    SETTINGS,
    SLIP,
    bank_line,
    env as site,
    new_form,
    schedule_row,
)

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)
SUITE = "M-Pesa Suite Settings"
SUP, EMP = "Supplier M-Pesa Account", "Employee M-Pesa Account"
BANKS = "M-Pesa Bank Paybill"
B2B = "Mpesa B2B Payment Request"
PHONE, PAYBILL, TILL, BANK = "Phone (B2C)", "Paybill", "Till (Buy Goods)", "Bank Account (via Bank Paybill)"
PARTY = {SUP: ("supplier", "Total Kenya"), EMP: ("employee", "EMP-1")}


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    # every approval here is the confirmed Approve step (send_confirm.deliberate); the step itself is
    # tested in test_send_confirm.py and test_payee_accounts.py
    frappe.flags["mpsa_deliberate"] = [("Approve (unit test)", None)]
    try:
        frappe.db.table(SUITE)[SUITE] = {"supplier_account_hold_hours": 24, "employee_account_hold_hours": 48,
                                         "allow_one_off_b2b": 0}
        frappe.db.table("Mpesa Settings")["Astral 600999"] = {
            "name": "Astral 600999", "business_shortcode": "600999", "till_number": "5123456",
        }
        frappe.db.table(BANKS)["KCB"] = {
            "name": "KCB", "code": "KCB", "institution_name": "KCB Bank Kenya Limited", "paybill": "522522",
            "enabled": 1, "bank": None, "receiver_name": "KCB Paybill AC", "account_number_pattern": r"\d{13}",
            "account_number_rule": "The 13-digit KCB account number.", "observed_receiver_name": None}
        frappe.db.table(BANKS)["EQUITY-BANK"] = {
            "name": "EQUITY-BANK", "code": "EQUITY-BANK", "institution_name": "Equity Bank Kenya Limited",
            "paybill": "247247", "enabled": 1, "bank": "Equity", "receiver_name": None, "account_number_pattern": None,
            "observed_receiver_name": None}
        frappe.db.table(BANKS)["OLD-SACCO"] = {
            "name": "OLD-SACCO", "code": "OLD-SACCO", "institution_name": "Old Sacco", "paybill": "333444",
            "enabled": 0, "receiver_name": None}
        frappe.db.table("Employee")["EMP-1"] = {"name": "EMP-1", "employee_name": "Jane Wanjiru", "user_id": None}
        frappe.db.table("Employee")["EMP-2"] = {"name": "EMP-2", "employee_name": "Otieno Ouma", "user_id": None}
        frappe.db.table("Supplier")["Total Kenya"] = {"name": "Total Kenya", "supplier_name": "Total Kenya"}
        frappe.db.table("Supplier")["Acme"] = {"name": "Acme", "supplier_name": "Acme Hardware"}
        frappe.user_roles = {"HR Manager", "Accounts Manager"}
        pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        sa = importlib.import_module("mpsa_suite.money_out.supplier_accounts")
        ea = importlib.import_module("mpsa_suite.money_out.employee_accounts")
        alerts = []
        pa_alert = pa.alert
        pa.alert = lambda message, doc, roles=None: alerts.append((doc.name, message, roles))
        yield fake_frappe._dict(frappe=frappe, pa=pa, sa=sa, ea=ea, alerts=alerts)
        pa.alert = pa_alert
    finally:
        fake_frappe.uninstall(saved)


def stored(env, doctype, name, **kw):
    """An approved account, written as the database holds it."""
    field, party = PARTY[doctype]
    row = {"name": name, "docstatus": 1, field: party, "account_type": PAYBILL, "phone": None, "shortcode": "888880",
           "account_number": "TK-001", "bank_paybill": None, "bank": None, "account_holder_name": None,
           "expected_name": "Total Kenya", "active_from": NOW - datetime.timedelta(hours=1), "is_default": 0,
           "name_mismatch": 0, "needs_reapproval": 0, "owner": "clerk@example.com", "proof": "/files/p.pdf",
           "creation": f"2026-10-0{len(env.frappe.db.table(doctype)) + 1}"}
    row.update(kw)
    row["destination_key"] = env.pa.destination_key(row)
    env.frappe.db.table(doctype)[name] = row
    return row


def draft(doctype, **kw):
    field, party = PARTY[doctype]
    row = {"name": None, "docstatus": 0, field: party, "account_type": PAYBILL, "phone": None, "shortcode": "888880",
           "account_number": "TK-001", "bank_paybill": None, "expected_name": "Total Kenya", "owner": "clerk@example.com",
           "proof": "/files/p.pdf"}
    row.update(kw)
    return row


def new(env, doctype, **kw):
    return env.frappe.get_doc(dict(draft(doctype, **kw), doctype=doctype))


def bank(doctype, **kw):
    return dict(account_type=BANK, bank_paybill="KCB", account_number="1234 567-890 123", shortcode=None,
                expected_name=None, **kw)


BOTH = pytest.mark.parametrize("doctype", [SUP, EMP])


# ---------------------------------------------------------------- the four types on both DocTypes
@BOTH
def test_both_doctypes_take_the_four_types_and_have_every_field_the_code_reads(env, doctype):
    folder = doctype.lower().replace(" ", "_").replace("-", "_")
    path = Path(__file__).resolve().parents[2] / "mpsa_suite" / "mpsa_suite" / "doctype" / folder / f"{folder}.json"
    fields = {f["fieldname"]: f for f in json.loads(path.read_text(encoding="utf-8"))["fields"]}
    assert fields["account_type"]["options"].split("\n") == list(env.pa.ALL_TYPES)
    assert set(env.pa.PROFILES[doctype].fields) - {"name", "docstatus"} <= set(fields)
    assert fields["bank_paybill"]["options"] == BANKS and fields["bank"]["fetch_from"] == "bank_paybill.bank"
    assert fields["is_default"]["read_only_depends_on"] == "eval:doc.docstatus==1"
    # the block and the last payment are written by the code (frappe.db.set_value) only: not Allow on
    # Submit, so a save after approval cannot clear them (Frappe does not enforce read only there)
    for name in env.pa.SET_BY_THE_SUITE:
        assert (fields[name]["read_only"], fields[name].get("allow_on_submit"), fields[name]["no_copy"]) == (1, None, 1)
        assert fields[name]["label"] == env.pa.SET_BY_THE_SUITE[name]
    assert fields["destination_key"]["search_index"] == 1 and fields["destination_key"]["hidden"] == 1


@BOTH
def test_a_phone_is_kept_as_2547_and_drops_the_number_fields(env, doctype):
    doc = new(env, doctype, account_type=PHONE, phone="0722 000 111", shortcode="888880", account_number="X",
              bank_paybill="KCB", expected_name="Jane Wanjiru")
    env.pa.validate_account(doc)
    assert (doc.phone, doc.shortcode, doc.account_number, doc.bank_paybill) == ("254722000111", None, None, None)
    assert doc.destination_key == "B2C:254722000111"
    assert not env.frappe.messages


@BOTH
def test_a_phone_that_is_not_safaricom_is_warned_not_refused(env, doctype):
    doc = new(env, doctype, account_type=PHONE, phone="0733 000 111", expected_name="Jane Wanjiru")
    env.pa.validate_account(doc)  # numbers moved to Safaricom keep their old prefix
    assert any("does not start like a Safaricom number" in m for m in env.frappe.messages)


@BOTH
@pytest.mark.parametrize("kw, msg", [
    ({"shortcode": "88A880"}, "Paybill / Till Number must be 5 to 7 digits"),
    ({"account_number": None}, "A paybill needs the Account Number"),
    ({"shortcode": "600999"}, "own M-Pesa numbers"),
    ({"account_type": TILL, "shortcode": "5123456"}, "own M-Pesa numbers"),
    ({"expected_name": None}, "Enter the name M-Pesa shows"),
    ({"shortcode": "522522", "account_number": "1234567890123"},
     "522522 is KCB Bank Kenya Limited's bank paybill: pick Account Type Bank Account \\(via Bank Paybill\\) and KCB"),
])
def test_bad_paybill_or_till_details_are_refused(env, doctype, kw, msg):
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.pa.validate_account(new(env, doctype, **kw))


@BOTH
def test_a_disabled_banks_paybill_may_be_a_plain_paybill_and_a_till_drops_its_account(env, doctype):
    doc = new(env, doctype, shortcode=" 333444 ", account_number=" M-1 ")
    env.pa.validate_account(doc)
    assert (doc.shortcode, doc.account_number, doc.destination_key) == ("333444", "M-1", "PB:333444:M1")
    doc = new(env, doctype, account_type=TILL, shortcode="835811", account_number="X", phone="0722000111")
    env.pa.validate_account(doc)
    assert (doc.account_number, doc.phone, doc.destination_key) == (None, None, "TILL:835811")


@BOTH
def test_a_bank_account_copies_its_bank_and_keeps_the_number_plain(env, doctype):
    doc = new(env, doctype, **bank(doctype, phone="0722000111"))
    env.pa.validate_account(doc)
    assert (doc.shortcode, doc.account_number, doc.phone) == ("522522", "1234567890123", None)
    assert doc.expected_name == "KCB Paybill AC"  # Safaricom's name for the bank, not the holder
    assert doc.destination_key == "PB:522522:1234567890123"
    party_name = "Total Kenya" if doctype == SUP else "Jane Wanjiru"
    assert doc.account_holder_name == party_name


@BOTH
def test_a_bank_account_checks_the_banks_pattern_and_that_the_bank_is_on(env, doctype):
    with pytest.raises(env.frappe.ValidationError,
                       match="12345 does not look like a KCB Bank Kenya Limited account number \\(The 13-digit KCB"):
        env.pa.validate_account(new(env, doctype, **dict(bank(doctype), account_number="12345")))
    with pytest.raises(env.frappe.ValidationError, match="Old Sacco is switched off in M-Pesa Bank Paybills"):
        env.pa.validate_account(new(env, doctype, **dict(bank(doctype), bank_paybill="OLD-SACCO")))


@BOTH
def test_a_collection_reference_at_a_bank_paybill_stays_a_paybill_with_a_warning(env, doctype):
    # "Lipa na I&M": a business's code at I&M's paybill, then the payer's reference (the research of
    # 2026-10-27); not an account at the bank, whose 14-digit pattern it could never fit
    env.frappe.db.table(BANKS)["IM-BANK"] = {
        "name": "IM-BANK", "code": "IM-BANK", "institution_name": "I&M Bank Limited", "paybill": "542542",
        "enabled": 1, "bank": None, "receiver_name": None, "account_number_pattern": r"\d{14}",
        "account_number_rule": "The full 14-digit I&M account number.", "observed_receiver_name": None}
    doc = new(env, doctype, shortcode="542542", account_number=" 888888#house10 ", expected_name="I&M Bank")
    env.pa.validate_account(doc)
    assert (doc.account_type, doc.shortcode, doc.account_number) == (PAYBILL, "542542", "888888#house10")
    assert doc.destination_key == "PB:542542:888888#HOUSE10"
    assert any("888888#house10 is a business's collection reference there (code#reference), so this stays a "
               "Paybill" in m for m in env.frappe.messages)
    # typed as a bank account, it is pointed to the Paybill type
    with pytest.raises(env.frappe.ValidationError,
                       match="888888#house10 is a business's collection reference \\(code#reference\\): pick Account "
                             "Type Paybill, with Paybill 542542"):
        env.pa.validate_account(new(env, doctype, **dict(bank(doctype), bank_paybill="IM-BANK",
                                                         account_number="888888#house10")))
    # a plain account number at I&M is still an account at the bank
    with pytest.raises(env.frappe.ValidationError,
                       match="542542 is I&M Bank Limited's bank paybill: pick Account Type Bank Account"):
        env.pa.validate_account(new(env, doctype, shortcode="542542", account_number="12345678901234",
                                    expected_name="I&M Bank"))
    env.pa.validate_account(new(env, doctype, **dict(bank(doctype), bank_paybill="IM-BANK",
                                                     account_number="1234 5678 9012 34")))


@BOTH
def test_a_third_partys_bank_account_is_warned(env, doctype):
    doc = new(env, doctype, **bank(doctype, account_holder_name="John Kamau"))
    env.pa.validate_account(doc)
    assert any("a third party's account" in m for m in env.frappe.messages)


def test_an_employee_paid_to_a_business_number_is_warned(env):
    env.pa.validate_account(new(env, EMP, account_type=TILL, shortcode="835811"))
    assert any("pays an employee to a business paybill or till" in m for m in env.frappe.messages)
    env.frappe.messages.clear()
    env.pa.validate_account(new(env, SUP, account_type=TILL, shortcode="835811"))
    assert not env.frappe.messages


@pytest.mark.parametrize("row, key", [
    ({"account_type": PAYBILL, "shortcode": "888880", "account_number": "tk 00-1"}, "PB:888880:TK001"),
    ({"account_type": BANK, "shortcode": "522522", "account_number": "0123 456/789"}, "PB:522522:0123456789"),
    ({"account_type": "Bank Paybill (B2B)", "shortcode": "247247", "account_number": "01-23"}, "PB:247247:0123"),
    ({"account_type": TILL, "shortcode": "835811", "account_number": "x"}, "TILL:835811"),
    ({"account_type": PHONE, "phone": "254722000111"}, "B2C:254722000111"),
    ({"account_type": PAYBILL, "shortcode": ""}, None),
])
def test_the_destination_key(env, row, key):
    assert env.pa.destination_key(row) == key


# ---------------------------------------------------------------- the same destination twice
@BOTH
def test_the_same_destination_for_the_same_party_is_refused_however_it_is_typed(env, doctype):
    stored(env, doctype, "ACC-1", account_number="TK-001")
    with pytest.raises(env.frappe.ValidationError, match="already has an approved account with these details"):
        env.pa.validate_account(new(env, doctype, account_number="tk 001"))


def test_two_employees_never_share_a_destination(env):
    stored(env, EMP, "MEA-1", account_type=PHONE, phone="254722000111", shortcode=None, account_number=None)
    stored(env, EMP, "MEA-2", account_type=TILL, shortcode="835811", account_number=None)
    for kw, what in (({"account_type": PHONE, "phone": "0722000111"}, "phone"),
                     ({"account_type": TILL, "shortcode": "835811"}, "till")):
        with pytest.raises(env.frappe.ValidationError, match=f"The same {what} is already approved for EMP-1"):
            env.pa.validate_account(new(env, EMP, employee="EMP-2", **kw))


def test_two_suppliers_sharing_a_number_are_warned(env):
    stored(env, SUP, "MSA-1", supplier="Acme")
    env.pa.validate_account(new(env, SUP))
    assert any("The same paybill and account number is already approved for Acme" in m for m in env.frappe.messages)


@pytest.mark.parametrize("kw", [
    {"account_type": PHONE, "phone": "254722000111", "shortcode": None, "account_number": None},
    {"account_type": BANK, "bank_paybill": "KCB", "shortcode": "522522", "account_number": "1234567890123"},
])
def test_a_supplier_and_an_employee_never_share_a_phone_or_a_bank_account(env, kw):
    stored(env, EMP, "MEA-1", **kw)
    typed = dict(bank(SUP)) if kw["account_type"] == BANK else {"account_type": PHONE, "phone": "0722000111",
                                                                 "expected_name": "Jane"}
    with pytest.raises(env.frappe.ValidationError, match="how a fake supplier looks"):
        env.pa.validate_account(new(env, SUP, **typed))
    stored(env, SUP, "MSA-1", **kw)
    with pytest.raises(env.frappe.ValidationError, match="already approved for EMP-1, Total Kenya. A supplier and"):
        env.pa.validate_account(new(env, EMP, employee="EMP-2", **typed))


def test_a_supplier_and_an_employee_may_share_a_paybill_with_a_warning(env):
    stored(env, EMP, "MEA-1")
    env.pa.validate_account(new(env, SUP))
    assert any("already approved for EMP-1" in m for m in env.frappe.messages)


def test_the_employee_is_named_only_to_who_may_see_salaries(env):
    stored(env, EMP, "MEA-1", account_type=PHONE, phone="254722000111", shortcode=None, account_number=None)
    env.frappe.user_roles = {"Purchase Manager"}  # enters supplier accounts, sees no salaries
    with pytest.raises(env.frappe.ValidationError) as caught:
        env.pa.validate_account(new(env, SUP, account_type=PHONE, phone="0722000111", expected_name="X"))
    assert "an employee" in str(caught.value) and "EMP-1" not in str(caught.value)


def test_a_shared_bank_paybill_with_other_account_numbers_is_no_duplicate(env):
    stored(env, EMP, "MEA-1", account_type=BANK, bank_paybill="KCB", shortcode="522522", account_number="1234567890123")
    env.pa.validate_account(new(env, EMP, employee="EMP-2", **dict(bank(EMP), account_number="9999999999999",
                                                                   account_holder_name="Otieno Ouma")))
    env.pa.validate_account(new(env, SUP, **dict(bank(SUP), account_number="5555555555555")))
    assert not env.frappe.messages


# ---------------------------------------------------------------- defaults (D1-D5)
def doc_of(env, doctype, name):
    return env.frappe.get_doc(doctype, name)


def versions(env):
    return [(r["docname"], r["data"]) for r in env.frappe.db.table("Version").values()]


@BOTH
def test_a_second_approved_account_keeps_the_first_as_default(env, doctype):
    stored(env, doctype, "ACC-1")
    env.pa.after_approve(doc_of(env, doctype, "ACC-1"))  # the only one: nothing is ticked
    assert env.frappe.db.table(doctype)["ACC-1"]["is_default"] == 0
    stored(env, doctype, "ACC-2", shortcode="111111")
    env.pa.after_approve(doc_of(env, doctype, "ACC-2"))
    table = env.frappe.db.table(doctype)
    assert (table["ACC-1"]["is_default"], table["ACC-2"]["is_default"]) == (1, 0)
    assert [v[0] for v in versions(env)] == ["ACC-1"] and '"is_default", 0, 1' in versions(env)[0][1]
    assert {c[1] for c in env.frappe.comments} == {"ACC-1", "ACC-2"}
    stored(env, doctype, "ACC-3", shortcode="222222")
    env.pa.after_approve(doc_of(env, doctype, "ACC-3"))  # a default exists: nothing moves
    assert [table[n]["is_default"] for n in ("ACC-1", "ACC-2", "ACC-3")] == [1, 0, 0]


@BOTH
def test_an_account_approved_as_default_takes_over_and_a_blocked_one_is_never_promoted(env, doctype):
    stored(env, doctype, "ACC-1", is_default=1)
    stored(env, doctype, "ACC-2", shortcode="111111", is_default=1)
    env.pa.after_approve(doc_of(env, doctype, "ACC-2"))
    table = env.frappe.db.table(doctype)
    assert (table["ACC-1"]["is_default"], table["ACC-2"]["is_default"]) == (0, 1)
    stored(env, doctype, "ACC-3", shortcode="333333", name_mismatch=1, **{PARTY[doctype][0]: "P2"})
    stored(env, doctype, "ACC-4", shortcode="444444", **{PARTY[doctype][0]: "P2"})
    env.pa.after_approve(doc_of(env, doctype, "ACC-4"))
    assert table["ACC-3"]["is_default"] == 0  # blocked by a name mismatch (D3)


@BOTH
def test_cancelling_leaves_the_last_account_the_default_and_never_guesses_between_two(env, doctype):
    stored(env, doctype, "ACC-1", is_default=1)
    stored(env, doctype, "ACC-2", shortcode="111111")
    table = env.frappe.db.table(doctype)
    table["ACC-1"].update(docstatus=2, is_default=0)
    env.pa.after_cancel(doc_of(env, doctype, "ACC-1"))
    assert table["ACC-2"]["is_default"] == 1
    stored(env, doctype, "ACC-3", shortcode="333333")
    table["ACC-2"].update(docstatus=2, is_default=0)
    stored(env, doctype, "ACC-4", shortcode="444444")
    env.pa.after_cancel(doc_of(env, doctype, "ACC-2"))
    assert (table["ACC-3"]["is_default"], table["ACC-4"]["is_default"]) == (0, 0)
    party = PARTY[doctype][1]
    assert env.pa.pick_account(doctype, party) is None and env.pa.default_account(doctype, party) is None


@pytest.mark.parametrize("doctype, roles, allowed", [
    (SUP, {"Accounts User"}, True), (SUP, {"Purchase Manager"}, True), (SUP, {"HR Manager"}, False),
    (SUP, {"Accounts Manager"}, True), (SUP, {"M-Pesa Manager"}, True),
    # where payments go is routing, not configuration: never System Manager (decision 1, 2026-10-27)
    (SUP, {"System Manager"}, False), (EMP, {"System Manager"}, False),
    (EMP, {"HR Manager"}, True), (EMP, {"M-Pesa Manager"}, True), (EMP, {"Accounts Manager"}, False),
    (EMP, {"HR User"}, False),
])
def test_who_may_make_an_account_the_default(env, doctype, roles, allowed):
    stored(env, doctype, "ACC-1", is_default=1)
    stored(env, doctype, "ACC-2", shortcode="111111")
    env.frappe.user_roles = roles
    if not allowed:
        with pytest.raises(env.frappe.PermissionError):
            env.pa.make_default(doc_of(env, doctype, "ACC-2"))
        return
    env.pa.make_default(doc_of(env, doctype, "ACC-2"))
    table = env.frappe.db.table(doctype)
    assert (table["ACC-1"]["is_default"], table["ACC-2"]["is_default"]) == (0, 1)
    assert sorted(v[0] for v in versions(env)) == ["ACC-1", "ACC-2"]
    assert any(c[1] == "ACC-2" and "Made the default" in c[2] and "in place of ACC-1" in c[2]
               for c in env.frappe.comments)
    # an employee's default change alerts the Notify Role, with no employee named
    assert [a[1] for a in env.alerts] == (
        ["payer@example.com changed the default M-Pesa account of an employee to ACC-2."] if doctype == EMP else [])


@BOTH
def test_a_blocked_or_unapproved_account_is_never_the_default_but_one_on_hold_is(env, doctype):
    env.frappe.user_roles = {"M-Pesa Manager"}
    stored(env, doctype, "ACC-1", needs_reapproval=1, reapproval_reason="Bank paybill KCB changed")
    with pytest.raises(env.frappe.ValidationError, match="ACC-1 is blocked and cannot be the default"):
        env.pa.make_default(doc_of(env, doctype, "ACC-1"))
    stored(env, doctype, "ACC-2", shortcode="111111", docstatus=0)
    with pytest.raises(env.frappe.ValidationError, match="Only an approved account can be the default"):
        env.pa.make_default(doc_of(env, doctype, "ACC-2"))
    stored(env, doctype, "ACC-3", shortcode="333333", active_from=NOW + datetime.timedelta(hours=5))
    env.pa.make_default(doc_of(env, doctype, "ACC-3"))
    assert env.frappe.db.table(doctype)["ACC-3"]["is_default"] == 1


class Saved(fake_frappe._dict):
    def has_value_changed(self, field):
        return self.get(field) != self["_before"].get(field)

    def get_doc_before_save(self):
        return fake_frappe._dict(self["_before"])


def test_the_default_of_an_approved_account_changes_only_with_make_default(env):
    with pytest.raises(env.frappe.ValidationError, match="Use Make Default"):
        env.pa.refuse_default_edit(Saved(doctype=SUP, is_default=1, _before={"is_default": 0}))
    env.pa.refuse_default_edit(Saved(doctype=SUP, is_default=1, remarks="x", _before={"is_default": 1}))


@pytest.mark.parametrize("field, before, after", [
    ("needs_reapproval", 1, 0),  # KCB's paybill changed: only cancel, amend and approve again clears it
    ("name_mismatch", 1, 0),  # only Receiver Name Confirmed (a manager, named) clears it
    ("reapproval_reason", "Bank paybill KCB changed from 522522 to 522599", None),
    ("last_receiver_name", "999999 - JOHN", "522522 - KCB Paybill AC"),
    ("last_paid_on", datetime.datetime(2026, 10, 1, 9, 0), datetime.datetime(2026, 10, 2, 9, 0)),
])
def test_a_save_after_approval_never_clears_a_block_nor_rewrites_the_last_payment(env, field, before, after):
    with pytest.raises(env.frappe.ValidationError, match="set by M-Pesa Suite only on an approved M-Pesa account"):
        env.pa.refuse_default_edit(Saved(doctype=EMP, **{field: after}, _before={field: before}))
    env.pa.refuse_default_edit(Saved(doctype=EMP, **{field: before}, _before={field: before}))  # unchanged


def test_a_save_after_approval_that_changes_nothing_passes(env):
    # 0, "" and None alike: a form or API client sending an empty field changes nothing
    env.pa.refuse_default_edit(Saved(doctype=SUP, needs_reapproval=None, name_mismatch="", reapproval_reason="",
                                     _before={"needs_reapproval": 0, "name_mismatch": 0, "reapproval_reason": None}))


def test_the_form_knows_when_a_default_is_missing(env):
    stored(env, SUP, "MSA-1")
    stored(env, SUP, "MSA-2", shortcode="111111")
    env.frappe.user_roles = {"Accounts User"}
    state = env.pa.form_state(doc_of(env, SUP, "MSA-1"))
    assert state == {"can_make_default": True, "approved": 2, "default": None}
    env.frappe.user_roles = {"Purchase User"}
    assert env.pa.form_state(doc_of(env, SUP, "MSA-1"))["can_make_default"] is False
    env.frappe.user_roles = {"System Manager"}
    assert env.pa.form_state(doc_of(env, SUP, "MSA-1"))["can_make_default"] is False


@BOTH
def test_the_shared_administrator_login_cannot_change_a_default(env, doctype):
    stored(env, doctype, "ACC-1", is_default=1)
    stored(env, doctype, "ACC-2", shortcode="111111")
    env.frappe.user_roles, env.frappe.session.user = {"M-Pesa Manager"}, "Administrator"
    assert env.pa.form_state(doc_of(env, doctype, "ACC-2"))["can_make_default"] is False  # no button
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        env.pa.make_default(doc_of(env, doctype, "ACC-2"))
    table = env.frappe.db.table(doctype)
    assert (table["ACC-1"]["is_default"], table["ACC-2"]["is_default"]) == (1, 0) and not versions(env)


# ---------------------------------------------------------------- picking the account of a channel
def test_b2b_picks_the_only_b2b_account_beside_a_phone_default(env):
    stored(env, EMP, "MEA-1", account_type=PHONE, phone="254722000111", shortcode=None, account_number=None,
           is_default=1)
    stored(env, EMP, "MEA-2", account_type=BANK, bank_paybill="KCB", shortcode="522522", account_number="1234567890123")
    assert env.pa.pick_account(EMP, "EMP-1", env.pa.B2B_TYPES) == "MEA-2"
    assert env.pa.default_account(EMP, "EMP-1") == "MEA-1"
    stored(env, EMP, "MEA-3", account_type=TILL, shortcode="835811", account_number=None)
    assert env.pa.pick_account(EMP, "EMP-1", env.pa.B2B_TYPES) is None  # two B2B accounts, neither the default


def request(env, **kw):
    row = {"name": "MB2B-1", "docstatus": 0, "supplier": None, "supplier_mpesa_account": None, "employee": None,
           "employee_mpesa_account": None, "command_id": None, "receiver_shortcode": "999999",
           "account_reference": "TYPED"}
    row.update(kw)
    env.frappe.db.table(B2B)[row["name"]] = row
    return env.frappe.get_doc(B2B, row["name"])


@pytest.mark.parametrize("kw, command, reference", [
    ({}, "BusinessPayBill", "TK-001"),
    ({"account_type": TILL, "shortcode": "835811", "account_number": None}, "BusinessBuyGoods", None),
    ({"account_type": BANK, "bank_paybill": "KCB", "shortcode": "522522", "account_number": "1234567890123"},
     "BusinessPayBill", "1234567890123"),
])
def test_b2b_pays_a_paybill_till_or_bank_account_of_either_party(env, kw, command, reference):
    stored(env, SUP, "MSA-1", **kw)
    stored(env, EMP, "MEA-1", **kw)
    for module, values in ((env.sa, {"supplier": "Total Kenya"}), (env.ea, {"employee": "EMP-1"})):
        doc = request(env, **values)
        module.apply_destination(doc)
        assert (doc.command_id, doc.account_reference) == (command, reference)
        assert doc.receiver_shortcode == kw.get("shortcode", "888880")
        module.check_before_send(doc)
        doc.command_id = "BusinessBuyGoods" if command == "BusinessPayBill" else "BusinessPayBill"
        with pytest.raises(env.frappe.ValidationError, match="does not match"):
            module.check_before_send(doc)


def test_a_suppliers_phone_is_never_paid_by_b2b(env):
    stored(env, SUP, "MSA-1", account_type=PHONE, phone="254722000111", shortcode=None, account_number=None)
    with pytest.raises(env.frappe.ValidationError, match="is a phone account: B2B pays a paybill, till or bank"):
        env.sa.apply_destination(request(env, supplier="Total Kenya", supplier_mpesa_account="MSA-1"))
    with pytest.raises(env.frappe.ValidationError, match="Total Kenya has none that can be paid yet"):
        env.sa.apply_destination(request(env, supplier="Total Kenya"))  # its phone is not picked
    doc = request(env, supplier="Total Kenya", supplier_mpesa_account="MSA-1", receiver_shortcode=None,
                  command_id="BusinessPayBill", account_reference=None)
    with pytest.raises(env.frappe.ValidationError, match="is a phone account"):
        env.sa.check_before_send(doc)


# ---------------------------------------------------------------- after payment
@pytest.mark.parametrize("received, mismatch", [
    ("522522 - KCB Paybill AC", False),  # the live result of 2026-10-22
    ("522522– KCB Paybill AC", False),  # an en-dash, as in Safaricom's own sample
    ("522523 - KCB Paybill AC", True),  # another number
    ("522522 - JOHN DOE", True),  # another name
])
def test_a_bank_account_is_checked_against_its_banks_number_and_name(env, received, mismatch):
    stored(env, EMP, "MEA-1", account_type=BANK, bank_paybill="KCB", shortcode="522522", account_number="1234567890123",
           expected_name="KCB Paybill AC")
    payout = request(env, employee="EMP-1", employee_mpesa_account="MEA-1", docstatus=1)
    assert env.pa.record_receiver_name(EMP, "MEA-1", received, payout) is mismatch
    assert env.frappe.db.table(EMP)["MEA-1"]["name_mismatch"] == int(mismatch)
    if received.startswith("522523"):
        assert "number 522523, not 522522 on Employee M-Pesa Account MEA-1" in env.alerts[0][1]


def test_a_bank_with_no_name_yet_checks_the_number_and_asks_a_manager_to_confirm_the_name(env):
    stored(env, SUP, "MSA-1", account_type=BANK, bank_paybill="EQUITY-BANK", shortcode="247247",
           account_number="0123456789", expected_name="Equity Bank Kenya Limited")
    payout = request(env, supplier="Total Kenya", supplier_mpesa_account="MSA-1", docstatus=1)
    assert env.pa.record_receiver_name(SUP, "MSA-1", "247247 - EQUITY BANK PAYBILL", payout) is False
    assert env.frappe.db.table(BANKS)["EQUITY-BANK"]["observed_receiver_name"] == "EQUITY BANK PAYBILL"
    (alert,) = env.alerts
    assert alert[0] == "EQUITY-BANK" and "M-Pesa Manager" in alert[2] and "copy it into Name M-Pesa Shows" in alert[1]
    env.pa.record_receiver_name(SUP, "MSA-1", "247247 - EQUITY BANK PAYBILL", payout)
    assert len(env.alerts) == 1  # once per new name
    assert env.pa.record_receiver_name(SUP, "MSA-1", "247248 - EQUITY BANK PAYBILL", payout) is True


def test_phones_and_tills(env):
    stored(env, EMP, "MEA-1", account_type=PHONE, phone="254722000111", shortcode=None, account_number=None,
           expected_name="Jane Wanjiru")
    payout = fake_frappe._dict(doctype="B2C Disbursement Request", name="B2C-1", add_comment=lambda *a: None)
    assert env.pa.record_receiver_name(EMP, "MEA-1", "2547****0111 - JANE WANJIRU", payout) is False  # masked
    assert env.pa.record_receiver_name(EMP, "MEA-1", "0722000111 - JANE WANJIRU", payout) is False
    assert env.pa.record_receiver_name(EMP, "MEA-1", "254733000000 - JANE WANJIRU", payout) is True
    stored(env, SUP, "MSA-1", account_type=TILL, shortcode="835811", account_number=None)
    assert env.pa.record_receiver_name(SUP, "MSA-1", None, payout) is None  # a Buy Goods result names no one


def test_names_match_ignores_the_number_and_an_en_dash(env):
    assert env.pa.names_match("Biller Co", "000000– Biller Co")
    assert env.pa.without_number("522522 - KCB Paybill AC") == "KCB Paybill AC"
    assert env.pa.number_prefix("JOHN DOE") is None


def test_unblocking_a_bank_account_keeps_the_banks_name_on_the_bank(env):
    stored(env, EMP, "MEA-1", account_type=BANK, bank_paybill="KCB", shortcode="522522", account_number="1234567890123",
           expected_name="KCB Paybill AC", name_mismatch=1, last_receiver_name="522522 - KCB BANK")
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.pa.confirm_receiver_name(doc_of(env, EMP, "MEA-1"), adopt_name=True)
    row = env.frappe.db.table(EMP)["MEA-1"]
    assert (row["name_mismatch"], row["expected_name"]) == (0, "KCB Paybill AC")
    assert "kept on M-Pesa Bank Paybill KCB" in env.frappe.comments[-1][2]


def test_an_account_whose_bank_paybill_changed_is_blocked(env):
    stored(env, SUP, "MSA-1", needs_reapproval=1, reapproval_reason="Bank paybill KCB changed from 522522 to 522523: "
           "cancel and amend this account so a manager approves the new number.")
    problem = env.sa.usable_problem(env.sa.get_account("MSA-1"), "Total Kenya")
    assert problem.startswith("Supplier M-Pesa Account MSA-1 is blocked: Bank paybill KCB changed from 522522")
    assert env.sa.pick_account("Total Kenya") is None


# ---------------------------------------------------------------- the approval
def test_the_shared_administrator_login_cannot_approve(env):
    env.frappe.session.user = "Administrator"
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        env.pa.approve(new(env, SUP))


def test_administrator_is_no_longer_exempt_from_the_second_person_rule(env, monkeypatch):
    class Settings(dict):
        def get(self, key, default=None):
            return 0 if key == "refuse_administrator_payouts" else default

    monkeypatch.setattr(env.frappe, "get_cached_doc", lambda *a, **kw: Settings())
    env.frappe.session.user = "Administrator"
    with pytest.raises(env.frappe.ValidationError, match="A different person must approve"):
        env.pa.approve(new(env, SUP, owner="Administrator"))


def test_the_approval_is_the_deliberate_step(env, monkeypatch):
    send_confirm = importlib.import_module("mpsa_suite.money_out.send_confirm")
    seen = []
    monkeypatch.setattr(send_confirm, "assert_deliberate", seen.append)
    env.frappe.user_roles, env.frappe.session.user = {"M-Pesa Manager"}, "boss@example.com"
    doc = new(env, EMP)
    env.pa.approve(doc)
    assert seen == [doc] and doc.approved_by == "boss@example.com"


def test_a_bank_name_copied_again_at_approval_is_nobodys_change_but_another_bank_is(env):
    env.frappe.db.columns = env.frappe.db.columns | {(EMP, "entered_by")}
    saved = dict(draft(EMP, **bank(EMP)), name="MEA-NEW", entered_by="hr@example.com", owner="hr@example.com")
    env.frappe.session.user = "hr@example.com"
    first = env.frappe.get_doc(dict(saved, doctype=EMP))
    env.pa.validate_account(first)  # HR's save
    env.frappe.db.table(EMP)["MEA-NEW"] = {k: v for k, v in first._row.items()}
    env.frappe.db.table(BANKS)["KCB"]["receiver_name"] = "KCB PAYBILL ACCOUNT"  # an admin corrects the bank's name
    env.frappe.user_roles, env.frappe.session.user = {"M-Pesa Manager"}, "boss@example.com"
    doc = env.frappe.get_doc(dict(env.frappe.db.table(EMP)["MEA-NEW"], doctype=EMP))  # the form's copy
    env.pa.validate_account(doc)
    env.pa.approve(doc)
    assert (doc.entered_by, doc.expected_name, doc.approved_by) == ("hr@example.com", "KCB PAYBILL ACCOUNT",
                                                                    "boss@example.com")
    env.frappe.db.table(BANKS)["EQUITY-BANK"]["account_number_pattern"] = None
    doc = env.frappe.get_doc(dict(env.frappe.db.table(EMP)["MEA-NEW"], doctype=EMP))
    doc.bank_paybill = "EQUITY-BANK"  # another bank in the submit itself
    env.pa.validate_account(doc)
    with pytest.raises(env.frappe.ValidationError, match="A different person must approve"):
        env.pa.approve(doc)


@BOTH
def test_a_bank_paybill_changed_since_the_draft_was_saved_is_never_approved_unseen(env, doctype):
    # Approve shows the draft as saved (522522); the approval copies the bank's row again
    first = env.frappe.get_doc(dict(draft(doctype, **bank(doctype)), name="ACC-NEW", owner="hr@example.com",
                                    doctype=doctype))
    env.pa.validate_account(first)  # HR's save
    env.frappe.db.table(doctype)["ACC-NEW"] = dict(first._row)
    assert first.shortcode == "522522"
    env.frappe.db.table(BANKS)["KCB"]["paybill"] = "123456"  # a CONFIG role edits the bank's row meanwhile

    def approving():  # before_submit: the saved draft, docstatus 1
        return env.frappe.get_doc(dict(env.frappe.db.table(doctype)["ACC-NEW"], doctype=doctype, docstatus=1))

    with pytest.raises(env.frappe.ValidationError, match="The bank's paybill changed from 522522 to 123456 since this "
                                                         "account was saved: save it again and press Approve again"):
        env.pa.validate_account(approving())
    resaved = env.frappe.get_doc(dict(env.frappe.db.table(doctype)["ACC-NEW"], doctype=doctype))
    env.pa.validate_account(resaved)  # saved again: the draft, and so the Approve summary, show 123456
    env.frappe.db.table(doctype)["ACC-NEW"] = dict(resaved._row)
    doc = approving()
    env.pa.validate_account(doc)
    assert (doc.shortcode, doc.destination_key) == ("123456", "PB:123456:1234567890123")
    # a paybill, till or phone is typed, never copied: nothing to compare
    other = env.frappe.get_doc(dict(draft(doctype), name="ACC-PB", doctype=doctype, docstatus=1))
    env.frappe.db.table(doctype)["ACC-PB"] = dict(other._row, docstatus=0)
    env.pa.validate_account(other)


# ---------------------------------------------------------------- migrate
def test_migrate_renames_the_old_type_links_banks_and_fills_keys(env, monkeypatch):
    env.frappe.db.table("DocType").update({d: {"name": d} for d in (SUP, EMP, BANKS)})
    env.frappe.db.columns = env.frappe.db.columns | {(SUP, "destination_key"), (EMP, "destination_key")}
    stored(env, EMP, "MEA-1", account_type="Bank Paybill (B2B)", shortcode="247247", account_number="0123")
    stored(env, SUP, "MSA-1", account_number="TK 001")
    for table in (env.frappe.db.table(EMP), env.frappe.db.table(SUP)):
        for row in table.values():
            row["destination_key"] = None

    def sql(query, values=()):
        doctype = query.split("`tab", 1)[1].split("`", 1)[0]
        for row in env.frappe.db.table(doctype).values():
            if row.get("account_type") == values[1]:
                row["account_type"] = values[0]

    monkeypatch.setattr(env.frappe.db, "sql", sql, raising=False)
    env.pa.after_migrate()
    env.pa.after_migrate()  # idempotent
    mea, msa = env.frappe.db.table(EMP)["MEA-1"], env.frappe.db.table(SUP)["MSA-1"]
    assert (mea["account_type"], mea["bank_paybill"], mea["destination_key"]) == (BANK, "EQUITY-BANK", "PB:247247:0123")
    assert msa["destination_key"] == "PB:888880:TK001"


# ---------------------------------------------------------------- the schedule and payroll routes
def test_a_supplier_line_never_pays_a_phone_and_takes_its_b2b_account(site):
    db = site.frappe.db
    db.table("Supplier")["SUP-P"] = {"name": "SUP-P", "supplier_name": "Phone Supplier"}
    base = {"docstatus": 1, "supplier": "SUP-P", "active_from": NOW - datetime.timedelta(days=1), "name_mismatch": 0,
            "needs_reapproval": 0}
    db.table(SUP)["MSA-P"] = dict(base, name="MSA-P", account_type=PHONE, phone="254722000555", is_default=1,
                                  creation="2026-10-01")
    db.table(SUP)["MSA-B"] = dict(base, name="MSA-B", account_type=PAYBILL, shortcode="888885",
                                  account_number="SP-1", is_default=0, creation="2026-10-02")
    doc = new_form(site, [{"party_type": "Supplier", "party": "SUP-P", "amount": 100, "reference_doctype": None,
                           "reference_name": None}])
    site.schedule.validate_schedule(doc)
    assert (doc.lines[0].payee_account, doc.lines[0].route) == ("MSA-B", "B2B Paybill")  # its only B2B account
    doc = new_form(site, [{"party_type": "Supplier", "party": "SUP-P", "payee_account": "MSA-P", "amount": 100,
                           "reference_doctype": None, "reference_name": None}])
    with pytest.raises(site.frappe.ValidationError, match="MSA-P is a phone account: pay a supplier's phone with a B2C"):
        site.schedule.validate_schedule(doc)


def test_a_line_whose_account_changed_type_is_not_sent(site):
    schedule = fake_frappe._dict(schedule_row(site))
    line = fake_frappe._dict(bank_line(site))
    assert site.schedule._send_problem(line, schedule, "payer@example.com") is None
    site.frappe.db.table(EMPACC)["MEA-1"].update(account_type=TILL)
    assert site.schedule._send_problem(line, schedule, "payer@example.com") == (
        "MEA-1 is a Till (Buy Goods) account, not paid by B2B Paybill.")


def test_a_default_change_does_not_move_a_planned_line(site):
    site.frappe.db.table(EMPACC)["MEA-5"] = dict(site.frappe.db.table(EMPACC)["MEA-1"], name="MEA-5",
                                                  account_number="5555", is_default=0)
    line = fake_frappe._dict(bank_line(site))
    site.frappe.db.table(EMPACC)["MEA-1"]["is_default"] = 0
    site.frappe.db.table(EMPACC)["MEA-5"]["is_default"] = 1  # D5: what Make Default leaves
    assert line.payee_account == "MEA-1"
    assert site.schedule._send_problem(line, fake_frappe._dict(schedule_row(site)), "payer@example.com") is None


@pytest.mark.parametrize("kw, route, destination", [
    ({"account_type": PAYBILL, "shortcode": "400200", "account_number": "SACCO-9"}, "B2B Paybill", "400200 / SACCO-9"),
    ({"account_type": TILL, "shortcode": "835811", "account_number": None}, "B2B Till", "835811"),
])
def test_payroll_pays_a_paybill_or_till_default_by_b2b(site, kw, route, destination):
    # an Accounts User who may also see salaries: the payroll schedule lists them (decision D4)
    site.frappe.session.user, site.frappe.user_roles = "accounts@example.com", {"Accounts User", "HR Manager"}
    site.frappe.db.table(EMPACC)["MEA-3"] = {
        "name": "MEA-3", "docstatus": 1, "employee": "EMP-3", "phone": None, "expected_name": "Achieng Mary",
        "active_from": NOW - datetime.timedelta(days=2), "is_default": 1, "name_mismatch": 0, **kw}
    answer = site.payroll.from_payroll_entry("PAY-1", b2b_settings=SETTINGS, b2c_settings=SETTINGS)
    line = next(r for r in site.frappe.db.table("M-Pesa Payment Schedule Line").values()
                if r["parent"] == answer["name"] and r["party"] == "EMP-3")
    assert (line["route"], line["destination"], line["reference_doctype"]) == (route, destination, SLIP)
