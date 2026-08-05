package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ServerStoreTest {
    @Test
    fun `known servers contains the current recent only once`() {
        val store = testStore()
        store.connect("https://first.example")
        store.connect("https://current.example")

        assertEquals(
            listOf("https://current.example", "https://first.example"),
            store.knownServers(),
        )
    }
}
