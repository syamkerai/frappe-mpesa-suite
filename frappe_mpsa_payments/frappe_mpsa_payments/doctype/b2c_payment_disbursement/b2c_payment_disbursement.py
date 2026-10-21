# Copyright (c) 2024, Navari Limited and contributors
# For license information, please see license.txt

from typing import Dict, List, Optional

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import DocType
from frappe.utils import cint, flt, now

from ....daraja.guard import B2C_MIN_AMOUNT
from ....utils.doctype_names import (
    B2C_REQUEST_DOCTYPE,
    MPESA_SETTINGS_DOCTYPE,
)
from .. import app_logger
from .config import DOCTYPE_CONFIGS, DoctypeConfig

# Reference row fields only the payout jobs and Results write (never the form).
SERVER_ROW_FIELDS = ("b2c_disbursement_request", "payment_status", "reference_no", "reference_date")


class B2CPaymentDisbursement(Document):
    """B2C Payment Disbursement Class"""

    def validate(self) -> None:
        """Validations for the document and references."""
        self.error = ""
        if "erpnext" not in frappe.get_installed_apps():
            return
        self.validate_mandatory_fields()
        self.validate_mode_of_payment()
        self.validate_party_type()
        self.set_outstanding_from_documents()
        for ref in self.references:
            ref.validate()
        self.validate_reference_doctypes()

    def before_save(self) -> None:
        """Set missing values before saving."""
        if "erpnext" not in frappe.get_installed_apps():
            return
        self.set_missing_values()

    def before_submit(self) -> None:
        """Initital payment trigger"""
        if self.payment_type == "Mpesa Disbursement":
            settings = self._get_mpesa_settings()

        else:
            frappe.throw(f"Unsupported payment type: {self.payment_type}")

        if "erpnext" in frappe.get_installed_apps():
            self.validate_no_double_payout()

        for ref in self.references:
            if ref.b2c_disbursement_request:
                # Row copied on amend: its payout was already sent from the original
                # document. Re-sending would pay the same person twice.
                sent_status = frappe.db.get_value(
                    B2C_REQUEST_DOCTYPE, ref.b2c_disbursement_request, "status"
                )
                if sent_status and sent_status != "Failed":
                    ref.payment_status = "Paid" if sent_status == "Paid" else "Initiated"
                    continue
                ref.b2c_disbursement_request = None
            ref.payment_status = "Initiated"
            frappe.enqueue(
                "frappe_mpsa_payments.frappe_mpsa_payments.doctype.b2c_payment_disbursement.b2c_payment_disbursement.create_b2c_request",
                queue="short",
                timeout=300,
                ref=ref,
                settings=settings,
                b2c_disbursement=self,
                enqueue_after_commit=True,
            )

        self.status = "Initiated"

    def before_update_after_submit(self) -> None:
        """A save after submit (form Update, API, scripts) writes every row back from memory.

        The payout jobs and Results write the request link and result to the rows directly,
        so the copy being saved may be older: keep what is in the database. Locking read, so
        a value committed after this transaction's snapshot is seen too.
        """
        locking = "FOR UPDATE" if frappe.db.db_type == "mariadb" else ""
        current = {
            row.name: row
            for row in frappe.db.sql(
                f"""
                SELECT name, {", ".join(SERVER_ROW_FIELDS)}
                FROM `tabB2C Payment Disbursement Reference`
                WHERE parent = %s AND parenttype = %s
                {locking}
                """,
                (self.name, self.doctype),
                as_dict=True,
            )
        }
        for ref in self.references:
            if ref.name in current:
                ref.update({field: current[ref.name][field] for field in SERVER_ROW_FIELDS})

    def set_outstanding_from_documents(self) -> None:
        """Outstanding comes from the document, never from the (editable) row."""
        for ref in self.references:
            owed = current_outstanding(ref.reference_doctype, ref.reference_name)
            if owed is not None:
                ref.outstanding_amount = owed

    def _rows_to_send(self) -> list:
        """Rows this submit will pay (amended copies of already-sent rows are not re-sent)."""
        rows = []
        for ref in self.references:
            if ref.b2c_disbursement_request:
                sent_status = frappe.db.get_value(
                    B2C_REQUEST_DOCTYPE, ref.b2c_disbursement_request, "status"
                )
                if sent_status and sent_status != "Failed":
                    continue
            rows.append(ref)
        return rows

    def _own_requests(self) -> list:
        """Requests this disbursement's rows count as their own: sent by it, or by the cancelled
        disbursement it amends (an amend copies the links). A link copied any other way (a
        Duplicate made before the link was no_copy, the API) is not trusted: it would hide
        another disbursement's live payout from the checks below."""
        own = []
        for request in sorted({r.b2c_disbursement_request for r in self.references if r.b2c_disbursement_request}):
            sent_from = frappe.db.get_value(B2C_REQUEST_DOCTYPE, request, "b2c_payment_disbursement")
            if sent_from and (sent_from == self.name or cint(frappe.db.get_value(self.doctype, sent_from, "docstatus")) == 2):
                own.append(request)
        return own

    def validate_no_double_payout(self) -> None:
        """Refuse to pay a document past what it owes, or while another payout for it is open.

        Runs under a per-document lock held until commit, and reads the other payouts with
        a locking read, so two disbursements submitted at the same moment cannot both pass.
        """
        from ....money_in.common import named_lock
        from ..b2c_disbursement_request.b2c_disbursement_request import paid_without_entry

        rows = self._rows_to_send()
        own_requests = self._own_requests()
        totals = {}
        for ref in rows:
            key = (ref.reference_doctype, ref.reference_name)
            totals[key] = totals.get(key, 0) + flt(ref.allocated_amount)

        for (doctype, name), allocated in sorted(totals.items()):
            if not doctype or not name:
                continue
            with named_lock(f"b2c-pay:{doctype}:{name}"):
                others = list(open_payouts(doctype, name, exclude_parent=self.name, exclude_requests=own_requests))
                # Paid payouts nothing books (entry cancelled, Cancel All, or older than the
                # retry window) are not in what the document owes; the retry guard counts them
                # too. A Salary Slip's owed already takes off every Paid payout.
                if doctype != "Salary Slip":
                    for paid in paid_without_entry(doctype, name):
                        if paid.request in own_requests or self.name in (paid.parents or [paid.parent]):
                            allocated += flt(paid.allocated_amount)  # this disbursement's own, not sent again
                        else:
                            others.append(paid)
                if others:
                    frappe.throw(
                        _("{0} {1} already has a payout in progress or awaiting accounting: {2}").format(
                            doctype, name, ", ".join(sorted({o.parent for o in others}))
                        ),
                        title=_("Duplicate Payout"),
                    )
                owed = current_outstanding(doctype, name, for_update=True)
                if owed is not None and flt(allocated, 2) > flt(owed, 2):
                    frappe.throw(
                        _("{0} {1} owes {2}; this disbursement allocates {3}.").format(
                            doctype, name, flt(owed, 2), flt(allocated, 2)
                        ),
                        title=_("Allocated More Than Outstanding"),
                    )

    def validate_mandatory_fields(self) -> None:
        mandatory_fields = [
            "company",
            "posting_date",
            "party_type",
            "paid_from",
            "paid_to",
        ]
        for field in mandatory_fields:
            if not self.get(field):
                frappe.throw(f"Field {self.meta.get_label(field)} is mandatory.")

        if not self.references:
            frappe.throw("At least one reference is required")

    def validate_mode_of_payment(self) -> None:
        if not self.mode_of_payment:
            frappe.throw("Mode of Payment is required")

    def validate_party_type(self) -> None:
        if self.party_type not in ["Employee", "Supplier"]:
            frappe.throw("Party Type must be Employee or Supplier")

    def validate_amounts(self) -> None:
        """Validate paid_amount and base_paid_amount."""
        if flt(self.paid_amount) <= 0:
            frappe.throw("Paid Amount must be greater than zero")
        if not self.base_paid_amount and self.paid_from_account_currency != "KES":
            frappe.throw("Base Paid Amount (KES) is required for M-Pesa")

        total_allocated = sum(flt(row.allocated_amount) for row in self.references)
        if flt(total_allocated) != flt(self.paid_amount):
            frappe.throw(
                f"Total allocated amount {total_allocated} must equal Paid Amount {self.paid_amount}"
            )

    def validate_reference_doctypes(self) -> None:
        for ref in self.references:
            if ref.reference_doctype != self.transaction_to_pay_against:
                frappe.throw(
                    f"Reference doctype '{ref.reference_doctype}' does not match '{self.transaction_to_pay_against}' for row {ref.idx}."
                )

    def set_missing_values(self):
        from erpnext.setup.utils import get_exchange_rate

        if not self.company_currency:
            self.company_currency = frappe.get_cached_value(
                "Company", self.company, "default_currency"
            )

        if not self.source_exchange_rate:
            self.source_exchange_rate = get_exchange_rate(
                self.paid_from_account_currency,
                self.company_currency,
                self.posting_date,
            )

        if self.paid_from_account_currency != "KES" and self.paid_amount:
            kes_rate = get_exchange_rate(
                self.company_currency, "KES", self.posting_date
            )
            self.base_paid_amount = flt(
                self.paid_amount * self.source_exchange_rate
            ) / flt(kes_rate)

    def _get_mpesa_settings(self) -> Document:
        """Fetch and validate Mpesa Settings for the Payment Gateway."""

        try:
            return frappe.get_doc(
                MPESA_SETTINGS_DOCTYPE,
                {"payment_gateway_name": self.mode_of_payment[6:]},
                as_dict=True,
            )
        except frappe.DoesNotExistError:
            frappe.throw(f"No Mpesa Settings found for gateway {self.mode_of_payment}")

    @frappe.whitelist()
    def retry_failed_payments(self) -> None:
        """Retry failed B2C Payment References"""

        for ref in self.references:
            if ref.payment_status == "Failed" and ref.b2c_disbursement_request:
                try:
                    b2c_request = frappe.get_doc(
                        B2C_REQUEST_DOCTYPE, ref.b2c_disbursement_request
                    )
                    if b2c_request.status == "Failed":
                        b2c_request.retry_failed_payment()
                except Exception as e:
                    frappe.log_error(
                        f"Retry error for B2C Request {ref.b2c_disbursement_request}: {str(e)}",
                        "Retry Failed Payments Error",
                    )

    @frappe.whitelist()
    def update_disbursement_status(self) -> None:
        """
        Update the B2C Payment Disbursement status based on all its child reference payment statuses.
        Called from the scheduler and the form button.

        Never saves: ``self`` may be the form's copy from submit time, and a save writes every
        row back, wiping the request links and results the payout jobs wrote since. The row
        statuses are read from the database and only the status fields are written, with
        db_set, so on_change (Value Change notifications, webhooks) and the Version still run.
        """

        try:
            statuses = frappe.get_all(
                "B2C Payment Disbursement Reference",
                filters={"parent": self.name, "parenttype": self.doctype, "parentfield": "references"},
                pluck="payment_status",
                order_by="idx asc",
            )

            total = len(statuses)
            paid = statuses.count("Paid")
            failed = statuses.count("Failed")

            new_status = "Not Initiated"
            if paid == total and total > 0:
                new_status = "Paid"
            elif failed == total and total > 0:
                new_status = "Failed"
            elif paid or failed:
                new_status = "Partially Paid"
            elif total > 0:
                new_status = "Initiated"

            current = frappe.db.get_value(
                self.doctype,
                self.name,
                ["status", "retry_count", "last_status_check", "modified"],
                as_dict=True,
                for_update=True,
            )
            old_status = current.status
            retry_count = cint(current.retry_count) + 1

            # a plain read (no row locks), so the events below and the form see the database
            self.reload()
            # db_set loads the whole document for update unless it has a before-image, and
            # that would lock the rows the payout jobs are writing to
            before = frappe.get_doc(self.as_dict())
            before.update(current)
            self._doc_before_save = before
            self.db_set(
                {"status": new_status, "retry_count": retry_count, "last_status_check": now()},
                notify=True,  # open forms reload instead of keeping their old rows
            )
            self.save_version()

            if new_status != old_status:
                return (
                    f"Status changed from '{old_status}' to '{new_status}'. "
                    f"References: {paid} Paid, {failed} Failed, {total} Total. "
                    f"Retry #{retry_count}."
                )
            else:
                return (
                    f"No status change (still '{new_status}'). "
                    f"References: {paid} Paid, {failed} Failed, {total} Total. "
                    f"Retry #{retry_count}."
                )

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"Error updating B2C Payment Disbursement status for {self.name}",
            )
            frappe.throw("Failed to update status")

    @frappe.whitelist()
    def get_outstanding_reference_documents(self, args: Dict) -> List[Dict]:
        """
        Fetch outstanding references based on filters.

        Args:
            args: Dictionary containing filter parameters

        Returns:
            List of outstanding reference documents
        """

        args = args.get("args", args) if isinstance(args, dict) else args
        doctype = args["transaction_to_pay_against"]
        config = self._get_doctype_config(doctype)
        filters = self._build_filters(args, config)
        entries = self._fetch_entries(doctype, config, args, filters)
        return self._populate_references(entries, args, doctype, config.date_field)

    def _get_doctype_config(self, doctype: str) -> DoctypeConfig:
        """
        Retrieve configuration for the specified doctype.

        Args:
            doctype: Document type name.

        Returns:
            DoctypeConfig object for the given doctype.
        """
        if not (config := DOCTYPE_CONFIGS.get(doctype)):
            frappe.throw("Invalid transaction type")
        return config

    def _build_filters(self, args: Dict, config: DoctypeConfig) -> Dict:
        """Build database query filters."""
        filters = {
            "docstatus": 1,
            "company": args["company"],
            **config.additional_filters,
        }

        if args.get("party"):
            filters["employee"] = args["party"]

        self._add_date_filter(
            filters,
            config.date_field,
            args.get("from_posting_date"),
            args.get("to_posting_date"),
        )

        due_date_field = (
            "due_date"
            if args["transaction_to_pay_against"] == "Purchase Invoice"
            else "schedule_date"
        )
        self._add_date_filter(
            filters, due_date_field, args.get("from_due_date"), args.get("to_due_date")
        )

        if greater_than := args.get("outstanding_amt_greater_than"):
            filters["outstanding_amount"] = [">", greater_than]
        if less_than := args.get("outstanding_amt_less_than"):
            if greater_than:
                filters["outstanding_amount"] = ["between", [greater_than, less_than]]
            else:
                filters["outstanding_amount"] = ["<", less_than]

        return filters

    def _add_date_filter(
        self,
        filters: Dict,
        field: str,
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> None:
        """
        Add date range filter to filters dictionary.

        Args:
            filters: Dictionary to update with date filters.
            field: Field name for the date filter.
            from_date: Start date for the filter.
            to_date: End date for the filter.
        """

        if from_date and to_date:
            filters[field] = ["between", [from_date, to_date]]
        elif from_date:
            filters[field] = [">=", from_date]
        elif to_date:
            filters[field] = ["<=", to_date]

    def _fetch_entries(
        self, doctype: str, config: DoctypeConfig, args: Dict, filters: Dict
    ) -> List[Dict]:
        """
        Fetch documents from database.

        Args:
            doctype: Document type to fetch.
            config: Doctype configuration.
            args: Filter parameters.
            filters: Database query filters

        Returns:
            List of document entries.
        """
        if "erpnext" in frappe.get_installed_apps() and config.use_erpnext_function:
            return self._fetch_erpnext_entries(doctype, args)

        if "hrms" in frappe.get_installed_apps() and doctype == "Salary Slip":
            return self._fetch_unpaid_salary_slips(doctype, args, filters)

        try:
            entries = frappe.db.get_all(
                doctype,
                filters=filters,
                fields=config.fields or [],
                order_by=f"{config.date_field} asc",
                limit=1000,
            )
        except Exception as e:
            frappe.log_error(
                f"Error fetching {doctype}: {str(e)}", "Error Fetching Entries"
            )
            frappe.msgprint(
                _(f"Failed to fetch {doctype} references"),
                title=_("Error"),
                indicator="red",
            )
            return []

        if doctype == "Employee Advance":
            entries = [
                e for e in entries if (e.paid_amount or 0) < (e.advance_amount or 0)
            ]

        if not entries:
            frappe.msgprint(
                _("No outstanding references found for the set filters"),
                title=_("No References"),
                indicator="blue",
            )

        return entries

    def _fetch_unpaid_salary_slips(
        self, doctype: str, args: Dict, filters: Dict
    ) -> List[Dict]:
        """
        Fetch unpaid salary slips for a given Payroll Entry.
        """

        SalarySlip = DocType("Salary Slip")
        JournalEntry = DocType("Journal Entry")
        JournalEntryAccount = DocType("Journal Entry Account")

        if not args.get("payroll_entry"):
            frappe.throw("Payroll Entry is required to fetch unpaid salary slips")

        # Fetch all Salary Slips linked to the Payroll Entry
        salary_slips_query = (
            frappe.qb.from_(SalarySlip)
            .select(
                SalarySlip.name,
                SalarySlip.posting_date,
                SalarySlip.employee,
                SalarySlip.net_pay,
                SalarySlip.currency,
                SalarySlip.journal_entry,
                SalarySlip.base_rounded_total,
                SalarySlip.payroll_entry,
            )
            .where(SalarySlip.payroll_entry == args["payroll_entry"])
            .where(SalarySlip.docstatus == 1)
        )

        if filters.get("from_posting_date"):
            salary_slips_query = salary_slips_query.where(
                SalarySlip.posting_date >= filters["from_posting_date"]
            )
        if filters.get("to_posting_date"):
            salary_slips_query = salary_slips_query.where(
                SalarySlip.posting_date <= filters["to_posting_date"]
            )
        if filters.get("outstanding_amt_greater_than"):
            salary_slips_query = salary_slips_query.where(
                SalarySlip.net_pay > filters["outstanding_amt_greater_than"]
            )

        salary_slips = salary_slips_query.run(as_dict=True)

        if not salary_slips:
            frappe.msgprint(
                _("No outstanding references found for the set filters"),
                title=_("No References"),
                indicator="blue",
            )
            return []

        # 2: Check for linked Bank Entries (Journal Entry)
        journal_entries_query = (
            frappe.qb.from_(JournalEntry)
            .join(JournalEntryAccount)
            .on(JournalEntry.name == JournalEntryAccount.parent)
            .select(JournalEntry.name)
            .where(JournalEntry.voucher_type == "Bank Entry")
            .where(JournalEntry.docstatus == 1)
            .where(JournalEntryAccount.reference_type == "Payroll Entry")
            .where(JournalEntryAccount.reference_name == args["payroll_entry"])
            .distinct()
        )
        journal_entries = journal_entries_query.run(as_dict=True)
        je_names = [je.name for je in journal_entries]

        paid_employees = set()
        if je_names:
            # 3: Fetch Journal Entry Accounts for these Journal Entries
            je_accounts_query = (
                frappe.qb.from_(JournalEntryAccount)
                .select(
                    JournalEntryAccount.party, JournalEntryAccount.debit_in_account_currency
                )
                .where(JournalEntryAccount.parent.isin(je_names))
                .where(JournalEntryAccount.party_type == "Employee")
                .where(JournalEntryAccount.debit_in_account_currency > 0)
            )
            je_accounts = je_accounts_query.run(as_dict=True)

            paid_employees = {
                (acc.party, acc.debit_in_account_currency) for acc in je_accounts
            }

        # 4: Identify unpaid Salary Slips. A B2C payout's entry debits the rounded total, which
        # never matches a net_pay with cents, so also ask what the slip still owes.
        unpaid_slips = []
        for slip in salary_slips:
            if (slip.employee, slip.net_pay) in paid_employees:
                continue
            if flt(current_outstanding(doctype, slip.name)) < B2C_MIN_AMOUNT:
                continue
            unpaid_slips.append(slip)

        if not unpaid_slips:
            frappe.msgprint(
                _(
                    f"All Salary Slips for Payroll Entry {args['payroll_entry']} are fully paid."
                ),
                title=_("All Paid"),
                indicator="green",
            )

        return unpaid_slips

    def _fetch_erpnext_entries(self, doctype: str, args: Dict) -> List[Dict]:
        """
        Fetch entries using ERPNext's function for specific doctypes.

        Args:
            doctype: Document type to fetch.
            args: Filter parameters.

        Returns:
            List of document entries.
        """
        from erpnext.accounts.doctype.payment_entry.payment_entry import (
            get_outstanding_reference_documents as original_get_outstanding_reference_documents,
        )

        from erpnext.accounts.party import get_party_account

        party_type = args.get("party_type")
        company = args.get("company")
        party = args.get("party")
        parties = (
            [party]
            if party
            else frappe.get_all(party_type, filters={"disabled": 0}, pluck="name")
        )

        # Batch fetch party accounts
        party_accounts = {}
        for p in parties:
            try:
                accounts = get_party_account(
                    party_type,
                    p,
                    company,
                    include_advance=args.get(
                        "book_advance_payments_in_separate_party_account", False
                    ),
                )
                party_accounts[p] = (
                    accounts[0] if isinstance(accounts, list) else accounts
                )
            except Exception as e:
                app_logger.error(
                    f"Failed fetching account for {party_type} {p}: {str(e)}"
                )

        all_entries = []
        for party in parties:
            if not (party_account := party_accounts.get(party)):
                continue

            erpnext_args = {
                "party_type": party_type,
                "party": party,
                "company": company,
                "party_account": party_account,
                "get_outstanding_invoices": doctype == "Purchase Invoice",
                "get_orders_to_be_billed": doctype == "Purchase Order",
                "from_posting_date": args.get("from_posting_date"),
                "to_posting_date": args.get("to_posting_date"),
                "from_due_date": args.get("from_due_date"),
                "to_due_date": args.get("to_due_date"),
                "outstanding_amt_greater_than": args.get(
                    "outstanding_amt_greater_than"
                ),
                "outstanding_amt_less_than": args.get("outstanding_amt_less_than"),
                "book_advance_payments_in_separate_party_account": args.get(
                    "book_advance_payments_in_separate_party_account", False
                ),
            }

            try:
                entries = original_get_outstanding_reference_documents(erpnext_args)
                entries = [e for e in entries if e["voucher_type"] == doctype]
                for entry in entries:
                    entry["party"] = party
                    entry["party_type"] = party_type
                all_entries.extend(entries)
            except Exception as e:
                app_logger.error(f"Failed fetching for {party_type} {party}: {str(e)}")

        return all_entries

    def _populate_references(
        self, entries: List[Dict], args: Dict, doctype: str, date_field: str
    ) -> List[Dict]:
        """Process entries and populate the references child table."""
        if not entries:
            return []

        # Clear existing references
        self.set("references", [])
        references = []
        config = self._get_doctype_config(doctype)

        for entry in entries:
            payable_amount = config.payable_amount_calc(entry)
            if payable_amount <= 0:
                app_logger.info(
                    f"Skipping entry due to zero or negative payable amount: {entry}"
                )
                continue

            if not (party_info := self._extract_party_info(entry, args)):
                frappe.log_error(
                    title="Skipping Entry",
                    message=f"Skipping entry due to missing party or party_type: {entry}",
                )

            party, party_type = party_info["party"], party_info["party_type"]

            # Payee M-Pesa number (required field)
            party_details = self.get_party_identifier(
                entry, party_type, self.payment_type
            )

            if not party_details.get("account_number"):
                frappe.log_error(
                    title=f"Missing Phone - {party}"[:140],
                    message=f"Skipping entry due to missing phone number for {party}: {entry}",
                )

            currency = entry.get("currency") or self.company_currency
            if not entry.get("currency"):
                app_logger.warning(
                    f"Currency not found for {doctype} {entry.get('name')}"
                )

            reference = {
                "reference_doctype": doctype,
                "reference_name": entry.get("voucher_no") or entry.get("name"),
                "party_type": party_type,
                "party": party,
                "payroll_entry": (
                    entry.get("payroll_entry")
                    if doctype == "Salary Slip"
                    and self.transaction_to_pay_against == "Salary Slip"
                    else None
                ),
                "due_date": entry.get("due_date") or entry.get("schedule_date"),
                "total_amount": self._get_invoice_amount(entry, config),
                "outstanding_amount": payable_amount,
                "allocated_amount": 0,
                "currency": currency,
                "exchange_rate": self._get_exchange_rate(entry, doctype),
                "partyb": party_details.get("account_number"),
                "payment_status": "Not Initiated",
            }
            references.append(reference)

            self.append(
                "references",
                {
                    "reference_doctype": reference["reference_doctype"],
                    "reference_name": reference["reference_name"],
                    "party_type": reference["party_type"],
                    "party": reference["party"],
                    "payroll_entry": reference["payroll_entry"],
                    "due_date": reference["due_date"],
                    "total_amount": reference["total_amount"],
                    "outstanding_amount": reference["outstanding_amount"],
                    "allocated_amount": reference["allocated_amount"],
                    "currency": reference["currency"],
                    "exchange_rate": reference["exchange_rate"],
                    "partyb": reference["partyb"],
                },
            )

        return references

    def _extract_party_info(self, entry: Dict, args: Dict) -> Optional[Dict]:
        """Extract party and party_type from entry or args."""
        party = entry.get("party") or entry.get("employee") or entry.get("supplier")
        party_type = entry.get("party_type") or args.get("party_type")

        if not party or not party_type:
            app_logger.info(
                f"Skipping entry due to missing party or party_type: {entry}"
            )
            return None

        return {"party": party, "party_type": party_type}

    def _get_invoice_amount(self, entry: Dict, config: DoctypeConfig) -> float:
        """
        Calculate invoice amount based on document type.

        Args:
            entry: Document entry.
            config: Doctype configuration.

        Returns:
            Invoice amount.
        """
        return entry.get(config.invoice_amount_field, 0)

    def _get_exchange_rate(self, entry: Dict, doctype: str) -> float:
        """Calculate exchange rate for the document."""
        from erpnext.setup.utils import get_exchange_rate

        if self.paid_to_account_currency == self.company_currency:
            return 1

        return get_exchange_rate(
            entry.get("currency") or self.company_currency,
            self.company_currency,
            entry.get("posting_date"),
        )

    def get_party_identifier(
        self, entry: Dict, party_type: str, payment_type: str
    ) -> dict:
        """
        Return {"account_number": <payee's M-Pesa number>}:
        the employee's cell number, or the supplier contact's phone / mobile.
        """

        result = {"account_number": ""}

        # Mobile Payouts
        if party_type == "Employee":
            result["account_number"] = (
                frappe.db.get_value("Employee", entry.employee, "cell_number") or ""
            )
            return result

        if party_type == "Supplier":
            contact = frappe.db.get_all(
                "Contact",
                filters={"link_name": entry.party},
                fields=["phone", "mobile_no"],
                limit=1,
            )
            if contact:
                result["account_number"] = (
                    contact[0].get("phone") or contact[0].get("mobile_no") or ""
                )
            return result

        return result

    @frappe.whitelist()
    def allocate_amount_to_references(
        self, paid_amount, paid_amount_change=0, allocate_payment_amount=None
    ):
        if not self.references:
            return

        allocate_payment_amount = (
            allocate_payment_amount
            if allocate_payment_amount is not None
            else frappe.flags.allocate_payment_amount or False
        )

        if not allocate_payment_amount:
            for ref in self.references:
                ref.allocated_amount = 0
            return

        precision = self.precision("paid_amount")
        total_positive_outstanding = 0
        total_negative_outstanding = 0
        paid_amount = flt(paid_amount, precision)

        for ref in self.references:
            outstanding_amount = flt(ref.outstanding_amount, precision)
            if outstanding_amount > 0:
                total_positive_outstanding += outstanding_amount
            else:
                total_negative_outstanding -= abs(outstanding_amount)

        allocated_positive_outstanding = 0
        allocated_negative_outstanding = 0

        if total_positive_outstanding > paid_amount:
            remaining_outstanding = flt(
                total_positive_outstanding - paid_amount, precision
            )
            allocated_negative_outstanding = min(
                remaining_outstanding, total_negative_outstanding
            )
        allocated_positive_outstanding = paid_amount + allocated_negative_outstanding

        for ref in self.references:
            outstanding_amount = flt(ref.outstanding_amount, precision)
            if outstanding_amount > 0 and allocated_positive_outstanding >= 0:
                ref.allocated_amount = min(
                    allocated_positive_outstanding, outstanding_amount
                )
                allocated_positive_outstanding = flt(
                    allocated_positive_outstanding - ref.allocated_amount, precision
                )
            elif outstanding_amount < 0 and allocated_negative_outstanding > 0:
                ref.allocated_amount = (
                    min(allocated_negative_outstanding, abs(outstanding_amount)) * -1
                )
                allocated_negative_outstanding = flt(
                    allocated_negative_outstanding - abs(ref.allocated_amount),
                    precision,
                )
            else:
                ref.allocated_amount = 0


def create_b2c_request(ref, settings, b2c_disbursement):
    from ..b2c_payment_disbursement_reference.b2c_payment_disbursement_reference import (
        b2c_amount_problem,
    )

    if problem := b2c_amount_problem(ref.allocated_amount):
        # Checked before anything is created or sent, so nothing went out. Failing the job
        # instead would roll back and leave the row Initiated with no request: never paid,
        # and counted as a live payout that refuses every later one for the document.
        frappe.db.set_value(ref.doctype, ref.name, "payment_status", "Failed")
        frappe.log_error(
            title=f"B2C payout not sent: {b2c_disbursement.name} row {ref.idx}"[:140],
            message=f"{ref.reference_doctype} {ref.reference_name}: {problem}",
        )
        return

    try:
        data = {
            "doctype": B2C_REQUEST_DOCTYPE,
            "amount": ref.allocated_amount,
            "b2c_payment_disbursement": b2c_disbursement.name,
            "b2c_payment_disbursement_reference": ref.name,
            "reference_doctype": ref.reference_doctype,
            "reference_name": ref.reference_name,
            "party_type": ref.party_type,
            "party": ref.party,
        }

        data["phone_number"] = ref.partyb
        data["mpesa_settings"] = settings.name
        data["payment_provider"] = "Mpesa"

        b2c_request = frappe.get_doc(data)
        b2c_request.insert(ignore_permissions=True)
        b2c_request.submit()
        frappe.db.set_value(
            ref.doctype, ref.name, "b2c_disbursement_request", b2c_request.name
        )

    except Exception as e:
        frappe.log_error(
            title="Failed to create B2C Request", message=frappe.get_traceback()
        )
        frappe.throw(f"Failed to create B2C Request {e}")


def current_outstanding(doctype: str | None, name: str | None, for_update: bool = False) -> float | None:
    """What ``doctype`` ``name`` still owes, read from the document (None: not tracked).

    ``for_update``: see ``salary_slip_paid``; the double-payout check passes it. The
    document's own totals are then a locking read too: a booking committed after this
    transaction's snapshot drops its payout from ``open_payouts``, and the plain read would
    still show the outstanding from before it.
    """
    from ....utils.payouts import OWED_FIELDS, owed

    if not doctype or not name or doctype not in OWED_FIELDS:
        return None
    meta = frappe.get_meta(doctype)
    # a Salary Slip also needs who it pays and its Payroll Entry, to find what was paid
    extra = ("employee", "payroll_entry") if doctype == "Salary Slip" else ()
    fields = [f for f in (*OWED_FIELDS[doctype], *extra) if meta.has_field(f)]
    values = frappe.db.get_value(doctype, name, ["docstatus", *fields], as_dict=True, for_update=for_update)
    if not values or values.docstatus != 1:
        return 0.0
    paid = 0.0
    if doctype == "Salary Slip":
        paid = salary_slip_paid(
            name, values.get("employee"), values.get("payroll_entry"), for_update=for_update
        )
    return owed(doctype, values, paid)


def salary_slip_paid(
    name: str, employee: str | None = None, payroll_entry: str | None = None, for_update: bool = False
) -> float:
    """What was already paid against a Salary Slip, whose totals never go down when it is paid.

    Every Paid B2C payout for the slip counts, booked or not and however old, from any
    disbursement, also once its request is cancelled (as with Cancel All on a disbursement:
    the money is still gone); payouts still in flight are refused by ``open_payouts``. So
    does a submitted bank / cash Journal Entry that pays the employee against the slip's
    Payroll Entry and was not made by the B2C flow (payroll banking).

    ``for_update``: also read the Paid payouts through the disbursement rows with a locking
    read, as ``open_payouts`` does. The plain read sees this transaction's snapshot, and
    ``open_payouts`` stops listing a payout once it is Paid and booked, so a payout that got
    there after the snapshot would otherwise be missed by both.
    """
    # request -> amount, so a request found by both reads (or by two rows) counts once
    b2c = dict(
        frappe.db.sql(
            """
            SELECT name, amount FROM `tabB2C Disbursement Request`
            WHERE docstatus IN (1, 2) AND status = 'Paid'
              AND reference_doctype = 'Salary Slip' AND reference_name = %s
            """,
            (name,),
        )
    )
    if for_update:
        # by the indexed reference_name on the rows; the request table has no index to lock by
        locking = "FOR UPDATE" if frappe.db.db_type == "mariadb" else ""
        b2c.update(
            frappe.db.sql(
                f"""
                SELECT req.name, req.amount
                FROM `tabB2C Payment Disbursement Reference` ref
                JOIN `tabB2C Disbursement Request` req ON req.name = ref.b2c_disbursement_request
                WHERE ref.reference_name = %s AND ref.reference_doctype = 'Salary Slip'
                  AND req.docstatus IN (1, 2) AND req.status = 'Paid'
                {locking}
                """,
                (name,),
            )
        )
    bank = 0.0
    if employee and payroll_entry:
        # without the link field a B2C entry would count twice, which only refuses more
        not_b2c = (
            "AND COALESCE(jea.b2c_payment_disbursement, '') = ''"
            if frappe.get_meta("Journal Entry Account").has_field("b2c_payment_disbursement")
            else ""
        )
        bank = frappe.db.sql(
            f"""
            SELECT COALESCE(SUM(jea.debit_in_account_currency), 0)
            FROM `tabJournal Entry Account` jea
            JOIN `tabJournal Entry` je ON je.name = jea.parent
            WHERE je.docstatus = 1 AND je.voucher_type IN ('Bank Entry', 'Cash Entry')
              AND jea.reference_type = 'Payroll Entry' AND jea.reference_name = %s
              AND jea.party_type = 'Employee' AND jea.party = %s
              AND jea.debit_in_account_currency > 0
              {not_b2c}
            """,
            (payroll_entry, employee),
        )[0][0]
    return round(sum(flt(amount) for amount in b2c.values()) + flt(bank), 2)


def open_payouts(doctype: str, name: str, exclude_parent: str | None = None, exclude_requests=None) -> list:
    """Other payouts for this document that have not yet reduced what it owes.

    A row counts unless its payout failed, or it was paid and its accounting entry booked
    (then the document's outstanding already reflects it). Locking read on the indexed
    reference_name, so a payout committed while we waited for the lock is seen.
    """
    from frappe.utils import add_days, nowdate

    from ....services.b2c_postprocess_service import RETRY_WINDOW_DAYS

    # rows of other disbursements whose request is not created yet have no request name;
    # the placeholder keeps them in (an empty list would exclude '' and so those rows)
    exclude_requests = [r for r in (exclude_requests or []) if r] or ["(none)"]
    # Paid-but-unbooked payouts older than the accounting retry window are left to staff
    # and must not block new payouts forever (e.g. rows paid before this check existed).
    booked_cutoff = add_days(nowdate(), -RETRY_WINDOW_DAYS)
    locking = "FOR UPDATE" if frappe.db.db_type == "mariadb" else ""
    return frappe.db.sql(
        f"""
        SELECT ref.parent, ref.allocated_amount, ref.payment_status, req.status AS request_status
        FROM `tabB2C Payment Disbursement Reference` ref
        JOIN `tabB2C Payment Disbursement` d ON d.name = ref.parent
        LEFT JOIN `tabB2C Disbursement Request` req ON req.name = ref.b2c_disbursement_request
        WHERE ref.reference_name = %(name)s AND ref.reference_doctype = %(doctype)s
          AND ref.parent != %(parent)s
          AND COALESCE(ref.b2c_disbursement_request, '') NOT IN %(own)s
          AND (d.docstatus = 1 OR (req.name IS NOT NULL AND req.docstatus = 1))
          AND COALESCE(req.status, ref.payment_status, '') != 'Failed'
          AND NOT (
            COALESCE(req.status, '') = 'Paid'
            AND (COALESCE(req.accounting_booked, 0) = 1 OR req.creation < %(booked_cutoff)s)
          )
        {locking}
        """,
        {
            "name": name,
            "doctype": doctype,
            "parent": exclude_parent or "",
            "own": tuple(exclude_requests),
            "booked_cutoff": booked_cutoff,
        },
        as_dict=True,
    )
