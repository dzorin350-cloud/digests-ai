"""The point of the project: an event is reported once; reprints, re-tellings and stale events are not news."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from digestkit import config, daily
from digestkit.collect import RawDoc, collect
from digestkit.config import Block, Source
from digestkit.store import Store

T0 = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)
DOWNGRADE = ("АКРА понизило кредитный рейтинг Полипласта до BBB-(RU)",
             "Рейтинговое агентство АКРА понизило кредитный рейтинг ПАО «Полипласт» с уровня A-(RU) до BBB-(RU), "
             "прогноз негативный. Агентство указывает на рост долговой нагрузки и снижение рентабельности. "
             "Облигации компании в обращении: серии П02-БО-01, П02-БО-02.")


RETELL = [
    ("Облигации Полипласта подешевели после понижения рейтинга АКРА",
     "Бумаги Полипласта снизились в цене после того, как АКРА понизило рейтинг компании до BBB-(RU)."),
    ("Инвесторы распродают бумаги «Полипласта»", "После снижения рейтинга АКРА доходность выпусков Полипласта выросла."),
    ("Что будет с облигациями Полипласта после даунгрейда", "Разбираем, чем грозит держателям понижение рейтинга Полипласта агентством АКРА."),
    ("Полипласт: рейтинг снижен, что делать держателям", "АКРА понизило рейтинг, держатели облигаций Полипласта обсуждают продажу бумаг."),
]


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


class World:
    """Fake internet: what each source returns on each day."""

    def __init__(self) -> None:
        self.feeds: dict[tuple[str, int], list[RawDoc]] = {}

    def add(self, source_name: str, n: int, title: str, text: str, published: datetime | None = None,
            url: str | None = None, publisher: str = "", watch: list[str] | None = None) -> None:
        self.feeds.setdefault((source_name, n), []).append(RawDoc(
            url=url or f"https://news.example/{source_name}/{n}/{abs(hash(title)) % 10**6}",
            title=title, text=text, published_at=published or day(n) - timedelta(hours=2),
            source_name=publisher, watch=watch or []))

    def fetcher(self, n: int) -> Any:
        async def f(src: Source, block: Block) -> list[RawDoc]:
            return list(self.feeds.get((src.name or "", n), []))
        return f


def agent(pack: dict[str, Any]) -> dict[str, Any]:
    """A stand-in for the agent following prompts/daily.md with simple, explicit rules."""
    items = []
    for b in pack["blocks"]:
        for c in b["candidates"]:
            text = " ".join(i["title"] + " " + i["text"] for i in c["items"]).lower()
            rel = c["related"][0]["story"] if c["related"] else None
            if "27 октября" in text and "оферт" not in text:
                items.append({"candidate": c["id"], "decision": "stale", "related": rel})
            elif rel and "оферт" in text:
                items.append({"candidate": c["id"], "decision": "update", "related": rel, "headline": c["headline"],
                              "summary": "Компания объявила оферту.", "facts": ["оферта по П02-БО-01 назначена"]})
            elif rel:
                items.append({"candidate": c["id"], "decision": "repeat", "related": rel})
            else:
                items.append({"candidate": c["id"], "decision": "new", "headline": c["headline"],
                              "summary": "Сводка.", "facts": [c["headline"]], "event_date": c["published"][0]})
    return {"pack_id": pack["pack_id"], "items": items}


def run_day(st: Store, d: config.Digest, world: World, n: int) -> tuple[dict[str, Any], dict[str, Any]]:
    asyncio.run(collect(st, d, day(n), world.fetcher(n)))
    pack = daily.build_pack(st, d, day(n))
    res = daily.publish(st, d, daily.Decisions.model_validate(agent(pack)), day(n), deliver=False)
    return pack, res


def test_polyplast_downgrade_reported_once(home: Path) -> None:
    w = World()
    # day 0: the event, in two outlets plus the news search
    w.add("Интерфакс", 0, *DOWNGRADE)
    w.add("search", 0, "АКРА снизило рейтинг «Полипласта» до BBB-", DOWNGRADE[1][:200], publisher="РБК", watch=["Полипласт"])
    for n in range(1, 10):
        # every day: a reprint of the same text under another URL and a re-telling
        w.add("search", n, DOWNGRADE[0], DOWNGRADE[1], publisher=f"Агрегатор{n}", watch=["Полипласт"])
        w.add("search", n, *RETELL[n % len(RETELL)], publisher=f"Смартлаб{n}", watch=["Полипласт"])
    # day 4: an old article re-surfaces with an old publication date
    w.add("Интерфакс", 4, "Полипласт: итоги полугодия", "Полипласт опубликовал отчёт за полугодие, выручка выросла.",
          published=day(-12))
    # day 5: a fresh article about the old event (event date in the text)
    w.add("search", 5, "Аналитики обсуждают решение АКРА по Полипласту",
          "Решение АКРА от 27 октября о понижении рейтинга Полипласта по-прежнему обсуждают инвесторы.",
          publisher="Финам", watch=["Полипласт"])
    # day 7: a real development
    w.add("search", 7, "Полипласт объявил оферту по облигациям после понижения рейтинга",
          "Полипласт назначил оферту по выпуску П02-БО-01 на 20 ноября после понижения рейтинга АКРА.",
          publisher="Интерфакс", watch=["Полипласт"])
    # an off-watchlist item in the bonds block is dropped by code
    w.add("Интерфакс", 2, "Сбербанк увеличил прибыль", "Сбербанк отчитался о росте прибыли в третьем квартале.")

    d = config.load("test")
    shown_titles: list[str] = []
    with Store() as st:
        for n in range(10):
            pack, res = run_day(st, d, w, n)
            body = st.one("SELECT body FROM issues WHERE id=?", (res["issue_id"],))["body"]
            shown_titles += [ln for ln in body.splitlines() if ln.startswith("- **[")]
            if n in (1, 2, 3):
                # the re-telling must arrive with its memory: the known story and its facts
                bonds = next(b for b in pack["blocks"] if b["id"] == "bonds")
                assert bonds["candidates"], "re-telling should be a candidate"
                assert all(c["related"] for c in bonds["candidates"])
                assert "Значимых новостей нет" in body
        states = dict(st.q("SELECT state, COUNT(*) FROM items GROUP BY state"))
        reasons = [r["reason"] for r in st.q("SELECT reason FROM items WHERE state IN ('old','filtered')")]

    downgrade = [t for t in shown_titles if "рейтинг" in t.lower() and "оферт" not in t.lower()]
    assert len(downgrade) == 1, shown_titles
    assert any("оферт" in t.lower() for t in shown_titles), shown_titles
    assert len(shown_titles) == 2, shown_titles
    assert states.get("dup", 0) >= 8  # exact reprints never reach the agent
    assert any(r.startswith("published") for r in reasons)  # the 12-day-old article
    assert "no watchlist item mentioned" in reasons


def test_update_is_rendered_as_update(home: Path) -> None:
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    w.add("search", 1, "Полипласт объявил оферту по облигациям после понижения рейтинга",
          "Полипласт назначил оферту по выпуску П02-БО-01 после понижения рейтинга АКРА до BBB-(RU).",
          publisher="Интерфакс", watch=["Полипласт"])
    d = config.load("test")
    with Store() as st:
        run_day(st, d, w, 0)
        _, res = run_day(st, d, w, 1)
        body = st.one("SELECT body FROM issues WHERE id=?", (res["issue_id"],))["body"]
        story = st.one("SELECT * FROM stories WHERE status='reported'")
        events = st.q("SELECT kind FROM story_events WHERE story_id=? ORDER BY id", (story["id"],))
    assert "Обновление (впервые в дайджесте 27.10)" in body
    assert [e["kind"] for e in events] == ["new", "update"]
    assert "оферта" in story["facts"]


def test_unlisted_candidates_are_remembered(home: Path) -> None:
    w = World()
    w.add("ЦБ", 0, "ЦБ сохранил ключевую ставку", "Совет директоров Банка России сохранил ключевую ставку на уровне 16,5%.")
    d = config.load("test")
    with Store() as st:
        asyncio.run(collect(st, d, day(0), w.fetcher(0)))
        pack = daily.build_pack(st, d, day(0))
        assert sum(len(b["candidates"]) for b in pack["blocks"]) == 1
        daily.publish(st, d, daily.Decisions(pack_id=pack["pack_id"]), day(0), deliver=False)
        # the same story reprinted next day is a silent dup
        w.add("ЦБ", 1, "ЦБ сохранил ключевую ставку", "Совет директоров Банка России сохранил ключевую ставку на уровне 16,5%.",
              url="https://other.example/x")
        asyncio.run(collect(st, d, day(1), w.fetcher(1)))
        pack2 = daily.build_pack(st, d, day(1))
        assert sum(len(b["candidates"]) for b in pack2["blocks"]) == 0
        body = daily.publish(st, d, daily.Decisions(pack_id=pack2["pack_id"]), day(1), deliver=False)
        assert body["shown"] == 0


def test_abandoned_pack_items_come_back_until_they_age_out(home: Path) -> None:
    w = World()
    w.add("ЦБ", 0, "ЦБ опубликовал проект положения", "Банк России опубликовал проект положения о требованиях к капиталу банков.")
    d = config.load("test")
    with Store() as st:
        asyncio.run(collect(st, d, day(0), w.fetcher(0)))
        p1 = daily.build_pack(st, d, day(0))
        assert sum(len(b["candidates"]) for b in p1["blocks"]) == 1
        # nobody published; next run 12h later still offers it, 2 days later it is too old
        p2 = daily.build_pack(st, d, day(0) + timedelta(hours=12))
        assert sum(len(b["candidates"]) for b in p2["blocks"]) == 1
        p3 = daily.build_pack(st, d, day(2))
        assert sum(len(b["candidates"]) for b in p3["blocks"]) == 0
        assert st.one("SELECT status FROM packs WHERE id=?", (p1["pack_id"],))["status"] == "abandoned"


def test_cap_per_block_and_publish_guards(home: Path) -> None:
    w = World()
    for k in range(5):
        w.add("search", 0, f"РЖД разместила выпуск облигаций серии {k}0{k}", f"РЖД разместила облигации серии {k}0{k} объёмом {k + 1}0 млрд руб.",
              publisher=f"P{k}", watch=["РЖД"])
    d = config.load("test")
    with Store() as st:
        asyncio.run(collect(st, d, day(0), w.fetcher(0)))
        pack = daily.build_pack(st, d, day(0))
        cands = pack["blocks"][0]["candidates"]
        dec = {"pack_id": pack["pack_id"], "items": [
            {"candidate": c["id"], "decision": "new", "headline": c["headline"], "importance": 3 if i == 4 else 1}
            for i, c in enumerate(cands)] + [{"candidate": "c999", "decision": "new", "headline": "x"}]}
        res = daily.publish(st, d, daily.Decisions.model_validate(dec), day(0), deliver=False)
        assert res["shown"] == 3  # max_items
        assert any("c999" in x for x in res["warnings"])
        try:
            daily.publish(st, d, daily.Decisions.model_validate(dec), day(0), deliver=False)
            raise AssertionError("second publish of the same pack must fail")
        except daily.PublishError:
            pass
