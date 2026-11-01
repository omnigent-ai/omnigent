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
        // Required fields, isNull-aware: optString turns an explicit JSON null
        // into the literal string "null", so two malformed `"id": null` rows
        // would BOTH parse as session "null" — manufacturing fake transitions
        // between them and a /c/null deep link — instead of being skipped as
        // documented above.
        val id = item.stringOrNull("id") ?: continue
        val status = item.stringOrNull("status") ?: continue
        out.add(
            SessionState(
                id = id,
                status = status,
                pendingElicitations = item.optInt("pending_elicitations_count", 0),
                // Same isNull-aware read; a null title would otherwise surface
                // as a notification titled "null" instead of the "New session"
                // fallback.
                title = item.stringOrNull("title"),
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

/** Parse the batch `/health?session_ids=...` response's known runner states. */
fun parseRunnerLiveness(body: String): Map<String, Boolean> {
    val sessions =
        try {
            JSONObject(body).optJSONObject("sessions")
        } catch (_: Throwable) {
            null
        } ?: return emptyMap()
    return buildMap {
        for (id in sessions.keys()) {
            val state = sessions.optJSONObject(id) ?: continue
            val online = state.opt("runner_online") as? Boolean ?: continue
            put(id, online)
        }
    }
}

/**
 * A string field's value, or null when the key is absent, explicitly JSON
 * null, or empty — never the literal string "null" that [JSONObject.optString]
 * produces for an explicit null.
 */
private fun JSONObject.stringOrNull(key: String): String? =
    takeUnless { isNull(key) }
        ?.optString(key)
        ?.ifEmpty { null }
