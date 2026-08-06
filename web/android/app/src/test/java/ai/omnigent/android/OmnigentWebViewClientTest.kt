package ai.omnigent.android

import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.net.Uri
import android.net.http.SslCertificate
import android.net.http.SslError
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.TestWebResourceError
import android.webkit.ValueCallback
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.util.ReflectionHelpers
import java.io.ByteArrayInputStream
import java.util.Date

@RunWith(RobolectricTestRunner::class)
class OmnigentWebViewClientTest {
    @Test
    fun `page start does not inject into the outgoing document`() {
        val webView = webView()
        val client = client()

        client.onPageStarted(webView, PINNED_URL, null)

        assertNull(webView.evaluatedScript)
    }

    @Test
    fun `page start resets native layout before the new page finishes`() {
        val webView = webView()
        var navigationStarts = 0
        val client =
            client(
                shouldInjectBridgeAtPageReady = false,
                onNavigationStarted = { navigationStarts++ },
            )

        client.onPageStarted(webView, PINNED_URL, null)
        assertEquals(1, navigationStarts)

        client.onPageFinished(webView, PINNED_URL)
        assertEquals(1, navigationStarts)
    }

    @Test
    fun `fallback injects the facade before declaring the page ready`() {
        val webView = webView()
        var readyUrl: String? = null
        val client =
            client(
                shouldInjectBridgeAtPageReady = true,
                onPageReady = { url -> readyUrl = url },
            )

        client.onPageFinished(webView, PINNED_URL)

        assertEquals(NativeBridgeScript.source, webView.evaluatedScript)
        assertNull(readyUrl)

        webView.completeEvaluation()
        assertEquals(PINNED_URL, readyUrl)
    }

    @Test
    fun `off-origin idp bounce stops the load and starts native login`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        client.onPageStarted(webView, OWN_IDP_URL, null)

        assertEquals(1, webView.stopLoadingCalls)
        assertTrue(loginRequired)
    }

    @Test
    fun `front-door proxy authorize page loads inline`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        client.onPageStarted(webView, PROXY_AUTH_URL, null)

        assertEquals(0, webView.stopLoadingCalls)
        assertFalse(loginRequired)
    }

    @Test
    fun `a failed or transitional page start does not trigger native login`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        // Null / about:blank / chrome-error:// mean the pinned server failed or
        // is transitioning, not that an IdP bounced us — never pop the browser.
        client.onPageStarted(webView, null, null)
        client.onPageStarted(webView, "about:blank", null)
        client.onPageStarted(webView, "chrome-error://chromewebdata/", null)

        assertEquals(0, webView.stopLoadingCalls)
        assertFalse(loginRequired)
    }

    @Test
    fun `redirect leg enters the flow with or without a gesture`() {
        listOf(false, true).forEach { gesture ->
            val webView = webView()
            var loginRequired = false
            val client = client(onLoginRequired = { loginRequired = true })

            assertFalse(
                client.shouldOverrideUrlLoading(
                    webView,
                    request(PROXY_AUTH_URL, gesture = gesture, redirect = true),
                ),
            )
            assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
            assertFalse(loginRequired)
        }
    }

    @Test
    fun `a proxy-shaped gestureless non-redirect navigation enters on page start`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL)))
        client.onPageStarted(webView, PROXY_AUTH_URL, null)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `a proxy-shaped gestured non-redirect navigation is handed off`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        assertTrue(
            client.shouldOverrideUrlLoading(
                webView,
                request(PROXY_AUTH_URL, gesture = true),
            ),
        )
        val startedIntent = webView.startedActivities.singleOrNull()
        assertEquals(Intent.ACTION_VIEW, startedIntent?.action)
        assertEquals(PROXY_AUTH_URL, startedIntent?.dataString)

        assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `a non-proxy gestureless navigation still starts native login`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))

        assertTrue(loginRequired)
    }

    @Test
    fun `a gestured cross-origin navigation stays inline while in flight`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        enterFlow(client, webView)

        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(GOOGLE_IDP_URL, gesture = true),
            ),
        )
        assertFalse(loginRequired)
        assertTrue(webView.startedActivities.isEmpty())
    }

    @Test
    fun `deadline expiry ends the flow in shouldOverrideUrlLoading`() {
        var now = UPTIME_MILLIS
        val webView = webView()
        var loginRequired = false
        var flowEnded = 0
        val client =
            client(
                clock = { now },
                onLoginRequired = { loginRequired = true },
                onProxyAuthFlowEnded = { flowEnded++ },
            )
        enterFlow(client, webView)

        now = UPTIME_MILLIS + DEADLINE_MILLIS + 1

        assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertTrue(loginRequired)
        assertEquals(0, flowEnded)
    }

    @Test
    fun `the flow survives just short of the deadline`() {
        var now = UPTIME_MILLIS
        val webView = webView()
        var loginRequired = false
        val client = client(clock = { now }, onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)

        now = UPTIME_MILLIS + DEADLINE_MILLIS - 1

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `deadline expiry is observed first in onPageStarted`() {
        var now = UPTIME_MILLIS
        val webView = webView()
        var loginRequired = false
        val client = client(clock = { now }, onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)

        now = UPTIME_MILLIS + DEADLINE_MILLIS + 1
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        assertEquals(1, webView.stopLoadingCalls)
        assertTrue(loginRequired)
    }

    @Test
    fun `a stale page start after reset does not resurrect the error tracker`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        client.endProxyAuth()
        client.onPageStarted(webView, STALE_IDP_URL, null)
        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(PROXY_AUTH_URL, redirect = true),
            ),
        )
        client.handleReceivedError(request(STALE_IDP_URL))

        loginRequired = false
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `a stale page start after an origin change does not launch login`() {
        var currentOrigin = OLD_PINNED_ORIGIN
        var loginCallbacks = 0
        val webView = webView()
        val client =
            client(
                pinnedOrigin = { currentOrigin },
                onLoginRequired = { loginCallbacks++ },
            )
        client.onPageStarted(webView, OLD_PINNED_URL, null)

        currentOrigin = NEW_PINNED_ORIGIN
        client.resetForOriginChange(NEW_PINNED_ORIGIN)
        client.stopLoadingAndLedger(webView)
        client.onPageStarted(webView, STALE_IDP_URL, null)

        assertEquals(0, loginCallbacks)
        client.onPageStarted(webView, NEW_PINNED_URL, null)
        assertEquals(0, loginCallbacks)
    }

    @Test
    fun `page start is stopped without login when no origin is pinned`() {
        var loginCallbacks = 0
        val webView = webView()
        val client =
            client(
                pinnedOrigin = { null },
                onLoginRequired = { loginCallbacks++ },
            )

        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        // Fail closed: with no pin there is no trust decision, so nothing may
        // load inline — and login against an unknown origin makes no sense.
        assertEquals(1, webView.stopLoadingCalls)
        assertEquals(0, loginCallbacks)
    }

    @Test
    fun `page finish is ignored when no origin is pinned`() {
        var pageReadyCallbacks = 0
        val webView = webView()
        val client =
            client(
                pinnedOrigin = { null },
                shouldInjectBridgeAtPageReady = true,
                onPageReady = { pageReadyCallbacks++ },
            )

        client.onPageFinished(webView, null)

        assertNull(webView.evaluatedScript)
        assertEquals(0, pageReadyCallbacks)
    }

    @Test
    fun `opaque https navigation is blocked when no origin is pinned`() {
        val client = client(pinnedOrigin = { null })

        assertTrue(client.shouldOverrideUrlLoading(webView(), request("https:opaque")))
    }

    @Test
    fun `a ledgered pinned-origin finish does not end a new same-server flow`() {
        val webView = webView()
        var flowEnded = 0
        val client = client(onProxyAuthFlowEnded = { flowEnded++ })

        client.onPageStarted(webView, PINNED_URL, null)
        client.stopLoadingAndLedger(webView)
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(0, flowEnded)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `reload sequence resets then ledgers then loads`() {
        val webView = webView()
        var flowEnded = 0
        val client = client(onProxyAuthFlowEnded = { flowEnded++ })

        enterFlow(client, webView)
        client.onPageStarted(webView, PINNED_URL, null)
        client.endProxyAuth()
        client.stopLoadingAndLedger(webView)
        webView.loadUrl(PINNED_URL)

        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(listOf("stopLoading", "loadUrl:$PINNED_URL"), webView.callOrder)
        assertEquals(0, flowEnded)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `a stale slot spares page plumbing for a genuine same-url finish`() {
        val webView = webView()
        var flowEnded = 0
        var readyUrl: String? = null
        val client =
            client(
                shouldInjectBridgeAtPageReady = true,
                onPageReady = { readyUrl = it },
                onProxyAuthFlowEnded = { flowEnded++ },
            )

        client.onPageStarted(webView, PINNED_URL, null)
        client.stopLoadingAndLedger(webView)
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PINNED_URL)))
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(0, flowEnded)
        assertEquals(NativeBridgeScript.source, webView.evaluatedScript)
        assertNull(readyUrl)
        webView.completeEvaluation()
        assertEquals(PINNED_URL, readyUrl)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `an unledgered override cancel never suppresses a later genuine entry`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        assertTrue(
            client.shouldOverrideUrlLoading(
                webView,
                request(PROXY_AUTH_URL, gesture = true),
            ),
        )

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL)))
        client.onPageStarted(webView, PROXY_AUTH_URL, null)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `the ledger slot clears on a non-matching page finish only`() {
        val survivingView = webView()
        val survivingClient = client()
        survivingClient.onPageStarted(survivingView, PINNED_URL, null)
        survivingClient.stopLoadingAndLedger(survivingView)
        survivingClient.onPageStarted(survivingView, PROXY_AUTH_URL, null)
        survivingClient.onPageFinished(survivingView, PINNED_URL)
        assertFalse(
            survivingClient.shouldOverrideUrlLoading(
                survivingView,
                request(PLAIN_IDP_URL),
            ),
        )

        val clearedView = webView()
        var loginRequired = false
        val clearedClient = client(onLoginRequired = { loginRequired = true })
        clearedClient.onPageStarted(clearedView, PINNED_URL, null)
        clearedClient.stopLoadingAndLedger(clearedView)
        clearedClient.onPageStarted(clearedView, PROXY_AUTH_URL, null)
        clearedClient.onPageFinished(clearedView, OTHER_IDP_URL)
        clearedClient.onPageFinished(clearedView, PINNED_URL)

        assertTrue(clearedClient.shouldOverrideUrlLoading(clearedView, request(PLAIN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `endProxyAuth clears the ledger slot but an error callback does not`() {
        val clearedView = webView()
        val clearedClient = client()
        clearedClient.onPageStarted(clearedView, PINNED_URL, null)
        clearedClient.stopLoadingAndLedger(clearedView)
        clearedClient.endProxyAuth()

        // The reset dropped the slot, so the stopped load's finish is ordinary
        // again and ends a flow entered afterwards.
        enterFlow(clearedClient, clearedView)
        clearedClient.onPageStarted(clearedView, PLAIN_IDP_URL, null)
        clearedClient.onPageFinished(clearedView, PINNED_URL)
        assertTrue(clearedClient.shouldOverrideUrlLoading(clearedView, request(PLAIN_IDP_URL)))

        val keptView = webView()
        val keptClient = client()
        keptClient.onPageStarted(keptView, PINNED_URL, null)
        keptClient.stopLoadingAndLedger(keptView)
        keptClient.handleReceivedError(request(OTHER_IDP_URL))

        // An error for another URL must leave the slot alone.
        enterFlow(keptClient, keptView)
        keptClient.onPageStarted(keptView, PLAIN_IDP_URL, null)
        keptClient.onPageFinished(keptView, PINNED_URL)
        assertFalse(keptClient.shouldOverrideUrlLoading(keptView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `a flow entered on page start tracks the url for its error exit`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        // Heuristic entry must write the tracker in the same callback, or the
        // error exit below meets an empty tracker and the flow never ends.
        client.onPageStarted(webView, PROXY_AUTH_URL, null)
        client.handleReceivedError(request(PROXY_AUTH_URL))

        assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `a stale error after a server switch does not kill the new flow`() {
        var currentOrigin = OLD_PINNED_ORIGIN
        val webView = webView()
        val client = client(pinnedOrigin = { currentOrigin })
        val oldProxyUrl = proxyAuthUrl(OLD_PINNED_ORIGIN)
        val newProxyUrl = proxyAuthUrl(NEW_PINNED_ORIGIN)

        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(oldProxyUrl, redirect = true),
            ),
        )
        client.onPageStarted(webView, STALE_IDP_URL, null)

        currentOrigin = NEW_PINNED_ORIGIN
        client.endProxyAuth()
        client.stopLoadingAndLedger(webView)
        webView.loadUrl(NEW_PINNED_URL)
        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(newProxyUrl, redirect = true),
            ),
        )
        client.handleReceivedError(request(STALE_IDP_URL))

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `an entry-hop error before any page start exits the flow`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        // The IdP hop failed before it committed (no onPageStarted) — the flow
        // must not sit in flight until the deadline.
        enterFlow(client, webView)
        client.handleReceivedError(request(PROXY_AUTH_URL))

        assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `an unrelated error before any page start does not exit the flow`() {
        val webView = webView()
        val client = client()

        enterFlow(client, webView)
        client.handleReceivedError(request(OTHER_IDP_URL))

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `a mid-flow hop error before it commits exits the flow`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        client.handleReceivedError(request(OTHER_IDP_URL))

        assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `flow survives a pinned-origin hop mid-flow`() {
        val webView = webView()
        val client = client()
        enterFlow(client, webView)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PINNED_URL)))

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `flow does not end at navigation start`() {
        val webView = webView()
        val client = client()
        enterFlow(client, webView)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PINNED_URL)))
        client.onPageStarted(webView, PINNED_URL, null)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `flow ends on a pinned-origin page finish with either bridge path`() {
        listOf(false, true).forEach { injectAtPageReady ->
            val webView = webView()
            var loginRequired = false
            val client =
                client(
                    shouldInjectBridgeAtPageReady = injectAtPageReady,
                    onLoginRequired = { loginRequired = true },
                )
            enterFlow(client, webView)
            client.onPageStarted(webView, PINNED_URL, null)

            client.onPageFinished(webView, PINNED_URL)

            assertTrue(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
            assertTrue(loginRequired)
        }
    }

    @Test
    fun `onProxyAuthFlowEnded fires once before onPageReady`() {
        val webView = webView()
        val events = mutableListOf<String>()
        val client =
            client(
                onPageReady = { events += "ready" },
                onProxyAuthFlowEnded = { events += "ended" },
            )
        enterFlow(client, webView)

        client.onPageFinished(webView, PINNED_URL)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(1, events.count { it == "ended" })
        assertEquals(listOf("ended", "ready"), events.take(2))
    }

    @Test
    fun `a finish for the old origin after a server switch does not end the new flow`() {
        var currentOrigin = OLD_PINNED_ORIGIN
        val webView = webView()
        var flowEnded = 0
        val client =
            client(
                pinnedOrigin = { currentOrigin },
                onProxyAuthFlowEnded = { flowEnded++ },
            )

        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(proxyAuthUrl(OLD_PINNED_ORIGIN), redirect = true),
            ),
        )
        currentOrigin = NEW_PINNED_ORIGIN
        client.endProxyAuth()
        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(proxyAuthUrl(NEW_PINNED_ORIGIN), redirect = true),
            ),
        )

        client.onPageFinished(webView, OLD_PINNED_URL)

        assertEquals(0, flowEnded)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `main-frame onReceivedError for the current URL ends the flow`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        client.handleReceivedError(request(PLAIN_IDP_URL))

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `onReceivedError override delegates current main-frame failure`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        client.onReceivedError(
            webView,
            request(PLAIN_IDP_URL),
            TestWebResourceError(),
        )

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `a subframe error does not end the flow`() {
        val webView = webView()
        val client = client()
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        client.handleReceivedError(request(PLAIN_IDP_URL, mainFrame = false))

        assertFalse(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
    }

    @Test
    fun `main-frame onReceivedHttpError for an uncommitted hop ends the flow`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)

        client.onReceivedHttpError(
            webView,
            request(PROXY_AUTH_URL),
            httpErrorResponse(),
        )

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `main-frame onReceivedHttpError for a committed IdP page keeps the flow alive`() {
        // An IdP can serve its interactive login form with a 401/403 status;
        // the user is still authenticating, so the flow must stay in flight.
        val webView = webView()
        val client = client()
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)

        client.onReceivedHttpError(
            webView,
            request(PLAIN_IDP_URL),
            httpErrorResponse(),
        )

        assertFalse(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
    }

    @Test
    fun `matching terminal callback prevents a later stop from creating a ledger`() {
        val webView = webView()
        var flowEnded = 0
        var loginRequired = false
        val client =
            client(
                onProxyAuthFlowEnded = { flowEnded++ },
                onLoginRequired = { loginRequired = true },
            )
        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL),
            httpErrorResponse(),
        )

        client.stopLoadingAndLedger(webView)
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(1, flowEnded)
        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `onReceivedSslError on the tracked hop ends the flow`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)

        val handler = ReflectionHelpers.callConstructor(SslErrorHandler::class.java)
        val certificate = SslCertificate("CN=subject", "CN=issuer", Date(0), Date(1))
        client.onReceivedSslError(
            webView,
            handler,
            SslError(SslError.SSL_UNTRUSTED, certificate, PROXY_AUTH_URL),
        )

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `onReceivedSslError on a subresource keeps the flow alive`() {
        // A cert failure on an IdP subresource (or a stale prior navigation)
        // must not misroute the flow's next redirect into native login.
        val webView = webView()
        var flowEnded = 0
        val client = client(onProxyAuthFlowEnded = { flowEnded++ })
        enterFlow(client, webView)

        val handler = ReflectionHelpers.callConstructor(SslErrorHandler::class.java)
        val certificate = SslCertificate("CN=subject", "CN=issuer", Date(0), Date(1))
        client.onReceivedSslError(
            webView,
            handler,
            SslError(SslError.SSL_UNTRUSTED, certificate, "https://cdn.idp.example.com/asset.js"),
        )

        assertEquals(0, flowEnded)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `onReceivedSslError preserves loading state for a later ledgered stop`() {
        val webView = webView()
        var flowEnded = 0
        var loginRequired = false
        val client =
            client(
                onProxyAuthFlowEnded = { flowEnded++ },
                onLoginRequired = { loginRequired = true },
            )
        client.onPageStarted(webView, PINNED_URL, null)

        val handler = ReflectionHelpers.callConstructor(SslErrorHandler::class.java)
        val certificate = SslCertificate("CN=subject", "CN=issuer", Date(0), Date(1))
        client.onReceivedSslError(
            webView,
            handler,
            SslError(SslError.SSL_UNTRUSTED, certificate, PINNED_URL),
        )

        client.stopLoadingAndLedger(webView)
        enterFlow(client, webView)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(0, flowEnded)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `onRenderProcessGone ends the flow`() {
        val webView = webView()
        var loginRequired = false
        var webViewUnusable = 0
        val client =
            client(
                onLoginRequired = { loginRequired = true },
                onWebViewUnusable = { webViewUnusable++ },
            )
        enterFlow(client, webView)

        val handled = client.onRenderProcessGone(webView, renderProcessGoneDetail())

        assertTrue(handled)
        assertEquals(1, webViewUnusable)
        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `a proxy auth flow keeps the real user agent`() {
        val webView = webViewWithWebViewUa()
        val original = webView.settings.userAgentString
        val client = client()

        enterFlow(client, webView)

        assertEquals(original, webView.settings.userAgentString)
        assertTrue(webView.loadedUrls.isEmpty())
    }

    @Test
    fun `refusal stops loading and notifies without reloading or mutating the user agent`() {
        val webView = webViewWithWebViewUa()
        val original = webView.settings.userAgentString
        var unsupported = 0
        val client = client(onEmbeddedSignInUnsupported = { unsupported++ })
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_QUERY_URL, null)

        assertEquals(1, webView.stopLoadingCalls)
        assertEquals(1, unsupported)
        assertTrue(webView.loadedUrls.isEmpty())
        assertEquals(original, webView.settings.userAgentString)
    }

    @Test
    fun `refusal is detected in the query`() {
        val webView = webView()
        var unsupported = 0
        val client = client(onEmbeddedSignInUnsupported = { unsupported++ })
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_QUERY_URL, null)

        assertEquals(1, unsupported)
    }

    @Test
    fun `refusal is detected in the fragment`() {
        val webView = webView()
        var unsupported = 0
        val client = client(onEmbeddedSignInUnsupported = { unsupported++ })
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_FRAGMENT_URL, null)

        assertEquals(1, unsupported)
    }

    @Test
    fun `refusal is detected one decoded nesting level deep`() {
        val webView = webView()
        var unsupported = 0
        val client = client(onEmbeddedSignInUnsupported = { unsupported++ })
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_NESTED_URL, null)

        assertEquals(1, unsupported)
    }

    @Test
    fun `repeated refusal page starts notify once and never start native login`() {
        val webView = webView()
        var unsupported = 0
        var loginRequired = false
        val client =
            client(
                onLoginRequired = { loginRequired = true },
                onEmbeddedSignInUnsupported = { unsupported++ },
            )
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_QUERY_URL, null)
        client.onPageStarted(webView, REJECTION_QUERY_URL, null)

        assertEquals(1, unsupported)
        assertEquals(1, webView.stopLoadingCalls)
        assertFalse(loginRequired)
    }

    @Test
    fun `while refused an off-origin navigation is cancelled without handoff or login`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        refuse(client, webView)

        assertTrue(
            client.shouldOverrideUrlLoading(
                webView,
                request(OTHER_IDP_URL, gesture = true),
            ),
        )

        assertFalse(loginRequired)
        assertTrue(webView.startedActivities.isEmpty())
    }

    @Test
    fun `while refused a mailto main-frame navigation is handed off externally`() {
        val webView = webView()
        val client = client()
        refuse(client, webView)

        assertTrue(client.shouldOverrideUrlLoading(webView, request(MAILTO_URL)))

        val startedIntent = webView.startedActivities.single()
        assertEquals(Intent.ACTION_VIEW, startedIntent.action)
        assertEquals(MAILTO_URL, startedIntent.dataString)
    }

    @Test
    fun `while refused a same-origin navigation loads inline`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        refuse(client, webView)

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PINNED_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `while refused a subframe navigation remains unchanged`() {
        val webView = webView()
        val client = client()
        refuse(client, webView)

        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(OTHER_IDP_URL, mainFrame = false),
            ),
        )
    }

    @Test
    fun `while refused page finishes do not change state or notify again`() {
        val webView = webView()
        var unsupported = 0
        var loginRequired = false
        val client =
            client(
                onLoginRequired = { loginRequired = true },
                onEmbeddedSignInUnsupported = { unsupported++ },
            )
        refuse(client, webView, REJECTION_QUERY_URL)

        client.onPageFinished(webView, REJECTION_QUERY_URL)
        client.onPageFinished(webView, PINNED_URL)

        assertEquals(1, unsupported)
        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `while refused a main-frame terminal callback is ignored`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        refuse(client, webView)

        client.handleReceivedError(request(REJECTION_QUERY_URL))

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `endProxyAuth returns refused state to idle`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        refuse(client, webView)

        client.endProxyAuth()

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OTHER_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `endProxyAuth ends the flow without a pinned page load`() {
        val webView = webView()
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })
        enterFlow(client, webView)

        client.endProxyAuth()

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OWN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `a path mentioning disallowed_useragent is not a refusal`() {
        val webView = webViewWithWebViewUa()
        val original = webView.settings.userAgentString
        var unsupported = 0
        val client = client(onEmbeddedSignInUnsupported = { unsupported++ })
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_PATH_URL, null)

        assertEquals(0, unsupported)
        assertEquals(0, webView.stopLoadingCalls)
        assertTrue(webView.loadedUrls.isEmpty())
        assertEquals(original, webView.settings.userAgentString)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    @Test
    fun `a benign parameter carrying the token is not a refusal`() {
        val webView = webView()
        var unsupported = 0
        val client = client(onEmbeddedSignInUnsupported = { unsupported++ })
        enterFlow(client, webView)

        client.onPageStarted(webView, REJECTION_LOOKALIKE_URL, null)

        assertEquals(0, unsupported)
        assertEquals(0, webView.stopLoadingCalls)
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PLAIN_IDP_URL)))
    }

    private fun enterFlow(
        client: OmnigentWebViewClient,
        webView: RecordingWebView,
    ) {
        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request(PROXY_AUTH_URL, redirect = true),
            ),
        )
    }

    private fun refuse(
        client: OmnigentWebViewClient,
        webView: RecordingWebView,
        url: String = REJECTION_QUERY_URL,
    ) {
        enterFlow(client, webView)
        client.onPageStarted(webView, url, null)
    }

    private fun request(
        url: String,
        gesture: Boolean = false,
        mainFrame: Boolean = true,
        redirect: Boolean = false,
    ): WebResourceRequest =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)

            override fun isForMainFrame(): Boolean = mainFrame

            override fun isRedirect(): Boolean = redirect

            override fun hasGesture(): Boolean = gesture

            override fun getMethod(): String = "GET"

            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private fun httpErrorResponse() =
        WebResourceResponse(
            "text/plain",
            "UTF-8",
            500,
            "Internal Server Error",
            emptyMap(),
            ByteArrayInputStream("error".toByteArray()),
        )

    private fun renderProcessGoneDetail() =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = true

            override fun rendererPriorityAtExit(): Int = 0
        }

    // onPageReady stays last so callers can pass it as a trailing lambda.
    private fun client(
        pinnedOrigin: () -> String? = { PINNED_ORIGIN },
        shouldInjectBridgeAtPageReady: Boolean = false,
        onLoginRequired: () -> Unit = {},
        onNavigationStarted: () -> Unit = {},
        onPageReady: (String?) -> Unit = {},
        onProxyAuthFlowEnded: () -> Unit = {},
        clock: () -> Long = { UPTIME_MILLIS },
        onEmbeddedSignInUnsupported: () -> Unit = {},
        onWebViewUnusable: () -> Unit = {},
    ) = OmnigentWebViewClient(
        pinnedOrigin = pinnedOrigin,
        shouldInjectBridgeAtPageReady = { shouldInjectBridgeAtPageReady },
        onPageReady = onPageReady,
        onLoginRequired = onLoginRequired,
        onNavigationStarted = onNavigationStarted,
        onProxyAuthFlowEnded = onProxyAuthFlowEnded,
        clock = clock,
        onEmbeddedSignInUnsupported = onEmbeddedSignInUnsupported,
        onWebViewUnusable = onWebViewUnusable,
    )

    private fun webView(): RecordingWebView =
        RecordingWebView(
            RecordingContext(ApplicationProvider.getApplicationContext()),
        )

    private fun webViewWithWebViewUa(): RecordingWebView =
        webView().apply {
            settings.userAgentString = WEBVIEW_USER_AGENT
        }

    private fun proxyAuthUrl(origin: String): String =
        "https://idp.example.com/oidc/oauth2/v2.0/authorize?response_type=code" +
            "&redirect_uri=${Uri.encode("$origin/.auth/callback")}"

    private class RecordingContext(
        base: Context,
    ) : ContextWrapper(base) {
        val startedActivities = mutableListOf<Intent>()

        override fun startActivity(intent: Intent) {
            startedActivities += intent
        }
    }

    private class RecordingWebView(
        private val recordingContext: RecordingContext,
    ) : WebView(recordingContext) {
        var evaluatedScript: String? = null
        var stopLoadingCalls = 0
        val loadedUrls = mutableListOf<String>()
        val callOrder = mutableListOf<String>()
        val startedActivities: List<Intent>
            get() = recordingContext.startedActivities
        private var callback: ValueCallback<String>? = null

        override fun loadUrl(url: String) {
            loadedUrls += url
            callOrder += "loadUrl:$url"
        }

        override fun evaluateJavascript(
            script: String,
            resultCallback: ValueCallback<String>?,
        ) {
            evaluatedScript = script
            callback = resultCallback
        }

        override fun stopLoading() {
            stopLoadingCalls++
            callOrder += "stopLoading"
        }

        fun completeEvaluation() {
            callback?.onReceiveValue("null")
        }
    }

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val PINNED_URL = "$PINNED_ORIGIN/app"
        const val OLD_PINNED_ORIGIN = "https://old.example.com"
        const val OLD_PINNED_URL = "$OLD_PINNED_ORIGIN/app"
        const val NEW_PINNED_ORIGIN = "https://new.example.com"
        const val NEW_PINNED_URL = "$NEW_PINNED_ORIGIN/app"
        const val DEADLINE_MILLIS = 6 * 60_000L

        // Device uptime is never zero; a zero-based fake clock would let the
        // missing flow-start stamp pass as a flow that started at boot.
        const val UPTIME_MILLIS = 9_000_000L

        const val PROXY_AUTH_URL =
            "https://idp.example.com/oidc/oauth2/v2.0/authorize?response_type=code" +
                "&redirect_uri=https%3A%2F%2Fexample.com%2F.auth%2Fcallback"

        const val OWN_IDP_URL =
            "https://accounts.example.org/authorize?response_type=code" +
                "&redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback"

        const val PLAIN_IDP_URL = "https://idp.example.com/login/sso"
        const val OTHER_IDP_URL = "https://other-idp.example.net/login"
        const val GOOGLE_IDP_URL = "https://accounts.google.com/signin"
        const val STALE_IDP_URL = "https://stale-idp.example.net/login"
        const val MAILTO_URL = "mailto:support@example.com"

        const val REJECTION_QUERY_URL =
            "https://accounts.google.com/v3/signin/rejected?error=disallowed_useragent"
        const val REJECTION_FRAGMENT_URL =
            "https://accounts.google.com/v3/signin/rejected#error=disallowed_useragent"
        const val REJECTION_NESTED_URL =
            "https://accounts.google.com/signin?continue=https%3A%2F%2Faccounts.google.com" +
                "%2Frejected%3Ferror%3Ddisallowed%5Fuseragent"
        const val REJECTION_PATH_URL =
            "https://accounts.google.com/help/disallowed_useragent/details"
        const val REJECTION_LOOKALIKE_URL =
            "https://idp.example.com/authorize?state=disallowed_useragent" +
                "&note=error%3Ddisallowed" // an error mention that is not the error param

        const val WEBVIEW_USER_AGENT =
            "Mozilla/5.0 (Linux; Android 14; Pixel Build/X; wv) AppleWebKit/537.36 " +
                "(KHTML, like Gecko) Version/4.0 Chrome/126.0.0.0 Mobile Safari/537.36"
    }
}
