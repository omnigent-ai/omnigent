package ai.omnigent.android

import org.json.JSONObject

/** A debug-build authentication fault, offered in the native menu and the sidebar server picker. */
internal data class AuthDebugAction(
    val id: Int,
    val label: String,
) {
    /** The picker payload entry, `{"id": …, "label": …}`. */
    fun toJson(): JSONObject = JSONObject().put("id", id).put("label", label)
}
