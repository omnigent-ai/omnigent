package ai.omnigent.android

import java.net.URI
import java.net.URLDecoder
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

/** One independently stored Databricks workspace grant. */
data class DatabricksCredentialScope(
    val workspaceOrigin: URI,
    val clientId: String,
    val workspaceId: String?,
) {
    val account: String
        get() {
            val origin = workspaceOrigin.toString()
            val base = "${origin.toByteArray().size}:$origin$clientId"
            return workspaceId?.let { "o:${it.length}:$it:$base" } ?: base
        }

    companion object {
        fun from(
            workspaceUrl: URI,
            configuration: DatabricksOAuthConfiguration,
        ): DatabricksCredentialScope {
            if (
                !workspaceUrl.scheme.equals("https", ignoreCase = true) ||
                workspaceUrl.host == null ||
                workspaceUrl.rawUserInfo != null ||
                (workspaceUrl.port != -1 && workspaceUrl.port != 443) ||
                serverAuthentication("https://${workspaceUrl.host}") !=
                ServerAuthentication.DATABRICKS_WORKSPACE
            ) {
                throw DatabricksOAuthException.InvalidWorkspace()
            }
            val identifiers = queryItems(workspaceUrl).filter { it.first == "o" }
            if (identifiers.size > 1) throw DatabricksOAuthException.InvalidWorkspace()
            val workspaceId = identifiers.singleOrNull()?.second
            if (
                workspaceId != null &&
                (workspaceId.isEmpty() || !workspaceId.all { it in '0'..'9' })
            ) {
                throw DatabricksOAuthException.InvalidWorkspace()
            }
            val origin = URI("https", null, workspaceUrl.host.lowercase(), -1, null, null, null)
            return DatabricksCredentialScope(origin, configuration.clientId, workspaceId)
        }
    }
}

internal fun queryItems(uri: URI): List<Pair<String, String?>> {
    val query = uri.rawQuery ?: return emptyList()
    if (query.isEmpty()) return emptyList()
    return query.split('&').map { field ->
        val parts = field.split('=', limit = 2)
        decodeForm(parts[0]) to parts.getOrNull(1)?.let(::decodeForm)
    }
}

internal fun formEncode(value: String): String =
    URLEncoder.encode(value, StandardCharsets.UTF_8.name()).replace("+", "%20")

private fun decodeForm(value: String): String =
    try {
        URLDecoder.decode(value, StandardCharsets.UTF_8.name())
    } catch (_: IllegalArgumentException) {
        throw DatabricksOAuthException.InvalidWorkspace()
    }
