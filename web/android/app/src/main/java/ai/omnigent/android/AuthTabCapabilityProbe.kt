package ai.omnigent.android

import android.content.Context
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.util.JsonReader
import android.util.JsonToken
import java.io.InputStreamReader
import java.io.Reader
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.Locale
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/**
 * Probes whether an origin anonymously serves Digital Asset Links matching the
 * running app. Results are cached per origin until explicitly forgotten.
 */
internal class AuthTabCapabilityProbe(
    context: Context,
    private val signingFingerprints: () -> Set<String> = { signingCertificateSha256(context) },
    private val fetch: (String, String, Set<String>) -> Boolean = ::fetchAssetLinks,
    private val execute: ((() -> Unit) -> Unit) = { task -> IO.execute(task) },
    private val post: ((() -> Unit) -> Unit) = { task -> MAIN.post(task) },
) {
    private class PendingProbe(
        val callbacks: MutableList<(Boolean) -> Unit>,
    )

    private val packageName = context.packageName
    private val lock = Any()
    private val results = mutableMapOf<String, Boolean>()
    private val pending = mutableMapOf<String, PendingProbe>()

    fun probe(
        origin: String?,
        onResult: (Boolean) -> Unit,
    ) {
        val normalized = originOf(origin)
        if (normalized == null || URL(normalized).protocol != "https") {
            post { onResult(false) }
            return
        }

        val pendingProbe =
            synchronized(lock) {
                results[normalized]?.let { cached ->
                    post { onResult(cached) }
                    return
                }
                pending[normalized]?.let { probe ->
                    probe.callbacks += onResult
                    return
                }
                PendingProbe(mutableListOf(onResult)).also { pending[normalized] = it }
            }

        execute {
            // This is only a launch hint; browser DAL verification remains authoritative.
            val supported =
                signingFingerprints().takeIf { it.isNotEmpty() }?.let { fingerprints ->
                    runCatching { fetch(normalized, packageName, fingerprints) }.getOrDefault(false)
                } ?: false
            val callbacks =
                synchronized(lock) {
                    if (pending[normalized] !== pendingProbe) {
                        emptyList()
                    } else {
                        results[normalized] = supported
                        pending.remove(normalized)
                        pendingProbe.callbacks.toList()
                    }
                }
            post { callbacks.forEach { callback -> callback(supported) } }
        }
    }

    fun forget(origin: String?) {
        val normalized = originOf(origin) ?: return
        synchronized(lock) {
            results.remove(normalized)
            pending.remove(normalized)
        }
    }

    private companion object {
        val IO: Executor by lazy { Executors.newCachedThreadPool() }
        val MAIN: Handler by lazy { Handler(Looper.getMainLooper()) }
        const val TIMEOUT_MS = 3_000

        fun fetchAssetLinks(
            origin: String,
            packageName: String,
            signingFingerprints: Set<String>,
        ): Boolean {
            val connection =
                URL("$origin/.well-known/assetlinks.json").openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            connection.connectTimeout = TIMEOUT_MS
            connection.readTimeout = TIMEOUT_MS
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Cookie", "")
            return try {
                if (connection.responseCode != HttpURLConnection.HTTP_OK) return false
                hasMatchingAssetLinks(
                    InputStreamReader(connection.inputStream, Charsets.UTF_8),
                    packageName,
                    signingFingerprints,
                )
            } finally {
                connection.disconnect()
            }
        }

        fun signingCertificateSha256(context: Context): Set<String> =
            runCatching {
                @Suppress("DEPRECATION")
                val packageInfo =
                    context.packageManager.getPackageInfo(
                        context.packageName,
                        PackageManager.GET_SIGNING_CERTIFICATES,
                    )
                val signingInfo = packageInfo.signingInfo ?: return@runCatching emptySet()
                (
                    signingInfo.apkContentsSigners.orEmpty().asSequence() +
                        signingInfo.signingCertificateHistory.orEmpty().asSequence()
                ).map { certificate ->
                    MessageDigest
                        .getInstance("SHA-256")
                        .digest(certificate.toByteArray())
                        .joinToString(":") { byte ->
                            String.format(Locale.US, "%02X", byte.toInt() and 0xff)
                        }
                }.toSet()
            }.getOrDefault(emptySet())
    }
}

internal fun hasMatchingAssetLinks(
    source: Reader,
    packageName: String,
    signingFingerprints: Set<String>,
): Boolean =
    runCatching {
        JsonReader(source).use { reader ->
            if (reader.peek() != JsonToken.BEGIN_ARRAY) return@use false
            reader.beginArray()
            var matches = false
            while (reader.hasNext()) {
                matches = readAssetLink(reader, packageName, signingFingerprints) || matches
            }
            reader.endArray()
            matches
        }
    }.getOrDefault(false)

private fun readAssetLink(
    reader: JsonReader,
    packageName: String,
    signingFingerprints: Set<String>,
): Boolean {
    if (reader.peek() != JsonToken.BEGIN_OBJECT) {
        reader.skipValue()
        return false
    }
    reader.beginObject()
    var relationMatches = false
    var targetMatches = false
    while (reader.hasNext()) {
        when (reader.nextName()) {
            "relation" -> {
                relationMatches =
                    readStringArrayContains(
                        reader,
                        "delegate_permission/common.handle_all_urls",
                    )
            }

            "target" -> {
                targetMatches = readTarget(reader, packageName, signingFingerprints)
            }

            else -> {
                reader.skipValue()
            }
        }
    }
    reader.endObject()
    return relationMatches && targetMatches
}

private fun readTarget(
    reader: JsonReader,
    packageName: String,
    signingFingerprints: Set<String>,
): Boolean {
    if (reader.peek() != JsonToken.BEGIN_OBJECT) {
        reader.skipValue()
        return false
    }
    reader.beginObject()
    var namespace: String? = null
    var targetPackage: String? = null
    var fingerprintMatches = false
    while (reader.hasNext()) {
        when (reader.nextName()) {
            "namespace" -> {
                namespace = readString(reader)
            }

            "package_name" -> {
                targetPackage = readString(reader)
            }

            "sha256_cert_fingerprints" -> {
                fingerprintMatches =
                    readStringArrayContains(
                        reader,
                        signingFingerprints.mapTo(mutableSetOf(), ::normalizeFingerprint),
                        ::normalizeFingerprint,
                    )
            }

            else -> {
                reader.skipValue()
            }
        }
    }
    reader.endObject()
    return namespace == "android_app" &&
        targetPackage == packageName &&
        fingerprintMatches
}

private fun readString(reader: JsonReader): String? {
    if (reader.peek() != JsonToken.STRING) {
        reader.skipValue()
        return null
    }
    return reader.nextString()
}

private fun readStringArrayContains(
    reader: JsonReader,
    expected: String,
): Boolean = readStringArrayContains(reader, setOf(expected))

private fun readStringArrayContains(
    reader: JsonReader,
    expected: Set<String>,
    normalize: (String) -> String = { it },
): Boolean {
    if (reader.peek() != JsonToken.BEGIN_ARRAY) {
        reader.skipValue()
        return false
    }
    reader.beginArray()
    var matches = false
    while (reader.hasNext()) {
        if (reader.peek() == JsonToken.STRING) {
            matches = normalize(reader.nextString()) in expected || matches
        } else {
            reader.skipValue()
        }
    }
    reader.endArray()
    return matches
}

private fun normalizeFingerprint(fingerprint: String): String =
    fingerprint.replace(":", "").uppercase(Locale.US)
