"""Buttons of the Mpesa Sandbox Test Run form, plus the ad-hoc tools (simulate a C2B payment,
replay any stored callback). All need the lab switched on, sandbox records and an M-Pesa admin."""

import json

import frappe
from frappe.utils import cint

from ..daraja.callbacks import KIND_RESULT, rejected_response, safaricom_callback
from ..daraja.guard import is_zero_code
from . import payloads
from .framework import AUTO, PENDING, RUN, post_callback
from .guard import lab_enabled, require_lab, require_sandbox_settings
from .runner import case_row, check_run, health_check, load_scenarios, start_case, update_summary
from .scenarios import BY_KEY, M_B2B_RESULT, M_B2C_RESULT, M_CONFIRMATION, M_STK_CALLBACK

M_REVERSAL_RESULT = "frappe_mpsa_payments.money_out.api.reversal_result"


def _run(name: str):
    run = frappe.get_doc(RUN, name)
    run.check_permission("write")
    require_lab(run)
    return run


@frappe.whitelist(methods=["POST"])
def add_scenarios(run: str) -> int:
    doc = _run(run)
    added = load_scenarios(doc)
    doc.save()
    update_summary(doc.name)
    return added


@frappe.whitelist(methods=["POST"])
def run_case(run: str, case: str) -> str:
    doc = _run(run)
    status = start_case(doc, case_row(doc, case))
    update_summary(doc.name)
    return status


@frappe.whitelist(methods=["POST"])
def run_ready(run: str) -> dict:
    """Start every Pending automatic case whose prerequisite has passed (guided ones need you
    at the phone, so they are started one by one)."""
    doc = _run(run)
    started = {}
    for case in doc.cases:
        scenario = BY_KEY.get(case.scenario)
        if case.status != PENDING or not scenario or scenario.mode != AUTO:
            continue
        if scenario.depends:
            dep = next((c for c in doc.cases if c.scenario == scenario.depends), None)
            if not dep or frappe.db.get_value(case.doctype, dep.name, "status") != "Passed":
                continue
        started[case.scenario] = start_case(doc, case)
    update_summary(doc.name)
    return started


@frappe.whitelist(methods=["POST"])
def reset_case(run: str, case: str) -> None:
    doc = _run(run)
    row = case_row(doc, case)
    frappe.db.set_value(row.doctype, row.name, {"status": PENDING, "result": "", "state": "{}",
                                                 "started_on": None, "finished_on": None})
    update_summary(doc.name)


@frappe.whitelist(methods=["POST"])
def check_now(run: str) -> None:
    _run(run)
    check_run(run)


@frappe.whitelist(methods=["POST"])
def run_health_check(run: str) -> list:
    return health_check(_run(run))


# ---------------------------------------------------------------- ad-hoc tools
@frappe.whitelist(methods=["POST"])
def simulate_c2b(mpesa_settings: str, shortcode: str, amount: int, bill_ref: str = "",
                 msisdn: str = payloads.DARAJA_TEST_MSISDN, paybill: int = 1) -> dict:
    """Make Daraja's sandbox pretend a customer paid the paybill / till."""
    from ..money_out.common import post

    require_lab()
    require_sandbox_settings(mpesa_settings)
    settings = frappe.get_doc("Mpesa Settings", mpesa_settings)
    body = payloads.c2b_simulate(shortcode, cint(amount), msisdn, bill_ref, bool(cint(paybill)))
    answers = []
    for endpoint in (payloads.C2B_SIMULATE_V2, payloads.C2B_SIMULATE_V1):
        data, error = post(settings, endpoint, body, doctype="Mpesa Settings", name=mpesa_settings,
                           description="Mpesa C2B Simulate (sandbox lab)")
        if error:
            frappe.throw(f"Could not reach Daraja ({error})")
        answers.append(data)
        if is_zero_code(data.get("ResponseCode")) or "accept" in str(data.get("ResponseDescription", "")).lower():
            return data
    frappe.throw(f"Safaricom refused the simulated payment: {answers}")


REPLAYABLE = ("Mpesa Express Request", "Mpesa C2B Payment Register", "B2C Disbursement Request",
              "Mpesa B2B Payment Request", "Mpesa Reversal Request")


@frappe.whitelist(methods=["POST"])
def replay_callback(doctype: str, name: str, with_token: int = 1) -> dict:
    """Post the callback Safaricom sent for this document to our own URL again, as Safaricom would
    when it re-sends. With ``with_token=0`` it shows what a forger without the token gets."""
    require_lab()
    if doctype not in REPLAYABLE:
        frappe.throw(f"Callbacks of {doctype} cannot be replayed. Use one of: {', '.join(REPLAYABLE)}")
    frappe.get_doc(doctype, name).check_permission("read")
    method, payload, params = _stored_callback(doctype, name)
    status, answer = post_callback(method, payload, token=bool(cint(with_token)), **params)
    return {"http_status": status, "answer": answer, "endpoint": method}


def _stored_callback(doctype: str, name: str):
    def logged(description):
        rows = frappe.get_all("Integration Request", filters={"reference_docname": name,
                                                              "request_description": description},
                              fields=["data"], order_by="creation asc", limit=1)
        if not rows:
            frappe.throw(f"No stored '{description}' for {name}: nothing to replay yet")
        return json.loads(rows[0].data)

    if doctype == "Mpesa Express Request":
        stored = frappe.db.get_value(doctype, name, "callback_payload")
        if not stored:
            frappe.throw(f"{name} has no STK callback yet")
        return M_STK_CALLBACK, payloads.stk_body(json.loads(stored)), {}
    if doctype == "Mpesa C2B Payment Register":
        row = frappe.db.get_value(doctype, name, "*", as_dict=True)
        return M_CONFIRMATION, payloads.confirmation_from_register(row), {}
    if doctype == "B2C Disbursement Request":
        return M_B2C_RESULT, {"Result": logged("Mpesa B2C Callback")}, {"provider": "Mpesa"}
    if doctype == "Mpesa B2B Payment Request":
        return M_B2B_RESULT, logged("Mpesa B2B Result"), {"ref": name}
    return M_REVERSAL_RESULT, logged("Mpesa Reversal Result"), {"ref": name}


# ---------------------------------------------------------------- health-check target
@frappe.whitelist(allow_guest=True, methods=["POST"])
@safaricom_callback(KIND_RESULT)
def ping(**kwargs):
    """Answers the lab's own reachability check through the same door Safaricom uses."""
    if not lab_enabled():
        return rejected_response()
    return {"ResultCode": 0, "ResultDesc": "Accepted", "lab": "pong"}
