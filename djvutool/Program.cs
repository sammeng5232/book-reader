// djvutool: DjVu document access for Book Reader.
//
// Command line (for tests and debugging):
//   djvutool info   <file>                       JSON: pages, outline
//   djvutool text   <file> <page>                JSON: text layer of one page
//   djvutool render <file> <page> <width> <out> [jpg|png]  image of one page
//   djvutool zigzag                              the IW44 coefficient order, for tests
//   djvutool serve                               request loop on stdin/stdout
//
// Serve protocol: one request per line, UTF-8, tab-separated:
//   open <path> | info | text <page> | alltext | render <page> <width> <jpg|png>
//   | layers <page> [<max planes>] | quit
// Each response is a 4-byte big-endian length followed by the payload: JSON for
// info/text/alltext/open, and for render one format byte ('P' PNG, 'J' JPEG) and the
// image, or 'E' and a UTF-8 message.  layers answers 'L' and the page's layers in a
// binary layout (see Document.Layers), or 'E' and a message.  Errors on JSON requests
// are {"error": "..."}.
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace DjvuTool
{
    // ----------------------------------------------------------------------
    // IFF
    // ----------------------------------------------------------------------
    internal sealed class Chunk
    {
        public string Id;          // "FORM" or a leaf id
        public string Form;        // secondary id for FORM chunks
        public int Offset;         // start of the chunk header
        public int DataOffset;     // start of the payload (after the secondary id for FORMs)
        public int Length;         // payload length
        public List<Chunk> Kids;

        public Chunk Find(string id)
        {
            if (Kids == null) return null;
            foreach (Chunk c in Kids) if (c.Id == id) return c;
            return null;
        }

        public static List<Chunk> Parse(byte[] b, int off, int end)
        {
            List<Chunk> list = new List<Chunk>();
            while (off + 8 <= end)
            {
                string id = Encoding.ASCII.GetString(b, off, 4);
                int size = (b[off + 4] << 24) | (b[off + 5] << 16) | (b[off + 6] << 8) | b[off + 7];
                if (size < 0 || off + 8 + size > end) size = Math.Max(0, end - off - 8);
                Chunk c = new Chunk { Id = id, Offset = off, DataOffset = off + 8, Length = size };
                if (id == "FORM" && size >= 4)
                {
                    c.Form = Encoding.ASCII.GetString(b, off + 8, 4);
                    c.DataOffset = off + 12;
                    c.Length = size - 4;
                    c.Kids = Parse(b, off + 12, off + 8 + size);
                }
                list.Add(c);
                off += 8 + size + (size & 1);
            }
            return list;
        }
    }

    internal sealed class Component
    {
        public string Id, Title;
        public int Kind;           // 0 include, 1 page, 2 thumbnails
        public byte[] Bytes;
        public Chunk Form;
    }

    internal sealed class PageInfo
    {
        public int Width, Height, Dpi = 300, Rotation;   // rotation: 0, 90, 180, 270 (clockwise)
        public bool HasText, HasColor;
    }

    // ----------------------------------------------------------------------
    // Document
    // ----------------------------------------------------------------------
    internal sealed class Document
    {
        readonly string path;
        readonly List<Component> components = new List<Component>();
        public readonly List<Component> Pages = new List<Component>();
        readonly Dictionary<string, Component> byId = new Dictionary<string, Component>(StringComparer.Ordinal);
        readonly Dictionary<string, JB2Dict> dicts = new Dictionary<string, JB2Dict>(StringComparer.Ordinal);
        public string OutlineJson = "[]";
        public bool Truncated;

        public Document(string file)
        {
            path = file;
            byte[] b = File.ReadAllBytes(file);
            if (b.Length < 16 || Encoding.ASCII.GetString(b, 0, 4) != "AT&T")
                throw new InvalidDataException("not a DjVu file");
            List<Chunk> top = Chunk.Parse(b, 4, b.Length);
            if (top.Count == 0 || top[0].Id != "FORM") throw new InvalidDataException("no top-level FORM");
            Chunk root = top[0];
            // a partial download still parses; its top-level FORM promises more than the file holds
            long declared = ((long)b[8] << 24) | ((long)b[9] << 16) | ((long)b[10] << 8) | b[11];
            Truncated = 12 + declared > b.Length;
            if (root.Form == "DJVU" || root.Form == "DJVI")
            {
                Component c = new Component { Id = Path.GetFileName(file), Kind = root.Form == "DJVU" ? 1 : 0, Bytes = b, Form = root };
                Add(c);
            }
            else if (root.Form == "DJVM")
            {
                LoadMultipage(b, root);
            }
            else throw new InvalidDataException("unsupported DjVu form " + root.Form);
            foreach (Component c in components) if (c.Kind == 1) Pages.Add(c);
            if (Pages.Count == 0) throw new InvalidDataException("the document has no pages");
        }

        void Add(Component c)
        {
            components.Add(c);
            if (c.Id != null && !byId.ContainsKey(c.Id)) byId[c.Id] = c;
        }

        void LoadMultipage(byte[] b, Chunk root)
        {
            Chunk dirm = root.Find("DIRM");
            if (dirm == null) throw new InvalidDataException("multipage document without DIRM");
            int p = dirm.DataOffset;
            int flags = b[p];
            bool bundled = (flags & 0x80) != 0;
            int n = (b[p + 1] << 8) | b[p + 2];
            p += 3;
            if (bundled) p += 4 * n;                  // offsets: the FORM children are walked instead
            byte[] dir = Bzz.Decode(b, p, dirm.DataOffset + dirm.Length - p);
            int q = 3 * n;                            // sizes (INT24 each) are not needed
            int[] kinds = new int[n];
            for (int i = 0; i < n; i++) kinds[i] = dir[q + i];
            q += n;
            string[] ids = new string[n], titles = new string[n];
            for (int i = 0; i < n; i++)
            {
                ids[i] = ReadZ(dir, ref q);
                if ((kinds[i] & 0x80) != 0) ReadZ(dir, ref q);                // name
                titles[i] = (kinds[i] & 0x40) != 0 ? ReadZ(dir, ref q) : null;  // title
            }
            List<Chunk> forms = new List<Chunk>();
            foreach (Chunk c in root.Kids) if (c.Id == "FORM") forms.Add(c);
            string dirName = Path.GetDirectoryName(Path.GetFullPath(path));
            for (int i = 0; i < n; i++)
            {
                Component c = new Component { Id = ids[i], Title = titles[i], Kind = kinds[i] & 0x3f };
                if (bundled)
                {
                    if (i >= forms.Count) break;
                    c.Bytes = b;
                    c.Form = forms[i];
                }
                else
                {
                    string f = Path.Combine(dirName, ids[i]);
                    if (File.Exists(f))
                    {
                        byte[] fb = File.ReadAllBytes(f);
                        List<Chunk> t = Chunk.Parse(fb, 4, fb.Length);
                        if (t.Count > 0 && t[0].Id == "FORM") { c.Bytes = fb; c.Form = t[0]; }
                    }
                }
                Add(c);
            }
            Chunk navm = root.Find("NAVM");
            if (navm != null)
            {
                try { OutlineJson = ParseOutline(Bzz.Decode(b, navm.DataOffset, navm.Length)); }
                catch (Exception) { OutlineJson = "[]"; }
            }
        }

        static string ReadZ(byte[] d, ref int q)
        {
            int s = q;
            while (q < d.Length && d[q] != 0) q++;
            string r = Encoding.UTF8.GetString(d, s, q - s);
            q++;
            return r;
        }

        // NAVM: count, then pre-order records {nChildren, INT24 len, title, INT24 len, url}
        string ParseOutline(byte[] d)
        {
            int q = 0;
            if (d.Length < 2) return "[]";
            int count = (d[0] << 8) | d[1];
            q = 2;
            StringBuilder sb = new StringBuilder();
            int remaining = count;
            sb.Append('[');
            bool first = true;
            while (remaining > 0 && q < d.Length)
            {
                if (!first) sb.Append(',');
                first = false;
                OutlineRecord(d, ref q, ref remaining, sb);
            }
            sb.Append(']');
            return sb.ToString();
        }

        void OutlineRecord(byte[] d, ref int q, ref int remaining, StringBuilder sb)
        {
            int kids = d[q++];
            int tl = (d[q] << 16) | (d[q + 1] << 8) | d[q + 2];
            q += 3;
            string title = Encoding.UTF8.GetString(d, q, Math.Min(tl, d.Length - q));
            q += tl;
            int ul = (d[q] << 16) | (d[q + 1] << 8) | d[q + 2];
            q += 3;
            string url = Encoding.UTF8.GetString(d, q, Math.Min(ul, d.Length - q));
            q += ul;
            remaining--;
            sb.Append("{\"t\":").Append(Json.Str(title)).Append(",\"p\":").Append(ResolvePage(url)).Append(",\"c\":[");
            for (int i = 0; i < kids && remaining > 0 && q < d.Length; i++)
            {
                if (i > 0) sb.Append(',');
                OutlineRecord(d, ref q, ref remaining, sb);
            }
            sb.Append("]}");
        }

        // "#12" (page number), "#p0012.djvu" (component id or title) -> 0-based page, or -1
        int ResolvePage(string url)
        {
            if (string.IsNullOrEmpty(url) || url[0] != '#') return -1;
            string key = url.Substring(1);
            for (int i = 0; i < Pages.Count; i++)
                if (Pages[i].Id == key || Pages[i].Title == key) return i;
            int n;
            if (int.TryParse(key, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) && n >= 1 && n <= Pages.Count)
                return n - 1;
            return -1;
        }

        public PageInfo Info(int page)
        {
            Component c = Pages[page];
            PageInfo pi = new PageInfo();
            if (c.Form == null) return pi;
            byte[] b = c.Bytes;
            Chunk info = c.Form.Find("INFO");
            if (info != null && info.Length >= 4)
            {
                int o = info.DataOffset;
                pi.Width = (b[o] << 8) | b[o + 1];
                pi.Height = (b[o + 2] << 8) | b[o + 3];
                if (info.Length >= 8)
                {
                    int dpi = b[o + 6] | (b[o + 7] << 8);          // little-endian
                    if (dpi > 0) pi.Dpi = dpi;
                }
                if (info.Length >= 10)
                {
                    switch (b[o + 9] & 7)
                    {
                        case 6: pi.Rotation = 270; break;   // 90 degrees counter-clockwise
                        case 2: pi.Rotation = 180; break;
                        case 5: pi.Rotation = 90; break;    // 90 degrees clockwise
                        default: pi.Rotation = 0; break;
                    }
                }
            }
            foreach (Chunk k in c.Form.Kids)
            {
                if (k.Id == "TXTz" || k.Id == "TXTa") pi.HasText = true;
                if (k.Id == "BG44" || k.Id == "FG44" || k.Id == "BGjp" || k.Id == "FGjp") pi.HasColor = true;
            }
            return pi;
        }

        JB2Dict Dictionary(string id)
        {
            JB2Dict d;
            if (dicts.TryGetValue(id, out d)) return d;
            Component c;
            if (!byId.TryGetValue(id, out c) || c.Form == null) return null;
            Chunk djbz = c.Form.Find("Djbz");
            if (djbz == null) return null;
            JB2Dict parent = null;
            Chunk incl = c.Form.Find("INCL");
            if (incl != null) parent = Dictionary(Encoding.UTF8.GetString(c.Bytes, incl.DataOffset, incl.Length).Trim('\0', ' '));
            d = JB2Decoder.DecodeDict(c.Bytes, djbz.DataOffset, djbz.Length, parent);
            dicts[id] = d;
            return d;
        }

        // ---- rendering ----------------------------------------------------------------
        // format: "jpg" (24-bit JPEG) or "png" (8-bit grey PNG, or 24-bit when the page has colour);
        // the caller decides, because the EPUB manifest declares each page image's type up front
        public byte[] Render(int page, int maxWidth, string want, out char format)
        {
            PageLayers L = DecodeLayers(page);
            PageInfo pi = L.Info;
            int W = L.W, H = L.H;
            ushort[] mask = L.Mask;
            byte[] palette = L.Palette;
            byte[] bgRgb = L.BgRgb; int bgW = L.BgW, bgH = L.BgH; bool bgBottomUp = L.BgBottomUp;
            byte[] fgRgb = L.FgRgb; int fgW = L.FgW, fgH = L.FgH; bool fgBottomUp = L.FgBottomUp;
            bool color = bgRgb != null || fgRgb != null || PaletteHasColor(palette);
            // ---- compose at the output size ----
            int r0 = Math.Max(1, (W + maxWidth - 1) / Math.Max(1, maxWidth));
            int OW = (W + r0 - 1) / r0, OH = (H + r0 - 1) / r0;
            byte[] outRgb = new byte[OW * OH * 3];      // top-down
            for (int oy = 0; oy < OH; oy++)
            {
                // output row oy (top-down) covers page rows (bottom-up) [yb0, yb1)
                int yt0 = oy * r0, yt1 = Math.Min(H, yt0 + r0);
                int yb0 = H - yt1, yb1 = H - yt0;
                for (int ox = 0; ox < OW; ox++)
                {
                    int x0 = ox * r0, x1 = Math.Min(W, x0 + r0);
                    int total = (x1 - x0) * (yb1 - yb0);
                    int covered = 0, fr = 0, fg = 0, fb = 0;
                    if (mask != null)
                    {
                        for (int y = yb0; y < yb1; y++)
                        {
                            int row = y * W;
                            for (int x = x0; x < x1; x++)
                            {
                                int v = mask[row + x];
                                if (v == 0) continue;
                                covered++;
                                if (palette != null && v - 1 < palette.Length / 3)
                                {
                                    int pi3 = (v - 1) * 3;
                                    fb += palette[pi3]; fg += palette[pi3 + 1]; fr += palette[pi3 + 2];
                                }
                            }
                        }
                    }
                    int cx = (x0 + x1) / 2, cyb = (yb0 + yb1) / 2;     // block centre, bottom-up
                    int br = 255, bgc = 255, bb = 255;
                    if (bgRgb != null) Sample(bgRgb, bgW, bgH, W, H, cx, cyb, bgBottomUp, out br, out bgc, out bb);
                    int or_, og, ob;
                    if (covered == 0) { or_ = br; og = bgc; ob = bb; }
                    else
                    {
                        int cr, cg, cb;
                        if (palette != null)                  // FGbz: each glyph's own colour
                        {
                            cr = fr / covered; cg = fg / covered; cb = fb / covered;
                        }
                        else if (fgRgb != null) Sample(fgRgb, fgW, fgH, W, H, cx, cyb, fgBottomUp, out cr, out cg, out cb);
                        else { cr = cg = cb = 0; }
                        int un = total - covered;
                        or_ = (br * un + cr * covered) / total;
                        og = (bgc * un + cg * covered) / total;
                        ob = (bb * un + cb * covered) / total;
                    }
                    int o = (oy * OW + ox) * 3;
                    outRgb[o] = (byte)or_; outRgb[o + 1] = (byte)og; outRgb[o + 2] = (byte)ob;
                }
            }
            if (mask == null && bgRgb == null && fgRgb == null && L.HasSmmr)
                throw new NotSupportedException("this page uses MMR (fax) compression, which is not supported");
            return Encode(outRgb, OW, OH, color, want == "jpg", pi.Rotation, out format);
        }

        // The decoded layers of one page, as DjVu stores them (shared by Render and Layers).
        internal sealed class PageLayers
        {
            public PageInfo Info;
            public int W, H;
            public ushort[] Mask;          // bottom-up; per pixel 0 = no ink, else 1 + blit colour index
            public byte[] Palette;         // FGbz: B, G, R triples, or null
            public byte[] BgRgb, FgRgb;    // R, G, B
            public int BgW, BgH, FgW, FgH;
            public bool BgBottomUp = true, FgBottomUp = true, HasSmmr;
        }

        PageLayers DecodeLayers(int page)
        {
            Component c = Pages[page];
            PageInfo pi = Info(page);
            int W = pi.Width, H = pi.Height;
            if (W <= 0 || H <= 0) throw new InvalidDataException("page without a size");
            byte[] b = c.Bytes;
            Chunk sjbz = null, fgbz = null, bgjp = null, fgjp = null, smmr = null;
            List<Chunk> bg44 = new List<Chunk>(), fg44 = new List<Chunk>();
            JB2Dict dict = null;
            foreach (Chunk k in c.Form.Kids)
            {
                switch (k.Id)
                {
                    case "Sjbz": sjbz = k; break;
                    case "FGbz": fgbz = k; break;
                    case "BG44": bg44.Add(k); break;
                    case "FG44": fg44.Add(k); break;
                    case "BGjp": bgjp = k; break;
                    case "FGjp": fgjp = k; break;
                    case "Smmr": smmr = k; break;
                    case "INCL":
                        {
                            JB2Dict d = Dictionary(Encoding.UTF8.GetString(b, k.DataOffset, k.Length).Trim('\0', ' '));
                            if (d != null) dict = d;
                            break;
                        }
                }
            }
            // ---- mask (per pixel: 0 = background, else 1 + blit colour index) ----
            ushort[] mask = null;
            int[] blitColor = null;
            byte[] palette = null;        // B, G, R triples
            if (sjbz != null)
            {
                JB2Decoder jb = JB2Decoder.DecodeImage(b, sjbz.DataOffset, sjbz.Length, dict);
                if (fgbz != null) ReadPalette(b, fgbz, out palette, out blitColor);
                mask = new ushort[W * H];
                for (int i = 0; i < jb.Blits.Count; i++)
                {
                    Blit bl = jb.Blits[i];
                    Bitmap1 s = jb.Shape(bl.Shape);
                    ushort v = 1;
                    if (blitColor != null && i < blitColor.Length) v = (ushort)(blitColor[i] + 1);
                    for (int r = 0; r < s.H; r++)
                    {
                        int y = bl.Bottom + r;
                        if (y < 0 || y >= H) continue;
                        int src = s.Row(r), dst = y * W;
                        for (int x0 = 0; x0 < s.W; x0++)
                        {
                            if (s.D[src + x0] == 0) continue;
                            int x = bl.Left + x0;
                            if (x >= 0 && x < W) mask[dst + x] = v;
                        }
                    }
                }
            }
            // ---- colour layers ----
            byte[] bgRgb = null; int bgW = 0, bgH = 0; bool bgBottomUp = true;
            if (bg44.Count > 0)
            {
                IWPixmap pm = new IWPixmap();
                foreach (Chunk k in bg44)
                {
                    try { pm.DecodeChunk(b, k.DataOffset, k.Length); }
                    catch (Exception) { break; }             // keep what decoded so far
                }
                if (pm.Width > 0) { bgRgb = pm.ToRgb(); bgW = pm.Width; bgH = pm.Height; }
            }
            else if (bgjp != null)
            {
                DecodeJpeg(b, bgjp, out bgRgb, out bgW, out bgH);
                bgBottomUp = false;
            }
            byte[] fgRgb = null; int fgW = 0, fgH = 0; bool fgBottomUp = true;
            if (fg44.Count > 0)
            {
                IWPixmap pm = new IWPixmap();
                foreach (Chunk k in fg44)
                {
                    try { pm.DecodeChunk(b, k.DataOffset, k.Length); }
                    catch (Exception) { break; }
                }
                if (pm.Width > 0) { fgRgb = pm.ToRgb(); fgW = pm.Width; fgH = pm.Height; }
            }
            else if (fgjp != null)
            {
                DecodeJpeg(b, fgjp, out fgRgb, out fgW, out fgH);
                fgBottomUp = false;
            }
            PageLayers L = new PageLayers();
            L.Info = pi; L.W = W; L.H = H; L.Mask = mask; L.Palette = palette;
            L.BgRgb = bgRgb; L.BgW = bgW; L.BgH = bgH; L.BgBottomUp = bgBottomUp;
            L.FgRgb = fgRgb; L.FgW = fgW; L.FgH = fgH; L.FgBottomUp = fgBottomUp;
            L.HasSmmr = smmr != null;
            return L;
        }

        // ---- layers: the page's layers for a mixed-raster PDF --------------------------
        // Reply (after the 'L' tag byte; integers big-endian):
        //   u8 version (1); u32 W, u32 H (page pixels); u16 dpi; u16 rotation (0/90/180/270)
        //   u8 flags: 1 = the page has a mask, 2 = too many distinct ink colours (no planes sent)
        //   u16 nplanes, then per plane: u8 R, G, B; u8 source (0 = the solid colour R,G,B,
        //       1 = the foreground image); u32 x, y, w, h: the plane's bounding box in top-down
        //       page pixels (the whole page for source 1); u32 length; packed 1-bit rows of the
        //       box, top-down, MSB first, (w + 7) / 8 bytes per row, bit 1 = no ink, 0 = ink
        //       (usable as a PDF stencil mask as is)
        //   background: u32 w, u32 h, w*h*3 bytes RGB top-down (w = 0: none)
        //   foreground: u32 w, u32 h, RGB top-down (only when a plane uses it, else w = 0)
        // Planes are the mask split by ink colour (FGbz palette entries with the same RGB merged,
        // empty ones dropped).  With more than maxPlanes colours no planes and no layers are sent:
        // the caller composes the page with render instead.
        public byte[] Layers(int page, int maxPlanes)
        {
            PageLayers L = DecodeLayers(page);
            if (L.Mask == null && L.BgRgb == null && L.FgRgb == null && L.HasSmmr)
                throw new NotSupportedException("this page uses MMR (fax) compression, which is not supported");
            int W = L.W, H = L.H;
            // ink value v (1 + blit colour index) -> plane; plane colours as RGB
            List<int> planeColor = new List<int>();          // 0xRRGGBB
            List<byte> planeSource = new List<byte>();
            int[] valuePlane = null;
            bool tooMany = false;
            if (L.Mask != null)
            {
                int maxV = 0;
                bool[] seen = new bool[65536];
                foreach (ushort v in L.Mask) if (v != 0 && !seen[v]) { seen[v] = true; if (v > maxV) maxV = v; }
                valuePlane = new int[maxV + 1];
                Dictionary<int, int> byColor = new Dictionary<int, int>();
                for (int v = 1; v <= maxV; v++)
                {
                    valuePlane[v] = -1;
                    if (!seen[v]) continue;
                    int rgb; byte src;
                    if (L.Palette != null)
                    {
                        int k = v - 1 < L.Palette.Length / 3 ? v - 1 : -1;
                        rgb = k < 0 ? 0 : (L.Palette[3 * k + 2] << 16) | (L.Palette[3 * k + 1] << 8) | L.Palette[3 * k];
                        src = 0;
                    }
                    else if (L.FgRgb != null) { rgb = 0; src = 1; }
                    else { rgb = 0; src = 0; }
                    int key = src == 1 ? -1 : rgb, p;
                    if (!byColor.TryGetValue(key, out p))
                    {
                        p = planeColor.Count;
                        byColor[key] = p;
                        planeColor.Add(rgb);
                        planeSource.Add(src);
                    }
                    valuePlane[v] = p;
                }
                if (planeColor.Count > maxPlanes) tooMany = true;
            }
            MemoryStream ms = new MemoryStream();
            ms.WriteByte((byte)'L');
            ms.WriteByte(1);
            U32(ms, W); U32(ms, H); U16(ms, L.Info.Dpi); U16(ms, L.Info.Rotation);
            ms.WriteByte((byte)((L.Mask != null ? 1 : 0) | (tooMany ? 2 : 0)));
            if (tooMany)
            {
                U16(ms, 0);
                U32(ms, 0); U32(ms, 0); U32(ms, 0); U32(ms, 0);
                return ms.ToArray();
            }
            int n = planeColor.Count;
            // each plane's bounding box (top-down page pixels, exclusive ends); a plane painted
            // with the foreground image covers the whole page, as the image does
            int[] bx0 = new int[n], by0 = new int[n], bx1 = new int[n], by1 = new int[n];
            for (int p = 0; p < n; p++)
            {
                if (planeSource[p] == 1) { bx1[p] = W; by1[p] = H; }
                else { bx0[p] = W; by0[p] = H; }
            }
            for (int yt = 0; yt < H; yt++)
            {
                int src = (H - 1 - yt) * W;
                for (int x = 0; x < W; x++)
                {
                    int v = L.Mask[src + x];
                    if (v == 0) continue;
                    int p = valuePlane[v];
                    if (x < bx0[p]) bx0[p] = x;
                    if (x >= bx1[p]) bx1[p] = x + 1;
                    if (yt < by0[p]) by0[p] = yt;
                    if (yt >= by1[p]) by1[p] = yt + 1;
                }
            }
            byte[][] bits = new byte[n][];
            int[] rowBytes = new int[n];
            for (int p = 0; p < n; p++)
            {
                rowBytes[p] = (bx1[p] - bx0[p] + 7) / 8;
                bits[p] = new byte[rowBytes[p] * (by1[p] - by0[p])];
                for (int i = 0; i < bits[p].Length; i++) bits[p][i] = 0xFF;
            }
            for (int yt = 0; yt < H; yt++)
            {
                int src = (H - 1 - yt) * W;
                for (int x = 0; x < W; x++)
                {
                    int v = L.Mask[src + x];
                    if (v == 0) continue;
                    int p = valuePlane[v], rx = x - bx0[p];
                    bits[p][(yt - by0[p]) * rowBytes[p] + (rx >> 3)] &= (byte)~(0x80 >> (rx & 7));
                }
            }
            U16(ms, n);
            bool usesFg = false;
            for (int p = 0; p < n; p++)
            {
                int rgb = planeColor[p];
                ms.WriteByte((byte)(rgb >> 16)); ms.WriteByte((byte)(rgb >> 8)); ms.WriteByte((byte)rgb);
                ms.WriteByte(planeSource[p]);
                if (planeSource[p] == 1) usesFg = true;
                U32(ms, bx0[p]); U32(ms, by0[p]); U32(ms, bx1[p] - bx0[p]); U32(ms, by1[p] - by0[p]);
                U32(ms, bits[p].Length);
                ms.Write(bits[p], 0, bits[p].Length);
                bits[p] = null;
            }
            WriteLayer(ms, L.BgRgb, L.BgW, L.BgH, L.BgBottomUp);
            if (usesFg) WriteLayer(ms, L.FgRgb, L.FgW, L.FgH, L.FgBottomUp);
            else { U32(ms, 0); U32(ms, 0); }
            return ms.ToArray();
        }

        static void WriteLayer(MemoryStream ms, byte[] rgb, int w, int h, bool bottomUp)
        {
            if (rgb == null) { U32(ms, 0); U32(ms, 0); return; }
            U32(ms, w); U32(ms, h);
            int stride = w * 3;
            for (int yt = 0; yt < h; yt++)
                ms.Write(rgb, (bottomUp ? h - 1 - yt : yt) * stride, stride);
        }

        static void U32(Stream s, int v)
        {
            s.WriteByte((byte)(v >> 24)); s.WriteByte((byte)(v >> 16)); s.WriteByte((byte)(v >> 8)); s.WriteByte((byte)v);
        }

        static void U16(Stream s, int v) { s.WriteByte((byte)(v >> 8)); s.WriteByte((byte)v); }

        static bool PaletteHasColor(byte[] pal)
        {
            if (pal == null) return false;
            for (int i = 0; i + 2 < pal.Length; i += 3)
                if (pal[i] != pal[i + 1] || pal[i + 1] != pal[i + 2]) return true;
            return false;
        }

        // bilinear sample of a reduced layer at page position (px, py_bottom_up)
        static void Sample(byte[] rgb, int lw, int lh, int W, int H, int px, int pyb, bool bottomUp,
                           out int r, out int g, out int b)
        {
            double fx = (px + 0.5) * lw / W - 0.5, fy = (pyb + 0.5) * lh / H - 0.5;
            int x0 = (int)Math.Floor(fx), y0 = (int)Math.Floor(fy);
            double ax = fx - x0, ay = fy - y0;
            double[] acc = new double[3];
            for (int dy = 0; dy <= 1; dy++)
                for (int dx = 0; dx <= 1; dx++)
                {
                    int x = Math.Min(lw - 1, Math.Max(0, x0 + dx));
                    int y = Math.Min(lh - 1, Math.Max(0, y0 + dy));
                    if (!bottomUp) y = lh - 1 - y;
                    double w = (dx == 0 ? 1 - ax : ax) * (dy == 0 ? 1 - ay : ay);
                    int o = (y * lw + x) * 3;
                    acc[0] += w * rgb[o]; acc[1] += w * rgb[o + 1]; acc[2] += w * rgb[o + 2];
                }
            r = (int)(acc[0] + 0.5); g = (int)(acc[1] + 0.5); b = (int)(acc[2] + 0.5);
        }

        static void ReadPalette(byte[] b, Chunk k, out byte[] palette, out int[] blitColor)
        {
            int p = k.DataOffset;
            int version = b[p];
            int n = (b[p + 1] << 8) | b[p + 2];
            p += 3;
            palette = new byte[n * 3];
            Array.Copy(b, p, palette, 0, Math.Min(n * 3, b.Length - p));
            p += n * 3;
            blitColor = null;
            if ((version & 0x80) != 0 && p + 3 <= k.DataOffset + k.Length)
            {
                int count = (b[p] << 16) | (b[p + 1] << 8) | b[p + 2];
                p += 3;
                byte[] idx = Bzz.Decode(b, p, k.DataOffset + k.Length - p);
                blitColor = new int[count];
                for (int i = 0; i < count && 2 * i + 1 < idx.Length; i++)
                {
                    int c = (idx[2 * i] << 8) | idx[2 * i + 1];
                    blitColor[i] = c < n ? c : 0;
                }
            }
        }

        static void DecodeJpeg(byte[] b, Chunk k, out byte[] rgb, out int w, out int h)
        {
            using (MemoryStream ms = new MemoryStream(b, k.DataOffset, k.Length))
            using (System.Drawing.Bitmap bmp = new System.Drawing.Bitmap(ms))
            {
                w = bmp.Width;
                h = bmp.Height;
                rgb = new byte[w * h * 3];
                BitmapData bd = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
                byte[] row = new byte[bd.Stride];
                for (int y = 0; y < h; y++)
                {
                    Marshal.Copy(IntPtr.Add(bd.Scan0, y * bd.Stride), row, 0, bd.Stride);
                    for (int x = 0; x < w; x++)
                    {
                        int o = (y * w + x) * 3;
                        rgb[o] = row[3 * x + 2]; rgb[o + 1] = row[3 * x + 1]; rgb[o + 2] = row[3 * x];
                    }
                }
                bmp.UnlockBits(bd);
            }
        }

        static byte[] Encode(byte[] rgb, int w, int h, bool color, bool jpeg, int rotation, out char format)
        {
            System.Drawing.Bitmap bmp;
            if (color || jpeg)
            {
                bmp = new System.Drawing.Bitmap(w, h, PixelFormat.Format24bppRgb);
                BitmapData bd = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.WriteOnly, PixelFormat.Format24bppRgb);
                byte[] row = new byte[bd.Stride];
                for (int y = 0; y < h; y++)
                {
                    for (int x = 0; x < w; x++)
                    {
                        int o = (y * w + x) * 3;
                        row[3 * x] = rgb[o + 2]; row[3 * x + 1] = rgb[o + 1]; row[3 * x + 2] = rgb[o];
                    }
                    Marshal.Copy(row, 0, IntPtr.Add(bd.Scan0, y * bd.Stride), bd.Stride);
                }
                bmp.UnlockBits(bd);
            }
            else
            {
                bmp = new System.Drawing.Bitmap(w, h, PixelFormat.Format8bppIndexed);
                ColorPalette pal = bmp.Palette;
                for (int i = 0; i < 256; i++) pal.Entries[i] = System.Drawing.Color.FromArgb(i, i, i);
                bmp.Palette = pal;
                BitmapData bd = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.WriteOnly, PixelFormat.Format8bppIndexed);
                byte[] row = new byte[bd.Stride];
                for (int y = 0; y < h; y++)
                {
                    for (int x = 0; x < w; x++) row[x] = rgb[(y * w + x) * 3];
                    Marshal.Copy(row, 0, IntPtr.Add(bd.Scan0, y * bd.Stride), bd.Stride);
                }
                bmp.UnlockBits(bd);
            }
            try
            {
                if (rotation == 90) bmp.RotateFlip(RotateFlipType.Rotate90FlipNone);
                else if (rotation == 180) bmp.RotateFlip(RotateFlipType.Rotate180FlipNone);
                else if (rotation == 270) bmp.RotateFlip(RotateFlipType.Rotate270FlipNone);
                using (MemoryStream ms = new MemoryStream())
                {
                    if (jpeg)
                    {
                        ImageCodecInfo jpegCodec = null;
                        foreach (ImageCodecInfo ci in ImageCodecInfo.GetImageEncoders())
                            if (ci.FormatID == ImageFormat.Jpeg.Guid) jpegCodec = ci;
                        EncoderParameters ep = new EncoderParameters(1);
                        ep.Param[0] = new EncoderParameter(System.Drawing.Imaging.Encoder.Quality, 90L);
                        bmp.Save(ms, jpegCodec, ep);
                        format = 'J';
                    }
                    else
                    {
                        bmp.Save(ms, ImageFormat.Png);
                        format = 'P';
                    }
                    return ms.ToArray();
                }
            }
            finally { bmp.Dispose(); }
        }

        // ---- text layer ---------------------------------------------------------------
        public string TextJson(int page)
        {
            Component c = Pages[page];
            PageInfo pi = Info(page);
            byte[] data = null;
            foreach (Chunk k in c.Form.Kids)
            {
                if (k.Id == "TXTz") { data = Bzz.Decode(c.Bytes, k.DataOffset, k.Length); break; }
                if (k.Id == "TXTa") { data = new byte[k.Length]; Array.Copy(c.Bytes, k.DataOffset, data, 0, k.Length); break; }
            }
            StringBuilder sb = new StringBuilder();
            sb.Append("{\"w\":").Append(pi.Width).Append(",\"h\":").Append(pi.Height).Append(",\"dpi\":").Append(pi.Dpi);
            sb.Append(",\"rot\":").Append(pi.Rotation).Append(",\"lines\":[");
            if (data != null && data.Length >= 3)
            {
                int len = (data[0] << 16) | (data[1] << 8) | data[2];
                len = Math.Min(len, data.Length - 3);
                int q = 3 + len;
                if (q < data.Length) q++;                         // version byte
                List<Zone> roots = new List<Zone>();
                try
                {
                    Zone prev = null;
                    while (q + 17 <= data.Length)
                    {
                        Zone z = Zone.Decode(data, ref q, null, prev, len);
                        roots.Add(z);
                        prev = z;
                    }
                }
                catch (Exception) { }
                List<Zone> lines = new List<Zone>();
                foreach (Zone z in roots) z.CollectLines(lines);
                bool firstLine = true;
                foreach (Zone line in lines)
                {
                    List<Zone> words = new List<Zone>();
                    line.CollectWords(words);
                    if (words.Count == 0) words.Add(line);
                    if (!firstLine) sb.Append(',');
                    firstLine = false;
                    sb.Append('[');
                    AppendRect(sb, line, pi.Height);
                    sb.Append(",[");
                    bool fw = true;
                    foreach (Zone w in words)
                    {
                        string t = Encoding.UTF8.GetString(data, 3 + Math.Max(0, w.TextStart),
                            Math.Max(0, Math.Min(w.TextLength, len - w.TextStart))).Trim();
                        if (t.Length == 0) continue;
                        if (!fw) sb.Append(',');
                        fw = false;
                        sb.Append('[');
                        AppendRect(sb, w, pi.Height);
                        sb.Append(',').Append(Json.Str(t)).Append(']');
                    }
                    sb.Append("]]");
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        // x0, y0, x1, y1 in top-down page pixels
        static void AppendRect(StringBuilder sb, Zone z, int H)
        {
            sb.Append(z.X).Append(',').Append(H - (z.Y + z.H)).Append(',').Append(z.X + z.W).Append(',').Append(H - z.Y);
        }

        public string InfoJson()
        {
            StringBuilder sb = new StringBuilder("{\"pages\":[");
            for (int i = 0; i < Pages.Count; i++)
            {
                PageInfo pi = Info(i);
                if (i > 0) sb.Append(',');
                sb.Append("{\"w\":").Append(pi.Width).Append(",\"h\":").Append(pi.Height)
                  .Append(",\"dpi\":").Append(pi.Dpi).Append(",\"rot\":").Append(pi.Rotation)
                  .Append(",\"text\":").Append(pi.HasText ? "true" : "false")
                  .Append(",\"color\":").Append(pi.HasColor ? "true" : "false")
                  .Append(",\"id\":").Append(Json.Str(Pages[i].Id ?? ""))
                  .Append(",\"title\":").Append(Json.Str(Pages[i].Title ?? "")).Append('}');
            }
            sb.Append("],\"outline\":").Append(OutlineJson)
              .Append(",\"truncated\":").Append(Truncated ? "true" : "false").Append('}');
            return sb.ToString();
        }
    }

    // ---- text zones (bottom-up coordinates; offsets relative, per the reference) --------
    internal sealed class Zone
    {
        public int Type, X, Y, W, H, TextStart, TextLength;
        public List<Zone> Kids = new List<Zone>();

        public static Zone Decode(byte[] d, ref int q, Zone parent, Zone prev, int maxText)
        {
            Zone z = new Zone();
            z.Type = d[q];
            if (z.Type < 1 || z.Type > 7) throw new InvalidDataException("bad text zone");
            int x = U16(d, q + 1) - 0x8000, y = U16(d, q + 3) - 0x8000;
            int w = U16(d, q + 5) - 0x8000, h = U16(d, q + 7) - 0x8000;
            int start = U16(d, q + 9) - 0x8000;       // REF: relative text offset, not "always 0"
            int length = (d[q + 11] << 16) | (d[q + 12] << 8) | d[q + 13];
            int nkids = (d[q + 14] << 16) | (d[q + 15] << 8) | d[q + 16];
            q += 17;
            if (prev != null)
            {
                if (z.Type == 1 || z.Type == 4 || z.Type == 5)     // page, paragraph, line
                {
                    x += prev.X;
                    y = prev.Y - (y + h);
                }
                else                                               // column, word, character
                {
                    x += prev.X + prev.W;
                    y += prev.Y;
                }
                start += prev.TextStart + prev.TextLength;
            }
            else if (parent != null)
            {
                x += parent.X;
                y = parent.Y + parent.H - (y + h);
                start += parent.TextStart;
            }
            z.X = x; z.Y = y; z.W = w; z.H = h; z.TextStart = start; z.TextLength = length;
            Zone prevKid = null;
            for (int i = 0; i < nkids && q + 17 <= d.Length; i++)
            {
                Zone k = Decode(d, ref q, z, prevKid, maxText);
                z.Kids.Add(k);
                prevKid = k;
            }
            return z;
        }

        static int U16(byte[] d, int i) { return (d[i] << 8) | d[i + 1]; }

        public void CollectLines(List<Zone> into)
        {
            if (Type == 5 || (Type >= 5 && Kids.Count == 0)) { into.Add(this); return; }
            if (Kids.Count == 0 && Type < 5) { into.Add(this); return; }
            foreach (Zone k in Kids) k.CollectLines(into);
        }

        public void CollectWords(List<Zone> into)
        {
            if (Type == 6 || (Kids.Count == 0 && Type != 7)) { into.Add(this); return; }
            if (Type == 7) { into.Add(this); return; }
            foreach (Zone k in Kids) k.CollectWords(into);
        }
    }

    internal static class Json
    {
        public static string Str(string s)
        {
            StringBuilder sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            return sb.Append('"').ToString();
        }
    }

    // ----------------------------------------------------------------------
    // entry point
    // ----------------------------------------------------------------------
    internal static class Program
    {
        static int Main(string[] args)
        {
            try
            {
                if (args.Length == 0) { Console.Error.WriteLine("usage: djvutool info|text|render|serve|zigzag ..."); return 2; }
                switch (args[0])
                {
                    case "serve": return Serve();
                    case "zigzag":
                        {
                            StringBuilder sb = new StringBuilder();
                            for (int i = 0; i < 1024; i++) { if (i > 0) sb.Append(','); sb.Append(IWMap.Zigzag[i]); }
                            Console.WriteLine(sb.ToString());
                            return 0;
                        }
                    case "info":
                        WriteUtf8(new Document(args[1]).InfoJson());
                        return 0;
                    case "text":
                        WriteUtf8(new Document(args[1]).TextJson(int.Parse(args[2], CultureInfo.InvariantCulture)));
                        return 0;
                    case "render":
                        {
                            Document doc = new Document(args[1]);
                            char fmt;
                            byte[] img = doc.Render(int.Parse(args[2], CultureInfo.InvariantCulture),
                                                    int.Parse(args[3], CultureInfo.InvariantCulture),
                                                    args.Length > 5 ? args[5] : "jpg", out fmt);
                            File.WriteAllBytes(args[4], img);
                            Console.WriteLine(fmt == 'J' ? "jpeg" : "png");
                            return 0;
                        }
                }
                Console.Error.WriteLine("unknown command " + args[0]);
                return 2;
            }
            catch (Exception e)
            {
                Console.Error.WriteLine(e.GetType().Name + ": " + e.Message);
                return 1;
            }
        }

        static void WriteUtf8(string s)
        {
            byte[] b = Encoding.UTF8.GetBytes(s + "\n");
            using (Stream o = Console.OpenStandardOutput()) o.Write(b, 0, b.Length);
        }

        static int Serve()
        {
            Stream stdout = Console.OpenStandardOutput();
            StreamReader stdin = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
            Document doc = null;
            string line;
            while ((line = stdin.ReadLine()) != null)
            {
                string[] parts = line.Split('\t');
                byte[] reply;
                try
                {
                    switch (parts[0])
                    {
                        case "open":
                            doc = new Document(parts[1]);
                            reply = Encoding.UTF8.GetBytes(doc.InfoJson());
                            break;
                        case "info":
                            reply = Encoding.UTF8.GetBytes(Need(doc).InfoJson());
                            break;
                        case "text":
                            reply = Encoding.UTF8.GetBytes(Need(doc).TextJson(int.Parse(parts[1], CultureInfo.InvariantCulture)));
                            break;
                        case "alltext":
                            {
                                Document d = Need(doc);
                                StringBuilder sb = new StringBuilder("[");
                                for (int i = 0; i < d.Pages.Count; i++)
                                {
                                    if (i > 0) sb.Append(',');
                                    string t;
                                    try { t = d.TextJson(i); } catch (Exception) { t = "{\"lines\":[]}"; }
                                    sb.Append(t);
                                }
                                reply = Encoding.UTF8.GetBytes(sb.Append(']').ToString());
                                break;
                            }
                        case "render":
                            {
                                char fmt;
                                byte[] img = Need(doc).Render(int.Parse(parts[1], CultureInfo.InvariantCulture),
                                                              int.Parse(parts[2], CultureInfo.InvariantCulture),
                                                              parts.Length > 3 ? parts[3] : "jpg", out fmt);
                                reply = new byte[img.Length + 1];
                                reply[0] = (byte)fmt;
                                Array.Copy(img, 0, reply, 1, img.Length);
                                break;
                            }
                        case "layers":
                            reply = Need(doc).Layers(int.Parse(parts[1], CultureInfo.InvariantCulture),
                                                     parts.Length > 2 ? int.Parse(parts[2], CultureInfo.InvariantCulture) : 32);
                            break;
                        case "quit":
                            return 0;
                        default:
                            throw new InvalidOperationException("unknown request " + parts[0]);
                    }
                }
                catch (Exception e)
                {
                    string msg = e.GetType().Name + ": " + e.Message;
                    reply = parts[0] == "render" || parts[0] == "layers"
                        ? Prefix((byte)'E', Encoding.UTF8.GetBytes(msg))
                        : Encoding.UTF8.GetBytes("{\"error\":" + Json.Str(msg) + "}");
                }
                byte[] len = { (byte)(reply.Length >> 24), (byte)(reply.Length >> 16), (byte)(reply.Length >> 8), (byte)reply.Length };
                stdout.Write(len, 0, 4);
                stdout.Write(reply, 0, reply.Length);
                stdout.Flush();
            }
            return 0;
        }

        static Document Need(Document d)
        {
            if (d == null) throw new InvalidOperationException("no document is open");
            return d;
        }

        static byte[] Prefix(byte b, byte[] rest)
        {
            byte[] r = new byte[rest.Length + 1];
            r[0] = b;
            Array.Copy(rest, 0, r, 1, rest.Length);
            return r;
        }
    }
}
