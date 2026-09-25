package ai.omnigent.android

import android.content.Context
import java.util.concurrent.CompletableFuture

internal class DatabricksRecoveryPolicy(
    private val cooldownMillis: Long = 60_000L,
) {
    private var lastAttempt: Long? = null
    private var pageWasReady = false

    @Synchronized
    fun markReady() {
        pageWasReady = true
    }

    @Synchronized
    fun begin(nowEpochMillis: Long = System.currentTimeMillis()): Boolean {
        val previous = lastAttempt
        if (previous != null && (!pageWasReady || nowEpochMillis - previous < cooldownMillis)) {
            return false
        }
        lastAttempt = nowEpochMillis
        pageWasReady = false
        return true
    }
}

/** Durable local cleanup intent for one workspace WebView profile. */
internal class DatabricksSignOutManager(
    context: Context,
    private val tokens: DatabricksTokenManager = DatabricksTokenManager.shared(context),
) {
    private val preferences =
        context.getSharedPreferences("ai.omnigent.android.workspace-signout", Context.MODE_PRIVATE)
    private val jobs = mutableMapOf<String, CompletableFuture<Void>>()

    @Synchronized
    fun isPending(context: DatabricksWebContext): Boolean =
        preferences.getStringSet(PENDING_KEY, emptySet()).orEmpty().contains(context.profileName)

    @Synchronized
    fun begin(
        context: DatabricksWebContext,
        profile: DatabricksWebProfile,
    ): CompletableFuture<Void> {
        jobs[context.profileName]?.let { return it }
        setPending(context.profileName, true)
        val result = CompletableFuture<Void>()
        jobs[context.profileName] = result
        try {
            tokens.clear(context.scope)
            profile.clear().whenComplete { _, error ->
                val completionError =
                    synchronized(this) {
                        jobs.remove(context.profileName)
                        error
                            ?: runCatching {
                                setPending(
                                    context.profileName,
                                    false,
                                )
                            }.exceptionOrNull()
                    }
                if (completionError == null) {
                    result.complete(null)
                } else {
                    result.completeExceptionally(completionError)
                }
            }
        } catch (error: Throwable) {
            jobs.remove(context.profileName)
            result.completeExceptionally(error)
        }
        return result
    }

    fun finishPending(
        context: DatabricksWebContext,
        profile: DatabricksWebProfile,
    ): CompletableFuture<Void> =
        if (isPending(context)) begin(context, profile) else CompletableFuture.completedFuture(null)

    @Synchronized
    private fun setPending(
        profileName: String,
        pending: Boolean,
    ) {
        val names = preferences.getStringSet(PENDING_KEY, emptySet()).orEmpty().toMutableSet()
        if (pending) names += profileName else names -= profileName
        if (!preferences.edit().putStringSet(PENDING_KEY, names).commit()) {
            throw CredentialStorageException.Unavailable()
        }
    }

    private companion object {
        const val PENDING_KEY = "pending_profiles"
    }
}
