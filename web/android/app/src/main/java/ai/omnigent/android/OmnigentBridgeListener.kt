package ai.omnigent.android

import android.net.Uri
import android.webkit.WebView
import androidx.appcompat.app.AppCompatDelegate
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
 * [BlobSaver] offloads writes to its own worker.
 */
internal class OmnigentBridgeListener(
    private val notifications: NativeNotificationManager,
    private val blobSaver: BlobSaver,
    /** Relays validated bounds without retaining the Activity-owned view. */
    private val onServerSwitcherBand: (ServerSwitcherBand) -> Unit,
    private val onServerSwitcherHidden: (Boolean) -> Unit,
    private val pinnedOrigin: () -> String?,
) : WebViewCompat.WebMessageListener {
    override fun onPostMessage(
        view: WebView,
        message: WebMessageCompat,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
        replyProxy: JavaScriptReplyProxy,
    ) {
        handle(message.data, sourceOrigin, isMainFrame)
    }

    internal fun handle(
        data: String?,
        sourceOrigin: Uri,
        isMainFrame: Boolean,
    ) {
        if (!isMainFrame) return // origin allowlist already gates; defense in depth.
        val pin = pinnedOrigin() ?: return
        if (originOf(sourceOrigin.toString()) != pin) return
        handle(data ?: return)
    }

    /** Parse and dispatch one bridge message; malformed input is dropped. */
    internal fun handle(data: String) {
        val json =
            try {
                JSONObject(data)
            } catch (_: Throwable) {
                return
            }

        when (json.optString("method")) {
            "setColorScheme" -> {
                when (json.optString("scheme")) {
                    "light" -> {
                        AppCompatDelegate.setDefaultNightMode(
                            AppCompatDelegate.MODE_NIGHT_NO,
                        )
                    }

                    "dark" -> {
                        AppCompatDelegate.setDefaultNightMode(
                            AppCompatDelegate.MODE_NIGHT_YES,
                        )
                    }

                    "system" -> {
                        AppCompatDelegate.setDefaultNightMode(
                            AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM,
                        )
                    }
                }
            }

            "setBadgeCount" -> {
                notifications.setBadgeCount(
                    count = json.optInt("count", 0),
                    navigatePath = json.optString("navigatePath").ifEmpty { null },
                    title = json.optString("title").ifEmpty { null },
                    body = json.optString("body").ifEmpty { null },
                )
            }

            "notify" -> {
                val params = json.optJSONObject("params") ?: return
                val title = params.optString("title").ifEmpty { return }
                notifications.notify(
                    title = title,
                    body = params.optString("body").ifEmpty { null },
                    navigatePath = params.optString("navigatePath").ifEmpty { null },
                )
            }

            "setServerSwitcherBand" -> {
                // Reject strings before optDouble can coerce them.
                if (json.opt("leftFraction") !is Number) return
                if (json.opt("rightFraction") !is Number) return
                val left = json.optDouble("leftFraction", Double.NaN)
                val right = json.optDouble("rightFraction", Double.NaN)
                val band = ServerSwitcherBand.from(left, right) ?: return
                onServerSwitcherBand(band)
            }

            "setServerSwitcherHidden" -> {
                val hidden = json.opt("hidden") as? Boolean ?: return
                onServerSwitcherHidden(hidden)
            }

            "blobBase64" -> {
                blobSaver.save(
                    base64 = json.optString("base64").ifEmpty { return },
                    mimeType = json.optString("mimeType").ifEmpty { "application/octet-stream" },
                    suggestedName = json.optString("name"),
                )
            }
        }
    }

    companion object {
        /** Name of the injected transport object as seen from page JS. */
        const val JS_OBJECT_NAME = "omnigentNativeBridge"
    }
}
