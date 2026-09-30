"""Historical NFL player-prop lines from The Odds API, for testing the Line Desk prop model against real prices.
Runs on GitHub Actions (.github/workflows/props_hist.yml); resumable: every run continues where the last one stopped and
re-queues itself until everything is pulled. Publishes to this repo's release "props-hist":
  props_<season>.csv.gz   one row per book x market x player x side x snapshot:
                          event_id, season, week, game_type, commence, away, home, snap, snap_ts, book, market, player, side, point, price, book_update
  props_state.json        which (event, snapshot) pairs are done, credits used, progress per season
Snapshots per game: early = kickoff - 96 h (typically Wednesday/Thursday, near the opener), mid = kickoff - 24 h, close = kickoff - 15 min.
Markets: passing / rushing / receiving yards, receptions, anytime TD. Books: the same 10 as the line poller (= 1 region of credits).
Cost: 10 credits x markets returned per snapshot (about 50), about 150 per game, about 130,000 for 2023-2026.
Env: ODDS_API_KEY (required), SEASONS (default 2023,2024,2025,2026), CREDIT_FLOOR (default 1,000,000: never go below this, so the
line poller always has credits), RUN_CREDIT_CAP (default 45,000 per run), RUN_SECONDS (default 1500)."""
import os, io, csv, gzip, json, time, datetime
import requests

KEY = os.environ["ODDS_API_KEY"]
API = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
BOOKS = "draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,pinnacle,bovada,betonlineag,lowvig"
MARKETS = "player_pass_yds,player_rush_yds,player_reception_yds,player_receptions,player_anytime_td"
SEASONS = [int(s) for s in (os.environ.get("SEASONS") or "2023,2024,2025,2026").split(",")]
FLOOR = int(os.environ.get("CREDIT_FLOOR") or 1_000_000); CAP = int(os.environ.get("RUN_CREDIT_CAP") or 45_000); SECS = int(os.environ.get("RUN_SECONDS") or 1500)
SNAPS = {"early": datetime.timedelta(hours=96), "mid": datetime.timedelta(hours=24), "close": datetime.timedelta(minutes=15)}
FIRST_PROPS = datetime.datetime(2023, 5, 3, tzinfo=datetime.timezone.utc)      # historical player props start here
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REL = f"https://github.com/{REPO}/releases/download/props-hist/{{}}" if REPO else None
S = requests.Session(); S.headers["User-Agent"] = "line-desk-props (github actions)"
os.makedirs("out", exist_ok=True)
NOW = datetime.datetime.now(datetime.timezone.utc); T0 = time.time()
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
pt = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
U = {"remaining": None, "spent_this_run": 0, "calls": 0}
COLS = ["event_id", "season", "week", "game_type", "commence", "away", "home", "snap", "snap_ts", "book", "market", "player", "side", "point", "price", "book_update"]

def call(path, **params):
    params["apiKey"] = KEY
    for k in range(5):
        r = S.get(API + path, params=params, timeout=90)
        if r.status_code == 429: time.sleep(4 * (k + 1)); continue
        U["calls"] += 1
        if r.headers.get("x-requests-remaining") is not None: U["remaining"] = float(r.headers["x-requests-remaining"])
        if r.headers.get("x-requests-last"): U["spent_this_run"] += float(r.headers["x-requests-last"])
        if r.status_code == 404 or (r.status_code == 422 and "EVENT_NOT_FOUND" in r.text): return None
        if r.status_code != 200: raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"{path}: rate limited")

def budget_ok():
    if U["remaining"] is not None and U["remaining"] < FLOOR: return False
    return U["spent_this_run"] < CAP and time.time() - T0 < SECS

def fetch(name):
    if not REL: return None
    try:
        r = S.get(REL.format(name), timeout=300)
        return r.content if r.status_code == 200 else None
    except Exception: return None

# ---------- state and existing data ----------
raw = fetch("props_state.json"); st = json.loads(raw) if raw else {"done": {}, "events": {}, "credits": 0}
data = {}
for s in SEASONS:
    raw = fetch(f"props_{s}.csv.gz")
    data[s] = gzip.decompress(raw).decode() if raw else ",".join(COLS) + "\n"

# ---------- schedule (nflverse) -> weekly windows ----------
G = list(csv.DictReader(io.StringIO(S.get("https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv", timeout=120).text)))
weeks, ngames = {}, {}
for g in G:
    s = int(g["season"])
    if s not in SEASONS or not g["gameday"]: continue
    d = datetime.datetime.fromisoformat(g["gameday"]).replace(tzinfo=datetime.timezone.utc)
    k = (s, int(g["week"]), g["game_type"])
    lo, hi = weeks.get(k, (d, d)); weeks[k] = (min(lo, d), max(hi, d)); ngames[k] = ngames.get(k, 0) + 1

# ---------- list each week's events once (1 credit per call) ----------
for (s, w, gt), (lo, hi) in sorted(weeks.items()):
    key = f"{s}-{w:02d}"
    if key in st["events"] or hi + datetime.timedelta(days=1) > NOW or lo < FIRST_PROPS: continue
    if not budget_ok(): break
    snap = lo - datetime.timedelta(days=2)
    r = call(f"/historical/sports/{SPORT}/events", date=iso(snap.replace(hour=12)), commenceTimeFrom=iso(lo - datetime.timedelta(hours=12)),
             commenceTimeTo=iso(hi + datetime.timedelta(days=1, hours=12)), dateFormat="iso")
    evs = (r or {}).get("data", []) or []
    st["events"][key] = [{"id": e["id"], "commence": e["commence_time"], "away": e["away_team"], "home": e["home_team"], "gt": gt} for e in evs]
    st.setdefault("weekcheck", {})[key] = [len(evs), ngames[(s, w, gt)]]          # events found vs games on the schedule
print("weeks listed", len(st["events"]), "credits", U["spent_this_run"], flush=True)

# ---------- snapshots: soonest-first within each season, oldest season first ----------
todo = []
for key, evs in sorted(st["events"].items()):
    s, w = int(key[:4]), int(key[5:])
    for e in evs:
        for sn, dt in SNAPS.items():
            if f'{e["id"]}|{sn}' not in st["done"]: todo.append((s, w, e, sn, pt(e["commence"]) - dt))
rows_new = {s: [] for s in SEASONS}; n_done = 0
for s, w, e, sn, t in todo:
    if not budget_ok(): break
    r = call(f"/historical/sports/{SPORT}/events/{e['id']}/odds", date=iso(t), bookmakers=BOOKS, markets=MARKETS, oddsFormat="american", dateFormat="iso")
    ev = (r or {}).get("data") or {}
    ts = (r or {}).get("timestamp", "")
    for b in ev.get("bookmakers", []) or []:
        for m in b.get("markets", []) or []:
            for o in m.get("outcomes", []) or []:
                rows_new[s].append([e["id"], s, w, e["gt"], e["commence"], e["away"], e["home"], sn, ts, b["key"], m["key"], o.get("description", ""),
                                    o.get("name", ""), o.get("point", ""), o.get("price", ""), m.get("last_update", "")])
    st["done"][f'{e["id"]}|{sn}'] = len(ev.get("bookmakers", []) or [])
    n_done += 1
    if n_done % 50 == 0: print(n_done, "snapshots", "credits", U["spent_this_run"], "remaining", U["remaining"], flush=True)

# ---------- write ----------
for s in SEASONS:
    buf = io.StringIO(); w_ = csv.writer(buf); [w_.writerow(r) for r in rows_new[s]]
    txt = data[s] + buf.getvalue()
    open(f"out/props_{s}.csv.gz", "wb").write(gzip.compress(txt.encode(), 6))
total = sum(len(v) * len(SNAPS) for v in st["events"].values())
st["credits"] = st.get("credits", 0) + U["spent_this_run"]
short = {k: v for k, v in st.get("weekcheck", {}).items() if v[0] < v[1]}
st["progress"] = {"weeks_missing_games": short, "snapshots_done": len(st["done"]), "snapshots_total": total, "remaining_credits": U["remaining"], "last_run": iso(NOW),
                  "this_run": {"snapshots": n_done, "credits": U["spent_this_run"], "calls": U["calls"]}}
json.dump(st, open("out/props_state.json", "w"))
pending_weeks = [k for k, (lo, hi) in weeks.items() if f"{k[0]}-{k[1]:02d}" not in st["events"] and hi + datetime.timedelta(days=1) <= NOW and lo >= FIRST_PROPS]
complete = len(st["done"]) >= total and not pending_weeks
floor_hit = U["remaining"] is not None and U["remaining"] < FLOOR
if complete or floor_hit: open("out/COMPLETE", "w").write("floor" if floor_hit and not complete else "done")
print(json.dumps(st["progress"]), "complete" if complete else ("stopped at credit floor" if floor_hit else "more to do"))
