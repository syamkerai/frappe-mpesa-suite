"""The Expense Claim / Employee Advance desk scripts (round 4, WP3: public/js/employee_pay.js,
expense_claim.js, employee_advance.js and their list scripts), run in node against a small
stand-in for the desk. The buttons show only to who the server accepts (frappe.boot.mpsa,
can_create) for a payable document that still owes; they prepare drafts only; the list extends
HRMS's settings and prepares one batch from the ticked documents. The B2B request's Make Entry
offers a claim's or advance's payout its own document."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "mpsa_suite" / "public" / "js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

PRELUDE = r"""
const fs = require("fs");
const path = require("path");
const setup = JSON.parse(process.env.MPSA_SETUP || "{}");
global.__ = (s, args) => (args ? String(s).replace(/\{(\d+)\}/g, (_, i) => String(args[i])) : s);
global.flt = (v) => parseFloat(v) || 0;
global.format_currency = (v, currency) => `${currency} ${v}`;
global.window = global;
const handlers = {};
const calls = [];
let last_dialog = null;
const perms = Object.assign({ create: {} }, setup.perms || {});
global.frappe = {
	boot: { mpsa: setup.mpsa || {} },
	provide: (ns) => { let o = global; for (const p of ns.split(".")) { o[p] = o[p] || {}; o = o[p]; } return o; },
	require: (item, cb) => { load(path.join(process.env.MPSA_JS, path.basename(item))); if (cb) cb(); },
	ui: {
		form: { on: (dt, h) => { handlers[dt] = Object.assign(handlers[dt] || {}, h); } },
		Dialog: class { constructor(opts) { this.opts = opts; last_dialog = this; } show() { calls.push(["dialog", this.opts.title]); } hide() {} },
	},
	listview_settings: setup.listview_settings_preset ? { [setup.listview_settings_preset]: { onload() { calls.push(["hrms onload"]); } } } : {},
	model: { can_create: (dt) => Boolean(perms.create[dt]) },
	xcall: (method, args) => { calls.push(["xcall", method.split(".").pop(), args]); return Promise.resolve((setup.replies || {})[method.split(".").pop()]); },
	utils: { escape_html: (s) => String(s), get_form_link: (dt, name) => `/app/${dt}/${name}` },
	msgprint: (m) => calls.push(["msgprint", typeof m === "string" ? m : m.message]),
	show_alert: () => {},
	set_route: (...route) => calls.push(["route", ...route]),
};
global.mk_frm = (doctype, doc) => {
	const buttons = [];
	return {
		doctype, doc: Object.assign({ doctype, name: "DOC-1", employee: "EMP-1" }, doc), buttons, actions: {},
		add_custom_button(label, fn, group) { buttons.push(group ? `${group} > ${label}` : label); this.actions[label] = fn; },
		dashboard: { set_headline: (t, colour) => buttons.push(`headline (${colour}): ${t}`) },
	};
};
global.load = (file) => (0, eval)(fs.readFileSync(file, "utf8"));
const tick = () => new Promise((r) => setTimeout(r, 0));
"""

STATUS = {"owed": 5000, "left": 4000, "amount": 4000, "paid": 0, "in_flight": 1000, "unbooked": 0,
          "drafts_planned": 0, "parts": [], "problem": None, "can_prepare": True, "phone": "2547****0111",
          "phone_approved": True, "approved_phones_only": True, "b2c_settings": ["Astral 600999"],
          "b2b_settings": ["Astral 600999"], "drafts": [], "b2b_default": "MEA-1",
          "b2b_accounts": [{"name": "MEA-1", "label": "Bank 247247, account ****6789 (default)", "is_default": 1},
                           {"name": "MEA-3", "label": "Till (Buy Goods) 835811", "is_default": 0}]}
BOTH = {"create": {"B2C Payment Disbursement": True, "Mpesa B2B Payment Request": True}}


def run(body: str, setup: dict) -> dict:
    script = PRELUDE + "\n(async () => {\n" + body + "\nconsole.log(JSON.stringify(out));\n})();"
    env = dict(os.environ, MPSA_SETUP=json.dumps(setup), MPSA_JS=str(JS))
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", env=env,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


FORM = r"""
load(path.join(process.env.MPSA_JS, "{script}"));
const frm = mk_frm("{doctype}", {{ docstatus: {docstatus} }});
handlers["{doctype}"].refresh(frm);
await tick(); await tick();
const out = {{ buttons: frm.buttons, calls }};
"""


def form(doctype="Expense Claim", script="expense_claim.js", docstatus=1, **setup):
    setup.setdefault("replies", {"document_status": STATUS})
    return run(FORM.format(script=script, doctype=doctype, docstatus=docstatus), setup)


def test_a_payable_claim_offers_both_routes_and_the_headline():
    out = form(mpsa={"can_prepare": True}, perms=BOTH)
    assert out["buttons"] == ["headline (blue): M-Pesa: paid KES 0 · in flight KES 1000 · left KES 4000",
                              "M-Pesa > Pay employee (phone)", "M-Pesa > Pay employee (bank / paybill / till)"]
    assert out["calls"][0] == ["xcall", "document_status", {"doctype": "Expense Claim", "name": "DOC-1"}]


def test_an_advance_has_the_same_buttons():
    out = form("Employee Advance", "employee_advance.js", mpsa={"can_prepare": True}, perms=BOTH)
    assert "M-Pesa > Pay employee (phone)" in out["buttons"]


@pytest.mark.parametrize("mpsa, perms, docstatus, buttons", [
    ({"can_prepare": True}, BOTH, 0, []),  # a draft: nothing asked
    ({}, BOTH, 1, []),  # neither prepares nor pays: nothing asked
    ({"can_pay": True}, BOTH, 1, ["headline"]),  # a payer sees the headline, prepares nothing
    ({"can_prepare": True}, {"create": {"Mpesa B2B Payment Request": True}}, 1,
     ["headline", "M-Pesa > Pay employee (bank / paybill / till)"]),
])
def test_buttons_show_only_to_who_the_server_accepts(mpsa, perms, docstatus, buttons):
    out = form(mpsa=mpsa, perms=perms, docstatus=docstatus)
    assert [b.split(" (")[0] if b.startswith("headline") else b for b in out["buttons"]] == buttons


def test_a_document_that_cannot_be_paid_offers_no_button_and_says_why():
    reply = dict(STATUS, problem="HR-EXP-1 is not approved (Approval Status: Draft).")
    out = form(mpsa={"can_prepare": True}, perms=BOTH, replies={"document_status": reply})
    assert out["buttons"] == ["headline (orange): M-Pesa: paid KES 0 · in flight KES 1000 · left KES 4000"
                              "<br>HR-EXP-1 is not approved (Approval Status: Draft)."]
    paid = dict(STATUS, left=0, amount=0, paid=5000, in_flight=0)
    out = form(mpsa={"can_prepare": True}, perms=BOTH, replies={"document_status": paid})
    assert out["buttons"] == ["headline (green): M-Pesa: paid KES 5000 · in flight KES 0 · left KES 0"]


PAY = r"""
load(path.join(process.env.MPSA_JS, "expense_claim.js"));
const frm = mk_frm("Expense Claim", {{ docstatus: 1 }});
handlers["Expense Claim"].refresh(frm);
await tick(); await tick();
frm.actions["{label}"]();
const fields = last_dialog ? last_dialog.opts.fields.map((f) => [f.fieldname, f.default || null, f.options || null]) : [];
if (last_dialog) {{ last_dialog.opts.primary_action({values}); }}
await tick(); await tick();
const out = {{ fields, calls }};
"""


def test_pay_employee_by_phone_prepares_a_draft_and_opens_it():
    replies = {"document_status": STATUS, "prepare_b2c": {"doctype": "B2C Payment Disbursement", "name": "D-1"}}
    out = run(PAY.format(label="Pay employee (phone)", values="{}"),
              {"mpsa": {"can_prepare": True}, "perms": BOTH, "replies": replies})
    assert [f[0] for f in out["fields"]] == ["intro"]  # one B2C shortcode: not asked
    assert "the approved phone 2547****0111" in out["fields"][0][2]
    assert ["xcall", "prepare_b2c", {"doctype": "Expense Claim", "name": "DOC-1", "mpesa_settings": "Astral 600999"}] in \
        out["calls"]
    assert out["calls"][-1] == ["route", "Form", "B2C Payment Disbursement", "D-1"]


def test_pay_employee_by_phone_without_one_explains_instead():
    replies = {"document_status": dict(STATUS, phone="", phone_approved=False)}
    out = run(PAY.format(label="Pay employee (phone)", values="{}"),
              {"mpsa": {"can_prepare": True}, "perms": BOTH, "replies": replies})
    assert out["fields"] == [] and "has no approved phone that can be paid now" in out["calls"][-1][1]


def test_pay_employee_to_an_account_picks_the_default_and_asks_the_shortcode_when_there_are_two():
    status = dict(STATUS, b2b_settings=["Astral 600999", "Astral 600888"])
    replies = {"document_status": status, "prepare_b2b": {"doctype": "Mpesa B2B Payment Request", "name": "MB2B-1"}}
    values = '{ employee_mpesa_account: "MEA-3", mpesa_settings: "Astral 600888" }'
    out = run(PAY.format(label="Pay employee (bank / paybill / till)", values=values),
              {"mpsa": {"can_prepare": True}, "perms": BOTH, "replies": replies})
    account = next(f for f in out["fields"] if f[0] == "employee_mpesa_account")
    assert account[1] == "MEA-1" and [o["label"] for o in account[2]] == [
        "Bank 247247, account ****6789 (default)", "Till (Buy Goods) 835811"]
    assert any(f[0] == "mpesa_settings" for f in out["fields"])
    assert ["xcall", "prepare_b2b", {"doctype": "Expense Claim", "name": "DOC-1", "employee_mpesa_account": "MEA-3",
                                     "mpesa_settings": "Astral 600888"}] in out["calls"]
    assert out["calls"][-1] == ["route", "Form", "Mpesa B2B Payment Request", "MB2B-1"]


LIST = r"""
load(path.join(process.env.MPSA_JS, "{script}"));
const settings = frappe.listview_settings["{doctype}"];
const items = [];
const listview = {{
	page: {{ add_action_item: (label, fn) => items.push([label, fn]) }},
	get_checked_items: () => ["HR-1", "HR-2"],
	refresh: () => calls.push(["refresh"]),
}};
settings.onload(listview);
if (items.length) {{
	items[0][1]();
	last_dialog.opts.primary_action({{ b2c_settings: "Astral 600999" }});
	await tick(); await tick();
}}
const out = {{ items: items.map((i) => i[0]), calls }};
"""


def test_pay_by_mpesa_on_the_list_extends_hrms_and_prepares_one_batch():
    replies = {"prepare_bulk": {"b2c": "D-9", "b2c_rows": 1, "b2b": ["MB2B-9"],
                                "skipped": [{"name": "HR-3", "reason": "HR-3 is not approved."}]}}
    out = run(LIST.format(script="expense_claim_list.js", doctype="Expense Claim"),
              {"mpsa": {"can_prepare": True}, "replies": replies, "listview_settings_preset": "Expense Claim"})
    assert out["items"] == ["Pay by M-Pesa"]
    assert out["calls"][0] == ["hrms onload"]  # HRMS's own onload still runs, first
    assert ["xcall", "prepare_bulk", {"doctype": "Expense Claim", "names": ["HR-1", "HR-2"],
                                      "b2c_settings": "Astral 600999"}] in out["calls"]
    message = next(c[1] for c in out["calls"] if c[0] == "msgprint")
    assert "/app/B2C Payment Disbursement/D-9" in message and "HR-3: HR-3 is not approved." in message


def test_the_list_action_is_for_preparers_only():
    out = run(LIST.format(script="employee_advance_list.js", doctype="Employee Advance"), {"mpsa": {"can_pay": True}})
    assert out["items"] == []


# ---------------------------------------------------------------- Make Entry on a B2B request
B2B_JS = ROOT / "mpsa_suite" / "mpsa_suite" / "doctype" / "mpesa_b2b_payment_request" / "mpesa_b2b_payment_request.js"
MAKE_ENTRY = r"""
global.format_number = (v) => String(v);
frappe.datetime = { str_to_user: (d) => d };
frappe.boot.versions = setup.hrms ? { hrms: "16.0.0" } : {};
(0, eval)(fs.readFileSync(process.env.MPSA_B2B_JS, "utf8") + "\nglobalThis.mpsa_b2b = mpsa_b2b;");
const frm = { doc: { name: "MB2B-1" }, reload_doc: () => calls.push(["reload"]) };
await mpsa_b2b.make_entry(frm);
const field = (name) => last_dialog.opts.fields.find((f) => f.fieldname === name);
const kind = field("kind");
const pick = { "Expense Claim": "expense_claim", "Employee Advance": "employee_advance" }[kind.default];
const picked = pick ? { [pick]: field(pick).default } : {};
last_dialog.opts.primary_action(Object.assign({ kind: kind.default, submit: 1 }, picked));
await tick();
const out = {
	kinds: kind.options.split("\n"), kind: kind.default,
	claim: [field("expense_claim").default || null, field("expense_claim").depends_on, field("expense_claim").get_query()],
	advance: [field("employee_advance").default || null, field("employee_advance").get_query()],
	supplier: field("supplier").depends_on, calls,
};
"""
FACTS = {"amount": 5000, "date": "2026-10-27", "receipt": "QEXP1", "paid_to": "Equity Bank", "gl": "M-Pesa - AIL",
         "charge_entry": None, "supplier": None, "purchase_invoice": None, "purchase_order": None, "employee": "EMP-1",
         "salary_slip": None, "expense_claim": None, "employee_advance": None, "company": "Astral",
         "cost_center": None}


def make_entry(facts: dict, hrms: bool = True) -> dict:
    return run(MAKE_ENTRY, {"hrms": hrms, "replies": {"entry_facts": dict(FACTS, **facts),
                                                      "make_entry": {"doctype": "Payment Entry", "name": "PE-1"}}})


def test_make_entry_books_a_claim_or_advance_payout_on_its_own_document(monkeypatch):
    # a claim's payout whose automatic booking failed: Make Entry offers the claim, not only a
    # Journal Entry that HRMS would never count against it
    monkeypatch.setenv("MPSA_B2B_JS", str(B2B_JS))
    out = make_entry({"expense_claim": "HR-EXP-1"})
    assert out["kinds"] == ["Salary Slip", "Expense Claim", "Employee Advance", "Journal Entry"]
    assert out["kind"] == "Expense Claim"
    assert out["claim"] == ["HR-EXP-1", "eval:doc.kind=='Expense Claim'",
                            {"filters": {"docstatus": 1, "employee": "EMP-1", "approval_status": "Approved"}}]
    assert "Expense Claim" not in out["supplier"] and "Purchase Invoice" in out["supplier"]
    assert ["xcall", "make_entry", {"name": "MB2B-1", "kind": "Expense Claim", "submit": 1,
                                    "expense_claim": "HR-EXP-1"}] in out["calls"]
    out = make_entry({"employee_advance": "HR-EAD-1"})
    assert out["kind"] == "Employee Advance"
    assert out["advance"] == ["HR-EAD-1", {"filters": {"docstatus": 1, "employee": "EMP-1"}}]
    assert make_entry({})["kind"] == "Journal Entry"  # an employee payment naming nothing
    assert make_entry({"expense_claim": "HR-EXP-1"}, hrms=False)["kinds"] == ["Journal Entry"]
