package ai.omnigent.intellij.config

import com.intellij.openapi.options.Configurable
import com.intellij.ui.components.JBTextField
import com.intellij.util.ui.FormBuilder
import javax.swing.JComponent
import javax.swing.JPanel

/**
 * Settings → Tools → Omnigent: a single Server URL field bound to
 * OmnigentSettings. Restores parity with VS Code's auto-generated
 * `omnigent.serverUrl` settings row, which IntelliJ does not provide for free
 * from a bare PersistentStateComponent.
 */
class OmnigentConfigurable : Configurable {
    private val settings = OmnigentSettings.getInstance()
    private var serverUrlField: JBTextField? = null

    override fun getDisplayName(): String = "Omnigent"

    override fun createComponent(): JComponent {
        val field = JBTextField(settings.serverUrl, 40)
        serverUrlField = field
        return FormBuilder.createFormBuilder()
            .addLabeledComponent("Server URL:", field)
            .addComponentFillVertically(JPanel(), 0)
            .panel
    }

    override fun isModified(): Boolean = serverUrlField?.text != settings.serverUrl

    override fun apply() {
        serverUrlField?.let { settings.serverUrl = it.text }
    }

    override fun reset() {
        serverUrlField?.text = settings.serverUrl
    }
}
