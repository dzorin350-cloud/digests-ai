from __future__ import annotations

from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"

CONFIG = """
title: Тестовый дайджест
language: ru
timezone: Europe/Moscow
cadences:
  daily: {enabled: true, max_age_hours: 36}
  weekly: {enabled: true, weekday: 6}
  monthly: {enabled: true, day: 1}
blocks:
  - id: bonds
    title: Мои облигации
    ref: B
    brief: события по эмитентам из списка
    require_watch_match: true
    max_items: 3
    watchlist:
      - {name: Полипласт, aliases: [Polyplast]}
      - {name: РЖД}
    sources:
      - {kind: news_search, query: "{watch}", name: search}
      - {kind: rss, url: "https://example.com", feed_url: "https://example.com/feed", name: Интерфакс}
  - id: macro
    title: Экономика
    ref: E
    brief: решения ЦБ
    exclude: [гороскоп]
    sources:
      - {kind: rss, url: "https://cbr.example", feed_url: "https://cbr.example/rss", name: ЦБ}
"""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DIGEST_HOME", str(tmp_path))
    for k in ("EMBED_BASE_URL", "EMBED_MODEL", "DIGEST_DB", "SMTP_HOST", "TELEGRAM_BOT_TOKEN", "IMAP_HOST"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "digests").mkdir()
    (tmp_path / "digests" / "test.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path
