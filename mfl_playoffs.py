#!/usr/bin/env python3
"""Weekly playoff-race post for an MFL league, sent to a Discord channel.

Shows standings, playoff odds (from simulating the rest of the season),
clinched / eliminated teams, magic numbers, and the week's biggest game.

Environment variables:
  DISCORD_PLAYOFF_WEBHOOK_URL  (required unless DRY_RUN=1)
  MFL_LEAGUE_ID      (default 24958)
  MFL_YEAR           (default 2026)
  MFL_HOST           (default www44.myfantasyleague.com)
  MFL_APIKEY         (optional, private leagues)
  PLAYOFF_TEAMS      number of playoff teams (blank = read from MFL, else 6)
  DIVISION_WINNERS_IN  "true" if division winners get automatic spots
  LAST_REGULAR_WEEK  last regular-season week (blank = read from MFL)
  DRY_RUN=1          print the message instead of posting
  FORCE=1            post even if this week was already posted
"""
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LEAGUE_ID = os.environ.get("MFL_LEAGUE_ID") or "24958"
YEAR = os.environ.get("MFL_YEAR") or "2026"
HOST = os.environ.get("MFL_HOST") or "www44.myfantasyleague.com"
APIKEY = (os.environ.get("MFL_APIKEY") or "").strip()
WEBHOOK = (os.environ.get("DISCORD_PLAYOFF_WEBHOOK_URL") or "").strip()
STATE_FILE = os.environ.get("PLAYOFF_STATE_FILE") or "playoff_state.json"
TRUTHY = ("1", "true", "yes")
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in TRUTHY
FORCE = (os.environ.get("FORCE") or "").lower() in TRUTHY
DIV_IN = (os.environ.get("DIVISION_WINNERS_IN") or "").lower() in TRUTHY
SEASON_GAMES = int(os.environ.get("SEASON_GAMES") or 0)  # regular-season games per team
SIMS = 10000
UA = "DyNastyTradeBot/1.0 (+https://github.com)"


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def num(x, default=0.0):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return default


def mfl(type_, required=True, **params):
    q = {"TYPE": type_, "L": LEAGUE_ID, "JSON": "1", **params}
    if APIKEY:
        q["APIKEY"] = APIKEY
    url = f"https://{HOST}/{YEAR}/export?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        if required:
            raise
        print(f"  (couldn't read {type_}: {e})")
        return {}
    if "error" in data:
        if required:
            raise RuntimeError(f"MFL error for {type_}: {data['error']}")
        return {}
    return data


# ---------- reading the league ----------

def read_league():
    league = mfl("league").get("league", {})
    teams = {}
    for f in as_list(league.get("franchises", {}).get("franchise")):
        teams[f["id"]] = {"name": f.get("name", f["id"]), "div": f.get("division")}
    divs = {d["id"]: d.get("name", d["id"])
            for d in as_list(league.get("divisions", {}).get("division")) if "id" in d}

    brackets = as_list(mfl("playoffBrackets", required=False)
                       .get("playoffBrackets", {}).get("playoffBracket"))
    main = brackets[0] if brackets else {}

    n = os.environ.get("PLAYOFF_TEAMS") or main.get("teamsInvolved") or "6"
    last = (os.environ.get("LAST_REGULAR_WEEK") or league.get("lastRegularSeasonWeek")
            or (int(num(main.get("startWeek"))) - 1 if main.get("startWeek") else None))
    return teams, divs, int(num(n, 6)), int(num(last, 0)) or None


def read_schedule(last_week):
    """Returns (played_games, remaining_games, last_complete_week)."""
    weeks = as_list(mfl("schedule").get("schedule", {}).get("weeklySchedule"))
    played, remaining, complete = [], [], 0
    for w in weeks:
        wk = int(num(w.get("week")))
        if last_week and wk > last_week:
            continue
        games = []
        for m in as_list(w.get("matchup")):
            fr = as_list(m.get("franchise"))
            if len(fr) == 2:
                games.append(fr)
        if not games:
            continue
        done = all(g[0].get("result") in ("W", "L", "T") for g in games)
        for a, b in games:
            if done:
                played.append({"week": wk, "a": a["id"], "b": b["id"], "ra": a["result"],
                               "sa": num(a.get("score")), "sb": num(b.get("score"))})
            else:
                remaining.append({"week": wk, "a": a["id"], "b": b["id"]})
        if done:
            complete = max(complete, wk)
    return played, remaining, complete


def build_records(teams, played):
    rec = {t: {"w": 0.0, "l": 0.0, "t": 0.0, "pf": 0.0, "scores": []} for t in teams}
    for g in played:
        for me, opp, my_s, res in ((g["a"], g["b"], g["sa"], g["ra"]),
                                   (g["b"], g["a"], g["sb"], {"W": "L", "L": "W", "T": "T"}[g["ra"]])):
            if me not in rec:
                continue
            r = rec[me]
            r["w" if res == "W" else "l" if res == "L" else "t"] += 1
            r["pf"] += my_s
            r["scores"].append(my_s)

    # Prefer MFL's official standings for W-L-T and points if available
    st = as_list(mfl("leagueStandings", required=False).get("leagueStandings", {}).get("franchise"))
    for f in st:
        r = rec.get(f.get("id"))
        if r is None or f.get("h2hw") is None:
            continue
        r["w"], r["l"], r["t"] = num(f.get("h2hw")), num(f.get("h2hl")), num(f.get("h2ht"))
        if f.get("pf") is not None:
            r["pf"] = num(f.get("pf"))
    return rec


# ---------- the math ----------

def wins(r):
    return r["w"] + 0.5 * r["t"]


def rank_key(r_w, r_pf):
    return (-r_w, -r_pf)  # tiebreaker: points for


def playoff_set(ws, pfs, teams, n, div_in):
    order = sorted(ws, key=lambda t: rank_key(ws[t], pfs[t]))
    if not div_in:
        return set(order[:n])
    chosen = set()
    seen_div = set()
    for t in order:
        d = teams[t]["div"]
        if d and d not in seen_div:
            seen_div.add(d)
            chosen.add(t)
    for t in order:
        if len(chosen) >= n:
            break
        chosen.add(t)
    return chosen


def simulate(teams, rec, remaining, n, div_in, next_week, extra=None):
    """extra = {team: number of games left that MFL's schedule doesn't list};
    those are simulated against a randomly chosen opponent."""
    extra = {t: k for t, k in (extra or {}).items() if k > 0}
    team_ids = list(teams)
    all_scores = [s for r in rec.values() for s in r["scores"]]
    lg_mean = sum(all_scores) / len(all_scores) if all_scores else 100.0
    lg_sd = (math.sqrt(sum((s - lg_mean) ** 2 for s in all_scores) / (len(all_scores) - 1))
             if len(all_scores) > 1 else 20.0)
    mean = {}
    for t, r in rec.items():
        k = len(r["scores"])
        tm = sum(r["scores"]) / k if k else lg_mean
        mean[t] = (k * tm + 5 * lg_mean) / (k + 5)  # shrink early-season numbers

    rng = random.Random(2026)
    made = {t: 0 for t in teams}
    nw_games = [g for g in remaining if g["week"] == next_week]
    # swing[(team, outcome)] = [times made playoffs, times that outcome happened]
    swing = {}
    for _ in range(SIMS):
        ws = {t: wins(r) for t, r in rec.items()}
        pfs = {t: r["pf"] for t, r in rec.items()}
        nw = {}
        for g in remaining:
            sa = rng.gauss(mean[g["a"]], lg_sd)
            sb = rng.gauss(mean[g["b"]], lg_sd)
            pfs[g["a"]] += sa
            pfs[g["b"]] += sb
            win = g["a"] if sa > sb else g["b"]
            ws[win] += 1
            if g["week"] == next_week:
                nw[g["a"]] = win == g["a"]
                nw[g["b"]] = win == g["b"]
        for t, k in extra.items():
            for _ in range(k):
                opp = rng.choice([o for o in team_ids if o != t])
                st = rng.gauss(mean[t], lg_sd)
                pfs[t] += st
                ws[t] += st > rng.gauss(mean[opp], lg_sd)
        ins = playoff_set(ws, pfs, teams, n, div_in)
        for t in ins:
            made[t] += 1
        for t, won in nw.items():
            s = swing.setdefault((t, won), [0, 0])
            s[1] += 1
            s[0] += t in ins

    odds = {t: made[t] / SIMS for t in teams}
    best = None
    for g in nw_games:
        parts = []
        total = 0.0
        for t in (g["a"], g["b"]):
            w = swing.get((t, True), [0, 0])
            lo = swing.get((t, False), [0, 0])
            pw = w[0] / w[1] if w[1] else odds[t]
            pl = lo[0] / lo[1] if lo[1] else odds[t]
            parts.append((t, pw, pl))
            total += abs(pw - pl)
        if best is None or total > best[0]:
            best = (total, parts)
    return odds, best


def games_left(teams, rec, remaining, season_games):
    """Games each team has left: the larger of what MFL's schedule lists and
    (season length - games played), so a schedule missing future weeks can
    never make a team look clinched or eliminated too early."""
    sched = {t: 0 for t in teams}
    for g in remaining:
        sched[g["a"]] += 1
        sched[g["b"]] += 1
    left = {}
    for t in teams:
        played = rec[t]["w"] + rec[t]["l"] + rec[t]["t"]
        by_count = max(0, season_games - int(played)) if season_games else 0
        left[t] = max(sched[t], by_count)
    return left, sched


def clinch_status(teams, rec, left, n, div_in):
    cur = {t: wins(rec[t]) for t in teams}
    mx = {t: cur[t] + left[t] for t in teams}
    n_divs = len({teams[t]["div"] for t in teams if teams[t]["div"]})
    use_div = div_in and n_divs > 0
    status = {}
    for t in teams:
        others = [o for o in teams if o != t]
        threats = sum(mx[o] >= cur[t] for o in others)
        ahead = sum(cur[o] > mx[t] for o in others)
        need = n - n_divs if use_div else n
        if threats < need:
            status[t] = "clinched"
        elif ahead >= n and (not use_div or any(
                teams[o]["div"] == teams[t]["div"] and cur[o] > mx[t] for o in others)):
            status[t] = "eliminated"
        else:
            status[t] = None
    return status, cur, mx


# ---------- message ----------

def fmt_rec(r):
    s = f"{int(r['w'])}-{int(r['l'])}"
    return s + (f"-{int(r['t'])}" if r["t"] else "")


def short(name, width):
    return name if len(name) <= width else name[:width - 1] + "…"


def build_message(teams, rec, weeks_left, n, div_in, week, odds, best, status, cur, mx):
    order = sorted(teams, key=lambda t: rank_key(cur[t], rec[t]["pf"]))
    in_now = playoff_set(cur, {t: rec[t]["pf"] for t in teams}, teams, n, div_in)
    seeded = [t for t in order if t in in_now] + [t for t in order if t not in in_now]
    first_out = next((t for t in seeded if t not in in_now), None)
    last_in = [t for t in seeded if t in in_now][-1] if in_now else None

    w = max(10, min(18, max(len(teams[t]["name"]) for t in teams)))
    lines = [f"{'#':>2}  {'Team':<{w}}  {'W-L':<6}{'PF':>7}  {'Odds':>4}  Note"]
    for i, t in enumerate(seeded, 1):
        r = rec[t]
        st = status[t]
        if st == "clinched":
            note = "CLINCHED"
        elif st == "eliminated":
            note = "OUT"
        elif t in in_now and first_out:
            magic = math.floor(mx[first_out] - cur[t]) + 1
            note = f"magic #{magic}"
        elif last_in:
            gb = ((cur[last_in] - cur[t]) + ((r["l"] + 0.5 * r["t"]) -
                  (rec[last_in]["l"] + 0.5 * rec[last_in]["t"]))) / 2
            note = f"{gb:g} GB" if gb > 0 else "tied, PF"
        else:
            note = ""
        pct = odds[t] * 100
        p = ">99%" if pct >= 99.5 else "<1%" if pct < 0.5 else f"{pct:.0f}%"
        if st == "clinched":
            p = "100%"
        if st == "eliminated":
            p = "0%"
        lines.append(f"{i:>2}  {short(teams[t]['name'], w):<{w}}  {fmt_rec(r):<6}"
                     f"{r['pf']:>7.1f}  {p:>4}  {note}")
        if i == len(in_now):
            lines.append("─" * (w + 32) + " playoff line")

    title = (f"🏆 **Playoff Race — after Week {week}**" if weeks_left else
             f"🏆 **Final Regular-Season Standings — Week {week}**")
    sub = (f"{weeks_left} week{'s' if weeks_left != 1 else ''} left · top {n} make the playoffs"
           if weeks_left else f"Top {n} made the playoffs")
    if div_in:
        sub += " · division winners get in"
    out = [title, sub, "```", *lines, "```"]

    cl = [teams[t]["name"] for t in seeded if status[t] == "clinched"]
    el = [teams[t]["name"] for t in seeded if status[t] == "eliminated"]
    if weeks_left:
        if cl:
            out.append("✅ **Clinched:** " + ", ".join(cl))
        if el:
            out.append("❌ **Eliminated:** " + ", ".join(el))
        if best and best[0] >= 0.05:
            (a, aw, al), (b, bw, bl) = best[1]
            out.append(f"🔥 **Game of the week:** {teams[a]['name']} vs {teams[b]['name']}\n"
                       f"  {teams[a]['name']}: {aw:.0%} with a win, {al:.0%} with a loss\n"
                       f"  {teams[b]['name']}: {bw:.0%} with a win, {bl:.0%} with a loss")
        out.append("-# Odds from 10,000 simulations of the remaining schedule based on each "
                   "team's scoring this season. Ties broken by points for. Magic # = wins by "
                   "that team plus losses by the first team out needed to clinch.")
    msg = "\n".join(out)
    return msg if len(msg) <= 2000 else msg[:1997] + "…"


# ---------- posting ----------

def post_discord(content):
    if DRY_RUN:
        print(content)
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(float(e.headers.get("Retry-After", "2")) + 1)
                continue
            raise


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    if not WEBHOOK and not DRY_RUN:
        sys.exit("DISCORD_PLAYOFF_WEBHOOK_URL is not set")

    teams, divs, n, last_week = read_league()
    div_in = DIV_IN and bool(divs or any(t["div"] for t in teams.values()))
    print(f"Settings: {len(teams)} teams, top {n} make playoffs, "
          f"last regular-season week {last_week or 'unknown'}, "
          f"division winners in: {'yes' if div_in else 'no'}")
    if DIV_IN and not div_in:
        print("  (DIVISION_WINNERS_IN is on but MFL lists no divisions; ignoring it)")

    played, remaining, week = read_schedule(last_week)
    if week == 0:
        print("No completed weeks yet; nothing to post.")
        return
    state = load_state()
    key = f"{YEAR}-{week}"
    if state.get("last_posted") == key and not FORCE:
        print(f"Week {week} already posted.")
        return
    rec = build_records(teams, played)
    season_games = SEASON_GAMES or last_week or 0
    left, sched = games_left(teams, rec, remaining, season_games)
    print(f"Season length: {season_games or 'unknown'} games · last completed week: {week} · "
          f"MFL schedule lists {len(remaining)} games still to play")
    for t in teams:
        r = rec[t]
        print(f"  {teams[t]['name']}: {fmt_rec(r)}, {left[t]} games left"
              + (f" (schedule only lists {sched[t]})" if sched[t] != left[t] else ""))
    if not season_games:
        print("  ! Season length unknown. Set LAST_REGULAR_WEEK in the workflow file.")

    weeks_left = max(left.values(), default=0)
    if not weeks_left and state.get("final_posted") == YEAR and not FORCE:
        print("Regular season is over and final standings were already posted.")
        return
    next_week = min((g["week"] for g in remaining), default=None)
    extra = {t: left[t] - sched[t] for t in teams}
    odds, best = simulate(teams, rec, remaining, n, div_in, next_week, extra)
    status, cur, mx = clinch_status(teams, rec, left, n, div_in)
    post_discord(build_message(teams, rec, weeks_left, n, div_in, week,
                               odds, best, status, cur, mx))

    if not DRY_RUN:
        state["last_posted"] = key
        if not weeks_left:
            state["final_posted"] = YEAR
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
    print(f"{'Previewed' if DRY_RUN else 'Posted'} playoff race after week {week}.")


if __name__ == "__main__":
    main()
