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

    fun addItems(menu: Menu) {
        val debug = menu.addSubMenu(3, MENU_ROOT, 0, "Debug Authentication")
        debug.add(3, CLEAR_SESSION_COOKIE, 0, "Clear Session Cookie")
        debug.add(3, EXPIRE_ACCESS_TOKEN, 1, "Expire Access Token")
        debug.add(3, REJECT_ACCESS_TOKEN, 2, "Reject Access Token")
        debug.add(3, CLEAR_REFRESH_GRANT, 3, "Clear Refresh Grant")
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
