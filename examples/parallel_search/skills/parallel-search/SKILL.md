---
name: parallel-search
description: Procedure for answering questions with cited, cross-checked web research using Parallel Search MCP.
---

# parallel-search — cited, cross-checked web research

Use this procedure for questions that need current, verifiable information
from the web. The deliverable is a concise synthesis where every load-bearing
claim is backed by a source you actually read.

## Tools

- `web_search(query)` — find candidate pages for a focused question.
- `web_fetch(url)` — read clean Markdown from a specific URL.

## Procedure

1. **Plan.** Break the question into 3-6 focused sub-queries. For contested or
   high-stakes topics, include at least two independent angles.
2. **Search.** Use `web_search` for each sub-query. Prefer primary sources and
   use recent or date-specific searches for time-sensitive claims.
3. **Read.** Use `web_fetch` on the most promising pages. Do not cite a result
   snippet without reading the page behind it.
4. **Cross-check.** Verify each load-bearing claim against at least two
   independent sources, and surface meaningful disagreements.
5. **Synthesize.** Separate well-supported findings from uncertainty, cite the
   URLs you fetched inline, and finish with a Sources list of those URLs.

If coverage is thin or sources conflict, say so plainly instead of filling the
gaps from prior knowledge.
