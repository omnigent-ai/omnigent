package ai.omnigent.android

import android.content.Context
import android.util.Base64
import android.util.Log
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException

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
    context: Context,
) {
    private val storage = DownloadStorage(context.applicationContext ?: context)
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
        try {
            io.execute {
                val bytes =
                    try {
                        Base64.decode(base64, Base64.DEFAULT)
                    } catch (_: Throwable) {
                        return@execute
                    }
                val result =
                    storage.save(suggestedName, mimeType) { output ->
                        output.write(bytes)
                    }
                storage.report(result)
            }
        } catch (_: RejectedExecutionException) {
            Log.w(TAG, "Dropping blob save because the worker is shut down")
        }
    }

    private companion object {
        const val TAG = "BlobSaver"
    }
}
