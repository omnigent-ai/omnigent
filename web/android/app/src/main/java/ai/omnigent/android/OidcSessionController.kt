package ai.omnigent.android

import java.net.URI
import java.util.concurrent.CancellationException
import java.util.concurrent.CompletionException
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executor

/**
 * The native OIDC session of the shell's web view, mirroring the desktop and iOS shells.
 *
 * Connecting reads the server's manifest, then reuses the session cookie if `/v1/me` accepts it,
 * else renews it from the stored grant, else signs in through the browser. Only an explicit
 * connect opens the browser by itself; any other connect asks first.
 *
 * While connected, the cookie is renewed before it expires and on return to the foreground when
 * it's due. The page asking to sign in renews silently and reloads it, and asking again within
 * 15 s asks the user. Sign-out forgets the grant, clears the cookie and revokes the grant in the
 * background.
 *
 * Every method runs on the main thread; network work runs on [io] and comes back through [main].
 */
internal class OidcSessionController(
    private val host: Host,
    private val credentials: OidcCredentials,
    private val cookies: OidcCookieJar,
    private val io: Executor,
    private val main: Executor,
    private val scheduler: OidcScheduler,
    private val now: () -> Long = System::currentTimeMillis,
    private val fetchManifest: (String) -> ServerManifest = { ServerManifest.fetch(it) },
) {
    interface Host {
        /** Covers the page with progress and Cancel. */
        fun showProgress(progress: Progress)

        /** Covers the page with [message], **Sign In** and Cancel. */
        fun showSignInRequired(message: String)

        /** The server doesn't offer this app native sign-in: load it as before. */
        fun loadWithoutNativeSignIn(url: String)

        /** The session is installed: uncover and load [url]. */
        fun loadPage(url: String)

        /** Back to the Connect screen with the server kept, and why, if anything went wrong. */
        fun returnToSetup(message: String?)

        /** Opens the browser sign-in; false when no browser could. */
        fun launchSignIn(
            serverUrl: String,
            cookieName: String,
        ): Boolean

        /** Signed out of the server: back to the Connect screen with [message]. */
        fun signedOut(message: String)
    }

    enum class Progress { CONNECTING, SIGNING_IN, COMPLETING }

    /** The connection whose session is installed; the shell then owns its lifecycle. */
    var connection: OidcConnection? = null
        private set

    /** Whether the connected server can be signed out natively. */
    val canSignOut: Boolean get() = connection != null

    private data class Attempt(
        val connection: OidcConnection,
        val page: String,
        /** Why the session couldn't be renewed, kept for a sign-in the user then closes. */
        val cause: Throwable?,
    )

    private var generation = 0
    private var browserSignIn: Attempt? = null
    private var prompt: Attempt? = null
    private var pageUrl: String? = null
    private var guard = OidcRenewalGuard()
    private var renewing = false
    private var reloadRequested = false
    private var cancelTimer: (() -> Unit)? = null
    private var foreground = true

    /** A browser sign-in is open or being completed; it installs its own session. */
    private var completing = false
    private val signingIn: Boolean get() = browserSignIn != null || completing

    /** The session this view installed; its expiry times renewal when the web view can't tell. */
    private var installed: OidcSessionToken? = null

    /** Why a background renewal lost the grant, kept for the next prompt. */
    private var renewalCause: OidcSignInException? = null

    /** Connects to [serverUrl]; only an [interactive] connect may open the browser by itself. */
    fun connect(
        serverUrl: String,
        interactive: Boolean,
    ) {
        stop()
        val id = generation
        host.showProgress(Progress.CONNECTING)
        io.execute {
            val manifest = fetchManifest(serverUrl)
            main.execute {
                if (id != generation) return@execute
                val cookieName = manifest.nativeSignInCookie(OidcRedirect.REDIRECT_URI)
                if (cookieName == null) {
                    host.loadWithoutNativeSignIn(serverUrl)
                } else {
                    ensureSession(OidcConnection(serverUrl, cookieName), serverUrl, interactive, id)
                }
            }
        }
    }

    /** Ends this view's native session lifecycle; the stored grant and cookie are left alone. */
    fun stop() {
        generation++
        connection = null
        browserSignIn = null
        prompt = null
        pageUrl = null
        guard = OidcRenewalGuard()
        renewing = false
        reloadRequested = false
        installed = null
        renewalCause = null
        completing = false
        cancelRenewalTimer()
    }

    /** **Sign In** on the prompt. */
    fun signIn() {
        val prompted = prompt ?: return
        supersede()
        openBrowser(prompted)
    }

    /** **Cancel** on the overlay; a sign-in a failed renewal opened keeps its reason. */
    fun cancel() {
        val cause = OidcWebSession.cancelledSignInCause(browserSignIn?.cause)
        if (browserSignIn != null) credentials.cancelSignIn()
        stop()
        host.returnToSetup(cause?.message)
    }

    /**
     * Signs out of the connected server: forgets the grant (revoked in the background), clears
     * the session cookie, and returns to setup. False when no native session is connected.
     */
    fun signOut(): Boolean {
        val connected = connection ?: return false
        // Stop first: forgetting the grant cancels a renewal in flight, whose failure must not
        // reach this view as a reason to return to setup.
        stop()
        val grantForgotten = runCatching { credentials.signOut(connected.serverUrl) }.isSuccess
        val deletion = OidcWebSession.deletionCookie(connected.cookieName, connected.serverUrl)
        cookies.set(connected.origin, deletion) {
            main.execute {
                cookies.flush()
                val remaining =
                    OidcWebSession.cookieValue(
                        cookies.get(meUrl(connected)),
                        connected.cookieName,
                    )
                host.signedOut(
                    OidcWebSession.signedOutMessage(
                        connected.host,
                        grantForgotten && remaining == null,
                    ),
                )
            }
        }
        return true
    }

    /** Debug fault injection: deletes the session cookie; [done] says whether one was removed. */
    fun clearSessionCookie(done: (Boolean) -> Unit) {
        val connected = connection ?: return done(false)
        val url = meUrl(connected)
        if (OidcWebSession.cookieValue(cookies.get(url), connected.cookieName) ==
            null
        ) {
            return done(false)
        }
        cookies.set(
            connected.origin,
            OidcWebSession.deletionCookie(connected.cookieName, connected.serverUrl),
        ) {
            main.execute {
                cookies.flush()
                done(OidcWebSession.cookieValue(cookies.get(url), connected.cookieName) == null)
            }
        }
    }

    /** Debug fault injection: forgets the stored grant without revoking it. */
    fun forgetGrant(): Boolean {
        val connected = connection ?: return false
        return credentials.forgetStoredGrant(connected.serverUrl)
    }

    /** The app came to the foreground: renew now if the cookie is missing or due. */
    fun onForeground() {
        foreground = true
        if (connection != null && prompt == null && !renewing && !signingIn) scheduleRenewal()
    }

    /** The app left the foreground: no renewal runs until it returns. */
    fun onBackground() {
        foreground = false
        cancelRenewalTimer()
    }

    /** The browser returned the redirect. */
    fun onBrowserCallback(callback: URI) = complete { credentials.completeSignIn(callback) }

    /** The callback receiver finished a sign-in the browser handed it, or failed with [error]. */
    fun onCallbackHandedOff(error: String?) {
        if (error != null) {
            stop()
            host.returnToSetup(error)
            return
        }
        resumeHandedOffSignIn()
    }

    /** Installs a sign-in the callback receiver completed; false when there is none. */
    fun resumeHandedOffSignIn(): Boolean {
        val completed = credentials.takeHandedOff() ?: return false
        complete { completed }
        return true
    }

    /** The browser closed without a redirect. */
    fun onBrowserClosed() {
        credentials.cancelSignIn()
        // Unknown after Android restarted the app: the relaunch connect is already on screen.
        val closed = browserSignIn ?: return
        val cause = OidcWebSession.cancelledSignInCause(closed.cause)
        stop()
        host.returnToSetup(cause?.message)
    }

    /** The browser rejected the redirect it was given. */
    fun onBrowserFailed() {
        credentials.cancelSignIn()
        val serverHost = browserSignIn?.connection?.host
        stop()
        host.returnToSetup(OidcSignInException.InvalidCallback(serverHost).message)
    }

    /**
     * Whether the shell takes over a main-frame navigation to [url]. `<mount>/auth/login` renews
     * silently instead of loading the identity provider, and `<mount>/auth/logout` signs out.
     */
    fun handlesNavigation(url: String?): Boolean {
        val connected = connection ?: return false
        when (OidcAuthRoute.of(url, connected.serverUrl)) {
            OidcAuthRoute.LOGIN -> onSignInRequested()
            OidcAuthRoute.LOGOUT -> signOut()
            null -> return false
        }
        return true
    }

    /** The page asked to sign in again, or the server redirected it to the identity provider. */
    fun onSignInRequested() {
        val connected = connection ?: return
        if (prompt != null || browserSignIn != null) return
        if (!guard.shouldRenew(now(), renewing)) {
            ask(
                Attempt(
                    connected,
                    page(connected),
                    OidcSignInException.SessionRejected(connected.host),
                ),
            )
            return
        }
        reloadRequested = true
        renew(connected)
    }

    /** Remembers the last app page, which a renewal the page asked for reloads. */
    fun onPageVisited(url: String?) {
        val connected = connection ?: return
        if (OidcWebSession.isPage(url, connected.serverUrl)) pageUrl = url
    }

    private fun ensureSession(
        target: OidcConnection,
        page: String,
        interactive: Boolean,
        id: Int,
    ) {
        val existing = OidcWebSession.cookieValue(cookies.get(meUrl(target)), target.cookieName)
        if (existing == null) {
            renewOrSignIn(target, page, interactive, id)
            return
        }
        io.execute {
            val accepted =
                runCatching {
                    credentials.isAccepted(
                        target.serverUrl,
                        target.cookieName,
                        existing,
                    )
                }
            main.execute {
                if (id != generation) return@execute
                accepted.fold(
                    { reused ->
                        if (reused) {
                            connected(
                                target,
                                page,
                            )
                        } else {
                            renewOrSignIn(target, page, interactive, id)
                        }
                    },
                    { error -> failConnect(Attempt(target, page, error), interactive) },
                )
            }
        }
    }

    private fun renewOrSignIn(
        target: OidcConnection,
        page: String,
        interactive: Boolean,
        id: Int,
    ) {
        host.showProgress(Progress.SIGNING_IN)
        renewCookie(target, id) { renewed ->
            renewed.fold(
                { connected(target, page) },
                { error ->
                    val failed = Attempt(target, page, error)
                    val canOpenBrowser =
                        interactive && error !is CancellationException &&
                            !OidcWebSession.isNetworkFailure(error)
                    if (canOpenBrowser) openBrowser(failed) else failConnect(failed, interactive)
                },
            )
        }
    }

    private fun openBrowser(attempt: Attempt) {
        prompt = null
        browserSignIn = attempt
        host.showProgress(Progress.SIGNING_IN)
        val launched =
            try {
                host.launchSignIn(attempt.connection.serverUrl, attempt.connection.cookieName)
            } catch (error: OidcSignInException) {
                browserSignIn = null
                failConnect(attempt.copy(cause = error), interactive = true)
                return
            }
        if (!launched) {
            browserSignIn = null
            val unavailable = OidcSignInException.BrowserUnavailable(attempt.connection.host)
            failConnect(attempt.copy(cause = unavailable), interactive = true)
        }
    }

    private fun complete(work: () -> OidcCredentials.CompletedSignIn) {
        supersede()
        val id = generation
        val signIn = browserSignIn
        completing = true
        host.showProgress(Progress.COMPLETING)
        io.execute {
            val result = runCatching(work)
            main.execute {
                if (id != generation) return@execute
                browserSignIn = null
                val completed =
                    result.getOrElse { error ->
                        stop()
                        host.returnToSetup(
                            if (error is CancellationException) null else error.message,
                        )
                        return@execute
                    }
                val target = OidcConnection(completed.serverUrl, completed.cookieName)
                val page =
                    signIn?.page?.takeIf { OidcWebSession.isPage(it, target.serverUrl) }
                        ?: target.serverUrl
                install(target, completed.session, id) { installedResult ->
                    completing = false
                    installedResult.fold(
                        { connected(target, page) },
                        { error -> failConnect(Attempt(target, page, error), interactive = true) },
                    )
                }
            }
        }
    }

    /** Renews the session from the stored grant and installs it. */
    private fun renewCookie(
        target: OidcConnection,
        id: Int,
        done: (Result<Unit>) -> Unit,
    ) {
        credentials.refresh(target.serverUrl).whenComplete { session, error ->
            main.execute {
                if (id != generation) return@execute
                if (error != null) {
                    done(Result.failure(unwrap(error)))
                } else {
                    install(target, session, id, done)
                }
            }
        }
    }

    /** Installs a session the server accepts, and reads it back so the page never loads first. */
    private fun install(
        target: OidcConnection,
        session: OidcSessionToken,
        id: Int,
        done: (Result<Unit>) -> Unit,
    ) {
        val rejected = OidcSignInException.SessionRejected(target.host)
        io.execute {
            val accepted =
                runCatching {
                    credentials.isAccepted(
                        target.serverUrl,
                        target.cookieName,
                        session.token,
                    )
                }
            main.execute {
                if (id != generation) return@execute
                accepted.exceptionOrNull()?.let {
                    done(Result.failure(it))
                    return@execute
                }
                if (accepted.getOrNull() != true) {
                    done(Result.failure(rejected))
                    return@execute
                }
                val cookie =
                    OidcWebSession.sessionCookie(
                        target.cookieName,
                        session,
                        target.serverUrl,
                    )
                cookies.set(target.origin, cookie) { stored ->
                    main.execute {
                        if (id != generation) return@execute
                        cookies.flush()
                        val readBack =
                            OidcWebSession.cookieValue(
                                cookies.get(meUrl(target)),
                                target.cookieName,
                            )
                        if (stored && readBack == session.token) {
                            installed = session
                            done(Result.success(Unit))
                        } else {
                            done(Result.failure(rejected))
                        }
                    }
                }
            }
        }
    }

    /**
     * Renews from the stored grant. A renewal the page asked for reloads it or asks to sign in;
     * a background one keeps the current cookie, retries an unreachable server, and remembers a
     * lost grant for the next prompt.
     */
    private fun renew(connected: OidcConnection) {
        if (renewing || prompt != null || signingIn) return
        cancelRenewalTimer()
        renewing = true
        renewCookie(connected, generation) { renewed ->
            renewing = false
            val reload = reloadRequested
            reloadRequested = false
            renewed.fold(
                {
                    renewalCause = null
                    scheduleRenewal()
                    if (reload) host.loadPage(page(connected))
                },
                { error ->
                    when {
                        reload -> {
                            failSession(connected, error)
                        }

                        OidcWebSession.isNetworkFailure(error) -> {
                            retryRenewal(connected)
                        }

                        else -> {
                            OidcWebSession.rememberedRenewalCause(error)?.let {
                                renewalCause =
                                    it
                            }
                        }
                    }
                },
            )
        }
    }

    /**
     * Renews before the cookie expires; a missing cookie renews now. A cookie whose expiry is
     * unknown gets no timer: the page's own sign-in request recovers it.
     */
    private fun scheduleRenewal() {
        cancelRenewalTimer()
        val connected = connection ?: return
        if (!foreground || prompt != null || renewing || signingIn) return
        val url = meUrl(connected)
        val value = OidcWebSession.cookieValue(cookies.get(url), connected.cookieName)
        val delay =
            if (value == null) {
                0L
            } else {
                val expiresAt =
                    cookies.expiry(url, connected.cookieName)
                        ?: installed?.takeIf { it.token == value }?.expiresAtEpochMillis
                        ?: return
                OidcWebSession.renewalDelay(expiresAt, now())
            }
        startTimer(delay) { renew(connected) }
    }

    /** Tries an unreachable server again later; in the background, [onForeground] does. */
    private fun retryRenewal(connected: OidcConnection) {
        if (!foreground) return
        startTimer(RETRY_DELAY_MS) { renew(connected) }
    }

    private fun startTimer(
        delayMillis: Long,
        task: () -> Unit,
    ) {
        cancelRenewalTimer()
        val id = generation
        cancelTimer =
            scheduler.schedule(delayMillis) {
                if (id != generation) return@schedule
                cancelTimer = null
                if (foreground) task()
            }
    }

    /**
     * Drops the work of the current generation: its results are ignored from now on, so its
     * renewal state and timer go too, or a dropped renewal would block every later one.
     */
    private fun supersede() {
        generation++
        renewing = false
        reloadRequested = false
        cancelRenewalTimer()
    }

    private fun cancelRenewalTimer() {
        cancelTimer?.invoke()
        cancelTimer = null
    }

    private fun connected(
        target: OidcConnection,
        page: String,
    ) {
        browserSignIn = null
        prompt = null
        connection = target
        pageUrl = page
        host.loadPage(page)
        scheduleRenewal()
    }

    /** A connect or sign-in that can't finish: ask to sign in, or return to setup. */
    private fun failConnect(
        attempt: Attempt,
        interactive: Boolean,
    ) {
        val error = attempt.cause
        if (!interactive && error !is CancellationException &&
            !OidcWebSession.isNetworkFailure(error)
        ) {
            ask(attempt)
        } else {
            stop()
            host.returnToSetup(if (error is CancellationException) null else error?.message)
        }
    }

    /** The connected session can't continue: a connection error returns to setup, else ask. */
    private fun failSession(
        connected: OidcConnection,
        error: Throwable,
    ) {
        if (OidcWebSession.isNetworkFailure(error) || error is CancellationException) {
            stop()
            host.returnToSetup(if (error is CancellationException) null else error.message)
            return
        }
        val cause = OidcWebSession.reauthenticationCause(error, renewalCause)
        renewalCause = null
        ask(Attempt(connected, page(connected), cause))
    }

    /** Asks to sign in; only **Sign In** opens the browser. */
    private fun ask(attempt: Attempt) {
        cancelRenewalTimer()
        browserSignIn = null
        prompt = attempt
        host.showSignInRequired(
            OidcWebSession.reauthenticationMessage(attempt.cause, attempt.connection.host),
        )
    }

    private fun page(connected: OidcConnection): String = pageUrl ?: connected.serverUrl

    private fun meUrl(target: OidcConnection): String =
        serverEndpoint(target.serverUrl, "/v1/me")?.toString() ?: target.serverUrl

    private fun unwrap(error: Throwable): Throwable {
        var current = error
        while ((current is CompletionException || current is ExecutionException) &&
            current.cause != null
        ) {
            current = current.cause!!
        }
        return current
    }

    private companion object {
        /** A background renewal that couldn't reach the server tries again after this. */
        const val RETRY_DELAY_MS = 30_000L
    }
}

/** Runs [task] on the main thread after a delay; the returned action cancels it. */
internal fun interface OidcScheduler {
    fun schedule(
        delayMillis: Long,
        task: () -> Unit,
    ): () -> Unit
}
