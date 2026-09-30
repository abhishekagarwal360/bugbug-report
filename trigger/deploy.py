"""Deploy the bugbug-trigger Vercel project to his OFFICIAL Vercel team (abhishekagarwal360s-projects).

    python trigger/deploy.py            # create project if needed, set env vars, deploy to production
    python trigger/deploy.py --new-secret   # also rotate CRON_SECRET

Keys come from local files and are never printed:
  C:\\Users\\abhis\\.secrets\\vercel-official-token.txt   Vercel token for the official account
  .secrets.json -> TRIGGER_GH_TOKEN                   repo-scoped GitHub token (Actions: read & write)
  .secrets.json -> TRIGGER_CRON_SECRET                shared secret Vercel sends on cron calls
"""
import json, os, secrets, sys, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
SECRETS = os.path.join(SKILL, ".secrets.json")
TOKEN = open(r"C:\Users\abhis\.secrets\vercel-official-token.txt", encoding="utf-8").read().strip()
TEAM_SLUG = "abhishekagarwal360s-projects"
NAME = "bugbug-trigger"

S = json.load(open(SECRETS, encoding="utf-8"))
if "--new-secret" in sys.argv or not S.get("TRIGGER_CRON_SECRET"):
    S["TRIGGER_CRON_SECRET"] = secrets.token_hex(24)
    json.dump(S, open(SECRETS, "w", encoding="utf-8"), indent=1)


def api(method, path, body=None, team=None):
    if team:
        path += ("&" if "?" in path else "?") + f"teamId={team}"
    req = urllib.request.Request("https://api.vercel.com" + path, json.dumps(body).encode() if body is not None else None,
                                 {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode()
            return r.status, (json.loads(t) if t else {})
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


st, teams = api("GET", "/v2/teams")
team = next(t["id"] for t in teams["teams"] if t["slug"] == TEAM_SLUG)

st, p = api("GET", f"/v9/projects/{NAME}", team=team)
if st == 404:
    st, p = api("POST", "/v11/projects", {"name": NAME, "framework": None}, team=team)
    print("project created:", st, p.get("id") or p)
else:
    print("project exists:", p.get("id"))

env = [{"key": "CRON_SECRET", "value": S["TRIGGER_CRON_SECRET"], "type": "encrypted", "target": ["production"]},
       {"key": "GH_TOKEN", "value": S["TRIGGER_GH_TOKEN"], "type": "encrypted", "target": ["production"]}]
st, _ = api("POST", f"/v10/projects/{NAME}/env?upsert=true", env, team=team)
print("env vars set:", st, [e["key"] for e in env])

files = [{"file": f, "data": open(os.path.join(HERE, f), encoding="utf-8").read()} for f in ("api/trigger.js", "vercel.json")]
st, d = api("POST", "/v13/deployments?forceNew=1", {
    "name": NAME, "project": NAME, "target": "production", "files": files,
    "projectSettings": {"framework": None, "buildCommand": None, "installCommand": None, "outputDirectory": None}}, team=team)
if st >= 300:
    sys.exit(f"deploy failed: {st} {json.dumps(d)[:400]}")
for _ in range(60):
    st, d = api("GET", f"/v13/deployments/{d['id']}", team=team)
    if d.get("readyState") in ("READY", "ERROR", "CANCELED"):
        break
    time.sleep(3)
print("deployment:", d.get("readyState"), "| aliases:", d.get("alias"))
st, p = api("GET", f"/v9/projects/{NAME}", team=team)
print("crons registered:", [(c["path"], c["schedule"]) for c in (p.get("crons") or {}).get("definitions", [])])
