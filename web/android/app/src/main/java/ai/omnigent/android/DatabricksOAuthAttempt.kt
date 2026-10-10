package ai.omnigent.android

import org.json.JSONObject
import java.net.URI
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/** Transient material for one authorization-code flow. */
data class DatabricksOAuthAttempt(
    val configuration: DatabricksOAuthConfiguration,
    val credentialScope: DatabricksCredentialScope,
    val state: String,
    val verifier: String,
) {
    val authorizationUri: URI
        get() {
            val fields =
                mutableListOf(
                    "client_id" to configuration.clientId,
                    "redirect_uri" to configuration.redirectUri.toString(),
                    "response_type" to "code",
                    "scope" to SCOPE,
                    "state" to state,
                    "code_challenge" to challenge(verifier),
                    "code_challenge_method" to "S256",
                )
            credentialScope.workspaceId?.let { fields += "o" to it }
            val query = fields.joinToString("&") { (key, value) -> "$key=${formEncode(value)}" }
            return URI("${credentialScope.workspaceOrigin}/oidc/v1/authorize?$query")
        }

    fun authorizationResponse(callback: URI): AuthorizationResponse {
        if (!configuration.matchesCallback(callback)) {
            throw DatabricksOAuthException.InvalidCallback()
        }
        val items =
            try {
                queryItems(callback)
            } catch (_: Throwable) {
                throw DatabricksOAuthException.InvalidCallback()
            }
        val states = items.filter { it.first == "state" }
        val codes = items.filter { it.first == "code" }
        val errors = items.filter { it.first == "error" }
        if (states.size != 1 || states.single().second != state) {
            throw DatabricksOAuthException.InvalidCallback()
        }
        if (errors.isNotEmpty()) {
            if (errors.size != 1 || codes.isNotEmpty() || errors.single().second.isNullOrEmpty()) {
                throw DatabricksOAuthException.InvalidCallback()
            }
            if (errors.single().second == "access_denied") {
                throw DatabricksOAuthException.AuthorizationDenied()
            }
            throw DatabricksOAuthException.AuthorizationFailed()
        }
        val code = codes.singleOrNull()?.second
        if (code.isNullOrEmpty()) throw DatabricksOAuthException.InvalidCallback()
        val issuers = items.filter { it.first == "iss" }
        val issuer =
            if (issuers.isEmpty()) {
                DatabricksOAuthIssuer(URI("${credentialScope.workspaceOrigin}/oidc"))
            } else {
                val value = issuers.singleOrNull()?.second
                if (value.isNullOrEmpty()) throw DatabricksOAuthException.InvalidIssuer()
                try {
                    DatabricksOAuthIssuer(URI(value))
                } catch (error: DatabricksOAuthException.InvalidIssuer) {
                    throw error
                } catch (_: Throwable) {
                    throw DatabricksOAuthException.InvalidIssuer()
                }
            }
        return AuthorizationResponse(code, issuer)
    }

    data class AuthorizationResponse(
        val code: String,
        val issuer: DatabricksOAuthIssuer,
    )

    companion object {
        private const val SCOPE = "all-apis offline_access"
        private val secureRandom = SecureRandom()

        fun create(
            workspaceUrl: URI,
            configuration: DatabricksOAuthConfiguration,
            randomBytes: (Int) -> ByteArray = ::secureRandomBytes,
        ): DatabricksOAuthAttempt =
            DatabricksOAuthAttempt(
                configuration = configuration,
                credentialScope = DatabricksCredentialScope.from(workspaceUrl, configuration),
                state = mobileRedirectState(base64Url(randomBytes(32))),
                verifier = base64Url(randomBytes(32)),
            )

        fun challenge(verifier: String): String =
            base64Url(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()))

        fun mobileRedirectState(nonce: String): String {
            val document =
                JSONObject()
                    .put("scheme", MOBILE_REDIRECT_SCHEME)
                    .put("nonce", nonce)
                    .toString()
            return Base64.getEncoder().encodeToString(document.toByteArray())
        }

        const val MOBILE_REDIRECT_SCHEME = "ai.omnigent.android"

        private fun secureRandomBytes(size: Int): ByteArray =
            ByteArray(size).also(secureRandom::nextBytes)

        private fun base64Url(bytes: ByteArray): String =
            Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }
}
