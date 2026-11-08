package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import android.view.ViewGroup
import android.widget.TextView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OAuthCallbackIntentFilterTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()

    @Test
    fun `browser redirects reach the callback receiver only at the registered callbacks`() {
        assertEquals(
            listOf(OAuthCallbackActivity::class.java.name),
            receivers("ai.omnigent.android:/oauth/callback?code=c&state=s"),
        )
        assertEquals(
            listOf(OAuthCallbackActivity::class.java.name),
            receivers("ai.omnigent.android://mobile-redirect?code=c&state=s"),
        )
        assertEquals(emptyList<String>(), receivers("ai.omnigent.android:/somewhere-else"))
        assertEquals(emptyList<String>(), receivers("ai.omnigent.android://oauth/callback?code=c"))
    }

    @Test
    fun `the receiver names the sign-in it is completing`() {
        assertEquals(
            "Completing sign-in…",
            receiverText("ai.omnigent.android:/oauth/callback?code=c&state=s"),
        )
        assertEquals(
            "Completing Databricks sign-in…",
            receiverText("ai.omnigent.android://mobile-redirect?code=c&state=s"),
        )
    }

    private fun receiverText(uri: String): String {
        val intent = Intent(context, OAuthCallbackActivity::class.java).setData(Uri.parse(uri))
        val activity =
            Robolectric
                .buildActivity(
                    OAuthCallbackActivity::class.java,
                    intent,
                ).create()
                .get()
        val content =
            activity
                .findViewById<ViewGroup>(
                    android.R.id.content,
                ).getChildAt(0) as TextView
        return content.text.toString()
    }

    private fun receivers(uri: String): List<String> =
        context.packageManager
            .queryIntentActivities(
                Intent(Intent.ACTION_VIEW, Uri.parse(uri)).addCategory(Intent.CATEGORY_BROWSABLE),
                0,
            ).map { it.activityInfo.name }
}
