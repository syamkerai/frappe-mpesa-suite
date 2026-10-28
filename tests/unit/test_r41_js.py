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
// Frappe's own get_indicator answers from the DocType's states first (HRMS 15 lists "Unpaid"), before
// listview_settings.get_indicator is asked: stood in for here
frappe.get_indicator = (doc, doctype) => (doc.status ? [doc.status, "yellow", "status,=," + doc.status] : null);
load(%s);
load(%s);  // loaded again (another list): the suite's answer is set once
return { fields: frappe.listview_settings[%s].add_fields,
         shown: %s.map((doc) => frappe.get_indicator(doc, %s)[0]) };
"""


@pytest.mark.parametrize("script, doctype, docs, shown", [
    ("expense_claim_list.js", "Expense Claim",
     [{"docstatus": 1, "status": "Unpaid", "total_amount_reimbursed": 20, "grand_total": 90},
      {"docstatus": 1, "status": "Unpaid", "total_amount_reimbursed": 0, "grand_total": 90},
      {"docstatus": 1, "status": "Paid", "total_amount_reimbursed": 90, "grand_total": 90}],
     ["Partly Paid", "Unpaid", "Paid"]),
    ("employee_advance_list.js", "Employee Advance",
     [{"docstatus": 1, "status": "Unpaid", "paid_amount": 3000, "advance_amount": 8000},
      {"docstatus": 1, "status": "Partially Paid", "paid_amount": 3000, "advance_amount": 8000}],  # HRMS 16's own
     ["Partly Paid", "Partially Paid"]),
])
def test_a_document_paid_in_part_shows_partly_paid_on_the_list(tmp_path, script, doctype, docs, shown):
    """Review R9: through frappe.get_indicator itself, which answers from the DocType's states first."""
    path = json.dumps(str(JS / script))
    out = run_desk(tmp_path, [], LIST % (path, path, json.dumps(doctype), json.dumps(docs), json.dumps(doctype)))
    assert out["shown"] == shown
    assert set(out["fields"]) >= ({"total_amount_reimbursed", "grand_total"} if doctype == "Expense Claim"
                                  else {"paid_amount", "advance_amount"})


DEFER = """
// Frappe's own order: Not Saved first, then the workflow state where it is shown, then the states
frappe.workflow = { workflows: %s, avoid_status_override: {}, get_state_fieldname: (dt) => (%s ? "workflow_state" : null) };
frappe.get_indicator = (doc, doctype, show_workflow_state) => {
    if (doc.__unsaved) return ["Not Saved", "orange"];
    const flow = frappe.workflow.workflows[doctype || doc.doctype];
    if (doc.workflow_state && (!(flow ? flow.override_status : true) || show_workflow_state)) return [doc.workflow_state, "gray"];
    return [doc.status, "yellow"];
};
load(%s);
const part = { doctype: %s, docstatus: 1, status: "Unpaid", total_amount_reimbursed: 20, grand_total: 90,
               paid_amount: 20, advance_amount: 90, workflow_state: "Approved" };
return [frappe.get_indicator(part)[0], frappe.get_indicator(Object.assign({ __unsaved: 1 }, part))[0],
        frappe.get_indicator(part, part.doctype, true)[0]];
"""


@pytest.mark.parametrize("script, doctype", [("expense_claim_list.js", "Expense Claim"),
                                             ("employee_advance_list.js", "Employee Advance")])
@pytest.mark.parametrize("workflow, shown", [
    (None, ["Partly Paid", "Not Saved", "Partly Paid"]),  # no workflow: no state to show
    ({"override_status": 1}, ["Partly Paid", "Not Saved", "Approved"]),  # the Workflow State column shows it
    ({"override_status": 0}, ["Approved", "Not Saved", "Approved"]),  # the workflow's state is the status
])
def test_partly_paid_never_hides_not_saved_or_the_workflow_state(tmp_path, script, doctype, workflow, shown):
    """Fix check S12: an unsaved edit after submit still shows Not Saved; a workflow's state still shows where
    Frappe shows it (show_workflow_state, or a Workflow that does not override the status)."""
    flows = {doctype: workflow} if workflow else {}
    out = run_desk(tmp_path, [], DEFER % (json.dumps(flows), json.dumps(bool(workflow)), json.dumps(str(JS / script)),
                                          json.dumps(doctype)))
    assert out == shown


PROOF = """
load(%s);
frappe.model.can_email = (doctype, frm) => !(frm.doc.no_email);
frappe.model.with_doctype = (dt, cb) => cb();
frappe.model.with_doc = (dt, name) => Promise.resolve({ doctype: dt, name });
frappe.views = { CommunicationComposer: class { constructor(o) {
    calls.push(["composer", o.doc.doctype, o.doc.name, o.recipients, o.subject, o.attach_document_print]);
    this.dialog = { fields_dict: { select_print_format: {} }, set_value: (f, v) => calls.push(["set", f, v]) }; } } };
const out = {};
for (const [doctype, doc] of %s) {
    const frm = mk_frm(doctype, doc);
    handlers[doctype].refresh(frm);
    out[`${doctype} ${doc.status} ${doc.docstatus}${doc.no_email ? " no email" : ""}${doc.salary_slip || doc.transaction_to_pay_against ? " salary" : ""}`] = frm.buttons;
    if (frm.actions["Send Payment Proof"]) {
        frm.actions["Send Payment Proof"](); await tick(); await tick();
        if (last_prompt) { last_prompt.cb({ row: last_prompt.fields[0].options[0].value }); last_prompt = null; await tick(); await tick(); }
    }
}
load(%s);  // a second payout form loading the script sets no second handler
const again = mk_frm("Mpesa B2B Payment Request", { docstatus: 1, status: "Paid" });
handlers["Mpesa B2B Payment Request"].refresh(again);
out.again = again.buttons;
out.calls = calls.filter((c) => c[0] !== "xcall" || c[1].endsWith("proof_recipients"));
return out;
"""


def test_send_payment_proof_goes_to_one_party_only(tmp_path):
    """Review R1, R2, R12: a paid payout's proof to its one party; a batch of several parties picks one row and
    sends that row's payout only; never a salary payout; only for who may send email on it."""
    script = str(JS / "payment_proof.js")
    cases = [["Mpesa B2B Payment Request", {"docstatus": 1, "status": "Paid"}],
             ["Mpesa B2B Payment Request", {"docstatus": 1, "status": "Initiated"}],
             ["Mpesa B2B Payment Request", {"docstatus": 1, "status": "Paid", "no_email": 1}],
             ["Mpesa B2B Payment Request", {"docstatus": 1, "status": "Paid", "salary_slip": "SAL-1"}],
             ["B2C Payment Disbursement", {"docstatus": 1, "status": "Paid", "transaction_to_pay_against": "Salary Slip"}],
             ["B2C Disbursement Request", {"docstatus": 0, "status": "Paid"}]]
    one = {"doctype": "Mpesa B2B Payment Request", "name": "DOC-1", "recipients": ["pay@acme.example.com"],
           "subject": "Proof of payment: X", "print_format": "M-Pesa 80mm X"}
    out = run_desk(tmp_path, [], PROOF % (json.dumps(script), json.dumps(cases), json.dumps(script)),
                   replies={"mpsa_suite.money_out.payment_print.proof_recipients": one})
    assert out["Mpesa B2B Payment Request Paid 1"] == ["Send Payment Proof"]
    for key in ("Mpesa B2B Payment Request Initiated 1", "Mpesa B2B Payment Request Paid 1 no email",
                "Mpesa B2B Payment Request Paid 1 salary", "B2C Payment Disbursement Paid 1 salary",
                "B2C Disbursement Request Paid 0"):
        assert out[key] == [], key
    assert out["again"] == ["Send Payment Proof"]
    assert ["composer", "Mpesa B2B Payment Request", "DOC-1", "pay@acme.example.com", "Proof of payment: X",
            True] in out["calls"]
    assert ["set", "select_print_format", "M-Pesa 80mm X"] in out["calls"]


BATCH = """
load(%s);
const allow_request = %s;
frappe.model.can_email = (dt, frm) => (frm ? true : dt === "B2C Disbursement Request" && allow_request);
frappe.views = { CommunicationComposer: class { constructor(o) {
    calls.push(["composer", o.doc.doctype, o.doc.name, o.recipients, o.frm ? o.frm.doc.name : null]);
    this.dialog = { fields_dict: { select_print_format: {} }, set_value: (f, v) => calls.push(["set", f, v]) }; } } };
let answer = 0;
frappe.xcall = (method, args) => { calls.push(["xcall", method.split(".").pop(), args]);
    return Promise.resolve(answer++ ? %s : %s); };
const frm = mk_frm("B2C Payment Disbursement", { docstatus: 1, status: "Paid", party_type: "Employee",
    transaction_to_pay_against: "Expense Claim", references: [
        { party: "EMP-1", reference_doctype: "Expense Claim" }, { party: "EMP-2", reference_doctype: "Expense Claim" }] });
handlers["B2C Payment Disbursement"].refresh(frm);
if (!frm.actions["Send Payment Proof"]) return { buttons: frm.buttons };
frm.actions["Send Payment Proof"](); await tick(); await tick();
const options = last_prompt.fields[0].options;
last_prompt.cb({ row: "D-1-R2" }); await tick(); await tick();
const composed_on_batch = calls.some((c) => c[0] === "composer");
// another request's form first: not the one asked for, nothing opens there
const other = mk_frm("B2C Disbursement Request", { name: "REQ-1", docstatus: 1, status: "Paid", party_type: "Employee",
                                                    reference_doctype: "Expense Claim" });
handlers["B2C Disbursement Request"].refresh(other);
const req = mk_frm("B2C Disbursement Request", { name: "REQ-2", docstatus: 1, status: "Paid", party_type: "Employee",
                                                  reference_doctype: "Expense Claim" });
handlers["B2C Disbursement Request"].refresh(req);
handlers["B2C Disbursement Request"].refresh(req);  // refreshed again: the composer opened once
return { buttons: frm.buttons, options, composed_on_batch, calls };
"""


ROWS = {"rows": [{"row": "D-1-R1", "label": "#1 EMP-1 2547****0331 KES 1,500.00"},
                 {"row": "D-1-R2", "label": "#2 EMP-2 2547****0332 KES 900.00"}]}
ROW = {"doctype": "B2C Disbursement Request", "name": "REQ-2", "recipients": ["two@example.com"],
       "subject": "Proof of payment: B2C Disbursement Request REQ-2", "print_format": "M-Pesa 80mm B2C Disbursement Request"}


def batch(tmp_path, allow_request=True):
    return run_desk(tmp_path, [], BATCH % (json.dumps(str(JS / "payment_proof.js")), json.dumps(allow_request),
                                           json.dumps(ROW), json.dumps(ROWS)))


def test_a_batch_of_several_parties_proves_one_chosen_rows_payout_to_its_party(tmp_path):
    """Fix check S1: the row's proof is composed on that payout's own form, where Frappe's composer lists its
    print formats and attaches the print (with no form it attaches nothing)."""
    out = batch(tmp_path)
    assert [o["value"] for o in out["options"]] == ["D-1-R1", "D-1-R2"]
    assert ["xcall", "proof_recipients", {"doctype": "B2C Payment Disbursement", "name": "DOC-1", "row": "D-1-R2"}] in         out["calls"]
    assert not out["composed_on_batch"]
    assert ["route", "Form", "B2C Disbursement Request", "REQ-2"] in out["calls"]
    composed = [c for c in out["calls"] if c[0] == "composer"]
    assert composed == [["composer", "B2C Disbursement Request", "REQ-2", "two@example.com", "REQ-2"]]  # with its form
    assert ["set", "select_print_format", "M-Pesa 80mm B2C Disbursement Request"] in out["calls"]


def test_a_batch_of_several_parties_is_not_offered_to_who_may_not_email_its_payouts(tmp_path):
    """Fix check S6/S8: each row is proved on its B2C Disbursement Request; an Expense Approver, who may email the
    batch but has no right on the requests, is not offered a button that cannot finish."""
    assert batch(tmp_path, allow_request=False)["buttons"] == []


SALARY = """
load(%s);
frappe.model.can_email = () => true;
return %s.map(([doctype, doc]) => { const frm = mk_frm(doctype, doc); handlers[doctype].refresh(frm); return frm.buttons; });
"""


def test_an_employee_paid_for_no_claim_or_advance_is_a_salary_and_gets_no_proof(tmp_path):
    """Fix check S7: as salary_access reads a salary (the server refuses it too)."""
    paid = {"docstatus": 1, "status": "Paid"}
    cases = [["Mpesa B2B Payment Request", dict(paid, employee="EMP-1")],
             ["Mpesa B2B Payment Request", dict(paid, employee="EMP-1", payroll_entry="PAY-1")],
             ["Mpesa B2B Payment Request", dict(paid, employee="EMP-1", expense_claim="HR-EXP-1")],
             ["Mpesa B2B Payment Request", dict(paid, supplier="ACME")],
             ["B2C Disbursement Request", dict(paid, party_type="Employee")],
             ["B2C Disbursement Request", dict(paid, party_type="Employee", reference_doctype="Employee Advance")],
             ["B2C Payment Disbursement", dict(paid, party_type="Employee")],
             ["B2C Payment Disbursement", dict(paid, party_type="Employee", transaction_to_pay_against="Expense Claim",
                                               references=[{"party": "EMP-1", "reference_doctype": "Expense Claim"}])]]
    out = run_desk(tmp_path, [], SALARY % (json.dumps(str(JS / "payment_proof.js")), json.dumps(cases)))
    assert [bool(b) for b in out] == [False, False, True, True, False, True, False, True]
