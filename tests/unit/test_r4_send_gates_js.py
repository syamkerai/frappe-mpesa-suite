"""Round 4 review (WP1): the desk offers payout buttons only to who the server accepts (decision 1:
System Manager configures, the shared Administrator login does not pay), and Send via M-Pesa's
summary names whose bank account it is. Run in node against a small stand-in for the desk."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DUE = ROOT / "mpsa_suite" / "mpsa_suite" / "report" / "m_pesa_payments_due" / "m_pesa_payments_due.js"
SEND = ROOT / "mpsa_suite" / "public" / "js" / "mpsa_send.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="node is not installed")

PRELUDE = r"""
const fs = require("fs");
const setup = JSON.parse(process.env.MPSA_SETUP || "{}");
global.__ = (s, args) => (args ? String(s).replace(/\{(\d+)\}/g, (_, i) => String(args[i])) : s);
global.flt = (v) => parseFloat(v) || 0;
global.format_currency = (v) => String(v);
global.window = global;
global.frappe = {
	boot: { mpsa: setup.mpsa || {} },
	provide: (ns) => { let o = global; for (const p of ns.split(".")) { o[p] = o[p] || {}; o = o[p]; } },
	ui: { form: { on: () => {} } },
	query_reports: {},
	defaults: { get_user_default: () => null },
	user: { has_role: (roles) => [].concat(roles).some((r) => (setup.roles || []).includes(r)) },
	model: { can_write: (dt) => (setup.can_write || []).includes(dt) },
	utils: { escape_html: (s) => String(s) },
};
global.load = (path) => (0, eval)(fs.readFileSync(path, "utf8"));
"""


def run(tmp_path, path, scenario, **setup):
    script = (PRELUDE + f"load({json.dumps(str(path))});\n"
              + "console.log(JSON.stringify((() => {\n" + scenario + "\n})()));\n")
    harness = tmp_path / "harness.js"
    harness.write_text(script, encoding="utf-8")
    done = subprocess.run([NODE, str(harness)], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, MPSA_SETUP=json.dumps(setup)))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


BUTTONS = """
const labels = [];
const report = { page: { add_inner_button: (label) => labels.push(label) } };
frappe.query_reports["M-Pesa Payments Due"].onload(report);
return labels;
"""
SCHEDULE = ["M-Pesa Payment Schedule"]


@pytest.mark.parametrize("who,setup,expected", [
    ("System Manager", dict(mpsa={"can_collect": True, "can_configure": True}, roles=["System Manager"], can_write=[]),
     ["Refresh Balances"]),
    ("System Manager with a site rule giving it write", dict(mpsa={"can_collect": True, "can_configure": True},
                                                             roles=["System Manager"], can_write=SCHEDULE),
     ["Refresh Balances"]),
    ("Administrator refused by the Suite setting", dict(
        mpsa={"can_prepare": True, "can_collect": True, "can_configure": True, "can_see_salaries": True},
        roles=["System Manager", "M-Pesa Manager", "M-Pesa Payer", "Accounts User"], can_write=SCHEDULE),
     ["Move Date", "Plan Again", "Refresh Balances"]),
    ("M-Pesa Payer", dict(mpsa={"can_pay": True, "can_see_salaries": True}, roles=["M-Pesa Payer"], can_write=[]),
     ["Pay Selected"]),
    ("Accounts User", dict(mpsa={"can_prepare": True, "can_collect": True}, roles=["Accounts User"],
                           can_write=SCHEDULE), ["Move Date", "Plan Again", "Refresh Balances"]),
    ("HR User (prepares, no write on schedules)", dict(mpsa={"can_prepare": True}, roles=["HR User"], can_write=[]),
     []),
    ("M-Pesa Manager", dict(mpsa={"can_prepare": True, "can_pay": True, "can_approve": True}, roles=["M-Pesa Manager"],
                            can_write=SCHEDULE), ["Pay Selected", "Move Date", "Plan Again", "Refresh Balances"]),
])
def test_payments_due_offers_what_the_server_accepts(tmp_path, who, setup, expected):
    assert run(tmp_path, DUE, BUTTONS, **setup) == expected, who


SUMMARY = """
return [%s].map((rows) => mpsa_send.summary_html({doctype: "Supplier M-Pesa Account", rows, problems: []}));
"""


def test_send_summary_names_the_holder_of_a_bank_account(tmp_path):
    bank = {"receiver": "Acme Hardware Ltd", "destination": "Equity Bank (bank paybill 247247), account 0123456789",
            "account_holder": "Someone Else Ltd", "expected_name": "EQUITY BANK"}
    phone = {"receiver": "Acme Hardware Ltd", "destination": "M-Pesa phone 2547****5678", "account_holder": None}
    with_holder, without = run(tmp_path, SEND, SUMMARY % ", ".join(json.dumps([r]) for r in (bank, phone)))
    assert "Equity Bank (bank paybill 247247), account 0123456789" in with_holder
    assert "Account holder: Someone Else Ltd" in with_holder
    assert "Account holder" not in without


WARNED = """
const r = {doctype: "Supplier M-Pesa Account", rows: [], problems: [], warnings: ["The account holder X is not Y"]};
return [mpsa_send.summary_html(r), mpsa_send.summary_html(Object.assign({}, r, {warnings: []}))];
"""


def test_the_summary_shows_what_to_check_before_confirming(tmp_path):
    # final review: Approve shows the third-party account warning, which used to show only at the submit
    warned, plain = run(tmp_path, SEND, WARNED)
    assert "Check before you confirm" in warned and "The account holder X is not Y" in warned
    assert "Cannot be confirmed" not in warned, "a warning does not stop the confirm"
    assert "Check before you confirm" not in plain
