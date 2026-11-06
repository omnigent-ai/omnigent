package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ConnectionStatusTest {
    @Test
    fun `cold start and recovery states present the approved actions`() {
        val host = "omni.example"
        val details = ConnectionFailureDetails("https://omni.example", "ERROR_CONNECT", 0)
        val cases =
            listOf(
                ConnectionStatus.Connecting(host) to
                    Triple(R.string.oidc_connecting, StatusAction.NONE, true),
                ConnectionStatus.WorkspaceConnecting(host) to
                    Triple(R.string.oauth_connecting, StatusAction.NONE, true),
                ConnectionStatus.SigningIn(host) to
                    Triple(R.string.oidc_signing_in, StatusAction.NONE, true),
                ConnectionStatus.CompletingSignIn(host, false) to
                    Triple(R.string.oidc_completing, StatusAction.NONE, true),
                ConnectionStatus.CompletingSignIn(host, true) to
                    Triple(R.string.oauth_completing, StatusAction.NONE, true),
                ConnectionStatus.AuthTabHandoff(host) to
                    Triple(R.string.status_opening_sign_in, StatusAction.NONE, true),
                ConnectionStatus.SigningOut(host) to
                    Triple(R.string.oauth_signing_out, StatusAction.NONE, false),
                ConnectionStatus.SignInRequired(host, "Sign in to omni.example", false) to
                    Triple(R.string.status_sign_in_required, StatusAction.SIGN_IN, false),
                ConnectionStatus.ServerUnreachable(host, UnreachableKind.GENERIC, details) to
                    Triple(R.string.status_server_unreachable, StatusAction.RETRY, false),
                ConnectionStatus.WorkspaceFailure(host, "failed", details) to
                    Triple(R.string.status_workspace_failure, StatusAction.RETRY, false),
                ConnectionStatus.CrashRecovery(host, details) to
                    Triple(R.string.status_crash_recovery, StatusAction.RETRY, false),
                ConnectionStatus.ConnectingSlow(host) to
                    Triple(R.string.status_still_trying, StatusAction.NONE, true),
                ConnectionStatus.UpdateRequired(host) to
                    Triple(R.string.status_update_required, StatusAction.HOW_TO_UPDATE, false),
            )
        for ((state, expected) in cases) {
            val actual = state.presentation()
            assertEquals(state.toString(), expected.first, actual.headline)
            assertEquals(state.toString(), expected.second, actual.primary)
            assertEquals(state.toString(), expected.third, actual.cancel)
        }
        assertEquals(
            StatusAction.CHANGE_SERVER,
            ConnectionStatus.UpdateRequired(host).presentation().secondary,
        )
        assertFalse(ConnectionStatus.ConnectingSlow(host).presentation().hostChip)
        assertTrue(ConnectionStatus.AuthTabHandoff(host).presentation().progress)
    }

    @Test
    fun `failed server navigation classifies transport TLS and offline signals`() {
        for (code in listOf(-2, -6, -7, -8, -11)) {
            assertTrue(isConnectionError(code, "net::ERR_CONNECTION_FAILED"))
        }
        assertFalse(isConnectionError(-3, "net::ERR_ABORTED"))
        assertFalse(isConnectionError(-6, "net::ERR_ABORTED"))
        assertFalse(isConnectionError(-10, "unsupported scheme"))
        assertEquals(UnreachableKind.GENERIC, unreachableKind(validated = true, tls = false))
        assertEquals(UnreachableKind.OFFLINE, unreachableKind(validated = false, tls = false))
        assertEquals(UnreachableKind.TLS, unreachableKind(validated = false, tls = true))
        assertEquals("ERROR_CONNECT", connectionErrorName(-6))
        assertEquals(
            "net::ERR_CONNECTION_REFUSED (ERROR_CONNECT)",
            webViewErrorName(-6, "net::ERR_CONNECTION_REFUSED"),
        )
        for (description in listOf(
            "net::ERR_FOO?x=1",
            "https://example.com/net::ERR_CONNECTION_REFUSED",
            "net::ERR_${"A".repeat(65)}",
            "net::ERR_X\n",
            "net::err_connection_refused",
            "net::ЕRR_X",
        )) {
            assertEquals("ERROR_CONNECT", webViewErrorName(-6, description))
        }
    }

    @Test
    fun `exception labels come from fixed classes and ignore messages`() {
        val privateUrl = "https://example.com/callback?state=ERR_PRIVATESTATE"
        assertEquals(
            "UnknownHostException",
            failureClassName(java.net.UnknownHostException(privateUrl)),
        )
        assertEquals("ConnectException", failureClassName(java.net.ConnectException(privateUrl)))
        assertEquals("IOException", failureClassName(java.io.IOException(privateUrl)))
        assertEquals(
            "NetworkUnavailable",
            failureClassName(OidcSignInException.Network(privateUrl)),
        )
        val workspaceFailures =
            listOf(
                DatabricksSessionException.InvalidResponse() to "InvalidResponse",
                DatabricksSessionException.UnsafeRedirect() to "UnsafeRedirect",
                DatabricksSessionException.WorkspaceChanged() to "WorkspaceChanged",
                DatabricksSessionException.UnsafeCookie() to "UnsafeCookie",
                DatabricksSessionException.MissingCookie() to "MissingCookie",
                DatabricksSessionException.UnexpectedLanding() to "UnexpectedLanding",
                DatabricksSessionException.NetworkUnavailable() to "NetworkUnavailable",
                DatabricksSessionException.Rejected(403) to "Rejected",
                DatabricksSessionException.WorkspaceRequired() to "WorkspaceRequired",
                DatabricksSessionException.UnsupportedWebView() to "UnsupportedWebView",
            )
        for ((failure, label) in workspaceFailures) {
            assertEquals(label, failureClassName(failure))
        }
        assertEquals("ERROR_UNKNOWN", failureClassName(IllegalStateException(privateUrl)))
    }

    @Test
    fun `legacy sign in detects only the reserved upgrade response`() {
        assertTrue(isUpgradeRequiredTicket("upgrade-required"))
        assertFalse(isUpgradeRequiredTicket("other"))
        val notice =
            """{"error":"code_challenge (S256) is required. """ +
                """Upgrade Omnigent to sign in"}"""
        assertTrue(isUpgradeRequiredResponse(400, notice))
        assertTrue(
            isUpgradeRequiredResponse(
                400,
                "code_challenge (S256) is required. Please update your client.",
            ),
        )
        assertFalse(isUpgradeRequiredResponse(500, notice))
        assertFalse(isUpgradeRequiredResponse(400, "invalid challenge"))
    }
}
