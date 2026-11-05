from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from bs4 import BeautifulSoup


@dataclass
class RawDoc:
    url: str
    title: str
    text: str
    published_at: datetime | None = None
    source_name: str = ""  # publisher, when the source is an aggregator (news search)
    watch: list[str] = field(default_factory=list)  # watchlist items the doc was searched for


_WS = re.compile(r"[ \t ]+")


def clean_text(text: str) -> str:
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    return clean_text(BeautifulSoup(html, "lxml").get_text("\n"))


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
