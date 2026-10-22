# Till 835813: from manual entry to live M-Pesa integration

Draft, 2026-10-22. For Astral's finance lead, IT lead and the till cashiers.

## Today

- Till 835813 is a Lipa na M-Pesa **Buy Goods** till. It has **no** Daraja integration: Navari never had an Mpesa Settings record for it (live copy, `astral.local`).
- Cashiers record till payments by hand with the Modes of Payment "Mpesa Till No. 835813" and "Lipa na Mpesa - 835813". Both post to `1.2.01.04.02 - Current Account LIPA-NA-MPESA No. 835813 - AIL` (`../astral_export/astral_config_snapshot.md:23`).
- POS profile "test pos" uses "Mpesa Till No. 835813". "Nairobi Cash Sale" uses "Mpesa-Mpesa Till Number SKT", a different Mode of Payment whose till number is **unknown** (copy). Both till Modes of Payment were disabled on the copy; their live state is **unknown**.
- The suite upgrade does not touch the till (GO_LIVE_PLAN.md, short answers). This plan is a separate change that comes **after** Astral's go-live is stable, and after the app rename (RENAME_PLAN.md), so the URLs registered with Safaricom are the final ones.

## The goal

1. Every payment to 835813 reaches the ERP within seconds as an M-Pesa receipt: a C2B confirmation creates an Mpesa C2B Payment Register.
2. At the POS, the cashier picks the customer's receipt from a list instead of typing it. POS quick-pay puts it on the invoice and books it with the M-Pesa receipt as the reference, once only.
3. For a sale on credit or a delivery, staff can send an STK prompt to the customer's phone for the till, and the paid prompt is booked against the invoice automatically.
4. Each receipt gets a Bank Transaction on the till's M-Pesa bank account, reconciled automatically, like the paybills.

## What Safaricom needs (finance lead)

| # | Step | Done when |
|---|---|---|
| S1 | Get the till's **Head Office (HO) / store number**. For a till, Daraja uses the HO/store number as the BusinessShortCode for STK and for C2B URL registration, and the till number only as PartyB (`../docs/daraja_api_reference.md:48,54`; `../docs/DARAJA_VERIFIED_2026-10-19.md` Q11). | HO/store number noted. |
| S2 | Daraja **Go-Live** for that HO/store number with **C2B (Register URL)** and **M-Pesa Express (Lipa na M-Pesa Online)**. Note the production **passkey** Safaricom emails, and the Go-Live app's Consumer Key/Secret. | Go-Live approved; passkey and keys received. |
| S3 | On the M-PESA org portal, create an API operator (initiator) on the HO with the **Transaction Status query ORG API** role. The suite then verifies each till confirmation with Safaricom before booking it. Without one, confirmations are registered unverified, as Astral's paybill 208911 is today (SUITE_CHANGES.md "Upgrade safety"). | Operator created; its password known to the IT lead only. |
| S4 | Optional: Pull registration for the HO, so missed confirmations can be recovered (Safaricom keeps 48 hours). | Registered, or decided not needed. |
| S5 | Confirm what "Mpesa Till Number SKT" is: another till that should follow this plan, or obsolete. | Answer noted. |

A till cannot pay out by B2C or B2B unless Safaricom converts it into a Business One Account (`../docs/DARAJA_VERIFIED_2026-10-19.md` Q25). This plan covers collecting only.

## ERP set-up (IT lead, in the desk)

| # | Step | Done when |
|---|---|---|
| E1 | New **Mpesa Settings** record, e.g. "Till 835813": Paybill Type = **Buy Goods**; Business Shortcode = the HO/store number (S1); Till Number = **835813**; Sandbox off; APIs = **Receive C2B** and **Send STK Prompts** only; Company = Astral Industries Limited; Consumer Key/Secret and Online Passkey from S2; Initiator Name and Initiator Password from S3 (typing the password generates the Security Credential). | Record saved, no warnings. |
| E2 | **Accounting Mapping** on that record: a company Bank Account whose GL is `1.2.01.04.02 - Current Account LIPA-NA-MPESA No. 835813 - AIL`, plus the cost centre. | Bank Account mapped. |
| E3 | **Mpesa C2B Payment Register URL** for the till: Mpesa Settings = E1, Till Number 835813, Business Shortcode = HO/store number, Mode of Payment = the cashier-facing **"Mpesa Till No. 835813"**, so POS quick-pay finds the till's receipts under the Mode of Payment cashiers already use. Save once: production registration is **one-time** (`../docs/daraja_api_reference.md` C2B section), and the app refuses URLs Safaricom would reject before sending them. | Register Status = Success. |
| E4 | Enable "Mpesa Till No. 835813" on the POS profile the till actually uses (find out which: "test pos" is not a shop profile) and make sure cashiers hold **Sales User** (most POS cashiers already do) or **M-Pesa Collector**, the roles POS quick-pay accepts (`permissions.py:11-19`). | Cashier sees "M-Pesa payments" at the POS. |
| E5 | Leave "Lipa na Mpesa - 835813" for manual corrections, or disable it, so the same money is not entered twice. The suite's duplicate guard refuses a second Payment Entry with the same M-Pesa receipt. | Finance decision noted. |

## Live tests (real money, sent by staff)

| # | Test | Who | Expected |
|---|---|---|---|
| L1 | A customer pays KES 10 to till 835813. | Cashier + staff phone | Within seconds an Mpesa C2B Payment Register appears for the receipt (verified with Safaricom if S3 is done), and its Bank Transaction is created on the till's account. |
| L2 | At the POS, ring up a KES 10 sale and pick L1's receipt under "M-Pesa payments". | Cashier | The invoice is paid with the receipt as the reference. The register links the invoice, and the receipt cannot be picked again. |
| L3 | From a Sales Invoice, send a KES 10 STK prompt for the till (Buy Goods). | Staff + customer phone | The prompt names the till. After the PIN, the prompt is Completed and exactly one Payment Entry is booked against the invoice. |
| L4 | End of day: compare the till statement from the org portal with the registers. | Finance | Every statement line has a register; no extras. |

## Order and timing

1. Astral go-live (GO_LIVE_PLAN.md) and its switch-on phases are stable, including bank lines (M5).
2. The app rename is live (RENAME_PLAN.md). Register the till's URLs only after it, so Safaricom has the final paths.
3. S1–S5 with Safaricom. Go-Live approval timing is Safaricom's.
4. E1–E5 and L1–L4 in one quiet afternoon.

## Risks

- Production C2B registration cannot be redone without Safaricom deleting the URLs. Check the domain and paths before E3 and press it once.
- Till money lands in the **Merchant** account, not Utility (`../docs/daraja_api_reference.md` account table). Balances and statements for the till are read from that account.
- If cashiers keep typing receipts by hand as well, the duplicate guard stops a second booking of the same receipt, but staff should stop manual entry for till sales once L2 passes.
- Which number Safaricom puts in a till confirmation's BusinessShortCode, the HO/store number or 835813, is **unknown**. The suite matches either one to the record (`money_in/common.py:59-80`), so both work.
