package ai.omnigent.android

import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity

/**
 * Receives an `omnigent://` link and hands its conversation to [MainActivity].
 *
 * A link can come from any app or web page, so it only ever opens a conversation, and only on a
 * server the user chose: one they connected to before opens directly, and any other server is
 * named in a prompt first. Connecting gives a server's page the app's notifications, microphone
 * and file access, which a tapped link must not hand to a server of its author's choosing.
 */
class DeepLinkActivity : ComponentActivity() {
    private val store by lazy { ServerStore(this) }
    private var prompt: AlertDialog? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val link = DeepLink.parse(intent?.dataString)
        if (link == null) {
            // Not a link this app opens: show the app as it is rather than nothing at all.
            bringAppForward()
            return
        }
        val connected = store.connectedServer(link.origin)
        if (connected != null) {
            open(connected, link.path, userConfirmed = false)
        } else {
            askToConnect(link)
        }
    }

    override fun onDestroy() {
        prompt?.dismiss()
        super.onDestroy()
    }

    private fun askToConnect(link: DeepLink) {
        prompt =
            AlertDialog
                .Builder(this)
                .setTitle(R.string.deep_link_consent_title)
                .setMessage(
                    getString(
                        R.string.deep_link_consent_message,
                        link.origin.substringAfter("://"),
                    ),
                ).setNegativeButton(android.R.string.cancel) { _, _ -> finish() }
                .setPositiveButton(R.string.deep_link_open) { _, _ ->
                    val server = store.presetServer(link.origin) ?: link.origin
                    open(server, link.path, userConfirmed = true)
                }.setOnCancelListener { finish() }
                .show()
    }

    /**
     * Makes [serverUrl] the app's server and opens [path] on it. Only a connect the user just
     * confirmed is marked as one they asked for, which lets a native sign-in open its browser
     * without showing Sign In first. A link to a known server connects like an app launch does.
     */
    private fun open(
        serverUrl: String,
        path: String,
        userConfirmed: Boolean,
    ) {
        store.connect(serverUrl)
        showApp(
            Intent(this, MainActivity::class.java).apply {
                putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, path)
                if (userConfirmed) {
                    putExtra(MainActivity.EXTRA_CONNECT_REQUEST, UserConnectRequests.issue())
                }
            },
        )
    }

    /**
     * Brings the app's task forward exactly as it was left, the way tapping its icon does: a
     * screen open above the conversation, such as the server picker, keeps what was typed in it.
     */
    private fun bringAppForward() {
        startActivity(
            Intent(Intent.ACTION_MAIN)
                .addCategory(Intent.CATEGORY_LAUNCHER)
                .setClass(this, MainActivity::class.java)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_RESET_TASK_IF_NEEDED,
                ),
        )
        finish()
    }

    private fun showApp(intent: Intent) {
        // The app that sent the link may have started this activity in its own task. NEW_TASK
        // finds the app's task, so the open conversation is reused instead of loaded twice.
        intent.flags =
            Intent.FLAG_ACTIVITY_NEW_TASK or
            Intent.FLAG_ACTIVITY_CLEAR_TOP or
            Intent.FLAG_ACTIVITY_SINGLE_TOP
        startActivity(intent)
        finish()
    }
}
