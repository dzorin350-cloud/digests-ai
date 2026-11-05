"""Issue rendering. A daily issue is first built as a structure (`data`), then rendered to Markdown (file,
Telegram) and to HTML cards with inline CSS (e-mail). Every shown item gets a short ref (E1, B2…) for feedback.
"""

from __future__ import annotations

import html as h
import re
import sqlite3
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import markdown as mdlib

from digestkit.config import Digest

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
PALETTE = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#db2777", "#0891b2", "#65a30d", "#dc2626"]

T = {
    "ru": {"empty": "Значимых новостей нет.", "update": "Обновление", "reminder": "Напоминание",
           "since": "впервые в дайджесте", "quiet": "новостей нет", "experts": "Мнения",
           "sources_down": "Источники блока не ответили — пусто не значит «ничего не было».",
           "sources_partly": "Не ответили источники: {n} из {total}.",
           "feedback": "Оценить: ответьте на письмо или сообщение, например "
           "«{ex} +, {ex2} −, меньше про …». Ссылки 👍/👎 в письме делают то же самое.",
           "feedback1": "Оценить: ответьте на письмо или сообщение: «{ex} +» или «{ex} −, меньше про …». "
           "Ссылки 👍/👎 в письме делают то же самое.",
           "nothing": "Сегодня ничего нового — значимых событий по вашим темам не нашлось.",
           "nothing_subject": "ничего нового", "more": "ещё {n}",
           "weekly": "недельный разбор", "monthly": "месячный разбор"},
    "en": {"empty": "Nothing significant.", "update": "Update", "reminder": "Reminder", "since": "first reported",
           "quiet": "no news", "experts": "Views",
           "sources_down": "This block's sources did not respond: empty does not mean nothing happened.",
           "sources_partly": "Sources not responding: {n} of {total}.",
           "feedback": "Rate: reply e.g. “{ex} +, {ex2} −, less about …”. The 👍/👎 links in the e-mail do the same.",
           "feedback1": "Rate: reply “{ex} +” or “{ex} −, less about …”. The 👍/👎 links in the e-mail do the same.",
           "nothing": "Nothing new today: no significant events on your topics.",
           "nothing_subject": "nothing new", "more": "{n} more",
           "weekly": "weekly review", "monthly": "monthly review"},
}


def tr(d: Digest, key: str) -> str:
    return T.get(d.language, T["en"]).get(key, T["en"][key])


def local_dt(now: datetime, tz: str) -> datetime:
    try:
        return now.astimezone(ZoneInfo(tz))
    except Exception:
        return now


def local_date(now: datetime, tz: str) -> str:
    return local_dt(now, tz).date().isoformat()


def human_date(now: datetime, d: Digest) -> str:
    lt = local_dt(now, d.timezone)
    if d.language == "ru":
        return f"{lt.day} {MONTHS_GEN[lt.month - 1]} {lt.year}"
    return f"{lt.strftime('%B')} {lt.day}, {lt.year}"  # no %-d: it does not exist on Windows


def _short(s: str | None) -> str:
    if not s:
        return ""
    return f"{int(s[8:10])}.{s[5:7]}"


def _links(items: list[sqlite3.Row], names: dict[str, str], limit: int = 3) -> list[list[str]]:
    seen, out = set(), []
    for r in items:
        name = r["publisher"] or names.get(r["source_id"], r["source_id"])
        if name in seen:
            continue
        seen.add(name)
        out.append([name, r["url"]])
        if len(out) >= limit:
            break
    return out


def block_color(d: Digest, i: int) -> str:
    c = d.blocks[i].color
    return c if c and re.fullmatch(r"#[0-9a-fA-F]{6}", c) else PALETTE[i % len(PALETTE)]


# ------------------------------------------------------------------ daily: structure
def build_daily(d: Digest, now: datetime, lead: str, subject: str, shown: dict[str, list[dict[str, Any]]],
                notes: dict[str, str], names: dict[str, str], issue_id: int,
                source_health: dict[str, tuple[int, int]]) -> tuple[dict[str, Any], dict[str, int]]:
    refs: dict[str, int] = {}
    blocks = []
    for i, b in enumerate(d.blocks):
        entries = sorted(shown.get(b.id, []), key=lambda e: -e["d"].importance)
        items = []
        mentioned: set[str] = set()
        for k, e in enumerate(entries, 1):
            ref = f"{b.ref}{k}"
            refs[ref] = e["story_id"]
            dec = e["d"]
            mentioned |= set(e.get("watch") or [])
            items.append({"ref": ref, "kind": e["kind"], "headline": dec.headline.strip(), "summary": dec.summary.strip(),
                          "comment": dec.comment.strip() if b.expert_comments else "",
                          "since": _short(e.get("since")), "links": _links(e["items"], names),
                          "watch": e.get("watch") or [], "importance": dec.importance})
        quiet = [w.name for w in b.watchlist if w.name not in mentioned] if b.show_quiet_watch else []
        failed, total = source_health.get(b.id, (0, 0))
        warn = ""
        if total and failed == total:
            warn = tr(d, "sources_down")
        elif failed and failed * 2 >= total:
            warn = tr(d, "sources_partly").format(n=failed, total=total)
        blocks.append({"id": b.id, "title": b.title, "ref": b.ref, "color": block_color(d, i), "items": items,
                       "quiet": quiet, "note": (notes.get(b.id) or "").strip(), "warning": warn})
    data = {"digest": d.id, "issue": issue_id, "cadence": "daily", "title": d.title, "date": human_date(now, d),
            "lead": lead.strip(), "subject": (subject.strip() or fallback_subject(d, blocks)), "blocks": blocks}
    return data, refs


def fallback_subject(d: Digest, blocks: list[dict[str, Any]]) -> str:
    """No hook from the agent: the most important headlines become the subject."""
    items = sorted((it for b in blocks for it in b["items"]), key=lambda it: -it["importance"])
    if not items:
        return f"{d.title}: {tr(d, 'nothing_subject')}"
    subj = items[0]["headline"]
    if len(items) > 1 and len(subj) + len(items[1]["headline"]) < 110:
        subj += " · " + items[1]["headline"]
        rest = len(items) - 2
    else:
        rest = len(items) - 1
    if rest > 0:
        subj += " · " + tr(d, "more").format(n=rest)
    return subj[:150]


def only_blocks(data: dict[str, Any], keep: list[str]) -> dict[str, Any]:
    if not keep:
        return data
    return {**data, "blocks": [b for b in data["blocks"] if b["id"] in keep]}


# ------------------------------------------------------------------ daily: Markdown
def daily_markdown(data: dict[str, Any], d: Digest) -> str:
    lines = [f"# {data['title']} — {data['date']}", ""]
    total = sum(len(b["items"]) for b in data["blocks"])
    if data["lead"]:
        lines += [data["lead"], ""]
    elif total == 0:
        lines += [tr(d, "nothing"), ""]
    refs = []
    for b in data["blocks"]:
        lines += [f"## {b['title']}", ""]
        if b["warning"]:
            lines += [f"> ⚠️ {b['warning']}", ""]
        if not b["items"] and not b["quiet"]:
            lines += [f"_{tr(d, 'empty')}_", ""]
        for it in b["items"]:
            refs.append(it["ref"])
            label = ""
            if it["kind"] == "update":
                label = f"_{tr(d, 'update')} ({tr(d, 'since')} {it['since']})._ "
            elif it["kind"] == "reminder":
                label = f"_{tr(d, 'reminder')}._ "
            src = ", ".join(f"[{n}]({u})" for n, u in it["links"])
            tail = f" — {src}" if src else ""
            lines.append(f"- **[{it['ref']}] {it['headline']}**  ")
            body = (label + it["summary"]).strip()
            lines.append(f"  {body}{tail}" if body else f"  {tail.lstrip(' —')}")
            if it["comment"]:
                lines.append(f"  _{tr(d, 'experts')}: {it['comment']}_")
        for name in b["quiet"]:
            lines.append(f"- **{name}** — {tr(d, 'quiet')}")
        if b["note"]:
            lines += ["", f"_{b['note']}_"]
        lines.append("")
    if refs:
        hint = tr(d, "feedback").format(ex=refs[0], ex2=refs[1]) if len(refs) > 1 else tr(d, "feedback1").format(ex=refs[0])
        lines += ["---", hint, ""]
    lines.append(f"<!-- digest:{data['digest']} issue:{data['issue']} -->")
    return "\n".join(lines)


# ------------------------------------------------------------------ HTML (e-mail)
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def tag(digest_id: str, issue_id: int) -> str:
    """Marker that ties a reply to an issue (kept out of the subject)."""
    return f"[digest:{digest_id} #{issue_id}]"


def _vote(ref: str, subject: str, reply_to: str | None, digest_id: str, issue_id: int) -> str:
    if not reply_to:
        return ""
    s = quote(f"Re: {subject}")

    def link(sign: str, emoji: str) -> str:
        body = quote(f"{ref} {sign}\n\n{tag(digest_id, issue_id)}")
        return (f'<a href="mailto:{h.escape(reply_to)}?subject={s}&amp;body={body}" '
                f'style="text-decoration:none;font-size:15px;margin-left:6px">{emoji}</a>')

    return link("+", "👍") + link("-", "👎")


def _card(title: str, color: str, inner: str) -> str:
    return (f'<div style="background:#ffffff;border:1px solid #e5e7eb;border-left:4px solid {color};border-radius:10px;'
            f'padding:16px 18px;margin:0 0 16px 0">'
            f'<div style="font-size:13px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:{color};'
            f'margin:0 0 10px 0">{h.escape(title)}</div>{inner}</div>')


def _page(title: str, date: str, lead: str, cards: str, footer: str) -> str:
    lead_html = f'<p style="margin:0 0 18px 0;font-size:16px;line-height:1.5;color:#111827">{h.escape(lead)}</p>' if lead else ""
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'</head><body style="margin:0;padding:0;background:#f3f4f6">'
            f'<div style="max-width:680px;margin:0 auto;padding:20px 14px;font-family:{FONT};color:#1f2937">'
            f'<div style="font-size:22px;font-weight:700;color:#111827;margin:0 0 2px 0">{h.escape(title)}</div>'
            f'<div style="font-size:13px;color:#6b7280;margin:0 0 16px 0">{h.escape(date)}</div>'
            f'{lead_html}{cards}'
            f'<div style="font-size:12px;line-height:1.5;color:#6b7280;margin:8px 0 0 0">{footer}</div>'
            f'</div></body></html>')


def daily_html(data: dict[str, Any], d: Digest, reply_to: str | None) -> str:
    cards = []
    refs = []
    for b in data["blocks"]:
        rows = []
        if b["warning"]:
            rows.append(f'<div style="background:#fef3c7;color:#92400e;border-radius:6px;padding:8px 10px;'
                        f'font-size:13px;margin:0 0 10px 0">⚠️ {h.escape(b["warning"])}</div>')
        for it in b["items"]:
            refs.append(it["ref"])
            label = ""
            if it["kind"] == "update":
                label = (f'<span style="background:{b["color"]}1a;color:{b["color"]};border-radius:4px;padding:1px 6px;'
                         f'font-size:12px;margin-right:6px">{h.escape(tr(d, "update"))} · {h.escape(tr(d, "since"))} '
                         f'{h.escape(it["since"])}</span>')
            elif it["kind"] == "reminder":
                label = (f'<span style="background:#f3f4f6;color:#374151;border-radius:4px;padding:1px 6px;font-size:12px;'
                         f'margin-right:6px">{h.escape(tr(d, "reminder"))}</span>')
            links = " · ".join(f'<a href="{h.escape(u)}" style="color:#6b7280">{h.escape(n)}</a>' for n, u in it["links"])
            comment = (f'<div style="font-size:13px;color:#4b5563;font-style:italic;margin:4px 0 0 0">'
                       f'{h.escape(tr(d, "experts"))}: {h.escape(it["comment"])}</div>') if it["comment"] else ""
            rows.append(
                f'<div style="margin:0 0 12px 0">'
                f'<div style="font-size:15px;font-weight:600;line-height:1.35;color:#111827">'
                f'<span style="color:{b["color"]};font-weight:700;margin-right:6px">{it["ref"]}</span>'
                f'{h.escape(it["headline"])}{_vote(it["ref"], data["subject"], reply_to, data["digest"], data["issue"])}</div>'
                f'<div style="font-size:14px;line-height:1.5;color:#374151;margin:3px 0 0 0">{label}{h.escape(it["summary"])}</div>'
                f'{comment}'
                f'<div style="font-size:12px;margin:3px 0 0 0">{links}</div></div>')
        if b["quiet"]:
            rows.append('<div style="font-size:13px;color:#6b7280;line-height:1.7">' + "<br>".join(
                f'<span style="color:#9ca3af">•</span> <b style="color:#4b5563">{h.escape(n)}</b> — {h.escape(tr(d, "quiet"))}'
                for n in b["quiet"]) + "</div>")
        if not b["items"] and not b["quiet"] and not b["warning"]:
            rows.append(f'<div style="font-size:14px;color:#6b7280">{h.escape(tr(d, "empty"))}</div>')
        if b["note"]:
            rows.append(f'<div style="font-size:13px;color:#6b7280;margin:8px 0 0 0">{h.escape(b["note"])}</div>')
        cards.append(_card(b["title"], b["color"], "".join(rows)))
    lead = data["lead"] or ("" if refs else tr(d, "nothing"))
    hint = ""
    if refs:
        hint = tr(d, "feedback").format(ex=refs[0], ex2=refs[1]) if len(refs) > 1 else tr(d, "feedback1").format(ex=refs[0])
    footer = h.escape(hint) + f'<br><span style="color:#d1d5db">{h.escape(tag(data["digest"], data["issue"]))}</span>'
    return _page(data["title"], data["date"], lead, "".join(cards), footer)


def review_html(markdown_text: str, d: Digest, title: str, date: str, digest_id: str, issue_id: int) -> str:
    """Review Markdown → one card per `## section` (block colours where the heading is a block title)."""
    text = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.S).strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip()
        lines = lines[1:]
    sections: list[tuple[str, list[str]]] = [("", [])]
    for ln in lines:
        if ln.startswith("## "):
            sections.append((ln[3:].strip(), []))
        else:
            sections[-1][1].append(ln)
    colors = {b.title.strip().lower(): block_color(d, i) for i, b in enumerate(d.blocks)}
    style = {"<p>": '<p style="margin:0 0 8px 0;font-size:14px;line-height:1.55;color:#374151">',
             "<ul>": '<ul style="margin:0 0 8px 0;padding-left:20px;font-size:14px;line-height:1.55;color:#374151">',
             "<li>": '<li style="margin:0 0 4px 0">', "<h3>": '<h3 style="font-size:14px;margin:10px 0 4px 0;color:#111827">',
             "<a ": '<a style="color:#2563eb" '}
    cards, lead = [], ""
    k = 0
    for name, body in sections:
        inner = mdlib.markdown("\n".join(body).strip(), extensions=["extra", "sane_lists"])
        for a, b in style.items():
            inner = inner.replace(a, b)
        if not name:
            lead = re.sub(r"<[^>]+>", "", inner).strip() and inner
            continue
        color = colors.get(name.lower(), PALETTE[k % len(PALETTE)])
        k += 1
        cards.append(_card(name, color, inner))
    page = _page(title, date, "", (lead or "") + "".join(cards), h.escape(tag(digest_id, issue_id)))
    return page


def review_subject(markdown_text: str, d: Digest, cadence: str, period: str) -> str:
    m = re.search(r"<!--\s*subject:\s*(.+?)\s*-->", markdown_text)
    if m:
        return m.group(1).strip()[:150]
    return f"{d.title}: {tr(d, cadence)} {period}"
