"""Sandbox Test Lab: payload shapes, the sandbox-only guard, the scenario catalogue, the runner's
timeout / summary logic and the anti-forgery checks (on an in-memory frappe)."""

import datetime
import importlib
import json
import os
import random

import pytest

import fake_frappe

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN_JSON = os.path.join(ROOT, "frappe_mpsa_payments", "frappe_mpsa_payments", "doctype",
                        "mpesa_sandbox_test_run", "mpesa_sandbox_test_run.json")
NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        frappe.conf.mpesa_sandbox_lab = 1
        frappe.user_roles = {"M-Pesa Manager"}
        frappe.db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"] = {}
        mods = {
            "frappe": frappe,
            "payloads": importlib.import_module("frappe_mpsa_payments.sandbox_lab.payloads"),
            "guard": importlib.import_module("frappe_mpsa_payments.sandbox_lab.guard"),
            "fw": importlib.import_module("frappe_mpsa_payments.sandbox_lab.framework"),
            "sc": importlib.import_module("frappe_mpsa_payments.sandbox_lab.scenarios"),
            "runner": importlib.import_module("frappe_mpsa_payments.sandbox_lab.runner"),
        }
        yield fake_frappe._dict(mods)
    finally:
        fake_frappe.uninstall(saved)


# ---------------------------------------------------------------- payloads (pure)
def test_simulate_paybill_and_buy_goods():
    from frappe_mpsa_payments.sandbox_lab import payloads as p

    body = p.c2b_simulate("600999", 10, bill_ref="SINV-1")
    assert body == {"ShortCode": "600999", "CommandID": "CustomerPayBillOnline", "Amount": 10,
                    "Msisdn": "254708374149", "BillRefNumber": "SINV-1"}
    till = p.c2b_simulate("600999", 10, bill_ref="SINV-1", paybill=False)
    assert till["CommandID"] == "CustomerBuyGoodsOnline" and till["BillRefNumber"] is None
    with pytest.raises(ValueError):
        p.c2b_simulate("600999", 0)


def test_fake_receipts_are_recognisable():
    from frappe_mpsa_payments.sandbox_lab import payloads as p

    r = p.fake_receipt(random.Random(1))
    assert len(r) == 10 and r.startswith("ZZ") and r.isalnum() and r.isupper()
    assert len(p.lab_reference("U", random.Random(1))) <= 12


def test_typo_variant_is_what_the_matcher_repairs():
    from frappe_mpsa_payments.sandbox_lab import payloads as p

    assert p.typo_variant("ACC-SINV-2026-00012") == "accsinv202600012"
    assert p.typo_variant("abc123") == "ABC123"  # never returns the reference unchanged


def test_stk_callbacks_paid_and_cancelled():
    from frappe_mpsa_payments.sandbox_lab import payloads as p

    paid = p.stk_callback("ws_CO_1", "M1", 0, 10, "ZZ12345678", "254712345678", NOW)["Body"]["stkCallback"]
    items = {i["Name"]: i["Value"] for i in paid["CallbackMetadata"]["Item"]}
    assert items == {"Amount": 10.0, "MpesaReceiptNumber": "ZZ12345678", "TransactionDate": 20261018120000,
                     "PhoneNumber": 254712345678}
    cancelled = p.stk_callback("ws_CO_1", result_code=1032)["Body"]["stkCallback"]
    assert cancelled["ResultCode"] == 1032 and "CallbackMetadata" not in cancelled
    inner = {"CheckoutRequestID": "ws_CO_1"}
    assert p.stk_body(inner) == p.stk_body({"Body": {"stkCallback": inner}}) == {"Body": {"stkCallback": inner}}


def test_confirmation_rebuilt_from_register():
    from frappe_mpsa_payments.sandbox_lab import payloads as p

    row = {"transid": "SBX1", "transamount": 10, "businessshortcode": "600999", "billrefnumber": "SINV-1",
           "msisdn": "254708374149", "transtime": "20261018120000", "firstname": "John"}
    body = p.confirmation_from_register(row)
    assert body["TransID"] == "SBX1" and body["TransAmount"] == "10.00" and body["BillRefNumber"] == "SINV-1"
    assert body["TransactionType"] == "Pay Bill"
    assert set(body) == set(p.c2b_confirmation("X", 1, "600999"))


# ---------------------------------------------------------------- guard
def test_lab_off_refuses_everything(env):
    env.frappe.conf.mpesa_sandbox_lab = 0
    with pytest.raises(env.frappe.ValidationError, match="Sandbox Test Lab is off"):
        env.guard.require_lab()


def test_lab_needs_admin_role(env):
    env.frappe.user_roles = {"Accounts User"}
    with pytest.raises(env.frappe.PermissionError):
        env.guard.require_lab()


def test_lab_refuses_live_settings(env):
    env.frappe.db.table("Mpesa Settings")["Live"] = {"name": "Live", "sandbox": 0}
    env.frappe.db.table("Mpesa Settings")["Test"] = {"name": "Test", "sandbox": 1}
    env.guard.require_lab(fake_frappe._dict(c2b_settings="Test"))
    with pytest.raises(env.frappe.ValidationError, match="not a Sandbox record"):
        env.guard.require_lab(fake_frappe._dict(c2b_settings="Test", b2c_settings="Live"))


# ---------------------------------------------------------------- catalogue integrity
def test_catalogue_is_consistent(env):
    run_fields = {f["fieldname"] for f in json.load(open(RUN_JSON, encoding="utf8"))["fields"]}
    keys = {s.key for s in env.sc.SCENARIOS}
    assert len(keys) == len(env.sc.SCENARIOS)
    for s in env.sc.SCENARIOS:
        assert callable(s.start) and callable(s.check), s.key
        assert not s.depends or s.depends in keys, s.key
        assert set(s.needs) <= run_fields, (s.key, set(s.needs) - run_fields)
        if s.mode == env.fw.GUIDED:
            assert s.instructions, s.key
    groups = {s.group.split(". ")[1] for s in env.sc.SCENARIOS}
    assert groups == {"Health", "C2B", "STK", "B2C", "B2B", "Reversal"}


# ---------------------------------------------------------------- runner
def make_run(env, *scenarios, **fields):
    run = fake_frappe._dict(name="MLAB-1", cases=[], **fields)
    for i, (key, status) in enumerate(scenarios):
        row = {"name": f"C{i}", "parent": "MLAB-1", "parenttype": env.fw.RUN, "scenario": key, "status": status,
               "state": "{}", "started_on": NOW - datetime.timedelta(minutes=1)}
        env.frappe.db.table(env.fw.CASE)[row["name"]] = row
        run.cases.append(fake_frappe._dict(row))
    run.get = lambda k, d=None: run[k] if k in run else d
    return run


def test_load_scenarios_adds_each_once(env):
    run = fake_frappe._dict(cases=[])
    run.append = lambda table, row: run.cases.append(fake_frappe._dict(row))
    assert env.runner.load_scenarios(run) == len(env.sc.SCENARIOS)
    assert env.runner.load_scenarios(run) == 0


def test_waiting_case_times_out(env, monkeypatch):
    run = make_run(env, ("c2b_unknown", "Waiting"))
    case = run.cases[0]
    monkeypatch.setitem(env.sc.BY_KEY, "c2b_unknown",
                        env.fw.Scenario("c2b_unknown", "2. C2B", "t", None, lambda ctx: (env.fw.WAITING, "still"),
                                        timeout_minutes=5))
    assert env.runner.check_case(run, case) == "Waiting"
    env.frappe.db.table(env.fw.CASE)["C0"]["started_on"] = NOW - datetime.timedelta(minutes=6)
    assert env.runner.check_case(run, case) == "Failed"
    assert "No result after 5 minutes" in env.frappe.db.table(env.fw.CASE)["C0"]["result"]


def test_check_error_fails_the_case_not_the_job(env, monkeypatch):
    run = make_run(env, ("c2b_unknown", "Waiting"))

    def boom(ctx):
        raise RuntimeError("bad")

    monkeypatch.setitem(env.sc.BY_KEY, "c2b_unknown", env.fw.Scenario("c2b_unknown", "2. C2B", "t", None, boom))
    assert env.runner.check_case(run, run.cases[0]) == "Failed"


def test_dependent_scenario_skipped_until_prerequisite_passes(env):
    run = make_run(env, ("c2b_exact", "Waiting"), ("c2b_duplicate", "Pending"))
    assert env.runner.start_case(run, run.cases[1]) == "Skipped"
    assert "first" in env.frappe.db.table(env.fw.CASE)["C1"]["result"]


def test_missing_setup_skips_with_what_to_set(env):
    run = make_run(env, ("c2b_unknown", "Pending"))
    run.meta = fake_frappe._dict(get_label=lambda f: "C2B Mpesa Settings")
    assert env.runner.start_case(run, run.cases[0]) == "Skipped"
    assert env.frappe.db.table(env.fw.CASE)["C0"]["result"] == "Set C2B Mpesa Settings on the test run"


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["Passed", "Waiting"], "Running"),
        (["Passed", "Failed"], "Failed"),
        (["Passed", "Passed"], "Passed"),
        (["Passed", "Pending"], "Partly Run"),
        (["Pending"], "Draft"),
    ],
)
def test_run_summary(env, statuses, expected):
    env.frappe.db.table(env.fw.RUN)["MLAB-1"] = {"name": "MLAB-1"}
    make_run(env, *[("health", s) for s in statuses])
    env.runner.update_summary("MLAB-1")
    assert env.frappe.db.table(env.fw.RUN)["MLAB-1"]["status"] == expected


# ---------------------------------------------------------------- anti-forgery checks
def forged_ctx(env, transid="ZZFORGED01", minutes_ago=5):
    run = make_run(env, ("c2b_forged_receipt", "Waiting"))
    ctx = env.fw.Ctx(run, run.cases[0])
    ctx.state.update(transid=transid, since=NOW - datetime.timedelta(minutes=minutes_ago))
    return ctx


def test_forged_receipt_that_becomes_a_register_fails(env):
    env.frappe.db.table("Mpesa C2B Payment Register")["R1"] = {"name": "R1", "transid": "ZZFORGED01", "docstatus": 0}
    status, msg = env.sc.check_c2b_forged_receipt(forged_ctx(env))
    assert status == "Failed" and "became a receipt" in msg


def test_forged_receipt_that_never_lands_passes_after_settling(env):
    assert env.sc.check_c2b_forged_receipt(forged_ctx(env, minutes_ago=1))[0] == "Waiting"
    assert env.sc.check_c2b_forged_receipt(forged_ctx(env, minutes_ago=5))[0] == "Passed"


def test_forged_stk_success_accepted_is_critical(env):
    run = make_run(env, ("stk_forged", "Waiting"))
    ctx = env.fw.Ctx(run, run.cases[0])
    ctx.state.update(request="REQ1", forged_receipt="ZZFAKE0001")
    env.frappe.db.table("Mpesa Express Request")["REQ1"] = {"name": "REQ1", "status": "Completed",
                                                           "transaction_id": "ZZFAKE0001"}
    status, msg = env.sc.check_stk_forged(ctx)
    assert status == "Failed" and "CRITICAL" in msg


def test_forged_stk_rejected_by_verification_passes(env):
    run = make_run(env, ("stk_forged", "Waiting"))
    ctx = env.fw.Ctx(run, run.cases[0])
    ctx.state.update(request="REQ1", forged_receipt="ZZFAKE0001")
    env.frappe.db.table("Mpesa Express Request")["REQ1"] = {"name": "REQ1", "status": "In Progress",
                                                           "verification_status": "Mismatch"}
    assert env.sc.check_stk_forged(ctx)[0] == "Passed"


def test_duplicate_stk_booking_detected(env):
    run = make_run(env, ("stk_duplicate", "Waiting"))
    ctx = env.fw.Ctx(run, run.cases[0])
    ctx.state.update(request="REQ1", receipt="SBX123", entries_before=["PE-1"], since=NOW - datetime.timedelta(minutes=5))
    env.frappe.db.table("Mpesa Express Request")["REQ1"] = {"name": "REQ1", "status": "Completed"}
    env.frappe.db.table("Payment Entry")["PE-1"] = {"name": "PE-1", "reference_no": "SBX123", "docstatus": 1}
    assert env.sc.check_stk_duplicate(ctx)[0] == "Passed"
    env.frappe.db.table("Payment Entry")["PE-2"] = {"name": "PE-2", "reference_no": "SBX123", "docstatus": 0}
    assert env.sc.check_stk_duplicate(ctx)[0] == "Failed"


def test_rejected_answer_detection(env):
    assert env.fw.rejected({"ResultCode": 1, "ResultDesc": "Rejected"})
    assert not env.fw.rejected({"ResultCode": 0, "ResultDesc": "Accepted"})
    assert not env.fw.rejected({"_raw": "Internal Server Error"})
