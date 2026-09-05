# -*- coding: utf-8 -*-
"""common/dds_pixel 解码器测试。

- 标量 oracle：直接移植 bimg 参考实现的 BC1/BC2/BC3/BC4/BC5/BC7 标量解码，
  与向量化 numpy 实现做随机模糊对拍（随机块逐像素一致）。
- 手工向量：几个可手算的锚点块（含 BC7 mode 6 全索引为 0 的块）。
- DDS 容器：头解析、DX10、尺寸、不支持格式的容错。
"""

import importlib.util
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "_dds_pixel_under_test", REPO_ROOT / "common" / "dds_pixel.py"
)
dds_pixel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dds_pixel
_spec.loader.exec_module(dds_pixel)

P2 = dds_pixel._BPTC_P2
P3 = dds_pixel._BPTC_P3
A2 = dds_pixel._BPTC_A2
A3 = dds_pixel._BPTC_A3
FACTORS = dds_pixel._BPTC_FACTORS
MODE_INFO = dds_pixel._BC7_MODE_INFO


# ---------------------------------------------------------------------------
# 标量 oracle（bimg C 的逐行移植）
# ---------------------------------------------------------------------------
class _BitReader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def _read_at(self, bitpos, n):
        shift = bitpos & 7
        byte_pos = bitpos >> 3
        chunk = self.data[byte_pos:byte_pos + 4]
        chunk = chunk + b"\x00" * (4 - len(chunk))
        return (int.from_bytes(chunk, "little") >> shift) & ((1 << n) - 1)

    def read(self, n):
        pos = self.pos
        self.pos += n
        return self._read_at(pos, n)

    def peek(self, offset, n):
        return self._read_at(self.pos + offset, n)


def _brc(v, frm, to):
    x = v * ((1 << to) - 1) + ((1 << frm) - 1)
    return (x + (x >> frm)) >> frm


def scalar_565(c):
    return (_brc(c & 0x1F, 5, 8), _brc((c >> 5) & 0x3F, 6, 8), _brc((c >> 11) & 0x1F, 5, 8))


def scalar_bc1(src):
    colors = [0, 0, 0, 0] * 4
    c0 = src[0] | src[1] << 8
    c1 = src[2] | src[3] << 8
    r0, g0, b0 = scalar_565(c0)
    r1, g1, b1 = scalar_565(c1)
    colors[0:3] = (r0, g0, b0)
    colors[4:7] = (r1, g1, b1)
    colors[3] = colors[7] = 255
    if c0 > c1:
        colors[8] = (2 * colors[0] + colors[4]) // 3
        colors[9] = (2 * colors[1] + colors[5]) // 3
        colors[10] = (2 * colors[2] + colors[6]) // 3
        colors[11] = 255
        colors[12] = (colors[0] + 2 * colors[4]) // 3
        colors[13] = (colors[1] + 2 * colors[5]) // 3
        colors[14] = (colors[2] + 2 * colors[6]) // 3
        colors[15] = 255
    else:
        colors[8] = (colors[0] + colors[4]) // 2
        colors[9] = (colors[1] + colors[5]) // 2
        colors[10] = (colors[2] + colors[6]) // 2
        colors[11] = 255
        colors[12] = colors[13] = colors[14] = colors[15] = 0
    out = np.zeros((16, 4), np.uint8)
    idx32 = int.from_bytes(src[4:8], "little")
    for i in range(16):
        idx = (idx32 >> (2 * i)) & 3
        # bimg 的 colors 以 B,G,R 存储（写 BGRA 输出），这里转成 RGBA 与向量化实现一致
        out[i] = (colors[idx * 4 + 2], colors[idx * 4 + 1], colors[idx * 4 + 0], colors[idx * 4 + 3])
    return out


def scalar_bc23_color(src):
    colors = [0, 0, 0] * 4
    c0 = src[0] | src[1] << 8
    c1 = src[2] | src[3] << 8
    r0, g0, b0 = scalar_565(c0)
    r1, g1, b1 = scalar_565(c1)
    colors[0:3] = (r0, g0, b0)
    colors[3:6] = (r1, g1, b1)
    colors[6] = (2 * colors[0] + colors[3]) // 3
    colors[7] = (2 * colors[1] + colors[4]) // 3
    colors[8] = (2 * colors[2] + colors[5]) // 3
    colors[9] = (colors[0] + 2 * colors[3]) // 3
    colors[10] = (colors[1] + 2 * colors[4]) // 3
    colors[11] = (colors[2] + 2 * colors[5]) // 3
    out = np.zeros((16, 3), np.uint8)
    idx32 = int.from_bytes(src[4:8], "little")
    for i in range(16):
        idx = (idx32 >> (2 * i)) & 3
        # colors 以 B,G,R 存储，转成 RGB
        out[i] = (colors[idx * 3 + 2], colors[idx * 3 + 1], colors[idx * 3 + 0])
    return out


def scalar_bc23_alpha(src):
    out = np.zeros((16, 1), np.uint8)
    a64 = int.from_bytes(src[0:8], "little")
    for i in range(16):
        out[i] = _brc((a64 >> (4 * i)) & 0xF, 4, 8)
    return out


def scalar_bc45_alpha(src):
    alpha = [0] * 8
    alpha[0] = src[0]
    alpha[1] = src[1]
    if alpha[0] > alpha[1]:
        alpha[2] = (6 * alpha[0] + 1 * alpha[1]) // 7
        alpha[3] = (5 * alpha[0] + 2 * alpha[1]) // 7
        alpha[4] = (4 * alpha[0] + 3 * alpha[1]) // 7
        alpha[5] = (3 * alpha[0] + 4 * alpha[1]) // 7
        alpha[6] = (2 * alpha[0] + 5 * alpha[1]) // 7
        alpha[7] = (1 * alpha[0] + 6 * alpha[1]) // 7
    else:
        alpha[2] = (4 * alpha[0] + 1 * alpha[1]) // 5
        alpha[3] = (3 * alpha[0] + 2 * alpha[1]) // 5
        alpha[4] = (2 * alpha[0] + 3 * alpha[1]) // 5
        alpha[5] = (1 * alpha[0] + 4 * alpha[1]) // 5
        alpha[6] = 0
        alpha[7] = 255
    idx0 = int.from_bytes(src[2:5], "little")
    idx1 = int.from_bytes(src[5:8], "little")
    out = np.zeros(16, np.uint8)
    for i in range(16):
        out[i] = alpha[(idx0 >> (3 * i)) & 7] if i < 8 else alpha[(idx1 >> (3 * (i - 8))) & 7]
    return out


def scalar_bc7(src):
    br = _BitReader(src)
    mode = 0
    while mode < 8 and br.read(1) == 0:
        mode += 1
    if mode == 8:
        return np.zeros((16, 4), np.uint8)

    ns, pb, rb, isb, cb, ab, epb, spb, ib, ib2 = MODE_INFO[mode]
    mode_pbits = epb if epb else spb
    part = br.read(pb)
    rot = br.read(rb)
    isel = br.read(isb)

    ep = np.zeros((2 * ns, 4), np.uint16)
    # 参考实现位序：先全部 R，再全部 G，再全部 B
    for ch in range(3):
        for ii in range(ns):
            ep[ii * 2 + 0, ch] = br.read(cb) << mode_pbits
            ep[ii * 2 + 1, ch] = br.read(cb) << mode_pbits
    if ab:
        for ii in range(ns):
            ep[ii * 2 + 0, 3] = br.read(ab) << mode_pbits
            ep[ii * 2 + 1, 3] = br.read(ab) << mode_pbits
    else:
        ep[:, 3] = 255
    if mode_pbits:
        for ii in range(ns):
            pda = br.read(mode_pbits)
            pdb = pda if spb else br.read(mode_pbits)
            ep[ii * 2 + 0] |= pda
            ep[ii * 2 + 1] |= pdb

    color_bits = cb + mode_pbits
    for ii in range(2 * ns):
        for ch in range(3):
            ep[ii, ch] = _brc(int(ep[ii, ch]), color_bits, 8)
    if ab:
        alpha_bits = ab + mode_pbits
        for ii in range(2 * ns):
            ep[ii, 3] = _brc(int(ep[ii, 3]), alpha_bits, 8)

    has_ib1 = ib2 != 0
    offset0 = 0
    offset1 = ns * (16 * ib - 1)
    fac0 = FACTORS[ib - 2]
    fac1 = FACTORS[ib2 - 2] if has_ib1 else fac0

    out = np.zeros((16, 4), np.uint32)
    for idx in range(16):
        subset_index = 0
        index_anchor = 0
        if ns == 2:
            subset_index = int((P2[part] >> idx) & 1)
            index_anchor = int(A2[part]) if subset_index else 0
        elif ns == 3:
            subset_index = int((P3[part] >> (2 * idx)) & 3)
            index_anchor = int(A3[subset_index - 1, part]) if subset_index else 0
        anchor = 1 if idx == index_anchor else 0
        num0 = ib - anchor
        index0 = br.peek(offset0, num0)
        offset0 += num0
        if has_ib1:
            num1 = ib2 - anchor
            index1 = br.peek(offset1, num1)
            offset1 += num1
        else:
            index1 = index0
        idx_pairs = (index0, index1)
        fc = (fac0 if isel == 0 else fac1)[idx_pairs[isel]]
        fa = (fac0 if isel == 1 else fac1)[idx_pairs[1 - isel]]

        ss = subset_index * 2
        fc_i = int(fc)
        fa_i = int(fa)
        rr = (int(ep[ss, 0]) * (64 - fc_i) + int(ep[ss + 1, 0]) * fc_i + 32) >> 6
        gg = (int(ep[ss, 1]) * (64 - fc_i) + int(ep[ss + 1, 1]) * fc_i + 32) >> 6
        bb = (int(ep[ss, 2]) * (64 - fc_i) + int(ep[ss + 1, 2]) * fc_i + 32) >> 6
        aa = (int(ep[ss, 3]) * (64 - fa_i) + int(ep[ss + 1, 3]) * fa_i + 32) >> 6
        if rot == 1:
            aa, rr = rr, aa
        elif rot == 2:
            aa, gg = gg, aa
        elif rot == 3:
            aa, bb = bb, aa
        out[idx] = (rr, gg, bb, aa)
    return out.astype(np.uint8)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
class BitRangeConvertTests(unittest.TestCase):
    def test_known_rounding(self):
        self.assertEqual(int(dds_pixel._bit_range_convert(np.array([17], np.uint32), 5, 8)[0]), 140)
        self.assertEqual(int(dds_pixel._bit_range_convert(np.array([31], np.uint32), 5, 8)[0]), 255)
        self.assertEqual(int(dds_pixel._bit_range_convert(np.array([0], np.uint32), 5, 8)[0]), 0)
        self.assertEqual(int(dds_pixel._bit_range_convert(np.array([15], np.uint32), 4, 8)[0]), 255)
        self.assertEqual(int(dds_pixel._bit_range_convert(np.array([1], np.uint32), 4, 8)[0]), 17)


class Bc1HandVectors(unittest.TestCase):
    def test_four_color_white_black(self):
        # c0=0xFFFF(白) > c1=0x0000(黑)，全索引 2 -> (2*255+0)/3 = 170
        idx = 0
        for i in range(16):
            idx |= 2 << (2 * i)
        block = struct.pack("<HHI", 0xFFFF, 0x0000, idx)
        out = dds_pixel._decode_bc1(np.frombuffer(block, np.uint8).reshape(1, 8))[0]
        self.assertTrue(np.array_equal(out, np.full((16, 4), (170, 170, 170, 255), np.uint8)))

    def test_three_color_transparent(self):
        # c0=0x0000(黑) <= c1=0xFFFF(白)：索引 3 -> 透明黑，索引 2 -> 中灰 127
        idx = 0
        for i in range(16):
            idx |= (3 if i % 2 else 2) << (2 * i)
        block = struct.pack("<HHI", 0x0000, 0xFFFF, idx)
        out = dds_pixel._decode_bc1(np.frombuffer(block, np.uint8).reshape(1, 8))[0]
        self.assertTrue(np.array_equal(out[0], np.array([127, 127, 127, 255], np.uint8)))
        self.assertTrue(np.array_equal(out[1], np.array([0, 0, 0, 0], np.uint8)))


class OracleFuzzTests(unittest.TestCase):
    def _check(self, vectorized, scalar, count, raw_size):
        rng = np.random.default_rng(12345)
        for _ in range(count):
            raw = rng.integers(0, 256, size=raw_size, dtype=np.uint8)
            v = vectorized(raw.reshape(1, raw_size))[0]
            s = scalar(raw.tobytes())
            self.assertTrue(np.array_equal(v, s), f"block={raw.tobytes().hex()}")

    def test_bc1_fuzz(self):
        self._check(dds_pixel._decode_bc1, scalar_bc1, 300, 8)

    def test_bc2_fuzz(self):
        def vec(blocks):
            return dds_pixel._decode_bc2(blocks)
        def sca(raw):
            rgb = scalar_bc23_color(raw[8:16])
            alpha = scalar_bc23_alpha(raw[0:8])
            return np.concatenate([rgb, alpha], axis=-1)
        self._check(vec, sca, 200, 16)

    def test_bc3_fuzz(self):
        def vec(blocks):
            return dds_pixel._decode_bc3(blocks)
        def sca(raw):
            rgb = scalar_bc23_color(raw[8:16])
            alpha = scalar_bc45_alpha(raw[0:8])
            return np.concatenate([rgb, alpha[:, None]], axis=-1)
        self._check(vec, sca, 200, 16)

    def test_bc4_fuzz(self):
        def vec(blocks):
            return dds_pixel._decode_bc4(blocks)
        def sca(raw):
            v = scalar_bc45_alpha(raw[0:8])
            out = np.zeros((16, 4), np.uint8)
            out[:, 0] = v
            out[:, 3] = 255
            return out
        self._check(vec, sca, 200, 8)

    def test_bc5_fuzz(self):
        def vec(blocks):
            return dds_pixel._decode_bc5(blocks)
        def sca(raw):
            r = scalar_bc45_alpha(raw[0:8])
            g = scalar_bc45_alpha(raw[8:16])
            out = np.zeros((16, 4), np.uint8)
            out[:, 0] = r
            out[:, 1] = g
            out[:, 3] = 255
            return out
        self._check(vec, sca, 200, 16)

    def test_bc7_fuzz_all_modes(self):
        rng = np.random.default_rng(777)
        for mode in range(8):
            # 首字节强制 mode：m 个前导零 + 1
            byte0 = 1 << mode
            for _ in range(80):
                raw = np.zeros(16, np.uint8)
                raw[1:] = rng.integers(0, 256, size=15, dtype=np.uint8)
                raw[0] = byte0  # mode 0..7；mode 7 -> 0x80
                v = dds_pixel._decode_bc7(raw.reshape(1, 16))[0]
                s = scalar_bc7(raw.tobytes())
                self.assertTrue(np.array_equal(v, s), f"mode={mode} block={raw.tobytes().hex()}")
        # 随机首字节（覆盖各 mode 概率分布 + 非法 0x00 -> 全黑）
        for _ in range(120):
            raw = rng.integers(0, 256, size=16, dtype=np.uint8)
            v = dds_pixel._decode_bc7(raw.reshape(1, 16))[0]
            s = scalar_bc7(raw.tobytes())
            self.assertTrue(np.array_equal(v, s), f"block={raw.tobytes().hex()}")

    def test_bc7_mode6_hand_built(self):
        """手工构造 mode 6 块（1 子集、7 位 RGBA、2 P 位、4 位索引），
        索引全 0 -> 全部像素等于扩展后的 endpoint0。"""
        # mode 6: mode 位=7（6 个 0 + 1），byte0 = 0b01000000（bit6=1）
        # 字段按参考实现的通道主序：R0,R1 / G0,G1 / B0,B1 / A0,A1 之后才是 P 位与索引
        fields = [(7, 64)]  # (bits, value) LSB 序
        fields.append((7, 3))   # R0
        fields.append((7, 9))   # R1
        fields.append((7, 4))   # G0
        fields.append((7, 10))  # G1
        fields.append((7, 5))   # B0
        fields.append((7, 11))  # B1
        fields.append((7, 6))   # A0
        fields.append((7, 12))  # A1
        fields.append((1, 1))   # p0
        fields.append((1, 0))   # p1
        fields.append((3, 0))   # 像素0 索引(锚点, 3 位)
        for _ in range(15):
            fields.append((4, 0))  # 其余像素索引 4 位全 0
        bits = 0
        offset = 0
        for nbits, value in fields:
            bits |= value << offset
            offset += nbits
        self.assertEqual(offset, 128)
        raw = bits.to_bytes(16, "little")
        out = dds_pixel._decode_bc7(np.frombuffer(raw, np.uint8).reshape(1, 16))[0]
        # ep0 = (v << 1) | p0：R=(3<<1)|1=7, G=(4<<1)|1=9, B=11, A=13
        expected = np.array([7, 9, 11, 13], np.uint8)
        self.assertTrue(np.array_equal(out, np.full((16, 4), expected, np.uint8)))
        # 与标量 oracle 一致
        self.assertTrue(np.array_equal(out, scalar_bc7(raw)))


def _build_dds(fourcc, width, height, payload, dx10=None):
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)
    struct.pack_into("<I", header, 8, 0x1007)  # CAPS|HEIGHT|WIDTH|PIXELFORMAT
    struct.pack_into("<I", header, 12, height)
    struct.pack_into("<I", header, 16, width)
    struct.pack_into("<I", header, 20, 0)  # pitch
    struct.pack_into("<I", header, 28, 0)  # mips
    struct.pack_into("<I", header, 76, 32)  # pf size
    struct.pack_into("<I", header, 80, 0x4)  # FOURCC
    header[84:88] = fourcc
    struct.pack_into("<I", header, 108, 0x1000)  # caps
    if dx10 is not None:
        header[84:88] = b"DX10"
        ext = struct.pack("<III", dx10, 3, 1)
        return bytes(header) + b"\x00" * 0 + ext[0:12] + b"\x00\x00\x00\x00\x00\x00\x00\x00" + payload
    return bytes(header) + payload


class DdsContainerTests(unittest.TestCase):
    def test_parse_dxt1(self):
        data = _build_dds(b"DXT1", 8, 8, b"\x00" * 32)
        header = dds_pixel.parse_dds_header(data)
        self.assertEqual(header["kind"], "bc1")
        self.assertEqual(dds_pixel.dds_dimensions(data), (8, 8))

    def test_parse_dx10_bc7(self):
        # BC7_UNORM_SRGB = 99
        data = _build_dds(b"DX10", 4, 4, b"\x00" * 16, dx10=99)
        header = dds_pixel.parse_dds_header(data)
        self.assertEqual(header["kind"], "bc7")

    def test_dx10_rgba8(self):
        data = _build_dds(b"DX10", 2, 1, bytes([10, 20, 30, 40, 50, 60, 70, 80]), dx10=28)
        out = dds_pixel.decode_dds_rgba8(data)
        self.assertEqual(out.shape, (1, 2, 4))
        self.assertTrue(np.array_equal(out[0, 0], [10, 20, 30, 40]))
        self.assertTrue(np.array_equal(out[0, 1], [50, 60, 70, 80]))

    def test_dx10_bgra8_preserves_alpha(self):
        # DXGI 87 B8G8R8A8_UNORM：内存序 B,G,R,A；alpha 必须参与比较，
        # 否则仅 alpha 不同的贴图会被像素去重误判为重复。
        pixels = bytes([30, 20, 10, 40, 70, 60, 50, 80])
        data = _build_dds(b"DX10", 2, 1, pixels, dx10=87)
        out = dds_pixel.decode_dds_rgba8(data)
        self.assertTrue(np.array_equal(out[0, 0], [10, 20, 30, 40]))
        self.assertTrue(np.array_equal(out[0, 1], [50, 60, 70, 80]))

    def test_dx10_bgrx8_ignores_x(self):
        # DXGI 88 B8G8R8X8_UNORM：X 通道无意义，忽略并置不透明。
        pixels = bytes([30, 20, 10, 0, 70, 60, 50, 255])
        data = _build_dds(b"DX10", 2, 1, pixels, dx10=88)
        out = dds_pixel.decode_dds_rgba8(data)
        self.assertTrue(np.array_equal(out[0, 0], [10, 20, 30, 255]))
        self.assertTrue(np.array_equal(out[0, 1], [50, 60, 70, 255]))

    def test_bc6h_unsupported(self):
        data = _build_dds(b"BC6H", 4, 4, b"\x00" * 16)
        self.assertIsNone(dds_pixel.decode_dds_rgba8(data))
        self.assertIn("BC6H", dds_pixel.last_error)

    def test_cubemap_rejected(self):
        header = bytearray(_build_dds(b"DXT1", 4, 4, b"\x00" * 8))
        struct.pack_into("<I", header, 112, 0xFE00)  # caps2 cubemap
        self.assertIsNone(dds_pixel.decode_dds_rgba8(bytes(header)))
        self.assertIn("立方体", dds_pixel.last_error)

    def test_non_multiple_of_four_clip(self):
        # 5x5 BC1：需要 2x2 块，输出裁剪为 5x5
        c0, c1 = 0xFFFF, 0x0000
        idx = 0
        for i in range(16):
            idx |= 1 << (2 * i)
        block = struct.pack("<HHI", c0, c1, idx)
        data = _build_dds(b"DXT1", 5, 5, block * 4)
        out = dds_pixel.decode_dds_rgba8(data)
        self.assertEqual(out.shape, (5, 5, 4))
        self.assertEqual(out[4, 4].tolist(), [0, 0, 0, 255])

    def test_dxt1_end_to_end(self):
        # 8x4 双块：全索引 2 -> 灰 170
        idx = 0
        for i in range(16):
            idx |= 2 << (2 * i)
        block = struct.pack("<HHI", 0xFFFF, 0x0000, idx)
        data = _build_dds(b"DXT1", 8, 4, block * 2)
        out = dds_pixel.decode_dds_rgba8(data)
        self.assertEqual(out.shape, (4, 8, 4))
        self.assertTrue(np.array_equal(out, np.full((4, 8, 4), (170, 170, 170, 255), np.uint8)))

    def test_dx9_uncompressed_32bit_masks(self):
        # D3DFMT_A8R8G8B8：内存序 B,G,R,A，掩码 R=0x00ff0000
        pixels = bytes([0, 0, 255, 255, 10, 20, 30, 255])  # 像素1: (255,0,0,255) 像素2: (30,20,10,255)
        data = _build_dds(b"\x00\x00\x00\x00", 2, 1, pixels)
        header = bytearray(data)
        struct.pack_into("<I", header, 80, 0x41)  # RGB|ALPHAPIXELS
        struct.pack_into("<I", header, 88, 32)
        struct.pack_into("<I", header, 92, 0x00FF0000)
        struct.pack_into("<I", header, 96, 0x0000FF00)
        struct.pack_into("<I", header, 100, 0x000000FF)
        struct.pack_into("<I", header, 104, 0xFF000000)
        out = dds_pixel.decode_dds_rgba8(bytes(header))
        self.assertTrue(np.array_equal(out[0, 0], [255, 0, 0, 255]))
        self.assertTrue(np.array_equal(out[0, 1], [30, 20, 10, 255]))

    def test_dx9_uncompressed_565(self):
        # D3DFMT_R5G6B5：R=0xF800 G=0x07E0 B=0x001F（内存序 B,G,R？565 无 alpha）
        pixel = 0b11111_000000_11111  # R=31 G=0 B=31
        px = struct.pack("<H", pixel)
        data = _build_dds(b"\x00\x00\x00\x00", 1, 1, px)
        header = bytearray(data)
        struct.pack_into("<I", header, 80, 0x40)
        struct.pack_into("<I", header, 88, 16)
        struct.pack_into("<I", header, 92, 0xF800)
        struct.pack_into("<I", header, 96, 0x07E0)
        struct.pack_into("<I", header, 100, 0x001F)
        out = dds_pixel.decode_dds_rgba8(bytes(header))
        # 31 -> 255 (bitRangeConvert(31,5,8)=255)，0 -> 0
        self.assertTrue(np.array_equal(out[0, 0], [255, 0, 255, 255]))

    def test_files_pixels_equal(self):
        idx = 0
        for i in range(16):
            idx |= 2 << (2 * i)
        block1 = struct.pack("<HHI", 0xFFFF, 0x0000, idx)
        block2 = struct.pack("<HHI", 0xFFFE, 0x0001, idx)  # 不同端点但插值后仍是 170？(2*254+1)/3=169 ≠ 170
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "a.dds")
            p2 = os.path.join(td, "b.dds")
            with open(p1, "wb") as f:
                f.write(_build_dds(b"DXT1", 4, 4, block1))
            with open(p2, "wb") as f:
                f.write(_build_dds(b"DXT3", 4, 4, block2 + b"\xff" * 8))
            dds_pixel.last_error = ""
            equal, reason = dds_pixel.dds_files_pixels_equal(p1, p2)
            self.assertFalse(equal, reason)
            # DXT3 用相同颜色块 + alpha 全 F -> 像素与 DXT1 全一致
            with open(p2, "wb") as f:
                f.write(_build_dds(b"DXT3", 4, 4, b"\xff" * 8 + block1))
            equal, reason = dds_pixel.dds_files_pixels_equal(p1, p2)
            self.assertTrue(equal, reason)


if __name__ == "__main__":
    unittest.main()