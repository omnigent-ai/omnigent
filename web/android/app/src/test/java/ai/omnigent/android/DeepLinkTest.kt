package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class DeepLinkTest {
    @Test
    fun `share links name the server origin and the conversation route`() {
        assertEquals(
            DeepLink("https://omnigent.example.com", "/c/2144378716924436"),
            DeepLink.parse("omnigent://omnigent.example.com/c/2144378716924436"),
        )
        assertEquals(
            DeepLink(
                "https://omnigent.example.com:8443",
                "/c/208533c837b2451bbee7ac060b04e5f1",
            ),
            DeepLink.parse(
                "omnigent://omnigent.example.com:8443/c/208533c837b2451bbee7ac060b04e5f1",
            ),
        )
        assertEquals(
            DeepLink("http://localhost:6767", "/c/abc123"),
            DeepLink.parse("omnigent://localhost:6767/c/abc123"),
        )
        assertEquals(
            "omnigent.example.com:8443",
            DeepLink.parse("omnigent://omnigent.example.com:8443/c/abc")?.hostLabel,
        )
    }

    @Test
    fun `origins are canonical and only the conversation route is kept`() {
        assertEquals(
            DeepLink("https://omnigent.example.com", "/c/abc"),
            DeepLink.parse("omnigent://Omnigent.Example.COM:443/c/abc/?view=terminal#top"),
        )
        assertEquals(
            DeepLink("http://127.0.0.1", "/c/abc"),
            DeepLink.parse("omnigent://127.0.0.1:80/c/abc"),
        )
        assertEquals(
            DeepLink("http://[::1]:8000", "/c/abc"),
            DeepLink.parse("omnigent://[::1]:8000/c/abc"),
        )
    }

    @Test
    fun `anything but a conversation link on a server is rejected`() {
        val rejected =
            listOf(
                null,
                "",
                "not a link",
                "https://omnigent.example.com/c/abc",
                "ai.omnigent.android:/oauth/callback?code=c",
                "omnigent:/c/abc",
                "omnigent:///c/abc",
                "omnigent://omnigent.example.com",
                "omnigent://omnigent.example.com/settings",
                "omnigent://omnigent.example.com/omnigent/c/abc",
                "omnigent://omnigent.example.com/c/",
                "omnigent://omnigent.example.com/c/abc/def",
                "omnigent://omnigent.example.com:0/c/abc",
                "omnigent://omnigent.example.com:70000/c/abc",
            )
        for (link in rejected) assertNull(link, DeepLink.parse(link))
    }

    @Test
    fun `conversation ids cannot smuggle URL structure`() {
        val smuggling =
            listOf(
                "abc%3Fview=terminal",
                "abc%23top",
                "a%2Fb",
                "a%5Cb",
                "..",
                "%2E%2E",
                "abc%00",
                "abc%0A",
                "abc%25",
                "abc%zz",
            )
        for (id in smuggling) {
            assertNull(id, DeepLink.parse("omnigent://omnigent.example.com/c/$id"))
        }
    }
}
