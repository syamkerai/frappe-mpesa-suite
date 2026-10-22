"""Supplier M-Pesa Accounts: B2B payments only go to a paybill / till a second person approved.

Covers: maker-checker approval, the hold period, typed destinations being overwritten by the
approved account, cancelled / other-supplier / held accounts refused (also at send time),
the one-off switch, and the receiver-name check after payment.
"""

import datetime
import importlib

import pytest

import fake_frappe

NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)
SUITE = "M-Pesa Suite Settings"
ACC = "Supplier M-Pesa Account"
B2B = "Mpesa B2B Payment Request"


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.table(SUITE)[SUITE] = {"supplier_account_hold_hours": 24, "allow_one_off_b2b": 0}
        frappe.db.table("Mpesa Settings")["Astral 600999"] = {
            "name": "Astral 600999", "business_shortcode": "600999", "till_number": "5123456",
        }
        sa = importlib.import_module("mpsa_suite.money_out.supplier_accounts")
        yield fake_frappe._dict(frappe=frappe, sa=sa)
    finally:
        fake_frappe.uninstall(saved)


def account(env, name="MSA-1", **kw):
    row = {"name": name, "docstatus": 1, "supplier": "Total Kenya", "account_type": "Paybill",
           "shortcode": "888880", "account_number": "TK-001", "expected_name": "Total Kenya",
           "active_from": NOW - datetime.timedelta(hours=1), "is_default": 0, "owner": "clerk@example.com",
           "proof": "/private/files/letter.pdf", "creation": "2026-10-01"}
    row.update(kw)
    env.frappe.db.table(ACC)[name] = row
    return row


def new_account(env, **kw):
    row = {"name": "MSA-NEW", "docstatus": 0, "supplier": "Total Kenya", "account_type": "Paybill",
           "shortcode": " 888880 ", "account_number": "TK-001", "expected_name": "Total Kenya",
           "owner": "clerk@example.com", "proof": "/private/files/letter.pdf"}
    row.update(kw)
    return env.frappe.get_doc(dict(row, doctype=ACC))


def request(env, **kw):
    row = {"name": "MB2B-1", "docstatus": 0, "supplier": "Total Kenya", "supplier_mpesa_account": None,
           "command_id": "BusinessBuyGoods", "receiver_shortcode": "999999", "account_reference": "TYPED"}
    row.update(kw)
    env.frappe.db.table(B2B)[row["name"]] = row
    return env.frappe.get_doc(B2B, row["name"])


# ---------------------------------------------------------------- entering an account
def test_valid_paybill_is_cleaned(env):
    doc = new_account(env)
    env.sa.validate_account(doc)
    assert doc.shortcode == "888880"


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"shortcode": "88A880"}, "5 to 7 digits"),
        ({"shortcode": "123"}, "5 to 7 digits"),
        ({"account_number": ""}, "needs the Account Number"),
        ({"shortcode": "600999"}, "own M-Pesa numbers"),
        ({"shortcode": "5123456", "account_type": "Till (Buy Goods)"}, "own M-Pesa numbers"),
    ],
)
def test_bad_account_details_refused(env, kw, msg):
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.sa.validate_account(new_account(env, **kw))


def test_till_drops_account_number(env):
    doc = new_account(env, account_type="Till (Buy Goods)", account_number="X")
    env.sa.validate_account(doc)
    assert doc.account_number is None


def test_duplicate_for_same_supplier_refused_other_supplier_warned(env):
    account(env)
    with pytest.raises(env.frappe.ValidationError, match="already has an approved account"):
        env.sa.validate_account(new_account(env))
    env.sa.validate_account(new_account(env, supplier="Someone Else"))
    assert any("Total Kenya" in m for m in env.frappe.messages)


# ---------------------------------------------------------------- approval
def test_maker_cannot_approve_own_account(env):
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.frappe.session.user = "clerk@example.com"
    with pytest.raises(env.frappe.ValidationError, match="different person"):
        env.sa.approve(new_account(env))


def test_clerk_without_manager_role_cannot_approve(env):
    env.frappe.user_roles = {"Accounts User"}
    env.frappe.session.user = "other@example.com"
    with pytest.raises(env.frappe.PermissionError):
        env.sa.approve(new_account(env))


def test_manager_approval_starts_hold(env):
    env.frappe.user_roles = {"Accounts Manager"}
    env.frappe.session.user = "boss@example.com"
    doc = new_account(env)
    env.sa.approve(doc)
    assert doc.approved_by == "boss@example.com"
    assert doc.active_from == NOW + datetime.timedelta(hours=24)


def test_zero_hold_is_usable_at_once(env):
    env.frappe.db.table(SUITE)[SUITE]["supplier_account_hold_hours"] = 0
    env.frappe.user_roles = {"Accounts Manager"}
    env.frappe.session.user = "boss@example.com"
    doc = new_account(env)
    env.sa.approve(doc)
    assert doc.active_from == NOW


# ---------------------------------------------------------------- B2B requests
def test_request_takes_destination_from_account_not_typed_values(env):
    account(env)
    doc = request(env, supplier_mpesa_account="MSA-1")
    env.sa.apply_destination(doc)
    assert (doc.command_id, doc.receiver_shortcode, doc.account_reference) == ("BusinessPayBill", "888880", "TK-001")


def test_request_picks_suppliers_only_account(env):
    account(env, account_type="Till (Buy Goods)", account_number=None, shortcode="777777")
    doc = request(env)
    env.sa.apply_destination(doc)
    assert doc.supplier_mpesa_account == "MSA-1"
    assert (doc.command_id, doc.receiver_shortcode, doc.account_reference) == ("BusinessBuyGoods", "777777", None)


def test_request_picks_default_among_several(env):
    account(env, "MSA-1", shortcode="111111")
    account(env, "MSA-2", shortcode="222222", is_default=1)
    doc = request(env)
    env.sa.apply_destination(doc)
    assert doc.supplier_mpesa_account == "MSA-2"


def test_several_accounts_without_default_must_be_chosen(env):
    account(env, "MSA-1", shortcode="111111")
    account(env, "MSA-2", shortcode="222222")
    with pytest.raises(env.frappe.ValidationError, match="Pick the supplier's approved M-Pesa account"):
        env.sa.apply_destination(request(env))


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"docstatus": 0}, "not approved"),
        ({"docstatus": 2}, "cancelled"),
        ({"supplier": "Someone Else"}, "belongs to Someone Else"),
        ({"active_from": NOW + datetime.timedelta(hours=3)}, "on hold"),
    ],
)
def test_unusable_account_refused(env, kw, msg):
    account(env, **kw)
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.sa.apply_destination(request(env, supplier_mpesa_account="MSA-1"))


def test_typed_number_refused_without_one_off_switch(env):
    with pytest.raises(env.frappe.ValidationError, match="has none that can be paid yet"):
        env.sa.apply_destination(request(env))


def test_one_off_needs_switch_and_manager(env):
    env.frappe.db.table(SUITE)[SUITE]["allow_one_off_b2b"] = 1
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        env.sa.apply_destination(request(env))
    env.frappe.user_roles = {"M-Pesa Manager"}
    doc = request(env)
    env.sa.apply_destination(doc)
    assert doc.receiver_shortcode == "999999"  # typed one-off kept for a manager


def test_account_cancelled_after_request_saved_blocks_send(env):
    account(env)
    doc = request(env, supplier_mpesa_account="MSA-1")
    env.sa.apply_destination(doc)
    env.frappe.db.table(ACC)["MSA-1"]["docstatus"] = 2
    with pytest.raises(env.frappe.ValidationError, match="cancelled"):
        env.sa.check_before_send(doc)


def test_tampered_destination_blocks_send(env):
    account(env)
    doc = request(env, supplier_mpesa_account="MSA-1")
    env.sa.apply_destination(doc)
    doc.receiver_shortcode = "999999"
    with pytest.raises(env.frappe.ValidationError, match="does not match"):
        env.sa.check_before_send(doc)


# ---------------------------------------------------------------- after payment
@pytest.mark.parametrize(
    "expected, received, ok",
    [
        ("Total Kenya", "888880 - TOTAL KENYA LIMITED", True),
        ("KPLC Prepaid", "888880 - KPLC PREPAID", True),
        ("Davis & Shirtliff", "Davis and Shirtliff Ltd", True),
        ("Total Kenya", "888880 - JOHN DOE ENTERPRISES", False),
        ("Total Kenya", "", False),
    ],
)
def test_names_match(env, expected, received, ok):
    assert env.sa.names_match(expected, received) is ok


def test_mismatch_flags_request_and_account(env):
    account(env)
    doc = request(env, supplier_mpesa_account="MSA-1", docstatus=1)
    env.sa.record_receiver_name(doc, "888880 - JOHN DOE ENTERPRISES")
    assert env.frappe.db.table(B2B)["MB2B-1"]["receiver_name_mismatch"] == 1
    row = env.frappe.db.table(ACC)["MSA-1"]
    assert row["name_mismatch"] == 1 and row["last_receiver_name"].endswith("JOHN DOE ENTERPRISES")
    assert any("JOHN DOE" in c[2] for c in env.frappe.comments)


def test_matching_name_clears_flag(env):
    account(env, name_mismatch=1)
    doc = request(env, supplier_mpesa_account="MSA-1", docstatus=1)
    env.sa.record_receiver_name(doc, "888880 - TOTAL KENYA LTD")
    assert env.frappe.db.table(ACC)["MSA-1"]["name_mismatch"] == 0
    assert not env.frappe.db.table(B2B)["MB2B-1"].get("receiver_name_mismatch")


# ---------------------------------------------------------------- blocked after a name mismatch
def test_mismatched_account_is_blocked_on_save_and_send(env):
    account(env, name_mismatch=1, last_receiver_name="888880 - JOHN DOE ENTERPRISES")
    with pytest.raises(env.frappe.ValidationError, match="blocked"):
        env.sa.apply_destination(request(env, supplier_mpesa_account="MSA-1"))
    with pytest.raises(env.frappe.ValidationError, match="blocked"):  # picked automatically
        env.sa.apply_destination(request(env))
    account(env)  # flag cleared, request saved, then a mismatch is recorded before sending
    doc = request(env, supplier_mpesa_account="MSA-1")
    env.sa.apply_destination(doc)
    env.frappe.db.table(ACC)["MSA-1"].update(name_mismatch=1, last_receiver_name="888880 - JOHN DOE")
    with pytest.raises(env.frappe.ValidationError, match="blocked"):
        env.sa.check_before_send(doc)


@pytest.mark.parametrize("adopt, expected", [(True, "JOHN DOE ENTERPRISES"), (False, "Total Kenya")])
def test_manager_confirms_receiver_name(env, adopt, expected):
    account(env, name_mismatch=1, last_receiver_name="888880 - JOHN DOE ENTERPRISES")
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.sa.confirm_receiver_name(env.frappe.get_doc(ACC, "MSA-1"), adopt_name=adopt)
    row = env.frappe.db.table(ACC)["MSA-1"]
    assert row["name_mismatch"] == 0 and row["expected_name"] == expected
    assert any("confirmed with the supplier" in c[2] for c in env.frappe.comments)
    env.sa.apply_destination(request(env, supplier_mpesa_account="MSA-1"))  # payable again


def test_clerk_cannot_unblock(env):
    account(env, name_mismatch=1, last_receiver_name="888880 - JOHN DOE")
    env.frappe.user_roles = {"M-Pesa Payer"}
    with pytest.raises(env.frappe.PermissionError):
        env.sa.confirm_receiver_name(env.frappe.get_doc(ACC, "MSA-1"), adopt_name=True)
    assert env.frappe.db.table(ACC)["MSA-1"]["name_mismatch"] == 1
