# M-Pesa Suite — changes on top of navariltd/frappe-mpsa-payments

Branch `astral-suite`, based on upstream `version-16` (newest features: STK pay page, POS quick pay,
bundled Safaricom certs, Pull Transactions fixes). Package name stays `frappe_mpsa_payments` so all
callback URLs already registered with Safaricom keep working and `bench migrate` upgrades in place.

## Phase 1 — Foundation (this change)

**Runs on Frappe/ERPNext v15 and v16**
- `pyproject.toml`: `frappe >=15,<17`, `erpnext >=15,<17`.
- `compat.py`: cache accessor, typed Single reads, `in_test()`, lazy `IntegrationTestCase`.
- CI: unit tests on Python 3.10–3.14 + bench tests on `version-15`/py3.11 and `version-16`/py3.14.
- Removed `frappe.db.commit()` from `Mpesa Settings.on_update` (not allowed inside document hooks on v16).

**One Daraja client** (`daraja/client.py`)
- All four token paths (builder connector, B2C connector, `get_token`, legacy connector) share one
  Redis-cached token with a 120 s safety margin, 10 s/30 s timeouts.
- Tokens are no longer written to `Mpesa Settings`; `Authorization` headers are redacted in
  Integration Request logs.

**Callback security** (`daraja/callbacks.py`, new **M-Pesa Suite Settings**)
- Every Safaricom-facing endpoint (STK, C2B validation/confirmation, B2C result, balance,
  transaction status, queue timeout, legacy verify, pull) goes through `@safaricom_callback`:
  optional IP allow-list, optional URL token `?t=…`, and runs as a configurable **Integration User**
  instead of switching the request to Administrator.
- Rejections are counted on the settings page (rate-limited writes).
- Every callback URL we generate now carries the token. Pull registration (permanent at
  Safaricom) is IP-checked only.

**Closed guest access**
- `get_mpesa_draft_c2b_payments`, `get_draft_pos_invoice`, `get_outstanding_invoices`: were open
  to anonymous users (customer names, phone hashes, invoices) → login + read permission.
- `Mpesa Express Request` `create_new_request` / `retry_stkpush` / `get_request_status` /
  `get_stkpush_defaults`: anonymous STK prompts to any phone, and a 30-second blocking poll →
  login + document permission. (Public checkout returns in Phase 3 via signed pay-page tokens.)

**Role gates** (`permissions.py`, new roles M-Pesa Manager / Payer / Collector / Viewer)
- Direct API calls to STK push / STK query need a collect role (incl. Sales User, Accounts User).
- Balance, transaction status, pull need a finance role; pull registration needs an admin role.
- Calls reached through a permission-checked document flow (e.g. Payment Request submit) are
  unchanged.

**B2C security credential fix** (`Mpesa Settings`)
- Upstream encrypted the STK *passkey* with OAEP for B2C and never regenerated. Now:
  RSA-PKCS1v15 of the *initiator password*, regenerated when the password is retyped, the
  environment flips, or on the new **Regenerate Security Credential** button.

**Accounting mapping on Mpesa Settings** (ERPNext custom fields): Bank Account, Cost Center,
M-Pesa Charges Account — replaces hard-coded `Mpesa-208911 - AIL` / `LIKE '%code%'` lookups in
later phases.

**Shared phone helper** (`utils/phone.py`): one normaliser (2547…/2541…), local form, Safaricom
SHA-256 MSISDN hash, network detection, masking.

## Rollout at Astral (staging copy first)
1. `bench get-app` this branch, `bench --site <site> migrate`. Existing records keep their data
   (fields upstream removed from the JSON are recreated as custom fields with the same names).
2. Open **M-Pesa Suite Settings**: set Integration User (System User with Accounts User + Sales User
   + M-Pesa Manager), keep enforcement off.
3. Fill **Accounting Mapping** on each Mpesa Settings (208911, 4134019, 4139683).
4. On each B2C settings record press **Regenerate Security Credential**, then run a balance query.
5. Turn on **Only Accept Callbacks From Allowed IPs**, watch the rejection counter for a day.
6. Turn on **Require Token on STK / B2C / …** (new requests carry the token immediately).
7. Re-register C2B URLs (Mpesa C2B Payment Register URL → save), then turn on the C2B token.
8. Astral's Server Scripts keep running; they are retired one by one in Phases 2–4.
