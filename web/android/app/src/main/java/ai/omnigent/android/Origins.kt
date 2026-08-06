package ai.omnigent.android

import android.icu.text.IDNA
import android.net.Uri

// Chromium canonicalizes hosts with UTS-46 (non-transitional), so the pin must
// too: java.net.IDN is IDNA2003 and maps e.g. faß.de to fass.de — a different
// registrable domain than the xn--fa-hia.de the WebView actually loads.
private val uts46: IDNA =
    IDNA.getUTS46Instance(
        IDNA.NONTRANSITIONAL_TO_ASCII or IDNA.CHECK_BIDI or IDNA.CHECK_CONTEXTJ,
    )

// Bracketed IPv6 literals are not domain names — canonicalize them the way
// Chromium serializes them (shortest RFC 5952 form) instead of running IDNA.
private fun toAsciiHost(rawHost: String): String? {
    if (rawHost.startsWith("[")) return canonicalIpv6(rawHost)
    val info = IDNA.Info()
    val ascii = StringBuilder()
    val mapped =
        try {
            uts46.nameToASCII(rawHost, ascii, info)
            if (info.hasErrors()) null else ascii.toString()
        } catch (_: RuntimeException) {
            null
        } ?: return null
    // WHATWG: a host whose last label is a number is an IPv4 address — it must
    // parse as one (shorthand like `127.1` included) or the URL is invalid.
    return if (endsInIpv4Number(mapped)) canonicalIpv4(mapped) else mapped
}

// `[0:0:0:0:0:0:0:1]` and `[::1]` are the same address; the WebView reports
// the canonical form, so the pin must collapse to it too.
private fun canonicalIpv6(bracketed: String): String? {
    if (!bracketed.endsWith("]")) return null
    val literal = bracketed.substring(1, bracketed.length - 1)
    // Zone ids (%eth0) are not valid in URLs.
    if (literal.isEmpty() || literal.contains('%')) return null
    // A literal with a colon can't be a DNS name, so this never resolves.
    if (!literal.contains(':')) return null
    val address =
        try {
            java.net.InetAddress.getByName(literal)
        } catch (_: Exception) {
            return null
        }
    val bytes =
        when (address) {
            is java.net.Inet6Address -> {
                address.address
            }

            // Java collapses an IPv4-mapped literal (`::ffff:1.2.3.4`) to an
            // Inet4Address — restore the 16-byte mapped form.
            is java.net.Inet4Address -> {
                ByteArray(16).also {
                    it[10] = -1
                    it[11] = -1
                    address.address.copyInto(it, 12)
                }
            }

            else -> {
                return null
            }
        }
    val groups =
        IntArray(8) { i ->
            ((bytes[2 * i].toInt() and 0xff) shl 8) or (bytes[2 * i + 1].toInt() and 0xff)
        }
    // RFC 5952: compress the leftmost longest run (length >= 2) of zero groups.
    var bestStart = -1
    var bestLength = 0
    var start = -1
    for (i in 0..8) {
        if (i < 8 && groups[i] == 0) {
            if (start == -1) start = i
        } else {
            if (start != -1 && i - start > bestLength) {
                bestStart = start
                bestLength = i - start
            }
            start = -1
        }
    }
    if (bestLength < 2) bestStart = -1
    val out = StringBuilder("[")
    var i = 0
    while (i < 8) {
        if (i == bestStart) {
            out.append("::")
            i += bestLength
            continue
        }
        out.append(groups[i].toString(16))
        if (i + 1 < 8 && i + 1 != bestStart) out.append(':')
        i++
    }
    out.append(']')
    return out.toString()
}

// WHATWG "ends in a number": at most ONE trailing empty label is ignored
// (`1.2.3..` stays a domain), and an all-digit last label counts even when it
// later fails IPv4 number parsing (`foo.09` must then be rejected, not treated
// as a domain — matching Chromium's invalid-URL behavior).
private fun ipv4CandidateLabels(host: String): List<String> {
    val parts = host.split('.')
    return if (parts.size > 1 && parts.last().isEmpty()) parts.dropLast(1) else parts
}

private fun endsInIpv4Number(host: String): Boolean {
    val last = ipv4CandidateLabels(host).last()
    if (last.isEmpty()) return false
    if (last.all { it in '0'..'9' }) return true
    return parseIpv4Number(last) != null
}

private fun parseIpv4Number(part: String): Long? {
    if (part.isEmpty()) return null
    val (digits, radix) =
        when {
            part.length >= 2 && (part.startsWith("0x") || part.startsWith("0X")) -> {
                part.substring(2) to 16
            }

            part.length >= 2 && part.startsWith("0") -> {
                part.substring(1) to 8
            }

            else -> {
                part to 10
            }
        }
    if (digits.isEmpty()) return 0L // "0x" alone parses as zero per WHATWG
    // WHATWG IPv4 numbers are bare radix digits — toLong would accept a
    // leading sign, letting host "+1" pin as 0.0.0.1 while Chromium treats
    // it as a domain.
    if (digits.any { it.digitToIntOrNull(radix) == null }) return null
    val significant = digits.trimStart('0').ifEmpty { "0" }
    // WHATWG numbers are arbitrary precision: an over-long digit run is a
    // VALID number (so the host stays on the IPv4 path) that then fails the
    // address's 32-bit range check — not a fall-through to the domain path.
    if (significant.length > 12) return 0x1_0000_0000L
    return try {
        significant.toLong(radix)
    } catch (_: NumberFormatException) {
        null
    }
}

// WHATWG IPv4 parser: up to four numeric parts, the last covering the
// remaining bytes, serialized to canonical dotted-decimal (`127.1` -> 127.0.0.1).
private fun canonicalIpv4(host: String): String? {
    val parts = ipv4CandidateLabels(host)
    if (parts.any(String::isEmpty) || parts.size > 4) return null
    val numbers = parts.map { parseIpv4Number(it) ?: return null }
    if (numbers.dropLast(1).any { it > 0xff }) return null
    val shift = (4 - numbers.size) * 8
    if (numbers.last() shr shift > 0xff) return null
    val value =
        numbers.dropLast(1).foldIndexed(0L) { index, acc, number ->
            acc or (number shl ((3 - index) * 8))
        } or numbers.last()
    return (3 downTo 0).joinToString(".") { ((value shr (it * 8)) and 0xff).toString() }
}

/**
 * Normalizes a URL to its origin (`scheme://host[:port]`), the unit of trust
 * the bridge and navigation gating compare on. Returns null for anything
 * without both a scheme and host.
 */
fun originOf(url: String?): String? {
    val uri = url?.let(Uri::parse) ?: return null
    val scheme = uri.scheme?.lowercase() ?: return null
    val rawHost = uri.host ?: return null
    // A malformed port ("host:notaport") survives inside Uri's host instead of
    // failing the parse. WHATWG treats a non-numeric port as an invalid URL —
    // reject it, allowing the colons of a bracketed IPv6 literal.
    if (rawHost.lastIndexOf(':') > rawHost.lastIndexOf(']')) return null
    val host = toAsciiHost(rawHost)?.lowercase() ?: return null
    // Canonicalize like a browser origin (WHATWG): lowercase scheme + host and
    // omit the default port — so an explicit `https://host:443` (or odd casing)
    // the user typed compares equal to the WebView's normalized `https://host`.
    // The pinned origin and every page URL both flow through here, so they
    // canonicalize identically.
    val port = uri.port
    // WHATWG rejects ports beyond the 16-bit range; letting one through would
    // persist a server the WebView can never load.
    if (port > 0xffff) return null
    // Uri.parsePort overflows Int-sized digit runs to -1 — a port that was
    // written but failed to parse rejects the URL, it doesn't vanish.
    if (port == -1) {
        val hostPort = uri.encodedAuthority?.substringAfterLast('@') ?: ""
        val colon = hostPort.lastIndexOf(':')
        if (colon > hostPort.lastIndexOf(']') && colon < hostPort.length - 1) return null
    }
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
 * True when [url] is a front-door auth-proxy authorize page for [pinnedOrigin]:
 * it carries a `redirect_uri` query parameter that returns to the pinned origin
 * on a path other than the server's own OIDC callback (`/auth/callback`).
 *
 * Deployments fronted by a hosting proxy (e.g. Databricks Apps) intercept every
 * request — including the login endpoints — and bounce unauthenticated visitors
 * to the host's IdP. That flow must complete inside the WebView so the proxy's
 * session cookie lands in the WebView's cookie store; the server's own IdP
 * bounce (redirect_uri path `/auth/callback`) still runs in the system browser
 * (RFC 8252 — Google blocks WebView sign-in).
 */
fun isProxyAuthUrl(
    url: String?,
    pinnedOrigin: String?,
): Boolean {
    if (pinnedOrigin == null) return false
    val uri = url?.let(Uri::parse) ?: return false
    // getQueryParameter throws on opaque (non-hierarchical) URIs like mailto:.
    if (uri.isOpaque) return false
    val redirect = uri.getQueryParameter("redirect_uri") ?: return false
    if (originOf(redirect) != pinnedOrigin) return false
    // Trailing-slash-insensitive: `/auth/callback/` is still the server's own.
    return Uri.parse(redirect).path?.trimEnd('/') != "/auth/callback"
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
    val normalized = withScheme.trimEnd('/')
    // The pinned-origin gate fails closed on hosts originOf can't canonicalize
    // (e.g. `a..b`); rejecting them here surfaces a connect-time error instead
    // of persisting a server that can only ever render a blank page.
    if (originOf(normalized) == null) return null
    return normalized
}
