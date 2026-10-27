"""M-Pesa Bank Paybills (money_out.bank_paybills): the shipped list of bank paybills and its sync.

Covers: the shipped file (no more than the top 50 institutions, unique codes and paybills, only
fields the DocType has, KCB with Safaricom's own name); the sync plan (insert, update a field the
admin left alone, keep an admin's edit and say so, adopt a row an admin made, retire an untouched
row, never bring back what is not shipped, nothing to do the second time); the sync itself against
the in-memory site; who may change a row and what it may hold; a shipped row never deleted; a
paybill change blocking the approved accounts that copied the old number, and unblocking them
when it changes back.
"""

import importlib
import json
from pathlib import Path

import pytest

import fake_frappe

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "mpsa_suite" / "money_out" / "data" / "bank_paybills.json"
DOCTYPE_JSON = ROOT / "mpsa_suite" / "mpsa_suite" / "doctype" / "m_pesa_bank_paybill" / "m_pesa_bank_paybill.json"
BANKS = "M-Pesa Bank Paybill"
SUP, EMP = "Supplier M-Pesa Account", "Employee M-Pesa Account"


@pytest.fixture
def env():
    frappe, saved = fake_frappe.install()
    try:
        frappe.db.table("Mpesa Settings")["Astral 600999"] = {
            "name": "Astral 600999", "business_shortcode": "600999", "till_number": "5123456",
        }
        bp = importlib.import_module("mpsa_suite.money_out.bank_paybills")
        pa = importlib.import_module("mpsa_suite.money_out.party_accounts")
        alerts = []
        pa_alert = pa.alert
        pa.alert = lambda message, doc, roles=None: alerts.append((doc.name, message))
        yield fake_frappe._dict(frappe=frappe, bp=bp, pa=pa, alerts=alerts)
        pa.alert = pa_alert
    finally:
        fake_frappe.uninstall(saved)


def shipped(**kw):
    row = {"code": "KCB", "institution_name": "KCB Bank Kenya Limited", "institution_type": "Bank",
           "paybill": "522522", "account_number_label": "Account Number at KCB",
           "account_number_rule": "The KCB account number, digits only.", "account_number_pattern": "",
           "receiver_name": "KCB Paybill AC", "enabled": 1, "source": "Researched", "verified_on": "2026-10-27"}
    row.update(kw)
    return row


def site_row(env, item, **kw):
    """A row as a sync left it: its values, and the shipment they came from."""
    new = env.bp._shipment(item)
    row = {"name": new["code"], **new, "is_shipped": 1, "shipped_values": json.dumps(new)}
    row.update(kw)
    return row


# ---------------------------------------------------------------- the shipped file
def test_the_shipped_file_is_the_top_institutions_only():
    rows = json.loads(SHIPPED.read_text(encoding="utf-8"))
    assert 0 < len(rows) <= 50
    fields = {f["fieldname"] for f in json.loads(DOCTYPE_JSON.read_text(encoding="utf-8"))["fields"]}
    types = next(f for f in json.loads(DOCTYPE_JSON.read_text(encoding="utf-8"))["fields"]
                 if f["fieldname"] == "institution_type")["options"].split("\n")
    assert len({r["code"] for r in rows}) == len({r["paybill"] for r in rows}) == len(rows)
    for r in rows:
        assert set(r) - {"retired"} <= fields, r["code"]
        assert r["code"] == r["code"].upper() and r["code"].replace("-", "").isalnum(), r["code"]
        assert r["paybill"].isdigit() and 5 <= len(r["paybill"]) <= 7, r["code"]
        assert r["institution_type"] in types and r["enabled"] in (0, 1), r["code"]
        if r["account_number_pattern"]:
            import re

            re.compile(r["account_number_pattern"])
        if not r["enabled"]:
            assert "Unverified" in r["source"], r["code"]  # a medium-confidence row ships switched off
    kcb = next(r for r in rows if r["paybill"] == "522522")
    assert (kcb["code"], kcb["receiver_name"], kcb["enabled"]) == ("KCB", "KCB Paybill AC", 1)


def test_load_shipped_reads_the_file_and_a_missing_file_is_nothing(env, tmp_path):
    assert len(env.bp.load_shipped()) == len(json.loads(SHIPPED.read_text(encoding="utf-8")))
    assert env.bp.load_shipped(tmp_path / "none.json") == []


# ---------------------------------------------------------------- the plan
def test_a_missing_row_is_inserted_as_shipped(env):
    (action,) = env.bp.plan_seed([], [shipped()])
    assert (action["action"], action["code"]) == ("insert", "KCB")
    assert action["values"]["is_shipped"] == 1 and action["values"]["shipped_values"]["paybill"] == "522522"


def test_a_field_the_admin_left_alone_follows_the_release_and_an_edit_is_kept(env):
    existing = [site_row(env, shipped(), source="Checked with KCB")]  # the admin changed the source
    release = shipped(source="Researched again", account_number_rule="A new rule", receiver_name="KCB Paybill AC")
    (action,) = env.bp.plan_seed(existing, [release])
    assert action["action"] == "update"
    assert action["values"]["account_number_rule"] == "A new rule"
    assert "source" not in action["values"]
    assert action["kept"] == {"source": ("Researched again", "Checked with KCB")}
    assert action["values"]["shipped_values"]["source"] == "Researched again"


def test_nothing_to_do_when_the_release_ships_the_same(env):
    assert env.bp.plan_seed([site_row(env, shipped(), enabled=0)], [shipped()]) == []


@pytest.mark.parametrize("match", [{"name": "KCB-OWN", "code": "KCB-OWN"}, {"name": "KCB", "paybill": "522599"}])
def test_a_row_an_admin_made_is_adopted_as_it_is(env, match):
    own = {"name": "X", "code": "X", "institution_name": "Kenya Commercial", "paybill": "522522", "enabled": 1,
           "is_shipped": 0, "shipped_values": None,
           "account_number_rule": "The KCB account number, digits only.", **match}
    (action,) = env.bp.plan_seed([own], [shipped()])
    assert (action["action"], action["name"]) == ("adopt", own["name"])
    assert set(action["values"]) == {"is_shipped", "shipped_values"}  # the admin's values stay
    # adopted, it is the shipped KCB from then on: a release changes only what the admin did not
    row = dict(own, is_shipped=1, shipped_values=json.dumps(action["values"]["shipped_values"]))
    (again,) = env.bp.plan_seed([row], [shipped(account_number_rule="A new rule", institution_name="KCB Bank PLC")])
    assert again["action"] == "update" and again["values"]["account_number_rule"] == "A new rule"
    assert again["kept"]["institution_name"] == ("KCB Bank PLC", "Kenya Commercial")


def test_a_retired_row_is_disabled_only_while_untouched(env):
    (action,) = env.bp.plan_seed([site_row(env, shipped())], [shipped(retired=True)])
    assert (action["action"], action["values"]["enabled"]) == ("retire", 0)
    assert env.bp.plan_seed([site_row(env, shipped(), receiver_name="KCB")], [shipped(retired=True)]) == []
    assert env.bp.plan_seed([], [shipped(retired=True)]) == []  # never inserted


def test_a_paybill_another_shipped_row_holds_is_not_taken(env):
    other = site_row(env, shipped(code="KCB-OLD"))
    assert env.bp.plan_seed([other], [shipped(code="KCB")]) == []


# ---------------------------------------------------------------- the sync on a site
class Row(fake_frappe._dict):
    """A bank paybill row with its controller's save path (validate, on_update)."""

    def __init__(self, env, values, before=None):
        super().__init__(values)
        self["doctype"] = BANKS
        self["flags"] = fake_frappe._dict()
        self["_env"] = env
        self["_before"] = before

    def get_doc_before_save(self):
        return self["_before"]

    def add_comment(self, kind, text):
        self["_env"].frappe.comments.append((BANKS, self.name, text))

    def _store(self):
        self["_env"].frappe.db.table(BANKS)[self.name] = {k: v for k, v in self.items()
                                                         if k not in ("flags", "_env", "_before", "doctype")}

    def insert(self, ignore_permissions=False):
        self.name = self.code
        self["_env"].bp.validate_row(self)
        self._store()
        return self

    def save(self, ignore_permissions=False):
        self["_env"].bp.validate_row(self)
        self._store()
        self["_env"].bp.after_change(self)
        return self


@pytest.fixture
def site(env, monkeypatch, tmp_path):
    env.frappe.db.table("DocType")[BANKS] = {"name": BANKS}
    plain = env.frappe.get_doc

    def get_doc(doctype, name=None, **kw):
        if isinstance(doctype, dict) and doctype.get("doctype") == BANKS:
            return Row(env, {k: v for k, v in doctype.items() if k != "doctype"})
        if doctype == BANKS:
            row = env.frappe.db.table(BANKS)[name]
            return Row(env, dict(row), before=fake_frappe._dict(row))
        return plain(doctype, name, **kw)

    monkeypatch.setattr(env.frappe, "get_doc", get_doc)
    env.file = tmp_path / "bank_paybills.json"
    return env


def ship(env, *rows):
    env.file.write_text(json.dumps(list(rows)), encoding="utf-8")
    return env.bp.sync_shipped(env.file)


def test_sync_inserts_then_keeps_an_admin_edit_with_a_comment_and_is_idempotent(site):
    ship(site, shipped(), shipped(code="EQUITY-BANK", institution_name="Equity Bank Kenya Limited", paybill="247247",
                                  receiver_name=""))
    table = site.frappe.db.table(BANKS)
    assert set(table) == {"KCB", "EQUITY-BANK"} and table["KCB"]["is_shipped"] == 1
    table["KCB"]["account_number_rule"] = "Our own note"  # an admin's edit
    ship(site, shipped(account_number_rule="A new rule", source="Researched again"),
         shipped(code="EQUITY-BANK", institution_name="Equity Bank Kenya Limited", paybill="247247", receiver_name=""))
    assert (table["KCB"]["account_number_rule"], table["KCB"]["source"]) == ("Our own note", "Researched again")
    assert any("ships account_number_rule 'A new rule'; your 'Our own note' is kept" in c[2]
               for c in site.frappe.comments)
    comments = len(site.frappe.comments)
    assert ship(site, shipped(account_number_rule="A new rule", source="Researched again"),
                shipped(code="EQUITY-BANK", institution_name="Equity Bank Kenya Limited", paybill="247247",
                        receiver_name="")) == []
    assert len(site.frappe.comments) == comments


def test_a_row_that_fails_does_not_stop_the_sync(site):
    ship(site, shipped(code="OWN", paybill="600999"), shipped())  # 600999 is this company's own shortcode
    assert set(site.frappe.db.table(BANKS)) == {"KCB"}
    assert site.frappe.errors and "OWN" in site.frappe.errors[0][0][1]


def test_a_release_that_changes_an_untouched_paybill_blocks_the_accounts_on_it(site):
    ship(site, shipped())
    site.frappe.db.table(EMP)["MEA-1"] = {"name": "MEA-1", "docstatus": 1, "bank_paybill": "KCB", "shortcode": "522522",
                                          "needs_reapproval": 0}
    ship(site, shipped(paybill="522533"))
    assert site.frappe.db.table(EMP)["MEA-1"]["needs_reapproval"] == 1


def test_no_site_table_no_sync(env):
    assert env.bp.sync_shipped() == []


# ---------------------------------------------------------------- the row itself
def row(env, **kw):
    values = {"name": "KCB", "code": "kcb ", "paybill": " 522522 ", "account_number_pattern": "", "receiver_name": "",
              "is_shipped": 0}
    values.update(kw)
    doc = fake_frappe._dict(values)
    doc.flags = fake_frappe._dict()
    return doc


def test_only_config_roles_change_a_row_unless_the_release_does(env):
    env.frappe.user_roles = {"Accounts Manager"}
    with pytest.raises(env.frappe.PermissionError):
        env.bp.validate_row(row(env))
    doc = row(env)
    doc.flags.mpsa_seed = True
    env.bp.validate_row(doc)
    assert (doc.code, doc.paybill) == ("KCB", "522522")
    env.frappe.user_roles = {"M-Pesa Manager"}
    env.bp.validate_row(row(env))


@pytest.mark.parametrize("kw, msg", [
    ({"paybill": "52A522"}, "Paybill must be 5 to 7 digits"),
    ({"paybill": "600999"}, "own M-Pesa numbers"),
    ({"code": "K B"}, "Code is letters, digits and dashes only"),
    ({"account_number_pattern": "[0-9"}, "not a valid regular expression"),
    ({"receiver_name": "522523 - KCB Paybill AC"}, "starts with 522523, not this row's paybill 522522"),
])
def test_a_rows_values_are_checked(env, kw, msg):
    env.frappe.user_roles = {"M-Pesa Manager"}
    with pytest.raises(env.frappe.ValidationError, match=msg):
        env.bp.validate_row(row(env, **kw))


def test_the_number_in_front_of_a_pasted_name_is_dropped_and_a_paybill_is_on_one_row(env):
    env.frappe.user_roles = {"M-Pesa Manager"}
    doc = row(env, receiver_name="522522 - KCB Paybill AC")
    env.bp.validate_row(doc)
    assert doc.receiver_name == "KCB Paybill AC"
    env.frappe.db.table(BANKS)["KCB-2"] = {"name": "KCB-2", "paybill": "522522"}
    with pytest.raises(env.frappe.ValidationError, match="Paybill 522522 is already M-Pesa Bank Paybill KCB-2"):
        env.bp.validate_row(row(env))


def test_a_shipped_row_is_disabled_not_deleted(env):
    with pytest.raises(env.frappe.ValidationError, match="came with M-Pesa Suite: disable it instead"):
        env.bp.check_delete(row(env, is_shipped=1))
    env.bp.check_delete(row(env))


# ---------------------------------------------------------------- a paybill change
def test_a_paybill_change_blocks_approved_accounts_and_a_change_back_unblocks_them(env):
    for doctype, name in ((SUP, "MSA-1"), (EMP, "MEA-1"), (EMP, "MEA-2")):
        env.frappe.db.table(doctype)[name] = {"name": name, "docstatus": 1, "bank_paybill": "KCB", "shortcode": "522522",
                                              "needs_reapproval": 0}
    env.frappe.db.table(EMP)["MEA-3"] = {"name": "MEA-3", "docstatus": 0, "bank_paybill": "KCB", "shortcode": "522522"}
    changed = fake_frappe._dict(name="KCB", paybill="522533")
    assert env.bp.flag_accounts(changed, "522522") == {SUP: 1, EMP: 2}
    mea = env.frappe.db.table(EMP)["MEA-1"]
    assert mea["needs_reapproval"] == 1
    assert mea["reapproval_reason"].startswith("Bank paybill KCB changed from 522522 to 522533: cancel and amend")
    assert "needs_reapproval" not in env.frappe.db.table(EMP)["MEA-3"]  # a draft copies the new number on its save
    employees = next(m for n, m in env.alerts if "Employee M-Pesa Account" in m)
    assert "2 approved Employee M-Pesa Accounts are blocked" in employees and "MEA-" not in employees
    acc = env.pa.get_account(EMP, "MEA-1")
    assert "is blocked: Bank paybill KCB changed from 522522 to 522533" in env.pa.usable_problem(acc, None, EMP)
    env.bp.flag_accounts(fake_frappe._dict(name="KCB", paybill="522522"), "522533")
    assert mea["needs_reapproval"] == 0 and env.pa.usable_problem(env.pa.get_account(EMP, "MEA-1"), None, EMP) is None
    assert any("is 522522 again" in c[2] for c in env.frappe.comments)


def test_a_save_that_keeps_the_paybill_flags_nothing(env):
    env.frappe.db.table(EMP)["MEA-1"] = {"name": "MEA-1", "docstatus": 1, "bank_paybill": "KCB", "shortcode": "522522",
                                         "needs_reapproval": 0}
    doc = fake_frappe._dict(name="KCB", paybill="522522", get_doc_before_save=lambda: {"paybill": "522522"})
    env.bp.after_change(doc)
    assert env.frappe.db.table(EMP)["MEA-1"]["needs_reapproval"] == 0
