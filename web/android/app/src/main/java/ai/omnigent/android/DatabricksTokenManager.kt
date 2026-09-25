package ai.omnigent.android

import android.content.Context
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** Process-wide coordinator for rotating refresh grants. */
internal class DatabricksTokenManager(
    private val store: DatabricksCredentialStorage,
    private val client: DatabricksTokenRefreshing = DatabricksOAuthClient(),
    private val executor: ExecutorService = Executors.newCachedThreadPool(),
    private val now: () -> Long = System::currentTimeMillis,
) {
    private val refreshes = mutableMapOf<DatabricksCredentialScope, Refresh>()
    private val generations = mutableMapOf<DatabricksCredentialScope, UUID>()
    private val pendingWrites = mutableMapOf<DatabricksCredentialScope, Write>()

    @Synchronized
    fun tokens(scope: DatabricksCredentialScope): CompletableFuture<DatabricksOAuthTokens?> =
        lookup(scope, forceRefresh = false)

    @Synchronized
    fun refreshRejected(
        scope: DatabricksCredentialScope,
        rejected: DatabricksOAuthTokens,
    ): CompletableFuture<DatabricksOAuthTokens?> {
        flushPendingWrite(scope)
        val current = store.load(scope) ?: return CompletableFuture.completedFuture(null)
        if (current != rejected) throw CredentialStorageException.Changed()
        return lookup(scope, forceRefresh = true)
    }

    @Synchronized
    fun save(
        scope: DatabricksCredentialScope,
        tokens: DatabricksOAuthTokens,
    ) {
        if (!tokens.isValid) throw CredentialStorageException.InvalidData()
        invalidate(scope)
        pendingWrites[scope] = Write.Save(tokens)
        flushPendingWrite(scope)
    }

    @Synchronized
    fun clear(scope: DatabricksCredentialScope) {
        invalidate(scope)
        pendingWrites[scope] = Write.Delete
        flushPendingWrite(scope)
    }

    @Synchronized
    fun isCurrent(
        scope: DatabricksCredentialScope,
        tokens: DatabricksOAuthTokens,
    ): Boolean {
        if (pendingWrites.containsKey(scope)) return false
        return store.load(scope) == tokens
    }

    @Synchronized
    private fun lookup(
        scope: DatabricksCredentialScope,
        forceRefresh: Boolean,
    ): CompletableFuture<DatabricksOAuthTokens?> {
        flushPendingWrite(scope)
        refreshes[scope]?.let { return detached(it.future) }
        val saved = store.load(scope) ?: return CompletableFuture.completedFuture(null)
        if (!saved.isValid) throw CredentialStorageException.InvalidData()
        if (!forceRefresh && saved.expiresAtEpochMillis > now() + REFRESH_MARGIN_MS) {
            return CompletableFuture.completedFuture(saved)
        }
        return startRefresh(scope, saved)
    }

    private fun startRefresh(
        scope: DatabricksCredentialScope,
        saved: DatabricksOAuthTokens,
    ): CompletableFuture<DatabricksOAuthTokens?> {
        val id = UUID.randomUUID()
        val generation = generations[scope]
        val future = CompletableFuture<DatabricksOAuthTokens?>()
        refreshes[scope] = Refresh(id, future)
        executor.execute {
            val result = runCatching { client.refresh(saved.refreshToken, scope, saved.issuer) }
            finishRefresh(scope, id, generation, result)
        }
        return detached(future)
    }

    @Synchronized
    private fun finishRefresh(
        scope: DatabricksCredentialScope,
        id: UUID,
        generation: UUID?,
        result: Result<DatabricksOAuthTokens>,
    ) {
        val refresh = refreshes[scope]
        if (refresh?.id != id) return
        refreshes.remove(scope)
        if (generations[scope] != generation) {
            refresh.future.completeExceptionally(CancellationException())
            return
        }
        try {
            val tokens =
                result.getOrElse { error ->
                    if (error is DatabricksOAuthException.InvalidRefreshGrant) {
                        pendingWrites[scope] = Write.Delete
                        flushPendingWrite(scope)
                        refresh.future.complete(null)
                        return
                    }
                    throw error
                }
            pendingWrites[scope] = Write.Save(tokens)
            flushPendingWrite(scope)
            refresh.future.complete(tokens)
        } catch (error: Throwable) {
            refresh.future.completeExceptionally(error)
        }
    }

    private fun flushPendingWrite(scope: DatabricksCredentialScope) {
        when (val write = pendingWrites[scope] ?: return) {
            is Write.Save -> store.save(scope, write.tokens)
            Write.Delete -> store.delete(scope)
        }
        pendingWrites.remove(scope)
    }

    private fun invalidate(scope: DatabricksCredentialScope) {
        generations[scope] = UUID.randomUUID()
        refreshes.remove(scope)?.future?.completeExceptionally(CancellationException())
    }

    /** A dependent future can be cancelled without cancelling the shared refresh. */
    private fun detached(source: CompletableFuture<DatabricksOAuthTokens?>) =
        source.thenApply { it }

    private sealed interface Write {
        data class Save(
            val tokens: DatabricksOAuthTokens,
        ) : Write

        data object Delete : Write
    }

    private data class Refresh(
        val id: UUID,
        val future: CompletableFuture<DatabricksOAuthTokens?>,
    )

    companion object {
        private const val REFRESH_MARGIN_MS = 60_000L

        @Volatile private var shared: DatabricksTokenManager? = null

        fun shared(context: Context): DatabricksTokenManager =
            shared ?: synchronized(this) {
                shared ?: DatabricksTokenManager(
                    DatabricksCredentialStore(context.applicationContext),
                ).also { shared = it }
            }
    }
}
