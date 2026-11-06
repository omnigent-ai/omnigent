package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Unit tests for the `GET /v1/sessions` response parser. Runs under Robolectric
 * because the parser uses `org.json`, which is an unmocked Android stub in a
 * plain JVM unit test — Robolectric supplies the real implementation. No device.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
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
    fun `parses only known boolean runner liveness`() {
        val parsed =
            parseRunnerLiveness(
                """{
                  "sessions": {
                    "online": {"runner_online": true},
                    "offline": {"runner_online": false},
                    "unknown": {"runner_online": null},
                    "malformed": {"runner_online": "true"}
                  }
                }""",
            )

        assertEquals(mapOf("online" to true, "offline" to false), parsed)
    }

    @Test
    fun `malformed runner liveness response is unknown`() {
        assertTrue(parseRunnerLiveness("not json").isEmpty())
        assertTrue(parseRunnerLiveness("""{"status":"ok"}""").isEmpty())
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
    fun `explicit JSON null title parses as null, not the string "null"`() {
        val body =
            """
            {"object":"list","data":[
              {"id":"a","status":"idle","title":null},
              {"id":"b","status":"idle","title":""}
            ]}
            """.trimIndent()
        val sessions = parseSessionList(body)
        // optString would have produced the literal "null" here, which would
        // notify as a session titled "null" instead of the generic fallback.
        assertNull(sessions[0].title)
        assertNull(sessions[1].title)
        assertEquals("New session", sessionDisplayLabel(sessions[0].title))
    }

    @Test
    fun `explicit JSON null id or status skips the row instead of parsing "null"`() {
        // optString would turn `"id": null` into the literal string "null":
        // two consecutive malformed rows would BOTH become session "null",
        // manufacturing a fake running -> idle transition (and a /c/null deep
        // link) between them. They must be skipped like missing fields.
        val body =
            """
            {"object":"list","data":[
              {"id":null,"status":"running"},
              {"id":null,"status":"idle"},
              {"id":"real","status":null},
              {"id":"kept","status":"idle"}
            ]}
            """.trimIndent()
        val sessions = parseSessionList(body)
        assertEquals(listOf("kept"), sessions.map { it.id })

        // End to end: even with a poisoned prior snapshot containing "null",
        // the skipped rows can never produce a transition for it.
        val previous = mapOf("null" to SessionSnapshot("running", 0))
        assertTrue(detectIdleTransitions(previous, sessions).isEmpty())
    }

    @Test
    fun `malformed or empty body yields an empty list`() {
        assertTrue(parseSessionList("not json").isEmpty())
        assertTrue(parseSessionList("{}").isEmpty())
        assertTrue(parseSessionList("""{"object":"list"}""").isEmpty())
    }
}
