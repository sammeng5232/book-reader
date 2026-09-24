#!/usr/bin/env python3
"""Reject offline bundles whose installed font metrics refer to missing font files.

Tectonic's format build may fetch a TFM without ever rendering a glyph from it.
A successful simple probe therefore does not prove that the PFB needed by a later
math document is present. Check the pdftex.map dependency for every shipped TFM.
"""
from pathlib import Path
import argparse
import re
import sys
import shutil


def missing_fonts(bundle: Path) -> list[tuple[str, str]]:
    mapping = {}
    for line in (bundle / "pdftex.map").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("%"):
            continue
        mapping[line.split()[0]] = re.findall(r"<+([^\s<>]+\.(?:pfb|enc))", line)
    missing = {(metric.name, filename)
               for metric in bundle.glob("*.tfm")
               for filename in mapping.get(metric.stem, ())
               if not (bundle / filename).is_file()}
    for definition in bundle.glob("*.fd"):
        for font in re.findall(r"\\UnicodeFontFile\{([A-Za-z0-9_-]+)\}", definition.read_text(encoding="utf-8")):
            filename = font + ".otf"
            if not (bundle / filename).is_file():
                missing.add((definition.name, filename))
    return sorted(missing)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--font-source", type=Path,
                        help="optional local TeX fonts directory from which to complete mapped fonts")
    args = parser.parse_args()
    missing = missing_fonts(args.bundle)
    if missing and args.font_source:
        wanted = {filename for _, filename in missing}
        sources = {path.name: path for path in args.font_source.rglob("*")
                   if path.is_file() and path.name in wanted}
        for filename in sorted(wanted):
            if filename in sources and Path(filename).name == filename:
                shutil.copyfile(sources[filename], args.bundle / filename)
                print(f"Included {filename}")
        missing = missing_fonts(args.bundle)
    if missing:
        for metric, filename in missing:
            print(f"{metric}: missing {filename}", file=sys.stderr)
        print("Offline TeX bundle is incomplete; include these fonts and rebuild its fingerprint.", file=sys.stderr)
        return 1
    print("Offline TeX font dependencies are complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
