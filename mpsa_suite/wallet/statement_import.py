"""Import an M-PESA Org Portal statement: every movement of one sub-account becomes its line.

One file is one account of one shortcode (statement_file). Each row is put on the Bank
Account of the sub-account it moved (wallet.accounts) and, where the answer is certain,
booked and reconciled:

- payout, receipt: the line the live feed made is found by its receipt (or made) and
  reconciled with the entry that carries the receipt, as the live feed would;
- charge: the line "CHG-<receipt>" with the charge Safaricom actually took, and its Journal
  Entry from the M-Pesa Charges Account (Book Safaricom Charges Automatically);
- transfer between two of the shortcode's sub-accounts: a line on each side under the one
  receipt and an Internal Transfer Payment Entry between their GLs, both lines reconciled with
  it (nothing when both sub-accounts share a Bank Account: the money did not leave it);
- funding from a bank, and withdrawals to one: the line, reconciled with the entry staff already
  booked into that wallet's GL (exactly one of that amount within Match Bank Lines Within
  (Days)), or else, when exactly one unmatched bank line of that amount lies in that window, an
  Internal Transfer between the two GLs with both lines reconciled;
- a payout Safaricom charged nothing for (KES 100 or less) whose old "CHG-" line says otherwise
  is left for staff to undo.

Anything less certain (another amount, two candidates, an unknown kind) gets its line and is
left for staff: each row's outcome is an Mpesa Statement Row. Importing the same file again,
or an overlapping one, makes nothing twice and retries what was left.
"""

import hashlib
import json
from collections import Counter

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate

from ..money_in.bank_transaction import _stamp, create_statement_line, find_existing
from ..money_in.common import as_user, enabled, mpesa_bank_accounts, named_lock, settings_for_shortcode, suite_int
from ..money_out import statement as payouts
from . import accounts, statement_file

IMPORT = "Mpesa Statement Import"
ROW = "Mpesa Statement Row"
BT = "Bank Transaction"
PE = "Payment Entry"
B2C, B2B = payouts.B2C, payouts.B2B
COMMIT_EVERY = 50
# A reconciled row is done; anything else is looked at again (a transfer skipped while both
# wallets shared a Bank Account is booked once one of them has its own).
FINAL = ("Reconciled",)
TOLERANCE = 0.005


# ---------------------------------------------------------------- entry points
def start(name: str) -> None:
    frappe.db.set_value(IMPORT, name, {"status": "Queued", "error": None})
    frappe.enqueue(
        "mpsa_suite.wallet.statement_import.run",
        queue="long",
        timeout=3600,
        enqueue_after_commit=True,
        name=name,
    )


def run(name: str) -> None:
    """The import job: commits as it goes (every row is idempotent), so a failure part-way
    keeps what was done and the next run carries on."""
    import_now(name, commit=True)


def import_now(name: str, commit: bool = False) -> None:
    """Import one statement; ``commit``: as the job does (tests run it inside their transaction)."""
    doc = frappe.get_doc(IMPORT, name)
    doc.db_set({"status": "Importing", "error": None})
    if commit:
        frappe.db.commit()
    frappe.db.savepoint("mpesa_statement_import")
    try:
        st = statement_file.read(*file_content(doc.statement_file))
        settings = settings_for(doc, st)
        importer = Importer(doc, st, settings, commit=commit)
        importer.run()
        doc.db_set({**importer.summary(), "status": "Imported", "tie_out": importer.tie_out()})
        from . import balances

        balances.recheck(settings, st.sub_account)
    except Exception as e:
        known = isinstance(e, (statement_file.StatementError, frappe.ValidationError))
        message = str(e) if known else frappe.get_traceback().strip().splitlines()[-1]
        if commit:
            frappe.db.rollback()
        else:
            frappe.db.rollback(save_point="mpesa_statement_import")
        if not known:
            frappe.log_error(title=f"M-Pesa statement import {name} failed", message=frappe.get_traceback())
        frappe.db.set_value(IMPORT, name, {"status": "Failed", "error": message[:1000]})
    if commit:
        frappe.db.commit()


def file_content(file_url: str) -> tuple:
    file_doc = frappe.get_doc("File", {"file_url": file_url})
    content = file_doc.get_content()
    if isinstance(content, str):  # Frappe decodes files that are valid text (CSV)
        content = content.encode("utf-8")
    return content, file_doc.file_name or file_url


def settings_for(doc, st) -> str:
    """The Mpesa Settings of the statement's shortcode (the one chosen must match the file)."""
    shortcode = cstr(st.shortcode).strip()
    if doc.mpesa_settings:
        codes = frappe.db.get_value("Mpesa Settings", doc.mpesa_settings, ["business_shortcode", "till_number"])
        if shortcode and shortcode not in {cstr(c).strip() for c in codes or ()}:
            frappe.throw(f"This statement is for shortcode {shortcode}, not {doc.mpesa_settings}'s")
        return doc.mpesa_settings
    settings = settings_for_shortcode(shortcode)
    if not settings:
        frappe.throw(f"No Mpesa Settings for shortcode {shortcode}: choose one on the import")
    return settings


def row_key(shortcode: str, sub_account: str, row) -> str:
    raw = f"{shortcode}|{sub_account}|{row.receipt}|{row.amount:.2f}|{row.details.strip()[:80]}"
    return hashlib.sha1(raw.encode()).hexdigest()


# ---------------------------------------------------------------- the importer
class Importer:
    def __init__(self, doc, st, settings: str, commit: bool = False):
        self.doc, self.st, self.settings, self.commit = doc, st, settings, commit
        self.shortcode = cstr(st.shortcode).strip()
        self.sub = st.sub_account
        self.bank_account = accounts.bank_account(settings, self.sub)
        if not self.bank_account:
            frappe.throw(f"Map a Bank Account on Mpesa Settings {settings} (Accounting Mapping)")
        self.gl = frappe.db.get_value("Bank Account", self.bank_account, "account")
        self.company = frappe.db.get_value("Bank Account", self.bank_account, "company")
        self.counts = Counter()
        kinds = [(r, statement_file.classify(r, self.shortcode, self.sub)) for r in st.rows]
        self.charged = {r.receipt for r, k in kinds if k.kind == "charge"}
        self.receipts = {r.receipt for r in st.rows}
        self.main_gl = frappe.db.get_value("Bank Account", accounts.bank_account(settings), "account")
        figures = {"opening_balance": st.opening_balance, "closing_balance": st.closing_balance,
                   "total_paid_in": st.total_paid_in, "total_withdrawn": st.total_withdrawn}
        doc.db_set({
            "mpesa_settings": settings, "shortcode": self.shortcode, "account_name": st.account_name,
            "sub_account": self.sub, "bank_account": self.bank_account, "period_from": st.period_from,
            "period_to": st.period_to,
            **{k: v for k, v in figures.items() if v is not None},  # Currency columns hold no NULL
        })

    def run(self) -> None:
        for i, row in enumerate(reversed(self.st.rows), 1):  # oldest first, as the money moved
            self.counts["rows_read"] += 1
            frappe.db.savepoint("mpesa_statement_row")
            try:
                self.handle(row)
            except Exception:
                frappe.db.rollback(save_point="mpesa_statement_row")
                frappe.log_error(title=f"M-Pesa statement row {row.receipt} failed", message=frappe.get_traceback())
                self.record(row, statement_file.Kind("other"), "Left for Staff",
                            note=f"Could not import: {frappe.get_traceback().strip().splitlines()[-1]}"[:500])
            if self.commit and i % COMMIT_EVERY == 0:
                frappe.db.commit()  # also releases the per-line locks
        if self.st.rows:
            from . import balances

            newest = self.st.rows[0]
            if newest.balance is not None and newest.completed_at:
                balances.record(self.settings, self.sub, newest.balance, newest.completed_at, "Statement",
                                IMPORT, self.doc.name)

    def summary(self) -> dict:
        c = self.counts
        return {key: cint(c[key]) for key in ("rows_read", "lines_made", "lines_found", "entries_made", "reconciled",
                                              "left_for_staff", "skipped")}

    # ------------------------------------------------------------ one row
    def handle(self, row) -> None:
        kind = statement_file.classify(row, self.shortcode, self.sub)
        key = row_key(self.shortcode, self.sub, row)
        done = frappe.db.get_value(ROW, {"row_key": key}, ["name", "result"], as_dict=True)
        if done and done.result in FINAL:
            self.counts["reconciled"] += 1
            return
        handler = getattr(self, f"on_{kind.kind}", self.on_other)
        result, line, entry, note = handler(row, kind)
        self.record(row, kind, result, line=line, entry=entry, note=note, key=key, existing=done and done.name)

    def record(self, row, kind, result, line=None, entry=None, note=None, key=None, existing=None) -> None:
        self.counts[{"Reconciled": "reconciled", "Left for Staff": "left_for_staff", "Skipped": "skipped"}.get(result, "open")] += 1
        values = {
            "statement_import": self.doc.name, "mpesa_settings": self.settings, "shortcode": self.shortcode,
            "sub_account": self.sub, "receipt": row.receipt, "completed_at": row.completed_at, "kind": kind.kind,
            "result": result, "paid_in": row.paid_in, "withdrawn": row.withdrawn, "balance": row.balance,
            "details": row.details, "reason_type": cstr(row.reason_type)[:140], "other_party": cstr(row.other_party)[:140],
            "bank_transaction": line, "entry_doctype": entry[0] if entry else None,
            "entry": entry[1] if entry else None, "note": note,
            "row_key": key or row_key(self.shortcode, self.sub, row),
        }
        if existing:
            frappe.db.set_value(ROW, existing, values, update_modified=True)
            return
        doc = frappe.new_doc(ROW)
        doc.update(values)
        doc.flags.ignore_permissions = True
        doc.insert()

    # ------------------------------------------------------------ kinds
    def on_skipped(self, row, kind):
        return "Skipped", None, None, f"Transaction Status {row.status}: nothing moved"

    def on_payout(self, row, kind):
        line, made, note = self.line(self.bank_account, row.receipt, row, self.sub)
        if not line:
            return "Left for Staff", None, None, note
        request = self.request(kind.api, row.receipt)
        if request:
            payouts._link(request, "bank_transaction", line)
        entry = None
        if payouts.reconciling():
            found = payouts.reconcile_withdrawal(line, self.gl)
            entry = found and (self.entry_type(found), found)
        if row.receipt not in self.charged:
            stale = find_existing(self.bank_account, payouts.CHARGE_PREFIX + row.receipt, withdrawal=True)
            if stale:
                charged = flt(frappe.db.get_value(BT, stale, "withdrawal"))
                je = self.charge_entry_name(payouts.CHARGE_PREFIX + row.receipt)
                return "Left for Staff", line, entry, (
                    f"Safaricom charged nothing for this payout, but charge line {stale} says {charged}"
                    + (f" (entry {je})" if je else "") + ": cancel that line and its entry."
                )
        return self.result(line, made), line, entry, None

    def on_charge(self, row, kind):
        key = payouts.CHARGE_PREFIX + row.receipt
        line, made, note = self.line(self.bank_account, key, row, self.sub,
                                     text=f"Safaricom charge on M-Pesa {kind.api or ''} {row.receipt}".replace("  ", " "))
        if not line:
            return "Left for Staff", None, None, note
        amount = flt(frappe.db.get_value(BT, line, "withdrawal"))
        if abs(amount - row.withdrawn) > TOLERANCE:
            return "Left for Staff", line, None, (
                f"Line {line} says {amount}; the statement charged {row.withdrawn}. Correct the line, then import again."
            )
        request = self.request(kind.api, row.receipt)
        if request:
            payouts._link(request, "charge_bank_transaction", line)
            payouts._link(request, "mpesa_charge", row.withdrawn)
        if not enabled("book_payout_charges"):
            return self.result(line, made), line, None, "Book Safaricom Charges Automatically is off"
        # no payout request (made on the portal, or before the suite): the line stands in for it
        payout = request or frappe._dict(doctype=BT, name=line, receipt=row.receipt, mpesa_settings=self.settings)
        before = self.charge_entry_name(key)
        je = payouts.charge_entry(payout, self.bank_account, line, row.withdrawn)
        if je and not before:
            self.counts["entries_made"] += 1
        if je and payouts.reconciling():
            payouts.reconcile_withdrawal(line, self.gl)
        return self.result(line, made), line, je and ("Journal Entry", je), None

    def on_receipt(self, row, kind):
        line, made, note = self.line(self.bank_account, row.receipt, row, self.sub, withdrawal=False)
        if not line:
            return "Left for Staff", None, None, note
        entry = None
        if enabled("auto_reconcile_bank_transactions"):
            from ..money_in.reconcile import reconcile_line

            bt = frappe.db.get_value(BT, line, ["name", "reference_number", "transaction_id", "deposit",
                                                "unallocated_amount"], as_dict=True)
            if flt(bt.unallocated_amount) > TOLERANCE:
                found = reconcile_line(bt, self.gl)
                entry = found and (PE, found)
        return self.result(line, made), line, entry, None

    def on_transfer(self, row, kind):
        if not kind.from_sub or not kind.to_sub:
            line, made, note = self.line(self.bank_account, row.receipt, row, self.sub, withdrawal=row.amount < 0)
            return "Left for Staff", line, None, note or "A transfer whose other account the statement does not name"
        from_account = accounts.bank_account(self.settings, kind.from_sub)
        to_account = accounts.bank_account(self.settings, kind.to_sub)
        if from_account == to_account:
            return "Skipped", None, None, (
                f"{kind.from_sub} to {kind.to_sub}: both on {from_account}, so nothing moved between Bank Accounts"
            )
        amount = abs(row.amount)
        out_line, out_made, note = self.line(from_account, row.receipt, row, kind.from_sub, withdrawal=True,
                                             amount=amount)
        in_line, in_made, note_in = self.line(to_account, row.receipt, row, kind.to_sub, withdrawal=False,
                                              amount=amount)
        if not out_line or not in_line:
            return "Left for Staff", out_line or in_line, None, note or note_in
        from_gl = frappe.db.get_value("Bank Account", from_account, "account")
        to_gl = frappe.db.get_value("Bank Account", to_account, "account")
        entry = self.transfer_entry(row, amount, from_gl, to_gl, f"{kind.from_sub} to {kind.to_sub}")
        if not entry:
            return "Left for Staff", self.own_side(out_line, in_line), None, "Transfer entry not booked: see Error Log"
        done = self.reconcile_both(out_line, in_line, entry, amount)
        line = self.own_side(out_line, in_line)
        return ("Reconciled" if done else self.result(line, out_made or in_made)), line, (PE, entry), None

    def on_funding(self, row, kind):
        return self.with_bank(row, money_in=True)

    def on_withdrawal(self, row, kind):
        return self.with_bank(row, money_in=False)

    def on_reversal(self, row, kind):
        line, made, note = self.line(self.bank_account, row.receipt, row, self.sub, withdrawal=row.amount < 0)
        return "Left for Staff", line, None, note or "A reversal: book and reconcile it by hand"

    def on_other(self, row, kind):
        if not row.amount:
            return "Skipped", None, None, "Nothing moved"
        line, made, note = self.line(self.bank_account, row.receipt, row, self.sub, withdrawal=row.amount < 0)
        return "Left for Staff", line, None, note or "Not a kind of movement the import books: book it by hand"

    # ------------------------------------------------------------ bank funding / withdrawals
    def with_bank(self, row, money_in: bool):
        """Money from (or to) one of the company's banks: the M-Pesa line, then the bank's own line."""
        amount = abs(row.amount)
        line, made, note = self.line(self.bank_account, row.receipt, row, self.sub, withdrawal=not money_in)
        if not line:
            return "Left for Staff", None, None, note
        if self.unallocated(line) <= TOLERANCE:
            return "Reconciled", line, None, None
        existing = frappe.db.get_value(PE, {"reference_no": row.receipt, "docstatus": 1, "payment_type": "Internal Transfer"},
                                       "name", order_by="creation asc")
        booked, hint = (None, None) if existing else self.booked_entry(row, amount, money_in)
        if booked:
            # the entry staff already made for this money (e.g. Dr M-Pesa / Cr KCB, "Transfer to B2C")
            done = self.reconcile_with(line, booked[0], booked[1], amount)
            return ("Reconciled" if done else self.result(line, made)), line, booked, (
                None if done else f"{booked[0]} {booked[1]} is this money but could not be matched: match it by hand"
            )
        if existing:
            bank_line = None
        else:
            candidates = self.bank_lines(amount, row, money_in)
            if len(candidates) != 1:
                what = "money in from a bank" if money_in else "money out to a bank"
                found = ", ".join(f"{c.name} ({c.bank_account})" for c in candidates[:5])
                count = "no" if not candidates else str(len(candidates))
                return self.result(line, made), line, None, hint or (
                    f"{what}: no entry in {self.gl} and {count} unmatched bank line of {amount} within "
                    f"{self.days()} days" + (": " + found if found else "") + f". Book it (bank to {self.sub}) or "
                    "import that bank's statement, then import this statement again."
                )
            bank_line = candidates[0]
            bank_gl = frappe.db.get_value("Bank Account", bank_line.bank_account, "account")
            from_gl, to_gl = (bank_gl, self.gl) if money_in else (self.gl, bank_gl)
            existing = self.transfer_entry(row, amount, from_gl, to_gl, f"bank {'to' if money_in else 'from'} {self.sub}")
            if not existing:
                return "Left for Staff", line, None, "Transfer entry not booked: see Error Log"
        done = self.reconcile_with(line, PE, existing, amount)
        if bank_line:
            done = self.reconcile_with(bank_line.name, PE, existing, amount) and done
        return ("Reconciled" if done else self.result(line, made)), line, (PE, existing), None

    def booked_entry(self, row, amount: float, money_in: bool):
        """((doctype, name), None) for the one submitted entry that already moved exactly this
        money into (or out of) the wallet's GL around the row's date and is not matched with a line
        on this account yet; else (None, a hint for staff)."""
        free, taken = self.entries_moving(self.gl, row, amount, money_in)
        if len(free) > 1:
            free = self.named(free, row) or free
        if len(free) == 1:
            return free[0][:2], None
        if free:
            names = ", ".join(v[1] for v in free[:4])
            return None, f"{len(free)} entries of {amount} went into {self.gl} around then ({names}): match the right one by hand"
        if taken:
            doctype, name, line = taken[0]
            return None, (f"{doctype} {name} ({amount}) is already matched to line {line} on {self.bank_account}: if "
                          "that line is this same money (an older hand-made top-up line), cancel one of the two lines")
        if self.main_gl and self.main_gl != self.gl:
            elsewhere, _ = self.entries_moving(self.main_gl, row, amount, money_in, check_lines=False)
            if len(elsewhere) == 1:
                doctype, name = elsewhere[0][:2]
                return None, (f"{doctype} {name} put {amount} into {self.main_gl}, not {self.gl} where {self.sub} "
                              f"posts: amend it to {self.gl}, then import this statement again")
        return None, None

    def entries_moving(self, gl, row, amount, money_in, check_lines=True):
        """Submitted Journal / Payment Entries moving ``amount`` into (or out of) ``gl`` within the
        window: (free, taken), taken ones being already matched with a line on this account."""
        day = getdate(row.completed_at)
        into = "debit - credit" if money_in else "credit - debit"
        vouchers = frappe.db.sql(
            f"""SELECT voucher_type, voucher_no, SUM({into}) AS moved FROM `tabGL Entry`
            WHERE account = %(gl)s AND is_cancelled = 0 AND voucher_type IN ('Journal Entry', 'Payment Entry')
              AND posting_date BETWEEN %(start)s AND %(end)s
            GROUP BY voucher_type, voucher_no
            HAVING ABS(SUM({into}) - %(amount)s) <= {TOLERANCE}""",
            {"gl": gl, "start": add_days(day, -self.days()), "end": add_days(day, 1), "amount": amount},
            as_dict=True,
        )
        free, taken = [], []
        for v in vouchers:
            field = "reference_no" if v.voucher_type == PE else "cheque_no"
            reference = cstr(frappe.db.get_value(v.voucher_type, v.voucher_no, field)).strip()
            if reference != row.receipt and (reference in self.receipts or reference.startswith(payouts.CHARGE_PREFIX)):
                continue  # another M-Pesa movement's own entry (a transfer, a payout, a charge)
            line = check_lines and frappe.db.sql(
                """SELECT bt.name FROM `tabBank Transaction Payments` p
                JOIN `tabBank Transaction` bt ON bt.name = p.parent
                WHERE p.payment_document = %s AND p.payment_entry = %s AND bt.bank_account = %s AND bt.docstatus = 1
                LIMIT 1""",
                (v.voucher_type, v.voucher_no, self.bank_account),
            )
            if line:
                taken.append((v.voucher_type, v.voucher_no, line[0][0]))
            else:
                free.append((v.voucher_type, v.voucher_no, None))
        return free, taken

    def named(self, vouchers, row) -> list:
        """Among several entries, the one whose remarks name M-Pesa, the shortcode or the receipt."""
        words = ("b2c", "mpesa", "m-pesa", "safaricom", self.shortcode.lower(), row.receipt.lower())
        out = []
        for doctype, name, extra in vouchers:
            remark = cstr(frappe.db.get_value(doctype, name, "user_remark" if doctype == "Journal Entry" else "remarks"))
            if any(w and w in remark.lower() for w in words):
                out.append((doctype, name, extra))
        return out if len(out) == 1 else []

    def bank_lines(self, amount: float, row, money_in: bool) -> list:
        """Unmatched lines of exactly ``amount`` on the company's other banks, around the row's date."""
        day = getdate(row.completed_at)
        mpesa = mpesa_bank_accounts()
        side = "withdrawal" if money_in else "deposit"
        rows = frappe.get_all(
            BT,
            filters=[
                [BT, "company", "=", self.company],
                [BT, "docstatus", "=", 1],
                [BT, side, ">=", amount - TOLERANCE],
                [BT, side, "<=", amount + TOLERANCE],
                [BT, "unallocated_amount", ">=", amount - TOLERANCE],
                [BT, "unallocated_amount", "<=", amount + TOLERANCE],
                [BT, "date", ">=", add_days(day, -self.days())],
                [BT, "date", "<=", add_days(day, 1)],
            ],
            fields=["name", "bank_account", "date", "description", "reference_number"],
            order_by="date asc, creation asc",
            limit=20,
        )
        rows = [r for r in rows if r.bank_account not in mpesa]
        if len(rows) > 1:
            # several of that amount: the one whose narration names M-Pesa, the shortcode or the receipt
            words = ("safaricom", "mpesa", "m-pesa", self.shortcode.lower(), row.receipt.lower())
            named = [r for r in rows if any(w and w in f"{r.description} {r.reference_number}".lower() for w in words)]
            if len(named) == 1:
                return named
        return rows

    def days(self) -> int:
        return suite_int("bank_match_days", 3)

    # ------------------------------------------------------------ helpers
    def line(self, bank_account, key, row, sub, withdrawal=True, amount=None, text=None):
        """(line, made, note): the live withdrawal / deposit line for ``key`` on ``bank_account``."""
        amount = abs(row.amount) if amount is None else amount
        existing = find_existing(bank_account, key, withdrawal)
        if existing:
            _stamp(existing, sub, row.completed_at)  # a line made without them (Astral's scripts)
            self.counts["lines_found"] += 1
            return existing, False, None
        elsewhere = self.line_elsewhere(bank_account, key, withdrawal)
        if elsewhere:
            return None, False, (
                f"{key} is already on {elsewhere.name} ({elsewhere.bank_account}), not on {bank_account} where "
                f"{sub} posts: move that line, then import again"
            )
        name = create_statement_line(
            bank_account=bank_account,
            receipt=key,
            amount=amount,
            date=getdate(row.completed_at),
            description=text or " | ".join(filter(None, [row.details, row.other_party, f"M-Pesa {sub}"])),
            transaction_type=cstr(row.reason_type or row.details)[:50],
            withdrawal=withdrawal,
            sub_account=sub,
            completed_at=row.completed_at,
        )
        self.counts["lines_made"] += 1
        return name, True, None

    def line_elsewhere(self, bank_account, key, withdrawal):
        others = sorted(mpesa_bank_accounts() - {bank_account})
        if not others:
            return None
        for field in ("reference_number", "transaction_id"):
            row = frappe.db.get_value(
                BT,
                {field: key, "bank_account": ["in", others], "withdrawal" if withdrawal else "deposit": [">", 0],
                 "docstatus": ["<", 2]},
                ["name", "bank_account"],
                as_dict=True,
                order_by="creation asc",
            )
            if row:
                return row
        return None

    def request(self, api, receipt):
        """The suite's payout request for this receipt (B2C or B2B), as money_out.statement loads it."""
        for doctype in ((B2C,) if api == "B2C" else (B2B,) if api == "B2B" else (B2C, B2B)):
            name = frappe.db.get_value(doctype, {"transaction_id": receipt, "docstatus": 1}, "name")
            if name:
                return payouts.load(doctype, name)
        return None

    def transfer_entry(self, row, amount, from_gl, to_gl, what) -> str | None:
        """Internal Transfer Payment Entry for ``row``'s receipt (one per receipt)."""
        with named_lock(f"xfer:{row.receipt}"):
            existing = frappe.db.get_value(
                PE, {"reference_no": row.receipt, "payment_type": "Internal Transfer", "docstatus": ["<", 2]}, "name",
                order_by="docstatus desc, creation asc", for_update=True,
            )
            if existing:
                return existing
            pe = frappe.new_doc(PE)
            pe.update({
                "payment_type": "Internal Transfer", "company": self.company, "posting_date": getdate(row.completed_at),
                "paid_from": from_gl, "paid_to": to_gl, "paid_amount": amount, "received_amount": amount,
                "source_exchange_rate": 1, "target_exchange_rate": 1, "reference_no": row.receipt,
                "reference_date": getdate(row.completed_at),
                "remarks": f"M-Pesa {self.shortcode} {what}, receipt {row.receipt}: {row.details}"[:500],
            })
            pe.flags.ignore_permissions = True
            savepoint = "mpesa_transfer_entry"
            frappe.db.savepoint(savepoint)
            try:
                pe.set_missing_values()
                pe.insert()
                if cint(frappe.db.get_single_value("M-Pesa Suite Settings", "submit_statement_entries")):
                    pe.submit()
            except Exception:
                frappe.db.rollback(save_point=savepoint)
                frappe.log_error(title=f"M-Pesa transfer entry for {row.receipt} failed", message=frappe.get_traceback())
                return None
            self.counts["entries_made"] += 1
            return pe.name

    def reconcile_both(self, out_line, in_line, entry, amount) -> bool:
        a = self.reconcile_with(out_line, PE, entry, amount)
        b = self.reconcile_with(in_line, PE, entry, amount)
        return a and b

    def reconcile_with(self, line, doctype, entry, amount) -> bool:
        """Reconcile ``line`` with ``entry`` when it is submitted and the line still has exactly
        ``amount`` open. True when the line is matched (now or before)."""
        if not payouts.reconciling() or frappe.db.get_value(doctype, entry, "docstatus") != 1:
            return False
        from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import reconcile_vouchers

        bt = frappe.db.get_value(BT, line, ["unallocated_amount", "docstatus"], as_dict=True, for_update=True)
        if not bt or bt.docstatus != 1:
            return False
        if frappe.db.exists("Bank Transaction Payments", {"parent": line, "payment_document": doctype,
                                                          "payment_entry": entry}):
            return flt(bt.unallocated_amount) <= TOLERANCE
        if abs(flt(bt.unallocated_amount) - amount) > TOLERANCE:
            return False
        with as_user("Administrator"):
            reconcile_vouchers(line, json.dumps([{"payment_doctype": doctype, "payment_name": entry, "amount": amount}]))
        return True

    def unallocated(self, line) -> float:
        return flt(frappe.db.get_value(BT, line, "unallocated_amount"))

    def result(self, line, made) -> str:
        if line and self.unallocated(line) <= TOLERANCE:
            return "Reconciled"
        return "Line Made" if made else "Already There"

    def own_side(self, out_line, in_line):
        """The line on this statement's own Bank Account."""
        return out_line if frappe.db.get_value(BT, out_line, "bank_account") == self.bank_account else in_line

    def entry_type(self, name) -> str:
        return PE if frappe.db.exists(PE, name) else "Journal Entry"

    def charge_entry_name(self, key):
        return frappe.db.get_value("Journal Entry", {"cheque_no": key, "docstatus": ["<", 2]}, "name")

    # ------------------------------------------------------------ tie-out
    def tie_out(self) -> str:
        st = self.st
        notes = []
        paid_in = round(sum(r.paid_in for r in st.rows), 2)
        withdrawn = round(sum(r.withdrawn for r in st.rows), 2)
        if st.total_paid_in is not None and abs(paid_in - flt(st.total_paid_in)) > 0.01:
            notes.append(f"Rows add up to {paid_in} paid in, the header says {st.total_paid_in}.")
        if st.total_withdrawn is not None and abs(withdrawn - abs(flt(st.total_withdrawn))) > 0.01:
            notes.append(f"Rows add up to {withdrawn} withdrawn, the header says {abs(flt(st.total_withdrawn))}.")
        closing = st.rows[0].balance if st.rows and st.rows[0].balance is not None else st.closing_balance
        if not accounts.separate(self.settings, self.sub) and self.sub != accounts.receiving(self.settings):
            notes.append(
                f"{self.sub} shares {self.bank_account} with the shortcode's other sub-accounts, so its balance cannot "
                f"be compared on its own: map {self.sub} to a Bank Account of its own (Mpesa Settings → Sub-Accounts)."
            )
        elif closing is not None and st.period_to:
            receiving = accounts.receiving(self.settings)
            shared = [s for s in (accounts.WORKING, accounts.UTILITY)
                      if s not in (self.sub, receiving) and not accounts.separate(self.settings, s)]
            from erpnext.accounts.utils import get_balance_on

            books = flt(get_balance_on(account=self.gl, date=getdate(st.rows[0].completed_at or st.period_to),
                                       company=self.company))
            diff = round(flt(closing) - books, 2)
            notes.append(
                f"Statement balance {flt(closing):,.2f}; books ({self.gl}) on that date {books:,.2f}"
                + (": they agree." if abs(diff) <= 0.01 else
                   f": {abs(diff):,.2f} {'more' if diff > 0 else 'less'} on the statement. Lines still open in Bank "
                   "Reconciliation, or an opening balance not yet posted to the GL, explain it.")
                + (f" The books also hold {', '.join(shared)}, which have no Bank Account of their own."
                   if shared and abs(diff) > 0.01 else "")
            )
        return " ".join(notes) or "Nothing to compare."
