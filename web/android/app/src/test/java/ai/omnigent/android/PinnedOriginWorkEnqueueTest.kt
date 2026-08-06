@file:Suppress("RestrictedApi")

package ai.omnigent.android

import android.app.Application
import android.webkit.CookieManager
import androidx.test.core.app.ApplicationProvider
import androidx.work.WorkManager
import androidx.work.impl.WorkManagerImpl
import androidx.work.testing.WorkManagerTestInitHelper
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowCookieManager
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class PinnedOriginWorkEnqueueTest {
    private lateinit var context: Application
    private lateinit var workManager: WorkManagerImpl

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ShadowCookieManager.resetCookies()
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        workManager = checkNotNull(WorkManagerImpl.getInstance(context))
    }

    @After
    fun tearDown() {
        WorkManager
            .getInstance(context)
            .cancelAllWork()
            .result
            .get(5, TimeUnit.SECONDS)
        WorkManagerTestInitHelper.closeWorkDatabase()
        ShadowCookieManager.resetCookies()
    }

    @Test
    fun `enqueued database input contains no session cookie`() {
        val url = "$PINNED_ORIGIN/private/report"
        CookieManager.getInstance().setCookie(url, SESSION_COOKIE)

        PinnedOriginDownloader(context).download(
            url,
            PINNED_ORIGIN,
            USER_AGENT,
            "text/plain",
            "report.txt",
        )

        val workSpec =
            workManager.workDatabase.workSpecDao().let { dao ->
                dao.getWorkSpec(dao.getAllWorkSpecIds().single())
            }
        val input = checkNotNull(workSpec).input.keyValueMap
        assertEquals(
            mapOf(
                PinnedOriginDownloadWorker.KEY_URL to url,
                PinnedOriginDownloadWorker.KEY_PINNED_ORIGIN to PINNED_ORIGIN,
                PinnedOriginDownloadWorker.KEY_USER_AGENT to USER_AGENT,
                PinnedOriginDownloadWorker.KEY_MIME_TYPE to "text/plain",
                PinnedOriginDownloadWorker.KEY_SUGGESTED_NAME to "report.txt",
            ),
            input,
        )
        assertFalse(input.keys.any { key -> key.contains("cookie", ignoreCase = true) })
        assertFalse(input.values.contains(SESSION_COOKIE))
    }

    @Test
    fun `repeated tap keeps the existing pending unique transfer`() {
        val downloader = PinnedOriginDownloader(context)
        downloader.download(
            "$PINNED_ORIGIN/report",
            PINNED_ORIGIN,
            USER_AGENT,
            "text/plain",
            "report.txt",
        )
        val firstId =
            workManager.workDatabase
                .workSpecDao()
                .getAllWorkSpecIds()
                .single()

        downloader.download(
            "$PINNED_ORIGIN/report",
            PINNED_ORIGIN,
            USER_AGENT,
            "text/plain",
            "report.txt",
        )

        assertEquals(
            listOf(firstId),
            workManager.workDatabase.workSpecDao().getAllWorkSpecIds(),
        )
    }

    @Test
    fun `different download URLs enqueue distinct pending transfers`() {
        val downloader = PinnedOriginDownloader(context)
        downloader.download(
            "$PINNED_ORIGIN/first",
            PINNED_ORIGIN,
            USER_AGENT,
            "text/plain",
            "first.txt",
        )
        downloader.download(
            "$PINNED_ORIGIN/second",
            PINNED_ORIGIN,
            USER_AGENT,
            "text/plain",
            "second.txt",
        )

        assertEquals(
            2,
            workManager.workDatabase
                .workSpecDao()
                .getAllWorkSpecIds()
                .size,
        )
    }

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val SESSION_COOKIE = "__Host-ap_session=secret"
        const val USER_AGENT = "OmnigentTest/1.0"
    }
}
