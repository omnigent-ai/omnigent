package ai.omnigent.android

import android.webkit.WebView
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class NativeBridgeWebViewTest {
    @Test
    fun generatedBridgeExecutesLivePickerRoundTripInWebView() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        lateinit var webView: WebView
        instrumentation.runOnMainSync {
            webView = WebView(instrumentation.targetContext)
            webView.settings.javaScriptEnabled = true
            webView.loadDataWithBaseURL(
                "https://current.example.com",
                "<html><head></head><body></body></html>",
                "text/html",
                "UTF-8",
                null,
            )
        }
        instrumentation.waitForIdleSync()

        evaluate(
            webView,
            "window.omnigentNativeBridge={messages:[]," +
                "postMessage:function(value){this.messages.push(JSON.parse(value));}};" +
                NativeBridgeScript.source,
        )
        evaluate(webView, "window.omnigentNative.nativeWebReady(1)")
        evaluate(
            webView,
            "window.__pickerResult=null;window.omnigentNative.getServerPicker().then(function(value){window.__pickerResult=value;});",
        )
        val requestId =
            evaluate(
                webView,
                "window.omnigentNativeBridge.messages.find(m=>m.method==='getServerPicker').requestId",
            )
        evaluate(
            webView,
            "window.__omnigentNativeEmitServerPicker($requestId,{currentOrigin:'https://current.example.com',currentServerUrl:'https://current.example.com/app',managedServers:['https://managed.example.com'],recentServers:['https://recent.example.com']})",
        )

        assertEquals(
            "\"https://current.example.com/app\"",
            evaluate(webView, "window.__pickerResult.currentServerUrl"),
        )
        assertTrue(
            evaluate(
                webView,
                "window.omnigentNativeBridge.messages.some(m=>m.method==='nativeWebReady')",
            ) ==
                "true",
        )
        instrumentation.runOnMainSync { webView.destroy() }
    }

    private fun evaluate(
        webView: WebView,
        script: String,
    ): String {
        val latch = CountDownLatch(1)
        var result = "null"
        InstrumentationRegistry.getInstrumentation().runOnMainSync {
            webView.evaluateJavascript(script) {
                result = it
                latch.countDown()
            }
        }
        assertTrue("JavaScript evaluation timed out", latch.await(10, TimeUnit.SECONDS))
        return result
    }
}
