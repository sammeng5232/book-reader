#!/usr/bin/env bash
#
# Builds the TeX engine (Tectonic: XeTeX + xdvipdfmx) for Android and drops
# libbookreadertex.so plus libc++_shared.so into app/src/main/jniLibs/<abi>/.
#
# Run it from WSL (or any Linux host):
#     ./build-native.sh                 # both ABIs
#     ./build-native.sh x86_64          # just the emulator one
#
# Prerequisites, set up once:
#   * Rust + targets:  rustup target add aarch64-linux-android x86_64-linux-android
#   * cargo-ndk:       cargo install cargo-ndk
#   * Android NDK:     $ANDROID_NDK_HOME (r28c here)
#   * The C stack, per ABI, built with vcpkg into $VCPKG_ROOT:
#       vcpkg install --triplet x64-android   "harfbuzz[core,freetype,graphite2,icu,png]" \
#                                             freetype graphite2 icu libpng fontconfig
#       vcpkg install --triplet arm64-android "harfbuzz[core,freetype,graphite2,icu,png]" \
#                                             freetype graphite2 icu libpng fontconfig
#
# The two patches below are not optional: without them the engine aborts the whole
# app on Android.  Both are upstream bugs that only bite on Android, and both were
# first identified by the TeXslate project (https://github.com/thobgg/TeXslate),
# whose build script this one follows.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# Cargo is painfully slow on a Windows drive mounted into WSL, so the crate can be
# built from a copy on the Linux filesystem: set CRATE_DIR / JNILIBS_OUT for that.
CRATE="${CRATE_DIR:-$HERE/bookreadertex}"
JNILIBS="${JNILIBS_OUT:-$HERE/../app/src/main/jniLibs}"

ABIS=("$@")
[ ${#ABIS[@]} -eq 0 ] && ABIS=("x86_64" "arm64-v8a")

[ -f "$HOME/.cargo/env" ] && source "$HOME/.cargo/env"
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$HOME/androidtex/android-ndk-r28c}"
export VCPKG_ROOT="${VCPKG_ROOT:-$HOME/vcpkg}"
export TECTONIC_DEP_BACKEND="vcpkg"
NDK_SYSROOT_LIB="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/sysroot/usr/lib"

echo "NDK:        $ANDROID_NDK_HOME"
echo "VCPKG_ROOT: $VCPKG_ROOT"
echo "ABIs:       ${ABIS[*]}"

registry_file() {   # registry_file <crate-glob> <path-inside-crate>
    # the arguments hold globs, so they must stay unquoted here
    local base="${CARGO_HOME:-$HOME/.cargo}/registry/src" hit
    for hit in $base/*/$1/$2; do
        [ -f "$hit" ] && { echo "$hit"; return 0; }
    done
    return 0
}

# Patch 1 - print_glyph_name() used to free an interior pointer: glibc shrugs, but
# Android's Scudo allocator aborts ("misaligned pointer when deallocating") on any
# document that reaches \XeTeXglyphname.  Upstream fixed this (it now walks the
# string by index and frees the original pointer), so this is a guard, not a patch:
# it fails loudly if a future version reintroduces the pattern.
patch_print_glyph_name() {
    local f; f="$(registry_file 'tectonic_engine_xetex-*' 'xetex/xetex-ext.c')"
    [ -z "$f" ] && { echo "  check 1: crate not fetched yet"; return 0; }
    if grep -q 'print_char(\*s++)' "$f"; then
        echo "  check 1 FAILED: print_glyph_name frees an interior pointer again -> would abort on Android" >&2
        exit 1
    fi
    echo "  check 1: print_glyph_name is safe in this version"
}

# Patch 2 - closing the same output twice unwraps a None and panics; the panic
# crosses an extern "C" frame, which aborts the process instead of unwinding.
patch_double_close() {
    local f; f="$(registry_file 'tectonic_bridge_core-*' 'src/lib.rs')"
    [ -z "$f" ] && { echo "  patch 2: crate not fetched yet"; return 0; }
    grep -q 'double-close fix' "$f" && { echo "  patch 2: already applied"; return 0; }
    perl -0777 -pi -e 's#let mut oh = self\.output_handles\[id\.idx\(\)\]\.take\(\)\.unwrap\(\);#/* double-close fix */\n        let mut oh = match self.output_handles.get_mut(id.idx()).and_then(|s| s.take()) { Some(h) => h, None => return false };#' "$f"
    grep -q 'double-close fix' "$f" || { echo "  patch 2 FAILED (crate changed?)" >&2; exit 1; }
    echo "  patch 2: applied to $f"
    rm -rf "$CRATE"/target/*/release/build/tectonic_bridge_core-* \
           "$CRATE"/target/*/release/deps/*tectonic_bridge_core* 2>/dev/null || true
}

abi_to_triplet() { case "$1" in
    x86_64)      echo x64-android ;;
    arm64-v8a)   echo arm64-android ;;
    *) echo "unknown ABI $1" >&2; return 1 ;;
esac }
abi_to_ndklib() { case "$1" in
    x86_64)      echo x86_64-linux-android ;;
    arm64-v8a)   echo aarch64-linux-android ;;
esac }
abi_to_target() { case "$1" in
    x86_64)      echo x86_64-linux-android ;;
    arm64-v8a)   echo aarch64-linux-android ;;
esac }

echo "== fetching crate sources (the patches need them on disk) =="
for abi in "${ABIS[@]}"; do
    ( cd "$CRATE" && cargo fetch --target "$(abi_to_target "$abi")" >/dev/null )
done
patch_print_glyph_name
patch_double_close

for abi in "${ABIS[@]}"; do
    triplet="$(abi_to_triplet "$abi")"
    echo "== building $abi (vcpkg triplet $triplet) =="
    if [ ! -d "$VCPKG_ROOT/installed/$triplet" ]; then
        echo "   missing C stack for $triplet - run the vcpkg line at the top of this script" >&2
        exit 1
    fi
    export VCPKGRS_TRIPLET="$triplet"
    ( cd "$CRATE" && cargo ndk -t "$abi" -o "$JNILIBS" build --release )
    # cargo-ndk copies every .so in the graph; only ours belongs in the APK.
    find "$JNILIBS/$abi" -maxdepth 1 -name '*.so' ! -name 'libbookreadertex.so' -delete
    # HarfBuzz and ICU are C++: the runtime library must travel with them.
    cp "$NDK_SYSROOT_LIB/$(abi_to_ndklib "$abi")/libc++_shared.so" "$JNILIBS/$abi/"
    ls -la "$JNILIBS/$abi"
done

echo "done - jniLibs ready for :app"
