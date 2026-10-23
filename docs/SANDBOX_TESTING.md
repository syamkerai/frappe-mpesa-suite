# M-Pesa Sandbox Test Lab

Runs every M-Pesa flow against Safaricom's Daraja **sandbox** and checks what the app did:
receipts, verification, booking, duplicates, forgeries, payouts and reversals. Use it on a **test
site** before going live. It refuses to run on live keys.

## 1. Test site (once)

1. Create a separate Frappe Cloud site (a copy of the live site, or a fresh ERPNext with a test
   company). Tests post real Payment Entries and GL entries on it.
2. Install `mpsa_suite` (branch `mpsa-suite`) and run `migrate`.
3. Switch the lab on for that site only:
   `bench --site <test-site> set-config mpesa_sandbox_lab 1`
   (Frappe Cloud: Site → Site Config → add `mpesa_sandbox_lab` = `1`).
4. Make sure `host_name` is the site's public `https://` address. Safaricom posts results to it.

## 2. Daraja sandbox app (once)

On [developer.safaricom.co.ke](https://developer.safaricom.co.ke):

1. Go to **My Apps → Add a new app** and tick every product: M-Pesa Express, C2B, B2C, B2B,
   Transaction Status, Account Balance and Reversal. Safaricom's pages disagree on whether one
   app can hold both C2B and B2C. If the portal refuses, make a second app for B2C and give it its
   own Mpesa Settings record (same shortcode, that app's key and secret), then point the test
   run's B2C field at it.
2. Copy the app's **Consumer Key** and **Consumer Secret**.
3. Open the **Test Credentials** page (you must be logged in) and note:
   - Initiator Name and Initiator Password
   - Shortcode 1 (Party A, 600xxx), which works for every API except B2B, and Shortcode 2,
     which is B2B's Party B
   - Test MSISDN (Daraja's docs use **254708374149**)
   - the M-Pesa Express shortcode **174379** (for M-Pesa Express only) and its **Passkey**

Type secrets into the ERPNext forms directly. Never paste them into chats or tickets.

## 3. ERPNext setup on the test site

| Record | Settings |
|---|---|
| Mpesa Settings "Sandbox C2B" | **Sandbox** ticked; the key and secret; **Business Shortcode** and **Till Number** both = Shortcode 1 (600xxx). Under **APIs this record uses** tick Receive C2B, B2C Payouts, B2B Supplier Payments and Reversals. In sandbox, B2C, B2B, balance, status and reversal send the **Till Number** as the paying party; Initiator Name and Password from Test Credentials; Company and Accounting Mapping. |
| Mpesa C2B Payment Register URL | For "Sandbox C2B". Register Status must be **Success**. |
| Mpesa Settings "Sandbox STK" | **Sandbox** ticked; the key and secret; **Business Shortcode 174379**; the sandbox **Passkey**; **Send STK Prompts** ticked; Company. |

One record can also do everything, like a live Business One Account: Business Shortcode **174379**
(STK), Till Number **600xxx** (C2B and every money-out API), all switches ticked, and the same record
in every field of the test run. **Health Check** lists each record's switches and names any the run
needs but the record lacks.
| M-Pesa Suite Settings | Integration User. Keep both **Verify** switches on. Leave the IP allow-list **off** while testing, because the lab's replay and forgery posts come from the server itself. |
| Test Sales Invoice | Submitted, with at least 5 × the test amount outstanding. |
| Test Supplier | One **approved Supplier M-Pesa Account** to Party B (600000). For the lab, set **Hold New Supplier M-Pesa Accounts** to 0. |
| Test B2C Payment Disbursement | One row paying the Test MSISDN. Leave it as a draft; the lab submits it. |

The B2C and B2B fields of the test run can point to the C2B record, because Shortcode 1 has the
initiator in sandbox.

## 4. Running

1. Go to **M-Pesa workspace → Sandbox Test Lab → New**. Fill in Sandbox Setup and Test Documents,
   then save. Every scenario is added as a row.
2. Click **Health Check**. Fix every red line before anything else.
3. Click **Run Ready Tests** to start the automatic tests.
4. Start **Guided** tests with the row's **Run** button, one at a time. The confirmation says what
   to do on your phone: pay, or press Cancel.
5. Tests that depend on another test (replays, status checks, reversal) become ready once it has
   passed. Click **Run Ready Tests** again.
6. Results are checked every minute and the page refreshes itself. Each row shows Passed or Failed
   with the reason and a link to the document it created.

**Tools** menu:
- **Simulate a C2B Payment** makes any sandbox paybill or till payment you like.
- **Replay a Callback** re-sends a stored Safaricom callback, with or without the token, to see
  what a re-send or a forger gets.

## 5. What is tested

| Group | Scenarios |
|---|---|
| Health | Setup and connectivity (token, initiator, public callback URL, C2B registration, go-live switches); Account Balance |
| C2B | Exact invoice reference; mistyped reference; unknown reference; duplicate confirmation; forged without token; forged receipt Safaricom never issued; wrong shortcode |
| STK | Paid; cancelled; forged "paid" callback while the prompt is open; duplicate callback; status check of a paid prompt; callback without token |
| B2C | Payout paid and booked once; duplicate result; status query refused for a settled payout |
| B2B | Typed paybill refused; payment to an approved supplier account; duplicate result |
| Reversal | Reverse a received C2B payment |

Pull Transactions cannot be tested in the sandbox (Safaricom does not offer it there), and real
customer payments cannot be either. Both get a KES 1–10 live check at go-live.

Sandbox quirks Safaricom documents: STK test money is reversed automatically at 6am the next day,
so a sandbox balance can change overnight; C2B simulation exists only in the sandbox; register the
C2B URLs again before simulating if the sandbox has forgotten them. Safaricom doesn't publish the
queue-timeout message, so the lab never relies on one.

## 6. Go-live gate

Run the whole lab twice in a row with every row **Passed**, with the go-live switches on (both
Verify switches, both Require Token switches, Match and Book Receipts). Keep the test runs: they
are the sign-off record.

## 7. Running the lab on a local bench

- Expose the site with a tunnel (ngrok's free plan gives you a fixed dev domain) and set
  `host_name` to its bare `https://` address.
- On a dev bench Frappe appends the web port to `host_name` in `get_url()`, so callback URLs
  point at a port the tunnel doesn't serve. Set `bench set-config -g -p restart_supervisor_on_update 1`
  (production benches already have it).
- Sandbox test shortcodes (600xxx) and the `testapi` initiator are shared by many developers.
  Expect "API password is locked" (8006) and C2B confirmations going to whoever registered URLs
  last. Re-register before a C2B round and retry another day if they never arrive.
- If Safaricom answers with a 403 HTML page (Incapsula), wait a minute and retry. It blocks bursts.
