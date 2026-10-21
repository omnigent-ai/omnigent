package ai.omnigent.android

import android.security.keystore.KeyPermanentlyInvalidatedException
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.security.KeyStoreException
import javax.crypto.KeyGenerator

@RunWith(RobolectricTestRunner::class)
class AndroidKeystoreRecordCipherTest {
    private val aad = "record".toByteArray()
    private val key = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
    private val sealed =
        AndroidKeystoreRecordCipher("test") {
            key
        }.encrypt("secret".toByteArray(), aad)

    @Test
    fun `a record decrypts with its key`() {
        assertArrayEquals(
            "secret".toByteArray(),
            AndroidKeystoreRecordCipher("test") { key }.decrypt(sealed, aad),
        )
    }

    @Test
    fun `a key that can't be used now leaves the record unavailable, not invalid`() {
        val locked =
            AndroidKeystoreRecordCipher("test") { throw KeyStoreException("device locked") }

        assertThrows(CredentialStorageException.Unavailable::class.java) {
            locked.decrypt(sealed, aad)
        }
    }

    @Test
    fun `a tampered record or a permanently invalidated key is invalid`() {
        val tampered = sealed.copyOf().also { it[it.lastIndex] = (it.last() + 1).toByte() }
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            AndroidKeystoreRecordCipher("test") { key }.decrypt(tampered, aad)
        }
        val otherKey = KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            AndroidKeystoreRecordCipher("test") { otherKey }.decrypt(sealed, aad)
        }
        assertThrows(CredentialStorageException.InvalidData::class.java) {
            AndroidKeystoreRecordCipher("test") { throw KeyPermanentlyInvalidatedException() }
                .decrypt(sealed, aad)
        }
    }
}
