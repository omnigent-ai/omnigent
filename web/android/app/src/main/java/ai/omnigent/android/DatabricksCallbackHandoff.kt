package ai.omnigent.android

import android.content.Context

/** Non-secret lifecycle marker that distinguishes Custom Tabs handoff from user cancellation. */
internal class DatabricksCallbackHandoff(
    context: Context,
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val preferences =
        context.getSharedPreferences("ai.omnigent.android.oauth-callback", Context.MODE_PRIVATE)

    fun markInProgress() {
        preferences.edit().putLong(KEY_STARTED_AT, now()).commit()
    }

    fun isInProgress(): Boolean {
        val startedAt = preferences.getLong(KEY_STARTED_AT, 0L)
        val age = now() - startedAt
        return startedAt > 0 && age in 0..MAX_AGE_MS
    }

    fun clear() {
        preferences.edit().remove(KEY_STARTED_AT).commit()
    }

    private companion object {
        const val KEY_STARTED_AT = "started_at_ms"
        const val MAX_AGE_MS = 2 * 60 * 1_000L
    }
}
