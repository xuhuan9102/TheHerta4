"""TTL 协议公共实现：多个后处理节点共用同一份解析/生成逻辑。"""

import re
from pathlib import Path


def require_apmi_ttl_core(runtime_ini):
    if not runtime_ini or not Path(runtime_ini).is_file():
        raise ValueError("APMI：TTL 需要有效的加载器 d3dx.ini。")
    core = Path(runtime_ini).parent / "Core"
    for name in ("APMI_TTL.ini", "APMI_TTLMask.hlsl", "APMI_TTLComposite.hlsl"):
        if not (core / name).is_file():
            raise ValueError(f"APMI：TTL 缺少加载器文件 {core / name}。")


_TTL_DRAW_ARG_RE = re.compile(
    r"\$\\TTL\\_([123])\s*=\s*([+-]?\d+)\s*(?:;.*)?", re.IGNORECASE
)
_EFMI_TTL_DRAW_ARG_RE = re.compile(
    r"\$\\EFMITTL\\(count|first)\s*=\s*(\d+)\s*(?:;.*)?", re.IGNORECASE
)
_MESH_ANCHOR_RE = re.compile(r"\[mesh:[^\]]+\]", re.IGNORECASE)


def _read_draw_arguments(lines, line_index, block_start_index, pattern, keys):
    values = {}
    for index in range(line_index - 1, block_start_index - 1, -1):
        stripped = str(lines[index] or "").strip()
        if _MESH_ANCHOR_RE.search(stripped):
            break
        if not stripped or stripped.startswith((";", "//")):
            continue
        match = pattern.fullmatch(stripped)
        if not match or match.group(1).casefold() in values:
            break
        values[match.group(1).casefold()] = int(match.group(2))
        if len(values) == len(keys):
            args = tuple(values[key] for key in keys)
            return (index, args) if args[0] > 0 and args[1] >= 0 else None
    return None


def parse_ttl_draw_args(lines, line_index, block_start_index=0):
    """从本次 TTLib 调用之前的 `_1/_2/_3` 读取绘制参数。"""
    parsed = parse_ttl_draw_call(lines, line_index, block_start_index)
    return parsed[1] if parsed else None


def parse_ttl_draw_call(lines, line_index, block_start_index=0):
    """返回 TTLib 参数组的位置及绘制参数。"""
    return _read_draw_arguments(lines, line_index, block_start_index, _TTL_DRAW_ARG_RE, ('1', '2', '3'))


def parse_efmi_ttl_draw_call(lines, line_index, block_start_index=0):
    """返回 EFMITTL 参数组的位置及 count、first。EFMITTL 固定 base vertex 为 0。"""
    return _read_draw_arguments(lines, line_index, block_start_index, _EFMI_TTL_DRAW_ARG_RE, ('count', 'first'))


def build_transparency_draw_lines(info, resource, index_count, index_offset, base_vertex):
    """按「透明贴图」节点配置生成一笔 TTL 绘制的完整参数组。

    TEXTURE 模式用贴图做遮罩，alpha 固定写 1（遮罩本身决定透明度）；
    ALPHA 模式不引用贴图，alpha 用节点数值。alpha 行必须每笔都写：
    TTL 库在 Draw 结尾会复位这些变量，漏写会沿用上一笔的残留值。
    """
    lines = []
    if info["mode"] == "TEXTURE":
        lines.append(r"Resource\TTL\TransparencyTex = ref " + str(resource))
        alpha_text = "1"
    else:
        lines.append(r"Resource\TTL\TransparencyTex = null")
        alpha_text = str(info["alpha"])
    lines.extend([
        r"$\TTL\mask_channel = " + str(info["mask_channel"]),
        r"$\TTL\mask_invert = " + str(int(info["mask_invert"])),
        r"$\TTL\alpha = " + alpha_text,
        r"$\TTL\V = " + str(info["brightness"]),
        r"$\TTL\_1 = " + str(index_count),
        r"$\TTL\_2 = " + str(index_offset),
        r"$\TTL\_3 = " + str(base_vertex),
        r"run = CommandList\TTL\Draw",
    ])
    return lines
