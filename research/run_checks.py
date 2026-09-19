# -*- coding: utf-8 -*-
"""Exercise epub_model against fixtures and real books."""
import sys, os, posixpath
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from epub_model import (open_book, obfuscation_key, deobfuscate,
                        OBFUS_IDPF, OBFUS_ADOBE)
from epub_resolve import resolve_href


def dump_toc(nodes, depth=0, limit=[24]):
    for n in nodes:
        if limit[0] <= 0:
            return
        limit[0] -= 1
        mark = " [hidden]" if n.hidden else ""
        po = " po=%s" % n.play_order if n.play_order is not None else ""
        print("    " + "  " * depth + "- %-34s -> %s%s%s%s"
              % (n.label[:34], n.entry, ("#" + n.fragment) if n.fragment else "",
                 mark, po))
        dump_toc(n.children, depth + 1, limit)


def report(path):
    print("\n" + "#" * 100)
    print("# " + path)
    print("#" * 100)
    try:
        b = open_book(path)
    except Exception as e:
        import traceback; traceback.print_exc(); return
    print("version=%s  opf=%s  dir=%r  ppd=%s  fixed_layout=%s"
          % (b.version, b.opf_path, b.opf_dir, b.page_direction, b.fixed_layout))
    print("title   = %r" % b.metadata.get("display_title"))
    print("authors = %r" % b.metadata.get("authors"))
    print("uid     = %r" % b.metadata.get("unique_identifier"))
    print("lang    = %r" % [r["value"] for r in b.metadata.get("language", [])])
    for c in b.metadata.get("creator", []):
        print("  creator %r role=%r file_as=%r" % (c["value"], c.get("role"), c.get("file_as")))
    print("manifest=%d  spine=%d (linear=%d)  cover=%r via %s"
          % (len(b.items), len(b.spine), sum(1 for s in b.spine if s.linear),
             b.cover_entry, b.cover_source))
    print("obfuscated fonts: %r" % b.obfuscated)
    print("DRM-encrypted   : %r" % b.drm)
    for si in b.spine[:6]:
        print("  spine %-12s linear=%-5s fxl=%-5s -> %s"
              % (si.idref, si.linear, si.pre_paginated, si.item.entry))
    print("TOC (%d top-level):" % len(b.toc))
    dump_toc(b.toc, limit=[20])
    if b.landmarks:
        print("landmarks: %r" % [(n.label, n.entry) for n in b.landmarks])
    if b.page_list:
        print("page-list: %r" % [(n.label, n.entry) for n in b.page_list][:4])
    if b.warnings:
        print("WARNINGS (%d):" % len(b.warnings))
        for w in b.warnings:
            print("   ! " + w)

    # de-obfuscation proof
    for entry, algo in b.obfuscated.items():
        raw = b.zf.read(entry)
        key = obfuscation_key(algo, b.metadata["unique_identifier"])
        out = deobfuscate(raw, algo, key)
        sig = out[:4]
        known = {b"\x00\x01\x00\x00": "TTF", b"OTTO": "OTF", b"true": "TTF",
                 b"ttcf": "TTC", b"wOFF": "WOFF", b"wOF2": "WOFF2"}
        print("  deobfuscate %-28s %s -> %r %s"
              % (posixpath.basename(entry), algo.rsplit("/", 1)[-1], sig,
                 known.get(sig, "!! STILL SCRAMBLED")))

    # every in-container href in every spine doc must resolve
    bad = []
    import re
    RE = re.compile(rb'(?:src|href|xlink:href)\s*=\s*["\']([^"\']+)["\']', re.I)
    for si in b.spine[:60]:
        try:
            data = b.zf.read(si.item.entry)
        except Exception:
            continue
        base = posixpath.dirname(si.item.entry)
        for m in RE.finditer(data):
            h = m.group(1).decode("utf-8", "replace")
            e, frag, remote = resolve_href(b.index, base, h)
            if not remote and h and not h.startswith("#") and e is None:
                bad.append((si.item.entry, h))
    print("unresolvable in-document hrefs: %d %s" % (len(bad), bad[:5]))


if __name__ == "__main__":
    for p in sys.argv[1:]:
        report(p)
