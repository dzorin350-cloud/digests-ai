---
name: digest-new
description: Create a new personal news digest (or add a block to one) when the user asks for a digest, newsletter or news briefing on some topics, companies, bonds or products. Builds digests/<id>.yaml, finds and checks sources, makes the first issue and sets up a schedule.
---

1. Make sure the CLI works: `.venv/bin/digest -h` (if missing: `uv venv && uv pip install -e .`).
2. Run `.venv/bin/digest prompt new` and follow it step by step.
3. Finish with: the config path, blocks and sources chosen, the first issue path, and the schedule set up.
