"""
Minimal, dependency-free QR Code encoder (byte mode, EC level M, versions 1-10).

Why this exists: the quiz runs on a LAN with no internet, so we cannot pull a
QR library from a CDN and we do not want a pip install before a live event.
Ten versions at level M carry 271 bytes, far more than a join URL needs.

Implements ISO/IEC 18004: bit stream -> Reed-Solomon ECC over GF(256) ->
block interleaving -> module placement -> mask selection by the four standard
penalty rules -> format/version information.

`tests/test_qr.py` verifies this by decoding the produced matrix back with an
independent reader and by checking the Reed-Solomon syndromes are zero.
"""

# --------------------------------------------------------------- GF(256)
_EXP = [0] * 512
_LOG = [0] * 256


def _init_gf():
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_gf()


def _gmul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _poly_mul(a, b):
    out = [0] * (len(a) + len(b) - 1)
    for i, ai in enumerate(a):
        if ai == 0:
            continue
        for j, bj in enumerate(b):
            out[i + j] ^= _gmul(ai, bj)
    return out


def rs_ecc(data, n_ec):
    """Reed-Solomon error correction codewords for `data`."""
    gen = [1]
    for i in range(n_ec):
        gen = _poly_mul(gen, [1, _EXP[i]])
    rem = list(data) + [0] * n_ec
    for i in range(len(data)):
        coef = rem[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            rem[i + j] ^= _gmul(g, coef)
    return rem[len(data):]


# ------------------------------------------------------- capacity tables (M)
# version -> (total_codewords, data_codewords, ec_per_block, [(blocks, data_per_block)...])
_TABLE_M = {
    1: (26, 16, 10, [(1, 16)]),
    2: (44, 28, 16, [(1, 28)]),
    3: (70, 44, 26, [(1, 44)]),
    4: (100, 64, 18, [(2, 32)]),
    5: (134, 86, 24, [(2, 43)]),
    6: (172, 108, 16, [(4, 27)]),
    7: (196, 124, 18, [(4, 31)]),
    8: (242, 154, 22, [(2, 38), (2, 39)]),
    9: (292, 182, 22, [(3, 36), (2, 37)]),
    10: (346, 216, 26, [(4, 43), (1, 44)]),
}

_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

_EC_BITS_M = 0b00  # EC level M


def _bch(data, poly, deg):
    """Remainder of (data << deg) mod poly over GF(2)."""
    v = data << deg
    plen = poly.bit_length()
    while v.bit_length() >= plen:
        v ^= poly << (v.bit_length() - plen)
    return v


def format_info(mask, ec_bits=_EC_BITS_M):
    data = (ec_bits << 3) | mask
    return ((data << 10) | _bch(data, 0x537, 10)) ^ 0x5412


def version_info(version):
    return (version << 12) | _bch(version, 0x1F25, 12)


# -------------------------------------------------------------- bit stream
def _build_bitstream(payload, version):
    total, data_cw, ec_per_block, groups = _TABLE_M[version]
    bits = []

    def put(value, length):
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(payload), 8 if version <= 9 else 16)
    for b in payload:
        put(b, 8)

    cap = data_cw * 8
    if len(bits) > cap:
        raise ValueError("payload too long for version %d" % version)
    # terminator
    put(0, min(4, cap - len(bits)))
    # pad to byte boundary
    while len(bits) % 8:
        bits.append(0)
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    pads = [0xEC, 0x11]
    i = 0
    while len(codewords) < data_cw:
        codewords.append(pads[i % 2])
        i += 1

    # split into blocks, compute ECC
    blocks, ecblocks = [], []
    pos = 0
    for nblocks, per in groups:
        for _ in range(nblocks):
            blk = codewords[pos:pos + per]
            pos += per
            blocks.append(blk)
            ecblocks.append(rs_ecc(blk, ec_per_block))

    # interleave
    out = []
    maxlen = max(len(b) for b in blocks)
    for i in range(maxlen):
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ec_per_block):
        for b in ecblocks:
            out.append(b[i])
    assert len(out) == total, (len(out), total)
    return out


def _choose_version(payload):
    for v in range(1, 11):
        cap_bits = _TABLE_M[v][1] * 8
        header = 4 + (8 if v <= 9 else 16)
        if header + len(payload) * 8 <= cap_bits:
            return v
    raise ValueError("payload too long for this encoder (max ~216 bytes)")


# ------------------------------------------------------------- module grid
class _Grid(object):
    def __init__(self, version):
        self.version = version
        self.size = 17 + 4 * version
        n = self.size
        self.m = [[0] * n for _ in range(n)]      # 0/1 module value
        self.fixed = [[False] * n for _ in range(n)]  # function pattern?

    def set(self, r, c, val, fixed=True):
        self.m[r][c] = val
        self.fixed[r][c] = fixed

    def draw_function_patterns(self):
        n = self.size
        # finder patterns + separators
        for (br, bc) in ((0, 0), (0, n - 7), (n - 7, 0)):
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    r, c = br + dr, bc + dc
                    if not (0 <= r < n and 0 <= c < n):
                        continue
                    if 0 <= dr <= 6 and 0 <= dc <= 6:
                        ring = max(abs(dr - 3), abs(dc - 3))
                        self.set(r, c, 1 if ring != 2 else 0)
                    else:
                        self.set(r, c, 0)
        # timing patterns
        for i in range(8, n - 8):
            v = 1 if i % 2 == 0 else 0
            self.set(6, i, v)
            self.set(i, 6, v)
        # alignment patterns
        centers = _ALIGN[self.version]
        for r in centers:
            for c in centers:
                if (r < 8 and c < 8) or (r < 8 and c > n - 9) or (r > n - 9 and c < 8):
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        ring = max(abs(dr), abs(dc))
                        self.set(r + dr, c + dc, 1 if ring != 1 else 0)
        # reserve format info areas
        for i in range(9):
            if i != 6:
                self.set(8, i, 0)
                self.set(i, 8, 0)
        for i in range(8):
            self.set(8, n - 1 - i, 0)
            self.set(n - 1 - i, 8, 0)
        # dark module -- inside the reserved strip above, so it is set last
        self.set(n - 8, 8, 1)
        # reserve version info
        if self.version >= 7:
            for i in range(6):
                for j in range(3):
                    self.set(n - 11 + j, i, 0)
                    self.set(i, n - 11 + j, 0)

    def data_positions(self):
        """Zig-zag module order, right to left in column pairs, skipping col 6."""
        n = self.size
        col = n - 1
        upward = True
        while col > 0:
            if col == 6:
                col -= 1
            rows = range(n - 1, -1, -1) if upward else range(n)
            for r in rows:
                for c in (col, col - 1):
                    if not self.fixed[r][c]:
                        yield r, c
            col -= 2
            upward = not upward

    def place_data(self, codewords):
        bits = []
        for cw in codewords:
            for i in range(7, -1, -1):
                bits.append((cw >> i) & 1)
        it = iter(bits)
        for (r, c) in self.data_positions():
            try:
                self.m[r][c] = next(it)
            except StopIteration:
                self.m[r][c] = 0

    def apply_mask(self, mask):
        n = self.size
        f = _MASKS[mask]
        for r in range(n):
            for c in range(n):
                if not self.fixed[r][c] and f(r, c):
                    self.m[r][c] ^= 1

    def draw_format_info(self, mask):
        n = self.size
        fmt = format_info(mask)
        # b[i] is bit i counted from the LSB (0..14)
        b = [(fmt >> i) & 1 for i in range(15)]
        # top-left, around (8,*) and (*,8)
        coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
                   (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
        for i, (r, c) in enumerate(coords1):
            self.set(r, c, b[14 - i])
        coords2 = [(n - 1, 8), (n - 2, 8), (n - 3, 8), (n - 4, 8), (n - 5, 8),
                   (n - 6, 8), (n - 7, 8),
                   (8, n - 8), (8, n - 7), (8, n - 6), (8, n - 5), (8, n - 4),
                   (8, n - 3), (8, n - 2), (8, n - 1)]
        for i, (r, c) in enumerate(coords2):
            self.set(r, c, b[14 - i])
        self.set(n - 8, 8, 1)  # dark module (must stay 1)

    def draw_version_info(self):
        if self.version < 7:
            return
        n = self.size
        vi = version_info(self.version)
        for i in range(18):
            bit = (vi >> i) & 1
            self.set(n - 11 + i % 3, i // 3, bit)
            self.set(i // 3, n - 11 + i % 3, bit)

    # ------------------------------------------------------------ penalties
    def penalty(self):
        n, m = self.size, self.m
        score = 0
        # rule 1: runs of >=5
        for line in list(m) + [[m[r][c] for r in range(n)] for c in range(n)]:
            run, prev = 1, line[0]
            for v in line[1:]:
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run, prev = 1, v
            if run >= 5:
                score += 3 + (run - 5)
        # rule 2: 2x2 blocks
        for r in range(n - 1):
            for c in range(n - 1):
                v = m[r][c]
                if v == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                    score += 3
        # rule 3: 1:1:3:1:1 finder-like pattern with 4 light modules on a side
        pat_a = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
        pat_b = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1]
        for line in list(m) + [[m[r][c] for r in range(n)] for c in range(n)]:
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == pat_a or seg == pat_b:
                    score += 40
        # rule 4: dark proportion
        dark = sum(sum(row) for row in m)
        pct = dark * 100.0 / (n * n)
        score += 10 * int(abs(pct - 50) / 5)
        return score


_MASKS = [
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: ((i // 2) + (j // 3)) % 2 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) == 0,
    lambda i, j: (((i * j) % 2 + (i * j) % 3) % 2) == 0,
    lambda i, j: (((i + j) % 2 + (i * j) % 3) % 2) == 0,
]


def encode(text):
    """Returns (matrix, version) where matrix[r][c] is 1 for a dark module."""
    payload = text.encode("utf-8")
    version = _choose_version(payload)
    codewords = _build_bitstream(payload, version)

    best = None
    for mask in range(8):
        g = _Grid(version)
        g.draw_function_patterns()
        g.place_data(codewords)
        g.apply_mask(mask)
        g.draw_format_info(mask)
        g.draw_version_info()
        p = g.penalty()
        if best is None or p < best[0]:
            best = (p, g, mask)
    return best[1].m, version


def to_svg(text, module=6, quiet=4, dark="#0a0a0a", light="#ffffff"):
    m, _v = encode(text)
    n = len(m)
    total = (n + 2 * quiet) * module
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" shape-rendering="crispEdges">' % (total, total, total, total),
        '<rect width="%d" height="%d" fill="%s"/>' % (total, total, light),
        '<path fill="%s" d="' % dark,
    ]
    d = []
    for r in range(n):
        c = 0
        while c < n:
            if m[r][c]:
                start = c
                while c < n and m[r][c]:
                    c += 1
                d.append(
                    "M%d %dh%dv%dh-%dz"
                    % ((start + quiet) * module, (r + quiet) * module,
                       (c - start) * module, module, (c - start) * module)
                )
            else:
                c += 1
    parts.append("".join(d))
    parts.append('"/></svg>')
    return "".join(parts)


def to_ascii(text):
    """Terminal rendering, so the quizmaster can show a QR from the console."""
    m, _v = encode(text)
    n = len(m)
    rows = [[0] * (n + 4)] * 2 + [[0, 0] + r + [0, 0] for r in m] + [[0] * (n + 4)] * 2
    out = []
    for i in range(0, len(rows), 2):
        top = rows[i]
        bot = rows[i + 1] if i + 1 < len(rows) else [0] * len(top)
        line = []
        for j in range(len(top)):
            t, b = top[j], bot[j]
            # dark module -> filled block; use half blocks to halve the height
            line.append({(0, 0): "█", (1, 1): " ", (1, 0): "▄", (0, 1): "▀"}[(t, b)])
        out.append("".join(line))
    return "\n".join(out)
