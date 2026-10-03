package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
class DatabricksTokenManagerTest {
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val scope =
        DatabricksCredentialScope.from(
            URI("https://dbc-123.cloud.databricks.com?o=42"),
            configuration,
        )
    private val issuer = DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc"))

    @Test
    fun `returns a current saved grant without refreshing`() {
        val saved = tokens("access", "refresh", 120_000L)
        val store = FakeStore(saved)
        val calls = AtomicInteger()
        val manager =
            manager(store, refresher = { _, _, _ ->
                calls.incrementAndGet()
                saved
            })

        assertEquals(saved, manager.tokens(scope).get(1, TimeUnit.SECONDS))
        assertEquals(0, calls.get())
    }

    @Test
    fun `callers share one refresh and a cancelled waiter does not cancel rotation`() {
        val store = FakeStore(tokens("old", "old-refresh", 1L))
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val calls = AtomicInteger()
        val rotated = tokens("next", "next-refresh", 200_000L)
        val manager =
            manager(
                store,
                refresher = { _, _, _ ->
                    calls.incrementAndGet()
                    entered.countDown()
                    release.await(5, TimeUnit.SECONDS)
                    rotated
                },
            )

        val first = manager.tokens(scope)
        assertTrue(entered.await(1, TimeUnit.SECONDS))
        val cancelled = manager.tokens(scope)
        val second = manager.tokens(scope)
        cancelled.cancel(true)
        release.countDown()

        assertEquals(rotated, first.get(1, TimeUnit.SECONDS))
        assertEquals(rotated, second.get(1, TimeUnit.SECONDS))
        assertEquals(rotated, store.value)
        assertEquals(1, calls.get())
    }

    @Test
    fun `invalid grant clears only that scope while transient errors retain it`() {
        val original = tokens("old", "old-refresh", 1L)
        val invalidStore = FakeStore(original)
        val invalid =
            manager(
                invalidStore,
                refresher = { _, _, _ -> throw DatabricksOAuthException.InvalidRefreshGrant() },
            )
        assertNull(invalid.tokens(scope).get(1, TimeUnit.SECONDS))
        assertNull(invalidStore.value)

        val transientStore = FakeStore(original)
        val transient =
            manager(
                transientStore,
                refresher = { _, _, _ -> throw DatabricksOAuthException.NetworkUnavailable() },
            )
        val error =
            assertThrows(ExecutionException::class.java) {
                transient.tokens(scope).get(1, TimeUnit.SECONDS)
            }
        assertTrue(error.cause is DatabricksOAuthException.NetworkUnavailable)
        assertEquals(original, transientStore.value)
    }

    @Test
    fun `rotated grant persistence is retried before the consumed grant can be reused`() {
        val original = tokens("old", "consumed-refresh", 1L)
        val rotated = tokens("next", "rotated-refresh", 200_000L)
        val store = FakeStore(original).apply { failedSaves = 1 }
        val manager = manager(store, refresher = { _, _, _ -> rotated })

        val firstError =
            assertThrows(ExecutionException::class.java) {
                manager.tokens(scope).get(1, TimeUnit.SECONDS)
            }
        assertTrue(firstError.cause is CredentialStorageException.Unavailable)
        assertEquals(original, store.value)

        assertEquals(rotated, manager.tokens(scope).get(1, TimeUnit.SECONDS))
        assertEquals(rotated, store.value)
    }

    @Test
    fun `clear fences a late refresh result`() {
        val store = FakeStore(tokens("old", "refresh", 1L))
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val manager =
            manager(
                store,
                refresher = { _, _, _ ->
                    entered.countDown()
                    release.await(5, TimeUnit.SECONDS)
                    tokens("late", "late-refresh", 200_000L)
                },
            )

        val refresh = manager.tokens(scope)
        assertTrue(entered.await(1, TimeUnit.SECONDS))
        manager.clear(scope)
        release.countDown()

        assertThrows(Exception::class.java) { refresh.get(1, TimeUnit.SECONDS) }
        Thread.sleep(50)
        assertNull(store.value)
    }

    @Test
    fun `rejected access token refresh requires the same saved snapshot`() {
        val current = tokens("current", "refresh", 200_000L)
        val manager = manager(FakeStore(current), refresher = { _, _, _ -> current })

        assertThrows(CredentialStorageException.Changed::class.java) {
            manager.refreshRejected(scope, tokens("other", "refresh", 200_000L))
        }
    }

    private fun manager(
        store: FakeStore,
        refresher: DatabricksTokenRefreshing,
    ) = DatabricksTokenManager(
        store,
        refresher,
        Executors.newSingleThreadExecutor(),
    ) { 0L }

    private fun tokens(
        access: String,
        refresh: String,
        expires: Long,
    ) = DatabricksOAuthTokens(access, refresh, expires, issuer)

    private class FakeStore(
        var value: DatabricksOAuthTokens?,
    ) : DatabricksCredentialStorage {
        var failedSaves = 0

        override fun load(scope: DatabricksCredentialScope): DatabricksOAuthTokens? = value

        override fun save(
            scope: DatabricksCredentialScope,
            tokens: DatabricksOAuthTokens,
        ) {
            if (failedSaves > 0) {
                failedSaves--
                throw CredentialStorageException.Unavailable()
            }
            value = tokens
        }

        override fun delete(scope: DatabricksCredentialScope) {
            value = null
        }
    }
}
