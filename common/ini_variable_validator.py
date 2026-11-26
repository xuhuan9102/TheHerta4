"""生成产物 INI 的变量声明校验（导出后自检）。

为什么需要它
------------
3DMigoto 里没有被 ``global`` / ``local`` 声明的 ``$var`` 会退化成**该段（命令
列表）的局部变量**：跨 ``run =`` 边界不传递，别的段也读不到。历史事故都属于这
一类，而且在游戏里表现为"功能静默失效"，几乎无法从现象定位：

* ``$ssmtdrag_viewport_valid`` 未声明 → 子列表置 1 后调用方仍读 0 → 视口恒无效
  → 光标恒 (-1,-1) → 检测永不命中（见 tests/test_node_postprocess_draginteraction.py）；
* 形态键预分配被动画驱动的**引用名**挤成 ``Freq_xxx_1`` 后，驱动段写的是
  ``$Freq_xxx``（无人声明）而形态键着色器读 ``$Freq_xxx_1`` → 联动整条死掉。

分配口径与发射侧已经修好（variable_registry 只按 owner 判冲突 + 发射前引用对齐）；
本模块是**最后一道防线**：导出收尾时扫一遍产物 ini，把"引用了但没声明"的变量、
以及被重复声明的全局变量报出来，避免同类问题再次静默上线。
"""

import glob
import os
import re
from pathlib import Path

# 3DMigoto 内建变量：无需声明，跨段可读。
BUILTIN_VARIABLES = frozenset({
    "$active",
    "$swapkey",
    "$frame",
    "$object",
    "$draw",
    "$ShaderModel",
    "$windows",
    "$normal",
    "$comp",
    "$lambda",
    "$texture_count",
    "$time",
    "$cursor_x",
    "$cursor_y",
    "$cursor_window_x",
    "$cursor_window_y",
    "$cursor_screen_x",
    "$cursor_screen_y",
    "$window_width",
    "$window_height",
    "$res_width",
    "$res_height",
    "$rt_width",
    "$rt_height",
    "$draw_type",
    "$DRAW_TYPE",
    "$INDEX_COUNT",
    "$FIRST_INDEX",
    "$FIRST_VERTEX",
    "$INSTANCE_COUNT",
    "$FIRST_INSTANCE",
})

_VAR_TOKEN_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_GLOBAL_DECL_RE = re.compile(r"^\s*global(?:\s+persist)?\s+(\$[A-Za-z_][A-Za-z0-9_]*)")
_LOCAL_DECL_RE = re.compile(r"^\s*local\s+(\$[A-Za-z_][A-Za-z0-9_]*)")
_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
# 池：[PoolFoo] 段头即声明，变量形态是 $PoolFoo[index]（整段名带 Pool 前缀，
# 不剥前缀）。实证：K 盘的 3Dmigoto/Zmd/Core/EFMI/SpatialIdentification.ini 里
# [PoolSpatialIdentity_SpatialIds] 对应
# $PoolSpatialIdentity_SpatialIds[$draw_call_instance_id]。池值按段过期、天然
# 跨段可读，不需要 global；不登记就会把每次池读写误报成「引用未声明」。
_POOL_SECTION_RE = re.compile(r"^\s*\[(Pool[A-Za-z0-9_]+)\]\s*$")


def _strip_comment(line: str) -> str:
    return str(line or "").split(";", 1)[0]


def analyze_ini_text(text: str, *, runtime_globals=()) -> dict:
    """扫描 ini 文本，返回声明/引用统计与问题清单。

    两遍扫描：先收集全部声明（3DMigoto 的变量登记与声明出现位置无关，跨段引用
    是常态——动画驱动段在文件最上方，其引用的 $ssmtdrag_booted_* 声明在下方），
    再逐行找"被引用但没有声明"的变量。

    返回键：
      ``declared``           全局声明集合
      ``local_declared``     局部声明集合
      ``undeclared``         {变量: [首次出现的段名, ...]}（按首次出现排序）
      ``duplicate_globals``  {变量: 声明次数}（>1 即重复声明）
      ``section_count``
    """
    source = str(text or "")
    lines = source.splitlines()

    declared = set()
    local_declared = set()
    global_counts = {}
    runtime_names = {name.casefold() for name in runtime_globals}

    for raw_line in lines:
        body = _strip_comment(raw_line)
        if not body.strip():
            continue
        for pool_name in _POOL_SECTION_RE.findall(body):
            declared.add("$" + pool_name)
        for name in _GLOBAL_DECL_RE.findall(body):
            declared.add(name)
            global_counts[name] = global_counts.get(name, 0) + 1
        for name in _LOCAL_DECL_RE.findall(body):
            local_declared.add(name)

    undeclared = {}
    section = "<文件头>"
    section_count = 0
    for raw_line in lines:
        match = _SECTION_RE.match(raw_line)
        if match:
            section = match.group(0).strip()
            section_count += 1
            continue

        body = _strip_comment(raw_line)
        if not body.strip():
            continue

        for name in _VAR_TOKEN_RE.findall(body):
            if name in declared or name in local_declared or name in BUILTIN_VARIABLES:
                continue
            if name.casefold() in runtime_names:
                continue
            if name in undeclared:
                if section not in undeclared[name]:
                    undeclared[name].append(section)
                continue
            undeclared[name] = [section]

    duplicate_globals = {name: count for name, count in global_counts.items() if count > 1}
    return {
        "declared": declared,
        "local_declared": local_declared,
        "undeclared": undeclared,
        "duplicate_globals": duplicate_globals,
        "section_count": section_count,
    }


def _read_runtime_globals(path: str) -> set[str]:
    """读取 3DMigoto 主配置 [Constants] 中由模组共用的全局变量。"""
    if not path:
        return set()
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
            in_constants = False
            names = set()
            for line in handle:
                body = _strip_comment(line).strip().casefold()
                section = _SECTION_RE.fullmatch(body)
                if section:
                    in_constants = section[1] == "constants"
                elif in_constants:
                    names.update(_GLOBAL_DECL_RE.findall(body))
            return names
    except OSError:
        return set()


def validate_ini_file(ini_path: str, *, runtime_ini_path: str = "") -> dict:
    """校验单个 ini 文件；文件不存在时返回 ``{"missing": True}``。"""
    path = str(ini_path or "")
    if not path or not os.path.isfile(path):
        return {"missing": True, "path": path}
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        text = handle.read()
    report = analyze_ini_text(text, runtime_globals=_read_runtime_globals(runtime_ini_path))
    report["missing"] = False
    report["path"] = path
    return report


def find_runtime_ini(mod_export_path: str, configured_game_path: str = "") -> str:
    """优先使用产物所在 Mods 目录的主配置，自定义输出目录沿用当前游戏配置。"""
    if mod_export_path:
        output = Path(mod_export_path).resolve()
        for folder in (output, *output.parents):
            if folder.name.casefold() == "mods":
                candidate = folder.parent / "d3dx.ini"
                if candidate.is_file():
                    return str(candidate)
    return str(Path(configured_game_path) / "d3dx.ini") if configured_game_path else ""


def find_primary_ini(mod_export_path: str, workspace_name: str = "") -> str:
    """在导出目录里挑出主 ini（排除备份/纹理附属 ini）。"""
    folder = str(mod_export_path or "")
    if not folder or not os.path.isdir(folder):
        return ""
    candidates = [
        path for path in glob.glob(os.path.join(folder, "*.ini"))
        if ".bak" not in os.path.basename(path).lower()
    ]
    if not candidates:
        return ""

    name = str(workspace_name or "").strip()
    if name:
        exact = [
            path for path in candidates
            if os.path.splitext(os.path.basename(path))[0] == name
        ]
        if len(exact) == 1:
            return exact[0]
        prefixed = [
            path for path in candidates
            if os.path.basename(path).startswith(f"{name}_")
        ]
        if len(prefixed) == 1:
            return prefixed[0]

    if len(candidates) == 1:
        return candidates[0]
    # 多候选时取体积最大的（主 ini 远大于附属 ini）
    try:
        return max(candidates, key=os.path.getsize)
    except OSError:
        return candidates[0]


def format_findings(report: dict, max_items: int = 20) -> list:
    """把报告整理成可直接打印/记日志的行。"""
    if not report or report.get("missing"):
        return []

    lines = []
    undeclared = report.get("undeclared") or {}
    if undeclared:
        lines.append(
            f"⚠️ [INI 校验] 有 {len(undeclared)} 个变量被引用但未声明"
            "（3DMigoto 会当成段内局部变量 → 跨段失效，功能会静默失效）:"
        )
        for index, (name, sections) in enumerate(sorted(undeclared.items())):
            if index >= max_items:
                lines.append(f"    …另有 {len(undeclared) - max_items} 个")
                break
            where = ", ".join(sections[:3])
            if len(sections) > 3:
                where += f" 等 {len(sections)} 处"
            lines.append(f"    {name}  ← [{where}]")

    duplicates = report.get("duplicate_globals") or {}
    if duplicates:
        lines.append(
            f"ℹ️ [INI 校验] 有 {len(duplicates)} 个全局变量被重复声明"
            "（3DMigoto 会告警；面板与拖拽模块同时声明同一变量时常见）:"
        )
        for index, (name, count) in enumerate(sorted(duplicates.items())):
            if index >= max_items:
                lines.append(f"    …另有 {len(duplicates) - max_items} 个")
                break
            lines.append(f"    {name}  ×{count}")

    if not lines:
        lines.append(
            f"✅ [INI 校验] 变量声明自检通过（{report.get('section_count', 0)} 个段，"
            f"{len(report.get('declared') or ())} 个全局变量）"
        )
    return lines
