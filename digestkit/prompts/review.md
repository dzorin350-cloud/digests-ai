# Weekly / monthly review: how to write it

You receive a **review pack** (JSON) built by `digest review`. There is no new collection: this is a look back
over the digest's memory for the `period`. The daily issues told the reader *what happened*; the review
tells them *how things developed and what it adds up to*.

## Inputs

- `blocks[].threads[]`: a thread is one story and its continuations over time. `timeline` has dated events
  (`new`, `update`) with facts; `in_period: false` events are earlier context. `stories[].rating` is reader
  feedback (positive = liked).
- `blocks[].skipped_counts`: how much was filtered out (stale / repeat / irrelevant / not selected).
- `blocks[].reader_notes`: feedback text from the period.
- `system_changes`: what the system changed on its own (sources added, muted, broken).
- `earlier_reviews`: for a monthly review, the weekly reviews of the month; for a weekly, the previous one.
- `reader`, `style`, `language`.

## What to write (Markdown, in the pack `language`)

0. First line: `<!-- subject: … -->`, the e-mail subject: a hook with the period's main development,
   ≤ 90 characters ("Неделя: Полипласт ушёл в оферту, ставка без изменений").
1. `# {title}: {weekly|monthly} review, {period.from} – {period.to}`.
2. **Main points**: 3–5 bullets, the most important developments across all blocks, each one line.
3. One section per block (`## {block title}`):
   - Threads that moved: for each, 2–4 sentences on how it developed in the period (start → now), with
     dates and numbers from the timeline, and what to watch next if the facts suggest it. Group small
     related threads together. Do not retell every daily item.
   - For watchlist blocks: one line per watched name, including the ones without news ("Брусника — без
     событий").
   - Where the timeline has attributed expert views, you may summarise them in one sentence, with names.
   - If a block had nothing material, say so in one line.
4. Monthly only: **Trends**: what changed over the month compared with the weekly reviews; recurring themes;
   what turned out to matter and what fizzled out.
5. **Digest housekeeping** (short, last): notable `system_changes`, how much noise was filtered out, and how
   the reader's feedback was taken into account. Skip if nothing to say.

Rules: only facts present in the pack; no invented forecasts (you may point at scheduled dates mentioned in
the facts); concise: a weekly review fits on one screen per block, a monthly one on two; no item refs like
[E1] (they belong to daily issues).

Save the Markdown to a file and run `digest publish-review <digest> --pack <pack_id> --file <file>`.
