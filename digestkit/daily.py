"""Daily contour.

build_pack(): new items -> drop reprints of known stories (code) -> group the rest into candidate stories ->
attach memory (related known stories with their facts and dates, reader preferences) -> pack JSON.

publish(): the agent's (or LLM's) decisions -> memory ledger (stories, facts, threads) -> rendered issue.
Candidates the decisions do not mention are remembered as "seen, not selected": they never come back.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field, ValidationError, field_validator

from digestkit import memory, paths, render, textsim
from digestkit import sources as reg
from digestkit.config import Block, Digest
from digestkit.store import Store, iso, jdump, jload, parse_iso
from digestkit.vectors import Vectorizer

log = logging.getLogger(__name__)

DUP_LOOKBACK_DAYS = 30
REPRINT_CHUNK = 2000
PREFILTER_DIM = 512
RELATED_LOOKBACK_DAYS = 45
THREAD_LOOKBACK_DAYS = 90
DUP_CONTAINMENT = 0.6


# ---------------------------------------------------------------- pack
def _abandon_open(store: Store, d: Digest) -> None:
    for p in store.q("SELECT id FROM packs WHERE digest=? AND cadence='daily' AND status='open'", (d.id,)):
        store.x("UPDATE items SET state='new', pack_id=NULL WHERE pack_id=? AND state='pending'", (p["id"],))
        store.x("UPDATE packs SET status='abandoned' WHERE id=?", (p["id"],))


def _age_out(store: Store, d: Digest, now: datetime) -> None:
    edge = iso(now - timedelta(hours=d.cadences.daily.max_age_hours))
    store.x("UPDATE items SET state='old', reason='aged out before publishing' WHERE digest=? AND state='new'"
            " AND COALESCE(published_at, fetched_at) < ?", (d.id, edge))


def _title_key(title: str) -> str | None:
    """Same headline = same news, but only for specific headlines (not "Доброе утро" or "Новости дня")."""
    st = textsim.stems(title)
    return " ".join(st) if len(st) >= 4 else None


def _sig(r: sqlite3.Row) -> set[int]:
    return textsim.shingles(f"{r['title']}\n{r['text'][:1500]}")


def _mark_reprints(store: Store, d: Digest, block: Block, new: list[sqlite3.Row], now: datetime) -> list[sqlite3.Row]:
    """Items that re-tell an item of a known story (same text or same headline) join that story silently."""
    known = store.q(
        "SELECT * FROM items WHERE digest=? AND block=? AND story_id IS NOT NULL AND fetched_at>=?",
        (d.id, block.id, iso(now - timedelta(days=DUP_LOOKBACK_DAYS))))
    if not known or not new:
        return new
    nv = np.stack([textsim.hashed_vec(r["title"], r["text"], PREFILTER_DIM) for r in new])
    # known items: small cached vectors (computed once per item), compared in chunks
    best: list[list[tuple[float, int]]] = [[] for _ in new]
    for start in range(0, len(known), REPRINT_CHUNK):
        chunk = known[start:start + REPRINT_CHUNK]
        ids = [int(r["id"]) for r in chunk]
        have = store.get_vecs("itemh", ids, "h512", np.float16)
        for r in chunk:
            if int(r["id"]) not in have:
                v = textsim.hashed_vec(r["title"], r["text"], PREFILTER_DIM)
                store.put_vec("itemh", int(r["id"]), "h512", v, np.float16)
                have[int(r["id"])] = v
        kv = np.stack([have[i] for i in ids])
        sims = nv @ kv.T
        for i in range(len(new)):
            top = np.argsort(-sims[i])[:8]
            best[i] += [(float(sims[i, j]), start + int(j)) for j in top if sims[i, j] >= 0.3]
    known_titles = {_title_key(r["title"]): r for r in known if _title_key(r["title"])}
    keep = []
    for i, r in enumerate(new):
        key = _title_key(r["title"])
        hit = known_titles.get(key) if key else None
        if hit is None:
            sig = _sig(r)
            for _, j in sorted(best[i], reverse=True)[:8]:
                if textsim.containment(sig, _sig(known[j])) >= DUP_CONTAINMENT:
                    hit = known[j]
                    break
        if hit is not None:
            store.x("UPDATE items SET state='dup', reason=?, story_id=? WHERE id=?",
                    (f"reprint of item {hit['id']}", hit["story_id"], r["id"]))
        else:
            keep.append(r)
    return keep


def _cluster(rows: list[sqlite3.Row], mat: np.ndarray, threshold: float) -> list[list[int]]:
    parent = list(range(len(rows)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    if len(rows) > 1:
        sim = mat @ mat.T
        for a, b in zip(*np.where(np.triu(sim, 1) >= threshold), strict=True):
            same_src = rows[a]["source_id"] == rows[b]["source_id"]
            if same_src and sim[a, b] < 0.9:  # one source writes different news in similar words
                continue
            parent[find(int(a))] = find(int(b))
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    return list(groups.values())


def _trust(d: Digest, block: Block) -> dict[str, float]:
    return {s.id: s.trust for s in block.sources}


def _source_names(store: Store, d: Digest) -> dict[str, str]:
    out = {}
    for r in store.q("SELECT source_id, spec FROM sources WHERE digest=?", (d.id,)):
        out[r["source_id"]] = jload(r["spec"], {}).get("name") or r["source_id"]
    return out


def item_source(r: sqlite3.Row, names: dict[str, str]) -> str:
    return r["publisher"] or names.get(r["source_id"], r["source_id"])


def build_pack(store: Store, d: Digest, now: datetime, max_candidates: int | None = None) -> dict[str, Any]:
    _abandon_open(store, d)
    _age_out(store, d, now)
    vz = Vectorizer(store)
    names = _source_names(store, d)
    trust_all: dict[str, float] = {}
    for b in d.blocks:
        trust_all.update(_trust(d, b))
    pack_id = store.x("INSERT INTO packs(digest, cadence, created_at, path) VALUES (?,?,?,?)", (d.id, "daily", iso(now), ""))
    cmap: dict[str, dict[str, Any]] = {}
    blocks_out = []
    n = 0
    for block in d.blocks:
        new = store.q("SELECT * FROM items WHERE digest=? AND block=? AND state='new' ORDER BY id", (d.id, block.id))
        new = _mark_reprints(store, d, block, new, now)
        stories = memory.recent_stories(store, d.id, block.id, now - timedelta(days=RELATED_LOOKBACK_DAYS))
        mat, smat = vz.pair("item", new, "story", stories)
        groups = _cluster(new, mat, vz.th.cluster)

        def gscore(g: list[int], new: list[sqlite3.Row] = new, block: Block = block) -> float:
            rows = [new[i] for i in g]
            srcs = {r["publisher"] or r["source_id"] for r in rows}
            watch = any(jload(r["watch"], []) for r in rows)
            inc = sum(textsim.mentions(f"{r['title']} {r['text'][:800]}", block.include) for r in rows[:1]) if block.include else 0
            return len(srcs) + max(trust_all.get(r["source_id"], 0.6) for r in rows) + (1.0 if watch else 0) + 0.5 * inc

        groups.sort(key=gscore, reverse=True)
        cap = max_candidates or min(30, max(15, block.max_items * 4))
        for g in groups[cap:]:
            for i in g:
                store.x("UPDATE items SET state='overflow', reason='below candidate cap' WHERE id=?", (new[i]["id"],))
        groups = groups[:cap]

        cands = []
        for g in groups:
            rows = sorted((new[i] for i in g), key=lambda r: (-trust_all.get(r["source_id"], 0.6), -len(r["text"])))
            n += 1
            cid = f"c{n}"
            watch = list(dict.fromkeys(w for r in rows for w in jload(r["watch"], [])))
            related: list[dict[str, Any]] = []
            if stories:
                centroid = mat[g].mean(axis=0)
                centroid /= max(float(np.linalg.norm(centroid)), 1e-9)
                sims = smat @ centroid
                order = list(np.argsort(-sims))
                picked = [j for j in order if sims[j] >= vz.th.related][:3]
                if watch:  # same company / bond / product: always show its recent history
                    for j in order:
                        if len(picked) >= 4:
                            break
                        if j not in picked and set(jload(stories[j]["watch"], [])) & set(watch) and sims[j] >= vz.th.related / 3:
                            picked.append(j)
                for j in picked:
                    rb = memory.story_brief(stories[j])
                    rb["similarity"] = round(float(sims[j]), 2)
                    related.append(rb)
            main = rows[0]
            dates = sorted({(r["published_at"] or r["fetched_at"])[:10] for r in rows})
            cands.append({
                "id": cid,
                "headline": main["title"],
                "published": dates,
                "date_hints": textsim.date_hints(f"{main['title']} {main['text'][:2000]}", parse_iso(main["published_at"]) or now),
                "watch": watch,
                "n_sources": len({item_source(r, names) for r in rows}),
                "items": [{"source": item_source(r, names), "url": r["url"], "title": r["title"],
                           "published_at": r["published_at"],
                           "text": textsim.lead(r["text"], 1200 if k == 0 else 300)} for k, r in enumerate(rows[:5])],
                "related": related,
            })
            cmap[cid] = {"block": block.id, "items": [r["id"] for r in rows]}
            for r in rows:
                store.x("UPDATE items SET state='pending', pack_id=? WHERE id=?", (pack_id, r["id"]))
        reminders = _reminders(store, d, block, now)
        for rm in reminders:
            cmap[rm["id"]] = {"block": block.id, "items": [], "reminder": int(rm["story"][1:])}
        recent = [memory.story_brief(r, facts=2) for r in memory.recent_stories(
            store, d.id, block.id, now - timedelta(days=14), status="reported")[:25]]
        blocks_out.append({
            "id": block.id, "title": block.title, "ref": block.ref, "brief": block.brief,
            "include": block.include, "max_items": block.max_items,
            "expert_comments": block.expert_comments, "show_quiet_watch": block.show_quiet_watch,
            "watchlist": [{"name": w.name, **({"note": w.note} if w.note else {})} for w in block.watchlist],
            "preferences": memory.preferences(store, d.id, block.id, now),
            "recently_reported": recent,
            "reminders": reminders,
            "candidates": cands,
        })
    pack = {
        "pack_id": pack_id, "digest": d.id, "cadence": "daily", "title": d.title,
        "created_at": iso(now), "local_date": render.local_date(now, d.timezone),
        "window_hours": d.cadences.daily.max_age_hours, "language": d.language,
        "reader": d.reader, "style": d.style,
        "instructions": "Run `digest prompt daily` and follow it. Write decisions JSON, then `digest publish`.",
        "blocks": blocks_out,
    }
    path = paths.packs_dir(d.id) / f"{render.local_date(now, d.timezone)}-daily-{pack_id}.json"
    path.write_text(json.dumps(pack, ensure_ascii=False, indent=1), encoding="utf-8")
    store.x("UPDATE packs SET path=?, candidates=? WHERE id=?", (str(path), jdump(cmap), pack_id))
    store.commit()
    pack["path"] = str(path)
    return pack


def _reminders(store: Store, d: Digest, block: Block, now: datetime) -> list[dict[str, Any]]:
    if not d.remind_after_days:
        return []
    edge = now - timedelta(days=d.remind_after_days)
    rows = store.q("SELECT * FROM stories WHERE digest=? AND block=? AND status='reported' AND reminded_at IS NULL"
                   " AND rating>=0 AND reported_at<=? AND reported_at>=? ORDER BY reported_at DESC LIMIT 3",
                   (d.id, block.id, iso(edge), iso(edge - timedelta(days=2))))
    out = []
    for r in rows:
        b = memory.story_brief(r)
        b["id"] = f"r{r['id']}"
        out.append(b)
    return out


# ---------------------------------------------------------------- decisions
Decision = Literal["new", "update", "repeat", "stale", "irrelevant", "reminder"]
SHOWN = {"new", "update", "reminder"}


class ItemDecision(BaseModel):
    candidate: str
    decision: Decision
    related: str | None = None  # s<ID> of a known story (update / repeat / stale / same thread)
    headline: str = ""
    summary: str = ""
    facts: list[str] = Field(default_factory=list)
    comment: str = ""  # attributed expert / analyst view from the sources (blocks with expert_comments)
    event_date: str | None = None
    importance: int = 2  # 1..3
    why: str = ""

    @field_validator("related", mode="before")
    @classmethod
    def _rel(cls, v: object) -> str | None:  # models write 41, "41", "s41" or "S41"
        if v is None or v == "":
            return None
        t = str(v).strip().lower()
        return t if t.startswith("s") else f"s{t}"

    @field_validator("candidate", mode="before")
    @classmethod
    def _cand(cls, v: object) -> str:
        t = str(v).strip().lower()
        return t if t[:1] in ("c", "r") else f"c{t}"

    @field_validator("importance", mode="before")
    @classmethod
    def _imp(cls, v: object) -> int:
        try:
            return min(3, max(1, int(str(v))))
        except ValueError:
            return 2


class Decisions(BaseModel):
    pack_id: int
    subject: str = ""  # e-mail subject: a hook built from today's main news
    lead: str = ""
    items: list[ItemDecision] = Field(default_factory=list)
    block_notes: dict[str, str] = Field(default_factory=dict)


def lenient_decisions(data: dict[str, Any]) -> tuple[Decisions, list[str]]:
    """For LLM output: keep every valid item, report the rest instead of failing the whole run."""
    good, bad = [], []
    for raw in data.get("items", []) or []:
        try:
            good.append(ItemDecision.model_validate(raw))
        except ValidationError as e:
            bad.append(f"dropped invalid decision {str(raw)[:80]}: {e.errors()[0].get('msg')}")
    notes = data.get("block_notes") if isinstance(data.get("block_notes"), dict) else {}
    dec = Decisions(pack_id=int(data["pack_id"]), lead=str(data.get("lead") or ""), items=good,
                    subject=str(data.get("subject") or ""),
                    block_notes={str(k): str(v) for k, v in (notes or {}).items()})
    return dec, bad


def load_decisions(path_or_json: str | Path | dict[str, Any]) -> Decisions:
    if isinstance(path_or_json, dict):
        data = path_or_json
    else:
        p = Path(path_or_json)
        data = json.loads(p.read_text(encoding="utf-8") if p.exists() else str(path_or_json))
    return Decisions.model_validate(data)


class PublishError(ValueError):
    pass


def _story_row(store: Store, d: Digest, ref: str | None, block: str) -> sqlite3.Row | None:
    if not ref:
        return None
    try:
        sid = int(ref.lstrip("s"))
    except ValueError:
        return None
    return store.one("SELECT * FROM stories WHERE id=? AND digest=? AND block=?", (sid, d.id, block))


def _merge_facts(old: list[str], new: list[str]) -> list[str]:
    seen = {textsim.stems(f).__str__() for f in old}
    out = list(old)
    for f in new:
        k = textsim.stems(f).__str__()
        if f.strip() and k not in seen:
            out.append(f.strip())
            seen.add(k)
    return out[-20:]


def publish(store: Store, d: Digest, dec: Decisions, now: datetime, deliver: bool = True) -> dict[str, Any]:
    pack = store.one("SELECT * FROM packs WHERE id=? AND digest=?", (dec.pack_id, d.id))
    if pack is None:
        raise PublishError(f"pack {dec.pack_id} not found for digest {d.id}")
    if pack["status"] != "open":
        raise PublishError(f"pack {dec.pack_id} is {pack['status']}; run `digest collect` again")
    cmap: dict[str, dict[str, Any]] = jload(pack["candidates"], {})
    warnings: list[str] = []
    by_cand: dict[str, ItemDecision] = {}
    for one in dec.items:
        if one.candidate not in cmap:
            warnings.append(f"unknown candidate {one.candidate}: ignored")
            continue
        if one.candidate in by_cand:
            warnings.append(f"duplicate decision for {one.candidate}: first one kept")
            continue
        by_cand[one.candidate] = one

    # cap per block
    shown_per_block: dict[str, list[ItemDecision]] = defaultdict(list)
    for cid, one in by_cand.items():
        if one.decision in SHOWN:
            if not one.headline.strip():
                raise PublishError(f"{cid}: decision {one.decision} needs a headline (it is shown to the reader)")
            shown_per_block[cmap[cid]["block"]].append(one)
    for bid, lst in shown_per_block.items():
        cap = d.block(bid).max_items
        if len(lst) > cap:
            lst.sort(key=lambda x: -x.importance)
            for extra in lst[cap:]:
                warnings.append(f"{extra.candidate}: block {bid} is capped at {cap}, kept as not selected")
                extra.decision = "irrelevant"
                extra.why = extra.why or "over block cap"
                extra.headline = extra.headline or ""

    vz = Vectorizer(store)
    issue_id = store.x("INSERT INTO issues(digest, cadence, created_at, pack_id) VALUES (?,?,?,?)",
                       (d.id, "daily", iso(now), dec.pack_id))
    shown: dict[str, list[dict[str, Any]]] = defaultdict(list)
    new_story_ids: list[tuple[int, str]] = []
    for cid, info in cmap.items():
        block = info["block"]
        item_ids: list[int] = info["items"]
        it = by_cand.get(cid)
        items = [r for r in (store.one("SELECT * FROM items WHERE id=?", (i,)) for i in item_ids) if r is not None]
        watch = list(dict.fromkeys(w for r in items for w in jload(r["watch"], [])))
        title0 = items[0]["title"] if items else ""

        if "reminder" in info:
            if it is None or it.decision != "reminder":
                continue
            srow = store.one("SELECT * FROM stories WHERE id=?", (info["reminder"],))
            if srow is None:
                continue
            store.x("UPDATE stories SET reminded_at=? WHERE id=?", (iso(now), srow["id"]))
            store.x("INSERT INTO story_events(story_id, at, kind, text, issue_id) VALUES (?,?,?,?,?)",
                    (srow["id"], iso(now), "reminder", it.summary or srow["summary"], issue_id))
            shown[block].append({"story_id": srow["id"], "kind": "reminder", "d": it, "items": [], "since": srow["reported_at"],
                                 "watch": jload(srow["watch"], [])})
            continue

        related = _story_row(store, d, it.related, block) if it else None
        if it is not None and it.related and related is None:
            warnings.append(f"{cid}: related {it.related} not found in block {block}")
        decision = it.decision if it else "not_selected"
        if decision in ("update", "repeat") and related is None:
            decision = "new" if decision == "update" else "not_selected"
            warnings.append(f"{cid}: {it.decision if it else ''} without a known related story -> {decision}")
        if decision == "reminder":
            decision = "new"

        story_id: int
        if decision == "new":
            assert it is not None
            story_id = store.x(
                "INSERT INTO stories(digest, block, title, summary, facts, watch, event_date, status, created_at, reported_at, updated_at, thread_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.id, block, it.headline.strip(), it.summary.strip(), jdump(it.facts), jdump(watch), it.event_date,
                 "reported", iso(now), iso(now), iso(now),
                 (related["thread_id"] or related["id"]) if related is not None else None))
            if related is None:
                new_story_ids.append((story_id, block))
            store.x("INSERT INTO story_events(story_id, at, kind, text, facts, issue_id) VALUES (?,?,?,?,?,?)",
                    (story_id, iso(now), "new", it.summary.strip(), jdump(it.facts), issue_id))
            shown[block].append({"story_id": story_id, "kind": "new", "d": it, "items": items, "watch": watch})
        elif decision == "update":
            assert it is not None and related is not None
            story_id = related["id"]
            facts = _merge_facts(jload(related["facts"], []), it.facts)
            was_reported = related["status"] == "reported"
            store.x("UPDATE stories SET summary=?, facts=?, updated_at=?, status='reported', reason=NULL,"
                    " reported_at=COALESCE(reported_at, ?), watch=?, event_date=COALESCE(?, event_date) WHERE id=?",
                    (it.summary.strip() or related["summary"], jdump(facts), iso(now), iso(now),
                     jdump(list(dict.fromkeys(jload(related["watch"], []) + watch))), it.event_date, story_id))
            vz.forget_story(story_id)
            store.x("INSERT INTO story_events(story_id, at, kind, text, facts, issue_id) VALUES (?,?,?,?,?,?)",
                    (story_id, iso(now), "update" if was_reported else "new", it.summary.strip(), jdump(it.facts), issue_id))
            shown[block].append({"story_id": story_id, "kind": "update" if was_reported else "new", "d": it,
                                 "items": items, "since": related["reported_at"],
                                 "watch": list(dict.fromkeys(jload(related["watch"], []) + watch))})
        elif related is not None and decision in ("repeat", "stale"):
            story_id = related["id"]
        else:
            reason = {"stale": "stale", "irrelevant": "irrelevant", "repeat": "repeat"}.get(decision, "not_selected")
            title = (it.headline.strip() if it and it.headline.strip() else title0) or "?"
            story_id = store.x(
                "INSERT INTO stories(digest, block, title, summary, facts, watch, event_date, status, reason, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d.id, block, title, (it.summary if it else "") or "", jdump(it.facts if it else []), jdump(watch),
                 it.event_date if it else None, "skipped", reason, iso(now)))
        for r in items:
            store.x("UPDATE items SET state='done', story_id=? WHERE id=?", (story_id, r["id"]))

    _auto_thread(store, d, vz, new_story_ids, now)
    store.x("UPDATE packs SET status='published' WHERE id=?", (dec.pack_id,))

    names = _source_names(store, d)
    data, refs = render.build_daily(d, now, dec.lead, dec.subject, shown, dec.block_notes, names, issue_id,
                                    reg.failed_last_run(store, d))
    md = render.daily_markdown(data, d)
    path = paths.out_dir(d.id) / f"{render.local_date(now, d.timezone)}-daily.md"
    path.write_text(md, encoding="utf-8")
    path.with_suffix(".html").write_text(render.daily_html(data, d, None), encoding="utf-8")
    store.x("UPDATE issues SET path=?, refs=?, body=?, data=?, subject=?, period_start=?, period_end=? WHERE id=?",
            (str(path), jdump(refs), md, jdump(data), data["subject"],
             iso(now - timedelta(hours=d.cadences.daily.max_age_hours)), iso(now), issue_id))
    changes = reg.health(store, d, now)
    store.prune(now)
    store.commit()
    delivered: list[str] = []
    n_shown = sum(len(v) for v in shown.values())
    if deliver and (n_shown or d.delivery.send_empty):
        from digestkit.deliver import deliver_issue

        delivered = deliver_issue(store, d, issue_id)
    return {"issue_id": issue_id, "path": str(path), "shown": n_shown,
            "warnings": warnings, "source_changes": changes, "delivered": delivered}


def _auto_thread(store: Store, d: Digest, vz: Vectorizer, new_ids: list[tuple[int, str]], now: datetime) -> None:
    """New stories without an explicit related story join the closest earlier thread, if close enough."""
    by_block: dict[str, list[int]] = defaultdict(list)
    for sid, b in new_ids:
        by_block[b].append(sid)
    for block, ids in by_block.items():
        olds = store.q("SELECT * FROM stories WHERE digest=? AND block=? AND status='reported' AND reported_at>=? AND id NOT IN ({})".format(",".join("?" * len(ids))), [d.id, block, iso(now - timedelta(days=THREAD_LOOKBACK_DAYS)), *ids])
        news = store.q("SELECT * FROM stories WHERE id IN ({})".format(",".join("?" * len(ids))), ids)
        if olds:
            om, nm = vz.pair("story", olds, "story", news)
            sims = nm @ om.T
        for k, r in enumerate(news):
            thread = r["id"]
            if olds:
                j = int(np.argmax(sims[k]))
                if sims[k, j] >= vz.th.thread:
                    thread = olds[j]["thread_id"] or olds[j]["id"]
            store.x("UPDATE stories SET thread_id=? WHERE id=?", (thread, r["id"]))


def validate_decisions_file(path: str) -> Decisions:
    try:
        return load_decisions(path)
    except (ValidationError, json.JSONDecodeError) as e:
        raise PublishError(f"bad decisions JSON: {e}") from e
