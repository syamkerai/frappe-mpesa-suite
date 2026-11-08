"""Send via M-Pesa / Approve (money_out.send_confirm): the token's lifecycle (bound to the user, the
document, its modified, amount and destination; 5 seconds; 10 minutes; once), the summary, the
refusal of every other submit (assert_deliberate, deliberate), the payout controllers calling it
and the named-login rule, maker-checker on reversals, and the alert when a payout-safety setting
changes (M-Pesa Suite Settings)."""

import ast
import importlib
import itertools
import types
from pathlib import Path

import fake_frappe
import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
DOCTYPES = APP / "mpsa_suite" / "doctype"
B2B, B2C, B2C_REQUEST = "Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request"
REVERSAL, SUPPLIER_ACCOUNT, EMPLOYEE_ACCOUNT = "Mpesa Reversal Request", "Supplier M-Pesa Account", "Employee M-Pesa Account"
PAYER, OTHER_PAYER, CLERK, MANAGER = "payer@example.com", "payer2@example.com", "clerk@example.com", "boss@example.com"
MODIFIED = "2026-10-27 10:00:00.000000"


class Cache:
    """frappe.cache: values kept as set, with their expiry recorded."""

    def __init__(self):
        self.values, self.expiry = {}, {}

    def get_value(self, key, *a, **kw):
        return self.values.get(key)

    def set_value(self, key, value, expires_in_sec=None, **kw):
        self.values[key], self.expiry[key] = value, expires_in_sec

    def delete_value(self, key):
        self.values.pop(key, None)


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        sc = importlib.import_module("mpsa_suite.money_out.send_confirm")
        mc = importlib.import_module("mpsa_suite.money_out.maker_checker")
        e = types.SimpleNamespace(frappe=frappe, sc=sc, mc=mc, cache=Cache(), clock=[1000.0], submitted=[],
                                  refuse_submit=None, no_permission=False)
        monkeypatch.setattr(sc, "get_cache", lambda: e.cache)
        monkeypatch.setattr(sc, "_clock", lambda: e.clock[0])
        monkeypatch.setattr(sc, "_b2c_charge", lambda amount: None)
        monkeypatch.setattr(mc, "enabled", lambda: True)
        hashes = itertools.count(1)
        frappe.generate_hash = lambda *a, length=10, **kw: f"t{next(hashes):0{length - 1}d}"[:length]
        frappe.get_cached_doc = lambda *a, **kw: fake_frappe._dict(refuse_administrator_payouts=1)

        class Doc(fake_frappe.FakeDoc):
            """A saved document: its submit runs the payout's first before_submit check."""

            def check_permission(self, ptype="read"):
                if e.no_permission:
                    raise frappe.PermissionError(f"no {ptype}")

            def submit(self):
                e.submitted.append((self.doctype, self.name, list(frappe.flags.get(sc.FLAG) or [])))
                sc.assert_deliberate(self)
                if e.refuse_submit:
                    raise frappe.ValidationError(e.refuse_submit)
                self._row["docstatus"] = 1

        def get_doc(doctype, name=None, **kw):
            row = frappe.db.table(doctype).get(name)
            if row is None:
                raise frappe.DoesNotExistError(f"{doctype} {name}")
            return Doc(frappe, doctype, row)

        frappe.get_doc = get_doc
        frappe.db.table("Supplier")["ACME"] = {"name": "ACME", "supplier_name": "Acme Hardware Ltd"}
        frappe.db.table("Employee")["EMP-1"] = {"name": "EMP-1", "employee_name": "Jane Wanjiru", "user_id": "jane@x"}
        frappe.db.table("Mpesa Settings")["WORKING"] = {"name": "WORKING", "business_shortcode": "4139683"}
        frappe.db.table(SUPPLIER_ACCOUNT)["MSA-1"] = {"name": "MSA-1", "expected_name": "ACME HARDWARE"}
        frappe.session.user, frappe.user_roles = PAYER, {"M-Pesa Payer"}
        yield e
    finally:
        fake_frappe.uninstall(saved)


def b2b(env, name="MB2B-1", **values):
    row = dict(name=name, docstatus=0, owner=CLERK, modified_by=CLERK, modified=MODIFIED, mpesa_settings="WORKING",
               amount=3000.0, supplier="ACME", supplier_mpesa_account="MSA-1", command_id="BusinessPayBill",
               receiver_shortcode="888880", account_reference="ACME-1", purchase_invoice="PINV-1")
    row.update(values)
    env.frappe.db.table(B2B)[name] = row
    return row


def disbursement(env, name="B2C-1", **values):
    rows = [fake_frappe._dict(name="R1", party_type="Employee", party="EMP-1", partyb="254712345678",
                              allocated_amount=1500.0, reference_doctype="Expense Claim", reference_name="EC-1"),
            fake_frappe._dict(name="R2", party_type="Employee", party="EMP-1", partyb="0712345679",
                              allocated_amount=500.0, reference_doctype="Expense Claim", reference_name="EC-2")]
    row = dict(name=name, docstatus=0, owner=CLERK, modified_by=CLERK, modified=MODIFIED, party_type="Employee",
               mode_of_payment="Mpesa-WORKING", references=rows)
    row.update(values)
    env.frappe.db.table(B2C)[name] = row
    return row


def account(env, name="MSA-9", **values):
    row = dict(name=name, docstatus=0, owner=CLERK, entered_by=CLERK, modified_by=CLERK, modified=MODIFIED,
               supplier="ACME", account_type="Paybill", shortcode="888880", account_number="ACME-1",
               expected_name="ACME HARDWARE", proof="/private/files/letter.pdf")
    row.update(values)
    env.frappe.db.table(SUPPLIER_ACCOUNT)[name] = row
    return row


def prepared(env, doctype=B2B, name="MB2B-1"):
    return env.sc.prepare_send(doctype, name)


# ---------------------------------------------------------------- the summary and its token
def test_the_summary_shows_receiver_destination_amount_and_maker(env):
    b2b(env)
    r = prepared(env)
    assert r["token"] and r["delay"] == 5 and r["expires_in"] == 600 and r["problems"] == []
    assert r["rows"] == [{"receiver": "Acme Hardware Ltd", "party": "ACME", "expected_name": "ACME HARDWARE",
                          "destination": "Paybill 888880, account ACME-1", "account_holder": None, "amount": 3000.0,
                          "charge": None, "document": "Purchase Invoice PINV-1"}]
    assert (r["total"], r["pay_from"], r["shortcode"], r["prepared_by"], r["changed_by"]) == (
        3000.0, "WORKING", "4139683", CLERK, CLERK)
    assert r["charge"] is None and "after it pays" in r["charge_note"]


def test_the_token_is_bound_to_the_user_document_modified_amount_and_destination(env):
    b2b(env)
    r = prepared(env)
    key = f"mpesa_suite:send_token:{r['token']}"
    bound = env.cache.values[key]
    assert env.cache.expiry[key] == 600
    assert set(bound) == {"user", "doctype", "name", "modified", "amount", "destination", "issued_at"}
    assert (bound["user"], bound["doctype"], bound["name"], bound["modified"], bound["issued_at"]) == (
        PAYER, B2B, "MB2B-1", MODIFIED, 1000.0)
    assert len(bound["amount"]) == len(bound["destination"]) == 64  # fingerprints, not the values


def test_a_till_and_a_typed_number_read_as_such(env):
    b2b(env, command_id="BusinessBuyGoods", receiver_shortcode="5123456", account_reference=None,
        supplier=None, supplier_mpesa_account=None, purchase_invoice=None)
    row = prepared(env)["rows"][0]
    assert row["destination"] == "Till 5123456 (a number not saved on a supplier)"
    assert row["receiver"] == "Not saved on a supplier" and row["document"] is None


BANK = "Bank Account (via Bank Paybill)"


def test_a_supplier_bank_account_names_its_bank_and_its_holder(env):
    # round 4: Supplier M-Pesa Accounts hold bank accounts too; the bank's name is its paybill row's
    env.frappe.db.table("M-Pesa Bank Paybill")["EQUITY"] = {"name": "EQUITY", "institution_name": "Equity Bank"}
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-2"] = {
        "name": "MSA-2", "expected_name": "EQUITY BANK", "account_type": BANK, "bank_paybill": "EQUITY", "bank": None,
        "account_holder_name": "Acme Hardware Ltd"}
    b2b(env, supplier_mpesa_account="MSA-2", receiver_shortcode="247247", account_reference="0123456789")
    row = prepared(env)["rows"][0]
    assert row["destination"] == "Equity Bank (bank paybill 247247), account 0123456789"
    assert (row["account_holder"], row["expected_name"]) == ("Acme Hardware Ltd", "EQUITY BANK")


def test_an_employee_bank_account_without_its_paybill_row_names_the_erpnext_bank(env):
    env.frappe.db.table("Employee M-Pesa Account")["EMA-2"] = {
        "name": "EMA-2", "expected_name": "KCB", "account_type": BANK, "bank_paybill": None, "bank": "KCB Bank",
        "account_holder_name": "Jane Wanjiru"}
    b2b(env, supplier=None, supplier_mpesa_account=None, employee="EMP-1", employee_mpesa_account="EMA-2",
        receiver_shortcode="522522", account_reference="1234567890", purchase_invoice=None)
    row = prepared(env)["rows"][0]
    assert (row["receiver"], row["destination"], row["account_holder"]) == (
        "Jane Wanjiru", "KCB Bank (bank paybill 522522), account 1234567890", "Jane Wanjiru")


def test_b2c_rows_show_each_masked_phone_amount_charge_and_the_total(env, monkeypatch):
    monkeypatch.setattr(env.sc, "_b2c_charge", lambda amount: 15.0 if amount > 1000 else 7.0)
    disbursement(env)
    r = prepared(env, B2C, "B2C-1")
    assert [(x["receiver"], x["destination"], x["amount"], x["charge"], x["document"]) for x in r["rows"]] == [
        ("Jane Wanjiru", "M-Pesa phone 2547****5678", 1500.0, 15.0, "Expense Claim EC-1"),
        ("Jane Wanjiru", "M-Pesa phone 2547****5679", 500.0, 7.0, "Expense Claim EC-2"),
    ]
    assert (r["total"], r["charge"], r["charge_note"], r["pay_from"]) == (2000.0, 22.0, None, "Mpesa-WORKING")


def test_b2c_without_charge_bands_says_so(env):
    disbursement(env)
    r = prepared(env, B2C, "B2C-1")
    assert r["charge"] is None and "No B2C Charge Bands" in r["charge_note"]


def test_the_maker_gets_the_maker_checker_refusal_and_no_token(env):
    b2b(env, owner=PAYER)
    r = prepared(env)
    assert r["token"] is None and env.cache.values == {}
    assert "another person must send it" in r["problems"][0]


def test_maker_checker_switched_off_lets_the_maker_send(env, monkeypatch):
    monkeypatch.setattr(env.mc, "enabled", lambda: False)
    b2b(env, owner=PAYER, modified_by=PAYER)
    assert prepared(env)["token"]


def test_a_scheduled_payment_is_not_sent_by_hand(env):
    b2b(env, schedule_line="L1")
    r = prepared(env)
    assert r["token"] is None and "Pay Selected" in r["problems"][0]


def test_a_form_older_than_the_saved_document_is_refused(env):
    b2b(env)
    with pytest.raises(env.frappe.ValidationError, match="changed since you opened it"):
        env.sc.prepare_send(B2B, "MB2B-1", modified="2026-10-27 09:00:00.000000")
    assert env.sc.prepare_send(B2B, "MB2B-1", modified=MODIFIED)["token"]


# ---------------------------------------------------------------- who may press it
@pytest.mark.parametrize("roles", [{"Accounts User"}, {"Accounts Manager"}, {"HR Manager"}, {"System Manager"},
                                   {"M-Pesa Viewer"}])
def test_only_the_payer_roles_send(env, roles):
    b2b(env)
    env.frappe.user_roles = roles
    with pytest.raises(env.frappe.PermissionError):
        prepared(env)


@pytest.mark.parametrize("roles,allowed", [({"M-Pesa Payer"}, False), ({"System Manager"}, False),
                                           ({"Accounts Manager"}, False), ({"M-Pesa Manager"}, True)])
def test_only_an_mpesa_manager_approves_payee_accounts(env, roles, allowed):
    account(env)
    env.frappe.session.user, env.frappe.user_roles = MANAGER, roles
    if allowed:
        assert prepared(env, SUPPLIER_ACCOUNT, "MSA-9")["token"]
    else:
        with pytest.raises(env.frappe.PermissionError):
            prepared(env, SUPPLIER_ACCOUNT, "MSA-9")


def test_the_shared_administrator_login_is_refused(env):
    b2b(env)
    env.frappe.session.user, env.frappe.user_roles = "Administrator", {"System Manager", "M-Pesa Manager"}
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        prepared(env)
    with pytest.raises(env.frappe.PermissionError, match="Use your own login"):
        env.sc.confirm_send(B2B, "MB2B-1", "t1")


def test_submit_permission_on_the_document_is_needed(env):
    b2b(env)
    env.no_permission = True
    with pytest.raises(env.frappe.PermissionError, match="no submit"):
        prepared(env)


@pytest.mark.parametrize("docstatus,message", [(1, "is sent already"), (2, "is cancelled already")])
def test_only_a_draft_is_sent(env, docstatus, message):
    b2b(env, docstatus=docstatus)
    with pytest.raises(env.frappe.ValidationError, match=message):
        prepared(env)


def test_other_doctypes_are_not_sent_this_way(env):
    with pytest.raises(env.frappe.ValidationError, match="not sent with Send via M-Pesa"):
        env.sc.prepare_send("Sales Invoice", "SINV-1")


# ---------------------------------------------------------------- confirm
def test_confirm_works_after_five_seconds_and_submits_inside_the_deliberate_step(env):
    b2b(env)
    token = prepared(env)["token"]
    with pytest.raises(env.frappe.ValidationError, match="Read the summary first"):
        env.sc.confirm_send(B2B, "MB2B-1", token)
    env.clock[0] += 4.9
    with pytest.raises(env.frappe.ValidationError, match="Read the summary first"):
        env.sc.confirm_send(B2B, "MB2B-1", token)
    assert env.submitted == []  # an early press does not use the token
    env.clock[0] += 0.2
    assert env.sc.confirm_send(B2B, "MB2B-1", token)["docstatus"] == 1
    assert env.submitted == [(B2B, "MB2B-1", [("Send via M-Pesa", (B2B, "MB2B-1"))])]
    assert env.frappe.flags.get(env.sc.FLAG) is None  # the step ended with the submit


def test_a_token_is_used_once_whatever_happens(env):
    b2b(env)
    token = prepared(env)["token"]
    env.clock[0] += 6
    env.refuse_submit = "M-Pesa is down"
    with pytest.raises(env.frappe.ValidationError, match="M-Pesa is down"):
        env.sc.confirm_send(B2B, "MB2B-1", token)
    env.refuse_submit = None
    with pytest.raises(env.frappe.ValidationError, match="expired or was used already"):
        env.sc.confirm_send(B2B, "MB2B-1", token)


def test_an_expired_token_is_refused(env):
    b2b(env)
    token = prepared(env)["token"]
    env.clock[0] += 601
    env.cache.values.clear()  # Redis dropped it after 600 seconds
    with pytest.raises(env.frappe.ValidationError, match="expired or was used already"):
        env.sc.confirm_send(B2B, "MB2B-1", token)


def test_no_token_is_refused(env):
    b2b(env)
    with pytest.raises(env.frappe.ValidationError, match="Press Send via M-Pesa first"):
        env.sc.confirm_send(B2B, "MB2B-1", " ")


def test_the_token_is_for_its_user_only(env):
    b2b(env)
    token = prepared(env)["token"]
    env.clock[0] += 6
    env.frappe.session.user = OTHER_PAYER
    with pytest.raises(env.frappe.ValidationError, match="made for another user"):
        env.sc.confirm_send(B2B, "MB2B-1", token)
    assert env.submitted == []


def test_the_token_is_for_its_document_only(env):
    b2b(env)
    b2b(env, name="MB2B-2")
    token = prepared(env)["token"]
    env.clock[0] += 6
    with pytest.raises(env.frappe.ValidationError, match="is for MB2B-1, not MB2B-2"):
        env.sc.confirm_send(B2B, "MB2B-2", token)


@pytest.mark.parametrize("change,message", [
    ({"modified": "2026-10-27 10:00:05.000000"}, "it was saved again"),
    ({"amount": 30000.0}, "the amount"),  # a db write that left modified alone
    ({"receiver_shortcode": "247247"}, "who is paid or where"),
    ({"supplier_mpesa_account": "MSA-2"}, "who is paid or where"),
    ({"purchase_invoice": "PINV-2"}, "who is paid or where"),
])
def test_a_change_after_the_summary_refuses_the_confirm(env, change, message):
    row = b2b(env)
    token = prepared(env)["token"]
    env.clock[0] += 6
    row.update(change)
    with pytest.raises(env.frappe.ValidationError, match=message):
        env.sc.confirm_send(B2B, "MB2B-1", token)
    assert env.submitted == []


def test_a_b2c_row_changed_after_the_summary_refuses_the_confirm(env):
    row = disbursement(env)
    token = prepared(env, B2C, "B2C-1")["token"]
    env.clock[0] += 6
    row["references"][1]["partyb"] = "254799999999"
    with pytest.raises(env.frappe.ValidationError, match="who is paid or where"):
        env.sc.confirm_send(B2C, "B2C-1", token)


def test_send_for_test_skips_the_wait_in_tests_only(env):
    b2b(env)
    doc = env.frappe.get_doc(B2B, "MB2B-1")
    with pytest.raises(env.frappe.ValidationError, match="tests only"):
        env.sc.send_for_test(doc)
    env.frappe.flags.in_test = True
    assert env.sc.send_for_test(doc).docstatus == 1
    assert env.submitted == [(B2B, "MB2B-1", [("Send via M-Pesa", (B2B, "MB2B-1"))])]


def test_send_for_test_says_why_it_cannot_send(env):
    b2b(env, owner=PAYER)
    env.frappe.flags.in_test = True
    with pytest.raises(env.frappe.ValidationError, match="another person must send it"):
        env.sc.send_for_test(env.frappe.get_doc(B2B, "MB2B-1"))


# ---------------------------------------------------------------- approving a payee account
def test_an_account_summary_shows_the_destination_and_who_entered_it(env):
    account(env)
    env.frappe.session.user, env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    r = prepared(env, SUPPLIER_ACCOUNT, "MSA-9")
    assert r["action"] == "Approve" and r["total"] is None and r["token"]
    assert r["rows"][0]["destination"] == "Paybill 888880, account ACME-1"
    assert (r["rows"][0]["receiver"], r["rows"][0]["expected_name"]) == ("Acme Hardware Ltd", "ACME HARDWARE")
    assert (r["entered_by"], r["proof"]) == (CLERK, "/private/files/letter.pdf")


def test_approving_a_bank_account_shows_the_bank_and_whose_account_it_is(env):
    env.frappe.db.table("M-Pesa Bank Paybill")["EQUITY"] = {"name": "EQUITY", "institution_name": "Equity Bank"}
    account(env, account_type=BANK, bank_paybill="EQUITY", shortcode="247247", account_number="0123456789",
            account_holder_name="Someone Else Ltd", expected_name="EQUITY BANK")
    env.frappe.session.user, env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    row = prepared(env, SUPPLIER_ACCOUNT, "MSA-9")["rows"][0]
    assert row["destination"] == "Equity Bank (bank paybill 247247), account 0123456789"
    assert (row["account_holder"], row["document"]) == ("Someone Else Ltd", BANK)


@pytest.mark.parametrize("values,message", [
    (dict(entered_by=MANAGER), "you entered or changed the details"),
    (dict(owner=MANAGER), "you entered or changed the details"),
    (dict(proof=None), "Attach the supplier's proof"),
])
def test_an_account_the_approver_cannot_approve_gets_no_token(env, values, message):
    account(env, **values)
    env.frappe.session.user, env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    r = prepared(env, SUPPLIER_ACCOUNT, "MSA-9")
    assert r["token"] is None and message in " ".join(r["problems"])


def test_an_employee_cannot_approve_their_own_account(env):
    env.frappe.db.table(EMPLOYEE_ACCOUNT)["EMA-1"] = dict(
        name="EMA-1", docstatus=0, owner=CLERK, entered_by=CLERK, modified_by=CLERK, modified=MODIFIED,
        employee="EMP-1", account_type="Phone (B2C)", phone="254712345678", expected_name="JANE WANJIRU",
        proof="/private/files/reg.pdf")
    env.frappe.session.user, env.frappe.user_roles = "jane@x", {"M-Pesa Manager"}
    r = prepared(env, EMPLOYEE_ACCOUNT, "EMA-1")
    assert r["rows"][0]["destination"] == "M-Pesa phone 2547****5678"
    assert r["token"] is None and "your own M-Pesa account" in r["problems"][0]


# ---------------------------------------------------------------- final review: who an Expense Claim payee payout pays
CLAIM_PAYEE = "M-Pesa Claim Payee"
SUITE = "M-Pesa Suite Settings"


def claim_payees(env, *lines, allow=1, max_amount=0, daily_cap=0):
    """Expense Claim EC-9 of Jane Wanjiru (EMP-1) with payee ``lines`` (M-Pesa Claim Payee rows)."""
    env.frappe.db.table("Expense Claim")["EC-9"] = {"name": "EC-9", "employee": "EMP-1", "employee_name": "Jane Wanjiru"}
    env.frappe.db.table(SUITE)[SUITE] = {"allow_claim_payees": allow, "claim_payee_max_amount": max_amount,
                                         "claim_payee_daily_cap": daily_cap}
    for i, values in enumerate(lines, start=1):
        row = dict(name=f"CP-{i}", idx=i, parent="EC-9", parenttype="Expense Claim", **values)
        env.frappe.db.table(CLAIM_PAYEE)[row["name"]] = row


def payee_b2b(env, **values):
    return b2b(env, **dict(dict(supplier=None, supplier_mpesa_account=None, purchase_invoice=None, employee="EMP-1",
                                expense_claim="EC-9", claim_payee="CP-1", command_id="BusinessBuyGoods",
                                receiver_shortcode="835811", account_reference=None), **values))


def test_a_merchant_payee_is_the_receiver_not_the_claimant(env):
    claim_payees(env, dict(payee_type="Till (Buy Goods)", payee_name="Shell Kilimani", shortcode="835811", amount=3000))
    payee_b2b(env)
    r = prepared(env)
    assert r["rows"] == [{"receiver": "Shell Kilimani", "party": "payee on EC-9, claimant Jane Wanjiru",
                          "expected_name": None,
                          "destination": "Till 835811 (payee of Expense Claim EC-9, not an approved account)",
                          "account_holder": None, "amount": 3000.0, "charge": None,
                          "document": "Expense Claim EC-9, payee row #1"}]
    assert r["token"] and r["problems"] == []


def test_a_bank_payee_names_its_bank_and_a_paybill_its_account(env):
    env.frappe.db.table("M-Pesa Bank Paybill")["EQUITY"] = {"name": "EQUITY", "institution_name": "Equity Bank"}
    claim_payees(env, dict(payee_type=BANK, payee_name="Mama Mboga Ltd", bank_paybill="EQUITY", shortcode="247247",
                           account_number="0123456789", amount=3000),
                 dict(payee_type="Paybill", payee_name="Nairobi County", shortcode="647647", account_number="PARK-7",
                      amount=3000))
    payee_b2b(env, command_id="BusinessPayBill", receiver_shortcode="247247", account_reference="0123456789")
    row = prepared(env)["rows"][0]
    assert row["destination"] == ("Equity Bank (bank paybill 247247), account 0123456789 (payee of Expense Claim EC-9, "
                                  "not an approved account)")
    assert row["receiver"] == "Mama Mboga Ltd"
    payee_b2b(env, claim_payee="CP-2", command_id="BusinessPayBill", receiver_shortcode="647647",
              account_reference="PARK-7")
    row = prepared(env)["rows"][0]
    assert (row["receiver"], row["document"]) == ("Nairobi County", "Expense Claim EC-9, payee row #2")
    assert row["destination"].startswith("Paybill 647647, account PARK-7 (payee of Expense Claim EC-9")


def test_a_payee_that_is_a_suppliers_approved_account_shows_its_name(env, monkeypatch):
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    monkeypatch.setattr(cp, "approved_supplier_account", lambda line: "MSA-1")
    claim_payees(env, dict(payee_type="Till (Buy Goods)", payee_name="Acme", shortcode="835811", amount=3000))
    payee_b2b(env)
    row = prepared(env)["rows"][0]
    assert row["destination"] == "Till 835811 (a supplier's approved account, MSA-1)"
    assert row["expected_name"] == "ACME HARDWARE"


def test_casual_payees_in_one_batch_are_each_named(env):
    claim_payees(env, dict(payee_type="Phone (B2C)", payee_name="Peter Otieno", phone="254711000111", amount=800),
                 dict(payee_type="Phone (B2C)", payee_name="Mary Akinyi", phone="254711000222", amount=700))
    rows = [fake_frappe._dict(name=f"R{i}", party_type="Employee", party="EMP-1", partyb=phone, allocated_amount=amount,
                              reference_doctype="Expense Claim", reference_name="EC-9", claim_payee=f"CP-{i}")
            for i, (phone, amount) in enumerate((("254711000111", 800.0), ("254711000222", 700.0)), start=1)]
    disbursement(env, references=rows, company="Test Co")
    r = prepared(env, B2C, "B2C-1")
    assert [(x["receiver"], x["party"], x["destination"]) for x in r["rows"]] == [
        ("Peter Otieno", "payee on EC-9, claimant Jane Wanjiru",
         "M-Pesa phone 2547****0111 (payee of Expense Claim EC-9, not an approved account)"),
        ("Mary Akinyi", "payee on EC-9, claimant Jane Wanjiru",
         "M-Pesa phone 2547****0222 (payee of Expense Claim EC-9, not an approved account)"),
    ]
    assert r["total"] == 1500.0 and r["token"]


@pytest.mark.parametrize("settings,message", [
    (dict(allow=0), "switched off"),
    (dict(max_amount=1000), "EC-9: Row #1: KES 3,000 is more than may be paid at once"),
])
def test_the_payee_switch_and_caps_are_said_before_the_token(env, settings, message):
    claim_payees(env, dict(payee_type="Till (Buy Goods)", payee_name="Shell Kilimani", shortcode="835811", amount=3000),
                 **settings)
    payee_b2b(env)
    r = prepared(env)
    assert r["token"] is None and message in " ".join(r["problems"])


def test_the_days_payee_cap_is_said_before_the_token(env, monkeypatch):
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    seen = []
    monkeypatch.setattr(cp, "sent_today", lambda company, exclude=(None, None), for_update=False:
                        seen.append((company, exclude)) or 4000.0)
    claim_payees(env, dict(payee_type="Till (Buy Goods)", payee_name="Shell Kilimani", shortcode="835811", amount=3000),
                 daily_cap=5000)
    payee_b2b(env, company="Test Co")
    r = prepared(env)
    assert r["token"] is None and "with this KES 3,000 it would pass the day's cap of KES 5,000" in r["problems"][0]
    assert seen == [("Test Co", (B2B, "MB2B-1"))]  # this payout is not counted twice
    monkeypatch.setattr(cp, "sent_today", lambda *a, **kw: 2000.0)
    assert prepared(env)["token"]


# ---------------------------------------------------------------- final review: what the send would refuse, said first
def test_nobody_is_offered_to_send_their_own_payout(env):
    env.frappe.session.user = "jane@x"  # EMP-1's user, an M-Pesa Payer
    b2b(env, supplier=None, supplier_mpesa_account=None, purchase_invoice=None, employee="EMP-1",
        expense_claim="EC-1")
    r = prepared(env)
    assert r["token"] is None and "You are EMP-1: another person must pay you." in r["problems"]
    disbursement(env)  # both rows pay EMP-1
    assert "You are EMP-1" in " ".join(prepared(env, B2C, "B2C-1")["problems"])


def test_a_row_paying_their_own_claim_is_refused_even_to_another_party(env):
    env.frappe.db.table("Expense Claim")["EC-1"] = {"name": "EC-1", "employee": "EMP-1"}
    env.frappe.session.user = "jane@x"
    rows = [fake_frappe._dict(name="R1", party_type="Employee", party="EMP-2", partyb="254712345678",
                              allocated_amount=1500.0, reference_doctype="Expense Claim", reference_name="EC-1")]
    disbursement(env, references=rows)
    r = prepared(env, B2C, "B2C-1")
    assert r["token"] is None and "You are EMP-1" in r["problems"][0]


def test_a_hand_made_b2c_request_to_the_payer_is_refused_first(env):
    env.frappe.db.table(B2C_REQUEST)["B2CR-1"] = dict(
        name="B2CR-1", docstatus=0, owner=CLERK, modified_by=CLERK, modified=MODIFIED, party_type="Employee",
        party="EMP-1", phone_number="254712345678", amount=500.0, mpesa_settings="WORKING")
    env.frappe.session.user = "jane@x"
    r = prepared(env, B2C_REQUEST, "B2CR-1")
    assert r["token"] is None and "You are EMP-1" in r["problems"][0]


def test_an_employee_payment_for_no_document_is_for_a_manager_only(env):
    b2b(env, supplier=None, supplier_mpesa_account=None, purchase_invoice=None, employee="EMP-1")
    r = prepared(env)
    assert r["token"] is None and "paid by an M-Pesa" in r["problems"][0]
    env.frappe.session.user, env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    assert prepared(env)["token"]
    env.frappe.session.user, env.frappe.user_roles = PAYER, {"M-Pesa Payer"}
    b2b(env, supplier=None, supplier_mpesa_account=None, purchase_invoice=None, employee="EMP-1", salary_slip="SAL-1")
    assert prepared(env)["token"], "a Salary Slip caps it: a Payer sends it"


# ---------------------------------------------------------------- final review: Approve says what to check
def test_approving_a_third_partys_bank_account_says_so(env):
    env.frappe.db.table("M-Pesa Bank Paybill")["EQUITY"] = {"name": "EQUITY", "institution_name": "Equity Bank"}
    account(env, account_type=BANK, bank_paybill="EQUITY", shortcode="247247", account_number="0123456789",
            account_holder_name="Someone Else Ltd", expected_name="EQUITY BANK")
    env.frappe.session.user, env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    r = prepared(env, SUPPLIER_ACCOUNT, "MSA-9")
    assert r["warnings"] == ["The account holder Someone Else Ltd is not Acme Hardware Ltd: this pays a third party's "
                             "account. Check the proof before approving."]
    assert r["token"], "a warning, not a refusal"
    env.frappe.db.table(SUPPLIER_ACCOUNT)["MSA-9"]["account_holder_name"] = "ACME HARDWARE LTD"
    assert prepared(env, SUPPLIER_ACCOUNT, "MSA-9")["warnings"] == []


def test_approving_an_employees_business_number_says_so(env):
    env.frappe.db.table(EMPLOYEE_ACCOUNT)["EMA-2"] = dict(
        name="EMA-2", docstatus=0, owner=CLERK, entered_by=CLERK, modified_by=CLERK, modified=MODIFIED,
        employee="EMP-1", account_type="Till (Buy Goods)", shortcode="835811", expected_name="JANE SHOP",
        proof="/private/files/reg.pdf")
    env.frappe.session.user, env.frappe.user_roles = MANAGER, {"M-Pesa Manager"}
    r = prepared(env, EMPLOYEE_ACCOUNT, "EMA-2")
    assert "business paybill or till" in r["warnings"][0] and r["token"]
    b2b(env)
    env.frappe.session.user, env.frappe.user_roles = PAYER, {"M-Pesa Payer"}
    assert prepared(env)["warnings"] == []  # payouts carry none


# ---------------------------------------------------------------- assert_deliberate / deliberate
def _doc(doctype=B2B, name="MB2B-1"):
    return fake_frappe._dict(doctype=doctype, name=name)


@pytest.mark.parametrize("doctype,message", [(B2B, "Send via M-Pesa button"), (B2C, "Send via M-Pesa button"),
                                             (B2C_REQUEST, "Send via M-Pesa button"), (REVERSAL, "Send via M-Pesa button"),
                                             (SUPPLIER_ACCOUNT, "Approve button"), (EMPLOYEE_ACCOUNT, "Approve button")])
def test_a_plain_submit_is_refused(env, doctype, message):
    with pytest.raises(env.frappe.ValidationError, match=message):
        env.sc.assert_deliberate(_doc(doctype))


def test_bench_tests_are_refused_too(env):
    env.frappe.flags.in_test = True
    with pytest.raises(env.frappe.ValidationError):
        env.sc.assert_deliberate(_doc())


@pytest.mark.parametrize("flag", ["in_install", "in_migrate", "in_patch"])
def test_install_migrate_and_patches_pass(env, flag):
    env.frappe.flags[flag] = True
    env.sc.assert_deliberate(_doc())


def test_deliberate_for_one_document_lets_only_that_one_through(env):
    with env.sc.deliberate("Pay Selected", _doc()):
        env.sc.assert_deliberate(_doc())
        with pytest.raises(env.frappe.ValidationError):
            env.sc.assert_deliberate(_doc(name="MB2B-2"))
        with pytest.raises(env.frappe.ValidationError):
            env.sc.assert_deliberate(_doc(B2C, "MB2B-1"))
    with pytest.raises(env.frappe.ValidationError):
        env.sc.assert_deliberate(_doc())  # the step is over


def test_deliberate_without_a_document_lets_any_through_and_nests(env):
    with env.sc.deliberate("A server flow"):
        with env.sc.deliberate("Pay Selected", _doc(name="MB2B-2")):
            env.sc.assert_deliberate(_doc())
        env.sc.assert_deliberate(_doc(B2C, "B2C-1"))
        assert env.frappe.flags[env.sc.FLAG] == [("A server flow", None)]
    assert env.frappe.flags.get(env.sc.FLAG) is None


def test_deliberate_ends_when_its_block_fails(env):
    with pytest.raises(RuntimeError), env.sc.deliberate("Pay Selected", _doc()):
        raise RuntimeError("boom")
    with pytest.raises(env.frappe.ValidationError):
        env.sc.assert_deliberate(_doc())


# ---------------------------------------------------------------- the payout controllers (source)
CONTROLLERS = {
    B2B: DOCTYPES / "mpesa_b2b_payment_request" / "mpesa_b2b_payment_request.py",
    B2C: DOCTYPES / "b2c_payment_disbursement" / "b2c_payment_disbursement.py",
    B2C_REQUEST: DOCTYPES / "b2c_disbursement_request" / "b2c_disbursement_request.py",
    REVERSAL: DOCTYPES / "mpesa_reversal_request" / "mpesa_reversal_request.py",
}


def _function(path, name, cls=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope = tree.body
    if cls:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef)).body
    return next(n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name)


def _statements(node):
    """The function's statements, its docstring left out."""
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return [ast.unparse(s) for s in body]


@pytest.mark.parametrize("doctype", sorted(CONTROLLERS))
def test_every_payout_before_submit_starts_with_the_named_login_and_the_deliberate_step(doctype):
    first = _statements(_function(CONTROLLERS[doctype], "before_submit", cls=True))[:2]
    assert first == ["require_named_user()", "send_confirm.assert_deliberate(self)"], doctype


def test_the_reversal_has_maker_checker():
    assert "maker_checker.check(self)" in _statements(_function(CONTROLLERS[REVERSAL], "before_submit", cls=True))


@pytest.mark.parametrize("path,cls,name", [
    (CONTROLLERS[B2C_REQUEST], True, "retry_failed_payment"),
    (CONTROLLERS[B2C], True, "retry_failed_payments"),
    (APP / "money_out" / "schedule.py", False, "prepare_payment"),
    (APP / "money_out" / "schedule.py", False, "confirm_payment"),
    (APP / "money_out" / "party_accounts.py", False, "confirm_receiver_name"),
])
def test_retries_pay_selected_and_receiver_name_confirm_need_a_named_login(path, cls, name):
    assert "require_named_user()" in _statements(_function(path, name, cls=cls))


@pytest.mark.parametrize("path", [APP / "money_out" / "maker_checker.py", APP / "money_out" / "schedule.py"],
                         ids=lambda p: p.name)
def test_no_administrator_exemption_is_left_in_maker_checker(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    compared = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)
                and any(isinstance(c, ast.Constant) and c.value == "Administrator" for c in n.comparators)]
    assert compared == []


def test_plan_again_of_a_line_that_may_have_been_paid_needs_a_named_manager():
    # decision 1: Administrator holds every role, M-Pesa Manager too, so it is refused by name first
    statements = _statements(_function(APP / "money_out" / "schedule.py", "_named_manager"))
    assert statements[0] == "require_named_user()"
    for name in ("plan_again", "_check_b2c_not_paid"):
        assert "_named_manager()" in ast.unparse(_function(APP / "money_out" / "schedule.py", name)), name


@pytest.mark.parametrize("doctype", [B2B, B2C_REQUEST])
def test_a_cancel_of_a_payout_that_may_have_paid_needs_a_named_manager(doctype):
    """Each ``require(ADMIN_ROLES)`` of before_cancel (the payout may have been paid) comes right after
    ``require_named_user()``: ``require`` lets Administrator through."""
    node = _function(CONTROLLERS[doctype], "before_cancel", cls=True)
    found = 0
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if not isinstance(body, list):
            continue
        lines = [ast.unparse(s) for s in body]
        for i, line in enumerate(lines):
            if line == "require(ADMIN_ROLES)":
                found += 1
                assert i and lines[i - 1] == "require_named_user()", lines
    assert found == 2


def test_pay_selected_checks_administrator_like_everyone():
    for name in ("line_problem", "_send_problem"):
        source = ast.unparse(_function(APP / "money_out" / "schedule.py", name))
        assert "Administrator" not in source, name


SERVER_SUBMITS = (APP / "money_out" / "schedule.py", CONTROLLERS[B2C], APP / "sandbox_lab" / "scenarios.py",
                  APP / "money_out" / "send_confirm.py")


@pytest.mark.parametrize("path", SERVER_SUBMITS, ids=lambda p: p.name)
def test_every_server_submit_of_a_payout_is_a_deliberate_step(path):
    """Pay Selected, a disbursement's B2C requests, the Sandbox Lab, confirm_send: each ``.submit()``
    runs inside ``with ... deliberate(...)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inside = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any("deliberate(" in ast.unparse(i.context_expr) for i in node.items):
            inside |= {id(n) for n in ast.walk(node)}
    submits = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "submit"]
    assert submits, path
    assert [ast.unparse(n) for n in submits if id(n) not in inside] == []


# ---------------------------------------------------------------- maker-checker on reversals
def _reversal(**values):
    row = dict(doctype=REVERSAL, name="REV-1", owner=CLERK, c2b_register="C2B-1", transaction_id="TJR12345",
               amount=500.0, mpesa_settings="WORKING")
    row.update(values)
    return row


class SentDoc(fake_frappe._dict):
    @property
    def meta(self):
        return types.SimpleNamespace(get_field=lambda f: fake_frappe._dict(fieldtype="Currency" if f == "amount" else "Data"))

    def get_doc_before_save(self):
        return self.before


@pytest.mark.parametrize("field,value", [("c2b_register", "C2B-2"), ("transaction_id", "TJR99999"), ("amount", 900.0),
                                         ("mpesa_settings", "OTHER")])
def test_a_reversal_cannot_change_while_being_sent(env, field, value):
    before = SentDoc(_reversal(), modified_by=CLERK, before=None)
    env.mc.check(SentDoc(_reversal(), before=before))
    with pytest.raises(env.frappe.ValidationError, match="changed while being sent"):
        env.mc.check(SentDoc(_reversal(**{field: value}), before=before))


def test_who_made_a_reversal_cannot_send_it(env):
    env.frappe.session.user = CLERK
    before = SentDoc(_reversal(), modified_by=CLERK, before=None)
    with pytest.raises(env.frappe.ValidationError, match="another person must send it"):
        env.mc.check(SentDoc(_reversal(), before=before))


# ---------------------------------------------------------------- desk flags for the new role sets
@pytest.mark.parametrize("roles,expected", [
    ({"System Manager"}, dict(can_collect=True, can_configure=True)),
    ({"System Manager", "Accounts Manager"}, dict(can_prepare=True, can_collect=True, can_configure=True)),
    ({"M-Pesa Payer"}, dict(can_pay=True, can_see_salaries=True)),
    ({"M-Pesa Manager"}, dict(can_prepare=True, can_pay=True, can_approve=True, can_collect=True, can_configure=True,
                              can_see_salaries=True)),
])
def test_system_manager_is_offered_neither_send_nor_approve(env, roles, expected):
    boot = importlib.import_module("mpsa_suite.boot")
    env.frappe.session.user, env.frappe.user_roles = "someone@example.com", roles
    env.frappe.has_permission = lambda doctype, ptype="read", *a, **kw: True  # create on Payment Request
    assert boot.flags() == dict(dict.fromkeys(boot.FLAGS, False), maker_checker=True, **expected)


# ---------------------------------------------------------------- M-Pesa Suite Settings alerts
LABELS = {"schedule_maker_checker": ("Check", "Who Sends a Payout Must Not Be Who Prepared It"),
          "refuse_administrator_payouts": ("Check", "Administrator May Not Send Payouts or Approve Accounts"),
          "allow_one_off_b2b": ("Check", "Allow B2B to Numbers Not Saved on a Supplier"),
          "supplier_account_hold_hours": ("Int", "Hold New Supplier M-Pesa Accounts (Hours)"),
          "employee_account_hold_hours": ("Int", "Hold New Employee M-Pesa Accounts (Hours)"),
          "integration_user": ("Link", "Integration User"),
          "notify_role": ("Link", "Notify Role"),
          "b2c_approved_phones_only": ("Check", "Pay Employees by B2C Only to Approved Phones"),
          "allow_claim_payees": ("Check", "Allow Paying Merchants and Casual Payees from Expense Claims"),
          "claim_payee_max_amount": ("Currency",
                                     "Most per Payment to a Payee That Is Not an Approved Account (0 = no cap)"),
          "claim_payee_daily_cap": ("Currency", "Most per Day to Such Payees, per Company (0 = no cap)"),
          "claim_payable_workflow_states": ("Small Text", "Expense Claim Workflow States That May Be Paid"),
          "enforce_ip_allowlist": ("Check", "Enforce IP Allow-list")}
SAFE = dict(schedule_maker_checker=1, refuse_administrator_payouts=1, allow_one_off_b2b=0,
            supplier_account_hold_hours=24, employee_account_hold_hours=24, integration_user="mpesa@x",
            notify_role="Accounts Manager", b2c_approved_phones_only=1, allow_claim_payees=0,
            claim_payee_max_amount=0, claim_payee_daily_cap=0, claim_payable_workflow_states="Approved\nPaid Out",
            enforce_ip_allowlist=0)


@pytest.fixture
def settings(env, monkeypatch):
    env.frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)  # the doctype package's
    env.frappe.logger = lambda *a, **kw: None
    module = importlib.import_module("mpsa_suite.mpsa_suite.doctype.m_pesa_suite_settings.m_pesa_suite_settings")
    accounts = importlib.import_module("mpsa_suite.money_out.party_accounts")
    alerts, comments = [], []
    monkeypatch.setattr(accounts, "alert",
                        lambda message, doc, roles=None: alerts.append((message, doc.doctype, roles)))

    def make(before, **now):
        doc = module.MPesaSuiteSettings()
        values = dict(SAFE, **now)
        doc.doctype = doc.name = "M-Pesa Suite Settings"
        doc.meta = types.SimpleNamespace(get_field=lambda f: fake_frappe._dict(
            fieldtype=LABELS[f][0], label=LABELS[f][1]) if f in LABELS else None)
        doc.get = lambda key, default=None: values.get(key, default)
        doc.get_doc_before_save = lambda: fake_frappe._dict(before) if before is not None else None
        doc.add_comment = lambda kind, text: comments.append((kind, text))
        return doc

    env.frappe.session.user = "it@example.com"
    return make, alerts, comments


def test_switching_off_maker_checker_or_the_administrator_refusal_alerts_the_notify_role(settings):
    make, alerts, comments = settings
    make(SAFE, schedule_maker_checker=0, refuse_administrator_payouts="0").alert_safety_changes()
    message = ("it@example.com changed M-Pesa Suite Settings: Who Sends a Payout Must Not Be Who Prepared It from on "
               "to off; Administrator May Not Send Payouts or Approve Accounts from on to off.")
    assert comments == [("Comment", message)]
    assert alerts == [(message, "M-Pesa Suite Settings", ["Accounts Manager"])]


@pytest.mark.parametrize("change,text", [
    (dict(allow_one_off_b2b=1), "Allow B2B to Numbers Not Saved on a Supplier from off to on"),
    (dict(supplier_account_hold_hours=0), "Hold New Supplier M-Pesa Accounts (Hours) from 24 to 0"),
    (dict(employee_account_hold_hours=2), "Hold New Employee M-Pesa Accounts (Hours) from 24 to 2"),
    (dict(integration_user="someone@x"), "Integration User from mpesa@x to someone@x"),
    (dict(integration_user=None), "Integration User from mpesa@x to not set"),
    (dict(b2c_approved_phones_only=0), "Pay Employees by B2C Only to Approved Phones from on to off"),
    (dict(allow_claim_payees=1), "Allow Paying Merchants and Casual Payees from Expense Claims from off to on"),
    (dict(claim_payee_max_amount=5000),
     "Most per Payment to a Payee That Is Not an Approved Account (0 = no cap) from 0 to 5,000"),
    (dict(claim_payee_daily_cap=12500.5), "Most per Day to Such Payees, per Company (0 = no cap) from 0 to 12,500.50"),
    (dict(notify_role="HR Manager"), "Notify Role from Accounts Manager to HR Manager"),
    # final review: the payable gate of claims (plan C.3): widened, or cleared (any approved claim is payable)
    (dict(claim_payable_workflow_states="Approved\nPaid Out\nPending Attachment"),
     "Expense Claim Workflow States That May Be Paid from Approved, Paid Out to Approved, Paid Out, Pending Attachment"),
    (dict(claim_payable_workflow_states=""),
     "Expense Claim Workflow States That May Be Paid from Approved, Paid Out to not set"),
])
def test_each_payout_safety_change_is_told(settings, change, text):
    make, alerts, comments = settings
    make(SAFE, **change).alert_safety_changes()
    assert text in comments[0][1] and len(alerts) == 1


def test_a_save_that_also_changes_the_notify_role_tells_the_old_role_and_the_new(settings):
    # a role nobody holds, with maker-checker off in the same save: the old role still hears of both
    make, alerts, _comments = settings
    make(SAFE, notify_role="Nobody's Role", schedule_maker_checker=0).alert_safety_changes()
    (message, _doctype, roles), = alerts
    assert roles == ["Accounts Manager", "Nobody's Role"]
    assert "Notify Role from Accounts Manager to Nobody's Role" in message and "Prepared It from on to off" in message
    alerts.clear()
    make(SAFE, notify_role=None).alert_safety_changes()
    assert alerts[0][2] == ["Accounts Manager"]
    alerts.clear()
    make(dict(SAFE, notify_role=None), notify_role=None, allow_one_off_b2b=1).alert_safety_changes()
    assert alerts[0][2] is None  # none set before or after: the alert finds the default role itself


def test_other_changes_and_the_first_save_tell_nobody(settings):
    make, alerts, comments = settings
    make(SAFE, enforce_ip_allowlist=1).alert_safety_changes()  # not a payout-safety switch
    make(dict(SAFE, claim_payee_max_amount=None), claim_payee_max_amount=0).alert_safety_changes()  # no cap either way
    make(dict(SAFE, schedule_maker_checker="1"), schedule_maker_checker=1).alert_safety_changes()  # same value
    # the same states, written with other spacing or blank lines
    make(SAFE, claim_payable_workflow_states=" Approved \n\nPaid Out\n").alert_safety_changes()
    make(None, schedule_maker_checker=0).alert_safety_changes()  # install
    assert alerts == comments == []
