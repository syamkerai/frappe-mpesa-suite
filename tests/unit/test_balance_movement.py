"""Balance snapshots checked against the ERP's payout records when no statement is imported
(handover F12), on an in-memory frappe, with Trident's live balances of 27 Oct 2026 (shortcode
985401): 13:37 Utility 6,002.91; 13:57 Utility 4,002.91 (100 reserved) and Working 2,000 (moved
from Utility on the portal); 14:11 B2C 100; 14:38-14:39 two B2B of 50 (charge 3 each); 16:34
Utility 3,802.91 and Working 1,894.00. The ERP sent one Utility payout of 100, but 200 left
Utility: the other 100 was a payment made on the M-Pesa portal.
"""

import datetime
import importlib

import pytest

import fake_frappe
import rich_db

SETTINGS = "985401"
SNAPSHOT = "Mpesa Balance Snapshot"
B2C, B2B = "B2C Disbursement Request", "Mpesa B2B Payment Request"
REGISTER = "Mpesa C2B Payment Register"
DAY = datetime.datetime(2026, 10, 27)


def at(h, m, s=0):
    return DAY.replace(hour=h, minute=m, second=s)


class Doc(fake_frappe.FakeDoc):
    def update(self, values):
        self._row.update(values)

    @property
    def flags(self):
        return self._row.setdefault("_flags", fake_frappe._dict())


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    try:
        rich_db.install(frappe)
        frappe.get_meta = lambda doctype: type("Meta", (), {"has_field": staticmethod(lambda field: True)})()
        frappe.new_doc = lambda doctype: Doc(frappe, doctype, {})
        plain = frappe.get_doc
        frappe.get_doc = lambda dt, name=None, **kw: (
            Doc(frappe, dt, frappe.db.table(dt)[name]) if dt == SNAPSHOT else plain(dt, name, **kw)
        )
        frappe.utils.now_datetime = lambda: DAY.replace(hour=17)
        frappe.db.table("Mpesa Settings")[SETTINGS] = {"name": SETTINGS, "business_shortcode": SETTINGS,
                                                       "paybill_type": "Pay Bill", "till_number": "",
                                                       "creation": DAY}
        balances = importlib.import_module("mpsa_suite.wallet.balances")
        status = importlib.import_module("mpsa_suite.money_out.b2b_status")
        alerts = []
        monkeypatch.setattr(balances, "_alert", lambda message, s: alerts.append((message, s.name)))
        monkeypatch.setattr(status, "still_asking", lambda name: False)
        yield fake_frappe._dict(frappe=frappe, balances=balances, status=status, alerts=alerts)
    finally:
        fake_frappe.uninstall(saved)


def result(env, text, when):
    return env.balances.from_account_balance(SETTINGS, text, when.strftime("%Y%m%d%H%M%S"), "IR-1")


def snapshot(env, name):
    return env.frappe.db.table(SNAPSHOT)[name]


def erp_payouts(env, till_charge="Business Buy Goods Charge|KES|3.00"):
    env.frappe.db.table(B2C)["B2C-26-10-000001"] = {
        "name": "B2C-26-10-000001", "docstatus": 1, "status": "Paid", "amount": 100.0, "mpesa_settings": SETTINGS,
        "transaction_completed_datetime": at(14, 11, 2), "mpesa_charge": 0, "creation": at(14, 10)}
    env.frappe.db.table(B2B)["MB2B-26-10-000001"] = {
        "name": "MB2B-26-10-000001", "docstatus": 1, "status": "Paid", "amount": 50.0, "mpesa_settings": SETTINGS,
        "trans_completed_time": at(14, 38, 56), "debit_party_charges": "Business Pay Bill Charge|KES|3.00",
        "mpesa_charge": 3.0, "creation": at(14, 38, 50)}
    env.frappe.db.table(B2B)["MB2B-26-10-000002"] = {
        "name": "MB2B-26-10-000002", "docstatus": 1, "status": "Paid", "amount": 50.0, "mpesa_settings": SETTINGS,
        "trans_completed_time": at(14, 39), "debit_party_charges": till_charge, "mpesa_charge": 0,
        "creation": at(14, 38, 55)}


def test_account_balance_parts_keep_available_and_reserved(env):
    parts = env.balances.parse_parts("Working Account|KES|2000.00|2000.00|0.00|0.00"
                                     "&Utility Account|KES|4002.91|3902.91|100.00|0.00")
    assert parts == {"Working": (2000.0, 2000.0, 0.0), "Utility": (4002.91, 3902.91, 100.0)}
    assert env.balances.parse_parts("Working Account|KES|1894.00|1894.00") == {"Working": (1894.0, 1894.0, 0.0)}
    assert env.balances.parse_parts("Working Account|KES|5.00") == {"Working": (5.0, 5.0, 0.0)}
    assert env.balances.parse_parts("garbage|x&{Amount=1}") == {}
    # the current balance is still what parse_balances gives
    assert env.balances.parse_balances("Utility Account|KES|4002.91|3902.91|100.00|0.00") == {"Utility": 4002.91}


def test_reserved_money_with_nothing_in_flight_is_a_warning_and_an_alert(env):
    (working, utility) = result(env, "Working Account|KES|2000.00|2000.00|0.00|0.00"
                                     "&Utility Account|KES|4002.91|3902.91|100.00|0.00", at(13, 57))
    row = snapshot(env, utility)
    assert (row["balance"], row["available"], row["reserved"]) == (4002.91, 3902.91, 100.0)
    assert "100.00 of Utility is reserved" in row["warning"] and "outside the ERP" in row["warning"]
    assert not snapshot(env, working).get("warning")
    assert [name for _message, name in env.alerts] == [utility]


def test_reserved_money_while_an_erp_payout_is_in_flight_is_no_warning(env):
    env.frappe.db.table(B2C)["B2C-9"] = {"name": "B2C-9", "docstatus": 1, "status": "Initiated",
                                         "mpesa_settings": SETTINGS, "creation": at(13, 56)}
    (_working, utility) = result(env, "Working Account|KES|2000.00|2000.00|0.00|0.00"
                                      "&Utility Account|KES|4002.91|3902.91|100.00|0.00", at(13, 57))
    assert not snapshot(env, utility).get("warning") and env.alerts == []


def test_a_transfer_between_working_and_utility_is_explained(env):
    result(env, "Working Account|KES|0.00|0.00|0.00|0.00&Utility Account|KES|6002.91|6002.91|0.00|0.00", at(13, 37))
    (working, utility) = result(env, "Working Account|KES|2000.00|2000.00|0.00|0.00"
                                     "&Utility Account|KES|4002.91|4002.91|0.00|0.00", at(13, 57))
    assert env.balances.check(utility) == "Explained"
    assert env.balances.check(working) == "Explained"
    row = snapshot(env, utility)
    assert row["difference"] == -2000.0 and row["erp_movement"] == 0.0 and "transfer between them" in row["note"]
    assert row["previous_snapshot"] and env.alerts == []


def test_money_that_left_utility_outside_the_erp_is_a_gap_and_an_alert(env):
    result(env, "Working Account|KES|2000.00|2000.00|0.00|0.00&Utility Account|KES|4002.91|4002.91|0.00|0.00",
           at(13, 57))
    erp_payouts(env)
    (working, utility) = result(env, "Working Account|KES|1894.00|1894.00|0.00|0.00"
                                     "&Utility Account|KES|3802.91|3802.91|0.00|0.00", at(16, 34))
    assert env.balances.check(utility) == "Gap"
    row = snapshot(env, utility)
    assert (row["erp_movement"], row["expected_balance"], row["difference"]) == (-100.0, 3902.91, -100.0)
    assert "moved -200.00" in row["note"] and "recorded -100.00" in row["note"]
    assert "-100.00 was not sent or received by the ERP" in row["note"]
    assert [name for _message, name in env.alerts] == [utility]
    # Working fell by both B2B and their charges: 2,000 - 53 - 53
    assert env.balances.check(working) == "Agrees"
    assert snapshot(env, working)["erp_movement"] == -106.0


def test_a_b2b_charge_still_unknown_waits_for_its_status_query(env, monkeypatch):
    result(env, "Working Account|KES|2000.00|2000.00|0.00|0.00", at(13, 57))
    erp_payouts(env, till_charge="")
    (working,) = result(env, "Working Account|KES|1894.00|1894.00|0.00|0.00", at(16, 34))
    monkeypatch.setattr(env.status, "still_asking", lambda name: name == "MB2B-26-10-000002")
    assert env.balances.check(working) == "Not Checked"
    assert snapshot(env, working)["check_status"] == "Not Checked"
    # no query left: counted as 0 and said so, so the 3 shows as a gap
    monkeypatch.setattr(env.status, "still_asking", lambda name: False)
    assert env.balances.check(working) == "Gap"
    assert "No charge is known yet for MB2B-26-10-000002" in snapshot(env, working)["note"]


def test_a_receipt_on_the_paybill_counts_on_utility(env):
    result(env, "Utility Account|KES|1000.00|1000.00|0.00|0.00", at(9, 0))
    env.frappe.db.table(REGISTER)["MPC2B-1"] = {"name": "MPC2B-1", "businessshortcode": SETTINGS, "docstatus": 1,
                                                "transid": "UJR1C2B000", "transamount": 500.0,
                                                "transtime": "20261027093000"}
    (utility,) = result(env, "Utility Account|KES|1500.00|1500.00|0.00|0.00", at(10, 0))
    assert env.balances.check(utility) == "Agrees"
    assert snapshot(env, utility)["erp_movement"] == 500.0


def test_no_balance_before_it_cannot_be_checked(env):
    (utility,) = result(env, "Utility Account|KES|1500.00|1500.00|0.00|0.00", at(10, 0))
    assert env.balances.check(utility) == "Cannot Check"
    assert "no earlier Utility balance" in snapshot(env, utility)["note"]
    # one more than a week old does not count either
    result(env, "Merchant Account|KES|10.00|10.00|0.00|0.00", at(10, 0) - datetime.timedelta(days=8))
    (merchant,) = result(env, "Merchant Account|KES|10.00|10.00|0.00|0.00", at(10, 0))
    assert env.balances.check(merchant) == "Cannot Check"


def test_a_statement_anchor_is_still_checked_from_the_statement(env, monkeypatch):
    env.frappe.db.table(SNAPSHOT)["ANCHOR"] = {"name": "ANCHOR", "shortcode": SETTINGS, "sub_account": "Utility",
                                               "source": "Statement", "balance": 1000.0, "taken_at": at(8, 0)}
    (utility,) = result(env, "Utility Account|KES|1500.00|1500.00|0.00|0.00", at(10, 0))
    monkeypatch.setattr(env.balances, "check_movement", lambda s: pytest.fail("compared with the balance before"))
    monkeypatch.setattr(env.balances.accounts, "bank_account", lambda settings, sub: "M-Pesa 985401")
    monkeypatch.setattr(env.balances.accounts, "separate", lambda settings, sub: False)
    env.frappe.db.sql = lambda query, values=None, **kw: [[0]] if "COUNT" in query else [[500.0]]
    assert env.balances.check(utility) == "Agrees"
