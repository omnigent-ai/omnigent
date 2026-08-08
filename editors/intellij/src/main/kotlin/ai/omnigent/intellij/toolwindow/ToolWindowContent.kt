package ai.omnigent.intellij.toolwindow

import ai.omnigent.intellij.config.HostType
import ai.omnigent.intellij.config.TargetResolution

/**
 * Pure tool-window content decision (mirrors panel/host.ts `shouldUseIframe`
 * plus the placeholder/no-server branching, minus any IDE dependency). The
 * Factory adapter is a thin caller of this function; JCEF availability and the
 * resolved target are the only inputs.
 */
sealed class ContentKind {
    object JcefGuidance : ContentKind()
    object Resolving : ContentKind()
    data class Navigate(val url: String) : ContentKind()
    object NoServer : ContentKind()
}

/**
 * Choose what the tool window should show.
 *  - no JCEF-capable JBR -> JcefGuidance
 *  - resolution not yet available -> Resolving
 *  - resolved to a loopback target -> Navigate(url)
 *  - resolved to a non-loopback target (defense-in-depth) or needs-prompt -> NoServer
 */
fun chooseToolWindowContent(jcefSupported: Boolean, resolution: TargetResolution?): ContentKind {
    if (!jcefSupported) return ContentKind.JcefGuidance
    if (resolution == null) return ContentKind.Resolving
    return when (resolution) {
        is TargetResolution.Resolved ->
            if (resolution.target.hostType == HostType.LOCAL) {
                ContentKind.Navigate(resolution.target.baseUrl)
            } else {
                ContentKind.NoServer
            }
        is TargetResolution.NeedsPrompt -> ContentKind.NoServer
    }
}
