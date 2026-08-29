package ai.omnigent.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/** SharedPreferences round-trip for the persisted poll snapshot. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SessionSnapshotStoreTest {
    private lateinit var store: SessionSnapshotStore

    @Before
    fun setUp() {
        store = SessionSnapshotStore(ApplicationProvider.getApplicationContext<Application>())
    }

    /** Persist via the only write path — the CAS — against the live generation. */
    private fun save(snapshot: Map<String, SessionSnapshot>) {
        val identity = store.lastAccountIdentity() ?: "digest-a"
        store.bindAccount(identity) {}
        assertTrue(store.saveIfCurrentAccount(snapshot, store.generation(), identity))
    }

    @Test
    fun `load is empty before anything is saved`() {
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `save then load round-trips the snapshot, including the held flag`() {
        val snapshot =
            mapOf(
                "conv_a" to SessionSnapshot("running", 0),
                "conv_b" to SessionSnapshot("waiting", 3),
                "conv_held" to SessionSnapshot("idle", 0, held = true),
                "conv_old" to SessionSnapshot("running", 0, offWindowPolls = 3),
            )
        save(snapshot)
        assertEquals(snapshot, store.load())
    }

    @Test
    fun `unbinding drops the snapshot so the next load seeds fresh`() {
        save(mapOf("conv_a" to SessionSnapshot("idle", 1)))
        store.unbindAccount {}
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `unbinding bumps the generation so an in-flight poll can detect it`() {
        // The worker captures generation() before its fetch and aborts when it
        // changed after — this is the same-server account-switch guard.
        store.bindAccount("digest-a") {}
        val before = store.generation()
        store.unbindAccount {}
        assertEquals(before + 1, store.generation())
        store.bindAccount("digest-b") {}
        val rebound = store.generation()
        store.unbindAccount {}
        assertEquals(rebound + 1, store.generation())
    }

    @Test
    fun `save does not change the generation`() {
        store.bindAccount("digest-a") {}
        val before = store.generation()
        save(mapOf("conv_a" to SessionSnapshot("running", 0)))
        assertEquals(before, store.generation())
    }

    @Test
    fun `account binding atomically clears the snapshot and advances generation`() {
        assertNull(store.lastAccountIdentity())
        store.bindAccount("digest-a") {}
        assertEquals("digest-a", store.lastAccountIdentity())
        save(mapOf("conv_a" to SessionSnapshot("running", 0)))
        val before = store.generation()

        assertTrue(store.bindAccount("digest-b") {})

        assertEquals("digest-b", store.lastAccountIdentity())
        assertTrue(store.load().isEmpty())
        assertEquals(before + 1, store.generation())
    }

    @Test
    fun `binding the same account preserves its baseline`() {
        store.bindAccount("digest-a") {}
        save(mapOf("conv_a" to SessionSnapshot("running", 0)))
        val before = store.generation()

        assertFalse(store.bindAccount("digest-a") {})

        assertEquals(mapOf("conv_a" to SessionSnapshot("running", 0)), store.load())
        assertEquals(before, store.generation())
    }

    @Test
    fun `unbinding removes identity and snapshot together`() {
        store.bindAccount("digest-a") {}
        save(mapOf("conv_a" to SessionSnapshot("running", 0)))

        store.unbindAccount {}

        assertNull(store.lastAccountIdentity())
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `saveIfCurrentAccount persists while generation and identity are unchanged`() {
        val snapshot = mapOf("conv_a" to SessionSnapshot("running", 0))
        store.bindAccount("digest-a") {}
        val boundGeneration = store.generation()
        assertTrue(store.saveIfCurrentAccount(snapshot, boundGeneration, "digest-a"))
        assertEquals(snapshot, store.load())
    }

    @Test
    fun `saveIfCurrentAccount refuses a stale generation and leaves the store untouched`() {
        // The worker captured its generation, then a new account bind bumped it:
        // the CAS must lose, or the worker's save would overwrite the new
        // account's cleared baseline and the next poll would diff B against A.
        store.bindAccount("digest-a") {}
        val staleGeneration = store.generation()
        store.bindAccount("digest-b") {}
        assertFalse(
            store.saveIfCurrentAccount(
                mapOf("conv_a" to SessionSnapshot("running", 0)),
                staleGeneration,
                "digest-a",
            ),
        )
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `saveIfCurrentAccount succeeds against the new account generation`() {
        store.bindAccount("digest-b") {}
        val snapshot = mapOf("conv_b" to SessionSnapshot("idle", 1))
        assertTrue(store.saveIfCurrentAccount(snapshot, store.generation(), "digest-b"))
        assertEquals(snapshot, store.load())
    }

    @Test
    fun `save refuses to seed a snapshot without a bound identity`() {
        assertFalse(
            store.saveIfCurrentAccount(
                mapOf("conv_a" to SessionSnapshot("running", 0)),
                store.generation(),
                "digest-a",
            ),
        )
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `notification guard excludes an account switch until cancellation finishes`() {
        store.bindAccount("digest-a") {}
        val generation = store.generation()
        val postEntered = CountDownLatch(1)
        val releasePost = CountDownLatch(1)
        val switchFinished = CountDownLatch(1)
        val post =
            thread {
                store.runIfCurrentAccount(generation, "digest-a") {
                    postEntered.countDown()
                    releasePost.await()
                }
            }
        assertTrue(postEntered.await(1, TimeUnit.SECONDS))

        val accountSwitch =
            thread {
                store.bindAccount("digest-b") {}
                switchFinished.countDown()
            }
        assertFalse(switchFinished.await(100, TimeUnit.MILLISECONDS))

        releasePost.countDown()
        post.join(1_000)
        accountSwitch.join(1_000)
        assertTrue(switchFinished.await(1, TimeUnit.SECONDS))
        assertFalse(store.runIfCurrentAccount(generation, "digest-a") {})
    }

    @Test
    fun `save overwrites rather than merges`() {
        save(mapOf("conv_a" to SessionSnapshot("running", 0)))
        save(mapOf("conv_b" to SessionSnapshot("idle", 0)))
        assertEquals(setOf("conv_b"), store.load().keys)
    }

    @Test
    fun `load quarantines poisoned invalid-id entries`() {
        // A blob written by an older build whose parser produced the literal
        // "null" id (or a blank one) must not resurrect those entries — held
        // at cap priority they could evict a genuine mid-flight session.
        save(
            mapOf(
                "null" to SessionSnapshot("running", 0),
                "" to SessionSnapshot("running", 0),
                " " to SessionSnapshot("running", 0),
                "conv_ok" to SessionSnapshot("running", 0),
            ),
        )
        assertEquals(setOf("conv_ok"), store.load().keys)
    }
}
