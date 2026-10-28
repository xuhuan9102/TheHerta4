"""HIMI（崩坏3 / HI3）导出器：静态合并骨架的导出侧接线（T7 交付）。

职责边界（计划书 §6.3 / §7）：
* **数据层**（``common/himi_skeleton.py``，T6）负责反查、去重、落盘，**并持有 G1
  门控的唯一实现**（``prepare(target_bares=本次导出集合)``）；站位（stub）机制的
  **判据本体**也在那里（``decide_stub_injection``，T22）。
* **运行时计划**（``common/himi_merged_runtime.py``）只**消费**该门控结论，把落盘
  结果翻译成资源声明 + 可重绑的 deform pass 清单。
* **本模块**只做接线：导出前算计划并过契约；把 ``vs-t0`` 重绑与
  ``draw = <deform 顶点数>`` 注入 Blend 类别的 ``TextureOverride`` 段；
  追加角色级骨架资源声明；把骨架二进制发布到 mod 的缓冲目录；为**被吸收的缺席
  部件**注入 3 顶点占位对象以抑制原版 draw（计划书 §7.7）。

运行时**唯一**动作 = 该 deform pass 的 ``vs-t0`` 换成静态合并骨架
（``vb1`` 本来就是这个 mod 的 Blend，不变）。HIMI 不走 ZZMI 的 attach CS /
出现次槽位 / 每槽守卫 / 姿态指纹（计划书 D01/D10/N02/N07）。

站位机制（T22 / 计划书 §7.7）
-----------------------------
D09 的「整体不移植」已被重新裁定为错：站位承载两条**独立**作用，D09 的理由
（「合并不改几何拓扑」）只覆盖了作用 A（让缺席部件的 deform pass 跑起来填骨架，
HIMI 静态骨架 ⇒ 无对应），**完全没有覆盖作用 B（抑制原版 draw 防重影）**。关键一跳：
HIMI 的 IB override 段只为「场景里存在对象的部件」生成（``ui/universal/unity.py:75-125``）
⇒ 部件缺席 ⇒ mod 里没有它的 IB 段 ⇒ ``handling = skip`` 不生效 ⇒ 原版那一笔照跑。

**方向与 ZZMI 相反（保守）**：HIMI 的站位不承载作用 A ⇒ 插桩的唯一效果就是压掉
原版 draw ⇒ 宽松口径会「静默让用户原版部件消失」。少压 = 可见重影（用户立刻发现），
多压 = 静默部件消失（用户以为提取失败）⇒ 退化情形一律**不插桩**。
EFMI 侧 t37「宁松勿漏」裁决的前提是作用 A，**在 HIMI 不成立、不得照搬**。

非合并路径**逐字节不变**：所有新增行都在「该部件带合并数据 且 通过 G1」时才出现，
否则逐字走基类实现（``ui/universal/unity.py``）——由
``tests/test_himi_merged_runtime.py::HIMIExporterWiringTests::test_vb_section_without_plan_matches_base``
逐行锁死。

**t107/t108 修正：站位不再整段跳过（普通模式三条规则照跑）。** 原先
``import_merged_vgmap()`` 关闭时 ``_himi_inject_stub_objects`` 直接 ``return []``，
把「要不要产出合并骨架资源」和「该压掉的原版 draw」绑成了同一根开关。这两件事正交：
站位的全部正当性都是「用 ``handling = skip`` 压掉原版那一笔」，而三条规则的证据来源
（工作空间 VGMap + 场景顶点组）没有一条依赖合并骨架资源。绑在一起的后果是用户实测的
两种形态：① 同一 IB 的第二笔原版 draw（``4386@57792``）没有任何段接住 ⇒ 原版几何与
mod 几何同时出现 ⇒ 两份重叠、其中一份没有贴图、顶点槽错位串到别的物体；② 整族被并入
另一个 IB（``1b62725d`` 并进头发）后，那个 IB 的原版 draw 同样没有任何段接住。
``_himi_add_missing_writer_stubs``（t106）仍留在开关后面 —— 它按 palette 槽位记账，
确实只在合并骨架运行时成立。

注入顺序（T26 F1）
------------------
站位注入发生在 ``__init__`` 里、**基类组装模型之前**（形状对齐
``ui/universal/zzmi.py:443`` 注入 → ``:445`` ``super()``）。这不是风格问题：
``ui/universal/drawib_export_base.py:11-16`` 在它自己的 ``__init__`` 里**立即**把
``ordered_draw_obj_data_model_list`` 解析成 ``drawib_model_list``；注入晚一步，站位
DrawCall 就只留在 ``ordered`` 里而**永远进不了**
``drawib_model_list[*].submesh_model_list[*].drawcall_model_list`` ⇒ 站位部件的 IB
override 段（``handling = skip``）不生成 ⇒ 抑制重影失效。
``tests/test_himi_stub_objects.py::StubInjectionOrderTests`` 是这条的回归钉子
（实测：改回旧顺序该用例失败、恢复后通过）。

顺序修正带来一个**必须同时处理**的副作用：站位件现在真的会出现在
``drawib_model_list`` 里，而 ``SubmeshModel.vg_count`` 是按 **unique_str** 从工作空间
json 解析的（``common/submesh_metadata.py:178``）—— 缺席部件的 json 往往仍然存在
（它是被 join 走的，不是没提取过）⇒ 它的 ``vg_count`` 可能 > 0。

**口径（已反转）：站位 bare 进 `export_bares`。** 「排除站位」看似更干净，但它
**必然中止导出** —— 判据只在「缺席部件**有** `VGMap` 且专属槽位被引用」时插桩
⇒ 能插桩的缺席部件**必然已落盘**；而 G1 家族门是 `L(ib) ⊆ T`、`L(ib)` 取
**已落盘**组件 ⇒ 排除站位会让被吸收成员永远 `missing`、家族永远不 `safe`、契约
`level=error` ⇒ `Fatal` 中止 —— 而那个场景**正是**站位要解决的场景。ZZMI 同语义
（站位 DrawCall 进 `ordered`，`ui/universal/zzmi.py:1008-1013`）。

⇒ ``is_stub`` 只作**只读标注**（``himi_stub_bares()`` 供诊断/上层查询），
**不参与过滤**；过滤口径只有一个事实源：``vg_count > 0``。回归钉子
``StubInjectionOrderTests::test_stub_bare_enters_export_set_to_close_the_family_gate``
（反证链的独立复核见 ``reports/himi-merge/18-stub-rereview.md`` §4.2；规格裁定见
计划书 §7.7.11 与 ``reports/himi-merge/05-decisions.md`` §3.20）。
"""

import math
import os
import re
import sys

from ...common import himi_merged_runtime as himi_runtime
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...common.himi_merged_contract import (
    DEFORM_GATE_REJECT_PREFIX,
    VERTEX_CALIBRATION_KEY,
    VERTEX_CALIBRATION_NONE,
    attach_placement_identity,
    evaluate_merged_skeleton_contract,
    shapekey_direct_mode_problem,
)
from ...utils.json_utils import JsonUtils
from ...common.himi_skeleton import (
    HIMI_COMPONENT_MAP_BASENAME,
    HIMI_MERGED_SKELETON_METADATA_FILE,
    HIMI_STUB_MARKER,
    HIMIStubDrawCall,
    HIMI_STUB_TRIANGLE_VERTICES,
    bare_from_unique_str,
    blender_collect_used_group_ids,
    blender_create_stub_object,
    blender_object_present,
    blender_purge_stale_stub_objects,
    blender_remove_stub_object_data,
    blender_remove_stub_object_data_by_name,
    decide_stub_injection,
    load_component_family_map,
)
from ...common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType
from .unity import ExportUnity

_LOG_PREFIX = "[HIMI骨骼合并]"
#: 站位诊断前缀（与合并骨架诊断分开，便于用户区分两件事）
_STUB_LOG_PREFIX = "[HIMI站位]"
#: 本环境无法取证的清单（计划书 §11 V1/V2/V3 + §7.4 R3）；导出时打印，绝不静默。
UNVERIFIED_IN_GAME = himi_runtime.UNVERIFIED_IN_GAME


def _himi_buffer_byte_size(payload) -> int:
    """缓冲载荷的**字节数**（``bytes``/``bytearray`` 直接量长；numpy 数组用 ``nbytes``）。

    ``category_buffer_dict`` 的值既可能是 bytes，也可能是 numpy 视图（基类在别处用
    ``payload.tobytes()``）⇒ 宿主族判定必须两条都吃，否则会把「数组」误判成 0 字节。
    """
    if payload is None:
        return 0
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)
    nbytes = getattr(payload, "nbytes", None)
    if nbytes is not None:
        try:
            return int(nbytes)
        except (TypeError, ValueError):
            return 0
    try:
        return len(payload)
    except TypeError:
        return 0


# --------------------------------------------------------------------------- #
# t75：**单个**合并 attach CS（5 个族共用一份）+ `cs =` 引用硬门
# --------------------------------------------------------------------------- #
#
# 口径（用户 2026-09-25 明确："这个着色器需要合并成一个"）：
#
#   * `<mod>/res/` 下**只有一个** `himi_attach.hlsl`，ini 里 5 条 `cs =` **指向同一路径**；
#   * 因此「本族在合并骨架里的目标基址」**不能再逐族写进 #define** —— 必须在**同一个** shader
#     内部推出来。做法：读**源 palette 资源自身**的槽位数，在表里查出目标基址（一族一行，
#     注释里标该族自己的族 ID）。
#   * 槽位数一律走 `GetDimensions`（运行期真值），**绝不读 `IniParams`**：
#     ini 参数纹理布局随 3DMigoto fork 变化（标准版 `x1/y1` 落 `IniParams[0]`，
#     Armor fork 从 `[1]` 起），读错位置只会读到未设置的 0 ⇒ CS 静默不写 ⇒ 骨架留空
#     ⇒ 顶点位置全零、模型爆炸且**不报任何错**（本仓 key 记忆 2026-09-22 实测形态）。
#   * `HIMI_MERGED_SKELETON_SLOTS` 是**该 ini 这一版合并骨架的总槽位数**，由导出侧写进
#     文件；它只用于「写入越界保护」的上界，**不参与**基址推导（基址只由源槽位数决定）。
#
# 基址表的**来源**只有一个：本次 `plan.family_ranges`。每族在合并骨架里的 (span, base) 由导出侧
# 排布决定，表由 `merged_attach_base_table_lines()` 按它生成后再渲染进 CS —— 表与 ini 的
# `array=` / `Dispatch` 因此**同源**。旧口径（表是参考角色那一份的冻结常量）会与本族自己打架：
# 本族 span 恰好等于参考角色某族、基址却不同（实测 `077983c9`：span 51、base 0，参考表里 51→292）
# ⇒ 运行期 `base_slot != identity_base` 整族拒写，导出期 F6 硬门 Fatal。表不能有第二个来源。
# 运行期查不到（palette 维度不在表里）时 `base_slot` 停在 0xFFFFFFFF，越界保护挡掉全部写入 ——
# **宁可一根骨都不写，也不写错位置**。

#: 合并 attach CS 的文件名（**一个**，不是每族一个）
MERGED_ATTACH_HLSL_BASENAME = "himi_attach.hlsl"
#: ini 里引用的相对路径（从 ini 自身所在目录 = mod 根算起）
MERGED_ATTACH_HLSL_PATH = "./res/" + MERGED_ATTACH_HLSL_BASENAME
#: 【t94】渲染段自蒙皮 CS：与 attach 同目录（`Toolset/`），一个文件全部族共用。
MERGED_SKIN_HLSL_BASENAME = himi_runtime.MERGED_SKIN_CS_BASENAME
MERGED_SKIN_HLSL_PATH = himi_runtime.MERGED_SKIN_CS_PATH
#: 路线开关：关掉 = 不发射蒙皮段/声明、不换 `vb0`（回到"只有 attach"的旧产物）。
#:
#: 【t118】关掉：这条路线自 t100 起没有任何消费者（IB 段不再插 `run = …Skin_*` /
#: `vb0 = ResourceHI3SkinOut_*`），但段里那句 `cs = ./res/himi_merged_skin.hlsl` 仍在
#: 发射，而生成器从不写这个文件 ⇒ `verify_cs_references` 硬门每次导出都报「引用的文件
#: 不存在」，进而把整条合并骨架判成 error、打印「已降级为普通导出」。关掉它既去掉假
#: 失败，也去掉每帧 reload 的 `Shader not found` 噪声。要复活蒙皮路线必须连
#: `_himi_write_skin_cs` 的落盘一起接回，单开一个常量不够。
MERGED_SKIN_ROUTE = False
#: HLSL 里的槽位/基址常量名（测试据它核对，不与 `common/himi_merged_runtime.py` 的
#: 逐族 `HIMI_ATTACH_BASE` 接口混用）
MERGED_ATTACH_SLOTS_MACRO = "HIMI_MERGED_SKELETON_SLOTS"
#: 基址表行里 `//` 落在第几列（不足则至少留 3 个空格）—— 纯排版，不参与任何判据。
MERGED_ATTACH_BASE_TABLE_COMMENT_COLUMN = 58
#: 基址表的**参考角色样例**（`role_0512e26f` 的 343 槽真实排布）。
#:
#: 它**不是**生产路径的来源：真导出时表一律由 :func:`merged_attach_base_table_lines`
#: 按本次 ``plan.family_ranges`` 生成（唯一权威来源）。这五族留在这里，是为了给测试与
#: 「没有 plan 的参考渲染」一份可核对的样本 —— 换角色不需要改这里，也不需要改代码。
MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES = {
    "2c706fbd": {"base": 0, "slots": 184, "own_block": True},
    "694025c6": {"base": 184, "slots": 18, "own_block": True},
    "70a4da3e": {"base": 202, "slots": 71, "own_block": True},
    "b35cfc6c": {"base": 273, "slots": 19, "own_block": True},
    "def0c0ae": {"base": 292, "slots": 51, "own_block": True},
}


def merged_attach_base_table_lines(ranges) -> list:
    """按本次 ``plan.family_ranges`` 生成基址表行（CS 里那张表**唯一**的来源）。

    为什么必须是生成而不是常量：每族的 ``(span, base)`` 只由导出侧排布决定，而 ini 的
    ``array=`` / ``Dispatch`` 也来自同一份排布。表若是别处抄来的冻结副本，就会在「本族
    span 恰好等于参考角色某族、基址却不同」时自相矛盾 —— 运行期 ``base_slot != identity_base``
    整族拒写，导出期 F6 硬门 Fatal（实测角色 ``077983c9``：span 51 / base 0，
    参考表里 51→292）。

    行形状（与解析侧 `_BASE_TABLE_ROW_RE` 严格配对）::

        if (source_slots == <本族源槽位数>u) { base_slot = <本族合并基址>u; }   // <族 ID>

    * 第一行用 ``if``、其余用 ``else if``；
    * 按 ``base`` 升序排（与槽位排布同序，人读友好；运行期查表的语义与顺序无关）；
    * 注释里标**该族自己的族 ID**（不是参考角色的）；
    * 借位族（``own_block`` 为假）与 span ≤ 0 的族**不进表**（它们没有自己的槽块可写）。
    """
    wanted: list = []
    for family, info in dict(ranges or {}).items():
        info = dict(info or {})
        if not bool(info.get("own_block", False)):
            continue
        try:
            span = int(info.get("slots", 0) or 0)
            base = int(info.get("base", 0) or 0)
        except (TypeError, ValueError):
            continue
        if span <= 0 or base < 0:
            continue
        wanted.append((base, span, str(family)))
    wanted.sort()
    lines: list = []
    for index, (base, span, family) in enumerate(wanted):
        head = "if" if index == 0 else "else if"
        row = f"    {head} (source_slots == {span}u) {{ base_slot = {base}u; }}"
        pad = " " * max(3, int(MERGED_ATTACH_BASE_TABLE_COMMENT_COLUMN) - len(row))
        lines.append(f"{row}{pad}// {family}")
    return lines


def _hlsl_u32_table_rows(values, per_line: int = 8) -> list:
    """把一串 u32 渲染成 HLSL 静态数组的字面量行（哨兵保留十六进制写法，便于肉眼核对）。"""
    cells: list = []
    for value in values:
        number = int(value)
        cells.append("0xFFFFFFFFu" if number == 0xFFFFFFFF else f"{number}u")
    rows: list = []
    step = max(1, int(per_line))
    for start in range(0, len(cells), step):
        rows.append("    " + ", ".join(cells[start:start + step]) + ",")
    return rows


#: 上表渲染出来的样子（**由生成器算出来**，不手写 —— 样本与生成器不可能漂移）。
MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES = tuple(
    merged_attach_base_table_lines(MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES)
)
#: 【t74】运行期 X 的文本探针：`(HLSL 片段, 缺它时的报错)`。
#: 缺失任意一条都必须让 `merged_attach_hlsl_problems` 报错（测试用删行变异逐条证红）——
#: 这几行合起来才是「X 由运行期 cb3 算」这件事本身：少 cbuffer 就取不到本族物体矩阵，
#: 少 u1 表就没有宿主族 M_base 可读，少选择分支就退化成抓帧常量。
HIMI_RUNTIME_X_PROBES = (
    (
        "cbuffer himi_object_cb : register(b1)",
        "【t74】缺 cbuffer himi_object_cb : register(b1)（cs-cb1 = 本族 vs-cb3 的副本）",
    ),
    (
        "float4 himi_obj_rows[8];",
        "【t74】cbuffer 必须按 8 行 float4 读 vs-cb3（行 0..3 = 物体矩阵，行 4..7 = 其逆）",
    ),
    (
        "RWBuffer<float4> himi_object_matrix : register(u1);",
        "【t74】缺物体矩阵表声明（RWBuffer<float4> himi_object_matrix : register(u1);）——"
        "没有它宿主族读不到 M_base，X 只能取抓帧常量",
    ),
    (
        "HimiAffine himi_cb3_affine()",
        "【t74】缺「把本族 vs-cb3 行 0..3 收成 Affine」的函数定义（HimiAffine himi_cb3_affine()）",
    ),
    (
        "himi_cb3_affine();",
        "【t74】缺「把本族 vs-cb3 行 0..3 收成 Affine」的读取（himi_cb3_affine()）",
    ),
    (
        "HimiAffine himi_cb3_inverse_affine()",
        "【t74】缺 vs-cb3 行 4..7 的函数定义（HimiAffine himi_cb3_inverse_affine()）——"
        "校验物体矩阵必须核对自洽性，否则会把错口径的行当成物体矩阵",
    ),
    (
        "himi_cb3_inverse_affine()",
        "【t74】缺 vs-cb3 行 4..7 的读取（himi_cb3_inverse_affine()）——"
        "校验物体矩阵必须核对自洽性，否则会把错口径的行当成物体矩阵",
    ),
    (
        "if (asuint(attach_range.z) >= 4u)",
        "【t74】缺版本门（if (asuint(attach_range.z) >= 4u)）——旧产物（v3）必须逐字节走老路径",
    ),
    (
        "bool himi_object_matrix_valid()",
        "【t74】缺物体矩阵校验（himi_object_matrix_valid()：齐次 w、|det|、行 4..7 与行 0..3 互逆）",
    ),
    (
        "himi_affine_inverse(m_base)",
        "【t74】缺 M_base⁻¹（himi_affine_inverse(m_base)）——没有逆就只能按宿主族摆放",
    ),
    (
        "himi_affine_mul(himi_affine_inverse(m_base), own_object)",
        "【t74】缺 X = M_base⁻¹·M_own 的合成（himi_affine_mul(himi_affine_inverse(m_base), own_object)）",
    ),
    (
        "uint own_index = asuint(himi_attach_range[7].x);",
        "【t74】缺本族索引（uint own_index = asuint(himi_attach_range[7].x);）——"
        "载荷 [7].x 是导出期写死的族序号",
    ),
    (
        "uint base_index = asuint(himi_attach_range[7].y);",
        "【t74】缺宿主族索引（uint base_index = asuint(himi_attach_range[7].y);）",
    ),
    (
        "if (own_index == base_index)",
        "【t74】缺宿主族分支（if (own_index == base_index) ⇒ X = 单位阵）",
    ),
    (
        "himi_object_matrix[own_row + 0u] = own_object.r0;",
        "【t74】缺「把本族校验通过的物体矩阵写进 u1 表」——"
        "不写的话宿主族永远读不到任何 M_base，X 无法实时算出",
    ),
    (
        "float4 base0 = himi_object_matrix[base_row + 0u];",
        "【t74】缺宿主族行的读回（float4 base0/1/2 = himi_object_matrix[base_row + 0/1/2u]）",
    ),
    (
        "float4 own0 = himi_object_matrix[own_row + 0u];",
        "【t74】缺「采集无效时读表里本族那段」的回退（float4 own0/1/2 = himi_object_matrix[own_row + 0/1/2u]）——"
        "deform 段那 368 B 的 cb3 不是物体矩阵，这时只能退回上一帧 render 写下的好值",
    ),
    (
        "else if (object_in_range)",
        "【t74】缺「采集无效时的回退分支」（else if (object_in_range)）——"
        "采集失败就直接用烘死 X，等于运行期 X 从未生效",
    ),
    (
        "if (own_ready && base_ready)",
        "【t74】回退分支必须要求表里 own/base 两段都非全零（if (own_ready && base_ready)）——"
        "任一段空就乘进去会得到垃圾 X",
    ),
)
#: `cs =` 行的解析（3DMigoto 的 ini 键名大小写不敏感，故用 IGNORECASE）。
#: 值允许为空（``cs =``）—— 那是一条**坏的**引用，必须被抓到而不是被跳过，
#: 所以 `(\S*)` 的星号不能少。
_CS_REFERENCE_RE = re.compile(r"^\s*cs\s*=\s*(\S*)", re.IGNORECASE)


def merged_attach_hlsl_text(
    decl=None,
    base_table_lines=None,
    ranges=None,
    borrow_rows=None,
    borrow_pairs=None,
) -> str:
    """渲染**单个**合并 attach CS（5 个族共用）。

    ``decl`` 只用于签出该 ini 这一版合并骨架的总槽位数（写越界保护的上界）；缺省取
    参考声明。内容与「每族一个 CS + ``#define HIMI_ATTACH_BASE``」的旧口径**不兼容**：
    这里没有基址宏、只有运行期查表（见文件头那段注释）。

    基址表那几行由 ``base_table_lines`` / ``ranges`` 决定（**唯一权威来源是 plan**）：

    * 给了 ``base_table_lines`` ⇒ **逐字**用它（离线工具与「陈旧表」两态用它造反例）；
    * 否则给了 ``ranges``（= 本次 ``plan.family_ranges``）⇒ 按每族 span/base 生成
      （:func:`merged_attach_base_table_lines`）；
    * 两者都没有 ⇒ 用参考角色样例（``MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES``）：
      只有测试与「没有 plan 的参考渲染」走这条路，真导出永远传 ``ranges``。

    借用槽（【t91】）的两个量同样是**可选**参数，缺省保持「参考渲染」口径：

    * ``borrow_rows``：本族借用表的最大行数（真导出按 ``ranges`` 现算）。缺省用
      :func:`common.himi_merged_runtime.borrow_map_rows_by_family` 对同一份 ``ranges``
      求最大值再兜到 1。运行期**不许**改成读 ``GetDimensions``：typed 视图的分量数与
      ``uint4`` 元素不一致时会落进跨显卡未定义区（实机只回 1/4 的行数）。
    * ``borrow_pairs``：``{(本族序号, 来源族 rank): (本族局部槽, 来源族局部槽)}`` ——
      同一根物理骨在两支 palette 里的槽对。缺省全 ``0xFFFFFFFF``（= 没有骨对，借用槽
      那时先原样写 P_源）。表里**只有索引**，矩阵一律每帧从活 palette 现算。
    """
    if decl is None:
        decl = himi_runtime.build_merged_skeleton_decl(
            himi_runtime.REFERENCE_ROLE_KEY, himi_runtime.REFERENCE_SLOT_COUNT
        )
    table_lines = [str(line) for line in base_table_lines] if base_table_lines else None
    if table_lines is None:
        table_lines = merged_attach_base_table_lines(
            MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES if ranges is None else ranges
        )
    # 借用槽的来源族（按块基址升序 = 借用表第 2 分量的编号空间）。没有 plan 的参考渲染
    # 用同一份样例区间，保证「参考文本」与真产物同形（否则判据只能覆盖真导出这一条路）。
    palette_families = himi_runtime.borrow_palette_families(
        MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES if ranges is None else ranges
    )
    # 【t91 借用槽】行数：真导出由调用方按 ranges 现算后传进来；缺省路径（测试 / 参考渲染）
    # 走同一份 ranges，保证「参考文本」与真产物同形 —— 判据只有一条路可走就形同虚设。
    if borrow_rows is None:
        rows_by_family = himi_runtime.borrow_map_rows_by_family(
            MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES if ranges is None else ranges
        )
        borrow_rows = max([len(rows) for rows in rows_by_family.values()] or [0])
    borrow_rows = max(1, int(borrow_rows or 0))
    # 【t91 借用槽】骨对表：下标 = 本族序号 * HIMI_BORROW_RANK_COUNT + 来源族 rank。
    # 族数与 rank 空间同源（``borrow_palette_families`` 的次序），本族序号那一路取法见
    # ``_himi_family_index_map``（与载荷 [7].x 同一套）。族数为 0 也留一格合法声明。
    borrow_rank_count = max(1, len(palette_families))
    pair_lookup = dict(borrow_pairs or {})
    pair_own_values: list = []
    pair_src_values: list = []
    for pair_own_index in range(borrow_rank_count):
        for pair_rank in range(borrow_rank_count):
            pair = pair_lookup.get((pair_own_index, pair_rank))
            own_slot = src_slot = himi_runtime.BORROW_MAP_EMPTY_SENTINEL
            if pair is not None and len(pair) >= 2:
                try:
                    own_slot = int(pair[0])
                    src_slot = int(pair[1])
                except (TypeError, ValueError):
                    own_slot = src_slot = himi_runtime.BORROW_MAP_EMPTY_SENTINEL
            if own_slot < 0 or src_slot < 0:
                own_slot = src_slot = himi_runtime.BORROW_MAP_EMPTY_SENTINEL
            pair_own_values.append(own_slot)
            pair_src_values.append(src_slot)
    slots = int(getattr(decl, "slots", 0) or 0)
    skeleton_name = str(getattr(decl, "name", "") or "")
    lines: list = []
    lines.append("// himi_attach.hlsl —— HIMI（崩坏3 / HI3）合并骨架 attach CS（**单文件，5 族共用**）")
    lines.append("// 由 ui/universal/himi.py::merged_attach_hlsl_text() 生成；请勿手改。")
    lines.append("//")
    lines.append("// 【为什么只有一个文件】用户口径：这份着色器**合并成一个**。5 个 deform 族段")
    lines.append("// （[CustomShaderHI3MergedSkeletonAttach_<fam>]）的 `cs =` 全部指向本文件，")
    lines.append("// 每个段只是把**自己那一族的当帧 palette 副本**绑到 cs-t0、把合并骨架绑到 cs-u0。")
    lines.append("//")
    lines.append("// 【为什么没有 #define HIMI_ATTACH_BASE】旧口径每族一个文件、基址写死在各文件里。")
    lines.append("// 合并成一个文件后，基址必须由**运行期真值**推出来：读源 palette 自身的槽位数，")
    lines.append("// 查下面那张表得到该族在合并骨架里的起始槽（一族一行）。")
    lines.append("// 表由导出侧按**本次** `plan.family_ranges` 生成（与 ini 的 array/Dispatch 同源）：")
    lines.append("// 每一行 = 「本族 palette 的槽位数 → 本族在合并骨架里的起始槽」。源槽数不在表里")
    lines.append("// （palette 池换了尺寸）时 base_slot 保持 0xFFFFFFFF，越界保护会把本 dispatch 的")
    lines.append("// 所有写入挡掉 —— **宁可一根骨都不写，也绝不写进别的族的槽位**。")
    lines.append("//")
    lines.append("// 【机制链（逐帧，顺序不可换）】")
    lines.append("//   ① deform VB 段先捕获**当帧** palette：")
    lines.append("//        ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null      ← LHS 是**目标**")
    lines.append("//   ② run = CustomShaderHI3MergedSkeletonAttach_<fam> → 本 CS 把该副本搬到合并骨架的")
    lines.append("//        [base_slot, base_slot + 源槽位数) 区间；")
    lines.append("//   ③ 之后才把 vs-t0 换绑成合并骨架（供游戏 deform VS 读取），最后 handling = skip + draw。")
    lines.append("//   顺序由导出侧护栏（ui/universal/himi.py::blend_block_order_problems）在产物落盘前拦死。")
    lines.append("//")
    lines.append("// 【跨显卡不变量（三条必须同时满足，2026-09-22 ZZMI 实机验证）】")
    lines.append("//  A1 本 palette 必须声明为 Buffer<float4>（typed 16 B 元素），**不是** StructuredBuffer<48B>：")
    lines.append("//     palette 是 copy vs-t0 的目标，加载器会把源视图的 Format 注入进来（SRV 实际是 typed")
    lines.append("//     16 B）。按 48 B 结构化声明会落进 D3D11 未定义区：AMD 按描述符取址 → 骨架 2/3 错位 →")
    lines.append("//     模型爆炸。所以这里手工按 bone*3 取三个 float4，不依赖任何 stride 解释。")
    lines.append("//     ini 侧该副本必须是 type = Buffer / stride = 48 / **绝不允许** format。")
    lines.append("//  A2 合并骨架必须声明为 RWBuffer<float4>（typed 16 B），按 slot*3 写。按结构化（SBS=48）")
    lines.append("//     声明会让 AMD 按 48 取址 → 骨号 b 实际读到槽 3b → 越界读返回精确 0。")
    lines.append("//     ini 侧：type = RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数")
    lines.append("//     + bind_flags = shader_resource unordered_access（缺 bind_flags ⇒ SRV 创建静默失败）。")
    lines.append("//  A3 槽数一律取**资源自身维度**（GetDimensions 单参后 /3u 还原），槽位下标一律 *3u，")
    lines.append("//     **绝不读 IniParams**：ini 参数纹理布局随 3DMigoto fork 变化（标准版 x1/y1 在")
    lines.append("//     IniParams[0]，Armor fork 从 [1] 起），读错位置只会读到未设置的 0 ⇒ CS 静默不写 ⇒")
    lines.append("//     骨架留空 ⇒ 顶点位置全零，且**不报任何错**。")
    lines.append("//")
    lines.append("// 【本环境未取证的项（不得当成已成立）】")
    for item in himi_runtime.UNVERIFIED_IN_GAME:
        lines.append(f"//  - {item}")
    lines.append("//")
    lines.append("// ---- 机器可读声明行（测试据它与 ini 逐字段比对）----")
    lines.append("// " + decl.machine_line())
    lines.append("//")
    lines.append("// ---- 资源声明（必须与 ini 的 [{0}] 段逐字段一致）----".format(skeleton_name))
    lines.append(f"#define {MERGED_ATTACH_SLOTS_MACRO} {slots}u")
    lines.append(f"#define HIMI_MERGED_SKELETON_STRIDE {himi_runtime.MERGED_SKELETON_STRIDE}u")
    lines.append(
        "// 身份魔数（身份资源是否可用：不可用则退回冻结表 = 今天的行为）"
    )
    lines.append(
        f"#define {himi_runtime.ATTACH_RANGE_MAGIC_MACRO} {himi_runtime.ATTACH_RANGE_MAGIC}u"
    )
    lines.append("")
    lines.append("// ---- 【t91 借用槽】行数 + 骨对表（只有**索引**，矩阵一律每帧从活 palette 现算）----")
    lines.append("// 行数由导出期按本次 ranges 算出（= 本族借用表的最大行数）。运行期**不信**")
    lines.append("// 运行期读维度：typed 视图的分量数与 uint4 元素不一致时会落进")
    lines.append("// 跨显卡未定义区（实机只回 1/4 的行数）⇒ 表里后半段那几十行永远不执行。")
    lines.append(f"#define HIMI_BORROW_ROWS {borrow_rows}u")
    lines.append(f"#define HIMI_BORROW_RANK_COUNT {borrow_rank_count}u")
    lines.append(f"#define HIMI_BORROW_PAIR_COUNT {len(pair_own_values)}u")
    lines.append("// 骨对表：下标 = 本族序号 * HIMI_BORROW_RANK_COUNT + 来源族 rank；值 = 同一根物理骨")
    lines.append("// 在两支 palette 里的局部槽（本族 / 来源族）。0xFFFFFFFF = 这对不用（该族没有借用")
    lines.append("// 行，或导出期解不出）⇒ 该槽的 X 退回单位阵（本帧原样写 P_源，少一层校正）。")
    lines.append(f"static const uint HIMI_BORROW_PAIR_OWN[{len(pair_own_values)}] = {{")
    lines.extend(_hlsl_u32_table_rows(pair_own_values))
    lines.append("};")
    lines.append(f"static const uint HIMI_BORROW_PAIR_SRC[{len(pair_src_values)}] = {{")
    lines.extend(_hlsl_u32_table_rows(pair_src_values))
    lines.append("};")
    lines.append("")
    lines.append("// cs-t0 = ref ResourceHI3PaletteFam_<fam>（ini: Buffer / stride = 48 / 无 format）")
    lines.append("Buffer<float4> himi_src_palette : register(t0);")
    lines.append(
        "// cs-t1 = ref ResourceHI3AttachRange_<fam>"
        "（ini: Buffer + format = R32G32B32A32_FLOAT + filename；16 字节 = 本族 base/span）"
    )
    lines.append("Buffer<float4> himi_attach_range : register(t1);")
    lines.append("")
    lines.append("// ---- 【t91 借用槽】来源族的 palette 副本 ----")
    lines.append("// 块尾那几根骨是**别的族**引用过来的：它们的值必须是 X_源 · P_源，而 P_源 只在**来源族**")
    lines.append("// 手里 ⇒ 借用方自己把那一份捕获下来（ini: 该族的 VB/IB 段里 `ResourceHI3PaletteFam_<fam>")
    lines.append("// = copy vs-t0 unless_null`，再由本段 `cs-t2/t3/t5/…` 引进来）。")
    lines.append("// 绑定顺序 = 来源族序号（块基址升序）：0 → t2、1 → t3、2 → t5 …（t4 留给 BorrowMap；")
    lines.append("// t0/t1 是本族 palette 与身份资源，永不挪作它用）。")
    for rank, family in enumerate(palette_families):
        lines.append(
            f"// cs-{himi_runtime.borrow_palette_register(rank)} = ref "
            f"ResourceHI3PaletteFam_{family}（来源族 #{rank}）"
        )
        lines.append(
            f"Buffer<float4> {himi_runtime.borrow_palette_hlsl_name(rank)} : "
            f"register({himi_runtime.borrow_palette_register(rank)});"
        )
    lines.append("// ---- 【t91 借用槽】本族的借用表 ----")
    lines.append("// cs-t4 = ref ResourceHI3BorrowMap_<fam>（ini: Buffer + format = R32G32B32A32_UINT +")
    lines.append("//   stride = 16 + array = 行数 + filename = <缓冲目录>/<fam>-BorrowMap.buf）")
    lines.append("// 格式必须是**4 分量** typed：与 Buffer<uint4> 的元素一一对应。写成 R32_UINT")
    lines.append("// （1 分量视图 + 4 分量元素）会落进跨显卡未定义区 —— 实机就是这么丢了一半借用槽。")
    lines.append("// 每行 = (目标槽, 来源族序号, 源局部槽, 0)；行号 = bone_index - 源槽数；")
    lines.append("// 目标槽 = 0xFFFFFFFF ⇒ 本线程不写（空表就是一行这个哨兵）。")
    lines.append("Buffer<uint4> himi_borrow_map : register(t4);")
    lines.append("// cs-u0 = ref " + skeleton_name)
    lines.append("// （ini: RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 槽位数 + bind_flags）")
    lines.append(decl.hlsl_declaration())
    lines.append("")
    lines.append("// ---- 【t74】族级物体矩阵 X_f 的运行期来源（不再只靠抓帧烘死的常量）----")
    lines.append("// cs-cb1 = ref ResourceHI3Cb3_<fam>（ini: **只有段名一行**，不写 type/stride ——")
    lines.append("//   尺寸与布局由 copy vs-cb3 的源视图决定；同一先例见本仓 ZZMI 的 copy vs-cb1）")
    lines.append("// 该族那一笔 deform draw 的 vs-cb3 前 8 行 = 它的物体矩阵：")
    lines.append("//   行 0..2 = 3×3 线性（**行向量**约定）、行 3 = 平移（w ≈ 1）、行 4..7 = 前 4 行的逆")
    lines.append("//   （行 4..7 是游戏自己算好的，用来交叉校验我们读到的口径对不对）。")
    lines.append("cbuffer himi_object_cb : register(b1)")
    lines.append("{")
    lines.append("    float4 himi_obj_rows[8];")
    lines.append("};")
    lines.append("// cs-u1 = ref ResourceHI3ObjectMatrix_<role>")
    lines.append("// （ini: RWBuffer + format = R32G32B32A32_FLOAT + array = 3 × 族数")
    lines.append("//   + bind_flags = shader_resource unordered_access）")
    lines.append("// 表的**唯一**布局：第 f 族占 [f*3 + 0..2]，表项 i = 该族 3×4 矩阵第 i 行**逐字**")
    lines.append("// ⇒ (r0.xyz, r3.x) / (r1.xyz, r3.y) / (r2.xyz, r3.z)：第 i 行的 .w 正是平移行 r3 的")
    lines.append("// 第 i 个分量，12 个数一个不浪费、也没有第二套下标约定可写错。全零 = 尚未写入。")
    lines.append("RWBuffer<float4> himi_object_matrix : register(u1);")
    lines.append("")
    lines.append("// 3×4 仿射（列向量约定，行主序存三行；.w = 平移分量）。结构与乘法照 ZZMI 参考插件")
    lines.append("// （.dsh-tmp/loyaltools/common/zzmi_merged_skeleton.py:493-557，HLSL 里已验证可行）。")
    lines.append("struct HimiAffine")
    lines.append("{")
    lines.append("    float4 r0;")
    lines.append("    float4 r1;")
    lines.append("    float4 r2;")
    lines.append("};")
    lines.append("")
    lines.append("HimiAffine himi_affine_identity()")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = float4(1.0, 0.0, 0.0, 0.0);")
    lines.append("    m.r1 = float4(0.0, 1.0, 0.0, 0.0);")
    lines.append("    m.r2 = float4(0.0, 0.0, 1.0, 0.0);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("float4 himi_affine_mul_row(float4 row, HimiAffine b)")
    lines.append("{")
    lines.append("    return row.x * b.r0 + row.y * b.r1 + row.z * b.r2 + float4(0.0, 0.0, 0.0, row.w);")
    lines.append("}")
    lines.append("")
    lines.append("HimiAffine himi_affine_mul(HimiAffine a, HimiAffine b)")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = himi_affine_mul_row(a.r0, b);")
    lines.append("    m.r1 = himi_affine_mul_row(a.r1, b);")
    lines.append("    m.r2 = himi_affine_mul_row(a.r2, b);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("// 余子式求逆（照参考插件的写法）：|det| 太小 ⇒ 返回单位阵（调用方另有校验门）")
    lines.append("HimiAffine himi_affine_inverse(HimiAffine m)")
    lines.append("{")
    lines.append("    float3 a = m.r0.xyz;")
    lines.append("    float3 b = m.r1.xyz;")
    lines.append("    float3 c = m.r2.xyz;")
    lines.append("    float3 c0 = cross(b, c);")
    lines.append("    float3 c1 = cross(c, a);")
    lines.append("    float3 c2 = cross(a, b);")
    lines.append("    float det = dot(a, c0);")
    lines.append("    HimiAffine r = himi_affine_identity();")
    lines.append("    if (abs(det) >= 1e-20)")
    lines.append("    {")
    lines.append("        float inv_det = 1.0 / det;")
    lines.append("        float3 i0 = float3(c0.x, c1.x, c2.x) * inv_det;")
    lines.append("        float3 i1 = float3(c0.y, c1.y, c2.y) * inv_det;")
    lines.append("        float3 i2 = float3(c0.z, c1.z, c2.z) * inv_det;")
    lines.append("        float3 t = float3(m.r0.w, m.r1.w, m.r2.w);")
    lines.append("        r.r0 = float4(i0, -dot(i0, t));")
    lines.append("        r.r1 = float4(i1, -dot(i1, t));")
    lines.append("        r.r2 = float4(i2, -dot(i2, t));")
    lines.append("    }")
    lines.append("    return r;")
    lines.append("}")
    lines.append("")
    lines.append("// 12 个分量是否非全零：本帧还没捕获到该族 palette 时**不要**对它求逆")
    lines.append("// （求逆只会静默回单位阵，乘出来等于没算，把「没数据」伪装成「X = 单位阵」）")
    lines.append("bool himi_affine_nonzero(HimiAffine m)")
    lines.append("{")
    lines.append("    return (m.r0.x != 0.0 || m.r0.y != 0.0 || m.r0.z != 0.0 || m.r0.w != 0.0")
    lines.append("        || m.r1.x != 0.0 || m.r1.y != 0.0 || m.r1.z != 0.0 || m.r1.w != 0.0")
    lines.append("        || m.r2.x != 0.0 || m.r2.y != 0.0 || m.r2.z != 0.0 || m.r2.w != 0.0);")
    lines.append("}")
    lines.append("")
    lines.append("// cb3 行 0..3 → 列向量口径 3×4（与 common/himi_family_x.py::_col_t 同一映射：")
    lines.append("// 结果行 i = (行0[i], 行1[i], 行2[i], 行3[i])）")
    lines.append("HimiAffine himi_cb3_affine()")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = float4(himi_obj_rows[0].x, himi_obj_rows[1].x, himi_obj_rows[2].x, himi_obj_rows[3].x);")
    lines.append("    m.r1 = float4(himi_obj_rows[0].y, himi_obj_rows[1].y, himi_obj_rows[2].y, himi_obj_rows[3].y);")
    lines.append("    m.r2 = float4(himi_obj_rows[0].z, himi_obj_rows[1].z, himi_obj_rows[2].z, himi_obj_rows[3].z);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("// cb3 行 4..7 → 游戏自己算好的那份逆（同一映射）")
    lines.append("HimiAffine himi_cb3_inverse_affine()")
    lines.append("{")
    lines.append("    HimiAffine m;")
    lines.append("    m.r0 = float4(himi_obj_rows[4].x, himi_obj_rows[5].x, himi_obj_rows[6].x, himi_obj_rows[7].x);")
    lines.append("    m.r1 = float4(himi_obj_rows[4].y, himi_obj_rows[5].y, himi_obj_rows[6].y, himi_obj_rows[7].y);")
    lines.append("    m.r2 = float4(himi_obj_rows[4].z, himi_obj_rows[5].z, himi_obj_rows[6].z, himi_obj_rows[7].z);")
    lines.append("    return m;")
    lines.append("}")
    lines.append("")
    lines.append("float himi_affine_max_diff(HimiAffine a, HimiAffine b)")
    lines.append("{")
    lines.append("    float4 d0 = abs(a.r0 - b.r0);")
    lines.append("    float4 d1 = abs(a.r1 - b.r1);")
    lines.append("    float4 d2 = abs(a.r2 - b.r2);")
    lines.append("    float diff = max(max(max(d0.x, d0.y), max(d0.z, d0.w)),")
    lines.append("                     max(max(d1.x, d1.y), max(d1.z, d1.w)));")
    lines.append("    return max(diff, max(max(d2.x, d2.y), max(d2.z, d2.w)));")
    lines.append("}")
    lines.append("")
    lines.append("// cb3 副本是否可信。判据与 common/himi_family_x.py::object_matrix_from_registers 同一套")
    lines.append("// （残差容差放大到 1e-2：副本内容的精度由 copy vs-cb3 的源视图决定）：")
    lines.append("//   ① 行 0..2/4..6 的 .w ≈ 0、行 3/7 的 .w ≈ 1（齐次行形状）；")
    lines.append("//   ② |det3(行 0..2.xyz)| ≥ 1e-6（可逆，否则 M_base⁻¹ 无意义）；")
    lines.append("//   ③ 行 0..3 与行 4..7 互为逆（compose 回单位阵，残差 ≤ 1e-2）。")
    lines.append("// ③ 同时挡住「cb3 绑空/全零」与「cb3 里是别的族的矩阵」两种失败 ⇒ 各失败都退回载荷 X。")
    lines.append("bool himi_object_matrix_valid()")
    lines.append("{")
    lines.append("    if (abs(himi_obj_rows[0].w) > 1e-3 || abs(himi_obj_rows[1].w) > 1e-3")
    lines.append("        || abs(himi_obj_rows[2].w) > 1e-3 || abs(himi_obj_rows[3].w - 1.0) > 1e-3")
    lines.append("        || abs(himi_obj_rows[4].w) > 1e-3 || abs(himi_obj_rows[5].w) > 1e-3")
    lines.append("        || abs(himi_obj_rows[6].w) > 1e-3 || abs(himi_obj_rows[7].w - 1.0) > 1e-3)")
    lines.append("    {")
    lines.append("        return false;")
    lines.append("    }")
    lines.append("    float3 la = himi_obj_rows[0].xyz;")
    lines.append("    float3 lb = himi_obj_rows[1].xyz;")
    lines.append("    float3 lc = himi_obj_rows[2].xyz;")
    lines.append("    if (abs(dot(la, cross(lb, lc))) < 1e-6)")
    lines.append("    {")
    lines.append("        return false;")
    lines.append("    }")
    lines.append("    HimiAffine product = himi_affine_mul(himi_cb3_affine(), himi_cb3_inverse_affine());")
    lines.append("    return himi_affine_max_diff(product, himi_affine_identity()) <= 1e-2;")
    lines.append("}")
    lines.append("")
    lines.append("[numthreads(64, 1, 1)]")
    lines.append("void main(uint3 dispatch_id : SV_DispatchThreadID)")
    lines.append("{")
    lines.append("    uint bone_index = dispatch_id.x;")
    lines.append("")
    lines.append("    // 源槽数取**资源自身维度**：typed Buffer<float4> 的 GetDimensions 是**单参**形式，")
    lines.append("    // 返回 float4 元素数 = 槽数 × 3；必须 /3u 还原成槽数。")
    lines.append("    uint palette_elements = 0u;")
    lines.append("    himi_src_palette.GetDimensions(palette_elements);")
    lines.append("    uint source_slots = palette_elements / 3u;")
    lines.append("")
    lines.append("    // 【本族识别 + 目标基址】由源槽位数查表 —— 这是本文件里**唯一**的排布知识来源，")
    lines.append("    // 不读 IniParams、不读逐族 #define。查不到时保持 0xFFFFFFFF ⇒ 全部写入被越界保护挡掉。")
    lines.append("    uint base_slot = 0xFFFFFFFFu;")
    lines.extend(table_lines)
    lines.append("")
    lines.append("    // 【F5 本族身份资源（cs-t1）】它由导出侧按**本次** plan.family_ranges 生成")
    lines.append("    // （16 字节 = (base, span, version, magic)），是 base/span 的**权威**来源。")
    lines.append("    // 为什么必须独立于 source_slots：单文件方案里 span 与查表键同源 ⇒ 只按源维度")
    lines.append("    // 度量时「本族 span 上界」是恒真判据；副本维度一旦大于本族 span（角色的 palette")
    lines.append("    // 池常大于被引用槽数），多出的骨就会写进**下一族的块**（报告 50 §1.2 入口 ①）。")
    lines.append("    // 身份不可用（绑空 / magic 不符）时**退回冻结表** = 与今天逐字节等价的行为：")
    lines.append("    // 不新增「模型消失」的失败模式，也绝不静默乱写。")
    lines.append("    uint identity_base = 0xFFFFFFFFu;")
    lines.append("    uint span_slots = 0u;")
    lines.append("    float4 attach_range = himi_attach_range[0];")
    lines.append("    // 【t18 / T5-F1】身份**可用** = 魔数命中 **且 span > 0**：span=0 与上面")
    lines.append("    // `uint span_slots = 0u` 的初值同义（= 身份不可用）⇒ 一并退回冻结表，")
    lines.append("    // 与「资源绑空 / 魔数不符」走同一条路（模型侧 attach_write_target 同语义）。")
    lines.append(
        f"    if (asuint(attach_range.w) == {himi_runtime.ATTACH_RANGE_MAGIC_MACRO}"
        " && asuint(attach_range.y) > 0u)"
    )
    lines.append("    {")
    lines.append("        identity_base = asuint(attach_range.x);")
    lines.append("        span_slots = asuint(attach_range.y);")
    lines.append("    }")
    lines.append("    if (identity_base != 0xFFFFFFFFu)")
    lines.append("    {")
    lines.append("        if (base_slot != 0xFFFFFFFFu && base_slot != identity_base)")
    lines.append("        {")
    lines.append("            return;   // 冻结表与本次排布**不一致**：拒写（绝不冒用别的族的槽位）")
    lines.append("        }")
    lines.append("        base_slot = identity_base;   // 身份资源权威（冻结表只作交叉核对）")
    lines.append("    }")
    lines.append("")
    lines.append("    uint skeleton_elements = 0u;")
    lines.append(f"    {skeleton_name}.GetDimensions(skeleton_elements);")
    lines.append("    uint skeleton_slots = skeleton_elements / 3u;")
    lines.append("")
    lines.append("    // 逐 dispatch 的越界保护：**借用槽**（bone_index 越过源槽数）、族未识别、")
    lines.append("    // **本族 span**、目标越界，任一成立就不写。借用槽那一支必须先判 —— 借用槽就在")
    lines.append("    // 本族块尾，它的 bone_index 必然 ≥ 源槽数（palette 维度 = 本族 span），")
    lines.append("    // 放到 span 截断之后就会被截掉，块尾那几个槽永远没人写。")
    lines.append("    // 【t91 借用槽】bone_index 越过本族源槽数 ⇒ 本线程负责的是**块尾的借用槽**：")
    lines.append("    // 那几根骨属于**别的族**，值取 X_源 · P_源（X_源 = M_宿主⁻¹·M_源）。")
    lines.append("    //   · 为什么乘 X_源 而本族自己的槽乘 X_本族：借用顶点已经烘在**宿主族物体空间**里，")
    lines.append("    //     游戏侧那一刻只有 M_宿主 一笔 ⇒ 要让 M_宿主·S·v = M_源·P_源·v 成立，")
    lines.append("    //     S 只能是 M_宿主⁻¹·M_源·P_源；本族自己的骨是 M_本族·P_本族·v，乘的是 X_本族。")
    lines.append("    //     两个槽的值不一样 ⇒ 借用骨必须在本族块尾另占一个槽（不能复用源族的槽）。")
    lines.append("    //   · 为什么由**本族**（借用方）写：这些槽在本族块里，只有本族的 dispatch 有写权；")
    lines.append("    //     P_源 由上面的 palette 副本提供 ⇒ 不需要来源族先跑、也不需要它知道谁借了它。")
    lines.append("    //   · 表行号 = bone_index - 源槽数（行表由导出期算好，目标槽 = 块尾连续分配）。")
    lines.append("    if (bone_index >= source_slots)")
    lines.append("    {")
    lines.append("        uint borrow_index = bone_index - source_slots;")
    lines.append("        // 行数取导出期常量，**不读 GetDimensions**：这张表是 Buffer<uint4>（16 B/元素），")
    lines.append("        // 而 ini 的 typed 视图一旦按 1 分量声明，实机 GetDimensions 只回不到 1/4 的行数")
    lines.append("        // （216 行只回 60）⇒ 后半段借用槽整段不执行。行数由导出期按 ranges 算好。")
    lines.append("        uint borrow_elements = HIMI_BORROW_ROWS;")
    lines.append("        if (borrow_index >= borrow_elements)")
    lines.append("        {")
    lines.append("            return;   // 越过借用表长度：本线程没有对应行（dispatch 按块长向上取整 ⇒ 常见）")
    lines.append("        }")
    lines.append("        uint4 borrow_entry = himi_borrow_map[borrow_index];")
    lines.append("        if (borrow_entry.x == 0xFFFFFFFFu)")
    lines.append("        {")
    lines.append("            return;   // 空行（哨兵）：本族没有这么多借用槽")
    lines.append("        }")
    lines.append("        // 结构门：借用目标必然落在本族块尾（>= base + span）。读到 0 / 被截断的行时")
    lines.append("        // 直接跳过 —— 否则会把「行号 0 的那根骨」写进 slot 0，覆盖别人的块。")
    lines.append("        if (base_slot != 0xFFFFFFFFu && span_slots > 0u && borrow_entry.x < base_slot + span_slots)")
    lines.append("        {")
    lines.append("            return;")
    lines.append("        }")
    lines.append("        uint borrow_rank = borrow_entry.y;")
    lines.append("        uint borrow_dst = borrow_entry.x * 3u;")
    lines.append("        uint borrow_src = borrow_entry.z * 3u;")
    lines.append("        // 只防骨架侧的写越界：源侧的“源局部槽 < 源族槽数”由导出期校验（")
    lines.append("        // borrow_map_layout_problems），运行期多读一行也只是拿到 0（D3D11 有界读），")
    lines.append("        // 而按本族 palette 维度去判会**错杀**来源族更大的那些行。")
    lines.append("        if (borrow_dst + 2u >= skeleton_elements)")
    lines.append("        {")
    lines.append("            return;   // 越界保护：宁可少写一个槽，也不写坏别人的槽")
    lines.append("        }")
    lines.append("        // 按来源族序号取那一族的 palette 副本（序号 = 块基址升序，与 ini 的 cs-tN 同源）")
    lines.append("        float4 borrow_b0 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("        float4 borrow_b1 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("        float4 borrow_b2 = float4(0.0, 0.0, 0.0, 0.0);")
    for rank in range(max(0, len(palette_families) - 1)):
        keyword = "if" if rank == 0 else "else if"
        lines.append(f"        {keyword} (borrow_rank == {rank}u)")
        lines.append("        {")
        lines.append(
            f"            borrow_b0 = {himi_runtime.borrow_palette_hlsl_name(rank)}[borrow_src + 0u];"
        )
        lines.append(
            f"            borrow_b1 = {himi_runtime.borrow_palette_hlsl_name(rank)}[borrow_src + 1u];"
        )
        lines.append(
            f"            borrow_b2 = {himi_runtime.borrow_palette_hlsl_name(rank)}[borrow_src + 2u];"
        )
        lines.append("        }")
    if palette_families:
        last_rank = len(palette_families) - 1
        prefix = "else" if last_rank > 0 else ""
        lines.append(f"        {prefix}".rstrip() if prefix else "        // 只有一族可借时无条件用它（序号越界不该出现，兜底用最后一族）")
        if prefix:
            lines.append("        {")
        lines.append(
            f"        borrow_b0 = {himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_src + 0u];".replace(
                "        borrow_b0", "            borrow_b0"
            )
            if prefix
            else f"        borrow_b0 = {himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_src + 0u];"
        )
        lines.append(
            f"        borrow_b1 = {himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_src + 1u];".replace(
                "        borrow_b1", "            borrow_b1"
            )
            if prefix
            else f"        borrow_b1 = {himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_src + 1u];"
        )
        lines.append(
            f"        borrow_b2 = {himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_src + 2u];".replace(
                "        borrow_b2", "            borrow_b2"
            )
            if prefix
            else f"        borrow_b2 = {himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_src + 2u];"
        )
        if prefix:
            lines.append("        }")
    lines.append("        // X_源 = P_本族[p] · P_来源[q]⁻¹（**own 在前**）：p/q 是同一根物理骨在两支")
    lines.append("        // palette 里的局部槽，由导出期解出（HIMI_BORROW_PAIR_* 索引表）。两支都是本帧")
    lines.append("        // 活数据 ⇒ X 只能每帧现算；载荷 [4..6] 里那支 X 是**宿主族**的，拿它的逆用在")
    lines.append("        // 借用槽上就是错口径（那张表恒零时更错：X 静默退回单位阵，借用骨直接摆错位置）。")
    lines.append("        HimiAffine borrow_x;")
    lines.append("        borrow_x.r0 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("        borrow_x.r1 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("        borrow_x.r2 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("        bool borrow_x_ready = false;")
    lines.append("        uint borrow_pair_index = asuint(himi_attach_range[7].x) * HIMI_BORROW_RANK_COUNT + borrow_rank;")
    lines.append(
        "        if (borrow_rank < HIMI_BORROW_RANK_COUNT"
        " && borrow_pair_index < HIMI_BORROW_PAIR_COUNT)"
    )
    lines.append("        {")
    lines.append("            uint borrow_pair_own = HIMI_BORROW_PAIR_OWN[borrow_pair_index];")
    lines.append("            uint borrow_pair_src = HIMI_BORROW_PAIR_SRC[borrow_pair_index];")
    lines.append("            if (borrow_pair_own != 0xFFFFFFFFu && borrow_pair_src != 0xFFFFFFFFu)")
    lines.append("            {")
    lines.append("                // 本族那一份：本段 cs-t0 就是本族 palette（索引 = 那根骨在**本族**的槽）")
    lines.append("                HimiAffine borrow_m_own;")
    lines.append("                borrow_m_own.r0 = himi_src_palette[borrow_pair_own * 3u + 0u];")
    lines.append("                borrow_m_own.r1 = himi_src_palette[borrow_pair_own * 3u + 1u];")
    lines.append("                borrow_m_own.r2 = himi_src_palette[borrow_pair_own * 3u + 2u];")
    lines.append("                // 来源族那一份：按 rank 选 cs-t2/t3/t5/… 的 palette 副本")
    lines.append("                HimiAffine borrow_m_src;")
    lines.append("                borrow_m_src.r0 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("                borrow_m_src.r1 = float4(0.0, 0.0, 0.0, 0.0);")
    lines.append("                borrow_m_src.r2 = float4(0.0, 0.0, 0.0, 0.0);")
    if palette_families:
        last_rank = len(palette_families) - 1
        for rank in range(last_rank):
            keyword = "if" if rank == 0 else "else if"
            lines.append(f"                {keyword} (borrow_rank == {rank}u)")
            lines.append("                {")
            for row in range(3):
                lines.append(
                    f"                    borrow_m_src.r{row} = "
                    f"{himi_runtime.borrow_palette_hlsl_name(rank)}[borrow_pair_src * 3u + {row}u];"
                )
            lines.append("                }")
        tail = "                    " if last_rank > 0 else "                "
        if last_rank > 0:
            lines.append("                else")
            lines.append("                {")
        for row in range(3):
            lines.append(
                f"{tail}borrow_m_src.r{row} = "
                f"{himi_runtime.borrow_palette_hlsl_name(last_rank)}[borrow_pair_src * 3u + {row}u];"
            )
        if last_rank > 0:
            lines.append("                }")
    lines.append("                if (himi_affine_nonzero(borrow_m_own) && himi_affine_nonzero(borrow_m_src))")
    lines.append("                {")
    lines.append("                    borrow_x = himi_affine_mul(borrow_m_own, himi_affine_inverse(borrow_m_src));")
    lines.append("                    borrow_x_ready = true;")
    lines.append("                }")
    lines.append("            }")
    lines.append("        }")
    lines.append("        if (!borrow_x_ready)")
    lines.append("        {")
    lines.append("            // 骨对缺失或两族无共享骨：尝试从实时 himi_object_matrix 计算 M_host⁻¹ · M_src")
    lines.append("            uint object_elements_b = 0u;")
    lines.append("            himi_object_matrix.GetDimensions(object_elements_b);")
    lines.append("            uint last_row_b = object_elements_b >= 3u ? (object_elements_b - 3u) / 3u : 0u;")
    lines.append("            uint own_index_b = asuint(himi_attach_range[7].x);")
    lines.append("            if (object_elements_b >= 3u && own_index_b <= last_row_b && borrow_rank <= last_row_b)")
    lines.append("            {")
    lines.append("                HimiAffine m_host_b;")
    lines.append("                if (himi_object_matrix_valid())")
    lines.append("                {")
    lines.append("                    m_host_b = himi_cb3_affine();")
    lines.append("                }")
    lines.append("                else")
    lines.append("                {")
    lines.append("                    uint host_row_b = own_index_b * 3u;")
    lines.append("                    m_host_b.r0 = himi_object_matrix[host_row_b + 0u];")
    lines.append("                    m_host_b.r1 = himi_object_matrix[host_row_b + 1u];")
    lines.append("                    m_host_b.r2 = himi_object_matrix[host_row_b + 2u];")
    lines.append("                }")
    lines.append("                uint src_row_b = borrow_rank * 3u;")
    lines.append("                HimiAffine m_src_b;")
    lines.append("                m_src_b.r0 = himi_object_matrix[src_row_b + 0u];")
    lines.append("                m_src_b.r1 = himi_object_matrix[src_row_b + 1u];")
    lines.append("                m_src_b.r2 = himi_object_matrix[src_row_b + 2u];")
    lines.append("                if (himi_affine_nonzero(m_host_b) && himi_affine_nonzero(m_src_b))")
    lines.append("                {")
    lines.append("                    borrow_x = himi_affine_mul(himi_affine_inverse(m_host_b), m_src_b);")
    lines.append("                    borrow_x_ready = true;")
    lines.append("                }")
    lines.append("            }")
    lines.append("        }")
    lines.append("        if (!borrow_x_ready)")
    lines.append("        {")
    lines.append("            borrow_x = himi_affine_identity();")
    lines.append("        }")
    lines.append("        // 列向量口径：X · P = 逐行点积（P 拆成三列 + 平移列）")
    lines.append("        float3 borrow_d0 = float3(borrow_b0.x, borrow_b1.x, borrow_b2.x);")
    lines.append("        float3 borrow_d1 = float3(borrow_b0.y, borrow_b1.y, borrow_b2.y);")
    lines.append("        float3 borrow_d2 = float3(borrow_b0.z, borrow_b1.z, borrow_b2.z);")
    lines.append("        float3 borrow_dt = float3(borrow_b0.w, borrow_b1.w, borrow_b2.w);")
    lines.append("        float3 borrow_a0 = float3(borrow_x.r0.x, borrow_x.r0.y, borrow_x.r0.z);")
    lines.append("        float3 borrow_a1 = float3(borrow_x.r1.x, borrow_x.r1.y, borrow_x.r1.z);")
    lines.append("        float3 borrow_a2 = float3(borrow_x.r2.x, borrow_x.r2.y, borrow_x.r2.z);")
    lines.append(f"        {skeleton_name}[borrow_dst + 0u] = float4(")
    lines.append("            dot(borrow_a0, borrow_d0), dot(borrow_a0, borrow_d1),")
    lines.append("            dot(borrow_a0, borrow_d2), dot(borrow_a0, borrow_dt) + borrow_x.r0.w);")
    lines.append(f"        {skeleton_name}[borrow_dst + 1u] = float4(")
    lines.append("            dot(borrow_a1, borrow_d0), dot(borrow_a1, borrow_d1),")
    lines.append("            dot(borrow_a1, borrow_d2), dot(borrow_a1, borrow_dt) + borrow_x.r1.w);")
    lines.append(f"        {skeleton_name}[borrow_dst + 2u] = float4(")
    lines.append("            dot(borrow_a2, borrow_d0), dot(borrow_a2, borrow_d1),")
    lines.append("            dot(borrow_a2, borrow_d2), dot(borrow_a2, borrow_dt) + borrow_x.r2.w);")
    lines.append("        return;   // 借用槽写完即止：本线程不再走本族槽那条路")
    lines.append("    }")
    lines.append("    if (base_slot == 0xFFFFFFFFu)")
    lines.append("    {")
    lines.append("        return;   // 未知族：不写，绝不冒用别的族的槽位")
    lines.append("    }")
    lines.append("    // 【F5】本族 span 上界：副本维度**大于**本族 span 时**截断**（本线程不写），")
    lines.append("    // 绝不把多出来的骨写进下一族的块。span 来自身份资源，与 source_slots 独立。")
    lines.append("    if (span_slots > 0u && bone_index >= span_slots)")
    lines.append("    {")
    lines.append("        return;")
    lines.append("    }")
    lines.append("    uint target_slot = base_slot + bone_index;")
    lines.append("    if (target_slot >= skeleton_slots)")
    lines.append("    {")
    lines.append("        return;   // 越界保护：宁可少写，也不写坏别人的槽")
    lines.append("    }")
    lines.append("")
    lines.append("    // 每根骨占 3 个 float4（4×3 行主序矩阵），两端都显式按 *3 取址 ——")
    lines.append("    // 与两侧的 typed 16 B 视图精确匹配。")
    lines.append("    uint src = bone_index * 3u;                 // 读侧索引：bone * 3u")
    lines.append(f"    uint dst = target_slot * 3u;                // 写侧索引：slot * 3u")
    lines.append("")
    lines.append("    // 【t39/t44 历史 · t78 方案 B】这一段原本复合法因子 Q_f⁻¹（P'_f[b] = P_f[b] ∘ Q_f⁻¹，")
    lines.append("    // 配合顶点侧 v' = Q_f·v）。方案 B 取消顶点侧校准后 `[1..3]` **恒为单位阵** ⇒ 这里")
    lines.append("    // 乘的是恒等变换：写进合并骨架的就是 S = X_f · P_f（游戏侧 M_base·S·v = M_f·P_f·v）。")
    lines.append("    // 代码保留只为兼容既有 128 B 载荷布局与 CS 下标（删掉它就要同时改 ini/载荷/ABCD 对齐）；")
    lines.append("    // 版本 < 3 或这一段全零 ⇒ 直接跳过（与旧行为一致，两种都等价于恒等）。")
    lines.append("    float4 placement0 = himi_attach_range[1];")
    lines.append("    float4 placement1 = himi_attach_range[2];")
    lines.append("    float4 placement2 = himi_attach_range[3];")
    lines.append("    bool calibrated = (asuint(attach_range.z) >= 3u)")
    lines.append("        && (placement0.x != 0.0 || placement0.y != 0.0 || placement0.z != 0.0")
    lines.append("            || placement0.w != 0.0 || placement1.x != 0.0 || placement1.y != 0.0")
    lines.append("            || placement1.z != 0.0 || placement1.w != 0.0 || placement2.x != 0.0")
    lines.append("            || placement2.y != 0.0 || placement2.z != 0.0 || placement2.w != 0.0);")
    lines.append("    float3 qc0 = float3(placement0.x, placement1.x, placement2.x);")
    lines.append("    float3 qc1 = float3(placement0.y, placement1.y, placement2.y);")
    lines.append("    float3 qc2 = float3(placement0.z, placement1.z, placement2.z);")
    # P∘Q 的平移列 = A·s + t：s 是 Q 的平移列，必须逐分量取，
    # 不能写成 placement_i.www（那是第 i 行平移分量的广播，会算成 (p_i0+p_i1+p_i2)·t_i）。
    lines.append("    float3 placement_t = float3(placement0.w, placement1.w, placement2.w);")
    lines.append("    float4 src0 = himi_src_palette[src + 0u];")
    lines.append("    float4 src1 = himi_src_palette[src + 1u];")
    lines.append("    float4 src2 = himi_src_palette[src + 2u];")
    lines.append("    float4 out0 = src0;")
    lines.append("    float4 out1 = src1;")
    lines.append("    float4 out2 = src2;")
    lines.append("    if (calibrated)")
    lines.append("    {")
    lines.append("        // P' = P ∘ Q：线性部分 A·B，平移 A·s + t（列向量约定，行主序存储）。")
    lines.append("        out0 = float4(dot(src0.xyz, qc0), dot(src0.xyz, qc1), dot(src0.xyz, qc2),")
    lines.append("                      dot(src0.xyz, placement_t) + src0.w);")
    lines.append("        out1 = float4(dot(src1.xyz, qc0), dot(src1.xyz, qc1), dot(src1.xyz, qc2),")
    lines.append("                      dot(src1.xyz, placement_t) + src1.w);")
    lines.append("        out2 = float4(dot(src2.xyz, qc0), dot(src2.xyz, qc1), dot(src2.xyz, qc2),")
    lines.append("                      dot(src2.xyz, placement_t) + src2.w);")
    lines.append("    }")
    lines.append("    // 【t39-X 族级物体矩阵】合并几何整块落在**宿主族**的一笔 draw 里 ⇒ 那一刻只有")
    lines.append("    // 宿主族的对象矩阵 M_base；而各族真值是 world = M_f·(P_f·v) ⇒ 必须再左乘")
    lines.append("    // X_f = M_base⁻¹·M_f：")
    lines.append("    //     S = X_f ∘ P_f ∘ (单位阵) = X_f · P_f   ← 方案 B：[1..3] 恒为单位阵")
    lines.append("    // 【t74 X 的来源】主路径 = **运行期从本族 vs-cb3 实时算**（下面的选择块）；载荷")
    lines.append("    // [4..6] 是导出期烘死的同一份 X_f，只在 cb3 拿不到/校验不过时兜底。")
    lines.append("    // X 与 Q 是**两个独立开关**：X 的 12 个数全零 ⇒ 不左乘（逐字节等价旧行为）；")
    lines.append("    // 宿主族自己的 X 是单位阵（非零 ⇒ 会施加，但数值恒等）。")
    lines.append("    float4 attach_x0 = himi_attach_range[4];")
    lines.append("    float4 attach_x1 = himi_attach_range[5];")
    lines.append("    float4 attach_x2 = himi_attach_range[6];")
    lines.append("")
    lines.append("    // 【t74 运行期 X】只在载荷版本 ≥ 4（= 带 [7] 族索引）时启用，旧产物（v3）逐字节照旧。")
    lines.append("    // 三态，顺序即优先级，**任何分支都不 return** —— draw 必须无条件执行，分支只决定「写什么矩阵」：")
    lines.append("    //   (a) own_index == base_index ⇒ X = 单位阵（不读表、不依赖采集）；")
    lines.append("    //   (b) 采集有效 ⇒ **先**把本族三行写进表（表只由这条路径填起来）、**再**读宿主族三行：")
    lines.append("    //       非全零 ⇒ X = M_base⁻¹·M_own；全零（非常首帧）⇒ 保留载荷兜底 X；")
    lines.append("    //   (c) 采集无效 ⇒ 读表里 own + base 两段（上一帧写下的好值）：都非全零 ⇒ X = M_base⁻¹·M_own，")
    lines.append("    //       否则保留兜底 X。**采集无效时绝不写表** —— 那会把表里已有的好值覆盖成垃圾。")
    lines.append("    if (asuint(attach_range.z) >= 4u)")
    lines.append("    {")
    lines.append("        uint own_index = asuint(himi_attach_range[7].x);")
    lines.append("        uint base_index = asuint(himi_attach_range[7].y);")
    lines.append("        HimiAffine own_object = himi_cb3_affine();")
    lines.append("        bool object_valid = himi_object_matrix_valid();")
    lines.append("        // 表长由 ini 的 array=（3 × 族数）决定：越界的族索引一律不碰表，防 OOB 读写")
    lines.append("        uint object_elements = 0u;")
    lines.append("        himi_object_matrix.GetDimensions(object_elements);")
    lines.append("        uint last_row = object_elements >= 3u ? (object_elements - 3u) / 3u : 0u;")
    lines.append("        bool object_in_range = object_elements >= 3u")
    lines.append("            && own_index <= last_row && base_index <= last_row;")
    lines.append("        if (object_valid && object_in_range)")
    lines.append("        {")
    lines.append("            // (b) 写表：把本族**校验通过**的物体矩阵写进自己那一段（宿主族那笔随后读回来）。")
    lines.append("            // 每族只在自己那笔里写自己那段 ⇒ 同一 dispatch 内不存在读写竞争。")
    lines.append("            uint own_row = own_index * 3u;")
    lines.append("            himi_object_matrix[own_row + 0u] = own_object.r0;")
    lines.append("            himi_object_matrix[own_row + 1u] = own_object.r1;")
    lines.append("            himi_object_matrix[own_row + 2u] = own_object.r2;")
    lines.append("        }")
    lines.append("        if (own_index == base_index)")
    lines.append("        {")
    lines.append("            // (a) 宿主族：X 恒为单位阵（不能读回自己那段：那是上一帧它自己写的 M_base）")
    lines.append("            attach_x0 = float4(1.0, 0.0, 0.0, 0.0);")
    lines.append("            attach_x1 = float4(0.0, 1.0, 0.0, 0.0);")
    lines.append("            attach_x2 = float4(0.0, 0.0, 1.0, 0.0);")
    lines.append("        }")
    lines.append("        else if (object_valid && object_in_range)")
    lines.append("        {")
    lines.append("            // (b) 采集有效：M_own 就是本次 dispatch 的 cb3（刚写进表的那份）")
    lines.append("            uint base_row = base_index * 3u;")
    lines.append("            float4 base0 = himi_object_matrix[base_row + 0u];")
    lines.append("            float4 base1 = himi_object_matrix[base_row + 1u];")
    lines.append("            float4 base2 = himi_object_matrix[base_row + 2u];")
    lines.append("            // 宿主族那一段全零 = 本帧还没写过（首次进游戏 / 刚重载）⇒ 保留兜底 X")
    lines.append("            if (base0.x != 0.0 || base0.y != 0.0 || base0.z != 0.0 || base0.w != 0.0")
    lines.append("                || base1.x != 0.0 || base1.y != 0.0 || base1.z != 0.0 || base1.w != 0.0")
    lines.append("                || base2.x != 0.0 || base2.y != 0.0 || base2.z != 0.0 || base2.w != 0.0)")
    lines.append("            {")
    lines.append("                HimiAffine m_base;")
    lines.append("                m_base.r0 = base0;")
    lines.append("                m_base.r1 = base1;")
    lines.append("                m_base.r2 = base2;")
    lines.append("                HimiAffine x_rows = himi_affine_mul(himi_affine_inverse(m_base), own_object);")
    lines.append("                attach_x0 = x_rows.r0;")
    lines.append("                attach_x1 = x_rows.r1;")
    lines.append("                attach_x2 = x_rows.r2;")
    lines.append("            }")
    lines.append("        }")
    lines.append("        else if (object_in_range)")
    lines.append("        {")
    lines.append("            // (c) 采集无效：拿不到可信的 M_own ⇒ 只能读表里**上一帧**写下的 own + base 两段。")
    lines.append("            // 这里绝不写表：写了就把表里已有的好值覆盖成垃圾（下一帧也一起坏掉）。")
    lines.append("            uint own_row = own_index * 3u;")
    lines.append("            float4 own0 = himi_object_matrix[own_row + 0u];")
    lines.append("            float4 own1 = himi_object_matrix[own_row + 1u];")
    lines.append("            float4 own2 = himi_object_matrix[own_row + 2u];")
    lines.append("            uint base_row = base_index * 3u;")
    lines.append("            float4 base0 = himi_object_matrix[base_row + 0u];")
    lines.append("            float4 base1 = himi_object_matrix[base_row + 1u];")
    lines.append("            float4 base2 = himi_object_matrix[base_row + 2u];")
    lines.append("            // 两段都非全零才够算：任一段空 ⇒ 表还没被写过 ⇒ 保留兜底 X")
    lines.append("            bool own_ready = (own0.x != 0.0 || own0.y != 0.0 || own0.z != 0.0 || own0.w != 0.0")
    lines.append("                || own1.x != 0.0 || own1.y != 0.0 || own1.z != 0.0 || own1.w != 0.0")
    lines.append("                || own2.x != 0.0 || own2.y != 0.0 || own2.z != 0.0 || own2.w != 0.0);")
    lines.append("            bool base_ready = (base0.x != 0.0 || base0.y != 0.0 || base0.z != 0.0 || base0.w != 0.0")
    lines.append("                || base1.x != 0.0 || base1.y != 0.0 || base1.z != 0.0 || base1.w != 0.0")
    lines.append("                || base2.x != 0.0 || base2.y != 0.0 || base2.z != 0.0 || base2.w != 0.0);")
    lines.append("            if (own_ready && base_ready)")
    lines.append("            {")
    lines.append("                HimiAffine m_base;")
    lines.append("                m_base.r0 = base0;")
    lines.append("                m_base.r1 = base1;")
    lines.append("                m_base.r2 = base2;")
    lines.append("                HimiAffine m_own;")
    lines.append("                m_own.r0 = own0;")
    lines.append("                m_own.r1 = own1;")
    lines.append("                m_own.r2 = own2;")
    lines.append("                HimiAffine x_rows = himi_affine_mul(himi_affine_inverse(m_base), m_own);")
    lines.append("                attach_x0 = x_rows.r0;")
    lines.append("                attach_x1 = x_rows.r1;")
    lines.append("                attach_x2 = x_rows.r2;")
    lines.append("            }")
    lines.append("        }")
    lines.append("    }")
    lines.append("    bool x_calibrated = (attach_x0.x != 0.0 || attach_x0.y != 0.0 || attach_x0.z != 0.0")
    lines.append("        || attach_x0.w != 0.0 || attach_x1.x != 0.0 || attach_x1.y != 0.0")
    lines.append("        || attach_x1.z != 0.0 || attach_x1.w != 0.0 || attach_x2.x != 0.0")
    lines.append("        || attach_x2.y != 0.0 || attach_x2.z != 0.0 || attach_x2.w != 0.0);")
    lines.append("    if (x_calibrated)")
    lines.append("    {")
    lines.append("        // 左乘：把 out（= P∘Q⁻¹，3×4 行主序）先拆成三列 + 平移列，再逐行点积（列向量约定）。")
    lines.append("        float3 xa0 = float3(attach_x0.x, attach_x0.y, attach_x0.z);")
    lines.append("        float3 xa1 = float3(attach_x1.x, attach_x1.y, attach_x1.z);")
    lines.append("        float3 xa2 = float3(attach_x2.x, attach_x2.y, attach_x2.z);")
    lines.append("        float3 d0 = float3(out0.x, out1.x, out2.x);")
    lines.append("        float3 d1 = float3(out0.y, out1.y, out2.y);")
    lines.append("        float3 d2 = float3(out0.z, out1.z, out2.z);")
    lines.append("        float3 dt = float3(out0.w, out1.w, out2.w);")
    lines.append("        out0 = float4(dot(xa0, d0), dot(xa0, d1), dot(xa0, d2), dot(xa0, dt) + attach_x0.w);")
    lines.append("        out1 = float4(dot(xa1, d0), dot(xa1, d1), dot(xa1, d2), dot(xa1, dt) + attach_x1.w);")
    lines.append("        out2 = float4(dot(xa2, d0), dot(xa2, d1), dot(xa2, d2), dot(xa2, dt) + attach_x2.w);")
    lines.append("    }")
    lines.append(f"    {skeleton_name}[dst + 0u] = out0;")
    lines.append(f"    {skeleton_name}[dst + 1u] = out1;")
    lines.append(f"    {skeleton_name}[dst + 2u] = out2;")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


#: 从基址表行里抽出 ``(源槽位数, 基址)``（表行形状由 :func:`merged_attach_base_table_lines`
#: 生成；解析侧只认这个形状，形状一改这里必须一起改）。
_BASE_TABLE_ROW_RE = re.compile(r"source_slots\s*==\s*(\d+)u\)\s*\{\s*base_slot\s*=\s*(\d+)u;")


def merged_attach_base_table_lines_in_text(text) -> list:
    """从**渲染好的 CS 全文**里把基址表那几行原样取出来（含行尾的族注释）。"""
    rows: list = []
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("if (source_slots ==") or stripped.startswith(
            "else if (source_slots =="
        ):
            rows.append(raw.rstrip())
    return rows


def merged_attach_base_table_text_problems(text) -> list:
    """CS 里那张基址表的**形状**判据（纯文本、**不比对任何常量** —— 表的内容随 plan 变）。

    * 必须**恰好**有一条起始 ``if (source_slots == …)`` 行，且它是表的第一行；
      少了它 ``base_slot`` 会停在 ``0xFFFFFFFF`` ⇒ 整族拒写（模型静默消失）；
    * 其余行必须是 ``else if (…)``（漏掉 ``else`` 说明表被手改坏了）；
    * 每行都必须解析得出 ``(源槽位数, 基址)``，且**源槽位数不得重复**：运行期只按源槽位数
      查表，两族同 span ⇒ 后写的覆盖先写的（这是唯一还剩下、必须 Fatal 的真矛盾）。
    """
    code = _strip_hlsl_comments(text)
    rows = merged_attach_base_table_lines_in_text(code)
    if not rows:
        return [
            "缺基址表（必须有一行 `if (source_slots == <n>u) { base_slot = <m>u; }`）"
            "—— 没有它基址永远是 0xFFFFFFFF，整族拒写"
        ]
    problems: list = []
    starts = [row for row in rows if row.lstrip().startswith("if (")]
    if len(starts) != 1:
        problems.append(f"基址表必须只有一条起始 `if (` 行，实际 {len(starts)} 条")
    if not rows[0].lstrip().startswith("if ("):
        problems.append("基址表第一行必须是 `if (`（否则 base_slot 停在 0xFFFFFFFF、整族拒写）")
    spans: dict = {}
    for row in rows:
        match = _BASE_TABLE_ROW_RE.search(row)
        if not match:
            problems.append(f"基址表行无法解析：{row}")
            continue
        span = int(match.group(1))
        if span in spans:
            problems.append(
                f"基址表出现重复的源槽位数 {span}（两族会撞同一个基址，后写的覆盖先写的）"
            )
        spans[span] = int(match.group(2))
    return problems


def merged_attach_hlsl_problems(text: str) -> list:
    """**单文件合并 attach CS 的判据**（纯函数，返回违规清单；空 = 通过）。

    与 `tests/test_himi_merged_runtime.py::_attach_cs_problems`（t57 的**逐族**口径）
    分开：那条判据要求 `#define HIMI_ATTACH_BASE 0`，本口径**禁止**它 —— 合并成一个文件后
    基址只能从运行期量出来。

    两态证明直接复用它：把基址表删掉 / 把 IniParams 读回来 / 把 `* 3u` 去掉，
    每一条都必须报错（见 `tests/test_himi_merged_runtime.py::HIMIMergedAttachCsTests`）。
    """
    code = _strip_hlsl_comments(text)
    problems: list = []
    if "[numthreads(64, 1, 1)]" not in code:
        problems.append("缺 [numthreads(64, 1, 1)]（与 Dispatch = ceil(槽数/64) 配套）")
    if "void main(" not in code:
        problems.append("缺 void main(...) 入口")
    if "SV_DispatchThreadID" not in code:
        problems.append("缺 SV_DispatchThreadID（块搬运的线程映射）")
    if "GetDimensions(palette_elements);" not in code:
        problems.append("源槽数必须取资源自身维度（GetDimensions 单参）")
    if "GetDimensions(palette_elements, " in code:
        problems.append("不得出现两参 GetDimensions（那是结构化声明口径）")
    if "palette_elements / 3u" not in code:
        problems.append("单参 GetDimensions 之后必须 /3u 还原槽数（源）")
    if "skeleton_elements / 3u" not in code:
        problems.append("单参 GetDimensions 之后必须 /3u 还原槽数（骨架）")
    if "IniParams" in code:
        problems.append("CS 不得读 IniParams（fork 相关 ⇒ 读错位置只会读到未设置的 0）")
    if "StructuredBuffer" in code:
        problems.append("不得出现结构化声明（typed 16 B 与结构化 48 B 混用会落 D3D11 未定义区）")
    if "[CustomShader" in code:
        problems.append("CS 文件里不得声明 ini 段（段在 ini 侧）")
    if "Dispatch =" in code or "Dispatch(" in code:
        problems.append("Dispatch 由 ini 段给出，CS 里不得出现")
    if "HIMI_ATTACH_BASE" in code:
        problems.append(
            "合并成单文件后不得再出现逐族 #define HIMI_ATTACH_BASE（基址必须由源槽位数查表算出）"
        )
    # 基址表：形状与唯一性（内容随 plan 变，所以这里不比任何常量；见
    # merged_attach_base_table_lines / merged_attach_base_table_text_problems）。
    problems.extend(merged_attach_base_table_text_problems(text))
    if "base_slot = base_slot" in code or "base_slot + bone_index" not in code:
        problems.append("写侧必须按 (base_slot + bone_index) 取目标槽位")
    if "bone_index * 3u" not in code:
        problems.append("读侧必须按 bone * 3u 索引（typed 16 B 视图）")
    if "target_slot * 3u" not in code:
        problems.append("写侧必须按 target_slot * 3u 索引（typed 16 B 视图）")
    # ---- 【F5】本族身份资源 + 本族 span 上界（t4 新增；见报告 50 §1.2 入口 ①）----
    if "himi_attach_range : register(t1);" not in code:
        problems.append(
            "缺 cs-t1 身份资源声明（Buffer<float4> himi_attach_range : register(t1);）——"
            "没有它，本族 span 与查表键同源 ⇒ span 上界恒真，副本维度大于 span 时会写进下一族的块"
        )
    if f"asuint(attach_range.w) == {himi_runtime.ATTACH_RANGE_MAGIC_MACRO}" not in code:
        problems.append(
            f"身份资源必须校验魔数（asuint(attach_range.w) == {himi_runtime.ATTACH_RANGE_MAGIC_MACRO}）"
        )
    if "asuint(attach_range.y) > 0u" not in code:
        problems.append(
            "身份可用性必须包含 span > 0（span=0 是「身份不可用」哨兵，语义须与 "
            "common/himi_merged_runtime.py::attach_write_target 一致 —— t18/T5-F1）"
        )
    if "identity_base = asuint(attach_range.x);" not in code:
        problems.append("身份资源必须提供 base（identity_base = asuint(attach_range.x)）")
    if "span_slots = asuint(attach_range.y);" not in code:
        problems.append("身份资源必须提供 span（span_slots = asuint(attach_range.y)）")
    if "base_slot != identity_base" not in code:
        problems.append(
            "冻结表与身份资源**不一致时**必须拒写（base_slot != identity_base ⇒ return）"
        )
    if "bone_index >= span_slots" not in code:
        problems.append(
            "缺【F5】本族 span 上界（if (span_slots > 0u && bone_index >= span_slots) return;）——"
            "副本维度大于本族 span 时会把多出的骨写进下一族的块"
        )
    # ---- 【t39/t78 方案 B】palette 侧只写 S = X_f·P_f（载荷 [1..3] 恒为单位阵，这里仍当因子复合）----
    if "himi_attach_range[1]" not in code or "himi_attach_range[2]" not in code \
            or "himi_attach_range[3]" not in code:
        problems.append(
            "缺【t39】族级摆放的读取（placement0/1/2 = himi_attach_range[1..3]）——"
            "palette 侧不校准会让跨族借权重落在错的位置"
        )
    if "asuint(attach_range.z) >= 3u" not in code:
        problems.append(
            "缺【t39/t44】版本门（asuint(attach_range.z) >= 3u）—— v1/v2 载荷（16 B / 存 Q_f 的旧方向）"
            "一律不得复合：会让骨架被写成 0 或按**取反**的方向复合"
        )
    if "float4 out0 = src0;" not in code or "out0 = float4(dot(src0.xyz, qc0)" not in code:
        problems.append(
            "缺【t39】P' = P ∘ Q 的复合（线性部分 A·B + 平移 A·s + t）；"
            "直接写 himi_src_palette 就等于「不校准」"
        )
    if "[dst + 0u] = out0;" not in code:
        problems.append("【t39】写侧必须写复合后的 out0/1/2，而不是原始 palette 行")
    # ---- 【t41 / T40-F3】平移项：文本门 + 与 CS 文本同源的数值门 ----
    # 把表达式**解析成模型**（himi_runtime.attach_compose_model_from_hlsl），只用一种事实源：
    # 文本一被改坏（例如退回 `placement_i.www`、或被 `* 0.0` 抹掉平移），这里就报错，
    # 而同一份模型在测试里还会与 mat3x4_compose 做数值对齐。
    compose_model = himi_runtime.attach_compose_model_from_hlsl(text)
    for problem in compose_model.get("problems") or ():
        problems.append("【t41/T40-F3】" + str(problem))
    wrong_modes = {
        index: mode for index, mode in (compose_model.get("modes") or {}).items()
        if mode != himi_runtime.ATTACH_COMPOSE_CORRECT
    }
    if wrong_modes:
        problems.append(
            "【t41/T40-F3】P∘Q 的平移项写法不对（应 `dot(src_i.xyz, placement_t) + src_i.w`，"
            "其中 `placement_t = float3(placement0.w, placement1.w, placement2.w)`）："
            + "；".join(f"out{index}={mode}" for index, mode in sorted(wrong_modes.items()))
        )
    if "float3 placement_t = float3(placement0.w, placement1.w, placement2.w);" not in code:
        problems.append(
            "【t41/T40-F3】缺 `float3 placement_t = float3(placement0.w, placement1.w, placement2.w);`"
            "（平移列必须按行取 `placement_k.w` 再与 `src_i.xyz` 点积）"
        )
    # ---- 【t44 / t78 方案 B】文本门保留原样：palette 侧仍把载荷当**因子**直接复合
    # （`P ∘ factor`），但方案 B 起那个因子**恒为单位阵**（顶点侧不再搬顶点，见
    # `_himi_calibrate_bind_positions` 的只读诊断）。这一段不删，因为它是「载荷必须在 CS 里
    # 当因子用、不能在 CS 里再求逆」的护栏；数值语义由 verify_attach_range_files 的单位阵断言钉住。
    if "himi_attach_range[1]" not in code or "placement_t" not in code:
        problems.append("【t44】palette 侧必须把载荷当**因子**直接复合（`P ∘ factor`，factor = Q⁻¹）")
    for index in (0, 1, 2):
        if f"placement{index}.www" in code:
            problems.append(
                f"【t41/T40-F3】出现了旧写法 `placement{index}.www`"
                "（= `(p_i0+p_i1+p_i2)·t_i`，会把盘绕项算错，实测差 3.010439）"
            )
    # ---- 【t39-X】族级物体矩阵 X_f 的左乘（A4 的三条锚点：删任意一条都必须报错）----
    x_read_tokens = ("himi_attach_range[4]", "himi_attach_range[5]", "himi_attach_range[6]")
    if not all(token in code for token in x_read_tokens):
        problems.append(
            "缺【t39-X】族级物体矩阵的读取（attach_x0/1/2 = himi_attach_range[4..6]）——"
            "合并在宿主族的一笔 draw 里渲染，缺这一项时每个族都会按**宿主族的对象矩阵**摆放"
        )
    if "float3 d0 = float3(out0.x, out1.x, out2.x);" not in code:
        problems.append(
            "缺【t39-X】左乘前的列拆分（float3 d0/d1/d2 = out 的三列、dt = out 的平移列）"
        )
    if "out0 = float4(dot(xa0, d0), dot(xa0, d1), dot(xa0, d2), dot(xa0, dt) + attach_x0.w);" not in code:
        problems.append(
            "缺【t39-X】S = X∘(P∘Q⁻¹) 的左乘（线性部分 A_X·A_out + 平移 A_X·t_out + t_X）"
        )
    if "float3 xa0 = float3(attach_x0.x, attach_x0.y, attach_x0.z);" not in code:
        problems.append("【t39-X】缺 `float3 xa0/xa1/xa2 = attach_x{0,1,2}.xyz`（X 行的线性部分）")
    # ---- 【t74】运行期 X：本族 vs-cb3 → u1 物体矩阵表 → X 选择（每一条都能被删行变异证红）----
    for probe, message in HIMI_RUNTIME_X_PROBES:
        if _strip_hlsl_comments(probe).strip() not in code:
            problems.append(message)
    # 兜底 X 必须先于选择块读入：变异成「先烘死 X 再判断」会让每族都按抓帧常量摆放，
    # 而所有“采集有效/无效”分支的探针都还在 ⇒ 只能靠位置关系抓出来。
    baked_decl = _strip_hlsl_comments("float4 attach_x0 = himi_attach_range[4];").strip()
    version_gate = _strip_hlsl_comments("if (asuint(attach_range.z) >= 4u)").strip()
    if code.count(baked_decl) != 1:
        problems.append(
            "【t39-X / t74】兜底 X 必须**恰好一次**读入（float4 attach_x0/1/2 = himi_attach_range[4..6]），"
            f"实际出现 {code.count(baked_decl)} 次"
        )
    else:
        gate_index = code.find(version_gate)
        # 版本门本身缺失时已由探针报出；这里只判位置关系。
        if gate_index >= 0 and code.index(baked_decl) > gate_index:
            problems.append(
                "【t74】兜底 X 必须在运行期选择块**之前**读入：否则 (b)/(c) 分支还没算完就已被抓帧常量定死"
            )
    # (b) 的写表必须三行齐全、且发生在选择分支**之前**：只写一两行、或写进 (a) 分支里，
    # 宿主族就读不到本族的 M_base（探针只查「第一行在不在」，行数与位置只能靠这里抓）。
    write_rows = (
        "himi_object_matrix[own_row + 0u] = own_object.r0;",
        "himi_object_matrix[own_row + 1u] = own_object.r1;",
        "himi_object_matrix[own_row + 2u] = own_object.r2;",
    )
    identity_branch = _strip_hlsl_comments("if (own_index == base_index)").strip()
    for row in write_rows:
        token = _strip_hlsl_comments(row).strip()
        count = code.count(token)
        if count != 1:
            problems.append(
                "【t74】(b) 写表必须恰好一次且三行齐全（" + token + "），实际出现 "
                + str(count) + " 次 —— 少写一行宿主族只读到半截物体矩阵"
            )
        elif identity_branch in code and code.index(token) > code.index(identity_branch):
            problems.append(
                "【t74】(b) 写表必须在 (a) own==base 分支**之前**：写进分支里时"
                "采集有效但 own==base 的那些帧永远填不了表"
            )
    # (c) 必须把 own 段三行读回来：少一行等于采集无效时拿不到本族矩阵，只能退回抓帧常量。
    # base 段的读回行在 (b)/(c) 各出现一次，所以这里只钉 own。
    for row in (
        "float4 own0 = himi_object_matrix[own_row + 0u];",
        "float4 own1 = himi_object_matrix[own_row + 1u];",
        "float4 own2 = himi_object_matrix[own_row + 2u];",
    ):
        token = _strip_hlsl_comments(row).strip()
        count = code.count(token)
        if count != 1:
            problems.append(
                "【t74】(c) 采集无效时必须读回表里本族的三行（" + token + "），实际出现 "
                + str(count) + " 次"
            )
    # 与 P∘Q 同款：把表达式**解析成模型**（himi_runtime.attach_x_model_from_hlsl），
    # 文本一被写坏（少一行、平移项漏掉）这里就报错，同一份模型在测试里与 mat3x4_compose 数值对齐。
    x_model = himi_runtime.attach_x_model_from_hlsl(text)
    for problem in x_model.get("problems") or ():
        problems.append("【t39-X】" + str(problem))
    return problems


def merged_attach_borrow_problems(text: str) -> list:
    """【t91】借用槽写入块的结构自检（与 :func:`merged_attach_hlsl_problems` 分开）。

    为什么单独一个函数：借用槽是**可选**能力（本族没有跨族引用时借用表只有一行哨兵、整块
    不生效），把它并进主判据会让「旧 CS 文本」通不过 —— 主判据还被历史产物/测试文本复用。

    钉的都是「改坏就会**静默写出错位的骨**」的点：

    1. 借用行的来源是 ``himi_borrow_map``（每族一张表、绑在本族 dispatch 的 cs-t4）；
       载荷 ``himi_attach_range`` 里**不该**再有借用行（`[8u` 出现即说明旧机制漏删）；
    2. 行三列语义与导出侧一致：``.x`` 目标槽、``.y`` 来源族序号、``.z`` 源局部槽；
       ``.x == 0xFFFFFFFF`` 是空行哨兵，必须先判掉再取其他分量；
    3. 分支入口是 ``bone_index >= source_slots``（本族 palette 维度），且**必须早于**
       ``if (base_slot == 0xFFFFFFFFu)`` 与 span 截断 —— 借用槽就在块尾，晚判就被截掉，
       块尾那几个槽永远没人写（= 静默黑掉/错位的骨）；
    4. 骨的值必须取**来源族**的 palette（``himi_borrow_palette_*[borrow_src …]``），
       拿本族 palette 就是原来的错解（P_本族 用在别人的骨上）；
    5. X_源 = P_本族[p] · P_来源[q]⁻¹（**own 在前**）必须每帧从**两支活 palette** 现算：
       骨对（p/q = 同一根物理骨在两族 palette 里的局部槽）只以**索引表**形式出现
       （``HIMI_BORROW_PAIR_OWN`` / ``HIMI_BORROW_PAIR_SRC``），矩阵一律每帧现算 ——
       载荷里那支 X 是**宿主族**的，回头读它或拿它的逆都是错口径，把矩阵烘死更是原罪；
    6. 借用表行数必须来自导出期常量（``HIMI_BORROW_ROWS``）：1 分量 typed 视图配 4 分量
       HLSL 元素会让 ``GetDimensions`` 只回不到 1/4 的行数（实机 216 → 60），出现
       ``himi_borrow_map.GetDimensions(`` 即判红；
    7. 骨架侧越界保护必须**先于**写入。
    """
    code = _strip_hlsl_comments(text)
    problems: list = []
    for token in (
        "uint4 borrow_entry = himi_borrow_map[borrow_index];",
        "if (borrow_entry.x == 0xFFFFFFFFu)",
        "uint borrow_rank = borrow_entry.y;",
        "uint borrow_dst = borrow_entry.x * 3u;",
        "uint borrow_src = borrow_entry.z * 3u;",
        "if (bone_index >= source_slots)",
        "himi_borrow_palette_a[borrow_src + 0u]",
        "if (borrow_dst + 2u >= skeleton_elements)",
        "uint borrow_elements = HIMI_BORROW_ROWS;",
    ):
        if token not in code:
            problems.append(f"【t91】借用槽块缺少必要语句（{token}）")
    # 旧机制（载荷里的借用行）必须彻底消失：留着的话 CS 会读一段永远不存在的载荷
    if "himi_attach_range[8u" in code:
        problems.append(
            "【t91】载荷里不该再有借用行（himi_attach_range[8u …] 出现在 CS 里）"
            "⇒ 借用关系已改由 himi_borrow_map 承载"
        )
    # 哨兵判断必须先于取 .y/.z（否则空行会把 0xFFFFFFFF 当地址用）
    sentinel = "if (borrow_entry.x == 0xFFFFFFFFu)"
    rank_read = "uint borrow_rank = borrow_entry.y;"
    if sentinel in code and rank_read in code and code.index(sentinel) > code.index(rank_read):
        problems.append("【t91】借用表的空行哨兵（borrow_entry.x == 0xFFFFFFFFu）必须判在取 .y/.z 之前")
    # 入口位置：借用分支必须早于族未识别与 span 截断
    entry = "if (bone_index >= source_slots)"
    for later in ("if (base_slot == 0xFFFFFFFFu)", "if (span_slots > 0u && bone_index >= span_slots)"):
        if entry in code and later in code and code.index(entry) > code.index(later):
            problems.append(
                f"【t91】借用槽分支必须早于 {later}（否则块尾的借用槽会被当成越界截掉）"
            )
    # 越界保护必须先于写入
    guard = "if (borrow_dst + 2u >= skeleton_elements)"
    write_token = "[borrow_dst + 0u]"
    if guard in code and write_token in code and code.index(guard) > code.index(write_token):
        problems.append("【t91】借用槽的骨架侧越界保护必须在写入**之前**（否则会写坏别人的槽）")
    # 行数必须来自导出期常量：反例是退回运行期读维度（typed 视图与 uint4 元素不一致时
    # GetDimensions 只回不到 1/4 的行数），那样表里后半段的行永远不执行。
    if "himi_borrow_map.GetDimensions(" in code:
        problems.append(
            "【t91】借用表的行数必须来自导出期常量（uint borrow_elements = HIMI_BORROW_ROWS;）——"
            "出现 himi_borrow_map.GetDimensions( 即说明退回运行期读维度（跨显卡未定义区，"
            "实机 216 行只回 60 行）"
        )
    # 骨对的两支矩阵：本族在 cs-t0（himi_src_palette），来源族在按 rank 选的副本里
    for token, detail in (
        ("himi_src_palette[borrow_pair_own * 3u + 0u]", "本族那一支（cs-t0 的 palette）"),
        ("[borrow_pair_src * 3u + 0u]", "来源族那一支（按 rank 选的 palette 副本）"),
    ):
        if token not in code:
            problems.append(f"【t91】X_源 必须从两支**活 palette** 取矩阵，缺 {detail}（{token}）")
    # X_源 必须每帧现算、且只算一次（own 在前：P_本族[p]·P_来源[q]⁻¹）
    x_runtime = code.count("himi_affine_mul(borrow_m_own, himi_affine_inverse(borrow_m_src))")
    if x_runtime != 1:
        problems.append(
            "【t91】X_源 = P_本族[p]·P_来源[q]⁻¹ 必须每帧现算**恰好一次**"
            f"（himi_affine_mul(borrow_m_own, himi_affine_inverse(borrow_m_src))），实际 {x_runtime} 次"
        )
    # 旧口径（拿宿主族物体矩阵的逆合成 X）必须彻底消失：表恒零时它会静默退回单位阵
    if "borrow_m_base" in code or "borrow_base_row" in code or "borrow_src_row" in code:
        problems.append(
            "【t91】借用槽的 X_源 不该再读 himi_object_matrix（borrow_m_base / borrow_base_row /"
            " borrow_src_row 出现了）——那张表的来源在真机上不是物体矩阵，X 会恒退回单位阵"
        )
    return problems


def _strip_hlsl_comments(text: str) -> str:
    """去掉 HLSL 的 ``//`` 行注释与 ``/* */`` 块注释后再判结构。

    必须先 strip：抬头**故意**写着「不要用 StructuredBuffer」「绝不读 IniParams」这类
    **反例说明**，对全文做子串搜索会把注释里的字样当成真实代码。
    """
    without_block = re.sub(r"/\*.*?\*/", "", str(text or ""), flags=re.S)
    return re.sub(r"//[^\n]*", "", without_block)


# --------------------------------------------------------------------------- #
# 【F6】基址表 ← 本次 plan.family_ranges（**唯一权威来源**）
#
# 生成器在常量区（:func:`merged_attach_base_table_lines`，紧跟参考样例），这里只剩
# 「从渲染好的 CS 里抠出表」与「表 vs plan 的审计」两件事。
# --------------------------------------------------------------------------- #


def merged_attach_base_table_rows(table_lines=None) -> list:
    """把基址表行解析成 ``[(slots, base, family), …]``（**只读**，供 F6 硬门与测试用）。

    ``table_lines`` 缺省 = 参考角色样例（``MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES``）；
    生产路径的表由 :func:`merged_attach_base_table_lines` 按 plan 生成后传进来。
    """
    lines = (
        MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES
        if table_lines is None
        else tuple(table_lines)
    )
    rows: list = []
    for line in lines:
        match = _BASE_TABLE_ROW_RE.search(str(line))
        if not match:
            continue
        comment = str(line).split("//", 1)[1].strip() if "//" in str(line) else ""
        rows.append((int(match.group(1)), int(match.group(2)), comment))
    return rows


def merged_attach_base_table_audit(ranges, slot_count: int = 0, table_lines=None) -> dict:
    """【F6】**CS 里那张基址表** vs 本次 ``plan.family_ranges`` 的审计：``{"problems", "notices"}``。

    比对对象是「**实际渲染进 CS 的那张表**」，不是任何常量：

    * ``table_lines`` 缺省 ⇒ 按本次 ``ranges`` 生成（= 导出侧要渲染的那张表）⇒ 审计退化为
      **自洽**判据：它能抓的只剩「同一源槽位数出现在两族」（运行期只按源槽位数查表
      ⇒ 后写的覆盖先写的）与「表行形状被写坏」；
    * ``table_lines`` 显式传入 ⇒ 审计一张**外来的表**（部署里已经躺着的那份 CS、或测试造的
      陈旧表）。这时才可能出现「同 span 不同 base」的真矛盾。

    为什么要两级（t4 口径保留）：

    * 运行期的权威来源是身份资源（``cs-t1`` = 每族 16 字节的 ``(base, span)``），所以
      「plan 的 span 不在表里」**不等于会消失**，不该 Fatal —— 把它当 Fatal 会让 HIMI
      只对某一个角色可用（换角色必然中止导出），方向反了；
    * 真正危险的是**表与 plan 直接矛盾**（同一个 span 在表里映射到别的 base）⇒ 运行期
      ``base_slot != identity_base`` 会**整族拒写**（fail-closed，安全但不完整）。

    ``problems``（⇒ 调用方 Fatal）：表行无法解析 / 表内重复 span / **同 span 不同 base**。
    ``notices``（⇒ 打印，不阻断）：plan 的 span 表里没有（身份资源兜住）/ 表里有 plan 里
    没有的行 / 表的 span 之和 ≠ 本次骨架槽位数。**notices 绝不静默**。
    """
    problems: list = []
    notices: list = []
    lines = merged_attach_base_table_lines(ranges) if table_lines is None else list(table_lines)
    rows = merged_attach_base_table_rows(lines)
    if len(rows) != len(lines):
        problems.append(
            f"基址表有 {len(lines) - len(rows)} 行无法解析"
            "（形状必须仍是 `if (source_slots == <n>u) { base_slot = <m>u; }`）"
        )
    spans: dict = {}
    for slots, base, family in rows:
        if slots in spans:
            problems.append(
                f"基址表出现重复的源槽位数 {slots}（{spans[slots][1]} 与 {family}）⇒ "
                "两族会撞同一个基址，后写的覆盖先写的"
            )
        spans[slots] = (base, family)
    wanted: dict = {}
    for family, info in dict(ranges or {}).items():
        info = dict(info or {})
        if not bool(info.get("own_block", False)):
            continue
        count = int(info.get("slots", 0) or 0)
        if count <= 0:
            continue
        wanted[str(family)] = (count, int(info.get("base", 0) or 0))
    for family, (count, base) in sorted(wanted.items()):
        entry = spans.get(count)
        if entry is None:
            notices.append(
                f"基址表没有 {family} 的映射（本族槽位数 {count}、基址 {base}）："
                "运行期不依赖这张表就能写（cs-t1 身份资源是从本次 plan 生成的），"
                "但表因此只作交叉核对"
            )
            continue
        table_base, table_family = entry
        if table_base != base:
            problems.append(
                f"基址表与本次排布**直接矛盾**：{family} 槽位数 {count} 在表里映射到基址 "
                f"{table_base}（表内标注 {table_family or '?'}），而 plan 给的是 {base} ⇒ "
                "运行期该族会拒写（宁可一根骨都不写，也不写进别的族的槽块）"
            )
    for slots, (base, family) in sorted(spans.items()):
        if slots in {count for count, _ in wanted.values()}:
            continue
        notices.append(
            f"基址表多出一行（槽位数 {slots} → 基址 {base}，标注 {family or '?'}）："
            "本次导出的家族区间里没有这个 span（这张表不是按本次 plan 生成的？）"
        )
    total = int(slot_count or 0)
    if total > 0 and rows:
        table_total = sum(slots for slots, _base, _family in rows)
        if table_total != total:
            notices.append(
                f"基址表 span 之和 {table_total} ≠ 本次骨架槽位数 {total}"
                f"（差 {total - table_total} 个槽没有写方：身份载荷的 span 是历史子集时正常）"
            )
    return {"problems": problems, "notices": notices}


def merged_attach_base_table_problems(ranges, slot_count: int = 0, table_lines=None) -> list:
    """【F6 硬门】只返回**必须 Fatal** 的矛盾项（= :func:`merged_attach_base_table_audit` 的
    ``problems``）；诊断项在 ``notices`` 里单独取（见该函数的「为什么必须两级」）。"""
    return list(
        merged_attach_base_table_audit(ranges, slot_count, table_lines).get("problems") or ()
    )


def merged_deform_key_problems(rebinds) -> list:
    """【K4 / t13 收编】合并 deform 的**触发键**必须一族一个（计划内部不得 1→多）。

    背景（t13 逐帧实测 + 队长的裁定）：合并 deform 的段是
    ``[TextureOverride_VB_<宿主族>_<alias>_Blend]``，键 = ``hash = <CategoryHash.Blend>``；
    ``draw = <行数>, 0`` 用的是**当前绑定的管线状态（含 SO 目标）**。若同一个 Blend 键落在
    **两个族**上，重放理论上可能在别的族的变形段上触发 ⇒ 合并几何被写进那个族的 SO 目标、
    而宿主族的 SO 当帧没有写方。

    **t13 实测：4 帧里绑定 ``4349c385`` 的 draw 各只 1 笔且是同一笔 ⇒ 实际无歧义**
    （1→多只是理论风险）⇒ 本判据**只当诊断**（``self._himi_notify(warning=True)``），
    不阻断导出（"不要为杞人忧天写守卫"）。判据本身仍要留：它是 K4 的**导出期**那一半。

    ⚠ 边界：只能证明**计划内部**没有两族共用同一个 Blend 键；「游戏里是否只有本族的变形段
    绑定它」属 dump 侧（K1/K3），见 ``reports/himi-merge/51-fix-implementation.md`` §8 U7。
    """
    by_hash: dict = {}
    by_family: dict = {}
    for item in rebinds or ():
        record = dict(item or {})
        family = str(record.get("family") or record.get("draw_ib") or "").strip()
        value = str(record.get("category_hash") or "").strip()
        if not family or not value:
            continue
        by_family.setdefault(family, value)
        by_hash.setdefault(value.lower(), set()).add(family)
    problems: list = []
    for value, families in sorted(by_hash.items()):
        if len(families) < 2:
            continue
        problems.append(
            f"合并 deform 的触发键 `hash = {value}` 被 {len(families)} 个族共用"
            f"（{', '.join(sorted(families))}）⇒ 理论上重放可能在**别的族**的变形段上触发"
            "（t13 实测 4 帧内无实际歧义，故此处只作诊断）"
        )
    return problems


#: 【K2】变形 pass 的抽取方式：跨族合并要求它是**非索引 pointlist Draw**（t11 §2 实测
#: ``hash = ea05602f`` 只在 deform pass 触发、pointlist count=3789）；渲染类别（Texcoord 等）
#: 本来就是 trianglelist，**不参与**这条判据。
DEFORM_POINTLIST_CATEGORIES = ("Position", "Blend")
#: 变形 pass 的结构判据要求的抽取方式（工作空间 json 的 ``ExtractTechnique``）。
DEFORM_POINTLIST_TECHNIQUE = "pointlist"


def deform_draw_structure_problems(payload, *, unique_str: str = "") -> list:
    """【K2 导出期那一半】宿主/成员部件的 Position/Blend 必须是 ``pointlist`` 抽取。

    为什么能在这条链上判：跨族合并把「一族一个 deform pass」重放成 ``draw = <行数>, 0``，
    其语义前提是该 pass 是**非索引 pointlist Draw**（带 SO 目标）。工作空间 json 的
    ``CategoryBufferList[*].D3D11ElementList[*].ExtractTechnique`` 就是导入侧记录的抽取方式
    （实测本角色：Position 4 个元素 + Blend 2 个元素全 = ``pointlist``，Texcoord = ``trianglelist``）
    ⇒ 若哪一族不是 pointlist，重放的语义就不成立，必须**在导出期**说出来而不是进游戏才发现。

    ``payload`` = 该组件 json 的 dict。返回问题清单（空 = 通过）。
    """
    problems: list = []
    payload = dict(payload or {})
    if not payload:
        return problems  # 无 json ⇒ unavailable，不阻断
    label = f"{unique_str}: " if unique_str else ""
    seen_categories: set = set()
    for buffer in payload.get("CategoryBufferList") or ():
        if not isinstance(buffer, dict):
            continue
        for element in buffer.get("D3D11ElementList") or ():
            if not isinstance(element, dict):
                continue
            category = str(element.get("Category", "") or "")
            if category not in DEFORM_POINTLIST_CATEGORIES:
                continue
            seen_categories.add(category)
            technique = str(element.get("ExtractTechnique", "") or "")
            if technique != DEFORM_POINTLIST_TECHNIQUE:
                problems.append(
                    f"{label}{category} 的抽取方式是 `{technique or '(空)'}`，而变形 pass 必须是"
                    f" `{DEFORM_POINTLIST_TECHNIQUE}`（非索引 Draw + SO 目标）⇒ "
                    "`draw = <行数>, 0` 的重放语义不成立"
                )
    missing = [cat for cat in DEFORM_POINTLIST_CATEGORIES if cat not in seen_categories]
    if seen_categories and missing:
        problems.append(
            f"{label}json 里缺 {'/'.join(missing)} 类别的元素记录 ⇒ 无法核对变形 pass 结构"
        )
    return problems


def deform_draw_structure_judged(payload) -> bool:
    """【K2 / t18】这份 json 是否**真的**包含可判的 Position/Blend 元素记录。

    为什么需要它（t5 的连带问题）：`deform_draw_structure_problems()` 返回空既可能表示
    「判过且合格」，也可能表示「根本没有可判的东西」—— 两者不能都算「通过」。
    本函数让调用方能分开计数（`checked` vs `unjudgeable`），
    否则「没有报错」仍然可以被当成「检查过了」。
    """
    for buffer in dict(payload or {}).get("CategoryBufferList") or ():
        if not isinstance(buffer, dict):
            continue
        for element in buffer.get("D3D11ElementList") or ():
            if not isinstance(element, dict):
                continue
            if str(element.get("Category", "") or "") in DEFORM_POINTLIST_CATEGORIES:
                return True
    return False


def product_ini_section_problems(ini_text: str) -> list:
    """【t39 / §13.17 教训】产物 ini 的**逐段解析自检**：任何段"属性为 0 条"即失败。

    为什么要有它：2026-09-26 出过一次事故 —— 往 ini 插新段时**把段头插在了别的段的属性行之间**，
    而 INI 的段**持续到下一个 ``[``** ⇒ 被截断的那一段（骨架资源）属性被清空、整段失效。
    这类事故在"看产物"时不一定看得见（文件还在、行还在），但资源已经废了。

    判据（只读、纯函数）：
    * 每个 ``[…]`` 段头之后，到下一个个段头之前，**至少有一条 ``key = value`` 属性行**
      （``[Constants]`` 例外：它可以只有 ``global $x = 0`` 声明行，同样算属性）；
    * **例外：纯注释段放行** —— 段内一行实质内容都没有、但**有 ``;`` 注释行**时不算失败。
      理由是这类段完全无害（3DMigoto 对 ``;`` 开头的行整行跳过），而它确实有存在目的：
      ``M_IniHelper._report_standard_route_baked_shapekeys`` 就是把「标准路线烘焙掉了形态键」
      的诊断注释挂在 ``[Present]`` 段上，好让告警在产物里可见（2026-09-27 实测：
      这条 R-A 诊断会稳定触发，若一并判死就等于「有告警就导不出」）。
      区分方式：**真被截断的段是「既无属性、也无注释」**（新段头直接顶掉旧段），
      而纯注释段至少有注释行 —— 前者仍然报错。
    * 不允许出现"属性行紧跟在段头之前、却没有归属段"的悬空属性（即文件开头就是 ``key = value``）；
    * 空段（``[]``）直接报错。
    """
    problems: list = []
    lines = str(ini_text or "").splitlines()
    current = ""
    attrs = 0
    comments = 0
    start_line = 0
    for index, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            if current:
                comments += 1
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if current:
                if attrs == 0 and comments == 0:
                    problems.append(
                        f"第 {start_line} 行的段 [{current}] 属性为 0 条（段头插错位置/被截断？）"
                    )
            elif attrs:
                problems.append(
                    f"第 {index} 行之前有 {attrs} 条属性没有归属段（段头缺失或被顶掉）"
                )
            if not name:
                problems.append(f"第 {index} 行是空段头 `[]`")
            current = name
            start_line = index
            attrs = 0
            comments = 0
            continue
        if "=" in line:
            attrs += 1
    if current and attrs == 0 and comments == 0:
        problems.append(f"第 {start_line} 行的段 [{current}] 属性为 0 条（段头插错位置/被截断？）")
    return problems


def verify_attach_range_references(ini_text: str, mod_root: str) -> list:
    """【F5 硬门】attach 段的 ``cs-t1 = ref ResourceHI3AttachRange_<fam>`` 必须有**真实文件**。

    与 :func:`verify_cs_references` 同款：返回问题清单（空 = 通过），调用方 ``Fatal``。
    身份资源绑空 ⇒ CS 退回冻结表（不是静默乱写），但那样 F5 的保护就没了 ⇒ 必须硬拦。
    """
    text = str(ini_text or "")
    root = os.path.abspath(str(mod_root or "").strip() or ".")
    declared: dict = {}
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current.startswith(himi_runtime.ATTACH_RANGE_RESOURCE_PREFIX) and line.lower().startswith(
            "filename"
        ):
            declared[current] = line.split("=", 1)[1].strip() if "=" in line else ""
    problems: list = []
    prefix = himi_runtime.ATTACH_RANGE_RESOURCE_PREFIX
    # 只认身份资源：`[CustomShaderHI3MergedSkin_*]` 段（t100 起没有任何 run，段仍保留）里的
    # `cs-t1 = ref Resource<fam>Blend` 绑的是 VB 缓冲，不是身份载荷；把它算进来会让这条门在
    # 每次导出都假报「没有 [Resource<fam>Blend] 声明」⇒ 契约降级成普通导出（t109）。
    referenced = [
        name
        for name in (
            match.group(1)
            for match in re.finditer(
                r"^\s*cs-t1\s*=\s*ref\s+(\S+)", text, flags=re.IGNORECASE | re.MULTILINE
            )
        )
        if name.startswith(prefix)
    ]
    if not referenced:
        problems.append(
            "整份 ini 里没有任何 `cs-t1 = ref ResourceHI3AttachRange_*`（F5 的本族身份没有绑定）"
        )
    for name in referenced:
        if name not in declared:
            problems.append(f"`cs-t1 = ref {name}` 没有对应的 [{name}] 资源声明")
            continue
        value = declared[name]
        if not value:
            problems.append(f"[{name}] 的 filename 为空（身份资源必须指向真实文件）")
            continue
        normalised = value.replace("\\", "/")
        relative = normalised[2:] if normalised.startswith("./") else normalised
        candidate = os.path.abspath(os.path.join(root, os.path.normpath(relative)))
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside = False
        if not inside:
            problems.append(f"[{name}] 的 filename 越出 mod 目录：{value}")
            continue
        if not os.path.isfile(candidate):
            # 与 `verify_cs_references` 的「引用的文件不存在」保持同一措辞（用户看到的
            # 应当是同一句话的两个入口；t20/T6-F2 的回归用例也按这句断言）。
            problems.append(
                f"[{name}] 的 filename 引用的文件不存在（{candidate}）"
            )
            continue
        # 【t39】长度硬门：v2 的身份载荷必须带 Q_f（64 B；t74 起为 128 B）。16 B 的旧载荷在 CS 里
        # 会被版本门挡掉（不做校准）——那意味着**顶点侧校准了、palette 侧没校准**，
        # 两者必须同源同报 ⇒ 宁可 Fatal，也不产出半校准的产物。
        try:
            size = os.path.getsize(candidate)
        except OSError as exc:
            problems.append(f"[{name}] 的 filename 无法取大小（{exc}）")
            continue
        if size != himi_runtime.ATTACH_RANGE_BYTES:
            problems.append(
                f"[{name}] 的身份载荷长度是 {size} B，而 v{himi_runtime.ATTACH_RANGE_VERSION}"
                f" 要求 {himi_runtime.ATTACH_RANGE_BYTES} B"
                "（头部 + 单位阵 3×4 + X_f 3×4 + 族索引 [7]）⇒ "
                "CS 读不到 X_f / 族索引（短载荷会让 himi_attach_range[4..7] 越界），必须重新导出"
            )
    return problems


def verify_borrow_map_references(ini_text: str, mod_root: str) -> list:
    """【t91 硬门】每张 ``[ResourceHI3BorrowMap_<fam>]`` 都必须有真实文件，且长度 = ``array × 16``。

    为什么也要硬拦：借用表是**唯一**告诉 CS「块尾那几个槽该写谁」的地方。表缺了 ⇒ CS 侧
    ``GetDimensions`` 拿到 0 行 ⇒ 块尾借用槽**永远没人写**（deform 读零 ⇒ 借用骨塌成一团），
    而 ini 里其他东西全都合法 ⇒ 只能靠这条门在导出期拦住。长度也要对：``array`` 是 ini 声明的
    行数，D3D 侧的视图就按它给维度，文件比它短 ⇒ CS 读到 0 行外的东西（同样全部空缺）。

    与 :func:`verify_attach_range_references` 分开：身份资源恒 128 B，借用表长度随借用骨数变。
    """
    text = str(ini_text or "")
    root = os.path.abspath(str(mod_root or "").strip() or ".")
    # 段名 → (filename, array)
    declared: dict = {}
    current = ""
    current_fields: dict = {}
    prefix = himi_runtime.BORROW_MAP_RESOURCE_PREFIX

    def flush():
        if current.startswith(prefix):
            declared[current] = dict(current_fields)

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            flush()
            current = line[1:-1].strip()
            current_fields = {}
            continue
        if "=" in line and not line.startswith(";"):
            key, value = line.split("=", 1)
            current_fields[key.strip().lower()] = value.strip()
    flush()
    problems: list = []
    if not declared:
        return problems  # 没有借用表声明的产物（未触发借用）不是错误
    referenced = {
        match.group(1).lower()
        for match in re.finditer(r"^\s*cs-t4\s*=\s*ref\s+(\S+)", text, flags=re.IGNORECASE | re.MULTILINE)
    }
    for name, fields in sorted(declared.items()):
        if name.lower() not in referenced:
            problems.append(f"[{name}] 声明了却没有任何 `cs-t4 = ref {name}`（借用表没有绑上 CS）")
        value = fields.get("filename", "")
        if not value:
            problems.append(f"[{name}] 的 filename 为空（借用表必须指向真实文件）")
            continue
        normalised = value.replace("\\", "/")
        relative = normalised[2:] if normalised.startswith("./") else normalised
        candidate = os.path.abspath(os.path.join(root, os.path.normpath(relative)))
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside = False
        if not inside:
            problems.append(f"[{name}] 的 filename 越出 mod 目录：{value}")
            continue
        if not os.path.isfile(candidate):
            problems.append(f"[{name}] 的 filename 引用的文件不存在（{candidate}）")
            continue
        try:
            rows = int(fields.get("array", "") or 0)
        except (TypeError, ValueError):
            rows = 0
        if rows <= 0:
            problems.append(f"[{name}] 的 array 必须是正整数（当前 {fields.get('array', '')!r}）")
            continue
        try:
            size = os.path.getsize(candidate)
        except OSError as exc:
            problems.append(f"[{name}] 的 filename 无法取大小（{exc}）")
            continue
        expected = himi_runtime.BORROW_MAP_ROW_BYTES * rows
        if size != expected:
            problems.append(
                f"[{name}] 的借用表长度是 {size} B，而 ini 声明 array = {rows}"
                f"（每行 {himi_runtime.BORROW_MAP_ROW_BYTES} B）⇒ 应为 {expected} B；"
                "长度不符时 CS 的 GetDimensions 与导出期算出的行数不一致，块尾借用槽会空着"
            )
    return problems


# --------------------------------------------------------------------------- #
# 【F1/F2】变形重放必须**无条件**执行（s3 那种守卫永不得回流）
# --------------------------------------------------------------------------- #
#: 条件行前缀（`if` / `else if` / `else` / `endif`）—— 出现在变形重放块里就是坏产物。
_CONDITIONAL_LINE_RE = re.compile(r"^\s*(if|else|endif)\b", re.IGNORECASE)


def deform_replay_block_problems(lines) -> list:
    """Blend 块里的**变形重放**必须无条件执行（纯函数；空 = 通过）。

    根因（报告 50 §4.1 + 队长两帧实测）：该族 deform 段的原 draw 已被 ``handling = skip``
    吞掉 ⇒ SO 目标当帧**唯一的写方**就是 mod 重放的这一笔。守卫一旦包住它（s3 诊断态的
    ``if $seen_… >= 1``），守卫在宿主族段里**恒假** ⇒ SO 全零 ⇒ **整块几何消失**。

    ⇒ 判据：块内不得出现任何条件行；``handling = skip`` 之后必须紧跟一笔
    ``draw = <行数>, 0``；重放行只能有**一笔**（多笔 = 越界写 SO 池）。

    为什么是**导出期**判据而不是运行期守卫：守卫只能放在「骨架内容更新」这一层
    （报告 50 §4.2 原则 2），`draw` 本身永远无条件。
    """
    items = [str(line) for line in (lines or ())]
    if not items:
        return []  # 空块不是「变形重放块」（判据只对真块生效，不制造假阳性）
    problems: list = []
    for index, line in enumerate(items, 1):
        if _CONDITIONAL_LINE_RE.match(line):
            problems.append(
                f"第 {index} 行是条件行（`{line.strip()}`）：变形重放块里不得有任何守卫 ——"
                "守卫一旦包住 `draw`，宿主族段里恒假 ⇒ SO 目标无写方 ⇒ 模型整块消失"
                "（s3 实测：消失帧该缓冲 31054 顶点全零）"
            )
    skip_index = None
    draw_rows: list = []
    draw_index = None
    for index, line in enumerate(items, 1):
        stripped = line.strip()
        if stripped.startswith("handling = skip"):
            skip_index = index
        if stripped.startswith("draw = "):
            draw_rows.append(index)
            draw_index = index
    if skip_index is None:
        problems.append("缺 `handling = skip`（不吞掉原 draw ⇒ 原版几何与合并几何重影）")
    if not draw_rows:
        problems.append("缺变形重放行 `draw = <行数>, 0`（SO 目标当帧没有写方 ⇒ 模型消失）")
    elif len(draw_rows) > 1:
        problems.append(
            f"变形重放行有 {len(draw_rows)} 笔（第 {draw_rows} 行）：只允许一笔"
            "（多笔会把角色级 SO 池写爆）"
        )
    if skip_index is not None and draw_index is not None and draw_index < skip_index:
        problems.append(
            f"`draw`（第 {draw_index} 行）排在 `handling = skip`（第 {skip_index} 行）之前 ⇒"
            "原 draw 会与重放各写一次 SO（顺序必须是 skip → draw）"
        )
    return problems


def verify_cs_references(ini_text: str, mod_root: str) -> list:
    """**硬门**：ini 里每一条 ``cs =`` 引用都必须能在 mod 目录解析到真实文件。

    返回**问题清单**（空 = 通过）。调用方（:meth:`ExportHIMI.generate_unity_vs_config_ini`）
    在非空时 ``raise Fatal``（**不降级为 warning、不跳过**）。

    规则：

    * 逐条匹配 ``^\\s*cs\\s*=\\s*(\\S+)``（3DMigoto 的键名大小写不敏感）；
    * 相对路径从 **ini 自身所在目录（= mod 根）** 算起：``./res/x.hlsl`` → ``<mod_root>/res/x.hlsl``；
    * **拒绝 `../` 越界**（解析后必须仍在 ``mod_root`` 之内）；
    * 有值但文件不存在 ⇒ 报「引用不存在」；
    * 值为空 ⇒ 报「空引用」；
    * 文件存在但不是普通文件（目录/符号链接断链）⇒ 报「不是普通文件」。
    """
    problems: list = []
    root = os.path.abspath(str(mod_root or "").strip() or ".")
    text = str(ini_text or "")
    seen: list = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        match = _CS_REFERENCE_RE.match(raw)
        if not match:
            continue
        value = match.group(1).strip()
        if not value:
            problems.append(f"第 {lineno} 行：`cs =` 是空引用（必须指向一个真实文件）")
            continue
        seen.append(value)
        if value.startswith("\\\\") or value.startswith("//"):
            # 网络路径 / URL：3DMigoto 的 ini 只接受本地相对或绝对路径，这里一律拒绝，
            # 否则「解析到真实文件」这条判据会被绕开。
            problems.append(f"第 {lineno} 行：`cs = {value}` 不是本地相对路径")
            continue
        normalised = value.replace("\\", "/")
        if os.path.isabs(normalised) or re.match(r"^[A-Za-z]:", normalised):
            candidate = os.path.abspath(normalised)
        else:
            # 相对路径从 **ini 自身所在目录（= mod 根）** 算起；`normpath` 先把 `../` 收掉，
            # 再用 abspath（**不要**先 abspath 再 join —— 那会把 `..` 当成普通文件名）。
            relative = normalised[2:] if normalised.startswith("./") else normalised
            candidate = os.path.abspath(os.path.join(root, os.path.normpath(relative)))
        try:
            inside = os.path.commonpath([root, candidate]) == root
        except ValueError:
            inside = False
        if not inside:
            problems.append(
                f"第 {lineno} 行：`cs = {value}` 越出 mod 目录（`../` 越界）⇒ 拒绝"
            )
            continue
        if not os.path.exists(candidate):
            problems.append(f"第 {lineno} 行：`cs = {value}` 引用的文件不存在（{candidate}）")
            continue
        if not os.path.isfile(candidate):
            problems.append(f"第 {lineno} 行：`cs = {value}` 不是普通文件（{candidate}）")
    if not seen:
        problems.append("整份 ini 里没有任何 `cs =` 引用（合并骨架的每帧填充没有写方）")
    return problems


def merged_attach_reference_problems(cs_paths) -> list:
    """**单文件口径**：全部 ``cs =`` 必须指向**同一个**路径。

    防止实现回退成「每族一份」——那时 5 条 `cs =` 会是 5 个不同路径（或者只剩一条），
    两种都必须报错。``cs_paths`` 是 `cs =` 右边的值序列。
    """
    values = [str(item or "").strip() for item in (cs_paths or ())]
    values = [item for item in values if item]
    if not values:
        return []
    uniq = sorted(set(values))
    if len(uniq) > 1:
        return [
            "`cs =` 引用不是同一个文件（合并成一个 shader 后 5 个族段必须共用一份）："
            + "、".join(uniq)
        ]
    return []



def blend_block_order_problems(lines, *, expect_capture=None) -> list:
    """**源码级顺序护栏**：Blend 块内「捕获 → run → 重绑」的相对顺序。

    每帧机制的整条链只在这三行的顺序上成立（``ResourceHI3PaletteFam_<fam>`` 逐族一个）：

    .. code-block:: ini

       ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null   ; ① 捕获当帧 palette
       run = CustomShaderHI3MergedSkeletonAttach_<fam>        ; ② 块搬运进合并骨架
       vs-t0 = ResourceHI3MergedSkeleton_<role_key>           ; ③ 之后才重绑

    * 捕获排在 ③ 之后 ⇒ 捕到的是**自己刚绑上去的骨架**（自反馈：骨架被写成上一帧的自己，
      而游戏当帧 palette 永远进不了骨架 ⇒ 「骨骼动画正常但各部件整体错位」）；
    * ``run`` 排在 ③ 之后 ⇒ CS 读的是重绑后的骨架而不是当帧 palette（同样错）。
    * 方向同样致命：3DMigoto 的 ``copy`` 是**赋值式**，LHS 是**目标** —— 写成
      ``copy vs-t0 = 副本`` 会把空副本写进游戏当帧 palette（比不改更坏）。

    ``expect_capture``：``True`` = 本块**必须**有捕获行（该族有专属块，正常路径）；
    ``False`` = 允许没有（借位族 ``own_block=False``：不发捕获行/attach 段，只重绑）；
    ``None``（默认）= 由产物自行判定 —— 捕获行与 ``run`` 必须**同时出现或同时缺席**
    （只出现一个必然是写漏，必须抓到）。

    纯文本判定（不依赖 ini 解析器）⇒ 可被单测与两态复现直接调用。返回问题描述列表，空 = 通过。
    """
    capture = run = rebind = None
    reversed_direction = None
    for index, raw in enumerate(lines or ()):
        line = str(raw).strip()
        if line.startswith("copy vs-t0"):
            reversed_direction = (index, line)
        if re.match(r"^ResourceHI3PaletteFam_\S+\s*=\s*copy vs-t0\b", line):
            capture = index
        elif re.match(r"^run\s*=\s*" + re.escape(himi_runtime.ATTACH_SECTION_PREFIX), line):
            run = index
        elif line.startswith("vs-t0"):
            rebind = index
    problems: list = []
    if reversed_direction is not None:
        problems.append(
            f"第 {reversed_direction[0]} 行捕获行方向写反（`{reversed_direction[1]}`）——"
            "3DMigoto 的 copy 是赋值式，LHS 必须是**目标**："
            f"应为 `{himi_runtime.family_palette_name('<fam>')} = copy vs-t0 unless_null`"
        )
    if rebind is None:
        return problems
    if expect_capture is None:
        expect_capture = capture is not None or run is not None
    if capture is None:
        if expect_capture:
            problems.append(
                f"Blend 块里没有捕获行（`{himi_runtime.family_palette_name('<fam>')} = "
                "copy vs-t0 unless_null`）⇒ 骨架不会被写入当帧姿态"
            )
        return problems
    if run is None:
        problems.append("捕获行有、但 `run =` attach 段缺失 ⇒ 骨架不会被写入")
    elif run < capture:
        problems.append(
            f"`run`（第 {run} 行）排在捕获行（第 {capture} 行）**之前** ⇒ "
            "CS 读到的是上一次捕获的内容（顺序必须是 捕获 → run）"
        )
    if capture > rebind:
        problems.append(
            f"捕获行（第 {capture} 行）排在 `vs-t0 =` 重绑（第 {rebind} 行）**之后** ⇒ "
            "自反馈：捕到的是刚绑上的骨架"
        )
    if run is not None and run > rebind:
        problems.append(
            f"`run`（第 {run} 行）排在 `vs-t0 =` 重绑（第 {rebind} 行）**之后** ⇒ "
            "CS 读的是重绑后的骨架而不是当帧 palette"
        )
    return problems


def _finite_vec3(a, b, c):
    """三个值 ⇒ float 三元组；任一非有限（源缓冲的 NaN 尾巴）⇒ ``None``（属性不可判）。

    【t24】顶点族判别的查询属性入口：法线（列 3..5）/切线（列 7..9）随行传给
    ``himi_runtime.nearest_family``。属性在索引/查询任一侧缺失或不可判时，
    判别自动停用（nearest_family 落回位置口径），绝不因为属性坏而改变族归属。
    """
    try:
        values = (float(a), float(b), float(c))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    return values


class ExportHIMI(ExportUnity):
    """崩坏3 / HI3 导出器（VA 模式 + 静态合并骨架 + 站位抑制原版 draw）。"""

    def __init__(self, blueprint_model):
        # 站位对象必须在基类组装 drawib/submesh 模型**之前**注入，否则下游
        # SubMeshModel 找不到对应 Blender 对象（计划书 §7.7.5 生命周期）。
        #
        # **顺序对齐 ZZMI**（`ui/universal/zzmi.py:443` 注入 → `:445` super()）：
        # `ui/universal/drawib_export_base.py:11-16` 在它自己的 `__init__` 里**立即**
        # 用 `ExportHelper.parse_drawib_model_list_from_blueprint_model` 把
        # `ordered_draw_obj_data_model_list` 解析成 `drawib_model_list`。若注入发生在
        # super() 之后，站位 DrawCall 只会留在 `ordered` 里而**永远进不了**
        # `drawib_model_list[*].submesh_model_list[*].drawcall_model_list`
        # ⇒ 站位部件的 IB override 段（`handling = skip`）根本不会生成 ⇒ 抑制重影失效。
        self.blueprint_model = blueprint_model
        self._himi_stub_object_names: list = []
        self._himi_stub_draw_calls: list = []
        self._himi_stub_bares: set = set()
        #: 【t106】自动补站位的族（"槽没有写方" 的那批）及其槽号，供诊断/上层读取。
        self._himi_stub_writer_slots: dict = {}
        self._himi_stub_decision = None
        self._himi_plan = None
        self._himi_plan_ready = False
        # 【t98】导出 Blend 里没有写方的合并骨架槽（逐族）⇒ 导出 Fatal，见 _himi_apply_borrow_layout
        self._himi_borrow_orphans = {}
        self._himi_contract_decision = None
        self._himi_resource_emitted = False
        self._himi_publish_result = {}
        self._himi_blend_verdicts = {}
        #: t75：本次导出发射的全部 `cs =` 值（单文件口径下应全部等于
        #: :data:`MERGED_ATTACH_HLSL_PATH`）。供测试/上层读取。
        self._himi_attach_cs_paths = []
        #: t75：`res/himi_attach.hlsl` 的落盘结果（``{"ok","path","written","bytes","message"}``）。
        self._himi_attach_cs_result = {}
        #: 【F5】每族身份资源（``Meshes/<fam>-AttachRange.buf``）的落盘结果。
        self._himi_attach_range_result = {}
        #: 【t18 / T5-F5】导出期诊断（F6 基址表 notice / K4 触发键 / K2 账目）的收集器：
        #: 它们会**同时**进控制台+LOG 与**产物 ini 末尾的注释块**——否则用户不看控制台就会错过。
        self._himi_base_table_notices: list = []
        self._himi_key_notices: list = []
        self._himi_deform_structure_result: dict = {}
        self._himi_diagnostics_emitted = False
        #: 【t85】普通导出时清掉的上一次合并导出残留文件账目（``{"removed","failed"}``）。
        self._himi_merged_residue: dict = {}
        #: 【t39/t78】族级摆放 Q_f 的缓存与问题清单（方案 B 起只用于诊断，不参与任何变换）。
        self._himi_placements_cache = None
        self._himi_placements_problems: list = []
        self._himi_placements_skipped: list = []
        self._himi_calibration_result: dict = {}
        #: 【t49】方案 A/C：旁观文件落盘结果 + 越界引用告警
        self._himi_blend_origin_result: dict = {}
        self._himi_blend_origin_notices: list = []
        try:
            self._himi_inject_stub_objects()
        except Exception:
            # 构造失败时 export() 的 finally 尚未接管；站位对象/网格必须立刻回滚，
            # 否则下一次导出会引用已删除对象（与 ZZMI 同款事务闭合）。
            self._himi_cleanup_stub_objects()
            raise
        # 模型组装（含 `ordered` → `drawib_model_list` 的解析）必须在站位注入之后。
        super().__init__(blueprint_model=blueprint_model)

    # ------------------------------------------------------------------ #
    # 站位（stub）：抑制被吸收部件的原版 draw（计划书 §7.7）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _himi_draw_call_object_name(draw_call) -> str:
        try:
            return str(draw_call.get_blender_obj_name() or "")
        except Exception:  # noqa: BLE001
            return str(getattr(draw_call, "obj_name", "") or "")

    @classmethod
    def _himi_drawcall_object_present(cls, draw_call) -> bool:
        """该 DrawCall 在**场景里**是否真有对象（场景真相，不是蓝图声明）。

        ZZMI t80 §2.3 实测：把「声明了」当 present 会让占位整段跳过，紧接着下游
        ``SubMeshModel`` 按不存在的对象直接 ``Fatal``。因此判据必须是
        ``bpy.data.objects`` 里真能取到那个对象。
        """
        return blender_object_present(cls._himi_draw_call_object_name(draw_call))

    def _himi_ordered_draw_calls(self) -> list:
        return getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None) or []

    @staticmethod
    def _himi_merged_data_present(workspace_root: str) -> bool:
        """工作空间里是否真有合并骨架数据（= 本次导出会产出合并骨架资源的必要前置）。

        判据：`LOD*/<bare>/<WorkGameType>/<bare>.json` 里**至少有一份**带
        `VGMap` 或 `VGCount`。只看「目录名同名的那份 json」（组件 json 的命名契约），
        不盲扫全树，也不解析无关 json。

        不用 `HIMISkeletonMergeHelper.missing_merged_metadata_exist` 的整批语义：
        它需要调用方给出 unique_str 列表，而这里（`__init__` 期）拿不到导出集合。
        """
        root = str(workspace_root or "").strip()
        if not root or not os.path.isdir(root):
            return False
        try:
            lod_names = sorted(os.listdir(root))
        except OSError:
            return False
        for lod_name in lod_names:
            if not (lod_name.startswith("LOD") and lod_name[3:].isdigit()):
                continue
            lod_dir = os.path.join(root, lod_name)
            try:
                bare_names = sorted(os.listdir(lod_dir))
            except OSError:
                continue
            for bare in bare_names:
                bare_dir = os.path.join(lod_dir, bare)
                if not os.path.isdir(bare_dir):
                    continue
                try:
                    game_type_names = sorted(os.listdir(bare_dir))
                except OSError:
                    continue
                for game_type in game_type_names:
                    path = os.path.join(bare_dir, game_type, bare + ".json")
                    if not os.path.isfile(path):
                        continue
                    try:
                        payload = JsonUtils.LoadFromFile(path)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(payload, dict) and (
                        payload.get("VGMap") or int(payload.get("VGCount", 0) or 0) > 0
                    ):
                        return True
        return False

    def _himi_collect_used_group_ids(self) -> set:
        """场景对象实际引用（weight > 0）的数字顶点组 id 集合。

        **必须排除站位对象自己**：否则本轮刚建的站位会反过来成为下一轮的吸收证据
        （自证循环，计划书 §7.7.5「标记的第一个消费点」/ ST9）。
        """
        names = []
        for draw_call in self._himi_ordered_draw_calls():
            if bool(getattr(draw_call, "himi_stub", False)):
                continue
            name = self._himi_draw_call_object_name(draw_call)
            if name:
                names.append(name)
        return blender_collect_used_group_ids(names)

    def _himi_remove_stub_object_data(self, obj) -> None:
        blender_remove_stub_object_data(obj)

    def _himi_purge_stale_stub_state(self) -> set:
        """清理上次异常残留的站位对象/网格/带标记 DrawCall。

        **必须早于 present 集合构造**（计划书 §7.7.5）：残留站位对象名恰好等于某真
        部件身份时，该部件会被误判「场景存在」⇒ 不插桩 ⇒ 随后对象被删 ⇒ 下游
        ``SubMeshModel`` 引用不存在的对象而 ``Fatal``。
        """
        stale_names = blender_purge_stale_stub_objects()
        ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if not bool(getattr(draw_call, "himi_stub", False))
                and self._himi_draw_call_object_name(draw_call) not in stale_names
            ]
        return stale_names

    def _himi_create_stub_object(self, bare: str) -> str:
        """创建占位对象：3 顶点 1 三角面（1e-6 尺度），权重挂在全局槽 0。

        权重组名恒为 ``"0"``：站位对象只有 1 个权重组，导出侧
        ``_prepare_merged_skeleton_vertex_groups`` 会「剔非数字组 → 补缺 → 按名排序 →
        重命名 ``str(index)``」⇒ index 恒 0 ⇒ **不新增任何槽位**（计划书 §7.7.5）。
        """
        return blender_create_stub_object(bare)

    def _himi_inject_stub_objects(self) -> list:
        """按计划书 §7.7.3/§7.7.4 的三条规则注入站位。

        **t107：普通模式（``import_merged_vgmap()`` 关闭）不再整段跳过** —— 三条判定规则
        全部照跑，与开关无关。理由：那个开关管的是「要不要产出合并骨架资源」，而站位的
        全部正当性都在**另一件事**上 —— 用 `handling = skip` 把原版那一笔压掉。规则①
        （部分在场的家族给缺失成员补站位）、规则②④（整族缺席但它的 palette 槽被现存
        对象实际引用 ⇒ 几何被别的 IB 接管）的证据都来自「工作空间 VGMap + 场景顶点组」，
        没有一条依赖合并骨架资源。曾经的整段跳过导致两类漏压都无人接住（用户实测：
        ① 同一 IB 的第二笔 ``4386@57792`` 原版 draw；② 整族被并入头发之后的那个 IB）。

        创建失败**不得静默继续**：站位是「原版 draw 会被压制」的唯一凭据，失败后继续
        导出会得到「几何已合并 + 原版照画」的重影产物；与 ZZMI 同款，用 ``Fatal``
        中止导出（调用方的 ``finally`` 会清理半成品）。
        """
        merged_runtime = bool(GlobalProterties.import_merged_vgmap())
        from ...utils.format_utils import Fatal

        # 自愈必须早于 present 集合构造（残留站位对象会被误判为真实部件）。
        self._himi_purge_stale_stub_state()

        workspace_root = GlobalConfig.path_workspace_folder()
        if merged_runtime and not self._himi_merged_data_present(workspace_root):
            # 前置门（计划书 §7.7.7 S1/S2）：**没有合并数据 ⇒ 没有合并骨架资源 ⇒
            # 站位无从谈起**。HIMI 的站位不承载作用 A（静态骨架），它的唯一效果就是
            # 用 `handling = skip` 压掉原版 draw；在「本次导出根本不产出合并骨架」的
            # 场景下压掉原版 draw 只会让部件凭空空掉。
            #
            # 为什么不用 `_himi_resource_emitted` / `plan.ok` 做门：两者都在
            # `__init__` 之后才成立 —— `_himi_resource_emitted` 只在 INI 生成期置位，
            # 而计划要等模型组装完才能算（`_himi_export_entries` 读
            # `drawib_model_list`）。用它们做门会让**永远**不注入。故此处用
            # 「工作空间是否真有合并数据」这个与导出集合无关、且在 `__init__` 可判的
            # 等价前置条件。
            print(
                f"{_STUB_LOG_PREFIX} 工作空间没有合并骨架数据（VGMap/VGCount）"
                "⇒ 本次导出不产出合并骨架资源 ⇒ 不注入站位"
            )
            return []

        present_bares: set = set()
        for draw_call in self._himi_ordered_draw_calls():
            if bool(getattr(draw_call, "himi_stub", False)):
                continue
            if not self._himi_drawcall_object_present(draw_call):
                continue
            unique_str = ""
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:  # noqa: BLE001
                unique_str = str(getattr(draw_call, "obj_name", "") or "")
            bare = bare_from_unique_str(unique_str)
            if bare:
                present_bares.add(bare)

        # 兼容兜底：一个真实场景对象都解析不到（轻量宿主 / 拿不到 bpy 数据）⇒ 退回
        # 「按声明名判定」= 全部视为存在 ⇒ 不插桩。宁可不插桩，也不要在信息不足时
        # 凭空给全部部件插桩（与 ZZMI 同款兜底）。
        declared_present = {
            bare_from_unique_str(self._himi_draw_call_object_name(draw_call))
            for draw_call in self._himi_ordered_draw_calls()
            if not bool(getattr(draw_call, "himi_stub", False))
        }
        declared_present.discard("")
        scene_present = present_bares if present_bares else declared_present

        family_map = load_component_family_map(workspace_root)
        used_group_ids = self._himi_collect_used_group_ids()
        decision = decide_stub_injection(
            workspace_root=workspace_root,
            scene_present_bares=scene_present,
            family_map=family_map,
            used_group_ids=used_group_ids,
        )
        self._himi_stub_decision = decision
        # t108：逐家族把判定结论打进导出日志。
        # 「为什么这个 IB 没被跳过」是用户最常问、也最难凭产物反推的问题：站位判定
        # 只看「场景真相 + 工作空间 VGMap」，产物 ini 里除了「没有段」之外不留痕迹。
        # 这里每个家族一行（decision / rule / in_scene / absent / stub / skipped），
        # 失败排查直接读日志，不用再靠猜。
        for family_ib, family_info in sorted((decision.families or {}).items()):
            if not isinstance(family_info, dict):
                continue
            print(
                f"{_STUB_LOG_PREFIX} 判定 {family_ib}: "
                f"decision={family_info.get('decision')} rule={family_info.get('rule')} "
                f"in_scene={family_info.get('in_scene')} absent={family_info.get('absent')} "
                f"stub={family_info.get('stub') or []} skipped={family_info.get('skipped') or []}"
            )
        for line in decision.diagnostics:
            try:
                print(f"{_STUB_LOG_PREFIX} {line}")
            except UnicodeEncodeError:
                encoding = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
                print(f"{_STUB_LOG_PREFIX} {line}".encode(encoding, errors="replace").decode(encoding, errors="replace"))
        if merged_runtime:
            # t106 的 writer 站位只对合并骨架运行时成立（它按 palette 槽位记账）。
            self._himi_add_missing_writer_stubs(
                decision,
                workspace_root=workspace_root,
                scene_present=scene_present,
                used_group_ids=used_group_ids,
                family_map=family_map,
            )

        created: list = []
        for bare in decision.stub_members:
            try:
                obj_name = self._himi_create_stub_object(bare)
            except Exception as exc:  # noqa: BLE001
                raise Fatal(
                    f"HIMI 站位对象创建失败（{bare}）：{exc}。站位是抑制原版 draw、"
                    "避免重影的唯一凭据；继续导出会得到重影产物，故中止导出。"
                ) from exc
            if not obj_name:
                raise Fatal(
                    f"HIMI 站位对象创建失败（{bare}）：未返回对象名。中止导出。"
                )
            # 必须在任何后续构造步骤之前登记；否则批量创建中途失败时
            # __init__ 的异常回滚拿不到先前创建的对象。
            self._himi_stub_object_names.append(obj_name)
            stub_draw_call = HIMIStubDrawCall(
                obj_name=obj_name,
                vertex_count=len(HIMI_STUB_TRIANGLE_VERTICES),
                index_count=len(HIMI_STUB_TRIANGLE_VERTICES),
                index_offset=0,
            )
            ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
            if ordered is not None:
                ordered.append(stub_draw_call)
            self._himi_stub_draw_calls.append(stub_draw_call)
            # 登记站位 bare：`_himi_export_entries` 用它做**只读标注**（`is_stub`），
            # **不**用它把站位 bare 排除出 `export_bares`（理由见 `_himi_export_bares`）。
            self._himi_stub_bares.add(str(bare))
            created.append(obj_name)
            print(
                f"{_STUB_LOG_PREFIX} 部件 {bare} 没有对应对象，"
                "已创建极限小三角面占位（游戏内不可见，用于抑制原版 draw 防重影）"
            )
        return created

    def _himi_add_missing_writer_stubs(
        self,
        decision,
        *,
        workspace_root: str,
        scene_present,
        used_group_ids,
        family_map,
    ) -> list:
        """t106：把「保管着本次导出引用的槽、却没有写方」的家族补成 3 点站位。

        场景（用户实机）：只选宿主族 ``077983c9`` 做快速局部导出，宿主 Blend 却引用了槽
        216 —— 那槽由 ``1b62725d-1740-0`` 注册。它已被合并进宿主（它的顶点在宿主顶点池
        里逐字节存在），因此**不需要再画几何**；但那批 palette 是槽 216 唯一的写方，
        缺了它运行期读到零矩阵 ⇒ 顶点被拉向原点（模型爆炸）。

        所以这里给「保管这些槽的族」补 3 点站位：站位的 IB 段会把它的 palette/attach
        段带进产物，几何仍由宿主的 draw 画一次。找不到组件数据时**不猜**：留给导出期的
        orphan 门说清楚缺什么。
        """
        payload = himi_runtime.workspace_slot_owners(workspace_root) or {}
        owners = dict(payload.get("owners") or {})
        if not owners:
            return []
        present = {str(bare) for bare in (scene_present or ()) if str(bare)}
        by_bare: dict = {}
        used = {int(slot) for slot in (used_group_ids or ()) if int(slot) >= 0}
        for slot in sorted(used):
            bare = str(owners.get(slot) or "")
            if not bare or bare in present:
                continue
            by_bare.setdefault(bare, []).append(slot)
        if not by_bare:
            return []
        available = {
            str(member) for members in (family_map or {}).values() for member in members
        }
        added: list = []
        for bare in sorted(by_bare):
            slots = by_bare[bare]
            if bare in decision.stub_members:
                continue
            if available and bare not in available:
                print(
                    f"{_STUB_LOG_PREFIX} 部件 {bare} 保管着合并骨架槽 {slots}，工作空间里却"
                    "没有它的组件数据 ⇒ 无法补站位（导出期 orphan 门会说明缺什么）"
                )
                continue
            if bare in decision.skipped_members:
                decision.skipped_members.remove(bare)
            decision.stub_members.append(bare)
            self._himi_stub_writer_slots[str(bare)] = list(slots)
            added.append(bare)
            print(
                f"{_STUB_LOG_PREFIX} 部件 {bare} 保管着本次导出引用的合并骨架槽 {slots}，"
                "但它不在场景里 ⇒ 自动补一个 3 点站位（几何已在别的部件顶点池里，"
                "站位只负责给这些槽写矩阵）"
            )
        return added

    def _himi_cleanup_stub_objects(self) -> None:
        """导出结束后移除站位对象、网格与注入蓝图的 DrawCall（事务闭合）。"""
        tracked = list(getattr(self, "_himi_stub_draw_calls", []) or [])
        object_names = set(getattr(self, "_himi_stub_object_names", []) or [])
        object_names.update(
            self._himi_draw_call_object_name(draw_call) for draw_call in tracked
        )
        object_names.discard("")
        tracked_ids = {id(draw_call) for draw_call in tracked}
        ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is not None:
            ordered[:] = [
                draw_call
                for draw_call in ordered
                if id(draw_call) not in tracked_ids
                and not bool(getattr(draw_call, "himi_stub", False))
            ]
        for obj_name in sorted(object_names):
            blender_remove_stub_object_data_by_name(obj_name)
        if object_names:
            print(f"{_STUB_LOG_PREFIX} 已清理 {len(object_names)} 个占位小三角面对象")
        self._himi_stub_object_names = []
        self._himi_stub_draw_calls = []
        self._himi_stub_bares = set()
        self._himi_stub_writer_slots = {}

    def himi_stub_decision(self):
        """本次导出的站位决策（无决策时为 ``None``）。"""
        return self._himi_stub_decision

    def himi_stub_object_names(self) -> list:
        return list(self._himi_stub_object_names)

    def himi_stub_bares(self) -> set:
        """本次注入的站位 bare（**只读标注**：这些 bare 仍**进** `export_bares`，
        理由见 `_himi_export_bares`）。"""
        return set(getattr(self, "_himi_stub_bares", ()) or ())

    # ------------------------------------------------------------------ #
    # 计划
    # ------------------------------------------------------------------ #

    @staticmethod
    def _himi_submesh_unique_str(submesh_model) -> str:
        return str(
            getattr(submesh_model, "workspace_unique_str", "")
            or getattr(submesh_model, "unique_str", "")
            or ""
        )

    def _himi_export_entries(self) -> list:
        """本次导出的部件（unique_str / bare / vg_count / is_stub）。

        **F1 之后站位件真的会出现在 `drawib_model_list` 里**（注入早于基类组装模型），
        而 `SubmeshModel.vg_count` 是按 **unique_str** 从工作空间 json 解析的
        （`common/submesh_metadata.py:178`）—— 缺席部件的 json 往往**仍然存在**
        （它是被 join 走的，不是没提取过）⇒ 它的 `vg_count` 可能 > 0。

        因此这里显式标注 `is_stub`，由调用方决定是否排除；**当前口径是不排除**，
        理由见 `_himi_export_bares`（排除会让 G1 家族门在站位场景必然中止导出）。
        标注本身仍有用途：它是「该 bare 是**被站位的成员**、几何不在场景里」的唯一
        只读凭据，供诊断与上层查询（计划书 §7.7.11.4）。
        """
        stub_bares = set(getattr(self, "_himi_stub_bares", ()) or ())
        entries: list = []
        for drawib_model in self.drawib_model_list:
            for submesh_model in drawib_model.submesh_model_list:
                unique_str = self._himi_submesh_unique_str(submesh_model)
                if not unique_str:
                    continue
                bare = himi_runtime.bare_from_unique_str(unique_str)
                entries.append(
                    {
                        "unique_str": unique_str,
                        "bare": bare,
                        "draw_ib": str(drawib_model.draw_ib),
                        "vg_count": int(getattr(submesh_model, "vg_count", 0) or 0),
                        # 站位件的判定**优先于** vg_count：它是合成对象，永远不是
                        # 「本次导出真正带合并数据的部件」。
                        "is_stub": bare in stub_bares
                        or bool(getattr(submesh_model, "himi_stub", False)),
                    }
                )
        return entries

    @staticmethod
    def _himi_export_bares(entries) -> list:
        """G1 目标集合（= 本次导出真正会写进 mod 的部件）。

        **站位 bare 必须进入这个集合**（与 ZZMI 同语义）。这不是疏忽，是唯一
        不自相矛盾的选择 —— 反证（已实测，见
        ``tests/test_himi_stub_objects.py::StubEndToEndIniTests``）：

        1. 判据只在「缺席部件**有** `VGMap` 且专属槽位被引用」时插桩
           （`is_drawib_absorbed`）⇒ 能插桩的缺席部件**必然已落盘**（有 json）；
        2. G1 的家族门是 `L(ib) ⊆ T`，而 `L(ib)` = 该家族**已落盘**组件
           （`common/himi_skeleton.py` 的 `family_members`）；
        3. 于是若把站位 bare 排除出 `T`，被吸收成员永远 `missing` ⇒ 家族永远不
           `safe` ⇒ 契约 `level=error` ⇒ `Fatal` **中止导出**。

        即：**在站位被注入的唯一场景里，排除站位会让导出必然中止** —— 正是站位
        要解决的那个场景。ZZMI 的做法与此一致：站位 DrawCall 被追加进 `ordered`
        （`ui/universal/zzmi.py:1008-1013`），因此它的身份自然进入 ZZMI 的计划，
        家族门才会放行。

        `is_stub` 保留为**只读标注**（`himi_stub_bares()` 供诊断/上层查询），
        **不参与过滤** —— 过滤口径只有一个事实源：`vg_count > 0`。

        规格裁定：计划书 §7.7.11（方案 (A) 采用 / 方案 (B)「排除」否决）+
        `reports/himi-merge/05-decisions.md` §3.20；反证链的独立复核：
        `reports/himi-merge/18-stub-rereview.md` §4.2。
        """
        return [
            entry["bare"]
            for entry in entries
            if int(entry.get("vg_count", 0) or 0) > 0
        ]

    def _himi_discover_borrow_slots(self, plan, *, slot_count: int):
        """【t98】从**本次导出的 Blend 字节**里发现跨族引用槽，并报出无写方的孤儿槽。

        为什么不能用工作空间元数据：``part.referenced_slots`` 取自组件 json 的 VGMap，
        而 VGMap 只映射**物体自己的**局部顶点组（实测 077:51 项→全局 0..50、09c:165 项→
        51..215、1b6:1 项→216），于是「引用集 − 注册集」恒空。跨族引用只出现在**池化行**
        上：同一个 draw 里拼进来的别族几何，它的骨号是全局合并槽。

        逐值判归属（与 ``_himi_remap_merged_blend_to_blocks`` 同一口径）：
        1. 落在本族注册块里 ⇒ 本族，忽略；
        2. ``0 <= slot < 本族槽数`` ⇒ 局部编号（重映射会加基址），不算借用；
        3. 落在**别族**的注册块里 ⇒ 借用；
        4. 谁都不认领 ⇒ 孤儿（导出直接 Fatal：运行期那根索引读到零矩阵）。
        """
        borrowed: dict = {}
        orphans: dict = {}
        parts = list(getattr(plan, "parts", None) or ())
        if not parts:
            return borrowed, orphans
        try:
            base_ranges = himi_runtime.compute_family_ranges(
                parts, int(slot_count or 0), complete=False
            )
        except Exception:  # noqa: BLE001 - 发现失败不影响既有导出路径
            return borrowed, orphans
        owner_of: dict = {}
        own_base_of: dict = {}
        own_slots_of: dict = {}
        for family, info in (base_ranges or {}).items():
            body = dict(info)
            if not bool(body.get("own_block", False)):
                continue
            base = int(body.get("own_base", body.get("base", 0)) or 0)
            slots = int(body.get("slots", 0) or 0)
            own_base_of[str(family)] = base
            own_slots_of[str(family)] = slots
            for slot in range(base, base + slots):
                owner_of[slot] = str(family)
        if not owner_of:
            return borrowed, orphans
        host_family = ""
        max_pos_size = -1
        for m in getattr(self, "drawib_model_list", None) or ():
            fam = str(getattr(m, "draw_ib", "") or "")
            pos_buf = (getattr(m, "category_buffer_dict", None) or {}).get("Position")
            sz = _himi_buffer_byte_size(pos_buf)
            if sz > max_pos_size:
                max_pos_size = sz
                host_family = fam
        if not host_family and plan is not None:
            host_info = self._himi_host_family(plan)
            host_family = str(host_info.get("family") or "") if isinstance(host_info, dict) and host_info.get("ok") else ""
        for drawib_model in getattr(self, "drawib_model_list", None) or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            if family not in own_base_of:
                continue
            payload = self._himi_exported_blend_bytes(drawib_model)
            if not payload:
                continue
            try:
                span = himi_runtime.blend_indices_span(
                    drawib_model.d3d11GameType.D3D11ElementList
                )
            except Exception:  # noqa: BLE001
                continue
            if int(span.get("width", 0) or 0) <= 0:
                continue
            try:
                values = himi_runtime.extract_blend_index_values(payload, span)
            except Exception:  # noqa: BLE001
                continue
            base = own_base_of[family]
            slots = own_slots_of[family]
            verdict = str(
                (
                    self._himi_blend_namespace_verdict(drawib_model, None)
                    or {}
                ).get("verdict", "")
                or ""
            )
            is_global = verdict == "global" or bool(host_family and family == host_family)
            for slot in sorted(int(value) for value in (values or ())):
                if base <= slot < base + slots:
                    continue
                if not is_global and 0 <= slot < max(1, slots):
                    continue
                owner = owner_of.get(slot)
                if owner is None:
                    orphans.setdefault(family, set()).add(slot)
                elif owner != family:
                    borrowed.setdefault(family, set()).add(slot)
        return borrowed, orphans

    def _himi_orphan_message(self, slot_count: int) -> str:
        """【t99】把「没有写方的合并骨架槽」翻译成**可执行的处置**（含每个槽的归属家族）。

        t98 只报槽号，真实导出里那条消息回答不了「我该怎么做」：槽 216 属于 1b62725d，
        它已落盘、只是没进本次导出 —— 用户把它一起选中即可。归属取自工作空间里**已落盘**
        部件的 VGMap（:func:`himi_runtime.workspace_slot_owners`），与本次导出集无关，
        因此能区分「没进本次导出」/「工作空间里也没有写方」这两种完全不同的处置。
        """
        try:
            detail = himi_runtime.describe_ownerless_slots(
                GlobalConfig.path_workspace_folder(),
                self._himi_borrow_orphans,
                exported_bares=[
                    str(getattr(model, "draw_ib", "") or "")
                    for model in (getattr(self, "drawib_model_list", None) or ())
                ],
            )
        except Exception:  # noqa: BLE001 - 取证失败不得掩盖原始错误
            detail = {"lines": [], "missing_bares": []}
        lines = [str(line) for line in (detail.get("lines") or []) if str(line).strip()]
        missing = [
            str(name) for name in (detail.get("missing_bares") or []) if str(name).strip()
        ]
        message = (
            "导出 Blend 引用了本次导出里没有写方的合并骨架槽（运行期读到零矩阵 ⇒ "
            "顶点被拉向原点、模型爆炸）："
            + "；".join(
                f"{family} 引用槽 {slots}"
                for family, slots in sorted(self._himi_borrow_orphans.items())
            )
            + f"。合并骨架共 {int(slot_count or 0)} 槽。"
        )
        if lines:
            message += "".join(f"\n- {line}" for line in lines) + "\n"
        if missing:
            message += (
                "处置：把 "
                + "、".join(missing)
                + " 一起选中再导出（它保管着这些槽的 palette，本批部件与它共用同一批 draw "
                "顶点，缺了它就没人给这些槽写矩阵）；确实只想导一部分时，请先取消合并骨骼。"
            )
        else:
            message += (
                "处置：这些槽在工作空间里也没有写方 —— 请在 LOD0/ 下补齐对应族的组件 json"
                "（或重新导入该族）后再导出。"
            )
        return message

    def _himi_refresh_borrow_layout(self) -> dict:
        """【t98】用**最终** Blend 字节重算一次借用布局（落盘前唯一一次机会）。

        时机：``generate_buffer_files`` 里 ``_himi_remap_ordinary_blend_to_local`` 之后、
        ``_himi_remap_merged_blend_to_blocks`` 之前 —— 此刻每个 DrawIB 的
        ``category_buffer_dict["Blend"]`` 已是「本 draw 全部顶点（含池化行）+ 全局槽号」，
        而骨架 buf / AttachRange / palette / 借用表 / attach 段都还没写盘。
        """
        plan = self.himi_merged_runtime_plan()
        if not getattr(plan, "ok", False) or not getattr(plan, "family_ranges", None):
            return {}
        self._himi_borrow_ledger = {}
        return self._himi_apply_borrow_layout(plan)

    def _himi_apply_borrow_layout(self, plan) -> dict:
        """【t84】把「顶点**实际引用**到的跨族骨」排进各族的块里（就地改 plan，唯一一处）。

        为什么必须在这里改：``plan.family_ranges`` 是**唯一事实源** —— 基址表、AttachRange
        载荷、ini 段、``slot_count``、Blend 索引重写全部从它派生。在计划入口改一次，下游
        不可能互相不一致。

        语义：每族的块 = 自己的 palette 槽（原顺序）+ 它顶点引用到的**别族**槽（升序追加
        在块尾）。块尾那些槽由**源族**的 dispatch 写 ``X_借用方 · P_源``（见 CS 的 t84 块），
        于是借用顶点拿到的矩阵仍然在**借用方**的空间里 ⇒ 不再跟别人的骨骼跑。

        没有借用时**一个字段都不改**（既有无借用路径的输出逐字节不变）。
        """
        if not getattr(plan, "ok", False) or not getattr(plan, "family_ranges", None):
            return {}
        # 【t98】借用集的**事实来源**是「本次导出 Blend 里权重>0 的全局槽」。
        # t94 那条「局部顶点组反查 − 自己写入」在本工程里恒为空（VGMap 只映射物体自己的
        # 顶点组 ⇒ 反查集 ≡ 注册集 ⇒ 差集恒空），所以整套借用机制从来没被触发过。跨族引用
        # 真正出现在 **池化行** 上（同一个 draw 里拼进来的别族几何），那些行的骨号是全局
        # 合并槽；被池化它们的那笔 draw 的 X_f 摆位 ⇒ 必须由引用方写借用槽才正确。
        old_slot_count = max(
            0,
            int(getattr(plan, "slot_count", 0) or 0)
            - int(getattr(plan, "skeleton_extra_slots", 0) or 0),
        )
        referenced: dict = {}
        borrow_plan = dict(getattr(plan, "borrow_plan", None) or {})
        for family, slots in dict(borrow_plan.get("family_borrows") or {}).items():
            values = {int(slot) for slot in (slots or ())}
            if values:
                referenced.setdefault(str(family), set()).update(values)
        discovered, orphans = self._himi_discover_borrow_slots(plan, slot_count=old_slot_count)
        for family, slots in discovered.items():
            values = {int(slot) for slot in (slots or ())}
            if values:
                referenced.setdefault(str(family), set()).update(values)
        self._himi_borrow_orphans = {
            str(family): sorted(int(slot) for slot in (slots or ()))
            for family, slots in (orphans or {}).items()
            if slots
        }
        if self._himi_borrow_orphans:
            from ...utils.format_utils import Fatal

            raise Fatal(self._himi_orphan_message(old_slot_count))
        if not referenced:
            return {}
        # 【t98】每次重排都从 `part.slot_values` **重新派生** own_base/base
        # （`compute_family_ranges` 是纯函数，不叠加位移），所以不再需要 t94 的
        # 「已经 shift 过就只复核」分支。`complete=False`：块尾借用槽让
        # ∑block_slots > 缓存槽数，覆盖性由「借用行 source_local 合法」保证。
        try:
            ranges = himi_runtime.compute_family_ranges(
                plan.parts,
                old_slot_count,
                complete=False,
                borrowed_slots=referenced,
            )
        except Exception as exc:  # noqa: BLE001 - 借用重排失败绝不打断导出
            self._himi_notify(
                f"跨族借用槽重排失败（本次按旧排布导出，借用顶点仍会错位）：{exc}",
                warning=True,
            )
            return {}
        new_slot_count = int(himi_runtime.ranges_slot_count(ranges))
        # 【t98】t94 的「借用集必须与局部顶点组反查逐项相等」硬门作废：反查口径在池化行上
        # 本来就是空集，留着它会把每一个**真实的**借用判红。改判据见下面的排布自检与
        # 覆盖性自检（无写方槽 ⇒ Fatal，上面 already 处理）。
        borrow_mismatch = himi_runtime.borrow_slot_problems(plan.parts, ranges)
        if borrow_mismatch:
            self._himi_notify(
                "借用槽与「局部顶点组反查」不一致（t98 起以导出 Blend 为准，仅供参考）："
                + "；".join(borrow_mismatch),
                warning=True,
            )
        borrows = {
            str(family): list(info.get("borrow") or ())
            for family, info in (ranges or {}).items()
            if list(dict(info).get("borrow") or ())
        }
        if not borrows:
            return {}
        new_slot_count = int(himi_runtime.ranges_slot_count(ranges))
        # 【t93 硬门】块尾借用槽一旦排进去，运行期就靠「借用表 + 身份资源 + 块基址」三样同时
        # 成立才写得对；任何一项与布局不一致，产出的就是「看着导出了、进游戏错位」的东西
        # （顶点索引指向别人的槽，或借用行读别人的 palette）。所以判红即 **Fatal**，不是
        # warning —— 排布已经改动源，退回旧排布也不再正确（旧编号在新区间里是别人的槽）。
        layout_problems = himi_runtime.borrow_map_layout_problems(
            ranges, himi_runtime.borrow_map_rows_by_family(ranges)
        )
        layout_problems += himi_runtime.attach_range_layout_problems(
            ranges, skeleton_slots=new_slot_count
        )
        if layout_problems:
            from ...utils.format_utils import Fatal

            raise Fatal(
                "跨族借用槽的排布自检未通过（借用表 / 身份资源会让运行期读错槽）："
                + "；".join(layout_problems)
            )
        plan.family_ranges = ranges
        plan.slot_count = new_slot_count
        plan.skeleton_extra_slots = max(0, new_slot_count - old_slot_count)
        ledger = {"old_slot_count": old_slot_count, "new_slot_count": new_slot_count, "families": {}}
        for family, info in sorted(
            (str(name), dict(body)) for name, body in ranges.items()
        ):
            if not bool(info.get("own_block", False)):
                continue
            borrow = list(info.get("borrow") or ())
            ledger["families"][family] = {
                "base": int(info.get("base", 0) or 0),
                "slots": int(info.get("slots", 0) or 0),
                "block_slots": int(info.get("block_slots", info.get("slots", 0)) or 0),
                "borrow": [(int(row["target_slot"]), int(row["source_slot"]), str(row["source_family"])) for row in borrow],
            }
            if borrow:
                self._himi_notify(
                    f"{family}: 块 [{int(info.get('base', 0))}, "
                    f"{int(info.get('base', 0)) + int(info.get('block_slots', 0))}) 里追加 "
                    f"{len(borrow)} 个借用槽 ⇒ "
                    + "、".join(
                        f"槽{int(row['target_slot'])}←{row['source_family']}的{int(row['source_slot'])}"
                        for row in borrow
                    )
                )
        self._himi_notify(
            f"跨族借用槽已排入计划：合并骨架 {old_slot_count} → {new_slot_count} 槽"
            f"（追加 {plan.skeleton_extra_slots} 个块尾槽；每个借用槽由**借用方自己的** dispatch "
            "写 X_源·P_源，来源族的 palette 以 cs-tN 副本绑进来）"
        )
        self._himi_borrow_ledger = ledger
        return ledger

    def himi_merged_runtime_plan(self):
        """本次导出的运行时计划（只读工作空间缓存；dump 删了也能算）。

        本模块的异常**绝不**中断导出：任何意外都退化成「没有运行时计划」+ 大声诊断，
        由契约判定决定是继续（普通/部分合并）还是中止（有数据却全被拒）。
        """
        if self._himi_plan_ready:
            return self._himi_plan
        self._himi_plan_ready = True
        try:
            entries = self._himi_export_entries()
            plan = himi_runtime.load_workspace_plan(
                workspace_root=GlobalConfig.path_workspace_folder(),
                export_unique_strs=[entry["unique_str"] for entry in entries],
                # G1 目标集合 = 本次导出真正会写进 mod 的部件；数据层会用它做
                # pass 级门控（家族里任何落盘成员缺席 ⇒ 整个 pass 不合并）。
                # 站位 bare **进**这个集合的理由（含反证）见 `_himi_export_bares`。
                export_bares=self._himi_export_bares(entries),
                checkbox_enabled=bool(GlobalProterties.import_merged_vgmap()),
            )
        except Exception as exc:  # noqa: BLE001 - 导出通道不得被本模块异常打断
            plan = himi_runtime.HIMIExportRuntimePlan(
                ok=False, message=f"HIMI 合并骨架运行时计划计算异常：{exc}", source="exception"
            )
            plan.diagnostics.append(plan.message)
        # 【t84】跨族借用：在**计划入口**把借用槽排进各族块（下游全部同源）。
        self._himi_apply_borrow_layout(plan)
        for line in plan.diagnostics:
            print(f"{_LOG_PREFIX} {line}")
        self._himi_plan = plan
        return plan

    # ------------------------------------------------------------------ #
    # 契约（必须在任何写盘之前）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _himi_notify(detail: str, *, warning: bool = False) -> None:
        prefix = f"{_LOG_PREFIX} 警告 " if warning else f"{_LOG_PREFIX} "
        for line in str(detail).splitlines() or [""]:
            try:
                print(prefix + line)
            except UnicodeEncodeError:
                encoding = getattr(sys.stdout, "encoding", "utf-8") or "utf-8"
                print((prefix + line).encode(encoding, errors="replace").decode(encoding, errors="replace"))
        try:
            from ...utils.log_utils import LOG

            (LOG.warning if warning else LOG.info)(detail)
        except Exception:  # noqa: BLE001 - 日志器不可用时 print 已经送达
            pass

    def _himi_verify_deform_draw_structure(self, plan) -> None:
        """【K2】参与合并的每个部件，其 Position/Blend 必须是 ``pointlist`` 抽取（否则 `Fatal`）。

        根因（t11 §2 实测 + t13 逐帧核对）：跨族合并把「一族一个 deform pass」重放成
        ``draw = <行数>, 0``，其语义前提是该 pass 是**非索引 pointlist Draw（带 SO 目标）**；
        若某族的 Position/Blend 是 trianglelist，重放画出来的就不是变形流输出。

        **t18 / T5-F2 修复**：此前本门用 ``record.get("unique_str")`` 定位 json，而
        `plan.rebinds` 的字典**没有这个键**（`common/himi_merged_runtime.py` 的手写字面量）
        ⇒ **每条记录被 `continue` 跳过**：门在真实导出路径上一次判断都没做，
        `unavailable == 0` 同样是空转的副产物（t5 实测「通知 0 条」）。现在：
        * 计划侧补齐 `unique_str`（单一事实源在计划里）；
        * 本门维护**可观测账目**（``判过/判定/读不到/缺身份`` 四个计数）并**无论成败都打一行汇总**
          ——「没有报错」不再被当成「检查过了」的证据；
        * 记录缺 ``unique_str``（计划形状回退）会被**单独计数并大声警告**，而不是静默跳过。
        """
        from ...utils.format_utils import Fatal

        problems: list = []
        judged = 0           # 判据看过的记录数（= 每条 rebind 都算，不只算能定位 json 的）
        checked = 0          # 真读到 json **且含可判元素**、被判定过的部件数
        unavailable = 0      # 有身份但 json 读不到（cache-only 重建 / 文件缺失）
        unjudgeable = 0      # json 读到了，但里面没有 Position/Blend 元素记录（不可判）
        missing_identity = 0  # 记录没有 unique_str ⇒ 门无法定位 json（计划形状回退）
        for item in getattr(plan, "rebinds", None) or ():
            record = dict(item or {})
            judged += 1
            unique_str = str(record.get("unique_str") or "")
            if not unique_str:
                missing_identity += 1
                continue
            try:
                json_path = himi_runtime.resolve_submesh_json_path(
                    GlobalConfig.path_workspace_folder(), unique_str
                )
            except Exception:  # noqa: BLE001 - 核对本身不得打断导出
                json_path = ""
            if not json_path or not os.path.isfile(json_path):
                unavailable += 1
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:  # noqa: BLE001
                unavailable += 1
                continue
            if not deform_draw_structure_judged(payload):
                unjudgeable += 1
                continue
            checked += 1
            problems.extend(
                deform_draw_structure_problems(payload, unique_str=unique_str)
            )
        if problems:
            verdict = "失败"
        elif judged == 0:
            # 【t20 / T19-F1】一条记录都没判过（`rebinds` 为空：当前不可达，但**形状必须显式排除**）
            # ⇒ 不得报「通过」：那正是 K2 原病（**空转也报成功**）的同一个形状。
            verdict = "未核对（无 rebind 记录）"
        elif checked == 0:
            # 【t20 / T19-F1 的完整形状】有记录、但**一个部件都没真判过**
            # （全部缺身份 / 读不到 json / json 无可判元素）⇒ 同样是「空转」，
            # 只能说「未核对」；三种原因各自的计数与警告在上面的账目里。
            verdict = "未核对（没有可判的部件）"
        else:
            verdict = "通过"
        summary = (
            f"变形 pass 结构核对（K2）：判过 {judged} 条记录、读到 json 并判定 {checked} 个部件、"
            f"json 里没有可判元素 {unjudgeable} 个、读不到 json {unavailable} 个、"
            f"记录缺身份 {missing_identity} 条 ⇒ {verdict}"
        )
        # **可观测证据**：无论成败都打这一行（t18 起「有没有真的检查」由它回答）。
        print(f"{_LOG_PREFIX} {summary}")
        self._himi_deform_structure_result = {
            "judged": judged,
            "checked": checked,
            "unavailable": unavailable,
            "unjudgeable": unjudgeable,
            "missing_identity": missing_identity,
            "problems": list(problems),
            "verdict": verdict,
            "summary": summary,
        }
        if missing_identity:
            self._himi_notify(
                f"变形 pass 结构核对：{missing_identity} 条 rebind 记录没有 unique_str ⇒ 门**无法定位"
                "部件 json**、这一部分根本没判过（计划侧应始终带上该字段，见 "
                "common/himi_merged_runtime.py::plan_from_metadata）",
                warning=True,
            )
        if unjudgeable:
            self._himi_notify(
                f"变形 pass 结构核对：{unjudgeable} 个部件的 json 里**没有 Position/Blend 元素记录**"
                "⇒ 这几种情况标为未核对（不阻断，但也**不算通过**）",
                warning=True,
            )
        if unavailable:
            self._himi_notify(
                f"变形 pass 结构核对：{unavailable} 个部件没有可读 json "
                "（cache-only 重建？）⇒ 这些部件标为未核对（不阻断）",
                warning=True,
            )
        if problems:
            raise Fatal(
                "变形 pass 结构自检失败 ⇒ 中止导出（`draw = <行数>, 0` 的重放语义不成立）："
                + "；".join(problems[:6])
                + ("；…" if len(problems) > 6 else "")
            )

    # ------------------------------------------------------------------ #
    # 【F6 / T45-F4】导出前的两道硬门（都必须早于任何写盘）
    # ------------------------------------------------------------------ #

    def _himi_verify_base_table_against_plan(self, plan) -> None:
        """【F6】本次 plan 生成的那张基址表 vs 本族区间：**矛盾就 Fatal**，其余大声诊断。

        表**就是**由 ``plan.family_ranges`` 生成的（``merged_attach_base_table_lines``），
        所以正常情况下这道门必然通过 —— 它抓的是生成器自己出问题的那一种：**两族源槽位数
        相同**。运行期只按源槽位数查表，两族同 span ⇒ 后写的覆盖先写的（第二族整族写到第一族
        的槽块上）⇒ 必须在写盘前 Fatal，不能产出一个「看上去导出了、进游戏是错位 / 消失」的
        产物。

        历史（报告 50 §1.2 入口 ②）：表曾是参考角色那一份的**冻结副本**，与本族排布无关联
        ⇒ 「同 span 不同 base」时运行期 ``base_slot != identity_base`` 整族拒写、5 族全不写
        ⇒ 骨架全零 ⇒ 模型静默消失。把表改成由 plan 生成之后，这种矛盾不可能再出现；
        本门留下的是它的**结构性残余**（重复 span）+ 诊断（∑span ≠ 槽位数）。
        """
        from ...utils.format_utils import Fatal

        audit = merged_attach_base_table_audit(
            getattr(plan, "family_ranges", None) or {},
            int(getattr(plan, "slot_count", 0) or 0),
        )
        # 诊断收集器用 getattr 兜底：本仓有 `ExportHIMI.__new__` 的手工夹具（不走 __init__）。
        notices = getattr(self, "_himi_base_table_notices", None)
        if notices is None:
            notices = []
            self._himi_base_table_notices = notices
        for line in audit.get("notices") or ():
            notices.append(str(line))
            self._himi_notify(f"基址表诊断：{line}", warning=True)
        problems = list(audit.get("problems") or ())
        if problems:
            raise Fatal(
                "合并骨架基址表与本次家族区间**直接矛盾** ⇒ 中止导出"
                "（运行期会整族拒写，或两族撞同一个槽块）："
                + "；".join(problems)
            )

    def _himi_enforce_contract(self) -> dict:
        """按合并骨架契约判定「继续 / 中止」；必须早于 ``generate_buffer_files``。

        * ``error``（有数据但一个部件都没进合并）⇒ ``Fatal``（仓库既有致命通道，
          导出操作符会把它变成用户可见的 ``report({'ERROR'})``）。
        * ``warning``（部分被拒）⇒ 用户可见提示后继续导出。
        * ``notice``（没有合并数据）⇒ 一行日志，绝不打断。
        """
        from ...utils.format_utils import Fatal

        plan = self.himi_merged_runtime_plan()
        # 【t85·结构性隔离】下面三项是**合并骨架专属**检查（F6 基址表 / K4 触发键 /
        # K2 变形 pass 结构），只在合并路径激活时执行。旧行为在普通导出（开关关闭）也跑：
        # 基址表对着**空的** `family_ranges` 判出「多出 5 行」⇒ 控制台刷 5 条 Warning，
        # 而且这些诊断被写进产物 ini 的合并骨架注释块 —— 用户实报「不使用合并骨骼时
        # 配置表里面会有合并骨骼的生成」。判据与 `generate_unity_vs_config_ini` 用
        # **同一个谓词**：`_himi_merged_attach_active()`。
        merged_active = self._himi_merged_attach_active()
        if merged_active:
            # 【F6】基址表 vs 动态排布（先于任何写盘）：不一致 = 运行期「一族都不写 ⇒ 模型消失」。
            self._himi_verify_base_table_against_plan(plan)
        if merged_active:
            # 【t12/t13 收编 K2/K4】合并 deform 的**触发键**与**变形 pass 结构**：
            # K4（键不得 1→多）：只诊断不阻断 —— t13 实测 4 帧内无实际歧义（理论风险）。
            key_notices = getattr(self, "_himi_key_notices", None)
            if key_notices is None:
                key_notices = []
                self._himi_key_notices = key_notices
            for line in merged_deform_key_problems(getattr(plan, "rebinds", None) or ()):
                key_notices.append(str(line))
                self._himi_notify(f"合并 deform 触发键诊断：{line}", warning=True)
            # K2（宿主/成员族的变形 pass 必须是 pointlist 抽取）：`draw = <行数>, 0` 的语义前提。
            self._himi_verify_deform_draw_structure(plan)
        # 【t78 方案 B】Q_f 已**彻底退出变换路径**（顶点侧不校准、palette 侧不乘 Q_f⁻¹）⇒ 摆放读不到
        # 不再是「半校准」：它既不进顶点缓冲，也不进身份载荷的 [1..3]（那里恒为单位阵）。因此这里
        # 不再因为摆放缺失而降级合并骨架（降级会白白丢掉合并产物），只把读失败当诊断打出来。
        # 真正**参与写槽**的族级矩阵只有 X_f（来自抓帧 cb3）⇒ 它的三态门与摆放无关，必须独立判定。
        # 【门控】只要本次导出走的是**共享骨架 + 分族写槽**这条通路，每族就要自己的 X_f：
        # 各族顶点是按宿主空间烘的、骨矩阵却分块写进同一张骨架，非宿主族不乘 X 就按宿主摆
        # （历史实测偏差 1.0~3.8 游戏单位，就是头发这类非宿主子网格的偏移量级）。
        # 这与「有没有把物体合并到一起」无关 —— 合并只决定有没有借用槽。
        if (
            bool(GlobalProterties.import_merged_vgmap())
            and getattr(plan, "has_rebinds", False)
        ):
            self._himi_family_placements()
            placement_problems = list(getattr(self, "_himi_placements_problems", []) or [])
            if placement_problems:
                self._himi_notify(
                    "族级摆放 Q_f 只读到一部分（方案 B 下它不参与任何变换，仅记录）："
                    + "；".join(placement_problems[:6])
                    + ("；…" if len(placement_problems) > 6 else ""),
                    warning=True,
                )
            # 【t39-X】族级物体矩阵 `X_f = M_base⁻¹·M_f`：合并后整块只在**宿主族一笔 draw**
            # 里渲染，缺了它各族就按宿主族摆放（实测偏差 1.0~3.8 游戏单位）。三态：
            #   * 全部族都有合法 X_f ⇒ 载荷 [4..6] 写 X_f（冷启动兜底，方案 B 的族级矩阵）；
            #   * **部分**缺失/不合法 ⇒ 只警告、不中止：校准的权威在运行期（CS 每帧按骨算
            #     X = M_宿主⁻¹·M_来源，见借用槽与 cs-u1 表），导出期 X_f 只是「表还没热起来的
            #     头一两帧」的兜底。半校准在这里不等于坏产物：缺的族退回单位阵，下一帧自愈。
            #   * 一个都没有（老工作空间没有抓帧）⇒ `_himi_family_x` 里警告、不校准，
            #     导入/导出照常（CS 读到全零 X ⇒ 跳过左乘，与旧行为逐字节等价）。
            self._himi_family_x()
            family_x_problems = list(getattr(self, "_himi_family_x_problems", []) or [])
            if family_x_problems:
                self._himi_notify(
                    "族级物体矩阵（t39-X）缺一部分族，退回单位阵兜底"
                    "（运行期 CS 每帧自算 X，不影响最终校准）："
                    + "；".join(family_x_problems[:6])
                    + ("；…" if len(family_x_problems) > 6 else ""),
                    warning=True,
                )
        elif bool(GlobalProterties.import_merged_vgmap()):
            # 开了「融合统一顶点组」但本次没有 rebind pass（整个模型都没被重绑）⇒ 没有共享
            # 骨架可写，族级物体矩阵无从谈起。
            self._himi_notify("本次没有重新绑定的对象 ⇒ 不生成共享骨架，跳过族级物体矩阵（t39-X）")
        snapshot = plan.contract_snapshot(
            checkbox_enabled=bool(GlobalProterties.import_merged_vgmap())
        )
        decision = evaluate_merged_skeleton_contract(**snapshot)
        self._himi_contract_decision = decision

        level = str(decision.get("level") or "")
        detail = "\n".join(
            str(part) for part in (decision.get("message"), decision.get("hint")) if str(part)
        )
        if level == "error":
            # 【t73·兜底原则】不再一票否决导出：合并骨架侧失败 ⇒ **降级为普通导出 + 大声警告**。
            # （旧行为 `raise Fatal(...)` 会把普通导出一起干掉 —— 兜底不可被剥夺。）
            self._himi_degrade_to_ordinary(detail or "HIMI 骨骼合并契约校验失败")
            level = "degraded"
            self._himi_contract_decision = dict(decision, level="degraded",
                                                degrade_reason=detail or "")
        if level == "warning":
            self._himi_notify(detail, warning=True)
        elif level == "notice":
            print(f"{_LOG_PREFIX} {decision.get('message', '')}")
        elif level == "degraded":
            print(f"{_LOG_PREFIX} 已降级为普通导出（合并骨架侧失败，见上面的警告）")
        if plan.ok:
            print(f"{_LOG_PREFIX} {plan.message}")
        else:
            print(f"{_LOG_PREFIX} 本次按普通导出（合并骨架路径未激活/已降级）：{plan.message}")
        for line in UNVERIFIED_IN_GAME:
            print(f"{_LOG_PREFIX} 未实机验证（{line}）")
        return decision

    # ------------------------------------------------------------------ #
    # 【B3】形态键 × 合并骨架的互斥硬门
    # ------------------------------------------------------------------ #

    def _himi_shapekey_object_probe(self) -> list:
        """本次导出涉及形态键的物体（**可注入**：测试替换本方法即可，不需要 bpy）。

        默认读 ``BlueprintExportHelper.shapekey_objects``（导出前由导出器填好）；拿不到 ⇒ 空列表
        （= 本次没有形态键可导出，门放行）。这个钩子是 B3 唯一的取数面：本仓库没有「当前导出树」
        的全局入口，``ExportHIMI`` 自己也没有 ``ordered_postprocess_nodes``。
        """
        try:
            from ...blueprint.export_helper import BlueprintExportHelper
        except Exception:  # noqa: BLE001 - 离线测试/oracle 没有 bpy ⇒ 没有这个模块
            return []
        return list(getattr(BlueprintExportHelper, "shapekey_objects", None) or [])

    def _himi_non_basis_shapekey_blocks(self) -> int:
        """非 Basis 形态键块总数（0 = 没有形态键 / 只有 Basis）。

        ``count_exportable_shapekey_blocks`` 是鸭子类型的（只读 ``data.shape_keys.key_blocks``）
        ⇒ 测试可以直接拿 SimpleNamespace 物体喂它。探测异常一律按 0 处理：门不能因为探测失败
        去误伤普通导出。
        """
        counter = None
        try:
            from ...blueprint.export_helper import BlueprintExportHelper
            counter = BlueprintExportHelper.count_exportable_shapekey_blocks
        except Exception:  # noqa: BLE001 - 离线测试/oracle 没有 bpy
            counter = None
        total = 0
        for obj in self._himi_shapekey_object_probe() or ():
            if counter is not None:
                try:
                    total += int(counter(obj) or 0)
                    continue
                except Exception:  # noqa: BLE001
                    pass
            # 无 bpy 时的鸭子镜像：判据与 count_exportable_shapekey_blocks 同一条（key_blocks 去掉 Basis）
            try:
                shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
                blocks = list(getattr(shape_keys, "key_blocks", None) or ())
            except Exception:  # noqa: BLE001
                blocks = []
            total += max(0, len(blocks) - 1)
        return total

    def _himi_shapekey_direct_mode_probe(self) -> bool:
        """是否有形态键节点**没有**勾直出（True = 会走标准槽位轮次）。

        优先用挂在本 exporter 上的 ``_needs_standard_shapekey_rounds`` 钩子（直出会话可以把
        ``blueprint/direct_export.py`` 的同名判据绑上来）；否则读
        ``BlueprintExportHelper.shapekey_postprocess_nodes`` 里任一 ``direct_export_mode`` 为假。
        """
        hook = getattr(self, "_needs_standard_shapekey_rounds", None)
        if callable(hook):
            try:
                return bool(hook())
            except Exception:  # noqa: BLE001
                pass
        try:
            from ...blueprint.export_helper import BlueprintExportHelper
        except Exception:  # noqa: BLE001
            return False
        for node in getattr(BlueprintExportHelper, "shapekey_postprocess_nodes", None) or ():
            if not bool(getattr(node, "direct_export_mode", False)):
                return True
        return False

    def _himi_enforce_shapekey_direct_mode(self) -> dict:
        """【B3 硬门】合并骨架 + 非 Basis 形态键块 + 形态键节点未勾直出 ⇒ ``Fatal``。

        为什么是 Fatal 而不是告警：标准（非直出）路线会先落盘 ``Meshes1001+`` 的形态键槽位，而
        合并骨架**只在 ``Meshes0000`` 里**重建了 Position 缓冲与全局骨骼编号 —— 两边的顶点组/
        槽位编号对不上，游戏里一开形态键就整块撕裂。这条判据还必须落在**导出前置阶段**：直出流程
        下 ``common/m_ini_helper.py`` 那段告警根本不会执行（``suppress_shapekey_resource_export``
        时直接 return），等于没有保护。
        """
        merged_active = bool(self._himi_merged_attach_active())
        problem = shapekey_direct_mode_problem(
            merged_active=merged_active,
            non_basis_blocks=self._himi_non_basis_shapekey_blocks() if merged_active else 0,
            needs_standard_rounds=self._himi_shapekey_direct_mode_probe() if merged_active else False,
        )
        result = {"ok": not problem, "merged_active": merged_active, "problem": problem}
        self._himi_shapekey_gate_result = result
        if problem:
            # 文案里的「怎么改」提示（SHAPEKEY_DIRECT_MODE_HINT）由纯判据自带，这里不重复拼。
            from ...utils.format_utils import Fatal

            raise Fatal(problem)
        return result

    # ------------------------------------------------------------------ #
    # 导出入口
    # ------------------------------------------------------------------ #

    def export(self):
        try:
            # 计划 + 契约判定必须先于任何写盘：generate_buffer_files 是本次导出的第一个
            # 落盘点，error 时不得留下半成品 mod（.buf / .ini）。
            self._himi_enforce_contract()
            # 【B3】形态键 × 合并骨架的互斥硬门：同样在写盘之前（直出流程里 ini 侧告警不执行）。
            self._himi_enforce_shapekey_direct_mode()
            super().export()
        finally:
            # 事务闭合：正常与异常路径都必须清掉站位对象/网格/注入的 DrawCall。
            self._himi_cleanup_stub_objects()

    def export_buffers_only(self):
        """多轮导出的纯缓冲路径也必须闭合站位对象事务。"""
        try:
            return super().export_buffers_only()
        finally:
            self._himi_cleanup_stub_objects()

    # ------------------------------------------------------------------ #
    # deform 重绑（唯一运行时动作）
    # ------------------------------------------------------------------ #

    def _himi_rebind_for(self, drawib_model):
        """该 DrawIB 的可重绑记录；不可重绑（未通过 G1 / 无数据 / 前置条件不成立）返回 ``None``。"""
        plan = self.himi_merged_runtime_plan()
        if not plan.has_rebinds:
            return None
        rebind = plan.rebind_for(str(drawib_model.draw_ib))
        if rebind is None:
            return None
        declared_hash = str(
            (getattr(drawib_model, "category_hash_dict", {}) or {}).get("Blend", "") or ""
        )
        plan_hash = str(rebind.get("category_hash", "") or "")
        if declared_hash and plan_hash and declared_hash.lower() != plan_hash.lower():
            # 段匹配 hash 与计划里的不一致 ⇒ 重绑可能落到别的 pass 上：宁可退普通路径。
            self._himi_notify(
                f"{drawib_model.draw_ib}: VB 段的 Blend hash（{declared_hash}）与"
                f"合并数据里的 CategoryHash.Blend（{plan_hash}）不一致，"
                "该部件退回普通路径（不重绑 vs-t0）。",
                warning=True,
            )
            return None
        verdict = self._himi_blend_namespace_verdict(drawib_model, rebind)
        if verdict.get("verdict") == "local":
            self._himi_notify(
                f"{drawib_model.draw_ib}: {verdict.get('note')}", warning=True
            )
            return None
        if verdict.get("verdict") in {"indeterminate", "unavailable"}:
            print(f"{_LOG_PREFIX} 提示 {drawib_model.draw_ib}: {verdict.get('note')}")
        return rebind

    def himi_family_range(self, family: str):
        """该族在合并骨架里的 ``{"base", "slots", "own_block"}``；不可发段 ⇒ ``None``。

        只有一个事实源（``plan.family_ranges``，步骤① 的 fail-closed 判定）：
        ``own_block=False`` 的**借位族**没有自己的块 ⇒ **不发捕获行、不发 attach 段**
        （它的槽由拥有者家族的段写入，矩阵按逐位去重语义相同）。
        """
        plan = self.himi_merged_runtime_plan()
        info = dict((plan.family_ranges or {}).get(str(family or "")) or {})
        if not info or not bool(info.get("own_block", False)):
            return None
        if int(info.get("slots", 0) or 0) <= 0:
            return None
        return info

    def himi_family_range_items(self) -> list:
        """全部**可发段**家族（按 base 升序）：``[(family, {"base","slots","own_block"}), …]``。"""
        plan = self.himi_merged_runtime_plan()
        if plan is None:
            return []
        items = [
            (str(family), dict(info))
            for family, info in (getattr(plan, "family_ranges", None) or {}).items()
            if bool(dict(info).get("own_block", False)) and int(dict(info).get("slots", 0) or 0) > 0
        ]
        items.sort(key=lambda item: (int(item[1]["base"]), item[0]))
        return items

    def _himi_inject_deform_capture(self, ini_builder, drawib_model, *, before: int) -> None:
        """给**不重绑**的族补三行捕获（本族 deform pass 是该块槽的唯一写方）。

        【t95】与 :meth:`_himi_rebind_for` 解耦：重绑只决定「本族自己的顶点要不要走合并
        骨架」，而 attach 段决定「骨架里本族那块槽当帧有没有内容」——它是**别人的**输入。
        两者共用一个门 ⇒ 未通过 G1 的族不生成 capture/run ⇒ 它的槽恒零 ⇒ 任何索引到该块
        的顶点都拿零矩阵（模型爆炸/塌到原点），且**在游戏里完全静默**。

        行文本同源于 :func:`himi_runtime.render_attach_capture_lines`（palette → cb3 →
        run），本模块不另写一份；插入位置 = 基类 VB 段内 draw 行之前。
        """
        family = str(getattr(drawib_model, "draw_ib", "") or "")
        if not family or self.himi_family_range(family) is None:
            return
        capture_lines = himi_runtime.render_attach_capture_lines(family)
        after = list(getattr(ini_builder, "ini_section_list", []) or ())
        touched = 0
        for section in after[int(before or 0):]:
            if getattr(section, "SectionType", None) != M_SectionType.TextureOverrideVB:
                continue
            lines = list(getattr(section, "SectionLineList", []) or ())
            out: list = []
            inserted = False
            for line in lines:
                if not inserted and str(line).strip().startswith("draw"):
                    out.extend(capture_lines)
                    inserted = True
                    touched += 1
                out.append(line)
            if inserted:
                section.SectionLineList[:] = out
        if not touched:
            print(
                f"{_LOG_PREFIX} 提示 {family}: VB 段里没有 draw 行 ⇒ 未插本族捕获行/attach"
                "（该族块槽本次不会被写入）"
            )

    def _himi_exported_blend_bytes(self, drawib_model):
        """本次导出真正写出的 Blend 字节（numpy uint8 扁平数组 → bytes）。"""
        buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
        payload = buffers.get("Blend")
        if payload is None:
            return b""
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        tobytes = getattr(payload, "tobytes", None)
        if callable(tobytes):
            try:
                return tobytes()
            except Exception:  # noqa: BLE001
                return b""
        try:
            return bytes(payload)
        except Exception:  # noqa: BLE001
            return b""

    def _himi_blend_namespace_verdict(self, drawib_model, rebind) -> dict:
        """核对 mod 的 Blend 是否带**全局**骨骼编号（重绑 vs-t0 的前置条件）。

        ``vb1`` 换成 mod 的 Blend 之后，游戏 deform VS 会用里面的编号去索引重绑后的
        骨架；只有编号是全局槽位时才正确。核对用的对照物是工作空间里那份**原部件**
        Blend（游戏局部编号）+ json 的 ``VGMap``。判定为 ``local`` 时该家族不合并
        （退回基类路径，保持 mod 与今天一致的行为）+ 大声诊断。
        """
        draw_ib = str(drawib_model.draw_ib)
        if draw_ib in self._himi_blend_verdicts:
            return self._himi_blend_verdicts[draw_ib]
        unique_strs = [
            self._himi_submesh_unique_str(submesh_model)
            for submesh_model in drawib_model.submesh_model_list
        ]
        span = himi_runtime.blend_indices_span(
            getattr(getattr(drawib_model, "d3d11GameType", None), "D3D11ElementList", []) or []
        )
        verdict = himi_runtime.check_exported_blend_namespace(
            workspace_root=GlobalConfig.path_workspace_folder(),
            unique_strs=[name for name in unique_strs if name],
            exported_blend_bytes=self._himi_exported_blend_bytes(drawib_model),
            blend_span=span,
        )
        self._himi_blend_verdicts[draw_ib] = verdict
        return verdict

    def add_unity_vs_texture_override_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """在基类 VB 段形态上注入 ``vs-t0`` 重绑与 deform 顶点数（计划书 §7.4 + t48 修正）。

        逐行同构于 ``ui/universal/unity.py:44-73``；只有「该部件通过 G1」时，
        在 draw 所在的那个类别段（HIMI = ``Blend`` 类别）里多两处差异：

        * ``vs-t0 = ResourceHI3MergedSkeleton_<role_key>``
        * ``draw = <**模组自己上传的行数**>, 0`` ——权威来源是 ``drawib_model.draw_number``
          （= ``vertex_count``，即同产物的 ``override_vertex_count``）：deform 重放就是把
          模组上传的每一行过一遍，写多会越界读并把角色级 SO 池写爆。

        ⚠ **被推翻的旧口径（t47 实机证伪）**：这里曾写「本 IB 家族各分片
        ``OriginalVertexCount`` 之和」（= 计划书 §7.4 第 1 条原文）。实测每家族只有一个
        deform pass，而同族每成员的 OVC **相同**（分片/LOD 变体）⇒ 求和是 N 倍
        （用户产物 99475 vs 模组 31066 行，最高 1263×）⇒ 正是「无骨骼约束 + 错位 +
        上下颠倒 + 部件随意旋转」的成因。家族 Σ 现在只作**诊断**（见
        ``himi_runtime.resolve_deform_draw_count``）。
        """
        rebind = self._himi_rebind_for(drawib_model)
        if rebind is None:
            # 【t95】不重绑 ≠ 不写槽。attach 段写的是**骨架里本族那块槽**，与本族顶点
            # 怎么画无关；旧实现把「写槽」和「重绑 vs-t0」共用一个门 ⇒ 未通过 G1 的族
            # 连 capture/run 都不生成 ⇒ 那块槽从未被写 ⇒ 引用它的顶点拿零矩阵
            # （13:54 产物：09c6e58a 的 165 个槽全空）。这里走基类段 + 后处理插入，
            # 与 IB 侧同一种模式：只补捕获，不动 vs-t0 / handling / draw。
            before = len(list(getattr(ini_builder, "ini_section_list", []) or ()))
            super().add_unity_vs_texture_override_vb_sections(
                ini_builder=ini_builder, drawib_model=drawib_model
            )
            self._himi_inject_deform_capture(ini_builder, drawib_model, before=before)
            return

        d3d11_game_type = drawib_model.d3d11GameType
        draw_ib = drawib_model.draw_ib

        texture_override_vb_section = M_IniSection(M_SectionType.TextureOverrideVB)
        texture_override_vb_section.append("; " + draw_ib)
        expect_capture = False
        for category_name in d3d11_game_type.OrderedCategoryNameList:
            category_hash = drawib_model.category_hash_dict.get(category_name, "")
            texture_override_vb_name_suffix = (
                "VB_" + draw_ib + "_" + drawib_model.draw_ib_alias + "_" + category_name
            )
            texture_override_vb_section.append("[TextureOverride_" + texture_override_vb_name_suffix + "]")
            texture_override_vb_section.append("hash = " + category_hash)

            for original_category_name, draw_category_name in d3d11_game_type.CategoryDrawCategoryDict.items():
                if category_name != draw_category_name:
                    continue
                category_original_slot = d3d11_game_type.CategoryExtractSlotDict[original_category_name]
                texture_override_vb_section.append(
                    category_original_slot + " = Resource" + draw_ib + original_category_name
                )

            draw_category_name = d3d11_game_type.CategoryDrawCategoryDict.get("Blend", None)
            if draw_category_name is not None and category_name == draw_category_name:
                # 【每帧机制 t54】① 捕获当帧 palette → ② run attach CS → ③ 才重绑，然后
                # `handling = skip` + `draw`。三行的**方向与顺序**都是硬约束（见
                # `blend_block_order_problems`：写反 = 把空副本写进游戏当帧 palette；
                # 反序 = 自反馈，捕到的是自己刚绑上的骨架）。逐行由步骤① 的渲染器产出，
                # 本模块不另写一份文本。
                family = str(rebind.get("family") or draw_ib)
                range_info = self.himi_family_range(family)
                if range_info is not None:
                    expect_capture = True
                    for line in himi_runtime.render_attach_capture_lines(family):
                        texture_override_vb_section.append(line)
                else:
                    print(
                        f"{_LOG_PREFIX} 提示 {draw_ib}: 家族 {family} 没有专属块（借位族或"
                        "区间不可得）⇒ 本次不发捕获行/attach 段，只重绑 vs-t0"
                    )
                # 与 draw 同一段内绑定，保证重绑与 draw 之间没有别的 pass 插进来。
                texture_override_vb_section.append(
                    "vs-t0 = " + str(rebind["vs_t0_resource"])
                )
                texture_override_vb_section.append("handling = skip")
                exported_rows = int(getattr(drawib_model, "draw_number", 0) or 0)
                resolved = himi_runtime.resolve_deform_draw_count(
                    rebind.get("family_deform_vertex_count"),  # 仅诊断（t48）
                    exported_rows,  # 权威：模组自己上传的行数
                )
                # 物理不变量（t48）：写进 ini 的 draw 不得超过**模组自己上传的行数**
                # （= 同产物 ``override_vertex_count``）。越界读会把角色级 SO 池写爆
                # （t47 实机症状）。这里**只报警、不静默夹取** —— 静默夹取会让离线护栏
                # 永远看不到越界值，而这条链的教训正是「没有护栏 ⇒ 差 68409 行零红」。
                if int(resolved["count"]) > exported_rows:
                    self._himi_notify(
                        f"{draw_ib}: deform 重放 draw（{int(resolved['count'])}）超过模组"
                        f"自身行数（{exported_rows}）⇒ 会越界读并把角色级 SO 池写爆"
                        "（t47 症状：无骨骼约束/错位/上下颠倒）",
                        warning=True,
                    )
                texture_override_vb_section.append(
                    "draw = " + str(int(resolved["count"])) + ", 0"
                )
                if resolved.get("note"):
                    print(
                        f"{_LOG_PREFIX} 提示 {draw_ib}: {resolved['note']}"
                        f"（采用 {int(resolved['count'])}，来源 {resolved['source']}）"
                    )

            if category_name == d3d11_game_type.CategoryDrawCategoryDict["Position"]:
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_vb_section.append("$active0 = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_vb_section.append("$ActiveCharacter = 1")

            texture_override_vb_section.new_line()

        # 【源码级顺序护栏】每个 Blend 块内：捕获 → run 都必须在 `vs-t0 =` 重绑之前。
        # 违反 ⇒ 产物必定坏（自反馈 / 空副本污染游戏 palette），而且**在游戏里完全静默**
        # ⇒ 宁可在这里 Fatal 中止，也不要把写坏的 ini 交给用户。
        problems = blend_block_order_problems(
            texture_override_vb_section.SectionLineList, expect_capture=expect_capture
        )
        # 【F1/F2 的落地判据】变形重放必须**无条件**执行：该族 deform 段的原 draw 已被
        # `handling = skip` 吞掉 ⇒ SO 目标当帧唯一写方就是这一笔。任何条件行（s3 诊断态的
        # `if $seen_… >= 1`）在宿主族段里**恒假** ⇒ SO 全零 ⇒ 整块几何消失（队长两帧实测）。
        # ⇒ 「把 draw 搬到渲染 IB 段」这类修法（报告 50 F2）已证伪（渲染段没有变形 VS /
        # SO 绑定），它的真需求（draw 无条件、不依赖到达次序）由这条判据钉死。
        problems = problems + deform_replay_block_problems(
            texture_override_vb_section.SectionLineList
        )
        if problems:
            from ...utils.format_utils import Fatal

            raise Fatal(
                f"{draw_ib}: 合并骨架 VB 段的每帧机制顺序自检失败 ⇒ 中止导出："
                + "；".join(problems)
            )

        ini_builder.append_section(texture_override_vb_section)

    # ------------------------------------------------------------------ #
    # 资源声明 + 骨架发布（角色级，只发一次）
    # ------------------------------------------------------------------ #

    def _himi_publish_skeleton(self, folder_name: str) -> dict:
        """把角色级骨架写进 mod 的缓冲目录（ini 的 ``filename`` 指向同一处）。"""
        if self._himi_publish_result:
            return self._himi_publish_result
        plan = self.himi_merged_runtime_plan()
        folder_path = GlobalConfig.path_generatemod_buffer_folder()
        # 单一来源核对：ini 里的 `filename = <folder_name>/...` 必须与真实落盘目录同名。
        if folder_name and os.path.basename(os.path.normpath(folder_path)) != folder_name:
            self._himi_notify(
                f"骨架落盘目录（{folder_path}）与 ini 里声明的目录（{folder_name}）"
                "不一致；ini 的 filename 会指到不存在的路径。",
                warning=True,
            )
        try:
            result = himi_runtime.publish_skeleton_binary(plan, folder_path)
        except Exception as exc:  # noqa: BLE001 - 同上：导出通道不得被本模块异常打断
            result = {
                "ok": False,
                "path": "",
                "written": False,
                "bytes": 0,
                "message": f"骨架发布异常：{exc}",
            }
        self._himi_publish_result = result
        if not result.get("ok"):
            self._himi_notify(
                f"角色级骨架未能写入 mod（{result.get('message')}）；"
                "ini 仍然声明了该资源 ⇒ 资源会加载失败（模型会明显异常），请重新导入后导出。",
                warning=True,
            )
        elif result.get("written"):
            print(f"{_LOG_PREFIX} {result.get('message')} → {result.get('path')}")
        return result

    def _himi_family_index_map(self, plan=None) -> dict:
        """【t74/t92】``{family: (own_index, base_index)}``：物体矩阵表（``cs-u1``）里的族序号。

        索引空间**唯一** = :meth:`himi_family_range_items` 的次序（= 发 ini 段的次序），
        导出侧写载荷 ``[7] = (own, base)`` 与运行期 CS 读它必须是同一套，否则每族会拿别族的
        M 去算 X。

        **``base`` 默认恒等于 ``own``（t92）**：每族的骨只由它自己的那笔 draw 消耗，游戏那一刻
        用的就是本族的对象矩阵 ⇒ ``X = M_own⁻¹·M_own = 单位阵``，本族槽写 ``P_f`` 即可。
        跨族只在**借用槽**上出现，而那些槽在本族块尾、由本族的 dispatch 写
        ``X_源 = M_本族⁻¹·M_源``（来源族由 ``BorrowMap`` 指明）—— 所以「宿主族」这个概念
        从写槽路径里彻底退出了。

        唯一的例外是**这族整块几何真的并进了宿主那一笔 draw**（只剩 ≤512 B 的站位股，自己那笔
        draw 只画零面积小三角）：那时它的槽只被宿主那笔读，``base = 宿主`` 才对。这个例外是
        **逐族**的 —— 同一份产物里「真几何的族」与「只剩站位的族」可以并存。

        之前按「Position 缓冲最大者」定宿主、让所有族都 ``base = 宿主``，等于给每个自成
        一 draw 的族乘了一次 ``M_宿主⁻¹·M_族``：实测 ``077983c9``（头发）被宿主
        ``09c6e58a`` 转了 13.66°、平移 0.29 —— 头发在自己位置里却被二次变换。判据必须
        是「这族的几何由哪一笔 draw 渲染」，不是「谁的缓冲最大」。
        """
        if plan is None:
            plan = self.himi_merged_runtime_plan()
        families = [str(family) for family, _info in (self.himi_family_range_items() or [])]
        if not families:
            return {}
        try:
            host = self._himi_host_family(plan)
            host_family = str(host.get("family") or "") if isinstance(host, dict) and host.get("ok") else ""
            if host_family in families and len(families) > 1:
                host_index = families.index(host_family)
                # 【2026-10-26 事故】`sizes` 缺失时**绝不当成「全是站位」**：空字典会让每一族都被
                # 判成 ≤512 B 的站位族 ⇒ 仍在自族那笔 draw 里画几何的族（实例：嗨♪爱愿妖精♥ 的
                # 694025c6 / 70a4da3e）被乘两遍 M_宿主⁻¹·M_族，整块错位。
                sizes = dict((host or {}).get("sizes") or {})
                if sizes:
                    # 逐族判定，**不是**「全体一起判」：站位与真几何可以同时存在于同一份产物
                    # （实例：嗨♪爱愿妖精♥ —— 694025c6 / 70a4da3e 自己那笔 draw 里画真几何，
                    # 而 b35cfc6c / def0c0ae 只留下 3 / 6 个零面积站位顶点）。
                    #   · 站位族（Position ≤ 512 B，实测 132 B = 3 顶点）⇒ 它的骨只被**宿主的**
                    #     那一笔 draw 读（几何已烘进宿主物体空间）⇒ base = 宿主。
                    #   · 其余族 ⇒ 自己那笔 draw 用本族对象矩阵读自己的槽 ⇒ base = 自己（= 单位阵）。
                    # 尺寸**未知**的族一律按「自己那笔」处理：判错的代价是少一层校正，
                    # 而把有效几何判成站位会多乘一层 M_宿主⁻¹·M_族 —— 后者才是肉眼看得到的错位。
                    return {
                        family: (
                            index,
                            host_index
                            if (family != host_family and family in sizes and sizes[family] <= 512)
                            else index,
                        )
                        for index, family in enumerate(families)
                    }
        except Exception:
            pass
        return {family: (index, index) for index, family in enumerate(families)}

    def _himi_publish_attach_ranges(self, plan, folder_name: str) -> dict:
        """【F5 + t39-X + t74 + t78】把每族的身份资源（128 B = base/span/ver/magic + **单位阵** + ``X_f`` + 族索引）写进 mod 缓冲目录。

        ``[1..3]`` 那一格原本存 palette 侧要复合的 ``Q_f⁻¹``；方案 B（t78）起顶点侧不再做 ``Q·v``
        校准，palette 侧也不再乘 Q⁻¹ ⇒ 写槽就是 ``S = X_f · P_f``，这一格**恒为单位阵**（段位与
        长度保留，只为兼容既有载荷布局与 CS 下标；乘单位阵 = 恒等）。

        身份资源缺失时 CS 会**退回冻结表**（= 今天的行为，不是静默乱写），但那样 F5 的
        保护就没了 ⇒ 另有文件存在性硬门 :func:`verify_attach_range_references` 在产物落盘
        那一步把它拦死（与 `cs =` 的硬门同款，不降级为 warning）。

        发布成功后还会用**同一组** ``ranges / placements / family_xs / indices`` 调
        :func:`_himi_verify_attach_range_sync` 复核落盘字节（族数 × 128 B 的四方同步）⇒
        任何一族不一致即 ``Fatal``，不留「CS 读到的 X_f 与计划不符」的产物。
        """
        folder_path = GlobalConfig.path_generatemod_buffer_folder()
        if folder_name and os.path.basename(os.path.normpath(folder_path)) != folder_name:
            self._himi_notify(
                f"身份资源落盘目录（{folder_path}）与 ini 里声明的目录（{folder_name}）"
                "不一致；ini 的 filename 会指到不存在的路径。",
                warning=True,
            )
        ranges = getattr(plan, "family_ranges", None) or {}
        # 【t78 方案 B】`[1..3]` 不再存任何因子：顶点侧不校准 ⇒ palette 侧就是 `S = X_f · P_f`，
        # 这一段**恒为单位阵**（CS 的 Q 段保留、乘单位阵 = 恒等；verify_attach_range_files 会断言
        # 它是单位阵，容差 1e-6 ⇒ 谁再把 Q⁻¹ 塞回来，导出期直接 Fatal）。
        # 因此这里**不再调** `_himi_family_placements()`：Q_f 已彻底退出变换路径。
        placement_rows = {str(family): attach_placement_identity() for family in (ranges or {})}
        family_xs = {}
        try:
            # 【t39-X】族级物体矩阵 X_f：载荷 [4..6] = X_f 的 3×4 行；CS 在算完 `P_f`（乘单位阵）
            # 之后对三条结果行各做一次左乘（X 全零 ⇒ CS 跳过，等价旧行为）。契约钩子已保证
            # 到这里要么全有、要么全无（部分缺失在更早的 `_himi_enforce_contract` 里 Fatal 中止）。
            try:
                family_xs = self._himi_family_x()
                self._himi_publish_family_x_record()
            except Exception as exc:  # noqa: BLE001 - X_f 只是可选增强（载荷 [4..6] 是兜底），
                # 算不出来就把整笔身份资源发布一起拖红没有意义：单位阵与 base/span 仍然要写。
                family_xs = {}
                self._himi_notify(f"族级物体矩阵 X_f 计算失败（{exc}）⇒ 不校准", warning=True)
            # 【t74】族索引：``[7] = (own, base)``，运行期 CS 靠它决定「读表里哪一段当 M_base」。
            # 与 u1 表的 array=（3 × 族数）同源（= ``himi_family_range_items`` 的次序）。
            family_indices = self._himi_family_index_map(plan)
            if family_xs and not family_indices:
                # 兜底 X 还在、但族索引拿不到 ⇒ 运行期分支不启用。不是错误（载荷兜底照样正确），
                # 但必须说清：否则用户会以为「已经实时计算」，实际仍在用抓帧常量。
                self._himi_notify(
                    "族索引不可用（宿主族判定失败或本次没有可发段的族）⇒ 物体矩阵表（cs-u1）"
                    "不启用，X_f 退回导出期烘死的载荷兜底（与旧产物逐字节等价）。",
                    warning=True,
                )
            result = himi_runtime.publish_attach_ranges(
                ranges,
                folder_path,
                placements=placement_rows,
                family_xs=family_xs,
                indices=family_indices,
                skeleton_slots=int(getattr(plan, "slot_count", 0) or 0),
            )
        except Exception as exc:  # noqa: BLE001 - 同上：导出通道不得被本模块异常打断
            result = {"ok": False, "written": False, "files": [], "message": f"身份资源发布异常：{exc}"}
        self._himi_attach_range_result = result
        if result.get("ok"):
            # 【t39-X/t74 硬门】写出去还不够：**落盘字节**必须与「族区间 / 单位阵 / X_f / 族索引 /
            # 128 B」五方一致（`verify_attach_range_files`，其中 `[1..3]` 另有一条「必须是单位阵」
            # 的无条件断言）。这里用**同一组** ranges/placements/family_xs/indices
            # 复核，所以文件缺失、被截断、或某族的摆放/X 与发布值不等都会在导出期被拦下 ——
            # 而不是留一个 CS 读不到 X_f 的产物（那比不校准更坏，与 :1826 的分级同款）。
            self._himi_verify_attach_range_sync(
                folder_path, ranges, placement_rows, family_xs, family_indices
            )
        if not result.get("ok"):
            self._himi_notify(
                f"家族身份资源未能写入 mod（{result.get('message')}）；"
                "ini 仍然声明了它们 ⇒ 资源会加载失败（F5 的 span 上界退化为冻结表）。",
                warning=True,
            )
        elif result.get("written"):
            print(f"{_LOG_PREFIX} {result.get('message')} → {folder_path}")
        return result

    def _himi_verify_attach_range_sync(
        self, folder_path: str, ranges, placements, family_xs, indices=None
    ) -> list:
        """【t39-X 硬门】核对**已落盘**的身份资源与「族区间 / 单位阵 / X_f / 族索引 / 128 B」一致。

        导出期唯一的调用点就是 :func:`_himi_publish_attach_ranges`（发布 ``ok`` 之后、用同一组
        输入复核），因此这条判据不再只活在 oracle / 单测里：文件缺失、被截断、或某族的摆放段 /
        ``X_f`` 与发布值**逐字节不等**都会在这里被拦下 —— 分级与 :1826 的「半校准比不校准更坏」
        同款（``Fatal`` 中止导出），而不是留一个 CS 读不到正确 X_f 的产物。

        返回问题清单（空 = 通过）；``Fatal`` 的局部 import 与 :2108 的既有硬门同款（模块顶部
        不得新增 import —— fake-包单测按文件路径加载本模块，顶部 import 会在收集期炸）。
        """
        from ...utils.format_utils import Fatal

        problems = list(
            himi_runtime.verify_attach_range_files(
                folder_path,
                ranges,
                placements=placements,
                family_xs=family_xs,
                indices=indices,
            )
            or []
        )
        if problems:
            raise Fatal(
                "族级身份资源（t39-X/t74）与发布计划不一致（族数 × 128 B 的四方同步失败）："
                + "；".join(problems[:6])
                + ("；…" if len(problems) > 6 else "")
            )
        return problems

    def himi_attach_range_result(self) -> dict:
        """本次导出的身份资源落盘结果（``{"ok","written","files","message"}``）。"""
        return dict(getattr(self, "_himi_attach_range_result", {}) or {})

    # ------------------------------------------------------------------ #
    # 【t39 历史 / t78 方案 B】族级摆放 Q_f（族 palette 第 0 槽）：现在只作诊断来源
    # ------------------------------------------------------------------ #

    def _himi_family_placements(self) -> dict:
        """``{family: 12 个 float（Q_f，行主序 3×4）}``（不可得 ⇒ 空字典）。

        【t78 方案 B】Q_f **不参与任何变换**：顶点侧不校准、palette 侧只写 ``S = X_f · P_f``、
        身份载荷 ``[1..3]`` 恒为单位阵。这个函数现在只服务于**诊断**（逐行族归属的候选提示）
        与 oracle 的 A1-B 对照，因此三态都不再阻断导出：
        * 全部族都拿到 ⇒ 返回全部；
        * **部分**族拿到 ⇒ 返回空 + ``_himi_placements_problems`` 非空 ⇒ 调用方只打警告；
        * **一个都没拿到**（合成夹具 / 旧工作空间 / 无缓存）⇒ 返回空 + ``_himi_placements_skipped``
          非空 ⇒ 大声警告，产物不受影响（没有任何矩阵依赖它）。

        来源 = 工作空间 ``LOD0/<bare>/ModImpRuntime/<bare>-BoneMatrix.buf`` 的第 0 槽。
        缓存结果，避免一次导出里重复读盘。
        """
        cached = getattr(self, "_himi_placements_cache", None)
        if cached is not None:
            return cached
        plan = self.himi_merged_runtime_plan()
        workspace_root = GlobalConfig.path_workspace_folder()
        placements: dict = {}
        problems: list = []
        for item in getattr(plan, "rebinds", None) or ():
            record = dict(item or {})
            family = str(record.get("family") or record.get("draw_ib") or "").strip()
            if not family or family in placements:
                continue
            info = himi_runtime.load_family_placement(
                workspace_root, str(record.get("unique_str") or "")
            )
            if not info.get("ok"):
                problems.append(f"{family}: {info.get('note')}")
                continue
            placements[family] = list(info["placement"])
        if problems and not placements:
            # 一个都没拿到 ⇒ 退化成"今天的行为"，只大声说一句（不 Fatal）。
            self._himi_placements_skipped = problems
            self._himi_placements_problems = []
            self._himi_placements_cache = {}
            self._himi_notify(
                f"族级摆放 Q_f（t39/t78）跳过：{len(problems)} 个族都取不到 palette 缓存 ⇒ "
                "不影响产物（方案 B 下 Q_f 不参与任何变换：顶点侧不校准、载荷 [1..3] 恒为单位阵）。"
                "第一条原因：" + problems[0],
                warning=True,
            )
            return {}
        self._himi_placements_skipped = []
        self._himi_placements_problems = problems
        self._himi_placements_cache = placements if not problems else {}
        if problems:
            print(f"{_LOG_PREFIX} 族级摆放 Q_f：{len(problems)} 个族不可得 ⇒ 仅影响归属诊断（不参与变换）")
        return self._himi_placements_cache

    def _himi_host_family(self, plan) -> dict:
        """【t39-X】宿主族 = **合并几何整块落在哪一族**（需求②：由代码判定，绝不硬编码）。

        判据（唯一）：Position 缓冲字节数最大的那个族。合并骨架的顶点全部落在宿主族的
        ``<draw_ib>-Position.buf`` 里（``generate_buffer_files`` 按 ``drawib_model.draw_ib`` 落盘），
        其余族只剩骨架占位行 ⇒ 字节数差距是量级级的（实测 1366904 B vs 132~396 B）。
        并列时取字典序第一个（``himi_family_x.pick_host_family``）。

        证据优先用**本次导出**的内存缓冲（``drawib_model.category_buffer_dict`` 在
        ``DrawIBModel.__post_init__`` 里就装配好了，早于任何写盘）；内存里没有任何 Position 时
        （离线/纯 ini 重建）退回**产物 mod 的 Meshes 目录**。两条路都只认「参与合并的族」
        （``himi_family_range_items`` 的族集），避免把无关族算进来。
        """
        # 【t39-X】延迟 import：`tests/test_himi_stub_objects.py` 等 fake 包前言用 `__path__=[]` 的替身包
        # 按文件路径加载本模块，模块级 import 会让那些既有测试在**收集期**就 ImportError。
        from ...common import himi_family_x

        allowed = set()
        ranges = getattr(plan, "family_ranges", None) if plan is not None else None
        if ranges:
            allowed = {str(k) for k in ranges.keys()}
        else:
            for family, _info in (self.himi_family_range_items() or []):
                allowed.add(str(family))
        source = "本次导出的内存 Position 缓冲"
        sizes = {}
        for drawib_model in (getattr(self, "drawib_model_list", None) or []):
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            if not family or (allowed and family not in allowed):
                continue
            buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
            size = _himi_buffer_byte_size(buffers.get("Position"))
            if size > sizes.get(family, 0):
                sizes[family] = size
        if not any(size > 0 for size in sizes.values()):
            mod_root = himi_family_x.resolve_mod_root(GlobalConfig.path_workspace_folder())
            sizes = {}
            for family, size in (himi_family_x.position_buffer_sizes(mod_root) or {}).items():
                if allowed and str(family) not in allowed:
                    continue
                sizes[str(family)] = int(size or 0)
            source = f"产物 mod 的 Meshes 目录（{mod_root}）"
        picked = himi_family_x.pick_host_family(sizes)
        if not picked.get("ok"):
            return {"ok": False, "family": "", "size": 0, "source": source,
                    "note": picked.get("note") or "没有任何族的 Position 缓冲可用于判定宿主族"}
        # 【2026-10-26】``sizes`` 必须随判定结果一起返回：``_himi_family_index_map`` 要用它区分
        # 「几何真的并进宿主一笔 draw」（其余族只剩 ≤512 B 站位股）与「各族自己还有真几何」。
        # 少了这个键，那条判据会退化成「全是站位」⇒ 每个自成 draw 的族都被乘两遍族级 X。
        return {"ok": True, "family": str(picked.get("family") or ""),
                "size": int(sizes.get(str(picked.get("family") or ""), 0) or 0), "source": source,
                "note": picked.get("note") or "",
                "sizes": {str(key): int(value or 0) for key, value in sizes.items()}}

    def _himi_family_x_none(self, reason: str) -> dict:
        """【t39-X】「一个族都没有 X_f」= 第三态：**不校准 + 大声警告**，绝不中止。

        老工作空间没有抓帧、宿主族判不出来、抓帧里没有任何一族有合法对象矩阵 —— 这些都算
        「全无」。CS 读到全零 X ⇒ 跳过左乘 ⇒ 与旧行为**逐字节等价**（产物仍可用，只是各族
        仍按宿主族摆放，实测偏差 1.0~3.8 游戏单位）。
        """
        self._himi_family_x_skipped = [str(reason or "没有任何一族能给出合法 X_f")]
        self._himi_family_x_problems = []
        self._himi_family_x_cache = {}
        self._himi_notify(
            "族级物体矩阵（t39-X）：" + str(reason or "没有任何一族能给出合法 X_f")
            + " ⇒ 本次导出**不做 X_f 校准**（CS 里 X 全零 ⇒ 跳过左乘，与旧行为逐字节等价）；"
            "产物仍可用，只是各族仍按宿主族摆放。",
            warning=True,
        )
        return {}

    def _himi_family_x(self) -> dict:
        """【t39-X】``{family: 12 float}``：X_f = M_base⁻¹ · M_f（抓帧 vs-cb3 的对象矩阵）。

        三态（与 ``_himi_family_placements`` 同款）：
          * **全部**参与族都有合法 X_f ⇒ 返回全部（= 校准）；
          * **部分**缺失/不合法（0 < 有 X 的族 < 参与族）⇒ ``_himi_family_x_problems`` 非空、
            缓存清空（契约钩子据此 Fatal 并**指名缺哪族、为什么**）；
          * **一个都没有**（老工作空间没有抓帧 / 宿主族判不出来）⇒ ``_himi_family_x_skipped``
            非空 + 大声警告，**不校准**。
        """
        from ...common import himi_family_x

        cached = getattr(self, "_himi_family_x_cache", None)
        if cached is not None:
            return cached
        self._himi_family_x_cache = {}
        self._himi_family_x_problems = []
        self._himi_family_x_skipped = []
        self._himi_family_x_record = {}
        if not bool(GlobalProterties.import_merged_vgmap()):
            return {}
        plan = self.himi_merged_runtime_plan()
        if not getattr(plan, "has_rebinds", False):
            return {}
        allowed = set()
        for family, _info in (self.himi_family_range_items() or []):
            allowed.add(str(family))
        if not allowed:
            return {}
        host = self._himi_host_family(plan)
        if not host.get("ok"):
            return self._himi_family_x_none("宿主族判定失败：" + str(host.get("note") or ""))
        host_family = str(host.get("family") or "")
        if host_family not in allowed:
            return self._himi_family_x_none(
                f"宿主族 {host_family} 不在本次合并的族里（{len(allowed)} 族）"
            )
        participants = himi_family_x.participants_from_plan(plan)
        family_bares = {}
        for family in sorted(allowed):
            bare = str(participants.get(family) or "").strip()
            if bare:
                family_bares[family] = bare
        if not family_bares:
            return self._himi_family_x_none("没有任何参与族能给出部件名")
        workspace_root = GlobalConfig.path_workspace_folder()
        host_evidence = str(host.get("note") or "")
        # 【X 源】逐族定源（common/himi_family_x.py::resolve_family_x）：
        #   1. **palette 反解** —— 用本次工作空间的族 palette（导出时现算）：同一根骨在两族
        #      palette 里是同一份表达 ⇒ X_f = P_base[k]·P_f[j]⁻¹。不读帧分析目录，因此不会拿到
        #      「上一次场景」的 object matrix —— 改过拆并关系后，那正是会过期的来源。
        #   2. **抓帧 vs-cb3** —— 只给 palette 解不出的族兜底（单槽族 / 与宿主无共享骨），
        #      且按「配置里那帧 vs 最新一帧」判新鲜度；换源与逐族来源都写进 FamilyX 记录。
        record = himi_family_x.resolve_family_x(
            workspace_root,
            host_family=host_family,
            family_bares=family_bares,
            host_evidence=host_evidence,
            mod_root=himi_family_x.resolve_mod_root(workspace_root),
            prefer_freshest=True,
        )
        fallback_families = sorted(
            family
            for family, value in dict((record or {}).get("sources") or {}).items()
            if str(value) == "frame-vs-cb3"
        )
        if fallback_families:
            self._himi_notify(
                "族级物体矩阵 X_f："
                + str(len(fallback_families))
                + " 族 palette 反解不出（"
                + "、".join(fallback_families[:3])
                + "）⇒ 这几族退回抓帧 vs-cb3 兜底（"
                + str(
                    (record or {}).get("frame_note")
                    or (record or {}).get("source_frame")
                    or "抓帧目录"
                )
                + "）；逐族来源已写进 Config/HIMIMergedSkeleton.json 的 FamilyX.Sources",
                warning=True,
            )
        self._himi_family_x_record = record or {}
        problems = list((record or {}).get("problems") or [])
        families = dict((record or {}).get("x") or {})
        if not families:
            # 【一个都没有】= 第三态：不校准 + 大声警告（老工作空间没有抓帧就是这条路）。
            return self._himi_family_x_none(
                "；".join(problems[:4]) if problems else "抓帧里没有任何一族有合法对象矩阵"
            )
        if problems:
            # 【部分】= 有族算不出 X_f。权威在运行期（CS 每帧按骨算），这里只记录问题、
            # 把拿到的族照常交出去；缺的族在载荷里落零 ⇒ CS 跳过左乘（退回单位阵）。
            self._himi_family_x_problems = problems
            self._himi_family_x_cache = families
            return families
        missing = sorted(set(allowed) - set(families))
        if missing:
            self._himi_family_x_problems = [
                f"{family}: 没有可用的 X_f（抓帧 vs-cb3 里没有合法对象矩阵）" for family in missing
            ]
            self._himi_family_x_cache = families
            return families
        self._himi_family_x_cache = families
        print(
            f"{_LOG_PREFIX} 族级物体矩阵：{len(families)} 族"
            f"（宿主 {host_family}；来源 {(record or {}).get('source_frame') or '抓帧'}）"
        )
        return self._himi_family_x_cache
    def _himi_publish_family_x_record(self) -> dict:
        """【t39-X】把 X_f 的审计记录写进 ``Config/HIMIMergedSkeleton.json`` 顶层 ``FamilyX``。

        纯数据层记录（来源帧目录名、每族所用 draw/来源文件、对偶命中数与最大残差、是否单位阵），
        供审计；写不进去只警告（绝不让记录失败拖垮导出）。
        """
        from ...common import himi_family_x

        record = getattr(self, "_himi_family_x_record", None) or {}
        if not record:
            return {"ok": False, "path": "", "note": "没有 X_f 记录（本次导出没有做族级物体矩阵）"}
        host = self._himi_host_family(self.himi_merged_runtime_plan())
        audit = himi_family_x.family_x_audit_record(
            record,
            host_evidence=str(host.get("note") or ""),
            mod_root=himi_family_x.resolve_mod_root(GlobalConfig.path_workspace_folder()),
        )
        # 【t78 方案 B】审计记录里的口径必须跟着变换公式走：写槽是 `S = X_f · P_f`（顶点侧不校准、
        # palette 侧不乘 Q_f⁻¹）。覆盖 `family_x_audit_record` 里的历史文案，并显式记一条
        # `VertexCalibration: "none"` 供离线 oracle / 测试读取（第 4 项契约元数据）。
        audit["Algorithm"] = "S = X_f · P_f（方案 B：顶点侧不校准，palette 侧不乘 Q_f⁻¹）"
        audit[VERTEX_CALIBRATION_KEY] = VERTEX_CALIBRATION_NONE
        path = os.path.join(
            GlobalConfig.path_workspace_folder(), "Config", HIMI_MERGED_SKELETON_METADATA_FILE
        )
        result = himi_family_x.write_family_x_record(path, audit)
        if not result.get("ok"):
            self._himi_notify(
                "族级物体矩阵记录写入失败：" + str(result.get("note") or ""), warning=True
            )
        return result

    def _himi_vertex_family_index(self) -> dict:
        """``{family: set((x,y,z) 量化)}``：逐行判「**顶点自己**属于哪个族」（t41/T40-F2）。

        为什么不能只看 Blend：Blend 记的是**骨头槽位**（骨头族），而顶点族是"这个顶点原本
        来自哪个族"—— 合并网格整个落在宿主族的缓冲里，这个信息只存在于各族**源绑定坐标**里。
        t40 实测：顶点族 == 骨头族 30355 行、跨族 699 行、源坐标匹配失败 395 行（1.27%）。
        **按族分桶**返回：查询时先查骨头族（t40 的归属口径），再按族次序找；都找不到 ⇒ 调用方
        退回骨头族（= 与不校准等价，安全）。
        """
        cached = getattr(self, "_himi_vertex_family_cache", None)
        if cached is not None:
            return cached
        plan = self.himi_merged_runtime_plan()
        members = []
        for item in getattr(plan, "rebinds", None) or ():
            record = dict(item or {})
            family = str(record.get("family") or record.get("draw_ib") or "").strip()
            unique_str = str(record.get("unique_str") or "")
            if family and unique_str and (family, unique_str) not in members:
                members.append((family, unique_str))
        info = himi_runtime.load_family_position_index(
            GlobalConfig.path_workspace_folder(), members
        )
        if not info.get("ok"):
            self._himi_notify(
                "族级归属诊断（t41/t78）：读不到各族的**源绑定坐标** ⇒ 逐行归属退回「骨头族」"
                f"（= 不校准等价；{info.get('note')}）",
                warning=True,
            )
        elif info.get("missing"):
            self._himi_notify(
                f"族级归属诊断（t41/t78）：{info.get('note')}；缺的族按骨头族回退",
                warning=True,
            )
        self._himi_vertex_family_cache = dict(info or {})
        return self._himi_vertex_family_cache

    def _himi_bone_family_spans(self, plan) -> list:
        """``[(base, end, family), …]``：槽位 → 族的**权威**查表（与 palette 侧同一张
        ``plan.family_ranges``，即 ``compute_family_ranges`` 的产物）。

        t40/T40-F2 的教训：顶点侧**不能**按 ``drawib_model.draw_ib`` 选 Q —— 合并网格的
        31054 行**全在宿主族的 Position 缓冲**里，按 DrawIB 取就等于"整块乘 Q_host⁻¹"，
        而 palette 侧是按**骨头族**复合的 ⇒ 26477 行「顶点族 == 骨头族」的正常同族顶点
        被误位移（实测 max 3.937792）。正确口径是**逐行按该行骨头所属族**取 Q。
        """
        spans = []
        for family, info in (getattr(plan, "family_ranges", None) or {}).items():
            info = dict(info or {})
            base = int(info.get("base", 0) or 0)
            slots = int(info.get("slots", 0) or 0)
            if slots > 0:
                spans.append((base, base + slots, str(family)))
        spans.sort()
        return spans

    @staticmethod
    def _himi_row_bone_families(blend_bytes: bytes, spans: list, row_floats: int) -> tuple:
        """逐行算「该行骨头所属族」：``(families, stats)``。

        * ``families[i]`` = 第 ``i`` 行的族名（``""`` = 没有活动通道 / 无法归属）；
        * 活动通道 = ``weight > 0``（与 deform VS 的加权求和同口径）；
        * 一行里出现**多个族** ⇒ 记 ``mixed`` 并按**权重最大**的通道归属（实测本角色
          **0 行混族** ⇒ 良定义；仍留计数，供硬门盯住）。
        """
        import numpy

        families: list = []
        stats = {"rows": 0, "mixed": 0, "unweighted": 0, "unknown_slot": 0}
        if not blend_bytes:
            return families, stats
        if len(blend_bytes) % 32:
            stats["blend_misaligned"] = len(blend_bytes) % 32
            return families, stats
        weights = numpy.frombuffer(blend_bytes, dtype="<f4").reshape(-1, 8)[:, 0:4]
        indices = numpy.frombuffer(blend_bytes, dtype="<i4").reshape(-1, 8)[:, 4:8]
        rows = min(int(weights.shape[0]), int(row_floats))
        for row in range(rows):
            active = [(float(w), int(v)) for w, v in zip(weights[row], indices[row]) if w > 0.0]
            if not active:
                stats["unweighted"] += 1
                families.append("")
                continue
            seen = {}
            for weight, slot in active:
                family = ""
                for base, end, name in spans:
                    if base <= slot < end:
                        family = name
                        break
                if not family:
                    stats["unknown_slot"] += 1
                    continue
                seen[family] = seen.get(family, 0.0) + weight
            if not seen:
                families.append("")
                continue
            if len(seen) > 1:
                stats["mixed"] += 1
            families.append(max(seen.items(), key=lambda item: item[1])[0])
        stats["rows"] = len(families)
        return families, stats

    def _himi_calibrate_bind_positions(self) -> dict:
        """【t39 历史 / t78 方案 B】**只读**的逐行「几何族 vs 骨头族」诊断 —— 不改写任何字节。

        方案 B（现行）：顶点侧**不**做族级校准，Position 缓冲**原样落盘**；palette 侧只写
        ``S = X_f · P_f``（不乘 ``Q_f⁻¹``）。同族 ``P·Q⁻¹·Q·v ≡ P·v`` 恒等（残差 4.6e-16）
        ⇒ 对基态逐位等价；而形态键通道（``-Position_merged_packed_pos_delta.buf`` 的 δ 直接叠加
        到基态行上）因此天然与基态同空间 —— 以前顶点被 Q 搬过，运行期就成了 ``Q·v+δ`` 而不是
        ``Q·(v+δ)``，非宿主族整块撕裂。

        本函数保留的价值：**读**每行顶点认领到的族与骨头族的差异，输出逐族行数、跨族借用行数、
        标签翻转/多族歧义/混族行等数字（t24 的保守语义、t41/t43 的容差最近邻判据仍在此处）。

        **跨族借用行在方案 B 下不需要修**：LBS 是 ``world = Σ w_i·S[c_i]·v``，作用在 v 上的任何
        常量都能搬到槽上；五族顶点同处一个角色空间（实测 bbox 同处身高 1.69、脚 y≈0）⇒ 槽里的
        ``X_f·P_f[k]`` 对**任何族**的顶点都给真值 ``M_f·P_f[k]·v``。旧方案因顶点被乘 Q_f，跨族
        会多出 ``Q_g⁻¹·Q_f``（各族 Q 两两相差 133°–180°）⇒ 必然错。**跨族行 > 0 只记一条警告**
        （真实产物该值为 0；出现即说明有新的族间共享权重形态，需要人看）。

        返回 ``{"ok","families","rows","skipped","per_family","mixed","unweighted","cross",
        "claimed","label_flips","cross_flips","ambiguous","message"}``；**无** ``cross_max_shift``
        （没有顶点侧变换就没有位移）；**不抛**（异常由上层硬门处理）。
        """
        plan = self.himi_merged_runtime_plan()
        if not getattr(plan, "has_rebinds", False):
            # 非合并路径：产物与改造前逐字节相同（本任务不碰）。
            return {"ok": True, "families": 0, "rows": 0, "skipped": 0,
                    "per_family": {}, "mixed": 0, "unweighted": 0,
                    "message": "非合并路径：不做逐行族归属诊断"}
        # 【t78 方案 B】族级摆放 Q_f 已经**完全不参与**变换，这里不再取 `_himi_family_placements()`、
        # 也不再校验可逆性：曾经是「顶点乘 Q_f + palette 乘 Q_f⁻¹」成对使用，现在两侧都不乘。
        spans = self._himi_bone_family_spans(plan)
        if not spans:
            return {"ok": True, "families": 0, "rows": 0, "skipped": 0,
                    "per_family": {}, "mixed": 0, "unweighted": 0,
                    "message": "plan.family_ranges 为空 ⇒ 跳过逐行族归属诊断（顶点侧不校准，产物不受影响）"}
        rows = 0
        calibrated_families = 0
        skipped = 0
        debug_skip = ""
        mixed_total = 0
        unweighted_total = 0
        cross_total = 0
        claimed_total = 0
        label_flips = 0
        cross_flips = 0
        ambiguous_total = 0
        per_family: dict = {}
        vertex_index_enabled = True
        import numpy

        for drawib_model in self.drawib_model_list or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
            position = buffers.get("Position")
            if position is None or getattr(position, "dtype", None) is None:
                skipped += 1
                if not debug_skip:
                    debug_skip = "族 %s：buffers=%s；Position 类型=%s" % (
                        family, sorted(buffers)[:8], type(position).__name__,
                    )
                continue
            # 缓冲是**原始字节**（numpy uint8 一维，`.tofile()` 直接落盘）；44 B/行 = P12 N12 C4 TA16
            # ⇒ 每行 **11 个 little-endian float32**，前 3 个是 POSITION、3..5 NORMAL、
            # 6 COLOR、7..10 TANGENT(含 w)。**按字节解释**而不是假设 dtype（不然会把字节
            # 当 float 读 ⇒ 写出天文数字，实测踩过）。
            block = numpy.asarray(position)
            floats = numpy.frombuffer(block.tobytes(), dtype="<f4").copy()
            if floats.size % 11:
                skipped += 1
                if not debug_skip:
                    debug_skip = "族 %s：Position 字节 %d 不是 44 的整数倍（%s）" % (
                        family, block.nbytes, getattr(block, "shape", None),
                    )
                continue
            view2d = floats.reshape(-1, 11)
            blend = buffers.get("Blend")
            blend_bytes = numpy.asarray(blend).tobytes() if blend is not None else b""
            row_families, stats = self._himi_row_bone_families(
                blend_bytes, spans, int(view2d.shape[0])
            )
            # 【t41 / T40-F2 / t78】逐行判「**顶点自己**的族」（先按源绑定坐标认领，认不到退回骨头族）。
            # 方案 B 下这个数字**只用于诊断**：顶点缓冲原样落盘，跨族借用靠槽里的 `X_f·P_f` 自动正确
            # （LBS 的常量可以搬到槽上）。留着它是因为「跨族行数」是唯一能早期发现族间共享权重的信号
            # （真实产物该值 = 0；> 0 时打一条警告，见函数尾部）。
            vertex_index = self._himi_vertex_family_index() if vertex_index_enabled else {}
            family_order = [name for _base, _end, name in spans]
            cross_rows = 0
            claimed = 0
            for row_index, bone_family in enumerate(row_families):
                if not bone_family:
                    continue
                point = (float(view2d[row_index, 0]), float(view2d[row_index, 1]),
                         float(view2d[row_index, 2]))
                # 【t43 / T42-F2】**容差内最近邻**（不再是精确量化键 round(x*1e5)）：
                # 精确键对量化边界敏感 —— 两次独立导出的逐族行数会漂 ±4~5；漂移若落在跨族行
                # 上会换一个 Q（最多 ~3.7 单位位移）。容差 1e-4（依据见 runtime 的
                # `load_family_position_index` docstring：float32 + 重算噪声 ≲1e-5，几何尺度 1.81）。
                # 并列时优先骨头族（保守：判成同族 ⇒ 该行不动），再按族次序。
                # 【t24】法线/切线随行入判据：焊接点的两族拷贝只差 ~1e-8（float32 重舍入），
                # 位置胜负是掷硬币（94 行污染的病根，见报告 26 §5.1）⇒ 属性是第二证据；
                # 判定顺序与保守语义都在 nearest_family 内（并列 ⇒ prefer 骨头族；
                # 属性只在 prefer 没投票时做「唯一命中」改判；判不出 ⇒ 严格最近）。
                row_normal = _finite_vec3(view2d[row_index, 3], view2d[row_index, 4],
                                          view2d[row_index, 5])
                row_tangent = _finite_vec3(view2d[row_index, 7], view2d[row_index, 8],
                                           view2d[row_index, 9])
                hit = himi_runtime.nearest_family(
                    vertex_index, point, family_order=family_order, prefer=bone_family,
                    normal=row_normal, tangent=row_tangent,
                )
                vertex_family = str(hit.get("family") or "")
                if hit.get("ambiguous"):
                    ambiguous_total += 1
                exact_family = himi_runtime.exact_key_family(vertex_index, point)
                if exact_family != vertex_family:
                    label_flips += 1
                    if (bool(exact_family) and exact_family != bone_family) != (
                        bool(vertex_family) and vertex_family != bone_family
                    ):
                        cross_flips += 1
                if vertex_family:
                    claimed += 1
                    if vertex_family != bone_family:
                        cross_rows += 1        # 只计数：方案 B 下跨族借用不需要顶点侧修正
                else:
                    vertex_family = bone_family          # 安全回退：与骨头族同族 ⇒ 不校准等价
                row_families[row_index] = vertex_family
            stats["claimed"] = claimed
            stats["cross"] = cross_rows
            cross_total += cross_rows
            claimed_total += claimed
            mixed_total += int(stats.get("mixed", 0))
            unweighted_total += int(stats.get("unweighted", 0))
            if len(row_families) != int(view2d.shape[0]):
                # Blend 行数对不上 ⇒ 逐行归属不可信：**拒绝**按整块校准（宁可不校，也不误校）。
                skipped += 1
                if not debug_skip:
                    debug_skip = "族 %s：Blend 行数 %d ≠ Position 行数 %d" % (
                        family, len(row_families), int(view2d.shape[0]),
                    )
                continue
            # 【t78 方案 B】这里**不再改写缓冲**：原来这块把每行 44 B 按族乘 Q_f 再
            # `target[:] = ...` 写回，是形态键通道与基态分家的唯一源头。现在只按族数行数（诊断），
            # Position 保持原样；下面的 `view2d` 只读（不 copy、不赋值），`block` 从不被写回。
            for row_family in sorted({name for name in row_families if name}):
                count = sum(1 for name in row_families if name == row_family)
                per_family[row_family] = per_family.get(row_family, 0) + int(count)
            rows += sum(1 for name in row_families if name)
            calibrated_families = len(per_family)
        result = {
            "ok": rows > 0,
            "families": calibrated_families,
            "rows": rows,
            "skipped": skipped,
            "per_family": dict(per_family),
            "mixed": mixed_total,
            "unweighted": unweighted_total,
            "cross": cross_total,
            "claimed": claimed_total,
            "label_flips": label_flips,
            "cross_flips": cross_flips,
            "ambiguous": ambiguous_total,
            "message": (
                f"逐行族归属诊断（t78 方案 B：顶点侧不校准、缓冲未改写）：{rows} 行；"
                f"逐族行数 {dict(sorted(per_family.items()))}；跨族借用 {cross_total} 行、"
                f"源坐标认领 {claimed_total} 行、标签翻转 {label_flips}（跨族翻转 {cross_flips}）、"
                f"容差内多族 {ambiguous_total}、混族行 {mixed_total}、"
                f"无活动通道行 {unweighted_total}"
                + (f"；{skipped} 个 drawib 未涉及" if skipped else "")
                + (f"（{debug_skip}）" if debug_skip else "")
            ),
        }
        # 方案 B 的 R1 口径（coordinator 复审后撤销了「跨族行 > 0 ⇒ Fatal」）：跨族借用**不需要**
        # 顶点侧修正（LBS 的常量可以搬到槽上），但真实产物该值为 0 —— 出现就记一条警告让人看见。
        if cross_total:
            self._himi_notify(
                f"检测到跨族借用权重 {cross_total} 行，方案 B 下按目标族空间正确换算"
                "（槽里存 X_f·P_f ⇒ 对任何族的顶点都给 M_f·P_f·v；顶点缓冲未被改写）",
                warning=True,
            )
        self._himi_calibration_result = result
        print(f"{_LOG_PREFIX} {result['message']}")
        return result

    def _himi_write_blend_origin(self) -> dict:
        """【t49 / t47 方案 A+C】旁写 ``Meshes/<fam>-BlendOrigin.buf`` + 越界引用计数。

        方案 A（旁文件）：8 B/顶点 = ``[local_slot(u32), flags(u32)]``，行序与 ``<fam>-Blend.buf``
        逐行对齐；``flags.bit0`` = 该顶点**存在越界引用**（≥1 个活动通道的全局索引不在本族区段）。
        方案 C（计数日志）：每个族一行 ``family=<fam> vertices=<n> out_of_range_refs=<N> (<pct>%)``
        + 一行汇总 ``out_of_range_refs=<N> (<pct>%)``；``N > 0`` 时**告警**（控制台 + 产物 ini 注释块）。

        为什么需要它（t47）：产物里的**全局索引**是被写坏的（源数据 12/12 组件越界 0，而产物
        `2c706fbd` 有 449 行、取值只有 275、2.21%）⇒ 让导出自己把「本族局部槽号」旁写一份 +
        把越界条数报出来，"导出到底把哪些顶点写越界了"就不再需要逆向猜。

        **不改变任何既有产物的语义**：只新增一个文件 + 一行日志/注释。
        """
        out_of_range_total = 0
        vertices_total = 0
        per_family: list = []
        written: list = []
        problems: list = []
        folder = GlobalConfig.path_generatemod_buffer_folder()
        spans = self._himi_bone_family_spans(self.himi_merged_runtime_plan())
        range_of = {name: (base, end - base) for base, end, name in spans}
        for drawib_model in self.drawib_model_list or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            buffers = getattr(drawib_model, "category_buffer_dict", None) or {}
            blend = buffers.get("Blend")
            if blend is None:
                continue
            info = range_of.get(family)
            if info is None:
                problems.append(f"{family}: 不在 plan.family_ranges 里 ⇒ 无法判越界")
                continue
            base, span = info
            try:
                import numpy

                raw = numpy.asarray(blend).tobytes()
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{family}: 读 Blend 缓冲失败（{exc}）")
                continue
            result = himi_runtime.blend_origin_rows(raw, base=base, span=span)
            if not result.get("ok"):
                problems.append(f"{family}: {result.get('message')}")
                continue
            gate = himi_runtime.blend_origin_problems(
                result["rows"], span=span, vertices=result["vertices"]
            )
            problems.extend(f"{family}: {item}" for item in gate)
            try:
                path = os.path.join(folder, himi_runtime.blend_origin_basename(family))
                with open(path, "wb") as handle:
                    handle.write(himi_runtime.blend_origin_payload(result["rows"]))
                written.append(path)
            except OSError as exc:
                problems.append(f"{family}: 写 {family}-BlendOrigin.buf 失败（{exc}）")
            vertices = int(result["vertices"])
            out_of_range_total += int(result["out_of_range"])
            vertices_total += vertices
            pct = (100.0 * result["out_of_range"] / vertices) if vertices else 0.0
            per_family.append({
                "family": family, "vertices": vertices,
                "out_of_range": int(result["out_of_range"]),
                "pct": pct,
                "all_channels_out": int(result["all_channels_out"]),
                "local_unavailable": int(result["local_unavailable"]),
                "path": path if written and written[-1].startswith(folder) else "",
            })
            print(f"{_LOG_PREFIX} family={family} vertices={vertices} "
                  f"out_of_range_refs={result['out_of_range']} ({pct:.2f}%)")
        pct_total = (100.0 * out_of_range_total / vertices_total) if vertices_total else 0.0
        print(f"{_LOG_PREFIX} out_of_range_refs={out_of_range_total} ({pct_total:.2f}%)")
        summary = {
            "ok": not problems,
            "written": written,
            "families": per_family,
            "out_of_range_refs": out_of_range_total,
            "vertices": vertices_total,
            "pct": pct_total,
            "problems": problems,
        }
        self._himi_blend_origin_result = summary
        if out_of_range_total:
            notices = getattr(self, "_himi_blend_origin_notices", None)
            if notices is None:
                notices = []
                self._himi_blend_origin_notices = notices
            bad = [item for item in per_family if item["out_of_range"]]
            notices.append(
                "越界引用自检（t49/方案 C）：out_of_range_refs=%d (%.2f%%)；涉及族 %s；"
                "全通道越界行 %d、本族局部槽不可得行 %d —— 源数据越界 0，这些是导出侧写进去的"
                % (out_of_range_total, pct_total,
                   ", ".join("%s:%d" % (item["family"], item["out_of_range"]) for item in bad),
                   sum(item["all_channels_out"] for item in bad),
                   sum(item["local_unavailable"] for item in bad))
            )
            self._himi_notify(notices[-1], warning=True)
        return summary

    def _himi_remap_ordinary_blend_to_local(self) -> dict:
        """【t73·兜底原则】降级/普通导出写**局部编号**：`local = global − family_base`（本族槽）。

        为什么要它：普通模式没有运行时合并骨架，游戏按**部件局部编号**查骨。若这份 mesh 是
        「开关开着」导入的（组名/编号是全局的），降级出来的产物在游戏里会整块不显示 ⇒
        降级必须先把几何的编号搬回局部（用户裁定里的第 3 条）。

        口径与证据（t67 权威表，1163 行，逐行核对过）：

        * **本族槽**（`slot ∈ [base_f, base_f + slots_f)`）⇒ 减基址。`base_f = 0` 的族是恒等；
          `def0c0ae` 那族表里写得很清楚：`local 49 ↔ global 341`（341 − 292 = 49 ✓）；
        * **跨族槽**（槽落在**别族**块里）⇒ **不做减法**（否则 `275 − 273 = 2` 是个错值）：
          表里对 `LOD0.2c706fbd-7737-47199` 的 `275` 明确写「越族：275 ∉ [0,184)（该槽属于别的族）」
          且 `should_local_slot` **为空** ⇒ **无法回映射**（t51 的逐行取源恢复表才是那条路的答案）。
          这里记数 + **大声警告**（控制台 + 产物 ini 注释块），并在诊断里点名；
        * 没有合并计划（开关关闭的普通导出）⇒ `family_ranges` 为空 ⇒ **不做任何改写**（那时 mesh
          的编号本来就是局部的）。

        返回账目：``{"remapped", "cross_family", "skipped"}``。
        """
        plan = self.himi_merged_runtime_plan()
        ranges = dict(getattr(plan, "family_ranges", None) or {})
        ledger = {"remapped": 0, "cross_family": 0, "skipped": not bool(ranges)}
        if not ranges or self._himi_merged_attach_active():
            return ledger
        base_of = {}
        for name, info in ranges.items():
            try:
                base_of[str(name)] = (int(dict(info).get("base", 0) or 0),
                                      int(dict(info).get("slots", 0) or 0))
            except Exception:  # noqa: BLE001
                continue
        if not base_of:
            ledger["skipped"] = True
            return ledger
        try:
            import numpy
            import struct
        except Exception:  # noqa: BLE001
            return ledger
        for drawib_model in self.drawib_model_list or ():
            family = str(getattr(drawib_model, "draw_ib", "") or "")
            blend = (getattr(drawib_model, "category_buffer_dict", None) or {}).get("Blend")
            if blend is None:
                continue
            arr = numpy.asarray(blend)
            raw = arr.tobytes()
            rows = len(raw) // 32
            buf = bytearray(raw)
            for row in range(rows):
                weights = struct.unpack_from("<4f", buf, row * 32)
                indices = list(struct.unpack_from("<4i", buf, row * 32 + 16))
                touched = False
                for channel, (weight, slot) in enumerate(zip(weights, indices)):
                    if weight <= 0.0:
                        continue
                    owner = None
                    for name, (base, slots) in base_of.items():
                        if slots > 0 and base <= slot < base + slots:
                            owner = (name, base)
                            break
                    if owner is None or owner[1] == 0:
                        continue                      # 不属于任何族块 / base=0 ⇒ 恒等
                    if owner[0] == family:            # 本族 ⇒ 减基址
                        indices[channel] = int(slot) - owner[1]
                        ledger["remapped"] += 1
                        touched = True
                    else:                             # 跨族 ⇒ 不能减（t67 表：无法回映射）
                        ledger["cross_family"] += 1
                if touched:
                    struct.pack_into("<4i", buf, row * 32 + 16, *indices)
            try:
                arr.view(numpy.uint8)[:] = numpy.frombuffer(bytes(buf), dtype=numpy.uint8)
            except Exception:  # noqa: BLE001 - 写不回去就保持原样（绝不写坏）
                ledger["remapped"] = 0
                ledger["cross_family"] = 0
                ledger["skipped"] = True
        notices = list(getattr(self, "_himi_ordinary_local_remap_notices", None) or [])
        if ledger["remapped"]:
            text = ("普通模式已把 %d 个本族槽搬回**局部编号**（local = global − family_base，"
                    "t67 权威表口径）" % ledger["remapped"])
            print(f"{_LOG_PREFIX} {text}")
            notices.append(text)
        if ledger["cross_family"]:
            text = ("普通模式**无法回映射** %d 个跨族槽（槽落在别族块里，减基址会得到错值；"
                    "t67 权威表对这类行只写「越族」且 should_local_slot 为空）⇒ 这些行在普通模式里"
                    "没有正确的局部编号（要走合并骨架，或先用 t51 的逐行取源恢复表取源局部槽）"
                    % ledger["cross_family"])
            print(f"{_LOG_PREFIX} 警告 {text}")
            self._himi_notify(text, warning=True)
            notices.append(text)
        self._himi_ordinary_local_remap_notices = notices
        return ledger


    def _himi_remap_merged_blend_to_blocks(self) -> dict:
        """【t84】合并路径：把每个 DrawIB 的 Blend 索引重定到**它所属族的新块**。

        为什么必须在写盘前做：``Meshes/<draw_ib>-Blend.buf`` 里那 4 个 u32 索引就是
        游戏 deform VS 查合并骨架的槽号。t84 之后每族的块不再等于「全体族的全局槽」：
        自己的槽搬到了新基址，跨族借用的骨搬到了**本族块尾**。索引不跟着重定，顶点就会
        查到别人的槽（几何跟着别人的骨骼跑）。

        换算三级（对每个权重 > 0 的通道）：

        1. **本族注册槽** ``i ∈ [own_base, own_base + slots)`` ⇒ ``i``（旧全局编号）；
        2. **借用槽**（``i`` 落在别族块里）⇒ ``i``，再经本族 ``family_slot_map`` 落到块尾目标槽；
        3. **局部编号**（``i ∈ [0, slots)`` 但不在本族注册范围里，且命名空间判据不是 global）
           ⇒ ``own_base + i``。这一条修的是真实产物里的 ``1b62725d``：它的 3 个顶点写的是
           **局部 0**，而它的槽是 216 ⇒ 以前没有任何一步把基址加回去，游戏按槽 0 查到了
           别的族的骨。

        **【t95 改门】** 旧版只在「计划里真的有借用」时才动字节（``shifted`` 门）⇒ 没有借用表
        的产物（今天的所有产物）**一个字节都不改**，于是 09c6e58a 这类「块基址 = 51、Blend 里
        却写局部 0..164」的族永远不被加回基址，而合并骨架又被宿主族绑上去了 ⇒ 它那 18636 个
        顶点按槽 0..164 查到别人的骨（实测 93.93% 行）= 模型爆炸的主因。现在改成：

        * 索引修正**与借用布局解耦**，只要有 ``family_ranges`` 就跑（base=0 的族是恒等变换）；
        * 有借用时才追加 ``borrow_blend_remap_problems`` 的 Fatal（无借用的全局布局里，
          跨族引用本身就是合法的全局槽，不能判红）。

        返回账目：``{"rewritten", "channels", "local_rebased", "borrowed", "passthrough",
        "unowned", "unmapped", "skipped", "families"}``。
        """
        ledger = {
            "rewritten": 0,
            "channels": 0,
            "local_rebased": 0,
            "borrowed": 0,
            "passthrough": 0,
            "unowned": 0,
            "unmapped": 0,
            "skipped": True,
            "families": {},
        }
        plan = self.himi_merged_runtime_plan()
        ranges = dict(getattr(plan, "family_ranges", None) or {})
        if not ranges or not self._himi_merged_attach_active():
            return ledger
        shifted = any(list(dict(info).get("borrow") or ()) for info in ranges.values())
        slot_maps = himi_runtime.family_slot_map(ranges)
        own_base_of = {}
        own_slots_of = {}
        for name, info in ranges.items():
            data = dict(info or {})
            if not bool(data.get("own_block", False)):
                continue
            base = int(data.get("base", 0) or 0)
            own_base_of[str(name)] = int(data.get("own_base", base) or 0)
            own_slots_of[str(name)] = int(data.get("slots", 0) or 0)
        if not slot_maps:
            return ledger
        # 【t95】「有人写」的槽集合：本族块 + 块尾借用槽（block_slots 已含后者）。落在外面的
        # 全局引用在运行期读到的是**零矩阵** ⇒ 那些通道把顶点拉向原点。这不是重绑能救的：
        # 它的写方是本次导出里没有数据的家族（实测 077983c9 有 383 个通道引用 1b62725d 的
        # 槽 216，而 1b62725d 没有 palette/attach 段）—— 只能说出来，不能假装修好。
        covered_slots = set()
        for name, info in ranges.items():
            data = dict(info or {})
            base = int(data.get("base", 0) or 0)
            block = int(data.get("block_slots", data.get("slots", 0)) or 0)
            if block > 0:
                covered_slots.update(range(base, base + block))
        try:
            import numpy
            import struct
        except Exception:  # noqa: BLE001 - 没有 numpy 就不动字节（绝不写坏）
            return ledger
        ledger["skipped"] = False
        # 【t93】重定后仍被引用的索引，逐族留底 —— 块尾借用槽生效后这些值必须全部落在
        # 本族块 [base, base+block_slots) 里，落外面就是「顶点查到别人的槽」。
        seen_indices: dict = {}
        for drawib_model in self.drawib_model_list or ():
            draw_ib = str(getattr(drawib_model, "draw_ib", "") or "")
            rebind = self._himi_rebind_for(drawib_model)
            # 【t95】族按 ranges 的键认（= DrawIB），不再要求 rebind：命名空间修正是**前提**，
            # 不是重绑的结果 —— 旧写法里 rebind 为 None（判定 local）就 continue，于是
            # 「判定 local ⇒ 不加基址 ⇒ 还是 local」死锁，永远出不了这个环。
            family = draw_ib if draw_ib in slot_maps else str(
                (rebind or {}).get("family", "") or ""
            )
            mapping = slot_maps.get(family)
            if not mapping:
                continue
            blend = (getattr(drawib_model, "category_buffer_dict", None) or {}).get("Blend")
            if blend is None:
                continue
            verdict = str(
                (
                    self._himi_blend_namespace_verdict(
                        drawib_model, rebind if isinstance(rebind, dict) else {}
                    )
                    or {}
                ).get("verdict", "")
                or ""
            )
            own_base = int(own_base_of.get(family, 0) or 0)
            own_slots = int(own_slots_of.get(family, 0) or 0)
            host_info = self._himi_host_family(plan)
            host_family = str(host_info.get("family") or "") if isinstance(host_info, dict) and host_info.get("ok") else ""
            is_host = bool(host_family and str(family) == host_family)

            arr = numpy.asarray(blend)
            buf = bytearray(arr.tobytes())
            rows = len(buf) // 32
            touched = 0
            channels = 0
            for row in range(rows):
                weights = struct.unpack_from("<4f", buf, row * 32)
                indices = list(struct.unpack_from("<4i", buf, row * 32 + 16))
                changed = False
                for channel, (weight, slot) in enumerate(zip(weights, indices)):
                    if weight <= 0.0:
                        continue
                    channels += 1
                    global_slot = int(slot)
                    in_own = own_base <= global_slot < own_base + own_slots
                    if not in_own and not is_host and verdict != "global" and 0 <= global_slot < max(1, own_slots):
                        # 局部编号（非宿主独立网格未重命名时的 0..N）：加回基址。
                        # 宿主族（is_host）承载了全部件的合并网格，内部引用的 0..50 是其它族（如头发）的全局槽位，绝不加基址。
                        global_slot = own_base + global_slot
                        ledger["local_rebased"] += 1
                        in_own = own_base <= global_slot < own_base + own_slots
                    if global_slot not in covered_slots:
                        ledger["unowned"] += 1
                    new_slot = mapping.get(global_slot)
                    if new_slot is None and is_host and global_slot in covered_slots:
                        # 宿主族：全合并网格直接访问合并骨架的所有合法全局槽，不需要也不应该借用
                        new_slot = global_slot
                    if new_slot is None:
                        # 【t98】映射表没有这个槽：旧行为把它当「全局布局下合法的跨族引用」
                        # 原样保留（passthrough）—— 但那正是错位来源。现在借用集由导出 Blend
                        # 发现，跨族引用一律有块尾目标槽；走到这里说明发现集漏了它，
                        # 布局已带借用槽时（shifted）由下面的硬门 Fatal，不会再静默放行。
                        if global_slot not in covered_slots:
                            ledger["unowned"] += 1
                        ledger["unmapped"] += 1
                        ledger["passthrough"] += 1
                        continue
                    if not in_own and new_slot >= own_base + own_slots:
                        ledger["borrowed"] += 1
                    seen_indices.setdefault(family, set()).add(int(new_slot))
                    if new_slot != int(slot):
                        indices[channel] = int(new_slot)
                        changed = True
                if changed:
                    struct.pack_into("<4i", buf, row * 32 + 16, *indices)
                    touched += 1
            if touched:
                try:
                    arr.view(numpy.uint8)[:] = numpy.frombuffer(bytes(buf), dtype=numpy.uint8)
                except Exception:  # noqa: BLE001 - 写不回去就保持原样
                    continue
                # 字节变了 ⇒ 之前缓存的命名空间判定作废：重绑门必须看**修正后**的产物，
                # 否则「先判 local ⇒ 后修好」这条路径会让 vs-t0 永远绑不上去。
                self._himi_blend_verdicts.pop(draw_ib, None)
                ledger["rewritten"] += 1
                ledger["channels"] += channels
                ledger["families"][family] = ledger["families"].get(family, 0) + touched
                self._himi_notify(
                    f"{draw_ib}: Blend 索引已重定到族 {family} 的新块"
                    f"（{touched} 行 / {channels} 个权重大于 0 的通道，命名空间判定 {verdict or '未知'}）"
                )
        # 【t93 硬门】块尾借用槽已经改了各族的块基址 ⇒ 旧编号在这个布局里不再是「本族的槽」。
        # 此时还有未映射通道、或任何索引落在本族块外，都是一个必然错位的产物（几何跟着别人的
        # 骨骼跑）。**Fatal 而不是 warning**：默认警告只写日志，实测这种错位会被当成"导出成功"。
        if shifted:
            remap_problems = himi_runtime.borrow_blend_remap_problems(
                ledger, seen_indices, ranges, host_family=host_family
            )
            if remap_problems:
                from ...utils.format_utils import Fatal

                raise Fatal(
                    "合并骨架的 Blend 重定后仍有错位通道（块尾借用槽已生效，旧编号不再合法）："
                    + "；".join(remap_problems)
                    + "；复核用 tools/himi_t91_check.py 对产物跑一遍"
                )
        if ledger["unowned"]:
            orphan_families = sorted(
                {
                    str(dict(info).get("family_index", ""))
                    for info in ranges.values()
                    if not bool(dict(info).get("own_block", False))
                }
                - {""}
            )
            self._himi_notify(
                f"警告：{ledger['unowned']} 个权重大于 0 的通道引用了没有任何写方的槽"
                f"（骨架 {int(getattr(plan, 'slot_count', 0) or 0)} 槽里"
                f"只有 {len(covered_slots)} 个槽有块）⇒ 运行期读到零矩阵、那些顶点会被拉向原点。"
                "这些槽属于本次导出里没有数据的家族"
                + (f"（借位族 {', '.join(orphan_families[:4])}）" if orphan_families else "")
                + "：把缺失的部件一起导出、或重新导入让数据齐全",
                warning=True,
            )
        if ledger["local_rebased"]:
            self._himi_notify(
                "合并骨架：把局部骨骼编号加回族基址 "
                f"{ledger['local_rebased']} 个通道（涉及 {len(ledger['families'])} 个族）"
                f"；跨族全局引用 {ledger['passthrough']} 个通道原样保留"
            )
        return ledger

    def _himi_scan_bind_positions(self) -> dict:
        """【t74 + t76】H-A 硬门：产物里有没有"位置被写成别行的位置"的行（导出段拉丝的直接成因）。

        判据照搬 t72（`common/himi_merged_runtime.scan_bind_positions`）：逐元素对应 u→v、
        稳健相似拟合、离群行 + 「精确等于另一在用行」⇒ 判红，并指名**部件 + 行号 + 残差**。

        **【t76 更正覆盖范围】** t74 初版在这一步用了 `if not self._himi_merged_attach_active(): return`，
        于是**只在合并骨骼路径**上跑 —— 而 H-A 的载体是 `<fam>-Position.buf` 族池 + per-part IB
        （**两条路径都有**）⇒ 普通导出同样可能带这种错行却没人查。现在**两条路径都跑**：

        * 合并路径：判红 ⇒ `_himi_degrade_to_ordinary(...)`（降级 + 大声警告，**不抛 Fatal**）；
        * 普通路径 / 已降级：判红 ⇒ **大声警告**（控制台 + 产物 ini 注释块）并把账目留下 ——
          这里没有"可降级的目标"（它本来就是兜底），但必须**说出来**而不是静默发出去。
        """
        result = {"ok": True, "parts": [], "problems": [], "skipped": True}
        try:
            parts = []
            for drawib_model in self.drawib_model_list or ():
                family = str(getattr(drawib_model, "draw_ib", "") or "")
                for submesh_model in getattr(drawib_model, "submesh_model_list", None) or ():
                    bare = str(getattr(submesh_model, "blender_obj_name", "")
                               or getattr(submesh_model, "obj_name", "") or "")
                    if bare.startswith("LOD0."):
                        bare = bare[len("LOD0."):]
                    if bare and family:
                        parts.append((bare, family))
            if not parts:
                return result
            result = himi_runtime.scan_bind_positions(
                parts,
                mod_root=GlobalConfig.path_generate_mod_folder(),
                workspace_root=GlobalConfig.path_workspace_folder(),
            )
        except Exception as exc:  # noqa: BLE001 - 检查自身失败不得反过来弄坏导出
            result = {"ok": True, "parts": [], "problems": [], "skipped": True,
                      "error": str(exc)}
        self._himi_bind_position_result = result
        if not result.get("ok"):
            detail = (
                "H-A 自检：产物里有行的位置被写成了**别行的位置**（导出段拉丝的直接成因）："
                + "；".join(list(result.get("problems") or [])[:4])
                + ("；…" if len(result.get("problems") or []) > 4 else "")
            )
            if self._himi_merged_attach_active():
                # 合并路径：按兜底原则降级为普通导出（保留兜底通路 + 大声警告）。
                self._himi_degrade_to_ordinary(detail)
            else:
                # 【t76】普通路径（或已降级）：没有"可降级的目标" ⇒ 只大声警告 + 留账目，
                # 绝不静默发出，也绝不中止（普通导出是兜底）。
                print(f"{_LOG_PREFIX} 警告 {detail}")
                self._himi_notify(detail, warning=True)
                notices = list(getattr(self, "_himi_bind_position_warnings", None) or [])
                notices.append(detail)
                self._himi_bind_position_warnings = notices
        return result

    def himi_bind_position_result(self) -> dict:
        """【t74】H-A 自检账目（供测试/上层读）。"""
        return dict(getattr(self, "_himi_bind_position_result", {}) or {})

    def generate_buffer_files(self, output_folder: str):
        """【t39 顶点侧接点 / t78 方案 B】先做逐行族归属**诊断**（不写字节），再走基类落盘。

        为什么放在这里：`Meshes/<draw_ib>-Position.buf` 就是在本方法里落盘的
        （``ui/universal/drawib_export_base.py::generate_buffer_files`` 的
        ``category_buffer_dict`` 循环），**改在这里就不会动基类**；
        且它发生在任何 ini 写盘之前，与 palette 侧（attach CS 的 128 B 载荷）同源同报。
        """
        self._himi_calibrate_bind_positions()
        # 【t49 / t47 方案 A+C】旁写 <fam>-BlendOrigin.buf + 越界引用计数（只新增文件/日志）
        self._himi_write_blend_origin()
        # 【t73·兜底原则】普通/降级导出 ⇒ 把全局编号搬回**局部编号**（本族减基址；跨族记数 + 警告）
        self._himi_remap_ordinary_blend_to_local()
        # 【t98】此刻 Blend 才是最终形态（本 draw 全部顶点 + 全局槽号）：按它重算一次借用布局，
        # 否则跨族（池化行）引用永远不会被排进借用槽 —— 那正是「模型爆炸/错位」的现场。
        self._himi_refresh_borrow_layout()
        # 【t84】合并路径：把 Blend 索引重定到各族的**新块**（含块尾借用槽）
        ledger = self._himi_remap_merged_blend_to_blocks()
        if ledger.get("rewritten"):
            print(
                f"{_LOG_PREFIX} 合并路径已重定 {ledger['rewritten']} 个 DrawIB 的 Blend 索引"
                f"（{ledger['channels']} 个通道；局部重新加基址 {ledger['local_rebased']} 个，"
                f"借用通道 {ledger['borrowed']} 个，未映射 {ledger['unmapped']} 个）"
            )
        self._himi_blend_block_remap_ledger = ledger
        written = super().generate_buffer_files(output_folder)
        # 【t74】H-A 自检：产物写完之后查"位置被写成别行的位置"（判红 ⇒ 降级，不 Fatal）
        self._himi_scan_bind_positions()
        return written

    # ------------------------------------------------------------------ #
    # 【t18 / T5-F5】诊断必须落在「用户一定会看到的地方」：产物 ini 的注释块
    # ------------------------------------------------------------------ #

    def himi_deform_structure_result(self) -> dict:
        """【t18/T5-F2】K2 门的账目：``judged/checked/unavailable/unjudgeable/missing_identity``。

        「有没有真的检查过」由它回答（修前该门因计划缺 `unique_str` 而完全空转，
        而日志里「没有那一行」被误当成了检查过的证据）。
        """
        return dict(getattr(self, "_himi_deform_structure_result", {}) or {})

    def himi_diagnostic_lines(self) -> list:
        """本次导出的诊断行（**正例也留痕**）：F6 基址表 / K2 变形 pass 结构 / K4 触发键。

        为什么要有「正例」行：t5 的复核正是靠「日志里没有那一行」推断「检查过了」——
        而当时那一行**根本不会出现**（门在空转）。⇒ 现在无论有没有问题都留下账目，
        「没有报错」不再能被当作「检查过了」的证据。
        """
        lines: list = []
        for item in getattr(self, "_himi_base_table_notices", ()) or ():
            lines.append("警告（F6 基址表）：" + str(item))
        for item in getattr(self, "_himi_key_notices", ()) or ():
            lines.append("警告（K4 触发键）：" + str(item))
        # 【t49 / 方案 C】越界引用计数也进产物注释块（用户打开 mod 目录就能看到）
        blend_origin = dict(getattr(self, "_himi_blend_origin_result", {}) or {})
        if blend_origin:
            lines.append(
                "out_of_range_refs=%s (%.2f%%)（顶点 %s；逐族：%s）"
                % (blend_origin.get("out_of_range_refs", 0), float(blend_origin.get("pct", 0.0)),
                   blend_origin.get("vertices", 0),
                   ", ".join("%s:%s (%.2f%%)" % (item["family"], item["out_of_range"], item["pct"])
                             for item in (blend_origin.get("families") or ())))
            )
        for item in getattr(self, "_himi_blend_origin_notices", ()) or ():
            lines.append("警告（越界引用）：" + str(item))
        # 【t73·兜底原则】降级事实必须写进产物注释块（第三层诊断）：用户打开 mod 目录就能看到
        # 「本次其实是普通导出、以及为什么」。
        bind = dict(getattr(self, "_himi_bind_position_result", {}) or {})
        if bind and not bind.get("skipped"):
            ledger = dict(bind.get("ledger") or {})
            lines.append(
                "H-A 自检：部件 %d 个；越界/写错 = %s；内点 RMS = %s"
                % (len(bind.get("parts") or ()), "有" if not bind.get("ok") else "无",
                   ", ".join("%s:%s" % (part.get("bare"), (part.get("ledger") or {}).get("rms"))
                             for part in (bind.get("parts") or ())[:5] if not part.get("skipped")))
            )
            for item in (bind.get("problems") or ())[:4]:
                lines.append("警告（H-A 位置写错）：" + str(item))
        if self.himi_degraded_to_ordinary():
            lines.append("本次导出模式：普通导出（合并骨架侧失败后**已降级**，产物不含合并骨架）")
            for item in getattr(self, "_himi_degrade_notices", ()) or ():
                lines.append("警告（已降级）：" + str(item))
            for item in getattr(self, "_himi_ordinary_local_remap_notices", ()) or ():
                lines.append("提示（普通模式编号）：" + str(item))
        result = dict(getattr(self, "_himi_deform_structure_result", {}) or {})
        if result:
            # 【t20 / T19-F1】图标必须跟随**账目判词**：`judged == 0`（无 rebind 记录）时
            # 结果是「未核对（无 rebind 记录）」而不是「通过」—— 空转不得在产物里报成功。
            verdict = str(result.get("verdict") or "")
            if result.get("problems"):
                icon = "警告"
            elif verdict == "通过":
                icon = "通过"
            else:
                icon = "未核对"
            lines.append(f"{icon}（K2 变形 pass 结构）：" + str(result.get("summary") or ""))
            if result.get("problems"):
                for item in result.get("problems") or ():
                    lines.append("  - " + str(item))
        if not lines:
            lines.append("本次导出没有产生任何 HIMI 合并骨架诊断（未走合并路径，或各项均通过）")
        return lines

    def _himi_emit_diagnostic_comments(self, ini_builder: M_IniBuilder) -> None:
        """把诊断写进**产物 ini 的注释块**（一次；`;MARK:CreditInfo` 附近）。

        三个可见层（缺一不可，因为前两层都要用户主动去看）：
        1. Blender 控制台（`_himi_notify` 的 `print`）；
        2. 导出日志（`utils.log_utils.LOG.warning`，收尾会保存到 Text Editor）；
        3. **产物 ini 注释块**（本方法）—— 用户打开 mod 目录就能看到，且随产物长期存在。

        为什么不用 `report({'WARNING'})`：那条通道在导出操作符里
        （`ui/ui_func_export.py::ExportMod`），不在本模块的改动范围内（见报告 §未解决项）；
        注释块与 operator report 是「或」的关系（任务口径允许）。
        """
        if self._himi_diagnostics_emitted:
            return
        # 【t85·唯一谓词】普通导出（开关关闭、未降级）**一个字节都不发**：
        # 这是「关闭该特性 ⇒ 产物不含合并骨架内容」的闸门（层 3 是合并骨架的专属诊断层）。
        if not (self._himi_merged_attach_active() or self.himi_degraded_to_ordinary()):
            self._himi_diagnostics_emitted = True
            print(
                f"{_LOG_PREFIX} 普通导出（未启用合并骨架）：产物 ini 不写合并骨架诊断块"
                "（控制台/导出日志仍有本次导出的提示）"
            )
            return
        self._himi_diagnostics_emitted = True
        section = M_IniSection(M_SectionType.CreditInfo)
        section.append("; ===== HIMI 合并骨架诊断（导出侧自动生成；请勿手改，重新导出会覆盖）=====")
        for line in self.himi_diagnostic_lines():
            section.append("; " + line)
        section.append(
            "; 这些行只作诊断、不改变产物行为；出现 `警告` 时请连同 Blender 控制台/导出日志一起提供。"
        )
        section.new_line()
        ini_builder.append_section(section)

    def add_unity_vs_resource_vb_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """基类 VB 资源段 + 角色级合并骨架资源（本文件与 ini 由同一份计划渲染）。"""
        # 【t18 / T5-F5】诊断注释块：只发一次。**t85 起只在合并路径出内容时发**
        # （旧行为「与是否走合并路径无关」⇒ 普通导出的 ini 也带一整块合并骨架诊断，
        # 用户实报「不使用合并骨骼时配置表里面会有合并骨骼的生成」）。
        if self._himi_merged_attach_active() or self.himi_degraded_to_ordinary():
            self._himi_emit_diagnostic_comments(ini_builder)
        super().add_unity_vs_resource_vb_sections(
            ini_builder=ini_builder, drawib_model=drawib_model
        )
        if self._himi_resource_emitted:
            return
        plan = self.himi_merged_runtime_plan()
        if not plan.has_rebinds:
            return
        from ...blueprint.export_helper import BlueprintExportHelper

        folder_name = (
            str(BlueprintExportHelper.get_current_buffer_folder_name() or "")
            or himi_runtime.MERGED_SKELETON_DEFAULT_FOLDER
        )
        self._himi_publish_skeleton(folder_name)
        self._himi_publish_attach_ranges(plan, folder_name)
        lines = plan.resource_ini_lines(folder_name)
        if not lines:
            return
        self._himi_resource_emitted = True
        resource_section = M_IniSection(M_SectionType.ResourceBuffer)
        for line in lines:
            resource_section.append(line)
        resource_section.new_line()
        ini_builder.append_section(resource_section)
        self._himi_emit_family_palette_and_attach_sections(ini_builder, plan, folder_name)

    def _himi_emit_family_palette_and_attach_sections(
        self, ini_builder: M_IniSection, plan, folder_name: str
    ) -> None:
        """【每帧机制 t54 / t75】逐族发射：① 家族副本声明（``Buffer`` + stride 48 + 无 format）
        ② ``[CustomShaderHI3MergedSkeletonAttach_<fam>]`` 段。

        数据源只有一个 = ``plan.family_ranges``（``own_block=False`` 的借位族不发段）——
        与 VB 段的捕获行 / ``run`` 行**同源**，不会出现「声明了副本但没人写」或反之。

        **t75 起是「一个 shader、N 个段」**：段体仍由 ``render_attach_section_lines`` 给
        （形状：section 名 / flags / cs-t0 / cs-u0 / Dispatch / cs-u0 = null），但 ``cs =``
        一行被改写成**同一个** :data:`MERGED_ATTACH_HLSL_PATH`。基址不再进 shader 文件
        （旧口径的逐族 ``#define``）—— 由该 shader 用源槽位数在运行期查表算出来。
        """
        items = self.himi_family_range_items()
        if not items:
            return
        skeleton_name = ""
        decl = plan.resource(str(folder_name or himi_runtime.MERGED_SKELETON_DEFAULT_FOLDER))
        if decl is not None:
            skeleton_name = str(decl.name)
        palette_section = M_IniSection(M_SectionType.ResourceBuffer)
        for family, info in items:
            family_decl = himi_runtime.build_family_palette_decl(family, int(info["slots"]))
            for line in family_decl.ini_lines():
                palette_section.append(line)
        # 【t74】每族 **vs-cb3 的副本**（attach CS 的 `cs-cb1` 源）：资源段只写段名 —— 尺寸与
        # 布局由 `copy vs-cb3` 的源视图决定（本仓 ZZMI 的 `copy vs-cb1` 先例同款，
        # `tests/test_zzmi_merged_skeleton_ini.py` 钉死「不许写 type/stride」）。
        for family, _info in items:
            for line in himi_runtime.cb3_capture_ini_lines(family):
                palette_section.append(line)
        # 【t74】跨族物体矩阵表（`cs-u1`）：3 个 float4 × 族数，运行期由各族自己那段 CS 写入，
        # 宿主族那笔再读回来算 X = M_base⁻¹·M_own。
        object_matrix_name = ""
        try:
            object_decl = himi_runtime.build_object_matrix_decl(str(plan.role_key), len(items))
        except Exception as exc:  # noqa: BLE001 - 声明不合法 ⇒ 不发 u1（CS 退回载荷兜底 X）
            self._himi_notify(
                f"物体矩阵表（cs-u1）声明不合法（{exc}）⇒ 本次不发布该资源，"
                "X_f 退回导出期烘死的载荷兜底。",
                warning=True,
            )
            object_decl = None
        if object_decl is not None:
            object_matrix_name = str(object_decl.name)
            for line in object_decl.ini_lines():
                palette_section.append(line)
        # 【F5】每族的**身份资源**声明（16 字节 = base/span/version/magic）：
        # 它是 base/span 的权威来源，让「本族 span 上界」不再与查表键同源。
        buffer_folder = str(folder_name or himi_runtime.ATTACH_RANGE_DEFAULT_FOLDER)
        for family, info in items:
            for line in himi_runtime.attach_range_ini_lines(
                family,
                f"{buffer_folder}/{himi_runtime.attach_range_basename(family)}",
            ):
                palette_section.append(line)
        # 【t91】每族的**借用表**声明（`cs-t4`，每行 16 B = 目标槽/来源族序号/源局部槽/0）。
        # 与身份资源不同：表长随借用骨数变，所以 array 就是行数（空表恒 1 行哨兵）——
        # 声明与实际文件必须同长，否则 CS 侧 GetDimensions 拿到的行数就不是导出期算的那个。
        for family, _info in items:
            rows = himi_runtime.borrow_map_rows(plan.family_ranges, family)
            for line in himi_runtime.borrow_map_ini_lines(
                family,
                f"{buffer_folder}/{himi_runtime.borrow_map_basename(family)}",
                len(rows),
            ):
                palette_section.append(line)
        palette_section.new_line()
        ini_builder.append_section(palette_section)
        if not skeleton_name:
            self._himi_notify(
                "找不到合并骨架资源名 ⇒ 不发 attach 段（副本仍已声明，但不会有写方）",
                warning=True,
            )
            return
        emitted_paths: list = []
        # 来源族副本的**序号顺序**（= 块基址升序）由 plan 决定，ini 的 cs-tN 与 CS 里的
        # palette 声明必须同源 —— 两边都从这一个函数取，序号就不可能对不上。
        palette_families = himi_runtime.borrow_palette_families(plan.family_ranges)
        for family, info in items:
            attach_section = M_IniSection(M_SectionType.MergedSkeleton)
            for line in himi_runtime.render_attach_section_lines(
                family,
                base=int(info["base"]),
                slots=int(info["slots"]),
                skeleton_name=skeleton_name,
                object_matrix_name=object_matrix_name,
                # 【t91】Dispatch 覆盖**整块**（本族 span + 块尾借用槽），不是只覆盖 span：
                # 借用槽由本族 dispatch 自己写，少算一槽就等于少一个块尾槽没人写。
                block_slots=int(info.get("block_slots", info["slots"]) or info["slots"]),
                borrow_map_name=himi_runtime.borrow_map_resource_name(family),
                palette_families=palette_families,
            ):
                # 【单文件改写】只动 `cs =` 那一行，其余逐字保留（含 Dispatch / 解绑行）。
                if line.startswith("cs = "):
                    line = "cs = " + MERGED_ATTACH_HLSL_PATH
                    emitted_paths.append(MERGED_ATTACH_HLSL_PATH)
                attach_section.append(line)
            attach_section.new_line()
            ini_builder.append_section(attach_section)
        problems = merged_attach_reference_problems(emitted_paths)
        if problems:
            # 【t73·兜底原则】合并专属自检失败 ⇒ 降级为普通导出（不许一票否决）。
            self._himi_degrade_to_ordinary(
                "合并骨架 attach 段的 `cs =` 引用自检失败：" + "；".join(problems)
            )
        self._himi_attach_cs_paths = emitted_paths
        # 【t94】渲染段蒙皮段（声明 + CS 段）：紧跟在 attach 之后发，名字同源。
        self._himi_emit_family_skin_sections(ini_builder, plan, folder_name)

    # ------------------------------------------------------------------ #
    # t94：渲染段自蒙皮 pass（每族一段 + 一张自建输出缓冲）
    # ------------------------------------------------------------------ #

    def _himi_skin_route_active(self) -> bool:
        """蒙皮段是否发射：与 attach 同门（合并骨架路径激活 + 本次计划可用）。

        ``MERGED_SKIN_ROUTE = False`` 可整条关掉（回到「只有 attach」的旧产物，测试/兜底用）。
        """
        if not bool(MERGED_SKIN_ROUTE):
            return False
        try:
            return bool(self._himi_merged_attach_active())
        except Exception:  # noqa: BLE001 - 判不出就按"不发"（安全侧：不写半截产物）
            return False

    def _himi_skin_rows(self, family: str) -> int:
        """该族要被蒙皮的行数 = 它自己上传的 Position 行数（= deform 重放的行数）。

        首选 ``draw_number``（本次导出权威行数，与 ``draw = <行数>, 0`` 同源）；取不到时
        退回产物 ``Meshes/<fam>-Position.buf`` 的字节数 / 44（44 B/行 = P12 N12 C4 TA16）。
        """
        name = str(family or "")
        if not name:
            return 0
        for drawib_model in self.drawib_model_list or ():
            if str(getattr(drawib_model, "draw_ib", "") or "") != name:
                continue
            rows = int(getattr(drawib_model, "draw_number", 0) or 0)
            if rows > 0:
                return rows
        try:
            buffer_folder = GlobalConfig.path_generatemod_buffer_folder()
        except Exception:  # noqa: BLE001
            return 0
        path = os.path.join(
            str(buffer_folder or ""), name + "-Position.buf"
        )
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0
        stride = int(himi_runtime.SKIN_OUT_STRIDE)
        if size <= 0 or size % stride:
            return 0
        return size // stride

    def _himi_emit_family_skin_sections(
        self, ini_builder, plan, folder_name: str
    ) -> dict:
        """逐族发射：① ``[ResourceHI3SkinOut_<fam>]`` 声明 ② ``[CustomShaderHI3MergedSkin_<fam>]`` 段。

        为什么在这里发（资源段这一遍）而不是更早：``array`` 是该族**行数**，它只有在拿到
        ``draw_number``（deform 重放行数）之后才有值。段体（绑定/顺序）由
        ``himi_runtime.skin_section_lines`` 给，本模块不另写一份文本。

        返回 ``{family: {"rows", "run", "bind"}}``：IB 段插 ``run = …Skin_<fam>`` 与
        ``vb0 = ResourceHI3SkinOut_<fam>`` 时只查这张表（名字与声明同源）。
        """
        if not self._himi_skin_route_active():
            return {}
        items = self.himi_family_range_items()
        if not items:
            return {}
        decl = plan.resource(str(folder_name or himi_runtime.MERGED_SKELETON_DEFAULT_FOLDER))
        skeleton_name = str(getattr(decl, "name", "") or "")
        if not skeleton_name:
            self._himi_notify(
                "找不到合并骨架资源名 ⇒ 不发蒙皮段（cs-t2 无目标）", warning=True
            )
            return {}
        decls: list = []
        emitted: list = []
        for family, _info in items:
            rows = self._himi_skin_rows(family)
            if rows <= 0:
                self._himi_notify(
                    f"{family}: 取不到该族的 Position 行数 ⇒ 不发蒙皮段"
                    "（该族几何仍由 deform 流输出重放产出，借骨部分会慢一帧）",
                    warning=True,
                )
                continue
            try:
                skin_decl = himi_runtime.build_skin_out_decl(family, rows)
            except Exception as exc:  # noqa: BLE001 - 声明不合法就不发这一段
                self._himi_notify(
                    f"{family}: 蒙皮输出缓冲声明不合法（{exc}）⇒ 不发该族蒙皮段",
                    warning=True,
                )
                continue
            decls.extend(skin_decl.ini_lines())
            emitted.append((family, rows))
        if not emitted:
            return {}
        resource_section = M_IniSection(M_SectionType.ResourceBuffer)
        for line in decls:
            resource_section.append(line)
        resource_section.new_line()
        ini_builder.append_section(resource_section)
        sections: dict = {}
        for family, rows in emitted:
            skin_section = M_IniSection(M_SectionType.MergedSkeleton)
            for line in himi_runtime.skin_section_lines(
                family,
                rows=rows,
                skeleton_name=skeleton_name,
                hlsl_path=MERGED_SKIN_HLSL_PATH,
            ):
                skin_section.append(line)
            skin_section.new_line()
            ini_builder.append_section(skin_section)
            sections[str(family)] = {
                "rows": int(rows),
                "run": "run = " + himi_runtime.skin_section_name(family),
                "bind": himi_runtime.SKIN_OUT_BIND_SLOT
                + " = "
                + himi_runtime.skin_out_resource_name(family),
            }
        self._himi_skin_sections = dict(sections)
        return dict(sections)

    def himi_skin_sections(self) -> dict:
        """本次发射的蒙皮段账目（``{family: {"rows","run","bind"}}``；供测试/上层读）。"""
        return dict(getattr(self, "_himi_skin_sections", None) or {})

    def _himi_write_skin_cs(self) -> dict:
        """把 ``Toolset/himi_merged_skin.hlsl`` 拷进 ``<mod>/res/``（幂等、字节比对）。"""
        if not self._himi_skin_route_active():
            return {"skipped": True, "reason": "skin_route_inactive"}
        addon_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        source = os.path.join(addon_root, "Toolset", MERGED_SKIN_HLSL_BASENAME)
        if not os.path.isfile(source):
            result = {"ok": False, "path": "", "message": f"未找到蒙皮 CS：{source}"}
            self._himi_skin_cs_result = result
            from ...utils.format_utils import Fatal

            raise Fatal(
                f"缺渲染段蒙皮 CS（{source}）⇒ 蒙皮段会引用不存在的 shader "
                "（3DMigoto 对编译失败的 shader 静默跳过 ⇒ 整具模型塌到原点）"
            )
        with open(source, "rb") as handle:
            payload = handle.read()
        target_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "res")
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, MERGED_SKIN_HLSL_BASENAME)
        written = True
        try:
            with open(path, "rb") as handle:
                written = handle.read() != payload
        except OSError:
            written = True
        if written:
            with open(path, "wb") as handle:
                handle.write(payload)
        result = {
            "ok": True,
            "path": path,
            "written": written,
            "bytes": len(payload),
            "message": ("已写入" if written else "内容一致、未重写")
            + f" res/{MERGED_SKIN_HLSL_BASENAME}",
        }
        self._himi_skin_cs_result = result
        if written:
            print(f"{_LOG_PREFIX} {result['message']} → {path}")
        return result

    def himi_skin_cs_result(self) -> dict:
        """`res/himi_merged_skin.hlsl` 的落盘结果字典。"""
        return dict(getattr(self, "_himi_skin_cs_result", None) or {})

    def add_unity_vs_texture_override_ib_sections(self, ini_builder: M_IniBuilder, drawib_model):
        """基类 IB 段 + 每一笔 ``drawindexed`` **之前**插 ``cb3 捕获`` + ``run = <该族 attach>``。

        为什么每笔前都要跑（ZZMI 纪律，`ui/universal/zzmi.py:129-135`）：骨架由各族的 CS
        逐帧填充，而渲染 draw 可能发生在**本族 deform pass 之外的时刻**（多实例/多 IB 共享
        同一 SO 目标）；CS 幂等 ⇒ 每笔前插一次是安全且必要的（少了它 ⇒ 骨架为空 ⇒ 模型
        整体消失）。

        两行**同源**于 ``himi_runtime.render_attach_render_capture_lines``（不在这里拼文本）：

        * ``ResourceHI3Cb3_<fam> = copy vs-cb3 unless_null`` —— 渲染 draw（indexed）的 vs-cb3
          才是 **160 B / rows 0..7 = 物体矩阵 + 其逆**；deform（non-indexed）draw 的 368 B
          版本 rows 0..7 不是物体矩阵 ⇒ 采集点必须在这里，运行期 X 才会真正生效。
        * ``run = <该族 attach>`` —— 必须紧跟捕获行（否则 CS 读到的是别族/上一帧的副本）。

        只在该 DrawIB 通过 G1（``_himi_rebind_for`` 非 ``None``）且该族**有专属块**
        时插入；否则逐字走基类产物。
        """
        before = list(getattr(ini_builder, "ini_section_list", []) or ())
        super().add_unity_vs_texture_override_ib_sections(
            ini_builder=ini_builder, drawib_model=drawib_model
        )
        rebind = self._himi_rebind_for(drawib_model)
        # 【t95】渲染段的「cb3 捕获 → run attach」职责是让本族那块槽当帧被写，
        # 与「本族顶点要不要重绑 vs-t0」无关 ⇒ 不再以 rebind is None 短路。
        family = str((rebind or {}).get("family") or getattr(drawib_model, "draw_ib", "") or "")
        if self.himi_family_range(family) is None:
            return
        after = list(getattr(ini_builder, "ini_section_list", []) or ())
        new_sections = after[len(before):]
        attach = himi_runtime.attach_section_name(family)
        capture_lines = himi_runtime.render_attach_render_capture_lines(family)
        touched = 0
        for section in new_sections:
            if getattr(section, "SectionType", None) != M_SectionType.TextureOverrideIB:
                continue
            lines = list(getattr(section, "SectionLineList", []) or ())
            out: list = []
            for line in lines:
                if str(line).strip().startswith("drawindexed"):
                    out.extend(capture_lines)
                    touched += 1
                out.append(line)
            if touched:
                section.SectionLineList[:] = out
        if not touched:
            print(
                f"{_LOG_PREFIX} 提示 {drawib_model.draw_ib}: IB 段里没有 drawindexed 行 ⇒ "
                f"未插 `run = {attach}`"
            )

    # ------------------------------------------------------------------ #
    # t75：合并 attach CS 落盘 + `cs =` 引用硬门
    # ------------------------------------------------------------------ #

    def himi_attach_cs_paths(self) -> list:
        """本次导出发射的全部 `cs =` 值（单文件口径下应全部相同）。"""
        return list(self._himi_attach_cs_paths or ())

    def himi_attach_cs_result(self) -> dict:
        """`res/himi_attach.hlsl` 的落盘结果字典。"""
        return dict(self._himi_attach_cs_result or {})

    def _himi_borrow_pairs(self, plan, ranges) -> dict:
        """求借用槽的骨对：{(本族序号, 来源族 rank): (本族局部槽, 来源族局部槽)}。

        借用槽运行期要的 X_源 = P_本族[p] · P_来源[q]⁻¹，其中 p/q 是**同一根物理骨**在
        两族 palette 里的局部槽 —— 这是导出期唯一能定下来的索引信息，所以在这里算一次
        （照 common/himi_family_x.py::solve_palette_pair）；模板里只带索引表，矩阵每帧
        从活 palette 现算。解不出（缓存缺失 / 两族没有共享骨）的骨对留哨兵：那些槽运行期
        退回单位阵（少一层校正），**不抛异常、不让导出失败**。
        """
        from ...common import himi_family_x

        pairs: dict = {}
        try:
            rows_by_family = dict(himi_runtime.borrow_map_rows_by_family(ranges) or {})
        except Exception as exc:  # noqa: BLE001 - 归位表算不出来 ⇒ 当没有可解的骨对
            self._himi_notify(f"借用骨对：借用行归位表求解失败（{exc}）", warning=True)
            return pairs
        if not rows_by_family:
            return pairs
        order = list(himi_runtime.borrow_palette_families(ranges) or ())
        if not order:
            return pairs
        participants = himi_family_x.participants_from_plan(plan) or {}
        workspace_root = GlobalConfig.path_workspace_folder()
        palettes: dict = {}

        def palette_of(family: str) -> list:
            if family not in palettes:
                bare = str(participants.get(family) or "").strip()
                slots: list = []
                if bare:
                    loaded = himi_family_x.read_family_palette(workspace_root, bare)
                    if loaded.get("ok"):
                        slots = list(loaded.get("slots") or [])
                palettes[family] = slots
            return list(palettes[family])

        unresolved: list = []
        for own_index, family in enumerate(order):
            ranks = {int(row[1]) for row in (rows_by_family.get(family) or ())}
            if not ranks:
                continue
            borrower = palette_of(family)
            if not borrower:
                self._himi_notify(
                    f"借用骨对：{family} 的 palette 缓存不可读 ⇒ 该族的借用槽退回单位阵",
                    warning=True,
                )
                continue
            for rank in sorted(ranks):
                if rank < 0 or rank >= len(order):
                    unresolved.append(f"{family}←序号{rank}（来源族序号越界）")
                    continue
                source_family = order[rank]
                source = palette_of(source_family)
                if not source:
                    unresolved.append(f"{family}←{source_family}（来源族 palette 不可读）")
                    continue
                solved = himi_family_x.solve_palette_pair(borrower, source)
                if not solved.get("ok"):
                    unresolved.append(
                        f"{family}←{source_family}（{solved.get('note') or '解不出共享骨'}）"
                    )
                    continue
                pairs[(own_index, rank)] = (int(solved["own"]), int(solved["src"]))
        if unresolved:
            self._himi_notify(
                "借用骨对没解出的（这些槽运行期退回单位阵）：" + "、".join(unresolved),
                warning=True,
            )
        return pairs

    def _himi_write_attach_cs(self) -> dict:
        """把**单个**合并 attach CS 写进 `<mod>/res/himi_attach.hlsl`（幂等）。

        ``decl`` 只签出**合并骨架总槽位数**（越界保护上界）；基址表则**由本次 plan 生成**
        （``merged_attach_base_table_lines``）—— 表与 ini 的 ``array=`` / ``Dispatch`` 因此
        同源，换角色不需要改代码。落盘前把「**实际渲染进 CS 的那张表**」再与 plan 审计一遍
        （表是外部塞进来的 / 生成器漂移时才可能出问题）。
        """
        plan = self.himi_merged_runtime_plan()
        decl = None
        try:
            slots = int(getattr(plan, "slot_count", 0) or 0)
        except Exception:  # noqa: BLE001 - 任何形态异常都退回参考槽位数
            slots = 0
        if slots <= 0:
            decl = himi_runtime.build_merged_skeleton_decl(
                himi_runtime.REFERENCE_ROLE_KEY, himi_runtime.REFERENCE_SLOT_COUNT
            )
        else:
            decl = himi_runtime.build_merged_skeleton_decl(
                himi_runtime.REFERENCE_ROLE_KEY, slots
            )
        ranges = dict(getattr(plan, "family_ranges", None) or {})
        if not ranges:
            # 没有 plan（合并路径没激活 / 退化调用）⇒ 退回参考样例，与旧行为逐字一致。
            ranges = dict(MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES)
        table_lines = merged_attach_base_table_lines(ranges)
        # 借用表的行数与骨对都是**导出期**定的：行数只信本次 ranges（运行期 GetDimensions
        # 在 typed 视图与 uint4 元素不一致时只回不到 1/4 的行），骨对只给索引。
        borrow_rows = max(
            [len(rows) for rows in himi_runtime.borrow_map_rows_by_family(ranges).values()] or [0]
        )
        borrow_pairs = self._himi_borrow_pairs(plan, ranges)
        text = merged_attach_hlsl_text(
            decl,
            base_table_lines=table_lines,
            borrow_rows=max(1, int(borrow_rows)),
            borrow_pairs=borrow_pairs,
        )
        problems = merged_attach_hlsl_problems(text)
        # 【t84】借用槽块：只有本次计划真的有借用时才判（没有借用时该块根本不生效，
        # 但模板里始终渲染 —— 判据只看「模板是否还带着正确写法」）。
        borrow_enabled = any(
            list(dict(info).get("borrow") or ())
            for info in (ranges or {}).values()
        )
        if borrow_enabled:
            problems.extend(merged_attach_borrow_problems(text))
        if not problems:
            rendered = merged_attach_base_table_lines_in_text(text)
            if rendered != list(table_lines):
                problems.append(
                    "渲染出的 CS 里那张基址表与本次 plan 生成的行不一致（模板/生成器漂移）："
                    f"渲染 {rendered} vs 生成 {list(table_lines)}"
                )
            else:
                problems.extend(
                    merged_attach_base_table_audit(
                        ranges, slots, table_lines=rendered
                    ).get("problems")
                    or ()
                )
        if problems:
            # 【t73·兜底原则】合并专属自检失败 ⇒ 降级（本族的 CS 内容不可信，就不发这一段）。
            self._himi_degrade_to_ordinary(
                "合并 attach CS 自检失败（生成器与判据不一致）：" + "；".join(problems)
            )
            return {"ok": False, "path": "", "problems": list(problems)}
        target_dir = os.path.join(GlobalConfig.path_generate_mod_folder(), "res")
        os.makedirs(target_dir, exist_ok=True)
        path = os.path.join(target_dir, MERGED_ATTACH_HLSL_BASENAME)
        payload = text.encode("utf-8")
        written = True
        try:
            with open(path, "rb") as handle:
                written = handle.read() != payload
        except OSError:
            written = True
        if written:
            with open(path, "wb") as handle:
                handle.write(payload)
        result = {
            "ok": True,
            "path": path,
            "written": written,
            "bytes": len(payload),
            "message": ("已写入" if written else "内容一致、未重写") + f" res/{MERGED_ATTACH_HLSL_BASENAME}",
        }
        self._himi_attach_cs_result = result
        if written:
            print(f"{_LOG_PREFIX} {result['message']} → {path}")
        return result

    def _himi_enforce_attach_cs_references(self, ini_path: str) -> list:
        """落盘 CS 之后，把**产物 ini 文件**读回来跑 `cs =` 硬门；返回问题清单。"""
        try:
            with open(ini_path, encoding="utf-8") as handle:
                ini_text = handle.read()
        except OSError as exc:
            return [f"产物 ini 读不回来（{ini_path}）：{exc}"]
        return verify_cs_references(ini_text, GlobalConfig.path_generate_mod_folder())

    def _himi_raise_for_attach_cs_references(self, ini_path: str) -> None:
        """合并 attach CS 的 `cs =` 引用硬门：**只在合并路径激活时被调用**（t73）。

        调用点已被 ``_himi_merged_attach_active()`` 门住 ⇒ 普通导出看不到它
        （旧行为是在普通路径上也执行 ⇒ 把"整份 ini 里没有任何 `cs =` 引用"
        误判成"缺失引用"而中止，用户实报的第二次误拦）。
        失败通道按【t73·兜底原则】走**降级**，不再是 `Fatal`。

        与 :meth:`_himi_enforce_attach_cs_references` 分开是为了让两态复现可以单独调用
        这两步（问题清单 / 中止行为），而不是靠异常文本猜。
        """
        problems = self._himi_enforce_attach_cs_references(ini_path)
        if not problems:
            return
        # 【t73·兜底原则】合并侧失败 ⇒ 降级为普通导出 + 大声警告（普通导出不可被剥夺）。
        self._himi_degrade_to_ordinary(
            "合并骨架 attach CS 的 `cs =` 引用硬门未通过：" + "；".join(problems)
        )

    def _himi_enforce_attach_range_references(self, ini_path: str) -> list:
        """【F5 硬门】读回产物 ini，核对每个 ``cs-t1`` 身份资源都指向真实文件。"""
        try:
            with open(ini_path, encoding="utf-8") as handle:
                ini_text = handle.read()
        except OSError as exc:
            return [f"产物 ini 读不回来（{ini_path}）：{exc}"]
        return verify_attach_range_references(
            ini_text, GlobalConfig.path_generate_mod_folder()
        ) + verify_borrow_map_references(ini_text, GlobalConfig.path_generate_mod_folder())

    def _himi_raise_for_attach_range_references(self, ini_path: str) -> None:
        problems = self._himi_enforce_attach_range_references(ini_path)
        if not problems:
            return
        # 【t73·兜底原则】合并专属门 ⇒ 降级为普通导出 + 大声警告（不再是 Fatal）。
        self._himi_degrade_to_ordinary(
            "合并骨架 attach 段的 `cs-t1` 身份资源硬门未通过（F5 的「本族 span 上界」"
            "没有身份资源就会退化为恒真判据）：" + "；".join(problems)
        )

    def _himi_enforce_product_ini_sections(self, ini_path: str) -> list:
        """【t39】读回产物 ini，做逐段解析自检（段属性为 0 条 / 悬空属性）。"""
        try:
            with open(ini_path, encoding="utf-8") as handle:
                ini_text = handle.read()
        except OSError as exc:
            return [f"产物 ini 读不回来（{ini_path}）：{exc}"]
        return product_ini_section_problems(ini_text)

    def _himi_raise_for_product_ini_sections(self, ini_path: str) -> None:
        problems = self._himi_enforce_product_ini_sections(ini_path)
        if not problems:
            return
        from ...utils.format_utils import Fatal

        raise Fatal(
            "产物 ini 的逐段解析自检未通过 ⇒ 中止导出（段头位置错了会让整段资源失效）："
            + "；".join(problems[:6])
            + ("；…" if len(problems) > 6 else "")
        )

    def _himi_degrade_to_ordinary(self, reason: str) -> None:
        """【t73·兜底原则】合并骨架侧的任何失败都**不许**中止导出 ⇒ 降级为**普通导出** + 大声警告。

        为什么：合并骨架与普通导出是**两个独立模式**，且**普通导出是兜底、优先级最高** ——
        合并骨架暴毙也必须能出普通模式的产物（用户裁定 2026-09-26）。

        降级的三个动作（缺一不可）：
        1. `self._himi_force_ordinary = True` ⇒ ``_himi_merged_attach_active()`` 变 False，
           合并专属硬门（attach CS 落盘 / `cs =` 引用门 / `cs-t1` 身份资源门）全部**跳过**；
        2. **把计划本身关掉**（`plan.ok=False` / `rebinds=[]`）⇒ 所有以 `plan.ok`、`plan.has_rebinds`、
           `plan.family_ranges` 为门的合并资源与段落**全部退回普通路径**（不会产出半合并的产物）；
        3. **大声警告**：控制台 `print` + `_himi_notify(warning=True)` + 记进
           ``self._himi_degrade_notices`` ⇒ 由 :meth:`himi_diagnostic_lines` 写进**产物 ini 注释块**
           （t18 的三层诊断：控制台 / 日志 / 产物自述）。
        """
        if getattr(self, "_himi_force_ordinary", False) and str(reason) in (
            getattr(self, "_himi_degrade_reasons", None) or []
        ):
            return
        self._himi_force_ordinary = True
        reasons = list(getattr(self, "_himi_degrade_reasons", None) or [])
        reasons.append(str(reason))
        self._himi_degrade_reasons = reasons
        detail = (
            "合并骨架侧失败 ⇒ **已降级为普通导出**（普通导出是兜底，不可被剥夺）：" + str(reason)
        )
        try:
            plan = self.himi_merged_runtime_plan()
        except Exception:  # noqa: BLE001 - 计划不可得本来就走普通路径
            plan = None
        if plan is not None:
            try:
                plan.ok = False
                plan.rebinds = []
                plan.message = detail
                plan.diagnostics.append(detail)
            except Exception:  # noqa: BLE001
                pass
        print(f"{_LOG_PREFIX} 警告 {detail}")
        self._himi_notify(detail, warning=True)
        notices = list(getattr(self, "_himi_degrade_notices", None) or [])
        notices.append(detail)
        self._himi_degrade_notices = notices

    def himi_degraded_to_ordinary(self) -> bool:
        """本次导出是否**已降级为普通导出**（供测试/上层读）。"""
        return bool(getattr(self, "_himi_force_ordinary", False))

    def _himi_purge_merged_only_artifacts(self) -> dict:
        """【t85·残留清理】普通导出时清掉**上一次合并导出**留在产物目录里的合并骨架专属文件。

        为什么必须清（三条依据）：

        1. **不可能改变普通产物的行为**：这些文件只与合并骨架一起才有意义；普通导出的 ini
           对它们**一个引用都没有**（t85 实测：关开关的产物 ini 里 `ResourceHI3MergedSkeleton` /
           `ResourceHI3PaletteFam` / `ResourceHI3AttachRange` / `CustomShaderHI3MergedSkeletonAttach` /
           `himi_attach.hlsl` 命中数**全为 0**，且普通产物 61 个文件里清理名单 0 命中）；
        2. **t73 给用户的判据会失效**：「看产物 `res/` 有没有 `himi_attach.hlsl`：有 = 合并骨架导出、
           没有 = 普通导出」——残留文件会让用户以为还在走合并路径；
        3. 3DMigoto 只加载 ini 里声明过的资源，未引用文件是惰性的，但会随产物一起分发，
           长期留成「看不懂的垃圾」。

        清理范围是**有界名单**（按 `ui/universal/himi.py` 里合并侧的三处写盘点反推）：
        `res/himi_attach.hlsl`（`_himi_write_attach_cs`）、`Meshes/*-AttachRange.buf`
        （`_himi_publish_attach_ranges`）、`Meshes/role_*-MergedSkeleton.buf`（`_himi_publish_skeleton`）、
        `Meshes/*-BlendOrigin.buf`（`_himi_write_blend_origin` 的合并侧旁写）。

        返回账目：``{"removed": [...], "failed": [...]}``。
        """
        import glob as _glob
        root = str(GlobalConfig.path_generate_mod_folder() or "")
        ledger: dict = {"removed": [], "failed": []}
        if not root or not os.path.isdir(root):
            return ledger
        candidates: list = []
        candidates.append(os.path.join(root, "res", MERGED_ATTACH_HLSL_BASENAME))
        meshes = os.path.join(root, "Meshes")
        if os.path.isdir(meshes):
            for pattern in ("*-AttachRange.buf", "*-BlendOrigin.buf", "role_*-MergedSkeleton.buf"):
                candidates.extend(_glob.glob(os.path.join(meshes, pattern)))
        for path in candidates:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    ledger["removed"].append(os.path.relpath(path, root).replace("\\", "/"))
            except OSError as exc:  # noqa: BLE001 - 清理失败不得反过来弄坏导出
                ledger["failed"].append(f"{path}: {exc}")
        if ledger["removed"]:
            print(
                f"{_LOG_PREFIX} 普通导出：已清掉上一次合并导出残留的合并骨架专属文件 "
                f"{len(ledger['removed'])} 个（{', '.join(ledger['removed'][:6])}"
                + ("…" if len(ledger["removed"]) > 6 else "") + "）"
            )
        for item in ledger["failed"]:
            self._himi_notify(f"合并骨架残留清理失败：{item}", warning=True)
        return ledger

    def _himi_merged_attach_active(self) -> bool:
        """【t73】本次导出**是否真的在产出合并骨架**（= attach CS 的"写方"语义存在）。

        用于把「合并骨架专属的硬门」限制在合并骨架路径上：

        * 开关关闭 ⇒ ``plan.ok = False``（`himi_merged_runtime.plan_from_metadata` 里"绝不消费
          合并数据"那条）⇒ **普通导出**：没有合并骨架资源、也没有任何 `cs =` 引用
          ⇒ 那些硬门必须**跳过**（旧行为会把"没有引用"误判成"缺失引用"而中止，用户实报第二次误拦）；
        * 开关打开但计划本身没起来（元数据不全等）⇒ 同样没有写方 ⇒ 也跳过；
        * 开关打开且计划可用 ⇒ **激活**：落 CS + `cs =` 引用硬门 + `cs-t1` 身份资源硬门照旧全部执行。
        """
        try:
            if not bool(GlobalProterties.import_merged_vgmap()):
                return False
        except Exception:  # noqa: BLE001 - 拿不到开关状态一律按"未激活"（安全侧：跳过专属硬门）
            return False
        if bool(getattr(self, "_himi_force_ordinary", False)):
            return False          # 【t73】已降级 ⇒ 合并专属硬门一律不跑
        try:
            plan = self.himi_merged_runtime_plan()
        except Exception:  # noqa: BLE001
            return False
        return bool(getattr(plan, "ok", False))

    def generate_unity_vs_config_ini(self):
        """基类 ini 生成 + 【t75 收尾执行点】落 CS + `cs =` 引用硬门。

        **顺序是契约的一部分**（报告 §30 的依赖顺序）：

        1. 基类先把 ini 落到 ``<mod>/<workspace>.ini``；
        2. 本方法随后把**一个** ``res/himi_attach.hlsl`` 写到 ``<mod>/res/``（``mkdir`` +
        幂等写）；
        3. 再把 ini 读回来，逐条核对每个 `cs =` 都能解析到真实文件、且 5 条指向同一路径；
        4. 任一不成立 ⇒ ``Fatal`` 中止导出（**不降级为 warning、不跳过**）。

        **为什么必须先落 CS + 过硬门，才能撤掉骨架资源的 `filename`**：骨架是**活资源**
        （每帧由 attach CS 填充，ini 里不带 ``filename``）。若先撤 ``filename`` 而 CS 文件
        还不存在/不被引用，则从第一帧起就**没有任何写方** —— 骨架留空 ⇒ 顶点位置全零 ⇒ 模型
        爆炸，且**全程不报错**。所以顺序只能是「先有写方，再撤静态来源」。
        """
        super().generate_unity_vs_config_ini()
        ini_path = os.path.join(
            GlobalConfig.path_generate_mod_folder(),
            GlobalConfig.get_workspace_name() + ".ini",
        )
        # 【t73】落 CS + 两个"合并骨架专属"的引用硬门**只在合并骨架路径激活时**执行。
        # 普通导出（开关关闭）根本没有 attach CS、也没有 `cs =`/`cs-t1` 引用 ⇒ 旧行为会把
        # "整份 ini 里没有任何 `cs =` 引用"当成缺失而中止（用户实报的第二次误拦）。
        if self._himi_merged_attach_active():
            self._himi_write_attach_cs()
            self._himi_raise_for_attach_cs_references(ini_path)
            # 【F5】身份资源（`cs-t1`）的文件存在性硬门：缺一族 ⇒ 那族的 span 上界退回冻结表。
            self._himi_raise_for_attach_range_references(ini_path)
        else:
            print(
                f"{_LOG_PREFIX} 普通导出（未启用合并骨架）：跳过 attach CS 落盘与 `cs =`/`cs-t1` "
                "引用硬门 —— 本次不会产出合并骨架资源，也就没有这两类引用"
            )
            self._himi_attach_cs_result = {"skipped": True, "reason": "merged_path_inactive"}
            self._himi_attach_cs_paths = []
            # 【t85】普通导出：清掉上一次合并导出残留的合并骨架专属文件（含 res/himi_attach.hlsl），
            # 否则 t73 给用户的那条判据（「res/ 有 himi_attach.hlsl = 合并骨架导出」）会失效。
            self._himi_merged_residue = self._himi_purge_merged_only_artifacts()
        # 【t39 / §13.17】产物 ini 的逐段解析自检：任何段"属性为 0 条"即 Fatal
        # （防重演 2026-09-26「段头插在别的段属性行之间 ⇒ 骨架段被清空」）。**两种模式都要跑**。
        self._himi_raise_for_product_ini_sections(ini_path)

    # ------------------------------------------------------------------ #
    # 供测试/上层读取的检查点
    # ------------------------------------------------------------------ #

    def himi_merged_contract_decision(self) -> dict:
        return dict(self._himi_contract_decision or {})

    def himi_merged_diagnostics(self) -> str:
        plan = self.himi_merged_runtime_plan()
        return plan.diagnostics_text()

    def himi_merged_publish_result(self) -> dict:
        return dict(self._himi_publish_result or {})

    def himi_blend_namespace_verdicts(self) -> dict:
        """每个 DrawIB 的 Blend 骨骼编号命名空间核对结果（global/local/…）。"""
        return {str(key): dict(value) for key, value in self._himi_blend_verdicts.items()}


__all__ = [
    "ExportHIMI",
    "UNVERIFIED_IN_GAME",
    "DEFORM_GATE_REJECT_PREFIX",
    "MERGED_ATTACH_HLSL_BASENAME",
    "MERGED_ATTACH_HLSL_PATH",
    "MERGED_ATTACH_SLOTS_MACRO",
    "MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES",
    "MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES",
    "merged_attach_base_table_lines",
    "merged_attach_base_table_lines_in_text",
    "merged_attach_base_table_text_problems",
    "merged_attach_hlsl_text",
    "merged_attach_hlsl_problems",
    "merged_attach_reference_problems",
    "merged_attach_base_table_rows",
    "merged_attach_base_table_problems",
    "verify_cs_references",
    "verify_attach_range_references",
    "verify_borrow_map_references",
    "deform_replay_block_problems",
    "merged_deform_key_problems",
    "deform_draw_structure_problems",
    "deform_draw_structure_judged",
    "product_ini_section_problems",
]
