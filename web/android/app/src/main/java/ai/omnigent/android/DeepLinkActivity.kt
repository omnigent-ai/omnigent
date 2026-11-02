package ai.omnigent.android

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.os.Bundle

/** Opens a [DeepLink] in [MainActivity], switching to or connecting its server as needed. */
class DeepLinkActivity : Activity() {
    private var consent: AlertDialog? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val link = DeepLink.parse(intent?.dataString)
        if (link == null) {
            openApp(path = null)
            return
        }
        val store = ServerStore(this)
        val known = store.recentServer(link.origin)
        if (known == null) {
            askToConnect(store, link)
        } else if (store.hasServer() && originOf(store.currentServerUrl()) == link.origin) {
            openApp(link.path)
        } else {
            // Any app can send this link, so the switch must not start an interactive sign-in.
            store.connect(known)
            openApp(link.path)
        }
    }

    // Connecting grants the native bridge, so only servers in recents skip consent. A consented
    // server joins recents once its page loads; a preset on that origin keeps its mount.
    private fun askToConnect(
        store: ServerStore,
        link: DeepLink,
    ) {
        consent =
            AlertDialog
                .Builder(this)
                .setTitle(R.string.deep_link_consent_title)
                .setMessage(getString(R.string.deep_link_consent_message, link.hostLabel))
                .setPositiveButton(R.string.deep_link_consent_open) { _, _ ->
                    store.select(store.managed.serverFor(link.origin) ?: link.origin)
                    openApp(link.path, UserConnectRequests.issue())
                }.setNegativeButton(android.R.string.cancel) { _, _ -> finish() }
                .setOnCancelListener { finish() }
                .show()
                .apply { getButton(AlertDialog.BUTTON_POSITIVE).filterTouchesWhenObscured = true }
    }

    override fun onDestroy() {
        consent?.dismiss()
        super.onDestroy()
    }

    // NEW_TASK + CLEAR_TOP|SINGLE_TOP re-enter the app's own task, reusing a running shell.
    private fun openApp(
        path: String?,
        connectRequest: String? = null,
    ) {
        startActivity(
            Intent(this, MainActivity::class.java).apply {
                addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP,
                )
                path?.let { putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, it) }
                connectRequest?.let { putExtra(MainActivity.EXTRA_CONNECT_REQUEST, it) }
            },
        )
        finish()
    }
}
