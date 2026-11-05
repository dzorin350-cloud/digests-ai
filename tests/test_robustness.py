"""Reliability and extension points: rollback, pruning, embeddings failing mid-run, custom sources, agent items."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from test_novelty import DOWNGRADE, World, day, run_day

from digestkit import cli, config, daily, vectors
from digestkit.collect import RawDoc, collect
from digestkit.collect.adapters import docs_from_json, parse_json_items, run_command
from digestkit.store import Store


def test_error_rolls_back_the_run(home: Path) -> None:
    w = World()
    w.add("ЦБ", 0, "ЦБ сохранил ключевую ставку", "Совет директоров Банка России сохранил ключевую ставку на уровне 16,5%.")
    d = config.load("test")
    with Store() as st:
        asyncio.run(collect(st, d, day(0), w.fetcher(0)))
        pack = daily.build_pack(st, d, day(0))
    bad = daily.Decisions.model_validate({"pack_id": pack["pack_id"], "items": [
        {"candidate": pack["blocks"][1]["candidates"][0]["id"], "decision": "new", "headline": ""}]})
    with pytest.raises(daily.PublishError), Store() as st:
        daily.publish(st, d, bad, day(0), deliver=False)
    with Store() as st:  # nothing half-written: no issue, pack still open, can be published properly
        assert st.one("SELECT COUNT(*) AS n FROM issues")["n"] == 0
        assert st.one("SELECT status FROM packs WHERE id=?", (pack["pack_id"],))["status"] == "open"
        ok = daily.publish(st, d, daily.Decisions(pack_id=pack["pack_id"]), day(0), deliver=False)
        assert ok["issue_id"]


def test_lenient_llm_decisions() -> None:
    dec, bad = daily.lenient_decisions({"pack_id": 3, "lead": None, "items": [
        {"candidate": "c1", "decision": "skip"}, {"candidate": "c2", "decision": "repeat", "related": 41}, "junk"],
        "block_notes": "oops"})
    assert [i.candidate for i in dec.items] == ["c2"] and dec.items[0].related == "s41"
    assert len(bad) == 2 and dec.block_notes == {}


def test_prune_keeps_memory(home: Path) -> None:
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    w.add("Интерфакс", 0, "Старое", "Совсем старая заметка про Полипласт и его облигации.", published=day(-30))
    d = config.load("test")
    with Store() as st:
        run_day(st, d, w, 0)
        res = st.prune(day(200))
        assert res["items_deleted"] == 1 and res["texts_blanked"] == 1
        assert st.one("SELECT COUNT(*) AS n FROM stories WHERE status='reported'")["n"] == 1
        assert st.one("SELECT COUNT(*) AS n FROM items")["n"] == 1  # the URL still deduplicates


def test_embeddings_failing_mid_run_fall_back(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBED_BASE_URL", "http://embed.invalid/v1")
    monkeypatch.setenv("EMBED_MODEL", "m")
    calls = {"n": 0}

    def flaky(self: vectors.Vectorizer, texts: list[str]) -> np.ndarray:
        calls["n"] += 1
        if calls["n"] > 1:
            raise ConnectionError("down")
        m = np.random.default_rng(0).normal(size=(len(texts), 8)).astype(np.float32)
        return m / np.linalg.norm(m, axis=1, keepdims=True)

    monkeypatch.setattr(vectors.Vectorizer, "_embed", flaky)
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    w.add("search", 1, "Облигации Полипласта подешевели после понижения рейтинга", "Бумаги Полипласта снизились после решения АКРА.",
          publisher="Смартлаб", watch=["Полипласт"])
    d = config.load("test")
    with Store() as st:
        calls["n"] = -10  # day 0 works with embeddings
        run_day(st, d, w, 0)
        calls["n"] = 1  # day 1: items embed, stories fail -> both recomputed as hashed, no shape error
        pack, _ = run_day(st, d, w, 1)
    assert pack["blocks"][0]["candidates"][0]["related"]


def test_json_items_formats() -> None:
    feed = {"version": "https://jsonfeed.org/version/1.1", "items": [
        {"id": "1", "url": "https://a.example/1", "title": "Новость", "content_html": "<p>Текст <b>новости</b></p>",
         "date_published": "2026-10-27T05:00:00Z"}]}
    docs = docs_from_json(feed)
    assert docs[0].text == "Текст\nновости" or "новости" in docs[0].text
    assert docs[0].published_at is not None and docs[0].published_at.tzinfo
    lines = '{"link": "https://b.example", "headline": "Заголовок", "summary": "s", "publisher": {"name": "X"}}\n{"bad": 1}'
    got = parse_json_items(lines)
    assert len(got) == 1 and got[0].source_name == "X"


def test_command_source(home: Path) -> None:
    script = home / "src.py"
    script.write_text('import json; print(json.dumps([{"url": "https://c.example/1", "title": "ЦБ сохранил ставку", '
                      '"text": "Банк России сохранил ключевую ставку 16,5%.", "published_at": "2026-10-27T05:00:00+00:00"}]))',
                      encoding="utf-8")
    docs = asyncio.run(run_command(f'"{sys.executable}" "{script}"'))
    assert docs[0].title == "ЦБ сохранил ставку"
    with pytest.raises(RuntimeError):
        asyncio.run(run_command(f'"{sys.executable}" -c "import sys; sys.exit(3)"'))
    cfg = (home / "digests" / "test.yaml").read_text(encoding="utf-8")
    assert config.Source(kind="command", command="x").id.startswith("cmd-")
    assert "blocks" in cfg


def test_agent_added_items_join_the_pipeline(home: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    f = home / "found.jsonl"
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"url": "https://x.example/1", "title": "РЖД разместила облигации на 20 млрд руб.", "text": "РЖД разместила облигации.",
         "published_at": day(0).isoformat(), "source": "web-search"},
        {"url": "https://x.example/2", "title": "Новость без бумаг из списка", "text": "Про погоду.",
         "published_at": day(0).isoformat()}]), encoding="utf-8")
    assert cli.main(["add-items", "test", "--block", "bonds", "--file", str(f), "--now", day(0).isoformat()]) == 0
    assert "'new': 1" in capsys.readouterr().out
    d = config.load("test")
    with Store() as st:
        pack = daily.build_pack(st, d, day(0) + timedelta(hours=1))
        c = pack["blocks"][0]["candidates"]
        assert len(c) == 1 and c[0]["watch"] == ["РЖД"] and c[0]["items"][0]["source"] == "web-search"
        # added items never become a runnable source
        assert all(e.source.id != "manual-agent" for e in __import__("digestkit.sources", fromlist=["x"]).runnable(st, d, day(0)))
    assert RawDoc  # re-exported


def test_human_date_has_no_platform_specific_format(home: Path) -> None:
    from digestkit.render import human_date

    d = config.load("test")
    d.language = "en"
    assert human_date(day(0), d) == "October 27, 2026"


def test_recipients_get_their_blocks(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from digestkit import deliver

    cfg = (home / "digests" / "test.yaml").read_text(encoding="utf-8")
    cfg += """
recipients:
  - {name: boss, email: boss@example.com, blocks: [macro], cadences: [weekly, monthly]}
  - {name: analyst, telegram_chat_id: "42", blocks: [bonds]}
"""
    (home / "digests" / "test.yaml").write_text(cfg, encoding="utf-8")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(deliver, "send_email", lambda d, s, body, to, *a: bool(to) and not sent.append((",".join(to), body)))
    monkeypatch.setattr(deliver, "send_telegram", lambda d, body, chat: bool(chat) and not sent.append((str(chat), body)))
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    d = config.load("test")
    with Store() as st:
        _, res = run_day(st, d, w, 0)
        got = deliver.deliver_issue(st, d, res["issue_id"])
    assert got == ["file", "telegram@analyst"]  # boss gets only reviews
    body = sent[0][1]
    assert "## Мои облигации" in body and "## Экономика" not in body and "[B1]" in body
    with pytest.raises(ValueError):
        config.Digest.model_validate({"title": "x", "blocks": [{"id": "a", "title": "A", "brief": "b"}],
                                      "recipients": [{"name": "n", "blocks": ["zzz"]}]})
