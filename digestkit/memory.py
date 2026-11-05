"""What the digest remembers about a block: reported stories, reader preferences from feedback."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from digestkit.store import Store, iso, jload


def story_brief(r: sqlite3.Row, facts: int = 6) -> dict[str, Any]:
    return {
        "story": f"s{r['id']}",
        "title": r["title"],
        "status": r["status"] if r["status"] == "reported" else f"skipped:{r['reason']}",
        "first_reported": (r["reported_at"] or "")[:10] or None,
        "last_update": (r["updated_at"] or r["reported_at"] or r["created_at"] or "")[:10],
        "event_date": r["event_date"],
        "facts": jload(r["facts"], [])[-facts:],
        "rating": r["rating"],
    }


def recent_stories(store: Store, digest: str, block: str, since: datetime, status: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM stories WHERE digest=? AND block=? AND COALESCE(updated_at, created_at)>=?"
    args: list[Any] = [digest, block, iso(since)]
    if status:
        sql += " AND status=?"
        args.append(status)
    return store.q(sql + " ORDER BY COALESCE(updated_at, created_at) DESC", args)


def preferences(store: Store, digest: str, block: str, now: datetime) -> dict[str, Any]:
    notes = [r["text"] for r in store.q(
        "SELECT text FROM feedback WHERE digest=? AND (block=? OR block IS NULL) AND text!='' AND created_at>=?"
        " ORDER BY created_at DESC LIMIT 12", (digest, block, iso(now - timedelta(days=90))))]
    liked = [r["title"] for r in store.q(
        "SELECT title FROM stories WHERE digest=? AND block=? AND rating>0 AND created_at>=? ORDER BY created_at DESC LIMIT 8",
        (digest, block, iso(now - timedelta(days=60))))]
    disliked = [r["title"] for r in store.q(
        "SELECT title FROM stories WHERE digest=? AND block=? AND rating<0 AND created_at>=? ORDER BY created_at DESC LIMIT 8",
        (digest, block, iso(now - timedelta(days=60))))]
    out: dict[str, Any] = {}
    if notes:
        out["reader_notes"] = notes
    if liked:
        out["liked"] = liked
    if disliked:
        out["disliked"] = disliked
    return out
