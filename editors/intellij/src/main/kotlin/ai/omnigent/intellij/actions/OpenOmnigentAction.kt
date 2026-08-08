package ai.omnigent.intellij.actions

import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.wm.ToolWindowManager

/** Focuses the Omnigent tool window from the Tools menu / Find Action. */
class OpenOmnigentAction : AnAction() {
    override fun actionPerformed(e: AnActionEvent) {
        val project = e.project ?: return
        ToolWindowManager.getInstance(project).getToolWindow("Omnigent")?.activate(null)
    }
}
