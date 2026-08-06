package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OriginsTest {
    @Test
    fun `origin strips credentials and canonicalizes case and ports`() {
        val cases =
            mapOf(
                "https://pin@evil.com/x" to "https://evil.com",
                "https://good.com:443/x" to "https://good.com",
                "http://good.com:80/x" to "http://good.com",
                "HTTPS://GOOD.com/x" to "https://good.com",
                "https://a:b@good.com:8443" to "https://good.com:8443",
                "https://good.com./x" to "https://good.com.",
            )

        cases.forEach { (url, expected) -> assertEquals(url, expected, originOf(url)) }
        assertNull(originOf("https:foo"))
        // WHATWG: a non-numeric port is a parse failure, not a strippable extra.
        assertNull(originOf("https://good.com:notaport"))
    }

    @Test
    fun `bracketed ipv6 literals canonicalize with their ports intact`() {
        assertEquals("https://[::1]:8443", originOf("https://[::1]:8443/x"))
        assertEquals("https://[::1]", originOf("https://[::1]:443/x"))
        assertEquals("https://[2001:db8::1]:8443", originOf("https://[2001:DB8::1]:8443/x"))
    }

    @Test
    fun `ipv6 literals collapse to the form the WebView reports`() {
        // Chromium serializes the shortest RFC 5952 form; an uncompressed pin
        // must compare equal to it or a healthy server can never log in.
        assertEquals("https://[::1]:8000", originOf("https://[0:0:0:0:0:0:0:1]:8000/x"))
        assertEquals("https://[2001:db8::1]", originOf("https://[2001:0db8:0:0:0:0:0:1]/x"))
        assertEquals(
            "https://[2001:db8:0:1:1:1:1:1]",
            originOf("https://[2001:db8:0:1:1:1:1:1]/x"),
        )
        assertEquals("https://[::ffff:7f00:1]", originOf("https://[::ffff:127.0.0.1]/x"))
        assertNull(originOf("https://[not-an-address]/x"))
        assertNull(originOf("https://[::1%25eth0]/x"))
    }

    @Test
    fun `ipv4 shorthand literals expand like Chromium`() {
        assertEquals("https://127.0.0.1", originOf("https://127.1/x"))
        assertEquals("https://127.0.0.1", originOf("https://0x7f.0.0.1/x"))
        assertEquals("https://127.0.0.1:8000", originOf("https://2130706433:8000/x"))
        assertEquals("https://127.0.0.1", originOf("https://127.0.0.1/x"))
        // WHATWG: a host ending in a number must fully parse as IPv4.
        assertNull(originOf("https://1.2.3.4.5/x"))
        assertNull(originOf("https://127.0.0.999/x"))
        assertNull(originOf("https://foo.0x1/x"))
        // An all-digit last label forces the IPv4 path even when the number
        // parse fails — Chromium treats these as invalid URLs, not domains.
        assertNull(originOf("https://foo.09/x"))
        assertNull(originOf("https://foo.1234567890123/x"))
        // Only ONE trailing empty label is ignored: `1.2.3.` is the address
        // 1.2.0.3, while `1.2.3..` is a (never resolvable) Chromium domain —
        // it must not collapse onto that same pin.
        assertEquals("https://1.2.3.4", originOf("https://1.2.3.4./x"))
        assertEquals("https://1.2.0.3", originOf("https://1.2.3./x"))
        assertNotEquals("https://1.2.0.3", originOf("https://1.2.3../x"))
        // WHATWG numbers carry no sign — "+1" is a Chromium domain, and must
        // not pin as the address 0.0.0.1.
        assertNotEquals("https://0.0.0.1", originOf("https://+1/x"))
    }

    @Test
    fun `ports beyond the 16-bit range are rejected`() {
        assertNull(originOf("https://example.com:65536/x"))
        assertNull(normalizeServerUrl("https://example.com:65536"))
        assertEquals("https://example.com:65535", originOf("https://example.com:65535/x"))
        // Uri.parsePort overflows Int-sized digit runs to -1; the written port
        // must reject the URL rather than silently vanish.
        assertNull(originOf("https://example.com:99999999999/x"))
        // A bare trailing colon is an EMPTY port — valid per WHATWG.
        assertEquals("https://example.com", originOf("https://example.com:/x"))
    }

    @Test
    fun `over-long numeric labels stay on the IPv4 path and are rejected`() {
        // WHATWG numbers are arbitrary precision: a 13-hex-digit label is a
        // valid number that fails the 32-bit address range — an invalid URL in
        // Chromium, never a domain.
        assertNull(originOf("https://foo.0x1234567890123/x"))
        assertNull(originOf("https://0xdeadbeefcafebabe/x"))
    }

    @Test
    fun `hosts canonicalize like Chromium's UTS-46, not IDNA2003`() {
        // java.net.IDN (IDNA2003) maps faß.de to fass.de — a different
        // registrable domain than the xn--fa-hia.de the WebView loads.
        assertEquals("https://xn--fa-hia.de", originOf("https://faß.de/x"))
        assertEquals(originOf("https://xn--fa-hia.de"), originOf("https://faß.de"))
    }

    @Test
    fun `unicode and punycode hosts have the same origin`() {
        assertEquals(
            originOf("https://xn--r8jz45g.jp"),
            originOf("https://例え.jp"),
        )
    }

    @Test
    fun `server input is normalized or rejected`() {
        assertEquals("https://example.com", normalizeServerUrl("  example.com/  "))
        assertEquals("HTTP://GOOD.com/path", normalizeServerUrl("HTTP://GOOD.com/path/"))
        assertNull(normalizeServerUrl(""))
        assertNull(normalizeServerUrl("ftp://example.com"))
        assertNull(normalizeServerUrl("https://good.com/bad path"))
        assertNull(normalizeServerUrl("https:///missing-host"))
    }

    @Test
    fun `server input the pinned-origin gate cannot canonicalize is rejected`() {
        // Persisting one of these would pin a null origin, and the fail-closed
        // page gate would then silently stop every load — surface the error at
        // connect time instead.
        assertNull(normalizeServerUrl("https://a..b"))
        assertNull(normalizeServerUrl("https://good.com:notaport"))
        assertNull(normalizeServerUrl("https://[not-an-address]"))
        assertNull(normalizeServerUrl("https://1.2.3.4.5"))
    }

    @Test
    fun `http scheme matching is case insensitive and exact`() {
        assertTrue(isHttpScheme("HTTP"))
        assertTrue(isHttpScheme("Https"))
        assertFalse(isHttpScheme("httpfoo"))
        assertFalse(isHttpScheme("mailto"))
        assertFalse(isHttpScheme(null))
    }

    @Test
    fun `front-door proxy authorize url is detected`() {
        // Databricks-Apps-style: every path is intercepted and bounced to the
        // host's IdP with a redirect_uri returning to the pinned origin on the
        // proxy's own callback path.
        assertTrue(
            isProxyAuthUrl(
                "https://idp.example.com/oidc/oauth2/v2.0/authorize" +
                    "?client_id=abc&response_type=code" +
                    "&redirect_uri=https%3A%2F%2Fapp.example.com%2F.auth%2Fcallback",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `own oidc bounce is not a proxy url`() {
        // The app's own IdP bounce uses the server's /auth/callback — that flow
        // must keep going through the system browser, not load inline.
        assertFalse(
            isProxyAuthUrl(
                "https://accounts.google.com/o/oauth2/v2/auth" +
                    "?client_id=abc&redirect_uri=https%3A%2F%2Fapp.example.com%2Fauth%2Fcallback",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `own oidc bounce with trailing slash is not a proxy url`() {
        assertFalse(
            isProxyAuthUrl(
                "https://accounts.google.com/o/oauth2/v2/auth" +
                    "?client_id=abc&redirect_uri=https%3A%2F%2Fapp.example.com%2Fauth%2Fcallback%2F",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `redirect_uri to a foreign origin is not a proxy url`() {
        assertFalse(
            isProxyAuthUrl(
                "https://idp.example.com/authorize" +
                    "?redirect_uri=https%3A%2F%2Fother.example.com%2F.auth%2Fcallback",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `url without redirect_uri is not a proxy url`() {
        assertFalse(isProxyAuthUrl("https://idp.example.com/login", "https://app.example.com"))
    }

    @Test
    fun `null inputs are not a proxy url`() {
        assertFalse(isProxyAuthUrl(null, "https://app.example.com"))
        assertFalse(
            isProxyAuthUrl(
                "https://idp.example.com/authorize" +
                    "?redirect_uri=https%3A%2F%2Fapp.example.com%2F.auth%2Fcallback",
                null,
            ),
        )
    }

    @Test
    fun `opaque uri does not crash the classifier`() {
        // Uri.getQueryParameter throws on opaque (non-hierarchical) URIs.
        assertFalse(isProxyAuthUrl("mailto:someone@example.com", "https://app.example.com"))
    }
}
