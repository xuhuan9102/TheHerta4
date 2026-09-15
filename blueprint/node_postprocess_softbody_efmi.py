# -*- coding: utf-8 -*-
"""EFMI 拖拽交互独立分支（Arknights: Endfield / EFMI v1 运行时）。

与 zzmi 分支（node_postprocess_draginteraction.py）完全独立的实现：
本模块不 import zzmi 分支的任何符号，不共用着色器/常量/烘焙逻辑/段生成。
机制级继承的 3Dmigoto 卫生纪律（读必设、资源存还、幂等剥离重发、命名空间
机制）由本模块独立实现。

运行时契约来源（研究③ docs/analysis/efmi-drag-contract-and-migration-map.md）：
- 技能包 endfield-chest-drag（SKILL.md + references/* + scripts/generate_weights_typhoeus.py）
- 帧分析 FrameAnalysis-2026-09-03-055352
- EFMI 框架（API.ini / MergedSkeleton.ini / SpatialIdentification.ini）与
  TheHerta4 EFMI 导出器（ui/universal/efmi.py）生成的 EntryPoint / Draw 回调段

关键差异（vs zzmi）：
- 挂载点：EFMI EntryPoint → CommandList_Component_Instance_DrawCustom 回调链
  （MergedSkeleton_Apply 之后、原始绘制之前），逐空间实例（≤8）回调；
- 探针：native-VS 投影（点列表 → texel 直写 SV_Position），无矩阵重建；
- 检测：三角形重心命中 + reverse-Z + top-left/downward 屏幕公式 + 稀疏区权重
  （每顶点 K 对区 id+权重插值取最强区）；
- 模拟：每实例 4 公共槽 + 256 区 × 2 弹簧状态（Present 每帧并行 dispatch 32）；
- 变形：位置-only（只写前 3 个 uint word）、源/输出/绘制三分立、vb0/vb3 成对换绑；
- 烘焙：空间球场（brush_strength/brush_falloff_k，zzmi 同款公式；
  N 空物体 = N 区，0 基 zone id 0-255）+ 稀疏 K 原始场值（t16 起不归一，
  zzmi 对齐——归一化会压平单区衰减成 0/1 硬边界）；**efmi-zzmi-drag-align
  t3 起全面对齐 ZZMI 烘焙/运行时算法**（基准 docs/analysis/zzmi-drag-baseline-spec.md，
  逐项动作见 docs/analysis/efmi-zzmi-align-r1.md）：
  * 区域字段全消费：propagate（沿表面测地扩散，Dijkstra）/ include_objects
    （组件级包含过滤，经 EntryPoint `; [mesh:...]` 注释）/ 节点级 mask_plateau
    （平台化衰减）/ 非镜像工作流 X 镜像补偿；
  * body 侧每组件直烘 + 全无效早退（ZZMI _write_jiggle_masks 语义）；
    **cloth 侧保留 EFMI 独有的 body→cloth 16 近邻高斯传递**（D-2 用户
    拍板保留特性；传递源 = body 直烘表，天然继承新算法语义）；
  * 运行时物理 = ZZMI 半隐式弹簧直传（spring 0.176/0.055、follow 0.12、
    释放踢 1.10、释放动态阻尼 1.05/0.92、sim_speed/max_step 3.0/3.0、
    depth_pull 比例×冻结法线、dragScale 1.00）——临界阻尼 ω=√k→Hz 映射
    与定值 Y 深度已废弃（D-1，团队目标②推翻 2026-09 C2 保留项）；
  * 手型渲染链 = ZZMI rzm_jiggle_hand H1-H16 机制移植（三轴表面基经每区
    8 锚点（4 恒等拖拽基 + 4 烘焙 gizmo 轴）投影供给；charging 三态预览；
    25 项 persist 参数面）；
  * cursor 域保留项（队长硬约束/基准 §6.1）：fork 内建 cursor_x/y [0,1]
    top-down 双直用禁触（[Present] 光标注入两行）；手型链内部坐标域在
    preview CS 出口换算为 ZZMI Y-up px 约定，注入公式不受对齐影响；
  * poke 戳击脉冲物理不迁移（D-3：仅引 charging/蓄力手型机制）；
  * 无 VLR / 无 TTL / 无碰撞系统（本期不迁移，UI 侧提示不可用）。

已知语义（终审 F5，明示接受）：deform 对同一实例同一 mesh 种类的多个材质
子绘制（每帧重复回调）会重复 dispatch——deform 是幂等纯函数（Out = 源 +
当前弹簧位移，从源重建），重复执行结果精确一致、无累积误差，仅多耗 GPU；
「每帧每实例每 mesh 缓存一次」为性能优化项（t15 实机验证后按需加时间门控）。

路由：GlobalConfig.logic_name == EFMI → 本执行器；由 zzmi 节点类的
execute_postprocess 单入口集中分发（本模块不反向依赖）。
"""

import json
import os
import re
import shutil
import time
from collections import OrderedDict

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover - Blender 环境缺失 numpy 时降级
    NUMPY_AVAILABLE = False

# ---------------------------------------------------------------------------
# 常量（EFMI 独立命名空间，与 zzmi 的 ResourceDrag* / $ssmtdrag_* 不相交）
# ---------------------------------------------------------------------------

# 导出着色器在 ini 中的引用路径（mod 根 → res/drag_interaction_efmi/）
EFMI_RES_SHADER_DIR = "res/drag_interaction_efmi"

# 幂等标记
EFMI_TAIL_MARKER = "; --- AUTO-APPENDED DRAG INTERACTION MODULE (EFMI) ---"
EFMI_HOOK_BEGIN = "; --- EFMI DRAG HOOK BEGIN ---"
EFMI_HOOK_END = "; --- EFMI DRAG HOOK END ---"
# probe 级联独立标记（B1/t13：probe 移到真实 draw 之后——其段尾
# UnbindAllRenderTargets 会把 OM 颜色 RT 解绑为空，若在 draw 前执行则当帧主体
# 无渲染目标=模型消失；独立标记保证幂等剥离不掉 draw 行）
EFMI_PROBE_BEGIN = "; --- EFMI DRAG PROBE BEGIN ---"
EFMI_PROBE_END = "; --- EFMI DRAG PROBE END ---"
EFMI_PRESENT_BEGIN = "; --- EFMI DRAG PRESENT BEGIN ---"
EFMI_PRESENT_END = "; --- EFMI DRAG PRESENT END ---"
EFMI_SECTION_PREFIXES = (
    "[CustomShaderEFMIDrag",
    "[CommandListEFMIDrag",
    "[KeyEFMIDrag",
    "[ResourceEFMIDrag",
)

# 公共 auto-appended 尾块标记（INI 格式契约，与其余后处理节点共用字面量，
# 这里仅作字面量复制以便独立解析，不 import 任何后处理模块）
_AUTO_APPENDED_MARKERS = (
    "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
    "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
    "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---",
    EFMI_TAIL_MARKER,
)
_AUTO_APPENDED_MARKER_PREFIXES = ("; --- AUTO-APPENDED UI PANEL ",)
_ANIM_DRIVER_BEGIN = "; --- ANIMATION DRIVER SECTION ---"
_ANIM_DRIVER_END = "; --- END ANIMATION DRIVER SECTION ---"

DEFAULT_MOD_NAMESPACE = "A"
EFMI_MAX_INSTANCES = 8
# ---- 256 区模型（t23 契约升级：区域容量 256，用户要求支持 256 个范围）----
EFMI_MAX_ZONES = 256
EFMI_ZONES_PER_VERTEX = 4  # 稀疏权重 K：每顶点最多活动区数（超出取最强 K 个）
# 每实例 State 布局：6 公共槽（t21 增 grabCenter[b+4]；align-t3 [b+5] = 共享
# 抓取信息 (held, maxOffset, stretchFraction, 0)——ZZMI InteractionState[8] 同
# 语义，抓手倾斜/振动的驱动源）+ 256 区 × 4
# （t3 对齐 ZZMI 半隐式弹簧：每区 4 槽 = current/previous(含 prevStep)/
# filtered(含 prevTargetStep)/prevFiltered(含释放动态阻尼倍率)）
EFMI_STATE_PUBLIC_SLOTS = 6
EFMI_STATE_ZONE_STRIDE = 4
EFMI_STATE_STRIDE = EFMI_STATE_PUBLIC_SLOTS + EFMI_MAX_ZONES * EFMI_STATE_ZONE_STRIDE  # 1030
# 每实例 Candidate 记录（4 float4）：[0]=最强区(zone,weight,depth,frame)、
# [1]=right basis、[2]=down basis、[3]=命中锚点屏幕坐标
EFMI_CANDIDATE_STRIDE = 4
EFMI_PROBE_WIDTH = 512  # detect.hlsl 的 texel 解码硬编码 512 宽（契约）
# 每区锚点数（t3 起 4→8：行 0-3 = 中心+恒等轴 ±10mm（拖拽基，detect
# ComputeBasis 消费，语义不变）；行 4-7 = 中心+烘焙表面 gizmo 轴（法线/切线/
# 副切线，ZZMI BuildGizmoAxes 构造，手型三轴基经 hand_preview 投影消费））
EFMI_ANCHOR_COUNT = 8
EFMI_ANCHOR_OFFSET_M = 0.01  # 锚点局部偏移（10 mm）
# ---- 纯空间高斯球模型（t25 全面 zzmi 化：gb_core gaussian_field 等价语义，
#      独立实现；256 区 + 稀疏 K=4 布局不变）----
EFMI_BALL_FALLOFF_K = 4.6  # 默认画刷衰减 k（zzmi 同款）

# 契约基线调参（align-t3 起 = ZZMI 基准值，zzmi-drag-baseline-spec.md §2）：
# 物理档案 = ZZMI 节点属性默认值直传（半隐式弹簧语义，不再 ω=√k→Hz 映射）
DEFAULT_GRAB_SPRING = 0.176      # ZZMI phys_grab_spring 节点默认（L751）
DEFAULT_RELEASE_SPRING = 0.055   # ZZMI phys_release_spring 节点默认（L753）
DEFAULT_GRAB_DAMPING = 0.86      # ZZMI phys_grab_damping 节点默认（L750）
DEFAULT_RELEASE_DAMPING = 0.96   # ZZMI phys_release_damping 节点默认（L752）
DEFAULT_TARGET_FOLLOW = 0.12     # ZZMI phys_release_kick 节点默认（L754；键名历史错位=目标跟随）
DEFAULT_RELEASE_KICK = 1.10      # ZZMI phys_target_follow 节点默认（L755；键名历史错位=释放冲击）
DEFAULT_DRAG_SCALE = 1.00        # ZZMI JIGGLE_PARAMS.w（w68；旧 0.70 废除）
DEFAULT_DEPTH_PULL = 1.00        # ZZMI z73 depth_pull 默认（比例×冻结法线模型）
DEFAULT_MOUSE_XDIR = 1.00        # ZZMI y73
DEFAULT_MOUSE_YDIR = 1.00        # ZZMI z71
DEFAULT_SIM_SPEED = 3.0          # ZZMI $ssmtdrag_sim_speed 默认（L4761）
DEFAULT_MAX_STEP = 3.0           # ZZMI $ssmtdrag_max_step 默认（L4762）
DEFAULT_RELEASE_BOOST = 1.05     # ZZMI $ssmtdrag_release_boost（L4751）
DEFAULT_RELEASE_DECAY = 0.92     # ZZMI $ssmtdrag_release_decay（L4752）
# R7 t46-P1：默认最大位移对齐 ZZMI 显式 POLISH_PARAMS.x = 0.50
# （blueprint/node_postprocess_draginteraction.py L3803/L3940 `x71 = 0.50`；
# 运行时 maxOffset = 显式 0.50 × mult_radius；逐区 ssmt_drag_zone.max_offset>0
# 仍由其覆盖（shader 侧 ZoneParams[z*2].z 替换语义，ZZMI ZoneOffsetOverride 同款）。
DEFAULT_MAX_OFFSET = 0.50
# A1/t26：命中权重阈值 0.1 → 1e-4（ZZMI 同款，object_detect L654）——弱权重区
# （渐变权重边缘）可命中；emit 于 detect/Present 的 w152、simulate/ui_publish/
# hand_preview 四方共用
DEFAULT_HIT_THRESHOLD = 0.0001
# 主色 pass 门控阈值（t47 §10 pass 计数门控：深度 pass 恒 1/2 次、颜色 pass
# 恒第 3 次起——GDZGF 实证 o0=3315d2b5 主色 RT；ps hash 表达式门控已废弃）
DEFAULT_PROBE_PASS_THRESHOLD = 3
# 兼容保留（旧识别文件格式；不再用于门控）
DEFAULT_PROBE_PASS_HASH = "1718.1, d7bb9dd57f5b70c6"

SHADER_FILES = (
    "efmi_probe.hlsl",
    "efmi_detect.hlsl",
    "efmi_simulate.hlsl",
    "efmi_deform.hlsl",
)
# 形态键驱动着色器（F1 形态键联动开启时随拷贝，避免段族已发射而文件未落盘）
SHADER_DRIVE_FILES = ("efmi_shapekey_drive.hlsl",)
# 变量联动着色器（F4 变量联动开启时随拷贝；F4 硬依赖 F1）
SHADER_VARSYNC_FILES = ("efmi_shapekey_var_sync.hlsl",)
# 面板联动着色器（F3 面板联动开启时随拷贝；独立于 F1/F4）
SHADER_PANEL_FILES = ("efmi_ui_publish.hlsl",)
# 手型光标（enable_hand_cursor 开启时随拷贝；着色器独立自研，资产为共享网格
# 字节级复制——t22 UX 对齐恢复）
SHADER_HAND_FILES = ("efmi_hand.hlsl", "efmi_hand_preview.hlsl")
HAND_ASSET_FILES = (
    "HandAction.buf", "HandAction.ib", "HandAction_Normal.buf",
    "HandNoAction.buf", "HandNoAction.ib", "HandNoAction_Normal.buf",
)

# ---- F1 形态键联动（独立前缀 + 独立寄存器区）----
# 形态键消费方经拖拽节点 _drag_shapekey_resource_prefix() 推导同名前缀
#（跨节点契约，研究② §9；zzmi 前缀为 ResourceDragShapeKey）
EFMI_SKD_PREFIX = "ResourceEFMIDragShapeKey"
# EFMI 联动扩展区：紧邻 150-155 探测区，与 zzmi 77-124 不相交
EFMI_SKD_IP_BASE = 156
EFMI_SKD_SEED_BASE = 158
EFMI_SKD_MAX_SEED = 8
# ---- t37-P3 三态运行模式（0=关 / 1=仅命中 / 2=命中+拖拽）----
# 模式寄存器 159（150-158 探测/驱动区与 166+ 联动区之间空闲）：
# shader 侧（simulate 抓取门 / deform 位移门）经 IniParams[159].x 读。
EFMI_MODE_IP = 159
# ---- t42 P-1/P-2：全局倍率寄存器（160/161，150-159 探测/模式区与 166+
# 联动区之间空闲；shader 侧 simulate 经 IniParams[160].x / [161].x 读）----
# 160.x = mult_damping（P-2 damping override 替换语义的全局回退倍率；
#             ZZMI JIGGLE_MULT_EXTRA.x 同源，screen_state L332-333）
# 161.x = mult_strength（P-1 strength 全局倍率；ZZMI JIGGLE_MULTIPLIERS.z
#             同源，默认 0.333，screen_state L338 / interaction L842）
EFMI_MULT_DAMPING_IP = 160
EFMI_MULT_STRENGTH_IP = 161

# ---- align-t3：ZZMI 物理扩展区（162-165；EFMI 区原位扩展，槽位号不搬 ZZMI
# 67-104——区划 ABI 保留（t43 契约钉住），值与语义按 ZZMI 基准；裁决依据：
# m_ini_helper_gui.py 的 UI 面板子系统在用 x87/x88/x89，与 ZZMI 手型 89 槽
# 撞车）----
# 162 = POLISH 扩展：x=释放踢（phys_target_follow 1.10，ZZMI y71）、
#       y=mouseYDir 1.0（ZZMI z71）、z=目标跟随 follow（phys_release_kick 0.12，
#       ZZMI w71）、w=mouseXDir 1.0（ZZMI y73）
EFMI_POLISH_IP = 162
# 163 = 倍率扩展：x=mult_radius 1.0（ZZMI y72）、y=mult_spring 0.333（ZZMI w72）、
#       z=depth_pull 1.0（ZZMI z73，拖拽距离比例 × 冻结法线）
EFMI_MULT_EXTRA_IP = 163
# 164 = 时间步：x=sim_speed 3.0（$全局，ZZMI y76）、y=max_step 3.0（ZZMI z76）
EFMI_TIME_IP = 164
# 165 = 释放动态阻尼：x=release_boost 1.05（ZZMI x97）、y=release_decay 0.92（ZZMI y97）
EFMI_RELEASE_BOOST_IP = 165

# ---- F4 变量联动（值区/模式区在 EFMI 扩展区高位，与 150-165 探测/物理区不相交）----
EFMI_VAR_SYNC_VALUE_BASE = 166
EFMI_VAR_SYNC_MODE_BASE = 175
EFMI_SKD_MAX_BINDINGS = 36  # 值区 166-174 ×4 通道 + 模式区 175-183 ×4 通道

# ---- 手型光标（Present 手部绘制；扩展区 184+，与 150-183 各功能区不相交）----
# align-t3：ZZMI rzm_jiggle_hand 参数面（83/89-95/98）映射到 EFMI 187-195
# （槽位号保留 EFMI 高位区，参数语义/默认值与 ZZMI persist 全表一致）：
#   184 = 屏幕（res_width/res_height，既有）
#   187 = 表面 3 项（clip 1 / lift 0.018 / softness 0.020，ZZMI 83）
#   188 = 手型包围盒中心 xyz（ZZMI 89，persist）
#   189 = 缩放 0.5 + 不透明度 1.0（ZZMI 90，persist）
#   190 = LMB 归一蓄力 / time / RMB 归一蓄力（ZZMI 91）
#   191 = 倾斜 4 项（LMB min 10 / RMB min 10 / LMB max 45 / RMB max 45，ZZMI 92，persist）
#   192 = 振动 4 项（阈值 0.5 / 慢周期 0.8 / 快周期 0.1 / 幅度 4.0，ZZMI 93，persist）
#   193 = 直立防翻 80° + 抓取拉伸倾斜 max 45°（ZZMI 94，persist）
#   194 = 参考分辨率高度 2160（ZZMI 95，persist）
#   195 = 描边宽 2.0 / 描边段旗标 / 描边不透明度 1.0（ZZMI 98）
#   （ZZMI 96 假光照生成器不发射（shader 兜底 0.4/0.6/0.65 + ambient 0.55），EFMI 同）
EFMI_HAND_IP_BASE = 184
EFMI_HAND_SURFACE_IP = 187
EFMI_HAND_CENTER_IP = 188
EFMI_HAND_SCALE_IP = 189
EFMI_HAND_STATE_IP = 190
EFMI_HAND_TILT_IP = 191
EFMI_HAND_VIBRATE_IP = 192
EFMI_HAND_UPRIGHT_IP = 193
EFMI_HAND_REFERENCE_IP = 194
EFMI_HAND_OUTLINE_IP = 195
# 手型参数 ZZMI 预设默认值（persist 可调；zzmi-drag-baseline-spec §1.4 L4799-4827）
EFMI_HAND_SURFACE_CLIP = 1.0
EFMI_HAND_SURFACE_LIFT = 0.018
EFMI_HAND_SURFACE_SOFTNESS = 0.020
# 手部网格包围盒中间偏移（共享资产常量，P3F_C4F 网格局部空间）；
# align-t3：y 归并 ZZMI 0.275962（旧 0.275967 差 5e-6，顺手项）
EFMI_HAND_CENTER = (-0.000614, 0.275962, 0.065117)
EFMI_HAND_SCALE = 0.5          # ZZMI hand_scale persist 0.5（VS 再乘 0.72 → 有效 0.36）
EFMI_HAND_OPACITY = 1.0
EFMI_HAND_TILT_MIN = 10.0      # LMB 蓄力 min（同时是抓取拉伸倾斜的 floor）
EFMI_HAND_TILT_RMB_MIN = 10.0
EFMI_HAND_WINDUP_TIME = 1.0
EFMI_HAND_RMB_WINDUP_TIME = 1.0
EFMI_HAND_TILT_MAX = 45.0
EFMI_HAND_TILT_RMB_MAX = 45.0
EFMI_HAND_TILT_GRAB_MAX = 45.0
EFMI_HAND_VIBRATE_THRESHOLD = 0.5
EFMI_HAND_VIBRATE_PERIOD_SLOW = 0.8
EFMI_HAND_VIBRATE_PERIOD_FAST = 0.1
EFMI_HAND_VIBRATE_AMPLITUDE = 4.0
EFMI_HAND_UPRIGHT_MAX = 80.0
EFMI_HAND_OUTLINE_WIDTH = 2.0
EFMI_HAND_OUTLINE_OPACITY = 1.0
EFMI_HAND_REFERENCE_HEIGHT = 2160.0
EFMI_HAND_INDEX_COUNT = 1524


def _fmt(value):
    """ini 数值格式化：整数不带小数点，浮点最短表示。"""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return ("%g" % value)
    return str(value)


def _safe_float(value, default=0.0):
    """防御性 float 转换（None/空串/异常 → default）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN → default


def _normalize_var_name(text, fallback="var"):
    """变量名清洗（独立实现；语义对齐 variable_registry.normalize_variable_name）。"""
    safe = re.sub(r"\s+", "_", str(text or "").strip())
    safe = re.sub(r"[^a-zA-Z0-9_]", "", safe)
    if safe and safe[0].isdigit():
        safe = "_" + safe
    return safe or fallback


# ---------------------------------------------------------------------------
# 权重烘焙（纯 numpy，独立实现；align-t3：测地 Dijkstra 自实现，无 scipy）
# ---------------------------------------------------------------------------


def _ball_local_dist2(pos, center, shape):
    """椭球局部距离平方 d² = Σ_local_i²（ZZMI _to_ball_local 同语义，t28）。

    shape（= 区域配置元组第 2 元素）二选一：
    - 标量 r → 各向同性球 d² = |(p−c)|² / r²（向后兼容，旧数据/显式 radius）；
    - (3,3) lin3 = 旋转×非均匀缩放线性矩阵（列 = 缩放后局部轴，等价存 ball
      局部矩阵，对齐 ZZMI ball_matrix 上半 3×3）→
      d² = (p−c)ᵀ (lin3ᵀ·lin3)^{-1} (p−c) = |lin3^{-1}(p−c)|²。
    矩阵不可逆（某轴零缩放）返回 None。"""
    pos = np.asarray(pos, dtype=np.float64).reshape(-1, 3)
    c = np.asarray(center, dtype=np.float64).reshape(3)
    rel = pos - c
    s = np.asarray(shape, dtype=np.float64)
    if s.ndim == 0 or (s.ndim == 1 and s.size == 1):
        r = max(float(s), 1e-6)
        return np.einsum("ij,ij->i", rel, rel) / (r * r)
    lin = s.reshape(3, 3)
    try:
        m_inv = np.linalg.inv(lin)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(m_inv)):
        return None
    local = rel @ m_inv.T
    return np.einsum("ij,ij->i", local, local)


def _shape_field(d, strength, falloff_k, plateau=0.0):
    """统一衰减形状（ZZMI `_shape_field` L2957-2970 同款移植，独立实现）：
    plateau>0 → 平台化（d≤平台满强度，边缘 smoothstep 平滑过渡）；
    否则高斯 strength·exp(−k·d²)；d≥1 或不可达（inf/NaN）硬截止 0。
    欧氏/测地距离共用。"""
    d = np.asarray(d, dtype=np.float64)
    if plateau is not None and float(plateau) > 0.0:
        edge = float(plateau)
        t = np.clip((d - edge) / max(1.0 - edge, 1e-6), 0.0, 1.0)
        s = t * t * (3.0 - 2.0 * t)
        field = float(strength) * (1.0 - s)
    else:
        field = float(strength) * np.exp(-max(float(falloff_k), 1e-6) * d * d)
    field[d >= 1.0] = 0.0
    field[~np.isfinite(d)] = 0.0
    return field


def bake_zone_ball(positions, center, radius, strength=1.0, falloff_k=EFMI_BALL_FALLOFF_K,
                   grabbable=True, plateau=0.0):
    """单区高斯球/椭球权重场（t25 全面 zzmi 化 + t28 椭球化 + t36-P5 grabbable 语义对齐
    + align-t3 plateau 平台化）：

    - 归一化局部距离由 _ball_local_dist2 计算：传标量 radius → 各向同性球
      d = |p−c|/r（向后兼容）；传 (3,3) lin3 矩阵 → 旋转+非均匀缩放椭球
      d² = Σ((p−c)/r_i)²（ZZMI ball_matrix 同语义）；
    - 衰减形状 = _shape_field（ZZMI 同款）：plateau>0 平台化（节点级
      mask_plateau，ZZMI L781-784/L2961-2965），否则高斯
      field = strength × exp(-falloff_k × d²)；d ≥ 1 硬截止；
    - t36-P5（ZZMI ZoneGrabbable 语义对齐，t33 研究 §1.1 ⚠️ 差异修正）：
      **grabbable 参数不再清零权重**——不可抓区仍烘焙权重、可命中/可显示，
      抓取拒绝移到运行时：simulate 读 ZoneParams[z*2+1].y（grabbable 槽）拒绝
      抓取（ZZMI rzm_jiggle_screen_state L78-83 同款；EFMI 无 LMB+RMB 手势
      组合概念 → grabbable=0 直接拒绝本线程 grabbing）。
      参数保留仅为向后兼容（调用方仍传入；函数体内无分支作用）。
    """
    pos = np.asarray(positions, dtype=np.float64)
    d2 = _ball_local_dist2(pos, center, radius)
    if d2 is None:
        # 不可逆（某轴零缩放）→ 场 0（空物体本身无体积）
        return np.zeros(len(pos), dtype=np.float64)
    d = np.sqrt(np.maximum(d2, 0.0))
    return _shape_field(d, strength, falloff_k, plateau)


def bake_sparse_weights(positions, zone_configs, k=EFMI_ZONES_PER_VERTEX, slot_ids=None,
                        plateau=0.0, zone_fields=None):
    """稀疏权重烘焙（256 区模型，每顶点最多 K 个活动区）。

    返回 (zone_ids (N,K) uint32, weights (N,K) float32)：
    - 各区权重 = bake_zone_ball（zzmi 高斯球/椭球纯叠加——每区空物体即独立
      高斯球（t28：区域配置元组第 2 元素 lin3 矩阵 → 旋转+非均匀缩放椭球），
      多区按最强 K 选取即 merge 语义的稀疏形式，无 UV/无侧向门控）；
      align-t3：plateau 传入 _shape_field 平台化分支（ZZMI mask_plateau 同款）；
    - align-t3：`zone_fields`（可选）= 调用方预计算的逐区场列表（与
      zone_configs 同序；元素为 (N,) 场或 None=该区跳过）——导出驱动侧用
      （测地扩散/包含过滤/平台化在 _compute_zone_fields 完成）；给定后本函数
      不再走 bake_zone_ball 体积欧氏路径。缺省 None = 旧行为（纯函数测试/
      无拓扑调用不受影响）；
    - 每顶点取最强 K 个（权重 > 0；不足 K 的槽 0xFFFFFFFF / 0.0）——与 ZZMI
      逐区增量「最弱槽替换」（_write_jiggle_masks L2331-2339）结果等价
      （两侧终态都是该顶点场值 Top-K 集合；落槽顺序对下游消费无感：
      detect 插值取最强、deform 全 K 对累加）；
    - slot_ids（可选，t29 稳定 zone id）：给定每个 config 的**稳定 zone_id**
      落槽（zones.buf 槽 = 持久化 zone_id，物理重排/增删不漂移，与面板/形态键
      联动的 drag_zone_id/click_zone_id 引用一致）；缺省 = config 序（0..N-1，
      与旧行为一致，纯函数测试/无稳定 id 调用不受影响）；
    - **t16/zzmi 对齐：存原始场值（strength×exp(-falloff_k·d²)），不归一**——
      归一化会把单区权重压成 1.0（球内无衰减 = 整体一块硬边界），zzmi 存
      原始场（mask=sparseWeights[slot]，saturate 后直接用），拖拽强度随距离
      衰减（有 falloff 渐变）。
    """
    pos = np.asarray(positions, dtype=np.float64)
    n = len(pos)
    if slot_ids is None:
        slots = list(range(len(zone_configs)))
    else:
        slots = [int(s) for s in slot_ids]
        if len(slots) != len(zone_configs):
            # 防御：slot_ids 与 configs 脱节（config 被跳过）→ 显式失败而非静默错位
            raise ValueError(
                f"bake_sparse_weights slot_ids 长度 {len(slots)} 与 configs "
                f"{len(zone_configs)} 不一致——config 枚举与稳定 id 枚举脱节"
            )
    if zone_fields is not None and len(zone_fields) != len(zone_configs):
        raise ValueError(
            f"bake_sparse_weights zone_fields 长度 {len(zone_fields)} 与 configs "
            f"{len(zone_configs)} 不一致——场枚举与配置枚举脱节"
        )
    dense = np.zeros((n, max(slots, default=-1) + 1), dtype=np.float64)
    for z, cfg in enumerate(zone_configs):
        slot = slots[z]
        if zone_fields is not None:
            field = zone_fields[z]
            if field is None:
                # 该区对本组件被跳过（包含过滤未命中/影响球无交集——ZZMI
                # _write_jiggle_masks L2310-2328 逐区 continue 同款）
                continue
            dense[:, slot] = np.asarray(field, dtype=np.float64)
        else:
            center, shape, strength, falloff_k, grabbable = cfg
            dense[:, slot] = bake_zone_ball(
                pos, center, shape, strength=strength, falloff_k=falloff_k,
                grabbable=grabbable, plateau=plateau,
            )
        # D6/t26：影响球与顶点包围盒无交集告警（ZZMI _evaluate_zone_field
        # L2917-2924 同款）——区域错位/坐标系错误时给出诊断而非静默全零。
        if float(dense[:, slot].max(initial=0.0)) < 1e-4:
            print(
                f"[EFMIDrag][WARNING] 区域 {slot} 影响球与顶点包围盒无交集 "
                f"(max field {float(dense[:, slot].max(initial=0.0)):.3g})——该区不产生拖拽，"
                "请检查区域空物体位置/尺寸或坐标系"
            )
    ids = np.full((n, k), 0xFFFFFFFF, dtype=np.uint32)
    weights = np.zeros((n, k), dtype=np.float32)
    for i in range(n):
        row = dense[i]
        active = np.flatnonzero(row > 0)
        if active.size == 0:
            continue
        take = active[np.argsort(row[active])[::-1][:k]]
        w = row[take]
        # 原始场值直接落槽（不除以总和——见 t16 说明）
        ids[i, :take.size] = take.astype(np.uint32)
        weights[i, :take.size] = w.astype(np.float32)
    return ids, weights


# ---------------------------------------------------------------------------
# 测地（沿表面传播）距离 + 表面 gizmo 帧（align-t3，ZZMI 同款算法独立实现——
# 语义镜像 toolkit/gb_core.py edges_from_triangles L211 / build_surface_
# adjacency L221 / surface_distances L241 与 rzm_object_detect.hlsl
# BuildGizmoAxes L73-101；本模块零 import 约束保持，不 import gb_core）
# ---------------------------------------------------------------------------


def _ball_local_points(pos, center, shape):
    """椭球局部坐标 (N,3) = lin3⁻¹·(p−c)（或标量半径球 (p−c)/r）；矩阵不可逆
    返回 None。测地距离的度量空间（d = |local|，球面 d=1）。"""
    pos = np.asarray(pos, dtype=np.float64).reshape(-1, 3)
    c = np.asarray(center, dtype=np.float64).reshape(3)
    rel = pos - c
    s = np.asarray(shape, dtype=np.float64)
    if s.ndim == 0 or (s.ndim == 1 and s.size == 1):
        r = max(float(s), 1e-6)
        return rel / r
    lin = s.reshape(3, 3)
    try:
        m_inv = np.linalg.inv(lin)
    except np.linalg.LinAlgError:
        return None
    if not np.all(np.isfinite(m_inv)):
        return None
    return rel @ m_inv.T


def edges_from_triangles(tri_indices):
    """从三角形索引提取去重边 (E,2) int64；空输入返回 (0,2)。
    （ZZMI gb_core.edges_from_triangles 同语义独立实现）。"""
    t = np.asarray(tri_indices, dtype=np.int64).reshape(-1, 3)
    if t.shape[0] == 0:
        return np.zeros((0, 2), dtype=np.int64)
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]], axis=0)
    e.sort(axis=1)
    return np.unique(e, axis=0)


def build_surface_adjacency(local_pts, edge_verts):
    """Dijkstra 邻接表（边权重 = 局部空间点对距离）。多区共享拓扑时逐区局部
    空间不同（lin3 各异），邻接表按区局部坐标构建（gb_core 同规）。"""
    pts = np.asarray(local_pts, dtype=np.float64).reshape(-1, 3)
    n = pts.shape[0]
    adj = [[] for _ in range(n)]
    edges = np.asarray(edge_verts, dtype=np.int64).reshape(-1, 2)
    if len(edges):
        mask = (edges[:, 0] >= 0) & (edges[:, 0] < n) & (edges[:, 1] >= 0) & (edges[:, 1] < n)
        edges = edges[mask]
        weights = np.linalg.norm(pts[edges[:, 0]] - pts[edges[:, 1]], axis=1)
        for (a, b), w in zip(edges.tolist(), weights.tolist()):
            adj[a].append((b, float(w)))
            adj[b].append((a, float(w)))
    return adj


def surface_distances(local_pts, edge_verts, seed_mask, adjacency=None):
    """多源 Dijkstra 表面距离（球局部坐标度量；ZZMI gb_core.surface_distances
    同语义独立实现）。

    种子顶点初始距离 = 其欧氏 |local|，其余顶点从 ∞ 沿网格边松弛；表面
    距离 ≥ 欧氏距离恒成立——球体积覆盖的背面/对侧因表面绕行距离 ≥1 自然
    拿到 0 权重（d≥1 硬截止在 _shape_field）。不可达顶点为 inf。
    d ≥ 1 即停止传播（权重为 0，无需继续；gb_core 同款早退）。
    """
    import heapq
    local_pts = np.asarray(local_pts, dtype=np.float64).reshape(-1, 3)
    n = local_pts.shape[0]
    dist = np.full(n, np.inf)
    adj = adjacency if adjacency is not None else build_surface_adjacency(local_pts, edge_verts)
    heap = []
    for i in np.nonzero(np.asarray(seed_mask, dtype=bool).reshape(-1))[0]:
        d0 = float(np.linalg.norm(local_pts[i]))
        dist[i] = d0
        heapq.heappush(heap, (d0, int(i)))
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u] + 1e-12 or d >= 1.0:
            continue  # 陈旧堆项，或已出球面半径（权重为 0，无需继续传播）
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v] - 1e-12:
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def compute_zone_gizmo_frames(positions, triangles, zone_configs):
    """每区烘焙表面 gizmo 帧（ZZMI BuildGizmoAxes 同构造，rzm_object_detect
    L73-101；align-t3 手型三轴基 + depth_pull 冻结法线的烘焙源）。

    返回与 zone_configs 同序的 [(normal, tangent, bitangent), ...]（导出空间
    3D 单位向量；手型 VS 轴规约：X=法线=掌心朝向、Y=切线=手指方向、
    Z=副切线=宽度）。构造：
    - 种子 = 椭球局部空间中离球心最近的表面顶点（与测地种子同规则）；
    - 法线 = 种子顶点关联面法线的面积加权平均（叉积累加，ZZMI n0+n1+n2
      同款求和语义；位置缓冲无法线通道 → 由 IB 拓扑几何重建）；
    - 切线 = normalize(cross(localRef, normal))、副切线 = normalize(cross(
      normal, tangent))，localRef = 与法线最不平行的固定局部轴（|n.z|<0.75
      取 Z，否则 |n.y|<0.75 取 Y，否则 X——ZZMI L89-90 同款）。
    拓扑缺失/种子退化 → 该区回退恒等帧（(1,0,0)/(0,1,0)/(0,0,1)，手型按模型
    局部轴定向）并告警；config 为 None（矩阵奇异）时同样回退恒等帧。
    """
    frames = []
    pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    n = len(pos)
    vertex_normals = None
    if triangles is not None and len(pos):
        t = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
        t = t[(t >= 0).all(axis=1) & (t < n).all(axis=1)]
        if len(t):
            vertex_normals = np.zeros((n, 3), dtype=np.float64)
            face_n = np.cross(pos[t[:, 1]] - pos[t[:, 0]], pos[t[:, 2]] - pos[t[:, 0]])
            for corner in range(3):
                np.add.at(vertex_normals, t[:, corner], face_n)
    for zi, cfg in enumerate(zone_configs):
        center, shape = cfg[0], cfg[1]
        local = _ball_local_points(pos, center, shape)
        seed = None
        if local is not None and vertex_normals is not None and n:
            d2 = np.einsum("ij,ij->i", local, local)
            seed = int(np.argmin(d2))
        normal = None
        if seed is not None:
            raw = vertex_normals[seed]
            norm = float(np.linalg.norm(raw))
            if norm > 1e-10:
                normal = raw / norm
        if normal is None:
            if vertex_normals is not None:
                print(
                    f"[EFMIDrag][WARNING] 区域 {zi} gizmo 帧退化（种子顶点无有效"
                    "几何法线），回退恒等帧（手型按模型局部轴定向）"
                )
            frames.append((
                np.array([1.0, 0.0, 0.0]),
                np.array([0.0, 1.0, 0.0]),
                np.array([0.0, 0.0, 1.0]),
            ))
            continue
        local_ref = (
            np.array([0.0, 0.0, 1.0]) if abs(normal[2]) < 0.75
            else (np.array([0.0, 1.0, 0.0]) if abs(normal[1]) < 0.75
                  else np.array([1.0, 0.0, 0.0]))
        )
        tangent = np.cross(local_ref, normal)
        tangent /= max(np.linalg.norm(tangent), 1e-12)
        bitangent = np.cross(normal, tangent)
        bitangent /= max(np.linalg.norm(bitangent), 1e-12)
        frames.append((normal, tangent, bitangent))
    return frames


def _smoothstep(edge0, edge1, x):
    """平滑阶跃（numpy 向量化）。"""
    t = np.clip((np.asarray(x, dtype=np.float64) - edge0) / max(edge1 - edge0, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# body→cloth 16 近邻高斯传递（EFMI 架构独有特性——D-2 用户拍板保留；
# ZZMI 无对应子系统。传递源 = body 直烘稀疏表（align-t3 起 body 侧已带
# 测地/plateau/包含/镜像语义，传递天然继承）；无 scipy → 均匀网格近邻自实现）
# ---------------------------------------------------------------------------

EFMI_CLOTH_NEIGHBORS = 16
EFMI_CLOTH_SIGMA = 0.018
EFMI_CLOTH_NEAR = (0.010, 0.060)


def nearest_neighbors_grid(points, queries, k, cell_size, chunk=4096):
    """均匀网格 k 近邻（无 scipy 依赖）。返回 (dists (Q,k), idx (Q,k) int64)。

    候选不足 k 的查询点：dists 保持 1e30、idx 为 0（调用方按 dist 截止过滤）。
    """
    points = np.asarray(points, dtype=np.float64)
    queries = np.asarray(queries, dtype=np.float64)
    q_count = len(queries)
    dists = np.full((q_count, k), 1e30)
    idx = np.zeros((q_count, k), dtype=np.int64)
    if points.size == 0 or queries.size == 0:
        return dists, idx
    bmin = points.min(axis=0)
    p_cell = np.floor((points - bmin) / cell_size).astype(np.int64)
    q_cell = np.floor((queries - bmin) / cell_size).astype(np.int64)
    cells = {}
    for i, c in enumerate(p_cell):
        cells.setdefault(tuple(c.tolist()), []).append(i)
    q_groups = {}
    for i, c in enumerate(q_cell):
        q_groups.setdefault(tuple(c.tolist()), []).append(i)
    for qc, q_ids in q_groups.items():
        candidates = []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for oz in (-1, 0, 1):
                    candidates.extend(cells.get((qc[0] + ox, qc[1] + oy, qc[2] + oz), ()))
        if not candidates:
            continue
        cand = np.asarray(candidates, dtype=np.int64)
        qa = np.asarray(q_ids, dtype=np.int64)
        for start in range(0, len(qa), chunk):
            qs = qa[start:start + chunk]
            delta = points[cand][None, :, :] - queries[qs][:, None, :]
            d = np.sqrt(np.einsum("nki,nki->nk", delta, delta))
            take = min(k, len(cand))
            rows = np.arange(len(qs))[:, None]
            if take < k:
                order = np.argsort(d, axis=1)[:, :take]
            else:
                order = np.argpartition(d, take - 1, axis=1)[:, :take]
                order = order[rows, np.argsort(d[rows, order], axis=1)]
            row_idx = qs[:, None]
            col_idx = np.arange(k)[None, :take]
            dists[row_idx, col_idx] = d[rows, order]
            idx[row_idx, col_idx] = cand[order]
    return dists, idx


def bake_cloth_sparse(body_positions, body_zone_ids, body_weights, cloth_positions,
                      k=EFMI_ZONES_PER_VERTEX,
                      neighbors=EFMI_CLOTH_NEIGHBORS,
                      sigma=EFMI_CLOTH_SIGMA,
                      near=EFMI_CLOTH_NEAR,
                      cell_size=0.05,
                      max_zones=EFMI_MAX_ZONES,
                      chunk=8192):
    """body 稀疏表 → cloth 稀疏表（t22 契约升级：256 区模型，纯空间；
    D-2 用户拍板保留的 EFMI 独有特性）。

    16 近邻高斯插值（近者权重高 + 距离衰减 cloth 只跟随近层）→ 每 cloth 顶点
    聚合区权重（跨近邻并集）→ 取最强 K 个。分块累加控制内存。"""
    body_pos = np.asarray(body_positions, dtype=np.float64)
    body_zone_ids = np.asarray(body_zone_ids)
    body_weights = np.asarray(body_weights)
    cloth_pos = np.asarray(cloth_positions, dtype=np.float64)
    m = len(cloth_pos)
    dists, ids = nearest_neighbors_grid(body_pos, cloth_pos, neighbors, cell_size)
    q = np.exp(-(dists / sigma) ** 2)
    q = q / np.maximum(q.sum(axis=1, keepdims=True), 1e-12)
    fade = (1.0 - _smoothstep(near[0], near[1], dists[:, 0]))[:, None]
    out_ids = np.full((m, k), 0xFFFFFFFF, dtype=np.uint32)
    out_w = np.zeros((m, k), dtype=np.float32)
    for start in range(0, m, chunk):
        end = min(start + chunk, m)
        acc = np.zeros((end - start, max_zones), dtype=np.float64)
        rows = np.arange(end - start)
        for j in range(neighbors):
            nb = ids[start:end, j]
            wj = q[start:end, j] * fade[start:end, 0]
            for jk in range(body_zone_ids.shape[1]):
                z = body_zone_ids[nb, jk]
                w = body_weights[nb, jk] * wj
                valid = (z != 0xFFFFFFFF) & (w > 0.0)
                if not valid.any():
                    continue
                np.add.at(acc, (rows[valid], z[valid]), w[valid])
        for i in range(end - start):
            row = acc[i]
            active = np.flatnonzero(row > 0)
            if active.size == 0:
                continue
            take = active[np.argsort(row[active])[::-1][:k]]
            w = row[take]
            if w.sum() <= 0:
                continue
            # t16/zzmi 对齐：与 body 稀疏表一致，存原始聚合场值（不归一——
            # cloth 跟随 body 的衰减梯度，单区不再压成 1.0）。q 是近邻插值
            # 权重（已归一），acc 是 body 原始场 × q × fade 的聚合，天然带衰减。
            out_ids[start + i, :take.size] = take.astype(np.uint32)
            out_w[start + i, :take.size] = w.astype(np.float32)
    return out_ids, out_w


def active_indices(weights):
    """权重最大通道 > 0 的顶点索引表（uint32）。"""
    w = np.asarray(weights)
    return np.flatnonzero(w.max(axis=1) > 0).astype(np.uint32)


def identity_index_buffer(count):
    """顶点号顺序的索引缓冲（探针 point-list 用）。"""
    return np.arange(int(count), dtype=np.uint32)


def nearest_vertex_ids(positions, centers):
    """每个中心的最近顶点 id。"""
    pos = np.asarray(positions, dtype=np.float64)
    ids = []
    for center in centers:
        d = np.sum((pos - np.asarray(center, dtype=np.float64)) ** 2, axis=1)
        ids.append(int(np.argmin(d)))
    return ids


def make_anchor_positions(centers, gizmo_frames=None):
    """每区 8 锚点 × N 区 (N*8,4) float32（w=1）（align-t3 起 4→8）：
    行 0-3 = 中心 + 恒等 3 轴 ±10mm（拖拽基，detect ComputeBasis 消费）；
    行 4-7 = 中心 + gizmo 3 轴（法线/切线/副切线，compute_zone_gizmo_frames
    烘焙，ZZMI BuildGizmoAxes 构造）±10mm（手型三轴基，hand_preview 投影消费）。
    gizmo_frames 缺省/缺项 → 恒等轴（手型按模型局部轴定向的回退）。"""
    rows = []
    for zi, center in enumerate(centers):
        base = np.asarray(center, dtype=np.float64)
        rows.append(base)
        for axis in np.eye(3):
            rows.append(base + axis * EFMI_ANCHOR_OFFSET_M)
        gizmo = None
        if gizmo_frames is not None and zi < len(gizmo_frames):
            gizmo = gizmo_frames[zi]
        if gizmo is None:
            gizmo = tuple(np.eye(3))
        rows.append(base)
        for axis in gizmo:
            rows.append(base + np.asarray(axis, dtype=np.float64) * EFMI_ANCHOR_OFFSET_M)
    out = np.zeros((len(rows), 4), dtype=np.float32)
    out[:, :3] = np.asarray(rows, dtype=np.float32)
    out[:, 3] = 1.0
    return out


def anchor_streams(anchor_ids, texcoords, blends):
    """锚点 texcoord/blend 流（按锚点序从源顶点复制）。返回 (tex (N,3) f32,
    blend (N,4) f32)。tex 补零到 3 列 = 12B/行，对齐
    [ResourceEFMIDragAnchorTexcoord] 声明 stride=12（C3/t13：原实现只拷 2 列
    = 8B/行，与 stride 12 声明不符 → 探针 vb1 stride-12 读取越界）。"""
    tex_raw = np.asarray(texcoords, dtype=np.float32)[anchor_ids]
    tex = np.zeros((tex_raw.shape[0], 3), dtype=np.float32)
    tex[:, :2] = tex_raw[:, :2]
    blend = np.asarray(blends, dtype=np.float32)[anchor_ids]
    return tex, blend


# ---------------------------------------------------------------------------
# ini 解析/写回（独立实现，与 zzmi 不共享代码）
# ---------------------------------------------------------------------------


def _split_tail_content(content):
    """识别公共 auto-appended 尾块并把它们与主体分离（字面量契约）。"""
    text = str(content or "")
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = str(line).strip()
        if stripped in _AUTO_APPENDED_MARKERS or any(
            stripped.startswith(prefix) for prefix in _AUTO_APPENDED_MARKER_PREFIXES
        ):
            return text[:offset], text[offset:]
        offset += len(line)
    return text, ""


def _split_anim_driver_block(content):
    """从 ini 顶部剥离动画驱动块（防重复 [Present]/[Constants] 合并）。"""
    text = str(content or "")
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if _ANIM_DRIVER_BEGIN in line), None
    )
    if start is None:
        return "", text
    end = next(
        (i for i in range(start + 1, len(lines)) if _ANIM_DRIVER_END in lines[i]), None
    )
    if end is None:
        return "", text
    driver = "".join(lines[start:end + 1])
    before = "".join(lines[:start]).rstrip("\r\n")
    after = "".join(lines[end + 1:]).lstrip("\r\n")
    return driver, "\n\n".join(part for part in (before, after) if part)


def _strip_marked_block(lines, begin_marker, end_marker):
    """按 BEGIN/END 标记剥离块（支持多次出现；end 缺失时剥离到段尾）。"""
    cleaned = []
    index = 0
    while index < len(lines):
        if begin_marker not in str(lines[index]):
            cleaned.append(lines[index])
            index += 1
            continue
        end = next(
            (candidate for candidate in range(index + 1, len(lines))
             if end_marker in str(lines[candidate])),
            None,
        )
        index = len(lines) if end is None else end + 1
    return cleaned


def _strip_efmi_tail(tail):
    """从尾块剥离 EFMI 分支历史内容：TAIL_MARKER 块 + 搬迁进 UI [Present] 的
    PRESENT 块（按 BEGIN..END 标记；TAIL 块剥离到下一个公共 marker 或尾）。"""
    lines = str(tail or "").splitlines(keepends=True)
    out = []
    mode = None  # None / 'tail' / 'present'
    for line in lines:
        s = str(line).strip()
        if mode == 'present':
            if EFMI_PRESENT_END in s:
                mode = None
            continue
        if mode == 'tail':
            if s in _AUTO_APPENDED_MARKERS or any(
                s.startswith(prefix) for prefix in _AUTO_APPENDED_MARKER_PREFIXES
            ):
                mode = None
                out.append(line)
            continue
        if EFMI_PRESENT_BEGIN in s:
            mode = 'present'
            continue
        if EFMI_TAIL_MARKER in s:
            mode = 'tail'
            continue
        out.append(line)
    return "".join(out)


def _strip_hook_blocks(lines):
    lines = _strip_marked_block(lines, EFMI_HOOK_BEGIN, EFMI_HOOK_END)
    return _strip_marked_block(lines, EFMI_PROBE_BEGIN, EFMI_PROBE_END)


def _strip_present_blocks(lines):
    return _strip_marked_block(lines, EFMI_PRESENT_BEGIN, EFMI_PRESENT_END)


def _parse_key_value(line):
    """解析 'key = value' 行，返回 (key, value) 或 (None, None)。"""
    s = str(line).strip()
    if "=" not in s:
        return None, None
    key, _, value = s.partition("=")
    return key.strip(), value.strip()


def _section_key(name):
    """'[Section]' → 'Section'。"""
    return str(name).strip().strip("[]")


# ---------------------------------------------------------------------------
# 区域字段扩展消费（align-t3，ZZMI 区域定义同源字段的 EFMI 消费面；
# 基准规格 §3：SSMT_DragZoneSettings 11 字段，EFMI 此前只消费 5 个）
# ---------------------------------------------------------------------------

# EntryPoint/绘制段内的网格名注释（ui/universal/efmi.py 发射；
# `; [mesh:name1,name2] [vertex_count:N]` 或独立 `; [mesh:name1,name2]`）
_MESH_COMMENT_RE = re.compile(r"\[mesh:([^\]]+)\]")


def _zone_propagate(settings):
    """球级沿表面扩散开关（ZZMI `_zone_propagate` L219-224 同款）：旧工程
    （无 propagate 属性）默认开启。"""
    value = getattr(settings, "propagate", None)
    if value is not None:
        return bool(value)
    return True


def _zone_include_names(settings):
    """包含列表内物体的候选名集合（ZZMI `_zone_allowed_names` L227-249 的
    EFMI 等价：EFMI 组件 mesh 注释 = Blender 物体名直写（无 ZZMI 合并网格
    运行时后缀族），故只做精确名收集，不做后缀剥离）。"""
    include = getattr(settings, "include_objects", None) or ()
    names = set()
    for item in include:
        obj = getattr(item, "object", None)
        if obj is None:
            continue
        for candidate in (getattr(obj, "name", None), getattr(obj, "name_full", None)):
            if candidate:
                names.add(str(candidate))
    return names


def _zone_has_include_list(settings):
    """该区域是否配置了非空包含物体列表（ZZMI `_zone_has_included_objects`
    L268-276 同款）。"""
    include = getattr(settings, "include_objects", None)
    if include is None:
        return False
    try:
        return len(include) > 0
    except (TypeError, ValueError):
        return False


def _zone_component_allowed(settings, mesh_names):
    """组件级包含过滤（ZZMI `_zone_allowed_vertex_mask` 的 EFMI 等价粒度）：

    - 包含列表为空 → 允许（None 语义 = 全允许，不过滤）；
    - 列表非空 → 仅当本组件 mesh 名集合与包含名单有交集时允许；
    - 列表项物体指针全失效（names 空）→ 允许（与 ZZMI 预览侧
      `_zone_allowed_by_target` L279-290 同规约：避免误过滤清零）。
    """
    if not _zone_has_include_list(settings):
        return True
    allowed_names = _zone_include_names(settings)
    if not allowed_names:
        return True
    component_names = {str(n) for n in (mesh_names or ())}
    return bool(component_names & allowed_names)


# ---------------------------------------------------------------------------
# 主色 pass ps hash 动态识别（t48：导入/导出顺带从 FrameAnalysis 反查；
# 独立实现，不依赖 common/efmi_skeleton.py——AST 隔离与零 import 约束保持）
# ---------------------------------------------------------------------------

_PS_DUMP_RE = re.compile(
    r"^(\d{6}) 3DMigoto Dumping .+\\\d{6}-.+?-ps=([0-9a-f]{8,16})\.(?:buf|txt)(?: -> |$)"
)
_IB_DUMP_RE = re.compile(
    r"^(\d{6}) 3DMigoto Dumping .+\\\d{6}-ib=([0-9a-f]{8})-"
)
_O0_DUMP_RE = re.compile(
    r"^(\d{6}) 3DMigoto Dumping .+\\\d{6}-o0=([0-9a-f]{8})-"
)
# 识别侧 NumViews 解析（§10.6 整合：NumViews 判定保留在识别链——识别主色
# pass 用；门控不用 NumViews——门控用 pass 计数器）
_OMSET_NUMVIEWS_RE = re.compile(r"^(\d{6}) OMSetRenderTargets\(NumViews:(\d+)")


def _parse_draw_numviews(log_path):
    """每个 draw 的最终 NumViews（绘制前最后设置的 OMSetRenderTargets 值；
    深度/阴影 pass NumViews:0 无 RT、主色 pass NumViews>0、多 RT pass >1）。"""
    out = {}
    if not os.path.isfile(log_path):
        return out
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _OMSET_NUMVIEWS_RE.match(line.strip())
            if m:
                out[m.group(1)] = int(m.group(2))
    return out
_DXGI_R11G11B10_FLOAT = 26
_DXGI_R10G10B10A2_UNORM = 24
_MAIN_COLOR_FORMATS = {_DXGI_R11G11B10_FLOAT, _DXGI_R10G10B10A2_UNORM}


def _dds_dxgi_format(dds_path):
    """读取 dds 头部 DXGI 格式（DX10 扩展头）；无法识别返回 None。"""
    try:
        with open(dds_path, "rb") as f:
            head = f.read(148)
    except OSError:
        return None
    if len(head) < 128 or head[:4] != b"DDS ":
        return None
    fourcc = head[84:88]
    if fourcc == b"DX10":
        if len(head) < 148:
            return None
        return int.from_bytes(head[128:132], "little")
    return None


def _is_main_color_format(fmt):
    return fmt in _MAIN_COLOR_FORMATS


def identify_main_color_ps_hashes(log_path, ib_hashes):
    """诊断/识别用途（§10.6 门控定案后不驱动门控——ps hash 表达式在 Zmd
    构建不可用；ordinal 统计由 identify_pass_ordinal 驱动阈值）：
    从 FrameAnalysis log.txt 反查目标组件（ib hash 匹配）的主色 pass ps hash 集合。

    - draw 记录：dump 行 `NNNNNN-...-ps=HASH.buf` → (draw, ps_hash)；
      `NNNNNN-ib=HASH-...` → (draw, ib_hash)；`NNNNNN-o0=HASH-...` → (draw, o0_rt_hash)；
    - 候选：ib hash ∈ ib_hashes 的 draw；
    - 主色 RT 判定（§10.6 整合：识别侧保留 NumViews 判定——主色 pass =
      NumViews > 0 且 o0 存在且主色格式（R11G11B10_FLOAT/R10G10B10A2_UNORM，
      GDZGF 实证 o0=R11G11B10_FLOAT）；深度/阴影 pass NumViews:0 无 RT 排除；
      deduped 文件不可读格式时回退「o0 存在 且 NumViews > 0」）；
    - 返回去重后的 ps hash 列表（多 pass 多值合并）。"""
    if not os.path.isfile(log_path):
        return []
    base_dir = os.path.dirname(os.path.abspath(log_path))
    draw_ps = {}
    draw_ib = {}
    draw_o0 = {}
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            # 同一 dump 行可同时含 -ib= 与 -ps=：逐正则尝试，不用 continue 跳过早匹配
            m = _IB_DUMP_RE.match(s)
            if m:
                draw_ib.setdefault(m.group(1), m.group(2))
            m = _O0_DUMP_RE.match(s)
            if m:
                draw_o0.setdefault(m.group(1), m.group(2))
            m = _PS_DUMP_RE.match(s)
            if m:
                draw_ps[m.group(1)] = m.group(2)
    target_ib = {str(h).lower() for h in ib_hashes if h}
    draw_numviews = _parse_draw_numviews(log_path)
    found = []
    for draw, ps_hash in draw_ps.items():
        if draw not in draw_ib:
            continue
        if draw_ib[draw] not in target_ib:
            continue
        # 识别侧主色判定（NumViews>0 + o0 主色格式；NumViews 无记录时缺省视为
        # 有 RT（旧帧分析无 OMSet 行））
        if draw_numviews.get(draw, 1) < 1:
            continue
        o0_hash = draw_o0.get(draw)
        if not o0_hash:
            continue
        is_main = True
        dds = os.path.join(base_dir, "deduped", o0_hash + ".dds")
        fmt = _dds_dxgi_format(dds)
        if fmt is not None:
            is_main = _is_main_color_format(fmt)
        if is_main:
            found.append(ps_hash)
    seen = []
    for h in found:
        if h not in seen:
            seen.append(h)
    return seen


def _latest_frame_analysis_dir(workspace_root):
    """工作空间下最新 FrameAnalysis-* 目录（无则 None）。"""
    if not os.path.isdir(workspace_root):
        return None
    candidates = []
    try:
        for entry in os.scandir(workspace_root):
            if entry.is_dir() and entry.name.startswith("FrameAnalysis-"):
                candidates.append(entry.path)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0)
    return candidates[-1]


def _resolve_frame_analysis_dir(workspace_root):
    """FrameAnalysis 目录定位：Config/FrameAnalysisPath.json → 最新 FrameAnalysis-*。"""
    config_path = os.path.join(workspace_root, "Config", "FrameAnalysisPath.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            payload = {}
        path = payload.get("path") or payload.get("frame_analysis_dir")
        if path and os.path.isdir(path):
            return path
    return _latest_frame_analysis_dir(workspace_root)


def _workspace_probe_hash_path(workspace_root):
    return os.path.join(workspace_root, "Config", "DragProbePassHash.json")


def load_workspace_probe_hash(workspace_root):
    """读取工作空间缓存（识别值 dict 或旧版纯 hash 字符串；无返回 None）。"""
    path = _workspace_probe_hash_path(workspace_root)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return None
    value = payload.get("ps_hashes")
    if not value:
        return None
    return {
        "ps_hashes": str(value).strip(),
        "depth_pass_count": payload.get("depth_pass_count"),
        "color_pass_count": payload.get("color_pass_count"),
        "color_pass_first_ordinal": payload.get("color_pass_first_ordinal"),
    }


def save_workspace_probe_hash(workspace_root, ps_hashes, source_dir="",
                              pass_counts=None):
    """缓存识别结果（幂等写 Config/DragProbePassHash.json）。

    t47 §10 起附带 pass 计数（depth_pass_count/color_pass_count/
    color_pass_first_ordinal——门控阈值由 first ordinal 生成）。"""
    if not workspace_root:
        return
    try:
        os.makedirs(os.path.join(workspace_root, "Config"), exist_ok=True)
        payload = {
            "ps_hashes": ", ".join(ps_hashes),
            "source": source_dir,
            "version": 2,
        }
        if pass_counts:
            payload["depth_pass_count"] = int(pass_counts.get("depth_pass_count", 0))
            payload["color_pass_count"] = int(pass_counts.get("color_pass_count", 0))
            payload["color_pass_first_ordinal"] = int(
                pass_counts.get("color_pass_first_ordinal", 0))
        with open(_workspace_probe_hash_path(workspace_root), "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as exc:
        print(f"[EFMIDrag][WARNING] 主色 pass hash 缓存写失败: {exc}")


def identify_pass_ordinal(log_path, ib_hashes):
    """识别侧 pass 序统计（§10.6 整合：识别用 NumViews 判定，门控不用）。

    - 按 log 顺序给每个目标组件 draw 标号（1 基）；
    - 主色判定 = NumViews > 0（深度/阴影 pass NumViews:0 无 RT 排除；NumViews
      无记录缺省视为有 RT）+ o0 dump 存在且 deduped dds 主色格式（不可读回退
      o0 存在 且 NumViews > 0）；
    - 返回 (depth_pass_count, color_pass_count, color_pass_first_ordinal)
      ——GDZGF 实证 (2, 3, 3)：深度 pass 恒 1/2 次、颜色 pass 恒 3-5 次；
      无目标命中返回 (0, 0, 0)。"""
    if not os.path.isfile(log_path):
        return (0, 0, 0)
    base_dir = os.path.dirname(os.path.abspath(log_path))
    draw_ib = {}
    draw_o0 = {}
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            m = _IB_DUMP_RE.match(s)
            if m:
                draw_ib.setdefault(m.group(1), m.group(2))
            m = _O0_DUMP_RE.match(s)
            if m:
                draw_o0.setdefault(m.group(1), m.group(2))
    draw_numviews = _parse_draw_numviews(log_path)
    target_ib = {str(h).lower() for h in ib_hashes if h}
    ordinal = 0
    depth = 0
    color = 0
    first_color = 0
    for draw, ib_hash in draw_ib.items():
        if ib_hash not in target_ib:
            continue
        ordinal += 1
        o0_hash = draw_o0.get(draw)
        # 识别侧主色判定：NumViews > 0 且 o0 主色格式（AND；格式不可读回退
        # o0 存在 且 NumViews > 0）
        is_main = draw_numviews.get(draw, 1) >= 1
        if o0_hash:
            fmt = _dds_dxgi_format(os.path.join(base_dir, "deduped", o0_hash + ".dds"))
            if fmt is not None:
                is_main = is_main and _is_main_color_format(fmt)
        else:
            is_main = False
        if is_main:
            color += 1
            if first_color == 0:
                first_color = ordinal
        else:
            depth += 1
    return (depth, color, first_color)


def resolve_probe_pass_hash(node, workspace_root, ib_hashes):
    """【§10.6 定案】兼容保留（诊断/手动校验用途）——门控采用 pass 计数器后
    不再驱动任何门控；本函数仅用于手动校验/日志：
    1) 节点属性非空 → 手动覆盖值；
    2) 属性空 → 工作空间识别值；
    3) 无缓存 → 现场从 FrameAnalysis 识别并写缓存；
    4) 识别失败 → 默认多值 + 警告。"""
    manual = str(getattr(node, "efmi_probe_pass_hash", "") or "").strip()
    if manual:
        return manual
    cached = load_workspace_probe_hash(workspace_root)
    if cached:
        return cached.get("ps_hashes")
    frame_dir = _resolve_frame_analysis_dir(workspace_root)
    if frame_dir:
        hashes = identify_main_color_ps_hashes(
            os.path.join(frame_dir, "log.txt"), ib_hashes
        )
        if hashes:
            pass_counts = identify_pass_ordinal(
                os.path.join(frame_dir, "log.txt"), ib_hashes
            )
            save_workspace_probe_hash(
                workspace_root, hashes, source_dir=frame_dir,
                pass_counts={
                    "depth_pass_count": pass_counts[0],
                    "color_pass_count": pass_counts[1],
                    "color_pass_first_ordinal": pass_counts[2],
                },
            )
            return ", ".join(hashes)
    print(
        f"[EFMIDrag][WARNING] 未能自动识别主色 pass ps hash（FrameAnalysis "
        f"缺失或无组件命中）；使用默认多值门控 "
        f"'{DEFAULT_PROBE_PASS_HASH}'——若探针不运行请手动填写或提供含角色绘制的 FrameAnalysis"
    )
    return DEFAULT_PROBE_PASS_HASH


# ---------------------------------------------------------------------------
# 执行器
# ---------------------------------------------------------------------------


class DragInteractionEFMIExporter:
    """EFMI 拖拽交互独立分支执行器。

    由 zzmi 节点类 execute_postprocess 单入口在 GlobalConfig.logic_name == EFMI
    时分发调用；节点 UI 属性经 getattr 共享读取（属性名与 zzmi 节点一致或 EFMI
    专属），导出逻辑与资源完全独立。
    """

    # 默认运行模式变量（面板联动契约：复用 $ssmtdrag_ui_detected/zone，与
    # 面板生成器零改动——研究③ §6 决策点）
    _RUNTIME_VARIABLE_DEFAULTS = {
        "drag_mode_variable_name": "ssmtdrag_drag_enabled",
        "ui_detected_variable_name": "ssmtdrag_ui_detected",
        "ui_zone_variable_name": "ssmtdrag_ui_zone",
    }

    def __init__(self, node):
        self.node = node
        # ---- 属性快照（防御读取，缺省 = 节点默认）----
        self.hash_values = str(getattr(node, "hash_values", "") or "")
        self.mod_namespace = str(getattr(node, "mod_namespace", "") or "")
        self.grab_key = str(getattr(node, "grab_key", "ALT") or "ALT")
        self.bake_reference_object = getattr(node, "bake_reference_object", None)
        self.zone_objects = getattr(node, "zone_objects", None) or ()
        # ARCH-07 memo：`_collect_enabled_zone_entries` 结果按"输入签名"缓存
        # （见 `_zone_input_signature`：按序的「目标物体身份 + enabled」）。签名变化
        # （区域增删/换序/启用禁用）即重算——首版只缓存不校验输入，导致禁用区重新
        # 启用后仍返回旧缓存（实测 KeyError），已修正。
        self._zone_entries_cache = None
        self._zone_entries_cache_signature = None
        # 注：`_collect_zone_configs` **不缓存**（其结果是空物体世界变换与
        # ssmt_drag_zone 参数的函数，这些量可在同一实例生命周期内被就地修改，
        # 任何身份型签名都无法可靠感知；且它在导出/预览链中不是热点）。

        # 门控定案（§10.6 pass 计数器）：efmi_probe_pass_hash 属性废弃——
        # 旧值（如 1718.1）读取时告警清理（不驱动门控；F3 迁移提示）；门控阈值
        # 由工作空间识别值（color_pass_first_ordinal）驱动
        try:
            from ..common.global_config import GlobalConfig
            workspace_root = str(getattr(GlobalConfig, "path_workspace_folder", lambda: "")() or "")
        except Exception:
            workspace_root = ""
        self._probe_workspace_root = workspace_root
        legacy_ps = str(getattr(node, "efmi_probe_pass_hash", "") or "").strip()
        if legacy_ps:
            print(
                f"[EFMIDrag][WARNING] efmi_probe_pass_hash 属性已废弃（门控改用 "
                f"pass 计数器，ps hash 表达式在 Zmd 构建不可用）：旧值 "
                f"'{legacy_ps}' 将被忽略，请清空该属性"
            )

        # ---- 物理完全跟随全局（t25 全面 zzmi 化：删除 EFMI 专属 9 项参数）----
        # align-t3：拖拽效果变量采用 ZZMI 预设值（基准规格 §2）——spring 直传
        # （0.176/0.055，不再 ω=√k→Hz 映射）、深度改 depth_pull 比例模型
        # （不再定值 Y 偏移）、dragScale 0.70→1.00、接入 mult_radius/follow/
        # 释放踢/sim_speed/max_step/释放动态阻尼。深度/命中阈值为固定契约常量
        # （不留节点属性）。
        self.depth_pull = DEFAULT_DEPTH_PULL
        self.drag_scale = DEFAULT_DRAG_SCALE
        self.hit_threshold = DEFAULT_HIT_THRESHOLD
        (self.grab_spring, self.grab_damping, self.release_spring, self.release_damping,
         self.mult_damping, self.mult_strength, self.mult_spring, self.mult_radius,
         self.release_kick, self.target_follow) = self._map_phys_profile()
        # max_offset：由区域参数（各配置区 ssmt_drag_zone.max_offset 最大值，
        # 0 = 继承回退契约基线）
        self.max_offset = self._map_max_offset()
        self.enable_hand_cursor = bool(getattr(node, "enable_hand_cursor", False))
        # t37-P3 三态运行模式初始值（0=关/1=仅命中/2=命中+拖拽；F8 运行时循环切换）。
        # ARCH-08 修正：真实节点属性是 `drag_system_mode_default`
        # （node_postprocess_draginteraction.py:656 "默认运行模式" 0..2，默认 2；
        # init 内已迁移为 drag_enabled_default ? 2 : 1）。旧代码读的
        # `efmi_drag_mode_default` **全仓不存在**（`git grep efmi_drag_mode_default`
        # 0 命中，也不在节点类 Property 列表里）→ getattr 恒取兜底 2，用户在面板
        # 上设的"默认运行模式"对 EFMI 分支完全无效。此处改读真实属性。
        #
        # 兜底顺序（新名优先，旧名仅防御性只读回退）：
        #   ① `drag_system_mode_default`（唯一真实注册属性，生产环境恒存在）；
        #   ② 缺失/None 时退回 `efmi_drag_mode_default` —— 该名从未注册为 bpy
        #      属性，因此**不可能存在于旧 .blend**，仅早期测试桩/外部脚本可能传它，
        #      保留只读兜底以免打断这类调用方；
        #   ③ 仍不可用 → 2（完整模式，与节点属性默认值一致，兼容旧行为）。
        try:
            raw_mode = getattr(node, "drag_system_mode_default", None)
            if raw_mode is None:
                raw_mode = getattr(node, "efmi_drag_mode_default", 2)
            self.drag_mode_default = min(2, max(0, int(2 if raw_mode is None else raw_mode)))
        except (TypeError, ValueError):
            self.drag_mode_default = 2

    def _map_max_offset(self):
        """最大位移由区域参数映射（t25：不留独立属性）——各配置区
        ssmt_drag_zone.max_offset 最大值，0 = 继承回退契约基线
        （R7 t46-P1：基线 = DEFAULT_MAX_OFFSET 0.50 = ZZMI 显式 POLISH_PARAMS.x，
        见 L115 上方注释）。运行时对应 ZZMI ZoneOffsetOverride
        （rzm_jiggle_screen_state.hlsl L343-345）：>0 逐区覆盖全局回退。

        ARCH-05：本函数**只遍历 `self.zone_objects` 读 max_offset，不调用
        `_collect_zone_configs()`** —— 后者会做全量区域枚举 + 矩阵运算（且经
        `_map_max_offset`→`__init__` 形成自引用），原先那句
        `for cfg in self._collect_zone_configs(): del cfg; break` 是纯粹的白跑
        一趟全量枚举后丢弃结果，已删除。
        """
        best = 0.0
        for item in self.zone_objects:
            obj = getattr(item, "zone_object", None)
            if obj is None:
                continue
            settings = getattr(obj, "ssmt_drag_zone", None)
            if settings is None:
                continue
            if not getattr(settings, "enabled", True):
                continue
            try:
                v = float(getattr(settings, "max_offset", 0.0) or 0.0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0.0:
                best = max(best, v)
        if best > 0.0:
            return best
        return DEFAULT_MAX_OFFSET

    def _probe_pass_threshold(self):
        """主色 pass 门控阈值（§10.6 pass 计数器定案）：工作空间识别值
        （DragProbePassHash.json 的 color_pass_first_ordinal）优先；无识别
        → 默认 3（实证深度 pass 恒 1/2 次、颜色 pass 恒第 3 次起）。"""
        try:
            workspace_root = self._probe_workspace_root
        except AttributeError:
            workspace_root = ""
        if workspace_root:
            cached = load_workspace_probe_hash(workspace_root)
            if cached:
                ordinal = cached.get("color_pass_first_ordinal")
                if ordinal:
                    try:
                        value = int(ordinal)
                    except (TypeError, ValueError):
                        value = 0
                    if value >= 1:
                        return value
        return DEFAULT_PROBE_PASS_THRESHOLD

    def _probe_exec_pass(self):
        """probe 执行 pass（t4/P0 用户裁决：**在颜色层执行**）。

        返回 = `_probe_pass_threshold()`（= 首个颜色 pass 的帧内 draw 序号；
        默认 3，有工作空间识别时为 color_pass_first_ordinal）。

        历史与本次裁决：
        - t17/问题① 曾取 threshold-1（= 最后一个深度 pass），理由是「pass3 的
          deform 在 probe 之前执行恒 stale → 壳恒 BASE 与主色层分离」；
        - 但 R8/P0 把门控退化为「每帧首次 body draw 帧 latch」后，probe 实际钉在
          每帧**第一次 draw = 深度预 pass / 非主相机 pass**，投影与 Present 注入的
          光标域不成套 → detect 恒 miss → 手型恒隐藏 + deform 不位移（t3 根因）；
        - t4 恢复 pass 门控时先按 threshold-1 落地，随后**用户明确裁决 probe 应在
          颜色层而非深度层执行** → 本函数改回返回 threshold，门控 `pass >= exec`
          即「帧内第一个颜色 pass 的 draw 之后」执行。

        帧内 pass 计数实测语义（`_inject_hooks`）：深度 pass 恒第 1/2 次
        （NumViews:0 无 RT）、颜色 pass 恒第 3-5 次（o0=3315d2b5 主色 RT）。
        故 threshold=3 → probe 在第一个颜色 pass 的 draw 后运行一次；同帧
        颜色层后续 draw（3-5）与帧末 Present（simulate/hand_preview/hand）全部
        消费到同帧 fresh 候选（Candidate.w == 当前帧）。
        """
        return max(1, int(self._probe_pass_threshold()))

    def _map_phys_profile(self):
        """全局物理档案 + 全局倍率 → EFMI 弹簧参数（align-t3：ZZMI 预设值直传）。

        - 弹簧/阻尼：节点属性**直传**（ZZMI PHYS_PARAMS 70 槽语义：
          grab_damping 0.86 / grab_spring 0.176 / release_damping 0.96 /
          release_spring 0.055；shader 侧半隐式积分 `v·damping^step +
          (filtered−x)·spring·step`——不再 ω=√k→Hz 映射，D-1 随目标②换模型）；
        - 目标跟随/释放踢：phys_release_kick=0.12（显示名「目标跟随」，ZZMI
          键名历史错位 L746-749）/ phys_target_follow=1.10（显示名「释放冲击」）；
        - 倍率：mult_damping（替换语义回退倍率，t42 P-2）/ mult_strength /
          mult_spring / mult_radius 四项分离快照（Present 独立发射 160/161/163）。
        - 返回 (grab_spring, grab_damping, release_spring, release_damping,
                mult_damping, mult_strength, mult_spring, mult_radius,
                release_kick, target_follow)。"""
        grab_spring = _safe_float(getattr(self.node, "phys_grab_spring", DEFAULT_GRAB_SPRING), DEFAULT_GRAB_SPRING) or DEFAULT_GRAB_SPRING
        release_spring = _safe_float(getattr(self.node, "phys_release_spring", DEFAULT_RELEASE_SPRING), DEFAULT_RELEASE_SPRING) or DEFAULT_RELEASE_SPRING
        grab_damping = _safe_float(getattr(self.node, "phys_grab_damping", DEFAULT_GRAB_DAMPING), DEFAULT_GRAB_DAMPING) or DEFAULT_GRAB_DAMPING
        release_damping = _safe_float(getattr(self.node, "phys_release_damping", DEFAULT_RELEASE_DAMPING), DEFAULT_RELEASE_DAMPING) or DEFAULT_RELEASE_DAMPING
        mult_spring = _safe_float(getattr(self.node, "mult_spring", 0.333), 0.333) or 0.333
        mult_damping = _safe_float(getattr(self.node, "mult_damping", 1.0), 1.0) or 1.0
        mult_strength = _safe_float(getattr(self.node, "mult_strength", 0.333), 0.333) or 0.333
        # align-t3 新消费节点属性（ZZMI 节点默认即预设值；getattr 缺省=ZZMI 默认）
        mult_radius = _safe_float(getattr(self.node, "mult_radius", 1.0), 1.0) or 1.0
        release_kick = _safe_float(getattr(self.node, "phys_target_follow", DEFAULT_RELEASE_KICK), DEFAULT_RELEASE_KICK) or DEFAULT_RELEASE_KICK
        target_follow = _safe_float(getattr(self.node, "phys_release_kick", DEFAULT_TARGET_FOLLOW), DEFAULT_TARGET_FOLLOW) or DEFAULT_TARGET_FOLLOW
        return (float(grab_spring), float(grab_damping),
                float(release_spring), float(release_damping),
                float(mult_damping), float(mult_strength),
                float(mult_spring), float(mult_radius),
                float(release_kick), float(target_follow))

    # =======================================================================
    # 等价谓词接口（供消费方兼容读取；独立实现，语义对齐 zzmi 节点方法）
    # =======================================================================

    def _feature_skd(self):
        """F1 形态键联动总开关（EFMI 语义：与 zzmi 相同的消费方约束）。"""
        node = self.node
        if bool(getattr(node, "enable_shapekey_drive", False)):
            return True
        if not bool(getattr(node, "feature_shapekey_link", True)):
            return False
        return self._has_shapekey_consumer()

    def _has_shapekey_consumer(self):
        """同树存在开启拖拽驱动的形态键节点，或点击计数导出绑定。"""
        tree = getattr(self.node, "id_data", None)
        if tree is not None:
            for n in getattr(tree, "nodes", None) or []:
                if (
                    getattr(n, "bl_idname", "") == "SSMTNode_PostProcess_ShapeKey"
                    and getattr(n, "drag_drive_enabled", False)
                ):
                    return True
        return bool(self._collect_click_export_drivers())

    def _collect_click_export_drivers(self, capacity=None, zones_only=False):
        """扫描同主树动画驱动蓝图的点击计数导出节点（跨树反扫，独立实现）。
        返回 [(zone_id, cycle_length, 首个受控变量名或"")]。

        容量口径（ARCH-02/B2 修正）：**稳定区域号是几就是几**——容量由
        `_collect_zone_capacity()`（= 最大被引用 zone_id + 1）决定，而非启用
        区域**个数**（对齐 zzmi `_zone_capacity` max(zone_id)+1）。zone >= 容量
        的条目过滤并告警（防 ClickExport 越界 store）。

        参数:
            capacity: 显式容量（缺省 = `_collect_zone_capacity()`，与
                `_drag_drive_buffer_layout` 同一口径——两侧必须一致）；
            zones_only: 只回被引用的 zone 集合（**不做容量过滤**）。供
                `_collect_zone_capacity` 自身收集"被点击导出引用的区域号"用，
                避免容量过滤与容量推导互相依赖（先有鸡还是先有蛋）。
        """
        entries = []
        tree = getattr(self.node, "id_data", None)
        if tree is None:
            return entries
        try:
            import bpy  # 仅跨树反扫需要（延迟 import；无 bpy 环境（纯逻辑测试/脚本）
            # 优雅降级为空——ClickExport 跨树扫描不可用，F1 消费方约束退化为
            # 同树形态键节点判定）
        except ImportError:
            return entries
        if capacity is None and not zones_only:
            capacity = self._collect_zone_capacity()

        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_AnimDriver":
                continue
            if getattr(node, "mute", False):
                continue
            anim_tree = getattr(bpy.data, "node_groups", None)
            if anim_tree is None:
                continue
            anim_tree = anim_tree.get(str(getattr(node, "blueprint_name", "") or ""))
            if anim_tree is None:
                continue
            for anim_node in getattr(anim_tree, "nodes", None) or []:
                if getattr(anim_node, "bl_idname", "") != "SSMTNode_AnimDriver_ClickExport":
                    continue
                if getattr(anim_node, "mute", False):
                    continue
                try:
                    zone = int(getattr(anim_node, "click_zone_id", -1))
                except (TypeError, ValueError):
                    zone = -1
                if zone < 0:
                    continue
                if not zones_only and zone >= capacity:
                    print(
                        f"[EFMIDrag][WARNING] ClickExport 点击区域 {zone} 超出 EFMI "
                        f"区域容量 {capacity}（最大被引用区域号 + 1，zone id 0-255），已跳过"
                    )
                    continue
                try:
                    cycle = int(getattr(anim_node, "cycle_length", 0) or 0)
                except (TypeError, ValueError):
                    cycle = 0
                # 循环档数钳制（与 zzmi 同款 min(64, ...)，终审 F3）：防止布局
                # 档位扩展越界/超大缓冲
                cycle = min(64, max(0, cycle))
                first_var = ""
                for target in getattr(anim_node, "click_target_list", None) or []:
                    name = _normalize_var_name(
                        getattr(target, "variable_name", "") or ""
                    )
                    if name:
                        first_var = f"${name}"
                        break
                entries.append((zone, cycle, first_var))
        return entries

    def _click_export_seed_entries(self):
        """冷启动播种条目 [(zone_id, $首个受控变量)]：同区域去重、最多 8 条、
        超限抛错（等价 zzmi 链）。"""
        entries = []
        seen = set()
        for zone, _cycle, first_var in self._collect_click_export_drivers():
            if not first_var:
                continue
            if zone in seen:
                continue
            seen.add(zone)
            entries.append((zone, first_var))
        if len(entries) > EFMI_SKD_MAX_SEED:
            raise ValueError(
                f"点击计数导出播种最多支持 {EFMI_SKD_MAX_SEED} 个区域，"
                f"当前为 {len(entries)} 个"
            )
        return entries

    def _drag_drive_var_sync_bindings(self):
        """收集「导出变量 → 驱动缓冲槽位」同步绑定（等价接口，独立实现）。
        返回 [(var_name, slot_id, zone_id, nd_stage), ...]：var_name 带 $ 前缀；
        nd_stage 为无方向档位数（>=1），方向形态键为 -1（烘焙映射 0xFFFFFFFF）。
        槽位与 _drag_drive_buffer_layout 的 CPU 前缀和一致；上限 36。"""
        tree = getattr(self.node, "id_data", None)
        if tree is None:
            return []
        _total_slots, zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
        bindings = []
        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_ShapeKey":
                continue
            if not getattr(node, "drag_drive_enabled", False):
                continue
            get_var_name = getattr(node, "get_shape_key_export_variable_name", None)
            if get_var_name is None:
                continue
            for item in getattr(node, "shapekey_variable_items", None) or []:
                if not getattr(item, "export_enabled", True):
                    continue
                try:
                    zone = int(getattr(item, "drag_zone_id", -1))
                except (TypeError, ValueError):
                    zone = -1
                if zone < 0 or zone >= len(zone_bases):
                    continue
                try:
                    dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
                except (TypeError, ValueError):
                    dir_val = -1
                if 0 <= dir_val < 4:
                    slot_id = zone_bases[zone] + dir_val
                    nd_stage = -1
                else:
                    try:
                        stage = int(getattr(item, "drag_click_stage", 1) or 1)
                    except (TypeError, ValueError):
                        stage = 1
                    nd_stage = max(1, stage)
                    slot_id = zone_bases[zone] + 4 + (nd_stage - 1)
                var_name = get_var_name(getattr(item, "shape_key_name", "") or "")
                if not var_name:
                    continue
                bindings.append((var_name, slot_id, zone, nd_stage))
        if len(bindings) > EFMI_SKD_MAX_BINDINGS:
            raise ValueError(
                f"形态键变量同步最多支持 {EFMI_SKD_MAX_BINDINGS} 个绑定，"
                f"当前为 {len(bindings)} 个"
            )
        return bindings

    def _feature_var(self):
        """F4 变量联动总开关（硬依赖 F1 缓冲族）。"""
        if not self._feature_skd():
            return False
        return bool(getattr(self.node, "feature_variable_link", True))

    def _feature_panel(self):
        """F3 面板联动总开关。"""
        return bool(getattr(self.node, "feature_panel_link", True))

    def _resolve_namespace(self):
        """稳定命名空间后缀（等价方法，独立实现）：自定义名清洗；空 = 稳定默认 A。"""
        if self.mod_namespace.strip():
            return re.sub(r"\W+", "_", self.mod_namespace.strip()) or DEFAULT_MOD_NAMESPACE
        return DEFAULT_MOD_NAMESPACE

    def _runtime_variable_name(self, property_name, ns):
        default_base = self._RUNTIME_VARIABLE_DEFAULTS[property_name]
        raw = _normalize_var_name(getattr(self.node, property_name, "") or "")
        resolved = f"{default_base}_{ns}" if not raw or raw == default_base else raw
        return f"${resolved}"

    def _runtime_variable_names(self, ns):
        return (
            self._runtime_variable_name("drag_mode_variable_name", ns),
            self._runtime_variable_name("ui_detected_variable_name", ns),
            self._runtime_variable_name("ui_zone_variable_name", ns),
        )

    def _drag_drive_zone_stage_counts(self):
        """按区域统计点击档位数（等价接口，独立实现）：扫同树开启拖拽驱动的
        形态键节点，每个区域取该区域无方向形态键 drag_click_stage 最大值，最少 1。
        返回 {zone_id: stage_count}（zone_id 0 基 = EFMI 缓冲索引，与形态键节点
        drag_zone_id 语义一致）。"""
        tree = getattr(self.node, "id_data", None)
        counts = {}
        if tree is None:
            return counts
        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_ShapeKey":
                continue
            if not getattr(node, "drag_drive_enabled", False):
                continue
            for item in getattr(node, "shapekey_variable_items", None) or []:
                if not getattr(item, "export_enabled", True):
                    continue
                try:
                    zone = int(getattr(item, "drag_zone_id", -1))
                except (TypeError, ValueError):
                    zone = -1
                if zone < 0:
                    continue
                try:
                    dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
                except (TypeError, ValueError):
                    dir_val = -1
                if dir_val >= 0:
                    continue
                try:
                    stage = int(getattr(item, "drag_click_stage", 1) or 1)
                except (TypeError, ValueError):
                    stage = 1
                counts[zone] = max(counts.get(zone, 1), max(1, stage))
        return counts

    def _collect_zone_capacity(self):
        """驱动缓冲区域容量 = **最大被引用稳定 zone_id + 1**（ARCH-02/B2 修正）。

        用户裁决（原话）："这个区域空物体的编号应当是稳定的，也就是说它编号是几，
        那就是几" —— 容量必须由最大区域号决定，**不是启用区域个数**。区域号可以
        稀疏（如只有 zone 0 与 zone 5），此时容量 6：zone0..zone5 都占段位（未启用
        区域为全 1 回退段），这样 `zone_bases[zone]` 对任何被引用的区域号都不越界，
        该区域的档位统计也不会被静默丢弃。

        口径对齐 zzmi `node_postprocess_draginteraction.py:3098-3099`
        `_zone_capacity`（max(zone_id)+1）与 `:3292-3295` 的 ClickExport 容量扩展；
        并与本模块既有稀疏落槽实现一致（`:2705 sparse_capacity = max(slots) + 1`，
        各缓冲按**稳定 zone_id** 稀疏落槽）。

        被引用区域号的三个来源（对齐 zzmi 同款）：
            ① 启用区域空物体 `_collect_enabled_zone_entries()`（稳定 id，可能稀疏）；
            ② 同树形态键节点的 `drag_zone_id`（本模块 `_drag_drive_zone_stage_counts`
               已按 `counts[zone]` 键收集，容量不足时其统计会被 `range(capacity)`
               静默丢弃——这正是本修正要修的 bug）；
            ③ 点击计数导出节点的 `click_zone_id`（zzmi 同款扩展；zones_only=True
               反扫，不做容量过滤，避免"容量过滤依赖容量"的循环）。
        三者皆空 → 回退 1（zone0 全 1 回退，保持既有行为不变）。
        """
        max_zone = -1
        for zone_id, _item in self._collect_enabled_zone_entries():
            try:
                max_zone = max(max_zone, int(zone_id))
            except (TypeError, ValueError):
                continue
        for zone in self._drag_drive_zone_stage_counts():
            try:
                max_zone = max(max_zone, int(zone))
            except (TypeError, ValueError):
                continue
        for zone, _cycle, _first_var in self._collect_click_export_drivers(
            zones_only=True
        ):
            try:
                max_zone = max(max_zone, int(zone))
            except (TypeError, ValueError):
                continue
        if max_zone < 0:
            return 1
        return max(1, min(max_zone + 1, EFMI_MAX_ZONES))

    def _drag_drive_buffer_layout(self):
        """EFMI 语义的驱动缓冲布局（等价接口，形态键消费方委托）：区域容量 =
        **最大被引用稳定 zone_id + 1**（ARCH-02/B2 修正；0 个区域 → 1：zone0 全 1
        回退；上界 EFMI_MAX_ZONES=256），每区段 = 4 方向槽 + N 个无方向档位槽
        （N = _drag_drive_zone_stage_counts 与 ClickExport 循环档数取大，最少 1——
        研究② §3.2：点击循环不越界）。
        返回 (total_slots, zone_bases, zone_stage_counts)。

        关键契约（稀疏 zone_id 安全）：返回值按**区域号直接索引**——
        `zone_bases[zone]` / `zone_stage_counts[zone]`。启用区域号可以非密排
        （如 {0,5}）而 zone_stage_counts 覆盖 0..capacity-1 连续段：未启用的
        中间区域为"全 1 档位"占位段（真值恒 1，不会被任何消费方引用），从而
        保证 `zone_bases[被引用区域号]` 永不越界、该区域档位统计不丢失。

        ⚠️ 消费方约定：全部消费方（本模块 `_drag_drive_var_sync_bindings`
        L1585 越界跳过 → 形态键 `node_postprocess_shapekey.py:1965` /
        `ntmi_shapekey.py:249` 越界跳过 → ClickExport 经
        `_collect_click_export_drivers` 容量过滤）均已按
        `zone < len(zone_bases)` 判界，**不得假设槽位按启用序连续打包**
        （稀疏区域号下"启用序打包"会与稳定 zone 号错位）。
        """
        capacity = self._collect_zone_capacity()
        zone_counts = self._drag_drive_zone_stage_counts()
        zone_stage_counts = [max(1, int(zone_counts.get(z, 1))) for z in range(capacity)]
        # ClickExport 循环档数扩展（zone 已由容量口径纳入；此处仍显式判界防越界）
        for zone, cycle, _first_var in self._collect_click_export_drivers(
            capacity=capacity
        ):
            if cycle >= 2:
                # 点击计数导出的循环档数扩展该区域点击循环（0..档数-1），
                # 与形态键档位取大（研究② §3.2 同款语义）
                if 0 <= zone < capacity:
                    zone_stage_counts[zone] = max(zone_stage_counts[zone], cycle - 1)
        zone_bases = []
        running = 0
        for stage_count in zone_stage_counts:
            zone_bases.append(running)
            running += 4 + stage_count
        return running, zone_bases, zone_stage_counts

    # =======================================================================
    # 区域中心（空物体世界坐标 → 导出空间）
    # =======================================================================

    def _export_matrix(self):
        """Blender 空间 → 导出空间。EFMI 无导出空间旋转（position_export_matrix
        对 EFMI/HTMI 返回单位阵），返回 (4,4) 单位阵。"""
        return np.eye(4, dtype=np.float64)

    def _get_non_mirror_mirror(self):
        """非镜像工作流补偿矩阵（align-t3 移植 ZZMI `_get_non_mirror_mirror`
        L2833-2865，D-4）：检测场景网格是否带 _ssmt_non_mirror_workflow_processed
        标记（导入时被 X 镜像处理）。命中返回 X 镜像矩阵；否则返回 None。

        优先用烘焙参考物体判定（用户显式指定时最可靠）；参考物体非网格/无标记时
        退回扫描场景全部网格。无 bpy 环境（纯逻辑测试/脚本）优雅降级 None。
        """
        marker = "_ssmt_non_mirror_workflow_processed"
        mirror = np.diag([-1.0, 1.0, 1.0, 1.0])

        def _marked(obj):
            try:
                return bool(obj.get(marker, False))
            except Exception:
                try:
                    return bool(getattr(obj, marker, False))
                except Exception:
                    return False

        ref = self.bake_reference_object
        if ref is not None and getattr(ref, "type", None) == 'MESH' and _marked(ref):
            print("[EFMIDrag] 非镜像工作流：区域空物体矩阵已施加 X 镜像补偿（参考物体标记）")
            return mirror
        try:
            import bpy  # 延迟 import（无 bpy 环境降级 None）
            objects = getattr(bpy.data, "objects", None)
        except Exception:
            objects = None
        if objects is not None:
            try:
                for obj in objects:
                    if getattr(obj, "type", None) == 'MESH' and _marked(obj):
                        print("[EFMIDrag] 非镜像工作流：区域空物体矩阵已施加 X 镜像补偿")
                        return mirror
            except Exception:
                pass
        return None

    def _collect_enabled_zone_entries(self, _invalidate=False):
        """返回 [(稳定 zone_id, item), ...]（启用区域，按稳定 id 升序）。

        ZZMI `_collect_enabled_zone_entries` L3060-3091 同款（t29，用户硬性需求
        稳定 zone ID）——zone_id 是**稳定槽**而非列表序：
        - 显式已分配的 `item.zone_id`（0..EFMI_MAX_ZONES-1）沿用 → 物理重排/
          增删后 zone_id 不漂移（面板/形态键联动的 drag_zone_id/click_zone_id
          引用语义与 zones.buf 槽一致）；
        - 缺省/旧工程/重复 id → 本地按**最小空闲槽**解析（首载按列表序解析出
          0..N-1，迁移期 §3：旧配置升级不错位）；
        - 分配器取最小空闲槽 → 增删后自动密排（常见情形 zone_id == config
          序，与 ZoneParams/zones.buf 按序槽语义天然一致）。

        ARCH-07 修正（消除冗余 RNA 写 + memo 化）：

        **写回策略（条件化，不再是每次全量写）**：解析出的稳定 id 仍需落回
        `item.zone_id`——这是 t29 用户硬性需求的"稳定 zone ID"本体，被
        `TestEFMIStableZoneId` 四条用例（首载迁移持久化、删中间区保号、重排不漂移、
        禁用保槽）逐项钉住，属**既有对外语义**，不可移除。改动点：原先对每个条目
        **无条件** `item.zone_id = zone_id`（值相等也写），现在**仅当值真的不同才写**
        （`int(getattr(item, "zone_id", -1)) != zone_id`）。⇒ 稳态（区域已绑定、
        预览每帧重绘）下**零 RNA 写入**；只有首载/新增区/重复 id 这类真正需要绑定
        的条目才写一次，且幂等。

        **缓存（ARCH-07 memo）**：结果按 `_zone_input_signature()`（按序的
        「目标物体身份 + enabled」序列）缓存；签名变化（区域增删、换序、启用/禁用
        切换）即重算。首版只缓存结果不校验输入，导致禁用区重新启用后仍返回旧缓存
        （`TestEFMIStableZoneId::test_disabled_zone_keeps_slot` 实测 KeyError）——
        该缺陷已由签名校验修正。

        `_invalidate=True` 强制重读（供 `_bind_zone_ids()` 用）。本函数**可安全地
        被预览每帧调用**：命中缓存且无需绑定时是纯读。
        """
        if not _invalidate and self._zone_entries_cache is not None:
            if self._zone_entries_cache_signature == self._zone_input_signature():
                return self._zone_entries_cache
        entries = []
        used_ids = set()
        next_candidate = 0
        for item in self.zone_objects:
            obj = getattr(item, "zone_object", None)
            if obj is None:
                continue
            try:
                zone_id = int(getattr(item, "zone_id", -1))
            except (TypeError, ValueError):
                zone_id = -1
            if not (0 <= zone_id < EFMI_MAX_ZONES) or zone_id in used_ids:
                while next_candidate in used_ids and next_candidate < EFMI_MAX_ZONES:
                    next_candidate += 1
                if next_candidate >= EFMI_MAX_ZONES:
                    print(
                        f"[EFMIDrag][WARNING] 区域超过稳定 ID 上限 {EFMI_MAX_ZONES}，"
                        "其余区域已跳过"
                    )
                    break
                zone_id = next_candidate
            used_ids.add(zone_id)
            while next_candidate in used_ids and next_candidate < EFMI_MAX_ZONES:
                next_candidate += 1
            settings = getattr(obj, "ssmt_drag_zone", None)
            if settings is not None and not getattr(settings, "enabled", True):
                continue
            entries.append((zone_id, item))
        entries.sort(key=lambda entry: entry[0])
        # ARCH-07 补正：解析出的稳定 id 必须**落回** item（用户硬性需求：区域重排/
        # 增删后 zone_id 不漂移；drag_zone_id/click_zone_id 引用与 zones.buf 槽一致）。
        # 原实现直接写回 → 被预览每帧重绘间接调用时反复触发 RNA 写。这里改为
        # **仅在值真的变化时写**（相等即跳过）：语义与 ARCH-07 之前逐项一致，且
        # 稳态下（预览每帧）不再产生任何 RNA 写入。
        for zone_id, item in entries:
            try:
                if int(getattr(item, "zone_id", -1)) != int(zone_id):
                    item.zone_id = int(zone_id)
            except Exception:
                continue
        self._zone_entries_cache = entries
        self._zone_entries_cache_signature = self._zone_input_signature()
        return entries

    def _zone_input_signature(self):
        """`_collect_enabled_zone_entries` 的输入签名（缓存失效判定用）。

        签名 = 区域条目**按序**的「对象身份 + 当前 enabled」序列。（**不放入
        `zone_id`**：它正是本法要解析/写回的量，放进签名会让写回反过来使自己的
        结果失效。）启用状态变化或区域增删/换序 → 签名变化 → 缓存重算。
        **本函数不得写回任何值**（ARCH-07：签名计算是纯读，不得有副作用）。
        """
        signature = []
        for item in self.zone_objects:
            obj = getattr(item, "zone_object", None)
            if obj is None:
                continue
            settings = getattr(obj, "ssmt_drag_zone", None)
            signature.append(
                (id(obj), bool(getattr(settings, "enabled", True)))
            )
        return tuple(signature)

    def _bind_zone_ids(self):
        """导出入口的稳定 zone_id 显式绑定点（ARCH-07：ID 分配收口到导出/显式更新）。

        写回值 = `_collect_enabled_zone_entries()` 的解析结果本身（同一份代码），
        因此**导出侧 zone_id 行为与 ARCH-07 修复前逐项一致**；差别只在触发时机与
        写入条件：旧实现藏在读函数里且**每次全量写**（预览每帧重绘都改用户数据），
        现在由导出入口显式触发一次、且**仅在值确实变化时写**（`_invalidate=True`
        强制重算后再比对）。

        幂等、可重复调用；无 bpy 环境（纯逻辑测试桩）逐项 try/except 降级。
        """
        # 单次强制重算即可：写回条件化 + 结果/签名缓存都在被调方内完成
        self._collect_enabled_zone_entries(_invalidate=True)

    def _collect_zone_slots(self):
        """与 `_collect_zone_configs` 同序的稳定 zone id 列表（同一入口枚举来源，
        供 zones.buf 落槽用——bake_sparse_weights slot_ids 参数）。"""
        return [zone_id for zone_id, _item in self._collect_enabled_zone_entries()]

    def _collect_zone_configs(self):
        """全部启用区域空物体的权重配置（t25 全面 zzmi 化 + t28 椭球化 +
        t32-A 参数语义分离）：
        返回 [(center, lin3, strength, falloff_k, grabbable), ...]（≤256，按**稳定
        zone id** 升序——枚举来源 _collect_enabled_zone_entries（t29），重排/增删
        不漂移；config 序与 ZoneParams/zones.buf 按序槽一致）：
        - center：空物体世界位置（经烘焙参考物体逆矩阵与导出矩阵）；
        - lin3：(3,3) 线性矩阵 = 空物体旋转×缩放（世界矩阵上半 3×3）——
          椭球体（旋转+非均匀缩放，d²=Σ((p−c)/r_i)²，ZZMI ball_matrix 同语义）。
          **t32-A：命中范围恒由空物体变换决定（无显式 radius 覆盖分支）**——
          ssmt_drag_zone.radius 不再参与烘焙/命中（仅作拖拽衰减半径进
          ZoneParams，见 _bake_body）；空物体缩放/旋转决定命中区域形状；
        - strength：ssmt_drag_zone.brush_strength（画刷强度，默认 1.0）；
        - falloff_k：ssmt_drag_zone.brush_falloff_k（画刷衰减 k，默认 4.6）；
        - grabbable：ssmt_drag_zone.grabbable（t36-P5：不再清零权重——不可抓
          区仍烘焙可命中/显示，运行时 simulate 读 ZoneParams[z*2+1].y 拒绝抓取）。
        超过 EFMI_MAX_ZONES 的其余忽略并警告。

        ARCH-07 memo：同一 exporter 实例内结果缓存（导出链同帧内会重复询问
        4+ 次；预览路径每帧重绘也会问）。区域列表/变换在单次导出或单次重绘内
        不变化；`_bind_zone_ids()` 后的稳定 id 解析已由 entries memo 覆盖。
        """
        # ARCH-07 补正（正确性优先）：**不再缓存** zone configs。该结果是空物体
        # 世界变换与 ssmt_drag_zone 参数的函数，而这些量可在同一 exporter 实例
        # 生命周期内被就地修改（面板改半径/缩放、预览拖动空物体），任何基于
        # "身份 + 启用位"的签名都无法可靠感知 —— 实测回归：
        # test_empty_scale_drives_hit_range_not_radius 改 scale 后仍取到旧 configs。
        # 该调用点在导出链/预览链中不是热点（每次 execute/每帧预览常数次），
        # 故直接每次重算，保证"改了就能看到"。
        configs = []
        ref_inv = None
        ref = self.bake_reference_object
        if ref is not None:
            try:
                ref_inv = np.asarray(ref.matrix_world.inverted(), dtype=np.float64).reshape(4, 4)
            except Exception:
                ref_inv = None
        export_matrix = self._export_matrix()
        # align-t3（D-4）：非镜像工作流 X 镜像补偿（ZZMI _evaluate_zone_field
        # L2893-2895 变换链第 1 环；无标记场景恒 None，零行为变化）
        mirror = self._get_non_mirror_mirror()
        dropped = 0
        checked_zones = []  # t38-P4：进入 configs 的启用区（失配告警对象集）
        for _zone_id, item in self._collect_enabled_zone_entries():
            obj = getattr(item, "zone_object", None)
            if obj is None:
                continue
            settings = getattr(obj, "ssmt_drag_zone", None)
            if settings is not None and not getattr(settings, "enabled", True):
                continue
            mw = getattr(obj, "matrix_world", None)
            if mw is None:
                continue
            try:
                world = np.asarray(mw, dtype=np.float64).reshape(4, 4)
            except Exception:
                continue
            if mirror is not None:
                world = mirror @ world
            if ref_inv is not None:
                world = ref_inv @ world
            world = export_matrix @ world
            center = (float(world[0, 3]), float(world[1, 3]), float(world[2, 3]))
            if len(configs) >= EFMI_MAX_ZONES:
                dropped += 1
                continue
            strength = 1.0
            falloff_k = EFMI_BALL_FALLOFF_K
            grabbable = True
            if settings is not None:
                try:
                    strength = float(getattr(settings, "brush_strength", 1.0) or 1.0)
                except (TypeError, ValueError):
                    strength = 1.0
                try:
                    falloff_k = float(getattr(settings, "brush_falloff_k", EFMI_BALL_FALLOFF_K) or EFMI_BALL_FALLOFF_K)
                except (TypeError, ValueError):
                    falloff_k = EFMI_BALL_FALLOFF_K
                grabbable = bool(getattr(settings, "grabbable", True))
            # t32-A：命中范围恒 = 空物体全矩阵椭球（旋转×非均匀缩放，ZZMI
            # ball_matrix 同款）；退化为均匀缩放/无旋转时与旧缩放球一致。
            # radius 不再覆盖 lin3（radius 仅作拖拽衰减，见 _bake_body）。
            lin3 = np.asarray(world[:3, :3], dtype=np.float64).reshape(3, 3)
            configs.append((center, lin3, strength, falloff_k, grabbable))
            checked_zones.append(obj)
        if dropped:
            print(
                f"[EFMIDrag][WARNING] 区域空物体超过 {EFMI_MAX_ZONES} 上限，"
                f"其余 {dropped} 个已忽略"
            )
        # t38-P4：半径-尺度失配告警（ZZMI _check_zone_radius_scale 同款移植，
        # blueprint/node_postprocess_draginteraction.py L3033-3058；纯告警不改
        # 数据）。t32-A 后 radius 不再决定命中范围，但「衰减半径 vs 区域尺寸
        # 失配 → 衰减近乎平坦 → 整块刚体动」仍有诊断价值（t31 实证：radius=0.5
        # vs scale=0.2119 失配曾无任何提示）。
        self._check_zone_radius_scale(checked_zones)
        return configs

    def _check_zone_radius_scale(self, zones):
        """影响半径与区域尺度失配检查（导出时警告；纯告警不改数据）。

        ZZMI 同款移植（blueprint/node_postprocess_draginteraction.py
        L3033-3058）：RubberInfluence(dist, radius) 必须在区域内显著衰减，鼠标
        命中点才能成为变形峰（“点哪拖哪”）；radius 远大于区域尺寸时 R 在区域内
        几乎平坦，整块近似刚体平移，视觉上像“抓住权重中心整块拖走”。原版
        LEWDHAND 实测各区域 radius ≈ 区域空间宽度的 0.3~2.2 倍（ZZMI
        L3036-3039 注释；ZZMI 代码仅实现上界 2.5× 告警）。EFMI 移植实现双侧
        [0.3, 2.2] 告警（t38-P4，用户拍板）：t32-A 后 radius 仅作拖拽衰减半径
        （不再决定命中范围），失配两侧都有诊断价值——<0.3 衰减过陡峰值难命中，
        >2.2 衰减近乎平坦整块刚体动。

        scale = 空物体矩阵列范数均值（world 上半 3×3，ZZMI L3047-3049 同款；
        用原始 matrix_world，不经 ref_inv/export_matrix——radius 是场景空间
        语义值）；radius=0 回退 0.25（ZZMI L3046，继承回退档案）。

        返回警告条数（便于单测断言）。
        """
        ratio_min, ratio_max = 0.3, 2.2
        warned = 0
        for empty in zones:
            try:
                settings = getattr(empty, "ssmt_drag_zone", None)
                if settings is None:
                    continue
                try:
                    raw_radius = float(getattr(settings, "radius", 0.0) or 0.0)
                except (TypeError, ValueError):
                    raw_radius = 0.0
                zone_radius = raw_radius if raw_radius > 0 else 0.25
                m = np.asarray(getattr(empty, "matrix_world", None), dtype=np.float64).reshape(4, 4)
                scale = float(np.mean(np.linalg.norm(m[:3, :3], axis=0)))
            except Exception:
                continue
            if scale <= 1e-6:
                continue
            ratio = zone_radius / scale
            if ratio_min <= ratio <= ratio_max:
                continue
            warned += 1
            name = getattr(empty, "name", "?")
            if ratio > ratio_max:
                hint = "衰减在区域内近乎平坦，变形会整块刚体动（看起来像抓住权重中心拖）"
            else:
                hint = "衰减过陡，命中峰值过于局部，拖拽手感生硬"
            print(
                f"[EFMIDrag][WARNING] 区域 {name} 影响半径 {zone_radius:.3f} "
                f"与区域尺度 {scale:.3f} 失配（ratio {ratio:.1f}，实测范围 "
                f"{ratio_min}~{ratio_max}）→ {hint}。"
                f"建议把该区域影响半径调到 {scale*ratio_min:.3f}~{scale*ratio_max:.3f}"
            )
        return warned

    def _collect_zone_centers(self):
        """兼容入口：全部启用空物体球心列表（≤256）。"""
        return [cfg[0] for cfg in self._collect_zone_configs()]

    # =======================================================================
    # 区域场驱动（align-t3，ZZMI `_write_jiggle_masks` L2225-2386 语义移植：
    # 逐区场求值（测地/包含/平台化/镜像）→ 稀疏落槽；全无效 → 跳过组件）
    # =======================================================================

    def _compute_zone_fields(self, positions, triangles, mesh_names):
        """逐区权重场（与 _collect_enabled_zone_entries/_collect_zone_configs
        同序同源）。返回 (fields, all_invalid)：
        - fields：与 zone_configs 同序的 (N,) 场或 None（该区对本组件跳过：
          包含列表未命中本组件 / 影响球无交集 / 配置缺失）；
        - all_invalid：所有区对本组件均无有效权重（调用方按 ZZMI
          _write_jiggle_masks L2341-2351 语义跳过该组件注入）。

        ZZMI 对齐语义（基准规格 §4）：
        - 距离：propagate（球级开关，默认开）且有拓扑 → 测地（Dijkstra，
          种子 = 球局部最近表面顶点；surface_distances 独立实现同 gb_core
          语义）；关闭或拓扑缺失 → 体积椭球欧氏；
        - 包含过滤：include_objects 非空 → 组件级过滤（EFMI 组件 = 单 IB
          绘制入口；` mesh 名注释来自 EntryPoint 段）；未命中本组件 → 该区
          跳过 + 告警（ZZMI L2310-2316 同款语义，粒度为组件）；
        - 平台化：节点级 mask_plateau（ZZMI L781-784/L2961-2965）；
        - 场公式：_shape_field（高斯 strength·exp(−k·d²)，d≥1 硬截止）。
        """
        entries = self._collect_enabled_zone_entries()
        configs = self._collect_zone_configs()
        n = len(positions)
        fields = [None] * len(configs)
        if not configs:
            return fields, False
        try:
            plateau = float(getattr(self.node, "mask_plateau", 0.0) or 0.0)
        except (TypeError, ValueError):
            plateau = 0.0
        plateau = min(max(plateau, 0.0), 0.99)

        # 拓扑读取时机（ZZMI L2265-2285 同款）：任一区 propagate 且有 IB 拓扑
        # 时读一次边集（去重边，Dijkstra 用）
        edge_verts = None
        if triangles is not None and len(triangles):
            need_topo = any(
                _zone_propagate(getattr(getattr(item, "zone_object", None),
                                        "ssmt_drag_zone", None))
                for _zid, item in entries
            )
            if need_topo:
                edge_verts = edges_from_triangles(triangles)
        elif any(
            _zone_propagate(getattr(getattr(item, "zone_object", None),
                                    "ssmt_drag_zone", None))
            for _zid, item in entries
        ):
            print(
                f"[EFMIDrag][WARNING] 组件 {mesh_names or '?'} 无 IB 拓扑，"
                "沿表面扩散回退体积椭球欧氏距离"
            )

        for z, ((_zone_id, item), cfg) in enumerate(zip(entries, configs)):
            obj = getattr(item, "zone_object", None)
            settings = getattr(obj, "ssmt_drag_zone", None)
            # 包含过滤（组件级，ZZMI 包含列表的 EFMI 等价粒度）
            if settings is not None and not _zone_component_allowed(settings, mesh_names):
                print(
                    f"[EFMIDrag][WARNING] 区域 {getattr(obj, 'name', _zone_id)} 的"
                    f"包含物体列表未覆盖本组件（{mesh_names or '?'}），该区对本组件"
                    "被跳过；请检查包含物体是否属于当前组件"
                )
                continue
            center, shape, strength, falloff_k, _grabbable = cfg
            local = _ball_local_points(positions, center, shape)
            if local is None:
                # 矩阵奇异（某轴零缩放）→ 场 0（空物体本身无体积）
                fields[z] = np.zeros(n, dtype=np.float64)
                continue
            propagate = _zone_propagate(settings)
            if propagate and edge_verts is not None and len(edge_verts):
                d2 = np.einsum("ij,ij->i", local, local)
                seeds = np.zeros(n, dtype=bool)
                seeds[int(np.argmin(d2))] = True
                adjacency = build_surface_adjacency(local, edge_verts)
                d = surface_distances(local, edge_verts, seeds, adjacency=adjacency)
            else:
                d = np.sqrt(np.maximum(np.einsum("ij,ij->i", local, local), 0.0))
            fields[z] = _shape_field(d, strength, falloff_k, plateau)
        all_invalid = bool(configs) and not any(
            f is not None and float(np.max(f)) >= 1e-4 for f in fields
        )
        return fields, all_invalid

    # =======================================================================
    # hash 解析 / 组件定位
    # =======================================================================

    def _parse_hash_values(self, hash_str):
        """解析节点 hash_values：逗号分隔；支持裸 8-hex 或带前缀完整名
        （如 'LOD0.f52320ef-4248-0' → 取 draw_ib f52320ef）。独立实现。"""
        out = []
        for token in str(hash_str or "").split(","):
            t = token.strip()
            if not t:
                continue
            m = re.match(r"^[A-Za-z0-9_]+\.([0-9a-fA-F]{8})(?:[-.]|$)", t)
            if m:
                out.append(m.group(1).lower())
                continue
            out.append(t)
        return out

    def _locate_component(self, sections, hash_value):
        """定位一个 EFMI 组件：EntryPoint 段（hash 匹配）→ 回调 ref → Draw 回调段
        → Position 资源（stride/filename）。返回 comp dict 或 None。"""
        entries = []  # (draw_sec_name, lines)
        prefix = None
        for sec_name, lines in sections.items():
            if not str(sec_name).startswith("[TextureOverride_EntryPoint_"):
                continue
            hash_l = str(hash_value).lower()
            hash_found = any(
                (lambda kv: kv[0] == "hash" and str(kv[1]).lower() == hash_l)(
                    _parse_key_value(line)
                )
                for line in lines
            )
            if not hash_found:
                continue
            current_prefix = _section_key(sec_name)[len("TextureOverride_EntryPoint_"):]
            if prefix is None:
                prefix = current_prefix
            draw_name = None
            for line in lines:
                key, value = _parse_key_value(line)
                if key == "CommandList\\EFMIv1\\Callback_Component_DrawCustom" and value:
                    ref_match = re.match(r"^ref\s+(\S+)$", value)
                    if ref_match:
                        draw_name = ref_match.group(1)
            if draw_name:
                draw_sec = f"[{draw_name}]"
                if draw_sec in sections:
                    entries.append((draw_sec, sections[draw_sec]))
        if not entries:
            return None
        if prefix is None:
            return None

        pos_res = f"Resource_{prefix}_Position"
        pos_lines = sections.get(f"[{pos_res}]", [])
        stride = None
        position_file = None
        for line in pos_lines:
            key, value = _parse_key_value(line)
            if key == "stride":
                try:
                    stride = int(value)
                except (TypeError, ValueError):
                    stride = None
            elif key == "filename":
                position_file = value
        if stride is None or not position_file:
            return None

        # align-t3：解析 EntryPoint 段的 `; [mesh:name1,name2]` 注释（ui/
        # universal/efmi.py 骨骼合并 EntryPoint 发射），供区域 include_objects
        # 组件级包含过滤（ZZMI _zone_allowed_vertex_mask 的 EFMI 等价粒度——
        # EFMI 组件 = 单 IB 单绘制入口，部件名注释即该组件的网格名集合）。
        mesh_names = []
        for _sec_name, sec_lines in entries:
            for line in sec_lines:
                m = _MESH_COMMENT_RE.search(str(line))
                if m:
                    mesh_names.extend(
                        n.strip() for n in m.group(1).split(",") if n.strip()
                    )
        # EntryPoint 段自身也找一遍（注释写在 EntryPoint 段时的主路径）
        for sec_name, sec_lines in sections.items():
            if not str(sec_name).startswith("[TextureOverride_EntryPoint_"):
                continue
            if _section_key(sec_name)[len("TextureOverride_EntryPoint_"):] != prefix:
                continue
            for line in sec_lines:
                m = _MESH_COMMENT_RE.search(str(line))
                if m:
                    mesh_names.extend(
                        n.strip() for n in m.group(1).split(",") if n.strip()
                    )
        # 去重保序
        seen_names = set()
        mesh_names = [n for n in mesh_names if not (n in seen_names or seen_names.add(n))]

        ib_res = f"Resource_{prefix}_Index"
        tex_res = f"Resource_{prefix}_Texcoord"
        blend_res = f"Resource_{prefix}_Blend"
        return {
            "hash": hash_value,
            "prefix": prefix,
            "comp_name": prefix,
            "draw_entries": entries,
            "position_resource": pos_res,
            "position_file": position_file,
            "stride": stride,
            "ib_resource": ib_res,
            "texcoord_resource": tex_res,
            "blend_resource": blend_res,
            "vertex_count": None,
            "role": None,
            "mesh_names": mesh_names,
        }

    def _locate_components(self, sections, hash_values):
        components = []
        for hv in hash_values:
            comp = self._locate_component(sections, hv)
            if comp is None:
                print(f"[EFMIDrag][WARNING] hash {hv} 未找到 EFMI EntryPoint/绘制段，跳过")
                continue
            components.append(comp)
        if components:
            components[0]["role"] = "body"
            for comp in components[1:]:
                comp["role"] = "cloth"
        return components

    # =======================================================================
    # 主流程
    # =======================================================================

    def execute(self, mod_export_path):
        print(f"[EFMIDrag] 开始执行 EFMI 分支, 输出路径: {mod_export_path}")
        if not NUMPY_AVAILABLE:
            print("[EFMIDrag][ERROR] 需要 numpy，已跳过")
            return
        hash_values = self._parse_hash_values(self.hash_values)
        if not hash_values:
            print("[EFMIDrag] 未配置目标哈希值，已跳过")
            return
        ini_files = sorted(
            f for f in os.listdir(mod_export_path) if f.lower().endswith(".ini")
        )
        if not ini_files:
            print(f"[EFMIDrag] 未找到 ini 文件: {mod_export_path}")
            return

        # ARCH-07：稳定 zone_id 的**唯一写回点**收口到导出入口（原先藏在读函数
        # `_collect_enabled_zone_entries` 里，导致权重预览每帧重绘都改用户数据）。
        # 写回值 = 读路径同一算法的解析结果 → 导出侧 zone_id 与修复前逐项一致；
        # 幂等，重复导出不产生二次变动；此处在烘焙之前，后续所有读取都基于已绑定 id。
        self._bind_zone_ids()

        # 复制独立着色器（与 zzmi res/drag_interaction/ 完全分离）
        res_dir = os.path.join(mod_export_path, "res", "drag_interaction_efmi")
        os.makedirs(res_dir, exist_ok=True)
        self._copy_shaders(res_dir)

        any_injected = False
        for ini_file in ini_files:
            ini_path = os.path.join(mod_export_path, ini_file)
            sections, tail, driver = self._read_ini(ini_path)
            if not sections:
                continue
            components = self._locate_components(sections, hash_values)
            if not components:
                continue
            ns = self._resolve_namespace()
            self._create_cumulative_backup(ini_path, mod_export_path)

            bake_ok, active_components = self._bake_all(mod_export_path, sections, components, ns)
            if not bake_ok:
                print(f"[EFMIDrag][WARNING] {ini_file} 烘焙失败，未注入（着色器已复制）")
                continue
            if not active_components:
                print(f"[EFMIDrag][WARNING] {ini_file} 全部组件被跳过，未注入（着色器已复制）")
                continue

            self._emit_sections(sections, active_components, ns, mod_export_path)
            self._inject_hooks(sections, active_components, ns)
            self._write_ini(sections, ini_path, tail, driver, ns)
            print(f"[EFMIDrag] 已注入 {len(active_components)} 个组件到 {ini_file}")
            any_injected = True
        print("[EFMIDrag] 完成" if any_injected else "[EFMIDrag] 未注入任何 ini")

    # =======================================================================
    # ini 读写 / 幂等剥离
    # =======================================================================

    def _read_ini(self, ini_file_path):
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            return None, "", ""
        driver, content = _split_anim_driver_block(content)
        content, tail = _split_tail_content(content)
        # 剥离本分支历史尾块（重导出幂等）
        tail = _strip_efmi_tail(tail)
        sections = OrderedDict()
        current = None
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped
                sections[current] = []
            elif current is not None:
                sections[current].append(line)
        self._strip_efmi_sections(sections)
        return sections, tail, driver

    def _strip_efmi_sections(self, sections):
        """重导出剥离：删除 EFMI 段族、[Present] 内 EFMI 块、Draw 段内 EFMI hook。"""
        for name in [n for n in sections if str(n).startswith(EFMI_SECTION_PREFIXES)]:
            del sections[name]
        present = sections.get("[Present]")
        if present:
            sections["[Present]"] = _strip_present_blocks(present)
        for name, lines in sections.items():
            cleaned = _strip_hook_blocks(lines)
            if len(cleaned) != len(lines):
                sections[name] = cleaned

    def _write_ini(self, sections, ini_file_path, tail="", driver="", ns=None):
        if ns and self._feature_panel():
            # F3 面板联动：UI 尾块三步处理（文本层操作，等价 zzmi 语义）——
            # ①Present 块搬迁到 MODEL DRAG BINDING 标记前（消除重复 [Present]
            # 顺序依赖，桥变量在绑定帧可用）；②仅重写 $ssmtdrag_ui_detected_* /
            # $ssmtdrag_ui_zone_* 引用为实际 ns（面板侧锁存变量等不重写）。
            tail = self._relocate_present_into_ui_tail(sections, tail)
            tail = self._normalize_ui_drag_references(tail, ns)
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            if driver:
                f.write(driver)
                if not driver.endswith("\n"):
                    f.write("\n")
                f.write("\n")
            for sec_name, lines in sections.items():
                f.write(f"{sec_name}\n")
                normalized = list(lines)
                while normalized and not str(normalized[-1]).strip():
                    normalized.pop()
                for line in normalized:
                    f.write(f"{line}\n")
                f.write("\n")
            if tail:
                f.write("\n")
                f.write(tail)

    # =======================================================================
    # UI 尾块处理（F3 面板联动；文本层操作，独立实现）
    # =======================================================================

    def _extract_present_block(self, sections):
        """从主 [Present] 段提取 EFMI Present 块（BEGIN..END 含标记行），并从
        主段删除。返回块行列表或 None。"""
        present = sections.get("[Present]", [])
        start = next(
            (i for i, line in enumerate(present) if EFMI_PRESENT_BEGIN in str(line)),
            None,
        )
        if start is None:
            return None
        end = next(
            (i for i in range(start + 1, len(present))
             if EFMI_PRESENT_END in str(present[i])),
            None,
        )
        if end is None:
            return None
        block = present[start:end + 1]
        del present[start:end + 1]
        return block

    def _relocate_present_into_ui_tail(self, sections, tail_content):
        """把 EFMI Present 块搬到 UI 尾块 MODEL DRAG BINDING 标记之前。"""
        text = str(tail_content or "")
        if "; --- MODEL DRAG BINDING BEGIN ---" not in text:
            return text
        block = self._extract_present_block(sections)
        if block is None:
            return text
        lines = text.splitlines(keepends=True)
        marker_idx = next(
            (i for i, line in enumerate(lines)
             if "; --- MODEL DRAG BINDING BEGIN ---" in line),
            None,
        )
        if marker_idx is None:
            return text
        block_text = "\n".join(block) + "\n"
        lines[marker_idx:marker_idx] = block_text.splitlines(keepends=True)
        return "".join(lines)

    def _normalize_ui_drag_references(self, tail_content, ns):
        """仅重写 MODEL DRAG BINDING 块内的 $ssmtdrag_ui_detected_* /
        $ssmtdrag_ui_zone_* 引用为 EFMI 实际变量名（面板侧零改动契约）。"""
        text = str(tail_content or "")
        marker = "; --- MODEL DRAG BINDING BEGIN ---"
        marker_index = text.find(marker)
        if marker_index < 0:
            return text
        end_marker = "; --- MODEL DRAG BINDING END ---"
        end_index = text.find(end_marker, marker_index)
        if end_index < 0:
            end_index = len(text)
        else:
            end_index += len(end_marker)
        start = text.rfind("\n", 0, marker_index) + 1
        block = text[start:end_index]
        _mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
        for pattern, replacement in (
            (r"\$ssmtdrag_ui_detected_([A-Za-z0-9_]+)", ui_detected_var),
            (r"\$ssmtdrag_ui_zone_([A-Za-z0-9_]+)", ui_zone_var),
        ):
            block = re.sub(pattern, lambda _m: replacement, block)
        return text[:start] + block + text[end_index:]

    def _create_cumulative_backup(self, ini_file_path, mod_export_path):
        try:
            if not os.path.exists(ini_file_path):
                return
            backup_dir = os.path.join(mod_export_path, "Backups")
            os.makedirs(backup_dir, exist_ok=True)
            base = os.path.basename(ini_file_path)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(ini_file_path, os.path.join(backup_dir, f"{base}.{stamp}.bak"))
        except Exception as exc:
            print(f"[EFMIDrag][WARNING] 备份失败: {exc}")

    def _copy_shaders(self, res_dir):
        addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        toolset = os.path.join(addon_dir, "Toolset", "drag_interaction_efmi")
        files = list(SHADER_FILES)
        if self._feature_skd():
            files += list(SHADER_DRIVE_FILES)
        if self._feature_var():
            files += list(SHADER_VARSYNC_FILES)
        if self._feature_panel():
            files += list(SHADER_PANEL_FILES)
        if self.enable_hand_cursor:
            files += list(SHADER_HAND_FILES)
        for fname in files:
            src = os.path.join(toolset, fname)
            if not os.path.exists(src):
                print(f"[EFMIDrag][WARNING] 着色器缺失: {src}")
                continue
            shutil.copy2(src, os.path.join(res_dir, fname))
        # 手部网格/法线资产（二进制，字节级原样复制；与 zzmi 共享资产但位于
        # 独立目录，着色器为独立实现）
        if self.enable_hand_cursor:
            for fname in HAND_ASSET_FILES:
                src = os.path.join(toolset, fname)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(res_dir, fname))
                else:
                    print(f"[EFMIDrag][WARNING] 手部资产缺失: {src}")

    # =======================================================================
    # 烘焙
    # =======================================================================

    def _resolve_buf_path(self, mod_export_path, sections, resource_name):
        """资源段 filename → mod 内绝对路径。"""
        lines = sections.get(f"[{resource_name}]", [])
        for line in lines:
            key, value = _parse_key_value(line)
            if key == "filename" and value:
                rel = value.replace("\\", os.sep).replace("/", os.sep)
                return os.path.join(mod_export_path, rel)
        return None

    def _resource_stride(self, sections, resource_name):
        """资源段 stride 声明（字节/顶点）；无声明返回 None（不假设格式）。"""
        lines = sections.get(f"[{resource_name}]", [])
        for line in lines:
            key, value = _parse_key_value(line)
            if key == "stride":
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _read_float_buf(self, path, stride, columns):
        """读 Position 类 buf（float32, stride 字节/顶点），返回 (N, columns) 前若干列。"""
        if not path or not os.path.exists(path):
            return None
        words = max(1, stride // 4)
        data = np.fromfile(path, dtype=np.float32)
        if data.size % max(words, 1) != 0:
            data = data[: data.size - data.size % max(words, 1)]
        return data.reshape(-1, words)[:, :columns]

    def _read_vertex_float_buf(self, path, stride, vertex_count, columns, max_words=8):
        """读顶点 float buf（Texcoord/Blend 等）：优先资源段 stride 声明定字宽；
        无声明时按文件大小/顶点数反推（回退，不假设格式）。返回 (N, columns) 或 None。"""
        if not path or not os.path.exists(path):
            return None
        data = np.fromfile(path, dtype=np.float32)
        if stride:
            words = max(1, stride // 4)
        else:
            words = max(1, (len(data) // max(vertex_count, 1)) or columns)
            words = min(max(words, columns), max_words)
        usable = (len(data) // max(words, 1)) * words
        rows = usable // max(words, 1)
        if rows < vertex_count:
            return None
        return data[: vertex_count * words].reshape(-1, words)[:, :columns]

    def _read_index_buf(self, mod_export_path, sections, resource_name):
        """按资源段 format 声明读 IB，归一为 uint32（R16_UINT → uint16 升宽；
        R32_UINT → 原样；无声明 → 按 EFMI 实证默认 R32_UINT，t16 复核项）。"""
        lines = sections.get(f"[{resource_name}]", [])
        filename = None
        fmt = ""
        for line in lines:
            key, value = _parse_key_value(line)
            if key == "filename":
                filename = value
            elif key == "format":
                fmt = str(value or "").upper()
        if not filename:
            return None
        rel = filename.replace("\\", os.sep).replace("/", os.sep)
        path = os.path.join(mod_export_path, rel)
        if not os.path.exists(path):
            return None
        if "R16" in fmt:
            return np.fromfile(path, dtype=np.uint16).astype(np.uint32)
        if not fmt:
            # 无 format 声明：默认 R32（审计 t16 §3：格式必须以资源声明为准），
            # 按 R16 特征（索引数×2、非 4 对齐、可整除 3）试探并告警
            size = os.path.getsize(path)
            if size % 6 == 0 and size % 4 != 0:
                print(
                    "[EFMIDrag][WARNING] IB 资源无 format 声明但文件呈 R16_UINT 特征，"
                    "已按 R16 升宽读取（建议在资源段声明 format = R16_UINT）"
                )
                return np.fromfile(path, dtype=np.uint16).astype(np.uint32)
            print(
                "[EFMIDrag][WARNING] IB 资源无 format 声明，按 R32_UINT 读取"
                "（审计 t16：R16_UINT 为主——实机如为 R16 请在资源段声明 format）"
            )
            return np.fromfile(path, dtype=np.uint32)
        return np.fromfile(path, dtype=np.uint32)

    # =======================================================================
    # 锚点 blend 升宽（审计 t16 §7：合并骨架升宽后锚点流必须按升宽格式提供）
    # =======================================================================

    _ELEMENTFORMAT_BI_RE = re.compile(
        r"->ElementFormat\(\s*BLENDINDICES\s*,\s*\d+\s*\)\s*=\s*([A-Za-z0-9_]+)",
        re.IGNORECASE,
    )

    def _blendindices_widen_format(self, sections):
        """解析 ini 中合并骨架升宽行 '{slot}->ElementFormat(BLENDINDICES, N) = {fmt}'
        （efmi.py ConnectComponent 段实证）。返回格式字符串大写或 None。"""
        for lines in sections.values():
            for line in lines:
                m = self._ELEMENTFORMAT_BI_RE.search(str(line))
                if m:
                    return m.group(1).upper()
        return None

    @staticmethod
    def _format_component_bits(fmt):
        """格式字符串每通道位宽（R8G8B8A8→8、R16G16B16A16→16、R32G32B32A32→32）。"""
        m = re.search(r"[RGBA](\d+)", str(fmt or ""))
        if not m:
            return None
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extend_bi_bytes(bi_bytes, src_bits, dst_bits):
        """BLENDINDICES 位宽扩展（值不变，字节序列输出小端）。"""
        src = np.frombuffer(bi_bytes.tobytes(), dtype={
            8: np.uint8, 16: np.uint16, 32: np.uint32,
        }.get(src_bits, np.uint8))
        if dst_bits == src_bits:
            return np.asarray(bi_bytes, dtype=np.uint8)
        dst = src.astype({
            8: np.uint8, 16: np.uint16, 32: np.uint32,
        }.get(dst_bits, np.uint16))
        return dst.view(np.uint8)

    def _bake_anchor_blend(self, blend_path, blend_stride, anchor_ids, widen_bits):
        """读原始 body Blend 字节，按布局模型烘焙锚点 blend（升宽格式）。

        布局模型（审计 t16 §2.1 全部实证族，BW 在前 BI 在后）：
        - stride 12：BW 8B(R16G16B16A16_UNORM)@0 + BI 4B(R8G8B8A8_UINT)@8 → BI 升宽；
        - stride 16：BW 8B@0 + BI 8B@16（BI 已 u16）→ 原样；
        - stride 24：BW 16B@0 + BI 8B@16（BI 已 u16）→ 原样；
        - 其他 stride：无法建模 → 整行原样复制并告警（t15 实机验证）。
        返回 (anchors_blend_bytes, anchor_blend_stride) 或 (None, None)。"""
        if not blend_path or not os.path.exists(blend_path):
            return None, None
        stride = int(blend_stride or 0)
        if stride <= 0:
            return None, None
        raw = np.fromfile(blend_path, dtype=np.uint8)
        vertex_count = raw.size // stride
        if vertex_count == 0:
            return None, None
        rows = raw[: vertex_count * stride].reshape(-1, stride)
        model = {
            12: (0, 8, 8, 4, 8),   # bw_off, bw_len, bi_off, bi_len, src_bits
            16: (0, 8, 8, 8, 16),
            24: (0, 16, 16, 8, 16),
        }.get(stride)
        if model is None:
            print(
                f"[EFMIDrag][WARNING] body Blend stride {stride} 无实证布局模型，"
                "锚点 blend 整行原样复制（审计 t16 §7 升宽格式未保证，t15 实机验证）"
            )
            return rows[anchor_ids], stride
        bw_off, bw_len, bi_off, bi_len, src_bits = model
        dst_bits = widen_bits or 16
        if dst_bits not in (8, 16, 32):
            dst_bits = 16
        out_rows = []
        for idx in anchor_ids:
            src = rows[idx]
            bw = src[bw_off:bw_off + bw_len]
            bi = self._extend_bi_bytes(src[bi_off:bi_off + bi_len], src_bits, dst_bits)
            out_rows.append(np.concatenate([bw, bi]))
        out = np.asarray(out_rows, dtype=np.uint8)
        return out, out.shape[1]

    def _bake_all(self, mod_export_path, sections, components, ns):
        """烘焙全部组件；body 失败/全无效 → False（不注入）。

        align-t3 + D-2（用户拍板）：body 侧 = ZZMI 算法直烘（测地/plateau/
        包含/镜像 + 全无效早退——body 全无效则本 ini 不注入，因 probe/detect
        链以 body 为宿主）；cloth 侧 = **保留 EFMI 独有的 body→cloth 16NN
        高斯传递**（D-2=A 保留特性；ZZMI 无对应子系统；传递源 = body 新权重
        场，天然继承 ZZMI 算法语义）。

        文件布局（res/drag_interaction_efmi/，EFMI 独立目录）：
        - 每组件：<stem>_weights.buf / _active.buf（zones/weights/active）
        - body 额外：_triangles.buf / _identity_ib.buf
        - 锚点/gizmo：anchors_position/texcoord/blend/identity_ib.buf +
          centers.buf + GizmoNormals_{ns}.buf
        返回 (True/False, active_components)——active_components 为实际注入的
        组件列表。"""
        res_dir = os.path.join(mod_export_path, "res", "drag_interaction_efmi")
        os.makedirs(res_dir, exist_ok=True)
        body = components[0]
        body_ok = self._bake_body(mod_export_path, sections, body, res_dir)
        if not body_ok:
            return False, []
        active = [body]
        for comp in components[1:]:
            if not self._bake_cloth(mod_export_path, sections, comp, body, res_dir):
                return False, []
            active.append(comp)
        self._write_assets_json(res_dir, active, ns)
        return True, active

    def _bake_component_weights(self, comp, positions, triangles, res_dir):
        """单组件稀疏权重直烘（align-t3：ZZMI `_write_jiggle_masks` 语义——
        测地/plateau/包含过滤经 `_compute_zone_fields` 求场后落槽）。
        **仅 body 侧走本驱动**（D-2 用户拍板：cloth 保留 EFMI 独有的
        body→cloth 16NN 传递，见 `_bake_cloth`——传递源 = body 经本驱动
        产出的稀疏表，天然继承测地/plateau/包含/镜像语义；cloth 侧无全
        无效早退——传递恒有定义）。
        返回 True 写出 / None 全无效（ZZMI L2341-2351 早退语义：调用方
        对 body 的处理 = 整 ini 不注入，因 probe/detect 链以 body 为宿主）。

        无区域 → zone0 全 1 回退（ZZMI _write_masks_fallback 同款）。"""
        stem = comp["stem"]
        vertex_count = len(positions)
        zone_configs = self._collect_zone_configs()
        if not zone_configs:
            print(
                "[EFMIDrag][WARNING] 未配置区域空物体：按 zzmi 同款回退 zone0 全 1"
                "（整模型可抓）；建议配置区域空物体（球心 = 影响球中心）"
            )
            zone_ids = np.full((vertex_count, EFMI_ZONES_PER_VERTEX), 0xFFFFFFFF, dtype=np.uint32)
            zone_ids[:, 0] = 0
            weights = np.zeros((vertex_count, EFMI_ZONES_PER_VERTEX), dtype=np.float32)
            weights[:, 0] = 1.0
        else:
            zone_fields, all_invalid = self._compute_zone_fields(
                positions, triangles, comp.get("mesh_names"))
            if all_invalid:
                # ZZMI 早退：清掉可能残留的掩码文件（防陈旧缓冲被打包）
                for suffix in ("_zones.buf", "_weights.buf", "_active.buf"):
                    stale = os.path.join(res_dir, f"{stem}{suffix}")
                    if os.path.isfile(stale):
                        try:
                            os.remove(stale)
                        except OSError:
                            pass
                return None
            # t29 稳定 zone id：zone_ids 槽 = 持久化稳定 id（_collect_zone_slots
            # 与 zone_configs 同一入口枚举来源，顺序一致），物理重排/增删不漂移
            try:
                plateau = float(getattr(self.node, "mask_plateau", 0.0) or 0.0)
            except (TypeError, ValueError):
                plateau = 0.0
            zone_ids, weights = bake_sparse_weights(
                positions, zone_configs, slot_ids=self._collect_zone_slots(),
                plateau=plateau, zone_fields=zone_fields)
        zone_ids.tofile(os.path.join(res_dir, f"{stem}_zones.buf"))
        weights.tofile(os.path.join(res_dir, f"{stem}_weights.buf"))
        active_indices(weights).tofile(os.path.join(res_dir, f"{stem}_active.buf"))
        comp["active"] = int((weights.max(axis=1) > 0).sum())
        comp["max_weight"] = float(weights.max(initial=0.0))
        return True

    def _bake_body(self, mod_export_path, sections, comp, res_dir):
        """body 组件：256 区稀疏权重 + active + 三角形 + identity IB + 锚点流
        + ZoneParams（t22 契约升级：完全 zzmi 式空间球衰减，无 UV；Texcoord 仅
        锚点流需要——游戏 VS 输入完整性）。align-t3：锚点每区 4→8 行
        （+gizmo 表面帧），GizmoNormals 缓冲（depth_pull 冻结法线源）。"""
        pos_path = self._resolve_buf_path(mod_export_path, sections, comp["position_resource"])
        tex_path = self._resolve_buf_path(mod_export_path, sections, comp["texcoord_resource"])
        ib_path = self._resolve_buf_path(mod_export_path, sections, comp["ib_resource"])
        blend_path = self._resolve_buf_path(mod_export_path, sections, comp["blend_resource"])
        stride = comp["stride"]
        positions = self._read_float_buf(pos_path, stride, 3)
        if positions is None or len(positions) == 0:
            print(f"[EFMIDrag][WARNING] body Position 缺失/为空: {pos_path}")
            return False
        tex_stride = self._resource_stride(sections, comp["texcoord_resource"])
        blend_stride = self._resource_stride(sections, comp["blend_resource"])
        tex_raw = self._read_vertex_float_buf(tex_path, tex_stride, len(positions), 2)
        if tex_raw is None or len(tex_raw) != len(positions):
            print(f"[EFMIDrag][WARNING] body Texcoord 缺失/顶点数不一致: {tex_path}")
            return False
        blends = self._read_vertex_float_buf(blend_path, blend_stride, len(positions), 4)
        if blends is None or len(blends) != len(positions):
            print(f"[EFMIDrag][WARNING] body Blend 缺失/顶点数不一致: {blend_path}")
            return False

        vertex_count = len(positions)
        comp["vertex_count"] = vertex_count
        stem = os.path.splitext(os.path.basename(comp["position_file"]))[0]
        if stem.endswith("-Position"):
            stem = stem[: -len("-Position")]
        comp["stem"] = stem

        # 三角形索引表（detect 用 + align-t3 测地/gizmo 拓扑源；原 IB 全量拷贝，
        # format 声明感知）——提前读取（权重测地/gizmo 帧均消费）
        ib_data = self._read_index_buf(mod_export_path, sections, comp["ib_resource"])
        triangles = None
        if ib_data is not None:
            ib_data.tofile(os.path.join(res_dir, f"{stem}_triangles.buf"))
            triangles = np.asarray(ib_data, dtype=np.uint32).reshape(-1, 3)
        else:
            print(f"[EFMIDrag][WARNING] body IB 缺失，检测三角形表为空: {ib_path}")
            np.zeros(0, dtype=np.uint32).tofile(os.path.join(res_dir, f"{stem}_triangles.buf"))

        # 稀疏权重（256 区模型；共享驱动：测地/包含/平台化/全无效早退）
        zone_configs = self._collect_zone_configs()
        if self._bake_component_weights(comp, positions, triangles, res_dir) is None:
            # body 全无效：probe/detect 链绑 body，整 ini 不注入（ZZMI 跳过
            # 组件语义在 EFMI 的等价——body 是探测宿主，无法跳过单组件）
            print(
                "[EFMIDrag][WARNING] body 组件：所有配置区域均无有效权重，"
                "本 ini 不注入拖拽（ZZMI 全无效早退语义；body 为探测宿主）"
            )
            return False
        identity_index_buffer(vertex_count).tofile(os.path.join(res_dir, f"{stem}_identity_ib.buf"))

        # 锚点流（每区 8 点 = 中心+恒等 3 轴（拖拽基）+ 中心+gizmo 3 轴
        # （手型表面基，ZZMI BuildGizmoFrames 烘焙）× 全部区；texcoord 从源
        # 顶点复制，blend 按合并骨架升宽格式烘焙——审计 t16 §7）
        space_centers = [cfg[0] for cfg in zone_configs]
        if not space_centers:
            space_centers = [(0.0, 0.0, 0.0)]
        # align-t3：gizmo 帧烘焙（种子 = 球局部最近表面顶点；拓扑缺失 → 恒等帧）
        gizmo_frames = compute_zone_gizmo_frames(positions, triangles, zone_configs)
        # t39-P2：稳定 id 稀疏落槽——zone_configs 与 _collect_zone_slots 同序同源，
        # 各缓冲按**稳定 zone_id** 写（非密集枚举序）：显式稀疏 id（如 0/2 缺 1）
        # 时 centers/ZoneParams/anchors 与运行时按 zone id 读取（detect
        # Anchors.Load(zone*8+k) / Centers[gz] / deform ZoneParams[zone*2]）对齐。
        slots = self._collect_zone_slots()
        if not slots:
            slots = [0]  # 回退 zone0
        sparse_capacity = max(slots) + 1
        anchor_capacity = EFMI_ANCHOR_COUNT * sparse_capacity
        anchor_ids = nearest_vertex_ids(positions, space_centers)
        # C3/t13：探针 drawindexed = EFMI_ANCHOR_COUNT × 区数（8N），而
        # nearest_vertex_ids 每中心只给 1 个顶点 id——texcoord/blend 流若按
        # anchor_ids（N 行）拷贝会与 8N 锚点 draw 不匹配（少拷 7N 行 → 锚点
        # 1-7 读越界垃圾 → ComputeBasis/gizmo 基失效 → 拖不动）。按
        # make_anchor_positions 的 8N 布局（每中心：中心+恒等 3 轴+中心+gizmo
        # 3 轴）展开 id。
        anchor_ids_8n = [
            aid for aid in anchor_ids for _ in range(EFMI_ANCHOR_COUNT)
        ]
        anchor_pos_dense = make_anchor_positions(space_centers, gizmo_frames)
        anchor_tex_dense, _blend_rows = anchor_streams(anchor_ids_8n, tex_raw, blends)
        widen_fmt = self._blendindices_widen_format(sections)
        widen_bits = self._format_component_bits(widen_fmt)
        anchor_blend_dense, anchor_blend_stride = self._bake_anchor_blend(
            blend_path, blend_stride, anchor_ids_8n, widen_bits
        )
        if anchor_blend_dense is None:
            print(f"[EFMIDrag][WARNING] 锚点 blend 烘焙失败: {blend_path}")
            return False
        # t39-P2：按稳定 id 稀疏散射到全容量缓冲（未分配槽 = 0，运行时永不读
        # ——无顶点映射到未分配 zone id，detect 只读已落槽 id 的行）
        anchor_pos = np.zeros((anchor_capacity, 4), dtype=np.float32)
        anchor_tex = np.zeros((anchor_capacity, 3), dtype=np.float32)
        anchor_blend = np.zeros(
            (anchor_capacity, anchor_blend_stride), dtype=np.uint8)
        for i, sid in enumerate(slots):
            src = i * EFMI_ANCHOR_COUNT
            dst = sid * EFMI_ANCHOR_COUNT
            anchor_pos[dst:dst + EFMI_ANCHOR_COUNT] = anchor_pos_dense[src:src + EFMI_ANCHOR_COUNT]
            anchor_tex[dst:dst + EFMI_ANCHOR_COUNT] = anchor_tex_dense[src:src + EFMI_ANCHOR_COUNT]
            anchor_blend[dst:dst + EFMI_ANCHOR_COUNT] = anchor_blend_dense[src:src + EFMI_ANCHOR_COUNT]
        anchor_pos.tofile(os.path.join(res_dir, "anchors_position.buf"))
        anchor_tex.tofile(os.path.join(res_dir, "anchors_texcoord.buf"))
        anchor_blend.tofile(os.path.join(res_dir, "anchors_blend.buf"))
        comp["anchor_blend_stride"] = anchor_blend_stride
        comp["anchor_blend_widen_format"] = widen_fmt
        identity_index_buffer(anchor_capacity).tofile(
            os.path.join(res_dir, "anchors_identity_ib.buf"))
        centers = np.zeros((EFMI_MAX_ZONES, 4), dtype=np.float32)
        for i, sid in enumerate(slots):
            centers[sid, :3] = np.asarray(space_centers[i], dtype=np.float32)
            centers[sid, 3] = 1.0
        centers.tofile(os.path.join(res_dir, "centers.buf"))
        # align-t3：GizmoNormals（256×4）——每区烘焙表面法线（gizmo 帧 X 轴）
        # + valid 旗标；simulate 的 depth_pull 冻结法线源（ZZMI 抓取时冻结
        # PinnedDetectInfo[SLOT_NORMAL] 的 EFMI 静态等价——EFMI 探针无法线
        # 通道，法线按区烘焙于绑定姿态空间，与拖拽基同一坐标系）
        gizmo_normals = np.zeros((EFMI_MAX_ZONES, 4), dtype=np.float32)
        for i, sid in enumerate(slots):
            if i < len(gizmo_frames) and sid < EFMI_MAX_ZONES:
                gizmo_normals[sid, :3] = np.asarray(gizmo_frames[i][0], dtype=np.float32)
                gizmo_normals[sid, 3] = 1.0
        gizmo_normals.tofile(os.path.join(res_dir, f"GizmoNormals_{self._resolve_namespace()}.buf"))

        # ZoneParams（256 槽，t32-A 参数语义分离：布局逐槽对齐 ZZMI
        # _write_zone_resources —— [z*2+0]=(radius,strength,max_offset,falloff)、
        # [z*2+1]=(damping,grabbable,0,1)，全部用 ssmt_drag_zone 原始属性：
        #   radius    = 拖拽衰减半径（deform RubberInfluence 距离门；0=回退 0.25，
        #               shader 侧处理）——**不再是命中范围/烘焙半径**；
        #   strength  = 拖拽强度（ZZMI ZoneStrengthOverride 同源，原始值）；
        #   max_offset/falloff/damping/grabbable = 拖拽物理参数原样。
        # 命中范围由烘焙 zones/weights（空物体全矩阵椭球）决定，与此缓冲无关。
        # max_offset 同时用于本执行器全局 max_offset 映射（_map_max_offset）。）
        zone_params = np.zeros((EFMI_MAX_ZONES * 2, 4), dtype=np.float32)
        # t29 稳定 id：zone_configs 与 _collect_enabled_zone_entries 同序同源（均按
        # 稳定 zone id 升序）——取 ssmt_drag_zone 用枚举 item 而非 self.zone_objects[z]
        # 列表位（物理重排后列表位 ≠ 稳定 id 序，错位会错配每区物理参数）。
        # t39-P2：写索引改用**稳定 _zone_id**（稀疏落槽，非密集 z）——显式稀疏
        # id（如 0/2 缺 1）时与 deform ZoneParams[zone*2] / simulate 读对齐。
        _zone_entries = self._collect_enabled_zone_entries()
        for ((center, lin3, strength, falloff_k, grabbable), (_zone_id, _item)) in zip(
                zone_configs, _zone_entries):
            del center, lin3, strength, falloff_k
            s = None
            try:
                s = getattr(getattr(_item, "zone_object", None), "ssmt_drag_zone", None)
            except Exception:
                s = None
            drag_radius = 0.0
            drag_strength = 0.0
            drag_max_offset = 0.0
            drag_falloff = 0.0
            drag_damping = 0.0
            if s is not None:
                drag_radius = _safe_float(getattr(s, "radius", 0.0))
                drag_strength = _safe_float(getattr(s, "strength", 0.0))
                drag_max_offset = _safe_float(getattr(s, "max_offset", 0.0))
                drag_falloff = _safe_float(getattr(s, "falloff", 0.0))
                drag_damping = _safe_float(getattr(s, "damping", 0.0))
            sid = min(int(_zone_id), EFMI_MAX_ZONES - 1)
            # ZZMI 布局：radius/strength/max_offset/falloff + damping/grabbable/0/1
            zone_params[sid * 2 + 0] = (drag_radius, drag_strength, drag_max_offset, drag_falloff)
            zone_params[sid * 2 + 1] = (drag_damping, 1.0 if grabbable else 0.0, 0.0, 1.0)
        zone_params.tofile(os.path.join(res_dir, f"ZoneParams_{self._resolve_namespace()}.buf"))

        # PathVectors（t32 问题 2：合并骨骼站位符——zzmi 同款 ABI 占位，独立实现）：
        # 每区 4 float4 路径向量全零模板（w=0 无效；EFMI path 运行时永不进入，
        # 与 zzmi 的 PathVectors 192B 模板同语义——合并骨骼场景要求生成站位符）
        path_vectors = np.zeros((EFMI_MAX_ZONES, 4), dtype=np.float32)
        path_vectors.tofile(os.path.join(res_dir, f"PathVectors_{self._resolve_namespace()}.buf"))

        comp["space_centers"] = space_centers
        comp["zone_count"] = len(zone_configs)
        # t39-P2：锚点缓冲容量 = 稳定 id 稀疏容量（8 × (max_id+1)，非密集 8N）——
        # 显式稀疏 id（如 0/2 缺 1）时 AnchorProject RT 宽度/探针 draw 须覆盖全槽
        comp["anchor_count"] = anchor_capacity
        comp["positions"] = positions
        print(
            f"[EFMIDrag] body {stem}: {vertex_count} 顶点, zones={len(zone_configs)}, "
            f"active={comp['active']}, max_weight={comp['max_weight']}, "
            f"probe={EFMI_PROBE_WIDTH}x{(vertex_count + EFMI_PROBE_WIDTH - 1) // EFMI_PROBE_WIDTH}"
        )
        return True

    def _bake_cloth(self, mod_export_path, sections, comp, body, res_dir):
        """cloth 组件（align-t3 + D-2 用户拍板：**保留 body→cloth 16NN 高斯
        传递**（EFMI 架构独有特性；ZZMI 无对应子系统）——传递源 = body 直烘
        稀疏表（body 侧已是 ZZMI 算法：测地/plateau/包含/镜像，传递天然继承）。

        LOD0 stride40 / LOD1 stride16 共用传递公式，stride 区分。返回 True
        写出 / False 失败（中止导出）。cloth 侧不做全无效早退（传递恒有定义；
        body 全无效已在 body 侧拦截整 ini 不注入）。"""
        pos_path = self._resolve_buf_path(mod_export_path, sections, comp["position_resource"])
        stride = comp["stride"]
        positions = self._read_float_buf(pos_path, stride, 3)
        if positions is None or len(positions) == 0:
            print(f"[EFMIDrag][WARNING] cloth Position 缺失/为空: {pos_path}")
            return False
        comp["vertex_count"] = len(positions)
        stem = os.path.splitext(os.path.basename(comp["position_file"]))[0]
        if stem.endswith("-Position"):
            stem = stem[: -len("-Position")]
        comp["stem"] = stem

        body_pos = body.get("positions")
        if body_pos is None:
            body_pos = self._read_float_buf(
                self._resolve_buf_path(mod_export_path, sections, body["position_resource"]),
                body["stride"], 3,
            )
        body_zone_ids = np.fromfile(
            os.path.join(res_dir, f"{body['stem']}_zones.buf"), dtype=np.uint32
        ).reshape(-1, EFMI_ZONES_PER_VERTEX)
        body_w = np.fromfile(
            os.path.join(res_dir, f"{body['stem']}_weights.buf"), dtype=np.float32
        ).reshape(-1, EFMI_ZONES_PER_VERTEX)
        zone_ids, weights = bake_cloth_sparse(body_pos, body_zone_ids, body_w, positions)
        zone_ids.tofile(os.path.join(res_dir, f"{stem}_zones.buf"))
        weights.tofile(os.path.join(res_dir, f"{stem}_weights.buf"))
        active_indices(weights).tofile(os.path.join(res_dir, f"{stem}_active.buf"))
        comp["active"] = int((weights.max(axis=1) > 0).sum())
        comp["max_weight"] = float(weights.max(initial=0.0))
        print(
            f"[EFMIDrag] cloth {stem}: {len(positions)} 顶点 (stride {stride}), "
            f"active={comp['active']}, max_weight={comp['max_weight']}"
        )
        return True

    def _write_assets_json(self, res_dir, components, ns):
        info = {
            "version": 1,
            "namespace": ns,
            "probe_width": EFMI_PROBE_WIDTH,
            "probe_height": (components[0]["vertex_count"] + EFMI_PROBE_WIDTH - 1) // EFMI_PROBE_WIDTH,
            "components": [
                {
                    "prefix": c["prefix"],
                    "role": c["role"],
                    "stem": c.get("stem"),
                    "stride": c["stride"],
                    "vertex_count": c.get("vertex_count"),
                    "active": c.get("active"),
                    "max_weight": c.get("max_weight"),
                }
                for c in components
            ],
            "space_centers": components[0].get("space_centers"),
        }
        try:
            with open(os.path.join(res_dir, "efmi_assets.json"), 'w', encoding='utf-8') as f:
                json.dump(info, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[EFMIDrag][WARNING] assets.json 写入失败: {exc}")

    # =======================================================================
    # 段生成
    # =======================================================================

    def _probe_height(self, components):
        return (components[0]["vertex_count"] + EFMI_PROBE_WIDTH - 1) // EFMI_PROBE_WIDTH

    def _emit_globals(self, sections, ns):
        const_sec = "[Constants]"
        const_lines = sections.setdefault(const_sec, [])
        globals_to_add = [
            f"global $ssmtdrag_efmi_frame_{ns} = 0",
            f"global $ssmtdrag_efmi_enabled_{ns} = 1",
            # t37-P3：三态运行模式（0=关/1=仅命中/2=命中+拖拽；F8 循环切换）。
            # t41 P-4：ZZMI persist 同款（ZZMI.py L4736 global persist）——
            # 模式变量跨游戏会话保持（存 d3dx_user.ini），重启不回落默认。
            # enabled 保持 1（兼容旧消费者）；模式门控在 probe 门控（ini 侧）与
            # simulate/deform（shader 侧 IniParams[159].x）。
            f"global persist $ssmtdrag_efmi_mode_{ns} = {self.drag_mode_default}",
            f"global $ssmtdrag_efmi_modifier_{ns} = 0",
            # t6/P1：ALT 运行态门控族（照搬 ZZMI：ObjectDetectAllowed /
            # ssmtdrag_drawn / inputMode——见 node_postprocess_draginteraction.py
            # L4763/L4737/L4767 与门控 L4211/L4669）。
            #  - drawn：hook 内每帧首次绘制置 1，Present 帧末清 0（ZZMI L4664/L5172
            #    同款）→ 无绘制帧不产生命中/手型，避免残留。
            #  - allowed：enabled && modifier && drawn —— 作为显式语义旗标供
            #    Present 侧与诊断消费（ZZMI $ssmtdrag_mode_ 的 EFMI 等价物）。
            #  - ObjectDetectAllowed：detect/变形/手型预览的运行态总门
            #    （drag_enabled>=1 && inputMode==0 && mode==1 && drawn==1）。
            #    ZZMI 注释明载「inputMode 需在 Present 置位」，本分支同样提供
            #    `$inputMode` 全局（初值 0；框架/其它 mod 可覆盖），语义与 ZZMI 一致。
            f"global $ssmtdrag_efmi_drawn_{ns} = 0",
            f"global $ssmtdrag_efmi_allowed_{ns} = 0",
            f"global $ssmtdrag_ObjectDetectAllowed_{ns} = 0",
            "global $inputMode = 0",
            f"global $ssmtdrag_efmi_lmb_{ns} = 0",
            f"global $ssmtdrag_efmi_rmb_{ns} = 0",
            # t47-P2：Alt+X 等效左键抓取——独立键盘 X 键（ZZMI KeyDragInputManagerX
            # node_postprocess_draginteraction.py L3482 同款，key=X → $x_down）。
            # Present 按钮判定把 X 并入 LMB 位（折成同一抓取路径），故 X 键不必
            # 经过鼠标按钮码；本变量经 buttons 位（w151）驱动 simulate/shapekey_drive。
            f"global $ssmtdrag_efmi_x_{ns} = 0",
            f"global $ssmtdrag_efmi_buttons_{ns} = 0",
            # 门控定案（§10.6）：pass 计数器——帧变化清零 + 每绘制递增（hook 内维护）
            f"global $ssmtdrag_efmi_pass_{ns} = 0",
            f"global $ssmtdrag_efmi_frame_prev_{ns} = -1",
            # t4/P0（恢复 pass 门控后）：probe 帧 latch——门控为「每帧在 pass 序号
            # >= _probe_exec_pass()（默认 2 = 最后一个深度 pass）的第一帧至多一次」。
            # probe_frame_prev = 上次 probe 命中的帧号（→每帧至多一次）；
            # probe_pass       = 上次 probe 命中时的帧内 pass 序号（→帧号已跨帧但
            #                    上一帧从未达目标 pass 时的序号回绕补跑判据）。
            # 初始 -1 → 首帧 pass>=目标值即首次探测。
            f"global $ssmtdrag_efmi_probe_frame_prev_{ns} = -1",
            f"global $ssmtdrag_efmi_probe_pass_{ns} = -1",
            # EFMI 框架实例迭代变量兜底声明（框架通常提供；未声明时避免加载期报错）
            "global $draw_call_instance_id = 0",
            # 3Dmigoto 内建输入（默认值声明，zzmi 同款机制）
            "global $cursorX = -1",
            "global $cursorY = -1",
            "global $screenW = 1",
            "global $screenH = 1",
            # align-t3：ZZMI 物理全局变量（_emit_present_and_constants L4747-4762
            # 同源）——时间步与释放动态阻尼（persist 不挂：ZZMI 同款 plain global，
            # 用户调参经 d3dx_user.ini 覆盖初始值）
            f"global $ssmtdrag_efmi_sim_speed_{ns} = {_fmt(DEFAULT_SIM_SPEED)}",
            f"global $ssmtdrag_efmi_max_step_{ns} = {_fmt(DEFAULT_MAX_STEP)}",
            f"global $ssmtdrag_efmi_release_boost_{ns} = {_fmt(DEFAULT_RELEASE_BOOST)}",
            f"global $ssmtdrag_efmi_release_decay_{ns} = {_fmt(DEFAULT_RELEASE_DECAY)}",
        ]
        if self._feature_panel():
            _mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
            globals_to_add.extend([
                f"global {ui_detected_var} = -1",
                f"global {ui_zone_var} = -1",
            ])
        if self.enable_hand_cursor:
            # 手型光标：Present 经 store 读捕获状态选 Action/NoAction 网格
            globals_to_add.append(f"global $ssmtdrag_efmi_hand_action_{ns} = 0")
            # t32-B 诊断：手型状态码（0=可见/1=无命中隐藏；align-t3 起简化为
            # 二值可见性——D-6 基无效即 discard 对齐 ZZMI，废弃越界/基无效细分）
            globals_to_add.append(f"global $ssmtdrag_efmi_hand_state_{ns} = 0")
            # align-t3：ZZMI 手型 persist 参数面（L4799-4827 全表；persist 项
            # 可在 d3dx_user.ini 调参并跨重载保留）+ 蓄力归约变量（L4824-4826）
            globals_to_add.extend([
                f"global $ssmtdrag_efmi_hand_surface_clip_{ns} = {_fmt(EFMI_HAND_SURFACE_CLIP)}",
                f"global $ssmtdrag_efmi_hand_surface_lift_{ns} = {_fmt(EFMI_HAND_SURFACE_LIFT)}",
                f"global $ssmtdrag_efmi_hand_surface_softness_{ns} = {_fmt(EFMI_HAND_SURFACE_SOFTNESS)}",
                f"global persist $ssmtdrag_efmi_hand_center_x_{ns} = {_fmt(EFMI_HAND_CENTER[0])}",
                f"global persist $ssmtdrag_efmi_hand_center_y_{ns} = {_fmt(EFMI_HAND_CENTER[1])}",
                f"global persist $ssmtdrag_efmi_hand_center_z_{ns} = {_fmt(EFMI_HAND_CENTER[2])}",
                f"global persist $ssmtdrag_efmi_hand_scale_{ns} = {_fmt(EFMI_HAND_SCALE)}",
                f"global persist $ssmtdrag_efmi_hand_opacity_{ns} = {_fmt(EFMI_HAND_OPACITY)}",
                f"global persist $ssmtdrag_efmi_hand_tilt_min_{ns} = {_fmt(EFMI_HAND_TILT_MIN)}",
                f"global persist $ssmtdrag_efmi_hand_windup_time_{ns} = {_fmt(EFMI_HAND_WINDUP_TIME)}",
                f"global persist $ssmtdrag_efmi_hand_tilt_max_deg_{ns} = {_fmt(EFMI_HAND_TILT_MAX)}",
                f"global persist $ssmtdrag_efmi_hand_tilt_rmb_min_{ns} = {_fmt(EFMI_HAND_TILT_RMB_MIN)}",
                f"global persist $ssmtdrag_efmi_hand_rmb_windup_time_{ns} = {_fmt(EFMI_HAND_RMB_WINDUP_TIME)}",
                f"global persist $ssmtdrag_efmi_hand_tilt_rmb_max_deg_{ns} = {_fmt(EFMI_HAND_TILT_RMB_MAX)}",
                f"global persist $ssmtdrag_efmi_hand_tilt_grab_max_deg_{ns} = {_fmt(EFMI_HAND_TILT_GRAB_MAX)}",
                f"global persist $ssmtdrag_efmi_hand_vibrate_threshold_{ns} = {_fmt(EFMI_HAND_VIBRATE_THRESHOLD)}",
                f"global persist $ssmtdrag_efmi_hand_vibrate_period_slow_{ns} = {_fmt(EFMI_HAND_VIBRATE_PERIOD_SLOW)}",
                f"global persist $ssmtdrag_efmi_hand_vibrate_period_fast_{ns} = {_fmt(EFMI_HAND_VIBRATE_PERIOD_FAST)}",
                f"global persist $ssmtdrag_efmi_hand_vibrate_amplitude_{ns} = {_fmt(EFMI_HAND_VIBRATE_AMPLITUDE)}",
                f"global persist $ssmtdrag_efmi_hand_upright_max_deg_{ns} = {_fmt(EFMI_HAND_UPRIGHT_MAX)}",
                f"global persist $ssmtdrag_efmi_hand_outline_width_{ns} = {_fmt(EFMI_HAND_OUTLINE_WIDTH)}",
                f"global persist $ssmtdrag_efmi_hand_outline_opacity_{ns} = {_fmt(EFMI_HAND_OUTLINE_OPACITY)}",
                f"global persist $ssmtdrag_efmi_hand_reference_height_{ns} = {_fmt(EFMI_HAND_REFERENCE_HEIGHT)}",
                # 蓄力归约（ZZMI L4985-5023 同源）：按下沿时间戳 + 独按蓄力进度
                # + RMB 独按旗标（蓄力倾斜/网格切换驱动）
                f"global $ssmtdrag_efmi_lmb_prev_{ns} = 0",
                f"global $ssmtdrag_efmi_rmb_prev_{ns} = 0",
                f"global $ssmtdrag_efmi_lmb_press_time_{ns} = 0",
                f"global $ssmtdrag_efmi_rmb_press_time_{ns} = 0",
                f"global $ssmtdrag_efmi_lmb_hold_fraction_{ns} = 0",
                f"global $ssmtdrag_efmi_rmb_hold_fraction_{ns} = 0",
                f"global $ssmtdrag_efmi_lmb_wfrac_{ns} = 0",
                f"global $ssmtdrag_efmi_rmb_wfrac_{ns} = 0",
                f"global $ssmtdrag_efmi_rmb_lone_hold_{ns} = 0",
            ])
        if self._feature_skd():
            # F1 形态键联动：鼠标位移归约辅助变量 + 冷启动播种标志
            globals_to_add.extend([
                f"global $ssmtdrag_efmi_skdy_{ns} = 0",
                f"global $ssmtdrag_efmi_skdx_{ns} = 0",
                f"global $ssmtdrag_efmi_skprev_y_{ns} = 0",
                f"global $ssmtdrag_efmi_skprev_x_{ns} = 0",
                f"global $ssmtdrag_efmi_seed_pending_{ns} = 0",
                f"global $ssmtdrag_efmi_booted_{ns} = 0",
            ])
        if self._feature_var():
            # F4 变量联动：每绑定 5 个仲裁辅助变量（激活/回读/上一值/待确认/模式）
            bindings = self._drag_drive_var_sync_bindings()
            for i in range(len(bindings)):
                globals_to_add.extend([
                    f"global $ssmtdrag_efmi_skact_{ns}_{i} = 0",
                    f"global $ssmtdrag_efmi_skrb_{ns}_{i} = 0",
                    f"global $ssmtdrag_efmi_skprev_{ns}_{i} = 0",
                    f"global $ssmtdrag_efmi_skpending_{ns}_{i} = 0",
                    f"global $ssmtdrag_efmi_skmode_{ns}_{i} = 0",
                ])
        for g in globals_to_add:
            var = g.split("=", 1)[0].replace("global ", "").replace("persist ", "").strip()
            if not any(f"{var} " in line or f"{var}=" in line for line in const_lines):
                const_lines.append(g)

    def _emit_key_sections(self, sections, ns):
        key_defs = [
            (f"[KeyEFMIDragLMB_{ns}]", "VK_LBUTTON", f"$ssmtdrag_efmi_lmb_{ns}"),
            (f"[KeyEFMIDragRMB_{ns}]", "VK_RBUTTON", f"$ssmtdrag_efmi_rmb_{ns}"),
            # t47-P2：独立键盘 X 键（ZZMI KeyDragInputManagerX node_postprocess_
            # draginteraction.py L3482 同款）。X 只置位按键沿变量，Post 归零；
            # Present 按钮判定把它并入 LMB 位，达成 Alt+X 等效左键抓取。
            (f"[KeyEFMIDragX_{ns}]", "X", f"$ssmtdrag_efmi_x_{ns}"),
        ]
        if self.grab_key == 'ALT':
            key_defs.append((f"[KeyEFMIDragModifier_{ns}]", "VK_MENU", f"$ssmtdrag_efmi_modifier_{ns}"))
        for sec, key, var in key_defs:
            if sec not in sections:
                sections[sec] = [
                    f"key = {key}",
                    "type = hold",
                    f"{var} = 1",
                    f"post {var} = 0",
                ]
        # t37-P3：三态运行模式切换热键（ZZMI drag_system_mode + F8 cycle 同款——
        # L3496-3506：type=cycle，每次按键 0→1→2→0）
        mode_toggle_sec = f"[KeyEFMIDragModeToggle_{ns}]"
        mode_key = str(getattr(self.node, "mode_toggle_key", "") or "").strip() or "f8"
        mode_toggle_lines = [
            f"key = {mode_key}",
            "type = cycle",
            f"$ssmtdrag_efmi_mode_{ns} = 0,1,2",
        ]
        # 快捷键可修改后重新导出：段已存在时也按当前值覆盖（保持幂等）
        if sections.get(mode_toggle_sec) != mode_toggle_lines:
            sections[mode_toggle_sec] = mode_toggle_lines

    def _emit_global_resources(self, sections, components, ns):
        body = components[0]
        probe_height = self._probe_height(components)
        res = EFMI_RES_SHADER_DIR
        # t39-P2：锚点宽/探针 draw 用稀疏稳定 id 容量（comp["anchor_count"]，
        # 4×(max_id+1)），非密集 4×zone_count——显式稀疏 id 时覆盖全槽
        anchor_count = max(1, int(body.get("anchor_count") or 0))
        resources = {
            f"[ResourceEFMIDragProject_{ns}]": [
                "type = Texture2D", "mode = mono", "mips = 1", "array = 1",
                "msaa = 1", "msaa_quality = 0",
                "format = DXGI_FORMAT_R32G32B32A32_FLOAT",
                "bind_flags = render_target shader_resource",
                "width = " + _fmt(EFMI_PROBE_WIDTH),
                "height = " + _fmt(probe_height),
            ],
            f"[ResourceEFMIDragAnchorProject_{ns}]": [
                "type = Texture2D", "mode = mono", "mips = 1", "array = 1",
                "msaa = 1", "msaa_quality = 0",
                "format = DXGI_FORMAT_R32G32B32A32_FLOAT",
                "bind_flags = render_target shader_resource",
                "width = " + _fmt(anchor_count),
                "height = 1",
            ],
            # 256 区模型：每实例 Candidate 4 float4（最强区/right/down/锚点）
            f"[ResourceEFMIDragCandidate_{ns}]": [
                "type = RWBuffer", "format = R32G32B32A32_FLOAT",
                "array = " + _fmt(EFMI_MAX_INSTANCES * EFMI_CANDIDATE_STRIDE),
            ],
            # 每实例 State = 4 公共槽 + 256 区 × 2（offset/velocity）
            f"[ResourceEFMIDragState_{ns}]": [
                "type = RWBuffer", "format = R32G32B32A32_FLOAT",
                "array = " + _fmt(EFMI_MAX_INSTANCES * EFMI_STATE_STRIDE),
            ],
            f"[ResourceEFMIDragProbeIB_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/{body['stem']}_identity_ib.buf",
            ],
            f"[ResourceEFMIDragAnchorIB_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/anchors_identity_ib.buf",
            ],
            # 路径向量站位符（t32 问题 2：合并骨骼场景要求；w=0 无效模板，path
            # 运行时永不进入——zzmi 同款 ABI 占位，独立实现）
            f"[ResourceEFMIDragPathVectors_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/PathVectors_{self._resolve_namespace()}.buf",
            ],
            f"[ResourceEFMIDragIndices_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/{body['stem']}_triangles.buf",
            ],
            # 稀疏权重表（每顶点 K 对：区 id + 权重，t22 契约升级）
            f"[ResourceEFMIDragBodyZoneIDs_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/{body['stem']}_zones.buf",
            ],
            f"[ResourceEFMIDragBodyWeights_{ns}]": [
                "type = Buffer", "format = R32_FLOAT",
                f"filename = {res}/{body['stem']}_weights.buf",
            ],
            f"[ResourceEFMIDragAnchorPosition_{ns}]": [
                "type = Buffer", "stride = 16",
                f"filename = {res}/anchors_position.buf",
            ],
            f"[ResourceEFMIDragAnchorTexcoord_{ns}]": [
                "type = Buffer", "stride = 12",
                f"filename = {res}/anchors_texcoord.buf",
            ],
            f"[ResourceEFMIDragAnchorBlend_{ns}]": [
                "type = Buffer",
                # stride 随锚点 blend 烘焙结果（升宽格式，审计 t16 §7）；默认 16
                "stride = " + _fmt(body.get("anchor_blend_stride") or 16),
                f"filename = {res}/anchors_blend.buf",
            ],
            # t21/ZZMI parity：区域中心局部坐标（simulate grab-center 冻结用）
            f"[ResourceEFMIDragCenters_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/centers.buf",
            ],
            # t21/ZZMI parity：逐区物理参数（[z*2].x = 烘焙半径等；deform 运行时
            # 距离衰减用）——t19 P3：此前烘焙但从不绑定（死数据），现接入消费
            f"[ResourceEFMIDragZoneParams_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/ZoneParams_{self._resolve_namespace()}.buf",
            ],
            # align-t3：每区烘焙表面法线（gizmo 帧 X 轴）+ valid 旗标——
            # simulate depth_pull 的冻结法线源（ZZMI 抓取冻结命中法线的
            # EFMI 静态等价；探针无法线通道 → 按区烘焙于绑定姿态空间）
            f"[ResourceEFMIDragGizmoNormals_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/GizmoNormals_{self._resolve_namespace()}.buf",
            ],
        }
        for sec, lines in resources.items():
            sections.setdefault(sec, lines)

    def _emit_component_resources(self, sections, comp, ns):
        res = EFMI_RES_SHADER_DIR
        stem = comp["stem"]
        comp_resources = {
            # 源 Position R32 视图（t38 H1 爆炸根因；F2 重构：统一在本方法发射，
            # deform 段仅引用）：游戏/框架 Position 资源无 format（strided Buffer
            # 无 typed 视图），efmi_deform.hlsl 的 Buffer<uint> typed 读会视图不
            # 匹配 → 源读 0 → Out 全 0 → 模型坍缩。显式 format=R32_UINT 视图可用。
            f"[ResourceEFMIDragSourceR32_{comp['comp_name']}_{ns}]": [
                "type = Buffer",
                "format = R32_UINT",
                "stride = " + _fmt(comp["stride"]),
                f"filename = {comp['position_file']}",
            ],
            # 稀疏权重表（每顶点 K 对：区 id uint + 权重 float，t22 契约升级）
            f"[ResourceEFMIDragZoneIDs_{comp['comp_name']}_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/{stem}_zones.buf",
            ],
            f"[ResourceEFMIDragWeights_{comp['comp_name']}_{ns}]": [
                "type = Buffer", "format = R32_FLOAT",
                f"filename = {res}/{stem}_weights.buf",
            ],
            f"[ResourceEFMIDragActive_{comp['comp_name']}_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/{stem}_active.buf",
            ],
            # 三分立输出：源 Position SRV（既有资源）只读；本资源同时承载
            # deform 的输出 UAV 视图与绘制段 vb0/vb3 的 VB 视图（独立于源）。
            # array = 顶点数 × strideWords（R32_UINT 元素数；本仓 efmi.py 惯例：
            # 所有 Buffer 均带 array 或 filename 尺寸声明，终审 F2）
            f"[ResourceEFMIDragOut_{comp['comp_name']}_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                "stride = " + _fmt(comp["stride"]),
                "array = " + _fmt(
                    int(comp.get("vertex_count") or 0) * max(1, comp["stride"] // 4)
                ),
                "bind_flags = vertex_buffer unordered_access shader_resource",
            ],
        }
        for sec, lines in comp_resources.items():
            sections.setdefault(sec, lines)

    def _emit_probe_detect_sections(self, sections, components, ns):
        body = components[0]
        probe_height = self._probe_height(components)
        res = EFMI_RES_SHADER_DIR
        # t39-P2：探针锚点 draw 用稀疏稳定 id 容量（同 _emit_global_resources）
        anchor_count = max(1, int(body.get("anchor_count") or 0))
        # ---- body 探针（native-VS 投影）----
        probe_body = f"[CustomShaderEFMIDragProbeBody_{ns}]"
        sections[probe_body] = [
            "run = BuiltInCommandListUnbindAllRenderTargets",
            f"clear = ResourceEFMIDragProject_{ns} 0.0",
            "blend = ADD ONE ZERO",
            "alpha = ADD ONE ZERO",
            # vs 不指定：沿用游戏 native VS（合并骨架蒙皮后的运行时位置流）
            f"gs = {res}/efmi_probe.hlsl",
            f"ps = {res}/efmi_probe.hlsl",
            f"ib = ResourceEFMIDragProbeIB_{ns}",
            "topology = point_list",
            f"o0 = set_viewport no_view_cache ResourceEFMIDragProject_{ns}",
            # vb0/vb1/vb2 不重绑：沿用本实例回调的原始 Position/Texcoord 与
            # MergedSkeleton_Apply 换绑的合并骨架流
            # t43：Zmd 构建 ini-param 只支持单分量行（空格整行 Unrecognised）
            f"x150 = {_fmt(EFMI_PROBE_WIDTH)}",
            f"y150 = {_fmt(probe_height)}",
            "z150 = $draw_call_instance_id",
            f"w150 = $ssmtdrag_efmi_frame_{ns}",
            f"drawindexed = {_fmt(body['vertex_count'])}, 0, 0",
            "run = BuiltInCommandListUnbindAllRenderTargets",
        ]
        # ---- anchors 探针（自包含锚点流：align-t3 起每区 8 点 = 中心+恒等
        # 3 轴 ±10mm（拖拽基）+ 中心+gizmo 3 轴 ±10mm（手型表面基））----
        probe_anchors = f"[CustomShaderEFMIDragProbeAnchors_{ns}]"
        sections[probe_anchors] = [
            "run = BuiltInCommandListUnbindAllRenderTargets",
            f"clear = ResourceEFMIDragAnchorProject_{ns} 0.0",
            "blend = ADD ONE ZERO",
            "alpha = ADD ONE ZERO",
            f"gs = {res}/efmi_probe.hlsl",
            f"ps = {res}/efmi_probe.hlsl",
            f"vb0 = ResourceEFMIDragAnchorPosition_{ns}",
            f"vb1 = ResourceEFMIDragAnchorTexcoord_{ns}",
            f"vb2 = ResourceEFMIDragAnchorBlend_{ns}",
            # t14（H-B）：锚点探针必须**显式绑定 vb3**。组件绘制命令列表是
            # `vb0 = vb3 = Position`（EFMIv1 换绑 MergedSkeleton 时同样成对换
            # vb0/vb3），锚点探针若只绑 vb0/vb1/vb2 会**继承**那份 body Position
            # （stride 40），当该 pass 的 VS 变体从 vb3 取位置时，锚点会被投影成
            # body 顶点 → 锚点 RT 内容错乱 → detect 的 ComputeBasis / 手型 gizmo
            # 全错（表现为"方向完全与鼠标无关"）。绑定自身锚点缓冲后与
            # vb0/vb1/vb2 同源；若 VS 不读 vb3 则该绑定无副作用。
            f"vb3 = ResourceEFMIDragAnchorPosition_{ns}",
            f"ib = ResourceEFMIDragAnchorIB_{ns}",
            "topology = point_list",
            f"o0 = set_viewport no_view_cache ResourceEFMIDragAnchorProject_{ns}",
            # t43：单分量行（Zmd 构建）
            f"x150 = {_fmt(anchor_count)}",
            "y150 = 1",
            "z150 = $draw_call_instance_id",
            f"w150 = $ssmtdrag_efmi_frame_{ns}",
            f"drawindexed = {_fmt(anchor_count)}, 0, 0",
            "run = BuiltInCommandListUnbindAllRenderTargets",
        ]
        # ---- detect（每实例 1 dispatch；x150.z = 当前空间实例；稀疏权重表）----
        detect = f"[CustomShaderEFMIDragDetect_{ns}]"
        sections[detect] = [
            f"cs = {res}/efmi_detect.hlsl",
            f"cs-t0 = ResourceEFMIDragProject_{ns}",
            f"cs-t1 = ResourceEFMIDragAnchorProject_{ns}",
            f"cs-t2 = ResourceEFMIDragIndices_{ns}",
            f"cs-t3 = ResourceEFMIDragBodyZoneIDs_{ns}",
            f"cs-t4 = ResourceEFMIDragBodyWeights_{ns}",
            f"cs-u0 = ResourceEFMIDragCandidate_{ns}",
            # t120 = IniParams 由 3Dmigoto 自动绑定（契约；不写行）
            # t43：单分量行（Zmd 构建不支持空格整行）
            f"x150 = {_fmt(EFMI_PROBE_WIDTH)}",
            f"y150 = {_fmt(probe_height)}",
            "z150 = $draw_call_instance_id",
            f"w150 = $ssmtdrag_efmi_frame_{ns}",
            "x151 = $cursorX",
            "y151 = $cursorY",
            f"z151 = $ssmtdrag_efmi_modifier_{ns}",
            f"w151 = $ssmtdrag_efmi_buttons_{ns}",
            # align-t3：152 = ZZMI JIGGLE_PARAMS 语义（detect 只消费 w152 命中
            # 阈值；其余槽与 [Present] 同值发射，保持跨命令列表一致）
            f"x152 = 0.25",
            f"y152 = {_fmt(1.0)}",
            f"z152 = {_fmt(self.drag_scale)}",
            f"w152 = {_fmt(self.hit_threshold)}",
            "dispatch = 1, 1, 1",
            "post cs-t0 = null", "post cs-t1 = null", "post cs-t2 = null",
            "post cs-t3 = null", "post cs-t4 = null", "post cs-u0 = null",
        ]
        # ---- Probe CL（门控定案——§10.6 最终裁决：NumViews 判据不可用
        #      （颜色 pass 实测 5/2 非恒定）→ pass 计数器：深度 pass 恒 1/2 次、
        #      颜色 pass 恒 3+ 次——计数在绘制回调 hook 内递增；t17/问题①：
        #      probe 在「最后一个深度 pass」（threshold-1）draw 后执行一次，
        #      （== 而非 >=），使 pass3/4/5 全 fresh 同动（旧 >=3 + B1 后置 →
        #      pass3 壳恒 stale 分离，见 _probe_exec_pass 注释）；Alt 臂动）----
        probe_cl = f"[CommandListEFMIDragProbe_{ns}]"
        # t4/P0（本修复）：恢复 pass 门控——R8/P0 的「每帧首次 body draw 帧 latch」
        # 实际把 probe 钉在该 mesh 每帧的**第一次 draw**（深度预 pass / 非主相机
        # pass）而非主色 pass，探针写入的 clip 投影与 Present 注入的光标域不成套
        # → detect 恒 miss → 手型恒隐藏 + deform 不位移（两症状同源）。
        #
        # 恢复消费 _probe_exec_pass()（t4/P0 用户裁决：**在颜色层执行**；
        # = _probe_pass_threshold()，默认 3 = 帧内首个颜色 pass 的 draw 序号）。
        # 帧内 pass 实测（_inject_hooks）：深度 pass 恒第 1/2 次（NumViews:0 无 RT）、
        # 颜色 pass 恒第 3-5 次（o0=3315d2b5 主色 RT）。
        #
        # t6 修正（问题 1：probe_pass 逐帧交替缺陷）：
        #   旧兜底 `|| pass < probe_pass` 与「probe_pass 从不复位」叠加会逐帧交替——
        #   F1 在 p3 命中（probe_pass=3），F2 的 p1 满足 `1 < 3` → 兜底在**深度层**
        #   触发并把 probe_pass 改回 1，F3 又 `k<1` 永假 → 只有 `>=3` 能在 p3 命中，
        #   此后 F4 再次 `1 < 3` 触发……→ 手型闪烁 / 拖拽隔帧失效。
        #   修正采用任务书方案 A+B 组合（双保险）：
        #     A) `_inject_hooks` 帧变化块内把 probe_pass 复位为 -1（帧作用域）；
        #     B) 兜底判据改 `frame_prev < frame - 1`（上一帧完全未探测才补跑）。
        #   A 保证正常帧 probe_pass 初值 -1 使 `pass < probe_pass` 恒假 → 只在
        #   `pass >= exec_pass` 命中（稳定落在颜色层）；B 保证即便复位失效也不会
        #   在深度层被兜底触发。
        #
        # t6/P1：外层加 **两道**运行态门——`ObjectDetectAllowed == 1`（检测门：
        # enabled/输入模式/模式/绘制旗标）**且** `$ssmtdrag_efmi_allowed_{ns} == 1`
        # （ALT 手势门：enabled && modifier && drawn）。理由：ZZMI 的检测门不含
        # 修饰键（悬停即检测），但 EFMI 的手型/命中是同一套 Candidate 的直接产物，
        # 若不加 ALT 门，未按 Alt 时仍会逐帧算命中（与「不按 ALT 时不计算命中」
        # 的要求相悖）。两道门同时成立才探测，等价于「仅在允许手势下产生命中」，
        # 且因 probe → detect → deform 同帧链式消费，松开 Alt 不会有陈旧命中残留。
        #
        # 判据构成：
        #  1) `>=`（非 ==）：pass 计数帧内单调递增，`>=` 与 `==` 正常帧等价，
        #     但避免 R8/P0 记载的「绘制次数不足以命中唯一序号 → 死条件」退化；
        #  2) probe_frame_prev 记录「上次 probe 落在哪一帧」→ 每帧至多一次；
        #  3) 兜底 B：上一帧完全没探测（frame_prev < frame - 1）才补跑，且仍受
        #     `pass >= exec_pass` 约束，绝不回落深度层。
        #
        # 语义隔离：本次仅改 probe CL 门控与 _probe_exec_pass 的返回口径。
        # simulate grabbing / deform 位移门 [159].x>=2.0、shapekey_drive mode==1
        # 与 enabled 主开关均未动。
        exec_pass = self._probe_exec_pass()
        sections[probe_cl] = [
            f"if $ssmtdrag_efmi_enabled_{ns} == 1 "
            f"&& $ssmtdrag_efmi_frame_{ns} != $ssmtdrag_efmi_probe_frame_prev_{ns}"
            f" && $ssmtdrag_ObjectDetectAllowed_{ns} == 1"
            f" && $ssmtdrag_efmi_allowed_{ns} == 1",
            f"\tif $ssmtdrag_efmi_pass_{ns} >= {exec_pass}"
            f" || $ssmtdrag_efmi_probe_frame_prev_{ns} < $ssmtdrag_efmi_frame_{ns} - 1",
            f"\t\trun = CustomShaderEFMIDragProbeBody_{ns}",
            f"\t\trun = CustomShaderEFMIDragProbeAnchors_{ns}",
            f"\t\trun = CustomShaderEFMIDragDetect_{ns}",
            f"\t\t$ssmtdrag_efmi_probe_pass_{ns} = $ssmtdrag_efmi_pass_{ns}",
            f"\t\t$ssmtdrag_efmi_probe_frame_prev_{ns} = $ssmtdrag_efmi_frame_{ns}",
            "\tendif",
            "endif",
        ]

    def _emit_deform_sections(self, sections, components, ns):
        res = EFMI_RES_SHADER_DIR
        probe_height = self._probe_height(components)
        for comp in components:
            stride_words = max(1, comp["stride"] // 4)
            # t41 F1 全量写域：dispatch = ceil(vertex_count/64)（非 active 顶点
            # 也写 Out=Base 位精确复制，杜绝未写区域 0 坍缩）
            dispatch_n = max(1, (int(comp.get("vertex_count") or 1) + 63) // 64)
            sec = f"[CustomShaderEFMIDragDeform_{comp['comp_name']}_{ns}]"
            # 源 R32 视图资源段（t38 H1；F2 重构：发射统一在
            # _emit_component_resources，此处仅引用）
            src_res = f"ResourceEFMIDragSourceR32_{comp['comp_name']}_{ns}"
            sections[sec] = [
                f"cs = {res}/efmi_deform.hlsl",
                f"cs-t0 = {src_res}",
                # 稀疏权重表：t1 = 区 id、t2 = 权重、t3 = 活动顶点索引
                f"cs-t1 = ResourceEFMIDragZoneIDs_{comp['comp_name']}_{ns}",
                f"cs-t2 = ResourceEFMIDragWeights_{comp['comp_name']}_{ns}",
                f"cs-t3 = ResourceEFMIDragActive_{comp['comp_name']}_{ns}",
                f"cs-t4 = ResourceEFMIDragState_{ns}",
                # t38 H5 防御：fresh 帧校验（probe 未运行帧 Out=Base 原样）
                f"cs-t5 = ResourceEFMIDragCandidate_{ns}",
                # t21/ZZMI parity（fix ①）：逐区物理参数（[z*2].x = 烘焙半径）
                f"cs-t6 = ResourceEFMIDragZoneParams_{ns}",
                f"cs-u0 = ResourceEFMIDragOut_{comp['comp_name']}_{ns}",
                # t120 = IniParams 由 3Dmigoto 自动绑定（契约；不写行）
                # t38 P1：ini-param 拆单分量行（消除 override 记录异常；x150 =
                # 512、y150 = probe 高、z150 = 实例槽、w150 = 帧标）
                f"x150 = {_fmt(EFMI_PROBE_WIDTH)}",
                f"y150 = {_fmt(probe_height)}",
                "z150 = $draw_call_instance_id",
                f"w150 = $ssmtdrag_efmi_frame_{ns}",
                # 155：x = strideWords（16→4 / 40→10）、y = 字节 stride、
                # z = vertex_count（t41 全量写域越界判断）
                f"x155 = {_fmt(stride_words)}",
                f"y155 = {_fmt(comp['stride'])}",
                f"z155 = {_fmt(int(comp.get('vertex_count') or 0))}",
                # t21/ZZMI parity（fix ①）：x156 = 运行时距离衰减 falloffPower
                # （ZZMI 默认 1.5）
                "x156 = 1.5",
                # align-t3：x163 = mult_radius（ZZMI y72 同源）——deform 的
                # RubberInfluence 半径 = (zone 覆盖或回退 0.25) × mult_radius
                # （ZZMI screen_state L341/interaction 衰减同款倍率路径）
                f"x{EFMI_MULT_EXTRA_IP} = {_fmt(self.mult_radius)}",
                f"dispatch = {_fmt(dispatch_n)}, 1, 1",
                # 显式资源卫生：借用绑定存还（3Dmigoto 自动恢复 shader/OM，
                # 绑定显式清空）
                "post cs-t0 = null", "post cs-t1 = null", "post cs-t2 = null",
                "post cs-t3 = null", "post cs-t4 = null", "post cs-t5 = null",
                "post cs-t6 = null", "post cs-u0 = null",
            ]
            # Apply CL：**无条件**跑 deform + 成对换绑 vb0/vb3 到输出拷贝。
            #
            # t22 裁决（依据 t21 实机证据）：本段**不得带门控**。
            # 实机定位：门控变量 `ObjectDetectAllowed_` / `efmi_allowed_` 是在 hook 里
            # **逐笔 draw 重算**的（后者含 ALT 实时态 `modifier`），而 Apply CL 同样逐笔跑。
            # 一旦某一笔的门为 0，该笔就仍用 Draw CL 顶部绑定的原始
            # `Resource_LOD0..._Position` ⇒ 读到 **Base、且不经过弹簧** ⇒ 该阶段在释放期
            # **直接复位**（无回弹），与其它走 `Out` 的阶段错位 ⇒ 用户所报
            # 「有东西没跟着回弹 / 松手之后直接就复位了」的黑影。
            # 描边是每帧最后一笔（t21 日志序列确认），最容易被漏掉。
            #
            # 安全性：位移闸门只在 shader 侧、且**只按 mode 开合**——
            # `efmi_deform.hlsl` 为 `if (active && IniParams[159].x >= 2.0)`。
            # delta=0 时它把 `Out` 写成 **Base 的位精确拷贝**，与直接读 Position
            # 等价 ⇒ 去门后「未拖拽 / mode 关闭」等情形行为**不变**，只是阶段之间
            # 不再发散。这也正是 `efmi_deform.hlsl` 头部注释声明的契约
            #（「ini 侧 Apply CL 已取消门控」）。
            #
            # t47（2026-09，实机二分定案）：shader 侧原先还叠了一层**逐帧命中
            # 新鲜度**门（`Candidate[inst*4].w >= frame - 1`，t38 H5 加的、FR-1
            # 放宽过一次）。用 release 档位逐档走（各档之间只差一个机制：档 4/3
            # 无黑影、档 2/1/0 有）证明**那层门本身就是黑影的来源**：
            #   · `PullTowardLimit` 渐近 + probe 投的是**已变形**流（Draw CL 在
            #     probe 之前就把 vb0/vb3 换成了 Out）⇒ 拖到一定程度光标必然跑出
            #     网格投影 ⇒ detect 判 miss；
            #   · 门一关，`delta` 一帧内归零 ⇒ 网格 snap 回原姿势 ⇒ 黑影；
            #   · 与上面「逐笔门为 0 ⇒ 该笔读 Base」是**同一类** bug。
            # 现在只留 mode 门；位移一定归零由 `efmi_simulate.hlsl` 的弹簧衰减 +
            # 静止清零（`length(next) < 1e-5 && length(velocity) < 1e-4`）负责，
            # tests 里钉住。**不要再往 deform 加逐帧新鲜度门。**
            #
            # 历史：t9 曾把本段改为无门控，但与"放宽 EntryPoint 签名"捆绑在同一批，
            # 而那批的实机后果是「模型消失 + 命中消失」（签名放宽所致），t11 因此整批
            # 回退、连带把本段也还原成带门。经 t21 逐笔证据分离后确认：**签名放宽是
            # 有害的，本段去门控是必要的**，两者必须分开处理。
            apply_sec = f"[CommandListEFMIDragApply_{comp['comp_name']}_{ns}]"
            sections[apply_sec] = [
                f"run = CustomShaderEFMIDragDeform_{comp['comp_name']}_{ns}",
                f"vb0 = ResourceEFMIDragOut_{comp['comp_name']}_{ns}",
                f"vb3 = ResourceEFMIDragOut_{comp['comp_name']}_{ns}",
            ]

    def _emit_simulate_section(self, sections, ns):
        res = EFMI_RES_SHADER_DIR
        sec = f"[CustomShaderEFMIDragSimulate_{ns}]"
        sections[sec] = [
            f"cs = {res}/efmi_simulate.hlsl",
            f"cs-t0 = ResourceEFMIDragCandidate_{ns}",
            # t34-P1：逐区拖拽物理覆盖（strength/max_offset/damping）——本线程
            # zone 的 ZoneParams 覆盖，0 = 继承全局回退
            f"cs-t1 = ResourceEFMIDragZoneParams_{ns}",
            # t21/ZZMI parity（fix ③）：区域中心局部坐标（grab center 冻结用）
            f"cs-t2 = ResourceEFMIDragCenters_{ns}",
            # align-t3：每区烘焙表面法线（depth_pull 的冻结法线源——ZZMI
            # 抓取冻结命中法线 capture.xyz 的 EFMI 静态等价）
            f"cs-t3 = ResourceEFMIDragGizmoNormals_{ns}",
            # t51：锚点投影 RT（行 zone*8+0 = 区中心投影 clip）——capture 时
            # grab center 的参考屏幕 uv 来源（详见 efmi_simulate.hlsl capture 处）
            f"cs-t4 = ResourceEFMIDragAnchorProject_{ns}",
            f"cs-u0 = ResourceEFMIDragState_{ns}",
            # t120 = IniParams 由 3Dmigoto 自动绑定（契约；不写行）
            # t21/ZZMI parity（fix ③）：屏幕尺寸 → delta 按 min 维对称归一
            f"x155 = res_width",
            f"y155 = res_height",
            "z155 = 0", "w155 = 0",
            # 256 区并行积分：dispatch = 8 实例 × 256 区 / 64 线程
            f"dispatch = {_fmt(EFMI_MAX_INSTANCES * EFMI_MAX_ZONES // 64)}, 1, 1",
            "post cs-t0 = null", "post cs-t1 = null", "post cs-t2 = null",
            "post cs-t3 = null", "post cs-t4 = null", "post cs-u0 = null",
        ]

    def _emit_shapekey_drive_sections(self, sections, ns, mod_export_path=None):
        """F1 形态键联动缓冲族 + 驱动 CS（独立实现，等价 zzmi 缓冲族语义）。

        发射条件 = _feature_skd()（调用方已门控）；资源前缀 = EFMI_SKD_PREFIX
        （形态键消费方经拖拽节点 _drag_shapekey_resource_prefix() 推导同前缀）。
        布局 = _drag_drive_buffer_layout()：N 区 ×（4 方向槽 + N 档位槽），
        ZoneStageCounts 缓冲 0 基索引 = 区 id 直接对应。
        """
        total_slots, _zone_bases, zone_stage_counts = self._drag_drive_buffer_layout()
        capacity = len(zone_stage_counts)
        res = EFMI_RES_SHADER_DIR
        prefix = EFMI_SKD_PREFIX
        resources = {
            f"[{prefix}Drive_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {total_slots}",
            ],
            # 方向缓冲与驱动缓冲同构，末位 1 个上一帧按键状态槽
            f"[{prefix}Dir_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {total_slots + 1}",
            ],
            # 拖拽绑定锁存（独立单槽）：0=未绑定，否则运行时区 id+1（0-255 区
            # 均合法，+1 编码避开 0 哨兵——与 zzmi 同构的失效语义）
            f"[{prefix}DragLatch_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT", "array = 1",
            ],
            f"[{prefix}ClickCount_{ns}]": [
                "type = RWBuffer", "format = R32_UINT",
                f"array = {capacity}",
            ],
            f"[{prefix}ClickCountF_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {capacity}",
            ],
            f"[{prefix}ActiveDir_{ns}]": [
                "type = RWBuffer", "format = R32_UINT",
                f"array = {capacity}",
            ],
            f"[{prefix}ZoneStageCounts_{ns}]": [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/EFMIZoneStageCounts_{ns}.buf",
            ],
        }
        for sec, lines in resources.items():
            sections.setdefault(sec, lines)
        # 每区域档位数烘焙（R32_UINT ×capacity）
        if mod_export_path:
            try:
                stage_counts = np.array(zone_stage_counts, dtype=np.uint32)
                res_dir = os.path.join(mod_export_path, "res", "drag_interaction_efmi")
                os.makedirs(res_dir, exist_ok=True)
                stage_counts.tofile(os.path.join(res_dir, f"EFMIZoneStageCounts_{ns}.buf"))
            except Exception as exc:
                print(f"[EFMIDrag][WARNING] ZoneStageCounts 烘焙失败: {exc}")
        # 冷启动播种条目（F4 变量联动链）：y157 = 条目数、x158+i = (zone, $变量值)
        # t43：单分量行
        seed_entries = self._click_export_seed_entries() if self._feature_var() else []
        seed_lines = [
            f"x157 = $ssmtdrag_efmi_seed_pending_{ns}",
            f"y157 = {len(seed_entries)}",
            "z157 = 0",
            "w157 = 0",
        ]
        for seed_idx, (seed_zone, seed_var) in enumerate(seed_entries[:EFMI_SKD_MAX_SEED]):
            seed_lines.append(
                f"x{EFMI_SKD_SEED_BASE + seed_idx} = {seed_zone}"
            )
            seed_lines.append(
                f"y{EFMI_SKD_SEED_BASE + seed_idx} = {seed_var}"
            )
            seed_lines.append(f"z{EFMI_SKD_SEED_BASE + seed_idx} = 0")
            seed_lines.append(f"w{EFMI_SKD_SEED_BASE + seed_idx} = 0")
        # 驱动 CS（每帧 dispatch；命中输入 = Candidate 跨实例赢家仲裁）
        sens = float(getattr(self.node, "shapekey_drive_move_sensitivity", 0.02) or 0.02)
        sec = f"[CustomShaderEFMIDragShapeKeyDrive_{ns}]"
        sections[sec] = [
            f"cs = {res}/efmi_shapekey_drive.hlsl",
            f"cs-t0 = ResourceEFMIDragCandidate_{ns}",
            f"cs-t1 = {prefix}ZoneStageCounts_{ns}",
            f"cs-u0 = {prefix}Drive_{ns}",
            f"cs-u1 = {prefix}Dir_{ns}",
            f"cs-u2 = {prefix}ClickCount_{ns}",
            f"cs-u3 = {prefix}ActiveDir_{ns}",
            f"cs-u4 = {prefix}ClickCountF_{ns}",
            f"cs-u5 = {prefix}DragLatch_{ns}",
            # EFMI 联动扩展区（156+；与 150-155 探测区相邻、与 zzmi 77-124 不相交）
            # t43：单分量行
            f"x156 = $ssmtdrag_efmi_skdy_{ns}",
            f"y156 = $ssmtdrag_efmi_skdx_{ns}",
            f"z156 = {_fmt(sens)}",
            "w156 = 0",
        ]
        sections[sec].extend(seed_lines)
        sections[sec].extend([
            "dispatch = 1, 1, 1",
            "post cs-t0 = null", "post cs-t1 = null",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null",
            "post cs-u3 = null", "post cs-u4 = null", "post cs-u5 = null",
        ])

    def _emit_shapekey_var_sync_sections(self, sections, ns, mod_export_path=None):
        """F4 变量联动：var_sync CS + VarReadback CL + VarPrev/VarSyncMap/ZoneActive
        资源（独立实现，等价 zzmi 值仲裁状态机）。

        发射条件 = _feature_var()（调用方已门控，F4 硬依赖 F1 缓冲族）；
        值区/模式区在 EFMI 扩展区 166-183（与 150-158 探测/驱动区不相交）。
        """
        bindings = self._drag_drive_var_sync_bindings()
        if not bindings:
            return
        prefix = EFMI_SKD_PREFIX
        res = EFMI_RES_SHADER_DIR
        _total_slots, _zone_bases, zone_stage_counts = self._drag_drive_buffer_layout()
        capacity = len(zone_stage_counts)
        resources = {
            f"[{prefix}VarPrev_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {len(bindings)}",
            ],
            f"[{prefix}ZoneActive_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {capacity}",
            ],
            f"[{prefix}VarSyncMap_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_UINT",
                f"filename = {res}/EFMIZoneVarSyncMap_{ns}.buf",
            ],
        }
        for sec, lines in resources.items():
            sections.setdefault(sec, lines)
        # 变量同步映射表：每绑定 uint4 = (驱动槽位, 区域 ID, 无方向档位, 保留)；
        # 方向形态键 nd_stage 以 0xFFFFFFFF 哨兵表示
        if mod_export_path:
            try:
                sync_map = np.array(
                    [
                        (slot, zone, 0xFFFFFFFF if nd_stage < 0 else nd_stage, 0)
                        for _var, slot, zone, nd_stage in bindings
                    ],
                    dtype=np.uint32,
                )
                res_dir = os.path.join(mod_export_path, "res", "drag_interaction_efmi")
                os.makedirs(res_dir, exist_ok=True)
                sync_map.tofile(os.path.join(res_dir, f"EFMIZoneVarSyncMap_{ns}.buf"))
            except Exception as exc:
                print(f"[EFMIDrag][WARNING] VarSyncMap 烘焙失败: {exc}")
        # var_sync CS（每帧无条件，变量优先 + mode 握手）
        sec = f"[CustomShaderEFMIDragShapeKeyVarSync_{ns}]"
        lines = [f"cs = {res}/efmi_shapekey_var_sync.hlsl"]
        for i, (var_name, _slot, _zone, _nd_stage) in enumerate(bindings):
            lines.append(
                f"{'xyzw'[i % 4]}{EFMI_VAR_SYNC_VALUE_BASE + i // 4} = {var_name}"
            )
        for i, (_var_name, _slot, _zone, _nd_stage) in enumerate(bindings):
            lines.append(
                f"{'xyzw'[i % 4]}{EFMI_VAR_SYNC_MODE_BASE + i // 4} = "
                f"$ssmtdrag_efmi_skmode_{ns}_{i}"
            )
        lines.extend([
            f"cs-t1 = {prefix}VarSyncMap_{ns}",
            f"cs-u0 = {prefix}Drive_{ns}",
            f"cs-u1 = {prefix}ClickCount_{ns}",
            f"cs-u2 = {prefix}VarPrev_{ns}",
            f"cs-u3 = {prefix}ClickCountF_{ns}",
            f"cs-u4 = {prefix}ZoneActive_{ns}",
            # 驱动 CS 的绑定锁存（独立单槽资源）是 ZoneActive 的单一事实源
            f"cs-u5 = {prefix}DragLatch_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-t1 = null",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null",
            "post cs-u3 = null", "post cs-u4 = null", "post cs-u5 = null",
        ])
        sections[sec] = lines
        # VarReadback CL：store 读 ZoneActive + Drive 槽（不带 ref；依赖
        # 3Dmigoto 构建 StoreCommand，实机验证项——不支持则静默降级）
        readback_sec = f"[CommandListEFMIDragShapeKeyVarReadback_{ns}]"
        readback_lines = []
        for i, (var_name, slot, zone, _nd_stage) in enumerate(bindings):
            active = f"$ssmtdrag_efmi_skact_{ns}_{i}"
            rb = f"$ssmtdrag_efmi_skrb_{ns}_{i}"
            prev = f"$ssmtdrag_efmi_skprev_{ns}_{i}"
            pending = f"$ssmtdrag_efmi_skpending_{ns}_{i}"
            mode = f"$ssmtdrag_efmi_skmode_{ns}_{i}"
            readback_lines.extend([
                f"store = {active}, {prefix}ZoneActive_{ns}, {zone}",
                f"store = {rb}, {prefix}Drive_{ns}, {slot}",
                f"if {var_name} != {prev}",
                f"\t{prev} = {var_name}",
                f"\t{pending} = 1",
                f"\t{mode} = 2",
                f"elif {pending} == 1",
                f"\tif {rb} == {prev}",
                f"\t\t{pending} = 0",
                f"\t\t{mode} = 0",
                "\telse",
                f"\t\t{mode} = 2",
                "\tendif",
                f"elif {active} >= 1",
                f"\t{var_name} = {rb}",
                f"\t{prev} = {rb}",
                f"\t{mode} = 1",
                "else",
                f"\t{mode} = 0",
                "endif",
            ])
        sections[readback_sec] = readback_lines

    def _emit_present_block(self, sections, ns):
        present_sec = "[Present]"
        present_lines = sections.setdefault(present_sec, [])
        lines = [
            EFMI_PRESENT_BEGIN,
        ]
        if self.grab_key != 'ALT':
            # NONE 常开模式：无修饰键 Key 段，修饰标志恒 1
            lines.append(f"$ssmtdrag_efmi_modifier_{ns} = 1")
        lines.extend([
            # 按钮码：1=LMB 2=RMB 3=同按
            # t47-P2：Alt+X 等效左键抓取——X 键并入 LMB 位（bit0，值 1）。算式
            # lmb*(1-x)+x 求 lmb/x 逻辑或并把结果夹回 0/1（both 按下仍=1=LMB），
            # 与物理 LMB 完全同路径（simulate buttons==1 拉出、shapekey_drive
            # buttons&1 触发）。X 键本身由独立 Key 段置位（_emit_key_sections）。
            f"$ssmtdrag_efmi_buttons_{ns} = ($ssmtdrag_efmi_lmb_{ns} * (1 - $ssmtdrag_efmi_x_{ns}) + $ssmtdrag_efmi_x_{ns}) + 2 * $ssmtdrag_efmi_rmb_{ns}",
            # IniParams 150-165 专用区（EFMI 高位区原位扩展；槽位号不搬 ZZMI
            # 67-104——ABI 保留 + 避开本仓 UI 面板 x87-89；值与语义按 ZZMI
            # 基准规格 §2.2）；t43：单分量行
            f"x150 = {_fmt(EFMI_PROBE_WIDTH)}",
            "y150 = 1",
            "z150 = 0",
            f"w150 = $ssmtdrag_efmi_frame_{ns}",
            # C1a/t13 + R12：cursor 接线（域 [0,1] 双轴直用，终版）。
            # fork 内置 cursor_x/cursor_y 域：[0,1]（X left→right、Y top-down
            # 0=顶——与 ZZMI 生态一致：希格莉德 L8636 `cursor_x<1` 域判定、
            # ZZMI.py L4268-4272 `$cursorX = cursor_x` 直用无 /2）。
            # R12（用户「两倍」量化约束 + 半速压缩模型，debuglog-diag §11.4）：
            #   - 旧样本 cursor_x=1.768750 >1 推断 [0,2] 域——**无法在现有 log
            #     复现（09-07/09-08 帧无求值值），样本失效**；
            #   - 若域 [0,1] 而代码 /2 → cursor_uv = 实际/2（半速压缩）→ 命中点=
            #     手型=鼠标一半 →「手型偏左、命中区右移、差=2×偏移」✓ 用户全部
            #     观测吻合（应有命中位置 = 当前 × 2）；
            #   - Y 侧 `$cursorY = cursor_y` 直用已实测正确（上下同向）→ X 对称
            #     直用：**$cursorX = cursor_x（去掉 /2）**——与 ZZMI 同款、无缩放。
            # 残留小偏移（非半屏级）时再查 detect cursor.x 微调/viewport 映射。
            # ------------------------------------------------------------------
            # t48 记录（**已实机证伪并回退，勿原样重试**）：因日志实测到
            #   cursor_x ∈ [−1.0000, 2.2781]（396 样本，中位 1.6391）、
            #   cursor_y ∈ [−0.1546, 0.9583]（回读缓冲 res_width=1920/res_height=1080），
            # 曾把 X 改成 `cursor_x * res_height / res_width`（≡ ZZMI 的
            # `cursor_screen_x / screenW` 分支，逐轴÷自身尺寸的思路）。**实机结果
            # 更糟**（用户：「都偏到不知道哪去了」）⇒ 说明「cursor_x 按高归一化」
            # 这个模型不成立，或与探测端 uv 之间存在非纯缩放的映射（含偏移/子视口）。
            # 已回退为双轴直用。**下一步应做标定实测**：让用户把鼠标放在若干已知
            # 屏幕位置、记录手型/命中出现的实际位置，再解映射（纯缩放/含偏移/
            # 非线性），不要凭 cursor_x 的取值范围反推。
            # ------------------------------------------------------------------
            # 帧末赋值，下一帧 detect 读到（滞后 1 帧，Present 消费端本就帧末读
            # Candidate）。
            "$cursorX = cursor_x",
            "$cursorY = cursor_y",
            # R9（R2 默认禁用）：rt 尺寸校正块**不再发射**——rt_width/rt_height
            # 在 Zmd fork [Present] 的语义无帧证据（t13 S2-B）；若解析失败
            # Unrecognised 会中断 [Present]（frame 不自增 → 帧 latch 恒等 →
            # probe 只跑首帧 → Candidate 冻结 = 判定点钉右下 S2-A 链）。需要显式
            # 启用时手工加回：
            #   if rt_width > 0 && rt_height > 0 && (rt_width != res_width || rt_height != res_height)
            #   	$cursorX = $cursorX * res_width / rt_width
            #   	$cursorY = $cursorY * res_height / rt_height
            #   endif
            # （先按 t13 §4 判别读 rt/res 实值，确认 rt 语义后再启用；基准 §6.2
            # ViewportFrameAPI 同列为待实机判别项，不默认引入）
            "x151 = $cursorX",
            "y151 = $cursorY",
            f"z151 = $ssmtdrag_efmi_modifier_{ns}",
            f"w151 = $ssmtdrag_efmi_buttons_{ns}",
            # align-t3：152 = ZZMI JIGGLE_PARAMS（68）语义——x=回退半径 0.25、
            # y=回退强度 1.00、z=dragScale 1.00（旧 0.70 废除）、w=命中阈值 1e-4
            f"x152 = 0.25",
            f"y152 = {_fmt(1.0)}",
            f"z152 = {_fmt(self.drag_scale)}",
            f"w152 = {_fmt(self.hit_threshold)}",
            "x153 = time",
            f"y153 = $ssmtdrag_efmi_enabled_{ns}",
            f"z153 = {_fmt(self.max_offset)}",
            # ---- t52/t54：Y 约定出厂值（下述两处门控都是 `值 < 0.5` = 开启）----
            # 153.w = 光标 Y 约定：**0 = 本 fork 的 `cursor_y` 是 bottom-up** ——
            #   detect 的命中判定（efmi_detect.hlsl）与 simulate 的 grab center Δu
            #   （efmi_simulate.hlsl）都据此翻一次。约定错了会同时毁三件事：命中区
            #   镜像（命中区不可见，只能从手型/形变位置看出来）、抓取球心落到抓取点
            #   的上下镜像处、手型跟随错位。1 = 对照态（当 top-down 直用）。
            #   **不要改成 1**：t14 `delta.y`、t18 `uvDelta.y`、t51 球心三条实测都
            #   指向 bottom-up（见 .agent-teams/efmi-drag-debug/T52-CURSOR-CONVENTION-README.md）。
            "w153 = 0",
            # align-t3：154 = ZZMI PHYS_PARAMS（70）语义——弹簧**直传**（半隐式
            # 积分，不再 ω=√k→Hz；D-1 随目标②换模型）：
            # x=grab_damping 0.86、y=grab_spring 0.176、z=release_damping 0.96、
            # w=release_spring 0.055
            f"x154 = {_fmt(self.grab_damping)}",
            f"y154 = {_fmt(self.grab_spring)}",
            f"z154 = {_fmt(self.release_damping)}",
            f"w154 = {_fmt(self.release_spring)}",
            # t37-P3：三态模式寄存器（simulate 抓取门 / deform 位移门经
            # IniParams[159].x 读；mode 1 = 仅命中不拖拽）
            f"x{EFMI_MODE_IP} = $ssmtdrag_efmi_mode_{ns}",
            # t42 P-1/P-2：全局倍率寄存器（160.x = mult_damping——P-2 damping
            # override 替换语义的全局回退倍率；161.x = mult_strength——P-1
            # strength 全局倍率，默认 0.333，对齐 ZZMI JIGGLE_MULTIPLIERS.z）
            f"x{EFMI_MULT_DAMPING_IP} = {_fmt(self.mult_damping)}",
            f"x{EFMI_MULT_STRENGTH_IP} = {_fmt(self.mult_strength)}",
            # align-t3：162 = ZZMI POLISH_PARAMS（71）语义扩展——x=释放踢
            # （phys_target_follow 1.10，ZZMI y71）、y=mouseYDir（ZZMI z71）、
            # z=目标跟随 follow（phys_release_kick 0.12，ZZMI w71）
            f"x{EFMI_POLISH_IP} = {_fmt(self.release_kick)}",
            f"y{EFMI_POLISH_IP} = {_fmt(DEFAULT_MOUSE_YDIR)}",
            f"z{EFMI_POLISH_IP} = {_fmt(self.target_follow)}",
            # align-t3：163 = 倍率扩展——x=mult_radius（ZZMI y72）、
            # y=mult_spring（ZZMI w72）、z=depth_pull（ZZMI z73，拖拽距离比例 ×
            # 冻结法线，替换旧定值 Y 偏移深度模型）、w=mouseXDir（ZZMI y73
            # 同源语义——JIGGLE_MULT_EXTRA 系槽位；t6 修复：t3 误写 162.w
            # 死槽 + 163.w 写 0，与 simulate 读取位 IniParams[163].w 错线）
            f"x{EFMI_MULT_EXTRA_IP} = {_fmt(self.mult_radius)}",
            f"y{EFMI_MULT_EXTRA_IP} = {_fmt(self.mult_spring)}",
            f"z{EFMI_MULT_EXTRA_IP} = {_fmt(self.depth_pull)}",
            f"w{EFMI_MULT_EXTRA_IP} = {_fmt(DEFAULT_MOUSE_XDIR)}",
            # align-t3：164 = ZZMI TIME_PARAMS（76）语义——x=sim_speed（$全局
            # 3.0）、y=max_step（$全局 3.0）；shader 步长 = clamp(dt·60·speed,
            # 0.05, max_step)（原作步长公式，screen_state L63-69）
            # t47-D2（t30 审查）：本槽**只分配 .x/.y**。.z/.w 曾用于一个调试开关
            # （`$ssmtdrag_efmi_freeze_A` → 位移闸门的 A/B），该开关已随 t47 修复撤除、
            # 消费者归零，故不再发射 —— 免得后人看到一个没人读的 `z164 = 0`
            # 去反推不存在的消费者，也免得多写一个共用 t120 槽位。
            f"x{EFMI_TIME_IP} = $ssmtdrag_efmi_sim_speed_{ns}",
            f"y{EFMI_TIME_IP} = $ssmtdrag_efmi_max_step_{ns}",
            # align-t3：165 = ZZMI RELEASE_BOOST（97）语义——释放动态阻尼
            # x=boost 1.05 / y=decay 0.92（screen_state L392-406 消费）
            f"x{EFMI_RELEASE_BOOST_IP} = $ssmtdrag_efmi_release_boost_{ns}",
            f"y{EFMI_RELEASE_BOOST_IP} = $ssmtdrag_efmi_release_decay_{ns}",
            # t54：165.z = 手型**渲染位置**的 Y 镜像。0 = 悬停/蓄力的命中光标镜像
            #   （手型才落在鼠标下）；抓取（拖拽）路径在 efmi_hand_preview.hlsl 里用
            #   `IniParams[165].z < 0.5 && !capturing` **明确排除** —— 镜像作用在最终
            #   锚点 `UvToYupPx(frozenAnchor + uvDelta)` 上，抓取态含拖拽跟随量，
            #   一起镜像会把跟随方向反号（用户实测「拖拽的光标错位」）。1 = 关。
            #   显式发射：本槽此前无人写（缓冲区默认 0，值相同），写出来才是可读、可测的契约。
            f"z{EFMI_RELEASE_BOOST_IP} = 0",
            f"run = CustomShaderEFMIDragSimulate_{ns}",
        ])
        if self._feature_skd():
            # F1 形态键联动：鼠标位移归约 + 每帧驱动 dispatch（仅形态键联动
            # 开启时发射，避免悬空引用）
            lines.extend([
                f"$ssmtdrag_efmi_skdy_{ns} = $cursorY - $ssmtdrag_efmi_skprev_y_{ns}",
                f"$ssmtdrag_efmi_skdx_{ns} = $cursorX - $ssmtdrag_efmi_skprev_x_{ns}",
                f"$ssmtdrag_efmi_skprev_y_{ns} = $cursorY",
                f"$ssmtdrag_efmi_skprev_x_{ns} = $cursorX",
                # boot 清零 + 冷启动播种标志（ClickExport 链：变量变化亦置位，
                # 驱动 CS 播种后本行清零）
                f"if $ssmtdrag_efmi_booted_{ns} == 0",
                f"\t$ssmtdrag_efmi_seed_pending_{ns} = 1",
                f"\tclear = ResourceEFMIDragShapeKeyDrive_{ns} 0.0",
                f"\tclear = ResourceEFMIDragShapeKeyDir_{ns} 0.0",
                f"\tclear = ResourceEFMIDragShapeKeyDragLatch_{ns} 0.0",
                f"\tclear = ResourceEFMIDragShapeKeyClickCount_{ns}",
                f"\tclear = ResourceEFMIDragShapeKeyClickCountF_{ns} 0.0",
                f"\tclear = ResourceEFMIDragShapeKeyActiveDir_{ns}",
                f"\t$ssmtdrag_efmi_booted_{ns} = 1",
                "endif",
                # t41 P-7: ZZMI 严格互斥——形态键驱动仅 mode 1（门在 shader 内部：
                # mode!=1 → 保持 dir 槽 + 清 DragLatch + return，mode 2 = 变形专用）。
                # t43/P-8 严格对齐 ZZMI：CS dispatch 与 seed_pending 清零**无条件**
                # （ZZMI boot CL 无条件 run，播种段在 drive CS 的 mode 门之前）——
                # mode 2 下也完成播种但不驱动，回 mode 1 无需等待播种。
                f"run = CustomShaderEFMIDragShapeKeyDrive_{ns}",
                f"$ssmtdrag_efmi_seed_pending_{ns} = 0",
            ])
        if self._feature_var() and self._drag_drive_var_sync_bindings():
            # F4 变量联动：回读（boot 门控）→ 同步（每帧无条件，变量优先）；
            # 绑定项非空才发射（防悬空引用）
            lines.extend([
                f"if $ssmtdrag_efmi_booted_{ns} == 1",
                f"\tpre run = CommandListEFMIDragShapeKeyVarReadback_{ns}",
                "endif",
                f"run = CustomShaderEFMIDragShapeKeyVarSync_{ns}",
            ])
        if self._feature_panel() or self.enable_hand_cursor:
            # F3 面板联动 / 手型光标：发布命中（每帧）→ 帧末 store 回读（一帧延迟
            # 稳定消费；未命中/陈旧由发布 CS 的 fresh 门控写 -1）。
            # t6/P1：发布与回读**必须成对**执行——回读是 `post run`，若被门控跳过
            # 则不执行，变量保持上一帧值。故用「总门不通过 → 显式写 -1」的兜底
            # 形式（对标 ZZMI UI 兜底 L8963-8966：未允许时主动失效），而不是跳过
            # 整段，避免面板读到陈旧命中。
            _ui_detected_var, _ui_zone_var = self._runtime_variable_names(ns)[1:]
            lines.extend([
                f"if $ssmtdrag_ObjectDetectAllowed_{ns} == 1",
                f"\trun = CustomShaderEFMIDragUIPublish_{ns}",
                f"\tpost run = CommandListEFMIDragUIReadback_{ns}",
                "else",
                f"\t{_ui_detected_var} = -1",
                f"\t{_ui_zone_var} = -1",
                "endif",
            ])
        if self.enable_hand_cursor:
            # align-t3：手部蓄力归约（ZZMI [Present] L4985-5023 同款移植）——
            # 独按 LMB/RMB 期间 hold_fraction 持续累计并按各自 windup_time 封顶；
            # 组合键/非独按归零。**EFMI 适配**：EFMI 抓取手势 = 修饰键+按键
            # （Alt+LMB），「独按」须排除修饰键按住的真实抓取（否则蓄力倾斜与
            # 抓取拉伸倾斜双叠加）——lone = 按键按下 && 另一键未按 &&
            # modifier==0；grab_key=NONE 常开模式 modifier 恒 1 → 恒无蓄力
            # （每次按下都是真实抓取，语义一致）。
            lines.extend([
                f"if $ssmtdrag_efmi_lmb_{ns} == 1 && $ssmtdrag_efmi_lmb_prev_{ns} == 0",
                f"\t$ssmtdrag_efmi_lmb_press_time_{ns} = time",
                "endif",
                f"if $ssmtdrag_efmi_rmb_{ns} == 1 && $ssmtdrag_efmi_rmb_prev_{ns} == 0",
                f"\t$ssmtdrag_efmi_rmb_press_time_{ns} = time",
                "endif",
                f"if $ssmtdrag_efmi_lmb_{ns} == 1",
                f"\tif $ssmtdrag_efmi_rmb_{ns} == 0",
                f"\t\tif $ssmtdrag_efmi_modifier_{ns} == 0",
                f"\t\t\t$ssmtdrag_efmi_lmb_hold_fraction_{ns} = time - $ssmtdrag_efmi_lmb_press_time_{ns}",
                f"\t\t\tif $ssmtdrag_efmi_lmb_hold_fraction_{ns} > $ssmtdrag_efmi_hand_windup_time_{ns}",
                f"\t\t\t\t$ssmtdrag_efmi_lmb_hold_fraction_{ns} = $ssmtdrag_efmi_hand_windup_time_{ns}",
                "\t\t\tendif",
                "\t\telse",
                f"\t\t\t$ssmtdrag_efmi_lmb_hold_fraction_{ns} = 0",
                "\t\tendif",
                "\telse",
                f"\t\t$ssmtdrag_efmi_lmb_hold_fraction_{ns} = 0",
                "\tendif",
                "else",
                f"\t$ssmtdrag_efmi_lmb_hold_fraction_{ns} = 0",
                "endif",
                f"$ssmtdrag_efmi_rmb_lone_hold_{ns} = 0",
                f"if $ssmtdrag_efmi_rmb_{ns} == 1",
                f"\tif $ssmtdrag_efmi_lmb_{ns} == 0",
                f"\t\tif $ssmtdrag_efmi_modifier_{ns} == 0",
                f"\t\t\t$ssmtdrag_efmi_rmb_lone_hold_{ns} = 1",
                f"\t\t\t$ssmtdrag_efmi_rmb_hold_fraction_{ns} = time - $ssmtdrag_efmi_rmb_press_time_{ns}",
                f"\t\t\tif $ssmtdrag_efmi_rmb_hold_fraction_{ns} > $ssmtdrag_efmi_hand_rmb_windup_time_{ns}",
                f"\t\t\t\t$ssmtdrag_efmi_rmb_hold_fraction_{ns} = $ssmtdrag_efmi_hand_rmb_windup_time_{ns}",
                "\t\t\tendif",
                "\t\telse",
                f"\t\t\t$ssmtdrag_efmi_rmb_hold_fraction_{ns} = 0",
                "\t\tendif",
                "\telse",
                f"\t\t$ssmtdrag_efmi_rmb_hold_fraction_{ns} = 0",
                "\tendif",
                "else",
                f"\t$ssmtdrag_efmi_rmb_hold_fraction_{ns} = 0",
                "endif",
                # 归一化蓄力进度（÷windup_time；t43：ini-param 只支持单分量行，
                # 除法在 [Present] 归约完成——shader saturate 后 0..1）
                f"$ssmtdrag_efmi_lmb_wfrac_{ns} = $ssmtdrag_efmi_lmb_hold_fraction_{ns} / $ssmtdrag_efmi_hand_windup_time_{ns}",
                f"$ssmtdrag_efmi_rmb_wfrac_{ns} = $ssmtdrag_efmi_rmb_hold_fraction_{ns} / $ssmtdrag_efmi_hand_rmb_windup_time_{ns}",
                f"$ssmtdrag_efmi_lmb_prev_{ns} = $ssmtdrag_efmi_lmb_{ns}",
                f"$ssmtdrag_efmi_rmb_prev_{ns} = $ssmtdrag_efmi_rmb_{ns}",
            ])
            # 手型光标（t22 UX 对齐恢复；align-t3 三态语义）：预览 CS →
            # store 捕获状态 → 按状态画 Action/NoAction（描边垫底 + 填充）
            # t45：run 引用与发射段名统一（zzmi 原作命名惯例——NoAction 系
            # 无后缀 PresentHand/PresentHandOutline、Action 系带后缀）
            # t6/P1：整段用 ALLOWED（enabled && modifier && drawn）门控——
            # 不按 Alt 时既不跑预览 CS 也不画手型，杜绝"不按 ALT 也显示手型"
            # 的误导（对标 ZZMI 手型预览门 L1711-1730）。
            lines.extend([
                f"if $ssmtdrag_efmi_allowed_{ns} == 1",
                f"\trun = CustomShaderEFMIDragHandPreview_{ns}",
                # Preview[2].w = 预览状态（0=隐藏/1=悬停或蓄力/2=真实抓取；
                # float 索引 11）——>1.5 = 抓取态
                f"\tstore = $ssmtdrag_efmi_hand_action_{ns}, ResourceEFMIDragHandPreview_{ns}, 11",
                # t32-B 诊断：Preview[3].x = 手型状态码（float 索引 12）——
                # 0=可见 / 1=无命中隐藏（align-t3 起简化二值；D-6 基无效即
                # discard 对齐 ZZMI，废弃越界/基无效细分）
                f"\tstore = $ssmtdrag_efmi_hand_state_{ns}, ResourceEFMIDragHandPreview_{ns}, 12",
                # align-t3：网格切换对齐 ZZMI L5138-5144——真实抓取 **或 RMB
                # 独按蓄力** 即换 Action（握拳）网格
                f"\tif $ssmtdrag_efmi_hand_action_{ns} > 1.5 || $ssmtdrag_efmi_rmb_lone_hold_{ns} == 1",
                f"\t\trun = CustomShaderEFMIDragPresentHandActionOutline_{ns}",
                f"\t\trun = CustomShaderEFMIDragPresentHandAction_{ns}",
                "\telse",
                f"\t\trun = CustomShaderEFMIDragPresentHandOutline_{ns}",
                f"\t\trun = CustomShaderEFMIDragPresentHand_{ns}",
                "\tendif",
                "endif",
            ])
        # 帧标签递增必须位于所有 run 之后（终审 F1，high 阻断）：
        # detect 绘制期写 candidate.w = 本帧 Present 初的全局帧值；递增在 Present
        # 末 → 下一帧 Present 的 simulate/ui_publish/shapekey_drive 读到相同帧值，
        # fresh 判定（candidate.w == frame）成立。若递增在开头，Present 期读到
        # N+1 而 detect 写 N → 恒假 → 捕获/弹簧/面板/形态键驱动全部惰性。
        lines.extend([
            # t6/P1：drawn 帧末清 0（ZZMI L5172 同款）——下一帧由 hook 首次绘制
            # 重新置 1；无绘制帧 ObjectDetectAllowed 自然落 0，不产生命中/手型。
            f"$ssmtdrag_efmi_drawn_{ns} = 0",
            f"$ssmtdrag_efmi_frame_{ns} = $ssmtdrag_efmi_frame_{ns} + 1",
            EFMI_PRESENT_END,
        ])
        present_lines.extend(lines)

    def _emit_panel_sections(self, sections, ns):
        """F3 面板联动：命中发布 CS + UIReadback CL + UIDetect/UIZone 资源
        （独立实现，等价 zzmi F3 桥；面板变量名契约复用
        $ssmtdrag_ui_detected/zone——t5 §6 决策：面板侧零改动）。

        发布 CS 每帧对 Candidate 做跨实例赢家仲裁，写两个 R32 标量槽；
        UIReadback 经 store 读入只读联动变量（store 不带 ref，依赖
        StoreCommand，实机验证项——不支持则静默降级）。
        """
        res = EFMI_RES_SHADER_DIR
        resources = {
            f"[ResourceEFMIDragUIDetect_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT", "array = 1",
            ],
            f"[ResourceEFMIDragUIZone_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT", "array = 1",
            ],
        }
        for sec, lines in resources.items():
            sections.setdefault(sec, lines)
        sections[f"[CustomShaderEFMIDragUIPublish_{ns}]"] = [
            f"cs = {res}/efmi_ui_publish.hlsl",
            f"cs-t0 = ResourceEFMIDragCandidate_{ns}",
            f"cs-u0 = ResourceEFMIDragUIDetect_{ns}",
            f"cs-u1 = ResourceEFMIDragUIZone_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-t0 = null", "post cs-u0 = null", "post cs-u1 = null",
        ]
        _mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
        sections[f"[CommandListEFMIDragUIReadback_{ns}]"] = [
            f"store = {ui_detected_var}, ResourceEFMIDragUIDetect_{ns}, 0",
            f"store = {ui_zone_var}, ResourceEFMIDragUIZone_{ns}, 0",
        ]

    def _hand_param_lines(self, ns, outline):
        """PresentHand 四段共享的 IniParams 寄存器行（align-t3：ZZMI
        `_hand_param_lines` L4322-4356 同款参数面，槽位映射到 EFMI 高位区
        187-195——语义/默认值与 ZZMI persist 全表一致；t43 单分量行）。"""
        P = EFMI_HAND_SURFACE_IP      # 187 = ZZMI 83 表面 3 项
        C = EFMI_HAND_CENTER_IP       # 188 = ZZMI 89 中心
        S = EFMI_HAND_SCALE_IP        # 189 = ZZMI 90 缩放+不透明
        ST = EFMI_HAND_STATE_IP       # 190 = ZZMI 91 蓄力进度+time
        T = EFMI_HAND_TILT_IP         # 191 = ZZMI 92 倾斜 4 项
        V = EFMI_HAND_VIBRATE_IP      # 192 = ZZMI 93 振动 4 项
        U = EFMI_HAND_UPRIGHT_IP      # 193 = ZZMI 94 直立+抓取倾斜
        R = EFMI_HAND_REFERENCE_IP    # 194 = ZZMI 95 参考高
        O = EFMI_HAND_OUTLINE_IP      # 195 = ZZMI 98 描边 3 项
        lines = [
            f"x{P} = $ssmtdrag_efmi_hand_surface_clip_{ns}",
            f"y{P} = $ssmtdrag_efmi_hand_surface_lift_{ns}",
            f"z{P} = $ssmtdrag_efmi_hand_surface_softness_{ns}",
            f"x{C} = $ssmtdrag_efmi_hand_center_x_{ns}",
            f"y{C} = $ssmtdrag_efmi_hand_center_y_{ns}",
            f"z{C} = $ssmtdrag_efmi_hand_center_z_{ns}",
            f"x{S} = $ssmtdrag_efmi_hand_scale_{ns}",
            f"y{S} = $ssmtdrag_efmi_hand_opacity_{ns}",
            # t43：ini-param 单分量行——÷windup_time 已在 [Present] 归约完成
            f"x{ST} = $ssmtdrag_efmi_lmb_wfrac_{ns}",
            f"y{ST} = time",
            f"z{ST} = $ssmtdrag_efmi_rmb_wfrac_{ns}",
            f"x{T} = $ssmtdrag_efmi_hand_tilt_min_{ns}",
            f"y{T} = $ssmtdrag_efmi_hand_tilt_rmb_min_{ns}",
            f"z{T} = $ssmtdrag_efmi_hand_tilt_max_deg_{ns}",
            f"w{T} = $ssmtdrag_efmi_hand_tilt_rmb_max_deg_{ns}",
            f"x{V} = $ssmtdrag_efmi_hand_vibrate_threshold_{ns}",
            f"y{V} = $ssmtdrag_efmi_hand_vibrate_period_slow_{ns}",
            f"z{V} = $ssmtdrag_efmi_hand_vibrate_period_fast_{ns}",
            f"w{V} = $ssmtdrag_efmi_hand_vibrate_amplitude_{ns}",
            f"x{U} = $ssmtdrag_efmi_hand_upright_max_deg_{ns}",
            f"z{U} = $ssmtdrag_efmi_hand_tilt_grab_max_deg_{ns}",
            f"x{R} = $ssmtdrag_efmi_hand_reference_height_{ns}",
        ]
        if outline:
            lines.extend([
                f"x{O} = $ssmtdrag_efmi_hand_outline_width_{ns}",
                f"y{O} = 1",
                f"z{O} = $ssmtdrag_efmi_hand_outline_opacity_{ns}",
            ])
        else:
            lines.append(f"y{O} = 0")
        return lines

    def _emit_hand_sections(self, sections, ns):
        """手型光标（enable_hand_cursor 时发射；align-t3 全面对齐 ZZMI 渲染链：
        rzm_jiggle_hand H1-H16 机制 + 三态预览状态机 + 25 项 persist 参数面）。

        Present 每帧：HandPreview CS（Candidate 赢家 + AnchorProject gizmo 投影
        → 三态（悬停/蓄力/抓取）锚点/基/状态，ZZMI rzm_jiggle_cursor_preview
        同语义）→ store 读状态 → 真实抓取或 RMB 独按蓄力画 Action、否则
        NoAction（描边垫底 + 填充）。
        资产（P3F_C4F 网格 + 烘焙法线）与 ZZMI 字节级相同（6/6 SAME），
        拷贝于 EFMI 独立目录；着色器为 ZZMI 机制的 EFMI 移植实现。
        """
        res = EFMI_RES_SHADER_DIR
        ip = EFMI_HAND_IP_BASE
        resources = {
            # align-t3：预览记录 3→4 float4（ZZMI CursorPreview 契约：
            # [0]=锚点 px+屏幕 zw / [1]=gizmo XY 投影 / [2]=gizmo Z 投影+有效+
            # 状态 / [3]=诊断状态码+蓄力/抓取旗标+stretchFraction）
            f"[ResourceEFMIDragHandPreview_{ns}]": [
                "type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 4",
            ],
        }
        for mesh in ("Action", "NoAction"):
            resources[f"[ResourceEFMIDragHand{mesh}VB_{ns}]"] = [
                "type = Buffer", "stride = 28",
                f"filename = {res}/Hand{mesh}.buf",
            ]
            resources[f"[ResourceEFMIDragHand{mesh}IB_{ns}]"] = [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/Hand{mesh}.ib",
            ]
            resources[f"[ResourceEFMIDragHand{mesh}Normal_{ns}]"] = [
                "type = Buffer", "format = R32G32B32_FLOAT",
                f"filename = {res}/Hand{mesh}_Normal.buf",
            ]
        for sec, lines in resources.items():
            sections.setdefault(sec, lines)
        # 预览 CS：Candidate 赢家仲裁 + AnchorProject gizmo 投影 → HandPreview
        # 三态记录；cs-t2 = 锚点投影 RT（gizmo 行 zone*8+4..7 直读——
        # detect 无改动的关键：预览自足读取投影，不扩展 Candidate 步幅）
        # t43：单分量行 + res_width/res_height（3Dmigoto 内建；$screenW/$screenH
        # 为 zzmi 自定义维护变量，EFMI 无初始化链，Zmd 构建不可用）
        sections[f"[CustomShaderEFMIDragHandPreview_{ns}]"] = [
            f"cs = {res}/efmi_hand_preview.hlsl",
            f"cs-t0 = ResourceEFMIDragCandidate_{ns}",
            f"cs-t1 = ResourceEFMIDragState_{ns}",
            f"cs-t2 = ResourceEFMIDragAnchorProject_{ns}",
            f"cs-u0 = ResourceEFMIDragHandPreview_{ns}",
            f"x{ip} = res_width",
            f"y{ip} = res_height",
            f"z{ip} = 0",
            f"w{ip} = 0",
            "dispatch = 1, 1, 1",
            "post cs-t0 = null", "post cs-t1 = null", "post cs-t2 = null", "post cs-u0 = null",
        ]
        # PresentHand ×4：NoAction/Action × 描边/填充（描边先画垫底只露轮廓边）
        for action in (False, True):
            for outline in (False, True):
                tag = ("Action" if action else "") + ("Outline" if outline else "")
                mesh = "Action" if action else "NoAction"
                lines = [
                    f"vs = {res}/efmi_hand.hlsl",
                    f"ps = {res}/efmi_hand.hlsl",
                    "blend = ADD SRC_ALPHA INV_SRC_ALPHA",
                    "cull = none",
                    "topology = triangle_list",
                    "o0 = set_viewport bb",
                    f"vs-t67 = ResourceEFMIDragHandPreview_{ns}",
                    f"vs-t69 = ResourceEFMIDragHand{mesh}Normal_{ns}",
                    f"x{ip} = res_width",
                    f"y{ip} = res_height",
                    f"z{ip} = 0",
                    f"w{ip} = 0",
                ]
                lines.extend(self._hand_param_lines(ns, outline))
                lines.extend([
                    f"vb0 = ResourceEFMIDragHand{mesh}VB_{ns}",
                    f"ib = ResourceEFMIDragHand{mesh}IB_{ns}",
                    # 254 quads → 508 tris → 1524 indices（与手部 IB 资产一致）
                    f"DrawIndexed = {_fmt(EFMI_HAND_INDEX_COUNT)}, 0, 0",
                    "post vs-t67 = null", "post vs-t69 = null",
                    "post vb0 = null", "post ib = null",
                ])
                sections[f"[CustomShaderEFMIDragPresentHand{tag}_{ns}]"] = lines

    def _ensure_probe_identification(self):
        """识别产品链路（t50 F1 接入）：幂等——工作空间缓存存在则跳过；
        否则从 FrameAnalysis 现场识别（pass ordinal + ps hash 集合）并写缓存，
        供门控阈值（_probe_pass_threshold）与诊断使用。"""
        workspace_root = getattr(self, "_probe_workspace_root", "")
        if not workspace_root:
            return
        if load_workspace_probe_hash(workspace_root):
            return
        frame_dir = _resolve_frame_analysis_dir(workspace_root)
        if not frame_dir:
            return
        ib_hashes = self._parse_hash_values(self.hash_values)
        log_path = os.path.join(frame_dir, "log.txt")
        hashes = identify_main_color_ps_hashes(log_path, ib_hashes)
        depth, color, first_ordinal = identify_pass_ordinal(log_path, ib_hashes)
        if not hashes and first_ordinal == 0:
            return
        save_workspace_probe_hash(
            workspace_root, hashes, source_dir=frame_dir,
            pass_counts={
                "depth_pass_count": depth,
                "color_pass_count": color,
                "color_pass_first_ordinal": first_ordinal,
            },
        )
        print(
            f"[EFMIDrag] 主色 pass 识别完成（{frame_dir}）：depth={depth} "
            f"color={color} first_ordinal={first_ordinal}——门控阈值 "
            f"= {self._probe_pass_threshold()}"
        )

    def _emit_sections(self, sections, components, ns, mod_export_path=None):
        # 识别产品链路（幂等；阈值由识别值驱动——§10.6 pass 计数器定案）
        self._ensure_probe_identification()
        self._emit_globals(sections, ns)
        self._emit_key_sections(sections, ns)
        self._emit_global_resources(sections, components, ns)
        for comp in components:
            self._emit_component_resources(sections, comp, ns)
        self._emit_probe_detect_sections(sections, components, ns)
        self._emit_deform_sections(sections, components, ns)
        self._emit_simulate_section(sections, ns)
        if self._feature_skd():
            self._emit_shapekey_drive_sections(sections, ns, mod_export_path)
        if self._feature_var():
            self._emit_shapekey_var_sync_sections(sections, ns, mod_export_path)
        if self._feature_panel():
            self._emit_panel_sections(sections, ns)
        if self.enable_hand_cursor:
            self._emit_hand_sections(sections, ns)
        self._emit_present_block(sections, ns)

    # =======================================================================
    # 回调注入（MergedSkeleton_Apply 之后、原始绘制之前）
    # =======================================================================

    @staticmethod
    def _find_draw_line_index(lines):
        """找到 drawindexedinstanced / drawindexed 绘制行（注入锚点）。"""
        for i, line in enumerate(lines):
            s = str(line).strip()
            if s.startswith("drawindexedinstanced") or (
                s.startswith("drawindexed") and not s.startswith("drawindexedinstanced")
            ):
                return i
        return None

    def _inject_hooks(self, sections, components, ns):
        for comp in components:
            for draw_sec, lines in comp["draw_entries"]:
                cleaned = _strip_hook_blocks(lines)
                idx = self._find_draw_line_index(cleaned)
                if idx is None:
                    # ARCH-10：剥离结果**必须**先写回，否则本次 `_strip_hook_blocks`
                    # 的成果被丢弃、sections[draw_sec] 保留未剥离副本（旧钩子块残留，
                    # 破坏"剥离后重发"的幂等纪律；不会重复注入但语义不一致）。
                    sections[draw_sec] = cleaned
                    print(
                        f"[EFMIDrag][WARNING] {draw_sec} 未找到绘制行，跳过注入"
                        "（已剥离旧钩子块，保持幂等）"
                    )
                    continue
                pre = [EFMI_HOOK_BEGIN]
                # 门控定案（researcher §10.6 最终裁决：NumViews 判据不可用——
                # 颜色 pass NumViews 实测 5/2 非恒定）→ pass 计数器：帧标签变化
                # 清零 + 每绘制 +1；实证深度 pass 恒第 1/2 次（NumViews:0 无 RT）、
                # 颜色 pass 恒第 3-5 次（o0=3315d2b5 主色 RT、相机 cb0 一致投影等价）。
                # R8/P0 的「帧 latch 去 pass 序号」已被 t4/P0 与 t6 取代：probe 门控
                # 现由 `_emit_probe_detect_sections` 消费 `_probe_exec_pass()`
                # （颜色层，默认 3）+ `ObjectDetectAllowed` 总门共同决定；
                # pass 计数器在本块内维护（帧变化清零 + 每绘制 +1）并**参与门控**，
                # 同时仍可用于 FrameAnalysis 诊断。识别侧
                # _probe_pass_threshold/_ensure_probe_identification 不变。
                pre.extend([
                    f"if $ssmtdrag_efmi_frame_{ns} != $ssmtdrag_efmi_frame_prev_{ns}",
                    f"\t$ssmtdrag_efmi_pass_{ns} = 0",
                    # t6/P1：drawn 旗标——本帧该组件确有绘制（ZZMI L4664 同款），
                    # Present 帧末清 0（ZZMI L5172 同款）。
                    f"\t$ssmtdrag_efmi_drawn_{ns} = 1",
                    # t6 修正 A（问题 1）：probe_pass 帧作用域复位——不复位会与
                    # 兜底判据叠加造成逐帧 p3/p1 交替（手型闪烁、拖拽隔帧失效）。
                    f"\t$ssmtdrag_efmi_probe_pass_{ns} = -1",
                    f"\t$ssmtdrag_efmi_frame_prev_{ns} = $ssmtdrag_efmi_frame_{ns}",
                    "endif",
                    f"$ssmtdrag_efmi_pass_{ns} = $ssmtdrag_efmi_pass_{ns} + 1",
                    # t6/P1：ALT 运行态门控（照搬 ZZMI L4211 判据；drawn 已在上方置 1）
                    #   ObjectDetectAllowed = enabled>=1 && inputMode==0 && mode>=1 && drawn==1
                    # **EFMI 适配（关键）**：ZZMI 的 `$ssmtdrag_mode_{ns}` 是二值
                    # 修饰键旗标（==1 即"允许"），故原文用 `== 1`；EFMI 的
                    # `$ssmtdrag_efmi_mode_{ns}` 是 t37-P3 三态运行模式
                    # （0=关 / 1=仅命中 / 2=命中+拖拽，默认 **2**）。若照抄 `== 1`
                    # 会在默认 mode=2 下永久置 0 → detect/deform 全灭、拖拽彻底失效。
                    # 故按 EFMI 三态语义取 `>= 1`（mode 1 与 2 都允许检测与变形；
                    # mode 0 = 全关，仍被排除）。
                    f"if $ssmtdrag_efmi_enabled_{ns} >= 1 && $inputMode == 0 "
                    f"&& $ssmtdrag_efmi_mode_{ns} >= 1 && $ssmtdrag_efmi_drawn_{ns} == 1",
                    f"\t$ssmtdrag_ObjectDetectAllowed_{ns} = 1",
                    "else",
                    f"\t$ssmtdrag_ObjectDetectAllowed_{ns} = 0",
                    "endif",
                    # 显式语义旗标：enabled && modifier && drawn（供 Present 侧/诊断）
                    f"if $ssmtdrag_efmi_enabled_{ns} == 1 "
                    f"&& $ssmtdrag_efmi_modifier_{ns} == 1 "
                    f"&& $ssmtdrag_efmi_drawn_{ns} == 1",
                    f"\t$ssmtdrag_efmi_allowed_{ns} = 1",
                    "else",
                    f"\t$ssmtdrag_efmi_allowed_{ns} = 0",
                    "endif",
                ])
                pre.append(f"run = CommandListEFMIDragApply_{comp['comp_name']}_{ns}")
                pre.append(EFMI_HOOK_END)
                # B1/t13：probe 级联移到真实 draw 之后（EFMI_PROBE_BEGIN..END 独立
                # 标记）——ProbeBody/ProbeAnchors 段尾 UnbindAllRenderTargets 清空
                # OM 颜色 RT；若其在真实 draw 之前执行，主体 draw 无渲染目标 →
                # Alt 按住模型消失（t12 §6 裁决：RT 被清为主因，非顶点缓冲）。
                # draw 后执行则本帧主体绘制不受影响；结果下一帧消费（Present
                # 各消费端本就在帧末读 Candidate，滞后 1 帧可接受）。
                post = []
                if comp["role"] == "body":
                    post = [
                        EFMI_PROBE_BEGIN,
                        f"run = CommandListEFMIDragProbe_{ns}",
                        EFMI_PROBE_END,
                    ]
                cleaned[idx:idx] = pre
                if post:
                    cleaned[idx + len(pre) + 1:idx + len(pre) + 1] = post
                sections[draw_sec] = cleaned


# ---------------------------------------------------------------------------
# EFMI 权重预览（t25 恢复，zzmi 式语义独立实现——depsgraph 网格数据 + 选中
# 区域空物体才显示 + 高斯球权重场绘制；禁止 import zzmi 预览函数）
# ---------------------------------------------------------------------------

_efmi_preview_draw_handler = None
_efmi_preview_timer = None
_efmi_preview_deps_handler = None

_EFMI_PREVIEW_LOGIC = "EFMI"


def _efmi_preview_zone_configs_from_scene(node):
    """预览用区域配置（与导出同源：_collect_zone_configs）。"""
    try:
        exporter = DragInteractionEFMIExporter(node)
        return exporter._collect_zone_configs()
    except Exception:
        return []


def _efmi_preview_selected_zone_ids(node):
    """zzmi 语义：仅当前选中且属于本节点列表的区域空物体参与预览。
    返回 [(zone_id, obj), ...]（zone_id = **稳定 id**（_collect_enabled_zone_entries，
    t29），非配置序——重排/增删后预览区权重仍对应同一物理区域）。"""
    try:
        import bpy
        selected = {
            getattr(o, "name", None): o
            for o in getattr(bpy.context, "selected_objects", None) or ()
            if getattr(o, "type", "") == 'EMPTY'
            and getattr(o, "ssmt_drag_zone", None) is not None
        }
    except Exception:
        return []
    out = []
    try:
        exporter = DragInteractionEFMIExporter(node)
        entries = exporter._collect_enabled_zone_entries()
    except Exception:
        entries = []
    for zone_id, item in entries:
        obj = getattr(item, "zone_object", None)
        if obj is None:
            continue
        if getattr(obj, "name", None) in selected:
            out.append((zone_id, obj))
    return out


def _efmi_preview_targets(node):
    """预览网格解析（与 zzmi _preview_targets 同语义，独立实现）：
    集合优先（递归全部 MESH），未设集合回退单物体 preview_target。"""
    try:
        collection = getattr(node, "preview_collection", None)
        if collection is not None:
            objects = getattr(collection, "all_objects", None)
            if objects is None:
                objects = getattr(collection, "objects", ())
            unique = {}
            for obj in objects:
                if getattr(obj, "type", None) == 'MESH':
                    unique[getattr(obj, "name_full", obj.name)] = obj
            return [unique[name] for name in sorted(unique, key=str.casefold)]
        target = getattr(node, "preview_target", None)
        return [target] if target is not None else []
    except Exception:
        return []


def _efmi_preview_find_node():
    """场景里第一个 EFMI 模式拖拽节点。"""
    try:
        import bpy
        from ..common.global_config import GlobalConfig
        logic = str(getattr(GlobalConfig, "logic_name", "") or "")
        if logic != _EFMI_PREVIEW_LOGIC:
            return None
        for tree in getattr(bpy.data, "node_groups", None) or ():
            for n in getattr(tree, "nodes", None) or ():
                if getattr(n, "bl_idname", None) == "SSMTNode_PostProcess_DragInteraction":
                    return n
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# 热力图色带（t33 第二轮修复：权重 → 颜色映射独立实现，zzmi
# weights_to_colors 同视觉语义——蓝→青→绿→黄→红 5 段分段线性、按绝对值
# 映射不归一化、w<=0 alpha=0；禁止 import zzmi/gb_core 预览函数）
# ---------------------------------------------------------------------------

_EFMI_PREVIEW_ALPHA = 0.85
_EFMI_PREVIEW_GHOST_FACTOR = 0.3

_EFMI_COLOR_STOPS = np.array([
    [0.05, 0.10, 0.90],   # 0.00 蓝
    [0.00, 0.80, 0.90],   # 0.25 青
    [0.10, 0.85, 0.15],   # 0.50 绿
    [1.00, 0.85, 0.05],   # 0.75 黄
    [1.00, 0.10, 0.05],   # 1.00 红
], dtype=np.float64)


def _efmi_preview_weights_to_colors(weights, opacity=_EFMI_PREVIEW_ALPHA):
    """权重 → 热力图 RGBA（独立实现，zzmi weights_to_colors 同视觉语义）。

    - 5 段分段线性插值（蓝→青→绿→黄→红）；
    - 按**绝对值**映射（不按最大值归一化——归一化会把中心强度调整抵消，
      热力图上看不到变化）；
    - 权重 <= 0 的顶点 alpha=0（不显示）。
    返回 (N, 4) float64。"""
    w = np.asarray(weights, dtype=np.float64).reshape(-1)
    n = w.shape[0]
    if n == 0:
        return np.zeros((0, 4), dtype=np.float64)
    t = np.clip(w, 0.0, 1.0)
    seg = t * (len(_EFMI_COLOR_STOPS) - 1)
    idx = np.clip(seg.astype(np.int64), 0, len(_EFMI_COLOR_STOPS) - 2)
    frac = (seg - idx)[:, None]
    rgb = _EFMI_COLOR_STOPS[idx] + (
        _EFMI_COLOR_STOPS[idx + 1] - _EFMI_COLOR_STOPS[idx]) * frac
    alpha = np.where(w > 0.0, float(opacity) * t, 0.0)
    return np.concatenate([rgb, alpha[:, None]], axis=1)


def _efmi_preview_mesh_world(mesh, mw):
    """evaluated 网格 → (世界坐标顶点 (N,3) float64, 三角形索引 (T,3) int64)。

    foreach_get 提速取顶点、显式 calc_loop_triangles 取拓扑（Blender 4.x
    必须显式调用）、matrix_world 变换到世界坐标（热力图在视口世界空间叠加）。
    失败返回 (None, None)。"""
    try:
        n = len(mesh.vertices)
        co = np.empty(n * 3, dtype=np.float64)
        mesh.vertices.foreach_get('co', co)
        verts = co.reshape(n, 3)
        if mw is not None:
            m4 = np.asarray(mw, dtype=np.float64).reshape(4, 4)
            world = np.empty_like(verts)
            world[:, 0] = (m4[0, 0] * verts[:, 0] + m4[0, 1] * verts[:, 1]
                           + m4[0, 2] * verts[:, 2] + m4[0, 3])
            world[:, 1] = (m4[1, 0] * verts[:, 0] + m4[1, 1] * verts[:, 1]
                           + m4[1, 2] * verts[:, 2] + m4[1, 3])
            world[:, 2] = (m4[2, 0] * verts[:, 0] + m4[2, 1] * verts[:, 1]
                           + m4[2, 2] * verts[:, 2] + m4[2, 3])
            verts = world
        try:
            mesh.calc_loop_triangles()
        except Exception:
            pass
        try:
            tris = np.asarray(
                [tuple(lt.vertices) for lt in mesh.loop_triangles],
                dtype=np.int64,
            ).reshape(-1, 3)
        except Exception:
            tris = None
        return verts, tris
    except Exception:
        return None, None


def _efmi_preview_weights(node, obj, zone_id, mesh_verts_world):
    """单个选中区的高斯球/椭球权重（P2-1/t26 + t28 椭球 + t32-A：所见即所得——
    直接用空物体 world 矩阵与网格 world 顶点，不做导出期变换（ref_inv/
    export_matrix），对齐 ZZMI _preview_target_field L5432-5434「否则球会被翻到
    另一侧」；烘焙仍走导出链。椭球形状（lin3 = world 上半 3×3 恒为命中范围，
    t32-A：radius 不再覆盖 lin3——命中范围由空物体变换决定）与
    _collect_zone_configs 同规——预览场语义跟随 bake（P2-2）。
    返回 (verts_world (N,3), weights (N,)) 或 (None, None)。"""
    try:
        if obj is None:
            # 兜底：调用方未传 obj 时按稳定 zone_id 解析（_collect_enabled_zone_entries）
            try:
                exporter = DragInteractionEFMIExporter(node)
                obj = next(
                    (getattr(it, "zone_object", None)
                     for zi, it in exporter._collect_enabled_zone_entries()
                     if zi == zone_id),
                    None,
                )
            except Exception:
                obj = None
        if obj is None:
            return None, None
        settings = getattr(obj, "ssmt_drag_zone", None)
        mw = getattr(obj, "matrix_world", None)
        if mw is None:
            return None, None
        world_m = np.asarray(mw, dtype=np.float64).reshape(4, 4)
        center = (float(world_m[0, 3]), float(world_m[1, 3]), float(world_m[2, 3]))
        strength = 1.0
        falloff_k = EFMI_BALL_FALLOFF_K
        grabbable = True
        if settings is not None:
            try:
                strength = float(getattr(settings, "brush_strength", 1.0) or 1.0)
            except (TypeError, ValueError):
                strength = 1.0
            try:
                falloff_k = float(getattr(settings, "brush_falloff_k", EFMI_BALL_FALLOFF_K) or EFMI_BALL_FALLOFF_K)
            except (TypeError, ValueError):
                falloff_k = EFMI_BALL_FALLOFF_K
            grabbable = bool(getattr(settings, "grabbable", True))
        # t32-A：命中范围恒 = world 上半 3×3（旋转×非均匀缩放全矩阵，跟随 bake）
        shape = np.asarray(world_m[:3, :3], dtype=np.float64).reshape(3, 3)
        world = np.asarray(mesh_verts_world, dtype=np.float64)
        # align-t3：预览场跟随 bake 的平台化（节点级 mask_plateau，ZZMI
        # _shape_field plateau 分支同规——所见即所得）
        try:
            plateau = float(getattr(node, "mask_plateau", 0.0) or 0.0)
        except (TypeError, ValueError):
            plateau = 0.0
        weights = bake_zone_ball(
            world, center, shape, strength=strength,
            falloff_k=falloff_k, grabbable=grabbable,
            plateau=min(max(plateau, 0.0), 0.99),
        )
        return world, weights
    except Exception:
        return None, None


def _efmi_preview_draw_callback():
    """每帧绘制回调：EFMI 模式 + 启用预览 + 选中区域空物体 → 在预览网格上
    叠加高斯球权重热力图。

    数据路径（t28 修复）：预览目标是 MESH（preview_target/preview_collection），
    区域是选中空物体（zzmi 同语义）；顶点取 depsgraph evaluated 网格世界坐标，
    权重 = bake_zone_ball（P2-1/t26：世界空间所见即所得，不做导出期变换）。

    热力图绘制（t33 第二轮修复）：POINTS 单色点 → **TRIS 面片 + 5 段色带**
    （蓝→青→绿→黄→红，_efmi_preview_weights_to_colors 绝对值映射），
    SMOOTH_COLOR 逐顶点色插值；幽灵层（alpha×0.3，depth NONE，透视可见）
    先画、主层（depth LESS_EQUAL，遮挡正确）后画——对齐 zzmi _drag_preview_draw
    双图层语义；Blender 4.x gpu from_builtin 兼容。"""
    try:
        import bpy
        import gpu
        from gpu_extras.batch import batch_for_shader
    except Exception:
        return
    try:
        node = _efmi_preview_find_node()
        if node is None or not getattr(node, "preview_weights", False):
            return
        targets = _efmi_preview_targets(node)
        selected = _efmi_preview_selected_zone_ids(node)
        if not targets or not selected:
            return
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        try:
            gpu.state.blend_set('ALPHA')
            gpu.state.depth_test_set('NONE')
        except Exception:
            pass
        try:
            depsgraph = bpy.context.evaluated_depsgraph_get()
        except Exception:
            depsgraph = None
        for obj in targets:
            if getattr(obj, "type", None) != 'MESH':
                continue
            mesh = None
            eval_obj = None
            mw = None
            if depsgraph is not None:
                try:
                    eval_obj = obj.evaluated_get(depsgraph)
                    mesh = eval_obj.to_mesh()
                    mw = getattr(eval_obj, "matrix_world", None)
                except Exception:
                    mesh = None
            if mesh is None:
                continue
            try:
                verts_world, tris = _efmi_preview_mesh_world(mesh, mw)
                if verts_world is None or tris is None or len(tris) == 0:
                    continue
                for zone_id, zone_obj in selected:
                    _local, weights = _efmi_preview_weights(
                        node, zone_obj, zone_id, verts_world)
                    if weights is None:
                        continue
                    colors = _efmi_preview_weights_to_colors(weights)
                    pos = np.asarray(verts_world, dtype=np.float32)
                    indices = np.asarray(tris, dtype=np.int32)
                    ghost = np.array(colors, copy=True)
                    ghost[:, 3] *= _EFMI_PREVIEW_GHOST_FACTOR
                    ghost_batch = batch_for_shader(
                        shader, 'TRIS',
                        {"pos": pos, "color": ghost.astype(np.float32)},
                        indices=indices)
                    main_batch = batch_for_shader(
                        shader, 'TRIS',
                        {"pos": pos, "color": colors.astype(np.float32)},
                        indices=indices)
                    # 幽灵层：深度测试关闭（透视可见）；主层：LESS_EQUAL（遮挡正确）
                    try:
                        gpu.state.depth_test_set('NONE')
                    except Exception:
                        pass
                    ghost_batch.draw(shader)
                    try:
                        gpu.state.depth_test_set('LESS_EQUAL')
                    except Exception:
                        pass
                    main_batch.draw(shader)
            finally:
                try:
                    if eval_obj is not None:
                        eval_obj.to_mesh_clear()
                except Exception:
                    pass
    except Exception:
        return


def _ensure_efmi_preview_running(node=None):
    """注册 EFMI 预览（幂等）：draw handler + timer + depsgraph update。"""
    global _efmi_preview_draw_handler, _efmi_preview_timer, _efmi_preview_deps_handler
    try:
        import bpy
    except ImportError:
        return False
    if _efmi_preview_draw_handler is None:
        try:
            _efmi_preview_draw_handler = bpy.types.SpaceView3D.draw_handler_add(
                _efmi_preview_draw_callback, (), 'WINDOW', 'POST_VIEW')
        except Exception:
            _efmi_preview_draw_handler = None
    if _efmi_preview_timer is None:
        try:
            _efmi_preview_timer = bpy.app.timers.register(
                _efmi_preview_timer_callback)
        except Exception:
            _efmi_preview_timer = None
    if _efmi_preview_deps_handler is None:
        try:
            handlers_list = bpy.app.handlers.depsgraph_update_post
            if _efmi_preview_deps_callback not in handlers_list:
                handlers_list.append(_efmi_preview_deps_callback)
            _efmi_preview_deps_handler = _efmi_preview_deps_callback
        except Exception:
            _efmi_preview_deps_handler = None
    return True


def _efmi_preview_active():
    """是否存在**任一**启用权重预览的 EFMI 节点（t30：多节点场景下不因首节点
    关闭而误判停摆）。非 EFMI 环境 / 无启用节点 → False。"""
    try:
        import bpy
        from ..common.global_config import GlobalConfig
        if str(getattr(GlobalConfig, "logic_name", "") or "") != _EFMI_PREVIEW_LOGIC:
            return False
        for tree in getattr(bpy.data, "node_groups", None) or ():
            for n in getattr(tree, "nodes", None) or ():
                if getattr(n, "bl_idname", None) == "SSMTNode_PostProcess_DragInteraction":
                    if getattr(n, "preview_weights", False):
                        return True
    except Exception:
        return False
    return False


def _efmi_preview_timer_callback():
    """定期刷新；无任一 EFMI 节点启用预览时**全量拆卸**（t30：draw + depsgraph
    handler 一并移除，防常驻空转/永久重绘 churn——旧行为只清 timer 泄漏 handler）。
    返回 None = Blender 自动注销当前 timer；draw_buttons/init 会在再次启用时
    重新注册。"""
    global _efmi_preview_timer
    if _efmi_preview_active():
        return 0.25
    # 无活动预览 → 拆 handler + 结束 timer（返回 None 让 Blender 注销当前 timer）
    _efmi_preview_teardown_handlers()
    _efmi_preview_timer = None
    return None



def _efmi_preview_deps_callback(depsgraph):
    """depsgraph 变更后强制重绘（网格编辑即时反馈）。"""
    try:
        import bpy
        for area in getattr(bpy.context.screen, "areas", None) or ():
            if area.type == 'VIEW_3D':
                area.tag_redraw()
    except Exception:
        return


def _efmi_preview_teardown_handlers():
    """移除 draw handler + depsgraph handler 并清模块引用（t30 核心；timer 不在此
    处理——timer 由自身的 Blender 注销/外部 cleanup 负责）。幂等。"""
    global _efmi_preview_draw_handler, _efmi_preview_deps_handler
    try:
        import bpy
        if _efmi_preview_draw_handler is not None:
            bpy.types.SpaceView3D.draw_handler_remove(_efmi_preview_draw_handler, 'WINDOW')
    except Exception:
        pass
    try:
        import bpy
        if _efmi_preview_deps_handler is not None:
            handlers = getattr(bpy.app.handlers, "depsgraph_update_post", None) or ()
            if _efmi_preview_deps_handler in handlers:
                handlers.remove(_efmi_preview_deps_handler)
    except Exception:
        pass
    _efmi_preview_draw_handler = None
    _efmi_preview_deps_handler = None


def _efmi_preview_cleanup():
    """全量卸载预览（模块重载/插件 unregister 用）：timer + draw + depsgraph。"""
    global _efmi_preview_timer
    try:
        import bpy
        if _efmi_preview_timer is not None:
            bpy.app.timers.unregister(_efmi_preview_timer)
    except Exception:
        pass
    _efmi_preview_timer = None
    _efmi_preview_teardown_handlers()


# ---------------------------------------------------------------------------
# 模块注册钩子（ARCH-09）
# ---------------------------------------------------------------------------
# 本模块是**纯库模块**：只有 `DragInteractionEFMIExporter` 与一组烘焙/预览函数，
# 自身不注册任何 bpy 类（所有 Blender 类仍由 node_postprocess_draginteraction.py
# 注册）。登记进 `blueprint/__init__.py::_MODULE_REGISTRY` 后，注册器会对每个条目
# 调用 `mod.register()`，因此这里提供**空钩子**（幂等、无副作用）：
#   - 不注册类：避免与 node_postprocess_draginteraction 重复注册；
#   - 不在此处注册预览 handler：预览生命周期由 `_ensure_efmi_preview_running()`
#     / `_efmi_preview_cleanup()` 按需管理（节点 init / draw / 插件 unregister），
#     模块导入或加载即注册 handler 会在无 EFMI 节点时留下常驻回调。


def register():
    """空注册钩子：本模块无自带 bpy 类（见上方说明）。"""
    return None


def unregister():
    """空注销钩子；预览 handler 由 `_efmi_preview_cleanup()` 负责收尾。"""
    return None