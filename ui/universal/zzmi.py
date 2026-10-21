import json
import os

import bpy

from ...common.draw_call_model import DrawCallModel
from ...common.global_config import GlobalConfig
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.global_properties import GlobalProterties
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...common import safe_write
from ...utils.json_utils import JsonUtils
from ...utils.format_utils import Fatal
from ...utils.timer_utils import TimerUtils
from .unity import ExportUnity
from contextlib import nullcontext

# 「条件判断」后处理节点需要知道每个 ini 段属于哪个 draw_ib：导出期用
# ``register.draw_ib_context(draw_ib)`` 包住「构建该 draw_ib 的 ini 段」的代码，
# M_IniSection 构造时就会快照当前 draw_ib。导入失败时导出照常，只是不登记段。
try:
    from ...common import export_section_registry as _section_registry
except Exception:  # pragma: no cover - 轻量 fake 宿主 / 旧插件环境
    _section_registry = None


def _section_registry_context(draw_ib):
    """返回 draw_ib 的段登记上下文；登记表不可用时退化为空上下文（不改变导出行为）。"""
    if _section_registry is None:
        return nullcontext()
    return _section_registry.draw_ib_context(draw_ib)


# 导出模块在 Blender 启动时可以直接读取反查模块的版本；轻量 fake/旧插件环境
# 可能未加载该模块，使用同一当前版本常量仍保持“陈旧缓存拒绝”这一安全默认。
try:
    from ...common.zzmi_skeleton import (
        ZZMI_VG_MAP_ALGORITHM_VERSION,
        ZZMISkeletonMergeHelper as _ZZMISkeletonMergeHelper,
    )
except Exception:  # pragma: no cover - 仅兼容无完整 Blender 依赖的导入环境
    ZZMI_VG_MAP_ALGORITHM_VERSION = 5

    class _ZZMISkeletonMergeHelper:  # type: ignore[no-redef]
        """轻量 fake 宿主下的最小替身：与导入侧同一「可升级旧版本」口径。"""

        MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS = (4,)


# 「场景里是否真实存在这个对象」的统一判据（阻断修复 t80 §2.3）。
# 轻量 fake 宿主可能没加载 blueprint.export_helper：退化为只按 bpy 查名。
try:
    from ...blueprint.export_helper import BlueprintExportHelper as _BpHelper

    def _scene_object_present(*names):
        return _BpHelper.scene_object_present(*names)

except Exception:  # pragma: no cover - 轻量 fake 宿主
    def _scene_object_present(*names):
        # 轻量 fake 宿主 / 无完整 bpy：**不做"缺席"判定**，一律返回"存在" ⇒
        # `present` 的构建与旧行为逐字相同（宁可不插桩，也不要在拿不到真实场景
        # 信息时凭空判定部件缺席）。
        registry = getattr(bpy, "data", None)
        registry = getattr(registry, "objects", None)
        if registry is None or not hasattr(registry, "get"):
            return True
        try:
            for name in names:
                text = str(name or "").strip()
                if text and registry.get(text) is not None:
                    return True
        except Exception:
            return True
        return False

# t75：通道计划版本。导出侧与缓存门控共用同一常量（轻量 fake/旧插件环境
# 未加载 common.zzmi_channel 时用同一当前版本，保持「陈旧缓存拒绝」的安全默认）。
try:
    from ...common.zzmi_channel import ZZMI_CHANNEL_PLAN_VERSION
except Exception:  # pragma: no cover - 仅兼容无完整 Blender 依赖的导入环境
    ZZMI_CHANNEL_PLAN_VERSION = 1
# 共享骨图连通分量（池定容用）。轻量环境下退化为"每个部件自成一个分量"
# （池只会偏大，不会偏小——偏小才会同帧 FIFO 淘汰同帧键）。
try:
    from ...common.zzmi_channel import shared_bone_components as _shared_bone_components
except Exception:  # pragma: no cover
    def _shared_bone_components(components):
        return [[index] for index in range(len(components))]

# t75：判定「跨部件共享通道骨」的唯一实现（与导入期同一份代码）。轻量环境没有
# common.zzmi_channel 时保守判 False = 一律按「只供骨、不参与判定」处置——宁可
# 少发键块（老口径的出现次捕获仍在），也绝不把身份未知的部件放进门控。
try:
    from ...common.zzmi_channel import is_cross_part_channel as _is_cross_part_channel
except Exception:  # pragma: no cover
    def _is_cross_part_channel(record, min_parts: int = 2) -> bool:
        return False


def _zzmi_prop_flag(name: str, default: bool) -> bool:
    """Read an optional ZZMI boolean property without breaking lightweight hosts.

    Blender keeps newly added properties on the registered ``GlobalProterties``
    instance.  Tests and older running add-ons may expose only the older class,
    so a missing accessor must have the same value as the property's default.
    """
    getter = getattr(GlobalProterties, name, None)
    if not callable(getter):
        return bool(default)
    try:
        return bool(getter())
    except Exception:
        return bool(default)


class ZZMITextureMarkName:
    DiffuseMap = "DiffuseMap"
    NormalMap = "NormalMap"
    LightMap = "LightMap"
    MaterialMap = "MaterialMap"
    WengineFx = "WengineFx"
    WengineFX = "WengineFX"
    ZglowMap = "ZglowMap"


# 纯占位 target（子网格全成了占位小三角）的变体 pass 里，写入 SO 前缀的实写行数：
# target 自己的 deform draw 会被跳过，前缀只剩该 pass 里 carrier 的 3 顶点占位小三角。
# base_vertex / Redirect Texcoord pad / override_vertex_count 都按它取值，保证
# "前缀声明行数 == 实际写入行数"（历史事故：浮波柚叶01 base_vertex=6 而实写 3 行
# → 合并几何整体错位 3 个顶点，游戏内爆炸）。
ZZMI_STUB_PREFIX_ROWS = 3

# ---------------------------------------------------------------------------
# 合并骨架 v9：出现次槽位（occurrence slot）+ 每槽守卫
# ---------------------------------------------------------------------------
# 背景（用户游戏内实测通过的手改版 K:\...\浮波柚叶\浮波柚叶.ini 为语义基准）：
# v2 只保存**一份** palette / 骨架 / SO，并用 seen/phase 守卫「组内部件当帧全部
# 到达」。同一 IB 在场景中被画多次（多实例）时，两个实例的 deform pass 会交错
# 覆盖同一份 palette → 守卫即使成立也可能重放**半帧拼接**的骨架，表现为运动时
# 抖动 / 罕见反转帧混淆。v6 尝试「每次 deform 无条件重放」消除交错，结果更差：
# 第一个部件到达时另一个部件的数据还是上一帧的，那一笔重放本身是错的。
#
# v9 保留 v2 已验证的守卫，把单份资源扩成 **2 个槽位**：每个部件的 deform 段按
# 自己的出现次（occurrence）把当帧 palette / SO 写进 s1 或 s2，attach 也只写
# 该槽的合并骨架；守卫按槽判定「本组全部部件在该槽都已当帧到达」后才重放。
# 于是两个实例各占一槽、互不覆盖，任何一槽被消费时组内数据都是同一实例的。
#
# 三条硬约束（历次实测踩坑，改动时不得违反）：
# 1. `run = <CustomShader>` **绝不能在 if 体内**：本 3DMigoto fork 里 if 内的 run
#    不执行 → 骨架为空 → 模型整体消失。所有 attach run 必须在段顶层无条件执行
#    （每个 (部件, 槽) 一条）。
# 2. 出现在 if 条件里的 `$变量` 必须在**顶层**被赋值过：只在 if 体内赋值的变量会被
#    加载期优化器按初值静态折叠 → 整个守卫 if 被删除 → 重放不发生。因此到达标记
#    用 `$zz_ms_seen_<i><k> = $zz_ms_seen_<i><k> + ($zz_ms_occ_<i> == <k>)` 这种
#    顶层算术累加写法，**绝不在 if 体内赋值**。
# 3. [Present] 先把 `seen` 抄进 `$zz_ms_prev_<i><k>`（下一帧守卫的按槽预测值），
#    再清零 occ/seen；**不写任何资源复位**（`ResourceZZRedirectSO_* = null`
#    等 F8 构造经实测有害，会废掉 [Present] 清场）；也不生成 drawn/ready 之类闩锁变量。
#
# 已知限制：
# - 两个 pass 的实例提交顺序相反时，出现次槽位会配错（与 v6 同源）。
# - 某部件整帧被剔除时，按槽预测只覆盖「上一帧在该槽也没到」的情形：上一帧到过、
#   本帧才被剔除的部件会阻塞该槽守卫（保持上一帧内容），方向是安全的、且下一帧
#   预测值就失效（不再阻塞）；[Present] 的 occ/seen 清零只作跨帧兜底。
#
# v9.1（2026-09-13，FrameAnalysis 实证回归修复）：**直连路径不再等组内其它部件**。
# 旧口径把「本槽骨架齐全」与「本部件几何必须落盘」绑在同一个组级 seen 守卫上，
# 而该守卫只可能在组内**最后一个**到达的部件那段成立，且每个部件的几何只有它
# 自己的 deform 段能画（那一笔的 VB/SO 绑定只在该段有效）⇒ 先到的部件被
# `handling = skip` 吞掉且没有替代 draw，其 SO 当帧不写 → 模型随引擎的提交顺序
# 逐帧闪/消失（同一批 3 部件在两份 dump 里的 deform 顺序为 B→C→A 与 A→B→C，
# 后者最后到的是 3 顶点占位桩 → 整帧无可见几何 = 用户看到的"模型消失"帧）。
# 自足挂点（几何只采样自己 vg_map 覆盖的槽位）改为按本轮出现次绑本槽骨架后
# **无条件绘制**；只有画「含组内其它部件顶点的合并几何」的吸收挂点仍保留组级守卫。
ZZMI_MERGED_SKELETON_SLOTS: tuple[int, ...] = (1, 2)
# 出现次回绕上限：occ 自增到该值即回绕为槽位起点（1/2 循环）。
ZZMI_MERGED_SKELETON_OCC_WRAP = len(ZZMI_MERGED_SKELETON_SLOTS) + 1
# 计数器出现在 if 条件前必须能在顶层解析出的下限（槽位起点）。
ZZMI_MERGED_SKELETON_OCC_SLOT_BASE = ZZMI_MERGED_SKELETON_SLOTS[0]

# ---------------------------------------------------------------------------
# 到达标记的**消费点谓词**（主症①b「谓词非单调」修复，2026-09-17）
# ---------------------------------------------------------------------------
# `$zz_ms_seen_<i><k>` 是**帧内单调不减**的顶层 sticky 计数：
# `seen = seen + (occ == k)`，每帧末由 `[Present]` 清零（`occ` 回绕与 seen 累加
# 均**不得删除**，见 v9 三条硬约束）。因此「本部件本帧已在槽 k 出现过」的
# 正确写法是 `>= 1`，不是 `== 1`：
#   · n ≤ 2（= len(SLOTS)，设计上界）时 seen ∈ {0, 1}，两种写法**逐字节等价**；
#   · n ≥ 3 时第 3 笔的 occ 回绕回槽 1 ⇒ `seen_<i>1 = 2` ⇒ `== 1` 在本帧剩余
#     全部 draw 内**恒假** ⇒ SO-ready 与每槽守卫**集体关闭**（P-13）。后果不是
#     「少一次重放」，而是「帧内最后一次闭合退到守卫转假之前」——即帧末不再有
#     任何闭合点，SO 停在半帧拼接的骨架上。
# 改为 `>= 1` 后，守卫真值在本帧内**只可能由假转真**（见 `_merged_slot_seen_condition`
# 的单调性说明），于是「帧内最后一次闭合」必落在本帧最后一个必需部件的 pass 上
# （那一刻该槽 palettes 全部当帧刷新 ⇒ 该次落笔用的是完整骨架）。
ZZMI_MERGED_SEEN_PREDICATE = ">="

# ---------------------------------------------------------------------------
# 结构性缺口的**显式诊断**（不再静默降级）
# ---------------------------------------------------------------------------
# 生成器此前在三处**静默**退化（守卫/对齐块直接不发射，产物里没有任何痕迹）：
#   1. 组内 vg_map 交集为空 ⇒ 姿态指针对齐不可达（"有计划的组没有锚点"）；
#   2. 组有锚点但本轮没有重定向计划 ⇒ 对齐块被 `group_plan is not None` 门控挡住
#      （"有锚点的组没有计划"）；
#   3. 同帧同部件出现次数 > len(SLOTS) ⇒ occ 回绕把第 3 笔标成槽 1（设计上界外）。
# 三者都改成**机器可读**输出：ini 段里的注释行（`; ZZMI-MERGE-DIAG <code> ...`）
# + 一条 stdout 提示 + 进 `self._zzmi_merge_diagnostics` 供测试断言。
# 静态注释只**声明**上界，不能证明是否发生超界；运行时是否真的超界由
# `[CustomShaderZZMIMergedSkeletonAttach_*]` 的 `x3 = $zz_ms_seen_*` 探针透到
# IniParams（帧分析日志里该值 = 2 即「本帧出现次数超界」的显式失败标记）。
ZZMI_MERGE_DIAG_PREFIX = "; ZZMI-MERGE-DIAG"
ZZMI_MERGE_DIAG_SLOT_BOUND = "SLOT_BOUND"
ZZMI_MERGE_DIAG_POSE_UNAVAILABLE = "POSE_ALIGNMENT_UNAVAILABLE"
# ---------------------------------------------------------------------------
# F1 跨组骨骼合并（2026-10-10 用户实机复现件根因修复）
# ---------------------------------------------------------------------------
# 症状：`洛克茜` 复现件里 3 合 1 的那个物体整块消失，且「自身与同组件显示、
# 跨组件消失」。实测（`Meshes\4e6e989e-Blend.buf` 4562 行）：
#   · 宿主子网格 `4e6e989e-11700-0`（G2）与跨组件 `4e6d8882-1704-0`（G0）
#     合并进同一网格；跨组件贡献 **624 行**，骨 id {1,2}（= G0 槽位）；
#   · 生成的 ini 里，跨组件的 attach 只写它**自己组**的缓冲
#     （`cs-u0 = ref ResourceZZMergedSkeleton_G0_s1`），而绘制合并几何的
#     deform VS 绑的是**宿主组**缓冲（`vs-t0 = ..._G2_s1`）⇒ G2 的槽 1、2
#     没有任何人写过 ⇒ deform 读到未初始化矩阵 ⇒ 624 顶点塌陷 ⇒ 部件消失。
# 修复口径：跨组件的骨骼写进**吸收它的宿主组**缓冲（槽位 = 全局骨 id，
# 全宽缓冲天然不冲突），由宿主组的 deform 段顶层 `run` 调用。
# 开关：`cross_group_merged_vgmap_test`（关闭 = 恢复历史行为 + 大声报警）。
# ⚠ **已知未解（2026-10-10 实机报告，原因未定）**：角色展示页（大世界之外那条渲染
#   路径）里本方案的段命令 `draw` / `drawindexed` **不执行** —— 画面不推进、什么都
#   看不见（大世界一切正常；进程未死、资源级覆盖仍生效）。排查存档：本仓
#   `ZZMI骨骼合并计划书.md` ⑭ 与 §9。本开关默认关闭正是为此（关闭时逐字节保持历史行为）。
ZZMI_MERGE_DIAG_CROSS_GROUP_ATTACH = "CROSS_GROUP_ATTACH"
# ---------------------------------------------------------------------------
# O3（用户裁定 (a)：诊断先行、零行为变更）——「捕获 : 消费」记账
# ---------------------------------------------------------------------------
# 动机（t8 块口径结论，代次 `184431` / `log.txt` sha256 `fd6efcc7…a18471`）：
#   本帧 `999bff94` 的**捕获 : 重放 = 1 : 1**（部件口径）、组口径 **6 : 1 属设计行为**；
#   §52 的「1 次捕获 : 7 次重放」**不成立**（7 = IB 绑定/渲染绘制次数，是**消费端**，不是重放）。
# ⇒ 交付物降级为**可数证据**：每个「消费点」（绑定 `ResourceZZRedirectSO_G<g>_s<k>` 的重放块、
#   以及在守卫内发布合并几何的蒙皮 CS 块）旁落一行机器可读注释，并在导出期打一行结构比汇总。
# **硬不变量**：只加注释与 stdout，**不改**任何 `draw =` / `drawindexed` / `so0 = ref` / 守卫条件行
#   ⇒ IB 覆盖段与全部重放块**逐字节不变**（用 `dump_generated_ini.py` 的前后代次 A/B diff 证明）。
#   该取证工具**未入库**（`reports/`、`docs/` 都在 .gitignore 里），本机路径 =
#   `reports/zzmi-fix/evidence/dump_generated_ini.py`；引用它的说明只在本机可复现。
# **禁止**：为它新增任何 `$zz_ms_*` 变量、闩锁、if 体内 `$` 赋值（§61）。
ZZMI_MERGE_DIAG_REUSE_SITE = "REUSE_SITE"
ZZMI_MERGE_DIAG_REUSE_RATIO = "REUSE_RATIO"
# t75：通道骨哈希键 = 读**一根**骨的 48 字节矩阵（12 floats）。
# **逐字节精确**（`HashRegion`）：t73 实测同一实例、同一根物理骨在不同部件的
# palette 里逐位相同（Δ=0），而两个实例的差极小（G0 槽 0 = 1.4e-7…8.2e-6、
# G2 槽 185 = 1.32e-3），远小于任何量化格 ⇒ 旧的
# `SpatialHash(..., 0.01)` 会把两个实例并键（实测并键率最高 99/135）。
ZZMI_CHANNEL_HASH_BYTES = 48
# 键不可用（`HashRegion` 返回 -1/-2/-3）时写进产物的显式诊断码。
ZZMI_MERGE_DIAG_POSE_KEY_UNAVAILABLE = "POSE_KEY_HASH_UNAVAILABLE"

# ---------------------------------------------------------------------------
# t149：**开发者诊断注释默认不写进配置表**
# ---------------------------------------------------------------------------
# 下面这一族注释（`; ZZMI-MERGE-DIAG …`、`; channel_slot=… identity_basis=…`、
# `; --- 实例对齐：… ---`、以及 else 分支里的中文解释）只服务于生成器开发者与
# 回归测试：它们是"可数证据"，不是使用者需要的信息，却占了产物注释量的近一半。
# 因此默认**不发射**（连空行都不留）；需要复核时打开开关即可恢复**逐字节等价**
# 的旧输出：
#     · 环境变量 `ZZMI_MERGE_DIAG=1`（或 true/yes/on），在**导入前**设置；
#     · 或在测试里直接给本模块的 `ZZMI_MERGE_DIAG_EMIT` 赋 True（发射点按调用时
#       读模块全局，因此 monkeypatch 立即生效）。
# 注意：`_zzmi_merge_diagnostics` 记录表与 `⚠️ [ZZMI骨骼合并]` stdout 提示**不受**
# 本开关影响 —— 它们是开发/测试面，不是配置表内容，永远照发。
ZZMI_MERGE_DIAG_EMIT = str(os.environ.get("ZZMI_MERGE_DIAG", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# ---------------------------------------------------------------------------
# 蒙皮 CS 的**行布局单一事实源**（t40：动态逐元素识别，消除写死 magic number）
# ---------------------------------------------------------------------------
# 背景（B2 高危项 + 两处同类残余）：
#   `Toolset/zzmi_merged_skin.hlsl` 把 `cs-t0` 声明成 `StructuredBuffer<ZZVertex40>`
#   （40 字节/行）、`cs-t1` 声明成 `StructuredBuffer<ZZBlend32>`（32 字节/行）。
#   D3D11 的 SRV 元素步长由**结构体大小**决定，与底层 Buffer 的真实 stride 无关：
#   实际行布局窄于 / 宽于结构体时 CS 会跨行错读，且**不报任何错**。
#   旧实现只比"总宽度 == 32"，因此"总宽 32B 但元素顺序/语义不同"会被放行；
#   `src_rows` 的 40B 假设同样只隐含在写死的常量里。
#
# 本字典是唯一事实源：`hlsl` 段描述 HLSL 结构体字段（类型/字段名/字节宽/偏移），
# `elements` 段描述该行在**输入布局里的语义期望**（语义名/索引/格式/字节宽/偏移/
# `extract_slot`）。判据 = 实际布局元数据与 `elements` **逐元素**核对（不是只比宽度）；
# 一致性测试则把 `hlsl` 段与 `Toolset/zzmi_merged_skin.hlsl` 的真实结构体逐字段比对
# ⇒ 任一侧被改都会红。
#
# 对齐规则依据（**推导自真实数据，不是另写一套猜测**）：
#   · 管线元数据 `D3D11ElementList` 每个元素带 ByteWidth，类目 stride = 该类别 ByteWidth
#     之和（`common/d3d11_gametype.py:71-93`）；`AlignedByteOffset` 是**跨类别**的全局累加值
#     （同文件 :70-82），**不是**每类别缓冲内的偏移 —— 因此类别内偏移按声明顺序**紧凑累加**。
#   · 真实 SSMT 工作空间 json（只读复核 `K:\SSMT-Package-master\WorkSpace\ZZMI`，
#     按 `<部件>\TYPE_GPU_*\tmp.json` 统计）：`主角` 12 个 + `叶瞬光01` 20 个部件的
#     `Position` / `Blend` `ExtractSlot` **全为 vb0 / vb2**，无一例外。
#     （另一层 `LOD0\<部件>-*` 目录里同值 10 + 19 个，未计入上面的部件数。）据此：
#     Position = POSITION R32G32B32_FLOAT 12 +
#     NORMAL R32G32B32_FLOAT 12 + TANGENT R32G32B32A32_FLOAT 16 = 40B（偏移 0/12/24）；
#     Blend = BLENDWEIGHTS R32G32B32A32_FLOAT 16 + BLENDINDICES R32G32B32A32_UINT 16
#     = 32B（偏移 0/16）；窄布局实测 BW8_BI8 = 8+8、BI4 = 单 BLENDINDICES R32_UINT 4
#     （另有 BLENDINDICES 记成 SINT 的等价形态，见格式归并）。
#   · 3DMigoto 抓帧的 deduped 缓冲（文件名形如
#     `146c0784-vb0-layout=34c1b144-topology=trianglelist-stride=28-count=244-inst_count=2.txt`）
#     与 HLSL 结构体注释（"位置 = 0..2，法线 = 3..5，切线 = 6..9"）给出同一组偏移。
#     ⚠️ `layout=<签名>` 这个 token 本身**不含 slot**；文件名带 `vbN-` 前缀（本机复核：
#     184431 的 444 个、185933 的 216 个含 `layout=` 的 dedup 文件名**全部**带该前缀），
#     而该前缀与工作空间 `ExtractSlot` **同源**（都来自 ini / 导入声明）⇒ 可作佐证，
#     **不能**当独立证据；slot 溯源由 `elements[*]["extract_slot"]` 的逐元素比对把关
#     （与生成端 `cs-t0`→vb0 / `cs-t1`→vb2 的绑定源一致；实际值空 = 旧缓存不带 slot
#     溯源 ⇒ 保守放行）。
# 不满足判据时**不发 CS**、落具名诊断（指出具体哪个元素不匹配），并把发布退回给 draw 版重放。
# ⚠️ 该退回**不是无损的**（FR-2 如实化）：draw 版重放只能在「Blend 布局 == 锚点布局」的挂点落笔，
# 而 CS 发布块正是为「布局与锚点不一致、完全不能重放 draw」的必需部件准备的（见
# `_append_merged_skin_publish_block` 与计划书 §7 修复链 10）。因此锚点布局不支持 CS 时，
# 必需部件里那些「不在锚点布局内」的部件本帧**没有自己的发布点**；若其中某个排在最后到达，
# 该槽 SO 本帧可能不写 ⇒ 闪烁/缺失可能复现 —— 诊断文案必须如实说明这一点
# （覆盖面分档 complete / gap / legacy / unknown 的单一来源 = `_merged_skin_replay_coverage`）。
ZZMI_MERGED_SKIN_ROW_LAYOUT: dict = {
    "position": {
        # cs-t0：导出 Position 类目（渲染/蒙皮行 = 位置 + 法线 + 切线）
        "hlsl_struct": "ZZVertex40",
        "hlsl_srv": "src_rows",
        "hlsl_register": "t0",
        "hlsl_slot": "cs-t0",
        "hlsl_fields": (
            {"hlsl_type": "float4", "hlsl_name": "a", "byte_width": 16, "offset": 0},
            {"hlsl_type": "float4", "hlsl_name": "b", "byte_width": 16, "offset": 16},
            {"hlsl_type": "float2", "hlsl_name": "c", "byte_width": 8, "offset": 32},
        ),
        # CS 实际读取的表达式 + 它覆盖的字节区间（`p` / `n` / `t` 三个字段表达式）：
        # 这是"语义元素 ↔ HLSL 读取"的绑定依据，一致性测试会逐条比对
        "hlsl_reads": (
            {"expr": "v.a.xyz", "offset": 0, "byte_width": 12},
            {"expr": "float3(v.a.w, v.b.x, v.b.y)", "offset": 12, "byte_width": 12},
            {
                "expr": "float4(v.b.z, v.b.w, v.c.x, v.c.y)",
                "offset": 24,
                "byte_width": 16,
            },
        ),
        "elements": (
            {
                "semantic": "POSITION",
                "index": 0,
                "format": "R32G32B32_FLOAT",
                "byte_width": 12,
                "offset": 0,
                # 生成端把 `cs-t0` 绑到该部件的 vb0 资源（`cs-t0 = ref <vb0>`）⇒
                # 语义期望也带上 slot 溯源：工作空间复核的 `Position` `ExtractSlot`
                # 全为 "vb0"（口径见模块头「对齐规则依据」）。
                "extract_slot": "vb0",
            },
            {
                "semantic": "NORMAL",
                "index": 0,
                "format": "R32G32B32_FLOAT",
                "byte_width": 12,
                "offset": 12,
                "extract_slot": "vb0",
            },
            {
                "semantic": "TANGENT",
                "index": 0,
                "format": "R32G32B32A32_FLOAT",
                "byte_width": 16,
                "offset": 24,
                "extract_slot": "vb0",
            },
        ),
    },
    "blend": {
        # cs-t1：导出 Blend 类目（权重 + 全局槽位索引）
        "hlsl_struct": "ZZBlend32",
        "hlsl_srv": "src_blend",
        "hlsl_register": "t1",
        "hlsl_slot": "cs-t1",
        "hlsl_fields": (
            {"hlsl_type": "float4", "hlsl_name": "w", "byte_width": 16, "offset": 0},
            {"hlsl_type": "uint4", "hlsl_name": "i", "byte_width": 16, "offset": 16},
        ),
        "hlsl_reads": (
            {"expr": "blend.w[k]", "offset": 0, "byte_width": 16},
            {"expr": "blend.i[k]", "offset": 16, "byte_width": 16},
        ),
        "elements": (
            {
                "semantic": "BLENDWEIGHTS",
                "index": 0,
                "format": "R32G32B32A32_FLOAT",
                "byte_width": 16,
                "offset": 0,
                # 生成端把 `cs-t1` 绑到该部件的 vb2 资源（`cs-t1 = ref <vb2>`）⇒
                # 工作空间复核的 `Blend` `ExtractSlot` 全为 "vb2"（同上）。
                "extract_slot": "vb2",
            },
            {
                "semantic": "BLENDINDICES",
                "index": 0,
                "format": "R32G32B32A32_UINT",
                "byte_width": 16,
                "offset": 16,
                "extract_slot": "vb2",
            },
        ),
    },
}


def _zzmi_skin_row_bytes(kind: str) -> int:
    """CS 行字节数（**派生量**：由单一事实源的语义元素宽度求和，不再写死）。"""
    return sum(
        int(element["byte_width"])
        for element in ZZMI_MERGED_SKIN_ROW_LAYOUT[kind]["elements"]
    )


ZZMI_MERGED_SKIN_BLEND_ROW_BYTES = _zzmi_skin_row_bytes("blend")  # = 32（派生）
ZZMI_MERGED_SKIN_POSITION_ROW_BYTES = _zzmi_skin_row_bytes("position")  # = 40（派生）
ZZMI_MERGE_DIAG_SKIN_LAYOUT_UNSUPPORTED = "SKIN_LAYOUT_UNSUPPORTED"


class ExportZZMI(ExportUnity):
    MERGED_SKELETON_ATTACH_THREADS = 64

    CROSS_IB_METHOD_VB_COPY = "VB_COPY"
    CROSS_IB_METHOD_VB_COPY_CB1 = "VB_COPY_CB1"
    CROSS_IB_METHOD_VB_REF_SO0 = "VB_REF_SO0"
    CROSS_IB_METHOD_VB_COPY_NORMAL = "VB_COPY_NORMAL"

    SUPPORTED_CROSS_IB_METHODS = {
        CROSS_IB_METHOD_VB_COPY,
        CROSS_IB_METHOD_VB_COPY_CB1,
        CROSS_IB_METHOD_VB_REF_SO0,
        CROSS_IB_METHOD_VB_COPY_NORMAL,
    }

    @staticmethod
    def _atomic_write_binary(path: str, payload: bytes) -> None:
        """同目录临时文件完整落盘后原子替换，失败时保留旧产物。

        内容与现有文件相同时**直接返回**（见 ``common.safe_write``）：`os.replace`
        必然换掉 mtime，而 3DMigoto 的自定义着色器编译缓存正是按 `.hlsl` 的 mtime
        配对，无谓的 mtime 变更会让缓存失效、下次进游戏整族重编译。
        """
        safe_write.write_bytes_if_changed_atomic(path, payload)

    SLOT_FIX_RESOURCE_NAME_DICT = {
        ZZMITextureMarkName.DiffuseMap: r"Resource\ZZMI\Diffuse",
        ZZMITextureMarkName.NormalMap: r"Resource\ZZMI\NormalMap",
        ZZMITextureMarkName.LightMap: r"Resource\ZZMI\LightMap",
        ZZMITextureMarkName.MaterialMap: r"Resource\ZZMI\MaterialMap",
        ZZMITextureMarkName.WengineFx: r"Resource\ZZMI\WengineFx",
        ZZMITextureMarkName.WengineFX: r"Resource\ZZMI\WengineFx",
        ZZMITextureMarkName.ZglowMap: r"Resource\ZZMI\GlowMap",
    }

    def __init__(self, blueprint_model):
        # ZZMI 骨骼合并（分支选项）：复选框开启时，为「DrawIB 内存在但蓝图里没有对象」
        # 的部件自动创建极限小三角面占位对象（必须在 super().__init__ 组装模型之前注入）
        self.blueprint_model = blueprint_model
        self._zzmi_stub_object_names = []
        self._zzmi_stub_draw_calls = []
        # 合并物体身份账本。Blender 中一个合并物体的显示名称通常会带新的
        # 顶点/索引数量（例如 ``8c8de427-24180-0``），而工作区身份仍然是
        # 目标 IB 的原始前缀（``8c8de427-798-0``）。如果在构造
        # SubMeshModel 之前不把这两个身份对齐，导出器会把目标物体判成缺席，
        # 注入 3 顶点占位，最终把完整合并网格隐藏掉。
        self._zzmi_merged_identity_records = []
        try:
            self._normalize_merged_object_drawcalls(blueprint_model)
            if GlobalProterties.import_merged_vgmap():
                # 占位是合并骨架渲染身份完整性的硬前提。创建失败时中止导出，
                # 不能回退到旧的 ib=null/IB skip 路径让部件静默消失并串扰其它 hash。
                self._zzmi_stub_object_names = self._ensure_stub_objects_for_missing_parts(blueprint_model)

            super().__init__(blueprint_model)

            self.cross_ib_info_dict = blueprint_model.cross_ib_info_dict
            self.cross_ib_method_dict = blueprint_model.cross_ib_method_dict
            self.cross_ib_mapping_method = getattr(blueprint_model, "cross_ib_mapping_method", {})
            self.has_cross_ib = blueprint_model.has_cross_ib
            self.cross_ib_object_names = blueprint_model.cross_ib_object_names

            self.shader_replace_info_list = getattr(blueprint_model, "shader_replace_info_list", [])
            self.shader_replace_object_names = getattr(blueprint_model, "shader_replace_object_names", set())
            self.shader_replace_object_info_map = getattr(blueprint_model, "shader_replace_object_info_map", {})
            self.has_shader_replace = getattr(blueprint_model, "has_shader_replace", False)

            # ZZMI 骨骼合并（分支选项）：export() 时按复选框 + 反查数据收集组件信息
            self.merged_skeleton_components = []
            self.merged_skeleton_component_id_dict = {}
            self.has_merged_skeleton = False
            # B1：契约判定输入快照（由 _collect_merged_skeleton_components 填，
            # _enforce_merged_skeleton_contract 读）——开关关闭时也要统计
            # parts_with_data，否则「开关关闭 + 有数据」这条 error 永远不触发。
            # 初值 None = 尚未扫描（_enforce_* 会自行补扫一次）。
            self._zzmi_merged_contract_stats = None
            # 合并网格自动重定向计划（_build_merged_mesh_redirect_plan 产出，INI 生成时查询）
            self._redirect_carrier_map: dict = {}
            self._redirect_target_map: dict = {}
            # 重放用 Blend 重打包产物（carrier_ib -> (资源名, stride, 文件名, 二进制)）：
            # 载体与组内锚点布局不一致时，把载体的权重/索引按锚点布局加宽重打包，
            # 使任意锚点挂点都能在同一 IA 布局下重放合并几何。
            self._redirect_blend_retargets: dict = {}
            # A-opt1：旧有的 self._unredirected 字段全仓无读取方（_export_impl 用的是
            # 局部 unredirected / 返回值），已删除；v9 直连回退判据见
            # _build_merged_mesh_redirect_plan 的返回值。

            print(f"[CrossIB ZZMI] 初始化: has_cross_ib={self.has_cross_ib}")
            print(f"[CrossIB ZZMI] cross_ib_info_dict={self._format_cross_ib_info_dict(self.cross_ib_info_dict)}")
            print(f"[CrossIB ZZMI] cross_ib_object_names={self._format_name_set(self.cross_ib_object_names)}")
        except Exception:
            # 构造失败时 export() 的 finally 尚未接管；对象、mesh 与注入蓝图的
            # DrawCall 必须作为一个事务一起回滚，否则下一次导出会引用已删除对象。
            self._cleanup_stub_objects()
            raise

    # ------------------------------------------------------------------
    # 合并物体身份（Blender 侧一个对象 -> 一个目标 IB 前缀）
    # ------------------------------------------------------------------

    @staticmethod
    def _zzmi_bare_workspace_unique(value: str) -> str:
        """返回工作区唯一标识的裸部分（去掉 ``LOD<n>.``）。"""
        text = str(value or "").strip()
        if not text:
            return ""
        if "." in text:
            head, tail = text.split(".", 1)
            if head.upper().startswith("LOD") and head[3:].isdigit():
                return tail.strip()
        return text

    @classmethod
    def _zzmi_name_variants(cls, value: str) -> set[str]:
        """构造合并源对象名称的稳定匹配变体。

        合并工具会保留 ``.ZZMI_SOURCE`` 备份，前处理又会追加 ``_copy``；
        这些后缀不应改变「这是同一个逻辑源部件」的判定。只剥离已知运行时
        后缀，不做模糊的前缀匹配，避免把同 IB 的其它独立部件误吞进来。
        """
        raw = str(value or "").strip()
        if not raw:
            return set()
        variants = {raw}
        queue = [raw]
        suffixes = (
            ".ZZMI_SOURCE",
            "_copy",
            "_copy_temp",
            "_temp",
        )
        while queue:
            current = queue.pop()
            for suffix in suffixes:
                if not current.endswith(suffix):
                    continue
                stripped = current[: -len(suffix)]
                if stripped and stripped not in variants:
                    variants.add(stripped)
                    queue.append(stripped)
        # 工作区前缀本身也是一个可匹配身份（不含 Blender 运行时后缀）。
        bare = cls._zzmi_bare_workspace_unique(raw)
        if bare:
            variants.add(bare)
        return variants

    @classmethod
    def _zzmi_decode_merge_sources(cls, obj) -> list[dict]:
        """读取 Blender 合并工具写入的 ``ZZMI_MergeSources`` 账本。

        旧工程可能保存 Python list，新工程保存 JSON 字符串；两种格式都接受，
        非法/不完整条目被忽略，绝不让身份修复阻断普通导出。
        """
        if obj is None:
            return []
        try:
            raw = obj.get("ZZMI_MergeSources")
        except Exception:
            raw = None
        if not raw:
            return []
        payload = raw
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return []
        if not isinstance(payload, (list, tuple)):
            return []
        result = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            source_name = str(item.get("name", "") or "").strip()
            workspace_unique = str(item.get("workspace_unique_str", "") or "").strip()
            if not source_name and not workspace_unique:
                continue
            result.append(
                {
                    "name": source_name,
                    "workspace_unique_str": workspace_unique,
                    "bare_workspace_unique_str": cls._zzmi_bare_workspace_unique(
                        workspace_unique
                    ),
                }
            )
        return result

    @classmethod
    def _zzmi_collect_merged_identity_records(cls) -> list[dict]:
        """扫描当前 Blender 数据库，建立「源身份 -> 合并对象」账本。"""
        records = []
        try:
            objects = list(bpy.data.objects)
        except Exception:
            objects = []
        for obj in objects:
            try:
                if getattr(obj, "type", "MESH") != "MESH":
                    continue
                sources = cls._zzmi_decode_merge_sources(obj)
            except Exception:
                continue
            if not sources:
                continue

            target_workspace = str(
                obj.get("3DMigoto:WorkspaceUniqueStr", "") or ""
            ).strip()
            # 合并工具通常已经把目标 IB 的原始前缀写回 WorkspaceUniqueStr。
            # 若旧文件没有该属性，优先从源清单中找与对象名称同 IB 的记录。
            object_name = str(getattr(obj, "name", "") or "")
            object_draw_ib = object_name.split(".", 1)[-1].split("-", 1)[0]
            if not target_workspace:
                for source in sources:
                    candidate = source.get("workspace_unique_str", "")
                    candidate_draw_ib = cls._zzmi_bare_workspace_unique(candidate).split(
                        "-", 1
                    )[0]
                    if candidate_draw_ib == object_draw_ib:
                        target_workspace = candidate
                        break
            if not target_workspace and sources:
                target_workspace = sources[0].get("workspace_unique_str", "")

            source_names = set()
            source_workspace_names = set()
            for source in sources:
                source_names.update(cls._zzmi_name_variants(source.get("name", "")))
                source_workspace = source.get("workspace_unique_str", "")
                source_workspace_names.update(
                    cls._zzmi_name_variants(source_workspace)
                )
                bare_workspace = source.get("bare_workspace_unique_str", "")
                if bare_workspace:
                    source_workspace_names.add(bare_workspace)

            records.append(
                {
                    "object": obj,
                    "object_name": object_name,
                    "sources": sources,
                    "target_workspace_unique_str": target_workspace,
                    "target_bare_workspace_unique_str": cls._zzmi_bare_workspace_unique(
                        target_workspace
                    ),
                    "source_names": source_names,
                    "source_workspace_names": source_workspace_names,
                    "active": False,
                }
            )
        return records

    @classmethod
    def _zzmi_record_for_object(cls, obj, records: list[dict]) -> dict | None:
        if obj is None:
            return None
        try:
            obj_name = str(obj.name or "")
        except Exception:
            return None
        for record in records:
            if obj is record.get("object"):
                return record
            if obj_name == record.get("object_name"):
                return record
        return None

    @classmethod
    def _zzmi_records_for_name(cls, name: str, records: list[dict]) -> list[dict]:
        """返回名称命中的全部合并记录（原对象与前处理 `_copy` 可能并存）。"""
        variants = cls._zzmi_name_variants(name)
        if not variants:
            return []
        matched = []
        for record in records:
            if (
                variants & set(record.get("source_names", ()))
                or variants & set(record.get("source_workspace_names", ()))
            ):
                matched.append(record)
        return matched

    @classmethod
    def _zzmi_record_for_drawcall_target(
        cls, draw_call, records: list[dict]
    ) -> dict | None:
        """按 DrawCall 的实际对象或目标前缀查找合并记录。

        前处理复制/蓝图恢复期间对象名可能短暂不一致；目标前缀（记录中的
        ``WorkspaceUniqueStr``）是稳定键。源备份前缀只用于过滤，不会把普通
        源 DrawCall 误当成目标 DrawCall。
        """
        obj = cls._zzmi_drawcall_object(draw_call)
        record = cls._zzmi_record_for_object(obj, records)
        if record is not None:
            return record
        # 能解析到一个真实但非合并源对象时，这就是源备份 DrawCall；不要再
        # 仅凭其逻辑前缀把它升级成目标，否则目标缺失时会把整条导出链过滤掉。
        if obj is not None:
            return None
        candidates = [
            getattr(draw_call, "obj_name", "") or "",
            getattr(draw_call, "source_obj_name", "") or "",
        ]
        for candidate in candidates:
            candidate_variants = cls._zzmi_name_variants(candidate)
            if not candidate_variants:
                continue
            for item in records:
                target_variants = set()
                target_variants.update(
                    cls._zzmi_name_variants(
                        item.get("target_workspace_unique_str", "")
                    )
                )
                target_variants.update(
                    cls._zzmi_name_variants(item.get("object_name", ""))
                )
                if candidate_variants & target_variants:
                    return item
        return None

    @staticmethod
    def _zzmi_drawcall_object(draw_call):
        """解析 DrawCall 当前实际引用的 Blender 对象。"""
        candidates = []
        try:
            candidates.append(draw_call.get_blender_obj_name())
        except Exception:
            pass
        candidates.extend(
            [
                getattr(draw_call, "source_obj_name", "") or "",
                getattr(draw_call, "obj_name", "") or "",
            ]
        )
        for candidate in candidates:
            if not candidate:
                continue
            try:
                obj = bpy.data.objects.get(str(candidate))
            except Exception:
                obj = None
            if obj is not None:
                return obj
        return None

    @classmethod
    def _zzmi_rebind_drawcall_to_merged_object(
        cls, draw_call, record: dict
    ) -> None:
        """把 DrawCall 的逻辑前缀改为目标 IB，同时保留合并对象作为数据源。"""
        target_prefix = str(record.get("target_workspace_unique_str", "") or "").strip()
        merged_obj = record.get("object")
        if not target_prefix or merged_obj is None:
            return
        try:
            draw_call.obj_name = target_prefix
            draw_call.source_obj_name = str(merged_obj.name or "")
            # DrawCallModel 的 match_* 字段在 __post_init__ 中由 obj_name 派生；
            # 重新初始化只刷新身份字段，不会丢失 work_key/shader replace 数据。
            post_init = getattr(draw_call, "__post_init__", None)
            if callable(post_init):
                post_init()
        except Exception:
            # 测试桩/第三方 DrawCall 可能不是 dataclass；至少保留两个名称字段，
            # 后续 get_blender_obj_name 仍会指向合并对象。
            try:
                draw_call.obj_name = target_prefix
                draw_call.source_obj_name = str(merged_obj.name or "")
            except Exception:
                pass

    def _normalize_merged_object_drawcalls(self, blueprint_model) -> None:
        """在占位注入和 SubMeshModel 构造前收拢合并对象身份。

        合并工具保留源对象作为备份，部分蓝图也会同时列出这些备份对象。若让
        它们继续作为独立 DrawCall，源对象会抢先成为同一 IB 的数据源，目标合并
        对象反而被 `_ensure_stub_objects_for_missing_parts` 判成缺席。这里仅在
        **目标合并对象确实出现在当前蓝图**时收拢：目标 DrawCall 保留一条并改
        用目标 IB 原始前缀，源备份 DrawCall 从本轮列表移除。未选中的合并对象
        不影响普通导出。
        """
        self._zzmi_merged_identity_records = self._zzmi_collect_merged_identity_records()
        ordered = getattr(blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is None or not self._zzmi_merged_identity_records:
            return

        active_records = {}
        for draw_call in list(ordered):
            record = self._zzmi_record_for_drawcall_target(
                draw_call, self._zzmi_merged_identity_records
            )
            if record is None:
                continue
            key = id(record)
            active_records[key] = record

        if not active_records:
            return

        kept = []
        kept_record_ids = set()
        for draw_call in list(ordered):
            obj = self._zzmi_drawcall_object(draw_call)
            record = self._zzmi_record_for_drawcall_target(
                draw_call, self._zzmi_merged_identity_records
            )
            if record is not None:
                key = id(record)
                # 一个合并对象在蓝图中重复列出时只保留第一条目标 DrawCall，
                # 否则同一完整网格会被重复写入同一 IB。
                if key in kept_record_ids:
                    continue
                kept_record_ids.add(key)
                self._zzmi_rebind_drawcall_to_merged_object(draw_call, record)
                record["active"] = True
                kept.append(draw_call)
                continue

            # 源备份对象若属于一个已激活的合并账本，不再单独导出；其几何已经
            # 在目标合并对象中。其它普通对象/其它合并对象保持原样。
            source_records = []
            for candidate in (
                getattr(draw_call, "source_obj_name", "") or "",
                getattr(draw_call, "obj_name", "") or "",
            ):
                source_records.extend(
                    self._zzmi_records_for_name(
                        candidate, self._zzmi_merged_identity_records
                    )
                )
                if source_records:
                    break
            # 目标 DrawCall 可能排在源备份之后；使用第一阶段收集到的全量
            # active 集合，而不是依赖当前遍历顺序。
            if any(id(record) in active_records for record in source_records):
                continue
            kept.append(draw_call)

        ordered[:] = kept

    # ------------------------------------------------------------------
    # 占位小三角面（合并骨架模式：部件无对象时不再输出 ib=null）
    # ------------------------------------------------------------------

    @staticmethod
    def _draw_call_object_name(draw_call) -> str:
        try:
            return str(draw_call.get_blender_obj_name() or "")
        except Exception:
            return str(getattr(draw_call, "obj_name", "") or "")

    @staticmethod
    def _remove_stub_object_data(obj):
        mesh = getattr(obj, "data", None)
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and getattr(mesh, "users", 0) == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except (AttributeError, RuntimeError):
                pass

    def _purge_stale_stub_state(self, ordered):
        """清理上次异常残留的 stub 对象、mesh 与带标记 DrawCall。"""
        stale_names = set()
        for obj in list(bpy.data.objects):
            if not obj.get("ZZMI_STUB"):
                continue
            stale_names.add(str(obj.name))
            self._remove_stub_object_data(obj)

        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if not getattr(draw_call, "zzmi_stub", False)
                and self._draw_call_object_name(draw_call) not in stale_names
            ]

    def _ensure_stub_objects_for_missing_parts(self, blueprint_model) -> list[str]:
        """为「需要生成但没有对象」的部件创建极限小三角面占位对象。

        合并骨架模式下用户可自由 join/删改。占位规则（用户拍板）：
        - **部分缺失的 DrawIB**：缺失组件直接补占位（其几何显然被同 DrawIB 的
          幸存对象接管）；
        - **整个 DrawIB 缺席**：以**局部顶点组归属**为准判定「它的几何是不是被
          并进了现存对象」——缺席部件**自属声明段**内（VGMap ∪ [VGOffset,
          VGOffset + VGCount)）的全局骨骼 id 必须被「**现存对象自己没声明过**的
          引用」命中才算证据（即该引用无法用引用方自己的局部顶点组解释）→
          全组件补占位（游戏内不可见的小三角，抑制原版 draw 防止重影）；否则 =
          用户压根不想生成 → 保持原样不插桩（该 DrawIB 不进入 mod，游戏内显示
          原版）。
          **去重借位值不算证据**（2026-09-16 叶瞬光01 脸部被误插占位事故：借位
          canonical 槽位被载体对象引用被误读成「几何被吸收」）；**引用方自己声明
          的槽位也不算证据**（2026-10-14 合并组误判：A 的顶点组 {1,2,3,4,5}、
          B 的 {5,6,7,8}，5 是去重时合并的「合并组」且 canonical 落在 B 的自属
          声明段；只导出 A 时 A 用的是自己的局部骨骼 5，旧口径却读成「B 被并进
          A」→ 给根本没被 join 的 B 补了占位）。详见 `_is_drawib_absorbed` /
          `_collect_foreign_used_group_ids`。
        无反查数据（json 无 VGMap）的缺席 DrawIB 一律不插桩。
        - **dedup_excluded 正交**（VGMapDedupExcluded=True）：该部件即使被引用
          也不生成占位——显式排除优先于 absorbed 判定（对齐 EFMI，用户意图
          「完全不出现在 mod 里」，游戏保留原版绘制）。
        返回创建的对象名列表（export() 结束后清理）。
        """
        workspace_root = GlobalConfig.path_workspace_folder()
        component_map_path = os.path.join(workspace_root, "LOD0", "DrawIB-Component.json")
        if not os.path.isfile(component_map_path):
            return []
        component_map = JsonUtils.LoadFromFile(component_map_path)
        if not isinstance(component_map, dict) or not component_map:
            return []

        ordered = getattr(blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is None:
            return []

        # 自愈必须早于 present 集合构建；否则残留 DrawCall 会被误判为真实部件，
        # 随后对象又被删除，SubMeshModel 构建必然引用一个不存在的对象。
        self._purge_stale_stub_state(ordered)

        present = set()
        scene_present = set()
        for draw_call in ordered:
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                continue
            if unique_str:
                present.add(self._zzmi_bare_workspace_unique(unique_str))
            # 阻断修复（t80 §2.3 实测）：`ordered` 里的名字来自**蓝图声明**，
            # 声明了但场景里没有对象的部件（实测 19 件声明 / 4 个 mesh）**不能**被
            # 当成 present——否则占位注入整段被跳过，紧接着 SubMeshModel 会因为
            # 找不到 Blender 对象直接 Fatal（"部件无对象 → 极限小三角占位"这条
            # 受支持的路径变成不可达）。
            #
            # 判据用 **DrawCall 自己解析出的场景对象名**（`get_blender_obj_name()`：
            # source_obj_name 优先，回退 obj_name——前处理把对象改名为副本后这里
            # 拿到的就是副本名），而不是工作区身份字符串；两者在有合并/副本的工程里
            # 本来就不相同。
            if not self._zzmi_drawcall_object_present(draw_call):
                continue
            if unique_str:
                scene_present.add(self._zzmi_bare_workspace_unique(unique_str))

        # 兼容兜底：蓝图里**一个** DrawCall 都解析不到场景对象时（轻量测试宿主 /
        # 拿不到真实 bpy 数据），退回按声明名判定 —— 宁可不插桩，也不要在信息不足
        # 时凭空给全部部件插桩。只要能解析出至少一个真实对象，就按场景真相判定。
        if not scene_present:
            scene_present = set(present)

        # 只把活跃合并对象的**目标**身份加入 present。源身份必须继续被视为
        # 缺席：对应的 3 顶点占位会触发这些原始 deform pass 的 palette attach，
        # 这样合并对象的跨组件权重才能在全局骨架中得到矩阵。把所有源身份都
        # 标成 present 会跳过占位、导致源 palette 永远不进入 MergedSkeleton。
        for record in self._zzmi_merged_identity_records:
            if not record.get("active"):
                continue
            target_workspace = str(
                record.get("target_workspace_unique_str", "") or ""
            ).strip()
            if target_workspace:
                present.add(self._zzmi_bare_workspace_unique(target_workspace))

        # 惰性计算：首个「整个 DrawIB 缺席」需要判定时才算。
        # 只收「引用方自己没声明过」的槽位（局部顶点组归属口径）。
        foreign_used_group_ids = None

        # 阻断修复（t81 阻断④）：本次决策为「按用户意图不生成」的部件身份集合。
        # 光 `continue` 只表示**不建占位**，DrawCall 仍留在
        # `ordered_draw_obj_data_model_list` 里 ⇒ 下游 `SubMeshModel` 会去
        # `bpy.data.objects` 找那个**不存在**的对象并 `Fatal`（实测
        # `LOD0.611df76d-132-0`）。手改参考版里这些部件 total=0 ⇒ 决策本身是对的，
        # 缺的只是「决策 → 模型列表」这一步传播。
        skipped_identities: set[str] = set()

        created = []
        for draw_ib, comp_dict in component_map.items():
            members = sorted(str(v) for v in (comp_dict or {}).values())
            if not members:
                continue

            if any(member in scene_present for member in members):
                # 部分缺失：缺失组件补占位（判据只看**场景里真实存在的对象**）
                stub_members = [
                    member for member in members if member not in scene_present
                ]
            else:
                # 整个 DrawIB 在场景里缺席：判定几何是否被合并进其它对象
                # （`any(member in present ...)` 这条"有声明身份"的旧分支已并入：
                #  两种情况下都只能靠几何吸收证据决定补不补占位）。
                if foreign_used_group_ids is None:
                    foreign_used_group_ids = self._collect_foreign_used_group_ids(
                        ordered
                    )
                if self._is_drawib_absorbed(
                    draw_ib, workspace_root, foreign_used_group_ids
                ):
                    stub_members = members
                    print(
                        f"[ZZMI骨骼合并] DrawIB {draw_ib} 没有对象，但其本部件专属骨骼槽位"
                        f"被其它模型引用（几何已被合并），全组件补占位小三角面"
                    )
                else:
                    print(
                        f"[ZZMI骨骼合并] DrawIB {draw_ib} 无对象且本部件专属骨骼槽位"
                        "未被其它模型引用，按用户意图不生成"
                    )
                    skipped_identities.update(members)
                    continue

            for member in stub_members:
                if self._is_component_dedup_excluded(member):
                    print(
                        f"[ZZMI骨骼合并] 部件 {member} 已标记 VGMapDedupExcluded，"
                        "按用户意图不生成占位（游戏保留原版绘制）"
                    )
                    skipped_identities.add(member)
                    continue
                obj_name = self._create_stub_object(member)
                if obj_name:
                    # 必须在任何后续构造步骤之前登记到实例；否则批量创建中途
                    # 失败时 helper 尚未返回，__init__ 的异常回滚拿不到先前对象。
                    self._zzmi_stub_object_names.append(obj_name)
                    # 该 DrawCall 可能在 SubMeshModel 完成前就被用于生成 IB override。
                    # 显式填入占位几何的导出计数，避免默认的 0 让占位段退化成
                    # drawindexed = 0；SubMeshModel 后续仍会用真实 mesh 再校准一次。
                    stub_draw_call = DrawCallModel(obj_name=obj_name)
                    stub_draw_call.vertex_count = 3
                    stub_draw_call.index_count = 3
                    stub_draw_call.index_offset = 0
                    stub_draw_call.zzmi_stub = True
                    ordered.append(stub_draw_call)
                    self._zzmi_stub_draw_calls.append(stub_draw_call)
                    created.append(obj_name)
                    print(
                        f"[ZZMI骨骼合并] 部件 {member} 没有对应对象，"
                        f"已创建极限小三角面占位（游戏内不可见）"
                    )

        # 阻断修复（t81 阻断④）：把「判定不生成」的部件从模型列表**原地摘除**。
        # 范式与 `_purge_stale_stub_state`（`ordered[:] = [...]`）一致。
        # 只用**正向判定为缺席**（`_zzmi_drawcall_object_present` 为假）的条目做删除
        # ——拿不到真实场景时该谓词一律为真，因此这里不会误删真实对象。
        if skipped_identities:
            kept = []
            removed = []
            for draw_call in ordered:
                identity = self._zzmi_bare_workspace_unique(
                    self._draw_call_object_name(draw_call)
                )
                if identity in skipped_identities and not self._zzmi_drawcall_object_present(
                    draw_call
                ):
                    removed.append(identity)
                    continue
                kept.append(draw_call)
            if removed:
                ordered[:] = kept
                shown = "、".join(sorted(set(removed))[:5])
                suffix = "…" if len(set(removed)) > 5 else ""
                print(
                    f"[ZZMI骨骼合并] 已从模型列表摘除 {len(removed)} 个"
                    f"「按用户意图不生成」的部件（否则下游会按不存在的对象构造）："
                    f"{shown}{suffix}"
                )
        return created

    def _load_drawib_vg_slots(
        self, draw_ib: str, workspace_root: str
    ) -> tuple[set[int], set[int]]:
        """读取 DrawIB 全部组件写回的 VGMap 值域，并区分「本部件自属槽位」。

        返回 ``(all_slots, own_segment_slots)``：

        - ``all_slots``：全部 VGMap 值。合并骨架的跨部件去重会把「同一骨骼矩阵
          （bitwise 相同）」跨部件合并成**一个 canonical 全局槽位**（canonical
          按 weighted_vertex_count 选主，见 `common/zzmi_skeleton.py`），因此
          本部件 VGMap 里的值**允许借位**落在别的部件声明段内——这是合法且常见
          的「共享骨骼」，不代表几何归属变化。
        - ``own_segment_slots``：落在本组件自属声明段
          ``[VGOffset, VGOffset + VGCount)`` 内的那些 VGMap 值 = **本部件专属
          骨骼的运行时身份**。EFMI 的写盘域契约与此同义：BLENDINDICES 一律写
          运行时身份（``VGOffset + local``，自属段内），由
          ``EFMIBoneMapBuilder.build_per_mesh_identity_map`` 把「canonical 属
          别的组件」的槽位改回本组件成员身份，再由
          ``EFMIBoneMapBuilder.validate_export_indices_in_segment`` 兜底断言
          ——「越出自属段的池共享（canonical 借位）是设计语义」（见
          `common/efmi_skeleton.py` 第 1283-1285 行）。ZZMI 导出侧不做这层身份
          改写（顶点组名 = canonical 槽位，直接写盘），所以**只有自属段内的
          值**才具备 EFMI 写盘域那层含义（= 几何归属证据）。
        """
        all_slots: set[int] = set()
        own_segment_slots: set[int] = set()
        lod0_dir = os.path.join(workspace_root, "LOD0")
        if not os.path.isdir(lod0_dir):
            return all_slots, own_segment_slots
        for name in os.listdir(lod0_dir):
            if not name.startswith(draw_ib + "-"):
                continue
            submesh_dir = os.path.join(lod0_dir, name)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in os.listdir(submesh_dir):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, name + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                vg_map = payload.get("VGMap") or {}
                try:
                    segment_start = int(payload.get("VGOffset", 0) or 0)
                    segment_count = int(payload.get("VGCount", 0) or 0)
                except (TypeError, ValueError):
                    segment_start, segment_count = 0, 0
                segment_end = segment_start + segment_count
                for v in vg_map.values():
                    try:
                        slot = int(v)
                    except (TypeError, ValueError):
                        continue
                    all_slots.add(slot)
                    if segment_count > 0 and segment_start <= slot < segment_end:
                        own_segment_slots.add(slot)
        return all_slots, own_segment_slots

    def _load_drawib_vg_values(self, draw_ib: str, workspace_root: str) -> set[int]:
        """读取 DrawIB 全部组件写回的 VGMap 全局骨骼 id 集合（无数据返回空）。

        保留**全量**值域（含去重借位到其它部件 canonical 槽位的值），供需要
        「本部件引用了哪些全局槽位」的地方使用；吸收判定必须用
        `_load_drawib_vg_slots` 的自属段子集，理由见该函数文档。
        """
        all_slots, _own_segment_slots = self._load_drawib_vg_slots(
            draw_ib, workspace_root
        )
        return all_slots

    def _is_drawib_absorbed(
        self,
        draw_ib: str,
        workspace_root: str,
        foreign_used_group_ids: set[int],
    ) -> bool:
        """判定整个缺席的 DrawIB 的**几何**是否已被合并进其它对象（几何归属口径）。

        ## 历史事故（2026-09-16，叶瞬光01 游戏内实测：脸部被误插占位）

        旧判据 = ``VGMap 全量值域 ∩ 现存对象实际引用的槽位``。Z 轴的合并骨架会
        按骨骼矩阵 bitwise 去重，把跨部件共享的骨骼合并成**一个 canonical 全局
        槽位**，于是**任何**引用了共享骨骼的部件，其 VGMap 里都会出现「借位到
        别的部件声明段」的槽位。现存对象（尤其是被 join 过的载体对象）必然引用
        这些 canonical 槽位，于是「共享骨骼」被读成「几何被吸收」——脸部部件
        ``c209c22b-45087-0``（135 根骨骼 / 原顶点 10859）的 VGMap 里出现了
        ``3b1b73fe`` 声明段 [180,209) 与 ``4a178546`` 声明段 [209,256) 的借位
        槽位，仅凭这些槽位被载体对象引用就被判「已被合并」并注入极限小三角
        占位（同类样本：``869976a3-5202-0`` 的 VGMap 借位槽位 185/200）。

        ## 判据（用户 2026-10-14 裁决：**以局部顶点组为准**）

        参数 ``foreign_used_group_ids`` 只包含「**引用方自己没声明过**的槽位引用」
        （见 `_collect_foreign_used_group_ids`）——即那些引用无法用引用方自己的
        局部顶点组（统一顶点组经 VGMap 反查回局部顶点组）解释，只能是别的部件的
        局部骨骼连同几何一起进了这个对象。

        吸收证据 = **本部件自属声明段** ``[VGOffset, VGOffset + VGCount)`` 内的
        VGMap 值 ∩ ``foreign_used_group_ids`` 非空：

        - **真被 join 走**：本部件专属骨骼（自属段身份）随几何一起出现在别人的
          对象里，而那些槽位**不是**引用方自己声明的 → 证据非空 → 全组件补占位
          抑制原版防重影（计划书 R7 ③ 行为不变）；
        - **只是共享骨骼（含本轮修复的「合并组」）**：本部件自属段槽位被引用，
          但引用它的对象**自己也声明了同一个槽位**（去重把两部件的同一骨骼并成
          canonical）→ 该引用被归属回引用方自己的局部顶点组 → 证据为空 →
          **不判定被吸收、不注入占位**（该 DrawIB 不进 mod，游戏内保留原版绘制）。
          实测误判场景（用户报告）：A 的顶点组 {1,2,3,4,5}、B 的 {5,6,7,8}，5 是
          去重合并组且 canonical 落在 B 的自属声明段；只导出 A 时 A 确实引用了
          5，但 5 在 A 自己的 VGMap 值域里（A 借位使用 canonical 槽）⇒ 非外来
          引用 ⇒ B 没被 join 进 A ⇒ 不得给 B 补占位。

        EFMI 侧的写盘域被 ``build_per_mesh_identity_map`` +
        ``validate_export_indices_in_segment`` 钉在自属段内（canonical 借位会被
        改写成自身份），所以它的 ``used ∩ vg_values`` 事实上等价于本函数现在的
        「自属段 ∩ 非自属引用」。

        **退化情形**（没有任何自属段内的 VGMap 值：旧缓存缺 VGOffset/VGCount，
        或该部件全部骨骼都在去重中被别的部件选为 canonical）：写盘域证据不可
        构造，退回历史宽松口径（宁松勿漏——EFMI 侧 t37 实机裁决确认过「收紧会
        让被合并部件不再生成占位、修坏合并骨骼」）；该宽松口径**仍然**只认非
        自属引用（引用方自己声明过的槽位一律不计入），所以上面的合并组误判不会
        在退化路径上复现。

        跨组别引用由 `_warn_cross_group_bone_references` 在导出时大声报警
        （无校准模式下已禁止）。
        """
        all_slots, own_segment_slots = self._load_drawib_vg_slots(
            draw_ib, workspace_root
        )
        if not all_slots:
            return False
        if not own_segment_slots:
            return bool(all_slots & foreign_used_group_ids)
        return bool(own_segment_slots & foreign_used_group_ids)

    def _zzmi_workspace_vg_count(self, workspace_unique_str: str) -> int | None:
        """读回工作空间里该部件的 VGCount（合并骨架数据）；无法判定返回 None。

        ``None`` = 定位不到 json / 内容不可解析 ⇒ 调用方必须按旧行为（计入），
        不得当成 0。``0`` = 确证该部件是**普通路径**导入：几何用部件局部编号
        ``0..vg_count-1``，与合并骨架的全局槽位不是同一个编号空间。
        """
        json_path = self._zzmi_workspace_part_json_path(workspace_unique_str)
        if json_path is None:
            return None
        try:
            payload = JsonUtils.LoadFromFile(json_path)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return int(payload.get("VGCount", 0) or 0)
        except (TypeError, ValueError):
            return None

    def _zzmi_workspace_part_json_path(self, workspace_unique_str: str) -> str | None:
        """定位部件写回的子网格 json（``<ws>/<LOD>/<bare>/TYPE_*/<bare>.json``）。

        ``workspace_unique_str`` 既可能是裸身份也可能是带 LOD 前缀的身份
        （``LOD1.<bare>``）；定位不到返回 ``None``。
        """
        workspace_root = GlobalConfig.path_workspace_folder()
        value = str(workspace_unique_str or "").strip()
        if not workspace_root or not value:
            return None
        lod_name, bare = "LOD0", value
        if "." in value:
            head, tail = value.split(".", 1)
            if head.upper().startswith("LOD"):
                lod_name, bare = head, tail
        if not bare:
            return None
        submesh_dir = os.path.join(workspace_root, lod_name, bare)
        if not os.path.isdir(submesh_dir):
            return None
        try:
            type_dirs = os.listdir(submesh_dir)
        except OSError:
            return None
        for type_dir in type_dirs:
            if not type_dir.startswith("TYPE_"):
                continue
            json_path = os.path.join(submesh_dir, type_dir, bare + ".json")
            if os.path.isfile(json_path):
                return json_path
        return None

    def _zzmi_workspace_declared_slots(
        self, workspace_unique_str: str
    ) -> set[int] | None:
        """读回部件**自己声明**的统一顶点组槽位（子网格 json 的 ``VGMap`` 值域）。

        用于「以局部顶点组为准」的吸收判定：对象顶点引用的全局槽位若落在这个
        集合里，说明该引用能用**它自己的局部顶点组**解释（去重借位使用 canonical
        槽位属于此列），不构成「别的部件的几何被并进来」的证据。

        ``None`` = 定位不到 json / 内容不可解析 / 无 VGMap ⇒ 调用方必须**保留**
        该对象的全部引用（信息不足时按旧口径计入，宁松勿漏）。
        """
        json_path = self._zzmi_workspace_part_json_path(workspace_unique_str)
        if json_path is None:
            return None
        try:
            payload = JsonUtils.LoadFromFile(json_path)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        vg_map = payload.get("VGMap")
        if not isinstance(vg_map, dict) or not vg_map:
            return None
        declared: set[int] = set()
        for value in vg_map.values():
            try:
                slot = int(value)
            except (TypeError, ValueError):
                continue
            if slot >= 0:
                declared.add(slot)
        return declared

    def _collect_foreign_used_group_ids(self, ordered) -> set[int]:
        """收集「引用方**自己没声明过**」的全局骨骼槽位引用（局部顶点组归属口径）。

        合并骨架的去重会把跨部件共享的骨骼并成一个 canonical 全局槽位，于是
        「引用方借位使用 canonical 槽位」与「引用方的网格里真的混进了别的部件
        的几何」在**引用侧**长得一模一样；区分它们的唯一依据是**局部顶点组归属**
        ——该引用能不能用引用方**自己的**局部顶点组（统一顶点组经 VGMap 反查）
        解释。落在引用方自己 `VGMap` 值域里的槽位 = 自己的骨骼（借位或自身份），
        不构成吸收证据；不在值域里的槽位 = 别的部件的局部骨骼连同几何一起进了
        这个对象。

        两个过滤（缺一不可）：

        1. **编号空间**：只统计几何确实用**全局槽位编号**的对象（合并骨架模式
           导入 ⇔ 子网格 json ``VGCount > 0``）。普通路径导入的部件顶点组名是
           局部编号 ``0..vg_count-1``，与全局槽位不是同一个编号空间；把它们的
           ``0`` 当成全局槽位，会让 `_is_drawib_absorbed` 把「另一个部件的自属段
           槽位 0 被引用」当成几何归属证据。实测事故（2026-10-14，快速局部导出
           左推进器 ``acfa670a``）：该部件 json ``VGCount=0``（普通路径，顶点组名
           只有局部编号 ``0``），而右翅膀尖 ``14cf141e`` 的自属声明段恰为
           ``[0,1)`` ⇒ 编号空间不同却被求交命中，误判「几何已被合并」并注入占位
           小三角面；该占位段把 ``vs-t0`` 覆盖成合并骨架且**不恢复**，于是同帧
           紧随其后、**共享同一份 palette 资源（hash 23de2d6a）因而不会重新绑定
           vs-t0** 的左翅膀尖 ``1b655510`` 被错误的骨架蒙皮 —— 游戏内消失 /
           变形 + 高频闪烁（导出右推进器 ``760c8064`` 时左右对调，受害的是
           ``14cf141e``）。
        2. **局部顶点组归属**（本次修复，2026-10-14）：引用方 json 的 ``VGMap``
           值域内的槽位一律不计入（见 `_zzmi_workspace_declared_slots`）。实测
           误判（用户报告）：A 的顶点组 {1,2,3,4,5}、B 的 {5,6,7,8}，5 是去重时
           合并的「合并组」且 canonical 落在 B 的自属声明段；只导出 A 时 A 确实
           引用了 5（用的是自己的局部顶点组），旧口径把它读成「B 被并进 A」→
           给根本没被 join 的 B 补了占位。

        判定不到部件 json（`_zzmi_workspace_vg_count` / `_zzmi_workspace_declared_slots`
        返回 None）时按旧行为**计入** —— 信息不足时宁可保守插桩，也不要漏掉真正
        被吸收的几何。
        """
        foreign = set()
        vg_count_cache: dict[str, int | None] = {}
        declared_cache: dict[str, set[int] | None] = {}
        for draw_call in ordered:
            try:
                obj_name = draw_call.get_blender_obj_name()
            except Exception:
                continue
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            if obj is None or obj.get("ZZMI_STUB"):
                continue
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                unique_str = ""
            if unique_str:
                if unique_str not in vg_count_cache:
                    vg_count_cache[unique_str] = self._zzmi_workspace_vg_count(
                        unique_str
                    )
                vg_count = vg_count_cache[unique_str]
                if vg_count is not None and vg_count <= 0:
                    # 局部编号空间：组名不是全局槽位，不构成「引用了别人自属段」的证据
                    continue
                if unique_str not in declared_cache:
                    declared_cache[unique_str] = self._zzmi_workspace_declared_slots(
                        unique_str
                    )
            declared = declared_cache.get(unique_str)
            mesh = getattr(obj, "data", None) if obj is not None else None
            vertices = getattr(mesh, "vertices", None)
            if vertices is None:
                continue
            for vertex in vertices:
                for group_elem in vertex.groups:
                    if group_elem.weight <= 0:
                        continue
                    try:
                        group_index = int(group_elem.group)
                        group_name = str(obj.vertex_groups[group_index].name).strip()
                    except (AttributeError, IndexError, TypeError, ValueError):
                        continue
                    if not group_name.isdigit():
                        continue
                    slot = int(group_name)
                    if declared is not None and slot in declared:
                        # 引用方自己的局部顶点组：不构成几何吸收证据
                        continue
                    foreign.add(slot)
        return foreign

    def _build_shader_replace_base_vertex_map(self) -> dict[int, int]:
        """返回重定向 DrawCall 身份到 base_vertex 的映射。"""
        base_vertex_map: dict[int, int] = {}
        for carrier_ib, redirect_info in (self._redirect_carrier_map or {}).items():
            base_vertex = int(redirect_info.get("base_vertex", 0) or 0)
            for drawib_model in self.drawib_model_list:
                if drawib_model.draw_ib != carrier_ib:
                    continue
                for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
                    for draw_call in getattr(submesh_model, "drawcall_model_list", []) or []:
                        base_vertex_map[id(draw_call)] = base_vertex
        return base_vertex_map


    def _create_stub_object(self, bare_unique_str: str) -> str:
        """创建占位对象：3 顶点 1 三角面（1e-6 尺度），权重挂在已注册槽。

        权重组名必须是**已注册槽**（json VGMap 首值，对齐 EFMI
        efmi.py:_resolve_stub_registered_slot）：ZZZ 合并骨架模式下组名 =
        全局骨骼 id，占位三角的权重挂 json VGMap 引用槽即落在合法全局槽内，
        能通过导出侧数字组检查（submesh_model 的 index==name 不变量）——
        不依赖「0 恒在范围内」的巧合；json 无 VGMap（局部命名空间/无反查数据）
        保持 "0" 与旧行为一致（无反查数据不插桩语义由 _is_drawib_absorbed 保证）。
        """
        workspace_unique_str = bare_unique_str
        if not workspace_unique_str.upper().startswith("LOD"):
            workspace_unique_str = "LOD0." + workspace_unique_str

        mesh = None
        obj = None
        try:
            mesh = bpy.data.meshes.new(name="ZZMI_STUB_MESH_" + workspace_unique_str)
            mesh.from_pydata(
                [(0.0, 0.0, 0.0), (1e-6, 0.0, 0.0), (0.0, 1e-6, 0.0)],
                [],
                [(0, 1, 2)],
            )
            mesh.update()

            obj = bpy.data.objects.new(name=workspace_unique_str, object_data=mesh)
            obj["ZZMI_STUB"] = 1
            obj["3DMigoto:WorkspaceUniqueStr"] = workspace_unique_str
            slot_group_name = self._resolve_stub_registered_slot(bare_unique_str)
            vertex_group = obj.vertex_groups.new(name=slot_group_name)
            vertex_group.add([0, 1, 2], 1.0, 'REPLACE')

            try:
                bpy.context.collection.objects.link(obj)
            except Exception:
                bpy.context.scene.collection.objects.link(obj)
            return obj.name
        except Exception:
            if obj is not None and bpy.data.objects.get(obj.name) is not None:
                self._remove_stub_object_data(obj)
            elif mesh is not None and getattr(mesh, "users", 0) == 0:
                try:
                    bpy.data.meshes.remove(mesh)
                except (AttributeError, RuntimeError):
                    pass
            raise

    def _resolve_stub_registered_slot(self, bare_unique_str: str) -> str:
        """解析占位三角的权重槽：合并骨架部件取 json VGMap 的第一个非负值。

        缺失部件的 VGMap 引用槽是全局骨骼编号（ZZMI 组基址拼接后的合法全局槽），
        占位权重组名落在已注册槽内即可通过导出侧数字组检查；json 无 VGMap
        （局部命名空间/无反查数据）时返回 "0"（与旧行为一致）。
        搜索顺序：LOD0 目录 -> 工作空间根目录兜底（ZZZ 常规在 LOD0）。
        """
        base = GlobalConfig.path_workspace_folder()
        for root in (os.path.join(base, "LOD0"), base):
            submesh_dir = os.path.join(root, bare_unique_str)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in sorted(os.listdir(submesh_dir)):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, bare_unique_str + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                vg_map = payload.get("VGMap") or {}
                for raw in vg_map.values():
                    try:
                        slot = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if slot >= 0:
                        return str(slot)
                return "0"
        return "0"

    def _is_component_dedup_excluded(self, bare_unique_str: str) -> bool:
        """部件是否被用户显式排除（json 标记 VGMapDedupExcluded=True）。

        显式排除 = 用户意图「完全不出现在 mod 里」：跳过占位小三角面生成，游戏侧
        保留原版绘制。与占位的 `absorbed`（几何被合并进其它对象）语义正交——
        合并场景下的缺失部件仍须占位抑制重影（absorbed=True 补占位），排除部件
        则相反（即使被引用也不插桩）。搜索顺序同 _resolve_stub_registered_slot：
        LOD0 目录 -> 工作空间根目录兜底。
        """
        base = GlobalConfig.path_workspace_folder()
        for root in (os.path.join(base, "LOD0"), base):
            submesh_dir = os.path.join(root, bare_unique_str)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in sorted(os.listdir(submesh_dir)):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, bare_unique_str + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                if bool(payload.get("VGMapDedupExcluded")):
                    return True
                return False
        return False

    def _cleanup_stub_objects(self):
        """导出结束后移除占位对象、mesh 数据和注入蓝图的 DrawCall。"""
        tracked_draw_calls = list(getattr(self, "_zzmi_stub_draw_calls", []) or [])
        object_names = set(getattr(self, "_zzmi_stub_object_names", []) or [])
        object_names.update(
            self._draw_call_object_name(draw_call)
            for draw_call in tracked_draw_calls
        )
        object_names.discard("")
        tracked_draw_call_ids = {id(draw_call) for draw_call in tracked_draw_calls}
        ordered = getattr(
            getattr(self, "blueprint_model", None),
            "ordered_draw_obj_data_model_list",
            None,
        )
        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if id(draw_call) not in tracked_draw_call_ids
                and not getattr(draw_call, "zzmi_stub", False)
            ]

        for obj_name in sorted(object_names):
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            self._remove_stub_object_data(obj)
        if object_names:
            print(f"[ZZMI骨骼合并] 已清理 {len(object_names)} 个占位小三角面对象")
        self._zzmi_stub_object_names = []
        self._zzmi_stub_draw_calls = []

    def _collect_merged_skeleton_components(self):
        """收集 ZZMI 合并骨架组件信息（按 DrawIB 去重，骨架组+vg_offset 排序）。

        双条件门控：复选框 import_merged_vgmap 开启 且 子网格 json 已由反查写回
        VGCount > 0（common/zzmi_skeleton.py 的 ensure_skeleton_data）。
        同 DrawIB 的拆分子网格共享同一 palette/偏移，只取第一个有效值。
        skeleton_group：渲染 cb1 对象变换分组号（json SkeletonGroup 字段），
        每组一套 ResourceZZMergedSkeleton_G<N>，跨组绝不共享。

        B1（契约接线）：本方法只**扫描与统计**，不中止导出——结果写入
        ``self._zzmi_merged_contract_stats``（``checkbox_enabled`` /
        ``parts_with_data`` / ``parts_with_global_ids`` / ``component_count`` /
        ``skip_reasons``），由
        ``_enforce_merged_skeleton_contract`` 在写盘前判定 error/warning/notice。
        开关关闭时**不做早退**：带数据的部件照样要统计，否则「开关关闭 + 有数据」
        这条 error 永远无法被评估。
        返回 (components, {draw_ib: component_id})。
        """
        components = []
        checkbox_enabled = bool(GlobalProterties.import_merged_vgmap())
        # B1：契约判定所需的统计。parts_with_data 的判据 = 子网格 json 里
        # vg_count > 0（= 导入侧写回过 VGMap）；skip_reasons 面向用户、按 DrawIB 去重。
        parts_with_data: set[str] = set()
        # 契约判据修正：几何**确实**按全局骨骼编号导出的部件（判据 = 顶点组编号
        # 空间，而不是「json 里有缓存」——后者是导入侧无条件生成的派生数据）。
        parts_with_global_ids: set[str] = set()
        skip_reasons: dict[str, str] = {}
        # 阻断修复（t80 §2.4）：按「内容可用」而**不是**按版本号放行的旧缓存，
        # 逐 DrawIB 记一条可读痕迹（绝不静默降级）。
        migrated_cache: dict[str, int] = {}

        def _record_reject(component_draw_ib: str, reason: str) -> None:
            skip_reasons.setdefault(str(component_draw_ib), reason)

        def _record_migration(component_draw_ib: str, version: int) -> None:
            migrated_cache.setdefault(str(component_draw_ib), int(version))

        for drawib_model in self.drawib_model_list:
            draw_ib = str(drawib_model.draw_ib)
            for submesh_model in drawib_model.submesh_model_list:
                vg_count = int(getattr(submesh_model, "vg_count", 0) or 0)
                if vg_count <= 0:
                    continue
                # B1：**先**统计带数据的部件（开关关闭时同样统计）。
                parts_with_data.add(draw_ib)
                if self._submesh_uses_global_bone_ids(submesh_model, vg_count):
                    parts_with_global_ids.add(draw_ib)
                if not checkbox_enabled:
                    # 开关关闭：本部件不进入合并骨架（导出仍走局部编号），但**不早退**——
                    # 「开关关闭 + 有数据」必须交给契约判定（否则导出会静默退化成
                    # 全局骨骼编号 + 无运行时骨架）。
                    break
                if not bool(
                    getattr(submesh_model, "merged_skeleton_metadata_valid", True)
                ):
                    print(
                        f"[ZZMI骨骼合并] 警告 {draw_ib}: "
                        "骨骼合并元数据含非整数/越界值，该部件不进入合并骨架；"
                        "请重新生成骨骼合并缓存"
                    )
                    _record_reject(draw_ib, "骨骼合并元数据含非整数/越界值")
                    continue
                cache_version = getattr(submesh_model, "vg_map_algorithm_version", None)
                if (
                    cache_version is not None
                    and int(cache_version or 0) != ZZMI_VG_MAP_ALGORITHM_VERSION
                ):
                    # 阻断修复（t80 §2.4 实测）：旧版缓存**不得**再"逐件拒绝 +
                    # 契约把整次导出中止"。v5 相对 v4 的新增字段全部可由既有 VGMap
                    # 单独推导（见 `_merged_resolve_channel_plans` 与
                    # common/zzmi_channel.select_channel_plan），palette 也在
                    # ModImpRuntime 里 ⇒ 缓存可用性应看**内容**（VGMap 完整覆盖 +
                    # 槽位合法），而不是看版本号这一个整数。
                    #
                    # 放行范围**显式限定**在已知可升级的旧版本（当前 = 4），
                    # 与导入侧 `ZZMISkeletonMergeHelper.MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS`
                    # 同一口径；未知版本仍拒绝（宁缺毋滥）。
                    if (
                        int(cache_version or 0)
                        in _ZZMISkeletonMergeHelper.MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS
                        and self._merged_cached_vg_map_usable(submesh_model, vg_count)
                    ):
                        print(
                            f"[ZZMI骨骼合并] 提示 {draw_ib}: VGMap 缓存版本 "
                            f"{cache_version} != 当前版本 "
                            f"{ZZMI_VG_MAP_ALGORITHM_VERSION}，但 VGMap 内容完整"
                            "（可脱离 dump 使用）⇒ 按当前算法就地补算通道计划，"
                            "不拒绝该部件；请重新一键导入以把缓存固化到 v"
                            f"{ZZMI_VG_MAP_ALGORITHM_VERSION}"
                        )
                        _record_migration(draw_ib, int(cache_version or 0))
                    else:
                        print(
                            f"[ZZMI骨骼合并] 警告 {draw_ib}: "
                            f"VGMap 缓存版本 {cache_version} != 当前版本 "
                            f"{ZZMI_VG_MAP_ALGORITHM_VERSION}，且 VGMap 内容不完整"
                            "（无法脱离 dump 复用），拒绝导出该部件；"
                            "请先用当前 FrameAnalysis，或仅凭工作区缓存，重新一键导入"
                        )
                        _record_reject(
                            draw_ib,
                            f"VGMap 缓存版本 {cache_version} 过旧且内容不完整"
                            "（需重新一键导入）",
                        )
                        continue
                # t75：通道计划（唯一判定口径）随缓存一起就位。缓存缺失/版本不符时
                # **不静默**：这里先记下缺口，装配完成后由
                # `_merged_resolve_channel_plans` 在**整组**口径上补算一次（共享骨图
                # 是组级性质，单看一个部件算不出通道骨），并落显式诊断；补算仍失败
                # 的部件按"无通道部件"处置（撤销门控到达项、保留 palette 捕获与
                # attach），绝不退回出现次判据。
                channel_version = int(
                    getattr(submesh_model, "channel_plan_version", 0) or 0
                )
                channel_plan = getattr(submesh_model, "channel_plan", None)
                channel_cache_ok = bool(
                    channel_version == ZZMI_CHANNEL_PLAN_VERSION
                    and isinstance(channel_plan, dict)
                    and channel_plan.get("channel_slot") is not None
                    and channel_plan.get("channel_local") is not None
                )
                if not channel_cache_ok:
                    print(
                        f"[ZZMI骨骼合并] 提示 {draw_ib}: 通道计划缓存缺失或版本不符"
                        f"（{channel_version} != {ZZMI_CHANNEL_PLAN_VERSION}），"
                        "将按整组共享骨图重新推导通道骨；请重新一键导入以固化缓存"
                    )
                # 导出侧防线：VGMap 必须完整覆盖 0..vg_count-1 且槽位非负。
                # 缓存正常时由 ensure_skeleton_data 保证；此处兜底拦截陈旧/被
                # 手工改坏的 json——缺键会让 attach CS 的 vg_map.get(local, 0)
                # 静默塌缩到槽位 0，整块蒙皮炸裂，宁可整部件退出合并骨架。
                try:
                    vg_map = {}
                    for raw_key, raw_value in (
                        getattr(submesh_model, "vg_map", {}) or {}
                    ).items():
                        key = int(raw_key)
                        if key in vg_map:
                            raise ValueError(f"规范化后键重复: {key}")
                        vg_map[key] = int(raw_value)
                except (TypeError, ValueError):
                    vg_map = {}
                expected_keys = set(range(vg_count))
                missing_keys = sorted(expected_keys - set(vg_map.keys()))
                extra_keys = sorted(set(vg_map.keys()) - expected_keys)
                negative_slots = [slot for slot in vg_map.values() if slot < 0]
                oversized_slots = [slot for slot in vg_map.values() if slot > 0xFFFFFFFF]
                vg_offset = int(getattr(submesh_model, "vg_offset", 0) or 0)
                skeleton_group = int(getattr(submesh_model, "skeleton_group", 0) or 0)
                if (
                    missing_keys
                    or extra_keys
                    or negative_slots
                    or oversized_slots
                    or vg_offset < 0
                    or skeleton_group < 0
                ):
                    print(
                        f"[ZZMI骨骼合并] 警告 {draw_ib}: VGMap 未完整覆盖 "
                        f"0..{vg_count - 1}（缺失 {missing_keys[:5]}，多余 {extra_keys[:5]}）"
                        "、槽位/偏移/分组越界，"
                        "该部件不进入合并骨架；请重新一键导入刷新骨骼合并缓存"
                    )
                    if missing_keys or extra_keys:
                        _record_reject(
                            draw_ib,
                            f"VGMap 未完整覆盖 0..{vg_count - 1}"
                            f"（缺失 {missing_keys[:5]}，多余 {extra_keys[:5]}）",
                        )
                    else:
                        _record_reject(
                            draw_ib, "VGMap 槽位/偏移/分组含越界值"
                        )
                    continue
                components.append({
                    "draw_ib": drawib_model.draw_ib,
                    "unique_str": str(getattr(submesh_model, "unique_str", "") or ""),
                    "vg_offset": vg_offset,
                    "vg_count": vg_count,
                    "skeleton_group": skeleton_group,
                    # 局部骨骼 id -> 全局槽位（attach CS 按此写合并骨架，
                    # 本部件引用的共享 canonical 槽位当帧覆盖）
                    "vg_map": vg_map,
                    # 导出侧守卫元数据（反查写回）：deform pass draw 序号 +
                    # 原部件顶点数；缺省 0（旧缓存未刷新）
                    "deform_draw": int(getattr(submesh_model, "deform_draw_index", 0) or 0),
                    "original_vertex_count": int(
                        getattr(submesh_model, "original_vertex_count", 0) or 0
                    ),
                    # t75 通道计划（唯一判定口径；导入期算好，导出期只读）
                    "channel": dict(channel_plan),
                    "channel_digest": str(
                        getattr(submesh_model, "channel_plan_digest", "") or ""
                    ),
                    # F6（复核发现）：缓存里落盘的「跨部件顶点权重合计」必须带进
                    # 导出侧——补算（缓存缺失）时它是候选排序的**第二键**，
                    # `ChannelPlanSlotWeights` 也才有消费方。缺省空表 = 全部权重 0
                    # （排序退化为按槽位号，仍确定性，但与导入期口径不一致）。
                    "slot_weights": {
                        int(key): int(value)
                        for key, value in (
                            getattr(submesh_model, "channel_plan_slot_weights", {}) or {}
                        ).items()
                        if str(key).lstrip("-").isdigit()
                    },
                })
                break
        if components:
            buffer_slots = max(c["vg_offset"] + c["vg_count"] for c in components)
            valid_components = []
            for component in components:
                invalid_slots = sorted({
                    slot for slot in component["vg_map"].values()
                    if slot >= buffer_slots
                })
                if invalid_slots:
                    print(
                        f"[ZZMI骨骼合并] 警告 {component['draw_ib']}: VGMap 槽位 "
                        f"{invalid_slots[:5]} 超出合并骨架范围 0..{buffer_slots - 1}，"
                        "该部件不进入合并骨架；请重新一键导入刷新骨骼合并缓存"
                    )
                    _record_reject(
                        component["draw_ib"],
                        f"VGMap 槽位 {invalid_slots[:5]} 超出合并骨架范围 "
                        f"0..{buffer_slots - 1}",
                    )
                    continue
                valid_components.append(component)
            components = valid_components
        components.sort(key=lambda c: (c["skeleton_group"], c["vg_offset"], c["draw_ib"]))
        # t75：缓存缺失通道计划的部件在这里按**整组共享骨图**补算（缓存优先，
        # 只补缺的组），补算结果写回组件记录；仍未解析出通道的部件保持 None
        # ⇒ 由 `_merged_component_channel_record` 判为"无通道部件"。
        self._merged_resolve_channel_plans(components)
        component_id_dict = {c["draw_ib"]: i for i, c in enumerate(components)}
        # B1：把判定输入快照落盘在实例上（_enforce_merged_skeleton_contract 读取）。
        self._zzmi_merged_contract_stats = {
            "checkbox_enabled": checkbox_enabled,
            "parts_with_data": len(parts_with_data),
            "parts_with_global_ids": len(parts_with_global_ids),
            "component_count": len(components),
            "skip_reasons": dict(skip_reasons),
            # 阻断修复（t80 §2.4）：因「版本号旧但内容完整」而按当前算法就地补算
            # 通道计划、未被拒绝的部件（{draw_ib: 旧版本号}）。为空 = 无降级。
            "migrated_cache": dict(migrated_cache),
        }
        return components, component_id_dict

    @staticmethod
    def _zzmi_drawcall_object_present(draw_call) -> bool:
        """该 DrawCall 解析到的 Blender 对象在场景里**真实存在**吗（阻断修复 t80 §2.3）。

        `DrawCallModel.get_blender_obj_name()` = ``source_obj_name or obj_name``：
        前处理把对象改成副本后，`to_draw_call_model()` 已把 source_obj_name 写成
        副本名，因此这里读到的是**场景里真正要导出的那个对象**，而不是蓝图声明的
        源名。拿不到对象名（或宿主拿不到真实场景）时返回 True —— 宁可不插桩，
        也不要在信息不足时凭空判定部件缺席。
        """
        try:
            resolved = str(draw_call.get_blender_obj_name() or "").strip()
        except Exception:
            return True
        if not resolved:
            return True
        return _scene_object_present(resolved)

    @staticmethod
    def _merged_cached_vg_map_usable(submesh_model, vg_count: int) -> bool:
        """旧版 VGMap 缓存是否**仅凭自身内容**可用（不依赖 FrameAnalysis/dump）。

        阻断修复（t80 §2.4 实测）：版本号从 4 抬到 5 后，旧工作区被"逐件拒绝 +
        契约把整次导出中止"，而"重新一键导入"依赖的 FrameAnalysis 帧可能已被
        删除 ⇒ 用户无路可走。v5 相对 v4 的**全部**新增字段都是可由既有 VGMap
        推导的派生量（``ChannelPlan*`` / ``BoneIdentity*`` /
        ``ChannelPlanSlotWeights``，见 `_merged_resolve_channel_plans` 与
        ``common/zzmi_channel.select_channel_plan``），palette 也在 ModImpRuntime
        里，因此**判据应当是内容完整性，而不是版本号**：

        - ``VGMap`` 是 dict、键**完整覆盖** ``0..vg_count-1``（缺键会让 attach CS
          的 ``vg_map.get(local, 0)`` 静默塌缩到槽位 0，整块蒙皮炸裂）；
        - 槽位是非负整数且在 32 位范围内。

        返回 False 时调用方仍按"拒绝该部件"处置（内容不完整无法安全复用）。
        """
        try:
            expected_count = int(vg_count)
        except (TypeError, ValueError):
            return False
        if expected_count <= 0:
            return False
        raw_map = getattr(submesh_model, "vg_map", None)
        if not isinstance(raw_map, dict) or not raw_map:
            return False
        try:
            normalized: dict[int, int] = {}
            for raw_key, raw_value in raw_map.items():
                key = int(raw_key)
                if key in normalized:
                    return False
                normalized[key] = int(raw_value)
        except (TypeError, ValueError):
            return False
        if set(normalized.keys()) != set(range(expected_count)):
            return False
        return all(0 <= slot <= 0xFFFFFFFF for slot in normalized.values())

    @staticmethod
    def _submesh_uses_global_bone_ids(submesh_model, vg_count: int) -> bool:
        """该部件的**几何**顶点组是否落在全局骨骼编号空间（契约判据修正）。

        契约原先只看「json 里有没有 VGMap 缓存」就断定几何用全局编号，但这份缓存
        是导入侧**无条件**落盘的派生数据（ui/ui_func_import_ssmt.py「生成侧与消费侧
        分离」），与「本次导入是否按合并骨架建顶点组」无关 ⇒ 只要工作区被无条件
        生成过缓存，从未开启过该开关的普通导出也会被判成致命错误。

        几何的真实编号空间只能从顶点组本身读：
        - 普通导入：组名 = 部件局部编号 ``0..vg_count-1``（消费侧的合并骨架预处理
          完全由「使用融合统一顶点组」门控，见 common/submesh_model.py）；
        - 合并导入：组名 = 全局骨骼 id ``[VGOffset, VGOffset+VGCount)``。

        `_prepare_merged_skeleton_vertex_groups` 只补缺/排序、**不改数值**，所以
        「最大数字组名 >= vg_count」是全局编号的充分判据：全局最大组名
        = ``VGOffset + vg_count - 1 >= vg_count``（``VGOffset >= 1``）；而
        ``VGOffset == 0`` 时全局与局部编号完全重合、导出结果一致，判为局部无害。

        拿不到对象（被删/改名）或无数字组时返回 False——契约只在**确证**几何是
        全局编号时才中止导出，不再把普通导出误判为致命错误。
        """
        try:
            expected = int(vg_count)
        except (TypeError, ValueError):
            return False
        if expected <= 0:
            return False
        saw_object = False
        max_numeric_id = -1
        for draw_call in getattr(submesh_model, "drawcall_model_list", []) or []:
            try:
                obj_name = draw_call.get_blender_obj_name()
            except Exception:
                continue
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            if obj is None:
                continue
            saw_object = True
            for vertex_group in getattr(obj, "vertex_groups", []) or []:
                name = str(getattr(vertex_group, "name", ""))
                if name.isascii() and name.isdigit():
                    max_numeric_id = max(max_numeric_id, int(name))
        if not saw_object:
            return False
        return max_numeric_id >= expected

    def _enforce_merged_skeleton_contract(self) -> dict:
        """B1：按合并骨架契约判定「继续 / 中止」，**必须在任何写盘之前调用**。

        判据与行动（契约实现见 ``common/zzmi_merged_contract.py``）：
        - ``error``（有数据但一个组件都没收到，且**几何确实使用全局骨骼编号**，
          或开关开启后部件全部被拒）⇒ 抛 ``Fatal``（仓库既有的致命错误
          通道）：导出操作符的 ``except Exception`` 会把它变成用户可见的
          ``self.report({'ERROR'}, "导出失败: …")``（``ui/ui_func_export.py``），
          并行轮次的 ``_run_worker`` 也把 ``str(error)`` 回传到主进程报错；
          message + hint 一并进入异常文本，因此**不是**仅 print。
        - ``warning``（部分部件被拒）⇒ 用户可见提示（控制台 + 导出日志）后继续导出。
        - ``notice``（无数据也无组件 = 普通导出）⇒ 仅记一行日志，**不打断用户**。
        - ``ok`` ⇒ 无动作。

        返回契约判定结果（供测试断言与上层报告使用）。
        """
        # 函数内导入：本模块在轻量 fake 宿主里被装载时，假包 ``__path__`` 为空、
        # 解析不了 ``common/zzmi_merged_contract`` 这个新依赖；本方法只在真实导出
        # 路径（含新测试）被调用，放在函数内可避免拖垮无关测试的模块装载。
        from ...common.zzmi_merged_contract import (
            evaluate_merged_skeleton_contract,
        )

        stats = getattr(self, "_zzmi_merged_contract_stats", None)
        if not stats:
            # 兜底：尚未扫描过（例如直接调用本方法）时自行扫描一次。
            self._collect_merged_skeleton_components()
            stats = self._zzmi_merged_contract_stats
        decision = evaluate_merged_skeleton_contract(
            checkbox_enabled=bool(stats.get("checkbox_enabled")),
            parts_with_data=int(stats.get("parts_with_data") or 0),
            parts_with_global_ids=int(stats.get("parts_with_global_ids") or 0),
            component_count=int(stats.get("component_count") or 0),
            skip_reasons=stats.get("skip_reasons") or {},
        )
        level = str(decision.get("level") or "")
        message = str(decision.get("message") or "")
        hint = str(decision.get("hint") or "")
        detail = "\n".join(part for part in (message, hint) if part)

        if level == "error":
            # Fatal 是本仓库既有的「终止整个操作」通道（utils/format_utils.py），
            # 且 common/mesh_create_helper.py 已用它在同域（合并骨架 VGMap 数据）
            # 报致命错误；调用方 `except Exception` → report({'ERROR'}) 送达用户。
            raise Fatal(detail or "骨骼合并契约校验失败，已中止导出。")

        if level == "warning":
            self._notify_merged_contract(detail, warning=True)
        elif level == "notice":
            # 普通导出（没有合并数据）：只留一行日志，绝不弹提示/不打断。
            print(f"[ZZMI骨骼合并] {message}")
        return decision

    @staticmethod
    def _notify_merged_contract(detail: str, warning: bool = False) -> None:
        """把非致命提示送到用户可见处（控制台 + 导出日志；LOG 不可用则只 print）。"""
        prefix = "[ZZMI骨骼合并] 警告 " if warning else "[ZZMI骨骼合并] "
        for line in str(detail).splitlines() or [""]:
            print(f"{prefix}{line}")
        try:
            from ...utils.log_utils import LOG

            (LOG.warning if warning else LOG.info)(detail)
        except Exception:
            # 轻量宿主/日志链异常都不得影响导出本身。
            pass

    def _get_submesh_ib_key(self, submesh_model, draw_ib):
        return f"{draw_ib}_{submesh_model.match_first_index}"

    def _append_drawindexed_with_shader_replace(
        self, section, drawcall_list, draw_offset_dict, base_vertex=0
    ):
        """将 drawcall 列表写入 section，对着色器替换物体使用条件运行逻辑替代 drawindexed。

        ``base_vertex`` 用于合并网格自动重定向。保持普通绘制的同一输出路径，
        因此重定向绘制也会生成 mesh 注释、条件块和 shader-replace 逻辑。
        """
        if not self.has_shader_replace:
            drawindexed_kwargs = {"obj_name_draw_offset_dict": draw_offset_dict}
            if base_vertex:
                drawindexed_kwargs["base_vertex"] = base_vertex
            for drawindexed_str in M_IniHelper.get_drawindexed_str_list(drawcall_list, **drawindexed_kwargs):
                section.append(drawindexed_str)
            return

        resolved_drawcalls = [
            (
                drawcall,
                M_IniHelper.get_draw_call_shader_replace_info_list(
                    drawcall,
                    shader_replace_object_names=self.shader_replace_object_names,
                    shader_replace_object_info_map=self.shader_replace_object_info_map,
                    shader_replace_info_list=self.shader_replace_info_list,
                ),
            )
            for drawcall in drawcall_list
        ]
        for dc, obj_infos in resolved_drawcalls:
            if not obj_infos:
                drawindexed_kwargs = {"obj_name_draw_offset_dict": draw_offset_dict}
                if base_vertex:
                    drawindexed_kwargs["base_vertex"] = base_vertex
                for drawindexed_str in M_IniHelper.get_drawindexed_str_list([dc], **drawindexed_kwargs):
                    section.append(drawindexed_str)
                continue

            draw_offset = dc.index_offset
            if draw_offset_dict:
                draw_offset = draw_offset_dict.get(dc.obj_name, dc.index_offset)

            # 输出物体标识注释（与 get_drawindexed_str_list 格式一致）
            display_name = str(getattr(dc, 'obj_name', '') or '')
            section.append(f"; [mesh:{display_name}] [vertex_count:{dc.vertex_count}]")

            for info in obj_infos:
                condition_str = dc.get_condition_str()
                indent = "  " if condition_str else ""
                if condition_str:
                    section.append(f"if {condition_str}")
                run_lines = M_IniHelper.get_shader_replace_run_logic(
                    info,
                    dc.match_draw_ib or "0",
                    dc.match_first_index if dc.match_first_index else "0",
                    info.get('component_index', 0),
                    dc.index_count,
                    draw_offset,
                    base_vertex,
                )
                for line in run_lines:
                    section.append(f"{indent}{line}")
                if condition_str:
                    section.append("endif")
            section.append("")

    @staticmethod
    def _format_name_set(names) -> list[str]:
        return sorted(str(name) for name in (names or []))

    @staticmethod
    def _format_cross_ib_info_dict(mapping) -> dict[str, list[str]]:
        ordered = {}
        for key in sorted((mapping or {}).keys(), key=str):
            ordered[str(key)] = sorted(str(item) for item in ((mapping or {}).get(key) or []))
        return ordered

    def _get_mapping_method(self, source_ib_key: str, target_ib_key: str) -> str:
        return self.cross_ib_mapping_method.get(
            (source_ib_key, target_ib_key),
            self.CROSS_IB_METHOD_VB_COPY,
        )

    def _get_source_methods(self, source_ib_key: str) -> set[str]:
        methods = {
            method
            for (mapped_source_key, _mapped_target_key), method in self.cross_ib_mapping_method.items()
            if mapped_source_key == source_ib_key
        }
        if not methods and source_ib_key in self.cross_ib_info_dict:
            methods.add(self.CROSS_IB_METHOD_VB_COPY)
        return methods

    def _get_source_body_vb_resource_name(self, source_hash: str, source_first_index: int) -> str:
        return f"ResourceBodyVB_{source_hash}_{source_first_index}"

    def _get_source_cb1_capture_resource_name(self, source_hash: str, source_first_index: int) -> str:
        return f"ResourceCaptureCB1_{source_hash}_{source_first_index}"

    def _get_target_cb1_temp_resource_name(self, target_hash: str, target_first_index: int) -> str:
        return f"ResourceTempCB1_{target_hash}_{target_first_index}"

    def _get_source_so0_resource_name(self, source_hash: str, source_first_index: int) -> str:
        return f"ResourceBodyVB0_{source_hash}_{source_first_index}"

    def _append_source_capture_sections(
        self,
        section: M_IniSection,
        source_hash: str,
        source_first_index: int,
        source_methods: set[str],
    ) -> None:
        if self.CROSS_IB_METHOD_VB_REF_SO0 in source_methods:
            section.append("[" + self._get_source_so0_resource_name(source_hash, source_first_index) + "]")
            section.append("type = Buffer")
            section.append("stride = 40")

        if self.CROSS_IB_METHOD_VB_COPY in source_methods or self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods or self.CROSS_IB_METHOD_VB_COPY_NORMAL in source_methods:
            section.append("[" + self._get_source_body_vb_resource_name(source_hash, source_first_index) + "]")

        if self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods:
            section.append("[" + self._get_source_cb1_capture_resource_name(source_hash, source_first_index) + "]")

    def _append_source_capture_lines(
        self,
        section: M_IniSection,
        source_hash: str,
        source_first_index: int,
        source_methods: set[str],
    ) -> None:
        if self.CROSS_IB_METHOD_VB_REF_SO0 in source_methods:
            section.append(
                self._get_source_so0_resource_name(source_hash, source_first_index) + " = ref so0"
            )

        if self.CROSS_IB_METHOD_VB_COPY in source_methods or self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods or self.CROSS_IB_METHOD_VB_COPY_NORMAL in source_methods:
            section.append(
                self._get_source_body_vb_resource_name(source_hash, source_first_index) + " = copy vb0"
            )

        if self.CROSS_IB_METHOD_VB_COPY_CB1 in source_methods:
            section.append(
                self._get_source_cb1_capture_resource_name(source_hash, source_first_index)
                + " = copy vs-cb1 unless_null"
            )

    def _append_source_capture_override(
        self,
        section: M_IniSection,
        texture_override_name_suffix: str,
        source_hash: str,
        source_first_index: int,
        source_methods: set[str],
    ) -> None:
        section.append("[TextureOverride_" + texture_override_name_suffix + "_copy]")
        section.append("hash = " + source_hash)
        section.append("match_first_index = " + str(source_first_index))
        if self.CROSS_IB_METHOD_VB_COPY_NORMAL not in source_methods:
            section.append("match_instance_count = 0")
        self._append_source_capture_lines(
            section,
            source_hash,
            source_first_index,
            source_methods,
        )

    def _append_target_cross_ib_draw(
        self,
        section: M_IniSection,
        method: str,
        source_hash: str,
        source_first_index: int,
        source_ib_resource_name: str,
        target_hash: str,
        target_first_index: int,
    ) -> None:
        section.append("ib = " + source_ib_resource_name)

        if method == self.CROSS_IB_METHOD_VB_REF_SO0:
            source_body_vb0_name = self._get_source_so0_resource_name(source_hash, source_first_index)
            section.append("vb0 = " + source_body_vb0_name)
            section.append("vb1 = Resource" + source_hash + "Texcoord")
            section.append("vb2 = Resource" + source_hash + "Blend")
            section.append("vb3 = " + source_body_vb0_name)
            return

        source_body_vb_name = self._get_source_body_vb_resource_name(source_hash, source_first_index)
        section.append("vb0 = " + source_body_vb_name)
        section.append("vb1 = Resource" + source_hash + "Texcoord")

        if method == self.CROSS_IB_METHOD_VB_COPY_CB1:
            temp_resource_name = self._get_target_cb1_temp_resource_name(target_hash, target_first_index)
            section.append(temp_resource_name + " = ref vs-cb1")
            section.append("vs-cb1 = " + self._get_source_cb1_capture_resource_name(source_hash, source_first_index))
        else:
            section.append("vb2 = Resource" + source_hash + "Blend")
            if method != self.CROSS_IB_METHOD_VB_COPY_NORMAL:
                section.append("vb3 = " + source_body_vb_name)

    def _append_target_cross_ib_cleanup(
        self,
        section: M_IniSection,
        method: str,
        target_hash: str,
        target_first_index: int,
    ) -> None:
        if method == self.CROSS_IB_METHOD_VB_COPY_CB1:
            temp_resource_name = self._get_target_cb1_temp_resource_name(target_hash, target_first_index)
            section.append("vs-cb1 = ref " + temp_resource_name)

    def _find_source_submesh(self, source_ib_key: str):
        source_parts = source_ib_key.split("_")
        source_hash = source_parts[0]
        source_first_index = int(source_parts[1]) if len(source_parts) > 1 else 0

        source_drawib_model = None
        for dib_model in self.drawib_model_list:
            if dib_model.draw_ib == source_hash:
                source_drawib_model = dib_model
                break

        if source_drawib_model is None:
            return None, None, source_hash, source_first_index

        for source_submesh in source_drawib_model.submesh_model_list:
            if str(source_submesh.match_first_index) == str(source_first_index):
                return source_drawib_model, source_submesh, source_hash, source_first_index

        return source_drawib_model, None, source_hash, source_first_index

    # ------------------------------------------------------------------
    # 合并骨架 v9：出现次槽位命名 / 守卫条件
    # ------------------------------------------------------------------

    @classmethod
    def _merged_skeleton_slots(cls) -> tuple[int, ...]:
        """出现次槽位号列表（1/2 循环）。改这里即可扩到更多槽位。"""
        return tuple(ZZMI_MERGED_SKELETON_SLOTS)

    @staticmethod
    def _merged_occ_var(component_id: int) -> str:
        """部件出现次计数器（deform 段顶层自增，1/2 循环）。"""
        return f"$zz_ms_occ_{component_id}"

    @staticmethod
    def _merged_seen_var(component_id: int, slot: int) -> str:
        """部件在槽 <slot> 的当帧到达标记（顶层 sticky 累加）。"""
        return f"$zz_ms_seen_{component_id}{slot}"

    @staticmethod
    def _merged_prev_var(component_id: int, slot: int) -> str:
        """部件在槽 <slot> 的「上一帧到达」标记（[Present] 从 `seen` 抄录后清零 seen）。

        用途（2026-09-17 重放时机修复，dump 实证见 `_merged_slot_seen_condition`）：
        守卫的豁免项必须是**按槽**预测的「本槽本帧不会来」，不能用「本帧至今没出现」。
        用上一帧的到达情况当预测值：上一帧在该槽到过 ⇒ 本帧当作「会来」，守卫必须
        等它；上一帧在该槽没到过 ⇒ 本帧当作「不会来」，豁免它、不阻塞守卫。

        两条语义各自对应一次实机事故：
        - 旧口径用 `$zz_ms_any_<i>`（本帧至今是否出现过）：帧首所有部件都还没出现，
          条件**恒真** → 守卫在载体自己的 deform pass 就落笔，排在载体之后的部件
          用的是**上一帧**的 palette（见下方 `_merged_slot_seen_condition` 的 dump
          实证）→ 角色身体一部分慢一帧、且慢的集合随引擎提交顺序变化 = 用户实测
          「身体莫名其妙的有一卡一卡」。
        - 而且 `any == 0` 对「整帧只出现一次」的部件永远不成立（它出现过），
          却要求它 `seen_<i><2> == 1`（它没有第 2 次）→ 第 2 槽守卫**永不闭合** →
          第二实例的 SO 永不写 → 用户实测「只有那个实例化的物体有问题，
          头发像转了 90 度」。
        """
        return f"$zz_ms_prev_{component_id}{slot}"

    @staticmethod
    def _merged_palette_name(draw_ib: str, slot: int) -> str:
        """该部件该槽的 palette 持久副本资源名。"""
        return f"ResourceZZPalette_{draw_ib}_s{slot}"

    @staticmethod
    def _merged_palette_lag_name(draw_ib: str, slot: int) -> str:
        """该部件该槽的 **上一帧** palette 副本资源名（跨组校准专用）。

        用户 2026-10-10 指出的自洽性问题：deform 段手里的 `ResourceZZCb1_G*`
        必然是**上一帧**的对象变换（渲染 draw 才写、deform 排在帧内最前），
        而 palette 捕获是**当帧**的 ⇒ 跨组换算拿的是
        `C(上一帧) × palette(当帧)`，把源部件当帧的骨摆进了**上一帧的宿主对象
        空间**，这正是「运动时错位、静止正常」。修法：跨组 attach 一律读这份
        **滞后副本**，让 `C` 与 palette 同帧（都是上一帧），结果 = 源部件上一帧
        的合法摆放（一致的一帧延迟，而不是两种空间的混合）。

        滞后副本在每次主捕获**之前**从主副本拷出（此时主副本还装着上一帧的值）；
        F7（宿主渲染段同帧重算）会先把主副本的当帧值刷进滞后副本，再跑同一套
        attach ⇒ 那条路径仍是**当帧**精确。
        """
        return f"ResourceZZPaletteLag_{draw_ib}_s{slot}"

    def _merged_seen_arrived_condition(self, component_id: int, slot: int) -> str:
        """消费点谓词：「本部件**本帧**已在槽 <slot> 出现过」（`>= 1`，帧内单调）。

        单一来源：所有消费点（SO-ready 门与每槽期望集合门）都必须用它，避免
        `== 1` / `>= 1` 两种写法散落在生成器里各写一遍（历史回归的形态就是
        消费点与状态机语义不一致）。理由见模块级 `ZZMI_MERGED_SEEN_PREDICATE`。
        """
        return (
            f"{self._merged_seen_var(int(component_id), int(slot))} "
            f"{ZZMI_MERGED_SEEN_PREDICATE} 1"
        )

    def _merged_diag_sink(self) -> list:
        """本导出期的诊断记录表（懒建，不依赖 `__init__` 是否跑到）。"""
        sink = getattr(self, "_zzmi_merge_diagnostics", None)
        if sink is None:
            sink = []
            self._zzmi_merge_diagnostics = sink
        return sink

    def _merged_diag(self, code: str, message: str, **fields) -> str:
        """记录一条结构性缺口诊断，返回写进 ini 段的机器可读注释行。

        输出三重落点（都是机器可检测的，不再静默降级）：
        1. 返回的注释行由调用方 append 进产物段（`; ZZMI-MERGE-DIAG <code> k=v ...`）；
        2. 同一条（按 code+字段去重）以 `⚠️ [ZZMI骨骼合并]` 打到 stdout——与既有
           诊断打印同口径，测试用 `contextlib.redirect_stdout` 断言；
        3. 进 `self._zzmi_merge_diagnostics`（list[dict]），供测试与上层报告读取。
        """
        record = {"code": str(code)}
        record.update({str(key): value for key, value in fields.items()})
        sink = self._merged_diag_sink()
        if record not in sink:
            sink.append(record)
            detail = " ".join(f"{key}={value}" for key, value in fields.items())
            print(f"⚠️ [ZZMI骨骼合并] {ZZMI_MERGE_DIAG_PREFIX} {code} {detail} {message}")
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        if not ZZMI_MERGE_DIAG_EMIT:
            # t149：诊断记录表与 stdout 提示已在上方落盘（开发/测试面），
            # 但配置表里**不写**这行注释。返回空串，由 `_append_merged_diag`
            # 负责"空串 = 连行都不追加"。
            return ""
        return f"{ZZMI_MERGE_DIAG_PREFIX} {code} {detail}"

    def _merged_reuse_site(
        self, draw_ib: str, skeleton_group: int, slot: int, kind: str
    ) -> str:
        """O3：登记一个「消费点」并返回其机器可读注释行（**不改任何 ini 语义**）。

        `kind` ∈ {"publish-cs"(蒙皮 CS 发布), "replay-draw"(宿主 draw 版重放),
        "replay-absorbed"(吸收宿主重放)}。同一 (组, 槽, 部件, 种类) 只登记一次
        （生成器对每个部件段各发一遍，计数按**发射点**记，与 `07-reuse-site-inventory.py`
        同口径）。
        """
        counts = getattr(self, "_merged_reuse_consumers", None)
        if counts is None:
            counts = {}
            self._merged_reuse_consumers = counts
        key = (int(skeleton_group), int(slot))
        counts.setdefault(key, [])
        entry = f"{draw_ib}|{kind}"
        if entry not in counts[key]:
            counts[key].append(entry)
        if not ZZMI_MERGE_DIAG_EMIT:
            # t149：消费点计数照旧登记（`_merged_reuse_ratio_records` 与测试要用），
            # 只是不再往配置表写这行注释。
            return ""
        return (
            f"{ZZMI_MERGE_DIAG_PREFIX} {ZZMI_MERGE_DIAG_REUSE_SITE}"
            f" group=G{int(skeleton_group)} slot={int(slot)}"
            f" consumer={draw_ib} kind={kind}"
        )

    @staticmethod
    def _append_merged_diag(section, line: str, indent: str = "") -> None:
        """把一条**开发者诊断注释**追加进段；空串（= 开关关闭）时连空行都不追加。

        开关默认关闭 ⇒ 配置表里既没有注释行、也没有被顶出来的空行，
        与非注释行/空行计数保持不变（t149 验收：非注释非门控行数变化为 0）。
        """
        if line:
            section.append(indent + line)

    def _merged_reuse_capture_site(self, skeleton_group: int, slot: int) -> None:
        """O3：登记一个 referent 捕获点（`ResourceZZRedirectSO_G<g>_s<k> = ref so0`）。"""
        counts = getattr(self, "_merged_reuse_captures", None)
        if counts is None:
            counts = {}
            self._merged_reuse_captures = counts
        key = (int(skeleton_group), int(slot))
        counts[key] = int(counts.get(key, 0)) + 1

    def _merged_reuse_ratio_records(self) -> list[dict]:
        """O3：导出期的「捕获 : 消费」结构比记录（供 stdout 汇总与测试读取）。"""
        consumers = getattr(self, "_merged_reuse_consumers", None) or {}
        captures = getattr(self, "_merged_reuse_captures", None) or {}
        keys = sorted(set(consumers) | set(captures))
        records = []
        for group, slot in keys:
            site_count = len(consumers.get((group, slot), []))
            capture_count = int(captures.get((group, slot), 0))
            records.append(
                {
                    "group": f"G{int(group)}",
                    "slot": int(slot),
                    "captures": capture_count,
                    "consumers": site_count,
                    "ratio": f"{site_count}:{capture_count}",
                }
            )
        return records

    @staticmethod
    def _merged_skeleton_name(skeleton_group: int, slot: int) -> str:
        """该组该槽的合并骨架资源名（骨架按槽分份，attach 只写本槽）。"""
        return f"ResourceZZMergedSkeleton_G{skeleton_group}_s{slot}"

    @staticmethod
    def _merged_redirect_so_name(skeleton_group: int, slot: int) -> str:
        """该组该槽的 SO 重定向资源名（**必须按组命名空间化**）。

        历史事故（2026-09-16，叶瞬光01 游戏内实测：模型爆炸 + 持续闪烁）：
        旧实现只按槽位命名 `ResourceZZRedirectSO_s<k>`，全 mod 一个变量，**跨组
        共享**。一个导出里有两个及以上骨架组发生重定向时，两组的 SO owner 都写
        同一个变量（`ResourceZZRedirectSO_s1 = ref so0`），**后捕获者覆盖先捕获
        者**；于是先闭合守卫的那一组会把**自己载体的合并几何写进另一组的 SO
        缓冲**——载体渲染用本组 IB 索引另一组的几何 → 顶点全部错位（爆炸），
        且每帧谁后捕获随引擎 deform 提交顺序变化 → 来回闪。
        dump 实证（FrameAnalysis-2026-09-16-141347）：G2 的重放（000065）把
        3b1b73fe 的蒙皮结果写进了 G0 载体 8c8de427 的 SO，重算蒙皮
        13671/13671 行误差 1.8e-07；而 8c8de427 自己的蒙皮结果 0 行吻合。
        因此资源名必须带上骨架组号。
        """
        return f"ResourceZZRedirectSO_G{skeleton_group}_s{slot}"

    @staticmethod
    def _merged_skin_out_name(skeleton_group: int, slot: int) -> str:
        """该组该槽的**自建**蒙皮输出资源名（合并几何的最终顶点缓冲）。

        为什么不能继续写 `ResourceZZRedirectSO_G<g>_s<k>`（= `ref so0` 别名）：
        别名要求源与目标的 bind flag 兼容，而游戏自己的 SO 只有
        `vertex_buffer shader_resource stream_output`，我们的目标却因为
        蒙皮 CS 用 `cs-u0` 写它而带上 `unordered_access` ⇒ 视图创建失败。
        帧日志铁证（FrameAnalysis-2026-10-11-003405，draw 000028）：

            src bind_flags=0x019 [vertex_buffer shader_resource stream_output]
            dst bind_flags=0x081 [vertex_buffer unordered_access]
            WARNING: referenced resource is missing bind flags required by
            destination, view creation will fail

        失败后该别名是无效资源 ⇒ 拆分绘制绑到无效 vb0 ⇒ 展示页整屏卡死。
        自建缓冲自己声明 `bind_flags = unordered_access vertex_buffer` 即可同时
        当 UAV 写与 VB 读（**且 CS 在 deform 段、VB 读在渲染段 ⇒ 不同 draw，
        不触发 D3D11「同一次 draw 输入/输出互斥」**）。
        """
        return f"ResourceZZSkinOut_G{skeleton_group}_s{slot}"

    @staticmethod
    def _merged_skin_out_row_count(group_plan: dict | None) -> int:
        """自建蒙皮输出的行数 = 各 carrier 的合并几何行数之和（与 `array` 对齐）。"""
        if not group_plan:
            return 0
        return sum(
            max(0, int(draw_count or 0))
            for _vb0, _vb2, draw_count in group_plan.get("deform_draws", [])
        )

    @staticmethod
    def _merged_attach_name(component_id: int, slot: int) -> str:
        """(部件, 槽) 的 attach CustomShader 段名。"""
        return f"CustomShaderZZMIMergedSkeletonAttach_C{component_id}_s{slot}"

    @staticmethod
    def _merged_cb1_name(skeleton_group: int) -> str:
        """该骨架组当帧对象变换的捕获资源名（各部件渲染段 `copy vs-cb1 unless_null` 写入）。

        跨组拆分绘制用它换绑 `vs-cb1`，让每个来源用自己的对象变换；因为采集与使用都发生在
        渲染段（同帧），所以没有「慢一帧」问题。
        """
        return f"ResourceZZCb1_G{int(skeleton_group)}"

    @staticmethod
    def _merged_cross_attach_name(component_id: int, host_group: int, slot: int) -> str:
        """跨组 attach 的 CustomShader 段名：(跨组件, **宿主组**, 槽)。

        刻意与 `_merged_attach_name` 分开命名：同一个部件的同一个槽可能存在两条
        性质不同的 attach（写自己组 / 写吸收它的宿主组），名字必须能区分，否则
        ini 里后者会覆盖前者（3DMigoto 同名的 `[CustomShader*]` 段只保留一条）。
        """
        return (
            f"CustomShaderZZMIMergedSkeletonAttachX_C{component_id}"
            f"_g{int(host_group)}_s{slot}"
        )

    @staticmethod
    def _merged_cross_group_enabled() -> bool:
        """跨组骨骼合并总开关（`cross_group_merged_vgmap_test`）。

        关闭时保持历史行为：跨组件骨骼只写自己组（实机上会让被吸收的顶点塌陷），
        并由 `_warn_cross_group_bone_references` 大声报警。开启后由 `_merged_cross_*`
        系列把它们的骨骼写进**吸收它的宿主组**缓冲。
        """
        return bool(_zzmi_prop_flag("cross_group_merged_vgmap_test", False))

    @staticmethod
    def _merged_cb1_capture_name(skeleton_group: int) -> str:
        """骨架组的「对象→世界 cb1」捕获资源名（F2 跨组校准用）。

        每组一份：同组部件的对象变换逐位相同（分组的定义），因此组内任一部件的
        渲染 draw 处 `copy vs-cb1` 都是同一个矩阵（last-wins 无害）。
        """
        return f"ResourceZZCb1_G{int(skeleton_group)}"

    @staticmethod
    def _merged_calibrated_attach_shader_path() -> str:
        """跨组 attach CS 的 mod 内相对路径（`[CustomShader*]` 的 cs = 值）。

        2026-10-10 定案：跨组走**拆分绘制**（每个来源用自己的 cb1 单独绘制一次），
        因此 attach 必须是**直拷**——源件骨骼留在自己的对象空间，由它自己的 cb1
        摆到世界；不需要（也不能）在 CS 里做对象空间换算。名字保留是为了不动调用点。
        """
        return "./res/zzmi_merged_skeleton_attach.hlsl"

    @staticmethod
    def _merged_skin_shader_filename() -> str:
        """合并几何蒙皮 CS 文件名（生成时复制到 Mod 的 res/）。"""
        return "zzmi_merged_skin.hlsl"

    @staticmethod
    def _merged_skin_cs_name(skeleton_group: int, slot: int, carrier_index: int = 0) -> str:
        """(组, 槽, carrier 序号) 的蒙皮 CustomShader 段名。"""
        suffix = "" if carrier_index == 0 else f"_c{carrier_index}"
        return f"CustomShaderZZMISkin_G{skeleton_group}_s{slot}{suffix}"

    @staticmethod
    def _merged_skin_dispatch_count(row_count: int, prefix_rows: int = 0) -> int:
        """蒙皮 CS 的 Dispatch 组数（64 线程/组；前缀行也由第一段 CS 写）。"""
        work = max(int(row_count or 0), int(prefix_rows or 0))
        return max(1, (work + 63) // 64)

    @staticmethod
    def _zzmi_layout_element_format(semantic, element_format) -> str:
        """元素格式的可比口径（BLENDINDICES 的 UINT/SINT 归并为 INT）。

        不同捕获路径可能把同一组 32 位骨骼索引记录成 UINT/SINT；对非负骨骼编号而言
        位宽与读取步长相同，不应因此把本来兼容的布局判成不匹配（与
        `_blend_layout_key` 同口径）。
        """
        text = str(element_format or "").upper()
        if str(semantic or "").upper() == "BLENDINDICES":
            text = text.replace("_UINT", "_INT").replace("_SINT", "_INT")
        return text

    def _merged_skin_row_layout_mismatches(
        self, kind: str, layout: dict | None, label: str
    ) -> list[str]:
        """把实际行布局与单一事实源**逐元素**核对；返回不匹配描述（空 = 匹配）。

        判据三条（都不是"只比总宽度"）：
        1. **声明 stride**（= 资源真实读取步长）必须等于该行的派生字节数
           —— 取自 `layout["stride"]`，不是 Σ 元素宽度（元数据分叉时以声明为准）；
        2. 元素列表（若元数据携带）按**顺序**逐元素比对
           （语义名 / 索引 / 格式 / 字节宽 / 偏移 / `extract_slot`）；其中
           `extract_slot` 只在**两值都非空**时按大小写归一后比对（实际值空 =
           旧缓存不带 slot 溯源 ⇒ 保守放行，与第 3 条同口径），不匹配报
           `elements[N].extract_slot`；
        3. 元素为空（旧缓存/桩缺元素表）时退化为只比声明 stride —— 与 B2 引入时的
           口径一致，并在诊断里如实标注 `elements:actual=unknown`。
        """
        expected_bytes = _zzmi_skin_row_bytes(kind)
        if not layout:
            return [f"{label}:layout-metadata-missing:expected_bytes={expected_bytes}"]
        mismatches: list[str] = []
        try:
            declared_stride = int(layout.get("stride", 0) or 0)
        except (TypeError, ValueError):
            declared_stride = 0
        if declared_stride != expected_bytes:
            mismatches.append(
                f"{label}:stride:expected={expected_bytes}:actual={declared_stride}"
            )
        elements = layout.get("elements") or []
        expected_elements = ZZMI_MERGED_SKIN_ROW_LAYOUT[kind]["elements"]
        if not elements:
            # 元数据**不携带元素表**（旧缓存 / 测试桩）⇒ 只能按声明 stride 判定，
            # 与 B2 引入时的口径一致（不额外放宽也不额外收紧）。真实工作空间
            # json 的 `CategoryBufferList` 带完整元素表（见单一事实源注释），
            # 因此生产路径走的是下面的逐元素比对。
            return mismatches
        if len(elements) != len(expected_elements):
            mismatches.append(
                f"{label}:element-count:"
                f"expected={len(expected_elements)}:actual={len(elements)}"
            )
        for index, expected in enumerate(expected_elements):
            if index >= len(elements):
                break
            actual = elements[index]
            expected_semantic = str(expected["semantic"]).upper()
            actual_semantic = str(actual.get("semantic", "") or "").upper()
            if expected_semantic != actual_semantic:
                mismatches.append(
                    f"{label}:elements[{index}].semantic:"
                    f"expected={expected_semantic}:actual={actual_semantic}"
                )
            expected_format = self._zzmi_layout_element_format(
                expected_semantic, expected["format"]
            )
            actual_format = self._zzmi_layout_element_format(
                actual_semantic, actual.get("format", "")
            )
            if expected_format != actual_format:
                mismatches.append(
                    f"{label}:elements[{index}].format:"
                    f"expected={expected_format}:actual={actual_format}"
                )
            expected_slot = str(expected.get("extract_slot", "") or "").strip().upper()
            actual_slot = str(actual.get("extract_slot", "") or "").strip().upper()
            # slot 溯源：期望侧（生成端实际绑定的 cs-t0→vb0 / cs-t1→vb2）与实际元数据
            # 两值都非空时按大小写归一比对；**实际值为空**（旧缓存不带 ExtractSlot）
            # ⇒ 保守放行，不因缺溯源信息就停发 CS（与「元素表缺失只比 stride」同口径）。
            if expected_slot and actual_slot and expected_slot != actual_slot:
                mismatches.append(
                    f"{label}:elements[{index}].extract_slot:"
                    f"expected={expected_slot}:actual={actual_slot}"
                )
            for field in ("index", "byte_width", "offset"):
                try:
                    actual_value = int(actual.get(field, -1))
                except (TypeError, ValueError):
                    actual_value = -1
                expected_value = int(expected[field])
                if expected_value != actual_value:
                    mismatches.append(
                        f"{label}:elements[{index}].{field}:"
                        f"expected={expected_value}:actual={actual_value}"
                    )
        return mismatches

    def _merged_skin_layout_mismatches(self, group_plan: dict | None) -> list[str]:
        """蒙皮 CS 两处行布局（Blend 锚点 + Position 载体/目的）的逐元素核对。

        - `cs-t1` 绑定**锚点布局**的 Blend 资源（`anchor_layout`）；
        - `cs-t0` 绑定**每个 deform_draws 条目**的 Position 资源（载体 / target 前缀）；
        - 目的侧行宽 `so_stride`（= CS 的 `w1*4`）也必须等于 Position 行字节数。
        """
        if not group_plan:
            return ["plan:missing"]
        mismatches = self._merged_skin_row_layout_mismatches(
            "blend", group_plan.get("anchor_layout"), "blend:anchor"
        )
        for draw_ib in group_plan.get("deform_draw_ibs") or ():
            position_layout = self._drawib_category_layout(str(draw_ib), "Position")
            mismatches.extend(
                self._merged_skin_row_layout_mismatches(
                    "position", position_layout, f"position:{draw_ib}"
                )
            )
        try:
            so_stride = int(group_plan.get("so_stride", 0) or 0)
        except (TypeError, ValueError):
            so_stride = 0
        if so_stride != ZZMI_MERGED_SKIN_POSITION_ROW_BYTES:
            mismatches.append(
                "dest:so_stride:"
                f"expected={ZZMI_MERGED_SKIN_POSITION_ROW_BYTES}:actual={so_stride}"
            )
        return mismatches

    def _merged_skin_anchor_blend_bytes(self, group_plan: dict | None) -> int:
        """锚点行布局的**声明 stride**（= 资源真实读取步长）；取不到按 0。

        优先取声明 stride（`anchor_layout["stride"]`）而不是 Σ 元素宽度：元数据分叉时
        以资源声明为准（诊断里报告的也必须是这个值）。
        """
        if not group_plan:
            return 0
        layout = group_plan.get("anchor_layout") or {}
        try:
            declared = int(layout.get("stride", 0) or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > 0:
            return declared
        try:
            return int(
                self._blend_layout_width(group_plan.get("anchor_layout_key")) or 0
            )
        except (TypeError, ValueError):
            return 0

    def _merged_skin_publish_supported(self, group_plan: dict | None) -> bool:
        """CS 行布局是否与 HLSL 事实一致（B2/t40 守卫，**逐元素**判据）。

        CS 的 `cs-t0`/`cs-t1` 由 HLSL 结构体决定步长（40 / 32 字节），与底层 Buffer
        的真实 stride 无关；实际行布局（资源声明 stride + 元素构成）与
        `ZZMI_MERGED_SKIN_ROW_LAYOUT` 不一致时必须**不发 CS**（否则跨行错读且不报错），
        发布改由 draw 版重放承担 —— 但该回退**只覆盖锚点布局内的必需部件**（FR-2）：
        覆盖面由 `_merged_skin_replay_coverage` 判定并写进诊断（见 `_merged_skin_layout_diag`）。
        不匹配的具体元素由 `_merged_skin_layout_mismatches` 给出。
        """
        return not self._merged_skin_layout_mismatches(group_plan)

    def _merged_skin_layout_diag(
        self, skeleton_group: int, group_plan: dict | None
    ) -> str:
        """B2/t40/FR-2：行布局不匹配时的可读诊断行（指元素 + **如实**说明发布覆盖面）。

        文案按 `_merged_skin_replay_coverage` 的覆盖面状态分档，**不再**一律声称
        「已跳过 CS、整组由 draw 版重放完整接管」——那对锚点布局之外的必需部件是过度承诺：

        - ``complete``：全部必需部件的 Blend 布局都在锚点布局内 ⇒ draw 版重放可在任意
          必需部件挂点落笔，如实写「覆盖完整（本帧无发布缺口）」；
        - ``gap``：有必需部件不在锚点布局内 ⇒ 这些部件本帧**不发布**合并几何（自身不是
          重放挂点）；仅当最后一个到达的必需部件属于锚点布局时才由它兜底，否则该槽
          本帧无人写入 ⇒ 如实写「闪烁/缺失可能复现」并给修法指引；
        - ``legacy``：锚点布局与全部必需部件的签名都不一致 ⇒ 计划按旧行为把全部必需
          部件都当挂点，不写「全部都在锚点布局内」，改为给复核指引；
        - ``unknown``：计划缺必需部件集合 / 锚点布局签名 ⇒ 不给出任何覆盖面结论。
        """
        mismatches = self._merged_skin_layout_mismatches(group_plan)
        shown = mismatches[:4]
        detail = ",".join(shown)
        if len(mismatches) > len(shown):
            detail += f",…(+{len(mismatches) - len(shown)})"
        header = (
            "蒙皮 CS 的行布局与 HLSL 结构体不一致（cs-t0=40B Position / "
            "cs-t1=32B Blend）⇒ 已跳过 CS 发布；"
        )
        blocked_ibs, coverage = self._merged_skin_replay_coverage(
            skeleton_group, group_plan
        )
        if coverage == "unknown":
            body = (
                "计划缺必需部件集合 / 锚点布局签名，发布覆盖面无法判定，"
                "请按计划书 §7 修复链 10 复核本组必需部件的发布者。"
            )
            blocked_field = "unknown"
            publish_gap_field = "unknown"
        elif coverage == "legacy":
            body = (
                "锚点布局与全部必需部件的布局签名都不一致（布局元数据不完整或组内只有"
                "单一布局）⇒ 计划已按旧行为把全部必需部件都当重放挂点，覆盖面不能按"
                "锚点布局声明，请按计划书 §7 修复链 10 复核本组必需部件的发布者。"
            )
            blocked_field = "/".join(blocked_ibs)
            publish_gap_field = "unknown"
        elif blocked_ibs:
            body = (
                f"本组有 {len(blocked_ibs)} 个必需部件的 Blend 布局不在锚点布局内"
                f"（{'/'.join(blocked_ibs)}）：draw 版重放只能在锚点布局的挂点落笔，"
                "这些部件本帧不发布合并几何（自身不是重放挂点）；仅当最后一个到达的"
                "必需部件属于锚点布局时，才由它的挂点兜底写全该槽，否则该槽 SO 本帧"
                "可能不写 ⇒ 闪烁/缺失可能复现（即计划书 §7 修复链 10 的症状）。"
                "处置：清理工作空间 Blend/VGMap 缓存后重新导入，让锚点布局回到 "
                "32B Blend / 40B Position；或把合并后的物体挂到组内该布局占多数的"
                "部件上重新导出。"
            )
            blocked_field = "/".join(blocked_ibs)
            publish_gap_field = "1"
        else:
            body = (
                "本组全部必需部件的 Blend 布局都在锚点布局内 ⇒ draw 版重放覆盖完整"
                "（本帧无发布缺口）。"
            )
            blocked_field = "-"
            publish_gap_field = "0"
        return self._merged_diag(
            ZZMI_MERGE_DIAG_SKIN_LAYOUT_UNSUPPORTED,
            header + body,
            group=f"G{int(skeleton_group)}",
            anchor_blend_bytes=self._merged_skin_anchor_blend_bytes(group_plan),
            required_blend_bytes=ZZMI_MERGED_SKIN_BLEND_ROW_BYTES,
            mismatch=detail or "-",
            replay_coverage=coverage,
            blocked_required=blocked_field,
            publish_gap=publish_gap_field,
        )

    def _merged_skin_replay_coverage(
        self, skeleton_group: int, group_plan: dict | None
    ) -> tuple[list[str], str]:
        """draw 版重放对「本组必需部件」的覆盖面 —— FR-2 判定的**单一来源**。

        返回 ``(不在锚点布局内的必需部件 draw_ib 升序列表, 状态)``，状态取值：

        - ``"complete"``：全部必需部件的 Blend 布局签名 == 锚点布局 ⇒ 每个必需部件都是
          重放挂点候选，而 `seen` 在帧内 sticky ⇒ 最后一个到达的必需部件必然可落笔，
          本帧无发布缺口；
        - ``"gap"``：存在必需部件的签名 ≠ 锚点布局（即落在
          `compatible_component_ids` 之外）⇒ 这些部件自身不是挂点，只有「最后一个到达
          的必需部件恰在锚点布局内」时才由它兜底写全该槽；
        - ``"legacy"``：锚点布局与**全部**必需部件的签名都不一致 ⇒ 计划已按旧行为把
          全部必需部件都当挂点（`compatible_component_ids` 的空集回退 = required，
          见 `_build_merged_mesh_redirect_plan`），覆盖面不能按锚点布局声明；
        - ``"unknown"``：缺必需部件集合，或既无 `anchor_layout_key` 也无
          `compatible_component_ids` ⇒ 无法判定。

        判据与计划构建**同源**：签名 = `_drawib_blend_layout_signature`（元素级 + 资源
        声明 stride），锚点 = 计划里的 `anchor_layout_key`（`_build_merged_mesh_redirect_plan`
        选出的部件数最多、同数取最宽的布局）。只有拿不到 `anchor_layout_key` 的旧计划
        才退化为「已存 `compatible_component_ids` 集合差」，此时无法区分 complete 与 legacy。
        """
        if not group_plan:
            return [], "unknown"
        raw_ids = group_plan.get("required_component_ids")
        if raw_ids is None:
            raw_ids = self._merged_group_component_ids(skeleton_group)
        required_ids: list[int] = []
        for raw_id in raw_ids or ():
            try:
                component_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if 0 <= component_id < len(self.merged_skeleton_components):
                if component_id not in required_ids:
                    required_ids.append(component_id)
        if not required_ids:
            return [], "unknown"
        anchor_key = group_plan.get("anchor_layout_key")
        compatible_raw = group_plan.get("compatible_component_ids")
        if anchor_key is None:
            if compatible_raw is None:
                return [], "unknown"
            compatible = {int(cid) for cid in compatible_raw}
            blocked = {
                str(self.merged_skeleton_components[component_id]["draw_ib"])
                for component_id in required_ids
                if component_id not in compatible
            }
            return sorted(blocked), ("gap" if blocked else "complete")
        blocked: set[str] = set()
        compared = 0
        for component_id in required_ids:
            compared += 1
            draw_ib = str(self.merged_skeleton_components[component_id]["draw_ib"])
            if self._drawib_blend_layout_signature(draw_ib) == anchor_key:
                continue
            blocked.add(draw_ib)
        if not blocked:
            return [], "complete"
        if (
            compatible_raw is not None
            and len(blocked) == compared
            and {int(cid) for cid in compatible_raw} == set(required_ids)
        ):
            # 全部必需部件都不在锚点布局内，且计划仍把它们全当挂点 ⇒ 命中的是
            # `compatible_component_ids` 的空集回退（旧行为），不是「有窄布局部件被挡」。
            return sorted(blocked), "legacy"
        return sorted(blocked), "gap"

    def _merged_group_redirect_plan(self, skeleton_group: int) -> dict | None:
        """本骨架组的重定向计划（无则 None）：按 target 的组件所属组匹配。"""
        for target_ib, plan in self._redirect_target_map.items():
            target_cid = plan.get("target_component_id")
            if target_cid is None:
                target_cid = self.merged_skeleton_component_id_dict.get(str(target_ib))
            if target_cid is None:
                continue
            target_cid = int(target_cid)
            if not 0 <= target_cid < len(self.merged_skeleton_components):
                continue
            if (
                int(self.merged_skeleton_components[target_cid]["skeleton_group"])
                == int(skeleton_group)
            ):
                return plan
        return None

    def _append_merged_skin_publish_block(
        self,
        section,
        skeleton_group: int,
        group_plan: dict | None,
        consumer_ib: str = "",
    ) -> None:
        """每槽守卫内发布合并几何（蒙皮 CS，绕开 IA 布局 → 任意必需部件可发布）。

        与 draw 版重放共用同一套门控（SO 别名当帧就绪 + 按槽期望集合），
        因此**所有**必需部件（含 Blend 布局与锚点不一致、完全不能重放 draw 的
        窄布局部件）都能在自己那段把 SO 写全；CS 按索引写 = 幂等，帧内最后一次
        派发（最后一个必需部件到达处）即最终内容。draw 版重放保留为兜底。

        **B2/t40 守卫（前置条件）**：CS 只有一种行布局（`ZZMI_MERGED_SKIN_ROW_LAYOUT`：
        cs-t0 = 40B Position / cs-t1 = 32B Blend）。锚点行布局与之不一致时本块整体不发射
        （`_merged_skin_publish_supported` 为假 ⇒ 落一行 SKIN_LAYOUT_UNSUPPORTED 诊断后
        return）——否则 CS 会跨行错读权重/索引且不报错。

        ⚠️ **守卫拦下后的 draw 版重放回退不是无损的**（FR-2）：draw 版重放只能在
        `compatible_component_ids`（Blend 布局 == 锚点布局 `anchor_layout_key`）的挂点落笔，
        而本块存在的意义正是替「布局与锚点不一致、完全不能重放 draw」的必需部件发布。
        故本块被守卫拦下时，那些部件本帧**没有自己的发布点**；只有当最后一个到达的必需
        部件恰在锚点布局内时，才由它的挂点兜底写全该槽，否则该槽 SO 本帧可能不写
        （闪烁/缺失）。诊断文案据此如实分档（见 `_merged_skin_replay_coverage` /
        `_merged_skin_layout_diag`，四态 complete / gap / legacy / unknown）。

        形式选 `if <守卫>` + 直接 `run = CustomShader...`：本 fork 支持 if 内
        跑 CustomShader（`Core/ZZMI/Libraries/HP bar/hp.ini` 在嵌套 if 内
        `run = CustomShader.OrderIds`，即官方库自身的用法）。
        `so0 = null` 先解绑流输出目标：同一帧内该缓冲还被游戏当 SO 目标绑着，
        同时当 UAV 写会撞 D3D11 的输入/输出互斥。
        """
        if not group_plan:
            return
        if not self._merged_skin_publish_supported(group_plan):
            # B2：锚点行布局 ≠ CS 写死的行布局 ⇒ **不发 CS**（发了就是静默错读
            # 权重/索引）。发布改由 draw 版重放承担，但它**只覆盖锚点布局内的必需
            # 部件**（FR-2）：锚点布局之外的必需部件本帧没有任何发布者 ——
            # 覆盖面由 `_merged_skin_replay_coverage` 如实分档进诊断行。
            self._append_merged_diag(
                section, self._merged_skin_layout_diag(skeleton_group, group_plan)
            )
            return
        slots = self._merged_skeleton_slots()
        guard_component_ids = group_plan.get("required_component_ids") or (
            self._merged_group_component_ids(skeleton_group)
        )
        deform_draws = [
            (int(draw_count or 0))
            for _vb0, _vb2, draw_count in group_plan.get("deform_draws", [])
        ]
        for slot in slots:
            # O3：消费点标记（注释行；块体与守卫条件一字不改 ⇒ 逐字节不变量）
            self._append_merged_diag(
                section,
                self._merged_reuse_site(
                    consumer_ib or "-", skeleton_group, slot, "publish-cs"
                ),
                indent="    ",
            )
            section.append(
                f"if {self._merged_slot_guard_condition(guard_component_ids, skeleton_group, slot)}"
            )
            section.append("    so0 = null")
            for carrier_index, draw_count in enumerate(deform_draws):
                if draw_count <= 0:
                    continue
                section.append(
                    "    run = "
                    + self._merged_skin_cs_name(skeleton_group, slot, carrier_index)
                )
            section.append("endif")

    def _append_merged_skin_cs_sections(
        self,
        section,
        skeleton_group: int,
        group_plan: dict | None,
        slots,
        use_skin_out: bool = False,
    ) -> None:
        """发一组蒙皮 CS 段定义（重定向计划与跨组发布计划共用）。

        段体与历史逐字一致；抽成方法只是为了让「跨组吸收宿主组」（F3-CS，
        `_merged_cross_group_publish_plan`）能复用同一套段定义 —— 它没有重定向
        计划，却必须能在最后一个供骨件到达处重发布合并几何。

        `use_skin_out=True`（仅跨组发布计划）把 UAV 写目标从
        `ResourceZZRedirectSO_*`（= `ref so0` 别名）换成**自建**的
        `ResourceZZSkinOut_*`：跨组路径下这个别名会被拆分绘制当 `vb0` 读，
        于是目标带上 `unordered_access vertex_buffer`，而游戏 SO 只有
        `vertex_buffer shader_resource stream_output` ⇒ 视图创建失败 ⇒
        绑定它的绘制全部无效（展示页整屏卡死）。重定向路径不读该别名当 VB，
        保持原样以免动到已投产行为。
        """
        if not group_plan:
            return
        if not self._merged_skin_publish_supported(group_plan):
            # B2：锚点行布局 ≠ CS 写死的行布局 ⇒ 不发 CS 段定义（发布点也已
            # 按同一守卫跳过，产物里不会留下任何 CS 引用）。诊断在此也留一条，
            # 与 `_append_merged_skin_publish_block` 的同一记录去重后只打一行。
            self._append_merged_diag(
                section,
                self._merged_skin_layout_diag(skeleton_group, group_plan),
            )
            return
        so_prefix_rows = int(group_plan.get("so_prefix_rows", 0) or 0)
        # 每行 float 数 = SO 行 stride / 4（Position 类目 stride = SO override_byte_stride）。
        # 2026-09-22：skin CS 已改为从 src_rows 的 stride 自行推算行宽，这里
        # 仍照旧写 w1 只为保持 ini 产物形状稳定（shader 不再读取该参数）。
        row_stride = int(group_plan.get("so_stride", 40) or 40)
        row_floats = max(1, row_stride // 4)
        dest_start = so_prefix_rows
        for carrier_index, (vb0_resource, vb2_resource, draw_count) in enumerate(
            group_plan.get("deform_draws", [])
        ):
            if int(draw_count or 0) <= 0:
                continue
            for slot in slots:
                cs_name = self._merged_skin_cs_name(skeleton_group, slot, carrier_index)
                section.append(f"[{cs_name}]")
                section.append(
                    "flags = optimization_level3 all_resources_bound skip_validation"
                )
                section.append(f"cs = ./res/{self._merged_skin_shader_filename()}")
                # x1 有双重身份，**不要删除、不要改语义**：
                # ① 本 carrier 的合并几何行数（导出侧口径）；
                # ② skin CS 判定 ini 参数纹理布局（fork 相关：本 fork 从 [1] 起、
                #    标准版从 [0] 起）的**选组判据** —— shader 要求 x1 等于
                #    src_rows 的元素数（cs-t0 绑的就是本 carrier 的 Position，
                #    二者同源），据此挑出本次运行真正生效的那一组参数。
                # 缺了它，换一套 3DMigoto 运行时整组参数读成 0 → 蒙皮不写。
                section.append(f"x1 = {int(draw_count)}")
                section.append(f"y1 = {int(dest_start)}")
                section.append(
                    f"z1 = {int(so_prefix_rows if carrier_index == 0 else 0)}"
                )
                section.append(f"w1 = {int(row_floats)}")
                section.append(f"cs-t0 = ref {vb0_resource}")
                section.append(f"cs-t1 = ref {vb2_resource}")
                section.append(
                    "cs-t2 = ref " + self._merged_skeleton_name(skeleton_group, slot)
                )
                section.append(
                    "cs-u0 = ref "
                    + (
                        self._merged_skin_out_name(skeleton_group, slot)
                        if use_skin_out
                        else self._merged_redirect_so_name(skeleton_group, slot)
                    )
                )
                section.append(
                    "Dispatch = "
                    + str(
                        self._merged_skin_dispatch_count(
                            int(draw_count),
                            int(so_prefix_rows if carrier_index == 0 else 0),
                        )
                    )
                    + ", 1, 1"
                )
                section.append("cs-u0 = null")
                section.new_line()
            dest_start += int(draw_count)

    def _merged_split_group_plan(self) -> dict:
        """参与「按组拆分绘制」的骨架组 → ``(宿主组, 宿主发布计划, 宿主 DrawIB)``。

        合并物体由多个来源部件组成，每个来源部件属于一个骨架组。拆分绘制按组切段，
        再把每一段**放回该组自己的渲染段**里画 —— 那里的 `vs-cb1` 必然是当帧的，
        所以帧内绘制顺序不再影响正确性。旧做法把全部段塞进宿主段，跨组 cb1 在宿主段
        执行时可能还是上一帧的值 ⇒ 影子/镜像物体。
        """
        cached = getattr(self, "_merged_split_group_cache", None)
        if cached is not None:
            return cached
        plan_map: dict = {}
        if self.has_merged_skeleton and self._merged_cross_group_enabled():
            for skeleton_group in self._merged_skeleton_groups():
                cross_component_ids = self._merged_cross_group_component_ids(
                    skeleton_group
                )
                if not cross_component_ids:
                    continue
                group_plan = self._merged_cross_group_publish_plan(
                    skeleton_group
                ) or self._merged_group_redirect_plan(skeleton_group)
                if not group_plan:
                    continue
                host_group = int(skeleton_group)
                host_ib = str(group_plan.get("target_ib", ""))
                plan_map[host_group] = (host_group, group_plan, host_ib)
                for component_id in cross_component_ids:
                    index = int(component_id)
                    if not (0 <= index < len(self.merged_skeleton_components)):
                        continue
                    source_group = int(
                        self.merged_skeleton_components[index].get(
                            "skeleton_group", host_group
                        )
                    )
                    if source_group == host_group:
                        continue
                    plan_map[source_group] = (host_group, group_plan, host_ib)
        self._merged_split_group_cache = plan_map
        return plan_map

    def _merged_split_representative(
        self, skeleton_group, host_group, host_ib
    ) -> str:
        """该骨架组的**代表段** DrawIB：一组只发一次拆分绘制，避免重复绘制。

        代表段必须同时满足两件事，否则那一段绘制拿不到当帧对象变换：

        1. 它是**这个组的组件**（`merged_skeleton_components` 里 `skeleton_group` 相同）；
        2. 它的渲染段里**有 cb1 捕获** —— `= copy vs-cb1 unless_null` 只在
           `merged_skeleton_component_id_dict` 登记过的 DrawIB 的段里发射。

        第 2 条容易踩坑：同一骨架组可能有多个 DrawIB（例如 `3f797188` 的两个子网格），
        但只有登记为组件的那个才有捕获。挑错代表段 ⇒ 该段绑的 `ResourceZZCb1_G<N>`
        永远是空值或上一帧的值。
        """
        if int(skeleton_group) == int(host_group):
            return str(host_ib)
        best_ib = ""
        best_score = -1
        for component in self.merged_skeleton_components:
            if int(component.get("skeleton_group", 0)) != int(skeleton_group):
                continue
            draw_ib = str(component.get("draw_ib"))
            if draw_ib not in self.merged_skeleton_component_id_dict:
                continue
            score = int(component.get("vg_offset", 0)) + int(
                component.get("vg_count", 0)
            )
            if score > best_score:
                best_score = score
                best_ib = draw_ib
        if best_ib:
            return best_ib
        # 兜底：没有任何登记捕获的组件时退回本组任意组件，至少不丢几何。
        for component in self.merged_skeleton_components:
            if int(component.get("skeleton_group", 0)) == int(skeleton_group):
                return str(component.get("draw_ib"))
        return ""

    def _merged_split_own_group(self, draw_ib):
        """这个 DrawIB 的渲染段要画哪一组的拆分绘制；不参与时返回 ``None``。"""
        plan_map = self._merged_split_group_plan()
        for skeleton_group, (host_group, _plan, host_ib) in plan_map.items():
            representative = self._merged_split_representative(
                skeleton_group, host_group, host_ib
            )
            if str(representative) == str(draw_ib):
                return int(skeleton_group)
        return None

    def _merged_split_draws_target(self, draw_ib) -> bool:
        """这个 DrawIB 是否会走「按来源拆分绘制」（判定口径与发射器完全一致）。

        调用方需要在**发射拆分绘制之前**知道答案：拆分绘制必须继承合并物体那行
        `; [mesh:<名>] [vertex_count:<N>]` 标注 —— 拖拽交互（
        `blueprint/node_postprocess_draginteraction.py`）正是靠这行注释把绘制与
        物体配对，注释丢了整个物体的拖拽钩子就跟着丢。
        """
        return self._merged_split_own_group(draw_ib) is not None

    def _merged_index_unique_str(self, draw_ib) -> str:
        """合并索引缓冲的 unique_str（``LOD0.<IB>-<count>-<start>``）；读不到返回空串。

        资源名规则：``"Resource_" + unique_str.replace("-", "_") + "_Index"``
        （与 `ui/universal/yysls.py:19` 同型），产物里就是
        `Resource_LOD0.d5e1e7ff_61305_0_Index`。非宿主段要自己绑合并 IB，必须能
        从产物文件名反推这个名字。
        """
        meshes_dir = os.path.join(
            GlobalConfig.path_generate_mod_folder(), "Meshes"
        )
        candidates = []
        try:
            for name in os.listdir(meshes_dir):
                if (
                    name.startswith("LOD")
                    and str(draw_ib) in name
                    and name.endswith("-Index.buf")
                ):
                    candidates.append(name)
        except OSError:
            return ""
        if not candidates:
            return ""
        best = max(
            candidates,
            key=lambda item: os.path.getsize(os.path.join(meshes_dir, item)),
        )
        return best[: -len("-Index.buf")]

    def _merged_split_mesh_comment(self, host_ib) -> str:
        """拆分绘制用的 mesh 标注：**所有来源段统一写合并物体（宿主）的名字**。

        拖拽交互（`blueprint/node_postprocess_draginteraction.py`）靠
        `; [mesh:<名>]` 与其后的 `drawindexed` 配对，并把 `$ssmtdrag_objvis_*` 前置到那条
        绘制上。合并后的物体在游戏里是**一个**物体，所以各来源段必须共用同一个名字，
        否则拖拽节点会把它们当成三个不同物体（显隐/形态键只作用在其中一个上）。

        取宿主 DrawIB 里顶点数最多的那个子网格的首个 drawcall（= 合并网格本身）的名字。
        读不到时返回空串，调用方退回本段自己的名字。
        """
        for drawib_model in self.drawib_model_list:
            if str(getattr(drawib_model, "draw_ib", "")) != str(host_ib):
                continue
            best_submesh = None
            best_count = -1
            for submesh_model in getattr(drawib_model, "submesh_model_list", []) or []:
                count = int(getattr(submesh_model, "vertex_count", 0) or 0)
                if count > best_count:
                    best_count = count
                    best_submesh = submesh_model
            if best_submesh is None:
                continue
            drawcalls = getattr(best_submesh, "drawcall_model_list", []) or []
            if not drawcalls:
                continue
            name = str(getattr(drawcalls[0], "obj_name", "") or "")
            if not name:
                continue
            return (
                f"; [mesh:{name}]"
                f" [vertex_count:{int(getattr(drawcalls[0], 'vertex_count', 0) or 0)}]"
            )
        return ""

    def _merged_source_index_runs(self, draw_ib) -> list[tuple[int, int, int]]:
        """把合并网格的索引按**来源骨架组**切成连续段：``[(skeleton_group, start, count)]``。

        判定用**骨索引值域**（而不是顶点行区间）：合并后的行序由外部 join 决定、不可预测，
        但骨索引是导出侧自己写进各组件 ``vg_map`` 的，稳定可靠。逐顶点取 Blend 里权重最大
        的那个骨索引，查它落在哪个组件的 ``vg_map`` 值域里，再按三角形把索引切成连续段。

        读不到合并 Blend/Index（例如 ini 先于缓冲落盘）时返回 ``[]``，调用方退回单次绘制。
        """
        import struct as _struct  # 与本文件其它处一致：局部导入
        meshes_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes")
        blend_path = os.path.join(meshes_dir, str(draw_ib) + "-Blend.buf")
        index_candidates = []
        try:
            for name in os.listdir(meshes_dir):
                if name.startswith("LOD") and str(draw_ib) in name and name.endswith("-Index.buf"):
                    index_candidates.append(os.path.join(meshes_dir, name))
        except OSError:
            return []
        if not os.path.exists(blend_path) or not index_candidates:
            return []
        # 合并索引：取体积最大的那个（同一 DrawIB 可能还有空占位 IB）
        index_path = max(index_candidates, key=lambda p: os.path.getsize(p))
        try:
            with open(blend_path, "rb") as handle:
                blend_data = handle.read()
            with open(index_path, "rb") as handle:
                index_data = handle.read()
        except OSError:
            return []
        row_count = len(blend_data) // 32
        if row_count <= 0 or len(index_data) < 12:
            return []
        index_count = len(index_data) // 4
        # 各组件的 vg_map 值域 → 骨架组
        ranges: list[tuple[int, int, int]] = []
        for component in self.merged_skeleton_components:
            vg_map = component.get("vg_map") or {}
            values = [int(v) for v in vg_map.values()]
            if not values:
                continue
            ranges.append((min(values), max(values), int(component.get("skeleton_group", 0))))
        if not ranges:
            return []
        owner_of_row: list[int] = []
        for row in range(row_count):
            offset = row * 32
            weights = _struct.unpack_from("<4f", blend_data, offset)
            indices = _struct.unpack_from("<4I", blend_data, offset + 16)
            best_weight = -1.0
            best_bone = int(indices[0])
            for slot_index in range(4):
                weight = float(weights[slot_index])
                if weight > best_weight:
                    best_weight = weight
                    best_bone = int(indices[slot_index])
            owner = -1
            for low, high, group in ranges:
                if low <= best_bone <= high:
                    owner = group
                    break
            owner_of_row.append(owner)
        runs: list[tuple[int, int, int]] = []
        for triangle_start in range(0, index_count - 2, 3):
            vertex = _struct.unpack_from("<I", index_data, triangle_start * 4)[0]
            owner = owner_of_row[vertex] if vertex < row_count else -1
            if runs and runs[-1][0] == owner:
                group, start, count = runs[-1]
                runs[-1] = (group, start, count + 3)
            else:
                runs.append((owner, triangle_start, 3))
        return runs

    def _append_merged_render_split_draws(self, section, draw_ib) -> bool:
        """跨组拆分绘制：合并网格保持一份，但**按来源分成多次 `drawindexed`**，
        每次绘制前换 `vs-cb1`，让每个来源用自己的对象变换。

        为什么必须拆：对象变换 cb1 是**每次绘制一份**的常量（464 B，前 16 float = 3×4
        对象→世界矩阵），顶点没有索引它的机制；而合并后的网格里有两群顶点属于**不同的
        对象空间**（跨组件的骨骼按直拷留在各自的对象空间）。一个 draw 只能有一个 cb1
        ⇒ 只能拆绘制。骨骼矩阵仍然全部合并在**一份**骨架缓冲里（一个物体用所有 IB 的
        骨骼矩阵这一点不变），拆的只是绘制调用。

        跨组段用的 `ResourceZZCb1_G<组>` 由各部件自己的渲染段 `copy vs-cb1 unless_null`
        采集（同帧），因此没有「慢一帧」问题 —— 这也是它优于渲染段重算（F7）的地方。

        返回 ``True`` 表示已经发过拆分绘制 ⇒ 调用方必须**跳过**原来的单次合并绘制，
        否则合并网格会被画两遍（第二遍用错的 cb1）。
        """
        plan_map = self._merged_split_group_plan()
        if not plan_map:
            return False
        emitted = getattr(self, "_merged_render_split_emitted", None)
        if emitted is None:
            emitted = set()
            self._merged_render_split_emitted = emitted
        if draw_ib in emitted:
            return False
        for skeleton_group, (host_group, _group_plan, host_ib) in plan_map.items():
            representative = self._merged_split_representative(
                skeleton_group, host_group, host_ib
            )
            if str(representative) != str(draw_ib):
                continue
            emitted.add(draw_ib)
            all_runs = self._merged_source_index_runs(str(host_ib))
            if not all_runs:
                self._append_merged_diag(
                    section,
                    self._merged_diag(
                        ZZMI_MERGE_DIAG_CROSS_GROUP_ATTACH,
                        "合并网格索引分段不可用（读不到 Blend/Index）⇒ 退回单次绘制",
                        draw_ib=str(draw_ib),
                    ),
                )
                return False
            # 只发本组自己的段。归属判定不出来（-1）的段按宿主组处理：宿主组是唯一
            # 保证存在 cb1 捕获的组，退化成「用宿主对象变换画」而不是绑一个不存在的
            # ResourceZZCb1_G-1。
            own_runs = []
            for owner_group, run_start, run_count in all_runs:
                resolved_group = (
                    int(owner_group)
                    if int(owner_group) >= 0
                    else int(host_group)
                )
                if resolved_group == int(skeleton_group):
                    own_runs.append((int(run_start), int(run_count)))
            if not own_runs:
                return False
            section.new_line()
            # 本组 cb1 就地捕获：必须与拆分绘制**同段**，否则「捕获在第一个子网格段、
            # 拆分绘制在另一个子网格段」时（实测：`3f797188-102-1236` 画、
            # `3f797188-1236-0` 捕获）读到的是上一帧对象变换。同段捕获 ⇒ 当帧。
            # last-wins：与首个子网格处那次捕获同值（同组各部件对象变换逐位相同）。
            section.append(
                self._merged_cb1_capture_name(int(skeleton_group))
                + " = copy vs-cb1 unless_null"
            )
            if int(skeleton_group) != int(host_group):
                # 非宿主段：合并 IB / 合并 Texcoord / 合并顶点缓冲都得自己绑 ——
                # 这些段原本绑的是自己的空占位缓冲，直接画什么都画不出来。
                # 顶点缓冲用**自建**的 `ResourceZZSkinOut_G<宿主>_s<槽>`（蒙皮 CS
                # 的输出）：不能用 `ResourceZZRedirectSO_*`（= `ref so0` 别名），
                # 那条别名因 bind flag 不兼容而创建失败（见 `_merged_skin_out_name`）。
                unique_str = self._merged_index_unique_str(str(host_ib))
                if not unique_str:
                    return False
                section.append(
                    "ib = Resource_" + unique_str.replace("-", "_") + "_Index"
                )
                section.append("vb1 = Resource" + str(host_ib) + "Texcoord")
                host_component_id = int(
                    self.merged_skeleton_component_id_dict.get(str(host_ib), -1)
                )
                if host_component_id < 0:
                    return False
                section.append("if $zz_ms_occ_%d == 2" % host_component_id)
                section.append(
                    "    vb0 = " + self._merged_skin_out_name(host_group, 2)
                )
                section.append("else")
                section.append(
                    "    vb0 = " + self._merged_skin_out_name(host_group, 1)
                )
                section.append("endif")
            # 本段内全是同一个组 ⇒ cb1 只换绑一次。
            section.append("vs-cb1 = " + self._merged_cb1_name(int(skeleton_group)))
            for run_start, run_count in own_runs:
                if run_count <= 0:
                    continue
                section.append(
                    "drawindexed = %d, %d, 0" % (run_count, run_start)
                )
            return True
        return False

    def _merged_pose_key_pool_sizes(self, groups) -> dict[int, int]:
        """每组「键 → 槽位」池的容量：``连通分量数 × 槽位数 × 实例上界（2）``。

        键是**逐字节精确**的：同一连通分量的同实例必然算出同一个键（t73 实测
        同实例跨部件逐位相同），不同连通分量各用**自己的**通道骨（分量之间本来
        就不共骨）⇒ 一个组一帧最多 `分量数 × 2` 个不同键。池按此定容，既不会
        同帧 FIFO 淘汰同帧键，也不再是无依据的写死 16。
        """
        sizes: dict[int, int] = {}
        for skeleton_group in groups:
            group_components = [
                self.merged_skeleton_components[component_id]
                for component_id in self._merged_group_component_ids(skeleton_group)
            ]
            if not self._merged_group_has_key_driven(skeleton_group):
                continue
            partitions = _shared_bone_components(group_components)
            channel_partitions = [
                members
                for members in partitions
                if any(
                    self._merged_component_is_key_driven(group_components[index])
                    for index in members
                )
            ]
            sizes[int(skeleton_group)] = max(
                len(self._merged_skeleton_slots()),
                len(channel_partitions) * len(self._merged_skeleton_slots()) * 2,
            )
        return sizes

    def _merged_group_component_ids(self, skeleton_group: int) -> list[int]:
        """本骨架组包含的组件号列表（升序；与 merged_skeleton_components 同序）。

        v9：本组**全部**部件的 seen 标记都要参与每个槽守卫的条件——「组内部件
        当帧全部到达（或按上一帧预测本槽不会到）」才允许消费该槽骨架。
        仅用于**直连路径**与「取不到 required 集合」的兜底；重定向路径用
        `required_component_ids`（见 `_merged_slot_seen_condition`）。
        """
        return [
            int(component_id)
            for component_id, component in enumerate(self.merged_skeleton_components)
            if int(component["skeleton_group"]) == int(skeleton_group)
        ]

    def _merged_referenced_bone_ids(self, draw_ib: str) -> set[int]:
        """部件（DrawIB）在源对象里**实际引用**的骨骼 id 集合（导出期记忆化）。

        `_collect_drawib_referenced_bone_ids` 要遍历该 DrawIB 全部源对象的顶点组
        与顶点，而跨组判定会在「组 × 部件」上反复询问同一结果（本组合集、异组
        反查、诊断各一次），必须记忆化，否则导出耗时随部件数成倍增长。
        """
        cache = getattr(self, "_merged_referenced_bone_ids_cache", None)
        if cache is None:
            cache = {}
            self._merged_referenced_bone_ids_cache = cache
        key = str(draw_ib)
        if key not in cache:
            cache[key] = set(self._collect_drawib_referenced_bone_ids(key))
        return cache[key]

    def _merged_cross_group_component_ids(self, skeleton_group: int) -> list[int]:
        """本组几何里**引用到的骨骼归属在别的骨架组**的组件号（升序）。

        实测场景（`洛克茜` 复现件，2026-10-10）：G0 的 `4e6d8882-1704-0`（骨 id
        {1,2}）被合进 G2 宿主 `4e6e989e` 的 `4e6e989e-11700-0` 网格（合并 Blend
        4562 行，其中异组 624 行、位置 [3066,3689]），而绘制合并几何的 deform VS
        绑的是**宿主组**缓冲 ⇒ 必须在宿主组缓冲里把 {1,2} 写出来。

        判据与 `_merged_component_geometry_absorbed` 同源（权威口径）：
        `(本组各部件引用的骨骼 id 并集) − 本组合法槽位` 非空 ⇒ 存在跨组吸收；
        再用每个组件的 `[vg_offset, vg_offset+vg_count)` 反查骨骼归属组件。
        关闭开关时返回空列表（保持历史行为，由守卫报警）。
        """
        if not self._merged_cross_group_enabled():
            return []
        member_ids = [int(cid) for cid in self._merged_group_component_ids(skeleton_group)]
        legal: set[int] = set()
        referenced: set[int] = set()
        for component_id in member_ids:
            component = self.merged_skeleton_components[component_id]
            offset = int(component["vg_offset"])
            legal.update(range(offset, offset + int(component["vg_count"])))
            referenced |= self._merged_referenced_bone_ids(component["draw_ib"])
        offending = referenced - legal
        if not offending:
            return []
        member_set = set(member_ids)
        owners: list[int] = []
        for component_id, component in enumerate(self.merged_skeleton_components):
            if component_id in member_set:
                continue
            offset = int(component["vg_offset"])
            if any(
                bone_id in offending
                for bone_id in range(offset, offset + int(component["vg_count"]))
            ):
                owners.append(int(component_id))
        return owners

    def _merged_cross_host_group_map(self) -> dict[int, list[int]]:
        """跨组供骨件 → 吸收它的宿主骨架组列表（升序；开关关闭时恒空）。

        `_merged_cross_group_component_ids` 的反向索引：后者按「宿主组」回答「本组
        几何引用了哪些别组部件」，这里按「部件」回答「我被哪些宿主组吸收了」——
        生成跨组件自己的 deform 段时需要它（跨组 attach 的 `run` 与合并几何重放的
        落点，见 `_append_merged_cross_group_replays`）。
        """
        cache = getattr(self, "_merged_cross_host_group_cache", None)
        if cache is not None:
            return cache
        cache: dict[int, list[int]] = {}
        for group in self._merged_skeleton_groups():
            for component_id in self._merged_cross_group_component_ids(int(group)):
                cache.setdefault(int(component_id), []).append(int(group))
        for component_id in list(cache):
            cache[component_id] = sorted(set(cache[component_id]))
        self._merged_cross_host_group_cache = cache
        return cache

    def _merged_cross_group_offending_bone_ids(self, skeleton_group: int) -> set[int]:
        """本组几何引用、归属却在别组的骨骼 id（诊断用；口径同上）。"""
        member_ids = [int(cid) for cid in self._merged_group_component_ids(skeleton_group)]
        legal: set[int] = set()
        referenced: set[int] = set()
        for component_id in member_ids:
            component = self.merged_skeleton_components[component_id]
            offset = int(component["vg_offset"])
            legal.update(range(offset, offset + int(component["vg_count"])))
            referenced |= self._merged_referenced_bone_ids(component["draw_ib"])
        return referenced - legal

    def _merged_cross_group_host_component_ids(self, skeleton_group: int) -> set[int]:
        """本组里**几何吸收了别组部件**的组件号（F3-CS；开关关闭时恒空）。

        与 `_merged_group_absorbed_hosts` 的区别：后者只认**组内**吸收（`legal`
        判据限本组合法槽位），跨组引用根本不在它的判定范围内；这里专门回答
        「本组哪个部件的导出网格里混进了别组顶点」——它就是合并几何的**宿主**，
        其 SO 引用必须被捕获，跨组重发布（蒙皮 CS）才有落点。
        """
        if not self._merged_cross_group_enabled():
            return set()
        offending = self._merged_cross_group_offending_bone_ids(skeleton_group)
        if not offending:
            return set()
        hosts: set[int] = set()
        for component_id in self._merged_group_component_ids(skeleton_group):
            component = self.merged_skeleton_components[component_id]
            if self._merged_referenced_bone_ids(component["draw_ib"]) & offending:
                hosts.add(int(component_id))
        return hosts

    def _merged_cross_group_publish_plan(self, skeleton_group: int) -> dict | None:
        """跨组吸收宿主组的**蒙皮 CS 发布计划**（F3-CS；只服务直连路径）。

        为什么需要它（2026-10-10 FrameAnalysis + 用户实机症状「一帧延迟、运动时
        错位、静止正常」）：合并几何挂在宿主（实测 `4e6e989e-11700-0`，G2）自己的
        deform draw 上，而它的骨骼 = 本组 + 被吸收的跨组件（`4e6d8882`，G0）。
        宿主那段无论怎么排都会在**自己到达时**就把 SO 写掉，此时跨组件若还没到
        （实测宿主 deform=000020 早于跨组件 000023）⇒ 写进去的是**上一帧**的跨组
        骨骼。把跨组件并入守卫到达集合（F3）只能让 **draw 版重放**推迟到最后一个
        供骨件，而 draw 版重放受 IA 输入布局限制（跨组件 BI4 vs 宿主 BW16_BI16 ⇒
        `_merged_absorbed_replay_compatible` 为假，重放会把 BLENDINDICES 按错格式
        解释）。蒙皮 CS 从 SRV 读顶点属性、按索引写 SO ⇒ **与 IA 布局无关、幂等**，
        最后一个到达的必需部件派发即最终内容 —— 这正是它存在的意义。

        返回的字典与 `_build_merged_mesh_redirect_plan` 的计划同键（供
        `_merged_skin_publish_supported` / `_append_merged_skin_publish_block` /
        CS 段定义共用），`cross_group=True` 只作来源标记。取不到宿主 / 顶点数为 0
        时返回 None（保持历史行为）。
        """
        hosts = self._merged_cross_group_host_component_ids(skeleton_group)
        if not hosts:
            return None
        if self._merged_group_redirect_target(skeleton_group):
            # 该宿主组走重定向路径：合并几何的发布点由它自己的计划（载体/锚点）决定，
            # 不在这里叠加第二套。
            return None
        candidates = [
            self.merged_skeleton_components[component_id] for component_id in sorted(hosts)
        ]
        # 合并几何挂在**导出顶点数最多**的宿主上（其余宿主是自己那段的几何）。
        host = max(
            candidates,
            key=lambda component: self._drawib_exported_vertex_count(
                str(component["draw_ib"])
            ),
        )
        host_ib = str(host["draw_ib"])
        vertex_count = int(self._drawib_exported_vertex_count(host_ib))
        if vertex_count <= 0:
            return None
        position_layout = self._drawib_category_layout(host_ib, "Position") or {}
        required = set(self._merged_group_component_ids(skeleton_group))
        required.update(self._merged_cross_group_component_ids(skeleton_group))
        return {
            "cross_group": True,
            "target_ib": host_ib,
            "deform_draws": [
                (
                    f"Resource{host_ib}Position",
                    f"Resource{host_ib}Blend",
                    vertex_count,
                )
            ],
            "deform_draw_ibs": [host_ib],
            "so_prefix_rows": 0,
            "so_stride": int(position_layout.get("stride", 0) or 0),
            "anchor_layout_key": self._drawib_blend_layout_signature(host_ib),
            "anchor_layout": self._drawib_category_layout(host_ib, "Blend"),
            "required_component_ids": sorted(required),
        }

    def _merged_cross_group_publish_plans(self) -> dict[int, dict]:
        """``{宿主组: 跨组发布计划}``（F3-CS；按需构建并记忆化）。"""
        cache = getattr(self, "_merged_cross_group_publish_plan_cache", None)
        if cache is not None:
            return cache
        cache: dict[int, dict] = {}
        for group in self._merged_skeleton_groups():
            plan = self._merged_cross_group_publish_plan(int(group))
            if plan:
                cache[int(group)] = plan
        self._merged_cross_group_publish_plan_cache = cache
        return cache

    def _merged_determinable_component_ids(
        self, component_ids, fallback_to_all: bool = True
    ) -> list[int]:
        """从守卫集合里剔除**不参与实例判定**的部件（t75，用户 2026-09-18 实机拍板）。

        不参与判定的部件 = 导入期缓存里没有通道骨记录（导入未刷新 / json 被改坏）
        **或**通道骨退化（所在共享骨连通分量内没有 ≥2 件引用的槽位，例如叶瞬光01
        G0 的 ``8c8de427``，槽 50..55 与谁都不共骨）。这两类都**无法**参与跨部件
        实例对齐：把它们留在守卫里只会让槽的闭合被一个"身份未知"的部件拖住，
        **实测症状 = 骨骼动画直接卡住**（它的项既挡住、也半开其它部件的发布）。
        处置：

        - **撤销它在门控里的到达项**（不再阻塞本槽闭合）；
        - **保留**它的 palette 捕获与 attach（它是自己那些槽位的唯一写入者，
          撤销写入会让合并几何读不到那些骨）；
        - 由 `_append_merged_skeleton_deform_block_body` 落一条显式诊断点名它
          （``POSE_ALIGNMENT_UNAVAILABLE`` / ``POSE_KEY_HASH_UNAVAILABLE``）。

        残余风险（已在报告具名）：该部件的骨可能在"本槽闭合"之后才写入 ⇒ 合并
        几何最多读到**上一帧**的该部件骨（attach 每帧都跑，一帧内收敛）。

        ``fallback_to_all``：过滤会把集合清空时怎么兜底（**三级**，逐级放宽）：

        1. **参与判定**的部件（跨部件共享通道骨）——正常形态；
        2. 退到**有通道记录**的部件（退化候选：它的到达仍是本槽闭合的必要条件，
           只是不参与跨部件身份判定）；
        3. 再退到**原集合**：一个**空**的到达条件会让 `if ` 变成语法垃圾，整段
           重放/发布静默失效，宁可保守等待（`fallback_to_all=True` 时）。

        ``fallback_to_all=False``（SO 别名就绪门）时不做兜底 —— 由
        `_merged_so_ready_capturer_ids` 另行处理。
        """
        ordered = [int(component_id) for component_id in component_ids]
        judgeable = [
            component_id
            for component_id in ordered
            if self._merged_component_is_key_driven(
                self.merged_skeleton_components[component_id]
            )
        ]
        if judgeable:
            return judgeable
        if not fallback_to_all:
            return []
        # 二级：有通道记录（退化候选）的部件仍参与到达闭合——它们按出现次捕获，
        # `seen` 就是「本槽当帧已到达」的正确标记。
        recorded = [
            component_id
            for component_id in ordered
            if self._merged_component_channel_record(
                self.merged_skeleton_components[component_id]
            )
            is not None
        ]
        if recorded:
            return recorded
        return ordered

    def _merged_so_ready_capturer_ids(self, component_ids) -> list[int]:
        """SO 别名就绪门的**捕获者**集合（**不退回到全部**）。

        判据是「有没有通道记录」（不是「参不参与判定」）：按出现次捕获的**只供骨**
        部件同样会把当帧 SO 引用写进 `ResourceZZRedirectSO_s<k>`，它的
        `$zz_ms_seen_<i><k>` 正是「别名本槽当帧已刷新」的标记 —— 摘掉它会让重放
        绑到上一帧 / 未赋值的别名（2026-09-17 实测：当帧正确写入被丢掉）。所以
        这里只摘**连通道记录都没有**的捕获者（它的捕获槽位根本无从判定），
        判据与 §`_merged_component_channel_record` 一致、与实机确认要摘掉的那一项
        一致。**不退回**到原集合：一个空集合会让 `if ` 变成语法垃圾。
        """
        return [
            int(component_id)
            for component_id in component_ids
            if self._merged_component_channel_record(
                self.merged_skeleton_components[int(component_id)]
            )
            is not None
        ]

    def _merged_slot_seen_condition(
        self, component_ids, slot: int, cull_aware: bool = False
    ) -> str:
        """指定部件集合在该槽的守卫条件。

        重定向路径用 `required_component_ids`（= 合并几何真正引用的骨骼所属部件 +
        载体 + SO owner），**不是全组**：几何只读它引用的那些槽位，等齐它们就等于
        骨架当帧完整；等全组会把守卫推迟到组内最后一个部件（2026-09-16 实测：
        叶瞬光01 G0 组内最后到达的是 `c28e6303`#66，而它不在锚点集合 → 守卫永不
        闭合 → 合并几何整帧不写 → 「闪烁 + 物体消失」）。

        `cull_aware=True`（重定向路径用）：条件写成「（**上一帧没在本槽到达**）
        或（本帧在该槽已到达）」。被 LOD/视锥整帧剔除的写入者不再阻塞守卫，
        同时**帧首不再恒真**——这是个纯时机修复，见下。

        ------------------------------------------------------------------
        2026-09-17 dump 实证（`FrameAnalysis-2026-09-17-022132`，用户实测「身体一卡一卡」）：
        该帧单实例，G0 deform 顺序 = 01ef4403#21 → **999bff94#22（载体）** →
        9258d5f8#23 → ae840e72#29 → 8c8de427#31 → 38b3bd13#32（最后到达）。
        旧口径 `(any == 0 || seen == 1)` 在 #22 恒真（后面 4 个部件 `any` 还是 0）
        → 重放在 **#22** 落笔，用的是「只有 01ef4403/999bff94 是本帧 palette、
        其余 4 个是上一帧 palette」的骨架。把载体 SO（hash `01d5a625`，行 3..13673）
        与各 pass 的 `vs-t0` 逐顶点重算蒙皮比对：`#22` 骨架 13671/13671 行吻合
        （最大误差 2.2e-07），`#23/#29/#31/#32` 骨架最大误差 0.0226/0.0857 ——即
        SO 内容确实是 **#22 时刻**的骨架写出来的，排在载体之后的部件整帧慢一帧
        （G2 同样：SO `67a50546` 吻合 `#27` 载体 pass，不吻合 `#33` 最后到达）。
        改成按槽的「上一帧到达」预测后，帧首不再是「后面部件都不来」，守卫只会在
        **最后一个必需部件到达处**闭合一次；帧末的额外闭合写的是同一个 SO 的
        已越过缓冲末尾的偏移，实测为 no-op（同 dump：其后 4 次闭合没有改动
        行 3..13673 的任何一个字节）。

        ------------------------------------------------------------------
        2026-09-17（主症①b / AC-A2 修复）：消费点谓词由 `seen == 1` 改为
        `seen >= 1`（`_merged_seen_arrived_condition`，单一来源）。本守卫因此
        在本帧内**单调**——真值只可能由假转真，证明只用两条事实：
        · `seen` 只被顶层 `seen = seen + (occ == k)`（非负增量）修改，且只在
          `[Present]` 清零 ⇒ 帧内单调不减；
        · `prev` 只在 `[Present]` 写 ⇒ 帧内为常量。
        于是「帧内最后一次闭合」必然落在本帧**最后一个必需部件的 pass** 上；那一刻
        该槽全部 palette 都已当帧刷新 ⇒ 该次落笔用的是完整骨架（提前闭合无害，
        见 tests/…::test_guard_truth_is_monotone_and_last_closure_follows_captures）。
        n ≤ len(SLOTS) 时 `seen ≤ 1`，该改动与 `== 1` **逐字节等价**（行为不变的
        保守修复）；只有超出设计上界的 n ≥ 3 才改变轨迹——由 `SLOT_BOUND` 声明
        与 attach 段的 `x3` 探针显式化，不再静默。
        """
        if cull_aware:
            return " && ".join(
                "({prev_var} == 0 || {seen_clause})".format(
                    prev_var=self._merged_prev_var(int(component_id), slot),
                    seen_clause=self._merged_seen_arrived_condition(
                        int(component_id), slot
                    ),
                )
                for component_id in component_ids
            )
        return " && ".join(
            self._merged_seen_arrived_condition(int(component_id), slot)
            for component_id in component_ids
        )

    @staticmethod
    def _merged_absorb_redundant_seen_clauses(condition: str) -> str:
        """布尔吸收：删掉被同一条件里其它合取项蕴含的 cull_aware 子句。

        唯一会被删的形态（`_merged_slot_seen_condition(cull_aware=True)` 的产出）::

            (A) && (P || A) && ...

        其中 ``A`` = ``<seen> >= 1``（`_merged_seen_arrived_condition`，单一来源），
        来自 SO 别名就绪门 `_merged_so_ready_condition` —— **单捕获者**时它恰好包成
        ``(A)``。由布尔吸收 ``A && (P || A) ≡ A`` 可知该 cull_aware 子句恒真，属
        **可证明冗余**：守卫本来就要等这个捕获者到达，"被 LOD/视锥整帧剔除"的豁免
        对**它自己**没有意义（它不来守卫就不该闭合）。

        为什么不改变运行时行为：
          · 全部合取项都是纯变量比较（无赋值、无副作用）⇒ `&&` 短路顺序不影响结果；
          · 只在 ``A`` 已被**同一条件**断言时才删该子句，此刻它恒为真；
          · 其它部件的 cull_aware 子句（其 ``A`` 未被断言）**原样保留**，豁免语义不变。

        只做删除，不重排、不做其它代数改写；不匹配该形态的合取项一律原样保留。
        """
        parts = [p.strip() for p in str(condition or "").split(" && ") if p.strip()]
        if len(parts) < 2:
            return condition

        def _core(part: str) -> str:
            if part.startswith("(") and part.endswith(")"):
                return part[1:-1].strip()
            return part

        asserted = set()
        for part in parts:
            core = _core(part)
            if core.endswith(" >= 1") and " " not in core[:-5].strip():
                asserted.add(core)
        if not asserted:
            return condition

        kept = []
        for part in parts:
            disjuncts = [d.strip() for d in _core(part).split(" || ")]
            if (
                len(disjuncts) == 2
                and disjuncts[0].endswith(" == 0")
                and disjuncts[1] in asserted
            ):
                continue
            kept.append(part)
        return " && ".join(kept)

    @staticmethod
    def _merged_pose_key_pool_prefix(skeleton_group: int) -> str:
        """本组「姿态指纹 → 槽位」池的前缀名。"""
        return f"PoolZZMISlotOfKey_G{skeleton_group}"

    @staticmethod
    def _merged_pose_slot_taken_pool(skeleton_group: int) -> str:
        """本组「槽位已被占用」池名（索引 = 槽位号 1/2）。"""
        return f"PoolZZMIG_Taken_G{skeleton_group}"

    @staticmethod
    def _merged_pose_key_var(skeleton_group: int) -> str:
        """本 pass 姿态指纹变量名。"""
        return f"$zz_ms_pose_key_{skeleton_group}"

    def _merged_resolve_channel_plans(self, components) -> None:
        """补算缺失的通道计划（**缓存优先**，只对缺的骨架组重跑判定）。

        生产链路里通道骨由导入期算好并写进工作区 json（``ChannelPlan``），导出侧
        直接消费。只有在缓存缺失 / 版本不符时才在这里按**整组共享骨图**重算一次
        ——通道骨是「共享骨图连通分量」的组级性质，单看一个部件算不出来（会退化成
        "本部件最小槽位"）。补算结果写回组件记录，并在诊断表里留一条可检测痕迹。

        重算用**真实实现**（``common/zzmi_channel.select_channel_plan``），与导入期
        同一份代码 ⇒ 两条链路口径一致，不存在"导出侧另有一套判据"。
        """
        missing_groups = {
            int(component["skeleton_group"])
            for component in components
            if not isinstance(component.get("channel"), dict)
            or component["channel"].get("channel_slot") is None
        }
        if not missing_groups:
            return
        try:
            from ...common.zzmi_channel import select_channel_plan
        except Exception:  # pragma: no cover - 轻量 fake 宿主
            return
        for skeleton_group in sorted(missing_groups):
            group_components = [
                component
                for component in components
                if int(component["skeleton_group"]) == int(skeleton_group)
            ]
            plan = select_channel_plan(
                [
                    {
                        "draw_ib": str(component.get("draw_ib") or ""),
                        "vg_map": dict(component.get("vg_map") or {}),
                        "vg_count": int(component.get("vg_count") or 0),
                        "vg_offset": int(component.get("vg_offset") or 0),
                        "skeleton_group": int(skeleton_group),
                        # F6（复核发现）：补算必须带上缓存里的跨部件权重，否则
                        # 候选排序的第二键（权重合计）在补算路径上**失效**，
                        # 补算结果可能与导入期选出的通道骨不同
                        # （`ChannelPlanSlotWeights` 也就永远没有消费方）。
                        "slot_weights": dict(component.get("slot_weights") or {}),
                    }
                    for component in group_components
                ]
            )
            for component in group_components:
                record = plan.get(str(component.get("draw_ib") or ""))
                if record is not None and record.get("channel_slot") is not None:
                    component["channel"] = dict(record)
                    component["channel_digest"] = ""
            print(
                f"[ZZMI骨骼合并] 提示 G{int(skeleton_group)}: 通道计划缓存缺失，"
                "已按整组共享骨图在导出期补算（请重新一键导入以固化缓存）"
            )

    def _merged_component_channel_record(self, component: dict) -> dict | None:
        """本部件在导入期算好的**通道骨**记录（唯一判定口径的输入）。

        返回的 dict 至少含 ``channel_slot``（通道骨的全局槽位，跨部件一致）与
        ``channel_local``（该骨在**本部件 palette** 里的本地骨下标，算键用）。
        缓存缺失 / 字段非法 / 与 VGMap 自相矛盾一律返回 None——调用方必须把它
        当**显式诊断**处理，绝不静默退回出现次判据（用户 2026-09-18 拍板）。
        """
        record = component.get("channel")
        if not isinstance(record, dict):
            return None
        try:
            slot = int(record.get("channel_slot"))
            local = int(record.get("channel_local"))
        except (TypeError, ValueError):
            return None
        if slot < 0 or local < 0:
            return None
        vg_map = component.get("vg_map") or {}
        try:
            mapped = int(vg_map.get(local, vg_map.get(str(local), -1)))
        except (TypeError, ValueError):
            return None
        if mapped != slot:
            return None
        return record

    def _merged_component_key_record(self, component: dict) -> dict | None:
        """本部件**参与跨部件实例判定**的通道记录；不参与时 None。

        判据只有一条（``common/zzmi_channel.is_cross_part_channel``）：通道骨被
        **≥2 个部件**引用。分量内没有共享骨时导入期给的是**退化候选**（本部件最小
        槽位，只区分它自己两个实例）——它不能当跨部件判定输入（不同物理骨 ⇒ 不同
        哈希 ⇒ 同组两件各占一个池槽、槽位永远对不上），因此与「缓存缺失」同样处置：
        **只供骨、不参与任何门控**（用户 2026-09-18 实机确认的硬事实：无判定的部件
        留在门控里会把有键部件的发布一起拖死，症状 = 骨骼动画直接卡住）。
        """
        record = self._merged_component_channel_record(component)
        if record is None:
            return None
        if not _is_cross_part_channel(record):
            return None
        return record

    def _merged_group_has_key_driven(self, skeleton_group: int) -> bool:
        """本组是否按**精确哈希键**选槽（= 该组至少一个部件参与跨部件判定）。

        组级门控的单一事实源：全局键变量声明、键池段、SKIN 段的键诊断探针都用它，
        确保「发键块」与「声明键变量 / 发键池」永远同进同退（F1）。
        """
        return any(
            self._merged_component_is_key_driven(
                self.merged_skeleton_components[component_id]
            )
            for component_id in self._merged_group_component_ids(skeleton_group)
        )

    def _merged_component_is_key_driven(self, component: dict) -> bool:
        """本部件是否按**精确哈希键**选槽（= 参与跨部件实例判定的唯一模式）。

        两个条件**同时**成立才算：

        1. 有跨部件共享通道记录（``_merged_component_key_record``）；
        2. 本组走**重定向路径**（``_merged_group_redirect_plan`` 非 None）。

        条件 2 是 F1（复核发现）的修复：直连路径（本组没有重定向计划）的绘制决策
        仍然是**按出现次**的（``_append_merged_direct_slot_guards`` 的自足挂点
        `if $zz_ms_occ_<i> == <slot>` → 绑本槽骨架），而 palette 捕获若改由键块写进
        `$PoolZZMISlotOfKey_G<g>[key]` 算出的槽，两者在 ``pool[key] != occ`` 时会
        对不上——本 pass 把自己的 palette 写进键槽，却用出现次槽的合并骨架绘制
        ⇒ 用**另一实例/上一帧**的骨架画本部件几何。HEAD 的直连路径**根本不发键块**
        （`group_plan is not None` 把整个对齐块挡掉），因此这里回到 HEAD 行为：
        直连路径一律按键出现次捕获（palette + SO 别名一起），不发键块、不声明键
        变量、不发键池。重定向路径（用户实机验证过的 v9 形态）行为不变。
        """
        if self._merged_component_key_record(component) is None:
            return False
        try:
            skeleton_group = int(component["skeleton_group"])
        except (KeyError, TypeError, ValueError):
            return False
        return self._merged_group_redirect_plan(skeleton_group) is not None

    def _merged_group_pose_anchor_slot(self, skeleton_group: int) -> int | None:
        """本组通道骨的**全局槽位**（t75：唯一判定口径，读导入期缓存）。

        返回值只用于**复核/诊断**：真正的键输入是逐部件自己的 ``channel_local``
        （同一连通分量内不同部件映射到同一通道槽位的本地下标可以不同，例如
        叶瞬光01 G0 的槽 0：``01ef4403``#0 / ``999bff94``#7 / ``ae840e72``#2）。
        同组内不同连通分量可以有不同通道槽位，此时返回升序第一个。

        旧实现取全组 ``vg_map`` **值集合的交集最小值**，只要有一件与谁都不共骨
        （G0 的 ``8c8de427``，槽 50..55）就整组返回 None ⇒ 六个部件各打一行
        ``no_shared_canonical_bone`` 后整组静默退回出现次口径。t75 改为按
        **共享骨图连通分量**选通道骨（``common/zzmi_channel.py``）。
        """
        slots = sorted(
            {
                int(record["channel_slot"])
                for record in (
                    self._merged_component_channel_record(
                        self.merged_skeleton_components[component_id]
                    )
                    for component_id in self._merged_group_component_ids(skeleton_group)
                )
                if record is not None
            }
        )
        return slots[0] if slots else None

    def _merged_component_pose_anchor_local(
        self, component: dict, anchor_slot: int | None = None
    ) -> int | None:
        """该部件里参与精确哈希的**本地骨下标**（t75：= 缓存里的 channel_local）。

        ``anchor_slot`` 只用于复核（缓存里的通道槽位必须与它一致）；为 None 时
        不做复核（旧调用点形态）。
        """
        record = self._merged_component_channel_record(component)
        if record is None:
            return None
        local = int(record["channel_local"])
        if anchor_slot is not None and int(record["channel_slot"]) != int(anchor_slot):
            return None
        return local

    def _merged_pose_alignment_unavailable_reason(
        self, component_id: int, group_plan: dict | None
    ) -> str | None:
        """本部件**通道判定结构性不可达**的原因码；可达时 None。

        只有两种情况算缺口（都必须落机器可读诊断，绝不静默）：
        - ``no_redirect_plan``：该组件所在组没有重定向计划（没有合并几何 / SO
          语义，判定与发布都无从落地）；
        - ``no_channel_plan_cache``：工作区缓存里没有该部件的通道记录
          （导入期未刷新，或 json 被手工改坏）。
        单部件组**不是**缺口：它自成一个连通分量，通道骨只用于区分**它自己**的
        两个实例（跨部件混槽在该组内不可能发生），因此既不诊断也不发键块。
        另外，即使有重定向计划，单部件组也没有"跨部件实例对齐"这回事 ⇒ 不报。
        """
        component_ids = self._merged_group_component_ids(
            int(self.merged_skeleton_components[component_id]["skeleton_group"])
        )
        if len(component_ids) < 2:
            return None
        if group_plan is None:
            return "no_redirect_plan"
        component = self.merged_skeleton_components[component_id]
        if self._merged_component_channel_record(component) is None:
            return "no_channel_plan_cache"
        return None

    def _append_merged_pose_key_alignment(
        self,
        section,
        draw_ib: str,
        component_id: int,
        skeleton_group: int,
        group_plan: dict | None,
        capture_so: bool | None = None,
    ) -> None:
        """把本 pass 的 palette / SO 引用按**通道骨精确哈希键**投到正确的槽。

        t75 唯一判定口径（用户 2026-09-18 拍板后只剩这一条）：

        - 键 = ``vs-t0->HashRegion(48 * channel_local, 48)``——只读**一根**骨的
          48 字节矩阵，**逐字节精确**（源码 ``CommandList.cpp:4033/7961``；失败
          返回 -1/-2/-3）。
        - 通道骨 ``channel_local`` 来自导入期缓存（``common/zzmi_channel.py``）：
          该骨是**共享骨连通分量**内被最多部件引用、且带顶点权重最多的槽位在本
          部件 palette 里的本地下标。
        - 池 ``PoolZZMISlotOfKey_G<g>`` 用 ``pool_index_type = fifo``：键是 32 位
          整数，**全位精确匹配**，不能再用量化空间池（0.01 格会把两个实例并键，
          实测并键率最高 99/135）。
        - 守卫写 ``> 0``：``HashRegion`` 失败返回 -1/-2/-3，``!= 0`` 挡不住。

        **为什么必须逐字节精确**：t73 实测同一实例、同一根物理骨在不同部件的
        palette 里**逐位相同（Δ=0）**，而两个实例的差极小（G0 槽 0 只有
        1.4e-7…8.2e-6、G2 槽 185 = 1.32e-3），远小于任何量化格。

        **判据不退回出现次，但捕获必须在两条结果路径上都发**：指纹是**唯一**的
        *判定*口径，``HashRegion`` 失败（键 ≤ 0）时走**显式诊断**（由调用方分级
        记录），绝不静默改用出现次*判定*。但**写不能只存在于 ``> 0`` 体内**：键
        失败（实测返回 -1/-2/-3）时该分支整块不执行，若无条件读取 palette 的
        attach（顶层无条件 ``run``）/ 重放守卫照常读取，则它们读到**从未写入**的
        缓冲 ⇒ 合并骨架为空（可全零）→ 模型消失 / 形态键静默失效（用户实测症状
        「门控的问题，就是形态键失效」，叶瞬光01：键恒 0/-1，失败分支曾是**空壳**）。
        因此两条结果路径各发一次捕获（``> 0`` 用键算出的槽；``≤ 0`` 用出现次这个
        **位置标签**定落点，与绘制侧在键不可用时同样按出现次选槽的口径一致）；
        它们是**同一判据**的两条结果路径，不是两套判据。
        """
        component = self.merged_skeleton_components[component_id]
        record = self._merged_component_channel_record(component)
        if record is None:
            return
        local = int(record["channel_local"])
        slots = self._merged_skeleton_slots()
        slot_first, slot_second = slots[0], slots[-1]
        key_var = self._merged_pose_key_var(skeleton_group)
        # 键不可用时的**落点**依据：本 pass 的出现次（位置标签，不是判据）。
        occ_var = self._merged_occ_var(component_id)
        # 池在表达式里带 `$` 前缀（EFMI 口径：$PoolInput_ObjectSpatialIdentity[...]）
        key_pool = "$" + self._merged_pose_key_pool_prefix(skeleton_group)
        taken_pool = "$" + self._merged_pose_slot_taken_pool(skeleton_group)
        offset = ZZMI_CHANNEL_HASH_BYTES * local
        # SO 引用捕获条件由调用方给出（与旧出现次分支逐字同条件：重定向路径的
        # SO owner / 直连路径的合并宿主）；未给出时退回「本组 SO owner」这一读法。
        owner = (
            bool(capture_so)
            if capture_so is not None
            else self._merged_component_is_so_owner(draw_ib, group_plan)
        )
        reason = str(record.get("channel_reason") or "")
        shared = int(record.get("channel_shared_components") or 0)
        weight = int(record.get("channel_shared_weight") or 0)

        def emit_capture(target_slot: int, indent: str = "    ") -> None:
            """把当帧 palette 落到 <target_slot>，SO 别名只由 SO owner 重捕获。"""
            # 先把**上一帧**的值留一份给跨组校准（此刻主副本还是上一帧的内容）。
            section.append(
                f"{indent}{self._merged_palette_lag_name(draw_ib, target_slot)} = "
                f"copy {self._merged_palette_name(draw_ib, target_slot)}"
            )
            section.append(
                f"{indent}{self._merged_palette_name(draw_ib, target_slot)} = "
                "copy vs-t0 unless_null"
            )
            if owner:
                section.append(
                    f"{indent}{self._merged_redirect_so_name(skeleton_group, target_slot)}"
                    " = ref so0"
                )
                # O3：登记 referent 捕获点（只计数，不改产物文本）。键块里两个槽
                # 各发一次（if/else 两条结果路径），与旧出现次分支的登记口径一致。
                self._merged_reuse_capture_site(skeleton_group, target_slot)

        section.append("")
        # t149：下面两条是**开发者诊断注释**（身份口径说明），默认不写进配置表。
        # 它们不是锚点：没有任何重导出逻辑 re-parse 它们（唯一消费者是回归测试）。
        if ZZMI_MERGE_DIAG_EMIT:
            section.append(
                "; --- 实例对齐：通道骨精确哈希键（唯一判定口径）→ 槽位 ---"
            )
            # F5（复核发现）：身份口径必须**在产物里可见**——只读 ini 的人必须能一眼看到
            # 这条判定用的是骨名身份还是 `local_slot_map` 弱代理（槽位号 ≠ 骨头身份）。
            section.append(
                f"; channel_slot={int(record['channel_slot'])} channel_local={local} "
                f"hash_region={offset},{ZZMI_CHANNEL_HASH_BYTES} shared_components={shared} "
                f"shared_weight={weight} reason={reason} "
                f"identity_basis={record.get('channel_identity_basis') or 'unknown'} "
                f"identity_token={record.get('channel_identity_token') or ''} "
                "逐字节精确匹配（无容差）"
            )
        section.append("ResourceZZPoseKeySrc = ref vs-t0")
        section.append(
            f"{key_var} = ResourceZZPoseKeySrc->HashRegion("
            f"{offset}, {ZZMI_CHANNEL_HASH_BYTES})"
        )
        # 守卫 > 0：HashRegion 失败返回 -1/-2/-3，`!= 0` 挡不住。
        section.append(f"if {key_var} > 0")
        section.append(f"    if {key_pool}[{key_var}] == 0")
        section.append(f"        if {taken_pool}[{slot_first}] == 0")
        section.append(f"            {taken_pool}[{slot_first}] = 1")
        section.append(f"            {key_pool}[{key_var}] = {slot_first}")
        section.append("        else")
        section.append(f"            {taken_pool}[{slot_second}] = 1")
        section.append(f"            {key_pool}[{key_var}] = {slot_second}")
        section.append("        endif")
        section.append("    endif")
        section.append(f"    if {key_pool}[{key_var}] == {slot_second}")
        emit_capture(slot_second)
        section.append("    else")
        emit_capture(slot_first)
        section.append("    endif")
        section.append("else")
        # t149：这三行是解释 else 分支为何"照发 palette / SO 引用"的**开发者说明**，
        # 默认不写进配置表。else 分支的**功能行**（下面的 occ 落点循环）照旧发射。
        if ZZMI_MERGE_DIAG_EMIT:
            section.append(
                "    ; 键 <= 0：HashRegion 失败（-1/-2/-3）。判定**不退回出现次判定**"
                "（唯一判据仍是键），但本 pass 的"
            )
            section.append(
                "    ; palette / SO 引用**必须照发**：写不能只存在于上面的 if 体内"
                "（无条件读取它的 attach / 重放守卫会读走空骨架）"
            )
            section.append("    ; → 落点改用出现次位置标签（只定落点，不参与判定）。")
        for index, slot in enumerate(slots):
            section.append(f"    if {occ_var} == {slot}" if index == 0 else "    else")
            emit_capture(slot, indent="        ")
        if len(slots) > 1:
            section.append("    endif")
        section.append("endif")

    def _merged_group_so_capturer_component_ids(self, skeleton_group: int) -> list[int]:
        """本组 `ResourceZZRedirectSO_G<g>_s<k>` 别名的捕获者组件号（通常恰 1 个）。

        重定向路径 = 计划的 `so_owner_ib`（载体）；直连路径 = 合并宿主
        （`_merged_group_absorbed_hosts`，它们在**自己的** deform 段顶层把本轮
        SO 引用捕获下来）。某组若有重定向计划就不再叠加吸收宿主（两条路径互斥）。
        """
        capturers: set[int] = set()
        for target_ib, plan in self._redirect_target_map.items():
            target_cid = plan.get("target_component_id")
            if target_cid is None:
                target_cid = self.merged_skeleton_component_id_dict.get(str(target_ib))
            if target_cid is None:
                continue
            target_cid = int(target_cid)
            if not 0 <= target_cid < len(self.merged_skeleton_components):
                continue
            group_component = self.merged_skeleton_components[target_cid]
            if int(group_component["skeleton_group"]) != int(skeleton_group):
                continue
            owner_cid = self.merged_skeleton_component_id_dict.get(
                str(plan.get("so_owner_ib") or "")
            )
            if owner_cid is not None:
                capturers.add(int(owner_cid))
        if not capturers:
            for host in self._merged_group_absorbed_hosts(skeleton_group):
                capturers.add(int(host["component_id"]))
        return sorted(capturers)

    def _merged_so_ready_condition(self, component_ids, slot: int) -> str:
        """显式给出捕获者集合时的「SO 别名当帧已捕获」条件（空串 = 不需要门）。

        谓词用 `_merged_seen_arrived_condition`（`>= 1`）：捕获者第 3 次以上出现
        时 `seen` 会到 2，`== 1` 会让这道门在本帧剩余 draw 内恒假 ⇒ 守卫集体关闭。
        """
        parts = [
            self._merged_seen_arrived_condition(int(component_id), slot)
            for component_id in component_ids
        ]
        if not parts:
            return ""
        if len(parts) == 1:
            return f"({parts[0]})"
        return "(" + " || ".join(parts) + ")"

    def _merged_slot_so_ready_condition(self, skeleton_group: int, slot: int) -> str:
        """本槽 SO 别名「当帧已被捕获」的条件（可空串 = 本组无别名捕获者）。

        为什么必须有这道门（2026-09-17 复核）：别名的赋值只发生在捕获者本槽到达
        的那一笔（`if $zz_ms_occ_<owner> == <slot>` 体内 `… = ref so0`）。守卫若
        早于它落笔，`so0 = ref ResourceZZRedirectSO_G<g>_s<k>` 读到的是**上一帧**
        捕获的缓冲（往往就是同一个缓冲、且偏移已停在末尾 ⇒ 当帧的正确写入被丢掉、
        留在缓冲里的还是上一帧内容）或**从未赋值**的资源（3DMigoto 未定义行为，
        写入落点不可控）。加上这道门之后，守卫最早只能在「捕获者本槽到达」之后
        闭合，与 `$zz_ms_prev_<i><k>` 的期望集合门控合起来得到：
        `max(捕获者本槽到达, 最后一个必需部件本槽到达)` —— 单实例帧里第 2 槽
        永远不闭合（不再往未赋值别名落笔），多实例帧里每槽在别名刷新后闭合一次。

        t75：捕获者集合先剔除**连通道记录都没有**的部件（用户实机确认 2026-09-18：
        把这种部件（G0 的 ``8c8de427`` 一类）留在门控里会把**有键部件的发布一起
        拖死**，症状 = 骨骼动画直接卡住）。它只"供骨"（palette 捕获 + attach 照旧
        保留），绝不参与任何门控。判据见 `_merged_so_ready_capturer_ids`：
        「有没有通道记录」——按出现次捕获的只供骨部件仍靠 `seen` 标记本槽别名已刷新，
        摘掉它会丢 F8 保护。
        """
        return self._merged_so_ready_condition(
            self._merged_so_ready_capturer_ids(
                self._merged_group_so_capturer_component_ids(skeleton_group),
            ),
            slot,
        )

    def _merged_slot_guard_condition(
        self, component_ids, skeleton_group: int, slot: int, cull_aware: bool = True
    ) -> str:
        """每槽守卫的完整条件 =（SO 别名当帧已捕获）&&（期望到达集合）。

        前半段防「写进上一帧/未赋值的别名」，后半段防「提前用半帧骨架落笔」——
        两条各自对应 2026-09-17 的一次实机现象，说明见两个子条件的 docstring。

        t75：期望到达集合先剔除**无通道判定**的部件（
        `_merged_determinable_component_ids`）——它的实例身份未知，留在集合里
        只会阻塞或污染本槽闭合；它的 attach / palette 捕获照旧保留（供骨）。
        """
        parts = [
            self._merged_slot_so_ready_condition(skeleton_group, slot),
            self._merged_slot_seen_condition(
                self._merged_determinable_component_ids(component_ids),
                slot,
                cull_aware=cull_aware,
            ),
        ]
        # t149：SO 就绪门断言了捕获者的 `seen >= 1` ⇒ 该捕获者自己的 cull_aware
        # 子句 `(prev == 0 || seen >= 1)` 恒真，按布尔吸收删除（行为等价，见
        # `_merged_absorb_redundant_seen_clauses`）。
        return self._merged_absorb_redundant_seen_clauses(
            " && ".join(part for part in parts if part)
        )

    def _append_merged_skeleton_deform_block(
        self, texture_override_vb_section, drawib_model
    ) -> None:
        """deform 段合并骨架注入 = 正文（每槽 draw 门控）+ 末尾「每槽发布」块。

        正文见 `_append_merged_skeleton_deform_block_body`（v9 出现次槽位 + 守卫）。
        本包装在正文之后追加**蒙皮 CS 发布块**（`_append_merged_skin_publish_block`），
        对**所有**必需部件（含 Blend 布局与锚点不一致、不能重放 draw 的窄布局部件）
        都发一份：draw 版重放只能在兼容锚点落笔，窄布局必需部件排在最后到达时
        该槽 SO 整帧不写（2026-09-17 FrameAnalysis-025058 实证：G0 required 最后
        到达是 8c8de427 #83/#84，最后锚点 ae840e72 #82 → 两个实例 SO 与完整骨架
        只有 ~3100/13671 行吻合 = 用户看到的「一直闪」）。
        """
        self._append_merged_skeleton_deform_block_body(
            texture_override_vb_section, drawib_model
        )
        draw_ib = drawib_model.draw_ib
        component_id = int(self.merged_skeleton_component_id_dict[draw_ib])
        skeleton_group = int(
            self.merged_skeleton_components[component_id]["skeleton_group"]
        )
        group_plan = self._merged_group_redirect_plan(skeleton_group)
        if group_plan is None:
            # F3-CS：跨组合并的宿主组走直连路径（没有重定向计划），但它的合并几何
            # 混进了别组顶点 ⇒ 必须在**最后一个供骨件到达处**用当帧骨架重发布一次
            # （否则宿主自己那段写 SO 时跨组 palette 还是上一帧的 = 用户实测的
            # 「一帧延迟 + 运动时错位、静止正常」）。本部件既可能是宿主，也可能是
            # 供骨件；两个方向都发一份，守卫条件相同 ⇒ 一帧内只在最后一个供骨件
            # 到达处闭合一次（CS 按索引写 = 幂等，不会重复叠加）。
            cross_plans = self._merged_cross_group_publish_plans()
            for host_group in self._merged_cross_host_group_map().get(
                int(component_id), []
            ):
                host_plan = cross_plans.get(int(host_group))
                if host_plan:
                    self._append_merged_skin_publish_block(
                        texture_override_vb_section,
                        int(host_group),
                        host_plan,
                        draw_ib,
                    )
            own_plan = cross_plans.get(int(skeleton_group))
            if own_plan:
                self._append_merged_skin_publish_block(
                    texture_override_vb_section, skeleton_group, own_plan, draw_ib
                )
            return
        self._append_merged_skin_publish_block(
            texture_override_vb_section, skeleton_group, group_plan, draw_ib
        )

    def _append_merged_skeleton_deform_block_body(
        self, texture_override_vb_section, drawib_model
    ) -> None:
        """deform 段合并骨架注入（v9：出现次槽位 + 每槽守卫）。

        生成顺序**必须**保持如下（每一条都对应一次游戏内实测失败）：
        1. `$zz_ms_occ_<i>` 顶层自增 + `>= 3` 回绕为 1（槽位 1/2 循环）；
        2. `$zz_ms_seen_<i><k>` 顶层 sticky 累加（**绝不能在 if 体内赋值**：
           只在 if 体内赋值的变量会被加载期优化器按初值静态折叠 → 守卫整段
           被删除 → 重放不发生）；
        3. `if $zz_ms_occ_<i> == 1 ... else ... endif` 把当帧 palette 复制进本槽
           的 palette 资源；SO 捕获**只由 SO owner（载体）部件做**；
        4. 顶层无条件 `run` 全部 (部件, 槽) attach（**run 绝不能进 if**：本 fork
           里 if 内的 run 不执行 → 骨架为空 → 模型整体消失）；
        5. `vs-t0 = <本组 s1 骨架>` 顶层默认换绑；
        6. `handling = skip` 与载体 3 顶点前缀 stub（`draw = 3, 0`，在守卫之前）；
        7. 每槽绘制：体内**只允许**绑定与 draw（vs-t0 / so0 = ref / vb2 / vb0 /
           draw / so0 = null），**不得出现 run、不得给 $变量赋值**。两种门控：
           - 自足挂点（直连路径且几何只采样自己的槽位）：`if <本部件 occ> == 槽`
             后直接画自己的几何（不等组内其它部件，v9.1）；
           - 组级门控：`if <本槽 SO 别名的捕获者当帧已到达> && <必需部件「上一帧本槽
             没到」或「本帧本槽已到」相与>`（重定向重放宿主与吸收挂点——它们画的
             是含组内其它部件顶点的合并几何）。豁免项由 `$zz_ms_prev_<i><k>`
             （上一帧到达情况）预测，**不能**用「本帧至今没出现」：后者帧首恒真 →
             守卫在载体自己那段就落笔、排在载体之后的部件用上一帧 palette
             （2026-09-17 dump 实证，见 `_merged_slot_seen_condition`）。
             前半段（`_merged_slot_so_ready_condition`）必须有：别名的赋值只在
             捕获者本槽到达那一笔（`if occ == k` 体内），早于它落笔就是
             `so0 = ref <上一帧别名 / 从未赋值>` ——上一帧别名往往就是同一个缓冲
             且偏移已到末尾 ⇒ 当帧正确写入被丢掉；从未赋值则是 3DMigoto 未定义行为。
        """
        draw_ib = drawib_model.draw_ib
        component_id = int(self.merged_skeleton_component_id_dict[draw_ib])
        component = self.merged_skeleton_components[component_id]
        skeleton_group = int(component["skeleton_group"])
        slots = self._merged_skeleton_slots()

        occ_var = self._merged_occ_var(component_id)
        slot_first = slots[0]

        # 1) 出现次（顶层）
        texture_override_vb_section.append(f"{occ_var} = {occ_var} + 1")
        texture_override_vb_section.append(f"if {occ_var} >= {ZZMI_MERGED_SKELETON_OCC_WRAP}")
        texture_override_vb_section.append(f"    {occ_var} = {slot_first}")
        texture_override_vb_section.append("endif")
        # 2) 到达标记（顶层 sticky 累加；绝不在 if 体内赋值）
        for slot in slots:
            seen_var = self._merged_seen_var(component_id, slot)
            texture_override_vb_section.append(
                f"{seen_var} = {seen_var} + ({occ_var} == {slot})"
            )

        # 3) 按槽捕获 palette（与 SO owner / 合并宿主的 SO 引用）
        #
        # 【⚠ F5 试过并已回退（2026-10-10）】曾把本捕获整体后移到 attach 之后，
        # 想让 attach 读到的 palette 与它读到的（必然是上一帧的）cb1 同源。**实测失败**：
        # 合并骨架缓冲是**宿主与跨组供骨件共用**的，捕获后移会让**整块合并骨架**
        # （含宿主自己的骨骼）一起慢一帧 ⇒ 宿主自身几何也变陈旧 ⇒ 用户实测
        # 「无论哪边都在抖、骨骼矩阵不对」。结论：只让**跨组换算**用上一帧矩阵，
        # 必须另开一份滞后副本（宿主当帧 + 跨组滞后），不能动主捕获的时序。
        capture_lines: list[str] = []
        #
        # 合并几何的归属先算出来：直连路径（该组没有重定向计划）里，宿主导出的
        # 合并几何必须能在**任何**布局兼容的挂点闭合守卫后重放——引擎把同组部件
        # 的 deform pass 排成什么顺序每帧都可能不同，只让宿主自己那段重放时，
        # 宿主排在前面的帧里合并几何整段不写（用户实测"合并之后还在闪"）。
        redirect_carrier = self._redirect_carrier_map.get(draw_ib)
        group_target = self._merged_group_redirect_target(skeleton_group)
        group_plan = self._redirect_target_map.get(group_target) if group_target else None
        absorbed_hosts = (
            self._merged_group_absorbed_hosts(skeleton_group) if group_plan is None else []
        )
        is_absorbed_host = any(
            int(host["component_id"]) == int(component_id) for host in absorbed_hosts
        )
        # F3-CS：几何吸收了**别组**部件的宿主（判据见 `_merged_cross_group_host_component_ids`）。
        # 它的 SO 引用同样必须捕获 —— 否则跨组重发布（蒙皮 CS）没有落点，只能退回
        # 「宿主自己那段写 SO」= 用户实测的一帧延迟。注意这里**不**改
        # `absorbed_hosts`：那会换掉宿主的每槽守卫档位（直连路径无条件 draw 是
        # CS 绑定失败时的兜底，必须保留）。
        is_cross_group_host = int(component_id) in self._merged_cross_group_host_component_ids(
            skeleton_group
        )
        so_owner_target_ibs = self._merged_so_owner_target_ibs(draw_ib)
        # t75：**唯一判定模式**。通道骨被 ≥2 件引用（真正跨部件共享）⇒ 由键块按
        # 精确哈希算出的槽捕获 palette / SO 引用（`_append_merged_pose_key_alignment`）；
        # 没有通道判定（缓存缺失，或通道骨退化为"只区分自己两个实例"的候选）⇒
        # 保留历史上的出现次捕获：它是自己那些槽位的唯一写入者，必须供骨；同时它
        # 从**所有**门控里摘掉（`_merged_determinable_component_ids`）。
        # 两条路径互斥，不会重复捕获，也不存在"两套判据同时生效"。
        channel_record = self._merged_component_channel_record(component)
        key_driven_capture = self._merged_component_is_key_driven(component)
        # SO 引用（`ResourceZZRedirectSO_s<k> = ref so0`）的捕获条件与旧出现次分支
        # **逐字一致**：重定向路径里作为 SO owner 的挂点，或直连路径里被吸收的合并
        # 宿主。判据换成键以后这个条件必须原样带过去——否则直连路径的宿主不再刷新
        # 自己的 SO（它的每槽重放会写上一帧的缓冲）。
        captures_redirect_so = (
            bool(so_owner_target_ibs) or is_absorbed_host or is_cross_group_host
        )
        if not key_driven_capture:
            for index, slot in enumerate(slots):
                palette_line = (
                    f"{self._merged_palette_name(draw_ib, slot)} = copy vs-t0 unless_null"
                )
                condition = f"if {occ_var} == {slot}" if index == 0 else "else"
                capture_lines.append(condition)
                capture_lines.append(f"    {palette_line}")
                for _so_owner_target_ib in so_owner_target_ibs:
                    capture_lines.append(
                        f"    {self._merged_redirect_so_name(skeleton_group, slot)} = ref so0"
                    )
                    # O3：登记 referent 捕获点（只计数，不改产物文本）
                    self._merged_reuse_capture_site(skeleton_group, slot)
                if is_absorbed_host or is_cross_group_host:
                    # 直连路径的合并宿主（组内被吸收 / 跨组被吸收）：把本轮自己的 SO
                    # 引用捕获下来（与重定向路径同名资源、同语义），任何兼容挂点闭合
                    # 守卫后都能把合并几何写进去。
                    capture_lines.append(
                        f"    {self._merged_redirect_so_name(skeleton_group, slot)} = ref so0"
                    )
                    self._merged_reuse_capture_site(skeleton_group, slot)
            if len(slots) > 1:
                capture_lines.append("endif")

        # 3.5) 实例对齐：把本 pass 的 palette / SO 引用按**通道骨精确哈希键**
        # 改投到正确槽位（唯一判定口径，见 `_append_merged_pose_key_alignment`）。
        # 出现次只是**位置标签**：两个实例的提交先后可以逐部件不同（排序键本身
        # 随实例变化），按出现次捕获会把两份姿态混进同一槽骨架。
        if key_driven_capture:
            self._append_merged_pose_key_alignment(
                capture_lines,
                draw_ib,
                component_id,
                skeleton_group,
                group_plan,
                capture_so=captures_redirect_so,
            )
        # 判定块**不可达**时必须留下机器可读痕迹：此前这里是**静默**退化
        # （有计划的组没有共享锚点 / 有锚点的组没有计划 ⇒ 出现次口径成为唯一手段，
        # 而出现次是位置标签、不是实例标签 ⇒ 主症②的槽位归属翻转无人可判）。
        unavailable_reason = self._merged_pose_alignment_unavailable_reason(
            component_id, group_plan
        )
        if key_driven_capture:
            # 键块已记录完整复核信息（通道槽位 / 本地下标 / 理由），无需重复诊断。
            pass
        elif unavailable_reason is not None:
            # F1：直连路径（`no_redirect_plan`）或缓存缺失（`no_channel_plan_cache`）
            # ⇒ 本部件按「只供骨、不参与判定」处置 —— 必须显式点名（绝不静默）。
            # 直连路径上**不能**发键块：绘制决策仍是按出现次
            # （`_append_merged_direct_slot_guards` 的自足挂点 `occ == slot`），
            # 键驱动捕获会把 palette 写进 `pool[key]` 而绘制读 `occ` 槽 ⇒ 两套判据
            # 混用（复核实测：直连路径 HEAD 0 个键块 ✓ / 工作区 6 次 ✗）。
            # F9（**待用户裁定**）：退化/无通道部件是否允许把它的 `seen` 留在
            # **SO 别名就绪项**里（`_merged_so_ready_capturer_ids` 判据是「有没有
            # 通道记录」，不是「参不参与判定」）——现状 = 允许（理由见该函数
            # docstring 与复核报告 F9/判定 2），本修复**不改语义**，仅标注待裁定。
            self._append_merged_diag(
                texture_override_vb_section,
                self._merged_diag(
                    ZZMI_MERGE_DIAG_POSE_UNAVAILABLE,
                    "通道骨判定不可达：本部件不参与跨部件实例判定（不做键驱动捕获），"
                    "仅按出现次捕获当帧 palette + attach 供骨，并已从全部门控里摘掉"
                    "（身份未知的门控项会拖死有键部件的发布）。"
                    if unavailable_reason == "no_channel_plan_cache"
                    else "实例对齐（通道骨）不可达：本组没有重定向计划（直连路径），"
                    "绘制决策按出现次、捕获也必须按出现次 ⇒ 不发键块。"
                    "判定与发布都无从落地。",
                    group=f"G{int(skeleton_group)}",
                    draw_ib=draw_ib,
                    reason=unavailable_reason,
                    slots=",".join(str(int(slot)) for slot in slots),
                ),
            )
        elif channel_record is not None:
            # 通道骨**退化**（分量内没有全体成员可比的共享骨，例如叶瞬光01 G0 的
            # `8c8de427`）：键只区分本部件自己的两个实例，不能当跨部件判定输入，
            # 因此本部件按「只供骨、不参与判定」处置 —— 必须显式点名（绝不静默）。
            channel_diag = str(channel_record.get("channel_diagnostic") or "")
            self._append_merged_diag(
                texture_override_vb_section,
                self._merged_diag(
                    ZZMI_MERGE_DIAG_POSE_KEY_UNAVAILABLE,
                    channel_diag
                    or "通道骨退化为本部件候选：不参与跨部件实例判定。",
                    group=f"G{int(skeleton_group)}",
                    draw_ib=draw_ib,
                    channel_slot=int(channel_record["channel_slot"]),
                    channel_local=int(channel_record["channel_local"]),
                    reason=str(channel_record.get("channel_reason") or ""),
                ),
            )

        # 3.9) 捕获按原次序落在 attach **之前**（attach 读到当帧 palette；F5 的后移已回退）。
        for _capture_line in capture_lines:
            texture_override_vb_section.append(_capture_line)

        # 4) 顶层无条件 attach（每个 (部件, 槽) 一条 run；run 绝不进 if）
        #
        # 4b) 跨组合并（F1，开关 `cross_group_merged_vgmap_test`）：本组几何里被吸收
        #     进来的**别组**部件，其骨骼必须写进**本组**缓冲——绘制合并几何的 deform
        #     VS 绑的是本组缓冲（`vs-t0 = ..._G<本组>_s<槽>`），写进它自己组没有任何人
        #     读（实机症状：被吸收的顶点读到未初始化矩阵 ⇒ 塔陷 ⇒ 部件消失）。
        #     槽位 = 全局骨 id，缓冲是全宽（array = 全部骨骼数×3），因此跨组 attach
        #     与宿主 attach 不会写同一个槽，可以安全地叠在宿主 attach 之后。
        for slot in slots:
            for group_component_id in self._merged_group_component_ids(skeleton_group):
                texture_override_vb_section.append(
                    f"run = {self._merged_attach_name(group_component_id, slot)}"
                )
            for cross_component_id in self._merged_cross_group_component_ids(skeleton_group):
                texture_override_vb_section.append(
                    "run = "
                    + self._merged_cross_attach_name(
                        cross_component_id, skeleton_group, slot
                    )
                )
            # 4c) 本部件若**自己**是别组的跨组供骨件（F3）：在自己那段也跑一次「写宿主组
            #     缓冲」的跨组 attach。宿主那段同样会跑它，但读到的是本部件**上一帧**的
            #     palette（宿主排在前面时）⇒ 那一笔是陈旧的，必须在这里用当帧 palette 补。
            for host_group in self._merged_cross_host_group_map().get(int(component_id), []):
                texture_override_vb_section.append(
                    "run = "
                    + self._merged_cross_attach_name(component_id, int(host_group), slot)
                )

        # 4.5) 【已回退】F5 曾在此处（attach 之后）追加一次捕获以实现跨组同帧化，
        #      用户实测导致「两侧都抖、骨骼矩阵不对」——原因见 3) 上方注释。

        # 5) 默认换绑本组 s<slot_first> 骨架（每槽绘制按需按槽覆盖）
        texture_override_vb_section.append(
            f"vs-t0 = {self._merged_skeleton_name(skeleton_group, slot_first)}"
        )
        texture_override_vb_section.append("handling = skip")

        # 5b) 跨组供骨件自己那段的合并几何重放（F3）：与宿主那段同一套守卫条件，
        #     「最后一个到达的供骨件落笔」⇒ 与引擎提交顺序无关。
        self._append_merged_cross_group_replays(
            texture_override_vb_section, draw_ib, component_id, slots
        )

        # 6) 每槽守卫（体内只有绑定与 draw）
        #
        # 合并几何的归属（与 `_build_merged_mesh_redirect_plan` 同一口径）：
        # - 重放宿主（replay host）：由本挂点的每槽守卫重放整段合并几何（绑定
        #   carrier 的 vb0/vb2）——挂在 target 上，或 target 自身布局不兼容时挂在
        #   兼容的 carrier 上；
        # - 纯 carrier（几何被 target 吸收，且 target 挂点能承载重放）：deform
        #   退化为 3 顶点前缀 stub，写本槽 SO 的前缀行，不发守卫；
        # - 几何已被吸收、但本轮没有任何挂点能承载重放：不画（没有可见几何，
        #   也不该画自己的占位 stub）；
        # - 该组没有可行的重定向：几何留在**拥有导出几何的那个部件**自己的
        #   deform draw 上（直连路径）——几何只采样自己槽位时按「本部件出现次」
        #   自足绘制；几何跨部件（合并宿主）时把重放发给组内每个布局兼容的挂点
        #   （v9.1，不依赖引擎的提交顺序）。
        #
        # 无论哪条路径，`run = <CustomShader>` 都已在上面**顶层**无条件执行完毕
        # （if 内的 run 在本 fork 上不执行）。
        if group_plan is None:
            # 该组没有可行的重定向计划（未发生重定向，或计划被判为不可行）：
            # 几何留在承载部件自己的 deform draw 上（直连路径）。
            self._append_merged_direct_slot_guards(
                texture_override_vb_section,
                draw_ib,
                skeleton_group,
                slots,
                fallback_draw_number=int(getattr(drawib_model, "draw_number", 0) or 0),
                absorbed_hosts=absorbed_hosts,
            )
            return

        # 重定向载体但**不是本组的重放锚点**（自身 Blend 输入布局与锚点布局不同，
        # 例：叶瞬光01 G0 的载体 8c8de427 是 BW8_BI8，组内其余 5 个是 BW16_BI16）：
        #  - 本段必须写「本槽 SO 的 3 顶点前缀行」（渲染用 base_vertex 3 跳过）；
        #  - **但它自己的 IA 布局天然匹配自己的几何**：若本帧「最后一个必需部件」
        #    正好是它（实测会闪——守卫等齐全部必需部件时，别的锚点都已经过去了，
        #    没有任何落笔点），就用**原生 vb2/vb0** 在这里重放一次。
        # 守卫条件仍是 required 相与、且锚点集合 ⊆ required，因此一帧内仍只可能在
        # 「最后一个必需部件」那一处闭合一次，SO 恰好写一份 [前缀 3 行][合并行]。
        if redirect_carrier is not None and not self._merged_component_layout_compatible(
            draw_ib, group_plan
        ):
            self._append_merged_carrier_prefix_stub(
                texture_override_vb_section, draw_ib, group_plan, group_target
            )
            carrier_count = int(
                (self._redirect_carrier_map.get(draw_ib) or {}).get("vertex_count", 0) or 0
            )
            if (
                carrier_count > 0
                and group_plan.get("so_owner_ib") == draw_ib
                and not group_plan.get("target_has_real_geometry", True)
            ):
                guard_component_ids = group_plan.get("required_component_ids") or (
                    self._merged_group_component_ids(skeleton_group)
                )
                for slot in slots:
                    texture_override_vb_section.append(
                        f"if {self._merged_slot_guard_condition(guard_component_ids, skeleton_group, slot)}"
                    )
                    texture_override_vb_section.append(
                        f"    vs-t0 = {self._merged_skeleton_name(skeleton_group, slot)}"
                    )
                    texture_override_vb_section.append(
                        f"    so0 = ref {self._merged_redirect_so_name(skeleton_group, slot)}"
                    )
                    texture_override_vb_section.append(
                        f"    vb2 = Resource{draw_ib}Blend"
                    )
                    texture_override_vb_section.append(
                        f"    vb0 = Resource{draw_ib}Position"
                    )
                    texture_override_vb_section.append(
                        f"    draw = {carrier_count}, 0"
                    )
                    texture_override_vb_section.append("    so0 = null")
                    texture_override_vb_section.append("endif")
            return

        target_viable = self._merged_target_viable_as_replay_host(
            group_target, group_plan
        )
        if self._merged_component_layout_compatible(draw_ib, group_plan):
            if target_viable:
                # v9（用户实测口径）：重定向可行时，**每个布局兼容的组内部件挂点都发
                # 同一套每槽守卫** —— 守卫的触发时机可能落在组内任意部件的 deform 段
                # （取决于引擎提交次序，两个实例的 pass 次序甚至可能相反）。只让单一
                # 挂点持有守卫时，若该挂点先于组内其它部件 deform，守卫永不触发 →
                # 该槽 SO 只剩 3 顶点前缀 → 合并几何整段消失（重新导出的实测回归）。
                # 每槽守卫的条件是「几何引用的骨骼所属部件该槽当帧到达」（
                # `required_component_ids`），因此一帧内只会在最后一个必需部件
                # 到达处闭合一次；多锚点是把「落笔的那个挂点」从固定变成按提交
                # 顺序自适应，不是重复写入。
                self._append_merged_carrier_prefix_stub(
                    texture_override_vb_section, draw_ib, group_plan, group_target
                )
                self._append_merged_anchor_own_geometry_draw(
                    texture_override_vb_section,
                    draw_ib,
                    component_id,
                    skeleton_group,
                    slots,
                    group_plan,
                )
                self._append_merged_target_slot_guards(
                    texture_override_vb_section,
                    group_target,
                    skeleton_group,
                    slots,
                    consumer_ib=draw_ib,
                )
                return
            # target 挂点不可行：由兼容锚点承载重放。锚点集合 = required 里布局
            # 兼容的部件（见 _build_merged_mesh_redirect_plan），因此只要有一个锚点
            # 排在最后一个必需部件之后就能落笔；锚点集合只有 1 个时才需要报警。
            # 前缀 stub 仍必须写（base_vertex 由 _redirect_plan_prefix_rows 取 3，
            # 渲染从 SO 第 3 行读本段，不写就整体错位 3 行 = 爆炸）。
            if self._merged_component_can_host_replay(
                draw_ib, group_plan, group_target, target_viable
            ):
                self._append_merged_carrier_prefix_stub(
                    texture_override_vb_section, draw_ib, group_plan, group_target
                )
                self._append_merged_anchor_own_geometry_draw(
                    texture_override_vb_section,
                    draw_ib,
                    component_id,
                    skeleton_group,
                    slots,
                    group_plan,
                )
                self._append_merged_target_slot_guards(
                    texture_override_vb_section,
                    group_target,
                    skeleton_group,
                    slots,
                    consumer_ib=draw_ib,
                )
                if len(group_plan.get("compatible_component_ids") or []) <= 1:
                    print(
                        "⚠️ [ZZMI骨骼合并] 合并几何重放锚点唯一（回退路径）: DrawIB "
                        f"{draw_ib}（骨架组 G{skeleton_group}）是 target "
                        f"{group_target} 布局不兼容时唯一可承载重放的挂点。"
                        "影响：守卫只在该挂点排在本组必需部件之后才闭合；排在前面时"
                        "该帧合并几何不写 → 模型闪/局部缺失。"
                        "处置：把合并几何挂到组内 Blend 布局占多数的部件上重新导出"
                        "（生成器会为多数布局的每个挂点都发守卫，与提交顺序无关）。"
                    )
                return

        if (
            draw_ib not in self._redirect_carrier_map
            and self._merged_component_geometry_absorbed(skeleton_group, draw_ib)
        ):
            # 几何已被组内其它部件合并走，且本轮已确定由某个挂点重放：本部件没有
            # 可见几何，不能在这里再画自己的占位 stub（否则重复绘制 / 画出占位
            # 小三角）。
            return

        # 其余情况（几何未被吸收的组内部件；或 target 挂点不可行时由兼容 carrier
        # 兜底）：合并几何必须由本挂点自己画，否则会整体消失。仍用同一套
        # 「组内部件当帧全部到达」门控。
        self._append_merged_direct_slot_guards(
            texture_override_vb_section,
            draw_ib,
            skeleton_group,
            slots,
            fallback_draw_number=int(getattr(drawib_model, "draw_number", 0) or 0),
        )

    def _merged_component_is_so_owner(self, draw_ib: str, group_plan: dict | None) -> bool:
        """本部件是不是本组 SO（重定向缓冲）的 owner。

        owner 的变形输出缓冲 = 合并几何的落点（重放写进它），非 owner 的挂点重放
        写的是**别人**的 SO，因此它**自己**的 SO 仍需被自己填。
        """
        if not group_plan:
            return False
        return group_plan.get("so_owner_ib") == draw_ib

    def _append_merged_anchor_own_geometry_draw(
        self,
        section,
        draw_ib: str,
        component_id: int,
        skeleton_group: int,
        slots,
        group_plan: dict | None,
    ) -> None:
        """挂点自己的 SO 也要写：按本轮出现次自足绘制本部件自己的几何。

        为什么必须写（2026-09-16 实测事故）：重定向只在 **SO owner** 的缓冲里生成
        合并几何；其它挂点（尤其是被吸收后只剩 3 顶点占位桩的部件，如 `9258d5f8`）
        的重放写的是 owner 的 SO，而游戏渲染 draw 读的是**本部件自己的 SO**
        （`base_vertex = 0`）。若本段只发守卫不画自己，本部件的 SO 会一直残留
        mod 之前的旧数据 → 渲染出几个乱飞的三角（表现为"整个模型炸开 + 不停闪"）。
        owner 不需要（它的 SO 由 [前缀 stub][重放] 填满）。
        """
        if self._merged_component_is_so_owner(draw_ib, group_plan):
            return
        own_count = int(self._drawib_exported_vertex_count(draw_ib) or 0)
        if own_count <= 0:
            return
        occ_var = self._merged_occ_var(int(component_id))
        for slot in slots:
            section.append(f"if {occ_var} == {slot}")
            section.append(
                f"    vs-t0 = {self._merged_skeleton_name(skeleton_group, slot)}"
            )
            section.append(f"    draw = {own_count}, 0")
            section.append("endif")

    def _append_merged_carrier_prefix_stub(
        self, section, draw_ib: str, group_plan: dict, group_target: str | None
    ) -> None:
        """重定向载体写本槽 SO 的前缀 stub（仅 SO owner 且不是 target 时）。

        前缀行数由 `_redirect_plan_prefix_rows` 决定（纯占位 target = 3 行），
        `base_vertex` / Redirect Texcoord pad / override_vertex_count 与它同源；
        这里漏写就会让渲染从没写过的行开始读（整体错位 = 爆炸）。
        """
        if group_plan.get("so_owner_ib") == draw_ib and draw_ib != group_target:
            section.append("draw = 3, 0")

    @staticmethod
    def _merged_target_viable_as_replay_host(target_ib: str, group_plan: dict) -> bool:
        """target 挂点本身能否承载整段重放。

        两个否决条件（与 `_build_merged_mesh_redirect_plan` 同源）：
        - Blend 输入布局不兼容（`compatible_component_ids` 不含 target）；
        - 必需骨骼依赖到达晚于所有兼容宿主（计划记为 `unredirected`，
          `$zz_ms_seen_*` 在 target 挂点永远不会全部成立）。
        """
        if not target_ib:
            return False
        compatible_ids = group_plan.get("compatible_component_ids")
        if compatible_ids is not None:
            target_component_id = group_plan.get("target_component_id")
            if target_component_id is not None and int(target_component_id) not in [
                int(cid) for cid in compatible_ids
            ]:
                return False
        return bool(group_plan.get("target_viable", True))

    def _merged_component_layout_compatible(self, draw_ib: str, group_plan: dict) -> bool:
        """本部件挂点的输入布局是否与合并几何兼容（BI4 与 BW16_BI16 不能混用）。

        v9 起：**兼容即发守卫**（不再区分 target / carrier 只留一个挂点）——见调用处
        注释：守卫的触发时机可能落在组内任意部件的 deform 段上。
        """
        component_id = self.merged_skeleton_component_id_dict.get(draw_ib)
        if component_id is None:
            return False
        compatible_ids = group_plan.get("compatible_component_ids")
        if compatible_ids is None:
            allowed = [int(cid) for cid in group_plan.get("required_component_ids", [])]
        else:
            allowed = [int(cid) for cid in compatible_ids]
        return int(component_id) in allowed

    def _merged_component_can_host_replay(
        self, draw_ib: str, group_plan: dict, target_ib: str, target_viable: bool
    ) -> bool:
        """本部件挂点能否承载该组的整段重放（BI4 与 BW16_BI16 不能混用）。

        - 本部件就是 target：只要 target 自身可行即可；
        - 本部件是兼容的 carrier：仅当 target 挂点不可行（布局不兼容 / 依赖到达
          过晚）时才由它兜底重放，否则合并几何统一由 target 挂点重放一次。
        """
        component_id = self.merged_skeleton_component_id_dict.get(draw_ib)
        if component_id is None:
            return False
        compatible_ids = group_plan.get("compatible_component_ids")
        if compatible_ids is None:
            allowed = [
                int(cid) for cid in group_plan.get("required_component_ids", [])
            ]
        else:
            allowed = [int(cid) for cid in compatible_ids]
        if int(component_id) not in allowed:
            return False
        if draw_ib == target_ib:
            return bool(target_viable)
        return not target_viable

    def _merged_component_geometry_absorbed(
        self, skeleton_group: int, draw_ib: str
    ) -> bool:
        """本部件引用的骨骼是否包含**组内其它部件**的槽位（= 几何已被吸收）。

        与 `_build_merged_mesh_redirect_plan` 判定 carrier 的口径一致：
        `(引用的骨骼 id - 本部件 vg_map 值集合) ∩ 本组合法槽位` 非空即被吸收。
        被吸收的部件没有自己的可见几何（它的行已经写进 target 的对象里），
        渲染侧不能重复绘制。
        """
        component_id = self.merged_skeleton_component_id_dict.get(draw_ib)
        if component_id is None:
            return False
        component = self.merged_skeleton_components[int(component_id)]
        own = set((component.get("vg_map") or {}).values())
        legal: set[int] = set()
        for other_id in self._merged_group_component_ids(skeleton_group):
            other = self.merged_skeleton_components[other_id]
            legal.update(
                range(
                    int(other["vg_offset"]),
                    int(other["vg_offset"]) + int(other["vg_count"]),
                )
            )
        absorbed = (self._collect_drawib_referenced_bone_ids(draw_ib) - own) & legal
        return bool(absorbed)

    def _merged_group_redirect_target(self, skeleton_group: int) -> str | None:
        """本骨架组被重定向到的 target DrawIB；该组未发生重定向时返回 None。

        一组至多一个 target（= 组内最后一个 deform draw，见
        `_build_merged_mesh_redirect_plan`）。
        """
        group_component_ids = set(self._merged_group_component_ids(skeleton_group))
        for target_ib, plan in self._redirect_target_map.items():
            target_component_id = self.merged_skeleton_component_id_dict.get(target_ib)
            if (
                target_component_id is not None
                and int(target_component_id) in group_component_ids
            ):
                return target_ib
        return None

    def _merged_so_owner_target_ibs(self, draw_ib: str) -> list[str]:
        """本部件作为 SO owner 时要捕获 SO 的 target 列表（升序）。

        - target 有真实几何：由 target **自己**的 deform 段捕获 SO（此时
          `so_owner_ib == target_ib`，本函数同样返回该 target）；
        - 纯占位 target：SO 必须由一个真实 carrier 挂点拥有，否则 target 晚到时
          会把空 SO / BI4 布局的 SO 覆盖掉有效内容。
        只有 owner 挂点才能写 `ResourceZZRedirectSO_s<k> = ref so0`。
        """
        return sorted(
            target_ib
            for target_ib, plan in self._redirect_target_map.items()
            if plan.get("so_owner_ib") == draw_ib
        )

    def _append_merged_target_slot_guards(
        self,
        section,
        target_ib: str,
        skeleton_group: int,
        slots,
        consumer_ib: str = "",
    ) -> None:
        """重放宿主挂点的每槽守卫（绑定 carrier 的 vb0/vb2 + 本槽 SO）。

        调用方已保证本挂点在 `compatible_component_ids` 内（BI4 与 BW16_BI16
        不能混用；不兼容的挂点不进入本函数）。
        """
        plan = self._redirect_target_map[target_ib]
        guard_component_ids = plan.get("required_component_ids") or (
            self._merged_group_component_ids(skeleton_group)
        )
        for slot in slots:
            # 每槽守卫：本槽 SO 别名当帧已（由捕获者）刷新 + 几何引用的骨骼所属
            # 部件在该槽都已当帧到达（或按上一帧预测本槽不会到）才重放；if 内只有
            # 绑定与 draw（说明留源码，不写进配置表）
            # O3：消费点标记（注释行；守卫条件与块体一字不改 ⇒ 逐字节不变量）
            self._append_merged_diag(
                section,
                self._merged_reuse_site(
                    consumer_ib or target_ib, skeleton_group, slot, "replay-draw"
                ),
            )
            section.append(
                f"if {self._merged_slot_guard_condition(guard_component_ids, skeleton_group, slot)}"
            )
            section.append(
                f"    vs-t0 = {self._merged_skeleton_name(skeleton_group, slot)}"
            )
            section.append(
                f"    so0 = ref {self._merged_redirect_so_name(skeleton_group, slot)}"
            )
            for vb0_resource, vb2_resource, draw_count in plan.get("deform_draws", []):
                section.append(f"    vb2 = {vb2_resource}")
                section.append(f"    vb0 = {vb0_resource}")
                section.append(f"    draw = {int(draw_count)}, 0")
            section.append("    so0 = null")
            section.append("endif")

    def _merged_direct_draw_is_self_contained(
        self, skeleton_group: int, draw_ib: str
    ) -> bool:
        """本部件 deform 段画的几何是否**只依赖自己的槽位**（= 无需等组内其它部件）。

        自足 = 该部件不是「几何被吸收」的挂点：它导出的 VB 里只有自己的顶点，
        顶点采样的全局槽位全部被自己的 vg_map 覆盖 ⇒ 本段 attach 用当帧 palette
        写进本槽之后，本槽骨架对这段几何就是完整的（跨部件共享 canonical 槽位
        的取值按导出期去重口径 bitwise 相同，谁写都一样）。
        """
        if draw_ib not in self.merged_skeleton_component_id_dict:
            return False
        return not self._merged_component_geometry_absorbed(skeleton_group, draw_ib)

    def _merged_group_absorbed_hosts(self, skeleton_group: int) -> list[dict]:
        """本组「几何被吸收」的挂点：跨部件的合并几何就挂在这些 DrawIB 的导出 VB 上。

        它们画的几何引用组内其它部件的骨骼 ⇒ 必须等全组当帧到位才能画。返回
        `[{"component_id", "draw_ib", "draw_count"}]`（按组件号升序）。
        """
        hosts: list[dict] = []
        for component_id in self._merged_group_component_ids(skeleton_group):
            component = self.merged_skeleton_components[int(component_id)]
            draw_ib = str(component["draw_ib"])
            if not self._merged_component_geometry_absorbed(skeleton_group, draw_ib):
                continue
            draw_count = int(self._drawib_exported_vertex_count(draw_ib) or 0)
            if draw_count <= 0:
                continue
            hosts.append(
                {
                    "component_id": int(component_id),
                    "draw_ib": draw_ib,
                    "draw_count": draw_count,
                }
            )
        return hosts

    def _merged_absorbed_replay_compatible(self, draw_ib: str, host_ib: str) -> bool:
        """本挂点能否重放宿主导出的合并几何（Blend 输入布局必须一致）。

        重放在本挂点的 IA 状态下执行、绑定宿主的 vb0/vb2：BI4 与 BW16_BI16
        混用时 BLENDINDICES 会被按错误格式解释，流输出通常直接全零。布局元数据
        缺失时按「不可重放」处理（保守方向：退回只在宿主自己段重放）。
        """
        if draw_ib == host_ib:
            return True
        here = self._drawib_blend_layout_signature(draw_ib)
        there = self._drawib_blend_layout_signature(host_ib)
        return here is not None and here == there

    def _append_merged_absorbed_replay(
        self,
        section,
        host: dict,
        skeleton_group: int,
        slot: int,
        consumer_ib: str = "",
    ) -> None:
        """组级守卫的合并几何重放：把宿主导出的合并几何写进它捕获的 SO。

        宿主在自己的 deform 段顶层把本轮 SO 引用捕获到
        `ResourceZZRedirectSO_s<k>`；本块的守卫条件保证**该宿主**当帧在该槽已
        到达（`_merged_so_ready_condition`，别名一定是本帧刷新的）+ 组内期望集合
        已到齐，因此任何布局兼容的挂点闭合守卫后都能写。
        """
        gate = self._merged_so_ready_condition([int(host["component_id"])], slot)
        # 跨组供骨件（F3）：合并几何里被吸收的**别组**顶点，其骨骼由跨组 attach 写进
        # 本组缓冲，而那个 attach 读的是**跨组件自己那一段**捕获的 palette。守卫若不
        # 等它们到达，就只可能在宿主自己那段闭合——而宿主常常排在跨组件**之前**
        # （2026-10-10 FrameAnalysis 实测 洛克茜：宿主 d5e1e7ff deform=000020，跨组件
        # 4e6d8882 deform=000023）⇒ 合并几何是用**上一帧**的跨组 palette 画出来的 =
        # 用户实测的「一帧延迟 + 运动时错位」（静止时上一帧 == 当帧，所以看不出来）。
        # 把跨组件并入到达集合后，守卫最早只能在「最后一个供骨件到达」处闭合；跨组件
        # 自己那段也会发同一套守卫（`_append_merged_cross_group_replays`），落笔点
        # 因此自动跟着引擎的提交顺序走。开关关闭时 `_merged_cross_group_component_ids`
        # 返回空 ⇒ 条件与历史逐字一致。
        clauses = self._merged_slot_seen_condition(
            self._merged_group_component_ids(skeleton_group)
            + self._merged_cross_group_component_ids(skeleton_group),
            slot,
            cull_aware=True,
        )
        # O3：消费点标记（注释行；守卫条件与块体一字不改 ⇒ 逐字节不变量）
        self._append_merged_diag(
            section,
            self._merged_reuse_site(
                consumer_ib or str(host["draw_ib"]), skeleton_group, slot, "replay-absorbed"
            ),
        )
        section.append(
            f"if {self._merged_absorb_redundant_seen_clauses(' && '.join(p for p in (gate, clauses) if p))}"
        )
        section.append(f"    vs-t0 = {self._merged_skeleton_name(skeleton_group, slot)}")
        section.append(
            f"    so0 = ref {self._merged_redirect_so_name(skeleton_group, slot)}"
        )
        section.append(f"    vb2 = Resource{host['draw_ib']}Blend")
        section.append(f"    vb0 = Resource{host['draw_ib']}Position")
        section.append(f"    draw = {int(host['draw_count'])}, 0")
        section.append("    so0 = null")
        section.append("endif")

    def _append_merged_cross_group_replays(
        self, section, draw_ib: str, component_id: int, slots
    ) -> None:
        """跨组供骨件**自己那段**的合并几何重放（F3，开关 `cross_group_merged_vgmap_test`）。

        为什么必须由跨组件自己再发一份（2026-10-10 FrameAnalysis 实证，洛克茜复现件）：
        合并几何挂在宿主（d5e1e7ff，G3）的导出 VB 上，而它的骨骼来自 G3 自己 +
        被吸收的 G2/G0 部件。宿主那段的守卫现在要等**全部供骨件**到达（见
        `_append_merged_absorbed_replay`），而引擎把宿主排在跨组件之前时（实测
        deform 提交次序 000017 4e6e989e → 000020 宿主 → 000023 4e6d8882），宿主
        那段永远等不到 ⇒ 若这里不再发一份，守卫整帧不闭合、合并几何整段不写。

        守卫条件与宿主那段**逐字相同** ⇒ 一帧内只会在「最后一个供骨件到达」处闭合
        一次（不会重复写 SO，不会翻倍顶点）。落笔点从「固定在宿主」变成「按提交
        顺序自适应」，这是 v9.1 对**组内**挂点已经采用的同一口径，这里只是把它
        扩到**跨组**供骨件。
        """
        for host_group in self._merged_cross_host_group_map().get(int(component_id), []):
            if self._merged_group_redirect_target(int(host_group)):
                # 该宿主组走重定向路径：合并几何的落笔点由它自己的计划（载体/锚点）
                # 决定，不在这里叠加第二套。
                continue
            for host in self._merged_group_absorbed_hosts(int(host_group)):
                if not self._merged_absorbed_replay_compatible(
                    draw_ib, str(host["draw_ib"])
                ):
                    # Blend 输入布局与宿主不一致（BI4 vs BW16_BI16 之类）：重放会把
                    # BLENDINDICES 按错误格式解释 ⇒ 流输出全零。退回宿主自己那段。
                    continue
                for slot in slots:
                    self._append_merged_absorbed_replay(
                        section, host, int(host_group), slot, consumer_ib=draw_ib
                    )

    def _append_merged_direct_slot_guards(
        self,
        section,
        draw_ib: str,
        skeleton_group: int,
        slots,
        fallback_draw_number: int = 0,
        absorbed_hosts: list[dict] | None = None,
    ) -> None:
        """直连路径（无 SO 重定向）的每槽绘制。

        vb0/vb2 沿用本部件自己的绑定（几何就是从本部件导出的 VB 读的）或按宿主
        显式绑定，各分支体内都只有绑定与 draw——满足「if 内不得 run / 不得给
        $变量赋值」。

        draw 顶点数取本 DrawIB 的**导出顶点数**（所有子网格导出顶点之和 =
        本部件 VB 里的实际行数）——不用 draw_number：合并几何是从导出 buffer 读的，
        超出原部件顶点数的部分正是被合并进来的其它部件几何，按原部件顶点数画会
        截掉它们。只有导出顶点数为 0 的测试桩/空变体才回退到 `fallback_draw_number`
        （保持旧行为，避免旧工作空间突然不画）。

        三种角色（判据见 `_merged_direct_draw_is_self_contained` /
        `_merged_group_absorbed_hosts`）：

        1. **自足挂点**（本部件几何只采样自己的槽位、本组没有合并宿主）：按本轮
           出现次绑定本槽骨架后**无条件绘制**，绝不使用组级 seen 门控。理由
           （2026-09-13 FrameAnalysis 实证）：组级门控只可能在组内**最后一个**
           到达的部件那段成立，而每个部件的几何只有它自己的 deform 段能画（那一
           笔的 VB/SO 绑定只在该段有效、且该段被 `handling = skip` 吃掉了原
           draw）⇒ 先到的部件整帧没有变形输出，渲染只能读到旧内容/零值 → 模型随
           引擎提交顺序逐帧闪/消失。自足挂点自己的槽位已由本段 attach 写全，等
           其它部件没有任何正确性收益。
        2. **合并宿主**（本部件导出 VB 上挂着跨部件的合并几何）：自己的几何**必须
           等全组当帧到位**（半帧拼接的骨架会把跨部件几何画错位/塌陷）→ 组级
           seen 守卫；体内显式绑定自己捕获的 SO（`ResourceZZRedirectSO_s<k>`）与
           自己的 vb0/vb2。
        3. **普通挂点 + 本组有合并宿主**：先按第 1 条自足画自己的几何，再对本组
           每个布局兼容的宿主各发一条第 2 条的重放——**哪个挂点最后到达每帧都
           可能不同**，只让宿主自己发重放时，宿主先 deform 的帧里合并几何整段
           消失（用户实测"合并之后还在闪"）。
        """
        draw_count = int(self._drawib_exported_vertex_count(draw_ib) or 0)
        if draw_count <= 0:
            draw_count = int(fallback_draw_number or 0)
        if draw_count <= 0:
            # 该变体下没有任何可画的合并几何：不发守卫，避免 `draw = 0, 0`。
            return

        hosts = list(absorbed_hosts or [])
        own_host = next(
            (host for host in hosts if str(host["draw_ib"]) == str(draw_ib)), None
        )

        if own_host is not None:
            for slot in slots:
                self._append_merged_absorbed_replay(
                    section, own_host, skeleton_group, slot, consumer_ib=draw_ib
                )
            group_component_ids = self._merged_group_component_ids(skeleton_group)
            if len(group_component_ids) > 1 and not any(
                self._merged_absorbed_replay_compatible(
                    str(self.merged_skeleton_components[int(other_id)]["draw_ib"]),
                    str(draw_ib),
                )
                for other_id in group_component_ids
                if int(other_id) != int(own_host["component_id"])
            ):
                print(
                    "⚠️ [ZZMI骨骼合并] 合并宿主 " + str(draw_ib) + " 在本组内没有 Blend "
                    "布局兼容的其它挂点：合并几何只能由它自己的 deform 段重放，"
                    "若引擎把它的 deform pass 排在组内其它部件之前，该帧合并几何不写 "
                    "→ 模型闪/消失。影响：合并（join 成一个物体）的导出。"
                    "处置：把这条打印发给开发者（需要按 IA 布局兼容性放宽重放挂点）。"
                )
            return

        if self._merged_direct_draw_is_self_contained(skeleton_group, draw_ib):
            occ_var = self._merged_occ_var(
                int(self.merged_skeleton_component_id_dict[draw_ib])
            )
            replay_hosts = [
                host
                for host in hosts
                if self._merged_absorbed_replay_compatible(draw_ib, str(host["draw_ib"]))
            ]
            for slot in slots:
                # 直连路径自足挂点：按本轮出现次绑本槽骨架后直接绘制本部件几何
                # （说明留源码，不写进配置表）
                section.append(f"if {occ_var} == {slot}")
                section.append(
                    f"    vs-t0 = {self._merged_skeleton_name(skeleton_group, slot)}"
                )
                section.append(f"    draw = {draw_count}, 0")
                section.append("endif")
                for host in replay_hosts:
                    self._append_merged_absorbed_replay(
                        section, host, skeleton_group, slot, consumer_ib=draw_ib
                    )
            return

        for slot in slots:
            # 每槽守卫（直连路径/吸收挂点）：本槽 SO 别名当帧已被宿主刷新 + 本组
            # 期望集合在该槽已到达才绘制（说明留源码，不写进配置表）
            section.append(
                f"if {self._merged_slot_guard_condition(self._merged_group_component_ids(skeleton_group), skeleton_group, slot)}"
            )
            section.append(
                f"    vs-t0 = {self._merged_skeleton_name(skeleton_group, slot)}"
            )
            section.append(f"    draw = {draw_count}, 0")
            section.append("endif")

    def _zzmi_pose_expand_groups(self, drawib_model):
        """同一 DrawIB 的多个 SubMesh 若使用**不同的预蒙皮 draw**，返回需要各自成组的组件列表。

        背景：SSMT 的 ini 机制原本假设「一个 DrawIB 只有一次预蒙皮 draw」，
        因此只按 first_submesh 的 CategoryHash 生成一组 VB 覆盖段。像翅膀喷射器这种
        一个 IB 配了 4 次预蒙皮 draw 的部件，会导致只有其中 1 个部件被替换、其余保持原样。

        本方法把「各 SubMesh 的 CategoryHash 是否相同」作为判据：
        - 全部相同（正常角色） → 返回 []，调用方走原逻辑，行为与改动前完全一致；
        - 存在不同（一个 IB 多次预蒙皮） → 返回 [(后缀, CategoryHash 字典, 顶点数), ...]。

        返回元素的后缀取自 SubMesh 的 workspace_unique_str 末段（例如 `000014`）。
        """
        try:
            from ...common.submesh_metadata import SubmeshMetadataResolver
        except Exception:
            return []

        stride_dict = drawib_model.d3d11GameType.CategoryStrideDict
        position_stride = int(stride_dict.get("Position", 0) or 0)
        if position_stride <= 0:
            return []

        groups = []
        seen_keys = set()
        for submesh_model in drawib_model.submesh_model_list:
            unique_str = (
                getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            )
            try:
                submesh_metadata = SubmeshMetadataResolver.resolve(unique_str)
                category_hash_dict = dict(
                    getattr(submesh_metadata, "category_hash_dict", {}) or {}
                )
            except Exception:
                return []
            if not category_hash_dict:
                return []

            hash_key = tuple(sorted((k, str(v)) for k, v in category_hash_dict.items()))
            if hash_key in seen_keys:
                continue
            seen_keys.add(hash_key)

            position_buffer = (
                getattr(submesh_model, "category_buffer_dict", {}) or {}
            ).get("Position")
            if position_buffer is None:
                return []
            vertex_count = len(position_buffer) // position_stride
            if vertex_count <= 0:
                return []

            groups.append((str(unique_str).rsplit("-", 1)[-1], category_hash_dict, vertex_count))

        # 只有 1 组（含：多个 SubMesh 共用同一次预蒙皮）时不展开，保持原行为
        if len(groups) <= 1:
            return []
        return groups

    def _zzmi_expand_render_representative(self, drawib_model, expand_groups) -> str:
        """展开组里「能代表全部组件」的渲染段后缀；空串 = 必须保留每个组件各自的段。

        ## 为什么需要它（2026-10-14 用户实机现象：推进器本身高频闪烁 + 多出一片扭曲网格）

        实测数据链（FrameAnalysis 2026-10-13-170438 + mod 产物逐字节核对）：

        - 游戏里同一个 IB（如 ``760c8064``）在一帧内被 render **4 次**，四次
          ``DrawIndexed(IndexCount:918, StartIndexLocation:0, BaseVertexLocation:0)``
          **参数完全相同**，唯一的区别是 vb0 指向**各自实例**的预蒙皮 SO 输出
          （实测每个实例 vb0 hash 不同、都只有 ``count=277`` 个顶点）；
        - 游戏原始 IB 的索引值域是 ``0..276``（R16，918 个索引）；
        - 而展开模式给 4 个组件各发了一个 render 段（``hash`` 与
          ``match_first_index`` 都相同）⇒ **每次实例 draw 都会把 4 个组件全部重画**；
        - 组件 2/3/4 的索引被加了 ``277/554/831`` 的顶点段偏移（实测归一化后与
          组件 1 逐字节相同），而当前 vb0 只有 277 顶点 ⇒ 后 3 份索引**越界**读出
          垃圾 ⇒ 那一片「扭曲的网格」，再叠加深度冲突 ⇒ 高频闪烁。

        ## 判据（只放宽「已证明是同一套索引」的情形）

        仅当**全部组件的索引在减去各自段偏移后完全相同**（同一套三角形，只是被加了
        顶点段偏移）**且代表组件本身的值域已从 0 开始**时，才返回该后缀 ——
        此时每次实例 draw 用「本实例的 277 顶点 + 这一份归一化索引」即可正确画出该
        实例，其余组件的几何由**它们各自实例的那次 render** 承担，一份都不会丢。

        任何一条不满足（缺 ib / 索引拓扑不同 / 代表组件值域不从 0 开始）都返回空串，
        调用方保持「每个组件各发一段」的原有行为 —— 宁可不改，也不误伤
        「同一 IB 的不同部件需要各自渲染」的其它用法。
        """
        suffixes = [group[0] for group in expand_groups]
        if len(suffixes) < 2:
            return ""
        ib_dict = getattr(drawib_model, "submesh_ib_dict", {}) or {}
        signature = None
        representative = ""
        representative_ib = None
        for submesh_model in drawib_model.submesh_model_list:
            unique_str = (
                getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            )
            suffix = str(unique_str).rsplit("-", 1)[-1]
            if suffix not in suffixes:
                continue
            ib_buffer = ib_dict.get(getattr(submesh_model, "unique_str", unique_str))
            if ib_buffer is None or len(ib_buffer) == 0:
                return ""
            try:
                import numpy
                indices = numpy.asarray(ib_buffer, dtype=numpy.int64).reshape(-1)
            except Exception:
                return ""
            if indices.size == 0:
                return ""
            normalized = numpy.sort(indices - int(indices.min()))
            digest = normalized.tobytes()
            if signature is None:
                signature = digest
                representative = suffix
                representative_ib = indices
            elif digest != signature:
                # 索引拓扑不同 = 不同部件，必须各自渲染
                return ""
        if not representative or representative_ib is None:
            return ""
        if int(representative_ib.min()) != 0:
            # 代表组件的索引不从 0 开始 ⇒ 单段渲染同样会越界，保持原行为
            return ""
        return representative

    def generate_buffer_files(self, output_folder: str):
        """基类写出合并缓冲之后，为「多次预蒙皮 draw」的每个组件另写一份独立缓冲。

        仅当 _zzmi_pose_expand_groups 判定需要展开时才写；正常角色不会多出任何文件。
        """
        super().generate_buffer_files(output_folder)

        for drawib_model in self.drawib_model_list:
            expand_groups = self._zzmi_pose_expand_groups(drawib_model)
            if not expand_groups:
                continue

            suffix_submesh_dict = {}
            for submesh_model in drawib_model.submesh_model_list:
                unique_str = (
                    getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
                )
                suffix_submesh_dict[str(unique_str).rsplit("-", 1)[-1]] = submesh_model

            for component_suffix, _category_hash_dict, _vertex_count in expand_groups:
                submesh_model = suffix_submesh_dict.get(component_suffix)
                if submesh_model is None:
                    continue
                for category_name, category_buffer in (
                    getattr(submesh_model, "category_buffer_dict", {}) or {}
                ).items():
                    buffer_path = os.path.join(
                        output_folder,
                        drawib_model.draw_ib
                        + "-"
                        + category_name
                        + "-"
                        + component_suffix
                        + ".buf",
                    )
                    with open(buffer_path, "wb") as file_obj:
                        category_buffer.tofile(file_obj)

    def add_unity_vs_texture_override_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        d3d11_game_type = drawib_model.d3d11GameType
        draw_ib = drawib_model.draw_ib

        # 一个 DrawIB 配了多次预蒙皮 draw 时（例如翅膀的 4 个推进器），
        # 为每个组件各生成一组 VB 覆盖段；否则走下面的原逻辑（行为与改动前一致）。
        expand_groups = self._zzmi_pose_expand_groups(drawib_model)
        if (
            expand_groups
            and not self.has_cross_ib
            and not self.merged_skeleton_component_id_dict.get(draw_ib)
            and not self.blueprint_model.keyname_mkey_dict.keys()
        ):
            texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
            texture_override_vb_section.append("; " + draw_ib + " (按预蒙皮 draw 展开)")
            for component_suffix, category_hash_dict, component_vertex_count in expand_groups:
                for category_name in d3d11_game_type.OrderedCategoryNameList:
                    vb_name_suffix = (
                        "VB_" + draw_ib + "_" + drawib_model.draw_ib_alias
                        + "_" + component_suffix + "_" + category_name
                    )
                    texture_override_vb_section.append("[TextureOverride_" + vb_name_suffix + "]")
                    texture_override_vb_section.append(
                        "hash = " + category_hash_dict.get(category_name, "")
                    )

                    for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                        if category_name != draw_category_name:
                            continue
                        category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                        texture_override_vb_section.append(
                            category_original_slot
                            + " = Resource"
                            + draw_ib
                            + original_category_name
                            + "_"
                            + component_suffix
                        )

                    blend_draw_category_name = d3d11_game_type.CategoryDrawCategoryDict.get("Blend", None)
                    if blend_draw_category_name is not None and category_name == blend_draw_category_name:
                        texture_override_vb_section.append("handling = skip")
                        texture_override_vb_section.append("draw = " + str(component_vertex_count) + ", 0")

                    texture_override_vb_section.new_line()

            ini_builder.append_section(texture_override_vb_section)
            return

        so0_source_resource_names = []
        for submesh_model in drawib_model.submesh_model_list:
            source_ib_key = self._get_submesh_ib_key(submesh_model, draw_ib)
            if self.CROSS_IB_METHOD_VB_REF_SO0 in self._get_source_methods(source_ib_key):
                so0_source_resource_names.append(
                    self._get_source_so0_resource_name(draw_ib, submesh_model.match_first_index)
                )

        texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
        texture_override_vb_section.append("; " + draw_ib)
        for category_name in d3d11_game_type.OrderedCategoryNameList:
            category_hash = drawib_model.category_hash_dict.get(category_name, "")
            texture_override_vb_name_suffix = "VB_" + draw_ib + "_" + drawib_model.draw_ib_alias + "_" + category_name
            texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_name_suffix + "]")
            texture_override_vb_section.append("hash = " + category_hash)

            for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                if category_name != draw_category_name:
                    continue
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_vb_section.append(category_original_slot + " = Resource" + draw_ib + original_category_name)

            draw_category_name = d3d11_game_type.CategoryDrawCategoryDict.get("Blend", None)
            if draw_category_name is not None and category_name == draw_category_name:
                if self.merged_skeleton_component_id_dict.get(draw_ib) is not None:
                    self._append_merged_skeleton_deform_block(
                        texture_override_vb_section, drawib_model
                    )
                else:
                    # B3/C1 回归修复（用户裁决 2026-09-11）：非合并路径必须保留
                    # 「抑制原 deform draw + 用模组顶点按原顶点数重绘」语义。本次改动
                    # 曾把下面两行收窄为"仅合并组件才发"，而本方法被 ExportZZMI 整体
                    # 覆写且不调 super()（基类 unity.py:58-61 兜不住）⇒ 非合并模式
                    # （未勾合并 / 无合并缓存 / vg_count<=0）少发这两条指令且无等价
                    # 替代（IB 段的 handling=skip 管渲染 draw，不管 deform draw）。
                    # 此处按 `git show HEAD:ui/universal/zzmi.py` 809-865 的原始行为
                    # 无条件恢复同样的两行、同样顺序。
                    texture_override_vb_section.append("handling = skip")
                    texture_override_vb_section.append(
                        "draw = " + str(drawib_model.draw_number) + ", 0"
                    )
                for so0_source_resource_name in so0_source_resource_names:
                    texture_override_vb_section.append(so0_source_resource_name + " = ref so0")

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_vb_section.append("$active0 = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_vb_section.append("$ActiveCharacter = 1")

            texture_override_vb_section.new_line()

        ini_builder.append_section(texture_override_vb_section)

    def add_unity_vs_texture_override_vlr_section(
        self, ini_builder: M_IniBuilder, drawib_model, include_uav_byte_stride: bool = True
    ):
        """VertexLimitRaise 段（覆盖基类）：合并网格自动重定向时按 SO 实际大小声明。

        carrier（被重定向的合并网格挂载 IB）SO 退化为 3 顶点 stub；
        target（组内最后 deform draw 的 IB）SO = 自身真实几何 + 全部重定向
        合并网格之和。
        """
        d3d11_game_type = getattr(drawib_model, "d3d11GameType", None)
        if d3d11_game_type is None or not getattr(d3d11_game_type, "GPU_PreSkinning", False):
            return
        draw_ib = drawib_model.draw_ib
        redirect_carrier = self._redirect_carrier_map.get(draw_ib)
        redirect_target = self._redirect_target_map.get(draw_ib)
        if redirect_carrier is None and redirect_target is None:
            super().add_unity_vs_texture_override_vlr_section(
                ini_builder=ini_builder,
                drawib_model=drawib_model,
                include_uav_byte_stride=include_uav_byte_stride,
            )
            return

        if redirect_carrier is not None:
            carrier_target_plan = self._redirect_target_map.get(
                redirect_carrier.get("target"), {}
            )
            if (
                not carrier_target_plan.get("target_has_real_geometry", True)
                and carrier_target_plan.get("so_owner_ib") == draw_ib
            ):
                vertex_count = carrier_target_plan["so_vertex_count"]
            else:
                vertex_count = 3
                # F9 边界（t6 复核保留项）：carrier 的渲染 drawindexed 读本实例 SO
                # 的「前缀 3 行 + 合并行」，因此要求本实例 SO 容量覆盖
                # so_vertex_count 行。SO 容量由 **SO owner 部件**的
                # VertexLimitRaise 声明（纯占位 target 时 owner = 第一个 carrier；
                # 有真实几何时 owner = target，target 段自带声明）。
                # 多 carrier 时非 owner 的 carrier 这里只声明自己的 3 行占位容量：
                # 若游戏按"本 IB 的声明"分配共享 SO，合并几何尾部会被截断。
                # 该组合当前无实测样本。**仅升级诊断措辞与标注，未改动下面的
                # 声明行数（vertex_count = 3）**——改声明会改变生成产物、有实机
                # 风险。F9：待实机确认游戏是否按 SO owner 的声明分配本实例共享 SO。
                so_owner_ib = str(carrier_target_plan.get("so_owner_ib", "") or "")
                required_rows = int(carrier_target_plan.get("so_vertex_count", 0) or 0)
                if required_rows > vertex_count and so_owner_ib not in (
                    draw_ib,
                    str(redirect_carrier.get("target", "") or ""),
                ):
                    print(
                        "⚠️ [ZZMI骨骼合并] 需要你确认（F9 / 多 carrier SO 容量）："
                        f"DrawIB {draw_ib} 的 VertexLimitRaise 只声明 {vertex_count} 行，"
                        f"但本组合并几何渲染读取 {required_rows} 行（SO owner = {so_owner_ib}）。"
                        "影响：若游戏按『本 IB 的声明』分配共享 SO，合并几何尾部会被截断"
                        "（实机现象：模型局部缺失 / 网格错位）。"
                        "处置：导出不中断；请实机确认 SO 分配口径，若确认截断请回报。"
                    )
        else:
            vertex_count = redirect_target["so_vertex_count"]
        vertexlimit_section = M_IniSection(M_SectionType.TextureOverrideVertexLimitRaise)
        vertexlimit_section.append(
            "[TextureOverride_" + draw_ib + "_" + drawib_model.draw_ib_alias
            + "_VertexLimitRaise]"
        )
        vertexlimit_section.append("hash = " + drawib_model.vertex_limit_hash)
        vertexlimit_section.append(
            "override_byte_stride = "
            + str(d3d11_game_type.CategoryStrideDict["Position"])
        )
        vertexlimit_section.append("override_vertex_count = " + str(vertex_count))
        if include_uav_byte_stride:
            vertexlimit_section.append("uav_byte_stride = 4")
        vertexlimit_section.new_line()
        ini_builder.append_section(vertexlimit_section)

    def _merged_skeleton_groups(self) -> list[int]:
        """当前导出组件涉及的骨架组列表（升序）。

        组号统一按 `int` 归一后再排序：本轮实测出现过「组号是数字字符串」的组件
        记录，裸 `sorted` 会对 `str` 与 `int` 混排抛 TypeError；组号本身在下游
        （`_merged_group_component_ids` / 组命名 / 字典索引）已经一律 `int(...)`
        处理，因此这里归一不改变任何既有行为。
        """
        return sorted({int(c["skeleton_group"]) for c in self.merged_skeleton_components})

    # ------------------------------------------------------------------
    # 跨组别引用守卫（开关关闭 = 无校准模式：禁止跨组别骨骼合并；开启 = 按来源拆分绘制已接入）
    # ------------------------------------------------------------------

    #: 「该骨骼不属于任何导出组件」在守卫内部的**类型统一**哨兵（负数，永不与
    #: 真实组号 0..N 冲突）。显示文案在打印前替换，不进集合、不参与比较。
    #: 用类属性（经 `type(self)` 取）而非模块全局：守卫函数内不出现裸名字，
    #: 轻量 fake 宿主把模块 `__globals__` 换掉时同样解析得到。
    UNKNOWN_BONE_GROUP = -1

    def _collect_drawib_referenced_bone_ids(self, draw_ib: str) -> set[int]:
        """该 DrawIB 全部子网格源对象实际引用（权重>0）的骨骼 id 集合。

        骨骼 id 取顶点组**名字**（导入约定：组名 = 全局骨骼 id；join 按名合并，
        组名恒为骨骼 id，而索引不保证）。非数字组名跳过（不是骨骼）。
        占位小三角面对象（ZZMI_STUB，权重挂在已注册槽——json VGMap 首值）跳过——它是
        不可见标记，不是真实几何，不该触发跨组报警。

        P1c 性能（输出与逐元素旧实现完全等价）：
        - 每个对象的「组下标 → 数字组名」只构建一次；旧实现每个顶点组元素都要
          `len(groups)` + `groups[i].name` + `str` + `isdigit`（cProfile：单函数
          `builtins.len` 约 196 万次），内层循环现在只剩两次属性读 + 两次列表索引；
        - 结果按 DrawIB 缓存：同一 DrawIB 在一轮导出里被多个阶段重复收集
          （cProfile 45 次调用）。缓存条目带**结构签名**（对象名 / 顶点数 / 组数 /
          组名元组），签名不一致即重算，因此顶点组被增删 / 改名 / 换对象后不会
          拿到旧结果；`_export_impl` 每轮导出开头还会清空缓存；
        - 数字组名的 `int()` 保持**懒解析**（第一次被权重>0 的顶点引用时才解析），
          病态组名（`isdigit()` 为真但 `int()` 抛 ValueError，如 "\u00b2"）的异常
          时机与旧实现一致。
        """
        cache = getattr(self, "_zzmi_drawib_bone_refs_cache", None)
        if cache is None:
            cache = {}
            try:
                setattr(self, "_zzmi_drawib_bone_refs_cache", cache)
            except (AttributeError, TypeError):
                cache = None
        if cache is not None:
            entry = cache.get(draw_ib)
            if entry is not None and entry[0] == self._drawib_bone_refs_signature(
                draw_ib
            ):
                # 调用方会把返回值与别的集合做差集 / 迭代，返回副本以免改到缓存。
                return set(entry[1])
        used, signature = self._scan_drawib_bone_refs(draw_ib)
        if cache is not None:
            cache[draw_ib] = (signature, frozenset(used))
        return used

    def _iter_drawib_bone_ref_objects(self, draw_ib: str):
        """遍历该 DrawIB 参与骨骼引用统计的 Blender 对象（跳过占位 / 取不到的）。

        与旧 `_collect_drawib_referenced_bone_ids` 的对象筛选逐行同构：
        `get_blender_obj_name()` 抛异常继续；名字为空继续；`bpy.data.objects.get`
        取不到或带 `ZZMI_STUB` 继续。
        """
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                for draw_call in submesh_model.drawcall_model_list:
                    try:
                        obj_name = draw_call.get_blender_obj_name()
                    except Exception:
                        continue
                    obj = bpy.data.objects.get(obj_name) if obj_name else None
                    if obj is None or obj.get("ZZMI_STUB"):
                        continue
                    yield obj

    def _drawib_bone_refs_signature(self, draw_ib: str) -> tuple:
        """骨骼引用缓存的失效签名（结构层面：对象名 / 顶点数 / 组数 / 组名）。"""
        signature = []
        for obj in self._iter_drawib_bone_ref_objects(draw_ib):
            mesh = getattr(obj, "data", None)
            vertices = getattr(mesh, "vertices", None)
            groups = getattr(obj, "vertex_groups", None)
            obj_name = str(getattr(obj, "name", ""))
            if vertices is None or groups is None:
                signature.append((obj_name, None, None, None))
                continue
            signature.append(
                (
                    obj_name,
                    len(vertices),
                    len(groups),
                    tuple(str(vertex_group.name) for vertex_group in groups),
                )
            )
        return tuple(signature)

    def _scan_drawib_bone_refs(self, draw_ib: str) -> tuple[set[int], tuple]:
        """一次扫描：返回（引用的骨骼 id 集合, 本次扫描对应的结构签名）。

        内层循环等价于旧实现的判定顺序：
        `weight <= 0` 跳过 → `group >= len(groups)` 跳过 → 组名非纯数字跳过 →
        计入 `int(组名)`。组名列表按 `vertex_groups` 的位置索引（与旧实现
        `groups[group_elem.group]` 同一索引语义，含负索引回绕）。
        """
        used: set[int] = set()
        used_add = used.add
        signature = []
        for obj in self._iter_drawib_bone_ref_objects(draw_ib):
            mesh = getattr(obj, "data", None)
            vertices = getattr(mesh, "vertices", None)
            groups = getattr(obj, "vertex_groups", None)
            obj_name = str(getattr(obj, "name", ""))
            if vertices is None or groups is None:
                signature.append((obj_name, None, None, None))
                continue
            group_count = len(groups)
            group_names: list[str] = []
            group_is_digit: list[bool] = []
            for vertex_group in groups:
                name = str(vertex_group.name)
                group_names.append(name)
                group_is_digit.append(name.isdigit())
            signature.append(
                (obj_name, len(vertices), group_count, tuple(group_names))
            )
            if not group_count:
                continue
            resolved: list[int | None] = [None] * group_count
            for vertex in vertices:
                for group_elem in vertex.groups:
                    if group_elem.weight <= 0:
                        continue
                    group_index = group_elem.group
                    if group_index >= group_count:
                        continue
                    if not group_is_digit[group_index]:
                        continue
                    bone_id = resolved[group_index]
                    if bone_id is None:
                        bone_id = int(group_names[group_index])
                        resolved[group_index] = bone_id
                    used_add(bone_id)
        return used, tuple(signature)

    def _warn_cross_group_bone_references(self):
        """跨组骨骼引用的守卫（开关感知）。

        历史口径（`cross_group_merged_vgmap_test` 关闭时仍然适用）：无 CB1 校准的
        运行时，每组骨架只在 deform pass 直拷本组骨骼；顶点引用其它组的骨骼 id 时，
        对应槽位永远不会被写入 = 原点塌陷。检出即大声报警（列出越界骨骼 id 与归属
        组），不中断导出——与 _warn_missing_drawib_parts 同款"让用户看见"口径。

        开关开启后（2026-10-10 定案：**按来源拆分绘制 + 各回各家**）：这些槽位由**跨组
        attach** 写进吸收它的宿主组缓冲（`AttachX_C<部件>_g<宿主组>_s<槽>`），骨骼按
        全局骨 id 落在宿主组骨架的对应槽位里 ⇒ 不再是"永远不会被写入"；几何侧由
        `_append_merged_render_split_draws` 按**顶点组归属**把合并网格的索引切成若干段，
        每段**放回该来源自己的渲染段**里画（`ib`/`vb1`/`vb0` 由该段自己绑成合并网格的
        那一份）——那里的 `vs-cb1` 天然就是它自己的当帧对象变换，因此**不依赖帧内绘制
        顺序**、也不需要跨对象空间的校准矩阵。因此改为如实报告「已接入 + 剩余风险」。
        （历史路线全部实测否决并移除：F4/F7「在宿主渲染段用当帧 cb1 重跑校准再写回顶点
        流」——重算 CS 实际未执行，且把 mod 自建缓冲当 `vb0` 的四次尝试全部静默失败；
        F5「把 palette 捕获也后移，让两者同属上一帧」——合并骨架缓冲是宿主与跨组供骨件
        **共用**的 ⇒ 整块骨架（含宿主自己的骨骼）一起慢一帧 ⇒ 用户实测「无论哪边都在
        抖、骨骼矩阵不对」，连原始物体都错。）
        **残余**：① 某来源部件本帧未被游戏提交渲染时，它那一段不会被画（与游戏自身的
        剔除口径一致，方向比"用陈旧 cb1 画错"更安全）；② palette / cb1 首帧未捕获时该帧
        骨架为未初始化值（一帧闪动，随后自愈）；③ 读不到合并 Blend/Index 时退回单次绘制
        并落 CROSS_GROUP_ATTACH 诊断。
        绝不谎称已修复。
        """
        if not self.merged_skeleton_components:
            return
        cross_enabled = self._merged_cross_group_enabled()
        unknown_group = int(type(self).UNKNOWN_BONE_GROUP)
        # 每组合法骨骼 id 集合 = 该组全部导出组件槽位并集（缺席部件的骨骼不会
        # attach，也不可被引用——同组缺席部件被并入现成对象同样会报警）
        group_legal: dict[int, set[int]] = {}
        id_to_group: dict[int, int] = {}
        for component in self.merged_skeleton_components:
            # 组号先归一成 int：它既做 `group_legal`/`id_to_group` 的键，又直接进
            # 下面的 `sorted`（混排 str 与 int 会抛 TypeError）。
            skeleton_group = int(component["skeleton_group"])
            legal = group_legal.setdefault(skeleton_group, set())
            for bone_id in range(
                component["vg_offset"], component["vg_offset"] + component["vg_count"]
            ):
                legal.add(bone_id)
                id_to_group.setdefault(bone_id, skeleton_group)

        for component in self.merged_skeleton_components:
            draw_ib = component["draw_ib"]
            skeleton_group = int(component["skeleton_group"])
            legal = group_legal[skeleton_group]
            _refs = self._collect_drawib_referenced_bone_ids(draw_ib)
            offending = sorted(bone_id for bone_id in _refs if bone_id not in legal)
            if not offending:
                continue
            # 类型纪律：本集合**只装组号（int）**。归属未知的骨骼一律落进同一个
            # 哨兵键，最后再换成显示文案——`sorted` 混排 str 与 int 会直接抛
            # TypeError（实测报错点即此处：`offending` 里既有别组骨骼、又有不在任何
            # 导出组件范围内的骨骼时，旧实现把两种类型塞进同一个 set ⇒ 整次导出
            # 中止在守卫里）。`int(...)` 同时兜住「组号是数字字符串」的历史缓存记录。
            offending_group_ids_set: set[int] = set()
            for bone_id in offending:
                owner = id_to_group.get(bone_id, unknown_group)
                offending_group_ids_set.add(
                    unknown_group if owner == unknown_group else int(owner)
                )
            offending_group_ids = sorted(offending_group_ids_set)
            offending_groups = [
                "未知（不在导出组件范围）" if group_id == unknown_group else group_id
                for group_id in offending_group_ids
            ]
            if cross_enabled:
                print(
                    f"[ZZMI骨骼合并] 跨组骨骼合并已接入（按来源拆分绘制）: DrawIB {draw_ib} "
                    f"（骨架组 G{skeleton_group}）的顶点引用了别组骨骼 id "
                    f"{offending}（归属组: {offending_groups}）——这些槽位由跨组 attach "
                    f"写入**吸收它的宿主组**骨架缓冲，几何侧则按顶点组把合并网格的索引"
                    f"拆成若干段、每段放回**该来源自己的渲染段**里画（那里的 vs-cb1 天然"
                    f"就是它自己的当帧对象变换），因此不再塌陷、也不依赖帧内绘制顺序。"
                )
                print(
                    "[ZZMI骨骼合并] 残余：① 某来源部件本帧未被游戏提交渲染时，它那一段"
                    "不会被画（与游戏自身的剔除口径一致）；② palette / cb1 首帧未捕获时"
                    "该帧骨架为未初始化值（一帧闪动后自愈）；③ 读不到合并 Blend/Index 时"
                    "退回单次绘制并落 CROSS_GROUP_ATTACH 诊断。历史路线 F4/F7（渲染段重算"
                    "校准再写回顶点流）与 F5（palette 捕获后移）均已实测否决并移除。"
                )
                continue
            print(
                f"[ZZMI骨骼合并] !!! 禁止跨组别骨骼合并: DrawIB {draw_ib} "
                f"（骨架组 G{skeleton_group}）的顶点引用了非本组骨骼 id "
                f"{offending}（归属组: {offending_groups}）——无校准模式下这些槽位"
                f"永远不会被写入本组骨架，游戏内将渲染为原点塌陷。"
            )
            print(
                "[ZZMI骨骼合并] 请只把同一骨架组（相同对象空间）的部件合并到同一对象，"
                "或把这些顶点的权重改刷到本组骨骼。"
            )

    def _warn_merged_mesh_timing(self, unredirected: dict | None = None):
        """无法自动重定向的合并网格时序报警（见 _build_merged_mesh_redirect_plan）。

        可自动重定向的合并网格已由导出器挪到组内最后 deform draw（用户无感，
        任意 IB 挂载均正确）；这里只对**无法**重定向的情况大声报警。
        """
        unredirected = unredirected or {}
        if not unredirected:
            return
        by_group: dict[int, list[tuple[str, str, str]]] = {}
        for component in self.merged_skeleton_components:
            info = unredirected.get(component["draw_ib"])
            if info is None:
                continue
            by_group.setdefault(int(component["skeleton_group"]), []).append(
                (component["draw_ib"], info.get("reason", ""), info.get("target", ""))
            )
        for skeleton_group, entries in by_group.items():
            for draw_ib, reason, target_ib in entries:
                if reason == "required-dependency-after-compatible-host":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）有必需骨骼依赖到达晚于所有兼容重放宿主；"
                        "本次帧不会消费旧 RedirectSO 数据。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请把合并几何拆回相同 Blend 输入布局的部件，"
                        "或重新导入后统一参与重放部件的 Blend 布局；当前 BI4/BI16 混合顺序"
                        "无法安全自动重放。"
                    )
                    continue
                if reason == "incompatible-blend-layout":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）与目标挂点的 Blend 输入布局不兼容；"
                        "强行重放会按错误的 BLENDINDICES/BLENDWEIGHT 格式读取并导致爆炸。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请让合并网格挂在组内最后一个 deform draw，"
                        "或重新导入并统一参与重放部件的 Blend 布局后再导出。"
                    )
                    continue
                if reason == "blend-retarget-unsupported":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）的 Blend 输入布局与组内多数布局"
                        "不一致，且无法无损重打包（目标布局通道更窄，重打包会丢骨骼影响）。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请把合并几何挂到与组内多数部件相同 Blend 布局的"
                        "部件上，或把该部件的布局统一后再导出。"
                    )
                    continue
                if reason == "target-layout-not-anchor":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）的目标挂点自身有真实几何，"
                        "但其 Blend 布局不是组内多数布局——前缀行与合并行无法在同一段写出。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请把合并几何挂到组内多数 Blend 布局的部件上后重新导出。"
                    )
                    continue
                if reason == "missing-blend-layout":
                    print(
                        f"[ZZMI骨骼合并] !!! 合并网格无法自动重定向: DrawIB {draw_ib}"
                        f"（骨架组 G{skeleton_group}）缺少可验证的 Blend 输入布局；"
                        "为避免按错误的 BLENDINDICES/BLENDWEIGHT 格式重放，已停止该重定向。"
                    )
                    print(
                        "[ZZMI骨骼合并] 请重新导入该角色的全部参与部件，"
                        "确保 GameType 包含有效的 Blend 元素或正数 stride 后再导出。"
                    )
                    continue
                print(
                    f"[ZZMI骨骼合并] !!! 合并网格时序无法自动修复: DrawIB {draw_ib}"
                    f"（骨架组 G{skeleton_group}）引用了其它部件的骨骼，但其 deform "
                    f"pass 早于组内最后一个 deform draw"
                    + (
                        "，且反查缓存缺少 DeformDrawIndex（请先重新执行「骨骼合并"
                        "反查」刷新缓存后再导出）。"
                        if reason == "missing-deform-draw"
                        else "，且该部件配置了跨 IB 重定向（暂不与自动重定向兼容）。"
                    )
                )
                if target_ib:
                    print(
                        "[ZZMI骨骼合并] 手动修复：把合并后的物体改名为组内最后一个 "
                        f"deform draw 部件的子网格名（{target_ib} 或带 _copy 后缀）"
                        "后重新导出。"
                    )

    # ------------------------------------------------------------------
    # 合并网格自动重定向（2026-08-25 设计兑现：合并网格可挂在任意 DrawIB）
    # ------------------------------------------------------------------
    #
    # 背景：palette 是 per-pass 独立 Map 上传的 ring scratch（dump 实测：
    # 同一资源 hash 帧内两次 dump 内容不同），早 pass 时刻读不到晚 pass 部件
    # 的当帧骨骼——所以合并网格（引用组内多个部件骨骼）物理上只能在组内
    # **最后一个 deform draw** 蒙皮。为兑现「用户可自由 join 到任意 IB」的
    # 设计承诺，导出侧自动重定向：
    #   - 合并网格挂载的 DrawIB（carrier）的 deform override 退化为 stub draw
    #     （3 顶点，保留 copy palette + attach 写当帧骨骼）；
    #   - 组内最后一个 deform draw 的 DrawIB（target）的 deform override 追加
    #     画合并网格（绑定 carrier 的 vb0/vb2），其 SO 按 [target 完整导出顶点
    #     （含 stub）][merged...] 拼接；
    #   - carrier 的 render override 保留 carrier 自己的 hash/first_index，并显式
    #     绑定 target RedirectSO（base_vertex = target 完整导出顶点数）；
    #   - target/缺失部件始终保留自己的 hash、IB 和极限小三角占位，不用 ib=null
    #     静默跳过，避免不同物体共享 hash 时发生串扰；
    #   - VertexLimitRaise：carrier = 3，target = SO 总大小。
    # 对用户完全透明：任意 IB 挂载都正确，无需改名。

    def _submesh_is_stub(self, submesh_model) -> bool:
        """子网格是否只有占位小三角面对象（无真实几何）。"""
        saw_confirmed_stub = False
        for draw_call in getattr(submesh_model, "drawcall_model_list", []) or []:
            try:
                obj_name = draw_call.get_blender_obj_name()
            except Exception:
                return False
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            if obj is None or not obj.get("ZZMI_STUB"):
                return False
            saw_confirmed_stub = True
        return saw_confirmed_stub

    def _submesh_exported_vertex_count(self, submesh_model) -> int:
        """子网格导出 buffer 顶点数（去重后；与 drawib_model.vertex_count 口径一致）。"""
        index_vertex_id_dict = getattr(submesh_model, "index_vertex_id_dict", None)
        if index_vertex_id_dict:
            try:
                return int(len(index_vertex_id_dict))
            except TypeError:
                pass
        category_buffer_dict = getattr(submesh_model, "category_buffer_dict", None) or {}
        position_buffer = category_buffer_dict.get("Position")
        d3d11_game_type = getattr(submesh_model, "d3d11_game_type", None)
        if position_buffer is None or d3d11_game_type is None:
            return 0
        position_stride = int(
            (getattr(d3d11_game_type, "CategoryStrideDict", {}) or {}).get("Position", 0) or 0
        )
        if position_stride <= 0:
            return 0
        return int(len(position_buffer) / position_stride)

    def _drawib_exported_vertex_count(self, draw_ib: str) -> int:
        """DrawIB 完整导出顶点数之和，包含用于保持 IB 布局的 stub 顶点。"""
        total = 0
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                total += self._submesh_exported_vertex_count(submesh_model)
        return total

    def _drawib_has_real_geometry(self, draw_ib: str) -> bool:
        """判断 DrawIB 是否包含真实几何（而非全部为 ZZMI 占位子网格）。"""
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                if not self._submesh_is_stub(submesh_model):
                    return True
        return False

    def _drawib_category_layout(self, draw_ib: str, category: str) -> dict | None:
        """该 DrawIB 某**类目**的输入布局（含每个元素在缓冲内的字节偏移）。

        返回 ``{"stride": int, "elements": [{"semantic", "index", "format",
        "byte_width", "offset", "extract_slot"}, ...]}``；元素按
        `D3D11ElementList` 顺序累加 `ByteWidth` 得到**类别内**偏移（每个 Category
        单独一个缓冲 ⇒ 偏移在 Category 内累加；元数据的 `AlignedByteOffset` 是
        **跨类别**的全局累加值，不能当缓冲内偏移用，见 `common/d3d11_gametype.py:70-93`）。
        `stride` 优先取该类目的**声明 stride**；**声明缺失**（取不到或为 0）而元素存在时，
        **回退为元素 `ByteWidth` 之和**（即 `category_stride or offset`）—— 不是永远以
        声明为准。无该类目元素时退化为 ``{"stride": 类目 stride, "elements": []}``；
        两者都取不到则 None。
        """
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            game_type = getattr(drawib_model, "d3d11GameType", None)
            elements = getattr(game_type, "D3D11ElementList", None)
            layout_elements: list[dict] = []
            offset = 0
            for element in elements or []:
                if str(getattr(element, "Category", "") or "") != str(category):
                    continue
                byte_width = int(getattr(element, "ByteWidth", 0) or 0)
                layout_elements.append({
                    "semantic": str(getattr(element, "SemanticName", "") or "").upper(),
                    "index": int(getattr(element, "SemanticIndex", 0) or 0),
                    "format": str(getattr(element, "Format", "") or "").upper(),
                    "byte_width": byte_width,
                    "offset": offset,
                    "extract_slot": str(getattr(element, "ExtractSlot", "") or ""),
                })
                offset += byte_width
            stride_dict = getattr(game_type, "CategoryStrideDict", {}) or {}
            try:
                category_stride = int(stride_dict.get(str(category), 0) or 0)
            except (TypeError, ValueError):
                category_stride = 0
            if layout_elements:
                return {
                    "stride": category_stride or offset,
                    "elements": layout_elements,
                }
            if category_stride > 0:
                return {"stride": category_stride, "elements": []}
            return None
        return None

    def _drawib_blend_layout(self, draw_ib: str) -> dict | None:
        """该 DrawIB 的 Blend 输入布局（`_drawib_category_layout` 的特化）。"""
        return self._drawib_category_layout(draw_ib, "Blend")

    @staticmethod
    def _blend_layout_key(layout: dict | None):
        """布局可比键（与历史 `_drawib_blend_layout_signature` 输出完全一致）。"""
        if not layout:
            return None
        elements = layout.get("elements") or []
        if elements:
            signature = []
            for element in elements:
                element_format = ExportZZMI._zzmi_layout_element_format(
                    element.get("semantic", ""), element.get("format", "")
                )
                signature.append((
                    str(element.get("semantic", "") or "").upper(),
                    int(element.get("index", 0) or 0),
                    element_format,
                    int(element.get("byte_width", 0) or 0),
                    str(element.get("extract_slot", "") or ""),
                ))
            if signature:
                return ("elements", tuple(signature))
        try:
            blend_stride = int(layout.get("stride", 0) or 0)
        except (TypeError, ValueError):
            blend_stride = 0
        if blend_stride > 0:
            return ("stride", blend_stride)
        return None

    def _drawib_blend_layout_signature(self, draw_ib: str):
        """返回用于 deform 重放的 Blend 输入布局签名。

        自动重定向会在另一个 DrawIB 的 IA 状态下执行 draw；Blend 槽的
        stride/元素布局不兼容时，BLENDINDICES 会被按错误格式解释，结果通常
        是流输出全零。优先比较完整元素，测试桩或旧模型则退化为 stride。
        """
        return self._blend_layout_key(self._drawib_blend_layout(draw_ib))

    @staticmethod
    def _blend_layout_width(layout_key) -> int:
        """布局的字节宽度（用于选"组内最大的那个"布局作为锚点）。"""
        if not layout_key:
            return 0
        if layout_key[0] == "stride":
            try:
                return int(layout_key[1])
            except (TypeError, ValueError):
                return 0
        return sum(int(element[3]) for element in layout_key[1])

    def _merged_group_layout_catalog(self, skeleton_group: int) -> dict:
        """本组候选重放布局目录：``{布局键: {"count": 部件数, "layout": 布局}}``。

        只统计**有 deform pass** 的部件（没有 deform 段就无法作为重放锚点）。
        """
        catalog: dict = {}
        for component_id in self._merged_group_component_ids(skeleton_group):
            component = self.merged_skeleton_components[component_id]
            if int(component.get("deform_draw", 0) or 0) <= 0:
                continue
            draw_ib = str(component["draw_ib"])
            key = self._drawib_blend_layout_signature(draw_ib)
            if key is None:
                continue
            entry = catalog.setdefault(
                key,
                {"count": 0, "layout": self._drawib_blend_layout(draw_ib)},
            )
            entry["count"] += 1
        return catalog

    def _drawib_exported_blend_bytes(self, draw_ib: str) -> bytes | None:
        """该 DrawIB 导出后的 Blend 缓冲字节（BLENDINDICES 已是全局骨骼 id）。"""
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            category_buffer = (
                getattr(drawib_model, "category_buffer_dict", {}) or {}
            ).get("Blend")
            if category_buffer is None:
                return None
            if hasattr(category_buffer, "tobytes"):
                return category_buffer.tobytes()
            return bytes(category_buffer)
        return None

    def _build_redirect_blend_retarget(
        self, carrier_ib: str, anchor_layout: dict
    ) -> tuple[str, int, str, bytes] | None:
        """把载体的 Blend 缓冲重打包成锚点布局（只允许**无损加宽**）。

        用途：载体与组内多数部件的 Blend 输入布局不一致时，让合并几何能在
        任意锚点挂点的 IA 布局下重放。规则：
        - 目标元素按 (SemanticName, SemanticIndex) 在源布局里找同通道；
        - 源宽度 > 目标宽度（降宽，会丢骨骼影响）或源有目标没有的元素 → 放弃；
        - 目标有源没有的通道 → 补 0（权重 0 不参与混权；索引 0 配 0 权重无害）。
        例：BW8_BI8（2×f32 权重 + 2×u32 索引，stride 16）→ BW16_BI16
        （4×f32 + 4×u32，stride 32）= 前 8 字节照抄、后 8 字节补 0，无损。
        """
        carrier_layout = self._drawib_blend_layout(carrier_ib)
        anchor_elements = (anchor_layout or {}).get("elements") or []
        if not carrier_layout or not anchor_elements:
            return None
        source_bytes = self._drawib_exported_blend_bytes(carrier_ib)
        if not source_bytes:
            return None
        stride_source = int(carrier_layout.get("stride", 0) or 0)
        if stride_source <= 0 or len(source_bytes) % stride_source != 0:
            return None
        source_elements = carrier_layout.get("elements") or []
        # 源布局里有、目标布局里没有的元素：可能承载真实权重/索引 → 拒绝。
        for source_element in source_elements:
            if not any(
                element["semantic"] == source_element["semantic"]
                and element["index"] == source_element["index"]
                for element in anchor_elements
            ):
                return None
        mapping = []
        for anchor_element in anchor_elements:
            source_element = next(
                (
                    element
                    for element in source_elements
                    if element["semantic"] == anchor_element["semantic"]
                    and element["index"] == anchor_element["index"]
                ),
                None,
            )
            if source_element is None:
                mapping.append((None, anchor_element))
                continue
            if int(source_element["byte_width"]) > int(anchor_element["byte_width"]):
                return None
            mapping.append((source_element, anchor_element))
        stride_anchor = int(anchor_layout.get("stride", 0) or 0)
        if stride_anchor <= 0:
            return None
        rows = len(source_bytes) // stride_source
        payload = bytearray(rows * stride_anchor)
        for row in range(rows):
            source_base = row * stride_source
            anchor_base = row * stride_anchor
            for source_element, anchor_element in mapping:
                if source_element is None:
                    continue
                width = min(
                    int(source_element["byte_width"]),
                    int(anchor_element["byte_width"]),
                )
                if width <= 0:
                    continue
                source_offset = source_base + int(source_element["offset"])
                anchor_offset = anchor_base + int(anchor_element["offset"])
                payload[anchor_offset:anchor_offset + width] = source_bytes[
                    source_offset:source_offset + width
                ]
        return (
            self._redirect_blend_resource_name(carrier_ib, stride_anchor),
            stride_anchor,
            self._redirect_blend_filename(carrier_ib, stride_anchor),
            bytes(payload),
        )


    def _drawib_stub_submeshes(self, draw_ib: str) -> list:
        """DrawIB 的 stub 子网格列表（占位对象，无真实几何）。"""
        result = []
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                if self._submesh_is_stub(submesh_model):
                    result.append(submesh_model)
        return result

    def _drawib_first_match_first_index(self, draw_ib: str) -> list[int]:
        """DrawIB 子网格的 match_first_index 列表（升序；重挂 render override 用）。"""
        indices = []
        for drawib_model in self.drawib_model_list:
            if drawib_model.draw_ib != draw_ib:
                continue
            for submesh_model in drawib_model.submesh_model_list:
                try:
                    indices.append(int(submesh_model.match_first_index))
                except (TypeError, ValueError):
                    continue
        return sorted(indices)

    def _drawib_is_cross_ib(self, draw_ib: str) -> bool:
        """DrawIB 是否参与跨 IB 重定向（source 或 target）——暂不与自动重定向兼容。

        cross_ib_info_dict 的键/值是 ib_key（`<draw_ib>_<first_index>`），按前缀匹配。
        """
        prefix = draw_ib + "_"
        if any(str(key).startswith(prefix) for key in (self.cross_ib_info_dict or {})):
            return True
        return any(
            str(target).startswith(prefix)
            for targets in (self.cross_ib_info_dict or {}).values()
            for target in targets
        )

    def _build_merged_mesh_redirect_plan(self):
        """构建合并网格自动重定向计划。

        返回 (carrier_map, target_map, unredirected)：
        - carrier_map: draw_ib -> {"target": 目标 DrawIB,
                                   "base_vertex": 该合并网格在 target SO 中的偏移,
                                   "target_first_index": 重挂 render 用的 match_first_index,
                                   "vertex_count": 合并网格导出顶点数}
        - target_map: draw_ib -> {"deform_draws": [(vb0 资源名, vb2 资源名, 顶点数), ...],
                                  "so_vertex_count": target SO 总大小（含自身 stub）,
                                  "target_own_vertices": target 完整导出顶点数,
                                  "so_owner_ib": 实际持有 SO 的 DrawIB,
                                  "compatible_component_ids": 可安全执行重放的组件 id}
        - unredirected: draw_ib -> {"reason": str, "target": str|""}（无法自动重定向）
        """
        carrier_map: dict[str, dict] = {}
        target_map: dict[str, dict] = {}
        unredirected: dict[str, dict] = {}
        # 每次重建计划都从零开始（测试与重复导出会多次调用本函数）。
        self._redirect_blend_retargets = {}

        groups: dict[int, list[dict]] = {}
        for component in self.merged_skeleton_components:
            groups.setdefault(int(component["skeleton_group"]), []).append(component)
        component_id_by_draw_ib = {
            component["draw_ib"]: component_id
            for component_id, component in enumerate(self.merged_skeleton_components)
        }

        for skeleton_group, components in groups.items():
            legal: set[int] = set()
            for component in components:
                for bone_id in range(
                    int(component["vg_offset"]),
                    int(component["vg_offset"]) + int(component["vg_count"]),
                ):
                    legal.add(bone_id)

            with_draw = [c for c in components if int(c.get("deform_draw", 0) or 0) > 0]
            if not with_draw:
                for component in components:
                    if (
                        self._collect_drawib_referenced_bone_ids(component["draw_ib"])
                        - set((component.get("vg_map") or {}).values())
                    ) & legal:
                        unredirected[component["draw_ib"]] = {
                            "reason": "missing-deform-draw",
                            "target": "",
                        }
                continue

            last = max(with_draw, key=lambda c: int(c.get("deform_draw", 0) or 0))
            target_ib = last["draw_ib"]
            # target 的 SO 前缀必须与自身导出 VB/IB 使用同一完整顶点布局。
            # stub 的 remapped IB 也引用这 3 个顶点；若将其排除，紧随其后的
            # carrier 顶点会占据相同索引范围，target 占位 draw 将画出真实几何。
            target_own_vertices = self._drawib_exported_vertex_count(target_ib)
            target_has_real_geometry = self._drawib_has_real_geometry(target_ib)
            target_first_indices = self._drawib_first_match_first_index(target_ib)
            target_first_index = target_first_indices[0] if target_first_indices else 0

            carriers: list[dict] = []
            for component in components:
                referenced = self._collect_drawib_referenced_bone_ids(component["draw_ib"])
                own = set((component.get("vg_map") or {}).values())
                absorbed = (referenced - own) & legal
                if not absorbed:
                    continue  # 未合并其它部件
                if int(component.get("deform_draw", 0) or 0) == int(last["deform_draw"]):
                    continue  # 已挂在最后 pass：无需重定向
                if int(component.get("deform_draw", 0) or 0) <= 0:
                    unredirected[component["draw_ib"]] = {
                        "reason": "missing-deform-draw",
                        "target": last.get("unique_str") or "",
                    }
                    continue  # 缺 DeformDrawIndex：无法确定时序
                if self._drawib_is_cross_ib(component["draw_ib"]) or self._drawib_is_cross_ib(target_ib):
                    unredirected[component["draw_ib"]] = {
                        "reason": "cross-ib",
                        "target": last.get("unique_str") or "",
                    }
                    continue  # 跨 IB 重定向与合并网格自动重定向暂不兼容
                # 合并网格的导出顶点数（该 DrawIB 全部子网格——合并场景下通常一个）
                merged_vertices = 0
                for drawib_model in self.drawib_model_list:
                    if drawib_model.draw_ib != component["draw_ib"]:
                        continue
                    for submesh_model in drawib_model.submesh_model_list:
                        merged_vertices += self._submesh_exported_vertex_count(submesh_model)
                carriers.append({
                    "draw_ib": component["draw_ib"],
                    "vertex_count": merged_vertices,
                })

            if not carriers:
                continue

            # 重放锚点布局的选择（用户 2026-09-16 拍板）：
            #   **能用多数派就用多数派，但绝不要求任何部件降宽。**
            # - 同组不同 IB 的 Blend 输入布局可以不同（叶瞬光01 组 0：`8c8de427`
            #   是 BW8_BI8 = 2×f32 权重 + 2×u32 索引 / stride 16，其余 6 个部件是
            #   BW16_BI16 = 4×f32 + 4×u32 / stride 32）；几何被合并到**窄**布局的
            #   部件上时，必须把该部件的权重/索引**升宽**到锚点布局（补 0 通道，
            #   无损；以后出现 32 字节的同样按"能容下所有载体"来升）。
            # - 降宽（把 4 通道压成 2 通道）会丢骨骼影响、顶点权重和 <1 → 蒙皮
            #   位移，**绝不允许**，因此候选布局必须能容下全部载体；在此前提下
            #   取部件数最多者（锚点集合最大，与引擎 deform 提交顺序无关）。
            # - 每槽守卫判定「全组当帧到达」，只有组内**最后一个**到达的部件那段
            #   才可能闭合；而哪个部件最后到达逐帧都在变（2026-09-13/16 dump 实证
            #   提交顺序跨帧不一致）。锚点集合越小，越容易出现"载体排在前面 →
            #   整帧不写 → 模型闪/消失"（用户实测）。
            unknown_carrier = next(
                (
                    carrier["draw_ib"]
                    for carrier in carriers
                    if self._drawib_blend_layout_signature(carrier["draw_ib"]) is None
                ),
                None,
            )
            if unknown_carrier is not None:
                for carrier in carriers:
                    unredirected[carrier["draw_ib"]] = {
                        "reason": "missing-blend-layout",
                        "target": last.get("unique_str") or "",
                    }
                continue
            catalog = self._merged_group_layout_catalog(skeleton_group)
            candidates: dict = {}
            for layout_key, entry in catalog.items():
                mapping: dict[str, str] = {}
                retargets: dict[str, tuple[str, int, str, bytes]] = {}
                usable = True
                for carrier in carriers:
                    carrier_ib = carrier["draw_ib"]
                    if self._drawib_blend_layout_signature(carrier_ib) == layout_key:
                        mapping[carrier_ib] = f"Resource{carrier_ib}Blend"
                        continue
                    retarget = self._build_redirect_blend_retarget(
                        carrier_ib, entry["layout"] or {}
                    )
                    if retarget is None:
                        usable = False
                        break
                    retargets[carrier_ib] = retarget
                    mapping[carrier_ib] = retarget[0]
                if usable:
                    candidates[layout_key] = {
                        "mapping": mapping,
                        "retargets": retargets,
                        "count": int(entry["count"]),
                    }
            if not candidates:
                for carrier in carriers:
                    unredirected[carrier["draw_ib"]] = {
                        "reason": "blend-retarget-unsupported",
                        "target": last.get("unique_str") or "",
                    }
                continue
            anchor_layout_key = max(
                candidates,
                key=lambda key: (
                    candidates[key]["count"],
                    self._blend_layout_width(key),
                ),
            )
            anchor_layout = catalog[anchor_layout_key]["layout"]
            replay_blend_resource: dict[str, str] = candidates[anchor_layout_key]["mapping"]
            group_retargets: dict[str, tuple[str, int, str, bytes]] = candidates[
                anchor_layout_key
            ]["retargets"]
            # target 有真实几何时，它的前缀行只能与合并行在同一段写出（前缀行数 =
            # target 自身导出顶点数）；因此要求 target 自身就属于锚点布局，否则
            # 前缀会被按错误布局重放。
            if (
                target_has_real_geometry
                and self._drawib_blend_layout_signature(target_ib) != anchor_layout_key
            ):
                for carrier in carriers:
                    unredirected[carrier["draw_ib"]] = {
                        "reason": "target-layout-not-anchor",
                        "target": last.get("unique_str") or "",
                    }
                continue

            # NOTE: 下面沿用既有口径计算 prefix / base_vertex / so_total；
            # 锚点集合改为「锚点布局的必需组件」（见后）。

            # target 的 SO 布局：**[target 在变体 pass 里实际写入的前缀行][carrier merged]...**
            #
            # 关键不变量：base_vertex 必须等于本 pass **实际写进 SO 的行数之和**
            # （= 本 pass 中所有写 SO 的 draw 顶点数之和）。历史事故：浮波柚叶01
            # 的 target 两个子网格都成了占位小三角，target 自己那条 deform draw
            # 被跳过（不能把它的 BI4/BI8 输入布局带进 carrier 重放），prefix 只剩
            # carrier 的 3 顶点占位 stub —— 而 base_vertex 仍按"设计意图"取
            # target_own_vertices=6，渲染从 SO[6] 开始读实际只写满 SO[0..2] 的
            # 缓冲 → 合并几何整体错位 3 个顶点（爆炸）。
            #
            # 因此前缀行数改由 _redirect_plan_prefix_rows() 按"本 pass 实写行数"
            # 计算：纯占位 target 不额外写前缀（用占位 stub 的实写行数），base_vertex
            # 与 redirect Texcoord pad 同源取值，三者不可能再不一致。
            prefix_draws = self._build_redirect_plan_prefix_draws(
                target_ib,
                target_own_vertices,
                target_has_real_geometry,
            )
            so_prefix_rows = self._redirect_plan_prefix_rows(
                target_own_vertices,
                target_has_real_geometry,
            )
            # plan_prefix_rows 是"本 pass 前缀实写行数"的不可变快照：
            # base_vertex 会被下面按 carrier 累加（渲染侧依次读到各 carrier 区段），
            # 因此自检与 target_map 必须引用这个快照，不能引用被累加后的 base_vertex。
            #
            # A1 修复（用户裁决 2026-09-11）：此前 `plan_prefix_rows = so_prefix_rows`
            # 把两侧赋成同一个值，使下面 L2003 的自检**恒真、护栏实际不存在**
            # （历史事故：浮波柚叶01 的 base_vertex 按设计意图取 6、实际只写满
            # SO[0..2] → 合并几何整体错位 3 个顶点、画面爆炸）。现改为**独立重算**
            # 「实写侧前缀行数」，与 base_vertex 同源取值路径（纯占位 target
            # 用占位 stub 的实写行数），使自检真正能发现不一致。
            plan_prefix_rows = self._redirect_plan_prefix_rows(
                target_own_vertices,
                target_has_real_geometry,
            )
            base_vertex = so_prefix_rows
            deform_draws = list(prefix_draws)
            # t40：与 deform_draws 一一对应的 draw_ib（前缀 draw 属 target 自己），
            # 供蒙皮 CS 的行布局守卫逐元素核对每个 cs-t0 源的 Position 布局。
            deform_draw_ibs = [str(target_ib)] * len(prefix_draws)
            so_total = base_vertex
            # 合并几何真正依赖哪些当帧 palette：**所有会写这些槽位的部件**。
            #  - `slot_owner`：按声明段 [VGOffset, VGOffset+VGCount) 找该槽位的归属部件；
            #  - `slot_writers`：按 vg_map **值**找所有写入者——去重产生的共享 canonical
            #    槽位允许借位落在别人的段内，**借用者自己的 attach 也会写这个槽位**
            #    （`merged[vg_map[local]] = palette[local]`，同帧 bitwise 相同、后者覆盖）。
            #    守卫漏掉借用者时，借用者晚到会让该槽位保持**上一帧/上一实例**的旧值
            #    （2026-09-16 FrameAnalysis-051644 双实例帧实证：共享槽位 35 在借用者
            #    `999bff94` 第一次到达前一直是上一帧的值 cffe09）→ 半帧拼接、姿态串台。
            #
            # 已知边界（多实例）：
            # ① 出现次槽位只有 s1/s2 两份：同帧 3 个及以上实例时 occ 会回绕、槽位配错；
            # ② 某写入者在某个实例里被剔除（LOD/视锥）时，该实例的槽位预测靠「上一帧
            #    在该槽是否到过」：上一帧也缺席 ⇒ 本槽守卫照常闭合（不冻结）；只有
            #    「上一帧到过、本帧才被剔除」的那一帧会被推迟一次（该实例的合并几何
            #    自适应地保持上一帧内容一帧）。取舍见 ZZMI骨骼合并计划书 §7 的已知限制。
            # ③ 两个实例的**部件子集不同**（一个实例被部分剔除）时，实例在「出现次」上
            #    会错位（主控实例的部分部件落在 s1、部分落在 s2）——这是出现次口径的
            #    根本限制，彻底修复需要加载器给出**实例身份**信号，见计划书 §7。
            slot_owner: dict[int, int] = {}
            slot_writers: dict[int, set[int]] = {}
            for component_id, component in enumerate(self.merged_skeleton_components):
                if int(component["skeleton_group"]) != int(skeleton_group):
                    continue
                for bone_id in range(
                    int(component["vg_offset"]),
                    int(component["vg_offset"]) + int(component["vg_count"]),
                ):
                    slot_owner.setdefault(bone_id, component_id)
                for bone_id in set((component.get("vg_map") or {}).values()):
                    slot_writers.setdefault(int(bone_id), set()).add(component_id)
            required_component_ids: set[int] = set()
            target_component_id = component_id_by_draw_ib.get(target_ib)
            if target_component_id is not None and target_has_real_geometry:
                # target 的 deform 段负责捕获 ResourceZZRedirectSO_<target>；
                # 没有它就绪，carrier 即使其它 palette 都到齐也不能回放。
                required_component_ids.add(target_component_id)
            for carrier in carriers:
                carrier_component_id = component_id_by_draw_ib.get(carrier["draw_ib"])
                if carrier_component_id is not None:
                    required_component_ids.add(carrier_component_id)
                for bone_id in self._collect_drawib_referenced_bone_ids(carrier["draw_ib"]):
                    owner_id = slot_owner.get(bone_id)
                    if owner_id is not None:
                        required_component_ids.add(owner_id)
                    # 借用该槽位的写入者同样必须当帧到达（见上面 slot_writers 说明）。
                    required_component_ids.update(slot_writers.get(bone_id, ()))
                deform_draws.append((
                    f"Resource{carrier['draw_ib']}Position",
                    replay_blend_resource.get(
                        carrier["draw_ib"], f"Resource{carrier['draw_ib']}Blend"
                    ),
                    carrier["vertex_count"],
                ))
                deform_draw_ibs.append(str(carrier["draw_ib"]))
                carrier_map[carrier["draw_ib"]] = {
                    "target": target_ib,
                    "base_vertex": base_vertex,
                    "target_first_index": target_first_index,
                    "vertex_count": carrier["vertex_count"],
                }
                base_vertex += carrier["vertex_count"]
                so_total += carrier["vertex_count"]

            # target 只有占位几何时，合并 SO 必须由一个真实 carrier 挂点拥有。
            # 这样 target 晚到也不会把空的/BI4 的 SO 覆盖掉，carrier 自己的
            # BI16（或同类）输入布局可以在任意兼容挂点完成实际流输出。
            so_owner_ib = target_ib
            if not target_has_real_geometry and carriers:
                so_owner_ib = carriers[0]["draw_ib"]
                owner_component_id = component_id_by_draw_ib.get(so_owner_ib)
                if owner_component_id is not None:
                    required_component_ids.add(owner_component_id)

            # 锚点集合 = **required 组件里布局能承载重放的那些**（required ∩ 锚点布局）。
            #
            # 为什么必须是 required 的**子集**（关键不变量）：守卫条件收窄成 required
            # 后，`seen` 在帧内是 sticky 的（只在 [Present] 清零），所以「守卫成立」
            # 一旦发生在最后一个必需部件到达处，**之后每个锚点也都会成立**。而 SO 的
            # 写入偏移是**跨 pass 累积**的（前缀 stub 3 行 + 合并行，容量 = 前缀+合并），
            # 若锚点集合里含有非必需部件，就会在后面反复重放同一段几何 → 偏移越写越
            # 远、跨过缓冲末尾（越界写入/偏移失控，实测闪退嫌疑）。锚点 ⊆ required
            # 时，守卫只可能在「最后一个必需部件」自己的 deform 段闭合一次（该部件的
            # seen 只能在它自己那一段置位）→ SO 恰好写一份 [前缀][合并]。
            #
            # 代价：若最后一个到达的必需部件布局不可承载（例如把合并几何挂在 BW8_BI8
            # 部件上、而组内多数是 BW16_BI16），那一帧就没有锚点可落笔 → 会在下面
            # 报警提示改组内多数布局的部件承载。
            compatible_component_ids = []
            for component_id in sorted(required_component_ids):
                component_draw_ib = self.merged_skeleton_components[component_id]["draw_ib"]
                if (
                    self._drawib_blend_layout_signature(component_draw_ib)
                    == anchor_layout_key
                ):
                    compatible_component_ids.append(component_id)
            if not compatible_component_ids:
                # 元数据不完整/布局不可判定时保持旧行为（不因缺少布局对象而突然
                # 失去自动重定向）；真实模型会在上面的完整签名分支收紧。
                compatible_component_ids = sorted(required_component_ids)
            else:
                blocked = [
                    self.merged_skeleton_components[component_id]["draw_ib"]
                    for component_id in sorted(required_component_ids)
                    if component_id not in compatible_component_ids
                ]
                if blocked:
                    print(
                        "⚠️ [ZZMI骨骼合并] 有必需部件的 Blend 布局无法承载合并几何重放: "
                        f"{blocked}（骨架组 G{skeleton_group}，锚点布局宽="
                        f"{self._blend_layout_width(anchor_layout_key)}）。"
                        "若这些部件里最后到达的那个是它，该帧合并几何不会写入 → 闪/缺失。"
                        "处置：把合并后的物体挂到组内 Blend 布局占多数的部件上重新导出"
                        "（该布局的必需部件都是锚点，与提交顺序无关）。"
                    )

            # A required palette may belong to a later deform pass whose input
            # layout cannot host the replay (for example, a BI4 rigid
            # component arriving after all BI16 hosts).  Then every compatible
            # host has already run before the last dependency arrives, so the
            # emitted guard can never become true in this frame.  Keep the
            # carrier stub so stale RedirectSO data is not rendered as real
            # geometry, and publish an explicit diagnostic instead of silently
            # producing a flickering mesh (the merged draw stays undrawn because
            # the replay never ran; 2026-09 v2 has no frame latch, so the next
            # instance/frame re-evaluates the same guard).
            #
            # N3/F9：取消帧闩锁后 carrier 的渲染 drawindexed 是**无条件**的
            # （不再包 `if drawn == 1`），所以上述"重放永不成立"的变体下，渲染
            # 可能读到上一帧 SO 的尾部。取舍固定：恢复帧闩锁会在多实例下再次
            # 吞掉后续实例（用户实测已证伪该方向），因此保持无闩锁 + 导出期
            # 大声诊断，由用户换帧重抓或改名修复；SO 容量侧的不变量说明见
            # add_unity_vs_texture_override_vlr_section（非 SO owner 的 carrier 会
            # 打印显式诊断）。
            if required_component_ids:
                required_draws = [
                    int(
                        self.merged_skeleton_components[component_id].get(
                            "deform_draw", 0
                        )
                        or 0
                    )
                    for component_id in required_component_ids
                ]
                compatible_draws = [
                    int(
                        self.merged_skeleton_components[component_id].get(
                            "deform_draw", 0
                        )
                        or 0
                    )
                    for component_id in compatible_component_ids
                ]
                if not compatible_draws or max(required_draws) > max(compatible_draws):
                    for carrier in carriers:
                        unredirected[carrier["draw_ib"]] = {
                            "reason": "required-dependency-after-compatible-host",
                            "target": last.get("unique_str") or "",
                        }

            # 离线自检（无副作用诊断）：声明侧 base_vertex 与实写侧前缀行数
            # 必须同源。prefix_write_rows = 本变体 pass 里真正会写 SO 前缀的行数：
            #   target 自身 deform draw 的行数（有真实几何）+ 占位 stub 的行数
            #   （纯占位 target，由该 pass 里 carrier 的 stub draw 实写）。
            # 二者不一致时导出仍然继续（fixtures/骨架未就绪的旧工作空间不误伤），
            # 但会大声打印，便于离线自检脚本与用户第一时间发现错位。
            if int(plan_prefix_rows or 0) != int(so_prefix_rows or 0):
                print(
                    f"[ZZMI骨骼合并] !!! SO 前缀不一致: base_vertex="
                    f"{int(so_prefix_rows or 0)} 但本变体 pass 实写前缀 "
                    f"{int(plan_prefix_rows or 0)} 行（DrawIB {target_ib}）——"
                    "合并几何会整体位移；请检查 base_vertex 与实写前缀是否同源。"
                )

            target_map[target_ib] = {
                "target_ib": target_ib,
                "target_component_id": component_id_by_draw_ib.get(target_ib),
                "deform_draws": deform_draws,
                # 与 deform_draws 一一对应的 draw_ib（前缀 draw 属 target）：
                # 蒙皮 CS 的行布局守卫按它取每个 cs-t0 源的 Position 布局。
                "deform_draw_ibs": tuple(deform_draw_ibs),
                "so_vertex_count": so_total,
                "target_own_vertices": target_own_vertices,
                "so_prefix_rows": int(plan_prefix_rows or 0),
                "target_has_real_geometry": target_has_real_geometry,
                "so_owner_ib": so_owner_ib,
                "required_component_ids": sorted(required_component_ids),
                "compatible_component_ids": compatible_component_ids,
                # 实例对齐用的**通道骨**（t75 唯一判定口径）：导入期已按「共享骨图
                # 连通分量 + 引用件数 + 顶点权重」算好并缓存，这里只做组级复核读数
                # （逐部件的真实键输入见组件记录的 `channel.channel_local`）。
                "pose_anchor_slot": self._merged_group_pose_anchor_slot(
                    skeleton_group
                ),
                # 本次重定向采用的锚点布局签名（= 组内有 deform pass 的部件
                # Blend 布局的多数派）与为并入锚点集合而重打包的载体清单。
                "anchor_layout_key": anchor_layout_key,
                # t40：锚点布局的**完整元数据**（声明 stride + 元素表），
                # 供蒙皮 CS 行布局守卫逐元素核对（不再只比总宽度）。
                "anchor_layout": anchor_layout,
                "replay_blend_resources": dict(replay_blend_resource),
                "blend_retarget_carriers": sorted(group_retargets),
                # 该组最后一个 deform 挂点自己能否承载重放：布局兼容且必需依赖
                # 不会晚于所有兼容宿主（后者由下方 unredirected 判定）。
                "target_viable": (
                    target_ib not in unredirected
                    and target_component_id in compatible_component_ids
                ),
                "so_stride": next(
                    (
                        int(
                            drawib_model.d3d11GameType.CategoryStrideDict.get(
                                "Position", 40
                            )
                        )
                        for drawib_model in self.drawib_model_list
                        if drawib_model.draw_ib == target_ib
                    ),
                    40,
                ),
            }
            print(
                f"[ZZMI骨骼合并] 合并网格自动重定向: "
                f"{[c['draw_ib'] for c in carriers]} -> DrawIB {target_ib}"
                f"（组 G{skeleton_group} 最后 deform draw {last['deform_draw']}，"
                f"SO={so_total} 顶点，base_vertex 依次 "
                f"{[carrier_map[c['draw_ib']]['base_vertex'] for c in carriers]}）"
            )
            if group_retargets:
                print(
                    "[ZZMI骨骼合并] 载体 Blend 布局与组内多数布局不同，已重打包为"
                    f"锚点布局以便任意挂点重放: "
                    f"{[(ib, retarget[1]) for ib, retarget in sorted(group_retargets.items())]}"
                    f"（重放锚点组件 {compatible_component_ids}）"
                )
            self._redirect_blend_retargets.update(group_retargets)

        return carrier_map, target_map, unredirected

    @staticmethod
    def _redirect_plan_prefix_rows(
        target_own_vertices: int,
        target_has_real_geometry: bool,
    ) -> int:
        """本变体 pass 里写入 SO 前缀的行数（= base_vertex 的唯一来源）。

        - target 有真实几何：target 自己的 deform draw 写满 target_own_vertices 行；
        - 纯占位 target：target 自己的 draw 被跳过，前缀只剩该 pass 里 carrier 的
          占位 stub draw（3 顶点小三角）——前缀行数必须按"实写行数"取，不能按
          target 的声明顶点数取，否则渲染会从没写过的行开始读（浮波柚叶01 爆炸）。
        """
        if target_has_real_geometry:
            return int(target_own_vertices or 0)
        return int(ZZMI_STUB_PREFIX_ROWS)

    def _build_redirect_plan_prefix_draws(
        self,
        target_ib: str,
        target_own_vertices: int,
        target_has_real_geometry: bool,
    ) -> list[tuple[str, str, int]]:
        """构造变体 pass 里**写入 SO 前缀**的 target 自身 draw 序列。

        返回 ``[(vb0 资源名, vb2 资源名, 顶点数), ...]``，按写入顺序排列。
        - target 有真实几何：target 自己的 deform draw 承担 target_own_vertices 行；
        - 纯占位 target：target 自己的 draw 被跳过，前缀由该 pass 里 carrier 的
          占位 stub draw 实写（见 ``_redirect_plan_prefix_rows``），这里不再产生
          draw（避免把 target 的输入布局带进 carrier 重放）。
        """
        prefix_draws: list[tuple[str, str, int]] = []
        if not target_has_real_geometry:
            return prefix_draws
        prefix_rows = int(target_own_vertices or 0)
        if prefix_rows <= 0:
            return prefix_draws
        prefix_draws.append((
            f"Resource{target_ib}Position",
            f"Resource{target_ib}Blend",
            prefix_rows,
        ))
        return prefix_draws

    @staticmethod
    def _redirect_texcoord_resource_name(target_ib: str, carrier_ib: str, base_vertex: int) -> str:
        """返回合并网格 carrier 专用的、已按 base_vertex 对齐的 Texcoord 资源名。"""
        return (
            f"ResourceZZRedirectTexcoord_{target_ib}_{carrier_ib}_{int(base_vertex)}"
        )

    @staticmethod
    def _redirect_texcoord_filename(target_ib: str, carrier_ib: str, base_vertex: int) -> str:
        return f"zz_redirect_texcoord_{target_ib}_{carrier_ib}_{int(base_vertex)}.buf"

    @staticmethod
    def _redirect_blend_resource_name(carrier_ib: str, layout_stride: int) -> str:
        """载体合并几何在「锚点 Blend 布局」下重打包后的 vb2 资源名。"""
        return f"ResourceZZRedirectBlend_{carrier_ib}_s{int(layout_stride)}"

    @staticmethod
    def _redirect_blend_filename(carrier_ib: str, layout_stride: int) -> str:
        return f"zz_redirect_blend_{carrier_ib}_s{int(layout_stride)}.buf"

    def _build_redirect_texcoord_payload(self, carrier_ib: str, carrier_info: dict) -> tuple[bytes, int]:
        """为 carrier 的 vb1 生成与 RedirectSO 相同顶点偏移的缓冲。

        D3D11 的 ``base_vertex`` 会同时作用于所有顶点输入槽。合并重定向把本实例
        deform 的合并行写进本实例 SO（渲染段沿用游戏原生 vb0，不再覆写），而
        carrier 原本的 vb1 从第 0 行开始，因而会在每个索引上错读 ``base_vertex``
        行。这里在 Texcoord 前补齐同样数量的空行，使 ``vb1[index + base_vertex]``
        仍命中 carrier 的 UV 行。
        """
        drawib_model = next(
            (
                model
                for model in self.drawib_model_list
                if model.draw_ib == carrier_ib
            ),
            None,
        )
        if drawib_model is None:
            raise RuntimeError(
                f"[ZZMI骨骼合并] 找不到重定向 carrier DrawIB {carrier_ib}，无法生成 Texcoord 对齐缓冲"
            )

        game_type = getattr(drawib_model, "d3d11GameType", None)
        stride = int(
            (getattr(game_type, "CategoryStrideDict", {}) or {}).get("Texcoord", 0)
            or 0
        )
        if stride <= 0:
            # 没有 Texcoord 输入槽时不需要绑定 vb1；调用方会据此跳过资源。
            return b"", 0

        category_buffer = (getattr(drawib_model, "category_buffer_dict", {}) or {}).get(
            "Texcoord"
        )
        if category_buffer is None:
            raise RuntimeError(
                f"[ZZMI骨骼合并] carrier {carrier_ib} 缺少 Texcoord 缓冲，"
                "不能生成与 RedirectSO 对齐的 vb1"
            )
        if hasattr(category_buffer, "tobytes"):
            category_bytes = category_buffer.tobytes()
        else:
            category_bytes = bytes(category_buffer)

        vertex_count = int(carrier_info.get("vertex_count", 0) or 0)
        if vertex_count < 0 or len(category_bytes) != vertex_count * stride:
            raise RuntimeError(
                f"[ZZMI骨骼合并] carrier {carrier_ib} 的 Texcoord 长度不匹配："
                f"实际 {len(category_bytes)} 字节，期望 {vertex_count}*{stride}"
            )

        base_vertex = int(carrier_info.get("base_vertex", 0) or 0)
        if base_vertex < 0:
            raise RuntimeError(
                f"[ZZMI骨骼合并] carrier {carrier_ib} 的 base_vertex 不能为负数: {base_vertex}"
            )
        return (b"\x00" * (base_vertex * stride)) + category_bytes, stride

    def _write_redirect_texcoord_resources(self) -> list[tuple[str, int, str]]:
        """写出所有 carrier 的对齐 Texcoord，并返回 INI 资源定义。"""
        resource_definitions = []
        mod_meshes_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes")
        for carrier_ib, carrier_info in sorted((self._redirect_carrier_map or {}).items()):
            payload, stride = self._build_redirect_texcoord_payload(carrier_ib, carrier_info)
            if stride <= 0:
                continue
            target_ib = carrier_info["target"]
            base_vertex = int(carrier_info.get("base_vertex", 0) or 0)
            resource_name = self._redirect_texcoord_resource_name(
                target_ib, carrier_ib, base_vertex
            )
            filename = self._redirect_texcoord_filename(target_ib, carrier_ib, base_vertex)
            self._atomic_write_binary(os.path.join(mod_meshes_dir, filename), payload)
            resource_definitions.append((resource_name, stride, filename))
        return resource_definitions

    def _write_redirect_blend_resources(self) -> list[tuple[str, int, str]]:
        """写出重放用 Blend 重打包缓冲（载体布局 -> 锚点布局），返回 INI 资源定义。"""
        resource_definitions = []
        mod_meshes_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes")
        for carrier_ib, retarget in sorted((self._redirect_blend_retargets or {}).items()):
            resource_name, stride, filename, payload = retarget
            self._atomic_write_binary(os.path.join(mod_meshes_dir, filename), payload)
            resource_definitions.append((resource_name, stride, filename))
        return resource_definitions

    def add_merged_skeleton_sections(self, ini_builder: M_IniBuilder):
        """生成 ZZMI 合并骨架段（组内统一骨架 + 出现次槽位 v9 版）。

        架构（2026-08-24 用户拍板分组；2026-08-25 移除 CB1 校准；2026-08-26 增加
        依赖就绪守卫；2026-09 v9 出现次槽位，用户游戏内实测通过；2026-10-10 跨组
        骨骼合并定案 = 按来源拆分绘制 + 各回各家，开关 `cross_group_merged_vgmap_test`）：
        - 骨骼 id = 全局编号（组基址拼接组内槽位）；Blender 侧组内 join 无歧义。
        - 骨架**按槽分份**：每组每槽一份 `ResourceZZMergedSkeleton_G<N>_s<k>`
          （array 同现状 = 全局 max(vg_offset+vg_count)）。deform 段按出现次把
          当帧 palette 写进 s<k>，attach 也只写该槽 → 多实例各占一槽、互不覆盖。
        - **跨组别骨骼合并 = 默认关闭的实验总开关**（`cross_group_merged_vgmap_test`）。
          关闭（默认）：各组骨架只含本组骨骼；跨组别引用在导出时大声报警
          （`_warn_cross_group_bone_references`，无校准的运行时这些槽位永远不会被
          写入 = 原点塌陷）。开启：① 跨组件骨骼由 `AttachX_C<部件>_g<宿主组>_s<槽>`
          **直拷**进吸收它的宿主组缓冲（骨骼按全局骨 id 落槽，保持各自对象空间）；
          ② 合并网格的索引按**顶点组归属**切成若干段，每段由
          `_append_merged_render_split_draws` 放回**该来源自己的渲染段**里画——那里
          的 `vs-cb1` 天然就是它自己的当帧对象变换，故不需要任何对象空间校准矩阵，
          也不依赖帧内绘制顺序。
        - **顶层无条件 attach + 每槽守卫**：所有 `run` 都在段顶层（本 fork 里
          if 内的 run 不执行）；到达标记 seen 全部顶层 sticky 累加（if 体内赋值
          会被优化器静态折叠）；守卫体内只有资源绑定与 draw。
        - `[Constants]` 只声明 occ/seen/prev；`[Present]` 先把 seen 抄进 prev、
          再清零 occ/seen（prev 是下一帧守卫的按槽「期望集合」预测值）。
          不生成 drawn/ready 之类闩锁变量，**不在 [Present] 里写任何资源复位**
          （`ResourceZZRedirectSO_* = null` 的 F8 构造经实测有害，会废掉
          [Present] 清场）。
        - 未生成组件无需任何延迟机制，继续走游戏原渲染（当帧 palette）。
        """
        section = M_IniSection(M_SectionType.MergedSkeleton)
        constants_section = M_IniSection(M_SectionType.Constants)
        constants_section.SectionName = "Constants"
        groups = self._merged_skeleton_groups()
        slots = self._merged_skeleton_slots()

        # [Constants] 只声明出现次、到达标记与「上一帧到达」标记（守卫的按槽预测值）；
        # 每帧末由 [Present] 抄录 prev 后清零 seen/occ。
        # prev 初值取 **1**：首帧（还没有上一帧可参考）按「本槽所有必需部件都会到」
        # 保守等待 → 首帧也能在最后一个必需部件处闭合一次；第一帧末 [Present]
        # 就会把真实到达情况抄进来，之后照常预测。
        for skeleton_group in groups:
            # 只要该组有任一部件**参与跨部件实例判定**（跨部件共享通道骨）**且走
            # 重定向路径**，就发出组级通道骨键变量：同一组共享同一条键变量与同一个池
            # （键是逐字节精确的，同分量同实例必然算出同一个键 ⇒ 同槽）。
            #
            # F1（复核发现）：条件里**必须**含重定向路径（`_merged_group_has_key_driven`
            # 已内含）。直连路径（无 target）现在**不发键块**（回 HEAD 行为：
            # 绘制决策按出现次，捕获也必须按出现次，两者不可混用），因此也不能声明
            # `$zz_ms_pose_key_*` —— 否则会留下一个**写不进也读不出**的悬挂全局变量。
            # 旧注释「直连路径也发键块，漏声明会让变量退化成部件级局部变量」只适用于
            # 直连路径真的发键块的假设，与 HEAD 代码不符（更正见 t75 修复报告）。
            if self._merged_group_has_key_driven(skeleton_group):
                constants_section.append(
                    f"global {self._merged_pose_key_var(skeleton_group)} = 0"
                )
        for component_id in range(len(self.merged_skeleton_components)):
            constants_section.append(f"global {self._merged_occ_var(component_id)} = 0")
            for slot in slots:
                constants_section.append(
                    f"global {self._merged_seen_var(component_id, slot)} = 0"
                )
                constants_section.append(
                    f"global {self._merged_prev_var(component_id, slot)} = 1"
                )
        constants_section.new_line()

        # 全宽口径：全局骨骼编号空间的大小 = 全部组件 max(vg_offset+vg_count)
        # （导出子集时 vg_offset 是工作空间全局槽位，可能远超导出内 sum——
        # 同组 3 部件 0~10/11~30/31~50 且中间缺席时 sum=31 但 max=51，按 max 声明）。
        bones_count = max(c["vg_offset"] + c["vg_count"] for c in self.merged_skeleton_components)

        # 每部件每槽 palette 持久副本资源声明（deform VB 段里 copy vs-t0 写入当帧
        # 内容）。type=stride 必须显式声明，否则空声明的 SRV 视图格式不受控。
        #
        # 【2026-09-22 跨显卡修复】这里的 SRV **实际是 typed 16B 元素**，不是结构化
        # 48B —— 因为 palette 是 `copy vs-t0` 的目标，加载器会把源视图的 Format
        # 注入进来（FillInMissingInfo），再在 FillOutBufferDescCommon 用
        # dxgi_format_size(format)=16 覆盖 stride。所以 attach CS 里 palette 必须声明成
        # `Buffer<float4>` 并手工按 `bone*3` 取三个 float4（见 Toolset/
        # zzmi_merged_skeleton_attach.hlsl）。若按 `StructuredBuffer<ZZBone3x4>` 声明，
        # 声明与视图不匹配即落进未定义区：AMD 驱动按描述符(16)取址 → 骨架 2/3 错位
        # → 模型爆炸（实测 A 卡骨架合格率仅 1/3，N 卡正常）。
        # 注意：**不要**给这里加 `format =` —— 加 format 会把 buffer 的
        # StructureByteStride 改成 16、ByteWidth 缩到 16*vg_count，而 copy vs-t0
        # 要拷 48*vg_count 字节，会溢出。
        for component in self.merged_skeleton_components:
            for slot in slots:
                section.append(
                    f"[{self._merged_palette_name(component['draw_ib'], slot)}]"
                )
                section.append("type = Buffer")
                section.append("stride = 48")
                section.append(f"array = {component['vg_count']}")
                section.new_line()
                # 上一帧副本（跨组校准专用，见 _merged_palette_lag_name）：
                # 声明与主副本完全一致（同为 `copy` 目标，视图格式同样由源决定）。
                section.append(
                    f"[{self._merged_palette_lag_name(component['draw_ib'], slot)}]"
                )
                section.append("type = Buffer")
                section.append("stride = 48")
                section.append(f"array = {component['vg_count']}")
                section.new_line()

        # 每部件 vg_map 表（局部骨骼 id -> 合并骨架全局槽位）：attach CS 的 cs-t1
        # 按此写槽位——本部件引用的共享 canonical 槽位当帧覆盖，后续 deform 的
        # 部件读到当帧内容（同帧 bitwise 相同，覆盖无害）。
        # **改用 filename 加载二进制文件（2026-08-23 双帧实证）**：多行 data 在
        # 本 3DMigoto fork 上只写入第 0 个元素（G3 仅 slot 0/79/88 非零，其余
        # 线程 vg_map 读到 0 -> 全部骨骼塌进 slot 0，蒙皮炸裂）。filename 与
        # VB 资源同一加载路径，buffer 大小由文件内容决定，与 format 视图精确
        # 匹配。文件格式：每元素 4×uint32（槽位值, 0, 0, 0）= R32G32B32A32_UINT。
        import struct as _struct

        mod_meshes_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes")
        for component in self.merged_skeleton_components:
            vg_map = component.get("vg_map") or {}
            section.append(f"[ResourceZZVgMap_{component['draw_ib']}]")
            section.append("type = Buffer")
            section.append("format = R32G32B32A32_UINT")
            vgmap_filename = f"zz_vgmap_{component['draw_ib']}.buf"
            section.append("filename = Meshes/" + vgmap_filename)
            section.new_line()
            payload = b"".join(
                _struct.pack("<4I", int(vg_map[local]), 0, 0, 0)
                for local in range(component["vg_count"])
            )
            self._atomic_write_binary(
                os.path.join(mod_meshes_dir, vgmap_filename),
                payload,
            )

        # 跨组：每骨架组一份「对象→世界 cb1」捕获资源（**拆分绘制**用）。
        # 合并网格按来源拆成多次绘制时，每次绘制要绑**该来源自己**的对象变换，而
        # `vs-cb1` 由游戏逐 draw 上传（464 B，前 16 float = 3×4 对象→世界矩阵），在
        # 别的段里读不到，只能在本部件自己的渲染 draw 处拷一份出来：
        #   ResourceZZCb1_G<N> = copy vs-cb1 unless_null
        # （见 add_unity_vs_texture_override_ib_sections，last-wins；同组部件的对象
        # 变换逐位相同，谁写都是同一个矩阵）。
        # **不要**退回历史失败版的「按 IB hash 匹配的 TextureOverrideIB 捕获段」：
        # 同一个 IB 在多个 pass 下 cb1 内容不同（实测同帧两个 pass 差 8.2°），按 hash
        # 匹配会抓到别的 pass 的常量块 ⇒ 整个变换错，这正是 2026-08-25 那版被
        # 删除的直接原因（见 ZZMI骨骼合并计划书.md）。
        # 资源不声明 type/stride：`copy vs-cb1` 是常量块整体拷贝，尺寸与布局由源视图
        # 决定；随后被 `vs-cb1 = ResourceZZCb1_G<N>` 整块绑回常量槽（与已投产的
        # `ResourceCaptureCB1_*` 完全同型，用户实测有效）。
        if self._merged_cross_group_enabled():
            for skeleton_group in groups:
                section.append(
                    f"[{self._merged_cb1_capture_name(int(skeleton_group))}]"
                )
                section.append(
                    "; F2 跨组校准：由本组部件的渲染 draw 处 "
                    "`copy vs-cb1 unless_null` 写入（last-wins）"
                )
                section.new_line()

        # 每槽一份 SO 重定向资源（**按骨架组命名空间化**）。只有 SO owner（载体）
        # 部件的 deform 段捕获 `ref so0`；target 先到时由自身捕获，纯占位 target
        # 则由兼容的 carrier 捕获，避免 target 晚到时把有效 SO 覆盖为空。
        #
        # 必须按组区分：一个导出里可以同时存在多个发生重定向的骨架组，而
        # `ResourceZZRedirectSO_<...>` 是**全局变量**——若按槽共享，两组的捕获会
        # 互相覆盖，先闭合守卫的那组会把合并几何写进另一组的 SO（2026-09-16
        # 叶瞬光01 游戏内实测：模型爆炸 + 持续闪烁的根因）。
        so_stride_by_group_slot: dict[tuple[int, int], int] = {}
        for skeleton_group in groups:
            group_component_ids = set(self._merged_group_component_ids(skeleton_group))
            for target_ib, plan in self._redirect_target_map.items():
                target_component_id = self.merged_skeleton_component_id_dict.get(target_ib)
                if (
                    target_component_id is None
                    or int(target_component_id) not in group_component_ids
                ):
                    continue
                for slot in slots:
                    so_stride_by_group_slot.setdefault(
                        (int(skeleton_group), int(slot)),
                        int(plan.get("so_stride", 40)),
                    )
        for skeleton_group in groups:
            for slot in slots:
                section.append(
                    f"[{self._merged_redirect_so_name(skeleton_group, slot)}]"
                )
                section.append("type = Buffer")
                section.append(
                    "stride = "
                    + str(int(so_stride_by_group_slot.get((int(skeleton_group), int(slot)), 40)))
                )
                section.new_line()

        # 自建蒙皮输出（`ResourceZZSkinOut_G<g>_s<k>`）：跨组蒙皮 CS 的 UAV 写目标，
        # 同时是跨组拆分绘制的 `vb0`。**必须是自建缓冲**——写游戏 SO 的别名
        # （`ResourceZZRedirectSO_* = ref so0`）会因 bind flag 不兼容而创建失败
        # （源无 `unordered_access`、目标要当 UAV 写），失败后绑定它的绘制全部
        # 无效 ⇒ 角色展示页整屏卡死。详见 `_merged_skin_out_name` 的日志铁证。
        # 只给跨组发布计划发：重定向路径不读该别名当 VB，继续走 RedirectSO。
        for skeleton_group, plan in sorted(
            self._merged_cross_group_publish_plans().items()
        ):
            rows = self._merged_skin_out_row_count(plan)
            if rows <= 0:
                continue
            for slot in slots:
                section.append(
                    f"[{self._merged_skin_out_name(skeleton_group, slot)}]"
                )
                section.append("type = Buffer")
                section.append(
                    "stride = " + str(int(plan.get("so_stride", 40) or 40))
                )
                section.append("array = " + str(int(rows)))
                section.append("bind_flags = unordered_access vertex_buffer")
                section.new_line()

        # RedirectSO 使用 DrawIndexed 的 base_vertex 读取合并 Position；D3D11 会
        # 将这个偏移同时应用到 vb1，因此必须给每个 carrier 的 Texcoord 前面补
        # 同样数量的顶点行。否则位置与 UV 会错位，表现为 UV 整体乱跳/串块。
        redirect_texcoord_resources = self._write_redirect_texcoord_resources()
        # 载体 Blend 布局与组内锚点布局不一致时，重打包一份锚点布局的 vb2，
        # 使合并几何能在任意锚点挂点（而不仅是载体自己那一段）重放。
        redirect_blend_resources = self._write_redirect_blend_resources()

        # 每组每槽一份合并骨架（组内统一：只直拷本组骨骼，跨组别禁止合并）。
        #
        # O3（用户裁定 (a)：诊断先行）——导出期打一行「捕获 : 消费」结构比汇总。
        # 口径 = **发射点数**（不是运行次数）：`captures` = 该 (组,槽) 的 referent 捕获点数
        # （`= ref so0`），`consumers` = 绑定该别名的重放块 + 守卫内发布块数。
        # 依据（t8 块口径，代次 `184431` / `log.txt` sha256 `fd6efcc7…a18471`）：该帧部件口径
        # **捕获 : 重放 = 1 : 1**、组口径 6 : 1 属**设计行为** ⇒ 本行**不得**被读成"复用次数"，
        # 也不得据此声称任何可见现象已修复。
        for record in self._merged_reuse_ratio_records():
            self._merged_diag(
                ZZMI_MERGE_DIAG_REUSE_RATIO,
                "结构比 = 发射点数（非运行次数）；不得读成复用次数。",
                group=record["group"],
                slot=record["slot"],
                captures=record["captures"],
                consumers=record["consumers"],
                ratio=record["ratio"],
            )
        #
        # 设计上界必须**显式落进产物**（D1 强制要求 / AC-A3）：同帧同部件出现次数 >
        # len(SLOTS) 时 occ 回绕把第 3 笔标回槽 1，槽内混两个实例的 palette。
        # 导出期**逐组**声明（含组号 / 部件数 / 设计上界，D1 要求的字段），运行时是否
        # **真的**超界由 attach 段的 `x3` 探针透出（帧分析日志 `ini param override = 2`）。
        # 注意：运行期实例数在导出期不可知（同一 DrawIB 的多次实例不在导出数据里），
        # 因此这里声明的是**上界**与**超界形态**，不是「已检测到超界」。
        for skeleton_group in groups:
            self._append_merged_diag(
                section,
                self._merged_diag(
                    ZZMI_MERGE_DIAG_SLOT_BOUND,
                    "超过该次数的同帧实例会回绕复用槽位（槽内混实例）。",
                    group=f"G{int(skeleton_group)}",
                    components=len(self._merged_group_component_ids(skeleton_group)),
                    slots=",".join(str(int(slot)) for slot in slots),
                    occ_wrap=ZZMI_MERGED_SKELETON_OCC_WRAP,
                    max_same_frame_occurrences=len(slots),
                    overflow=f"occurrence>{len(slots)}_wraps_to_slot_{slots[0]}",
                    runtime_probe="x3=$zz_ms_seen_<i>" + str(slots[0]),
                ),
            )
        for skeleton_group in groups:
            for slot in slots:
                section.append(
                    f"[{self._merged_skeleton_name(skeleton_group, slot)}]"
                )
                # 【2026-09-22 跨显卡修复】合并骨架必须是 typed buffer，不能是结构化。
                #
                # 游戏的 deform VS 按 **typed 索引** `palette[3*bone + m]` 读取本缓冲
                # （每根骨 3 个 float4）。若本资源声明成结构化（SBS=48），则出现
                # 「视图元素尺寸 vs 声明步长」不匹配，落进 D3D11 未定义区：
                #   · NVIDIA 驱动按视图元素尺寸(16)取址 → (3b+m)*16 = 48b+16m → 正确；
                #   · AMD 驱动按 buffer 的 StructureByteStride(48)取址 → 骨 b 实际读到
                #     槽位 3b/3b+1/3b+2 的矩阵混合 → 错位；且 3b+m 越过写入区间时
                #     越界读返回精确 0 → 顶点塌到原点（实测 A 卡 5939/11111 个顶点为零，
                #     阈值恰为 ⌈bones_count/3⌉，即「索引被放大 3 倍」的唯一指纹）。
                #
                # 改成 typed（format + array=3*bones_count，字节数不变）后，SRV 元素
                # 尺寸 = 16B，与 `palette[3b+m]` 精确匹配 → 两卡取址一致。
                #
                # bind_flags 必须显式声明（EFMI 的 ResourceMergedSkeletonDataRW 同款坑）：
                # RWBuffer 缺 bind_flags 会导致资源创建失败，且**引用路径不会补** ——
                # SRV 创建静默失败后绑 NULL，deform 读到全零（实测过一次，骨架内容
                # 完全正确但 4 个 SO 全部 100% 零）。
                section.append("type = RWBuffer")
                section.append("format = R32G32B32A32_FLOAT")
                section.append("array = " + str(bones_count * 3))
                section.append("bind_flags = shader_resource unordered_access")
                section.new_line()

        # 逐 (部件, 槽) attach 段（y1 = vg_count；仅由 deform VB 段顶层调用）。
        # Dispatch 按 HLSL numthreads(64,1,1) 动态取整，避免 palette > 512 时
        # 固定 8 组漏掉尾部骨骼。
        for slot in slots:
            for component_id, component in enumerate(self.merged_skeleton_components):
                vg_count = int(component["vg_count"])
                dispatch_count = max(
                    1,
                    (vg_count + self.MERGED_SKELETON_ATTACH_THREADS - 1)
                    // self.MERGED_SKELETON_ATTACH_THREADS,
                )
                section.append(f"[{self._merged_attach_name(component_id, slot)}]")
                section.append("flags = optimization_level3 all_resources_bound skip_validation")
                section.append("cs = ./res/zzmi_merged_skeleton_attach.hlsl")
                section.append("x1 = 0")
                section.append(f"y1 = {vg_count}")
                # 新鲜度守卫（2026-10-20，丽娜槽 97 漂移根因）：跨部件骨骼矩阵
                # 去重后同一个 canonical 槽位会被**多个部件**写，而每个部件的
                # palette 只在**它自己的 deform 段**捕获。某部件本帧没画（例如
                # 只剩 3 个顶点的子网格被裁掉）时它的 palette 停在上一帧，此时
                # 再跑它的 attach 就会用**上一帧矩阵**盖掉本帧刚写好的共享槽位
                # ⇒ 该骨骼（本例头/发 4046 个顶点全压在槽 97）运动时滞后漂移。
                # y2 = 1 启用守卫，z2 = 该 (部件, 槽) 的本帧到达计数（seen）：
                # 计数 0 ⇒ 本帧没捕获 ⇒ CS 直接返回，槽里保留**当帧**值，绝不
                # 把新值降级成旧值。y2 缺省 0 = 关闭守卫（未升级的段逐字节不变）。
                # 用第 2 组参数而不是 z1/w1：第 1 组已被 y1=骨骼数与蒙皮 CS 的
                # z1/w1 占用。
                section.append("y2 = 1")
                section.append(
                    f"z2 = {self._merged_seen_var(component_id, slot)}"
                )
                section.append(
                    f"cs-t0 = ref {self._merged_palette_name(component['draw_ib'], slot)}"
                )
                section.append(f"cs-t1 = ref ResourceZZVgMap_{component['draw_ib']}")
                section.append(
                    "cs-u0 = ref "
                    + self._merged_skeleton_name(
                        int(component["skeleton_group"]), slot
                    )
                )
                section.append(f"Dispatch = {dispatch_count}, 1, 1")
                section.append("cs-u0 = null")
                component_key_driven = self._merged_component_is_key_driven(component)
                if slot == slots[0]:
                    # 诊断探针（主症①b / AC-A3 的**运行时**失败标记）：把本部件在
                    # 槽 <slots[0]> 的到达计数透到 IniParams。该值是帧内单调累加
                    # `seen = seen + (occ == slot)`，静态注释 `SLOT_BOUND` 只能声明
                    # 上界，而这里能在帧分析日志里直接读出「本帧是否超界」：
                    # `ini param override = 2` ⇒ 同帧同部件出现次数 > len(SLOTS)
                    # ⇒ 第 3 笔已被 occ 回绕复用槽位（槽内混了实例）。
                    section.append(f"x3 = {self._merged_seen_var(component_id, slot)}")
                if component_key_driven:
                    # 诊断探针：把本 pass 的通道骨键（HashRegion，逐字节精确）透到
                    # IniParams，帧分析日志里会以 `ini param override = <值>` 出现
                    # （每个部件每次 deform 一条），用来核对两个实例是否算出不同的
                    # 键、以及同分量跨部件是否算出同一个键。
                    pose_key_var = self._merged_pose_key_var(int(component["skeleton_group"]))
                    section.append(f"x2 = {pose_key_var}")
                section.new_line()

        # 跨组 attach（F1）：本组几何吸收了**别组**部件时，把那些部件的骨骼写进本组
        # 缓冲。段名带宿主组号（`AttachX_C<跨组件>_g<宿主组>_s<槽>`），与写自身组的
        # `Attach_C<跨组件>_s<槽>` 并存、互不覆盖；调色板/VgMap 仍取**跨组件自己**的
        # 资源（它的 deform 段照常跑、palette 照常捕获），只有写入目标换成宿主组缓冲。
        #
        # 残余（写进产物注释，不许当成"已修复"）：跨组件的 palette 捕获发生在它自己
        # 的 deform 段，帧内顺序若在宿主段之后，则本段读到的是**上一帧**的姿态。
        # 静止时帧差 ≈ 0（实测 0.00000°/6e-6 单位），运动中可能有一帧级延迟。
        for skeleton_group in groups:
            cross_component_ids = self._merged_cross_group_component_ids(skeleton_group)
            if not cross_component_ids:
                continue
            if self._merged_cross_group_enabled():
                offending = sorted(
                    self._merged_cross_group_offending_bone_ids(skeleton_group)
                )
                self._append_merged_diag(
                    section,
                    self._merged_diag(
                        ZZMI_MERGE_DIAG_CROSS_GROUP_ATTACH,
                        "跨组骨骼合并（F1）：别组部件的骨骼写入吸收它的宿主组缓冲。",
                        group=f"G{int(skeleton_group)}",
                        components=",".join(str(int(cid)) for cid in cross_component_ids),
                        bone_ids=",".join(str(int(bone_id)) for bone_id in offending),
                        owner_groups=",".join(
                            sorted(
                                {
                                    f"G{int(self.merged_skeleton_components[cid]['skeleton_group'])}"
                                    for cid in cross_component_ids
                                }
                            )
                        ),
                    ),
                )
            for cross_component_id in cross_component_ids:
                cross_component = self.merged_skeleton_components[cross_component_id]
                vg_count = int(cross_component["vg_count"])
                dispatch_count = max(
                    1,
                    (vg_count + self.MERGED_SKELETON_ATTACH_THREADS - 1)
                    // self.MERGED_SKELETON_ATTACH_THREADS,
                )
                for slot in slots:
                    section.append(
                        "["
                        + self._merged_cross_attach_name(
                            cross_component_id, skeleton_group, slot
                        )
                        + "]"
                    )
                    section.append(
                        "flags = optimization_level3 all_resources_bound skip_validation"
                    )
                    # 拆分绘制路线：跨组 attach 也是**直拷** —— 源件骨骼留在**它自己的
                    # 对象空间**里（不做任何换算），几何由该来源自己的渲染段用**它自己的
                    # cb1** 单独绘制一次摆到世界（`_append_merged_render_split_draws`）。
                    # 因此这里既不需要校准常量缓冲，也不需要校准版 CS。
                    section.append(
                        "cs = " + self._merged_calibrated_attach_shader_path()
                    )
                    section.append("x1 = 0")
                    section.append(f"y1 = {vg_count}")
                    # 与同组 attach 同一守卫（见同组发射点的长注释）：
                    # 跨组件的 palette 同样只在**它自己**的 deform 段捕获，
                    # 本帧没画时读到的是上一帧姿态 ⇒ 不许它盖掉本帧值。
                    section.append("y2 = 1")
                    section.append(
                        f"z2 = {self._merged_seen_var(int(cross_component_id), slot)}"
                    )
                    section.append(
                        "cs-t0 = ref "
                        + self._merged_palette_name(cross_component["draw_ib"], slot)
                    )
                    section.append(
                        f"cs-t1 = ref ResourceZZVgMap_{cross_component['draw_ib']}"
                    )
                    # 拆分绘制路线：attach 直拷源件骨骼（留在源件自己的对象空间），
                    # 由源件自己的 cb1 在它那一段绘制里摆到世界 ⇒ 不需要校准常量缓冲。
                    section.append(
                        "cs-u0 = ref "
                        + self._merged_skeleton_name(int(skeleton_group), slot)
                    )
                    section.append(f"Dispatch = {dispatch_count}, 1, 1")
                    section.append("cs-u0 = null")
                    section.new_line()

        # ---------------------------------------------------------------
        # 通道骨键 → 槽位 的池（t75 唯一判定口径）
        #
        # 出现次是**位置标签**：引擎按 mesh+instance 排序提交 deform，两个实例的
        # 相对先后可以逐部件不同（实测 033720：G2 四件的先后切分是 2-2） ⇒「第 1
        # 次出现」对某些部件是 A、对另一些是 B，同一槽骨架混进两份姿态。
        # 改用**共享骨连通分量**内被最多部件引用的通道骨、以 `HashRegion(48*local,
        # 48)` 的**逐字节精确**哈希当键（同实例同帧同分量逐位相同；不同实例差
        # 1e-7…1e-2，远在 0.01 格之下 ⇒ 必须精确匹配）。
        # 池按帧过期（pool_expiration_timeout_frames = 1）= 每帧重新分配；
        # `pool_index_type = fifo`：键是 32 位整数，全位精确匹配——**不能**再用
        # `spatial` + `pool_spatial_radius`（会把两个实例并键，实测并键率最高
        # 99/135）。池容量按「该组连通分量数 × 槽位数 × 实例上界」定，见下。
        # ---------------------------------------------------------------
        pool_size_by_group = self._merged_pose_key_pool_sizes(groups)
        for skeleton_group in groups:
            # 池容量只由「该组是否有参与判定的部件（且走重定向路径）」决定
            # （`_merged_pose_key_pool_sizes` 已按 `_merged_group_has_key_driven`
            # 过滤）。F1：直连路径**不发键块**（绘制按出现次，捕获也必须按出现次），
            # 因此这里也不发池——发了会留下没有任何消费者的空池段。
            pool_size = pool_size_by_group.get(int(skeleton_group), 0)
            if pool_size <= 0:
                continue
            section.append(f"[{self._merged_pose_key_pool_prefix(skeleton_group)}]")
            section.append(f"pool_size = {int(pool_size)}")
            section.append("pool_index_type = fifo")
            section.append("pool_variable_default_value = 0")
            section.append("pool_expiration_timeout_frames = 1")
            section.append("pool_expiration_reset_elements = 1")
            section.new_line()
            section.append(f"[{self._merged_pose_slot_taken_pool(skeleton_group)}]")
            section.append("pool_size = 4")
            section.append("pool_variable_default_value = 0")
            section.append("pool_expiration_timeout_frames = 1")
            section.append("pool_expiration_reset_elements = 1")
            section.new_line()

        # ---------------------------------------------------------------
        # 合并几何蒙皮 CS + 发布 CommandList（2026-09-17「闪」修复）        #
        # draw 版重放受 IA 输入布局限制，只能在「兼容锚点」落笔；窄布局必需部件
        # 排在最后到达时没有任何锚点能写 → 该槽 SO 整帧不写 → 用户实测闪烁。
        # 蒙皮 CS 按 SV_DispatchThreadID 从 SRV 读顶点属性（不经过 IA 布局），
        # 因此**任意必需部件的 deform 段都能发布**；按索引写 = 幂等，
        # 帧内最后一次派发（最后一个必需部件到达处）用最完整骨架覆盖。
        # draw 版重放**保留**作为兜底（CS 绑定失败时行为与旧版一致）。
        # 详见 Toolset/zzmi_merged_skin.hlsl 顶部说明与 ZZMI骨骼合并计划书 §7 修复链 10。
        #
        # F3-CS（2026-10-10）：**跨组吸收宿主组**同样要发这套段定义。它没有重定向
        # 计划（直连路径），但合并几何混进了别组顶点，宿主自己那段写 SO 时跨组
        # palette 往往还是上一帧的（实测宿主 deform 早于跨组件）⇒ 必须能在「最后
        # 一个供骨件到达处」用当帧骨架重发布。见 `_merged_cross_group_publish_plan`。
        # ---------------------------------------------------------------
        for skeleton_group in groups:
            self._append_merged_skin_cs_sections(
                section,
                int(skeleton_group),
                self._merged_group_redirect_plan(skeleton_group),
                slots,
            )
        for skeleton_group, cross_plan in sorted(
            self._merged_cross_group_publish_plans().items()
        ):
            self._append_merged_skin_cs_sections(
                section, int(skeleton_group), cross_plan, slots, use_skin_out=True
            )

        # [Present]：先把本帧到达情况抄进 `$zz_ms_prev_<i><k>`（下一帧守卫的按槽预测
        # 值），再清零 occ/seen（跨帧兜底）。
        # 教训（2026-09 实测回归）：不要在 [Present] 里写 RedirectSO 资源复位
        # （ResourceZZRedirectSO_<ib> = null，即原 F8 防御性构造）。该语句会
        # 废掉 [Present] 段的正常执行，使 occ/seen 的跨帧清场失效 → 第二实例
        # 加入/被剔除的过渡帧残留半组状态，随后错槽重放，表现为后加入实例
        # 闪烁直至卡死无动画。RedirectSO 只在同槽「deform 捕获 → 守卫重放」
        # 窗口内使用，渲染段不引用，无需帧末复位。
        present_section = M_IniSection(M_SectionType.Present)
        present_section.SectionName = "Present"
        for component_id in range(len(self.merged_skeleton_components)):
            for slot in slots:
                present_section.append(
                    f"{self._merged_prev_var(component_id, slot)} = "
                    f"{self._merged_seen_var(component_id, slot)}"
                )
            present_section.append(f"{self._merged_occ_var(component_id)} = 0")
            for slot in slots:
                present_section.append(
                    f"{self._merged_seen_var(component_id, slot)} = 0"
                )
        present_section.new_line()

        ini_builder.append_section(section)
        ini_builder.append_section(constants_section)
        ini_builder.append_section(present_section)

        if redirect_texcoord_resources or redirect_blend_resources:
            resource_section = M_IniSection(M_SectionType.ResourceBuffer)
            for resource_name, stride, filename in (
                list(redirect_texcoord_resources) + list(redirect_blend_resources)
            ):
                resource_section.append(f"[{resource_name}]")
                resource_section.append("type = Buffer")
                resource_section.append(f"stride = {stride}")
                resource_section.append(f"filename = Meshes/{filename}")
                resource_section.new_line()
            ini_builder.append_section(resource_section)

    def _copy_merged_skeleton_shader_to_mod(self):
        """把合并骨架 attach CS 与合并几何蒙皮 CS 复制到生成 Mod 的 res/ 目录。"""
        addon_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        shader_names = [
            "zzmi_merged_skeleton_attach.hlsl",
            self._merged_skin_shader_filename(),
        ]
        # 拆分绘制路线不使用校准版 attach：跨组 attach 与同组一样是**直拷**，源件骨骼
        # 留在自己的对象空间，由该来源自己的渲染段用**它自己的 cb1** 单独绘制一次
        # （见 `_append_merged_render_split_draws`）。因此 res/ 里只有直拷 attach 与
        # 蒙皮 CS 两个着色器。
        res_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "res")
        for shader_name in shader_names:
            shader_src = os.path.join(addon_root, "Toolset", shader_name)
            if not os.path.isfile(shader_src):
                raise FileNotFoundError(f"未找到 ZZMI 合并骨架着色器: {shader_src}")
            with open(shader_src, "rb") as shader_file:
                shader_payload = shader_file.read()
            self._atomic_write_binary(
                os.path.join(res_dir, shader_name),
                shader_payload,
            )

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        super().add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)

        # 多次预蒙皮 draw 的组件，各自的 VB 资源段（缓冲文件按组件单独写出）
        expand_groups = self._zzmi_pose_expand_groups(drawib_model)
        if expand_groups and not self.has_cross_ib:
            from ...blueprint.export_helper import BlueprintExportHelper
            buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
            expand_resource_section = M_IniSection(M_SectionType.ResourceBuffer)
            for component_suffix, _category_hash_dict, _vertex_count in expand_groups:
                for category_name in drawib_model.d3d11GameType.OrderedCategoryNameList:
                    expand_resource_section.append(
                        "[Resource" + drawib_model.draw_ib + category_name + "_" + component_suffix + "]"
                    )
                    expand_resource_section.append("type = Buffer")
                    expand_resource_section.append(
                        "stride = " + str(drawib_model.d3d11GameType.CategoryStrideDict[category_name])
                    )
                    expand_resource_section.append(
                        "filename = " + buffer_folder_name + "/" + drawib_model.draw_ib
                        + "-" + category_name + "-" + component_suffix + ".buf"
                    )
                    expand_resource_section.new_line()
            ini_builder.append_section(expand_resource_section)

        position_stride = drawib_model.d3d11GameType.CategoryStrideDict.get("Position", 40)
        so0_resource_section = M_IniSection(M_SectionType.ResourceBuffer)
        appended_resource_names = set()
        for submesh_model in drawib_model.submesh_model_list:
            source_ib_key = self._get_submesh_ib_key(submesh_model, drawib_model.draw_ib)
            if self.CROSS_IB_METHOD_VB_REF_SO0 not in self._get_source_methods(source_ib_key):
                continue

            resource_name = self._get_source_so0_resource_name(drawib_model.draw_ib, submesh_model.match_first_index)
            if resource_name in appended_resource_names:
                continue
            appended_resource_names.add(resource_name)

            so0_resource_section.append("[" + resource_name + "]")
            so0_resource_section.append("type = Buffer")
            so0_resource_section.append("stride = " + str(position_stride))
            so0_resource_section.new_line()

        ini_builder.append_section(so0_resource_section)

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)
        draw_ib = drawib_model.draw_ib

        print(f"[CrossIB ZZMI] 处理 draw_ib={draw_ib}, has_cross_ib={self.has_cross_ib}")

        # 展开模式（一个 IB 配多次预蒙皮 draw，例如翅膀的 4 个推进器）：
        # 当各组件只是「同一套索引 + 顶点段偏移」时，**只为一个代表组件**生成 render 段。
        # 游戏里该 IB 的多次 render draw 参数完全相同（DrawIndexed(918,0,0)），唯一区别是
        # vb0 指向各自实例的预蒙皮 SO 输出（各 277 顶点）；逐个组件都发段会让每次实例
        # draw 重画全部组件，其中带偏移的索引还会越界（实测用户看到的「多出一片高频
        # 闪烁的扭曲网格」）。判据与理由见 `_zzmi_expand_render_representative`。
        expand_render_suffix = ""
        _expand_groups = self._zzmi_pose_expand_groups(drawib_model)
        if (
            _expand_groups
            and not self.has_cross_ib
            and not self.merged_skeleton_component_id_dict.get(draw_ib)
            and not self.blueprint_model.keyname_mkey_dict.keys()
        ):
            expand_render_suffix = self._zzmi_expand_render_representative(
                drawib_model, _expand_groups
            )
            if expand_render_suffix:
                print(
                    f"[ZZMI多预蒙皮] {draw_ib}: 展开组件索引同构（归一化后逐字节相同），"
                    f"只为代表组件 {expand_render_suffix} 发 render 段；"
                    "其余组件的几何由各自实例的那次 render 承担"
                )

        texture_override_ib_section.append("[TextureOverride_IB_" + draw_ib + "]")
        texture_override_ib_section.append("hash = " + draw_ib)
        texture_override_ib_section.append("handling = skip")
        texture_override_ib_section.new_line()

        for submesh_model in drawib_model.submesh_model_list:
            if expand_render_suffix:
                _submesh_suffix = str(
                    getattr(submesh_model, "workspace_unique_str", "")
                    or submesh_model.unique_str
                ).rsplit("-", 1)[-1]
                if _submesh_suffix != expand_render_suffix:
                    # 展开组（一个 IB 多次预蒙皮）：非代表组件不再发 render 段，
                    # 否则每次实例 draw 都会重画全部组件 → 带偏移索引越界（扭曲）
                    # 且多份几何深度冲突（高频闪烁）。见本方法开头的判定。
                    continue
            texture_override_name_suffix = drawib_model.get_submesh_texture_override_suffix(submesh_model)
            ib_resource_name = drawib_model.get_submesh_ib_resource_name(submesh_model)

            current_ib_key = self._get_submesh_ib_key(submesh_model, draw_ib)
            is_cross_ib_source = current_ib_key in self.cross_ib_info_dict
            is_cross_ib_target = any(current_ib_key in targets for targets in self.cross_ib_info_dict.values())

            print(
                f"[CrossIB ZZMI] submesh={submesh_model.unique_str}, ib_key={current_ib_key}, "
                f"is_source={is_cross_ib_source}, is_target={is_cross_ib_target}"
            )

            source_ib_list_for_target = []
            if is_cross_ib_target:
                for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                    if current_ib_key in target_ib_list:
                        source_ib_list_for_target.append(source_ib)

            source_methods = self._get_source_methods(current_ib_key) if is_cross_ib_source else set()
            if is_cross_ib_source:
                self._append_source_capture_sections(
                    texture_override_ib_section,
                    draw_ib,
                    submesh_model.match_first_index,
                    source_methods,
                )
            elif self.CROSS_IB_METHOD_VB_COPY_CB1 in {
                self._get_mapping_method(source_ib_key, current_ib_key)
                for source_ib_key in source_ib_list_for_target
            }:
                texture_override_ib_section.append(
                    "[" + self._get_target_cb1_temp_resource_name(draw_ib, submesh_model.match_first_index) + "]"
                )

            if is_cross_ib_source:
                self._append_source_capture_override(
                    texture_override_ib_section,
                    texture_override_name_suffix,
                    draw_ib,
                    submesh_model.match_first_index,
                    source_methods,
                )
                texture_override_ib_section.new_line()

            # 合并网格自动重定向：渲染身份必须仍归属于原始 DrawIB/物体。
            #
            # 变形阶段可以把 carrier 的几何写入 target 的 RedirectSO，但这不
            # 等于渲染阶段也要把 carrier 的 TextureOverride 改挂到 target hash。
            # 以前这里复用 target hash + target first_index，会让不同物体落到同
            # 一个运行时匹配键下：纹理、透明、shader replace 和 mesh 备注互相
            # 覆盖；target 的占位段还会用 ib=null 把对应物体整个跳过。
            #
            # 现在每个段始终使用自己的 hash/first_index。carrier 的合并行由**本实例**
            # 的 deform 段重放写入本实例 SO，渲染段沿用游戏原生 vb0（不再覆写
            # RedirectSO）；target/缺失部件的占位 IB 保持可见（几何尺寸为 1e-6），
            # 不再使用 ib=null 作为“跳过”手段。
            redirect_carrier_info = self._redirect_carrier_map.get(draw_ib)
            override_hash = draw_ib
            override_first_index = submesh_model.match_first_index

            texture_override_ib_section.append("[TextureOverride_" + texture_override_name_suffix + "]")
            texture_override_ib_section.append("hash = " + override_hash)
            texture_override_ib_section.append("match_first_index = " + str(override_first_index))

            # F2 跨组校准：在本部件**自己的渲染 draw** 处捕获本骨架组的
            # 「对象→世界 cb1」到 `ResourceZZCb1_G<N>`。
            #
            # 为什么在这里：跨组 attach 需要把被吸收部件的骨骼从**源对象空间**
            # 换算到**宿主对象空间**，换算矩阵 C = U_dst^-1 × U_src 的两个 U 就是
            # 两个对象各自的渲染 cb1。渲染段是本 mod 唯一能确定「此刻 vs-cb1 就是
            # 这个物体的对象→世界矩阵」的位置（游戏在该 draw 前逐物体 Map 上传 cb1，
            # 实测 dump：draw 000204 前 `Map hash=30cb248b`）。
            #
            # 历史失败版是 `[TextureOverrideIB_<IB>_Cb1Capture]` + `hash = <源 IB>`：
            # 同一个 IB 会被多个 pass 匹配（实测同帧两个 pass 的 cb1 差 8.2°），
            # 抓到哪个 pass 取决于提交顺序 ⇒ 校准矩阵整个错、模型偏移。改到渲染段后
            # 每个捕获都严格对应一个真实物体的渲染 draw。
            #
            # last-wins：同组各部件写的是同一个矩阵（分组的定义就是对象变换逐位相同），
            # 覆盖无害。每个 DrawIB 只在本段的第一条子网格处追加一次（该 DrawIB 的
            # 所有子网格共用同一个渲染段对象）。
            if self.has_merged_skeleton and self._merged_cross_group_enabled():
                first_indices = self._drawib_first_match_first_index(draw_ib)
                if first_indices and int(override_first_index) == int(first_indices[0]):
                    capture_component_id = self.merged_skeleton_component_id_dict.get(draw_ib)
                    if capture_component_id is not None:
                        capture_group = int(
                            self.merged_skeleton_components[int(capture_component_id)].get(
                                "skeleton_group", 0
                            )
                            or 0
                        )
                        texture_override_ib_section.append(
                            f"{self._merged_cb1_capture_name(capture_group)}"
                            " = copy vs-cb1 unless_null"
                        )

            # 2026-09 多实例分离 v2（用户实测通过）：carrier 的渲染 draw
            # **不再覆写 vb0**。旧实现 `vb0 = ResourceZZRedirectSO_<target>` 把该
            # IB 的所有实例都钉到同一个 SO 资源变量上（后到实例的捕获会改指向，
            # 先到实例的渲染因此读到别的实例的蒙皮结果）。游戏渲染 draw 的 vb0
            # 天然是本实例 deform 的 SO（so0=vb0 指针族严格分实例），重放已把
            # 本实例的合并行写进去；索引仍属于 carrier，渲染匹配键仍保持
            # carrier hash，不会与 target 或同 DrawIB 的其它子网格串台。

            ib_buf = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, None)
            if ib_buf is None or len(ib_buf) == 0:
                if self.has_merged_skeleton:
                    raise RuntimeError(
                        f"[ZZMI骨骼合并] 子网格 {submesh_model.unique_str} 的索引缓冲为空；"
                        "合并骨架导出禁止以 ib=null/IB skip 静默跳过，请重新导出以生成"
                        "对应的物体或极限小三角占位"
                    )
                texture_override_ib_section.append("ib = null")
                texture_override_ib_section.new_line()
                continue

            texture_override_ib_section.append("ib = " + ib_resource_name)

            # 合并网格渲染换绑：导出顶点数超过原部件顶点数时（= 本对象把同组
            # 其它部件的几何也合并了进来），渲染 draw 必须把 vb1 换绑为本 mod
            # 的 Texcoord buffer——游戏原 vb1 只覆盖原部件顶点数，合并网格的
            # 索引会越界读（D3D11 OOB 返回 0，UV 全糊到 (0,0) 角落）。
            # 数量不超时保持游戏原绑定（数据同源，零行为变化）。
            if redirect_carrier_info is not None:
                # DrawIndexed 的 base_vertex 会作用于 vb0/vb1 的所有输入槽；
                # 使用导出阶段补齐前缀的 carrier Texcoord，保证与 RedirectSO
                # 中的 Position 行保持同一顶点索引。
                texcoord_stride = int(
                    drawib_model.d3d11GameType.CategoryStrideDict.get("Texcoord", 0)
                    or 0
                )
                if texcoord_stride > 0:
                    base_vertex = int(redirect_carrier_info.get("base_vertex", 0) or 0)
                    texcoord_resource_name = self._redirect_texcoord_resource_name(
                        redirect_carrier_info["target"], draw_ib, base_vertex
                    )
                    texture_override_ib_section.append(f"vb1 = {texcoord_resource_name}")
            elif (
                int(getattr(submesh_model, "vertex_count", 0) or 0)
                > int(getattr(submesh_model, "original_vertex_count", 0) or 0)
                and int(getattr(submesh_model, "original_vertex_count", 0) or 0) > 0
            ):
                texture_override_ib_section.append(f"vb1 = Resource{draw_ib}Texcoord")

            texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
            if not GlobalProterties.forbid_auto_texture_ini() and texture_markup_info_list:
                slot_fix_enabled = GlobalProterties.zzz_use_slot_fix()
                uses_slot_fix = False

                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(texture_markup_info.mark_type):
                        continue

                    slot_fix_resource_name = self.SLOT_FIX_RESOURCE_NAME_DICT.get(texture_markup_info.mark_name)
                    if slot_fix_enabled and slot_fix_resource_name is not None:
                        texture_override_ib_section.append(
                            slot_fix_resource_name + " = ref " + texture_markup_info.get_resource_name()
                        )
                        uses_slot_fix = True
                    else:
                        texture_override_ib_section.append(
                            texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name()
                        )

                if uses_slot_fix:
                    texture_override_ib_section.append(r"run = CommandList\ZZMI\SetTextures")

            if texture_markup_info_list:
                texture_override_ib_section.append("run = CommandListSkinTexture")

            # 跨组：合并网格按**来源骨架组**拆成多次绘制，每段放回该来源自己的渲染段里
            # 画（`_append_merged_render_split_draws`）。对象变换 cb1 是**每次绘制一份**
            # 的常量（464 B，前 16 float = 3×4 对象→世界矩阵），顶点没有索引它的机制，
            # 而合并后的网格里有两群顶点属于不同对象空间 ⇒ 只能拆绘制；骨骼矩阵仍然全部
            # 合并在同一份骨架缓冲里（「一个物体用所有 IB 的骨骼矩阵」不变）。
            # 段放回**自己**的渲染段是关键：那一刻 `vs-cb1` 天然就是它自己的当帧对象变换，
            # 因此既不依赖帧内绘制顺序，也不需要跨对象空间的校准矩阵。旧做法把全部段塞进
            # 宿主段 ⇒ 跨组 cb1 在宿主段执行时可能还是上一帧的值 ⇒ 影子/镜像物体。
            # 历史路线（全部实测否决并移除）：F7/F4 在宿主渲染段用当帧 cb1 重跑校准 + 重
            # 蒙皮再写回顶点流——重算 CS 实际未执行，且把 mod 自建缓冲当 `vb0` 的四次尝试
            # 全部静默失败；F5 把 palette 捕获后移让 palette 与 cb1 同源——合并骨架缓冲
            # 宿主与跨组共用 ⇒ 整块骨架一起慢一帧，用户实测「无论哪边都在抖」，连原始
            # 物体都错。
            if self._merged_split_draws_target(draw_ib):
                # 拆分绘制继承原单次绘制前面那行 mesh 标注（拖拽交互靠它配对物体）。
                # 非宿主段统一改写成**合并物体**（宿主）的名字：合并后游戏里只有一个物体，
                # 各来源段必须共用同一个名字，否则拖拽节点会把它们当成三个不同物体。
                _mesh_comment = ""
                _own_group = self._merged_split_own_group(draw_ib)
                _split_plan = (
                    self._merged_split_group_plan().get(int(_own_group))
                    if _own_group is not None
                    else None
                )
                if _split_plan is not None and int(_split_plan[0]) != int(_own_group):
                    _mesh_comment = self._merged_split_mesh_comment(_split_plan[2])
                if not _mesh_comment:
                    for _drawindexed_line in M_IniHelper.get_drawindexed_str_list(
                        submesh_model.drawcall_model_list,
                        obj_name_draw_offset_dict=drawib_model.obj_name_draw_offset,
                    ):
                        if _drawindexed_line.strip().startswith(";"):
                            _mesh_comment = _drawindexed_line
                            break
                if not _mesh_comment:
                    # 兜底：着色器替换路径下注释由另一分支发出，这里按同一格式补一行，
                    # 否则物体名标注会整行缺失（拖拽交互与人工排查都依赖它）。
                    _dc = (
                        submesh_model.drawcall_model_list[0]
                        if submesh_model.drawcall_model_list
                        else None
                    )
                    if _dc is not None:
                        _mesh_comment = (
                            f"; [mesh:{getattr(_dc, 'obj_name', '') or ''}]"
                            f" [vertex_count:{getattr(_dc, 'vertex_count', 0)}]"
                        )
                if _mesh_comment:
                    texture_override_ib_section.append(_mesh_comment)
            split_draws_emitted = self._append_merged_render_split_draws(
                texture_override_ib_section, draw_ib
            )

            if is_cross_ib_source:
                non_cross_ib_drawcalls = []
                for drawcall_model in submesh_model.drawcall_model_list:
                    obj_name = drawcall_model.obj_name if hasattr(drawcall_model, "obj_name") else str(drawcall_model)
                    if obj_name not in self.cross_ib_object_names:
                        non_cross_ib_drawcalls.append(drawcall_model)

                print(f"[CrossIB ZZMI] 源块绘制非跨IB物体: {len(non_cross_ib_drawcalls)} 个")
                self._append_drawindexed_with_shader_replace(
                    texture_override_ib_section,
                    non_cross_ib_drawcalls,
                    drawib_model.obj_name_draw_offset,
                )
            else:
                print(f"[CrossIB ZZMI] 非源块绘制物体: {len(submesh_model.drawcall_model_list)} 个")
                if split_draws_emitted:
                    # 合并网格已按来源拆成多次 drawindexed（每次带自己的 vs-cb1）：
                    # 这里绝不能再发一次单次合并绘制，否则会被画两遍。
                    print("[CrossIB ZZMI] 合并网格已按来源拆分绘制，跳过单次合并绘制")
                elif redirect_carrier_info is not None:
                    # 合并网格重定向：drawindexed 带 base_vertex——从**本实例**的
                    # SO 中读本合并网格的区段（offset 保持本 submesh 的索引偏移）。
                    # 2026-09 多实例分离 v2（用户实测通过）：不再覆写 vb0、也不再包
                    # `if $zz_ms_redirect_drawn_<target> == 1` 帧闩锁——旧写法把两个
                    # 实例钉到同一个 SO 资源变量上，并让同一帧的后续实例等不到重放。
                    # 游戏渲染 draw 的 vb0 本来就是本实例自己的 deform SO（so0=vb0
                    # 指针族严格分实例），重放已把本实例的蒙皮结果写进去，因此这里
                    # 无条件发 drawindexed，每个实例各画一次。
                    base_vertex = redirect_carrier_info["base_vertex"]
                    # base_vertex 只给**真正承载合并几何**的那个子网格：载体的其它
                    # 子网格是 3 顶点占位（自身原顶点数不变），它们必须从 SO 第 0 行
                    # 读自己的前缀 stub；错加 base_vertex 会让占位小三角读到合并几何
                    # 的前几行，在场景里多画一个杂散三角（2026-09-16 实测：999bff94
                    # 的 `17946_0` 占位子网格被写成 `drawindexed = 3,0,3`）。
                    merged_submesh = True
                    original_vertices = int(
                        getattr(submesh_model, "original_vertex_count", 0) or 0
                    )
                    exported_vertices = int(
                        getattr(submesh_model, "vertex_count", 0) or 0
                    )
                    if original_vertices > 0 and exported_vertices > 0:
                        merged_submesh = exported_vertices > original_vertices
                    self._append_drawindexed_with_shader_replace(
                        texture_override_ib_section,
                        submesh_model.drawcall_model_list,
                        drawib_model.obj_name_draw_offset,
                        base_vertex=base_vertex if merged_submesh else 0,
                    )
                else:
                    self._append_drawindexed_with_shader_replace(
                        texture_override_ib_section,
                        submesh_model.drawcall_model_list,
                        drawib_model.obj_name_draw_offset,
                    )

            if is_cross_ib_target and source_ib_list_for_target:
                print(f"[CrossIB ZZMI] 目标块处理: source_ib_list={source_ib_list_for_target}")

                for source_ib_key in source_ib_list_for_target:
                    print(f"[CrossIB ZZMI] 查找源块: ib_key={source_ib_key}")
                    source_drawib_model, source_submesh, source_hash, source_first_index = self._find_source_submesh(
                        source_ib_key
                    )
                    target_method = self._get_mapping_method(source_ib_key, current_ib_key)

                    if source_submesh:
                        source_ib_resource_name = source_drawib_model.get_submesh_ib_resource_name(source_submesh)
                        self._append_target_cross_ib_draw(
                            texture_override_ib_section,
                            target_method,
                            source_hash,
                            source_first_index,
                            source_ib_resource_name,
                            draw_ib,
                            submesh_model.match_first_index,
                        )

                        cross_ib_drawcalls = []
                        for drawcall_model in source_submesh.drawcall_model_list:
                            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, "obj_name") else str(drawcall_model)
                            if obj_name in self.cross_ib_object_names:
                                cross_ib_drawcalls.append(drawcall_model)

                        print(f"[CrossIB ZZMI] 跨IB物体数量: {len(cross_ib_drawcalls)}")
                        if cross_ib_drawcalls:
                            self._append_drawindexed_with_shader_replace(
                                texture_override_ib_section,
                                cross_ib_drawcalls,
                                source_drawib_model.obj_name_draw_offset,
                            )

                        self._append_target_cross_ib_cleanup(
                            texture_override_ib_section,
                            target_method,
                            draw_ib,
                            submesh_model.match_first_index,
                        )
                    else:
                        print(f"[CrossIB ZZMI] 警告: 未找到源块 submesh for {source_ib_key}")

        ini_builder.append_section(texture_override_ib_section)

    def _warn_missing_drawib_parts(self):
        """检测 DrawIB 内缺失对象的部件（物体被合并/删除/改名导致）并大声报警。

        判定：DrawIBModel 元数据里的部件表（match_first_index_partname_dict）与本次导出
        实际拿到对象的子网格（submesh_model_list 的 match_first_index）比对。
        合并骨架模式下缺失部件应已由初始化阶段注入占位；这里仅用于发现
        占位注入之外的异常输入并提示用户，不负责用空 IB 静默隐藏部件。
        返回缺失清单 [{draw_ib, missing:[(first_index, part_name)], present:[...]}]。
        """
        missing_report = []
        for drawib_model in self.drawib_model_list:
            expected = getattr(drawib_model, "match_first_index_partname_dict", {}) or {}
            if not expected:
                continue
            present = set()
            for submesh_model in drawib_model.submesh_model_list:
                try:
                    present.add(int(submesh_model.match_first_index))
                except (TypeError, ValueError):
                    continue
            missing = []
            for first_index, part_name in sorted(expected.items(), key=lambda kv: int(kv[0])):
                if int(first_index) not in present:
                    missing.append((first_index, str(part_name)))
            if missing:
                missing_report.append({
                    "draw_ib": drawib_model.draw_ib,
                    "missing": missing,
                    "present_count": len(present),
                    "expected_count": len(expected),
                })

        for item in missing_report:
            missing_names = [name for _fi, name in item["missing"]]
            print(
                f"[ZZMI导出] !!! 部件缺失警告: DrawIB {item['draw_ib']} 有 "
                f"{item['expected_count']} 个部件，但只找到 {item['present_count']} 个的对象，"
                f"缺失: {missing_names}"
            )
            print(
                "[ZZMI导出] 合并骨架模式会为这些缺失部件注入极限小三角占位；"
                "若仍出现在此处，说明占位注入未生效，导出的 hash/IB 映射可能不完整。"
                "常见原因：对象被删除或改名，或工作区 DrawIB-Component/VGMap 缓存过期。"
            )
        return missing_report

    def export(self):
        try:
            self._export_impl()
        finally:
            self._cleanup_stub_objects()

    def export_buffers_only(self):
        """多轮导出的纯缓冲路径也必须闭合占位对象事务。"""
        try:
            return super().export_buffers_only()
        finally:
            self._cleanup_stub_objects()

    def _export_impl(self):
        # 骨骼引用缓存按本次导出的几何状态重算（导出流程内顶点组只读；缓存条目
        # 自身也带结构签名，见 `_collect_drawib_referenced_bone_ids`）。
        self._zzmi_drawib_bone_refs_cache = {}
        # ZZMI 骨骼合并（B1 契约接线）：组件扫描 + 契约判定必须**先于任何写盘**。
        # 下面的 generate_buffer_files 是本次导出的第一个落盘点（Meshes/*.buf），
        # 契约 error 时不得留下半成品 Mod 产物（.buf / .ini / res/*.hlsl）。
        # 扫描本身只读内存数据（子网格 json 写回的 vg_count/vg_map 等），不依赖
        # 已生成的缓冲，因此可以在最前面安全执行。
        self.merged_skeleton_components, self.merged_skeleton_component_id_dict = (
            self._collect_merged_skeleton_components()
        )
        self._enforce_merged_skeleton_contract()

        TimerUtils.start_stage("缓冲文件生成")
        # 后台导出的状态栏细分文本：基础导出（写 .buf 资源）是本次导出里最长的
        # 单段之一（实测约 17s），给一句更具体的名字。只改**显示**：report_stage
        # 不推进步骤计数；同步导出 / 轻量宿主没有 stage 文件时静默。
        try:
            from ...blueprint import export_progress

            export_progress.report_stage("基础导出：写入资源…")
        except Exception:
            pass
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        TimerUtils.end_stage("缓冲文件生成")

        if self.has_cross_ib:
            for node_name, cross_ib_method in self.cross_ib_method_dict.items():
                if cross_ib_method and cross_ib_method not in self.SUPPORTED_CROSS_IB_METHODS:
                    print(
                        f"[CrossIB] 错误: 节点 '{node_name}' 使用的跨 IB 方式 '{cross_ib_method}' 不适用于 ZZMI 模式"
                    )
                    print(
                        f"[CrossIB] ZZMI 模式只支持: {sorted(self.SUPPORTED_CROSS_IB_METHODS)}"
                    )
                    self.has_cross_ib = False
                    break

        print(f"[CrossIB ZZMI] export: has_cross_ib={self.has_cross_ib}")

        # ZZMI 骨骼合并：组件信息已在本次导出最前面收集（B1：契约判定必须在任何
        # 写盘之前完成），此处只落标志位；下面的重定向计划构建依赖已生成的缓冲
        # （载体的 Blend 重打包要读 category_buffer_dict），顺序不能提前。
        self.has_merged_skeleton = len(self.merged_skeleton_components) > 0
        if self.has_merged_skeleton:
            buffer_slots = max(
                c["vg_offset"] + c["vg_count"] for c in self.merged_skeleton_components
            )
            print(
                f"[ZZMI骨骼合并] 合并骨架: {len(self.merged_skeleton_components)} 个部件, "
                f"缓冲 {buffer_slots} 槽（max(vg_offset+vg_count)）"
            )
            # 跨组别引用守卫（开关关闭 = 无校准模式：引用其它组骨骼 = 运行时塌陷，大声报警；
            # 开关开启 = F1 写进宿主组缓冲 + F2 渲染 cb1 校准）
            self._warn_cross_group_bone_references()
            # 合并网格自动重定向计划先按现有实现计算；RedirectSO 这条路径在
            # 部分 ZZMIv1 帧序下会让完整合并物体整块消失，因此默认关闭。
            # 关闭时仍保留原始 DrawIB/Blend 布局和合并骨架，只走组内宿主直连重放。
            self._redirect_carrier_map, self._redirect_target_map, unredirected = (
                self._build_merged_mesh_redirect_plan()
            )
            if not _zzmi_prop_flag(
                "zzmi_merged_redirect_enabled", False
            ) and (self._redirect_carrier_map or self._redirect_target_map):
                self._redirect_carrier_map = {}
                self._redirect_target_map = {}
                print(
                    "[ZZMI骨骼合并] RedirectSO 自动重定向默认关闭；"
                    "保留单一合并对象，改走组内宿主直连重放。"
                    "如需复核跨 DrawIB 重定向，请打开实验开关。"
                )
            # 无法自动重定向的合并网格（缺反查缓存/跨 IB）大声报警
            self._warn_merged_mesh_timing(unredirected)

        # 部件缺失守卫：正常的合并骨架流程已在 ExportZZMI 初始化阶段为缺失部件
        # 注入极限小三角占位，因此这里仅报告仍未能匹配的异常输入；不会再主动
        # 生成 ib=null 来静默跳过对应物体。
        self._warn_missing_drawib_parts()

        TimerUtils.start_stage("INI配置生成")
        ini_builder = M_IniBuilder()
        drawib_drawibmodel_dict = {drawib_model.draw_ib: drawib_model for drawib_model in self.drawib_model_list}

        M_IniHelper.generate_hash_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelper.generate_shared_slot_style_texture_ini(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        self._integrate_object_swap_ini_hook(ini_builder)
        for drawib_model in self.drawib_model_list:
            # 只把这个 draw_ib 自己的 ini 段包进登记上下文：段名 / 参数都由该
            # draw_ib 派生，「条件判断」后处理按 draw_ib 反查时才能拿全。
            with _section_registry_context(drawib_model.draw_ib):
                self.add_unity_vs_texture_override_vlr_section(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_unity_vs_texture_override_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_unity_vs_texture_override_ib_sections(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_unity_vs_resource_vb_sections(ini_builder=ini_builder, drawib_model=drawib_model)
                self.add_resource_texture_sections(ini_builder=ini_builder, drawib_model=drawib_model)
            # 下面两步不是 ini 段构建（贴图搬移 / 全局 Mod 计数），留在上下文外。
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)
            GlobalKeyCountHelper.generated_mod_number = GlobalKeyCountHelper.generated_mod_number + 1

        M_IniHelper.add_branch_key_sections(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)
        # legacy / never-fires-on-ZZMI：经典（非直出）形态键发射器。ZZMI 的两条实际路径上
        # 它都不产出 —— ① 直出路线在 Meshes0000 基础轮次主动抑制形态键资源，② 标准路线的前
        # 处理已把键块烘焙掉（blueprint/preprocess.py::_apply_shape_keys）。4 份产物实测
        # `CustomShaderComputeShapes` 恒为 0，见 review-reports/t82-shapekey-drag-retest.md §2.6/§3.3 R-B。
        # 保留此调用而非删除：该发射器全仓共 9 处调用点，其中 8 处在 ZZMI 之外
        # （unity×2 / srmi / gimi / identityv / yysls / snowbreak / zzmidx12），
        # 且它现在是本轮 R-A「非直出不得静默丢弃」诊断的唯一落点。
        # 形态键导出请走直出：SSMTNode_PostProcess_ShapeKey.direct_export_mode（新节点默认勾选）。
        M_IniHelper.add_shapekey_ini_sections(ini_builder=ini_builder, drawib_drawibmodel_dict=drawib_drawibmodel_dict)
        M_IniHelperGUI.add_branch_mod_gui_section(ini_builder=ini_builder, key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict)

        if self.has_shader_replace:
            M_IniHelper.add_shader_replace_sections(
                ini_builder=ini_builder,
                shader_replace_info_list=self.shader_replace_info_list,
                shader_replace_object_names=self.shader_replace_object_names,
                draw_call_models=self.blueprint_model.ordered_draw_obj_data_model_list,
                mod_export_path=GlobalConfig.path_generate_mod_folder(),
                shader_replace_object_info_map=self.shader_replace_object_info_map,
                draw_call_offset_map=M_IniHelper.build_draw_call_offset_map(self.drawib_model_list),
                draw_call_base_vertex_map=self._build_shader_replace_base_vertex_map(),
            )

        if self.has_merged_skeleton:
            self.add_merged_skeleton_sections(ini_builder)
            self._copy_merged_skeleton_shader_to_mod()

        ini_builder.save_to_file(os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini"))
        TimerUtils.end_stage("INI配置生成")


ModModelZZMI = ExportZZMI
