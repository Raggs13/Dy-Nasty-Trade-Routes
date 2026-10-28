#!/usr/bin/env python3
"""One-time backfill: post past MFL trades to a Discord history channel.

For every draft pick in a trade, it shows which player was eventually
drafted with that pick.

Environment variables:
  DISCORD_HISTORY_WEBHOOK_URL  (required unless DRY_RUN=1)
  HISTORY_FIRST_YEAR / HISTORY_LAST_YEAR   (default 2023 / 2026)
  MFL_LEAGUE_ID    (default 24958)
  MFL_APIKEY       (optional, for private leagues)
  DRY_RUN=1        print messages instead of posting them
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LEAGUE_ID = os.environ.get("MFL_LEAGUE_ID", "24958")
HOST = os.environ.get("MFL_HISTORY_HOST", "api.myfantasyleague.com")  # redirects to the right server per year
APIKEY = os.environ.get("MFL_APIKEY", "").strip()
WEBHOOK = os.environ.get("DISCORD_HISTORY_WEBHOOK_URL", "").strip()
FIRST_YEAR = int(os.environ.get("HISTORY_FIRST_YEAR") or 2023)
LAST_YEAR = int(os.environ.get("HISTORY_LAST_YEAR") or 2026)
LOOKBACK = 3  # earlier seasons read (not posted) to follow picks traded years ahead
STATE_FILE = os.environ.get("HISTORY_STATE_FILE", "history_state.json")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
UA = "DyNastyTradeBot/1.0 (+https://github.com)"


# ---------- MFL access ----------

def as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def split_assets(s):
    return [a for a in (s or "").split(",") if a]


def mfl(year, type_, **params):
    """Fetch an MFL export. Returns None (with a warning) if unavailable."""
    q = {"TYPE": type_, "L": LEAGUE_ID, "JSON": "1", **params}
    if APIKEY:
        q["APIKEY"] = APIKEY
    url = f"https://{HOST}/{year}/export?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            time.sleep(1)  # be polite to MFL
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            if "error" in data:
                print(f"  ! MFL {year} {type_}: {data['error']}")
                return None
            return data
        except Exception as e:  # network hiccup, throttling, bad JSON
            if attempt == 2:
                print(f"  ! MFL {year} {type_} failed: {e}")
                return None
            time.sleep(5 * (attempt + 1))


def load_trades(year):
    data = mfl(year, "transactions", TRANS_TYPE="TRADE")
    trades = []
    for t in as_list((data or {}).get("transactions", {}).get("transaction")):
        if t.get("type", "TRADE").upper() != "TRADE":
            continue
        trades.append({
            "year": year,
            "ts": int(t.get("timestamp") or 0),
            "a": t.get("franchise"),
            "b": t.get("franchise2"),
            "a_gave": split_assets(t.get("franchise1_gave_up")),
            "b_gave": split_assets(t.get("franchise2_gave_up")),
            "comments": t.get("comments") or "",
        })
    return trades


def load_teams(year):
    data = mfl(year, "league") or {}
    return {f["id"]: f.get("name", f["id"])
            for f in as_list(data.get("league", {}).get("franchises", {}).get("franchise"))}


def load_draft(year):
    """List of {round, pick, franchise, player} (1-based), or None."""
    data = mfl(year, "draftResults")
    if not data:
        return None
    picks = []
    for unit in as_list(data.get("draftResults", {}).get("draftUnit")):
        for p in as_list(unit.get("draftPick")):
            try:
                picks.append({"round": int(p["round"]), "pick": int(p["pick"]),
                              "franchise": p.get("franchise"), "player": p.get("player") or ""})
            except (KeyError, ValueError):
                pass
    return picks or None


def load_players(year, ids):
    out = {}
    ids = sorted(ids)
    for i in range(0, len(ids), 150):
        data = mfl(year, "players", PLAYERS=",".join(ids[i:i + 150])) or {}
        for p in as_list(data.get("players", {}).get("player")):
            name = p.get("name", p.get("id"))
            if ", " in name:
                last, first = name.split(", ", 1)
                name = f"{first} {last}"
            extra = ", ".join(x for x in (p.get("position"), p.get("team")) if x)
            out[p["id"]] = f"{name} ({extra})" if extra else name
    return out


# ---------- following traded picks to the draft ----------

def legs(t):
    """(giver, receiver, assets) for each side of a trade."""
    return ((t["a"], t["b"], t["a_gave"]), (t["b"], t["a"], t["b_gave"]))


def resolve_future_picks(trades, drafts, teams_by_year):
    """Work out which draft slot each original team's pick became.

    MFL records a future pick only as "Team X's 2025 1st". Once the draft
    order is set it becomes a numbered slot. We follow each future pick
    through trades to its final owner, follow each numbered slot backwards
    from the team that used it, and match the two up. When one team held
    several picks in a round, the league's draft order (linear or snake,
    learned from the unambiguous picks) breaks the tie.

    Returns {(draft_year, round, original_team): slot or [candidate slots]}.
    """
    trades = sorted(trades, key=lambda t: t["ts"])
    result = {}
    for D, picks in drafts.items():
        if not picks:
            continue
        rounds = {}
        for p in picks:
            rounds.setdefault(p["round"], {})[p["pick"]] = p
        origs = set(teams_by_year.get(D) or {p["franchise"] for p in picks})
        n_slots = max(len(s) for s in rounds.values())
        hints = {}

        for R, slots in rounds.items():
            owner = {o: o for o in origs}
            for t in trades:
                for giver, recv, assets in legs(t):
                    for x in assets:
                        parts = x.split("_")
                        if (parts[0] == "FP" and len(parts) == 4 and parts[1] in owner
                                and parts[2] == str(D) and parts[3].isdigit() and int(parts[3]) == R):
                            owner[parts[1]] = recv
            first_owner = {s: p["franchise"] for s, p in slots.items()}
            for t in reversed(trades):
                if t["year"] != D:
                    continue
                for giver, recv, assets in legs(t):
                    for x in assets:
                        parts = x.split("_")
                        if parts[0] == "DP" and len(parts) == 3:
                            r, s = int(parts[1]) + 1, int(parts[2]) + 1
                            if r == R and s in first_owner:
                                first_owner[s] = giver

            groups = {}
            for o in origs:
                groups.setdefault(owner[o], ([], []))[0].append(o)
            for s in slots:
                groups.setdefault(first_owner[s], ([], []))[1].append(s)
            for os_, ss in groups.values():
                if len(os_) == 1 and len(ss) == 1:
                    result[(D, R, os_[0])] = ss[0]
                    hints.setdefault(os_[0], {})[R] = ss[0]
                else:
                    for o in os_:
                        result[(D, R, o)] = sorted(ss)

        # Learn linear vs snake order from unambiguous picks
        linear = snake = 0
        for h in hints.values():
            items = sorted(h.items())
            for (r1, s1), (r2, s2) in zip(items, items[1:]):
                if (r2 - r1) % 2 == 1:
                    linear += s1 == s2
                    snake += s2 == n_slots + 1 - s1
        style = "snake" if snake > linear else "linear"

        def predict(o, R):
            for r2, s2 in hints.get(o, {}).items():
                if style == "snake" and (R - r2) % 2 == 1:
                    return n_slots + 1 - s2
                return s2
            return None

        for (d, R, o) in list(result):
            v = result[(d, R, o)]
            if d != D or not isinstance(v, list):
                continue
            guess = predict(o, R)
            if guess in v:
                result[(d, R, o)] = guess
                # remove from other teams' candidate lists in this group
                for k, v2 in result.items():
                    if k[:2] == (d, R) and isinstance(v2, list) and k != (d, R, o) and guess in v2:
                        v2.remove(guess)
                        if len(v2) == 1:
                            result[k] = v2[0]
    return result


# ---------- formatting ----------

def describe(asset, year, ctx):
    teams = ctx["teams"].get(year, {})
    players = ctx["players"]
    if asset.startswith("BB_"):
        return f"${asset[3:]} FAAB"
    if asset.startswith("DP_"):
        _, r, s = asset.split("_")
        R, S = int(r) + 1, int(s) + 1
        return f"{year} Pick {R}.{S:02d}" + became(year, R, S, ctx)
    if asset.startswith("FP_"):
        _, fid, yr, rnd = asset.split("_")
        D, R = int(yr), int(rnd)
        label = f"{yr} Round {rnd} pick ({teams.get(fid) or ctx['teams'].get(D, {}).get(fid, fid)}'s)"
        if D > ctx["latest_draft_year"]:
            return label + " → not drafted yet"
        if ctx["drafts"].get(D) is None:
            return label + " → draft result not found"
        slot = ctx["resolved"].get((D, R, fid))
        if isinstance(slot, int):
            return label + became(D, R, slot, ctx, show_slot=True)
        if slot:
            names = [ctx["slot_text"](D, R, s) for s in slot]
            return label + " → one of: " + " / ".join(names)
        return label + " → couldn't trace"
    return players.get((year, asset)) or players.get(asset) or f"Player #{asset}"


def became(D, R, S, ctx, show_slot=False):
    if ctx["drafts"].get(D) is None:
        return " → draft result not found" if D <= ctx["latest_draft_year"] else " → not drafted yet"
    text = ctx["slot_text"](D, R, S)
    return f" → {text}"


def make_slot_text(drafts, players):
    index = {(D, p["round"], p["pick"]): p for D, ps in drafts.items() if ps for p in ps}

    def slot_text(D, R, S):
        p = index.get((D, R, S))
        if not p:
            return f"{R}.{S:02d} (not in draft results)"
        if not p["player"]:
            return f"{R}.{S:02d} (pick not made yet)"
        name = players.get((D, p["player"])) or players.get(p["player"]) or f"Player #{p['player']}"
        return f"{R}.{S:02d} **{name}**"
    return slot_text


def format_trade(t, ctx):
    teams = ctx["teams"].get(t["year"], {})
    a_name, b_name = teams.get(t["a"], t["a"]), teams.get(t["b"], t["b"])
    lines = [
        "🔄 **TRADE**" + (f"  <t:{t['ts']}:D>" if t["ts"] else ""),
        f"**{a_name}** receive:",
        *([f"• {describe(x, t['year'], ctx)}" for x in t["b_gave"]] or ["• (nothing)"]),
        f"**{b_name}** receive:",
        *([f"• {describe(x, t['year'], ctx)}" for x in t["a_gave"]] or ["• (nothing)"]),
    ]
    if t["comments"]:
        lines.append(f"_“{t['comments']}”_")
    msg = "\n".join(lines)
    return msg if len(msg) <= 1990 else msg[:1987] + "…"


# ---------- Discord ----------

def post_discord(content):
    if DRY_RUN:
        print(content, "\n")
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(WEBHOOK, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30):
                break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(float(e.headers.get("Retry-After", "5")) + 1)
                continue
            raise
    time.sleep(2.2)  # stay under Discord's webhook limit (~30 posts/minute)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"posted": []}


def save_state(state):
    if DRY_RUN:
        return
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)
        f.write("\n")


def trade_key(t):
    return f"{t['year']}|{t['ts']}|{t['a']}|{t['b']}"


# ---------- main ----------

def main():
    if not WEBHOOK and not DRY_RUN:
        sys.exit("DISCORD_HISTORY_WEBHOOK_URL is not set")
    post_years = list(range(FIRST_YEAR, LAST_YEAR + 1))
    all_years = list(range(FIRST_YEAR - LOOKBACK, LAST_YEAR + 1))

    print("Reading league data from MFL…")
    trades, teams, drafts = [], {}, {}
    for y in all_years:
        trades += load_trades(y)
        teams[y] = load_teams(y)
        drafts[y] = load_draft(y)
        print(f"  {y}: {sum(t['year'] == y for t in trades)} trades, "
              f"draft {'found' if drafts[y] else 'not found'}")

    resolved = resolve_future_picks(trades, drafts, teams)
    latest_draft_year = max([y for y, d in drafts.items() if d] or [FIRST_YEAR - 1])

    # Player names: players in posted trades and players drafted
    need = {}
    for t in trades:
        if t["year"] in post_years:
            for x in t["a_gave"] + t["b_gave"]:
                if x[:3] not in ("FP_", "DP_", "BB_"):
                    need.setdefault(t["year"], set()).add(x)
    for D, ps in drafts.items():
        for p in ps or []:
            if p["player"]:
                need.setdefault(D, set()).add(p["player"])
    players = {}
    for y, ids in need.items():
        for pid, name in load_players(y, ids).items():
            players[(y, pid)] = name
            players.setdefault(pid, name)

    ctx = {"teams": teams, "players": players, "drafts": drafts, "resolved": resolved,
           "latest_draft_year": latest_draft_year, "slot_text": make_slot_text(drafts, players)}

    state = load_state()
    posted = set(state["posted"])
    count = 0
    for y in post_years:
        season = sorted((t for t in trades if t["year"] == y), key=lambda t: t["ts"])
        if not season:
            continue
        hkey = f"header|{y}"
        if hkey not in posted:
            post_discord(f"📅 **{y} Season Trades** ({len(season)})")
            posted.add(hkey); state["posted"].append(hkey); save_state(state)
        for t in season:
            k = trade_key(t)
            if k in posted:
                continue
            post_discord(format_trade(t, ctx))
            posted.add(k); state["posted"].append(k); save_state(state)
            count += 1
    print(f"Done. {'Printed' if DRY_RUN else 'Posted'} {count} trade(s).")


if __name__ == "__main__":
    main()
