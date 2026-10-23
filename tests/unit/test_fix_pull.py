"""Pull imports, on an in-memory frappe.

Covers: a cancelled register still owns its receipt, so Pull, Transaction Status and held
C2B confirmations never import it again (an amended copy is the live register; an STK
prompt still wins); so does a deleted one, through its Deleted Document; each pulled
receipt releases its lock, skipped ones too; the hourly pull reads back 47 hours, so
receipts lost in an outage longer than an hour are recovered, and still pulls the last
2 hours on their own, so routine recovery does not depend on how Daraja pages.
"""

import datetime
import importlib
import json
import sys
import types

import pytest

import fake_frappe

REGISTER = "Mpesa C2B Payment Register"
EXPRESS = "Mpesa Express Request"
IR = "Integration Request"
HELD = "Mpesa C2B Verification"
SETTINGS = "TESTC2B"
SHORTCODE = "174379"


class NewDoc(fake_frappe.FakeDoc):
    def update(self, values):
        self._row.update(values)


@pytest.fixture
def env(monkeypatch):
    frappe, saved = fake_frappe.install()
    # m_pesa_api imports the security-credential helper, which needs `cryptography`
    stub = types.ModuleType("frappe_mpsa_payments.utils.encoding_initiator_password")
    stub.generate_security_credential = lambda *a, **kw: ""
    sys.modules[stub.__name__] = stub
    try:
        frappe.db.db_type = "fake"  # named_lock is a no-op off MariaDB
        frappe.calls = []
        frappe.db.commit = lambda: frappe.calls.append(("commit",))
        frappe.new_doc = lambda doctype: NewDoc(frappe, doctype, {"docstatus": 0, "flags": fake_frappe._dict()})
        frappe.db.table("Mpesa Settings")[SETTINGS] = {
            "name": SETTINGS, "business_shortcode": SHORTCODE, "till_number": "", "sandbox": 0,
            "enable_hourly_pull_transactions": 1,
        }
        mods = fake_frappe._dict(
            frappe=frappe,
            receipts=importlib.import_module("frappe_mpsa_payments.money_in.receipts"),
            c2b_verify=importlib.import_module("frappe_mpsa_payments.money_in.c2b_verify"),
            handler=importlib.import_module("frappe_mpsa_payments.frappe_mpsa_payments.api.mpesa_response_handler"),
            hourly=importlib.import_module("frappe_mpsa_payments.services.b2c_pull_transaction_scheduler_service"),
            queried=[],
        )
        monkeypatch.setattr(mods.c2b_verify, "send_query", lambda ir_name: mods.queried.append(ir_name))
        yield mods
    finally:
        fake_frappe.uninstall(saved)


def register(env, name, transid="QAB12", docstatus=1, **kw):
    env.frappe.db.table(REGISTER)[name] = {"name": name, "transid": transid, "docstatus": docstatus,
                                           "transamount": 5000.0, "billrefnumber": "SINV-0001", **kw}


def registers(env, transid="QAB12"):
    return sorted(r["name"] for r in env.frappe.db.table(REGISTER).values() if r.get("transid") == transid)


# ---------------------------------------------------------------- C17 cancelled register owns its receipt
def test_cancelled_register_still_owns_its_receipt(env):
    # refunded in the M-PESA portal, so staff cancelled the register (and its Payment Entry)
    register(env, "MPC2B-1", docstatus=2)
    outcome, name = env.receipts.insert_register({"transid": "QAB12", "transamount": 5000.0})
    assert (outcome, name) == (env.receipts.CANCELLED, "MPC2B-1")
    assert registers(env) == ["MPC2B-1"]  # no second register to auto-submit and book
    # a locking read, like the live check: a cancel committed after our snapshot is not missed
    assert (REGISTER, "MPC2B-1") in env.frappe.db.locks


def test_amended_copy_is_the_live_register(env):
    register(env, "MPC2B-1", docstatus=2)
    register(env, "MPC2B-1-1", docstatus=1, amended_from="MPC2B-1")
    assert env.receipts.insert_register({"transid": "QAB12"}) == (env.receipts.DUPLICATE, "MPC2B-1-1")
    assert registers(env) == ["MPC2B-1", "MPC2B-1-1"]


@pytest.mark.parametrize("docstatus", [0, 1])
def test_live_register_is_a_duplicate(env, docstatus):
    register(env, "MPC2B-1", docstatus=docstatus)
    assert env.receipts.insert_register({"transid": " QAB12 "}) == (env.receipts.DUPLICATE, "MPC2B-1")


def test_stk_prompt_still_wins_over_a_cancelled_register(env):
    # Express "neutralise" cancels the C2B register; the prompt keeps booking the receipt
    register(env, "MPC2B-1", docstatus=2)
    env.frappe.db.table(EXPRESS)["MER-1"] = {"name": "MER-1", "transaction_id": "QAB12"}
    assert env.receipts.insert_register({"transid": "QAB12"}) == (env.receipts.STK_OWNED, None)


def test_new_receipt_is_inserted(env):
    register(env, "MPC2B-1", transid="OTHER1", docstatus=2)
    outcome, name = env.receipts.insert_register({"transid": " QAB12 ", "transamount": 5000.0})
    assert outcome == env.receipts.INSERTED
    assert registers(env) == [name]
    assert env.frappe.db.table(REGISTER)[name]["transid"] == "QAB12"


# ---------------------------------------------------------------- deleted register owns its receipt
DELETED_DOCUMENT = "Deleted Document"


def deleted(env, name, transid="QAB12", doctype=REGISTER, docstatus=2):
    """What frappe.delete_doc leaves behind: the document as frappe.as_json writes it."""
    data = json.dumps({"doctype": doctype, "name": name, "transid": transid, "docstatus": docstatus,
                       "transamount": 5000.0}, indent=1, sort_keys=True, separators=(",", ": "))
    env.frappe.db.table(DELETED_DOCUMENT)[f"DEL-{name}"] = {
        "name": f"DEL-{name}", "deleted_doctype": doctype, "deleted_name": name, "data": data,
    }


def watch_tombstone_reads(env):
    reads = []
    real_get_all = env.frappe.get_all

    def get_all(doctype, *a, **kw):
        if doctype == DELETED_DOCUMENT:
            reads.append(kw.get("filters"))
        return real_get_all(doctype, *a, **kw)

    env.frappe.get_all = get_all
    return reads


@pytest.mark.parametrize("docstatus", [0, 2])
def test_deleted_register_still_owns_its_receipt(env, docstatus):
    # a System Manager deleted the register (a cancelled one, or a draft) to tidy up
    deleted(env, "MPC2B-1", docstatus=docstatus)
    reads = watch_tombstone_reads(env)
    assert env.receipts.insert_register({"transid": " QAB12 "}) == (env.receipts.DELETED, None)
    assert registers(env) == []  # nothing to auto-submit and book again
    (filters,) = reads
    assert filters["deleted_doctype"] == REGISTER
    # the LIKE finds the receipt as frappe.as_json stores it in the Deleted Document
    op, pattern = filters["data"]
    assert op == "like" and pattern.strip("%") in env.frappe.db.table(DELETED_DOCUMENT)["DEL-MPC2B-1"]["data"]


def test_tombstones_of_other_receipts_or_doctypes_do_not_block(env):
    deleted(env, "MPC2B-1", transid="QAB123")  # a LIKE on "QAB12" alone would catch this one
    deleted(env, "MPC2B-2", transid="QAB1_")
    deleted(env, "SINV-1", doctype="Sales Invoice")
    outcome, name = env.receipts.insert_register({"transid": "QAB12"})
    assert outcome == env.receipts.INSERTED and registers(env) == [name]


def test_restored_register_is_the_live_owner(env):
    deleted(env, "MPC2B-1", docstatus=1)
    register(env, "MPC2B-1", docstatus=1)  # restored from its Deleted Document
    assert env.receipts.insert_register({"transid": "QAB12"}) == (env.receipts.DUPLICATE, "MPC2B-1")


def test_a_receipt_that_is_not_daraja_shaped_never_reaches_the_like(env):
    deleted(env, "MPC2B-1")
    reads = watch_tombstone_reads(env)
    outcome, _name = env.receipts.insert_register({"transid": "%"})  # would match every tombstone
    assert outcome == env.receipts.INSERTED
    assert reads == []


# ---------------------------------------------------------------- Pull import
def pull_response(*transids):
    return {
        "ResponseCode": "1000",
        "Response": [[{"transactionId": t, "trxDate": "2026-10-18T10:00:00+03:00", "msisdn": "254712345678",
                       "transactiontype": "c2b-paybill-debi", "billreference": "SINV-0001", "amount": "5000.00"}
                      for t in transids]],
    }


def run_pull(env, *transids):
    published = []
    env.frappe.publish_realtime = lambda *a, **kw: published.append(kw.get("message"))
    real_insert = env.receipts.insert_register

    def insert_register(values, flags=None):
        env.frappe.calls.append(("insert", values["transid"]))
        return real_insert(values, flags)

    env.receipts.insert_register = insert_register  # the handler imports it at call time
    try:
        env.handler.pull_transaction_on_success(pull_response(*transids), SETTINGS, settings_name=SETTINGS)
    finally:
        env.receipts.insert_register = real_insert
    return published[-1]


def test_pull_skips_a_receipt_whose_register_was_cancelled(env):
    register(env, "MPC2B-1", docstatus=2)
    result = run_pull(env, "QAB12", "QAB13")
    assert registers(env) == ["MPC2B-1"]  # QAB12 not imported again
    assert len(registers(env, "QAB13")) == 1  # a new receipt still is
    assert (result["created"], result["skipped"], result["failed"]) == (1, 1, 0)
    assert env.frappe.db.table("Mpesa Settings")[SETTINGS]["last_pull_status"] == "Success"


def test_pull_skips_a_receipt_whose_register_was_deleted(env):
    # the 47h window re-reads it on every run after the delete
    deleted(env, "MPC2B-1")
    result = run_pull(env, "QAB12", "QAB13")
    assert registers(env) == []
    assert len(registers(env, "QAB13")) == 1
    assert (result["created"], result["skipped"], result["failed"]) == (1, 1, 0)


def test_pull_releases_each_receipts_lock_skipped_ones_too(env):
    # the 47h window re-reads mostly known receipts: their locks must not pile up for the page
    register(env, "MPC2B-1", docstatus=1)
    register(env, "MPC2B-2", transid="QAB13", docstatus=2)
    run_pull(env, "QAB12", "QAB13", "QAB14")
    assert env.frappe.calls == [
        ("insert", "QAB12"), ("commit",),
        ("insert", "QAB13"), ("commit",),
        ("insert", "QAB14"), ("commit",),
        ("commit",),
    ]


# ---------------------------------------------------------------- held C2B confirmations
def test_confirmation_for_a_cancelled_receipt_is_not_held_or_queried(env):
    register(env, "MPC2B-1", docstatus=2)
    assert env.c2b_verify.hold({"transid": "QAB12", "transamount": 5000, "businessshortcode": SHORTCODE}) is None
    assert env.frappe.db.table(IR) == {}
    assert env.queried == []


def test_sweep_closes_a_held_confirmation_whose_register_was_cancelled(env):
    register(env, "MPC2B-1", docstatus=2)
    env.frappe.db.table(IR)["IR-HELD"] = {
        "name": "IR-HELD", "integration_request_service": HELD, "status": "Queued", "request_id": "QAB12",
        "output": json.dumps({"attempts": 1}), "modified": datetime.datetime(2026, 10, 18, 11, 0),
        "creation": datetime.datetime(2026, 10, 18, 10, 0),
    }
    env.c2b_verify.sweep()
    assert env.frappe.db.table(IR)["IR-HELD"]["status"] == "Completed"
    assert env.queried == []  # no Transaction Status query for a receipt that cannot be imported


# ---------------------------------------------------------------- C19 hourly pull window
def hourly_windows(env, monkeypatch, now):
    pulls = []
    monkeypatch.setattr(env.hourly, "now_datetime", lambda: now)
    monkeypatch.setattr(env.hourly, "pull_transactions", lambda **kw: pulls.append(kw))
    env.hourly.run_hourly_pull_transactions()
    return pulls


def parse(value):
    return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def test_hourly_pull_reads_back_47_hours_after_the_last_2_on_their_own(env, monkeypatch):
    env.frappe.db.table("Mpesa Settings")["OFF"] = {"name": "OFF", "enable_hourly_pull_transactions": 0}
    pulls = hourly_windows(env, monkeypatch, datetime.datetime(2026, 10, 18, 12, 0, 0))
    assert pulls == [
        # the window this job always read: normally one page, whatever OffSetValue means
        {"mpesa_settings": SETTINGS, "start_date": "2026-10-18 10:00:00", "end_date": "2026-10-18 12:00:00",
         "offset": 0},
        # the outage catch-up, inside Daraja's 48 hours
        {"mpesa_settings": SETTINGS, "start_date": "2026-10-16 13:00:00", "end_date": "2026-10-18 12:00:00",
         "offset": 0},
    ]


def test_first_run_after_an_outage_recovers_receipts_from_its_start(env, monkeypatch):
    # site down 10:00-13:30: confirmations failed and Daraja does not resend them. Frappe runs
    # the missed hourly job once on resume, so that one run has to reach back past 10:00.
    resumed = datetime.datetime(2026, 10, 18, 13, 30, 0)
    pulls = hourly_windows(env, monkeypatch, resumed)
    assert all(parse(p["end_date"]) == resumed for p in pulls)
    start = min(parse(p["start_date"]) for p in pulls)
    assert start <= datetime.datetime(2026, 10, 18, 10, 0, 0)
    assert resumed - start < datetime.timedelta(hours=48)  # Daraja keeps only 48 hours


def test_one_failing_window_does_not_stop_the_other(env, monkeypatch):
    pulls = []

    def pull_transactions(**kw):
        pulls.append(kw["start_date"])
        if len(pulls) == 1:
            raise RuntimeError("queue down")

    monkeypatch.setattr(env.hourly, "now_datetime", lambda: datetime.datetime(2026, 10, 18, 12, 0, 0))
    monkeypatch.setattr(env.hourly, "pull_transactions", pull_transactions)
    env.hourly.run_hourly_pull_transactions()
    assert pulls == ["2026-10-18 10:00:00", "2026-10-16 13:00:00"]
    assert len(env.frappe.errors) == 1
