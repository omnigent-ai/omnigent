package ai.omnigent.android

import android.webkit.CookieManager
import java.net.URI
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/** A web view connection whose OIDC session the shell owns: the server and its session cookie. */
internal data class OidcConnection(
    /** The clean server URL (origin plus mount), never a page under it. */
    val serverUrl: String,
    val cookieName: String,
) {
    val origin: String get() = originOf(serverUrl) ?: serverUrl
    val host: String get() = origin.substringAfter("://")
}

/** The server routes the shell handles itself instead of loading them in the web view. */
internal enum class OidcAuthRoute {
    LOGIN,
    LOGOUT,
    ;

    companion object {
        /** The route [url] is under [serverUrl]'s mount, or null for any other page or origin. */
        fun of(
            url: String?,
            serverUrl: String,
        ): OidcAuthRoute? {
            val origin = originOf(url) ?: return null
            if (origin != originOf(serverUrl)) return null
            val path = runCatching { URI(url).rawPath }.getOrNull() ?: return null
            val mount = OidcWebSession.mountPath(serverUrl)
            return when (path) {
                "$mount/auth/login" -> LOGIN
                "$mount/auth/logout" -> LOGOUT
                else -> null
            }
        }
    }
}

/** Pure decisions behind the web view's OIDC session lifecycle, mirroring the desktop and iOS. */
internal object OidcWebSession {
    /** The server URL's path without trailing slashes; empty for a server at the origin root. */
    fun mountPath(serverUrl: String): String =
        runCatching { URI(serverUrl).rawPath.orEmpty().trimEnd('/') }.getOrDefault("")

    /** Whether [url] is an app page of the server: same origin, under its mount, not an auth route. */
    fun isPage(
        url: String?,
        serverUrl: String,
    ): Boolean {
        if (url == null || originOf(url) != originOf(serverUrl)) return false
        if (OidcAuthRoute.of(url, serverUrl) != null) return false
        val path = runCatching { URI(url).rawPath.orEmpty() }.getOrNull() ?: return false
        val mount = mountPath(serverUrl)
        return mount.isEmpty() || path == mount || path.startsWith("$mount/")
    }

    /**
     * The `Set-Cookie` value installing [session] as the server's own session cookie: host-only,
     * `/`, HttpOnly, SameSite=Lax, Secure on https, and expiring with the session.
     */
    fun sessionCookie(
        name: String,
        session: OidcSessionToken,
        serverUrl: String,
    ): String =
        buildString {
            append(name).append('=').append(session.token)
            append("; Path=/; HttpOnly; SameSite=Lax")
            if (isHttps(serverUrl)) append("; Secure")
            session.expiresAtEpochMillis?.let { expiresAt ->
                append("; Expires=")
                append(
                    DateTimeFormatter.RFC_1123_DATE_TIME.format(
                        Instant.ofEpochMilli(expiresAt).atOffset(ZoneOffset.UTC),
                    ),
                )
            }
        }

    /** The `Set-Cookie` value deleting the cookie [sessionCookie] installs. */
    fun deletionCookie(
        name: String,
        serverUrl: String,
    ): String =
        buildString {
            append(name).append("=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
            if (isHttps(serverUrl)) append("; Secure")
        }

    /** The value of [name] in a `Cookie` header, as [CookieManager.getCookie] returns it. */
    fun cookieValue(
        header: String?,
        name: String,
    ): String? =
        header
            ?.split(';')
            ?.map(String::trim)
            ?.firstOrNull { it.startsWith("$name=") }
            ?.substringAfter('=')
            ?.takeIf(String::isNotEmpty)

    /** An unreachable server is a connection error, never a reason to sign in again. */
    fun isNetworkFailure(error: Throwable?): Boolean = error is OidcSignInException.Network

    /**
     * The reason to keep when the user closes a sign-in that a failed renewal opened: an expired,
     * ended or refused session. Null when there was simply no sign-in to renew.
     */
    fun cancelledSignInCause(renewalError: Throwable?): OidcSignInException? =
        when (renewalError) {
            is OidcSignInException.GrantExpired,
            is OidcSignInException.GrantRejected,
            is OidcSignInException.SessionRejected,
            -> renewalError

            else -> null
        }

    /** The "Sign in again" message for a session that can't be renewed. */
    fun reauthenticationMessage(
        error: Throwable?,
        host: String,
    ): String =
        (error as? OidcSignInException)?.takeUnless { isNetworkFailure(it) }?.message
            ?: OidcSignInException.NoStoredGrant(host).message.orEmpty()

    private fun isHttps(serverUrl: String): Boolean =
        serverUrl.startsWith("https://", ignoreCase = true)
}

/**
 * Stops renew-and-reload loops: the page asking to sign in again this soon after a renewal
 * means the server rejected the renewed session.
 */
internal class OidcRenewalGuard(
    private val windowMillis: Long = REJECTED_RENEWAL_WINDOW_MS,
) {
    private var lastRecoveryAt: Long? = null

    /** Records a sign-in request; a request that joins a renewal in flight is always answered. */
    fun shouldRenew(
        now: Long,
        renewalPending: Boolean,
    ): Boolean {
        val last = lastRecoveryAt
        if (!renewalPending && last != null && now - last in 0 until windowMillis) return false
        lastRecoveryAt = now
        return true
    }

    companion object {
        const val REJECTED_RENEWAL_WINDOW_MS = 15_000L
    }
}

/** The web view cookie store the native OIDC session lives in. */
internal interface OidcCookieJar {
    /** The `Cookie` header the web view would send to [url]. */
    fun get(url: String): String?

    fun set(
        url: String,
        setCookie: String,
        callback: (Boolean) -> Unit,
    )

    fun flush()
}

/** The default WebView profile, which generic servers share as they always have. */
internal object DefaultProfileCookieJar : OidcCookieJar {
    override fun get(url: String): String? = CookieManager.getInstance().getCookie(url)

    override fun set(
        url: String,
        setCookie: String,
        callback: (Boolean) -> Unit,
    ) {
        val cookies = CookieManager.getInstance()
        cookies.setAcceptCookie(true)
        cookies.setCookie(url, setCookie) { accepted -> callback(accepted) }
    }

    override fun flush() = CookieManager.getInstance().flush()
}
