package ai.omnigent.android

import org.junit.Assert.assertTrue
import org.junit.Test

class NativeBridgeScriptTest {
    @Test
    fun `android facade publishes switcher hidden state`() {
        assertTrue(NativeBridgeScript.source.contains("setServerSwitcherHidden(hidden)"))
        assertTrue(
            NativeBridgeScript.source.contains(
                """post({ method: "setServerSwitcherHidden", hidden });""",
            ),
        )
    }
}
