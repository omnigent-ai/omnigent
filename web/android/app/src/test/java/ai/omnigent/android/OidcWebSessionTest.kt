package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OidcWebSessionTest {
    @Test
    fun `auth routes are matched under the server's mount on its origin`() {
        val mounted = "https://omni.example/omnigent/"
        assertEquals(
            OidcAuthRoute.LOGIN,
            OidcAuthRoute.of("https://omni.example/omnigent/auth/login?return_to=%2F", mounted),
        )
        assertEquals(
            OidcAuthRoute.LOGOUT,
            OidcAuthRoute.of("https://omni.example/omnigent/auth/logout", mounted),
        )
        assertEquals(
            OidcAuthRoute.LOGIN,
            OidcAuthRoute.of("https://OMNI.example:443/auth/login", "https://omni.example"),
        )
        listOf(
            "https://omni.example/auth/login",
            "https://omni.example/omnigent/auth/login/",
            "https://omni.example/omnigent/auth/loginx",
            "https://omni.example/omnigent/auth/callback",
            "https://omni.example:8443/omnigent/auth/login",
            "http://omni.example/omnigent/auth/login",
            "https://idp.example/omnigent/auth/login",
            "about:blank",
            null,
        ).forEach { url -> assertNull(url, OidcAuthRoute.of(url, mounted)) }
    }

    @Test
    fun `app pages are under the mount and never auth routes`() {
        assertTrue(
            OidcWebSession.isPage(
                "https://omni.example/omnigent",
                "https://omni.example/omnigent/",
            ),
        )
        assertTrue(
            OidcWebSession.isPage(
                "https://omni.example/omnigent/c/1?x=1",
                "https://omni.example/omnigent",
            ),
        )
        assertTrue(OidcWebSession.isPage("https://omni.example/anything", "https://omni.example"))
        assertFalse(
            OidcWebSession.isPage(
                "https://omni.example/omnigentx",
                "https://omni.example/omnigent",
            ),
        )
        assertFalse(
            OidcWebSession.isPage("https://omni.example/auth/login", "https://omni.example"),
        )
        assertFalse(OidcWebSession.isPage("https://other.example/c/1", "https://omni.example"))
        assertFalse(OidcWebSession.isPage(null, "https://omni.example"))
    }

    @Test
    fun `the session cookie is the server's own host-only HttpOnly cookie`() {
        val session = OidcSessionToken("jwt.payload.sig", 1_700_000_000_000L)

        assertEquals(
            "__Host-ap_session=jwt.payload.sig; Path=/; HttpOnly; SameSite=Lax; Secure; " +
                "Expires=Tue, 14 Nov 2023 22:13:20 GMT",
            OidcWebSession.sessionCookie(
                "__Host-ap_session",
                session,
                "https://omni.example/omnigent",
            ),
        )
        assertEquals(
            "ap_session=t; Path=/; HttpOnly; SameSite=Lax",
            OidcWebSession.sessionCookie(
                "ap_session",
                OidcSessionToken("t", null),
                "http://10.0.2.2:6891",
            ),
        )
        assertEquals(
            "__Host-ap_session=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure",
            OidcWebSession.deletionCookie("__Host-ap_session", "https://omni.example"),
        )
    }

    @Test
    fun `the session cookie is read from the header the web view would send`() {
        assertEquals(
            "b",
            OidcWebSession.cookieValue("ap_session2=a; ap_session=b; theme=dark", "ap_session"),
        )
        assertEquals("a=b", OidcWebSession.cookieValue("ap_session=a=b", "ap_session"))
        assertNull(OidcWebSession.cookieValue("ap_session=", "ap_session"))
        assertNull(OidcWebSession.cookieValue("other=1", "ap_session"))
        assertNull(OidcWebSession.cookieValue(null, "ap_session"))
    }

    @Test
    fun `a renewal asked for again within 15 s is a rejected session`() {
        val guard = OidcRenewalGuard()

        assertTrue(guard.shouldRenew(now = 1_000, renewalPending = false))
        assertTrue(guard.shouldRenew(now = 2_000, renewalPending = true))
        assertFalse(guard.shouldRenew(now = 14_000, renewalPending = false))
        assertTrue(guard.shouldRenew(now = 18_000, renewalPending = false))
    }

    @Test
    fun `the web view's cookie info gives the session cookie's expiry`() {
        val info =
            listOf(
                "theme=dark; path=/",
                "ap_session=t; domain=omni.example; path=/; expires=Tue, 14 Nov 2023 22:13:20 GMT; httponly",
            )

        assertEquals(1_700_000_000_000L, OidcWebSession.cookieExpiry(info, "ap_session"))
        assertNull(OidcWebSession.cookieExpiry(listOf("ap_session=t; path=/"), "ap_session"))
        assertNull(OidcWebSession.cookieExpiry(listOf("ap_session=t; expires=soon"), "ap_session"))
        assertNull(OidcWebSession.cookieExpiry(info, "other"))
    }

    @Test
    fun `renewal starts a fifth of the lifetime early, at most a minute`() {
        assertEquals(3_540_000L, OidcWebSession.renewalDelay(expiresAt = 3_600_000L, now = 0L))
        assertEquals(80_000L, OidcWebSession.renewalDelay(expiresAt = 100_000L, now = 0L))
        assertEquals(0L, OidcWebSession.renewalDelay(expiresAt = 5_000L, now = 5_000L))
        assertEquals(0L, OidcWebSession.renewalDelay(expiresAt = 1_000L, now = 5_000L))
    }

    @Test
    fun `a lost grant explains a later prompt and sign-out reports what it finished`() {
        val rejected = OidcSignInException.GrantRejected("h")
        assertEquals(rejected, OidcWebSession.rememberedRenewalCause(rejected))
        assertNull(OidcWebSession.rememberedRenewalCause(OidcSignInException.Network("h")))
        assertEquals(
            rejected,
            OidcWebSession.reauthenticationCause(OidcSignInException.NoStoredGrant("h"), rejected),
        )
        val network = OidcSignInException.Network("h")
        assertEquals(network, OidcWebSession.reauthenticationCause(network, rejected))
        assertEquals(
            "You're signed out of h.",
            OidcWebSession.signedOutMessage("h", complete = true),
        )
        assertTrue(
            OidcWebSession
                .signedOutMessage(
                    "h",
                    complete = false,
                ).startsWith("Couldn't finish signing out of h"),
        )
    }

    @Test
    fun `prompts explain why the session needs a sign-in`() {
        assertEquals(
            "Sign in to omni.example to continue.",
            OidcWebSession.reauthenticationMessage(
                OidcSignInException.Network("omni.example"),
                "omni.example",
            ),
        )
        assertEquals(
            "Sign in to omni.example to continue.",
            OidcWebSession.reauthenticationMessage(null, "omni.example"),
        )
        assertEquals(
            "Your sign-in to omni.example has expired. Sign in again to continue.",
            OidcWebSession.reauthenticationMessage(
                OidcSignInException.GrantExpired("omni.example"),
                "omni.example",
            ),
        )
        val expired = OidcSignInException.GrantExpired("h")
        assertEquals(expired, OidcWebSession.cancelledSignInCause(expired))
        assertNull(OidcWebSession.cancelledSignInCause(OidcSignInException.NoStoredGrant("h")))
        assertNull(OidcWebSession.cancelledSignInCause(OidcSignInException.Network("h")))
        assertTrue(OidcWebSession.isNetworkFailure(OidcSignInException.Network("h")))
        assertFalse(OidcWebSession.isNetworkFailure(expired))
    }
}
