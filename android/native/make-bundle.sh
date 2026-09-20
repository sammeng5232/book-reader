#!/usr/bin/env bash
#
# Assembles the offline TeX bundle that ships inside the APK.
#
# Tectonic normally fetches LaTeX packages from the network on demand.  The app
# must work with no network at all, so we compile representative documents here
# on the build host, which fills Tectonic's cache with exactly the files those
# documents need, and then copy that cache into the app's assets as a "directory
# bundle" (plus the SHA256SUM fingerprint the engine asks for).
#
#     ./make-bundle.sh [extra.tex ...]
#
# Add a .tex here whenever the converter starts emitting something new; anything
# missing from the bundle is simply "file not found" on the device.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ASSETS="$HERE/../app/src/main/assets/texbundle"
PROBES="$HERE/probes"
TECTONIC="${TECTONIC:-$HOME/.cargo/bin/tectonic}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/tectonic/bundles/data"

[ -x "$TECTONIC" ] || { echo "no tectonic at $TECTONIC (cargo install tectonic)" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

shopt -s nullglob
docs=("$PROBES"/*.tex "$@")
[ ${#docs[@]} -eq 0 ] && { echo "no probe documents in $PROBES" >&2; exit 1; }

echo "== compiling ${#docs[@]} probe document(s) to populate the cache =="
for doc in "${docs[@]}"; do
    cp "$doc" "$work/"
    ( cd "$work" && "$TECTONIC" -X compile "$(basename "$doc")" --outfmt pdf >/dev/null 2>&1 ) \
        && echo "   ok   $(basename "$doc")" \
        || echo "   WARN $(basename "$doc") did not compile cleanly (its files are still cached)"
done

dir="$(ls -d "$CACHE"/*/ 2>/dev/null | head -1)"
[ -n "$dir" ] || { echo "no cached bundle under $CACHE" >&2; exit 1; }

echo "== copying $(find "$dir" -type f | wc -l) files into the APK assets =="
rm -rf "$ASSETS"
mkdir -p "$ASSETS"
cp -r "$dir"/. "$ASSETS"/
# The engine wants a fingerprint for the bundle; make one from the contents so it
# changes whenever the bundle does (that also re-keys the cached format file).
( cd "$ASSETS" && rm -f SHA256SUM \
  && find . -type f -exec sha256sum {} + | sort -k2 | sha256sum | cut -d' ' -f1 > SHA256SUM )

echo "bundle: $(du -sh "$ASSETS" | cut -f1) across $(find "$ASSETS" -type f | wc -l) files"
echo "fingerprint: $(cat "$ASSETS/SHA256SUM")"
