package ai.omnigent.android

import android.animation.ValueAnimator
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.os.SystemClock
import android.text.format.DateFormat
import android.view.LayoutInflater
import android.view.View
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import java.time.Instant
import java.util.Date
import kotlin.math.max
import kotlin.math.min

internal class ConnectionStatusView(
    context: Context,
    private val onAction: (StatusAction) -> Unit,
) : FrameLayout(context) {
    private val scroll =
        LayoutInflater
            .from(
                context,
            ).inflate(R.layout.view_connection_status, this, false) as ScrollView
    private val column: LinearLayout = scroll.findViewById(R.id.status_column)
    private val anchorGap: View = scroll.findViewById(R.id.status_anchor_gap)
    private val content: View = scroll.findViewById(R.id.status_content)
    private val progress: ProgressBar = scroll.findViewById(R.id.status_progress)
    private val icon: ImageView = scroll.findViewById(R.id.status_icon)
    private val headline: TextView = scroll.findViewById(R.id.status_headline)
    private val body: TextView = scroll.findViewById(R.id.status_body)
    private val host: TextView = scroll.findViewById(R.id.status_host)
    private val detailsPanel: View = scroll.findViewById(R.id.status_details_panel)
    private val detailsContent: View = scroll.findViewById(R.id.status_details_content)
    private val detailsText: TextView = scroll.findViewById(R.id.status_details_text)
    private val detailsToggle: Button = scroll.findViewById(R.id.status_details_toggle)
    private val copyDetails: Button = scroll.findViewById(R.id.status_copy_details)
    private val primary: Button = scroll.findViewById(R.id.status_primary)
    private val secondary: Button = scroll.findViewById(R.id.status_secondary)
    private val cancel: Button = scroll.findViewById(R.id.status_cancel)
    private var pendingRender: Runnable? = null
    private var firstRequestedAt = 0L
    private var lastShownAt = 0L
    private var transitionGeneration = 0
    private var topInset = 0
    private var renderedState: ConnectionStatus? = null
    var detailsExpanded: Boolean = false
        private set

    init {
        addView(scroll, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT))
        isClickable = true
        isFocusable = true
        ViewCompat.setOnApplyWindowInsetsListener(this) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            topInset = bars.top
            column.setPadding(dp(24), bars.top, dp(24), bars.bottom + dp(16))
            updateAnchor()
            insets
        }
        scroll.addOnLayoutChangeListener {
            _,
            left,
            _,
            right,
            bottom,
            oldLeft,
            _,
            oldRight,
            oldBottom,
            ->
            if (bottom != oldBottom || right - left != oldRight - oldLeft) updateAnchor()
        }
        detailsToggle.setOnClickListener {
            detailsExpanded = !detailsExpanded
            detailsContent.visibility = if (detailsExpanded) View.VISIBLE else View.GONE
            detailsToggle.rotation = if (detailsExpanded) 180f else 0f
        }
    }

    fun show(
        state: ConnectionStatus,
        immediate: Boolean = false,
        retainDetails: Boolean = false,
    ) {
        visibility = View.VISIBLE
        content.animate().cancel()
        val generation = ++transitionGeneration
        pendingRender?.let(::removeCallbacks)
        if (retainDetails) {
            detailsExpanded = true
        } else if (state != renderedState) {
            detailsExpanded = false
        }
        val now = SystemClock.uptimeMillis()
        if (firstRequestedAt == 0L) firstRequestedAt = now
        val delay =
            if (immediate || !ValueAnimator.areAnimatorsEnabled()) {
                0L
            } else {
                max(500L - (now - firstRequestedAt), 500L - (now - lastShownAt)).coerceAtLeast(0L)
            }
        val render =
            Runnable {
                pendingRender = null
                if (renderedState != null && !immediate && ValueAnimator.areAnimatorsEnabled()) {
                    content.animate().cancel()
                    content
                        .animate()
                        .alpha(0f)
                        .setDuration(90)
                        .withEndAction {
                            if (generation != transitionGeneration) return@withEndAction
                            bind(state)
                            lastShownAt = SystemClock.uptimeMillis()
                            renderedState = state
                            content
                                .animate()
                                .alpha(1f)
                                .setDuration(210)
                                .start()
                        }.start()
                } else {
                    content.animate().cancel()
                    bind(state)
                    lastShownAt = SystemClock.uptimeMillis()
                    content.alpha = 1f
                    renderedState = state
                }
                content.visibility = View.VISIBLE
            }
        if (delay == 0L) {
            render.run()
        } else {
            pendingRender = render
            postDelayed(render, delay)
        }
    }

    fun hide() {
        transitionGeneration++
        pendingRender?.let(::removeCallbacks)
        pendingRender = null
        content.animate().cancel()
        content.visibility = View.GONE
        visibility = View.GONE
        firstRequestedAt = 0L
        lastShownAt = 0L
        renderedState = null
    }

    private fun updateAnchor() {
        val anchor = max(dp(120), (height * .30f).toInt())
        val gap = max(dp(32), anchor - topInset - dp(56))
        if (anchorGap.layoutParams.height != gap) {
            anchorGap.layoutParams = anchorGap.layoutParams.apply { height = gap }
        }
        val maxWidth = min(width - dp(48), dp(400))
        if (maxWidth > 0 && content.layoutParams.width != maxWidth) {
            content.layoutParams = content.layoutParams.apply { this.width = maxWidth }
        }
    }

    private fun bind(state: ConnectionStatus) {
        val presentation = state.presentation()
        headline.text =
            if (state is ConnectionStatus.ConnectingSlow) {
                context.getString(presentation.headline, breakableHost(state.host))
            } else {
                context.getString(presentation.headline)
            }
        body.text = presentation.dynamicBody ?: presentation.body?.let(context::getString).orEmpty()
        body.visibility = if (body.text.isEmpty()) View.GONE else View.VISIBLE
        host.text = breakableHost(state.host)
        host.visibility = if (presentation.hostChip) View.VISIBLE else View.GONE
        val animate = ValueAnimator.areAnimatorsEnabled()
        progress.visibility = if (presentation.progress && animate) View.VISIBLE else View.INVISIBLE
        icon.visibility = if (presentation.icon == StatusIcon.NONE) View.GONE else View.VISIBLE
        if (presentation.icon != StatusIcon.NONE) {
            icon.setImageResource(
                when (presentation.icon) {
                    StatusIcon.UNREACHABLE -> R.drawable.ic_status_unreachable
                    StatusIcon.ALERT -> R.drawable.ic_status_alert
                    StatusIcon.CRASH -> R.drawable.ic_status_crash
                    StatusIcon.LOCK -> R.drawable.ic_status_lock
                    StatusIcon.NONE -> error("No icon")
                },
            )
        }
        val details =
            when (state) {
                is ConnectionStatus.ServerUnreachable -> state.details
                is ConnectionStatus.WorkspaceFailure -> state.details
                is ConnectionStatus.CrashRecovery -> state.details
                else -> null
            }
        detailsPanel.visibility = if (details == null) View.GONE else View.VISIBLE
        detailsContent.visibility =
            if (details != null && detailsExpanded) View.VISIBLE else View.GONE
        detailsToggle.rotation = if (detailsExpanded) 180f else 0f
        if (details != null) bindDetails(details)
        bindAction(primary, presentation.primary, state)
        bindAction(secondary, presentation.secondary, state)
        cancel.visibility = if (presentation.cancel) View.VISIBLE else View.GONE
        cancel.setOnClickListener { onAction(StatusAction.CANCEL) }
        ViewCompat.setAccessibilityPaneTitle(this, headline.text)
    }

    private fun bindAction(
        button: Button,
        action: StatusAction,
        state: ConnectionStatus,
    ) {
        button.visibility = if (action == StatusAction.NONE) View.GONE else View.VISIBLE
        if (action == StatusAction.NONE) return
        button.setText(
            when (action) {
                StatusAction.SIGN_IN -> {
                    R.string.status_sign_in
                }

                StatusAction.RETRY -> {
                    if (state is ConnectionStatus.CrashRecovery) {
                        R.string.status_reload
                    } else {
                        R.string.status_retry
                    }
                }

                StatusAction.CHANGE_SERVER -> {
                    R.string.status_change_server
                }

                StatusAction.HOW_TO_UPDATE -> {
                    R.string.status_how_to_update
                }

                else -> {
                    error("Unsupported button action")
                }
            },
        )
        button.setOnClickListener { onAction(action) }
    }

    private fun bindDetails(details: ConnectionFailureDetails) {
        val localTime =
            DateFormat.getDateFormat(context).format(Date(details.timeMillis)) + " " +
                DateFormat.getTimeFormat(context).format(Date(details.timeMillis))
        val version =
            context.getString(
                R.string.status_app_version,
                BuildConfig.VERSION_NAME,
                BuildConfig.VERSION_CODE,
            )
        detailsText.text =
            listOf(
                context.getString(R.string.status_server) to details.origin,
                context.getString(R.string.status_error) to details.error,
                context.getString(R.string.status_time) to localTime,
                context.getString(R.string.status_app_version_label) to version,
            ).joinToString("\n\n") { (label, value) -> "$label\n$value" }
        copyDetails.setOnClickListener {
            val copied = "${detailsText.text}\n${context.getString(
                R.string.status_utc,
            )}: ${Instant.ofEpochMilli(
                details.timeMillis,
            )}\n${context.getString(
                R.string.status_android_api,
            )}: ${Build.VERSION.SDK_INT}\n${context.getString(
                R.string.status_webview,
            )}: ${android.webkit.WebView.getCurrentWebViewPackage()?.versionName.orEmpty()}"
            val clipboard = ContextCompat.getSystemService(context, ClipboardManager::class.java)
            clipboard?.setPrimaryClip(
                ClipData.newPlainText(context.getString(R.string.status_details), copied),
            )
            if (Build.VERSION.SDK_INT <= 32) {
                Toast.makeText(context, R.string.status_copied, Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun breakableHost(value: String): String =
        value.replace(".", ".\u200b").replace("-", "-\u200b")

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + .5f).toInt()
}
