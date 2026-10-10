package ai.omnigent.android

import android.content.Context
import org.json.JSONObject
import java.util.UUID

/** The one native OIDC sign-in in progress; [OidcPendingSignInStore] keeps it on device. */
internal interface OidcPendingSignIns {
    fun begin(
        serverUrl: String,
        cookieName: String,
        state: String,
        verifier: String,
    ): OidcPendingSignInStore.PendingSignIn

    fun load(): OidcPendingSignInStore.PendingSignIn?

    fun consume(expectedId: String): OidcPendingSignInStore.PendingSignIn?

    fun clear()
}

/**
 * The one native OIDC sign-in in progress, encrypted so it survives Android killing the app
 * while the browser is in front. Consumed once by the callback that completes it.
 */
internal class OidcPendingSignInStore(
    context: Context,
    private val records: EncryptedRecordStore =
        EncryptedRecordStore(context, "oidc-sign-in-attempt"),
    private val now: () -> Long = System::currentTimeMillis,
) : OidcPendingSignIns {
    data class PendingSignIn(
        val id: String,
        val createdAtEpochMillis: Long,
        /** The clean server URL (origin plus mount) the sign-in is for. */
        val serverUrl: String,
        /** The session cookie the server named in its manifest. */
        val cookieName: String,
        val state: String,
        val verifier: String,
    ) {
        override fun toString(): String = "PendingSignIn(id=$id, serverUrl=$serverUrl)"
    }

    @Synchronized
    override fun begin(
        serverUrl: String,
        cookieName: String,
        state: String,
        verifier: String,
    ): PendingSignIn {
        val pending =
            PendingSignIn(
                UUID.randomUUID().toString(),
                now(),
                serverUrl,
                cookieName,
                state,
                verifier,
            )
        records.write(RECORD_KEY, encode(pending))
        return pending
    }

    /** The attempt in progress; an expired one is discarded. */
    @Synchronized
    override fun load(): PendingSignIn? {
        val data = records.read(RECORD_KEY) ?: return null
        val pending = decode(data)
        val age = now() - pending.createdAtEpochMillis
        if (age !in 0..MAX_AGE_MS) {
            records.remove(RECORD_KEY)
            return null
        }
        return pending
    }

    /** Removes and returns the attempt, only if it is still [expectedId]. */
    @Synchronized
    override fun consume(expectedId: String): PendingSignIn? {
        val pending = load() ?: return null
        if (pending.id != expectedId) return null
        records.remove(RECORD_KEY)
        return pending
    }

    @Synchronized
    override fun clear() {
        records.remove(RECORD_KEY)
    }

    private fun encode(pending: PendingSignIn): ByteArray =
        JSONObject()
            .put("version", VERSION)
            .put("id", pending.id)
            .put("created_at_ms", pending.createdAtEpochMillis)
            .put("server_url", pending.serverUrl)
            .put("cookie_name", pending.cookieName)
            .put("state", pending.state)
            .put("verifier", pending.verifier)
            .toString()
            .toByteArray()

    private fun decode(data: ByteArray): PendingSignIn {
        try {
            val record = JSONObject(data.toString(Charsets.UTF_8))
            if (record.getInt("version") != VERSION) throw CredentialStorageException.InvalidData()
            return PendingSignIn(
                id = record.getString("id"),
                createdAtEpochMillis = record.getLong("created_at_ms"),
                serverUrl = record.getString("server_url"),
                cookieName = record.getString("cookie_name"),
                state = record.getString("state"),
                verifier = record.getString("verifier"),
            )
        } catch (error: CredentialStorageException) {
            throw error
        } catch (_: Exception) {
            throw CredentialStorageException.InvalidData()
        }
    }

    private companion object {
        const val VERSION = 1
        const val RECORD_KEY = "pending"
        const val MAX_AGE_MS = 10 * 60 * 1_000L
    }
}
