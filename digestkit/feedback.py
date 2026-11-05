"""Reader feedback: "E1 +, B2 −, меньше про котировки" from a reply e-mail, a Telegram message or the CLI.

Effects: story rating (liked / disliked titles go into the next packs as examples), free-text notes go into
block preferences (the agent reads them every day and can fold them into the YAML brief), source stats
(sources whose items are disliked get muted).
"""

from __future__ import annotations

import email
import email.header
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.message import Message
from typing import Any

import httpx

from digestkit.config import Digest
from digestkit.store import Store, iso, jload, now_utc

_HOMO = str.maketrans("АВЕКМНОРСТХ", "ABEKMHOPCTX")
_REF_LIKE = re.compile(r"(?<![\w])([А-ЯA-Z]{1,2})(?=\d)")
_POS = r"(?:\+|👍|плюс|круто|класс|полезно|good|yes)"
_NEG = r"(?:-|−|–|—|👎|минус|мимо|не\s+нужно|не\s+интересно|bad|no)"
_REF = r"([A-Z]{1,2}\d{1,2})"
_AFTER = re.compile(rf"(?<![\w]){_REF}\s*[:]?\s*(?:({_POS})|({_NEG}))(?!\w)", re.I)
_BEFORE = re.compile(rf"(?:(\+|👍)|(-|−|👎))\s*{_REF}(?!\w)")


def parse(text: str) -> list[tuple[str | None, int, str]]:
    """-> [(ref|None, value, note)]. One line may rate several refs; the rest of the line is the note."""
    out: list[tuple[str | None, int, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        norm = _REF_LIKE.sub(lambda m: m.group(1).translate(_HOMO), line)  # Cyrillic "Е1" -> "E1", same length
        found: list[tuple[str, int, tuple[int, int]]] = []
        for m in _AFTER.finditer(norm):
            found.append((m.group(1).upper(), 1 if m.group(2) else -1, m.span()))
        for m in _BEFORE.finditer(norm):
            if not any(a <= m.start() < b for _, _, (a, b) in found):
                found.append((m.group(3).upper(), 1 if m.group(1) else -1, m.span()))
        found.sort(key=lambda x: x[2][0])
        note = line
        for _, _, (a, b) in sorted(found, key=lambda x: -x[2][0]):
            note = note[:a] + " " + note[b:]
        note = re.sub(r"^[\s,;.:\-–—]+|[\s,;]+$", "", re.sub(r"\s{2,}", " ", note)).strip()
        if len(note) < 3:
            note = ""
        if not found:
            if note:
                out.append((None, 0, note))
            continue
        for k, (ref, val, _) in enumerate(found):
            out.append((ref, val, note if k == 0 else ""))
    return out


def _block_by_ref(d: Digest, ref: str) -> str | None:
    letters = re.match(r"[A-Z]+", ref)
    for b in d.blocks:
        if letters and b.ref == letters.group(0):
            return b.id
    return None


def _block_by_text(d: Digest, text: str) -> str | None:
    low = text.lower()
    hits = [b.id for b in d.blocks if b.title.lower() in low or re.search(rf"\b{re.escape(b.id.lower())}\b", low)]
    return hits[0] if len(hits) == 1 else None


def apply(store: Store, d: Digest, text: str, issue_id: int | None = None, channel: str = "cli",
          ext_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    if ext_id and store.one("SELECT 1 FROM feedback WHERE digest=? AND ext_id=?", (d.id, ext_id)):
        return {"skipped": "already processed"}
    if issue_id is None:
        row = store.one("SELECT id FROM issues WHERE digest=? AND cadence='daily' ORDER BY created_at DESC LIMIT 1", (d.id,))
        issue_id = row["id"] if row else None
    issue = store.one("SELECT * FROM issues WHERE id=? AND digest=?", (issue_id, d.id)) if issue_id else None
    refs: dict[str, int] = jload(issue["refs"], {}) if issue else {}
    rated, notes, unknown = [], [], []
    first = True
    for ref, val, note in parse(text):
        story_id = refs.get(ref) if ref else None
        if ref and story_id is None:
            unknown.append(ref)
        block = _block_by_ref(d, ref) if ref else _block_by_text(d, note)
        store.x("INSERT INTO feedback(digest, issue_id, ref, story_id, block, value, text, channel, created_at, ext_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (d.id, issue_id, ref, story_id, block, val, note, channel, iso(now), ext_id if first else None))
        first = False
        if story_id is not None and val:
            store.x("UPDATE stories SET rating=rating+? WHERE id=?", (val, story_id))
            rated.append(f"{ref}{'+' if val > 0 else '-'}")
        if note:
            notes.append(note)
    store.commit()
    return {"issue_id": issue_id, "rated": rated, "notes": notes, "unknown_refs": unknown}


# ---------------- pulling replies ----------------
def _plain(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True)
    return payload.decode(msg.get_content_charset() or "utf-8", "replace") if isinstance(payload, bytes) else ""


def strip_quoted(body: str) -> str:
    out = []
    for line in body.splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^(On .+ wrote:|.+ пишет:|.+написал\(а\):|-----Original Message-----|-- $)", s):
            break
        out.append(line)
    return "\n".join(out).strip()


def issue_of_email(msg: Message, digest_id: str, body: str) -> int | None:
    for hdr in ("In-Reply-To", "References"):
        m = re.search(rf"digest-{re.escape(digest_id)}-(\d+)\.", msg.get(hdr, "") or "")
        if m:
            return int(m.group(1))
    m = re.search(rf"\[digest:{re.escape(digest_id)} #(\d+)\]", body)
    return int(m.group(1)) if m else None


def apply_email(store: Store, d: Digest, msg: Message, fallback_id: str) -> dict[str, Any] | None:
    """One e-mail -> feedback. Ignores the digest itself and messages that do not belong to this digest."""
    mid = msg.get("Message-ID") or fallback_id
    if re.search(rf"<digest-{re.escape(d.id)}-\d+\.", mid):
        return None  # our own issue (e.g. sent to oneself)
    body = _plain(msg)
    issue = issue_of_email(msg, d.id, body)
    if issue is None:
        return None
    text = strip_quoted(body)
    text = re.sub(rf"\[digest:{re.escape(d.id)} #\d+\]", "", text).strip()
    if not text:
        return None
    r = apply(store, d, text, issue, "email", mid)
    return None if "skipped" in r else r


def pull_email(store: Store, d: Digest) -> list[dict[str, Any]]:
    host = os.environ.get("IMAP_HOST")
    if not host:
        return []
    user, pwd = os.environ.get("IMAP_USER", ""), os.environ.get("IMAP_PASSWORD", "")
    out = []
    with imaplib.IMAP4_SSL(host, int(os.environ.get("IMAP_PORT", "993"))) as m:
        m.login(user, pwd)
        m.select(os.environ.get("IMAP_FOLDER", "INBOX"))
        since = (now_utc() - timedelta(days=30)).strftime("%d-%b-%Y")
        # replies carry In-Reply-To <digest-<id>-<issue>…>; 👍/👎 mailto links put [digest:<id> #N] in the body
        _, data = m.search(None, "SINCE", since, "OR", "HEADER", "In-Reply-To", f"digest-{d.id}-",
                           "BODY", f"digest:{d.id}")
        for num in (data[0] or b"").split()[-100:]:
            _, msg_data = m.fetch(num.decode(), "(RFC822)")
            raw = msg_data[0][1] if msg_data and isinstance(msg_data[0], tuple) else None
            if not isinstance(raw, bytes):
                continue
            r = apply_email(store, d, email.message_from_bytes(raw), f"imap-{num.decode()}")
            if r is not None:
                out.append(r)
    return out


def pull_telegram(store: Store, d: Digest) -> list[dict[str, Any]]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chats = {str(c) for c in [d.delivery.telegram_chat_id, *[r.telegram_chat_id for r in d.recipients]] if c}
    if not token or not chats:
        return []
    api = f"https://api.telegram.org/bot{token}"
    r = httpx.get(f"{api}/getUpdates", params={"timeout": 0}, timeout=30)
    r.raise_for_status()
    updates = r.json().get("result", [])
    out = []
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) not in chats or not msg.get("text") or msg["text"].startswith("/"):
            continue
        res = apply(store, d, msg["text"], None, "telegram", f"tg-{u['update_id']}")
        if "skipped" not in res:
            out.append(res)
    if updates:  # confirm, so Telegram does not keep re-sending them
        httpx.get(f"{api}/getUpdates", params={"offset": updates[-1]["update_id"] + 1, "timeout": 0}, timeout=30)
    return out
