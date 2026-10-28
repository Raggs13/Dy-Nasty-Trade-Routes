#!/usr/bin/env python3
"""Post new MyFantasyLeague trades to a Discord channel.

Runs once per invocation (GitHub Actions calls it every few minutes).
Remembers the newest trade it has seen in state.json so nothing posts twice.

Environment variables:
  DISCORD_WEBHOOK_URL  (required) Discord channel webhook URL
  MFL_LEAGUE_ID        (default 24958)
  MFL_YEAR             (default 2026)
  MFL_HOST             (default www44.myfantasyleague.com)
  MFL_APIKEY           (optional) only needed if the league is private
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LEAGUE_ID = os.environ.get("MFL_LEAGUE_ID", "24958")
YEAR = os.environ.get("MFL_YEAR", "2026")
HOST = os.environ.get("MFL_HOST", "www44.myfantasyleague.com")
APIKEY = os.environ.get("MFL_APIKEY", "").strip()
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UA = "DyNastyTradeBot/1.0 (+https://github.com)"


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
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"MFL error for {type_}: {data['error']}")
    return data


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def trade_key(t):
    return f"{t.get('timestamp')}|{t.get('franchise')}|{t.get('franchise2')}"


def split_assets(s):
    return [a for a in (s or "").split(",") if a]


def player_names(ids):
    if not ids:
        return {}
    data = mfl("players", PLAYERS=",".join(sorted(ids)))
    out = {}
    for p in as_list(data.get("players", {}).get("player")):
        name = p.get("name", p.get("id"))
        if ", " in name:  # MFL uses "Last, First"
            last, first = name.split(", ", 1)
            name = f"{first} {last}"
        extra = ", ".join(x for x in (p.get("position"), p.get("team")) if x)
        out[p["id"]] = f"{name} ({extra})" if extra else name
    return out


def describe_asset(a, players, teams):
    if a.startswith("FP_"):  # future pick: FP_franchise_year_round
        _, fid, yr, rnd = a.split("_")
        return f"{yr} Round {rnd} pick ({teams.get(fid, fid)}'s)"
    if a.startswith("DP_"):  # current-year pick: DP_round_pick (0-based)
        _, rnd, pick = a.split("_")
        return f"{YEAR} Pick {int(rnd) + 1}.{int(pick) + 1:02d}"
    if a.startswith("BB_"):  # blind-bid (FAAB) dollars
        return f"${a[3:]} FAAB"
    return players.get(a, f"Player #{a}")


def format_trade(t, players, teams):
    a, b = t.get("franchise"), t.get("franchise2")
    a_name, b_name = teams.get(a, a), teams.get(b, b)
    a_gave = [describe_asset(x, players, teams) for x in split_assets(t.get("franchise1_gave_up"))]
    b_gave = [describe_asset(x, players, teams) for x in split_assets(t.get("franchise2_gave_up"))]
    lines = [
        "🔄 **TRADE ALERT**",
        f"**{a_name}** receive:",
        *([f"• {x}" for x in b_gave] or ["• (nothing)"]),
        f"**{b_name}** receive:",
        *([f"• {x}" for x in a_gave] or ["• (nothing)"]),
    ]
    if t.get("comments"):
        lines.append(f"_\u201c{t['comments']}\u201d_")
    try:
        lines.append(f"<t:{int(t['timestamp'])}:f>")  # Discord local-time stamp
    except (KeyError, ValueError):
        pass
    return "\n".join(lines)


def post_discord(content):
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(
        WEBHOOK, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:  # rate limited
                time.sleep(float(e.headers.get("Retry-After", "2")))
                continue
            raise


def main():
    if not WEBHOOK:
        sys.exit("DISCORD_WEBHOOK_URL is not set")

    data = mfl("transactions", TRANS_TYPE="TRADE", COUNT="50")
    trades = [t for t in as_list(data.get("transactions", {}).get("transaction"))
              if t.get("type", "TRADE").upper() == "TRADE"]
    trades.sort(key=lambda t: int(t.get("timestamp", 0)))

    state = load_state()
    if state is None:
        # First run: remember existing trades without posting old news.
        save_state({"seen": [trade_key(t) for t in trades][-200:]})
        print(f"Initialized with {len(trades)} existing trade(s); nothing posted.")
        return

    seen = set(state.get("seen", []))
    new = [t for t in trades if trade_key(t) not in seen]
    if not new:
        print("No new trades.")
        return

    league = mfl("league").get("league", {})
    teams = {f["id"]: f.get("name", f["id"])
             for f in as_list(league.get("franchises", {}).get("franchise"))}
    ids = {a for t in new
           for a in split_assets(t.get("franchise1_gave_up")) + split_assets(t.get("franchise2_gave_up"))
           if a[:3] not in ("FP_", "DP_", "BB_")}
    players = player_names(ids)

    order = state.get("seen", [])
    for t in new:
        post_discord(format_trade(t, players, teams))
        order.append(trade_key(t))
        save_state({"seen": order[-200:]})  # save after each post
        print("Posted trade", trade_key(t))
        time.sleep(1)


if __name__ == "__main__":
    main()
