package ai.omnigent.android

import android.net.Uri
import android.util.Base64
import java.security.SecureRandom

/**
 * Tracks the single in-flight Auth Tab login and binds every callback to
 * the flow that started it: a callback is accepted if its `state` nonce
 * matches the pending flow AND the pinned origin is still the one
 * the flow was launched for. A result from a previous server (switched
 * away mid-login) or an unsolicited callback can therefore never
 * advance a login.
 *
 * The flow is two-legged (see [NativeAuth]): leg 1 returns a one-time
 * code, leg 2 exchanges it with the PKCE verifier held here. The POST
 * transport keeps the verifier in a request body; the front-door tab
 * transport must put code + verifier in its second-hop URL, where browser
 * history and browser/proxy diagnostics may observe them. The final
 * header-mode token redirect has the same diagnostic exposure; the server
 * validates every binding before consuming the code.
 *
 * Main-thread confined, like the rest of the login state in
 * [MainActivity].
 */
class AuthTabFlow {
    private data class Pending(
        val state: String,
        val origin: String,
        val verifier: String,
        var codeIssued: Boolean = false,
    )

    /** What a callback means for the caller. */
    sealed interface Outcome {
        /** Leg 1 done; open [url] in a second Auth Tab to exchange. */
        data class LaunchExchangeTab(
            val url: Uri,
        ) : Outcome

        /** Leg 1 done; exchange these fields with a native POST. */
        data class ExchangePost(
            val origin: String,
            val code: String,
            val state: String,
            val verifier: String,
        ) : Outcome

        /** The flow finished with a credential (flow cleared). */
        data class Complete(
            val result: NativeAuth.Result,
        ) : Outcome
    }

    private val random = SecureRandom()
    private var pending: Pending? = null

    /** True while a flow is awaiting a callback or exchange. */
    val inFlight: Boolean get() = pending != null

    /**
     * Start a flow against [origin] (the pinned server). Returns the URL
     * to open in the Auth Tab, or null when a flow is already in flight
     * (a redirect storm must not stack tabs).
     */
    fun begin(
        origin: String,
        clientPackage: String,
    ): Uri? {
        if (pending != null) return null
        val state = randomUrlSafe()
        val verifier = randomUrlSafe(32)
        pending = Pending(state, origin, verifier)
        return NativeAuth.completionUrl(
            origin,
            state,
            NativeAuth.deriveCodeChallenge(verifier),
            clientPackage,
        )
    }

    /**
     * Advance the pending flow with a callback [uri]. Returns what to do
     * next, or null — leaving any pending flow armed — when the callback
     * doesn't bind: wrong shape for the current leg, state mismatch, or
     * [currentOrigin] no longer the flow's launch origin. The *caller*
     * decides whether an unmatched callback abandons the flow; results
     * delivered through the Auth Tab launcher do (the tab is gone), while
     * a spurious callback must stay inert.
     */
    fun handleCallback(
        uri: Uri?,
        currentOrigin: String?,
    ): Outcome? {
        val flow = pending ?: return null
        if (currentOrigin == null || currentOrigin != flow.origin) return null

        if (!flow.codeIssued) {
            val grant = NativeAuth.parseCodeCallback(uri, flow.origin) ?: return null
            if (grant.state != flow.state) return null
            flow.codeIssued = true
            return if (grant.exchange == NativeAuth.EXCHANGE_TAB) {
                Outcome.LaunchExchangeTab(
                    NativeAuth.exchangeUrl(flow.origin, grant, flow.verifier),
                )
            } else {
                Outcome.ExchangePost(flow.origin, grant.code, grant.state, flow.verifier)
            }
        }

        val result = NativeAuth.parseTokenCallback(uri, flow.origin) ?: return null
        if (result.state != flow.state) return null
        pending = null
        return Outcome.Complete(result)
    }

    /** Abandon the pending flow (tab dismissed, exchange failed, server switch). */
    fun cancel() {
        pending = null
    }

    /** A fresh URL-safe nonce (base64url alphabet, no padding). */
    private fun randomUrlSafe(byteCount: Int = 16): String {
        val bytes = ByteArray(byteCount)
        random.nextBytes(bytes)
        return Base64.encodeToString(
            bytes,
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING,
        )
    }
}
