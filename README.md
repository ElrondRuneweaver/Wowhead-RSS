# WoW Forever News Bot

Posts WoW Forever news from Wowhead into a Discord channel:

| Feed | URL |
| --- | --- |
| Wowhead news articles (Forever category only) | https://www.wowhead.com/news/rss/forever |
| Blue tracker (Forever only, US + EU forums and Blizzard news) | https://www.wowhead.com/forever/blue-tracker?rss |

It posts through a Discord **webhook**, so there's no bot account or token.
Every run checks both feeds and posts anything new (title, summary, link,
article image, Blizzard poster). Blue posts that appear on both the US and EU
forums are posted once. Python standard library only.

Run it in **one** place only (cloud *or* this PC), or everything gets posted twice.

## 1. Create the Discord webhook

In Discord, open the channel's settings (gear icon) → *Integrations* →
*Webhooks* → *New Webhook* → *Copy Webhook URL*. You need the *Manage Webhooks*
permission on that server.

Keep the URL private: anyone who has it can post in that channel. If it leaks,
delete the webhook in Discord and create a new one.

## 2a. Run it in the cloud, 24/7 (GitHub Actions, free)

GitHub runs `forever_news.py` every 10 minutes on its own servers. Nothing to
install; everything is done on github.com.

1. **Sign in** to GitHub (or create a free account at github.com/signup).
2. **Create a repository** at github.com/new. Any name, for example
   `wow-forever-news`. Choose **Public**: public repos get unlimited free Actions
   minutes. A private repo only has enough free minutes for a run every 30
   minutes; to use one, change `*/10` to `*/30` in the workflow file.
   The repo only holds the code and the list of posted Wowhead links. The
   webhook URL is stored as a secret and is never shown.
3. **Upload the code.** On the new repo's page click *uploading an existing
   file*, drag in `forever_news.py`, `README.md` and `.gitignore`, then
   *Commit changes*. **Don't upload `config.json`.**
4. **Add the workflow.** *Add file* → *Create new file*. As the name, type
   `.github/workflows/forever-news.yml` (the slashes create the folders). Paste
   the contents of this folder's `.github\workflows\forever-news.yml`, then
   *Commit changes*.
5. **Add the secret.** Repo *Settings* → *Secrets and variables* → *Actions* →
   *New repository secret*. Name: `DISCORD_WEBHOOK_URL`, value: the webhook
   URL. Optional: a second secret `DISCORD_BLUE_WEBHOOK_URL` sends blue posts to
   their own channel.
6. **Start it.** *Actions* tab → *WoW Forever news* → *Run workflow*. The first
   run posts the newest 3 entries of each feed and saves `state.json` into the
   repo. From then on it runs every 10 minutes and posts only new entries.

Good to know:

- GitHub often starts scheduled runs a few minutes late, sometimes more at busy times.
- If a run fails (for example, the webhook was deleted), GitHub emails you.
  The run log on the *Actions* tab shows why.
- GitHub turns off schedules in public repos after 60 days without repository
  activity. The bot's own `state.json` commits count as activity. If it ever
  gets turned off anyway, re-enable it on the *Actions* tab.
- To rename the poster or give it an avatar, edit `BOT_NAME` / `AVATAR_URL` in
  the workflow file on GitHub (pencil icon).

## 2b. Or run it on this PC (Task Scheduler)

Only runs while the PC is on and you're logged in. If the PC was off, the next
run catches up (the feeds keep the last 40 articles and 50 blue posts).

1. Paste the webhook URL into `config.json` as `webhook_url` (optionally
   `blue_webhook_url`).
2. `python forever_news.py --test` posts the newest article and blue post.
3. `python forever_news.py --install` runs it every 10 minutes
   (`--install 5` for every 5; 5 is the minimum).

## Settings

| config.json key | Environment variable | Meaning |
| --- | --- | --- |
| `webhook_url` | `DISCORD_WEBHOOK_URL` | Channel webhook for everything (required). |
| `blue_webhook_url` | `DISCORD_BLUE_WEBHOOK_URL` | Optional separate channel for blue posts. |
| `bot_name` | `BOT_NAME` | Name shown on the posts. |
| `avatar_url` | `AVATAR_URL` | Optional image URL for the poster's avatar. |
| `first_run_posts` | – | Recent entries per feed to post on the very first run (default 3, 0 = none). |

Environment variables win over `config.json`.

## Commands

| Command | Does |
| --- | --- |
| `python forever_news.py` | Check once and post what's new (what both schedulers run). |
| `python forever_news.py --dry-run` | Show what would be posted; posts and saves nothing. |
| `python forever_news.py --test` | Post the newest article and blue post now. |
| `python forever_news.py --install [MIN]` | Create/update the local "WoW Forever News Bot" scheduled task. |
| `python forever_news.py --uninstall` | Remove the local scheduled task. |

## Files

- `state.json` – IDs already posted. Delete it to start over (the next run counts as a first run).
- `bot.log` – local runs only: what was posted and any errors. Trimmed automatically.
