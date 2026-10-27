"""The salary Journal Entry of an M-Pesa salary payout (HRMS), for both routes.

A Payroll Entry's accrual Journal Entry credits each salary's net pay to its payroll payable
account. When M-Pesa confirms a salary paid by B2B (to the employee's bank paybill) or by B2C
(to the employee's phone, from a payment schedule), this entry clears it:

- Bank Entry, the receipt as Cheque No, posted and dated on the M-Pesa completion date (R6);
- Dr the Payroll Entry's payroll payable account, reference the Payroll Entry, for what the
  salary still had on the payable (its net pay less what was paid before); party Employee only
  when Payroll Settings books payroll per employee (as the accrual does);
- M-Pesa pays whole shillings, so the difference (under KES 1; at most 0.50 with rounded totals)
  goes to the company's Round Off account and cost center;
- Cr the payout's own M-Pesa GL (Working for B2B, Utility for B2C), with its Bank Account.
Both the payable and the M-Pesa rows take one cost center (handover F5, as every other payout
entry and HRMS's own bank entry): the Payroll Entry's, else the payout shortcode's (Mpesa
Settings), else the company's default (common.payout_cost_center).

The payable row is tagged with the payout (Journal Entry Account mpesa_b2b_payment_request or
b2c_payment_disbursement), so salary_slip_paid, which counts the Paid payout itself, does not
count the entry as a bank payment too. A part payment debits the payable with what was paid.
One entry per receipt: the B2B booking looks for the receipt under its lock (b2b_entries), and
the B2C booking by its receipt and tag (b2c_postprocess_service.booked_docstatus).
"""

import frappe
from frappe.utils import cint, cstr, flt

from . import owed

JE = "Journal Entry"
PAYROLL_ENTRY = "Payroll Entry"
MIN = owed.MIN


def employee_wise() -> bool:
    """Payroll Settings: the accrual books the payroll payable per employee (party Employee)."""
    try:
        return bool(cint(frappe.db.get_single_value("Payroll Settings",
                                                    "process_payroll_accounting_entry_based_on_employee")))
    except Exception:
        return False


def paid_before(slip, exclude_b2b=None, exclude_b2c=None) -> float:
    """What was paid against the slip before this payout (its own Paid request left out)."""
    from ..mpsa_suite.doctype.b2c_payment_disbursement.b2c_payment_disbursement import salary_slip_paid

    return flt(salary_slip_paid(slip.name, slip.get("employee"), slip.get("payroll_entry"),
                                exclude_b2b=exclude_b2b, exclude_b2c=exclude_b2c), 2)


def split(net_pay, before, amount) -> tuple:
    """(payable, round_off, problem): how a payout of ``amount`` clears a salary of ``net_pay`` of
    which ``before`` was paid already. Paying what is left (to the shilling) clears it, the
    difference to round off; paying less debits the payable with what was paid."""
    amount = flt(amount, 2)
    rest = flt(flt(net_pay, 2) - flt(before, 2), 2)
    if rest <= MIN:
        return amount, 0.0, (f"nothing was left of its net pay of {owed.money(net_pay)} on the payroll payable "
                             f"(KES {owed.money(before)} was paid before)")
    diff = flt(amount - rest, 2)
    if abs(diff) < 1:
        return rest, diff, None
    if diff < 0:
        return amount, 0.0, None
    return amount, 0.0, f"KES {owed.money(amount)} is more than the KES {owed.money(rest)} it had left"


def build(slip_name, amount, receipt, date, gl, bank_account, remark, before, tag_field, tag_value,
          settings=None) -> tuple:
    """(Journal Entry, problem) clearing the salary of ``slip_name`` paid by ``amount`` from ``gl``.
    ``problem``: the slip had less left than was paid, a possible double payment. ``settings``: the
    payout's Mpesa Settings (its cost center when the Payroll Entry has none)."""
    slip = owed.read_slip(slip_name)
    if not slip:
        frappe.throw(f"Salary Slip {slip_name} not found")
    if not slip.get("payroll_entry"):
        frappe.throw(f"{slip_name} is not part of a Payroll Entry, so it has no payroll payable to clear.")
    payroll = frappe.db.get_value(PAYROLL_ENTRY, slip.payroll_entry,
                                  ["name", "company", "payroll_payable_account", "cost_center"], as_dict=True)
    if not payroll or not payroll.payroll_payable_account:
        frappe.throw(f"Payroll Entry {slip.payroll_entry} has no Payroll Payable Account.")
    if not gl:
        frappe.throw("The M-Pesa account this salary left has no Bank Account mapped (Mpesa Settings, Accounting Mapping).")
    company = payroll.company or slip.company
    payable, round_off, problem = split(slip.get("net_pay"), before, amount)
    if problem:
        problem = f"{slip_name}: {problem}. Check that {slip.employee} was not paid twice."
    from .common import payout_cost_center

    cost_center = payroll.cost_center or payout_cost_center(settings, company)
    je = frappe.new_doc(JE)
    je.voucher_type = "Bank Entry"
    je.company = company
    je.posting_date = je.cheque_date = date
    je.cheque_no = receipt
    je.user_remark = remark
    if je.meta.has_field("custom_remark"):  # v16 hides user_remark and builds remark itself unless told
        je.custom_remark = 1
        je.remark = remark
    per_employee = employee_wise()
    je.party_not_required = not per_employee  # as HRMS's own payroll entries
    row = {"account": payroll.payroll_payable_account, "debit_in_account_currency": payable,
           "reference_type": PAYROLL_ENTRY, "reference_name": payroll.name, "cost_center": cost_center,
           "user_remark": remark}
    if per_employee:
        row.update({"party_type": "Employee", "party": slip.employee})
    if tag_field and frappe.get_meta("Journal Entry Account").has_field(tag_field):
        row[tag_field] = tag_value
    je.append("accounts", row)
    if abs(round_off) > MIN:
        account, center = frappe.db.get_value("Company", company, ["round_off_account", "round_off_cost_center"])
        if not account:
            frappe.throw(f"Set the Round Off Account on Company {company}: M-Pesa pays whole shillings, so "
                         f"KES {owed.money(abs(round_off))} of {slip_name} goes to round off.")
        je.append("accounts", {"account": account, "cost_center": center or cost_center, "user_remark": remark,
                               ("debit_in_account_currency" if round_off > 0 else "credit_in_account_currency"):
                                   abs(round_off)})
    je.append("accounts", {"account": gl, "credit_in_account_currency": flt(amount, 2), "bank_account": bank_account,
                           "cost_center": cost_center, "user_remark": remark})
    return je, problem


def remark_text(kind, payout, receipt, slip, employee) -> str:
    return f"M-Pesa {kind} salary to {employee} for {slip} ({payout}), receipt {receipt}"


def for_b2b(doc, bank_account, gl, date, slip=None) -> tuple:
    """(Journal Entry, problem) of a Paid B2B salary request (b2b_entries books it). ``slip``: the
    Salary Slip Make Entry chose, else the request's."""
    name = slip or doc.salary_slip
    values = owed.read_slip(name)
    if not values:
        frappe.throw(f"Salary Slip {name} not found")
    receipt = cstr(doc.transaction_id).strip()
    text = remark_text("B2B", doc.name, receipt, name, values.get("employee_name") or doc.employee)
    return build(name, flt(doc.amount), receipt, date, gl, bank_account, text,
                 paid_before(values, exclude_b2b=doc.name), "mpesa_b2b_payment_request", doc.name,
                 settings=doc.get("mpesa_settings"))


def for_b2c(disbursement, ref, submit: bool = False) -> str:
    """Book the salary Journal Entry of a Paid B2C row made by a payment schedule (the B2C
    booking's _book calls it instead of journal_service). Returns the entry."""
    from . import statement

    request = ref.b2c_disbursement_request
    p = statement.load(statement.B2C, request)
    if not p:
        frappe.throw(f"B2C Disbursement Request {request} not found")
    bank_account = statement.payout_bank_account(p)
    gl = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
    slip = owed.read_slip(ref.reference_name)
    if not slip:
        frappe.throw(f"Salary Slip {ref.reference_name} not found")
    receipt = p.receipt or cstr(ref.reference_no).strip()
    text = remark_text("B2C", request, receipt, ref.reference_name, slip.get("employee_name") or ref.party)
    je, problem = build(ref.reference_name, flt(p.amount) or flt(ref.allocated_amount), receipt, p.date, gl,
                        bank_account, text, paid_before(slip, exclude_b2c=request), "b2c_payment_disbursement",
                        disbursement.name, settings=p.get("mpesa_settings"))
    je.flags.ignore_permissions = True
    je.insert(ignore_permissions=True)
    if submit:
        je.submit()
    if problem:
        frappe.log_error(title="M-Pesa salary may be paid twice", message=f"{request}: {problem}",
                         reference_doctype=statement.B2C, reference_name=request)
        from .party_accounts import alert

        alert(f"{request}: {problem}", frappe._dict(doctype=statement.B2C, name=request))
    return je.name
