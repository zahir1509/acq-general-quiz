"""
Verification for the hand-rolled QR encoder.

Four independent lines of evidence:

1. `format_info` / `version_info` are compared against the published ISO/IEC
   18004 bit strings (values I did not compute with this code).
2. The Reed-Solomon codewords are checked by evaluating the codeword
   polynomial at alpha^0..alpha^(n-1); a valid RS codeword has all syndromes
   zero. `rs_ecc` never computes syndromes, so this genuinely tests it.
3. The number of available data modules is compared against the spec's
   total-codewords + remainder-bits figures, which validates that exactly the
   right function modules were reserved.
4. Full decode round-trip: read format info out of the matrix, un-mask,
   read the zig-zag, de-interleave, syndrome-check every block, and parse the
   byte-mode segment back to the original string.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wiz import qr  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, extra))
        FAILED.append(name)


# --------------------------------------------------------------- 1. BCH tables
def test_format_and_version_info():
    print("format / version information bits (vs ISO table)")
    # (ec_bits, mask) -> published 15-bit string
    known = {
        (0b01, 0): "111011111000100",  # L
        (0b00, 0): "101010000010010",  # M
        (0b11, 0): "011010101011111",  # Q
        (0b10, 0): "001011010001001",  # H
        (0b00, 5): "100000011001110",  # M, mask 5
    }
    for (ec, mask), want in known.items():
        got = format_bits(qr.format_info(mask, ec))
        check("format ec=%d mask=%d" % (ec, mask), got == want, "got %s want %s" % (got, want))

    # 18-bit version information words, ISO/IEC 18004 Annex D
    known_v = {
        7: "000111110010010100",
        8: "001000010110111100",
        9: "001001101010011001",
        40: "101000110001101001",
    }
    for v, want in known_v.items():
        got = bin(qr.version_info(v))[2:].zfill(18)
        check("version %d" % v, got == want, "got %s want %s" % (got, want))

    # BCH design distances: a wrong generator polynomial would break these.
    fvals = [qr.format_info(m, e) for e in range(4) for m in range(8)]
    fmd = min(bin(a ^ b).count("1") for i, a in enumerate(fvals) for b in fvals[i + 1:])
    check("format info: 32 distinct words", len(set(fvals)) == 32)
    check("format info min Hamming distance == 7", fmd == 7, "got %d" % fmd)
    vvals = [qr.version_info(v) for v in range(7, 41)]
    vmd = min(bin(a ^ b).count("1") for i, a in enumerate(vvals) for b in vvals[i + 1:])
    check("version info min Hamming distance == 8", vmd == 8, "got %d" % vmd)


def format_bits(v):
    return bin(v)[2:].zfill(15)


# ---------------------------------------------------------- 2. RS syndromes
def poly_eval_at_alpha(cw, i):
    """Evaluate the codeword polynomial at alpha^i over GF(256)."""
    acc = 0
    for coef in cw:  # Horner, cw[0] is the highest-order term
        acc = qr._gmul(acc, qr._EXP[i]) ^ coef
    return acc


def test_rs_syndromes():
    print("Reed-Solomon syndromes are zero for generated codewords")
    for data_len, n_ec in ((16, 10), (28, 16), (44, 26), (32, 18), (43, 24), (39, 22)):
        data = [(i * 37 + 11) % 256 for i in range(data_len)]
        ec = qr.rs_ecc(data, n_ec)
        check("len(ec) == %d" % n_ec, len(ec) == n_ec)
        full = data + ec
        bad = [i for i in range(n_ec) if poly_eval_at_alpha(full, i) != 0]
        check("syndromes zero (data=%d ec=%d)" % (data_len, n_ec), not bad, "nonzero at %s" % bad)


# ------------------------------------------------- 3. data module capacity
REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}


def test_capacity():
    print("available data modules match spec codeword counts")
    for v in range(1, 11):
        g = qr._Grid(v)
        g.draw_function_patterns()
        avail = len(list(g.data_positions()))
        total = qr._TABLE_M[v][0]
        want = total * 8 + REMAINDER_BITS[v]
        check("v%d modules" % v, avail == want, "got %d want %d" % (avail, want))
        # structural spot checks: finder = dark 3x3 core, light ring, dark border
        check("v%d finder core" % v, g.m[3][3] == 1 and g.m[2][3] == 1)
        check("v%d finder light ring" % v, g.m[1][3] == 0 and g.m[3][1] == 0)
        check("v%d finder border" % v, g.m[0][3] == 1 and g.m[6][0] == 1)
        check("v%d separator" % v, g.m[7][3] == 0 and g.m[3][7] == 0)
        check("v%d dark module" % v, g.m[g.size - 8][8] == 1)
        check("v%d timing" % v, g.m[6][8] == 1 and g.m[6][9] == 0)


# ------------------------------------------------------- 4. decode round-trip
def decode(matrix):
    n = len(matrix)
    version = (n - 17) // 4

    # -- read format info (top-left copy) and validate its BCH
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    val = 0
    for i, (r, c) in enumerate(coords1):
        val |= matrix[r][c] << (14 - i)
    unmasked = val ^ 0x5412
    if qr._bch(unmasked >> 10, 0x537, 10) != (unmasked & 0x3FF):
        raise AssertionError("format info BCH mismatch")
    ec_bits = (unmasked >> 13) & 0b11
    mask = (unmasked >> 10) & 0b111
    assert ec_bits == 0b00, "expected EC level M, got %s" % bin(ec_bits)

    # -- rebuild the function-pattern map and un-mask
    g = qr._Grid(version)
    g.draw_function_patterns()
    f = qr._MASKS[mask]
    bits = []
    for (r, c) in g.data_positions():
        b = matrix[r][c]
        if f(r, c):
            b ^= 1
        bits.append(b)

    total, data_cw, ec_per_block, groups = qr._TABLE_M[version]
    cws = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, total * 8, 8)]

    # -- de-interleave
    blocks = []
    for nb, per in groups:
        for _ in range(nb):
            blocks.append([None] * per)
    maxlen = max(len(b) for b in blocks)
    idx = 0
    for i in range(maxlen):
        for b in blocks:
            if i < len(b):
                b[i] = cws[idx]
                idx += 1
    ecblocks = [[None] * ec_per_block for _ in blocks]
    for i in range(ec_per_block):
        for b in ecblocks:
            b[i] = cws[idx]
            idx += 1

    # -- syndrome check every block
    for bi, (d, e) in enumerate(zip(blocks, ecblocks)):
        for i in range(ec_per_block):
            if poly_eval_at_alpha(d + e, i) != 0:
                raise AssertionError("block %d syndrome %d nonzero" % (bi, i))

    # -- parse the byte-mode segment
    stream = []
    for b in blocks:
        stream.extend(b)
    bitstr = "".join(bin(c)[2:].zfill(8) for c in stream)
    mode = int(bitstr[0:4], 2)
    assert mode == 0b0100, "expected byte mode, got %s" % bin(mode)
    cbits = 8 if version <= 9 else 16
    length = int(bitstr[4:4 + cbits], 2)
    start = 4 + cbits
    raw = bytearray()
    for i in range(length):
        raw.append(int(bitstr[start + i * 8:start + i * 8 + 8], 2))
    return bytes(raw).decode("utf-8")


def test_roundtrip():
    print("encode -> decode round trip")
    cases = [
        "http://192.168.1.5:8080/j",
        "http://10.0.0.42:8080/j?t=Ab3xY9zQ_1kLmN0pQrSt",
        "A",
        "POUNCE & BOUNCE",
        "http://192.168.29.117:8080/j?t=" + "x" * 60,
        "x" * 200,
    ]
    for text in cases:
        m, v = qr.encode(text)
        got = decode(m)
        check("roundtrip v%-2d %r" % (v, text[:34]), got == text,
              "got %r" % got[:40])


if __name__ == "__main__":
    test_format_and_version_info()
    test_rs_syndromes()
    test_capacity()
    test_roundtrip()
    print("")
    if FAILED:
        print("%d CHECK(S) FAILED: %s" % (len(FAILED), FAILED))
        sys.exit(1)
    print("all QR checks passed")
