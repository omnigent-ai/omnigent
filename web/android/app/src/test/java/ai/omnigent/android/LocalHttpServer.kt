package ai.omnigent.android

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** A real loopback HTTP server for transport tests; [handle] answers every request. */
internal class LocalHttpServer(
    private val handle: (Request) -> Response,
) : AutoCloseable {
    data class Request(
        val method: String,
        val path: String,
        val query: String?,
        val headers: Map<String, String>,
        val body: String,
    )

    data class Response(
        val status: Int,
        val body: String = "",
        val headers: Map<String, String> = emptyMap(),
        /** Sends the body one byte at a time, this far apart, as a slow server would. */
        val trickleMillis: Long = 0,
        /** Waits this long before the headers, sends one body byte, then hangs until closed. */
        val stallAfterMillis: Long? = null,
    )

    private val closed = CountDownLatch(1)

    private val server =
        HttpServer.create(InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0).apply {
            createContext("/") { exchange -> respond(exchange) }
            start()
        }

    val requests: MutableList<Request> = Collections.synchronizedList(mutableListOf())

    val origin: String get() = "http://127.0.0.1:${server.address.port}"

    private fun respond(exchange: HttpExchange) {
        val request =
            Request(
                method = exchange.requestMethod,
                path = exchange.requestURI.rawPath,
                query = exchange.requestURI.rawQuery,
                headers =
                    exchange.requestHeaders.entries.associate { (name, values) ->
                        name.lowercase() to values.joinToString(",")
                    },
                body = exchange.requestBody.readBytes().toString(Charsets.UTF_8),
            )
        requests += request
        val response = handle(request)
        response.headers.forEach { (name, value) -> exchange.responseHeaders.add(name, value) }
        val bytes = response.body.toByteArray()
        if (response.stallAfterMillis != null) {
            Thread.sleep(response.stallAfterMillis)
            exchange.sendResponseHeaders(response.status, 0)
            runCatching {
                exchange.responseBody.write(bytes.take(1).toByteArray())
                exchange.responseBody.flush()
                closed.await(10, TimeUnit.SECONDS)
            }
        } else if (response.trickleMillis > 0) {
            exchange.sendResponseHeaders(response.status, 0)
            // The client may give up mid-body; that ends the trickle.
            runCatching {
                exchange.responseBody.use { out ->
                    bytes.forEach { byte ->
                        out.write(byte.toInt())
                        out.flush()
                        Thread.sleep(response.trickleMillis)
                    }
                }
            }
        } else {
            exchange.sendResponseHeaders(
                response.status,
                if (bytes.isEmpty()) -1 else bytes.size.toLong(),
            )
            if (bytes.isNotEmpty()) exchange.responseBody.use { it.write(bytes) }
        }
        exchange.close()
    }

    override fun close() {
        closed.countDown()
        server.stop(0)
    }

    companion object {
        /** Calls [block] with the origin of a server that drops every connection unanswered. */
        fun <T> withDroppingServer(block: (origin: String) -> T): T {
            val socket = ServerSocket(0, 50, InetAddress.getLoopbackAddress())
            val dropper =
                Thread {
                    while (!socket.isClosed) runCatching { socket.accept().close() }
                }.apply {
                    isDaemon = true
                    start()
                }
            try {
                return block("http://127.0.0.1:${socket.localPort}")
            } finally {
                socket.close()
                dropper.join(1_000)
            }
        }
    }
}
