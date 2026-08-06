package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerSwitcherPositionTest {
    @Test
    fun `valid band retains normalized fractions`() {
        assertEquals(
            ServerSwitcherBand(0.3, 0.8),
            ServerSwitcherBand.from(0.3, 0.8),
        )
    }

    @Test
    fun `invalid bands are rejected`() {
        assertNull(ServerSwitcherBand.from(Double.NaN, 0.8))
        assertNull(ServerSwitcherBand.from(0.2, Double.POSITIVE_INFINITY))
        assertNull(ServerSwitcherBand.from(-0.1, 0.8))
        assertNull(ServerSwitcherBand.from(0.2, 1.1))
        assertNull(ServerSwitcherBand.from(0.5, 0.5))
        assertNull(ServerSwitcherBand.from(0.8, 0.2))
    }

    @Test
    fun `pill is centered within the published band`() {
        assertEquals(
            470,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.3, 0.8),
            ),
        )
    }

    @Test
    fun `pill exactly as wide as the band is anchored to its left edge`() {
        assertEquals(
            400,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.4, 0.56),
            ),
        )
    }

    @Test
    fun `pill wider than the band is anchored to the band edge`() {
        assertEquals(
            450,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.45, 0.55),
            ),
        )
    }

    @Test
    fun `pill wider than a right edge band stays inside the container`() {
        val containerWidth = 1000
        val switcherWidth = 160
        val left =
            serverSwitcherLeftMargin(
                containerWidth = containerWidth,
                switcherWidth = switcherWidth,
                band = ServerSwitcherBand(0.98, 1.0),
            )

        assertTrue(left + switcherWidth <= containerWidth)
        assertEquals(switcherWidth, minOf(left + switcherWidth, containerWidth) - maxOf(left, 0))
    }

    @Test
    fun `pill wider than a left edge band stays inside the container`() {
        val left =
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.0, 0.02),
            )

        assertTrue(left >= 0)
    }

    @Test
    fun `parent clamp does not move a pill that fits its band`() {
        assertEquals(
            470,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.3, 0.8),
            ),
        )
    }

    @Test
    fun `fractional band bounds round inward`() {
        val band = ServerSwitcherBand(0.3005, 0.7002)

        assertEquals(399, serverSwitcherBandWidth(containerWidth = 1000, band))
        assertEquals(
            301,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 399,
                band = band,
            ),
        )
    }

    @Test
    fun `header control reserves reduce the usable band`() {
        assertEquals(
            104,
            serverSwitcherUsableWidth(
                containerWidth = 1000,
                band = ServerSwitcherBand(0.4, 0.6),
                edgeReserve = 48,
            ),
        )
        assertEquals(
            4,
            serverSwitcherUsableWidth(
                containerWidth = 1000,
                band = ServerSwitcherBand(0.45, 0.55),
                edgeReserve = 48,
            ),
        )
        assertFalse(
            serverSwitcherBandCanFit(
                containerWidth = 1000,
                band = ServerSwitcherBand(0.45, 0.55),
                edgeReserve = 48,
                minimumWidth = 48,
            ),
        )
    }

    @Test
    fun `pill stays between reserved header controls`() {
        assertEquals(
            448,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 104,
                band = ServerSwitcherBand(0.4, 0.6),
                edgeReserve = 48,
            ),
        )
        // A stale (wider) pill anchors at the reserved edge, not the raw band
        // edge — centring without the reserve would land at 428 here.
        assertEquals(
            448,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 104,
                band = ServerSwitcherBand(0.4, 0.56),
                edgeReserve = 48,
            ),
        )
    }

    @Test
    fun `zero pixel band anchors the pill at the band edge`() {
        val band = ServerSwitcherBand(0.4501, 0.4502)

        assertEquals(0, serverSwitcherBandWidth(containerWidth = 1000, band))
        assertEquals(
            451,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 48,
                band = band,
            ),
        )
    }

    @Test
    fun `pill is clamped to bands at either container edge`() {
        assertEquals(
            0,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 50,
                band = ServerSwitcherBand(0.0, 0.05),
            ),
        )
        assertEquals(
            950,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 50,
                band = ServerSwitcherBand(0.95, 1.0),
            ),
        )
    }

    @Test
    fun `pill wider than the container stays at the parent start`() {
        assertEquals(
            0,
            serverSwitcherLeftMargin(
                containerWidth = 100,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.2, 0.8),
            ),
        )
    }

    @Test
    fun `pill stays within every band that can contain it`() {
        listOf(
            ServerSwitcherBand(0.0, 0.16),
            ServerSwitcherBand(0.2, 0.36),
            ServerSwitcherBand(0.3, 0.8),
            ServerSwitcherBand(0.84, 1.0),
        ).forEach { band ->
            val left =
                serverSwitcherLeftMargin(
                    containerWidth = 1000,
                    switcherWidth = 160,
                    band = band,
                )
            val bandLeft = (1000 * band.left).toInt()
            val bandRight = (1000 * band.right).toInt()

            assertTrue(left >= bandLeft)
            assertTrue(left + 160 <= bandRight)
        }
    }
}
