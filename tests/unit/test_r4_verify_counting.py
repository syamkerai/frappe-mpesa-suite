"""Round 4 verify pass, counting group (docs/r4_notes/VERIFY_COUNTING.md), on the fake sites of the
neighbouring tests.

K1: a B2C payout of an Expense Claim or Employee Advance booked in part (its entry took only what
HRMS still owed) counts its unallocated rest, as a B2B payout's does, so a cancelled bank entry does
not make that money look owed again. K2: a named M-Pesa Manager settles a Draft or Failed payee line
outside M-Pesa (settle_line): Cancelled with a note, no longer held from the employee, never paid by
M-Pesa after. K13: a claim is not cancelled while a payee line's payout may have paid, a cancelled
Unknown request too. K14: the claim's and advance's Cancel lists none of their M-Pesa payouts.
"""

import importlib

import fake_frappe
import pytest
from test_claim_payees import (  # noqa: F401
    B2B,
    B2C,
    CLAIM,
    LINE,
    PAYBILL,
    REQ,
    claim,
    disbursement,
    env,
    payee,
    raises,
    request,
    stored,
)
from test_employee_pay_js import run as run_js
from test_fix2_b2c2 import frappe, owed  # noqa: F401
from test_r4_buttons_js import JS
from test_r4_buttons_js import run as run_desk

_dict = fake_frappe._dict
EA = "Employee Advance"
OWED = "mpsa_suite.money_out.owed"


# ---------------------------------------------------------------- K1: a partly booked B2C payout
def _booked(owed, monkeypatch, allocated):  # noqa: F811
    """Every payout booked (a submitted entry carries its receipt); ``allocated``: what entries carrying
    each receipt allocate to the document."""
    asked = []

    def allocated_on(dt, dn, receipt, for_update=False):
        asked.append((dt, dn, receipt, for_update))
        return allocated.get(receipt, 0.0)

    monkeypatch.setattr(owed.post, "booked_docstatus", lambda disbursement, row, for_update=False: 1)
    monkeypatch.setattr(importlib.import_module(OWED), "allocated_on", allocated_on)
    return asked


def test_a_partly_booked_claim_payout_counts_what_its_entry_left_unallocated(owed, monkeypatch):  # noqa: F811
    # EC-1 owed 5,000 and was paid 2,000 by bank while B2C D-2 was on its way: D-2's entry took 3,000; the bank
    # entry is then cancelled, and the 2,000 left on D-2's entry still counts, as a B2B payout's rest does
    asked = _booked(owed, monkeypatch, {"SKT2": 3000})
    owed.paid = [{"request": "B2C-2", "sent_from": "D-2", "parent": "D-2", "amount": 5000, "transaction_id": "SKT2"}]
    rows = owed.mod.paid_without_entry(CLAIM, "HR-EXP-1")
    assert [(r.request, r.parent, r.allocated_amount, r.request_status) for r in rows] == [("B2C-2", "D-2", 2000, "Paid")]
    assert asked == [(CLAIM, "HR-EXP-1", "SKT2", True)]  # a locking read, as every other read here
    for allocated in (5000, 0):  # allocated whole; or booked by an entry that does not carry the receipt
        _booked(owed, monkeypatch, {"SKT2": allocated})
        assert owed.mod.paid_without_entry(EA, "HR-EAD-1") == []


def test_a_booked_payout_of_an_invoice_or_one_open_payouts_counts_is_not_read_again(owed, monkeypatch):  # noqa: F811
    asked = _booked(owed, monkeypatch, {"SKT2": 3000})
    owed.paid = [{"request": "B2C-2", "sent_from": "D-2", "parent": "D-2", "amount": 5000, "transaction_id": "SKT2"}]
    assert owed.mod.paid_without_entry("Purchase Invoice", "PI-1") == []  # its entry takes the whole payout
    owed.paid[0]["listed"] = 1  # not marked booked yet: open_payouts counts the whole amount, never both
    assert owed.mod.paid_without_entry(CLAIM, "HR-EXP-1") == []
    assert asked == []


def test_a_partly_booked_payout_refuses_a_retry_as_awaiting_accounting(owed, monkeypatch):  # noqa: F811
    _booked(owed, monkeypatch, {"SKT2": 3000})
    owed.paid = [{"request": "B2C-2", "sent_from": "D-2", "parent": "D-2", "amount": 5000, "transaction_id": "SKT2"}]
    request = owed.failed(doctype=CLAIM, reference="HR-EXP-1")
    with pytest.raises(fake_frappe.ValidationError, match="awaiting accounting: D-2"):
        request.retry_failed_payment()
    assert owed.sends == []


# ---------------------------------------------------------------- K2: a line settled outside M-Pesa
def _manager(env):  # noqa: F811
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.frappe.session.user = "manager@example.com"


def test_a_manager_settles_a_draft_line_and_its_money_is_no_longer_held(env):  # noqa: F811
    lines = [payee("LN-1", 1), payee("LN-2", 2, PAYBILL, amount=1500)]
    claim(env, lines)
    assert [p.line for p in env.cp.unpaid_lines("HR-EXP-1")] == ["LN-1", "LN-2"]
    with pytest.raises(env.frappe.PermissionError):  # a preparer does not
        env.cp.settle_line("HR-EXP-1", "LN-2", "Bank ACC-PAY-1")
    _manager(env)
    with raises(env, "Say how the payee was paid"):
        env.cp.settle_line("HR-EXP-1", "LN-2", "  ")
    with raises(env, "LN-9 is not a payee row of HR-EXP-1"):
        env.cp.settle_line("HR-EXP-1", "LN-9", "Bank ACC-PAY-1")
    assert env.cp.settle_line("HR-EXP-1", "LN-2", "Bank ACC-PAY-1") == {"line": "LN-2", "status": "Cancelled"}
    row = stored(env, "LN-2")
    assert (row["status"], row["error_message"]) == (
        "Cancelled", "Settled outside M-Pesa by manager@example.com: Bank ACC-PAY-1")
    assert env.frappe.comments[-1][:2] == (CLAIM, "HR-EXP-1")
    assert "Payee row #2 (Nairobi County, 647647 / PARK-77, KES 1,500) settled outside M-Pesa by manager@example.com: " \
           "Bank ACC-PAY-1" in env.frappe.comments[-1][2]
    assert "b2c-pay:Expense Claim:HR-EXP-1" in env.locks  # under the claim's lock
    # its 1,500 is the employee's again; the other line still holds its 2,000
    assert [p.line for p in env.cp.unpaid_lines("HR-EXP-1")] == ["LN-1"]
    env.cp.sync_lines("HR-EXP-1")
    assert (stored(env, "LN-2")["status"], stored(env, "LN-2")["error_message"]) == ("Cancelled", row["error_message"])


@pytest.mark.parametrize("other, status", [
    ({"docstatus": 0, "status": "Not Initiated"}, "Queued"),  # a draft waits for the line: refused too
    ({"docstatus": 1, "status": "Initiated"}, "In Flight"),
    ({"docstatus": 2, "status": "Unknown"}, "In Flight"),  # cancelled, but it may have paid
    ({"docstatus": 1, "status": "Paid"}, "Paid"),
])
def test_a_line_on_its_way_paid_or_in_doubt_is_not_settled(env, other, status):  # noqa: F811
    line = payee()
    claim(env, [line])
    request(env, line, "MB2B-1", **other)
    _manager(env)
    with raises(env, f"Payee row #1 of HR-EXP-1 \\(Shell Kilimani\\) is {status}"):
        env.cp.settle_line("HR-EXP-1", "LN-1", "Bank ACC-PAY-1")
    assert stored(env)["status"] == status


def test_a_line_of_a_claim_not_submitted_is_not_settled(env):  # noqa: F811
    claim(env, [payee()], docstatus=2)
    _manager(env)
    with raises(env, "HR-EXP-1 is not submitted"):
        env.cp.settle_line("HR-EXP-1", "LN-1", "Bank ACC-PAY-1")


def test_a_settled_failed_line_is_never_paid_by_mpesa_again(env):  # noqa: F811
    lines = [payee("LN-1", 1), payee("LN-2", 2, payee_type="Phone (B2C)", amount=1000)]
    claim(env, lines)
    request(env, lines[0], "MB2B-1", docstatus=1, status="Failed", error_message="no float")
    env.frappe.db.table(REQ)["B2C-1"] = {"name": "B2C-1", "docstatus": 1, "status": "Failed", "amount": 1000,
                                         "b2c_payment_disbursement": "MPESA-B2C-1",
                                         "b2c_payment_disbursement_reference": "MPESA-B2C-1-R1"}
    disbursement(env, [(lines[1], {"b2c_disbursement_request": "B2C-1", "payment_status": "Failed"})],
                 name="MPESA-B2C-1", docstatus=1)
    env.cp.sync_lines("HR-EXP-1")
    assert [stored(env, n)["status"] for n in ("LN-1", "LN-2")] == ["Failed", "Failed"]
    _manager(env)
    env.cp.settle_line("HR-EXP-1", "LN-1", "Paid by bank, ACC-PAY-7")
    env.cp.settle_line("HR-EXP-1", "LN-2", "Paid cash, voucher 12")
    # its failed payout no longer decides it: it stays settled, with its note, and holds nothing
    env.cp.sync_lines("HR-EXP-1")
    assert [stored(env, n)["status"] for n in ("LN-1", "LN-2")] == ["Cancelled", "Cancelled"]
    assert stored(env, "LN-1")["error_message"].endswith("Paid by bank, ACC-PAY-7")
    assert env.cp.unpaid_lines("HR-EXP-1") == []
    env.frappe.user_roles = {"Accounts User"}
    with raises(env, "No payee line has failed"):
        env.cp.retry_payees("HR-EXP-1")
    with raises(env, "Nothing to pay"):
        env.cp.pay_payees("HR-EXP-1")
    # a copy of its failed request, saved or sent, and the failed B2C payout's own retry: refused
    copy = _dict(request(env, lines[0], "MB2B-2"))
    with raises(env, "Payee row #1 of HR-EXP-1 \\(Shell Kilimani\\) is Cancelled: it is not paid by M-Pesa"):
        env.cp.check_b2b(copy)
    with raises(env, "is Cancelled: it is not paid by M-Pesa"):
        env.cp.check_b2b(_dict(copy, docstatus=1))
    assert stored(env, "LN-1")["status"] == "Cancelled"  # the send never marked it In Flight
    with raises(env, "Payee row #2 of HR-EXP-1 \\(Jane Wanjiku\\) is Cancelled"):
        env.cp.check_retry("B2C-1")
    parent, refs = disbursement(env, [(lines[1], {})], name="MPESA-B2C-2")
    with raises(env, "is Cancelled: it is not paid by M-Pesa"):
        env.cp.check_b2c_row(parent, refs[0])


def test_the_line_values_keep_a_settled_line_until_a_payout_of_it_is_live(env):  # noqa: F811
    cp = env.cp
    settled = _dict(status="Cancelled", error_message="Settled outside M-Pesa by m: bank")

    def p(state):
        return _dict(state=state, doctype=B2B, name="MB2B-1", creation="2026-10-27", receipt="R1",
                     receiver_name="X", paid_on="2026-10-27", error="no float")

    assert (cp.line_values(settled, [], 1)["status"], cp.line_values(settled, [], 1)["error_message"]) == (
        "Cancelled", settled.error_message)
    assert cp.line_values(settled, [p("Failed")], 1)["error_message"] == settled.error_message
    # a late Result on a payout that was on its way: the money left, the line says so
    assert cp.line_values(settled, [p("Paid")], 1)["status"] == "Paid"
    assert cp.line_values(_dict(status="Failed"), [p("Failed")], 1)["status"] == "Failed"


# ---------------------------------------------------------------- K13: the claim's cancel and its lines
def test_a_claim_is_not_cancelled_while_a_payee_lines_cancelled_payout_may_have_paid(env):  # noqa: F811
    ed = importlib.import_module("mpsa_suite.money_out.employee_documents")
    lines = [payee("LN-1", 1), payee("LN-2", 2, PAYBILL, amount=1500)]
    doc = claim(env, lines)
    request(env, lines[0], "MB2B-1", docstatus=2, status="Not Initiated")  # never sent: lets it go
    request(env, lines[1], "MB2B-2", docstatus=1, status="Failed")
    ed.before_cancel(doc)
    # a manager cancelled the Unknown request: its line is still In Flight (claim_payees.b2b_state)
    request(env, lines[1], "MB2B-3", docstatus=2, status="Unknown")
    with raises(env, "HR-EXP-1 cannot be cancelled while M-Pesa payouts of it are paid or on their way: MB2B-3"):
        ed.before_cancel(doc)
    assert env.cp.lines_paid_or_on_their_way("HR-EXP-1") == ["MB2B-3"]
    # M-Pesa's answer since (a status query or a late Result) says it failed: the cancel goes ahead
    env.frappe.db.table(B2B)["MB2B-3"]["status"] = "Failed"
    ed.before_cancel(doc)
    assert env.cp.lines_paid_or_on_their_way("HR-EXP-2") == []  # no payee lines: nothing more read


# ---------------------------------------------------------------- K14: Cancel lists no M-Pesa payout
SETUP = r"""
load(path.join(process.env.MPSA_JS, "{script}"));
const frm = mk_frm("{doctype}", {{ docstatus: 1 }});
frm.ignore_doctypes_on_cancel_all = ["GL Entry", "Mpesa B2B Payment Request"];
handlers["{doctype}"].setup(frm);
handlers["{doctype}"].setup(frm);
const out = {{ ignored: frm.ignore_doctypes_on_cancel_all }};
"""


@pytest.mark.parametrize("doctype, script", [(CLAIM, "expense_claim.js"), (EA, "employee_advance.js")])
def test_cancel_never_offers_to_cancel_the_documents_mpesa_payouts(doctype, script):
    out = run_js(SETUP.format(doctype=doctype, script=script), {})
    # what HRMS (or another app) set is kept; each payout DocType once
    assert out["ignored"] == ["GL Entry", "Mpesa B2B Payment Request", "B2C Payment Disbursement",
                              "B2C Disbursement Request"]


# ---------------------------------------------------------------- K2: the claim form's button
SETTLE = """
load(%s);
const frm = mk_frm("Expense Claim", { docstatus: 1, mpesa_payees: [
    { name: "LN-1", idx: 1, payee_name: "Shell", amount: 2000 }, { name: "LN-2", idx: 2, payee_name: "County", amount: 1500 }] });
frm.reload_doc = () => calls.push(["reload"]);
handlers["Expense Claim"].refresh(frm);
await tick(); await tick();
const shown = frm.buttons.filter((b) => b.includes("Settle"));
if (frm.actions["Settle Payee Outside M-Pesa"]) {
    frm.actions["Settle Payee Outside M-Pesa"]();
    last_prompt.cb({ line: "LN-2", reason: "Bank ACC-PAY-1" });
    await tick(); await tick();
}
return { shown, options: last_prompt ? last_prompt.fields[0].options.map((o) => o.label) : null,
         sent: calls.filter((c) => c[0] === "xcall" && c[1].endsWith("settle_line")).map((c) => c[2]),
         reloaded: calls.some((c) => c[0] === "reload") };
"""
STATE = {"enabled": False, "changed": False, "totals": {}, "suggestions": [], "payable_problem": None,
         "lines": [{"name": "LN-1", "idx": 1, "status": "Queued"}, {"name": "LN-2", "idx": 2, "status": "Failed"}]}


@pytest.mark.parametrize("mpsa, lines, shown", [
    ({"can_approve": True}, STATE["lines"], True),  # also while payees are switched off
    ({"can_prepare": True, "can_pay": True}, STATE["lines"], False),  # only a named M-Pesa Manager settles
    ({"can_approve": True}, [dict(STATE["lines"][1], status="Paid")], False),  # nothing a manager may settle
])
def test_a_manager_settles_a_payee_from_the_claim_form(tmp_path, mpsa, lines, shown):
    script = SETTLE % repr(str(JS / "expense_claim_payees.js"))
    reply = {"mpsa_suite.money_out.claim_payees.claim_state": dict(STATE, lines=lines)}
    out = run_desk(tmp_path, [], script, mpsa=mpsa, replies=reply)
    assert bool(out["shown"]) is shown
    if shown:
        assert out["shown"] == ["M-Pesa > Settle Payee Outside M-Pesa"]
        assert out["options"] == ["#2 County 1500 (Failed)"]  # Draft and Failed lines only
        assert out["sent"] == [{"claim": "DOC-1", "line": "LN-2", "reason": "Bank ACC-PAY-1"}]
        assert out["reloaded"]

