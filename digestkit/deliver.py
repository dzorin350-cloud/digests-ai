"""Delivery: file (always), e-mail (SMTP, HTML + 👍/👎 mailto links per item), Telegram bot."""

from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from typing import Any

import httpx

from digestkit.config import Digest, Recipient
from digestkit.store import Store, iso, now_utc

log = logging.getLogger(__name__)


def payload(store: Store, d: Digest, row: Any, keep: list[str]) -> tuple[str, str, str]:
    """-> (subject, Markdown text, HTML) of an issue for one recipient (only the blocks they get)."""
    from digestkit import render
    from digestkit.store import jload, parse_iso

    reply_to = os.environ.get("FEEDBACK_EMAIL") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or None
    data = jload(row["data"], {}) if "data" in row.keys() else {}
    created = parse_iso(row["created_at"]) or now_utc()
    if data.get("blocks"):
        part = render.only_blocks(data, keep)
        return data["subject"], render.daily_markdown(part, d), render.daily_html(part, d, reply_to)
    body = only_blocks(row["body"], d, keep)
    subj = row["subject"] if "subject" in row.keys() and row["subject"] else f"{d.title} {render.local_date(created, d.timezone)}"
    return subj, body, render.review_html(body, d, d.title, render.human_date(created, d), d.id, row["id"])


def only_blocks(body: str, d: Digest, keep: list[str]) -> str:
    """Drop `## <block title>` sections of blocks the recipient does not get (other sections stay)."""
    if not keep:
        return body
    drop = {b.title.strip().lower() for b in d.blocks if b.id not in keep}
    out, skipping = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            skipping = line[3:].strip().lower() in drop
        elif line.startswith("# ") or line.startswith("---") or line.startswith("<!--"):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def send_email(d: Digest, subj: str, body: str, to: list[str], html: str = "", issue_id: int = 0) -> bool:
    host = os.environ.get("SMTP_HOST")
    if not host or not to:
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, pwd = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM") or user
    reply_to = os.environ.get("FEEDBACK_EMAIL") or sender
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subj, sender, ", ".join(to)
    if reply_to:
        msg["Reply-To"] = reply_to
    # replies point back at this id (In-Reply-To), so the subject can stay a clean hook
    msg["Message-ID"] = f"<digest-{d.id}-{issue_id}.{uuid.uuid4().hex[:10]}@ai-digests>"
    msg["X-Digest"] = f"{d.id}#{issue_id}"
    msg.set_content(re.sub(r"<!--.*?-->", "", body, flags=re.S).strip() + f"\n\n[digest:{d.id} #{issue_id}]\n")
    if html:
        msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            if user:
                s.login(user, pwd)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            if port == 587:
                s.starttls(context=ctx)
            if user:
                s.login(user, pwd)
            s.send_message(msg)
    return True


def tg_html(body: str) -> str:
    """Markdown -> the small HTML subset Telegram accepts."""
    out = []
    for line in body.splitlines():
        if line.startswith("<!--"):
            continue
        line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", line)
        line = re.sub(r"^#{1,3}\s*(.+)$", r"<b>\1</b>", line)
        line = re.sub(r"^- ", "• ", line)
        line = line.rstrip()
        if line.strip() == "---":
            line = ""
        out.append(line.strip() if line.startswith("  ") else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def send_telegram(d: Digest, body: str, chat: str | None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not chat:
        return False
    text = tg_html(body)
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > 3900 and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    with httpx.Client(timeout=30) as c:
        for ch in chunks:
            r = c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": ch, "parse_mode": "HTML", "disable_web_page_preview": True})
            r.raise_for_status()
    return True


def deliver_issue(store: Store, d: Digest, issue_id: int) -> list[str]:
    row = store.one("SELECT * FROM issues WHERE id=?", (issue_id,))
    if row is None:
        return []
    done = ["file"] if row["path"] else []
    targets = [Recipient(name="", email=d.delivery.email, telegram_chat_id=d.delivery.telegram_chat_id)]
    targets += [r for r in d.recipients if row["cadence"] in r.cadences]
    for t in targets:
        subj, body, html = payload(store, d, row, t.blocks)
        label = f"@{t.name}" if t.name else ""
        for name, fn in (("email", lambda t=t, subj=subj, body=body, html=html: send_email(d, subj, body, t.email, html, issue_id)),
                         ("telegram", lambda t=t, body=body: send_telegram(d, body, t.telegram_chat_id))):
            try:
                if fn():
                    done.append(name + label)
            except Exception as e:
                log.warning("delivery via %s%s failed: %s", name, label, e)
                done.append(f"{name}{label}:failed ({type(e).__name__}: {e})")
    store.x("UPDATE issues SET delivered=? WHERE id=?", (",".join(done) + f" @ {iso(now_utc())}", issue_id))
    store.commit()
    return done
