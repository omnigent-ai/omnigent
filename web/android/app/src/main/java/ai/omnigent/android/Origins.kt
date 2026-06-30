package ai.omnigent.android

import android.net.Uri

/**
 * Normalizes a URL to its origin (`scheme://host[:port]`), the unit of trust
 * the bridge and navigation gating compare on. Returns null for anything
 * without both a scheme and host.
 */
fun originOf(url: String?): String? {
    val uri = url?.let(Uri::parse) ?: return null
    val scheme = uri.scheme ?: return null
    val host = uri.host ?: return null
    return if (uri.port == -1) "$scheme://$host" else "$scheme://$host:${uri.port}"
}

/**
 * Normalize user-entered server text into a loadable URL, or null if it isn't a
 * usable http(s) address. Adds a default `https://` scheme when omitted and
 * trims a trailing slash.
 */
fun normalizeServerUrl(input: String): String? {
    val trimmed = input.trim().ifBlank { return null }
    // No internal whitespace — a stray newline would otherwise split the
    // newline-delimited recents store into bogus entries.
    if (trimmed.any { it.isWhitespace() }) return null
    val withScheme = if (trimmed.contains("://")) trimmed else "https://$trimmed"
    val uri = Uri.parse(withScheme)
    val scheme = uri.scheme?.lowercase() ?: return null
    if (scheme != "http" && scheme != "https") return null
    if (uri.host.isNullOrBlank()) return null
    return withScheme.trimEnd('/')
}
