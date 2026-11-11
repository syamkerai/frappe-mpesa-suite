"""The 80 mm payment prints' data (money_out.payment_print, round 4.1) on the in-memory site of
test_claim_payees: the name M-Pesa paid split on its first " - ", phones masked as the send summary
shows them, a Payment Entry carrying a payout's receipt printed once, what was paid and is left read
from the entries (not a stored total), a salary's amounts hidden from who may not see salaries, the
proof's SMS line, and the reader's permission checked first."""

import datetime
import importlib

import fake_frappe
import pytest
from test_claim_payees import B2C, CLAIM, REQ, ROW, env, hrms_modules, hrms_stub  # noqa: F401 (fixtures)

_dict = fake_frappe._dict


@pytest.fixture
def pp(env):
    return importlib.import_module("mpsa_suite.money_out.payment_print")


@pytest.mark.parametrize("value, parts", [
    ("254722000111 - JANE DOE", ("254722000111", "JANE DOE")),
    ("835811 - JOHN-PAUL KAMAU - SHOP", ("835811", "JOHN-PAUL KAMAU - SHOP")),  # the first " - " only
    ("MARY-ANNE WANJIKU", (None, "MARY-ANNE WANJIKU")),  # a hyphenated name is never cut
    ("", (None, "")),
])
def test_the_name_mpesa_paid_is_split_on_its_first_spaced_hyphen(pp, value, parts):
    assert pp.split_receiver(value) == parts


@pytest.mark.parametrize("value, shown", [
    ("254722000111", "2547****0111"), ("0722000111", "2547****0111"), ("2547****0111", "2547****0111"),
    ("835811", "835811"), ("1234567890", "****7890"), ("PK-77", "PK-77"), ("PARK-0123456789", "****6789"), (None, ""),
])
def test_numbers_are_masked_as_the_send_summary_shows_them(pp, value, shown, masked_numbers):
    assert pp.masked(value) == shown


def test_the_proof_reads_like_the_mpesa_sms(pp):
    when = datetime.datetime(2026, 10, 28, 13, 57)
    assert pp.sms("UJSSN5D5CE", 50, "ASTRAL INDUSTRIES LTD HQ", "", when, False) == (
        "UJSSN5D5CE Confirmed. KES 50.00 paid to ASTRAL INDUSTRIES LTD HQ on 28/10/26 at 1:57 PM.")
    assert pp.sms("UJS2", 1500, "JANE DOE", "2547****0111", when.replace(hour=9), True) == (
        "UJS2 Confirmed. KES 1,500.00 sent to JANE DOE 2547****0111 on 28/10/26 at 9:57 AM.")


def test_an_entry_carrying_a_payouts_receipt_is_printed_once(pp):
    entries = [_dict(name="PE-1", reference_no="ujs1abc"), _dict(name="PE-2", reference_no="BANK-77"),
               _dict(name="PE-3", reference_no=None)]
    assert [e.name for e in pp.without_covered(entries, ["UJS1ABC", ""])] == ["PE-2", "PE-3"]


def test_a_claims_payments_list_its_payouts_and_other_entries_once(env, pp, masked_numbers):
    db = env.frappe.db
    db.table(REQ).update({
        "REQ-1": {"name": "REQ-1", "docstatus": 1, "status": "Paid", "transaction_id": "UJS1", "amount": 2000,
                  "receiver_public_name": "254711000333 - JANE WANJIKU", "phone_number": "254711000333",
                  "reference_doctype": CLAIM, "reference_name": "HR-EXP-1", "creation": datetime.datetime(2026, 10, 28, 9),
                  "transaction_completed_datetime": datetime.datetime(2026, 10, 28, 9, 5),
                  "b2c_payment_disbursement_reference": "D-1-R1"},
        "REQ-2": {"name": "REQ-2", "docstatus": 2, "status": "Failed", "reference_doctype": CLAIM,
                  "reference_name": "HR-EXP-1", "amount": 10}})  # cancelled unpaid: not printed
    db.table(ROW)["D-1-R1"] = {"name": "D-1-R1", "claim_payee": "LN-1"}
    db.table("Payment Entry Reference").update({
        "PER-1": {"name": "PER-1", "parent": "PE-1", "parenttype": "Payment Entry", "reference_doctype": CLAIM,
                  "reference_name": "HR-EXP-1", "docstatus": 1, "allocated_amount": 2000},
        "PER-2": {"name": "PER-2", "parent": "PE-2", "parenttype": "Payment Entry", "reference_doctype": CLAIM,
                  "reference_name": "HR-EXP-1", "docstatus": 1, "allocated_amount": 500}})
    db.table("Payment Entry").update({
        "PE-1": {"name": "PE-1", "reference_no": "UJS1", "posting_date": datetime.date(2026, 10, 28)},
        "PE-2": {"name": "PE-2", "reference_no": "CHQ-9", "mode_of_payment": "Cash", "party_name": "Mary",
                 "posting_date": datetime.date(2026, 10, 29)}})
    rows = pp.payment_rows(CLAIM, "HR-EXP-1")
    assert [(r.via, r.receipt, r.amount, r.name, r.number, r.payee) for r in rows] == [
        ("M-Pesa B2C", "UJS1", 2000, "JANE WANJIKU", "2547****0333", "LN-1"),
        ("Cash", "CHQ-9", 500, "Mary", "", None)]
    assert rows[0].when_text == "28/10/26 09:05"


def test_what_was_paid_comes_from_the_entries_not_a_stored_total(env, pp, hrms_modules, monkeypatch):
    hrms_stub(env, booked=90)
    claim = _dict(doctype=CLAIM, name="HR-EXP-1", grand_total=100, total_amount_reimbursed=70)  # a lost update
    assert pp.paid_and_left(CLAIM, claim) == (100, 90, 10)
    monkeypatch.setattr(env.frappe.db, "sql", lambda query, values=None, **kw: [[1500.0]])
    invoice = _dict(doctype="Purchase Invoice", name="PINV-1", grand_total=5000, rounded_total=0,
                    outstanding_amount=5000)  # the stored figure is not read while the ledger has one
    assert pp.paid_and_left("Purchase Invoice", invoice) == (5000, 3500, 1500)
    # R14: a slip as the suite reads it (salary_slip_paid: Paid payouts and the payroll's bank entry)
    disb = importlib.import_module("mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement")
    monkeypatch.setattr(disb, "salary_slip_paid", lambda name, employee=None, payroll_entry=None, **kw: 30000)
    slip = _dict(doctype="Salary Slip", name="SAL-1", rounded_total=30000, employee="EMP-1", payroll_entry="PAY-1")
    rows = [_dict(amount=30000, status="Failed")]  # the M-Pesa payout failed; the payroll's bank entry paid it
    assert pp.paid_and_left("Salary Slip", slip, rows) == (30000, 30000, 0)
    # fix check S11: and the advice lists that bank entry, so its rows agree with its total
    monkeypatch.setattr(disb, "slip_bank_payments", lambda employee, payroll_entry: [_dict(
        name="ACC-JV-1", voucher_type="Bank Entry", cheque_no="CHQ-7", posting_date=datetime.date(2026, 10, 25),
        amount=30000)] if (employee, payroll_entry) == ("EMP-1", "PAY-1") else [])
    listed = pp.payment_rows("Salary Slip", "SAL-1", _dict(slip, employee_name="Mary Atieno"))
    assert [(r.via, r.payout, r.receipt, r.amount, r.status, r.name) for r in listed] == [
        ("Bank Entry", "ACC-JV-1", "CHQ-7", 30000, "Paid", "Mary Atieno")]
    assert pp.payment_rows("Salary Slip", "SAL-1") == []  # no slip given: as before


def test_a_salary_slips_amounts_are_hidden_from_who_may_not_see_salaries(env, pp, monkeypatch):
    salary_access = importlib.import_module("mpsa_suite.money_out.salary_access")
    monkeypatch.setattr(salary_access, "can_see_salaries", lambda user=None: False)
    monkeypatch.setattr(pp, "payment_rows", lambda dt, dn, doc=None: [_dict(amount=20000, status="Paid", payee=None)])
    slip = fake_frappe.FakeDoc(env.frappe, "Salary Slip", {"name": "SAL-1", "employee": "EMP-1", "net_pay": 30000,
                                                           "docstatus": 1})
    advice = pp.mpsa_payment_advice(slip)
    assert advice.hidden and (advice.total, advice.paid, advice.left) == (None, None, None)
    assert advice.rows[0].amount is None


def test_a_b2b_proof_reads_like_the_sms_with_the_shortcode_it_left_from(env, pp, monkeypatch):
    monkeypatch.setattr(pp, "_sent_by", lambda doctype, name: "payer@example.com")
    env.frappe.db.table("Mpesa Settings")["S-1"] = {"name": "S-1", "business_shortcode": "600999"}
    request = fake_frappe.FakeDoc(env.frappe, "Mpesa B2B Payment Request", {
        "name": "MB2B-1", "docstatus": 1, "status": "Paid", "transaction_id": "UJSSN5D5CE", "amount": 50,
        "receiver_public_name": "835811 - ASTRAL INDUSTRIES LTD HQ", "receiver_shortcode": "835811",
        "command_id": "BusinessBuyGoods", "mpesa_settings": "S-1", "debit_party_charges": "Buy Goods Charge|KES|2.00",
        "trans_completed_time": datetime.datetime(2026, 10, 28, 13, 57), "supplier": "ACME", "owner": "acc@example.com",
        "purchase_invoice": "PINV-1"})
    proof = pp.mpsa_payout_proof(request)
    (block,) = proof.blocks
    assert block.sms == "UJSSN5D5CE Confirmed. KES 50.00 paid to ASTRAL INDUSTRIES LTD HQ 835811 on 28/10/26 at 1:57 PM."
    assert (block.paid_from, block.charge, block.receipt, block.when_text) == ("600999", 2.0, "UJSSN5D5CE",
                                                                               "28/10/26 13:57")
    assert block.document.endswith("PINV-1")


def test_a_print_checks_the_readers_permission_first(env, pp):
    env.denied.add(("Mpesa B2B Payment Request", "read"))
    with pytest.raises(env.frappe.PermissionError):
        pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, "Mpesa B2B Payment Request", {"name": "MB2B-1"}))
    with pytest.raises(env.frappe.PermissionError):
        pp.proof_recipients("Mpesa B2B Payment Request", "MB2B-1")


def test_send_payment_proof_offers_the_partys_email_and_the_suites_format(env, pp):
    env.frappe.db.table("Mpesa B2B Payment Request")["MB2B-1"] = {"name": "MB2B-1", "supplier": "ACME",
                                                                  "employee": None}
    env.frappe.db.table("Supplier")["ACME"] = {"name": "ACME", "email_id": "pay@acme.example.com"}
    assert pp.proof_recipients("Mpesa B2B Payment Request", "MB2B-1") == {
        "doctype": "Mpesa B2B Payment Request", "name": "MB2B-1", "recipients": ["pay@acme.example.com"],
        "print_format": "M-Pesa 80mm Mpesa B2B Payment Request",
        "subject": "Proof of payment: Mpesa B2B Payment Request MB2B-1"}
    env.denied.add(("Mpesa B2B Payment Request", "email"))  # R12: who may not send email on it gets no composer
    with pytest.raises(env.frappe.PermissionError):
        pp.proof_recipients("Mpesa B2B Payment Request", "MB2B-1")


def batch(env, rows, **kw):
    env.frappe.db.table(B2C)["D-1"] = {"name": "D-1", "docstatus": 1, "status": "Paid", "party_type": "Employee",
                                       "references": [_dict(r) for r in rows], **kw}


def test_a_batch_of_several_parties_is_proved_one_paid_row_to_its_own_party(env, pp, masked_numbers):
    """R1/R2: never the whole batch to everyone; each party gets their own payout's proof only."""
    db = env.frappe.db
    env.meta_fields["Employee"] = {"cell_number", "bank_ac_no", "prefered_email"}
    db.table("Employee").update({"EMP-1": dict(db.table("Employee")["EMP-1"], prefered_email="one@example.com"),
                                 "EMP-2": dict(db.table("Employee")["EMP-2"], prefered_email="two@example.com")})
    batch(env, [{"name": "D-1-R1", "idx": 1, "party_type": "Employee", "party": "EMP-1", "partyb": "254711000331",
                 "allocated_amount": 1500, "payment_status": "Paid", "b2c_disbursement_request": "REQ-1",
                 "reference_doctype": CLAIM},
                {"name": "D-1-R2", "idx": 2, "party_type": "Employee", "party": "EMP-2", "partyb": "254711000332",
                 "allocated_amount": 900, "payment_status": "Paid", "b2c_disbursement_request": "REQ-2",
                 "reference_doctype": CLAIM},
                {"name": "D-1-R3", "idx": 3, "party_type": "Employee", "party": "EMP-2", "partyb": "254711000333",
                 "allocated_amount": 100, "payment_status": "Failed", "b2c_disbursement_request": "REQ-3",
                 "reference_doctype": CLAIM}], transaction_to_pay_against=CLAIM)
    db.table(REQ).update({"REQ-2": {"name": "REQ-2", "party_type": "Employee", "party": "EMP-2",
                                    "reference_doctype": CLAIM}})
    env.denied.add((REQ, "email"))  # fix check S8: each row is proved on its request, so who may not email those
    with pytest.raises(env.frappe.PermissionError, match="you may not send email on a B2C Disbursement Request"):
        pp.proof_recipients(B2C, "D-1")
    env.denied.discard((REQ, "email"))
    choose = pp.proof_recipients(B2C, "D-1")
    assert choose == {"rows": [{"row": "D-1-R1", "label": "#1 EMP-1 2547****0331 KES 1,500.00"},
                               {"row": "D-1-R2", "label": "#2 EMP-2 2547****0332 KES 900.00"}]}  # paid rows only
    proof = pp.proof_recipients(B2C, "D-1", row="D-1-R2")
    assert (proof["doctype"], proof["name"], proof["recipients"], proof["print_format"]) == (
        REQ, "REQ-2", ["two@example.com"], "M-Pesa 80mm B2C Disbursement Request")
    with pytest.raises(env.frappe.ValidationError, match="is no paid payout of it"):
        pp.proof_recipients(B2C, "D-1", row="D-1-R3")


def test_a_salary_payout_gets_no_payment_proof_by_email(env, pp):
    batch(env, [{"name": "D-1-R1", "idx": 1, "party_type": "Employee", "party": "EMP-1", "payment_status": "Paid",
                 "reference_doctype": "Salary Slip", "b2c_disbursement_request": "REQ-1"}],
          transaction_to_pay_against="Salary Slip")
    with pytest.raises(env.frappe.ValidationError, match="A salary payout gets no payment proof by email"):
        pp.proof_recipients(B2C, "D-1")
    env.frappe.db.table("Mpesa B2B Payment Request")["MB2B-1"] = {"name": "MB2B-1", "employee": "EMP-1",
                                                                  "salary_slip": "SAL-1"}
    with pytest.raises(env.frappe.ValidationError, match="A salary payout gets no payment proof by email"):
        pp.proof_recipients("Mpesa B2B Payment Request", "MB2B-1")
    # fix check S7: an employee paid by B2B for no slip, claim or advance is a salary too (salary_access)
    env.frappe.db.table("Mpesa B2B Payment Request")["MB2B-2"] = {"name": "MB2B-2", "employee": "EMP-1"}
    with pytest.raises(env.frappe.ValidationError, match="A salary payout gets no payment proof by email"):
        pp.proof_recipients("Mpesa B2B Payment Request", "MB2B-2")
    env.frappe.db.table("Mpesa B2B Payment Request")["MB2B-3"] = {"name": "MB2B-3", "employee": "EMP-1",
                                                                  "expense_claim": CLAIM + "-1"}
    assert pp.proof_recipients("Mpesa B2B Payment Request", "MB2B-3")["name"] == "MB2B-3"


def test_the_proofs_total_is_what_was_paid(env, pp, monkeypatch):
    """R11: a failed or unsent row moved no money."""
    monkeypatch.setattr(pp, "_sent_by", lambda doctype, name: None)
    rows = [{"name": f"D-1-R{i}", "idx": i, "party": "EMP-1", "partyb": "254711000331", "allocated_amount": 1000,
             "payment_status": status} for i, status in ((1, "Paid"), (2, "Failed"), (3, "Not Initiated"))]
    doc = fake_frappe.FakeDoc(env.frappe, B2C, {"name": "D-1", "docstatus": 1, "status": "Partially Paid",
                                                "references": [_dict(r) for r in rows]})
    assert pp.mpsa_payout_proof(doc).total == 1000


def test_a_claims_payee_account_numbers_are_masked_on_its_advice(env, pp, masked_numbers):
    """R13: a bank or paybill account number as ****6789, as the payout rows show it."""
    cp = importlib.import_module("mpsa_suite.money_out.claim_payees")
    line = _dict(payee_type=cp.BANK, shortcode="522522", account_number="0123456789")
    assert pp._line_to(line, cp) == "522522 / ****6789"
    assert pp._line_to(_dict(payee_type=cp.TILL, shortcode="835811"), cp) == "835811"
    assert pp._line_to(_dict(payee_type=cp.PHONE, phone="254711000333"), cp) == "2547****0333"


def test_an_advances_returns_are_counted_apart_and_an_orders_moved_advance_still_counts(env, pp):
    """R14: HRMS's Return of an advance is not netted off what was paid. R6: an order's advance that Get
    Advances moved onto an invoice keeps the order as its advance voucher."""
    db = env.frappe.db
    db.columns = db.columns | {("Payment Entry Reference", "advance_voucher_no")}
    db.table("Payment Entry Reference").update({
        "PER-1": {"name": "PER-1", "parent": "PE-1", "parenttype": "Payment Entry", "docstatus": 1,
                  "reference_doctype": "Employee Advance", "reference_name": "EA-1", "allocated_amount": 10000},
        "PER-2": {"name": "PER-2", "parent": "PE-2", "parenttype": "Payment Entry", "docstatus": 1,
                  "reference_doctype": "Purchase Invoice", "reference_name": "PINV-1", "allocated_amount": 50000,
                  "advance_voucher_type": "Purchase Order", "advance_voucher_no": "PO-1"}})
    db.table("Payment Entry").update({"PE-1": {"name": "PE-1", "payment_type": "Pay"},
                                      "PE-2": {"name": "PE-2", "payment_type": "Pay"}})
    real_sql = db.sql
    db.sql = lambda query, values=None, **kw: [[0, 3000]] if "Journal Entry Account" in query else real_sql(
        query, values, **kw)
    advance = _dict(doctype="Employee Advance", name="EA-1", advance_amount=10000)
    assert pp.paid_and_left("Employee Advance", advance) == (10000, 10000, 0)
    assert pp._allocated("Employee Advance", "EA-1") == (10000, 3000)
    db.sql = lambda query, values=None, **kw: [[0, 0]] if "Journal Entry Account" in query else real_sql(
        query, values, **kw)
    order = _dict(doctype="Purchase Order", name="PO-1", grand_total=50000, rounded_total=50000)
    assert pp.paid_and_left("Purchase Order", order) == (50000, 50000, 0)
    db.sql = real_sql


def test_the_print_formats_are_standard_80mm_jinja_and_read_only_through_the_helpers():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "mpsa_suite"
    formats = sorted((root / "mpsa_suite" / "print_format").glob("*/*.json"))
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in formats]
    assert sorted(d["doc_type"] for d in docs) == sorted([
        "Purchase Invoice", "Purchase Order", "Expense Claim", "Employee Advance", "Salary Slip",
        "Mpesa B2B Payment Request", "B2C Payment Disbursement", "B2C Disbursement Request", "Payment Entry"])
    for d, path in zip(docs, formats):
        assert (d["standard"], d["module"], d["print_format_type"], d["custom_format"]) == ("Yes", "Mpsa Suite",
                                                                                          "Jinja", 1)
        assert path.parent.name == d["name"].lower().replace("-", "_").replace(" ", "_")
    templates = root / "templates" / "print"
    for name in ("mpsa_payment_advice_80mm.html", "mpsa_payout_proof_80mm.html", "mpsa_80mm_style.html"):
        text = (templates / name).read_text(encoding="utf-8")
        assert "get_all" not in text and "get_list" not in text and "4139683" not in text
    assert "size: 80mm auto; margin: 0" in (templates / "mpsa_80mm_style.html").read_text(encoding="utf-8")
