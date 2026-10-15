package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class ServerManifestTest {
    private val json = mapOf("Content-Type" to "application/json; charset=utf-8")

    @Test
    fun `reads an OIDC manifest at the origin with its cookie and native redirects`() {
        LocalHttpServer {
            LocalHttpServer.Response(
                200,
                """
                {"manifest_version": 1, "server_version": "0.18.0", "auth": {"mode": "oidc",
                 "session_cookie": "ap_session",
                 "native_redirect_uris": ["ai.omnigent.android:/oauth/callback", 7, null]}}
                """.trimIndent(),
                json,
            )
        }.use { server ->
            val manifest = ServerManifest.fetch("${server.origin}/omnigent/?o=1#chat")

            assertEquals(
                ServerManifest(
                    1.0,
                    ServerManifest.Auth(
                        ServerManifest.Mode.OIDC,
                        "ap_session",
                        listOf("ai.omnigent.android:/oauth/callback"),
                    ),
                ),
                manifest,
            )
            assertEquals(listOf("GET"), server.requests.map { it.method })
            assertEquals(listOf("/.well-known/omnigent.json"), server.requests.map { it.path })
        }
    }

    @Test
    fun `an auth block without native redirects lists none`() {
        val manifest = parse("""{"manifest_version": 1, "auth": {"mode": "accounts"}}""")

        assertEquals(
            ServerManifest.Auth(ServerManifest.Mode.ACCOUNTS, null, emptyList()),
            manifest.auth,
        )
    }

    @Test
    fun `unknown modes and cookie names are not trusted`() {
        assertNull(parse("""{"manifest_version": 1, "auth": {"mode": "magic"}}""").auth)
        assertNull(parse("""{"manifest_version": 1, "auth": "oidc"}""").auth)
        assertNull(
            parse(
                """{"manifest_version": 1, "auth": {"mode": "oidc", "session_cookie": "sid"}}""",
            ).auth!!.sessionCookie,
        )
    }

    @Test
    fun `the __Host- cookie is only kept for https servers`() {
        val body =
            """{"manifest_version": 2, "auth": {"mode": "oidc", "session_cookie": "__Host-ap_session"}}"""

        assertNull(parse(body, "http://omni.example").auth!!.sessionCookie)
        assertEquals("__Host-ap_session", parse(body, "https://omni.example").auth!!.sessionCookie)
    }

    @Test
    fun `anything short of a manifest is the baseline`() {
        listOf(
            "",
            "<html></html>",
            "{",
            "[]",
            """{"auth": {"mode": "oidc"}}""",
            """{"manifest_version": "1"}""",
            """{"manifest_version": true}""",
            """{"manifest_version": null}""",
        ).forEach { body ->
            assertEquals(body, ServerManifest.BASELINE, parse(body))
        }
    }

    @Test
    fun `failed responses are the baseline`() {
        val manifest = """{"manifest_version": 1, "auth": {"mode": "oidc"}}"""
        listOf(
            LocalHttpServer.Response(404, """{"detail": "Not Found"}""", json),
            LocalHttpServer.Response(500, manifest, json),
            LocalHttpServer.Response(200, manifest, mapOf("Content-Type" to "text/html")),
            LocalHttpServer.Response(200, manifest),
        ).forEach { response ->
            LocalHttpServer { response }.use { server ->
                assertEquals(
                    response.toString(),
                    ServerManifest.BASELINE,
                    ServerManifest.fetch(server.origin),
                )
            }
        }
    }

    @Test
    fun `a redirect is not followed`() {
        LocalHttpServer { request ->
            if (request.path == "/elsewhere.json") {
                LocalHttpServer.Response(200, """{"manifest_version": 1}""", json)
            } else {
                LocalHttpServer.Response(302, headers = mapOf("Location" to "/elsewhere.json"))
            }
        }.use { server ->
            assertEquals(ServerManifest.BASELINE, ServerManifest.fetch(server.origin))
            assertEquals(listOf("/.well-known/omnigent.json"), server.requests.map { it.path })
        }
    }

    @Test
    fun `a manifest still arriving at the deadline is the baseline`() {
        val manifest = """{"manifest_version": 1, "auth": {"mode": "oidc"}}"""
        val trickled = LocalHttpServer.Response(200, manifest, json, trickleMillis = 100)
        LocalHttpServer { trickled }.use { server ->
            assertEquals(
                ServerManifest.BASELINE,
                ServerManifest.fetch(server.origin, UrlConnectionOAuthTransport(timeoutMs = 500)),
            )
        }
    }

    @Test
    fun `an unreachable or non-http server is the baseline`() {
        LocalHttpServer.withDroppingServer { origin ->
            assertEquals(ServerManifest.BASELINE, ServerManifest.fetch(origin))
        }
        assertEquals(ServerManifest.BASELINE, ServerManifest.fetch("ftp://omni.example"))
        assertEquals(ServerManifest.BASELINE, ServerManifest.fetch("not a url"))
    }

    @Test
    fun `native sign-in needs OIDC, the app redirect and a session cookie`() {
        val redirect = "ai.omnigent.android:/oauth/callback"

        fun manifest(
            mode: ServerManifest.Mode,
            cookie: String?,
            redirects: List<String>,
        ) = ServerManifest(1.0, ServerManifest.Auth(mode, cookie, redirects))

        assertEquals(
            "ap_session",
            manifest(ServerManifest.Mode.OIDC, "ap_session", listOf(redirect))
                .nativeSignInCookie(redirect),
        )
        assertNull(
            manifest(
                ServerManifest.Mode.OIDC,
                "ap_session",
                listOf("ai.omnigent.ios:/oauth/callback"),
            ).nativeSignInCookie(redirect),
        )
        assertNull(
            manifest(ServerManifest.Mode.OIDC, null, listOf(redirect)).nativeSignInCookie(redirect),
        )
        assertNull(
            manifest(ServerManifest.Mode.ACCOUNTS, "ap_session", listOf(redirect))
                .nativeSignInCookie(redirect),
        )
        assertNull(ServerManifest.BASELINE.nativeSignInCookie(redirect))
    }

    private fun parse(
        body: String,
        serverUrl: String = "https://omni.example",
    ) = ServerManifest.parse(body.toByteArray(), serverUrl)
}
