"""The lab's test scenarios: every M-Pesa flow, the happy path and the ways it can go wrong.

Each scenario has a ``start`` (do the thing: call Daraja's sandbox, or replay / forge a callback
against our own URL) and a ``check`` (read what the app did and decide Passed / Failed / still
Waiting). Checks run every minute from the scheduler until they pass, fail or time out.
"""

import json

import frappe
from frappe.utils import cint, flt, now_datetime

from ..daraja.guard import is_zero_code
from . import payloads
from .framework import (
    AUTO,
    FAILED,
    GUIDED,
    PASSED,
    RUN,
    WAITING,
    Fail,
    Scenario,
    Skip,
    post_callback,
    rejected,
)

SUITE = "M-Pesa Suite Settings"
REGISTER = "Mpesa C2B Payment Register"
EXPRESS = "Mpesa Express Request"
B2C_REQUEST = "B2C Disbursement Request"
B2B = "Mpesa B2B Payment Request"
REVERSAL = "Mpesa Reversal Request"
IR = "Integration Request"
HELD_SERVICE = "Mpesa C2B Verification"

M_CONFIRMATION = "mpsa_suite.mpsa_suite.api.m_pesa_api.confirmation"
M_STK_CALLBACK = "mpsa_suite.mpsa_suite.api.m_pesa_api.stk_push_callback"
M_B2C_RESULT = "mpsa_suite.mpsa_suite.api.b2c.b2c_results_callback"
M_B2B_RESULT = "mpsa_suite.money_out.api.b2b_result"

SETTLE_MINUTES = 3  # how long a replay / forgery is watched before declaring nothing happened


# ---------------------------------------------------------------- helpers
def switch(fieldname: str) -> int:
    return cint(frappe.db.get_single_value(SUITE, fieldname))


def no_ip_allowlist() -> None:
    if switch("enforce_ip_allowlist"):
        raise Skip(
            "Only Accept Callbacks From Allowed IPs is on, so the lab's own posts (from this server, "
            "not Safaricom) are rejected by design. Turn it off while running replay tests."
        )


def settings(ctx, fieldname: str, label: str):
    return frappe.get_doc("Mpesa Settings", ctx.need(fieldname, label))


def amount(ctx) -> int:
    value = cint(ctx.run.amount) or 10
    if value <= 0:
        raise Skip("Set a positive Test Amount on the test run")
    return value


def test_invoice(ctx, times: int = 1) -> str:
    name = ctx.need("test_sales_invoice", "Test Sales Invoice")
    row = frappe.db.get_value("Sales Invoice", name, ["docstatus", "outstanding_amount"], as_dict=True)
    if not row or row.docstatus != 1:
        raise Skip(f"Test Sales Invoice {name} must be submitted")
    if flt(row.outstanding_amount) < amount(ctx) * times:
        raise Skip(f"Test Sales Invoice {name} has only {row.outstanding_amount} outstanding; use a bigger one")
    return name


def payment_entries(receipt: str) -> list:
    if not receipt:
        return []
    return frappe.get_all(
        "Payment Entry", filters={"reference_no": receipt, "docstatus": ["<", 2]}, pluck="name"
    )


def live_registers(transid: str) -> list:
    return frappe.get_all(
        REGISTER, filters={"transid": transid, "docstatus": ["<", 2]}, fields=["name", "docstatus"]
    )


def register_entries(register) -> set:
    """Payment Entries booked for a register, however the booking recorded it."""
    names = set(payment_entries(register.transid))
    if frappe.get_meta(REGISTER).has_field("payment_entry") and register.get("payment_entry"):
        names.add(register.payment_entry)
    names.update(
        n for n in frappe.get_all(
            "Mpesa Match Log", filters={"register": register.name, "payment_entry": ["is", "set"]},
            pluck="payment_entry",
        ) if n and frappe.db.get_value("Payment Entry", n, "docstatus") in (0, 1)
    )
    return names


def last_match_log(register_name: str):
    rows = frappe.get_all(
        "Mpesa Match Log", filters={"register": register_name},
        fields=["result", "method", "details", "reference_name"], order_by="creation desc", limit=1,
    )
    return rows[0] if rows else None


def held_confirmation(since, bill_ref: str | None = None, transid: str | None = None):
    filters = {"integration_request_service": HELD_SERVICE, "creation": [">=", since]}
    if transid:
        filters["request_id"] = transid
    elif bill_ref:
        filters["data"] = ["like", f'%"billrefnumber": "{bill_ref}"%']
        # The forged-receipt test uses the same invoice number; never mistake its ZZ receipt for ours.
        filters["request_id"] = ["not like", f"{payloads.FAKE_RECEIPT_PREFIX}%"]
    rows = frappe.get_all(IR, filters=filters, fields=["name", "status", "error", "request_id"],
                          order_by="creation desc", limit=1)
    return rows[0] if rows else None


def c2b_shortcode(ctx) -> str:
    s = settings(ctx, "c2b_settings", "C2B Mpesa Settings")
    code = ctx.run.c2b_shortcode or s.till_number or s.business_shortcode
    if not code:
        raise Skip("Set C2B Shortcode on the test run (the 600xxx number from Daraja's Test Credentials)")
    return str(code)


# ---------------------------------------------------------------- health / balance
def start_health(ctx):
    from .runner import health_check

    report = health_check(ctx.run)
    problems = [r["label"] + ": " + r["detail"] for r in report if r["level"] == "error"]
    ctx.save(report=report)
    if problems:
        ctx.done(FAILED, "; ".join(problems))
    else:
        warnings = [r["label"] for r in report if r["level"] == "warning"]
        ctx.done(PASSED, "All checks passed" + (f" (warnings: {', '.join(warnings)})" if warnings else ""))


def check_decided(ctx):
    return WAITING, "Not started"


def start_balance(ctx):
    from ..mpsa_suite.api.m_pesa_api import get_account_balance

    name = ctx.run.b2c_settings or ctx.need("c2b_settings", "C2B Mpesa Settings")
    before = frappe.db.get_value("Mpesa Settings", name, ["modified", "account_balance"], as_dict=True)
    ctx.save(settings=name, since=now_datetime(), modified_before=before.modified)
    get_account_balance(name)
    ctx.set_reference("Mpesa Settings", name)


def check_balance(ctx):
    row = frappe.db.get_value("Mpesa Settings", ctx.state["settings"], ["modified", "account_balance"], as_dict=True)
    if str(row.modified) > str(ctx.state["modified_before"]) and row.account_balance:
        return PASSED, f"Balance received: {str(row.account_balance)[:200]}"
    errors = frappe.get_all("Error Log", filters={"creation": [">=", ctx.state["since"]],
                                                  "method": ["like", "%Balance%"]}, pluck="method", limit=1)
    if errors:
        return FAILED, f"Error Log: {errors[0]}"
    return WAITING, "Waiting for Safaricom's balance result"


# ---------------------------------------------------------------- C2B
def simulate_c2b(ctx, bill_ref: str) -> None:
    from ..money_out.common import post

    s = settings(ctx, "c2b_settings", "C2B Mpesa Settings")
    paybill = (ctx.run.c2b_type or "Pay Bill") == "Pay Bill"
    body = payloads.c2b_simulate(c2b_shortcode(ctx), amount(ctx), ctx.run.test_msisdn or payloads.DARAJA_TEST_MSISDN,
                                 bill_ref, paybill)
    answers = []
    for endpoint in (payloads.C2B_SIMULATE_V2, payloads.C2B_SIMULATE_V1):
        data, error = post(s, endpoint, body, doctype=RUN, name=ctx.run.name,
                           description="Mpesa C2B Simulate (sandbox lab)")
        if error:
            raise Fail(f"Could not reach Daraja ({error})")
        if is_zero_code(data.get("ResponseCode")) or "accept" in str(data.get("ResponseDescription", "")).lower():
            ctx.save(bill_ref=bill_ref, since=now_datetime(), amount=amount(ctx), simulate_answer=data)
            return
        answers.append(data)
    raise Fail(f"Safaricom refused the simulated payment: {answers}")


def c2b_arrival(ctx):
    """(register, waiting-or-failure message) for the simulated payment."""
    since, ref = ctx.state["since"], ctx.state["bill_ref"]
    rows = frappe.get_all(
        REGISTER,
        filters={"billrefnumber": ref, "creation": [">=", since], "docstatus": ["<", 2],
                 "transid": ["not like", f"{payloads.FAKE_RECEIPT_PREFIX}%"]},  # not the lab's forgeries
        fields=["*"], order_by="creation asc",
    )
    rows = [r for r in rows if abs(flt(r.transamount) - flt(ctx.state["amount"])) < 0.01]
    if rows:
        if len(rows) > 1:
            return rows[0], f"FAIL:{len(rows)} registers for one payment: {', '.join(r.name for r in rows)}"
        return rows[0], ""
    held = held_confirmation(since, bill_ref=ref)
    if held and held.status == "Failed":
        return None, f"FAIL:Confirmation {held.name} failed verification: {held.error}"
    if held:
        return None, f"Confirmation {held.name} received, held until Transaction Status confirms it ({held.status})"
    return None, "Waiting for Safaricom's C2B confirmation (check the C2B URL registration if this never arrives)"


def check_booked_to_invoice(ctx, reg, invoice):
    if ctx.case.reference_name != reg.name:
        ctx.set_reference(REGISTER, reg.name)
    ctx.save(transid=reg.transid, register=reg.name)
    entries = register_entries(reg)
    if len(entries) > 1:
        return FAILED, f"Receipt {reg.transid} booked twice: {', '.join(sorted(entries))}"
    if not switch("enable_matching"):
        return PASSED, f"Received as {reg.name} ({reg.transid}). Match and Book Receipts is off, so not booked."
    if entries:
        pe = next(iter(entries))
        if frappe.db.exists("Payment Entry Reference", {"parent": pe, "reference_name": invoice}):
            return PASSED, f"{reg.transid} received ({reg.name}) and booked by {pe} against {invoice}"
        return FAILED, f"{reg.transid} was booked by {pe}, but not against {invoice}"
    if reg.matched_reference_name and reg.matched_reference_name != invoice:
        return FAILED, f"Matched to {reg.matched_reference_name}, expected {invoice}"
    log = last_match_log(reg.name)
    if log and log.result in ("Error", "Ambiguous"):
        return FAILED, f"Match Log {log.result}: {log.details or ''}"[:500]
    return WAITING, f"Received as {reg.name}; waiting for matching and booking (runs every 2 minutes)"


def start_c2b_exact(ctx):
    simulate_c2b(ctx, test_invoice(ctx, times=3))


def check_c2b_exact(ctx):
    reg, msg = c2b_arrival(ctx)
    if msg.startswith("FAIL:"):
        return FAILED, msg[5:]
    if not reg:
        return WAITING, msg
    return check_booked_to_invoice(ctx, reg, ctx.run.test_sales_invoice)


def start_c2b_typo(ctx):
    simulate_c2b(ctx, payloads.typo_variant(test_invoice(ctx, times=3)))


def check_c2b_typo(ctx):
    reg, msg = c2b_arrival(ctx)
    if msg.startswith("FAIL:"):
        return FAILED, msg[5:]
    if not reg:
        return WAITING, msg
    if not switch("match_invoice_typos") and switch("enable_matching"):
        return PASSED, f"Received as {reg.name}; Repair Mistyped Invoice Numbers is off, so not matched."
    return check_booked_to_invoice(ctx, reg, ctx.run.test_sales_invoice)


def start_c2b_unknown(ctx):
    simulate_c2b(ctx, payloads.lab_reference("U"))


def check_c2b_unknown(ctx):
    reg, msg = c2b_arrival(ctx)
    if msg.startswith("FAIL:"):
        return FAILED, msg[5:]
    if not reg:
        return WAITING, msg
    ctx.set_reference(REGISTER, reg.name)
    entries = register_entries(reg)
    if entries:
        return FAILED, f"A payment with an unknown reference was booked: {', '.join(sorted(entries))}"
    if reg.matched_reference_name:
        return FAILED, f"Unknown reference was matched to {reg.matched_reference_name}"
    if not ctx.state.get("arrived"):
        ctx.save(arrived=now_datetime())
    if ctx.minutes_since("arrived") < SETTLE_MINUTES:
        return WAITING, f"Received as {reg.name}; watching that nothing books it"
    return PASSED, f"Received as {reg.name} and left for staff (not booked, not matched)"


def start_c2b_duplicate(ctx):
    no_ip_allowlist()
    _, state, _ = ctx.other("c2b_exact")
    reg_name = state.get("register")
    reg = frappe.get_doc(REGISTER, reg_name)
    ctx.set_reference(REGISTER, reg_name)
    status, answer = post_callback(M_CONFIRMATION, payloads.confirmation_from_register(reg.as_dict()))
    ctx.save(transid=reg.transid, since=now_datetime(), entries_before=sorted(register_entries(reg)), answer=answer)
    if rejected(answer):
        raise Fail(f"Our own confirmation URL rejected the replay: {answer}")


def check_c2b_duplicate(ctx):
    transid = ctx.state["transid"]
    regs = live_registers(transid)
    if len(regs) > 1:
        return FAILED, f"Replayed confirmation created a second register: {', '.join(r.name for r in regs)}"
    entries = sorted(payment_entries(transid))
    if len(entries) > max(1, len(ctx.state.get("entries_before") or [])):
        return FAILED, f"Replayed confirmation booked the receipt again: {', '.join(entries)}"
    if ctx.minutes_since() < SETTLE_MINUTES:
        return WAITING, "Watching for a second register or Payment Entry"
    return PASSED, f"Safaricom re-sending {transid} changed nothing (one register, {len(entries)} Payment Entry)"


def forged_confirmation(ctx, token: bool, shortcode: str | None = None):
    receipt = payloads.fake_receipt()
    body = payloads.c2b_confirmation(
        receipt, amount(ctx), shortcode or c2b_shortcode(ctx),
        bill_ref=ctx.run.test_sales_invoice or payloads.lab_reference("F"),
        msisdn=ctx.run.test_msisdn or payloads.DARAJA_TEST_MSISDN,
    )
    status, answer = post_callback(M_CONFIRMATION, body, token=token)
    ctx.save(transid=receipt, since=now_datetime(), answer=answer, http_status=status)
    return answer


def check_no_receipt(ctx, label: str):
    transid = ctx.state["transid"]
    regs = live_registers(transid)
    if regs:
        return FAILED, f"{label} became a receipt: {', '.join(r.name for r in regs)} ({transid}). Turn on the verification switches."
    if payment_entries(transid):
        return FAILED, f"{label} was booked: {', '.join(payment_entries(transid))}"
    if ctx.minutes_since() < SETTLE_MINUTES:
        held = held_confirmation(ctx.state["since"], transid=transid)
        return WAITING, f"Watching {transid}" + (f" (held in {held.name}, {held.status})" if held else "")
    held = held_confirmation(ctx.state["since"], transid=transid)
    return PASSED, f"{label} {transid} never became a receipt" + (
        f"; held confirmation {held.name} is {held.status}" + (f": {held.error}" if held.error else "") if held else ""
    )


def start_c2b_forged_no_token(ctx):
    no_ip_allowlist()
    answer = forged_confirmation(ctx, token=False)
    if rejected(answer):
        ctx.done(PASSED, "Forged confirmation without the callback token was rejected at the door")
    elif not switch("enforce_token_c2b"):
        ctx.save(warning="Require Token on C2B is off: forged confirmations get in and only verification stops them")


def check_c2b_forged_no_token(ctx):
    status, msg = check_no_receipt(ctx, "Forged confirmation (no token)")
    if status == PASSED and ctx.state.get("warning"):
        msg += ". WARNING: " + ctx.state["warning"]
    return status, msg


def start_c2b_forged_receipt(ctx):
    no_ip_allowlist()
    if not switch("verify_c2b_confirmations"):
        ctx.save(warning="Verify C2B Confirmations is off")
    answer = forged_confirmation(ctx, token=True)
    if rejected(answer):
        raise Fail(f"Our own confirmation URL rejected a correctly signed post: {answer}")


def check_c2b_forged_receipt(ctx):
    return check_no_receipt(ctx, "Confirmation with a receipt Safaricom never issued")


def start_c2b_wrong_shortcode(ctx):
    no_ip_allowlist()
    forged_confirmation(ctx, token=True, shortcode="999999")


def check_c2b_wrong_shortcode(ctx):
    return check_no_receipt(ctx, "Confirmation for a shortcode that is not ours")


# ---------------------------------------------------------------- STK
def send_stk(ctx) -> str:
    from ..mpsa_suite.doctype.mpesa_express_request.mpesa_express_request import create_new_request

    s = settings(ctx, "stk_settings", "STK Mpesa Settings")
    phone = ctx.need("tester_phone", "Your Phone (for STK prompts)")
    gateway = frappe.db.get_value("Payment Gateway", {"gateway_controller": s.name}, "name")
    if not gateway:
        raise Skip(f"Mpesa Settings {s.name} has no Payment Gateway yet: set its Company and save it")
    invoice = test_invoice(ctx, times=2)
    name = create_new_request(phone, gateway, "Sales Invoice", invoice, amount(ctx),
                              title="Sandbox lab", description=ctx.case.scenario)
    ctx.set_reference(EXPRESS, name)
    req = frappe.db.get_value(EXPRESS, name, ["checkout_request_id", "merchant_request_id", "status",
                                              "response_description", "result_desc"], as_dict=True)
    ctx.save(request=name, since=now_datetime(), checkout_request_id=req.checkout_request_id,
             merchant_request_id=req.merchant_request_id)
    if not req.checkout_request_id:
        raise Fail(f"Safaricom did not accept the prompt: {req.response_description or req.result_desc or req.status}")
    return name


def stk_row(name):
    return frappe.db.get_value(
        EXPRESS, name,
        ["status", "verification_status", "transaction_id", "result_code", "result_desc", "is_reconciled", "amount"],
        as_dict=True,
    )


def start_stk_pay(ctx):
    send_stk(ctx)


def check_stk_pay(ctx):
    req = stk_row(ctx.state["request"])
    if req.status == "Failed":
        return FAILED, f"Prompt failed ({req.result_code}): {req.result_desc}. Did you enter the PIN?"
    if req.verification_status == "Mismatch":
        return FAILED, f"Genuine payment rejected by verification: {req.result_desc}"
    if req.status != "Completed":
        return WAITING, "Waiting for you to enter your M-Pesa PIN, then for Safaricom's callback"
    ctx.save(receipt=req.transaction_id)
    entries = payment_entries(req.transaction_id)
    if len(entries) > 1:
        return FAILED, f"Paid once, booked {len(entries)} times: {', '.join(entries)}"
    if req.is_reconciled or entries:
        return PASSED, f"Paid {req.amount} ({req.transaction_id}), verified and booked" + (f" by {entries[0]}" if entries else "")
    if ctx.minutes_since() > 6:
        return FAILED, f"Paid ({req.transaction_id}) but not booked after 6 minutes: see Error Log 'M-Pesa STK booking failed'"
    return WAITING, f"Paid ({req.transaction_id}); waiting for booking"


def start_stk_cancel(ctx):
    send_stk(ctx)


def check_stk_cancelled(ctx, label="Prompt"):
    req = stk_row(ctx.state["request"])
    if req.status == "Completed":
        receipt = req.transaction_id
        if ctx.state.get("forged_receipt") and receipt == ctx.state["forged_receipt"]:
            return FAILED, f"CRITICAL: the forged receipt {receipt} was accepted as a real payment"
        return FAILED, f"{label} was paid ({receipt}); press Cancel on the phone for this test"
    if req.status == "Failed":
        forged = ctx.state.get("forged_receipt")
        if forged and payment_entries(forged):
            return FAILED, f"CRITICAL: forged receipt {forged} was booked"
        if req.is_reconciled:
            return FAILED, "A cancelled prompt was marked reconciled (booked)"
        return PASSED, f"{label} failed as expected ({req.result_code}: {req.result_desc}); nothing booked"
    if req.verification_status == "Mismatch" and ctx.state.get("forged_receipt"):
        return PASSED, "Forged success callback rejected: STK Query disagreed with it; nothing booked"
    return WAITING, "Waiting for you to press Cancel on the prompt, then for Safaricom's callback"


def check_stk_cancel(ctx):
    return check_stk_cancelled(ctx)


def start_stk_forged(ctx):
    no_ip_allowlist()
    send_stk(ctx)
    receipt = payloads.fake_receipt()
    body = payloads.stk_callback(ctx.state["checkout_request_id"], ctx.state.get("merchant_request_id"),
                                 0, amount(ctx), receipt, ctx.run.tester_phone)
    status, answer = post_callback(M_STK_CALLBACK, body)
    ctx.save(forged_receipt=receipt, answer=answer)


def check_stk_forged(ctx):
    return check_stk_cancelled(ctx, "Prompt with a forged success callback")


def start_stk_duplicate(ctx):
    no_ip_allowlist()
    _, state, _ = ctx.other("stk_pay")
    name = state.get("request")
    payload = frappe.db.get_value(EXPRESS, name, "callback_payload")
    if not payload:
        raise Fail(f"{name} has no stored callback to replay")
    ctx.set_reference(EXPRESS, name)
    ctx.save(request=name, receipt=state.get("receipt"), since=now_datetime(),
             entries_before=payment_entries(state.get("receipt")))
    status, answer = post_callback(M_STK_CALLBACK, payloads.stk_body(json.loads(payload)))
    ctx.save(answer=answer)


def check_stk_duplicate(ctx):
    req = stk_row(ctx.state["request"])
    entries = payment_entries(ctx.state.get("receipt"))
    if len(entries) > max(1, len(ctx.state.get("entries_before") or [])):
        return FAILED, f"Replayed STK callback booked again: {', '.join(entries)}"
    if req.status != "Completed":
        return FAILED, f"Replayed callback changed the request to {req.status}"
    if ctx.minutes_since() < SETTLE_MINUTES:
        return WAITING, "Watching for a second booking"
    return PASSED, "Safaricom re-sending the STK callback changed nothing"


def start_stk_query(ctx):
    from ..mpsa_suite.api.m_pesa_api import check_transaction_status

    _, state, _ = ctx.other("stk_pay")
    name = state.get("request")
    ctx.set_reference(EXPRESS, name)
    ctx.save(request=name, receipt=state.get("receipt"), since=now_datetime(),
             entries_before=payment_entries(state.get("receipt")))
    check_transaction_status(name)


def check_stk_query(ctx):
    req = stk_row(ctx.state["request"])
    entries = payment_entries(ctx.state.get("receipt"))
    if req.status != "Completed":
        return FAILED, f"Checking the status of a paid prompt changed it to {req.status}"
    if len(entries) > max(1, len(ctx.state.get("entries_before") or [])):
        return FAILED, f"Checking the status booked the payment again: {', '.join(entries)}"
    if ctx.minutes_since() < 1:
        return WAITING, "Watching for side effects"
    return PASSED, "Status check of a paid prompt left it Completed and booked once"


def start_stk_forged_no_token(ctx):
    no_ip_allowlist()
    body = payloads.stk_callback("ws_CO_LAB" + payloads.fake_receipt(), "LAB", 0, amount(ctx))
    status, answer = post_callback(M_STK_CALLBACK, body, token=False)
    ctx.save(answer=answer, http_status=status)
    if rejected(answer):
        ctx.done(PASSED, "STK callback without the token was rejected at the door")
    elif switch("enforce_token_results"):
        ctx.done(FAILED, f"Token enforcement is on but the callback was not rejected: {answer}")
    else:
        ctx.done(PASSED, "Unknown prompt ignored. WARNING: Require Token on STK / B2C callbacks is off")


# ---------------------------------------------------------------- B2C
def b2c_requests(disbursement: str) -> list:
    return frappe.get_all(
        B2C_REQUEST, filters={"b2c_payment_disbursement": disbursement, "docstatus": 1},
        fields=["name", "status", "transaction_id", "result_desc", "error_message", "accounting_booked",
                "accounting_error", "b2c_payment_disbursement_reference"],
    )


def b2c_entries(disbursement: str) -> int:
    pe = frappe.db.count("Payment Entry Reference", {"b2c_payment_disbursement": disbursement, "docstatus": ["<", 2]})
    je = frappe.db.count("Journal Entry Account", {"b2c_payment_disbursement": disbursement, "docstatus": ["<", 2]})
    return pe + je


def start_b2c_paid(ctx):
    settings(ctx, "b2c_settings", "B2C Mpesa Settings")
    name = ctx.need("test_b2c_disbursement", "Test B2C Payment Disbursement")
    doc = frappe.get_doc("B2C Payment Disbursement", name)
    ctx.set_reference(doc.doctype, name)
    if doc.docstatus == 0:
        doc.submit()
    elif doc.docstatus == 2:
        raise Skip(f"{name} is cancelled: use a new test disbursement")
    ctx.save(disbursement=name, since=now_datetime())


def check_b2c_paid(ctx):
    rows = b2c_requests(ctx.state["disbursement"])
    if not rows:
        return WAITING, "Waiting for the payout requests to be created and sent"
    failed = [r for r in rows if r.status == "Failed"]
    if failed:
        return FAILED, "; ".join(f"{r.name}: {r.result_desc or r.error_message}" for r in failed)[:500]
    unknown = [r.name for r in rows if r.status == "Status Unknown"]
    if unknown:
        return FAILED, f"No answer from M-Pesa for {', '.join(unknown)} (Status Unknown): check B2C status before anything else"
    if any(r.status != "Paid" for r in rows):
        return WAITING, "Waiting for Safaricom's B2C result: " + ", ".join(f"{r.name} {r.status}" for r in rows)
    not_booked = [r for r in rows if not r.accounting_booked]
    errors = [f"{r.name}: {r.accounting_error}" for r in not_booked if r.accounting_error]
    if errors:
        return FAILED, "Paid but accounting failed: " + "; ".join(errors)[:500]
    if not_booked:
        return WAITING, "Paid; waiting for the Payment Entry / Journal Entry"
    ctx.save(request=rows[0].name)
    return PASSED, f"{len(rows)} payout(s) paid ({', '.join(r.transaction_id or '' for r in rows)}) and booked once"


def start_b2c_replay(ctx):
    no_ip_allowlist()
    _, state, _ = ctx.other("b2c_paid")
    request = state.get("request")
    log = frappe.get_all(IR, filters={"reference_docname": request, "request_description": "Mpesa B2C Callback"},
                         fields=["name", "data"], order_by="creation asc", limit=1)
    if not log:
        raise Fail(f"No stored B2C result for {request} to replay")
    ctx.set_reference(B2C_REQUEST, request)
    ctx.save(disbursement=state.get("disbursement"), request=request, since=now_datetime(),
             entries_before=b2c_entries(state.get("disbursement")))
    status, answer = post_callback(M_B2C_RESULT, {"Result": json.loads(log[0].data)}, provider="Mpesa")
    ctx.save(answer=answer)


def check_b2c_replay(ctx):
    count = b2c_entries(ctx.state["disbursement"])
    if count > cint(ctx.state.get("entries_before")):
        return FAILED, f"Replayed B2C result booked again ({count} entries, was {ctx.state['entries_before']})"
    if frappe.db.get_value(B2C_REQUEST, ctx.state["request"], "status") != "Paid":
        return FAILED, "Replayed result changed the payout's status"
    if ctx.minutes_since() < SETTLE_MINUTES:
        return WAITING, "Watching for a second booking"
    return PASSED, "Safaricom re-sending the B2C result changed nothing"


def start_b2c_status_guard(ctx):
    from ..money_out.b2c_status import start_query

    _, state, _ = ctx.other("b2c_paid")
    request = state.get("request")
    ctx.set_reference(B2C_REQUEST, request)
    try:
        query = start_query(request)
    except frappe.ValidationError as e:
        ctx.done(PASSED, f"Refused to re-query a settled payout: {e}")
        return
    ctx.done(FAILED, f"A status query ({query}) was sent for a payout that is already Paid")


# ---------------------------------------------------------------- B2B
def start_b2b_blocked(ctx):
    settings(ctx, "b2b_settings", "B2B Mpesa Settings")
    if switch("allow_one_off_b2b"):
        raise Skip("Allow B2B to Numbers Not Saved on a Supplier is on")
    doc = frappe.get_doc({"doctype": B2B, "mpesa_settings": ctx.run.b2b_settings, "command_id": "BusinessPayBill",
                          "receiver_shortcode": "600000", "account_reference": "LAB", "amount": amount(ctx),
                          "remarks": "Sandbox lab: must be refused"})
    savepoint = "mpesa_lab_b2b_blocked"
    frappe.db.savepoint(savepoint)
    try:
        doc.insert()
    except frappe.ValidationError as e:
        frappe.db.rollback(save_point=savepoint)
        ctx.done(PASSED, f"A typed paybill without an approved supplier account was refused: {e}"[:500])
        return
    frappe.db.rollback(save_point=savepoint)
    ctx.done(FAILED, "A B2B payment to a typed paybill (no approved supplier account) was accepted")


def start_b2b_pay(ctx):
    settings(ctx, "b2b_settings", "B2B Mpesa Settings")
    supplier = ctx.need("test_supplier", "Test Supplier")
    # The lab pays its own test supplier again on every run: its earlier lab payouts (supplier
    # advances) are known not to have paid the test invoice.
    doc = frappe.get_doc({"doctype": B2B, "mpesa_settings": ctx.run.b2b_settings, "supplier": supplier,
                          "purchase_invoice": ctx.run.test_purchase_invoice or None, "amount": amount(ctx),
                          "remarks": "Sandbox lab", "checked_earlier_payouts": 1})
    try:
        doc.insert()
    except frappe.ValidationError as e:
        raise Skip(f"Could not create the B2B payment: {e}. Give {supplier} an approved Supplier M-Pesa Account "
                   "(Party B 600000 from Test Credentials) and set the hold to 0 hours for the lab.")
    try:
        doc.submit()  # every check runs again here, before anything is sent
    except frappe.ValidationError as e:
        raise Skip(f"Could not send the B2B payment: {e}")
    ctx.set_reference(B2B, doc.name)
    ctx.save(request=doc.name, since=now_datetime())


def check_b2b_pay(ctx):
    row = frappe.db.get_value(B2B, ctx.state["request"], ["status", "result_desc", "error_message", "transaction_id",
                                                          "payment_entry", "journal_entry", "entry_status",
                                                          "entry_note", "receiver_public_name",
                                                          "receiver_name_mismatch"], as_dict=True)
    if row.status in ("Failed", "Unknown"):
        return FAILED, f"{row.status}: {row.result_desc or row.error_message}"
    if row.status != "Paid":
        return WAITING, f"Waiting for Safaricom's B2B result ({row.status})"
    ctx.save(receipt=row.transaction_id)
    note = f"; M-Pesa paid '{row.receiver_public_name}'" + (" (name did not match the account)" if row.receiver_name_mismatch else "")
    entry = row.payment_entry or row.journal_entry
    if row.entry_status in ("Entry Made", "Draft Entry") and entry:
        return PASSED, f"Paid ({row.transaction_id}), booked by {entry} ({row.entry_status})" + note
    if row.entry_status == "Needs Checking":
        return FAILED, f"Paid ({row.transaction_id}) but the entry needs checking: {row.entry_note}" + note
    # the lab books automatically, so no entry yet means the booking job is late or failed
    if ctx.minutes_since() > 6:
        return FAILED, f"Paid ({row.transaction_id}) but no accounting entry after 6 minutes: {row.entry_note or ''}" + note
    return WAITING, "Paid; waiting for the accounting entry"


def start_b2b_replay(ctx):
    no_ip_allowlist()
    _, state, _ = ctx.other("b2b_pay")
    request = state.get("request")
    log = frappe.get_all(IR, filters={"reference_docname": request, "request_description": "Mpesa B2B Result"},
                         fields=["data"], order_by="creation asc", limit=1)
    if not log:
        raise Fail(f"No stored B2B result for {request} to replay")
    ctx.set_reference(B2B, request)
    ctx.save(request=request, receipt=state.get("receipt"), since=now_datetime(),
             entries_before=payment_entries(state.get("receipt")))
    status, answer = post_callback(M_B2B_RESULT, json.loads(log[0].data), ref=request)
    ctx.save(answer=answer)


def check_b2b_replay(ctx):
    entries = payment_entries(ctx.state.get("receipt"))
    if len(entries) > max(1, len(ctx.state.get("entries_before") or [])):
        return FAILED, f"Replayed B2B result booked again: {', '.join(entries)}"
    if ctx.minutes_since() < SETTLE_MINUTES:
        return WAITING, "Watching for a second booking"
    return PASSED, "Safaricom re-sending the B2B result changed nothing"


# ---------------------------------------------------------------- reversal
def start_reversal(ctx):
    from ..money_out.api import make_reversal

    _, state, _ = ctx.other("c2b_exact")
    name = make_reversal(state.get("register"), "Sandbox lab reversal test")
    doc = frappe.get_doc(REVERSAL, name)
    doc.submit()
    ctx.set_reference(REVERSAL, name)
    ctx.save(request=name, since=now_datetime())


def check_reversal(ctx):
    row = frappe.db.get_value(REVERSAL, ctx.state["request"], ["status", "result_desc", "error_message"], as_dict=True)
    if row.status in ("Reversed", "Already Reversed"):
        return PASSED, f"{row.status}: {row.result_desc or ''}"
    if row.status in ("Failed", "Unknown"):
        return FAILED, f"{row.status}: {row.result_desc or row.error_message}"
    return WAITING, f"Waiting for Safaricom's reversal result ({row.status})"


# ---------------------------------------------------------------- catalogue
SCENARIOS = [
    Scenario("health", "1. Health", "Setup and connectivity check", start_health, check_decided,
             request_context=True,
             expectations=["Daraja token", "initiator credentials", "public callback URL reachable", "C2B URLs registered"]),
    Scenario("balance", "1. Health", "Account Balance query (proves the initiator and Security Credential)",
             start_balance, check_balance, request_context=True, timeout_minutes=5),
    Scenario("c2b_exact", "2. C2B", "Customer pays the paybill with the invoice number",
             start_c2b_exact, check_c2b_exact, needs=("c2b_settings", "test_sales_invoice"), timeout_minutes=12,
             expectations=["confirmation verified", "one register", "booked to the invoice"]),
    Scenario("c2b_typo", "2. C2B", "Customer mistypes the invoice number (no dashes, lower case)",
             start_c2b_typo, check_c2b_typo, needs=("c2b_settings", "test_sales_invoice"), timeout_minutes=12),
    Scenario("c2b_unknown", "2. C2B", "Customer uses an account number nobody knows",
             start_c2b_unknown, check_c2b_unknown, needs=("c2b_settings",), timeout_minutes=12),
    Scenario("c2b_duplicate", "2. C2B", "Safaricom sends the same confirmation twice",
             start_c2b_duplicate, check_c2b_duplicate, depends="c2b_exact", timeout_minutes=8),
    Scenario("c2b_forged_no_token", "2. C2B", "Forged confirmation without the callback token",
             start_c2b_forged_no_token, check_c2b_forged_no_token, needs=("c2b_settings",), timeout_minutes=8),
    Scenario("c2b_forged_receipt", "2. C2B", "Forged confirmation with a receipt Safaricom never issued",
             start_c2b_forged_receipt, check_c2b_forged_receipt, needs=("c2b_settings",), timeout_minutes=8),
    Scenario("c2b_wrong_shortcode", "2. C2B", "Confirmation for a shortcode that is not ours",
             start_c2b_wrong_shortcode, check_c2b_wrong_shortcode, needs=("c2b_settings",), timeout_minutes=8),
    Scenario("stk_pay", "3. STK", "STK prompt paid", start_stk_pay, check_stk_pay, mode=GUIDED,
             instructions="A prompt arrives on Your Phone: enter your M-Pesa PIN.",
             needs=("stk_settings", "tester_phone", "test_sales_invoice"), request_context=True, timeout_minutes=10),
    Scenario("stk_cancel", "3. STK", "STK prompt cancelled by the customer", start_stk_cancel, check_stk_cancel,
             mode=GUIDED, instructions="A prompt arrives on Your Phone: press Cancel.",
             needs=("stk_settings", "tester_phone", "test_sales_invoice"), request_context=True, timeout_minutes=8),
    Scenario("stk_forged", "3. STK", "Forged 'paid' callback while the prompt is open", start_stk_forged,
             check_stk_forged, mode=GUIDED,
             instructions="A prompt arrives on Your Phone: press Cancel. The lab sends a fake 'paid' callback meanwhile.",
             needs=("stk_settings", "tester_phone", "test_sales_invoice"), request_context=True, timeout_minutes=10),
    Scenario("stk_duplicate", "3. STK", "Safaricom sends the same STK callback twice", start_stk_duplicate,
             check_stk_duplicate, depends="stk_pay", timeout_minutes=8),
    Scenario("stk_query", "3. STK", "Check Transaction Status on a paid prompt", start_stk_query, check_stk_query,
             depends="stk_pay", request_context=True, timeout_minutes=5),
    Scenario("stk_forged_no_token", "3. STK", "STK callback without the callback token", start_stk_forged_no_token,
             check_decided, timeout_minutes=2),
    Scenario("b2c_paid", "4. B2C", "B2C payout paid and booked", start_b2c_paid, check_b2c_paid,
             needs=("b2c_settings", "test_b2c_disbursement"), timeout_minutes=15,
             instructions="Prepare a B2C Payment Disbursement to the Test MSISDN (draft or submitted)."),
    Scenario("b2c_replay", "4. B2C", "Safaricom sends the same B2C result twice", start_b2c_replay, check_b2c_replay,
             depends="b2c_paid", timeout_minutes=8),
    Scenario("b2c_status_guard", "4. B2C", "Status query refused for a payout that is already Paid",
             start_b2c_status_guard, check_decided, depends="b2c_paid", timeout_minutes=2),
    Scenario("b2b_blocked", "5. B2B", "B2B to a typed paybill is refused", start_b2b_blocked, check_decided,
             needs=("b2b_settings",), timeout_minutes=2),
    Scenario("b2b_pay", "5. B2B", "B2B payment to an approved supplier account", start_b2b_pay, check_b2b_pay,
             needs=("b2b_settings", "test_supplier"), timeout_minutes=12),
    Scenario("b2b_replay", "5. B2B", "Safaricom sends the same B2B result twice", start_b2b_replay, check_b2b_replay,
             depends="b2b_pay", timeout_minutes=8),
    Scenario("reversal", "6. Reversal", "Reverse a received C2B payment", start_reversal, check_reversal,
             depends="c2b_exact", timeout_minutes=15),
]

BY_KEY = {s.key: s for s in SCENARIOS}
assert len(BY_KEY) == len(SCENARIOS), "duplicate scenario key"
__all__ = ["SCENARIOS", "BY_KEY", "AUTO", "GUIDED"]
