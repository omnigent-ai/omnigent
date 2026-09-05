package ai.omnigent.android

import android.webkit.RenderProcessGoneDetail
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/**
 * Renderer-death handling. When Android kills or crashes the WebView's
 * renderer process it calls [android.webkit.WebViewClient.onRenderProcessGone]
 * on the registered client. Returning `false` — the inherited default — tells
 * the framework the app did not handle the loss, and Android terminates the
 * hosting process: the app vanishes and cold-restarts, losing the SPA's
 * transient state. Renderer death is routine on this shell's targets (memory
 * pressure while backgrounded on a mid-range phone), so the shell's client
 * must claim the event by returning `true` and recover the WebView instead.
 */
@RunWith(RobolectricTestRunner::class)
class OmnigentWebViewClientRenderProcessGoneTest {
    @Test
    fun `a renderer crash is handled so Android does not kill the app`() {
        val handled = client().onRenderProcessGone(webView(), detail(crashed = true))

        assertTrue(
            "onRenderProcessGone returned false for a crashed renderer: the " +
                "framework treats the event as unhandled and terminates the " +
                "hosting process, so the app dies instead of recovering",
            handled,
        )
    }

    @Test
    fun `a system-initiated renderer kill is handled so Android does not kill the app`() {
        val handled = client().onRenderProcessGone(webView(), detail(crashed = false))

        assertTrue(
            "onRenderProcessGone returned false for a system-killed renderer " +
                "(ordinary memory pressure while backgrounded): the framework " +
                "treats the event as unhandled and terminates the hosting " +
                "process, so the app dies instead of recovering",
            handled,
        )
    }

    private fun webView() = WebView(ApplicationProvider.getApplicationContext())

    private fun client() =
        OmnigentWebViewClient(
            pinnedOrigin = { "https://server.example" },
            shouldInjectBridgeAtPageReady = { false },
            onPageReady = {},
            onLoginRequired = {},
            onRendererGone = {},
        )

    private fun detail(crashed: Boolean) =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = crashed

            override fun rendererPriorityAtExit(): Int = WebView.RENDERER_PRIORITY_IMPORTANT
        }
}
