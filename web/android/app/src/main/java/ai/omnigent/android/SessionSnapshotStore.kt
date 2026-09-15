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
                    // Quarantine keys no valid parse can produce (the parser
                    // skips null/blank ids): a blob poisoned by an older build
                    // with "null" entries would otherwise carry them forward at
                    // cap priority forever, able to evict a genuine mid-flight
                    // session.
                    if (id.isBlank() || id == "null") continue
                    val entry = json.optJSONObject(id) ?: continue
                    val status = entry.optString(FIELD_STATUS).ifEmpty { null } ?: continue
                    put(
                        id,
                        SessionSnapshot(
                            status,
                            entry.optInt(FIELD_PENDING, 0),
                            entry.optBoolean(FIELD_HELD, false),
                            entry.optInt(FIELD_OFF_WINDOW_POLLS, 0).coerceAtLeast(0),
                        ),
                    )
                }
            }
        } catch (_: Throwable) {
            // Corrupt/legacy blob — treat as no prior snapshot rather than crash.
            emptyMap()
        }
    }

    /**
     * Compare-and-swap save — the ONLY snapshot write path. Both generation
     * and account identity must still match, so an unbound cookie can never
     * seed a baseline and an account switch cannot be overwritten by an old
     * worker.
     */
    fun saveIfCurrentAccount(
        snapshot: Map<String, SessionSnapshot>,
        expectedGeneration: Long,
        expectedIdentity: String,
    ): Boolean =
        synchronized(generationLock) {
            if (generation() != expectedGeneration) return false
            if (lastAccountIdentity() != expectedIdentity) return false
            prefs.edit().putString(KEY_SNAPSHOT, encode(snapshot)).apply()
            true
        }

    private fun encode(snapshot: Map<String, SessionSnapshot>): String {
        val json = JSONObject()
        for ((id, state) in snapshot) {
            val entry =
                JSONObject()
                    .put(FIELD_STATUS, state.status)
                    .put(FIELD_PENDING, state.pendingElicitations)
            // Written only when set — the common (un-held) entry stays compact
            // and byte-identical to the pre-`held` format.
            if (state.held) entry.put(FIELD_HELD, true)
            if (state.offWindowPolls > 0) {
                entry.put(FIELD_OFF_WINDOW_POLLS, state.offWindowPolls)
            }
            json.put(id, entry)
        }
        return json.toString()
    }

    /**
     * Bind [identity], clearing the previous baseline and advancing its
     * generation in the same locked preference edit. [onChanged] runs under
     * that lock so notification cancellation cannot race a guarded worker post.
     */
    fun bindAccount(
        identity: String,
        onChanged: () -> Unit,
    ): Boolean =
        synchronized(generationLock) {
            if (lastAccountIdentity() == identity) return false
            prefs
                .edit()
                .remove(KEY_SNAPSHOT)
                .putLong(KEY_GENERATION, generation() + 1)
                .putString(KEY_IDENTITY, identity)
                .apply()
            onChanged()
            true
        }

    /** Unbind the current account and invalidate its worker state atomically. */
    fun unbindAccount(onChanged: () -> Unit): Boolean =
        synchronized(generationLock) {
            if (lastAccountIdentity() == null && !prefs.contains(KEY_SNAPSHOT)) return false
            prefs
                .edit()
                .remove(KEY_SNAPSHOT)
                .remove(KEY_IDENTITY)
                .putLong(KEY_GENERATION, generation() + 1)
                .apply()
            onChanged()
            true
        }

    /**
     * Monotonic snapshot epoch, bumped by every account bind/unbind. Persisted
     * because the worker often runs in a different process incarnation.
     */
    fun generation(): Long = prefs.getLong(KEY_GENERATION, 0L)

    /**
     * Identity marker (see [sessionAccountIdentity]) bound to the WebView's
     * current session cookie, or null while logged out/unverified.
     */
    fun lastAccountIdentity(): String? = prefs.getString(KEY_IDENTITY, null)

    /** Run [action] only while the worker still belongs to the bound account. */
    fun runIfCurrentAccount(
        expectedGeneration: Long,
        expectedIdentity: String,
        action: () -> Unit,
    ): Boolean =
        synchronized(generationLock) {
            if (generation() != expectedGeneration) return false
            if (lastAccountIdentity() != expectedIdentity) return false
            action()
            true
        }

    private companion object {
        // Makes account transitions atomic against guarded saves and posts.
        // MainActivity and WorkManager share this process.
        val generationLock = Any()

        const val PREFS = "ai.omnigent.android.session_snapshot"
        const val KEY_SNAPSHOT = "snapshot_json"
        const val KEY_GENERATION = "snapshot_generation"
        const val KEY_IDENTITY = "account_identity"
        const val FIELD_STATUS = "status"
        const val FIELD_PENDING = "pending"
        const val FIELD_HELD = "held"
        const val FIELD_OFF_WINDOW_POLLS = "off_window_polls"
    }
}
