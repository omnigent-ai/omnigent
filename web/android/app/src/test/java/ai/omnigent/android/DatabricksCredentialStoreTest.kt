package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.security.MessageDigest
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class DatabricksCredentialStoreTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
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
    fun `credential record roundtrips and deletes by exact scope`() {
        val records = records()
        val store = DatabricksCredentialStore(context, records)
        val tokens = DatabricksOAuthTokens("access", "refresh", 123_000L, issuer)

        store.save(scope, tokens)

        assertEquals(tokens, store.load(scope))
        val other =
            DatabricksCredentialScope.from(
                URI("https://dbc-123.cloud.databricks.com?o=43"),
                configuration,
            )
        assertNull(store.load(other))
        store.delete(scope)
        assertNull(store.load(scope))
    }

    @Test
    fun `issuer aware and legacy records use distinct validated versions`() {
        val records = records()
        val store = DatabricksCredentialStore(context, records)
        store.save(scope, DatabricksOAuthTokens("a", "r", 10L, null))
        assertNull(store.load(scope)!!.issuer)

        records.write(
            scope.account,
            """{"version":1,"access_token":"a","refresh_token":"r","expires_at_ms":10,"issuer":"${issuer.uri}"}"""
                .toByteArray(),
        )
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            store.load(scope)
        }
    }

    @Test
    fun `malformed or tampered encrypted records fail closed`() {
        val records = records()
        val store = DatabricksCredentialStore(context, records)
        records.write(scope.account, "not-json".toByteArray())
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            store.load(scope)
        }

        val cipher = TestRecordCipher()
        val isolated =
            EncryptedRecordStore(
                context,
                "tamper-${UUID.randomUUID()}",
                cipher,
            )
        isolated.write("scope-a", "secret".toByteArray())
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            cipher.decrypt(cipher.lastCiphertext!!, "wrong-scope".toByteArray())
        }
    }

    private fun records() =
        EncryptedRecordStore(
            context,
            "test-oauth-${UUID.randomUUID()}",
            TestRecordCipher(),
        )

    private class TestRecordCipher : RecordCipher {
        var lastCiphertext: ByteArray? = null

        override fun encrypt(
            plaintext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = (digest(associatedData) + plaintext).also { lastCiphertext = it }

        override fun decrypt(
            ciphertext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray {
            val prefix = digest(associatedData)
            if (!ciphertext.take(prefix.size).toByteArray().contentEquals(prefix)) {
                throw CredentialStorageException.InvalidData()
            }
            return ciphertext.copyOfRange(prefix.size, ciphertext.size)
        }

        private fun digest(value: ByteArray) =
            MessageDigest.getInstance("SHA-256").digest(value).copyOf(8)
    }
}
