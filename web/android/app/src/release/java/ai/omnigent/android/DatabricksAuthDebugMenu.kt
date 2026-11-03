package ai.omnigent.android

import android.view.Menu
import java.util.concurrent.CompletableFuture

/** Shipping builds expose no authentication fault-injection surface. */
internal object DatabricksAuthDebugMenu {
    fun addItems(menu: Menu) = Unit

    fun handles(itemId: Int): Boolean = false

    fun run(
        itemId: Int,
        tokens: DatabricksTokenManager,
        context: DatabricksWebContext,
        profile: DatabricksWebProfile,
        session: DatabricksWebSession,
        now: Long = System.currentTimeMillis(),
    ): CompletableFuture<String> =
        CompletableFuture<String>().also {
            it.completeExceptionally(UnsupportedOperationException("Unavailable in release builds"))
        }
}
