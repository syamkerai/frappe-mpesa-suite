"""Transaction Status queries for B2B payouts (handover F2.4).

A Buy Goods (till) Result carries almost nothing: no receiver name, no charge and no time, only
"{Amount={CurrencyCode=KES, MinimumAmount=189400, BasicAmount=1894.00}}". Safaricom's
Transaction Status answer for the receipt has all of them: ReceiptNo, CreditPartyName
("835811 - ASTRAL INDUSTRIES LTD HQ"), DebitPartyCharges, FinalisedTime and DebitAccountType.
Its top-level TransactionID is a placeholder ("UJR1000000"): ReceiptNo is the receipt.

So a Paid Result that names no receiver or states no charge is queried at once (``ask_later``)
and again by ``sweep`` while something is missing (Safaricom may answer "not found" at first),
and anyone who may check a payout can ask from the form (money_out.api.check_b2b_status). The
query goes through the B2C query doctype and endpoints (b2c_status), whose result handler
passes it to ``handle_result``.

An answer counts only when it is Completed for the payout's amount, its ReceiptNo is the
payout's receipt, its own ConversationID or OriginatorConversationID is the payout's, and it
paid the payout's receiver shortcode. Then only blank fields are filled (what the Result stated
is kept; an estimated time is replaced), the receiver name is checked against the approved
account and the charge gets its statement line. A request whose Result never came (Initiated,
Unknown) is settled by the answer exactly as its Result would have settled it.
"""

import re

import frappe
from frappe.utils import add_to_date, cint, cstr, get_datetime, now_datetime

from ..daraja import money_out as daraja
from . import b2c_status
from .common import log_callback

B2B_DOCTYPE = "Mpesa B2B Payment Request"
QUERY_DOCTYPE = b2c_status.QUERY_DOCTYPE
# Paid: fill what its Result left out. Initiated, Unknown: its Result never came; settle it.
ASKABLE = ("Paid", "Initiated", "Unknown")
UNSETTLED = ("Initiated", "Unknown")
# Asked again while something is missing: this many queries in all, this far apart, for this
# long after the payout.
MAX_QUERIES = 3
QUERY_GAP_MINUTES = 10
ASK_WITHIN_HOURS = 48
LABELS = {
    "receiver_public_name": "Receiver",
    "debit_party_charges": "Charges",
    "trans_completed_time": "Completed At",
}


# ---------------------------------------------------------------- asking
def start_query(b2b_request: str) -> str:
    """Create and send a status query for ``b2b_request``; returns the query's name."""
    doc = frappe.db.get_value(
        B2B_DOCTYPE,
        b2b_request,
        ["name", "docstatus", "status", "mpesa_settings", "transaction_id", "originator_conversation_id"],
        as_dict=True,
    )
    if not doc:
        frappe.throw(f"B2B request {b2b_request} not found")
    if doc.docstatus not in (1, 2):
        frappe.throw("Only a submitted B2B request can be checked")
    if not doc.mpesa_settings:
        frappe.throw(f"{b2b_request} has no Pay From (Mpesa Settings)")
    if doc.status not in ASKABLE:
        frappe.throw(f"{b2b_request} is {doc.status or 'Not Initiated'}: only a payment M-Pesa may have made can be checked")
    receipt = cstr(doc.transaction_id).strip() if doc.status == "Paid" else ""
    if not (receipt or doc.originator_conversation_id):
        frappe.throw("Nothing to query by: M-Pesa never acknowledged this request, so it has no receipt and no "
                     "OriginatorConversationID. Check the M-Pesa statement.")
    recent = frappe.db.get_value(
        QUERY_DOCTYPE,
        {
            "b2b_request": b2b_request,
            "status": ["in", ("Queued", "Sent")],
            "creation": [">", add_to_date(now_datetime(), minutes=-b2c_status.QUERY_COOLDOWN_MINUTES)],
        },
        "name",
        order_by="creation desc",
    )
    if recent:
        frappe.throw(f"Status query {recent} is still waiting for Safaricom's answer")
    return b2c_status.send_query(
        {"b2b_request": b2b_request},
        doc.mpesa_settings,
        receipt,
        doc.originator_conversation_id,
        remarks=f"Status {b2b_request}",
        description="Mpesa B2B Transaction Status",
    )


def ask_later(name: str) -> None:
    """Query the payout from a job, once its Result is committed."""
    frappe.enqueue(
        "mpsa_suite.money_out.b2b_status.auto_query",
        queue="short",
        timeout=300,
        enqueue_after_commit=True,
        job_id=f"mpesa_b2b_status_{name}",
        deduplicate=True,
        name=name,
    )


def auto_query(name: str) -> str | None:
    """The Result's (or the sweep's) query, when one is still worth asking. Never raises."""
    if not may_ask(name):
        return None
    try:
        return start_query(name)
    except Exception:
        frappe.db.rollback()
        frappe.log_error(title=f"M-Pesa B2B status query failed for {name}"[:140], message=frappe.get_traceback())
        return None


def missing(row) -> list:
    """What a status answer would give that the Paid request still lacks."""
    lacking = [f for f in ("receiver_public_name", "debit_party_charges") if not cstr(row.get(f)).strip()]
    if not row.get("trans_completed_time") or cint(row.get("completed_time_estimated")):
        lacking.append("trans_completed_time")
    return lacking


def migrated() -> bool:
    """The query can name a B2B request and the request can carry an estimated time."""
    return frappe.get_meta(QUERY_DOCTYPE).has_field("b2b_request") and frappe.get_meta(B2B_DOCTYPE).has_field(
        "completed_time_estimated")


def may_ask(name: str) -> bool:
    """Whether Safaricom is (still) asked about a Paid request: something is missing, fewer than
    MAX_QUERIES were made, the last one QUERY_GAP_MINUTES ago or more, no matching answer came
    yet, and the payout is less than ASK_WITHIN_HOURS old."""
    if not migrated():
        return False
    row = frappe.db.get_value(
        B2B_DOCTYPE,
        name,
        ["name", "docstatus", "status", "transaction_id", "creation", "mpesa_settings", "receiver_public_name",
         "debit_party_charges", "trans_completed_time", "completed_time_estimated"],
        as_dict=True,
    )
    if (not row or row.docstatus not in (1, 2) or row.status != "Paid" or not row.transaction_id
            or not row.mpesa_settings or not missing(row)):
        return False
    if get_datetime(row.creation) < add_to_date(now_datetime(), hours=-ASK_WITHIN_HOURS):
        return False
    queries = frappe.get_all(QUERY_DOCTYPE, filters={"b2b_request": name}, fields=["name", "creation", "outcome"],
                             order_by="creation desc")
    if len(queries) >= MAX_QUERIES or any(q.outcome == "Paid" for q in queries):
        return False
    newest = max((get_datetime(q.creation) for q in queries), default=None)
    return not newest or newest <= add_to_date(now_datetime(), minutes=-QUERY_GAP_MINUTES)


def still_asking(name: str) -> bool:
    """True while a status answer may still give what the request lacks (a query is out, or the
    sweep will ask again): the charge line and the balance check wait for it."""
    if may_ask(name):
        return True
    return migrated() and bool(frappe.db.exists(QUERY_DOCTYPE, {
        "b2b_request": name,
        "status": ["in", ("Queued", "Sent")],
        "creation": [">", add_to_date(now_datetime(), minutes=-QUERY_GAP_MINUTES)],
    }))


def sweep(limit: int = 20) -> None:
    """Every five minutes: ask again about Paid requests whose receiver, charge or time is still
    missing (at most ``limit`` queries per run)."""
    if not migrated():
        return  # not migrated yet
    names = frappe.get_all(
        B2B_DOCTYPE,
        filters={"docstatus": ["in", [1, 2]], "status": "Paid", "transaction_id": ["is", "set"],
                 "creation": [">=", add_to_date(now_datetime(), hours=-ASK_WITHIN_HOURS)]},
        or_filters={"receiver_public_name": ["is", "not set"], "debit_party_charges": ["is", "not set"],
                    "trans_completed_time": ["is", "not set"], "completed_time_estimated": 1},
        pluck="name",
        order_by="creation asc",
        limit=200,
    )
    asked = 0
    for name in names:
        if asked >= limit:
            break
        if auto_query(name):
            asked += 1
        frappe.db.commit()


# ---------------------------------------------------------------- the answer
def credit_shortcode(text) -> str:
    """The shortcode of a CreditPartyName: "835811 - ASTRAL INDUSTRIES LTD HQ" -> "835811"."""
    return re.split(r"\s*-\s*", cstr(text).strip(), maxsplit=1)[0].strip()


def answer_outcome(doc, result: dict, values: dict) -> tuple[str, str]:
    """(Paid, Failed or Unresolved; why): what Safaricom's answer says about this payout. Only an
    answer for its amount, receipt, conversation and receiver counts; anything else is Unresolved
    and changes nothing."""
    outcome, reason = daraja.b2c_status_outcome(result, expected_amount=doc.amount)
    if outcome == "Unresolved":
        return outcome, reason
    own = (("ConversationID", doc.get("conversation_id")), ("OriginatorConversationID", doc.get("originator_conversation_id")))
    if not any(cstr(ours).strip() and cstr(values.get(key)).strip() == cstr(ours).strip() for key, ours in own):
        said = values.get("ConversationID") or values.get("OriginatorConversationID") or "(none)"
        return "Unresolved", f"The answer is for conversation {said}, not this payout's"
    if outcome == "Paid":
        receipt = cstr(values.get("ReceiptNo")).strip().upper()
        if doc.get("transaction_id") and receipt != cstr(doc.transaction_id).strip().upper():
            return "Unresolved", f"The answer is for receipt {receipt}, not {doc.transaction_id}"
        paid_to = credit_shortcode(values.get("CreditPartyName"))
        if paid_to != cstr(doc.get("receiver_shortcode")).strip():
            return "Unresolved", (f"The answer says {values.get('CreditPartyName') or 'nobody'} was paid, not "
                                  f"{doc.get('receiver_shortcode')}")
    return outcome, reason


def handle_result(query, result: dict) -> None:
    """b2c_status.handle_result, for a query about a B2B payout (the query's row is locked)."""
    values = daraja.result_values(result)
    # Lock the payout before reading it (a locking read: the latest committed row), so a real
    # B2B Result arriving now is serialised with this answer.
    frappe.db.get_value(B2B_DOCTYPE, query.b2b_request, "name", for_update=True)
    doc = frappe.get_doc(B2B_DOCTYPE, query.b2b_request, for_update=True)
    outcome, reason = answer_outcome(doc, result, values)
    query.db_set(
        {
            "status": "Result Received",
            "result_code": daraja.result_code(result),
            "result_desc": result.get("ResultDesc"),
            "transaction_status": values.get("TransactionStatus"),
            "receipt_no": values.get("ReceiptNo"),
            "amount": daraja.to_number(values.get("Amount")),
            "finalised_time": daraja.parse_daraja_datetime(values.get("FinalisedTime")),
            "debit_party_name": values.get("DebitPartyName"),
            "credit_party_name": values.get("CreditPartyName"),
            "debit_party_charges": values.get("DebitPartyCharges"),
            "debit_account_type": values.get("DebitAccountType"),
            "outcome": outcome,
            "outcome_reason": reason[:500],
        },
        notify=True,
    )
    log_callback({"Result": result}, "Mpesa B2B Status Result", QUERY_DOCTYPE, query.name, "Completed")

    if outcome == "Unresolved":
        return
    if doc.status == "Paid":
        if outcome != "Paid":
            query.db_set("outcome_reason", f"{reason}, but the payout's Result said Paid. Left unchanged: check the "
                         "M-Pesa statement."[:500])
            return
        filled = fill(doc, values, f"Transaction Status query {query.name}")
        query.db_set({"applied": 1 if filled else 0,
                      "outcome_reason": (f"{reason}. Filled: {', '.join(LABELS[f] for f in filled)}." if filled
                                         else f"{reason}. Nothing was missing on the payout.")[:500]})
        return
    if doc.status in UNSETTLED and doc.get("result_code") in (None, ""):
        settle(doc, query, outcome, values)
        return
    # The real Result got there first; it is the authority.
    query.db_set("outcome_reason", f"{reason}. Payout already {doc.status}; left unchanged."[:500])


def apply_answer(name: str, result: dict, source: str) -> tuple[str, str, list]:
    """An answer about this Paid request's receipt to a query asked elsewhere (Mpesa Settings →
    Check Transaction Status): the same checks and filling as a query of ours. Returns
    (outcome, reason, fields filled)."""
    frappe.db.get_value(B2B_DOCTYPE, name, "name", for_update=True)
    doc = frappe.get_doc(B2B_DOCTYPE, name, for_update=True)
    values = daraja.result_values(result)
    outcome, reason = answer_outcome(doc, result, values)
    if outcome != "Paid" or doc.status != "Paid":
        return outcome, reason, []
    return outcome, reason, fill(doc, values, source)


def fill(doc, values: dict, source: str) -> list:
    """Write what the answer gives and the Result left blank (an estimated time is replaced), then
    check the receiver name and make the charge's statement line. Returns the fields written."""
    update = {}
    name = cstr(values.get("CreditPartyName")).strip()
    if name and not cstr(doc.get("receiver_public_name")).strip():
        update["receiver_public_name"] = name
    charges = cstr(values.get("DebitPartyCharges")).strip()
    if charges and not cstr(doc.get("debit_party_charges")).strip():
        update["debit_party_charges"] = charges
    finalised = daraja.parse_daraja_datetime(values.get("FinalisedTime"))
    if finalised and (not doc.get("trans_completed_time") or cint(doc.get("completed_time_estimated"))):
        update["trans_completed_time"] = finalised
        update["completed_time_estimated"] = 0
    if not update:
        return []
    doc.db_set(update, notify=True)
    filled = [f for f in LABELS if f in update]
    doc.add_comment("Comment", f"{', '.join(LABELS[f] for f in filled)} filled from {source}.")
    if "receiver_public_name" in update:
        check_receiver_name(doc, name)
    from . import schedule
    from .statement import after_payout

    after_payout(B2B_DOCTYPE, doc.name)  # the charge line, now that the charge is known (never raises)
    schedule.sync_line_later(doc)
    return filled


def settle(doc, query, outcome: str, values: dict) -> None:
    """Settle a request whose Result never came, exactly as its Result would have."""
    if outcome == "Paid":
        finalised = daraja.parse_daraja_datetime(values.get("FinalisedTime"))
        update = {
            "status": "Paid",
            # A non-empty result_code makes a late Result a no-op (b2b._record_result): never booked twice.
            "result_code": "0",
            "result_desc": f"Confirmed by Transaction Status query {query.name}",
            "transaction_id": cstr(values.get("ReceiptNo")).strip(),
            "trans_completed_time": finalised or now_datetime(),
            "completed_time_estimated": 0 if finalised else 1,
            "receiver_public_name": cstr(values.get("CreditPartyName")).strip() or None,
            "debit_party_charges": cstr(values.get("DebitPartyCharges")).strip() or None,
            # as b2b._record_result: the sweep books it if the job below is lost
            "entry_status": "Entry Not Made",
            "entry_note": "Booking the entry…",
            "entry_attempted_on": now_datetime(),
        }
    else:
        update = {
            "status": "Failed",
            "result_code": f"TS:{values.get('TransactionStatus') or 'Failed'}"[:140],
            "result_desc": f"Transaction Status query {query.name}: {values.get('TransactionStatus')}",
        }
    doc.db_set(update, notify=True)
    query.db_set("applied", 1)
    doc.add_comment("Comment", f"Settled as {outcome} by Transaction Status query {query.name}.")
    from . import schedule

    schedule.sync_line_later(doc)  # a payment schedule's line follows its request
    if outcome == "Paid":
        check_receiver_name(doc, update["receiver_public_name"])
        frappe.enqueue(
            "mpsa_suite.money_out.b2b.book_payment_entry",
            queue="short",
            timeout=300,
            enqueue_after_commit=True,
            name=doc.name,
        )


def check_receiver_name(doc, received_name) -> None:
    """The name M-Pesa paid, against the approved account's (as b2b._record_result does; a
    mismatch blocks the account). Never raises."""
    from . import employee_accounts, supplier_accounts

    try:
        if cstr(doc.get("employee")).strip():
            employee_accounts.record_receiver_name(doc, doc.get("employee_mpesa_account"), received_name)
        else:
            supplier_accounts.record_receiver_name(doc, received_name)
    except Exception:
        frappe.log_error(title="M-Pesa B2B receiver name check failed", message=f"{doc.name}\n{frappe.get_traceback()}")
