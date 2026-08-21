package ai.omnigent.android

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider

// A bare 32-hex uuid — the id form the API emits today.
const val TEST_CONVERSATION_ID = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"

fun viewIntent(link: String): Intent =
    Intent(Intent.ACTION_VIEW, Uri.parse(link)).addCategory(Intent.CATEGORY_BROWSABLE)

fun testStore(): ServerStore = ServerStore(ApplicationProvider.getApplicationContext())

fun MainActivity.privateField(name: String): Any? =
    MainActivity::class.java
        .getDeclaredField(name)
        .apply { isAccessible = true }
        .get(this)

fun MainActivity.setPrivateField(
    name: String,
    value: Any?,
) = MainActivity::class.java
    .getDeclaredField(name)
    .apply { isAccessible = true }
    .set(this, value)

fun MainActivity.testWebView(): WebView = privateField("webView") as WebView

fun MainActivity.invokeOnPageReady(
    url: String,
    mainFrameLoadFailed: Boolean = false,
    mainFramePersistenceFailed: Boolean = false,
    loadGeneration: Long = privateField("loadGeneration") as Long,
) {
    MainActivity::class
        .java
        .getDeclaredMethod(
            "onPageReady",
            String::class.java,
            Boolean::class.java,
            Boolean::class.java,
            Long::class.javaObjectType,
        ).apply { isAccessible = true }
        .invoke(this, url, mainFrameLoadFailed, mainFramePersistenceFailed, loadGeneration)
}

fun MainActivity.invokeRetryPinnedOrigin() {
    MainActivity::class
        .java
        .getDeclaredMethod("retryPinnedOrigin")
        .apply { isAccessible = true }
        .invoke(this)
}

class RecordingLoadWebView(
    context: Context,
    var currentUrl: String?,
) : WebView(context) {
    val loadedUrls = mutableListOf<String>()
    var reloadCount = 0

    override fun getUrl(): String? = currentUrl

    override fun loadUrl(url: String) {
        loadedUrls += url
        currentUrl = url
    }

    override fun reload() {
        reloadCount++
    }
}
