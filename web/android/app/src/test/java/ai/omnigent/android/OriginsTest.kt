package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OriginsTest {
    @Test
    fun `canonicalizes case and default ports`() {
        assertEquals("https://example.com", originOf("HTTPS://Example.COM:443/some/path"))
        assertEquals("http://example.com", originOf("http://example.com:80"))
        assertEquals("https://example.com:8443", originOf("https://example.com:8443"))
    }

    @Test
    fun `canonical origin and default port helpers share normalization`() {
        assertEquals("https://example.com", canonicalOrigin("HTTPS", "Example.COM", 443))
        assertEquals("http://[::1]:8000", canonicalOrigin("http", "::1", 8000))
        assertTrue(isDefaultPort("HTTPS", 443))
        assertTrue(isDefaultPort("http", 80))
        assertFalse(isDefaultPort("https", 8443))
    }

    @Test
    fun `rebrackets ipv6 hosts`() {
        assertEquals("http://[::1]:8000", originOf("http://[::1]:8000"))
        assertEquals("https://[2001:db8::1]", originOf("https://[2001:db8::1]"))
    }

    @Test
    fun `null for scheme-less or host-less input`() {
        assertNull(originOf("example.com"))
        assertNull(originOf(null))
        assertNull(originOf("https://"))
    }
}
