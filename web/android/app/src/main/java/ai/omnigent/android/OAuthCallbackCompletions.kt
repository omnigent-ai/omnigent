package ai.omnigent.android

import java.util.concurrent.CompletableFuture
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/**
 * Completes each browser callback once per process, so a callback activity recreated mid
 * exchange (rotation, for instance) attaches to the running completion instead of repeating it.
 */
internal object OAuthCallbackCompletions {
    /** Runs completions; replaced in tests to hold one open. */
    @Volatile
    var executor: Executor = Executors.newSingleThreadExecutor { Thread(it, "oauth-callback") }

    private val running = mutableMapOf<String, CompletableFuture<String?>>()

    /** The completion of [callback], started with [work] unless one is already running. */
    @Synchronized
    fun start(
        callback: String,
        work: () -> Unit,
    ): CompletableFuture<String?> =
        running.getOrPut(callback) {
            CompletableFuture.supplyAsync(
                {
                    runCatching(
                        work,
                    ).exceptionOrNull()?.let { it.message ?: it.javaClass.simpleName }
                },
                executor,
            )
        }

    /** Forgets [callback] once an activity has returned its result to the app. */
    @Synchronized
    fun finish(callback: String) {
        running.remove(callback)
    }
}
