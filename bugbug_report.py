"""AA-bugbug helper. Reads BugBug + JIRA MCP results straight from the Claude Code
session transcript (inline results and "saved to file" results both), so nothing
has to be copied by hand.

  python bugbug_report.py plan   [--nights 7]   -> next MCP calls to make, or DONE
  python bugbug_report.py report [--nights 7] [--out DIR]  -> caches closed nights, then markdown + .xlsx
  python bugbug_report.py sync   -> only cache closed nights to Supabase

Night = 15:00 IST to 15:00 IST next day, labelled by the first date. That window
holds each project's scheduled batch (91m ~00:30-14:30 IST, MSP ~19:30-00:30,
Indonesia ~15:30-19:30) plus any QA re-runs that day.
"""
import glob, json, os, re, sys, uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PROJECTS = {  # BugBug project id -> label (91mobilesTest is deliberately excluded)
    "0320aed9-3ddd-4bea-be81-91f8359db0c7": "91mobiles",
    "17942376-ba96-4e0c-80dd-90506f423dce": "MSP",
    "0863f1c5-90e7-4e0b-9c27-a834e9e193c6": "91m Indonesia",
}
PAGE = 100
CUT = timedelta(hours=9, minutes=30)          # 15:00 IST == 09:30 UTC
FINAL = {"passed", "failed", "error"}
DONE = FINAL | {"skipped", "stopped"}   # terminal; anything else (running, queued) may still change
OVERLAP = timedelta(minutes=15)   # re-fetch margin below a partial night's newest cached run
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LINK = re.compile(r"projects/[^/\s]*?(" + UUID + r")/(runs-history/tests|tests)/(" + UUID + ")")
# Selector/locator errors usually mean the test script broke. CODE_EXECUTION_ERROR is NOT here:
# QC's custom JS asserts throw it on real bugs too (e.g. "Duplicate product(s)" -> M91-12938).
SCRIPT_ERR = {"ELEMENT_DOES_NOT_EXIST", "FRAME_DOES_NOT_EXIST", "SCROLL_FAILED", "ELEMENT_NOT_VISIBLE"}


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def night_of(started):
    return (ts(started) - CUT).date()


def windows(nights):
    now = datetime.now(timezone.utc)
    cur = (now - CUT).date()
    out = []
    for i in range(nights - 1, -1, -1):
        d = cur - timedelta(days=i)
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + CUT
        out.append((d, start, start + timedelta(days=1)))
    return out, now


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Supabase cache (seo-digest project) ----------
# Closed nights live in bugbug_test_runs / bugbug_nights (see supabase_tables.sql),
# so only the still-open night is fetched from BugBug. Creds come from seo-digest's .env.
SB_ENV = r"C:\Users\abhis\thecrux-bootcamp\seo-digest\.env"


def sb(method, path, body=None, prefer=None):
    import urllib.request, urllib.error
    if os.environ.get("SUPABASE_URL"):
        url, key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    else:
        txt = open(SB_ENV, encoding="utf-8").read()
        url, key = (re.search(r"^%s\s*=\s*(\S+)" % k, txt, re.M).group(1) for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"))
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/rest/v1/" + path, data, h, method=method), timeout=60) as r:
            t = r.read().decode()
            return json.loads(t) if t else None
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {path.split('?')[0]}: {e.read().decode()[:300]}") from None


def sb_select(path):
    out, off = [], 0
    while True:
        page = sb("GET", f"{path}&limit=1000&offset={off}")
        out += page
        off += 1000
        if len(page) < 1000:
            return out


def from_row(r):  # DB row -> BugBug list item shape
    return {"id": r["id"], "testId": r["test_id"], "name": r["name"], "status": r["status"],
            "started": r["started"], "errorCode": r["error_code"], "webappUrl": r["web_url"]}


def load_cache(wins, run_ids=()):
    """-> (cached {(pid, night)}, runs {id: item} for the window, runmap {runId: testId},
    floors {(pid, night): datetime}). Never fatal.
    A floor is how far down a partial night still needs re-fetching: its newest cached run,
    or its oldest unfinished one, minus OVERLAP. Everything older is already in the cache."""
    label2pid = {v: k for k, v in PROJECTS.items()}
    try:
        # partial nights (still running when cached) are deliberately not treated as cached,
        # so the next run re-fetches and overwrites them with the full night
        nights = sb_select("bugbug_nights?select=project,night,partial")
        cached = {(label2pid.get(r["project"]), datetime.fromisoformat(r["night"]).date())
                  for r in nights if not r["partial"]}
        partial = {(label2pid.get(r["project"]), datetime.fromisoformat(r["night"]).date())
                   for r in nights if r["partial"]}
        rows = sb_select(f"bugbug_test_runs?select=*&night=gte.{wins[0][0]}&night=lte.{wins[-1][0]}")
        runmap = {}
        if run_ids:
            for r in sb_select("bugbug_test_runs?select=id,test_id&id=in.(%s)" % ",".join(run_ids)):
                runmap[r["id"]] = r["test_id"]
        floors = {}
        for r in rows:
            key = (label2pid.get(r["project"]), datetime.fromisoformat(r["night"]).date())
            if key in partial and r["started"]:
                floors.setdefault(key, []).append((ts(r["started"]), r["status"] in DONE))
        floors = {k: min([max(t for t, _ in v)] + [t for t, fin in v if not fin]) - OVERLAP
                  for k, v in floors.items()}
        return cached, {r["id"]: from_row(r) for r in rows}, runmap, floors
    except Exception as e:
        print(f"(Supabase cache unavailable: {e}; fetching everything from BugBug)")
        return set(), {}, {}, {}


def pid_of(r):
    m = re.search(r"projects/[^/]*?(" + UUID + ")", r.get("webappUrl") or "")
    return m.group(1) if m else None


def window_state(calls, pid, start, floor=None):
    """-> (complete, cursor). Order-independent: each successful, strictly ordered page covers
    [oldest item, startedBefore] (a short page covers down to the window start). A night is
    complete when the pages chain from its top down to its start. For a closed night the top is
    its close time, so a pass made while it was still running can't count as complete."""
    end = start + timedelta(days=1)
    pages = []
    for inp, items, _ in calls:
        if (inp.get("projectId") == pid and inp.get("startedAfter") == iso(start)
                and inp.get("ordering") == "-started" and not inp.get("status") and inp.get("startedBefore")):
            got = [ts(i["started"]) for i in items if i.get("started")]
            bottom = start if len(items) < PAGE or not got else min(got)
            pages.append((ts(inp["startedBefore"]), bottom))
    if not pages:
        return False, None
    closed = datetime.now(timezone.utc) >= end
    cur = end if closed else max(t for t, _ in pages)
    while True:
        reach = [b for t, b in pages if t >= cur and b < cur]
        if not reach:
            break
        cur = min(reach)
    if cur <= max(start, floor or start):   # partial night: older runs are already cached
        return True, None
    return False, cur.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def sync(nights):
    """Save every fully-fetched night that isn't cached yet. Idempotent.
    A night still running is stored with partial=true and re-synced once it closes."""
    wins, now = windows(nights)
    calls, runs, _, _ = collect()
    cached, db_runs, _, floors = load_cache(wins)
    saved = []
    try:
        for pid, label in PROJECTS.items():
            for d, start, end in wins:
                open_night = end > now
                if (pid, d) in cached or not window_state(calls, pid, start, floors.get((pid, d)))[0]:
                    continue
                pick = lambda src: {i: r for i, r in src.items() if pid in (r.get("webappUrl") or "")
                                    and r.get("started") and night_of(r["started"]) == d}
                new = pick(runs)
                # partial night: runs below the floor come from the cache, the rest from this fetch
                mine = list({**(pick(db_runs) if (pid, d) in floors else {}), **new}.values())
                rows = [{"id": r["id"], "project": label, "test_id": r["testId"], "name": r.get("name"),
                         "status": r["status"], "error_code": r.get("errorCode"), "started": r["started"],
                         "night": str(d), "web_url": r.get("webappUrl")} for r in new.values()]
                for i in range(0, len(rows), 500):
                    sb("POST", "bugbug_test_runs", rows[i:i + 500], "resolution=merge-duplicates")
                last = {}
                for r in mine:
                    if r["status"] in FINAL and (r["testId"] not in last or r["started"] > last[r["testId"]]["started"]):
                        last[r["testId"]] = r
                sb("POST", "bugbug_nights", [{"project": label, "night": str(d), "runs": len(mine), "tests": len(last),
                                              "failed": sum(1 for r in last.values() if r["status"] != "passed"),
                                              "partial": open_night}],
                   "resolution=merge-duplicates")
                saved.append(f"{label} {d:%d %b} ({len(mine)} runs, {len(rows)} fetched{', PARTIAL' if open_night else ''})")
        # old runs looked up only because a JIRA links them (run -> test mapping for next time)
        old = [{"id": r["id"], "project": PROJECTS.get(pid_of(r), "other"), "test_id": r["testId"],
                "name": r.get("name"), "status": r["status"], "error_code": r.get("errorCode"),
                "started": r["started"], "night": str(night_of(r["started"])), "web_url": r.get("webappUrl")}
               for r in runs.values() if r.get("started") and night_of(r["started"]) < wins[0][0]]
        if old:
            sb("POST", "bugbug_test_runs", old, "resolution=merge-duplicates")
    except Exception as e:
        print(f"(Supabase save failed: {e})")
    print("Cached to Supabase: " + (", ".join(saved) if saved else "nothing new"))


# ---------- transcript parsing ----------
def transcripts():
    """Current session's transcript + its subagents' transcripts (the paging is
    usually delegated to a subagent, so its results live in subagents/*.jsonl)."""
    if "--session" in sys.argv:
        main = sys.argv[sys.argv.index("--session") + 1]
    else:
        def last_touch(p):
            subs = glob.glob(p[:-6] + "/subagents/*.jsonl")
            return max([os.path.getmtime(p)] + [os.path.getmtime(s) for s in subs])
        main = max(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=last_touch)
    return [main] + sorted(glob.glob(main[:-6] + "/subagents/*.jsonl"))


def result_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


def load_payloads(text):
    """Return parsed JSON objects from a tool result (inline or saved-to-file)."""
    m = re.search(r"Output has been saved to (.+?\.txt)", text)
    if m:
        try:
            raw = open(m.group(1), encoding="utf-8").read()
        except OSError:
            return []
    else:
        raw = text
    out = []
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):  # [{type,text}, ...] blocks
            for b in obj:
                try:
                    out.append(json.loads(b.get("text", "")))
                except Exception:
                    pass
        else:
            out.append(obj)
    except Exception:
        for chunk in raw.split("\n"):  # inline multi-block results
            chunk = chunk.strip()
            if chunk.startswith("{"):
                try:
                    out.append(json.loads(chunk))
                except Exception:
                    pass
    return out


def collect():
    uses, calls, runs, jira, runmap = {}, [], {}, {}, {}
    lines = (l for f in transcripts() for l in open(f, encoding="utf-8"))
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if b.get("type") == "tool_use":
                uses[b["id"]] = (b.get("name", ""), b.get("input") or {})
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                name, inp = uses[b["tool_use_id"]]
                payloads = load_payloads(result_text(b.get("content")))
                if name.endswith("bugbug_list_test_runs"):
                    if not any("items" in p for p in payloads):
                        continue  # errored call (e.g. ECONNRESET) - must not look like a short last page
                    items = [i for p in payloads for i in p.get("items", [])]
                    total = max([p.get("total_count") or 0 for p in payloads] or [0])
                    calls.append((inp, items, total))
                    for i in items:
                        if not i.get("testId"):  # test since deleted in BugBug: stable id from its name
                            i["testId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "bugbug-test:" + (i.get("name") or i["id"])))
                        runs[i["id"]] = i
                elif name.endswith("bugbug_get_test_run"):
                    for p in payloads:
                        it = p.get("item") or {}
                        if it.get("id") and it.get("testId"):
                            runmap[it["id"]] = it["testId"]
                            runs[it["id"]] = {k: it.get(k) for k in
                                              ("id", "testId", "name", "status", "started", "errorCode", "webappUrl")}
                elif "searchJiraIssuesUsingJql" in name or "getJiraIssue" in name:
                    global JIRA_SEARCHED
                    JIRA_SEARCHED = JIRA_SEARCHED or any("issues" in p for p in payloads)
                    for p in payloads:
                        for n in (p.get("issues") or {}).get("nodes", []):
                            jira[n["key"]] = n
    for r in runs.values():
        runmap[r["id"]] = r["testId"]
    return calls, runs, jira, runmap


# ---------- plan ----------
def plan(nights):
    wins, now = windows(nights)
    calls, _, jira, runmap = collect()
    jira = {**jira_cache()["nodes"], **jira}
    jira_runs = sorted({rid for _, kind, rid in jira_links(jira) if kind == "runs-history/tests"})
    cached, _, db_map, floors = load_cache(wins, jira_runs)
    runmap = {**db_map, **runmap}
    todo, skipped = [], []
    # optional slicing so several helpers can page in parallel without overlap
    only = sys.argv[sys.argv.index("--project") + 1] if "--project" in sys.argv else None
    rng = sys.argv[sys.argv.index("--dates") + 1].split("..") if "--dates" in sys.argv else None
    for pid in PROJECTS:
        if only and PROJECTS[pid] != only:
            continue
        for d, start, end in wins:
            if rng and not (rng[0] <= str(d) <= rng[1]):
                continue
            if (pid, d) in cached:
                skipped.append(f"{PROJECTS[pid]} {d:%d %b}")
                continue
            done, cursor = window_state(calls, pid, start, floors.get((pid, d)))
            if done:
                continue
            before = cursor or iso(min(end, now + timedelta(minutes=5)))
            todo.append({"tool": "bugbug_list_test_runs", "projectId": pid, "startedAfter": iso(start),
                         "startedBefore": before, "pageSize": PAGE, "ordering": "-started"})
    if skipped:
        print(f"From Supabase cache ({len(skipped)} nights, not re-fetched): {', '.join(skipped)}")
    if todo:
        for t in todo:
            print(f"{PROJECTS[t['projectId']]:14} projectId={t['projectId']} startedAfter={t['startedAfter']} "
                  f"startedBefore={t['startedBefore']}")
        print(f"\n{len(todo)} bugbug_list_test_runs calls pending (pageSize=100, ordering=-started).")
        print("Make them (parallel is fine), then run plan again.")
        return todo
    if not JIRA_SEARCHED:
        print("BugBug DONE. Next: run this JIRA search (markdown, maxResults 100, fields "
              "summary,status,created,description,reporter,issuetype,labels,components), then plan again:")
        print(jira_jql())
        return
    unknown = [r for r in jira_runs if r not in runmap]
    if unknown:
        print(json.dumps([{"tool": "bugbug_get_test_run", "runId": r} for r in unknown], indent=1))
        print(f"\n{len(unknown)} old test-run IDs in JIRA need mapping to a test. Call these, then plan again.")
        return
    print("DONE. Run: report")


def jira_links(jira):
    for key, n in jira.items():
        desc = ((n.get("fields") or {}).get("description")) or ""
        if not isinstance(desc, str):
            desc = json.dumps(desc)
        for pid, kind, rid in LINK.findall(desc):
            yield key, kind, rid


# ---------- JIRA cache ----------
JPROJ = {"M91": "91mobiles", "MSP": "MSP"}
JIRA_SEARCHED = False
JIRA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jira_cache.json")
JIRA_MATCH = '(labels in (bugbug, BugBug, Bugbug) OR description ~ "app.bugbug.io" OR summary ~ "bugbug")'


def jira_cache():
    """Local cache of raw JIRA nodes, so each update only asks JIRA for tickets updated since the last one."""
    try:
        return json.load(open(JIRA_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_search": None, "nodes": {}}


def jira_jql():
    last = jira_cache()["last_search"]
    since = f'updated >= "{(datetime.fromisoformat(last) - timedelta(days=1)).date()}"' if last else "created >= -90d"
    return f"project in (M91, MSP) AND {since} AND {JIRA_MATCH} ORDER BY created DESC"


def slim(n):  # keep what classify_jira / jira_links / the sheet read; drop avatars and image blobs
    f = n.get("fields") or {}
    desc = f.get("description") or ""
    if isinstance(desc, str):
        desc = re.sub(r"!\[\]\(blob:[^)]*\)", "", desc)
    return {"key": n["key"], "webUrl": n.get("webUrl"), "fields": {
        "summary": f.get("summary"), "description": desc, "created": f.get("created"),
        "status": {"name": (f.get("status") or {}).get("name")},
        "issuetype": {"name": (f.get("issuetype") or {}).get("name")},
        "reporter": {"displayName": (f.get("reporter") or {}).get("displayName")},
        "labels": f.get("labels") or [], "components": [{"name": c.get("name")} for c in f.get("components") or []],
        "project": f.get("project") or {}}}


def classify_jira(n):
    """-> row, or None when the ticket only mentions BugBug in a comment (not BugBug-raised)."""
    f = n["fields"]
    desc = f.get("description") or ""
    if not isinstance(desc, str):
        desc = json.dumps(desc)
    summary = f.get("summary") or ""
    labels = [l.lower() for l in (f.get("labels") or [])]
    comps = [c.get("name", "") for c in (f.get("components") or [])]
    link = "app.bugbug.io" in desc.lower()
    matched = ("link" if link else
               "label" if "bugbug" in labels else
               "component" if any("bugbug" in c.lower() for c in comps) else
               "description" if "bugbug" in desc.lower() else
               "summary" if "bugbug" in summary.lower() else None)
    if not matched:
        return None
    jp = (f.get("project") or {}).get("key") or n["key"].split("-")[0]
    bb = sorted({PROJECTS[i] for i in set(re.findall(r"projects/[a-z0-9-]*?(" + UUID + ")", desc))
                 if i in PROJECTS})
    proj = bb[0] if bb else ("91m Indonesia" if "indonesia" in (summary + desc).lower() else JPROJ.get(jp))
    m = re.search(r"https://app\.bugbug\.io/\S+?/(?:runs-history/tests|tests)/" + UUID + r"/?", desc)
    return {"key": n["key"], "jira_project": jp, "bugbug_project": proj, "summary": summary,
            "issue_type": (f.get("issuetype") or {}).get("name"), "status": (f.get("status") or {}).get("name"),
            "created": f["created"][:10], "has_link": link, "matched_by": matched,
            "reporter": (f.get("reporter") or {}).get("displayName"),
            "bugbug_url": m.group(0).rstrip("/") if m else None,
            "url": n.get("webUrl") or f"https://91mobile.atlassian.net/browse/{n['key']}"}


def sync_jira():
    _, _, fresh, _ = collect()
    if not JIRA_SEARCHED:
        print("No JIRA search in this session. Run this JQL first:\n" + jira_jql())
        return
    cache = jira_cache()
    cache["nodes"].update({k: slim(n) for k, n in fresh.items()})
    cache["last_search"] = datetime.now(timezone.utc).isoformat()
    json.dump(cache, open(JIRA_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    jira = cache["nodes"]
    rows = [r for r in (classify_jira(n) for n in jira.values()) if r]
    if rows:
        sb("POST", "bugbug_jira", rows, "resolution=merge-duplicates")
    skipped = len(jira) - len(rows)
    print(f"JIRA: {len(fresh)} new/updated this time; cached {len(rows)} BugBug tickets "
          f"({skipped} mention BugBug only in comments, skipped)")


# ---------- report ----------
def report(nights, outdir):
    sync(nights)  # cache any newly closed nights first
    wins, now = windows(nights)
    _, runs, jira, runmap = collect()
    jira = {**jira_cache()["nodes"], **jira}
    jira_runs = sorted({rid for _, kind, rid in jira_links(jira) if kind == "runs-history/tests"})
    _, db_runs, db_map, _ = load_cache(wins, jira_runs)
    runs = {**db_runs, **runs}
    runmap = {**db_map, **runmap}
    nightset = {d for d, _, _ in wins}
    # final status per project/night/test = latest finished run
    final = defaultdict(dict)   # (pid, night) -> testId -> run
    for r in runs.values():
        m = LINK.search(r.get("webappUrl", "")) or re.search(r"projects/[^/]*?(" + UUID + ")", r.get("webappUrl", ""))
        pid = m.group(1) if m else None
        if pid not in PROJECTS or not r.get("started") or r.get("status") not in FINAL:
            continue
        n = night_of(r["started"])
        if n not in nightset:
            continue
        r["_pid"] = pid
        cur = final[(pid, n)].get(r["testId"])
        if cur is None or r["started"] > cur["started"]:
            final[(pid, n)][r["testId"]] = r

    # JIRA: test id -> tickets
    t2j = defaultdict(set)
    for key, kind, rid in jira_links(jira):
        tid = runmap.get(rid) if kind == "runs-history/tests" else rid
        if tid:
            t2j[tid].add(key)

    def jstat(key):
        f = jira[key].get("fields") or {}
        return f"{key} ({(f.get('status') or {}).get('name', '?')})"

    cur_night = wins[-1][0]
    lines, sheets = [], {"Summary": [], "7-night trend": [], "Failed last night": [], "Stuck (no fix, no JIRA)": []}
    trend_hdr = ["Night"] + [f"{p} failed / total (%)" for p in PROJECTS.values()]
    trend_rows = []
    summary = []
    for d, _, _ in wins:
        row = [d.strftime("%d %b") + (" (in progress)" if d == cur_night and now < wins[-1][2] else "")]
        for pid in PROJECTS:
            f = final.get((pid, d), {})
            tot, bad = len(f), sum(1 for r in f.values() if r["status"] != "passed")
            row.append(f"{bad} / {tot} ({bad / tot * 100:.0f}%)" if tot else "-")
        trend_rows.append(row)

    for pid, label in PROJECTS.items():
        per = [(d, final.get((pid, d), {})) for d, _, _ in wins]
        sizes = sorted(len(f) for _, f in per if f)
        med = sizes[len(sizes) // 2] if sizes else 0
        usable = [(d, f) for d, f in per if f and len(f) >= 0.5 * med]
        if not usable:
            summary.append([label, "no runs", "", "", "", "", "", "", ""])
            continue
        last_d, last = usable[-1]
        tot = len(last)
        failed = {t: r for t, r in last.items() if r["status"] != "passed"}
        pct = lambda f: sum(1 for r in f.values() if r["status"] != "passed") / len(f) * 100
        rates = [pct(f) for _, f in usable]
        half = max(1, len(rates) // 2)
        early, late = sum(rates[:half]) / half, sum(rates[-half:]) / half
        trend = "Improving" if late < early - 2 else "Worsening" if late > early + 2 else "Flat"

        fail_nights = defaultdict(int)
        for _, f in usable:
            for t, r in f.items():
                if r["status"] != "passed":
                    fail_nights[t] += 1
        ever_failed = set(fail_nights)
        recovered = [t for t in ever_failed if t in last and last[t]["status"] == "passed"]
        raised = [t for t in ever_failed if t2j.get(t)]
        stuck = [t for t, c in fail_nights.items() if c >= 3 and t in failed and not t2j.get(t)]
        script_like = sum(1 for r in failed.values() if (r.get("errorCode") or "") in SCRIPT_ERR)
        verdict = ("Keeping up" if len(stuck) <= 0.1 * max(1, len(failed)) and raised
                   else "Not keeping up" if len(stuck) > 0.3 * max(1, len(failed)) else "Partly")
        summary.append([label, last_d.strftime("%d %b"), tot, len(failed), f"{len(failed) / tot * 100:.0f}%",
                        trend, len(raised), len(recovered), len(stuck), script_like, verdict])
        for t, r in sorted(failed.items(), key=lambda x: -fail_nights[x[0]]):
            sheets["Failed last night"].append([label, r["name"], r.get("errorCode") or "",
                                                "likely script/env" if (r.get("errorCode") or "") in SCRIPT_ERR else "likely real bug",
                                                f"{fail_nights[t]}/{len(usable)}",
                                                ", ".join(jstat(k) for k in sorted(t2j.get(t, []))) or "NOT RAISED",
                                                r.get("webappUrl", "")])
        for t in sorted(stuck, key=lambda t: -fail_nights[t]):
            r = failed[t]
            sheets["Stuck (no fix, no JIRA)"].append([label, r["name"], r.get("errorCode") or "",
                                                      f"{fail_nights[t]}/{len(usable)}", r.get("webappUrl", "")])

    sum_hdr = ["Project", "Last night", "Tests run", "Failed", "Fail %", "7-night trend", "Tests with JIRA",
               "Recovered (failed earlier, pass now)", "Stuck (failing 3+ nights, no JIRA)",
               "Failed w/ script-type error", "QC verdict"]
    sheets["Summary"] = [sum_hdr] + summary
    sheets["7-night trend"] = [trend_hdr] + trend_rows
    sheets["Failed last night"].insert(0, ["Project", "Test", "Error", "Likely cause (by error code)",
                                           "Nights failed", "JIRA", "Last run link"])
    sheets["Stuck (no fix, no JIRA)"].insert(0, ["Project", "Test", "Error", "Nights failed", "Last run link"])

    def md(rows):
        h, *b = rows
        s = "| " + " | ".join(map(str, h)) + " |\n|" + "---|" * len(h) + "\n"
        return s + "".join("| " + " | ".join(map(str, r)) + " |\n" for r in b)

    print("## Last night\n" + md([[r[0], r[1], r[3], r[2], r[4], r[5]] for r in
                                   [["Project", "Night", "Tests", "Failed", "Fail %", "Trend"]] +
                                   [[x[0], x[1], x[2], x[3], x[4], x[5]] for x in summary if len(x) > 6]]))
    print("## Failed count, last 7 nights\n" + md(sheets["7-night trend"]))
    print("## QC check\n" + md([[x[0], x[6], x[7], x[8], x[9], x[10]] for x in
                                [["Project", "", "", "", "", "", "Tests with JIRA", "Recovered", "Stuck (no JIRA)",
                                  "Script-type errors", "Verdict"]] + [s for s in summary if len(s) > 6]]))
    print(f"JIRA tickets with BugBug links found: {len(jira)}; mapped to tests: {len(t2j)}")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        wb.remove(wb.active)
        for name, rows in sheets.items():
            ws = wb.create_sheet(name[:31])
            for r in rows:
                ws.append(r)
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="2172D6")
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(70, max(10, *(len(str(c.value or "")) for c in col)) + 2)
            ws.freeze_panes = "A2"
        path = os.path.join(outdir, f"bugbug-qc-{now.astimezone(timezone(timedelta(hours=5, minutes=30))):%Y-%m-%d}.xlsx")
        wb.save(path)
        print("XLSX:", path)
    except Exception as e:
        print("xlsx not written:", e)


# ---------- daily sheet (from Supabase) ----------
NCOL = 4  # per project: Failed | Total | Fail % | JIRA created


def sheet_data(a="2000-01-01", b="2999-12-31"):
    """-> (labels, days, daily rows, jira rows). Pure data, shared by the xlsx and the Google Sheet."""
    nights = sb_select(f"bugbug_nights?select=project,night,tests,failed,partial&night=gte.{a}&night=lte.{b}")
    part = {r["night"] for r in nights if r["partial"]}
    jira = sorted(sb_select("bugbug_jira?select=*"), key=lambda r: r["created"], reverse=True)
    by = {(r["project"], r["night"]): r for r in nights}
    jcount = defaultdict(int)
    for t in jira:
        jcount[(t["bugbug_project"], t["created"])] += 1
    days = sorted({r["night"] for r in nights}, reverse=True)
    labels = list(PROJECTS.values())
    daily = []
    for d in days:
        row = [datetime.fromisoformat(d).strftime("%d %b %Y") + (" (partial)" if d in part else "")]
        for lab in labels:
            n = by.get((lab, d))
            row += ([n["failed"], n["tests"], round(n["failed"] / n["tests"], 4)] if n and n["tests"]
                    else ["-", "-", "-"]) + [jcount.get((lab, d), 0)]
        daily.append(row)
    full = [r for d, r in zip(days, daily) if d not in part]  # a partial night would drag the mean down
    if days:  # average row (JIRA column is a total over every day, not a mean)
        avg = ["Average" + (" (excl. partial)" if part else "")]
        for i, lab in enumerate(labels):
            c = 1 + i * NCOL
            vals = [(r[c], r[c + 1]) for r in full if isinstance(r[c], int)]
            if vals:
                f = sum(v[0] for v in vals) / len(vals)
                t = sum(v[1] for v in vals) / len(vals)
                avg += [round(f, 1), round(t), round(f / t, 4)]
            else:
                avg += ["-", "-", "-"]
            avg.append(sum(r[c + 3] for r in daily))
        daily.append(avg)
    return labels, days, daily, jira


def sheet():
    """Writes the JSON payload that sheets_push.js sends to the live Google Sheet.
    No local xlsx - the Google Sheet is the only copy (he said so on 29 Sep)."""
    import tempfile
    a = sys.argv[sys.argv.index("--from") + 1] if "--from" in sys.argv else "2000-01-01"
    b = sys.argv[sys.argv.index("--to") + 1] if "--to" in sys.argv else "2999-12-31"
    labels, days, daily, jira = sheet_data(a, b)

    # Layout Abhishek settled on (25 Sep): no "JIRA" key column (the Link cell already shows the
    # key) and no "Matched by" (internal plumbing). Don't re-add them - a push rewrites the header.
    jhdr = ["Project", "Created", "Reporter", "Status", "Type", "Summary", "Link", "BugBug test"]
    def link(url, label):  # clickable in both Google Sheets and Excel
        return f'=HYPERLINK("{url}","{label}")' if url else ""

    jrows = [[t["bugbug_project"] or t["jira_project"], t["created"], t.get("reporter"),
              t["status"], t["issue_type"],
              ("'" + t["summary"]) if (t["summary"] or "").startswith(("=", "+")) else t["summary"],
              link(t["url"], t["key"]),
              link(t.get("bugbug_url"), "View run" if "runs-history" in (t.get("bugbug_url") or "") else "View test")]
             for t in jira]
    payload = {"labels": labels, "daily": daily, "jira_header": jhdr, "jira": jrows,
               "range": [days[-1], days[0]]}
    jpath = os.path.join(tempfile.gettempdir(), "bugbug_sheet_data.json")
    json.dump(payload, open(jpath, "w", encoding="utf-8"))
    print(f"{len(days)} days, {len(jira)} JIRA tickets")
    print("payload:", jpath)


# ---------- auto: the whole sheet update with no Claude in the loop ----------
# BugBug's REST API is plan-gated, but its MCP server (the one Claude uses) takes a per-project
# token as a Bearer header. JIRA comes from its REST API. Results are written in transcript
# shape to a scratch .jsonl, so plan/sync/jira read them exactly as they read a Claude session.
SECRETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secrets.json")
SHEET_ID = "16or5bwqoGmlC6QuV1CZm8xXCUoO6VGPNjx3hpPF69Wc"
SHEET_FROM = "2026-08-25"
JIRA_SITE = "https://91mobile.atlassian.net"


def secret(k):
    if os.environ.get(k):
        v = os.environ[k]
        return json.loads(v) if k == "BUGBUG_TOKENS" else v
    return json.load(open(SECRETS, encoding="utf-8"))[k]


def http_json(url, body, headers, tries=3):
    import urllib.request, time
    for i in range(tries):
        try:
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read().decode()
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  retry {i + 1} after: {e}")
            time.sleep(5 * (i + 1))


def bugbug_page(call, tokens):
    args = {k: v for k, v in call.items() if k != "tool"}
    raw = http_json("https://mcp.bugbug.io/mcp",
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "bugbug_list_test_runs", "arguments": args}},
                    {"Authorization": "Bearer " + tokens[call["projectId"]], "Accept": "application/json, text/event-stream"})
    res = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")][-1]
    if "error" in res or res["result"].get("isError"):
        raise RuntimeError(f"BugBug error: {json.dumps(res)[:300]}")
    return args, res["result"]["structuredContent"]


def jira_search():
    import base64
    auth = base64.b64encode(f'{secret("ATLASSIAN_EMAIL")}:{secret("ATLASSIAN_API_TOKEN")}'.encode()).decode()
    nodes, token = [], None
    jql = f"project in (M91, MSP) AND created >= -90d AND {JIRA_MATCH} ORDER BY created DESC"
    while True:
        body = {"jql": jql, "maxResults": 100,
                "fields": ["summary", "status", "created", "description", "reporter", "issuetype", "labels", "components", "project"]}
        if token:
            body["nextPageToken"] = token
        r = json.loads(http_json(JIRA_SITE + "/rest/api/3/search/jql", body,
                                 {"Authorization": "Basic " + auth, "Accept": "application/json"}))
        for i in r.get("issues", []):
            i["webUrl"] = f"{JIRA_SITE}/browse/{i['key']}"
            nodes.append(i)
        token = r.get("nextPageToken")
        if not token:
            return nodes


def auto():
    import subprocess, tempfile
    tokens = secret("BUGBUG_TOKENS")
    log = os.path.join(tempfile.gettempdir(), f"bugbug_auto_{uuid.uuid4().hex[:8]}.jsonl")
    sys.argv += ["--session", log]

    def record(name, inp, result):
        tid = "toolu_" + uuid.uuid4().hex
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}) + "\n")
            f.write(json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                                          "content": json.dumps(result)}]}}) + "\n")

    open(log, "w").close()
    record("searchJiraIssuesUsingJql", {}, {"issues": {"nodes": jira_search()}})
    n = int(sys.argv[sys.argv.index("--nights") + 1]) if "--nights" in sys.argv else 7
    for rnd in range(1, 300):
        todo = plan(n)
        if not todo:
            break
        for c in todo:
            args, page = bugbug_page(c, tokens)
            record("bugbug_list_test_runs", args, page)
        print(f"-- round {rnd}: {len(todo)} pages")
    else:
        raise SystemExit("gave up: BugBug paging did not finish in 300 rounds")
    sync(n)
    sync_jira()
    to = str(windows(1)[0][-1][0])
    sys.argv += ["--from", SHEET_FROM, "--to", to]
    sheet()
    here = os.path.dirname(os.path.abspath(__file__))
    subprocess.run(["node", os.path.join(here, "sheets_push.js"), SHEET_ID,
                    os.path.join(tempfile.gettempdir(), "bugbug_sheet_data.json"), "--verify"], check=True)


if __name__ == "__main__":
    n = int(sys.argv[sys.argv.index("--nights") + 1]) if "--nights" in sys.argv else 7
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.expanduser("~/Downloads")
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    {"plan": lambda: plan(n), "sync": lambda: sync(n), "sheet": lambda: sheet(), "jira": lambda: sync_jira(), "auto": auto}.get(mode, lambda: report(n, out))()
