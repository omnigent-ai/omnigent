package ai.omnigent.android

import android.content.Intent
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity

/**
 * Native server-entry screen, the Android counterpart to the iOS `ConnectView`:
 * a URL field plus a tappable recent-servers list. On connect it persists the
 * server via [ServerStore] and hands off to [MainActivity].
 *
 * [MainActivity] routes here on launch when no server has been configured yet.
 */
class ConnectActivity : ComponentActivity() {
    private val store by lazy { ServerStore(this) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_connect)

        val field = findViewById<EditText>(R.id.server_url)
        findViewById<Button>(R.id.connect).setOnClickListener { connect(field.text.toString()) }
        field.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_GO) {
                connect(field.text.toString())
                true
            } else {
                false
            }
        }

        renderRecents()
    }

    private fun connect(input: String) {
        val url = normalizeServerUrl(input)
        val error = findViewById<TextView>(R.id.error_text)
        if (url == null) {
            error.setText(R.string.connect_invalid)
            error.visibility = View.VISIBLE
            return
        }
        error.visibility = View.GONE
        store.connect(url)
        startActivity(
            Intent(this, MainActivity::class.java).apply {
                flags = Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP
            },
        )
        finish()
    }

    private fun renderRecents() {
        val recents = store.recentServers()
        val container = findViewById<LinearLayout>(R.id.recents)
        findViewById<TextView>(R.id.recents_label).visibility =
            if (recents.isEmpty()) View.GONE else View.VISIBLE

        val inflater = LayoutInflater.from(this)
        for (url in recents) {
            val row = inflater.inflate(R.layout.item_recent, container, false) as TextView
            row.text = url
            row.setOnClickListener { connect(url) }
            container.addView(row)
        }
    }
}
