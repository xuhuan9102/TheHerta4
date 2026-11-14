"""TTL 协议公共实现：多个后处理节点共用同一份解析/生成逻辑。"""

import re


_TTL_DRAW_ARG_RE = re.compile(
    r"\$\\TTL\\_([123])\s*=\s*([+-]?\d+)\s*(?:;.*)?", re.IGNORECASE
)
_MESH_ANCHOR_RE = re.compile(r"\[mesh:[^\]]+\]", re.IGNORECASE)


def parse_ttl_draw_args(lines, line_index, block_start_index=0):
    """只从 TTL 调用紧邻的参数组读取 `_1/_2/_3`，避免借用前一笔绘制的参数。

    找不到完整三参数、或范围非法（count<=0 / first_index<0）时返回 None。
    """
    values = {}
    for index in range(line_index - 1, block_start_index - 1, -1):
        stripped = str(lines[index] or "").strip()
        if _MESH_ANCHOR_RE.search(stripped):
            break
        if not stripped or stripped.startswith((";", "//")):
            continue
        match = _TTL_DRAW_ARG_RE.fullmatch(stripped)
        if not match or match.group(1) in values:
            break
        values[match.group(1)] = int(match.group(2))
        if len(values) == 3:
            params = (values["1"], values["2"], values["3"])
            return params if params[0] > 0 and params[1] >= 0 else None
    return None


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
