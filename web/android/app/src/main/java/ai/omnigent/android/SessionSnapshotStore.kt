package ai.omnigent.android

import android.content.Context
import org.json.JSONObject

/**
 * Persists the prior-run session snapshot (id → {status, pending_elicitations})
 * between [SessionPollWorker] runs, so the worker can diff the current poll
 * against what it last saw and only notify on a genuine transition. Backed by
 * SharedPreferences, mirroring [ServerStore]'s minimal style.
 *
 * The snapshot is the dedup mechanism: once a transition is notified, the new
 * (terminal / higher-count) state is persisted, so the next poll's diff no
 * longer sees the same edge.
 */
class SessionSnapshotStore(
    context: Context,
) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /**
     * The last persisted snapshot, or an empty map when nothing has been stored
     * yet (a first run, or after login cleared it). An empty map makes the
     * first diff fire nothing — matching the SPA's "seed from first observed
     * value" semantics.
     */
    fun load(): Map<String, SessionSnapshot> {
        val raw = prefs.getString(KEY_SNAPSHOT, null) ?: return emptyMap()
        return try {
            val json = JSONObject(raw)
            buildMap {
                for (id in json.keys()) {
                    val entry = json.optJSONObject(id) ?: continue
                    val status = entry.optString(FIELD_STATUS).ifEmpty { null } ?: continue
                    put(id, SessionSnapshot(status, entry.optInt(FIELD_PENDING, 0)))
                }
            }
        } catch (_: Throwable) {
            // Corrupt/legacy blob — treat as no prior snapshot rather than crash.
            emptyMap()
        }
    }

    /** Replace the persisted snapshot with the current poll's state. */
    fun save(snapshot: Map<String, SessionSnapshot>) {
        val json = JSONObject()
        for ((id, state) in snapshot) {
            json.put(
                id,
                JSONObject()
                    .put(FIELD_STATUS, state.status)
                    .put(FIELD_PENDING, state.pendingElicitations),
            )
        }
        prefs.edit().putString(KEY_SNAPSHOT, json.toString()).apply()
    }

    /**
     * Drop the snapshot. Called on a fresh login so the next poll seeds from
     * scratch and can't fire stale transitions against a previous user's state.
     */
    fun clear() {
        prefs.edit().remove(KEY_SNAPSHOT).apply()
    }

    private companion object {
        const val PREFS = "ai.omnigent.android.session_snapshot"
        const val KEY_SNAPSHOT = "snapshot_json"
        const val FIELD_STATUS = "status"
        const val FIELD_PENDING = "pending"
    }
}
