# Creating a new digest from a request like "I want a digest about X"

Goal: a working `digests/<id>.yaml` with good blocks and sources, a first issue, and a schedule. Do it
yourself; ask the user at most one short round of questions, only if the topic is truly ambiguous
(e.g. "my stocks" without a list). Otherwise pick sensible defaults and tell the user what you chose.

1. **Shape it.** From the request decide: `reader` (who and why), language, 1–5 blocks. A block is a topic the
   reader thinks of separately ("Economy", "My bonds", "AI agents"). For each block write a `brief` like a task
   for a human researcher: what matters, what does not (be explicit about noise to drop: quotes, ads,
   opinions, minor price moves). Things the reader holds or follows (tickers, bonds, companies, products,
   people) go to `watchlist` with `aliases` (other spellings, Latin/Cyrillic) and a search `query`; set
   `require_watch_match: true` for blocks that are only about the watchlist.
2. **Start from a template:** `digest templates`, then `digest init <id> --template <closest> --title "..."`
   and edit the file. Keep comments short. Ids are lowercase latin; `ref` is one capital letter per block.
3. **Find sources** (3–10 per block). Prefer primary and fast sources: regulators' and companies' official
   news pages, agencies with RSS, specialised outlets, active Telegram channels with real news. Use web
   search. For each candidate run `digest sources add <id> <url> --block <block>` (it finds the RSS or the
   article links by itself) or put it into the YAML directly. A `news_search` source with `query: "{watch}"`
   covers watchlists; add 1–2 topical `news_search` queries per general block as a safety net.
4. **Check:** `digest sources check <id>`. Remove or fix sources that FAIL or return 0 items; prefer an RSS
   feed or a `selectors.link_pattern` for HTML pages.
5. **First run:** follow `digest prompt run` (daily part). The first issue may contain a few days of news;
   that is expected. Show the user the result and adjust briefs if the selection is off.
6. **Schedule** (tell the user which one you set up):
   - Claude Cowork: a scheduled task, daily at the reader's morning time, prompt:
     "In the ai-digests folder, run the digest `<id>` following `digest prompt run`."
   - Claude Code: `/schedule` (or a cron job running `claude -p "..."` with the same prompt).
   - Codex / Kimi Code / other agents: their scheduler or cron with the same prompt.
   - No agent: `digest run <id>` from cron with `LLM_*` set in `.env` (standalone mode).
7. **Memory between runs.** If the scheduled runs start from a clean copy (cloud), the user needs a private git
   repository for state: set `DIGEST_STATE_GIT` (and optionally `DIGEST_STATE_PASSPHRASE`) in the environment
   and run `digest state push` once now. Never commit `digests/` to the tool repository: it is personal.
8. Write a short `digests/<id>.changelog.md` entry: what you created and why.
