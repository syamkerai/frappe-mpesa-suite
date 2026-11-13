"""Round 4.5 review of "Know who you pay" (docs/BULK_PAYOUT_PLAN.md): the paste reader and the M-Pesa
Known Name list.

Covers, one section per finding:
1. the amount column is scored (a total wins; a rate, a per-day amount, an advance, days are never the
   amount, whatever their "(KES)"), both paste dialogs say which column each field came from and let
   the user pick another amount column (parse_payees, the bulk parse_rows; read_rows' ``columns``);
2. names are kept as plain text (no stored HTML) and the list view escapes them; an SMS's name only
   starts a record, unconfirmed: shown as from SMS, never a match, never locking a claim payee's
   name, and a Result's name replaces it;
3. a thousands comma in comma-separated text is never guessed (a problem row), but where the join is
   certain (one cell more than the column names, one place to join);
4. a row paid already is caught under column names in any other cell, and by M-Pesa code,
   transaction, reference and Paid columns;
5. Pay Selected (M-Pesa Payment Schedule) shows the names and needs the tick, enforced on the server;
6. one typed word against a longer M-Pesa name is "partial" (amber, acknowledged with the differences);
7. the bank flag is read from the current M-Pesa Bank Paybill list;
8. a lock wait never loses a name, and a Transaction Status answer keeps the B2C payout's name;
9. a phone paid to an Employee is hidden from who reads neither claims nor salaries;
10. pasted row numbers count blank lines.
"""

import datetime
import importlib
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import fake_frappe
import pytest
from test_claim_payees import CLAIM, REQ, claim
from test_claim_payees import env as claims_env
from test_money_out_flow import env as flow_env
from test_money_out_flow import payout as flow_payout
from test_money_out_flow import start as flow_start
from test_money_out_flow import status_result
from test_payroll_schedule import env as payroll_env
from test_r45_bulk_payout import CASUAL, PREPARER, SITE_A, as_user
from test_r45_bulk_payout import env as bulk_env
from test_schedule import env as schedule_env
from test_schedule import line_row, prepared, schedule_row

# the fixtures of the suites this one extends (payroll_env: the bulk sheet's own)
FIXTURES = (claims_env, flow_env, payroll_env, bulk_env, schedule_env)
_dict = fake_frappe._dict
ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "mpsa_suite"
KNOWN = "M-Pesa Known Name"
BANK_PAYBILL = "M-Pesa Bank Paybill"
AT = datetime.datetime(2026, 10, 29, 9, 0)
NODE = shutil.which("node")
node = pytest.mark.skipif(not NODE, reason="node is not installed")


def kn():
    return importlib.import_module("mpsa_suite.money_out.known_names")


def cpm():
    return importlib.import_module("mpsa_suite.money_out.claim_payees")


def store(env, key, name, times=1, source="B2C Result", payout="B2C-1", **values):
    kind, number = kn().split_key(key)
    env.frappe.db.table(KNOWN)[key] = {
        "name": key, "destination_key": key, "destination_type": kind, "number": number, "mpesa_name": name,
        "received": f"{number} - {name}", "first_seen": AT, "last_seen": AT, "times_paid": times,
        "last_payout_doctype": REQ, "last_payout": payout, "source": source, "salary_only": 0, "bank": 0, **values}
    return env.frappe.db.table(KNOWN)[key]


def known(env, key):
    return env.frappe.db.table(KNOWN).get(key)


def request(env, name, phone, received, minutes=0, **values):
    row = {"name": name, "docstatus": 1, "status": "Paid", "phone_number": phone, "receiver_public_name": received,
           "reference_doctype": CLAIM, "party_type": "Employee", "amount": 1000,
           "transaction_completed_datetime": AT + datetime.timedelta(minutes=minutes),
           "creation": AT + datetime.timedelta(minutes=minutes), **values}
    env.frappe.db.table(REQ)[name] = row
    return row


def checked(cp, table, **kw):
    rows, _left = cp.read_rows(table, **kw)
    return [(r["row"], r["payee_name"], r["amount"], r["errors"])
            for r in cp.check_pasted(rows, "EMP-1", [], cp.employee_destinations())]


# ---------------------------------------------------------------- 1. the amount column
@pytest.mark.parametrize("header, cells, amount, label", [
    (["NAME", "ID NO", "PHONE NO", "DAYS", "RATE (KSHS)", "GRAND TOTAL"],
     ["JANE", "23456789", "0711000333", "5", "300", "1500"], 1500, "GRAND TOTAL"),
    (["Name", "Phone", "Days", "Amount per Day", "Total Amount"], ["Jane", "0711000333", "5", "300", "1500"], 1500,
     "Total Amount"),
    (["Name", "Phone", "Days", "Rate", "Gross", "Advance Amount", "Net Pay"],
     ["Jane", "0711000333", "5", "600", "3000", "500", "2500"], 2500, "Net Pay"),
    (["Name", "Phone", "Days", "Rate (KES)", "Gross Pay"], ["Jane", "0711000333", "5", "300", "1500"], 1500,
     "Gross Pay"),
    (["Name", "Phone", "Days", "Rate (Kshs)", "Wages"], ["Jane", "0711000333", "5", "300", "1500"], 1500, "Wages"),
    (["Name", "Phone", "Days", "Pay Rate", "Pay"], ["Jane", "0711000333", "5", "300", "1500"], 1500, "Pay"),
    (["Name", "Phone", "Hours", "Rate/Hour (KES)", "OT Pay"], ["Jane", "0711000333", "4", "150", "600"], 600,
     "OT Pay"),
    (["Name", "Phone", "Days", "Daily Rate (Ksh)", "Total Earnings"], ["Jane", "0711000333", "5", "300", "1500"],
     1500, "Total Earnings"),
    (["Name", "Phone", "Days Worked", "Rate", "Total Pay (Kshs)"], ["Jane", "0711000333", "5", "300", "1500"], 1500,
     "Total Pay (Kshs)"),
    (["Name", "Phone", "Gross", "PAYE", "Amount Payable"], ["Jane", "0711000333", "1800", "300", "1500"], 1500,
     "Amount Payable"),
    (["Name", "Phone", "Basic Pay", "Balance", "Total"], ["Jane", "0711000333", "1000", "200", "1500"], 1500,
     "Total"),
    (["Name", "ID Number", "Tel", "Amount"], ["Jane", "23456789", "0711000333", "1500"], 1500, "Amount"),
])
def test_the_amount_is_the_strongest_total_never_a_rate_or_a_part(claims_env, header, cells, amount, label):
    cp = claims_env.cp
    read = cp.read_rows([header, cells])
    (row,) = cp.check_pasted(read[0], "EMP-1", [], cp.employee_destinations())
    assert (row["amount"], row["phone"], row["errors"]) == (amount, "254711000333", [])
    assert read.columns["fields"]["amount"] == label and read.columns["header_row"] == 1
    assert read.columns["fields"]["phone"] in ("PHONE NO", "Phone", "Tel")


def test_a_header_whose_only_money_column_is_a_rate_pays_nobody_until_the_amount_is_picked(claims_env):
    cp = claims_env.cp
    table = [["Name", "Phone", "Days", "Rate (KES)"], ["Jane", "0711000333", "5", "300"]]
    read = cp.read_rows(table)
    (row,) = read[0]
    assert row.amount_text == "" and row.problem.startswith("no column reads as the amount (a rate, days or an "
                                                            "advance is never taken: Days, Rate (KES))")
    assert read.columns["amount_column"] is None
    assert read.columns["amount_choices"] == [{"column": 2, "label": "Days"}, {"column": 3, "label": "Rate (KES)"}]
    # the user picks the rate: it is theirs to pay
    picked = cp.read_rows(table, amount_column=3)
    assert (picked[0][0].amount_text, picked[0][0].problem, picked.columns["picked"]) == ("300", None, True)


def test_the_amount_column_can_be_picked_and_never_the_name_or_the_phone(claims_env):
    cp = claims_env.cp
    table = [["Name", "Phone", "Days", "Rate", "Net Pay"], ["Jane", "0711000333", "5", "300", "1500"]]
    assert cp.read_rows(table)[0][0].amount_text == "1500"
    read = cp.read_rows(table, amount_column="3")
    assert read[0][0].amount_text == "300" and read.columns["fields"]["amount"] == "Rate"
    for wrong in (0, 1, 9, -1):
        with pytest.raises(claims_env.frappe.ValidationError, match="other than the name and the phone"):
            cp.read_rows(table, amount_column=wrong)
    with pytest.raises(claims_env.frappe.ValidationError, match="no column names to pick the amount from"):
        cp.read_rows([["Jane", "0711000333", "1500"]], amount_column=2)
    bare = cp.read_rows([["Jane", "0711000333", "1500"]])
    assert bare.columns == {"header_row": None, "fields": {}, "amount_column": None, "amount_choices": [],
                            "picked": False}


def test_a_column_that_says_it_was_paid_is_the_amount_last(claims_env):
    cp = claims_env.cp
    rows = cp.read_rows([["Name", "Phone", "Amount Paid", "Amount"], ["Jane", "0711000333", "0", "1500"]])[0]
    assert (rows[0].amount_text, rows[0].problem) == ("1500", None)  # Amount Paid 0: noted, and not paid yet


def test_parse_payees_takes_the_picked_column_and_says_where_each_field_came_from(claims_env):
    claim(claims_env, [])
    text = "NAME\tID NO\tPHONE NO\tDAYS\tRATE (KSHS)\tGRAND TOTAL\nJane\t23456789\t0711000333\t5\t300\t1500"
    answer = claims_env.cp.parse_payees(text=text, claim="HR-EXP-1")
    assert answer["rows"][0]["amount"] == 1500
    assert answer["columns"]["fields"] == {"payee_name": "NAME", "phone": "PHONE NO", "amount": "GRAND TOTAL"}
    assert [c["label"] for c in answer["columns"]["amount_choices"]] == ["ID NO", "DAYS", "RATE (KSHS)",
                                                                        "GRAND TOTAL"]
    answer = claims_env.cp.parse_payees(text=text, claim="HR-EXP-1", amount_column="4")
    assert answer["rows"][0]["amount"] == 300 and answer["columns"]["picked"] is True


def test_the_bulk_sheets_paste_takes_the_picked_column_too(bulk_env):
    text = "Name\tPhone\tDays\tRate (Kshs)\tWages\tProject\nJane Wanjiku\t0711000331\t5\t300\t1500\t"
    with as_user(bulk_env, PREPARER, "Accounts User"):
        answer = bulk_env.bulk.parse_rows(text=text, company="Astral", expense_type=CASUAL, cost_center=SITE_A)
        assert answer["rows"][0]["amount"] == 1500
        assert answer["columns"]["fields"] == {"payee_name": "Name", "phone": "Phone", "amount": "Wages",
                                               "project": "Project"}
        assert [c["label"] for c in answer["columns"]["amount_choices"]] == ["Days", "Rate (Kshs)", "Wages"]
        picked = bulk_env.bulk.parse_rows(text=text, company="Astral", expense_type=CASUAL, cost_center=SITE_A,
                                          amount_column=3)
    assert picked["rows"][0]["amount"] == 300 and picked["columns"]["fields"]["amount"] == "Rate (Kshs)"


# ---------------------------------------------------------------- 1. the dialogs (node)
DESK = r"""
const calls = [];
const replies = [];
global.__ = (s, a) => (a ? String(s).replace(/\{(\d+)\}/g, (_, i) => String(a[i])) : s);
global.flt = (v) => parseFloat(v) || 0;
global.format_currency = (v) => String(v);
global.window = global;
global.locals = {};
global.setInterval = (fn) => { setTimeout(fn, 0); return 1; };
global.clearInterval = () => {};
let last_dialog = null;
class Dialog {
	constructor(opts) {
		this.opts = opts; this.values = {}; this.df = {}; this.html = {}; this.inputs = {}; this.fields_dict = {};
		this.changes = 0;
		for (const f of opts.fields) {
			this.df[f.fieldname] = Object.assign({}, f);
			if (f.default !== undefined) this.values[f.fieldname] = f.default;
			this.fields_dict[f.fieldname] = {
				$wrapper: { html: (h) => { this.html[f.fieldname] = h; } },
				$input: { on: (ev, fn) => { this.inputs[f.fieldname] = fn; } },
			};
		}
		this.label = opts.primary_action_label;
		const btn = { disabled: false };
		btn.html = (t) => { this.label = t; return btn; };
		btn.text = (t) => { this.label = t; return btn; };
		btn.prop = (k, v) => { if (k === "disabled") btn.disabled = v; return btn; };
		this.btn = btn;
		this.$wrapper = { remove: () => {} };
		last_dialog = this;
	}
	set_df_property(f, k, v) { this.df[f][k] = v; }
	set_value(f, v) {
		this.values[f] = v;
		const df = this.df[f];
		if (df && (df.change || df.onchange)) { this.changes += 1; (df.change || df.onchange)(); }
		return Promise.resolve();
	}
	get_value(f) { return this.values[f]; }
	get_values() { return Object.assign({}, this.values); }
	get_primary_btn() { return this.btn; }
	set_primary_action(label, fn) { this.label = label; this.opts.primary_action = fn; }
	show() {}
	hide() { this.hidden = true; }
}
global.frappe = {
	ui: { Dialog, form: { on: () => {} } },
	utils: { escape_html: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;") },
	xcall: (method, args) => { calls.push([method, JSON.parse(JSON.stringify(args || {}))]);
		return Promise.resolve(replies.shift()); },
	msgprint: (m) => calls.push(["msgprint", m]),
	show_alert: () => {},
	model: { set_value: () => {} },
	provide: (ns) => { let o = global; for (const p of ns.split(".")) { o[p] = o[p] || {}; o = o[p]; } },
	boot: {},
	query_reports: {},
	defaults: { get_user_default: () => null },
};
const tick = () => new Promise((r) => setTimeout(r, 0));
"""


def run_desk(tmp_path, sources, scenario, **data):
    script = DESK + f"const DATA = {json.dumps(data)};\n"
    script += "".join(Path(s).read_text(encoding="utf-8") + "\n" for s in sources)
    script += "(async () => {\n" + scenario + "\n})().then((out) => console.log(JSON.stringify(out)));\n"
    harness = tmp_path / "desk.js"
    harness.write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, str(harness)], capture_output=True, text=True, encoding="utf-8", timeout=60,
                          env=dict(os.environ))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def answer_of(cp, table, **kw):
    read = cp.read_rows(table, **kw)
    rows = cp.check_pasted(read[0], "EMP-1", [], cp.employee_destinations())
    return {"rows": rows, "good": sum(1 for r in rows if not r["errors"]), "bad": sum(1 for r in rows if r["errors"]),
            "good_total": sum(r["amount"] or 0 for r in rows if not r["errors"]), "left_out": read[1],
            "columns": read.columns}


PASTE = """
d.values.text = "pasted";
replies.push(DATA.first, DATA.second);
d.opts.primary_action(d.get_values());
await tick(); await tick();
const first = { html: d.html.result, hidden: d.df.amount_column.hidden, value: d.values.amount_column,
                options: d.df.amount_column.options, asked: calls.length, label: d.label };
d.values.amount_column = "4";
d.df.amount_column.change();
await tick(); await tick();
const again = calls[calls.length - 1];
d.inputs.text();  // other rows pasted: the columns are read afresh
return { first, again: [again[0], again[1].amount_column], html: d.html.result, hidden: d.df.amount_column.hidden };
"""
GRAND = [["NAME", "ID NO", "PHONE NO", "DAYS", "RATE (KSHS)", "GRAND TOTAL"],
         ["Jane", "23456789", "0711000333", "5", "300", "1500"]]


def check_paste(out):
    first = out["first"]
    assert "Amount ← <b>GRAND TOTAL</b>" in first["html"] and "Phone ← <b>PHONE NO</b>" in first["html"]
    assert first["hidden"] == 0 and first["value"] == "5" and first["asked"] == 1  # setting it asked nothing
    assert [o["label"] for o in first["options"]] == ["ID NO", "DAYS", "RATE (KSHS)", "GRAND TOTAL"]
    assert out["again"][1] == 4 and "Amount ← <b>RATE (KSHS)</b>" in out["html"]
    assert out["hidden"] == 1


@node
def test_the_claims_paste_dialog_shows_the_columns_and_takes_another_amount_column(claims_env, tmp_path):
    cp = claims_env.cp
    scenario = """
const frm = { doc: { name: "HR-EXP-1", employee: "EMP-1", expenses: [{ name: "EXP-1", expense_type: "Casual" }],
                     mpesa_payees: [] }, is_new: () => false };
mpsa_payees.saved_expenses = () => [{ name: "EXP-1", expense_type: "Casual", doctype: "Expense Claim Detail" }];
mpsa_payees.state = () => ({ allowed_types: ["Casual"] });
mpsa_payees.expense_label = () => "Casual";
mpsa_payees.line_note = () => "";
mpsa_payees.paste_dialog(frm);
const d = last_dialog;
""" + PASTE
    out = run_desk(tmp_path, [APP / "public" / "js" / "expense_claim_payees.js"], scenario,
                   first=answer_of(cp, GRAND), second=answer_of(cp, GRAND, amount_column=4))
    check_paste(out)


@node
def test_the_bulk_sheets_paste_dialog_shows_the_columns_and_takes_another_amount_column(claims_env, tmp_path):
    cp = claims_env.cp
    scenario = """
const frm = { doc: { name: "BP-1", company: "Astral", rows: [] }, is_new: () => false };
mpsa_bulk.paste(frm);
const d = last_dialog;
""" + PASTE
    path = APP / "mpsa_suite" / "doctype" / "m_pesa_bulk_payout" / "m_pesa_bulk_payout.js"
    out = run_desk(tmp_path, [path], scenario, first=answer_of(cp, GRAND), second=answer_of(cp, GRAND, amount_column=4))
    check_paste(out)


# ---------------------------------------------------------------- 2. plain names, SMS names
def test_names_are_kept_as_plain_text(claims_env):
    k = kn()
    assert k.plain('<img src=x onerror=alert(document.cookie)>') == ""
    assert k.plain('<b>JANE</b> "WANJIKU" `X`') == "JANE WANJIKU X"
    assert k.plain("254705783406 - O'BRIEN & SONS") == "254705783406 - O'BRIEN & SONS"
    assert k.plain("<script") == "script"


def test_an_sms_with_html_never_changes_a_results_name(claims_env):
    env, k = claims_env, kn()
    record = store(env, "B2C:254711000333", "JANE WANJIKU", times=3)
    payee = "<img src=x onerror=alert(document.cookie)>"
    assert not k.observe("Phone", "0711000333", f"0711000333 - {payee}", when=AT + datetime.timedelta(days=1),
                         payout_doctype="Payment Entry", payout="ACC-PAY-1", source="SMS")
    assert not k.observe("Phone", "0711000333", "0711000333 - PETER OTIENO", when=AT + datetime.timedelta(days=1),
                         payout_doctype="Payment Entry", payout="ACC-PAY-2", source="SMS")
    assert (record["mpesa_name"], record["times_paid"], record["source"]) == ("JANE WANJIKU", 3, "B2C Result")
    assert cpm().mpesa_names(["0711000333"])["254711000333"].name == "JANE WANJIKU"


def test_a_results_html_is_kept_without_it(claims_env):
    k = kn()
    assert k.record_b2c(request(claims_env, "B2C-1", "0711000333", '0711000333 - <b onclick="x()">JANE</b> DOE'))
    record = known(claims_env, "B2C:254711000333")
    assert (record["mpesa_name"], record["received"]) == ("JANE DOE", "0711000333 - JANE DOE")
    # a record stored before this fix is read as plain text too
    store(claims_env, "B2C:254711000444", "<i>PETER</i>")
    assert k.lookup(["B2C:254711000444"])["B2C:254711000444"].name == "PETER"


def test_an_sms_name_starts_a_record_unconfirmed_and_a_result_replaces_it(claims_env):
    env, k, cp = claims_env, kn(), cpm()
    assert k.observe("Phone", "0711000333", "0711000333 - JANE TYPED", when=AT, payout_doctype="Payment Entry",
                     payout="ACC-PAY-1", source="SMS")
    record = known(env, "B2C:254711000333")
    assert (record["mpesa_name"], record["source"], record["times_paid"]) == ("JANE TYPED", "SMS", 1)
    # another SMS changes nothing
    assert not k.observe("Phone", "0711000333", "0711000333 - SOMEONE ELSE", when=AT + datetime.timedelta(hours=1),
                         payout_doctype="Payment Entry", payout="ACC-PAY-2", source="SMS")
    assert known(env, "B2C:254711000333")["mpesa_name"] == "JANE TYPED"
    # shown as from SMS, never a match, and it never locks a claim payee's name
    info = k.describe("B2C:254711000333", k.lookup(["B2C:254711000333"])["B2C:254711000333"], "Jane Typed")
    assert (info["status"], info["verdict"], info["confirmed"]) == ("known", "new", 0)
    assert info["badge"] == {"colour": "blue", "text": "From SMS, not confirmed by a payout: JANE TYPED"}
    names = k.summary([("B2C:254711000333", "Jane Typed")], can_see_salaries=True)
    assert names["counts"]["new"] == 1 and names["acknowledge"] == "I checked the 1 new number with the people paid."
    assert cp.mpesa_names(["0711000333"]) == {} and cp.mpesa_name("0711000333") == {}
    # the first Result naming the number replaces it
    assert k.record_b2c(request(env, "B2C-9", "0711000333", "254711000333 - JANE WANJIKU", minutes=120))
    record = known(env, "B2C:254711000333")
    assert (record["mpesa_name"], record["source"], record["times_paid"], record["last_payout"]) == (
        "JANE WANJIKU", "B2C Result", 1, "B2C-9")
    assert cp.mpesa_names(["0711000333"])["254711000333"].name == "JANE WANJIKU"


def test_the_list_view_escapes_the_names():
    script = (APP / "mpsa_suite" / "doctype" / "m_pesa_known_name" / "m_pesa_known_name_list.js").read_text(
        encoding="utf-8")
    for field in ("mpesa_name", "received", "number"):
        assert f"{field}: (value) => frappe.utils.escape_html(value || \"\")" in script


# ---------------------------------------------------------------- 3. thousands commas
@pytest.mark.parametrize("text, split", [
    ("Jane, 0711000333, 5, 300", "5,300"),
    ("Jane, 0711000333, 1,500", "1,500"),
    ("Name,Phone,Amount,Remarks\nJane,0711000333,12,000", "12,000"),
    ("Name,Phone,Amount,ID\nJane,0711000333,10,500", "10,500"),
    ("Name,Phone,Days,Amount\nJane,0711000333,15,300,000", "300,000"),  # one cell more, two places: not certain
])
def test_a_comma_split_amount_is_never_guessed(claims_env, text, split):
    cp = claims_env.cp
    (row,) = checked(cp, cp.parse_text(text))
    assert row[2] is None and row[3] == [cp.COMMA_SPLIT.format(split)]


@pytest.mark.parametrize("text, amount", [
    ("Name,Phone,Amount\nJane,0711000333,12,000", 12000),  # one cell more than the names: certain
    ("Name, Phone, Amount, Remarks\nJane, 0711000333, 1500, 12,000", 1500),  # the join is in the remarks
    ('Jane, 0711000333, "1,500"', 1500),  # quoted after a space
    ("Name,Phone,Days,Amount\nJane,0711000333,5,300", 300),  # days, then a 3-digit amount
    ("Jane\t0711000333\t1,500", 1500),  # a spreadsheet's cell
])
def test_a_comma_amount_is_read_where_it_is_certain(claims_env, text, amount):
    cp = claims_env.cp
    (row,) = checked(cp, cp.parse_text(text))
    assert (row[2], row[3]) == (amount, [])


def test_spreadsheet_cells_are_never_joined_or_called_split(claims_env):
    cp = claims_env.cp
    (row,) = cp.read_rows(cp.parse_text("Jane\t0711000333\t5\t300"))[0]
    assert "more than one number after the phone" in row.problem


# ---------------------------------------------------------------- 4. rows paid already
@pytest.mark.parametrize("header, value, problem", [
    ("M-Pesa Code", "UKD1A2B3C4", "has an M-Pesa receipt (UKD1A2B3C4)"),
    ("Mpesa Code", "ukd1a2b3c4", "has an M-Pesa receipt (ukd1a2b3c4)"),
    ("Code", "B2C-26-10-000830", "has a payment reference (B2C-26-10-000830)"),
    ("Transaction ID", "UKD1A2B3C4", "has an M-Pesa receipt (UKD1A2B3C4)"),
    ("Transaction", "ACC-PAY-2026-00001", "has a payment reference (ACC-PAY-2026-00001)"),
    ("Ref", "UKD1A2B3C4", "has an M-Pesa receipt (UKD1A2B3C4)"),
    ("Paid", "Yes", "marked paid already (Yes)"),
    ("Paid On", "2026-11-12", "marked paid already (2026-11-12)"),
    ("Remarks", "Paid", "marked Paid already"),
    ("Comment", "Settled", "marked Settled already"),
    ("Note", "UKD1A2B3C4", "has an M-Pesa receipt (UKD1A2B3C4)"),
])
def test_a_row_paid_already_is_caught_under_column_names(claims_env, header, value, problem):
    cp = claims_env.cp
    (row,) = cp.read_rows([["Name", "Phone", "Amount", header], ["Jane", "0711000333", "1500", value]])[0]
    assert row.problem == f"{problem}: not added, so it is never paid twice"


@pytest.mark.parametrize("header, value", [
    ("Paid", "No"), ("Paid", "0"), ("Paid", "Pending"), ("Ref", "7"), ("Ref No", "12"),
    ("Employee Code", "EMP001"), ("Transaction Cost", "15"), ("Remarks", "site B"), ("Status", "Pending"),
])
def test_a_column_that_names_no_payment_marks_nothing(claims_env, header, value):
    cp = claims_env.cp
    (row,) = cp.read_rows([["Name", "Phone", "Amount", header], ["Jane", "0711000333", "1500", value]])[0]
    assert (row.problem, row.amount_text) == (None, "1500")


def test_an_mpesa_code_column_is_never_the_phone(claims_env):
    cp = claims_env.cp
    read = cp.read_rows([["Name", "Amount", "M-Pesa Code", "M-Pesa No."],
                         ["Jane", "1500", "", "0711000333"]])
    assert (read[0][0].phone, read[0][0].problem) == ("0711000333", None)
    assert read.columns["fields"]["phone"] == "M-Pesa No." and read.columns["fields"]["reference"] == "M-Pesa Code"


# ---------------------------------------------------------------- 5. Pay Selected asks for the names
def pay_env(env):
    env.pi()
    schedule_row(env)
    line_row(env, "L1")
    return env


def test_pay_selected_shows_the_names_and_refuses_without_the_tick(schedule_env):
    env = pay_env(schedule_env)
    result = prepared(env, "L1")
    names = result["known_names"]
    assert (names["header"], names["acknowledge"]) == ("1 new", "I checked the 1 new number with the people paid.")
    assert result["parties"][0]["known_name"]["badge"]["colour"] == "blue"
    for refused in (None, 0, "", "0"):
        with pytest.raises(env.frappe.ValidationError, match=r'Tick "I checked the 1 new number'):
            env.schedule.confirm_payment(result["token"], acknowledged=refused)
    assert not env.frappe.db.table("Mpesa B2B Payment Request")  # nothing paid; the token still works
    answer = env.schedule.confirm_payment(result["token"], acknowledged="1")
    assert [s["line"] for s in answer["sent"]] == ["L1"]


def test_pay_selected_needs_no_tick_when_every_name_matches_and_reads_them_again(schedule_env):
    env = pay_env(schedule_env)
    env.frappe.db.table("Supplier M-Pesa Account")["MSA-1"]["expected_name"] = "Acme Fuel"
    store(env, "PAYBILL:888880", "ACME FUEL LIMITED", times=4)
    result = prepared(env, "L1")
    assert result["known_names"]["acknowledge"] is None and result["known_names"]["header"] == "1 known and matching"
    known(env, "PAYBILL:888880")["mpesa_name"] = "SOMEONE ELSE"  # a Result since the summary
    with pytest.raises(env.frappe.ValidationError, match="1 under another name"):
        env.schedule.confirm_payment(result["token"])
    known(env, "PAYBILL:888880")["mpesa_name"] = "ACME FUEL LIMITED"
    assert [s["line"] for s in env.schedule.confirm_payment(result["token"])["sent"]] == ["L1"]


def test_a_lines_expected_name_is_its_accounts_else_its_partys_else_its_payees(schedule_env):
    env, s = schedule_env, schedule_env.schedule
    assert s._name_target(_dict(route="B2B Paybill", destination="888880 / ACME-1",
                                payee_account_doctype="Supplier M-Pesa Account", payee_account="MSA-1",
                                party_type="Supplier", party="ACME")) == ("PAYBILL:888880", "ACME")
    env.frappe.db.table("Supplier M-Pesa Account")["MSA-1"]["expected_name"] = ""
    assert s._name_target(_dict(route="B2B Paybill", destination="888880 / ACME-1",
                                payee_account_doctype="Supplier M-Pesa Account", payee_account="MSA-1",
                                party_type="Supplier", party="ACME"))[1] == "Acme Fuel Ltd"
    assert s._name_target(_dict(route="B2B Till", destination="555555", payee_name="Kenya Power")) == (
        "TILL:555555", "Kenya Power")
    assert s._name_target(_dict(route="B2B Till", destination="")) is None


@node
def test_the_pay_selected_dialog_shows_the_names_and_confirms_only_once_ticked(tmp_path):
    scenario = """
const r = { count: 1, token: "t1", delay: 1, total: 3000, routes: [], balances: [], warnings: [], not_included: [],
	parties: [{ payee: "Acme", party: "ACME", route: "B2B Paybill", destination: "888880 / ACME-1", lines: 1,
		amount: 3000, known_name: { status: "new", verdict: "new",
			badge: { colour: "blue", text: "New number: never paid; check it with the person" } } }],
	known_names: { header: "1 new", acknowledge: "I checked the 1 new number with the people paid." } };
mpsa_due.confirm({}, r);
await tick();  // the countdown is over
const d = last_dialog;
const html = d.html.summary;
const field = d.df.acknowledged;
const before = d.btn.disabled;
await d.opts.primary_action();
const asked_before = calls.length;
d.values.acknowledged = 1;
field.onchange();
const after = d.btn.disabled;
await d.opts.primary_action();
return { html, label: field.label, before, after, asked_before, sent: calls.map((c) => c[1]) };
"""
    out = run_desk(tmp_path, [APP / "public" / "js" / "mpsa_send.js",
                              APP / "mpsa_suite" / "report" / "m_pesa_payments_due" / "m_pesa_payments_due.js"],
                   scenario)
    assert "New number: never paid; check it with the person" in out["html"] and "1 new" in out["html"]
    assert out["label"] == "I checked the 1 new number with the people paid."
    assert (out["before"], out["asked_before"], out["after"]) == (True, 0, False)
    assert out["sent"] == [{"token": "t1", "acknowledged": 1}]


# ---------------------------------------------------------------- 6. one typed word
@pytest.mark.parametrize("typed, name, verdict", [
    ("Jane", "JANE WANJIKU", "partial"), ("Kamau", "PETER KAMAU", "partial"), ("Shell", "SHELL KILIMANI", "partial"),
    ("Jane Wanjiku", "JANE WANJIKU NJERI", "match"), ("Mama Mboga", "MAMA MBOGA", "match"), ("Jane", "JANE", "match"),
    ("Jane Achieng", "JANE WANJIKU", "differs"), ("Peter", "JANE WANJIKU", "differs"),
])
def test_one_typed_word_against_a_longer_name_is_partial(claims_env, typed, name, verdict):
    assert kn().verdict(typed, {"name": name}) == verdict


def test_a_partial_name_is_amber_and_acknowledged_with_the_differences(claims_env):
    k = kn()
    store(claims_env, "B2C:254711000333", "JANE WANJIKU", times=2)
    store(claims_env, "B2C:254711000444", "PETER OTIENO")
    names = k.summary([("B2C:254711000333", "Jane"), ("B2C:254711000444", "John Doe")], can_see_salaries=True)
    assert names["rows"][0]["badge"] == {"colour": "amber",
                                         "text": "M-Pesa: JANE WANJIKU · only one name given: check it is this person"}
    assert names["counts"] == {"match": 0, "partial": 1, "differs": 1, "new": 0, "bank": 0, "hidden": 0}
    assert names["header"] == "1 matching one name only · 1 under another name"
    assert names["acknowledge"] == "I checked the 2 names that differ or match in part with the people paid."
    only = k.summary([("B2C:254711000333", "Jane")], can_see_salaries=True)
    assert only["acknowledge"] == "I checked the 1 name that differs or matches in part with the people paid."


@node
def test_the_desk_shows_a_partial_name_in_amber(tmp_path):
    from test_r45_known_names import KNOWN_JS, SEND_JS, run_node

    out = run_node(tmp_path, [KNOWN_JS, SEND_JS], """
const info = { status: "known", verdict: "partial", typed_name: "Jane",
	badge: { colour: "amber", text: "M-Pesa: JANE WANJIKU · only one name given: check it is this person" } };
return { badge: mpsa_known_names.badge_html(info), send: mpsa_send.name_html(info),
	counts: mpsa_known_names.counts([info, { badge: {}, verdict: "match" }]) };
""")
    assert "var(--yellow-600)" in out["badge"] and "(given: Jane)" in out["badge"]
    assert "var(--yellow-600)" in out["send"] and "Name given: Jane" in out["send"]
    assert out["counts"] == "1 known and matching · 1 matching one name only"


# ---------------------------------------------------------------- 7. the bank flag
def test_the_bank_flag_is_read_from_the_current_bank_paybill_list(claims_env):
    env, k = claims_env, kn()
    store(env, "PAYBILL:247247", "EQUITY PAYBILL AC", bank=0)  # a bank added to the list after its record
    store(env, "PAYBILL:522599", "OLD BANK AC", bank=1)  # a paybill since taken off the list
    env.frappe.db.table(BANK_PAYBILL)["EQ"] = {"name": "EQ", "paybill": "247247", "enabled": 1}
    found = k.lookup(["PB:247247:999", "PAYBILL:522599"])
    assert (found["PAYBILL:247247"].bank, found["PAYBILL:522599"].bank) == (1, 0)
    names = k.summary([("PB:247247:999", "Peter Otieno"), ("PAYBILL:522599", "Peter Otieno")], can_see_salaries=True)
    assert [r["verdict"] for r in names["rows"]] == ["match", "differs"] and names["counts"]["bank"] == 1
    answer = k.for_destinations([{"id": "a", "key": "PB:247247:999", "typed_name": "Peter"}])
    assert answer["a"]["bank"] == 1 and answer["a"]["badge"]["colour"] == "gray"


# ---------------------------------------------------------------- 8. names never lost
def test_a_lock_wait_logs_and_carries_on_but_a_deadlock_is_raised(claims_env, monkeypatch):
    env, k = claims_env, kn()
    frappe = env.frappe

    def wait(*a, **kw):
        raise frappe.QueryTimeoutError("Lock wait timeout exceeded")

    monkeypatch.setattr(frappe, "get_doc", wait)
    assert k.record_b2c(request(env, "B2C-1", "0711000333", "0711000333 - JANE")) is False
    assert ("rollback", "mpesa_known_name") in env.log
    assert any(kw.get("title") == "M-Pesa known name not kept" for _a, kw in frappe.errors)

    def deadlock(*a, **kw):
        raise frappe.QueryDeadlockError("deadlock")

    monkeypatch.setattr(frappe, "get_doc", deadlock)
    with pytest.raises(frappe.QueryDeadlockError):
        k.record_b2c(request(env, "B2C-2", "0711000333", "0711000333 - JANE"))


def credit_answer(name):
    result = status_result()
    result["ResultParameters"]["ResultParameter"].append({"Key": "CreditPartyName", "Value": name})
    return result


def test_a_status_answer_keeps_the_b2c_payouts_name(flow_env):
    row = flow_payout(flow_env, phone_number="254708374149", party_type="Supplier", party="ACME",
                      receiver_public_name=None)
    flow_env.api.b2c_status_result(Result=credit_answer("254708374149 - JOHN DOE"), ref=flow_start(flow_env))
    assert (row["status"], row["receiver_public_name"]) == ("Paid", "254708374149 - JOHN DOE")
    record = flow_env.frappe.db.table(KNOWN)["B2C:254708374149"]
    assert (record["mpesa_name"], record["source"], record["last_payout"]) == ("JOHN DOE", "B2C Result", row["name"])


def test_a_status_answer_leaves_a_name_already_given_and_keeps_none_of_another_number(flow_env):
    row = flow_payout(flow_env, phone_number="254708374149", receiver_public_name="254708374149 - JOHN D")
    flow_env.api.b2c_status_result(Result=credit_answer("254708374149 - JOHN DOE"), ref=flow_start(flow_env))
    assert row["receiver_public_name"] == "254708374149 - JOHN D"
    assert flow_env.frappe.db.table(KNOWN)["B2C:254708374149"]["mpesa_name"] == "JOHN D"
    source = (APP / "money_out" / "b2c_status.py").read_text(encoding="utf-8")
    assert 'values.get("CreditPartyName")' in source and "known_names.record_b2c(b2c)" in source


# ---------------------------------------------------------------- 9. an employee's phone
def readers(env, monkeypatch, perms):
    """frappe.permissions.get_valid_perms: ``perms`` {doctype: roles given read (level 0)}."""
    permissions = types.ModuleType("frappe.permissions")
    permissions.get_valid_perms = lambda doctype, user: [
        {"role": role, "read": 1, "permlevel": 0, "if_owner": 0}
        for role in perms.get(doctype, ()) if role in env.frappe.user_roles]
    monkeypatch.setitem(sys.modules, "frappe.permissions", permissions)


@pytest.mark.parametrize("values, employee", [
    ({"reference_doctype": CLAIM, "party_type": "Employee"}, 1),
    ({"reference_doctype": "Employee Advance", "party_type": "Employee"}, 1),
    ({"reference_doctype": "Salary Slip", "party_type": "Employee"}, 1),
    ({"reference_doctype": "Purchase Invoice", "party_type": "Supplier"}, 0),
    ({"reference_doctype": "M-Pesa Bulk Payout", "party_type": None}, 0),
])
def test_a_phone_paid_to_an_employee_is_marked(claims_env, values, employee):
    kn().record_b2c(request(claims_env, "B2C-1", "0711000555", "0711000555 - MARY AKINYI", **values))
    assert known(claims_env, "B2C:254711000555")["employee_payee"] == employee


def test_the_mark_stays_once_set(claims_env):
    k = kn()
    k.record_b2c(request(claims_env, "B2C-1", "0711000555", "0711000555 - MARY AKINYI", reference_doctype=None,
                         party_type=None))
    # a salary to the same phone: left out of what others see, but the phone is an employee's now
    assert k.record_b2c(request(claims_env, "B2C-2", "0711000555", "0711000555 - MARY SALARY", minutes=5,
                                reference_doctype="Salary Slip"))
    record = known(claims_env, "B2C:254711000555")
    assert (record["mpesa_name"], record["times_paid"], record["employee_payee"]) == ("MARY AKINYI", 1, 1)
    k.record_b2c(request(claims_env, "B2C-3", "0711000555", "0711000555 - MARY AKINYI", minutes=9,
                         reference_doctype=None, party_type=None))
    assert known(claims_env, "B2C:254711000555")["employee_payee"] == 1
    assert k.rebuild([k.b2c_observation(r, "History") for r in claims_env.frappe.db.table(REQ).values()])[
        "B2C:254711000555"]["employee_payee"] == 1  # the backfill marks it too


def test_an_employees_phone_is_hidden_from_who_reads_neither_claims_nor_salaries(claims_env, monkeypatch):
    env, k, frappe = claims_env, kn(), claims_env.frappe
    store(env, "B2C:254711000555", "MARY AKINYI", employee_payee=1)
    store(env, "TILL:835811", "SHELL KILIMANI", employee_payee=1)  # tills and paybills are never hidden so
    readers(env, monkeypatch, {CLAIM: ("Accounts User", "HR User", "Expense Approver")})
    asked = [{"id": "a", "key": "B2C:254711000555", "typed_name": "Mary"}, {"id": "b", "key": "TILL:835811"}]
    frappe.user_roles = {"Purchase Manager"}
    answer = k.for_destinations(asked)
    assert answer["a"]["status"] == "hidden" and "name" not in answer["a"] and "last_payout" not in answer["a"]
    assert answer["b"]["name"] == "SHELL KILIMANI"
    assert k.summary([("B2C:254711000555", "Mary")])["counts"]["hidden"] == 1
    assert k.has_permission(_dict(employee_payee=1, destination_type="Phone")) is False
    assert k.has_permission(_dict(employee_payee=1, destination_type="Till")) is True
    assert "employee_payee" in k.query_conditions("buyer@example.com")
    frappe.user_roles = {"Accounts User"}  # reads Expense Claims
    assert k.for_destinations(asked)["a"]["name"] == "MARY AKINYI"
    assert k.has_permission(_dict(employee_payee=1, destination_type="Phone")) is True
    assert k.query_conditions("accounts@example.com") == "COALESCE(`tabM-Pesa Known Name`.salary_only, 0) = 0"
    frappe.user_roles = {"HR User"}  # sees salaries
    assert k.query_conditions("hr@example.com") == "" and k.for_destinations(asked)["a"]["status"] == "known"


def test_the_doctype_has_the_mark():
    doc = json.loads((APP / "mpsa_suite" / "doctype" / "m_pesa_known_name" / "m_pesa_known_name.json")
                     .read_text(encoding="utf-8"))
    (field,) = [f for f in doc["fields"] if f["fieldname"] == "employee_payee"]
    assert (field["fieldtype"], field["read_only"], field["label"]) == ("Check", 1, "Paid to an Employee")
    assert doc["field_order"][-1] == "employee_payee" and doc["modified"] > "2026-11-13 12:00:00"


# ---------------------------------------------------------------- 10. row numbers
def test_blank_lines_keep_their_row_numbers(claims_env):
    cp = claims_env.cp
    text = "Jane, 0711000333, 1500\nPeter, 0711000444, 2000\n\n\nAnn, 0711000555, x\n"
    rows, left_out = cp.read_rows(cp.parse_text(text))
    assert [r.row for r in rows] == [1, 2, 5] and left_out == []
    claim(claims_env, [])
    answer = cp.parse_payees(text="Name, Phone, Amount\n\nJane, 0711000333, 1500\n\nBad, 1, 1", claim="HR-EXP-1")
    assert [r["row"] for r in answer["rows"]] == [3, 5] and answer["left_out"] == [1]
