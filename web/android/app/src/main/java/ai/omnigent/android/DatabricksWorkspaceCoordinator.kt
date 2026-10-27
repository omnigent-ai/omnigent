package ai.omnigent.android

import android.content.Context
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** Obtains a native grant, creates a web session, and installs it into the scoped profile. */
internal class DatabricksWorkspaceCoordinator(
    context: Context,
    private val tokens: DatabricksTokenManager = DatabricksTokenManager.shared(context),
    private val sessions: DatabricksSessionCreating = DatabricksSessionClient(),
    private val executor: ExecutorService = Executors.newCachedThreadPool(),
    private val signOuts: DatabricksSignOutManager = DatabricksSignOutManager(context, tokens),
) {
    fun prepare(
        context: DatabricksWebContext,
        profile: DatabricksWebProfile,
    ): CompletableFuture<DatabricksWebSession> =
        CompletableFuture.supplyAsync(
            {
                await(signOuts.finishPending(context, profile))
                var credential =
                    await(tokens.tokens(context.scope))
                        ?: throw DatabricksSessionException.ReauthenticationRequired()
                var session =
                    try {
                        sessions.create(context, credential)
                    } catch (error: DatabricksSessionException.Rejected) {
                        if (error.status != 401) throw error
                        credential =
                            await(tokens.refreshRejected(context.scope, credential))
                                ?: throw DatabricksSessionException.ReauthenticationRequired()
                        sessions.create(context, credential)
                    }
                if (!tokens.isCurrent(context.scope, credential)) {
                    throw CredentialStorageException.Changed()
                }
                await(profile.install(session))
                if (!tokens.isCurrent(context.scope, credential)) {
                    throw CredentialStorageException.Changed()
                }
                session
            },
            executor,
        )

    fun shutdown() {
        executor.shutdownNow()
    }

    private fun <T> await(future: CompletableFuture<T>): T =
        try {
            future.get()
        } catch (error: ExecutionException) {
            throw error.cause ?: error
        }
}
