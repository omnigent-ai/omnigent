package ai.omnigent.android

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.StringReader

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AuthTabCapabilityProbeTest {
    @Test
    fun `get login creds relation does not advertise auth tab capability`() {
        assertFalse(
            matches(
                assetLink(relation = "delegate_permission/common.get_login_creds"),
            ),
        )
    }

    @Test
    fun `wrong package does not advertise auth tab capability`() {
        assertFalse(matches(assetLink(packageName = "com.example.other")))
    }

    @Test
    fun `wrong signing fingerprint does not advertise auth tab capability`() {
        assertFalse(matches(assetLink(fingerprint = WRONG_FINGERPRINT)))
    }

    @Test
    fun `web namespace does not advertise auth tab capability`() {
        assertFalse(matches(assetLink(namespace = "web")))
    }

    @Test
    fun `matching android app entry advertises auth tab capability`() {
        assertTrue(matches(assetLink()))
    }

    @Test
    fun `lowercase colonless fingerprint advertises auth tab capability`() {
        assertTrue(
            matches(
                assetLink(fingerprint = SIGNING_FINGERPRINT.replace(":", "").lowercase()),
            ),
        )
    }

    @Test
    fun `second signer of multi signer app advertises auth tab capability`() {
        assertTrue(
            matches(
                assetLink(fingerprint = SECOND_SIGNING_FINGERPRINT),
                setOf(SIGNING_FINGERPRINT, SECOND_SIGNING_FINGERPRINT),
            ),
        )
    }

    @Test
    fun `historical signing certificate advertises auth tab capability`() {
        assertTrue(
            matches(
                assetLink(fingerprint = HISTORICAL_SIGNING_FINGERPRINT),
                setOf(SIGNING_FINGERPRINT, HISTORICAL_SIGNING_FINGERPRINT),
            ),
        )
    }

    @Test
    fun `unrelated fingerprint does not match any app signer`() {
        assertFalse(
            matches(
                assetLink(fingerprint = WRONG_FINGERPRINT),
                setOf(SIGNING_FINGERPRINT, SECOND_SIGNING_FINGERPRINT),
            ),
        )
    }

    @Test
    fun `matching array with trailing padding advertises auth tab capability`() {
        assertTrue(matches(assetLink() + "\n<!-- CDN padding -->"))
    }

    @Test
    fun `malformed asset links does not advertise auth tab capability`() {
        assertFalse(matches("[{"))
    }

    @Test
    fun `probe caches the anonymous asset links result per origin`() {
        var fetches = 0
        val results = mutableListOf<Boolean>()
        val probe =
            AuthTabCapabilityProbe(
                context(),
                signingFingerprints = { setOf(SIGNING_FINGERPRINT) },
                fetch = { _, _, _ ->
                    fetches++
                    true
                },
                execute = { task -> task() },
                post = { task -> task() },
            )

        probe.probe("https://front-door.example.com", results::add)
        probe.probe("https://front-door.example.com/", results::add)

        assertEquals(1, fetches)
        assertEquals(listOf(true, true), results)
    }

    @Test
    fun `forget discards the cached result for an origin`() {
        var fetches = 0
        val results = mutableListOf<Boolean>()
        val probe =
            AuthTabCapabilityProbe(
                context(),
                signingFingerprints = { setOf(SIGNING_FINGERPRINT) },
                fetch = { _, _, _ -> ++fetches > 1 },
                execute = { task -> task() },
                post = { task -> task() },
            )

        probe.probe("https://front-door.example.com", results::add)
        probe.forget("https://front-door.example.com")
        probe.probe("https://front-door.example.com", results::add)

        assertEquals(2, fetches)
        assertEquals(listOf(false, true), results)
    }

    @Test
    fun `probe coalesces concurrent requests for one origin`() {
        var fetches = 0
        var queued: (() -> Unit)? = null
        val results = mutableListOf<Boolean>()
        val probe =
            AuthTabCapabilityProbe(
                context(),
                signingFingerprints = { setOf(SIGNING_FINGERPRINT) },
                fetch = { _, _, _ ->
                    fetches++
                    false
                },
                execute = { task -> queued = task },
                post = { task -> task() },
            )

        probe.probe("https://front-door.example.com", results::add)
        probe.probe("https://front-door.example.com", results::add)
        assertTrue(results.isEmpty())

        queued!!()

        assertEquals(1, fetches)
        assertEquals(listOf(false, false), results)
    }

    @Test
    fun `probe rejects non-https origins without fetching`() {
        var fetched = false
        var result = true
        val probe =
            AuthTabCapabilityProbe(
                context(),
                signingFingerprints = { setOf(SIGNING_FINGERPRINT) },
                fetch = { _, _, _ ->
                    fetched = true
                    true
                },
                execute = { task -> task() },
                post = { task -> task() },
            )

        probe.probe("http://front-door.example.com") { result = it }

        assertFalse(fetched)
        assertFalse(result)
    }

    private fun matches(
        body: String,
        signingFingerprints: Set<String> = setOf(SIGNING_FINGERPRINT),
    ): Boolean =
        hasMatchingAssetLinks(
            StringReader(body),
            PACKAGE_NAME,
            signingFingerprints,
        )

    private fun assetLink(
        relation: String = "delegate_permission/common.handle_all_urls",
        namespace: String = "android_app",
        packageName: String = PACKAGE_NAME,
        fingerprint: String = SIGNING_FINGERPRINT,
    ): String =
        """[
          {
            "relation": ["$relation"],
            "target": {
              "namespace": "$namespace",
              "package_name": "$packageName",
              "sha256_cert_fingerprints": ["$fingerprint"]
            }
          }
        ]"""

    private fun context(): Context = ApplicationProvider.getApplicationContext()

    private companion object {
        const val PACKAGE_NAME = "ai.omnigent.android"
        const val SIGNING_FINGERPRINT =
            "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:" +
                "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00"
        const val WRONG_FINGERPRINT =
            "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:" +
                "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99"
        const val SECOND_SIGNING_FINGERPRINT =
            "22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:" +
                "22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11"
        const val HISTORICAL_SIGNING_FINGERPRINT =
            "33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22:" +
                "33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:11:22"
    }
}
