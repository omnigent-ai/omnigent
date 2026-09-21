package ai.omnigent.android

import org.junit.Assert.assertThrows
import org.junit.Test
import java.net.ServerSocket
import java.net.URI

class UrlConnectionOAuthTransportTest {
    @Test
    fun `a POST to an unreachable host reports the network as unavailable`() {
        // A port that was just released refuses the connection the body write opens.
        val port = ServerSocket(0).use { it.localPort }
        val request =
            OAuthHttpRequest(
                uri = URI("http://127.0.0.1:$port/oidc/v1/token"),
                method = "POST",
                headers = mapOf("Content-Type" to "application/x-www-form-urlencoded"),
                body = "grant_type=refresh_token".toByteArray(),
            )

        assertThrows(DatabricksOAuthException.NetworkUnavailable::class.java) {
            UrlConnectionOAuthTransport().execute(request)
        }
    }
}
