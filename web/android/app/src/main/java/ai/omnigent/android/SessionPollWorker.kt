package ai.omnigent.android

import android.content.Context
import android.webkit.CookieManager
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * Background session poller. Fires local notifications when a session finishes
 * (`running` → `idle`/`failed`) or gains a new pending elicitation, without the
 * app being foregrounded — an interim, OS-scheduled mirror of the web SPA's
 * `useIdleNotifications` hook, driven by WorkManager instead of a live poll.
 *
 * Client-only: it reuses the JWT the shell already injected into the WebView's
 * [CookieManager] (see MainActivity.onSessionToken) rather than duplicating
 * credential storage, and hits the existing `GET /v1/sessions` endpoint. No
 * server change.
 *
 * Terminal transitions get the same 10-second settle check as the SPA: a
 * second list read must still show the session terminal before it can post.
 *
 * Order within a successful poll is detect → notify → save (see doWork): a
 * mid-run process kill after notifying must not advance the snapshot, or the
 * finish would be silently missed. The snapshot is MERGED, not replaced, so a
 * `running` session that scrolls off the fixed top-window keeps its prior
 * status until observed terminal. Posting goes through
 * [NativeNotificationManager], whose constructor (re-)creates the channel so a
 * cold background process still has it, and which no-ops when the
 * POST_NOTIFICATIONS grant is missing.
 *
 * Graceful no-ops (always [Result.success], never a crash or retry storm):
 *   * not logged in / cookie expired → no session cookie → nothing to poll
 *   * no pinned server → nothing to poll
 *   * network/HTTP error → skip this run, prior snapshot untouched, retry next tick
 */
class SessionPollWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result =
        withContext(Dispatchers.IO) {
            val origin = pinnedOrigin() ?: return@withContext Result.success()

            // Capture the snapshot generation FIRST — before even the cookie
            // read. MainActivity's account binding bumps it
            // BEFORE injecting the new account's cookie, so any ordering of
            // "worker reads cookie" vs "login lands" is caught: a capture
            // taken after the bump but paired with the OLD account's cookie
            // would sail through every later generation recheck.
            val store = SessionSnapshotStore(applicationContext)
            val generationAtStart = store.generation()

            val secure = origin.startsWith("https://")
            val jwt =
                extractCookieValue(
                    CookieManager.getInstance().getCookie(origin),
                    sessionCookieName(secure),
                )
                    ?: return@withContext Result.success() // not logged in / expired — no-op

            // Bind the CREDENTIAL to the account identity, at the worker. The
            // generation guard alone cannot order us against the login's async
            // setCookie: onSessionToken bumps the generation and persists the
            // new identity BEFORE the cookie commit lands, so a worker can
            // capture the post-bump generation yet read the PRE-switch cookie
            // — and pass every generation recheck (including the CAS, whose
            // generation genuinely matches). Comparing the cookie's own
            // computed identity against the persisted marker closes every such
            // interleaving regardless of injection timing. Null also aborts:
            // onPageReady binds WebView-side accounts-mode sessions before the
            // worker is allowed to persist or post.
            val cookieIdentity = sessionAccountIdentity(origin, jwt)
            if (identityMismatch(
                    store.lastAccountIdentity(),
                    cookieIdentity,
                )
            ) {
                return@withContext Result.success()
            }
            // Generation recheck immediately after the cookie read stays as
            // the second barrier against an intervening account bind/unbind.
            if (store.generation() != generationAtStart) return@withContext Result.success()

            val body = fetchSessions(origin, jwt, secure) ?: return@withContext Result.success()
            val sessions = parseSessionList(body)

            // Multi-account guards: a poll started against account A can land
            // after account B logs in. Re-read the pinned origin (a server
            // switch re-points ServerStore) AND the snapshot generation (a
            // same-server account switch bumps it) right after the fetch — the
            // long pole; if either changed mid-poll, bail without notifying or
            // saving so we don't post A's titles/deep-links after the
            // account-switch cancelAll(), or re-persist A's snapshot over the
            // clear (the next poll would then diff B against A).
            if (!isPollCurrent(store, origin, generationAtStart, cookieIdentity)) {
                return@withContext Result.success()
            }

            val previous = store.load()

            // Diff FIRST, then notify, then save (in that order). If the process
            // is killed after notifying but before saving, the next run diffs
            // against the un-advanced snapshot and re-fires — an occasional
            // duplicate beats a silently-missed finish alert. Saving first would
            // turn a mid-run kill into a permanent miss.
            //
            // A first run (empty `previous`) yields no transitions, so nothing
            // fires; we still fall through to save so the next run has a baseline.
            var toSave = mergeSnapshot(previous, sessions)
            if (previous.isNotEmpty()) {
                val idleTransitions = detectIdleTransitions(previous, sessions)
                val newElicitations = detectNewElicitations(previous, sessions)
                if (AppVisibility.isAppVisible) {
                    // App on screen: the SPA's live useIdleNotifications hook
                    // surfaces these edges in-app, so don't post a duplicate —
                    // but do NOT consume them either. If the activity stops
                    // before the SPA settles the edge, a consumed snapshot
                    // would lose it on both paths forever. Hold the suppressed
                    // sessions at their prior state so every poll re-detects
                    // until one runs with the app off screen and posts
                    // (duplicate-over-miss, the poller's stated policy).
                    toSave =
                        holdSuppressedTransitions(
                            toSave,
                            previous,
                            idleTransitions,
                            newElicitations,
                        )
                } else {
                    // Constructing this ensures the notification channel exists
                    // in THIS process — the worker often runs in a cold
                    // background process where no Activity created it, and an
                    // O+ post to a missing channel is silently dropped. post()
                    // itself no-ops when POST_NOTIFICATIONS is not granted, so
                    // an ungranted background run never crashes.
                    val notifications = NativeNotificationManager(applicationContext)

                    // Elicitations are actionable immediately, but only a fresh
                    // /health batch can establish that their runner is online.
                    val elicitationLiveness =
                        fetchRunnerLiveness(origin, jwt, secure, newElicitations)
                    val onlineElicitations =
                        newElicitations.withLiveness(elicitationLiveness).filter {
                            it.runnerOnline == true
                        }
                    val unknownElicitations =
                        newElicitations.filter { elicitationLiveness?.get(it.id) == null }
                    if (AppVisibility.isAppVisible) {
                        toSave =
                            holdSuppressedTransitions(
                                toSave,
                                previous,
                                emptyList(),
                                newElicitations,
                            )
                    } else {
                        notify(
                            store,
                            generationAtStart,
                            cookieIdentity,
                            notifications,
                            onlineElicitations,
                            ELICITATION_BODY,
                        )
                        // A temporary/older-server liveness omission must not
                        // consume the edge; retry once liveness is known.
                        toSave =
                            holdSuppressedTransitions(
                                toSave,
                                previous,
                                emptyList(),
                                unknownElicitations,
                            )
                    }

                    // A prompt supersedes a turn-end cue for the same session.
                    // For the remainder, wait the SPA's settle interval and
                    // require a second list observation to remain terminal.
                    val elicitationIds = newElicitations.mapTo(mutableSetOf()) { it.id }
                    val idleCandidates = idleTransitions.filter { it.id !in elicitationIds }
                    if (idleCandidates.isNotEmpty()) {
                        delay(IDLE_SETTLE_MS)
                        if (!isPollCurrent(
                                store,
                                origin,
                                generationAtStart,
                                cookieIdentity,
                            )
                        ) {
                            return@withContext Result.success()
                        }
                        val confirmationBody = fetchSessions(origin, jwt, secure)
                        if (confirmationBody == null) {
                            toSave =
                                holdSuppressedTransitions(
                                    toSave,
                                    previous,
                                    idleCandidates,
                                    emptyList(),
                                )
                        } else {
                            val confirmation = parseSessionList(confirmationBody)
                            val confirmationById = confirmation.associateBy { it.id }
                            val idleCandidateIds = idleCandidates.mapTo(mutableSetOf()) { it.id }
                            val observed = idleCandidates.filter { it.id in confirmationById }
                            toSave =
                                toSave.mapValues { (id, snapshot) ->
                                    val status = confirmationById[id]?.status
                                    if (id !in idleCandidateIds || status == null) {
                                        snapshot
                                    } else {
                                        snapshot.copy(status = status)
                                    }
                                }
                            val absent = idleCandidates.filter { it.id !in confirmationById }
                            toSave =
                                holdSuppressedTransitions(
                                    toSave,
                                    previous,
                                    absent,
                                    emptyList(),
                                )

                            val confirmed = confirmIdleTransitions(observed, confirmation)
                            if (AppVisibility.isAppVisible) {
                                toSave =
                                    holdSuppressedTransitions(
                                        toSave,
                                        previous,
                                        confirmed,
                                        emptyList(),
                                    )
                            } else {
                                val idleLiveness =
                                    fetchRunnerLiveness(origin, jwt, secure, confirmed)
                                val onlineIdle =
                                    confirmed.withLiveness(idleLiveness).filter {
                                        it.runnerOnline == true
                                    }
                                notify(
                                    store,
                                    generationAtStart,
                                    cookieIdentity,
                                    notifications,
                                    onlineIdle,
                                    IDLE_BODY,
                                )
                                val unknownIdle =
                                    confirmed.filter { idleLiveness?.get(it.id) == null }
                                toSave =
                                    holdSuppressedTransitions(
                                        toSave,
                                        previous,
                                        unknownIdle,
                                        emptyList(),
                                    )
                            }
                        }
                    }
                }
            }

            // MERGE rather than replace (see mergeSnapshot): carrying recent
            // off-window entries forward preserves a scrolled-off `running`
            // session's eventual finish, while the absent-poll TTL eventually
            // expires entries that can no longer resolve.
            //
            // Account-aware compare-and-swap: an unbound or switched account
            // cannot be overwritten even if a future caller misses a recheck.
            store.saveIfCurrentAccount(toSave, generationAtStart, cookieIdentity)
            Result.success()
        }

    /** The pinned server's origin, or null when no server is set. */
    private fun pinnedOrigin(): String? =
        ServerStore(applicationContext).let {
            if (it.hasServer()) originOf(it.currentServerUrl()) else null
        }

    private fun isPollCurrent(
        store: SessionSnapshotStore,
        origin: String,
        generation: Long,
        identity: String,
    ): Boolean =
        pinnedOrigin() == origin &&
            store.generation() == generation &&
            !identityMismatch(store.lastAccountIdentity(), identity)

    /**
     * Post one known-online notification per session, with the account check
     * and post sharing the store's account lock. An account switch therefore
     * either cancels a completed old-account post or invalidates it before it
     * can post. Each is keyed by the session id as the notification
     * TAG plus a stable [notificationIdFor] numeric id: the tag carries the
     * distinctness (String.hashCode-derived ids can collide across sessions,
     * e.g. "Aa"/"BB", and a collision would silently replace the other
     * session's notification), while the stable id keeps a re-fire updating the
     * same notification instead of stacking. Deep-links to the session.
     */
    private fun notify(
        store: SessionSnapshotStore,
        generation: Long,
        identity: String,
        notifications: NativeNotificationManager,
        sessions: List<SessionState>,
        body: String,
    ) {
        for (session in sessions) {
            if (session.runnerOnline != true) continue
            store.runIfCurrentAccount(generation, identity) {
                notifications.notify(
                    title = sessionDisplayLabel(session.title),
                    body = body,
                    navigatePath = "/c/${session.id}",
                    notificationId = notificationIdFor(session.id),
                    tag = session.id,
                )
            }
        }
    }

    private fun List<SessionState>.withLiveness(
        liveness: Map<String, Boolean>?,
    ): List<SessionState> = map { it.copy(runnerOnline = liveness?.get(it.id)) }

    private fun fetchRunnerLiveness(
        origin: String,
        jwt: String,
        secure: Boolean,
        sessions: List<SessionState>,
    ): Map<String, Boolean>? {
        if (sessions.isEmpty()) return emptyMap()
        val ids = sessions.map { it.id }.distinct().joinToString(",")
        val encoded = URLEncoder.encode(ids, "UTF-8")
        val body = fetch(origin, "/health?session_ids=$encoded", jwt, secure) ?: return null
        return parseRunnerLiveness(body)
    }

    /**
     * GET the sessions list, authenticating with the reused JWT as both a
     * `Cookie` (matching the WebView) and `Authorization: Bearer` (the server
     * accepts either). Returns the body on 200, or null on any non-200 /
     * network error so the run no-ops and retries next tick.
     */
    private fun fetchSessions(
        origin: String,
        jwt: String,
        secure: Boolean,
    ): String? = fetch(origin, SESSIONS_LIST_PATH, jwt, secure)

    private fun fetch(
        origin: String,
        path: String,
        jwt: String,
        secure: Boolean,
    ): String? {
        // Build the connection INSIDE the try: a malformed/legacy non-HTTP
        // persisted origin makes URL(...)/openConnection()/the cast throw, and
        // that must no-op like every other error path rather than fail the run.
        var conn: HttpURLConnection? = null
        return try {
            conn = URL(origin + path).openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            // This request carries the session JWT (Cookie + Bearer). Do NOT
            // auto-follow redirects: a cross-origin or HTTPS→HTTP-downgrade 3xx
            // would otherwise resend the credential off the pinned origin. A 3xx
            // now surfaces as a non-200 responseCode and is treated as a no-op.
            conn.instanceFollowRedirects = false
            conn.setRequestProperty("Cookie", "${sessionCookieName(secure)}=$jwt")
            conn.setRequestProperty("Authorization", "Bearer $jwt")
            conn.setRequestProperty("Accept", "application/json")
            conn.connectTimeout = HTTP_TIMEOUT_MS
            conn.readTimeout = HTTP_TIMEOUT_MS
            if (conn.responseCode != 200) return null
            // Cheap size cap: the endpoint is limit=20 so a well-behaved body is
            // small, but readText() is otherwise unbounded. Read in fixed chunks
            // into a StringBuilder that grows to the actual body size, capped at
            // MAX_RESPONSE_BYTES; a larger body is treated as a no-op (null)
            // rather than buffered whole. Sizing the buffer to the response (a
            // sane page is a few KB) avoids allocating the full cap every poll.
            conn.inputStream.bufferedReader().use { reader ->
                val chunk = CharArray(READ_CHUNK_CHARS)
                val out = StringBuilder()
                while (true) {
                    val n = reader.read(chunk)
                    if (n < 0) break
                    // Appending this chunk would exceed the cap → oversized, bail.
                    if (out.length + n > MAX_RESPONSE_BYTES) return null
                    out.append(chunk, 0, n)
                }
                out.toString()
            }
        } catch (_: Throwable) {
            null
        } finally {
            conn?.disconnect()
        }
    }

    companion object {
        // Mirrors useIdleNotifications' IDLE_BODY / ELICITATION_BODY.
        const val IDLE_BODY = "Agent finished and is ready for your input."
        const val ELICITATION_BODY = "Agent is asking for your input."
        private const val IDLE_SETTLE_MS = 10_000L
        private const val HTTP_TIMEOUT_MS = 15_000

        // Upper bound on the list-response body we buffer. The endpoint is
        // limit=20, so a sane page is well under this; a larger body is treated
        // as a no-op rather than read whole. ~1M chars.
        private const val MAX_RESPONSE_BYTES = 1_048_576

        // Per-read chunk size. Small and fixed so the buffer footprint tracks
        // the actual (tiny) response rather than the cap above.
        private const val READ_CHUNK_CHARS = 8_192
    }
}
