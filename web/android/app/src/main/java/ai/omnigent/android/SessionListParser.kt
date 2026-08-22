package ai.omnigent.android

import org.json.JSONObject

/**
 * Parses a `GET /v1/sessions` response body into [SessionState]s. Pure (no
 * Android/HTTP), so the parse is unit-testable. Tolerant of missing/extra
 * fields: only `id` and `status` are required per item; anything malformed is
 * skipped rather than throwing, so one bad row can't sink a whole poll.
 *
 * Response shape (see the server's `SessionList`):
 * `{ "object": "list", "data": [ SessionListItem, ... ], ... }`.
 */
fun parseSessionList(body: String): List<SessionState> {
    val root =
        try {
            JSONObject(body)
        } catch (_: Throwable) {
            return emptyList()
        }
    val data = root.optJSONArray("data") ?: return emptyList()
    val out = ArrayList<SessionState>(data.length())
    for (i in 0 until data.length()) {
        val item = data.optJSONObject(i) ?: continue
        val id = item.optString("id").ifEmpty { null } ?: continue
        val status = item.optString("status").ifEmpty { null } ?: continue
        out.add(
            SessionState(
                id = id,
                status = status,
                pendingElicitations = item.optInt("pending_elicitations_count", 0),
                title = item.optString("title").ifEmpty { null },
                // Present only on richer responses; the list omits it, so this
                // is usually null (treated as online by the diff).
                runnerOnline =
                    item
                        .takeUnless {
                            it.isNull(
                                "runner_online",
                            )
                        }?.optBoolean("runner_online"),
            ),
        )
    }
    return out
}
