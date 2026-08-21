package ai.omnigent.android

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class NativeAuthTest {
    @Test
    fun `valid code callback parses`() {
        val grant =
            NativeAuth.parseCodeCallback(
                callback("state=abc12345&code=one-time_Code&exchange=tab"),
                ORIGIN,
            )

        assertNotNull(grant)
        assertEquals("abc12345", grant!!.state)
        assertEquals("one-time_Code", grant.code)
        assertEquals(NativeAuth.EXCHANGE_TAB, grant.exchange)
    }

    @Test
    fun `code callback with unknown exchange transport is rejected`() {
        assertNull(
            NativeAuth.parseCodeCallback(
                callback("state=abc12345&code=c0de&exchange=carrier-pigeon"),
                ORIGIN,
            ),
        )
    }

    @Test
    fun `code callback never parses as a token callback and vice versa`() {
        val codeUri = callback("state=abc12345&code=c0de&exchange=post")
        val tokenUri = callback("state=abc12345&token_type=session&token=$JWT")

        assertNull(NativeAuth.parseTokenCallback(codeUri, ORIGIN))
        assertNull(NativeAuth.parseCodeCallback(tokenUri, ORIGIN))
    }

    @Test
    fun `valid session and bearer token callbacks parse`() {
        val session =
            NativeAuth.parseTokenCallback(
                callback("state=abc12345&token_type=session&token=$JWT"),
                ORIGIN,
            )
        val bearer =
            NativeAuth.parseTokenCallback(
                callback("state=abc12345&token_type=bearer&token=tok-123"),
                ORIGIN,
            )

        assertEquals(NativeAuth.TOKEN_TYPE_SESSION, session!!.tokenType)
        assertEquals(JWT, session.token)
        assertEquals(NativeAuth.TOKEN_TYPE_BEARER, bearer!!.tokenType)
    }

    @Test
    fun `wrong scheme host port or path is rejected`() {
        val query = "state=abc12345&token_type=session&token=$JWT"
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("http://server.example.com${NativeAuth.CALLBACK_PATH}?$query"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("https://evil.example.com${NativeAuth.CALLBACK_PATH}?$query"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("https://server.example.com:8443${NativeAuth.CALLBACK_PATH}?$query"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("$ORIGIN/auth/other-callback?$query"),
                ORIGIN,
            ),
        )
        assertNull(NativeAuth.parseTokenCallback(null, ORIGIN))
        assertNull(NativeAuth.parseCodeCallback(null, ORIGIN))
    }

    @Test
    fun `missing or empty fields are rejected`() {
        assertNull(
            NativeAuth.parseTokenCallback(
                callback("token_type=session&token=$JWT"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                callback("state=&token_type=session&token=$JWT"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                callback("state=abc12345&token=$JWT"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                callback("state=abc12345&token_type=session&token="),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseCodeCallback(
                callback("state=abc12345&exchange=tab"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseCodeCallback(
                callback("state=abc12345&code=&exchange=tab"),
                ORIGIN,
            ),
        )
        assertNull(
            NativeAuth.parseCodeCallback(
                callback("state=abc12345&code=c0de"),
                ORIGIN,
            ),
        )
    }

    @Test
    fun `server error report parses as neither shape`() {
        val uri = callback("state=abc12345&error=no_token")
        assertNull(NativeAuth.parseCodeCallback(uri, ORIGIN))
        assertNull(NativeAuth.parseTokenCallback(uri, ORIGIN))
    }

    @Test
    fun `unknown token type is rejected`() {
        assertNull(
            NativeAuth.parseTokenCallback(
                callback("state=abc12345&token_type=refresh&token=$JWT"),
                ORIGIN,
            ),
        )
    }

    @Test
    fun `token with header-breaking characters is rejected`() {
        for (token in listOf("a b", "a;b", "a\"b", "a\nb", "a,b", "日本")) {
            assertNull(
                NativeAuth.parseTokenCallback(
                    Uri
                        .parse("$ORIGIN${NativeAuth.CALLBACK_PATH}")
                        .buildUpon()
                        .appendQueryParameter("state", "abc12345")
                        .appendQueryParameter("token_type", "bearer")
                        .appendQueryParameter("token", token)
                        .build(),
                    ORIGIN,
                ),
            )
        }
    }

    @Test
    fun `token68 alphabet is accepted`() {
        assertTrue(NativeAuth.isTokenSafe(JWT))
        assertTrue(NativeAuth.isTokenSafe("dapi+abc/def=="))
        assertFalse(NativeAuth.isTokenSafe(""))
    }

    @Test
    fun `code challenge derivation matches the RFC 7636 vector`() {
        assertEquals(
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            NativeAuth.deriveCodeChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
        )
    }

    @Test
    fun `completion and exchange urls target the pinned origin`() {
        val completion =
            NativeAuth.completionUrl(
                "https://x.databricksapps.com",
                "st4te-abc",
                "chall",
                PACKAGE,
            )
        assertEquals(
            "https://x.databricksapps.com/auth/native-complete" +
                "?state=st4te-abc&code_challenge=chall&client_package=$PACKAGE",
            completion.toString(),
        )

        val exchange =
            NativeAuth.exchangeUrl(
                "https://x.databricksapps.com",
                NativeAuth.CodeGrant("st4te-abc", "c0de", NativeAuth.EXCHANGE_TAB),
                "v3rifier",
            )
        assertEquals(
            "https://x.databricksapps.com/auth/native-exchange" +
                "?code=c0de&state=st4te-abc&code_verifier=v3rifier",
            exchange.toString(),
        )
    }

    @Test
    fun `callback binding uses the HTTPS origin and fixed path`() {
        assertEquals(
            NativeAuth.Callback("server.example.com", NativeAuth.CALLBACK_PATH),
            NativeAuth.callback(ORIGIN),
        )
        assertNull(NativeAuth.callback("http://server.example.com"))
        assertNull(NativeAuth.callback("not an origin"))
    }

    @Test
    fun `no activity claims the callback as a VIEW intent`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val intent = Intent(Intent.ACTION_VIEW, callback("state=x&code=c&exchange=tab"))

        val handlers =
            context.packageManager
                .queryIntentActivities(intent, 0)
                .filter { it.activityInfo?.packageName == context.packageName }

        assertTrue(handlers.isEmpty())
    }

    private fun callback(query: String): Uri =
        Uri.parse("$ORIGIN${NativeAuth.CALLBACK_PATH}?$query")

    private companion object {
        const val ORIGIN = "https://server.example.com"
        const val PACKAGE = "ai.omnigent.android"
        const val JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.c2ln"
    }
}
