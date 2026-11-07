package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Test
import java.net.URI

class WorkspaceSessionProbeTest {
    private val page = URI("https://dbc-123.cloud.databricks.com/omnigent/c/chat-1?o=42#latest")

    @Test
    fun `accepted session probes the workspace identity endpoint with the DBAUTH cookie`() {
        val transport = RecordingTransport(200)

        val check = WorkspaceSessionProbe(transport).check(page, "session-value")

        assertEquals(WorkspaceSessionCheck.Valid, check)
        val request = transport.requests.single()
        assertEquals(URI("https://dbc-123.cloud.databricks.com/omnigent/v1/me?o=42"), request.uri)
        assertEquals("DBAUTH=session-value", request.headers["Cookie"])
    }

    @Test
    fun `401 means the session expired and recovery should run without a status`() {
        assertEquals(
            WorkspaceSessionCheck.Rejected(null),
            WorkspaceSessionProbe(RecordingTransport(401)).check(page, "value"),
        )
    }

    @Test
    fun `403 is reported so the shell shows the permission error`() {
        assertEquals(
            WorkspaceSessionCheck.Rejected(403),
            WorkspaceSessionProbe(RecordingTransport(403)).check(page, "value"),
        )
    }

    @Test
    fun `redirect to the workspace sign-in page counts as an expired session`() {
        val transport =
            RecordingTransport(
                302,
                mapOf("Location" to listOf("/login.html?next=%2Fomnigent")),
            )

        assertEquals(
            WorkspaceSessionCheck.Rejected(null),
            WorkspaceSessionProbe(transport).check(page, "value"),
        )
    }

    @Test
    fun `redirect within the app is not a rejection`() {
        val transport =
            RecordingTransport(302, mapOf("Location" to listOf("/omnigent/elsewhere")))

        assertEquals(
            WorkspaceSessionCheck.Unknown,
            WorkspaceSessionProbe(transport).check(page, "value"),
        )
    }

    @Test
    fun `server errors keep the session`() {
        assertEquals(
            WorkspaceSessionCheck.Unknown,
            WorkspaceSessionProbe(RecordingTransport(503)).check(page, "value"),
        )
    }

    @Test
    fun `network failure keeps the session`() {
        val probe =
            WorkspaceSessionProbe { throw DatabricksSessionException.NetworkUnavailable() }

        assertEquals(WorkspaceSessionCheck.Unknown, probe.check(page, "value"))
    }

    @Test
    fun `any unexpected transport failure keeps the session`() {
        val probe = WorkspaceSessionProbe { throw IllegalStateException("bad stream") }

        assertEquals(WorkspaceSessionCheck.Unknown, probe.check(page, "value"))
    }

    @Test
    fun `opaque redirect target without a path is not a rejection`() {
        val transport = RecordingTransport(302, mapOf("Location" to listOf("mailto:someone")))

        assertEquals(
            WorkspaceSessionCheck.Unknown,
            WorkspaceSessionProbe(transport).check(page, "value"),
        )
    }

    private class RecordingTransport(
        private val status: Int,
        private val headers: Map<String, List<String>> = emptyMap(),
    ) : SessionTransport {
        val requests = mutableListOf<SessionHttpRequest>()

        override fun execute(request: SessionHttpRequest): SessionHttpResponse {
            requests += request
            return SessionHttpResponse(request.uri, status, headers)
        }
    }
}
