plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

// The reader's Python lives once, in the desktop project; copy the modules the app
// needs into the build so there is never a second edited copy of them.
val sharedPythonModules = listOf(
    "epublib.py", "mobi.py", "bookformats.py", "store.py", "djvu.py", "djvupdf.py", "latexexport.py",
)
val sharedPythonDir = layout.buildDirectory.dir("generated/pythonShared")
val copySharedPython = tasks.register<Copy>("copySharedPython") {
    from(rootProject.projectDir.parentFile) { include(sharedPythonModules) }
    into(sharedPythonDir)
}
tasks.named("preBuild") { dependsOn(copySharedPython) }
// Chaquopy collects the Python sources in its own task, which does not go through preBuild.
tasks.matching { it.name.matches(Regex("merge.*PythonSources")) }.configureEach {
    dependsOn(copySharedPython)
}

// The reading engine (reader.js / reader.css) also lives once, in the desktop project.
val readerAssetsDir = layout.buildDirectory.dir("generated/readerAssets")
val copyReaderAssets = tasks.register<Copy>("copyReaderAssets") {
    from(rootProject.projectDir.parentFile) { include("assets/reader.js", "assets/reader.css") }
    into(readerAssetsDir)
}
tasks.named("preBuild") { dependsOn(copyReaderAssets) }
tasks.matching { it.name.matches(Regex("merge.*Assets")) }.configureEach {
    dependsOn(copyReaderAssets)
}

android {
    namespace = "com.bookreader"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.bookreader"
        minSdk = 29
        targetSdk = 35
        versionCode = 1
        versionName = "1.2.0"
        // Only the ABIs we build the TeX engine for.
        ndk { abiFilters += listOf("x86_64", "arm64-v8a") }
    }

    // The TeX engine is a native executable/library: it must be unpacked on install
    // so it can be executed and so its data files can be read by path.
    packaging {
        jniLibs {
            useLegacyPackaging = true
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        debug {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    // jniLibs are produced outside Gradle by native/build-native.sh (cargo-ndk).
    sourceSets["main"].jniLibs.srcDirs("src/main/jniLibs")
    // reader.js / reader.css, copied from the desktop project by copyReaderAssets.
    sourceSets["main"].assets.srcDir(readerAssetsDir.map { it.asFile })
}

chaquopy {
    defaultConfig {
        version = "3.13"
        // 3.14 has almost no Android wheels yet; 3.13 runs the same sources.
        buildPython("C:/Users/mengz/AppData/Local/Programs/Python/Python313/python.exe")
        pip {
            install("lxml==5.3.0")
            install("Pillow==11.0.0")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
            srcDir(sharedPythonDir)
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.recyclerview:recyclerview:1.3.2")
    // WebViewAssetLoader + WebMessageListener for the reader (pure Java client-side
    // glue around the system WebView; no bundled Chromium).
    implementation("androidx.webkit:webkit:1.12.1")
}
