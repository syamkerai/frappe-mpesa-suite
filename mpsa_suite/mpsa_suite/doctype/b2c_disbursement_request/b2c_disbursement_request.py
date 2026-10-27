# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import contextlib
import uuid

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ...api.b2c import make_b2c_payment_request
from ....money_out import maker_checker
from ....permissions import ADMIN_ROLES, PAYOUT_ROLES, require

# Written only by the send, the ack, the Result and the booking (db writes). A scheduled payout's
# cancel decides on, and keeps, what the database holds.
SERVER_FIELDS = (
    "status", "send_started_on", "conversation_id", "originator_conversation_id", "response_code",
    "response_description", "error_code", "error_message", "result_type", "result_code", "result_desc",
    "transaction_id", "transaction_completed_datetime", "receiver_public_name", "recipient_is_registered_customer",
    "accounting_booked", "accounting_attempted_on", "accounting_error", "charges_paid_acct_avlbl_funds",
    "utility_acct_avlbl_funds", "working_acct_avlbl_funds",
)


class B2CDisbursementRequest(Document):
    """B2C Payments Transactions"""

    def before_insert(self) -> None:
        """Insert Missing Values"""
        self.originator_conversation_id = self._generate_uuid_v4()

    def validate(self) -> None:
        """B2C Payments Transactions validations"""
        if not self.originator_conversation_id:
            self.originator_conversation_id = self._generate_uuid_v4()
        from ....money_out.schedule import check_b2c_request

        check_b2c_request(self)  # a scheduled payout only from the job that makes it
        from ....money_out import employee_documents

        # made by hand: a claim or advance that may be paid, an employee only to an approved phone
        employee_documents.check_b2c_request(self)

    def before_submit(self) -> None:
        require(PAYOUT_ROLES)  # submitting sends the money
        if not (self.flags.get("mpesa_from_disbursement") or self.flags.get("mpesa_from_schedule")):
            maker_checker.check(self)

    def on_submit(self):
        if self.flags.get("mpesa_send_later") or self.schedule_line():
            return  # a scheduled payout is sent by money_out.schedule.send_b2c, after it is committed
        make_b2c_payment_request(self.name)

    def before_update_after_submit(self) -> None:
        """A save after submit through the form or the API (permissions checked) writes back what
        the database holds for the server's fields. Frappe does not enforce read only on the
        server, and a changed status or send marker would let a payment schedule send the payout
        again (schedule.b2c_never_sent). The Result saves with ignore_permissions and keeps its own."""
        if not self.flags.get("ignore_permissions"):
            self._server_fields_from_db()

    def _server_fields_from_db(self) -> None:
        """Locking read, so a value committed after this transaction's snapshot is seen too."""
        fields = [f for f in SERVER_FIELDS if self.meta.has_field(f)]
        current = frappe.db.get_value(self.doctype, self.name, fields, as_dict=True, for_update=True)
        if current:
            self.update({f: current.get(f) for f in fields})

    def before_cancel(self):
        """A payout of a payment schedule line that may have reached M-Pesa is cancelled only by a
        manager who checked the statement (or a Transaction Status check), and keeps its status
        (one whose send began and never answered becomes Status Unknown); a Paid one is the
        record of that salary and is not cancelled. Its line then waits for a manager, never
        planned again by anyone else (money_out.schedule)."""
        if not self.schedule_line():
            return
        self._server_fields_from_db()
        status = self.status or "Not Initiated"
        if status == "Not Initiated" and self.get("send_started_on"):
            from ....money_out.schedule import B2C_INTERRUPTED

            require(ADMIN_ROLES)
            self.status = "Status Unknown"
            self.error_message = B2C_INTERRUPTED
            return
        if status in ("Not Initiated", "Failed"):
            return
        if status in ("Initiated", "Status Unknown"):
            require(ADMIN_ROLES)
            return
        frappe.throw(f"{self.name} paid a salary from a payment schedule ({status}): it is the record of that payment "
                     "and cannot be cancelled.")

    def on_cancel(self):
        if self.schedule_line():
            from ....money_out.schedule import sync_b2c_line_later

            sync_b2c_line_later(self.b2c_payment_disbursement_reference)  # its line shows the cancel

    def on_trash(self):
        """A scheduled payout that may have reached M-Pesa is the only record of that money: its
        line waits on it (a manager checks the statement), and a late Result needs it to match."""
        if not self.schedule_line():
            return
        from ....money_out.schedule import b2c_never_sent

        if not b2c_never_sent(self):
            frappe.throw(f"{self.name} may have paid a salary from a payment schedule ({self.status}). It is the "
                         "record of that payment and cannot be deleted.")

    def _generate_uuid_v4(self) -> str:
        return str(uuid.uuid4())

    @frappe.whitelist()
    def retry_failed_payment(self) -> None:
        """Retry only payments Safaricom reported as failed."""
        require(PAYOUT_ROLES)
        # The document lock before the row lock: a disbursement submit takes the document
        # lock first and its locking read then reaches this row.
        with self._document_lock():
            # One locking read, which also takes the row lock: a plain reload sees this
            # transaction's snapshot, which can predate a concurrent retry's commit (the send
            # commits midway), and would let the same request be sent twice.
            self.flags.for_update = True
            self.reload()
            if self.docstatus != 1 or self.status != "Failed":
                frappe.throw(f"Only submitted, failed B2C requests can be retried ({self.status})")
            if self.schedule_line():
                # a scheduled payment is paid again only with Pay Selected (date rule, maker-checker)
                frappe.throw(f"{self.name} was paid from a payment schedule: plan its line again (Plan Again) and "
                             "pay it with Pay Selected.")
            self.validate_still_owed()
            from ....money_out import employee_documents

            # what the send checked, again: the claim may no longer be payable, the approved phone
            # may be cancelled or on hold, and nobody resends a payout to themselves
            employee_documents.check_b2c_retry(self)
            # Daraja rejects a reused OriginatorConversationID (500.002.1001), and the result
            # callback ignores re-sends for a request that already has a final result.
            self.db_set(
                {
                    "originator_conversation_id": self._generate_uuid_v4(),
                    "status": "Not Initiated",
                    "result_code": None,
                    "result_desc": None,
                    "transaction_id": None,
                }
            )
            make_b2c_payment_request(self.name)

    def schedule_line(self) -> str | None:
        """The M-Pesa Payment Schedule line this payout pays (its disbursement row's), or None."""
        ref = getattr(self, "b2c_payment_disbursement_reference", None)
        if not ref:
            return None
        return frappe.db.get_value("B2C Payment Disbursement Reference", ref, "mpesa_schedule_line") or None

    def _document_lock(self):
        """The double-payout check's per-document lock (held until commit; re-entrant)."""
        from ....money_in.common import named_lock
        from ....money_out.owed import busy_message

        if not self.reference_doctype or not self.reference_name:
            return contextlib.nullcontext()
        return named_lock(
            f"b2c-pay:{self.reference_doctype}:{self.reference_name}",
            busy=busy_message(self.reference_doctype, self.reference_name),
        )

    def validate_still_owed(self) -> None:
        """Refuse a retry the document no longer needs: a failed payout stops counting, so the
        document may have been paid since (another disbursement, a bank Payment Entry).

        Same per-document lock and reads as the submit check (validate_no_double_payout).
        Payouts of other disbursements still in flight or awaiting accounting refuse it;
        this disbursement's other rows for the document were approved together with this
        one, so they only take their share of what is owed. A Paid payout nothing books
        counts here however old, also once cancelled or once its entry was cancelled
        (``paid_without_entry``): a retry resends without anyone choosing the document again.
        A Paid payout that a row of this disbursement links (a row an amend copied from the
        cancelled original, which is not sent again) is this disbursement's own. B2B requests
        for the document refuse it too, an invoice's order advances are taken off, and an order
        that has invoices since is refused.
        """
        from ....money_out.owed import (
            DOC_FIELDS,
            EMPLOYEE_DOC_FIELDS,
            SLIP,
            b2b_open,
            b2b_slip_open,
            billed_order,
            order_advances,
            planned_lines,
            slip_bank_problem,
        )
        from ..b2c_payment_disbursement.b2c_payment_disbursement import (
            current_outstanding,
            open_payouts,
        )

        dt, dn = self.reference_doctype, self.reference_name
        if not dt or not dn:
            return
        mine = self.b2c_payment_disbursement
        with self._document_lock():
            # this request is Failed, so open_payouts leaves it out without being told to
            others = list(open_payouts(dt, dn))
            elsewhere = {o.parent for o in others if o.parent != mine}
            if dt != "Salary Slip":  # a Salary Slip's owed takes off every Paid payout, however old
                paid = paid_without_entry(dt, dn)
                elsewhere |= {o.parent for o in paid if mine not in (o.parents or [o.parent])}
                others += paid
            if dt in DOC_FIELDS or dt in EMPLOYEE_DOC_FIELDS or dt == SLIP:  # B2B for it (same lock, locking read)
                b2b = b2b_slip_open(dn, for_update=True) if dt == SLIP else b2b_open(dt, dn, for_update=True)
                elsewhere |= {o.parent for o in b2b}
                others += b2b
            if dt == SLIP:
                bank = slip_bank_problem(dn)  # its payroll may be paid by bank already
                if bank:
                    frappe.throw(bank, title=_("Paid by Bank?"))
            elsewhere = sorted(elsewhere)
            if elsewhere:
                frappe.throw(
                    _("{0} {1} already has a payout in progress or awaiting accounting: {2}").format(
                        dt, dn, ", ".join(elsewhere)
                    ),
                    title=_("Duplicate Payout"),
                )
            # an advance still on the invoice's orders, and lines a payment schedule plans for the
            # document, are taken off, as on submit (R3)
            advances = order_advances(dn, for_update=True) if dt == "Purchase Invoice" else []
            advances += planned_lines(dt, dn, for_update=True)
            owed = current_outstanding(dt, dn, for_update=True)  # the document row last (R4)
            billed = billed_order(dn, for_update=True) if dt == "Purchase Order" else None
            if billed:  # its invoices are paid instead (B2B may have paid them already)
                frappe.throw(billed, title=_("Order Already Invoiced"))
            if owed is None:
                return
            pending = sum(
                flt(o.allocated_amount)
                for o in others
                # a Salary Slip's owed already has every Paid payout taken off
                if not (dt == "Salary Slip" and o.request_status == "Paid")
            )
            advance = flt(sum(flt(a.amount) for a in advances), 2)
            if advance and flt(flt(self.amount) + pending + advance, 2) > flt(owed, 2):
                frappe.throw(
                    _("{0} {1} owes {2}, less {3}; retrying would pay {4}, and {5} is still being paid for it.").format(
                        dt, dn, flt(owed, 2), "; ".join(a.label for a in advances), flt(self.amount, 2),
                        flt(pending, 2),
                    ),
                    title=_("Already Paid"),
                )
            if flt(flt(self.amount) + pending, 2) > flt(owed, 2):
                frappe.throw(
                    _("{0} {1} owes {2}; retrying would pay {3}, and {4} is still being paid for it.").format(
                        dt, dn, flt(owed, 2), flt(self.amount, 2), flt(pending, 2)
                    ),
                    title=_("Already Paid"),
                )


def _erpnext_installed() -> bool:
    """The disbursement's submit check asks the same: Payment Entries need ERPNext."""
    return "erpnext" in frappe.get_installed_apps()


def paid_without_entry(doctype: str, name: str) -> list:
    """Paid payouts for the document that ``open_payouts`` does not list, though no submitted
    entry books them, so what the document owes does not reflect them and a retry must
    count them:

    - older than the accounting retry window and never booked;
    - on a request cancelled with its disbursement (Cancel All), however recent;
    - marked booked, though the entry that booked them was cancelled since: nothing resets
      ``accounting_booked`` then, and the document owes the amount again;
    - on a row that lost its link to the request (an old full save of the disbursement wiped
      it): found from the request, which still names the document and the row.

    One row per payout (request): ``request``, ``parent`` (the disbursement that sent it),
    ``parents`` (it and every disbursement whose rows link or name the request: an amend
    copies the link), ``allocated_amount`` (what was paid) and ``request_status``. A payout
    that ``open_payouts`` lists through any of its rows is left to it, so none is counted
    twice.

    Booked is checked, not trusted: what the booking sweep counts as the payout's entry
    (``booked_docstatus``), which a submitted entry against the document carrying the receipt
    always is. Without ERPNext there is no Payment Entry (nor its tables) and nothing can
    have booked them, so they all count.

    Every read is a locking read, as in ``open_payouts``: it sees what is committed now (a
    Cancel All or a Payment Entry cancel committed after this transaction's snapshot) and
    waits for one still in flight. So, given that ``open_payouts`` reads the same way, no
    payout is missed by both, whatever committed since the retry started.
    """
    from frappe.utils import add_days, nowdate

    from ....services.b2c_postprocess_service import RETRY_WINDOW_DAYS, booked_docstatus

    locking = "FOR UPDATE" if frappe.db.db_type == "mariadb" else ""
    values = {"doctype": doctype, "name": name, "cutoff": add_days(nowdate(), -RETRY_WINDOW_DAYS)}
    payout = """req.name AS request, req.amount, req.transaction_id, req.party,
          req.b2c_payment_disbursement AS sent_from, ref.parent, ref.allocated_amount, ref.reference_no"""
    # Through the rows that link the request. ``listed`` is exactly what open_payouts lists
    # for a Paid request: its disbursement joined, submitted request or disbursement, not
    # marked booked, inside the window.
    found = frappe.db.sql(
        f"""
        SELECT {payout},
          (d.name IS NOT NULL AND (d.docstatus = 1 OR req.docstatus = 1)
            AND COALESCE(req.accounting_booked, 0) = 0
            AND req.creation IS NOT NULL AND req.creation >= %(cutoff)s) AS listed
        FROM `tabB2C Payment Disbursement Reference` ref
        JOIN `tabB2C Disbursement Request` req ON req.name = ref.b2c_disbursement_request
        LEFT JOIN `tabB2C Payment Disbursement` d ON d.name = ref.parent
        WHERE ref.reference_name = %(name)s AND ref.reference_doctype = %(doctype)s
          AND req.docstatus IN (1, 2) AND req.status = 'Paid'
        {locking}
        """,
        values,
        as_dict=True,
    )
    # From the request, when the row it names does not link it back (or is gone). When that
    # row links no existing request, open_payouts lists the row itself by its own status:
    # it is this payout's row, so ``listed`` then too.
    found += frappe.db.sql(
        f"""
        SELECT {payout},
          (ref.name IS NOT NULL AND linked.name IS NULL
            AND ref.reference_name = %(name)s AND ref.reference_doctype = %(doctype)s
            AND d.name IS NOT NULL AND d.docstatus = 1
            AND COALESCE(ref.payment_status, '') != 'Failed') AS listed
        FROM `tabB2C Disbursement Request` req
        LEFT JOIN `tabB2C Payment Disbursement Reference` ref ON ref.name = req.b2c_payment_disbursement_reference
        LEFT JOIN `tabB2C Disbursement Request` linked ON linked.name = ref.b2c_disbursement_request
        LEFT JOIN `tabB2C Payment Disbursement` d ON d.name = ref.parent
        WHERE req.reference_name = %(name)s AND req.reference_doctype = %(doctype)s
          AND req.docstatus IN (1, 2) AND req.status = 'Paid'
          AND (ref.name IS NULL OR COALESCE(ref.b2c_disbursement_request, '') != req.name)
        {locking}
        """,
        values,
        as_dict=True,
    )
    payouts = {}
    for row in found:
        seen = payouts.setdefault(row.request, frappe._dict(row, listed=False, parents=set()))
        seen.listed = seen.listed or cint(row.listed) == 1
        seen.parents.update(p for p in (row.sent_from, row.parent) if p)

    erpnext = _erpnext_installed()
    rows = []
    for request, seen in payouts.items():
        if seen.listed:
            continue
        paid = frappe._dict(
            request=request,
            request_status="Paid",
            parent=seen.sent_from or seen.parent or request,
            parents=sorted(seen.parents),
            allocated_amount=flt(seen.amount) or flt(seen.allocated_amount),
            reference_doctype=doctype,
            reference_name=name,
            party=seen.party,
            reference_no=seen.transaction_id or seen.reference_no,
        )
        if erpnext and booked_docstatus(seen.sent_from or seen.parent, paid, for_update=True) == 1:
            continue
        rows.append(paid)
    return rows
