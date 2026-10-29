"""The 80 mm prints after a retry (round 4.2): a failed payout that a later M-Pesa payout of the same
thing paid (the same payee line, else the document's own party) is left off the document's advice and
off a batch's proof, with a line saying so; a failure nothing paid since stays; a failed payout's own
proof, and a batch with no other row, still show it, with where it was paid again."""

import datetime
import importlib

import fake_frappe
import pytest
from test_claim_payees import B2B, B2C, CLAIM, REQ, ROW, env  # noqa: F401 (fixtures)

_dict = fake_frappe._dict
AT = datetime.datetime(2026, 10, 29, 12, 58)


@pytest.fixture
def pp(env, monkeypatch):
    module = importlib.import_module("mpsa_suite.money_out.payment_print")
    monkeypatch.setattr(module, "_sent_by", lambda doctype, name: None)
    monkeypatch.setattr(module, "_charge", lambda doctype, values: (None, ""))
    return module


def row(payout, status, minutes, payee=None, party="EMP-1", receipt="", batch=None):
    return _dict(payout=payout, status=status, created=AT + datetime.timedelta(minutes=minutes), payee=payee,
                 party=party, receipt=receipt, batch=batch)


def test_a_failure_a_later_payout_of_the_same_thing_paid_is_mapped_to_it(pp):
    rows = [row("F-1", "Failed", 0, payee="LN-1"), row("F-2", "Failed", 0, payee="LN-2"),
            row("P-1", "Paid", 8, payee="LN-1"), row("P-1b", "Paid", 9, payee="LN-1"),
            row("F-3", "Failed", 1), row("P-3", "Paid", 2),  # the claimant's own share, paid again
            row("F-4", "Failed", 4)]  # a later part payment of it that failed: P-3 came before, still owed
    again = pp.paid_again(rows)
    assert {k: v.payout for k, v in again.items()} == {"F-1": "P-1", "F-3": "P-3"}
    # F-1: the first payout paid after it (P-1, not P-1b); F-2: another line's payout paid, not its own


def test_another_partys_payout_never_counts_as_the_retry(pp):
    rows = [row("F-1", "Failed", 0, party="ACME"), row("P-1", "Paid", 5, party="OTHER")]
    assert pp.paid_again(rows) == {}


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
             {"name": "REQ-4", "status": "Paid", "creation": AT + datetime.timedelta(minutes=8),
              "transaction_id": "UJS4", "b2c_payment_disbursement": "D-2",
              "b2c_payment_disbursement_reference": "D-2-R1"})
    env.frappe.db.table(ROW).update({
        "D-1-R1": {"name": "D-1-R1", "claim_payee": "LN-1"}, "D-1-R2": {"name": "D-1-R2", "claim_payee": "LN-2"},
        "D-1-R3": {"name": "D-1-R3", "claim_payee": "LN-3"}, "D-2-R1": {"name": "D-2-R1", "claim_payee": "LN-1"}})


def test_the_advice_leaves_off_a_failure_paid_again_and_keeps_one_nothing_paid(env, pp):
    retried_claim(env)
    rows = pp.payment_rows(CLAIM, "HR-EXP-1")
    assert [(r.payout, r.status) for r in rows] == [("REQ-2", "Paid"), ("REQ-3", "Failed"), ("REQ-4", "Paid")]


def batch_doc(env, name, rows):
    return fake_frappe.FakeDoc(env.frappe, B2C, {"name": name, "docstatus": 1, "status": "Partially Paid",
                                                 "references": [_dict(r) for r in rows]})


def test_a_batch_leaves_off_its_rows_paid_again_later_with_a_line_saying_so(env, pp):
    retried_claim(env)
    doc = batch_doc(env, "D-1", [
        {"name": f"D-1-R{i}", "idx": i, "party": "EMP-1", "partyb": "254711000331", "allocated_amount": 1000,
         "payment_status": status, "b2c_disbursement_request": f"REQ-{i}", "claim_payee": f"LN-{i}"}
        for i, status in ((1, "Failed"), (2, "Paid"), (3, "Failed"))])
    proof = pp.mpsa_payout_proof(doc)
    assert [(b.payout, b.status) for b in proof.blocks] == [("REQ-2", "Paid"), ("REQ-3", "Failed")]
    assert proof.paid_again_note == "1 failed payout was paid again later (D-2): not listed."
    assert proof.total == 1000


def test_a_batch_with_no_other_row_and_a_failed_payouts_own_proof_still_show_it(env, pp):
    retried_claim(env)
    doc = batch_doc(env, "D-1", [{"name": "D-1-R1", "idx": 1, "party": "EMP-1", "partyb": "254711000331",
                                  "allocated_amount": 1000, "payment_status": "Failed",
                                  "b2c_disbursement_request": "REQ-1", "claim_payee": "LN-1"}])
    proof = pp.mpsa_payout_proof(doc)
    (block,) = proof.blocks
    assert (block.status, block.paid_again, proof.paid_again_note) == ("Failed", "REQ-4 · UJS4", "")
    own = pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, REQ, env.frappe.db.table(REQ)["REQ-1"]))
    assert [(b.status, b.note, b.paid_again) for b in own.blocks] == [("Failed", "Insufficient balance",
                                                                       "REQ-4 · UJS4")]
    nothing = pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, REQ, env.frappe.db.table(REQ)["REQ-3"]))
    assert [(b.status, b.paid_again) for b in nothing.blocks] == [("Failed", "")]  # nothing paid it since


def test_a_b2b_payout_paid_again_is_left_off_the_invoices_advice(env, pp):
    if not env.frappe.db.has_column(B2B, "purchase_invoice"):
        env.frappe.db.columns |= {(B2B, "purchase_invoice")}
    env.frappe.db.table(B2B).update({
        "MB2B-1": {"name": "MB2B-1", "docstatus": 1, "status": "Failed", "purchase_invoice": "PINV-1",
                   "supplier": "ACME", "amount": 500, "creation": AT, "receiver_shortcode": "835811"},
        "MB2B-2": {"name": "MB2B-2", "docstatus": 1, "status": "Paid", "purchase_invoice": "PINV-1",
                   "supplier": "ACME", "amount": 500, "creation": AT + datetime.timedelta(hours=1),
                   "transaction_id": "UJB2", "receiver_shortcode": "835811"}})
    assert [r.payout for r in pp.payment_rows("Purchase Invoice", "PINV-1")] == ["MB2B-2"]
    own = pp.mpsa_payout_proof(fake_frappe.FakeDoc(env.frappe, B2B, env.frappe.db.table(B2B)["MB2B-1"]))
    assert own.blocks[0].paid_again == "MB2B-2 · UJB2"


def test_the_proof_template_prints_where_a_failure_was_paid_again():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "mpsa_suite" / "templates" / "print"
            / "mpsa_payout_proof_80mm.html").read_text(encoding="utf-8")
    assert "b.paid_again" in text and "p.paid_again_note" in text
