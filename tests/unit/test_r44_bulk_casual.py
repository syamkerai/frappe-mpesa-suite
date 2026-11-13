"""Round 4.4: bulk casual payouts made easy. Paste Payees reads the sheets staff really have (title rows
above the column names, a payee table's own Download, a first column of row numbers, a TOTAL row, a
phone typed as a number) and says which rows it left out; the list's Pay Casual Payees by M-Pesa names
the ticked claims it had nothing to pay for, and why (above all: no lines in the M-Pesa Payees table,
as when the casuals were typed into another list on the claim)."""

import csv
import io
import json
from pathlib import Path

import b2b_fixture
import pytest
from test_claim_payees import LINE, claim, payee, phone_line, raises
from test_claim_payees import env as claims_env  # noqa: F401 (the fixture)

ROOT = Path(__file__).resolve().parents[2] / "mpsa_suite"


@pytest.fixture(autouse=True)
def _line_columns(request, monkeypatch):
    """The payee table has round 4.3's name columns even when no claim has a line yet."""
    if "claims_env" in request.fixturenames:
        request.getfixturevalue("claims_env")
        monkeypatch.setitem(b2b_fixture.COLUMNS, LINE, tuple(dict.fromkeys(
            b2b_fixture.COLUMNS[LINE] + ("mpesa_name", "mpesa_name_from", "typed_name"))))


def as_csv(rows) -> str:
    out = io.StringIO()
    csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
    return out.getvalue()


def payees(rows):
    return [(r.row, r.payee_name, r.phone, r.amount_text) for r in rows]


# ---------------------------------------------------------------- reading the pasted or uploaded sheet
def test_a_payee_tables_own_download_is_read_under_its_title_and_notes(claims_env):
    # what a child table's Download (Frappe grid) writes: a title, labels, field names, descriptions, notes, rows
    cp = claims_env.cp
    labels = ["Payee Name", "Phone", "M-Pesa No.", "Amount", "Status", "M-Pesa Receipt", "Payout Request", "Paid On"]
    table = [["Bulk Edit Payees"], labels,
             ["payee_name", "phone", "msisdn", "amount", "payment_status", "transaction_id", "b2c_request", "paid_on"],
             [" "] * len(labels), ["The CSV format is case sensitive"],
             ["Do not edit headers which are preset in the template"], ["------"],
             ["Jane Wanjiku", "0711 000 333", "254711000333", "1500", "Pending", "", "", ""],
             ["Peter Otieno", "0711000444", "254711000444", "2000", "Pending", "", "", ""]]
    rows, left_out = cp.read_rows(cp.parse_text(as_csv(table)))
    # row numbers are the file's lines (the blank description row 4 is counted, and is no row at all)
    assert payees(rows) == [(8, "Jane Wanjiku", "0711 000 333", "1500"), (9, "Peter Otieno", "0711000444", "2000")]
    assert left_out == [1, 2, 3, 5, 6, 7]


def test_title_rows_above_the_column_names_and_a_total_row_are_left_out(claims_env):
    cp = claims_env.cp
    text = "\n".join(["CASUAL LABOUR - SITE A - 11 NOV 2026", "", "No.\tName\tPhone\tAmount",
                      "1\tJane Wanjiku\t0711000333\t1,500", "2\tPeter Otieno\t0711000444\t2,000", "\tTOTAL\t\t3,500"])
    rows, left_out = cp.read_rows(cp.parse_text(text))
    assert payees(rows) == [(3, "Jane Wanjiku", "0711000333", "1,500"), (4, "Peter Otieno", "0711000444", "2,000")]
    assert left_out == [1, 2, 5]


def test_the_amount_column_is_the_one_named_amount_not_pay_to(claims_env):
    cp = claims_env.cp
    rows = cp.table_rows([["Pay To", "Payee Name", "Phone", "Amount"], ["Phone (B2C)", "Jane", "0711000333", "1500"]])
    assert payees(rows) == [(2, "Jane", "0711000333", "1500")]
    rows = cp.table_rows([["Payment Status", "Amount", "Name", "Mobile"], ["Pending", "700", "Ann", "0711000555"]])
    assert payees(rows) == [(2, "Ann", "0711000555", "700")]


def test_without_column_names_row_numbers_and_columns_after_the_amount_are_passed_over(claims_env):
    cp = claims_env.cp
    rows, left_out = cp.read_rows(cp.parse_text("\n".join([
        "1, Jane Wanjiku, 0711000333, 1500",
        "2\tPeter Otieno\t0711000444\t2000\tSite B",
        "0711000555, Ann Njeri, 700",
        "John Otieno, 711000666, 1,200",
        "Mary, 07110, 900",  # a mistyped phone stays, to be shown wrong
        "Only A Name",
    ])))
    assert payees(rows) == [(1, "Jane Wanjiku", "0711000333", "1500"), (2, "Peter Otieno", "0711000444", "2000"),
                            (3, "Ann Njeri", "0711000555", "700"), (4, "John Otieno", "711000666", "1200"),
                            (5, "Mary", "07110", "900")]
    assert left_out == [6]


def test_a_spreadsheets_number_cells_are_read_whole(claims_env):
    cp = claims_env.cp
    rows = cp.table_rows([["Jane", 711000333.0, 1500.0], ["Cents", 711000444.0, 10.5]])
    assert payees(rows) == [(1, "Jane", "711000333", "1500"), (2, "Cents", "711000444", "10.5")]


def test_column_names_under_a_payee_row_are_not_taken_as_the_header(claims_env):
    cp = claims_env.cp
    rows, left_out = cp.read_rows([["Jane", "0711000333", "1500"], ["Name", "Phone", "Amount"]])
    assert (payees(rows), left_out) == ([(1, "Jane", "0711000333", "1500")], [2])


def test_the_paste_dialog_is_told_the_good_total_and_the_rows_left_out(claims_env):
    claim(claims_env, [])
    text = "CASUALS 11 NOV\nName, Phone, Amount\nJane, 0711000333, 1500\nPeter, 0711000444, 2000\nBad, 1, 1"
    answer = claims_env.cp.parse_payees(text=text, claim="HR-EXP-1")
    assert (answer["good"], answer["bad"], answer["good_total"], answer["left_out"]) == (2, 1, 3500, [1, 2])


# ---------------------------------------------------------------- the list's Pay Casual Payees by M-Pesa
def test_a_claim_with_no_mpesa_payees_lines_is_named_and_told_where_payees_go(claims_env):
    claim(claims_env, [], name="HR-EXP-1")
    with raises(claims_env, "^Nothing to pay: HR-EXP-1 has no lines in the M-Pesa Payees table, so there is nobody "
                            "to pay: M-Pesa pays only that table's payees, never a list typed anywhere else on the "
                            "claim. A submitted claim takes no new payees: cancel it, amend it, add them with M-Pesa "
                            "> Paste Payees"):
        claims_env.cp.pay_claims(["HR-EXP-1"], "Astral B2C")
    claim(claims_env, [], name="HR-EXP-2")
    with raises(claims_env, "^Nothing to pay: HR-EXP-1 and HR-EXP-2 have no lines in the M-Pesa Payees table"):
        claims_env.cp.pay_claims(["HR-EXP-2", "HR-EXP-1"], "Astral B2C")


def test_the_batch_says_which_ticked_claims_it_left_out_and_why(claims_env):
    claim(claims_env, [phone_line("LN-1")], name="HR-EXP-1")
    claim(claims_env, [], name="HR-EXP-2")
    claim(claims_env, [payee("LN-5", 1)], name="HR-EXP-3")
    made = claims_env.cp.pay_claims(json.dumps(["HR-EXP-1", "HR-EXP-2", "HR-EXP-3"]), "Astral B2C")
    assert made.lines == 1 and made.b2c
    assert made.skipped == [
        "HR-EXP-2 has no lines in the M-Pesa Payees table, so there is nobody to pay: M-Pesa pays only that table's "
        "payees, never a list typed anywhere else on the claim. A submitted claim takes no new payees: cancel it, "
        "amend it, add them with M-Pesa > Paste Payees (many at once, from Excel) and approve it again.",
        "HR-EXP-3 has no phone payees: its merchants (till, paybill, bank account) are paid from the claim, with "
        "M-Pesa > Pay Merchants and Payees."]


def test_claims_paid_already_are_named_when_several_are_ticked(claims_env):
    claim(claims_env, [phone_line("LN-1")], name="HR-EXP-1")
    claim(claims_env, [phone_line("LN-3", phone="0711000555")], name="HR-EXP-2", employee="EMP-2")
    assert claims_env.cp.pay_claims(["HR-EXP-1", "HR-EXP-2"], "Astral B2C").skipped == []
    with raises(claims_env, "^Nothing to pay: every phone payee line of HR-EXP-1 and HR-EXP-2 is paid, on its way, "
                            "or has a payout waiting to be sent.$"):
        claims_env.cp.pay_claims(["HR-EXP-1", "HR-EXP-2"], "Astral B2C")
    # one claim (its own form): said as before
    with raises(claims_env, "^Nothing to pay: every payee line is paid, on its way, or has a payout waiting to be "
                            "sent.$"):
        claims_env.cp.pay_payees("HR-EXP-1")


@pytest.mark.parametrize("skipped, ticked, said", [
    ({"no_lines": ["A"]}, 1, "A has no lines in the M-Pesa Payees table"),
    ({"no_phones": ["A", "B", "C"]}, 3, "A, B and C have no phone payees: their merchants"),
    ({"done": ["A"]}, 1, "every payee line is paid"),
    ({"done": ["A"]}, 2, "every payee line of A is paid"),
])
def test_nothing_to_pay_is_said_per_group(claims_env, skipped, ticked, said):
    assert claims_env.cp.nothing_to_pay(skipped, ticked).startswith(said)


# ---------------------------------------------------------------- the form and the list (desk scripts)
def test_paste_payees_is_in_plain_sight_and_saves_the_claim_first():
    js = (ROOT / "public" / "js" / "expense_claim_payees.js").read_text(encoding="utf-8")
    assert 'class="btn btn-xs btn-primary mpsa-paste"' in js and '.find(".mpsa-paste")' in js
    assert "if (!after_save) frm.save().then(() => this.paste(frm, true));" in js
    assert 'fieldname: "set_line_amount"' in js and "line_total(frm, line, checked)" in js
    assert __import__("re").search(r"d\.hide\(\);[\s\S]{0,400}frm\.save\(\);", js)


def test_the_list_names_the_claims_left_out_of_the_batch():
    js = (ROOT / "public" / "js" / "expense_claim_payees_list.js").read_text(encoding="utf-8")
    assert "(r.skipped || []).length" in js and "frappe.utils.escape_html(s)" in js
