package ai.omnigent.android

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity

/**
 * Trampoline for `omnigent://` links: exists only because Android's
 * task-fronting can swallow a VIEW intent aimed at [MainActivity] when
 * another activity (e.g. `ConnectActivity`) sits on top of the same task —
 * confirmed on-device (`START ... result code=3`, no `onCreate`/`onNewIntent`,
 * link silently dropped). The same drop recurs one level up if this
 * trampoline itself roots a task matching a later VIEW launch (also
 * confirmed on-device) — so its manifest entry declares `taskAffinity=""`,
 * giving every launch its own disposable task that's gone the instant this
 * activity finishes, and can never be matched and fronted later.
 * `FLAG_ACTIVITY_NEW_TASK` on the forward is required to route
 * [MainActivity] into its own (default) affinity's task from that
 * empty-affinity task, rather than into this activity's dying one. No
 * parsing/validation here — [MainActivity]'s intake stays the single
 * authority on link contents.
 */
class DeepLinkActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        startActivity(
            Intent(this, MainActivity::class.java)
                .setAction(Intent.ACTION_VIEW)
                .setData(intent?.data)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP,
                ),
        )
        finish()
    }
}
