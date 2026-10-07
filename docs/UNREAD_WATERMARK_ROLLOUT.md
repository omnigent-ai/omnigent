# Unread message watermark

`conversations.last_message_at` distinguishes visible messages from metadata
updates. New conversations write `0` for a known-empty transcript; visible,
non-meta message appends advance it, while metadata and hidden messages leave it
unchanged. Existing rows may remain `NULL`, which means legacy or unknown and
keeps the `updated_at` unread fallback. REST may omit the nullable field and
WebSocket rows may send `null`.

There is no backfill job or reader feature gate. Full forks may carry the source
watermark; truncated forks leave it `NULL`. The fallback covers rows that remain
unknown, but it cannot detect an old writer appending after a numeric watermark
exists. Deploy the schema first and drain old writers before serving this
application version.
