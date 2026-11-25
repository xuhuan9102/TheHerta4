"""三级变形接力链（多文件 → 形态键）的 INI 规整模块。

接力协议 v3（见 .kunsdd/plan 计划书 §2.2）：
1. 锚点别名统一为 ``{X}_0``（多文件从旧的 ``_1`` 迁移）；
2. 多文件 CS 输出双写：``{X}_mf = ref cs-u5`` 与 ``{X} = ref cs-u5``；
3. 运行时就位标志 ``$ssmt_mf_ran_{X}``：多文件的 [Present] 块每帧显式置 1/0；
4. 形态键条件锚定：``if $ssmt_mf_ran_{X} == 1 → copy {X}_mf else → copy {X}_0``；
5. [Present] 变形接力块内 run 行按 rank（多文件 10 → 形态键 20）排序，
   与后处理节点执行先后解耦；
6. ``post {X} = copy_desc {X}_0`` 复位行每资源去重为一条；
   ``post run`` 行由本模块移除（[Present] 为唯一执行窗口）。

本模块是纯函数式的（只操作 ``sections: OrderedDict[str, list[str]]``），
不依赖 bpy，可在单元测试中直接运行。``finalize_deform_chain`` 幂等。
"""

from __future__ import annotations

import re
from typing import Optional, Tuple


# ---- 常量 --------------------------------------------------------------------

CHAIN_BEGIN = "; --- SSMT DEFORM CHAIN BEGIN ---"
CHAIN_END = "; --- SSMT DEFORM CHAIN END ---"
PRESENT_SECTION = "[Present]"
CONSTANTS_SECTION = "[Constants]"

RANK_MULTIFILE = 10
RANK_SHAPEKEY = 20
# 形态键权重搬运必须先于形态键 CS：先把打包窗口搬进 mod 专属权重缓冲并清零窗口，
# Anim CS 才能从缓冲读到本帧强度。rank 取 18（< RANK_SHAPEKEY=20）保证排序在前。
RANK_SHAPEKEY_WEIGHT_SYNC = 18
# 原生 morph 权重在 Present 的 Anim CS 消费完后才能清掉；下一帧重新捕获。
RANK_NATIVE_MORPH_CLEAR = 30

_INDENT = "    "

# ``run = CustomShader_xxx_1Anim`` → (rank, 资源键)；``run = CustomShader_{hash}_Anim`` → rank 20
_CHAIN_RUN_RE = re.compile(r"^\s*run\s*=\s*CustomShader_(.+)_1Anim\s*$")
_CHAIN_RUN_SHAPEKEY_WEIGHT_SYNC_RE = re.compile(r"^\s*run\s*=\s*CustomShaderShapeKeyWeightSync_(.+)\s*$")
_CHAIN_RUN_SHAPEKEY_RE = re.compile(r"^\s*run\s*=\s*CustomShader_(.+)_Anim\s*$")
_CHAIN_RUN_NATIVE_MORPH_CLEAR_RE = re.compile(
    r"^\s*run\s*=\s*CustomShaderZZMINativeMorphClear_(.+)\s*$"
)

_MF_REF_RE = re.compile(r"^\s*(\S+)_mf\s*=\s*ref\s+cs-u5\s*$")
_REF_RE = re.compile(r"^\s*(\S+)\s*=\s*ref\s+cs-u5\s*$")
_POST_RUN_RE = re.compile(r"^\s*post\s+run\s*=\s*(CustomShader_\S+)\s*$")
_DIRECT_SHAPEKEY_PRESENT_BEGIN = "; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---"

_BONE_MATRIX_ORDER = {
    "SSMTNode_PostProcess_MultiFile": 0,
    "SSMTNode_PostProcess_ShapeKey": 1,
    "SSMTNode_PostProcess_ShapeKeyExt": 2,
    "SSMTNode_PostProcess_Material": 3,
    "SSMTNode_PostProcess_CustomMaterialAssign": 3,
    "SSMTNode_PostProcess_BoneMatrix": 4,
    "SSMTNode_PostProcess_DragInteraction": 5,
    "SSMTNode_PostProcess_FluidProbe": 6,
}

TEXT_APPEND_NODE = "SSMTNode_PostProcess_TextAppend"
BUFFER_CLEANUP_NODE = "SSMTNode_PostProcess_BufferCleanup"


def order_bone_matrix_nodes(nodes):
    """Order the nodes that share a ZZMI draw while keeping other nodes in place."""
    bone_matrix = "SSMTNode_PostProcess_BoneMatrix"
    if not any(node.bl_idname == bone_matrix and not getattr(node, "mute", False) for node in nodes):
        return nodes
    edits = [i for i, node in enumerate(nodes) if node.bl_idname in _BONE_MATRIX_ORDER]
    ordered = sorted((nodes[i] for i in edits), key=lambda node: _BONE_MATRIX_ORDER[node.bl_idname])
    result = list(nodes)
    for i, node in zip(edits, ordered):
        result[i] = node
    return result


def order_postprocess_nodes(nodes):
    ordered = order_bone_matrix_nodes(nodes)
    active_types = {node.bl_idname for node in ordered if not getattr(node, "mute", False)}
    if {TEXT_APPEND_NODE, BUFFER_CLEANUP_NODE} <= active_types:
        # Cleanup needs the filename references introduced by the terminal text node.
        order = {TEXT_APPEND_NODE: 1, BUFFER_CLEANUP_NODE: 2}
        return sorted(ordered, key=lambda node: order.get(node.bl_idname, 0))
    return ordered


# ---- 小工具 ------------------------------------------------------------------

def _trimmed(lines):
    """去掉首尾空行（strip() 为空的行）。"""
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _strip_line(line) -> str:
    return line.strip()


def _swapkey_var_name(raw_var: str) -> str:
    return raw_var if raw_var.startswith("$") else "$" + raw_var


def mf_ran_var(resource_name: str) -> str:
    """多文件运行时就位标志变量名。"""
    return f"$ssmt_mf_ran_{resource_name}"


def classify_chain_run(line) -> Optional[Tuple[int, str]]:
    """判断一行是否属于变形接力 run 行。

    返回 ``(rank, 资源键)``：多文件 rank=10、键为 base_name；形态键 rank=20、键为 hash。
    非接力 run 行返回 None。
    """
    m = _CHAIN_RUN_RE.match(line)
    if m:
        return (RANK_MULTIFILE, m.group(1))
    m = _CHAIN_RUN_SHAPEKEY_WEIGHT_SYNC_RE.match(line)
    if m:
        return (RANK_SHAPEKEY_WEIGHT_SYNC, m.group(1))
    m = _CHAIN_RUN_SHAPEKEY_RE.match(line)
    if m:
        return (RANK_SHAPEKEY, m.group(1))
    m = _CHAIN_RUN_NATIVE_MORPH_CLEAR_RE.match(line)
    if m:
        return (RANK_NATIVE_MORPH_CLEAR, m.group(1))
    return None


# ---- multifile 侧：锚定与输出改写 ---------------------------------------------

def rewrite_multifile_shader_lines(shader_lines):
    """把多文件 shader 段的锚定改为共享锚点 ``_0``、输出双写 ``_mf`` + 规范名。

    只匹配模块自己生成的行形态；幂等（重复调用不再改动）。
    返回 ``(lines, changed, base_resource_name)``。
    """
    changed = False
    base_resource_name = None
    out_lines = []
    for line in list(shader_lines):
        stripped = _strip_line(line)
        m = re.match(r"^cs-u5\s*=\s*copy\s+(\S+)_1\s*$", stripped)
        if m:
            base_resource_name = m.group(1)
            line = line.replace(f"copy {base_resource_name}_1", f"copy {base_resource_name}_0")
            changed = True
            out_lines.append(line)  # 修正：替换后的行必须保留（原实现吞掉了锚定行）
            continue
        m = re.match(r"^cs-u5\s*=\s*copy\s+(\S+)_0\s*$", stripped)
        if m:
            base_resource_name = m.group(1)
        out_lines.append(line)

    if base_resource_name:
        mf_ref_line = f"{_INDENT}{base_resource_name}_mf = ref cs-u5"
        has_mf_ref = any(_strip_line(l) == f"{base_resource_name}_mf = ref cs-u5" for l in out_lines)
        if not has_mf_ref:
            for index, line in enumerate(out_lines):
                m = _REF_RE.match(line)
                if m and m.group(1) == base_resource_name:
                    out_lines.insert(index, mf_ref_line)
                    changed = True
                    break
    return out_lines, changed, base_resource_name


def ensure_multifile_present_block(present_lines, active_swapkey, active_value, mf_ran_vars, run_lines):
    """把多文件的 run 行迁入带 mf_ran 标志的激活块（幂等）。

    - 从 present_lines 中移除这些 run 行（无论在哪个块里）；
    - 删除本模块之前生成的旧激活块（按 mf_ran 标志行识别）；
    - 追加标准块：
      ``if {active_swapkey} == {active_value}`` → 置 ``$ssmt_mf_ran_{X} = 1`` ×N → run ×M →
      ``else`` → 置 ``= 0`` ×N → ``endif``。
    """
    run_stripped = {_strip_line(r) for r in run_lines}
    flag_vars = list(dict.fromkeys(mf_ran_vars))  # 去重保序

    new_lines = []
    index = 0
    while index < len(present_lines):
        line = present_lines[index]
        stripped = _strip_line(line)
        if stripped in run_stripped:
            index += 1
            continue
        # 删除旧的激活块：从 ``if {active} == {value}`` 开始、含本模块标志行的块
        if stripped == f"if {active_swapkey} == {active_value}":
            block_end = _find_endif(present_lines, index)
            if block_end is not None:
                block = present_lines[index:block_end + 1]
                if any(_strip_line(b).startswith("$ssmt_mf_ran_") for b in block):
                    index = block_end + 1
                    continue
        new_lines.append(line)
        index += 1

    new_lines = _trimmed(new_lines)
    block = [f"if {active_swapkey} == {active_value}"]
    for var in flag_vars:
        block.append(f"{_INDENT}{var} = 1")
    for run_line in run_lines:
        block.append(f"{_INDENT}run = {run_line}" if not run_line.strip().startswith("run =") else f"{_INDENT}{_strip_line(run_line)}")
    if flag_vars:
        block.append("else")
        for var in flag_vars:
            block.append(f"{_INDENT}{var} = 0")
    block.append("endif")
    if new_lines:
        new_lines.append("")
    new_lines.extend(block)
    return new_lines


def _find_endif(lines, start_index):
    depth = 0
    for index in range(start_index, len(lines)):
        stripped = _strip_line(lines[index])
        if stripped.startswith("if ") or stripped.startswith("if("):
            depth += 1
        elif stripped == "endif":
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_guarded_shapekey_insert_index(lines):
    """返回接力块应插入的位置，确保其先于受条件保护的形态键 run。"""
    for index, line in enumerate(lines):
        if _strip_line(line) == _DIRECT_SHAPEKEY_PRESENT_BEGIN:
            return index

        stripped = _strip_line(line)
        if not (stripped.startswith("if ") or stripped.startswith("if(")):
            continue
        block_end = _find_endif(lines, index)
        if block_end is None:
            continue
        for block_line in lines[index:block_end + 1]:
            classified = classify_chain_run(block_line)
            if classified and classified[0] == RANK_SHAPEKEY:
                return index
    return len(lines)


# ---- shapekey 侧：条件锚定改写 --------------------------------------------------

def rewrite_shapekey_anchor_lines(block_lines, hash_prefix, has_mf_by_prefix):
    """把形态键 shader 段里对资源 X 的锚定改为条件锚定（幂等）。

    输入为 ``[CustomShader_{h}_Anim]`` 的行列表；``hash_prefix`` 为该 hash 的资源前缀；
    ``has_mf_by_prefix`` 为 ``{prefix: bool}``。
    返回 ``(lines, changed)``。
    """
    if not has_mf_by_prefix.get(hash_prefix):
        return block_lines, False
    changed = False
    out_lines = []
    for line in list(block_lines):
        stripped = _strip_line(line)
        m = re.match(r"^cs-u5\s*=\s*copy\s+(\S+)_0\s*$", stripped)
        if m:
            resource_name = m.group(1)
            conditional = _build_conditional_anchor(resource_name)
            out_lines.extend(conditional)
            changed = True
            continue
        out_lines.append(line)
    return out_lines, changed


def _build_conditional_anchor(resource_name):
    ran_var = mf_ran_var(resource_name)
    return [
        f"{_INDENT}if {ran_var} == 1",
        f"{_INDENT}{_INDENT}cs-u5 = copy {resource_name}_mf",
        f"{_INDENT}else",
        f"{_INDENT}{_INDENT}cs-u5 = copy {resource_name}_0",
        f"{_INDENT}endif",
    ]


def _infer_resource_stride(sections, resource_name, default=40):
    for candidate_name in (resource_name, f"{resource_name}_0", f"{resource_name}_1"):
        for line in sections.get(f"[{candidate_name}]", []):
            match = re.match(r"^\s*stride\s*=\s*(\d+)\s*$", line, re.IGNORECASE)
            if match:
                stride = int(match.group(1))
                if stride > 0:
                    return stride
    return default


# ---- 终态规整（幂等） -----------------------------------------------------------

def finalize_deform_chain(sections):
    """规整变形接力链到协议 v3 终态。

    - [Present]：把散落的接力 run 行（rank 10/20）收拢进 ``CHAIN_BEGIN/END`` 块，按 rank 稳定排序；
    - [Constants]：声明 ``$ssmt_mf_ran_{X}``（global persist = 0，由 multifile 标志行反推）；
    - [Constants]：``post run = CustomShader_...`` 行移除；
    - [Constants]：``post {X} = copy_desc {X}_0`` 复位行每资源去重为一条；
    - [CustomShader_*_Anim]（形态键，rank 20）：若存在 rank 10 段则把锚定改为条件锚定，
      并补齐 ``{X}_mf`` 空声明资源段。

    幂等：对已规整的 sections 再调用不产生变化。
    """
    mf_resources = set()

    # 1) Present：收集接力 run 行（含激活块内的 multifile run），重建接力块
    present_lines = sections.get(PRESENT_SECTION)
    if present_lines is not None:
        chain_entries = []  # (rank, key, 原始run文本)
        rest_lines = []
        index = 0
        while index < len(present_lines):
            line = present_lines[index]
            stripped = _strip_line(line)
            if stripped in (CHAIN_BEGIN, CHAIN_END):
                index += 1
                continue
            classified = classify_chain_run(line)
            if classified:
                chain_entries.append((classified[0], classified[1], stripped))
                index += 1
                continue
            # 多文件激活块：含 mf_ran 标志的整块平移（run 行被接力块收编、标志行保留）
            if stripped.startswith("if ") and "$ssmt_mf_ran_" not in stripped:
                block_end = _find_endif(present_lines, index)
                if block_end is not None:
                    block = present_lines[index:block_end + 1]
                    if any(_strip_line(b).startswith("$ssmt_mf_ran_") for b in block):
                        for b in block:
                            b_stripped = _strip_line(b)
                            classified_b = classify_chain_run(b)
                            if classified_b:
                                chain_entries.append((classified_b[0], classified_b[1], b_stripped))
                            else:
                                if b_stripped.startswith("$ssmt_mf_ran_") and b_stripped.endswith("= 1"):
                                    var = b_stripped.split(" ")[0]
                                    mf_resources.add(var[len("$ssmt_mf_ran_"):])
                                rest_lines.append(b)
                        index = block_end + 1
                        continue
                    rest_lines.extend(block)
                    index = block_end + 1
                    continue
            rest_lines.append(line)
            index += 1

        # 去重（rank+文本），稳定按 rank 排序
        seen = set()
        unique_entries = []
        for rank, key, text in chain_entries:
            marker = (rank, text)
            if marker not in seen:
                seen.add(marker)
                unique_entries.append((rank, key, text))
        unique_entries.sort(key=lambda item: (item[0], item[1]))

        rest_lines = _trimmed(rest_lines)
        new_present = list(rest_lines)
        if unique_entries:
            chain_block = [CHAIN_BEGIN]
            for _rank, _key, text in unique_entries:
                run_body = text[text.index("run"):].strip() if "run" in text else text
                # text 已是 ``run = CustomShader_...`` 形态（stripped）
                chain_block.append(f"{_INDENT}{run_body}")
            chain_block.append(CHAIN_END)

            insert_at = _find_guarded_shapekey_insert_index(new_present)
            before = _trimmed(new_present[:insert_at])
            after = _trimmed(new_present[insert_at:])
            new_present = before
            if new_present:
                new_present.append("")
            new_present.extend(chain_block)
            if after:
                new_present.append("")
                new_present.extend(after)
        sections[PRESENT_SECTION] = new_present

    # 2) CustomShader 段扫描：多文件锚定迁移(_1→_0)+输出双写、统计 mf 资源
    mf_ref_resources = set()
    for section_name in list(sections.keys()):
        if not (section_name.startswith("[CustomShader_") and section_name.endswith("_1Anim]")):
            continue
        new_lines, changed, base = rewrite_multifile_shader_lines(sections[section_name])
        if changed:
            sections[section_name] = new_lines
        if base:
            mf_ref_resources.add(base)
    for section_name, lines in sections.items():
        if not (section_name.startswith("[CustomShader_") and section_name.endswith("]")):
            continue
        for line in lines:
            m = _MF_REF_RE.match(line)
            if m:
                mf_ref_resources.add(m.group(1))
    mf_resources |= mf_ref_resources

    # 3) 形态键条件锚定：仅对确实存在 _mf 输出的资源生效（避免悬空引用）
    if mf_ref_resources:
        for section_name in list(sections.keys()):
            if not (section_name.startswith("[CustomShader_") and section_name.endswith("_Anim]")):
                continue
            if section_name.endswith("_1Anim]"):
                continue
            lines = sections[section_name]
            new_lines = []
            changed = False
            index = 0
            while index < len(lines):
                line = lines[index]
                stripped = _strip_line(line)
                # 幂等守卫：已在条件块内（if $ssmt_mf_ran_ 起、endif 止）的锚定行跳过
                if stripped.startswith("if $ssmt_mf_ran_"):
                    block_end = _find_endif(lines, index)
                    if block_end is not None:
                        new_lines.extend(lines[index:block_end + 1])
                        index = block_end + 1
                        continue
                m = re.match(r"^cs-u5\s*=\s*copy\s+(\S+)_0\s*$", stripped)
                if m and m.group(1) in mf_ref_resources:
                    new_lines.extend(_build_conditional_anchor(m.group(1)))
                    changed = True
                    index += 1
                    continue
                new_lines.append(line)
                index += 1
            if changed:
                sections[section_name] = new_lines

    # 4) {X}_mf 空声明资源段
    for resource_name in sorted(mf_ref_resources):
        mf_section = f"[{resource_name}_mf]"
        if mf_section not in sections:
            stride = _infer_resource_stride(sections, resource_name)
            sections[mf_section] = ["type = Buffer", f"stride = {stride}"]

    # 4) Constants：mf_ran 声明、post run 移除、复位行去重
    constants_lines = sections.get(CONSTANTS_SECTION)
    if constants_lines is not None:
        new_constants = []
        seen_reset_resources = set()
        declared_mf_vars = set()
        for line in constants_lines:
            stripped = _strip_line(line)
            post_run_match = _POST_RUN_RE.match(stripped)
            if post_run_match and classify_chain_run(f"run = {post_run_match.group(1)}"):
                continue  # 仅移除变形接力自身的 post run；保留其他 CustomShader 调用
            # 旧版 _1 复位行迁移为 _0（协议 v3 锚点统一）
            m = re.match(r"^post\s+(\S+)\s*=\s*copy_desc\s+\S+_1\s*$", stripped)
            if m:
                resource = m.group(1)
                if resource not in seen_reset_resources:
                    seen_reset_resources.add(resource)
                    new_constants.append(f"post {resource} = copy_desc {resource}_0")
                continue
            m = re.match(r"^post\s+(\S+)\s*=\s*copy_desc\s+\S+_0\s*$", stripped)
            if m:
                resource = m.group(1)
                if resource in seen_reset_resources:
                    continue
                seen_reset_resources.add(resource)
            m = re.match(r"^global\s+persist\s+(\$ssmt_mf_ran_\S+)", stripped)
            if m:
                declared_mf_vars.add(m.group(1))
            new_constants.append(line)
        # 声明 mf_ran（persist 初值 0；每帧由 Present 显式置位）
        missing = [mf_ran_var(res) for res in sorted(mf_resources) if mf_ran_var(res) not in declared_mf_vars]
        if missing:
            new_constants = _trimmed(new_constants)
            if new_constants:
                new_constants.append("")
            for var in missing:
                new_constants.append(f"global persist {var} = 0")
        sections[CONSTANTS_SECTION] = new_constants

    return sections
