package ai.omnigent.android

import android.net.Uri
import java.net.IDN

/**
 * A parsed `omnigent://<host>[:port]/c/<id>` deep link (see
 * docs/android-deep-link-design.md). Stricter than iOS/desktop: userinfo,
 * query, or fragment on the link is rejected outright, not silently stripped.
 */
data class DeepLink(
    /** `originOf`-canonical http(s) origin, no trailing slash. */
    val origin: String,
    /** Basename-less SPA conversation path, always `/c/<id>`. */
    val path: String,
) {
    companion object {
        private const val MAX_LINK_LENGTH = 2048

        // Hosts that resolve to the local machine — http (local dev is plain
        // http); everything else https. Mirrors iOS/desktop `localHosts`.
        private val LOCAL_HOSTS = setOf("localhost", "127.0.0.1", "::1")

        // Denylist, not a grammar: characters that smuggle URL structure,
        // enable traversal, or signal a malformed escape. The SPA's /c/:id
        // route stays the authority on what a valid id IS.
        private val BLOCKED_ID_CHARS = setOf('?', '#', '/', '.', '%')

        /** Parse an `omnigent://` URI; null for anything off-contract. */
        fun parse(raw: Uri): DeepLink? {
            if (raw.toString().length > MAX_LINK_LENGTH) return null
            if (!raw.isHierarchical) return null
            if (raw.scheme?.lowercase() != "omnigent") return null
            // Stricter than iOS/desktop (which drop these silently): our own
            // link emitters never produce them, so their presence is off-contract.
            if (raw.encodedUserInfo != null) return null
            if (raw.encodedQuery != null || raw.encodedFragment != null) return null

            val rawHostRaw = raw.host?.takeIf { it.isNotEmpty() } ?: return null
            val authority = raw.encodedAuthority ?: ""
            val isBracketedHost = authority.startsWith("[")
            // Only a bracketed authority (`[::1]`) legitimizes a colon in the
            // host: for anything else, Uri.getHost() can fold an extra colon
            // from a malformed multi-colon authority (e.g. "h:8000:9000")
            // straight into the host string ("h:8000") instead of rejecting it.
            val rawHost =
                if (isBracketedHost) {
                    rawHostRaw.removeSurrounding(
                        "[",
                        "]",
                    )
                } else {
                    rawHostRaw
                }
            if (!isBracketedHost && ":" in rawHost) return null

            // Uri.getPort() returns -1 for both "no port" and a malformed one
            // (":99999", ":abc"), silently treating invalid as absent. Re-derive
            // the port spec from the authority text to tell them apart.
            val afterHost =
                if (isBracketedHost) {
                    authority.substringAfter(']', "")
                } else {
                    val idx = authority.lastIndexOf(':')
                    if (idx == -1) "" else authority.substring(idx)
                }
            val port =
                when {
                    afterHost.isEmpty() -> {
                        -1
                    }

                    !afterHost.startsWith(":") -> {
                        return null
                    }

                    else -> {
                        afterHost.substring(1).let { spec ->
                            if (spec.isEmpty() || !spec.all(Char::isDigit)) return null
                            spec.toIntOrNull() ?: return null
                        }
                    }
                }
            if (port != -1 && port !in 1..65535) return null

            // Uri.getPath is percent-DECODED, so encoded separators (%3F, %23,
            // %2F, %2E, %00) reappear as literals and hit the denylist below;
            // a malformed escape (%zz) leaves a stray literal '%'.
            val path = raw.path ?: return null
            if (!path.startsWith("/c/")) return null
            var id = path.substring(3)
            if (id.endsWith("/")) id = id.dropLast(1)
            if (id.isEmpty()) return null
            if (id.any { it.code <= 0x1F || it.code == 0x7F || it in BLOCKED_ID_CHARS }) {
                return null
            }

            val host = canonicalHost(rawHost) ?: return null
            val scheme = if (host in LOCAL_HOSTS) "http" else "https"
            val origin = canonicalOrigin(scheme, host, port)
            return DeepLink(origin, "/c/$id")
        }

        /**
         * IDNA-normalize to a lowercase ASCII host, or null if impossible.
         * Caller has already stripped brackets and vetted any ':' as
         * legitimate IPv6, not a malformed authority.
         */
        private fun canonicalHost(host: String): String? =
            try {
                IDN.toASCII(host.lowercase()).takeIf { it.isNotEmpty() }
            } catch (_: IllegalArgumentException) {
                null
            }
    }
}
