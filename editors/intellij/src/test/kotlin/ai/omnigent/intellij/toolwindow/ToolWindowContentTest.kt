package ai.omnigent.intellij.toolwindow

import ai.omnigent.intellij.config.HostType
import ai.omnigent.intellij.config.NeedsPromptReason
import ai.omnigent.intellij.config.ServerTarget
import ai.omnigent.intellij.config.TargetResolution
import ai.omnigent.intellij.config.TargetSource
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test

class ToolWindowContentTest {

    private fun target(hostType: HostType) = ServerTarget(
        baseUrl = "http://127.0.0.1:6767",
        origin = "http://127.0.0.1:6767",
        hostType = hostType,
        source = TargetSource.DISCOVERED,
    )

    @Test
    fun `no JCEF-capable JBR shows guidance regardless of resolution`() {
        assertEquals(ContentKind.JcefGuidance, chooseToolWindowContent(false, null))
        assertEquals(
            ContentKind.JcefGuidance,
            chooseToolWindowContent(false, TargetResolution.Resolved(target(HostType.LOCAL))),
        )
    }

    @Test
    fun `no resolution yet shows the resolving placeholder`() {
        assertEquals(ContentKind.Resolving, chooseToolWindowContent(true, null))
    }

    @Test
    fun `resolved loopback target navigates`() {
        assertEquals(
            ContentKind.Navigate("http://127.0.0.1:6767"),
            chooseToolWindowContent(true, TargetResolution.Resolved(target(HostType.LOCAL))),
        )
    }

    @Test
    fun `resolved non-loopback target is rejected at the render surface`() {
        assertEquals(
            ContentKind.NoServer,
            chooseToolWindowContent(true, TargetResolution.Resolved(target(HostType.REMOTE))),
        )
    }

    @Test
    fun `needs-prompt shows no-server guidance`() {
        assertEquals(
            ContentKind.NoServer,
            chooseToolWindowContent(true, TargetResolution.NeedsPrompt(NeedsPromptReason.NoManualNoLocal)),
        )
    }
}
