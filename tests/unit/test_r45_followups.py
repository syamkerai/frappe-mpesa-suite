"""Round 4.5: what the three review fixers handed over to each other, done after their merge.

1. A number in front of an M-Pesa name with no dash ("254711000999 JANE DOE", "+254711000999 JANE") is
   the number's, so another number's name is never kept; a short number leads a business's name
   ("24 HOUR SHOP") and stays part of it.
2. On a bulk payout sheet, a number whose only M-Pesa name an SMS gave is new: it is tested first
   (KES 10) when the sheet says so.
3. A bulk sheet row's payout to an Employee's phone (no party on the payout) marks the phone as an
   employee's in the known names, so its name is hidden as any staff payee's is.
"""

import importlib

import pytest
from test_claim_payees import env as claims_env  # noqa: F401 (the fixture)


@pytest.mark.parametrize("received, number, name", [
    ("254711000999 - JANE DOE", "254711000999", "JANE DOE"),
    ("254711000999 JANE DOE", "254711000999", "JANE DOE"),
    ("+254711000999 JANE", "254711000999", "JANE"),
    ("2547****0999 - JANE", "2547****0999", "JANE"),
    ("835811 - 24 HOUR SHOP", "835811", "24 HOUR SHOP"),
    ("24 HOUR SHOP", None, "24 HOUR SHOP"),
    ("3M KENYA", None, "3M KENYA"),
    ("JANE DOE", None, "JANE DOE"),
])
def test_a_number_in_front_of_a_name_is_found_with_or_without_a_dash(claims_env, received, number, name):
    cp = claims_env.cp
    pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
    assert cp.number_prefix(received) == number and pa.number_prefix(received) == number
    assert cp._without_number(received) == name and pa.without_number(received) == name


def test_a_name_naming_another_number_without_a_dash_is_not_kept(claims_env):
    known_names = importlib.import_module("mpsa_suite.money_out.known_names")
    seen = known_names.observation(known_names.PHONE, "254711000333", "254711000999 JANE DOE",
                                   source=known_names.B2C_RESULT)
    assert not seen  # M-Pesa's text names another number: not this phone's name


def test_a_number_only_an_sms_named_is_new_on_a_bulk_sheet(claims_env, monkeypatch):
    bulk = importlib.import_module("mpsa_suite.money_out.bulk_payout")
    known_names = importlib.import_module("mpsa_suite.money_out.known_names")
    records = {"B2C:254711000333": {"mpesa_name": "JANE DOE", "source": known_names.SMS},
               "B2C:254711000444": {"mpesa_name": "PETER OTIENO", "source": known_names.B2C_RESULT}}
    monkeypatch.setattr(known_names, "lookup", lambda keys: {k: records[k] for k in keys if k in records})
    rows = [claims_env.frappe._dict(phone=p) for p in ("254711000333", "254711000444", "254711000555")]
    assert bulk._new_numbers(rows) == {"254711000333", "254711000555"}


def test_a_bulk_rows_payout_to_an_employee_marks_the_phone_an_employees(claims_env, monkeypatch):
    known_names = importlib.import_module("mpsa_suite.money_out.known_names")
    partyless = importlib.import_module("mpsa_suite.money_out.partyless")
    asked = []
    monkeypatch.setattr(partyless, "payee_employee", lambda request: asked.append(request) or "EMP-7")
    request = claims_env.frappe._dict(status="Paid", phone_number="254711000333", party_type=None,
                                      receiver_public_name="254711000333 - JANE DOE", name="B2C-9",
                                      reference_doctype="M-Pesa Bulk Payout", reference_name="MBP-1",
                                      b2c_payment_disbursement_reference="MPESA-B2C-1-R1")
    seen = known_names.b2c_observation(request)
    assert seen and seen.get("employee") and asked
    # a payout with a party: the party says it, the handler is not asked
    asked.clear()
    seen = known_names.b2c_observation(claims_env.frappe._dict(request, party_type="Supplier"))
    assert seen and not seen.get("employee") and not asked


def test_the_bulk_handler_names_the_rows_employee(claims_env, monkeypatch):
    bulk = importlib.import_module("mpsa_suite.money_out.bulk_payout")
    row = claims_env.frappe._dict(name="ROW-1", employee="EMP-7", parent="MBP-1", parenttype=bulk.SHEET)
    monkeypatch.setattr(bulk, "_sheet_row", lambda reference_row, sheet=None: (row, False))
    request = claims_env.frappe._dict(reference_row="ROW-1", reference_name="MBP-1")
    assert bulk.HANDLER.employee(request) == "EMP-7"
    monkeypatch.setattr(bulk, "_sheet_row", lambda reference_row, sheet=None: (None, False))
    assert bulk.HANDLER.employee(request) is None


def test_a_long_mpesa_error_never_overflows_the_rows_progress(claims_env):
    # bench (MariaDB): "Data too long for column 'progress'" after Mark as Not Paid's long note
    bulk = importlib.import_module("mpsa_suite.money_out.bulk_payout")
    current = claims_env.frappe._dict(is_test=False, state=bulk.FAILED, error="x" * 400, amount=1000)
    text = bulk._progress(bulk.FAILED, current, None)
    assert len(text) == bulk.PROGRESS_LENGTH and text.startswith("Payout failed: xxx") and text.endswith("…")
    assert bulk._fit("short") == "short" and bulk._fit(None) is None and bulk._fit("") is None


# ---------------------------------------------------------------- the verification's last findings
@pytest.mark.parametrize("header, row, amount", [
    (["Name", "Phone", "Total", "Deductions", "Amount Payable"], ["Jane", "0711000333", "3000", "500", "2500"], "2500"),
    (["Name", "Phone", "Total", "Advance", "Amount Due"], ["Jane", "0711000333", "3000", "500", "2500"], "2500"),
    (["Name", "Phone", "Gross", "NSSF", "Net Pay"], ["Jane", "0711000333", "3000", "200", "2800"], "2800"),
    (["Name", "Phone", "Days", "Rate", "Total"], ["Jane", "0711000333", "5", "600", "3000"], "3000"),
])
def test_what_is_paid_in_the_end_is_the_amount(claims_env, header, row, amount):
    (got,) = claims_env.cp.table_rows([header, row])
    assert got.amount_text == amount and not got.problem


@pytest.mark.parametrize("header", [
    ["Name", "Phone", "Gross", "Advance", "Balance"],
    ["Name", "Phone", "Grand Total", "Loan"],
])
def test_past_a_deduction_a_total_or_gross_is_never_taken_as_the_amount(claims_env, header):
    read = claims_env.cp.read_rows([header, ["Jane", "0711000333", "3000", "500", "2500"][:len(header)]])
    assert read.columns["amount_column"] is None  # the user picks the column; nobody is paid the gross


def test_a_paid_by_column_is_no_paid_mark_but_a_paid_remark_is(claims_env):
    cp = claims_env.cp
    (row,) = cp.table_rows([["Name", "Phone", "Amount", "Paid By"], ["Jane", "0711000333", "1500", "John Kamau"]])
    assert not row.problem
    for remark in ("Paid.", "paid on 3/11", "PAID via M-Pesa"):
        (row,) = cp.table_rows([["Name", "Phone", "Amount", "Remarks"], ["Jane", "0711000333", "1500", remark]])
        assert row.problem and "already" in row.problem, remark


def test_column_names_are_plain_text_in_the_amount_picker(claims_env):
    read = claims_env.cp.read_rows([["Name", "Phone", "Amount", "<img src=x onerror=alert(1)> Remarks"],
                                    ["Jane", "0711000333", "1500", "ok"]])
    labels = [c["label"] for c in read.columns["amount_choices"]]
    assert len(labels) == 2 and all("<" not in label and ">" not in label for label in labels)


def test_one_typed_word_is_no_full_agreement_with_mpesas_name(claims_env):
    known_names = importlib.import_module("mpsa_suite.money_out.known_names")
    assert not known_names.agrees("Otieno", "OTIENO OUMA")
    assert known_names.agrees("Otieno Ouma", "OTIENO OUMA") and known_names.agrees("Jane Wanjiku", "JANE WANJIKU")
    assert not known_names.agrees("Peter Kamau", "OTIENO OUMA")


def test_a_claim_payees_payout_never_marks_the_phone_an_employees(claims_env):
    known_names = importlib.import_module("mpsa_suite.money_out.known_names")
    base = dict(status="Paid", phone_number="254711000333", receiver_public_name="254711000333 - JANE DOE",
                name="B2C-9", party_type="Employee", reference_doctype="Expense Claim")
    payee = known_names.b2c_observation(claims_env.frappe._dict(base, claim_payee="LN-1"))
    own = known_names.b2c_observation(claims_env.frappe._dict(base, claim_payee=""))
    assert payee and not payee.get("employee")  # a casual paid on the claim: the claimant is only the party
    assert own and own.get("employee")  # the claimant reimbursed to their own phone
