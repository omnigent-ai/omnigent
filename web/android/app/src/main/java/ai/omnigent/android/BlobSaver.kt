package ai.omnigent.android

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Base64
import android.widget.Toast
import androidx.annotation.RequiresApi
import java.io.File
import java.util.concurrent.Executors

/**
 * Decodes a base64 payload (produced by [BlobDownloadScript], dispatched by
 * [OmnigentBridgeListener]) and writes it to the device's Downloads — MediaStore
 * on API 29+, the app-specific external dir on 28 (both permission-free). The
 * decode + write run on a worker so a large file never blocks the caller.
 *
 * Trust is enforced upstream by the bridge's origin allowlist + main-frame gate,
 * so this class does no origin checking of its own.
 */
class BlobSaver(
    private val context: Context,
) {
    private val main = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()

    /** Release the worker thread; call from the host's onDestroy. */
    fun shutdown() {
        io.shutdown()
    }

    fun save(
        base64: String,
        mimeType: String,
        suggestedName: String,
    ) {
        io.execute {
            val bytes =
                try {
                    Base64.decode(base64, Base64.DEFAULT)
                } catch (_: Throwable) {
                    return@execute
                }
            val name = safeFileName(suggestedName)
            val saved =
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    saveViaMediaStore(name, mimeType, bytes)
                } else {
                    saveToAppDownloads(name, bytes)
                }
            main.post {
                val msg = if (saved) "Saved $name to Downloads" else "Couldn't save $name"
                Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
            }
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun saveViaMediaStore(
        name: String,
        mimeType: String,
        bytes: ByteArray,
    ): Boolean =
        runCatching {
            val resolver = context.contentResolver
            val values =
                ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, name)
                    put(MediaStore.Downloads.MIME_TYPE, mimeType)
                    put(MediaStore.Downloads.IS_PENDING, 1)
                }
            val uri =
                resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                    ?: return false
            resolver.openOutputStream(uri)?.use { it.write(bytes) } ?: return false
            values.clear()
            values.put(MediaStore.Downloads.IS_PENDING, 0)
            resolver.update(uri, values, null, null)
            true
        }.getOrDefault(false)

    private fun saveToAppDownloads(
        name: String,
        bytes: ByteArray,
    ): Boolean =
        runCatching {
            val dir =
                context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                    ?: context.filesDir
            File(dir, name).outputStream().use { it.write(bytes) }
            true
        }.getOrDefault(false)

    private fun safeFileName(suggested: String): String {
        // Basename past either separator style — a Windows-flavored "foo\bar.txt"
        // suggestion should save as "bar.txt", not "foo_bar.txt".
        val cleaned =
            suggested
                .substringAfterLast('/')
                .substringAfterLast('\\')
                .replace(Regex("[^A-Za-z0-9._-]"), "_")
        // "" / "." / ".." aren't usable names — on the API 28 File path "." and
        // ".." resolve to a directory, so the write would fail. Fall back instead.
        return if (cleaned.isBlank() || cleaned == "." || cleaned == "..") {
            "omnigent-${System.currentTimeMillis()}"
        } else {
            cleaned
        }
    }
}
