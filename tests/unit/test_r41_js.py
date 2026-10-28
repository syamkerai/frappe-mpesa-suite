"""Round 4.1's desk changes, run in node against test_r4_buttons_js's stand-in for the desk: the
claim form's payee summary (the reimbursed-total warning for preparers and payers too, a name not
verified in orange and one that does not match in red, staff or self payments and Clear Staff
Flag), who sends what is prepared as maker-checker is on or off, and Partly Paid on the claim and
advance lists of HRMS 15."""

import json

import pytest
from test_employee_pay_js import BOTH, PAY, STATUS
from test_employee_pay_js import run as run_js
from test_r4_buttons_js import JS, NODE
from test_r4_buttons_js import run as run_desk

pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")
API = "mpsa_suite.money_out.claim_payees"

FORM = """
load(%s);
let html = "";
const frm = mk_frm("Expense Claim", { docstatus: 1, mpesa_payees: [
    { name: "LN-1", idx: 1, payee_name: "Shell", destination: "835811", amount: 2000, payee_type: "Till (Buy Goods)" },
    { name: "LN-2", idx: 2, payee_name: "Jane", destination: "0711 000 333", amount: 500, payee_type: "Phone (B2C)" }] });
frm.fields_dict.mpesa_payees_summary = { $wrapper: { html: (h) => { html = h; }, find: () => ({ on: () => {} }) } };
frm.is_dirty = () => false;
handlers["Expense Claim"].refresh(frm);
await tick(); await tick();
let intro = null;
if (frm.actions["Pay Merchants and Payees"]) { frm.actions["Pay Merchants and Payees"](); intro = last_dialog.opts.fields[0].options; }
if (frm.actions["Clear Staff Flag"]) {
    frm.actions["Clear Staff Flag"]();
    last_prompt.cb({ line: last_prompt.fields[0].default || "LN-1", reason: "asked the claimant" });
    await tick(); await tick();
}
return { html, intro, buttons: frm.buttons,
         options: last_prompt ? last_prompt.fields[0].options.map((o) => o.value) : null,
         cleared: calls.filter((c) => c[0] === "xcall" && c[1].endsWith("clear_staff_flag")).map((c) => c[2]) };
"""
STATE = {"enabled": True, "changed": False, "totals": {}, "suggestions": [], "payable_problem": None, "blocked": [],
         "reimbursed": None, "lines": [{"name": "LN-1", "idx": 1, "status": "Draft"},
                                       {"name": "LN-2", "idx": 2, "status": "Draft"}]}


def form(tmp_path, state=None, **mpsa):
    reply = {f"{API}.claim_state": dict(STATE, **(state or {}))}
    return run_desk(tmp_path, [], FORM % json.dumps(str(JS / "expense_claim_payees.js")), mpsa=mpsa, replies=reply)


def test_a_reimbursed_gap_is_shown_to_preparers_and_payers_and_stops_paying(tmp_path):
    gap = {"reimbursed": {"stored": 70, "booked": 90}}
    out = form(tmp_path, gap, can_prepare=True)
    assert "but the entries booked against the claim add up to 90: ask an M-Pesa Manager to repair it" in out["html"]
    assert not any("Pay Merchants" in b for b in out["buttons"])  # the server refuses it too
    out = form(tmp_path, gap, can_pay=True)
    assert "ask an M-Pesa Manager to repair it" in out["html"]
    out = form(tmp_path, gap, can_approve=True)
    assert "press Repair Reimbursed Total" in out["html"] and "M-Pesa > Repair Reimbursed Total" in out["buttons"]
    assert "Total Amount Reimbursed" not in form(tmp_path, gap)["html"]  # neither prepares, pays nor repairs


def test_a_name_not_verified_is_orange_and_one_that_does_not_match_red(tmp_path):
    lines = [{"name": "LN-1", "idx": 1, "status": "Paid", "name_verdict": "Not Verified", "name_note": "Not verified: bank"},
             {"name": "LN-2", "idx": 2, "status": "Paid", "name_verdict": "Does Not Match", "name_note": "but JOHN"},
             {"name": "LN-3", "idx": 3, "status": "Paid", "name_matches": 0, "name_note": "older line"},
             {"name": "LN-4", "idx": 4, "status": "Paid", "name_verdict": "Matches", "name_note": "M-Pesa paid X"}]
    html = form(tmp_path, {"lines": lines})["html"]
    assert '<p style="color: var(--orange-600)">Row #1: Not verified: bank</p>' in html
    assert '<p style="color: var(--red-600)">Row #2: but JOHN</p>' in html
    assert '<p style="color: var(--red-600)">Row #3: older line</p>' in html
    assert "M-Pesa paid X" not in html


def test_a_staff_flag_is_shown_and_cleared_by_a_manager(tmp_path):
    lines = [{"name": "LN-1", "idx": 1, "status": "Paid", "staff_check": "Blocked", "staff_note": "Staff or self payment: X"},
             {"name": "LN-2", "idx": 2, "status": "Draft"}]
    blocked = [{"line": "LN-2", "idx": 2, "claim": "HR-EXP-9", "flagged_idx": 3}]
    out = form(tmp_path, {"lines": lines, "blocked": blocked}, can_prepare=True)
    assert "Row #1: Staff or self payment: X" in out["html"]
    assert "Row #2: blocked for claim payees, M-Pesa paid this destination under a staff member's name (HR-EXP-9, " \
           "payee row #3)." in out["html"]
    assert not any("Clear Staff Flag" in b for b in out["buttons"])  # an M-Pesa Manager's
    out = form(tmp_path, {"lines": lines, "blocked": blocked}, can_approve=True)
    assert out["options"] == ["LN-1", "LN-2"]
    assert out["cleared"] == [{"claim": "DOC-1", "line": "LN-1", "reason": "asked the claimant"}]


@pytest.mark.parametrize("maker_checker, text", [
    (True, "Nothing is sent now: an M-Pesa Payer who did not prepare them sends each with Send via M-Pesa."),
    (False, "Nothing is sent now: an M-Pesa Payer sends each with Send via M-Pesa."),
])
def test_who_sends_the_payees_payouts_follows_maker_checker(tmp_path, maker_checker, text):
    out = form(tmp_path, can_prepare=True, maker_checker=maker_checker)
    assert out["intro"].endswith(f" {text}</p>")


@pytest.mark.parametrize("maker_checker, text", [
    (True, "Nothing is sent now: an M-Pesa Payer who did not prepare it sends it with Send via M-Pesa."),
    (False, "Nothing is sent now: an M-Pesa Payer sends it with Send via M-Pesa."),
])
def test_who_sends_the_employees_payout_follows_maker_checker(maker_checker, text):
    replies = {"document_status": STATUS, "prepare_b2c": {"doctype": "B2C Payment Disbursement", "name": "D-1"}}
    out = run_js(PAY.format(label="Pay employee (phone)", values="{}"),
                 {"mpsa": {"can_prepare": True, "maker_checker": maker_checker}, "perms": BOTH, "replies": replies})
    assert text in out["fields"][0][2]


LIST = """
load(%s);
const s = frappe.listview_settings[%s];
return { fields: s.add_fields, shown: %s.map((doc) => (s.get_indicator(doc) || [null])[0]) };
"""


@pytest.mark.parametrize("script, doctype, docs, shown", [
    ("expense_claim_list.js", "Expense Claim",
     [{"docstatus": 1, "status": "Unpaid", "total_amount_reimbursed": 20, "grand_total": 90},
      {"docstatus": 1, "status": "Unpaid", "total_amount_reimbursed": 0, "grand_total": 90},
      {"docstatus": 1, "status": "Paid", "total_amount_reimbursed": 90, "grand_total": 90}],
     ["Partly Paid", None, None]),
    ("employee_advance_list.js", "Employee Advance",
     [{"docstatus": 1, "status": "Unpaid", "paid_amount": 3000, "advance_amount": 8000},
      {"docstatus": 1, "status": "Partially Paid", "paid_amount": 3000, "advance_amount": 8000}],  # HRMS 16's own
     ["Partly Paid", None]),
])
def test_a_document_paid_in_part_shows_partly_paid_on_the_list(tmp_path, script, doctype, docs, shown):
    out = run_desk(tmp_path, [], LIST % (json.dumps(str(JS / script)), json.dumps(doctype), json.dumps(docs)))
    assert out["shown"] == shown
    assert set(out["fields"]) >= ({"total_amount_reimbursed", "grand_total"} if doctype == "Expense Claim"
                                  else {"paid_amount", "advance_amount"})


PROOF = """
load(%s);
frappe.views = { CommunicationComposer: class { constructor(o) {
    calls.push(["composer", o.recipients, o.subject, o.attach_document_print]);
    this.dialog = { fields_dict: { select_print_format: {} }, set_value: (f, v) => calls.push(["set", f, v]) }; } } };
const out = {};
for (const [doctype, status, docstatus] of %s) {
    const frm = mk_frm(doctype, { docstatus, status });
    handlers[doctype].refresh(frm);
    out[`${doctype} ${status} ${docstatus}`] = frm.buttons;
    if (frm.actions["Send Payment Proof"]) { frm.actions["Send Payment Proof"](); await tick(); await tick(); }
}
load(%s);  // a second payout form loading the script sets no second handler
const again = mk_frm("B2C Payment Disbursement", { docstatus: 1, status: "Paid" });
handlers["B2C Payment Disbursement"].refresh(again);
out.again = again.buttons;
out.calls = calls.filter((c) => c[0] !== "xcall" || c[1].endsWith("proof_recipients"));
return out;
"""


def test_send_payment_proof_opens_the_email_composer_on_a_paid_payout_only(tmp_path):
    script = str(JS / "payment_proof.js")
    cases = [["Mpesa B2B Payment Request", "Paid", 1], ["Mpesa B2B Payment Request", "Initiated", 1],
             ["B2C Payment Disbursement", "Partially Paid", 1], ["B2C Disbursement Request", "Paid", 0]]
    reply = {"mpsa_suite.money_out.payment_print.proof_recipients": {
        "recipients": ["pay@acme.example.com"], "subject": "Proof of payment: X", "print_format": "M-Pesa 80mm X"}}
    out = run_desk(tmp_path, [], PROOF % (json.dumps(script), json.dumps(cases), json.dumps(script)), replies=reply)
    assert out["Mpesa B2B Payment Request Paid 1"] == ["Send Payment Proof"]
    assert out["Mpesa B2B Payment Request Initiated 1"] == []
    assert out["B2C Payment Disbursement Partially Paid 1"] == ["Send Payment Proof"]
    assert out["B2C Disbursement Request Paid 0"] == []  # a draft
    assert out["again"] == ["Send Payment Proof"]
    assert ["composer", "pay@acme.example.com", "Proof of payment: X", True] in out["calls"]
    assert ["set", "select_print_format", "M-Pesa 80mm X"] in out["calls"]
