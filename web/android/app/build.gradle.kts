import org.gradle.api.tasks.Exec
import java.io.File
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.nio.file.Files
import java.nio.file.Path
import java.util.Properties
import java.util.concurrent.TimeUnit

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.play.publisher)
}

// Release signing credentials come from a gitignored keystore.properties (local
// dev) or, failing that, environment variables (CI). Absent both, the release
// signing config is skipped so debug builds still work without the keystore.
val keystorePropsFile = rootProject.file("keystore.properties")
val keystoreProps =
    Properties().apply {
        if (keystorePropsFile.exists()) keystorePropsFile.inputStream().use { load(it) }
    }

fun signingValue(
    propKey: String,
    envKey: String,
): String? = keystoreProps.getProperty(propKey) ?: System.getenv(envKey)

val storeFilePath = signingValue("storeFile", "OMNIGENT_KEYSTORE_FILE")

android {
    namespace = "ai.omnigent.android"
    compileSdk = 36

    defaultConfig {
        applicationId = "ai.omnigent.android"
        minSdk = 28
        targetSdk = 36
        versionCode = (project.findProperty("versionCode") as? String)?.toIntOrNull() ?: 7
        versionName = "0.1.2"

        // Instrumented (androidTest) runner — required for UI Automator / Espresso
        // screenshot tests. Mirrors the androidx.test stable line pinned below.
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        if (storeFilePath != null) {
            create("release") {
                storeFile = file(storeFilePath)
                storePassword = signingValue("storePassword", "OMNIGENT_KEYSTORE_PASSWORD")
                keyAlias = signingValue("keyAlias", "OMNIGENT_KEY_ALIAS")
                keyPassword = signingValue("keyPassword", "OMNIGENT_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            signingConfig = signingConfigs.findByName("release")
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    buildFeatures {
        buildConfig = true // for BuildConfig.DEBUG (gates authLog + WebView remote debugging)
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // AGP 9 has built-in Kotlin support; the jvmTarget is derived from the
    // Java 17 sourceCompatibility/targetCompatibility settings above.

    testOptions {
        unitTests {
            // Robolectric needs the module's resources (channel name, plurals).
            isIncludeAndroidResources = true
        }
    }
}

// Gradle Play Publisher: `./gradlew publishReleaseBundle` builds the signed AAB
// and uploads it to the internal track. The service-account JSON is a secret —
// point PLAY_SERVICE_ACCOUNT_JSON at it, or drop it at web/android/
// play-credentials.json (both gitignored). Publish tasks only run when the file
// is present; without it the config is inert so ordinary builds are unaffected.
val playCredentialsFile =
    (System.getenv("PLAY_SERVICE_ACCOUNT_JSON")?.let { file(it) })
        ?: rootProject.file("play-credentials.json")

play {
    enabled.set(playCredentialsFile.exists())
    if (playCredentialsFile.exists()) {
        serviceAccountCredentials.set(playCredentialsFile)
    }
    track.set("internal")
    defaultToAppBundles.set(true)
    // First upload of any new version code must clear review; fail fast rather
    // than hang if Google hasn't finished processing a prior upload.
    releaseStatus.set(com.github.triplet.gradle.androidpublisher.ReleaseStatus.COMPLETED)
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity)
    implementation(libs.androidx.appcompat)
    implementation(libs.androidx.webkit)
    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)

    // Instrumented (androidTest) screenshot tests. Run via connectedDebugAndroidTest
    // against a device/emulator; see the `recordScreenshots` Gradle task. UI Automator
    // is the capture mechanism (UiDevice.takeScreenshot(File)); espresso + runner/rules
    // provide the ActivityScenario/JUnit harness.
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.test.rules)
    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.espresso.core)
    androidTestImplementation(libs.uiautomator)
}

// Local development helpers — these delegate to adb so they work with physical
// devices or any running emulator.
//
// Resolve adb from the configured Android SDK directory rather than relying on
// it being on PATH: the Gradle daemon is long-lived and may have been started
// from an environment whose PATH doesn't include platform-tools (e.g. homebrew's
// android-commandlinetools), which makes a bare `commandLine("adb", ...)` fail
// with "A problem occurred starting process 'command 'adb''". AGP's own tasks
// (installDebug, etc.) resolve adb from the SDK internally, so we do the same.
val adbPath =
    androidComponents
        .sdkComponents
        .sdkDirectory
        .get()
        .asFile
        .resolve("platform-tools/adb")
        .absolutePath

tasks.register<Exec>("listDevices") {
    description = "List attached Android devices/emulators"
    group = "install"
    commandLine(adbPath, "devices", "-l")
}

tasks.register("runDebug") {
    description = "Install and launch the debug APK on a device/emulator"
    group = "install"
    dependsOn("installDebug")
    doLast {
        ProcessBuilder(
            adbPath,
            "shell",
            "am",
            "start",
            "-n",
            "ai.omnigent.android/.MainActivity",
        ).inheritIO().start().waitFor()
    }
}

tasks.register<Exec>("reverseProxy") {
    description = "Forward local port 8000 to the Android device/emulator (adb reverse)"
    group = "install"
    commandLine(adbPath, "reverse", "tcp:8000", "tcp:8000")
}

// ---------------------------------------------------------------------------
// Screenshot capture (instrumented, on-device)
//
// `recordScreenshots` is the one-shot entry point: it verifies the Vite dev
// server is reachable on localhost:5173, wires `adb reverse tcp:5173 tcp:5173`
// so the device's `localhost:5173` resolves to the host (works on both emulator
// and USB-attached physical devices — no --host 0.0.0.0 / LAN IP needed),
// builds+installs the androidTest APK, runs the screenshot test(s), then pulls
// the captured PNGs into app/build/screenshots/. The test itself
// (ScreenshotTest) seeds ServerStore with http://localhost:5173 and captures
// via UiDevice.takeScreenshot.
//
// Note: the app's DEFAULT_DEBUG_SERVER / `reverseProxy` task target port 8000,
// but the actual Vite dev server is 5173 (see web/README.md). This screenshot
// flow uses the real port and its own reverse mapping rather than coupling to
// those stale 8000 values.
//
// Requires exactly one attached device. Run from web/android:
//
//   ./gradlew recordScreenshots
//   open app/build/screenshots/home.png
//
// The Vite dev server is managed automatically: `startWebDevServer` launches it
// (if one isn't already up) and `stopWebDevServer` tears it down. So you don't
// need a separate `npm run dev` terminal — just run `recordScreenshots`.
// ---------------------------------------------------------------------------

/** Local Vite dev-server URL the device will reach via `adb reverse`. Must match
 *  the URL ScreenshotTest seeds into ServerStore and the cleartext allowlist in
 *  app/src/debug/res/xml/network_security_config.xml (localhost is permitted,
 *  any port). 5173 is Vite's default per web/README.md. Override the port with
 *  -PvitePort=… and host with -PviteHost=… . */
private val screenshotVitePort =
    (project.findProperty("vitePort") as? String)?.toIntOrNull() ?: 5173
private val screenshotViteHost = (project.findProperty("viteHost") as? String) ?: "127.0.0.1"
private val screenshotServerUrl = "http://localhost:$screenshotVitePort"
private val screenshotHostPort = screenshotVitePort

/** On-device path the instrumented test writes PNGs to (app external-files dir;
 *  pulled by `recordScreenshots` before the app is uninstalled). */
private val localScreenshotsDir =
    layout.buildDirectory
        .dir("screenshots")
        .get()
        .asFile

/** The web/ project (sibling of web/android/) — where Vite lives. */
private val webDir = rootProject.projectDir.parentFile

/** The vite CLI entry launched directly via `node` (avoids spawning `npm`/
 *  `pnpm`, whose grandchild vite process is hard to kill reliably). */
private val viteEntry = File(webDir, "node_modules/vite/bin/vite.js")

/** Backend (omnigent server) port — Vite proxies /v1 here (OMNIGENT_URL default
 *  is http://localhost:6767 per web/vite.config.ts). Override with -PbackendPort=. */
private val screenshotBackendPort =
    (project.findProperty("backendPort") as? String)?.toIntOrNull() ?: 6767

/** The agent YAML to pre-register so POST /v1/sessions has an agent_id. Override
 *  with -PagentSpec=. */
private val screenshotAgentSpec =
    (project.findProperty("agentSpec") as? String)
        ?: "examples/kimi_hello.yaml"

/** The demo user message seeded into the session so `session.png` has content. */
private val screenshotDemoPrompt =
    "Can you list the files in the current directory and explain what this project does?"

tasks.register<Exec>("reverseScreenshotPort") {
    description =
        "Forward local Vite port $screenshotHostPort to the device (adb reverse) for screenshot tests"
    group = "verification"
    commandLine(adbPath, "reverse", "tcp:$screenshotHostPort", "tcp:$screenshotHostPort")
}

/** Polls 127.0.0.1:$port until a TCP connection succeeds (Vite is ready) or the
 *  timeout elapses. Vite's first build can take a few seconds. */
private fun waitForPort(
    host: String,
    port: Int,
    timeoutMs: Long,
): Boolean {
    val deadline = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < deadline) {
        try {
            Socket().use { it.connect(InetSocketAddress(host, port), 1000) }
            return true
        } catch (e: Exception) {
            Thread.sleep(250)
        }
    }
    return false
}

/** The dev-server process we started (null if we reused an existing server or
 *  haven't started one). Kept in a top-level var so `stopWebDevServer` — a
 *  separate task in the same build — can reach it. */
private var devServerProcess: Process? = null
private var devServerStartedByUs = false

/** The backend (omnigent server) process we started. */
private var backendProcess: Process? = null
private var backendTempDir: Path? = null

/** The session id created by seedDemoSession (passed to the `session` screen). */
private var seededSessionId: String? = null

// Ensures web/node_modules is installed before launching Vite. Uses `npm`
// (the repo's canonical manager per web/README.md + package-lock.json); skip
// if node_modules already exists. Override with -PwebPackageManager=… .
tasks.register<Exec>("installWebDeps") {
    description = "Run npm install in web/ if node_modules is missing (prereq for the dev server)"
    group = "verification"
    onlyIf { !File(webDir, "node_modules").exists() }
    val pm = (project.findProperty("webPackageManager") as? String) ?: "npm"
    workingDir = webDir
    commandLine(pm, "install")
}

tasks.register("startWebDevServer") {
    description = "Start the Vite dev server (or reuse an existing one) for screenshot capture"
    group = "verification"
    dependsOn("installWebDeps")
    doLast {
        // Reuse an already-running server on the IPv4 loopback — adb reverse
        // forwards to 127.0.0.1, so the server must be reachable there. A default
        // `npm run dev` binds IPv6 `::1` only on macOS and would NOT be reached;
        // in that case we start our own with --host 127.0.0.1.
        fun alive(host: String) =
            try {
                Socket().use {
                    it.connect(InetSocketAddress(host, screenshotVitePort), 1000)
                    true
                }
            } catch (e: Exception) {
                false
            }
        if (alive("127.0.0.1")) {
            logger.lifecycle(
                "recordScreenshots: reusing existing dev server on 127.0.0.1:$screenshotVitePort",
            )
            return@doLast
        }
        if (!viteEntry.exists()) {
            throw GradleException(
                "Vite entry not found at ${viteEntry.absolutePath}. Run `npm install` in web/ first.",
            )
        }
        val pb =
            ProcessBuilder(
                "node",
                viteEntry.absolutePath,
                "--host",
                screenshotViteHost,
                "--port",
                screenshotVitePort.toString(),
            ).directory(webDir)
                .redirectErrorStream(true)
        // Inherit env (the Gradle daemon's PATH includes node via nvm). Pipe stdout
        // so Vite's logs don't flood the build output; on failure we dump them.
        devServerProcess = pb.start()
        devServerStartedByUs = true
        logger.lifecycle(
            "recordScreenshots: starting Vite dev server on $screenshotViteHost:$screenshotVitePort ...",
        )
        if (!waitForPort(screenshotViteHost, screenshotVitePort, 60_000)) {
            val log = devServerProcess!!.inputStream.readBytes().toString(Charsets.UTF_8)
            devServerProcess?.destroyForcibly()
            devServerProcess = null
            devServerStartedByUs = false
            throw GradleException(
                "Vite dev server didn't become ready on $screenshotViteHost:" +
                    "$screenshotVitePort within 60s.\n" +
                    "Vite output:\n$log",
            )
        }
        logger.lifecycle(
            "recordScreenshots: dev server ready on $screenshotViteHost:$screenshotVitePort",
        )
    }
}

tasks.register("stopWebDevServer") {
    description = "Stop the Vite dev server started by startWebDevServer (no-op if reused)"
    group = "verification"
    doLast {
        val p = devServerProcess
        if (p == null || !devServerStartedByUs) {
            logger.lifecycle(
                "recordScreenshots: dev server was reused or never started — nothing to stop",
            )
            return@doLast
        }
        // Kill the whole process tree: vite spawns esbuild/rollup workers as
        // children; destroyForcibly on the parent alone orphans them.
        p.descendants().forEach { runCatching { it.destroyForcibly() } }
        runCatching { p.destroyForcibly() }
        p.waitFor(3, TimeUnit.SECONDS)
        devServerProcess = null
        devServerStartedByUs = false
        logger.lifecycle("recordScreenshots: dev server stopped")
    }
}

// ---------------------------------------------------------------------------
// Backend (omnigent server) — isolated, in a temp dir, no auth.
//
// `startBackendServer` launches `omnigent server` with OMNIGENT_DATA_DIR /
// OMNIGENT_CONFIG_HOME pointed at a throwaway mktemp dir so it never touches
// the user's real ~/.omnigent. On loopback it's single-user (no login wall).
// Vite proxies /v1 to this backend (OMNIGENT_URL default = localhost:6767),
// so the SPA in the WebView gets real data — populated session list + a
// session page with content. `seedDemoSession` creates a session with a user
// message via POST /v1/sessions; `stopBackendServer` tears it down + cleans
// the temp dir.
// ---------------------------------------------------------------------------

tasks.register("startBackendServer") {
    description =
        "Start an isolated omnigent backend server (temp data dir, no auth) for real SPA data"
    group = "verification"
    doLast {
        // Reuse an already-running backend on the configured port.
        if (waitForPort("127.0.0.1", screenshotBackendPort, 0)) {
            logger.lifecycle(
                "recordScreenshots: reusing existing backend on 127.0.0.1:$screenshotBackendPort",
            )
            return@doLast
        }
        backendTempDir = Files.createTempDirectory("omnigent-screenshot")
        val dataDir = backendTempDir!!.resolve("data").toFile().apply { mkdirs() }
        val configDir = backendTempDir!!.resolve("config").toFile().apply { mkdirs() }
        val artifactsDir = backendTempDir!!.resolve("artifacts").toFile().apply { mkdirs() }
        val dbUri = "sqlite:///${dataDir.absolutePath}/chat.db"
        // Resolve the agent spec relative to the repo root (parent of web/).
        val agentSpecFile = File(rootProject.projectDir.parentFile, screenshotAgentSpec)
        val agentArg =
            if (agentSpecFile.exists()) {
                listOf(
                    "--agent",
                    agentSpecFile.absolutePath,
                )
            } else {
                emptyList()
            }
        val pb =
            ProcessBuilder(
                "omnigent",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                screenshotBackendPort.toString(),
                "--database-uri",
                dbUri,
                "--artifact-location",
                artifactsDir.absolutePath,
                *agentArg.toTypedArray(),
            ).directory(rootProject.projectDir.parentFile)
                .redirectErrorStream(true)
        pb.environment()["OMNIGENT_DATA_DIR"] = dataDir.absolutePath
        pb.environment()["OMNIGENT_CONFIG_HOME"] = configDir.absolutePath
        pb.environment()["OMNIGENT_DATABASE_URI"] = dbUri
        backendProcess = pb.start()
        logger.lifecycle(
            "recordScreenshots: starting backend on 127.0.0.1:$screenshotBackendPort (data dir: $backendTempDir) ...",
        )
        if (!waitForPort("127.0.0.1", screenshotBackendPort, 30_000)) {
            val log = backendProcess!!.inputStream.readBytes().toString(Charsets.UTF_8)
            backendProcess?.destroyForcibly()
            backendProcess = null
            throw GradleException(
                "Backend didn't become ready on :$screenshotBackendPort within 30s.\nOutput:\n$log",
            )
        }
        logger.lifecycle("recordScreenshots: backend ready on 127.0.0.1:$screenshotBackendPort")
    }
}

tasks.register("seedDemoSession") {
    description =
        "Create a session with a user message on the backend so the session screenshot has content"
    group = "verification"
    dependsOn("startBackendServer")
    doLast {
        val baseUrl = "http://127.0.0.1:$screenshotBackendPort"
        // Find a registered agent id (the --agent flag pre-registered one).
        val agentsJson = URI(baseUrl + "/v1/agents").toURL().readText()
        // Agent IDs may or may not have an "ag_" prefix depending on source;
        // just grab the first id from the list.
        val agentId =
            Regex(""""id":\s*"([^"]+)"""").find(agentsJson)?.groupValues?.get(1)
                ?: throw GradleException("No agent found on the backend at $baseUrl/v1/agents")
        // Create a session with an initial user message.
        val body =
            """{"agent_id":"$agentId","initial_items":[{"type":"message",""""" +
                """"data":{"role":"user","content":[{"type":"input_text",""""" +
                """"text":"$screenshotDemoPrompt"}]}]}}]}"""
        val conn = (URI(baseUrl + "/v1/sessions").toURL().openConnection() as HttpURLConnection)
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "application/json")
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toByteArray(Charsets.UTF_8)) }
        val resp = conn.inputStream.bufferedReader().use { it.readText() }
        seededSessionId = Regex(""""id":\s*"([^"]+)"""").find(resp)?.groupValues?.get(1)
            ?: throw GradleException("Failed to create session. Response: $resp")
        logger.lifecycle(
            "recordScreenshots: created demo session $seededSessionId (agent=$agentId)",
        )
    }
}

tasks.register("stopBackendServer") {
    description = "Stop the isolated backend server and clean its temp dir"
    group = "verification"
    doLast {
        backendProcess?.let { p ->
            p.descendants().forEach { runCatching { it.destroyForcibly() } }
            runCatching { p.destroyForcibly() }
            p.waitFor(3, TimeUnit.SECONDS)
        }
        backendProcess = null
        backendTempDir?.let {
            runCatching {
                Files.walk(it).sorted(reverseOrder()).forEach { f ->
                    runCatching { Files.deleteIfExists(f) }
                }
            }
        }
        backendTempDir = null
        logger.lifecycle("recordScreenshots: backend stopped + temp dir cleaned")
    }
}

/** Application id of the app under test. */
private val screenshotAppId = "ai.omnigent.android"

/** Test component (test package / instrumentation runner) for `am instrument`. */
private val screenshotTestComponent =
    "$screenshotAppId.test/androidx.test.runner.AndroidJUnitRunner"

tasks.register("recordScreenshots") {
    description =
        "Capture the server-select + home + session-list + session screens into app/build/screenshots"
    group = "verification"
    dependsOn("installDebug")
    dependsOn("installDebugAndroidTest")
    dependsOn("startBackendServer")
    dependsOn("seedDemoSession")
    dependsOn("startWebDevServer")
    dependsOn("reverseScreenshotPort")
    finalizedBy("stopWebDevServer")
    finalizedBy("stopBackendServer")
    doLast {
        localScreenshotsDir.mkdirs()
        // The session id to deep-link to. Prefer the one seeded by seedDemoSession;
        // fall back to -PsessionId=… then to a placeholder (empty session chrome).
        val sessionId =
            seededSessionId
                ?: (project.findProperty("sessionId") as? String).orEmpty()
        val screens =
            linkedMapOf(
                "server_select" to "",
                "home" to "",
                "session_list" to "",
                "session" to sessionId,
            )
        val reportDir =
            layout.buildDirectory
                .dir("screenshots-instrumentation")
                .get()
                .asFile
        reportDir.mkdirs()
        for ((screen, sid) in screens) {
            logger.lifecycle("recordScreenshots: capturing screen=$screen ...")
            // Per-screen device prep: pm clear so launch routes to ConnectActivity,
            // and pre-grant POST_NOTIFICATIONS to suppress the runtime dialog.
            ProcessBuilder(adbPath, "shell", "pm", "clear", screenshotAppId)
                .inheritIO()
                .start()
                .waitFor()
            ProcessBuilder(
                adbPath,
                "shell",
                "pm",
                "grant",
                screenshotAppId,
                "android.permission.POST_NOTIFICATIONS",
            ).inheritIO().start().waitFor()
            // pm clear stops the app process; give the system a moment to settle
            // before am instrument starts a fresh one, else the instrumentation
            // can race with the process shutdown and crash.
            Thread.sleep(1_000)
            // Run the single test method, parameterized by screen (+sessionId).
            val instrumentLog = File(reportDir, "instrument-$screen.log")
            val instrumentArgs =
                mutableListOf(
                    adbPath,
                    "shell",
                    "am",
                    "instrument",
                    "-w",
                    "-r",
                    "-e",
                    "class",
                    "ai.omnigent.android.ScreenshotTest",
                    "-e",
                    "screen",
                    screen,
                    "-e",
                    "serverUrl",
                    screenshotServerUrl,
                )
            if (sid.isNotEmpty()) {
                instrumentArgs.addAll(listOf("-e", "sessionId", sid))
            }
            instrumentArgs.add(screenshotTestComponent)
            ProcessBuilder(instrumentArgs)
                .redirectOutput(instrumentLog)
                .redirectError(instrumentLog)
                .start()
                .waitFor()
            val log = instrumentLog.readText()
            val failures =
                Regex("INSTRUMENTATION_RESULT: failures=(\\d+)")
                    .find(log)
                    ?.groupValues
                    ?.get(1)
                    ?.toIntOrNull() ?: 0
            if (failures > 0 || !log.contains("OK (1 test)")) {
                throw GradleException(
                    "Screenshot test failed for screen=$screen.\nInstrumentation output:\n$log",
                )
            }
            // Pull this screen's PNG while the app is still installed (am
            // instrument, unlike connectedDebugAndroidTest, doesn't uninstall).
            val src = "/sdcard/Android/data/$screenshotAppId/files/screenshots/$screen.png"
            val outFile = File(localScreenshotsDir, "$screen.png")
            ProcessBuilder(adbPath, "pull", src, outFile.absolutePath)
                .inheritIO()
                .start()
                .waitFor()
            if (!outFile.exists() || outFile.length() == 0L) {
                throw GradleException("No screenshot pulled for screen=$screen from $src.")
            }
            logger.lifecycle("recordScreenshots: saved ${outFile.name} (${outFile.length()} bytes)")
        }
        logger.lifecycle(
            "recordScreenshots: done — ${screens.size} screenshots in ${localScreenshotsDir.absolutePath}",
        )
    }
}
