package ai.omnigent.android

import android.content.ContentResolver
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.SharedPreferences
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.widget.Toast
import androidx.annotation.RequiresApi
import java.io.File
import java.io.OutputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock

internal data class DownloadSaveResult(
    val name: String,
    val saved: Boolean,
)

/** Reduce a suggested download name to a safe, path-free leaf filename. */
internal fun safeFileName(suggested: String): String {
    // Treat both separator styles as paths before replacing unsafe characters.
    val leaf =
        suggested
            .substringAfterLast('/')
            .substringAfterLast('\\')
    val cleaned = leaf.replace(Regex("[^A-Za-z0-9._-]"), "_")
    // Dot paths resolve to directories on the pre-Q filesystem destination.
    return if (leaf.isBlank() || cleaned == "." || cleaned == "..") {
        "omnigent-${System.currentTimeMillis()}"
    } else {
        cleaned
    }
}

/** SHA-256 of [value] as lowercase hex — a stable, filename/key-safe identity. */
internal fun sha256Hex(value: String): String =
    MessageDigest
        .getInstance("SHA-256")
        .digest(value.toByteArray(Charsets.UTF_8))
        .joinToString(separator = "") { byte ->
            Integer.toHexString(byte.toInt() and 0xff).padStart(2, '0')
        }

/** Shared destination, filename, and user-notification handling for downloaded files. */
internal class DownloadStorage(
    context: Context,
) {
    private val context = context.applicationContext ?: context
    private val main = Handler(Looper.getMainLooper())

    fun save(
        suggestedName: String,
        mimeType: String,
        operationId: String = UUID.randomUUID().toString(),
        shouldAbort: () -> Boolean = { false },
        write: (OutputStream) -> Unit,
    ): DownloadSaveResult {
        val name = safeFileName(suggestedName)
        val saveLock =
            acquireSaveLock(name, shouldAbort) ?: return DownloadSaveResult(name, false)
        val saved =
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    saveViaMediaStore(name, mimeType, operationId, shouldAbort, write)
                } else {
                    saveToAppDownloads(name, operationId, shouldAbort, write)
                }
            } finally {
                saveLock.lock.unlock()
                releaseSaveLock(name, saveLock)
            }
        return DownloadSaveResult(name, saved)
    }

    fun failed(suggestedName: String): DownloadSaveResult =
        DownloadSaveResult(safeFileName(suggestedName), false)

    fun report(result: DownloadSaveResult) {
        main.post {
            val message =
                if (result.saved) {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        "Saved ${result.name} to Downloads"
                    } else {
                        "Saved ${result.name} to app storage"
                    }
                } else {
                    "Couldn't save ${result.name}"
                }
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun saveViaMediaStore(
        name: String,
        mimeType: String,
        operationId: String,
        shouldAbort: () -> Boolean,
        write: (OutputStream) -> Unit,
    ): Boolean {
        val resolver = context.contentResolver
        val journalKey = operationKey(operationId)
        val destination =
            synchronized(MEDIA_STORE_LOCK) {
                sweepOrphanedMediaStoreRows()
                // A live operation's entry must not be evicted while it writes:
                // the next save's sweep would delete the row under the writer.
                mediaStoreDestination(operationId, name, mimeType)
                    ?.also { if (!it.published) ACTIVE_MEDIA_OPERATIONS += journalKey }
            } ?: return false
        if (destination.published) return true
        try {
            val uri = destination.uri
            // Cancelled work is never retried, so an abandoned pending row
            // would stay journaled and pending forever — delete it now.
            if (shouldAbort()) {
                deleteMediaStoreDestination(operationId, uri)
                return false
            }

            val wrote =
                runCatching {
                    val output = resolver.openOutputStream(uri) ?: error("No output stream")
                    output.use(write)
                }.isSuccess
            if (!wrote) {
                deleteMediaStoreDestination(operationId, uri)
                return false
            }
            if (shouldAbort()) {
                deleteMediaStoreDestination(operationId, uri)
                return false
            }

            val publishValues = ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }
            val published =
                runCatching {
                    resolver.update(uri, publishValues, null, null) > 0
                }.getOrDefault(false)
            if (!published || shouldAbort()) {
                deleteMediaStoreDestination(operationId, uri)
                return false
            }
            return true
        } finally {
            synchronized(MEDIA_STORE_LOCK) { ACTIVE_MEDIA_OPERATIONS -= journalKey }
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun mediaStoreDestination(
        operationId: String,
        name: String,
        mimeType: String,
    ): MediaStoreDestination? {
        val journal = context.getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)
        val journalKey = operationKey(operationId)
        val remembered = journal.getString(journalKey, null)?.let(Uri::parse)
        if (remembered != null) {
            // The entry outlives publication: a stop can void this run's result
            // after the row is published, and only the journal lets the rerun
            // recognize that row instead of downloading a duplicate.
            when (mediaStoreRowState(remembered)) {
                MediaStoreRowState.PENDING -> {
                    return MediaStoreDestination(remembered, false)
                }

                MediaStoreRowState.PUBLISHED -> {
                    return MediaStoreDestination(remembered, true)
                }

                MediaStoreRowState.UNKNOWN -> {
                    return null
                }

                MediaStoreRowState.MISSING -> {
                    journal
                        .edit()
                        .remove(journalKey)
                        .remove(SEQ_PREFIX + journalKey)
                        .commit()
                }
            }
        }

        val pendingValues =
            ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mimeType)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
        val uri =
            runCatching {
                context.contentResolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                    pendingValues,
                )
            }.getOrNull() ?: return null
        if (!rememberMediaStoreDestination(journal, journalKey, uri)) {
            runCatching { context.contentResolver.delete(uri, null, null) }
            return null
        }
        return MediaStoreDestination(uri, false)
    }

    /** Journals the row with a sequence number, evicting the oldest entries past the cap. */
    private fun rememberMediaStoreDestination(
        journal: SharedPreferences,
        journalKey: String,
        uri: Uri,
    ): Boolean {
        val sequence = journal.getLong(KEY_JOURNAL_SEQ, 0L) + 1
        val editor =
            journal
                .edit()
                .putString(journalKey, uri.toString())
                .putLong(SEQ_PREFIX + journalKey, sequence)
                .putLong(KEY_JOURNAL_SEQ, sequence)
        val entries = journal.all
        val operationKeys =
            entries.keys.filter { key -> key != KEY_JOURNAL_SEQ && !key.startsWith(SEQ_PREFIX) }
        if (operationKeys.size + 1 > MAX_JOURNAL_ENTRIES) {
            operationKeys
                .filter { key -> key != journalKey && key !in ACTIVE_MEDIA_OPERATIONS }
                .sortedBy { key -> entries[SEQ_PREFIX + key] as? Long ?: 0L }
                .take(operationKeys.size + 1 - MAX_JOURNAL_ENTRIES)
                .forEach { key -> editor.remove(key).remove(SEQ_PREFIX + key) }
        }
        return editor.commit()
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun mediaStoreRowState(uri: Uri): MediaStoreRowState =
        try {
            queryIncludingPending(
                uri,
                arrayOf(MediaStore.Downloads.IS_PENDING),
            )?.use { cursor ->
                if (!cursor.moveToFirst()) {
                    MediaStoreRowState.MISSING
                } else if (cursor.getInt(0) == 0) {
                    MediaStoreRowState.PUBLISHED
                } else {
                    MediaStoreRowState.PENDING
                }
            } ?: MediaStoreRowState.UNKNOWN
        } catch (_: Throwable) {
            MediaStoreRowState.UNKNOWN
        }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun sweepOrphanedMediaStoreRows() {
        val resolver = context.contentResolver
        val journalUris =
            context
                .getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)
                .all
                .values
                .filterIsInstance<String>()
                .toSet()
        runCatching {
            queryIncludingPending(
                MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                arrayOf(MediaStore.Downloads._ID),
                "${MediaStore.Downloads.IS_PENDING} = ? AND " +
                    "${MediaStore.MediaColumns.OWNER_PACKAGE_NAME} = ?",
                arrayOf("1", context.packageName),
            )?.use { cursor ->
                while (cursor.moveToNext()) {
                    val uri =
                        ContentUris.withAppendedId(
                            MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                            cursor.getLong(0),
                        )
                    if (uri.toString() !in journalUris) {
                        runCatching { resolver.delete(uri, null, null) }
                    }
                }
            }
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun queryIncludingPending(
        uri: Uri,
        projection: Array<String>,
        selection: String? = null,
        selectionArgs: Array<String>? = null,
    ) = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
        val queryArgs =
            Bundle().apply {
                selection?.let { putString(ContentResolver.QUERY_ARG_SQL_SELECTION, it) }
                selectionArgs?.let {
                    putStringArray(ContentResolver.QUERY_ARG_SQL_SELECTION_ARGS, it)
                }
                putInt(MediaStore.QUERY_ARG_MATCH_PENDING, MediaStore.MATCH_INCLUDE)
            }
        context.contentResolver.query(uri, projection, queryArgs, null)
    } else {
        context.contentResolver.query(
            MediaStore.setIncludePending(uri),
            projection,
            selection,
            selectionArgs,
            null,
        )
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun deleteMediaStoreDestination(
        operationId: String,
        uri: Uri,
    ) {
        synchronized(MEDIA_STORE_LOCK) {
            runCatching { context.contentResolver.delete(uri, null, null) }
            val journalKey = operationKey(operationId)
            context
                .getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)
                .edit()
                .remove(journalKey)
                .remove(SEQ_PREFIX + journalKey)
                .commit()
        }
    }

    private fun saveToAppDownloads(
        name: String,
        operationId: String,
        shouldAbort: () -> Boolean,
        write: (OutputStream) -> Unit,
    ): Boolean {
        val dir =
            context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                ?: context.filesDir
        val temporary = File(dir, ".omnigent-${operationKey(operationId)}.tmp")
        synchronized(TEMPORARY_LOCK) {
            dir
                .listFiles()
                .orEmpty()
                .filter(File::isTemporaryDownload)
                .filterNot { file -> file.absolutePath in ACTIVE_TEMPORARIES }
                .forEach(File::delete)
            ACTIVE_TEMPORARIES += temporary.absolutePath
        }
        return try {
            temporary.outputStream().use(write)
            if (shouldAbort()) return false
            replaceFile(temporary, File(dir, name))
            true
        } catch (_: Throwable) {
            false
        } finally {
            temporary.delete()
            synchronized(TEMPORARY_LOCK) {
                ACTIVE_TEMPORARIES -= temporary.absolutePath
            }
        }
    }

    private fun replaceFile(
        source: File,
        destination: File,
    ) {
        try {
            Files.move(
                source.toPath(),
                destination.toPath(),
                StandardCopyOption.ATOMIC_MOVE,
                StandardCopyOption.REPLACE_EXISTING,
            )
        } catch (_: AtomicMoveNotSupportedException) {
            Files.move(
                source.toPath(),
                destination.toPath(),
                StandardCopyOption.REPLACE_EXISTING,
            )
        }
    }

    private fun acquireSaveLock(
        name: String,
        shouldAbort: () -> Boolean,
    ): SaveLock? {
        if (shouldAbort()) return null
        val saveLock =
            synchronized(SAVE_LOCK_REGISTRY_LOCK) {
                SAVE_LOCKS.getOrPut(name, ::SaveLock).also { it.users++ }
            }
        var acquired = false
        var retained = false
        try {
            while (!acquired) {
                if (shouldAbort()) return null
                acquired =
                    try {
                        saveLock.lock.tryLock(SAVE_LOCK_POLL_INTERVAL_MS, TimeUnit.MILLISECONDS)
                    } catch (_: InterruptedException) {
                        Thread.currentThread().interrupt()
                        return null
                    }
            }
            if (shouldAbort()) return null
            retained = true
            return saveLock
        } finally {
            if (!retained) {
                if (acquired) saveLock.lock.unlock()
                releaseSaveLock(name, saveLock)
            }
        }
    }

    private fun releaseSaveLock(
        name: String,
        saveLock: SaveLock,
    ) {
        synchronized(SAVE_LOCK_REGISTRY_LOCK) {
            saveLock.users--
            if (saveLock.users == 0 && SAVE_LOCKS[name] === saveLock) {
                SAVE_LOCKS.remove(name)
            }
        }
    }

    private companion object {
        const val MEDIA_STORE_JOURNAL = "ai.omnigent.android.download_media_store"
        const val KEY_JOURNAL_SEQ = "seq"
        const val SEQ_PREFIX = "seq."
        const val MAX_JOURNAL_ENTRIES = 64
        private const val SAVE_LOCK_POLL_INTERVAL_MS = 100L
        private val SAVE_LOCK_REGISTRY_LOCK = Any()
        private val SAVE_LOCKS = mutableMapOf<String, SaveLock>()
        private val MEDIA_STORE_LOCK = Any()
        private val TEMPORARY_LOCK = Any()
        private val ACTIVE_TEMPORARIES = mutableSetOf<String>()

        // A set, not a multiset: assumes at most one in-flight save per
        // operationId. Overlapping saves sharing an id under different names
        // would unshield the still-live sibling when the first one finishes.
        private val ACTIVE_MEDIA_OPERATIONS = mutableSetOf<String>()

        private fun operationKey(operationId: String): String = sha256Hex(operationId)
    }
}

private class SaveLock(
    val lock: ReentrantLock = ReentrantLock(),
    var users: Int = 0,
)

private data class MediaStoreDestination(
    val uri: Uri,
    val published: Boolean,
)

private enum class MediaStoreRowState {
    PENDING,
    PUBLISHED,
    MISSING,
    UNKNOWN,
}

private fun File.isTemporaryDownload(): Boolean =
    name.startsWith(".omnigent-") && name.endsWith(".tmp")
