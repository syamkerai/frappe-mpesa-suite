"""Wallet helpers on an in-memory frappe: which sub-account a result's balance names, the
balances of an Account Balance result (as Astral's 4139683 returned them), and the statement
row key that keeps a re-imported row from being booked twice."""

import datetime
import importlib

import pytest

import fake_frappe

# Astral 4139683, 2 Oct 2026 (the layout Safaricom returns)
ACCOUNT_BALANCE = ("Working Account|KES|0.00|0.00|0.00|0.00&Utility Account|KES|501590.01|501590.01|0.00|0.00"
                   "&Charges Paid Account|KES|0.00|0.00|0.00|0.00&Merchant Account|KES|0.00|0.00|0.00|0.00")


@pytest.fixture
def wallet():
    frappe, saved = fake_frappe.install()
    try:
        yield fake_frappe._dict(
            frappe=frappe,
            accounts=importlib.import_module("frappe_mpsa_payments.wallet.accounts"),
            balances=importlib.import_module("frappe_mpsa_payments.wallet.balances"),
            importer=importlib.import_module("frappe_mpsa_payments.wallet.statement_import"),
        )
    finally:
        fake_frappe.uninstall(saved)


@pytest.mark.parametrize("text, sub", [
    ("Working Account|KES|1476.00|1476.00|0.00|0.00", "Working"),
    ("MMF Account|KES|10.00", "Working"),
    ("Utility Account|KES|5.00", "Utility"),
    ("Merchant Account|KES|0.00", "Merchant"),
    ("Charges Paid Account|KES|-1540.00", "Charges Paid"),
    ("Organization Settlement Account|KES|0.00", "Organization Settlement"),
    ("{Amount={CurrencyCode=KES, MinimumAmount=147600, BasicAmount=1476.00}}", None),  # names no account
    ("", None),
    (None, None),
])
def test_a_results_balance_names_its_sub_account(wallet, text, sub):
    assert wallet.accounts.from_balance_text(text) == sub


def test_account_balance_result_gives_each_sub_accounts_current_balance(wallet):
    assert wallet.balances.parse_balances(ACCOUNT_BALANCE) == {
        "Working": 0.0, "Utility": 501590.01, "Charges Paid": 0.0, "Merchant": 0.0}
    assert wallet.balances.parse_balances("garbage|x") == {}
    assert wallet.balances.parse_time("20261002104324") == datetime.datetime(2026, 10, 2, 10, 43, 24)
    assert wallet.balances.parse_time(None) == wallet.frappe.utils.now_datetime()


def test_receiving_sub_account_is_merchant_for_a_till(wallet):
    wallet.frappe.db.table("Mpesa Settings")["TILL"] = {"name": "TILL", "paybill_type": "Buy Goods"}
    wallet.frappe.db.table("Mpesa Settings")["PAYBILL"] = {"name": "PAYBILL", "paybill_type": "Pay Bill"}
    assert wallet.accounts.receiving("TILL") == "Merchant"
    assert wallet.accounts.receiving("PAYBILL") == "Utility"
    assert wallet.accounts.receiving(None) == "Utility"


def test_statement_row_key_is_stable_and_tells_rows_apart(wallet):
    from frappe_mpsa_payments.wallet.statement_file import Row

    def row(details, withdrawn):
        return Row("UJMTM6331C", datetime.datetime(2026, 10, 22, 17, 34, 53), None, details, "Completed", 0.0,
                   withdrawn, 1476.0)

    payout, charge = row("Business Pay Bill via API to 522522 - KC", 10.0), row("Business Pay Bill Charge", 2.0)
    key = wallet.importer.row_key
    assert key("4139683", "Working", payout) == key("4139683", "Working", row(payout.details, 10.0))
    assert key("4139683", "Working", payout) != key("4139683", "Working", charge)  # one receipt, two rows
    assert key("4139683", "Working", payout) != key("4139683", "Utility", payout)  # each side of a transfer
    assert len(key("4139683", "Working", payout)) == 40
