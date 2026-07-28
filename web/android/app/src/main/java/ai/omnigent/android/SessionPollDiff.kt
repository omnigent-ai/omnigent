package ai.omnigent.android

// Pure snapshot-diff logic for the background session poller, mirroring the web
// SPA's `idleTransitions.ts`. Kept free of Android imports so the "which
// sessions newly need attention" decision is unit-testable without a device,
// exactly as the web module is testable without React.
//
// SessionPollWorker owns the I/O (HTTP fetch, snapshot persistence); this
// module only diffs two snapshots.

// Statuses that mean "the agent stopped and is waiting on the user".
private val TERMINAL_STATUSES = setOf("idle", "failed")

/** One session as seen in a `GET /v1/sessions` list item — the fields we diff. */
data class SessionState(
    val id: String,
    val status: String,
    val pendingElicitations: Int,
    // Best-effort display name; the list carries `title` and a fallback name.
    val title: String?,
    // The HTTP list omits `runner_online`, so this is usually null. Null (or
    // true) is treated as online — we never over-suppress a genuine finish.
    val runnerOnline: Boolean?,
)

/** Prior-run snapshot of a single session, keyed by id in the persisted map. */
data class SessionSnapshot(
    val status: String,
    val pendingElicitations: Int,
)

/** Snapshot the current list into the map persisted between poll runs. */
fun buildSnapshot(sessions: List<SessionState>): Map<String, SessionSnapshot> =
    sessions.associate { it.id to SessionSnapshot(it.status, it.pendingElicitations) }

/** Cap on the persisted snapshot so carried-forward off-window entries can't grow unbounded. */
const val MAX_SNAPSHOT_ENTRIES = 200

/**
 * Merge the current poll's snapshot over the previous one, carrying forward
 * prior entries for sessions NOT in this poll's window.
 *
 * The list endpoint returns only the top window (`limit=20`, no `since`), so a
 * `running` session can scroll off the window between polls and reappear later
 * as terminal. A full-replace snapshot would drop its prior `running` status
 * while it's off-window, so the eventual `running` → terminal edge would be
 * missed. Merging keeps the off-window entry until we actually observe it
 * terminal — then the current window's terminal status overwrites it.
 *
 * Bounded by [cap]: current-window entries are always kept; carried-forward
 * prior entries fill the remaining slots, prioritizing sessions mid-flight
 * (`running`, or with pending elicitations) — those whose finish/elicitation
 * edge we'd lose if dropped — over already-settled ones, so a churn of
 * ephemeral sessions can't grow the snapshot without bound.
 */
fun mergeSnapshot(
    previous: Map<String, SessionSnapshot>,
    sessions: List<SessionState>,
    cap: Int = MAX_SNAPSHOT_ENTRIES,
): Map<String, SessionSnapshot> {
    val current = buildSnapshot(sessions)
    val merged = LinkedHashMap<String, SessionSnapshot>(current)
    val room = cap - current.size
    if (room <= 0) return merged
    val carried =
        previous
            .filterKeys { it !in current }
            .entries
            // Keep mid-flight sessions first so a cap can never drop a `running`
            // (or elicitation-bearing) entry in favor of a settled one.
            .sortedByDescending { it.value.status == "running" || it.value.pendingElicitations > 0 }
    for ((id, snapshot) in carried.take(room)) merged[id] = snapshot
    return merged
}

/**
 * Sessions whose status went `running` → `idle`/`failed` between the previous
 * snapshot and the current list.
 *
 * Requiring the *previous* status to be exactly `running` means a first run
 * (empty [previous]) fires nothing, and steady-state idle rows never re-notify
 * on a later poll — only a genuine finish does. This is what dedups a
 * transition across runs: once notified, the persisted status is the terminal
 * one, so the next poll's `previous` is no longer `running`.
 */
fun detectIdleTransitions(
    previous: Map<String, SessionSnapshot>,
    sessions: List<SessionState>,
): List<SessionState> =
    sessions.filter { session ->
        session.status in TERMINAL_STATUSES && previous[session.id]?.status == "running"
    }

/**
 * Sessions whose pending-elicitation count *increased* between the previous
 * snapshot and the current list — the agent just raised a new prompt.
 *
 * Requiring a previous entry means a first run with already-pending
 * elicitations fires nothing; only an increase this client observed does. A
 * 0 → 1 change fires; a steady count or a decrease (the user answered) does
 * not. Persisting the new count dedups the increase across runs.
 */
fun detectNewElicitations(
    previous: Map<String, SessionSnapshot>,
    sessions: List<SessionState>,
): List<SessionState> =
    sessions.filter { session ->
        val prior = previous[session.id] ?: return@filter false
        session.pendingElicitations > prior.pendingElicitations
    }

/**
 * The session-cookie name the server issues, matching MainActivity's injection
 * and the server's `session_cookie_name`: the `__Host-` prefix on HTTPS.
 */
fun sessionCookieName(secure: Boolean): String = if (secure) "__Host-ap_session" else "ap_session"

/**
 * Pull one cookie's value out of a `CookieManager.getCookie` string
 * ("a=1; b=2; …"), or null if [name] isn't present. Returned trimmed; a
 * present-but-empty value is treated as absent (null).
 */
fun extractCookieValue(
    cookieHeader: String?,
    name: String,
): String? {
    if (cookieHeader.isNullOrBlank()) return null
    for (part in cookieHeader.split(';')) {
        val eq = part.indexOf('=')
        if (eq <= 0) continue
        if (part.substring(0, eq).trim() != name) continue
        return part.substring(eq + 1).trim().ifEmpty { null }
    }
    return null
}

/**
 * Display label for a session notification, mirroring the web
 * `conversationDisplayLabel` fallback chain (title, then a generic label). The
 * background poll has no access to the wrapper-label lookup the SPA uses for
 * native coding-agent names, so it falls back straight to the generic label.
 */
fun sessionDisplayLabel(title: String?): String = title?.takeIf { it.isNotBlank() } ?: "New session"

/**
 * Lowest notification id a per-session notification may use. Kept strictly
 * above the reserved badge-summary id (1, `BADGE_NOTIFICATION_ID`) so a session
 * notification can never replace the badge.
 */
const val MIN_SESSION_NOTIFICATION_ID = 2

/**
 * Stable per-session notification id, derived deterministically from the
 * session id and mapped into `[MIN_SESSION_NOTIFICATION_ID, Int.MAX_VALUE]`.
 *
 * Deterministic across worker runs and manager instances (same id → same
 * notification id), so a session that finishes, is re-observed, and fires again
 * UPDATES its own notification instead of spawning a duplicate — and, crucially,
 * distinct sessions get distinct ids so a later session's finish never silently
 * replaces an earlier, still-undismissed one. `String.hashCode` is a specified,
 * stable algorithm, so the mapping is reproducible. Guarded to never land on the
 * reserved badge id (1).
 */
fun notificationIdFor(sessionId: String): Int {
    val span = (Int.MAX_VALUE - MIN_SESSION_NOTIFICATION_ID).toLong() + 1
    val unsigned = sessionId.hashCode().toLong() and 0xFFFFFFFFL
    return MIN_SESSION_NOTIFICATION_ID + (unsigned % span).toInt()
}

/** The list query the poller hits — the existing endpoint, no new params. */
const val SESSIONS_LIST_PATH =
    "/v1/sessions?order=desc&sort_by=updated_at&limit=20&include_archived=true"
