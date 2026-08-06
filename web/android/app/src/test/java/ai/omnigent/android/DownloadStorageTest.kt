package ai.omnigent.android

import android.app.Application
import android.content.ContentProvider
import android.content.ContentResolver
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.Environment
import android.provider.MediaStore
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DownloadStorageTest {
    private lateinit var context: Application
    private lateinit var provider: RecordingMediaProvider
    private lateinit var output: ByteArrayOutputStream

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context
            .getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        provider = RecordingMediaProvider()
        output = ByteArrayOutputStream()
        ShadowContentResolver.registerProviderInternal("media", provider)
        shadowOf(context.contentResolver).registerOutputStream(provider.insertedUri, output)
    }

    @After
    fun tearDown() {
        ShadowContentResolver.reset()
    }

    @Test
    fun `failed MediaStore publish reports failure and deletes the pending row`() {
        provider.updateResult = 0

        val result =
            DownloadStorage(context).save("report.txt", "text/plain") { output ->
                output.write("report".toByteArray())
            }

        assertFalse(result.saved)
        assertEquals(1, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
    }

    @Test
    fun `failed MediaStore write deletes the pending row without publishing it`() {
        val result =
            DownloadStorage(context).save("report.txt", "text/plain") {
                error("write failed")
            }

        assertFalse(result.saved)
        assertEquals(0, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
    }

    @Test
    fun `successful MediaStore save inserts metadata writes and publishes the row`() {
        val result =
            DownloadStorage(context).save("report.txt", "text/plain") { stream ->
                stream.write("report".toByteArray())
            }

        assertTrue(result.saved)
        assertEquals(
            "report.txt",
            provider.insertedValues?.getAsString(MediaStore.Downloads.DISPLAY_NAME),
        )
        assertEquals(
            "text/plain",
            provider.insertedValues?.getAsString(MediaStore.Downloads.MIME_TYPE),
        )
        assertEquals(1, provider.insertedValues?.getAsInteger(MediaStore.Downloads.IS_PENDING))
        assertEquals(0, provider.updatedValues?.getAsInteger(MediaStore.Downloads.IS_PENDING))
        assertEquals("report", output.toString(Charsets.UTF_8.name()))
        assertEquals(0, provider.deleteCalls)
        // The entry survives publication so a stop-voided result's rerun can
        // recognize this row instead of downloading a duplicate.
        assertTrue(
            mediaStoreJournal().all.values.contains(provider.insertedUri.toString()),
        )
    }

    @Test
    fun `remembered published MediaStore row is reused and stays journaled`() {
        val operationId = "published-work"
        insertMediaStoreRow(pending = false)
        rememberMediaStoreRow(operationId, provider.insertedUri)
        val storage = DownloadStorage(context)
        var writes = 0

        val result =
            storage.save("report.txt", "text/plain", operationId = operationId) {
                writes++
            }

        assertTrue(result.saved)
        assertEquals(1, provider.insertCalls)
        assertEquals(0, provider.updateCalls)
        assertEquals(0, writes)
        assertEquals(
            provider.insertedUri.toString(),
            mediaStoreJournal().getString(operationKey(operationId), null),
        )
    }

    @Test
    @Config(sdk = [29, 35])
    fun `rerun reuses a journaled pending MediaStore row`() {
        val operationId = "pending-work"
        insertMediaStoreRow(pending = true)
        rememberMediaStoreRow(operationId, provider.insertedUri)

        val result =
            DownloadStorage(context).save(
                "report.txt",
                "text/plain",
                operationId = operationId,
            ) { stream ->
                stream.write("report".toByteArray())
            }

        assertTrue(result.saved)
        assertEquals(1, provider.insertCalls)
        assertEquals(1, provider.updateCalls)
        assertEquals("report", output.toString(Charsets.UTF_8.name()))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            assertTrue(provider.bundleIncludePendingQueries > 0)
        } else {
            assertTrue(provider.uriIncludePendingQueries > 0)
        }
        assertEquals(
            provider.insertedUri.toString(),
            mediaStoreJournal().getString(operationKey(operationId), null),
        )
    }

    @Test
    fun `journal evicts its oldest entries past the cap`() {
        val storage = DownloadStorage(context)
        val cap = 64

        (0..cap).forEach { index ->
            val saved =
                storage.save(
                    "evict-$index.txt",
                    "text/plain",
                    operationId = "op-$index",
                ) { stream ->
                    stream.write(byteArrayOf(1))
                }
            assertTrue(saved.saved)
        }

        val journal = mediaStoreJournal()
        assertNull(journal.getString(operationKey("op-0"), null))
        assertEquals(
            provider.insertedUri.toString(),
            journal.getString(operationKey("op-1"), null),
        )
        assertEquals(
            provider.insertedUri.toString(),
            journal.getString(operationKey("op-$cap"), null),
        )
        // Eviction must drop the seq sibling with the entry, or orphaned seq
        // Longs accumulate forever — cap ops, cap seqs, one counter.
        assertFalse(journal.contains("seq." + operationKey("op-0")))
        assertEquals(cap * 2 + 1, journal.all.size)
    }

    @Test
    fun `journal eviction spares a live operation`() {
        val storage = DownloadStorage(context)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val executor = Executors.newSingleThreadExecutor()

        try {
            val live =
                executor.submit<DownloadSaveResult> {
                    storage.save("live.txt", "text/plain", operationId = "live-op") { stream ->
                        stream.write(byteArrayOf(1))
                        entered.countDown()
                        check(release.await(5, TimeUnit.SECONDS))
                    }
                }
            assertTrue(entered.await(5, TimeUnit.SECONDS))

            (1..64).forEach { index ->
                val saved =
                    storage.save(
                        "evict-live-$index.txt",
                        "text/plain",
                        operationId = "later-$index",
                    ) { stream -> stream.write(byteArrayOf(1)) }
                assertTrue(saved.saved)
            }

            // The in-flight operation outlasts 64 younger entries; eviction takes
            // the oldest completed one instead.
            val journal = mediaStoreJournal()
            assertNotNull(journal.getString(operationKey("live-op"), null))
            assertNull(journal.getString(operationKey("later-1"), null))
            release.countDown()
            assertTrue(live.get(5, TimeUnit.SECONDS).saved)
        } finally {
            release.countDown()
            executor.shutdownNow()
        }
    }

    @Test
    fun `orphaned pending MediaStore row is swept before the next save`() {
        provider.addRow(99L, pending = true)

        val result =
            DownloadStorage(context).save("report.txt", "text/plain") { stream ->
                stream.write("report".toByteArray())
            }

        assertTrue(result.saved)
        assertTrue(
            provider.deletedUris.contains(
                Uri.parse("content://media/external/downloads/99"),
            ),
        )
        assertTrue(provider.bundleIncludePendingQueries > 0)
        assertTrue(
            provider.queriedSelections.contains(
                "${MediaStore.Downloads.IS_PENDING} = ? AND " +
                    "${MediaStore.MediaColumns.OWNER_PACKAGE_NAME} = ?",
            ),
        )
    }

    @Test
    fun `MediaStore abort after writing deletes the pending row without publishing`() {
        val abort = AtomicBoolean()

        val result =
            DownloadStorage(context).save(
                "aborted.txt",
                "text/plain",
                shouldAbort = abort::get,
            ) { stream ->
                stream.write("partial".toByteArray())
                abort.set(true)
            }

        assertFalse(result.saved)
        assertEquals(0, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
    }

    @Test
    fun `MediaStore abort right after row creation deletes the pending row`() {
        // Cancelled work is never retried, so bailing out here without a
        // delete would leave a journaled row pending forever.
        val result =
            DownloadStorage(context).save(
                "cancelled.txt",
                "text/plain",
                shouldAbort = { provider.insertCalls > 0 },
            ) { stream ->
                stream.write("never".toByteArray())
            }

        assertFalse(result.saved)
        assertEquals(1, provider.insertCalls)
        assertEquals(0, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
        assertFalse(
            mediaStoreJournal().all.values.contains(provider.insertedUri.toString()),
        )
    }

    @Test
    fun `safe file names strip paths replace illegal characters and fall back for dots`() {
        val storage = DownloadStorage(context)

        assertEquals("passwd", storage.failed("../../etc/passwd").name)
        assertEquals("bar.txt", storage.failed("foo\\bar.txt").name)
        assertEquals("bad_name_.txt", storage.failed("bad:name?.txt").name)
        listOf(".", "..", "", "   ").forEach { suggested ->
            val fallback = storage.failed(suggested).name
            assertTrue(fallback.startsWith("omnigent-"))
            assertTrue(fallback != "." && fallback != "..")
        }
    }

    @Test
    @Config(sdk = [28])
    fun `failed app storage write preserves an existing file and deletes its temporary`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val target = File(dir, "existing-download.txt")
        val temporaryFilesBefore =
            dir
                .listFiles()
                .orEmpty()
                .filter(File::isTemporaryDownload)
                .map(File::getName)
                .toSet()
        target.writeText("complete original")

        try {
            val result =
                DownloadStorage(context).save(target.name, "text/plain") { stream ->
                    stream.write("partial".toByteArray())
                    error("network failed")
                }

            assertFalse(result.saved)
            assertEquals("complete original", target.readText())
            assertEquals(
                temporaryFilesBefore,
                dir
                    .listFiles()
                    .orEmpty()
                    .filter(File::isTemporaryDownload)
                    .map(File::getName)
                    .toSet(),
            )
        } finally {
            target.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `same name saves are serialized across storage instances`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val target = File(dir, "concurrent-download.txt")
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val secondEntered = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)

        try {
            val first =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(target.name, "text/plain") { stream ->
                        stream.write("first ".toByteArray())
                        firstEntered.countDown()
                        check(releaseFirst.await(5, TimeUnit.SECONDS))
                        stream.write("complete".toByteArray())
                    }
                }
            assertTrue(firstEntered.await(5, TimeUnit.SECONDS))
            val second =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(target.name, "text/plain") { stream ->
                        secondEntered.countDown()
                        stream.write("second complete".toByteArray())
                    }
                }

            assertFalse(secondEntered.await(200, TimeUnit.MILLISECONDS))
            releaseFirst.countDown()

            assertTrue(first.get(5, TimeUnit.SECONDS).saved)
            assertTrue(second.get(5, TimeUnit.SECONDS).saved)
            assertEquals("second complete", target.readText())
        } finally {
            releaseFirst.countDown()
            executor.shutdownNow()
            target.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `different colliding names save concurrently`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val firstTarget = File(dir, "Aa.txt")
        val secondTarget = File(dir, "BB.txt")
        assertEquals(
            Math.floorMod(firstTarget.name.hashCode(), 32),
            Math.floorMod(secondTarget.name.hashCode(), 32),
        )
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val secondEntered = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)

        try {
            val first =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(firstTarget.name, "text/plain") { stream ->
                        firstEntered.countDown()
                        check(releaseFirst.await(5, TimeUnit.SECONDS))
                        stream.write("first".toByteArray())
                    }
                }
            assertTrue(firstEntered.await(5, TimeUnit.SECONDS))
            val second =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(secondTarget.name, "text/plain") { stream ->
                        secondEntered.countDown()
                        stream.write("second".toByteArray())
                    }
                }

            assertTrue(secondEntered.await(1, TimeUnit.SECONDS))
            assertTrue(second.get(5, TimeUnit.SECONDS).saved)
            releaseFirst.countDown()
            assertTrue(first.get(5, TimeUnit.SECONDS).saved)
            assertEquals("first", firstTarget.readText())
            assertEquals("second", secondTarget.readText())
        } finally {
            releaseFirst.countDown()
            executor.shutdownNow()
            firstTarget.delete()
            secondTarget.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `exact name lock registry releases idle entries`() {
        val target =
            File(
                checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)),
                "registry-release.txt",
            )

        try {
            val result =
                DownloadStorage(context).save(target.name, "text/plain") { stream ->
                    stream.write("complete".toByteArray())
                }
            val locks =
                org.robolectric.util.ReflectionHelpers.getStaticField<Map<*, *>>(
                    DownloadStorage::class.java,
                    "SAVE_LOCKS",
                )

            assertTrue(result.saved)
            assertTrue(locks.isEmpty())
        } finally {
            target.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `waiting for a busy same name lock observes abort`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val target = File(dir, "abort-while-waiting.txt")
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val secondEntered = CountDownLatch(1)
        val abortSecond = AtomicBoolean()
        val executor = Executors.newFixedThreadPool(2)

        try {
            val first =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(target.name, "text/plain") { stream ->
                        firstEntered.countDown()
                        check(releaseFirst.await(5, TimeUnit.SECONDS))
                        stream.write("first".toByteArray())
                    }
                }
            assertTrue(firstEntered.await(5, TimeUnit.SECONDS))
            val second =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(
                        target.name,
                        "text/plain",
                        shouldAbort = abortSecond::get,
                    ) { stream ->
                        secondEntered.countDown()
                        stream.write("second".toByteArray())
                    }
                }

            assertFalse(secondEntered.await(200, TimeUnit.MILLISECONDS))
            abortSecond.set(true)
            assertFalse(second.get(2, TimeUnit.SECONDS).saved)
            assertEquals(1L, secondEntered.count)
            releaseFirst.countDown()
            assertTrue(first.get(5, TimeUnit.SECONDS).saved)
        } finally {
            releaseFirst.countDown()
            executor.shutdownNow()
            target.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `temporary sweep preserves another active save`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val firstTarget = File(dir, "live-first.txt")
        val secondTarget =
            generateSequence(0, Int::inc)
                .map { index -> File(dir, "sweep-second-$index.txt") }
                .first { target ->
                    Math.floorMod(target.name.hashCode(), 32) !=
                        Math.floorMod(firstTarget.name.hashCode(), 32)
                }
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val executor = Executors.newSingleThreadExecutor()

        try {
            val first =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(firstTarget.name, "text/plain") { stream ->
                        stream.write("still active".toByteArray())
                        firstEntered.countDown()
                        check(releaseFirst.await(5, TimeUnit.SECONDS))
                    }
                }
            assertTrue(firstEntered.await(5, TimeUnit.SECONDS))

            val second =
                DownloadStorage(context).save(secondTarget.name, "text/plain") { stream ->
                    stream.write("second".toByteArray())
                }

            assertTrue(second.saved)
            releaseFirst.countDown()
            assertTrue(first.get(5, TimeUnit.SECONDS).saved)
            assertEquals("still active", firstTarget.readText())
            assertEquals("second", secondTarget.readText())
        } finally {
            releaseFirst.countDown()
            executor.shutdownNow()
            firstTarget.delete()
            secondTarget.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `app storage save sweeps a stale temporary from a dead process`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val stale = File(dir, ".omnigent-dead-process.tmp")
        val target = File(dir, "fresh-download.txt")
        stale.writeText("partial")

        try {
            val result =
                DownloadStorage(context).save(target.name, "text/plain") { stream ->
                    stream.write("complete".toByteArray())
                }

            assertTrue(result.saved)
            assertFalse(stale.exists())
            assertEquals("complete", target.readText())
        } finally {
            stale.delete()
            target.delete()
        }
    }

    private companion object {
        const val MEDIA_STORE_JOURNAL = "ai.omnigent.android.download_media_store"
    }

    private fun mediaStoreJournal() =
        context.getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)

    private fun rememberMediaStoreRow(
        operationId: String,
        uri: Uri,
    ) {
        mediaStoreJournal()
            .edit()
            .putString(operationKey(operationId), uri.toString())
            .commit()
    }

    private fun insertMediaStoreRow(pending: Boolean) {
        val values =
            ContentValues().apply {
                put(MediaStore.Downloads.IS_PENDING, if (pending) 1 else 0)
            }
        assertEquals(
            provider.insertedUri,
            context.contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values),
        )
    }

    private fun operationKey(operationId: String): String =
        MessageDigest
            .getInstance("SHA-256")
            .digest(operationId.toByteArray(Charsets.UTF_8))
            .joinToString(separator = "") { byte ->
                Integer.toHexString(byte.toInt() and 0xff).padStart(2, '0')
            }
}

private fun File.isTemporaryDownload(): Boolean =
    name.startsWith(".omnigent-") && name.endsWith(".tmp")

private class RecordingMediaProvider : ContentProvider() {
    // Literal rather than MediaStore.Downloads.EXTERNAL_CONTENT_URI: that constant
    // is null below Q, and the pre-Q cases build this provider too.
    val insertedUri: Uri = Uri.parse("content://media/external/downloads/42")
    var insertedValues: ContentValues? = null
    var updatedValues: ContentValues? = null
    var updateResult = 1
    var insertCalls = 0
    var updateCalls = 0
    var deleteCalls = 0
    var bundleIncludePendingQueries = 0
    var uriIncludePendingQueries = 0
    val deletedUris = mutableListOf<Uri>()
    val queriedSelections = mutableListOf<String?>()
    private val rows = mutableMapOf<Long, MediaRow>()

    override fun onCreate(): Boolean = true

    fun addRow(
        id: Long,
        pending: Boolean,
    ) {
        rows[id] = MediaRow(pending, context?.packageName ?: TEST_PACKAGE)
    }

    override fun insert(
        uri: Uri,
        values: ContentValues?,
    ): Uri {
        insertCalls++
        addRow(
            42L,
            pending = (values?.getAsInteger(MediaStore.Downloads.IS_PENDING) ?: 1) != 0,
        )
        insertedValues = values?.let { ContentValues(it) }
        return insertedUri
    }

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        updateCalls++
        updatedValues = values?.let { ContentValues(it) }
        if (updateResult > 0) {
            uri.rowId()?.let { id ->
                rows[id]?.pending = values?.getAsInteger(MediaStore.Downloads.IS_PENDING) != 0
            }
        }
        return updateResult
    }

    override fun delete(
        uri: Uri,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        deleteCalls++
        deletedUris += uri
        uri.rowId()?.let(rows::remove)
        return 1
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        queryArgs: Bundle?,
        cancellationSignal: CancellationSignal?,
    ): Cursor? {
        val includePending =
            queryArgs?.getInt(MediaStore.QUERY_ARG_MATCH_PENDING) == MediaStore.MATCH_INCLUDE
        if (includePending) bundleIncludePendingQueries++
        return queryRows(
            uri,
            projection,
            queryArgs?.getString(ContentResolver.QUERY_ARG_SQL_SELECTION),
            queryArgs?.getStringArray(ContentResolver.QUERY_ARG_SQL_SELECTION_ARGS),
            includePending,
        )
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor? {
        val includePending = uri.getQueryParameter(INCLUDE_PENDING_PARAMETER) == "1"
        if (includePending) uriIncludePendingQueries++
        return queryRows(uri, projection, selection, selectionArgs, includePending)
    }

    private fun queryRows(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        includePending: Boolean,
    ): Cursor {
        queriedSelections += selection
        val columns = projection ?: emptyArray()
        val cursor = MatrixCursor(columns)
        val candidates =
            uri.rowId()?.let { id -> rows[id]?.let { row -> listOf(id to row) }.orEmpty() }
                ?: rows.toList()
        candidates
            .filter { (_, row) -> !row.pending || includePending }
            .filter { (_, row) -> matchesSelection(row, selection, selectionArgs) }
            .forEach { (id, row) ->
                cursor.addRow(
                    columns.map { column ->
                        when (column) {
                            MediaStore.Downloads.IS_PENDING -> if (row.pending) 1 else 0
                            MediaStore.Downloads._ID -> id
                            MediaStore.MediaColumns.OWNER_PACKAGE_NAME -> row.ownerPackageName
                            else -> null
                        }
                    },
                )
            }
        return cursor
    }

    private fun matchesSelection(
        row: MediaRow,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Boolean {
        if (selection == null) return true
        if (selection.contains(MediaStore.Downloads.IS_PENDING)) {
            val expectedPending = selectionArgs?.firstOrNull() ?: return false
            if ((if (row.pending) "1" else "0") != expectedPending) return false
        }
        if (selection.contains(MediaStore.MediaColumns.OWNER_PACKAGE_NAME)) {
            val expectedOwner = selectionArgs?.lastOrNull() ?: return false
            if (row.ownerPackageName != expectedOwner) return false
        }
        return true
    }

    override fun getType(uri: Uri): String? = null

    private fun Uri.rowId(): Long? = lastPathSegment?.toLongOrNull()

    private data class MediaRow(
        var pending: Boolean,
        val ownerPackageName: String,
    )

    private companion object {
        const val INCLUDE_PENDING_PARAMETER = "includePending"
        const val TEST_PACKAGE = "ai.omnigent.android"
    }
}
