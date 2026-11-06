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
class OidcPendingSignInStoreTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val namespace = "test-oidc-pending-${UUID.randomUUID()}"

    @Test
    fun `an attempt survives a new store instance and is consumed once`() {
        var now = 1_000L
        val started = store { now }.begin("https://omni.example/omnigent", "ap_session", "s", "v")
        now += 9 * 60 * 1_000L
        val restored = store { now }

        assertEquals(started, restored.load())
        assertNull(restored.consume("another-attempt"))
        assertEquals(started, restored.consume(started.id))
        assertNull(restored.consume(started.id))
        assertNull(restored.load())
    }

    @Test
    fun `an attempt expires after ten minutes or when the clock runs backwards`() {
        var now = 1_000L
        val pending = store { now }
        pending.begin("https://omni.example", "ap_session", "s", "v")

        now += 10 * 60 * 1_000L + 1
        assertNull(pending.load())

        now = 1_000L
        pending.begin("https://omni.example", "ap_session", "s", "v")
        now = 999L
        assertNull(pending.load())
    }

    @Test
    fun `a new attempt replaces the old one and clear removes it`() {
        val pending = store { 1_000L }
        val first = pending.begin("https://a.example", "ap_session", "s1", "v1")
        val second = pending.begin("https://b.example", "__Host-ap_session", "s2", "v2")

        assertNull(pending.consume(first.id))
        assertEquals(second, pending.load())
        pending.clear()
        assertNull(pending.load())
    }

    @Test
    fun `a malformed record is invalid and never prints its secrets`() {
        val records = EncryptedRecordStore(context, namespace, PlainCipher)
        records.write("pending", """{"version":1,"id":"x"}""".toByteArray())

        assertThrows(CredentialStorageException.InvalidData::class.java) {
            OidcPendingSignInStore(context, records) { 1_000L }.load()
        }
        val pending =
            OidcPendingSignInStore.PendingSignIn(
                "id",
                1L,
                "https://h",
                "c",
                "state",
                "verifier",
            )
        assertEquals("PendingSignIn(id=id, serverUrl=https://h)", pending.toString())
    }

    private fun store(now: () -> Long) =
        OidcPendingSignInStore(context, EncryptedRecordStore(context, namespace, PlainCipher), now)

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
