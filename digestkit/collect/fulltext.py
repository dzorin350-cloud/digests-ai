"""Article text extraction (trafilatura, BeautifulSoup fallback)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import trafilatura
from bs4 import BeautifulSoup

from digestkit.collect.base import clean_text


@dataclass
class Extracted:
    title: str
    text: str
    published_at: datetime | None


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def extract(html: str, url: str, body_selector: str | None = None) -> Extracted:
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else "") or ""
    date = _parse_date(meta.date if meta else None)
    text = ""
    if body_selector:
        node = BeautifulSoup(html, "lxml").select_one(body_selector)
        if node:
            text = node.get_text("\n", strip=True)
    if not text:
        text = trafilatura.extract(html, url=url, include_comments=False, favor_recall=True) or ""
    if not text:
        soup = BeautifulSoup(html, "lxml")
        for t in soup(["script", "style", "nav", "header", "footer"]):
            t.decompose()
        node = soup.find("article") or soup.body
        text = node.get_text("\n", strip=True) if node else ""
    return Extracted(title=title.strip(), text=clean_text(text), published_at=date)
