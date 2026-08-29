package ai.omnigent.android

import java.util.Base64

/**
 * Shared test builder for HS256-shaped JWTs with an unverified signature. The
 * account-identity code only reads the payload's `sub` claim, so the signature
 * is a fixed placeholder. Used by the identity and MainActivity account tests.
 */
internal object TestJwt {
    /** A JWT whose body is [payload] verbatim (arbitrary claims / malformed shapes). */
    fun forPayload(payload: String): String {
        val enc = Base64.getUrlEncoder().withoutPadding()
        val header = enc.encodeToString("""{"alg":"HS256","typ":"JWT"}""".toByteArray())
        val body = enc.encodeToString(payload.toByteArray())
        return "$header.$body.c2ln"
    }

    /** A JWT carrying just `{"sub": subject}`. */
    fun forSubject(subject: String): String = forPayload("""{"sub":"$subject"}""")
}
