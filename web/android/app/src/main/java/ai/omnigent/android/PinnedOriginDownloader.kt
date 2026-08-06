package ai.omnigent.android

import android.content.Context
import android.os.SystemClock
import android.util.Log
import android.webkit.CookieManager
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter
import java.time.format.DateTimeParseException
import java.util.UUID
import java.util.concurrent.ExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

internal interface PinnedOriginDownloadHandler {
    fun download(
        url: String,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    )

    fun shutdown()
}

internal fun interface PinnedOriginWorkEnqueuer {
    fun enqueue(
        uniqueName: String,
        policy: ExistingWorkPolicy,
        request: OneTimeWorkRequest,
    )
}

/** Schedules durable pinned-origin downloads without persisting session credentials. */
internal class PinnedOriginDownloader(
    context: Context,
    private val workEnqueuer: PinnedOriginWorkEnqueuer = defaultWorkEnqueuer(context),
) : PinnedOriginDownloadHandler {
    private val context = context.applicationContext ?: context
    private val notifications = DownloadNotificationManager(this.context)
    private val shutDown = AtomicBoolean()

    override fun download(
        url: String,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ) {
        if (originOf(url) != pinnedOrigin) return
        if (shutDown.get()) {
            Log.w(TAG, "Dropping download because the worker is shut down")
            return
        }

        val request =
            try {
                PinnedOriginDownloadWorker.request(
                    url,
                    pinnedOrigin,
                    userAgent,
                    mimeType,
                    suggestedName,
                )
            } catch (failure: RuntimeException) {
                reportEnqueueFailure(url, pinnedOrigin, suggestedName, failure)
                return
            }
        // KEEP lets a pending or running transfer finish instead of restarting it
        // when the user taps the same download again.
        try {
            workEnqueuer.enqueue(
                uniqueWorkName(url, pinnedOrigin, suggestedName),
                ExistingWorkPolicy.KEEP,
                request,
            )
            notifications.queued(safeFileName(suggestedName))
        } catch (failure: RuntimeException) {
            reportEnqueueFailure(url, pinnedOrigin, suggestedName, failure)
        }
    }

    /** Stops this Activity-owned scheduler; already-enqueued work remains durable. */
    override fun shutdown() {
        shutDown.set(true)
    }

    private fun reportEnqueueFailure(
        url: String,
        pinnedOrigin: String,
        suggestedName: String,
        failure: RuntimeException,
    ) {
        Log.e(TAG, "Couldn't enqueue download for $suggestedName", failure)
        val failureId =
            UUID.nameUUIDFromBytes(
                downloadIdentity(url, pinnedOrigin, suggestedName).toByteArray(),
            )
        notifications.failed(safeFileName(suggestedName), failureId)
    }

    private companion object {
        const val TAG = PinnedOriginDownloadWorker.TAG
        const val UNIQUE_WORK_PREFIX = "pinned-origin-download:"

        fun defaultWorkEnqueuer(context: Context): PinnedOriginWorkEnqueuer {
            val applicationContext = context.applicationContext ?: context
            return PinnedOriginWorkEnqueuer { uniqueName, policy, request ->
                WorkManager
                    .getInstance(applicationContext)
                    .enqueueUniqueWork(uniqueName, policy, request)
            }
        }

        /** One download's identity: the tuple behind both its work name and failure id. */
        fun downloadIdentity(
            url: String,
            pinnedOrigin: String,
            suggestedName: String,
        ): String = "$pinnedOrigin\u0000$url\u0000$suggestedName"

        fun uniqueWorkName(
            url: String,
            pinnedOrigin: String,
            suggestedName: String,
        ): String =
            UNIQUE_WORK_PREFIX + sha256Hex(downloadIdentity(url, pinnedOrigin, suggestedName))
    }
}

/** Streams a pinned-origin download while keeping WebView cookies on their trusted origin. */
internal class PinnedOriginDownloadWorker(
    context: Context,
    workerParameters: WorkerParameters,
) : Worker(context, workerParameters) {
    private val storage = DownloadStorage(applicationContext)
    private val notifications = DownloadNotificationManager(applicationContext)
    private val safeSuggestedName by lazy {
        safeFileName(inputData.getString(KEY_SUGGESTED_NAME) ?: FALLBACK_NAME)
    }

    @Volatile
    private var activeConnection: HttpURLConnection? = null

    override fun doWork(): Result {
        val suggestedName = safeSuggestedName
        // Every run start increments runAttemptCount — retries, stops, and crash
        // recoveries alike — so this gate bounds restart loops the stop counter
        // cannot see, like repeated process deaths mid-transfer.
        if (runAttemptCount >= MAX_LIVES) {
            return terminalFailure(
                suggestedName,
                TerminalDownloadException("Download restarted too many times"),
            )
        }
        val stopCount = stopCount()
        if (stopCount >= MAX_STOPS) {
            return terminalFailure(
                suggestedName,
                TerminalDownloadException("Download stopped too many times"),
            )
        }
        val foregroundEstablished = establishForeground()
        if (isStopped) return stoppedFailure()
        if (!foregroundEstablished && stopCount > 0) {
            return terminalFailure(
                suggestedName,
                TerminalDownloadException("Foreground execution unavailable after a stop"),
            )
        }
        val input = readInput()
        if (input == null) {
            return terminalFailure(
                suggestedName,
                TerminalDownloadException("Missing download input"),
            )
        }

        val initialUrl =
            try {
                URL(input.url)
            } catch (failure: MalformedURLException) {
                return terminalFailure(
                    input.suggestedName,
                    TerminalDownloadException("Invalid download URL", failure),
                )
            }
        if (!isHttpScheme(initialUrl.protocol) ||
            !hasPinnedOrigin(initialUrl, input.pinnedOrigin)
        ) {
            return terminalFailure(
                input.suggestedName,
                TerminalDownloadException("Rejected download origin"),
            )
        }

        var usedRetryAfter = false
        while (true) {
            try {
                throwIfStopped()
                val saved =
                    downloadFollowingRedirects(
                        initialUrl,
                        input.pinnedOrigin,
                        input.userAgent,
                        input.mimeType,
                        input.suggestedName,
                    )
                throwIfStopped()
                if (saved.saved) {
                    clearStopCount()
                    notifications.succeeded(saved.name, id)
                    return Result.success()
                }
                return terminalFailure(
                    saved.name,
                    TerminalDownloadException("Couldn't save download"),
                )
            } catch (failure: RetryAfterDownloadException) {
                if (!usedRetryAfter && runAttemptCount < MAX_ATTEMPTS - 1) {
                    usedRetryAfter = true
                    if (!waitForRetryAfter(failure.delayMillis)) return stoppedFailure()
                    continue
                }
                return transientFailure(input.suggestedName, failure)
            } catch (_: StoppedDownloadException) {
                return stoppedFailure()
            } catch (failure: TransientDownloadException) {
                return transientFailure(input.suggestedName, failure)
            } catch (failure: IOException) {
                if (isStopped) return stoppedFailure()
                return transientFailure(input.suggestedName, failure)
            } catch (failure: AuthenticationRequiredException) {
                return terminalFailure(input.suggestedName, failure)
            } catch (failure: TerminalDownloadException) {
                return terminalFailure(input.suggestedName, failure)
            } catch (failure: Throwable) {
                if (isStopped) return stoppedFailure()
                return terminalFailure(input.suggestedName, failure)
            }
        }
    }

    override fun getForegroundInfo(): ForegroundInfo =
        notifications.foregroundInfo(
            safeSuggestedName,
            id,
        )

    override fun onStopped() {
        incrementStopCount()
        activeConnection?.disconnect()
        super.onStopped()
    }

    private fun establishForeground(): Boolean =
        try {
            setForegroundAsync(getForegroundInfo()).get()
            true
        } catch (failure: InterruptedException) {
            Thread.currentThread().interrupt()
            Log.w(TAG, "Foreground download setup was interrupted", failure)
            false
        } catch (failure: ExecutionException) {
            Log.w(TAG, "Foreground download notification is unavailable", failure.cause)
            false
        } catch (failure: RuntimeException) {
            // Notification permission or foreground-service state can change at runtime.
            Log.w(TAG, "Foreground download notification is unavailable", failure)
            false
        }

    private fun readInput(): DownloadInput? {
        val url = inputData.getString(KEY_URL) ?: return null
        val pinnedOrigin = inputData.getString(KEY_PINNED_ORIGIN) ?: return null
        val userAgent = inputData.getString(KEY_USER_AGENT) ?: return null
        // The name is required input, but only ever used sanitized.
        if (inputData.getString(KEY_SUGGESTED_NAME) == null) return null
        return DownloadInput(
            url,
            pinnedOrigin,
            userAgent,
            inputData.getString(KEY_MIME_TYPE),
            safeSuggestedName,
        )
    }

    private fun transientFailure(
        suggestedName: String,
        failure: Throwable,
    ): Result {
        Log.w(TAG, "Transient download failure for $suggestedName", failure)
        if (runAttemptCount < MAX_ATTEMPTS - 1) {
            // Retries restart from byte zero (no partial-file resumption). Stopped
            // runs also advanced runAttemptCount, so a much-stopped download
            // exhausts this budget early — deliberately tight, never unbounded.
            return Result.retry()
        }
        return terminalFailure(suggestedName, failure)
    }

    private fun terminalFailure(
        suggestedName: String,
        failure: Throwable,
    ): Result {
        Log.e(TAG, "Download failed for $suggestedName", failure)
        clearStopCount()
        if (failure is AuthenticationRequiredException) {
            notifications.authenticationRequired(suggestedName, id)
        } else {
            notifications.failed(suggestedName, id)
        }
        return Result.failure()
    }

    private fun stoppedFailure(): Result {
        Log.w(TAG, "Download stopped; retrying from byte zero")
        return Result.retry()
    }

    private fun stopCount(): Int =
        synchronized(STOP_COUNT_LOCK) {
            applicationContext
                .getSharedPreferences(STOP_COUNT_PREFERENCES, Context.MODE_PRIVATE)
                .getInt(id.toString(), 0)
        }

    private fun incrementStopCount() {
        synchronized(STOP_COUNT_LOCK) {
            val preferences =
                applicationContext.getSharedPreferences(
                    STOP_COUNT_PREFERENCES,
                    Context.MODE_PRIVATE,
                )
            preferences
                .edit()
                .putInt(id.toString(), preferences.getInt(id.toString(), 0) + 1)
                .commit()
        }
    }

    private fun clearStopCount() {
        synchronized(STOP_COUNT_LOCK) {
            applicationContext
                .getSharedPreferences(STOP_COUNT_PREFERENCES, Context.MODE_PRIVATE)
                .edit()
                .remove(id.toString())
                .commit()
        }
    }

    private fun throwIfStopped() {
        if (isStopped) throw StoppedDownloadException()
    }

    private fun waitForRetryAfter(delayMillis: Long): Boolean {
        val deadline = SystemClock.elapsedRealtime() + delayMillis
        while (true) {
            if (isStopped) return false
            val remaining = deadline - SystemClock.elapsedRealtime()
            if (remaining <= 0) return true
            try {
                Thread.sleep(minOf(remaining, STOP_POLL_INTERVAL_MS))
            } catch (_: InterruptedException) {
                Thread.currentThread().interrupt()
                return false
            }
        }
    }

    /**
     * Fetch the live WebView cookie for one request URL. Queried per hop so
     * Path-scoped cookies match each hop's own path — never fetched at enqueue
     * time, which keeps session credentials out of WorkManager's on-disk
     * database. The WebView provider itself can be unavailable (mid-update) —
     * fail like any transient error so the user still gets a notification.
     */
    private fun cookieFor(url: URL): String? =
        try {
            CookieManager.getInstance().getCookie(url.toString())
        } catch (failure: Throwable) {
            throw TransientDownloadException("WebView cookie store unavailable", failure)
        }

    private fun downloadFollowingRedirects(
        initialUrl: URL,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ): DownloadSaveResult {
        var currentUrl = initialUrl
        var cookieAllowed = true
        var redirectCount = 0

        while (true) {
            throwIfStopped()
            val connection = currentUrl.openConnection() as HttpURLConnection
            activeConnection = connection
            try {
                throwIfStopped()
                connection.instanceFollowRedirects = false
                connection.connectTimeout = CONNECT_TIMEOUT_MS
                connection.readTimeout = READ_TIMEOUT_MS
                connection.requestMethod = "GET"
                if (userAgent.isNotBlank()) connection.setRequestProperty("User-Agent", userAgent)
                if (cookieAllowed) {
                    val cookie = cookieFor(currentUrl)
                    if (!cookie.isNullOrBlank()) {
                        connection.setRequestProperty("Cookie", cookie)
                    }
                }

                val status = connection.responseCode
                throwIfStopped()
                if (status in REDIRECT_STATUS_CODES) {
                    if (redirectCount >= MAX_REDIRECTS) {
                        throw TerminalDownloadException("Too many redirects")
                    }
                    val location =
                        connection
                            .getHeaderField("Location")
                            ?.takeIf(String::isNotBlank)
                            ?: throw TerminalDownloadException("Redirect missing Location")
                    val nextUrl =
                        try {
                            URL(currentUrl, location)
                        } catch (failure: MalformedURLException) {
                            throw TerminalDownloadException("Invalid redirect Location", failure)
                        }
                    if (!isHttpScheme(nextUrl.protocol)) {
                        throw TerminalDownloadException("Unsupported redirect scheme")
                    }
                    if (isProxyAuthUrl(nextUrl.toString(), pinnedOrigin)) {
                        throw AuthenticationRequiredException()
                    }
                    cookieAllowed = cookieAllowed && hasPinnedOrigin(nextUrl, pinnedOrigin)
                    currentUrl = nextUrl
                    redirectCount++
                    throwIfStopped()
                    continue
                }
                if (status == 408 || status == 429 || status in 500..599) {
                    val retryAfterMillis =
                        if (status == 429 || status == 503) {
                            retryAfterMillis(connection.getHeaderField("Retry-After"))
                        } else {
                            null
                        }
                    if (retryAfterMillis != null) {
                        throw RetryAfterDownloadException(status, retryAfterMillis)
                    }
                    throw TransientDownloadException("Download failed with HTTP $status")
                }
                if (status !in 200..299) {
                    throw TerminalDownloadException("Download failed with HTTP $status")
                }

                // A 200 HTML response where something else was expected is a
                // front-door login page, not the file. When no MIME type was
                // requested (common from onDownloadStart), infer intent from
                // the file name so a login page can't be saved as `report.pdf`.
                val requestedMimeType = normalizedMimeType(mimeType)
                val responseMimeType = normalizedMimeType(connection.contentType)
                val expectsHtml =
                    requestedMimeType?.let(::isHtmlMimeType)
                        ?: hasHtmlExtension(suggestedName)
                if (!expectsHtml && responseMimeType?.let(::isHtmlMimeType) == true) {
                    throw AuthenticationRequiredException()
                }
                val resolvedMimeType =
                    mimeType?.takeIf(String::isNotBlank)
                        ?: responseMimeType
                        ?: DEFAULT_MIME_TYPE
                return connection.inputStream.use { input ->
                    var streamFailure: Throwable? = null
                    val result =
                        storage.save(
                            suggestedName,
                            resolvedMimeType,
                            operationId = id.toString(),
                            shouldAbort = { isStopped },
                        ) { output ->
                            try {
                                copyUntilStopped(input, output)
                            } catch (failure: Throwable) {
                                streamFailure = failure
                                throw failure
                            }
                        }
                    streamFailure?.let { throw it }
                    result
                }
            } finally {
                if (activeConnection === connection) activeConnection = null
                connection.disconnect()
            }
        }
    }

    private fun copyUntilStopped(
        input: java.io.InputStream,
        output: java.io.OutputStream,
    ) {
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        while (true) {
            throwIfStopped()
            val read = input.read(buffer)
            if (read == -1) break
            throwIfStopped()
            output.write(buffer, 0, read)
        }
        throwIfStopped()
    }

    private fun retryAfterMillis(value: String?): Long? {
        val candidate = value?.trim()?.takeIf(String::isNotEmpty) ?: return null
        val seconds = candidate.toLongOrNull()
        val delayMillis =
            if (seconds != null) {
                if (seconds < 0 || seconds > MAX_RETRY_AFTER_SECONDS) return null
                TimeUnit.SECONDS.toMillis(seconds)
            } else {
                val retryAt =
                    try {
                        ZonedDateTime.parse(candidate, DateTimeFormatter.RFC_1123_DATE_TIME)
                    } catch (_: DateTimeParseException) {
                        return null
                    }
                maxOf(0L, retryAt.toInstant().toEpochMilli() - System.currentTimeMillis())
            }
        return delayMillis.takeIf { it <= TimeUnit.SECONDS.toMillis(MAX_RETRY_AFTER_SECONDS) }
    }

    // Same canonicalization as the enqueue-side gate, so both sides agree.
    internal fun hasPinnedOrigin(
        url: URL,
        pinnedOrigin: String,
    ): Boolean = originOf(url.toString()) == pinnedOrigin

    private fun normalizedMimeType(mimeType: String?): String? =
        mimeType
            ?.substringBefore(';')
            ?.trim()
            ?.takeIf(String::isNotBlank)

    private fun isHtmlMimeType(mimeType: String): Boolean =
        mimeType.equals("text/html", ignoreCase = true) ||
            mimeType.equals("application/xhtml+xml", ignoreCase = true)

    private fun hasHtmlExtension(name: String): Boolean =
        name.substringAfterLast('.', "").lowercase() in HTML_EXTENSIONS

    private data class DownloadInput(
        val url: String,
        val pinnedOrigin: String,
        val userAgent: String,
        val mimeType: String?,
        val suggestedName: String,
    )

    private class TransientDownloadException(
        message: String,
        cause: Throwable? = null,
    ) : Exception(message, cause)

    private class RetryAfterDownloadException(
        status: Int,
        val delayMillis: Long,
    ) : Exception("Download failed with HTTP $status")

    private class StoppedDownloadException : Exception()

    private class AuthenticationRequiredException : Exception("Sign in again to download")

    private class TerminalDownloadException(
        message: String,
        cause: Throwable? = null,
    ) : Exception(message, cause)

    companion object {
        internal const val TAG = "PinnedOriginDownloader"
        internal const val KEY_URL = "url"
        internal const val KEY_PINNED_ORIGIN = "pinned_origin"
        internal const val KEY_USER_AGENT = "user_agent"
        internal const val KEY_MIME_TYPE = "mime_type"
        internal const val KEY_SUGGESTED_NAME = "suggested_name"
        internal const val MAX_ATTEMPTS = 3
        internal const val STOP_COUNT_PREFERENCES = "ai.omnigent.android.download_stop_counts"

        // Stops advance runAttemptCount too, but conflated with transient retries;
        // this counter tracks stops precisely, while MAX_LIVES backstops restart
        // paths that never run onStopped, such as process death.
        internal const val MAX_STOPS = 3
        internal const val MAX_LIVES = 8
        private const val WORK_TAG = "pinned-origin-download"
        private const val MAX_REDIRECTS = 10
        private const val CONNECT_TIMEOUT_MS = 15_000
        private const val READ_TIMEOUT_MS = 30_000
        private const val STOP_POLL_INTERVAL_MS = 250L
        private const val MAX_RETRY_AFTER_SECONDS = 15 * 60L
        private const val DEFAULT_MIME_TYPE = "application/octet-stream"
        private const val FALLBACK_NAME = "download"
        private const val BACKOFF_SECONDS = 45L
        private val REDIRECT_STATUS_CODES = setOf(301, 302, 303, 307, 308)
        private val HTML_EXTENSIONS = setOf("html", "htm", "xhtml")
        private val STOP_COUNT_LOCK = Any()

        internal fun request(
            url: String,
            pinnedOrigin: String,
            userAgent: String,
            mimeType: String?,
            suggestedName: String,
        ): OneTimeWorkRequest {
            val constraints =
                Constraints
                    .Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            return OneTimeWorkRequestBuilder<PinnedOriginDownloadWorker>()
                .setInputData(inputData(url, pinnedOrigin, userAgent, mimeType, suggestedName))
                .setConstraints(constraints)
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    BACKOFF_SECONDS,
                    TimeUnit.SECONDS,
                ).addTag(WORK_TAG)
                .build()
        }

        internal fun inputData(
            url: String,
            pinnedOrigin: String,
            userAgent: String,
            mimeType: String?,
            suggestedName: String,
        ): Data =
            workDataOf(
                KEY_URL to url,
                KEY_PINNED_ORIGIN to pinnedOrigin,
                KEY_USER_AGENT to userAgent,
                KEY_MIME_TYPE to mimeType,
                KEY_SUGGESTED_NAME to suggestedName,
            )
    }
}
