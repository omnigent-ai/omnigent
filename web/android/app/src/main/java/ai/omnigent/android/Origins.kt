package ai.omnigent.android

import android.net.Uri

/**
 * Normalizes a URL to its origin (`scheme://host[:port]`), the unit of trust
 * the bridge and navigation gating compare on. Returns null for anything
 * without both a scheme and host.
 */
fun originOf(url: String?): String? {
    val uri = url?.let(Uri::parse) ?: return null
    val scheme = uri.scheme?.lowercase() ?: return null
    val host = (uri.host ?: "").lowercase().ifBlank { return null }
    return canonicalOrigin(scheme, host, uri.port)
}

/** Build the canonical browser-style origin for already-validated components. */
fun canonicalOrigin(
    scheme: String,
    host: String,
    port: Int = -1,
): String {
    val normalizedScheme = scheme.lowercase()
    val hostPart = bracketIfIpv6(host.lowercase().removeSurrounding("[", "]"))
    return if (port != -1 && !isDefaultPort(normalizedScheme, port)) {
        "$normalizedScheme://$hostPart:$port"
    } else {
        "$normalizedScheme://$hostPart"
    }
}

/** Whether [port] is implicit for an HTTP(S) [scheme]. */
fun isDefaultPort(
    scheme: String?,
    port: Int,
): Boolean =
    (scheme.equals("https", ignoreCase = true) && port == 443) ||
        (scheme.equals("http", ignoreCase = true) && port == 80)

/**
 * Re-wrap a bare IPv6 literal in the brackets a URL authority requires —
 * without them a rebuilt origin is unparseable ("https://::1:8000"). Callers
 * strip any existing brackets first: `Uri.getHost` usually strips them but has
 * been observed returning them intact (Robolectric SDK 35), so neither shape
 * can be assumed.
 */
fun bracketIfIpv6(host: String): String = if (":" in host) "[$host]" else host

/**
 * True for the only two schemes the WebView loads inline (http/https). This
 * gates a security boundary (which navigations load in the bridged WebView vs.
 * trigger login / hand off to the system), so it lowercases internally rather
 * than trust callers to pre-normalize — `"HTTPS"` counts. Everything else
 * (mailto:, intent:, about:, chrome-error://, null) is handed off or ignored.
 */
fun isHttpScheme(scheme: String?): Boolean {
    val normalized = scheme?.lowercase() ?: return false
    return normalized == "http" || normalized == "https"
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
    if (!isHttpScheme(scheme)) return null
    if (uri.host.isNullOrBlank()) return null
    return withScheme.trimEnd('/')
}
