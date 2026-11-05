"""Command line: `digest <command> <digest> ...`. Run `digest -h` for the list."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from digestkit import config as cfg
from digestkit import daily, feedback, llm, paths, render, review
from digestkit import sources as reg
from digestkit.collect import collect
from digestkit.store import Store, jload, now_utc, parse_iso


def _now(a: argparse.Namespace) -> datetime:
    return parse_iso(a.now) or now_utc() if getattr(a, "now", None) else now_utc()


def _out(a: argparse.Namespace, data: Any, text: str) -> None:
    if getattr(a, "json", False):
        print(json.dumps(data, ensure_ascii=False, indent=1, default=str))
    else:
        print(text)


# ---------------------------------------------------------------- commands
def cmd_init(a: argparse.Namespace) -> int:
    src = paths.template_path(a.template)
    if not src.exists():
        print(f"no template {a.template}; available: " + ", ".join(p.stem for p in (paths.PKG / "templates").glob("*.yaml")))
        return 2
    dst = paths.digests_dir() / f"{a.id}.yaml"
    if dst.exists() and not a.force:
        print(f"{dst} exists (use --force)")
        return 2
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = src.read_text(encoding="utf-8")
    if a.title:
        data = yaml.safe_load(text)
        text = text.replace(f"title: {data['title']}", f"title: {a.title}", 1)
    dst.write_text(text, encoding="utf-8")
    print(f"created {dst}\nnext: edit it (or ask your agent: `digest prompt new`), then `digest sources check {a.id}`")
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    n = sum(len(b.sources) for b in d.blocks)
    print(f"ok: {d.id} «{d.title}», {len(d.blocks)} blocks ({', '.join(f'{b.ref}={b.id}' for b in d.blocks)}), {n} sources")
    return 0


def _cold_guard(a: argparse.Namespace) -> None:
    from digestkit.state import cold_start_risk

    risk = cold_start_risk()
    if risk and not getattr(a, "allow_cold", False):
        raise ValueError(risk)


def cmd_collect(a: argparse.Namespace) -> int:
    _cold_guard(a)
    d = cfg.load(a.digest)
    now = _now(a)
    with Store() as st:
        res = asyncio.run(collect(st, d, now)) if not a.no_fetch else {"skipped": "fetch"}
        pack = daily.build_pack(st, d, now)
    n = sum(len(b["candidates"]) for b in pack["blocks"])
    lines = [f"pack {pack['pack_id']}: {n} candidates → {pack['path']}"]
    for b in pack["blocks"]:
        lines.append(f"  {b['ref']} {b['id']}: {len(b['candidates'])} candidates, {len(b['recently_reported'])} recently reported")
    if isinstance(res, dict) and res.get("errors"):
        lines.append(f"  source errors: {len(res['errors'])} (see `digest sources list {d.id}`)")
    lines.append("next: read `digest prompt daily`, write decisions JSON, run "
                 f"`digest publish {d.id} --decisions <file>`")
    _out(a, {"collect": res, "pack_id": pack["pack_id"], "path": pack["path"], "candidates": n}, "\n".join(lines))
    return 0


def cmd_publish(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    dec = daily.validate_decisions_file(a.decisions)
    with Store() as st:
        res = daily.publish(st, d, dec, _now(a), deliver=not a.no_deliver)
    text = f"issue {res['issue_id']}: {res['shown']} items → {res['path']} (delivered: {', '.join(res['delivered']) or '-'})"
    for w in res["warnings"]:
        text += f"\n  warning: {w}"
    for c in res["source_changes"]:
        text += f"\n  sources: {c}"
    _out(a, res, text)
    return 0


def cmd_review(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    with Store() as st:
        pack = review.build_pack(st, d, a.cadence, _now(a))
    n = sum(len(b["threads"]) for b in pack["blocks"])
    _out(a, {"pack_id": pack["pack_id"], "path": pack["path"], "threads": n},
         f"{a.cadence} pack {pack['pack_id']}: {n} threads → {pack['path']}\n"
         f"next: read `digest prompt review`, write Markdown, run `digest publish-review {d.id} --pack {pack['pack_id']} --file <md>`")
    return 0


def cmd_publish_review(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    body = Path(a.file).read_text(encoding="utf-8")
    with Store() as st:
        res = review.publish_review(st, d, a.pack, body, _now(a), deliver=not a.no_deliver)
    _out(a, res, f"issue {res['issue_id']} → {res['path']} (delivered: {', '.join(res['delivered']) or '-'})")
    return 0


def due(st: Store, d: cfg.Digest, now: datetime) -> list[str]:
    today = render.local_date(now, d.timezone)
    lt = render.local_dt(now, d.timezone)
    out = []

    def last(cad: str) -> datetime | None:
        r = st.one("SELECT created_at FROM issues WHERE digest=? AND cadence=? ORDER BY created_at DESC LIMIT 1", (d.id, cad))
        return parse_iso(r["created_at"]) if r else None

    ld = last("daily")
    if d.cadences.daily.enabled and (ld is None or render.local_date(ld, d.timezone) != today):
        out.append("daily")
    has_data = st.one("SELECT 1 FROM stories WHERE digest=? AND status='reported' LIMIT 1", (d.id,)) is not None
    lw = last("weekly")
    if d.cadences.weekly.enabled and has_data and (
            (lt.weekday() == d.cadences.weekly.weekday and (lw is None or now - lw > timedelta(days=5)))
            or (lw is not None and now - lw > timedelta(days=8))):
        out.append("weekly")
    lm = last("monthly")
    if d.cadences.monthly.enabled and has_data and (
            (lt.day == d.cadences.monthly.day and (lm is None or now - lm > timedelta(days=20)))
            or (lm is not None and now - lm > timedelta(days=33))):
        out.append("monthly")
    return out


def cmd_due(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    with Store() as st:
        res = due(st, d, _now(a))
    _out(a, res, " ".join(res) if res else "nothing due")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    """Standalone: everything due (or --cadence) with the configured LLM."""
    _cold_guard(a)
    d = cfg.load(a.digest)
    now = _now(a)
    with Store() as st:
        todo = [a.cadence] if a.cadence else due(st, d, now)
        if not todo:
            print("nothing due")
            return 0
        for cad in todo:
            if cad == "daily":
                if not a.no_fetch:
                    print("collect:", asyncio.run(collect(st, d, now)))
                pack = daily.build_pack(st, d, now)
                dec, dropped = daily.lenient_decisions(llm.daily_decisions(pack))
                res = daily.publish(st, d, dec, now, deliver=not a.no_deliver)
                res["warnings"] = dropped + res["warnings"]
                print(f"daily issue {res['issue_id']}: {res['shown']} items → {res['path']}")
                for w in res["warnings"]:
                    print("  warning:", w)
            else:
                pack = review.build_pack(st, d, cad, now)
                body = llm.review_markdown(pack)
                res = review.publish_review(st, d, pack["pack_id"], body, now, deliver=not a.no_deliver)
                print(f"{cad} issue {res['issue_id']} → {res['path']}")
    return 0


def cmd_add_items(a: argparse.Namespace) -> int:
    from digestkit.collect import add_items
    from digestkit.collect.adapters import parse_json_items

    d = cfg.load(a.digest)
    raw = Path(a.file).read_text(encoding="utf-8") if a.file != "-" else sys.stdin.read()
    docs = parse_json_items(raw)
    with Store() as st:
        res = add_items(st, d, a.block, docs, _now(a), a.source)
    _out(a, res, f"{len(docs)} items read: {res}. They join the next `digest collect {d.id}` pack.")
    return 0


def cmd_state(a: argparse.Namespace) -> int:
    from digestkit import state

    try:
        if a.action == "push":
            res = state.push()
            text = f"saved to {os.environ.get('DIGEST_STATE_GIT')} ({res['branch']}): {res['issues']} issues" + (
                ", encrypted" if res["encrypted"] else "")
        elif a.action == "pull":
            res = state.pull(force=a.force)
            text = res["status"] + (f"; configs: {', '.join(res.get('configs_restored', []))}" if res.get("configs_restored") else "")
        elif a.action == "export":
            res = state.export(Path(a.file or "digest-state.tar.gz"))
            text = f"exported {res['issues']} issues to {a.file or 'digest-state.tar.gz'}"
        else:
            if not a.file:
                raise ValueError("import needs --file")
            res = state.restore(Path(a.file))
            state.mark_pulled()
            text = f"imported (snapshot from {res.get('exported_at')}); configs: {', '.join(res.get('configs_restored', [])) or '-'}"
    except state.StateError as e:
        raise ValueError(str(e)) from e
    _out(a, res, text)
    return 0


def cmd_feedback(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    with Store() as st:
        if a.pull:
            res: Any = {"email": feedback.pull_email(st, d), "telegram": feedback.pull_telegram(st, d)}
            _out(a, res, f"pulled: {len(res['email'])} e-mail replies, {len(res['telegram'])} telegram messages")
            return 0
        text = " ".join(a.text) if a.text else sys.stdin.read()
        res = feedback.apply(st, d, text, a.issue)
    _out(a, res, f"issue {res.get('issue_id')}: rated {', '.join(res['rated']) or '-'}; notes: {len(res['notes'])}"
         + (f"; unknown refs: {', '.join(res['unknown_refs'])}" if res.get("unknown_refs") else ""))
    return 0


def cmd_sources(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    with Store() as st:
        reg.sync(st, d)
        if a.action == "list":
            stats = reg.stats(st, d, now_utc())
            rows = []
            for r in st.q("SELECT * FROM sources WHERE digest=? AND status!='removed' ORDER BY block, status, source_id", (d.id,)):
                s = stats.get((r["block"], r["source_id"]), {})
                rows.append({"block": r["block"], "id": r["source_id"], "name": jload(r["spec"], {}).get("name"),
                             "origin": r["origin"], "status": r["status"], "items_28d": s.get("items", 0),
                             "shown_28d": s.get("shown", 0), "last_ok": (r["last_ok"] or "")[:16],
                             "error": r["last_error"], "reason": r["reason"]})
            text = "\n".join(f"{x['block']:<10} {x['status']:<8} {x['items_28d']:>4} items {x['shown_28d']:>3} shown  "
                             f"{x['id']}" + (f"  ! {x['error']}" if x["error"] else "") for x in rows)
            _out(a, rows, text or "no sources")
            return 0
        if a.action == "check":
            return _check(a, st, d)
        if a.action == "add":
            return _add(a, st, d)
        if a.action in ("enable", "disable"):
            n = reg.set_status(st, d, a.target, "active" if a.action == "enable" else "disabled", a.block)
            print(f"{n} source(s) {a.action}d")
            return 0 if n else 1
        if a.action == "suggest":
            return _suggest(a, st, d)
    return 2


def _check(a: argparse.Namespace, st: Store, d: cfg.Digest) -> int:
    from digestkit.collect.adapters import fetch
    from digestkit.collect.http import Client

    async def go() -> list[dict[str, Any]]:
        async with Client() as c:
            async def one(e: reg.Entry) -> dict[str, Any]:
                try:
                    docs = await asyncio.wait_for(fetch(e.source, c, d, d.block(e.block).watchlist, 24 * 7), 120)
                    dates = [x.published_at for x in docs if x.published_at]
                    return {"block": e.block, "id": e.source.id, "ok": True, "items": len(docs),
                            "newest": max(dates).date().isoformat() if dates else None,
                            "undated": sum(1 for x in docs if not x.published_at),
                            "sample": docs[0].title[:80] if docs else ""}
                except Exception as ex:
                    return {"block": e.block, "id": e.source.id, "ok": False, "error": f"{type(ex).__name__}: {ex}"[:200]}
            ents = [e for e in reg.entries(st, d) if e.status not in ("disabled",) and (not a.block or e.block == a.block)]
            return list(await asyncio.gather(*(one(e) for e in ents)))

    res = asyncio.run(go())
    text = "\n".join(
        (f"OK   {r['block']:<10} {r['id']:<40} {r['items']:>3} items, newest {r['newest']}, undated {r['undated']} | {r['sample']}"
         if r["ok"] else f"FAIL {r['block']:<10} {r['id']:<40} {r['error']}") for r in res)
    _out(a, res, text or "no sources")
    return 0 if all(r["ok"] for r in res) else 1


def _add(a: argparse.Namespace, st: Store, d: cfg.Digest) -> int:
    from digestkit.collect.adapters import detect
    from digestkit.collect.http import Client

    if not a.block:
        print("--block is required")
        return 2
    if a.query:
        src = cfg.Source(kind="news_search", query=a.query, name=a.name)
        ok = reg.add_discovered(st, d, a.block, src, note=a.note or "")
        print(("added " if ok else "already there: ") + src.id)
        return 0

    async def go() -> dict[str, Any]:
        async with Client() as c:
            return await detect(a.target, c)

    info = asyncio.run(go())
    if not info.get("kind"):
        print(f"could not find a feed or article links at {a.target}; add it to the YAML with selectors.link_pattern")
        return 1
    src = cfg.Source(kind=info["kind"], url=info["url"], feed_url=info.get("feed_url"),
                     selectors=info.get("selectors", {}), name=a.name, trust=a.trust)
    ok = reg.add_discovered(st, d, a.block, src, note=a.note or "")
    print(f"{'added' if ok else 'already there'}: {src.id} ({info['kind']}, {info['count']} items)")
    for s in info["sample"]:
        print(f"  - {s.title[:100]}")
    return 0


def _suggest(a: argparse.Namespace, st: Store, d: cfg.Digest) -> int:
    """Publishers that keep appearing in reported news-search results are worth reading directly."""
    rows = st.q("""SELECT i.block, i.publisher, COUNT(*) AS n FROM items i JOIN stories s ON s.id=i.story_id
                   WHERE i.digest=? AND s.status='reported' AND i.publisher IS NOT NULL
                   GROUP BY i.block, i.publisher HAVING n>=2 ORDER BY n DESC LIMIT 20""", (d.id,))
    have = {(jload(r["spec"], {}).get("name") or "").lower() for r in st.q("SELECT spec FROM sources WHERE digest=?", (d.id,))}
    out = [{"block": r["block"], "publisher": r["publisher"], "reported_items": r["n"]} for r in rows
           if (r["publisher"] or "").lower() not in have]
    _out(a, out, "\n".join(f"{x['block']:<10} {x['reported_items']:>3}  {x['publisher']}" for x in out)
         or "nothing yet: suggestions come from publishers seen in reported news-search items")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    now = _now(a)
    with Store() as st:
        issues = [dict(r) for r in st.q("SELECT id, cadence, created_at, path, delivered FROM issues WHERE digest=? ORDER BY created_at DESC LIMIT 10", (d.id,))]
        stories = review.thread_stats(st, d)
        fb = st.one("SELECT COUNT(*) AS n, SUM(value>0) AS up, SUM(value<0) AS down, SUM(text!='') AS notes FROM feedback WHERE digest=?", (d.id,))
        changes = [f"{r['at'][:10]} {r['text']}" for r in st.q("SELECT * FROM changes WHERE digest=? ORDER BY at DESC LIMIT 10", (d.id,))]
        notes = [r["text"] for r in st.q("SELECT text FROM feedback WHERE digest=? AND text!='' ORDER BY created_at DESC LIMIT 10", (d.id,))]
        open_packs = [dict(r) for r in st.q("SELECT id, cadence, created_at, path FROM packs WHERE digest=? AND status='open'", (d.id,))]
        res: dict[str, Any] = {"digest": d.id, "due": due(st, d, now), "issues": issues, "stories": stories,
               "feedback": dict(fb) if fb else {}, "recent_notes": notes, "changes": changes, "open_packs": open_packs}
    text = [f"{d.title} ({d.id}); due now: {', '.join(res['due']) or 'nothing'}",
            f"stories: {stories}", f"feedback: {res['feedback']}"]
    text += ["last issues:"] + [f"  #{i['id']} {i['cadence']:<7} {i['created_at'][:16]} {i['delivered'] or ''}" for i in issues]
    if open_packs:
        text += ["open packs:"] + [f"  {p['id']} {p['cadence']} {p['path']}" for p in open_packs]
    if changes:
        text += ["system changes:"] + [f"  {c}" for c in changes]
    if notes:
        text += ["reader notes:"] + [f"  {n}" for n in notes]
    _out(a, res, "\n".join(text))
    return 0


def cmd_prompt(a: argparse.Namespace) -> int:
    p = paths.prompt_path(a.name)
    if not p.exists():
        print("prompts: " + ", ".join(x.stem for x in (paths.PKG / "prompts").glob("*.md")))
        return 2
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_issue(a: argparse.Namespace) -> int:
    d = cfg.load(a.digest)
    with Store() as st:
        r = st.one("SELECT * FROM issues WHERE digest=? " + ("AND id=? " if a.id else "") + "ORDER BY created_at DESC LIMIT 1",
                   (d.id, a.id) if a.id else (d.id,))
        if r is None:
            print("no issues yet")
            return 1
        if a.resend:
            from digestkit.deliver import deliver_issue

            print("delivered:", deliver_issue(st, d, r["id"]))
            return 0
        from digestkit.deliver import payload

        subj, text, html = payload(st, d, r, a.blocks.split(",") if a.blocks else [])
    if a.format == "subject":
        print(subj)
    elif a.format == "html":
        if a.out:
            Path(a.out).write_text(html, encoding="utf-8")
            print(a.out)
        else:
            print(html)
    elif a.format == "json":
        print(json.dumps({"issue_id": r["id"], "cadence": r["cadence"], "subject": subj, "markdown": text,
                          "html": html, "tag": f"[digest:{d.id} #{r['id']}]"}, ensure_ascii=False))
    else:
        print(text)
    return 0


def cmd_templates(a: argparse.Namespace) -> int:
    for p in sorted((paths.PKG / "templates").glob("*.yaml")):
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        print(f"{p.stem:<16} {data.get('title')}: " + ", ".join(b["title"] for b in data.get("blocks", [])))
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="digest", description="Agent-first news digests with memory.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Any, help: str, digest: bool = True, now: bool = False) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help)
        if digest:
            sp.add_argument("digest", help="digest id (digests/<id>.yaml) or path to YAML")
        if now:
            sp.add_argument("--now", help="pretend it is this ISO time (testing, catching up)")
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("init", cmd_init, "create digests/<id>.yaml from a template", digest=False)
    sp.add_argument("id")
    sp.add_argument("--template", default="blank")
    sp.add_argument("--title")
    sp.add_argument("--force", action="store_true")
    add("templates", cmd_templates, "list templates", digest=False)
    add("validate", cmd_validate, "check the YAML")
    sp = add("collect", cmd_collect, "daily step 1: fetch sources, build the candidate pack", now=True)
    sp.add_argument("--no-fetch", action="store_true", help="only rebuild the pack from stored items")
    sp.add_argument("--allow-cold", action="store_true", help="run even though remote state was not pulled")
    sp = add("publish", cmd_publish, "daily step 2: apply decisions JSON, write memory, render and deliver", now=True)
    sp.add_argument("--decisions", required=True, help="path to decisions JSON")
    sp.add_argument("--no-deliver", action="store_true")
    sp = add("review", cmd_review, "weekly/monthly step 1: build the review pack from memory", now=True)
    sp.add_argument("cadence", choices=["weekly", "monthly"])
    sp = add("publish-review", cmd_publish_review, "weekly/monthly step 2: store and deliver the review", now=True)
    sp.add_argument("--pack", type=int, required=True)
    sp.add_argument("--file", required=True)
    sp.add_argument("--no-deliver", action="store_true")
    add("due", cmd_due, "which contours are due now: daily / weekly / monthly", now=True)
    sp = add("run", cmd_run, "standalone mode: do everything due with LLM_* from .env", now=True)
    sp.add_argument("--cadence", choices=["daily", "weekly", "monthly"])
    sp.add_argument("--no-deliver", action="store_true")
    sp.add_argument("--no-fetch", action="store_true")
    sp.add_argument("--allow-cold", action="store_true")
    sp = add("add-items", cmd_add_items, "add items you found yourself (JSON / JSON lines: url, title, text, published_at, source)", now=True)
    sp.add_argument("--block", required=True)
    sp.add_argument("--file", required=True, help="path or - for stdin")
    sp.add_argument("--source", default="agent", help="label for these items, e.g. web-search, slack")
    sp = add("feedback", cmd_feedback, "record feedback text ('E1 +, B2 -, less about X') or --pull replies")
    sp.add_argument("text", nargs="*")
    sp.add_argument("--issue", type=int)
    sp.add_argument("--pull", action="store_true", help="read replies from IMAP and the Telegram bot")
    sp = add("sources", cmd_sources, "sources: list | check | add | suggest | enable | disable")
    sp.add_argument("action", choices=["list", "check", "add", "suggest", "enable", "disable"])
    sp.add_argument("target", nargs="?", help="URL (add) or source id (enable/disable)")
    sp.add_argument("--block")
    sp.add_argument("--query", help="add a news-search source with this query")
    sp.add_argument("--name")
    sp.add_argument("--note")
    sp.add_argument("--trust", type=float, default=0.7)
    sp = add("state", cmd_state, "memory + configs snapshot: pull | push (git, DIGEST_STATE_GIT) | export | import",
             digest=False)
    sp.add_argument("action", choices=["pull", "push", "export", "import"])
    sp.add_argument("--file")
    sp.add_argument("--force", action="store_true", help="pull: overwrite even if local memory is newer")
    add("status", cmd_status, "issues, memory, feedback, automatic changes", now=True)
    sp = add("issue", cmd_issue, "print the last (or --id) issue; --resend delivers it again")
    sp.add_argument("--id", type=int)
    sp.add_argument("--resend", action="store_true")
    sp.add_argument("--format", choices=["md", "html", "subject", "json"], default="md",
                    help="html / subject: for sending through your own mail connector (e.g. Gmail in Cowork)")
    sp.add_argument("--out", help="write the HTML to this file")
    sp.add_argument("--blocks", help="only these block ids, comma-separated (one recipient's part)")
    sp = add("prompt", cmd_prompt, "print instructions for the agent: daily | review | new | tune", digest=False)
    sp.add_argument("name")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # agents read our output through pipes; Windows pipes default to cp1251/cp1252
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    paths.load_dotenv()
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(a.fn(a) or 0)
    except (FileNotFoundError, daily.PublishError, ValueError, llm.LLMError, ValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

