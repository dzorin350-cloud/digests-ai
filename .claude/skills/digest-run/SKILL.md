---
name: digest-run
description: Run a personal news digest from this repository (daily issue, and weekly/monthly review when due), including reading feedback replies and delivering. Use for scheduled tasks and for "run my digest".
---

1. `.venv/bin/digest -h` must work (if not: `uv venv && uv pip install -e .`). Digest ids: `ls digests/`.
2. Run `.venv/bin/digest prompt run` and follow it exactly; it calls `digest prompt daily` / `review`.
3. Report in 1–2 lines what was published per block and anything that failed. Do not ask questions during a scheduled run.
