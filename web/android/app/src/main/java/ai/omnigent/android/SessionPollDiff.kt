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
    // The HTTP list omits `runner_online`; the worker fills this from /health
    // before posting and treats null as unverified.
    val runnerOnline: Boolean?,
)

/**
 * Prior-run snapshot of a single session, keyed by id in the persisted map.
 *
 * [status] and [pendingElicitations] are the last ACKNOWLEDGED values per
 * dimension — advanced when a transition was posted (or none existed), rolled
 * back per-dimension by [holdSuppressedTransitions] when a detected transition
 * was suppressed because the app was on screen.
 *
 * [held] marks an entry carrying such an un-posted edge. It exists for cap
 * eviction: a held elicitation edge is (status = current, pending = rolled
 * back, often 0), which looks settled to the mid-flight priority test — at the
 * snapshot cap it would evict, and the edge would be lost. [mergeSnapshot]
 * treats held entries as mid-flight. Cleared automatically the first time the
 * session is saved without a hold (edge posted, or it evaporated in-app).
 * [offWindowPolls] bounds how long any absent running/held entry can retain cap
 * priority. Current-window observations reset it to zero.
 */
data class SessionSnapshot(
    val status: String,
    val pendingElicitations: Int,
    val held: Boolean = false,
    val offWindowPolls: Int = 0,
)

/** Snapshot the current list into the map persisted between poll runs. */
fun buildSnapshot(sessions: List<SessionState>): Map<String, SessionSnapshot> =
    sessions.associate { it.id to SessionSnapshot(it.status, it.pendingElicitations) }

/** Cap on the persisted snapshot so carried-forward off-window entries can't grow unbounded. */
const val MAX_SNAPSHOT_ENTRIES = 500

/** Twenty-four absent polls (at least six hours at WorkManager's cadence). */
const val MAX_OFF_WINDOW_POLLS = 24

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
 * prior entries fill the remaining slots until [MAX_OFF_WINDOW_POLLS], ordered
 * by transition priority and then recency. The TTL bounds unresolved zombies;
 * the cap is large enough for a full 20-row window plus every retained cohort,
 * including migration from the former 200-entry cap.
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
            .mapValues { (_, snapshot) ->
                snapshot.copy(offWindowPolls = snapshot.offWindowPolls + 1)
            }.filterValues { it.offWindowPolls <= MAX_OFF_WINDOW_POLLS }
            .entries
            // Keep mid-flight sessions first so a cap can never drop a `running`
            // (or elicitation-bearing) entry in favor of a settled one. A held
            // entry (un-posted suppressed edge) counts as mid-flight: its
            // rolled-back values can look settled (e.g. pending 0), but
            // evicting it would lose the edge exactly like evicting a running
            // one.
            .sortedWith(
                compareByDescending<Map.Entry<String, SessionSnapshot>> {
                    it.value.status == "running" ||
                        it.value.pendingElicitations > 0 ||
                        it.value.held
                }.thenBy { it.value.offWindowPolls },
            )
    for ((id, snapshot) in carried.take(room)) merged[id] = snapshot
    return merged
}

/** Terminal candidates that are still terminal in a short confirmation poll. */
fun confirmIdleTransitions(
    candidates: List<SessionState>,
    confirmation: List<SessionState>,
): List<SessionState> {
    val confirmedStatuses = confirmation.associate { it.id to it.status }
    return candidates.filter { confirmedStatuses[it.id] in TERMINAL_STATUSES }
}

/**
 * Roll back — PER DIMENSION — the parts of [merged] whose transitions were
 * DETECTED but not POSTED (the app was on screen, so posting was skipped in
 * favor of the SPA's in-app path), so the un-posted edge is not consumed by
 * the snapshot advance.
 *
 * Without this, "worker saw the edge while visible, activity stopped before
 * the SPA settled it" silently loses the notification on both paths forever —
 * violating the poller's own duplicate-over-miss policy. Holding the prior
 * value keeps the edge pending: every visible poll re-detects (and re-holds),
 * and the first poll that runs with the app off screen posts it. If the user
 * did see it in-app, that post is a duplicate — accepted by policy.
 *
 * The rollback is per-dimension, NOT the whole prior record: a visible poll
 * seeing running/0 → waiting/1 suppresses only the elicitation edge, so only
 * the pending count is rolled back — status advances to `waiting`. Restoring
 * the entire prior record would rewrite status to `running`, and after the
 * user answers in-app, the next background poll's idle/0 would fabricate a
 * stale running → idle "finished" notification. With the per-dimension hold,
 * the acknowledged status is `waiting`, so no idle edge exists — and the
 * answered elicitation (pending back at 0) fires nothing either.
 *
 * Held entries are flagged [SessionSnapshot.held] so cap eviction keeps them
 * (their rolled-back values can look settled). Sessions in the suppressed
 * lists always have a [previous] entry (both detectors require one); an id
 * unexpectedly missing there is left at its merged value.
 */
fun holdSuppressedTransitions(
    merged: Map<String, SessionSnapshot>,
    previous: Map<String, SessionSnapshot>,
    suppressedIdle: List<SessionState>,
    suppressedElicitations: List<SessionState>,
): Map<String, SessionSnapshot> {
    if (suppressedIdle.isEmpty() && suppressedElicitations.isEmpty()) return merged
    val idleIds = suppressedIdle.map { it.id }.toSet()
    val elicitationIds = suppressedElicitations.map { it.id }.toSet()
    return merged.mapValues { (id, snapshot) ->
        val holdIdle = id in idleIds
        val holdElicitation = id in elicitationIds
        if (!holdIdle && !holdElicitation) return@mapValues snapshot
        val prior = previous[id] ?: return@mapValues snapshot
        SessionSnapshot(
            status = if (holdIdle) prior.status else snapshot.status,
            pendingElicitations =
                if (holdElicitation) prior.pendingElicitations else snapshot.pendingElicitations,
            held = true,
        )
    }
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
