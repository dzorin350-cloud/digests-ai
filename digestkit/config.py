"""Digest config: one YAML file per digest (digests/<id>.yaml). See docs/CONFIG.md."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from digestkit import paths

CadenceName = Literal["daily", "weekly", "monthly"]
CADENCES: tuple[CadenceName, ...] = ("daily", "weekly", "monthly")
SourceKind = Literal["auto", "rss", "html", "telegram", "news_search", "json", "command"]


class Source(BaseModel):
    """One place to read from. `kind: auto` is resolved on fetch (feed / telegram / html page)."""

    id: str = ""
    kind: SourceKind = "auto"
    url: str | None = None
    feed_url: str | None = None
    query: str | None = None  # news_search: search query; "{watch}" expands to every watchlist item
    command: str | None = None  # command: shell command printing items as JSON (see docs/CONFIG.md)
    name: str | None = None
    trust: float = 0.7
    selectors: dict[str, Any] = Field(default_factory=dict)  # html: link_pattern (regex) and/or list (css)
    fulltext: bool = True  # download article pages for feeds that only carry a teaser
    max_items: int | None = None
    enabled: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def _fill(self) -> Source:
        if self.kind == "auto" and self.url and "t.me/" in self.url:
            self.kind = "telegram"
        if self.kind == "news_search" and not self.query:
            raise ValueError("news_search source needs `query`")
        if self.kind == "command" and not self.command:
            raise ValueError("command source needs `command`")
        if self.kind not in ("news_search", "command") and not (self.url or self.feed_url):
            raise ValueError("source needs `url` or `feed_url`")
        if not self.id:
            self.id = make_source_id(self)
        if not self.name:
            self.name = (self.query if self.kind == "news_search" else self.id if self.kind == "command"
                         else (urlsplit(self.url or self.feed_url or "").hostname or self.id))
        return self


def make_source_id(s: Source) -> str:
    if s.kind == "news_search":
        return "search-" + hashlib.sha1((s.query or "").encode()).hexdigest()[:8]
    if s.kind == "command":
        return "cmd-" + hashlib.sha1((s.command or "").encode()).hexdigest()[:8]
    u = urlsplit(s.url or s.feed_url or "")
    host = (u.hostname or "src").removeprefix("www.")
    if "t.me" in host:
        return "tg-" + u.path.strip("/").split("/")[-1].lower()
    tail = re.sub(r"[^a-z0-9]+", "-", u.path.lower()).strip("-")[:30]
    return re.sub(r"[^a-z0-9.-]+", "-", host) + (f"-{tail}" if tail else "")


class WatchItem(BaseModel):
    """Something the reader holds or follows: a bond, a company, a product, a person."""

    name: str
    aliases: list[str] = Field(default_factory=list)  # word stems are fine: "Полипласт" matches "Полипласта"
    query: str | None = None  # search query for news_search "{watch}" (default: the name)
    note: str | None = None

    def terms(self) -> list[str]:
        return list(dict.fromkeys([self.name, *self.aliases]))


class Block(BaseModel):
    id: str
    title: str
    brief: str  # what matters to the reader here, in plain words; the main instruction for selection
    ref: str = ""  # letter used in item refs (E1, E2...), default: first letter of id
    include: list[str] = Field(default_factory=list)  # hint words (boost, not a filter)
    exclude: list[str] = Field(default_factory=list)  # items mentioning any of these are dropped by code
    watchlist: list[WatchItem] = Field(default_factory=list)
    require_watch_match: bool = False  # keep only items that mention a watchlist item
    max_items: int = 5  # hard cap for the daily issue
    show_quiet_watch: bool = False  # one line per watchlist item even without news ("Брусника — новостей нет")
    expert_comments: bool = False  # add attributed analyst/expert views found in the sources, next to facts
    color: str | None = None  # card accent colour in the HTML issue (#RRGGBB); default: palette by position
    sources: list[Source] = Field(default_factory=list)

    @field_validator("watchlist", mode="before")
    @classmethod
    def _watch_strings(cls, v: Any) -> Any:
        return [{"name": x} if isinstance(x, str) else x for x in (v or [])]


class Cadence(BaseModel):
    enabled: bool = True
    max_age_hours: int = 48  # daily: older events/publications are not news any more
    weekday: int = 0  # weekly: 0 = Monday
    day: int = 1  # monthly: day of month


class Cadences(BaseModel):
    daily: Cadence = Field(default_factory=Cadence)
    weekly: Cadence = Field(default_factory=Cadence)
    monthly: Cadence = Field(default_factory=Cadence)


class Delivery(BaseModel):
    file: bool = True  # always write out/<id>/<date>-<cadence>.md
    email: list[str] = Field(default_factory=list)
    telegram_chat_id: str | None = None
    send_empty: bool = True  # also send a day with no news ("nothing new" is information too)


class Recipient(BaseModel):
    """Someone who gets (a part of) the digest: own blocks, contours and channels; memory stays shared."""

    name: str
    email: list[str] = Field(default_factory=list)
    telegram_chat_id: str | None = None
    blocks: list[str] = Field(default_factory=list)  # empty = all blocks
    cadences: list[CadenceName] = Field(default_factory=lambda: list(CADENCES))

    @field_validator("email", mode="before")
    @classmethod
    def _one(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v


class NewsSearch(BaseModel):
    provider: Literal["google", "bing"] = "google"
    hl: str = "ru"
    gl: str = "RU"


class Digest(BaseModel):
    id: str = ""
    title: str
    language: str = "ru"
    timezone: str = "Europe/Moscow"
    reader: str = ""  # who reads this and why: shapes tone and selection
    style: str = ""  # free-form wishes for writing
    remind_after_days: int | None = None  # re-mention a reported story once after N days (None = never)
    blocks: list[Block]
    cadences: Cadences = Field(default_factory=Cadences)
    delivery: Delivery = Field(default_factory=Delivery)
    recipients: list[Recipient] = Field(default_factory=list)  # optional: per-person parts of the digest
    news_search: NewsSearch = Field(default_factory=NewsSearch)
    source_path: str = ""

    @model_validator(mode="after")
    def _refs(self) -> Digest:
        used: set[str] = set()
        for b in self.blocks:
            ref = (b.ref or b.id[:1]).upper()
            if ref in used:
                for c in b.id.upper() + "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                    if c.isalpha() and c not in used:
                        ref = c
                        break
            b.ref = ref
            used.add(ref)
        ids = [b.id for b in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("block ids must be unique")
        for r in self.recipients:
            unknown = set(r.blocks) - set(ids)
            if unknown:
                raise ValueError(f"recipient {r.name}: unknown blocks {sorted(unknown)}")
        return self

    def block(self, block_id: str) -> Block:
        for b in self.blocks:
            if b.id == block_id:
                return b
        raise KeyError(block_id)


def resolve_path(name_or_path: str) -> Path:
    p = Path(name_or_path)
    if p.suffix in (".yaml", ".yml") and p.exists():
        return p.resolve()
    for cand in (paths.digests_dir() / f"{name_or_path}.yaml", paths.digests_dir() / f"{name_or_path}.yml"):
        if cand.exists():
            return cand.resolve()
    raise FileNotFoundError(f"digest config not found: {name_or_path} (looked in {paths.digests_dir()})")


def load(name_or_path: str) -> Digest:
    path = resolve_path(name_or_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data.setdefault("id", path.stem)
    d = Digest.model_validate(data)
    d.source_path = str(path)
    return d
