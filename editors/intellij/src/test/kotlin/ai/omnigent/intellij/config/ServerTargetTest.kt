package ai.omnigent.intellij.config

import ai.omnigent.intellij.discovery.HealthOutcome
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.CsvSource

class ServerTargetTest {

    @ParameterizedTest
    @CsvSource(
        "http://127.0.0.1:6767, LOCAL",
        "http://localhost:6767, LOCAL",
        "http://[::1]:6767, LOCAL",
        "https://omnigent.example.com, REMOTE",
        "https://dbc-abc123.cloud.databricks.com, REMOTE",
        "'not a url', UNKNOWN",
    )
    fun `hostTypeOf classifies loopback vs remote vs unknown`(url: String, expected: String) {
        assertEquals(HostType.valueOf(expected), hostTypeOf(url))
    }

    @Test
    fun `originOf strips path and trailing slash`() {
        assertEquals("http://127.0.0.1:6767", originOf("http://127.0.0.1:6767/app/"))
    }

    @Test
    fun `manual loopback override wins over discovered local`() {
        val r = resolveServerTarget(
            settings("http://127.0.0.1:9000"),
            DiscoverySummary(found = true, baseUrl = "http://127.0.0.1:6767", health = HealthOutcome.OK),
        )
        val resolved = r as TargetResolution.Resolved
        assertEquals(TargetSource.MANUAL, resolved.target.source)
        assertEquals(HostType.LOCAL, resolved.target.hostType)
        assertEquals("http://127.0.0.1:9000", resolved.target.origin)
    }

    @Test
    fun `rejects a manual REMOTE override`() {
        val r = resolveServerTarget(
            settings("https://omnigent.example.com"),
            DiscoverySummary(found = true, baseUrl = "http://127.0.0.1:6767", health = HealthOutcome.OK),
        )
        assertEquals(TargetResolution.NeedsPrompt(NeedsPromptReason.RemoteUnsupported), r)
    }

    @Test
    fun `rejects a malformed manual override as unknown to remote-unsupported`() {
        val r = resolveServerTarget(settings("not a url"), DiscoverySummary(found = false))
        assertEquals(TargetResolution.NeedsPrompt(NeedsPromptReason.RemoteUnsupported), r)
    }

    @Test
    fun `uses discovered local when healthy and no manual override`() {
        val r = resolveServerTarget(
            settings(""),
            DiscoverySummary(found = true, baseUrl = "http://127.0.0.1:6767", health = HealthOutcome.OK),
        )
        val resolved = r as TargetResolution.Resolved
        assertEquals(TargetSource.DISCOVERED, resolved.target.source)
        assertEquals(HostType.LOCAL, resolved.target.hostType)
        assertEquals("http://127.0.0.1:6767", resolved.target.baseUrl)
    }

    @Test
    fun `needs-prompt when discovered local is unhealthy`() {
        val r = resolveServerTarget(
            settings(""),
            DiscoverySummary(found = true, baseUrl = "http://127.0.0.1:6767", health = HealthOutcome.TIMEOUT),
        )
        assertEquals(TargetResolution.NeedsPrompt(NeedsPromptReason.LocalUnhealthy), r)
    }

    @Test
    fun `needs-prompt when nothing is available`() {
        val r = resolveServerTarget(settings(""), DiscoverySummary(found = false))
        assertEquals(TargetResolution.NeedsPrompt(NeedsPromptReason.NoManualNoLocal), r)
    }

    private fun settings(url: String): Settings = object : Settings {
        override val serverUrl: String = url
    }
}
