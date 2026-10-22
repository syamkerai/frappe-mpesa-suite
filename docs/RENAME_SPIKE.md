# Rename to Mpsa Suite: how the branch was made, and the evidence so far

The app `frappe_mpsa_payments` ("Astral M-Pesa Suite", module "Frappe Mpsa Payments") becomes
`mpsa_suite` ("Mpsa Suite", module "Mpsa Suite") on branch `mpsa-suite`. DocType names, tables,
roles, secrets, Payment Gateway names and the `/pay/<token>` page do not change. The full plan,
with the hand-over steps for Trident and Astral, is `docs/RENAME_PLAN.md`, written alongside the
old-name branch (it is not part of this branch). This note records what was built here and what
has been shown to work.

## How the branch was made

1. **Release C.** The last old-name release is commit `ee5b201` on `astral-suite`. It carries the
   old app's own uninstall guard (R3, added in `02d94a0`): it refuses to uninstall
   `frappe_mpsa_payments` unless `mpsa_suite` is installed and owns the module "Mpsa Suite", or
   site_config has `mpsa_allow_old_uninstall` set to 1 or true (`088608d` stopped "0" unlocking
   it; `ee5b201` installs openpyxl in CI's unit job). Nothing may land on the old-name branch
   after C.
2. **Rename commit.** `scripts/do_rename.py` run on a clean checkout of `02d94a0`. C differs from
   it only in the old app's guard and its test (`088608d`), which this branch replaces with R2,
   and in a CI line this branch already has (`ee5b201`). Both are merged into this branch
   without changing its files, and the generated tree is unchanged:

   ```
   python3 scripts/do_rename.py --src <checkout of 02d94a0> --dst <new dir> \
       --modified "2026-10-22 20:35:00.000000"
   ```

   It copied 347 files, rewrote 165 of them (739 replacements), bumped `modified` in 14 record
   JSONs so a plain migrate re-imports them into the new module, wrote `mpsa_suite/handover.py`
   and built the lookup table: 70 old paths, 29 of them guest. The output was committed as is.
   Running the same command on `02d94a0` reproduces that commit. Run it under Linux or WSL: on Windows,
   deep paths exceed the 260-character limit.
3. **Safeguards commit.** R1, R2 and R4 to R9 below, added by hand.
4. **Review fixes.** `before_install` for Frappe 15's module map (below), the R4 bench test moved
   to where Frappe 15's test runner finds it, and the docs updated to the new names.

## The safeguards

| # | What | Where |
|---|---|---|
| R1 | `after_sync` runs `rehome()` at the end of install-app, so nothing is left in the old module once mpsa_suite is installed (the check before uninstalling the old app can expect zero). | `mpsa_suite/hooks.py` `after_sync`; `handover.after_sync` |
| R2 | mpsa_suite's own `before_uninstall` refuses every uninstall of mpsa_suite, whatever the row counts, unless site_config has `mpsa_allow_uninstall: 1`. It runs even when mpsa_suite is missing from installed_apps. `handover.before_app_uninstall("mpsa_suite")` repeats the check. | `mpsa_suite/uninstall_guard.py`; `handover.before_app_uninstall` |
| R3 | Old app only (release C), see above. Not shipped under the new name. | `frappe_mpsa_payments/uninstall_guard.py` at C (`ee5b201`) |
| R4 | Unit test: each of the 70 whitelisted functions of C has an entry, each target exists, is whitelisted and keeps C's guest setting. A bench test imports every target as Frappe's handler does. | `tests/unit/test_rename_safeguards.py`, `tests/unit/rename_whitelist_at_c.json` (made by `scripts/gen_whitelist_snapshot.py` from git); bench test `mpsa_suite/tests/test_rename_lookup.py` |
| R5 | The 6 Payment Entry STK fields are made on every migrate and at install, filed under `MODULE`; the patch now calls the same function. | `mpsa_suite/mpsa_suite/migrate.py` `create_payment_entry_mpesa_fields` |
| R6 | `rehome()` also moves `User.default_app` and System Settings `default_app` from the old app to mpsa_suite. | `handover.APP_FIELDS`, `SINGLE_APP_FIELDS` |
| R7 | The rename script and this note are kept in the repository. | `scripts/do_rename.py`, `docs/RENAME_SPIKE.md` |
| R8 | Each of the 8 registered patches returns at once when its twin `frappe_mpsa_payments.frappe_mpsa_payments.patches.*` is in Patch Log with `skipped = 0` (what Frappe itself counts as done). No patch was added. | `handover.ran_under_old_name`; first lines of each `execute()` in `mpsa_suite/mpsa_suite/patches/` |
| R9 | CI runs on pushes to `mpsa-suite`. The linters stay on pull requests only, as on `astral-suite`: the code did not pass them before the rename, and a red run on every push would hide a failing test run. | `.github/workflows/ci.yml` |

The lookup table (`override_whitelisted_methods` in `hooks.py`) stays for good: Safaricom holds
the registered C2B and Pull URLs under the old paths until they are re-registered.

### Frappe 15 and a stale module map (`before_install`)

Frappe builds its map of apps to modules once per process, in `frappe.init`, from the site's
Redis cache key `app_modules`. `bench get-app` adds mpsa_suite to apps.txt but does not clear
that key. On Frappe 15, `install-app` uses the map as it found it, so on a bench whose cache was
warm before `get-app` it syncs no DocType of mpsa_suite. It still commits the Module Def
"Mpsa Suite", mpsa_suite in installed_apps and 8 Patch Log rows, and after_install then fails
with `DoesNotExistError: Module Mpsa Suite not found`. Frappe 16 rebuilds the map inside
install-app (`installer.install_app`, after `add_module_defs`); Frappe 15 does not.

`mpsa_suite.install.before_install` does what Frappe 16 does: when the map lacks mpsa_suite it
deletes the cache key and rebuilds the map from apps.txt. Frappe runs before_install inside
install-app before the Module Def and the DocType sync, so this covers a self-hosted bench and
Frappe Cloud's Install App alike.

## Evidence so far

**Frappe v16 spike** (throwaway site `rename16.localhost`, since dropped; outbound network
blocked; renamed copy made from an earlier working tree, f427f2b plus work not yet committed,
without R1 to R9). The site had the old
app with M-Pesa records, secrets, and, to mimic Astral, a site-made report, Server Script,
Client Script, Bank custom field holding a value and custom DocType with one row.

- Install mpsa_suite next to the old app (`install-app --force`, as Frappe Cloud runs it): 26
  shipped DocTypes and 44 of 45 custom fields moved to "Mpsa Suite"; both apps' 9 scheduled jobs
  existed side by side; 8 `mpsa_suite.*` Patch Log rows written, no patch ran.
- Uninstall the old app (`--yes --no-backup`): the hand-over hook moved the last 5 records
  (custom DocType, custom field, Server Script, report, Client Script); Frappe then removed only
  the empty Module Def "Frappe Mpsa Payments".
- After migrate: 27 DocTypes in "Mpsa Suite", 0 in the old module; 9 jobs, all `mpsa_suite.*`.
- Unchanged at every step: 11 rows in the M-Pesa tables, 56 + 9 Singles values, 5 secret rows,
  168 custom fields, the Bank field's value and the custom DocType's row.
- Old paths after the old app was gone: C2B validation, confirmation (through `/api/method/`
  and `/api/v2/method/`) and the STK callback returned 200, and their queued jobs ran under the
  new names and created the registers. An old path not in the table returned 417
  "App frappe_mpsa_payments is not installed".
- Without the hand-over hook the same uninstall would have dropped the custom DocType's table
  and deleted the report, both scripts and the Bank field.
- The bench files (apps.txt, apps.json, common_site_config.json) were restored to their
  original checksums afterwards.

**Frappe v15 hand-over spike** (2026-10-22, bench `~/mpesa15`, Frappe 15.123.1 / ERPNext
15.123.0, throwaway site `rename15.localhost`, branch `mpsa-suite` at `6d30ad0`, outbound network
blocked). The site had the old app at C with the same kinds of records as the v16 site.

- R3 refused `uninstall-app frappe_mpsa_payments --dry-run` while mpsa_suite was not installed.
- The first `install-app mpsa_suite --force` failed with the stale module map described above;
  nothing was lost, and nothing had been synced. After `clear-cache` the same command succeeded:
  R1 moved the site-made DocType, custom field, report, Client Script and Server Script; 27
  DocTypes in "Mpsa Suite", 0 in the old module; R5 added the 6 Payment Entry fields; the 9 old
  scheduled jobs were replaced by the 9 new ones, as expected on v15.
- Uninstalling the old app (R3 now allowed it) deleted only the empty Module Def "Frappe Mpsa
  Payments". The migrate after it found no orphaned DocType. Rows, Singles, secrets, the Bank
  field's value and the custom DocType's row were unchanged against the baseline.
- R2 refused `uninstall-app mpsa_suite --dry-run`.
- Old paths over HTTP after the old app was gone: C2B validation and confirmation (v1 and v2),
  the STK callback, the B2C result, Payments API `get_status` and `list_payments` and the Sandbox
  Lab ping all reached mpsa_suite; an unmapped old path returned 417.
- Bench tests on a fresh v15 site: all modules passed.

**The `before_install` fix, on the same v15 site.** The cached map was set to one without
mpsa_suite, as a warm cache from before `get-app` holds it. `install-app mpsa_suite --force`
without before_install failed with `Module Mpsa Suite not found`; with before_install, and no
`clear-cache`, the same command synced the DocTypes and finished.

**Bench tests after the review fixes** (`run-tests --app mpsa_suite`): 292 tests passed on v15
(`t15suite.localhost`) and 292 on v16 (`t16suite.localhost`). Each run includes
`mpsa_suite.tests.test_rename_lookup` (R4). Before the move, v15's runner skipped that test.

**Frappe v15 hand-over at the branch tip** (2026-10-22, bench `~/mpesa15`, Frappe 15.123.1 /
ERPNext 15.123.0, mpsa_suite code at `9927c16`, old app at C `02d94a0`, new throwaway sites
`rename15.localhost` and `rename15b.localhost`, outbound network blocked, the sites' jobs on a
redis queue no worker reads, scheduler paused). The site had the same records as the earlier
v15 site, and the landing page of Administrator and of System Settings set to the old app.

- R3 refused `uninstall-app frappe_mpsa_payments --dry-run`. The read-only danger list (plan
  section 7.2 step 4) at that point: 25 tables to drop, 2 Singles, 45 custom fields, 2 property
  setters, 5 reports (1 made on the site), the workspace, dashboard, 4 charts, 2 number cards, the
  Server Script and the Client Script. With `mpsa_allow_old_uninstall: 1` set for one run, the dry
  run listed the 27 DocTypes and changed nothing (every count equal); the flag was removed straight
  after.
- The site's cached module map was set to one without mpsa_suite (a cache warm from before
  `get-app`), with no `clear-cache`. `install-app mpsa_suite --force` succeeded in 9.8 s:
  before_install rebuilt the map; R1 moved the site-made DocType, custom field, report, Client
  Script and Server Script, and R6 moved both landing pages to mpsa_suite. Nothing was left in the
  old module (A2, A3 empty; A10 no rows); 27 DocTypes in "Mpsa Suite"; 9 new scheduled jobs and no
  old ones; 8 + 8 Patch Log rows. R5 created the 6 Payment Entry fields under "Mpsa Suite": the old
  app's install had recorded that patch as done without making them (custom fields 70 to 76).
- Both apps installed: the 18 Safaricom paths, the Sandbox Lab ping and two logged-in paths,
  called on the old and on the new path, answered the same (21 of 21 pairs), and the queued jobs
  were all `mpsa_suite.*`. A migrate with both apps installed (38 s) left everything in
  "Mpsa Suite", 9 new jobs and no old ones, and printed no orphan line.
- `uninstall-app frappe_mpsa_payments --no-backup --yes --force` (what Frappe Cloud runs) took 3 s:
  the hand-over hook found nothing left to move, and Frappe removed only the empty Module Def. The
  migrate after it printed no orphan line. Rows, Singles, secrets (each still decrypts), the Bank
  field's value and the custom DocType's row equal the baseline; the only differences are what the
  test added (an API client, the 6 Payment Entry fields, one property setter, Integration Requests
  from the HTTP calls).
- Old paths over HTTP with the old app gone, each next to its new path: all 18 Safaricom paths
  returned 200; the 7 Payments API paths answered with the gateway's own 401 without credentials and
  200 with a client key (`list_payments`); the pay page paths answered the gateway's own 404 for an
  unknown token; ping and `convert_amount_to_kes` 200; all 41 logged-in paths 403 to a guest (found
  and whitelisted) and 200 with an Administrator token for the two read-only ones tried; the two C2B
  paths through `/api/v2/method/` 200; an old path not in the table 417. 68 of 70 pairs answered the
  same; the 2 that differ are the STK and B2C results, where the old path carried IDs that exist
  (200) and the new path unknown ones. The queued jobs ran under the new names and made the C2B
  registers; the STK request took the callback; the B2C request and its reference row turned
  Failed (result 2001); both balance results were stored and their Integration Requests completed.
- Not caused by the rename, the same on old and new paths: an STK or B2C result with an unknown
  CheckoutRequestID or OriginatorConversationID gets HTTP 417, and `process_balance_info` with an
  unknown ConversationID gets 403. "Every old path returns 200" holds for payloads that match a
  record.
- R2 refused `uninstall-app mpsa_suite --dry-run`, and refused `uninstall-app mpsa_suite --yes
  --force --no-backup` after `remove-from-installed-apps mpsa_suite`; nothing changed.
- R8: with mpsa_suite's Patch Log rows deleted and mpsa_suite put back in installed_apps without
  install-app, the next migrate ran the 8 patches and each returned at once ("already ran as
  frappe_mpsa_payments..."). The sentinels a patch would have changed (Mpesa Settings API switches,
  a register with no currency) were untouched.
- Scheduler: `pause_scheduler: 1` in site_config made the scheduler inactive; the RQ Job list's
  "Enable Scheduler" call (`frappe.utils.scheduler.activate_scheduler`, as Administrator) set it to
  0 and made it active again.
- Orphan sweep on v15: a standard DocType filed under "Mpsa Suite" that mpsa_suite does not ship was
  listed by A10, and the next migrate printed "Orphaned DocType(s) found" and deleted its DocType
  record; its table and row stayed (plan section 3.2 G).
- Install without `--force` (`rename15b.localhost`): the DocTypes and the standard reports,
  workspace and number cards were re-imported into "Mpsa Suite" (the bumped `modified` dates), R1
  moved the site-made DocType and report, the uninstall removed only the empty Module Def, and the
  migrate after it printed no orphan line.

**Bench tests at the branch tip** (new sites set up as CI does: new-site, ERPNext, payments,
mpsa_suite, migrate, `run-tests --app mpsa_suite`): 292 tests passed on v15 (`t15suite.localhost`)
and 292 on v16 (`t16suite.localhost`), each including `mpsa_suite.tests.test_rename_lookup`. On
v16 the network guard refused 21 look-ups of `api.frankfurter.dev` (ERPNext's exchange rates) and
nothing else; on v15 it refused none. Unit tests: 1046 passed (Python 3.13 and 3.10).

**Not shown yet:** the Frappe Cloud dashboard flow (Install App, Uninstall, `pause_scheduler` in
the Site Config tab), a real restore that drops mpsa_suite from installed_apps (R8 was shown with
the app put back by hand), a site that came from Navari, v16 Desktop Icon and Workspace Sidebar
rows pointing at the old app, and live traffic during the switch. `docs/RENAME_PLAN.md` section 7
lists the rehearsals.

## Corrections to `docs/RENAME_PLAN.md`

That plan lives with the old-name branch, so these are recorded here:

- **Appendix A, check A7.** Frappe 15 and 16 keep installed_apps under `parent = '__global'`
  (`db.get_global` / `set_global`), so the query as written (`parent = '__default'`) returns no
  rows. Use the query below; in the v15 spike it returned the expected list at each step.

  ```sql
  SELECT defvalue FROM `tabDefaultValue` WHERE parent = '__global' AND defkey = 'installed_apps';
  ```
- **Sections 3.2 D and 7.2 step 6.** mpsa_suite's before_install now rebuilds a stale module map,
  so `install-app` needs no `clear-cache` first. On a self-hosted bench a
  `bench --site <site> clear-cache` before `install-app` is harmless and can still be added.
- **R3's flag (section 4.2, 7.1).** At `02d94a0` the old app's guard let the uninstall through
  for any value of `mpsa_allow_old_uninstall` that was not empty, so `bench set-config
  mpsa_allow_old_uninstall 0` (stored as the text "0" without `-p`) unlocked it. Fixed in
  `088608d`, part of C: like R2's `mpsa_allow_uninstall`, it accepts only 1 or true. Removing
  the key is still the clean way to lock it again.
