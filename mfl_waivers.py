#!/usr/bin/env python3
"""Post MFL adds, drops, and waiver / FAAB results to a Discord channel.

- Free-agent pickups and drops are posted as they happen.
- Blind-bid (FAAB) and priority waiver results are grouped into one
  "Waiver Results" post per waiver run, with bid amounts.
- Losing bids are included if MFL makes them available (see the log).

Environment variables:
  DISCORD_WAIVERS_WEBHOOK_URL  (required unless DRY_RUN=1)
  MFL_LEAGUE_ID / MFL_YEAR / MFL_HOST / MFL_APIKEY   (as in the other bots)
  DRY_RUN=1          print messages instead of posting (state is not saved)
  PREVIEW_RECENT=N   print the N most recent moves as they would look, then stop
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LEAGUE_ID = os.environ.get("MFL_LEAGUE_ID") or "24958"
YEAR = os.environ.get("MFL_YEAR") or "2026"
HOST = os.environ.get("MFL_HOST") or "www44.myfantasyleague.com"
APIKEY = (os.environ.get("MFL_APIKEY") or "").strip()
WEBHOOK = (os.environ.get("DISCORD_WAIVERS_WEBHOOK_URL") or "").strip()
STATE_FILE = os.environ.get("WAIVERS_STATE_FILE") or "waivers_state.json"
TRUTHY = ("1", "true", "yes")
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in TRUTHY
PREVIEW_RECENT = int(os.environ.get("PREVIEW_RECENT") or 0)
UA = "DyNastyTradeBot/1.0 (+https://github.com)"

FREE_AGENT = {"FREE_AGENT"}
WAIVER = {"WAIVER"}
BBID = {"BBID_WAIVER"}
# Types that might describe bids that didn't win (names vary; reported in the log)
LOSING_HINTS = ("REQUEST", "LOS", "FAIL", "UNSUCC", "REJECT")
HANDLED_ELSEWHERE = {"TRADE", "TRADE_PROPOSAL", "TRADE_ACCEPT", "TRADE_REJECT"}


def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def mfl(type_, **params):
    q = {"TYPE": type_, "L": LEAGUE_ID, "JSON": "1", **params}
    if APIKEY:
        q["APIKEY"] = APIKEY
    url = f"https://{HOST}/{YEAR}/export?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"MFL error for {type_}: {data['error']}")
    return data


def ids(s):
    return [x for x in (s or "").split(",") if x]


def money(s):
    try:
        v = float(str(s).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return int(v) if v == int(v) else v


def parse(t):
    """Normalize one MFL transaction into {type, fid, ts, adds, drops, bid, raw}."""
    typ = (t.get("type") or "").upper()
    parts = (t.get("transaction") or "").split("|")
    adds, drops, bid = [], [], None
    if typ in BBID or (len(parts) >= 3 and typ not in FREE_AGENT | WAIVER):
        adds = ids(parts[0]) if parts else []
        bid = money(parts[1]) if len(parts) > 1 else None
        drops = ids(parts[2]) if len(parts) > 2 else []
    else:
        adds = ids(parts[0]) if parts else []
        drops = ids(parts[1]) if len(parts) > 1 else []
    return {"type": typ, "fid": t.get("franchise", ""), "ts": int(t.get("timestamp") or 0),
            "adds": adds, "drops": drops, "bid": bid, "raw": t}


def key(t):
    r = t["raw"]
    return f"{r.get('timestamp')}|{r.get('franchise')}|{r.get('type')}|{r.get('transaction')}"


# ---------- names ----------

def load_names(moves):
    league = mfl("league").get("league", {})
    teams, faab = {}, {}
    for f in as_list(league.get("franchises", {}).get("franchise")):
        teams[f["id"]] = f.get("name", f["id"])
        bal = money(f.get("bbidAvailableBalance"))
        if bal is not None:
            faab[f["id"]] = bal
    pids = sorted({p for m in moves for p in m["adds"] + m["drops"]})
    players = {}
    for i in range(0, len(pids), 150):
        data = mfl("players", PLAYERS=",".join(pids[i:i + 150]))
        for p in as_list(data.get("players", {}).get("player")):
            name = p.get("name", p.get("id"))
            if ", " in name:
                last, first = name.split(", ", 1)
                name = f"{first} {last}"
            extra = ", ".join(x for x in (p.get("position"), p.get("team")) if x)
            players[p["id"]] = f"{name} ({extra})" if extra else name
    return teams, faab, players


# ---------- messages ----------

def plist(pids, players):
    return ", ".join(players.get(p, f"Player #{p}") for p in pids)


def fa_message(m, teams, players):
    team = teams.get(m["fid"], m["fid"])
    if m["adds"] and m["drops"]:
        return (f"🆓 **{team}** added {plist(m['adds'], players)}\n"
                f"✂️ dropped {plist(m['drops'], players)}")
    if m["adds"]:
        return f"🆓 **{team}** added {plist(m['adds'], players)}"
    return f"✂️ **{team}** dropped {plist(m['drops'], players)}"


def waiver_message(wins, losers, teams, faab, players):
    """One post for a waiver run: winners with $ (and losing bids if known)."""
    lost_by_player = {}
    for m in losers:
        for p in m["adds"]:
            lost_by_player.setdefault(p, []).append(m)
    has_bids = any(m["bid"] is not None for m in wins)
    lines = ["💰 **Waiver Results**" if has_bids else "📝 **Waiver Results**"]
    for m in sorted(wins, key=lambda m: (-(m["bid"] or 0), teams.get(m["fid"], ""))):
        team = teams.get(m["fid"], m["fid"])
        bid = f" for **${m['bid']}**" if m["bid"] is not None else ""
        left = f" (${faab[m['fid']]} left)" if has_bids and m["fid"] in faab else ""
        lines.append(f"✅ **{team}** won {plist(m['adds'], players)}{bid}{left}")
        if m["drops"]:
            lines.append(f"    ✂️ dropped {plist(m['drops'], players)}")
        others = []
        for p in m["adds"]:
            for lm in lost_by_player.pop(p, []):
                amt = f" ${lm['bid']}" if lm["bid"] is not None else ""
                others.append(f"{teams.get(lm['fid'], lm['fid'])}{amt}")
        if others:
            lines.append("    ↳ other bids: " + ", ".join(others))
    for p, lms in lost_by_player.items():  # bids on players nobody won
        bids = ", ".join(f"{teams.get(l['fid'], l['fid'])}"
                         + (f" ${l['bid']}" if l["bid"] is not None else "") for l in lms)
        lines.append(f"❌ {players.get(p, 'Player #' + p)}: {bids} (not awarded)")
    return lines


def chunk_lines(lines, limit=1900):
    msgs, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            msgs.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        msgs.append(cur)
    return msgs


def build_posts(moves, teams, faab, players):
    """Turn new moves into Discord messages, oldest first."""
    posts = []
    fa = [m for m in moves if m["type"] in FREE_AGENT]
    wins = [m for m in moves if m["type"] in BBID | WAIVER]
    won = {(m["fid"], p) for m in wins for p in m["adds"]}
    losers = [m for m in moves if is_request(m) and m["adds"]
              and not any((m["fid"], p) in won for p in m["adds"])]  # skip winners' own bids
    timeline = [(m["ts"], "fa", m) for m in fa]
    if wins or losers:
        timeline.append((min(m["ts"] for m in wins + losers), "waivers", None))
    for _, kind, m in sorted(timeline, key=lambda x: x[0]):
        if kind == "fa":
            posts.append(fa_message(m, teams, players))
        else:
            posts += chunk_lines(waiver_message(wins, losers, teams, faab, players))
    return posts


# ---------- Discord / state ----------

def post_discord(content):
    if DRY_RUN or PREVIEW_RECENT:
        print(content, "\n")
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(WEBHOOK, data=body, method="POST",
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
    time.sleep(1.2)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(seen):
    if DRY_RUN or PREVIEW_RECENT:
        return
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": seen[-1000:]}, f, indent=1)
        f.write("\n")


def report_types(raw):
    counts, samples = {}, {}
    for t in raw:
        typ = (t.get("type") or "?").upper()
        counts[typ] = counts.get(typ, 0) + 1
        samples.setdefault(typ, t)
    print("Transaction types MFL returned (last 14 days): "
          + (", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "none"))
    known = FREE_AGENT | WAIVER | BBID | HANDLED_ELSEWHERE
    for typ, t in samples.items():
        if typ not in known:
            print(f"  sample {typ}: {json.dumps(t)}")


def is_request(m):
    return any(h in m["type"] for h in LOSING_HINTS)


def hold_pending_bids(moves):
    """Never reveal a bid before its waiver run is processed.

    Bid-request entries are only posted alongside a processed waiver run
    (a winning BBID/WAIVER result in the same batch), and only if they were
    placed before that run. Anything else waits, unseen, for a later run.
    """
    wins = [m for m in moves if m["type"] in BBID | WAIVER]
    if not wins:
        return [m for m in moves if not is_request(m)]
    run_time = max(m["ts"] for m in wins)
    return [m for m in moves if not is_request(m) or m["ts"] <= run_time]


def main():
    if not WEBHOOK and not (DRY_RUN or PREVIEW_RECENT):
        sys.exit("DISCORD_WAIVERS_WEBHOOK_URL is not set")

    data = mfl("transactions", DAYS="14")
    raw = as_list(data.get("transactions", {}).get("transaction"))
    report_types(raw)
    relevant = [parse(t) for t in raw if (t.get("type") or "").upper() not in HANDLED_ELSEWHERE]
    relevant = [m for m in relevant
                if m["type"] in FREE_AGENT | WAIVER | BBID
                or any(h in m["type"] for h in LOSING_HINTS)]
    relevant.sort(key=lambda m: m["ts"])

    if PREVIEW_RECENT:
        moves = hold_pending_bids(relevant[-PREVIEW_RECENT:])
        print(f"Preview of the {len(moves)} most recent move(s):\n")
        if moves:
            teams, faab, players = load_names(moves)
            for p in build_posts(moves, teams, faab, players):
                post_discord(p)
        return

    state = load_state()
    if state is None:
        save_state([key(m) for m in relevant])
        print(f"First run: recorded {len(relevant)} existing move(s); nothing posted.")
        return

    seen_list = state.get("seen", [])
    seen = set(seen_list)
    new = [m for m in relevant if key(m) not in seen]
    new = hold_pending_bids(new)
    if not new:
        print("No new adds, drops, or waiver results.")
        return

    teams, faab, players = load_names(new)
    for p in build_posts(new, teams, faab, players):
        post_discord(p)
    seen_list += [key(m) for m in new]
    save_state(seen_list)
    print(f"Posted {len(new)} new move(s).")


if __name__ == "__main__":
    main()
