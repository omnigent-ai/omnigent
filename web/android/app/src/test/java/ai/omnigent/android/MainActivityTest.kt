package ai.omnigent.android

import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.net.Uri
import android.net.http.SslCertificate
import android.net.http.SslError
import android.os.Bundle
import android.os.Looper
import android.os.SystemClock
import android.text.TextUtils
import android.view.View
import android.view.ViewGroup
import android.webkit.FakeWebResourceError
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.widget.Button
import android.widget.TextView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowPopupMenu
import org.robolectric.shadows.ShadowRestrictionsManager
import org.robolectric.shadows.ShadowSslErrorHandler
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URI
import java.time.Duration
import java.util.concurrent.CompletableFuture
import java.util.concurrent.Executor
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MainActivityTest {
    @Test
    fun `native OIDC network failure stays on the recovery screen`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val origin = "http://127.0.0.1:1"
        val grantStore =
            object : OidcCredentialStorage {
                override fun load(origin: String): OidcRefreshGrant? =
                    OidcRefreshGrant("stored-grant", null)

                override fun save(
                    origin: String,
                    grant: OidcRefreshGrant,
                ) = Unit

                override fun delete(origin: String) = Unit
            }
        val credentials =
            OidcCredentials(
                grantStore,
                OidcPendingSignInStore(context),
                executor = Executor { it.run() },
            )
        OidcCredentials::class.java.getDeclaredField("shared").apply {
            isAccessible = true
            set(null, credentials)
        }
        MainActivity.manifestReader = {
            ServerManifest(
                1.0,
                ServerManifest.Auth(
                    ServerManifest.Mode.OIDC,
                    "ap_session",
                    listOf(OidcRedirect.REDIRECT_URI),
                ),
            )
        }
        ServerStore(context).connect(origin)

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertTrue(
            activity.declared<ConnectionStatus?>(
                "connectionStatus",
            ) is ConnectionStatus.ServerUnreachable,
        )
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        assertFalse(activity.isFinishing)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.findViewById<Button>(R.id.status_details_toggle).performClick()
        val details = activity.findViewById<TextView>(R.id.status_details_text).text.toString()
        assertTrue(details, details.contains("NetworkUnavailable"))
        assertFalse(details.contains("Couldn't reach"))
    }

    @Test
    fun `reserved login ticket shows update instructions and keeps change server available`() {
        LocalHttpServer { request ->
            if (request.path == "/auth/cli-login") {
                LocalHttpServer.Response(
                    200,
                    """{"ticket":"upgrade-required","login_url":"/auth/cli-upgrade"}""",
                )
            } else {
                LocalHttpServer.Response(404)
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
            val webView = activity.webView()
            webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
            val deadline = System.currentTimeMillis() + 10_000
            while (activity.declared<ConnectionStatus?>(
                    "connectionStatus",
                ) !is ConnectionStatus.UpdateRequired &&
                System.currentTimeMillis() < deadline
            ) {
                shadowOf(Looper.getMainLooper()).idle()
                Thread.sleep(5)
            }
            assertTrue(
                activity.declared<ConnectionStatus?>(
                    "connectionStatus",
                ) is ConnectionStatus.UpdateRequired,
            )
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
            assertEquals(
                activity.getString(R.string.status_update_required),
                activity.findViewById<TextView>(R.id.status_headline).text.toString(),
            )
            assertEquals(
                activity.getString(R.string.status_how_to_update),
                activity.findViewById<Button>(R.id.status_primary).text.toString(),
            )
            assertEquals(
                activity.getString(R.string.status_change_server),
                activity.findViewById<Button>(R.id.status_secondary).text.toString(),
            )

            activity.findViewById<Button>(R.id.status_primary).performClick()
            assertEquals(
                "${server.origin}/auth/cli-upgrade",
                shadowOf(activity).nextStartedActivity.dataString,
            )
            activity.findViewById<Button>(R.id.status_secondary).performClick()
            assertEquals(
                ConnectActivity::class.java.name,
                shadowOf(activity).nextStartedActivity.component?.className,
            )
        }
    }

    @Test
    fun `legacy ticket browser wait shows the auth handoff instead of slow connecting`() {
        LocalHttpServer { request ->
            when (request.path) {
                "/auth/cli-login" -> {
                    LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
                }

                "/auth/cli-poll" -> {
                    LocalHttpServer.Response(202)
                }

                else -> {
                    LocalHttpServer.Response(404)
                }
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread {
                    activity.declared<ConnectionStatus?>("connectionStatus") is
                        ConnectionStatus.AuthTabHandoff
                }
                assertEquals(
                    "${server.origin}/auth/login",
                    shadowOf(activity).nextStartedActivity.dataString,
                )
                shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(10_500))
                assertTrue(
                    activity.declared<ConnectionStatus?>("connectionStatus") is
                        ConnectionStatus.AuthTabHandoff,
                )
                assertEquals(
                    activity.getString(R.string.status_opening_sign_in),
                    activity.findViewById<TextView>(R.id.status_headline).text.toString(),
                )
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `legacy ticket network failure shows server unreachable`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("http://127.0.0.1:1")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        try {
            val webView = activity.webView()
            webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
            awaitMainThread {
                activity.declared<ConnectionStatus?>("connectionStatus") is
                    ConnectionStatus.ServerUnreachable
            }
            val status = activity.declared<ConnectionStatus.ServerUnreachable>("connectionStatus")
            assertEquals("ConnectException", status.details?.error)
        } finally {
            controller.pause().stop().destroy()
        }
    }

    @Test
    fun `legacy ticket request server error shows generic unreachable with numeric details`() {
        LocalHttpServer { request ->
            if (request.path == "/auth/cli-login") {
                LocalHttpServer.Response(503, "secret-response-body")
            } else {
                LocalHttpServer.Response(404)
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread {
                    activity.isFinishing ||
                        activity.declared<ConnectionStatus?>(
                            "connectionStatus",
                        ) is ConnectionStatus.ServerUnreachable
                }
                assertFalse("HTTP 503 must show recovery instead of refusal", activity.isFinishing)
                val status =
                    activity.declared<ConnectionStatus.ServerUnreachable>(
                        "connectionStatus",
                    )
                assertEquals(UnreachableKind.GENERIC, status.kind)
                assertEquals("HTTP 503", status.details?.error)
                assertFalse(
                    status.details
                        ?.error
                        .orEmpty()
                        .contains("secret-response-body"),
                )
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `legacy ticket poll denial leaves the handoff for sign in setup`() {
        LocalHttpServer { request ->
            when (request.path) {
                "/auth/cli-login" -> {
                    LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
                }

                "/auth/cli-poll" -> {
                    LocalHttpServer.Response(410)
                }

                else -> {
                    LocalHttpServer.Response(404)
                }
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread {
                    activity.declared<ConnectionStatus?>("connectionStatus") is
                        ConnectionStatus.AuthTabHandoff
                }
                awaitMainThread { activity.isFinishing }
                val setup = shadowOf(activity).nextStartedActivity
                assertEquals(ConnectActivity::class.java.name, setup.component?.className)
                assertEquals(
                    OidcSignInException
                        .SignInRefused(
                            server.origin.removePrefix("http://"),
                            null,
                        ).message,
                    setup.getStringExtra(ConnectActivity.EXTRA_ERROR),
                )
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `legacy browser launch failure shows browser unavailable in setup`() {
        LocalHttpServer { request ->
            if (request.path == "/auth/cli-login") {
                LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
            } else {
                LocalHttpServer.Response(202)
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                activity.declared<OidcLoginManager>("oidcLoginManager").browserLauncher =
                    { _, _ -> false }
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread { activity.isFinishing }
                assertEquals(
                    OidcSignInException
                        .BrowserUnavailable(
                            server.origin.removePrefix("http://"),
                        ).message,
                    shadowOf(
                        activity,
                    ).nextStartedActivity.getStringExtra(ConnectActivity.EXTRA_ERROR),
                )
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `legacy ticket poll server error shows generic unreachable with numeric details`() {
        LocalHttpServer { request ->
            when (request.path) {
                "/auth/cli-login" -> {
                    LocalHttpServer.Response(
                        200,
                        """{"ticket":"abc","login_url":"/auth/login"}""",
                    )
                }

                "/auth/cli-poll" -> {
                    LocalHttpServer.Response(503, "secret-response-body")
                }

                else -> {
                    LocalHttpServer.Response(404)
                }
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                activity.declared<OidcLoginManager>("oidcLoginManager").pollIntervalMillis = 20
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread {
                    activity.declared<ConnectionStatus?>(
                        "connectionStatus",
                    ) is ConnectionStatus.ServerUnreachable
                }
                val status =
                    activity.declared<ConnectionStatus.ServerUnreachable>(
                        "connectionStatus",
                    )
                assertEquals(UnreachableKind.GENERIC, status.kind)
                assertEquals("HTTP 503", status.details?.error)
                shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
                activity.findViewById<Button>(R.id.status_details_toggle).performClick()
                val details =
                    activity
                        .findViewById<TextView>(
                            R.id.status_details_text,
                        ).text
                        .toString()
                assertTrue(details.contains("HTTP 503"))
                assertFalse(details.contains("secret-response-body"))
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `legacy ticket poll network deadline shows unreachable`() {
        val server =
            LocalHttpServer { request ->
                if (request.path == "/auth/cli-login") {
                    LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
                } else {
                    LocalHttpServer.Response(202)
                }
            }
        try {
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                activity.declared<OidcLoginManager>("oidcLoginManager").apply {
                    pollIntervalMillis = 100
                    pollTimeoutMillis = 400
                }
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread {
                    activity.declared<ConnectionStatus?>(
                        "connectionStatus",
                    ) is ConnectionStatus.AuthTabHandoff
                }
                server.close()
                awaitMainThread {
                    activity.declared<ConnectionStatus?>(
                        "connectionStatus",
                    ) is ConnectionStatus.ServerUnreachable
                }
                val status =
                    activity.declared<ConnectionStatus.ServerUnreachable>(
                        "connectionStatus",
                    )
                assertTrue(
                    status.kind == UnreachableKind.OFFLINE ||
                        status.kind == UnreachableKind.GENERIC,
                )
                assertTrue(status.details?.error in setOf("ConnectException", "IOException"))
            } finally {
                controller.pause().stop().destroy()
            }
        } finally {
            server.close()
        }
    }

    @Test
    fun `legacy ticket poll pending deadline returns neutral setup message`() {
        LocalHttpServer { request ->
            if (request.path == "/auth/cli-login") {
                LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
            } else {
                LocalHttpServer.Response(202)
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                activity.declared<OidcLoginManager>("oidcLoginManager").apply {
                    pollIntervalMillis = 20
                    pollTimeoutMillis = 180
                }
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread { activity.isFinishing }
                assertEquals(
                    "Sign-in timed out. Try again.",
                    shadowOf(
                        activity,
                    ).nextStartedActivity.getStringExtra(ConnectActivity.EXTRA_ERROR),
                )
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `legacy ticket poll recovers from a network error before neutral timeout`() {
        val pollConnections = AtomicInteger()
        LocalHttpServer { request ->
            if (request.path == "/auth/cli-login") {
                LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
            } else {
                LocalHttpServer.Response(202)
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                activity.declared<OidcLoginManager>("oidcLoginManager").apply {
                    pollIntervalMillis = 20
                    pollTimeoutMillis = 600
                    pollConnectionFactory = { url ->
                        if (pollConnections.getAndIncrement() == 0) {
                            object : HttpURLConnection(url) {
                                override fun getResponseCode(): Int =
                                    throw IOException("transient poll failure")

                                override fun connect() = Unit

                                override fun disconnect() = Unit

                                override fun usingProxy(): Boolean = false
                            }
                        } else {
                            url.openConnection() as HttpURLConnection
                        }
                    }
                }
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread { pollConnections.get() >= 2 }
                awaitMainThread {
                    activity.isFinishing ||
                        activity.declared<ConnectionStatus?>(
                            "connectionStatus",
                        ) is ConnectionStatus.ServerUnreachable
                }
                assertTrue("a later 202 must clear the earlier network error", activity.isFinishing)
                assertTrue(pollConnections.get() >= 2)
                assertTrue(server.requests.any { it.path == "/auth/cli-poll" })
                assertEquals(
                    "Sign-in timed out. Try again.",
                    shadowOf(activity)
                        .nextStartedActivity
                        .getStringExtra(ConnectActivity.EXTRA_ERROR),
                )
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `Back during legacy browser wait cancels late session delivery`() {
        LocalHttpServer { request ->
            when (request.path) {
                "/auth/cli-login" -> {
                    LocalHttpServer.Response(200, """{"ticket":"abc","login_url":"/auth/login"}""")
                }

                "/auth/cli-poll" -> {
                    LocalHttpServer.Response(202)
                }

                else -> {
                    LocalHttpServer.Response(404)
                }
            }
        }.use { server ->
            ServerStore(ApplicationProvider.getApplicationContext()).connect(server.origin)
            val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
            val activity = controller.get()
            try {
                val webView = activity.webView()
                webView.webViewClient.onPageStarted(webView, "https://idp.example.test/login", null)
                awaitMainThread {
                    activity.declared<ConnectionStatus?>("connectionStatus") is
                        ConnectionStatus.AuthTabHandoff
                }
                shadowOf(activity).nextStartedActivity // consume browser launch

                activity.onBackPressedDispatcher.onBackPressed()

                assertTrue(activity.isFinishing)
                assertEquals(
                    ConnectActivity::class.java.name,
                    shadowOf(activity).nextStartedActivity.component?.className,
                )
                val manager = activity.declared<OidcLoginManager>("oidcLoginManager")
                val callback =
                    OidcLoginManager::class.java
                        .getDeclaredField("sessionCallback")
                        .apply {
                            isAccessible = true
                        }.get(manager)
                assertNull(callback)
                shadowOf(activity).clearNextStartedActivities()
                activity.invoke("onSessionToken", arrayOf(String::class.java), "aaa.bbb.ccc")
                assertNull(shadowOf(activity).nextStartedActivity)
            } finally {
                controller.pause().stop().destroy()
            }
        }
    }

    @Test
    fun `unreachable server offers retry then a successful commit clears the screen`() {
        var manifestReads = 0
        MainActivity.manifestReader = {
            manifestReads++
            ServerManifest.BASELINE
        }
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/chat/123"
        webView.loadUrl(url)
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(url),
            FakeWebResourceError(-6, "net::ERR_CONNECTION_REFUSED"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))

        assertEquals(
            activity.getString(R.string.status_server_unreachable),
            activity.findViewById<TextView>(R.id.status_headline).text.toString(),
        )
        assertEquals(
            activity.getString(R.string.status_retry),
            activity.findViewById<Button>(R.id.status_primary).text.toString(),
        )
        assertEquals(
            activity.getString(R.string.status_change_server),
            activity.findViewById<Button>(R.id.status_secondary).text.toString(),
        )
        assertEquals(
            View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS,
            webView.importantForAccessibility,
        )
        activity.findViewById<Button>(R.id.status_details_toggle).performClick()
        val details = activity.findViewById<TextView>(R.id.status_details_text).text.toString()
        assertTrue(details.contains("https://example.com"))
        assertTrue(details, details.contains("net::ERR_CONNECTION_REFUSED (ERROR_CONNECT)"))
        assertFalse(details.contains("/chat/123"))
        activity.findViewById<Button>(R.id.status_copy_details).performClick()
        val copied =
            activity
                .getSystemService(ClipboardManager::class.java)
                .primaryClip!!
                .getItemAt(0)
                .text
                .toString()
        assertTrue(copied.startsWith(details))
        assertTrue(copied.contains("Android API\n"))

        activity.findViewById<Button>(R.id.status_primary).performClick()
        assertEquals(2, manifestReads)
        assertEquals(url, shadowOf(webView).lastLoadedUrl)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onPageCommitVisible(webView, url)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(
            View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS,
            webView.importantForAccessibility,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(View.IMPORTANT_FOR_ACCESSIBILITY_AUTO, webView.importantForAccessibility)
    }

    @Test
    fun `retry after rejected certificate shows TLS failure again without page start`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com"
        val callbackUrl = "$url/"
        val certificate = SslCertificate("host", "issuer", java.util.Date(), java.util.Date())
        val error = SslError(SslError.SSL_UNTRUSTED, certificate, callbackUrl)
        val firstHandler = Shadow.newInstanceOf(SslErrorHandler::class.java)

        webView.webViewClient.onReceivedSslError(webView, firstHandler, error)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertEquals(
            UnreachableKind.TLS,
            (
                activity.declared<ConnectionStatus?>("connectionStatus") as
                    ConnectionStatus.ServerUnreachable
            ).kind,
        )

        activity.findViewById<Button>(R.id.status_primary).performClick()
        assertEquals(url, shadowOf(webView).lastLoadedUrl)
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )
        val secondHandler = Shadow.newInstanceOf(SslErrorHandler::class.java)
        webView.webViewClient.onReceivedSslError(webView, secondHandler, error)

        assertEquals(
            UnreachableKind.TLS,
            (
                activity.declared<ConnectionStatus?>("connectionStatus") as
                    ConnectionStatus.ServerUnreachable
            ).kind,
        )
        listOf(firstHandler, secondHandler).forEach { handler ->
            val shadow = Shadow.extract<ShadowSslErrorHandler>(handler)
            assertTrue(shadow.wasCancelCalled())
            assertFalse(shadow.wasProceedCalled())
        }

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.findViewById<Button>(R.id.status_primary).performClick()
        webView.webViewClient.onReceivedError(
            webView,
            request(callbackUrl),
            FakeWebResourceError(-6, "net::ERR_CONNECTION_REFUSED"),
        )
        val transportFailure =
            activity.declared<ConnectionStatus?>("connectionStatus") as
                ConnectionStatus.ServerUnreachable
        assertTrue(transportFailure.details!!.error.contains("ERROR_CONNECT"))
    }

    @Test
    fun `server menu Reload reports a second TLS failure without page start`() {
        val activity = launch()
        val webView = activity.webView()
        val error =
            SslError(
                SslError.SSL_UNTRUSTED,
                SslCertificate("host", "issuer", java.util.Date(), java.util.Date()),
                "https://example.com/",
            )
        val firstHandler = Shadow.newInstanceOf(SslErrorHandler::class.java)
        webView.webViewClient.onReceivedSslError(webView, firstHandler, error)
        val firstFailure =
            activity.declared<ConnectionStatus?>("connectionStatus") as
                ConnectionStatus.ServerUnreachable

        activity.invoke(
            "showServerSwitcherMenu",
            arrayOf(View::class.java),
            activity.switchButton(),
        )
        val popup = ShadowPopupMenu.getLatestPopupMenu()
        assertTrue(shadowOf(popup).onMenuItemClickListener.onMenuItemClick(popup.menu.findItem(3)))
        val secondHandler = Shadow.newInstanceOf(SslErrorHandler::class.java)
        webView.webViewClient.onReceivedSslError(webView, secondHandler, error)

        val secondFailure =
            activity.declared<ConnectionStatus?>("connectionStatus") as
                ConnectionStatus.ServerUnreachable
        assertNotSame(firstFailure, secondFailure)
        assertEquals(UnreachableKind.TLS, secondFailure.kind)
        listOf(firstHandler, secondHandler).forEach { handler ->
            val shadow = Shadow.extract<ShadowSslErrorHandler>(handler)
            assertTrue(shadow.wasCancelCalled())
            assertFalse(shadow.wasProceedCalled())
        }
    }

    @Test
    fun `main frame error details use numeric code and exclude callback query`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/oauth/callback?state=ERR_PRIVATESTATE&code=private-code"
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(url),
            FakeWebResourceError(-2, url),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(800))
        activity.findViewById<Button>(R.id.status_details_toggle).performClick()
        val rendered = activity.findViewById<TextView>(R.id.status_details_text).text.toString()
        activity.findViewById<Button>(R.id.status_copy_details).performClick()
        val copied =
            activity
                .getSystemService(ClipboardManager::class.java)
                .primaryClip!!
                .getItemAt(0)
                .text
                .toString()
        assertTrue(copied.startsWith(rendered))
        assertTrue(copied.contains("Android API\n"))
        assertTrue(rendered.contains("ERROR_HOST_LOOKUP"))
        assertFalse(rendered.contains("ERR_PRIVATESTATE"))
        assertFalse(rendered.contains("private-code"))
        assertFalse(rendered.contains("/oauth/callback"))
    }

    @Test
    fun `malformed WebView error description falls back to numeric code`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(url),
            FakeWebResourceError(-6, "net::ERR_FOO?x=1"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.findViewById<Button>(R.id.status_details_toggle).performClick()
        val details = activity.findViewById<TextView>(R.id.status_details_text).text.toString()
        assertTrue(details.contains("ERROR_CONNECT"))
        assertFalse(details.contains("ERR_FOO"))
        assertFalse(details.contains("x=1"))
        activity.findViewById<Button>(R.id.status_copy_details).performClick()
        val copied =
            activity
                .getSystemService(ClipboardManager::class.java)
                .primaryClip!!
                .getItemAt(0)
                .text
                .toString()
        assertTrue(copied.startsWith(details))
        assertTrue(copied.contains("Android API\n"))
    }

    @Test
    fun `successful commit makes retry change server and Back inert during the hold`() {
        var manifestReads = 0
        MainActivity.manifestReader = {
            manifestReads++
            ServerManifest.BASELINE
        }
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/chat/123"
        val status =
            ConnectionStatus.ServerUnreachable(
                "example.com",
                UnreachableKind.GENERIC,
                null,
            )
        shadowOf(activity).nextStartedActivity
        activity.invoke("showStatus", arrayOf(ConnectionStatus::class.java), status)
        activity.declared<ConnectionStatusView>("connectionOverlay").show(status, immediate = true)
        webView.loadUrl(url)
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onPageCommitVisible(webView, url)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        val before = manifestReads
        val page = shadowOf(webView).lastLoadedUrl
        activity.findViewById<Button>(R.id.status_primary).performClick()
        activity.findViewById<Button>(R.id.status_secondary).performClick()
        activity.onBackPressedDispatcher.onBackPressed()
        assertEquals(before, manifestReads)
        assertEquals(url, page)
        assertEquals(url, shadowOf(webView).lastLoadedUrl)
        assertNull(shadowOf(activity).nextStartedActivity)
        assertFalse(activity.isFinishing)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
    }

    @Test
    fun `successful commit makes Cancel inert during the hold`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        val status = ConnectionStatus.ConnectingSlow("example.com")
        shadowOf(activity).nextStartedActivity
        activity.invoke("showStatus", arrayOf(ConnectionStatus::class.java), status)
        activity.declared<ConnectionStatusView>("connectionOverlay").show(status, immediate = true)
        webView.loadUrl(url)
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onPageCommitVisible(webView, url)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        activity.findViewById<Button>(R.id.status_cancel).performClick()
        assertEquals(url, shadowOf(webView).lastLoadedUrl)
        assertNull(shadowOf(activity).nextStartedActivity)
        assertFalse(activity.isFinishing)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
    }

    @Test
    fun `fast page commit keeps the first connecting status visible for its minimum`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(501))
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onPageCommitVisible(webView, url)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(
            View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS,
            webView.importantForAccessibility,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(View.IMPORTANT_FOR_ACCESSIBILITY_AUTO, webView.importantForAccessibility)
    }

    @Test
    fun `rotation preserves the remaining successful commit hold`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(501))
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onPageCommitVisible(webView, url)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(200))
        val oldOverlay = activity.declared<View>("connectionOverlay")
        val originalDeadline =
            (oldOverlay as ConnectionStatusView).pendingHideSnapshot()!!.deadlineMillis

        activity.onConfigurationChanged(
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            },
        )

        val rebuilt = activity.declared<View>("connectionOverlay")
        assertNotSame(oldOverlay, rebuilt)
        assertEquals(View.VISIBLE, rebuilt.visibility)
        assertEquals(
            activity.getString(R.string.oidc_connecting),
            activity.findViewById<TextView>(R.id.status_headline).text.toString(),
        )
        assertEquals(
            View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS,
            webView.importantForAccessibility,
        )
        val remaining = originalDeadline - SystemClock.uptimeMillis()
        assertTrue(remaining > 1)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(remaining - 1))
        assertEquals(View.VISIBLE, rebuilt.visibility)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1))
        assertEquals(View.GONE, rebuilt.visibility)
        assertEquals(View.IMPORTANT_FOR_ACCESSIBILITY_AUTO, webView.importantForAccessibility)
    }

    @Test
    fun `same origin cold start redirect stays covered until final commit`() {
        val activity = launch()
        val webView = activity.webView()
        val final = "https://example.com/omnigent/c/123"
        assertEquals("https://example.com", shadowOf(webView).lastLoadedUrl)
        webView.webViewClient.onPageStarted(webView, final, null)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )
        webView.webViewClient.onPageCommitVisible(webView, final)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(500))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
        assertFalse(
            activity.declared<ConnectionStatus?>(
                "connectionStatus",
            ) is ConnectionStatus.ServerUnreachable,
        )
    }

    @Test
    fun `unvalidated network displays offline copy for a main frame failure`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(url),
            FakeWebResourceError(-6, "refused"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertEquals(
            activity.getString(R.string.status_unreachable_offline),
            activity.findViewById<TextView>(R.id.status_body).text.toString(),
        )
    }

    @Test
    fun `workspace sign in after expired session returns to the recovered page`() {
        val activity = launch()
        val fixture = WorkspaceFixture()
        activity.setDeclared("workspaceContext", fixture.root)
        activity.setDeclared("workspaceProfile", fixture.profile)
        activity.setDeclared(
            "workspaceCoordinator",
            fixture.coordinator {
                _,
                _,
                ->
                fixture.session
            },
        )
        activity.invoke(
            "beginWorkspaceConnection",
            arrayOf(
                DatabricksWebContext::class.java,
                DatabricksWebProfile::class.java,
                Boolean::class.javaPrimitiveType!!,
            ),
            fixture.page,
            fixture.profile,
            false,
        )
        awaitWorkspace(activity) {
            it.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.SignInRequired &&
                it.findViewById<Button>(R.id.status_primary).isEnabled
        }
        assertTrue(
            activity.declared<ConnectionStatus?>(
                "connectionStatus",
            ) is ConnectionStatus.SignInRequired,
        )

        assertTrue(activity.findViewById<Button>(R.id.status_primary).performClick())
        assertEquals(
            fixture.page,
            activity.declared<DatabricksWebContext?>("pendingRecoveryContext"),
        )
        fixture.hasGrant = true
        activity.invoke(
            "onNewIntent",
            arrayOf(Intent::class.java),
            Intent(activity, MainActivity::class.java).apply {
                putExtra(OAuthCallbackActivity.EXTRA_OAUTH_CALLBACK, true)
            },
        )
        awaitWorkspace(activity) {
            it.declared<URI?>("lastWorkspacePageUri") == fixture.page.pageUri
        }
        assertEquals(fixture.page.pageUri, activity.declared<URI?>("lastWorkspacePageUri"))
        assertEquals(fixture.page.pageUri.toString(), shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `workspace coordinator network failure shows server unreachable`() {
        val activity = launch()
        val fixture = WorkspaceFixture().apply { hasGrant = true }
        activity.setDeclared(
            "workspaceCoordinator",
            fixture.coordinator { _, _ ->
                throw DatabricksSessionException.NetworkUnavailable()
            },
        )
        activity.invoke(
            "beginWorkspaceConnection",
            arrayOf(
                DatabricksWebContext::class.java,
                DatabricksWebProfile::class.java,
                Boolean::class.javaPrimitiveType!!,
            ),
            fixture.page,
            fixture.profile,
            false,
        )
        awaitWorkspace(activity) {
            it.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.ServerUnreachable
        }
        assertTrue(
            activity.declared<ConnectionStatus?>(
                "connectionStatus",
            ) is ConnectionStatus.ServerUnreachable,
        )
    }

    @Test
    fun `workspace coordinator rejected failure shows workspace failure`() {
        val activity = launch()
        val fixture = WorkspaceFixture().apply { hasGrant = true }
        activity.setDeclared(
            "workspaceCoordinator",
            fixture.coordinator { _, _ ->
                throw DatabricksSessionException.Rejected(403)
            },
        )
        activity.invoke(
            "beginWorkspaceConnection",
            arrayOf(
                DatabricksWebContext::class.java,
                DatabricksWebProfile::class.java,
                Boolean::class.javaPrimitiveType!!,
            ),
            fixture.page,
            fixture.profile,
            false,
        )
        awaitWorkspace(activity) {
            it.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.WorkspaceFailure
        }
        assertTrue(
            activity.declared<ConnectionStatus?>(
                "connectionStatus",
            ) is ConnectionStatus.WorkspaceFailure,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.findViewById<Button>(R.id.status_details_toggle).performClick()
        val details = activity.findViewById<TextView>(R.id.status_details_text).text.toString()
        assertTrue(details, details.contains("Rejected"))
        assertFalse(details.contains("HTTP 403"))
    }

    @Test
    fun `change server from failure opens setup and action Back finishes`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(url),
            FakeWebResourceError(-6, "refused"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.onBackPressedDispatcher.onBackPressed()
        assertTrue(activity.isFinishing)

        val another = launch()
        val anotherWebView = another.webView()
        anotherWebView.webViewClient.onPageStarted(anotherWebView, url, null)
        anotherWebView.webViewClient.onReceivedError(
            anotherWebView,
            request(url),
            FakeWebResourceError(-6, "refused"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        another.findViewById<Button>(R.id.status_secondary).performClick()
        assertEquals(
            ConnectActivity::class.java.name,
            shadowOf(another).nextStartedActivity.component?.className,
        )
    }

    @Test
    fun `a slow cold start offers change server and Back cancels`() {
        val activity = launch()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(10_500))

        assertEquals(
            activity.getString(R.string.status_still_trying, "example.\u200bcom"),
            activity.findViewById<TextView>(R.id.status_headline).text.toString(),
        )
        assertEquals(View.VISIBLE, activity.findViewById<Button>(R.id.status_secondary).visibility)
        assertEquals(View.VISIBLE, activity.findViewById<Button>(R.id.status_cancel).visibility)

        activity.onBackPressedDispatcher.onBackPressed()
        assertEquals(
            ConnectActivity::class.java.name,
            shadowOf(activity).nextStartedActivity.component?.className,
        )
    }

    @Test
    fun `cold start becomes slow ten seconds after the first Connecting`() {
        val pending = mutableListOf<Runnable>()
        MainActivity.oidcWorker = Executor { pending += it }
        val activity = launch()
        assertEquals(1, pending.size)
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(5_000))
        pending.single().run()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(4_900))
        assertFalse(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(200))
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )
    }

    @Test
    fun `post sign in page load becomes slow connecting after ten seconds`() {
        val activity = launch()
        val host =
            Class
                .forName("ai.omnigent.android.MainActivity\$OidcHost")
                .getDeclaredConstructor(MainActivity::class.java)
                .apply { isAccessible = true }
                .newInstance(activity) as OidcSessionController.Host
        val page = "https://example.com/omnigent/c/recovered"
        host.showProgress(OidcSessionController.Progress.COMPLETING)
        host.loadPage(page)
        assertEquals(page, shadowOf(activity.webView()).lastLoadedUrl)

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(9_900))
        assertFalse(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )
        assertEquals(View.VISIBLE, activity.findViewById<Button>(R.id.status_secondary).visibility)
        assertEquals(
            activity.getString(R.string.status_change_server),
            activity.findViewById<Button>(R.id.status_secondary).text.toString(),
        )
    }

    @Test
    fun `legacy page load never resets slow connecting to connecting`() {
        val activity = launch()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(10_500))
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )
        val host =
            Class
                .forName("ai.omnigent.android.MainActivity\$OidcHost")
                .getDeclaredConstructor(MainActivity::class.java)
                .apply { isAccessible = true }
                .newInstance(activity) as OidcSessionController.Host

        host.loadWithoutNativeSignIn("https://example.com/after-manifest")

        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )
        assertEquals(View.VISIBLE, activity.findViewById<Button>(R.id.status_secondary).visibility)
    }

    @Test
    fun `night configuration rebinds an expanded failure panel`() {
        val activity = launch()
        val webView = activity.webView()
        val url = "https://example.com/"
        webView.webViewClient.onPageStarted(webView, url, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(url),
            FakeWebResourceError(-6, "refused"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.findViewById<Button>(R.id.status_details_toggle).performClick()
        val before = activity.declared<View>("connectionOverlay")
        val dark =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }

        activity.onConfigurationChanged(dark)

        assertNotSame(before, activity.declared<View>("connectionOverlay"))
        assertEquals(
            View.VISIBLE,
            activity.findViewById<View>(R.id.status_details_content).visibility,
        )
        assertEquals(
            activity.getString(R.string.status_server_unreachable),
            activity.findViewById<TextView>(R.id.status_headline).text.toString(),
        )
    }

    @Test
    fun `old deferred hide cannot expose WebView after configuration failure`() {
        val activity = launch()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        activity.invoke("hideStatus")
        val oldOverlay = activity.declared<View>("connectionOverlay")
        val dark =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(dark)
        activity.invoke(
            "showUnreachable",
            arrayOf(String::class.java, Boolean::class.javaPrimitiveType!!),
            "ERROR_CONNECT",
            false,
        )

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))

        assertNotSame(oldOverlay, activity.declared<View>("connectionOverlay"))
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ServerUnreachable,
        )
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(
            View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS,
            activity.webView().importantForAccessibility,
        )
    }

    @Before
    fun readManifestsWithoutTheNetwork() {
        MainActivity.manifestReader = { ServerManifest.BASELINE }
        MainActivity.oidcWorker = Executor { it.run() }
    }

    @After
    fun restoreManifestReader() {
        MainActivity.manifestReader = { ServerManifest.fetch(it) }
        MainActivity.oidcWorker = null
        OidcCredentials::class.java.getDeclaredField("shared").apply {
            isAccessible = true
            set(null, null)
        }
    }

    @Test
    fun `a server offering native sign-in stays covered until it has a session`() {
        MainActivity.manifestReader = {
            ServerManifest(
                1.0,
                ServerManifest.Auth(
                    ServerManifest.Mode.OIDC,
                    "__Host-ap_session",
                    listOf(OidcRedirect.REDIRECT_URI),
                ),
            )
        }
        ServerStore(
            ApplicationProvider.getApplicationContext(),
        ).connect("https://native.example.test")

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // Nothing of the server loads; a prompt blanks the page underneath.
        assertTrue(shadowOf(activity.webView()).lastLoadedUrl in setOf(null, "about:blank"))
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        // Still renewing, or asking: a relaunch never opens the browser by itself.
        assertTrue(
            activity.findViewById<TextView>(R.id.status_headline).text.toString() in
                setOf(
                    activity.getString(R.string.oidc_signing_in),
                    activity.getString(R.string.status_sign_in_required),
                ),
        )
    }

    @Test
    fun `switching to a server offering native sign-in stops the previous page under the cover`() {
        MainActivity.manifestReader = { url ->
            if (url.contains("native")) {
                ServerManifest(
                    1.0,
                    ServerManifest.Auth(
                        ServerManifest.Mode.OIDC,
                        "__Host-ap_session",
                        listOf(OidcRedirect.REDIRECT_URI),
                    ),
                )
            } else {
                ServerManifest.BASELINE
            }
        }
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://native.example.test")
        val activity = launch()
        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)

        activity.invoke(
            "onSwitchServerRequested",
            arrayOf(String::class.java),
            "https://native.example.test",
        )

        assertEquals("about:blank", shadowOf(activity.webView()).lastLoadedUrl)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
    }

    @Test
    fun `a server without native sign-in remains covered until page commit`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        val webView = activity.webView()
        webView.webViewClient.onPageStarted(webView, "https://example.com/", null)
        webView.webViewClient.onPageCommitVisible(webView, "https://example.com/")
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
    }

    @Test
    fun `Databricks App connecting uncovers only a committed foreign sign-in page`() {
        ServerStore(ApplicationProvider.getApplicationContext())
            .connect("https://myapp.databricksapps.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val webView = activity.webView()
        val loginUrl = "https://login.databricks.com/sign-in"

        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        webView.webViewClient.onPageStarted(webView, loginUrl, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(loginUrl),
            FakeWebResourceError(-6, "net::ERR_CONNECTION_REFUSED"),
        )
        webView.webViewClient.onPageCommitVisible(webView, loginUrl)
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )

        webView.webViewClient.onPageStarted(webView, loginUrl, null)
        val sslHandler = Shadow.newInstanceOf(SslErrorHandler::class.java)
        webView.webViewClient.onReceivedSslError(
            webView,
            sslHandler,
            SslError(
                SslError.SSL_UNTRUSTED,
                SslCertificate("host", "issuer", java.util.Date(), java.util.Date()),
                loginUrl,
            ),
        )
        webView.webViewClient.onPageCommitVisible(webView, loginUrl)
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )
        val sslShadow = Shadow.extract<ShadowSslErrorHandler>(sslHandler)
        assertTrue(sslShadow.wasCancelCalled())
        assertFalse(sslShadow.wasProceedCalled())

        webView.webViewClient.onPageStarted(webView, loginUrl, null)
        webView.webViewClient.onPageCommitVisible(webView, loginUrl)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(10_500))

        assertNull(activity.declared<ConnectionStatus?>("connectionStatus"))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(View.IMPORTANT_FOR_ACCESSIBILITY_AUTO, webView.importantForAccessibility)
    }

    @Test
    fun `Databricks App stalled load reaches slow and Retry stays covered until commit`() {
        ServerStore(ApplicationProvider.getApplicationContext())
            .connect("https://myapp.databricksapps.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val webView = activity.webView()
        val page = "https://myapp.databricksapps.com/"

        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(10_500))
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ConnectingSlow,
        )
        assertEquals(View.VISIBLE, activity.findViewById<Button>(R.id.status_secondary).visibility)
        assertEquals(View.VISIBLE, activity.findViewById<Button>(R.id.status_cancel).visibility)

        webView.webViewClient.onPageStarted(webView, page, null)
        webView.webViewClient.onReceivedError(
            webView,
            request(page),
            FakeWebResourceError(-6, "net::ERR_CONNECTION_REFUSED"),
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is
                ConnectionStatus.ServerUnreachable,
        )
        activity.findViewById<Button>(R.id.status_primary).performClick()
        assertTrue(
            activity.declared<ConnectionStatus?>("connectionStatus") is ConnectionStatus.Connecting,
        )
        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        assertEquals(
            View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS,
            webView.importantForAccessibility,
        )

        webView.webViewClient.onPageStarted(webView, page, null)
        webView.webViewClient.onPageCommitVisible(webView, page)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertEquals(View.GONE, activity.declared<View>("connectionOverlay").visibility)
    }

    @Test
    fun `main-frame http error opens server selection once and finishes`() {
        val activity = launch()
        val webView = activity.webView()
        val request = request("https://example.com/app")
        val response = httpError(503)
        val activityShadow = shadowOf(activity)
        activityShadow.clearNextStartedActivities()

        webView.webViewClient.onReceivedHttpError(webView, request, response)

        assertEquals(
            ConnectActivity::class.java.name,
            activityShadow.nextStartedActivity.component?.className,
        )
        assertTrue(activity.isFinishing)

        webView.webViewClient.onReceivedHttpError(webView, request, response)

        assertNull(activityShadow.nextStartedActivity)
    }

    @Test
    fun `http error after activity starts finishing does not reopen connect`() {
        val activity = launch()
        val webView = activity.webView()
        val activityShadow = shadowOf(activity)
        activityShadow.clearNextStartedActivities()
        activity.finish()

        webView.webViewClient.onReceivedHttpError(
            webView,
            request("https://example.com/app"),
            httpError(503),
        )

        assertNull(activityShadow.nextStartedActivity)
    }

    @Test
    fun `workspace without WebView profile support fails closed before WebView load`() {
        val workspace = "https://dbc-123.cloud.databricks.com/omnigent?o=42"
        ServerStore(ApplicationProvider.getApplicationContext()).connect(workspace)

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertTrue(activity.isFinishing)
        val setup = shadowOf(activity).nextStartedActivity
        assertEquals(ConnectActivity::class.java.name, setup.component!!.className)
        assertEquals(workspace, setup.getStringExtra(ConnectActivity.EXTRA_SERVER_URL))
        // Robolectric's WebView has no multi-profile support for workspace cookies.
        val error = setup.getStringExtra(ConnectActivity.EXTRA_ERROR)!!
        assertTrue(error.contains("Update Android System WebView"))
    }

    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.webView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)
        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)

        val lightConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_NO
            }
        activity.onConfigurationChanged(lightConfiguration)
        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `navigation hides the server pill until the watchdog expires`() {
        val activity = launch()
        val webView = activity.webView()
        val pill = activity.switchButton()

        webView.webViewClient.onPageStarted(webView, "https://example.com/app", null)
        assertEquals(View.GONE, pill.visibility)

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
        assertEquals(View.VISIBLE, pill.visibility)
    }

    @Test
    fun `picker requests cancel the watchdog and hide a visible fallback`() {
        val activity = launch()
        val webView = activity.webView()

        webView.webViewClient.onPageStarted(webView, "https://example.com/app", null)
        activity.invoke("onServerPickerRequested")
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
        assertEquals(View.GONE, activity.switchButton().visibility)

        webView.webViewClient.onPageStarted(webView, "https://example.com/app", null)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
        assertEquals(View.VISIBLE, activity.switchButton().visibility)

        activity.invoke("onServerPickerRequested")

        assertEquals(View.GONE, activity.switchButton().visibility)
        val script = shadowOf(webView).lastEvaluatedJavascript.orEmpty()
        assertTrue(script.contains("__omnigentNativeEmitServerPicker"))
        assertTrue(script.contains("currentOrigin") && script.contains("example.com"))
        // Only a server whose sign-in the shell owns offers "Sign out of <server>".
        assertTrue(script.contains("\"canSignOut\":false"))
    }

    @Test
    fun `sign-out the shell can't perform answers the page false`() {
        val activity = launch()
        val webView = activity.webView()

        activity.invoke("onSignOutOfServerRequested")

        assertEquals(
            "window.__omnigentNativeEmitSignOutResult && " +
                "window.__omnigentNativeEmitSignOutResult(false);",
            shadowOf(webView).lastEvaluatedJavascript,
        )
        assertFalse(activity.isFinishing)
    }

    @Test
    fun `server menu items above the server entries are never treated as servers`() {
        val activity = launch()
        activity.invoke(
            "showServerSwitcherMenu",
            arrayOf(View::class.java),
            activity.switchButton(),
        )
        val popup = ShadowPopupMenu.getLatestPopupMenu()
        val listener = shadowOf(popup).onMenuItemClickListener

        // The debug submenus use ids far above the server entries.
        val debugRoot = popup.menu.add(3, 20_100, 0, "Debug Authentication")

        assertFalse(listener.onMenuItemClick(debugRoot))
        assertEquals(
            "https://example.com",
            ServerStore(ApplicationProvider.getApplicationContext()).currentServerUrl(),
        )
    }

    @Test
    fun `server switches accept only picker-offered URLs`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://second.example.test")
        val activity = launch()

        activity.invoke(
            "onSwitchServerRequested",
            arrayOf(String::class.java),
            "https://unlisted.example.test",
        )
        assertEquals("https://example.com", ServerStore(context).currentServerUrl())

        activity.invoke(
            "onSwitchServerRequested",
            arrayOf(String::class.java),
            "https://second.example.test",
        )
        assertEquals("https://second.example.test", ServerStore(context).currentServerUrl())
        assertEquals("https://second.example.test", shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `server pill tracks the container width and truncates in the middle`() {
        val activity = launch()
        val pill = activity.switchButton() as TextView
        val container = pill.parent as ViewGroup
        val density = activity.resources.displayMetrics.density

        container.layout(0, 0, (300 * density).toInt(), (800 * density).toInt())
        assertEquals((120 * density).toInt(), pill.maxWidth)

        container.layout(0, 0, (400 * density).toInt(), (800 * density).toInt())
        assertEquals((152 * density).toInt(), pill.maxWidth)

        container.layout(0, 0, (500 * density).toInt(), (800 * density).toInt())
        assertEquals((172 * density).toInt(), pill.maxWidth)
        assertEquals(TextUtils.TruncateAt.MIDDLE, pill.ellipsize)
    }

    @Test
    fun `renderer death swaps in a fresh WebView and reloads the server`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dead = activity.webView()
        val container = dead.parent as ViewGroup

        val handled = dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        assertTrue(handled)
        val replacement = activity.webView()
        assertNotSame(dead, replacement)
        assertSame(container, replacement.parent)
        assertEquals("https://example.com", shadowOf(replacement).lastLoadedUrl)
    }

    @Test
    fun `a late renderer-death report for a replaced WebView is ignored`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val first = activity.webView()
        first.webViewClient.onRenderProcessGone(first, rendererGone())
        val replacement = activity.webView()

        // The stale report names the WebView that was already torn down.
        replacement.webViewClient.onRenderProcessGone(first, rendererGone())

        assertSame(replacement, activity.webView())
        assertFalse(shadowOf(replacement).wasDestroyCalled())
    }

    @Test
    fun `renderer death reloads the last route, not the server root`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dead = activity.webView()
        // The user was on a deep same-origin route when the renderer died;
        // getUrl() reports the last-committed URL, which survives the death.
        dead.loadUrl("https://example.com/chat/abc123")

        dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        // The rebuilt WebView returns to the route, not the landing page.
        assertEquals(
            "https://example.com/chat/abc123",
            shadowOf(activity.webView()).lastLoadedUrl,
        )
    }

    @Test
    fun `a foreign last URL falls back to the server root on recovery`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dead = activity.webView()
        // A foreign / error origin must not be restored into — reload the server.
        dead.loadUrl("https://accounts.google.com/o/oauth2/v2/auth")

        dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `a renderer crash loop stops auto-reloading but still rebuilds a live WebView`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // MAX_RENDERER_CRASHES + 1 clustered crashes. Each still rebuilds (manual
        // recovery reuses the webView field, so the corpse can't stay); the loop
        // is broken by what loads, not by refusing to rebuild.
        var dead = activity.webView()
        repeat(4) {
            dead.loadUrl("https://example.com/chat/loops")
            val container = dead.parent as ViewGroup
            dead.webViewClient.onRenderProcessGone(dead, rendererGone(crashed = true))
            val rebuilt = activity.webView()
            // Every death — even the over-budget one — yields a fresh, attached,
            // non-destroyed WebView, so manual recovery always has a live target.
            assertNotSame(dead, rebuilt)
            assertSame(container, rebuilt.parent)
            assertFalse(shadowOf(rebuilt).wasDestroyCalled())
            dead = rebuilt
        }

        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertEquals(
            activity.getString(R.string.status_crash_recovery),
            activity.findViewById<TextView>(R.id.status_headline).text.toString(),
        )
    }

    @Test
    fun `a load-then-crash loop still trips the budget despite successful page loads`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // Common crash shape: page loads fine, then crashes seconds later. A
        // page-load reset would clear the budget each cycle and never trip; the
        // gap-based budget must accumulate these clustered crashes.
        var dead = activity.webView()
        repeat(4) {
            dead.loadUrl("https://example.com/chat/heavy")
            // Simulate the successful load that precedes each crash.
            dead.webViewClient.onPageFinished(dead, "https://example.com/chat/heavy")
            dead.webViewClient.onRenderProcessGone(dead, rendererGone(crashed = true))
            dead = activity.webView()
        }

        assertEquals(View.VISIBLE, activity.declared<View>("connectionOverlay").visibility)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(600))
        assertEquals(
            activity.getString(R.string.status_crash_recovery),
            activity.findViewById<TextView>(R.id.status_headline).text.toString(),
        )
    }

    @Test
    fun `system reclaims never exhaust the crash budget`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // Far more than MAX_RENDERER_CRASHES, but all system reclaims
        // (didCrash=false): each must recover, none counts against the budget.
        repeat(6) {
            val live = activity.webView()
            live.webViewClient.onRenderProcessGone(live, rendererGone(crashed = false))
        }
        val latest = activity.webView()

        latest.webViewClient.onRenderProcessGone(latest, rendererGone(crashed = false))
        assertNotSame(latest, activity.webView())
    }

    private fun rendererGone(crashed: Boolean = false) =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = crashed

            override fun rendererPriorityAtExit(): Int = WebView.RENDERER_PRIORITY_IMPORTANT
        }

    private fun request(url: String) =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)

            override fun isForMainFrame(): Boolean = true

            override fun isRedirect(): Boolean = false

            override fun hasGesture(): Boolean = false

            override fun getMethod(): String = "GET"

            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private fun httpError(statusCode: Int) =
        WebResourceResponse(
            "text/html",
            "UTF-8",
            statusCode,
            "HTTP error",
            emptyMap(),
            null,
        )

    @Test
    fun `a managed preset never overrides the server the user picked`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")
        val manager = context.getSystemService(RestrictionsManager::class.java)
        Shadow
            .extract<ShadowRestrictionsManager>(manager)
            .setApplicationRestrictions(
                Bundle().apply {
                    putString(ManagedConfig.KEY_SERVER_URLS, "https://managed.example.com")
                },
            )

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun launch(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    private fun awaitMainThread(done: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10)
        while (!done() && System.nanoTime() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            Thread.sleep(5)
        }
        assertTrue("background result did not reach the main thread", done())
    }

    @Suppress("UNCHECKED_CAST")
    private fun <T> MainActivity.declared(name: String): T =
        MainActivity::class
            .java
            .getDeclaredField(name)
            .apply { isAccessible = true }
            .get(this) as T

    private fun MainActivity.switchButton(): View =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as View

    private fun MainActivity.invoke(
        name: String,
        parameterTypes: Array<Class<*>> = emptyArray(),
        vararg args: Any,
    ) {
        MainActivity::class
            .java
            .getDeclaredMethod(name, *parameterTypes)
            .apply { isAccessible = true }
            .invoke(this, *args)
    }

    private fun MainActivity.setDeclared(
        name: String,
        value: Any?,
    ) {
        MainActivity::class.java
            .getDeclaredField(
                name,
            ).apply { isAccessible = true }
            .set(this, value)
    }

    private fun awaitWorkspace(
        activity: MainActivity,
        settled: (MainActivity) -> Boolean,
    ) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10)
        while (System.nanoTime() < deadline) {
            shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(10))
            if (settled(activity)) return
            Thread.sleep(10)
        }
        val future = activity.declared<CompletableFuture<DatabricksWebSession>?>("workspaceFuture")
        error(
            "Workspace UI did not settle: futureDone=${future?.isDone}, " +
                "status=${activity.declared<ConnectionStatus?>("connectionStatus")}",
        )
    }

    private class WorkspaceFixture {
        private val config =
            DatabricksOAuthConfiguration(
                "public-client",
                URI("https://login.databricks.com/mobile-redirect"),
            )
        private val rootUri = URI("https://dbc-123.cloud.databricks.com/omnigent?o=42")
        val root =
            DatabricksWebContext(
                config,
                DatabricksCredentialScope.from(rootUri, config),
                rootUri,
            )
        val page =
            root.navigatingTo(
                URI("https://dbc-123.cloud.databricks.com/omnigent/c/my-page?o=42"),
            )
        private val cookies = mutableMapOf<String, String>()
        val profile =
            DatabricksWebProfile(
                root.profileName,
                object : WebProfileBackend {
                    override fun setAcceptCookie(accept: Boolean) = Unit

                    override fun removeAllCookies(callback: (Boolean) -> Unit) {
                        cookies.clear()
                        callback(true)
                    }

                    override fun setCookie(
                        url: String,
                        value: String,
                        callback: (Boolean) -> Unit,
                    ) {
                        val pair = value.substringBefore(';').split('=', limit = 2)
                        cookies[pair[0]] = pair[1]
                        callback(true)
                    }

                    override fun getCookie(url: String): String =
                        cookies.entries.joinToString("; ") {
                            "${it.key}=${it.value}"
                        }

                    override fun flushCookies() = Unit

                    override fun clearWebStorage() = Unit
                },
            )
        var hasGrant = false
        private val token =
            DatabricksOAuthTokens(
                "access",
                "refresh",
                Long.MAX_VALUE,
                DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc")),
            )
        val session =
            DatabricksWebSession(
                page.pageUri,
                listOf(
                    SessionCookie(
                        "DBAUTH",
                        "session",
                        "dbc-123.cloud.databricks.com",
                        "/",
                        true,
                        true,
                        true,
                        -1,
                        rootUri,
                        "DBAUTH=session; Path=/; Secure; HttpOnly",
                    ),
                ),
                setOf("https://dbc-123.cloud.databricks.com"),
                config,
                "42",
            )

        fun coordinator(create: DatabricksSessionCreating): DatabricksWorkspaceCoordinator {
            val store =
                object : DatabricksCredentialStorage {
                    override fun load(scope: DatabricksCredentialScope) =
                        if (hasGrant) token else null

                    override fun save(
                        scope: DatabricksCredentialScope,
                        credential: DatabricksOAuthTokens,
                    ) = Unit

                    override fun delete(scope: DatabricksCredentialScope) = Unit
                }
            return DatabricksWorkspaceCoordinator(
                ApplicationProvider.getApplicationContext(),
                DatabricksTokenManager(store),
                create,
                Executors.newSingleThreadExecutor(),
            )
        }
    }
}
