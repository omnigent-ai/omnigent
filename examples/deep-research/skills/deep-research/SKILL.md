---
name: deep-research
description: Procedure for answering a question with a thorough, cross-checked, cited web-research report using the Keenable search + fetch tools.
---

# deep-research — cited, cross-checked web research

Use this for any question that needs current, verifiable information from the
web. The deliverable is a synthesized answer where every load-bearing claim is
backed by a source you actually read.

## Tools
- `search_web_pages(query, [site], [published_after], [published_before], [mode])`
  — discover candidate sources. Write the `query` as a natural-language
  description of the ideal page, not keywords. Use `mode: pro` for quality,
  `realtime` when latency matters.
- `fetch_page_content(url, [max_chars])` — read the full page (markdown). A
  search snippet is NEVER sufficient evidence — fetch before you cite.

## Procedure
1. **Plan.** Break the question into 3-6 focused sub-queries that together
   cover it. For contested or high-stakes questions, plan at least two
   independent angles.
2. **Search.** Run `search_web_pages` per sub-query. Prefer primary sources;
   use `published_after` for anything time-sensitive.
3. **Read.** `fetch_page_content` on the 2-3 most promising results per
   sub-query. Quote/cite only what you read, not what a snippet implied.
4. **Cross-check.** Verify every load-bearing claim against ≥2 INDEPENDENT
   sources (independent = different owners, not mirrors of one another). When
   sources disagree, surface the disagreement rather than picking silently.
5. **Synthesize.** Write a structured answer. Each non-obvious claim gets an
   inline citation to the URL you fetched. Separate "well-supported" from
   "uncertain / single-source".
6. **Cite.** End with a `Sources` list of the URLs you actually fetched.

## Notes
- Don't answer from prior knowledge with a disclaimer — search and read first.
- If coverage is thin or sources conflict irreconcilably, say so explicitly;
  an honest "the evidence is mixed" beats false confidence.
- Keep each sub-query narrow enough that a couple of fetches resolve it.
