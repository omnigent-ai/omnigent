package ai.omnigent.intellij.discovery

import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.time.Duration

/**
 * Pure /health-probe result interpretation. The probe's IO is abstracted into
 * a HealthObservation so this logic is unit-testable without real network
 * access. The runtime fetch + timeout live in probeHealth() below.
 */
const val DEFAULT_HEALTH_TIMEOUT_MS = 2000L

data class HealthObservation(
    /** HTTP status code; omitted on timeout / network error. */
    val status: Int? = null,
    /** Parsed JSON body when available (expects a simple `{"status": ...}` shape). */
    val body: Map<String, Any?>? = null,
    val timedOut: Boolean = false,
    val networkError: Boolean = false,
)

enum class HealthOutcome { OK, UNHEALTHY, TIMEOUT, UNREACHABLE }

/** Interpret a probe observation into an outcome. Pure. */
fun interpretHealth(obs: HealthObservation): HealthOutcome {
    if (obs.timedOut) {
        return HealthOutcome.TIMEOUT
    }
    if (obs.networkError) {
        return HealthOutcome.UNREACHABLE
    }
    if (obs.status == 200 && isStatusOk(obs.body)) {
        return HealthOutcome.OK
    }
    return HealthOutcome.UNHEALTHY
}

private fun isStatusOk(body: Map<String, Any?>?): Boolean {
    return body?.get("status") == "ok"
}

/**
 * Runtime probe: GET {base}/health with a short timeout, reduced to a pure
 * observation that is then interpreted. Kept thin so the testable surface is
 * interpretHealth().
 */
fun probeHealth(
    base: String,
    timeoutMs: Long = DEFAULT_HEALTH_TIMEOUT_MS,
    client: HttpClient = HttpClient.newHttpClient(),
): HealthOutcome {
    val url = base.trimEnd('/') + "/health"
    val request = HttpRequest.newBuilder(URI.create(url))
        .timeout(Duration.ofMillis(timeoutMs))
        .GET()
        .build()
    return try {
        val response = client.send(request, HttpResponse.BodyHandlers.ofString())
        val body = parseStatusBody(response.body())
        interpretHealth(HealthObservation(status = response.statusCode(), body = body))
    } catch (e: java.net.http.HttpTimeoutException) {
        interpretHealth(HealthObservation(timedOut = true))
    } catch (e: Exception) {
        interpretHealth(HealthObservation(networkError = true))
    }
}

/** Minimal `{"status": "..."}` extraction — avoids a full JSON dependency for one field. */
private fun parseStatusBody(raw: String?): Map<String, Any?>? {
    if (raw == null) return null
    val match = Regex("\"status\"\\s*:\\s*\"([^\"]*)\"").find(raw) ?: return null
    return mapOf("status" to match.groupValues[1])
}
