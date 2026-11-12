package ai.omnigent.android

import android.webkit.CookieManager
import androidx.webkit.CookieManagerCompat
import androidx.webkit.WebViewFeature
import java.net.URI
import java.time.Instant
import java.time.ZoneOffset
import java.time.ZonedDateTime
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

    /**
     * When a `Set-Cookie`-style string from [CookieManagerCompat.getCookieInfo] for [name]
     * expires, or null for a session cookie or an unreadable date.
     */
    fun cookieExpiry(
        cookieInfo: List<String>,
        name: String,
    ): Long? {
        val cookie = cookieInfo.firstOrNull { it.trimStart().startsWith("$name=") } ?: return null
        val expires =
            cookie
                .split(';')
                .map(String::trim)
                .firstOrNull { it.startsWith("expires=", ignoreCase = true) }
                ?.substringAfter('=')
                ?: return null
        return runCatching {
            ZonedDateTime
                .parse(
                    expires.trim(),
                    DateTimeFormatter.RFC_1123_DATE_TIME,
                ).toInstant()
                .toEpochMilli()
        }.getOrNull()
    }

    /**
     * How long to wait before renewing a cookie that expires at [expiresAt]: a fifth of the
     * remaining lifetime early, at most a minute; zero once it is due.
     */
    fun renewalDelay(
        expiresAt: Long,
        now: Long,
    ): Long {
        val remaining = expiresAt - now
        if (remaining <= 0) return 0
        return maxOf(0, remaining - minOf(60_000L, remaining / 5))
    }

    /** A background renewal failure worth remembering: it deleted the grant. */
    fun rememberedRenewalCause(error: Throwable?): OidcSignInException? =
        when (error) {
            is OidcSignInException.GrantExpired, is OidcSignInException.GrantRejected -> error
            else -> null
        }

    /** The error to explain at the next prompt: a remembered cause replaces "no stored grant". */
    fun reauthenticationCause(
        error: Throwable,
        remembered: OidcSignInException?,
    ): Throwable =
        if (remembered != null &&
            error is OidcSignInException.NoStoredGrant
        ) {
            remembered
        } else {
            error
        }

    /** The Connect screen message after a sign-out; [complete] is false when something survived. */
    fun signedOutMessage(
        host: String,
        complete: Boolean,
    ): String =
        if (complete) {
            "You're signed out of $host."
        } else {
            "Couldn't finish signing out of $host; its saved sign-in may still be used. " +
                "Connect, then sign out again."
        }

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

    /** When the cookie [name] sent to [url] expires, when the web view can tell. */
    fun expiry(
        url: String,
        name: String,
    ): Long? = null
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

    override fun expiry(
        url: String,
        name: String,
    ): Long? {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.GET_COOKIE_INFO)) return null
        val cookies =
            runCatching { CookieManagerCompat.getCookieInfo(CookieManager.getInstance(), url) }
        return OidcWebSession.cookieExpiry(cookies.getOrNull().orEmpty(), name)
    }
}
