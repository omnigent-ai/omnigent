package ai.omnigent.android

import android.view.Menu

/** Shipping builds expose no authentication fault-injection surface. */
internal object OidcAuthDebugMenu {
    const val IS_AVAILABLE = false

    fun addItems(menu: Menu) = Unit

    fun handles(itemId: Int): Boolean = false

    fun run(
        itemId: Int,
        session: OidcSessionController,
        report: (String) -> Unit,
    ) = report("Unavailable in release builds.")
}
