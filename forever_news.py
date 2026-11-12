"""WoW Forever news for Discord.

Polls two Wowhead RSS feeds and posts anything new to a Discord channel webhook:
  * WoW Forever news articles   https://www.wowhead.com/news/rss/forever
  * WoW Forever blue tracker    https://www.wowhead.com/forever/blue-tracker?rss

Python standard library only. See README.md for setup.

  python forever_news.py                  check once and post what's new (what the scheduled task runs)
  python forever_news.py --dry-run        print what would be posted; posts and saves nothing
  python forever_news.py --test           post the newest article and blue post now (state untouched)
  python forever_news.py --install [MIN]  run every MIN minutes (default 10) with Task Scheduler
  python forever_news.py --uninstall      remove the scheduled task
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
STATE_FILE = HERE / "state.json"
LOG_FILE = HERE / "bot.log"
TASK_NAME = "WoW Forever News Bot"
USER_AGENT = "WoWForeverNewsBot/1.0 (personal Discord feed relay)"

FEEDS = {
    "news": {
        "url": "https://www.wowhead.com/news/rss/forever",
        "label": "Wowhead News",
        "color": 0xC1272D,
    },
    "blue": {
        "url": "https://www.wowhead.com/forever/blue-tracker?rss",
        "label": "Blue Tracker",
        "color": 0x148EFF,
    },
}

DEFAULT_CONFIG = {
    "webhook_url": "",
    "blue_webhook_url": "",
    "bot_name": "WoW Forever News",
    "avatar_url": "",
    "first_run_posts": 3,
}

# Environment variables override config.json (used by the GitHub Actions workflow).
ENV_OVERRIDES = {
    "webhook_url": "DISCORD_WEBHOOK_URL",
    "blue_webhook_url": "DISCORD_BLUE_WEBHOOK_URL",
    "bot_name": "BOT_NAME",
    "avatar_url": "AVATAR_URL",
}

SEEN_DAYS = 30          # forget posted ids this old once they have left the feed
BLUE_DUPE_HOURS = 72    # same blue title posted on US and EU within this window = post once
MEDIA_NS = "{http://search.yahoo.com/mrss/}"
WEBHOOK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+$")
BLIZZ_NEWS_SUFFIX = re.compile(r"\s+—\s+World of Warcraft\s+—\s+Blizzard News\s*$")
BLUE_BYLINE = re.compile(
    r"(?:<br\s*/?>\s*)*By\s*<a[^>]*>([^<]+)</a>\s*on\s+[\d/]+\s+at\s+[^<]*$", re.I | re.S
)


# --------------------------------------------------------------------------- helpers

def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="replace")[-256_000:]
            LOG_FILE.write_text(tail, encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class _TextExtractor(HTMLParser):
    BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(fragment: str) -> str:
    parser = _TextExtractor()
    parser.feed(fragment or "")
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def to_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


# --------------------------------------------------------------------------- feeds

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_feed(key: str, data: bytes) -> list[dict]:
    items = []
    for node in ET.fromstring(data).iter("item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or link).strip()
        if not title or not guid:
            continue
        try:
            when = parsedate_to_datetime(node.findtext("pubDate") or "")
        except (TypeError, ValueError):
            when = None
        if when is None:
            when = datetime.now(timezone.utc)
        elif when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)

        raw = node.findtext("description") or ""
        item = {
            "feed": key, "id": guid, "title": title, "link": link or guid, "when": when,
            "image": None, "author": None, "footer": FEEDS[key]["label"], "dupe_key": None,
        }
        if key == "news":
            media = node.find(f"{MEDIA_NS}content")
            if media is not None and media.get("url") and media.get("medium", "image") == "image":
                item["image"] = media.get("url")
            text = re.sub(r"\s*Continue reading\s*»?\s*$", "", html_to_text(raw))
        else:
            byline = BLUE_BYLINE.search(raw)
            if byline:
                item["author"] = html_to_text(byline.group(1))
                raw = raw[: byline.start()]
            text = html_to_text(raw)
            official = bool(BLIZZ_NEWS_SUFFIX.search(title))
            title = BLIZZ_NEWS_SUFFIX.sub("", title)
            region = re.search(r"/(?:topic|news)/(us|eu)/", link)
            where = "Blizzard News" if official else "Forums"
            if region:
                where += f" ({region.group(1).upper()})"
            item["title"] = title
            item["footer"] = f"Blue Tracker · {where}"
            item["dupe_key"] = f"{'news' if official else 'forum'}|{title.casefold()}"
        item["summary"] = text
        items.append(item)
    return items


# --------------------------------------------------------------------------- discord

def webhook_for(item: dict, cfg: dict) -> str:
    if item["feed"] == "blue" and cfg.get("blue_webhook_url"):
        return cfg["blue_webhook_url"]
    return cfg["webhook_url"]


def build_payload(item: dict, cfg: dict) -> dict:
    embed = {
        "title": shorten(item["title"], 256),
        "url": item["link"],
        "color": FEEDS[item["feed"]]["color"],
        "timestamp": item["when"].astimezone(timezone.utc).isoformat(),
        "footer": {"text": item["footer"]},
    }
    if item["summary"]:
        embed["description"] = shorten(item["summary"], 400)
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    if item["author"]:
        embed["author"] = {"name": item["author"]}
    payload = {
        "username": cfg.get("bot_name") or DEFAULT_CONFIG["bot_name"],
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    if cfg.get("avatar_url"):
        payload["avatar_url"] = cfg["avatar_url"]
    return payload


def post(webhook: str, payload: dict) -> None:
    url = webhook + ("&" if "?" in webhook else "?") + "wait=true"
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.headers.get("X-RateLimit-Remaining") == "0":
                    time.sleep(float(resp.headers.get("X-RateLimit-Reset-After") or 1))
                return
        except urllib.error.HTTPError as err:
            detail = err.read()
            if err.code == 429 and attempt < 3:
                try:
                    wait = float(json.loads(detail or b"{}").get("retry_after", 2))
                except ValueError:
                    wait = 2.0
                time.sleep(min(wait, 60) + 0.25)
                continue
            raise RuntimeError(f"Discord answered HTTP {err.code}: {detail[:300].decode('utf-8', 'replace')}") from None
    raise RuntimeError("Discord kept rate-limiting; giving up for this run")


# --------------------------------------------------------------------------- state / config

def load_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log("state.json is unreadable; treating this as a first run")
        return None
    return {"seen": dict(data.get("seen", {})), "blue_titles": dict(data.get("blue_titles", {}))}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def load_config(need_webhook: bool) -> dict:
    """config.json, with environment variables (GitHub Actions secrets) taking precedence."""
    cfg = dict(DEFAULT_CONFIG)
    from_env = {field: os.environ.get(var, "").strip() for field, var in ENV_OVERRIDES.items()}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except ValueError as err:
            log(f"config.json is not valid JSON: {err}")
            sys.exit(2)
    elif not from_env["webhook_url"]:
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        log(f"created {CONFIG_FILE.name}; paste your Discord webhook URL into it")
    cfg.update({field: value for field, value in from_env.items() if value})
    if need_webhook:
        for field in ("webhook_url", "blue_webhook_url"):
            value = (cfg.get(field) or "").strip()
            cfg[field] = value
            if field == "webhook_url" and not value:
                log("no webhook URL: set webhook_url in config.json or the DISCORD_WEBHOOK_URL secret")
                sys.exit(2)
            if value and not WEBHOOK_RE.match(value):
                log(f"{field} ({ENV_OVERRIDES[field]}) doesn't look like a Discord webhook URL")
                sys.exit(2)
    return cfg


# --------------------------------------------------------------------------- main run

def run(cfg: dict, dry_run: bool) -> int:
    items: list[dict] = []
    live_ids: set[str] = set()
    all_ok = True
    for key, feed in FEEDS.items():
        try:
            got = parse_feed(key, fetch(feed["url"]))
        except Exception as err:  # network, HTTP or XML problem: try again next run
            log(f"{key}: could not read feed: {err}")
            all_ok = False
            continue
        items.extend(got)
        live_ids.update(i["id"] for i in got)
    if not items:
        return 1

    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    window = timedelta(hours=BLUE_DUPE_HOURS)
    state = load_state()
    first_run = state is None
    if first_run:
        state = {"seen": {}, "blue_titles": {}}

    def commit(item: dict) -> None:
        state["seen"][item["id"]] = stamp
        if item["dupe_key"]:
            state["blue_titles"][item["dupe_key"]] = item["when"].isoformat()

    def same_post_as(when: datetime, other_iso: str | None) -> bool:
        return other_iso is not None and abs(when - to_dt(other_iso)) < window

    # New items, oldest first. A blue post that appears on both the US and EU feeds
    # is posted once; the copy is only marked seen after the first one went out.
    items.sort(key=lambda i: i["when"])
    fresh, copies, claimed = [], [], {}
    for it in items:
        if it["id"] in state["seen"]:
            continue
        key = it["dupe_key"]
        if key and (same_post_as(it["when"], claimed.get(key))
                    or same_post_as(it["when"], state["blue_titles"].get(key))):
            copies.append(it)
            continue
        if key:
            claimed[key] = it["when"].isoformat()
        fresh.append(it)

    to_post = fresh
    if first_run:
        n = max(0, int(cfg.get("first_run_posts") or 0))
        to_post = []
        for key in FEEDS:
            per_feed = [i for i in fresh if i["feed"] == key]
            to_post += per_feed[-n:] if n else []
        to_post.sort(key=lambda i: i["when"])
        posting = {i["id"] for i in to_post}
        for it in fresh:
            if it["id"] not in posting:
                commit(it)
        log(f"first run: {len(fresh) - len(to_post)} existing entries marked as seen, "
            f"posting the newest {n} per feed")

    post_failed = False
    for it in to_post:
        if dry_run:
            print(f"[would post] {it['when']:%Y-%m-%d %H:%M}  {it['footer']:<32}  {it['title']}")
        else:
            try:
                post(webhook_for(it, cfg), build_payload(it, cfg))
            except Exception as err:
                log(f"post failed, will retry next run: {it['title']!r}: {err}")
                post_failed = True
                break
            log(f"posted [{it['feed']}] {it['title']}")
        commit(it)
        if not dry_run:
            save_state(state)

    for it in copies:
        if same_post_as(it["when"], state["blue_titles"].get(it["dupe_key"])):
            state["seen"][it["id"]] = stamp

    if all_ok:  # only forget old ids when every feed was read, so nothing reposts
        cutoff = now - timedelta(days=SEEN_DAYS)
        state["seen"] = {k: v for k, v in state["seen"].items() if k in live_ids or to_dt(v) > cutoff}
        state["blue_titles"] = {k: v for k, v in state["blue_titles"].items() if to_dt(v) > cutoff}

    if dry_run:
        print(f"dry run: {len(to_post)} would be posted, {len(copies)} US/EU copies skipped; nothing saved")
    else:
        save_state(state)
    return 1 if post_failed else 0


def send_test(cfg: dict) -> int:
    for key, feed in FEEDS.items():
        newest = max(parse_feed(key, fetch(feed["url"])), key=lambda i: i["when"])
        post(webhook_for(newest, cfg), build_payload(newest, cfg))
        log(f"test post [{key}] {newest['title']}")
    return 0


# --------------------------------------------------------------------------- scheduled task

def install(minutes: int) -> int:
    minutes = max(5, min(minutes, 1439))
    exe = Path(sys.executable)
    runner = exe.with_name("pythonw.exe")
    if not runner.exists():
        runner = exe
    action = f'"{runner}" "{Path(__file__).resolve()}"'
    subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "MINUTE", "/MO", str(minutes), "/TR", action],
        check=True,
    )
    print(f'Scheduled "{TASK_NAME}" every {minutes} minutes. Log: {LOG_FILE}')
    return 0


def uninstall() -> int:
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], check=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Post WoW Forever news and blue posts from Wowhead to Discord.")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="print what would be posted, change nothing")
    group.add_argument("--test", action="store_true", help="post the newest article and blue post now")
    group.add_argument("--install", nargs="?", type=int, const=10, metavar="MIN",
                       help="run every MIN minutes with Task Scheduler (default 10)")
    group.add_argument("--uninstall", action="store_true", help="remove the scheduled task")
    args = ap.parse_args()

    if args.install is not None:
        load_config(need_webhook=True)
        return install(args.install)
    if args.uninstall:
        return uninstall()
    cfg = load_config(need_webhook=not args.dry_run)
    if args.test:
        return send_test(cfg)
    return run(cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
