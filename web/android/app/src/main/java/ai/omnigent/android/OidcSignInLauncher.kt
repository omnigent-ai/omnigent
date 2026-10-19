package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResultLauncher
import androidx.browser.auth.AuthTabIntent

/**
 * Opens a native OIDC sign-in in Auth Tab, which hands the redirect straight back to the app.
 * Browsers without Auth Tab open a Custom Tab, whose redirect reaches [OAuthCallbackActivity].
 */
internal class OidcSignInLauncher(
    private val credentials: OidcCredentials,
    private val handoff: OAuthCallbackHandoff,
) {
    /** False when no browser could open the sign-in; the attempt is then discarded. */
    fun start(
        launcher: ActivityResultLauncher<Intent>,
        serverUrl: String,
        cookieName: String,
    ): Boolean {
        val authorization = credentials.beginSignIn(serverUrl, cookieName)
        handoff.clear()
        return try {
            AuthTabIntent
                .Builder()
                .build()
                .launch(launcher, Uri.parse(authorization.toString()), OidcRedirect.SCHEME)
            true
        } catch (_: Exception) {
            credentials.cancelSignIn()
            false
        }
    }
}
