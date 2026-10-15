package ai.omnigent.android

import android.content.Context
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.core.content.ContextCompat

/**
 * Full-screen cover for the WebView while the system-browser login runs. The
 * shell cancels the server's IdP bounce and authenticates in the browser, so
 * without this cover the WebView would keep presenting the last-painted SPA
 * document — an unauthenticated page that looks signed in while every API call
 * is rejected. Also renders the give-up state once the login retry budget is
 * exhausted (or the flow fails outright), with a retry action so the dead end
 * is recoverable.
 *
 * Theme-aware via the brand palette (values / values-night colors.xml), like
 * the floating server switcher it sits under.
 */
class PendingLoginOverlay(
    context: Context,
    onRetry: () -> Unit,
) : LinearLayout(context) {
    private val spinner = ProgressBar(context)
    private val title = TextView(context)
    private val body = TextView(context)
    private val retry = Button(context)

    init {
        orientation = VERTICAL
        gravity = Gravity.CENTER
        // Opaque and touch-consuming: nothing from the covered page may show
        // through or be poked while the login is unresolved. The floating
        // server switcher is added above this view, so it stays reachable as
        // the recovery path.
        setBackgroundColor(ContextCompat.getColor(context, R.color.brand_background))
        isClickable = true
        val dp = resources.displayMetrics.density
        title.setTextColor(ContextCompat.getColor(context, R.color.brand_foreground))
        title.textSize = 18f
        title.gravity = Gravity.CENTER
        title.setPadding(0, (16 * dp).toInt(), 0, 0)
        body.setTextColor(ContextCompat.getColor(context, R.color.brand_muted_foreground))
        body.textSize = 14f
        body.gravity = Gravity.CENTER
        body.setPadding((32 * dp).toInt(), (8 * dp).toInt(), (32 * dp).toInt(), (16 * dp).toInt())
        retry.text = context.getString(R.string.login_retry)
        retry.setOnClickListener { onRetry() }
        addView(spinner)
        addView(title)
        addView(body)
        addView(retry)
        visibility = GONE
    }

    /** Present the "signing in…" state (login running in the system browser). */
    fun showPending() {
        spinner.visibility = VISIBLE
        retry.visibility = GONE
        title.text = context.getString(R.string.login_pending_title)
        body.text = context.getString(R.string.login_pending_body)
        visibility = VISIBLE
    }

    /** Present the sign-in-failed state with the retry action. */
    fun showError() {
        spinner.visibility = GONE
        retry.visibility = VISIBLE
        title.text = context.getString(R.string.login_failed_title)
        body.text = context.getString(R.string.login_failed_body)
        visibility = VISIBLE
    }
}
