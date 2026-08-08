package ai.omnigent.intellij.config

import ai.omnigent.intellij.discovery.HealthOutcome
import java.net.URI

/**
 * Config + server-target + host-type resolution.
 *
 * Resolution order: manual override (serverUrl set) > auto-discovered local >
 * (caller prompts). This build is LOCALHOST-ONLY: a manual override that does
 * not resolve to a loopback host is rejected (the JCEF render path only hosts
 * local servers). All decision logic is pure and isolated from the IntelliJ
 * platform so it is unit-testable without an IDE host. The thin adapter lives
 * in OmnigentSettings.toSettings().
 */

enum class HostType { LOCAL, REMOTE, UNKNOWN }

enum class TargetSource { MANUAL, DISCOVERED }

data class ServerTarget(
    val baseUrl: String,
    val origin: String,
    val hostType: HostType,
    /** Where the target came from, for diagnostics. */
    val source: TargetSource,
)

/** Thin, stubbable settings surface (isolates the platform API). */
interface Settings {
    val serverUrl: String
}

private val LOOPBACK_HOSTS = setOf("localhost", "127.0.0.1", "::1", "[::1]")

/** Derive the origin (scheme://host[:port]) from a URL. Pure. */
fun originOf(url: String): String {
    val u = URI(url)
    return "${u.scheme}://${u.authority}"
}

/** Classify a URL's host as local (loopback) or remote. Pure. */
fun hostTypeOf(url: String): HostType {
    return try {
        val u = URI(url)
        if (LOOPBACK_HOSTS.contains(u.host)) HostType.LOCAL else HostType.REMOTE
    } catch (e: Exception) {
        HostType.UNKNOWN
    }
}

/** Build a ServerTarget from a manual override URL. Pure. */
fun manualTarget(serverUrl: String): ServerTarget {
    val trimmed = serverUrl.removeSuffix("/")
    return ServerTarget(
        baseUrl = trimmed,
        origin = originOf(trimmed),
        hostType = hostTypeOf(trimmed),
        source = TargetSource.MANUAL,
    )
}

/** Build a ServerTarget from a discovered local baseUrl. Pure. */
fun discoveredTarget(baseUrl: String): ServerTarget {
    return ServerTarget(
        baseUrl = baseUrl,
        origin = originOf(baseUrl),
        hostType = HostType.LOCAL,
        source = TargetSource.DISCOVERED,
    )
}

/** The discovery summary the resolver needs (kept abstract for testability). */
data class DiscoverySummary(
    val found: Boolean,
    val baseUrl: String? = null,
    val health: HealthOutcome? = null,
)

enum class NeedsPromptReason { NoManualNoLocal, LocalUnhealthy, RemoteUnsupported }

sealed class TargetResolution {
    data class Resolved(val target: ServerTarget) : TargetResolution()
    data class NeedsPrompt(val reason: NeedsPromptReason) : TargetResolution()
}

/**
 * Resolve a server target purely from settings + a discovery summary.
 *  1. manual override (serverUrl non-empty) wins — but ONLY if it is loopback;
 *     a non-loopback / malformed override is rejected (remote-unsupported),
 *     because the JCEF render path hosts local servers only.
 *  2. else auto-discovered local with health == HealthOutcome.OK
 *  3. else needs-prompt
 */
fun resolveServerTarget(settings: Settings, discovery: DiscoverySummary): TargetResolution {
    val manual = settings.serverUrl.trim()
    if (manual.isNotEmpty()) {
        // Classify FIRST (catch-safe) so a malformed URL never throws before
        // the host-type gate; both REMOTE and UNKNOWN are rejected.
        if (hostTypeOf(manual) != HostType.LOCAL) {
            return TargetResolution.NeedsPrompt(NeedsPromptReason.RemoteUnsupported)
        }
        return TargetResolution.Resolved(manualTarget(manual))
    }
    if (discovery.found && discovery.baseUrl != null) {
        if (discovery.health == HealthOutcome.OK) {
            return TargetResolution.Resolved(discoveredTarget(discovery.baseUrl))
        }
        return TargetResolution.NeedsPrompt(NeedsPromptReason.LocalUnhealthy)
    }
    return TargetResolution.NeedsPrompt(NeedsPromptReason.NoManualNoLocal)
}
