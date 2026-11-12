package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class WorkspaceChromeScriptTest {
    /**
     * The rule must key on Omnigent's own embed root, not on the monolith-owned
     * workspace nav markup, and must win over it (fixed + full-inset + top layer)
     * even when a workspace ancestor would trap a fixed descendant.
     */
    @Test
    fun `css promotes the embed root to a full-viewport overlay`() {
        assertEquals(
            """
            .omnigent-app {
              position: fixed !important;
              inset: 0 !important;
              z-index: 2147483647 !important;
            }
            :has(.omnigent-app) {
              transform: none !important;
              translate: none !important;
              rotate: none !important;
              scale: none !important;
              perspective: none !important;
              transform-style: flat !important;
              filter: none !important;
              backdrop-filter: none !important;
              -webkit-backdrop-filter: none !important;
              contain: none !important;
              container-type: normal !important;
              content-visibility: visible !important;
              will-change: auto !important;
              clip-path: none !important;
              mask: none !important;
              z-index: auto !important;
              isolation: auto !important;
              opacity: 1 !important;
              mix-blend-mode: normal !important;
            }
            """.trimIndent(),
            WorkspaceChromeScript.css,
        )
    }

    /**
     * The script runs on every finished load, so re-running it on a document that
     * already carries the stylesheet must not stack duplicate `<style>` nodes.
     */
    @Test
    fun `source installs the stylesheet at most once per document`() {
        val source = WorkspaceChromeScript.source

        assertTrue(
            source.contains("""document.querySelector("style[data-omnigent-workspace-chrome]")"""),
        )
        assertTrue(source.contains("return;"))
        assertTrue(source.contains("document.documentElement.appendChild(style)"))
    }

    /**
     * The CSS reaches the page as a JSON-encoded string literal (see [jsString]), so
     * a quote or newline in it can't break out of the surrounding script.
     */
    @Test
    fun `source embeds the css as an escaped string literal`() {
        assertTrue(WorkspaceChromeScript.source.contains(jsString(WorkspaceChromeScript.css)))
        // trimIndent leaves real newlines in the CSS; they must be escaped, not raw.
        assertTrue(WorkspaceChromeScript.source.contains("\\n"))
    }
}
