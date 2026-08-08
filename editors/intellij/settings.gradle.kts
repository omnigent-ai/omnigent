// Settings for the Omnigent IntelliJ/PyCharm plugin.
//
// Repositories are declared at the project level (build.gradle.kts) via
// `intellijPlatform { defaultRepositories() }`; nothing extra is needed here.

pluginManagement {
    repositories {
        gradlePluginPortal()
        mavenCentral()
    }
}

rootProject.name = "omnigent-intellij"
