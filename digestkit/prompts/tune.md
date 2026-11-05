# Weekly maintenance: keep the digest fitting the reader

Run after the weekly review (or when the user asks "the digest is noisy / missing X"). Make the changes
yourself; the user should not have to administer anything. Small, reversible steps.

1. Look: `digest status <id> --json` (reader notes, ratings, automatic changes),
   `digest sources list <id>` (items vs shown per source, errors), and the last daily issues
   (`out/<id>/`).
2. Fold feedback into the config (`digests/<id>.yaml`):
   - Repeated notes like "less about X" → add to the block `brief` (as "not needed: X") or to `exclude`
     if it is a clear keyword. "More about Y" → `brief`/`include`, and a source or a `news_search` query for Y.
   - Many disliked items in a block → tighten its `brief`; many liked → keep and note what works.
   - A block empty for 7+ days while the reader expects news → add sources; a block always full of minor
     items → lower `max_items` or sharpen the brief.
   - New names the reader mentions (a new bond, a competitor) → `watchlist`.
3. Sources: the system already mutes useless and broken ones (see `changes`). Remove muted/broken sources
   from the YAML if they stay that way for two weeks. `digest sources suggest <id>` lists publishers that keep
   appearing in selected news-search results: add the good ones (`digest sources add`). Look for 1–2 new
   sources per weak block with web search; they start as `trial` and are removed automatically if useless.
4. Validate: `digest validate <id>`, `digest sources check <id> --block <changed block>`.
5. Log every change in one line in `digests/<id>.changelog.md` (date, what, why). Commit if the folder is a
   git repository and the user keeps it in git.
