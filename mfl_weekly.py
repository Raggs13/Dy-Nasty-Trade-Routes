#!/usr/bin/env python3
"""Tuesday 10 AM (Eastern) posts: Matchup Recap and Power Rankings.

Runs inside the trade bot every 10 minutes, but only does anything on the
first run at or after Tuesday 10:00 AM Eastern each week (one MFL check per
week otherwise costs nothing).

Environment variables:
  DISCORD_RECAP_WEBHOOK_URL   channel for the recap (required unless DRY_RUN=1)
  DISCORD_POWER_WEBHOOK_URL   channel for power rankings (blank = same as recap)
  MFL_LEAGUE_ID / MFL_YEAR / MFL_HOST / MFL_APIKEY
  LAST_REGULAR_WEEK  power rankings stop after this week (default 14)
  WEEK=N        recap a specific week (for previews)
  DRY_RUN=1     print instead of posting; ignores the Tuesday timer
  FORCE=1       post now, even if it's not Tuesday or this week was posted
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

LEAGUE_ID = os.environ.get("MFL_LEAGUE_ID") or "24958"
YEAR = os.environ.get("MFL_YEAR") or "2026"
HOST = os.environ.get("MFL_HOST") or "www44.myfantasyleague.com"
APIKEY = (os.environ.get("MFL_APIKEY") or "").strip()
RECAP_HOOK = (os.environ.get("DISCORD_RECAP_WEBHOOK_URL") or "").strip()
POWER_HOOK = (os.environ.get("DISCORD_POWER_WEBHOOK_URL") or "").strip() or RECAP_HOOK
LAST_REG = int(os.environ.get("LAST_REGULAR_WEEK") or 14)
WEEK_OVERRIDE = int(os.environ.get("WEEK") or 0)
STATE_FILE = os.environ.get("WEEKLY_STATE_FILE") or "weekly_state.json"
TRUTHY = ("1", "true", "yes")
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in TRUTHY
FORCE = (os.environ.get("FORCE") or "").lower() in TRUTHY
POST_HOUR = 10  # 10 AM Eastern
ET = ZoneInfo("America/New_York")
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
    """Fetch from MFL, pausing between calls and retrying if MFL pushes back."""
    q = {"TYPE": type_, "L": LEAGUE_ID, "JSON": "1", **params}
    if APIKEY:
        q["APIKEY"] = APIKEY
    url = f"https://{HOST}/{YEAR}/export?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    problem = None
    for attempt in range(4):
        time.sleep(1 if attempt == 0 else 4 * attempt)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "error" not in data:
                return data
            problem = data["error"]
        except Exception as e:
            problem = e
    print(f"  ! MFL {type_} {params or ''} failed after retries: {problem}")
    if required:
        raise RuntimeError(f"MFL error for {type_}: {problem}")
    return {}


# ---------- the Tuesday timer ----------

def current_cycle(now=None):
    """Date of the most recent Tuesday 10 AM Eastern at or before now."""
    now = now or dt.datetime.now(ET)
    days = (now.weekday() - 1) % 7  # Tuesday = 1
    tue = (now - dt.timedelta(days=days)).date()
    if days == 0 and now.hour < POST_HOUR:
        tue -= dt.timedelta(days=7)
    return tue.isoformat()


# ---------- reading results ----------

def read_week(w):
    """List of matchups [(teamA, teamB)] with each team dict, or None if not finished."""
    data = mfl("weeklyResults", required=False, W=str(w)).get("weeklyResults", {})
    games = []
    for m in as_list(data.get("matchup")):
        fr = as_list(m.get("franchise"))
        if len(fr) != 2:
            continue
        teams = []
        for f in fr:
            players = []
            for p in as_list(f.get("player")):
                players.append({"id": p.get("id"), "score": num(p.get("score")),
                                "starter": (p.get("status") or "").lower() == "starter",
                                "should": p.get("shouldStart") == "1"})
            teams.append({"id": f.get("id"), "score": num(f.get("score")),
                          "result": f.get("result"),
                          "opt": num(f.get("opt_pts"), None) if f.get("opt_pts") not in (None, "") else None,
                          "players": players})
        games.append(tuple(teams))
    if not games:
        return None
    missing = [t["id"] for g in games for t in g if t["result"] not in ("W", "L", "T")]
    scored = any(t["score"] > 0 for g in games for t in g)
    if missing or not scored:
        if scored:  # only worth explaining for a week that has been played
            print(f"  Week {w}: no final result yet for team(s) {', '.join(missing)}")
        return None
    return games


def games_played():
    """Most games any team has played, per MFL's official standings (0 if unknown)."""
    st = as_list(mfl("leagueStandings", required=False).get("leagueStandings", {}).get("franchise"))
    best = 0
    for f in st:
        if f.get("h2hw") is not None:
            best = max(best, int(num(f.get("h2hw")) + num(f.get("h2hl")) + num(f.get("h2ht"))))
    return best


def read_season(upto=None):
    """{week: games} for every finished week (stops at the first unfinished one)."""
    official = games_played()
    if official:
        print(f"MFL standings show {official} games played.")
    weeks = {}
    for w in range(1, 23):
        if upto and w > upto:
            break
        g = read_week(w)
        if g is None and official and w <= official:
            print(f"  Week {w} didn't look finished on the first try; asking MFL again.")
            time.sleep(5)
            g = read_week(w)
        if g is None:
            if official and w <= official:
                print(f"  ! Week {w} is finished per the standings, but MFL's week results "
                      f"aren't complete yet; stopping at week {w - 1}.")
            break
        weeks[w] = g
    return weeks


def load_names(weeks):
    league = mfl("league").get("league", {})
    teams = {f["id"]: f.get("name", f["id"])
             for f in as_list(league.get("franchises", {}).get("franchise"))}
    pids = sorted({p["id"] for g in weeks.values() for m in g for t in m for p in t["players"] if p["id"]})
    players = {}
    for i in range(0, len(pids), 150):
        data = mfl("players", PLAYERS=",".join(pids[i:i + 150]))
        for p in as_list(data.get("players", {}).get("player")):
            name = p.get("name", p.get("id"))
            if ", " in name:
                last, first = name.split(", ", 1)
                name = f"{first} {last}"
            players[p["id"]] = {"name": name, "pos": p.get("position", ""), "team": p.get("team", "")}
    return teams, players


# ---------- recap ----------

def bench_loss(t, players):
    """(points left on the bench, best benched player id, starter they'd replace)."""
    starters = [p for p in t["players"] if p["starter"]]
    bench = [p for p in t["players"] if not p["starter"]]
    # Best single same-position swap (used for the "who" and as a fallback number)
    best = (0.0, None, None)
    for b in bench:
        pos = players.get(b["id"], {}).get("pos")
        for s in starters:
            if pos and players.get(s["id"], {}).get("pos") == pos and b["score"] - s["score"] > best[0]:
                best = (b["score"] - s["score"], b["id"], s["id"])
    if t["opt"] is not None and t["opt"] >= t["score"]:
        return (t["opt"] - t["score"], best[1], best[2])
    return best


def pname(pid, players):
    p = players.get(pid, {})
    return p.get("name", f"Player #{pid}")


def recap_message(week, games, teams, players):
    nm = lambda i: teams.get(i, i)
    lines = [f"📰 **Week {week} Recap**", ""]
    ordered = sorted(games, key=lambda g: -max(g[0]["score"], g[1]["score"]))
    for a, b in ordered:
        w, l = (a, b) if a["score"] >= b["score"] else (b, a)
        if w["score"] == l["score"]:
            lines.append(f"🤝 **{nm(w['id'])}** {w['score']:.2f} tied {nm(l['id'])} {l['score']:.2f}")
        else:
            lines.append(f"**{nm(w['id'])}** {w['score']:.2f} def. {nm(l['id'])} {l['score']:.2f}")
    lines.append("")

    all_t = [t for g in games for t in g]
    top = max(all_t, key=lambda t: t["score"])
    low = min(all_t, key=lambda t: t["score"])
    margins = [(abs(a["score"] - b["score"]), a, b) for a, b in games]
    blow = max(margins, key=lambda x: x[0])
    close = min(margins, key=lambda x: x[0])
    lines.append(f"🔥 **Top score:** {nm(top['id'])} — {top['score']:.2f}")
    lines.append(f"🧊 **Low score:** {nm(low['id'])} — {low['score']:.2f}")
    bw, bl = sorted(blow[1:], key=lambda t: -t["score"])
    lines.append(f"💥 **Biggest blowout:** {nm(bw['id'])} over {nm(bl['id'])} by {blow[0]:.2f}")
    cw, cl = sorted(close[1:], key=lambda t: -t["score"])
    lines.append(f"😬 **Closest game:** {nm(cw['id'])} over {nm(cl['id'])} by {close[0]:.2f}")

    # Unluckiest: highest score that still lost
    losers = [t for t in all_t if t["result"] == "L"]
    if losers:
        ul = max(losers, key=lambda t: t["score"])
        rank = sorted((t["score"] for t in all_t), reverse=True).index(ul["score"]) + 1
        if rank <= len(all_t) // 2:
            lines.append(f"🍀 **Tough luck:** {nm(ul['id'])} scored the #{rank} total of the week "
                         f"({ul['score']:.2f}) and still lost")

    # Player of the week (starters only)
    starters = [(p, t["id"]) for t in all_t for p in t["players"] if p["starter"]]
    if starters:
        p, owner = max(starters, key=lambda x: x[0]["score"])
        info = players.get(p["id"], {})
        tag = ", ".join(x for x in (info.get("pos"), info.get("team")) if x)
        lines.append(f"⭐ **Player of the week:** {pname(p['id'], players)}"
                     + (f" ({tag})" if tag else "") + f" — {p['score']:.2f} for {nm(owner)}")

    # Oops of the Week: most points left on the bench
    opp = {}
    for a, b in games:
        opp[a["id"]], opp[b["id"]] = b, a
    oops = max(((bench_loss(t, players), t) for t in all_t), key=lambda x: x[0][0])
    (lost, bpid, spid), t = oops
    if lost >= 1:
        line = f"🤦 **Oops of the Week:** {nm(t['id'])} left {lost:.2f} points on the bench"
        if bpid and spid:
            line += (f" ({pname(bpid, players)} scored {next(p['score'] for p in t['players'] if p['id'] == bpid):.2f}"
                     f" on the bench while {pname(spid, players)} started)")
        o = opp.get(t["id"])
        if t["result"] == "L" and o and t["score"] + lost > o["score"]:
            line += " — **the right lineup would have won**"
        lines.append(line)

    msg = "\n".join(lines)
    return msg if len(msg) <= 2000 else msg[:1997] + "…"


# ---------- power rankings ----------

def power_table(weeks, upto):
    """Rank teams using games through `upto`. Returns list of dicts, best first."""
    stats = {}
    for w in sorted(weeks):
        if w > upto:
            break
        scores = [t for g in weeks[w] for t in g]
        for g in weeks[w]:
            for t in g:
                s = stats.setdefault(t["id"], {"w": 0, "l": 0, "t": 0, "apw": 0, "apl": 0,
                                               "pts": [], "results": []})
                s[{"W": "w", "L": "l", "T": "t"}[t["result"]]] += 1
                s["results"].append(t["result"])
                s["pts"].append(t["score"])
                for o in scores:
                    if o is t:
                        continue
                    if t["score"] > o["score"]:
                        s["apw"] += 1
                    elif t["score"] < o["score"]:
                        s["apl"] += 1
                    else:
                        s["apw"] += 0.5
                        s["apl"] += 0.5
    if not stats:
        return []
    for s in stats.values():
        g = len(s["pts"])
        s["ppg"] = sum(s["pts"]) / g
        s["recent"] = sum(s["pts"][-3:]) / len(s["pts"][-3:])
        s["ap_pct"] = s["apw"] / max(1, s["apw"] + s["apl"])
        s["win_pct"] = (s["w"] + 0.5 * s["t"]) / g

    def norm(key):
        vals = [s[key] for s in stats.values()]
        lo, hi = min(vals), max(vals)
        return {t: (s[key] - lo) / (hi - lo) if hi > lo else 0.5 for t, s in stats.items()}

    ppg, recent = norm("ppg"), norm("recent")
    for t, s in stats.items():
        s["power"] = 0.40 * s["ap_pct"] + 0.30 * ppg[t] + 0.20 * recent[t] + 0.10 * s["win_pct"]
        s["id"] = t
    return sorted(stats.values(), key=lambda s: -s["power"])


def streak(results):
    if not results:
        return ""
    last, n = results[-1], 0
    for r in reversed(results):
        if r != last:
            break
        n += 1
    return f"{last}{n}" if n >= 3 else ""


def power_message(week, weeks, teams):
    now = power_table(weeks, week)
    before = {s["id"]: i for i, s in enumerate(power_table(weeks, week - 1), 1)} if week > 1 else {}
    w = max(10, min(18, max(len(teams.get(s["id"], s["id"])) for s in now)))
    aps = [f"{s['apw']:g}-{s['apl']:g}" for s in now]
    apw = max(8, max(len(a) for a in aps))
    rows = [f"{'#':>2} {'':3} {'Team':<{w}}  {'W-L':<5} {'All-Play':<{apw}} {'PPG':>6}  Note"]
    for i, s in enumerate(now, 1):
        prev = before.get(s["id"])
        move = "  —" if prev is None or prev == i else (f"▲{prev - i:<2}" if prev > i else f"▼{i - prev:<2}")
        rec = f"{s['w']}-{s['l']}" + (f"-{s['t']}" if s["t"] else "")
        ap = f"{s['apw']:g}-{s['apl']:g}"
        gap = s["win_pct"] - s["ap_pct"]
        notes = []
        if gap <= -0.15:
            notes.append("unlucky")
        elif gap >= 0.15:
            notes.append("lucky")
        st = streak(s["results"])
        if st:
            notes.append(("won " if st[0] == "W" else "lost ") + st[1:] + " straight")
        if s["recent"] == max(x["recent"] for x in now):
            notes.append("hottest")
        name = teams.get(s["id"], s["id"])
        name = name if len(name) <= w else name[:w - 1] + "…"
        rows.append(f"{i:>2} {move:3} {name:<{w}}  {rec:<5} {ap:<{apw}} {s['ppg']:>6.1f}  {', '.join(notes)}".rstrip())
    out = [f"📊 **Power Rankings — Week {week}**", "```", *rows, "```",
           "-# Ranked on all-play record (40%), points per game (30%), last 3 weeks (20%) "
           "and actual record (10%). All-Play = record if you played every team every week. "
           "Lucky / unlucky = actual record far above / below all-play."]
    msg = "\n".join(out)
    return msg if len(msg) <= 2000 else msg[:1997] + "…"


# ---------- Discord / state ----------

def post_discord(hook, content):
    if DRY_RUN:
        print(content, "\n")
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(hook, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30):
                break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(float(e.headers.get("Retry-After", "3")) + 1)
                continue
            raise
    time.sleep(1)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    if DRY_RUN:
        return
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def main():
    if not RECAP_HOOK and not DRY_RUN:
        sys.exit("DISCORD_RECAP_WEBHOOK_URL is not set")
    state = load_state()
    cycle = current_cycle()
    if not (DRY_RUN or FORCE):
        if state.get("cycle") == cycle:
            print(f"Already done for the week of {cycle}.")
            return
        now = dt.datetime.now(ET)
        if now.weekday() == 0 or (now.weekday() == 1 and now.hour < POST_HOUR):
            # Monday / early Tuesday: the week is still wrapping up
            print("Waiting for Tuesday 10 AM Eastern.")
            return

    weeks = read_season(WEEK_OVERRIDE or None)
    if not weeks:
        print("No finished weeks yet.")
        return
    week = WEEK_OVERRIDE if WEEK_OVERRIDE in weeks else max(weeks)
    print(f"Finished weeks: 1-{max(weeks)}. Recapping week {week}.")

    if state.get("recap_week") == f"{YEAR}-{week}" and not (DRY_RUN or FORCE or WEEK_OVERRIDE):
        print(f"Week {week} was already recapped (no new week finished yet).")
        if dt.datetime.now(ET).weekday() != 1:
            # Past Tuesday with no new week (bye week / offseason): stop checking until next Tuesday.
            # On Tuesday itself keep retrying, in case MFL finalizes Monday night's game late.
            state["cycle"] = cycle
            save_state(state)
        return

    teams, players = load_names({week: weeks[week]})
    post_discord(RECAP_HOOK, recap_message(week, weeks[week], teams, players))
    if week <= LAST_REG:
        post_discord(POWER_HOOK, power_message(week, weeks, teams))
    else:
        print(f"Week {week} is after the regular season; no power rankings.")

    state.update({"cycle": cycle, "recap_week": f"{YEAR}-{week}"})
    save_state(state)
    print(f"{'Previewed' if DRY_RUN else 'Posted'} week {week}.")


if __name__ == "__main__":
    main()
