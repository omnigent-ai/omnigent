# Faster session content search on large PostgreSQL deployments

Session search (`GET /v1/sessions?search_query=`, the sidebar **Search**
palette) matches a session when its title or any conversation item's text
contains the query. The content half is an unanchored
`conversation_items.search_text ILIKE '%term%'`. PostgreSQL has no index that
can serve that predicate by default, so a term that matches few or no items
reads every row before the listing can answer. Around a million items that
takes longer than the web client waits (10 s) and than the server's search
`statement_timeout` (15 s); the palette then shows an error instead of
"No results found".

Two opt-in steps make rare and absent terms answer in milliseconds. Both are
PostgreSQL-only; SQLite and CockroachDB deployments are unaffected.

## 1. Build the trigram index

```sh
omnigent debug db-build-search-index postgresql://<user>:<password>@host/dbname
```

The command enables the `pg_trgm` extension and runs
`CREATE INDEX CONCURRENTLY ix_conversation_items_search_text_gin_trgm ON
conversation_items USING gin (search_text gin_trgm_ops)`. It is deliberately
not part of the schema migrations: building the index over gigabytes of text
takes minutes, the finished index is a sizeable fraction of the text, the build
needs several times that in temporary sort space, and `CONCURRENTLY` lets
writers continue while it runs. Re-running the command is safe; it reports
whether the index already existed and replaces an invalid leftover from an
interrupted build.

Remove the index again with `--drop`:

```sh
omnigent debug db-build-search-index --drop postgresql://<user>:<password>@host/dbname
```

## 2. Enable the fast path

Set `OMNIGENT_PG_CONTENT_SEARCH=auto` in the server environment. The default,
`off`, keeps the existing query even when the index exists. With `auto`, the
server checks the catalog for a valid index (re-checked about once a minute) and
uses it when present; a missing index falls back to the legacy scan.

## How the fast path behaves

For a query with at least three consecutive ASCII letters or digits, the server
first fetches the matching `(conversation_id, position)` rows through the index,
capped at 1,000 rows:

- **Absent or rare terms** complete the probe. The listing filters on the
  returned session ids instead of scanning items, and result snippets are read
  from the exact matching positions, so no further `ILIKE` runs.
- **Common terms** overflow the cap and use the legacy correlated query, which
  fills its page from the first matching items of each session. Their latency is
  unchanged.

Shorter queries (for example `ab` or `a-b`) yield no index trigram and always
use the legacy query. Result sets, ordering, cursors, permissions and snippets
are the same on both paths.
