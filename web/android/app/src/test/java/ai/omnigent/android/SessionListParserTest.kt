package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * Unit tests for the `GET /v1/sessions` response parser. Runs under Robolectric
 * because the parser uses `org.json`, which is an unmocked Android stub in a
 * plain JVM unit test — Robolectric supplies the real implementation. No device.
 */
@RunWith(RobolectricTestRunner::class)
class SessionListParserTest {
    @Test
    fun `parses the documented list shape`() {
        val body =
            """
            {
              "object": "list",
              "data": [
                {"id": "conv_a", "agent_id": "ag", "status": "idle", "created_at": 1, "updated_at": 2,
                 "title": "First", "pending_elicitations_count": 0},
                {"id": "conv_b", "agent_id": "ag", "status": "waiting", "created_at": 1, "updated_at": 3,
                 "pending_elicitations_count": 2}
              ],
              "first_id": "conv_a", "last_id": "conv_b", "has_more": false
            }
            """.trimIndent()
        val sessions = parseSessionList(body)
        assertEquals(2, sessions.size)
        assertEquals(SessionState("conv_a", "idle", 0, "First", null), sessions[0])
        assertEquals(SessionState("conv_b", "waiting", 2, null, null), sessions[1])
    }

    @Test
    fun `reads runner_online when present and null when explicit null`() {
        val body =
            """
            {"object":"list","data":[
              {"id":"a","status":"idle","runner_online":false},
              {"id":"b","status":"idle","runner_online":null}
            ]}
            """.trimIndent()
        val sessions = parseSessionList(body)
        assertEquals(false, sessions[0].runnerOnline)
        assertNull(sessions[1].runnerOnline)
    }

    @Test
    fun `skips rows missing id or status but keeps the rest`() {
        val body =
            """
            {"object":"list","data":[
              {"id":"a","status":"idle"},
              {"status":"idle"},
              {"id":"c"},
              {"id":"d","status":"running"}
            ]}
            """.trimIndent()
        assertEquals(listOf("a", "d"), parseSessionList(body).map { it.id })
    }

    @Test
    fun `malformed or empty body yields an empty list`() {
        assertTrue(parseSessionList("not json").isEmpty())
        assertTrue(parseSessionList("{}").isEmpty())
        assertTrue(parseSessionList("""{"object":"list"}""").isEmpty())
    }
}
