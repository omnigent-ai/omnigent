package ai.omnigent.android

import android.content.Context
import android.content.SharedPreferences
import android.os.Looper
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import java.net.URI
import java.util.UUID
import java.util.concurrent.ExecutionException
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
class DatabricksWorkspaceLifecycleTest {
    private val appContext = ApplicationProvider.getApplicationContext<Context>()

    @Test
    fun `recovery requires a ready page and cooldown after the first attempt`() {
        val policy = DatabricksRecoveryPolicy(cooldownMillis = 60_000)

        assertTrue(policy.begin(1_000))
        assertFalse(policy.begin(62_000))
        policy.markReady()
        assertTrue(policy.begin(62_000))
        policy.markReady()
        assertFalse(policy.begin(100_000))
        assertTrue(policy.begin(123_000))
    }

    @Test
    fun `sign out clears scoped credentials profile data and durable marker`() {
        val fixture = fixture()
        val manager = DatabricksSignOutManager(appContext, fixture.tokens)
        val backend = FakeBackend().apply { cookies = true }
        val profile = DatabricksWebProfile(fixture.context.profileName, backend)

        val result = manager.begin(fixture.context, profile)
        shadowOf(Looper.getMainLooper()).idle()
        result.get(1, TimeUnit.SECONDS)

        assertNull(fixture.store.value)
        assertFalse(backend.cookies)
        assertTrue(backend.storageCleared)
        assertFalse(manager.isPending(fixture.context))
    }

    @Test
    fun `sign out that can't save its marker clears nothing and reports failure`() {
        val fixture = fixture()
        val saved =
            appContext.getSharedPreferences(
                "workspace-signout-${UUID.randomUUID()}",
                Context.MODE_PRIVATE,
            )
        val unwritable =
            object : SharedPreferences by saved {
                override fun edit(): SharedPreferences.Editor {
                    val real = saved.edit()
                    return object : SharedPreferences.Editor by real {
                        override fun putStringSet(
                            key: String?,
                            values: MutableSet<String>?,
                        ): SharedPreferences.Editor = apply { real.putStringSet(key, values) }

                        override fun commit() = false
                    }
                }
            }
        val manager = DatabricksSignOutManager(appContext, fixture.tokens, unwritable)
        val backend = FakeBackend().apply { cookies = true }
        val profile = DatabricksWebProfile(fixture.context.profileName, backend)

        val result = manager.begin(fixture.context, profile)
        shadowOf(Looper.getMainLooper()).idle()

        val failure =
            assertThrows(ExecutionException::class.java) { result.get(1, TimeUnit.SECONDS) }
        assertEquals(CredentialStorageException.Unavailable::class, failure.cause!!::class)
        assertNotNull(fixture.store.value)
        assertTrue(backend.cookies)
        assertFalse(backend.storageCleared)
        assertFalse(manager.isPending(fixture.context))
    }

    @Test
    fun `an interrupted sign-out is resumed at startup and only then`() {
        val fixture = fixture()
        val preferences =
            appContext.getSharedPreferences(
                "workspace-signout-${UUID.randomUUID()}",
                Context.MODE_PRIVATE,
            )
        val manager = DatabricksSignOutManager(appContext, fixture.tokens, preferences)
        val backend = FakeBackend().apply { cookies = true }
        val profile = DatabricksWebProfile(fixture.context.profileName, backend)

        assertFalse(manager.resumeInterrupted(fixture.context, profile))
        assertNotNull(fixture.store.value)

        preferences
            .edit()
            .putStringSet(
                "pending_profiles",
                setOf(fixture.context.profileName),
            ).commit()
        assertTrue(manager.resumeInterrupted(fixture.context, profile))
        shadowOf(Looper.getMainLooper()).idle()

        assertNull(fixture.store.value)
        assertFalse(backend.cookies)
        assertFalse(manager.isPending(fixture.context))
    }

    @Test
    fun `pending cleanup is finished before a later connection`() {
        val fixture = fixture()
        appContext
            .getSharedPreferences("ai.omnigent.android.workspace-signout", Context.MODE_PRIVATE)
            .edit()
            .putStringSet("pending_profiles", setOf(fixture.context.profileName))
            .commit()
        val manager = DatabricksSignOutManager(appContext, fixture.tokens)
        val backend = FakeBackend().apply { cookies = true }

        val result =
            manager.finishPending(
                fixture.context,
                DatabricksWebProfile(fixture.context.profileName, backend),
            )
        shadowOf(Looper.getMainLooper()).idle()
        result.get(1, TimeUnit.SECONDS)

        assertNull(fixture.store.value)
        assertFalse(backend.cookies)
        assertFalse(manager.isPending(fixture.context))
    }

    private fun fixture(): Fixture {
        val client = "client-${UUID.randomUUID()}"
        val configuration =
            DatabricksOAuthConfiguration(
                client,
                URI("https://login.databricks.com/mobile-redirect"),
            )
        val uri = URI("https://dbc-123.cloud.databricks.com/omnigent?o=42")
        val context =
            DatabricksWebContext(
                configuration,
                DatabricksCredentialScope.from(uri, configuration),
                uri,
            )
        val store =
            MemoryStore(
                DatabricksOAuthTokens(
                    "access",
                    "refresh",
                    Long.MAX_VALUE,
                    DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc")),
                ),
            )
        return Fixture(context, store, DatabricksTokenManager(store))
    }

    private data class Fixture(
        val context: DatabricksWebContext,
        val store: MemoryStore,
        val tokens: DatabricksTokenManager,
    )

    private class MemoryStore(
        var value: DatabricksOAuthTokens?,
    ) : DatabricksCredentialStorage {
        override fun load(scope: DatabricksCredentialScope) = value

        override fun save(
            scope: DatabricksCredentialScope,
            tokens: DatabricksOAuthTokens,
        ) {
            value = tokens
        }

        override fun delete(scope: DatabricksCredentialScope) {
            value = null
        }
    }

    private class FakeBackend : WebProfileBackend {
        var cookies = false
        var storageCleared = false

        override fun setAcceptCookie(accept: Boolean) = Unit

        override fun removeAllCookies(callback: (Boolean) -> Unit) {
            cookies = false
            callback(true)
        }

        override fun setCookie(
            url: String,
            value: String,
            callback: (Boolean) -> Unit,
        ) = callback(true)

        override fun getCookie(url: String): String? = if (cookies) "DBAUTH=session" else null

        override fun flushCookies() = Unit

        override fun clearWebStorage() {
            storageCleared = true
        }
    }
}
