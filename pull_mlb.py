"""Daily MLB play-by-play and pitch pull from the MLB Stats API, for the MLB Line Desk.

Runs on GitHub Actions (see .github/workflows/pull.yml). Publishes to this repo's release "data":
  mlb_pbp_{SEASON}_supp.parquet     one row per plate appearance (same columns as sportsdataverse mlb_pbp)
  mlb_pitches_{SEASON}_supp.parquet one row per pitch (same columns as sportsdataverse mlb_pitches)
  mlb_games_{SEASON}_supp.parquet   one row per final game: date, type, teams, score, starters
  mlb_hands_{SEASON}_supp.parquet   bats / throws for every player seen
  validate.json                     re-pulls games sportsdataverse already has and compares them row by row;
                                    the desk only uses this data when validate.json says ok
Env: SEASON (default: this year), SINCE (first date to pull, default March 1 of SEASON), VALIDATE_N (default 15)."""
import os, io, json, time, random, datetime
import requests, pandas as pd

API = "https://statsapi.mlb.com"
SDV = "https://github.com/sportsdataverse/sportsdataverse-data/releases/download/{}"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
REL = f"https://github.com/{REPO}/releases/download/data/{{}}" if REPO else None
TODAY = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=4)).date()   # US Eastern (EDT) date
SEASON = int(os.environ.get("SEASON") or TODAY.year)
SINCE = os.environ.get("SINCE") or f"{SEASON}-03-01"
VALIDATE_N = int(os.environ.get("VALIDATE_N") or 15)
TYPES = {"R", "F", "D", "L", "W"}          # regular season and all postseason rounds
S = requests.Session(); S.headers["User-Agent"] = "mlb-line-desk-data (github actions)"
os.makedirs("out", exist_ok=True)

def getj(url, tries=5):
    for k in range(tries):
        try:
            r = S.get(url, timeout=60)
            if r.status_code == 200: return r.json()
            if r.status_code == 404: return None
        except Exception as e: err = e
        time.sleep(2 * (k + 1))
    raise RuntimeError(f"failed: {url}")
def prev(name):
    if not REL: return None
    try:
        r = S.get(REL.format(name), timeout=120)
        return pd.read_parquet(io.BytesIO(r.content)) if r.status_code == 200 else None
    except Exception: return None
def g(o, *ks):
    for k in ks:
        if not isinstance(o, dict): return None
        o = o.get(k)
    return o

def parse(pk):
    """One game's live feed -> (pa rows, pitch rows, game row, hands)."""
    f = getj(f"{API}/api/v1.1/game/{pk}/feed/live")
    if not f: return [], [], None, {}
    gd, ld = f.get("gameData", {}), f.get("liveData", {})
    pas, pis, hands = [], [], {}
    for p in g(ld, "plays", "allPlays") or []:
        a, r, m = p.get("about", {}), p.get("result", {}), p.get("matchup", {})
        if not a.get("isComplete", True) or r.get("type") != "atBat": continue
        b, pt = g(m, "batter", "id"), g(m, "pitcher", "id")
        bs, ph = g(m, "batSide", "code"), g(m, "pitchHand", "code")
        if b and bs: hands.setdefault(("b", b), set()).add(bs)
        if pt and ph: hands.setdefault(("p", pt), set()).add(ph)
        pas.append(dict(game_pk=pk, at_bat_index=a.get("atBatIndex"), inning=a.get("inning"), half_inning=a.get("halfInning"), batter_id=b, pitcher_id=pt,
                        event_type=r.get("eventType"), event=r.get("event"), away_score=r.get("awayScore"), home_score=r.get("homeScore"),
                        start_time=a.get("startTime"), end_time=a.get("endTime"), outs=g(p, "count", "outs")))
        for e in p.get("playEvents") or []:
            if not e.get("isPitch"): continue
            d, pd_, hd = e.get("details", {}), e.get("pitchData", {}) or {}, e.get("hitData", {}) or {}
            pis.append(dict(game_pk=pk, at_bat_index=a.get("atBatIndex"), pitch_number=e.get("pitchNumber"), batter_id=b, pitcher_id=pt,
                            pitch_type=g(d, "type", "code"), pitch_name=g(d, "type", "description"), call_code=g(d, "call", "code"),
                            call_description=g(d, "call", "description"), balls=g(e, "count", "balls"), strikes=g(e, "count", "strikes"),
                            outs=g(e, "count", "outs"), start_speed=pd_.get("startSpeed"), end_speed=pd_.get("endSpeed"),
                            spin_rate=g(pd_, "breaks", "spinRate"), extension=pd_.get("extension"), px=g(pd_, "coordinates", "pX"),
                            pz=g(pd_, "coordinates", "pZ"), sz_top=pd_.get("strikeZoneTop"), sz_bot=pd_.get("strikeZoneBottom"),
                            launch_speed=hd.get("launchSpeed"), launch_angle=hd.get("launchAngle"), total_distance=hd.get("totalDistance"),
                            trajectory=hd.get("trajectory"), hardness=hd.get("hardness")))
    top = [x for x in pas if x["half_inning"] == "top"]; bot = [x for x in pas if x["half_inning"] == "bottom"]
    game = dict(game_pk=pk, date=g(gd, "datetime", "officialDate"), game_type=g(gd, "game", "type"), status=g(gd, "status", "detailedState"),
                away=g(gd, "teams", "away", "abbreviation"), home=g(gd, "teams", "home", "abbreviation"),
                away_score=g(ld, "linescore", "teams", "away", "runs"), home_score=g(ld, "linescore", "teams", "home", "runs"),
                home_sp=top[0]["pitcher_id"] if top else None, away_sp=bot[0]["pitcher_id"] if bot else None, n_pa=len(pas))
    return pas, pis, game, hands

def final_games(start, end):
    s = getj(f"{API}/api/v1/schedule?sportId=1&startDate={start}&endDate={end}")
    out = []
    for d in (s or {}).get("dates", []):
        for x in d.get("games", []):
            if x.get("gameType") in TYPES and g(x, "status", "codedGameState") in ("F", "O"): out.append(int(x["gamePk"]))
    return sorted(set(out))

def pull(pks):
    PA, PI, GM, H = [], [], [], {}
    for i, pk in enumerate(pks):
        a, b, c, h = parse(pk)
        if c and a: PA += a; PI += b; GM.append(c)
        for k, v in h.items(): H.setdefault(k, set()).update(v)
        if i % 25 == 0: print(f"  {i + 1}/{len(pks)}", flush=True)
        time.sleep(0.25)
    return pd.DataFrame(PA), pd.DataFrame(PI), pd.DataFrame(GM), H

# ---------- 1. incremental pull ----------
yday = (TODAY - datetime.timedelta(days=1)).isoformat()
pks = final_games(SINCE, TODAY.isoformat())
OLD = {k: prev(f"mlb_{k}_{SEASON}_supp.parquet") for k in ("pbp", "pitches", "games")}
done = set(OLD["games"].game_pk) if OLD["games"] is not None else set()
recent = set(OLD["games"][OLD["games"].date >= (TODAY - datetime.timedelta(days=2)).isoformat()].game_pk) if OLD["games"] is not None else set()
todo = [p for p in pks if p not in done or p in recent]      # re-pull the last two days in case of scoring changes
print(f"{len(pks)} final games since {SINCE}; pulling {len(todo)}", flush=True)
pa, pi, gm, H = pull(todo)
def merge(old, new, keys):
    if old is None or not len(old): return new
    if new is None or not len(new): return old
    return pd.concat([old[~old.game_pk.isin(set(new.game_pk))], new]).sort_values(keys).reset_index(drop=True)
PA = merge(OLD["pbp"], pa, ["game_pk", "at_bat_index"]); PI_ = merge(OLD["pitches"], pi, ["game_pk", "at_bat_index", "pitch_number"])
GM = merge(OLD["games"], gm, ["date", "game_pk"])
oh = prev(f"mlb_hands_{SEASON}_supp.parquet"); HD = {}
if oh is not None:
    for r in oh.itertuples(): HD[int(r.id)] = [r.bats or "", r.throws or ""]
for (kind, pid), v in H.items():
    e = HD.setdefault(int(pid), ["", ""])
    if kind == "b":   # batSide is the side he batted from in that plate appearance: both seen = switch hitter
        seen = set(v) | ({e[0]} if e[0] in ("L", "R") else set())
        e[0] = "B" if (e[0] == "B" or len(seen) > 1) else next(iter(seen))
    else:
        e[1] = next(iter(v)) if len(v) == 1 else (e[1] or sorted(v)[0])
HAND = pd.DataFrame([dict(id=k, bats=v[0], throws=v[1]) for k, v in HD.items()])

# ---------- 2. validation against sportsdataverse (games both sources have) ----------
V = {"season": SEASON, "ok": False}
try:
    sp = pd.read_parquet(io.BytesIO(S.get(SDV.format(f"mlb_pbp/mlb_pbp_{SEASON}.parquet"), timeout=600).content),
                         columns=["game_pk", "at_bat_index", "half_inning", "inning", "batter_id", "pitcher_id", "event_type", "away_score", "home_score"])
    spi = pd.read_parquet(io.BytesIO(S.get(SDV.format(f"mlb_pitches/mlb_pitches_{SEASON}.parquet"), timeout=600).content),
                          columns=["game_pk", "at_bat_index", "call_code", "launch_speed", "start_speed"])
    cand = sorted(set(sp.game_pk)); random.seed(SEASON); vpk = random.sample(cand[-300:], min(VALIDATE_N, len(cand)))
    a, b, _, _ = pull(vpk)
    m = sp[sp.game_pk.isin(vpk)].merge(a, on=["game_pk", "at_bat_index"], how="outer", suffixes=("_s", "_a"), indicator=True)
    both = m[m._merge == "both"]
    ag = lambda c: float((both[c + "_s"].astype(str) == both[c + "_a"].astype(str)).mean()) if len(both) else 0.0
    V.update(validation_games=len(vpk), pa_sdv=int((m._merge != "right_only").sum()), pa_api=int((m._merge != "left_only").sum()), pa_both=int(len(both)),
             batter=ag("batter_id"), pitcher=ag("pitcher_id"), event_type=ag("event_type"), half_inning=ag("half_inning"),
             home_score=float((both.home_score_s.astype(float) == both.home_score_a.astype(float)).mean()) if len(both) else 0.0)
    ps = spi[spi.game_pk.isin(vpk)].groupby(["game_pk", "at_bat_index"]).size(); pa_ = b.groupby(["game_pk", "at_bat_index"]).size()
    j = pd.concat([ps.rename("s"), pa_.rename("a")], axis=1).dropna(); V["pitches_per_pa"] = float((j.s == j.a).mean()) if len(j) else 0.0
    xs = spi[spi.game_pk.isin(vpk) & spi.call_code.isin(["X", "D", "E"])].groupby(["game_pk", "at_bat_index"]).launch_speed.last()
    xa = b[b.call_code.isin(["X", "D", "E"])].groupby(["game_pk", "at_bat_index"]).launch_speed.last()
    k = pd.concat([xs.rename("s"), xa.rename("a")], axis=1).dropna(); V["launch_speed"] = float(((k.s - k.a).abs() < 0.15).mean()) if len(k) else 0.0
    cover = V["pa_both"] / max(1, max(V["pa_sdv"], V["pa_api"]))
    V["coverage"] = cover
    V["ok"] = bool(cover >= 0.99 and min(V["batter"], V["pitcher"], V["event_type"], V["half_inning"]) >= 0.99 and V["pitches_per_pa"] >= 0.97 and V["launch_speed"] >= 0.95)
except Exception as e:
    V["error"] = str(e)[:500]
V.update(pulled_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), supp_games=int(len(GM)), supp_first=str(GM.date.min()) if len(GM) else None, supp_last=str(GM.date.max()) if len(GM) else None)
print(json.dumps(V, indent=1))

# ---------- 3. write ----------
PA.to_parquet(f"out/mlb_pbp_{SEASON}_supp.parquet", index=False); PI_.to_parquet(f"out/mlb_pitches_{SEASON}_supp.parquet", index=False)
GM.to_parquet(f"out/mlb_games_{SEASON}_supp.parquet", index=False); HAND.to_parquet(f"out/mlb_hands_{SEASON}_supp.parquet", index=False)
json.dump(V, open("out/validate.json", "w"), indent=1)
open("last_run.txt", "w").write(f"{V['pulled_at']} {len(GM)} games through {V['supp_last']} ok={V['ok']}\n")
