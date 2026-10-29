#!/usr/bin/env python3
"""Post the MFL trade block to Discord, and post again whenever a manager changes it.

First run (or POST_ALL=1): posts every team's current trade block.
Every run after: posts only teams whose block changed (added / removed / looking for).

Environment variables:
  DISCORD_TRADEBLOCK_WEBHOOK_URL  (required unless DRY_RUN=1)
  MFL_LEAGUE_ID   (default 24958)
  MFL_YEAR        (default 2026)
  MFL_HOST        (default www44.myfantasyleague.com)
  MFL_APIKEY      (optional, private leagues)
  POST_ALL=1      post the full trade block now, even if nothing changed
  DRY_RUN=1       print messages instead of posting (state is not saved)
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
WEBHOOK = (os.environ.get("DISCORD_TRADEBLOCK_WEBHOOK_URL") or "").strip()
STATE_FILE = os.environ.get("TRADEBLOCK_STATE_FILE") or "tradeblock_state.json"
TRUTHY = ("1", "true", "yes")
DRY_RUN = (os.environ.get("DRY_RUN") or "").lower() in TRUTHY
POST_ALL = (os.environ.get("POST_ALL") or "").lower() in TRUTHY
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
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"MFL error for {type_}: {data['error']}")
    return data


def first(d, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return ""


def read_block():
    """{franchise_id: {"assets": [...], "wants": "..."}} for teams with anything listed."""
    data = mfl("tradeBait")
    root = data.get("tradeBaits") or data.get("tradeBait") or {}
    entries = as_list(root.get("tradeBait") if isinstance(root, dict) else root)
    block = {}
    for e in entries:
        fid = first(e, "franchise_id", "franchise", "id")
        if not fid:
            continue
        assets = [a for a in str(first(e, "willGiveUp", "will_give_up", "players")).split(",") if a]
        wants = str(first(e, "inExchangeFor", "in_exchange_for", "comments")).strip()
        if assets or wants:
            block[fid] = {"assets": sorted(set(assets)), "wants": wants}
    return block


def read_teams():
    league = mfl("league").get("league", {})
    return {f["id"]: f.get("name", f["id"])
            for f in as_list(league.get("franchises", {}).get("franchise"))}


def read_players(ids):
    out = {}
    ids = sorted(ids)
    for i in range(0, len(ids), 150):
        data = mfl("players", PLAYERS=",".join(ids[i:i + 150]))
        for p in as_list(data.get("players", {}).get("player")):
            name = p.get("name", p.get("id"))
            if ", " in name:
                last, firstname = name.split(", ", 1)
                name = f"{firstname} {last}"
            extra = ", ".join(x for x in (p.get("position"), p.get("team")) if x)
            out[p["id"]] = f"{name} ({extra})" if extra else name
    return out


def is_pick(a):
    return a[:3] in ("FP_", "DP_")


def describe(a, players, teams):
    if a.startswith("FP_"):  # FP_franchise_year_round
        parts = a.split("_")
        if len(parts) == 4:
            _, fid, yr, rnd = parts
            return f"{yr} Round {rnd} pick ({teams.get(fid, fid)}'s)"
    if a.startswith("DP_"):  # DP_round_pick, 0-based
        parts = a.split("_")
        if len(parts) == 3:
            return f"{YEAR} Pick {int(parts[1]) + 1}.{int(parts[2]) + 1:02d}"
    return players.get(a, f"Player #{a}")


def sort_assets(assets, players, teams):
    # players first (alphabetical), then picks
    return sorted(assets, key=lambda a: (is_pick(a), describe(a, players, teams)))


# ---------- messages ----------

def full_entry(fid, entry, players, teams):
    lines = [f"**{teams.get(fid, fid)}**"]
    for a in sort_assets(entry["assets"], players, teams):
        lines.append(f"• {describe(a, players, teams)}")
    if entry["wants"]:
        lines.append(f"🔎 _Looking for: {entry['wants']}_")
    return "\n".join(lines)


def change_message(fid, old, new, players, teams):
    name = teams.get(fid, fid)
    if not new:
        return f"🧹 **{name}** cleared their trade block."
    old = old or {"assets": [], "wants": ""}
    added = [a for a in new["assets"] if a not in old["assets"]]
    removed = [a for a in old["assets"] if a not in new["assets"]]
    lines = [f"📢 **{name}** updated their trade block"]
    for a in sort_assets(added, players, teams):
        lines.append(f"➕ {describe(a, players, teams)}")
    for a in sort_assets(removed, players, teams):
        lines.append(f"➖ ~~{describe(a, players, teams)}~~")
    if new["wants"] != old["wants"] and new["wants"]:
        lines.append(f"🔎 Now looking for: _{new['wants']}_")
    elif new["wants"] != old["wants"]:
        lines.append("🔎 _No longer listing what they're looking for._")
    if new["assets"] and (added or removed):
        lines.append("")
        lines.append("Full block: " + ", ".join(
            describe(a, players, teams) for a in sort_assets(new["assets"], players, teams)))
    elif new["wants"]:
        pass
    return "\n".join(lines)


def chunk(parts, header=None, limit=1900):
    """Join parts into as few Discord messages as possible."""
    msgs, cur = [], header or ""
    for p in parts:
        piece = ("\n\n" if cur else "") + p
        if len(cur) + len(piece) > limit and cur:
            msgs.append(cur)
            cur = p
        else:
            cur += piece
    if cur:
        msgs.append(cur)
    return [m if len(m) <= 2000 else m[:1997] + "…" for m in msgs]


# ---------- Discord / state ----------

def post_discord(content):
    if DRY_RUN:
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
    time.sleep(1.5)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_state(block):
    if DRY_RUN:
        return
    with open(STATE_FILE, "w") as f:
        json.dump({"block": block}, f, indent=1, sort_keys=True)
        f.write("\n")


def main():
    if not WEBHOOK and not DRY_RUN:
        sys.exit("DISCORD_TRADEBLOCK_WEBHOOK_URL is not set")

    block = read_block()
    state = load_state()
    old = (state or {}).get("block", {})
    print(f"{len(block)} team(s) currently have something on the trade block.")

    full = state is None or POST_ALL
    changed = [] if full else sorted(f for f in set(block) | set(old) if block.get(f) != old.get(f))
    if not full and not changed:
        print("No trade block changes.")
        return

    teams = read_teams()
    ids = {a for e in list(block.values()) + [old.get(f) for f in changed if old.get(f)]
           for a in e["assets"] if not is_pick(a)}
    players = read_players(ids) if ids else {}

    if full:
        order = sorted(block, key=lambda f: teams.get(f, f).lower())
        if order:
            parts = [full_entry(f, block[f], players, teams) for f in order]
            for m in chunk(parts, header="📋 **Current Trade Block**"):
                post_discord(m)
        else:
            post_discord("📋 **Current Trade Block**\nNobody has anyone on the trade block right now.")
        save_state(block)
        print(f"Posted the full trade block ({len(block)} team(s)).")
        return

    for f in changed:
        post_discord(change_message(f, old.get(f), block.get(f), players, teams))
        old = dict(old)
        if f in block:
            old[f] = block[f]
        else:
            old.pop(f, None)
        save_state(old)  # save after each post so a failure never double-posts
        print(f"Posted update for {teams.get(f, f)}")


if __name__ == "__main__":
    main()
