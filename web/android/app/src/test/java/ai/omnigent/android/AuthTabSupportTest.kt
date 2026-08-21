package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AuthTabSupportTest {
    @Test
    fun `launch intent pins the resolved provider package`() {
        // launch() forwards this exact Intent instance, so pinning package here
        // pins the launched browser.
        val intent = AuthTabSupport.launchIntent(PROVIDER_PACKAGE).intent

        assertEquals(PROVIDER_PACKAGE, intent.`package`)
    }

    @Test
    fun `a null provider disables auth tab without probing support`() {
        // Robolectric does not enforce Android 11 package visibility; the
        // manifest <queries> path still needs device or bytecode verification.
        var supportChecked = false

        val provider =
            AuthTabSupport.supportedProviderPackage(null) {
                supportChecked = true
                true
            }

        assertNull(provider)
        assertFalse(supportChecked)
    }

    @Test
    fun `an unsupported resolved provider disables auth tab`() {
        val provider = AuthTabSupport.supportedProviderPackage(PROVIDER_PACKAGE) { false }

        assertNull(provider)
    }

    private companion object {
        const val PROVIDER_PACKAGE = "com.android.chrome"
    }
}
