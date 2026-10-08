package ai.omnigent.android

import android.content.Context
import org.json.JSONObject
import java.net.URI
import java.util.concurrent.CancellationException
import java.util.concurrent.CompletableFuture
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/** A session token minted by sign-in or refresh; it lives only in the session cookie. */
internal data class OidcSessionToken(
    val token: String,
    /** When the server said the token expires, measured from when it was requested. */
    val expiresAtEpochMillis: Long?,
) {
    override fun toString(): String = "OidcSessionToken(expiresAt=$expiresAtEpochMillis)"

    companion object {
        /** A non-empty RFC 6265 cookie value, so it can't break the `Cookie` header. */
        fun isCookieSafe(token: String): Boolean =
            token.isNotEmpty() && token.all { it.code in 0x21..0x7E && it !in "\",;\\" }
    }
}

/** The app's RFC 8252 §7.1 redirect for native OIDC sign-in. */
internal object OidcRedirect {
    const val SCHEME = "ai.omnigent.android"
    const val REDIRECT_URI = "ai.omnigent.android:/oauth/callback"
    private const val PATH = "/oauth/callback"

    /** Whether [callback] is addressed to this redirect: the scheme, no authority, the path. */
    fun matches(callback: URI): Boolean =
        callback.scheme.equals(SCHEME, ignoreCase = true) &&
            callback.rawAuthority == null &&
            callback.rawPath == PATH
}

/** Why an OIDC sign-in, renewal or session check failed, worded for the user. */
internal sealed class OidcSignInException(
    message: String,
) : Exception(message) {
    class InvalidServerUrl : OidcSignInException("Enter a valid server URL.")

    class BrowserUnavailable(
        host: String,
    ) : OidcSignInException("Couldn't open the sign-in page for $host. Try again.")

    /** The callback isn't this sign-in's: wrong URI or state, or no code. */
    class InvalidCallback(
        host: String?,
    ) : OidcSignInException(
            host?.let { "Sign-in to $it didn't complete. Try again." }
                ?: "Sign-in didn't complete. Try again.",
        )

    /** The server or IdP declined the sign-in; the server's reason, if it gave one. */
    class SignInRefused(
        host: String,
        description: String?,
    ) : OidcSignInException(
            description?.takeIf(
                String::isNotBlank,
            ) ?: "$host didn't accept the sign-in. Try again.",
        )

    /** The stored grant is past its lifetime (`expired_token`); it has been forgotten. */
    class GrantExpired(
        host: String,
    ) : OidcSignInException("Your sign-in to $host has expired. Sign in again to continue.")

    /** The server revoked or rejected the stored grant (`invalid_grant`); it has been forgotten. */
    class GrantRejected(
        host: String,
    ) : OidcSignInException("$host ended your session. Sign in again to continue.")

    /** No usable grant is stored, or the server can't refresh one; none remains stored. */
    class NoStoredGrant(
        host: String,
    ) : OidcSignInException("Sign in to $host to continue.")

    /** The server could not be reached or answered unexpectedly; any stored grant is kept. */
    class Network(
        host: String,
    ) : OidcSignInException("Couldn't reach $host. Check your connection and try again.")

    /** The server refused a freshly minted session, or the web view wouldn't store it. */
    class SessionRejected(
        host: String,
    ) : OidcSignInException("$host didn't accept the session. Sign in again to continue.")

    /** The saved sign-in couldn't be read or written, e.g. while the device is locked. */
    class StorageUnavailable :
        OidcSignInException("Couldn't access the saved sign-in. Unlock the device and try again.")
}

/**
 * Native OIDC sign-in (PKCE against the server's `/auth/login` native parameters, completed at
 * `/auth/native-token`) and the per-origin refresh grant that renews sessions.
 *
 * [completeSignIn], [isAccepted] and [signOut] block on the network, so call them off the main
 * thread. A [refresh] is shared by every caller for the same origin.
 */
internal class OidcCredentials(
    private val store: OidcCredentialStorage,
    private val pending: OidcPendingSignInStore,
    private val transport: OAuthTransport = UrlConnectionOAuthTransport(NETWORK_TIMEOUT_MS),
    private val verifyTransport: OAuthTransport = UrlConnectionOAuthTransport(VERIFY_TIMEOUT_MS),
    private val executor: Executor = Executors.newCachedThreadPool(),
    private val now: () -> Long = System::currentTimeMillis,
    private val randomValue: () -> String = { OAuthSupport.randomValue() },
) {
    /** A finished sign-in: the session to install and where to install it. */
    data class CompletedSignIn(
        val serverUrl: String,
        val cookieName: String,
        val session: OidcSessionToken,
    )

    private val refreshes = mutableMapOf<String, CompletableFuture<OidcSessionToken>>()
    private var handedOff: CompletedSignIn? = null

    /** Records a new attempt (replacing any other) and returns the URL to open. */
    @Synchronized
    fun beginSignIn(
        serverUrl: String,
        cookieName: String,
    ): URI {
        val server = Server.of(serverUrl)
        handedOff = null
        val attempt =
            try {
                pending.begin(serverUrl, cookieName, randomValue(), randomValue())
            } catch (_: CredentialStorageException) {
                throw OidcSignInException.StorageUnavailable()
            }
        return server.authorizationUri(attempt.state, OAuthSupport.challenge(attempt.verifier))
    }

    /** Abandons the attempt in progress and any result not yet picked up. */
    @Synchronized
    fun cancelSignIn() {
        handedOff = null
        runCatching { pending.clear() }
    }

    /**
     * Exchanges the code in [callback] for a session; the attempt is consumed, so a duplicate
     * delivery fails. Stores the server's refresh grant, or forgets an older one when the server
     * issues none. A grant that can't be stored is revoked.
     */
    fun completeSignIn(callback: URI): CompletedSignIn {
        val attempt =
            try {
                pending.load()
            } catch (_: CredentialStorageException) {
                // Kept for a retry: a locked device reads as unreadable too.
                throw OidcSignInException.StorageUnavailable()
            } ?: throw OidcSignInException.InvalidCallback(null)
        val server = Server.of(attempt.serverUrl)
        val items = callbackItems(callback, attempt.state, server.host)
        // Only this attempt's own callback ends it, so a stray one can't cancel a live sign-in.
        pending.consume(attempt.id) ?: throw OidcSignInException.InvalidCallback(server.host)
        if (items.any { it.first == "error" }) {
            val description = items.firstOrNull { it.first == "error_description" }?.second
            throw OidcSignInException.SignInRefused(server.host, description)
        }
        val code =
            items
                .filter { it.first == "code" }
                .singleOrNull()
                ?.second
                ?.takeIf(String::isNotEmpty)
                ?: throw OidcSignInException.InvalidCallback(server.host)

        val requestedAt = now()
        val response =
            post(
                server,
                "/auth/native-token",
                listOf(
                    "code" to code,
                    "code_verifier" to attempt.verifier,
                    "redirect_uri" to OidcRedirect.REDIRECT_URI,
                ),
            )
        val body = jsonObject(response.body)
        val session =
            sessionToken(body, "token", requestedAt)?.takeIf { response.status == 200 }
                ?: throw OidcSignInException.SignInRefused(server.host, null)

        val refreshToken = body?.opt("refresh_token") as? String
        if (!refreshToken.isNullOrEmpty()) {
            val grant = OidcRefreshGrant(refreshToken, body?.opt("user_id") as? String)
            synchronized(this) {
                try {
                    store.save(server.origin, grant)
                } catch (_: Exception) {
                    // A grant that can't be kept must not stay live on the server.
                    revoke(server, grant)
                    throw OidcSignInException.StorageUnavailable()
                }
            }
        } else {
            // A server without refresh grants: an older grant for this origin is stale, and a
            // later refresh must not sign in as whoever it belonged to.
            try {
                synchronized(this) { store.delete(server.origin) }
            } catch (_: Exception) {
                throw OidcSignInException.StorageUnavailable()
            }
        }
        return CompletedSignIn(attempt.serverUrl, attempt.cookieName, session)
    }

    /** [completeSignIn] for the callback receiver; the shell takes the result with [takeHandedOff]. */
    fun completeHandedOffSignIn(callback: URI) {
        val completed = completeSignIn(callback)
        synchronized(this) { handedOff = completed }
    }

    /** The sign-in the callback receiver completed, taken once. */
    @Synchronized
    fun takeHandedOff(): CompletedSignIn? = handedOff.also { handedOff = null }

    /**
     * Mints a session token from the stored grant, with one request in flight per origin.
     * `invalid_grant`, `expired_token` and a 404 forget the grant; any other failure keeps it.
     */
    @Synchronized
    fun refresh(serverUrl: String): CompletableFuture<OidcSessionToken> {
        val server =
            try {
                Server.of(serverUrl)
            } catch (error: OidcSignInException) {
                return CompletableFuture<OidcSessionToken>().apply { completeExceptionally(error) }
            }
        refreshes[server.origin]?.let { return it.thenApply { token -> token } }
        val flight = CompletableFuture<OidcSessionToken>()
        refreshes[server.origin] = flight
        executor.execute {
            val result = runCatching { performRefresh(server) }
            // Leaving the map and completing are one step, so a sign-out either cancels the
            // flight first or comes after its result was delivered.
            synchronized(this) {
                if (refreshes[server.origin] === flight) refreshes.remove(server.origin)
                result.fold(flight::complete, flight::completeExceptionally)
            }
        }
        // A dependent future, so one caller's cancellation leaves the shared refresh alone.
        return flight.thenApply { token -> token }
    }

    /**
     * Forgets the stored grant before any network call, then revokes it as a best effort. A
     * refresh in flight for the origin is dropped, so nobody can install a session minted from
     * the old grant. Throws when the grant couldn't be deleted; the returned future never fails.
     */
    fun signOut(serverUrl: String): CompletableFuture<Void> {
        val server = Server.of(serverUrl)
        val grant: OidcRefreshGrant?
        val deleted: Boolean
        synchronized(this) {
            refreshes.remove(server.origin)?.completeExceptionally(CancellationException())
            // An unreadable grant can't be revoked, but it is still deleted.
            grant = runCatching { store.load(server.origin) }.getOrNull()
            deleted = runCatching { store.delete(server.origin) }.isSuccess
        }
        val revocation =
            grant?.let { revoke(server, it) } ?: CompletableFuture.completedFuture<Void>(null)
        if (!deleted) throw OidcSignInException.StorageUnavailable()
        return revocation
    }

    /** Whether a readable refresh grant is stored for the server's origin. */
    fun hasStoredGrant(serverUrl: String): Boolean {
        val origin = originOf(serverUrl) ?: return false
        return runCatching { store.load(origin) != null }.getOrDefault(false)
    }

    /**
     * Whether the server accepts [token] as its session cookie: `GET <mount>/v1/me` answers 200.
     * A 401, 403 or redirect is a rejection; any other answer throws [OidcSignInException.Network].
     */
    fun isAccepted(
        serverUrl: String,
        cookieName: String,
        token: String,
    ): Boolean {
        val server = Server.of(serverUrl)
        val request =
            OAuthHttpRequest(
                server.endpoint("/v1/me"),
                headers = mapOf("Cookie" to "$cookieName=$token", "Accept" to "application/json"),
            )
        val response = send(verifyTransport, request, server.host)
        return when (response.status) {
            200 -> true
            401, 403, in 300..399 -> false
            else -> throw OidcSignInException.Network(server.host)
        }
    }

    private fun performRefresh(server: Server): OidcSessionToken {
        val grant =
            try {
                store.load(server.origin)
            } catch (_: CredentialStorageException) {
                // A locked device reads as unreadable too, so the record is kept for later.
                throw OidcSignInException.StorageUnavailable()
            } ?: throw OidcSignInException.NoStoredGrant(server.host)
        val requestedAt = now()
        val response =
            post(
                server,
                "/oauth/token",
                listOf("grant_type" to "refresh_token", "refresh_token" to grant.refreshToken),
            )
        val body = jsonObject(response.body)
        if (response.status ==
            200
        ) {
            sessionToken(body, "access_token", requestedAt)?.let { return it }
        }
        val deadGrant =
            when {
                body?.opt(
                    "error",
                ) == "invalid_grant" -> OidcSignInException.GrantRejected(server.host)

                body?.opt(
                    "error",
                ) == "expired_token" -> OidcSignInException.GrantExpired(server.host)

                response.status == 404 -> OidcSignInException.NoStoredGrant(server.host)

                else -> throw OidcSignInException.Network(server.host)
            }
        // A dead grant: forget it so the next connect signs in through the browser, unless a
        // newer sign-in already replaced it while this request was out.
        val forgotten =
            runCatching {
                synchronized(this) {
                    if (store.load(server.origin) == grant) store.delete(server.origin)
                }
            }
        if (forgotten.isFailure) throw OidcSignInException.StorageUnavailable()
        throw deadGrant
    }

    private fun revoke(
        server: Server,
        grant: OidcRefreshGrant,
    ): CompletableFuture<Void> =
        CompletableFuture.runAsync(
            {
                runCatching {
                    post(server, "/oauth/revoke", listOf("refresh_token" to grant.refreshToken))
                }
            },
            executor,
        )

    private fun post(
        server: Server,
        routePath: String,
        fields: List<Pair<String, String>>,
    ): OAuthHttpResponse =
        send(
            transport,
            OAuthHttpRequest(
                server.endpoint(routePath),
                method = "POST",
                headers =
                    mapOf(
                        "Content-Type" to "application/x-www-form-urlencoded",
                        "Accept" to "application/json",
                    ),
                body = OAuthSupport.formBody(fields),
            ),
            server.host,
        )

    private fun send(
        transport: OAuthTransport,
        request: OAuthHttpRequest,
        host: String,
    ): OAuthHttpResponse =
        try {
            transport.execute(request)
        } catch (_: OAuthNetworkException) {
            throw OidcSignInException.Network(host)
        }

    /** A server URL resolved to its credential origin, host label and mount-aware endpoints. */
    private class Server(
        val url: String,
        val origin: String,
        val host: String,
    ) {
        fun endpoint(routePath: String): URI =
            serverEndpoint(url, routePath) ?: throw OidcSignInException.InvalidServerUrl()

        fun authorizationUri(
            state: String,
            challenge: String,
        ): URI {
            val query =
                listOf(
                    "native_redirect_uri" to OidcRedirect.REDIRECT_URI,
                    "native_state" to state,
                    "code_challenge" to challenge,
                    "code_challenge_method" to "S256",
                ).joinToString("&") { (key, value) -> "$key=${formEncode(value)}" }
            return URI("${endpoint("/auth/login")}?$query")
        }

        companion object {
            fun of(serverUrl: String): Server {
                val origin = originOf(serverUrl) ?: throw OidcSignInException.InvalidServerUrl()
                val scheme = origin.substringBefore("://")
                if (!isHttpScheme(scheme) || serverEndpoint(serverUrl, "/") == null) {
                    throw OidcSignInException.InvalidServerUrl()
                }
                return Server(serverUrl, origin, origin.removePrefix("$scheme://"))
            }
        }
    }

    companion object {
        const val NETWORK_TIMEOUT_MS = 20_000
        const val VERIFY_TIMEOUT_MS = 10_000

        @Volatile private var shared: OidcCredentials? = null

        /** Shared by every activity so refreshes stay one per origin across reconnects. */
        fun shared(context: Context): OidcCredentials =
            shared ?: synchronized(this) {
                shared ?: OidcCredentials(
                    OidcCredentialStore(context.applicationContext),
                    OidcPendingSignInStore(context.applicationContext),
                ).also { shared = it }
            }

        /**
         * The query of a callback addressed to this sign-in: the app's redirect and exactly one
         * `state`, equal to [state].
         */
        private fun callbackItems(
            callback: URI,
            state: String,
            host: String,
        ): List<Pair<String, String?>> {
            if (!OidcRedirect.matches(callback)) throw OidcSignInException.InvalidCallback(host)
            val items =
                try {
                    queryItems(callback)
                } catch (_: Exception) {
                    throw OidcSignInException.InvalidCallback(host)
                }
            val states = items.filter { it.first == "state" }
            if (states.size != 1 || states.single().second != state) {
                throw OidcSignInException.InvalidCallback(host)
            }
            return items
        }

        private fun jsonObject(body: ByteArray): JSONObject? =
            runCatching { JSONObject(body.toString(Charsets.UTF_8)) }.getOrNull()

        private fun sessionToken(
            body: JSONObject?,
            key: String,
            requestedAt: Long,
        ): OidcSessionToken? {
            val token = body?.opt(key) as? String ?: return null
            if (!OidcSessionToken.isCookieSafe(token)) return null
            val expiresIn = (body.opt("expires_in") as? Number)?.toDouble()
            val expiresAt =
                expiresIn
                    ?.takeIf { it.isFinite() && it > 0 }
                    ?.let { seconds ->
                        runCatching {
                            Math.addExact(requestedAt, Math.multiplyExact(seconds.toLong(), 1_000L))
                        }.getOrNull()
                    }
            return OidcSessionToken(token, expiresAt)
        }
    }
}
