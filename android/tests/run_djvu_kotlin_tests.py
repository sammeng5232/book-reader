"""Compile/run DjVu navigation tests on the host using the existing Gradle Kotlin jars.

Usage: python android/tests/run_djvu_kotlin_tests.py [--outline-sample path.djvu]
No Android device, network access, Gradle daemon or new dependency is required.
"""
from pathlib import Path
import argparse
import os
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--android", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--outline-sample", type=Path)
    args = parser.parse_args()
    android = args.android.resolve()
    cache = Path(os.environ.get("GRADLE_USER_HOME", Path(os.environ["USERPROFILE"]) / ".gradle")) / "caches/modules-2/files-2.1"
    java_home = Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Microsoft\jdk-17.0.20.8-hotspot"))
    java = java_home / "bin/java.exe"
    sdk = Path(os.environ.get("ANDROID_HOME", str(Path(os.environ["LOCALAPPDATA"]) / "Android/Sdk")))
    android_jar = sdk / "platforms/android-35/android.jar"
    def jar(group, artifact, version):
        return next((cache / group / artifact / version).rglob(f"{artifact}-{version}.jar"))
    stdlib = jar("org.jetbrains.kotlin", "kotlin-stdlib", "2.0.21")
    dependencies = [jar("org.jetbrains.kotlin", "kotlin-compiler-embeddable", "2.0.21"), stdlib,
        jar("org.jetbrains.kotlin", "kotlin-script-runtime", "2.0.21"),
        jar("org.jetbrains.kotlin", "kotlin-reflect", "1.6.10"),
        jar("org.jetbrains.kotlin", "kotlin-daemon-embeddable", "2.0.21"),
        jar("org.jetbrains.intellij.deps", "trove4j", "1.0.20200330"),
        jar("org.jetbrains.kotlinx", "kotlinx-coroutines-core-jvm", "1.6.4"),
        jar("org.jetbrains", "annotations", "13.0")]
    source = android / "app/src/main/java/com/bookreader/djvu"
    tests = android / "tests/kotlin"
    sources = [source / name for name in ("DjvuOutline.kt", "DjvuDocument.kt", "Codecs.kt", "ZpTable.kt")]
    sources += [tests / "DjvuOutlineHostTest.kt", tests / "DjvuDocumentHostTest.kt"]
    with tempfile.TemporaryDirectory(prefix="bookreader-djvu-tests-") as tmp:
        subprocess.run([str(java), "-cp", os.pathsep.join(map(str, dependencies)),
            "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler", "-no-stdlib", "-no-reflect", "-jvm-target", "17",
            "-classpath", os.pathsep.join(map(str, [stdlib, android_jar])), "-d", tmp, *map(str, sources)], check=True)
        command = [str(java), "-cp", os.pathsep.join(map(str, [tmp, stdlib, android_jar]))]
        subprocess.run(command + ["com.bookreader.djvu.DjvuOutlineHostTestKt"], check=True)
        samples = [android.parent / "tests/samples/ia_jstor_20637537.djvu", android.parent / "tests/samples/ia_indiansummer.djvu"]
        if args.outline_sample:
            samples.append(args.outline_sample.resolve())
        subprocess.run(command + ["com.bookreader.djvu.DjvuDocumentHostTestKt", *map(str, samples)], check=True)


if __name__ == "__main__":
    main()
