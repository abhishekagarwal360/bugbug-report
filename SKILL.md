---
name: AA-bugbug
description: Nightly BugBug automation health for 91mobiles, MSP (MySmartPrice) and 91mobiles Indonesia — last night's failed count and fail %, a 7-night failed-count table, which project is improving, and whether the QC team is keeping up (fixing broken test scripts, and raising genuine failures in JIRA — matched via the BugBug link in the JIRA description). Read-only. Use whenever Abhishek says "bugbug report", "how did the nightly run go", "automation failures", "is QC fixing test cases", or invokes /AA-bugbug.
argument-hint: Optional — number of nights (default 7) or a single project
---

# /AA-bugbug — nightly automation + QC check

**Read-only.** Never run tests, edit BugBug, or touch JIRA. At the end, offer to raise JIRAs for unraised real bugs (via `/AA-JIRA-Raise`) and stop.

## Fixed facts (don't re-discover)

| Project | BugBug project id | JIRA |
|---|---|---|
| 91mobiles | `0320aed9-3ddd-4bea-be81-91f8359db0c7` | M91 |
| MSP | `17942376-ba96-4e0c-80dd-90506f423dce` | MSP (MSP bugs sometimes land in M91) |
| 91m Indonesia | `0863f1c5-90e7-4e0b-9c27-a834e9e193c6` | M91 |

- Ignore the `91mobilesTest` project.
- **Night** = 3 PM IST to 3 PM IST the next day, labelled by the first date. That covers every project's scheduled batch: 91m runs about 12:30 AM–2:30 PM IST, MSP about 7:30 PM–12:30 AM, Indonesia about 3:30–7:30 PM. It also picks up QA re-runs that day.
- **Total** = unique test cases run that night. **Failed** = test cases whose *last* run that night failed or errored. A test that passes on auto-retry or a QA re-run counts as passed.
- JIRA descriptions link either a test **run** (`…/runs-history/tests/<runId>/`) or a **test** (`…/tests/<testId>/`). The script handles both and maps run → test.
- BugBug quirks: `next_token` pagination is broken (always null), and lists are only strictly ordered when you pass `ordering=-started`. The script pages by walking `startedBefore` back.

## Supabase cache (closed nights are never re-fetched)

- Tables `bugbug_test_runs` (every test run) and `bugbug_nights` (one row per fully cached night) live in the **seo-digest** Supabase project `uvdeabpuozztqliatehe`. The schema is in `supabase_tables.sql`, and RLS is on.
- The script reads and writes them with `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` from `C:\Users\abhis\thecrux-bootcamp\seo-digest\.env`. It reads them in-process and never prints them.
- `plan` skips every night already in `bugbug_nights`. So on a normal day only the latest one or two nights are fetched (about 12–20 calls instead of about 90).
- `report` first runs `sync`. A night that has closed (after 3 PM IST the next day) and was fetched after it closed is stored with `partial = false`.
- A night still running is stored with **`partial = true`**. The next run fetches that night **only down to its newest cached run** (or its oldest run still `running`/`queued`, whichever is earlier), minus 15 minutes of overlap. Older runs come from the cache, and `sync` merges the two before overwriting the night's figures. `skipped` and `stopped` count as finished. Label it "partial" wherever it is shown — its tail is missing and its fail % will move.
- If Supabase is unreachable, the script prints a note and fetches everything from BugBug. It never fails.
- Re-cache a night: delete its rows from both tables, then run the skill again.

## Step 1 — Pull BugBug runs (delegate, one subagent)

Pages under ~95 items come back **inline** and flood the context (about 15k tokens each). So hand the paging to **one** `general-purpose` subagent with `model: haiku`. It only copies pages, so the cheapest model is enough. Use this prompt verbatim, with `<this session's .jsonl>` filled in:

> You are a mechanical data fetcher. Do NOT analyse or summarise any data. Loop:
> 1. Run: `PYTHONIOENCODING=utf-8 python "C:/Users/abhis/.claude/skills/AA-bugbug/bugbug_report.py" plan --session "<this session's .jsonl>"`
> 2. If the output says "bugbug_list_test_runs calls pending", make EVERY listed call with `mcp__plugin_bugbug_bugbug__bugbug_list_test_runs` (load it with ToolSearch `select:mcp__plugin_bugbug_bugbug__bugbug_list_test_runs` if needed). Use projectId, startedAfter and startedBefore exactly as printed, plus pageSize=100 and ordering="-started". Issue all calls in a round in parallel. Don't read results or saved files.
> 3. Repeat from 1 until the output no longer says "calls pending". Don't make bugbug_get_test_run calls.
> Final reply: one line — rounds run + first line of the final plan output.

- A full 7-night run is about 90 pages. 91mobiles alone runs roughly 750 test runs a night.
- The script reads results straight from the session transcripts, including `subagents/*.jsonl`. Nothing needs to be copied.
- Don't trust the subagent's summary. Step 3's `plan` is the check.

## Step 2 — JIRA tickets with BugBug links (main thread, while the subagent runs)

**Incremental.** `jira_cache.json` (next to the script) keeps every ticket seen so far and the time of the last search. So ask JIRA only for tickets **updated** since then. `plan` prints the exact JQL once BugBug is done, or you can build it yourself:
```
project in (M91, MSP) AND updated >= "<last search date - 1 day>" AND (labels in (bugbug, BugBug, Bugbug) OR description ~ "app.bugbug.io" OR summary ~ "bugbug") ORDER BY created DESC
```
If the cache file is missing, the JQL falls back to `created >= -90d`.

Call `searchJiraIssuesUsingJql` with cloudId `91mobile.atlassian.net`, markdown, maxResults 100, and fields `summary,status,created,description,reporter,issuetype,labels,components`. Skip `assignee`, because its avatar blobs bloat the output. Follow `nextPageToken` if there is one. Then run `jira --session <this session's .jsonl>`: it merges the results into the cache (which stores a slimmed copy, with no avatars or image blobs) and upserts `bugbug_jira`. An empty result is fine; it still counts as searched.

To rebuild the JIRA cache from scratch, delete `jira_cache.json` and run again.

## Step 3 — Close the gaps

Run `plan` again yourself:
- **Still "calls pending"**: send the subagent back to finish.
- **Lists `bugbug_get_test_run` calls**: these are old run IDs from JIRA. Make them, in parallel.
- **Prints `DONE`**: go to step 4.

## Step 4 — Report

```bash
PYTHONIOENCODING=utf-8 python "C:/Users/abhis/.claude/skills/AA-bugbug/bugbug_report.py" report
```
It prints three markdown tables and writes `~/Downloads/bugbug-qc-YYYY-MM-DD.xlsx`, which has four sheets: Summary, 7-night trend, Failed last night (with error, likely cause, nights failed, JIRA key + status, run link), and Stuck.

### Reply format (short, no fluff)

1. **Last night**: one line per project, in the form `failed / total (x%)`. Mark a night that is still running as in progress.
2. **7-night table**: paste it.
3. **Improving?** One line per project: Improving, Worsening or Flat. This compares the first half of the week's fail % with the second half, and anything within ±2 points counts as Flat.
4. **Is QC keeping up?** One line per project with the verdict, plus the numbers behind it:
   - **Tests with JIRA**: failing tests that have a BugBug-linked ticket.
   - **Recovered**: failed earlier in the week, pass now. This is the evidence that scripts or bugs got fixed.
   - **Stuck**: failed on 3+ nights, still failing, and no JIRA. This is the key red flag: the test was neither fixed nor raised.
   - **Verdict**: Keeping up (stuck is 10% or less of failures and some JIRAs raised), Not keeping up (stuck above 30%), otherwise Partly.
5. The **top 5 stuck tests** by name, plus any **"likely real bug"** failures marked NOT RAISED.
6. The xlsx path, then one line: *"Want me to raise JIRAs for the N unraised likely-real bugs?"*

### Say plainly (confidence)

- "Likely real bug" versus "likely script or env issue" is **inferred from BugBug's error code**. Selector errors (`ELEMENT_DOES_NOT_EXIST`, `FRAME_DOES_NOT_EXIST`, `SCROLL_FAILED`) lean towards a broken script. Everything else, including `ASSERT_FAILED` and `CODE_EXECUTION_ERROR`, is counted as a likely real bug. QC's custom JS checks throw `CODE_EXECUTION_ERROR` on real bugs (M91-12938's "Duplicate product(s)"). None of this is verified.
- JIRA matching only finds tickets whose description has a BugBug link. A bug raised without the link shows as NOT RAISED.

## Daily automation (no Claude, runs itself)

- **GitHub Actions** in the private repo `abhishekagarwal360/bugbug-report` runs `python3 bugbug_report.py auto` twice daily (GitHub can start it 5–30 min late): **11:00 AM IST** (cron `30 5 * * *` UTC) for standup, when last night's row is still partial, and **3:30 PM IST** (cron `0 10 * * *`), after the night closes at 3 PM, so the row is final. Last week the batches finished 11 AM–1 PM, and QA re-runs went on until about 2:55 PM. Manual run: https://github.com/abhishekagarwal360/bugbug-report/actions/workflows/nightly.yml → **Run workflow** → green **Run workflow** (signed in as abhishekagarwal360).
- `auto` = the whole "update sheet" flow with **zero Claude tokens**: JIRA via REST (Atlassian API token), BugBug via its MCP server at `https://mcp.bugbug.io/mcp` with a **per-project** Bearer token (the REST API `app.bugbug.io/api/v1` is blocked on the Pro plan, the MCP server is not), then sync → jira → sheet → push → read-back verify. It fails loudly if the read-back doesn't match.
- Keys: GitHub secrets `BUGBUG_TOKENS` (JSON map projectId → token), `ATLASSIAN_EMAIL`, `ATLASSIAN_API_TOKEN`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `GOOGLE_SA_JSON`. On the laptop the same keys come from `.secrets.json` (gitignored), seo-digest's `.env` and the SA key file in Downloads.
- A keepalive step makes an empty commit when the repo has had none for 50 days; GitHub disables scheduled jobs after 60 idle days.
- The skill folder **is** the repo's working copy. After changing the script, commit and push as `abhishekagarwal360` so the daily job runs the new code.
- "Update sheet" by hand in Claude: just run `python bugbug_report.py auto` — no subagent needed. The subagent flow below is only the fallback if BugBug ever closes token access to its MCP server.
- New BugBug project: add its id to `PROJECTS` **and** its token to `BUGBUG_TOKENS` (both places).

## Daily sheet (N days × 3 projects)

**The live sheet — always update this one, never make a new file:**
https://docs.google.com/spreadsheets/d/16or5bwqoGmlC6QuV1CZm8xXCUoO6VGPNjx3hpPF69Wc/edit

- **Daily** tab: days as rows, newest first. Per project: Failed | Total | Fail % | JIRA created, under a merged project header. Last row is the Average (JIRA created is a total, not a mean).
- **JIRA tickets** tab: every BugBug-related ticket, newest first — key, project, created, **reporter**, status, type, matched by, summary, JIRA link, **BugBug test link**. Both link columns are `=HYPERLINK()` formulas, so the pusher writes that tab with `USER_ENTERED` (RAW would store them as plain text).

**Always include the night still running.** Its date shows as `27 Sep 2026 (partial)` and it stays out of the Average. `sync` stores it with `partial = true`, so the next update re-fetches it and replaces the row with full figures (the label goes away). Set `--to` to that night's date.

Steps:
1. Top up missing nights with `plan --nights N+1 --dates <from>..<to>`. `<to>` is the in-progress night. `<from>` is the **oldest night marked (partial) in the sheet**, not the day after the last row. Otherwise that partial night never gets re-fetched (24 Sep sat as partial until 28 Sep). For big backfills, split the paging across helpers with `--project "<label>"`, one slice each.
2. `sync --nights N+1` to cache the closed nights.
3. Run the incremental JIRA JQL from step 2 of this skill, then `jira --session <this session's .jsonl>` to cache the tickets.
4. `sheet --from <date> --to <date>` writes only the push payload, `%TEMP%\bugbug_sheet_data.json`. **No local xlsx** — the Google Sheet is the only copy (he said so on 29 Sep). Don't mention a local file in the reply.
5. `node sheets_push.js <sheetId> "C:/Users/abhis/AppData/Local/Temp/bugbug_sheet_data.json"` pushes both tabs into the live sheet.
   Pass `--session <this session's .jsonl>` to `sync` and `jira`. When another Claude session is open, auto-detect picks the wrong transcript and reports "nothing new" or "No JIRA search in this session" (happened 29 Sep).
6. Read the sheet back and check the headers, the percentages, the Average row **and that the formatting survived** — header bands, column widths, the border box, and the newest rows on both tabs.

**He formatted the sheet by hand. Every insert, update and delete has to come out looking the same, on both tabs.** He said so on 28 Sep. Never invent a look, and never leave a new row unformatted.

`sheets_push.js` does this by **snapshotting his formatting before it writes, then re-applying it to the new shape**:

- It captures the two Daily header rows, the first date row, a middle date row, the last date row, the Average row, the JIRA header, one JIRA data row, and every column width — then replays them over the rows the new data actually occupies. So a change he makes in the sheet is carried forward by the next push instead of reverted, and a row that didn't exist before still lands fully formatted.
- **Four separate row templates, not one.** The first date row carries the box's top edge and the last carries the bottom, so neither can stand in for the middle rows — a middle row is captured from row 4. Copying row 3 down ruled a line across all 34 rows (caught and reverted on 28 Sep).
- The `DEFAULT_*` specs in the file are only a seed for a tab that doesn't exist yet: Date header blue `#2172D6`, project blocks alternating pink `#FF99FF` / blue, his column widths, box borders, `0.0%`, bold Average.
- Formatting **below** the data is cleared, so a shorter range leaves no orphan styling behind.
- **Column layout still lives in the code.** On 25 Sep he deleted the "JIRA" key column (the Link cell already shows the key) and "Matched by". `jhdr`/`jrows` match that now. A column change he makes in the sheet is reverted by the next push unless the code changes too.
- The unmerge is scoped to the columns the script owns, so anything he adds to the right of the data survives.

**Before changing `sheets_push.js`, back the formatting up and diff it after.** Dump both tabs with `includeGridData=true` to a scratch file, push, then compare `effectiveFormat` cell by cell — that is the only way to catch a border or number format landing on the wrong row. A restore script that replays the backup's `userEnteredFormat` turns a bad push into a 30-second fix.

**Writing to Google Sheets:**
- The Drive connector can only rename or move a file. Rewriting cell contents needs the Sheets API, so `sheets_push.js` signs in as the service account `seo-digest@claude-code-499105.iam.gserviceaccount.com`, which has Editor on the sheet.
- The pusher clears old merges **before** writing values: writing into a stale merged range silently drops the value.
- Read `userEnteredFormat` when snapshotting, and fall back to `effectiveFormat`, so a cell that only inherits its look isn't wiped.

## Counting JIRA tickets

A ticket counts as BugBug-related when the **description** has an `app.bugbug.io` link or the word bugbug, or the ticket has the `bugbug` label or a bugbug component, or "bugbug" is in the summary. `matched_by` records which one.

- A JQL `text ~ "bugbug"` also matches tickets that only mention BugBug **in a comment**. Those are ordinary tickets someone commented on, not BugBug-raised, and `classify_jira` drops them (8 of 22 on 24 Sep).
- Project comes from the BugBug project id inside the link. With no link, M91 maps to 91mobiles and MSP to MSP, unless the text says Indonesia.
- There is no bugbug component in JIRA today; only "Desktop Site" and "Mobile Site" exist. The component check is there for when QA adds one.

Note: Indonesia ran only about 3 tests a night until 7 Sep, and its full suite started on 8 Sep. Don't read its early averages as health.

## Maintenance

- A new BugBug project (for example a new locale) needs one line in the `PROJECTS` dict in `bugbug_report.py`.
- If the schedules move so that a batch crosses 3 PM IST, change `CUT` in the script.
