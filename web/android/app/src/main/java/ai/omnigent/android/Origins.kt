package ai.omnigent.android

import android.icu.text.IDNA
import android.net.Uri
import java.util.Locale

/**
 * Normalizes a URL to its origin (`scheme://host[:port]`), the unit of trust
 * the bridge and navigation gating compare on. Returns null for anything
 * without both a scheme and host.
 */
fun originOf(url: String?): String? {
    val uri = url?.let(Uri::parse) ?: return null
    val scheme = uri.scheme?.lowercase() ?: return null
    val host = canonicalHost(uri.host ?: return null) ?: return null
    return canonicalOrigin(scheme, host, uri.port)
}

/** IDNA-normalize a host to the lowercase ASCII form used for origin comparisons. */
fun canonicalHost(host: String): String? {
    val normalized = host.lowercase(Locale.ROOT).removeSurrounding("[", "]")
    if (":" in normalized) return normalized
    val info = IDNA.Info()
    val ascii = StringBuilder()
    UTS46.nameToASCII(normalized, ascii, info)
    return ascii.toString().takeIf { it.isNotEmpty() && !info.hasErrors() }
}

private val UTS46 = IDNA.getUTS46Instance(IDNA.NONTRANSITIONAL_TO_ASCII)

/** Build the canonical browser-style origin for already-validated components. */
fun canonicalOrigin(
    scheme: String,
    host: String,
    port: Int = -1,
): String {
    val normalizedScheme = scheme.lowercase()
    val hostPart =
        bracketIfIpv6(canonicalHost(host) ?: host.lowercase().removeSurrounding("[", "]"))
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

/** Path the Omnigent SPA is mounted at inside a Databricks workspace. */
const val WORKSPACE_UI_PATH = "/omnigent"

/**
 * Databricks domains that serve a workspace, and therefore mount the SPA at
 * [WORKSPACE_UI_PATH]. `databricksapps.com` is deliberately absent: Apps share
 * the workspace login story (see [IN_WEBVIEW_AUTH_DOMAINS]) but serve their own
 * app at the root, with no workspace mount to redirect to.
 */
private val WORKSPACE_DOMAINS = listOf("databricks.com", "azuredatabricks.net")

/** True when [host] is, or sits under, a Databricks workspace domain. */
private fun isDatabricksWorkspaceHost(host: String?): Boolean {
    val normalized = host?.lowercase() ?: return false
    return WORKSPACE_DOMAINS.any { normalized == it || normalized.endsWith(".$it") }
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
