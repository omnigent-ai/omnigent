package ai.omnigent.android

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI

/** Opaque OAuth grant plus the verified authority required to refresh it. */
data class DatabricksOAuthTokens(
    val accessToken: String,
    val refreshToken: String,
    val expiresAtEpochMillis: Long,
    val issuer: DatabricksOAuthIssuer?,
) {
    val isValid: Boolean
        get() =
            accessToken.isSafeTokenValue() &&
                refreshToken.isSafeTokenValue() &&
                expiresAtEpochMillis > 0
}

internal data class OAuthHttpRequest(
    val uri: URI,
    val method: String = "GET",
    val headers: Map<String, String> = emptyMap(),
    val body: ByteArray? = null,
)

internal data class OAuthHttpResponse(
    val uri: URI,
    val status: Int,
    val body: ByteArray,
)

internal fun interface OAuthTransport {
    fun execute(request: OAuthHttpRequest): OAuthHttpResponse
}

/** Redirect-disabled, cookie-independent native OAuth transport. */
internal class UrlConnectionOAuthTransport : OAuthTransport {
    override fun execute(request: OAuthHttpRequest): OAuthHttpResponse {
        val connection = request.uri.toURL().openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.useCaches = false
        connection.connectTimeout = TIMEOUT_MS
        connection.readTimeout = TIMEOUT_MS
        connection.requestMethod = request.method
        request.headers.forEach(connection::setRequestProperty)
        request.body?.let { body ->
            connection.doOutput = true
            connection.setFixedLengthStreamingMode(body.size)
            connection.outputStream.use { it.write(body) }
        }
        return try {
            val status = connection.responseCode
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            OAuthHttpResponse(
                uri = connection.url.toURI(),
                status = status,
                body = stream?.use { it.readBytes() } ?: byteArrayOf(),
            )
        } catch (_: Throwable) {
            throw DatabricksOAuthException.NetworkUnavailable()
        } finally {
            connection.disconnect()
        }
    }

    private companion object {
        const val TIMEOUT_MS = 30_000
    }
}

class DatabricksOAuthClient internal constructor(
    private val transport: OAuthTransport = UrlConnectionOAuthTransport(),
    private val now: () -> Long = System::currentTimeMillis,
) {
    fun exchange(
        code: String,
        attempt: DatabricksOAuthAttempt,
        issuer: DatabricksOAuthIssuer,
    ): DatabricksOAuthTokens {
        val discovery =
            send(
                OAuthHttpRequest(
                    uri = issuer.discoveryUri,
                    headers = mapOf("Accept" to "application/json"),
                ),
            )
        if (discovery.status != 200) throw DatabricksOAuthException.InvalidDiscovery()
        issuer.validateDiscovery(discovery.body)
        return requestTokens(
            tokenRequest(
                scope = attempt.credentialScope,
                fields =
                    listOf(
                        "grant_type" to "authorization_code",
                        "redirect_uri" to attempt.configuration.redirectUri.toString(),
                        "scope" to "all-apis offline_access",
                        "code_verifier" to attempt.verifier,
                        "code" to code,
                    ),
                issuer = issuer,
            ),
            issuer = issuer,
        )
    }

    fun refresh(
        refreshToken: String,
        scope: DatabricksCredentialScope,
        issuer: DatabricksOAuthIssuer?,
    ): DatabricksOAuthTokens =
        requestTokens(
            tokenRequest(
                scope = scope,
                fields =
                    listOf(
                        "grant_type" to "refresh_token",
                        "refresh_token" to refreshToken,
                    ),
                issuer = issuer,
            ),
            previousRefreshToken = refreshToken,
            issuer = issuer,
        )

    private fun requestTokens(
        request: OAuthHttpRequest,
        previousRefreshToken: String? = null,
        issuer: DatabricksOAuthIssuer? = null,
    ): DatabricksOAuthTokens {
        val requestedAt = now()
        val response = send(request)
        if (
            previousRefreshToken != null &&
            response.status == 400 &&
            runCatching {
                JSONObject(response.body.toString(Charsets.UTF_8)).optString("error")
            }.getOrNull() == "invalid_grant"
        ) {
            throw DatabricksOAuthException.InvalidRefreshGrant()
        }
        if (response.status != 200) throw DatabricksOAuthException.TokenExchangeFailed()
        return tokens(response.body, requestedAt, previousRefreshToken, issuer)
    }

    private fun send(request: OAuthHttpRequest): OAuthHttpResponse {
        val response = transport.execute(request)
        if (response.uri != request.uri) throw DatabricksOAuthException.TokenExchangeFailed()
        return response
    }

    companion object {
        internal fun tokenRequest(
            scope: DatabricksCredentialScope,
            fields: List<Pair<String, String>>,
            issuer: DatabricksOAuthIssuer?,
        ): OAuthHttpRequest {
            val endpoint = issuer?.tokenEndpoint ?: URI("${scope.workspaceOrigin}/oidc/v1/token")
            val body =
                (listOf("client_id" to scope.clientId) + fields)
                    .joinToString("&") { (key, value) -> "$key=${formEncode(value)}" }
                    .toByteArray()
            return OAuthHttpRequest(
                uri = endpoint,
                method = "POST",
                headers =
                    mapOf(
                        "Content-Type" to "application/x-www-form-urlencoded",
                        "Accept" to "application/json",
                    ),
                body = body,
            )
        }

        internal fun tokens(
            body: ByteArray,
            requestedAtEpochMillis: Long,
            previousRefreshToken: String? = null,
            issuer: DatabricksOAuthIssuer? = null,
        ): DatabricksOAuthTokens {
            val document =
                try {
                    JSONObject(body.toString(Charsets.UTF_8))
                } catch (_: Throwable) {
                    throw DatabricksOAuthException.InvalidTokenResponse()
                }
            val accessToken = document.optString("access_token")
            val refreshToken =
                if (document.has("refresh_token")) {
                    document.optString("refresh_token")
                } else {
                    previousRefreshToken
                }
            val expiresInSeconds = document.optDouble("expires_in", Double.NaN)
            if (
                !document.optString("token_type").equals("bearer", ignoreCase = true) ||
                refreshToken == null ||
                !expiresInSeconds.isFinite() ||
                expiresInSeconds <= 0
            ) {
                throw DatabricksOAuthException.InvalidTokenResponse()
            }
            val expiresAt =
                requestedAtEpochMillis +
                    runCatching { Math.multiplyExact(expiresInSeconds.toLong(), 1_000L) }
                        .getOrElse { throw DatabricksOAuthException.InvalidTokenResponse() }
            val tokens = DatabricksOAuthTokens(accessToken, refreshToken, expiresAt, issuer)
            if (!tokens.isValid) throw DatabricksOAuthException.InvalidTokenResponse()
            return tokens
        }
    }
}

private fun String.isSafeTokenValue(): Boolean =
    isNotEmpty() && none { it.isWhitespace() || it.isISOControl() }
