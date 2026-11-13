"""Round 4.5: nobody pays themselves (money_out.self_pay), on the in-memory site of b2b_fixture.

Covers: a user's own phones (the User's Mobile No and Phone, their Employee's Cell Number, the phones
of that Employee's M-Pesa Accounts, a draft account too, never a cancelled one, every Employee whose
User ID they are), all as 2547...; their names (the User's full name and the Employee's name); the
check of a payout's rows by phone and by name (the typed name and the name M-Pesa paid the phone
under before; two words at least, one first name matching no one); what the refusal says (the row,
the phone as staff see it, the action) and how many rows it lists.
"""

import importlib

import b2b_fixture
import pytest

USER = "payer@example.com"
EMA = "Employee M-Pesa Account"


@pytest.fixture
def env(monkeypatch):
    with b2b_fixture.site(monkeypatch) as env:
        db = env.frappe.db
        db.table("User")[USER] = {"name": USER, "full_name": "Peter Otieno Kamau", "mobile_no": "0722 000 111",
                                  "phone": "+254 733 000 222"}
        db.table("User")["other@example.com"] = {"name": "other@example.com", "full_name": "Ann Njeri",
                                                 "mobile_no": "0711000999", "phone": None}
        db.table("Employee").update({
            "EMP-7": {"name": "EMP-7", "employee_name": "Peter Kamau", "user_id": USER, "cell_number": "0744000333"},
            "EMP-8": {"name": "EMP-8", "employee_name": "Jane Wanjiku", "user_id": None, "cell_number": "0755000444"},
        })
        db.table(EMA).update({
            "MEA-7": {"name": "MEA-7", "employee": "EMP-7", "docstatus": 1, "phone": "254766000555"},
            "MEA-8": {"name": "MEA-8", "employee": "EMP-7", "docstatus": 0, "phone": "0777000666"},  # a draft
            "MEA-9": {"name": "MEA-9", "employee": "EMP-7", "docstatus": 2, "phone": "0788000777"},  # cancelled
            "MEA-X": {"name": "MEA-X", "employee": "EMP-8", "docstatus": 1, "phone": "0799000888"},
        })
        env.sp = importlib.import_module("mpsa_suite.money_out.self_pay")
        yield env


def row(phone=None, name=None, mpesa_name=None, **kw):
    return {"phone": phone, "payee_name": name, "mpesa_name": mpesa_name, **kw}


# ---------------------------------------------------------------- the user's own phones and names
def test_a_users_phones_are_their_users_their_employees_and_its_m_pesa_accounts(env):
    assert env.sp.own_phones(USER) == {"254722000111", "254733000222", "254744000333", "254766000555",
                                       "254777000666"}  # never the cancelled account's 0788..., nor EMP-8's
    assert env.sp.own_phones("other@example.com") == {"254711000999"}  # a user with no Employee
    assert env.sp.own_phones("Guest") == set()
    env.frappe.session.user = USER
    assert "254744000333" in env.sp.own_phones()  # the session user by default


def test_every_employee_of_the_user_counts(env):
    env.frappe.db.table("Employee")["EMP-9"] = {"name": "EMP-9", "employee_name": "Peter Kamau", "user_id": USER,
                                                "cell_number": "0710000123"}  # employed again
    assert "254710000123" in env.sp.own_phones(USER)


def test_a_user_without_the_phone_fields_or_the_user_id_field_still_has_the_rest(env):
    env.meta_fields["User"] = {"full_name"}  # a site whose User has no phone fields
    env.meta_fields["Employee"] = {"employee_name", "cell_number"}  # no User ID: no employee is theirs
    assert env.sp.own_phones(USER) == set()


def test_a_users_names_are_their_full_name_and_their_employees(env):
    assert env.sp.own_names(USER) == ["Peter Otieno Kamau", "Peter Kamau"]
    assert env.sp.own_names("Guest") == []


# ---------------------------------------------------------------- the check of a payout's rows
@pytest.mark.parametrize("paid, why", [
    (row("0722000111", "Somebody Else"), "your phone 254722000111"),  # the User's Mobile No
    (row("254733000222"), "your phone 254733000222"),  # the User's Phone
    (row("0744 000 333", "Casual Worker"), "your phone 254744000333"),  # the Employee's Cell Number
    (row("+254 766 000 555"), "your phone 254766000555"),  # an approved Employee M-Pesa Account
    (row("0777000666"), "your phone 254777000666"),  # a draft one too
    (row("0700000001", "PETER KAMAU"), "'PETER KAMAU', your name"),  # the Employee's name, as typed
    (row("0700000001", "Kamau Peter Otieno"), "'Kamau Peter Otieno', your name"),  # the User's, in any order
    (row("0700000001", "Jane W", "254700****001 - PETER OTIENO KAMAU"),
     "'254700****001 - PETER OTIENO KAMAU', your name"),  # the name M-Pesa paid the phone under before
])
def test_a_row_paying_the_users_phone_or_name_is_refused(env, paid, why):
    problem = env.sp.self_pay_problem(USER, [row("0711000333", "Jane Wanjiku"), paid], "send")
    assert problem == f"You may not send a payment to yourself: Row #2 pays {why}. Another person must send it."


@pytest.mark.parametrize("paid", [
    row("0788000777"),  # the phone of a cancelled account
    row("0799000888", "Jane Wanjiku"),  # another employee's phone and name
    row("0700000001", "Peter"),  # one first name matches no one
    row("0700000001", "Peter Mwangi"),  # another Peter
    row(None, None),
])
def test_other_peoples_rows_pass(env, paid):
    assert env.sp.self_pay_problem(USER, [paid], "approve") is None


def test_the_refusal_names_each_row_by_its_label_and_counts_the_rest(env):
    rows = [row("0722000111", label=f"Sheet row #{i}") for i in range(1, 8)]
    problem = env.sp.self_pay_problem(USER, rows, "approve")
    assert problem.startswith("You may not approve a payment to yourself: Sheet row #1 pays your phone 254722000111; ")
    assert "Sheet row #5 pays" in problem and "Sheet row #6" not in problem
    assert problem.endswith("; and 2 more. Another person must approve it.")
    assert env.sp.self_pay_problem(USER, [row("0722000111", idx=12)], "prepare").startswith(
        "You may not prepare a payment to yourself: Row #12 pays")


def test_nobody_and_nothing_is_never_refused(env):
    assert env.sp.self_pay_problem(None, [row("0722000111")]) is None
    assert env.sp.self_pay_problem("Guest", [row("0722000111")]) is None
    assert env.sp.self_pay_problem(USER, []) is None
    assert env.sp.matches("nobody@example.com", [row("0722000111", "Peter Otieno Kamau")]) == []


def test_a_masked_site_shows_the_phone_masked(env, masked_numbers):
    problem = env.sp.self_pay_problem(USER, [row("0722000111")])
    assert "Row #1 pays your phone 2547****0111" in problem and "254722000111" not in problem
