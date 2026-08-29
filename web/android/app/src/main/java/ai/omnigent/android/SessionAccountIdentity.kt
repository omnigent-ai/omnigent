package ai.omnigent.android

import org.json.JSONObject
import java.security.MessageDigest
import java.util.Base64

/**
 * Stable per-account identity marker for an (origin, session token) pair, used
 * by MainActivity.onSessionToken to decide whether an injected token actually
 * belongs to a DIFFERENT account than the last one — only then may the poll
 * snapshot / notifications be cleared. An unconditional clear on every token
 * hand-off (e.g. a cold start re-injecting the SAME session) wipes the
 * baseline, and a running → idle edge completing in that window is lost
 * forever (the empty-previous gate suppresses first-run firing).
 *
 * Identity is the JWT's `sub` claim scoped to the origin, so a re-login that
 * mints a NEW token for the SAME account keeps the baseline too. When the
 * payload is undecodable the raw token stands in — conservative: an
 * unidentifiable new token then reads as a new account and clears, which is
 * the pre-existing behavior. Persisted as a SHA-256 digest so the marker never
 * stores the subject (or, on the fallback path, the live credential) in plain
 * text.
 *
 * Pure JVM (no Android imports) so it's unit-testable; `org.json` matches
 * [parseSessionList]'s existing use.
 */
fun sessionAccountIdentity(
    origin: String,
    token: String,
): String {
    val subject = jwtSubject(token)
    val material = if (subject != null) "sub:$subject" else "tok:$token"
    // NUL separator: neither origin nor material can contain it, so distinct
    // (origin, material) pairs can never concatenate to the same digest input.
    return sha256Hex("$origin\u0000$material")
}

/**
 * True when the poll worker must NOT act on the credential it read: a
 * [persisted] identity marker is absent or names a different account than the
 * cookie's own [cookieIdentity]. A null marker is unverified: accounts-mode
 * login/logout happens entirely inside the WebView, so acting before its next
 * pinned page load binds the cookie could notify or persist across accounts.
 */
fun identityMismatch(
    persisted: String?,
    cookieIdentity: String,
): Boolean = persisted == null || persisted != cookieIdentity

/**
 * The JWT payload's `sub` claim, or null when the token isn't a decodable JWT
 * or the claim is absent/empty. Never throws — any malformed input is null.
 */
fun jwtSubject(token: String): String? {
    val parts = token.split('.')
    if (parts.size != 3) return null
    return try {
        val payload = String(Base64.getUrlDecoder().decode(parts[1]), Charsets.UTF_8)
        JSONObject(payload)
            .takeUnless { it.isNull("sub") }
            ?.optString("sub")
            ?.ifEmpty { null }
    } catch (_: Throwable) {
        null
    }
}

private fun sha256Hex(input: String): String =
    MessageDigest
        .getInstance("SHA-256")
        .digest(input.toByteArray(Charsets.UTF_8))
        .joinToString("") { "%02x".format(it) }
