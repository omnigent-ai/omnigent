package ai.omnigent.android

import android.content.Context
import org.json.JSONObject

/** An OIDC server's refresh grant; the session token itself lives only in the cookie. */
internal data class OidcRefreshGrant(
    val refreshToken: String,
    val userId: String?,
) {
    override fun toString(): String = "OidcRefreshGrant(userId=$userId)"
}

/** Refresh grants keyed by normalized server origin ([originOf]). */
internal interface OidcCredentialStorage {
    fun load(origin: String): OidcRefreshGrant?

    fun save(
        origin: String,
        grant: OidcRefreshGrant,
    )

    fun delete(origin: String)
}

/** Keystore-encrypted grants in app-private storage; `allowBackup=false` keeps them on device. */
internal class OidcCredentialStore(
    context: Context,
    private val records: EncryptedRecordStore = EncryptedRecordStore(context, "oidc-grants"),
) : OidcCredentialStorage {
    override fun load(origin: String): OidcRefreshGrant? {
        val data = records.read(origin) ?: return null
        try {
            val record = JSONObject(data.toString(Charsets.UTF_8))
            val refreshToken = record.getString("refresh_token")
            if (record.getInt("version") != VERSION || refreshToken.isEmpty()) {
                throw CredentialStorageException.InvalidData()
            }
            val userId = record.optString("user_id").takeIf(String::isNotEmpty)
            return OidcRefreshGrant(refreshToken, userId)
        } catch (error: CredentialStorageException) {
            throw error
        } catch (_: Exception) {
            throw CredentialStorageException.InvalidData()
        }
    }

    override fun save(
        origin: String,
        grant: OidcRefreshGrant,
    ) {
        if (grant.refreshToken.isEmpty()) throw CredentialStorageException.InvalidData()
        val record =
            JSONObject()
                .put("version", VERSION)
                .put("refresh_token", grant.refreshToken)
                .apply { grant.userId?.let { put("user_id", it) } }
        records.write(origin, record.toString().toByteArray())
    }

    override fun delete(origin: String) {
        records.remove(origin)
    }

    private companion object {
        const val VERSION = 1
    }
}
