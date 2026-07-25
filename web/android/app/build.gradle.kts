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
    implementation(libs.androidx.webkit)
    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
}
