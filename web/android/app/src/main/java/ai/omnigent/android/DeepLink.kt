package ai.omnigent.android

import java.net.URI

/** An `omnigent://<host[:port]>/c/<id>` Share QR link, parsed like the iOS and desktop apps. */
data class DeepLink(
    /** The inferred server origin, e.g. `https://omnigent.example.com`. */
    val origin: String,
    /** The mount-free SPA route, e.g. `/c/<id>`. */
    val path: String,
) {
    /** The `host[:port]` shown when asking to connect to an unknown server. */
    val hostLabel: String
        get() = origin.substringAfter("://")

    companion object {
        const val SCHEME = "omnigent"

        private const val CONVERSATION_PREFIX = "/c/"
        private val LOOPBACK_HOSTS = setOf("localhost", "127.0.0.1", "[::1]")

        // Decoded id characters that would smuggle a query, fragment, path, traversal or escape.
        private val BLOCKED_ID_CHARACTERS = setOf('?', '#', '/', '\\', '.', '%')

        /** The link's server and conversation, or null for anything else. */
        fun parse(link: String?): DeepLink? {
            val uri = link?.let { runCatching { URI(it) }.getOrNull() } ?: return null
            if (!SCHEME.equals(uri.scheme, ignoreCase = true)) return null
            val host = uri.host?.lowercase()?.takeIf(String::isNotEmpty) ?: return null
            val path = uri.path.orEmpty()
            if (!path.startsWith(CONVERSATION_PREFIX)) return null
            val id = path.removePrefix(CONVERSATION_PREFIX).removeSuffix("/")
            if (id.isEmpty() || id.any(::isBlockedIdCharacter)) return null
            val port = uri.port
            if (port != -1 && port !in 1..65535) return null
            val scheme = if (host in LOOPBACK_HOSTS) "http" else "https"
            val defaultPort = if (scheme == "http") 80 else 443
            val authority = if (port == -1 || port == defaultPort) host else "$host:$port"
            return DeepLink("$scheme://$authority", CONVERSATION_PREFIX + id)
        }

        private fun isBlockedIdCharacter(character: Char): Boolean =
            character in BLOCKED_ID_CHARACTERS || character.code <= 0x1F || character.code == 0x7F
    }
}
