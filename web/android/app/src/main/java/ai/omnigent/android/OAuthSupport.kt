package ai.omnigent.android

import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64

/** PKCE (RFC 7636), random values and form bodies shared by the native OAuth flows. */
internal object OAuthSupport {
    private val secureRandom = SecureRandom()

    fun randomBytes(size: Int): ByteArray = ByteArray(size).also(secureRandom::nextBytes)

    /** 32 random bytes as base64url: a 43-character verifier, state or nonce. */
    fun randomValue(randomBytes: (Int) -> ByteArray = ::randomBytes): String =
        base64Url(randomBytes(32))

    /** The S256 code challenge for [verifier]. */
    fun challenge(verifier: String): String =
        base64Url(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray()))

    /** An `application/x-www-form-urlencoded` body. */
    fun formBody(fields: List<Pair<String, String>>): ByteArray =
        fields
            .joinToString("&") { (key, value) -> "${formEncode(key)}=${formEncode(value)}" }
            .toByteArray()

    private fun base64Url(bytes: ByteArray): String =
        Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
}
