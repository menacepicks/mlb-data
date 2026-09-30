"""Historical NFL player-prop lines from The Odds API, for backtesting the props model. Runs on GitHub Actions (props_hist.yml).
For every NFL game since 2023 (props history starts May 2023) it saves two snapshots of every tracked book's player props:
  early = 72 hours before kickoff (roughly when props are first widely posted), close = 10 minutes before kickoff.
Markets: passing yards, rushing yards, receiving yards, receptions. Cost: 40 credits per snapshot (4 markets x 1 region of 10 books).
Resumable: it reads the previous props_hist.json.gz from this repo's release "props" and only pulls games it does not have yet.
The API key is read from the ODDS_API_KEY secret and never written anywhere.
Env: ODDS_API_KEY (required), SEASONS (default 2023,2024,2025,2026), CREDIT_FLOOR (default 200000), RUN_CREDIT_CAP (default 150000)."""
import os, io, json, gzip, time, datetime
import requests

KEY = os.environ["ODDS_API_KEY"]
API = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
BOOKS = "draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,pinnacle,bovada,betonlineag,lowvig"
MARKETS = "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions"
SEASONS = [int(s) for s in (os.environ.get("SEASONS") or "2023,2024,2025,2026").split(",")]
FLOOR = int(os.environ.get("CREDIT_FLOOR") or 200000); CAP = int(os.environ.get("RUN_CREDIT_CAP") or 150000)
REPO = os.environ.get("GITHUB_REPOSITORY", "")
S = requests.Session(); S.headers["User-Agent"] = "line-desk-props-history (github actions)"
NOW = datetime.datetime.now(datetime.timezone.utc)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
pt = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
U = {"remaining": None, "spent": 0, "calls": 0}
os.makedirs("out", exist_ok=True)

def call(path, **params):
    params["apiKey"] = KEY
    for k in range(5):
        r = S.get(API + path, params=params, timeout=90)
        if r.status_code == 429: time.sleep(4 * (k + 1)); continue
        U["calls"] += 1
        if r.headers.get("x-requests-remaining") is not None: U["remaining"] = float(r.headers["x-requests-remaining"])
        if r.headers.get("x-requests-last"): U["spent"] += float(r.headers["x-requests-last"])
        if r.status_code == 422 or r.status_code == 404: return None          # no snapshot for that event/time
        if r.status_code != 200: raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"{path}: rate limited")
def ok(): return (U["remaining"] is None or U["remaining"] > FLOOR) and U["spent"] < CAP

# previous results (resume)
DB = {"events": {}}
if REPO:
    try:
        r = S.get(f"https://github.com/{REPO}/releases/download/props/props_hist.json.gz", timeout=120)
        if r.status_code == 200: DB = json.loads(gzip.decompress(r.content))
    except Exception as e: print("no previous file:", e)
print("already have", len(DB["events"]), "games")

def save():
    DB["updated"] = iso(datetime.datetime.now(datetime.timezone.utc)); DB["usage"] = U
    DB["note"] = ("events[id] = {home, away, commence, snaps: {early|close: {ts, rows: [[book, market, player, side, point, price]]}}}. "
                  "early = 72 h before kickoff, close = 10 min before kickoff (The Odds API historical snapshots, 5-minute grid).")
    open("out/props_hist.json.gz", "wb").write(gzip.compress(json.dumps(DB, separators=(",", ":")).encode()))

# 1) list games: one events snapshot per week of each season (1 credit each)
games = {}
for s in SEASONS:
    t = datetime.datetime(s, 9, 1, tzinfo=datetime.timezone.utc); end = min(datetime.datetime(s + 1, 2, 20, tzinfo=datetime.timezone.utc), NOW)
    while t < end:
        d = call(f"/historical/sports/{SPORT}/events", date=iso(t), commenceTimeFrom=iso(t), commenceTimeTo=iso(t + datetime.timedelta(days=8)), dateFormat="iso")
        for e in (d or {}).get("data", []):
            if pt(e["commence_time"]) < NOW - datetime.timedelta(hours=3): games[e["id"]] = e
        t += datetime.timedelta(days=7)
print("games listed", len(games), "credits spent", U["spent"])

# 2) props snapshots, oldest first
def rows(ev):
    out = []
    for b in (ev or {}).get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                out.append([b["key"], m["key"], o.get("description"), o.get("name"), o.get("point"), o.get("price")])
    return out
n = 0
for gid, e in sorted(games.items(), key=lambda kv: kv[1]["commence_time"]):
    rec = DB["events"].setdefault(gid, {"home": e["home_team"], "away": e["away_team"], "commence": e["commence_time"], "snaps": {}})
    for lab, dt in (("close", datetime.timedelta(minutes=10)), ("early", datetime.timedelta(hours=72))):
        if lab in rec["snaps"]: continue
        if not ok(): break
        d = call(f"/historical/sports/{SPORT}/events/{gid}/odds", date=iso(pt(e["commence_time"]) - dt), bookmakers=BOOKS, markets=MARKETS, oddsFormat="american", dateFormat="iso")
        rec["snaps"][lab] = {"ts": (d or {}).get("timestamp"), "rows": rows((d or {}).get("data"))}
    n += 1
    if n % 25 == 0: save(); print(n, "games", U, flush=True)
    if not ok(): print("budget reached; rerun to continue"); break
save()
done = sum(1 for r in DB["events"].values() if "close" in r["snaps"] and "early" in r["snaps"])
print(json.dumps({"games_listed": len(games), "games_complete": done, "usage": U}))
