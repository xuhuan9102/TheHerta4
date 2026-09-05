# -*- coding: utf-8 -*-
"""DDS 贴图像素级解码与比较（纯 numpy 实现，无 Blender 依赖）。

供「贴图资源去重」节点的像素级去重模式使用：把 DDS 文件解码为 RGBA8 像素数组，
两文件像素完全相同时（即使文件格式不同、MD5 不同）判为重复。

支持格式：
- BC1 (DXT1) / BC2 (DXT2/DXT3) / BC3 (DXT4/DXT5)
- BC4 (ATI1/BC4U) / BC5 (ATI2/BC5U)
- BC7（全部 8 种 mode）
- 非压缩 DX9：32/24/16/8 位（按位掩码提取 R/G/B/A）
- DX10 头：RGBA8 / BGRA8 / BGRX8 / R8 / A8 及 BC1-BC7 的 DXGI 映射
- BC6H（HDR）暂不支持解码，返回 None（last_error 说明）

算法移植自 bimg 参考实现（Branimir Karadzic 的 bimg 库 expose decodeBlockDxt1/DecodeBlockBc7，
与 Khronos ARB_texture_compression_bptc 规范一致）。
"""

import os
import struct

import numpy as np

# ---------------------------------------------------------------------------
# BC7 参考表（Khronos BC7 规范 Table.P2/P3/A2/A3，从 bimg 参考实现提取）
# ---------------------------------------------------------------------------
_BPTC_P2 = np.array([
    52428, 34952, 61166, 60616, 51328, 65260, 65224, 60544, 51200, 65516, 65152, 59392,
    65512, 65280, 65520, 61440, 63248, 142, 28928, 2254, 140, 29456, 12544, 36046,
    2188, 12560, 26214, 13932, 6120, 4080, 29070, 14748, 43690, 61680, 23130, 13260,
    15420, 21930, 38550, 42330, 29646, 5064, 12876, 15324, 27030, 49980, 39270, 1632,
    626, 1252, 20032, 10016, 51510, 37740, 14790, 25500, 37686, 40134, 33150, 59160,
    52464, 4044, 30532, 60962,
], dtype=np.uint16)

_BPTC_P3 = np.array([
    2858963024, 1784303680, 1515864576, 1414570152, 2779054080, 2694860880, 1431675040,
    1515868240, 2857697280, 2857719040, 2863289600, 2425393296, 2492765332, 2762253476,
    2846200912, 705315408, 2777960512, 172118100, 2779096320, 1436590240, 2829603924,
    1785348160, 2762231808, 437912832, 5285028, 2862977168, 342452500, 1768494080,
    2693105056, 2860651540, 1352967248, 1784283648, 2846195712, 1351655592, 2829094992,
    606348324, 11162880, 613566756, 608801316, 1352993360, 1342874960, 2863285316,
    1717960704, 2778768800, 1352683680, 1764256040, 1152035396, 1717986816, 2856600644,
    1420317864, 2508232064, 2526451200, 2824098984, 2157286784, 2853442580, 2526412800,
    2863272980, 2689618080, 2695210400, 2516582400, 1082146944, 2846402984, 2863311428,
    709513812,
], dtype=np.uint32)

_BPTC_A2 = np.array([
    15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15,
    15, 2, 8, 2, 2, 8, 8, 15, 2, 8, 2, 2, 8, 8, 2, 2,
    15, 15, 6, 8, 2, 8, 15, 15, 2, 8, 2, 2, 2, 15, 15, 6,
    6, 2, 6, 8, 15, 15, 2, 2, 15, 15, 15, 15, 15, 2, 2, 15,
], dtype=np.uint8)

_BPTC_A3 = np.array([
    # subset 1
    3, 3, 15, 15, 8, 3, 15, 15, 8, 8, 6, 6, 6, 5, 3, 3,
    3, 3, 8, 15, 3, 3, 6, 10, 5, 8, 8, 6, 8, 5, 15, 15,
    8, 15, 3, 5, 6, 10, 8, 15, 15, 3, 15, 5, 15, 15, 15, 15,
    3, 15, 5, 5, 5, 8, 5, 10, 5, 10, 8, 13, 15, 12, 3, 3,
    # subset 2
    15, 8, 8, 3, 15, 15, 3, 8, 15, 15, 15, 15, 15, 15, 15, 8,
    15, 8, 15, 3, 15, 8, 15, 8, 3, 15, 6, 10, 15, 15, 10, 8,
    15, 3, 15, 10, 10, 8, 9, 10, 6, 15, 8, 15, 3, 6, 6, 8,
    15, 3, 15, 15, 15, 15, 15, 15, 15, 15, 15, 15, 3, 15, 15, 8,
], dtype=np.uint8).reshape(2, 64)

# 索引位宽 -> 插值因子表（2/3/4 位索引）
_BPTC_FACTORS = np.array([
    [0, 21, 43, 64, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    [0, 9, 18, 27, 37, 46, 55, 64, 0, 0, 0, 0, 0, 0, 0, 0],
    [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64],
], dtype=np.uint8)

# BC7 mode 信息：(子集数, 分区位, rotation位, 索引选择位, 颜色位, alpha位, 端点P位, 共享P位, 索引位, 副索引位)
_BC7_MODE_INFO = (
    (3, 4, 0, 0, 4, 0, 1, 0, 3, 0),  # mode 0
    (2, 6, 0, 0, 6, 0, 0, 1, 3, 0),  # mode 1
    (3, 6, 0, 0, 5, 0, 0, 0, 2, 0),  # mode 2
    (2, 6, 0, 0, 7, 0, 1, 0, 2, 0),  # mode 3
    (1, 0, 2, 1, 5, 6, 0, 0, 2, 3),  # mode 4
    (1, 0, 2, 0, 7, 8, 0, 0, 2, 2),  # mode 5
    (1, 0, 0, 0, 7, 7, 1, 0, 4, 0),  # mode 6
    (2, 6, 0, 0, 5, 5, 1, 0, 2, 0),  # mode 7
)

# DDS 头定义（DX9）
_DDS_MAGIC = b"DDS "
_DDPF_ALPHAPIXELS = 0x1
_DDPF_ALPHA = 0x2
_DDPF_FOURCC = 0x4
_DDPF_RGB = 0x40
_DDPF_LUMINANCE = 0x20000
_DDS_CAPS2_CUBEMAP = 0xFE00
_DDS_CAPS2_VOLUME = 0x200000

# FOURCC -> 解码种类
_FOURCC_KIND = {
    b"DXT1": "bc1",
    b"DXT2": "bc2",
    b"DXT3": "bc2",
    b"DXT4": "bc3",
    b"DXT5": "bc3",
    b"ATI1": "bc4",
    b"BC4U": "bc4",
    b"ATI2": "bc5",
    b"BC5U": "bc5",
    b"BC7 ": "bc7",
    b"BC6H": "bc6h",
    b"DX10": None,  # 单独处理
}

# DX10(DXGI) 格式号 -> 解码种类（只收录常见格式；少数未收录的返回 None）
_DXGI_KIND = {
    27: "rgba32", 28: "rgba32", 29: "rgba32", 30: "rgba32",
    60: "l8", 61: "l8",
    65: "a8",
    71: "bc1", 72: "bc1",
    74: "bc2", 75: "bc2",
    77: "bc3", 78: "bc3",
    80: "bc4", 81: "bc4",
    84: "bc5", 85: "bc5",
    87: "bgra32", 88: "bgrx32", 91: "bgra32", 92: "bgrx32",
    95: "bc6h", 96: "bc6h",
    98: "bc7", 99: "bc7",
}

_BLOCK_BYTES = {"bc1": 8, "bc2": 16, "bc3": 16, "bc4": 8, "bc5": 16, "bc7": 16}

last_error = ""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _le32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def _bit_range_convert(values, frm, to):
    """bimg bitRangeConvert：低精度值 -> 高精度（带舍入），按位镜像复制的等价实现。"""
    x = values.astype(np.uint32) * ((1 << to) - 1) + ((1 << frm) - 1)
    return (x + (x >> frm)) >> frm


def _decode_565(colors):
    """(N,) uint32 c0/c1 -> (N,) uint8 各通道（5/6/5 位 -> 8 位，bitRangeConvert 舍入）。"""
    r = _bit_range_convert((colors >> 11) & 0x1F, 5, 8)
    g = _bit_range_convert((colors >> 5) & 0x3F, 6, 8)
    b = _bit_range_convert(colors & 0x1F, 5, 8)
    return r, g, b


def _expand_indices(u32_indices, bits, count=16):
    """(N,) uint32 索引区 -> (N,16) 索引（LSB 位流）。"""
    numeric = u32_indices.astype(np.uint64)[:, None]
    shifts = (np.arange(count, dtype=np.uint64) * bits)[None, :]
    return ((numeric >> shifts) & ((1 << bits) - 1)).astype(np.uint8)


# ---------------------------------------------------------------------------
# BC1（DXT1）
# ---------------------------------------------------------------------------
def _decode_bc1(blocks):
    """blocks: (N,8) -> (N,16,4) RGBA。"""
    n = blocks.shape[0]
    words = blocks.view(np.dtype("<u2")).reshape(n, 4)
    c0 = words[:, 0].astype(np.uint32)
    c1 = words[:, 1].astype(np.uint32)
    idx_u32 = words.view(np.dtype("<u4")).reshape(n, 2)[:, 1]

    r0, g0, b0 = _decode_565(c0)
    r1, g1, b1 = _decode_565(c1)

    opaque = c0 > c1
    r2 = np.where(opaque, (2 * r0 + r1) // 3, (r0 + r1) // 2)
    g2 = np.where(opaque, (2 * g0 + g1) // 3, (g0 + g1) // 2)
    b2 = np.where(opaque, (2 * b0 + b1) // 3, (b0 + b1) // 2)
    a2 = np.full(n, 255, np.uint32)
    r3 = np.where(opaque, (r0 + 2 * r1) // 3, 0)
    g3 = np.where(opaque, (g0 + 2 * g1) // 3, 0)
    b3 = np.where(opaque, (b0 + 2 * b1) // 3, 0)
    a3 = np.where(opaque, 255, 0)

    pal_r = np.stack([r0, r1, r2, r3], axis=1)
    pal_g = np.stack([g0, g1, g2, g3], axis=1)
    pal_b = np.stack([b0, b1, b2, b3], axis=1)
    pal_a = np.stack([np.full(n, 255, np.uint32), np.full(n, 255, np.uint32), a2, a3], axis=1)

    indices = _expand_indices(idx_u32, 2)
    rows = np.arange(n)[:, None]
    out = np.stack([pal_r[rows, indices], pal_g[rows, indices], pal_b[rows, indices],
                    pal_a[rows, indices]], axis=-1)
    return out.astype(np.uint8)


def _decode_bc23_color_words(color_words):
    """BC2/BC3 颜色字（(N,) uint64 = 块字节8..16）：恒用 4 色插值 -> (N,16,3)。"""
    n = color_words.shape[0]
    c0 = (color_words & 0xFFFF).astype(np.uint32)
    c1 = ((color_words >> 16) & 0xFFFF).astype(np.uint32)
    idx_u32 = color_words >> 32

    r0, g0, b0 = _decode_565(c0)
    r1, g1, b1 = _decode_565(c1)
    r2 = (2 * r0 + r1) // 3
    g2 = (2 * g0 + g1) // 3
    b2 = (2 * b0 + b1) // 3
    r3 = (r0 + 2 * r1) // 3
    g3 = (g0 + 2 * g1) // 3
    b3 = (b0 + 2 * b1) // 3

    pal_r = np.stack([r0, r1, r2, r3], axis=1)
    pal_g = np.stack([g0, g1, g2, g3], axis=1)
    pal_b = np.stack([b0, b1, b2, b3], axis=1)
    indices = _expand_indices(idx_u32, 2)
    rows = np.arange(n)[:, None]
    out = np.stack([pal_r[rows, indices], pal_g[rows, indices], pal_b[rows, indices]], axis=-1)
    return out.astype(np.uint8)


def _decode_bc2(blocks):
    n = blocks.shape[0]
    words = blocks.view(np.dtype("<u8")).reshape(n, 2)
    w_alpha = words[:, 0]
    nib = ((w_alpha >> (4 * np.arange(16, dtype=np.uint64))[None, :]) & 0xF)
    alpha = _bit_range_convert(nib.astype(np.uint32), 4, 8)
    rgb = _decode_bc23_color_words(words[:, 1])
    return np.concatenate([rgb, alpha[..., None]], axis=-1)


def _decode_bc3(blocks):
    n = blocks.shape[0]
    words = blocks.view(np.dtype("<u8")).reshape(n, 2)
    w_alpha = words[:, 0]
    a0 = (w_alpha & 0xFF).astype(np.uint32)
    a1 = ((w_alpha >> 8) & 0xFF).astype(np.uint32)
    idx48 = w_alpha >> 16
    alpha = _alpha45_values(a0, a1).astype(np.uint8)
    alpha = _gather3bpp(alpha, idx48)
    rgb = _decode_bc23_color_words(words[:, 1])
    return np.concatenate([rgb, alpha[..., None]], axis=-1)


def _alpha45_values(a0, a1):
    """BC3/BC4 alpha 值表：(N,) -> (N,8)。"""
    n = a0.shape[0]
    gt = a0 > a1
    vals = np.empty((n, 8), np.uint32)
    vals[:, 0] = a0
    vals[:, 1] = a1
    vals[:, 2] = np.where(gt, (6 * a0 + a1) // 7, (4 * a0 + a1) // 5)
    vals[:, 3] = np.where(gt, (5 * a0 + 2 * a1) // 7, (3 * a0 + 2 * a1) // 5)
    vals[:, 4] = np.where(gt, (4 * a0 + 3 * a1) // 7, (2 * a0 + 3 * a1) // 5)
    vals[:, 5] = np.where(gt, (3 * a0 + 4 * a1) // 7, (a0 + 4 * a1) // 5)
    vals[:, 6] = np.where(gt, (2 * a0 + 5 * a1) // 7, 0)
    vals[:, 7] = np.where(gt, (a0 + 6 * a1) // 7, 255)
    return vals


def _gather3bpp(vals, idx48):
    """(N,8) 值表 + (N,) 48 位 3bpp 索引 -> (N,16)。"""
    n = vals.shape[0]
    indices = _expand_indices(idx48, 3, count=16)
    rows = np.arange(n)[:, None]
    return vals[rows, indices].astype(np.uint8)


def _decode_bc4(blocks):
    """BC4：单通道 -> (R,0,0,255)。"""
    n = blocks.shape[0]
    w = blocks.view(np.dtype("<u8")).reshape(n, 1)[:, 0]
    a0 = (w & 0xFF).astype(np.uint32)
    a1 = ((w >> 8) & 0xFF).astype(np.uint32)
    vals = _gather3bpp(_alpha45_values(a0, a1), w >> 16)
    out = np.zeros((n, 16, 4), np.uint8)
    out[..., 0] = vals
    out[..., 3] = 255
    return out


def _decode_bc5(blocks):
    n = blocks.shape[0]
    words = blocks.view(np.dtype("<u8")).reshape(n, 2)
    w_r = words[:, 0]
    w_g = words[:, 1]
    r_vals = _gather3bpp(_alpha45_values((w_r & 0xFF).astype(np.uint32),
                                         ((w_r >> 8) & 0xFF).astype(np.uint32)), w_r >> 16)
    g_vals = _gather3bpp(_alpha45_values((w_g & 0xFF).astype(np.uint32),
                                         ((w_g >> 8) & 0xFF).astype(np.uint32)), w_g >> 16)
    out = np.zeros((n, 16, 4), np.uint8)
    out[..., 0] = r_vals
    out[..., 1] = g_vals
    out[..., 3] = 255
    return out


# ---------------------------------------------------------------------------
# BC7
# ---------------------------------------------------------------------------
def _get_bits(w0, w1, pos, count):
    """从 128 位块（w0=低 64 位, w1=高 64 位）提取位段。

    pos: 标量或 (K,) / (K,N) uint64 数组（绝对位位置，LSB 起）；
    count: 位数（标量或与 pos 同形状的数组，如锚点索引宽度 ib-1）。
    """
    loc = pos & np.uint64(63)
    high_word = (pos >> np.uint64(6)) & np.uint64(1)
    if high_word.ndim == 2:
        w0b = w0[:, None]
        w1b = w1[:, None]
    else:
        w0b = w0
        w1b = w1
    word = np.where(high_word == 1, w1b, w0b)
    count_u = np.asarray(count, dtype=np.uint64)
    mask = np.left_shift(np.uint64(1), count_u) - np.uint64(1)
    lo = word >> loc
    spill = (loc + count_u) > np.uint64(64)
    shift = np.where(spill, np.uint64(64) - loc, np.uint64(0))
    hi = np.left_shift(w1b, shift)
    hi = np.where(spill & (high_word == 0), hi, np.uint64(0))
    return (lo | hi) & mask


def _decode_bc7(blocks):
    """blocks: (N,16) -> (N,16,4) RGBA。"""
    n = blocks.shape[0]
    out = np.zeros((n, 16, 4), np.uint8)
    if n == 0:
        return out

    b0 = blocks[:, 0].astype(np.uint64)
    mode = np.zeros(n, np.uint64)
    for k in range(8):
        bit = (b0 >> np.uint64(k)) & np.uint64(1)
        mode = np.where((mode == np.uint64(k)) & (bit == 0), np.uint64(k + 1), mode)
    valid = mode < np.uint64(8)  # 全零低字节为保留块 -> 输出全黑

    for m in range(8):
        selected = valid & (mode == m)
        if not selected.any():
            continue
        out[selected] = _decode_bc7_mode(blocks[selected], m)
    return out


def _decode_bc7_mode(blocks, m):
    """解码指定 mode 的一组块。blocks: (M,16) -> (M,16,4)。"""
    ns, pb, rb, isb, cb, ab, epb, spb, ib, ib2 = _BC7_MODE_INFO[m]
    m_count = blocks.shape[0]
    words = blocks.view(np.dtype("<u8")).reshape(m_count, 2)
    w0 = words[:, 0]
    w1 = words[:, 1]

    pos = np.uint64(m + 1)  # m 个 0 + 1 个 1
    part = _get_bits(w0, w1, pos, pb)
    pos = pos + np.uint64(pb)
    rot = _get_bits(w0, w1, pos, rb)
    pos = pos + np.uint64(rb)
    isel = _get_bits(w0, w1, pos, isb)
    pos = pos + np.uint64(isb)

    ne = 2 * ns
    mode_pbits = epb if epb else spb
    ep0 = np.zeros((m_count, ns, 4), np.uint32)
    ep1 = np.zeros((m_count, ns, 4), np.uint32)
    # 参考实现位序：先全部 R，再全部 G，再全部 B（每通道内按子集、端点序）
    for ch in range(3):
        for s in range(ns):
            ep0[:, s, ch] = _get_bits(w0, w1, pos, cb) << mode_pbits
            pos = pos + np.uint64(cb)
            ep1[:, s, ch] = _get_bits(w0, w1, pos, cb) << mode_pbits
            pos = pos + np.uint64(cb)
    if ab:
        for s in range(ns):
            ep0[:, s, 3] = _get_bits(w0, w1, pos, ab) << mode_pbits
            pos = pos + np.uint64(ab)
            ep1[:, s, 3] = _get_bits(w0, w1, pos, ab) << mode_pbits
            pos = pos + np.uint64(ab)
    else:
        ep0[:, :, 3] = 255
        ep1[:, :, 3] = 255

    if mode_pbits:
        shared = spb > 0
        for s in range(ns):
            pda = _get_bits(w0, w1, pos, mode_pbits)
            pos = pos + np.uint64(mode_pbits)
            if shared:
                pdb = pda
            else:
                pdb = _get_bits(w0, w1, pos, mode_pbits)
                pos = pos + np.uint64(mode_pbits)
            ep0[:, s, :] |= pda[:, None]
            ep1[:, s, :] |= pdb[:, None]

    # 端点扩展
    cb_total = cb + mode_pbits
    for s in range(ns):
        for ch in range(3):
            ep0[:, s, ch] = _bit_range_convert(ep0[:, s, ch], cb_total, 8)
            ep1[:, s, ch] = _bit_range_convert(ep1[:, s, ch], cb_total, 8)
    if ab:
        ab_total = ab + mode_pbits
        for s in range(ns):
            ep0[:, s, 3] = _bit_range_convert(ep0[:, s, 3], ab_total, 8)
            ep1[:, s, 3] = _bit_range_convert(ep1[:, s, 3], ab_total, 8)

    base0 = pos
    px = np.arange(16, dtype=np.uint64)

    # 分区 & 锚点
    if ns == 1:
        sub = np.zeros((m_count, 16), np.uint8)
        anchor = np.zeros((m_count, 16), np.uint64)
    elif ns == 2:
        sub = ((_BPTC_P2[part][:, None] >> px[None, :]) & 1).astype(np.uint8)
        anchor = np.where(sub == 0, 0, _BPTC_A2[part][:, None])
    else:
        sub = ((_BPTC_P3[part][:, None] >> (2 * px)[None, :]) & 3).astype(np.uint8)
        sub_safe = np.maximum(sub, 1)
        anchor = np.where(sub > 0, _BPTC_A3[sub_safe - 1, part[:, None]], 0)

    # 主索引流（锚点像素少 1 位：宽度与起点都按锚点计算）
    widths0 = ib - (px[None, :] == anchor).astype(np.uint64)
    starts0 = base0 + np.cumsum(widths0, axis=1) - widths0
    idx0 = _get_bits(w0, w1, starts0, widths0)
    if ib2:
        widths1 = ib2 - (px[None, :] == anchor).astype(np.uint64)
        base1 = base0 + np.uint64(ns * (16 * ib - 1))
        starts1 = base1 + np.cumsum(widths1, axis=1) - widths1
        idx1 = _get_bits(w0, w1, starts1, widths1)
        f0 = _BPTC_FACTORS[ib - 2][idx0]
        f1 = _BPTC_FACTORS[ib2 - 2][idx1]
        fc = np.where(isel[:, None] == 1, f1, f0)
        fa = np.where(isel[:, None] == 1, f0, f1)
    else:
        f0 = _BPTC_FACTORS[ib - 2][idx0]
        fc = f0
        fa = f0

    # 按像素取端点
    sub_idx = sub.astype(np.int64)[..., None]
    rows = np.arange(m_count)
    e0 = np.take_along_axis(ep0, sub_idx, axis=1)
    e1 = np.take_along_axis(ep1, sub_idx, axis=1)

    rgb = ((e0[..., :3].astype(np.uint32) * (64 - fc[..., None])
            + e1[..., :3].astype(np.uint32) * fc[..., None] + 32) >> 6)
    alpha = ((e0[..., 3].astype(np.uint32) * (64 - fa)
              + e1[..., 3].astype(np.uint32) * fa + 32) >> 6)
    rgba = np.concatenate([rgb, alpha[..., None]], axis=-1).astype(np.uint8)

    # rotation（仅 mode 4/5 有效）
    r_orig = rgba[..., 0]
    g_orig = rgba[..., 1]
    b_orig = rgba[..., 2]
    a_orig = rgba[..., 3]
    rot1 = rot[:, None] == 1
    rot2 = rot[:, None] == 2
    rot3 = rot[:, None] == 3
    r = np.where(rot1, a_orig, r_orig)
    a = np.where(rot1, r_orig, a_orig)
    g = np.where(rot2, a, g_orig)
    a = np.where(rot2, g_orig, a)
    b = np.where(rot3, a, b_orig)
    a = np.where(rot3, b_orig, a)
    return np.stack([r, g, b, a], axis=-1).astype(np.uint8)


# ---------------------------------------------------------------------------
# 非压缩格式
# ---------------------------------------------------------------------------
def _decode_uncompressed(data, width, height, bitcount, rmask, gmask, bmask, amask,
                         dxgi_kind):
    """非压缩像素解码。data 为按行对齐展开后的紧凑像素数据。

    dxgi_kind 为 None 时按 DX9 位掩码提取；为固定 DX10 种类时按固定通道语义。
    """
    if dxgi_kind == "rgba32":
        raw = np.frombuffer(data, dtype=np.dtype("<u4")).astype(np.uint32)
        r = ((raw >> 0) & 0xFF).astype(np.uint8)
        g = ((raw >> 8) & 0xFF).astype(np.uint8)
        b = ((raw >> 16) & 0xFF).astype(np.uint8)
        a = ((raw >> 24) & 0xFF).astype(np.uint8)
        return np.stack([r, g, b, a], axis=-1).reshape(height, width, 4)

    if dxgi_kind in ("bgra32", "bgrx32"):
        raw = np.frombuffer(data, dtype=np.dtype("<u4")).astype(np.uint32)
        b = ((raw >> 0) & 0xFF).astype(np.uint8)
        g = ((raw >> 8) & 0xFF).astype(np.uint8)
        r = ((raw >> 16) & 0xFF).astype(np.uint8)
        if dxgi_kind == "bgra32":
            a = ((raw >> 24) & 0xFF).astype(np.uint8)
        else:  # bgrx32：X 通道无意义，忽略并置不透明
            a = np.full(raw.shape, 255, np.uint8)
        return np.stack([r, g, b, a], axis=-1).reshape(height, width, 4)

    if dxgi_kind in ("l8", "a8"):
        arr = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
        if dxgi_kind == "l8":
            out = np.stack([arr, arr, arr, np.full_like(arr, 255)], axis=-1)
        else:
            out = np.stack([np.full_like(arr, 255), np.full_like(arr, 255),
                            np.full_like(arr, 255), arr], axis=-1)
        return out

    # DX9 位掩码通用路径
    if bitcount == 24:
        arr = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        rgb = arr[:, [2, 1, 0]].reshape(height, width, 3)  # D3DFMT_R8G8B8 内存序 B,G,R
        return np.concatenate([rgb, np.full((height, width, 1), 255, np.uint8)], axis=-1)

    if bitcount == 32:
        raw = np.frombuffer(data, dtype=np.dtype("<u4")).astype(np.uint32)
    elif bitcount == 16:
        raw = np.frombuffer(data, dtype=np.dtype("<u2")).astype(np.uint32)
    elif bitcount == 8:
        arr = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
        if amask:
            out = np.stack([np.full_like(arr, 255), np.full_like(arr, 255),
                            np.full_like(arr, 255), arr], axis=-1)
        else:
            out = np.stack([arr, arr, arr, np.full_like(arr, 255)], axis=-1)
        return out
    else:
        return None

    def extract(mask, default):
        if not mask:
            return np.full(raw.shape, default, np.uint8)
        shift = (mask & -mask).bit_length() - 1
        bits = mask.bit_count()
        vals = (raw >> shift) & ((1 << bits) - 1)
        if bits < 8:
            vals = _bit_range_convert(vals, bits, 8)
        elif bits > 8:
            vals = vals >> (bits - 8)
        return vals.astype(np.uint8)

    r = extract(rmask, 0)
    g = extract(gmask, 0)
    b = extract(bmask, 0)
    a = extract(amask, 255)
    return np.stack([r, g, b, a], axis=-1).reshape(height, width, 4)


# ---------------------------------------------------------------------------
# DDS 头解析与统一入口
# ---------------------------------------------------------------------------
def parse_dds_header(data):
    """解析 DDS 头；返回 dict(width,height,kind,bitcount,mask...,bc_blocks) 或 None。"""
    global last_error
    if data is None or len(data) < 128 or bytes(data[:4]) != _DDS_MAGIC:
        last_error = "不是有效的 DDS 文件（头部缺失/魔数不符）"
        return None
    try:
        size = _le32(data, 4)
        height = _le32(data, 12)
        width = _le32(data, 16)
        pitch = _le32(data, 20)
        depth = _le32(data, 24)
        pf_flags = _le32(data, 80)
        fourcc = bytes(data[84:88])
        bitcount = _le32(data, 88)
        rmask = _le32(data, 92)
        gmask = _le32(data, 96)
        bmask = _le32(data, 100)
        amask = _le32(data, 104)
        caps2 = _le32(data, 112)
    except (struct.error, IndexError):
        last_error = "DDS 头部读取失败"
        return None

    if width == 0 or height == 0 or width > 65536 or height > 65536:
        last_error = f"无效尺寸 {width}x{height}"
        return None
    if caps2 & _DDS_CAPS2_VOLUME or depth > 1:
        last_error = "不支持 3D 体积贴图"
        return None
    if caps2 & _DDS_CAPS2_CUBEMAP:
        last_error = "不支持立方体贴图"
        return None

    kind = None
    if pf_flags & _DDPF_FOURCC:
        if fourcc == b"DX10":
            if len(data) < 148:
                last_error = "DX10 头不完整"
                return None
            dxgi = _le32(data, 128)
            array_size = _le32(data, 140)
            if array_size > 1:
                last_error = "不支持数组/体积 DX10 贴图"
                return None
            kind = _DXGI_KIND.get(dxgi)
            if kind is None:
                last_error = f"不支持的 DXGI 格式 {dxgi}"
                return None
        else:
            kind = _FOURCC_KIND.get(fourcc)
            if kind is None:
                last_error = f"不支持的 FOURCC {fourcc}"
                return None
            if kind == "bc6h":
                last_error = "BC6H（HDR）暂不支持像素解码"
                return None
    elif pf_flags & (_DDPF_RGB | _DDPF_LUMINANCE | _DDPF_ALPHA | _DDPF_ALPHAPIXELS):
        if bitcount not in (8, 16, 24, 32):
            last_error = f"不支持的非压缩位深 {bitcount}"
            return None
        kind = "uncompressed"
    else:
        last_error = "无法识别 DDS 像素格式"
        return None

    return {
        "width": width,
        "height": height,
        "pitch": pitch,
        "bitcount": bitcount,
        "rmask": rmask,
        "gmask": gmask,
        "bmask": bmask,
        "amask": amask,
        "kind": kind,
        "payload_offset": 148 if fourcc == b"DX10" else 128,
    }


def dds_dimensions(data):
    """返回 (width, height) 或 None（只读头部，data 可仅含头部 160 字节）。"""
    header = parse_dds_header(bytes(data[:160]) if not isinstance(data, bytes) else data[:160])
    if header is None:
        return None
    return header["width"], header["height"]


def decode_dds_rgba8(data):
    """把完整 DDS 字节解码为 (height, width, 4) uint8 RGBA；失败返回 None（last_error 说明原因）。"""
    global last_error
    header = parse_dds_header(data)
    if header is None:
        return None
    width, height = header["width"], header["height"]
    kind = header["kind"]

    try:
        if kind in ("uncompressed", "rgba32", "bgra32", "bgrx32", "l8", "a8"):
            bitcount = header["bitcount"]
            if kind == "uncompressed":
                bytes_per_pixel = bitcount // 8
            elif kind in ("rgba32", "bgra32", "bgrx32"):
                bytes_per_pixel = 4
            else:
                bytes_per_pixel = 1
            row_bytes = width * bytes_per_pixel
            pitch = header["pitch"] or row_bytes
            if pitch < row_bytes:
                pitch = row_bytes
            payload = data[header["payload_offset"]:]
            if len(payload) < pitch * height:
                last_error = "非压缩数据长度不足"
                return None
            rows = [payload[r * pitch:r * pitch + row_bytes] for r in range(height)]
            packed = b"".join(rows)
            dxgi_kind = None if kind == "uncompressed" else kind
            return _decode_uncompressed(packed, width, height, bitcount,
                                        header["rmask"], header["gmask"],
                                        header["bmask"], header["amask"], dxgi_kind)

        block_bytes = _BLOCK_BYTES.get(kind)
        if block_bytes is None:
            last_error = f"不支持的格式种类 {kind}"
            return None
        grid_w = (width + 3) // 4
        grid_h = (height + 3) // 4
        need = grid_w * grid_h * block_bytes
        payload = data[header["payload_offset"]:]
        if len(payload) < need:
            last_error = "压缩数据长度不足"
            return None
        blocks = np.frombuffer(payload, dtype=np.uint8, count=need).reshape(-1, block_bytes)
        if kind == "bc1":
            pixels = _decode_bc1(blocks)
        elif kind == "bc2":
            pixels = _decode_bc2(blocks)
        elif kind == "bc3":
            pixels = _decode_bc3(blocks)
        elif kind == "bc4":
            pixels = _decode_bc4(blocks)
        elif kind == "bc5":
            pixels = _decode_bc5(blocks)
        elif kind == "bc7":
            pixels = _decode_bc7(blocks)
        else:
            last_error = f"不支持的格式种类 {kind}"
            return None

        # 块网格 -> 像素图，裁剪到实际尺寸
        img = pixels.reshape(grid_h, grid_w, 4, 4, 4)
        img = img.transpose(0, 2, 1, 3, 4).reshape(grid_h * 4, grid_w * 4, 4)
        return np.ascontiguousarray(img[:height, :width])
    except Exception as exc:  # 防御：任何解码异常都按"无法解码"处理
        last_error = f"解码失败: {exc}"
        return None


def dds_files_pixels_equal(path_a, path_b):
    """解码两个 DDS 文件并做像素级完全比较。

    返回 (equal, reason)：equal=True 表示解码成功且像素完全一致。
    """
    global last_error
    try:
        with open(path_a, "rb") as f:
            data_a = f.read()
        with open(path_b, "rb") as f:
            data_b = f.read()
    except OSError as exc:
        last_error = f"读取文件失败: {exc}"
        return False, last_error
    px_a = decode_dds_rgba8(data_a)
    if px_a is None:
        return False, f"{os.path.basename(path_a)}: {last_error}"
    px_b = decode_dds_rgba8(data_b)
    if px_b is None:
        return False, f"{os.path.basename(path_b)}: {last_error}"
    if px_a.shape != px_b.shape:
        last_error = "尺寸不一致"
        return False, last_error
    return bool(np.array_equal(px_a, px_b)), ""