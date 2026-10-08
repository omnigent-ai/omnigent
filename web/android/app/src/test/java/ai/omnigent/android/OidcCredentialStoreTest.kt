package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class OidcCredentialStoreTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()

    @Test
    fun `a grant round-trips per origin and deletes`() {
        val store = OidcCredentialStore(context, records())
        val grant = OidcRefreshGrant("refresh-1", "alice@example.com")

        store.save("https://omni.example", grant)

        assertEquals(grant, store.load("https://omni.example"))
        assertNull(store.load("https://omni.example:8443"))
        store.save("https://omni.example", OidcRefreshGrant("refresh-2", null))
        assertEquals(OidcRefreshGrant("refresh-2", null), store.load("https://omni.example"))
        store.delete("https://omni.example")
        assertNull(store.load("https://omni.example"))
    }

    @Test
    fun `malformed or foreign records are invalid`() {
        val records = records()
        val store = OidcCredentialStore(context, records)
        listOf(
            "not json",
            """{"version":2,"refresh_token":"r"}""",
            """{"version":1,"refresh_token":""}""",
            """{"version":1}""",
        ).forEach { record ->
            records.write("https://omni.example", record.toByteArray())
            assertThrows(record, CredentialStorageException.InvalidData::class.java) {
                store.load("https://omni.example")
            }
        }
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            store.save("https://omni.example", OidcRefreshGrant("", null))
        }
    }

    @Test
    fun `a grant never prints its refresh token`() {
        assertEquals(
            "OidcRefreshGrant(userId=alice@example.com)",
            OidcRefreshGrant("secret", "alice@example.com").toString(),
        )
    }

    private fun records() =
        EncryptedRecordStore(context, "test-oidc-${UUID.randomUUID()}", PlainCipher)

    private object PlainCipher : RecordCipher {
        override fun encrypt(
            plaintext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = associatedData + 0 + plaintext

        override fun decrypt(
            ciphertext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = ciphertext.copyOfRange(associatedData.size + 1, ciphertext.size)
    }
}
