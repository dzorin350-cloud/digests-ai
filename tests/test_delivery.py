"""Issue shape and delivery: every watched position listed, expert views, hook subject, HTML cards, reply matching."""

from __future__ import annotations

import email
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from test_novelty import DOWNGRADE, World, day, run_day

from digestkit import config, daily, deliver, feedback, render, review
from digestkit.store import Store, jload


def _cfg(home: Path, extra: str = "") -> config.Digest:
    p = home / "digests" / "test.yaml"
    text = p.read_text(encoding="utf-8").replace(
        "    require_watch_match: true\n", "    require_watch_match: true\n    show_quiet_watch: true\n    expert_comments: true\n")
    p.write_text(text + extra, encoding="utf-8")
    return config.load("test")


def _publish_one(home: Path, dec_extra: dict[str, Any] | None = None) -> tuple[Store, config.Digest, int]:
    d = _cfg(home)
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    st = Store()
    import asyncio

    from digestkit.collect import collect

    asyncio.run(collect(st, d, day(0), w.fetcher(0)))
    pack = daily.build_pack(st, d, day(0))
    c = pack["blocks"][0]["candidates"][0]
    assert pack["blocks"][0]["expert_comments"] is True
    dec = {"pack_id": pack["pack_id"], "items": [{"candidate": c["id"], "decision": "new", "headline": "АКРА понизила рейтинг Полипласта до BBB-",
           "summary": "Прогноз негативный.", "comment": "Аналитики Совкомбанка: давление на котировки сохранится",
           "facts": ["BBB-(RU)"], "importance": 3}], **(dec_extra or {})}
    res = daily.publish(st, d, daily.Decisions.model_validate(dec), day(0), deliver=False)
    return st, d, res["issue_id"]


def test_quiet_positions_and_expert_views(home: Path) -> None:
    st, d, issue = _publish_one(home)
    row = st.one("SELECT * FROM issues WHERE id=?", (issue,))
    md = row["body"]
    assert "- **РЖД** — новостей нет" in md  # every watched position gets a line
    assert "- **Полипласт** — новостей нет" not in md
    assert "_Мнения: Аналитики Совкомбанка: давление на котировки сохранится_" in md
    data = jload(row["data"], {})
    assert data["blocks"][0]["quiet"] == ["РЖД"]
    # the macro block has no watchlist: plain "nothing significant"
    assert "## Экономика\n\n_Значимых новостей нет._" in md
    html = Path(row["path"]).with_suffix(".html").read_text(encoding="utf-8")
    assert "РЖД</b> — новостей нет" in html and "Мнения:" in html
    st.close()


def test_expert_views_hidden_when_block_does_not_want_them(home: Path) -> None:
    d = config.load("test")  # expert_comments false
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    with Store() as st:
        pack, _ = run_day(st, d, w, 0)
        body = st.one("SELECT body FROM issues ORDER BY id DESC")["body"]
    assert "Мнения" not in body and "— новостей нет" not in body


def test_subject_hook_and_fallback(home: Path) -> None:
    st, d, issue = _publish_one(home, {"subject": "Полипласт: рейтинг снижен до BBB-"})
    assert st.one("SELECT subject FROM issues WHERE id=?", (issue,))["subject"] == "Полипласт: рейтинг снижен до BBB-"
    st.close()
    blocks = [{"items": [{"headline": "A", "importance": 1}, {"headline": "B", "importance": 3}, {"headline": "C", "importance": 2}]}]
    assert render.fallback_subject(config.load("test"), blocks) == "B · C · ещё 1"
    assert render.fallback_subject(config.load("test"), [{"items": []}]) == "Тестовый дайджест: ничего нового"


def test_email_message(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def __enter__(self) -> FakeSMTP:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

        def starttls(self, **k: Any) -> None:
            pass

        def login(self, *a: Any) -> None:
            pass

        def send_message(self, m: EmailMessage) -> None:
            sent.append(m)

    monkeypatch.setattr(deliver.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.setenv("SMTP_FROM", "digest@example.com")
    st, d, issue = _publish_one(home, {"subject": "Полипласт: рейтинг снижен"})
    d.delivery.email = ["me@example.com"]
    assert deliver.deliver_issue(st, d, issue) == ["file", "email"]
    m = sent[0]
    assert m["Subject"] == "Полипласт: рейтинг снижен"  # no technical tag in the subject
    assert m["Message-ID"].startswith(f"<digest-test-{issue}.")
    html = m.get_body(("html",)).get_content()
    assert "border-left:4px solid" in html and "mailto:digest@example.com" in html
    assert "%5Bdigest%3Atest%20%23" in html  # vote links carry the issue tag in the body
    assert f"[digest:test #{issue}]" in m.get_body(("plain",)).get_content()

    # a reply to it is matched by In-Reply-To, even with an unrelated subject
    reply = email.message_from_string(
        f"Message-ID: <r1@mail>\nIn-Reply-To: {m['Message-ID']}\nSubject: Re: whatever\n"
        "Content-Type: text/plain; charset=utf-8\n\nB1 - уже знал\n\n> quoted digest\n")
    r = feedback.apply_email(st, d, reply, "x")
    assert r is not None and r["rated"] == ["B1-"] and r["issue_id"] == issue
    assert feedback.apply_email(st, d, reply, "x") is None  # processed once
    # a 👍 link reply: tag in the body
    up = email.message_from_string(f"Message-ID: <r2@mail>\nSubject: Re: x\n\nB1 +\n\n[digest:test #{issue}]\n")
    assert feedback.apply_email(st, d, up, "y")["rated"] == ["B1+"]  # type: ignore[index]
    # our own issue in the inbox is not feedback
    own = email.message_from_string(f"Message-ID: {m['Message-ID']}\nSubject: x\n\n[digest:test #{issue}]\nB1 +")
    assert feedback.apply_email(st, d, own, "z") is None
    st.close()


def test_sources_down_warning(home: Path) -> None:
    d = config.load("test")
    from digestkit import sources as reg

    with Store() as st:
        reg.sync(st, d, day(0))
        for s in d.block("macro").sources:
            reg.record_run(st, d, "macro", s.id, False, "ConnectError", day(0))
        pack = daily.build_pack(st, d, day(0))
        daily.publish(st, d, daily.Decisions(pack_id=pack["pack_id"]), day(0), deliver=False)
        body = st.one("SELECT body FROM issues ORDER BY id DESC")["body"]
    assert "Источники блока не ответили" in body.split("## Экономика")[1]
    assert "Источники блока не ответили" not in body.split("## Экономика")[0]


def test_review_html_cards_and_subject(home: Path) -> None:
    d = config.load("test")
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    with Store() as st:
        run_day(st, d, w, 0)
        pack = review.build_pack(st, d, "weekly", day(3))
        md = ("<!-- subject: Неделя: Полипласт под давлением -->\n# Разбор недели\n\n- главное\n\n"
              "## Мои облигации\n\nПолипласт: рейтинг снижен.\n\n## Экономика\n\nТихо.")
        res = review.publish_review(st, d, pack["pack_id"], md, day(3), deliver=False)
        row = st.one("SELECT * FROM issues WHERE id=?", (res["issue_id"],))
        subj, text, html = deliver.payload(st, d, row, [])
    assert subj == "Неделя: Полипласт под давлением"
    assert html.count("border-left:4px solid") == 2 and "Разбор недели" in html
    assert Path(res["path"]).with_suffix(".html").exists()
    with Store() as st2:
        _, text2, _ = deliver.payload(st2, d, row, ["macro"])
    assert "Мои облигации" not in text2 and "Тихо." in text2
