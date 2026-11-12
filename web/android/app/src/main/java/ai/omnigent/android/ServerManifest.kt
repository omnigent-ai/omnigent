package ai.omnigent.android

import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

/**
 * A server's `/.well-known/omnigent.json`, read before loading so the shell can adapt to it.
 *
 * Mirrors the desktop and iOS readers: anything short of a well-formed manifest (404 from an
 * older server, unreachable host, HTML, malformed JSON, wrong types) is the [BASELINE], meaning
 * "keep the existing behavior". Gate on `manifestVersion >= N`, never `== N`.
 */
internal data class ServerManifest(
    val manifestVersion: Double,
    /** The `auth` block, or null when absent or untrustworthy. */
    val auth: Auth?,
) {
    data class Auth(
        val mode: Mode,
        /** `__Host-ap_session` or `ap_session`; never the `__Host-` name for an http server. */
        val sessionCookie: String?,
        /** Redirect URIs the server accepts for native sign-in; empty when it names none. */
        val nativeRedirectUris: List<String>,
    )

    enum class Mode(
        val wireName: String,
    ) {
        OIDC("oidc"),
        ACCOUNTS("accounts"),
        HEADER("header"),
        CUSTOM("custom"),
        NONE("none"),
    }

    /** The session cookie when the server offers native OIDC sign-in to [redirectUri]. */
    fun nativeSignInCookie(redirectUri: String): String? {
        val auth = auth ?: return null
        if (auth.mode != Mode.OIDC || redirectUri !in auth.nativeRedirectUris) return null
        return auth.sessionCookie
    }

    companion object {
        /** What a server without the manifest route implies. */
        val BASELINE = ServerManifest(0.0, null)

        const val PATH = "/.well-known/omnigent.json"
        private const val FETCH_TIMEOUT_MS = 5_000
        private val SESSION_COOKIE_NAMES = setOf("__Host-ap_session", "ap_session")

        /** Reads the manifest at [serverUrl]'s origin without following redirects. Blocking. */
        fun fetch(
            serverUrl: String,
            transport: OAuthTransport = UrlConnectionOAuthTransport(FETCH_TIMEOUT_MS),
        ): ServerManifest {
            val origin = originOf(serverUrl) ?: return BASELINE
            if (!isHttpScheme(Uri.parse(origin).scheme)) return BASELINE
            val uri = runCatching { URI(origin + PATH) }.getOrNull() ?: return BASELINE
            val response =
                try {
                    transport.execute(
                        OAuthHttpRequest(uri, headers = mapOf("Accept" to "application/json")),
                    )
                } catch (_: Exception) {
                    return BASELINE
                }
            if (response.status !in 200..299) return BASELINE
            if (response.contentType?.lowercase()?.contains("json") != true) return BASELINE
            return parse(response.body, serverUrl)
        }

        /** The manifest in a response body, or [BASELINE] when it is not one. */
        fun parse(
            body: ByteArray,
            serverUrl: String,
        ): ServerManifest {
            val document =
                try {
                    JSONObject(body.toString(Charsets.UTF_8))
                } catch (_: Exception) {
                    return BASELINE
                }
            val version = finiteNumber(document.opt("manifest_version")) ?: return BASELINE
            return ServerManifest(version, auth(document.opt("auth"), serverUrl))
        }

        /** Only known modes and the two real cookie names pass, and `__Host-` only for https. */
        fun auth(
            raw: Any?,
            serverUrl: String,
        ): Auth? {
            val block = raw as? JSONObject ?: return null
            val modeName = block.opt("mode") as? String ?: return null
            val mode = Mode.entries.firstOrNull { it.wireName == modeName } ?: return null
            var sessionCookie =
                (block.opt("session_cookie") as? String)?.takeIf(SESSION_COOKIE_NAMES::contains)
            if (sessionCookie?.startsWith("__Host-") == true &&
                Uri.parse(serverUrl).scheme?.lowercase() != "https"
            ) {
                sessionCookie = null
            }
            val redirects = block.opt("native_redirect_uris") as? JSONArray
            val nativeRedirectUris =
                redirects?.let { list ->
                    (0 until list.length()).mapNotNull { list.opt(it) as? String }
                }
            return Auth(mode, sessionCookie, nativeRedirectUris.orEmpty())
        }

        /** A finite JSON number; JSON booleans parse as [Boolean], so they never pass. */
        private fun finiteNumber(raw: Any?): Double? =
            (raw as? Number)?.toDouble()?.takeIf { it.isFinite() }
    }
}
