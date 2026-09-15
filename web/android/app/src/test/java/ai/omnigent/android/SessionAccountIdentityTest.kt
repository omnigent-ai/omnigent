package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Unit tests for the account-identity marker keying onSessionToken's cleanup.
 * Robolectric because jwtSubject uses `org.json` (an unmocked stub on a plain
 * JVM), matching SessionListParserTest.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SessionAccountIdentityTest {
    private val origin = "https://omnigent.example.com"

    private fun jwt(payload: String): String = TestJwt.forPayload(payload)

    @Test
    fun `re-injecting the same token keeps the same identity`() {
        // A cold start re-handing the SAME session must not read as an account
        // change — that's what stops the unconditional-clear baseline wipe.
        val token = jwt("""{"sub":"user-a"}""")
        assertEquals(sessionAccountIdentity(origin, token), sessionAccountIdentity(origin, token))
    }

    @Test
    fun `a new token for the same account keeps the same identity`() {
        // Re-login mints a new JWT (different iat/exp) for the same `sub`; the
        // baseline must survive that too.
        val first = jwt("""{"sub":"user-a","iat":1}""")
        val second = jwt("""{"sub":"user-a","iat":2}""")
        assertEquals(sessionAccountIdentity(origin, first), sessionAccountIdentity(origin, second))
    }

    @Test
    fun `a different account on the same server changes the identity`() {
        val a = jwt("""{"sub":"user-a"}""")
        val b = jwt("""{"sub":"user-b"}""")
        assertNotEquals(sessionAccountIdentity(origin, a), sessionAccountIdentity(origin, b))
    }

    @Test
    fun `the same account on a different server changes the identity`() {
        val token = jwt("""{"sub":"user-a"}""")
        assertNotEquals(
            sessionAccountIdentity("https://a.example.com", token),
            sessionAccountIdentity("https://b.example.com", token),
        )
    }

    @Test
    fun `an undecodable token falls back to per-token identity`() {
        // Conservative fallback: with no extractable subject, only the exact
        // same token reads as the same account — a different opaque token
        // clears, matching the pre-existing behavior.
        val opaque = "not-a-jwt"
        assertEquals(sessionAccountIdentity(origin, opaque), sessionAccountIdentity(origin, opaque))
        assertNotEquals(
            sessionAccountIdentity(origin, opaque),
            sessionAccountIdentity(origin, "also-not-a-jwt"),
        )
    }

    @Test
    fun `identity never contains the raw subject or token`() {
        val token = jwt("""{"sub":"user-a@example.com"}""")
        val identity = sessionAccountIdentity(origin, token)
        // A SHA-256 hex digest: fixed length, no credential material.
        assertEquals(64, identity.length)
        assertFalse(identity.contains("user-a"))
        assertFalse(identity.contains(token))
    }

    @Test
    fun `identityMismatch fails closed without a bound account`() {
        val a = sessionAccountIdentity(origin, jwt("""{"sub":"user-a"}"""))
        val b = sessionAccountIdentity(origin, jwt("""{"sub":"user-b"}"""))
        // The worker holds A's cookie while B's login already persisted its
        // marker — every such interleaving must abort, regardless of when the
        // async setCookie commits.
        assertEquals(true, identityMismatch(persisted = b, cookieIdentity = a))
        // Same account: proceed.
        assertEquals(false, identityMismatch(persisted = a, cookieIdentity = a))
        // No marker means the cookie has not been bound to the WebView's current
        // account. Acting on it could seed or notify across an accounts-mode
        // logout/login that never passed through onSessionToken.
        assertEquals(true, identityMismatch(persisted = null, cookieIdentity = a))
    }

    @Test
    fun `jwtSubject extracts sub and tolerates malformed input`() {
        assertEquals("user-a", jwtSubject(jwt("""{"sub":"user-a"}""")))
        assertNull(jwtSubject(jwt("""{"iat":1}"""))) // no sub
        assertNull(jwtSubject(jwt("""{"sub":null}"""))) // explicit null
        assertNull(jwtSubject(jwt("""{"sub":""}"""))) // empty
        assertNull(jwtSubject(jwt("not json")))
        assertNull(jwtSubject("two.parts"))
        assertNull(jwtSubject("header.!!!not-base64url!!!.sig"))
        assertNull(jwtSubject(""))
    }
}
