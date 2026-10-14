#!/usr/bin/env python3
"""
any2fedi — mirror third-party feeds (public Telegram channels, RSS/Atom)
into fediverse accounts on a Mastodon-API compatible server (GoToSocial,
Mastodon, Akkoma, ...).

Model:
    account = one bot account on your instance (one access token)
    source  = one feed (a Telegram channel, an RSS feed, ...)
    an account aggregates any number of sources.

Runs as a oneshot (e.g. from a systemd timer): each run polls every source
once, posts what is new, saves state and exits.

Dependencies: requests, beautifulsoup4, pyyaml.
License: MIT.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import logging
import mimetypes
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup, NavigableString, Tag

__version__ = "1.0.0"

log = logging.getLogger("any2fedi")

HTTP_TIMEOUT = 30
DEFAULT_CONFIG = "/srv/any2fedi/config.yaml"
DEFAULT_STATE_DB = "/srv/any2fedi/state/state.db"

# ---------------------------------------------------------------------------
# User-visible texts. Every string that ends up in a post lives here and can be
# overridden in config.yaml under `texts:` (globally, per account, per source).
# ---------------------------------------------------------------------------

DEFAULT_TEXTS = {
    # Overall layout of a status. Empty blocks are dropped together with
    # the blank lines around them.
    # Placeholders: {text} {forwarded} {media_note} {footer} {hashtags}
    "template": "{text}\n\n{forwarded}\n\n{media_note}\n\n{footer}\n\n{hashtags}",
    # Link to the original. Placeholders: {source} {url}
    "footer": "— {source}: {url}",
    # Shown for reposts. Placeholder: {name}
    "forwarded": "↪ forwarded from: {name}",
    # Shown when the original has video/files/polls that are not copied.
    "unsupported_media": "🎬 video / attachment — see the original",
    # Appended to the body when it is cut to fit max_chars.
    "truncated": "…",
    # RSS: how the entry title is put on top of the body. Placeholder: {title}
    "rss_title": "**{title}**",
}

# Options that may be set at defaults / account level and inherited by sources.
SOURCE_INHERITED = ("video_thumbs", "skip_textless_video", "title_only",
                    "strip_lines", "skip_if")


def merged_texts(*layers: dict | None) -> dict:
    out = dict(DEFAULT_TEXTS)
    for layer in layers:
        if layer:
            out.update({k: v for k, v in layer.items() if v is not None})
    return out


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Item:
    """One post fetched from a source, normalized."""
    uid: str                           # unique id within the source
    url: str                           # link to the original
    text: str                          # body in Markdown
    media: list[str] = field(default_factory=list)   # image URLs
    has_unsupported_media: bool = False             # video, files, polls...
    forwarded_from: str | None = None
    sort_key: tuple = ()


# ---------------------------------------------------------------------------
# HTML -> Markdown (covers what Telegram and typical feeds use)
# ---------------------------------------------------------------------------

_MD_ESCAPE = [
    (re.compile(r"([*`\\])"), r"\\\1"),            # * ` and backslash: always
    (re.compile(r"(?<!\w)_|_(?!\w)"), r"\\_"),      # _ only at word edges (keeps URLs intact)
    (re.compile(r"~~"), r"\\~\\~"),
]


def md_escape(text: str) -> str:
    for rx, repl in _MD_ESCAPE:
        text = rx.sub(repl, text)
    return text


def html_to_markdown(node: Tag | None) -> str:
    if node is None:
        return ""

    def inner(t: Tag) -> str:
        return "".join(conv(c) for c in t.children)

    def wrap(t: Tag, mark: str) -> str:
        # Markdown emphasis can't span lines and can't start/end with a space:
        # wrap every line separately and keep its whitespace outside the markers.
        out = []
        for line in inner(t).split("\n"):
            core = line.strip()
            if not core:
                out.append(line)
                continue
            lead = line[: len(line) - len(line.lstrip())]
            trail = line[len(line.rstrip()):]
            out.append(f"{lead}{mark}{core}{mark}{trail}")
        return "\n".join(out)

    def conv(n) -> str:
        if isinstance(n, NavigableString):
            return md_escape(str(n))
        if not isinstance(n, Tag):
            return ""
        name = n.name
        if name == "br":
            return "\n"
        if name in ("b", "strong"):
            return wrap(n, "**")
        if name == "i" and "emoji" in (n.get("class") or []):
            return n.get_text()                        # telegram emoji wrapper
        if name in ("i", "em"):
            return wrap(n, "*")
        if name in ("s", "del", "strike"):
            return wrap(n, "~~")
        if name == "code":
            return f"`{n.get_text()}`"
        if name == "pre":
            return f"\n```\n{n.get_text()}\n```\n"
        if name == "a":
            href = n.get("href", "")
            s = inner(n)
            if not href or href.startswith("javascript:"):
                return s
            if s.strip().rstrip("/") == href.rstrip("/"):
                return href
            if s.startswith("#") or s.startswith("@"):   # hashtags / mentions
                return s
            return f"[{s}]({href})"
        if name == "blockquote":
            body = inner(n).strip()
            return "\n" + "\n".join("> " + l for l in body.splitlines()) + "\n"
        if name in ("p", "div"):
            return inner(n).strip() + "\n\n"
        if name == "li":
            return "- " + inner(n).strip() + "\n"
        if name == "tg-emoji":                         # custom emoji -> fallback char
            return n.get_text()
        if name in ("img", "script", "style"):
            return ""
        return inner(n)

    text = "".join(conv(c) for c in node.children)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


BG_URL = re.compile(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)")


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class Source:
    """Base class. Subclasses implement fetch() -> items, oldest first."""
    kind = "base"

    def __init__(self, cfg: dict, session: requests.Session | None):
        self.cfg = cfg
        self.session = session
        self.label = cfg.get("label")
        self.texts = merged_texts()          # replaced by the runner

    @property
    def key(self) -> str:
        """Stable id used in the state db. Must not change between versions."""
        raise NotImplementedError

    def fetch(self) -> list[Item]:
        raise NotImplementedError

    def is_newer(self, uid: str, last_uid: str | None) -> bool:
        """Cheap pre-filter; the `posted` table is the real dedup."""
        return True

    def _regexes(self, key: str) -> list[re.Pattern]:
        cache = self.__dict__.setdefault("_rx", {})
        if key not in cache:
            cache[key] = [re.compile(p, re.I | re.M) for p in (self.cfg.get(key) or [])]
        return cache[key]

    def filter(self, items: list[Item]) -> list[Item]:
        """Apply `skip_if` (drop whole posts) and `strip_lines` (drop lines) from config.
        Both are lists of regexes; matched against the Markdown text, case-insensitive."""
        skip, strip = self._regexes("skip_if"), self._regexes("strip_lines")
        out = []
        for it in items:
            if any(rx.search(it.text) for rx in skip):
                log.debug("%s #%s skipped by skip_if", self.key, it.uid)
                continue
            if strip:
                lines = [l for l in it.text.split("\n")
                         if not any(rx.search(l.strip()) for rx in strip)]
                it.text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
            out.append(it)
        return out

    # Telegram ids grow monotonically, so a cursor is meaningful there.
    uses_cursor = False


class TelegramSource(Source):
    """
    Public channel via its web preview https://t.me/s/<channel>.
    No bot, no account, no API keys. Shows the last ~20 posts.
    """
    kind = "telegram"
    uses_cursor = True
    BASE = "https://t.me/s/"

    UNSUPPORTED = (
        ".tgme_widget_message_video_player, .tgme_widget_message_roundvideo_player, "
        ".tgme_widget_message_document_wrap, .tgme_widget_message_poll, "
        ".tgme_widget_message_voice_player, .tgme_widget_message_location_wrap, "
        ".message_media_not_supported_wrap, .message_media_not_supported"
    )
    VIDEO_THUMBS = ".tgme_widget_message_video_thumb, .tgme_widget_message_roundvideo_thumb"

    def __init__(self, cfg, session):
        super().__init__(cfg, session)
        self.channel = str(cfg["channel"]).lstrip("@").strip("/")
        self.channel = re.sub(r"^(https?://)?t\.me/(s/)?", "", self.channel)
        if not self.label:
            self.label = self.channel

    @property
    def key(self) -> str:
        return f"telegram:{self.channel.lower()}"

    def is_newer(self, uid, last_uid):
        return last_uid is None or int(uid) > int(last_uid)

    def fetch(self) -> list[Item]:
        r = self.session.get(self.BASE + self.channel, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        # Without a web preview Telegram redirects to t.me/<channel>
        if "/s/" not in r.url:
            raise RuntimeError(f"@{self.channel} has no public web preview")
        return self.parse(r.text)

    def parse(self, page: str) -> list[Item]:
        soup = BeautifulSoup(page, "html.parser")
        items: list[Item] = []
        for msg in soup.select("div.tgme_widget_message[data-post]"):
            if "service_message" in (msg.get("class") or []):
                continue
            post = msg["data-post"]                       # "channel/123"
            try:
                msg_id = int(post.rsplit("/", 1)[1])
            except (IndexError, ValueError):
                continue

            # Albums are rendered as one block; data-post is the first id.
            media = [m.group(1) for ph in msg.select("a.tgme_widget_message_photo_wrap")
                     if (m := BG_URL.search(ph.get("style", "")))]
            if self.cfg.get("video_thumbs", True):
                for th in msg.select(self.VIDEO_THUMBS):
                    m = BG_URL.search(th.get("style", ""))
                    if m and m.group(1) not in media:
                        media.append(m.group(1))

            unsupported = bool(msg.select(self.UNSUPPORTED))
            text = html_to_markdown(msg.select_one(".tgme_widget_message_text"))
            fwd = msg.select_one(".tgme_widget_message_forwarded_from_name")

            if not text and not media and not unsupported:
                continue        # sticker or other empty message
            if not text and unsupported and self.cfg.get("skip_textless_video", True):
                continue        # bare video/file without a caption

            items.append(Item(
                uid=str(msg_id),
                url=f"https://t.me/{post}",
                text=text,
                media=media,
                has_unsupported_media=unsupported,
                forwarded_from=fwd.get_text(strip=True) if fwd else None,
                sort_key=(msg_id,),
            ))
        items.sort(key=lambda i: i.sort_key)
        return items


class RSSSource(Source):
    """RSS 2.0 or Atom feed. Stdlib XML only."""
    kind = "rss"
    ATOM = "{http://www.w3.org/2005/Atom}"
    MEDIA = "{http://search.yahoo.com/mrss/}"
    CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

    def __init__(self, cfg, session):
        super().__init__(cfg, session)
        self.url = cfg["url"]
        if not self.label:
            self.label = re.sub(r"^https?://(www\.)?", "", self.url).split("/")[0]

    @property
    def key(self) -> str:
        return "rss:" + hashlib.sha1(self.url.encode()).hexdigest()[:16]

    def fetch(self) -> list[Item]:
        r = self.session.get(self.url, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return self.parse(r.content)

    def parse(self, data: bytes) -> list[Item]:
        root = ET.fromstring(data)
        atom = root.findall(f"{self.ATOM}entry")
        items = [self._atom(e) for e in atom] if atom else \
            [self._rss(e) for e in root.findall("./channel/item")]
        items.sort(key=lambda i: i.sort_key)
        return items

    def _body(self, title: str, raw_html: str) -> tuple[str, BeautifulSoup]:
        soup = BeautifulSoup(raw_html or "", "html.parser")
        head = self.texts["rss_title"].format(title=title) if title else ""
        if self.cfg.get("title_only") or not raw_html:
            return head, soup
        body = html_to_markdown(soup)
        if head and not body.startswith(title):
            body = f"{head}\n\n{body}"
        return body, soup

    @staticmethod
    def _images(soup: BeautifulSoup, extra: list[str]) -> list[str]:
        urls = extra + [i["src"] for i in soup.find_all("img") if i.get("src")]
        return list(dict.fromkeys(u for u in urls if u))

    def _rss(self, e) -> Item:
        title = (e.findtext("title") or "").strip()
        link = (e.findtext("link") or "").strip()
        uid = (e.findtext("guid") or link or title).strip()
        raw = e.findtext(f"{self.CONTENT}encoded") or e.findtext("description") or ""
        extra = [x.get("url") for x in e.findall("enclosure")
                 if (x.get("type") or "").startswith("image/")]
        extra += [x.get("url") for x in e.findall(f"{self.MEDIA}content")
                  if x.get("medium") == "image" or (x.get("type") or "").startswith("image/")]
        body, soup = self._body(title, raw)
        ts = 0.0
        try:
            ts = parsedate_to_datetime(e.findtext("pubDate")).timestamp()
        except Exception:
            pass
        return Item(uid=uid, url=link, text=body, media=self._images(soup, extra),
                    sort_key=(ts, uid))

    def _atom(self, e) -> Item:
        A = self.ATOM
        title = (e.findtext(f"{A}title") or "").strip()
        link = next((l.get("href", "") for l in e.findall(f"{A}link")
                     if l.get("rel", "alternate") == "alternate"), "")
        uid = (e.findtext(f"{A}id") or link or title).strip()
        raw = e.findtext(f"{A}content") or e.findtext(f"{A}summary") or ""
        body, soup = self._body(title, raw)
        ts = 0.0
        try:
            stamp = e.findtext(f"{A}published") or e.findtext(f"{A}updated") or ""
            ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        return Item(uid=uid, url=link, text=body, media=self._images(soup, []),
                    sort_key=(ts, uid))


# To add a source type: subclass Source and register it here.
SOURCE_TYPES: dict[str, type[Source]] = {
    TelegramSource.kind: TelegramSource,
    RSSSource.kind: RSSSource,
}


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def render(item: Item, src: Source, opts: dict) -> str:
    """Build the status text. All wording comes from src.texts."""
    t = src.texts
    tags = src.cfg.get("hashtags") or opts.get("hashtags") or []
    blocks = {
        "forwarded": t["forwarded"].format(name=item.forwarded_from) if item.forwarded_from else "",
        "media_note": t["unsupported_media"] if item.has_unsupported_media else "",
        "footer": t["footer"].format(source=src.label, url=item.url),
        "hashtags": " ".join(tags),
    }

    def assemble(body: str) -> str:
        out = t["template"].format(text=body, **blocks)
        return re.sub(r"\n{3,}", "\n\n", out).strip()

    out = assemble(item.text)
    limit = int(opts.get("max_chars", 5000))
    if len(out) > limit:
        overhead = len(assemble("")) + len(t["truncated"]) + 2
        body = item.text[: max(0, limit - overhead)].rstrip() + t["truncated"]
        out = assemble(body)
    return out


# ---------------------------------------------------------------------------
# State (sqlite)
# ---------------------------------------------------------------------------

class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursor (
                account TEXT, source TEXT, last_uid TEXT,
                PRIMARY KEY (account, source));
            CREATE TABLE IF NOT EXISTS posted (
                account TEXT, source TEXT, uid TEXT, status_id TEXT, ts INTEGER,
                PRIMARY KEY (account, source, uid));
        """)

    def has_cursor(self, acc, src):
        return self.db.execute("SELECT 1 FROM cursor WHERE account=? AND source=?",
                               (acc, src)).fetchone() is not None

    def last_uid(self, acc, src):
        row = self.db.execute("SELECT last_uid FROM cursor WHERE account=? AND source=?",
                              (acc, src)).fetchone()
        return row[0] if row else None

    def set_cursor(self, acc, src, uid):
        self.db.execute("INSERT OR REPLACE INTO cursor VALUES (?,?,?)", (acc, src, uid))
        self.db.commit()

    def seen(self, acc, src, uid):
        return self.db.execute("SELECT 1 FROM posted WHERE account=? AND source=? AND uid=?",
                               (acc, src, uid)).fetchone() is not None

    def close(self):
        self.db.close()

    def mark(self, acc, src, uid, status):
        self.db.execute("INSERT OR REPLACE INTO posted VALUES (?,?,?,?,?)",
                        (acc, src, uid, status, int(time.time())))
        self.db.commit()


# ---------------------------------------------------------------------------
# Fediverse client (Mastodon API subset)
# ---------------------------------------------------------------------------

class RateLimited(Exception):
    pass


class Fedi:
    def __init__(self, instance: str, token: str, session: requests.Session,
                 content_type: str = "text/markdown"):
        self.base = instance.rstrip("/")
        self.s = session
        self.h = {"Authorization": f"Bearer {token}"}
        self.content_type = content_type

    def upload(self, url: str, max_bytes: int) -> str | None:
        try:
            r = self.s.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            log.warning("media download failed %s: %s", url, e)
            return None
        if len(r.content) > max_bytes:
            log.warning("media too big (%d bytes), skipped: %s", len(r.content), url)
            return None
        ctype = (r.headers.get("Content-Type", "").split(";")[0]
                 or mimetypes.guess_type(url)[0] or "image/jpeg")
        ext = mimetypes.guess_extension(ctype) or ".jpg"
        resp = self.s.post(f"{self.base}/api/v1/media", headers=self.h,
                           files={"file": ("image" + ext, r.content, ctype)}, timeout=120)
        resp.raise_for_status()
        return resp.json()["id"]

    def post(self, text: str, media_ids: list[str], visibility: str,
             language: str | None, idem: str) -> str:
        data = [("status", text), ("visibility", visibility)]
        if self.content_type:
            data.append(("content_type", self.content_type))
        if language:
            data.append(("language", language))
        data += [("media_ids[]", m) for m in media_ids]
        r = self.s.post(f"{self.base}/api/v1/statuses",
                        headers={**self.h, "Idempotency-Key": idem}, data=data, timeout=60)
        if r.status_code == 429:
            raise RateLimited()
        r.raise_for_status()
        return r.json()["id"]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def build_sources(acc_name: str, acc: dict, global_texts: dict | None,
                  session) -> list[Source]:
    out = []
    for scfg in acc.get("sources") or []:
        scfg = {**{k: acc[k] for k in SOURCE_INHERITED if k in acc}, **scfg}
        cls = SOURCE_TYPES.get(scfg.get("type", "telegram"))
        if not cls:
            log.error("[%s] unknown source type %r", acc_name, scfg.get("type"))
            continue
        src = cls(scfg, session)
        src.texts = merged_texts(global_texts, acc.get("texts"), scfg.get("texts"))
        out.append(src)
    return out


def run(cfg: dict, dry_run: bool = False, only_account: str | None = None,
        fedi_factory=Fedi) -> None:
    state = None if dry_run else State(Path(cfg.get("state_db", DEFAULT_STATE_DB)))
    try:
        _run(cfg, state, dry_run, only_account, fedi_factory)
    finally:
        if state:
            state.close()


def _run(cfg, state, dry_run, only_account, fedi_factory) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = cfg.get("user_agent", f"any2fedi/{__version__}")
    delay = float(cfg.get("post_delay_seconds", 3))
    defaults = cfg.get("defaults") or {}
    global_texts = {**(defaults.get("texts") or {}), **(cfg.get("texts") or {})}

    for acc_name, acc in (cfg.get("accounts") or {}).items():
        if only_account and acc_name != only_account:
            continue
        acc = {**defaults, **acc}
        if acc.get("enabled", True) is False:
            continue
        backfill = int(acc.get("backfill", 0))
        queue: list[tuple[Item, Source]] = []

        for src in build_sources(acc_name, acc, global_texts, session):
            try:
                items = src.filter(src.fetch())
            except Exception as e:
                log.error("[%s] %s fetch failed: %s", acc_name, src.key, e)
                continue

            if dry_run:
                queue += [(it, src) for it in items[-(backfill or 3):]]
                continue

            if not state.has_cursor(acc_name, src.key):
                # New source: don't flood the timeline with its history.
                keep = items[-backfill:] if backfill else []
                skip = items[: len(items) - len(keep)]
                for it in skip:
                    state.mark(acc_name, src.key, it.uid, "skipped-initial")
                state.set_cursor(acc_name, src.key, skip[-1].uid if skip else None)
                log.info("[%s] %s initialized: %d existing skipped, %d to post",
                         acc_name, src.key, len(skip), len(keep))

            last = state.last_uid(acc_name, src.key)
            queue += [(it, src) for it in items
                      if src.is_newer(it.uid, last) and not state.seen(acc_name, src.key, it.uid)]

        if dry_run:
            for it, src in queue:
                text = render(it, src, acc)
                print(f"===== [{acc_name}] {src.key} #{it.uid} | {len(it.media)} image(s)")
                for u in it.media[: int(acc.get("max_media", 4))]:
                    print(f"  image: {u}")
                print(text + "\n")
            continue

        fedi = fedi_factory(cfg["instance"], acc["token"], session,
                            acc.get("content_type", "text/markdown"))
        max_media = int(acc.get("max_media", 4))
        max_bytes = int(acc.get("max_media_bytes", 10 * 1024 * 1024))
        posted = 0
        for it, src in queue:
            idem = hashlib.sha256(f"{acc_name}|{src.key}|{it.uid}".encode()).hexdigest()
            try:
                media_ids = [m for u in it.media[:max_media]
                             if (m := fedi.upload(u, max_bytes))]
                sid = fedi.post(render(it, src, acc), media_ids,
                                acc.get("visibility", "public"), acc.get("language"), idem)
            except RateLimited:
                log.warning("[%s] rate limited, stopping this run", acc_name)
                break
            except requests.HTTPError as e:
                resp = e.response
                code = resp.status_code if resp is not None else 0
                if 400 <= code < 500:
                    # Permanent rejection: skip so one bad post doesn't block the queue.
                    log.error("[%s] %s #%s rejected (%d), skipped: %s", acc_name, src.key,
                              it.uid, code, resp.text[:300] if resp is not None else "")
                    sid = f"rejected-{code}"
                else:
                    log.error("[%s] %s #%s server error %d, retry next run",
                              acc_name, src.key, it.uid, code)
                    break
            except Exception as e:
                log.error("[%s] %s #%s failed: %s, retry next run", acc_name, src.key, it.uid, e)
                break
            state.mark(acc_name, src.key, it.uid, sid)
            if src.uses_cursor:
                state.set_cursor(acc_name, src.key, it.uid)
            if not sid.startswith("rejected"):
                posted += 1
                time.sleep(delay)
        log.info("[%s] %d new, %d posted", acc_name, len(queue), posted)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="any2fedi",
        description="Mirror Telegram channels and RSS/Atom feeds into fediverse accounts")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and print what would be posted; post nothing, touch no state")
    ap.add_argument("--account", help="process only this account")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8")) or {}
    if a.dry_run:
        run(cfg, dry_run=True, only_account=a.account)
        return 0

    lock_path = Path(cfg.get("state_db", DEFAULT_STATE_DB)).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another run is in progress, exiting")
            return 0
        run(cfg, only_account=a.account)
    return 0


if __name__ == "__main__":
    sys.exit(main())
