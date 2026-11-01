package ai.omnigent.android

internal sealed interface ConnectionStatus {
    val host: String

    data class Connecting(
        override val host: String,
    ) : ConnectionStatus

    data class WorkspaceConnecting(
        override val host: String,
    ) : ConnectionStatus

    data class SigningIn(
        override val host: String,
    ) : ConnectionStatus

    data class CompletingSignIn(
        override val host: String,
        val workspace: Boolean,
    ) : ConnectionStatus

    data class AuthTabHandoff(
        override val host: String,
    ) : ConnectionStatus

    data class SigningOut(
        override val host: String,
    ) : ConnectionStatus

    data class SignInRequired(
        override val host: String,
        val message: String?,
        val workspace: Boolean,
    ) : ConnectionStatus

    data class ServerUnreachable(
        override val host: String,
        val kind: UnreachableKind,
        val details: ConnectionFailureDetails?,
    ) : ConnectionStatus

    data class WorkspaceFailure(
        override val host: String,
        val message: String,
        val details: ConnectionFailureDetails?,
    ) : ConnectionStatus

    data class CrashRecovery(
        override val host: String,
        val details: ConnectionFailureDetails?,
    ) : ConnectionStatus

    data class ConnectingSlow(
        override val host: String,
    ) : ConnectionStatus

    data class UpdateRequired(
        override val host: String,
    ) : ConnectionStatus
}

internal enum class UnreachableKind { GENERIC, OFFLINE, TLS }

internal fun unreachableKind(
    validated: Boolean,
    tls: Boolean,
): UnreachableKind =
    when {
        tls -> UnreachableKind.TLS
        !validated -> UnreachableKind.OFFLINE
        else -> UnreachableKind.GENERIC
    }

internal data class ConnectionFailureDetails(
    val origin: String,
    val error: String,
    val timeMillis: Long,
)

internal enum class StatusAction { NONE, CANCEL, SIGN_IN, RETRY, CHANGE_SERVER, HOW_TO_UPDATE }

internal data class ConnectionPresentation(
    val headline: Int,
    val body: Int? = null,
    val dynamicBody: String? = null,
    val progress: Boolean = false,
    val hostChip: Boolean = true,
    val icon: Int? = null,
    val primary: StatusAction = StatusAction.NONE,
    val secondary: StatusAction = StatusAction.NONE,
    val cancel: Boolean = false,
)

internal fun ConnectionStatus.presentation(): ConnectionPresentation =
    when (this) {
        is ConnectionStatus.Connecting -> {
            ConnectionPresentation(R.string.oidc_connecting, progress = true, cancel = true)
        }

        is ConnectionStatus.WorkspaceConnecting -> {
            ConnectionPresentation(R.string.oauth_connecting, progress = true, cancel = true)
        }

        is ConnectionStatus.SigningIn -> {
            ConnectionPresentation(R.string.oidc_signing_in, progress = true, cancel = true)
        }

        is ConnectionStatus.CompletingSignIn -> {
            ConnectionPresentation(
                if (workspace) R.string.oauth_completing else R.string.oidc_completing,
                progress = true,
                cancel = true,
            )
        }

        is ConnectionStatus.AuthTabHandoff -> {
            ConnectionPresentation(
                R.string.status_opening_sign_in,
                R.string.status_finish_sign_in,
                progress = true,
                cancel = true,
            )
        }

        is ConnectionStatus.SigningOut -> {
            ConnectionPresentation(R.string.oauth_signing_out, progress = true)
        }

        is ConnectionStatus.SignInRequired -> {
            ConnectionPresentation(
                R.string.status_sign_in_required,
                if (workspace && message == null) R.string.status_workspace_session_ended else null,
                message,
                icon = R.drawable.ic_status_lock,
                primary = StatusAction.SIGN_IN,
                secondary = StatusAction.CHANGE_SERVER,
            )
        }

        is ConnectionStatus.ServerUnreachable -> {
            ConnectionPresentation(
                R.string.status_server_unreachable,
                when (kind) {
                    UnreachableKind.GENERIC -> R.string.status_unreachable_generic
                    UnreachableKind.OFFLINE -> R.string.status_unreachable_offline
                    UnreachableKind.TLS -> R.string.status_unreachable_tls
                },
                icon = R.drawable.ic_status_unreachable,
                primary = StatusAction.RETRY,
                secondary = StatusAction.CHANGE_SERVER,
            )
        }

        is ConnectionStatus.WorkspaceFailure -> {
            ConnectionPresentation(
                R.string.status_workspace_failure,
                dynamicBody = message,
                icon = R.drawable.ic_status_alert,
                primary = StatusAction.RETRY,
                secondary = StatusAction.CHANGE_SERVER,
            )
        }

        is ConnectionStatus.CrashRecovery -> {
            ConnectionPresentation(
                R.string.status_crash_recovery,
                R.string.status_crash_recovery_body,
                icon = R.drawable.ic_status_crash,
                primary = StatusAction.RETRY,
                secondary = StatusAction.CHANGE_SERVER,
            )
        }

        is ConnectionStatus.ConnectingSlow -> {
            ConnectionPresentation(
                R.string.status_still_trying,
                progress = true,
                hostChip = false,
                secondary = StatusAction.CHANGE_SERVER,
                cancel = true,
            )
        }

        is ConnectionStatus.UpdateRequired -> {
            ConnectionPresentation(
                R.string.status_update_required,
                R.string.status_update_required_body,
                icon = R.drawable.ic_status_alert,
                primary = StatusAction.HOW_TO_UPDATE,
                secondary = StatusAction.CHANGE_SERVER,
            )
        }
    }

internal fun isConnectionError(
    code: Int,
    description: String,
): Boolean =
    !description.contains("ERR_ABORTED", ignoreCase = true) &&
        code in setOf(-2, -6, -7, -8, -11)

internal fun connectionErrorName(code: Int): String =
    when (code) {
        -2 -> "ERROR_HOST_LOOKUP"
        -6 -> "ERROR_CONNECT"
        -7 -> "ERROR_IO"
        -8 -> "ERROR_TIMEOUT"
        -11 -> "ERROR_FAILED_SSL_HANDSHAKE"
        else -> "ERROR_UNKNOWN"
    }

private val bareNetError = Regex("^net::ERR_[A-Z0-9_]{1,64}$")

internal fun webViewErrorName(
    code: Int,
    description: String,
): String {
    val constant = connectionErrorName(code)
    return if (bareNetError.matches(description)) "$description ($constant)" else constant
}

internal fun failureClassName(error: Throwable?): String =
    when (error) {
        is java.net.UnknownHostException -> "UnknownHostException"
        is java.net.ConnectException -> "ConnectException"
        is java.net.SocketTimeoutException -> "SocketTimeoutException"
        is java.io.IOException -> "IOException"
        is OidcSignInException.Network -> "NetworkUnavailable"
        is DatabricksSessionException.NetworkUnavailable -> "NetworkUnavailable"
        is DatabricksSessionException.InvalidResponse -> "InvalidResponse"
        is DatabricksSessionException.UnsafeRedirect -> "UnsafeRedirect"
        is DatabricksSessionException.WorkspaceChanged -> "WorkspaceChanged"
        is DatabricksSessionException.UnsafeCookie -> "UnsafeCookie"
        is DatabricksSessionException.MissingCookie -> "MissingCookie"
        is DatabricksSessionException.UnexpectedLanding -> "UnexpectedLanding"
        is DatabricksSessionException.Rejected -> "Rejected"
        is DatabricksSessionException.WorkspaceRequired -> "WorkspaceRequired"
        is DatabricksSessionException.UnsupportedWebView -> "UnsupportedWebView"
        else -> "ERROR_UNKNOWN"
    }

internal fun isUpgradeRequiredTicket(ticket: String?): Boolean = ticket == "upgrade-required"

internal fun isUpgradeRequiredResponse(
    status: Int,
    body: String,
): Boolean = status == 400 && body.contains("code_challenge (S256) is required.")
