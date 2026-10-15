package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI

@RunWith(RobolectricTestRunner::class)
class ServerEndpointTest {
    @Test
    fun `routes sit under the server's mount without its query or fragment`() {
        assertEquals(URI("https://h/auth/login"), serverEndpoint("https://h", "/auth/login"))
        assertEquals(URI("https://h/auth/login"), serverEndpoint("https://h/", "/auth/login"))
        assertEquals(
            URI("https://h/omnigent/v1/me"),
            serverEndpoint("https://h/omnigent/", "/v1/me"),
        )
        assertEquals(
            URI("http://10.0.2.2:6891/proxy/6891/oauth/token"),
            serverEndpoint("http://10.0.2.2:6891/proxy/6891?o=1#chat", "/oauth/token"),
        )
        assertEquals(URI("http://[::1]:8000/v1/me"), serverEndpoint("http://[::1]:8000", "/v1/me"))
    }

    @Test
    fun `only absolute routes on http servers without userinfo resolve`() {
        assertNull(serverEndpoint("https://h", "auth/login"))
        assertNull(serverEndpoint("ftp://h", "/auth/login"))
        assertNull(serverEndpoint("https://user@h", "/auth/login"))
        assertNull(serverEndpoint("not a url", "/auth/login"))
    }
}
