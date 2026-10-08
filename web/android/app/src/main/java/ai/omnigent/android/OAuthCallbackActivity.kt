package ai.omnigent.android

import android.content.Intent
import android.os.Bundle
import android.view.Gravity
import android.widget.TextView
import androidx.activity.ComponentActivity
import java.net.URI

/**
 * Callback receiver for browsers that fall back from Auth Tab to Custom Tabs: the Databricks
 * return page and the native OIDC redirect.
 */
class OAuthCallbackActivity : ComponentActivity() {
    private val callbackHandoff by lazy { OAuthCallbackHandoff(applicationContext) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        callbackHandoff.markInProgress()
        val callback = intent?.data?.toString()?.let { runCatching { URI(it) }.getOrNull() }
        val oidc = callback != null && OidcRedirect.matches(callback)
        setContentView(
            TextView(this).apply {
                text = getString(if (oidc) R.string.oidc_completing else R.string.oauth_completing)
                gravity = Gravity.CENTER
            },
        )
        if (callback == null) {
            returnToApp(getString(R.string.oauth_invalid_callback))
            return
        }
        val key = callback.toString()
        OAuthCallbackCompletions
            .start(key) { complete(callback) }
            .thenAccept { error ->
                runOnUiThread {
                    // A recreated instance has attached to the same completion and returns it.
                    if (isDestroyed) return@runOnUiThread
                    OAuthCallbackCompletions.finish(key)
                    returnToApp(error)
                }
            }
    }

    private fun complete(callback: URI) {
        if (OidcRedirect.matches(callback)) {
            // The shell installs the session; only the minted token is held for it, in memory.
            OidcCredentials.shared(applicationContext).completeHandedOffSignIn(callback)
        } else {
            DatabricksLoginManager(applicationContext).complete(callback)
        }
    }

    private fun returnToApp(error: String?) {
        startActivity(
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
                putExtra(EXTRA_OAUTH_CALLBACK, true)
                error?.let { putExtra(EXTRA_OAUTH_ERROR, it) }
            },
        )
        finish()
    }

    companion object {
        const val EXTRA_OAUTH_CALLBACK = "ai.omnigent.android.OAUTH_CALLBACK"
        const val EXTRA_OAUTH_ERROR = "ai.omnigent.android.OAUTH_ERROR"
    }
}
