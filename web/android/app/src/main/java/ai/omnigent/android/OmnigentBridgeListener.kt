package ai.omnigent.android

import android.net.Uri
import android.webkit.WebView
import androidx.webkit.JavaScriptReplyProxy
import androidx.webkit.WebMessageCompat
import androidx.webkit.WebViewCompat
import org.json.JSONObject

/**
 * The single web -> native bridge, installed via
 * `WebViewCompat.addWebMessageListener` with an origin allowlist of just the
 * pinned server. Unlike `addJavascriptInterface`, the injected object
 * (`window.`[JS_OBJECT_NAME]`)` is delivered ONLY to frames whose origin
 * matches the allowlist, so a sandboxed / opaque agent-HTML iframe never
 * receives it. We additionally drop non-main-frame messages — together the
 * structural equivalent of the iOS `isMainFrame` + frame-origin check that a
 * raw `addJavascriptInterface` bridge cannot express.
 *
 * Callbacks arrive on the UI thread, so notification calls need no hop; the
 * blob write offloads to [BlobSaver]'s own worker.
 */
class OmnigentBridgeListener(
    private val notifications: NativeNotificationManager,
    private val blobSaver: BlobSaver,
) : WebViewCompat.WebMessageListener {

    override fun onPostMessage(
        view: WebView,
        message: WebMessageCompat,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
        replyProxy: JavaScriptReplyProxy,
    ) {
        if (!isMainFrame) return // origin allowlist already gates; defense in depth.
        val data = message.data ?: return
        val json =
            try {
                JSONObject(data)
            } catch (_: Throwable) {
                return
            }

        when (json.optString("method")) {
            "setBadgeCount" -> notifications.setBadgeCount(json.optInt("count", 0))
            "notify" -> {
                val params = json.optJSONObject("params") ?: return
                val title = params.optString("title").ifEmpty { return }
                notifications.notify(
                    title = title,
                    body = params.optString("body").ifEmpty { null },
                    navigatePath = params.optString("navigatePath").ifEmpty { null },
                )
            }
            "blobBase64" ->
                blobSaver.save(
                    base64 = json.optString("base64").ifEmpty { return },
                    mimeType = json.optString("mimeType").ifEmpty { "application/octet-stream" },
                    suggestedName = json.optString("name"),
                )
        }
    }

    companion object {
        /** Name of the injected transport object as seen from page JS. */
        const val JS_OBJECT_NAME = "omnigentNativeBridge"
    }
}
