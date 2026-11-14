"""TTL 协议公共解析：多个后处理节点共用同一份读取逻辑。"""

import re


_TTL_DRAW_ARG_RE = re.compile(
    r"\$\\TTL\\_([123])\s*=\s*([+-]?\d+)\s*(?:;.*)?", re.IGNORECASE
)
_MESH_ANCHOR_RE = re.compile(r"\[mesh:[^\]]+\]", re.IGNORECASE)


def parse_ttl_draw_args(lines, line_index, block_start_index=0):
    """只从 TTL 调用紧邻的参数组读取 \`\`_1/_2/_3\`\`，避免借用前一笔绘制的参数。

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
