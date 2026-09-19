// DjVu codecs written from the DjVu v3 specification (LizardTech, 2005).
// Where the specification's pseudo-code is ambiguous or differs from the reference
// decoder that produced real-world files, the reference behaviour is followed; each
// such place is marked "REF:".  C# 5 (the compiler that ships with Windows).
using System;
using System.Collections.Generic;
using System.IO;

namespace DjvuTool
{
    // ======================================================================
    // Z'-Coder (spec Appendix 3)
    // ======================================================================
    internal sealed class ZP
    {
        static readonly ushort[] P = new ushort[256];
        static readonly ushort[] M = new ushort[256];
        static readonly byte[] UP = new byte[256];
        static readonly byte[] DN = new byte[256];
        static readonly byte[] FFZT = new byte[256];     // leading one bits of a byte

        static ZP()
        {
            int[] t = ZPTable.Rows;
            for (int i = 0; i < 256; i++)
            {
                P[i] = (ushort)t[4 * i];
                M[i] = (ushort)t[4 * i + 1];
                UP[i] = (byte)t[4 * i + 2];
                DN[i] = (byte)t[4 * i + 3];
            }
            for (int i = 0; i < 256; i++)
            {
                int n = 0;
                for (int j = i; (j & 0x80) != 0; j <<= 1) n++;
                FFZT[i] = (byte)n;
            }
        }

        readonly byte[] buf;
        int pos;
        readonly int end;
        uint a, code, fence, buffer;
        int scount;

        public ZP(byte[] data, int offset, int length)
        {
            buf = data;
            pos = offset;
            end = Math.Min(data.Length, offset + Math.Max(0, length));
            a = 0;
            code = Next() << 8;
            code |= Next();
            scount = 0;
            buffer = 0;
            Preload();
            fence = code >= 0x8000 ? 0x7fffu : code;
        }

        // Past the end of the chunk every further bit is 1 (spec 12.1).
        uint Next() { return pos < end ? buf[pos++] : 0xffu; }

        void Preload()
        {
            while (scount <= 24)
            {
                buffer = (buffer << 8) | Next();
                scount += 8;
            }
        }

        static int Ffz(uint x)
        {
            return x >= 0xff00 ? FFZT[x & 0xff] + 8 : FFZT[(x >> 8) & 0xff];
        }

        public int Decode(ref byte ctx)
        {
            uint z = a + P[ctx];
            if (z <= fence)
            {
                a = z;
                return ctx & 1;
            }
            return DecodeSub(ref ctx, z);
        }

        int DecodeSub(ref byte ctx, uint z)
        {
            int bit = ctx & 1;
            uint d = 0x6000 + ((z + a) >> 2);
            if (z > d) z = d;
            // REF: the spec's Figure 1 takes the MPS branch only when C > Z; the
            // reference decoder takes LPS only when Z > C, i.e. MPS on equality.
            if (z > code)
            {
                z = 0x10000 - z;
                a += z;
                code += z;
                ctx = DN[ctx];
                int shift = Ffz(a);
                scount -= shift;
                a = (a << shift) & 0xffff;
                code = ((code << shift) & 0xffff) | ((buffer >> scount) & ((1u << shift) - 1));
                if (scount < 16) Preload();
                fence = code >= 0x8000 ? 0x7fffu : code;
                return bit ^ 1;
            }
            if (a >= M[ctx]) ctx = UP[ctx];
            scount -= 1;
            a = (z << 1) & 0xffff;
            code = ((code << 1) & 0xffff) | ((buffer >> scount) & 1);
            if (scount < 16) Preload();
            fence = code >= 0x8000 ? 0x7fffu : code;
            return bit;
        }

        int DecodeSimple(uint z)
        {
            if (z > code)
            {
                z = 0x10000 - z;
                a += z;
                code += z;
                int shift = Ffz(a);
                scount -= shift;
                a = (a << shift) & 0xffff;
                code = ((code << shift) & 0xffff) | ((buffer >> scount) & ((1u << shift) - 1));
                if (scount < 16) Preload();
                fence = code >= 0x8000 ? 0x7fffu : code;
                return 1;
            }
            scount -= 1;
            a = (z << 1) & 0xffff;
            code = ((code << 1) & 0xffff) | ((buffer >> scount) & 1);
            if (scount < 16) Preload();
            fence = code >= 0x8000 ? 0x7fffu : code;
            return 0;
        }

        // REF: there are two context-free decoders.  BZZ's raw bits use a/2; the
        // spec's Figure 2 (3a/8) is the one IW44 uses for signs and mantissas.
        public int Passthrough() { return DecodeSimple(0x8000 + (a >> 1)); }
        public int IWPassthrough() { return DecodeSimple(0x8000 + ((a + a + a) >> 3)); }
    }

    // ======================================================================
    // BZZ (spec Appendix 4): Burrows-Wheeler + adaptive MTF over the Z'-Coder
    // ======================================================================
    internal static class Bzz
    {
        public static byte[] Decode(byte[] data, int offset, int length)
        {
            ZP zp = new ZP(data, offset, length);
            byte[] ctx = new byte[300];
            MemoryStream output = new MemoryStream();
            while (true)
            {
                int size = DecodeRaw(zp, 24);
                if (size == 0) break;
                if (size > 4096 * 1024) throw new InvalidDataException("BZZ block too large");
                byte[] block = DecodeBlock(zp, ctx, size);
                output.Write(block, 0, size - 1);
            }
            return output.ToArray();
        }

        static int DecodeRaw(ZP zp, int bits)
        {
            int n = 1, m = 1 << bits;
            while (n < m) n = (n << 1) | zp.Passthrough();
            return n - m;
        }

        static int DecodeBinary(ZP zp, byte[] ctx, int first, int bits)
        {
            int n = 1, m = 1 << bits;
            while (n < m) n = (n << 1) | zp.Decode(ref ctx[first + n - 1]);
            return n - m;
        }

        static byte[] DecodeBlock(ZP zp, byte[] ctx, int size)
        {
            byte[] data = new byte[size];
            int fshift = 0;
            if (zp.Passthrough() != 0)
            {
                fshift = 1;
                if (zp.Passthrough() != 0) fshift = 2;
            }
            byte[] mtf = new byte[256];
            for (int i = 0; i < 256; i++) mtf[i] = (byte)i;
            uint[] freq = new uint[4];
            uint fadd = 4;
            int mtfno = 3, markerpos = -1;
            for (int i = 0; i < size; i++)
            {
                int ctxid = mtfno < 2 ? mtfno : 2;       // contexts 0/1 after MTF 0/1, else 2
                if (zp.Decode(ref ctx[ctxid]) != 0) mtfno = 0;
                else if (zp.Decode(ref ctx[3 + ctxid]) != 0) mtfno = 1;
                else if (zp.Decode(ref ctx[6]) != 0) mtfno = 2 + DecodeBinary(zp, ctx, 7, 1);
                else if (zp.Decode(ref ctx[8]) != 0) mtfno = 4 + DecodeBinary(zp, ctx, 9, 2);
                else if (zp.Decode(ref ctx[12]) != 0) mtfno = 8 + DecodeBinary(zp, ctx, 13, 3);
                else if (zp.Decode(ref ctx[20]) != 0) mtfno = 16 + DecodeBinary(zp, ctx, 21, 4);
                else if (zp.Decode(ref ctx[36]) != 0) mtfno = 32 + DecodeBinary(zp, ctx, 37, 5);
                else if (zp.Decode(ref ctx[68]) != 0) mtfno = 64 + DecodeBinary(zp, ctx, 69, 6);
                else if (zp.Decode(ref ctx[132]) != 0) mtfno = 128 + DecodeBinary(zp, ctx, 133, 7);
                else
                {
                    mtfno = 256;                          // end-of-block marker
                    data[i] = 0;
                    markerpos = i;
                    continue;
                }
                data[i] = mtf[mtfno];
                // adaptive frequency estimate decides where the symbol moves to
                fadd = fadd + (fadd >> fshift);
                if (fadd > 0x10000000)
                {
                    fadd >>= 24;                          // REF: 24, as the pseudo-code says
                    for (int j = 0; j < 4; j++) freq[j] >>= 24;
                }
                uint fc = fadd;
                if (mtfno < 4) fc += freq[mtfno];
                int k = mtfno;
                for (; k >= 4; k--) mtf[k] = mtf[k - 1];
                for (; k > 0 && fc >= freq[k - 1]; k--)
                {
                    mtf[k] = mtf[k - 1];
                    freq[k] = freq[k - 1];             // (the spec's "freq[j]" is a typo)
                }
                mtf[k] = data[i];
                freq[k] = fc;
            }
            if (markerpos < 1 || markerpos >= size) throw new InvalidDataException("BZZ: bad block marker");
            // inverse Burrows-Wheeler transform
            int[] count = new int[256];
            uint[] posn = new uint[size];
            for (int i = 0; i < size; i++)
            {
                if (i == markerpos) continue;
                byte c = data[i];
                posn[i] = ((uint)c << 24) | (uint)(count[c] & 0xffffff);
                count[c]++;
            }
            int last = 1;
            for (int i = 0; i < 256; i++)
            {
                int t = count[i];
                count[i] = last;
                last += t;
            }
            int p = 0;
            last = size - 1;
            while (last > 0)
            {
                uint n = posn[p];
                byte c = (byte)(n >> 24);
                data[--last] = c;
                p = count[c] + (int)(n & 0xffffff);
            }
            if (p != markerpos) throw new InvalidDataException("BZZ: inverse transform failed");
            return data;
        }
    }

    // ======================================================================
    // JB2 (spec Appendix 2): bi-level masks and shared shape dictionaries
    // ======================================================================
    internal sealed class Bitmap1
    {
        public readonly int W, H, Pad, Stride;
        public readonly byte[] D;

        public Bitmap1(int w, int h, int pad)
        {
            W = w;
            H = h;
            Pad = pad;
            Stride = w + 2 * pad;
            D = new byte[Stride * (h + 2 * pad)];
        }

        // row 0 is the BOTTOM row (DjVu bitmaps are stored bottom-up)
        public int Row(int row) { return (row + Pad) * Stride + Pad; }

        public Bitmap1 WithPad(int pad)
        {
            if (pad <= Pad) return this;
            Bitmap1 b = new Bitmap1(W, H, pad);
            for (int r = 0; r < H; r++) Array.Copy(D, Row(r), b.D, b.Row(r), W);
            return b;
        }
    }

    internal struct LibRect { public int Left, Right, Top, Bottom; }

    internal sealed class JB2Dict
    {
        public readonly List<Bitmap1> Shapes = new List<Bitmap1>();
    }

    internal struct Blit { public int Shape, Left, Bottom; }

    internal sealed class JB2Decoder
    {
        const int START_OF_DATA = 0, NEW_MARK = 1, NEW_MARK_LIBRARY_ONLY = 2, NEW_MARK_IMAGE_ONLY = 3,
            MATCHED_REFINE = 4, MATCHED_REFINE_LIBRARY_ONLY = 5, MATCHED_REFINE_IMAGE_ONLY = 6,
            MATCHED_COPY = 7, NON_MARK_DATA = 8, REQUIRED_DICT_OR_RESET = 9, PRESERVED_COMMENT = 10,
            END_OF_DATA = 11;
        const int BIGPOSITIVE = 262142, BIGNEGATIVE = -262143;
        const int PAD = 20;

        readonly ZP zp;
        // numeric decision trees: roots (named contexts) and growable cells
        readonly int[] roots = new int[16];
        const int R_COMMENT_BYTE = 0, R_COMMENT_LENGTH = 1, R_RECORD_TYPE = 2, R_MATCH_INDEX = 3,
            R_ABS_LOC_X = 4, R_ABS_LOC_Y = 5, R_ABS_SIZE_X = 6, R_ABS_SIZE_Y = 7, R_IMAGE_SIZE = 8,
            R_INHERITED = 9, R_REL_LOC_X_CURRENT = 10, R_REL_LOC_X_LAST = 11, R_REL_LOC_Y_CURRENT = 12,
            R_REL_LOC_Y_LAST = 13, R_REL_SIZE_X = 14, R_REL_SIZE_Y = 15;
        byte[] bitcells = new byte[4096];
        int[] leftcell = new int[4096], rightcell = new int[4096];
        int ncell = 1;
        byte distRefinementFlag, offsetTypeDist;
        readonly byte[] bitdist = new byte[1024];
        readonly byte[] cbitdist = new byte[2048];

        bool gotStart;
        int imageColumns, imageRows;
        int lastLeft, lastRight, lastBottom, lastRowLeft, lastRowBottom;
        readonly int[] shortList = new int[3];
        int shortListPos;

        readonly List<Bitmap1> shapes = new List<Bitmap1>();     // shape number -> bitmap
        readonly List<int> lib2shape = new List<int>();
        readonly List<LibRect> libinfo = new List<LibRect>();
        public readonly List<Blit> Blits = new List<Blit>();
        public int Width { get { return imageColumns; } }
        public int Height { get { return imageRows; } }

        readonly JB2Dict inherited;
        readonly bool isDict;
        public readonly JB2Dict Dict = new JB2Dict();

        JB2Decoder(byte[] data, int offset, int length, JB2Dict inheritedDict, bool dict)
        {
            zp = new ZP(data, offset, length);
            inherited = inheritedDict;
            isDict = dict;
        }

        public static JB2Dict DecodeDict(byte[] data, int offset, int length, JB2Dict inheritedDict)
        {
            JB2Decoder d = new JB2Decoder(data, offset, length, inheritedDict, true);
            d.Run();
            return d.Dict;
        }

        public static JB2Decoder DecodeImage(byte[] data, int offset, int length, JB2Dict inheritedDict)
        {
            JB2Decoder d = new JB2Decoder(data, offset, length, inheritedDict, false);
            d.Run();
            return d;
        }

        public Bitmap1 Shape(int n) { return shapes[n]; }

        // ---- numeric coding (spec 11.2.2 / 11.2.5) --------------------------------------
        int NewCell()
        {
            if (ncell >= bitcells.Length)
            {
                int n = bitcells.Length * 2;
                Array.Resize(ref bitcells, n);
                Array.Resize(ref leftcell, n);
                Array.Resize(ref rightcell, n);
            }
            bitcells[ncell] = 0;
            leftcell[ncell] = rightcell[ncell] = 0;
            return ncell++;
        }

        int CodeNum(int low, int high, int root)
        {
            bool negative = false;
            int cutoff = 0, phase = 1, range = -1;
            int kind = 0, idx = root;                // where the current tree pointer lives
            while (range != 1)
            {
                int cell = kind == 0 ? roots[idx] : (kind == 1 ? leftcell[idx] : rightcell[idx]);
                if (cell == 0)
                {
                    cell = NewCell();
                    if (kind == 0) roots[idx] = cell;
                    else if (kind == 1) leftcell[idx] = cell;
                    else rightcell[idx] = cell;
                }
                bool decision = (low >= cutoff) || (high >= cutoff && zp.Decode(ref bitcells[cell]) != 0);
                kind = decision ? 2 : 1;
                idx = cell;
                switch (phase)
                {
                    case 1:
                        negative = !decision;
                        if (negative)
                        {
                            int temp = -low - 1;
                            low = -high - 1;
                            high = temp;
                        }
                        phase = 2;
                        cutoff = 1;
                        break;
                    case 2:
                        if (!decision)
                        {
                            phase = 3;
                            range = (cutoff + 1) / 2;
                            if (range == 1) cutoff = 0;
                            else cutoff -= range / 2;
                        }
                        else cutoff += cutoff + 1;
                        break;
                    case 3:
                        range /= 2;
                        if (range != 1)
                        {
                            if (!decision) cutoff -= range / 2;
                            else cutoff += range / 2;
                        }
                        else if (!decision) cutoff--;
                        break;
                }
            }
            return negative ? -cutoff - 1 : cutoff;
        }

        void ResetNumcoder()
        {
            for (int i = 0; i < roots.Length; i++) roots[i] = 0;
            ncell = 1;
        }

        // ---- records --------------------------------------------------------------------
        void Run()
        {
            int rectype;
            int guard = 0;
            do
            {
                rectype = CodeNum(START_OF_DATA, END_OF_DATA, R_RECORD_TYPE);
                Record(rectype);
                if (++guard > 50000000) throw new InvalidDataException("JB2: runaway stream");
            } while (rectype != END_OF_DATA);
        }

        void InitLibrary()
        {
            if (inherited == null) return;
            foreach (Bitmap1 s in inherited.Shapes)
            {
                // a dictionary that inherits passes the parent's shapes on, first
                if (isDict) Dict.Shapes.Add(s);
                shapes.Add(s);
                lib2shape.Add(shapes.Count - 1);
                libinfo.Add(BoundingBox(s));
            }
        }

        void AddLibrary(int shapeno)
        {
            lib2shape.Add(shapeno);
            libinfo.Add(BoundingBox(shapes[shapeno]));
        }

        static LibRect BoundingBox(Bitmap1 bm)
        {
            LibRect r = new LibRect();
            int w = bm.W, h = bm.H;
            byte[] d = bm.D;
            for (r.Right = w - 1; r.Right >= 0; r.Right--)
            {
                bool any = false;
                for (int y = 0; y < h && !any; y++) any = d[bm.Row(y) + r.Right] != 0;
                if (any) break;
            }
            for (r.Top = h - 1; r.Top >= 0; r.Top--)
            {
                bool any = false;
                int row = bm.Row(r.Top);
                for (int x = 0; x < w && !any; x++) any = d[row + x] != 0;
                if (any) break;
            }
            for (r.Left = 0; r.Left <= r.Right; r.Left++)
            {
                bool any = false;
                for (int y = 0; y < h && !any; y++) any = d[bm.Row(y) + r.Left] != 0;
                if (any) break;
            }
            for (r.Bottom = 0; r.Bottom <= r.Top; r.Bottom++)
            {
                bool any = false;
                int row = bm.Row(r.Bottom);
                for (int x = 0; x < w && !any; x++) any = d[row + x] != 0;
                if (any) break;
            }
            return r;
        }

        void Record(int rectype)
        {
            Bitmap1 bm = null;
            int shapeno = -1;
            Blit blit = new Blit();
            bool hasBlit = false;
            switch (rectype)
            {
                case START_OF_DATA:
                    {
                        int w = CodeNum(0, BIGPOSITIVE, R_IMAGE_SIZE);
                        int h = CodeNum(0, BIGPOSITIVE, R_IMAGE_SIZE);
                        zp.Decode(ref distRefinementFlag);     // eventual lossless-refinement flag
                        if (isDict)
                        {
                            if (w != 0 || h != 0) throw new InvalidDataException("JB2: dictionary with a size");
                            // REF: dictionaries start their location state at zero
                            lastLeft = 1; lastRowLeft = 0; lastRowBottom = 0; lastRight = 0;
                        }
                        else
                        {
                            if (w == 0 || h == 0) throw new InvalidDataException("JB2: zero image size");
                            imageColumns = w;
                            imageRows = h;
                            lastLeft = 1 + imageColumns; lastRowLeft = 0; lastRowBottom = imageRows; lastRight = 0;
                        }
                        FillShortList(lastRowBottom);
                        gotStart = true;
                        InitLibrary();
                        break;
                    }
                case NEW_MARK:
                    bm = AbsoluteSize();
                    DecodeDirect(bm);
                    blit = RelativeLocation(bm.H, bm.W);
                    hasBlit = true;
                    break;
                case NEW_MARK_LIBRARY_ONLY:
                    bm = AbsoluteSize();
                    DecodeDirect(bm);
                    break;
                case NEW_MARK_IMAGE_ONLY:
                    bm = AbsoluteSize();
                    DecodeDirect(bm);
                    blit = RelativeLocation(bm.H, bm.W);
                    hasBlit = true;
                    break;
                case MATCHED_REFINE:
                case MATCHED_REFINE_IMAGE_ONLY:
                    {
                        int match = CodeNum(0, lib2shape.Count - 1, R_MATCH_INDEX);
                        Bitmap1 cbm = shapes[lib2shape[match]];
                        LibRect l = libinfo[match];
                        bm = RelativeSize(l.Right - l.Left + 1, l.Top - l.Bottom + 1);
                        DecodeCross(bm, cbm, l);
                        blit = RelativeLocation(bm.H, bm.W);
                        hasBlit = true;
                        break;
                    }
                case MATCHED_REFINE_LIBRARY_ONLY:
                    {
                        int match = CodeNum(0, lib2shape.Count - 1, R_MATCH_INDEX);
                        Bitmap1 cbm = shapes[lib2shape[match]];
                        LibRect l = libinfo[match];
                        bm = RelativeSize(l.Right - l.Left + 1, l.Top - l.Bottom + 1);
                        // REF: the reference page decoder codes no bitmap for this record type
                        // (only dictionaries do); mirror it to stay in sync with real files.
                        if (isDict) DecodeCross(bm, cbm, l);
                        break;
                    }
                case MATCHED_COPY:
                    {
                        int match = CodeNum(0, lib2shape.Count - 1, R_MATCH_INDEX);
                        int shape = lib2shape[match];
                        LibRect l = libinfo[match];
                        Blit b = RelativeLocation(l.Top - l.Bottom + 1, l.Right - l.Left + 1);
                        b.Left -= l.Left;
                        b.Bottom -= l.Bottom;
                        b.Shape = shape;
                        Blits.Add(b);
                        break;
                    }
                case NON_MARK_DATA:
                    {
                        bm = AbsoluteSize();
                        DecodeDirect(bm);
                        int left = CodeNum(1, imageColumns, R_ABS_LOC_X);
                        int top = CodeNum(1, imageRows, R_ABS_LOC_Y);
                        blit.Bottom = top - bm.H;
                        blit.Left = left - 1;
                        hasBlit = true;
                        break;
                    }
                case PRESERVED_COMMENT:
                    {
                        int size = CodeNum(0, BIGPOSITIVE, R_COMMENT_LENGTH);
                        for (int i = 0; i < size; i++) CodeNum(0, 255, R_COMMENT_BYTE);
                        break;
                    }
                case REQUIRED_DICT_OR_RESET:
                    if (!gotStart)
                    {
                        int count = CodeNum(0, BIGPOSITIVE, R_INHERITED);
                        int have = inherited == null ? 0 : inherited.Shapes.Count;
                        if (count != have)
                            throw new InvalidDataException("JB2: shared dictionary has " + have + " shapes, page needs " + count);
                    }
                    else ResetNumcoder();
                    break;
                case END_OF_DATA:
                    break;
                default:
                    throw new InvalidDataException("JB2: unknown record type " + rectype);
            }
            // post-record bookkeeping, in the reference decoder's order
            if (bm != null)
            {
                shapes.Add(bm);
                shapeno = shapes.Count - 1;
                if (isDict) Dict.Shapes.Add(bm);
            }
            if (rectype == NEW_MARK || rectype == NEW_MARK_LIBRARY_ONLY || rectype == MATCHED_REFINE ||
                rectype == MATCHED_REFINE_LIBRARY_ONLY)
                AddLibrary(shapeno);
            if (hasBlit)
            {
                blit.Shape = shapeno;
                Blits.Add(blit);
            }
        }

        Bitmap1 AbsoluteSize()
        {
            int w = CodeNum(0, BIGPOSITIVE, R_ABS_SIZE_X);
            int h = CodeNum(0, BIGPOSITIVE, R_ABS_SIZE_Y);
            if (w > 65535 || h > 65535) throw new InvalidDataException("JB2: shape too large");
            return new Bitmap1(w, h, PAD);
        }

        Bitmap1 RelativeSize(int cw, int ch)
        {
            int w = cw + CodeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_SIZE_X);
            int h = ch + CodeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_SIZE_Y);
            if (w < 0 || h < 0 || w > 65535 || h > 65535) throw new InvalidDataException("JB2: bad refined size");
            return new Bitmap1(w, h, PAD);
        }

        void FillShortList(int v)
        {
            shortList[0] = shortList[1] = shortList[2] = v;
            shortListPos = 0;
        }

        int UpdateShortList(int v)
        {
            if (++shortListPos == 3) shortListPos = 0;
            int[] s = shortList;
            s[shortListPos] = v;
            return (s[0] >= s[1])
                ? ((s[0] > s[2]) ? ((s[1] >= s[2]) ? s[1] : s[2]) : s[0])
                : ((s[0] < s[2]) ? ((s[1] >= s[2]) ? s[2] : s[1]) : s[0]);
        }

        Blit RelativeLocation(int rows, int columns)
        {
            if (!gotStart) throw new InvalidDataException("JB2: location before start of data");
            int left, bottom, top, right;
            bool newRow = zp.Decode(ref offsetTypeDist) != 0;
            if (newRow)
            {
                int xdiff = CodeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_X_LAST);
                int ydiff = CodeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_Y_LAST);
                left = lastRowLeft + xdiff;
                top = lastRowBottom + ydiff;
                right = left + columns - 1;
                bottom = top - rows + 1;
                lastLeft = lastRowLeft = left;
                lastRight = right;
                lastBottom = lastRowBottom = bottom;
                FillShortList(bottom);
            }
            else
            {
                int xdiff = CodeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_X_CURRENT);
                int ydiff = CodeNum(BIGNEGATIVE, BIGPOSITIVE, R_REL_LOC_Y_CURRENT);
                left = lastRight + xdiff;
                bottom = lastBottom + ydiff;
                right = left + columns - 1;
                top = bottom + rows - 1;
                lastLeft = left;
                lastRight = right;
                lastBottom = UpdateShortList(bottom);
            }
            Blit b = new Blit();
            b.Bottom = bottom - 1;
            b.Left = left - 1;
            return b;
        }

        // direct coding: 10-pixel template (2 rows above + 2 pixels to the left)
        void DecodeDirect(Bitmap1 bm)
        {
            int dw = bm.W;
            int dy = bm.H - 1;
            byte[] d = bm.D;
            int up2 = bm.Row(dy + 2), up1 = bm.Row(dy + 1), up0 = bm.Row(dy);
            while (dy >= 0)
            {
                int ctx = (d[up2 - 1] << 9) | (d[up2] << 8) | (d[up2 + 1] << 7)
                        | (d[up1 - 2] << 6) | (d[up1 - 1] << 5) | (d[up1] << 4) | (d[up1 + 1] << 3) | (d[up1 + 2] << 2)
                        | (d[up0 - 2] << 1) | d[up0 - 1];
                for (int dx = 0; dx < dw; )
                {
                    int n = zp.Decode(ref bitdist[ctx]);
                    d[up0 + dx] = (byte)n;
                    dx++;
                    ctx = ((ctx << 1) & 0x37a) | (d[up1 + dx + 2] << 2) | (d[up2 + dx + 1] << 7) | n;
                }
                dy--;
                up2 = up1;
                up1 = up0;
                up0 = bm.Row(dy);
            }
        }

        // refinement coding: 11-pixel template over the new bitmap and the aligned match
        void DecodeCross(Bitmap1 bm, Bitmap1 cbm, LibRect l)
        {
            int cw = cbm.W, dw = bm.W, dh = bm.H;
            int xd2c = (dw / 2 - dw + 1) - ((l.Right - l.Left + 1) / 2 - l.Right);
            int yd2c = (dh / 2 - dh + 1) - ((l.Top - l.Bottom + 1) / 2 - l.Top);
            if (xd2c < -15 || xd2c > 15 || yd2c < -15 || yd2c > 15) throw new InvalidDataException("JB2: bad refinement offset");
            // the match is read around the new bitmap's extent: make sure it has border enough
            int need = Math.Max(Math.Max(2 - xd2c, 2 + dw + xd2c - cw),
                                Math.Max(2 - yd2c + 2, 2 + dh + yd2c - cbm.H + 2)) + 2;
            cbm = cbm.WithPad(Math.Max(need, PAD));
            byte[] d = bm.D, c = cbm.D;
            int dy = dh - 1;
            int cy = dy + yd2c;
            int up1 = bm.Row(dy + 1), up0 = bm.Row(dy);
            int xup1 = cbm.Row(cy + 1) + xd2c, xup0 = cbm.Row(cy) + xd2c, xdn1 = cbm.Row(cy - 1) + xd2c;
            while (dy >= 0)
            {
                int ctx = (d[up1 - 1] << 10) | (d[up1] << 9) | (d[up1 + 1] << 8) | (d[up0 - 1] << 7)
                        | (c[xup1] << 6) | (c[xup0 - 1] << 5) | (c[xup0] << 4) | (c[xup0 + 1] << 3)
                        | (c[xdn1 - 1] << 2) | (c[xdn1] << 1) | c[xdn1 + 1];
                for (int dx = 0; dx < dw; )
                {
                    int n = zp.Decode(ref cbitdist[ctx]);
                    d[up0 + dx] = (byte)n;
                    dx++;
                    ctx = ((ctx << 1) & 0x636) | (d[up1 + dx + 1] << 8) | (c[xup1 + dx] << 6)
                        | (c[xup0 + dx + 1] << 3) | c[xdn1 + dx + 1] | (n << 7);
                }
                up1 = up0;
                up0 = bm.Row(--dy);
                xup1 = xup0;
                xup0 = xdn1;
                xdn1 = cbm.Row((--cy) - 1) + xd2c;
            }
        }
    }

    // ======================================================================
    // IW44 (spec Appendix 1): progressive wavelet colour images
    // ======================================================================
    internal sealed class IWMap
    {
        public readonly int IW, IH, BW, BH, NB;
        public readonly short[][][] Blocks;     // [block][bucket 0..63] -> 16 coefficients (lazy)

        public IWMap(int w, int h)
        {
            IW = w; IH = h;
            BW = (w + 31) & ~31;
            BH = (h + 31) & ~31;
            NB = (BW * BH) / 1024;
            Blocks = new short[NB][][];
            for (int i = 0; i < NB; i++) Blocks[i] = new short[64][];
        }

        // coefficient n (0..1023) of a block -> position in its 32x32 tile: the index
        // bits alternate column/row from the most significant position down
        public static readonly int[] Zigzag = BuildZigzag();

        static int[] BuildZigzag()
        {
            int[] z = new int[1024];
            for (int n = 0; n < 1024; n++)
            {
                int col = 0, row = 0;
                for (int b = 0; b < 5; b++)
                {
                    col |= ((n >> (2 * b)) & 1) << (4 - b);
                    row |= ((n >> (2 * b + 1)) & 1) << (4 - b);
                }
                z[n] = row * 32 + col;
            }
            return z;
        }

        // reconstruct signed 8-bit samples (row 0 = bottom, as encoded)
        public sbyte[] Image(bool fast)
        {
            short[] data = new short[BW * BH];
            short[] lift = new short[1024];
            int blockno = 0;
            for (int i = 0; i < BH; i += 32)
            {
                for (int j = 0; j < BW; j += 32)
                {
                    Array.Clear(lift, 0, 1024);
                    short[][] blk = Blocks[blockno++];
                    for (int n1 = 0; n1 < 64; n1++)
                    {
                        short[] bucket = blk[n1];
                        if (bucket == null) continue;
                        for (int n2 = 0; n2 < 16; n2++) lift[Zigzag[n1 * 16 + n2]] = bucket[n2];
                    }
                    for (int ii = 0; ii < 32; ii++) Array.Copy(lift, ii * 32, data, (i + ii) * BW + j, 32);
                }
            }
            if (fast)
            {
                Backward(data, IW, IH, BW, 32, 2);
                for (int i = 0; i < BH; i += 2)
                    for (int jj = 0; jj < BW; jj += 2)
                    {
                        int p = i * BW + jj;
                        short v = data[p];
                        data[p + 1] = v;
                        if (i + 1 < BH) { data[p + BW] = v; data[p + BW + 1] = v; }
                    }
            }
            else Backward(data, IW, IH, BW, 32, 1);
            sbyte[] img = new sbyte[IW * IH];
            for (int i = 0; i < IH; i++)
                for (int j = 0; j < IW; j++)
                {
                    int x = (data[i * BW + j] + 32) >> 6;
                    if (x < -128) x = -128; else if (x > 127) x = 127;
                    img[i * IW + j] = (sbyte)x;
                }
            return img;
        }

        static void Backward(short[] d, int w, int h, int rowsize, int begin, int end)
        {
            for (int scale = begin >> 1; scale >= end; scale >>= 1)
            {
                FilterBV(d, 0, w, h, rowsize, scale);
                FilterBH(d, 0, w, h, rowsize, scale);
            }
        }

        static void FilterBV(short[] d, int p, int w, int h, int rowsize, int scale)
        {
            int y = 0;
            int s = scale * rowsize;
            int s3 = s + s + s;
            h = ((h - 1) / scale) + 1;
            while (y - 3 < h)
            {
                {
                    int q = p, e = q + w;
                    if (y >= 3 && y + 3 < h)
                    {
                        for (; q < e; q += scale)
                        {
                            int a = d[q - s] + d[q + s], b = d[q - s3] + d[q + s3];
                            d[q] = (short)(d[q] - (((a << 3) + a - b + 16) >> 5));
                        }
                    }
                    else if (y < h)
                    {
                        int q1 = (y + 1 < h) ? q + s : -1;
                        int q3 = (y + 3 < h) ? q + s3 : -1;
                        for (; q < e; q += scale)
                        {
                            int a = (y >= 1 ? d[q - s] : 0) + (q1 >= 0 ? d[q1] : 0);
                            int b = (y >= 3 ? d[q - s3] : 0) + (q3 >= 0 ? d[q3] : 0);
                            d[q] = (short)(d[q] - (((a << 3) + a - b + 16) >> 5));
                            if (q1 >= 0) q1 += scale;
                            if (q3 >= 0) q3 += scale;
                        }
                    }
                }
                {
                    int q = p - s3, e = q + w;
                    if (y >= 6 && y < h)
                    {
                        for (; q < e; q += scale)
                        {
                            int a = d[q - s] + d[q + s], b = d[q - s3] + d[q + s3];
                            d[q] = (short)(d[q] + (((a << 3) + a - b + 8) >> 4));
                        }
                    }
                    else if (y >= 3)
                    {
                        int q1 = (y - 2 < h) ? q + s : q - s;
                        for (; q < e; q += scale, q1 += scale)
                        {
                            int a = d[q - s] + d[q1];
                            d[q] = (short)(d[q] + ((a + 1) >> 1));
                        }
                    }
                }
                y += 2;
                p += s + s;
            }
        }

        static void FilterBH(short[] d, int p, int w, int h, int rowsize, int scale)
        {
            int y = 0;
            int s = scale, s3 = s + s + s;
            rowsize *= scale;
            while (y < h)
            {
                int q = p, e = p + w;
                int a0 = 0, a1 = 0, a2 = 0, a3 = 0, b0 = 0, b1 = 0, b2 = 0, b3 = 0;
                if (q < e)
                {
                    if (q + s < e) a2 = d[q + s];
                    if (q + s3 < e) a3 = d[q + s3];
                    b2 = b3 = d[q] - ((((a1 + a2) << 3) + (a1 + a2) - a0 - a3 + 16) >> 5);
                    d[q] = (short)b3;
                    q += s + s;
                }
                if (q < e)
                {
                    a0 = a1; a1 = a2; a2 = a3;
                    if (q + s3 < e) a3 = d[q + s3];
                    b3 = d[q] - ((((a1 + a2) << 3) + (a1 + a2) - a0 - a3 + 16) >> 5);
                    d[q] = (short)b3;
                    q += s + s;
                }
                if (q < e)
                {
                    b1 = b2; b2 = b3;
                    a0 = a1; a1 = a2; a2 = a3;
                    if (q + s3 < e) a3 = d[q + s3];
                    b3 = d[q] - ((((a1 + a2) << 3) + (a1 + a2) - a0 - a3 + 16) >> 5);
                    d[q] = (short)b3;
                    d[q - s3] = (short)(d[q - s3] + ((b1 + b2 + 1) >> 1));
                    q += s + s;
                }
                while (q + s3 < e)
                {
                    a0 = a1; a1 = a2; a2 = a3;
                    a3 = d[q + s3];
                    b0 = b1; b1 = b2; b2 = b3;
                    b3 = d[q] - ((((a1 + a2) << 3) + (a1 + a2) - a0 - a3 + 16) >> 5);
                    d[q] = (short)b3;
                    d[q - s3] = (short)(d[q - s3] + ((((b1 + b2) << 3) + (b1 + b2) - b0 - b3 + 8) >> 4));
                    q += s + s;
                }
                while (q < e)
                {
                    a0 = a1; a1 = a2; a2 = a3; a3 = 0;
                    b0 = b1; b1 = b2; b2 = b3;
                    b3 = d[q] - ((((a1 + a2) << 3) + (a1 + a2) - a0 - a3 + 16) >> 5);
                    d[q] = (short)b3;
                    d[q - s3] = (short)(d[q - s3] + ((((b1 + b2) << 3) + (b1 + b2) - b0 - b3 + 8) >> 4));
                    q += s + s;
                }
                while (q - s3 < e)
                {
                    b0 = b1; b1 = b2; b2 = b3;
                    if (q - s3 >= p) d[q - s3] = (short)(d[q - s3] + ((b1 + b2 + 1) >> 1));
                    q += s + s;
                }
                y += scale;
                p += rowsize;
            }
        }
    }

    internal sealed class IWCodec
    {
        const int ZERO = 1, ACTIVE = 2, NEW = 4, UNK = 8;
        static readonly int[] IW_QUANT = {
            0x004000, 0x008000, 0x008000, 0x010000, 0x010000, 0x010000, 0x020000, 0x020000,
            0x020000, 0x040000, 0x040000, 0x040000, 0x080000, 0x040000, 0x040000, 0x080000 };
        static readonly int[] BAND_START = { 0, 1, 2, 3, 4, 8, 12, 16, 32, 48 };
        static readonly int[] BAND_SIZE = { 1, 1, 1, 1, 4, 4, 4, 16, 16, 16 };

        readonly IWMap map;
        readonly int[] quantLo = new int[16];
        readonly int[] quantHi = new int[10];
        readonly byte[] coeffstate = new byte[256];
        readonly byte[] bucketstate = new byte[16];
        readonly byte[] ctxStart = new byte[32];
        readonly byte[,] ctxBucket = new byte[10, 8];
        byte ctxMant, ctxRoot;
        int curband, curbit = 1;

        public IWCodec(IWMap m)
        {
            map = m;
            int i = 0, q = 0;
            for (; i < 4; i++) quantLo[i] = IW_QUANT[q++];
            for (int j = 0; j < 4; j++) quantLo[i++] = IW_QUANT[q];
            q++;
            for (int j = 0; j < 4; j++) quantLo[i++] = IW_QUANT[q];
            q++;
            for (int j = 0; j < 4; j++) quantLo[i++] = IW_QUANT[q];
            q++;
            quantHi[0] = 0;
            for (int j = 1; j < 10; j++) quantHi[j] = IW_QUANT[q++];
        }

        bool IsNullSlice(int band)
        {
            if (band == 0)
            {
                bool isNull = true;
                for (int i = 0; i < 16; i++)
                {
                    int threshold = quantLo[i];
                    coeffstate[i] = ZERO;
                    if (threshold > 0 && threshold < 0x8000)
                    {
                        coeffstate[i] = UNK;
                        isNull = false;
                    }
                }
                return isNull;
            }
            int t = quantHi[band];
            return !(t > 0 && t < 0x8000);
        }

        public int CodeSlice(ZP zp)
        {
            if (curbit < 0) return 0;
            if (!IsNullSlice(curband))
                for (int b = 0; b < map.NB; b++)
                    DecodeBuckets(zp, curband, map.Blocks[b], BAND_START[curband], BAND_SIZE[curband]);
            quantHi[curband] >>= 1;
            if (curband == 0)
                for (int i = 0; i < 16; i++) quantLo[i] >>= 1;
            if (++curband >= 10)
            {
                curband = 0;
                curbit++;
                if (quantHi[9] == 0)
                {
                    curbit = -1;
                    return 0;
                }
            }
            return 1;
        }

        int DecodePrepare(int fbucket, int nbucket, short[][] blk)
        {
            int bbstate = 0;
            if (fbucket != 0)
            {
                for (int buckno = 0; buckno < nbucket; buckno++)
                {
                    int bstate = 0;
                    short[] pc = blk[fbucket + buckno];
                    int cs = buckno * 16;
                    if (pc == null) bstate = UNK;
                    else
                        for (int i = 0; i < 16; i++)
                        {
                            int c = pc[i] != 0 ? ACTIVE : UNK;
                            coeffstate[cs + i] = (byte)c;
                            bstate |= c;
                        }
                    bucketstate[buckno] = (byte)bstate;
                    bbstate |= bstate;
                }
            }
            else
            {
                short[] pc = blk[0];
                if (pc == null) bbstate = UNK;
                else
                    for (int i = 0; i < 16; i++)
                    {
                        int c = coeffstate[i];
                        if (c != ZERO) c = pc[i] != 0 ? ACTIVE : UNK;
                        coeffstate[i] = (byte)c;
                        bbstate |= c;
                    }
                bucketstate[0] = (byte)bbstate;
            }
            return bbstate;
        }

        void DecodeBuckets(ZP zp, int band, short[][] blk, int fbucket, int nbucket)
        {
            int bbstate = DecodePrepare(fbucket, nbucket, blk);
            if (nbucket < 16 || (bbstate & ACTIVE) != 0) bbstate |= NEW;
            else if ((bbstate & UNK) != 0)
            {
                if (zp.Decode(ref ctxRoot) != 0) bbstate |= NEW;
            }
            if ((bbstate & NEW) != 0)
                for (int buckno = 0; buckno < nbucket; buckno++)
                {
                    if ((bucketstate[buckno] & UNK) == 0) continue;
                    int ctx = 0;
                    if (band > 0)
                    {
                        int k = (fbucket + buckno) << 2;
                        short[] b = blk[k >> 4];
                        if (b != null)
                        {
                            k &= 0xf;
                            if (b[k] != 0) ctx++;
                            if (b[k + 1] != 0) ctx++;
                            if (b[k + 2] != 0) ctx++;
                            if (ctx < 3 && b[k + 3] != 0) ctx++;
                        }
                    }
                    if ((bbstate & ACTIVE) != 0) ctx |= 4;
                    if (zp.Decode(ref ctxBucket[band, ctx]) != 0) bucketstate[buckno] |= NEW;
                }
            if ((bbstate & NEW) != 0)
            {
                int thres = quantHi[band];
                for (int buckno = 0; buckno < nbucket; buckno++)
                {
                    if ((bucketstate[buckno] & NEW) == 0) continue;
                    int cs = buckno * 16;
                    short[] pc = blk[fbucket + buckno];
                    if (pc == null)
                    {
                        pc = blk[fbucket + buckno] = new short[16];
                        if (fbucket == 0)
                        {
                            for (int i = 0; i < 16; i++) if (coeffstate[cs + i] != ZERO) coeffstate[cs + i] = UNK;
                        }
                        else for (int i = 0; i < 16; i++) coeffstate[cs + i] = UNK;
                    }
                    int gotcha = 0;
                    const int maxgotcha = 7;
                    for (int i = 0; i < 16; i++) if ((coeffstate[cs + i] & UNK) != 0) gotcha++;
                    for (int i = 0; i < 16; i++)
                    {
                        if ((coeffstate[cs + i] & UNK) == 0) continue;
                        if (band == 0) thres = quantLo[i];
                        int ctx = gotcha >= maxgotcha ? maxgotcha : gotcha;
                        if ((bucketstate[buckno] & ACTIVE) != 0) ctx |= 8;
                        if (zp.Decode(ref ctxStart[ctx]) != 0)
                        {
                            coeffstate[cs + i] |= NEW;
                            int half = thres >> 1;
                            int coeff = thres + half - (half >> 2);
                            pc[i] = (short)(zp.IWPassthrough() != 0 ? -coeff : coeff);
                        }
                        if ((coeffstate[cs + i] & NEW) != 0) gotcha = 0;
                        else if (gotcha > 0) gotcha--;
                    }
                }
            }
            if ((bbstate & ACTIVE) != 0)
            {
                int thres = quantHi[band];
                for (int buckno = 0; buckno < nbucket; buckno++)
                {
                    if ((bucketstate[buckno] & ACTIVE) == 0) continue;
                    int cs = buckno * 16;
                    short[] pc = blk[fbucket + buckno];
                    for (int i = 0; i < 16; i++)
                    {
                        if ((coeffstate[cs + i] & ACTIVE) == 0) continue;
                        int coeff = pc[i];
                        if (coeff < 0) coeff = -coeff;
                        if (band == 0) thres = quantLo[i];
                        if (coeff <= 3 * thres)
                        {
                            coeff += thres >> 2;
                            if (zp.Decode(ref ctxMant) != 0) coeff += thres >> 1;
                            else coeff = coeff - thres + (thres >> 1);
                        }
                        else
                        {
                            if (zp.IWPassthrough() != 0) coeff += thres >> 1;
                            else coeff = coeff - thres + (thres >> 1);
                        }
                        pc[i] = (short)(pc[i] > 0 ? coeff : -coeff);
                    }
                }
            }
        }
    }

    // A progressive IW44 colour (or grey) image built from successive chunks.
    internal sealed class IWPixmap
    {
        IWMap ymap, cbmap, crmap;
        IWCodec ycodec, cbcodec, crcodec;
        int cslice, cserial, crcbDelay, crcbHalf;

        public int Width { get { return ymap == null ? 0 : ymap.IW; } }
        public int Height { get { return ymap == null ? 0 : ymap.IH; } }
        public bool Color { get { return crmap != null && crcbDelay >= 0; } }

        public void DecodeChunk(byte[] buf, int off, int len)
        {
            if (len < 2) throw new InvalidDataException("IW44: short chunk");
            int serial = buf[off], slices = buf[off + 1];
            int pos = off + 2;
            if (serial != cserial) throw new InvalidDataException("IW44: chunk out of order");
            int nslices = cslice + slices;
            if (cserial == 0)
            {
                int major = buf[pos], minor = buf[pos + 1];
                pos += 2;
                int w = (buf[pos] << 8) | buf[pos + 1], h = (buf[pos + 2] << 8) | buf[pos + 3];
                pos += 4;
                int delayByte = 0;
                if ((major & 0x7f) == 1 && minor >= 2) delayByte = buf[pos++];
                crcbDelay = minor >= 2 ? (delayByte & 0x7f) : 0;
                crcbHalf = minor >= 2 ? ((delayByte & 0x80) != 0 ? 0 : 1) : 0;
                if ((major & 0x80) != 0) crcbDelay = -1;
                if (w <= 0 || h <= 0) throw new InvalidDataException("IW44: zero size");
                ymap = new IWMap(w, h);
                ycodec = new IWCodec(ymap);
                if (crcbDelay >= 0)
                {
                    cbmap = new IWMap(w, h);
                    crmap = new IWMap(w, h);
                    cbcodec = new IWCodec(cbmap);
                    crcodec = new IWCodec(crmap);
                }
            }
            // each chunk restarts the arithmetic decoder; the codecs' contexts carry over
            ZP zp = new ZP(buf, pos, off + len - pos);
            int flag = 1;
            while (flag != 0 && cslice < nslices)
            {
                flag = ycodec.CodeSlice(zp);
                if (crcodec != null && cbcodec != null && crcbDelay <= cslice)
                {
                    flag |= cbcodec.CodeSlice(zp);
                    flag |= crcodec.CodeSlice(zp);
                }
                cslice++;
            }
            cserial++;
        }

        // RGB bytes, row 0 = bottom (as encoded)
        public byte[] ToRgb()
        {
            int w = ymap.IW, h = ymap.IH;
            byte[] rgb = new byte[w * h * 3];
            sbyte[] y = ymap.Image(false);
            if (Color)
            {
                sbyte[] cb = cbmap.Image(crcbHalf != 0);
                sbyte[] cr = crmap.Image(crcbHalf != 0);
                for (int i = 0; i < w * h; i++)
                {
                    int yy = y[i], b = cb[i], r = cr[i];
                    int t1 = b >> 2, t2 = r + (r >> 1), t3 = yy + 128 - t1;
                    int tr = yy + 128 + t2, tg = t3 - (t2 >> 1), tb = t3 + (b << 1);
                    rgb[3 * i] = Clamp(tr);
                    rgb[3 * i + 1] = Clamp(tg);
                    rgb[3 * i + 2] = Clamp(tb);
                }
            }
            else
            {
                for (int i = 0; i < w * h; i++)
                {
                    byte g = Clamp(127 - y[i]);
                    rgb[3 * i] = rgb[3 * i + 1] = rgb[3 * i + 2] = g;
                }
            }
            return rgb;
        }

        static byte Clamp(int v) { return (byte)(v < 0 ? 0 : (v > 255 ? 255 : v)); }
    }
}
