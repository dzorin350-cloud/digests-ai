"""Source registry and health. Sources come from the YAML (origin=config) or are discovered later
(origin=discovered, start as `trial`). The system mutes useless and broken sources on its own and writes
every such change to the changes log, which the weekly review reports. Nobody has to approve anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from digestkit.config import Digest, Source
from digestkit.store import Store, iso, jdump, jload, now_utc, parse_iso

BROKEN_AFTER_FAILS = 4
BROKEN_RETRY_DAYS = 3
MUTE_MIN_AGE_DAYS = 14
MUTE_MIN_ITEMS = 15
TRIAL_MIN_AGE_DAYS = 10
TRIAL_MIN_ITEMS = 5


@dataclass
class Entry:
    block: str
    source: Source
    origin: str
    status: str
    reason: str | None


def sync(store: Store, d: Digest, now: datetime | None = None) -> None:
    now = now or now_utc()
    seen = set()
    for b in d.blocks:
        for s in b.sources:
            seen.add((b.id, s.id))
            row = store.one("SELECT status, origin FROM sources WHERE digest=? AND block=? AND source_id=?", (d.id, b.id, s.id))
            if row is None:
                store.x("INSERT INTO sources(digest, block, source_id, spec, origin, status, added_at) VALUES (?,?,?,?,?,?,?)",
                        (d.id, b.id, s.id, s.model_dump_json(), "config", "active" if s.enabled else "disabled", iso(now)))
            else:
                status = row["status"]
                if not s.enabled:
                    status = "disabled"
                elif status in ("disabled", "removed"):
                    status = "active"
                store.x("UPDATE sources SET spec=?, origin='config', status=? WHERE digest=? AND block=? AND source_id=?",
                        (s.model_dump_json(), status, d.id, b.id, s.id))
    for r in store.q("SELECT block, source_id FROM sources WHERE digest=? AND origin='config' AND status!='removed'", (d.id,)):
        if (r["block"], r["source_id"]) not in seen:
            store.x("UPDATE sources SET status='removed' WHERE digest=? AND block=? AND source_id=?", (d.id, r["block"], r["source_id"]))
    store.commit()


def entries(store: Store, d: Digest) -> list[Entry]:
    out = []
    for r in store.q("SELECT * FROM sources WHERE digest=? AND status!='removed' ORDER BY block, origin, source_id", (d.id,)):
        out.append(Entry(r["block"], Source.model_validate_json(r["spec"]), r["origin"], r["status"], r["reason"]))
    return out


def runnable(store: Store, d: Digest, now: datetime) -> list[Entry]:
    out = []
    for r in store.q("SELECT * FROM sources WHERE digest=? AND status IN ('active','trial','broken')", (d.id,)):
        if r["status"] == "broken":
            last = parse_iso(r["last_run"])
            if last and now - last < timedelta(days=BROKEN_RETRY_DAYS):
                continue
        out.append(Entry(r["block"], Source.model_validate_json(r["spec"]), r["origin"], r["status"], r["reason"]))
    return out


def record_run(store: Store, d: Digest, block: str, sid: str, ok: bool, error: str | None, now: datetime) -> None:
    row = store.one("SELECT status, fails FROM sources WHERE digest=? AND block=? AND source_id=?", (d.id, block, sid))
    if row is None:
        return
    if ok:
        status = "active" if row["status"] == "broken" else row["status"]
        if row["status"] == "broken":
            store.log_change(d.id, f"Источник {sid} ({block}) снова работает", now)
        store.x("UPDATE sources SET last_run=?, last_ok=?, last_error=NULL, fails=0, status=? WHERE digest=? AND block=? AND source_id=?",
                (iso(now), iso(now), status, d.id, block, sid))
    else:
        fails = row["fails"] + 1
        status = row["status"]
        if fails >= BROKEN_AFTER_FAILS and status != "broken":
            status = "broken"
            store.log_change(d.id, f"Источник {sid} ({block}) отключён: не отвечает {fails} раза подряд ({error})", now)
        store.x("UPDATE sources SET last_run=?, last_error=?, fails=?, status=?, status_at=? WHERE digest=? AND block=? AND source_id=?",
                (iso(now), (error or "")[:300], fails, status, iso(now), d.id, block, sid))


def failed_last_run(store: Store, d: Digest) -> dict[str, tuple[int, int]]:
    """Per block: (sources that failed on their last run, sources that should be running)."""
    out: dict[str, tuple[int, int]] = {}
    for r in store.q("SELECT block, last_ok, last_run, last_error, spec FROM sources WHERE digest=? AND status IN ('active','trial','broken')",
                     (d.id,)):
        failed, total = out.get(r["block"], (0, 0))
        bad = bool(r["last_error"]) and (r["last_ok"] or "") < (r["last_run"] or "")
        out[r["block"]] = (failed + int(bad), total + 1)
    return out


def stats(store: Store, d: Digest, now: datetime, days: int = 28) -> dict[tuple[str, str], dict[str, int]]:
    since = iso(now - timedelta(days=days))
    out: dict[tuple[str, str], dict[str, int]] = {}
    for r in store.q(
        """SELECT i.block, i.source_id, COUNT(*) AS n,
                  SUM(CASE WHEN s.status='reported' THEN 1 ELSE 0 END) AS shown,
                  SUM(CASE WHEN s.rating>0 THEN 1 ELSE 0 END) AS liked,
                  SUM(CASE WHEN s.rating<0 THEN 1 ELSE 0 END) AS disliked
           FROM items i LEFT JOIN stories s ON s.id=i.story_id
           WHERE i.digest=? AND i.fetched_at>=? AND i.state NOT IN ('baseline')
           GROUP BY i.block, i.source_id""", (d.id, since)):
        out[(r["block"], r["source_id"])] = {"items": r["n"], "shown": r["shown"] or 0,
                                             "liked": r["liked"] or 0, "disliked": r["disliked"] or 0}
    return out


def health(store: Store, d: Digest, now: datetime | None = None) -> list[str]:
    """Promote useful trial sources, mute useless ones. Returns human-readable changes."""
    now = now or now_utc()
    st = stats(store, d, now)
    changes = []
    for r in store.q("SELECT * FROM sources WHERE digest=? AND status IN ('active','trial')", (d.id,)):
        spec = jload(r["spec"], {})
        s = st.get((r["block"], r["source_id"]), {"items": 0, "shown": 0, "liked": 0, "disliked": 0})
        age = (now - (parse_iso(r["added_at"]) or now)).days
        name = spec.get("name") or r["source_id"]
        new_status, why = None, ""
        if r["status"] == "trial" and s["shown"] > 0:
            new_status, why = "active", f"пробный источник «{name}» ({r['block']}) прижился: {s['shown']} материалов в дайджестах"
        elif spec.get("kind") == "news_search":
            continue
        elif r["status"] == "trial" and age >= TRIAL_MIN_AGE_DAYS and s["items"] >= TRIAL_MIN_ITEMS and s["shown"] == 0:
            new_status, why = "muted", f"пробный источник «{name}» ({r['block']}) убран: {s['items']} материалов, ни один не пригодился"
        elif r["status"] == "active" and age >= MUTE_MIN_AGE_DAYS and s["items"] >= MUTE_MIN_ITEMS and s["shown"] == 0:
            new_status, why = "muted", f"источник «{name}» ({r['block']}) приглушён: {s['items']} материалов за 4 недели, ни один не попал в дайджест"
        elif r["status"] == "active" and s["disliked"] >= 3 and s["liked"] == 0 and s["disliked"] >= s["shown"] // 2:
            new_status, why = "muted", f"источник «{name}» ({r['block']}) приглушён: {s['disliked']} отрицательных оценок"
        if new_status:
            store.x("UPDATE sources SET status=?, reason=?, status_at=? WHERE digest=? AND block=? AND source_id=?",
                    (new_status, why, iso(now), d.id, r["block"], r["source_id"]))
            store.log_change(d.id, why[0].upper() + why[1:], now)
            changes.append(why)
    store.commit()
    return changes


def add_discovered(store: Store, d: Digest, block: str, src: Source, now: datetime | None = None, note: str = "") -> bool:
    now = now or now_utc()
    d.block(block)  # validates the block id
    if store.one("SELECT 1 FROM sources WHERE digest=? AND block=? AND source_id=?", (d.id, block, src.id)):
        return False
    store.x("INSERT INTO sources(digest, block, source_id, spec, origin, status, reason, added_at) VALUES (?,?,?,?,?,?,?,?)",
            (d.id, block, src.id, src.model_dump_json(), "discovered", "trial", note or None, iso(now)))
    store.log_change(d.id, f"Добавлен пробный источник «{src.name}» в блок {block}" + (f": {note}" if note else ""), now)
    store.commit()
    return True


def set_status(store: Store, d: Digest, source_id: str, status: str, block: str | None = None) -> int:
    sql = "UPDATE sources SET status=?, reason='вручную', fails=0, status_at=? WHERE digest=? AND source_id=?"
    args: list = [status, iso(now_utc()), d.id, source_id]
    if block:
        sql += " AND block=?"
        args.append(block)
    n = store.db.execute(sql, args).rowcount
    store.commit()
    return int(n)


__all__ = ["add_discovered", "entries", "health", "jdump", "record_run", "runnable", "set_status", "stats", "sync"]
