"""Round 4.3: phone numbers, account numbers and the names M-Pesa paid shown in full (M-Pesa Suite
Settings > Show Numbers and Names in Full, on by default; off: masked as before; the external Payments
API always masks), and a claim payee's phone M-Pesa paid before taking the name M-Pesa paid it under,
never a typed one (the Payee Name locked; a new number keeps the typed name)."""

import datetime
import importlib
import json
from pathlib import Path

import fake_frappe
import pytest
from test_claim_payees import CLAIM, LINE, PHONE, REQ, TILL, claim, payee, validate
from test_claim_payees import env as claims_env  # noqa: F401 (the fixture)
from test_r42_phones import Employee
from test_send_confirm import B2C, disbursement, prepared
from test_send_confirm import env as send_env  # noqa: F401 (the fixture)

_dict = fake_frappe._dict
ROOT = Path(__file__).resolve().parents[2] / "mpsa_suite"
SUITE = "M-Pesa Suite Settings"
AT = datetime.datetime(2026, 10, 29, 20, 3)


def paid(env, name, phone, received, minutes=0, status="Paid"):
    env.frappe.db.table(REQ)[name] = {"name": name, "docstatus": 1, "status": status, "phone_number": phone,
                                      "receiver_public_name": received, "amount": 1000,
                                      "transaction_completed_datetime": AT + datetime.timedelta(minutes=minutes),
                                      "creation": AT + datetime.timedelta(minutes=minutes)}


# ---------------------------------------------------------------- the setting
@pytest.mark.parametrize("stored, full", [(None, True), ("", True), ("1", True), (1, True), ("0", False), (0, False)])
def test_numbers_show_in_full_unless_the_setting_is_off(claims_env, stored, full):
    claims_env.frappe.db.table(SUITE).setdefault(SUITE, {})["show_full_details"] = stored
    privacy = importlib.import_module("mpsa_suite.utils.privacy")
    assert privacy.full_details() is full
    assert privacy.phone("0722 000 111") == ("254722000111" if full else "2547****0111")
    assert privacy.account("0123456789") == ("0123456789" if full else "****6789")
    assert privacy.shown("25416055453") == ("25416055453" if full else "ending 453 (11 digits)")


def test_a_site_not_migrated_yet_shows_them_in_full(claims_env, monkeypatch):
    privacy = importlib.import_module("mpsa_suite.utils.privacy")

    def missing(*a, **kw):
        raise claims_env.frappe.ValidationError("Field show_full_details does not exist")

    monkeypatch.setattr(claims_env.frappe.db, "get_single_value", missing)
    assert privacy.full_details() is True


def test_the_setting_is_a_check_ticked_by_default():
    doc = json.loads((ROOT / "mpsa_suite" / "doctype" / "m_pesa_suite_settings" / "m_pesa_suite_settings.json")
                     .read_text(encoding="utf-8"))
    (field,) = [f for f in doc["fields"] if f["fieldname"] == "show_full_details"]
    assert (field["fieldtype"], field["default"], field["label"]) == ("Check", "1", "Show Numbers and Names in Full")
    assert "show_full_details" in doc["field_order"]


# ---------------------------------------------------------------- shown in full everywhere
def test_prints_show_phones_and_account_numbers_in_full(claims_env):
    pp = importlib.import_module("mpsa_suite.money_out.payment_print")
    assert [pp.masked(v) for v in ("254722000111", "0722000111", "1234567890", "835811", "2547****0111")] == [
        "254722000111", "254722000111", "1234567890", "835811", "2547****0111"]  # M-Pesa's own masking stays
    assert pp.masked_account("PARK-0123456789") == "PARK-0123456789"
    cp = claims_env.cp
    assert pp._line_to(_dict(payee_type=cp.BANK, shortcode="522522", account_number="0123456789"), cp) == (
        "522522 / 0123456789")


def test_the_send_dialog_shows_each_phone_in_full(send_env, monkeypatch):
    monkeypatch.setattr(send_env.sc, "_b2c_charge", lambda amount: None)
    disbursement(send_env)
    rows = prepared(send_env, B2C, "B2C-1")["rows"]
    assert [x["destination"] for x in rows] == ["M-Pesa phone 254712345678", "M-Pesa phone 254712345679"]


def test_claim_payee_messages_alerts_and_name_checks_show_the_numbers_in_full(claims_env):
    cp = claims_env.cp
    line = _dict(idx=2, payee_type=PHONE, phone="254711000333", payee_name="Jane Wanjiku", shortcode=None)
    assert cp.describe(line, masked=True) == "254711000333"
    assert cp.masked_name("254711000333 - JANE WANJIKU") == "254711000333 - JANE WANJIKU"
    ok, note = cp.name_check(line, "254711000334 - JOHN DOE")
    assert ok is False and note == "M-Pesa paid '254711000334 - JOHN DOE': number 254711000334, not 254711000333."


def test_the_pay_dialog_and_payroll_show_numbers_in_full(claims_env):
    ed = importlib.import_module("mpsa_suite.money_out.employee_documents")
    payroll = importlib.import_module("mpsa_suite.money_out.payroll")
    assert ed._masked_phone("254722000111") == "254722000111"
    assert ed._account_label(_dict(account_type="Paybill", shortcode="888880", account_number="ACME-AC-1",
                                   is_default=1)) == "Paybill 888880, account ACME-AC-1 (default)"
    assert payroll._masked("247247 / 0123456789") == "247247 / 0123456789"
    assert payroll._masked("254722000111") == "254722000111"


def test_a_mistyped_cell_number_is_named_in_full_in_its_message(claims_env):
    ea = importlib.import_module("mpsa_suite.money_out.employee_accounts")
    with pytest.raises(claims_env.frappe.ValidationError, match="Cell Number 25416055453 is not a Kenyan mobile"):
        ea.check_cell_number(Employee("25416055453", before="0716 055 453"))


def test_the_external_payments_api_still_masks_the_phone():
    source = (ROOT / "gateway" / "intents.py").read_text(encoding="utf-8")
    assert '"phone": mask(intent.phone)' in source and "privacy" not in source


def test_masked_mode_still_masks_everywhere(claims_env, masked_numbers):
    pp = importlib.import_module("mpsa_suite.money_out.payment_print")
    cp = claims_env.cp
    assert pp.masked("254722000111") == "2547****0111" and pp.masked_account("0123456789") == "****6789"
    assert cp.describe(_dict(payee_type=PHONE, phone="254711000333"), masked=True) == "2547****0333"


# ---------------------------------------------------------------- a payee's name from M-Pesa
def test_the_latest_name_mpesa_paid_a_phone_under_is_found(claims_env):
    cp = claims_env.cp
    paid(claims_env, "B2C-1", "254711000333", "254711000333 - JANE WANJIKU OLD", minutes=0)
    paid(claims_env, "B2C-2", "254711000333", "254711000333 - JANE WANJIKU", minutes=30)
    paid(claims_env, "B2C-3", "254711000333", "254711000333 - NOT PAID", minutes=60, status="Failed")
    paid(claims_env, "B2C-4", "0722000111", "0722000111 - MUSA YAWA CHIMWAGA")  # stored in local form
    paid(claims_env, "B2C-5", "254733000999", "254700000000 - SOMEONE ELSE")  # M-Pesa named another number
    found = cp.mpesa_names(["0711 000 333", "254722000111", "0733000999", "0799000000", None])
    assert {k: (v.name, v.payout) for k, v in found.items()} == {
        "254711000333": ("JANE WANJIKU", "B2C-2"), "254722000111": ("MUSA YAWA CHIMWAGA", "B2C-4")}
    assert cp.name_from(found["254711000333"]) == "B2C-2, paid 2026-10-29"
    assert cp.mpesa_names([]) == {} and cp.mpesa_names([None, "12"]) == {}


def test_a_known_phone_takes_mpesas_name_and_a_new_one_keeps_the_typed_name(claims_env):
    cp = claims_env.cp
    paid(claims_env, "B2C-2", "254711000333", "254711000333 - JANE WANJIKU NJERI")
    known = payee(payee_type=PHONE, phone="0711 000 333", payee_name="Jane Wanjiku")
    cp.clean_line(known, set(), [])
    assert (known.payee_name, known.mpesa_name, known.mpesa_name_from) == (
        "JANE WANJIKU NJERI", "JANE WANJIKU NJERI", "B2C-2, paid 2026-10-29")
    nameless = payee(payee_type=PHONE, phone="0711 000 333", payee_name="")  # a known number needs no typed name
    cp.clean_line(nameless, set(), [])
    assert nameless.payee_name == "JANE WANJIKU NJERI"
    new = payee(payee_type=PHONE, phone="0711 000 444", payee_name="Peter Otieno")
    cp.clean_line(new, set(), [])
    assert (new.payee_name, new.mpesa_name, new.mpesa_name_from) == ("Peter Otieno", None, None)
    with pytest.raises(claims_env.frappe.ValidationError, match="enter the Payee Name"):
        cp.clean_line(payee(payee_type=PHONE, phone="0711 000 444", payee_name=""), set(), [])
    till = payee(payee_type=TILL, mpesa_name="LEFT OVER", mpesa_name_from="B2C-9, paid 2026-10-01")
    cp.clean_line(till, set(), [])
    assert (till.payee_name, till.mpesa_name, till.mpesa_name_from) == ("Shell Kilimani", None, None)


def test_a_claims_save_reads_the_names_of_all_its_phones_once(claims_env, monkeypatch):
    cp = claims_env.cp
    paid(claims_env, "B2C-2", "254711000333", "254711000333 - JANE WANJIKU NJERI")
    calls, real = [], cp.mpesa_names

    def counted(numbers):
        numbers = list(numbers)
        calls.append(numbers)
        return real(numbers)

    monkeypatch.setattr(cp, "mpesa_names", counted)
    lines = [payee(payee_type=PHONE, phone="0711 000 333", payee_name="Typed Wrong", amount=500),
             payee(name="LN-2", idx=2, payee_type=PHONE, phone="0711 000 444", payee_name="Peter Otieno", amount=500),
             payee(name="LN-3", idx=3)]
    doc = claim(claims_env, lines, store=False, docstatus=0)
    validate(claims_env, doc)
    assert calls == [["254711000333", "254711000444"]]
    assert [r.payee_name for r in lines] == ["JANE WANJIKU NJERI", "Peter Otieno", "Shell Kilimani"]


def test_the_form_gets_the_name_when_a_phone_is_typed(claims_env):
    cp = claims_env.cp
    paid(claims_env, "B2C-2", "254711000333", "254711000333 - JANE WANJIKU NJERI")
    assert cp.mpesa_name("0711 000 333") == {"phone": "254711000333", "name": "JANE WANJIKU NJERI",
                                             "from": "B2C-2, paid 2026-10-29"}
    assert cp.mpesa_name("0711 000 444") == {} and cp.mpesa_name("12") == {} and cp.mpesa_name(None) == {}
    claims_env.denied.update({(CLAIM, "create"), (CLAIM, "write")})
    with pytest.raises(claims_env.frappe.PermissionError):
        cp.mpesa_name("0711 000 333")


def test_pasted_payees_take_mpesas_name_for_known_phones(claims_env):
    cp = claims_env.cp
    claim(claims_env, [])
    paid(claims_env, "B2C-2", "254711000333", "254711000333 - JANE WANJIKU NJERI")
    answer = cp.parse_payees(text="Jane W, 0711000333, 1500\n, 0711000333 , 200\nPeter, 0711000444, 300",
                             claim="HR-EXP-1")
    rows = answer["rows"]
    assert [(r["payee_name"], r.get("mpesa_name"), r.get("typed_name")) for r in rows] == [
        ("JANE WANJIKU NJERI", "JANE WANJIKU NJERI", "Jane W"),
        ("JANE WANJIKU NJERI", "JANE WANJIKU NJERI", ""),  # no name typed: M-Pesa's is enough
        ("Peter", None, None)]
    assert rows[0]["mpesa_name_from"] == "B2C-2, paid 2026-10-29"
    assert [r["errors"] for r in rows] == [[], ["this phone is on the claim already, or earlier in the list"], []]


def test_the_payee_name_is_locked_once_mpesa_named_the_phone():
    doc = json.loads((ROOT / "mpsa_suite" / "doctype" / "m_pesa_claim_payee" / "m_pesa_claim_payee.json")
                     .read_text(encoding="utf-8"))
    fields = {f["fieldname"]: f for f in doc["fields"]}
    assert fields["payee_name"]["read_only_depends_on"] == "eval:doc.mpesa_name"
    for name in ("mpesa_name", "mpesa_name_from"):
        assert fields[name]["read_only"] == 1 and fields[name]["fieldtype"] == "Data"
    assert doc["field_order"].index("mpesa_name") == doc["field_order"].index("payee_name") + 1
    script = (ROOT / "public" / "js" / "expense_claim_payees.js").read_text(encoding="utf-8")
    assert "mpsa_payees.mpesa_name(cdt, cdn);" in script and f"`${{MPSA_PAYEES_API}}.mpesa_name`" in script
    assert "mpesa_name: r.mpesa_name || null" in script  # the paste dialog adds the name's source too
    assert LINE == "M-Pesa Claim Payee"
