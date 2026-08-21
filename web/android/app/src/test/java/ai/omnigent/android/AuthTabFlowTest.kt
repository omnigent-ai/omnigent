package ai.omnigent.android

import android.net.Uri
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class AuthTabFlowTest {
    private val flow = AuthTabFlow()

    @Test
    fun `begin produces a completion url carrying app state and challenge`() {
        val url = flow.begin(ORIGIN, PACKAGE)

        assertNotNull(url)
        assertTrue(flow.inFlight)
        assertEquals(ORIGIN, "${url!!.scheme}://${url.host}")
        assertEquals("/auth/native-complete", url.path)
        val state = url.getQueryParameter("state")!!
        assertTrue(state.length >= 16)
        assertTrue(state.all { it.isLetterOrDigit() || it == '-' || it == '_' })
        // A 32-byte verifier's S256 challenge is 43 base64url chars.
        assertEquals(43, url.getQueryParameter("code_challenge")!!.length)
        assertEquals(PACKAGE, url.getQueryParameter("client_package"))
    }

    @Test
    fun `states are unique per flow`() {
        val first = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")
        flow.cancel()
        val second = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")

        assertTrue(first != second)
    }

    @Test
    fun `only one flow can be in flight`() {
        assertNotNull(flow.begin(ORIGIN, PACKAGE))
        assertNull(flow.begin(ORIGIN, PACKAGE)) // a redirect storm must not stack tabs
    }

    @Test
    fun `tab exchange grant builds the exchange url with the held verifier`() {
        val begin = flow.begin(ORIGIN, PACKAGE)!!
        val state = begin.getQueryParameter("state")!!
        val challenge = begin.getQueryParameter("code_challenge")!!

        val outcome = flow.handleCallback(codeCallback(state, exchange = "tab"), ORIGIN)

        val launch = outcome as AuthTabFlow.Outcome.LaunchExchangeTab
        assertEquals("/auth/native-exchange", launch.url.path)
        assertEquals("c0de", launch.url.getQueryParameter("code"))
        assertEquals(state, launch.url.getQueryParameter("state"))
        // The tab transport exposes the verifier in this URL; it must
        // still match the challenge committed at begin().
        val verifier = launch.url.getQueryParameter("code_verifier")!!
        assertEquals(challenge, NativeAuth.deriveCodeChallenge(verifier))
        assertTrue(flow.inFlight) // awaiting the leg-2 token callback
    }

    @Test
    fun `post exchange grant hands over the code and verifier`() {
        val begin = flow.begin(ORIGIN, PACKAGE)!!
        val state = begin.getQueryParameter("state")!!
        val challenge = begin.getQueryParameter("code_challenge")!!

        val outcome = flow.handleCallback(codeCallback(state, exchange = "post"), ORIGIN)

        val post = outcome as AuthTabFlow.Outcome.ExchangePost
        assertEquals(ORIGIN, post.origin)
        assertEquals("c0de", post.code)
        assertEquals(state, post.state)
        assertEquals(challenge, NativeAuth.deriveCodeChallenge(post.verifier))
    }

    @Test
    fun `token callback after the tab exchange completes and clears the flow`() {
        val state = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")!!
        flow.handleCallback(codeCallback(state, exchange = "tab"), ORIGIN)

        val outcome = flow.handleCallback(tokenCallback(state), ORIGIN)

        val complete = outcome as AuthTabFlow.Outcome.Complete
        assertEquals("tok", complete.result.token)
        assertFalse(flow.inFlight)
    }

    @Test
    fun `a token callback before any code was issued is rejected`() {
        val state = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")!!

        // Leg 2 can't be skipped: a credential-shaped callback is only
        // meaningful after the code leg ran.
        assertNull(flow.handleCallback(tokenCallback(state), ORIGIN))
        assertTrue(flow.inFlight)
    }

    @Test
    fun `a replayed code callback is rejected once the code was issued`() {
        val state = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")!!
        flow.handleCallback(codeCallback(state, exchange = "tab"), ORIGIN)

        assertNull(flow.handleCallback(codeCallback(state, exchange = "tab"), ORIGIN))
    }

    @Test
    fun `state mismatch is rejected and keeps the flow armed`() {
        flow.begin(ORIGIN, PACKAGE)

        assertNull(flow.handleCallback(codeCallback("attacker-state"), ORIGIN))
        assertNull(flow.handleCallback(tokenCallback("attacker-state"), ORIGIN))
        assertTrue(flow.inFlight) // the real result may still arrive
    }

    @Test
    fun `a result for a previous server never lands on the new one`() {
        val state = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")!!

        // The user switched servers while the tab was open.
        assertNull(flow.handleCallback(codeCallback(state), "https://other.example.com"))
        assertNull(flow.handleCallback(codeCallback(state), null))
    }

    @Test
    fun `unsolicited callback with no pending flow is dropped`() {
        assertNull(flow.handleCallback(codeCallback("any-state-1234"), ORIGIN))
        assertNull(flow.handleCallback(tokenCallback("any-state-1234"), ORIGIN))
    }

    @Test
    fun `cancel abandons the pending flow`() {
        val state = flow.begin(ORIGIN, PACKAGE)!!.getQueryParameter("state")!!
        flow.cancel()

        assertFalse(flow.inFlight)
        assertNull(flow.handleCallback(codeCallback(state), ORIGIN))
    }

    private fun codeCallback(
        state: String,
        exchange: String = "tab",
    ): Uri =
        Uri.parse("$ORIGIN${NativeAuth.CALLBACK_PATH}?state=$state&code=c0de&exchange=$exchange")

    private fun tokenCallback(state: String): Uri =
        Uri.parse("$ORIGIN${NativeAuth.CALLBACK_PATH}?state=$state&token_type=bearer&token=tok")

    private companion object {
        const val ORIGIN = "https://myapp.aws.databricksapps.com"
        const val PACKAGE = "ai.omnigent.android"
    }
}
