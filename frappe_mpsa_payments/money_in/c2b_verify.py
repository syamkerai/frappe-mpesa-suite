"""Hold C2B confirmations until Safaricom's Transaction Status confirms them.

Confirmations are unsigned: anyone who can reach the URL can post one. With the switch
"Verify C2B Confirmations with Transaction Status" on, a confirmation is kept in an
Integration Request (service ``Mpesa C2B Verification``, ``request_id`` = TransID) and a
Transaction Status query is sent for the receipt. The register is created only when the
result names the same receipt, a Completed status, the same amount and one of our
shortcodes. Registers therefore only ever hold confirmed money (or money we pulled from
Safaricom ourselves), so matching, booking, statement lines and the gateway need no change.

A confirmation that cannot be confirmed stays held. ``sweep`` re-asks a few times, the
hourly Pull import brings in real receipts anyway, and an M-Pesa Manager can release a
held confirmation by hand (``release``).
"""

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, get_url, now_datetime

from ..daraja import apis, verify
from ..daraja.guard import daraja_id, is_zero_code
from ..daraja.money_out import result_values, transaction_status_payload
from .common import EXPRESS, REGISTER, named_lock, settings_for_shortcode

SERVICE = "Mpesa C2B Verification"
IR = "Integration Request"
QUEUED, COMPLETED, FAILED = "Queued", "Completed", "Failed"
MAX_ATTEMPTS = 3
RETRY_AFTER_MINUTES = 10
RESULT_METHOD = "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.c2b_verification_result"
TIMEOUT_METHOD = "frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.handle_queue_timeout"
RELEASE_ROLES = ("System Manager", "M-Pesa Manager")


def own_shortcodes(settings_name: str | None) -> set:
    if not settings_name:
        return set()
    row = frappe.db.get_value("Mpesa Settings", settings_name, ["business_shortcode", "till_number"], as_dict=True)
    return {cstr(v).strip() for v in (row or {}).values() if cstr(v).strip()}


def _state(ir) -> dict:
    try:
        return json.loads(ir.output or "{}") or {}
    except ValueError:
        return {}


def _save(ir_name: str, status: str | None = None, error: str | None = None, **state) -> None:
    ir = frappe.get_doc(IR, ir_name)
    values = {"output": json.dumps({**_state(ir), **state}, default=str)}
    if status:
        values["status"] = status
    if error is not None:
        values["error"] = error[:1000]
    frappe.db.set_value(IR, ir_name, values)


# ---------------------------------------------------------------- confirmation -> held
def hold(c2b_data: dict) -> str | None:
    """Keep a confirmation for verification and ask Safaricom about it. Returns the IR name."""
    transid = cstr(c2b_data.get("transid")).strip()
    if not transid:
        frappe.log_error(title="M-Pesa C2B confirmation ignored", message=f"C2B confirmation without TransID: {c2b_data}")
        return None

    with named_lock(f"c2b:{transid}"):
        # any register, cancelled ones too: insert_register would refuse the receipt anyway
        if frappe.db.get_value(REGISTER, {"transid": transid}, "name", for_update=True):
            return None  # already imported (Pull, an earlier confirmation, Transaction Status)
        if frappe.db.get_value(EXPRESS, {"transaction_id": transid}, "name"):
            return None  # an STK prompt carries this receipt and books it
        existing = frappe.db.get_value(IR, {"request_id": transid, "integration_request_service": SERVICE}, "name")
        if existing:
            return existing  # Safaricom re-sent the confirmation

        ir = frappe.get_doc(
            {
                "doctype": IR,
                "integration_request_service": SERVICE,
                "request_description": "C2B confirmation held until Transaction Status confirms it",
                "is_remote_request": 1,
                "request_id": transid,
                "status": QUEUED,
                "data": json.dumps(c2b_data, default=str),
                "output": json.dumps({"attempts": 0}),
            }
        ).insert(ignore_permissions=True)
    frappe.db.commit()  # background job: keep the confirmation before calling Safaricom

    send_query(ir.name)
    return ir.name


def send_query(ir_name: str) -> None:
    """Send (or re-send) the Transaction Status query for a held confirmation."""
    from ..daraja.callbacks import with_token
    from ..money_out.common import initiator_settings, post

    def callback_url(method: str) -> str:
        # Runs in background jobs: utils.build_callback_url reads the request Host header and
        # raises "object is not bound" without a request, so use the site URL (host_name).
        return with_token(f"{get_url()}/api/method/{method}")

    ir = frappe.get_doc(IR, ir_name)
    if ir.status != QUEUED:
        return
    data = json.loads(ir.data)
    state = _state(ir)
    attempts = cint(state.get("attempts")) + 1

    settings_name = settings_for_shortcode(data.get("businessshortcode"))
    if not settings_name:
        _fail(ir_name, f"BusinessShortCode {data.get('businessshortcode')} is not configured in Mpesa Settings.")
        return
    try:
        settings = initiator_settings(settings_name)
    except frappe.ValidationError as e:
        _fail(ir_name, f"Cannot verify: {e}. Release it by hand or let the Pull import bring it in.")
        return

    payload = transaction_status_payload(
        initiator=settings.initiator_name,
        security_credential=settings.security_credential,
        party_a=apis.initiator_party_a(settings),
        transaction_id=data["transid"],
        result_url=callback_url(f"{RESULT_METHOD}?ir={ir_name}"),  # token appended
        queue_timeout_url=callback_url(TIMEOUT_METHOD),
        remarks=f"Verify {data['transid']}",
    )
    ack, error = post(
        settings,
        "/mpesa/transactionstatus/v1/query",
        payload,
        doctype=IR,
        name=ir_name,
        description="Mpesa C2B Verification Query",
    )
    _save(
        ir_name,
        attempts=attempts,
        last_sent=now_datetime(),
        settings=settings_name,
        conversation_id=(ack or {}).get("ConversationID"),
        originator_conversation_id=(ack or {}).get("OriginatorConversationID"),
        last_error=error or (None if is_zero_code((ack or {}).get("ResponseCode")) else cstr((ack or {}).get("errorMessage") or ack)),
    )
    frappe.db.commit()


def _fail(ir_name: str, reason: str) -> None:
    _save(ir_name, status=FAILED, error=reason)
    data = json.loads(frappe.db.get_value(IR, ir_name, "data") or "{}")
    frappe.log_error(
        title=f"M-Pesa C2B receipt {data.get('transid')} not verified"[:140],
        message=f"{reason}\n\nHeld confirmation: Integration Request {ir_name}\n{json.dumps(data, indent=1)}",
    )


# ---------------------------------------------------------------- result -> register
def find_held(result: dict) -> str | None:
    """The held confirmation whose latest query ``result`` answers, matched by its exact IDs.

    For a result whose ResultURL lost its ``?ir=``. The IDs come from an unauthenticated body:
    only Daraja-shaped IDs reach the LIKE, and only an exact match in the stored state counts.
    """
    for key in ("ConversationID", "OriginatorConversationID"):
        sent_id = daraja_id((result or {}).get(key))
        if not sent_id:
            continue
        for row in frappe.get_all(
            IR,
            filters={"integration_request_service": SERVICE, "output": ["like", f"%{sent_id}%"]},
            fields=["name", "output"],
            order_by="creation desc",
            limit=20,
        ):
            state = _state(row)
            if sent_id in (state.get("conversation_id"), state.get("originator_conversation_id")):
                return row.name
    return None


def apply_result(ir_name: str, result: dict) -> str:
    """Create the register if Safaricom confirms the confirmation. Returns the outcome."""
    from .receipts import insert_register

    frappe.db.get_value(IR, ir_name, "name", for_update=True)
    ir = frappe.get_doc(IR, ir_name)
    # a late answer still counts after we gave up waiting (Failed); only Completed is final
    if ir.integration_request_service != SERVICE or ir.status == COMPLETED:
        return "ignored"
    state = _state(ir)
    data = json.loads(ir.data)

    sent_ids = {state.get("conversation_id"), state.get("originator_conversation_id")} - {None, ""}
    # unauthenticated body: a list or dict "ID" must not raise (unhashable) in the job
    got_ids = {daraja_id(result.get(k)) for k in ("ConversationID", "OriginatorConversationID")} - {""}
    if sent_ids and not (sent_ids & got_ids):
        return "ignored"  # not the answer to our latest query (an older one, or forged)

    values = result_values(result)
    outcome, reason = verify.c2b_verification(data, result, own_shortcodes(state.get("settings")))
    _save(ir_name, result=result, outcome=outcome, reason=reason)

    if outcome == verify.PENDING:
        if ir.status == QUEUED and cint(state.get("attempts")) >= MAX_ATTEMPTS:
            _fail(ir_name, f"Not confirmed after {MAX_ATTEMPTS} Transaction Status queries: {reason}")
        return outcome
    if outcome == verify.MISMATCH:
        _fail(ir_name, f"Safaricom contradicts the confirmation: {reason}")
        return outcome

    # Verified: Safaricom's own name/MSISDN fill gaps (C2B v2 masks the MSISDN)
    if not data.get("msisdn") or "*" in cstr(data.get("msisdn")):
        debit = cstr(values.get("DebitPartyName")).split(" - ", 1)
        if debit and debit[0].strip().isdigit():
            data["msisdn"] = debit[0].strip()
    outcome_insert, register = insert_register(data)
    _save(ir_name, status=COMPLETED, register=register, insert=outcome_insert)
    if register:
        frappe.db.set_value(IR, ir_name, {"reference_doctype": REGISTER, "reference_docname": register})
    return outcome


# ---------------------------------------------------------------- scheduler / staff
def sweep(limit: int = 50) -> None:
    """Re-ask Safaricom about held confirmations that got no result; give up after a few."""
    stale = add_to_date(now_datetime(), minutes=-RETRY_AFTER_MINUTES)
    for name in frappe.get_all(
        IR,
        filters={
            "integration_request_service": SERVICE,
            "status": QUEUED,
            "modified": ["<", stale],
            "creation": [">=", add_to_date(now_datetime(), days=-3)],
        },
        pluck="name",
        order_by="modified asc",
        limit=limit,
    ):
        try:
            ir = frappe.get_doc(IR, name)
            transid = ir.request_id
            if frappe.db.get_value(REGISTER, {"transid": transid}, "name"):  # cancelled ones too
                _save(name, status=COMPLETED, insert="imported by another path")
            elif cint(_state(ir).get("attempts")) >= MAX_ATTEMPTS:
                _fail(name, f"No Transaction Status result after {MAX_ATTEMPTS} queries.")
            else:
                send_query(name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"M-Pesa C2B verification sweep failed for {name}"[:140])


@frappe.whitelist()
def release(integration_request: str, reason: str) -> str | None:
    """Create the register for a held confirmation without Safaricom's confirmation.

    For receipts a manager has checked on the M-Pesa statement. Recorded on the
    Integration Request with the user and reason.
    """
    from ..permissions import require
    from .receipts import insert_register

    require(RELEASE_ROLES)
    if not cstr(reason).strip():
        frappe.throw(_("Give the reason (e.g. 'seen on the M-Pesa statement')."))
    frappe.db.get_value(IR, integration_request, "name", for_update=True)
    ir = frappe.get_doc(IR, integration_request)
    if ir.integration_request_service != SERVICE or ir.status == COMPLETED:
        frappe.throw(_("{0} is not a held C2B confirmation.").format(integration_request))
    _outcome, register = insert_register(json.loads(ir.data))
    _save(
        integration_request,
        status=COMPLETED,
        error=f"Released by {frappe.session.user}: {reason}",
        register=register,
        released_by=frappe.session.user,
    )
    if register:
        frappe.db.set_value(IR, integration_request, {"reference_doctype": REGISTER, "reference_docname": register})
    return register
