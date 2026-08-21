package ai.omnigent.android

import android.net.Uri
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

/**
 * The native (`exchange=post`) transport for [AuthTabFlow]: POST the
 * one-time code + state + PKCE verifier to the server's
 * `/auth/native-exchange` and receive the credential in the response
 * body, so it never appears in any URL. Used for servers a native
 * request can reach (oidc/accounts); front-door servers exchange through
 * a second Auth Tab hop instead.
 */
class NativeAuthExchange {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    /**
     * Exchange [grant] using [verifier]; [onResult] is invoked on the
     * main thread with the validated credential, or null on any failure
     * (network, non-200, malformed body, unsafe token).
     */
    fun exchange(
        grant: AuthTabFlow.Outcome.ExchangePost,
        onResult: (NativeAuth.Result?) -> Unit,
    ) {
        io.execute {
            val result = runCatching { post(grant) }.getOrNull()
            main.post { onResult(result) }
        }
    }

    /** Cancel outstanding work and release the host. Call from onDestroy. */
    fun shutdown() {
        io.shutdownNow()
    }

    private fun post(grant: AuthTabFlow.Outcome.ExchangePost): NativeAuth.Result? {
        val conn =
            URL("${grant.origin}/auth/native-exchange").openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.doOutput = true
        conn.instanceFollowRedirects = false // a front door's 302 is a failure, not a page
        conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
        conn.connectTimeout = HTTP_TIMEOUT_MS
        conn.readTimeout = HTTP_TIMEOUT_MS
        return try {
            val body =
                "code=${Uri.encode(grant.code)}&state=${Uri.encode(grant.state)}" +
                    "&code_verifier=${Uri.encode(grant.verifier)}"
            conn.outputStream.use { it.write(body.toByteArray(Charsets.US_ASCII)) }
            if (conn.responseCode != 200) return null
            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            val tokenType = json.optString("token_type").ifEmpty { return null }
            val token = json.optString("token").ifEmpty { return null }
            if (tokenType != NativeAuth.TOKEN_TYPE_SESSION &&
                tokenType != NativeAuth.TOKEN_TYPE_BEARER
            ) {
                return null
            }
            if (!NativeAuth.isTokenSafe(token)) return null
            NativeAuth.Result(grant.state, tokenType, token)
        } finally {
            conn.disconnect()
        }
    }

    private companion object {
        const val HTTP_TIMEOUT_MS = 10_000
    }
}
