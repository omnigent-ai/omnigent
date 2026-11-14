package ai.omnigent.android

import android.animation.Animator
import android.animation.AnimatorListenerAdapter
import android.animation.ValueAnimator
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.os.SystemClock
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.format.DateFormat
import android.text.style.AbsoluteSizeSpan
import android.text.style.ForegroundColorSpan
import android.view.LayoutInflater
import android.view.View
import android.webkit.WebView
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
    private val safeWebViewError =
        Regex(
            "^net::ERR_[A-Z0-9_]{1,64} \\((?:ERROR_HOST_LOOKUP|ERROR_CONNECT|ERROR_IO|ERROR_TIMEOUT|ERROR_FAILED_SSL_HANDSHAKE)\\)$",
        )
    private val safeHttpStatus = Regex("^HTTP [1-5][0-9]{2}$")
    private val scroll =
        LayoutInflater
            .from(
                context,
            ).inflate(R.layout.view_connection_status, this, false) as ScrollView
    private val column: LinearLayout = scroll.findViewById(R.id.status_column)
    private val anchorGap: View = scroll.findViewById(R.id.status_anchor_gap)
    private val content: View = scroll.findViewById(R.id.status_content)
    private val actions: View = scroll.findViewById(R.id.status_actions)
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
    private var pendingHide: Runnable? = null
    private var hideDeadlineMillis = 0L
    private var fadeAnimator: ValueAnimator? = null
    private var firstRequestedAt = 0L
    private var lastShownAt = 0L
    private var transitionGeneration = 0
    private var topInset = 0
    private var renderedState: ConnectionStatus? = null
    var isHiding: Boolean = false
        private set
    private var actionsInteractive = false
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
            detailsToggle.compoundDrawablesRelative[2]?.level = if (detailsExpanded) 1 else 0
        }
    }

    fun show(
        state: ConnectionStatus,
        immediate: Boolean = false,
        retainDetails: Boolean = false,
    ) {
        visibility = View.VISIBLE
        isHiding = false
        pendingHide?.let(::removeCallbacks)
        pendingHide = null
        hideDeadlineMillis = 0L
        val generation = ++transitionGeneration
        fadeAnimator?.cancel()
        setActionsInteractive(false)
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
                    fade(0f, 90) {
                        if (generation != transitionGeneration) return@fade
                        bind(state)
                        lastShownAt = SystemClock.uptimeMillis()
                        renderedState = state
                        fade(1f, 210)
                        setActionsInteractive(true)
                    }
                } else {
                    bind(state)
                    lastShownAt = SystemClock.uptimeMillis()
                    content.alpha = 1f
                    actions.alpha = 1f
                    renderedState = state
                    setActionsInteractive(true)
                }
                content.visibility = View.VISIBLE
                actions.visibility = View.VISIBLE
            }
        if (delay == 0L) {
            render.run()
        } else {
            pendingRender = render
            postDelayed(render, delay)
        }
    }

    fun hide(onHidden: () -> Unit = {}) {
        val remaining =
            if (renderedState != null && ValueAnimator.areAnimatorsEnabled()) {
                (500L - (SystemClock.uptimeMillis() - lastShownAt)).coerceAtLeast(0L)
            } else {
                0L
            }
        hideUntil(SystemClock.uptimeMillis() + remaining, onHidden)
    }

    data class DeferredHide(
        val state: ConnectionStatus,
        val deadlineMillis: Long,
    )

    fun pendingHideSnapshot(): DeferredHide? =
        if (pendingHide !=
            null
        ) {
            renderedState?.let { DeferredHide(it, hideDeadlineMillis) }
        } else {
            null
        }

    fun restorePendingHide(
        snapshot: DeferredHide,
        onHidden: () -> Unit,
    ) {
        show(snapshot.state, immediate = true)
        hideUntil(snapshot.deadlineMillis, onHidden)
    }

    private fun hideUntil(
        deadlineMillis: Long,
        onHidden: () -> Unit,
    ) {
        isHiding = true
        setActionsInteractive(false)
        transitionGeneration++
        pendingRender?.let(::removeCallbacks)
        pendingRender = null
        pendingHide?.let(::removeCallbacks)
        pendingHide = null
        val remaining = (deadlineMillis - SystemClock.uptimeMillis()).coerceAtLeast(0L)
        if (remaining > 0L) {
            hideDeadlineMillis = deadlineMillis
            val deferred =
                Runnable {
                    pendingHide = null
                    finishHide(onHidden)
                }
            pendingHide = deferred
            postDelayed(deferred, remaining)
        } else {
            finishHide(onHidden)
        }
    }

    private fun finishHide(onHidden: () -> Unit) {
        fadeAnimator?.cancel()
        content.visibility = View.GONE
        actions.visibility = View.GONE
        visibility = View.GONE
        hideDeadlineMillis = 0L
        firstRequestedAt = 0L
        lastShownAt = 0L
        renderedState = null
        isHiding = false
        onHidden()
    }

    private fun fade(
        targetAlpha: Float,
        durationMillis: Long,
        onEnd: () -> Unit = {},
    ) {
        val animator = ValueAnimator.ofFloat(content.alpha, targetAlpha)
        animator.duration = durationMillis
        animator.addUpdateListener {
            content.alpha = it.animatedValue as Float
            actions.alpha = content.alpha
        }
        animator.addListener(
            object : AnimatorListenerAdapter() {
                private var cancelled = false

                override fun onAnimationCancel(animation: Animator) {
                    cancelled = true
                }

                override fun onAnimationEnd(animation: Animator) {
                    if (!cancelled) onEnd()
                }
            },
        )
        fadeAnimator = animator
        animator.start()
    }

    private fun setActionsInteractive(enabled: Boolean) {
        actionsInteractive = enabled
        for (button in listOf(primary, secondary, cancel, detailsToggle, copyDetails)) {
            button.isEnabled = enabled
        }
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
        if (maxWidth > 0 && actions.layoutParams.width != maxWidth) {
            actions.layoutParams = actions.layoutParams.apply { this.width = maxWidth }
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
        icon.visibility = if (presentation.icon == null) View.GONE else View.VISIBLE
        presentation.icon?.let(icon::setImageResource)
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
        detailsToggle.compoundDrawablesRelative[2]?.level = if (detailsExpanded) 1 else 0
        if (details != null) bindDetails(details)
        bindAction(primary, presentation.primary, state)
        bindAction(secondary, presentation.secondary, state)
        cancel.visibility = if (presentation.cancel) View.VISIBLE else View.GONE
        cancel.setOnClickListener { if (actionsInteractive) onAction(StatusAction.CANCEL) }
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
        button.setOnClickListener { if (actionsInteractive) onAction(action) }
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
        val rows =
            listOf(
                context.getString(R.string.status_server) to originOf(details.origin).orEmpty(),
                context.getString(R.string.status_error) to safeErrorName(details.error),
                context.getString(R.string.status_time) to localTime,
                context.getString(R.string.status_app_version_label) to version,
            )
        val styled = SpannableStringBuilder()
        val muted = ContextCompat.getColor(context, R.color.brand_muted_foreground)
        rows.forEachIndexed { index, (label, value) ->
            if (index > 0) styled.append("\n\n")
            val labelStart = styled.length
            styled.append(label)
            styled.setSpan(
                ForegroundColorSpan(muted),
                labelStart,
                styled.length,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
            )
            styled.setSpan(
                AbsoluteSizeSpan(12, true),
                labelStart,
                styled.length,
                Spanned.SPAN_EXCLUSIVE_EXCLUSIVE,
            )
            styled.append('\n').append(value)
        }
        detailsText.text = styled
        copyDetails.setOnClickListener {
            val webViewVersion =
                runCatching { WebView.getCurrentWebViewPackage()?.versionName }.getOrNull()
                    ?: context.getString(R.string.status_version_unavailable)
            val copied =
                buildString {
                    append(detailsText.text)
                    append("\n\n").append(context.getString(R.string.status_time_utc))
                    append('\n').append(Instant.ofEpochMilli(details.timeMillis))
                    append("\n\n").append(context.getString(R.string.status_android_api))
                    append('\n').append(Build.VERSION.SDK_INT)
                    append("\n\n").append(context.getString(R.string.status_webview_version))
                    append('\n').append(webViewVersion)
                }
            val clipboard = ContextCompat.getSystemService(context, ClipboardManager::class.java)
            clipboard?.setPrimaryClip(
                ClipData.newPlainText(context.getString(R.string.status_details), copied),
            )
            if (android.os.Build.VERSION.SDK_INT <= 32) {
                Toast.makeText(context, R.string.status_copied, Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun breakableHost(value: String): String =
        value.replace(".", ".\u200b").replace("-", "-\u200b")

    private fun safeErrorName(value: String): String =
        value.takeIf {
            it in
                setOf(
                    "ERROR_HOST_LOOKUP",
                    "ERROR_CONNECT",
                    "ERROR_IO",
                    "ERROR_TIMEOUT",
                    "ERROR_FAILED_SSL_HANDSHAKE",
                    "SSL_NOTYETVALID",
                    "SSL_EXPIRED",
                    "SSL_IDMISMATCH",
                    "SSL_UNTRUSTED",
                    "SSL_DATE_INVALID",
                    "SSL_INVALID",
                    "UnknownHostException",
                    "ConnectException",
                    "SocketTimeoutException",
                    "IOException",
                    "NetworkUnavailable",
                    "InvalidResponse",
                    "UnsafeRedirect",
                    "WorkspaceChanged",
                    "UnsafeCookie",
                    "MissingCookie",
                    "UnexpectedLanding",
                    "Rejected",
                    "WorkspaceRequired",
                    "UnsupportedWebView",
                    "RENDERER_CRASH",
                ) || safeWebViewError.matches(it) || safeHttpStatus.matches(it)
        } ?: "ERROR_UNKNOWN"

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density + .5f).toInt()
}
