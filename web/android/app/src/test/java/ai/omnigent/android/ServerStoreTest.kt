package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.os.Bundle
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowRestrictionsManager

/** Precedence between a user's stored server and organization presets. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ServerStoreTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    private fun storeWithPresets(vararg urls: String) =
        ServerStore(context, ManagedConfig(urls.toList()))

    @Test
    fun `unmanaged and unconfigured falls back to the debug default`() {
        val store = storeWithPresets()

        assertFalse(store.hasServer())
        assertEquals("http://10.0.2.2:8000", store.currentServerUrl())
    }

    @Test
    fun `presets are offered but never connected on the user's behalf`() {
        val store = storeWithPresets("https://managed.example.com")

        // The connect screen still owns the decision.
        assertFalse(store.hasServer())
        assertEquals(listOf("https://managed.example.com"), store.offeredServers())
        // Nothing was written, so a later policy change is picked up as-is.
        assertEquals(emptyList<String>(), store.recentServers())
        assertEquals("http://10.0.2.2:8000", store.currentServerUrl())
    }

    @Test
    fun `several presets are all offered`() {
        val store = storeWithPresets("https://a.example.com", "https://b.example.com")

        assertFalse(store.hasServer())
        assertEquals(
            listOf("https://a.example.com", "https://b.example.com"),
            store.offeredServers(),
        )
    }

    @Test
    fun `connecting to a preset is what makes it current`() {
        val store = storeWithPresets("https://managed.example.com")

        store.connect("https://managed.example.com")

        assertTrue(store.hasServer())
        assertEquals("https://managed.example.com", store.currentServerUrl())
    }

    @Test
    fun `offered servers lead with presets and drop recents they cover`() {
        storeWithPresets().connect("https://mine.example.com")
        // Same origin as the preset below, differently spelled.
        storeWithPresets().connect("https://managed.example.com:443")

        val store = storeWithPresets("https://managed.example.com")

        assertEquals(
            listOf("https://managed.example.com", "https://mine.example.com"),
            store.offeredServers(),
        )
    }

    @Test
    fun `presets are read from managed configuration by default`() {
        setApplicationRestrictions(
            Bundle().apply {
                putString(ManagedConfig.KEY_SERVER_URLS, "https://policy.example.com")
            },
        )

        assertEquals(listOf("https://policy.example.com"), ServerStore(context).managed.serverUrls)
    }

    private fun setApplicationRestrictions(restrictions: Bundle) {
        val manager = context.getSystemService(RestrictionsManager::class.java)
        Shadow.extract<ShadowRestrictionsManager>(manager).setApplicationRestrictions(restrictions)
    }
}
