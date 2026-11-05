"""Collection: fetch every runnable source, store new items, drop what is old or off-topic by code."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from digestkit import sources as reg
from digestkit import textsim
from digestkit.collect.base import RawDoc, aware

__all__ = ["RawDoc", "add_items", "classify_doc", "collect", "store_docs"]
from digestkit.config import Block, Digest, Source
from digestkit.store import Store, iso, jdump

log = logging.getLogger(__name__)

Fetcher = Callable[[Source, Block], Awaitable[list[RawDoc]]]
MAX_TEXT = 6000


def store_docs(store: Store, d: Digest, block_id: str, source_id: str, docs: list[RawDoc], now: datetime,
               first_run: bool, counts: dict[str, int]) -> None:
    window_h = d.cadences.daily.max_age_hours
    for doc in docs:
        url = textsim.canonical_url(doc.url)
        if store.one("SELECT 1 FROM items WHERE digest=? AND block=? AND url=?", (d.id, block_id, url)):
            counts["known"] = counts.get("known", 0) + 1
            continue
        state, reason, watch = classify_doc(doc, d.block(block_id), now, window_h, first_run)
        store.x(
            "INSERT INTO items(digest, block, source_id, url, title, text, publisher, published_at, fetched_at, watch, state, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.id, block_id, source_id, url, doc.title.strip()[:500], doc.text[:MAX_TEXT], doc.source_name or None,
             iso(aware(doc.published_at)), iso(now), jdump(watch), state, reason),
        )
        counts[state] = counts.get(state, 0) + 1


def add_items(store: Store, d: Digest, block_id: str, docs: list[RawDoc], now: datetime, source: str = "agent") -> dict[str, int]:
    """Items found by the agent itself (web search, MCP, mail, chats) enter the same pipeline and memory."""
    d.block(block_id)
    sid = f"manual-{source}"
    if not store.one("SELECT 1 FROM sources WHERE digest=? AND block=? AND source_id=?", (d.id, block_id, sid)):
        spec = Source(kind="command", command="(added by digest add-items)", id=sid, name=source)
        store.x("INSERT INTO sources(digest, block, source_id, spec, origin, status, added_at) VALUES (?,?,?,?,?,?,?)",
                (d.id, block_id, sid, spec.model_dump_json(), "discovered", "manual", iso(now)))
    counts: dict[str, int] = {}
    store_docs(store, d, block_id, sid, docs, now, False, counts)
    store.commit()
    return counts


def classify_doc(doc: RawDoc, block: Block, now: datetime, window_h: int, first_run: bool) -> tuple[str, str | None, list[str]]:
    """-> (state, reason, watch). Pure: the code-level filter before any LLM sees an item."""
    blob = f"{doc.title}\n{doc.text}"
    watch = list(doc.watch)
    for w in block.watchlist:
        if w.name not in watch and textsim.mentions(blob, w.terms()):
            watch.append(w.name)
    pub = aware(doc.published_at)
    if pub is not None and pub < now - timedelta(hours=window_h):
        return "old", f"published {pub.date().isoformat()}", watch
    if pub is None and first_run:
        return "baseline", "undated item on the first run of the source", watch
    for term in block.exclude:
        if textsim.mentions(blob, [term]):
            return "filtered", f"exclude: {term}", watch
    if block.require_watch_match and not watch:
        return "filtered", "no watchlist item mentioned", watch
    if len(doc.title.strip()) < 5:
        return "filtered", "no title", watch
    return "new", None, watch


async def collect(store: Store, d: Digest, now: datetime, fetcher: Fetcher | None = None) -> dict[str, Any]:
    reg.sync(store, d, now)
    window_h = d.cadences.daily.max_age_hours
    todo = reg.runnable(store, d, now)
    client = None
    if fetcher is None:
        from digestkit.collect.adapters import fetch
        from digestkit.collect.http import Client

        client = Client()

        async def default_fetch(src: Source, block: Block) -> list[RawDoc]:
            assert client is not None

            def known(url: str) -> bool:
                return store.one("SELECT 1 FROM items WHERE digest=? AND block=? AND url=?",
                                 (d.id, block.id, textsim.canonical_url(url))) is not None

            return await fetch(src, client, d, block.watchlist, window_h, known, now)

        fetcher = default_fetch

    async def run(e: reg.Entry) -> tuple[reg.Entry, list[RawDoc] | None, str | None]:
        try:
            assert fetcher is not None
            return e, await asyncio.wait_for(fetcher(e.source, d.block(e.block)), timeout=120), None
        except Exception as ex:  # one broken source never breaks the run
            return e, None, f"{type(ex).__name__}: {ex}"[:300]

    try:
        results = await asyncio.gather(*(run(e) for e in todo))
    finally:
        if client is not None:
            await client.aclose()

    counts: dict[str, int] = {}
    errors = []
    for e, docs, err in results:
        row = store.one("SELECT last_ok FROM sources WHERE digest=? AND block=? AND source_id=?", (d.id, e.block, e.source.id))
        first_run = row is None or row["last_ok"] is None
        reg.record_run(store, d, e.block, e.source.id, docs is not None, err, now)
        if docs is None:
            errors.append({"block": e.block, "source": e.source.id, "error": err})
            continue
        store_docs(store, d, e.block, e.source.id, docs, now, first_run, counts)
    store.commit()
    return {"sources": len(todo), "errors": errors, "items": counts}
