"""Trident live-test fixes (docs/TRIDENT_LIVE_TEST_HANDOVER.md), on an in-memory frappe.

F1: an STK payment and an auto-submitted C2B receipt are booked as Administrator, not as the
callback's Integration User (whom a User Permission may stop reading the order or invoice).
F2.1-F2.3: a till's (Buy Goods) Result: its "{Amount=...BasicAmount=1894.00}" balance is read as
Working's, its charge is the fall in the Working balance (1,947 -> 1,894 for 50: 3), and its
completed time is the Result's arrival, flagged as estimated.
F4: a B2C row whose document owes nothing is refused at save. F5: one cost-center rule for every
payout entry. F7: the Transaction Limit and the Minimum B2C Payout cap every B2C row, at save and
at send.
"""

import datetime
import importlib
import logging
import sys
import types

import pytest

import fake_frappe
import rich_db

B2B = "Mpesa B2B Payment Request"
EXPRESS = "Mpesa Express Request"
LIVE_TILL_BALANCE = "{Amount={CurrencyCode=KES, MinimumAmount=189400, BasicAmount=1894.00}}"


@pytest.fixture
def frappe(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        frappe.get_meta = lambda doctype: types.SimpleNamespace(has_field=lambda field: True)
        frappe.db.db_type = "fake"
        # what importing a DocType module needs (mpsa_suite.mpsa_suite.doctype sets up a logger)
        frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
        frappe.utils.now = lambda: "2026-10-18 12:00:00"
        frappe.logger = lambda *a, **kw: logging.getLogger("fake_frappe")
        yield frappe
    finally:
        fake_frappe.uninstall(saved)


# ================================================================ F1
class Express(fake_frappe.FakeDoc):
    def validate_duplicate_c2b_records(self):
        self._frappe.calls.append(("neutralise", self._frappe.session.user))

    def book_completed_payment(self):
        self._frappe.calls.append(("book", self._frappe.session.user))
        if self._row.get("fail"):
            raise RuntimeError("PermissionError: Sales Order SO-1")


@pytest.fixture
def stk(frappe, monkeypatch):
    stk_verify = importlib.import_module("mpsa_suite.money_in.stk_verify")
    frappe.calls = []
    frappe.session.user = "mpesa-integration@example.com"
    frappe.db.table(EXPRESS)["MEXP-1"] = {"name": "MEXP-1", "status": "Completed", "transaction_id": "UJR1STK000",
                                          "is_reconciled": 0, "booking_attempts": 0}
    plain = frappe.get_doc
    frappe.get_doc = lambda dt, name=None, **kw: (
        Express(frappe, dt, frappe.db.table(dt)[name]) if dt == EXPRESS else plain(dt, name, **kw))
    monkeypatch.setattr(stk_verify, "receipt_reversal", lambda receipt, for_update=False: None)
    return stk_verify


def test_stk_payment_is_booked_as_administrator(frappe, stk):
    assert stk.book("MEXP-1") is True
    assert frappe.calls == [("neutralise", "Administrator"), ("book", "Administrator")]
    assert frappe.session.user == "mpesa-integration@example.com", "the callback's user is restored"


def test_failed_stk_booking_restores_the_user_and_is_retried(frappe, stk):
    frappe.db.table(EXPRESS)["MEXP-1"]["fail"] = True
    assert stk.book("MEXP-1") is False
    assert frappe.session.user == "mpesa-integration@example.com"
    assert frappe.db.table(EXPRESS)["MEXP-1"]["booking_attempts"] == 1
    assert "M-Pesa STK booking failed" in frappe.errors[0][0][1]


def stub_payment_entry_api(monkeypatch):
    stub = types.ModuleType("mpsa_suite.mpsa_suite.api.payment_entry")
    stub.create_and_reconcile_payment_reconciliation = stub.create_payment_entry = lambda *a, **kw: None
    stub.get_outstanding_invoices = lambda *a, **kw: []
    monkeypatch.setitem(sys.modules, stub.__name__, stub)


@pytest.fixture
def register(frappe, monkeypatch):
    stub_payment_entry_api(monkeypatch)
    module = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.mpesa_c2b_payment_register.mpesa_c2b_payment_register")
    common = importlib.import_module("mpsa_suite.money_in.common")
    monkeypatch.setattr(common, "settings_for_shortcode", lambda shortcode: "985401")
    frappe.db.table("Mpesa Settings")["985401"] = {"name": "985401", "auto_reconcile_c2b": 1}
    frappe.session.user = "mpesa-integration@example.com"
    submitted = []

    class Register(module.MpesaC2BPaymentRegister):
        businessshortcode = "985401"
        docstatus = 0
        payment_entry = None

        def db_set(self, field, value=None, **kw):
            pass

        def submit(self):
            submitted.append(frappe.session.user)
            if getattr(self, "fail", False):
                raise RuntimeError("Not permitted")

    return fake_frappe._dict(Register=Register, submitted=submitted)


def test_c2b_receipt_is_auto_submitted_as_administrator(frappe, register):
    register.Register().after_insert()
    assert register.submitted == ["Administrator"]
    assert frappe.session.user == "mpesa-integration@example.com"


def test_failed_c2b_auto_submit_keeps_the_draft_and_the_callbacks_user(frappe, register):
    doc = register.Register()
    doc.fail = True
    doc.after_insert()
    assert register.submitted == ["Administrator"] and doc.docstatus == 0
    assert frappe.session.user == "mpesa-integration@example.com"
    assert "C2B Auto-submit Error" in frappe.errors[0][0][1]


# ================================================================ F2.1
def test_a_tills_balance_is_read_as_working():
    daraja = importlib.import_module("mpsa_suite.daraja.money_out")
    assert daraja.basic_amount(LIVE_TILL_BALANCE) == 1894.0
    assert daraja.basic_amount("{Amount={BasicAmount=1,476.50}}") == 1476.5
    assert daraja.basic_amount("Working Account|KES|1947.00") is None
    text = daraja.b2b_balance_text(LIVE_TILL_BALANCE)
    assert text == "Working Account|KES|1894.00|1894.00"
    pipe = "Working Account|KES|1947.00|1947.00|0.00|0.00"
    assert daraja.b2b_balance_text(pipe) == pipe  # the paybill's form: kept
    assert daraja.b2b_balance_text("{Amount=garbage}") == "{Amount=garbage}"  # unreadable: kept raw
    assert daraja.b2b_balance_text(None) is None


def test_the_tills_balance_counts_as_available_funds(frappe):
    balances = importlib.import_module("mpsa_suite.wallet.balances")
    daraja = importlib.import_module("mpsa_suite.daraja.money_out")
    assert balances.parse_available(daraja.b2b_balance_text(LIVE_TILL_BALANCE)) == {"Working": 1894.0}
    assert balances.parse_available(LIVE_TILL_BALANCE) == {}  # what was stored before


# ================================================================ F2.3 (and the F2.4 trigger)
def b2b_result(params, conv="AG_20261027_0100203101", occ="8a05-481f-85be-737ec5465783134990"):
    return {"ResultType": "0", "ResultCode": "0", "ResultDesc": "The service request is processed successfully.",
            "OriginatorConversationID": occ, "ConversationID": conv, "TransactionID": "UJRSN5BOEH",
            "ResultParameters": {"ResultParameter": [{"Key": k, "Value": v} for k, v in params]}}


TILL = [("DebitAccountCurrentBalance", LIVE_TILL_BALANCE), ("InitiatorAccountCurrentBalance", LIVE_TILL_BALANCE),
        ("Amount", "50.00")]
PAYBILL = [("DebitPartyAffectedAccountBalance", "Working Account|KES|1947.00|1947.00|0.00|0.00"),
           ("Amount", "50.00"), ("TransCompletedTime", "20261027143856"),
           ("DebitPartyCharges", "Business Pay Bill Charge|KES|3.00"),
           ("ReceiverPartyPublicName", "208911 - ASTRAL INDUSTRIES LIMITED")]


@pytest.fixture
def b2b(frappe):
    module = importlib.import_module("mpsa_suite.money_out.b2b")
    frappe.db.table(B2B)["MB2B-26-10-000002"] = {
        "name": "MB2B-26-10-000002", "docstatus": 1, "status": "Initiated", "amount": 50.0,
        "command_id": "BusinessBuyGoods", "receiver_shortcode": "835811", "supplier": "Astral Industries Ltd",
        "conversation_id": "AG_20261027_0100203101", "originator_conversation_id": "8a05-481f-85be-737ec5465783134990",
        "mpesa_settings": "985401"}
    return module


def test_a_tills_result_is_completed_at_its_arrival_flagged_and_queried(frappe, b2b):
    b2b.handle_result(b2b_result(TILL), "MB2B-26-10-000002")
    row = frappe.db.table(B2B)["MB2B-26-10-000002"]
    assert row["status"] == "Paid"
    assert row["trans_completed_time"] == frappe.utils.now_datetime() and row["completed_time_estimated"] == 1
    assert row["debit_account_balance"] == "Working Account|KES|1894.00|1894.00"
    jobs = [method for method, _kw in frappe.enqueued]
    assert "mpsa_suite.money_out.b2b.book_payment_entry" in jobs
    (query,) = [kw for method, kw in frappe.enqueued if method.endswith("b2b_status.auto_query")]
    assert query["name"] == "MB2B-26-10-000002" and query["enqueue_after_commit"]


def test_a_paybills_result_keeps_safaricoms_time_and_needs_no_query(frappe, b2b):
    b2b.handle_result(b2b_result(PAYBILL), "MB2B-26-10-000002")
    row = frappe.db.table(B2B)["MB2B-26-10-000002"]
    assert row["trans_completed_time"] == datetime.datetime(2026, 10, 27, 14, 38, 56)
    assert row["completed_time_estimated"] == 0
    assert row["debit_account_balance"] == "Working Account|KES|1947.00|1947.00|0.00|0.00"
    assert not [m for m, _kw in frappe.enqueued if m.endswith("auto_query")]


def test_a_failed_result_is_not_given_a_time(frappe, b2b):
    b2b.handle_result(dict(b2b_result([]), ResultCode="2001", ResultParameters=None), "MB2B-26-10-000002")
    row = frappe.db.table(B2B)["MB2B-26-10-000002"]
    assert row["status"] == "Failed" and row["trans_completed_time"] is None and row["completed_time_estimated"] == 0


# ================================================================ F2.2
@pytest.fixture
def wallet(frappe, monkeypatch):
    rich_db.install(frappe)
    statement = importlib.import_module("mpsa_suite.money_out.statement")
    frappe.db.table("Mpesa Settings")["985401"] = {"name": "985401", "business_shortcode": "985401",
                                                    "creation": datetime.datetime(2026, 1, 1)}

    def b2b_row(name, at, balance, amount=50.0, status="Paid", **kw):
        frappe.db.table(B2B)[name] = {
            "name": name, "docstatus": 1, "status": status, "amount": amount, "mpesa_settings": "985401",
            "transaction_id": f"UJR{name[-4:]}X", "trans_completed_time": at, "debit_account_balance": balance,
            "creation": (at or datetime.datetime(2026, 10, 27, 14, 30)) - datetime.timedelta(seconds=5),
            "debit_party_charges": "", **kw}
        return name

    return fake_frappe._dict(statement=statement, row=b2b_row)


PAYBILL_AT = datetime.datetime(2026, 10, 27, 14, 38, 56)
TILL_AT = datetime.datetime(2026, 10, 27, 14, 39, 1)


def charge(wallet, name):
    return wallet.statement.b2b_balance_charge(wallet.statement.load(B2B, name))


def test_tills_charge_is_the_fall_in_the_working_balance(wallet):
    wallet.row("MB2B-26-10-000001", PAYBILL_AT, "Working Account|KES|1947.00|1947.00|0.00|0.00",
               debit_party_charges="Business Pay Bill Charge|KES|3.00")
    wallet.row("MB2B-26-10-000002", TILL_AT, "Working Account|KES|1894.00|1894.00")
    assert charge(wallet, "MB2B-26-10-000002") == 3.0


def test_no_balance_before_gives_no_charge(wallet):
    wallet.row("MB2B-26-10-000002", TILL_AT, "Working Account|KES|1894.00|1894.00")
    assert charge(wallet, "MB2B-26-10-000002") is None


@pytest.mark.parametrize("between", [
    {"status": "Initiated", "at": None, "balance": None},          # in flight: it may have moved the wallet
    {"status": "Paid", "at": datetime.datetime(2026, 10, 27, 14, 38, 59), "balance": None},  # left no balance
])
def test_a_b2b_in_between_hides_the_charge(wallet, between):
    wallet.row("MB2B-26-10-000001", PAYBILL_AT, "Working Account|KES|1947.00|1947.00|0.00|0.00")
    wallet.row("MB2B-26-10-000003", between["at"], between["balance"], status=between["status"])
    wallet.row("MB2B-26-10-000002", TILL_AT, "Working Account|KES|1894.00|1894.00")
    assert charge(wallet, "MB2B-26-10-000002") is None


def test_a_later_account_balance_is_the_balance_before(wallet, frappe):
    wallet.row("MB2B-26-10-000001", PAYBILL_AT - datetime.timedelta(hours=1), "Working Account|KES|5000.00|5000.00")
    frappe.db.table("Mpesa Balance Snapshot")["S1"] = {
        "name": "S1", "shortcode": "985401", "sub_account": "Working", "source": "Account Balance",
        "taken_at": datetime.datetime(2026, 10, 27, 14, 0), "balance": 1947.0}
    wallet.row("MB2B-26-10-000002", TILL_AT, "Working Account|KES|1894.00|1894.00")
    assert charge(wallet, "MB2B-26-10-000002") == 3.0


@pytest.mark.parametrize("before", ["Working Account|KES|5000.00|5000.00", "Working Account|KES|1943.50|1943.50",
                                    "Working Account|KES|1940.00|1940.00"])
def test_a_fall_that_is_no_charge_gives_none(wallet, before):
    # a funding or a sweep in between (too much), half shillings, or less than the amount
    wallet.row("MB2B-26-10-000001", PAYBILL_AT, before)
    wallet.row("MB2B-26-10-000002", TILL_AT, "Working Account|KES|1894.00|1894.00")
    assert charge(wallet, "MB2B-26-10-000002") is None


# ================================================================ F4 and F7
class Q:
    def __getattr__(self, key):
        return self

    def __call__(self, *a, **kw):
        return self


@pytest.fixture
def disbursement(frappe, monkeypatch):
    query_builder = types.ModuleType("frappe.query_builder")
    query_builder.DocType = lambda name: Q()
    monkeypatch.setitem(sys.modules, "frappe.query_builder", query_builder)
    mod = importlib.import_module(
        "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement")
    owed = {"HR-EXP-2026-00710": 0.0, "HR-EXP-2026-00711": 150.0}
    monkeypatch.setattr(mod, "current_outstanding", lambda dt, dn, for_update=False: owed.get(dn))
    frappe.db.table("Mpesa Settings")["985401"] = {"name": "985401", "payment_gateway_name": "985401",
                                                    "transaction_limit": 100.0}

    class Doc(mod.B2CPaymentDisbursement):
        def __init__(self, rows, mode_of_payment="Mpesa-985401"):
            self.references = [fake_frappe._dict({"idx": i, "name": f"R{i}", "reference_doctype": "Expense Claim",
                                                  "b2c_disbursement_request": None, **row})
                               for i, row in enumerate(rows, 1)]
            self.mode_of_payment = mode_of_payment

    return fake_frappe._dict(mod=mod, Doc=Doc, owed=owed)


def test_a_row_whose_claim_owes_nothing_is_refused_at_save(frappe, disbursement):
    doc = disbursement.Doc([{"reference_name": "HR-EXP-2026-00710", "allocated_amount": 100}])
    with pytest.raises(frappe.ValidationError, match="Row #1: Expense Claim HR-EXP-2026-00710 has nothing left to pay"):
        doc.set_outstanding_from_documents()


def test_a_part_paid_claim_and_an_untracked_row_save(frappe, disbursement):
    doc = disbursement.Doc([{"reference_name": "HR-EXP-2026-00711", "allocated_amount": 100},
                            {"reference_name": "UNTRACKED", "allocated_amount": 100, "outstanding_amount": 7}])
    doc.set_outstanding_from_documents()
    assert [r.outstanding_amount for r in doc.references] == [150.0, 7]


def test_an_amended_copy_of_a_paid_row_still_saves(frappe, disbursement):
    frappe.db.table("B2C Disbursement Request")["B2C-1"] = {"name": "B2C-1", "status": "Paid"}
    doc = disbursement.Doc([{"reference_name": "HR-EXP-2026-00710", "allocated_amount": 100,
                             "b2c_disbursement_request": "B2C-1"}])
    doc.set_outstanding_from_documents()  # sent already: not sent again, so not refused
    frappe.db.table("B2C Disbursement Request")["B2C-1"]["status"] = "Failed"  # a failed row is sent again
    with pytest.raises(frappe.ValidationError, match="nothing left to pay"):
        doc.set_outstanding_from_documents()


def test_the_transaction_limit_caps_every_row_at_save(frappe, disbursement):
    disbursement.Doc([{"reference_name": "HR-EXP-2026-00711", "allocated_amount": 100}]).validate_payout_limits()
    doc = disbursement.Doc([{"reference_name": "HR-EXP-2026-00711", "allocated_amount": 150}])
    with pytest.raises(frappe.ValidationError, match="Row #1: Amount is above this shortcode's transaction limit"):
        doc.validate_payout_limits()
    frappe.db.table("Mpesa Settings")["985401"]["transaction_limit"] = 0  # no limit
    doc.validate_payout_limits()


def test_the_minimum_payout_setting_applies_at_save(frappe, disbursement):
    frappe.get_cached_doc = lambda doctype, *a, **kw: fake_frappe._dict(min_payout_amount=50)
    doc = disbursement.Doc([{"reference_name": "HR-EXP-2026-00711", "allocated_amount": 40}])
    with pytest.raises(frappe.ValidationError, match="at least KES 50.00"):
        doc.validate_payout_limits()


@pytest.fixture
def service(frappe):
    frappe.db.table("Mpesa Settings")["985401"] = {"name": "985401", "transaction_limit": 100.0}
    return importlib.import_module("mpsa_suite.services.b2c_request_service")


def request(amount):
    return fake_frappe._dict(name="B2C-1", amount=amount, mpesa_settings="985401", phone_number="254712345678")


def test_the_transaction_limit_caps_a_b2c_at_send(frappe, service):
    service.validate_b2c_amount_and_party(request(100))
    with pytest.raises(frappe.ValidationError, match="transaction limit of 100.00"):
        service.validate_b2c_amount_and_party(request(150))
    frappe.db.table("Mpesa Settings")["985401"]["transaction_limit"] = 0
    service.validate_b2c_amount_and_party(request(150))


def test_the_minimum_payout_never_goes_below_darajas_ten(frappe, service):
    assert service.min_payout_amount() == 10.0  # settings not there: Daraja's own minimum
    frappe.get_cached_doc = lambda doctype, *a, **kw: fake_frappe._dict(min_payout_amount=0)
    assert service.min_payout_amount() == 10.0
    frappe.get_cached_doc = lambda doctype, *a, **kw: fake_frappe._dict(min_payout_amount=20)
    assert service.payout_limit_problem(15, "985401").startswith("M-Pesa B2C pays at least KES 20.00")
    assert service.payout_limit_problem(20, "985401") is None


# ================================================================ F5
@pytest.fixture
def centers(frappe, monkeypatch):
    erpnext = types.ModuleType("erpnext")
    erpnext.get_default_cost_center = lambda company: f"Main - {company}"
    dims = types.ModuleType("erpnext.accounts.doctype.accounting_dimension.accounting_dimension")
    dims.get_accounting_dimensions = lambda: ["branch"]
    for name in ("erpnext.accounts", "erpnext.accounts.doctype", "erpnext.accounts.doctype.accounting_dimension"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "erpnext", erpnext)
    monkeypatch.setitem(sys.modules, dims.__name__, dims)
    frappe.db.table("Cost Center")["Project - TPL"] = {"name": "Project - TPL", "company": "TPL"}
    frappe.db.table("Cost Center")["Claims - TPL"] = {"name": "Claims - TPL", "company": "TPL"}
    frappe.db.table("Cost Center")["Other - AIL"] = {"name": "Other - AIL", "company": "AIL"}
    frappe.db.table("Mpesa Settings")["985401"] = {"name": "985401", "cost_center": "Project - TPL"}
    frappe.db.table("Expense Claim")["HR-EXP-1"] = {"name": "HR-EXP-1", "cost_center": "Claims - TPL",
                                                    "project": "TPL-23-013", "branch": None}
    frappe.db.table("Expense Claim")["HR-EXP-2"] = {"name": "HR-EXP-2", "cost_center": None, "project": None}
    return importlib.import_module("mpsa_suite.money_out.common")


def test_payout_cost_center_is_the_document_then_the_shortcode_then_the_company(frappe, centers):
    assert centers.payout_cost_center("985401", "TPL", "Expense Claim", "HR-EXP-1") == "Claims - TPL"
    assert centers.payout_cost_center("985401", "TPL", "Expense Claim", "HR-EXP-2") == "Project - TPL"
    assert centers.payout_cost_center("985401", "TPL") == "Project - TPL"  # a charge: the shortcode's
    assert centers.payout_cost_center(None, "TPL") == "Main - TPL"
    frappe.db.table("Mpesa Settings")["985401"]["cost_center"] = "Other - AIL"  # another company's
    assert centers.payout_cost_center("985401", "TPL") == "Main - TPL"


def test_document_dimensions_are_the_set_ones(frappe, centers):
    assert centers.document_dimensions("Expense Claim", "HR-EXP-1") == {"project": "TPL-23-013"}
    assert centers.document_dimensions("Expense Claim", "HR-EXP-2") == {}
    assert centers.document_dimensions(None, None) == {}


def test_b2c_payment_entry_takes_the_claims_cost_center_and_project(frappe, centers, monkeypatch):
    stub_payment_entry_api(monkeypatch)
    service = importlib.import_module("mpsa_suite.accounting.payment_entry_service")
    made = []
    monkeypatch.setattr(service, "create_payment_entry", lambda *a, **kw: made.append(kw))
    monkeypatch.setattr(service, "b2c_utility_gl", lambda ref: None)
    frappe.db.table("B2C Disbursement Request")["B2C-1"] = {"name": "B2C-1", "mpesa_settings": "985401"}
    frappe.db.table("Employee")["EMP-1"] = {"name": "EMP-1"}
    ref = fake_frappe._dict(party_type="Employee", party="EMP-1", allocated_amount=100,
                            reference_doctype="Expense Claim", reference_name="HR-EXP-1",
                            b2c_disbursement_request="B2C-1", reference_no="UJRASBFI9Y", reference_date=None)
    disbursement = fake_frappe._dict(name="D-1", paid_to="Payable - TPL", company="TPL", mode_of_payment="Mpesa-985401")
    service.create_payment_entry_for_doc(disbursement, ref)
    assert (made[0]["cost_center"], made[0]["dimensions"]) == ("Claims - TPL", {"project": "TPL-23-013"})
    ref.reference_name = "HR-EXP-2"
    service.create_payment_entry_for_doc(disbursement, ref)
    assert (made[1]["cost_center"], made[1]["dimensions"]) == ("Project - TPL", {})
