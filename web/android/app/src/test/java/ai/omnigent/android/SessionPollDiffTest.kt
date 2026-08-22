package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the pure snapshot-diff logic, mirroring the web SPA's
 * `idleTransitions.test.ts`. No Android/Robolectric — this logic is a plain
 * Kotlin function set, testable without a device.
 */
class SessionPollDiffTest {
    private fun session(
        id: String,
        status: String,
        pending: Int = 0,
        title: String? = null,
        runnerOnline: Boolean? = null,
    ) = SessionState(id, status, pending, title, runnerOnline)

    // --- idle transitions (running -> terminal) ---------------------------

    @Test
    fun `running to idle is an idle transition`() {
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "idle")))
        assertEquals(listOf("a"), transitions.map { it.id })
    }

    @Test
    fun `running to failed is an idle transition`() {
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "failed")))
        assertEquals(listOf("a"), transitions.map { it.id })
    }

    @Test
    fun `no prior snapshot fires no idle transition`() {
        // First run: empty previous — a session already idle must not notify.
        val transitions = detectIdleTransitions(emptyMap(), listOf(session("a", "idle")))
        assertTrue(transitions.isEmpty())
    }

    @Test
    fun `steady idle does not re-notify on a later poll`() {
        // Previous already terminal (as persisted after the first notify) — the
        // dedup: the same idle session must not fire again next run.
        val previous = mapOf("a" to SessionSnapshot("idle", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "idle")))
        assertTrue(transitions.isEmpty())
    }

    @Test
    fun `waiting to idle is not an idle transition`() {
        // Only an exact running -> terminal edge counts.
        val previous = mapOf("a" to SessionSnapshot("waiting", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "idle")))
        assertTrue(transitions.isEmpty())
    }

    // --- new elicitations (pending count increased) -----------------------

    @Test
    fun `elicitation count increase fires`() {
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val fired = detectNewElicitations(previous, listOf(session("a", "running", pending = 1)))
        assertEquals(listOf("a"), fired.map { it.id })
    }

    @Test
    fun `no prior snapshot fires no elicitation`() {
        // Already-pending on the first observation must not notify.
        val fired = detectNewElicitations(emptyMap(), listOf(session("a", "waiting", pending = 2)))
        assertTrue(fired.isEmpty())
    }

    @Test
    fun `steady elicitation count does not fire`() {
        val previous = mapOf("a" to SessionSnapshot("waiting", 2))
        val fired = detectNewElicitations(previous, listOf(session("a", "waiting", pending = 2)))
        assertTrue(fired.isEmpty())
    }

    @Test
    fun `answered elicitation decrease does not fire`() {
        val previous = mapOf("a" to SessionSnapshot("waiting", 2))
        val fired = detectNewElicitations(previous, listOf(session("a", "waiting", pending = 1)))
        assertTrue(fired.isEmpty())
    }

    // --- snapshot round-trip (the dedup key) ------------------------------

    @Test
    fun `buildSnapshot captures status and pending count per id`() {
        val snapshot =
            buildSnapshot(listOf(session("a", "running", pending = 1), session("b", "idle")))
        assertEquals(SessionSnapshot("running", 1), snapshot["a"])
        assertEquals(SessionSnapshot("idle", 0), snapshot["b"])
    }

    @Test
    fun `re-running the diff against the saved snapshot yields nothing`() {
        // Simulates run N notifying, persisting, then run N+1 diffing the same
        // list against that persisted snapshot — the transition must not repeat.
        val current = listOf(session("a", "idle"), session("b", "waiting", pending = 1))
        val afterNotify = buildSnapshot(current)
        assertTrue(detectIdleTransitions(afterNotify, current).isEmpty())
        assertTrue(detectNewElicitations(afterNotify, current).isEmpty())
    }

    // --- merge: off-window running sessions survive (Blocking 1) -----------

    @Test
    fun `merge carries forward a prior session absent from the current poll`() {
        // "a" was running last poll but scrolled off the top-window this poll.
        // Its prior running status must survive in the merged snapshot.
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val current = listOf(session("b", "idle"))
        val merged = mergeSnapshot(previous, current)
        assertEquals(SessionSnapshot("running", 0), merged["a"])
        assertEquals(SessionSnapshot("idle", 0), merged["b"])
    }

    @Test
    fun `off-window running session still fires an idle transition when it reappears`() {
        // Poll 1: "a" running, in window. Poll 2: "a" off-window (only "b").
        // Poll 3: "a" reappears idle. The finish must STILL fire — which only
        // works if the merge kept "a"=running through poll 2.
        var snapshot = buildSnapshot(listOf(session("a", "running"), session("b", "idle")))
        // Poll 2 — "a" absent from the window.
        val poll2 = listOf(session("b", "idle"))
        assertTrue(detectIdleTransitions(snapshot, poll2).isEmpty()) // "a" not seen terminal yet
        snapshot = mergeSnapshot(snapshot, poll2)
        assertEquals("running", snapshot["a"]?.status) // survived the off-window poll
        // Poll 3 — "a" reappears, now idle.
        val poll3 = listOf(session("a", "idle"), session("b", "idle"))
        assertEquals(listOf("a"), detectIdleTransitions(snapshot, poll3).map { it.id })
    }

    @Test
    fun `empty poll does not drop a prior running entry`() {
        // A poll whose data is legitimately empty (or all-invalid) must not wipe
        // the prior snapshot — otherwise an off-window finish is lost forever.
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val merged = mergeSnapshot(previous, emptyList())
        assertEquals(previous, merged)
    }

    @Test
    fun `merge caps size, keeping current window and mid-flight priors`() {
        // Current window (idle) always kept; a running prior is carried even when
        // the cap forces settled priors to be dropped.
        val previous =
            buildMap {
                put("run", SessionSnapshot("running", 0)) // mid-flight — must survive
                // settled priors — droppable when the cap is tight
                for (i in 0 until 10) put("old$i", SessionSnapshot("idle", 0))
            }
        val current = listOf(session("cur", "idle"))
        val merged = mergeSnapshot(previous, current, cap = 3)
        assertEquals(3, merged.size)
        assertTrue("current window entry kept", merged.containsKey("cur"))
        assertTrue("mid-flight running prior kept over settled", merged.containsKey("run"))
    }

    // --- ordering: crash between notify and save re-fires (Blocking 2) -----

    @Test
    fun `a finish missed by a save-skipping crash still fires on the next run`() {
        // doWork detects -> notifies -> saves. If the process dies after
        // notifying but before saving, the snapshot is NOT advanced, so the next
        // run diffs the SAME prior and re-detects the transition (a duplicate,
        // never a silent miss). Save-first would instead lose it permanently.
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val current = listOf(session("a", "idle"))
        // Run 1 detects the transition (would notify)...
        assertEquals(listOf("a"), detectIdleTransitions(previous, current).map { it.id })
        // ...then the process dies before save, so `previous` is unchanged.
        // Run 2 (same prior, same list) still detects it.
        assertEquals(listOf("a"), detectIdleTransitions(previous, current).map { it.id })
    }

    // --- cookie extraction ------------------------------------------------

    @Test
    fun `extractCookieValue pulls the named cookie`() {
        val header = "__Host-ap_session=abc.def.ghi; other=1"
        assertEquals("abc.def.ghi", extractCookieValue(header, "__Host-ap_session"))
    }

    @Test
    fun `extractCookieValue returns null when absent or empty`() {
        assertNull(extractCookieValue("other=1", "__Host-ap_session"))
        assertNull(extractCookieValue(null, "ap_session"))
        assertNull(extractCookieValue("ap_session=", "ap_session"))
    }

    @Test
    fun `sessionCookieName uses the Host prefix on https only`() {
        assertEquals("__Host-ap_session", sessionCookieName(secure = true))
        assertEquals("ap_session", sessionCookieName(secure = false))
    }

    @Test
    fun `sessionDisplayLabel falls back to a generic label`() {
        assertEquals("My session", sessionDisplayLabel("My session"))
        assertEquals("New session", sessionDisplayLabel(null))
        assertEquals("New session", sessionDisplayLabel("   "))
    }

    // --- stable per-session notification ids ------------------------------

    @Test
    fun `notificationIdFor is stable for the same session id`() {
        // Same id across worker runs / manager instances must map to the same
        // notification id, so a re-fire updates rather than duplicates.
        assertEquals(notificationIdFor("conv_abc123"), notificationIdFor("conv_abc123"))
    }

    @Test
    fun `notificationIdFor differs across distinct session ids`() {
        // Distinct sessions must not collide, or a later finish would silently
        // replace an earlier, still-undismissed notification.
        val ids = listOf("conv_a", "conv_b", "conv_c", "ecaf7591", "refactor-auth", "42")
        val mapped = ids.map { notificationIdFor(it) }
        assertEquals(ids.size, mapped.toSet().size)
    }

    @Test
    fun `notificationIdFor never collides with the badge id`() {
        // Must stay strictly above the reserved badge id (1) for every input,
        // including ones whose raw hash would otherwise land low or negative.
        val samples =
            listOf("", "1", "a", "conv_1", " ", "z".repeat(200)) +
                (0 until 1000).map { "conv_$it" }
        for (id in samples) {
            val n = notificationIdFor(id)
            assertTrue(
                "id for '$id' = $n must be >= $MIN_SESSION_NOTIFICATION_ID",
                n >= MIN_SESSION_NOTIFICATION_ID,
            )
        }
    }
}
