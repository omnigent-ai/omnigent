package ai.omnigent.android

import java.net.URI

/** What one probe of a workspace web session learned. */
internal sealed interface WorkspaceSessionCheck {
    /** The workspace accepted the session. */
    data object Valid : WorkspaceSessionCheck

    /**
     * The workspace no longer accepts the session. [status] is 403 for a denied
     * workspace; null means the session expired (401 or a redirect to sign-in).
     */
    data class Rejected(
        val status: Int?,
    ) : WorkspaceSessionCheck

    /** No verdict (network failure or an unrelated answer); the session is kept. */
    data object Unknown : WorkspaceSessionCheck
}

/**
 * Checks that a workspace `DBAUTH` session still authenticates with one GET to
 * the SPA's `/v1/me`, the identity probe the web app already uses. The cookie is
 * sent natively because the probe runs when the activity resumes, before the
 * page has a chance to notice anything.
 */
internal class WorkspaceSessionProbe(
    private val transport: SessionTransport = UrlConnectionSessionTransport(),
) {
    /**
     * @param pageUri Any workspace page URI; only its scheme and authority are used.
     * @param dbauthValue The current `DBAUTH` cookie value for that workspace.
     */
    fun check(
        pageUri: URI,
        dbauthValue: String,
    ): WorkspaceSessionCheck {
        val identityUri =
            URI(pageUri.scheme, pageUri.authority, "$WORKSPACE_UI_PATH/v1/me", null, null)
        val request =
            SessionHttpRequest(
                uri = identityUri,
                headers = mapOf("Cookie" to "DBAUTH=$dbauthValue", "Accept" to "application/json"),
            )
        val response =
            try {
                transport.execute(request)
            } catch (_: DatabricksSessionException) {
                return WorkspaceSessionCheck.Unknown
            }
        return when {
            response.status in 200..299 -> WorkspaceSessionCheck.Valid
            response.status == 401 -> WorkspaceSessionCheck.Rejected(null)
            response.status == 403 -> WorkspaceSessionCheck.Rejected(403)
            response.status in REDIRECT_STATUSES && redirectsToSignIn(request.uri, response) ->
                WorkspaceSessionCheck.Rejected(null)
            else -> WorkspaceSessionCheck.Unknown
        }
    }

    private fun redirectsToSignIn(
        requested: URI,
        response: SessionHttpResponse,
    ): Boolean {
        val location =
            response.headers.entries
                .firstOrNull { it.key.equals("Location", ignoreCase = true) }
                ?.value
                ?.singleOrNull()
                ?: return false
        val target = runCatching { requested.resolve(location) }.getOrNull() ?: return false
        return DatabricksWebSession.isLoginPath(target.path)
    }

    private companion object {
        val REDIRECT_STATUSES = setOf(301, 302, 303, 307, 308)
    }
}
