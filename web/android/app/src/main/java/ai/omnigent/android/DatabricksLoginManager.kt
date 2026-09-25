package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResultLauncher
import androidx.browser.auth.AuthTabIntent
import java.net.URI

/** Starts browser-owned OAuth and completes its Auth Tab result into durable credentials. */
internal class DatabricksLoginManager(
    context: android.content.Context,
    private val configuration: DatabricksOAuthConfiguration =
        DatabricksOAuthConfiguration.fromBuildConfig(),
    private val pending: DatabricksPendingAuthStore =
        DatabricksPendingAuthStore(context.applicationContext),
    private val client: DatabricksOAuthClient = DatabricksOAuthClient(),
    private val tokens: DatabricksTokenManager = DatabricksTokenManager.shared(context),
    private val callbackHandoff: DatabricksCallbackHandoff =
        DatabricksCallbackHandoff(context.applicationContext),
) {
    fun start(
        launcher: ActivityResultLauncher<Intent>,
        workspaceUrl: URI,
    ): Boolean {
        val attempt = DatabricksOAuthAttempt.create(workspaceUrl, configuration)
        callbackHandoff.clear()
        pending.begin(attempt)
        val redirect = configuration.redirectUri
        return try {
            AuthTabIntent
                .Builder()
                .build()
                .launch(
                    launcher,
                    Uri.parse(attempt.authorizationUri.toString()),
                    redirect.host,
                    redirect.path,
                )
            true
        } catch (_: Throwable) {
            pending.clear()
            false
        }
    }

    fun complete(callback: URI): DatabricksOAuthTokens {
        val saved = pending.load() ?: throw DatabricksOAuthException.InvalidCallback()
        val authorization = saved.attempt.authorizationResponse(callback)
        val claimed =
            pending.consume(saved.id) ?: throw DatabricksOAuthException.InvalidCallback()
        val result =
            client.exchange(
                authorization.code,
                claimed.attempt,
                authorization.issuer,
            )
        tokens.save(claimed.attempt.credentialScope, result)
        return result
    }

    fun cancel() {
        callbackHandoff.clear()
        pending.clear()
    }
}
