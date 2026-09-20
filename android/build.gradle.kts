// Top-level build file.  Plugin versions are declared here and applied in :app.
plugins {
    id("com.android.application") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    // Runs the reader's Python (the same modules the Windows app uses) on the device.
    id("com.chaquo.python") version "17.0.0" apply false
}
