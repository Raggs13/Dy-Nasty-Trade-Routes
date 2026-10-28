# Dy-Nasty Inc. Trade Bot

Posts every new trade in MFL league **24958** to a Discord channel, checking every 10 minutes.

## Setup (about 10 minutes)

### 1. Make a Discord webhook
1. In Discord, open the channel → ⚙️ **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**.
2. Name it (e.g. "Trade Bot"), then **Copy Webhook URL**. Keep this private, since anyone with it can post to the channel.

### 2. Create a GitHub repository
1. Sign in at github.com → **New repository**, e.g. `dynasty-trade-bot`.
2. Make it **Public**. Public repos get unlimited free Actions minutes; a private repo checking every 10 minutes would run past the free monthly limit. Your webhook stays hidden either way, because it's stored as a secret (step 3).
3. Upload the files, keeping this layout:
   ```
   mfl_trades.py
   README.md
   .github/workflows/mfl-trades.yml
   ```
   (In the web uploader you can type `.github/workflows/mfl-trades.yml` as the file name to create the folders.)

### 3. Add the webhook as a secret
Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
- Name: `DISCORD_WEBHOOK_URL`, Value: the webhook URL from step 1.

### 4. Turn it on
Repo → **Actions** tab → enable workflows if asked → **MFL trades to Discord** → **Run workflow**.

The first run only records existing trades, so your channel doesn't get flooded with old ones. After that, each new trade is posted within about 10–15 minutes.

## Posting trade history (one-time)

`mfl_history.py` posts every trade from the 2023–2026 seasons to a separate channel. For each draft pick in a trade, it also shows who was drafted with it, for example:

> • 2025 Round 1 pick (Gridiron Gurus's) → 1.05 **Ashton Jeanty (RB, LV)**

Setup:
1. In Discord, create a webhook in your history channel (same steps as above).
2. Add it as a repository secret named `DISCORD_HISTORY_WEBHOOK_URL`.
3. Upload `mfl_history.py` to the top level of the repo, and create `.github/workflows/mfl-history.yml` the same way as the other workflow file.
4. Go to **Actions → Post trade history to Discord → Run workflow**.
   - Tick **Preview only** first to see the messages in the run's log without posting anything.
   - Then run it again with the box unticked to post for real.

Posting takes about 2–3 seconds per trade, so a few hundred trades take 10–15 minutes. The workflow saves its progress in `history_state.json`, so if a run is interrupted, running it again picks up where it left off without double-posting.

What the pick results mean:
- **→ 1.05 Player Name**: the pick and who was taken with it.
- **→ not drafted yet**: a pick for a draft that hasn't happened (2027 and later).
- **→ one of: …**: the script couldn't tell which of a team's picks in that round this was; it lists the possibilities.
- **→ draft result not found**: MFL has no draft results for that year (e.g. a draft done offline).

## If the run fails with an MFL error (private league)
Some leagues hide transactions from non-members. If that's the case:
1. Log in to MFL and find your API key on the league's developer API page (under the league's **Help** menu, look for the developers/API section).
2. Add it as a second secret named `MFL_APIKEY`.

## Notes
- **Next season:** change `MFL_YEAR` in `.github/workflows/mfl-trades.yml` (and `MFL_HOST` if MFL moves your league to a different server).
- **Paused workflows:** GitHub pauses scheduled workflows in public repos after 60 days with no repo activity (such as a long offseason with no trades). GitHub emails you first, and you can re-enable it in the Actions tab.
- **Message format:** team names, player names with position and NFL team, draft picks, FAAB, and trade comments are all included. Edit `format_trade()` in `mfl_trades.py` to change the format.
