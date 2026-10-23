"""Employee M-Pesa Accounts (money_out.party_accounts, employee_accounts): an employee is paid only
to a phone or bank paybill account a second person approved.

Covers: the phone and bank paybill details, repeats for the same employee refused and for another
warned, maker-checker approval with proof and the employee hold period, the B2B request taking
its destination from an approved bank paybill account (never a phone, never typed), the checks
again before sending, the B2C phone check, and the receiver-name check that blocks an account
until a manager confirms it. The supplier wrappers keep their behaviour (test_supplier_accounts).
"""

import datetime
import importlib

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)
SUITE = "M-Pesa Suite Settings"
ACC = "Employee M-Pesa Account"
B2B = "Mpesa B2B Payment Request"


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.table(SUITE)[SUITE] = {"supplier_account_hold_hours": 24, "employee_account_hold_hours": 48,
                                         "allow_one_off_b2b": 1}
        frappe.db.table("Mpesa Settings")["Astral 600999"] = {
            "name": "Astral 600999", "business_shortcode": "600999", "till_number": "5123456",
        }
        pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        ea = importlib.import_module("mpsa_suite.money_out.employee_accounts")
        alerts = []
        pa_alert = pa.alert
        pa.alert = lambda message, doc: alerts.append((doc.name, message))
        yield fake_frappe._dict(frappe=frappe, pa=pa, ea=ea, alerts=alerts)
        pa.alert = pa_alert
    finally:
        fake_frappe.uninstall(saved)


def account(env, name="MEA-1", **kw):
    row = {"name": name, "docstatus": 1, "employee": "EMP-1", "account_type": "Bank Paybill (B2B)", "phone": None,
           "shortcode": "247247", "account_number": "0123456789", "bank": "Equity", "expected_name": "Equity Bank",
           "active_from": NOW - datetime.timedelta(hours=1), "is_default": 0, "owner": "hr@example.com",
           "proof": "/private/files/bank-letter.pdf", "creation": "2026-10-01"}
    row.update(kw)
    env.frappe.db.table(ACC)[name] = row
    return row


def phone(env, name="MEA-2", **kw):
    return account(env, name, account_type="Phone (B2C)", phone="254722000111", shortcode=None, account_number=None,
                   bank=None, expected_name="Jane Wanjiru", **kw)


def new_account(env, **kw):
    row = {"name": "MEA-NEW", "docstatus": 0, "employee": "EMP-1", "account_type": "Phone (B2C)", "phone": "0722 000 111",
           "shortcode": None, "account_number": None, "expected_name": "Jane Wanjiru", "owner": "hr@example.com",
           "proof": "/private/files/registration.pdf"}
    row.update(kw)
    return env.frappe.get_doc(dict(row, doctype=ACC))


def request(env, **kw):
    row = {"name": "MB2B-1", "docstatus": 0, "employee": "EMP-1", "employee_mpesa_account": None,
           "command_id": "BusinessBuyGoods", "receiver_shortcode": "999999", "account_reference": "TYPED"}
    row.update(kw)
    env.frappe.db.table(B2B)[row["name"]] = row
    return env.frappe.get_doc(B2B, row["name"])


# ---------------------------------------------------------------- entering an account
def test_a_phone_is_kept_as_2547_and_drops_bank_details(env):
    doc = new_account(env, shortcode="247247", account_number="X")
    env.pa.validate_account(doc)
    assert (doc.phone, doc.shortcode, doc.account_number) == ("254722000111", None, None)


@pytest.mark.parametrize("value", ["0722", "2547220001", "+1 202 555 0100", ""])
def test_a_phone_that_is_not_a_kenyan_mobile_is_refused(env, value):
    with pytest.raises(env.frappe.ValidationError, match="Enter the employee's M-Pesa phone number"):
        env.pa.validate_account(new_account(env, phone=value))


@pytest.mark.parametrize("kw, msg", [
    ({"shortcode": "24A247"}, "Bank Paybill Number must be 5 to 7 digits"),
    ({"account_number": ""}, "needs the employee's bank account number"),
    ({"shortcode": "600999"}, "own M-Pesa numbers"),
    ({"account_number": "1" * 21}, "longer than M-Pesa allows"),
])
def test_bad_bank_paybill_details_are_refused(env, kw, msg):
    values = {"account_type": "Bank Paybill (B2B)", "phone": None, "shortcode": " 247247 ", "account_number": "0123",
              **kw}
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.pa.validate_account(new_account(env, **values))


def test_a_bank_paybill_is_cleaned_and_drops_the_phone(env):
    doc = new_account(env, account_type="Bank Paybill (B2B)", phone="0722000111", shortcode=" 247247 ",
                      account_number=" 0123 ")
    env.pa.validate_account(doc)
    assert (doc.shortcode, doc.account_number, doc.phone) == ("247247", "0123", None)


def test_a_supplier_account_type_is_not_an_employee_one(env):
    with pytest.raises(env.frappe.ValidationError, match="Account Type must be Phone \\(B2C\\) or Bank Paybill \\(B2B\\)"):
        env.pa.validate_account(new_account(env, account_type="Paybill"))


def test_the_same_details_for_the_employee_are_refused_and_for_another_warned(env):
    phone(env)
    with pytest.raises(env.frappe.ValidationError, match="This employee already has an approved account"):
        env.pa.validate_account(new_account(env))
    env.pa.validate_account(new_account(env, employee="EMP-2"))
    assert any("already approved for EMP-1" in m and "Check with the employee" in m for m in env.frappe.messages)


# ---------------------------------------------------------------- approval
def test_who_entered_an_account_cannot_approve_it(env):
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.frappe.session.user = "hr@example.com"
    with pytest.raises(env.frappe.ValidationError, match="different person"):
        env.pa.approve(new_account(env))


def test_an_hr_user_cannot_approve(env):
    env.frappe.user_roles = {"HR User", "HR Manager"}
    env.frappe.session.user = "boss@example.com"
    with pytest.raises(env.frappe.PermissionError):
        env.pa.approve(new_account(env))


def test_approval_needs_the_employees_proof_and_starts_the_employee_hold(env):
    env.frappe.user_roles = {"Accounts Manager"}
    env.frappe.session.user = "boss@example.com"
    with pytest.raises(env.frappe.ValidationError, match="Attach the employee's proof"):
        env.pa.approve(new_account(env, proof=None))
    doc = new_account(env)
    env.pa.approve(doc)
    assert (doc.approved_by, doc.active_from) == ("boss@example.com", NOW + datetime.timedelta(hours=48))


def test_default_is_one_per_employee(env):
    account(env, "MEA-1", is_default=1)
    account(env, "MEA-3", employee="EMP-2", is_default=1)
    doc = env.frappe.get_doc(ACC, phone(env, "MEA-2", is_default=1)["name"])
    env.pa.set_default(doc)
    table = env.frappe.db.table(ACC)
    assert (table["MEA-1"]["is_default"], table["MEA-2"]["is_default"], table["MEA-3"]["is_default"]) == (0, 1, 1)


# ---------------------------------------------------------------- B2B requests to an employee
def test_a_b2b_request_takes_its_destination_from_the_bank_paybill_account(env):
    account(env)
    doc = request(env, employee_mpesa_account="MEA-1")
    env.ea.apply_destination(doc)
    assert (doc.command_id, doc.receiver_shortcode, doc.account_reference) == ("BusinessPayBill", "247247", "0123456789")


def test_a_b2b_request_picks_the_bank_paybill_not_the_phone(env):
    phone(env, is_default=1)
    account(env)
    doc = request(env)
    env.ea.apply_destination(doc)
    assert doc.employee_mpesa_account == "MEA-1"


def test_a_phone_account_is_never_paid_by_b2b(env):
    phone(env)
    with pytest.raises(env.frappe.ValidationError, match="is a phone account: B2B pays bank paybills only"):
        env.ea.apply_destination(request(env, employee_mpesa_account="MEA-2"))


def test_an_employee_without_an_approved_bank_paybill_is_refused_even_with_one_offs_on(env):
    phone(env)
    with pytest.raises(env.frappe.ValidationError, match="has none that can be paid by B2B"):
        env.ea.apply_destination(request(env))


@pytest.mark.parametrize("kw, msg", [
    ({"docstatus": 0}, "not approved"),
    ({"docstatus": 2}, "cancelled"),
    ({"employee": "EMP-9"}, "belongs to EMP-9"),
    ({"active_from": NOW + datetime.timedelta(hours=3)}, "on hold"),
    ({"name_mismatch": 1, "last_receiver_name": "247247 - KCB"}, "blocked"),
])
def test_an_unusable_account_is_refused(env, kw, msg):
    account(env, **kw)
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.ea.apply_destination(request(env, employee_mpesa_account="MEA-1"))


def test_before_sending_the_account_and_destination_are_checked_again(env):
    account(env)
    doc = request(env, employee_mpesa_account="MEA-1")
    env.ea.apply_destination(doc)
    env.ea.check_before_send(doc)
    doc.account_reference = "9999"
    with pytest.raises(env.frappe.ValidationError, match="does not match Employee M-Pesa Account MEA-1"):
        env.ea.check_before_send(doc)
    doc.account_reference = "0123456789"
    env.frappe.db.table(ACC)["MEA-1"]["docstatus"] = 2
    with pytest.raises(env.frappe.ValidationError, match="cancelled"):
        env.ea.check_before_send(doc)
    with pytest.raises(env.frappe.ValidationError, match="Pick the employee's approved bank paybill"):
        env.ea.check_before_send(request(env, employee_mpesa_account=None))


# ---------------------------------------------------------------- B2C to a phone
def test_a_b2c_payout_goes_only_to_the_approved_phone(env):
    phone(env)
    account(env)
    assert env.ea.phone_problem("MEA-2", "EMP-1", "254722000111") is None
    assert "does not match" in env.ea.phone_problem("MEA-2", "EMP-1", "254733000000")
    assert "is a bank paybill account" in env.ea.phone_problem("MEA-1", "EMP-1", "254722000111")
    assert "belongs to EMP-1" in env.ea.phone_problem("MEA-2", "EMP-2", "254722000111")


def test_destination_and_route_of_each_account_type(env):
    assert env.pa.destination(fake_frappe._dict(phone(env))) == ("B2C Phone", "254722000111")
    assert env.pa.destination(fake_frappe._dict(account(env))) == ("B2B Paybill", "247247 / 0123456789")


def test_the_default_account_counts_one_still_on_hold(env):
    phone(env, active_from=NOW + datetime.timedelta(hours=5))
    assert env.pa.default_account(ACC, "EMP-1") == "MEA-2"  # a line may be due after its hold
    assert env.pa.pick_account(ACC, "EMP-1") is None  # but nothing pays it now


# ---------------------------------------------------------------- after payment
@pytest.mark.parametrize("expected, received, ok", [
    ("Jane Wanjiru", "254722000111 - JANE WANJIRU KAMAU", True),
    ("Equity Bank", "247247 - Equity Bank (Kenya) Limited", True),
    ("Jane Wanjiru", "254722000111 - JOHN DOE", False),
])
def test_names_match_for_phones_and_banks(env, expected, received, ok):
    assert env.pa.names_match(expected, received) is ok


def test_a_name_mismatch_blocks_the_account_and_alerts(env):
    phone(env)
    payout = env.frappe.get_doc({"doctype": "B2C Disbursement Request", "name": "B2C-1"})
    env.frappe.db.table("B2C Disbursement Request")["B2C-1"] = {"name": "B2C-1"}
    assert env.ea.record_receiver_name(payout, "MEA-2", "254722000111 - JOHN DOE") is True
    row = env.frappe.db.table(ACC)["MEA-2"]
    assert row["name_mismatch"] == 1 and row["last_receiver_name"].endswith("JOHN DOE")
    assert env.alerts and "Check with the employee" in env.alerts[0][1]
    assert "blocked" in env.ea.phone_problem("MEA-2", "EMP-1", "254722000111")
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.pa.confirm_receiver_name(env.frappe.get_doc(ACC, "MEA-2"), adopt_name=True)
    assert row["name_mismatch"] == 0 and row["expected_name"] == "JOHN DOE"
    assert any("confirmed with the employee" in c[2] for c in env.frappe.comments)


def test_a_b2b_mismatch_flags_the_request_too(env):
    account(env)
    doc = request(env, employee_mpesa_account="MEA-1", docstatus=1)
    env.ea.record_receiver_name(doc, "MEA-1", "247247 - KCB BANK")
    assert env.frappe.db.table(B2B)["MB2B-1"]["receiver_name_mismatch"] == 1


def test_supplier_wrappers_keep_the_supplier_wording(env):
    sa = importlib.import_module("mpsa_suite.money_out.supplier_accounts")
    assert sa.usable_problem(None) == "Supplier M-Pesa Account not found"
    assert sa.ACCOUNT_DOCTYPE == "Supplier M-Pesa Account" and set(sa.COMMAND_FOR) == {"Paybill", "Till (Buy Goods)"}
