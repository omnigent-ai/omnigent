package ai.omnigent.android

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity

/**
 * Disposable trampoline that forwards `omnigent://` links to [MainActivity].
 *
 * Its empty task affinity prevents Android from fronting an existing task and
 * swallowing the VIEW intent. `FLAG_ACTIVITY_NEW_TASK` routes [MainActivity]
 * back to its default-affinity task. Link validation remains centralized there.
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
