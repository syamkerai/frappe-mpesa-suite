"""What a Purchase Invoice or Purchase Order still needs paying (money_out.owed), and the B2B
request checks built on it (money_out.b2b), on an in-memory site whose SQL runs on SQLite.

Covers: every kind of money that counts before ERPNext lowers the outstanding (B2B in flight or
Paid without a submitted entry on the document, also on a cancelled request; B2C payouts; draft
entries, skipped when they carry a receipt already counted; an order's advance not yet moved
onto its invoice); the document and supplier-hold wording (an invoice on hold is refused whatever
its release date); the per-document lock shared with B2C; payouts read before the document row
under locking reads (R3, R4); earlier payouts to the supplier; and the request checks on save
and at submit.
"""

import datetime

import pytest

import b2b_fixture
import fake_frappe

PI, PO = "Purchase Invoice", "Purchase Order"


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        yield env


def left(env, dt=PI, dn="PINV-1", **kw):
    return env.owed.left_to_pay(dt, dn, **kw)


# ---------------------------------------------------------------- what counts
def test_nothing_in_flight_leaves_the_outstanding(env):
    env.pi(outstanding_amount=10000)
    result = left(env)
    assert (result.owed, result.parts, result.left) == (10000, [], 10000)


@pytest.mark.parametrize("state", [
    {"docstatus": 0, "status": "Not Initiated"},  # a draft counts, as before
    {"docstatus": 1, "status": "Not Initiated"},
    {"docstatus": 1, "status": "Initiated"},
    {"docstatus": 1, "status": "Unknown"},
    {"docstatus": 1, "status": "Paid"},  # Paid, no entry
    {"docstatus": 2, "status": "Paid"},  # paid after the request was cancelled: the money left (R2)
])
def test_b2b_in_flight_or_paid_without_an_entry_counts_in_full(env, state):
    env.pi()
    env.request(purchase_invoice="PINV-1", amount=4000, **state)
    result = left(env)
    assert [(p.kind, p.name, p.amount) for p in result.parts] == [("b2b", "MB2B-1", 4000)]
    assert result.left == 6000
    assert result.parts[0].label == f"MB2B-1 {state['status']} 4,000"


@pytest.mark.parametrize("state", [
    {"docstatus": 1, "status": "Failed"},
    {"docstatus": 2, "status": "Unknown"},  # cancelled by an admin who checked the statement
    {"docstatus": 2, "status": "Not Initiated"},
])
def test_failed_or_cancelled_b2b_does_not_count(env, state):
    env.pi()
    env.request(purchase_invoice="PINV-1", **state)
    assert left(env).left == 10000


def test_paid_b2b_stops_counting_once_a_submitted_entry_is_allocated_to_the_invoice(env):
    env.pi(outstanding_amount=6000)  # ERPNext lowered it when the entry was submitted
    env.request(purchase_invoice="PINV-1", amount=4000, transaction_id="RCPT1")
    env.pe("ACC-PAY-1", "RCPT1", [(PI, "PINV-1", 4000)])
    assert (left(env).parts, left(env).left) == ([], 6000)


@pytest.mark.parametrize("entry", [
    {"docstatus": 0},  # a draft does not lower the outstanding (R8)
    {"docstatus": 2},  # cancelled
])
def test_paid_b2b_with_a_draft_or_cancelled_entry_still_counts(env, entry):
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", amount=4000, transaction_id="RCPT1")
    env.pe("ACC-PAY-1", "RCPT1", [(PI, "PINV-1", 4000)], **entry)
    result = left(env)
    # the draft carries the receipt already counted, so it is not counted a second time
    assert [(p.kind, p.amount) for p in result.parts] == [("b2b", 4000)] and result.left == 6000


def test_paid_b2b_with_an_unallocated_entry_still_counts(env):
    env.pi()
    env.request(purchase_invoice="PINV-1", amount=4000, transaction_id="RCPT1")
    env.pe("ACC-PAY-1", "RCPT1", [])  # an advance to the supplier, not on the invoice
    assert left(env).left == 6000


def test_partly_allocated_entry_counts_the_rest(env):
    env.pi(outstanding_amount=7000)
    env.request(purchase_invoice="PINV-1", amount=4000, transaction_id="RCPT1")
    env.pe("ACC-PAY-1", "RCPT1", [(PI, "PINV-1", 3000)])
    assert [(p.kind, p.amount) for p in left(env).parts] == [("b2b", 1000)]


def test_journal_entry_with_the_receipt_counts_as_allocated(env):
    env.pi(outstanding_amount=6000)
    env.request(purchase_invoice="PINV-1", amount=4000, transaction_id="RCPT1")
    env.je("ACC-JV-1", "RCPT1", [
        {"account": "Creditors - AIL", "party_type": "Supplier", "party": "ACME", "reference_type": PI,
         "reference_name": "PINV-1", "debit_in_account_currency": 4000},
        {"account": b2b_fixture.GL, "credit_in_account_currency": 4000}])
    assert left(env).parts == []


def test_order_advance_moved_onto_the_invoice_counts_as_allocated_to_the_order(env):
    # Get Advances: the entry's order row became an invoice row naming the order (F4)
    env.po(advance_paid=4000)
    env.request(purchase_order="PO-1", amount=4000, transaction_id="RCPT1")
    env.pe("ACC-PAY-1", "RCPT1", [(PI, "PINV-9", 4000, PO, "PO-1")])
    assert left(env, PO, "PO-1").parts == []


def test_without_advance_voucher_columns_only_the_order_row_counts(env):
    env.frappe.db.columns = set()  # ERPNext before advance_voucher_no (Trident's 15.122 may be)
    env.po()
    env.request(purchase_order="PO-1", amount=4000, transaction_id="RCPT1")
    env.pe("ACC-PAY-1", "RCPT1", [(PO, "PO-1", 4000)])
    assert left(env, PO, "PO-1").parts == []
    assert not [e for e in env.log if e[0] == "sql" and "advance_voucher" in str(e)]


def test_b2c_payouts_count_both_lists(env):
    env.pi()
    env.b2c.append({"dt": PI, "dn": "PINV-1", "parent": "D-1", "allocated_amount": 1500, "request_status": "Initiated"})
    env.b2c_paid.append({"dt": PI, "dn": "PINV-1", "parent": "D-2", "allocated_amount": 500, "request_status": "Paid"})
    result = left(env)
    assert [(p.kind, p.name, p.amount) for p in result.parts] == [("b2c", "D-1", 1500), ("b2c", "D-2", 500)]
    assert result.left == 8000 and result.parts[0].label == "B2C D-1 Initiated 1,500"


def test_draft_entries_count_unless_they_carry_a_receipt_counted_already(env):
    env.pi()
    env.pe("ACC-PAY-50", "BANK-REF-1", [(PI, "PINV-1", 1000)], docstatus=0)  # a manual draft
    env.je("ACC-JV-7", "CHQ-1", [{"account": "Creditors - AIL", "reference_type": PI, "reference_name": "PINV-1",
                                  "debit_in_account_currency": 700}], docstatus=0)
    env.frappe.db.table("B2C Disbursement Request")["B2C-9"] = {
        "name": "B2C-9", "docstatus": 1, "status": "Paid", "reference_doctype": PI, "reference_name": "PINV-1",
        "transaction_id": "B2CRCPT"}
    env.pe("ACC-PAY-51", "B2CRCPT", [(PI, "PINV-1", 300)], docstatus=0)  # the B2C payout's own draft
    result = left(env)
    assert [(p.kind, p.name, p.amount) for p in result.parts] == [
        ("draft", "ACC-JV-7", 700), ("draft", "ACC-PAY-50", 1000)]
    assert result.parts[1].label == "draft Payment Entry ACC-PAY-50 1,000" and result.left == 8300


def test_order_advance_not_yet_on_the_invoice_is_taken_off(env):
    env.pi(outstanding_amount=10000)
    env.frappe.db.table("Purchase Invoice Item")["PII-1"] = {
        "name": "PII-1", "parent": "PINV-1", "parenttype": PI, "purchase_order": "PO-7"}
    env.pe("ACC-PAY-1", "BANKADV", [(PO, "PO-7", 3000)])  # an advance on the order
    env.request(purchase_order="PO-7", amount=2000, status="Initiated", transaction_id=None)  # in flight
    env.b2c.append({"dt": PO, "dn": "PO-7", "parent": "D-3", "allocated_amount": 500, "request_status": "Initiated"})
    result = left(env)
    (part,) = result.parts
    assert (part.kind, part.name, part.amount, result.left) == ("advance", "PO-7", 5500, 4500)
    assert part.label == "advance still on PO-7 5,500 (allocate it with Get Advances first)"


def test_order_advance_already_moved_onto_the_invoice_is_not_taken_off_twice(env):
    env.pi(outstanding_amount=7000)  # the advance is on it now
    env.frappe.db.table("Purchase Invoice Item")["PII-1"] = {
        "name": "PII-1", "parent": "PINV-1", "parenttype": PI, "purchase_order": "PO-7"}
    env.pe("ACC-PAY-1", "BANKADV", [(PI, "PINV-1", 3000, PO, "PO-7")])
    assert left(env).parts == [] and left(env).left == 7000


def test_the_request_being_checked_is_left_out_of_its_orders_advance_too(env):
    # a request first linked to PO-7, now checked against the invoice made from it (a draft moved
    # over, or Make Entry): it must not count against itself as an advance still on the order
    env.pi(outstanding_amount=10000)
    env.frappe.db.table("Purchase Invoice Item")["PII-1"] = {
        "name": "PII-1", "parent": "PINV-1", "parenttype": PI, "purchase_order": "PO-7"}
    env.request(purchase_order="PO-7", amount=10000, transaction_id="RCPT1", entry_mode="Make the entry later")
    assert [(p.kind, p.amount) for p in left(env).parts] == [("advance", 10000)] and left(env).left == 0
    result = left(env, exclude_request="MB2B-1")
    assert (result.parts, result.left) == ([], 10000)


def test_the_request_being_checked_is_left_out_and_left_is_never_negative(env):
    env.pi(outstanding_amount=3000)
    env.request(purchase_invoice="PINV-1", amount=4000, status="Initiated")
    assert left(env).left == 0
    assert left(env, exclude_request="MB2B-1").left == 3000


def test_an_order_owes_its_total_less_advance_paid(env):
    env.po(rounded_total=0, grand_total=20000, advance_paid=5000)
    assert left(env, PO, "PO-1").owed == 15000


def test_summary_and_explanation(env):
    env.pi()
    env.request(purchase_invoice="PINV-1", amount=2000, status="Initiated")
    env.pe("ACC-PAY-50", "BANK-REF-1", [(PI, "PINV-1", 1000)], docstatus=0)
    result = left(env)
    assert env.owed.summary(result) == {"outstanding": 10000, "in_progress": 2000, "drafts": 1000, "planned": 0,
                                        "order_advance": 0, "left": 7000}
    assert env.owed.explain("PINV-1", result) == (
        "PINV-1 owes 10,000, less MB2B-1 Initiated 2,000; draft Payment Entry ACC-PAY-50 1,000: 7,000 is left to pay.")


# ---------------------------------------------------------------- locks and read order
def test_lock_key_is_b2cs(env):
    with env.owed.doc_lock(PI, "PINV-1"):
        pass
    assert env.locks == ["b2c-pay:Purchase Invoice:PINV-1"]


def test_a_second_payer_waiting_on_the_lock_is_told_why(env):
    # a B2B submit holds the document's lock through its call to M-Pesa (Risks)
    env.pi()
    env.busy_keys.add("b2c-pay:Purchase Invoice:PINV-1")
    with pytest.raises(env.frappe.ValidationError,
                       match="Another payment for Purchase Invoice PINV-1 is being sent or booked. Try again in a "
                             "minute."):
        env.b2b.validate_request(draft(env), at_submit=True)


def test_locking_reads_payouts_before_the_document_row(env):
    env.pi()
    env.request(purchase_invoice="PINV-1", amount=4000, transaction_id="RCPT1")
    env.log.clear()
    left(env, for_update=True)
    reads = [e for e in env.log if e[0] in ("sql", "row", "b2c_open_payouts", "b2c_paid_without_entry")]
    document = [i for i, e in enumerate(reads) if e[0] == "row" and e[1] == PI]
    assert document == [len(reads) - 1], reads  # the document row is read once, last (R4)
    assert reads[-1][3] is True  # FOR UPDATE
    payouts = [e for e in reads if e[0] == "sql" and "Mpesa B2B Payment Request" in e[1]]
    assert payouts and all(e[2] for e in payouts)  # locking reads (R3)
    # receipts of B2C payouts and an invoice's order list only name what to skip or look at
    plain = {"B2C Disbursement Request", "Purchase Invoice Item"}
    assert all(e[2] for e in reads if e[0] == "sql" and not plain & set(e[1]))


def test_plain_reads_at_draft(env):
    env.pi()
    env.log.clear()
    left(env)
    assert not [e for e in env.log if (e[0] == "sql" and e[2]) or (e[0] == "row" and e[3])]


# ---------------------------------------------------------------- the document and the supplier
@pytest.mark.parametrize("values, message", [
    ({"docstatus": 0}, "PINV-1 is not submitted."),
    ({"is_return": 1}, "PINV-1 is a debit note. It cannot be paid."),
    ({"currency": "USD"}, "PINV-1 is in USD. M-Pesa pays KES only."),
    ({"party_account_currency": "USD"}, "PINV-1 is in USD. M-Pesa pays KES only."),
    # ERPNext refuses an invoice on hold in a Payment Entry whatever its release date (F2)
    ({"on_hold": 1, "release_date": datetime.date(2026, 1, 1)}, "PINV-1 is on hold. Release the hold before paying."),
])
def test_invoice_problems(env, values, message):
    row = env.pi(**values)
    assert env.owed.document_problems(PI, fake_frappe._dict(row)) == [message]


@pytest.mark.parametrize("values, message", [
    ({"docstatus": 2}, "PO-1 is not submitted."),
    ({"status": "On Hold"}, "PO-1 is On Hold."),
    ({"status": "Closed"}, "PO-1 is Closed."),
    ({"status": "Completed", "per_billed": 0}, "PO-1 is Completed."),
    ({"per_billed": 40}, "PO-1 already has invoices: pay the Purchase Invoice instead."),
    ({"currency": "EUR"}, "PO-1 is in EUR. M-Pesa pays KES only."),
])
def test_order_problems(env, values, message):
    row = env.po(**values)
    assert env.owed.document_problems(PO, fake_frappe._dict(row)) == [message]


def test_a_payable_document_has_no_problems(env):
    assert env.owed.document_problems(PI, fake_frappe._dict(env.pi())) == []
    assert env.owed.document_problems(PO, fake_frappe._dict(env.po())) == []


def test_supplier_hold_uses_erpnexts_own_check_when_there(env):
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=1, hold_type="Payments",
                                                    release_date=datetime.date(2026, 10, 30))
    env.frappe.db.table("Supplier")["ACME"]["is_blocked_for"] = lambda kind: kind == "Payments"
    assert env.owed.supplier_hold("ACME") == "ACME is on hold for payments until 30 Oct 2026."
    env.frappe.db.table("Supplier")["ACME"]["is_blocked_for"] = lambda kind: False
    assert env.owed.supplier_hold("ACME") is None


@pytest.mark.parametrize("values, held", [
    ({"hold_type": "Payments", "release_date": None}, True),
    ({"hold_type": "All", "release_date": datetime.date(2026, 10, 30)}, True),
    ({"hold_type": "All", "release_date": b2b_fixture.TODAY}, True),  # one day longer than 15.123 (Risks)
    ({"hold_type": "All", "release_date": datetime.date(2026, 10, 1)}, False),  # released
    ({"hold_type": "Invoices", "release_date": None}, False),  # holds invoices, not payments
])
def test_supplier_hold_without_is_blocked_for(env, values, held):
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=1, **values)
    assert bool(env.owed.supplier_hold("ACME")) is held


def test_supplier_not_on_hold(env):
    assert env.owed.supplier_hold("ACME") is None and env.owed.supplier_hold(None) is None


def test_linking_both_an_invoice_and_an_order_is_refused(env):
    with pytest.raises(env.frappe.ValidationError, match="Link an invoice or an order, not both."):
        env.owed.linked_document(fake_frappe._dict(purchase_invoice="PINV-1", purchase_order="PO-1"))


# ---------------------------------------------------------------- earlier payouts
def test_earlier_payouts_lists_unbooked_and_unallocated_money_of_the_supplier(env):
    env.request("MB2B-0001", amount=500, transaction_id="R1", entry_status="Entry Not Made")
    env.request("MB2B-0002", amount=700, transaction_id="R2", entry_status="Entry Made", payment_entry="ACC-PAY-2")
    env.pe("ACC-PAY-2", "R2", [], paid_amount=700)  # an unallocated supplier advance
    env.request("MB2B-0003", amount=900, transaction_id="R3", entry_status="Entry Made", payment_entry="ACC-PAY-3")
    env.pe("ACC-PAY-3", "R3", [(PI, "PINV-5", 900)], paid_amount=900)  # allocated since: not listed
    env.request("MB2B-0004", amount=100, transaction_id="R4", purchase_invoice="PINV-4")  # on an invoice
    env.request("MB2B-0005", amount=250, transaction_id="R5", entry_status="Entry Made", journal_entry="ACC-JV-5")
    env.request("MB2B-0006", amount=300, transaction_id="R6", entry_status="Entry Made", payment_entry="ACC-PAY-6",
                purchase_order="PO-3")
    env.pe("ACC-PAY-6", "R6", [], paid_amount=300)  # an order's request booked unallocated (by hand)
    found = env.owed.earlier_payouts("ACME")
    assert [f.name for f in found] == ["MB2B-0001", "MB2B-0002", "ACC-PAY-6"]
    assert found[1].label.endswith("(KES 700 unallocated)")


def test_no_supplier_no_earlier_payouts(env):
    assert env.owed.earlier_payouts(None) == []


# ---------------------------------------------------------------- the request's checks (money_out.b2b)
def draft(env, **values):
    row = env.request("MB2B-NEW", **{"docstatus": 0, "status": "Not Initiated", "transaction_id": None, "amount": 3000,
                                     "purchase_invoice": "PINV-1", "supplier": None, "company": None,
                                     "entry_mode": None, **values})
    return env.frappe.get_doc(b2b_fixture.B2B, row["name"])


def test_supplier_company_and_destination_come_from_the_invoice(env):
    env.pi()
    doc = draft(env, supplier_mpesa_account=None)
    env.b2b.validate_request(doc)
    assert (doc.supplier, doc.company, doc.supplier_mpesa_account, doc.receiver_shortcode) == (
        "ACME", "Astral", "MSA-1", "888880")
    assert doc.entry_mode == "Book automatically"


def test_a_typed_supplier_must_be_the_invoices(env):
    env.pi()
    with pytest.raises(env.frappe.ValidationError, match="PINV-1 belongs to ACME, not Other Ltd."):
        env.b2b.validate_request(draft(env, supplier="Other Ltd"))


def test_the_company_follows_the_invoice(env):
    # Frappe fills a new request's company from the user's default (ERPNext's v15 test site
    # makes another company the default): the invoice's company wins
    env.pi()
    doc = draft(env, company="Wind Power LLC")
    env.b2b.validate_request(doc)
    assert doc.company == "Astral"


def test_send_refuses_a_request_whose_company_is_not_the_invoices(env):
    # only server code (flags.ignore_validate) can store one; the shortcode pays for Astral
    env.pi(company="Other Co")
    env.request("MB2B-SRV", status="Not Initiated", transaction_id=None, purchase_invoice="PINV-1", amount=1000)
    with pytest.raises(env.frappe.ValidationError, match="PINV-1 belongs to Other Co, not Astral."):
        env.b2b.check_ready_to_send(env.frappe.get_doc(b2b_fixture.B2B, "MB2B-SRV"))


def test_amount_above_what_is_left_names_the_breakdown(env):
    env.pi(outstanding_amount=10000)
    env.request(purchase_invoice="PINV-1", amount=8000, status="Initiated")
    with pytest.raises(env.frappe.ValidationError,
                       match=r"KES 3,000 is more than is left to pay. PINV-1 owes 10,000, less MB2B-1 Initiated "
                             r"8,000: 2,000 is left to pay."):
        env.b2b.validate_request(draft(env))


def test_submit_checks_again_under_the_document_lock_with_locking_reads(env):
    env.pi()
    doc = draft(env)
    env.log.clear()
    env.b2b.validate_request(doc, at_submit=True)
    assert env.locks == ["b2c-pay:Purchase Invoice:PINV-1"]
    lock_at = env.log.index(("lock", "b2c-pay:Purchase Invoice:PINV-1"))
    assert ("sql", ("Mpesa B2B Payment Request",), True) in env.log[lock_at:]
    assert ("row", "Purchase Invoice", "PINV-1", True) in env.log[lock_at:]


def test_document_problems_stop_the_request(env):
    env.pi(on_hold=1)
    with pytest.raises(env.frappe.ValidationError, match="PINV-1 is on hold"):
        env.b2b.validate_request(draft(env))


def test_supplier_on_hold_stops_the_request(env):
    env.pi()
    env.frappe.db.table("Supplier")["ACME"].update(on_hold=1, hold_type="All", release_date=None)
    with pytest.raises(env.frappe.ValidationError, match="ACME is on hold for payments."):
        env.b2b.validate_request(draft(env))


def test_shortcode_of_another_company_is_refused(env):
    env.pi(company="Other Co")
    with pytest.raises(env.frappe.ValidationError, match=f"Mpesa Settings {b2b_fixture.SETTINGS} pays for Astral"):
        env.b2b.validate_request(draft(env))


def test_no_supplier_means_the_entry_is_made_later(env):
    env.frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"]["allow_one_off_b2b"] = 1
    env.frappe.user_roles = {"M-Pesa Manager"}
    doc = draft(env, purchase_invoice=None, supplier_mpesa_account=None, receiver_shortcode="555555",
                entry_mode="Book automatically")
    env.b2b.validate_request(doc)
    assert doc.entry_mode == "Make the entry later" and doc.company == "Astral"


def test_earlier_payouts_warn_on_save_and_block_submit_until_ticked(env):
    env.pi()
    env.request("MB2B-0001", amount=500, transaction_id="R1", entry_status="Entry Not Made")
    doc = draft(env)
    env.b2b.validate_request(doc)
    assert any("MB2B-0001" in m for m in env.frappe.messages)
    with pytest.raises(env.frappe.ValidationError, match="Check that none of them paid this invoice"):
        env.b2b.validate_request(doc, at_submit=True)
    doc.checked_earlier_payouts = 1
    env.b2b.validate_request(doc, at_submit=True)


def test_order_request_needs_an_unbilled_order(env):
    env.po(per_billed=50)
    with pytest.raises(env.frappe.ValidationError, match="already has invoices"):
        env.b2b.validate_request(draft(env, purchase_invoice=None, purchase_order="PO-1"))
    env.po()
    doc = draft(env, purchase_invoice=None, purchase_order="PO-1")
    env.b2b.validate_request(doc)
    assert doc.supplier == "ACME"


def test_amount_left_tells_the_form_everything(env):
    env.pi()
    env.request(purchase_invoice="PINV-1", amount=2000, status="Initiated")
    answer = env.b2b.amount_left(PI, "PINV-1")
    assert (answer["supplier"], answer["company"], answer["mpesa_settings"], answer["left"]) == (
        "ACME", "Astral", [b2b_fixture.SETTINGS], 8000)
    assert answer["parts"] == ["MB2B-1 Initiated 2,000"] and answer["problems"] == []
    assert answer["supplier_mpesa_account"] == "MSA-1" and answer["earlier_payouts"] == []
    assert env.b2b.amount_left(PI, "PINV-1", request="MB2B-1")["left"] == 10000


def test_amount_left_needs_read_permission(env):
    env.pi()
    env.denied.add((PI, "read"))
    with pytest.raises(env.frappe.PermissionError):
        env.b2b.amount_left(PI, "PINV-1")
