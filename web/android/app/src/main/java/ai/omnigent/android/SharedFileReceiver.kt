package ai.omnigent.android

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.OpenableColumns
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * Reads OS-share ACTION_SEND(_MULTIPLE) file URIs off the main thread and
 * base64-encodes them for handoff to the web layer via
 * [NativeBridgeScript]'s `window.__omnigentNativeEmitSharedFiles`. The
 * reverse direction of [BlobSaver] (which decodes web -> native for saving):
 * this encodes native -> web for the composer's existing upload flow.
 *
 * Bounded so a shared video or other large file can't OOM the WebView's JS
 * heap decoding a giant base64 string, or wedge the bridge with a slow post:
 * anything over [MAX_BYTES_PER_FILE] is dropped entirely (not truncated — a
 * partial attachment would silently corrupt), and only the first
 * [MAX_FILES] of a multi-share survive. The web layer's own
 * `validateAttachments` re-checks type/size anyway; this bound exists purely
 * to protect the native<->web transport, not to enforce product limits.
 */
class SharedFileReceiver(
    private val context: Context,
) {
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()

    /** Release the worker thread; call from the host's onDestroy. */
    fun shutdown() {
        io.shutdown()
    }

    /**
     * Reads [uris] on a worker thread and invokes [onReady] on the main
     * thread with a JSON array of `{name, mimeType, base64}` objects — only
     * called when at least one file was read successfully.
     */
    fun readAndQueue(
        uris: List<Uri>,
        onReady: (String) -> Unit,
    ) {
        io.execute {
            val resolver = context.contentResolver
            val results = JSONArray()
            for (uri in uris.take(MAX_FILES)) {
                val payload = readOne(resolver, uri) ?: continue
                results.put(payload)
            }
            if (results.length() == 0) return@execute
            val json = results.toString()
            main.post { onReady(json) }
        }
    }

    private fun readOne(
        resolver: ContentResolver,
        uri: Uri,
    ): JSONObject? =
        runCatching {
            val size = querySize(resolver, uri)
            if (size != null && size > MAX_BYTES_PER_FILE) return null
            val bytes = resolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
            if (bytes.size > MAX_BYTES_PER_FILE) return null
            JSONObject().apply {
                put("name", queryName(resolver, uri) ?: "shared-file")
                put("mimeType", resolver.getType(uri) ?: "application/octet-stream")
                put("base64", Base64.encodeToString(bytes, Base64.NO_WRAP))
            }
        }.getOrNull()

    private fun queryName(
        resolver: ContentResolver,
        uri: Uri,
    ): String? =
        runCatching {
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        }.getOrNull()

    private fun querySize(
        resolver: ContentResolver,
        uri: Uri,
    ): Long? =
        runCatching {
            resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else null
            }
        }.getOrNull()

    companion object {
        private const val MAX_FILES = 5
        private const val MAX_BYTES_PER_FILE = 15L * 1024 * 1024 // 15 MB
    }
}
