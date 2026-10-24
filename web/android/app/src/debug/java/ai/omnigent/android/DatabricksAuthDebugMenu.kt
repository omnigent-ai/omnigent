package ai.omnigent.android

import android.view.Menu
import java.util.concurrent.CompletableFuture

/** Debug-only faults for exercising workspace authentication recovery against a live deployment. */
internal object DatabricksAuthDebugMenu {
    private const val MENU_ROOT = 20_000
    const val CLEAR_SESSION_COOKIE = 20_001
    const val EXPIRE_ACCESS_TOKEN = 20_002
    const val REJECT_ACCESS_TOKEN = 20_003
    const val CLEAR_REFRESH_GRANT = 20_004

    /** The faults, in menu order; the sidebar server picker lists them too. */
    val ACTIONS =
        listOf(
            AuthDebugAction(CLEAR_SESSION_COOKIE, "Clear Session Cookie"),
            AuthDebugAction(EXPIRE_ACCESS_TOKEN, "Expire Access Token"),
            AuthDebugAction(REJECT_ACCESS_TOKEN, "Reject Access Token"),
            AuthDebugAction(CLEAR_REFRESH_GRANT, "Clear Refresh Grant"),
        )

    fun addItems(menu: Menu) {
        val debug = menu.addSubMenu(3, MENU_ROOT, 0, "Debug Authentication")
        ACTIONS.forEachIndexed { order, action -> debug.add(3, action.id, order, action.label) }
    }

    fun handles(itemId: Int): Boolean = itemId in CLEAR_SESSION_COOKIE..CLEAR_REFRESH_GRANT

    fun run(
        itemId: Int,
        tokens: DatabricksTokenManager,
        context: DatabricksWebContext,
        profile: DatabricksWebProfile,
        session: DatabricksWebSession,
        now: Long = System.currentTimeMillis(),
    ): CompletableFuture<String> =
        when (itemId) {
            CLEAR_SESSION_COOKIE -> {
                profile.clearSessionCookie(session).thenApply { cleared ->
                    if (cleared) {
                        "Session cookie cleared. Background and reopen the app, or navigate, to trigger silent recovery."
                    } else {
                        "No workspace session cookie was available to clear."
                    }
                }
            }

            EXPIRE_ACCESS_TOKEN -> {
                completed(
                    tokens.replaceStored(context.scope) { saved ->
                        saved.copy(
                            accessToken = "debug-expired-access-token",
                            expiresAtEpochMillis = 1L,
                        )
                    },
                    "Access token expired. The next native recovery should refresh silently without Auth Tab.",
                )
            }

            REJECT_ACCESS_TOKEN -> {
                completed(
                    tokens.replaceStored(context.scope) { saved ->
                        saved.copy(
                            accessToken = "debug-rejected-access-token",
                            expiresAtEpochMillis =
                                maxOf(
                                    saved.expiresAtEpochMillis,
                                    now + 10 * 60 * 1_000L,
                                ),
                        )
                    },
                    "Rejected access token installed. The next native recovery should get one 401, refresh, and retry.",
                )
            }

            CLEAR_REFRESH_GRANT -> {
                completed(
                    tokens.clearStoredIfPresent(context.scope),
                    "Saved OAuth grant cleared. The next native recovery should ask before opening Auth Tab.",
                )
            }

            else -> {
                failed(IllegalArgumentException("Unknown authentication debug fault"))
            }
        }

    private fun completed(
        changed: Boolean,
        message: String,
    ): CompletableFuture<String> =
        CompletableFuture.completedFuture(
            if (changed) message else "No saved credentials exist for this workspace.",
        )

    private fun failed(error: Throwable): CompletableFuture<String> =
        CompletableFuture<String>().also { it.completeExceptionally(error) }
}
