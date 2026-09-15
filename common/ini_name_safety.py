"""INI 段名安全化：把会破坏段名的字符替换掉。

背景（2026-09-13 实测）：

部件名会被直接拼进段名，例如

    [TextureOverride_VB_ae840e72_头饰[丝带]_Position]

3DMigoto 解析段名时 ``[`` / ``]`` 是**定界符**，行内的方括号会提前结束
段名，于是这一行实际被读成 ``[TextureOverride_VB_ae840e72_头饰[丝带]`` 加一个
孤立的 ``]``。两个名字只差一个尾缀的部件（``头饰[丝带]`` / ``头饰[丝带1]``）
就这样塌成同一个段名，引擎报 ``Duplicate section found`` 并**忽略后一个**，
那个部件的 VB 绑定与 draw 全部失效。

实测依据：用户 2026-09-13 的叶瞬光导出（``Mods/SSMTGeneratedMod/叶瞬光（原）``）
里，``头饰[丝带]`` 与 ``头饰[丝带1]`` 两个部件产生 4 条 duplicate-section 警告。

本模块只做**名字安全化**，不改任何几何、权重或绑定语义。中文、数字、点号、
连字符、下划线、加号都原样保留（它们是合法的段名内容）。
"""

import re

# 段名定界符：出现即让段名提前结束（3DMigoto 的 ini 解析器按这两个字符定界）。
_SECTION_DELIMITERS = str.maketrans({"[": "_", "]": "_"})

# 控制字符与换行：写进 ini 会截断这一行，等于毁掉整个段。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# 行内注释符：段名里出现会让段头被当成注释截断（解析器差异，保守处理）。
_COMMENT_CHARS = str.maketrans({";": "_"})


def sanitize_section_name_part(text, fallback: str = "part") -> str:
    """返回可以安全拼进 ``[方括号]`` 段名的名字片段。

    - ``[`` / ``]`` → ``_``（**关键修复**：否则段名提前结束，产生重复段）
    - 分号 ``;`` → ``_``（避免被当注释截断）
    - 控制字符/换行 → ``_``
    - 首尾空白去掉；结果为空时返回 ``fallback``
    """
    if text is None:
        return fallback
    cleaned = _CONTROL_CHARS.sub("_", str(text))
    cleaned = cleaned.translate(_SECTION_DELIMITERS).translate(_COMMENT_CHARS)
    cleaned = cleaned.strip()
    return cleaned or fallback


def section_name_fragments_are_safe(*parts) -> bool:
    """自检用：全部片段都不含定界符/控制字符时返回 True。"""
    for part in parts:
        if part is None:
            continue
        text = str(part)
        if any(ch in text for ch in "[];"):
            return False
        if _CONTROL_CHARS.search(text):
            return False
    return True
