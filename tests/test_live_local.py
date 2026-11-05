"""Real HTTP path (adapters, fulltext, auto-detect) against a local server, and standalone mode with a fake LLM."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from digestkit import cli, config, daily, llm
from digestkit.collect import collect
from digestkit.collect.adapters import detect
from digestkit.collect.http import Client
from digestkit.store import Store

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
ARTICLE = "<html><head><title>{t}</title><meta property='article:published_time' content='2026-11-05T05:00:00+00:00'></head>" \
          "<body><article><h1>{t}</h1><p>{b}</p><p>Подробности: {b}</p></article></body></html>"
PAGES = {
    "/feed": ("application/rss+xml", """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>ЦБ сохранил ключевую ставку</title><link>{base}/a1</link><pubDate>Thu, 05 Nov 2026 05:00:00 GMT</pubDate>
<description>Коротко</description></item>
<item><title>Старая новость</title><link>{base}/a0</link><pubDate>Mon, 19 Oct 2026 05:00:00 GMT</pubDate></item>
</channel></rss>"""),
    "/a1": ("text/html", ARTICLE.format(t="ЦБ сохранил ключевую ставку", b="Совет директоров Банка России сохранил ключевую ставку на уровне 16,5% годовых. " * 4)),
    "/blog/": ("text/html", "<html><head><link rel='alternate' type='application/rss+xml' href='/feed'></head><body>blog</body></html>"),
}


class H(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        base = f"http://{self.headers['Host']}"
        if self.path not in PAGES:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = PAGES[self.path]
        data = body.replace("{base}", base).encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a: Any) -> None:
        pass


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("DIGEST_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _cfg(home: Path, base: str) -> None:
    (home / "digests" / "live.yaml").write_text(f"""
title: Live
blocks:
  - id: macro
    title: Экономика
    brief: решения ЦБ
    sources:
      - {{url: "{base}/blog/", name: Тестовый блог}}
""", encoding="utf-8")


def test_fetch_detect_and_fulltext(home: Path, server: str) -> None:
    _cfg(home, server)
    d = config.load("live")

    async def go() -> dict[str, Any]:
        async with Client() as c:
            return await detect(server + "/blog/", c)

    info = asyncio.run(go())
    assert info["kind"] == "rss" and info["feed_url"].endswith("/feed")
    with Store() as st:
        res = asyncio.run(collect(st, d, NOW))
        assert res["errors"] == [] and res["items"] == {"new": 1, "old": 1}
        text = st.one("SELECT text FROM items WHERE state='new'")["text"]
        assert "16,5%" in text and len(text) > 200  # full text, not the feed teaser


def test_standalone_run(home: Path, server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _cfg(home, server)
    calls = []

    def fake_chat(system: str, user: str, json_mode: bool, max_tokens: int = 6000) -> str:
        calls.append(json_mode)
        if json_mode:
            assert "stale" in system and '"candidates"' in user
            return '```json\n{"lead": "Ставка без изменений.", "items": [{"candidate": "c1", "decision": "new", ' \
                   '"headline": "ЦБ сохранил ставку 16,5%", "summary": "Без сюрпризов.", "facts": ["16,5%"], "importance": 3}]}\n```'
        return "# Обзор недели\n\nСтавка сохранена."

    monkeypatch.setattr(llm, "chat", fake_chat)
    assert cli.main(["run", "live", "--cadence", "daily", "--no-deliver", "--now", NOW.isoformat()]) == 0
    md = next((home / "out" / "live").glob("*-daily.md")).read_text(encoding="utf-8")
    assert "**[M1] ЦБ сохранил ставку 16,5%**" in md and "Ставка без изменений." in md
    assert cli.main(["run", "live", "--cadence", "weekly", "--no-deliver", "--now", NOW.isoformat()]) == 0
    assert (home / "out" / "live" / "2026-11-05-weekly.md").read_text(encoding="utf-8").startswith("# Обзор недели")
    assert calls == [True, False]
    # a second daily run the same day has nothing new
    with Store() as st:
        pack = daily.build_pack(st, config.load("live"), NOW)
        assert sum(len(b["candidates"]) for b in pack["blocks"]) == 0
