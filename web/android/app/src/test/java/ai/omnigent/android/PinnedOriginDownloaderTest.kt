@file:Suppress("RestrictedApi")

package ai.omnigent.android

import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import android.util.AndroidRuntimeException
import android.util.Log
import android.webkit.CookieManager
import androidx.concurrent.futures.ResolvableFuture
import androidx.test.core.app.ApplicationProvider
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.ForegroundUpdater
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.WorkInfo
import androidx.work.testing.TestListenableWorkerBuilder
import com.google.common.util.concurrent.ListenableFuture
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
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
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.shadows.ShadowContentResolver
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowLog
import org.robolectric.shadows.ShadowNotificationManager
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class PinnedOriginDownloaderTest {
    private lateinit var context: Application
    private lateinit var pinnedServer: HttpServer
    private lateinit var otherServer: HttpServer
    private lateinit var pinnedOrigin: String
    private lateinit var otherOrigin: String
    private val savedFiles = mutableListOf<File>()

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ShadowCookieManager.resetCookies()
        ShadowLog.clear()
        ShadowNotificationManager.reset()
        context
            .getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        context
            .getSharedPreferences(
                PinnedOriginDownloadWorker.STOP_COUNT_PREFERENCES,
                Context.MODE_PRIVATE,
            ).edit()
            .clear()
            .commit()
        pinnedServer = server()
        otherServer = server()
        pinnedOrigin = originOf(pinnedServer)
        otherOrigin = originOf(otherServer)
    }

    @After
    fun tearDown() {
        pinnedServer.stop(0)
        otherServer.stop(0)
        savedFiles.forEach(File::delete)
        ShadowCookieManager.resetCookies()
        ShadowLog.clear()
        ShadowNotificationManager.reset()
        ShadowContentResolver.reset()
    }

    @Test
    fun `cookie is dropped permanently after a redirect leaves the pinned origin`() {
        val firstCookie = AtomicReference<String?>()
        val sameOriginCookie = AtomicReference<String?>()
        val otherCookie = AtomicReference<String?>()
        val returnedCookie = AtomicReference<String?>()
        val userAgents = ConcurrentLinkedQueue<String>()
        pinnedServer.createContext("/start") { exchange ->
            firstCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.redirect("/same-origin")
        }
        pinnedServer.createContext("/same-origin") { exchange ->
            sameOriginCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.redirect("$otherOrigin/middle")
        }
        otherServer.createContext("/middle") { exchange ->
            otherCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.redirect("$pinnedOrigin/final")
        }
        pinnedServer.createContext("/final") { exchange ->
            returnedCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val target = targetFile("redirected.txt")
        val worker = worker("$pinnedOrigin/start", target.name)
        CookieManager.getInstance().setCookie(pinnedOrigin, SESSION_COOKIE)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.success(), result)
        assertEquals(SESSION_COOKIE, firstCookie.get())
        assertEquals(SESSION_COOKIE, sameOriginCookie.get())
        assertNull(otherCookie.get())
        assertNull(returnedCookie.get())
        assertEquals(listOf(USER_AGENT, USER_AGENT, USER_AGENT, USER_AGENT), userAgents.toList())
        assertEquals(DOWNLOAD_BODY, target.readText())
        val notification = notificationFor(worker)
        assertNotNull(notification)
        assertEquals(
            "Saved ${target.name} to app storage",
            notification!!.extras.getCharSequence(android.app.Notification.EXTRA_TEXT),
        )
    }

    @Test
    fun `cookie is dropped when a redirect changes only the host string`() {
        val redirectedCookie = AtomicReference<String?>()
        pinnedServer.createContext("/host-start") { exchange ->
            exchange.redirect("http://localhost:${pinnedServer.address.port}/host-final")
        }
        pinnedServer.createContext("/host-final") { exchange ->
            redirectedCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            exchange.respond(200, DOWNLOAD_BODY)
        }
        CookieManager.getInstance().setCookie(pinnedOrigin, SESSION_COOKIE)
        val target = targetFile("different-host.txt")

        val result = worker("$pinnedOrigin/host-start", target.name).doWork()

        assertEquals(ListenableWorker.Result.success(), result)
        assertNull(redirectedCookie.get())
        assertEquals(DOWNLOAD_BODY, target.readText())
    }

    @Test
    @Config(sdk = [28], shadows = [RecordingCookieManagerShadow::class])
    fun `each same-origin redirect hop gets the cookie for its own URL`() {
        RecordingCookieManagerShadow.reset()
        // Path-scoped cookies differ per hop; reusing the initial URL's header
        // would send /download's cookie to /protected/file.
        RecordingCookieManagerShadow.cookieForUrl = { url ->
            when (Uri.parse(url).path) {
                "/download" -> "scoped=download"
                "/protected/file" -> "scoped=protected"
                else -> null
            }
        }
        val downloadCookie = AtomicReference<String?>()
        val protectedCookie = AtomicReference<String?>()
        pinnedServer.createContext("/download") { exchange ->
            downloadCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            exchange.redirect("/protected/file")
        }
        pinnedServer.createContext("/protected/file") { exchange ->
            protectedCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val target = targetFile("scoped.txt")

        val result =
            try {
                worker("$pinnedOrigin/download", target.name).doWork()
            } finally {
                RecordingCookieManagerShadow.reset()
            }

        assertEquals(ListenableWorker.Result.success(), result)
        assertEquals("scoped=download", downloadCookie.get())
        assertEquals("scoped=protected", protectedCookie.get())
        assertEquals(DOWNLOAD_BODY, target.readText())
    }

    @Test
    fun `proxy auth redirect is rejected with a sign in again outcome`() {
        val idpHits = AtomicInteger()
        pinnedServer.createContext("/expired") { exchange ->
            exchange.redirect(
                "$otherOrigin/authorize?redirect_uri=" +
                    Uri.encode("$pinnedOrigin/after-login"),
            )
        }
        otherServer.createContext("/authorize") { exchange ->
            idpHits.incrementAndGet()
            exchange.respond(200, "login page")
        }
        val target = targetFile("expired-session.pdf")
        val worker = worker("$pinnedOrigin/expired", target.name)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(0, idpHits.get())
        assertFalse(target.exists())
        assertEquals(
            "Couldn't download ${target.name}. Sign in again and retry.",
            notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
        )
    }

    @Test
    fun `terminal HTML is rejected when a non HTML file was requested`() {
        val responseTypes = listOf("text/html; charset=utf-8", "application/xhtml+xml")
        responseTypes.forEachIndexed { index, contentType ->
            val path = "/expired-without-redirect-$index"
            pinnedServer.createContext(path) { exchange ->
                exchange.responseHeaders.add("Content-Type", contentType)
                exchange.respond(200, "<html>sign in</html>")
            }
            val target = targetFile("expired-without-redirect-$index.pdf")
            val worker =
                worker(
                    "$pinnedOrigin$path",
                    target.name,
                    mimeType = "application/pdf",
                )

            val result = worker.doWork()

            assertEquals(ListenableWorker.Result.failure(), result)
            assertFalse(target.exists())
            assertEquals(
                "Couldn't download ${target.name}. Sign in again and retry.",
                notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
            )
        }
    }

    @Test
    fun `terminal HTML is allowed when caller type is absent blank or HTML`() {
        val requestedTypes = listOf<String?>(null, "", "text/html", "application/xhtml+xml")
        requestedTypes.forEachIndexed { index, mimeType ->
            val path = "/legitimate-html-$index"
            val body = "<html>report $index</html>"
            pinnedServer.createContext(path) { exchange ->
                exchange.responseHeaders.add("Content-Type", "text/html; charset=utf-8")
                exchange.respond(200, body)
            }
            val target = targetFile("legitimate-html-$index.html")

            val result = worker("$pinnedOrigin$path", target.name, mimeType = mimeType).doWork()

            assertEquals(ListenableWorker.Result.success(), result)
            assertEquals(body, target.readText())
        }
    }

    @Test
    fun `terminal HTML with no caller type is rejected for a non-HTML file name`() {
        // onDownloadStart commonly supplies no MIME type; a proxy's 200 HTML
        // login page must still not be saved as the requested report.pdf.
        listOf<String?>(null, "").forEachIndexed { index, mimeType ->
            val path = "/login-page-$index"
            pinnedServer.createContext(path) { exchange ->
                exchange.responseHeaders.add("Content-Type", "text/html; charset=utf-8")
                exchange.respond(200, "<html>sign in</html>")
            }
            val target = targetFile("quarterly-report-$index.pdf")
            val worker = worker("$pinnedOrigin$path", target.name, mimeType = mimeType)

            val result = worker.doWork()

            assertEquals(ListenableWorker.Result.failure(), result)
            assertFalse(target.exists())
            assertEquals(
                "Couldn't download ${target.name}. Sign in again and retry.",
                notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
            )
        }
    }

    @Test
    @Config(sdk = [28], shadows = [UnavailableCookieManagerShadow::class])
    fun `an unavailable WebView provider still notifies and fails the download`() {
        val target = targetFile("no-webview.txt")
        val worker =
            worker(
                "$pinnedOrigin/file",
                target.name,
                runAttemptCount = PinnedOriginDownloadWorker.MAX_ATTEMPTS - 1,
            )

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertFalse(target.exists())
        assertNotNull(notificationFor(worker))
    }

    @Test
    @Config(sdk = [28], shadows = [UnavailableCookieManagerShadow::class])
    fun `an unavailable WebView provider is retried like a transient failure`() {
        val target = targetFile("no-webview-retry.txt")
        val worker = worker("$pinnedOrigin/file", target.name)

        assertEquals(ListenableWorker.Result.retry(), worker.doWork())
        assertFalse(target.exists())
    }

    @Test
    fun `stop between redirect hops does not fetch the next hop`() {
        val nextHopHits = AtomicInteger()
        val workerReference = AtomicReference<PinnedOriginDownloadWorker>()
        pinnedServer.createContext("/stop-after-hop") { exchange ->
            exchange.responseHeaders.add("Location", "$pinnedOrigin/should-not-run")
            exchange.sendResponseHeaders(302, -1)
            workerReference.get().stop(WorkInfo.STOP_REASON_TIMEOUT)
            exchange.close()
        }
        pinnedServer.createContext("/should-not-run") { exchange ->
            nextHopHits.incrementAndGet()
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val target = targetFile("stopped-redirect.txt")
        val worker = worker("$pinnedOrigin/stop-after-hop", target.name)
        workerReference.set(worker)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.retry(), result)
        assertEquals(0, nextHopHits.get())
        assertFalse(target.exists())
        assertNull(notificationFor(worker))
    }

    @Test
    fun `pinned origin canonicalizes hosts the same way as the enqueue-side gate`() {
        val worker = worker("$pinnedOrigin/file", "unicode.txt")

        // The redirect target arrives in its unicode form while the pin was
        // canonicalized to punycode at enqueue — the worker must agree.
        assertTrue(
            worker.hasPinnedOrigin(
                URL("https://faß.de/file"),
                "https://xn--fa-hia.de",
            ),
        )
        assertTrue(
            worker.hasPinnedOrigin(
                URL("https://pinned.example:443/file"),
                "https://pinned.example",
            ),
        )
    }

    @Test
    fun `pinned origin rejects a protocol change when host and port match`() {
        val worker = worker("$pinnedOrigin/file", "protocol.txt")

        assertFalse(
            worker.hasPinnedOrigin(
                URL("https://pinned.example:8443/file"),
                "http://pinned.example:8443",
            ),
        )
    }

    @Test
    fun `pinned origin rejects a port change when protocol and host match`() {
        val worker = worker("$pinnedOrigin/file", "port.txt")

        assertFalse(
            worker.hasPinnedOrigin(
                URL("https://pinned.example:8444/file"),
                "https://pinned.example:8443",
            ),
        )
    }

    @Test
    fun `redirect chains are capped`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/loop") { exchange ->
            hits.incrementAndGet()
            val step =
                exchange.requestURI.query
                    ?.substringAfter("step=")
                    ?.toInt() ?: 0
            if (step < 11) {
                exchange.redirect("$pinnedOrigin/loop?step=${step + 1}")
            } else {
                exchange.respond(200, DOWNLOAD_BODY)
            }
        }
        val target = targetFile("too-many-redirects.txt")
        val worker = worker("$pinnedOrigin/loop?step=0", target.name)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(11, hits.get())
        assertFalse(target.exists())
        assertNotNull(notificationFor(worker))
        val error = ShadowLog.getLogsForTag("PinnedOriginDownloader").single()
        assertEquals(Log.ERROR, error.type)
        assertEquals("Too many redirects", error.throwable.message)
    }

    @Test
    fun `enqueued work requires connected network and configured backoff`() {
        val calls = mutableListOf<EnqueueCall>()
        val downloader = recordingDownloader(calls)

        downloader.download(
            "$pinnedOrigin/report",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "report.txt",
        )

        val workSpec = calls.single().request.workSpec
        assertEquals(NetworkType.CONNECTED, workSpec.constraints.requiredNetworkType)
        assertEquals(TimeUnit.SECONDS.toMillis(45), workSpec.backoffDelayDuration)
    }

    @Test
    fun `off-origin work is rejected before enqueue`() {
        val calls = mutableListOf<EnqueueCall>()
        val downloader = recordingDownloader(calls)

        downloader.download(
            "$otherOrigin/report",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "report.txt",
        )

        assertTrue(calls.isEmpty())
    }

    @Test
    fun `HTTP 500 is retried before the attempt cap`() {
        pinnedServer.createContext("/unavailable") { exchange ->
            exchange.respond(503, "try later")
        }
        val worker = worker("$pinnedOrigin/unavailable", "retry.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.retry(), result)
        assertNull(notificationFor(worker))
    }

    @Test
    fun `HTTP 500 fails when the bounded attempt cap is reached`() {
        pinnedServer.createContext("/still-unavailable") { exchange ->
            exchange.respond(503, "try later")
        }
        val worker =
            worker(
                "$pinnedOrigin/still-unavailable",
                "retry-exhausted.txt",
                PinnedOriginDownloadWorker.MAX_ATTEMPTS - 1,
            )

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        val notification = notificationFor(worker)
        assertNotNull(notification)
        assertEquals(
            "Download failed",
            notification!!.extras.getCharSequence(android.app.Notification.EXTRA_TITLE),
        )
    }

    @Test
    fun `HTTP 4xx is terminal without a retry`() {
        pinnedServer.createContext("/missing") { exchange ->
            exchange.respond(404, "not found")
        }
        val worker = worker("$pinnedOrigin/missing", "missing.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Download failed with HTTP 404",
            ShadowLog
                .getLogsForTag("PinnedOriginDownloader")
                .single()
                .throwable.message,
        )
    }

    @Test
    fun `HTTP 408 and 429 are transient`() {
        listOf(408, 429).forEach { status ->
            val path = "/transient-$status"
            pinnedServer.createContext(path) { exchange ->
                exchange.respond(status, "try later")
            }

            val result = worker("$pinnedOrigin$path", "$status.txt").doWork()

            assertEquals(ListenableWorker.Result.retry(), result)
        }
    }

    @Test
    fun `sane Retry-After is used before WorkManager backoff`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/throttled") { exchange ->
            hits.incrementAndGet()
            exchange.responseHeaders.add("Retry-After", "0")
            exchange.respond(429, "try later")
        }

        val result = worker("$pinnedOrigin/throttled", "throttled.txt").doWork()

        assertEquals(ListenableWorker.Result.retry(), result)
        assertEquals(2, hits.get())
    }

    @Test
    fun `absurd Retry-After is ignored`() {
        val worker = worker("$pinnedOrigin/file", "absurd-retry-after.txt")

        val delay =
            org.robolectric.util.ReflectionHelpers.callInstanceMethod<Long?>(
                worker,
                "retryAfterMillis",
                org.robolectric.util.ReflectionHelpers.ClassParameter.from(
                    String::class.java,
                    "9999999",
                ),
            )

        assertNull(delay)
    }

    @Test
    fun `rejected initial origin is terminal without a retry`() {
        val worker =
            TestListenableWorkerBuilder<PinnedOriginDownloadWorker>(
                context = context,
                inputData =
                    PinnedOriginDownloadWorker.inputData(
                        "$otherOrigin/file",
                        pinnedOrigin,
                        USER_AGENT,
                        "text/plain",
                        "rejected.txt",
                    ),
                runAttemptCount = 0,
            ).build()

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Rejected download origin",
            ShadowLog
                .getLogsForTag("PinnedOriginDownloader")
                .single()
                .throwable.message,
        )
    }

    @Test
    fun `redirect without Location is terminal without a retry`() {
        pinnedServer.createContext("/missing-location") { exchange ->
            exchange.sendResponseHeaders(302, -1)
            exchange.close()
        }
        val worker = worker("$pinnedOrigin/missing-location", "missing-location.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Redirect missing Location",
            ShadowLog
                .getLogsForTag("PinnedOriginDownloader")
                .single()
                .throwable.message,
        )
    }

    @Test
    fun `non-http redirect is terminal without a retry`() {
        pinnedServer.createContext("/ftp") { exchange ->
            exchange.redirect("ftp://files.example.com/report.txt")
        }
        val worker = worker("$pinnedOrigin/ftp", "ftp.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Unsupported redirect scheme",
            ShadowLog
                .getLogsForTag("PinnedOriginDownloader")
                .single()
                .throwable.message,
        )
    }

    @Test
    fun `download submitted after shutdown is dropped with a warning`() {
        val calls = mutableListOf<EnqueueCall>()
        val downloader = recordingDownloader(calls)
        downloader.shutdown()

        downloader.download(
            "$pinnedOrigin/file",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "after-shutdown.txt",
        )

        assertTrue(calls.isEmpty())
        val warning = ShadowLog.getLogsForTag("PinnedOriginDownloader").single()
        assertEquals(Log.WARN, warning.type)
        assertEquals("Dropping download because the worker is shut down", warning.msg)
    }

    @Test
    fun `oversized WorkManager input is surfaced without enqueueing`() {
        val calls = mutableListOf<EnqueueCall>()
        val downloader = recordingDownloader(calls)
        val longUrl = "$pinnedOrigin/file?token=${"x".repeat(11_000)}"

        downloader.download(
            longUrl,
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "too-large.txt",
        )

        assertTrue(calls.isEmpty())
        val notification = shadowNotificationManager().allNotifications.single()
        assertEquals(
            "Download failed",
            notification.extras.getCharSequence(Notification.EXTRA_TITLE),
        )
    }

    @Test
    fun `missing input fails after requesting foreground execution`() {
        val foreground = RecordingForegroundUpdater()
        val worker =
            TestListenableWorkerBuilder<PinnedOriginDownloadWorker>(context = context)
                .setForegroundUpdater(foreground)
                .build()

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(listOf(worker.id), foreground.workIds)
        assertEquals(
            "Missing download input",
            ShadowLog
                .getLogsForTag(PinnedOriginDownloadWorker.TAG)
                .single()
                .throwable.message,
        )
    }

    @Test
    @Config(sdk = [35])
    fun `foreground download declares the data sync service type`() {
        val info = worker("$pinnedOrigin/file", "foreground.txt").getForegroundInfo()

        assertEquals(
            android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
            info.foregroundServiceType,
        )
        assertTrue(info.notification.flags and Notification.FLAG_ONGOING_EVENT != 0)
    }

    @Test
    fun `foreground notification sanitizes the persisted filename`() {
        val info = worker("$pinnedOrigin/file", "../../secret report.txt").getForegroundInfo()

        assertEquals(
            "Downloading secret_report.txt",
            info.notification.extras.getCharSequence(Notification.EXTRA_TITLE),
        )
    }

    @Test
    fun `pre Q foreground download omits a service type`() {
        val info = worker("$pinnedOrigin/file", "foreground.txt").getForegroundInfo()

        assertEquals(0, info.foregroundServiceType)
    }

    @Test
    fun `foreground setup failure degrades to a working download`() {
        pinnedServer.createContext("/foreground-denied") { exchange ->
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val target = targetFile("foreground-denied.txt")
        val foreground = RecordingForegroundUpdater(SecurityException("denied"))
        val worker = worker("$pinnedOrigin/foreground-denied", target.name, foreground = foreground)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.success(), result)
        assertEquals(DOWNLOAD_BODY, target.readText())
    }

    @Test
    fun `foreground setup failure after a stop fails without another transfer`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/foreground-denied-after-stop") { exchange ->
            hits.incrementAndGet()
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val workId = UUID.randomUUID()
        worker("$pinnedOrigin/foreground-denied-after-stop", "prior-life.txt", id = workId)
            .stop(WorkInfo.STOP_REASON_TIMEOUT)
        val target = targetFile("foreground-denied-after-stop.txt")
        val foreground = RecordingForegroundUpdater(SecurityException("denied"))
        val worker =
            worker(
                "$pinnedOrigin/foreground-denied-after-stop",
                target.name,
                foreground = foreground,
                id = workId,
            )

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(0, hits.get())
        assertFalse(target.exists())
        assertNotNull(notificationFor(worker))
        assertFalse(stopCounts().contains(workId.toString()))
    }

    @Test
    fun `a run past the lives bound fails terminally before another transfer`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/many-lives") { exchange ->
            hits.incrementAndGet()
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val worker =
            worker(
                "$pinnedOrigin/many-lives",
                "lives.txt",
                runAttemptCount = PinnedOriginDownloadWorker.MAX_LIVES,
            )

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(0, hits.get())
        assertEquals(
            "Couldn't download lives.txt",
            notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
        )
    }

    @Test
    fun `early terminal failure sanitizes the persisted filename`() {
        val worker =
            worker(
                "$pinnedOrigin/many-lives",
                "../../secret report.txt",
                runAttemptCount = PinnedOriginDownloadWorker.MAX_LIVES,
            )

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(
            "Couldn't download secret_report.txt",
            notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
        )
    }

    @Test
    fun `stopped terminal failure clears its persisted stop count`() {
        val workId = UUID.randomUUID()
        val worker =
            worker(
                "$pinnedOrigin/many-lives",
                "stopped-lives.txt",
                runAttemptCount = PinnedOriginDownloadWorker.MAX_LIVES,
                id = workId,
            )
        worker.stop(WorkInfo.STOP_REASON_TIMEOUT)
        assertTrue(stopCounts().contains(workId.toString()))

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertFalse(stopCounts().contains(workId.toString()))
    }

    @Test
    fun `three WorkManager stops terminate the operation before another transfer`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/stop-limit") { exchange ->
            hits.incrementAndGet()
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val workId = UUID.randomUUID()
        repeat(PinnedOriginDownloadWorker.MAX_STOPS) {
            worker("$pinnedOrigin/stop-limit", "stop-limit.txt", id = workId)
                .stop(WorkInfo.STOP_REASON_TIMEOUT)
        }
        assertEquals(
            PinnedOriginDownloadWorker.MAX_STOPS,
            stopCounts().getInt(workId.toString(), 0),
        )
        val worker = worker("$pinnedOrigin/stop-limit", "stop-limit.txt", id = workId)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(0, hits.get())
        assertEquals(
            "Couldn't download stop-limit.txt",
            notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
        )
        assertFalse(stopCounts().contains(workId.toString()))
    }

    @Test
    fun `terminal success and failure clear persisted stop counts`() {
        pinnedServer.createContext("/clear-stop-success") { exchange ->
            exchange.respond(200, DOWNLOAD_BODY)
        }
        pinnedServer.createContext("/clear-stop-failure") { exchange ->
            exchange.respond(404, "missing")
        }
        val successId = UUID.randomUUID()
        val failureId = UUID.randomUUID()
        stopCounts()
            .edit()
            .putInt(successId.toString(), 1)
            .putInt(failureId.toString(), 2)
            .commit()

        val success =
            worker(
                "$pinnedOrigin/clear-stop-success",
                targetFile("clear-stop-success.txt").name,
                id = successId,
            ).doWork()
        val failure =
            worker(
                "$pinnedOrigin/clear-stop-failure",
                "clear-stop-failure.txt",
                id = failureId,
            ).doWork()

        assertEquals(ListenableWorker.Result.success(), success)
        assertEquals(ListenableWorker.Result.failure(), failure)
        assertFalse(stopCounts().contains(successId.toString()))
        assertFalse(stopCounts().contains(failureId.toString()))
    }

    @Test
    fun `stopped copy refuses to write another byte`() {
        val worker = worker("$pinnedOrigin/file", "stopped-copy.txt")
        val output = ByteArrayOutputStream()
        worker.stop(WorkInfo.STOP_REASON_TIMEOUT)

        val failure =
            runCatching {
                org.robolectric.util.ReflectionHelpers.callInstanceMethod<Unit>(
                    worker,
                    "copyUntilStopped",
                    org.robolectric.util.ReflectionHelpers.ClassParameter.from(
                        java.io.InputStream::class.java,
                        ByteArrayInputStream("should not be written".toByteArray()),
                    ),
                    org.robolectric.util.ReflectionHelpers.ClassParameter.from(
                        OutputStream::class.java,
                        output,
                    ),
                )
            }.exceptionOrNull()

        assertNotNull(failure)
        assertEquals(0, output.size())
    }

    @Test
    fun `stopping a blocked response disconnects promptly without publishing`() {
        val firstChunkSent = CountDownLatch(1)
        val releaseServer = CountDownLatch(1)
        pinnedServer.createContext("/blocked") { exchange ->
            exchange.sendResponseHeaders(200, 16_384)
            exchange.responseBody.use { body ->
                body.write(ByteArray(8_192))
                body.flush()
                firstChunkSent.countDown()
                check(releaseServer.await(5, TimeUnit.SECONDS))
                runCatching { body.write(ByteArray(8_192)) }
            }
        }
        val target = targetFile("stopped-response.txt")
        val worker = worker("$pinnedOrigin/blocked", target.name)
        val executor = Executors.newSingleThreadExecutor()

        try {
            val future = executor.submit<ListenableWorker.Result> { worker.doWork() }
            assertTrue(firstChunkSent.await(5, TimeUnit.SECONDS))

            worker.stop(WorkInfo.STOP_REASON_TIMEOUT)

            assertEquals(ListenableWorker.Result.retry(), future.get(2, TimeUnit.SECONDS))
            assertFalse(target.exists())
            assertNull(notificationFor(worker))
        } finally {
            releaseServer.countDown()
            executor.shutdownNow()
        }
    }

    @Test
    @Config(sdk = [35])
    fun `worker publishes a successful MediaStore download and reports Downloads`() {
        pinnedServer.createContext("/media-success") { exchange ->
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val provider = WorkerMediaProvider()
        val output = ByteArrayOutputStream()
        registerMediaProvider(provider, output)
        val worker = worker("$pinnedOrigin/media-success", "media-success.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.success(), result)
        assertEquals(1, provider.insertCalls)
        assertEquals(
            1,
            provider.insertedValues?.getAsInteger(MediaStore.Downloads.IS_PENDING),
        )
        assertEquals(1, provider.updateCalls)
        assertEquals(0, provider.deleteCalls)
        assertEquals(0, provider.updatedValues?.getAsInteger(MediaStore.Downloads.IS_PENDING))
        assertEquals(DOWNLOAD_BODY, output.toString(Charsets.UTF_8.name()))
        assertEquals(
            "Saved media-success.txt to Downloads",
            notificationFor(worker)!!.extras.getCharSequence(Notification.EXTRA_TEXT),
        )
    }

    @Test
    @Config(sdk = [35])
    fun `worker deletes pending MediaStore row after a mid stream failure`() {
        pinnedServer.createContext("/media-failure") { exchange ->
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val provider = WorkerMediaProvider()
        registerMediaProvider(provider, FailingOutputStream())
        val worker = worker("$pinnedOrigin/media-failure", "media-failure.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.retry(), result)
        assertEquals(1, provider.insertCalls)
        assertEquals(0, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
        assertNull(notificationFor(worker))
    }

    @Test
    @Config(sdk = [35])
    fun `worker treats a rejected MediaStore insert as a terminal save failure`() {
        pinnedServer.createContext("/media-rejected") { exchange ->
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val provider = WorkerMediaProvider(insertResult = null)
        registerMediaProvider(provider, ByteArrayOutputStream())
        val worker = worker("$pinnedOrigin/media-rejected", "media-rejected.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(1, provider.insertCalls)
        assertEquals(0, provider.updateCalls)
        assertEquals(0, provider.deleteCalls)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Couldn't save download",
            ShadowLog
                .getLogsForTag(PinnedOriginDownloadWorker.TAG)
                .single()
                .throwable.message,
        )
    }

    private fun worker(
        url: String,
        suggestedName: String,
        runAttemptCount: Int = 0,
        foreground: ForegroundUpdater? = null,
        mimeType: String? = "text/plain",
        id: UUID = UUID.randomUUID(),
    ): PinnedOriginDownloadWorker {
        val builder =
            TestListenableWorkerBuilder<PinnedOriginDownloadWorker>(
                context = context,
                inputData =
                    PinnedOriginDownloadWorker.inputData(
                        url,
                        pinnedOrigin,
                        USER_AGENT,
                        mimeType,
                        suggestedName,
                    ),
                runAttemptCount = runAttemptCount,
            ).setId(id)
        if (foreground != null) builder.setForegroundUpdater(foreground)
        return builder.build()
    }

    private fun recordingDownloader(calls: MutableList<EnqueueCall>): PinnedOriginDownloader =
        PinnedOriginDownloader(
            context,
            PinnedOriginWorkEnqueuer { uniqueName, policy, request ->
                calls += EnqueueCall(uniqueName, policy, request)
            },
        )

    private fun registerMediaProvider(
        provider: WorkerMediaProvider,
        output: OutputStream,
    ) {
        ShadowContentResolver.registerProviderInternal("media", provider)
        shadowOf(context.contentResolver).registerOutputStream(provider.insertedUri, output)
    }

    private fun notificationFor(worker: PinnedOriginDownloadWorker) =
        shadowNotificationManager().getNotification(
            DownloadNotificationManager.notificationTag(worker.id),
            DownloadNotificationManager.NOTIFICATION_ID,
        )

    private fun shadowNotificationManager() =
        shadowOf(
            context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
        )

    private fun stopCounts() =
        context.getSharedPreferences(
            PinnedOriginDownloadWorker.STOP_COUNT_PREFERENCES,
            Context.MODE_PRIVATE,
        )

    private fun targetFile(name: String): File {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        return File(dir, name).also {
            it.delete()
            savedFiles += it
        }
    }

    private fun server(): HttpServer =
        HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0).apply { start() }

    private fun originOf(server: HttpServer): String = "http://127.0.0.1:${server.address.port}"

    private fun HttpExchange.redirect(location: String) {
        responseHeaders.add("Location", location)
        sendResponseHeaders(302, -1)
        close()
    }

    private fun HttpExchange.respond(
        status: Int,
        body: String,
    ) {
        val bytes = body.toByteArray(StandardCharsets.UTF_8)
        sendResponseHeaders(status, bytes.size.toLong())
        responseBody.use { it.write(bytes) }
    }

    private data class EnqueueCall(
        val uniqueName: String,
        val policy: ExistingWorkPolicy,
        val request: OneTimeWorkRequest,
    )

    private companion object {
        const val MEDIA_STORE_JOURNAL = "ai.omnigent.android.download_media_store"
        const val SESSION_COOKIE = "front_door=pinned"
        const val USER_AGENT = "OmnigentTest/1.0"
        const val DOWNLOAD_BODY = "download body"
    }
}

private class RecordingForegroundUpdater(
    private val failure: Throwable? = null,
) : ForegroundUpdater {
    val workIds = mutableListOf<UUID>()

    override fun setForegroundAsync(
        context: Context,
        id: UUID,
        foregroundInfo: ForegroundInfo,
    ): ListenableFuture<Void> =
        ResolvableFuture.create<Void>().apply {
            workIds += id
            if (failure == null) set(null) else setException(failure)
        }
}

private class WorkerMediaProvider(
    private val insertResult: Uri? = Uri.parse("content://media/external/downloads/73"),
) : ContentProvider() {
    val insertedUri: Uri = Uri.parse("content://media/external/downloads/73")
    var insertedValues: ContentValues? = null
    var updatedValues: ContentValues? = null
    var insertCalls = 0
    var updateCalls = 0
    var deleteCalls = 0

    override fun onCreate(): Boolean = true

    override fun insert(
        uri: Uri,
        values: ContentValues?,
    ): Uri? {
        insertCalls++
        insertedValues = values?.let { ContentValues(it) }
        return insertResult
    }

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        updateCalls++
        updatedValues = values?.let { ContentValues(it) }
        return 1
    }

    override fun delete(
        uri: Uri,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        deleteCalls++
        return 1
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor = MatrixCursor(projection ?: emptyArray())

    override fun getType(uri: Uri): String? = null
}

private class FailingOutputStream : OutputStream() {
    override fun write(value: Int): Unit = throw IOException("write failed")

    override fun write(
        bytes: ByteArray,
        offset: Int,
        length: Int,
    ) {
        if (length > 0) throw IOException("write failed after bytes arrived")
    }
}

/** Simulates the WebView provider being unavailable (e.g. mid-update). */
@Implements(CookieManager::class)
class UnavailableCookieManagerShadow {
    companion object {
        @JvmStatic
        @Implementation
        fun getInstance(): CookieManager =
            throw AndroidRuntimeException("WebView provider unavailable")
    }
}
