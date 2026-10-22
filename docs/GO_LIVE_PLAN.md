# Go-live plan: M-Pesa Suite on Astral's live site

Draft, 2026-10-22. For Astral's finance/IT lead and whoever runs the Frappe Cloud dashboard.

**Summary**
1. The M-Pesa app on the live Frappe Cloud site moves from Navari 15.3.1 to the Astral M-Pesa Suite, under the same app name (`frappe_mpsa_payments`), at release C (D3). Records and Safaricom's registered callback URLs stay as they are. The later rename to `mpsa_suite` is a separate change on another day. In `RP`'s terms this plan is Stage A and the rename is Stage B (RP section 6.1).
2. Astral's Server Scripts keep running on the day. They are switched off one at a time over about two weeks, each at the moment its suite switch goes on. Till 835813 is not part of Navari and does not change.
3. Downtime: migrate took 40–73 s on the local copy. The full Frappe Cloud update time is **unknown** until staging is timed. Plan a quiet window of about 5 hours on a Sunday.
4. Rollback means exporting what arrived since the backup, then restoring the on-demand backup together with Navari's code, inside the window only. After that, fix forward by switching features off.
5. Frappe Cloud facts settled on 2026-10-22: Astral's site is on a **private bench group**, there is **no SSH**, and swapping Navari's app source for the fork **in place** already worked on Trident Plumbers' site (19–20 Oct, no uninstall). Astral and Trident are on different bench groups, so nothing done to Trident reaches Astral.
6. **Trident Plumbers goes first** (section 0). Astral follows only after the Trident pilot passes.

**Sources.** `SC` = `SUITE_CHANGES.md` (repo root). Code paths are under `frappe_mpsa_payments/`; `inner/` means `frappe_mpsa_payments/frappe_mpsa_payments/`. `scripts/` = `../astral_export/server_scripts/` (live export of 2026-10-18). `DV` = `../docs/DARAJA_VERIFIED_2026-10-19.md` (workspace docs folder). "copy" = query on `astral.local`, the restored live copy on the WSL bench (data ends 2026-10-02). "FC" = the Frappe Cloud facts in docs.frappe.io/cloud/benches/custom-app. "C" = release C, commit ee5b201 on `astral-suite`, the last old-name release. `RP` = `docs/RENAME_PLAN.md` (the rename to `mpsa_suite`). `branch:` = the `mpsa-suite` branch at 27efca5 (worktree `../mpsa-suite-wt`); its `docs/RENAME_SPIKE.md` records the v15 hand-over test. `spike15:` = that test's logs, as in RP's source key: the v15 hand-over test with mpsa_suite code at 9927c16 (the same app code as 27efca5) and the old app at 02d94a0, in `C:\Users\syamk\AppData\Local\Temp\claude\C--Users-syamk-Documents-Ai-Brain-Mpesa-All-in-ONE-Frappe-App\c5101ebd-7b20-48bf-b36f-b192e3b7b4c5\scratchpad\final\v15\` (temporary scratchpad).

## Short answers to the three questions

**Can we install this on Frappe Cloud with no server or bench access?** Yes. Every step below is a dashboard or desk step; there is **no SSH** on Astral's bench group, so no step needs a terminal.
- Custom apps run only on a **private bench group** (FC). Astral's site is on one (user, 2026-10-22).
- Swapping Navari's source for the fork in place works: Trident Plumbers' Frappe Cloud site ran Navari (patches 2026-08-19), then the suite's patches on 2026-10-19 11:55 and 2026-10-20 22:06, with no uninstall (Trident Patch Log and Installed Applications, read 2026-10-22). Astral's live site is still on Navari 15.3.1 with only Navari's two patches, on Frappe 15.123.1 / ERPNext 15.123.0 (Astral Patch Log and Installed Applications, read 2026-10-22).
- How Frappe Cloud makes a staging site from a backup, and how it rolls a site back, is still **unknown**. Test both on staging, or on Trident (section 0).
- Never uninstall `frappe_mpsa_payments` (Navari's app, then the suite under the same name) or remove it from the bench group as part of this plan. Frappe's `remove_app` runs `DROP TABLE` on every DocType of the app. On the copy that is 1,584 B2C Disbursement Requests, 983 B2C Payment Disbursements, 261 C2B registers, 326 STK requests and 3 Mpesa Settings (`~/rr-bench/apps/frappe/frappe/installer.py:373,517`; `~/mpesa-rehearsal/r2/before_snapshot.txt`). Once a site runs C, the app refuses its own Uninstall (R3) unless `mpsa_suite` is installed and owns the module "Mpsa Suite" (the rename hand-over), or Site Config has `mpsa_allow_old_uninstall` = 1 or true (`uninstall_guard.py:16-32`; `hooks.py:118`; RP R3). Navari's code, live on Astral today, has no such guard. Never set that key. The rename (RP section 3.2 F) is the only planned uninstall, and the app leaves the bench group 7 or more quiet days after it (RP section 3.2 I, T18).

**Will Navari's current flows and the till keep working?**
- Navari usage on the copy: 208911 has 246 C2B receipts and ~305 STK prompts from 301 Payment Requests. 4134019 has 15 C2B and 21 STK (until July 2026). 4139683 has 1,474 Paid B2C payouts (copy).
- Rehearsal 1 on the copy (2026-10-20) replaced Navari (22c9f05) with the suite (8fb4b49) and lost no rows or columns (SC:485-489; `~/mpesa-rehearsal/navari_head.txt`; `compare.py before.json after.json`). Rehearsal 2 (2026-10-21) upgraded the suite to a later suite commit, 8fb4b49 → 75de2f1, also with no loss (SC:629-641). Only rehearsal 1 is like the live change.
- Every callback path Safaricom uses today still exists at the same address: STK `stk_push_callback`, B2C `b2c_results_callback`, C2B `confirmation`/`validation`, `balance_query_callback`. Requests sent before the switch still land, and no new Safaricom registration is needed (`inner/api/m_pesa_api.py:69,514,625,721`; `inner/api/b2c.py:21`; copy Integration Requests).
- Till 835813 is not set up in Navari: it has no Mpesa Settings, only Modes of Payment on POS profiles "test pos" and "Nairobi Cash Sale". The suite does not touch Modes of Payment it did not create. Mode of Payment rows stayed at 40 across rehearsal 2 (`~/mpesa-rehearsal/r2/compare.txt`).
- The one change for the till: the suite's duplicate guards also cover the till's bank account and GL. Astral's own guards already cover the till account (`money_in/common.py:133-168`; `scripts/AIL_Mpesa_Feed_BT_Duplicate_Guard.py:7-9`).

**Can every M-Pesa transaction become a bank line almost at once, reconciled to the right Payment Entry?**
- **Money in, yes.** With *Create Bank Transaction for Every Receipt* on, the suite makes one Bank Transaction per C2B receipt when it arrives, and per STK receipt within minutes. A 5-minute sweep catches misses. *Auto-Reconcile* matches each line to the receipt's Payment Entry by exact amount (SC:63-68).
- The Payment Entry carries the M-Pesa receipt as Reference No, with the transaction date as posting date. It is allocated to the matched Payment Request, Sales Order or Sales Invoice, capped at the outstanding amount (SC:80-86).
- Today Astral's scripts do the money-in part (Register To Bank Transaction on save, Auto Reconcile every 5 min). The suite takes over in Phase 2 below.
- **Residual risk on 208911.** Audit item 1 (forged callbacks booked as real money, `../docs/AUDIT_2026-10-18.md:53-57`) stays open for the main paybill. 208911 has no initiator, so its C2B confirmations are not verified (`money_in/c2b_verify.py:62-71`). The C2B token (D14) and the IP allow-list (D13) are deferred, and 208911 has `auto_reconcile_c2b` = 1 (copy). A forged confirmation to 208911 is therefore registered and auto-booked, the same as under Navari today. The first follow-up that closes it is an initiator with the Transaction Status role on 208911 (section 9). S2 (C2B token) also closes it, and so does S3 once Frappe Cloud confirms how X-Forwarded-For is set (audit item 2).
- **Money out, from f427f2b.** Today B2C withdrawal lines and charge lines come every 5 minutes from Astral's "AIL Mpesa Feed - B2C Payout To Bank" and "B2C Voucher Complete" (`scripts/AIL_Mpesa_Feed_B2C_Payout_To_Bank.py:1-14`; copy: 970 B2C lines and 165 charge lines in 30 days). From f427f2b the suite makes a withdrawal line and a charge line for each Paid B2C and B2B payout within seconds, reconciled to the payout's entry, once "Create Bank Transaction for Every Receipt and Payout" is on (SC:701-780). The two AIL scripts stay on until then and are disabled at that moment.
- **No lines at all** today for B2B payments and their KES 2 charges, reversals, float top-ups and Utility-to-bank sweeps. Five settlement lines (KES 5,166,281) were entered by hand in 30 days (copy). See section 9.
- Suite money-out lines are committed as **f427f2b** ("feat: payouts and their Safaricom charges as M-Pesa Bank Transactions"), the commit after 6097014, pushed to `suite/astral-suite` on 2026-10-22 with CI green on v15 and v16 (run 39805187216); its bench tests also passed locally on v15 and v16. It adds the patch `payout_lines_from_upgrade_date` and a Money Out tab, and the Money In switch becomes "Create Bank Transaction for Every Receipt and Payout" (`git show --stat f427f2b`; SC:701-780). Payout lines stay off until that switch is on, so deploying f427f2b changes nothing until then. Release C, **ee5b201**, is f427f2b plus the wallet work a68d401 (no effect on bookings until staff map a sub-account or import a statement; Get Account Balance records Balance Snapshots, D3), R3 (02d94a0 and 088608d: the app refuses its own Uninstall, see the never-uninstall bullet above) and a CI fix; CI is green on v15 and v16 (run 39842536376). D3 recommends C, and Astral gets it only after it passes the Trident pilot (P7). Frappe Cloud deploys a protected release branch cut at ee5b201, not `astral-suite` (D3).

## 0. Pilot on Trident Plumbers first

Trident Plumbers LTD's Frappe Cloud site already runs the suite, at a commit from about 2026-10-20 (around 13626df: its Patch Log stops at `mpesa_settings_api_switches`). It is on its own bench group with Frappe/ERPNext 15.122.0, and it has no Mpesa Settings, C2B receipts, STK prompts or payouts (Trident site, read 2026-10-22). Every Frappe Cloud step for Astral is proven there first, with no M-Pesa money at stake.

| # | Step | Who | Done when |
|---|---|---|---|
| P1 | Turn Trident's auto-update off (RP T1). Create the release branch at ee5b201 (D3), push it and protect it on GitHub (RP section 5), then point the `frappe_mpsa_payments` app on Trident's bench group at it. Validate App, then Deploy the bench group. | Developer + IT lead | Auto-update shows off. Deploy succeeds. Build time noted. The bench group shows `frappe_mpsa_payments` at ee5b201 (if the dashboard shows the commit hash; not checked; RP T3). |
| P2 | Take an on-demand backup of Trident. Site Config: add `pause_scheduler` = 1, as Astral's runbook step 2 does. Update the Trident site and time it. Then remove the key, as step 9 does; do not set it to the text "0" (RP T15). | IT lead | Update succeeds. Time noted: this sizes Astral's window. `/app/rq-job` showed "Scheduler: Inactive" while the key was set and "Scheduler: Active" after it was removed (RP T7, T15). |
| P3 | Desk checks on Trident. Patch Log gains the 3 patches missing since 20 Oct: `drop_text_search_index_setters`, `relink_b2c_reference_rows` and `payout_lines_from_upgrade_date`, so it now has every line of C's `patches.txt` (RP T3, check A6). M-Pesa Suite Settings shows the new Money Out and M-Pesa Accounts tabs. Customize Form → Bank Transaction → Save works. No new Error Log. | IT lead | All pass. |
| P4 | Restore rehearsal: create a **new** site from the P2 backup, not Trident's live site, if Frappe Cloud allows it. Time the restore and note the exact steps for section 7. If Frappe Cloud cannot restore to a new site, do this on Astral's staging (T-11) instead. | IT lead | Steps and time written into section 7. |
| P5 | Sandbox test on Trident. Create Mpesa Settings "Daraja Sandbox" with the Daraja sandbox keys, typed by staff in the desk. Set `mpesa_sandbox_lab` = 1 in Site Config from the dashboard. Run the Sandbox Lab: STK to a staff phone, C2B URL registration and simulate, and B2C/B2B with simulated results where the sandbox initiator is locked (docs/SANDBOX_TESTING.md). Trident's public URL receives Safaricom's callbacks directly, with no tunnel. | IT lead + a staff phone | The lab cases pass as they did on the local lab (SC:491-536). |
| P6 | Optional, only if Trident has its own paybill or till and a Daraja Go-Live app: tiny live tests sent by staff, like T1, T3 and T8. Trident has no M-Pesa Mode of Payment today (Trident site). | Staff | Each test passes. |
| P7 | Exit: P1–P5 pass. Astral then deploys **the same release-branch commit** that passed on Trident (D3) and follows sections 2–9. | Finance lead | Decision written down. |

From P2 on, Trident runs R3: Site → Apps → Uninstall of `frappe_mpsa_payments` is refused unless `mpsa_suite` is installed and owns the module "Mpsa Suite", or Site Config has `mpsa_allow_old_uninstall` = 1 or true (never-uninstall bullet in the short answers above). The guard refused a dry-run uninstall in the v15 test (spike15:f2.log:3-13; branch:docs/RENAME_SPIKE.md); whether the Frappe Cloud dashboard shows its message is not checked. Do not press Uninstall to test it, and never set that key. P1 and P2 are also steps T1 to T3 of the rename pilot (RP section 5). The rename steps after them (T4 on) are not part of this plan. RP's hard prerequisite, Support's confirmation of the `pause_scheduler` key (RP section 9.1 #4), is needed before RP T7 and before runbook step 2 on Astral, whichever comes first. P2 only tries the key on Trident, where nothing depends on it, so P1–P5 need not wait for Support.

## 2. Decisions needed before the day

| # | Decision | Recommended answer | Basis |
|---|---|---|---|
| D1 | Date and window | A Sunday, starting early morning EAT, with the developer, an M-Pesa Manager and the accountant free until evening. First confirm no payout run is planned. | Copy: Sundays had 2 C2B, 2 STK and 6 B2C in ~3 months. C2B/STK are 0 from 20:00 to 06:59, but B2C had 70 at 20:00. |
| D2 | Frappe Cloud path | Proceed: the in-place source swap is proven on Trident (section 0). Astral's group is a different private bench group, so repeat the same Add/Update App flow there. Stop only if Astral's group refuses it. | Section 0; Trident site |
| D3 | Commit to deploy | Recommended: **release C, ee5b201** (RP section 1). It is f427f2b (6097014's C2B fix plus the money-out lines, inert until their switch is on), plus a68d401 (wallet sub-accounts, statement import, balance checks), R3 (02d94a0, 088608d) and a CI fix. a68d401 adds no patch and no document hook, asks Safaricom for nothing while "Ask Safaricom for Balances Every (Minutes)" is 0, the default, and books nothing until staff map a sub-account or import a statement. Until then it adds 4 DocTypes, 4 Custom Fields, an M-Pesa Accounts settings tab and workspace links. Each Get Account Balance result also records the wallets' balances in Mpesa Balance Snapshot, a B2B result also stores its debit account's balance text, and the default B2C Charge Bands start with "100 = 0", used from M5 (`wallet/balances.py:73-83,171-198`; `inner/api/m_pesa_api.py:179-185`; SC:737,793-794,856-859). The 5-minute check then marks each snapshot "Cannot Check" until that wallet's statement is imported (`wallet/balances.py:98-100`). a68d401 also changes code the go-live runs from day 0: the B2C Payment Entry and Journal Entry paid-from account, the B2B Payment Entry's Bank Account, statement-line creation (each line is stamped with its wallet and completion time), the duplicate guard's list of M-Pesa Bank Accounts, Mpesa Settings validation and the balance callback. With no Sub-Accounts row each uses the same account as at f427f2b (`wallet/accounts.py:45-67`; SC:793-794,906). Rehearsal 2 ran at 75de2f1, before f427f2b and a68d401 (SC:643-656), so the staging runs (T-12, T-13) and rehearsal 3 (T-15) must run at C. **Why C, not f427f2b:** R3 must ship in this stage (RP section 4.2 R3), and the rename starts from a site running C, from which the `mpsa-suite` branch is built (RP section 3.2 A). f427f2b and 6097014 lack R3, so a site left on either would need another old-name deploy before the rename. C's patches are f427f2b's 8 (`patches.txt` is the same at both). CI is green on v15 and v16 for ee5b201 (run 39842536376). Create a release branch, e.g. `astral-suite-golive`, at ee5b201 and push it to the suite remote **before** Validate App. Point both Trident (P1) and Astral at that branch. Protect it on GitHub so nothing lands on it after C (RP section 5; not done yet): the `mpsa-suite` branch is built from C, a Deploy takes the branch head, and an old app past C could ship a DocType that branch lacks (RP section 3.2 G). RP says the same: do not deploy `astral-suite` itself (RP sections 1 and 5). This plan uses the release branch: it stays at ee5b201 even when `astral-suite` gets later commits, such as these plan documents. Code work after C goes to `mpsa-suite`, not to an old-name branch (branch:docs/RENAME_SPIKE.md:16-17). Until Astral's Stage B, that work adds no patch: install-app would record it as done without running it (RP section 4.4, point 4). Whether Frappe Cloud can select a tag is **unknown**, so use a branch. | `git log`; `git diff f427f2b ee5b201`; CI; SC:688-914; RP sections 1, 3.2 A, 4.2 R3, 5 |
| D4 | Staging | A Frappe Cloud staging site restored from a fresh live backup on the same private bench group. Also recommended: rehearsal 3 on `astral.local` from the same backup, with outbound network blocked. | The copy is stale: data to 2026-10-02, frappe 15.121.3 vs live 15.123.1 (`../astral_export/astral_config_snapshot.md:6`) |
| D5 | Who sends real money | Astral staff only: the finance lead pays C2B/STK from a phone, an M-Pesa Payer sends B2C/B2B, a cashier runs the till sale. An assistant only reads the ERP afterwards. | Task rules |
| D6 | Test documents and clean-up | The accountant picks a test customer, Sales Order/Invoice and supplier, and decides the clean-up (credit note, reversal). Test receipts post real GL. | SC:664-680 |
| D7 | Server Scripts in phase 1 | All stay on, except: disable `astral_b2c_release_payout` at the update, and disable "AIL B2C - Payment Entry Backstop" once the live B2C test shows the suite booked the payout once. | Section 5 |
| D8 | Verify C2B Confirmations | Keep it on. With 6097014 a shortcode's confirmations are held for verification only when its Mpesa Settings has **both** an Initiator and a Security Credential. Otherwise they register on arrival as under Navari, plus a daily Error Log. Copy: 208911 has no initiator, 4134019 has 'janki', 4139683 has 'skerai', and Security Credential is empty on all three (possibly from neutralisation). So on live, 4134019 is verified only if it has both; without a credential it behaves like 208911 (T1 result). If it has both, verification also needs 'janki' to hold the Transaction Status role (**unknown**); if that is not confirmed, untick the switch right after migrate. It is one global switch. | `money_in/c2b_verify.py:62-92`; `inner/api/m_pesa_api.py:746-754`; copy |
| D9 | Transaction Limit on 4134019 | Raise 150,000 to 250,000 at the update. A Payment Request above the limit is now refused, not split. | SC:570-573 |
| D10 | B2B float in Working | Finance sets the amount: at least KES 12 for the test (KES 10 plus the KES 2 charge), plus the expected supplier payments. The amount is **open**. | SC:664-686 |
| D11 | Which shortcode pays B2B first | 4139683 only, since it is lab-proven. First test: KES 10 from 4139683 to Astral's own paybill 208911. It is allowed, because the code refuses only the paying record's own shortcode or Party A, and the lab sent exactly this and it was Paid (receipt UJMTM632YM). It is the lowest-risk test, since the money stays with Astral; 208911 must first be approved as a Supplier M-Pesa Account. Whether 208911 also gets a C2B confirmation for it is **unknown**; if a register appears, the accountant handles it (D6). A real approved supplier is the second test. | `money_out/b2b.py:136-140`; SC:664-675 |
| D12 | Integration User | A new dedicated System User with Accounts User, Sales User and, after migrate, M-Pesa Manager. Name **to be chosen**. | SC:121-122 |
| D13 | IP allow-list | Leave off until Frappe Cloud confirms how it sets X-Forwarded-For. | Section 5, S3 |
| D14 | C2B token | Not in go-live week. It needs Safaricom to delete the production URLs first. | Section 5, S2 |
| D15 | Rollback deadline | The end of the window, before staff start work. After that, fix forward. | Section 7 |

## 3. Before the day (T-7 to T-1)

| # | Step | Who | Done when |
|---|---|---|---|
| 1 | Already known: private bench group, no SSH, in-place swap works (Trident). Ask Frappe Cloud support the rest: Is auto-update on for the live site? Does a deploy also pull newer commits of the other 16 apps? Does a site update use maintenance mode or pause the scheduler? How are a backup restore and a move back to the previous deploy done, and how long do they take? Can we set `maintenance_mode` in Site Config for a rollback? How is X-Forwarded-For set? | IT lead | Written answers on file. Stop if the swap needs an uninstall. |
| 2 | Trident pilot (section 0) passed. | IT lead | P7 decision written down. |
| 3 | Connect Frappe Cloud's GitHub app to the private repo (FC). | IT lead | Repo visible in Add App. |
| 4 | Use the release branch that passed the Trident pilot (D3, P1); it is already pushed to `github.com/syamkerai/frappe-mpesa-suite`. Confirm CI is green on v15 and v16 for its commit (for ee5b201: run 39842536376). | Developer | Branch name and hash recorded here: **(placeholder)**. |
| 5 | Prepare the rollback code: a validated branch at Navari 22c9f05 plus `[tool.bench.frappe-dependencies]` (22c9f05 has none), or Frappe Cloud's confirmation that the site can go back to the marketplace source or the old deploy. | Developer + IT lead | Rollback source passes Validate App, or FC confirms. |
| 6 | Re-export live Server Scripts and compare with the 2026-10-18 export. Scripts changed after the copy: Pull Sweep and pull_register (10-10), Backstop (10-07), WA_Payment_Order (10-13). | Developer | Differences noted in this plan. |
| 7 | Read live Mpesa Settings in the desk: Sandbox = 0 on all three; Initiator and whether a Security Credential is stored (copy: initiators 'janki' on 4134019 and 'skerai' on 4139683, none on 208911; Security Credential empty on all three, possibly from neutralisation); Transaction Limits; old API Type. These decide D8 and T2. Read Site Config: `host_name`. | IT lead | Values noted. Expected `host_name` is `https://erp.astralind.com` (inferred from copy logs; unconfirmed). |
| 8 | On the M-PESA Org Portal: does 'janki' (4134019) hold Transaction Status (and Reversal, B2B)? Does 'skerai' (4139683) hold Transaction Status (needed by `check_unsettled_payouts`)? Does the B2B payer hold the Business Paybill / Buy Goods initiator roles? Is there a plan for a 208911 initiator? | Finance lead | Answers noted (all **unknown** today). |
| 9 | Pull: which shortcodes are Pull-registered at Safaricom, and with which nominated number? The Pull Sweep comment names only 4134019. | Finance lead | Answers noted (**unknown** today). |
| 10 | Daraja portal → URL Management: note the host the C2B URLs of 208911 and 4134019 were registered with. That domain must stay on the Frappe Cloud site. | Finance lead | Host noted (**unknown** today). |
| 11 | Staging: restore a fresh live backup into a new site on the same bench group, **before** the fork is deployed. Neutralise it at once (next table). | IT lead | Staging up, neutralised, no Safaricom calls. |
| 12 | Deploy the release branch on the bench group with live auto-update **off**, then update staging only (Navari → the release-branch commit, the same move as live). Time the update. Before it, record the counts of section 4 step 3 on staging. After it, run the section 4 checks for steps 6–7 **only**. Step 8 (balance query) and every section 6 test run only on live: any Safaricom call from staging revokes live's token (`../docs/daraja_api_reference.md:39`), and with keys cleared it just fails. | IT lead + Developer | Update time recorded: **(placeholder)**. No row created before the step-3 time is missing. The Custom Field, Property Setter and Patch Log changes are recorded as the expected values for live: **(placeholder)**. |
| 13 | On staging, with Consumer Key/Secret cleared: run the payout accounting job about 5 times as a System Manager. v15 has no Execute button; from the browser console run `frappe.call('frappe.core.doctype.scheduled_job_type.scheduled_job_type.execute_event', {doc: JSON.stringify({name: 'b2c_postprocess_service.retry_unbooked_payouts'})})`. It enqueues the job with `force=True` (`frappe/core/doctype/scheduled_job_type/scheduled_job_type.py:189-193`; the job name comes from `autoname`, :52-53). (There is no SSH on Astral's bench group, so this browser call is the way.) Whether this works on a Frappe Cloud staging site with `pause_scheduler` = 1 is unverified; check Scheduled Job Log after each run. Then list every Payment Entry and Journal Entry created since the first run, payouts with an Accounting Error, and Error Logs titled "B2C payout … booked elsewhere". Only payouts from the last 30 days are checked (`services/b2c_postprocess_service.py:123,146`), so the September payouts in section 9 will **not** appear. | Accountant + IT lead | Every new entry reviewed and agreed before go/no-go. Accounting Error list agreed (section 9). |
| 14 | On staging: check Role Permissions. Custom DocPerm replaces the suite's permissions (SC:212-217). Copy findings: Accounts User, Accountant, Accounts Manager and System Manager can write and create Mpesa Settings (audit item 10 stays open); Integration Request is readable only by Admin, Super Admin and System Manager, so an M-Pesa Manager cannot open a held confirmation to press "Release Without Safaricom Confirmation" (`public/js/c2b_verification.js:12`); Mpesa Express Request also has Custom DocPerm; B2C Disbursement Request and B2C Payment Disbursement give create, write, submit and cancel only to B2C User (copy). Plan the rows in desk item 3. Test the Integration User. | IT lead | Permission changes listed. |
| 15 | Rehearsal 3 (recommended): download the same backup, restore it on `astral.local`, upgrade from Navari to the release-branch commit (D3), block outbound network, run each M-Pesa job once, compare counts (as in SC:643-656). | Developer | No row loss. Custom Field / Property Setter / Patch Log changes noted. Notes added to SC. |
| 16 | Create the Integration User on live (System User, Accounts User + Sales User). Add M-Pesa Manager after migrate, since the roles only exist then. | IT lead | User exists. |
| 17 | Move the B2B float into the Working (MMF) account of 4139683 (D10). | Finance lead | Working balance > 0 on the Org Portal. |
| 18 | List who gets M-Pesa Manager, M-Pesa Payer (every current "B2C User" holder who retries payouts) and M-Pesa Viewer. | Finance lead | List ready. |
| 19 | Edit Server Script texts that tell staff to call `astral_b2c_release_payout` or say "released automatically by HH:MM" for a payout (Watchdog, `astral_b2c_payout_status`). Add a guard to `astral_casual_cancel_failed` so it refuses unless every request is Failed. | Developer | Scripts saved on live. |
| 20 | Tell staff: the window, no STK prompts or payouts during it, new refusals (whole shillings 10–250,000, one open payout per document), and what "Status Unknown" means (never resend; wait for the status check). | Finance lead | Message sent. |

**Neutralise staging before its scheduler runs** (copy precedent: `pause_scheduler=1`, `mute_emails=1`, Sandbox=1 on all records):
- Restore at about 20 past the hour. The Pull Sweep runs at :15 and the Balance Tie-Out at 06:30 (`scripts/AIL_Mpesa_Feed_Pull_Sweep.py:2`, `AIL_Mpesa_Feed_Balance_Tie_Out.py:2`). Whether a restored site starts with the scheduler on is **unknown**.
- Site → Site Config: add `pause_scheduler` = 1, `mute_emails` = 1 and `host_name` = the staging URL, never erp.astralind.com. This path comes from `docs/SANDBOX_TESTING.md:14` and is unverified. System Settings cannot do it, because the v15 "Enable Scheduled Jobs" field is hidden (`frappe/core/doctype/system_settings/system_settings.json`).
- Disable these Server Scripts: AIL Mpesa Feed - Pull Sweep, Balance Tie-Out, B2C Payout To Bank, AIL B2C - Payout Watchdog, `ail_mpesa_pull_register`, `bank_collections_digest`.
- Clear Consumer Key/Secret on the three Mpesa Settings, or tick Sandbox.
- Never submit B2C, B2B, STK or Reversal on staging. Never press Register URLs or Register Pull there. Every OAuth token request revokes the previous token, so one staging call breaks live's cached token (`../docs/daraja_api_reference.md:39`).
- Whether a Frappe Cloud restore carries the encryption key, so live secrets would decrypt, is **unknown**. Treat staging as if they do.

## 4. Cutover runbook (the day, Frappe Cloud)

Expected total: about 4–5 hours. The Frappe Cloud backup, update and build times are **unknown** until staging is timed.

| # | Step (dashboard or desk) | Time | Check before going on |
|---|---|---|---|
| 0 | Go/no-go: written FC answers, staging passed (T-12, T-13), CI green, people present, rollback source ready. | 15 min | All yes. Otherwise postpone. |
| 1 | Freeze M-Pesa sends: no STK prompts, B2C, B2B or reversals until step 11. Write down the time: the window starts here. | — | No B2C Disbursement Request in Initiated or Status Unknown. No Express Request In Progress from the last hour. |
| 2 | Site Config: add `pause_scheduler` = 1. This stops Astral's 18 scheduler scripts (Backstop, Voucher Complete, B2C Payout To Bank and others), which otherwise keep making entries, so the counts in step 3 hold still. Keep the pause short. Jobs queued by callbacks still run, and customers can still pay 208911 and 4134019. Support's confirmation of this key (RP section 9.1 #4) and its trial on Trident (P2) come first. | 5 min | No new rows in Scheduled Job Log for 5 min. |
| 3 | Write down the time **T0**, then record counts from list views. For Payment Entry, Journal Entry, GL Entry, Bank Transaction, Payment Request, Mpesa C2B Payment Register, Mpesa Express Request, B2C Disbursement Request, B2C Payment Disbursement and Integration Request, add the filter Created On ≤ T0, so new receipts arriving later do not change the number. Also record Custom Field (total, and Module = "Frappe Mpsa Payments"), Property Setter, Patch Log (Patch like `frappe_mpsa_payments%`) and Mode of Payment. | 15 min | Screenshots saved, with T0. |
| 4 | Dashboard → site → Backups: take an on-demand backup, then **download** it. Write down its time. | **unknown** (DB >600 MB gzipped on the copy) | Backup listed with time; file on a local disk. |
| 5 | Update the live site to the new deploy (deploy made in T-12 with auto-update off). Migrate runs. Write down start and end times. | **unknown**; staging time + margin | Update shows success; desk loads; Installed Applications shows `frappe_mpsa_payments` 0.0.1. |
| 6 | Post-migrate checks (below). | 20 min | All pass, or go to section 7. |
| 7 | Desk configuration (below). | 30 min | Each item saved. |
| 8 | Get Account Balance on 4139683 and 4134019 (live only). | 10 min | Integration Request "Mpesa Balance Query" Completed; balance shown. |
| 9 | Remove `pause_scheduler` from Site Config. Write down the time. `retry_unbooked_payouts` now starts on about 20 days of payouts the copy never had (section 5). | 5 min, then 1 h of review | Within 15 min, Scheduled Job Log shows the suite jobs and the AIL scripts running; no new Error Log titles from section 8. Within the hour the accountant reviews every Payment Entry (Pay) and Journal Entry created since this time: each belongs to a Paid payout that had no entry, and none is a second entry for one payout. |
| 10 | Reconcile the window. From the M-PESA Org Portal, download the statements of 208911, 4134019 and 4139683 from step 1 to step 9. Compare them with Mpesa C2B Payment Registers, Express Requests, held "Mpesa C2B Verification" Integration Requests and B2C requests. Enter or release every miss. A callback that got an error while the site was updating never reaches the ERP, and whether the update uses maintenance mode is **unknown** (T-1). Can run alongside step 11. | 30 min | Every statement line in the window has its ERP record. Input to step 12. |
| 11 | Live tests (section 6). Lift the send freeze. | 60–90 min | Every test passes. |
| 12 | Go/rollback decision (section 7), using steps 6, 9, 10 and 11. | 10 min | Decision written down. |

**Migrate does** (`patches.txt`; `~/rr-bench/apps/frappe/frappe/migrate.py:111-158`):
- Pre-sync patch `create_suite_roles`, then the DocType sync (17 new DocTypes at C, 4 of them from a68d401, and new columns; sync never drops columns).
- Post-sync patches: `c2b_payment_register`, `payment_entry_custom_fields`, `remove_unused_upstream_features`, `mpesa_settings_api_switches`, `drop_text_search_index_setters`, `relink_b2c_reference_rows`, `payout_lines_from_upgrade_date` (C's `patches.txt`).
- Then the 8 new Scheduled Job Types (Navari had 1; `hooks.py` `scheduler_events`). The new `wallet.balances.run` asks Safaricom for nothing while the balance schedule is 0 (`wallet/balances.py:193-198`). Then `after_migrate`: roles, M-Pesa Suite Settings with a callback token, indexes on Bank Transaction, Payment Entry and Integration Request, and the suite's Custom Fields. C adds two read-only Bank Transaction columns, one indexed (`inner/migrate.py`); the migrate times in the summary were measured before them.
- `remove_unused_upstream_features` deletes the Stanbic and Mpesa Payment Reconciliation DocTypes and their fields. They are empty on the copy, and tables are not dropped.
- Migrate makes no Safaricom calls (SC:181-184,357-359; `inner/patches/*.py`).

**Post-migrate checks (step 6)**
- Data doctypes: with the same filter Created On ≤ T0, every count equals step 3. This means no older row was lost. Rows created after T0 (new C2B/STK receipts, their Integration Requests, Payment Entries and bank lines) are expected; list them separately.
- Metadata: Custom Field, Property Setter and Patch Log changed by the amounts recorded on staging (T-12). Without a staging figure, rehearsal 1 plus rehearsal 2 give the Navari → suite change at 75de2f1: Custom Field **+36**, all with Module = "Frappe Mpsa Payments" (8 → 44 on the copy); Property Setter about **+5** (rehearsal 1 905 → 911, rehearsal 2 911 → 910); Patch Log +7 suite patches (`~/mpesa-rehearsal` `compare.py before.json after.json`; `~/mpesa-rehearsal/r2/compare.txt`; copy). C's code defines 10 more Custom Fields under that module (6 payout fields from f427f2b, 4 from a68d401) and one more patch, so at C expect about **+46** Custom Fields (8 → about 54) and **+8** in Patch Log; these two figures come from the code and were not measured (`git diff 75de2f1 ee5b201`). If the deploy also updated other apps, their fixtures move these totals too: the copy gained 59 Custom Fields from other apps on 2026-10-22 (copy). Then compare only the Module = "Frappe Mpsa Payments" rows. Mode of Payment: unchanged.
- Patch Log has the 8 suite patches, every line of C's `patches.txt`. Live had only Navari's 2 (copy).
- Error Log: no new entries since step 5 apart from known noise ("Payment Entry ACC-PAY-… is not affecting bank account Mpesa-208911 - AIL").
- Customize Form → Bank Transaction → Save (no change) works. Before rehearsal 2 it failed with "Fieldtype Small Text … cannot be indexed" (SC:636-639).
- Mpesa Settings 208911, 4134019, 4139683 have Sandbox = 0 and Old API Type unchanged. API switches: 4139683 = B2C; 4134019 = C2B + STK + B2C; 208911 = C2B + STK; B2B and Reversals off (SC:456-489; copy).
- M-Pesa Suite Settings exists. These are ON: duplicate guards, receipt as reference, cost center, amendment clearance, phone, Verify C2B, Verify STK. These are OFF: Bank Transactions, Reconcile, Matching, Sales Orders, Polling, Status Sync, Supersede, Voucher Clearance, and every enforcement switch (copy). "Ask Safaricom for Balances Every (Minutes)" on the M-Pesa Accounts tab is 0 (C's default).
- The till Modes of Payment and POS profiles are unchanged.

**Desk configuration (step 7)**
1. Verify C2B: keep or untick per D8.
2. M-Pesa Suite Settings → Integration User = the user from T-16. Add M-Pesa Manager to that user. Keep "If No Integration User" = Warn until the first live callback has run as that user, then set it to Refuse (SC:173-175).
3. Give the M-Pesa roles from T-18. No user holds M-Pesa Manager after migrate. Astral's Custom DocPerm replaces the suite's permissions (SC:212-217; copy), so in Role Permissions Manager:
   - Mpesa Settings: remove write and create from Accounts User and Accountant (keep read); this closes audit item 10. Accounts Manager also has write and create today, while the suite intends read and export for it (SC:212-217): finance decides. Add M-Pesa Manager (read, write, create) and M-Pesa Viewer (read).
   - Integration Request: add M-Pesa Manager read and write, so a manager can open a held confirmation and press "Release Without Safaricom Confirmation" (`public/js/c2b_verification.js:12`; the release itself checks System Manager or M-Pesa Manager, `money_in/c2b_verify.py:34,302`) and do the section 8 checks. Today only Admin, Super Admin and System Manager can read it (copy).
   - Mpesa Express Request and Mpesa C2B Payment Register: add M-Pesa Manager (read, write) and M-Pesa Viewer (read).
   - B2C Disbursement Request and B2C Payment Disbursement: today only B2C User can create, write, submit and cancel (copy). Either add M-Pesa Payer rows with those rights, or let every payer keep B2C User. Add M-Pesa Manager and Viewer read.
4. Accounting Mapping: 208911 → "Mpesa Till 208911 - Mpesa", 4134019 → "Mpesa Till 4134019 - Mpesa", 4139683 → "Mpesa B2C 4139683 - Mpesa", plus Cost Center. M-Pesa Charges Account can wait, but set it before M5 on each paying record ("5.2.03.12 - (J015) M-Pesa Charges - AIL", SC:771-773): from f427f2b the payout charge Journal Entries debit it, and without it each charge line waits with an Error in Mpesa Match Log (`money_out/statement.py:447-461`). Leave Sub-Accounts (a68d401) empty: Working stays on the shortcode's Bank Account, as today (SC:891-893).
5. 4134019 Transaction Limit → 250,000.
6. Disable Server Script `astral_b2c_release_payout`. It fails a "Status Unknown" payout and re-opens its document for a second payout (`scripts/astral_b2c_release_payout.py:20-43`).
7. Confirm `host_name` in Site Config is the public `https://` address. Every callback URL in background jobs is built from it (SC:328-330).
8. Do **not** press Register URLs Again, Register Pull, Regenerate Callback Token or Regenerate Security Credential. Do not tick or untick Sandbox, and turn on no enforcement switch. Regenerating the credential is safe only if the stored initiator password is current. If you must, copy the old credential text first and run a balance query straight after (`inner/doctype/mpesa_settings/mpesa_settings.py:195-225`).

## 5. Switch-on order after cutover

Rule for every row: disable the Server Script, wait one cron cycle (check Scheduled Job Log), turn the switch on, then watch for a day. A System Manager disables a script by ticking Disabled. No bench is needed (copy: the Tamper/Delete Lock scripts guard only their own four scripts).

**Phase 1: day 0–1 (money out and identity)**

| Server Script | Suite switch / setting | Action and watch | Success signal |
|---|---|---|---|
| `astral_b2c_release_payout` | none (use "Check Status with M-Pesa" and `check_unsettled_payouts`) | Disabled at the update | No Status Unknown payout is resent. |
| AIL B2C - Payment Entry Backstop | none (suite books on the result and retries every 15 min) | Disable after live test T4 shows one Payment Entry | Paid payouts get `accounting_booked` = 1 within 15 min; no new "booked elsewhere" logs. |
| — | Integration User policy → Refuse | After the first live callback | Hourly "running as Administrator" logs stop. |
| AIL Mpesa Feed - B2C Payout To Bank, B2C Voucher Complete | "Create Bank Transaction for Every Receipt and Payout" (from f427f2b, so in C), turned on at M5 | **Keep on** until M5. At M5 disable both in the same minute the switch goes on (SC:760-765,771-773). | A withdrawal line and a charge line for each Paid B2C, from one source only, never both. |

`retry_unbooked_payouts` starts on its own at step 9 (every 15 min, `hooks.py:196-199`). Every historical Paid payout starts unbooked (copy: 1,068), and each run takes 100 from the last 30 days, so the first pass takes about 5 runs. Rehearsal 2 matched 412 payouts and created no new entries (SC:643-656), but only on data to 2026-10-02. On live it also meets about 20 days of payouts the copy does not have, alongside the Backstop. Hence the staging runs in T-13 and the review in step 9.

**Phase 2: money in, one row per working day**

| Day | Server Script(s) to disable at that moment | Suite switch to turn on | Watch | Success signal |
|---|---|---|---|---|
| M1 | AIL Mpesa Feed - Status Sync | Mark Payment Requests Paid When Settled | Payment Requests, Express Requests | Paid PRs show Paid; prompts Completed with receipt. |
| M2 | MPesa PR Cleanup Before Reprompt | Cancel Earlier Unpaid Prompt When Re-prompting | "Retry / Re-send STK" on Sales Orders | Old prompt cancelled properly; one prompt paid. |
| M3 | AIL BT Sync Voucher Clearance | Set Clearance Date When Every Bank Leg Is Matched | Clearance dates | Same dates as before. |
| M4 | AIL Mpesa Feed - Self Heal Unmatched, Invoice Collection Allocator, C2B No-Space Name Matcher, AIL Mpesa Feed - Payer Phone Matcher, AIL Mpesa Feed - Register Link Backfill | Match and Book Receipts Automatically. First lower Look Back (Days) from 45 and review drafts (copy: 19 on 208911). Untick Match Orphan STK until M6. | Mpesa Match Log (Error, Ambiguous) | Receipts booked once, against the right document. |
| M5 | AIL Mpesa Feed - Register To Bank Transaction, AIL Mpesa Feed - Auto Reconcile (both passes) | Create Bank Transaction for Every Receipt **and** Auto-Reconcile M-Pesa Bank Transactions, together. First check the M-Pesa Charges Account on 4139683 and 4134019 (desk item 4): without it every charge line waits with an Error in Mpesa Match Log (`money_out/statement.py:454-461`). | Bank Reconciliation Tool per M-Pesa account | One line per receipt, reconciled to its Payment Entry; the first sweep backfills 45 days without duplicates. |
| M6 | SO M-Pesa Payment Checker (the same minute) | Book Receipts Against Sales Orders. First set Approve Paid Orders As to a user with Sales Creator, Sales Approver Primary, Sales Approver Secondary and Credit Controller (copy: 15 users qualify, e.g. skerai@tridentplumbers.com). Re-tick Match Orphan STK. | Match Log; draft orders. Test "Customer Has Paid - Fetch Payment" on a paid draft order: that button only stamps the receipt's `billrefnumber`, and today the Checker then approves and books (`scripts/SO_M_Pesa_Fetch_Payment.py:12-13,73-74`; client scripts `SO_M_Pesa_Fetch_Payment_Submitted_Orders.js`, `SO_M_Pesa_STK_Request.js`). Confirm the suite's Sales Order booking now approves and books it; if not, untick the switch first, then re-enable the Checker (never both on). "WA Payment Order" also assumes the Checker approves stamped receipts (`scripts/WA_Payment_Order.py:21-24`); that no longer holds after M6. | Paid orders approved by workflow; credit-limited orders stay drafts with one Error row; a fetched payment is booked once. |
| M7 | AIL Mpesa Feed - Pull Sweep (same step) | Enable Hourly Pull Transactions on 208911 and 4134019, after live test T6 passes | Integration Requests "Mpesa Pull Transaction"; registers | Missed confirmations appear within 2 hours. Exactly one puller runs; never both, never neither. |
| later | none | Poll Safaricom for Unanswered STK Prompts, if STK callbacks do not land on live (Status Sync says they often don't) | Express Requests In Progress | Unanswered prompts resolve. |

Why the order matters:
- Self Heal books Payment Entries itself, defaulting to the 208911 GL, so it must be off before matching or statement lines start. Otherwise a 4134019 receipt could be booked twice on two GLs (`scripts/AIL_Mpesa_Feed_Self_Heal_Unmatched.py:11-12,58-107`).
- The SO Checker forces orders through the workflow with `ignore_credit_limit` (`scripts/SO_M_Pesa_Payment_Checker.py:81-146`). It must never run next to M6.
- Auto Reconcile cannot be half-disabled, which is why both switches go on together at M5.
- M7 needs the Pull Nominated Number on each record (empty on all three on the copy) and Pull registration at Safaricom (**unknown**).

**Retire after a quiet week** (safe alongside the default-on switches): AIL Mpesa Feed - BT Duplicate Guard, PE Reference To Receipt, PE Duplicate Receipt Guard, AIL PE/JE Amendment Clear Clearance, Normalize Mpesa Phone on Payment Request. **Last:** AIL Mpesa Feed - Duplicate BT Sweeper, after every line-creating script is off.

**Never retire with the suite:** AIL Mpesa Feed - PE Cost Center Autofill. It fills the cost centre on every Payment Entry, and the suite only on M-Pesa ones (`scripts/AIL_Mpesa_Feed_PE_Cost_Center_Autofill.py:5-22`).

**Keep** (the suite has no replacement):
- Payout Watchdog, the B2C pay dialogs and bulk/casual payout scripts, "B2C Request - carry user remarks" (without it every payout sends "here are my remarks"), and the Pause guard.
- The guest APIs `ail_mpesa_pull_callback` and `ail_mpesa_balance_result`, plus Balance Tie-Out. C has a balance schedule (a68d401), but it is off by default, and a difference it finds is only marked "Gap" on Mpesa Balance Snapshot, with no alert to anyone (`wallet/balances.py:87-128`). It can check a balance only after that wallet's statement is imported, otherwise "Cannot Check" (`wallet/balances.py:98-100`; SC:854). The tie-out alerts Accounts Managers (`scripts/AIL_Mpesa_Feed_Balance_Tie_Out.py:4-6`).

**Phase 3: callback security**

| Step | When | Action | Success signal |
|---|---|---|---|
| S1 Require Token on STK / B2C / Status / Balance / Pull results | Day 3 or later, once every request sent before the update has its result | Tick it. Keep `BALANCE_REFRESH = 0` in "AIL Mpesa Feed - B2C Payout To Bank": its balance results go to `balance_query_callback` without a token and would be rejected (`scripts/AIL_Mpesa_Feed_B2C_Payout_To_Bank.py:34,145-149`). | Rejection counter stays at 0. |
| S2 Require Token on C2B | Not in go-live week, and only after T6 passed and M7 (or the AIL Pull Sweep) is shown importing on **both** 208911 and 4134019. Pull is unproven today: the Nominated Number is empty on all three records (copy), Pull registration is **unknown** (T-9), and production URL registration is one-time (SC:426-428). If deletion or re-registration stalls, confirmations stop reaching the ERP. | In a quiet hour: 1) Delete the production C2B URLs (Daraja URL Management, two operators, or Safaricom support: both are documented, DV:54). 2) The IT lead (holding M-Pesa Manager) presses Register URLs Again on **live**, expecting a clean ResponseCode 0 with no "already registered". 3) The finance lead (D5) pays KES 10 to each shortcode. 4) Tick the switch; the finance lead pays again. 5) Reconcile the gap against the Org Portal statements of 208911 and 4134019, entering any miss. | Register created; counter unchanged; no statement line in the gap without a register. |
| S3 Only Accept Callbacks From Allowed IPs | Only after Frappe Cloud confirms X-Forwarded-For | Frappe uses the first X-Forwarded-For value (`frappe/auth.py:64-69`). There is no log-only mode, so a wrong proxy IP rejects every callback. | — |

Safaricom accepts a second production registration with 500.003.1001 and the app records it as Success. So a second "Register URLs Again" without deletion silently leaves the old URLs in place (DV Q13; `inner/doctype/mpesa_c2b_payment_register_url/mpesa_c2b_payment_register_url.py:20-26`). Once C2B token enforcement is live, do not press Regenerate Callback Token without repeating S2.

**Phase 4: B2B (day 2 or later)**
1. Tick B2B Supplier Payments on 4139683 and map its Bank Account (done in step 7).
2. Create a Supplier M-Pesa Account for 208911 (the first T8) and for the first real supplier, each with proof attached and M-Pesa's displayed name as the expected name. A different admin approves it. Keep "Hold New Supplier M-Pesa Accounts" at 24 h, since the lab guide sets it to 0.
3. Run test T8.

Known gaps:
- The B2B Payment Entry posts to the mapped Bank Account (Utility), not to Working, while Working has no Bank Account of its own. a68d401 lets Working be mapped (Sub-Accounts). It should be mapped only after a cut-over date and Working's opening balance (SC:894-896). The code does not enforce this (`wallet/accounts.py:107-131`), and importing the Aug–Oct statements with Working mapped would move KES 7.55M into Working twice (SC:884-889).
- With the switch on (M5; from f427f2b, in C), each B2B gets a withdrawal line and its KES 2 charge a charge line, booked and reconciled (SC:701-780). Before that, the charge is only stored and the Working account is reconciled by hand (SC:278-282; `money_out/b2b.py:226-261`).

## 6. Live verification with tiny real amounts

Real money is sent only by Astral staff (D5). An assistant may read the ERP afterwards.

| # | Test | Who sends | Expected result | Where to check |
|---|---|---|---|---|
| T1 | KES 10 C2B to 208911, account = test invoice/SO | Finance lead (phone) | Register created on arrival (no initiator, so it is not verified), plus one daily Error Log "M-Pesa C2B confirmations on 208911 are not verified". Payment Entry with Reference No = receipt. Astral's Register To BT makes the bank line. | Mpesa C2B Payment Register; Payment Entry; Bank Transaction; Error Log |
| T2 | KES 10 C2B to 4134019 | Finance lead | Verified only if live 4134019 has both an Initiator and a Security Credential (T-7, D8). Without a credential, expect the T1 result: registered unverified, daily Error Log. If verified: Integration Request "Mpesa C2B Verification" Completed, then register. If it Fails, read the failed Integration Request's error **before** unticking Verify. Two known causes: the initiator lacks the Transaction Status role, or the Transaction Status result had no CreditPartyName, which fails closed (SC:624-625). Either way an M-Pesa Manager releases the receipt (or Pull brings it in); untick Verify (D8) only for the role cause. | Integration Request; register; Error Log |
| T3 | STK from a Payment Request (gateway Mpesa-208911) | Staff submits PR; finance lead pays | Express Request has a callback payload, Verification Status Verified, Completed with receipt. Exactly one Payment Entry allocated to the PR's document; PR Paid; bank line reconciled. | Mpesa Express Request; Payment Request; Payment Entry |
| T4 | KES 10 B2C from 4139683, after T5 passes | M-Pesa Payer | Request Paid with receipt and receiver name; `accounting_booked` = 1; exactly **one** Payment Entry (none from the Backstop); withdrawal line plus CHG- line within ~5 min from the AIL feed. | B2C Disbursement Request; Payment Entry; Bank Transaction |
| T5 | Get Account Balance on 4139683 and 4134019 | M-Pesa Manager | Integration Request "Mpesa Balance Query" Completed; balances shown. Some token retries are normal. | Mpesa Settings; Integration Request |
| T6 | Manual Pull over a busy 47-hour window (Mpesa Settings → Pull Transactions) | Finance user | Imported + skipped = Safaricom's TotalRecords. TotalRecords is probably in the "Mpesa Pull Transaction" Integration Request (exact display **not verified**). It needs the Nominated Number. | Pull summary; Integration Request |
| T7 | Reversal of T2's receipt (optional) | M-Pesa Payer | Request Reversed; register commented; the receipt cannot be booked again. It needs Reversals ticked and an initiator with the Reversal role. Whether Safaricom enabled Reversal is **unknown**. Cancel any booked Payment Entry by hand. | Mpesa Reversal Request; register |
| T8 | Day 2+: first KES 10 B2B from 4139683 to Astral's own paybill 208911 (approved as a Supplier M-Pesa Account; allowed by `money_out/b2b.py:136-140`, Paid in the lab, SC:664-675), then KES 10 to a real approved supplier paybill (D11) | M-Pesa Payer | Paid with receipt; receiver name matches ("208911 - ASTRAL INDUSTRIES LIMITED" in the lab); Payment Entry with the receipt; KES 2 charge stored. | Mpesa B2B Payment Request; Payment Entry |
| T9 | Till 835813: a KES 10 sale on the POS profile staff use for the till | Cashier | Sale submits as before; no new Error Log. The live state of the till Modes of Payment is **unknown** (disabled on the copy). | POS / Sales Invoice; Error Log |
| T10 | Existing buttons: Customer / Sales Invoice "M-Pesa Collect", Sales Order "STK Request" | Staff | A Payment Request is made on Mpesa-208911 and prompts once. | Payment Request |

## 7. Rollback

**Hard triggers (restore, inside the window only):**
- The update or migrate fails and the site does not come up.
- A row created before T0 (step 3) is missing, or changed beyond what the migrate patches do ("Migrate does", section 4). Rows created after T0 never count against the upgrade.
- Custom Field, Property Setter or Patch Log changed by other than the staging amounts (T-12) and the difference cannot be explained.
- Payment Entry, Journal Entry, Bank Transaction or Payment Request saves fail with suite switches off.
- M-Pesa forms do not load.
- After step 9, `retry_unbooked_payouts` makes a second entry for a payout that already had one. First tick Stopped on its Scheduled Job Type (`b2c_postprocess_service.retry_unbooked_payouts`); restore if more than one payout is affected or the cause is not clear.
- T3 or T4 fails because of the app (not float or credentials).

**Soft triggers (no restore):** one feature misbehaves. Untick its switch in M-Pesa Suite Settings (every handler checks its switch first), or tick Stopped on its Scheduled Job Type, and re-enable the matching Astral Server Script.

**Recipe** (exact Frappe Cloud steps **unknown**; rehearse on staging):
1. Freeze M-Pesa sends again and set `pause_scheduler` = 1. Write down the time.
2. **Export before touching code.** From desk Report view (Menu → Export), export every Mpesa C2B Payment Register, Mpesa Express Request, B2C Disbursement Request, Mpesa B2B Payment Request, Mpesa Reversal Request, Integration Request (including held "Mpesa C2B Verification" ones), Payment Entry, Journal Entry and Bank Transaction created since the backup time (runbook step 4). Download the Org Portal statements of 208911, 4134019 and 4139683 for the same window. The restore deletes these records; without them a paid payout's document looks unpaid and could be paid again.
3. Optional but recommended: Site Config `maintenance_mode` = 1 for recipe steps 4–5. Frappe then answers every request, callbacks included, with 503 and refuses scheduled jobs (`frappe/app.py:228-232`; `frappe/exceptions.py:58-59`; `frappe/core/doctype/scheduled_job_type/scheduled_job_type.py:197-200`). Without it, Navari's code runs on the suite database between recipe steps 4 and 5, the untested state warned about below, and anything it processes is then overwritten by the restore. Callbacks refused in that time are recovered from the statement in recipe step 7. Whether the Frappe Cloud Site Config editor accepts `maintenance_mode`, and whether Frappe Cloud already sets it during a restore, is **unknown**. Code goes first: a Navari backup restored under the suite's code would be upgraded again by any migrate, because the suite patches are missing from its Patch Log.
4. Put the site back on Navari's code: the previous deploy if Frappe Cloud keeps it, or the rollback branch from T-5. Build time is **unknown**.
5. Restore the on-demand backup from runbook step 4 onto the live site. Restore time is **unknown**.
6. Check counts against runbook step 3 (Created On ≤ T0) and Navari's Patch Log. Remove `maintenance_mode`, then `pause_scheduler`.
7. Re-enter every exported item and every statement line from the window that the restored site lacks: B2C, B2B and reversals by hand, so their documents show as paid; C2B and STK receipts by hand. Treat Pull as a recovery path only for a shortcode where T6 has proven it. The AIL Pull Sweep is not proven: its header says it is disabled until a one-time Pull registration (`scripts/AIL_Mpesa_Feed_Pull_Sweep.py:10-12`, while its export line says `disabled=0`), and it skips any receipt with a submitted Express Request (`:50`). Pull covers only 47 hours, since Safaricom keeps 48 h.
8. Keep the send freeze until every exported item has been re-entered and checked against the statements.

**What is lost:** everything posted on the site after the backup, in all 17 apps, not only M-Pesa, except what recipe step 2 exported. Hence window only.

**What is kept:** Safaricom's money and statements, and the callback URLs (unchanged).

Do **not** try a code-only rollback (Navari code on the suite database). It was never rehearsed: 21 suite Custom Fields share names with Navari fields, with an unknown result.

## 8. First day / first week monitoring

**Day 1, hourly while staff work. Week 1, daily.**
- Error Log titles:
  - "M-Pesa C2B receipt … not verified"
  - "M-Pesa C2B confirmations on … are not verified" (daily for 208911, expected)
  - "M-Pesa C2B verification … failed"
  - "Delayed C2B Insert Error": the confirmation handler failed and no register was created (`inner/api/m_pesa_api.py:767`)
  - "M-Pesa C2B confirmation ignored": a confirmation came for a shortcode no Mpesa Settings matched (`inner/api/m_pesa_api.py:738-743`)
  - "M-Pesa STK settle/booking failed for"
  - "M-Pesa STK callback rejected for"
  - "B2C accounting failed for"
  - "B2C payout … booked elsewhere"
  - "B2C payout not sent:"
  - "[B2C Retry Error]"
  - "Mpesa B2C Transport Error"
  - "M-Pesa result mismatch"
  - "M-Pesa Suite: … failed"
  - "M-Pesa callbacks are running as Administrator"
  - "Mpesa Pull Transaction Error"
  - "[Hourly Pull Transaction Error] <settings>": the hourly Pull failed; matters from M7 (`services/b2c_pull_transaction_scheduler_service.py:39-42`)
  - "M-Pesa Balance Query Error"
  - "M-Pesa balance snapshot failed for" and "M-Pesa balance check failed for": recording or checking a wallet balance after Get Account Balance (a68d401, `wallet/balances.py:82,189`)
  - "M-Pesa B2B receiver name mismatch"
  - "M-Pesa statement line failed for"
  - "M-Pesa reconciliation failed for"
  - "Payment Entry Service Error"
- Ignore the old noise "Payment Entry ACC-PAY-… is not affecting bank account Mpesa-208911 - AIL".
- Integration Requests with service "Mpesa C2B Verification" that are Failed, or Queued for over 30 min. Read the error first. Two known causes of a failure: the initiator lacks the Transaction Status role, or the Transaction Status result had no CreditPartyName (fails closed, SC:624-625). An M-Pesa Manager then uses "Release Without Safaricom Confirmation" after checking the statement (needs the Integration Request permission from desk item 3), or Pull brings the receipt in. Untick Verify C2B only for the role cause.
- Express Requests: Verification Status Pending for over 10 min, Mismatch or Unverified; In Progress for over 60 min.
- B2C requests in Status Unknown, or Initiated for over 60 min. `check_unsettled_payouts` queries these, so **never resend by hand**.
- Paid payouts with `accounting_booked` = 0 and an Accounting Error note.
- B2B and Reversal requests in Unknown. Only an admin cancels them, after checking the statement.
- Scheduled Job Log: suite jobs and AIL scripts succeed.
- Mpesa Match Log: Error and Ambiguous rows, from M4 on.
- Rejected-callbacks counter on M-Pesa Suite Settings: stays 0.

**Daily accounting checks**
- No two live Payment Entries with the same Reference No on the same M-Pesa GL.
- Every C2B and STK receipt has a bank line.
- Every Paid B2C has exactly one withdrawal line (from the AIL feed until M5, from the suite after).
- The 06:30 balance tie-out alert is quiet.
- The Working account is reconciled by hand for B2B.

**End of week 1:** retire the Phase 2 "after a quiet week" scripts. Review Error Log volume, then decide on S1.

## 9. Known follow-ups and open questions

**Staff actions**
- **First:** get an initiator with the Transaction Status role for 208911, so its C2B confirmations can be verified. This closes the residual forged-callback risk on the main paybill (short answers).
- Allocate the payouts from rehearsal 2, working from **this list**. The suite flags such payouts only while they are less than 30 days old: `retry_unbooked_payouts` reads only the last 30 days (`services/b2c_postprocess_service.py:123,146`), and the double-payout guard stops counting older Paid-but-unbooked payouts (`inner/doctype/b2c_payment_disbursement/b2c_payment_disbursement.py:1071-1073,1088`). These 7 were created 2026-09-24 to 2026-09-29 (copy), so by go-live they are past 30 days: they get no new Error Log, they no longer block their documents from being paid again, and the T-13 staging list will not show them.
  - B2C-26-09-000809 (KES 32,000): ACC-PAY-2026-08667 → Purchase Invoice ACC-PINV-2026-01555.
  - B2C-26-09-000810 (4,000): ACC-PAY-2026-08668 → ACC-PINV-2026-01556.
  - B2C-26-09-000931 (20,000): ACC-PAY-2026-08850 → ACC-PINV-2026-01556.
  - B2C-26-09-000936 (2,836): ACC-PAY-2026-08859 → Purchase Order PORD26-01531.
  - Already on an invoice; nothing more is needed if that is what they paid: 000901 (ACC-PAY-2026-08787 on ACC-PINV-2026-01665), 000938 (ACC-PAY-2026-08861 on ACC-PINV-2026-01673-1), 001034 (ACC-PAY-2026-09045 on ACC-PINV-2026-01695).
  - SC:647-656 describes the guard while a payout is inside the 30-day window. Outside it, staff must allocate 000809, 000810, 000931 and 000936 from this list before anyone pays those documents again.
- Fill the Pull Nominated Number on each collecting record.

**Development**
- Money-out bank lines: B2C/B2B payout and charge lines are in f427f2b (`money_out/statement.py`, patch `payout_lines_from_upgrade_date`), CI green on v15 and v16. Reversals, float top-ups and settlement sweeps are still not covered.
- M-PESA statement import and sub-account balances (`wallet/`, new DocTypes Mpesa Statement Import, Mpesa Statement Row, Mpesa Balance Snapshot, Mpesa Sub Account) are in C as a68d401. They have no effect on bookings until a sub-account is mapped or a statement imported; Get Account Balance (runbook step 8, T5) already records Balance Snapshots, marked "Cannot Check" until a statement is imported (D3). Use them afterwards as SC:891-898 describes: Working not mapped at first, statements imported MMF first, and the duplicates and four wrong charge lines the import lists cancelled by staff. Before the first import the accountant agrees the periods: the dry run on the copy made 1,551 new charge lines and Journal Entries (KES 12,765) dated in August to October, then rolled back (SC:869-881). The import does not use Payout Lines From (`wallet/statement_import.py:233-258`). Set the M-Pesa Charges Account first (desk item 4). Code work after C goes to `mpsa-suite` and never reaches the release branch (D3).
- A token-carrying balance ResultURL for "AIL Mpesa Feed - B2C Payout To Bank", if `BALANCE_REFRESH` is ever set back to 1. The current URL also contains "query", which Daraja bans; whether Daraja already refuses it is **unknown**.
- An alert on a balance "Gap" from the suite's balance schedule (in C, off by default), so Balance Tie-Out can retire. The suite's check also needs that wallet's statement imported: without it every balance is "Cannot Check" (`wallet/balances.py:98-100`), so retiring Tie-Out also needs current statement imports.
- B2B accounting: from M5 the charge is booked to the M-Pesa Charges Account (f427f2b). A Working-account mapping exists in C (a68d401). It should be used only after a cut-over date and Working's opening balance (SC:894-896); the code does not enforce this (`wallet/accounts.py:107-131`), and importing earlier months with Working mapped would move KES 7.55M into Working twice (SC:884-889).
- The money-out scripts select records by the old API Type ("%B2C%"). B2C ticked later on 208911 would get no bank line or backstop.

**Frappe Cloud unknowns (ask support)**
- Answered 2026-10-22: private bench group yes, SSH no, in-place source swap works (Trident).
- Auto-update setting?
- Does a deploy also pull updates of the other 16 apps?
- Maintenance mode or scheduler pause during an update? Its length?
- Does Frappe Cloud take its own backup, and does it restore touched tables when migrate fails? Frappe writes `touched_tables.json` (`frappe/migrate.py:77-102`), but Frappe Cloud's use of it is unconfirmed.
- How restore and roll-back-to-previous-deploy work, and how long they take.
- Does a staging restore carry the encryption key, start with the scheduler on, and accept `pause_scheduler` in the Site Config editor?
- Does the Site Config editor accept `maintenance_mode` (rollback recipe step 3), and does Frappe Cloud set it itself during an update or restore?
- Can a tag be selected (a branch can: Add App picks repo + branch, FC)?
- How the proxy sets X-Forwarded-For.

**Live-site unknowns (read before the day)**
- Live `host_name` (expected `https://erp.astralind.com`, inferred from copy logs). Copy Error Logs also mention `astralerp.m.frappe.cloud`.
- The domain the C2B URLs were registered with.
- Sandbox, Initiator and Security Credential on each record.
- Is the stored initiator password current (Daraja passwords last 90 days)?
- The Transaction Status, Reversal and B2B roles of 'janki' and 'skerai'.
- Pull registration per shortcode, and whether the Pull Sweep imports on live today.
- Which Daraja URL-deletion process applies, and how long it takes.
- Where TotalRecords is shown after a manual Pull.
- Live Custom DocPerm and the holders of "B2C User".
- Whether the till Modes of Payment are enabled on live.
- Whether STK callbacks reach live at all. Status Sync says they often don't; the copy shows 4 "STK Push Callback Error" logs in a week.
- Paid payouts since 2026-10-02 with no Payment Entry. The sweep would book them on its first runs.
- Whether Safaricom has enabled Reversal on Astral's shortcodes.
- Whether a B2B payment into 208911 (first T8) also produces a C2B confirmation there.
- Whether the manual run of `retry_unbooked_payouts` (T-13) works on a Frappe Cloud staging site.
- Whether any non-M-Pesa path depends on the PE Cost Center Autofill script. Keep it either way.
- Whether Astral will ever pay B2C from 208911 or 4134019.
