#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add_character.py —— 给一个新角色推导出血量检测（HP bar）需要的全部数据。

输入是 3DMigoto 的一帧 FrameAnalysis（首选）或一段 d3d11_log.txt（次选），
输出是「这一帧里队伍 UI 的三个槽位」以及可直接追加进 characters_source.ini 的条目。
不需要人工填任何值：所有哈希、index_count、宽高/格式/mips 都从日志和 DDS 头里读。

槽位是怎么算出来的（配对依据，读代码先读这一段）
------------------------------------------------------------------
参考实现（ZZMI ``Libraries\\HP bar``）认角色靠两件事：
  1) 图标 PS 命中时，ps-t0 上绑的那张「角色图标贴图」由 77 条 TextureOverride 映射成 id；
  2) 血条 PS 命中时，按 ps-t0 的运行时签名（index_count 6/54 + 1024x1024 或 1024x512 +
     BC3_UNORM_SRGB + 1 mip）认血条（hp.ini:164-168）。
游戏对每个队员各画一次图标、各画一次血条；``OrderIds.CS.hlsl:51-60`` 与
``SummHp.CS.hlsl:61-70`` 各自按「该 draw 首顶点的屏幕 X」排序，再按下标同序配对。
本脚本没有去解码顶点缓冲（那要读 vb 的 first 顶点 float0，格式随 UI 版本变），
改为 **按 draw 序号配对**：日志里 draw 的先后就是 UI 的绘制顺序，
第 N 个图标 draw 配第 N 个血条 draw，编号 N 就是槽位号（左到右）。
两个数量对不上时会明确打印「槽 N 没配上血条」，不静默。

哪些 draw 算「角色图标 draw」（这是唯一一处推断，写在 --prove 的输出里）
------------------------------------------------------------------
  * VS == 图标 VS 且 PS == 图标 PS；
  * ps-t0 有哈希；
  * 该哈希在角色表里 → 一定是角色图标（表是权威）；
  * 不在表里 → 只有当「index_count == 6（一个四边形，整张贴图铺满）」且
    「ps-t0 的贴图是 BC* 压缩格式」时才收；并且排除被血条 draw 用作 ps-t0 的贴图
    （那是血条底图，不是角色图标）。
  最多收 3 个（参考实现 ``counter >= 3`` 就丢，队伍上限 3 人）。

用法
------------------------------------------------------------------
  python tools/add_character.py --frame-analysis "K:\\...\\FrameAnalysis-2026-10-23-170927" --list
  python tools/add_character.py --frame-analysis "K:\\...\\FrameAnalysis-2026-10-23-170927" --prove
  python tools/add_character.py --frame-analysis "K:\\...\\FrameAnalysis-2026-10-23-170927" \\
         --add --name 测试角色 --slot 1
  python tools/add_character.py --frame-analysis "K:\\...\\FrameAnalysis-2026-10-23-170927" \\
         --add --name 测试角色 --slot 1 --apply --ini .\\_demo\\characters_source.ini

退出码：0 正常；2 用法/输入错误；3 拒绝写（哈希已在表里 / id 冲突）；4 解析失败（缺哈希/缺 draw）。

纯标准库，Python 3.11。
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 与 blueprint/node_postprocess_health.py:43-46 的 DEFAULT_* 一致；游戏更新后这里也会对不上，
# 脚本会在输出里明确报警（而不是假装没事）。
DEFAULT_ICON_VS = "cdc90aee00e7900d"
DEFAULT_ICON_PS = "857cd16250c6142e"
DEFAULT_BAR_VS = "cdc7f730110fde36"
DEFAULT_BAR_PS = "bc8c64835ea6beed"

TEAM_SLOTS = 3                      # 参考实现 OrderIds/SummHp: if (counter >= 3) return;
BAR_INDEX_COUNTS = (6, 54)          # hp.ini:164
BAR_TEXTURE_SIZES = ((1024, 1024), (1024, 512))
BAR_FORMAT = "BC3_UNORM_SRGB"

# 与 blueprint/node_postprocess_health.py:53-55 逐字相同的三条正则。
# --apply 之后用同一套正则再解析一遍，保证节点仍然认得这份表。
_SECTION_RE = re.compile(r"^\s*\[TextureOverride\w*\s+(.+?)\]\s*$", re.MULTILINE)
_HASH_RE = re.compile(r"^\s*hash\s*=\s*([0-9A-Fa-f]+)\s*$", re.MULTILINE)
_CHARACTER_ID_RE = re.compile(r"^\s*\$character_id\s*=\s*(\d+)\s*$", re.MULTILINE)

_TOOLSET_INI = "Toolset/hp_detect/characters_source.ini"

# 3DMigoto FrameAnalysis 日志的每一行：前 6 位是 draw 序号
_PREFIX_RE = re.compile(r"^(\d{6}) (.*)$")
_DUMP_RE = re.compile(r"^3DMigoto Dumping \w+ (.+?) -> (.+)$")
_SRC_RE = re.compile(
    r"^(\d{6})-(.+?)=([0-9a-fA-F]+)-vs=([0-9a-fA-F]+)-ps=([0-9a-fA-F]+)\.\w+$"
)
_BIND_ENTRY_RE = re.compile(r"^\s+(\d+):\s+.*?hash=([0-9a-fA-F]{8})\s*$")
_IB_COUNT_RE = re.compile(r"-count=(\d+)")
_DDS_NAME_RE = re.compile(r"^([0-9a-fA-F]{8})-([A-Za-z0-9_]+)\.dds$")

_FOURCC = {
    b"DXT1": "BC1_UNORM",
    b"DXT3": "BC2_UNORM",
    b"DXT5": "BC3_UNORM",
    b"ATI1": "BC4_UNORM",
    b"BC5U": "BC5_UNORM",
}
_DXGI = {
    10: "R16G16B16A16_FLOAT",
    28: "R8G8B8A8_UNORM",
    29: "R8G8B8A8_UNORM_SRGB",
    54: "R16_UNORM",
    65: "R16_FLOAT",
    71: "BC1_UNORM",
    72: "BC1_UNORM_SRGB",
    74: "BC2_UNORM",
    75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM",
    78: "BC3_UNORM_SRGB",
    80: "BC4_UNORM",
    81: "BC4_SNORM",
    83: "BC5_UNORM",
    84: "BC5_SNORM",
    95: "BC6H_UF16",
    96: "BC6H_SF16",
    98: "BC7_UNORM",
    99: "BC7_UNORM_SRGB",
    87: "B8G8R8A8_UNORM",
}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def out(text: str = "") -> None:
    """打印。控制台编码不是 UTF-8 时退化成可编码字符，绝不让一个 ✅ 把脚本打死。"""
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(text.encode(enc, "replace").decode(enc, "replace"))


def die(message: str, code: int = 4) -> "None":
    out("")
    out("✗ " + message)
    raise SystemExit(code)


def ok(flag: bool) -> str:
    return "✅" if flag else "❌"


def short_path(path: str) -> str:
    return os.path.normpath(path) if path else "(无)"


# ---------------------------------------------------------------------------
# 角色表
# ---------------------------------------------------------------------------

@dataclass
class TableEntry:
    name: str
    hash: str
    character_id: int


def parse_table(text: str) -> list[TableEntry]:
    """与节点同一套正则解析角色表（见 blueprint/node_postprocess_health.py:63-79）。"""
    entries: list[TableEntry] = []
    headers = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(headers):
        block_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[match.end():block_end]
        hash_match = _HASH_RE.search(block)
        id_match = _CHARACTER_ID_RE.search(block)
        if not hash_match or not id_match:
            continue
        entries.append(
            TableEntry(
                name=match.group(1).strip(),
                hash=hash_match.group(1).lower(),
                character_id=int(id_match.group(1)),
            )
        )
    return entries


def load_table(path: Path) -> tuple[list[TableEntry], bytes, str]:
    """读角色表，返回 (条目, 原始字节, 解码后的文本)。"""
    if not path.is_file():
        out(f"⚠ 角色表不存在：{path}（本次所有哈希都会判「表内无」）")
        return [], b"", ""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    return parse_table(text), raw, text


def default_ini_path() -> Path:
    # 本文件在 <仓库>/tools/ 下，角色表在 <仓库>/Toolset/hp_detect/
    return Path(__file__).resolve().parents[1] / _TOOLSET_INI


# ---------------------------------------------------------------------------
# 日志解析
# ---------------------------------------------------------------------------

@dataclass
class Draw:
    id: int
    line: int
    vs: str | None = None
    ps: str | None = None
    ps_slots: dict[int, str] = field(default_factory=dict)
    ps_t0_bound: str | None = None
    ps_t0_line: int = 0
    vb0_hash: str | None = None
    ib_hash: str | None = None
    index_count: int | None = None
    start_index: int | None = None
    base_vertex: int | None = None
    is_indexed: bool = False
    drawn: bool = False
    # key -> {"hash": str, "dst": str, "src": str, "line": int}
    resources: dict[str, dict] = field(default_factory=dict)

    @property
    def ps_t0_dumped(self) -> str | None:
        res = self.resources.get("ps-t0")
        return res["hash"] if res else None

    @property
    def ps_t0(self) -> str | None:
        """ps-t0 的实际绑定：优先用落盘行里 3DMigoto 自己算出的哈希，其次用 PSSetShaderResources。"""
        return self.ps_t0_dumped or self.ps_t0_bound


def parse_log(log_path: Path) -> dict[int, Draw]:
    """一次线性扫描。

    D3D11 的状态是持久的：游戏不会每画一个 draw 就把 shader 重新 Set 一遍
    （实测 000547 就没有 VSSetShader/PSSetShader 行，它沿用 000546 绑的图标 shader）。
    所以 vs/ps/ps-t0 必须跨 draw 继承，否则会把真正的角色图标 draw 漏掉。
    """
    draws: dict[int, Draw] = {}
    current: Draw | None = None
    pending: tuple[str, int] | None = None  # ("ps", start_slot) / ("vb", -1)
    live_vs: str | None = None
    live_ps: str | None = None
    live_t0: str | None = None
    live_t0_line = 0

    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        for lineno, raw in enumerate(handle, 1):
            line = raw.rstrip("\r\n")
            head = _PREFIX_RE.match(line)
            if head:
                draw_id = int(head.group(1))
                body = head.group(2)
                draw = draws.get(draw_id)
                if draw is None:
                    draw = draws[draw_id] = Draw(
                        id=draw_id,
                        line=lineno,
                        vs=live_vs,
                        ps=live_ps,
                        ps_t0_bound=live_t0,
                        ps_t0_line=live_t0_line,
                    )
                current = draw

                if body.startswith("VSSetShader("):
                    found = re.search(r"hash=([0-9a-fA-F]{16})", body)
                    if found:
                        live_vs = found.group(1).lower()
                        draw.vs = live_vs
                    pending = None
                elif body.startswith("PSSetShader("):
                    found = re.search(r"hash=([0-9a-fA-F]{16})", body)
                    if found:
                        live_ps = found.group(1).lower()
                        draw.ps = live_ps
                    pending = None
                elif body.startswith("PSSetShaderResources("):
                    slot = re.search(r"StartSlot:(\d+)", body)
                    pending = ("ps", int(slot.group(1))) if slot else None
                elif body.startswith("IASetVertexBuffers("):
                    pending = ("vb", -1)
                elif body.startswith("IASetIndexBuffer("):
                    found = re.search(r"hash=([0-9a-fA-F]{8})", body)
                    if found:
                        draw.ib_hash = found.group(1).lower()
                    pending = None
                elif body.startswith("DrawIndexed("):
                    found = re.search(
                        r"IndexCount:(\d+),\s*StartIndexLocation:(-?\d+),\s*BaseVertexLocation:(-?\d+)",
                        body,
                    )
                    if found:
                        draw.index_count = int(found.group(1))
                        draw.start_index = int(found.group(2))
                        draw.base_vertex = int(found.group(3))
                    draw.is_indexed = True
                    draw.drawn = True
                    pending = None
                elif body.startswith("Draw("):
                    draw.drawn = True
                    pending = None
                else:
                    dump = _DUMP_RE.match(body)
                    if dump:
                        _record_dump(draw, dump.group(1), dump.group(2), lineno)
                    pending = None
                continue

            # 续行：`       0: view=0x... resource=0x... hash=f062e3b7`
            if current is not None and pending is not None:
                entry = _BIND_ENTRY_RE.match(line)
                if entry:
                    index = int(entry.group(1))
                    value = entry.group(2).lower()
                    kind, start = pending
                    if kind == "ps" and index == start:
                        current.ps_slots[index] = value
                        if index == 0:
                            live_t0 = value
                            live_t0_line = lineno
                            current.ps_t0_bound = value
                            current.ps_t0_line = lineno
                    elif kind == "vb" and index == 0:
                        current.vb0_hash = value

    return draws


def _record_dump(draw: Draw, src: str, dst: str, lineno: int) -> None:
    name = os.path.basename(src.replace("/", "\\"))
    match = _SRC_RE.match(name)
    if not match:
        return
    key = match.group(2).lower()
    # 落盘名里的 vs=/ps= 是 3DMigoto 当时实际绑的值，可当兜底
    if draw.vs is None:
        draw.vs = match.group(4).lower()
    if draw.ps is None:
        draw.ps = match.group(5).lower()
    draw.resources[key] = {
        "hash": match.group(3).lower(),
        "dst": dst,
        "src": src,
        "line": lineno,
    }
    # 兜底：没有 DrawIndexed 行时（--log 片段常常没有），用 ib 落盘名里的 count=
    if key == "ib" and draw.index_count is None:
        counted = _IB_COUNT_RE.search(os.path.basename(dst))
        if counted:
            draw.index_count = int(counted.group(1))


# ---------------------------------------------------------------------------
# DDS
# ---------------------------------------------------------------------------

@dataclass
class DdsInfo:
    path: str | None = None
    width: int | None = None
    height: int | None = None
    mips: int | None = None
    fmt: str | None = None

    def size_text(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "未知"

    def text(self) -> str:
        return (
            f"{self.size_text()} {self.fmt or '格式未知'} "
            f"mips={self.mips if self.mips is not None else '未知'}"
        )


def read_dds_header(path: str) -> tuple[int, int, int, str | None] | None:
    """读 DDS 头：宽在偏移 16、高在 12、mip 数在 28（u32 小端）。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(148)
    except OSError:
        return None
    if len(data) < 128 or data[0:4] != b"DDS ":
        return None
    height = int.from_bytes(data[12:16], "little")
    width = int.from_bytes(data[16:20], "little")
    flags = int.from_bytes(data[8:12], "little")
    mips = int.from_bytes(data[28:32], "little")
    if not (flags & 0x20000) or mips == 0:
        mips = 1
    fourcc = data[84:88]
    fmt = _FOURCC.get(fourcc)
    if fourcc == b"DX10" and len(data) >= 148:
        dxgi = int.from_bytes(data[128:132], "little")
        fmt = _DXGI.get(dxgi, f"DXGI_{dxgi}")
    return width, height, mips, fmt


def dds_info(resource: dict | None, search_dirs: list[Path]) -> DdsInfo:
    info = DdsInfo()
    if not resource:
        return info

    dst = resource.get("dst") or ""
    if dst:
        named = _DDS_NAME_RE.match(os.path.basename(dst))
        if named:
            info.fmt = named.group(2).upper()

    path: str | None = None
    if dst and os.path.isfile(dst):
        path = dst
    else:
        base = os.path.basename(dst) if dst else ""
        for folder in search_dirs:
            for sub in ("deduped", ""):
                base_dir = os.path.join(str(folder), sub) if sub else str(folder)
                candidate = os.path.join(base_dir, base) if base else ""
                if candidate and os.path.isfile(candidate):
                    path = candidate
                    break
            if path:
                break
    if path is None:
        texture_hash = resource.get("hash") or ""
        for folder in search_dirs:
            for sub in ("deduped", ""):
                base_dir = os.path.join(str(folder), sub) if sub else str(folder)
                for pattern in (f"{texture_hash}-*.dds", f"*-{texture_hash}-*.dds"):
                    hits = sorted(glob.glob(os.path.join(base_dir, pattern)))
                    if hits:
                        path = hits[0]
                        break
                if path:
                    break
            if path:
                break

    info.path = path
    if path:
        header = read_dds_header(path)
        if header:
            info.width, info.height, info.mips, header_fmt = header
            if header_fmt:
                info.fmt = header_fmt
    return info


# ---------------------------------------------------------------------------
# 槽位
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    index: int
    icon_draw: Draw
    icon_hash: str
    icon_dds: DdsInfo
    table_entry: TableEntry | None
    bar_draw: Draw | None
    bar_dds: DdsInfo
    reason: str
    confidence: str = ""
    usable: bool = False
    signature_ok: bool = False
    signature_detail: str = ""
    reject_reason: str = ""

    @property
    def status_text(self) -> str:
        if self.usable:
            return "✅可新增"
        return f"⚠候选（未通过: {self.reject_reason}）"

    def bar_signature(self) -> tuple[bool, str]:
        """参考实现 hp.ini:164-168 的血条贴图签名。"""
        if self.bar_draw is None:
            return False, "这帧没有配到血条 draw"
        d = self.bar_dds
        checks = []
        if self.bar_draw.index_count is None:
            checks.append("index_count 未知")
        else:
            checks.append(
                f"index_count={self.bar_draw.index_count}"
                + ("" if self.bar_draw.index_count in BAR_INDEX_COUNTS else "(不是 6/54)")
            )
        size_ok = (d.width, d.height) in BAR_TEXTURE_SIZES
        checks.append(
            f"尺寸={d.size_text()}" + ("" if size_ok else f"(不是 {BAR_TEXTURE_SIZES})")
        )
        fmt_ok = (d.fmt or "").upper() == BAR_FORMAT
        checks.append(f"格式={d.fmt or '未知'}" + ("" if fmt_ok else f"(不是 {BAR_FORMAT})"))
        mips_ok = d.mips == 1
        checks.append(f"mips={d.mips if d.mips is not None else '未知'}" + ("" if mips_ok else "(不是 1)"))
        passed = (
            self.bar_draw.index_count in BAR_INDEX_COUNTS
            and size_ok
            and fmt_ok
            and mips_ok
        )
        return passed, "，".join(checks)


def _is_bc(fmt: str | None) -> bool:
    return bool(fmt) and fmt.upper().startswith("BC")


def build_slots(
    draws: dict[int, Draw],
    table: list[TableEntry],
    icon_vs: str,
    icon_ps: str,
    bar_vs: str,
    bar_ps: str,
    search_dirs: list[Path],
) -> tuple[list[Slot], list[Draw], list[Draw], list[tuple[Draw, str]]]:
    ordered = [draws[key] for key in sorted(draws)]
    icon_draws = [d for d in ordered if d.drawn and d.vs == icon_vs and d.ps == icon_ps]
    bar_draws = [d for d in ordered if d.drawn and d.vs == bar_vs and d.ps == bar_ps]

    bar_textures = {d.ps_t0 for d in bar_draws if d.ps_t0}
    by_hash = {entry.hash: entry for entry in table}

    accepted: list[tuple[Draw, str, DdsInfo, TableEntry | None, str]] = []
    rejected: list[tuple[Draw, str]] = []

    for draw in icon_draws:
        texture_hash = draw.ps_t0
        if not texture_hash:
            rejected.append((draw, "没有 ps-t0 绑定（PSSetShaderResources 与落盘行都没有）"))
            continue
        info = dds_info(draw.resources.get("ps-t0"), search_dirs)
        entry = by_hash.get(texture_hash)
        if entry is not None:
            accepted.append((draw, texture_hash, info, entry, "表内哈希（权威）"))
            continue
        if texture_hash in bar_textures:
            owners = [d.id for d in bar_draws if d.ps_t0 == texture_hash]
            rejected.append(
                (draw, f"ps-t0={texture_hash} 是血条底图（血条 draw #{owners[0]} 也用它）")
            )
            continue
        if draw.index_count != 6:
            rejected.append(
                (draw, f"ps-t0={texture_hash} 不是角色图标：index_count={draw.index_count}（只收 6）")
            )
            continue
        if info.fmt is not None and not _is_bc(info.fmt):
            rejected.append(
                (draw, f"ps-t0={texture_hash} 不是角色图标：格式={info.fmt}（只收 BC*）")
            )
            continue
        accepted.append(
            (draw, texture_hash, info, None, "结构推断：单四边形(index_count=6) + BC 压缩格式")
        )

    slots: list[Slot] = []
    for position, (draw, texture_hash, info, entry, reason) in enumerate(accepted[:TEAM_SLOTS], 1):
        bar_draw = bar_draws[position - 1] if position - 1 < len(bar_draws) else None
        bar_info = dds_info(bar_draw.resources.get("ps-t0"), search_dirs) if bar_draw else DdsInfo()
        slots.append(
            Slot(
                index=position,
                icon_draw=draw,
                icon_hash=texture_hash,
                icon_dds=info,
                table_entry=entry,
                bar_draw=bar_draw,
                bar_dds=bar_info,
                reason=reason,
            )
        )

    frame_has_table_hit = any(slot.table_entry is not None for slot in slots)
    for slot in slots:
        signature_ok, detail = slot.bar_signature()
        slot.signature_ok = signature_ok
        slot.signature_detail = detail
        if slot.bar_draw is None:
            slot.reject_reason = "没配上血条"
        elif not signature_ok:
            slot.reject_reason = f"血条签名不符（{detail}）"
        slot.usable = signature_ok

        if slot.usable:
            if slot.table_entry is not None:
                slot.confidence = "高（表内已有）"
            elif frame_has_table_hit:
                slot.confidence = "高（血条签名通过 + 本帧另有表内角色命中）"
            else:
                slot.confidence = "中（血条签名通过，但本帧没有表内角色命中作对照）"
        else:
            slot.confidence = f"低（{slot.reject_reason}）"

    return slots, icon_draws, bar_draws, rejected


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def shader_hits(draws: dict[int, Draw], value: str, side: str) -> int:
    return sum(1 for draw in draws.values() if (draw.vs if side == "vs" else draw.ps) == value)


def print_shader_line(label: str, value: str, hits: int, expected: str) -> None:
    if hits == 0:
        out(f"  {label}: {value}  ❌ 整帧里没有这个哈希（游戏更新了？）")
        return
    if value != expected:
        out(f"  {label}: {value}  ⚠ 与节点默认 {expected} 不一致  出现 {hits} 个 draw")
        return
    out(f"  {label}: {value}  ✅ 与节点默认一致  出现 {hits} 个 draw")


def cmd_list(source_text: str, draws, slots, icon_draws, bar_draws, rejected, args) -> int:
    out("=" * 78)
    out(f"输入: {source_text}")
    out(f"解析: {len(draws)} 个 draw")
    out("4 个 shader 哈希（脚本按它们挑 draw；命中数 = 整帧里绑过该 shader 的 draw 数）:")
    print_shader_line("图标 VS", args.icon_vs, shader_hits(draws, args.icon_vs, "vs"), DEFAULT_ICON_VS)
    print_shader_line("图标 PS", args.icon_ps, shader_hits(draws, args.icon_ps, "ps"), DEFAULT_ICON_PS)
    print_shader_line("血条 VS", args.bar_vs, shader_hits(draws, args.bar_vs, "vs"), DEFAULT_BAR_VS)
    print_shader_line("血条 PS", args.bar_ps, shader_hits(draws, args.bar_ps, "ps"), DEFAULT_BAR_PS)
    out(f"图标 draw（VS+PS 同时命中）: {len(icon_draws)} 个；血条 draw: {len(bar_draws)} 个")
    out("=" * 78)
    usable = [slot for slot in slots if slot.usable]
    blocked = [slot for slot in slots if not slot.usable]
    out(
        f"队伍槽位: {len(slots)} 个（✅可新增 {len(usable)} 个，⚠候选被拒 {len(blocked)} 个）"
    )
    out("")

    if not slots:
        out("✗ 一个槽位都没识别出来。")
        if not icon_draws:
            out("  卡在这一步：帧里没有「图标 VS + 图标 PS」同时命中的 draw。")
            out(f"  期望 VS={args.icon_vs} PS={args.icon_ps}；如果游戏更新了，用 --icon-vs/--icon-ps 指定。")
        else:
            out("  卡在这一步：图标 draw 有，但没有一个满足「表内哈希 / 单四边形+BC 格式」。")
            for draw, reason in rejected[:10]:
                out(f"    draw#{draw.id}: {reason}")
        return 4

    for slot in slots:
        entry_text = (
            f"id={slot.table_entry.character_id} {slot.table_entry.name} ✅"
            if slot.table_entry
            else "无"
        )
        out(
            f"槽{slot.index}  {slot.status_text}  图标哈希={slot.icon_hash}  draw#{slot.icon_draw.id}  "
            f"index_count={slot.icon_draw.index_count}"
        )
        out(f"      图标贴图: {slot.icon_dds.text()}  ({short_path(slot.icon_dds.path or '')})")
        out(f"      表内: {entry_text}")
        if slot.bar_draw is None:
            out(
                f"      配对血条: ❌ 没配上（本帧血条 draw 只有 {len(bar_draws)} 个，"
                f"槽位 {slot.index} 超出）"
            )
            out(f"      尺寸对照: 图标={slot.icon_dds.size_text()}  ↔  血条=(没有)")
        else:
            gap = slot.icon_draw.id - slot.bar_draw.id
            out(
                f"      配对血条: draw#{slot.bar_draw.id}  index_count={slot.bar_draw.index_count}  "
                f"贴图={slot.bar_draw.ps_t0}  {slot.bar_dds.text()}  签名={ok(slot.signature_ok)}"
            )
            out(f"      签名明细: {slot.signature_detail}")
            out(
                f"      尺寸对照: 图标={slot.icon_dds.size_text()}  ↔  血条={slot.bar_dds.size_text()}"
                f"（参考实现里两者本就不是一个量级，仅作展示）"
            )
            out(
                f"      帧内顺序: 血条 draw#{slot.bar_draw.id} 在前，图标 draw#{slot.icon_draw.id} 在后"
                f"（间隔 {gap} 个 draw）"
            )
        out(f"      依据: {slot.reason}")
        out("")

    extras = [item for item in rejected if item[0].index_count == 6]
    if extras:
        out(f"被排除的图标 draw（共 {len(rejected)} 个，下面只列 index_count=6 的 {len(extras)} 个）:")
        for draw, reason in extras[:10]:
            out(f"  draw#{draw.id}: {reason}")
        out("")

    out("-" * 78)
    if usable:
        labels = []
        for slot in usable:
            named = f"={slot.table_entry.name}" if slot.table_entry else ""
            labels.append(f"槽{slot.index}({slot.icon_hash}{named})")
        out(f"可用槽位: {len(usable)} 个 → " + "、".join(labels))
    else:
        out("可用槽位: 0 个（这一帧没有任何槽通过血条签名）")
    for slot in blocked:
        out(f"候选被拒: 槽{slot.index}(hash={slot.icon_hash}) → {slot.reject_reason}")

    todo = [slot for slot in usable if slot.table_entry is None]
    if todo:
        numbers = "、".join(str(slot.index) for slot in todo)
        out(f"提示: 槽 {numbers} 是可新增的新角色 → --add --name <中文名> --slot <槽号>")
    else:
        out(
            "本帧没有「可新增」的槽：要给新角色加表，重抓一帧——"
            "抓帧时必须该角色在队伍 UI 里、且队伍血条可见（参考实现只在血条 draw 命中时才采样）。"
        )
    return 0


def cmd_prove(source_text: str, draws, slots, icon_draws, bar_draws, args) -> int:
    out("=" * 78)
    out("自证：从日志重新推导每个槽位，再拿 characters_source.ini 对表")
    out(f"输入: {source_text}    draw 总数={len(draws)}")
    out("-" * 78)
    out("推导链（每一条都能在日志/文件里复查）:")
    out(f"  1) 4 个 shader 哈希 → 从 VSSetShader/PSSetShader 的 hash= 回读：")
    print_shader_line("     图标 VS", args.icon_vs, shader_hits(draws, args.icon_vs, "vs"), DEFAULT_ICON_VS)
    print_shader_line("     图标 PS", args.icon_ps, shader_hits(draws, args.icon_ps, "ps"), DEFAULT_ICON_PS)
    print_shader_line("     血条 VS", args.bar_vs, shader_hits(draws, args.bar_vs, "vs"), DEFAULT_BAR_VS)
    print_shader_line("     血条 PS", args.bar_ps, shader_hits(draws, args.bar_ps, "ps"), DEFAULT_BAR_PS)
    out(f"     图标 draw={len(icon_draws)} 个；血条 draw={len(bar_draws)} 个")
    out("  2) 图标哈希 → 该 draw 的 PSSetShaderResources(StartSlot:0) 绑定行 / 落盘行的 ps-t0=")
    out("  3) index_count → DrawIndexed(IndexCount:)（缺失时退回 ib 落盘名里的 -count=）")
    out("  4) 宽高/格式/mips → deduped\\<hash>-<FORMAT>.dds 的 DDS 头（宽=偏移16 高=偏移12 mip=偏移28）")
    out("  5) 槽位号 = 图标 draw 的先后顺序；第 N 个图标 draw 配第 N 个血条 draw")
    out("=" * 78)

    if not slots:
        out("✗ 没有槽位可证：先跑 --list 看卡在哪一步。")
        return 4

    hit = 0
    for slot in slots:
        entry = slot.table_entry
        if entry is not None:
            hit += 1
            verdict = f"表内 id={entry.character_id} {entry.name} ✅"
        else:
            verdict = "表内无 ❌"
        out(f"槽{slot.index} hash={slot.icon_hash} {verdict}   {slot.status_text}")
        bound = (
            f"绑定行={slot.icon_draw.ps_t0_line}"
            if slot.icon_draw.ps_t0_line
            else "绑定行=无（用落盘行）"
        )
        out(
            f"    draw#{slot.icon_draw.id} {bound}  index_count={slot.icon_draw.index_count}  "
            f"图标 DDS={slot.icon_dds.size_text()} {slot.icon_dds.fmt or '格式未知'} "
            f"mips={slot.icon_dds.mips if slot.icon_dds.mips is not None else '未知'}"
        )
        if slot.bar_draw is not None:
            gap = slot.icon_draw.id - slot.bar_draw.id
            out(
                f"    配对血条 draw#{slot.bar_draw.id}（比图标早 {gap} 个 draw）"
                f" index_count={slot.bar_draw.index_count} "
                f"血条 DDS={slot.bar_dds.size_text()} {slot.bar_dds.fmt or '格式未知'} "
                f"mips={slot.bar_dds.mips if slot.bar_dds.mips is not None else '未知'}"
            )
            out(f"    血条签名={ok(slot.signature_ok)}（{slot.signature_detail}）")
        else:
            out("    配对血条: 没配上（本帧血条 draw 数量不足）")
        out(f"    判定依据: {slot.reason}；置信度={slot.confidence}")
        out("")

    out("-" * 78)
    out(f"对表结果: {hit} 槽命中 / {len(slots) - hit} 槽表内无")
    usable = [slot for slot in slots if slot.usable]
    out(f"可用性: ✅可新增 {len(usable)} 个 / ⚠候选被拒 {len(slots) - len(usable)} 个")
    for slot in slots:
        if not slot.usable:
            out(f"  槽{slot.index}(hash={slot.icon_hash}) 被拒：{slot.reject_reason}")
    now_new = [slot for slot in usable if slot.table_entry is None]
    if not now_new:
        out("结论: 这一帧没有「可新增」的新角色（能用的槽要么已在表里，要么没过签名）。")
    else:
        out("结论: 可新增的新角色槽 → " + "、".join(f"槽{slot.index}({slot.icon_hash})" for slot in now_new))
    if len(bar_draws) < len(slots):
        out(
            f"注意: 血条 draw 只有 {len(bar_draws)} 个，少于槽位 {len(slots)} 个；"
            f"槽 {len(bar_draws) + 1} 起的血条未配对（血条可能没画/被别的 UI 状态挡住）。"
        )
    return 0


def cmd_add(args, slots, table: list[TableEntry], ini_path: Path) -> int:
    if not slots:
        die("没有槽位可加：先跑 --list 看卡在哪一步。")
    if not (1 <= args.slot <= len(slots)):
        die(f"--slot {args.slot} 超出范围：这一帧只识别到 {len(slots)} 个槽位（1..{len(slots)}）。", 2)

    slot = slots[args.slot - 1]
    texture_hash = slot.icon_hash
    by_hash = {entry.hash: entry for entry in table}
    by_id: dict[int, TableEntry] = {}
    for entry in table:
        by_id.setdefault(entry.character_id, entry)

    out("=" * 78)
    out(
        f"槽{slot.index}  {slot.status_text}  hash={texture_hash}  draw#{slot.icon_draw.id}  "
        f"index_count={slot.icon_draw.index_count}  图标贴图={slot.icon_dds.text()}"
    )
    if slot.bar_draw is not None:
        out(
            f"配对血条 draw#{slot.bar_draw.id}  {slot.bar_dds.text()}  "
            f"签名={ok(slot.signature_ok)}（{slot.signature_detail}）"
        )
    else:
        out("配对血条: 没配上")
    out(f"依据: {slot.reason}")

    existing = by_hash.get(texture_hash)
    if existing is not None:
        out("")
        out(f"已在表里 id={existing.character_id} {existing.name}（hash={texture_hash}）")
        out("拒绝重复追加：没有写任何文件；已有条目、已有 id 都没被改动。")
        return 3

    if not slot.usable and not args.allow_unverified:
        out("")
        out(f"拒绝输出：槽{slot.index} 没通过参考实现的血条签名（{slot.reject_reason}）。")
        out("  这种槽加进表里，进游戏大概率读不到血量（静默失效：不报错、只是永远 1.0）。")
        out("  确认它就是那个角色、并愿意承担风险，再加 --allow-unverified 强行输出。")
        return 3

    if not slot.usable:
        out("")
        out("!" * 78)
        out(f"!! 警告：槽{slot.index} 血条签名未通过（{slot.reject_reason}）")
        out("!! 该哈希进游戏大概率读不到血量。只在你已经目视确认过那张贴图时才写进表。")
        out("!" * 78)

    same_name = [entry for entry in table if entry.name == args.name]
    if same_name:
        out("")
        out(
            f"拒绝追加：段名「{args.name}」已经被 hash={same_name[0].hash} "
            f"(id={same_name[0].character_id}) 占用，换个名字。"
        )
        return 3

    if args.id is not None:
        new_id = args.id
        owner = by_id.get(new_id)
        if owner is not None and owner.hash != texture_hash:
            out("")
            out(
                f"拒绝追加：id={new_id} 已被「{owner.name}」(hash={owner.hash}) 占用。"
                f"不指定 --id 时脚本会取 max(id)+1。"
            )
            return 3
    else:
        new_id = (max(entry.character_id for entry in table) + 1) if table else 1

    if new_id > 255:
        out("")
        out(f"拒绝追加：id={new_id} 超出 8bit 打包范围（参考实现只给 id 8 bit）。")
        return 3

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block_lines = [
        f"; 由 tools/add_character.py 追加（来源: {args.source_label} 槽{slot.index} "
        f"draw#{slot.icon_draw.id}，{stamp}）",
        f"[TextureOverride {args.name}]",
        "match_priority = -9999",
        f"hash = {texture_hash}",
        f"$character_id = {new_id}",
    ]

    out("")
    for line in block_lines[1:]:
        out(line)
    out("")
    out(f"新增 id={new_id} 名字={args.name} 置信度={slot.confidence}")
    out(f"（hash={texture_hash}；这条还没有写进 {short_path(str(ini_path))}）")
    if slot.table_entry is not None:
        out("⚠ 该槽本来就在表里，正常不会走到这里。")

    if not args.apply:
        out("")
        out("--add 默认只打印。要落盘请在同一条命令上加 --apply。")
        return 0

    return _apply_block(ini_path, block_lines, texture_hash, new_id, args.name)


def _apply_block(
    ini_path: Path, block_lines: list[str], texture_hash: str, new_id: int, name: str
) -> int:
    if not ini_path.is_file():
        die(f"--apply 失败：角色表不存在 {ini_path}", 2)

    raw = ini_path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    before = parse_table(text)

    if any(entry.hash == texture_hash for entry in before):
        out("")
        out("拒绝写入：文件里已经有这个 hash（可能刚刚写过）。没有改动文件。")
        return 3

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = ini_path.with_name(ini_path.name + f".bak-{stamp}")
    backup.write_bytes(raw)

    body = text
    if not body.endswith("\n"):
        body += newline
    if not body.endswith(newline + newline):
        body += newline
    body += newline.join(block_lines) + newline

    data = body.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    ini_path.write_bytes(data)

    after = parse_table(body)
    out("")
    out(f"已写入: {short_path(str(ini_path))}")
    out(f"备份:   {short_path(str(backup))}")
    out("")
    out("校验（与 blueprint/node_postprocess_health.py 同一套正则重解析整份文件）:")
    out(f"  [TextureOverride*] 段: {len(before)} → {len(after)}（期望 +1）")
    ids = [entry.character_id for entry in after]
    id_conflicts = sorted({value for value in ids if ids.count(value) > 1})
    hashes = [entry.hash for entry in after]
    hash_conflicts = sorted({value for value in hashes if hashes.count(value) > 1})
    before_ids = {entry.character_id for entry in before}
    added_ids = len(set(ids)) - len(before_ids)
    out(f"  重复 id（多皮肤共用，正常）: {id_conflicts if id_conflicts else '无'}")
    out(f"  唯一 id 数: {len(set(ids))}（原 {len(before_ids)} + 本次新增 {added_ids}）")
    out(f"  重复 hash: {hash_conflicts if hash_conflicts else '无'}")
    if ids:
        out(f"  id 上限: max={max(ids)}（<=255 {ok(max(ids) <= 255)}）")
    parsed = [entry for entry in after if entry.hash == texture_hash]
    if parsed:
        entry = parsed[0]
        out(f"  新段可被解析: name={entry.name} hash={entry.hash} id={entry.character_id} ✅")
    else:
        out(f"  新段可被解析: ❌ 没解析到 hash={texture_hash}（追加格式有问题）")
    if len(after) != len(before) + 1:
        out("  ⚠ 段数不是 +1，检查追加位置是否落在某个段体内部")
        return 4
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def resolve_source(args) -> tuple[Path, Path | None, str]:
    if args.frame_analysis:
        path = Path(args.frame_analysis)
        if path.is_dir():
            log = path / "log.txt"
            if not log.is_file():
                big = [
                    item
                    for item in sorted(path.glob("*.txt"))
                    if item.is_file() and item.stat().st_size > 100_000
                ]
                if not big:
                    die(f"--frame-analysis 目录里找不到 log.txt（也没有其它 >100KB 的 .txt）：{path}", 2)
                log = big[0]
            return log, path, f"FrameAnalysis {path}"
        if path.is_file():
            return path, path.parent, f"日志 {path}"
        die(f"--frame-analysis 路径不存在：{path}", 2)
    if args.log:
        path = Path(args.log)
        if not path.is_file():
            die(f"--log 文件不存在：{path}", 2)
        return path, path.parent, f"日志片段 {path}"
    die("必须给 --frame-analysis <目录> 或 --log <文件> 之一。", 2)
    raise AssertionError("unreachable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="add_character.py",
        description="从 FrameAnalysis / d3d11_log 推导新角色的血量检测数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--frame-analysis", metavar="DIR", help="FrameAnalysis 目录（主输入）")
    parser.add_argument("--log", metavar="FILE", help="d3d11_log.txt 片段（次输入）")
    parser.add_argument("--list", action="store_true", help="列出这一帧识别到的队伍槽位")
    parser.add_argument("--prove", action="store_true", help="自证：逐槽重新推导并与表对比")
    parser.add_argument("--add", action="store_true", help="把第 N 槽的条目打印出来（默认不写文件）")
    parser.add_argument("--apply", action="store_true", help="配合 --add：写回角色表（先留 .bak）")
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="允许对「血条签名未通过」的候选槽强行输出条目（会打警告，默认拒绝）",
    )
    parser.add_argument("--name", help="新角色的段名 / 中文名")
    parser.add_argument("--slot", type=int, help="槽位号（1..3，--list 里的编号）")
    parser.add_argument("--id", type=int, help="指定 character_id（默认 max(id)+1）")
    parser.add_argument("--ini", help=f"角色表路径（默认 <仓库>/{_TOOLSET_INI}）")
    parser.add_argument("--icon-vs", default=DEFAULT_ICON_VS, help="图标 VS 哈希（默认节点默认值）")
    parser.add_argument("--icon-ps", default=DEFAULT_ICON_PS, help="图标 PS 哈希")
    parser.add_argument("--bar-vs", default=DEFAULT_BAR_VS, help="血条 VS 哈希")
    parser.add_argument("--bar-ps", default=DEFAULT_BAR_PS, help="血条 PS 哈希")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = build_parser()
    args = parser.parse_args(argv)

    if not (args.list or args.prove or args.add):
        parser.print_help()
        return 2
    if args.apply and not args.add:
        die("--apply 只能和 --add 一起用（先看清条目再落盘）。", 2)
    if args.add and (not args.name or args.slot is None):
        die("--add 需要 --name <中文名> 和 --slot <槽号>。", 2)

    log_path, frame_dir, source_text = resolve_source(args)
    args.source_label = source_text

    draws = parse_log(log_path)
    if not draws:
        die(
            f"日志里一个 draw 都没解析到：{log_path}\n"
            "  卡在这一步：这不是 FrameAnalysis 的 log.txt（没有 6 位 draw 序号前缀）。",
            2,
        )

    ini_path = Path(args.ini).resolve() if args.ini else default_ini_path()
    table, _raw, _text = load_table(ini_path)

    search_dirs: list[Path] = []
    if frame_dir is not None:
        search_dirs.append(frame_dir)
    if log_path.parent not in search_dirs:
        search_dirs.append(log_path.parent)

    slots, icon_draws, bar_draws, rejected = build_slots(
        draws, table, args.icon_vs, args.icon_ps, args.bar_vs, args.bar_ps, search_dirs
    )

    if args.list:
        return cmd_list(source_text, draws, slots, icon_draws, bar_draws, rejected, args)
    if args.prove:
        return cmd_prove(source_text, draws, slots, icon_draws, bar_draws, args)
    return cmd_add(args, slots, table, ini_path)


if __name__ == "__main__":
    raise SystemExit(main())
