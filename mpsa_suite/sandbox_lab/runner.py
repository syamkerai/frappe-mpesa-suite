"""Starts lab scenarios, checks them every minute until they pass / fail / time out, and keeps the
run's summary. Starting happens in the web request (Daraja callback URLs are built from it);
checking only reads the database, so it runs from the scheduler."""

import json
from urllib.parse import urlparse

import frappe
from frappe.utils import add_to_date, cint, get_datetime, get_url, now_datetime

from ..daraja import apis
from ..daraja.guard import callback_url_problems
from .framework import CASE, FAILED, PASSED, PENDING, RUN, SKIPPED, WAITING, Ctx, Fail, Skip, post_callback, rejected
from .guard import lab_enabled, require_lab
from .scenarios import BY_KEY, SCENARIOS

PING = "mpsa_suite.sandbox_lab.api.ping"

# Switches each test-run record needs; the reversal test reverses a receipt on the C2B record.
LAB_APIS = {
    "c2b_settings": {apis.C2B, apis.REVERSAL},
    "stk_settings": {apis.STK},
    "b2c_settings": {apis.B2C},
    "b2b_settings": {apis.B2B},
}


# ---------------------------------------------------------------- loading
def load_scenarios(run) -> int:
    """Add a row for every scenario the run does not have yet; returns how many were added."""
    have = {row.scenario for row in run.cases}
    added = 0
    for s in SCENARIOS:
        if s.key in have:
            continue
        run.append("cases", {
            "scenario": s.key,
            "group": s.group,
            "title": s.title,
            "mode": s.mode,
            "instructions": s.instructions,
            "status": PENDING,
        })
        added += 1
    return added


def case_row(run, case_name: str):
    for row in run.cases:
        if row.name == case_name:
            return row
    frappe.throw(f"Case {case_name} is not on {run.name}")


# ---------------------------------------------------------------- starting
def start_case(run, case) -> str:
    """Run a scenario's start; returns the case's new status."""
    scenario = BY_KEY.get(case.scenario)
    if not scenario:
        return _finish(case, SKIPPED, f"Unknown scenario {case.scenario}")
    _reset(case)
    ctx = Ctx(run, case)
    try:
        if scenario.depends:
            status, _state, _row = ctx.other(scenario.depends)
            if status != PASSED:
                raise Skip(f"Run '{BY_KEY[scenario.depends].title}' first (it must pass)")
        for fieldname in scenario.needs:
            ctx.need(fieldname, run.meta.get_label(fieldname))
        frappe.db.set_value(CASE, case.name, {"status": WAITING, "started_on": now_datetime(),
                                              "result": "Starting..."})
        savepoint = "mpesa_lab_start"
        frappe.db.savepoint(savepoint)
        try:
            scenario.start(ctx)
        except (Skip, Fail):
            frappe.db.rollback(save_point=savepoint)
            raise
    except Skip as e:
        return _finish(case, SKIPPED, str(e))
    except Fail as e:
        return _finish(case, FAILED, str(e))
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"M-Pesa lab: {case.scenario} failed to start")
        return _finish(case, FAILED, f"Error while starting: {e}"[:500] + " (see Error Log)")

    if ctx.result:
        return _finish(case, *ctx.result)
    frappe.db.set_value(CASE, case.name, "result",
                        scenario.instructions or "Started; checking every minute")
    return check_case(run, case)


def _reset(case) -> None:
    frappe.db.set_value(CASE, case.name, {"state": "{}", "result": "", "reference_doctype": None,
                                          "reference_name": None, "finished_on": None})
    case.state = "{}"


def _finish(case, status: str, message: str) -> str:
    frappe.db.set_value(CASE, case.name, {"status": status, "result": (message or "")[:2000],
                                          "finished_on": now_datetime()})
    case.status = status
    return status


# ---------------------------------------------------------------- checking
def check_case(run, case) -> str:
    fresh = frappe.db.get_value(CASE, case.name, ["status", "state", "started_on"], as_dict=True)
    if fresh.status != WAITING:
        return fresh.status
    case.state = fresh.state
    scenario = BY_KEY[case.scenario]
    ctx = Ctx(run, case)
    try:
        status, message = scenario.check(ctx)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"M-Pesa lab: checking {case.scenario} failed")
        status, message = FAILED, f"Error while checking: {e}"[:500] + " (see Error Log)"
    if status == WAITING:
        deadline = add_to_date(get_datetime(fresh.started_on), minutes=scenario.timeout_minutes)
        if now_datetime() > deadline:
            return _finish(case, FAILED, f"No result after {scenario.timeout_minutes} minutes. Last state: {message}")
        frappe.db.set_value(CASE, case.name, "result", message[:2000])
        return WAITING
    return _finish(case, status, message)


def check_run(run_name: str) -> None:
    run = frappe.get_doc(RUN, run_name)
    for case in run.cases:
        if case.status == WAITING:
            check_case(run, case)
    update_summary(run_name)


def check_waiting() -> None:
    """Scheduler (every minute): advance every waiting case on the site."""
    if not lab_enabled():
        return
    runs = frappe.get_all(CASE, filters={"status": WAITING, "parenttype": RUN}, pluck="parent", distinct=True)
    for run_name in runs:
        try:
            check_run(run_name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa lab: checking {run_name} failed")


def update_summary(run_name: str) -> None:
    rows = frappe.get_all(CASE, filters={"parent": run_name, "parenttype": RUN}, pluck="status")
    counts = {s: rows.count(s) for s in (PASSED, FAILED, WAITING, SKIPPED, PENDING)}
    if counts[WAITING]:
        status = "Running"
    elif counts[FAILED]:
        status = "Failed"
    elif counts[PASSED] and not counts[PENDING]:
        status = "Passed"
    elif counts[PASSED] or counts[SKIPPED]:
        status = "Partly Run"
    else:
        status = "Draft"
    frappe.db.set_value(RUN, run_name, {
        "status": status,
        "passed": counts[PASSED],
        "failed": counts[FAILED],
        "waiting": counts[WAITING],
        "skipped": counts[SKIPPED],
        "last_checked": now_datetime(),
    })


# ---------------------------------------------------------------- health check
def health_check(run) -> list:
    """Everything that must be right before a test can mean anything. Each item: level
    (ok / warning / error), label, detail."""
    from ..daraja.client import base_url, fetch_token

    require_lab(run)
    items = []

    def add(level, label, detail):
        items.append({"level": level, "label": label, "detail": detail})

    url = get_url()
    host = urlparse(url).hostname or ""
    if host in ("localhost", "127.0.0.1") or host.endswith(".local") or host.replace(".", "").isdigit():
        add("error", "Public address", f"{url} is not reachable from Safaricom; set host_name to the site's public https URL")
    elif not url.startswith("https://"):
        add("warning", "Public address", f"{url} is http; sandbox allows it, production needs https")
    else:
        add("ok", "Public address", url)
    # Daraja refuses any callback URL with a banned word, so one in the host breaks them all.
    for problem in callback_url_problems(url, sandbox=True):
        add("error", "Public address", f"{url}: {problem}; Safaricom refuses every callback URL on it")

    suite = frappe.get_single("M-Pesa Suite Settings")
    add("ok" if suite.integration_user else "warning", "Integration User",
        suite.integration_user or "not set: callbacks run as Administrator")

    seen = set()
    for field in ("c2b_settings", "stk_settings", "b2c_settings", "b2b_settings"):
        name = run.get(field)
        if not name or name in seen:
            continue
        seen.add(name)
        s = frappe.get_doc("Mpesa Settings", name)
        try:
            fetch_token(s.consumer_key, s.get_password("consumer_secret"), base_url(s.sandbox), force=True)
            add("ok", f"Token: {name}", "Daraja accepted the Consumer Key / Secret")
        except Exception as e:
            add("error", f"Token: {name}", f"Daraja refused the Consumer Key / Secret: {e}"[:300])
        if field != "stk_settings":
            if s.initiator_name and s.security_credential:
                add("ok", f"Initiator: {name}", s.initiator_name)
            else:
                add("error", f"Initiator: {name}", "Initiator Name / Password missing (Daraja Test Credentials)")
        if field == "stk_settings" and not frappe.db.get_value("Payment Gateway", {"gateway_controller": name}, "name"):
            add("error", f"Payment Gateway: {name}", "missing: set the Company on this Mpesa Settings and save")

    # A record may fill several roles, so collect what each needs before checking its switches.
    needs = {}
    for field, wanted in LAB_APIS.items():
        if run.get(field):
            needs.setdefault(run.get(field), set()).update(wanted)
    for name, wanted in needs.items():
        s = frappe.db.get_value("Mpesa Settings", name, list(apis.ALL), as_dict=True) or {}
        missing = [apis.LABELS[api] for api in apis.ALL if api in wanted and not apis.enabled(s, api)]
        if missing:
            add("error", f"APIs: {name}", "tick " + ", ".join(missing) + " under APIs this record uses")
        else:
            add("ok", f"APIs: {name}", apis.describe(s))

    try:
        status, answer = post_callback(PING, {"ping": 1})
        if isinstance(answer, dict) and answer.get("lab") == "pong":
            add("ok", "Callback URL", "our public callback URL answered with the token accepted")
        elif rejected(answer):
            add("error", "Callback URL", "reachable but rejected the lab's post (IP allow-list on?): "
                + str(suite.last_rejected_callback_reason or ""))
        else:
            add("error", "Callback URL", f"unexpected answer (HTTP {status}): {str(answer)[:200]}")
    except Exception as e:
        add("error", "Callback URL", f"could not reach {url} from the server itself: {e}"[:300])

    if run.c2b_settings:
        reg = frappe.get_all("Mpesa C2B Payment Register URL", filters={"mpesa_settings": run.c2b_settings},
                             fields=["name", "register_status"], limit=5)
        if any(r.register_status == "Success" for r in reg):
            add("ok", "C2B URLs", "registered with Safaricom")
        else:
            add("error", "C2B URLs", "not registered: create an Mpesa C2B Payment Register URL for "
                f"{run.c2b_settings} (C2B tests wait forever otherwise)")

    flags = {
        "verify_c2b_confirmations": "Verify C2B Confirmations",
        "verify_stk_callbacks": "Verify STK Callbacks",
        "enforce_token_c2b": "Require Token on C2B",
        "enforce_token_results": "Require Token on STK / B2C callbacks",
        "enable_matching": "Match and Book Receipts",
    }
    off = [label for field, label in flags.items() if not cint(suite.get(field))]
    add("warning" if off else "ok", "Go-live switches",
        ("off: " + ", ".join(off) + ". Tests show what happens with them off.") if off else "all on")
    if cint(suite.enforce_ip_allowlist):
        add("warning", "IP allow-list", "on: replay and forgery tests are skipped (the lab posts from this server)")

    frappe.db.set_value(RUN, run.name, "health_report", json.dumps(items, indent=1))
    return items
