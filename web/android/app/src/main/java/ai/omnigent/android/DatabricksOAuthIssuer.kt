package ai.omnigent.android

import org.json.JSONObject
import java.net.URI

/** OAuth authority returned by Databricks, distinct from the requested page origin. */
data class DatabricksOAuthIssuer(
    val uri: URI,
) {
    init {
        if (!isValid(uri)) throw DatabricksOAuthException.InvalidIssuer()
    }

    val discoveryUri: URI
        get() = URI("${uri.toString().trimEnd('/')}/.well-known/openid-configuration")

    val tokenEndpoint: URI
        get() = URI("${uri.toString().trimEnd('/')}/v1/token")

    fun validateDiscovery(body: ByteArray) {
        val document =
            try {
                JSONObject(body.toString(Charsets.UTF_8))
            } catch (_: Throwable) {
                throw DatabricksOAuthException.InvalidDiscovery()
            }
        if (
            document.optString("issuer") != uri.toString() ||
            document.optString("token_endpoint") != tokenEndpoint.toString()
        ) {
            throw DatabricksOAuthException.InvalidDiscovery()
        }
    }

    companion object {
        private const val ACCOUNT_PREFIX = "/oidc/accounts/"

        private fun isValid(uri: URI): Boolean {
            if (
                !uri.scheme.equals("https", ignoreCase = true) ||
                uri.host == null ||
                uri.rawUserInfo != null ||
                (uri.port != -1 && uri.port != 443) ||
                uri.rawQuery != null ||
                uri.rawFragment != null ||
                serverAuthentication("https://${uri.host}") !=
                ServerAuthentication.DATABRICKS_WORKSPACE
            ) {
                return false
            }
            val path = uri.rawPath
            if (path == "/oidc") return true
            if (!path.startsWith(ACCOUNT_PREFIX)) return false
            val accountId = path.removePrefix(ACCOUNT_PREFIX)
            return accountId.isNotEmpty() &&
                accountId.all { it.isLetterOrDigit() || it == '-' || it == '_' } &&
                accountId.all { it.code < 128 }
        }
    }
}
