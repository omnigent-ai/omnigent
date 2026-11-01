package ai.omnigent.android

import android.view.View
import android.widget.PopupMenu
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OidcAuthDebugMenuTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()

    @Test
    fun `debug menu offers both OIDC session faults`() {
        val popup = PopupMenu(context, View(context))

        OidcAuthDebugMenu.addItems(popup.menu)

        assertTrue(OidcAuthDebugMenu.IS_AVAILABLE)
        assertNotNull(popup.menu.findItem(OidcAuthDebugMenu.CLEAR_SESSION_COOKIE))
        assertNotNull(popup.menu.findItem(OidcAuthDebugMenu.CLEAR_REFRESH_TOKEN))
        assertTrue(OidcAuthDebugMenu.handles(OidcAuthDebugMenu.CLEAR_REFRESH_TOKEN))
        assertFalse(OidcAuthDebugMenu.handles(DatabricksAuthDebugMenu.CLEAR_SESSION_COOKIE))
    }
}
