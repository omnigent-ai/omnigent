package ai.omnigent.android

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.ServerSocket
import java.net.URI

@RunWith(RobolectricTestRunner::class)
class OAuthTransportTest {
    @Test
    fun `posts the body and returns status, body and content type`() {
        LocalHttpServer {
            LocalHttpServer.Response(
                201,
                """{"ok":true}""",
                mapOf("Content-Type" to "application/json"),
            )
        }.use { server ->
            val response =
                UrlConnectionOAuthTransport().execute(
                    OAuthHttpRequest(
                        URI("${server.origin}/oauth/token"),
                        method = "POST",
                        headers = mapOf("Content-Type" to "application/x-www-form-urlencoded"),
                        body =
                            OAuthSupport.formBody(
                                listOf(
                                    "grant_type" to "refresh_token",
                                    "a b" to "c&d",
                                ),
                            ),
                    ),
                )

            assertEquals(201, response.status)
            assertArrayEquals("""{"ok":true}""".toByteArray(), response.body)
            assertEquals("application/json", response.contentType)
            val sent = server.requests.single()
            assertEquals("POST", sent.method)
            assertEquals("/oauth/token", sent.path)
            assertEquals("application/x-www-form-urlencoded", sent.headers["content-type"])
            assertEquals("grant_type=refresh_token&a%20b=c%26d", sent.body)
            assertNull(sent.headers["cookie"])
        }
    }

    @Test
    fun `a redirect is the final response`() {
        LocalHttpServer {
            LocalHttpServer.Response(
                302,
                headers = mapOf("Location" to "https://idp.example/login"),
            )
        }.use { server ->
            val response =
                UrlConnectionOAuthTransport().execute(
                    OAuthHttpRequest(URI("${server.origin}/v1/me")),
                )

            assertEquals(302, response.status)
            assertEquals(URI("${server.origin}/v1/me"), response.uri)
            assertEquals(1, server.requests.size)
        }
    }

    @Test
    fun `a server that trickles its response is cut off at the deadline`() {
        LocalHttpServer {
            LocalHttpServer.Response(200, "x".repeat(40), trickleMillis = 100)
        }.use { server ->
            val started = System.nanoTime()

            assertThrows(OAuthNetworkException::class.java) {
                UrlConnectionOAuthTransport(timeoutMs = 500).execute(
                    OAuthHttpRequest(URI("${server.origin}/v1/me")),
                )
            }

            // Every byte arrives well within the read timeout; only the overall deadline stops it.
            val elapsedMs = (System.nanoTime() - started) / 1_000_000
            assertTrue("took $elapsedMs ms", elapsedMs < 2_000)
        }
    }

    @Test
    fun `a body that stalls mid read is cut off at the deadline`() {
        // Headers and one byte arrive just before the deadline; then the server goes quiet.
        LocalHttpServer {
            LocalHttpServer.Response(200, "x".repeat(40), stallAfterMillis = 800)
        }.use { server ->
            val started = System.nanoTime()

            assertThrows(OAuthNetworkException::class.java) {
                UrlConnectionOAuthTransport(timeoutMs = 1_000).execute(
                    OAuthHttpRequest(URI("${server.origin}/v1/me")),
                )
            }

            // Waiting out the read timeout too would take about 1.8 s.
            val elapsedMs = (System.nanoTime() - started) / 1_000_000
            assertTrue("took $elapsedMs ms", elapsedMs < 1_500)
        }
    }

    @Test
    fun `a response larger than the limit fails`() {
        LocalHttpServer { LocalHttpServer.Response(200, "x".repeat(4_096)) }.use { server ->
            assertThrows(OAuthNetworkException::class.java) {
                UrlConnectionOAuthTransport(maxBodyBytes = 1_024).execute(
                    OAuthHttpRequest(URI("${server.origin}/.well-known/omnigent.json")),
                )
            }
            val response =
                UrlConnectionOAuthTransport(maxBodyBytes = 4_096).execute(
                    OAuthHttpRequest(URI("${server.origin}/.well-known/omnigent.json")),
                )
            assertEquals(4_096, response.body.size)
        }
    }

    @Test
    fun `no HTTP response is a network exception`() {
        LocalHttpServer.withDroppingServer { origin ->
            assertThrows(OAuthNetworkException::class.java) {
                UrlConnectionOAuthTransport().execute(OAuthHttpRequest(URI("$origin/v1/me")))
            }
        }
    }

    @Test
    fun `a POST to an unreachable host is a network exception`() {
        // A port that was just released refuses the connection the body write opens.
        val port = ServerSocket(0).use { it.localPort }
        val request =
            OAuthHttpRequest(
                uri = URI("http://127.0.0.1:$port/oidc/v1/token"),
                method = "POST",
                headers = mapOf("Content-Type" to "application/x-www-form-urlencoded"),
                body = "grant_type=refresh_token".toByteArray(),
            )

        assertThrows(OAuthNetworkException::class.java) {
            UrlConnectionOAuthTransport().execute(request)
        }
    }
}
