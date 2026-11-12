package ai.omnigent.android

import android.content.Context
import org.json.JSONObject
import java.net.URI

internal interface DatabricksCredentialStorage {
    fun load(scope: DatabricksCredentialScope): DatabricksOAuthTokens?

    fun save(
        scope: DatabricksCredentialScope,
        tokens: DatabricksOAuthTokens,
    )

    fun delete(scope: DatabricksCredentialScope)
}

internal class DatabricksCredentialStore(
    context: Context,
    private val records: EncryptedRecordStore =
        EncryptedRecordStore(context, "databricks-oauth"),
) : DatabricksCredentialStorage {
    override fun load(scope: DatabricksCredentialScope): DatabricksOAuthTokens? {
        val data = records.read(scope.account) ?: return null
        return decode(data)
    }

    override fun save(
        scope: DatabricksCredentialScope,
        tokens: DatabricksOAuthTokens,
    ) {
        if (!tokens.isValid) throw CredentialStorageException.InvalidData()
        records.write(scope.account, encode(tokens))
    }

    override fun delete(scope: DatabricksCredentialScope) {
        records.remove(scope.account)
    }

    private fun encode(tokens: DatabricksOAuthTokens): ByteArray =
        JSONObject()
            .put("version", if (tokens.issuer == null) 1 else 2)
            .put("access_token", tokens.accessToken)
            .put("refresh_token", tokens.refreshToken)
            .put("expires_at_ms", tokens.expiresAtEpochMillis)
            .apply { tokens.issuer?.let { put("issuer", it.uri.toString()) } }
            .toString()
            .toByteArray()

    private fun decode(data: ByteArray): DatabricksOAuthTokens {
        try {
            val record = JSONObject(data.toString(Charsets.UTF_8))
            val version = record.getInt("version")
            val issuer =
                record.optString("issuer").takeIf(String::isNotEmpty)?.let {
                    DatabricksOAuthIssuer(URI(it))
                }
            if (version != if (issuer == null) 1 else 2) {
                throw CredentialStorageException.InvalidData()
            }
            val tokens =
                DatabricksOAuthTokens(
                    accessToken = record.getString("access_token"),
                    refreshToken = record.getString("refresh_token"),
                    expiresAtEpochMillis = record.getLong("expires_at_ms"),
                    issuer = issuer,
                )
            if (!tokens.isValid) throw CredentialStorageException.InvalidData()
            return tokens
        } catch (error: CredentialStorageException) {
            throw error
        } catch (_: Throwable) {
            throw CredentialStorageException.InvalidData()
        }
    }
}
