"""Weekly and monthly contours: no new collection, a look back over the memory.

The pack groups reported stories into threads (a story and its continuations over time), with the full
timeline of facts, reader ratings, what the system changed on its own, and (monthly) the weekly reviews.
The agent writes an analytical review in Markdown; publish_review() stores and delivers it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from digestkit import paths, render
from digestkit.config import Digest
from digestkit.store import Store, iso, jload

PERIOD_DAYS = {"weekly": 7, "monthly": 30}


def build_pack(store: Store, d: Digest, cadence: str, now: datetime) -> dict[str, Any]:
    days = PERIOD_DAYS[cadence]
    start = now - timedelta(days=days)
    blocks_out = []
    for b in d.blocks:
        events = store.q(
            """SELECT e.*, s.thread_id, s.title AS story_title FROM story_events e JOIN stories s ON s.id=e.story_id
               WHERE s.digest=? AND s.block=? AND e.at>=? ORDER BY e.at""", (d.id, b.id, iso(start)))
        thread_ids = list(dict.fromkeys((e["thread_id"] or e["story_id"]) for e in events))
        threads: list[dict[str, Any]] = []
        for t in thread_ids:
            stories = store.q("SELECT * FROM stories WHERE digest=? AND (thread_id=? OR id=?) AND status='reported' ORDER BY reported_at",
                              (d.id, t, t))
            sids = [s["id"] for s in stories]
            if not sids:
                continue
            tl = store.q("SELECT * FROM story_events WHERE story_id IN ({}) ORDER BY at".format(",".join("?" * len(sids))), sids)
            timeline = [{"date": e["at"][:10], "kind": e["kind"], "story": f"s{e['story_id']}", "text": e["text"],
                         "facts": jload(e["facts"], []), "in_period": e["at"] >= iso(start)} for e in tl]
            # keep context compact: everything in the period + up to 3 earlier events
            early = [x for x in timeline if not x["in_period"]][-3:]
            timeline = early + [x for x in timeline if x["in_period"]]
            threads.append({
                "thread": f"t{t}",
                "stories": [{"story": f"s{s['id']}", "title": s["title"], "event_date": s["event_date"],
                             "first_reported": (s["reported_at"] or "")[:10], "rating": s["rating"],
                             "watch": jload(s["watch"], [])} for s in stories],
                "events_in_period": sum(1 for x in timeline if x["in_period"]),
                "timeline": timeline,
            })
        threads.sort(key=lambda x: (-x["events_in_period"], -len(x["stories"])))
        skipped = {r["reason"]: r["n"] for r in store.q(
            "SELECT reason, COUNT(*) AS n FROM stories WHERE digest=? AND block=? AND status='skipped' AND created_at>=? GROUP BY reason",
            (d.id, b.id, iso(start)))}
        notes = [r["text"] for r in store.q(
            "SELECT text FROM feedback WHERE digest=? AND (block=? OR block IS NULL) AND text!='' AND created_at>=?",
            (d.id, b.id, iso(start)))]
        blocks_out.append({"id": b.id, "title": b.title, "brief": b.brief,
                           "watchlist": [w.name for w in b.watchlist], "threads": threads,
                           "skipped_counts": skipped, "reader_notes": notes})
    changes = [f"{r['at'][:10]}: {r['text']}" for r in store.q(
        "SELECT * FROM changes WHERE digest=? AND at>=? ORDER BY at", (d.id, iso(start)))]
    dailies = store.one("SELECT COUNT(*) AS n FROM issues WHERE digest=? AND cadence='daily' AND created_at>=?", (d.id, iso(start)))
    earlier: list[dict[str, Any]] = []
    if cadence == "monthly":
        earlier = [{"date": r["created_at"][:10], "markdown": r["body"][:6000]} for r in store.q(
            "SELECT * FROM issues WHERE digest=? AND cadence='weekly' AND created_at>=? ORDER BY created_at", (d.id, iso(start)))]
    else:
        prev = store.one("SELECT * FROM issues WHERE digest=? AND cadence='weekly' AND created_at<? ORDER BY created_at DESC LIMIT 1",
                         (d.id, iso(start + timedelta(days=1))))
        if prev:
            earlier = [{"date": prev["created_at"][:10], "markdown": prev["body"][:4000]}]
    pack_id = store.x("INSERT INTO packs(digest, cadence, created_at, path) VALUES (?,?,?,?)", (d.id, cadence, iso(now), ""))
    pack = {
        "pack_id": pack_id, "digest": d.id, "cadence": cadence, "title": d.title,
        "period": {"from": render.local_date(start, d.timezone), "to": render.local_date(now, d.timezone)},
        "language": d.language, "reader": d.reader, "style": d.style,
        "daily_issues_in_period": dailies["n"] if dailies else 0,
        "instructions": "Run `digest prompt review` and follow it. Write Markdown, then `digest publish-review`.",
        "blocks": blocks_out, "system_changes": changes, "earlier_reviews": earlier,
    }
    path = paths.packs_dir(d.id) / f"{render.local_date(now, d.timezone)}-{cadence}-{pack_id}.json"
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    store.x("UPDATE packs SET path=? WHERE id=?", (str(path), pack_id))
    store.commit()
    pack["path"] = str(path)
    return pack


def publish_review(store: Store, d: Digest, pack_id: int, markdown: str, now: datetime, deliver: bool = True) -> dict[str, Any]:
    pack = store.one("SELECT * FROM packs WHERE id=? AND digest=?", (pack_id, d.id))
    if pack is None or pack["cadence"] not in PERIOD_DAYS:
        raise ValueError(f"review pack {pack_id} not found for digest {d.id}")
    if pack["status"] != "open":
        raise ValueError(f"pack {pack_id} is {pack['status']}")
    cadence = pack["cadence"]
    issue_id = store.x("INSERT INTO issues(digest, cadence, created_at, period_start, period_end, body, pack_id) VALUES (?,?,?,?,?,?,?)",
                       (d.id, cadence, iso(now), iso(now - timedelta(days=PERIOD_DAYS[cadence])), iso(now), markdown, pack_id))
    body = markdown.rstrip() + f"\n\n<!-- digest:{d.id} issue:{issue_id} -->\n"
    path = paths.out_dir(d.id) / f"{render.local_date(now, d.timezone)}-{cadence}.md"
    path.write_text(body, encoding="utf-8")
    period = f"{render.local_date(now - timedelta(days=PERIOD_DAYS[cadence]), d.timezone)} – {render.local_date(now, d.timezone)}"
    subject = render.review_subject(markdown, d, cadence, period)
    path.with_suffix(".html").write_text(
        render.review_html(body, d, d.title, render.human_date(now, d), d.id, issue_id), encoding="utf-8")
    store.x("UPDATE issues SET path=?, body=?, subject=? WHERE id=?", (str(path), body, subject, issue_id))
    store.x("UPDATE packs SET status='published' WHERE id=?", (pack_id,))
    store.commit()
    delivered: list[str] = []
    if deliver:
        from digestkit.deliver import deliver_issue

        delivered = deliver_issue(store, d, issue_id)
    return {"issue_id": issue_id, "path": str(path), "delivered": delivered}


def thread_stats(store: Store, d: Digest) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in store.q("SELECT status FROM stories WHERE digest=?", (d.id,)):
        out[r["status"]] += 1
    return dict(out)
