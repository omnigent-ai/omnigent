package ai.omnigent.android

import android.content.ActivityNotFoundException
import android.content.Intent
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContract
import androidx.core.app.ActivityOptionsCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class OidcSignInLauncherTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val credentials =
        OidcCredentials(
            store =
                object : OidcCredentialStorage {
                    override fun load(origin: String): OidcRefreshGrant? = null

                    override fun save(
                        origin: String,
                        grant: OidcRefreshGrant,
                    ) = Unit

                    override fun delete(origin: String) = Unit
                },
            pending =
                OidcPendingSignInStore(
                    context,
                    EncryptedRecordStore(context, "test-oidc-${UUID.randomUUID()}", PlainCipher),
                ),
            transport = { throw AssertionError("no network expected") },
        )
    private val handoff = OAuthCallbackHandoff(context)

    @Test
    fun `the sign-in opens in a browser for the app's redirect scheme`() {
        handoff.markInProgress()
        val launcher = RecordingLauncher()

        assertTrue(
            OidcSignInLauncher(
                credentials,
                handoff,
            ).start(launcher, "https://omni.example", "ap_session"),
        )

        val intent = launcher.launched.single()
        assertEquals("https", intent.data?.scheme)
        assertEquals("/auth/login", intent.data?.path)
        assertEquals(
            "ai.omnigent.android",
            intent.data?.getQueryParameter("native_redirect_uri")?.substringBefore(':'),
        )
        assertFalse(handoff.isInProgress())
    }

    @Test
    fun `a browser that can't open discards the attempt`() {
        val launcher = RecordingLauncher(fail = true)

        assertFalse(
            OidcSignInLauncher(
                credentials,
                handoff,
            ).start(launcher, "https://omni.example", "ap_session"),
        )

        assertThrows(OidcSignInException.InvalidCallback::class.java) {
            credentials.completeSignIn(URI("ai.omnigent.android:/oauth/callback?state=s&code=c"))
        }
    }

    private class RecordingLauncher(
        private val fail: Boolean = false,
    ) : ActivityResultLauncher<Intent>() {
        val launched = mutableListOf<Intent>()

        override val contract: ActivityResultContract<Intent, *>
            get() = throw UnsupportedOperationException()

        override fun launch(
            input: Intent,
            options: ActivityOptionsCompat?,
        ) {
            if (fail) throw ActivityNotFoundException("no browser")
            launched += input
        }

        override fun unregister() = Unit
    }

    private object PlainCipher : RecordCipher {
        override fun encrypt(
            plaintext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = plaintext

        override fun decrypt(
            ciphertext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = ciphertext
    }
}
