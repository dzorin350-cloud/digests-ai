---
name: digest-tune
description: Adjust a news digest to the reader — fold feedback into block briefs, fix noisy or empty blocks, add or remove sources. Use after a weekly review or when the user says the digest is noisy, repetitive or missing something.
---

1. If the user gave feedback in words, record it first: `.venv/bin/digest feedback <id> "<text>"`.
2. Run `.venv/bin/digest prompt tune` and follow it.
3. Tell the user in a few lines what you changed (it is also logged in `digests/<id>.changelog.md`).
