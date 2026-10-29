"""The 80 mm prints after a retry (round 4.2): a failed payout that a later M-Pesa payout of the same thing
paid (the same payee line, else the same payment schedule line, else the document's own party for the same
amount; never a payout of its own batch) is left off the document's advice and off a batch's proof, with a
line saying so; a failure nothing paid since stays; a failed payout's own proof, and a batch with no other
row, still show it, with where it was paid again."""

import datetime
import importlib
import json
from pathlib import Path

import fake_frappe
import pytest
from test_claim_payees import B2B, B2C, CLAIM, REQ, ROW, env  # noqa: F401 (fixtures)

_dict = fake_frappe._dict
AT = datetime.datetime(2026, 10, 29, 12, 58)
ROOT = Path(__file__).resolve().parents[2] / "mpsa_suite"


def at(minutes):
    return AT + datetime.timedelta(minutes=minutes)


@pytest.fixture
def pp(env, monkeypatch):
    module = importlib.import_module("mpsa_suite.money_out.payment_print")
    monkeypatch.setattr(module, "_sent_by", lambda doctype, name: None)
    monkeypatch.setattr(module, "_charge", lambda doctype, values: (None, ""))
    return module


def row(payout, status, minutes, payee=None, party="EMP-1", amount=1000, batch=None, line=None, paid=None):
    return _dict(payout=payout, status=status, created=at(minutes), payee=payee, party=party, amount=amount,
                 batch=batch, line=line, receipt="", when=at(paid) if paid is not None else at(minutes))


def mapped(pp, rows):
    return {k: v.payout for k, v in pp.paid_again(rows).items()}


# ---------------------------------------------------------------- the rule (pure)
def test_a_failure_a_later_payout_of_the_same_thing_paid_is_mapped_to_it(pp):
    rows = [row("F-1", "Failed", 0, payee="LN-1"), row("F-2", "Failed", 0, payee="LN-2"),
            row("P-1", "Paid", 8, payee="LN-1"), row("P-1b", "Paid", 9, payee="LN-1"),
            row("F-3", "Failed", 1), row("P-3", "Paid", 2),  # the claimant's own share, paid again
            row("F-4", "Failed", 4)]  # a later part payment of it that failed: P-3 came before, still owed
    assert mapped(pp, rows) == {"F-1": "P-1", "F-3": "P-3"}
    # F-1: the first payout paid after it (P-1, not P-1b); F-2: another line's payout paid, not its own


def test_another_partys_payout_or_another_amount_is_never_the_retry(pp):
    assert mapped(pp, [row("F-1", "Failed", 0, party="ACME"), row("P-1", "Paid", 5, party="OTHER")]) == {}
    # a deposit that failed and the balance paid later: a different part payment, still owed
    assert mapped(pp, [row("F-1", "Failed", 0, party="ACME", amount=4000),
                       row("P-1", "Paid", 5, party="ACME", amount=6000)]) == {}


def test_a_paid_row_of_the_failures_own_batch_is_never_its_retry(pp):
    """Two rows of one batch pay a document in parts: they were approved together, a part each."""
    rows = [row("REQ-1", "Failed", 0, batch="D-1"), row("REQ-2", "Paid", 0.02, batch="D-1")]
    assert mapped(pp, rows) == {}
    assert mapped(pp, rows + [row("REQ-9", "Paid", 30, batch="D-2")]) == {"REQ-1": "REQ-9"}


def test_another_schedule_instalment_is_not_the_retry_but_plan_again_on_its_line_is(pp):
    rows = [row("MB2B-A", "Failed", 0, party="ACME", line="L-A"),
            row("MB2B-B", "Paid", 20160, party="ACME", line="L-B")]  # instalment B, two weeks later
    assert mapped(pp, rows) == {}
    assert mapped(pp, rows + [row("MB2B-A2", "Paid", 60, party="ACME", line="L-A")]) == {"MB2B-A": "MB2B-A2"}


def test_an_older_try_retried_in_place_and_paid_counts_by_when_it_was_paid(pp):
    """REQ-1 failed, the re-pay REQ-2 failed too, then REQ-1 was retried in place and paid: REQ-1 keeps its
    creation, so it is ordered by when it was paid."""
    rows = [row("REQ-1", "Paid", 0, payee="LN-1", batch="D-1", paid=20), row("REQ-2", "Failed", 8, payee="LN-1",
                                                                              batch="D-2")]
    assert mapped(pp, rows) == {"REQ-2": "REQ-1"}
    # a part paid before the failure was made is still not its retry
    assert mapped(pp, [row("P-1", "Paid", 0, paid=1), row("F-1", "Failed", 5)]) == {}


@pytest.mark.parametrize("status", ["Initiated", "Not Initiated", "Unknown", "Failed"])
def test_only_a_paid_payout_is_a_retry_and_only_a_failed_one_is_left_off(pp, status):
    assert mapped(pp, [row("F-1", "Failed", 0, payee="LN-1"), row("R-1", status, 5, payee="LN-1")]) == {}
    assert mapped(pp, [row("R-1", status, 0, payee="LN-1"), row("P-1", "Paid", 5, payee="LN-1")]) == (
        {"R-1": "P-1"} if status == "Failed" else {})


# ---------------------------------------------------------------- the prints
def requests(env, *values):
    for v in values:
        env.frappe.db.table(REQ)[v["name"]] = {"docstatus": 1, "reference_doctype": CLAIM,
                                                "reference_name": "HR-EXP-1", "party": "EMP-1", "amount": 1000, **v}


def retried_claim(env):
    """HR-EXP-2026-00714 as Trident has it: lines paid on a second batch after the first one failed."""
    requests(env,
             {"name": "REQ-1", "status": "Failed", "creation": AT, "b2c_payment_disbursement": "D-1",
              "b2c_payment_disbursement_reference": "D-1-R1", "result_desc": "Insufficient balance"},
             {"name": "REQ-2", "status": "Paid", "creation": AT, "transaction_id": "UJS2",
              "b2c_payment_disbursement": "D-1", "b2c_payment_disbursement_reference": "D-1-R2"},
             {"name": "REQ-3", "status": "Failed", "creation": AT, "b2c_payment_disbursement": "D-1",
              "b2c_payment_disbursement_reference": "D-1-R3", "result_desc": "Insufficient balance"},
             {"name": "REQ-4", "status": "Paid", "creation": at(8), "transaction_id": "UJS4",
              "b2c_payment_disbursement": "D-2", "b2c_payment_disbursement_reference": "D-2-R1"})
    env.frappe.db.table(ROW).update({
        "D-1-R1": {"name": "D-1-R1", "claim_payee": "LN-1"}, "D-1-R2": {"name": "D-1-R2", "claim_payee": "LN-2"},
        "D-1-R3": {"name": "D-1-R3", "claim_payee": "LN-3"}, "D-2-R1": {"name": "D-2-R1", "claim_payee": "LN-1"}})


def test_the_advice_leaves_off_a_failure_paid_again_and_keeps_one_nothing_paid(env, pp):
    retried_claim(env)
    rows = pp.payment_rows(CLAIM, "HR-EXP-1")
    assert [(r.payout, r.status) for r in rows] == [("REQ-2", "Paid"), ("REQ-3", "Failed"), ("REQ-4", "Paid")]


def test_a_bank_payment_after_a_failure_is_never_taken_for_its_retry(env, pp):
    requests(env, {"name": "REQ-1", "status": "Failed", "creation": AT})
    env.frappe.db.table("Payment Entry Reference")["PER-1"] = {
        "name": "PER-1", "parent": "PE-1", "parenttype": "Payment Entry", "reference_doctype": CLAIM,
        "reference_name": "HR-EXP-1", "docstatus": 1, "allocated_amount": 1000}
    env.frappe.db.table("Payment Entry")["PE-1"] = {"name": "PE-1", "reference_no": "CHQ-9", "mode_of_payment": "Bank",
                                                    "posting_date": datetime.date(2026, 10, 30)}
    assert [(r.payout, r.status) for r in pp.payment_rows(CLAIM, "HR-EXP-1")] == [("REQ-1", "Failed"),
                                                                                  ("PE-1", "Paid")]


def batch_doc(env, name, rows):
    return fake_frappe.FakeDoc(env.frappe, B2C, {"name": name, "docstatus": 1, "status": "Partially Paid",
                                                 "creation": AT, "references": [_dict(r) for r in rows]})


def batch_row(i, status, request=None, **values):
    return {"name": f"D-1-R{i}", "idx": i, "party": "EMP-1", "partyb": "254711000331", "allocated_amount": 1000,
            "payment_status": status, "b2c_disbursement_request": request, "reference_doctype": CLAIM,
            "reference_name": "HR-EXP-1", **values}


def test_a_batch_leaves_off_its_rows_paid_again_later_with_a_line_saying_so(env, pp):
    retried_claim(env)
    doc = batch_doc(env, "D-1", [batch_row(i, status, f"REQ-{i}", claim_payee=f"LN-{i}")
                                 for i, status in ((1, "Failed"), (2, "Paid"), (3, "Failed"))])
    proof = pp.mpsa_payout_proof(doc)
    assert [(b.payout, b.status) for b in proof.blocks] == [("REQ-2", "Paid"), ("REQ-3", "Failed")]
    assert proof.paid_again_note == "1 failed row was paid again later (D-2): not listed."
    assert proof.total == 1000


def test_a_batch_row_that_failed_before_any_payout_is_left_off_once_its_line_was_paid(env, pp):
    """'Not sent' (the job was lost, the approved phone went on hold): no request; Plan Again paid the line
    on a later batch."""
    requests(env, {"name": "REQ-9", "status": "Paid", "creation": at(60), "transaction_id": "UJS9",
                   "b2c_payment_disbursement": "D-2", "b2c_payment_disbursement_reference": "D-2-R1"},
             {"name": "REQ-2", "status": "Paid", "creation": AT, "transaction_id": "UJS2",
              "b2c_payment_disbursement": "D-1", "b2c_payment_disbursement_reference": "D-1-R2"})
    env.frappe.db.table(ROW).update({"D-2-R1": {"name": "D-2-R1", "mpesa_schedule_line": "SL-1"},
                                     "D-1-R2": {"name": "D-1-R2", "mpesa_schedule_line": "SL-2"}})
    doc = batch_doc(env, "D-1", [batch_row(1, "Failed", mpesa_schedule_line="SL-1", creation=AT),
                                 batch_row(2, "Paid", "REQ-2", mpesa_schedule_line="SL-2")])
    proof = pp.mpsa_payout_proof(doc)
    assert [b.payout for b in proof.blocks] == ["REQ-2"]
    assert proof.paid_again_note == "1 failed row was paid again later (D-2): not listed."
    # its line not paid since: the row stays, Failed
    env.frappe.db.table(REQ)["REQ-9"]["status"] = "Failed"
    assert [b.status for b in pp.mpsa_payout_proof(doc).blocks] == ["Failed", "Paid"]


def test_a_batch_over_two_documents_reads_each_ones_payouts_and_names_each_retry(env, pp):
    retried_claim(env)
    requests(env, {"name": "REQ-5", "status": "Failed", "creation": AT, "reference_name": "HR-EXP-2",
                   "b2c_payment_disbursement": "D-1", "b2c_payment_disbursement_reference": "D-1-R5"},
             {"name": "REQ-6", "status": "Paid", "creation": at(30), "transaction_id": "UJS6", "reference_name": "HR-EXP-2",
              "b2c_payment_disbursement": "D-3", "b2c_payment_disbursement_reference": "D-3-R1"})
    env.frappe.db.table(ROW).update({"D-1-R5": {"name": "D-1-R5", "claim_payee": "LN-5"},
                                     "D-3-R1": {"name": "D-3-R1", "claim_payee": "LN-5"}})
    doc = batch_doc(env, "D-1", [batch_row(1, "Failed", "REQ-1", claim_payee="LN-1"),
                                 batch_row(2, "Paid", "REQ-2", claim_payee="LN-2"),
                                 batch_row(5, "Failed", "REQ-5", claim_payee="LN-5", reference_name="HR-EXP-2")])
    proof = pp.mpsa_payout_proof(doc)
    assert [b.payout for b in proof.blocks] == ["REQ-2"]
    assert proof.paid_again_note == "2 failed rows were paid again later (D-2, D-3): not listed."


def test_a_batch_with_no_other_row_and_a_failed_payouts_own_proof_still_show_it(env, pp):
    retried_claim(env)
    doc = batch_doc(env, "D-1", [batch_row(1, "Failed", "REQ-1", claim_payee="LN-1")])
    proof = pp.mpsa_payout_proof(doc)
    (block,) = proof.blocks
    assert (block.status, block.paid_again, proof.paid_again_note) == ("Failed", "REQ-4 · UJS4", "")
    own = pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, REQ, env.frappe.db.table(REQ)["REQ-1"]))
    assert [(b.status, b.note, b.paid_again) for b in own.blocks] == [("Failed", "Insufficient balance",
                                                                       "REQ-4 · UJS4")]
    nothing = pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, REQ, env.frappe.db.table(REQ)["REQ-3"]))
    assert [(b.status, b.paid_again) for b in nothing.blocks] == [("Failed", "")]  # nothing paid it since


def test_a_b2b_payout_paid_again_is_left_off_the_invoices_advice(env, pp):
    env.frappe.db.columns |= {(B2B, "purchase_invoice")}
    env.frappe.db.table(B2B).update({
        "MB2B-1": {"name": "MB2B-1", "docstatus": 1, "status": "Failed", "purchase_invoice": "PINV-1",
                   "supplier": "ACME", "amount": 500, "creation": AT, "receiver_shortcode": "835811"},
        "MB2B-2": {"name": "MB2B-2", "docstatus": 1, "status": "Paid", "purchase_invoice": "PINV-1",
                   "supplier": "ACME", "amount": 500, "creation": at(60), "transaction_id": "UJB2",
                   "receiver_shortcode": "835811"}})
    assert [r.payout for r in pp.payment_rows("Purchase Invoice", "PINV-1")] == ["MB2B-2"]
    own = pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, B2B, env.frappe.db.table(B2B)["MB2B-1"]))
    assert own.blocks[0].paid_again == "MB2B-2 · UJB2"


def test_the_payouts_are_read_with_fields_their_doctypes_have(env, pp, monkeypatch):
    """The fake get_all returns whole rows: check each field asked for is one the DocType defines."""
    def fields_of(folder):
        path = ROOT / "mpsa_suite" / "doctype" / folder / f"{folder}.json"
        return {f["fieldname"] for f in json.loads(path.read_text(encoding="utf-8"))["fields"]}

    known = {B2B: fields_of("mpesa_b2b_payment_request"), REQ: fields_of("b2c_disbursement_request"),
             ROW: fields_of("b2c_payment_disbursement_reference")}
    asked = []
    real = env.frappe.get_all

    def get_all(doctype, filters=None, fields=None, **kw):
        asked.append((doctype, fields))
        return real(doctype, filters=filters, fields=fields, **kw)

    monkeypatch.setattr(env.frappe, "get_all", get_all)
    env.frappe.db.columns |= {(B2B, "purchase_invoice")}
    retried_claim(env)
    pp.payment_rows(CLAIM, "HR-EXP-1")
    pp.payment_rows("Purchase Invoice", "PINV-1")
    seen = {d for d, _ in asked}
    assert {B2B, REQ, ROW} <= seen
    for doctype, fields in asked:
        if doctype in known:
            assert set(fields) - {"name", "docstatus", "creation"} <= known[doctype], doctype


def test_the_proof_template_prints_where_a_failure_was_paid_again():
    text = (ROOT / "templates" / "print" / "mpsa_payout_proof_80mm.html").read_text(encoding="utf-8")
    assert "b.paid_again" in text and "p.paid_again_note" in text
