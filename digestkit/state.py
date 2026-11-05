"""Persistent memory for runs that start from a clean copy (cloud agents, scheduled tasks, CI).

A snapshot = the SQLite memory + your digest configs (`digests/*`), packed into one archive and, if
DIGEST_STATE_PASSPHRASE is set, encrypted (AES-256 via the `openssl` CLI). It is kept on a branch of a git
repository (DIGEST_STATE_GIT, branch DIGEST_STATE_BRANCH, default `digest-state`) — use a PRIVATE repository,
or set the passphrase. Your configs then never need to live in the public tool repository.

    digest state pull   # at the start of a run: restore memory + configs
    digest state push   # at the end: save them
"""

from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from digestkit import paths
from digestkit.store import iso, now_utc

SNAPSHOT = "digest-state.tar.gz"
MARKER = ".pulled"


class StateError(RuntimeError):
    pass


# ---------------- archive ----------------
def _backup_db(dst: Path) -> None:
    src = paths.db_path()
    if not src.exists():
        raise StateError(f"no memory yet at {src}")
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)  # consistent copy even while WAL is in use


def export(dst: Path) -> dict[str, Any]:
    """Write the snapshot archive (plain .tar.gz) to dst."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "digest.db"
        _backup_db(db)
        with sqlite3.connect(db) as c:
            issues = c.execute("SELECT COUNT(*), MAX(created_at) FROM issues").fetchone()
        meta = {"exported_at": iso(now_utc()), "host": socket.gethostname(), "issues": issues[0],
                "last_issue_at": issues[1], "format": 1}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(db, arcname="digest.db")
            ddir = paths.digests_dir()
            if ddir.exists():
                for f in sorted(ddir.rglob("*")):
                    if f.is_file():
                        tar.add(f, arcname=f"digests/{f.relative_to(ddir).as_posix()}")
            data = json.dumps(meta, ensure_ascii=False).encode()
            info = tarfile.TarInfo("meta.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        dst.write_bytes(gzip.compress(buf.getvalue(), 6))
    return meta


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    ok = []
    for m in tar.getmembers():
        p = Path(m.name)
        if m.isfile() and not p.is_absolute() and ".." not in p.parts and (m.name in ("digest.db", "meta.json")
                                                                           or m.name.startswith("digests/")):
            ok.append(m)
    return ok


def read_meta(src: Path) -> dict[str, Any]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(src.read_bytes())), mode="r") as tar:
        f = tar.extractfile("meta.json")
        return json.loads(f.read()) if f else {}


def restore(src: Path, configs: bool = True) -> dict[str, Any]:
    """Replace the local memory (the old file is kept as .bak) and restore digests/ files."""
    raw = gzip.decompress(src.read_bytes())
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        members = _safe_members(tar)
        for m in members:  # members are checked above: plain files under known names only
            try:
                tar.extract(m, tmp, filter="data")
            except TypeError:  # Python < 3.11.4 has no extraction filters
                tar.extract(m, tmp)
        db = Path(tmp) / "digest.db"
        if not db.exists():
            raise StateError("snapshot has no digest.db")
        with sqlite3.connect(db) as c:  # sanity check before touching anything
            c.execute("SELECT COUNT(*) FROM issues").fetchone()
        target = paths.db_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copy2(target, target.with_suffix(".db.bak"))
            for ext in ("-wal", "-shm"):
                Path(str(target) + ext).unlink(missing_ok=True)
        shutil.copy2(db, target)
        restored = []
        if configs and (Path(tmp) / "digests").exists():
            for f in (Path(tmp) / "digests").rglob("*"):
                if f.is_file():
                    dst = paths.digests_dir() / f.relative_to(Path(tmp) / "digests")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dst)
                    restored.append(dst.name)
        meta = json.loads((Path(tmp) / "meta.json").read_text(encoding="utf-8")) if (Path(tmp) / "meta.json").exists() else {}
    meta["configs_restored"] = restored
    return meta


# ---------------- encryption (optional) ----------------
def _crypt(src: Path, dst: Path, decrypt: bool) -> None:
    if not shutil.which("openssl"):
        raise StateError("DIGEST_STATE_PASSPHRASE is set but `openssl` is not installed")
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "env:DIGEST_STATE_PASSPHRASE",
           "-in", str(src), "-out", str(dst)]
    if decrypt:
        cmd.insert(2, "-d")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise StateError("decryption failed (wrong DIGEST_STATE_PASSPHRASE?)" if decrypt else f"encryption failed: {r.stderr[:200]}")


# ---------------- git transport ----------------
def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise StateError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def _remote() -> tuple[str, str]:
    url = os.environ.get("DIGEST_STATE_GIT", "")
    if not url:
        raise StateError("set DIGEST_STATE_GIT (a private git repository URL or path) in .env")
    return url, os.environ.get("DIGEST_STATE_BRANCH", "digest-state")


def _workdir(url: str, branch: str) -> tuple[Path, bool]:
    """A small clone of only the state branch. Returns (dir, branch_exists)."""
    wd = paths.state_dir() / ".sync"
    if wd.exists():
        shutil.rmtree(wd)
    wd.mkdir(parents=True)
    _git(["init", "-q"], wd)
    _git(["remote", "add", "origin", url], wd)
    exists = _git(["fetch", "-q", "--depth", "1", "origin", branch], wd, check=False).returncode == 0
    if exists:
        _git(["checkout", "-q", "-B", branch, "FETCH_HEAD"], wd)
    else:
        _git(["checkout", "-q", "--orphan", branch], wd)
    return wd, exists


def _encrypted() -> bool:
    return bool(os.environ.get("DIGEST_STATE_PASSPHRASE"))


def push() -> dict[str, Any]:
    url, branch = _remote()
    wd, _ = _workdir(url, branch)
    plain = wd / ".snapshot.tmp"
    meta = export(plain)
    name = SNAPSHOT + (".enc" if _encrypted() else "")
    for old in (SNAPSHOT, SNAPSHOT + ".enc"):
        (wd / old).unlink(missing_ok=True)
    if _encrypted():
        _crypt(plain, wd / name, decrypt=False)
        plain.unlink()
    else:
        plain.rename(wd / name)
    (wd / "meta.json").write_text(json.dumps({**meta, "encrypted": _encrypted()}, ensure_ascii=False, indent=1), encoding="utf-8")
    _git(["add", "-A"], wd)
    _git(["-c", "user.name=ai-digests", "-c", "user.email=ai-digests@localhost", "commit", "-q", "-m",
          f"state {meta['exported_at']} ({meta['issues']} issues)"], wd)
    _git(["push", "-q", "origin", f"HEAD:refs/heads/{branch}"], wd)
    shutil.rmtree(wd, ignore_errors=True)
    mark_pulled()
    return {**meta, "branch": branch, "encrypted": _encrypted()}


def pull(force: bool = False) -> dict[str, Any]:
    url, branch = _remote()
    wd, exists = _workdir(url, branch)
    try:
        if not exists:
            mark_pulled()
            return {"status": "no remote state yet: starting with empty memory", "branch": branch}
        enc, plain = wd / (SNAPSHOT + ".enc"), wd / SNAPSHOT
        if enc.exists():
            if not _encrypted():
                raise StateError("remote state is encrypted: set DIGEST_STATE_PASSPHRASE")
            _crypt(enc, wd / ".plain.tmp", decrypt=True)
            plain = wd / ".plain.tmp"
        if not plain.exists():
            raise StateError(f"branch {branch} has no {SNAPSHOT}")
        remote_meta = read_meta(plain)
        local_last = _local_last_issue()
        if not force and local_last and (remote_meta.get("last_issue_at") or "") < local_last:
            mark_pulled()
            return {"status": "local memory is newer than the remote one: kept local (use --force to overwrite)",
                    "local_last_issue": local_last, "remote_last_issue": remote_meta.get("last_issue_at")}
        meta = restore(plain)
        mark_pulled()
        return {"status": "restored", **meta}
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def _local_last_issue() -> str | None:
    p = paths.db_path()
    if not p.exists():
        return None
    try:
        with sqlite3.connect(p) as c:
            row = c.execute("SELECT MAX(created_at) FROM issues").fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def mark_pulled() -> None:
    (paths.state_dir() / MARKER).write_text(iso(now_utc()) or "", encoding="utf-8")


def cold_start_risk() -> str | None:
    """Remote state is configured but this copy never pulled it: running now would treat everything as new."""
    if os.environ.get("DIGEST_STATE_GIT") and not (paths.state_dir() / MARKER).exists():
        return ("DIGEST_STATE_GIT is set but `digest state pull` has not run in this copy; without it every "
                "story looks new. Run `digest state pull` first (or pass --allow-cold).")
    return None


def age_hint(meta: dict[str, Any]) -> str:
    ts = meta.get("exported_at")
    if not ts:
        return ""
    return f"snapshot from {datetime.fromisoformat(ts).isoformat(timespec='minutes')}"
