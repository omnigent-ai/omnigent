package ai.omnigent.android

import java.net.URI
import java.net.URLDecoder

/**
 * A parsed `omnigent://<host>/c/<conversation id>` link, the same link the iOS and desktop shells
 * open. The link names a server by host (and port, when not the default) and carries no `http` or
 * `https`: [parse] infers it, `http` for this device and `https` for everything else.
 *
 * `omnigent` is a custom scheme, which Android lets any installed app claim, so another app can
 * receive a link meant for this one and learn its server host and conversation id. The id is not
 * a secret: the server still decides who may read the conversation.
 */
data class DeepLink(
    /** The server's origin, as [originOf] writes it. */
    val origin: String,
    /** The route to open under the server's mount: `/c/<id>`, with any file-view parameters. */
    val path: String,
) {
    companion object {
        private val LOCAL_HOSTS = setOf("localhost", "127.0.0.1", "[::1]")

        /** Parameters of the conversation route that a link may carry: which file to show, and how. */
        private val FILE_VIEW_PARAMETERS = setOf("file", "fullscreen")

        /** [raw] as a link, or null when it is not one this app opens. */
        fun parse(raw: String?): DeepLink? {
            val uri = raw?.let { runCatching { URI(it) }.getOrNull() } ?: return null
            if (!uri.scheme.equals("omnigent", ignoreCase = true)) return null
            // A link names a server only: credentials in it would be sent to that server.
            if (uri.rawUserInfo != null) return null
            val host = uri.host?.lowercase()?.takeIf(String::isNotEmpty) ?: return null
            val id = conversationId(uri.rawPath.orEmpty()) ?: return null
            val scheme = if (host in LOCAL_HOSTS) "http" else "https"
            val port = if (uri.port == -1) "" else ":${uri.port}"
            val origin = originOf("$scheme://$host$port") ?: return null
            val query = fileViewQuery(uri.rawQuery)
            return DeepLink(origin, if (query.isEmpty()) "/c/$id" else "/c/$id?$query")
        }

        /**
         * The id in `/c/<id>`, or null when the path is anything else. An id with an escape or a
         * dot is refused: the page's router would read `%2F`, `%3F`, `%23` or `..` as a longer
         * path, a query, a fragment or a step up, and open something other than a conversation.
         */
        private fun conversationId(rawPath: String): String? {
            if (!rawPath.startsWith("/c/")) return null
            val id = rawPath.removePrefix("/c/").removeSuffix("/")
            return id.takeIf { it.isNotEmpty() && it.none { char -> char in "/.%" } }
        }

        /** The file-view parameters of [rawQuery], values left exactly as the link encoded them. */
        private fun fileViewQuery(rawQuery: String?): String =
            rawQuery
                .orEmpty()
                .split("&")
                .filter { item ->
                    val name = item.substringBefore("=")
                    runCatching { URLDecoder.decode(name, "UTF-8") }.getOrNull() in
                        FILE_VIEW_PARAMETERS
                }.joinToString("&")
    }
}
