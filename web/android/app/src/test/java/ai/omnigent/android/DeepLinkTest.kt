package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Which `omnigent://` links the app opens, and what each one opens. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkTest {
    @Test
    fun `a link names its server by host and reaches it over https`() {
        assertEquals(
            DeepLink("https://workspace.example.test", "/c/0123abcd"),
            DeepLink.parse("omnigent://workspace.example.test/c/0123abcd"),
        )
        assertEquals(
            DeepLink("https://example.test:8443", "/c/0123abcd"),
            DeepLink.parse("omnigent://example.test:8443/c/0123abcd"),
        )
        // The origin matches what the app records for a server typed with any casing or port.
        assertEquals(
            DeepLink(originOf("https://Example.Test:443")!!, "/c/0123abcd"),
            DeepLink.parse("OMNIGENT://Example.Test:443/c/0123abcd/"),
        )
    }

    @Test
    fun `a link to this device reaches it over http`() {
        assertEquals(
            DeepLink("http://localhost:8000", "/c/conv_abc"),
            DeepLink.parse("omnigent://localhost:8000/c/conv_abc"),
        )
        assertEquals(
            DeepLink("http://127.0.0.1:8000", "/c/conv_abc"),
            DeepLink.parse("omnigent://127.0.0.1:8000/c/conv_abc"),
        )
        assertEquals(
            DeepLink("http://[::1]:8000", "/c/conv_abc"),
            DeepLink.parse("omnigent://[::1]:8000/c/conv_abc"),
        )
    }

    @Test
    fun `a link keeps only the parameters that pick a file to show`() {
        assertEquals(
            DeepLink("https://example.test", "/c/abc?file=src%2Fa+b.kt&fullscreen=1"),
            DeepLink.parse(
                "omnigent://example.test/c/abc?view=terminal&file=src%2Fa+b.kt&fullscreen=1#top",
            ),
        )
        assertEquals(
            DeepLink("https://example.test", "/c/abc"),
            DeepLink.parse("omnigent://example.test/c/abc?view=terminal&next=%2Fadmin"),
        )
    }

    @Test
    fun `any id the conversation route accepts is opened`() {
        for (id in listOf(
            "8f14e45fceea167a5a36dedd4bea2543",
            "8F14E45F-CEEA-167A-5A36-DEDD4BEA2543",
            "conv_abc",
            "ag-123",
            "a~b",
        )) {
            assertEquals(
                id,
                DeepLink("https://example.test", "/c/$id"),
                DeepLink.parse("omnigent://example.test/c/$id"),
            )
        }
    }

    @Test
    fun `anything but a conversation on a named server is refused`() {
        for (link in listOf(
            null,
            "",
            "not a link",
            "https://example.test/c/abc",
            "ai.omnigent.android://example.test/c/abc",
            "omnigent:///c/abc",
            "omnigent:c/abc",
            "omnigent://example.test",
            "omnigent://example.test/",
            "omnigent://example.test/c/",
            "omnigent://example.test/settings",
            "omnigent://example.test/c/abc/files",
            "omnigent://example.test/x/c/abc",
            "omnigent://user:secret@example.test/c/abc",
        )) {
            assertNull(link, DeepLink.parse(link))
        }
    }

    // The route is appended to the server's address, so a hidden separator would open another page.
    @Test
    fun `an id that hides a path, a query or a fragment is refused`() {
        for (id in listOf(
            "abc%3Fview=terminal",
            "abc%23top",
            "abc%2Ffiles",
            "%2E%2E",
            "..",
            "a.b",
            "abc%00",
            "abc%0A",
            "abc%7F",
            "abc%zz",
            "abc%25",
            "abc%253F",
        )) {
            assertNull(id, DeepLink.parse("omnigent://example.test/c/$id"))
        }
    }
}
