package ai.omnigent.android

import android.content.Context
import org.json.JSONObject
import java.net.URI
import java.util.UUID

/** Encrypted one-shot browser attempt, retained across Android process death. */
internal class DatabricksPendingAuthStore(
    context: Context,
    private val records: EncryptedRecordStore =
        EncryptedRecordStore(context, "databricks-oauth-attempt"),
    private val now: () -> Long = System::currentTimeMillis,
) {
    // Each sign-in manager builds its own store over the same record, so an instance lock
    // would let two callback deliveries consume one attempt.
    fun begin(attempt: DatabricksOAuthAttempt): PendingAuth =
        synchronized(LOCK) {
            val pending = PendingAuth(UUID.randomUUID().toString(), now(), attempt)
            records.write(RECORD_KEY, encode(pending))
            pending
        }

    fun load(): PendingAuth? =
        synchronized(LOCK) {
            val data = records.read(RECORD_KEY) ?: return null
            val pending = decode(data)
            if (now() - pending.createdAtEpochMillis > MAX_AGE_MS ||
                pending.createdAtEpochMillis > now()
            ) {
                records.remove(RECORD_KEY)
                return null
            }
            pending
        }

    fun consume(expectedId: String): PendingAuth? =
        synchronized(LOCK) {
            val pending = load() ?: return null
            if (pending.id != expectedId) return null
            records.remove(RECORD_KEY)
            pending
        }

    fun clear() =
        synchronized(LOCK) {
            records.remove(RECORD_KEY)
        }

    data class PendingAuth(
        val id: String,
        val createdAtEpochMillis: Long,
        val attempt: DatabricksOAuthAttempt,
    )

    private fun encode(pending: PendingAuth): ByteArray =
        JSONObject()
            .put("version", 1)
            .put("id", pending.id)
            .put("created_at_ms", pending.createdAtEpochMillis)
            .put("client_id", pending.attempt.configuration.clientId)
            .put(
                "redirect_uri",
                pending.attempt.configuration.redirectUri
                    .toString(),
            ).put(
                "workspace_origin",
                pending.attempt.credentialScope.workspaceOrigin
                    .toString(),
            ).apply {
                pending.attempt.credentialScope.workspaceId
                    ?.let { put("workspace_id", it) }
            }.put("state", pending.attempt.state)
            .put("verifier", pending.attempt.verifier)
            .toString()
            .toByteArray()

    private fun decode(data: ByteArray): PendingAuth {
        try {
            val record = JSONObject(data.toString(Charsets.UTF_8))
            if (record.getInt("version") != 1) throw CredentialStorageException.InvalidData()
            val configuration =
                DatabricksOAuthConfiguration(
                    record.getString("client_id"),
                    URI(record.getString("redirect_uri")),
                )
            val workspaceId = record.optString("workspace_id").takeIf(String::isNotEmpty)
            val workspaceUrl =
                URI(
                    record.getString("workspace_origin") +
                        (workspaceId?.let { "?o=${formEncode(it)}" } ?: ""),
                )
            val attempt =
                DatabricksOAuthAttempt(
                    configuration,
                    DatabricksCredentialScope.from(workspaceUrl, configuration),
                    record.getString("state"),
                    record.getString("verifier"),
                )
            return PendingAuth(
                record.getString("id"),
                record.getLong("created_at_ms"),
                attempt,
            )
        } catch (error: CredentialStorageException) {
            throw error
        } catch (_: Throwable) {
            throw CredentialStorageException.InvalidData()
        }
    }

    private companion object {
        val LOCK = Any()
        const val RECORD_KEY = "pending"
        const val MAX_AGE_MS = 10 * 60 * 1_000L
    }
}
