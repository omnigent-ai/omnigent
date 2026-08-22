package ai.omnigent.android

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** SharedPreferences round-trip for the persisted poll snapshot. */
@RunWith(RobolectricTestRunner::class)
class SessionSnapshotStoreTest {
    private lateinit var store: SessionSnapshotStore

    @Before
    fun setUp() {
        store = SessionSnapshotStore(ApplicationProvider.getApplicationContext<Application>())
    }

    @Test
    fun `load is empty before anything is saved`() {
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `save then load round-trips the snapshot`() {
        val snapshot =
            mapOf(
                "conv_a" to SessionSnapshot("running", 0),
                "conv_b" to SessionSnapshot("waiting", 3),
            )
        store.save(snapshot)
        assertEquals(snapshot, store.load())
    }

    @Test
    fun `clear drops the snapshot so the next load seeds fresh`() {
        store.save(mapOf("conv_a" to SessionSnapshot("idle", 1)))
        store.clear()
        assertTrue(store.load().isEmpty())
    }

    @Test
    fun `save overwrites rather than merges`() {
        store.save(mapOf("conv_a" to SessionSnapshot("running", 0)))
        store.save(mapOf("conv_b" to SessionSnapshot("idle", 0)))
        assertEquals(setOf("conv_b"), store.load().keys)
    }
}
