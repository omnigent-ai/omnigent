package ai.omnigent.intellij.config

import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.util.xmlb.XmlSerializerUtil

/**
 * App-level persisted settings: a single manual server URL override. This is
 * the thin IntelliJ adapter for the pure `Settings` interface in
 * ServerTarget.kt — everything else in config/ is pure and testable without
 * an IDE host.
 */
@Service(Service.Level.APP)
@State(name = "OmnigentSettings", storages = [Storage("omnigent.xml")])
class OmnigentSettings : PersistentStateComponent<OmnigentSettings.State> {
    class State {
        var serverUrl: String = ""
    }

    private var state = State()

    override fun getState(): State = state

    override fun loadState(state: State) {
        XmlSerializerUtil.copyBean(state, this.state)
    }

    /** Adapt to the pure `Settings` interface consumed by resolveServerTarget. */
    fun toSettings(): Settings = object : Settings {
        override val serverUrl: String get() = state.serverUrl
    }

    /** Convenience accessor for the Configurable UI (§OmnigentConfigurable). */
    var serverUrl: String
        get() = state.serverUrl
        set(value) {
            state.serverUrl = value
        }

    companion object {
        fun getInstance(): OmnigentSettings =
            ApplicationManager.getApplication().getService(OmnigentSettings::class.java)
    }
}
