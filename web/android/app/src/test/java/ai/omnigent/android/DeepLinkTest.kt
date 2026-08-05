package ai.omnigent.android

import android.net.Uri
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkTest {
    private val hex = TEST_CONVERSATION_ID

    private fun parse(link: String) = DeepLink.parse(Uri.parse(link))

    // --- valid links ---

    @Test
    fun `loopback hosts infer http`() {
        assertEquals(
            DeepLink("http://localhost:8000", "/c/$hex"),
            parse("omnigent://localhost:8000/c/$hex"),
        )
        assertEquals(
            DeepLink("http://127.0.0.1:8000", "/c/$hex"),
            parse("omnigent://127.0.0.1:8000/c/$hex"),
        )
        assertEquals(
            DeepLink("http://[::1]:8000", "/c/$hex"),
            parse("omnigent://[::1]:8000/c/$hex"),
        )
    }

    @Test
    fun `remote hosts infer https`() {
        assertEquals(
            DeepLink("https://my-workspace.cloud.databricks.com", "/c/$hex"),
            parse("omnigent://my-workspace.cloud.databricks.com/c/$hex"),
        )
        assertEquals(
            DeepLink("https://example.com:8443", "/c/$hex"),
            parse("omnigent://example.com:8443/c/$hex"),
        )
    }

    @Test
    fun `canonicalizes case and default ports`() {
        assertEquals(
            DeepLink("https://example.com", "/c/$hex"),
            parse("OMNIGENT://Example.COM:443/c/$hex"),
        )
    }

    @Test
    fun `tolerates one trailing slash and forwards ids as-is`() {
        assertEquals("/c/$hex", parse("omnigent://localhost:8000/c/$hex/")?.path)
        // Dashed uuids and legacy conv_ prefixes are the SPA router's business.
        assertEquals(
            "/c/e4f5a6b7-c8d9-e0f1-a2b3-c4d5e6f7a8b9",
            parse("omnigent://h.example/c/e4f5a6b7-c8d9-e0f1-a2b3-c4d5e6f7a8b9")?.path,
        )
        assertEquals("/c/conv_$hex", parse("omnigent://h.example/c/conv_$hex")?.path)
    }

    // --- rejected links ---

    @Test
    fun `rejects wrong scheme and missing host`() {
        assertNull(parse("https://example.com/c/$hex"))
        assertNull(parse("omnigent:///c/$hex"))
        assertNull(parse("omnigent:$hex")) // opaque, not hierarchical
    }

    @Test
    fun `rejects non-conversation paths`() {
        assertNull(parse("omnigent://h.example/"))
        assertNull(parse("omnigent://h.example/c/"))
        assertNull(parse("omnigent://h.example/settings"))
        assertNull(parse("omnigent://h.example/c/$hex/extra"))
    }

    @Test
    fun `rejects structure smuggled into the id`() {
        // Percent-encoded separators decode into literals in Uri.getPath.
        assertNull(parse("omnigent://h.example/c/$hex%3Fview=terminal")) // ?
        assertNull(parse("omnigent://h.example/c/$hex%23frag")) // #
        assertNull(parse("omnigent://h.example/c/..%2F..%2Fetc")) // / and .
        assertNull(parse("omnigent://h.example/c/$hex%00")) // control char
        assertNull(parse("omnigent://h.example/c/$hex%zz")) // malformed escape -> stray %
        assertNull(parse("omnigent://h.example/c/.."))
    }

    @Test
    fun `rejects real query fragment and userinfo`() {
        assertNull(parse("omnigent://h.example/c/$hex?view=terminal"))
        assertNull(parse("omnigent://h.example/c/$hex#frag"))
        assertNull(parse("omnigent://user@h.example/c/$hex"))
        assertNull(parse("omnigent://h.example/c/$hex?")) // empty query still rejected
    }

    @Test
    fun `rejects invalid ports and oversized input`() {
        assertNull(parse("omnigent://h.example:99999/c/$hex"))
        assertNull(parse("omnigent://h.example:0/c/$hex"))
        assertNull(parse("omnigent://h.example/c/" + "a".repeat(3000)))
    }

    @Test
    fun `rejects malformed multi-colon authority`() {
        // Uri.getHost() can fold the extra colon into the host ("h:8000")
        // instead of rejecting it; an unbracketed colon must never pass.
        assertNull(parse("omnigent://h:8000:9000/c/$hex"))
        assertNull(parse("omnigent://h:x:1/c/$hex"))
    }

    @Test
    fun `normalizes unicode hosts to punycode`() {
        assertEquals(
            "https://xn--bcher-kva.example",
            parse("omnigent://bücher.example/c/$hex")?.origin,
        )
    }
}
