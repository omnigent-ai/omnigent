package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class DatabricksPendingAuthStoreTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val attempt =
        DatabricksOAuthAttempt(
            configuration,
            DatabricksCredentialScope.from(
                URI("https://dbc-123.cloud.databricks.com?o=42"),
                configuration,
            ),
            "state",
            "verifier",
        )

    @Test
    fun `pending attempt survives a new store instance and is consumed once`() {
        val namespace = "pending-${UUID.randomUUID()}"
        val cipher = PlainRecordCipher()
        val first =
            DatabricksPendingAuthStore(
                context,
                EncryptedRecordStore(context, namespace, cipher),
            ) { 1_000L }
        val pending = first.begin(attempt)
        val restored =
            DatabricksPendingAuthStore(
                context,
                EncryptedRecordStore(context, namespace, cipher),
            ) { 2_000L }

        assertEquals(attempt, restored.load()!!.attempt)
        assertEquals(pending, restored.consume(pending.id))
        assertNull(restored.load())
        assertNull(restored.consume(pending.id))
    }

    @Test
    fun `expired future and malformed attempts are removed or rejected`() {
        val records =
            EncryptedRecordStore(
                context,
                "pending-expiry-${UUID.randomUUID()}",
                PlainRecordCipher(),
            )
        DatabricksPendingAuthStore(context, records) { 0L }.begin(attempt)
        assertNull(DatabricksPendingAuthStore(context, records) { 11 * 60 * 1_000L }.load())

        records.write("pending", "not-json".toByteArray())
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            DatabricksPendingAuthStore(context, records).load()
        }
    }

    private class PlainRecordCipher : RecordCipher {
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
