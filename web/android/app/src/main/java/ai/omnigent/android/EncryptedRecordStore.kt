package ai.omnigent.android

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import java.security.MessageDigest
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal interface RecordCipher {
    fun encrypt(
        plaintext: ByteArray,
        associatedData: ByteArray,
    ): ByteArray

    fun decrypt(
        ciphertext: ByteArray,
        associatedData: ByteArray,
    ): ByteArray
}

/** App-private encrypted records whose key material never leaves Android Keystore. */
internal class AndroidKeystoreRecordCipher(
    private val alias: String,
) : RecordCipher {
    override fun encrypt(
        plaintext: ByteArray,
        associatedData: ByteArray,
    ): ByteArray {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        cipher.updateAAD(associatedData)
        val encrypted = cipher.doFinal(plaintext)
        return byteArrayOf(FORMAT_VERSION) + cipher.iv + encrypted
    }

    override fun decrypt(
        ciphertext: ByteArray,
        associatedData: ByteArray,
    ): ByteArray {
        if (ciphertext.size <= 1 + IV_SIZE || ciphertext[0] != FORMAT_VERSION) {
            throw CredentialStorageException.InvalidData()
        }
        val iv = ciphertext.copyOfRange(1, 1 + IV_SIZE)
        val encrypted = ciphertext.copyOfRange(1 + IV_SIZE, ciphertext.size)
        return try {
            Cipher.getInstance(TRANSFORMATION).run {
                init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv))
                updateAAD(associatedData)
                doFinal(encrypted)
            }
        } catch (_: Throwable) {
            throw CredentialStorageException.InvalidData()
        }
    }

    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(alias, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec
                .Builder(
                    alias,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                ).setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setUnlockedDeviceRequired(true)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_SIZE = 12
        const val FORMAT_VERSION: Byte = 1
    }
}

internal class EncryptedRecordStore(
    context: Context,
    namespace: String,
    private val cipher: RecordCipher =
        AndroidKeystoreRecordCipher("ai.omnigent.android.$namespace.v1"),
) {
    private val preferences =
        context.getSharedPreferences("ai.omnigent.android.$namespace", Context.MODE_PRIVATE)
    private val aadPrefix = "ai.omnigent.android.$namespace.v1:"

    @Synchronized
    fun read(key: String): ByteArray? {
        val encoded = preferences.getString(recordKey(key), null) ?: return null
        return try {
            cipher.decrypt(Base64.getDecoder().decode(encoded), associatedData(key))
        } catch (error: CredentialStorageException) {
            throw error
        } catch (_: Throwable) {
            throw CredentialStorageException.InvalidData()
        }
    }

    @Synchronized
    fun write(
        key: String,
        value: ByteArray,
    ) {
        val encrypted =
            try {
                cipher.encrypt(value, associatedData(key))
            } catch (error: CredentialStorageException) {
                throw error
            } catch (_: Throwable) {
                throw CredentialStorageException.Unavailable()
            }
        val committed =
            preferences
                .edit()
                .putString(recordKey(key), Base64.getEncoder().encodeToString(encrypted))
                .commit()
        if (!committed) throw CredentialStorageException.Unavailable()
    }

    @Synchronized
    fun remove(key: String) {
        if (!preferences.edit().remove(recordKey(key)).commit()) {
            throw CredentialStorageException.Unavailable()
        }
    }

    private fun recordKey(key: String): String =
        MessageDigest
            .getInstance("SHA-256")
            .digest(key.toByteArray())
            .joinToString("") { "%02x".format(it.toInt() and 0xff) }

    private fun associatedData(key: String): ByteArray = (aadPrefix + key).toByteArray()
}

sealed class CredentialStorageException(
    message: String,
) : Exception(message) {
    class Unavailable :
        CredentialStorageException(
            "Could not access saved Databricks credentials. Unlock the device and try again.",
        )

    class InvalidData :
        CredentialStorageException("The saved Databricks credentials are invalid. Sign in again.")

    class Changed :
        CredentialStorageException("Workspace credentials changed. Reconnect to continue.")
}
