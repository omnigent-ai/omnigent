package ai.omnigent.android

import java.net.URI

/** Public-client configuration embedded in the Android build. */
data class DatabricksOAuthConfiguration(
    val clientId: String,
    val redirectUri: URI,
) {
    init {
        require(
            clientId.isNotEmpty() &&
                clientId.none { it.isWhitespace() || it.isISOControl() } &&
                !clientId.contains("$("),
        ) { "Configure a Databricks OAuth client ID for this build." }
        require(isValidRedirect(redirectUri)) {
            "Configure a valid HTTPS OAuth redirect URL for this build."
        }
    }

    fun matchesCallback(uri: URI): Boolean =
        uri.scheme.equals("https", ignoreCase = true) &&
            uri.host.equals(redirectUri.host, ignoreCase = true) &&
            effectivePort(uri) == effectivePort(redirectUri) &&
            uri.rawPath == redirectUri.rawPath &&
            uri.rawUserInfo == null &&
            uri.rawFragment == null

    companion object {
        fun fromBuildConfig(): DatabricksOAuthConfiguration =
            DatabricksOAuthConfiguration(
                clientId = BuildConfig.DATABRICKS_OAUTH_CLIENT_ID,
                redirectUri = URI(BuildConfig.DATABRICKS_OAUTH_REDIRECT_URL),
            )

        private fun isValidRedirect(uri: URI): Boolean =
            uri.scheme.equals("https", ignoreCase = true) &&
                !uri.host.isNullOrEmpty() &&
                uri.rawUserInfo == null &&
                (uri.port == -1 || uri.port == 443) &&
                !uri.rawPath.isNullOrEmpty() &&
                uri.rawQuery == null &&
                uri.rawFragment == null &&
                uri.toString().none { it.isWhitespace() || it.isISOControl() } &&
                !uri.toString().contains("$(")

        private fun effectivePort(uri: URI): Int = if (uri.port == -1) 443 else uri.port
    }
}

sealed class DatabricksOAuthException(
    message: String,
) : Exception(message) {
    class InvalidWorkspace :
        DatabricksOAuthException("Databricks OAuth requires an HTTPS workspace URL.")

    class InvalidCallback :
        DatabricksOAuthException("Databricks returned an invalid sign-in callback.")

    class InvalidIssuer :
        DatabricksOAuthException("Databricks returned an unsupported OAuth issuer.")

    class InvalidDiscovery :
        DatabricksOAuthException("Databricks returned inconsistent OAuth provider metadata.")

    class AuthorizationDenied : DatabricksOAuthException("Databricks sign-in was not authorized.")

    class AuthorizationFailed :
        DatabricksOAuthException("Databricks could not authorize this sign-in.")

    class NetworkUnavailable :
        DatabricksOAuthException("Could not reach Databricks. Please try again.")

    class TokenExchangeFailed :
        DatabricksOAuthException("Databricks could not complete sign-in. Please sign in again.")

    class InvalidRefreshGrant :
        DatabricksOAuthException("The saved Databricks session has expired. Please sign in again.")

    class InvalidTokenResponse :
        DatabricksOAuthException("Databricks returned an invalid token response.")
}
