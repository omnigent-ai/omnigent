package ai.omnigent.intellij.toolwindow

import ai.omnigent.intellij.config.DiscoverySummary
import ai.omnigent.intellij.config.OmnigentSettings
import ai.omnigent.intellij.config.resolveServerTarget
import ai.omnigent.intellij.discovery.LocalDiscovery
import ai.omnigent.intellij.discovery.defaultDiscoveryIO
import ai.omnigent.intellij.discovery.discoverLocalServer
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Disposer
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.components.JBLabel
import com.intellij.ui.content.ContentFactory
import com.intellij.ui.jcef.JBCefApp
import com.intellij.ui.jcef.JBCefBrowser
import java.awt.BorderLayout
import javax.swing.JPanel

/**
 * JCEF tool window: live-navigates to the resolved local server. JCEF is
 * same-origin full Chromium, so no CSP/iframe-bundling layer is needed — the
 * render surface re-asserts the loopback gate via chooseToolWindowContent
 * instead (see ToolWindowContent.kt).
 */
class OmnigentToolWindowFactory : ToolWindowFactory, DumbAware {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val panel = JPanel(BorderLayout())
        val content = ContentFactory.getInstance().createContent(panel, "", false)
        toolWindow.contentManager.addContent(content)

        val jcefSupported = JBCefApp.isSupported()
        if (chooseToolWindowContent(jcefSupported, null) == ContentKind.JcefGuidance) {
            panel.add(JBLabel(jcefGuidanceHtml), BorderLayout.CENTER)
            return
        }

        val browser = JBCefBrowser()
        // Release native CEF resources when the tool window / project closes.
        Disposer.register(content, browser)
        panel.add(browser.component, BorderLayout.CENTER)
        browser.loadHTML(resolvingHtml)

        ApplicationManager.getApplication().executeOnPooledThread {
            val resolution = resolveServerTarget(OmnigentSettings.getInstance().toSettings(), discoverySummary())
            ApplicationManager.getApplication().invokeLater(
                {
                    if (project.isDisposed || Disposer.isDisposed(content)) return@invokeLater
                    when (val kind = chooseToolWindowContent(jcefSupported, resolution)) {
                        is ContentKind.Navigate -> browser.loadURL(kind.url)
                        else -> browser.loadHTML(noServerHtml)
                    }
                },
                ModalityState.any(),
            )
        }
    }
}

/** Bridge discovery's LocalDiscovery to the pure resolver's DiscoverySummary. */
private fun discoverySummary(): DiscoverySummary {
    return when (val d = discoverLocalServer(defaultDiscoveryIO)) {
        is LocalDiscovery.Found -> DiscoverySummary(found = true, baseUrl = d.baseUrl, health = d.health)
        is LocalDiscovery.NotFound -> DiscoverySummary(found = false)
    }
}

private val jcefGuidanceHtml = "<html><b>Omnigent</b> requires a JCEF-capable runtime.<br><br>" +
    "The embedded browser (JCEF) is not available in this IDE's runtime.<br>" +
    "Switch to a JetBrains Runtime (JBR) with JCEF:<br>" +
    "&nbsp;&nbsp;Help → Find Action → \"Choose Boot Java Runtime for the IDE\"<br>" +
    "&nbsp;&nbsp;and select a JBR build that includes JCEF.</html>"

private val resolvingHtml = """
    <!DOCTYPE html><html><body style="font-family:sans-serif;display:flex;align-items:center;
    justify-content:center;height:100vh;margin:0;color:#888"><p>Resolving Omnigent server…</p></body></html>
""".trimIndent()

private val noServerHtml = """
    <!DOCTYPE html><html><body style="font-family:sans-serif;padding:1rem">
    <h3>Omnigent — no server found</h3>
    <p>No Omnigent server was auto-discovered and no manual server URL is configured.</p>
    <p>Start a local server with <code>omnigent server</code>, or set a loopback
    <code>Server URL</code> in Settings → Tools → Omnigent, then reopen this tool window.</p>
    </body></html>
""".trimIndent()
