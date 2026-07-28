import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
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
    compileSdk = 35

    defaultConfig {
        applicationId = "ai.omnigent.android"
        minSdk = 28
        targetSdk = 35
        versionCode = (project.findProperty("versionCode") as? String)?.toIntOrNull() ?: 2
        versionName = "0.1.0"
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

    kotlinOptions {
        jvmTarget = "17"
    }

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
val adbPath = android.sdkDirectory.resolve("platform-tools/adb").absolutePath

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
        exec {
            commandLine(
                adbPath,
                "shell",
                "am",
                "start",
                "-n",
                "ai.omnigent.android/.MainActivity",
            )
        }
    }
}

tasks.register<Exec>("reverseProxy") {
    description = "Forward local port 8000 to the Android device/emulator (adb reverse)"
    group = "install"
    commandLine(adbPath, "reverse", "tcp:8000", "tcp:8000")
}
