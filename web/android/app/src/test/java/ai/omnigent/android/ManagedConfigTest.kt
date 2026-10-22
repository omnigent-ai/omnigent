package ai.omnigent.android

import android.os.Bundle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Parsing of the `serverUrls` managed-configuration value. Robolectric supplies
 * the real [Bundle] and `Uri` parsing that [ManagedConfig] delegates to.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ManagedConfigTest {
    private fun configOf(value: String?): ManagedConfig =
        ManagedConfig.from(
            Bundle().apply { value?.let { putString(ManagedConfig.KEY_SERVER_URLS, it) } },
        )

    @Test
    fun `no restrictions bundle means unmanaged`() {
        assertEquals(emptyList<String>(), ManagedConfig.from(null as Bundle?).serverUrls)
    }

    @Test
    fun `missing key means unmanaged`() {
        assertEquals(emptyList<String>(), configOf(null).serverUrls)
    }

    @Test
    fun `blank value yields no presets`() {
        assertEquals(emptyList<String>(), configOf("   \n  ").serverUrls)
    }

    @Test
    fun `single url is the only preset`() {
        assertEquals(
            listOf("https://omnigent.example.com"),
            configOf("https://omnigent.example.com").serverUrls,
        )
    }

    @Test
    fun `newlines commas and surrounding space all delimit, order preserved`() {
        assertEquals(
            listOf("https://a.example.com", "https://b.example.com", "https://c.example.com"),
            configOf("https://a.example.com,\n https://b.example.com\r\nhttps://c.example.com")
                .serverUrls,
        )
    }

    @Test
    fun `scheme is defaulted and trailing slash trimmed`() {
        assertEquals(
            listOf("https://omnigent.example.com"),
            configOf("omnigent.example.com/").serverUrls,
        )
    }

    @Test
    fun `unparseable entries are dropped without losing the rest`() {
        assertEquals(
            listOf("https://good.example.com"),
            configOf("ftp://nope.example.com,https://good.example.com,https://").serverUrls,
        )
    }

    @Test
    fun `same-origin entries collapse to one preset`() {
        assertEquals(
            listOf("https://omnigent.example.com"),
            configOf("https://omnigent.example.com,https://omnigent.example.com:443").serverUrls,
        )
    }

    @Test
    fun `preset count is capped`() {
        val many = (1..20).joinToString(",") { "https://host$it.example.com" }

        assertEquals(8, configOf(many).serverUrls.size)
    }

    @Test
    fun `includes matches on origin, not on the literal string`() {
        val config = configOf("https://omnigent.example.com")

        assertTrue(config.includes("https://omnigent.example.com:443"))
        assertTrue(config.includes("https://OMNIGENT.example.com"))
        assertFalse(config.includes("https://other.example.com"))
        assertFalse(config.includes("not a url"))
    }
}
