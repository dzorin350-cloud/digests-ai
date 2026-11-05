"""Memory survives runs that start from a clean copy: state pull / push through a (private) git repository."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import CONFIG
from test_novelty import DOWNGRADE, World, day, run_day

from digestkit import cli, config, daily, state
from digestkit.collect import collect
from digestkit.store import Store


def _container(tmp: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh copy of the tool folder: no state/, no digests/."""
    home = tmp / name
    (home / "digests").mkdir(parents=True)
    monkeypatch.setenv("DIGEST_HOME", str(home))
    return home


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bare = tmp_path / "private-state.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    monkeypatch.setenv("DIGEST_STATE_GIT", str(bare))
    for k in ("DIGEST_DB", "EMBED_BASE_URL", "EMBED_MODEL", "DIGEST_STATE_PASSPHRASE"):
        monkeypatch.delenv(k, raising=False)
    return bare


def _world() -> World:
    w = World()
    w.add("search", 0, *DOWNGRADE, publisher="РБК", watch=["Полипласт"])
    w.add("search", 1, *DOWNGRADE, url="https://other.example/reprint", publisher="Агрегатор", watch=["Полипласт"])
    return w


def test_two_clean_runs_share_memory(tmp_path: Path, remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _world()
    # run 1: a clean container, the user's config exists only here
    h1 = _container(tmp_path, "run1", monkeypatch)
    (h1 / "digests" / "test.yaml").write_text(CONFIG, encoding="utf-8")
    assert state.pull()["status"].startswith("no remote state")
    with Store() as st:
        _, res = run_day(st, config.load("test"), w, 0)
        assert res["shown"] == 1
    pushed = state.push()
    assert pushed["issues"] == 1

    # run 2: another clean container: no memory, no config until pulled
    h2 = _container(tmp_path, "run2", monkeypatch)
    with pytest.raises(ValueError, match="state pull"):
        cli._cold_guard(type("A", (), {"allow_cold": False})())
    got = state.pull()
    assert got["status"] == "restored" and "test.yaml" in got["configs_restored"]
    assert (h2 / "digests" / "test.yaml").read_text(encoding="utf-8") == CONFIG
    with Store() as st:
        asyncio.run(collect(st, config.load("test"), day(1), w.fetcher(1)))
        pack = daily.build_pack(st, config.load("test"), day(1))
        # the reprint from day 1 is recognised thanks to the restored memory
        assert sum(len(b["candidates"]) for b in pack["blocks"]) == 0
        daily.publish(st, config.load("test"), daily.Decisions(pack_id=pack["pack_id"]), day(1), deliver=False)
    state.push()

    # run 1's stale copy must not overwrite newer remote memory silently
    monkeypatch.setenv("DIGEST_HOME", str(h1))
    res = state.pull()
    assert res["status"] == "restored"
    with Store() as st:
        assert st.one("SELECT COUNT(*) AS n FROM issues")["n"] == 2
    assert (h1 / "state" / "digest.db.bak").exists()


def test_local_newer_is_kept(tmp_path: Path, remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _world()
    _container(tmp_path, "a", monkeypatch)
    (tmp_path / "a" / "digests" / "test.yaml").write_text(CONFIG, encoding="utf-8")
    with Store() as st:
        run_day(st, config.load("test"), w, 0)
    state.push()
    with Store() as st:  # local moves on without pushing
        run_day(st, config.load("test"), w, 1)
    res = state.pull()
    assert res["status"].startswith("local memory is newer")
    assert state.pull(force=True)["status"] == "restored"


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_encrypted_state(tmp_path: Path, remote: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_STATE_PASSPHRASE", "correct horse battery staple")
    _container(tmp_path, "a", monkeypatch)
    (tmp_path / "a" / "digests" / "test.yaml").write_text(CONFIG, encoding="utf-8")
    with Store() as st:
        run_day(st, config.load("test"), _world(), 0)
    assert state.push()["encrypted"] is True
    # what lands in the repository is not readable without the passphrase
    show = subprocess.run(["git", "--git-dir", str(remote), "show", "digest-state:digest-state.tar.gz.enc"],
                          capture_output=True, check=True).stdout
    assert "Полипласт".encode() not in show and b"SQLite" not in show and b"test.yaml" not in show
    _container(tmp_path, "b", monkeypatch)
    monkeypatch.setenv("DIGEST_STATE_PASSPHRASE", "wrong")
    with pytest.raises(state.StateError, match="PASSPHRASE"):
        state.pull()
    monkeypatch.setenv("DIGEST_STATE_PASSPHRASE", "correct horse battery staple")
    assert state.pull()["status"] == "restored"


def test_export_import_files(home: Path) -> None:
    with Store() as st:
        run_day(st, config.load("test"), _world(), 0)
    f = home / "snap.tar.gz"
    assert cli.main(["state", "export", "--file", str(f)]) == 0
    (home / "state" / "digest.db").unlink()
    assert cli.main(["state", "import", "--file", str(f)]) == 0
    with Store() as st:
        assert st.one("SELECT COUNT(*) AS n FROM stories")["n"] >= 1
