"""Source adapters: RSS/Atom, HTML listing pages, public Telegram channels, news search (Google/Bing RSS).

Every adapter returns RawDoc with the best known publication date. Parsing functions are pure so they are
tested on fixtures; fetching goes through Client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import unescape
from time import mktime
from typing import Any
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit

import feedparser
from bs4 import BeautifulSoup

from digestkit.collect.base import RawDoc, aware, clean_text, html_to_text
from digestkit.collect.fulltext import extract
from digestkit.collect.http import Client
from digestkit.config import Digest, Source, WatchItem

log = logging.getLogger(__name__)

DEFAULT_MAX = {"rss": 15, "html": 8, "telegram": 15, "news_search": 10, "json": 30, "command": 30}


# ---------------- JSON items (json feeds, APIs, scripts, agents) ----------------
def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if d.get(k):
            return d[k]
    return None


def docs_from_json(data: Any) -> list[RawDoc]:
    """Accepts a list of items, {"items": [...]} (incl. JSON Feed 1.1) or JSON lines already parsed.
    Item fields (aliases in brackets): url [link, external_url], title [headline], text [content_text,
    summary, content_html, description], published_at [date_published, published, date], source [publisher]."""
    if isinstance(data, dict):
        data = data.get("items") or data.get("entries") or data.get("results") or []
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        url = _first(it, "url", "link", "external_url")
        title = _first(it, "title", "headline")
        if not url or not title:
            continue
        text = _first(it, "text", "content_text", "summary", "description") or ""
        if not text and it.get("content_html"):
            text = html_to_text(str(it["content_html"]))
        pub = _first(it, "published_at", "date_published", "published", "date")
        dt = None
        if pub:
            try:
                dt = aware(datetime.fromisoformat(str(pub).replace("Z", "+00:00")))
            except ValueError:
                dt = None
        src = _first(it, "source", "publisher")
        if isinstance(src, dict):
            src = src.get("name") or src.get("title")
        out.append(RawDoc(url=str(url), title=clean_text(str(title)), text=clean_text(str(text)), published_at=dt,
                          source_name=str(src or ""), watch=[str(w) for w in it.get("watch", []) or []]))
    return out


def parse_json_items(raw: str) -> list[RawDoc]:
    raw = raw.strip()
    if not raw:
        return []
    try:
        return docs_from_json(json.loads(raw))
    except json.JSONDecodeError:
        return docs_from_json([json.loads(ln) for ln in raw.splitlines() if ln.strip()])


async def run_command(cmd: str, timeout: float = 120) -> list[RawDoc]:
    """Run the user's own command (from their YAML) and read items from its stdout."""
    proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"command exited {proc.returncode}: {err.decode(errors='replace')[:200]}")
    return parse_json_items(out.decode("utf-8", errors="replace"))


# ---------------- RSS ----------------
def parse_feed(content: bytes | str) -> list[RawDoc]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime.fromtimestamp(mktime(ts), tz=UTC) if ts else None
        body = e.content[0].get("value", "") if e.get("content") else ""
        body = body or e.get("summary", "")
        out.append(RawDoc(url=link, title=clean_text(unescape(e.get("title", ""))), text=html_to_text(body),
                          published_at=dt))
    out.sort(key=lambda d: d.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out


def is_feed(content: bytes | str) -> bool:
    f = feedparser.parse(content)
    return bool(f.entries) and bool(f.get("version"))


def feed_links(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for ln in soup.find_all("link", attrs={"rel": "alternate"}):
        t = str(ln.get("type") or "")
        if "rss" in t or "atom" in t:
            out.append(urljoin(base, str(ln.get("href"))))
    return out


async def _fill_fulltext(docs: list[RawDoc], client: Client, src: Source, min_chars: int = 600) -> None:
    async def one(d: RawDoc) -> None:
        if len(d.text) >= min_chars:
            return
        try:
            r = await client.get(d.url)
            if r.status_code != 200:
                return
            ex = extract(r.text, str(r.url), src.selectors.get("body"))
            if len(ex.text) > len(d.text):
                d.text = ex.text
            d.published_at = d.published_at or ex.published_at
        except Exception as e:  # feed teaser stays
            log.info("fulltext failed %s: %s", d.url, e)

    await asyncio.gather(*(one(d) for d in docs))


# ---------------- HTML listing ----------------
def list_links(html: str, base_url: str, selectors: dict) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    anchors = soup.select(selectors["list"]) if selectors.get("list") else soup.find_all("a", href=True)
    pattern = re.compile(selectors["link_pattern"]) if selectors.get("link_pattern") else None
    found: dict[str, str] = {}
    for a in anchors:
        href = str(a.get("href") or "")
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue
        p = urlsplit(urljoin(base_url, href))
        url = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
        if pattern and not pattern.search(url):
            continue
        title = a.get_text(" ", strip=True)
        if url not in found or len(title) > len(found[url]):
            found[url] = title
    return list(found.items())


def guess_link_pattern(html: str, base_url: str) -> str | None:
    """Most common 'section/' prefix among same-site links with a long slug: a decent default for blogs."""
    host = urlsplit(base_url).netloc
    counts: dict[str, int] = {}
    for url, title in list_links(html, base_url, {}):
        p = urlsplit(url)
        parts = [x for x in p.path.split("/") if x]
        if p.netloc != host or len(parts) < 2 or len(parts[-1]) < 12 or len(title) < 20:
            continue
        prefix = "/" + "/".join(parts[:-1]) + "/"
        counts[prefix] = counts.get(prefix, 0) + 1
    if not counts:
        return None
    prefix, n = max(counts.items(), key=lambda kv: kv[1])
    if n < 3:
        return None
    return "^https?://" + re.escape(host) + re.escape(prefix) + r"[^/]{8,}/?$"


# ---------------- Telegram ----------------
def channel_of(url: str) -> str:
    path = urlsplit(url).path.strip("/")
    return path.split("/")[-1] if path.startswith("s/") else path.split("/")[0]


def parse_channel(html: str) -> list[RawDoc]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        body = msg.select_one(".tgme_widget_message_text")
        if body is None:
            continue
        for br in body.find_all("br"):
            br.replace_with("\n")
        text = clean_text(body.get_text())
        if len(text) < 40:
            continue
        t = msg.select_one("time[datetime]")
        dt = aware(datetime.fromisoformat(str(t["datetime"]))) if t else None
        first = text.split("\n", 1)[0]
        out.append(RawDoc(url=f"https://t.me/{msg['data-post']}", title=first if len(first) <= 160 else first[:157] + "…",
                          text=text, published_at=dt))
    out.sort(key=lambda d: d.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out


# ---------------- News search ----------------
def search_url(provider: str, query: str, hl: str, gl: str, days: int) -> str:
    if provider == "bing":
        return f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&setlang={hl}&cc={gl}"
    q = quote_plus(f"{query} when:{days}d")
    return f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{hl}"


def parse_search(content: bytes | str) -> list[RawDoc]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        link = str(e.get("link") or "")
        if "bing.com" in link and "url=" in link:  # Bing wraps target URLs
            link = parse_qs(urlsplit(link).query).get("url", [link])[0]
        title = clean_text(unescape(e.get("title", "")))
        publisher = ""
        src = e.get("source")
        if src is not None and hasattr(src, "get"):
            publisher = str(src.get("title") or "")
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        ts = e.get("published_parsed") or e.get("updated_parsed")
        dt = datetime.fromtimestamp(mktime(ts), tz=UTC) if ts else None
        snippet = html_to_text(e.get("summary", ""))
        if snippet.startswith(title):  # Google repeats the title in the description
            snippet = ""
        out.append(RawDoc(url=link, title=title, text=snippet or title, published_at=dt, source_name=publisher))
    return out


def expand_queries(src: Source, watchlist: list[WatchItem]) -> list[tuple[str, list[str]]]:
    q = src.query or ""
    if "{watch}" not in q:
        return [(q, [])]
    return [(q.replace("{watch}", w.query or f'"{w.name}"'), [w.name]) for w in watchlist]


# ---------------- dispatch ----------------
async def fetch(src: Source, client: Client, digest: Digest, watchlist: list[WatchItem], window_hours: int,
                known: Callable[[str], bool] | None = None, now: datetime | None = None) -> list[RawDoc]:
    """`known(url)` -> True for URLs already stored: they are not downloaded again (no article page fetch)."""
    kind = src.kind
    limit = src.max_items or DEFAULT_MAX.get(kind, 12)
    edge = (now or datetime.now(UTC)) - timedelta(hours=window_hours)

    def fresh_unknown(docs: list[RawDoc]) -> list[RawDoc]:
        """Only items worth a full-text download: not stored yet and not obviously old."""
        return [d for d in docs if not (known and known(d.url)) and not (d.published_at and d.published_at < edge)]

    if kind == "news_search":
        days = max(1, math.ceil(window_hours / 24))
        docs: list[RawDoc] = []
        for query, watch in expand_queries(src, watchlist):
            url = search_url(digest.news_search.provider, query, digest.news_search.hl, digest.news_search.gl, days)
            r = await client.get(url)
            r.raise_for_status()
            found = parse_search(r.content)[:limit]
            for d in found:
                d.watch = list(watch)
            docs += found
        if digest.news_search.provider == "bing" and src.fulltext:
            await _fill_fulltext(fresh_unknown(docs), client, src)
        return docs
    if kind == "command":
        return (await run_command(src.command or ""))[:limit]
    if kind == "json":
        r = await client.get(src.feed_url or src.url or "")
        r.raise_for_status()
        docs = parse_json_items(r.text)[:limit]
        if src.fulltext:
            await _fill_fulltext(fresh_unknown(docs), client, src)
        return docs
    if kind == "telegram":
        r = await client.get(f"https://t.me/s/{channel_of(src.url or '')}")
        r.raise_for_status()
        return parse_channel(r.text)[:limit]

    url = src.feed_url or src.url or ""
    r = await client.get(url)
    r.raise_for_status()
    if kind in ("rss", "auto") and (kind == "rss" or is_feed(r.content)):
        docs = parse_feed(r.content)[:limit]
        if src.fulltext:
            await _fill_fulltext(fresh_unknown(docs), client, src)
        return docs
    if kind == "auto":  # a page: try its feed first
        for f in feed_links(r.text, str(r.url)):
            try:
                fr = await client.get(f)
                if fr.status_code == 200 and is_feed(fr.content):
                    docs = parse_feed(fr.content)[:limit]
                    if src.fulltext:
                        await _fill_fulltext(fresh_unknown(docs), client, src)
                    return docs
            except Exception:
                continue
    sel = dict(src.selectors)
    if not sel.get("link_pattern") and not sel.get("list"):
        guessed = guess_link_pattern(r.text, str(r.url))
        if not guessed:
            raise ValueError("no feed and no article links recognised; set selectors.link_pattern")
        sel["link_pattern"] = guessed
    links = [(u, t) for u, t in list_links(r.text, str(r.url), sel)[:limit] if not (known and known(u))]

    async def one(link: str, anchor: str) -> RawDoc | None:
        try:
            ar = await client.get(link)
            if ar.status_code != 200:
                return None
        except Exception:
            return None
        ex = extract(ar.text, link, sel.get("body"))
        if len(ex.text) < 150:
            return None
        return RawDoc(url=link, title=ex.title or anchor, text=ex.text, published_at=ex.published_at)

    return [d for d in await asyncio.gather(*(one(u, t) for u, t in links)) if d]


async def detect(url: str, client: Client) -> dict:
    """Figure out how to read a URL: returns a Source-like dict (kind, url, feed_url, selectors) + sample."""
    if "t.me/" in url:
        r = await client.get(f"https://t.me/s/{channel_of(url)}")
        docs = parse_channel(r.text)
        return {"kind": "telegram", "url": url, "sample": docs[:3], "count": len(docs)}
    r = await client.get(url)
    if is_feed(r.content):
        docs = parse_feed(r.content)
        return {"kind": "rss", "url": url, "feed_url": url, "sample": docs[:3], "count": len(docs)}
    for f in feed_links(r.text, str(r.url)):
        try:
            fr = await client.get(f)
            if is_feed(fr.content):
                docs = parse_feed(fr.content)
                return {"kind": "rss", "url": url, "feed_url": f, "sample": docs[:3], "count": len(docs)}
        except Exception:
            continue
    pat = guess_link_pattern(r.text, str(r.url))
    if pat:
        links = list_links(r.text, str(r.url), {"link_pattern": pat})
        return {"kind": "html", "url": url, "selectors": {"link_pattern": pat},
                "sample": [RawDoc(url=u, title=t, text="") for u, t in links[:3]], "count": len(links)}
    return {"kind": None, "url": url, "sample": [], "count": 0}
