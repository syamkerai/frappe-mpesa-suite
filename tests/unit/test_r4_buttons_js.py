"""Round 4 (WP6): the desk buttons, list indicators and JS fixes, run in node against a small
stand-in for the desk (as test_fix2_stk2 does for the Express Request form). A button shows only to
who the server accepts (frappe.boot.mpsa flags, can_create / can_read); the server checks again."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "mpsa_suite" / "public" / "js"
DT = ROOT / "mpsa_suite" / "mpsa_suite" / "doctype"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

PRELUDE = r"""
const fs = require("fs");
const setup = JSON.parse(process.env.MPSA_SETUP || "{}");
global.__ = (s, args) => (args ? String(s).replace(/\{(\d+)\}/g, (_, i) => String(args[i])) : s);
global.flt = (v) => parseFloat(v) || 0;
global.cint = (v) => parseInt(v) || 0;
global.format_number = (v) => String(v);
global.format_currency = (v) => String(v);
global.$ = () => ({ find: () => ({ off: () => ({ on: () => {} }) }) });
global.window = global;
const handlers = {};
const calls = [];
const realtime = { on: [], off: [] };
let last_dialog = null, last_prompt = null;
const perms = Object.assign({ create: {}, read: {} }, setup.perms || {});
global.frappe = {
	boot: { app_data: [], mpsa: setup.mpsa || {} },
	provide: (ns) => { let o = global; for (const p of ns.split(".")) { o[p] = o[p] || {}; o = o[p]; } },
	ui: {
		form: { on: (dt, h) => { handlers[dt] = Object.assign(handlers[dt] || {}, h); } },
		Dialog: class { constructor(opts) { this.opts = opts; last_dialog = this; this.$wrapper = { find: () => ({ css: () => {} }), attr: () => {}, on: () => {} }; } show() {} hide() {} },
	},
	listview_settings: {},
	model: {
		can_create: (dt) => Boolean(perms.create[dt]),
		can_read: (dt) => Boolean(perms.read[dt]),
		with_doc: (dt, name) => Promise.resolve({ doctype: dt, name, lines: [] }),
		with_doctype: (dt, cb) => cb(),
		get_new_doc: (dt) => ({ doctype: dt, name: "new-schedule-1", lines: [] }),
		add_child: (doc, dt, field) => { const row = { doctype: dt }; doc[field].push(row); return row; },
	},
	db: {
		get_list: (dt, args) => { calls.push(["get_list", dt, args]); return Promise.resolve((setup.rows || {})[dt] || []); },
		get_value: (dt, name, fields, cb) => { const v = (setup.values || {})[dt] || {}; if (cb) cb(v); return Promise.resolve({ message: v }); },
	},
	xcall: (method, args) => { calls.push(["xcall", method, args]); return Promise.resolve((setup.replies || {})[method]); },
	call: (opts) => { calls.push(["call", opts.method, opts.args || null]); },
	realtime: { on: (ev, fn) => realtime.on.push([ev, fn]), off: (ev, fn) => realtime.off.push([ev, fn]) },
	utils: { escape_html: (s) => String(s), play_sound: () => {} },
	datetime: { get_today: () => "2026-10-27", add_days: (d) => d, str_to_user: (d) => d, str_to_obj: (d) => new Date(d) },
	msgprint: (m) => calls.push(["msgprint", typeof m === "string" ? m : m.message]),
	show_alert: () => {},
	prompt: (fields, cb) => { last_prompt = { fields, cb }; },
	set_route: (...route) => { calls.push(["route", ...route]); return Promise.resolve(); },
	new_doc: (dt, values) => calls.push(["new_doc", dt, values]),
};
global.mk_frm = (doctype, doc) => {
	const buttons = [];
	const frm = {
		doctype, doc: Object.assign({ doctype, name: "DOC-1" }, doc), buttons,
		is_new: () => Boolean(doc.__islocal),
		add_custom_button: (label, fn, group) => { buttons.push(group ? `${group} > ${label}` : label); frm.actions[label] = fn; },
		actions: {},
		dashboard: { set_headline: (t) => buttons.push(`headline: ${t}`), add_indicator: (t) => buttons.push(`indicator: ${t}`), add_section: () => {} },
		set_value: () => {}, add_web_link: () => {}, reload_doc: () => calls.push(["reload"]), refresh_field: () => {},
		set_query: () => {}, toggle_display: () => {}, set_intro: () => {},
		fields_dict: { payments: { wrapper: {}, grid: null } },
	};
	return frm;
};
global.load = (path) => (0, eval)(fs.readFileSync(path, "utf8"));
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def run(tmp_path, files, scenario, **setup):
    """Run ``scenario`` (JS, an async body that returns the result) after loading ``files``."""
    script = PRELUDE + "".join(f"load({json.dumps(str(f))});\n" for f in files) + (
        "(async () => {\n" + scenario + "\n})().then((r) => console.log(JSON.stringify(r)));\n")
    path = tmp_path / "harness.js"
    path.write_text(script, encoding="utf-8")
    env = dict(os.environ, MPSA_SETUP=json.dumps(setup))
    done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=60, env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


COLLECT = {"can_collect": True}
PR_CREATE = {"create": {"Payment Request": 1}}


# ---------------------------------------------------------------- list indicators
INDICATOR = """
const s = frappe.listview_settings[%s];
return %s.map((doc) => (s.get_indicator(doc) || [null])[0]);
"""


def test_payout_and_reversal_list_indicators(tmp_path):
    cases = {
        (DT / "b2c_payment_disbursement" / "b2c_payment_disbursement_list.js", "B2C Payment Disbursement"): [
            ({"docstatus": 1, "status": "Paid"}, "Paid"), ({"docstatus": 1, "status": "Partially Paid"}, "Partially Paid"),
            ({"docstatus": 1, "status": "Failed"}, "Failed"), ({"docstatus": 0, "status": ""}, None)],
        (DT / "b2c_disbursement_request" / "b2c_disbursement_request_list.js", "B2C Disbursement Request"): [
            ({"docstatus": 1, "status": "Status Unknown"}, "Status Unknown"),
            ({"docstatus": 1, "status": "Initiated"}, "Initiated"),
            ({"docstatus": 1, "status": "Paid", "accounting_booked": 0}, "Paid, not booked"),
            ({"docstatus": 1, "status": "Paid", "accounting_booked": 1}, "Paid"), ({"docstatus": 0}, None)],
        (DT / "mpesa_reversal_request" / "mpesa_reversal_request_list.js", "Mpesa Reversal Request"): [
            ({"docstatus": 1, "status": "Reversed"}, "Reversed"), ({"docstatus": 1, "status": "Unknown"}, "Unknown")],
        (DT / "mpesa_c2b_payment_register" / "mpesa_c2b_payment_register_list.js", "Mpesa C2B Payment Register"): [
            ({"docstatus": 0}, "Unmatched"), ({"docstatus": 0, "customer": "C"}, "Customer Matched"),
            ({"docstatus": 0, "customer": "C", "matched_reference_name": "SINV-1"}, "Matched"),
            ({"docstatus": 1, "payment_entry": "PE-1"}, "Booked"), ({"docstatus": 1, "sales_invoice": "SINV-1"}, "On Invoice"),
            ({"docstatus": 2}, "Cancelled")],
    }
    for (path, doctype), rows in cases.items():
        got = run(tmp_path, [path], INDICATOR % (json.dumps(doctype), json.dumps([d for d, _ in rows])))
        assert got == [want for _, want in rows], doctype


def test_the_register_list_keeps_its_status_query_button(tmp_path):
    path = DT / "mpesa_c2b_payment_register" / "mpesa_c2b_payment_register_list.js"
    got = run(tmp_path, [path], """
const s = frappe.listview_settings["Mpesa C2B Payment Register"];
const added = [];
s.onload({ page: { add_inner_button: (label) => added.push(label) }, refresh: () => {} });
return [added, s.has_indicator_for_draft, s.add_fields.includes("matched_reference_name")];
""")
    assert got == [["Check Transaction Status"], True, True]


# ---------------------------------------------------------------- Mpesa Express Request
EXPRESS = DT / "mpesa_express_request" / "mpesa_express_request.js"


def test_express_request_listens_once_however_often_it_refreshes(tmp_path):
    got = run(tmp_path, [EXPRESS], """
const frm = mk_frm("Mpesa Express Request", { docstatus: 1, status: "In Progress", currency: "KES", amount: 10 });
handlers["Mpesa Express Request"].refresh(frm);
handlers["Mpesa Express Request"].refresh(frm);
handlers["Mpesa Express Request"].refresh(frm);
const ons = realtime.on.filter(([ev]) => ev === "refresh_form");
const offs = realtime.off.filter(([ev]) => ev === "refresh_form");
return [ons.length, offs.length, offs[0][1] === ons[0][1], offs[1][1] === ons[1][1]];
""", mpsa=COLLECT)
    assert got == [3, 2, True, True]  # each new listener replaces the last


def test_express_request_actions_show_to_who_the_server_accepts(tmp_path):
    scenario = """
const frm = mk_frm("Mpesa Express Request", %s);
handlers["Mpesa Express Request"].refresh(frm);
return frm.buttons;
"""
    paid = json.dumps({"docstatus": 1, "status": "Completed", "is_reconciled": 0, "currency": "KES", "amount": 10})
    open_ = json.dumps({"docstatus": 1, "status": "In Progress", "currency": "KES", "amount": 10})
    assert run(tmp_path, [EXPRESS], scenario % open_, mpsa=COLLECT) == [
        "Mpesa Actions > Check Transaction Status", "Mpesa Actions > Initiate STK Push"]
    assert run(tmp_path, [EXPRESS], scenario % open_, mpsa={}) == []
    assert run(tmp_path, [EXPRESS], scenario % paid, mpsa=COLLECT) == []
    assert run(tmp_path, [EXPRESS], scenario % paid, mpsa={"can_approve": True}) == ["Mpesa Actions > Reconcile Payment"]


def test_express_request_says_the_prompt_went_out(tmp_path):
    got = run(tmp_path, [EXPRESS], """
const frm = mk_frm("Mpesa Express Request", { docstatus: 1, status: "Failed", phone_number: "254712345678", currency: "KES", amount: 10 });
let opts;
frappe.call = (o) => { opts = o; };
handlers["Mpesa Express Request"].refresh(frm);
frm.actions["Initiate STK Push"]();
opts.callback({});  // initiate_request returns nothing
return calls.filter((c) => c[0] !== "get_list");
""", mpsa=COLLECT)
    assert ["msgprint", "STK Push sent to 254712345678. The request updates when the customer answers."] in got
    assert ["reload"] in got


# ---------------------------------------------------------------- Sales Invoice / Sales Order: Request Payment
SI_FILES = [JS / "utils.js", JS / "sales_invoice.js"]
SO_FILES = [JS / "utils.js", JS / "sales_order.js"]
BUTTONS = """
const frm = mk_frm(%s, %s);
handlers[%s].refresh(frm);
await tick();
return [frm.buttons, calls.filter((c) => c[0] === "call").map((c) => c[1])];
"""


def _buttons(tmp_path, files, doctype, doc, **setup):
    return run(tmp_path, files, BUTTONS % (json.dumps(doctype), json.dumps(doc), json.dumps(doctype)), **setup)


SUBMITTED_SI = {"docstatus": 1, "is_pos": 0, "is_return": 0, "outstanding_amount": 500, "currency": "KES", "payments": []}


def test_a_submitted_invoice_offers_request_payment_to_collectors_who_create_payment_requests(tmp_path):
    shown = ["M-Pesa > Request Payment"]
    assert _buttons(tmp_path, SI_FILES, "Sales Invoice", SUBMITTED_SI, mpsa=COLLECT, perms=PR_CREATE)[0] == shown
    assert _buttons(tmp_path, SI_FILES, "Sales Invoice", SUBMITTED_SI, mpsa={}, perms=PR_CREATE)[0] == []
    assert _buttons(tmp_path, SI_FILES, "Sales Invoice", SUBMITTED_SI, mpsa=COLLECT)[0] == []
    for change in ({"is_pos": 1}, {"is_return": 1}, {"outstanding_amount": 0}, {"currency": "USD"}, {"docstatus": 0}):
        doc = dict(SUBMITTED_SI, **change)
        assert _buttons(tmp_path, SI_FILES, "Sales Invoice", doc, mpsa=COLLECT, perms=PR_CREATE)[0] == [], change


def test_draft_pos_invoice_stk_and_c2b_buttons_are_for_collectors(tmp_path):
    draft_pos = {"docstatus": 0, "is_pos": 1, "outstanding_amount": 500, "currency": "KES", "payments": []}
    eligibility = "mpsa_suite.mpsa_suite.api.sales_invoice.validate_stk_push_eligibility"
    assert eligibility in _buttons(tmp_path, SI_FILES, "Sales Invoice", draft_pos, mpsa=COLLECT)[1]
    assert _buttons(tmp_path, SI_FILES, "Sales Invoice", draft_pos, mpsa={})[1] == []


def test_the_payments_row_stk_button_is_for_collectors(tmp_path):
    scenario = """
const row = { name: "ROW-1", mode_of_payment: "Mpesa-TILL", type: "Phone", phone_number: "0712345678", amount: 50 };
const frm = mk_frm("Sales Invoice", { docstatus: 0, is_pos: 1, outstanding_amount: 0, payments: [row] });
handlers["Sales Invoice"].refresh(frm);
return Boolean(row.initiate_stk_push);
"""
    assert run(tmp_path, SI_FILES, scenario, mpsa=COLLECT) is True
    assert run(tmp_path, SI_FILES, scenario, mpsa={}) is False


def test_a_sales_order_offers_request_payment_while_it_owes(tmp_path):
    so = {"docstatus": 1, "status": "To Deliver and Bill", "per_billed": 0, "currency": "KES", "grand_total": 1000,
          "rounded_total": 1000, "advance_paid": 200}
    assert _buttons(tmp_path, SO_FILES, "Sales Order", so, mpsa=COLLECT, perms=PR_CREATE)[0] == ["M-Pesa > Request Payment"]
    for change in ({"advance_paid": 1000}, {"status": "Closed"}, {"per_billed": 100}, {"currency": "USD"}, {"docstatus": 0}):
        assert _buttons(tmp_path, SO_FILES, "Sales Order", dict(so, **change), mpsa=COLLECT, perms=PR_CREATE)[0] == [], change
    assert _buttons(tmp_path, SO_FILES, "Sales Order", so, mpsa={}, perms=PR_CREATE)[0] == []


DIALOG = """
const frm = mk_frm("Sales Order", { docstatus: 1, company: "TC" });
mpsa_collect.show_dialog(frm);
await tick();
if (!last_dialog) return calls;
const fields = Object.fromEntries(last_dialog.opts.fields.map((f) => [f.fieldname, f]));
last_dialog.opts.primary_action(%s);
return [fields.amount.default, fields.phone_number.default, fields.payment_gateway_account.hidden,
        calls.filter((c) => c[0] !== "xcall")];
"""
OPTIONS = {"owed": 4000, "held_by": [], "currency": "KES", "customer_name": "Wanjiku", "phone": "0712345678",
           "gateways": [{"name": "Mpesa-TILL-KES", "label": "TILL (174379)", "limit": 3000}]}


def test_the_request_dialog_asks_at_most_what_is_owed_and_the_limit(tmp_path):
    values = {"phone_number": "0712345678", "amount": 3000, "payment_gateway_account": "Mpesa-TILL-KES"}
    got = run(tmp_path, [JS / "utils.js"], DIALOG % json.dumps(values),
              replies={"mpsa_suite.money_in.collect.collect_options": OPTIONS})
    assert got[:3] == [3000, "0712345678", 1]
    assert got[3] == [["call", "mpsa_suite.money_in.collect.request_payment",
                       {"doctype": "Sales Order", "name": "DOC-1", "phone_number": "0712345678", "amount": 3000,
                        "payment_gateway_account": "Mpesa-TILL-KES"}]]
    over = dict(values, amount=3001)
    got = run(tmp_path, [JS / "utils.js"], DIALOG % json.dumps(over),
              replies={"mpsa_suite.money_in.collect.collect_options": OPTIONS})
    assert got[3] == [["msgprint", "Ask for more than 0 and at most KES 3000."]]


def test_the_request_dialog_explains_instead_of_opening(tmp_path):
    for options, message in (
        (dict(OPTIONS, problem="SO-1 is Closed: payment is not requested on it."), "SO-1 is Closed"),
        (dict(OPTIONS, gateways=[]), "No M-Pesa shortcode"),
        (dict(OPTIONS, owed=0, held_by=["PR-1"]), "Payment Requests PR-1 already ask"),
    ):
        got = run(tmp_path, [JS / "utils.js"], DIALOG % "{}",
                  replies={"mpsa_suite.money_in.collect.collect_options": options})
        assert any(c[0] == "msgprint" and message in c[1] for c in got), message


# ---------------------------------------------------------------- utils.js: Request Payment in the C2B dialog
REQUEST = """
const frm = mk_frm("Sales Invoice", { customer: "CUST-1", company: "TC", currency: "KES" });
mpesa_show_request_dialog(frm, 500, %s, null);
const fields = last_dialog.opts.fields.map((f) => f.fieldname);
last_dialog.opts.primary_action({ phone_number: "0712345678", mode_of_payment: "Mpesa-PICKED" });
return [fields, calls.filter((c) => c[0] === "call")];
"""


def test_request_payment_in_the_c2b_dialog_works_on_the_invoice_desk(tmp_path):
    fields, sent = run(tmp_path, [JS / "utils.js"], REQUEST % "null", values={"Customer": {"mobile_no": "0712"}})
    assert "mode_of_payment" in fields  # the desk passes no mop_config: the Mode of Payment is asked for
    assert sent[0][2]["mode_of_payment"] == "Mpesa-PICKED"
    _, sent = run(tmp_path, [JS / "utils.js"], REQUEST % json.dumps({"mop": "Mpesa-POS"}),
                  values={"Customer": {"mobile_no": "0712"}})
    assert sent[0][2]["mode_of_payment"] == "Mpesa-POS"


# ---------------------------------------------------------------- Payment Request
PR_FILES = [JS / "utils.js", JS / "payment_request.js"]
PR_DOC = {"docstatus": 1, "status": "Requested", "payment_gateway": "Mpesa-TILL", "mode_of_payment": "Mpesa-TILL",
          "company": "TC", "phone_number": "0712",
          "outstanding_amount": 800, "grand_total": 1000, "currency": "KES"}
EXPRESS_CREATE = {"create": {"Mpesa Express Request": 1}}


def test_payment_request_prompt_buttons(tmp_path):
    def buttons(doc=PR_DOC, last=None, **setup):
        rows = {"Mpesa Express Request": [last]} if last else {}
        setup.setdefault("mpsa", COLLECT)
        setup.setdefault("perms", EXPRESS_CREATE)
        return _buttons(tmp_path, PR_FILES, "Payment Request", doc, rows=rows, **setup)[0]

    sent = {"name": "EXP-1", "status": "In Progress", "checkout_request_id": "ws_CO_1"}
    assert buttons() == ["M-Pesa > Resend M-Pesa Prompt"]
    assert buttons(last=sent) == ["M-Pesa > Check M-Pesa Status", "M-Pesa > Resend M-Pesa Prompt"]
    assert buttons(last=dict(sent, verification_status="Pending")) == ["M-Pesa > Check M-Pesa Status"]
    assert buttons(last=dict(sent, status="Completed", transaction_id="SJK1")) == ["M-Pesa > Resend M-Pesa Prompt"]
    assert buttons(doc=dict(PR_DOC, status="Paid"), last=dict(sent, status="Completed")) == []
    assert buttons(mpsa={}) == []
    assert buttons(doc=dict(PR_DOC, payment_gateway="Stripe")) == []
    assert buttons(perms={}) == []


RESEND = """
const frm = mk_frm("Payment Request", %s);
handlers["Payment Request"].refresh(frm);
await tick();
frm.actions["Resend M-Pesa Prompt"]();
last_prompt.cb({ phone_number: "0722 000 111" });
return calls.filter((c) => c[0] === "call");
"""


def test_resend_sends_the_last_prompt_again_or_a_new_one(tmp_path):
    unpaid = {"name": "EXP-1", "status": "Failed", "checkout_request_id": "ws_CO_1"}
    got = run(tmp_path, PR_FILES, RESEND % json.dumps(PR_DOC), mpsa=COLLECT, perms=EXPRESS_CREATE,
              rows={"Mpesa Express Request": [unpaid]})
    assert got == [["call", "mpsa_suite.www.mpesa.stkpush.retry_stkpush", {"name": "EXP-1", "phone_number": "254722000111"}]]
    paid = dict(unpaid, status="Completed", transaction_id="SJK1")
    got = run(tmp_path, PR_FILES, RESEND % json.dumps(PR_DOC), mpsa=COLLECT, perms=EXPRESS_CREATE,
              rows={"Mpesa Express Request": [paid]})
    assert got == [["call", "mpsa_suite.mpsa_suite.doctype.mpesa_express_request.mpesa_express_request.create_new_request",
                    {"phone_number": "254722000111", "payment_gateway": "Mpesa-TILL", "reference_type": "Payment Request",
                     "reference_id": "DOC-1", "amount": 800, "currency": "KES"}]]


# ---------------------------------------------------------------- Purchase Invoice / Order, Supplier
SUBMITTED_PI = {"docstatus": 1, "outstanding_amount": 900, "currency": "KES", "on_hold": 0, "is_return": 0}
LEFT = "mpsa_suite.money_out.b2b.amount_left"


def test_purchase_invoice_schedule_button_and_left_to_pay_headline(tmp_path):
    files = [JS / "utils.js", JS / "purchase_invoice.js"]
    reply = {LEFT: {"summary": {"in_progress": 300, "planned": 100, "left": 500}}}
    perms = {"create": {"M-Pesa Payment Schedule": 1}, "read": {"Mpesa B2B Payment Request": 1}}
    got = _buttons(tmp_path, files, "Purchase Invoice", SUBMITTED_PI, perms=perms, replies=reply)[0]
    assert "M-Pesa > Add to M-Pesa Payment Schedule" in got
    assert ("headline: M-Pesa: KES 300 on its way, KES 100 planned on a payment schedule. Left to pay: KES 500."
            in got)
    got = _buttons(tmp_path, files, "Purchase Invoice", SUBMITTED_PI, perms={}, replies=reply)[0]
    assert got == []
    quiet = {LEFT: {"summary": {"in_progress": 0, "planned": 0, "left": 900}}}
    got = _buttons(tmp_path, files, "Purchase Invoice", SUBMITTED_PI, perms=perms, replies=quiet)[0]
    assert got == ["M-Pesa > Add to M-Pesa Payment Schedule"]


def test_add_to_schedule_opens_the_schedule_with_the_line(tmp_path):
    files = [JS / "utils.js", JS / "purchase_order.js"]
    reply = {LEFT: {"supplier": "SUP-1", "left": 700, "mpesa_settings": ["B2B-1"], "problems": []}}
    got = run(tmp_path, files, """
const frm = mk_frm("Purchase Order", { docstatus: 1, per_billed: 0, status: "To Receive and Bill", currency: "KES", company: "TC" });
handlers["Purchase Order"].refresh(frm);
await tick();
frm.actions["Add to M-Pesa Payment Schedule"]();
await tick();
let schedule;
frappe.model.get_new_doc = (dt) => (schedule = { doctype: dt, name: "new-1", lines: [] });
last_dialog.opts.primary_action({ planned_date: "2026-10-28", amount: 700 });
await tick();
return [schedule, calls.filter((c) => c[0] === "route")];
""", perms={"create": {"M-Pesa Payment Schedule": 1}}, replies=reply)
    schedule, route = got
    assert schedule["company"] == "TC" and schedule["b2b_settings"] == "B2B-1" and schedule["__unsaved"] == 1
    assert schedule["lines"] == [{"doctype": "M-Pesa Payment Schedule Line", "planned_date": "2026-10-28",
                                  "party_type": "Supplier", "party": "SUP-1", "reference_doctype": "Purchase Order",
                                  "reference_name": "DOC-1", "amount": 700, "remarks": "DOC-1"}]
    assert route == [["route", "Form", "M-Pesa Payment Schedule", "new-1"]]


def test_supplier_add_account_needs_create(tmp_path):
    files = [JS / "supplier.js"]
    read_only = {"read": {"Supplier M-Pesa Account": 1}}
    assert "M-Pesa > Add M-Pesa Account" not in _buttons(tmp_path, files, "Supplier", {}, perms=read_only)[0]
    can_add = {"read": {"Supplier M-Pesa Account": 1}, "create": {"Supplier M-Pesa Account": 1}}
    assert "M-Pesa > Add M-Pesa Account" in _buttons(tmp_path, files, "Supplier", {}, perms=can_add)[0]


def test_customer_shows_what_it_paid_by_mpesa(tmp_path):
    rows = {"Mpesa C2B Payment Register": [{"transamount": 100}, {"transamount": 250.5}]}
    got = _buttons(tmp_path, [JS / "customer.js"], "Customer", {}, perms={"read": {"Mpesa C2B Payment Register": 1}},
                   rows=rows)[0]
    assert got == ["indicator: M-Pesa received in 90 days: KES 350.5 (2 payments)"]
    assert _buttons(tmp_path, [JS / "customer.js"], "Customer", {}, rows=rows)[0] == []


# ---------------------------------------------------------------- payout buttons for payers only
@pytest.mark.parametrize("path, doctype, doc, label, flags", [
    (JS / "c2b_reversal.js", "Mpesa C2B Payment Register", {"docstatus": 1, "transid": "SJK1"}, "Actions > Reverse Payment",
     ({"can_pay": True}, {"can_approve": True})),
    (DT / "b2c_disbursement_request" / "b2c_disbursement_request.js", "B2C Disbursement Request",
     {"docstatus": 1, "status": "Failed"}, "Actions > Retry Payment", ({"can_pay": True},)),
    (JS / "b2c_status_check.js", "B2C Disbursement Request", {"docstatus": 1, "status": "Initiated", "mpesa_settings": "S"},
     "Actions > Check Status with M-Pesa", ({"can_pay": True}, {"can_approve": True})),
])
def test_payout_actions_show_to_payers_and_admins_only(tmp_path, path, doctype, doc, label, flags):
    for flag in flags:
        assert label in _buttons(tmp_path, [path], doctype, doc, mpsa=flag)[0], flag
    assert label not in _buttons(tmp_path, [path], doctype, doc, mpsa={"can_collect": True, "can_prepare": True})[0]


# ---------------------------------------------------------------- fixed lines
def test_b2c_disbursement_fixes():
    source = (DT / "b2c_payment_disbursement" / "b2c_payment_disbursement.js").read_text(encoding="utf-8")
    # the Less Than Amount filter reaches the server (the key was written twice)
    assert source.count("outstanding_amt_greater_than: filters.outstanding_amt_greater_than") == 1
    assert "outstanding_amt_less_than: filters.outstanding_amt_less_than" in source
    # paid_to for a DocType without its own account: the Company's default payable account
    fallback = source.split('frappe.db.get_value("Company", company, "default_payable_account")', 1)[1][:120]
    assert "response.message.default_payable_account" in fallback and "accountField" not in fallback
    # Retry Failed Transactions is a payout
    assert "has_failed_references && frm.doc.docstatus === 1 && frappe.boot.mpsa?.can_pay" in source


def test_sales_invoice_fixes():
    source = (JS / "sales_invoice.js").read_text(encoding="utf-8")
    assert "insert_payment_entry" not in source and '"Cash"' not in source
    fetch = source.split("open_mpesa_payment_modal: function", 1)[1].split("callback:", 1)[0]
    assert "company: frm.doc.company" in fetch and '["customer", "is", "not set"]' in fetch
