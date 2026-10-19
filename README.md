## Astral M-Pesa Suite

M-Pesa for Frappe / ERPNext **v15 and v16**, built and maintained by **Astral Industries Limited**
(Nairobi, Kenya). One app for everything Astral does with M-Pesa: collecting money (C2B paybill
and till payments, STK prompts), paying people (B2C, B2B), reversals, the bank side of every
receipt, Payment Entries, reconciliation, and an external Payments API for chatbots and shops.

What changed against the upstream app, release by release, is in [SUITE_CHANGES.md](SUITE_CHANGES.md).
The external API is documented in [docs/PAYMENTS_API.md](docs/PAYMENTS_API.md).

### What it does

**Money in**
- **C2B confirmations and STK callbacks are confirmed with Safaricom before anything is booked**
  (Transaction Status / STK Query). Callbacks are also checked for IP, a URL token and the
  shortcode, and they run as a dedicated Integration User.
- One **Mpesa C2B Payment Register** per receipt, whichever way it arrives: confirmation, hourly
  Pull, or a Transaction Status lookup.
- Automatic matching of receipts to Sales Invoices, Sales Orders, Payment Requests, Quotations and
  customers (account number, name, payer phone), with an **Mpesa Match Log** for every decision.
- Payment Entries carry the M-Pesa receipt as reference. Duplicate receipts are blocked on Payment
  Entry and Bank Transaction.
- A **Bank Transaction** for every receipt on the shortcode's bank account, auto-reconciled
  against its Payment Entry.
- **STK prompts** from Payment Requests and the desk (Sales Invoice, Payment Entry), plus the
  public pay page `/pay/<token>`.

**Money out**
- **B2C payouts** to employees and suppliers from Expense Claims, Employee Advances, Salary Slips,
  Purchase Invoices and Purchase Orders. A document can't be paid twice or paid past what it owes.
  Payouts that time out are settled by a Transaction Status query, never re-sent blindly, and
  accounting entries are retried until they are booked.
- **B2B** paybill / till payments, **Reversals**, and **B2C Transaction Status** checks.

**External Payments API** (`/api/method/frappe_mpsa_payments.gateway.v1.*`)
- API clients with keys, limits and IP allow-lists. Payment intents settle from STK, C2B, Pull or a
  typed receipt, and signed webhooks report the outcome.

### Install

```bash
bench get-app https://github.com/syamkerai/frappe-mpesa-suite.git --branch astral-suite
bench --site <site> install-app frappe_mpsa_payments
bench --site <site> migrate
```

Requires ERPNext; HRMS is optional (Salary Slip, Expense Claim and Employee Advance payouts).
The Python package keeps the upstream name `frappe_mpsa_payments`, so the callback URLs already
registered with Safaricom keep working.

### Configure

1. **Mpesa Settings**, one per paybill or till: consumer key and secret, passkey, shortcode, and
   the initiator name and password (press **Regenerate Security Credential**). Then fill
   **Accounting Mapping** (bank account, cost center, charges account).
2. **M-Pesa Suite Settings**:
   - set the Integration User;
   - turn on the IP allow-list, then the callback tokens;
   - leave callback verification on;
   - turn on the Money In switches one at a time.
3. Register the C2B URLs from **Mpesa C2B Payment Register URL**.

The rollout order used at Astral is in [SUITE_CHANGES.md](SUITE_CHANGES.md#rollout-at-astral-staging-copy-first).

### Roles

M-Pesa Manager (configuration, releases, reversals), M-Pesa Payer (payouts), M-Pesa Collector
(STK prompts), M-Pesa Viewer (read-only). Standard ERPNext Accounts and Sales roles keep the
access they need.

### Licence

Astral M-Pesa Suite is a modified version of
[navariltd/frappe-mpsa-payments](https://github.com/navariltd/frappe-mpsa-payments)
(© Navari Limited and contributors), which is licensed under the **GNU Affero General Public
License v3.0**. This modified version is distributed under the same licence; see
[license.txt](license.txt) and [NOTICE](NOTICE). Astral's own additions are © Astral Industries
Limited. If you let users interact with a modified version over a network, the AGPL requires you
to offer them its source code.
