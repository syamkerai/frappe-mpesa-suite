"""M-PESA Org Portal statements (wallet.statement_file): reading each account's export and
sorting its rows into payouts, charges, transfers, funding and receipts.

The layout is the portal's per-account export as Astral downloads it (header block, then a
"Receipt No." table, newest first, withdrawals printed negative). Names, phones and amounts
here are made up.
"""

import csv
import datetime
import io
import sys
import types

import openpyxl
import pytest

from frappe_mpsa_payments.wallet import statement_file as sf

HEADER = ["Receipt No.", "Completion Time", "Initiation Time", "Details", "Transaction Status", "Paid In",
          "Withdrawn", "Balance", "Balance Confirmed", "Reason Type", "Other Party Info", "Linked Transaction ID",
          "A/C No.", "Currency"]
OWN = "600999 - TEST TRADERS LIMITED"


def row(receipt, when, details, paid_in="", withdrawn="", balance="", reason="", party="", status="Completed",
        account_no=""):
    return [receipt, when, when, details, status, paid_in, withdrawn, balance, "true", reason, party, "", account_no,
            "KES"]


UTILITY_ROWS = [
    row("UJMTM62YMV", "22-10-2026 17:28:21", "Funds Transfer from Utility Account to Working Account",
        withdrawn=-1500.0, balance=389495.0, reason="Business Transfer From Utility Account To MMF Account via AP",
        party=OWN),
    row("UJMROBBTNX", "22-10-2026 16:15:18", "B2C Payment Charge", withdrawn=-11.0, balance=390995.0,
        reason="Salary Payment via API", party="SP"),
    row("UJMROBBTNX", "22-10-2026 16:15:18", "Salary Payment to 25470****111 - JANE **** DOE", withdrawn=-6000.0,
        balance=391006.0, reason="Salary Payment via API", party="25470****111 - JANE **** DOE"),
    row("UJMROBBTNY", "22-10-2026 16:10:00", "Salary Payment to 25470****222 - JOHN **** ROE", withdrawn=-100.0,
        balance=397006.0, reason="Salary Payment via API", party="25470****222 - JOHN **** ROE"),
    row("UJMROBBTNZ", "22-10-2026 16:05:00", "Business Payment to 25470****333 - ANN **** POE",
        withdrawn=-500.0, balance=397106.0, reason="Business Payment via API", status="Failed"),
    row("UJMTM62FSN", "22-10-2026 11:09:18", "Business Transfer from MMF Account to Utility Account via WEB by "
        "TEST TRADERS LIMITED\\clerk", paid_in=397106.0, balance=397106.0,
        reason="Business Transfer From MMF Account To Utility Account via WE", party=OWN),
]
MMF_ROWS = [
    row("UJMTM6331C", "22-10-2026 17:34:53", "Business Pay Bill Charge", withdrawn=-2.0, balance="1,476.00",
        reason="Business Pay Bill via API", party="SP"),
    row("UJMTM6331C", "22-10-2026 17:34:53", "Business Pay Bill via API to 888880 - TEST BILLER",
        withdrawn="-10.00", balance="1,478.00", reason="Business Pay Bill via API", party="888880 - TEST BILLER",
        account_no="INV-1"),
    row("UJMTM62YMV", "22-10-2026 17:28:21", "Funds Transfer from Utility Account to Working Account",
        paid_in=1500.0, balance=1488.0, reason="Business Transfer From Utility Account To MMF Account via AP",
        party=OWN),
    row("UJMTM62FSN", "22-10-2026 11:09:18", "Business Transfer from MMF Account to Utility Account via WEB by "
        "TEST TRADERS LIMITED\\clerk", withdrawn=-397106.0, balance=-12.0,
        reason="Business Transfer From MMF Account To Utility Account via WE", party=OWN),
    row("UJL004GDBH", "21-10-2026 15:57:31", "Deposit into MMF Control Account from M-PESA",
        paid_in="397,094.00", balance=397094.0, reason="Organization Deposit of Funds", party="SP"),
]


def grid(account, rows, shortcode="600999"):
    return [
        ["Account Holder:", "TEST TRADERS LIMITED"],
        ["Short Code:", shortcode],
        ["Account:", account],
        ["Time Period:", "From", "01-10-2026 00:00:00", "To", "22-10-2026 23:59:59"],
        ["Operator:", "clerk", "Organization:", "TEST TRADERS LIMITED", "Date of Report:", "22-10-2026 18:26:21"],
        ["Opening Balance:", "0.0", "Closing Balance:", "389495.0", "Available Balance:", "", "Total Paid In:",
         "397106.0", "Total Withdrawn:", "-7611.0"],
        HEADER,
        *rows,
    ]


def xlsx(rows) -> bytes:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "transaction"
    for r in rows:
        sheet.append(r)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def as_csv(rows) -> bytes:
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------- reading
def test_utility_export_reads_its_header_and_rows():
    s = sf.read(xlsx(grid("Utility Account", UTILITY_ROWS)), "ORG_600999_UtilityAccount_All_20261022182633.xlsx")
    assert (s.shortcode, s.holder, s.account_name, s.sub_account) == (
        "600999", "TEST TRADERS LIMITED", "Utility Account", "Utility")
    assert (s.period_from, s.period_to) == (datetime.datetime(2026, 10, 1), datetime.datetime(2026, 10, 22, 23, 59, 59))
    assert (s.opening_balance, s.closing_balance, s.total_paid_in, s.total_withdrawn) == (0, 389495, 397106, 7611)
    assert len(s.rows) == 6
    first = s.rows[0]
    assert (first.receipt, first.completed_at, first.paid_in, first.withdrawn, first.amount, first.balance) == (
        "UJMTM62YMV", datetime.datetime(2026, 10, 22, 17, 28, 21), 0, 1500, -1500, 389495)
    assert first.other_party == OWN and first.completed
    assert not s.rows[4].completed


def test_mmf_export_is_the_working_account_and_reads_comma_numbers():
    s = sf.read(as_csv(grid("MMF Account for Organization", MMF_ROWS)), "ORG_600999_MMFAccount.csv")
    assert s.sub_account == "Working"
    payment = s.rows[1]
    assert (payment.withdrawn, payment.amount, payment.balance, payment.account_no) == (10, -10, 1478, "INV-1")
    assert s.rows[4].paid_in == 397094


def test_running_balances_follow_the_amounts():
    s = sf.read(xlsx(grid("MMF Account for Organization", MMF_ROWS)), "x.xlsx")
    for newer, older in zip(s.rows, s.rows[1:]):
        assert round(older.balance + newer.amount, 2) == newer.balance


def test_legacy_xls_export_is_read(monkeypatch):
    # the portal sends BIFF .xls; xlwt is not installed to write one, so xlrd is stood in for
    cells = grid("Utility Account", UTILITY_ROWS[:2])
    width = max(len(r) for r in cells)
    sheet = types.SimpleNamespace(
        nrows=len(cells), ncols=width,
        cell=lambda r, c: types.SimpleNamespace(ctype=1, value=cells[r][c] if c < len(cells[r]) else ""),
    )
    fake = types.ModuleType("xlrd")
    fake.XL_CELL_DATE = 3
    fake.open_workbook = lambda file_contents: types.SimpleNamespace(sheet_by_index=lambda i: sheet, datemode=0)
    monkeypatch.setitem(sys.modules, "xlrd", fake)
    s = sf.read(sf.XLS_MAGIC + b"\x00" * 20, "ORG_600999_UtilityAccount_All.xls")
    assert (s.sub_account, len(s.rows), s.rows[1].withdrawn) == ("Utility", 2, 11)


def test_combined_export_is_refused():
    combined = [["Receipt No", "Completion Time", "Details", "Transaction Status", "Paid In (KSHs)",
                 "Withdrawn (KSHs)", "Balance (KSHs)"],
                ["UJMTM62YMV", "22/10/2026 17:28:21", "Funds Transfer from Utility Account to W", "Completed",
                 "1,500.00", "0.00", "1,500.00"]]
    with pytest.raises(sf.StatementError, match="Export each account separately"):
        sf.read(xlsx(combined), "Statements for 600999 from 2026-10-01 to 2026-10-22.xlsx")


@pytest.mark.parametrize("content, message", [
    (xlsx(grid("Loyalty Points Account", UTILITY_ROWS)), "Unknown M-PESA account"),
    (xlsx([["Account:", "Utility Account"], ["nothing", "here"]]), "Receipt No."),
    (b"", "empty"),
])
def test_unreadable_files_are_refused(content, message):
    with pytest.raises(sf.StatementError, match=message):
        sf.read(content, "statement.xlsx")


@pytest.mark.parametrize("name, key", [
    ("Utility Account", "Utility"), ("MMF Account for Organization", "Working"), ("MMF Account", "Working"),
    ("Working Account", "Working"), ("mmf", "Working"), ("Merchant Account", "Merchant"),
    ("Charges Paid Account", "Charges Paid"), ("Organization Settlement Account", "Organization Settlement"),
    ("Organisation  Settlement", "Organization Settlement"), ("Float Account", "Float"),
    ("MMF Control Account", "Working"), ("Loyalty Account", None), ("", None), (None, None),
])
def test_account_names(name, key):
    assert sf.sub_account_key(name) == key


# ---------------------------------------------------------------- kinds
def kinds(account, rows):
    s = sf.read(xlsx(grid(account, rows)), "x.xlsx")
    return [sf.classify(r, s.shortcode, s.sub_account) for r in s.rows]


def test_utility_rows_are_transfers_payouts_and_their_charges():
    out_to_working, charge, salary, small_salary, failed, from_mmf = kinds("Utility Account", UTILITY_ROWS)
    assert out_to_working == sf.Kind("transfer", from_sub="Utility", to_sub="Working")
    assert charge == sf.Kind("charge", api="B2C")
    assert salary == sf.Kind("payout", api="B2C") and small_salary == sf.Kind("payout", api="B2C")
    assert failed == sf.Kind("skipped")
    assert from_mmf == sf.Kind("transfer", from_sub="Working", to_sub="Utility")


def test_working_rows_are_b2b_payouts_charges_transfers_and_funding():
    charge, payment, from_utility, to_utility, deposit = kinds("MMF Account for Organization", MMF_ROWS)
    assert charge == sf.Kind("charge", api="B2B")
    assert payment == sf.Kind("payout", api="B2B")
    assert from_utility == sf.Kind("transfer", from_sub="Utility", to_sub="Working")
    assert to_utility == sf.Kind("transfer", from_sub="Working", to_sub="Utility")
    assert deposit == sf.Kind("funding")


def one(details, reason="", paid_in=0.0, withdrawn=0.0, party="", status="Completed", sub="Utility"):
    r = sf.Row(receipt="UJX0000001", completed_at=None, initiated_at=None, details=details, status=status,
               paid_in=paid_in, withdrawn=withdrawn, balance=None, reason_type=reason, other_party=party)
    return sf.classify(r, "600999", sub)


@pytest.mark.parametrize("kind, expected", [
    (one("Utility Account to Organization Settlement Account", withdrawn=880895),
     sf.Kind("transfer", from_sub="Utility", to_sub="Organization Settlement")),
    (one("Utility Account to Organization Settlement Account", paid_in=880895, sub="Organization Settlement"),
     sf.Kind("transfer", from_sub="Utility", to_sub="Organization Settlement")),
    # the own shortcode as the other party, with no account names: one side known
    (one("Funds movement", withdrawn=50, party=OWN), sf.Kind("transfer", from_sub="Utility")),
    (one("Funds movement", paid_in=50, party=OWN, sub="Working"), sf.Kind("transfer", to_sub="Working")),
    (one("Business Buy Goods Charge", reason="Business Buy Goods via API", withdrawn=1, sub="Working"),
     sf.Kind("charge", api="B2B")),
    (one("Promotion Payment to 2547****444 - X", reason="Promotion Payment via API", withdrawn=50),
     sf.Kind("payout", api="B2C")),
    (one("Business Buy Goods via API to 123456 - SHOP", reason="Business Buy Goods via API", withdrawn=50,
         sub="Working"), sf.Kind("payout", api="B2B")),
    (one("Pay Bill from 2547****555 - CUSTOMER", reason="Pay Bill Online", paid_in=1000), sf.Kind("receipt")),
    (one("I AND M BANK LTD - BANK TO TILL", reason="Organization To Organization Transfer", paid_in=79240),
     sf.Kind("receipt")),
    (one("Deposit into MMF Control Account from M-PESA", paid_in=5000, sub="Working"), sf.Kind("funding")),
    (one("Funds received", reason="Organization Deposit of Funds", paid_in=5000, sub="Working"), sf.Kind("funding")),
    (one("Reversal of Pay Bill from 2547****555", reason="Reversal", withdrawn=1000), sf.Kind("reversal")),
    (one("Business Withdrawal to Bank 1007044002400", reason="Withdraw Funds to Bank", withdrawn=880000,
         sub="Working"), sf.Kind("withdrawal")),
    (one("Adjustment", reason="Adjustment"), sf.Kind("other")),
    (one("Salary Payment to 2547****111 - JANE", reason="Salary Payment via API", withdrawn=10, status="Declined"),
     sf.Kind("skipped")),
])
def test_each_kind_of_row(kind, expected):
    assert kind == expected


def test_a_payment_to_another_of_our_shortcodes_is_a_payout_not_a_transfer():
    kind = one("Business Pay Bill via API to 208911 - TEST TRADERS LIMITED", reason="Business Pay Bill via API",
               withdrawn=10, party="208911 - TEST TRADERS LIMITED", sub="Working")
    assert kind == sf.Kind("payout", api="B2B")
