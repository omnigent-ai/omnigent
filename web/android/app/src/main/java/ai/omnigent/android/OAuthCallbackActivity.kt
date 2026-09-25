package ai.omnigent.android

import android.content.Intent
import android.os.Bundle
import android.view.Gravity
import android.widget.TextView
import androidx.activity.ComponentActivity
import java.net.URI
import java.util.concurrent.Executors

/** HTTPS compatibility receiver for browsers that fall back from Auth Tab to Custom Tabs. */
class OAuthCallbackActivity : ComponentActivity() {
    private val executor = Executors.newSingleThreadExecutor()
    private val callbackHandoff by lazy { DatabricksCallbackHandoff(applicationContext) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        callbackHandoff.markInProgress()
        setContentView(
            TextView(this).apply {
                text = getString(R.string.oauth_completing)
                gravity = Gravity.CENTER
            },
        )
        val callback = intent?.data?.toString()?.let { runCatching { URI(it) }.getOrNull() }
        if (callback == null) {
            returnToApp(getString(R.string.oauth_invalid_callback))
            return
        }
        executor.execute {
            val error =
                runCatching { DatabricksLoginManager(applicationContext).complete(callback) }
                    .exceptionOrNull()
                    ?.message
            runOnUiThread { returnToApp(error) }
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

    override fun onDestroy() {
        executor.shutdownNow()
        super.onDestroy()
    }

    companion object {
        const val EXTRA_OAUTH_CALLBACK = "ai.omnigent.android.OAUTH_CALLBACK"
        const val EXTRA_OAUTH_ERROR = "ai.omnigent.android.OAUTH_ERROR"
    }
}
