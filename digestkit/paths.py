"""Where things live. Everything is relative to DIGEST_HOME (default: current directory)."""

from __future__ import annotations

import os
from pathlib import Path

PKG = Path(__file__).resolve().parent


def home() -> Path:
    return Path(os.environ.get("DIGEST_HOME") or Path.cwd()).resolve()


def state_dir() -> Path:
    p = home() / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    env = os.environ.get("DIGEST_DB")
    return Path(env).resolve() if env else state_dir() / "digest.db"


def packs_dir(digest_id: str) -> Path:
    p = state_dir() / "packs" / digest_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir(digest_id: str) -> Path:
    p = home() / "out" / digest_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def digests_dir() -> Path:
    return home() / "digests"


def prompt_path(name: str) -> Path:
    return PKG / "prompts" / f"{name}.md"


def template_path(name: str) -> Path:
    return PKG / "templates" / f"{name}.yaml"


def load_dotenv() -> None:
    """Minimal .env loader (no dependency): KEY=VALUE lines, existing env wins."""
    f = home() / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
