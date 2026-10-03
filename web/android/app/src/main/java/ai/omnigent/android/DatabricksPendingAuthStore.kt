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
    @Synchronized
    fun begin(attempt: DatabricksOAuthAttempt): PendingAuth {
        val pending = PendingAuth(UUID.randomUUID().toString(), now(), attempt)
        records.write(RECORD_KEY, encode(pending))
        return pending
    }

    @Synchronized
    fun load(): PendingAuth? {
        val data = records.read(RECORD_KEY) ?: return null
        val pending = decode(data)
        if (now() - pending.createdAtEpochMillis > MAX_AGE_MS ||
            pending.createdAtEpochMillis > now()
        ) {
            records.remove(RECORD_KEY)
            return null
        }
        return pending
    }

    @Synchronized
    fun consume(expectedId: String): PendingAuth? {
        val pending = load() ?: return null
        if (pending.id != expectedId) return null
        records.remove(RECORD_KEY)
        return pending
    }

    @Synchronized
    fun clear() {
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
        const val RECORD_KEY = "pending"
        const val MAX_AGE_MS = 10 * 60 * 1_000L
    }
}
