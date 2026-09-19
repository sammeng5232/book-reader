# -*- coding: utf-8 -*-
"""
epub_model.py -- reference EPUB 2/3 parser. Standard library only.

Deliberately namespace-tolerant: real books in the wild put the OPF in the
wrong namespace or in no namespace at all, so every element match is done on
the *local name*. Namespaces are only used to disambiguate dc: vs opf: and to
recognise the epub:type attribute.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import unquote

from epub_resolve import ZipIndex, resolve_href, split_href, is_remote, normalize_zip_path

# ---- namespaces -----------------------------------------------------------
NS_OCF = "urn:oasis:names:tc:opendocument:xmlns:container"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_NCX = "http://www.daisy.org/z3986/2005/ncx/"
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_EPUB = "http://www.idpf.org/2007/ops"        # the epub: prefix (epub:type)
NS_ENC = "http://www.w3.org/2001/04/xmlenc#"

OBFUS_IDPF = "http://www.idpf.org/2008/embedding"
OBFUS_ADOBE = "http://ns.adobe.com/pdf/enc#RC"

XHTML_TYPES = {"application/xhtml+xml", "text/html", "application/xml", "text/xml"}
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/svg+xml", "image/webp"}


# ---- helpers --------------------------------------------------------------
def ln(el) -> str:
    """Local name of an element, namespace-insensitive."""
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) and "}" in t else (t or "")


def kids(el, name: str):
    return [c for c in el if ln(c) == name]


def find_deep(root, name: str):
    return [e for e in root.iter() if ln(e) == name]


def attr(el, name: str, *nss) -> str | None:
    """Get an attribute by local name: unprefixed first, then each namespace."""
    v = el.get(name)
    if v is not None:
        return v
    for ns in nss:
        v = el.get("{%s}%s" % (ns, name))
        if v is not None:
            return v
    # last resort: scan (handles unexpected prefixes/namespaces)
    for k, val in el.attrib.items():
        if k.rsplit("}", 1)[-1] == name:
            return val
    return None


def text_of(el) -> str:
    """Concatenated, whitespace-normalised text of an element subtree.

    Used for nav <a>/<span> labels (which may contain <b>, <i>, <img alt=...>)
    and for NCX <text>.
    """
    parts = []
    for node in el.iter():
        if node is not el and ln(node) in ("img", "image"):
            alt = node.get("alt") or node.get("title")
            if alt:
                parts.append(alt)
        if node.text:
            parts.append(node.text)
        if node is not el and node.tail:
            parts.append(node.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_xml_lenient(data: bytes, what: str = "") -> ET.Element:
    """Parse XML; on failure, retry with progressively dirtier repairs.

    Order: strict -> strip BOM/leading junk -> XMLParser(recover) is NOT in
    the stdlib, so fall back to entity-stubbing, then to the HTML tree builder.
    Raises ValueError only if everything fails.
    """
    for attempt in (data, _strip_preamble(data), _stub_entities(_strip_preamble(data))):
        try:
            return ET.fromstring(attempt)
        except ET.ParseError:
            continue
    tree = _html_to_element(data)
    if tree is None:
        raise ValueError("cannot parse %s" % (what or "document"))
    return tree


def _strip_preamble(data: bytes) -> bytes:
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    i = data.find(b"<")
    return data[i:] if i > 0 else data


_ENT = re.compile(rb"&(?!#|amp;|lt;|gt;|quot;|apos;)([A-Za-z][A-Za-z0-9]*);")


def _stub_entities(data: bytes) -> bytes:
    """XHTML files routinely use &nbsp; &mdash; etc. without a DOCTYPE, which
    is a hard XML parse error. Replace named entities with numeric ones."""
    import html.entities as he

    def sub(m):
        name = m.group(1).decode("ascii")
        cp = he.name2codepoint.get(name)
        return ("&#%d;" % cp).encode("ascii") if cp else b"&amp;" + m.group(1) + b";"

    return _ENT.sub(sub, data)


class _TreeHTMLParser(HTMLParser):
    """Recovery parser: builds an ElementTree from tag soup.

    html.parser is chosen over lxml.html on purpose -- it is stdlib, it never
    raises on malformed input, and it does not need libxml2. Attribute names
    keep their raw spelling, so 'epub:type' stays 'epub:type' (NOT namespaced);
    call nav_type() which checks both spellings.
    """

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("html")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        el = ET.SubElement(self.stack[-1], tag,
                           {k: (v if v is not None else "") for k, v in attrs})
        if tag not in self.VOID:
            self.stack.append(el)

    def handle_startendtag(self, tag, attrs):
        ET.SubElement(self.stack[-1], tag,
                      {k: (v if v is not None else "") for k, v in attrs})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        cur = self.stack[-1]
        if len(cur):
            cur[-1].tail = (cur[-1].tail or "") + data
        else:
            cur.text = (cur.text or "") + data


def _html_to_element(data: bytes) -> ET.Element | None:
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    p = _TreeHTMLParser()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    return p.root if len(p.root) else None


# ---- data model -----------------------------------------------------------
@dataclass
class Item:
    id: str
    href: str
    media_type: str
    properties: frozenset
    entry: str | None          # resolved zip entry, None if unresolvable
    fallback: str | None = None
    media_overlay: str | None = None


@dataclass
class SpineItem:
    idref: str
    item: Item | None
    linear: bool
    properties: frozenset

    @property
    def pre_paginated(self):
        return "rendition:layout-pre-paginated" in self.properties


@dataclass
class TocNode:
    label: str
    entry: str | None
    fragment: str
    href: str
    children: list = field(default_factory=list)
    hidden: bool = False
    play_order: int | None = None
    landmark_type: str = ""      # landmarks nav only: epub:type of the <a>


@dataclass
class Book:
    path: str
    zf: zipfile.ZipFile
    index: ZipIndex
    opf_path: str
    opf_dir: str
    version: str
    metadata: dict
    items: dict                 # id -> Item
    spine: list                 # [SpineItem]
    page_direction: str
    toc: list                   # [TocNode]
    landmarks: list
    page_list: list
    cover_entry: str | None
    cover_source: str
    obfuscated: dict            # zip entry -> algorithm URI
    drm: list                   # [(entry, algorithm)] genuinely encrypted
    fixed_layout: bool
    warnings: list


# ---- 1. discovery ---------------------------------------------------------
def check_mimetype(zf: zipfile.ZipFile, warn: list) -> None:
    infos = zf.infolist()
    if not infos:
        warn.append("mimetype: zip is empty")
        return
    first = infos[0]
    if first.filename != "mimetype":
        warn.append("mimetype: not the first zip entry (first is %r)" % first.filename)
    if first.filename == "mimetype":
        if first.compress_type != zipfile.ZIP_STORED:
            warn.append("mimetype: is compressed (must be stored)")
        if first.extra:
            warn.append("mimetype: has an extra field (must have none)")
    try:
        data = zf.read("mimetype")
    except KeyError:
        warn.append("mimetype: entry missing entirely")
        return
    if data != b"application/epub+zip":
        if data.strip() == b"application/epub+zip":
            warn.append("mimetype: has leading/trailing whitespace")
        else:
            warn.append("mimetype: wrong content %r" % data[:40])
    # NOTE: every one of these is a warning, never a refusal. Kobo, Calibre
    # and countless converters ship books that fail at least one.


def find_opf(zf: zipfile.ZipFile, index: ZipIndex, warn: list) -> str:
    """Locate the package document. Returns a zip entry name."""
    try:
        raw = zf.read("META-INF/container.xml")
    except KeyError:
        raw = None
        hit = index.lookup("META-INF/container.xml")   # case-mangled containers
        if hit:
            raw = zf.read(hit)
            warn.append("container.xml found only case-insensitively: %r" % hit)

    if raw is not None:
        try:
            root = parse_xml_lenient(raw, "container.xml")
            rfs = find_deep(root, "rootfile")
            # Prefer the correct media-type; the spec allows several rootfiles
            # (multiple renditions) but does not say how to choose -> take the
            # FIRST one, which is the universal reading-system convention.
            good = [r for r in rfs
                    if (r.get("media-type") or "") == "application/oebps-package+xml"]
            chosen = (good or rfs)
            if len(rfs) > 1:
                warn.append("container.xml lists %d rootfiles; using the first"
                            % len(rfs))
            for r in chosen:
                fp = r.get("full-path")
                if not fp:
                    continue
                # full-path is a URL relative to the container ROOT, not to
                # META-INF. It may be percent-encoded.
                entry, _, _ = resolve_href(index, "", fp)
                if entry:
                    return entry
                warn.append("rootfile full-path %r does not exist in the zip" % fp)
        except Exception as e:
            warn.append("container.xml unparsable (%s); scanning for an OPF" % e)

    # Fallback: any *.opf in the archive, shallowest path first.
    cands = [n for n in index.names if n.lower().endswith(".opf")]
    if not cands:
        raise ValueError("no package document found (no container.xml, no *.opf)")
    cands.sort(key=lambda n: (n.count("/"), len(n)))
    warn.append("recovered OPF by scanning: %r" % cands[0])
    return cands[0]


# ---- 2. encryption --------------------------------------------------------
def read_encryption(zf, index, warn) -> tuple[dict, list]:
    """Returns (obfuscated {entry: algo}, drm [(entry, algo)]).

    Font 'obfuscation' is NOT DRM: it is a documented, keyless XOR scramble and
    both variants are ~10 lines to undo. Real DRM (Adobe ADEPT, Apple FairPlay,
    LCP) uses AES and needs a key we do not have.
    """
    obf, drm = {}, []
    try:
        raw = zf.read("META-INF/encryption.xml")
    except KeyError:
        return obf, drm
    try:
        root = parse_xml_lenient(raw, "encryption.xml")
    except Exception as e:
        warn.append("encryption.xml unparsable: %s" % e)
        return obf, drm
    for ed in find_deep(root, "EncryptedData"):
        algo = None
        for m in find_deep(ed, "EncryptionMethod"):
            algo = m.get("Algorithm")
        for cr in find_deep(ed, "CipherReference"):
            uri = cr.get("URI") or ""
            entry, _, _ = resolve_href(index, "", uri)
            entry = entry or normalize_zip_path("", unquote(uri))
            if algo in (OBFUS_IDPF, OBFUS_ADOBE):
                obf[entry] = algo
            else:
                drm.append((entry, algo))
    return obf, drm


def obfuscation_key(algorithm: str, unique_identifier: str) -> bytes:
    """Derive the de-obfuscation key. VERIFIED against a real Adobe-obfuscated
    book on this machine (see report)."""
    import hashlib
    if algorithm == OBFUS_IDPF:
        # EPUB 3.3 sec 4.4.3: strip U+0020/09/0D/0A, SHA-1 of UTF-8 -> 20 bytes
        ws = (chr(0x20), chr(0x09), chr(0x0D), chr(0x0A))
        s = "".join(c for c in unique_identifier if c not in ws)
        return hashlib.sha1(s.encode("utf-8")).digest()
    if algorithm == OBFUS_ADOBE:
        # Adobe: strip 'urn:uuid:' and hyphens, take 32 hex chars -> 16 bytes
        uid = unique_identifier.strip()
        low = uid.lower()
        if low.startswith("urn:uuid:"):
            uid = uid[9:]
        uid = "".join(c for c in uid if c in "0123456789abcdefABCDEF")
        return bytes.fromhex(uid[:32])
    raise ValueError("unknown obfuscation algorithm %r" % algorithm)


def deobfuscate(data: bytes, algorithm: str, key: bytes) -> bytes:
    """XOR the head of the file with the cycling key.
    IDPF mangles 1040 bytes; Adobe mangles 1024."""
    n = min(1040 if algorithm == OBFUS_IDPF else 1024, len(data))
    head = bytearray(data[:n])
    klen = len(key)
    for i in range(n):
        head[i] ^= key[i % klen]
    return bytes(head) + data[n:]


# ---- 3. OPF ---------------------------------------------------------------
_DC_FIELDS = ("title", "creator", "contributor", "language", "identifier",
              "date", "publisher", "description", "subject", "rights", "type")

MARC_ROLES = {"aut": "Author", "edt": "Editor", "trl": "Translator",
              "ill": "Illustrator", "nrt": "Narrator", "bkp": "Producer",
              "pbl": "Publisher", "ctb": "Contributor"}


def parse_metadata(pkg, warn) -> dict:
    md_el = next((c for c in pkg if ln(c) == "metadata"), None)
    out = {k: [] for k in _DC_FIELDS}
    out["meta"] = []
    out["refines"] = {}
    out["unique_identifier"] = None
    if md_el is None:
        warn.append("OPF has no <metadata>")
        return out

    # Pass 1: collect elements, keyed by id for refinement in pass 2.
    by_id = {}
    records = []
    for el in md_el:
        name = ln(el)
        if name == "meta":
            prop = el.get("property")          # EPUB 3 form
            if prop is not None:
                out["meta"].append({
                    "property": prop.strip(),
                    "value": (el.text or "").strip(),
                    "refines": (el.get("refines") or "").strip(),
                    "scheme": el.get("scheme"),
                    "id": el.get("id"),
                })
            else:
                nm = el.get("name")            # EPUB 2 form <meta name= content=>
                if nm:
                    out["meta"].append({"property": None, "name": nm.strip(),
                                        "content": (el.get("content") or "").strip(),
                                        "refines": "", "scheme": None, "id": None})
            continue
        if name not in _DC_FIELDS:
            continue
        rec = {
            "value": (el.text or "").strip(),
            "id": el.get("id"),
            # EPUB 2 refinements live as opf:-prefixed attributes
            "file_as": attr(el, "file-as", NS_OPF),
            "role": attr(el, "role", NS_OPF),
            "scheme": attr(el, "scheme", NS_OPF),
            "event": attr(el, "event", NS_OPF),
            "lang": el.get("{http://www.w3.org/XML/1998/namespace}lang"),
            "dir": el.get("dir"),
        }
        records.append((name, rec))
        if rec["id"]:
            by_id[rec["id"]] = rec

    # Pass 2: apply EPUB 3 <meta refines="#id" property="..."> subexpressions.
    for m in out["meta"]:
        ref = m.get("refines") or ""
        if not ref.startswith("#"):
            continue
        target = by_id.get(ref[1:])
        if target is None:
            continue
        prop, val = m["property"], m["value"]
        if prop == "file-as":
            target["file_as"] = val
        elif prop == "role":
            target["role"] = val
        elif prop == "title-type":
            target["title_type"] = val
        elif prop == "display-seq":
            try:
                target["display_seq"] = int(val)
            except ValueError:
                pass
        elif prop == "alternate-script":
            target.setdefault("alternates", []).append(val)
        target.setdefault("refined", {})[prop] = val

    for name, rec in records:
        out[name].append(rec)

    # Which dc:identifier is THE identifier? -> package/@unique-identifier.
    uid_ref = pkg.get("unique-identifier")
    uid = None
    if uid_ref:
        for r in out["identifier"]:
            if r["id"] == uid_ref:
                uid = r["value"]
                break
        if uid is None:
            warn.append("unique-identifier=%r matches no dc:identifier" % uid_ref)
    if uid is None and out["identifier"]:
        uid = out["identifier"][0]["value"]
    out["unique_identifier"] = uid

    # Preferred display title: the one refined as title-type=main, else first.
    titles = out["title"]
    main = next((t for t in titles if t.get("title_type") == "main"), None)
    out["display_title"] = (main or (titles[0] if titles else {})).get("value") or ""
    # Authors: creators whose role is missing or 'aut'.
    out["authors"] = [c["value"] for c in out["creator"]
                      if not c.get("role") or c.get("role") == "aut"] \
        or [c["value"] for c in out["creator"]]
    return out


def parse_manifest(pkg, index, opf_dir, warn) -> dict:
    man = next((c for c in pkg if ln(c) == "manifest"), None)
    items = {}
    if man is None:
        warn.append("OPF has no <manifest>")
        return items
    for el in kids(man, "item"):
        iid = el.get("id")
        href = attr(el, "href", NS_OPF) or ""
        mt = (el.get("media-type") or "").strip().lower()
        props = frozenset((el.get("properties") or "").split())
        entry = None
        if href and not is_remote(href):
            entry, _, _ = resolve_href(index, opf_dir, href)
            if entry is None:
                warn.append("manifest item %r href %r resolves to nothing" % (iid, href))
        it = Item(iid, href, mt, props, entry,
                  el.get("fallback"), el.get("media-overlay"))
        if iid is None:
            warn.append("manifest item with no id (href=%r) skipped" % href)
            continue
        if iid in items:
            # Duplicate ids are illegal but happen. FIRST wins, because the
            # spine's idref was almost certainly written against the first.
            warn.append("duplicate manifest id %r; keeping the first" % iid)
            continue
        items[iid] = it
    return items


def parse_spine(pkg, items, warn) -> tuple[list, str, str | None]:
    sp = next((c for c in pkg if ln(c) == "spine"), None)
    if sp is None:
        warn.append("OPF has no <spine>")
        return [], "ltr", None
    out = []
    seen = set()
    for el in kids(sp, "itemref"):
        idref = el.get("idref")
        it = items.get(idref)
        if it is None:
            warn.append("spine itemref idref=%r is not in the manifest; dropped" % idref)
            continue
        if it.entry is None:
            warn.append("spine item %r points at a missing file %r; dropped"
                        % (idref, it.href))
            continue
        if idref in seen:
            warn.append("spine references %r more than once" % idref)
        seen.add(idref)
        linear = (el.get("linear") or "yes").strip().lower() != "no"
        out.append(SpineItem(idref, it, linear,
                             frozenset((el.get("properties") or "").split())))
    ppd = (sp.get("page-progression-direction") or "").strip().lower()
    if ppd not in ("ltr", "rtl"):
        ppd = "ltr"        # 'default' and absent both mean 'you decide'
    return out, ppd, sp.get("toc")


# ---- 4. TOC ---------------------------------------------------------------
def nav_type(el) -> str:
    """epub:type of a <nav>, across namespaced and raw-attribute parses."""
    v = el.get("{%s}type" % NS_EPUB) or el.get("epub:type") or el.get("type") or ""
    return v.strip().lower()


def is_hidden(el) -> bool:
    """HTML boolean attribute: present (even empty) == hidden."""
    if "hidden" in el.attrib:
        return (el.attrib.get("hidden") or "").strip().lower() != "false"
    return False


def parse_nav_doc(zf, index, nav_entry, warn):
    """EPUB 3 navigation document -> (toc, landmarks, page_list)."""
    try:
        raw = zf.read(nav_entry)
    except KeyError:
        return [], [], []
    try:
        root = parse_xml_lenient(raw, nav_entry)
    except Exception as e:
        warn.append("nav document unparsable: %s" % e)
        return [], [], []
    base = posixpath.dirname(nav_entry)

    def walk_ol(ol):
        nodes = []
        for li in kids(ol, "li"):
            a = next((c for c in li if ln(c) in ("a", "span")), None)
            if a is None:
                continue
            href = a.get("href") or ""
            entry, frag, remote = (None, "", False)
            if href:
                entry, frag, remote = resolve_href(index, base, href)
            node = TocNode(label=text_of(a) or "(untitled)",
                           entry=None if remote else entry,
                           fragment=frag, href=href,
                           hidden=is_hidden(li))
            for sub in kids(li, "ol"):
                node.children.extend(walk_ol(sub))
            nodes.append(node)
        return nodes

    toc, landmarks, page_list = [], [], []
    navs = find_deep(root, "nav")
    for nav in navs:
        t = nav_type(nav)
        ols = kids(nav, "ol") or find_deep(nav, "ol")
        if not ols:
            continue
        built = walk_ol(ols[0])
        if t == "toc" and not toc:
            toc = built
        elif t == "landmarks" and not landmarks:
            # each landmark <a> carries its own epub:type (cover, bodymatter,
            # toc, ...) -- that, not the label, is what a reader dispatches on
            anchors = [a for a in find_deep(nav, "a")]
            for node, a in zip(built, anchors):
                node.landmark_type = nav_type(a)
            landmarks = built
        elif t == "page-list" and not page_list:
            page_list = built
    # A nav document with exactly one un-typed <nav> is common in sloppy EPUB3;
    # treat it as the toc rather than showing the user nothing.
    if not toc:
        for nav in navs:
            if not nav_type(nav):
                ols = kids(nav, "ol") or find_deep(nav, "ol")
                if ols:
                    warn.append("nav document has no epub:type='toc'; using the "
                                "first untyped <nav>")
                    toc = walk_ol(ols[0])
                    break
    return toc, landmarks, page_list


def _play_order(raw):
    """Informational only. NEVER let this raise: real books use '00', '002',
    ' 3 ', and (verified on this machine) bare HEX like '1D' and '2A'."""
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    for base in (10, 16):
        try:
            return int(s, base)
        except ValueError:
            continue
    return None


def parse_ncx(zf, index, ncx_entry, warn):
    """EPUB 2 NCX -> (toc, page_list)."""
    try:
        raw = zf.read(ncx_entry)
    except KeyError:
        return [], []
    try:
        root = parse_xml_lenient(raw, ncx_entry)
    except Exception as e:
        warn.append("NCX unparsable: %s" % e)
        return [], []
    base = posixpath.dirname(ncx_entry)

    def label_of(np):
        for nl in kids(np, "navLabel"):
            for t in kids(nl, "text"):
                return text_of(t)
        return ""

    def src_of(np):
        for c in kids(np, "content"):
            return c.get("src") or ""
        return ""

    def walk(parent, tag="navPoint"):
        nodes = []
        for np in kids(parent, tag):
            href = src_of(np)
            entry, frag, remote = (None, "", False)
            if href:
                entry, frag, remote = resolve_href(index, base, href)
            node = TocNode(label=label_of(np) or "(untitled)",
                           entry=None if remote else entry,
                           fragment=frag, href=href,
                           play_order=_play_order(np.get("playOrder")))
            node.children = walk(np, "navPoint")       # navPoints nest
            nodes.append(node)
        # DOCUMENT ORDER IS AUTHORITATIVE -- never sort by playOrder.
        # Verified on a real book on this machine whose playOrder values are
        # HEXADECIMAL ('00','01',...,'1D','1E','1F','20',...,'2A','2F'):
        # decimal parsing raises on half of them and silently mis-orders the
        # rest. In every real NCX checked, document order already equalled
        # playOrder order, so sorting buys nothing and can only corrupt.
        return nodes

    toc = []
    for nm in find_deep(root, "navMap"):
        toc = walk(nm)
        break
    page_list = []
    for pl in find_deep(root, "pageList"):
        page_list = walk(pl, "pageTarget")
        break
    return toc, page_list


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)
_H_RE = re.compile(rb"<h[1-3][^>]*>(.*?)</h[1-3]>", re.I | re.S)
_TAG_RE = re.compile(rb"<[^>]+>")


def toc_from_spine(spine, zf) -> list:
    """Last-resort TOC: one entry per linear spine item, labelled from the
    document's <title>, else its first <h1>-<h3>, else the file name."""
    import html as _html
    out = []
    for si in spine:
        if not si.linear:
            continue
        label = None
        try:
            raw = zf.read(si.item.entry)[:65536]
            for rx in (_TITLE_RE, _H_RE):
                m = rx.search(raw)
                if m:
                    txt = _TAG_RE.sub(b" ", m.group(1)).decode("utf-8", "replace")
                    txt = re.sub(r"\s+", " ", _html.unescape(txt)).strip()
                    if txt:
                        label = txt
                        break
        except Exception:
            pass
        out.append(TocNode(label=label or posixpath.basename(si.item.entry),
                           entry=si.item.entry, fragment="", href=si.item.href))
    return out


# ---- 5. cover -------------------------------------------------------------
def find_cover(pkg, items, index, opf_dir, md, warn) -> tuple[str | None, str]:
    """Priority order, highest first."""
    # 1. EPUB 3: manifest properties="cover-image"
    for it in items.values():
        if "cover-image" in it.properties and it.entry:
            return it.entry, "manifest properties=cover-image"

    # 2. EPUB 2: <meta name="cover" content="ITEM-ID"/>
    for m in md.get("meta", []):
        if (m.get("name") or "").strip().lower() == "cover":
            cid = (m.get("content") or "").strip()
            it = items.get(cid)
            if it and it.entry and (it.media_type in IMAGE_TYPES
                                    or it.media_type.startswith("image/")):
                return it.entry, "meta name=cover -> %r" % cid
            if it and it.entry and it.media_type in XHTML_TYPES:
                # Some books point 'cover' at the cover *page*. Pull the first
                # image out of it.
                got = _first_image_in(index, it.entry)
                if got:
                    return got, "meta name=cover -> XHTML page -> first <img>"
            # content may (illegally) be a href rather than an id
            e, _, _ = resolve_href(index, opf_dir, cid)
            if e:
                return e, "meta name=cover (href, not id)"

    # 3. EPUB 2 guide: <reference type="cover" href="..."/>
    guide = next((c for c in pkg if ln(c) == "guide"), None)
    if guide is not None:
        for r in kids(guide, "reference"):
            if (r.get("type") or "").strip().lower() in ("cover", "coverimagestandard"):
                e, _, _ = resolve_href(index, opf_dir, r.get("href") or "")
                if e:
                    if e.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                        got = _first_image_in(index, e)
                        if got:
                            return got, "guide type=cover -> first <img>"
                    else:
                        return e, "guide type=cover"

    # 4. filename/id heuristics over image items only
    cands = [it for it in items.values()
             if it.entry and it.media_type.startswith("image/")]
    for it in cands:
        base = posixpath.basename(it.entry).lower()
        if base.startswith("cover") or (it.id or "").lower() in ("cover", "cover-image", "coverimage"):
            return it.entry, "filename/id heuristic"
    for it in cands:
        if "cover" in (it.entry + " " + (it.id or "")).lower():
            return it.entry, "loose 'cover' substring"

    # 5. first image of the first linear spine document
    return None, "not found"


_IMG_RE = re.compile(rb"""<(?:img|image)\b[^>]*?\b(?:src|xlink:href|href)\s*=\s*["']([^"']+)["']""",
                     re.I | re.S)


def _first_image_in(index, entry) -> str | None:
    try:
        import zipfile as _z
        raw = index_zip_read(entry)
    except Exception:
        return None
    m = _IMG_RE.search(raw or b"")
    if not m:
        return None
    href = m.group(1).decode("utf-8", "replace")
    e, _, _ = resolve_href(index, posixpath.dirname(entry), href)
    return e


index_zip_read = lambda entry: None     # rebound per-book in open_book()


# ---- 6. top level ---------------------------------------------------------
def open_book(path: str) -> Book:
    global index_zip_read
    warn: list = []
    zf = zipfile.ZipFile(path)

    # Zip-bomb guard. Refuse absurd expansion before reading anything big.
    infos = [i for i in zf.infolist() if not i.is_dir()]
    total = sum(i.file_size for i in infos)
    comp = sum(i.compress_size for i in infos) or 1
    if total > 4 * 1024 ** 3:
        raise ValueError("refusing: %.1f GB uncompressed" % (total / 1024 ** 3))
    if total / comp > 200 and total > 256 * 1024 ** 2:
        raise ValueError("refusing: compression ratio %.0f:1 looks like a zip bomb"
                         % (total / comp))

    check_mimetype(zf, warn)
    index = ZipIndex(zf)
    index_zip_read = lambda e: zf.read(e) if e in index._exact else None

    opf_path = find_opf(zf, index, warn)
    opf_dir = posixpath.dirname(opf_path)
    pkg = parse_xml_lenient(zf.read(opf_path), opf_path)
    if ln(pkg) != "package":
        warn.append("OPF root element is <%s>, not <package>" % ln(pkg))
    version = (pkg.get("version") or "").strip()

    obf, drm = read_encryption(zf, index, warn)
    md = parse_metadata(pkg, warn)
    items = parse_manifest(pkg, index, opf_dir, warn)
    spine, ppd, toc_idref = parse_spine(pkg, items, warn)
    if not version:
        version = "3.0" if any("nav" in i.properties for i in items.values()) else "2.0"
        warn.append("package/@version missing; guessed %s" % version)

    # ---- TOC: nav first, NCX second, spine last
    toc = landmarks = page_list = []
    nav_item = next((i for i in items.values() if "nav" in i.properties and i.entry), None)
    if nav_item:
        toc, landmarks, page_list = parse_nav_doc(zf, index, nav_item.entry, warn)
    if not toc:
        ncx = items.get(toc_idref) if toc_idref else None
        if ncx is None or not ncx.entry:
            ncx = next((i for i in items.values()
                        if i.media_type == "application/x-dtbncx+xml" and i.entry), None)
        if ncx is None:
            hit = next((n for n in index.names if n.lower().endswith(".ncx")), None)
            if hit:
                warn.append("NCX not in manifest; recovered %r" % hit)
                ncx = Item("__ncx", hit, "application/x-dtbncx+xml", frozenset(), hit)
        if ncx and ncx.entry:
            t, pl = parse_ncx(zf, index, ncx.entry, warn)
            toc = toc or t
            page_list = page_list or pl
    if not toc:
        warn.append("no nav document and no usable NCX; TOC built from the spine")
        toc = toc_from_spine(spine, zf)

    cover, csrc = find_cover(pkg, items, index, opf_dir, md, warn)

    global_layout = next((m["value"] for m in md.get("meta", [])
                          if m.get("property") == "rendition:layout"), "reflowable")
    fixed = global_layout == "pre-paginated"
    for si in spine:                    # per-item overrides
        if "rendition:layout-pre-paginated" in si.properties:
            fixed = True

    return Book(path, zf, index, opf_path, opf_dir, version, md, items, spine,
                ppd, toc, landmarks, page_list, cover, csrc, obf, drm, fixed, warn)
