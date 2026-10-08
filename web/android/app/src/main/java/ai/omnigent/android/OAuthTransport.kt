package ai.omnigent.android

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URI
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

internal data class OAuthHttpRequest(
    val uri: URI,
    val method: String = "GET",
    val headers: Map<String, String> = emptyMap(),
    val body: ByteArray? = null,
)

internal data class OAuthHttpResponse(
    val uri: URI,
    val status: Int,
    val body: ByteArray,
    val contentType: String? = null,
)

internal fun interface OAuthTransport {
    fun execute(request: OAuthHttpRequest): OAuthHttpResponse
}

/** The request never got an HTTP response; callers report it as their own network error. */
internal class OAuthNetworkException(
    cause: Throwable? = null,
) : IOException(cause)

/**
 * Redirect-disabled, cookie-independent native OAuth transport. [timeoutMs] bounds the whole
 * exchange, and a response body larger than [maxBodyBytes] fails it.
 */
internal class UrlConnectionOAuthTransport(
    private val timeoutMs: Int = DEFAULT_TIMEOUT_MS,
    private val maxBodyBytes: Int = DEFAULT_MAX_BODY_BYTES,
) : OAuthTransport {
    override fun execute(request: OAuthHttpRequest): OAuthHttpResponse {
        val connection =
            try {
                request.uri.toURL().openConnection() as HttpURLConnection
            } catch (error: Exception) {
                throw OAuthNetworkException(error)
            }
        connection.instanceFollowRedirects = false
        connection.useCaches = false
        connection.connectTimeout = timeoutMs
        connection.readTimeout = timeoutMs
        // Socket timeouts bound each wait, not the exchange, and a disconnect can wait behind a
        // blocked read. So the exchange runs on its own thread and the caller waits only until
        // the deadline; the abandoned thread ends at its socket timeout.
        val deadlineAt = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(timeoutMs.toLong())
        val exchange = EXCHANGES.submit(Callable { exchange(connection, request, deadlineAt) })
        return try {
            exchange.get(timeoutMs.toLong(), TimeUnit.MILLISECONDS)
        } catch (error: ExecutionException) {
            throw error.cause as? OAuthNetworkException ?: OAuthNetworkException(error.cause)
        } catch (error: Exception) {
            if (error is InterruptedException) Thread.currentThread().interrupt()
            exchange.cancel(true)
            EXCHANGES.execute(connection::disconnect)
            throw OAuthNetworkException(error)
        }
    }

    private fun exchange(
        connection: HttpURLConnection,
        request: OAuthHttpRequest,
        deadlineAt: Long,
    ): OAuthHttpResponse =
        try {
            connection.requestMethod = request.method
            request.headers.forEach(connection::setRequestProperty)
            request.body?.let { body ->
                connection.doOutput = true
                connection.setFixedLengthStreamingMode(body.size)
                connection.outputStream.use { it.write(body) }
            }
            val status = connection.responseCode
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            OAuthHttpResponse(
                uri = connection.url.toURI(),
                status = status,
                body = stream?.use { readBounded(it, deadlineAt) } ?: byteArrayOf(),
                contentType = connection.contentType,
            )
        } catch (error: Exception) {
            throw OAuthNetworkException(error)
        } finally {
            connection.disconnect()
        }

    private fun readBounded(
        stream: InputStream,
        deadlineAt: Long,
    ): ByteArray {
        val body = ByteArrayOutputStream()
        val buffer = ByteArray(8 * 1024)
        while (true) {
            if (System.nanoTime() > deadlineAt) throw IOException("response exceeded its deadline")
            val read = stream.read(buffer)
            if (read < 0) return body.toByteArray()
            if (body.size() + read > maxBodyBytes) throw IOException("response body too large")
            body.write(buffer, 0, read)
        }
    }

    private companion object {
        const val DEFAULT_TIMEOUT_MS = 30_000
        const val DEFAULT_MAX_BODY_BYTES = 1024 * 1024

        /** Runs exchanges and their disconnects. Daemon threads, so they never keep the app alive. */
        val EXCHANGES: ExecutorService =
            Executors.newCachedThreadPool { task ->
                Thread(task, "oauth-request").apply { isDaemon = true }
            }
    }
}
