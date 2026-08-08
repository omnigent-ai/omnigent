import org.jetbrains.intellij.platform.gradle.IntelliJPlatformType

// Omnigent IntelliJ/PyCharm plugin — minimal slice.
//
// Uses the IntelliJ Platform Gradle Plugin v2 (org.jetbrains.intellij.platform).
// Kotlin/JVM. Targets IDEA + PyCharm (Community + Professional). The
// discovery/config/tool-window-content logic is PURE (no IDE APIs) so the unit
// tests run on a plain JUnit5 JVM without bootstrapping an IDE.

plugins {
    kotlin("jvm") version "2.2.0"
    id("org.jetbrains.intellij.platform") version "2.16.0"
}

group = "ai.omnigent"
version = providers.gradleProperty("pluginVersion").getOrElse("0.1.0")

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

val platformType = providers.gradleProperty("platformType").getOrElse("IC")
val platformVersion = providers.gradleProperty("platformVersion").getOrElse("2024.1")

dependencies {
    intellijPlatform {
        // Switch platformType=PC (PyCharm Community) / PY (PyCharm Professional) /
        // IU (IDEA Ultimate) via gradle.properties to build/verify the other IDEs.
        // useInstaller=false resolves the SDK artifact from the intellij-repository
        // (maven layout) by build number, instead of a full IDE installer.
        create {
            type = IntelliJPlatformType.fromCode(platformType)
            version = platformVersion
            useInstaller = false
        }

        // NOTE: deliberately NOT registering testFramework(TestFrameworkType.Platform)
        // here. Every test in this plugin is pure JVM (no BasePlatformTestCase), and
        // that test framework alongside useJUnitPlatform() makes the Gradle test
        // executor fail to start (NoClassDefFoundError: junit/framework/TestCase, from
        // the platform's JUnit5TestSessionListener). Pure JUnit5 below is sufficient.
    }

    testImplementation(platform("org.junit:junit-bom:5.10.2"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

intellijPlatform {
    // Pure-Kotlin plugin: no Java sources and no .form GUI files, so there is
    // nothing for the IntelliJ code instrumenter to do.
    instrumentCode = false

    pluginConfiguration {
        version = project.version.toString()
        ideaVersion {
            sinceBuild = providers.gradleProperty("sinceBuild").getOrElse("241")
            // Explicit null for an open upper bound — otherwise the Gradle plugin
            // auto-derives a blocking "<branch>.*" untilBuild.
            untilBuild = provider { null }
        }
    }
}

kotlin {
    jvmToolchain(17)
}

tasks.test {
    useJUnitPlatform()
}
