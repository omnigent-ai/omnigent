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

/** Authentication strategy for a pinned Omnigent server. */
enum class ServerAuthentication {
    /** A Databricks workspace, where Omnigent is mounted at `/omnigent`. */
    DATABRICKS_WORKSPACE,

    /** A Databricks App, which serves the app at its own root. */
    DATABRICKS_APP,

    /** Omnigent's generic OIDC flow for every other server. */
    OIDC,
    ;

    /** Only Databricks Apps retain embedded platform SSO. */
    val usesInWebViewAuth: Boolean
        get() = this == DATABRICKS_APP
}

/** Path the Omnigent SPA is mounted at inside a Databricks workspace. */
const val WORKSPACE_UI_PATH = "/omnigent"

private val WORKSPACE_DOMAINS = listOf("databricks.com", "azuredatabricks.net")
private const val DATABRICKS_APP_DOMAIN = "databricksapps.com"

/** Classify [origin] without treating lookalike suffixes as Databricks hosts. */
fun serverAuthentication(origin: String?): ServerAuthentication {
    val host = origin?.let(Uri::parse)?.host?.lowercase()
    return when {
        matchesDomain(host, WORKSPACE_DOMAINS) -> ServerAuthentication.DATABRICKS_WORKSPACE
        matchesDomain(host, listOf(DATABRICKS_APP_DOMAIN)) -> ServerAuthentication.DATABRICKS_APP
        else -> ServerAuthentication.OIDC
    }
}

/** True when [host] is, or sits under, a Databricks workspace domain. */
fun isDatabricksWorkspaceHost(host: String?): Boolean = matchesDomain(host, WORKSPACE_DOMAINS)

private fun matchesDomain(
    host: String?,
    domains: List<String>,
): Boolean {
    val normalized = host?.lowercase() ?: return false
    return domains.any { normalized == it || normalized.endsWith(".$it") }
}

/**
 * The workspace-UI URL for a bare Databricks workspace root, or null when [url]
 * is anything else — a non-workspace host, or a URL that already carries a path
 * (a deliberate deep link we must not override).
 *
 * A bare workspace root shows the Databricks landing page, not Omnigent, so the
 * shell rewrites it to [WORKSPACE_UI_PATH]. Query and fragment survive because
 * `?o=<org>` selects which workspace the request lands in.
 */
fun databricksWorkspaceUiUrl(url: String?): String? {
    val uri = url?.let(Uri::parse) ?: return null
    if (!isHttpScheme(uri.scheme)) return null
    if (!isDatabricksWorkspaceHost(uri.host)) return null
    val path = uri.path.orEmpty()
    if (path.isNotEmpty() && path != "/") return null
    val origin = originOf(url) ?: return null
    return buildString {
        append(origin).append(WORKSPACE_UI_PATH)
        uri.encodedQuery?.let { append('?').append(it) }
        uri.encodedFragment?.let { append('#').append(it) }
    }
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
