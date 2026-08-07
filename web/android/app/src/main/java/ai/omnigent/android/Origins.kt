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
    val host = uri.host?.lowercase() ?: return null
    // Canonicalize like a browser origin (WHATWG): lowercase scheme + host and
    // omit the default port — so an explicit `https://host:443` (or odd casing)
    // the user typed compares equal to the WebView's normalized `https://host`.
    // The pinned origin and every page URL both flow through here, so they
    // canonicalize identically.
    val port = uri.port
    val hasExplicitPort =
        port != -1 &&
            !(scheme == "https" && port == 443) &&
            !(scheme == "http" && port == 80)
    return if (hasExplicitPort) "$scheme://$host:$port" else "$scheme://$host"
}

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
 * Server domains whose IdP permits embedded user-agents. Their login redirect
 * chain runs inside the WebView and the server sets the session cookie on its
 * own domain, so no system-browser hop is needed.
 */
private val IN_WEBVIEW_AUTH_DOMAINS =
    listOf("databricks.com", "azuredatabricks.net", "databricksapps.com")

/**
 * True when [origin]'s host is, or sits under, a domain that authenticates in
 * the WebView. Matches on a dot boundary so a lookalike host like
 * `databricks.com.example.org` does not qualify.
 */
fun usesInWebViewAuth(origin: String?): Boolean {
    val host = origin?.let(Uri::parse)?.host?.lowercase() ?: return false
    return IN_WEBVIEW_AUTH_DOMAINS.any { host == it || host.endsWith(".$it") }
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
