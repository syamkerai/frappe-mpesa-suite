# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

import contextlib
import uuid

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from ...api.b2c import make_b2c_payment_request
from ....permissions import PAYOUT_ROLES


class B2CDisbursementRequest(Document):
    """B2C Payments Transactions"""

    def before_insert(self) -> None:
        """Insert Missing Values"""
        self.originator_conversation_id = self._generate_uuid_v4()

    def validate(self) -> None:
        """B2C Payments Transactions validations"""
        if not self.originator_conversation_id:
            self.originator_conversation_id = self._generate_uuid_v4()

    def on_submit(self):
        make_b2c_payment_request(self.name)

    def _generate_uuid_v4(self) -> str:
        return str(uuid.uuid4())

    @frappe.whitelist()
    def retry_failed_payment(self) -> None:
        """Retry only payments Safaricom reported as failed."""
        if frappe.session.user != "Administrator":
            frappe.only_for(PAYOUT_ROLES)
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
            self.validate_still_owed()
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
        from ....money_out.owed import DOC_FIELDS, b2b_open, billed_order, order_advances
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
            if dt in DOC_FIELDS:  # B2B requests for the invoice or order (same lock, locking read)
                b2b = b2b_open(dt, dn, for_update=True)
                elsewhere |= {o.parent for o in b2b}
                others += b2b
            elsewhere = sorted(elsewhere)
            if elsewhere:
                frappe.throw(
                    _("{0} {1} already has a payout in progress or awaiting accounting: {2}").format(
                        dt, dn, ", ".join(elsewhere)
                    ),
                    title=_("Duplicate Payout"),
                )
            # an advance still on the invoice's orders is taken off, as on submit (R3)
            advances = order_advances(dn, for_update=True) if dt == "Purchase Invoice" else []
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
