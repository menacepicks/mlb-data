"""Opening and current lines for the Line Desks, from The Odds API. Runs on GitHub Actions every 15 minutes (.github/workflows/odds.yml).

For every upcoming game (NFL, college football, NHL, WNBA, MLB) and every tracked sportsbook, keeps per market (moneyline, spread, total):
  open  = the first price the book posted, with its time. Games first seen by a poll that followed another poll of the same sport
          within 30 minutes get the poll time (accurate to ~15 minutes). Games that were already posted when polling started are
          backfilled from The Odds API's historical snapshots (5-minute grid) by bisection, exactly like odds_history.ps1.
  now   = the latest pre-game price (it stops updating at first pitch / puck drop / kickoff, so it becomes the closing line).
Publishes to this repo's release "odds": lines_<sport>.json (one per sport) and usage.json (credits). The API key is read from the
ODDS_API_KEY secret and never written anywhere.
Env: ODDS_API_KEY (required), SPORTS (optional comma list), CREDIT_FLOOR (default 50000: stop backfilling below this many credits left),
     RUN_CREDIT_CAP (default 60000 credits of backfill per run), RUN_SECONDS (default 600 seconds of backfill per run)."""
import os, io, json, time, datetime
import requests

KEY = os.environ["ODDS_API_KEY"]
API = "https://api.the-odds-api.com/v4"
SPORTS = {"nfl": "americanfootball_nfl", "ncaaf": "americanfootball_ncaaf", "nhl": "icehockey_nhl", "wnba": "basketball_wnba", "mlb": "baseball_mlb"}
if os.environ.get("SPORTS"): SPORTS = {k: v for k, v in SPORTS.items() if k in os.environ["SPORTS"].split(",")}
BOOKS = "draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,pinnacle,bovada,betonlineag,lowvig"   # 10 books = 1 region of credits
MARKETS = "h2h,spreads,totals"
HORIZON = datetime.timedelta(days=10)            # track games starting within 10 days
LOOKBACK = datetime.timedelta(days=21)           # how far back a backfill searches for the first posting
FLOOR = int(os.environ.get("CREDIT_FLOOR") or 50000); CAP = int(os.environ.get("RUN_CREDIT_CAP") or 60000); SECS = int(os.environ.get("RUN_SECONDS") or 600)
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REL = f"https://github.com/{REPO}/releases/download/odds/{{}}" if REPO else None
S = requests.Session(); S.headers["User-Agent"] = "line-desk-odds (github actions)"
os.makedirs("out", exist_ok=True)
NOW = datetime.datetime.now(datetime.timezone.utc)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
pt = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
USAGE = {"remaining": None, "used": None, "spent_this_run": 0, "calls": 0}
T0 = time.time()

def call(path, **params):
    params["apiKey"] = KEY
    for k in range(4):
        r = S.get(API + path, params=params, timeout=60)
        if r.status_code == 429: time.sleep(3 * (k + 1)); continue
        USAGE["calls"] += 1
        for h, f in (("x-requests-remaining", "remaining"), ("x-requests-used", "used")):
            if r.headers.get(h) is not None: USAGE[f] = float(r.headers[h])
        if r.headers.get("x-requests-last"): USAGE["spent_this_run"] += float(r.headers["x-requests-last"])
        if r.status_code != 200: raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"{path}: rate limited")
def prev(name):
    if not REL: return None
    try:
        r = S.get(REL.format(name), timeout=120)
        return r.json() if r.status_code == 200 else None
    except Exception: return None

def outcomes(ev):
    """{book: {market: {outcome: (price, point)}}} from one event of an odds response."""
    out = {}
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            for o in m.get("outcomes", []):
                out.setdefault(b["key"], {}).setdefault(m["key"], {})[o["name"]] = (o.get("price"), o.get("point"))
    return out

# ---------- historical snapshots (cached per run) ----------
SNAP = {}
def snapshot(sport, t):
    """The snapshot at or before time t (5-minute grid). Returns (timestamp, {event_id: event})."""
    k = (sport, iso(t))
    if k not in SNAP:
        d = call(f"/historical/sports/{sport}/odds", date=iso(t), bookmakers=BOOKS, markets=MARKETS, oddsFormat="american", dateFormat="iso")
        SNAP[k] = (d.get("timestamp"), {e["id"]: e for e in d.get("data", [])})
    return SNAP[k]
def budget_ok():
    if USAGE["remaining"] is not None and USAGE["remaining"] < FLOOR: return False
    return USAGE["spent_this_run"] < CAP and time.time() - T0 < SECS
def first_time(sport, eid, lo, hi, pred):
    """Earliest snapshot in (lo, hi] where pred(event) holds, assuming it holds at hi. Bisection to 5 minutes.
    If it already holds at lo, returns lo's snapshot (the posting is at least that old; flagged by the caller)."""
    if not budget_ok(): return None
    ts, evs = snapshot(sport, lo)
    if eid in evs and pred(evs[eid]): return ts, evs[eid]
    while (hi - lo) > datetime.timedelta(minutes=5):
        if not budget_ok(): return None
        mid = lo + (hi - lo) / 2
        ts, evs = snapshot(sport, mid)
        if eid in evs and pred(evs[eid]): hi = mid
        else: lo = mid
    ts, evs = snapshot(sport, hi)
    return (ts, evs.get(eid)) if eid in evs else None

def backfill(sport, eid, E):
    """Find when each book first posted each market for this game, from historical snapshots."""
    commence = pt(E["commence"]); hi = pt(E["first_poll"]); lo = max(commence - LOOKBACK, datetime.datetime(2020, 6, 6, tzinfo=datetime.timezone.utc))
    r = first_time(sport, eid, lo, hi, lambda ev: bool(ev.get("bookmakers")))
    if not r: return False
    t_ev, ev0 = r; got = outcomes(ev0)
    if pt(t_ev) <= lo + datetime.timedelta(minutes=5): E["posted_before"] = iso(lo)     # already listed at the start of the search window
    for bk, mk_ in E["books"].items():
        for mk, outs in mk_.items():
            if outs.get("_src") != "pending": continue
            if bk in got and mk in got[bk]: when, vals = t_ev, got[bk][mk]
            else:
                rr = first_time(sport, eid, pt(t_ev), hi, lambda ev, bk=bk, mk=mk: mk in outcomes(ev).get(bk, {}))
                if rr is None:
                    if not budget_ok(): return False
                    outs["_src"] = "poll"; continue               # never posted before polling began: the poll is the first sighting
                when, ev1 = rr; vals = outcomes(ev1).get(bk, {}).get(mk, {})
            for name, (price, point) in vals.items():
                if name in outs: outs[name]["o"] = [price, point, when]
            outs["_src"] = "hist"
    E["backfilled"] = True
    return True

# ---------- poll ----------
report = {}
for tag, sport in SPORTS.items():
    st = prev(f"lines_{tag}.json") or {"events": {}}
    last_poll = st.get("polled")
    fresh_poll = bool(last_poll) and NOW - pt(last_poll) <= datetime.timedelta(minutes=30)
    try:
        evs = call(f"/sports/{sport}/events", dateFormat="iso")       # free
    except Exception as e:
        report[tag] = f"events failed: {e}"; continue
    soon = [e for e in evs if NOW < pt(e["commence_time"]) <= NOW + HORIZON]      # skip the paid call when nothing starts within 10 days
    E = st["events"]
    if soon:
        data = call(f"/sports/{sport}/odds", bookmakers=BOOKS, markets=MARKETS, oddsFormat="american", dateFormat="iso",
                    commenceTimeFrom=iso(NOW))      # every listed game (same cost): a game first listed weeks ahead is caught when it is posted
        for ev in data:
            if pt(ev["commence_time"]) <= NOW: continue               # never record in-play prices
            e = E.setdefault(ev["id"], {"home": ev["home_team"], "away": ev["away_team"], "commence": ev["commence_time"], "first_poll": iso(NOW),
                                        "books": {}, "backfilled": fresh_poll})
            e["commence"] = ev["commence_time"]
            for bk, mks in outcomes(ev).items():
                for mk, vals in mks.items():
                    outs = e["books"].setdefault(bk, {}).setdefault(mk, {})
                    new = not any(k for k in outs if k != "_src")
                    if new: outs["_src"] = "poll" if (fresh_poll or e.get("backfilled")) and last_poll else "pending"
                    for name, (price, point) in vals.items():
                        o = outs.setdefault(name, {"o": [price, point, iso(NOW)]})
                        o["c"] = [price, point, iso(NOW)]
    # backfill games that were already posted before polling started
    todo = [(eid, e) for eid, e in E.items() if not e.get("backfilled") and pt(e["commence"]) > NOW]
    todo.sort(key=lambda x: x[1]["commence"])
    done = 0
    for eid, e in todo:
        if not budget_ok(): break
        try:
            if backfill(sport, eid, e): done += 1
        except Exception as ex:
            report.setdefault(tag + "_errors", []).append(str(ex)[:200]); break
    # keep finished games 3 days (their last price is the closing line), drop older
    st["events"] = {k: v for k, v in E.items() if pt(v["commence"]) > NOW - datetime.timedelta(days=3)}
    st["polled"] = iso(NOW); st["sport"] = sport; st["books"] = BOOKS.split(",")
    st["note"] = "o = first price seen [price, point, time]; c = latest pre-game price. _src: hist = first posting found in historical snapshots; poll = first seen by the 15-minute poller; pending = not yet backfilled."
    json.dump(st, open(f"out/lines_{tag}.json", "w"), separators=(",", ":"))
    report[tag] = {"upcoming": len(soon), "tracked": len(st["events"]), "backfilled_now": done, "still_pending": len(todo) - done}

USAGE["polled"] = iso(NOW); USAGE["report"] = report
json.dump(USAGE, open("out/usage.json", "w"), indent=1)
print(json.dumps(USAGE, indent=1))
