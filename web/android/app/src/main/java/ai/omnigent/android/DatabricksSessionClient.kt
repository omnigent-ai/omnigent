package ai.omnigent.android

import java.net.HttpCookie
import java.net.HttpURLConnection
import java.net.URI

internal data class DatabricksWebContext(
    val configuration: DatabricksOAuthConfiguration,
    val scope: DatabricksCredentialScope,
    val pageUri: URI,
) {
    val profileName: String
        get() = "omnigent-databricks-${sha256Hex("web-profile-v1:${scope.account}")}".take(80)

    companion object {
        fun resolve(uri: URI): DatabricksWebContext? {
            if (serverAuthentication(originOf(uri.toString())) !=
                ServerAuthentication.DATABRICKS_WORKSPACE
            ) {
                return null
            }
            val configuration = DatabricksOAuthConfiguration.fromBuildConfig()
            val scope = DatabricksCredentialScope.from(uri, configuration)
            val page = URI(databricksWorkspaceUiUrl(uri.toString()) ?: uri.toString())
            if (DatabricksWebSession.isLoginPath(page.path)) {
                throw DatabricksOAuthException.InvalidWorkspace()
            }
            return DatabricksWebContext(configuration, scope, page)
        }
    }
}

internal data class DatabricksWebSession(
    val pageUri: URI,
    val cookies: List<SessionCookie>,
    val allowedOrigins: Set<String>,
    val configuration: DatabricksOAuthConfiguration,
    val workspaceId: String?,
) {
    fun navigationUri(uri: URI): URI? {
        val scope =
            runCatching { DatabricksCredentialScope.from(uri, configuration) }.getOrNull()
                ?: return null
        if (scope.workspaceOrigin.toString() !in allowedOrigins) return null
        if (scope.workspaceId != null && workspaceId != null &&
            scope.workspaceId != workspaceId
        ) {
            return null
        }
        if (isLoginPath(uri.path)) return null
        return uri
    }

    fun isAuthenticationUri(uri: URI): Boolean =
        runCatching { DatabricksCredentialScope.from(uri, configuration) }.isSuccess &&
            isLoginPath(uri.path)

    companion object {
        fun isLoginPath(path: String): Boolean =
            path == "/login" ||
                path == "/login.html" ||
                path.startsWith("/login/") ||
                path.startsWith("/oidc/") ||
                path == "/auth/login" ||
                path == "/auth/callback" ||
                path == "/auth/session/create"
    }
}

internal data class SessionCookie(
    val name: String,
    val value: String,
    val domain: String,
    val path: String,
    val secure: Boolean,
    val httpOnly: Boolean,
    val hostOnly: Boolean,
    val maxAgeSeconds: Long,
    val sourceUri: URI,
    val setCookieHeader: String,
) {
    val isDeletion: Boolean
        get() = value.isEmpty() || maxAgeSeconds == 0L

    fun appliesTo(uri: URI): Boolean {
        val host = uri.host?.lowercase() ?: return false
        val matchesHost =
            if (hostOnly) {
                host == domain
            } else {
                host == domain ||
                    host.endsWith(".$domain")
            }
        val targetPath = uri.path.takeUnless(String::isEmpty) ?: "/"
        val matchesPath =
            targetPath == path || targetPath.startsWith(if (path.endsWith('/')) path else "$path/")
        return matchesHost && matchesPath && (!secure || uri.scheme == "https") && !isDeletion
    }
}

internal data class SessionHttpRequest(
    val uri: URI,
    val headers: Map<String, String>,
)

internal data class SessionHttpResponse(
    val uri: URI,
    val status: Int,
    val headers: Map<String, List<String>>,
)

internal fun interface SessionTransport {
    fun execute(request: SessionHttpRequest): SessionHttpResponse
}

internal class UrlConnectionSessionTransport : SessionTransport {
    override fun execute(request: SessionHttpRequest): SessionHttpResponse {
        val connection = request.uri.toURL().openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.useCaches = false
        connection.connectTimeout = 20_000
        connection.readTimeout = 20_000
        request.headers.forEach(connection::setRequestProperty)
        return try {
            val status = connection.responseCode
            SessionHttpResponse(
                connection.url.toURI(),
                status,
                connection.headerFields.filterKeys { it != null }.mapKeys { it.key!! },
            )
        } catch (_: Throwable) {
            throw DatabricksSessionException.NetworkUnavailable()
        } finally {
            connection.disconnect()
        }
    }
}

internal fun interface DatabricksSessionCreating {
    fun create(
        context: DatabricksWebContext,
        tokens: DatabricksOAuthTokens,
    ): DatabricksWebSession
}

internal class DatabricksSessionClient(
    private val transport: SessionTransport = UrlConnectionSessionTransport(),
) : DatabricksSessionCreating {
    override fun create(
        context: DatabricksWebContext,
        tokens: DatabricksOAuthTokens,
    ): DatabricksWebSession {
        if (!tokens.isValid || DatabricksWebSession.isLoginPath(context.pageUri.path)) {
            throw DatabricksSessionException.InvalidResponse()
        }
        if (tokens.issuer
                ?.uri
                ?.path
                ?.startsWith("/oidc/accounts/") == true &&
            context.scope.workspaceId == null
        ) {
            throw DatabricksSessionException.WorkspaceRequired()
        }
        val exchangeOrigin = exchangeOrigin(context, tokens)
        val nextPath = context.pageUri.rawPath + (context.pageUri.rawQuery?.let { "?$it" } ?: "")
        var current = URI("$exchangeOrigin/auth/session/create?next_url=${formEncode(nextPath)}")
        val jar = mutableListOf<SessionCookie>()
        val updates = mutableListOf<SessionCookie>()
        var workspaceId =
            context.scope.workspaceId ?: scopeOf(context.pageUri, context)?.workspaceId
        repeat(MAX_REDIRECTS + 1) { hop ->
            val headers = mutableMapOf<String, String>()
            if (hop == 0) headers["Authorization"] = "Bearer ${tokens.accessToken}"
            jar
                .filter { it.appliesTo(current) }
                .sortedByDescending { it.path.length }
                .takeIf(List<SessionCookie>::isNotEmpty)
                ?.let { cookies ->
                    headers["Cookie"] = cookies.joinToString("; ") { "${it.name}=${it.value}" }
                }
            val response = transport.execute(SessionHttpRequest(current, headers))
            if (response.uri != current) throw DatabricksSessionException.InvalidResponse()
            parseCookies(response).forEach { cookie ->
                updates.removeAll { it.identity == cookie.identity }
                updates += cookie
                jar.removeAll { it.identity == cookie.identity }
                if (!cookie.isDeletion) jar += cookie
            }
            if (response.status in REDIRECT_STATUSES) {
                if (hop >= MAX_REDIRECTS) throw DatabricksSessionException.UnsafeRedirect()
                val location =
                    response.header("Location") ?: throw DatabricksSessionException.UnsafeRedirect()
                val next = current.resolve(location)
                val nextScope =
                    scopeOf(next, context) ?: throw DatabricksSessionException.UnsafeRedirect()
                nextScope.workspaceId?.let { nextId ->
                    if (workspaceId != null && workspaceId != nextId) {
                        throw DatabricksSessionException.WorkspaceChanged()
                    }
                    workspaceId = nextId
                }
                current =
                    URI("https", null, next.host.lowercase(), -1, next.path, next.rawQuery, null)
                return@repeat
            }
            if (response.status !in
                200..299
            ) {
                throw DatabricksSessionException.Rejected(response.status)
            }
            val knownOrigin =
                originOf(current.toString()) in
                    setOf(
                        context.scope.workspaceOrigin.toString(),
                        exchangeOrigin.toString(),
                        originOf(context.pageUri.toString()),
                    )
            val atAppPath =
                current.path in
                    setOf(context.pageUri.path, WORKSPACE_UI_PATH, "/auth/session/create")
            if (!atAppPath && !(knownOrigin && (current.path.isEmpty() || current.path == "/"))) {
                throw DatabricksSessionException.UnexpectedLanding()
            }
            val pageBuilder =
                StringBuilder(
                    originOf(current.toString())!!,
                ).append(context.pageUri.rawPath)
            val query = mutableListOf<String>()
            context.pageUri.rawQuery
                ?.takeIf(String::isNotEmpty)
                ?.let(query::add)
            if (context.scope.workspaceId == null && workspaceId != null &&
                queryItems(context.pageUri).none { it.first == "o" }
            ) {
                query += "o=${formEncode(workspaceId!!)}"
            }
            if (query.isNotEmpty()) pageBuilder.append('?').append(query.joinToString("&"))
            context.pageUri.rawFragment?.let { pageBuilder.append('#').append(it) }
            val pageUri = URI(pageBuilder.toString())
            if (jar.none {
                    it.name == "DBAUTH" && it.value.isNotEmpty() &&
                        it.appliesTo(
                            pageUri,
                        )
                }
            ) {
                throw DatabricksSessionException.MissingCookie()
            }
            return DatabricksWebSession(
                pageUri,
                updates,
                setOfNotNull(
                    context.scope.workspaceOrigin.toString(),
                    exchangeOrigin.toString(),
                    originOf(context.pageUri.toString()),
                    originOf(pageUri.toString()),
                ),
                context.configuration,
                workspaceId,
            )
        }
        throw DatabricksSessionException.UnsafeRedirect()
    }

    private fun exchangeOrigin(
        context: DatabricksWebContext,
        tokens: DatabricksOAuthTokens,
    ): URI {
        val issuer = tokens.issuer ?: return context.scope.workspaceOrigin
        if (issuer.uri.path != "/oidc") return context.scope.workspaceOrigin
        return runCatching {
            DatabricksCredentialScope.from(issuer.uri, context.configuration).workspaceOrigin
        }.getOrDefault(context.scope.workspaceOrigin)
    }

    private fun scopeOf(
        uri: URI,
        context: DatabricksWebContext,
    ): DatabricksCredentialScope? =
        runCatching { DatabricksCredentialScope.from(uri, context.configuration) }.getOrNull()

    companion object {
        private const val MAX_REDIRECTS = 8
        private val REDIRECT_STATUSES = setOf(301, 302, 303, 307, 308)

        internal fun parseCookies(response: SessionHttpResponse): List<SessionCookie> {
            if (response.uri.scheme != "https" || !isDatabricksWorkspaceHost(response.uri.host)) {
                throw DatabricksSessionException.InvalidResponse()
            }
            return response.headers.entries
                .filter { it.key.equals("Set-Cookie", ignoreCase = true) }
                .flatMap { it.value }
                .map { header -> parseCookie(response.uri, header) }
        }

        private fun parseCookie(
            source: URI,
            header: String,
        ): SessionCookie {
            val parsed =
                runCatching { HttpCookie.parse(header).single() }.getOrNull()
                    ?: throw DatabricksSessionException.UnsafeCookie()
            val host = source.host.lowercase()
            val explicitDomain = parsed.domain?.trimStart('.')?.lowercase()
            val domain = explicitDomain ?: host
            val hostOnly = explicitDomain == null
            if (
                !isDatabricksWorkspaceHost(domain) ||
                !(host == domain || (!hostOnly && host.endsWith(".$domain"))) ||
                parsed.name.isEmpty() ||
                parsed.name.any(Char::isISOControl) ||
                parsed.value.any(Char::isISOControl)
            ) {
                throw DatabricksSessionException.UnsafeCookie()
            }
            val path = parsed.path?.takeIf { it.startsWith('/') } ?: defaultCookiePath(source.path)
            val cookie =
                SessionCookie(
                    parsed.name,
                    parsed.value,
                    domain,
                    path,
                    parsed.secure,
                    parsed.isHttpOnly,
                    hostOnly,
                    parsed.maxAge,
                    source,
                    header,
                )
            if (cookie.name == "DBAUTH" && !cookie.isDeletion &&
                !(cookie.secure && cookie.httpOnly)
            ) {
                throw DatabricksSessionException.UnsafeCookie()
            }
            return cookie
        }

        private fun defaultCookiePath(path: String): String {
            if (!path.startsWith('/') || path == "/") return "/"
            return path.substringBeforeLast('/').ifEmpty { "/" }
        }
    }
}

private val SessionCookie.identity: Triple<String, String, String>
    get() = Triple(name, domain, path)

private fun SessionHttpResponse.header(name: String): String? =
    headers.entries
        .firstOrNull { it.key.equals(name, ignoreCase = true) }
        ?.value
        ?.singleOrNull()

internal sealed class DatabricksSessionException(
    message: String,
) : Exception(message) {
    class InvalidResponse :
        DatabricksSessionException("Databricks returned an invalid session response.")

    class UnsafeRedirect :
        DatabricksSessionException("Databricks returned an unsupported session redirect.")

    class WorkspaceChanged : DatabricksSessionException("The workspace changed. Select it again.")

    class UnsafeCookie : DatabricksSessionException("Databricks returned an unsafe session cookie.")

    class MissingCookie :
        DatabricksSessionException("Databricks did not create a workspace session cookie.")

    class UnexpectedLanding :
        DatabricksSessionException("Databricks did not finish loading the workspace session.")

    class NetworkUnavailable :
        DatabricksSessionException("Could not reach Databricks. Please try again.")

    class Rejected(
        val status: Int,
    ) : DatabricksSessionException("Databricks rejected session creation (HTTP $status).")

    class WorkspaceRequired :
        DatabricksSessionException("Enter a workspace-specific URL including its o parameter.")

    class ReauthenticationRequired :
        DatabricksSessionException("Sign in to continue to this workspace.")

    class UnsupportedWebView :
        DatabricksSessionException("Update Android System WebView to connect securely.")
}

private fun sha256Hex(value: String): String =
    java.security.MessageDigest
        .getInstance("SHA-256")
        .digest(value.toByteArray())
        .joinToString("") { "%02x".format(it.toInt() and 0xff) }
