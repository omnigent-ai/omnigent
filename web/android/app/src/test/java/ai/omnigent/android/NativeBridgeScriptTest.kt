package ai.omnigent.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NativeBridgeScriptTest {
    @Test
    fun `server picker bridge exposes offered servers and native actions`() {
        val source = NativeBridgeScript.source()

        assertTrue(source.contains("getServerPicker()"))
        assertTrue(source.contains("switchServer(url)"))
        assertTrue(source.contains("openServerSetup()"))
        assertTrue(source.contains("nativeBridgeVersion: 1"))
        assertTrue(source.contains("nativeWebReady(version)"))
        assertTrue(source.contains("nativeHeartbeat(version)"))
        assertTrue(source.contains("method: \"getServerPicker\", requestId"))
        assertTrue(source.contains("__omnigentNativeEmitServerPicker"))
        assertTrue(source.contains("return Promise.reject(new Error("))
        assertTrue(source.contains("""post({ method: "switchServer", url });"""))
        assertTrue(source.contains("""post({ method: "openServerSetup" });"""))
        assertFalse(source.contains("pickerCurrentOrigin"))
        assertFalse(source.contains("pickerRecentServers"))
        assertFalse(source.contains("setServerSwitcherHidden"))
    }
}
