package ai.omnigent.android

import android.view.Menu

/** Debug-only faults for exercising native OIDC renewal and the sign-in prompt by hand. */
internal object OidcAuthDebugMenu {
    private const val MENU_ROOT = 20_100
    const val CLEAR_SESSION_COOKIE = 20_101
    const val CLEAR_REFRESH_TOKEN = 20_102

    /** Debug builds keep the server pill reachable so these faults stay one tap away. */
    const val IS_AVAILABLE = true

    fun addItems(menu: Menu) {
        val debug = menu.addSubMenu(3, MENU_ROOT, 0, "Debug Authentication")
        debug.add(3, CLEAR_SESSION_COOKIE, 0, "Clear Session Cookie")
        debug.add(3, CLEAR_REFRESH_TOKEN, 1, "Clear Refresh Token")
    }

    fun handles(itemId: Int): Boolean =
        itemId == CLEAR_SESSION_COOKIE || itemId == CLEAR_REFRESH_TOKEN

    /** Injects the fault and reports what to expect next. */
    fun run(
        itemId: Int,
        session: OidcSessionController,
        report: (String) -> Unit,
    ) {
        when (itemId) {
            CLEAR_SESSION_COOKIE -> {
                session.clearSessionCookie { cleared ->
                    report(
                        if (cleared) {
                            "Session cookie cleared. Leave and reopen the app, or navigate, to renew it silently."
                        } else {
                            "No session cookie was available to clear."
                        },
                    )
                }
            }

            CLEAR_REFRESH_TOKEN -> {
                report(
                    if (session.forgetGrant()) {
                        "Refresh token forgotten. The next renewal asks you to sign in."
                    } else {
                        "No saved refresh token for this server. Sign in first."
                    },
                )
            }

            else -> {
                report("Unknown authentication debug fault.")
            }
        }
    }
}
