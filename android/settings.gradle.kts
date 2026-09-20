pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // Chaquopy (Python runtime) lives in its own repository
        maven("https://chaquo.com/maven")
    }
}

rootProject.name = "Book Reader"
include(":app")
