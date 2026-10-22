# Rename plan: frappe_mpsa_payments to mpsa_suite ("Mpsa Suite")

Draft, 2026-10-22, updated the same day after the `mpsa-suite` branch was built and tested. For Astral's finance/IT lead and the developer. Nothing in this plan has been run on Trident or Astral.

**Status in one line.** Release C is `ee5b201` on `astral-suite`, and the renamed app is branch `mpsa-suite` at `27efca5`, with R1 to R9; CI is green on both. The hand-over worked with nothing lost on throwaway sites: first on Frappe v16, then on Frappe v15 (15.123.1, Astral's version; Trident runs 15.122.0) with the real branch (section 7.1). It has not been run on Frappe Cloud, on Trident or Astral, or on a site that came from Navari, as Astral did.

**Source key.** Every claim cites a file and line.

| Short name | Meaning |
|---|---|
| `repo:` | This repo's working tree on 2026-10-22: HEAD f427f2b plus the other session's uncommitted work. Line numbers can shift while that work continues. That work is now committed as a68d401. |
| `C:` | Release C: commit `ee5b201` on `astral-suite`, in this repo |
| `branch:` | The `mpsa-suite` branch at `27efca5`, in the worktree `C:\Users\syamk\Documents\Ai Brain\Mpesa All in ONE Frappe App\mpsa-suite-wt`. Its `docs/RENAME_SPIKE.md` records how the branch was built and tested. |
| `v15:` | `~/mpesa15/apps/frappe/frappe/` on WSL: Frappe 15.123.1, commit d2efa79 |
| `v16:` | `~/mpesa16/apps/frappe/frappe/` on WSL: Frappe 16.52.0, commit 4515014 |
| `new:` | The renamed copy used in the v16 spike, at `C:\Users\syamk\AppData\Local\Temp\claude\C--Users-syamk-Documents-Ai-Brain-Mpesa-All-in-ONE-Frappe-App\c5101ebd-7b20-48bf-b36f-b192e3b7b4c5\scratchpad\rename\mpsa_suite_app\`. Its line numbers are not the branch's. The lookup table is 9 lines lower on the branch, in the same order (branch:mpsa_suite/hooks.py:317-388, against new:mpsa_suite/hooks.py:308-379), and `handover.py` grew with R1, R2, R6 and R8. Where a location moved that much, this plan cites `branch:` instead. |
| `spike:` | The v16 test logs and probe snapshots, in `...\scratchpad\rename\spike\` (same folder as above) |
| `final:` | The v15 hand-over test and the bench tests at the branch tip, in `...\scratchpad\final\` (same scratchpad): `v15\`, `t16\` (v16 bench tests) and `cleanup.log` |
| `spike15:` | `final:v15\`, the v15 hand-over test: mpsa_suite code at 9927c16 (the same app code as 27efca5) and the old app at 02d94a0 |
| `build:` | `...\scratchpad\build\` (same scratchpad): the branch build, including `do_rename.out` from the rename run on 02d94a0 |
| `fix15:` | `...\scratchpad\build\fix15\`: install-app over a stale module map on v15, before and after the `before_install` fix |
| `verify:` | `...\scratchpad\verify_r2\`: the retest after the fix (unit tests, v16 install over a stale map) |
| `journal:` | The build-and-test workflow's stage reports, one JSON line per stage: `C:\Users\syamk\.claude\projects\C--Users-syamk-Documents-Ai-Brain-Mpesa-All-in-ONE-Frappe-App\c5101ebd-7b20-48bf-b36f-b192e3b7b4c5\subagents\workflows\wf_2552cf82-592\journal.jsonl` |
| `press:` / `agent:` | Frappe Cloud's own source code on GitHub (github.com/frappe/press and github.com/frappe/agent, master, read 2026-10-22). It may differ from what Frappe Cloud runs today. |
| `fc-docs:` | Frappe Cloud documentation at docs.frappe.io/cloud |
| `GLP` | `docs/GO_LIVE_PLAN.md`: Astral's move from Navari to the suite under the old name |
| `ws-docs:` / `export:` | The workspace folders `..\docs\` and `..\astral_export\`, outside this repo |

The scratchpad folder is temporary. `do_rename.py` is now kept in the branch as `scripts/do_rename.py`, and branch:docs/RENAME_SPIKE.md summarises the evidence (item R7). The logs themselves are still only in the scratchpad: copy them somewhere permanent if they are to be kept.

---

## 1. Summary

**What changes**

- The Python package `frappe_mpsa_payments` becomes `mpsa_suite`.
- The app title "Astral M-Pesa Suite" becomes "Mpsa Suite".
- The module "Frappe Mpsa Payments" becomes "Mpsa Suite".
- Every code path, scheduled job, asset URL (`/assets/mpsa_suite/...`) and newly built callback URL changes with the package name.

**What stays the same**

- Every DocType name, so the same database tables and the same records.
- The Mpesa Settings secrets, Payment Gateway names, roles, the site URL and the `/pay/<token>` page.
- The URLs Safaricom already holds. The old `/api/method/frappe_mpsa_payments...` paths keep working: a lookup table in the new app sends each old path to the new code (`override_whitelisted_methods`, branch:mpsa_suite/hooks.py:317-388).

**How it is done.** Frappe has no rename command. Frappe Cloud identifies an app by its folder name (press: api/github.py, `_get_app_name_and_title_from_hooks`). So the rename is a hand-over between two apps on each site:

1. Install mpsa_suite next to the old app.
2. Uninstall the old app.

On its own, step 2 deletes the app's tables (v15:installer.py:417-420,512-517). A guard in mpsa_suite runs first and moves everything to the new module. In the tests, the uninstall then deleted only one empty module record, on v16 (spike:s4.log:3-5) and on v15 (spike15:f5.log:3-9).

**Risk.** High by nature, because Frappe's Uninstall is built to delete data. A dropped table can only be brought back from a backup (v15:installer.py:403-407,512-517). With the guard, the order and the stop checks in this plan, the risk is medium.

- **Callbacks Safaricom holds:** low risk. The old paths were tested over HTTP after the old app was gone (spike:s5.log:4-15). On v15 all 18 Safaricom paths returned 200 (spike15:f6.log:85).
- **The few minutes of each switch:** medium risk (section 6.4).

**Is it proven?** Partly.

- **Proven on v16:** on a throwaway site with M-Pesa records and site-made scripts, reports and fields, nothing was lost, and the old Safaricom paths still reached the new code after the old app was removed (section 3.4).
- **Proven on v15 (15.123.1),** with the real `mpsa-suite` branch and R1 to R9, on throwaway sites with the same kinds of records: nothing was lost, the guards R2 and R3 refused the wrong uninstalls, and the old paths reached the new code after the old app was removed. The test also found that install-app failed over a stale module map on v15; `before_install` now fixes that (sections 3.2 D and 7.1).
- **Not proven:**
  - The Frappe Cloud dashboard flow, and the Trident pilot (section 5).
  - A Navari-era site such as Astral. The v15 test started from a fresh site with the old app installed at 02d94a0, not from Navari 15.3.1.
  - Live traffic during the switch.
  - R8 by a real restore. It was shown only with mpsa_suite put back in installed_apps by hand (section 7.1).
  - Pausing the scheduler from the Frappe Cloud dashboard. The Desk has no control for it, so this plan relies on the Site Config key `pause_scheduler`, which Support must confirm first (section 9.1 #4). On a v15 bench the key works (spike15:f7.log:46-56).

**Recommendation.** Go ahead in stages, and stop at any failed check:

1. **The release.** Release **C** is `ee5b201` on `astral-suite`, pushed, CI green (run 39842536376). It is a68d401 (each wallet of a shortcode on its own statement) plus R3 (02d94a0), the R3 flag fix (088608d) and a CI fix (ee5b201). `mpsa-suite` was generated from 02d94a0 with the rename script, R1, R2 and R4 to R9 were added, and C was merged in; R2 replaces R3 there (section 4.1). **Still to do:** cut a release branch at `ee5b201` (GLP D3 suggests `astral-suite-golive`), push it, protect it on GitHub so that nothing lands on it after C, and point Trident (T3) and Astral's Stage A at it. Do not deploy `astral-suite` itself: later commits land there (GLP D3). A bench-group Deploy takes the branch head (section 5).
2. **Test on v15.** Done: the spike passed on throwaway v15 sites with the branch and R1 to R9 (section 7.1). Repeat it if the release branch's code changes before T3, or the `mpsa-suite` tip before T4 (section 5). Docs-only commits on `astral-suite`, such as this plan, do not count.
3. **Pilot on Trident.** Trident's Stage A (T1 to T3, done as GLP section 0, P1 to P7) passes before Astral Stage A. Trident's rename (T4 to T19) passes before Astral Stage B (section 6.1). Trident has no M-Pesa money (section 5).
4. **Astral Stage A.** Move Astral from Navari to the suite under the **old** name, as GLP describes, and let it settle. Do not combine this with the rename: installing mpsa_suite straight onto Navari's state would mark all 8 suite patches as done without running them (section 4.4).
5. **Rehearse Astral Stage B** on a fresh copy of Astral (section 7.2).
6. **Astral Stage B.** Do the rename hand-over on Astral (section 6).

**A note on Safaricom.** The rename does not change whether Safaricom accepts the app's URLs. Safaricom rejects callback URLs that contain "mpesa" or "M-Pesa" in any form (ws-docs:daraja_api_reference.md:24). Today's package already passes because it is spelled "mpsa", and the URLs Safaricom holds stay in use through the lookup table. New URLs will still contain `m_pesa_api`, for example `mpsa_suite.mpsa_suite.api.m_pesa_api.confirmation`. Safaricom accepted that segment in Astral's live URL (ws-docs:daraja_api_reference.md:24). Renaming that module as well was a separate decision; the user chose to keep the `m_pesa` path names (section 9.3).

---

## 2. What the rename touches

### 2.1 Code

| What | Count and place | Source |
|---|---|---|
| Mentions of `frappe_mpsa_payments` in Python | 459 in 93 .py files: 145 in 37 runtime files, 166 in 26 bench tests, 148 in 30 unit-test files | git grep at f427f2b, read-only (research) |
| Absolute imports | 189 lines. There is no `import frappe_mpsa_payments`. | same |
| Relative imports that name the inner folder | 9: gateway/intents.py:565, money_in/stk_poll.py:82, money_in/stk_verify.py:267, money_out/common.py:21, sandbox_lab/scenarios.py:156,385,504, utils/utils.py:184,298 | same |
| Dotted-path strings (jobs, enqueue targets) | 232 in .py files, 30 of them in runtime code outside hooks.py | same |
| hooks.py | 25 active lines, plus the fixtures module filter at repo:frappe_mpsa_payments/hooks.py:20 | repo:frappe_mpsa_payments/hooks.py:1,20,29-30,41,113,169-214,296 |
| JS `frappe.call` method strings | 32 in 15 files | research |
| JSON files with module "Frappe Mpsa Payments" | 35: 22 DocTypes, 4 Reports, 4 Dashboard Charts, 1 Dashboard, 2 Number Cards, 1 Workspace, 1 Workspace Sidebar. The work uncommitted then, now a68d401, adds 4 DocTypes, so C ships 26. | research; repo `git diff`; `git ls-tree ee5b201` |
| JSON files with app `frappe_mpsa_payments` | 3: workspace/m_pesa, workspace_sidebar/m_pesa, desktop_icon/m_pesa | research |
| Literal "Frappe Mpsa Payments" in .py | 10: migrate.py:4,335,344, patches/payment_entry_custom_fields.py:17,25,35,41,48,56, hooks.py:20. Also modules.txt:1. | research |
| Package name | repo:pyproject.toml:2 | |
| Folders | The inner folder becomes `mpsa_suite/mpsa_suite/`. The dashboard folder becomes `mpsa_suite_dashboard/`, because Frappe looks for `<module>_dashboard` (v15:utils/dashboard.py:97-104). | |
| Whitelisted functions | 70 module-level functions, 29 of them guest. Also 17 DocType class methods, which are called through their document rather than by dotted path. | repo grep, 2026-10-22 |
| Scheduled jobs | 9: the 8 at f427f2b plus `wallet.balances.run` | repo:frappe_mpsa_payments/hooks.py:194-214 |
| CI, tooling and docs | .github/workflows/ci.yml:3-9 (push triggers: develop, astral-suite and version-* only, so a push to `mpsa-suite` ran no CI until R9 added it: branch:.github/workflows/ci.yml:3-10) and :94-109; .github/workflows/linter.yml:4-6 (pull requests only); .pre-commit-config.yaml, README.md, docs/PAYMENTS_API.md:6, docs/SANDBOX_TESTING.md:11, docs/LICENSING.md (109 path mentions), SUITE_CHANGES.md, GLP | research |

The rename script makes all of these code changes in one pass. In the v16 spike it copied 340 files, rewrote 162 and made 732 replacements (spike:do_rename.out:1-2). The branch run is in section 4.1.

### 2.2 Data on each site

| Record | Where the old name sits | What the hand-over does | Source |
|---|---|---|---|
| Module Def | "Frappe Mpsa Payments", owned by `frappe_mpsa_payments`. Sites that came from Navari also have "Stanbic". | Uninstall deletes everything in modules the old app owns. The guard empties those modules first. | v15:installer.py:417-420; v16:installer.py:465,924-932 |
| DocType.module | 19 to 27 DocTypes, including the shared Mpesa Settings and Payment Gateway | Installing mpsa_suite re-imports them into "Mpsa Suite". The tables are untouched. | spike:p2.json (`doctype_count_by_module`, `mpesa_settings_module`, `payment_gateway_module`) |
| Custom Field, Property Setter | 43 to 45 fields and 2 setters | mpsa_suite's after_install re-tags the 44 fields C's code defines and (R5) creates or re-tags the 6 Payment Entry fields. R1 moves the rest at install; the guard catches anything left at uninstall. On v15, 51 fields were in "Mpsa Suite" after install: the 45 from the old module (R1 moved the 1 site-made one) and R5's 6 new ones. | spike:p1.json, p2.json, p3.json (`custom_field_by_module`); spike15:f3.log:12,21,45 |
| Report, Workspace, Dashboard, Dashboard Chart, Number Card | 4, 1, 1, 4 and 2 shipped records, plus any reports staff made | Frappe Cloud's install re-imports the shipped ones. The guard moves the site-made ones. | spike:p2.json, p3.json (`module_linked_counts`) |
| Server Scripts, Client Scripts and custom DocTypes made on the site | Unknown on Astral. The export has no module field. | The guard moves them. | spike:s2b_data.log:7-11, s4.log:3 |
| Scheduled Job Type.method | 9 jobs | Install adds `mpsa_suite.*` jobs. On v16 both sets exist until the next migrate. On v15 the new rows replaced the old at install: 0 old, 9 new. | spike:p2.json, p4.json; v15:core/doctype/scheduled_job_type/scheduled_job_type.py:52-53,142-151; spike15:f3.log:30 |
| Patch Log | Old names stay as harmless leftovers. | Install records the new names as done without running them. | spike:p4.json (`patch_log`); v15:installer.py:324-325 |
| installed_apps | Lists `frappe_mpsa_payments` | The uninstall removes it. | v15:installer.py:422-424 |
| User.default_app, System Settings.default_app | Possibly `frappe_mpsa_payments` (set from the apps screen) | R6: `rehome()` moves both to `mpsa_suite`; on v15 it moved both at install. A missing app only changes the landing page. | v15:apps.py:98-106; branch:mpsa_suite/handover.py:27-34,81-99; spike15:f3.log:44 |
| v16 Workspace, Workspace Sidebar, Desktop Icon `.app` | `frappe_mpsa_payments` | The guard re-points them. Not exercised in either spike. | branch:mpsa_suite/handover.py:27-30,81-88 |
| Queued Integration Requests ("Mpesa Transaction Status") | Old result URLs stored in `data` | A replayed request goes to the old path, which the lookup table covers. | repo:.../doctype/mpesa_settings/mpesa_settings.py:513-519 (research) |
| Background job queue | Jobs named `frappe_mpsa_payments.*` | These fail once the old app is uninstalled, so drain the queue first. | research: v15:utils/background_jobs.py:193-208 |

These names do not contain the app name and need nothing:

- Integration Request service names ("Mpesa", "Mpesa C2B Verification" and so on).
- Role names and Custom DocPerm.
- The cache prefix `mpsa_suite:`.
- Payment Gateway names `Mpesa-<settings>`.
- The webhook User-Agent (repo:frappe_mpsa_payments/gateway/webhooks.py:90).

### 2.3 Safaricom-facing URLs (18 guest paths)

Path prefixes:

- **A** = `frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api`
- **S** = `frappe_mpsa_payments.frappe_mpsa_payments.doctype.mpesa_settings.mpesa_settings`
- **M** = `frappe_mpsa_payments.money_out.api`

The "Defined at" lines are at f427f2b and come from the research.

| # | Old path | Used for | How long Safaricom keeps calling it | Defined at | Entry in new hooks | HTTP-tested |
|---|---|---|---|---|---|---|
| 1 | A.confirmation | C2B confirmation | **Registered** for shortcodes 208911 and 4134019, until re-registered | m_pesa_api.py:618 | :313 | Yes, through /api/method and /api/v2/method (spike:s5.log:7-12) |
| 2 | A.validation | C2B validation, which must answer within about 8 s | **Registered** | :714 | :318 | Yes (spike:s5.log:4-6) |
| 3 | A.stk_push_callback | STK result | While a prompt is open | :507 | :317 | Yes (spike:s5.log:13-15,38) |
| 4 | `...frappe_mpsa_payments.api.b2c.b2c_results_callback` | B2C result. Navari adds `?provider=Mpesa`, which is not part of the lookup. | While a payout is open | b2c.py:21 | :309 | v15 only (spike15:f6.log:6) |
| 5 | A.handle_queue_timeout | Timeout URL for B2C, balance, status and C2B verification | While a request is open | :981 | :314 | v15 only (spike15:f6.log:11) |
| 6 | A.handle_transaction_status_result | Transaction Status result, also replayed from queued Integration Requests | While a request is open | :765 | :315 | v15 only (spike15:f6.log:12) |
| 7 | A.c2b_verification_result | C2B verification | While a request is open | :656 | :312 | v15 only (spike15:f6.log:9) |
| 8 | A.account_balance_result | Account balance | While a request is open | :62 | :310 | v15 only (spike15:f6.log:7) |
| 9 | A.balance_query_callback | Navari's balance result; Astral's B2C Payout To Bank script would send it again if its BALANCE_REFRESH were set back to 1 (section 2.5) | While a request is open | :69 | :311 | v15 only (spike15:f6.log:8) |
| 10 | A.pull_registration_result | Pull Transactions callback | **Registered** where Pull is registered | :76 | :316 | v15 only (spike15:f6.log:13) |
| 11 | S.pull_transaction_callback | Older Pull callback | **Registered** for shortcodes registered with it | mpesa_settings.py:908 | :320 | v15 only (spike15:f6.log:17) |
| 12 | S.process_balance_info | Upstream balance result | While a request is open | mpesa_settings.py:365 | :319 | v15 only (spike15:f6.log:16) |
| 13-18 | M.b2b_result, M.b2b_queue_timeout, M.b2c_status_result, M.status_queue_timeout, M.reversal_result, M.reversal_queue_timeout | B2B, B2C status and reversals | While a request is open | money_out/api.py:65-95 | :330-335 | v15 only (spike15:f6.log:27-32) |

In the v15 test every one of the 18 old paths returned 200 after the old app was gone, with payloads that match a record (spike15:f6.log:6-32,85). An STK or B2C result with an unknown ID gets 417, and `process_balance_info` with an unknown ConversationID 403, on the old and the new path alike; that is not caused by the rename (branch:docs/RENAME_SPIKE.md:162-165). The "Entry in new hooks" column uses the spike copy's line numbers; add 9 for the branch.

A registered production URL can only be changed by deleting it in the Daraja portal (URL Management, two operators) and registering again (ws-docs:daraja_api_reference.md:102; ws-docs:DARAJA_VERIFIED_2026-10-19.md:27,54). So the lookup entries must stay for as long as Safaricom holds the old URLs. Navari's POS `verify_transaction` path already has no handler in the suite, and Astral sent no POS prompts, so it needs none (research).

**Other outside callers (11 guest paths)**

- **Payments API, 7 paths:** `gateway.v1.create_prompt`, `create_payment_link`, `get_status`, `resend_prompt`, `cancel`, `verify_receipt` and `list_payments`. Their documented base URL is repo:docs/PAYMENTS_API.md:6. Request signing does not cover the URL path, so clients do not notice the lookup.
- **Pay page, 2 paths:** `page_prompt` and `page_status`. Pages already open in a browser call the old base path (repo:frappe_mpsa_payments/www/pay.html:81).
- **Other, 2 paths:** `sandbox_lab.api.ping` and `utils.utils.convert_amount_to_kes`.

All 11 are mapped at new:mpsa_suite/hooks.py:321-329,336-337. The 41 logged-in paths are mapped at :338-378, for browser tabs left open across the switch and for any outside system that uses API keys. In the v15 test, with the old app gone, the 7 Payments API paths answered with the gateway's own 401 without credentials, the 2 pay page paths with its own 404 for an unknown token, ping and `convert_amount_to_kes` with 200, and the 41 logged-in paths with 403 to a guest (spike15:f6.log:18-26,33-75,85).

### 2.4 Frappe Cloud

- **How Frappe Cloud names an app.** The name is the first top-level folder that holds `hooks.py`, `patches.txt` and an `app_title`. The repo must have a `pyproject.toml` at its root. So a branch whose only such folder is `mpsa_suite/` is detected as a **new** app (press: api/github.py, `app()` and `_get_app_name_and_title_from_hooks`).
- **Never leave a `frappe_mpsa_payments/` app folder on the new branch.** It sorts first, so Frappe Cloud would offer "Update App" and replace the live app (same source).
- **Two apps from one repo.** Two apps from the same repo on different branches can share a bench group, as long as their names differ. The code allows it (press: release_group.py `validate_duplicate_app`; app_source.py `validate_source_signature`), but this is **not documented**. Prove it on Trident first.
- **Install App** only lists apps on the bench the site is on now (press: api/site.py `available_apps`). It runs `bench --site <site> install-app <app> --force`, with no backup, no maintenance mode and no scheduler pause (agent: site.py `install_app`).
- **Uninstall** has no "keep data" option. The dialog offers "Create an offsite backup" (ticked by default) and then runs `uninstall-app <app> --no-backup --yes --force` (press: dashboard/src/components/site/UninstallAppDialog.vue; agent: site.py `uninstall_app`).
- **Removing an app from a bench group** is allowed even while sites still have it installed. After that, those sites cannot update or uninstall it, and only Frappe Cloud Support can clear the stale entry (press: release_group.py `remove_app`, site_update.py `validate_apps`; discuss.frappe.io thread 162779).
- **Restores.** A restore removes from `installed_apps` every app that is in the backup but not in Frappe Cloud's current app list for the site. It then migrates and turns the scheduler on (press: agent.py `restore_site`; agent: site.py `restore_job`). Section 6.6 explains what this means for rollback.
- **No dashboard rename.** "Change Branch" and "Update App" keep the old app's identity, and site moves rename nothing (press: release_group.py `change_app_branch`; fc-docs: site-migrations/move-site-to-different-server-or-bench).

### 2.5 Astral's exported scripts

- **AIL Mpesa Feed - B2C Payout To Bank** runs every 5 minutes (export:server_scripts/AIL_Mpesa_Feed_B2C_Payout_To_Bank.py:2). It can send Account Balance queries whose ResultURL and QueueTimeOutURL are `.../frappe_mpsa_payments.frappe_mpsa_payments.api.m_pesa_api.balance_query_callback` (:149). Those queries are off: BALANCE_REFRESH = 0 since 2026-08-05 (:32-34,146). When on, it sends one when the last balance is over 15 minutes old (:145). The lookup covers this path (new:mpsa_suite/hooks.py:311). After the hand-over, either retire the script as GLP allows, or change both URLs in the script to `/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.account_balance_result`. Do **not** use `mpsa_suite...balance_query_callback`: "query" is one of the words Daraja bans in callback URLs (ws-docs:daraja_api_reference.md:24). The suite already ships `account_balance_result` with the same handler, and keeps `balance_query_callback` only so results for requests already sent still land (repo:frappe_mpsa_payments/frappe_mpsa_payments/api/m_pesa_api.py:55-71). If result-token enforcement is on in M-Pesa Suite Settings, add the `?t=<callback token>` parameter (repo:frappe_mpsa_payments/daraja/callbacks.py:27,79-80).
- **AIL_Mpesa_Feed_PE_Cost_Center_Autofill.py:9** mentions the old path only in a comment.
- **None of the 16 Client Scripts** mention the app (research).
- **Other API paths in the scripts**, such as `ail_mpesa_pull_callback` and `ail_mpesa_balance_result`, belong to Server Scripts and are not affected.
- **Unknown:** whether any of the 57 Server Scripts or 16 Client Scripts has module "Frappe Mpsa Payments". The export has no module field (export:astral_config_snapshot.md:8). Check it with A3 in Appendix A. The guard moves any it finds.

---

## 3. The hand-over, step by step

### 3.1 Why two apps

Frappe has no rename command. A Frappe Cloud app is known by its folder name, and changing a branch keeps that name. If the existing app were pointed at a branch whose package is `mpsa_suite`, it would be cloned into `apps/frappe_mpsa_payments`, and every import would fail (press: release_group.py `change_app_branch`). Installing a second app and then removing the first is the only route the dashboard offers.

### 3.2 The steps

**A. Precondition: the site runs the final old-name release C and has run all of C's patches.**

- *What Frappe does:* installing an app records every patch in its `patches.txt` as done without running it (v15:installer.py:324-325; v16:installer.py:371-372).
- *What the test showed:* after install, 8 `mpsa_suite.*` Patch Log rows existed, and none of those patches had run (spike:p2.json `patch_log`).

**B. Put both apps on the bench.**

- *What Frappe does:* uninstalling needs the old app's code, because Frappe loads its hooks first and fails if the package is missing (v15:installer.py:377; v15:__init__.py:1597-1613). Migrate also fails for a site that still lists an app the bench no longer has (v15:migrate.py:113-115).
- *What the test showed:* both apps were in apps.txt during the test (spike:s3.log:5-10).

**C. Pause the scheduler and wait for the background job queue to empty.**

- *How, on Frappe Cloud:* the Site Config key `pause_scheduler: 1` (section 5, T7). The scheduler skips the site while it is set (v15:utils/scheduler.py:138-141). On a v15 bench, `pause_scheduler: 1` in site_config made the scheduler inactive, and the RQ Job list's "Enable Scheduler" call made it active again and set the key to 0 (spike15:f7.log:46-56). Frappe Cloud's Site Config tab is not tested. System Settings' "Enable Scheduled Jobs" box cannot be used, because it is hidden (v15:core/doctype/system_settings/system_settings.json:222-225; v16:core/doctype/system_settings/system_settings.json:231-234).

- *What Frappe does:* while both apps are installed, both sets of scheduled jobs and both sets of document hooks run (v15:__init__.py:1580-1594; v15:model/document.py:1367-1374). Old-named jobs still in the queue fail after the uninstall (research).
- *What the test showed:* 9 old and 9 new jobs existed side by side on v16 (spike:p2.json `scheduled_jobs_count`). On v15 the install replaced the 9 old jobs with 9 new ones (spike15:f3.log:30).

**D. Install mpsa_suite. This is Frappe Cloud's "Install App".**

- *What Frappe does:*
  1. Runs mpsa_suite's `before_install` (v15:installer.py:307-310), which rebuilds a stale module map (below).
  2. Creates the module "Mpsa Suite" (v15:installer.py:316).
  3. Re-imports every DocType definition into the new module. It replaces the DocType's definition, never its table (v15:installer.py:318; v15:modules/import_file.py:146-159,204-243; v15:model/delete_doc.py:79-101).
  4. Records all patches as done (v15:installer.py:324-325).
  5. Runs after_install, which re-creates the suite's custom fields in "Mpsa Suite", including R5's 6 Payment Entry STK fields (branch:mpsa_suite/install.py:110-115; branch:mpsa_suite/mpsa_suite/migrate.py:4,24).
  6. Syncs the scheduled jobs (v15:installer.py:333).
  7. Runs `after_sync` last, where R1 files everything still in the old module under "Mpsa Suite" (v15:installer.py:338-339; branch:mpsa_suite/handover.py:105-109).
  8. From this moment, every old path resolves to the new code, because Frappe applies the lookup table before it imports anything (v15:handler.py:65-75; v15:__init__.py:2553-2556).
- *Frappe 15 and a stale module map:* Frappe loads its map of apps to modules once per process, from the site's Redis cache key `app_modules` (v15:__init__.py:269,1667-1695). `bench get-app` adds the app to apps.txt but does not clear that key. On v15, install-app uses the map as it found it. With a map cached before mpsa_suite was added, it syncs no DocType of mpsa_suite, still commits the Module Def "Mpsa Suite", mpsa_suite in installed_apps and 8 Patch Log rows, and after_install then fails with "Module Mpsa Suite not found" (fix15:a_before_fix.log:3,8,40; journal: v15 stage). Nothing is lost, but the site is left half-installed. v16 rebuilds the map itself inside install-app (v16:installer.py:348-350). mpsa_suite's `before_install` does the same on both versions when the map lacks mpsa_suite (branch:mpsa_suite/install.py:95-107). So install-app needs **no clear-cache first**, on a self-hosted bench (tested) or on Frappe Cloud (derived from the code, because Install App runs install-app: section 2.4; not tested on Frappe Cloud). On a self-hosted bench, a `bench --site <site> clear-cache` before install-app is harmless and can still be added.
- *What the test showed:*
  - On v16 (first spike, renamed copy without R1 to R9):
    - 26 shipped DocTypes moved, and their controllers now load from mpsa_suite.
    - 44 of 45 custom fields and both property setters moved.
    - The site-made report, Server Script, Client Script, custom field and custom DocType stayed in the old module.
    - Install took under 7 seconds (spike:s3.log:11-33; p2.json).
  - On v15 (the branch, with R1 to R9): with the cached map set to one without mpsa_suite and no clear-cache, `install-app mpsa_suite --force` succeeded in 9.8 s. R1 moved the site-made DocType, custom field, report, Client Script and Server Script, and R6 both landing pages. Nothing was left in the old module (A2 and A3 empty, A10 no rows), 27 DocTypes were in "Mpsa Suite", and R5 made the 6 Payment Entry fields (spike15:f3.log:5-12,21-45).
  - The fix on its own, on a v15 site with the same stale map: without `before_install` the install failed with "Module Mpsa Suite not found"; with it, the DocTypes synced and the install finished (fix15:a_before_fix.log:2-8,40; fix15:b_after_fix.log:2-12). On v16 an install over a stale map also worked (verify:t16_force.out).
  - In these runs the stale map was made by writing the cache key directly, not by a real `bench get-app` on a warm bench (journal: retest stage).

**E. Check before uninstalling (Appendix A).**

- *Why:* if anything is still in the old module at uninstall time and the guard did not run, it is deleted or dropped.

**F. Uninstall frappe_mpsa_payments. This is Frappe Cloud's "Uninstall".**

- *What Frappe does:* it calls every installed app's `before_app_uninstall` hook. This happens after the old app's own `before_uninstall` and **before** it reads which modules to delete (v15:installer.py:411-417; v16:installer.py:459-465). mpsa_suite's guard then:
  - moves every record still filed under "Frappe Mpsa Payments" to "Mpsa Suite" (branch:mpsa_suite/handover.py:66-102,123);
  - hands any other non-empty module the old app owns, such as "Stanbic", to mpsa_suite (:124-131).

  Frappe then finds only an empty module record and deletes it (v15:installer.py:441-471).
- *What the test showed:* on v16, without R1, the hook moved 5 records: 1 DocType, 1 Custom Field, 1 Server Script, 1 Report and 1 Client Script. The only thing Frappe removed was the empty "Frappe Mpsa Payments" module record (spike:s4.log:2-6). On v15 with R1, `uninstall-app frappe_mpsa_payments --no-backup --yes --force` took 3 s: the hook found nothing left to move, and Frappe removed only the empty module record; no DocType was removed and no table dropped (spike15:f5.log:3-9).

**G. Migrate.**

- *What Frappe does:*
  - It syncs DocTypes first (v15:migrate.py:123) and then runs the **orphan sweep**, on v15 as well as v16 (v15:migrate.py:148; v16:migrate.py:197). v16 also removes other orphaned records (v16:migrate.py:199).
  - The sweep deletes the DocType record of every standard (custom = 0) DocType whose controller import raises ImportError (v15:model/sync.py:153-176). With it, Frappe deletes every Table-type Custom Field that points at that DocType (v15:model/delete_doc.py:96-98). The table is not dropped, but its rows can no longer be reached.
  - For the hand-over, this hits any standard DocType filed under "Mpsa Suite" that mpsa_suite has no folder for, because its import path `mpsa_suite.mpsa_suite.doctype.<x>` fails. That happens if the old app as deployed ships a DocType the `mpsa-suite` branch lacks: R1 or the guard moves it into "Mpsa Suite" (branch:mpsa_suite/handover.py:71-80), and the next migrate deletes it. Check A10 must return no rows before the uninstall and before this migrate. If it returns rows, do not migrate: ship the missing DocType folder in mpsa_suite first. A site update syncs DocTypes before the sweep, so the fix lands in time.
  - On v16 the old scheduled jobs are deleted here. No patch runs.
- *What the test showed:* 0 old jobs and 9 new ones, and 27 DocTypes kept (spike:s4.log:38-55; p4.json). On v15 the migrate after the uninstall printed no orphan line, and every count matched the baseline apart from what the test and Install App added (A4) (spike15:f5.log:43-70). In a deliberate mismatch on v15, A10 listed a standard DocType filed under "Mpsa Suite" that mpsa_suite does not ship; the next migrate printed "Orphaned DocType(s) found" and deleted its DocType record, and its table and row stayed, as read above (spike15:f7.log:57-64).

**H. Resume the scheduler and test the old paths.**

- *What Frappe does:* old paths resolve through the lookup table even with the old package off the site.
- *What the test showed:*
  - Validation, confirmation (through both v1 and v2) and the STK callback all returned 200.
  - An unmapped old path returned 417 "App frappe_mpsa_payments is not installed" (spike:s5.log:4-22).
  - The queued C2B jobs, running under mpsa_suite names, created registers SPKHTTP001 and SPKHTTP002 (spike:s5.log:31-37).
  - On v15: all 18 Safaricom paths returned 200, the two C2B paths also through `/api/v2/method/`, and an unmapped old path 417. The queued jobs ran under the new names and made the C2B registers (spike15:f6.log:6-32,76-77,84-85,106-117,284).

**I. Later: remove frappe_mpsa_payments from the bench group.**

- *Why:* do this only once no site in that bench group has the old app installed (section 2.4).

### 3.3 What the guard does, exactly

`branch:mpsa_suite/handover.py`, called from `after_sync` (R1, branch:mpsa_suite/hooks.py:118), from `before_app_uninstall` (:312) and from `after_migrate` (:303-306):

- **Which fields it checks.** It builds the list from the site's own schema: `DocType.module` plus every Link-to-Module-Def field (handover.py:37-45). Single DocTypes, and fields whose table or column is missing, are skipped (:48-52).
- **`rehome()`.** It uses SQL to change `"Frappe Mpsa Payments"` to `"Mpsa Suite"` in each of those fields, `app = frappe_mpsa_payments` to `mpsa_suite` on Workspace, Workspace Sidebar and Desktop Icon, and (R6) `User.default_app` and System Settings `default_app` (:27-34,66-102). It does nothing until the module "Mpsa Suite" exists (:68-69), and running it twice is safe.
- **`after_sync()` (R1).** Install-app runs it last, so nothing is left in the old module once mpsa_suite is installed (:105-109).
- **`before_app_uninstall(app_name)`.** For `mpsa_suite` it repeats R2's check (:114-120). Otherwise it acts only when the old app is being removed (:121-122). It runs `rehome()`. Then, for every other module the old app still owns that has something in it, it changes the module's owner to mpsa_suite so the module is kept, not deleted (:124-128). It commits and prints a summary line (:129-131).
- **`after_migrate()`.** It runs `rehome()` again. This catches the old app's own after_migrate, which moves Mpesa Settings back to the old module if a migrate happens while both apps are installed (:134-138; repo:frappe_mpsa_payments/frappe_mpsa_payments/migrate.py:18-19,29-33).
- **`ran_under_old_name()` (R8).** Each of the 8 patches calls it first and returns when its old-name twin is in Patch Log with `skipped = 0` (:146-158).
- **In a dry run.** Frappe runs this hook even in a `--dry-run` uninstall, and the hook really commits its changes (v15:installer.py:411-417). A dry run made **after** mpsa_suite is installed therefore shows the result *with* the guard. To see the full danger list, build it with the read-only script in section 7.2 step 4 before installing mpsa_suite. A dry run at that point would be refused by R3: Frappe calls `before_uninstall` in a dry run too and does not tell it about `--dry-run` (v15:installer.py:403-412), so no hook can tell the two apart. In the v15 test R3 did refuse it (spike15:f2.log:3-13).

### 3.4 Spike evidence (v16, throwaway site `rename16.localhost`, since dropped)

The site had Frappe, ERPNext, payments and the old app. It held 2 Mpesa Settings with 4 secret rows, 2 Payment Gateways, 2 C2B registers, 1 STK request, and a B2C disbursement with its reference row and request. To mimic Astral, it also had a site-made report, Server Script and Client Script, a Bank custom field holding a value, and a custom DocType with 1 row (spike:s2b_data.log:3-11; s2c.log:1-6).

| Measure | p1 old app only | p2 both apps | p3 old app uninstalled | p4 after migrate |
|---|---|---|---|---|
| DocTypes in "Frappe Mpsa Payments" / "Mpsa Suite" | 27 / 0 | 1 / 26 | 0 / 27 | 0 / 27 |
| Rows in the M-Pesa tables | 11 | 11 | 11 | 11 |
| Singles values (Suite Settings + Public Key Certificate) | 56 + 9 | 56 + 9 | 56 + 9 | 56 + 9 |
| Secret rows (`__Auth`) | 5 | 5 | 5 | 5 |
| Custom Fields: total / in the old module | 168 / 45 | 168 / 1 | 168 / 0 | 168 / 0 |
| Property Setters in the old module | 2 | 0 | 0 | 0 |
| Module of Mpesa Settings and Payment Gateway | old | Mpsa Suite | Mpsa Suite | Mpsa Suite |
| Site-made report, Server Script, Client Script | old module | old module | Mpsa Suite | Mpsa Suite |
| Scheduled jobs: old / new | 9 / 0 | 9 / 9 | 9 / 9 | 0 / 9 |
| Patch Log: old / new | 8 / 0 | 8 / 8 | 8 / 8 | 8 / 8 |
| Bank field value; custom DocType row | kept | kept | kept | kept |

Source: spike:p1.json to p4.json, keys `doctype_count_by_module`, `table_rows`, `singles_rows`, `auth_rows`, `custom_field_total`, `custom_field_by_module`, `property_setter_by_module`, `scheduled_jobs_count`, `patch_log` and `data`.

**Without the guard,** the same uninstall would have dropped the custom DocType's table and deleted the site-made report, Server Script, Client Script and Bank custom field (spike:p2.json `would_delete_if_old_app_uninstalled`).

**Cleanup.** The test site was dropped, mpsa_suite was removed from the v16 bench, and apps.txt, apps.json, common_site_config.json and test16's site_config.json match their original checksums (spike:s6.log:44-79).

The spike agent disclosed that it ran `git log` and `git status` once, read-only, on the shared repo. It made no other git use and wrote nothing to the repo.

The v15 repeat, with the real branch and R1 to R9, is summarised in section 7.1.

### 3.5 Not tested yet

The v15 test (section 7.1) has since covered most of what this list held. What it showed, and what is still untested:

- **Frappe v15.** Three differences matter, from reading the source:
  - Uninstall does not skip custom modules (v15:installer.py:417, compared with v16:installer.py:924-932). On v15 the uninstall removed only the empty module record (spike15:f5.log:3-9).
  - The new scheduled job rows should replace the old ones at install (v15:core/doctype/scheduled_job_type/scheduled_job_type.py:52-53). They did: 0 old, 9 new (spike15:f3.log:30).
  - v15 **does** have the DocType orphan sweep, and runs it on every migrate (v15:migrate.py:148; v15:model/sync.py:143-176). It keys on ImportError and deletes DocType records (section 3.2 G). Only v16's sweep of other records is new (v16:migrate.py:199; v16:model/sync.py:261). The sweep skips a DocType whose module no app on the bench lists in its modules.txt: Frappe then raises DoesNotExistError, not ImportError (v15:modules/utils.py:298-302), and the sweep ignores that (v15:model/sync.py:164-167). A kept "Stanbic" module is in that case. In the v16 spike the sweep removed nothing. On v15 it removed nothing in the normal hand-over, and in the deliberate-mismatch run it deleted the DocType record as read here and kept the table (spike15:f5.log:43-45; spike15:f7.log:57-64). The "Stanbic" case is still untested.
- **Done on v15:** install without `--force`, where the bumped timestamps on 14 JSON files re-imported the shipped reports, workspace, dashboard, charts and number cards into "Mpsa Suite", and R1 had only the site-made DocType and report left to move (spike15:f8.log:66-71,94-104; spike15:a_p8.json `A3a`); a migrate and HTTP calls while both apps are installed (spike15:f4.log:5-57); a dry-run uninstall, refused by R3 and then run once with its flag set (spike15:f2.log:3-60); HTTP calls to all 18 Safaricom paths (spike15:f6.log:6-32); R1 to R9 (section 7.1); the unit tests, bench tests and CI on the branch (section 4.6).
- **Still untested:**
  - **A Navari-era site:** the "Stanbic" module and Navari's patches and fields.
  - **v16 Desktop Icon and Workspace Sidebar re-pointing.** No such rows existed on either test site.
  - **The hand-over with R1 to R9 on v16.** On v16 the branch passed its bench tests and an install over a stale module map, but the hand-over itself ran only in the first spike, without R1 to R9.
  - **R8 by a real restore** (section 6.6). It was shown only with mpsa_suite put back in installed_apps by hand.
  - **The scheduler pause through Site Config `pause_scheduler`** on Frappe Cloud (section 9.1 #4).

---

## 4. Code changes

### 4.1 The rename script

`do_rename.py` (now kept as branch:scripts/do_rename.py; first used from the scratchpad as `rename\do_rename.py`, usage at lines 1-22) reads the source and writes a renamed copy. It never writes to the source. The line numbers below are from the scratchpad copy; the branch copy has 7 more lines near the top, so later lines are 5 to 7 higher.

- **Folders:** every path part that contains `frappe_mpsa_payments` is renamed (:62-63). This includes the inner module folder and `mpsa_suite_dashboard/`.
- **Text:** "Astral M-Pesa Suite", "Frappe Mpsa Payments" and `frappe_mpsa_payments` are replaced with the new names in code and config (:49,94-99). Markdown changes only with `--rewrite-docs` (:88-93).
- **New files:** it writes `mpsa_suite/handover.py` (:199-299).
- **hooks.py:** it sets the title, adds the after_migrate and `before_app_uninstall` hand-over hooks, and builds `override_whitelisted_methods` from every module-level whitelisted function in the code (:163-196,302-331).
- **Timestamps:** it bumps `modified` in every record JSON that is not a DocType, so a plain migrate also moves those records (:108-146).
- **Report:** it prints any old names left over (:334-394). The spike run left only the `NOTICE` upstream credit (`NOTICE:5`, which is correct) and a comment in hooks.py (spike:do_rename.out:20-22).

How it was used for release C (done 2026-10-22):

1. It was run on a clean checkout of `02d94a0` (R3 on top of a68d401), not on the working tree, with `--modified "2026-10-22 20:35:00.000000"` (branch:docs/RENAME_SPIKE.md:18-32). Release C, `ee5b201`, came after it and differs from 02d94a0 only in R3's flag check and its test (088608d) and one CI line (ee5b201) (`git diff 02d94a0 ee5b201`).
2. R1, R2 and R4 to R9 were added by hand (6d30ad0), then the review fixes (9927c16: `before_install`, the R4 bench test moved to `mpsa_suite/tests/`, the docs).
3. The output was committed as is as the rename commit 0937c31 on the new branch `mpsa-suite`, whose parent is 02d94a0. C was then merged in without changing the branch's files: 770ffc3 merges 088608d keeping the branch side, because R2 replaces R3 there, and 27efca5 merges ee5b201, whose CI line the branch already had. C is an ancestor of `mpsa-suite` (`git merge-base --is-ancestor ee5b201 27efca5`).
4. The output matched what was expected: `override_whitelisted_methods: 70 paths (29 guest)`, 14 bumped JSON files, 346 files copied, 165 rewritten with 739 replacements (build:do_rename.out:1-3,18). Running the same command on 02d94a0 reproduces the rename commit (branch:docs/RENAME_SPIKE.md:31).

If C ever changes again, re-run the script on the new C, or merge the change into `mpsa-suite` as above, and repeat the v15 test (section 7.1).

Note: in the spike copy, Workspace "M-Pesa" already had a future `modified` of 2026-10-22 22:00 (spike:do_rename.out:16). The script keeps the bump at least one second later (:134); on the branch it went to 22:00:01 (build:do_rename.out:16).

### 4.2 Additions the script does not make

All of these are now built: R3 in release C, the others on `mpsa-suite` (6d30ad0; branch:docs/RENAME_SPIKE.md:37-49 lists where each one lives). R1, R2, R3, R5, R6 and R8 passed the v15 test (section 7.1); R3 ran there at 02d94a0, before its flag fix 088608d, which only unit tests cover. R4 passed in the unit and bench tests (4.6). R7 and R9 are in the repo. None has run on Frappe Cloud yet.

| # | Change | Why |
|---|---|---|
| R1 | Call `handover.rehome()` from an `after_sync` hook in mpsa_suite. Frappe runs after_sync last during install (v15:installer.py:338-339). | After install, nothing is left in the old module, so the check before Uninstall can expect **zero**. In the v16 spike, without R1, the site-made records moved only at uninstall time (spike:p2.json); on v15, R1 moved them at install (spike15:f3.log:12). |
| R2 | mpsa_suite's **own** `before_uninstall` hook (branch:mpsa_suite/hooks.py:125; branch:mpsa_suite/uninstall_guard.py:24-43) refuses **every** uninstall of mpsa_suite with `frappe.throw`, whatever the row counts, unless site_config has `mpsa_allow_uninstall` set to 1 or true. Any other value, "0" included, keeps it locked; removing the key is the clean way to lock it again. The same check runs in `handover.before_app_uninstall` for `app_name == "mpsa_suite"` as a second check only (branch:mpsa_suite/handover.py:114-120). On v15 it refused a dry run, and `--force` after `remove-from-installed-apps mpsa_suite` (spike15:f7.log:1-18). | After the hand-over, mpsa_suite owns the M-Pesa module, and an uninstall deletes much more than table rows (v15:installer.py:441-517): (1) it drops every non-single table, the shared Mpesa Settings and Payment Gateway included (:419-420,512-517); (2) it deletes the Singles M-Pesa Suite Settings and Mpesa Public Key Certificate (:459-460), with their `__Auth` secrets such as `callback_token` (v15:model/delete_doc.py:76; v15:utils/password.py:156-158); (3) it deletes the 44 or more Custom Fields and the Property Setters filed under the module (:465,474-480); (4) it deletes every site-made Server Script, Client Script, Report and custom DocType the guard moved in (branch:mpsa_suite/handover.py:66-102). A row count misses most of this, and on Trident, which has no M-Pesa rows, it would protect nothing. **Why `before_uninstall`:** Frappe reads the app's own hooks with `get_hooks(app_name=...)`, so they run even when mpsa_suite is missing from installed_apps (v15:installer.py:377,411-412). `before_app_uninstall` hooks come only from installed apps (v15:installer.py:414; v15:__init__.py:1602,1633-1639), and Frappe Cloud's `--force` skips the "not installed" check (v15:installer.py:380). mpsa_suite can own "Mpsa Suite" without being in installed_apps after a partly failed Install App, because the Module Def and the DocType re-import are committed before `add_to_installed_apps` (v15:installer.py:316-320; v15:model/sync.py:116), or after a restore that strips the app (2.4). |
| R3 | In the **old-name release C** (added in 02d94a0), frappe_mpsa_payments' own `before_uninstall` hook (C:frappe_mpsa_payments/hooks.py:118; C:frappe_mpsa_payments/uninstall_guard.py:16-31) refuses **every** uninstall, whatever the row counts, unless `mpsa_suite` is in `frappe.get_installed_apps()` and owns the Module Def "Mpsa Suite", or site_config has `mpsa_allow_old_uninstall` set to 1 or true. Since 088608d, part of C, only 1 or true unlocks it; any other value, the text "0" included, keeps it locked. Before 088608d any truthy value unlocked it: text such as "0", a non-zero number, or true (02d94a0:frappe_mpsa_payments/uninstall_guard.py:18). (`bench set-config` itself turns "0" and "1" into numbers: v15:installer.py:633-634. A text "0" can still come from a hand edit of site_config.json, or from Frappe Cloud's Site Config tab if it stores the value as text; not checked.) Removing the key is still the clean way to lock it again. It uses a different flag from R2, so that unlocking one never unlocks the other. R3 is not on `mpsa-suite`, where R2 replaces it. On v15 it refused the dry run while mpsa_suite was not installed (spike15:f2.log:3-13). | Protects against clicking Uninstall in the wrong order, which would drop everything. The old app's own hook runs first (v15:installer.py:411-412). The test must be on installed_apps, because that is exactly when mpsa_suite's guard will run (v15:installer.py:414; v15:__init__.py:1602). Like R2, it must not depend on row counts. No hook can tell a dry run from a real one: Frappe calls `before_uninstall` in both and passes no `dry_run` (v15:installer.py:403-412). So R3 also refuses `--dry-run` unless the flag is set; section 7.2 step 4 builds the danger list another way. R3 must ship in Stage A (Trident step T3, Astral go-live). |
| R4 | Add a unit test: every lookup target imports, is whitelisted and has the same guest setting as the old function, and every module-level whitelisted function in C has an entry. | An unmapped path returns 417, and the callback is lost (spike:s5.log:19-22). |
| R5 | Create the 6 Payment Entry STK fields in after_migrate using `MODULE`, instead of only in patch `payment_entry_custom_fields`. | Fresh installs never get them today, because install records that patch as done (repo:.../patches/payment_entry_custom_fields.py:4-56; v15:installer.py:324-325). |
| R6 | Extend `rehome()` to change `User.default_app` and System Settings `default_app` from `frappe_mpsa_payments` to `mpsa_suite`. | Both fields exist on v15 (v15:apps.py:98-106). Low impact: only the landing page changes. |
| R7 | Copy `do_rename.py` into the repo, for example `scripts/do_rename.py`, and keep the spike logs. Built: branch:scripts/do_rename.py, with branch:docs/RENAME_SPIKE.md as the record of the evidence. The logs themselves are not in the repo. | The scratchpad is temporary. |
| R8 | In the hand-over release, make each `mpsa_suite.mpsa_suite.patches.*` patch return at once when its twin `frappe_mpsa_payments.frappe_mpsa_payments.patches.*` is in Patch Log with `skipped = 0`. One shared helper and one line per patch. This changes existing patch files and adds no new patch (4.4 point 4). | Section 4.4 relies on install-app recording the patches as done (v15:installer.py:324-325), because Patch Log is the only gate (v15:modules/patch_handler.py:56,74-76). If mpsa_suite ever reaches installed_apps by a route that skips install-app, for example the restore reconciliation in 6.6 or Support adding it by hand, the next migrate runs all 8 patches again under the new names. `mpesa_settings_api_switches` would then rewrite every Mpesa Settings' API switches from the legacy `api_type` and usage (repo:frappe_mpsa_payments/frappe_mpsa_payments/patches/mpesa_settings_api_switches.py:32-46). That undoes switches set since Stage A and can turn C2B, STK or B2C on or off on live shortcodes. |
| R9 | Add `mpsa-suite` to `on.push.branches` in ci.yml, so that CI runs on every push. Built: branch:.github/workflows/ci.yml:3-10. The linters stay on pull requests only, on both branches (branch:.github/workflows/linter.yml:3-6, as on C), because the code already failed them before the rename: ruff reports 383 findings and 159 unformatted files at 02d94a0 (local ruff 0.17; pre-commit pins 0.8.1; journal: cleanup stage), so a red linter run on every push would hide a failing test run (c5cdf32). | A push to `mpsa-suite` ran no CI before R9. ci.yml ran on pushes to develop, astral-suite and version-* only (new:.github/workflows/ci.yml:3-9, unchanged from repo:.github/workflows/ci.yml:3-9). Without R9, "CI green" in section 5 would mean nothing. CI is green on `mpsa-suite` at 27efca5 (run 39842596370). |

### 4.3 The lookup table (`override_whitelisted_methods`)

- **Where:** branch:mpsa_suite/hooks.py:317-388. 70 entries: 29 guest (:318-346) and 41 logged-in (:347-387). Each maps the old dotted path to the same function in mpsa_suite.
- **Coverage:** the 18 Safaricom paths and 11 other outside paths are listed in section 2.3. They include `balance_query_callback` (:320), which only Astral's server script uses.
- **How Frappe uses it:** only for HTTP calls, through `/api/method/` and `/api/v2/method/`, including `frappe.call` from the browser (v15:handler.py:65-86; v15+v16:api/v2.py:28-51). It does **not** cover:
  - Scheduled Job Type methods;
  - background jobs queued as strings;
  - Number Card or Dashboard Chart method fields;
  - Python code inside Server Scripts.

  Those need the scheduler pause, the queue drain and the migrate in section 3.2.
- **Guest access:** it is checked on the target function (v15:handler.py:82-84). The script copies each function's guest flag, and R4 checks it (branch:tests/unit/test_rename_safeguards.py, against the snapshot of the 70 whitelisted functions at 02d94a0 in branch:tests/unit/rename_whitelist_at_c.json, the same set as at C, which adds no function; the bench test branch:mpsa_suite/tests/test_rename_lookup.py imports every target).
- **How long to keep it:** for as long as Safaricom holds the registered C2B and Pull URLs (paths 1, 2, 10 and 11). The user decided to keep it for good (section 9.3).

### 4.4 Patches: nothing re-runs harmfully

1. **Nothing re-runs under the new names.** Install records every `mpsa_suite.*` patch as done without running it (v15:installer.py:324-325; spike:p2.json `patch_log` 8/8). This rests on install-app alone, because Patch Log is the only thing that stops a patch from running (v15:modules/patch_handler.py:56,74-76). R8 is the guard for any route that puts mpsa_suite in installed_apps without install-app: each mpsa_suite patch returns early when its old-name twin has already run. On v15, with mpsa_suite's Patch Log rows deleted and the app put back in installed_apps by hand, the next migrate ran the 8 patches and each returned at once, leaving the API switches untouched (spike15:f7.log:19-45).
2. **The old Patch Log rows stay** as harmless leftovers (spike:p4.json).
3. **The danger is the opposite: patches that never run.** Before the hand-over, a site must have run every patch in C under the old name. Today:
   - Trident lacks 3: `drop_text_search_index_setters`, `relink_b2c_reference_rows` and `payout_lines_from_upgrade_date`.
   - Astral has only Navari's `sales_invoice_patch` and `mpesa_custom_fields`.

   Check with A6 (research; repo:frappe_mpsa_payments/patches.txt:6-16).
4. **The release used for the hand-over must add no patch** that has not already run under the old name. Install would record it as done without running it. Add new patches only in a later mpsa_suite release; they then run on that release's migrate.
5. **The hand-over fix is a hook, not a patch,** for the same reason.

### 4.5 Asset paths

- `/assets/frappe_mpsa_payments/` appears at repo:frappe_mpsa_payments/hooks.py:30,41 and desktop_icon/m_pesa.json:13. The script changes it to `/assets/mpsa_suite/`.
- Old asset URLs stop working once the old app leaves the bench. Desk picks up the new ones on reload.
- The test sites had no File records under the old asset path (research). Check Astral with A9.
- While both apps are installed, both apps' desk scripts and form scripts load. This is cosmetic, but keep that window short.

### 4.6 CI and tests

- The renamed CI installs and tests `mpsa_suite` (new:.github/workflows/ci.yml:94-109).
- As copied, it would not have run on the `mpsa-suite` branch: the push triggers were develop, astral-suite and version-* only (new:.github/workflows/ci.yml:3-9). R9 added `mpsa-suite` (branch:.github/workflows/ci.yml:3-10). linter.yml stays on pull requests only, as on C (branch:.github/workflows/linter.yml:3-6), because the code already failed the linters before the rename (R9).
- The unit tests that find the package folder by name (repo:tests/unit/test_guard.py:20,98, test_log_error_calls.py:9) are rewritten by the script.
- **Done:**
  - Unit tests on the branch at 9927c16: 1046 passed, on Python 3.13 and 3.10 (verify:unit_win.log:5; verify:unit310.log).
  - Bench tests (`run-tests --app mpsa_suite` on new sites set up as CI does): 292 passed on v15 and 292 on v16, each including the R4 bench test (spike15:f9b.log:9-13; final:t16\tests16.log:322-391).
  - CI green on `mpsa-suite` at 27efca5 (run 39842596370) and on C (run 39842536376): server jobs on Frappe version-15 and version-16, unit jobs on Python 3.10 to 3.14.
- **Optional:** add a CI job that installs C, adds records, installs mpsa_suite, uninstalls the old app and compares counts. No CI run exercises the hand-over today (research).

### 4.7 Docs

The script left 6 Markdown files with old names in the spike (spike:do_rename.out:19) and 5 on the branch (build:do_rename.out:19).

- **Updated in 9927c16:** README.md; docs/PAYMENTS_API.md, which gives the new base URL and says the old one still works; docs/SANDBOX_TESTING.md; docs/LICENSING.md, whose file paths, a legal-review list, now point at the new paths (`git show --stat 9927c16`). The old names left in them are deliberate: the old base URL that still works, and notes on the package's former name.
- **Keep as history:** SUITE_CHANGES.md, which keeps its old headings and adds a "Rename to Mpsa Suite" section (branch:SUITE_CHANGES.md:916-977), and GLP, which stays on `astral-suite`. The branch adds docs/RENAME_SPIKE.md. This plan is not on the branch.

### 4.8 Where the code lives

- Use the same GitHub repo (`syamkerai/frappe-mpesa-suite`) and a new branch whose only app folder is `mpsa_suite/`. Done: `mpsa-suite`, pushed to the suite remote.
- Set `pyproject.toml` name to `mpsa_suite` (branch:pyproject.toml:2, done). Both packages are installed in the same bench environment, so the distribution names must differ (research; repo:pyproject.toml:1-2).
- The repo name never appears in a URL, so renaming the repo is optional.
- If Frappe Cloud refuses two apps from one repo, use a separate repo instead.

---

## 5. Trident pilot (Frappe Cloud, dashboard only)

**Goal.** Prove every dashboard step on a site with no M-Pesa money: Trident runs the suite at about 13626df, on Frappe/ERPNext 15.122.0, with no Mpesa Settings or receipts (GLP:41). Trident is still a live ERP for its other business, so **never restore over Trident's live site**.

**Before Trident (developer)**

- **Done:** release C is `ee5b201` on `astral-suite`, pushed to the suite remote, with R3 included; CI green (run 39842536376).
- **To do:** C must sit on an old-name release branch that is **protected on GitHub**, so no commit lands on it after C. Cut a release branch at `ee5b201` (GLP D3 suggests `astral-suite-golive`), push it, protect it, and point T3 and Astral's Stage A at it. Do not deploy `astral-suite` itself: later commits land there (GLP D3). None of this is done yet: the suite remote has only `astral-suite` and `mpsa-suite` (`git branch -r`). A bench-group Deploy takes the current head of each app's branch, and a newer old app could ship a DocType that `mpsa-suite` lacks (section 3.2 G).
- **Done:** the `mpsa-suite` branch is built from C (section 4.1): generated from 02d94a0, R1, R2 and R4 to R9 added, C merged in, pushed at 27efca5, CI green (run 39842596370).
- **Done, with one condition:** the v15 spike (section 7.1) passed with mpsa_suite code at 9927c16, the same app code as 27efca5 (the later commits change only docs, the linter workflow and one unit test: `git diff --stat 9927c16 27efca5`), and with the old app at 02d94a0, which differs from C only in R3's flag check and a CI line. Repeat it if the old-name branch tip changes before T3, or the `mpsa-suite` tip before T4.
- **Hard prerequisite before T7, and before GLP runbook step 2 on Astral, whichever comes first** (ask before T1, so the pilot does not stop half-way): Frappe Cloud Support has confirmed that the Site Config key `pause_scheduler` is accepted and reaches the site (section 9.1 #4). GLP P2 tries the key on Trident during its Stage A, where nothing depends on it. The Desk cannot pause the scheduler: System Settings' "Enable Scheduled Jobs" box is hidden on v15 and v16 (v15:core/doctype/system_settings/system_settings.json:222-225; v16:core/doctype/system_settings/system_settings.json:231-234). The only Desk control, the RQ Job list's "Enable Scheduler" button, only turns it back on, and only for Administrator (v15:core/doctype/rq_job/rq_job_list.js:26-49; v15:utils/scheduler.py:225-237). If Support refuses, use the Stopped-flag fallback under the table, and prove it on Trident before Astral. On a self-hosted v15 bench the key paused the scheduler and the "Enable Scheduler" call resumed it (spike15:f7.log:46-56); that does not prove the Site Config tab.

| # | Where | Step | Check, or **stop** |
|---|---|---|---|
| T1 | Site → Update → Configure | Turn auto-update **off** (fc-docs: how-to-disable-auto-update-for-your-site). | Auto-update shows off. |
| T2 | Site → Backups | Schedule an on-demand backup and note the time. | Backup is listed. |
| T3 | Bench group → Apps | **Stage A for Trident:** point `frappe_mpsa_payments` at the protected old-name release branch at C (`ee5b201`), Validate, then Deploy. Then Site → Update; migrate runs. | A6 lists **every** line of C's patches.txt, including the 3 that were missing. **Stop** if any is missing. The bench group shows `frappe_mpsa_payments` at commit C, `ee5b201` (if the dashboard shows the commit hash; not checked). |
| T4 | Bench group → Apps → Add App → GitHub | Repo `syamkerai/frappe-mpesa-suite`, branch `mpsa-suite`. | The dialog says **"Add App"**, name `mpsa_suite`, title "Mpsa Suite". **Stop** if it says "Update App" or names `frappe_mpsa_payments`. **Stop** and ask Support if two apps from one repo are refused. |
| T5 | Bench group | Deploy. Then Site → Update to the new deploy. | **Stop** if the Deploy offers an update to `frappe_mpsa_payments`: its branch has moved past C. The site lists only `frappe_mpsa_payments` as installed (A7). |
| T6 | Dev Tools → SQL Playground (read-only) | Run A1 to A10 and save the results as the **baseline**. Save the exact A4 statements too; they are re-run unchanged later. | Saved. A10 returns no rows; any row is an early warning, so ask the developer. If the SQL Playground is not available on the plan, ask Support (section 9.1). |
| T7 | Site → Site Config | Add the key `pause_scheduler` with value `1` and save. The scheduler skips the site while it is set (v15:utils/scheduler.py:138-141). Then open `/app/rq-job` and wait until no job is queued or started. | The RQ Job list shows "Scheduler: Inactive" (v15:core/doctype/rq_job/rq_job_list.js:26-30), and the queue is empty. **Stop** if it still shows "Active". |
| T8 | Site → Apps → Install App | Install `mpsa_suite`. No cache clear is needed first: `before_install` rebuilds a stale module map (section 3.2 D). | The job succeeds. Note how long it took. |
| T9 | SQL Playground | Run A1, A2, A3, A5, A7, A10. | Expected: "Mpsa Suite" is owned by `mpsa_suite`; `mpsa_suite` is in A7; nothing at all is left in the old module, shipped or made on the site (R1); A10 returns no rows. **Stop** (do not uninstall, do not migrate) if any of these fails. |
| T10 | Any PC | `curl -X POST https://<trident-site>/api/method/frappe_mpsa_payments.sandbox_lab.api.ping`, and the same with `mpsa_suite.sandbox_lab.api.ping`. | Both return HTTP 200 with JSON. "Rejected" is fine: it means the lab is off (repo:frappe_mpsa_payments/sandbox_lab/api.py:148-154). |
| T11 | Desk → `/app/rq-job` | Look for jobs named `frappe_mpsa_payments.*`. | None queued. |
| T12 | Site → Apps → frappe_mpsa_payments → Uninstall | Keep "Create an offsite backup" ticked. | If the job log shows output (not verified): the "mpsa_suite hand-over: moved ..." line, then "removing Module Def 'Frappe Mpsa Payments'" only. **Stop and call the developer** at any "removing DocType" or "dropping Table" line. |
| T13 | SQL Playground | Run A1 to A10 again, using the saved A4 statements. | Every count matches the baseline, apart from the rises Install App can cause in A4c (Appendix A, expected results). The DocType totals per module in A4c add up to the baseline total. A10 returns no rows. The "Frappe Mpsa Payments" module is gone. Nothing is in the old module. **Stop** (do not migrate) if an A4 statement fails with "doesn't exist", because a table was dropped, or if A10 returns rows (section 3.2 G). |
| T14 | Site → Actions | Migrate, if the dashboard offers it (not verified); otherwise the next site update does it. Then run A2, A4 and A5. | If the migrate output is shown: no "Orphaned DocType(s) found" line (v15:model/sync.py:172). A2 and A4 are unchanged. Only `mpsa_suite.*` jobs remain. Re-tick "Stopped" on every job that was stopped in the baseline, because the new rows start un-stopped. |
| T15 | Site → Site Config | Remove `pause_scheduler` (or set it to the number 0) and save. A text "0" would keep the site paused, because the scheduler only tests whether the value is truthy (v15:utils/scheduler.py:138). If that fails, log in as Administrator and use `/app/rq-job` → Actions → "Enable Scheduler", which also sets `pause_scheduler` to 0 (v15:core/doctype/rq_job/rq_job_list.js:31-49; v15:utils/scheduler.py:225-237). Then remove the key in the dashboard as well, so that a later config push from Frappe Cloud does not pause the site again (not verified). | The RQ Job list shows "Scheduler: Active". Within an hour, Scheduled Job Log shows `mpsa_suite.*` runs with no new M-Pesa Error Logs. |
| T16 | Any PC | Repeat T10. | Old ping path returns HTTP 200. A 417 "App frappe_mpsa_payments is not installed" means the lookup table is missing: **stop**. |
| T17 | Desk | Open the M-Pesa workspace, M-Pesa Suite Settings, the Mpesa Settings list and the 4 reports. Check the apps screen. | All open. The apps screen shows "Mpsa Suite". |
| T18 | Bench group → Apps | After 7 or more quiet days, remove `frappe_mpsa_payments`, Deploy, then Site → Update. | The update succeeds. |
| T19 | This plan | Write down timings and surprises for Astral. | |

**Fallback if Support refuses `pause_scheduler`.** This pauses only the M-Pesa jobs, which is what the hand-over needs.

1. At T7, open Desk → Scheduled Job Type. Tick "Stopped" on every row whose method starts with `frappe_mpsa_payments.` (A5) and on every row backed by a Server Script, and keep a list of what you ticked. The scheduler only queues rows that are not Stopped (v15:utils/scheduler.py:117).
2. Right after T8, tick "Stopped" on the new `mpsa_suite.*` rows too. Install creates them un-stopped (v15:core/doctype/scheduled_job_type/scheduled_job_type.py:263-279).
3. At T15, untick "Stopped" from the T6 baseline and the list kept in step 1.

Try this on Trident first if it is needed. Do not use it on Astral until it has worked on Trident.

**Recommended restore test (not on the live site):** use "Restore Backup on another Site" with the T12 offsite backup, to learn what Frappe Cloud does when the backup's app list differs from the site's (press: dashboard/src/objects/site/backups.js; section 6.6).

**Trident rollback**

- **Before T12:** fix forward, or leave both apps installed with the scheduler paused while the developer looks. Do not migrate until A10 returns no rows (section 3.2 G). **Never** use "Uninstall mpsa_suite" as a rollback. mpsa_suite now owns the M-Pesa DocTypes, both Singles and their secrets, 44 or more custom fields and any site-made records the guard moved, and uninstalling it would drop or delete them (v15:installer.py:441-517). R2 refuses it on every site, with or without M-Pesa rows, unless site_config has `mpsa_allow_uninstall` set to 1 or true. Never set that flag on Trident.
- **After T12:** fix forward on the `mpsa-suite` branch. A restore loses everything Trident has posted since the backup, so use one only with Frappe Cloud Support, and export and re-enter the newer records as in the three restore steps of section 6.6.

---

## 6. Astral

### 6.1 Two stages, on different days

| Stage | What | Plan |
|---|---|---|
| A | Navari 15.3.1 to the suite at C (`ee5b201`), still named `frappe_mpsa_payments`. All of C's patches run. | GLP (in-place source swap, proven on Trident: GLP:19) |
| B | The rename hand-over from `frappe_mpsa_payments` to `mpsa_suite` | This section |

**Start Stage B only when all of these hold:**

- Stage A has settled: GLP's first-week monitoring is done (GLP section 8).
- A6 lists every line of C's patches.txt.
- The Trident rename pilot (T4 to T19) has passed.
- The Astral rehearsal (section 7.2) has passed.

### 6.2 What differs from Trident

- **Live data:** 3 Mpesa Settings with consumer keys, passkeys and initiator credentials, 4 Payment Gateways and 407 Payment Requests that use them (export:astral_config_snapshot.md:10-17,31-32,41). The restored copy holds 1,584 B2C requests, 983 disbursements, 261 C2B registers and 326 STK requests (counts from the copy, given in this task's brief; not re-queried here). A dropped `tabMpesa Settings` or `tabPayment Gateway` would break all of it. The guard moves both DocTypes regardless of which app synced them last (branch:mpsa_suite/handover.py:39,71-80).
- **Navari leftovers:**
  - Stage A's patch `remove_unused_upstream_features` drops Navari's unused DocTypes and the "Stanbic" module if it is empty (repo:.../patches/remove_unused_upstream_features.py:15-46).
  - If Stanbic is not empty, the guard keeps it for mpsa_suite. Its data and DocType records stay, but its DocTypes do not load, because no app on the bench lists "Stanbic" in modules.txt (v15:modules/utils.py:298-302). For the same reason the orphan sweep skips them (section 3.5).
  - Before the day, A1 should show no "Stanbic" row.
- **Navari's fixture custom fields:** 8 under Navari and 44 after Stage A on the copy (research). They are re-tagged by after_install where the suite's code defines them, and by R1 or the guard otherwise.
- **Site-made records:** "Astral Casual Payee", reports, and Server or Client Scripts may be in the old module. This is unknown until A2 and A3 are run. The guard moves them.
- **Callbacks Safaricom holds:** the C2B URLs for 208911 and 4134019 point at the old confirmation and validation paths (export:astral_config_snapshot.md:17; research). In-flight STK, B2C, status and balance results also use old paths. All of them are in the lookup table (section 2.3).
- **Astral's B2C Payout To Bank script** would send to `balance_query_callback` again if BALANCE_REFRESH is set back to 1 (section 2.5; GLP section 5, S1).
- **Old app's uninstall hooks:** after Stage A, the old app on the bench is the suite at C, whose `before_uninstall` is R3 and whose `after_uninstall` is commented out (C:frappe_mpsa_payments/hooks.py:118-119). This removes the open question about Navari 15.3.1's hooks.

### 6.3 Suite patches that must run in Stage A (under the old name)

| Patch (`frappe_mpsa_payments.frappe_mpsa_payments.patches.*`) | If skipped on Astral | Source |
|---|---|---|
| create_suite_roles | No M-Pesa roles. after_install creates them anyway. | repo:patches.txt:6; new:mpsa_suite/install.py:13-22 |
| c2b_payment_register | Currency is not backfilled on the 261 registers. | repo:patches.txt:10 |
| payment_entry_custom_fields | The 6 Payment Entry STK fields are missing until mpsa_suite is installed; R5 then creates them at install and on every migrate. | repo:patches.txt:11; branch:mpsa_suite/mpsa_suite/migrate.py:24,492 |
| remove_unused_upstream_features | Navari's Stanbic and reconciliation DocTypes stay; Stanbic becomes a leftover module. | repo:patches.txt:12; patch :15-46 |
| mpesa_settings_api_switches | The 3 Mpesa Settings refuse C2B, STK and B2C. | repo:patches.txt:13; patch :3-8 |
| drop_text_search_index_setters | Stale index setters remain. | repo:patches.txt:14 |
| relink_b2c_reference_rows | B2C reference rows are not relinked. | repo:patches.txt:15 |
| payout_lines_from_upgrade_date | The first payout sweep books hundreds of charge entries into past periods. | repo:patches.txt:16; patch :3-12 |

### 6.4 Live callbacks during the switch

- **Each site update with migrate:** Frappe Cloud turns maintenance mode on, so every request, Safaricom callbacks included, gets a 503 until migrate ends (research: agent server.py `update_site_migrate_job`; v15:app.py:228-231). Migrate took 40 to 73 s on the local copy (GLP:8). How long it takes on Frappe Cloud is unknown.
- **Install App:** a few seconds of DocType re-import (under 7 s on v16: spike:s3.log:11,33; 9.8 s on v15: spike15:f3.log:8). Whether a callback that lands mid-import fails is **unknown**.
- **Both apps installed (from Install to Uninstall):** document hooks run twice. That covers 9 handlers on 5 DocTypes, including the C2B register's after_insert (research; repo:frappe_mpsa_payments/hooks.py:169-189). The code has duplicate guards (Bank Transaction `guard_duplicate`, and `match_now` re-checks docstatus: repo:frappe_mpsa_payments/money_in/events.py:32-34), but concurrent double processing is **not tested**. Keep this window under 15 minutes, at a quiet hour, and run the duplicate check in step S15. An option to test in rehearsal is to untick the Money In switches for the window, since every handler checks its switch first (GLP:248).
- **Safaricom does not document re-sending** a failed callback. For C2B, Pull Transactions (48 h) or the Org Portal statement is the recovery (ws-docs:daraja_api_reference.md:27).

### 6.5 Order of work

**Before the day**

| # | Step | Check, or **stop** |
|---|---|---|
| B1 | Stage A settled; A6 complete; Trident rename pilot (T4 to T19) and Astral rehearsal passed, including the scheduler pause and resume exactly as used here. | **Stop** if any one is missing. |
| B2 | Astral's bench group: Add App `mpsa-suite` (the same branch tip as Trident; dialog checks as in T4), then Deploy. Deploying alone does not change the site. | "Add App", name `mpsa_suite`. **Stop** if the Deploy offers an update to `frappe_mpsa_payments`: its branch has moved past C. |
| B3 | Site → Update → Configure: auto-update **off**. | Off. |
| B4 | Run A1 to A10. Save the list of Stopped jobs and of everything in the old module, and the exact A4 statements. | Saved. **Stop** if A1 shows "Stanbic" or an unexpected module owned by the old app, or if A10 returns rows; ask the developer. |
| B5 | Site → Update to the new deploy at a quiet hour. Only the old app is installed, so nothing changes. This causes a 503 window, as in 6.4. | A7 is unchanged. |
| B6 | If there are Payments API clients, tell them the new base URL is `/api/method/mpsa_suite.gateway.v1.<endpoint>` and that the old one keeps working. | |

**On the day (quiet hour, for example early Sunday)**

| # | Step | Check, or **stop** |
|---|---|---|
| S1 | Write down **T0**. Run A1 to A10 with the A4 statements saved at B4. For an exact comparison, add `WHERE creation <= 'T0'` to each business-table line of A4. | Saved. |
| S2 | Take an on-demand backup and **download** it. | Listed and saved locally. |
| S3 | Pause the scheduler as in T7: Site Config `pause_scheduler: 1`, or the Stopped-flag fallback if that is what worked on Trident. Wait until `/app/rq-job` is empty. Astral's own scheduler scripts pause too, because they run as Scheduled Job Types. | "Scheduler: Inactive" (or every listed job Stopped), and the queue is empty. |
| S4 | Install App `mpsa_suite`. **The both-apps window starts.** | Job succeeds. |
| S5 | Run A1, A2, A3, A5, A7, A10. | Same expectations as T9, A10 included. **Stop** (do not uninstall, do not migrate) on any failure. |
| S6 | `curl -X POST` the old ping path. | HTTP 200. |
| S7 | Check `/app/rq-job` for `frappe_mpsa_payments.*` jobs. | None queued; wait if there are. |
| S8 | Uninstall `frappe_mpsa_payments`, offsite backup ticked. **The window ends.** Aim for S4 to S8 within 15 minutes. | Job output as in T12. |
| S9 | Run A1 to A10 with the saved A4 statements. | Counts at or before T0 match; without the T0 filter, no count is lower than at S1. The DocType totals per module in A4c add up to S1's total. A10 returns no rows. No row in the old module. **Hard stop** (section 6.6) if any count in A4 is lower, an A4 statement fails with "doesn't exist", or the DocType total is lower. **Stop** (do not migrate) if A10 returns rows. |
| S10 | Migrate if the dashboard offers it. Run A2, A4 and A5. Re-tick "Stopped" as in B4. | No "Orphaned DocType(s) found" line, if the output is shown. A2 and A4 unchanged. Only `mpsa_suite.*` jobs. |
| S11 | Resume the scheduler as in T15. | "Scheduler: Active"; jobs run. |
| S12 | Watch the next real C2B confirmation on 208911 arrive in Mpesa C2B Payment Register. Send one tiny STK prompt (GLP section 6). | Both land. |
| S13 | Error Log since T0. | No "Failed to get method" and no "App frappe_mpsa_payments is not installed". |
| S14 | Compare the Org Portal statements of 208911, 4134019 and 4139683 for the window with the registers and requests. | Recover anything missing through Pull (within 48 h) or by hand. |
| S15 | Duplicate check for S4 to S8: Bank Transactions with the same reference number, and Payment Entries per register. | None. |

**After the day**

| # | Step |
|---|---|
| L1 | After 7 or more quiet days: remove `frappe_mpsa_payments` from Astral's bench group, Deploy, then Site → Update. This is another 503 window. |
| L2 | Retire the B2C Payout To Bank script (GLP), or change its ResultURL and QueueTimeOutURL to `/api/method/mpsa_suite.mpsa_suite.api.m_pesa_api.account_balance_result`, with `?t=<callback token>` if result-token enforcement is on (section 2.5). Never use `balance_query_callback` for new requests: Daraja bans "query" in callback URLs (ws-docs:daraja_api_reference.md:24). |
| L3 | Re-registering the C2B URLs under the new paths is not required: the user decided to keep the lookup entries for good (section 9.3). |

### 6.6 Rollback for Stage B

**Every restore below loses data, so each one follows these three steps:**

1. **Before restoring:** note the backup's time, **TB**. Export every record created since TB in the M-Pesa DocTypes, Integration Request, Payment Entry and Bank Transaction, as GLP section 7 describes.
2. **Restore.** The restore's own maintenance window also returns 503 to Safaricom, and Safaricom documents no re-send (ws-docs:daraja_api_reference.md:27).
3. **After restoring:** run S14 again for the whole period from TB to the end of the restore: compare the Org Portal statements of 208911, 4134019 and 4139683 with the registers and requests. Recover missing C2B receipts with Pull Transactions within 48 hours of the transaction, and re-enter the other exported records by hand.

**The cases**

- **Before S8 (both apps installed):** fix forward, or hold with the scheduler paused. **Never** "Uninstall mpsa_suite": it would drop every M-Pesa table and delete both Singles, their secrets, the module's Custom Fields and the site-made records the guard moved (v15:installer.py:441-517). R2 refuses it unless site_config has `mpsa_allow_uninstall` set to 1 or true. Never set that flag on Astral.

  Restoring the S2 backup at this point is unclear. Frappe Cloud would list mpsa_suite as installed while the backup does not have it (press: agent.py `restore_site`). Ask Support first.
- **After S8, if the uninstall deleted something:** restore the offsite backup the Uninstall dialog took. That backup has both apps. Frappe Cloud's list for the site no longer has the old app, so the restore removes it from installed_apps without deleting anything (agent: site.py `restore_job`, `uninstall_unavailable_apps`). The site comes back with its tables and mpsa_suite installed. This conclusion is derived from the source, **not tested**; test it with "Restore Backup on another Site" during the Trident pilot. Everything written between that backup and the end of the restore is lost, so apply the three steps above.
- **After S8, if everything is intact but mpsa_suite misbehaves:** fix forward on the `mpsa-suite` branch, or switch the feature off in M-Pesa Suite Settings (GLP:248). Restoring a backup from before S4 leaves neither app registered and no lookup table, so callbacks fail (research). Do it only with Support, inside the window, following the three steps above.
- **Hard triggers (restore, inside the window only):**
  - any A4 count lower than the baseline, a saved A4 statement failing with "doesn't exist", or a lower DocType total per module (A4c);
  - an old Safaricom path returning 417;
  - Mpesa Settings secrets that no longer decrypt, such as token errors on the STK test;
  - more than one duplicated posting.

---

## 7. Rehearsals before the live sites

### 7.1 Repeat the spike on v15 (before Trident): done 2026-10-22

This is the procedure that was followed. The result is at the end of this section; branch:docs/RENAME_SPIKE.md:95-187 has the full record.

**Where.** On `~/mpesa15` with a throwaway site, as was done on v16. The v16 scripts are s2 to s6 plus probe.py and netguard.py in `spike\`. Restore apps.txt and the other bench files afterwards, as s6 did. A separate scratch bench is safer, because the shared bench serves other sites. (Done on the shared bench, with outbound network blocked, the sites' jobs on a private Redis queue no worker reads, and the scheduler paused. The throwaway sites were dropped and the mpsa_suite line removed from apps.txt afterwards: final:cleanup.log:5-19.)

**What to add:**

- R1 to R9.
- An install without `--force`.
- A migrate while both apps are installed.
- HTTP calls while both apps are installed.
- The read-only danger list before installing mpsa_suite (section 7.2 step 4). Also one `uninstall-app frappe_mpsa_payments --dry-run`: first without the flag, to see R3 refuse it, then with `mpsa_allow_old_uninstall: 1` set for that one run, and the flag removed straight after. Since 088608d only 1 or true unlocks R3; "0" keeps it locked, and removing the key is the clean way to lock it again.
- R2 with `--force` while mpsa_suite is missing from installed_apps (use `bench --site X remove-from-installed-apps mpsa_suite` on the throwaway site). It must still refuse, on a site with no M-Pesa rows too.
- A deliberate mismatch: give the old app a standard DocType that mpsa_suite lacks. A10 must list it, and a migrate after the hand-over must show the orphan sweep deleting its DocType record, as read in section 3.2 G.
- The scheduler pause through `pause_scheduler` in site_config, and the resume through the RQ Job list's "Enable Scheduler" button as Administrator.
- R8: put mpsa_suite in installed_apps without install-app, run a migrate, and confirm that no mpsa_suite patch does any work.
- HTTP posts with realistic payloads to all 18 Safaricom paths.
- A Navari-shaped start: Navari 15.3.1, then C under the old name, then the hand-over. **Not done** (see the result).

**Pass:**

- Every p3 count equals p1, as in section 3.4.
- Every old path returns 200.
- On v15, only `mpsa_suite.*` jobs exist after install.
- R2 and R3 refuse the wrong-order uninstalls, whatever the row counts.
- The migrate after the uninstall prints no "Orphaned DocType(s) found" line (v15:model/sync.py:172), except in the deliberate-mismatch run.

**Result (2026-10-22): passed.** Bench `~/mpesa15`, Frappe 15.123.1 / ERPNext 15.123.0, throwaway sites `rename15.localhost` and `rename15b.localhost` (no `--force`), mpsa_suite code at 9927c16 and the old app at 02d94a0, which is C without the flag fix 088608d and the CI line (section 5 says how these compare with the tips). The site held the same kinds of records as the v16 site, and both landing pages were set to the old app.

- **Danger list and R3:** R3 refused the dry run. The read-only list showed 25 tables to drop, 2 Singles, 45 custom fields, 2 property setters, 5 reports, the workspace, dashboard, charts, number cards and both site-made scripts. With the flag set to 1 for one run, the dry run listed the 27 DocTypes and changed nothing; the flag was removed straight after (spike15:f2.log:3-60).
- **Install over a stale module map, no clear-cache:** succeeded in 9.8 s. R1 moved the 5 site-made records and R6 both landing pages; nothing was left in the old module; 27 DocTypes in "Mpsa Suite"; 9 new jobs and no old ones; R5 made the 6 Payment Entry fields (spike15:f3.log:5-45). The fix itself is in section 3.2 D (fix15).
- **Both apps installed:** 21 of 21 old and new path pairs answered the same, and the queued jobs were all `mpsa_suite.*`. A migrate with both apps installed took 38 s, left everything in "Mpsa Suite" and printed no orphan line (spike15:f4.log:5-57).
- **Uninstall of the old app** (`--no-backup --yes --force`): 3 s; only the empty Module Def was removed. The migrate after it printed no orphan line. Rows, Singles, secrets (each still decrypts), the Bank field's value and the custom DocType's row equal the baseline; the only differences are what the test and Install App added (Appendix A, A4; spike15:f5.log:3-70).
- **Old paths with the old app gone:** all 18 Safaricom paths returned 200, all 41 logged-in paths 403 to a guest, and an unmapped path 417; 68 of 70 pairs answered the same, and the 2 that differ are STK and B2C results whose new-path payload carried unknown IDs (spike15:f6.log:6-86). So "every old path returns 200" holds for payloads that match a record.
- **R2:** refused the dry run, and refused `--yes --force --no-backup` after `remove-from-installed-apps mpsa_suite`; nothing changed (spike15:f7.log:1-18).
- **R8:** with mpsa_suite put back in installed_apps without install-app, each of the 8 patches returned at once and the sentinels stayed untouched (spike15:f7.log:19-45).
- **Scheduler:** `pause_scheduler: 1` made it inactive; the "Enable Scheduler" call as Administrator made it active and set the key to 0 (spike15:f7.log:46-56).
- **Deliberate mismatch:** A10 listed the extra DocType, and the next migrate deleted its DocType record and kept its table and row (spike15:f7.log:57-64).
- **Install without `--force`** (`rename15b`): worked, and so did the uninstall and migrate after it (spike15:f8.log:66-104; spike15:a_p8.json).
- **Bench tests:** 292 passed on a fresh v15 site and 292 on v16, each with the R4 bench test (spike15:f9b.log:9-13; final:t16\tests16.log:322-391).

**Not covered:** the Navari-shaped start, Frappe Cloud, R8 through a real restore, v16 Desktop Icon and Workspace Sidebar rows, and the hand-over with R1 to R9 on v16 (only the first v16 spike, without them, ran the hand-over there).

### 7.2 Rehearse on a fresh copy of Astral (before Stage B)

1. **Get a fresh copy.** Download a fresh Astral backup from Frappe Cloud. The existing copy `astral.local` is stale: its data ends on 2026-10-02 and it runs Frappe 15.121.3 (GLP:62). Restore the backup into a **new** site on a bench with Astral's apps at matching versions (export:astral_config_snapshot.md:6). Leave `astral.local` as it is. Adding mpsa_suite changes a bench's apps.txt for every site on it, so a separate bench is better.
2. **Isolate it.** Block outbound network (spike:netguard.py) and clear the secrets as GLP T-11 does.
3. **Stage A:** move from Navari to C and migrate, as GLP's rehearsal 3 describes.
4. **Danger list (read-only):** **before** mpsa_suite is installed, list what an uninstall of the old app would remove, without running one. In `bench --site X console`, for each Module Def with `app_name = 'frappe_mpsa_payments'`, list:
   - its DocTypes with `issingle`, which is what `_delete_modules` deletes or drops (v15:installer.py:441-471,512-517). Every non-single DocType here means DROP TABLE in a real run;
   - for each (doctype, field) pair from `frappe.installer._get_module_linked_doctype_field_map()` (v15:installer.py:483-509), the records filed under that module.

   This list is what the guard must protect. Do not use `uninstall-app --dry-run` for this: R3 refuses it, because no hook can tell a dry run from a real one (v15:installer.py:403-412). If a dry run is wanted as a cross-check, set `mpsa_allow_old_uninstall: 1` on the copy for that one run and remove it straight after. Only 1 or true unlocks R3 (088608d); "0" keeps it locked, and removing the key locks it again.
5. **Baseline:** take probe p1.
6. **Install mpsa_suite:** `bench get-app` from the `mpsa-suite` branch, then `install-app mpsa_suite --force`, which is what Frappe Cloud runs. No `clear-cache` is needed in between: mpsa_suite's `before_install` rebuilds a module map cached before `get-app` (section 3.2 D). A `bench --site <site> clear-cache` first is harmless. Take probe p2.
7. **Test old paths with both apps installed:** HTTP posts to the old paths.
8. **Uninstall the old app:** take a backup first, then run `uninstall-app frappe_mpsa_payments --yes --force --no-backup`, which is what Frappe Cloud runs. Take probe p3.
9. **Migrate.** Take probe p4. Run each M-Pesa job once, with the network still blocked.
10. **Repeat the HTTP posts** to all 18 Safaricom paths, using sample payloads shaped like Astral's.
11. **Time each step,** and record Error Logs and any Custom Field or Property Setter changes.

**Pass:**

- p3 and p4 equal p1 on every M-Pesa table, Singles, `__Auth`, Custom Field, Property Setter, Server Script, Client Script, Report and Payment Request count. Also run Appendix A's A4 and A10 on the copy, to prove those queries give the same answer as the probes.
- Nothing is left in the old module.
- Each old path returns 200 and lands in the right record.

---

## 8. Risks, mitigations and stop conditions

| # | Risk | Mitigation | Stop condition |
|---|---|---|---|
| 1 | The uninstall drops tables or deletes records still in the old module. Drops cannot be rolled back (v15:installer.py:512-517). | Guard (branch:mpsa_suite/handover.py:112-131), R1, checks A1 to A3, offsite backup ticked | Anything shipped is still in the old module after install, or the job log shows "removing DocType" or "dropping Table" |
| 2 | Uninstall is clicked in the wrong order (old app before mpsa_suite), so everything is dropped. | R3 in release C, refusing whatever the row counts unless mpsa_suite is in installed_apps, or `mpsa_allow_old_uninstall` is 1 or true; the step order; T9 and S5 checks | mpsa_suite is not in A7 |
| 3 | mpsa_suite is uninstalled later, or used as a "rollback", so everything is dropped or deleted: tables, Singles, secrets, Custom Fields and site-made records. | R2 in mpsa_suite's own `before_uninstall`, which runs even if the app is missing from installed_apps, refusing every uninstall unless `mpsa_allow_uninstall` is 1 or true; this plan's rollback rules | Never click it, and never set `mpsa_allow_uninstall` on a live site |
| 4 | An old path is not in the lookup table, so it returns 417 and the callback is lost (spike:s5.log:19-22). | Table generated from the code; R4; HTTP tests in 7.1 and 7.2 | Any old Safaricom path returns 417 in rehearsal |
| 5 | Patches never run, because install records them as done. | Stage A first; A6 check; no new patch in the hand-over release | A6 lacks any line of C's patches.txt |
| 6 | Hooks run twice while both apps are installed. | Window of 15 minutes or less at a quiet hour; scheduler paused; S15 duplicate check | Duplicates found in rehearsal |
| 7 | Old-named jobs fail, and Stopped flags are lost. | Scheduler paused; queue drained; migrate; re-tick Stopped from B4 | Old-named jobs queued at S7 |
| 8 | v15 behaves differently from v16. | Section 7.1, done: the v15 test with the branch lost nothing. It found one difference that mattered: v15's install-app failed over a stale module map, which `before_install` now fixes (section 3.2 D). Repeat 7.1 if the release branch's code changes before T3, or the `mpsa-suite` tip before T4. | Any loss in the v15 spike, or in a repeat of it |
| 9 | Frappe Cloud treats the new branch as "Update App" for the old app. | No `frappe_mpsa_payments/` app folder on the branch (2.4) | The dialog shows "Update App" |
| 10 | The two packages collide in the bench environment. | pyproject name `mpsa_suite` (branch:pyproject.toml:2) | Build fails, or the old app fails to import |
| 11 | The old app is removed from the bench group too early. | Only after every site in that group has uninstalled it; wait 7 days | Any site in the group still lists it |
| 12 | Callbacks are refused (503) during migrate windows or lost mid-install. | Quiet hour; statement comparison (S14); Pull | Unrecovered receipt after S14 |
| 13 | A restore rollback misbehaves because the app lists differ. | Rehearse "Restore on another Site"; prefer fixing forward | — |
| 14 | Edits made on the site to the standard M-Pesa reports or workspace are overwritten. Install with `--force` re-imports them, and the bumped timestamps do the same on migrate (research: v15:modules/import_file.py:124-144). | Note any such edits in B4 and redo them after | — |
| 15 | Install with `--force` resets standard DocType permissions to the JSON. Custom DocPerm is untouched (research: v15:installer.py:318). | Check role access in S12 | — |
| 16 | A leftover Stanbic or other non-empty module is kept but its DocTypes do not load. The orphan sweep does not delete them, because no app lists "Stanbic" in modules.txt, so Frappe raises DoesNotExistError rather than ImportError (v15:modules/utils.py:298-302; v15:model/sync.py:164-167). | Stage A patch; A1 check in B4 | A1 shows a non-empty extra module |
| 17 | default_app still points at the old app. | R6; A8 | — |
| 18 | The orphan sweep, on v15 and v16, deletes the DocType record (and its Table-type Custom Fields) of a standard DocType moved into "Mpsa Suite" that mpsa_suite does not ship. The table stays, but its data can no longer be reached (v15:migrate.py:148; v15:model/sync.py:153-176; v15:model/delete_doc.py:96-98). | Old-name release branch protected after C (still to do, section 5); Deploy at T5/B2 must not update the old app; A10 at T6/B4, T9/S5 and T13/S9 | A10 returns rows; a migrate prints "Orphaned DocType(s) found" |
| 19 | The 8 patches run again under the new names, because mpsa_suite reached installed_apps without install-app (restore reconciliation, manual fix). `mpesa_settings_api_switches` would reset live API switches. | R8 | Any mpsa_suite patch does work in the 7.1 R8 test |
| 20 | The scheduler cannot be paused from the dashboard: the System Settings box is hidden. | Site Config `pause_scheduler`, confirmed with Support before T7 and before GLP runbook step 2 (section 9.1 #4); Stopped-flag fallback (section 5), proven on Trident first | Support refuses, and the fallback has not worked on Trident |

---

## 9. Open questions

### 9.1 For Frappe Cloud Support

1. Does Install App still run `install-app --force`, and Uninstall `uninstall-app --no-backup --yes --force`, as the public agent code shows?
2. Can one bench group hold two apps from the same GitHub repo on different branches? The code allows it; it is not documented.
3. Is the app name `mpsa_suite` free on Frappe Cloud?
4. **Hard prerequisite before T7, and before GLP runbook step 2 on Astral, whichever comes first (ask before T1):** is `pause_scheduler` accepted in the Site Config tab, does it reach the site straight away, and does removing it resume the scheduler? T7, T15, S3 and S11 depend on it, and so do GLP runbook steps 2 and 9 and GLP rollback recipe steps 1 and 6. GLP P2 tries it on Trident first. The Desk cannot pause the scheduler: System Settings' "Enable Scheduled Jobs" box is hidden (v15:core/doctype/system_settings/system_settings.json:222-225; v16:core/doctype/system_settings/system_settings.json:231-234). The RQ Job list only offers "Enable Scheduler", for Administrator only (v15:core/doctype/rq_job/rq_job_list.js:26-49; v15:utils/scheduler.py:225-237). If Support refuses, use the Stopped-flag fallback in section 5.
5. Is the SQL Playground (Dev Tools) available on Trident's and Astral's plans?
6. Does the Install or Uninstall job log show the app's printed output?
7. Is there a Migrate action for a site?
8. When a backup's app list differs from the site's app list, what exactly does a restore do? (6.6)
9. Would Support run `remove-from-installed-apps frappe_mpsa_payments` instead of an uninstall, as a fallback? It deletes nothing (research).
10. Does a site update use a "Pull" (no migrate) or a "Migrate" deploy, and can we choose?
11. How long is the maintenance window of a site update?

### 9.2 Site data to query (read-only, Appendix A)

- **Trident and Astral:** which module Mpesa Settings and Payment Gateway are in; which modules the old app owns; Patch Log; Stopped jobs; default_app.
- **Astral:**
  - which DocTypes, reports, Server or Client Scripts, Custom Fields and Property Setters are in the old module, including "Astral Casual Payee";
  - which Pull CallBackURL is registered for each shortcode;
  - whether outside systems call logged-in `frappe_mpsa_payments.*` methods with API keys (for example the WhatsApp claim flow).

### 9.3 Decisions for the user

The user decided all five on 2026-10-22, together with the full rename to `mpsa_suite` ("Mpsa Suite") and Trident first, Astral later.

1. **Two stages for Astral.** Recommended: Stage A on its own day, Stage B later. The alternative, installing mpsa_suite straight onto Navari, would need all 8 patches rewritten as install code and is not recommended. *Decided:* Stage A first, then Stage B; R3 ships in the old-name release C, so Trident's Stage A deploys C.
2. **Rename `api/m_pesa_api.py` and other path segments with "mpesa" or "m_pesa"** (for example `doctype.mpesa_settings`)? Safaricom accepted `m_pesa` in Astral's live URL (ws-docs:daraja_api_reference.md:24). If yes, add the old paths to the lookup table. *Decided:* keep the `m_pesa` path names.
3. **Re-register the C2B URLs** under the new paths later (Daraja portal: delete, then register; two operators), or keep the lookup entries for good? *Decided:* keep the lookup table for good.
4. **Same repo with a new branch** (recommended), or a separate repo? *Decided:* same repo, new branch `mpsa-suite` (built and pushed).
5. **How long to keep the old app** on each bench group after the hand-over. This plan says at least 7 days. *Decided:* 7 days.

### 9.4 Still unknown

- Whether a callback that arrives during the few seconds of Install App fails.
- Whether Safaricom retries STK, B2C, B2B and status callbacks that were rejected with 503.
- Whether all of v15's behaviour matches the source reading (section 3.5). The v15 test confirmed it for the hand-over, the job rows and the orphan sweep; the kept "Stanbic" case is still untested.
- Whether R1 to R9 work on Frappe Cloud. They worked as described on the v15 test (section 7.1); R8 was shown only with mpsa_suite put back in installed_apps by hand, not through a restore.
- Whether the A4 generator query and `__Auth` counts run in Frappe Cloud's SQL Playground. Both ran as plain SQL on the v15 test site, not through the SQL Playground (spike15:f1.log:69-72).

---

## Appendix A. Read-only SQL checks (SQL Playground, Read Only mode)

```sql
-- A1 Modules: who owns what
SELECT name, app_name, custom FROM `tabModule Def`
WHERE app_name IN ('frappe_mpsa_payments','mpsa_suite')
   OR name IN ('Frappe Mpsa Payments','Mpsa Suite','Stanbic');

-- A2 DocTypes per module
SELECT module, name, custom, issingle FROM `tabDocType`
WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite','Stanbic') ORDER BY module, name;
SELECT name, module FROM `tabDocType` WHERE name IN ('Mpesa Settings','Payment Gateway');

-- A3 Records filed under the old and new module
SELECT 'Custom Field' dt, module, COUNT(*) n FROM `tabCustom Field` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Property Setter', module, COUNT(*) FROM `tabProperty Setter` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Report', module, COUNT(*) FROM `tabReport` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Workspace', module, COUNT(*) FROM `tabWorkspace` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Server Script', module, COUNT(*) FROM `tabServer Script` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Client Script', module, COUNT(*) FROM `tabClient Script` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Print Format', module, COUNT(*) FROM `tabPrint Format` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Notification', module, COUNT(*) FROM `tabNotification` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Dashboard', module, COUNT(*) FROM `tabDashboard` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Dashboard Chart', module, COUNT(*) FROM `tabDashboard Chart` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Number Card', module, COUNT(*) FROM `tabNumber Card` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module
UNION ALL SELECT 'Web Form', module, COUNT(*) FROM `tabWeb Form` WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite') GROUP BY module;
SELECT 'Server Script' dt, name FROM `tabServer Script` WHERE module = 'Frappe Mpsa Payments'
UNION ALL SELECT 'Client Script', name FROM `tabClient Script` WHERE module = 'Frappe Mpsa Payments'
UNION ALL SELECT 'Report', name FROM `tabReport` WHERE module = 'Frappe Mpsa Payments' AND is_standard = 'No';

-- A4 Row counts. Save the exact statements used for the baseline and re-run the same text later.
-- A statement that fails with "Table ... doesn't exist" means a table was dropped: hard stop.
-- On Astral, for an exact comparison add  WHERE creation <= 'T0'  to each business-table line.

-- A4a Generator: one COUNT for every non-single DocType in the suite's modules, shipped or made
-- on the site (for example Astral Casual Payee). Run it, copy the single value it returns, run
-- that as the next query, and save that text as the A4a statement for this site.
SELECT GROUP_CONCAT(CONCAT('SELECT ''', name, ''' t, COUNT(*) n FROM `tab', name, '`')
       ORDER BY name SEPARATOR ' UNION ALL ') AS a4a_statement
FROM `tabDocType`
WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite','Stanbic') AND issingle = 0;

-- A4b Fallback if the generator cannot be used: the 24 non-single DocTypes mpsa_suite ships at
-- 27efca5 (branch:mpsa_suite/mpsa_suite/doctype/: 26 DocTypes less the 2 Singles). Child tables
-- (istable = 1) are included, because an uninstall drops their tables too; that is also what the
-- generator's issingle = 0 picks. Checked against the branch: unchanged since the spike copy, and
-- the 4 wallet DocTypes from a68d401 are in. Regenerate if the branch adds a DocType. Add one line
-- per site-made DocType from A2. Delete a line only if A2 shows that DocType does not exist yet.
SELECT 'B2C Disbursement Request' t, COUNT(*) n FROM `tabB2C Disbursement Request`
UNION ALL SELECT 'B2C Payment Disbursement', COUNT(*) FROM `tabB2C Payment Disbursement`
UNION ALL SELECT 'B2C Payment Disbursement Reference', COUNT(*) FROM `tabB2C Payment Disbursement Reference`
UNION ALL SELECT 'Mpesa API Client', COUNT(*) FROM `tabMpesa API Client`
UNION ALL SELECT 'Mpesa B2B Payment Request', COUNT(*) FROM `tabMpesa B2B Payment Request`
UNION ALL SELECT 'Mpesa B2C Status Query', COUNT(*) FROM `tabMpesa B2C Status Query`
UNION ALL SELECT 'Mpesa Balance Snapshot', COUNT(*) FROM `tabMpesa Balance Snapshot`
UNION ALL SELECT 'Mpesa C2B Payment Register', COUNT(*) FROM `tabMpesa C2B Payment Register`
UNION ALL SELECT 'Mpesa C2B Payment Register URL', COUNT(*) FROM `tabMpesa C2B Payment Register URL`
UNION ALL SELECT 'Mpesa Conversion Rate', COUNT(*) FROM `tabMpesa Conversion Rate`
UNION ALL SELECT 'Mpesa Express Request', COUNT(*) FROM `tabMpesa Express Request`
UNION ALL SELECT 'Mpesa Match Log', COUNT(*) FROM `tabMpesa Match Log`
UNION ALL SELECT 'Mpesa Payment Intent', COUNT(*) FROM `tabMpesa Payment Intent`
UNION ALL SELECT 'Mpesa Reconciliation Priority', COUNT(*) FROM `tabMpesa Reconciliation Priority`
UNION ALL SELECT 'Mpesa Reversal Request', COUNT(*) FROM `tabMpesa Reversal Request`
UNION ALL SELECT 'Mpesa Sandbox Test Case', COUNT(*) FROM `tabMpesa Sandbox Test Case`
UNION ALL SELECT 'Mpesa Sandbox Test Run', COUNT(*) FROM `tabMpesa Sandbox Test Run`
UNION ALL SELECT 'Mpesa Settings', COUNT(*) FROM `tabMpesa Settings`
UNION ALL SELECT 'Mpesa Statement Import', COUNT(*) FROM `tabMpesa Statement Import`
UNION ALL SELECT 'Mpesa Statement Row', COUNT(*) FROM `tabMpesa Statement Row`
UNION ALL SELECT 'Mpesa Sub Account', COUNT(*) FROM `tabMpesa Sub Account`
UNION ALL SELECT 'Mpesa Webhook Delivery', COUNT(*) FROM `tabMpesa Webhook Delivery`
UNION ALL SELECT 'Payment Gateway', COUNT(*) FROM `tabPayment Gateway`
UNION ALL SELECT 'Supplier M-Pesa Account', COUNT(*) FROM `tabSupplier M-Pesa Account`;

-- A4c Shared tables, Singles, site-wide totals and DocTypes per module
SELECT 'Payment Gateway Account' t, COUNT(*) n FROM `tabPayment Gateway Account`
UNION ALL SELECT 'Payment Request', COUNT(*) FROM `tabPayment Request`
UNION ALL SELECT 'Integration Request', COUNT(*) FROM `tabIntegration Request`
UNION ALL SELECT 'Custom Field (all)', COUNT(*) FROM `tabCustom Field`
UNION ALL SELECT 'Property Setter (all)', COUNT(*) FROM `tabProperty Setter`
UNION ALL SELECT 'Server Script (all)', COUNT(*) FROM `tabServer Script`
UNION ALL SELECT 'Client Script (all)', COUNT(*) FROM `tabClient Script`
UNION ALL SELECT 'Report (all)', COUNT(*) FROM `tabReport`
UNION ALL SELECT CONCAT('Singles ', doctype), COUNT(*) FROM `tabSingles`
  WHERE doctype IN ('M-Pesa Suite Settings','Mpesa Public Key Certificate') GROUP BY doctype
UNION ALL SELECT CONCAT('DocTypes in ', module), COUNT(*) FROM `tabDocType`
  WHERE module IN ('Frappe Mpsa Payments','Mpsa Suite','Stanbic') GROUP BY module;

-- A4d Secret rows of the three DocTypes with Password fields (branch:mpsa_suite/mpsa_suite/doctype/
-- mpesa_settings, m_pesa_suite_settings, mpesa_api_client). The SQL Playground may refuse __Auth;
-- then note it and ask Support (9.1 #5).
SELECT doctype, COUNT(*) n FROM `__Auth`
WHERE doctype IN ('Mpesa Settings','M-Pesa Suite Settings','Mpesa API Client') GROUP BY doctype;

-- A5 Scheduled jobs
SELECT name, method, stopped FROM `tabScheduled Job Type`
WHERE method LIKE 'frappe\_mpsa\_payments.%' OR method LIKE 'mpsa\_suite.%' ORDER BY method;

-- A6 Patch Log
SELECT patch, skipped, creation FROM `tabPatch Log`
WHERE patch LIKE 'frappe\_mpsa\_payments.%' OR patch LIKE 'mpsa\_suite.%' ORDER BY creation;

-- A7 Installed apps. Frappe 15 and 16 keep installed_apps under parent '__global'
-- (db.get_global / set_global: v15:database/database.py:1133-1139; v16:database/database.py:1136-1142).
-- An earlier draft read parent '__default', which returns no rows (spike15:f1.log:75-76).
SELECT defvalue FROM `tabDefaultValue` WHERE parent = '__global' AND defkey = 'installed_apps';

-- A8 Default app
SELECT name FROM `tabUser` WHERE default_app = 'frappe_mpsa_payments';
SELECT value FROM `tabSingles` WHERE doctype = 'System Settings' AND field = 'default_app';

-- A9 Old paths stored in data
SELECT 'Server Script' t, name FROM `tabServer Script` WHERE script LIKE '%frappe_mpsa_payments%'
UNION ALL SELECT 'Client Script', name FROM `tabClient Script` WHERE script LIKE '%frappe_mpsa_payments%'
UNION ALL SELECT 'Queued Integration Request', name FROM `tabIntegration Request` WHERE status = 'Queued' AND data LIKE '%frappe_mpsa_payments%'
UNION ALL SELECT 'File', name FROM `tabFile` WHERE file_url LIKE '/assets/frappe_mpsa_payments/%';

-- A10 Standard DocTypes in the suite's modules that mpsa_suite does not ship. Must return no rows
-- before the uninstall (T12/S8) and before any migrate after Install App (section 3.2 G).
-- The list is every DocType mpsa_suite ships: 26 at 27efca5 (branch:mpsa_suite/mpsa_suite/doctype/),
-- Singles and child tables included, the same 26 as in the spike copy. In the v15 test the branch's
-- list equalled this one (spike15:f1.log:82). Regenerate it if the branch adds a DocType.
SELECT name, module FROM `tabDocType`
WHERE custom = 0 AND module IN ('Frappe Mpsa Payments','Mpsa Suite')
  AND name NOT IN ('B2C Disbursement Request','B2C Payment Disbursement',
  'B2C Payment Disbursement Reference','M-Pesa Suite Settings','Mpesa API Client',
  'Mpesa B2B Payment Request','Mpesa B2C Status Query','Mpesa Balance Snapshot',
  'Mpesa C2B Payment Register','Mpesa C2B Payment Register URL','Mpesa Conversion Rate',
  'Mpesa Express Request','Mpesa Match Log','Mpesa Payment Intent','Mpesa Public Key Certificate',
  'Mpesa Reconciliation Priority','Mpesa Reversal Request','Mpesa Sandbox Test Case',
  'Mpesa Sandbox Test Run','Mpesa Settings','Mpesa Statement Import','Mpesa Statement Row',
  'Mpesa Sub Account','Mpesa Webhook Delivery','Payment Gateway','Supplier M-Pesa Account');
```

**Expected results**

| Check | Before Install | After Install (with R1) | After Uninstall |
|---|---|---|---|
| A1 | "Frappe Mpsa Payments" owned by the old app; no "Mpsa Suite"; no "Stanbic" | Both modules; "Mpsa Suite" owned by `mpsa_suite` | Only "Mpsa Suite", owned by `mpsa_suite` |
| A2 old module | All suite DocTypes, plus any made on the site | 0 (without R1: only DocTypes made on the site) | 0 |
| A3 old module | Baseline | 0 (without R1: only records made on the site) | 0 |
| A4 | Baseline | Baseline (plus new receipts); DocType total across the modules unchanged. In A4c, "Custom Field (all)" and "Property Setter (all)" can rise by what Install App adds. On the v15 test site: Custom Field (all) +6 (R5's Payment Entry fields, where the site never got them) and Property Setter (all) +1 (spike15:f3.log:42-43). A rise there is fine; a lower count is not. | Baseline; DocType total unchanged; no statement fails |
| A5 | 9 old jobs | v15: 9 new, 0 old (spike15:f3.log:30). v16: 9 + 9. | v15: 9 new. v16: 9 + 9 until migrate, then 9 new. |
| A6 | Every line of C's patches.txt with the old prefix | Plus the same lines with the `mpsa_suite` prefix. A retried Install App writes them a second time; that is harmless (v15:installer.py:531-537; journal: v15 stage). | Unchanged |
| A7 | ... `frappe_mpsa_payments` | ... `frappe_mpsa_payments`, `mpsa_suite` | ... `mpsa_suite` |
| A10 | No rows (any row: ask the developer) | No rows, or **stop** | No rows, or **stop** before migrating |
