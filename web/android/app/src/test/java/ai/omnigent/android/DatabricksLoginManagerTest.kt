package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContract
import androidx.activity.result.contract.ActivityResultContracts
import androidx.browser.auth.AuthTabIntent
import androidx.core.app.ActivityOptionsCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.UUID
import java.util.concurrent.Executors

@RunWith(RobolectricTestRunner::class)
class DatabricksLoginManagerTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("ai.omnigent.android://mobile-redirect"),
        )

    @Test
    fun `Auth Tab attempt survives manager recreation and callback saves tokens`() {
        val namespace = "login-${UUID.randomUUID()}"
        val cipher = PlainRecordCipher()
        val firstPending =
            DatabricksPendingAuthStore(
                context,
                EncryptedRecordStore(context, namespace, cipher),
            )
        val tokenStore = MemoryCredentialStore()
        val transport = RecordingTransport()
        val oauth = DatabricksOAuthClient(transport) { 1_000L }
        val tokenManager =
            DatabricksTokenManager(
                tokenStore,
                oauth,
                Executors.newSingleThreadExecutor(),
            )
        val first =
            DatabricksLoginManager(
                context,
                configuration,
                firstPending,
                oauth,
                tokenManager,
            )
        val launcher = RecordingLauncher()

        assertTrue(
            first.start(
                launcher,
                URI("https://dbc-123.cloud.databricks.com/omnigent?o=42"),
            ),
        )
        val browserIntent = launcher.launched!!
        assertEquals("/oidc/v1/authorize", browserIntent.data!!.path)
        assertEquals(
            configuration.redirectUri.scheme,
            browserIntent.getStringExtra(AuthTabIntent.EXTRA_REDIRECT_SCHEME),
        )
        assertNull(browserIntent.getStringExtra(AuthTabIntent.EXTRA_HTTPS_REDIRECT_HOST))
        val saved = firstPending.load()!!

        val issuer =
            saved.attempt.credentialScope.workspaceOrigin
                .resolve("/oidc")
        transport.responses +=
            OAuthHttpResponse(
                URI("$issuer/.well-known/openid-configuration"),
                200,
                """{"issuer":"$issuer","token_endpoint":"$issuer/v1/token"}"""
                    .toByteArray(),
            )
        transport.responses +=
            OAuthHttpResponse(
                URI("$issuer/v1/token"),
                200,
                """{"access_token":"access","refresh_token":"refresh","token_type":"bearer","expires_in":3600}"""
                    .toByteArray(),
            )
        val recreated =
            DatabricksLoginManager(
                context,
                configuration,
                DatabricksPendingAuthStore(
                    context,
                    EncryptedRecordStore(context, namespace, cipher),
                ),
                oauth,
                tokenManager,
            )
        val callback =
            URI(
                "${configuration.redirectUri}?code=code&state=${saved.attempt.state}",
            )

        val tokens = recreated.complete(callback)

        assertEquals("access", tokens.accessToken)
        assertEquals(tokens, tokenStore.value)
        assertNull(firstPending.load())
        assertThrows(DatabricksOAuthException.InvalidCallback::class.java) {
            recreated.complete(callback)
        }
    }

    @Test
    fun `an HTTPS override still launches a verified HTTPS Auth Tab`() {
        val httpsConfiguration =
            configuration.copy(redirectUri = URI("https://login.databricks.com/mobile-redirect"))
        val manager =
            DatabricksLoginManager(
                context,
                httpsConfiguration,
                DatabricksPendingAuthStore(
                    context,
                    EncryptedRecordStore(
                        context,
                        "https-${UUID.randomUUID()}",
                        PlainRecordCipher(),
                    ),
                ),
                DatabricksOAuthClient(RecordingTransport()),
                DatabricksTokenManager(MemoryCredentialStore()),
            )
        val launcher = RecordingLauncher()

        assertTrue(manager.start(launcher, URI("https://dbc-123.cloud.databricks.com")))
        assertEquals(
            httpsConfiguration.redirectUri.host,
            launcher.launched!!.getStringExtra(AuthTabIntent.EXTRA_HTTPS_REDIRECT_HOST),
        )
        assertEquals(
            httpsConfiguration.redirectUri.path,
            launcher.launched!!.getStringExtra(AuthTabIntent.EXTRA_HTTPS_REDIRECT_PATH),
        )
    }

    @Test
    fun `the private redirect reaches the compatibility receiver`() {
        val intent =
            Intent(Intent.ACTION_VIEW, Uri.parse(configuration.redirectUri.toString()))
                .addCategory(Intent.CATEGORY_BROWSABLE)
        val receivers = context.packageManager.queryIntentActivities(intent, 0)

        assertTrue(
            receivers.any {
                it.activityInfo.name == OAuthCallbackActivity::class.java.name
            },
        )
    }

    @Test
    fun `cancel removes pending attempt before a late callback`() {
        val pending =
            DatabricksPendingAuthStore(
                context,
                EncryptedRecordStore(
                    context,
                    "cancel-${UUID.randomUUID()}",
                    PlainRecordCipher(),
                ),
            )
        val store = MemoryCredentialStore()
        val manager =
            DatabricksLoginManager(
                context,
                configuration,
                pending,
                DatabricksOAuthClient(RecordingTransport()),
                DatabricksTokenManager(store),
            )
        manager.start(RecordingLauncher(), URI("https://dbc-123.cloud.databricks.com"))
        val state = pending.load()!!.attempt.state

        manager.cancel()

        assertNull(pending.load())
        assertThrows(DatabricksOAuthException.InvalidCallback::class.java) {
            manager.complete(URI("${configuration.redirectUri}?code=x&state=$state"))
        }
        assertNull(store.value)
    }

    @Test
    fun `wrong callback state does not consume the current attempt`() {
        val pending =
            DatabricksPendingAuthStore(
                context,
                EncryptedRecordStore(
                    context,
                    "wrong-state-${UUID.randomUUID()}",
                    PlainRecordCipher(),
                ),
            )
        val manager =
            DatabricksLoginManager(
                context,
                configuration,
                pending,
                DatabricksOAuthClient(RecordingTransport()),
                DatabricksTokenManager(MemoryCredentialStore()),
            )
        manager.start(RecordingLauncher(), URI("https://dbc-123.cloud.databricks.com"))

        assertThrows(DatabricksOAuthException.InvalidCallback::class.java) {
            manager.complete(
                URI("${configuration.redirectUri}?code=x&state=wrong"),
            )
        }
        assertFalse(pending.load() == null)
    }

    private class RecordingLauncher : ActivityResultLauncher<Intent>() {
        var launched: Intent? = null

        override fun launch(
            input: Intent,
            options: ActivityOptionsCompat?,
        ) {
            launched = input
        }

        override fun unregister() = Unit

        override val contract: ActivityResultContract<Intent, *> =
            ActivityResultContracts.StartActivityForResult()
    }

    private class RecordingTransport : OAuthTransport {
        val responses = ArrayDeque<OAuthHttpResponse>()

        override fun execute(request: OAuthHttpRequest): OAuthHttpResponse = responses.removeFirst()
    }

    private class MemoryCredentialStore : DatabricksCredentialStorage {
        var value: DatabricksOAuthTokens? = null

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

    private class PlainRecordCipher : RecordCipher {
        override fun encrypt(
            plaintext: ByteArray,
            associatedData: ByteArray,
        ) = plaintext

        override fun decrypt(
            ciphertext: ByteArray,
            associatedData: ByteArray,
        ) = ciphertext
    }
}
