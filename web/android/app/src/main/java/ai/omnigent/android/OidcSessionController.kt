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
 * connect opens the browser by itself; any other connect asks first. While connected, the page
 * asking to sign in renews silently and reloads it, and asking again within 15 s asks the user.
 *
 * Every method runs on the main thread; network work runs on [io] and comes back through [main].
 */
internal class OidcSessionController(
    private val host: Host,
    private val credentials: OidcCredentials,
    private val cookies: OidcCookieJar,
    private val io: Executor,
    private val main: Executor,
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
    }

    enum class Progress { CONNECTING, SIGNING_IN, COMPLETING }

    /** The connection whose session is installed; the shell then owns its lifecycle. */
    var connection: OidcConnection? = null
        private set

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
    }

    /** **Sign In** on the prompt. */
    fun signIn() {
        val prompted = prompt ?: return
        generation++
        openBrowser(prompted)
    }

    /** **Cancel** on the overlay; a sign-in a failed renewal opened keeps its reason. */
    fun cancel() {
        val cause = OidcWebSession.cancelledSignInCause(browserSignIn?.cause)
        if (browserSignIn != null) credentials.cancelSignIn()
        stop()
        host.returnToSetup(cause?.message)
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
     * silently instead of loading the identity provider in the web view.
     */
    fun handlesNavigation(url: String?): Boolean {
        val connected = connection ?: return false
        if (OidcAuthRoute.of(url, connected.serverUrl) != OidcAuthRoute.LOGIN) return false
        onSignInRequested()
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
                    if (interactive && error !is CancellationException &&
                        !OidcWebSession.isNetworkFailure(error)
                    ) {
                        openBrowser(failed)
                    } else {
                        failConnect(failed, interactive)
                    }
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
        generation++
        val id = generation
        val signIn = browserSignIn
        host.showProgress(Progress.COMPLETING)
        io.execute {
            val result = runCatching(work)
            main.execute {
                if (id != generation) return@execute
                browserSignIn = null
                val completed =
                    result.getOrElse { error ->
                        stop()
                        host.returnToSetup(error.message)
                        return@execute
                    }
                val target = OidcConnection(completed.serverUrl, completed.cookieName)
                val page =
                    signIn?.page?.takeIf { OidcWebSession.isPage(it, target.serverUrl) }
                        ?: target.serverUrl
                install(target, completed.session, id) { installed ->
                    installed.fold(
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
                        val installed =
                            OidcWebSession.cookieValue(
                                cookies.get(meUrl(target)),
                                target.cookieName,
                            )
                        done(
                            if (stored &&
                                installed == session.token
                            ) {
                                Result.success(Unit)
                            } else {
                                Result.failure(rejected)
                            },
                        )
                    }
                }
            }
        }
    }

    private fun renew(connected: OidcConnection) {
        if (renewing) return
        renewing = true
        renewCookie(connected, generation) { renewed ->
            renewing = false
            val reload = reloadRequested
            reloadRequested = false
            renewed.fold(
                { if (reload) host.loadPage(page(connected)) },
                { error -> if (reload) failSession(connected, error) },
            )
        }
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
        ask(Attempt(connected, page(connected), error))
    }

    /** Asks to sign in; only **Sign In** opens the browser. */
    private fun ask(attempt: Attempt) {
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
}
