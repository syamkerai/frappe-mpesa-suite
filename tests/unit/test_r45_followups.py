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
