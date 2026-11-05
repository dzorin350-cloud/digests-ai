from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from conftest import FIX
from test_novelty import DOWNGRADE, World, day, run_day

from digestkit import cli, config, daily, feedback, review, textsim
from digestkit import sources as reg
from digestkit.collect import adapters, collect
from digestkit.deliver import tg_html
from digestkit.store import Store


# ---------------- parsing ----------------
def test_rss_fixture() -> None:
    docs = adapters.parse_feed((FIX / "rss_securelist.xml").read_bytes())
    assert len(docs) >= 5 and all(d.url.startswith("http") and d.title for d in docs)
    assert docs[0].published_at is not None


def test_telegram_fixture() -> None:
    docs = adapters.parse_channel((FIX / "tg_true_secator.html").read_text(encoding="utf-8"))
    assert docs and all(d.url.startswith("https://t.me/") for d in docs)
    assert docs[0].published_at and docs[0].published_at.tzinfo


def test_html_listing_and_guess() -> None:
    html = (FIX / "list_www_f6_ru_blog.html").read_text(encoding="utf-8")
    links = adapters.list_links(html, "https://www.f6.ru/blog/", {"link_pattern": r"^https://www\.f6\.ru/blog/[a-z0-9-]{4,}/$"})
    assert len(links) >= 5
    pat = adapters.guess_link_pattern(html, "https://www.f6.ru/blog/")
    assert pat and "f6" in pat


def test_search_feed_parsing() -> None:
    xml = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
    <item><title>АКРА понизило рейтинг Полипласта - РБК</title><link>https://news.google.com/rss/articles/abc</link>
    <pubDate>Tue, 27 Oct 2026 07:00:00 GMT</pubDate><description>&lt;a href="x"&gt;АКРА понизило рейтинг Полипласта&lt;/a&gt;</description>
    <source url="https://www.rbc.ru">РБК</source></item>
    <item><title>Bing item</title><link>http://www.bing.com/news/apiclick.aspx?url=https%3a%2f%2fexample.com%2fa&amp;c=1</link>
    <description>Snippet text</description></item></channel></rss>"""
    docs = adapters.parse_search(xml)
    assert docs[0].title == "АКРА понизило рейтинг Полипласта" and docs[0].source_name == "РБК"
    assert docs[1].url == "https://example.com/a"
    assert "when%3A2d" in adapters.search_url("google", "Полипласт", "ru", "RU", 2)


def test_expand_watch_queries(home: Path) -> None:
    d = config.load("test")
    b = d.block("bonds")
    qs = adapters.expand_queries(b.sources[0], b.watchlist)
    assert [w for _, w in qs] == [["Полипласт"], ["РЖД"]]


# ---------------- text ----------------
def test_mentions_tolerate_endings() -> None:
    assert textsim.mentions("Облигации Полипласта упали", ["Полипласт"])
    assert textsim.mentions("рейтинг ВУШ Холдинг", ["ВУШ"])
    assert not textsim.mentions("вушные раковины", ["ВУШ"])
    assert not textsim.mentions("АФК Системный подход", ["РЖД"])


def test_date_hints() -> None:
    ref = datetime(2026, 11, 5, tzinfo=UTC)
    assert textsim.date_hints("решение от 27 октября и выплата 03.12.2026", ref) == ["2026-10-27", "2026-12-03"]
    assert textsim.date_hints("on Dec 28 the firm", datetime(2027, 1, 3, tzinfo=UTC)) == ["2026-12-28"]


def test_similarity_is_meaningful() -> None:
    a = textsim.hashed_vec("АКРА понизило рейтинг Полипласта", "агентство понизило кредитный рейтинг")
    b = textsim.hashed_vec("Полипласту понизили рейтинг: АКРА", "кредитный рейтинг понижен")
    c = textsim.hashed_vec("OpenAI выпустила новую модель", "модель для агентов")
    assert float(a @ b) > 0.4 > float(a @ c)


# ---------------- feedback ----------------
def test_feedback_parse() -> None:
    got = feedback.parse("E1 +, B2 −\n👍A3 больше такого\nЕ4 минус — это реклама\nменьше про котировки")
    assert ("E1", 1, "") in got and ("B2", -1, "") in got
    assert ("A3", 1, "больше такого") in got
    assert ("E4", -1, "это реклама") in got  # Cyrillic Е works
    assert (None, 0, "меньше про котировки") in got


def test_feedback_apply(home: Path) -> None:
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    d = config.load("test")
    with Store() as st:
        _, res = run_day(st, d, w, 0)
        r = feedback.apply(st, d, "B1 - уже знал\nZ9 +\nЭкономика: больше про бюджет", channel="email", ext_id="m1")
        assert r["rated"] == ["B1-"] and r["unknown_refs"] == ["Z9"]
        assert feedback.apply(st, d, "B1 -", ext_id="m1") == {"skipped": "already processed"}
        assert st.one("SELECT rating FROM stories WHERE status='reported'")["rating"] == -1
        prefs = st.q("SELECT block, text FROM feedback WHERE text!=''")
        assert {(p["block"], p["text"]) for p in prefs} == {("bonds", "уже знал"), (None, "Экономика: больше про бюджет")} or \
            ("macro", "Экономика: больше про бюджет") in {(p["block"], p["text"]) for p in prefs}
        # the next pack carries the preferences
        pack = daily.build_pack(st, d, day(1))
        bonds = pack["blocks"][0]
        assert "уже знал" in bonds["preferences"]["reader_notes"]
        assert bonds["preferences"]["disliked"]


def test_strip_quoted() -> None:
    body = "B1 +\nспасибо\n\nOn Mon, 5 Nov 2026 Digest wrote:\n> # Дайджест\n> - [B1]"
    assert feedback.strip_quoted(body) == "B1 +\nспасибо"


# ---------------- delivery formatting ----------------
def test_tg_html() -> None:
    out = tg_html("# Заголовок\n\n- **[B1] A & B**  \n  текст — [РБК](https://rbc.ru/x)\n\n<!-- digest:x -->")
    assert "<b>Заголовок</b>" in out and "&amp;" in out and '<a href="https://rbc.ru/x">РБК</a>' in out
    assert "digest:x" not in out


# ---------------- reviews ----------------
def test_weekly_review_threads(home: Path) -> None:
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    w.add("search", 3, "Полипласт объявил оферту по облигациям после понижения рейтинга",
          "Полипласт назначил оферту по выпуску П02-БО-01 после понижения рейтинга АКРА.", publisher="Интерфакс",
          watch=["Полипласт"])
    d = config.load("test")
    with Store() as st:
        for n in range(5):
            run_day(st, d, w, n)
        pack = review.build_pack(st, d, "weekly", day(5))
        th = pack["blocks"][0]["threads"]
        assert len(th) == 1 and [e["kind"] for e in th[0]["timeline"]] == ["new", "update"]
        assert pack["daily_issues_in_period"] == 5
        res = review.publish_review(st, d, pack["pack_id"], "# Разбор\n\nТекст", day(5), deliver=False)
        assert Path(res["path"]).read_text(encoding="utf-8").startswith("# Разбор")
        mpack = review.build_pack(st, d, "monthly", day(6))
        assert mpack["earlier_reviews"] and mpack["earlier_reviews"][0]["markdown"].startswith("# Разбор")


def test_due(home: Path) -> None:
    d = config.load("test")
    sunday = datetime(2026, 11, 8, 6, tzinfo=UTC)  # weekday 6 in config
    with Store() as st:
        assert cli.due(st, d, sunday) == ["daily"]  # no data yet: no reviews
        w = World()
        w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
        run_day(st, d, w, 0)
        assert "weekly" in cli.due(st, d, sunday)
        assert cli.due(st, d, datetime(2026, 12, 1, 6, tzinfo=UTC)) == ["daily", "monthly"]


# ---------------- sources ----------------
def test_source_health(home: Path) -> None:
    d = config.load("test")
    with Store() as st:
        reg.sync(st, d, day(-20))
        sid = d.block("macro").sources[0].id
        for k in range(4):
            reg.record_run(st, d, "macro", sid, False, "ConnectError", day(k))
        assert st.one("SELECT status FROM sources WHERE source_id=?", (sid,))["status"] == "broken"
        # not retried right away, retried after a few days
        assert all(e.source.id != sid for e in reg.runnable(st, d, day(4)))
        assert any(e.source.id == sid for e in reg.runnable(st, d, day(8)))
        reg.record_run(st, d, "macro", sid, True, None, day(8))
        assert st.one("SELECT status FROM sources WHERE source_id=?", (sid,))["status"] == "active"

        # a source that floods without anything ever selected gets muted
        w = World()
        for k in range(16):
            w.add("ЦБ", 0, f"Пресс-релиз номер {k} о разном {k * 7}", f"Текст релиза {k} про разные вещи {k * 13} и {k * 17}.")
        asyncio.run(collect(st, d, day(0), w.fetcher(0)))
        pack = daily.build_pack(st, d, day(0))
        res = daily.publish(st, d, daily.Decisions(pack_id=pack["pack_id"]), day(0), deliver=False)
        assert any("приглушён" in c for c in res["source_changes"])
        assert reg.health(st, d, day(0) + timedelta(hours=1)) == []
        assert any("приглушён" in r["text"] for r in st.q("SELECT text FROM changes"))
        assert st.one("SELECT status FROM sources WHERE source_id=?", (sid,))["status"] == "muted"


def test_sync_removes_and_disables(home: Path) -> None:
    d = config.load("test")
    with Store() as st:
        reg.sync(st, d)
        d.block("macro").sources = []
        d.block("bonds").sources[1].enabled = False
        reg.sync(st, d)
        st_ = {r["source_id"]: r["status"] for r in st.q("SELECT * FROM sources")}
    assert "removed" in st_.values() and "disabled" in st_.values()


# ---------------- CLI ----------------
def test_cli_flow(home: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    w = World()
    w.add("ЦБ", 0, "ЦБ сохранил ключевую ставку 16,5%", "Совет директоров Банка России сохранил ключевую ставку 16,5% годовых.")
    d = config.load("test")
    with Store() as st:
        asyncio.run(collect(st, d, day(0), w.fetcher(0)))
    now = day(0).isoformat()
    assert cli.main(["collect", "test", "--no-fetch", "--now", now, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    pack = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    c = pack["blocks"][1]["candidates"][0]
    dec = home / "dec.json"
    dec.write_text(json.dumps({"pack_id": pack["pack_id"], "lead": "Главное: ставка без изменений.", "items": [
        {"candidate": c["id"], "decision": "new", "headline": "ЦБ сохранил ставку 16,5%", "summary": "Без изменений.",
         "facts": ["ставка 16,5%"], "importance": 3}]}), encoding="utf-8")
    assert cli.main(["publish", "test", "--decisions", str(dec), "--now", now, "--no-deliver"]) == 0
    capsys.readouterr()
    md = next((home / "out" / "test").glob("*-daily.md")).read_text(encoding="utf-8")
    assert "**[E1] ЦБ сохранил ставку 16,5%**" in md and "## Мои облигации" in md and "Значимых новостей нет" in md
    assert "«E1 +» или «E1 −" in md
    assert cli.main(["feedback", "test", "E1 +"]) == 0
    assert "E1+" in capsys.readouterr().out
    assert cli.main(["status", "test", "--json"]) == 0
    st_ = json.loads(capsys.readouterr().out)
    assert st_["feedback"]["up"] == 1
    assert cli.main(["prompt", "daily"]) == 0
    assert "stale" in capsys.readouterr().out
    assert cli.main(["init", "mine", "--template", "morning", "--title", "Мой утренний"]) == 0
    assert config.load("mine").title == "Мой утренний"


def test_templates_valid() -> None:
    root = Path(__file__).parents[1] / "digestkit" / "templates"
    for p in root.glob("*.yaml"):
        d = config.load(str(p))
        assert d.blocks and all(b.sources for b in d.blocks)
