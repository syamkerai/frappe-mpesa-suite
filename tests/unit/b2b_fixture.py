"""An in-memory site for the B2B payouts tests (money_out.owed, b2b, b2b_entries).

frappe.db.sql is answered by SQLite from fake_frappe's tables, rebuilt for every query, so the
SQL the code really sends is what the tests check. MariaDB-only parts are mapped: FOR UPDATE is
dropped (the read is logged as locking), %s / %(name)s become SQLite parameters and a tuple
parameter is spread out for IN. Every read is logged in order, so the lock order (R4) can be
checked. New Payment / Journal Entries land in the same tables.
"""

import contextlib
import datetime
import importlib
import re
import sqlite3
import sys
import types
from decimal import Decimal

import fake_frappe

B2B = "Mpesa B2B Payment Request"
PE, PER, JE, JEA = "Payment Entry", "Payment Entry Reference", "Journal Entry", "Journal Entry Account"
SETTINGS = "Astral 600999"
BANK_ACCOUNT, GL = "Mpesa 600999", "Mpesa 600999 - AIL"
NOW = datetime.datetime(2026, 10, 18, 12, 0, 0)  # fake_frappe.utils.now_datetime
TODAY = NOW.date()

COLUMNS = {
    B2B: ("name", "docstatus", "status", "amount", "transaction_id", "purchase_invoice", "purchase_order",
          "supplier", "company", "creation", "entry_status", "entry_note", "entry_mode", "payment_entry",
          "journal_entry", "entry_attempted_on", "entry_alerted_on", "trans_completed_time", "mpesa_settings",
          "payment_schedule", "schedule_line", "send_started_on", "debit_account_balance", "employee",
          "employee_mpesa_account", "salary_slip", "payroll_entry"),
    PE: ("name", "docstatus", "payment_type", "party_type", "party", "paid_from", "paid_to", "paid_amount",
         "unallocated_amount", "reference_no", "posting_date", "creation"),
    PER: ("name", "parent", "idx", "reference_doctype", "reference_name", "allocated_amount",
          "advance_voucher_type", "advance_voucher_no", "payment_term"),
    JE: ("name", "docstatus", "cheque_no", "creation", "voucher_type"),
    JEA: ("name", "parent", "idx", "account", "party_type", "party", "reference_type", "reference_name",
          "debit_in_account_currency", "credit_in_account_currency", "b2c_payment_disbursement",
          "mpesa_b2b_payment_request"),
    "Purchase Invoice Item": ("name", "parent", "parenttype", "purchase_order"),
    "Payment Schedule": ("name", "parent", "parenttype", "idx", "payment_term", "due_date", "payment_amount",
                         "paid_amount", "outstanding"),
    "B2C Disbursement Request": ("name", "docstatus", "status", "reference_doctype", "reference_name",
                                 "transaction_id", "mpesa_settings", "amount", "creation",
                                 "transaction_completed_datetime", "working_acct_avlbl_funds",
                                 "utility_acct_avlbl_funds", "send_started_on", "b2c_payment_disbursement",
                                 "b2c_payment_disbursement_reference"),
    "B2C Payment Disbursement": ("name", "docstatus", "payment_schedule", "company"),
    "B2C Payment Disbursement Reference": (
        "name", "parent", "parenttype", "idx", "reference_doctype", "reference_name", "party_type", "party",
        "allocated_amount", "partyb", "payment_status", "b2c_disbursement_request", "mpesa_schedule_line",
        "reference_no", "payroll_entry"),
    "M-Pesa Payment Schedule": ("name", "title", "company", "b2b_settings", "b2c_settings", "purpose",
                                "payroll_doctype", "payroll_entry", "closed", "status", "modified"),
    "M-Pesa Payment Schedule Line": (
        "name", "parent", "parenttype", "idx", "modified", "planned_date", "party_type", "party", "payee_name",
        "payee_account_doctype", "payee_account", "route", "destination", "one_off_type", "one_off_shortcode",
        "one_off_account", "amount", "reference_doctype", "reference_name", "entry_mode", "remarks", "status",
        "request", "receipt", "paid_on", "entry_status", "last_problem", "prepared_by", "changed_by", "approved_by",
        "approved_on", "batch", "attempts", "b2c_disbursement", "b2c_row", "b2c_request"),
}


def _value(v):
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, str)) or v is None:
        return v
    return None  # callables, lists...: not columns


class SQL:
    """frappe.db.sql for the fake site."""

    def __init__(self, frappe, log):
        self.frappe = frappe
        self.log = log

    def __call__(self, query, values=None, as_dict=False, pluck=False, **kw):
        tables = list(dict.fromkeys(re.findall(r"`tab([^`]+)`", query)))
        locking = bool(re.search(r"\bFOR\s+UPDATE\b", query, re.I))
        self.log.append(("sql", tuple(tables), locking))
        query = re.sub(r"\bFOR\s+UPDATE\b", "", query, flags=re.I)
        query, params = self._params(query, values)
        con = sqlite3.connect(":memory:")
        try:
            for doctype in tables:
                self._load(con, doctype)
            cur = con.execute(query, params)
            names = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        finally:
            con.close()
        if pluck:
            return [r[0] for r in rows]
        if as_dict:
            return [fake_frappe._dict(zip(names, r)) for r in rows]
        return [tuple(r) for r in rows]

    @staticmethod
    def _params(query, values):
        query = query.replace("%%", "%")
        if isinstance(values, dict):
            params = {}

            def named(match):
                key = match.group(1)
                value = values[key]
                if isinstance(value, (list, tuple, set)):
                    keys = [f"{key}_{i}" for i in range(len(value))]
                    params.update({k: _value(v) for k, v in zip(keys, value)})
                    return "(" + ", ".join(f":{k}" for k in keys) + ")"
                params[key] = _value(value)
                return f":{key}"

            return re.sub(r"%\((\w+)\)s", named, query), params
        values = list(values or ())
        params, it = [], iter(values)

        def positional(match):
            value = next(it)
            if isinstance(value, (list, tuple, set)):
                params.extend(_value(v) for v in value)
                return "(" + ", ".join("?" for _ in value) + ")"
            params.append(_value(value))
            return "?"

        return re.sub(r"%s", positional, query), params

    def _load(self, con, doctype):
        rows = list(self.frappe.db.table(doctype).values())
        columns = list(COLUMNS.get(doctype, ("name",)))
        columns += [c for c in ("owner",) if c not in columns]  # salary_access lets a record's creator see it
        for row in rows:
            columns += [k for k in row if k not in columns]
        con.execute(f"CREATE TABLE `tab{doctype}` ({', '.join(f'`{c}`' for c in columns)})")
        for row in rows:
            con.execute(
                f"INSERT INTO `tab{doctype}` VALUES ({', '.join('?' for _ in columns)})",
                [_value(row.get(c)) for c in columns],
            )


class NewDoc(fake_frappe.FakeDoc):
    """frappe.new_doc for Payment and Journal Entries: rows land in the fake tables on insert."""

    def __init__(self, env, doctype):
        super().__init__(env.frappe, doctype, {"doctype": doctype})
        object.__setattr__(self, "_env", env)
        object.__setattr__(self, "flags", fake_frappe._dict())
        fields = env.meta_fields.get(doctype)
        object.__setattr__(self, "meta", types.SimpleNamespace(
            has_field=lambda f: True if fields is None else f in fields))

    def append(self, table, row):
        self._row.setdefault(table, []).append(fake_frappe._dict(row))

    def set(self, key, value):
        self._row[key] = value

    def insert(self, ignore_permissions=False):
        env = self._env
        env.inserted.append((self.doctype, env.frappe.session.user, ignore_permissions))
        if env.fail_insert:
            raise env.fail_insert
        self._row["name"] = self._row.get("name") or f"{'ACC-PAY' if self.doctype == PE else 'ACC-JV'}-{len(env.inserted):04d}"
        self._row.setdefault("docstatus", 0)
        self._row.setdefault("creation", NOW)
        self._store()
        return self

    def submit(self):
        self._row["docstatus"] = 1
        self._store()

    def _store(self):
        db, name = self._env.frappe.db, self._row["name"]
        if self.doctype == PE:
            refs = self._row.get("references") or []
            db.table(PE)[name] = {**{k: v for k, v in self._row.items() if k != "references"},
                                  "unallocated_amount": float(self._row["paid_amount"]) - sum(
                                      float(r.allocated_amount) for r in refs)}
            for i, ref in enumerate(refs, 1):
                db.table(PER)[f"{name}-{i}"] = {"name": f"{name}-{i}", "parent": name, "idx": i, **ref}
        else:
            db.table(JE)[name] = {k: v for k, v in self._row.items() if k != "accounts"}
            for i, row in enumerate(self._row.get("accounts") or [], 1):
                db.table(JEA)[f"{name}-{i}"] = {"name": f"{name}-{i}", "parent": name, "idx": i,
                                                "debit_in_account_currency": 0, "credit_in_account_currency": 0,
                                                **row}


@contextlib.contextmanager
def _stub_modules(modules):
    saved = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _getdate(v=None):
    if v is None or v == "":
        return TODAY
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    return datetime.date.fromisoformat(str(v)[:10])


def _flt(value, precision=None):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(number, precision) if precision is not None else number


@contextlib.contextmanager
def site(monkeypatch):
    """The fake site and the modules under test, imported fresh."""
    frappe, saved = fake_frappe.install()
    stubs = {}
    b2c_api = types.ModuleType("mpsa_suite.mpsa_suite.api.b2c")
    b2c_api.make_b2c_payment_request = lambda name: None
    stubs[b2c_api.__name__] = b2c_api
    query_builder = types.ModuleType("frappe.query_builder")
    query_builder.DocType = lambda name: None
    stubs["frappe.query_builder"] = query_builder
    erpnext = types.ModuleType("erpnext")
    erpnext.get_default_cost_center = lambda company: "Main - AIL"
    party = types.ModuleType("erpnext.accounts.party")
    party.get_party_account = lambda party_type, party_name, company, include_advance=False: "Creditors - AIL"
    dims = types.ModuleType("erpnext.accounts.doctype.accounting_dimension.accounting_dimension")
    dims.get_accounting_dimensions = lambda as_list=True: ["project"]
    for name in ("erpnext.accounts", "erpnext.accounts.doctype", "erpnext.accounts.doctype.accounting_dimension"):
        stubs[name] = types.ModuleType(name)
    stubs.update({"erpnext": erpnext, party.__name__: party, dims.__name__: dims})
    try:
        with _stub_modules(stubs):
            frappe.utils.getdate = _getdate
            frappe.utils.flt = _flt
            frappe.utils.nowdate = lambda: TODAY.isoformat()
            frappe.utils.now = lambda: str(NOW)
            frappe.utils.logger = types.SimpleNamespace(set_log_level=lambda *a, **kw: None)
            frappe.logger = lambda *a, **kw: types.SimpleNamespace(debug=print, info=print, error=print)
            frappe.db.db_type = "mariadb"
            frappe.db.columns = {(PER, "advance_voucher_no")}
            frappe.db.commit = lambda: env.log.append(("commit",))
            frappe.db.savepoint = lambda name: env.log.append(("savepoint", name))
            frappe.db.rollback = lambda save_point=None: env.log.append(("rollback", save_point))
            env = fake_frappe._dict(frappe=frappe, log=[], locks=[], inserted=[], fail_insert=None, alerts=[],
                                    fed=[], b2c=[], b2c_paid=[], meta_fields={}, denied=set(), users=[],
                                    busy_keys=set())
            frappe.db.sql = SQL(frappe, env.log)
            plain_get_value = frappe.db.get_value

            def get_value(doctype, filters, fieldname="name", as_dict=False, for_update=False, **kw):
                if isinstance(filters, str):
                    env.log.append(("row", doctype, filters, bool(for_update)))
                return plain_get_value(doctype, filters, fieldname, as_dict=as_dict, for_update=for_update, **kw)

            frappe.db.get_value = get_value

            def get_meta(doctype):
                fields = env.meta_fields.get(doctype)
                return types.SimpleNamespace(has_field=lambda f: True if fields is None else f in fields)

            def has_permission(doctype, ptype="read", doc=None, throw=False, **kw):
                ok = (doctype, ptype) not in env.denied
                if not ok and throw:
                    raise frappe.PermissionError(f"no {ptype} on {doctype}")
                return ok

            frappe.get_meta = get_meta
            frappe.has_permission = has_permission
            frappe.new_doc = lambda doctype: NewDoc(env, doctype)

            common = importlib.import_module("mpsa_suite.money_in.common")

            @contextlib.contextmanager
            def named_lock(key, timeout=10, busy=None):
                if key in env.busy_keys:  # another worker holds it past the timeout
                    raise frappe.ValidationError(busy or f"Could not acquire lock {key}")
                if key not in env.locks:  # held until commit; re-entering is a no-op
                    env.locks.append(key)
                    env.log.append(("lock", key))
                yield

            monkeypatch.setattr(common, "named_lock", named_lock)
            env.owed = importlib.import_module("mpsa_suite.money_out.owed")
            env.b2b = importlib.import_module("mpsa_suite.money_out.b2b")
            env.entries = importlib.import_module("mpsa_suite.money_out.b2b_entries")
            env.statement = importlib.import_module("mpsa_suite.money_out.statement")
            sa = importlib.import_module("mpsa_suite.money_out.supplier_accounts")
            disb = importlib.import_module(
                "mpsa_suite.mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement")
            req = importlib.import_module(
                "mpsa_suite.mpsa_suite.doctype.b2c_disbursement_request.b2c_disbursement_request")

            def open_payouts(doctype, name, exclude_parent=None, exclude_requests=None):
                env.log.append(("b2c_open_payouts", doctype, name))
                return [fake_frappe._dict(r) for r in env.b2c if (r["dt"], r["dn"]) == (doctype, name)]

            def paid_without_entry(doctype, name):
                env.log.append(("b2c_paid_without_entry", doctype, name))
                return [fake_frappe._dict(r) for r in env.b2c_paid if (r["dt"], r["dn"]) == (doctype, name)]

            monkeypatch.setattr(disb, "open_payouts", open_payouts)
            monkeypatch.setattr(req, "paid_without_entry", paid_without_entry)
            monkeypatch.setattr(sa, "_alert", lambda message, doc: env.alerts.append((doc.name, message)))
            monkeypatch.setattr(env.statement, "after_payout", lambda doctype, name: env.fed.append(name))
            monkeypatch.setattr(env.statement, "payout_bank_account", lambda p: BANK_ACCOUNT)
            real_set_user = frappe.set_user
            frappe.set_user = lambda user: env.users.append(user) or real_set_user(user)

            db = frappe.db
            db.table("Bank Account")[BANK_ACCOUNT] = {"name": BANK_ACCOUNT, "account": GL, "company": "Astral"}
            db.table("Account")[GL] = {"name": GL, "company": "Astral", "account_currency": "KES",
                                       "account_type": "Bank", "report_type": "Balance Sheet"}
            db.table("Account")["Creditors - AIL"] = {"name": "Creditors - AIL", "company": "Astral",
                                                      "account_currency": "KES", "account_type": "Payable"}
            db.table("Mpesa Settings")[SETTINGS] = {
                "name": SETTINGS, "business_shortcode": "600999", "sandbox": 0, "transaction_limit": 250000,
                "use_b2b": 1, "company": "Astral", "cost_center": None, "submit_b2c_accounting_entries": 1,
                "initiator_name": "apiop"}
            db.table("Mode of Payment")[f"Mpesa-{SETTINGS}"] = {"name": f"Mpesa-{SETTINGS}"}
            db.table("M-Pesa Suite Settings")["M-Pesa Suite Settings"] = {"allow_one_off_b2b": 0}
            db.table("Supplier")["ACME"] = {"name": "ACME", "on_hold": 0}
            db.table("Supplier M-Pesa Account")["MSA-1"] = {
                "name": "MSA-1", "docstatus": 1, "supplier": "ACME", "account_type": "Paybill", "shortcode": "888880",
                "account_number": "ACME-1", "expected_name": "ACME", "active_from": datetime.datetime(2026, 10, 1),
                "is_default": 1, "name_mismatch": 0}
            env.pi = lambda name="PINV-1", **kw: _row(env, "Purchase Invoice", name, {
                "docstatus": 1, "supplier": "ACME", "company": "Astral", "currency": "KES",
                "party_account_currency": "KES", "is_return": 0, "on_hold": 0, "outstanding_amount": 10000,
                "credit_to": "Creditors - AIL", "cost_center": "Branch - AIL", "project": None,
                "payment_terms_template": None}, kw)
            env.po = lambda name="PO-1", **kw: _row(env, "Purchase Order", name, {
                "docstatus": 1, "supplier": "ACME", "company": "Astral", "currency": "KES",
                "party_account_currency": "KES", "status": "To Receive and Bill", "per_billed": 0,
                "rounded_total": 20000, "grand_total": 20000, "advance_paid": 0, "cost_center": None,
                "project": None, "payment_terms_template": None}, kw)
            env.request = lambda name="MB2B-1", **kw: _row(env, B2B, name, {
                "docstatus": 1, "status": "Paid", "amount": 4000, "transaction_id": f"R{name[-4:]}X", "supplier": "ACME",
                "company": "Astral", "mpesa_settings": SETTINGS, "command_id": "BusinessPayBill",
                "receiver_shortcode": "888880", "account_reference": "ACME-1", "supplier_mpesa_account": "MSA-1",
                "creation": datetime.datetime(2026, 10, 17, 9, 0), "remarks": "test",
                "trans_completed_time": datetime.datetime(2026, 10, 17, 9, 0, 5), "entry_mode": None}, kw)
            env.pe = lambda name, receipt, refs=(), **kw: _entry(env, name, receipt, refs, kw)
            env.je = lambda name, receipt, rows, **kw: _journal(env, name, receipt, rows, kw)
            yield env
    finally:
        fake_frappe.uninstall(saved)


def _row(env, doctype, name, defaults, values):
    row = {"name": name, **defaults, **values}
    env.frappe.db.table(doctype)[name] = row
    return row


def _entry(env, name, receipt, refs, values):
    """A Payment Entry carrying ``receipt``: refs are (doctype, name, amount[, advance doctype, advance name])."""
    row = {"name": name, "docstatus": 1, "payment_type": "Pay", "party_type": "Supplier", "party": "ACME",
           "paid_from": GL, "paid_amount": 4000, "reference_no": receipt, "creation": NOW, **values}
    row.setdefault("unallocated_amount", row["paid_amount"] - sum(r[2] for r in refs))
    env.frappe.db.table(PE)[name] = row
    for i, ref in enumerate(refs, 1):
        adv = ref[3:5] if len(ref) > 3 else (None, None)
        env.frappe.db.table(PER)[f"{name}-{i}"] = {
            "name": f"{name}-{i}", "parent": name, "idx": i, "reference_doctype": ref[0], "reference_name": ref[1],
            "allocated_amount": ref[2], "advance_voucher_type": adv[0], "advance_voucher_no": adv[1]}
    return row


def _journal(env, name, receipt, rows, values):
    """A Journal Entry with Cheque No ``receipt``: rows are dicts of Journal Entry Account."""
    env.frappe.db.table(JE)[name] = {"name": name, "docstatus": 1, "cheque_no": receipt, "creation": NOW, **values}
    for i, row in enumerate(rows, 1):
        env.frappe.db.table(JEA)[f"{name}-{i}"] = {
            "name": f"{name}-{i}", "parent": name, "idx": i, "debit_in_account_currency": 0,
            "credit_in_account_currency": 0, **row}

