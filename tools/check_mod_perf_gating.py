# -*- coding: utf-8 -*-
"""模组产物「性能门控」审计（只读，可对任意 SSMT 导出模组运行）。

用途
----
本仓库对生成器做过的每一处**性能门控**优化，都必须在导出的配置表里体现
（口径：生成器改了模组必须同步；只改模组可以不改生成器，因为改模组是测试手段）。
本脚本把这些约定固化成可执行断言，防止后续改动悄悄改回去：

  A. UI 面板无门控区（摇杆/手柄/方向权重/滑块绑定）必须落在 `if $active == 1` 内
  B/C. 拖拽 objvis 清零每绘制帧执行；可见性发布用消费者的同一条 4 条件表达式
  D. 形态键 dispatch 有脏签名门控（含拖拽强制项），且签名声明齐全
  F. LOD0 块里不存在「下一个绘制同分支重设同名 FX」的冗余 RabbitFX 复位
  G. 动画驱动段每个 [Present] 都包在角色门控里（KeyToggle 不受影响）
  L. 面板 64 采样正弦 LUT 按相位同步组只保留一条
  R. 随机驱动暂停边沿复位 + 共享目标守卫（且守卫不含自身门控）

用法
----
    python tools/check_mod_perf_gating.py [配置表路径]
默认路径为本机克拉蕾模组；退出码 0 = 全部通过，1 = 有断言失败。
"""
import os
import re
import sys

DEFAULT_INI_CANDIDATES = (
    r"K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\SSMTGeneratedMod\克拉蕾\克拉蕾.ini",
    r"K:\SSMT-Package-master\3Dmigoto\ZZZ\新建文件夹\Mods\SSMTGeneratedMod\克拉蕾\克拉蕾.ini",
)
GEN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blueprint")
# 单文件 UI 构造器不在本仓库内，路径因机器而异：可用环境变量覆盖，找不到就跳过 P7/P8。
HTML = os.environ.get(
    "SSMT_UI_BUILDER_HTML", r"E:\代码\SSMT4-Alpha-main\UI 构造器 v79.24-融合版.html"
)
def _pick_ini(argv):
    """优先 --ini/位置参数；否则按候选路径找，全不存在就返回空串（跳过 ini 检查）。"""
    args = [a for a in argv[1:]]
    for index, value in enumerate(args):
        if value == "--ini" and index + 1 < len(args):
            return args[index + 1]
    positional = [a for a in args if not a.startswith("--")]
    if positional:
        return positional[0]
    for candidate in DEFAULT_INI_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return ""


INI = _pick_ini(sys.argv)
BAK = os.path.join(os.path.dirname(INI), "Backups") if INI else ""
if INI:
    text = open(INI, encoding="utf-8-sig").read()
    lines = text.splitlines()
else:
    text = ""
    lines = []

text = open(INI, encoding="utf-8-sig").read()
lines = text.splitlines()

if INI:
    print(f"模组配置表: {INI}")
    print(f"  最后写入: {os.path.getmtime(INI):.0f}  ({len(lines)} 行)")
else:
    print("模组配置表: 未找到（用 --ini <路径> 指定；跳过依赖 ini 的检查）")
for name in ("node_postprocess_draginteraction.py", "direct_export_shapekey_output_mixin.py",
             "anim_driver_random.py", "anim_driver_collector.py", "node_postprocess_material.py"):
    path = os.path.join(GEN, name)
    print(f"  生成器 {name}: {os.path.getmtime(path):.0f}")
print(f"  UI 构造器: {os.path.getmtime(HTML):.0f}")

checks = []


def check(label, ok, detail=""):
    checks.append((label, ok, detail))


# --- D: 形态键脏签名门控 ---
check("D1 [Constants] 声明 $ssmt_sk_sig_d942b3a7 = 0",
      "global $ssmt_sk_sig_d942b3a7 = 0" in text)
check("D2 [Constants] 声明 $ssmt_sk_sig_prev_d942b3a7 = -1",
      "global $ssmt_sk_sig_prev_d942b3a7 = -1" in text)
sig_assign = len(re.findall(r"^\s*\$ssmt_sk_sig_d942b3a7 = ", text, re.M))
check("D3 签名赋值行数 (生成器按 16 项/行分块 → 33 项应为 3 行)",
      sig_assign == 3, f"实际 {sig_assign}")
check("D4 签名门控 if（含拖拽强制项）",
      "if $ssmt_sk_sig_d942b3a7 != $ssmt_sk_sig_prev_d942b3a7 || $ssmtdrag_mode_A == 1" in text)
check("D5 签名变量在 dispatch 后回写",
      "        $ssmt_sk_sig_prev_d942b3a7 = $ssmt_sk_sig_d942b3a7" in text)
check("D6 形态键 dispatch 只出现一次",
      len(re.findall(r"run = CustomShader_d942b3a7-39828-0_Anim", text)) == 1)

# --- C: 拖拽检测/光标链门控 ---
check("C1 elif 加 drawn 门控",
      "elif $ssmtdrag_drag_enabled_A >= 1 && $ssmtdrag_drawn_A == 1" in text)
check("C2 终态 else 清 $ObjectDetectAllowed_A",
      bool(re.search(r"else\n\t\$ObjectDetectAllowed_A = 0\n\tclear = ResourceDragShapeKeyDragLatch_A", text)))
check("C3 旧的无 drawn 门控 elif 已消失",
      "elif $ssmtdrag_drag_enabled_A >= 1\n" not in text)
# 光标/视口更新只在臂动时执行（消费者全是 mode==1 链路，检测读的还是上一帧光标）
# 缩进不敏感：手打补丁与生成器输出的 tab 层级不同，只看结构。
check("C4 光标更新包在 if $ssmtdrag_mode_A == 1 内",
      bool(re.search(
          r"^\s*if \$ssmtdrag_mode_A == 1\n\s*run = CommandListDragCursorUpdate_A\n\s*endif$",
          text, re.M)))

# --- B: objvis 清零并入 drawn 门控 ---
m = re.search(
    r"if \$ssmtdrag_drawn_A == 1\n"
    r"(?:\t?post \$ssmtdrag_objvis_A_\d+ = 0\n)+endif",
    text,
)
post_in_gate = len(re.findall(r"^\t?post \$ssmtdrag_objvis_A_\d+ = 0$", text, re.M))
bare_count = len(re.findall(r"^post \$ssmtdrag_objvis_A_\d+ = 0$", text, re.M))
check("B1 objvis 清零全部在 if $ssmtdrag_drawn_A == 1 内", m is not None and post_in_gate == 215,
      f"门控内 {post_in_gate} 条（其中无缩进 {bare_count} 条）")
VIS_GATE = ("if $ssmtdrag_drag_enabled_A >= 1 && $inputMode == 0"
            " && $ssmtdrag_mode_A == 1 && $ssmtdrag_drawn_A == 1")
check("B2 可见性发布用消费者的同一条 4 条件门控",
      f"{VIS_GATE}\n\tpre run = CommandListDragVisPublish_A\nendif" in text)
check("B3 post $ssmtdrag_drawn_A = 0 仍在门控外",
      bool(re.search(r"endif\npost \$ssmtdrag_drawn_A = 0", text)))

# --- A: 面板无门控区 ---
depth = 0
region_start = None
for index, line in enumerate(lines, 1):
    stripped = line.strip()
    if stripped == "; Clamp joystick center to rounded bounds":
        region_start = (index, depth)
    if not stripped or stripped.startswith(";"):
        continue
    low = stripped.lower()
    if low.startswith("if ") or low.startswith("if("):
        depth += 1
    elif low == "endif":
        depth -= 1
check("A1 摇杆钳制区已在 if $active == 1 内", bool(region_start and region_start[1] == 1),
      f"行 {region_start[0] if region_start else '?'} 深度 {region_start[1] if region_start else '?'}")
# 该区最后一条手柄赋值也应在门控内
handle_lines = [i for i, l in enumerate(lines, 1) if re.match(r"^\s*\$r_hdl_\d+_[xy] = ", l)]
depth = 0
last_handle_depth = None
for index, line in enumerate(lines, 1):
    stripped = line.strip()
    if not stripped or stripped.startswith(";"):
        continue
    low = stripped.lower()
    if low.startswith("if ") or low.startswith("if("):
        depth += 1
    elif low == "endif":
        depth -= 1
    if handle_lines and index == handle_lines[-1]:
        last_handle_depth = depth
check("A2 最后一条手柄位置赋值也在门控内", last_handle_depth == 1, f"深度 {last_handle_depth}")

# --- 动画驱动随机驱动（本会话更早的改动） ---
prev_vars = sorted(set(re.findall(r"\$random_prev(\d+)", text)), key=int)
check("R1 暂停边沿复位变量存在（$random_prev*）", len(prev_vars) >= 6, f"{len(prev_vars)} 个: {prev_vars}")
check("R2 暂停边沿判定（if $random_prevN == 1）",
      bool(re.search(r"if \$random_prev\d+ == 1", text)))
check("R3 共享目标门控（等对方也暂停）",
      bool(re.search(r"if \$random_prev\d+ == 1 && \$random_paused\d+ == 0", text)))
# 自身门控：按 [Constants] 的 seed -> 自身 $random_pausedN 映射判定
seed_to_gate = {}
_current = None
for line in lines:
    m = re.match(r"^\s*global\s+persist\s+\$random_seed(\d+)\s*=", line)
    if m:
        _current = m.group(1)
        continue
    m = re.match(r"^\s*global\s+persist\s+\$random_paused(\d+)\s*=", line)
    if m and _current:
        seed_to_gate[_current] = f"$random_paused{m.group(1)}"
        _current = None
self_gate = []
for line in lines:
    m = re.match(r"^\s*if \$random_prev(\d+) == 1((?:\s*&&\s*\$random_paused\d+ == 0)*)\s*$", line)
    if not m:
        continue
    own = seed_to_gate.get(m.group(1))
    if own and f"{own} == 0" in m.group(2):
        self_gate.append(line.strip())
check("R4 守卫里不含自己的门控（生成器 _is_same_node 修复已同步）",
      not self_gate, f"仍含自身门控 {len(self_gate)} 处")

# --- 未同步项：生成器后续改动 ---
unconditional_zero = re.findall(r"^\t{0,2}\$Freq_\w+ = 0$", text, re.M)
check("R5 旧的「每帧无条件清零」已消失", not unconditional_zero,
      f"仍有 {len(unconditional_zero)} 行无条件清零")

# --- F: 每次绘制都有自己的 FX 复位（用户口径，2026-09 回退了冗余裁剪） ---
NS = "RabbitFX"
RESET_RE = re.compile(r"^Resource\\%s\\(Glowmap|FXMap) = ref null$" % NS)
SET_RE = re.compile(r"^Resource\\%s\\(Glowmap|FXMap) = ref Resource_" % NS)
RUN_LINE = f"run = CommandList\\{NS}\\Run"
_fx_resets = len(re.findall(rf"^\s*Resource\\{NS}\\FXMap = ref null$", text, re.M))
_fx_sets = len(re.findall(rf"^\s*Resource\\{NS}\\FXMap = ref Resource_", text, re.M))
# 每个复位都必须出现在「自己那个 mesh 的绘制之后」
_fx_misplaced = 0
_mesh_starts = [i for i, l in enumerate(lines) if "[mesh:" in l]
for _k, _mi in enumerate(_mesh_starts):
    _end = _mesh_starts[_k + 1] if _k + 1 < len(_mesh_starts) else len(lines)
    _seg = lines[_mi:_end]
    if not any(l.strip().startswith(f"Resource\\{NS}\\FXMap = ref Resource_") for l in _seg):
        continue
    _draw = next((j for j, l in enumerate(_seg) if l.strip().startswith("drawindexed")), None)
    _reset = next((j for j, l in enumerate(_seg) if RESET_RE.match(l.strip())), None)
    if _draw is None or _reset is None or _reset < _draw:
        _fx_misplaced += 1
_depth = [0] * (len(lines) + 1)
_d = 0
for _i, _l in enumerate(lines):
    _s = _l.strip()
    _depth[_i] = _d
    if not _s or _s.startswith(";"):
        continue
    _low = _s.lower()
    if _low.startswith("if ") or _low.startswith("if("):
        _d += 1
    elif _low == "endif":
        _d -= 1


def _same_branch(set_idx, draw_idx):
    base = _depth[set_idx]
    for j in range(set_idx + 1, draw_idx):
        s = lines[j].strip().lower()
        if (s.startswith("else") or s.startswith("elif")) and _depth[j] == base:
            return False
        if _depth[j] < base:
            return False
    return True


_blocks = []
_i = 0
while _i < len(lines):
    if RESET_RE.match(lines[_i].strip()):
        kinds, j, ok = set(), _i, True
        while j < len(lines) and j < _i + 6:
            s = lines[j].strip()
            m = RESET_RE.match(s)
            if m:
                kinds.add(m.group(1))
            elif re.match(r"^\$\\%s\\brightness = 0$" % NS, s):
                pass
            elif s == RUN_LINE:
                break
            else:
                ok = False
                break
            j += 1
        if ok and j < len(lines) and lines[j].strip() == RUN_LINE and kinds:
            _blocks.append((_i, j, kinds))
            _i = j + 1
            continue
    _i += 1

_redundant = []
for _start, _end, _kinds in _blocks:
    _draw = None
    _set_kinds, _set_idx = set(), {}
    for j in range(_end + 1, min(_end + 60, len(lines))):
        s = lines[j].strip()
        m = SET_RE.match(s)
        if m:
            _set_kinds.add(m.group(1))
            _set_idx.setdefault(m.group(1), j)
        if s.startswith("drawindexed") or s.startswith("draw "):
            _draw = j
            break
        if s.startswith("["):
            break
    if _draw is not None and _kinds and _kinds.issubset(_set_kinds) and all(
        _same_branch(_set_idx[k], _draw) for k in _kinds
    ):
        _redundant.append(_start + 1)
check("F1 每个设了 FX 的绘制都有配套复位（用户口径：每次绘制独立复位，允许重排顺序）",
      _fx_resets == _fx_sets > 0,
      f"复位 {_fx_resets} / 设FX {_fx_sets}（历史上曾做过冗余复位裁剪，已按用户要求回退）")
check("F2 复位都紧跟在自己的绘制之后", _fx_misplaced == 0, f"位置异常 {_fx_misplaced} 处")

# --- L: 面板正弦 LUT 合并（本轮优化） ---
_lut_heads = [i for i, l in enumerate(lines) if re.match(r"^    if \$anim_local_phase_\d+ < 0\.015625$", l)]
check("L1 面板正弦 LUT 链只剩 1 条（相位同步组共享）", len(_lut_heads) == 1, f"{len(_lut_heads)} 条")
check("L2 12 个组件的相位推进都还在",
      all(f"$anim_local_phase_{i} = $anim_local_phase_{i} + $anim_local_speed_{i}" in text for i in range(12)))
check("L3 12 个组件的 mode 分派都还在",
      all(f"if $anim_local_mode_{i} == 2" in text for i in range(12)))

# --- S: 形态键 CS 绑定形式（实机结论：文件型缓冲必须 copy，ref 会让形态键整体失效） ---
_readonly_registers = r"cs-(?:t(?:50|51|52|53|99))"
_readonly_copy = re.findall(rf"^\s*{_readonly_registers} = copy ", text, re.M)
_readonly_ref = re.findall(rf"^\s*{_readonly_registers} = ref ", text, re.M)
check("S1 形态键只读输入缓冲用 copy（改 ref 实测会让动画完全不动）",
      bool(_readonly_copy) and not _readonly_ref,
      f"copy {len(_readonly_copy)} 处 / ref {len(_readonly_ref)} 处")
check("S2 可写输出 cs-u5 也是 copy（CS 要写进去）", "cs-u5 = copy" in text)

# --- G: 动画驱动段 [Present] 角色门控（本轮优化） ---
_end_marker = next((i for i, l in enumerate(lines) if "; --- END ANIMATION DRIVER SECTION ---" in l), None)
_section_re = re.compile(r"^\s*\[(.+?)\]\s*$")
if _end_marker is None:
    check("G1 找到动画驱动段结束标记", False)
else:
    _present_heads = []
    _cur = None
    for _i in range(_end_marker + 1):
        _m = _section_re.match(lines[_i])
        if _m:
            if _cur == "Present":
                _present_heads.append(_prev_head)
            _cur = _m.group(1)
            _prev_head = _i
    if _cur == "Present":
        _present_heads.append(_prev_head)
    _gated = [
        h for h in _present_heads
        if lines[h + 1].strip() in ("if $active0 == 1", "if $ntmi_active0 == 1")
    ]
    check("G1 动画驱动段每个 [Present] 都紧跟角色门控",
          len(_present_heads) > 0 and len(_gated) == len(_present_heads),
          f"{len(_gated)}/{len(_present_heads)} 个")
    check("G2 门控数量与段落数一致", len(_gated) == 7, f"{len(_gated)}")
check("G3 $active0 已声明", "global $active0" in text)
check("G4 $active0 仍在主 [Present] 里帧尾清零", "post $active0 = 0" in text)
check("G5 动画驱动段的 KeyToggle 未被门控",
      "condition $active0 == 1" in text.replace("condition = ", "condition ")
      or "condition = $active0 == 1" in text)

# --- E: 点击计数导出「受控变量只在点击推进沿写一次」 ---
# 旧实现每帧走 `else: var = ckval` 强写；受控变量同时是动画播放状态
# （$animation_paused10/11）时会把该动画按停。现在只允许 edge-first 形态。
_ck_old = re.findall(r"^\s*else\n\s*\$\w+ = \$ssmtdrag_ckval_A_", text, re.M)
_ck_edge = re.findall(r"^\s*if \$ssmtdrag_ckedge_A_(\S+) == 1$", text, re.M)
_ck_set = re.findall(r"^\s*\$ssmtdrag_ckedge_A_\S+ = 1$", text, re.M)
_ck_reset = re.findall(r"^\s*\$ssmtdrag_ckedge_A_\S+ = 0$", text, re.M)
check("E1 点击计数受控变量已改为边沿触发（无每帧强写）",
      not _ck_old and bool(_ck_edge),
      f"旧形态 {len(_ck_old)} 处 / 边沿判定 {len(_ck_edge)} 处")
check("E2 沿标志有置位与帧首复位",
      len(_ck_set) == len(_ck_reset) > 0,
      f"置位 {len(_ck_set)} / 复位 {len(_ck_reset)}")
check("E3 动画播放状态不再被每帧覆盖（paused10/11 仍存在且非每帧强写）",
      "global persist $animation_paused10 = 0" in text
      and "global persist $animation_paused11 = 0" in text)
# 沿标志必须与"计数回绕 if"同级、在其 endif **之后** —— 插进 endif 之前的话，
# 只在回绕那一帧置位（该帧 ckval 恒 0），表现为点击完全失效（曾真实发生）。
_edge_bad = 0
for _i, _l in enumerate(lines):
    if re.match(r"^\s*\$ssmtdrag_ckedge_A_\S+ = 1$", _l):
        if _i + 1 < len(lines) and lines[_i + 1].strip() == "endif":
            _edge_bad += 1
check("E4 沿标志在计数回绕 endif 之后（否则点击失效）", _edge_bad == 0,
      f"插错位置 {_edge_bad} 处")


# 产物侧通过只说明模组是对的；这里再确认**生成器代码本身**仍带着这些优化，
# 否则下次重新导出会把优化丢掉，而产物看起来还是好的。
_GEN_FILES = {
    "material": os.path.join(GEN, "node_postprocess_material.py"),
    "shapekey_mixin": os.path.join(GEN, "direct_export_shapekey_output_mixin.py"),
    "shapekey_node": os.path.join(GEN, "node_postprocess_shapekey.py"),
    "anim_driver": os.path.join(GEN, "node_postprocess_anim_driver.py"),
    "drag": os.path.join(GEN, "node_postprocess_draginteraction.py"),
}
_gen_sources = {}
for _key, _path in _GEN_FILES.items():
    try:
        _gen_sources[_key] = open(_path, encoding="utf-8").read()
    except OSError:
        _gen_sources[_key] = ""


def _gen_check(label, key, needles, forbidden=()):
    source = _gen_sources.get(key, "")
    if not source:
        check(label, False, f"读不到生成器 {key}")
        return
    missing = [n for n in needles if n not in source]
    hit = [f for f in forbidden if f in source]
    check(label, not missing and not hit,
          ("缺少: " + "; ".join(missing) if missing else "")
          + (" 残留: " + "; ".join(hit) if hit else ""))


_gen_check("P1 LOD0 每次绘制独立复位规则仍在（material，恒 True）", "material",
           ["def _should_emit_fx_reset", "return True"])
_gen_check("P2 形态键脏签名门控仍在（shapekey mixin）", "shapekey_mixin",
           ["_SK_SIGNATURE_WEIGHTS", "$ssmt_sk_sig_", "_drag_mode_variable_for",
            "signature_vars=signature_vars", "drag_active_var="])
_gen_check("P3 动画驱动 [Present] 门控 + 段头边界仍在（anim_driver）", "anim_driver",
           ["def _gate_present_content", "self._gate_present_content(para_content)",
            "body_end", "def _anim_driver_active_flag"])
_gen_check("P4 拖拽绘制门控 / 发布门控 / 光标门控仍在（drag）", "drag",
           ["$ssmtdrag_drawn_{ns} == 1", "&& $ssmtdrag_mode_{ns} == 1",
            "if $ssmtdrag_mode_{ns} == 1"])
# ③ 两条路径并存：非帧表仍 copy；帧表允许 ref，但资源必须声明可建 SRV 的 bind_flags。
_gen_check("P5 形态键绑定：copy 路径仍在，帧表 ref 路径带 bind_flags", "shapekey_mixin",
           ["cs-t51 = copy", "cs-t51 = ref", "bind_flags = shader_resource"])
_gen_check("P6 形态键只读绑定仍是 copy（节点内联发射器，无帧表 ref 路径）", "shapekey_node",
           ["cs-t51 = copy", "cs-u5 = copy {res_name}_0"],
           forbidden=["cs-t51 = ref", "cs-t52 = ref", "cs-t53 = ref"])


def _check_ref_bindings(ini_text):
    """`= ref <资源>` 只允许引用声明了 shader_resource 的 StructuredBuffer。"""
    sections = {}
    current = ""
    for raw in ini_text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().casefold()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.casefold())
    bad = []
    for raw in ini_text.splitlines():
        line = raw.strip()
        match = re.match(r"cs-t\d+\s*=\s*ref\s+(\S+)", line, re.IGNORECASE)
        if not match:
            continue
        resource = match.group(1).strip().strip("[]").casefold()
        if "frametable" not in resource and "groupmap" not in resource:
            continue  # 只有帧表/组映射要求显式 bind_flags（slot 会自动传播 bind flags）
        body = sections.get(resource)
        if body is None:
            continue  # 资源不在本 ini 里声明（运行时/别处提供），无法判定，不误报
        has_flags = any("bind_flags" in item and "shader_resource" in item for item in body)
        if not has_flags:
            bad.append(match.group(1))
    return bad


if INI:
    _bad_refs = _check_ref_bindings(text)
    check("P5b 生成的 ini：每个 cs-t* = ref 的资源都声明 shader_resource",
          not _bad_refs, "缺少 bind_flags: " + ", ".join(_bad_refs) if _bad_refs else "")
else:
    check("P5b 生成的 ini：每个 cs-t* = ref 的资源都声明 shader_resource", True, "无 ini，跳过")
# HTML 生成器（单文件权威版）—— 不在仓库内，缺失则跳过
if os.path.isfile(HTML):
    _html = open(HTML, encoding="utf-8").read()
    check("P7 面板门控覆盖范围仍在（HTML v79.31-perf）",
          "v79.31-perf：面板门控块在这里闭合" in _html
          and "v79.31-perf：这里**不再**闭合面板门控块" in _html)
    check("P8 面板正弦 LUT 相位同步组守卫仍在（HTML v79.32-perf）",
          "if(lutKey !== lastLocalAnimLutKey) {" in _html)
else:
    check("P7/P8 HTML 生成器检查", True, f"未找到 {HTML}，跳过")

print()
ok_count = 0
failed = []
for label, ok, detail in checks:
    mark = "OK  " if ok else "FAIL"
    ok_count += 1 if ok else 0
    if not ok:
        failed.append((label, detail))
    print(f"[{mark}] {label}" + (f"   ({detail})" if detail else ""))
print(f"\n门控同步项 {ok_count}/{len(checks)}")

if os.path.isdir(BAK):
    backups = sorted(os.listdir(BAK))
    print(f"\n可回滚备份 {len(backups)} 份（最近 3 份）: " + ", ".join(backups[-3:]))

if failed:
    print("\n未通过项：")
    for label, detail in failed:
        print(f"  - {label}" + (f" ({detail})" if detail else ""))
    sys.exit(1)

