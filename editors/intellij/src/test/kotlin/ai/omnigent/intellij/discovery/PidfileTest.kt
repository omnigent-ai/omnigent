package ai.omnigent.intellij.discovery

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class PidfileTest {
    @Test
    fun `parses a valid live pidfile into an ok result with a loopback baseUrl`() {
        assertEquals(
            PidfileResult.Ok(4242, 6767, "http://127.0.0.1:6767"),
            parsePidfile("4242\n6767\n", true),
        )
    }

    @Test
    fun `returns dead when the pid is not alive`() {
        assertEquals(PidfileResult.Dead(4242, 6767), parsePidfile("4242\n6767", false))
    }

    @Test
    fun `malformed fewer than two lines`() {
        assertEquals(
            PidfileResult.Malformed("expected two lines (pid then port)"),
            parsePidfile("4242", true),
        )
    }

    @Test
    fun `malformed pid not an integer`() {
        assertEquals(PidfileResult.Malformed("pid is not an integer"), parsePidfile("abc\n6767", true))
    }

    @Test
    fun `malformed non-positive pid`() {
        assertEquals(
            PidfileResult.Malformed("pid is not a positive integer"),
            parsePidfile("0\n6767", true),
        )
    }

    @Test
    fun `malformed port out of range`() {
        assertEquals(PidfileResult.Malformed("port out of range"), parsePidfile("4242\n99999", true))
    }

    @Test
    fun `tolerates surrounding whitespace and blank lines`() {
        assertEquals(
            PidfileResult.Ok(4242, 6767, "http://127.0.0.1:6767"),
            parsePidfile("  4242  \n  6767  \n\n", true),
        )
    }
}
