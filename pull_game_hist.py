"""Historical game lines (moneyline, spread, total) from The Odds API, for testing our numbers against OPENERS and the line path,
not only the close. Runs on GitHub Actions (game_hist.yml). One call returns every game on the board at that moment, so it walks
a time grid through each season:
  every 4 hours from Aug 20 to mid-Feb (the opener is the first snapshot a book shows a game; weekly openers post Sun/Mon),
  plus one snapshot 10 minutes before every distinct kickoff time (the close).
Only changes are stored: lines[book][market] = [[ts, point, price_a, price_b], ...]
  h2h: point None, price_a = home, price_b = away | spreads: point = HOME spread, prices home/away | totals: point = total, over/under.
Cost: 30 credits per snapshot (3 markets x 1 region of 10 books), about 30k credits per NFL season.
Resumable: reads game_hist_<sport>.json.gz from this repo's release "gamehist" and skips timestamps it already pulled.
The API key is read from the ODDS_API_KEY secret and never written anywhere.
Env: ODDS_API_KEY (required), SPORTS (default americanfootball_nfl; add americanfootball_ncaaf for CFB), SEASONS (default 2020-2026),
     STEP_HOURS (default 4), CREDIT_FLOOR (default 200000), RUN_CREDIT_CAP (default 400000)."""
import os, json, gzip, time, datetime
import requests

KEY = os.environ["ODDS_API_KEY"]
API = "https://api.the-odds-api.com/v4"
SPORTS = (os.environ.get("SPORTS") or "americanfootball_nfl").split(",")
BOOKS = "draftkings,fanduel,betmgm,williamhill_us,fanatics,betrivers,pinnacle,bovada,betonlineag,lowvig"
MARKETS = "h2h,spreads,totals"
SEASONS = [int(s) for s in (os.environ.get("SEASONS") or "2020,2021,2022,2023,2024,2025,2026").split(",")]
STEP = int(os.environ.get("STEP_HOURS") or 4)
FLOOR = int(os.environ.get("CREDIT_FLOOR") or 200000); CAP = int(os.environ.get("RUN_CREDIT_CAP") or 400000)
REPO = os.environ.get("GITHUB_REPOSITORY", "")
S = requests.Session(); S.headers["User-Agent"] = "line-desk-game-history (github actions)"
UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
pt = lambda s: datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
U = {"remaining": None, "spent": 0, "calls": 0}
os.makedirs("out", exist_ok=True)


def call(path, **params):
    params["apiKey"] = KEY
    for k in range(6):
        try:
            r = S.get(API + path, params=params, timeout=90)
        except requests.RequestException:
            time.sleep(5 * (k + 1)); continue
        if r.status_code == 429: time.sleep(4 * (k + 1)); continue
        U["calls"] += 1
        if r.headers.get("x-requests-remaining") is not None: U["remaining"] = float(r.headers["x-requests-remaining"])
        if r.headers.get("x-requests-last"): U["spent"] += float(r.headers["x-requests-last"])
        if r.status_code in (404, 422): return None
        if r.status_code != 200: raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"{path}: gave up after retries")


def ok(): return (U["remaining"] is None or U["remaining"] > FLOOR) and U["spent"] < CAP


def load(sport):
    if REPO:
        try:
            r = S.get(f"https://github.com/{REPO}/releases/download/gamehist/game_hist_{sport}.json.gz", timeout=120)
            if r.status_code == 200: return json.loads(gzip.decompress(r.content))
        except Exception as e: print("no previous file:", e)
    return {"events": {}, "done": []}


def save(sport, DB):
    DB["updated"] = iso(datetime.datetime.now(UTC)); DB["usage"] = U
    DB["note"] = ("events[id] = {home, away, commence, lines: {book: {market: [[ts, point, a, b], ...]}}} (changes only). "
                  "h2h: a=home price, b=away price; spreads: point=home spread, a=home, b=away; totals: point=total, a=over, b=under. "
                  "ts = snapshot time (Odds API historical snapshot the request resolved to). done = requested grid times already pulled.")
    open(f"out/game_hist_{sport}.json.gz", "wb").write(gzip.compress(json.dumps(DB, separators=(",", ":")).encode()))


def ingest(DB, snap):
    ts = snap.get("timestamp")
    for ev in snap.get("data") or []:
        e = DB["events"].setdefault(ev["id"], {"home": ev["home_team"], "away": ev["away_team"], "commence": ev["commence_time"], "lines": {}})
        e["commence"] = ev["commence_time"]                      # kickoff can be moved (flex): keep the latest
        if pt(ev["commence_time"]) <= pt(ts): continue           # in-play lines are not pregame lines
        for bk in ev.get("bookmakers") or []:
            for m in bk.get("markets") or []:
                oc = {o["name"]: o for o in m.get("outcomes") or []}
                if m["key"] == "totals":
                    o, u = oc.get("Over"), oc.get("Under")
                    if not (o and u): continue
                    row = [ts, o.get("point"), o.get("price"), u.get("price")]
                else:
                    h, a = oc.get(ev["home_team"]), oc.get(ev["away_team"])
                    if not (h and a): continue
                    row = [ts, h.get("point") if m["key"] == "spreads" else None, h.get("price"), a.get("price")]
                L = e["lines"].setdefault(bk["key"], {}).setdefault(m["key"], [])
                if L and L[-1][1:] == row[1:]: continue          # unchanged since the previous snapshot
                if L and L[-1][0] >= ts: continue                # already have this or a later snapshot
                L.append(row)


def run(sport):
    DB = load(sport); done = set(DB.get("done", []))
    print(sport, "already have", len(DB["events"]), "games,", len(done), "snapshots")
    grid = []
    for s in SEASONS:
        t = datetime.datetime(s, 8, 20, tzinfo=UTC); end = min(datetime.datetime(s + 1, 2, 16, tzinfo=UTC), NOW)
        while t < end: grid.append(t); t += datetime.timedelta(hours=STEP)
    n = 0
    for t in grid:
        k = iso(t)
        if k in done: continue
        if not ok(): print("credit floor/cap reached"); break
        d = call(f"/historical/sports/{sport}/odds", date=k, bookmakers=BOOKS, markets=MARKETS, oddsFormat="american", dateFormat="iso")
        if d: ingest(DB, d)
        done.add(k); n += 1
        if n % 100 == 0:
            DB["done"] = sorted(done); save(sport, DB); print(" ", k, "snapshots this run", n, "credits spent", U["spent"], "remaining", U["remaining"], flush=True)
    # closes: 10 minutes before every distinct kickoff time of games we know about
    kicks = sorted({e["commence"] for e in DB["events"].values() if pt(e["commence"]) < NOW and pt(e["commence"]).year >= min(SEASONS)})
    for c in kicks:
        k = iso(pt(c) - datetime.timedelta(minutes=10))
        if k in done: continue
        if not ok(): print("credit floor/cap reached"); break
        d = call(f"/historical/sports/{sport}/odds", date=k, bookmakers=BOOKS, markets=MARKETS, oddsFormat="american", dateFormat="iso")
        if d: ingest(DB, d)
        done.add(k); n += 1
        if n % 100 == 0: DB["done"] = sorted(done); save(sport, DB)
    DB["done"] = sorted(done); save(sport, DB)
    print(sport, "done:", len(DB["events"]), "games, snapshots this run", n, "credits spent", U["spent"], "remaining", U["remaining"])


for sp in SPORTS: run(sp)
