import bpy
import os
import re
import struct
import shutil
import time
from collections import OrderedDict

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from .node_postprocess_base import SSMTNode_PostProcess_Base
from . import deform_chain
from .drag_collision import bake_sweep_order, bake_cloth_adjacency, surface_shader_sections
from .variable_registry import normalize_variable_name
from ..common.object_prefix_helper import ObjectPrefixHelper, coerce_index
from ..common.mod_path_compat import (
    find_base_position_resource_name,
    ensure_resource_alias_section,
    iter_position_buffer_candidates,
)
from ..common.logic_name import LogicName
from ..common.safe_write import write_text_if_changed, copy_file_if_changed
from ..utils.export_space import position_export_matrix

try:
    from ..toolkit import gb_core
    GB_CORE_AVAILABLE = True
except Exception:
    GB_CORE_AVAILABLE = False


# ---------------------------------------------------------------------------
# 稀疏区域 ABI
# ---------------------------------------------------------------------------

MAX_ZONES = 256
DEFAULT_MOD_NAMESPACE = "A"
SPARSE_ZONE_SLOTS = 4
INVALID_ZONE_ID = 0xFFFFFFFF
ZONES_PER_PAGE = 1
VAR_SYNC_VALUE_BASE = 81
VAR_SYNC_MODE_BASE = 90

# 合并骨架（ZZMI MergedSkeleton）在产物 ini 里的稳定标记：R-C 用它把「合并骨架上必然出现
# 且良性」的两条诊断从 WARNING 降级为 INFO（`多个 base vertex` / `buf 与 VLR 不一致`）。
# 依据 review-reports/t82-shapekey-drag-retest.md §2.3：合并骨架给被吸收件补了 3 行前缀
# stub，于是同一 DrawIB 的多个 part 有不同 base vertex（实测 {0,3}），且
# `override_vertex_count = 合并 SO 总行数` 而 `Position.buf = 本件导出行数`（实测差 3 行）。
MERGED_SKELETON_PRODUCT_MARKERS = (
    "ZZMIMergedSkeleton",
    "ResourceZZMergedSkeleton",
    "zz_ms_seen_",
    "zz_ms_prev_",
    "ResourceZZRedirectSO",
)
# t88 绑定只准引用产物里真实存在的合并骨架组，且只准用本组件自己的出现次计数器：
#   组号 ← 本件 deform 段里引用的 ResourceZZMergedSkeleton_G<n>_s<k>（同一产物可以有多个
#          组，各组绝不共享骨架缓冲，只有本件自己那段能判定归属）；
#   cid  ← 同一段里的 $zz_ms_occ_<cid>（zzmi 生成侧按「合并组件序号」命名）。
MERGED_SKELETON_SECTION_RE = re.compile(r"^\[ResourceZZMergedSkeleton_G(\d+)_s\d+\]$")
MERGED_SKELETON_REF_RE = re.compile(r"ResourceZZMergedSkeleton_G(\d+)_s\d+\b")
MERGED_SKELETON_OCC_RE = re.compile(r"\$zz_ms_occ_(\d+)\b")
VAR_SYNC_FLOAT4_CAPACITY = 9
VAR_SYNC_MAX_BINDINGS = VAR_SYNC_FLOAT4_CAPACITY * 4


def _warn_missing_merged_skeleton_binding(comp_name, reason):
    """t88 绑定无法从产物唯一确定时告警：宁可整块不发射，也不 ref 一个不存在的资源。

    猜错组号会让 cs-t88 = ref ResourceZZMergedSkeleton_G<n>_s<k> 指向产物里没有的资源
    （3DMigoto 加载即报错），猜错 cid 会让本件读到别人的出现次计数器。
    """
    message = (
        f"[DragInteraction] 组件 {comp_name or '<未知>'} 未发射 cs-t88 合并骨架绑定：{reason}。"
        "碰撞面蒙皮因此不跟随骨架（绑定产物里不存在的资源会让 3DMigoto 加载失败）；"
        "若该组件本应跟随骨架，请确认它确实参与了合并骨架导出。"
    )
    try:
        from ..utils.log_utils import LOG as _LOG
    except Exception:
        print(message)
        return
    _LOG.warning(message)


def _node_identity_key(node):
    """返回节点稳定身份键，避免 bpy 包装对象被反复创建导致 id()/is 失效。"""
    if node is None:
        return None
    as_pointer = getattr(node, "as_pointer", None)
    if callable(as_pointer):
        try:
            return ("pointer", as_pointer())
        except Exception:
            pass
    tree_name = getattr(getattr(node, "id_data", None), "name", "") or ""
    node_name = getattr(node, "name", "") or ""
    if tree_name or node_name:
        return ("name", tree_name, node_name)
    return ("id", id(node))


def is_postprocess_node_on_export_chain(tree, target_node):
    """判断后处理节点是否从结果输出节点可达；无结果节点时兼容旧数据并视为可达。"""
    result_nodes = [
        node for node in (getattr(tree, "nodes", None) or [])
        if getattr(node, "bl_idname", "") in {
            "SSMTNode_Result_Output",
            "SSMTNode_Result_Output_NTMIModImp", "SSMTNode_VeloExportBridge",
        }
    ]
    if not result_nodes:
        return True

    target_key = _node_identity_key(target_node)
    if target_key is None:
        return False

    visited = set()
    pending = list(result_nodes)
    while pending:
        node = pending.pop()
        node_key = _node_identity_key(node)
        if node_key is None or node_key in visited:
            continue
        visited.add(node_key)
        if node_key == target_key:
            return True

        sockets = list(getattr(node, "outputs", None) or [])
        sockets.extend(getattr(node, "inputs", None) or [])
        for socket in sockets:
            for link in getattr(socket, "links", None) or []:
                for neighbor in (
                    getattr(link, "from_node", None),
                    getattr(link, "to_node", None),
                ):
                    neighbor_key = _node_identity_key(neighbor)
                    if neighbor_key is None or neighbor_key == node_key:
                        continue
                    neighbor_type = getattr(neighbor, "bl_idname", "")
                    if neighbor_type == "NodeReroute" or neighbor_type.startswith(
                        "SSMTNode_PostProcess_"
                    ):
                        pending.append(neighbor)
    return False

SHADER_FILES = (
    "rzm_gs_probe.hlsl",
    "rzm_object_detect.hlsl",
    "rzm_pin_detected.hlsl",
    "rzm_jiggle_screen_state.hlsl",
    "rzm_jiggle_interaction.hlsl",
    "rzm_vis_publish.hlsl",
)
SHADER_COLLISION_FILES = ("rzm_drag_collision_bounds.hlsl", "rzm_drag_collision_surface.hlsl")
# 形态键驱动 / 变量同步着色器不随基础组无条件复制：前者仅在“启用形态键联动”
# （F1）时拷贝，后者在“启用变量联动”（F4）时拷贝——避免段族已发射但文件未落盘
# 的 3Dmigoto 加载期缺失（漏拷 = 悬空引用，见 phase2/n1 §5.3）。
SHADER_DRIVE_FILES = ("rzm_shapekey_drive.hlsl",)
SHADER_VARSYNC_FILES = ("rzm_shapekey_var_sync.hlsl",)
HAND_SHADER_FILES = ("rzm_jiggle_cursor_preview.hlsl", "rzm_jiggle_hand.hlsl", "rzm_jiggle_cursor.hlsl")
# 手部网格/法线资产（二进制，原样复制）
HAND_ASSET_FILES = (
    "HandAction.buf", "HandAction.ib", "HandAction_Normal.buf",
    "HandNoAction.buf", "HandNoAction.ib", "HandNoAction_Normal.buf",
)
# 视口探针着色器（不读角色网格，无 struct VertexAttributes，字节级原样复制）
VIEWPORT_SHADER_FILES = (
    "rzm_viewport_layout_vs.hlsl",
    "rzm_viewport_layout_probe.hlsl",
    "rzm_viewport_layout_decode.hlsl",
)
DEFAULT_VERTEX_STRUCT = (
    "struct VertexAttributes {\n"
    "    float3 position;\n"
    "    float3 normal;\n"
    "    float4 tangent;\n"
    "};"
)

DRAG_TAIL_MARKER = "; --- AUTO-APPENDED DRAG INTERACTION MODULE ---"
MESH_COMMENT_RE = re.compile(r"^;\s*\[mesh:(?P<object_name>[^\]]+)\]", re.IGNORECASE)
# TTL 二次绘制的区间参数：$\TTL\_1 = 索引数，$\TTL\_2 = 起始索引，
# $\TTL\_3 = 起始顶点/base vertex；run 目标形如
# CommandListSSMTTTLDraw_<token>（TTL 库 drawindexedinstanced 读取 $_1/$_2/$_3）。
TTL_ARG1_RE = re.compile(r'^\$\\TTL\\_1\s*=\s*(\d+)', re.IGNORECASE)
TTL_ARG2_RE = re.compile(r'^\$\\TTL\\_2\s*=\s*(\d+)', re.IGNORECASE)
TTL_ARG3_RE = re.compile(r'^\$\\TTL\\_3\s*=\s*(\d+)', re.IGNORECASE)
TTL_DRAW_RUN_RE = re.compile(r'^run\s*=\s*CommandList(?:SSMTTTLDraw_|\\TTL\\Draw\b)', re.IGNORECASE)
# TTL 改成同段内联后，原绘制行被注释接管，参数留在注释里；收集绘制站点时
# 必须把它当成一笔正常绘制读，否则这个网格的钩子无处可挂。
TTL_TAKEN_DRAW_RE = re.compile(r'^;\s*\[SSMT TTL 已接管\]\s*(drawindexed\w*\s*=\s*.+)$',
                               re.IGNORECASE)
# 物体显隐 flag 行（注入绘制分支内；material 的 TTL 块重建必须保留）
DRAG_OBJVIS_LINE_RE = re.compile(r'^\s*\$ssmtdrag_objvis_[\w]*\s*=\s*1\s*$')

# 导出的着色器在 ini 中的引用路径（mod 根 → res/drag_interaction/）
RES_SHADER_DIR = "res/drag_interaction"


# ===== t33 explosion guard（爆炸守卫，生成器侧同源注入）=========================
# 守卫必须同时活在生成器里：_copy_shaders() 把 Toolset/*.hlsl 写进 mod 之前调用
# _inject_explosion_guard()，这样即使 Toolset 的 hlsl 被回退到未打补丁版本，
# 重新导出仍带着守卫（acceptance ④）。锚点缺失时只告警、原样放行，绝不阻断导出。
_EXPLOSION_GUARD_OPS = [
    # (名称, 正则, 替换) —— 与 .dbg/rzm15/make_guard_patch.py 的 OPS 同源
    ('G0-定义块', '(#define NORMAL_SIGN_MODE[^\\n]*\\n)', '\\1\n// ===== t33 explosion guard（爆炸守卫：非有限值消毒 + 位移硬上限）=================\n// 痛点（逐条数值证据见 .dbg/rzm15/probe_explosion.txt / .dbg/rzm15/verify_guard.txt（未入库的本地取证存档）：\n//   M1 非有限传播：物理状态/区域参数/上游任一环产生 NaN 时，\n//      ClampVectorLength（`lenSq > maxSq` 对 NaN 为假）、PullTowardLimit\n//      （`dragLen < 1e-6` 对 NaN 为假）、pow(saturate(..)) 全部“比较为假 ⇒ 原样返回”，\n//      NaN 一路活到 `v.position += appliedOffset` 并写回 rw_buffer。\n//   M2 求解器把静态错位放大成位移：CollisionSolveGrid 的收尾投影环每步最多 gap\n//      （≈1.7mm）× 64 步；碰撞面与当前姿态不一致时，**零位移**顶点也会被推走\n//      （实测 0.1 m 错位 ⇒ 单帧最大 0.0649 m，58/883 行打到 64 步上限）。\n//   M3 硬赋值无界：T16 滑移 `v.position = q1 + sn * gRow` 与 localPos 无关联，\n//      锚点/蒙皮面出错时位移是米级（实测 0.1~1.0 m）。\n// 守卫只做两件事，且对合法输入逐位不生效：\n//   (a) 任何非有限的中间量 → 回退到 fallback（通常是 0 或 localPos）；\n//   (b) 写回前的位移（相对 localPos）按 GUARD_MAX_OFFSET 限幅（保方向、缩长度）。\n// GUARD_MAX_OFFSET 默认 = maxOffset 的合法过冲余量：实测求解器正常收敛时会略超\n// maxOffset（A -n0 0.5 → 0.500758，+0.15%；A -n0 0.25 → 0.25029），这类过冲不该被\n// 守卫改掉（否则用户合法拖满时手感变化）；再加一次 gap 级推挤（|t0| ≤ 1.74mm）的余量。\n// 可用 IniParams[119].w 覆盖（0 = 用默认；非 0 = 绝对上限，调试用）。\n// 注意 .x 是滑移总闸、.z 已被 t7 的 NORMAL_SIGN_MODE 占用。\n#define GUARD_MAX_OFFSET (ZONE_RUNTIME_META.w > 0.0 ? ZONE_RUNTIME_META.w : (maxOffset * 1.01 + 0.005))\n// 求解器内部单步上限（投影环步长 / CCD 推挤量）：合法单步是 gap 级（≈1.7mm），\n// 50mm 已是 30 倍余量；超了说明碰撞面与当前姿态不一致（实机顶点到烘焙锚点距离\n// 中位数 0.5015m，姿态差半个身位）——此时宁可停在原处，也不把顶点瞬移半米。\n#define GUARD_MAX_STEP 0.05\n#define GUARD_EPS 1e-9\n\nfloat3 GuardFinite3(float3 v, float3 fallback)\n{\n    return all(isfinite(v)) ? v : fallback;\n}\n\n// 保方向、缩长度的位移限幅：|p - anchor| <= maxLen；非有限 ⇒ 直接回退到 anchor。\nfloat3 GuardClampDelta(float3 p, float3 anchor, float maxLen)\n{\n    float3 d = p - anchor;\n    float len2 = dot(d, d);\n    if (!all(isfinite(d)) || !isfinite(len2))\n        return anchor;\n    float maxLen2 = maxLen * maxLen;\n    if (len2 > maxLen2 && len2 > GUARD_EPS)\n        return anchor + d * (maxLen * rsqrt(len2));\n    return p;\n}\n// ===== end t33 explosion guard ===============================================\n'),
    ('G1-入参行消毒', '(    VertexAttributes v = base_buffer\\[i\\];\\n    float3 localPos = ToJiggleSpace\\(v\\.position\\);\\n)', '\\1    // t33 守卫：输入行本身非有限 ⇒ 整行原样透传（绝不让 NaN 进入物理/碰撞/写回）\n    if (!all(isfinite(v.position)) || !all(isfinite(v.normal)))\n    {\n        rw_buffer[i] = base_buffer[i];\n        return;\n    }\n'),
    ('G2-appliedOffset 消毒+源侧限幅', '    float3 appliedOffset = influence > 0\\.0 \\? OffsetToVertexSpace\\(nextCurrent\\.xyz \\* influence\\) : float3\\(0\\.0, 0\\.0, 0\\.0\\);\\n', '    // t33 守卫：物理位移先消毒（nextCurrent 携带上一帧状态，NaN 会一路传下去），\n    // 再在**源侧**按 maxOffset 硬限幅（captain 线索 4；写回口还有一道同值限幅）\n    float3 appliedOffset = influence > 0.0\n        ? GuardFinite3(OffsetToVertexSpace(nextCurrent.xyz * influence), float3(0.0, 0.0, 0.0))\n        : float3(0.0, 0.0, 0.0);\n    appliedOffset = ClampVectorLength(appliedOffset, maxOffset);\n'),
    ('G3-pathOffset 消毒', '(    if \\(sockSlideLen > SOCK_STROKE\\) pathOffset \\*= SOCK_STROKE / max\\(sockSlideLen, 1e-6\\);\\n)', '\\1    pathOffset = GuardFinite3(pathOffset, float3(0.0, 0.0, 0.0));   // t33 守卫\n'),
    ('G4-T16 硬赋值限幅', '            v\\.position = q1 \\+ sn \\* gRow;\\n', '            // t33 守卫：T16 滑移是硬赋值，先消毒再按 GUARD_MAX_OFFSET 相对 localPos 限幅\n            v.position = GuardClampDelta(GuardFinite3(q1 + sn * gRow, localPos), localPos, GUARD_MAX_OFFSET);\n'),
    ('G5-碰撞出口消毒+限幅', '(        float3 vnUnit = vnLen > 1e-6 \\? vnRaw / vnLen : float3\\(0\\.0, 1\\.0, 0\\.0\\);\\n)(        v\\.position = CollisionSolve\\(i, localPos, totalOffset, vnUnit\\);\\n)', '\\1        vnUnit = GuardFinite3(vnUnit, float3(0.0, 1.0, 0.0));   // t33 守卫\n        float3 solved = CollisionSolve(i, localPos, totalOffset, vnUnit);\n        solved = GuardFinite3(solved, localPos);                // t33 守卫\n        v.position = GuardClampDelta(solved, localPos, GUARD_MAX_OFFSET);\n'),
    ('G6-写回唯一出口', '(    rw_buffer\\[i\\] = v;\\n\\}\\n)\\Z', '    // t33 守卫：唯一写回出口。非有限 ⇒ 回退本帧输入行；位移按 GUARD_MAX_OFFSET 限幅。\n    v.position = GuardClampDelta(v.position, localPos, GUARD_MAX_OFFSET);\n    if (!all(isfinite(v.position)))\n        v = base_buffer[i];\n    rw_buffer[i] = v;\n}\n'),
    ('G7-投影环 哨兵/NaN/步长上限', '([ \\t]*)if \\(pd > 1e29 \\|\\| pd >= gap\\)\\n[ \\t]*break;[^\\n]*\\n([ \\t]*)p \\+= projN \\* \\(gap - pd\\);\\n', '\\1if (!isfinite(pd) || pd > 1e29 || pd < -1.0)\\n\\1    break;                              // t33：NaN/正哨兵/负哨兵 ⇒ 退出（不拿 pd 修正）\\n\\1if (pd >= gap)\\n\\1    break;                              // 已达标 ⇒ 停\\n\\1float3 guardStep = projN * (gap - pd);\\n\\1if (!all(isfinite(guardStep))\\n\\1    || dot(guardStep, guardStep) > GUARD_MAX_STEP * GUARD_MAX_STEP)\\n\\1    break;                              // t33：步长非有限/超大 ⇒ 停在原处\\n\\2p += guardStep;\\n'),
    ('G8-CollisionSolve 入口消毒', '(float3 CollisionSolve\\(uint i, float3 p0, float3 offset, float3 vn\\)\\n\\{\\n)', '\\1    // t33 守卫：入口消毒（p0/offset/vn 非有限时不做任何求解，原样返回）\n    if (!all(isfinite(p0)) || !all(isfinite(offset)) || !all(isfinite(vn)))\n        return p0;\n'),
    ('G9-物理状态写回消毒', '(        JiggleState\\[0u\\] = nextCurrent;\\n        JiggleState\\[1u\\] = nextPrevious;\\n        JiggleState\\[2u\\] = nextCenter;\\n        JiggleState\\[3u\\] = nextNormal;\\n        JiggleState\\[4u\\] = nextTarget;\\n        JiggleState\\[5u\\] = nextPrevTarget;\\n        JiggleState\\[6u\\] = nextScreenRight;\\n        JiggleState\\[7u\\] = nextScreenDown;\\n        JiggleState\\[8u\\] = nextInputFlags;\\n)', '        // t33 守卫：状态写回前消毒（非有限 ⇒ 保留上一帧状态，再不行 ⇒ 归零；\n        // 否则一帧 NaN 会让弹簧永久卡死，表现为「拖不动」）\n        float4 t33St[9];\n        t33St[0] = nextCurrent; t33St[1] = nextPrevious; t33St[2] = nextCenter;\n        t33St[3] = nextNormal;  t33St[4] = nextTarget;   t33St[5] = nextPrevTarget;\n        t33St[6] = nextScreenRight; t33St[7] = nextScreenDown; t33St[8] = nextInputFlags;\n        [unroll]\n        for (uint t33k = 0u; t33k < 9u; ++t33k)\n        {\n            float4 t33prev = ReadState(t33k, float4(0.0, 0.0, 0.0, 0.0));\n            t33St[t33k] = all(isfinite(t33St[t33k]))\n                ? t33St[t33k]\n                : (all(isfinite(t33prev)) ? t33prev : float4(0.0, 0.0, 0.0, 0.0));\n            JiggleState[t33k] = t33St[t33k];\n        }\n'),
    ('G10-CCD 双侧哨兵', '([ \\t]*)if \\(d > 1e29\\)\\n[ \\t]*continue;[^\\n]*\\n', '\\1// t33 守卫：正哨兵（打空）/负哨兵/NaN 一律不参与采样 —— 负哨兵会经\\n\\1// (gap - dStar) 被放大成 INF（captain 线索 1/2。本数据实测负哨兵未出现，\\n\\1// 属纵深防御：值域扫描见 probe_distrange.json）\\n\\1if (!isfinite(d) || d > 1e29 || d < -1.0)\\n\\1    continue;\\n'),
    ('G12-CCD 推挤量设界', '(        float3 pStarOut = pStar \\+ pushDir \\* \\(gap - dStar\\);\\n)', '        // t33 守卫：推挤量设界 —— 碰撞面陈旧时 gap - dStar 可达半身位（实机顶点\\n        // 到烘焙锚点距离中位数 0.5015m），此时宁可停在原处也不瞬移\\n        float guardPush = gap - dStar;\\n        if (!isfinite(guardPush) || guardPush > GUARD_MAX_STEP || guardPush < -GUARD_MAX_STEP)\\n            guardPush = 0.0;\\n        float3 pStarOut = pStar + pushDir * guardPush;\\n'),
]



def _require_toolset_file(path):
    """生成器依赖文件缺失 ⇒ 硬失败，中止导出。

    为什么不能只告警：发布包按 ``git ls-files`` 取文件 ⇒ 未被 git 跟踪的产物在干净检出里
    缺席；若这里只打一行 WARNING 继续，用户重导出会得到「ini 引用了不存在的 CS」的半成品
    模组（3DMigoto 侧 Shader not found、静默失效，看起来像修复根本没做）。
    """
    if not os.path.exists(path):
        raise RuntimeError(
            "[DragInteraction] 必需文件缺失，导出中止: %s\n"
            "  该文件是生成器依赖；发布包只包含 git 跟踪的文件 ⇒ 未入库或缺席时重导出会产出半成品模组。\n"
            "  修复：git add 该文件，或把它内嵌进生成器。" % path
        )
    return path




def _inject_explosion_guard(text):
    """t33 爆炸守卫注入（幂等）。返回注入后的文本；锚点不唯一时原样返回并告警。"""
    if "GUARD_MAX_OFFSET" in text:
        return text
    out = text
    for name, pat, rep in _EXPLOSION_GUARD_OPS:
        matches = list(re.finditer(pat, out))
        if len(matches) != 1:
            print(
                f"[DragInteraction][WARNING] 爆炸守卫锚点 {name} 命中 {len(matches)} 次，"
                "跳过注入（着色器保持原样，请在 Toolset 侧修复守卫）"
            )
            return text
        m = matches[0]
        out = out[: m.start()] + re.sub(pat, rep, out[m.start(): m.end()], count=1) + out[m.end():]
    return out
# ===== end t33 explosion guard ===============================================


















# ===== end T36 stage/cloth wiring ============================================


# ---------------------------------------------------------------------------
# 空物体区域参数 PropertyGroup
# ---------------------------------------------------------------------------

class SSMT_DragZoneIncludeRef(bpy.types.PropertyGroup):
    """SSMT_DragZoneSettings.include_objects 里的一项：指向一个 MESH 物体。"""
    object: bpy.props.PointerProperty(
        name="物体",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )


class SSMT_DragZoneColliderRef(bpy.types.PropertyGroup):
    """SSMT_DragZoneSettings.collider_objects 里的一项：指向一个作为碰撞体的 MESH 物体。"""
    object: bpy.props.PointerProperty(
        name="碰撞体",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )


class SSMT_DragZoneSettings(bpy.types.PropertyGroup):
    """挂在一个 Empty 上的区域参数（画刷 + 拖拽物理）。"""

    # 画刷参数
    brush_strength: bpy.props.FloatProperty(name="画刷强度", default=1.0, min=0.0, max=4.0)
    brush_falloff_k: bpy.props.FloatProperty(name="画刷衰减 k", default=4.6, min=0.1, max=50.0)
    enabled: bpy.props.BoolProperty(name="启用", default=True)
    propagate: bpy.props.BoolProperty(
        name="沿表面扩散",
        description="此权重球开启时沿网格表面测地扩散；关闭回退体积球（仅作用于本球）",
        default=True,
    )
    include_objects: bpy.props.CollectionProperty(type=SSMT_DragZoneIncludeRef)

    # 碰撞体对象列表（留空 = 该区域不参与碰撞约束）
    collider_objects: bpy.props.CollectionProperty(type=SSMT_DragZoneColliderRef)
    collision_override: bpy.props.IntProperty(
        name="碰撞开关",
        description="0=继承节点开关，1=强制开启，2=强制关闭（仅模式三/拉扯模式生效）",
        default=0, min=0, max=2,
    )

    # 拖拽参数（0 = 继承回退到全局）
    radius: bpy.props.FloatProperty(name="影响半径", default=0.0, min=0.0)
    strength: bpy.props.FloatProperty(name="拖拽强度", default=0.0, min=0.0)
    max_offset: bpy.props.FloatProperty(name="最大位移", default=0.0, min=0.0)
    falloff: bpy.props.FloatProperty(name="衰减", default=0.0, min=0.0)
    damping: bpy.props.FloatProperty(name="阻尼", default=0.0, min=0.0)
    grabbable: bpy.props.BoolProperty(name="可抓取", default=True)
    drag_along_normal: bpy.props.BoolProperty(
        name="法线方向拖拽",
        description=(
            "开启后拖拽方向严格锁定为「点中那个面的法线」：鼠标移动只决定沿法线的推进量"
            "（位移长度 = 强度），不再把拖拽指向鼠标方向。"
            "关闭则沿用 screen-stable 固定局部轴拖拽"
        ),
        default=False,
    )


# ---------------------------------------------------------------------------
# 区域 helper：球级沿表面扩散 + 包含物体列表过滤
# ---------------------------------------------------------------------------


def _zone_propagate(settings, node):
    """球级沿表面扩散开关；旧工程（无 propagate 属性）默认开启（节点级总开关已移除）。"""
    value = getattr(settings, "propagate", None)
    if value is not None:
        return bool(value)
    return True


def _zone_allowed_names(settings):
    """包含列表内物体的候选名（仅按导出运行时后缀归一化，不折叠 Blender 去重后缀）。

    Blender 去重后缀（如 Body.001）在导出 ini 里是真实独立 draw 的物体名一部分
    （ZZMI 合并网格里 .001 与基础名是两个独立网格/IB 区间），不能被折叠——否则
    包含 .001 会连带放行基础名网格，包含列表失效。归一化只做
    ``_blender_object_key`` 的运行时后缀（_copy/_chainN/_dupN/...）剥离。
    """
    include = getattr(settings, "include_objects", None) or ()
    names = set()
    for item in include:
        obj = getattr(item, "object", None)
        if obj is None:
            continue
        candidates = (getattr(obj, "name", None), getattr(obj, "name_full", None))
        for candidate in candidates:
            if not candidate:
                continue
            names.add(str(candidate))
            key = _blender_object_key(candidate)
            if key:
                names.add(key)
    return names


def _blender_object_key(name):
    """把导出 mesh 注释名归一化为场景物体名：仅剥掉导出追加的运行时后缀。

    保留完整 LOD/IB 结构化前缀。不同 IB 可能包含同名物体，前缀用于区分 IB，
    不能被剥掉。
    """
    if not name:
        return ""
    clean = str(name).strip()
    try:
        clean, _suffix = ObjectPrefixHelper._strip_runtime_suffix(clean)
    except Exception:
        pass
    return clean.strip()


def _zone_has_included_objects(settings):
    """返回该权重球是否配置了非空的包含物体列表（导出侧据此读取 IB 部件名）。"""
    include = getattr(settings, "include_objects", None)
    if include is None:
        return False
    try:
        return len(include) > 0
    except (TypeError, ValueError):
        return False


def _zone_allowed_by_target(settings, target_name):
    """预览单物体：包含列表为空 → 全部允许；否则仅列表内物体名命中。
    列表项全部无效（物体指针已清空）时同样按全部允许，避免预览被误过滤。"""
    include = getattr(settings, "include_objects", None) or ()
    if not include or len(include) == 0:
        return True
    if not target_name:
        return True
    allowed_names = _zone_allowed_names(settings)
    if not allowed_names:
        return True
    return str(target_name) in allowed_names


def _zone_allowed_vertex_mask(settings, vertex_count, triangles, tri_part_names):
    """烘焙侧：包含列表 → (N,) bool 只允许列表内部件覆盖的顶点；空列表/无拓扑 → None（全允许）。

    tri_part_names 与 triangles 逐行对齐（object 数组，无注释部件名为 None）。无注释的
    三角形按“无法判定归属”处理为允许，避免旧工程无 mesh 注释时权重被清零。
    """
    include = getattr(settings, "include_objects", None) or ()
    if not include or len(include) == 0:
        return None
    allowed_names = _zone_allowed_names(settings)
    if not allowed_names or triangles is None or tri_part_names is None:
        return None
    mask = np.zeros(int(vertex_count), dtype=bool)
    tri_names = np.asarray(tri_part_names)
    tri_keys = np.asarray([_blender_object_key(name) for name in tri_part_names], dtype=object)
    for name in allowed_names:
        name_key = _blender_object_key(name)
        tri_sel = (tri_names == name) | (tri_names == name_key) | (tri_keys == name) | (tri_keys == name_key)
        idx = triangles[tri_sel].reshape(-1)
        idx = idx[idx < int(vertex_count)]
        mask[idx] = True
    # 无注释部件按允许处理（无法判定归属，不因列表过滤而清零）
    none_sel = tri_names == None  # noqa: E711
    if np.any(none_sel):
        idx = triangles[none_sel].reshape(-1)
        idx = idx[idx < int(vertex_count)]
        mask[idx] = True
    return mask


def _collider_allowed_names(settings):
    """碰撞体列表内物体的候选名（仅按导出运行时后缀归一化，与 _zone_allowed_names 同规约）。"""
    colliders = getattr(settings, "collider_objects", None) or ()
    names = set()
    for item in colliders:
        obj = getattr(item, "object", None)
        if obj is None:
            continue
        candidates = (getattr(obj, "name", None), getattr(obj, "name_full", None))
        for candidate in candidates:
            if not candidate:
                continue
            names.add(str(candidate))
            key = _blender_object_key(candidate)
            if key:
                names.add(key)
    return names


def _collider_vertex_mask(settings, vertex_count, triangles, tri_part_names):
    """烘焙侧：碰撞体列表 → (N,) bool 表示属于碰撞体的顶点；空列表/无拓扑 → None。"""
    colliders = getattr(settings, "collider_objects", None) or ()
    if not colliders or len(colliders) == 0:
        return None
    allowed_names = _collider_allowed_names(settings)
    if not allowed_names or triangles is None or tri_part_names is None:
        return None
    mask = np.zeros(int(vertex_count), dtype=bool)
    tri_names = np.asarray(tri_part_names)
    tri_keys = np.asarray([_blender_object_key(name) for name in tri_part_names], dtype=object)
    for name in allowed_names:
        name_key = _blender_object_key(name)
        tri_sel = (tri_names == name) | (tri_names == name_key) | (tri_keys == name) | (tri_keys == name_key)
        idx = triangles[tri_sel].reshape(-1)
        idx = idx[idx < int(vertex_count)]
        mask[idx] = True
    return mask


class SSMT_DragZoneRef(bpy.types.PropertyGroup):
    """节点 zone_objects 列表里的一项：指向一个 Empty。"""
    zone_id: bpy.props.IntProperty(
        name="区域 ID", default=-1, min=-1, max=MAX_ZONES - 1,
        description="稳定区域编号；用于运行时命中和 UI 绑定，不随前方区域禁用而改变",
    )
    expanded: bpy.props.BoolProperty(name="展开区域参数", default=False)
    zone_object: bpy.props.PointerProperty(
        name="区域空物体", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'EMPTY',
    )


def _zone_page_count(node):
    return max(1, (len(node.zone_objects) + ZONES_PER_PAGE - 1) // ZONES_PER_PAGE)


def _zone_page_state(node, requested_page=None):
    page_count = _zone_page_count(node)
    raw_page = getattr(node, "zone_page", 0) if requested_page is None else requested_page
    page = min(max(int(raw_page), 0), page_count - 1)
    return page, page_count


def _clamp_zone_page(node, requested_page=None):
    page, page_count = _zone_page_state(node, requested_page)
    node.zone_page = page
    return page, page_count


# ---------------------------------------------------------------------------
# 空物体管理 operators
# ---------------------------------------------------------------------------

class SSMT_OT_DragZoneAdd(bpy.types.Operator):
    bl_idname = "ssmt.drag_zone_add"
    bl_label = "添加拖拽区域空物体"
    bl_description = "在场景原点创建一个拖拽区域空物体并挂到节点列表"

    node_name: bpy.props.StringProperty()
    node_tree: bpy.props.StringProperty()

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or len(node.zone_objects) >= MAX_ZONES:
            self.report({'WARNING'}, f"节点不存在或区域已达上限 {MAX_ZONES}")
            return {'CANCELLED'}

        used_ids = {
            int(getattr(entry, "zone_id", -1))
            for entry in node.zone_objects
            if 0 <= int(getattr(entry, "zone_id", -1)) < MAX_ZONES
        }
        zone_id = next((candidate for candidate in range(MAX_ZONES) if candidate not in used_ids), -1)
        if zone_id < 0:
            self.report({'WARNING'}, f"没有可用区域 ID（0-{MAX_ZONES - 1}）")
            return {'CANCELLED'}

        empty = bpy.data.objects.new(f"SSMT_DragZone_{zone_id}", None)
        empty.empty_display_type = 'SPHERE'
        empty.empty_display_size = 0.25
        empty.ssmt_drag_zone.radius = 0.5
        context.scene.collection.objects.link(empty)

        item = node.zone_objects.add()
        item.zone_id = zone_id
        item.zone_object = empty
        _clamp_zone_page(node, (len(node.zone_objects) - 1) // ZONES_PER_PAGE)
        self.report({'INFO'}, f"已创建区域空物体 {empty.name}")
        return {'FINISHED'}


class SSMT_OT_DragZoneRemove(bpy.types.Operator):
    bl_idname = "ssmt.drag_zone_remove"
    bl_label = "移除选中区域"
    bl_description = (
        "从列表移除该区域。SSMT 创建的区域空物体（SSMT_DragZone_*）在不被其他拖拽节点引用时一并删除；"
        "用户手动指定的已有空物体仅移除引用，保留在场景中"
    )

    node_name: bpy.props.StringProperty()
    node_tree: bpy.props.StringProperty()
    index: bpy.props.IntProperty()

    @staticmethod
    def _zone_empty_in_use(obj, exclude_node):
        """检查空物体是否仍被其他拖拽交互节点引用。"""
        for tree in bpy.data.node_groups:
            for n in tree.nodes:
                if n.bl_idname != 'SSMTNode_PostProcess_DragInteraction' or n == exclude_node:
                    continue
                for it in n.zone_objects:
                    if it.zone_object == obj:
                        return True
        return False

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or not (0 <= self.index < len(node.zone_objects)):
            return {'CANCELLED'}
        item = node.zone_objects[self.index]
        obj = item.zone_object
        node.zone_objects.remove(self.index)
        _clamp_zone_page(node)
        # 只有 SSMT 创建的区域空物体、且不再被任何拖拽节点引用时才随引用一并删除；
        # 用户手动指定的已有空物体（可能复用于其他用途）只移除引用
        if obj is not None and obj.name.startswith("SSMT_DragZone") and not self._zone_empty_in_use(obj, node):
            name = obj.name
            bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'INFO'}, f"已移除引用并删除空物体 {name}")
        else:
            self.report({'INFO'}, "已从列表移除（空物体保留在场景中）")
        return {'FINISHED'}


class SSMT_OT_DragZonePage(bpy.types.Operator):
    bl_idname = "ssmt.drag_zone_page"
    bl_label = "切换拖拽区域页"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()
    node_tree: bpy.props.StringProperty()
    direction: bpy.props.IntProperty(default=0)

    def execute(self, context):
        tree = bpy.data.node_groups.get(self.node_tree)
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None:
            return {'CANCELLED'}
        current_page, _page_count = _zone_page_state(node)
        _clamp_zone_page(node, current_page + self.direction)
        return {'FINISHED'}


def _selected_mesh_objects(context):
    """当前选中的 MESH 物体（多选，按选中顺序；活动物体一并兜底）。

    ``context.selected_objects`` 在节点编辑器的算子执行期是完整场景选择，
    因此 Shift 多选后一次点按钮即可批量加入。活动物体通常已在其中，只在
    不在时追加，避免重复。
    """
    candidates = []
    for obj in list(getattr(context, "selected_objects", None) or ()):
        if obj is not None and obj not in candidates:
            candidates.append(obj)
    active = getattr(context, "active_object", None)
    if active is not None and active not in candidates:
        candidates.append(active)
    return [obj for obj in candidates if getattr(obj, "type", None) == 'MESH']


def _append_object_refs(collection, objects):
    """把物体批量追加进 PointerProperty 集合；已存在的跳过。

    返回 ``(added, skipped)``；新加入的项随即出现在 ``collection`` 里，所以
    同一次调用内的重复物体也会被后续迭代判定为已存在。
    """
    added = []
    skipped = []
    for obj in objects:
        if any(getattr(item, "object", None) == obj for item in collection):
            skipped.append(obj)
            continue
        item = collection.add()
        item.object = obj
        added.append(obj)
    return added, skipped


def _join_object_names(objects, limit=5):
    """报告用短名单：最多列 ``limit`` 个名字，其余折叠成计数。"""
    names = [str(getattr(obj, "name", "") or "") for obj in list(objects)[:limit]]
    text = "、".join(names)
    if len(objects) > limit:
        text += f" 等 {len(objects)} 个"
    return text


class SSMT_OT_DragZoneIncludeAdd(bpy.types.Operator):
    """把当前选中的 MESH 物体（支持多选/Shift 连选）加入权重球的包含列表。"""
    bl_idname = "ssmt.drag_zone_include_add"
    bl_label = "添加包含物体"
    bl_description = "把场景中选中的网格物体加入包含列表（Shift 多选可一次加入多个；已存在的自动跳过）"
    bl_options = {'INTERNAL'}

    empty_name: bpy.props.StringProperty()

    def execute(self, context):
        empty = bpy.data.objects.get(self.empty_name) if self.empty_name else None
        if empty is None or not hasattr(empty, "ssmt_drag_zone"):
            self.report({'WARNING'}, "权重球不存在")
            return {'CANCELLED'}
        targets = _selected_mesh_objects(context)
        if not targets:
            self.report({'WARNING'}, "请先选中网格物体（可按住 Shift 多选）")
            return {'CANCELLED'}
        added, skipped = _append_object_refs(empty.ssmt_drag_zone.include_objects, targets)
        if not added:
            self.report({'INFO'}, f"选中的 {len(skipped)} 个物体已在包含列表")
        elif skipped:
            self.report(
                {'INFO'},
                f"已加入 {len(added)} 个包含物体（{_join_object_names(added)}）；"
                f"{len(skipped)} 个已存在",
            )
        else:
            self.report({'INFO'}, f"已加入 {len(added)} 个包含物体（{_join_object_names(added)}）")
        return {'FINISHED'}


class SSMT_OT_DragZoneIncludeRemove(bpy.types.Operator):
    """从权重球包含列表移除一项。"""
    bl_idname = "ssmt.drag_zone_include_remove"
    bl_label = "移除包含物体"
    bl_options = {'INTERNAL'}

    empty_name: bpy.props.StringProperty()
    index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        empty = bpy.data.objects.get(self.empty_name) if self.empty_name else None
        if empty is None or not hasattr(empty, "ssmt_drag_zone"):
            self.report({'WARNING'}, "权重球不存在")
            return {'CANCELLED'}
        settings = empty.ssmt_drag_zone
        if 0 <= self.index < len(settings.include_objects):
            settings.include_objects.remove(self.index)
            self.report({'INFO'}, "已移除包含物体")
        return {'FINISHED'}


class SSMT_OT_DragZoneColliderAdd(bpy.types.Operator):
    """把当前选中的 MESH 物体（支持多选/Shift 连选）加入权重球的碰撞体列表。"""
    bl_idname = "ssmt.drag_zone_collider_add"
    bl_label = "添加碰撞体"
    bl_description = "把场景中选中的网格物体加入碰撞体列表（Shift 多选可一次加入多个；已存在的自动跳过）"
    bl_options = {'INTERNAL'}

    empty_name: bpy.props.StringProperty()

    def execute(self, context):
        empty = bpy.data.objects.get(self.empty_name) if self.empty_name else None
        if empty is None or not hasattr(empty, "ssmt_drag_zone"):
            self.report({'WARNING'}, "权重球不存在")
            return {'CANCELLED'}
        targets = _selected_mesh_objects(context)
        if not targets:
            self.report({'WARNING'}, "请先选中网格物体（可按住 Shift 多选）")
            return {'CANCELLED'}
        added, skipped = _append_object_refs(empty.ssmt_drag_zone.collider_objects, targets)
        if not added:
            self.report({'INFO'}, f"选中的 {len(skipped)} 个物体已在碰撞体列表")
        elif skipped:
            self.report(
                {'INFO'},
                f"已加入 {len(added)} 个碰撞体（{_join_object_names(added)}）；"
                f"{len(skipped)} 个已存在",
            )
        else:
            self.report({'INFO'}, f"已加入 {len(added)} 个碰撞体（{_join_object_names(added)}）")
        return {'FINISHED'}


class SSMT_OT_DragZoneColliderRemove(bpy.types.Operator):
    """从权重球碰撞体列表移除一项。"""
    bl_idname = "ssmt.drag_zone_collider_remove"
    bl_label = "移除碰撞体"
    bl_options = {'INTERNAL'}

    empty_name: bpy.props.StringProperty()
    index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        empty = bpy.data.objects.get(self.empty_name) if self.empty_name else None
        if empty is None or not hasattr(empty, "ssmt_drag_zone"):
            self.report({'WARNING'}, "权重球不存在")
            return {'CANCELLED'}
        settings = empty.ssmt_drag_zone
        if 0 <= self.index < len(settings.collider_objects):
            settings.collider_objects.remove(self.index)
            self.report({'INFO'}, "已移除碰撞体")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 主节点
# ---------------------------------------------------------------------------

class SSMTNode_PostProcess_DragInteraction(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_DragInteraction'
    bl_label = '拖拽交互'
    bl_description = (
        '把 LEWDHAND 的鼠标拖拽模型效果接入蓝图后处理链：Alt+左键抓取拖拽、弹簧回弹、戳，'
        '可与多文件/形态键叠加（多文件→形态键→拉扯三级接力）。'
        '可选当前帧整面碰撞，以及按衣物拓扑生成的实时贴身上下拖拽。'
    )

    # ---- 基本属性 ----
    hash_values: bpy.props.StringProperty(
        name="目标哈希值",
        description="目标 DrawIB hash 列表（= 组件列表），多个用逗号分隔。支持 IB hash 或完整名称",
        default="",
    )
    mod_namespace: bpy.props.StringProperty(
        name="命名空间后缀",
        description="变量/资源命名后缀；默认使用稳定命名空间 A",
        default=DEFAULT_MOD_NAMESPACE,
    )
    grab_key: bpy.props.EnumProperty(
        name="武装修饰键",
        description="抓取的武装修饰键",
        items=[('ALT', "Alt", "按住 Alt 才能抓取"), ('NONE', "无（常开）", "无需修饰键")],
        default='ALT',
    )
    grab_gesture: bpy.props.EnumProperty(
        name="抓取手势",
        description=(
            "触发抓取（isMouseButtonDown）的按键组合。"
            "原作设计：Alt+左右键同按或 Alt+X 才是抓取，单键按下释放是戳；"
            "选左键/右键时该键的戳手势不再可用（按下即抓取，无释放脉冲）"
        ),
        items=[
            ('LMB', "左键（或 X）", "Alt+左键按住即抓取（最直觉；左键戳失效，右键戳保留）"),
            ('RMB', "右键（或 X）", "Alt+右键按住即抓取（右键戳失效，左键戳保留）"),
            ('COMBO', "左右键同按 / X（原作）", "Alt+左右键同按或 Alt+X 抓取（原作手势，单键戳全部保留）"),
        ],
        default='LMB',
    )
    enable_poke: bpy.props.BoolProperty(name="启用戳", default=True)
    poke_gesture: bpy.props.EnumProperty(
        name="戳按键",
        description="选择释放时触发向内戳击的鼠标按键；与抓取手势使用同一按键时，抓取优先",
        items=[
            ('RMB', "右键", "右键点击释放后沿表面法线向内戳"),
            ('LMB', "左键", "左键点击释放后沿表面法线向内戳"),
            ('BOTH', "左右键", "左键或右键点击释放后均向内戳"),
        ],
        default='RMB',
    )
    drag_enabled_default: bpy.props.BoolProperty(
        name="旧版默认拖拽开关",
        default=True,
        options={'HIDDEN'},
    )
    drag_system_mode_default: bpy.props.IntProperty(
        name="默认运行模式",
        description="0=完全关闭，1=仅命中检测，2=命中检测并允许模型变形",
        default=2,
        min=0,
        max=2,
    )
    drag_mode_initialized: bpy.props.BoolProperty(
        name="运行模式已迁移",
        default=False,
        options={'HIDDEN'},
    )
    drag_mode_variable_name: bpy.props.StringProperty(
        name="运行模式变量",
        description="预分配运行模式变量；保留默认名时自动附加命名空间后缀，也可输入自定义全局变量名",
        default="ssmtdrag_drag_enabled",
    )
    mode_toggle_key: bpy.props.StringProperty(
        name="模式切换快捷键",
        description="按下循环切换运行模式：0=关闭 → 1=仅命中 → 2=命中+拖拽（type=cycle），始终生成切换键",
        default="f8",
    )
    ui_detected_variable_name: bpy.props.StringProperty(
        name="命中绘制 ID 变量",
        description="预分配鼠标当前命中绘制 ID 的只读联动变量；未命中时为 -1",
        default="ssmtdrag_ui_detected",
    )
    ui_zone_variable_name: bpy.props.StringProperty(
        name="命中区域 ID 变量",
        description="预分配鼠标当前命中稳定区域 ID 的只读联动变量；编号与节点区域 ID 完全一致",
        default="ssmtdrag_ui_zone",
    )
    # ---- 形态键驱动输出（旧开关，仅作旧工程兼容存储；ShapeKeyDrive 缓冲，纯 GPU 无回读）----
    # UI 已并入「功能开关」box 的 feature_shapekey_link（启用形态键联动，t11 合并），
    # 本属性不再单独渲染：旧工程显式 True 经 _feature_skd 兼容分支无条件生效，
    # 并在 _migrate_feature_defaults 中继承后退位（phase2/n1 §5.4 档一）。
    enable_shapekey_drive: bpy.props.BoolProperty(
        name="形态键驱动输出",
        description="旧版开关，已并入功能开关的「启用形态键联动」（仅命中模式下命中区域按住左键/X：按鼠标位移驱动方向强度，或按点击档位 0/1 开关，写入 ShapeKeyDrive 缓冲）；仅作旧工程兼容存储，界面不再单独显示",
        default=False,
    )

    # ---- 四功能开关（总闸：关闭则不生成对应段族 / 不拷贝对应 hlsl）----
    # 两段式迁移（phase2/n1 §5.4 档一）：旧工程 enable_shapekey_drive 显式开启的值
    # 经 _migrate_feature_defaults 继承进 feature_shapekey_link 后旧开关退位；
    # 新工程默认 ON = “能力声明”，发射仍受消费方约束（见 _feature_skd），
    # 新建空工程不产生垃圾段、既有已配置工程导出不变。
    feature_shapekey_link: bpy.props.BoolProperty(
        name="启用形态键联动",
        description="生成形态键驱动整族（ShapeKeyDrive/Dir/DragLatch/ClickCount/ActiveDir 资源 + rzm_shapekey_drive.hlsl + 每帧 dispatch），供形态键节点绑定读取",
        default=True,
        update=lambda self, context: self._migrate_feature_defaults(context),
    )
    feature_variable_link: bpy.props.BoolProperty(
        name="启用变量联动",
        description="生成变量↔驱动缓冲双向同步（rzm_shapekey_var_sync.hlsl + readback 命令表 + ClickExport 点击回读）；依赖形态键联动缓冲，形态键联动关闭时自动一并关闭",
        default=True,
    )
    feature_panel_link: bpy.props.BoolProperty(
        name="启用面板联动",
        description="生成 UI 桥（$ssmtdrag_ui_detected/$ssmtdrag_ui_zone 只读联动变量 + readback 命令表）；关闭后模型拖拽命中不再暴露给面板（默认 ON = 现状恒发）",
        default=True,
    )
    shapekey_drive_move_sensitivity: bpy.props.FloatProperty(
        name="位移灵敏度",
        description="鼠标位移控制：每像素位移对应的强度增量（向上增、向下减），默认 0.02；无方向形态键不随位移，按点击档位 0/1 开关",
        default=0.02,
        min=0.0001,
        max=1.0,
    )
    enable_hand_cursor: bpy.props.BoolProperty(
        name="启用手型光标",
        default=True,
        description="生成 S8 手型光标（按住 Alt 时显示，是 mode 已激活的视觉反馈）",
    )
    enable_viewport_probe: bpy.props.BoolProperty(
        name="启用视口探针",
        default=True,
        description="生成 viewport 探针系统（角色查看器等子区域渲染时校正光标映射，否则检测射线打偏、命中为零。原作必备）",
    )

    # ---- EFMI 分支专属（仅 GlobalConfig.logic_name == EFMI 时经
    #      node_postprocess_draginteraction_efmi.py 消费；zzmi 分支不使用）----
    efmi_probe_pass_hash: bpy.props.StringProperty(
        name="EFMI 主色 Pass Hash",
        description="【已废弃】门控改用 NumViews == 1（Zmd 构建不支持 ps hash 表达式门控）；保留属性以兼容已存工程，值不再生效——导出时如有旧值会告警清理，请清空",
        default="",
    )

    # ---- 全局物理档案（IniParams 70/71 字面量）----
    # 注：phys_release_kick / phys_target_follow 的键名是历史错位——
    # phys_release_kick 实际写 w71（原作“目标跟随”槽，默认 0.12），
    # phys_target_follow 实际写 y71（原作“释放冲击”槽，默认 1.10）。
    # 为不改旧工程数值与已生成 ini，仅按实际槽位语义改显示名。
    phys_grab_damping: bpy.props.FloatProperty(name="抓取阻尼", default=0.86)
    phys_grab_spring: bpy.props.FloatProperty(name="抓取弹簧", default=0.176)
    phys_release_damping: bpy.props.FloatProperty(name="释放阻尼", default=0.96)
    phys_release_spring: bpy.props.FloatProperty(name="释放弹簧", default=0.055)
    phys_release_kick: bpy.props.FloatProperty(name="目标跟随", default=0.12)
    phys_target_follow: bpy.props.FloatProperty(name="释放冲击", default=1.10)

    # ---- 全局倍率（IniParams 72）----
    mult_radius: bpy.props.FloatProperty(name="半径倍率", default=1.0)
    mult_strength: bpy.props.FloatProperty(name="强度倍率", default=0.333)
    mult_spring: bpy.props.FloatProperty(name="弹簧倍率", default=0.333)
    mult_damping: bpy.props.FloatProperty(name="阻尼倍率", default=1.0)

    # ---- 区域空物体引用列表 ----
    zone_objects: bpy.props.CollectionProperty(type=SSMT_DragZoneRef)
    zone_page: bpy.props.IntProperty(
        name="区域页", default=0, min=0, max=MAX_ZONES - 1,
    )

    # ---- 权重预览（视口热力图，仿高斯球预览）----
    preview_weights: bpy.props.BoolProperty(name="权重预览", default=False)
    preview_target: bpy.props.PointerProperty(
        name="预览网格", type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )
    preview_collection: bpy.props.PointerProperty(
        name="预览集合", type=bpy.types.Collection,
        description="递归预览集合及其子集合中的全部网格；设置后优先于单个预览网格",
    )

    # ---- 权重平台化（烘焙 + 预览共用）----
    mask_plateau: bpy.props.FloatProperty(
        name="权重平台化", default=0.0, min=0.0, max=0.99,
        description="0=纯高斯（画刷衰减 k 生效）；>0 时球内 d≤平台保持满权重、边缘平滑过渡（此模式下画刷衰减 k 不参与）",
    )

    # ---- 烘焙参考物体（可选手动覆盖）----
    bake_reference_object: bpy.props.PointerProperty(
        name="烘焙参考物体（可选）",
        description="空物体世界坐标换算到模组局部空间的参考；留空自动解析",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == 'MESH',
    )

    # ---- 碰撞检测（防穿模；仅模式三/拉扯模式生效）----
    collision_enabled: bpy.props.BoolProperty(
        name="启用碰撞检测",
        description="按当前帧身体三角形限制整张受控面片的位移，避免大半径同时拖到正反面时新增穿插；仅模式2生效，不修复模型原有交叉",
        default=False,
    )
    collision_margin: bpy.props.FloatProperty(
        name="碰撞裕量",
        description="整面接触保留的安全间距（模型坐标单位），用于避免边界抖动",
        default=0.002, min=0.0, max=0.05,
    )
    collision_mode: bpy.props.EnumProperty(
        name="碰撞模式",
        description="旧版逐点求解兼容设置；当前三角面求解统一使用整面位移比例约束",
        items=[
            ('SOFT', "软投影", "投影回全部邻近面（位移场连续，面不被折）"),
            ('HARD', "硬钳制", "沿位移路径整体停止（相邻顶点位移不连续，面易被折）"),
        ],
        default='SOFT',
    )
    collision_point_budget: bpy.props.IntProperty(
        name="点云预算",
        description="碰撞体点云抽稀后的最大点数（0=不抽稀，用全部碰撞体顶点）；降低可减少显存与查询成本",
        default=4096, min=0, max=65536,
    )
    collision_cell_size: bpy.props.FloatProperty(
        name="网格边长",
        description="均匀网格细格边长（vb0 局部单位）；0=按点云平均间距自适应",
        default=0.0, min=0.0, max=10.0,
    )
    collision_tri_count: bpy.props.IntProperty(
        name="约束三角形数",
        description="旧工程兼容值；当前启用碰撞时始终烘焙完整身体三角面，不再因0回退到逐点检测",
        default=8, min=0, max=32,
    )

    cloth_slide_enabled: bpy.props.BoolProperty(
        name="启用上下拖拽",
        description="独立生成实时贴身穿脱、褶皱与动态贴合权重；不依赖普通碰撞开关。需要ZZMI合并骨架、衣物与同载体身体碰撞体",
        default=False,
    )
    cloth_runtime_enabled: bpy.props.BoolProperty(name="运行时默认开启", default=False)
    cloth_toggle_key: bpy.props.StringProperty(name="穿脱切换键", default="ctrl alt U")
    cloth_reset_key: bpy.props.StringProperty(name="穿脱复位键", default="ctrl alt VK_BACK")
    cloth_collection: bpy.props.PointerProperty(name="穿脱衣物集合（可选）", type=bpy.types.Collection,
        description="留空沿用区域包含物体；指定后只把此集合内的网格生成穿脱，其他区域仍可普通拖拽")
    cloth_collider_collection: bpy.props.PointerProperty(name="身体碰撞集合（可选）", type=bpy.types.Collection,
        description="留空沿用区域碰撞体；独立于普通碰撞开关，用于衣物表面滑移")
    cloth_drag_sensitivity: bpy.props.FloatProperty(name="穿脱灵敏度", default=2.0, min=.1, max=5.0)
    cloth_stretch_compliance: bpy.props.FloatProperty(name="拉伸柔度（越小越硬）", default=.00003, min=.000001, max=.01, precision=6)
    cloth_bend_compliance: bpy.props.FloatProperty(name="弯曲柔度（越小越硬）", default=.0008, min=.000001, max=.1, precision=6)
    cloth_velocity_retention: bpy.props.FloatProperty(name="速度保留率（越小阻尼越强）", default=.85, min=0, max=.999)
    cloth_bend_min_ratio: bpy.props.FloatProperty(name="抗折叠支撑", default=.55, min=0, max=.8)
    cloth_fold_height: bpy.props.FloatProperty(name="褶皱高度", default=.012, min=0, max=.025)
    cloth_fold_spacing: bpy.props.FloatProperty(name="褶皱间距", default=.075, min=.04, max=.2)
    cloth_gather_strength: bpy.props.FloatProperty(name="有序收拢强度", default=.8, min=0, max=1)
    cloth_min_height_ratio: bpy.props.FloatProperty(name="最低袜口高度比例", default=.22, min=.1, max=.9)
    cloth_proxy_budget: bpy.props.IntProperty(name="物理代理点预算", default=2450, min=64, max=2450,
        description="超出预算时自动简化物理代理，渲染网格保留原顶点")
    cloth_up_axis: bpy.props.EnumProperty(name="衣物向上轴", default='AUTO',
        items=[('AUTO','自动','由衣物长轴估计')]+[(v,v,'导出模型坐标方向') for v in ('X','Y','Z','-X','-Y','-Z')])

    _RUNTIME_VARIABLE_DEFAULTS = {
        "drag_mode_variable_name": "ssmtdrag_drag_enabled",
        "ui_detected_variable_name": "ssmtdrag_ui_detected",
        "ui_zone_variable_name": "ssmtdrag_ui_zone",
    }

    # ------------------------------------------------------------------
    # 四功能开关统一取值（phase2/n1 §5.2/§5.4 档一：迁移 + 消费方约束）
    # ------------------------------------------------------------------

    def _migrate_feature_defaults(self, context=None):
        """两段式迁移（档一）：旧工程 enable_shapekey_drive 显式开启（True）时
        继承进 feature_shapekey_link，随后旧开关退位；幂等，仅经新开关
        feature_shapekey_link 的 update 触发一次。Fail-closed：异常仅跳过迁移，
        读取侧由 _feature_skd 的旧值兼容兜底，不影响导出。"""
        try:
            if getattr(self, "_feature_defaults_migrated", False):
                return
            if getattr(self, "enable_shapekey_drive", False):
                self.feature_shapekey_link = True
            self.enable_shapekey_drive = False
            self._feature_defaults_migrated = True
        except Exception:
            pass

    def _has_shapekey_consumer(self):
        """档一消费方约束：同树存在开启拖拽驱动的形态键节点，或点击计数导出
        绑定（ClickExport）。两者任一存在时 F1 发射有意义，否则不发射。"""
        tree = getattr(self, "id_data", None)
        if tree is not None:
            for node in getattr(tree, "nodes", None) or []:
                if (
                    getattr(node, "bl_idname", "") == "SSMTNode_PostProcess_ShapeKey"
                    and getattr(node, "drag_drive_enabled", False)
                ):
                    return True
        return bool(self._collect_click_export_drivers())

    def _feature_skd(self):
        """F1 形态键联动总开关。迁移期旧值显式开启 → 无条件 ON（旧工程输出不变）；
        新开关默认 ON 但受消费方约束（档一）——无消费方时视为未配置，不发射。"""
        if getattr(self, "enable_shapekey_drive", False):
            return True
        if not bool(getattr(self, "feature_shapekey_link", True)):
            return False
        return self._has_shapekey_consumer()

    def _feature_var(self):
        """F4 变量联动总开关。硬依赖 F1 缓冲族 → F1 关时强制一并降级（§6 约束）。"""
        if not self._feature_skd():
            return False
        return bool(getattr(self, "feature_variable_link", True))

    def _feature_panel(self):
        """F3 面板联动总开关。默认 ON = 现状恒发，无依赖。"""
        return bool(getattr(self, "feature_panel_link", True))

    def init(self, context):
        super().init(context)
        self.drag_system_mode_default = 2 if self.drag_enabled_default else 1
        self.drag_mode_initialized = True
        # EFMI 权重预览注册（t28：不依赖导出——节点创建即尝试注册，幂等；
        # 逻辑门控保证非 EFMI 环境零副作用；draw_buttons 另有一处自愈入口）。
        try:
            from ..common.global_config import GlobalConfig
            if str(getattr(GlobalConfig, "logic_name", "") or "") == LogicName.EFMI:
                from .node_postprocess_draginteraction_efmi import (
                    _ensure_efmi_preview_running,
                )
                _ensure_efmi_preview_running(self)
        except Exception:
            pass

    def _default_drag_system_mode(self):
        if not getattr(self, "drag_mode_initialized", False):
            mode = 2 if getattr(self, "drag_enabled_default", True) else 1
            try:
                self.drag_system_mode_default = mode
                self.drag_mode_initialized = True
            except Exception:
                pass
            return mode
        return min(max(int(getattr(self, "drag_system_mode_default", 2)), 0), 2)

    def _runtime_variable_name(self, property_name, ns):
        default_base = self._RUNTIME_VARIABLE_DEFAULTS[property_name]
        raw_name = normalize_variable_name(getattr(self, property_name, ""))
        resolved = f"{default_base}_{ns}" if not raw_name or raw_name == default_base else raw_name
        return f"${resolved}"

    def _runtime_variable_names(self, ns):
        return (
            self._runtime_variable_name("drag_mode_variable_name", ns),
            self._runtime_variable_name("ui_detected_variable_name", ns),
            self._runtime_variable_name("ui_zone_variable_name", ns),
        )

    # =======================================================================
    # UI
    # =======================================================================

    def draw_buttons(self, context, layout):
        try:
            from ..common.global_config import GlobalConfig
            current_logic = str(getattr(GlobalConfig, "logic_name", "") or "")
        except Exception:
            current_logic = ""
        is_efmi = current_logic == LogicName.EFMI
        cloth_supported = not current_logic or LogicName.is_zzmi_family(current_logic)

        if is_efmi:
            # ---- EFMI 分支专属（t25 全面 zzmi 化：参数完全跟随全局，仅提示）----
            efmi_box = layout.box()
            efmi_box.label(text="EFMI 分支（Endfield 256 区高斯球场，独立运行时）", icon='INFO')
            efmi_box.label(
                text="权重/物理完全跟随全局：区域空物体（高斯画刷参数）+ 全局物理档案与"
                "倍率；ps hash 见下方「目标哈希值」旁的附加输入",
                icon='INFO',
            )
            layout.label(
                text="碰撞检测/视口探针在 EFMI 模式不可用（本期未迁移，不生成对应段族）；"
                "手型光标已支持（独立实现）；权重预览已支持（zzmi 式语义）",
                icon='INFO',
            )

        layout.prop(self, "hash_values")
        if is_efmi:
            # ---- 门控定案（NumViews == 1）：颜色 pass NumViews=1 / 深度阴影
            #      NumViews=0 / 多 RT 2/5/8——跨 mod 通用、无状态维护；ps hash
            #      表达式门控废弃（Zmd 构建不可用）----
            layout.label(
                text="主色 pass 自动门控（NumViews == 1）：探针/检测只在绘制到主色 "
                "RT（视图数=1）的 pass 运行——深度/阴影 pass（NumViews=0）自动排除，"
                "无需手动填哈希",
                icon='INFO',
            )
            # t49 F3：旧属性迁移提示（efmi_probe_pass_hash 已废弃）
            layout.label(
                text="旧属性「主色 Pass Hash」（efmi_probe_pass_hash）已废弃："
                "如节点曾填写（如 1718.1）请清空——该值不再生效，导出时会有清理告警",
                icon='ERROR',
            )
        layout.prop(self, "mod_namespace")
        row = layout.row(align=True)
        row.prop(self, "grab_key")
        gesture_row = row.row(align=True)
        gesture_row.enabled = not is_efmi
        gesture_row.prop(self, "grab_gesture")
        row = layout.row(align=True)
        row.prop(self, "enable_poke")
        poke_row = row.row(align=True)
        poke_row.enabled = self.enable_poke and not is_efmi
        poke_row.prop(self, "poke_gesture")
        layout.prop(self, "enable_hand_cursor")

        # ---- 功能开关（总闸：关闭则不生成对应段族 / 不拷对应 hlsl）----
        feature_box = layout.box()
        feature_box.label(text="功能开关（关闭则不生成对应段族）", icon='PREFERENCES')
        feature_box.prop(self, "feature_shapekey_link", text="启用形态键联动")
        var_row = feature_box.row()
        var_row.enabled = self._feature_skd()
        var_row.prop(self, "feature_variable_link", text="启用变量联动")
        if not self._feature_skd():
            feature_box.label(text="变量联动依赖形态键联动缓冲，形态键联动关闭时自动一并关闭", icon='INFO')
        feature_box.prop(self, "feature_panel_link", text="启用面板联动")
        collision_row = feature_box.row()
        collision_row.enabled = not is_efmi
        collision_row.prop(self, "collision_enabled", text="启用碰撞检测")
        cloth_row = feature_box.row()
        cloth_row.enabled = cloth_supported or self._cloth_enabled()
        cloth_row.prop(self, "cloth_slide_enabled", text="启用上下拖拽")
        if not cloth_supported and self._cloth_enabled():
            feature_box.label(text="当前穿脱需要 ZZMI 合并骨架，请关闭此开关后导出", icon='ERROR')
        if self._cloth_enabled() and cloth_supported:
            cloth_box = feature_box.box()
            cloth_box.label(text="实时贴身穿脱（ZZMI 合并骨架）", icon='MOD_CLOTH')
            cloth_box.prop(self, "cloth_collection")
            cloth_box.prop(self, "cloth_collider_collection")
            cloth_box.label(text="集合留空时，区域包含物体=衣物，碰撞体=身体", icon='INFO')
            cloth_box.label(text="连通衣物同步，分开的袜子独立；脚部固定", icon='INFO')
            for prop in ('cloth_runtime_enabled','cloth_toggle_key','cloth_reset_key','cloth_drag_sensitivity',
                         'cloth_fold_height','cloth_fold_spacing','cloth_gather_strength','cloth_stretch_compliance',
                         'cloth_bend_compliance','cloth_velocity_retention','cloth_bend_min_ratio',
                         'cloth_min_height_ratio','cloth_proxy_budget','cloth_up_axis'):
                cloth_box.prop(self, prop)

        # ---- 形态键驱动输出（开关已并入上方「功能开关·启用形态键联动」，此 box 仅余输出配置）----
        drive_box = layout.box()
        drive_box.label(text="形态键驱动输出", icon='SHAPEKEY_DATA')
        if self._feature_skd():
            drive_box.prop(self, "shapekey_drive_move_sensitivity", text="位移灵敏度")
            drive_box.label(text="点击档位数：由各形态键节点配置的最大点击档位自动推导", icon='INFO')
            drive_box.label(text="仅命中模式：命中区域按住左键/X，方向形态键随鼠标位移驱动，无方向形态键按点击档位直接 0/1 开关", icon='INFO')
        else:
            drive_box.label(text="未生效：开启上方「启用形态键联动」且存在形态键消费方时可用", icon='INFO')

        runtime_box = layout.box()
        runtime_box.label(text="运行时联动变量", icon='DRIVER')
        row = runtime_box.row(align=True)
        row.prop(self, "drag_system_mode_default", text="默认模式")
        row.prop(self, "drag_mode_variable_name", text="")
        row = runtime_box.row(align=True)
        row.label(text="模式切换键")
        row.prop(self, "mode_toggle_key", text="")
        row = runtime_box.row(align=True)
        row.label(text="命中绘制 ID")
        row.prop(self, "ui_detected_variable_name", text="")
        row = runtime_box.row(align=True)
        row.label(text="命中区域 ID")
        row.prop(self, "ui_zone_variable_name", text="")

        box = layout.box()
        box.label(text="全局物理档案（弹簧常数）", icon='PHYSICS')
        col = box.column(align=True)
        col.prop(self, "phys_grab_damping")
        col.prop(self, "phys_grab_spring")
        col.prop(self, "phys_release_damping")
        col.prop(self, "phys_release_spring")
        col.prop(self, "phys_release_kick")
        col.prop(self, "phys_target_follow")

        box = layout.box()
        box.label(text="全局倍率", icon='MODIFIER')
        col = box.column(align=True)
        col.prop(self, "mult_radius")
        col.prop(self, "mult_strength")
        col.prop(self, "mult_spring")
        col.prop(self, "mult_damping")

        # 区域空物体列表
        box = layout.box()
        row = box.row()
        row.label(text=f"区域空物体（{len(self.zone_objects)}/{MAX_ZONES}）", icon='EMPTY_DATA')
        add = row.operator(SSMT_OT_DragZoneAdd.bl_idname, text="", icon='ADD')
        add.node_name = self.name
        add.node_tree = self.id_data.name if self.id_data else ""

        # draw 回调只读页码，避免在绘制中改 RNA 属性导致后续面板短暂消失。
        page, page_count = _zone_page_state(self)
        page_row = box.row(align=True)
        previous_row = page_row.row(align=True)
        previous_row.enabled = page > 0
        previous = previous_row.operator(SSMT_OT_DragZonePage.bl_idname, text="", icon='TRIA_LEFT')
        previous.node_name = self.name
        previous.node_tree = self.id_data.name if self.id_data else ""
        previous.direction = -1
        page_row.label(text=f"第 {page + 1} / {page_count} 页")
        next_row = page_row.row(align=True)
        next_row.enabled = page + 1 < page_count
        next_page = next_row.operator(SSMT_OT_DragZonePage.bl_idname, text="", icon='TRIA_RIGHT')
        next_page.node_name = self.name
        next_page.node_tree = self.id_data.name if self.id_data else ""
        next_page.direction = 1
        start = page * ZONES_PER_PAGE
        end = min(start + ZONES_PER_PAGE, len(self.zone_objects))
        for i in range(start, end):
            item = self.zone_objects[i]
            row = box.row(align=True)
            zone_id = int(getattr(item, "zone_id", i))
            row.prop(
                item, "expanded", text="", emboss=False,
                icon='DISCLOSURE_TRI_DOWN' if item.expanded else 'DISCLOSURE_TRI_RIGHT',
            )
            row.prop(item, "zone_object", text=f"区域 {zone_id}")
            rm = row.operator(SSMT_OT_DragZoneRemove.bl_idname, text="", icon='X')
            rm.node_name = self.name
            rm.node_tree = self.id_data.name if self.id_data else ""
            rm.index = i
            obj = item.zone_object
            if obj is not None and item.expanded:
                sub = box.column(align=True)
                sub.use_property_split = True
                sub.prop(obj.ssmt_drag_zone, "enabled")
                sub.prop(obj.ssmt_drag_zone, "propagate", text="沿表面扩散")
                sub.prop(obj.ssmt_drag_zone, "brush_strength")
                sub.prop(obj.ssmt_drag_zone, "brush_falloff_k")
                sub.prop(obj.ssmt_drag_zone, "radius")
                # 建议范围：衰减需在球内显著变化，否则整块刚体动（原版实测 ratio 0.3~2.2）
                ws = sum(obj.matrix_world.to_scale()) / 3.0
                if ws > 1e-6:
                    sub.label(text=f"球半径≈{ws:.3f}，建议影响半径 {ws:.3f}~{ws*2.5:.3f}", icon='INFO')
                sub.prop(obj.ssmt_drag_zone, "strength")
                sub.prop(obj.ssmt_drag_zone, "max_offset")
                sub.prop(obj.ssmt_drag_zone, "falloff")
                sub.prop(obj.ssmt_drag_zone, "damping")
                sub.prop(obj.ssmt_drag_zone, "grabbable")
                sub.prop(obj.ssmt_drag_zone, "drag_along_normal")
                # 包含物体列表（留空 = 全部物体）
                inc_box = sub.box()
                inc_box.label(text="包含物体（留空=全部）", icon='OBJECT_DATA')
                inc_row = inc_box.row(align=True)
                add_inc = inc_row.operator(SSMT_OT_DragZoneIncludeAdd.bl_idname, text="添加选中物体", icon='ADD')
                add_inc.empty_name = obj.name
                for inc_index, inc_item in enumerate(obj.ssmt_drag_zone.include_objects):
                    inc_item_row = inc_box.row(align=True)
                    inc_item_row.prop(inc_item, "object", text="")
                    rm_inc = inc_item_row.operator(SSMT_OT_DragZoneIncludeRemove.bl_idname, text="", icon='X')
                    rm_inc.empty_name = obj.name
                    rm_inc.index = inc_index
                # 碰撞体列表（留空 = 该区域不参与碰撞约束）
                col_box = sub.box()
                col_box.label(text="碰撞体（防穿模，留空=不约束）", icon='PHYSICS')
                col_box.prop(obj.ssmt_drag_zone, "collision_override", text="普通碰撞开关")
                col_row = col_box.row(align=True)
                add_col = col_row.operator(SSMT_OT_DragZoneColliderAdd.bl_idname, text="添加选中物体", icon='ADD')
                add_col.empty_name = obj.name
                for col_index, col_item in enumerate(obj.ssmt_drag_zone.collider_objects):
                    col_item_row = col_box.row(align=True)
                    col_item_row.prop(col_item, "object", text="")
                    rm_col = col_item_row.operator(SSMT_OT_DragZoneColliderRemove.bl_idname, text="", icon='X')
                    rm_col.empty_name = obj.name
                    rm_col.index = col_index
                box.separator()

        layout.prop(self, "bake_reference_object")
        layout.prop(self, "mask_plateau")

        # 碰撞检测（防穿模）
        col_box = layout.box()
        col_box.label(text="碰撞检测（防穿模，仅拉扯模式生效）", icon='PHYSICS')
        col_box.prop(self, "collision_enabled")
        if self.collision_enabled:
            col = col_box.column(align=True)
            col.prop(self, "collision_margin")
            col.label(text="当前帧身体三角面 → 整面位移约束", icon='INFO')
            col_box.label(text="在下方区域的「碰撞体」列表中指定被遮挡物体（如大腿）", icon='INFO')

        # 权重预览（视口热力图）
        box = layout.box()
        box.label(text="权重预览（视口热力图）", icon='RESTRICT_VIEW_OFF')
        col = box.column(align=True)
        col.prop(self, "preview_weights")
        col.prop(self, "preview_target")
        col.prop(self, "preview_collection")
        if is_efmi:
            # EFMI 权重预览（t25 恢复，t28 注册时机修复）：与 zzmi 同语义——
            # 选中区域空物体在预览网格上显示高斯球权重场（depsgraph evaluated
            # 网格，世界坐标点绘制）；注册不依赖导出（draw_buttons/init 即注册，
            # 幂等，见 _ensure_efmi_preview_running）。
            if self.preview_weights:
                preview_count = len(_preview_targets(self))
                if preview_count == 0:
                    col.label(text="请选择预览网格或包含网格的集合", icon='INFO')
                else:
                    col.label(
                        text=f"正在预览 {preview_count} 个网格（选中区域空物体显示权重场）",
                        icon='INFO',
                    )
            try:
                from .node_postprocess_draginteraction_efmi import (
                    _ensure_efmi_preview_running,
                )
                _ensure_efmi_preview_running(self)
            except Exception:
                pass
        else:
            if self.preview_weights:
                preview_count = len(_preview_targets(self))
                if preview_count == 0:
                    col.label(text="请选择预览网格或包含网格的集合", icon='INFO')
                else:
                    col.label(text=f"正在预览 {preview_count} 个网格", icon='INFO')
            _ensure_preview_running()

        if not NUMPY_AVAILABLE:
            layout.label(text="警告: 未安装 numpy，烘焙不可用", icon='ERROR')
        if not GB_CORE_AVAILABLE:
            layout.label(text="警告: gb_core 不可用，高斯烘焙回退 zone0 全 1", icon='ERROR')

    # =======================================================================
    # 顶点结构 / 着色器源
    # =======================================================================

    def _get_vertex_attrs_node(self):
        inputs = getattr(self, "inputs", None)
        if not inputs or len(inputs) == 0 or not inputs[0].is_linked:
            return None
        source_node = self.inputs[0].links[0].from_node
        if source_node.bl_idname == 'SSMTNode_PostProcess_VertexAttrs':
            return source_node
        if source_node.inputs and source_node.inputs[0].is_linked:
            prev_node = source_node.inputs[0].links[0].from_node
            if prev_node.bl_idname == 'SSMTNode_PostProcess_VertexAttrs':
                return prev_node
        return None

    def _get_vertex_struct_definition(self):
        node = self._get_vertex_attrs_node()
        if node:
            try:
                return node.get_vertex_struct_definition()
            except Exception:
                pass
        return DEFAULT_VERTEX_STRUCT

    def _get_vertex_attribute_layout(self, attr_name):
        """按顶点结构定义解析某个属性的 (stride, offset, type)。

        找不到该属性时 offset/type 为 None，stride 仍是结构总长。
        """
        type_sizes = {
            'float': 4, 'float2': 8, 'float3': 12, 'float4': 16,
            'int': 4, 'int2': 8, 'int3': 12, 'int4': 16,
            'uint': 4, 'uint2': 8, 'uint3': 12, 'uint4': 16,
            'half': 2, 'half2': 4, 'half3': 6, 'half4': 8,
            'double': 8, 'double2': 16, 'double3': 24, 'double4': 32,
        }
        target = str(attr_name).casefold()
        stride = 0
        offset = None
        attr_type = None
        for raw_line in self._get_vertex_struct_definition().splitlines():
            line = raw_line.strip().rstrip(';').strip()
            parts = line.split()
            if len(parts) < 2 or parts[0] not in type_sizes:
                continue
            if parts[1].casefold() == target:
                offset = stride
                attr_type = parts[0]
            stride += type_sizes[parts[0]]
        return stride, offset, attr_type

    def _get_position_layout(self):
        stride, position_offset, position_type = self._get_vertex_attribute_layout('position')
        if stride <= 0 or position_offset is None or position_type is None:
            return 40, 0, 'float3'
        return stride, position_offset, position_type

    def _get_toolset_dir(self):
        addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(addon_dir, "Toolset", "drag_interaction")

    # =======================================================================
    # ini 读写 / hash 解析
    # =======================================================================

    @classmethod
    def _remove_existing_drag_tail_block(cls, tail_content):
        remaining = tail_content
        while True:
            start = remaining.find(DRAG_TAIL_MARKER)
            if start == -1:
                return remaining

            end = len(remaining)
            search_from = start + len(DRAG_TAIL_MARKER)
            for marker in cls.AUTO_APPENDED_SECTION_MARKERS:
                marker_position = remaining.find(marker, search_from)
                if marker_position != -1:
                    end = min(end, marker_position)

            before = remaining[:start].rstrip()
            after = remaining[end:].lstrip()
            remaining = "\n\n".join(part for part in (before, after) if part)

    @staticmethod
    def _remove_existing_draw_hooks(sections):
        for section_name, lines in sections.items():
            cleaned_lines = []
            index = 0
            while index < len(lines):
                if "DRAG HOOK BEGIN" not in lines[index]:
                    cleaned_lines.append(lines[index])
                    index += 1
                    continue

                hook_end = next(
                    (candidate for candidate in range(index + 1, len(lines))
                     if "DRAG HOOK END" in lines[candidate]),
                    None,
                )
                if hook_end is None:
                    cleaned_lines.append(lines[index])
                    index += 1
                    continue
                index = hook_end + 1
            sections[section_name] = cleaned_lines

    @staticmethod
    def _remove_existing_legacy_jiggle_vb0_sections(sections):
        """清除历代 Jiggle vb0 资源段（重导出迁移）。

        历史命名有两种：`[ResourceDragJiggleTempVB0_*]`（copy 往返，当前形态）与
        `[ResourceDragJiggleShadowVB_*]`（R9 常驻影子缓冲，已回退）。读入旧 ini 时
        按两种前缀整体剔除，由发射侧按当前形态重建（资源段为 setdefault 语义，
        不剔除会让 ShadowVB 时代残留与新段并存）。
        新 jiggle 段（CustomShaderDragJiggle）内容由生成器无条件重发，无需在此清。
        """
        for section_name in [name for name in sections if name.startswith((
                "[ResourceDragJiggleTempVB0_", "[ResourceDragJiggleShadowVB_"))]:
            del sections[section_name]

    @staticmethod
    def _remove_existing_legacy_shapekey_sections(sections):
        """清除锁存（latch）引入前的形态键驱动族段（重导出迁移，评审 D1）。

        当前治理目标：旧格式 ini（锁存前版本生成）重导出后，新 hlsl（声明
        DragLatch u5）会与旧段错配——旧 drive/var_sync CS 段无
        `cs-u5 = ResourceDragShapeKeyDragLatch_*` 绑定（u5 未绑定，GetDimensions
        落空或加载报错）、旧 PinDetected 段无 `clear = ResourceDragShapeKeyDragLatch`
        （boot/失臂锁存清零缺失 → F1 假绑 zone0 / F2 复活回潮）。
        按内容特征剔除：命中旧形态（缺锁存标记）的段删除，由发射侧按当前形态
        无条件重发（见 _emit_shapekey_* 的 D1 注释）。已含锁存标记的新段保留
        （重发内容与之一致，剔除与否无差异，仅在发射侧统一覆盖）。"""
        for family in (
            "[CustomShaderDragShapeKeyDrive_",
            "[CustomShaderDragShapeKeyVarSync_",
        ):
            for section_name in [name for name in sections
                                 if name.startswith(family)]:
                joined = "\n".join(str(line) for line in sections[section_name])
                if "cs-u5 = ResourceDragShapeKeyDragLatch" not in joined:
                    del sections[section_name]
        for section_name in [name for name in sections
                             if name.startswith("[CommandListDragPinDetected_")]:
            joined = "\n".join(str(line) for line in sections[section_name])
            if "clear = ResourceDragShapeKeyDragLatch" not in joined:
                del sections[section_name]

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        preserved_tail_content = ""
        preserved_driver_content = ""
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            preserved_driver_content, content = self.split_anim_driver_block_content(content)
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)
            preserved_tail_content = self._remove_existing_drag_tail_block(preserved_tail_content)
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith('[') and stripped.endswith(']'):
                    current_section = stripped
                    sections[current_section] = []
                elif current_section is not None:
                    sections[current_section].append(line)
            self._remove_existing_draw_hooks(sections)
            self._remove_existing_legacy_jiggle_vb0_sections(sections)
            self._remove_existing_legacy_shapekey_sections(sections)
            try:
                from .drag_cloth import clean_sections, clean_tail
            except (ImportError, ModuleNotFoundError) as _cloth_missing:
                # 丝袜穿脱（drag_cloth）是**仓库的一部分**（随本仓库一同提交），正常情况永远不该缺。
                # 一旦缺，只可能是：clone 到半截 / 手动删了 / 打包部署按白名单拷漏了
                # / 从旧 commit 检出。这四种都该当场停下报清楚——不能让「仓库不完整」
                # 变成「导出成功但布料悄悄少一块」。
                # （若将来真有人只要拖拽不要布料，那是一个显式开关的事，不许靠文件缺失
                #   隐式实现。）
                raise RuntimeError(
                    "仓库不完整：丝袜穿脱（drag_cloth）模块缺失，它应随本仓库一同提交。"
                    "缺了它导出的模组会「丝袜拖不动 / 穿不脱」，所以这里直接中止，不猜着继续。"
                    "请先 git checkout / 重新拉取后再导出，不要在半截仓库上生成模组。"
                ) from _cloth_missing
            else:
                clean_sections(sections, self._resolve_namespace(ini_file_path))
                preserved_tail_content = clean_tail(preserved_tail_content)
        except FileNotFoundError:
            return None, "", ""
        return sections, preserved_tail_content, preserved_driver_content

    @staticmethod
    def _is_merged_skeleton_product(sections) -> bool:
        """产物 ini 是否带 ZZMI 合并骨架（MergedSkeleton）痕迹。

        判据只依赖产物内容（段名 + 行文本），因此在导出后处理阶段对任何来源的 ini 都成立。
        合并骨架确实存在时，下面两条拖拽诊断在合并骨架上**必然出现且良性**，应当降级为
        INFO（勿删：`3b1b73fe` 的 `vertex_base=3` 分支依赖这些兜底逻辑）。
        """
        if not sections:
            return False
        for section_name, lines in sections.items():
            if any(marker in str(section_name) for marker in MERGED_SKELETON_PRODUCT_MARKERS):
                return True
            for line in lines or ():
                if any(marker in str(line) for marker in MERGED_SKELETON_PRODUCT_MARKERS):
                    return True
        return False

    def _diagnostic_level(self, sections, *, merged_skeleton_suffix: str = "") -> tuple:
        """返回 (级别标签, 附加说明)：合并骨架上降级为 INFO，其余保持 WARNING。"""
        if self._is_merged_skeleton_product(sections):
            return "INFO", merged_skeleton_suffix
        return "WARNING", ""

    @staticmethod
    def _merged_skeleton_binding(sections, comp):
        """推导本组件的 (合并骨架组号, 出现次计数器 cid, 诊断)，推导不出的一律返回 None。

        组号与 cid 只从产物文本推导，两级来源：
        1. 本组件自己的 deform 段（段名以 [TextureOverride_VB_<draw_ib>_ 开头）：
           段内 $zz_ms_occ_<cid> 的自增给出 cid，段内引用的
           ResourceZZMergedSkeleton_G<n>_s<k> 给出本件所属组。同一产物可以有多个组，
           各组绝不共享骨架缓冲，所以只有本件自己那段能判定归属。
        2. 组号兜底：全产物的 [ResourceZZMergedSkeleton_G<n>_s<k>] 资源段名，仅当只
           存在唯一一个组号时才采用（本件那段的骨架引用可能已被别的 redirect 吸收）。

        任一级出现零个或多个候选 ⇒ 返回 None 并给出诊断；调用方整块不发射并告警，
        禁止退化成 G3 / cid=0 之类的默认值。
        """
        draw_ib = str(comp.get("hash") or "")
        occ_ids = set()
        groups = set()
        if draw_ib:
            prefix = f"[TextureOverride_VB_{draw_ib}_".casefold()
            for section_name, lines in sections.items():
                section_text = str(section_name)
                if not section_text.casefold().startswith(prefix):
                    continue
                if not section_text.endswith("]"):
                    continue
                for line in lines or ():
                    text = str(line)
                    occ_ids.update(
                        int(match.group(1))
                        for match in MERGED_SKELETON_OCC_RE.finditer(text)
                    )
                    groups.update(
                        int(match.group(1))
                        for match in MERGED_SKELETON_REF_RE.finditer(text)
                    )
        if not groups:
            groups = {
                int(match.group(1))
                for match in (
                    MERGED_SKELETON_SECTION_RE.match(str(name))
                    for name in sections
                )
                if match
            }
        if len(occ_ids) != 1:
            return None, None, (
                f"本件 deform 段 [TextureOverride_VB_{draw_ib}_*] 里找到的出现次计数器为 "
                f"{sorted(occ_ids) or '空'}，无法唯一确定 $zz_ms_occ_<cid>"
            )
        if len(groups) != 1:
            return None, None, (
                f"产物里的合并骨架组号为 {sorted(groups) or '空'}，无法唯一确定 "
                "ResourceZZMergedSkeleton_G<n>_s<k>"
            )
        return next(iter(groups)), next(iter(occ_ids)), ""

    @staticmethod
    def _strip_legacy_help_mode_block(text):
        """Remove every old help-gated drag-mode override from UI tails."""
        lines = str(text or "").splitlines(keepends=True)
        changed = True
        while changed:
            changed = False
            for index, line in enumerate(lines):
                stripped = line.strip()
                if stripped != "if $help == 1":
                    continue
                body_parts = "".join(lines[index:index + 4]).lower()
                if "$ssmtdrag_mode_" not in body_parts:
                    continue
                depth = 1
                end = None
                for j in range(index + 1, len(lines)):
                    current = lines[j].strip()
                    if current.startswith("if "):
                        depth += 1
                    elif current == "endif":
                        depth -= 1
                        if depth == 0:
                            end = j
                            break
                if end is None:
                    continue
                body = "".join(lines[index:end + 1])
                if "$ssmtdrag_mode_" not in body:
                    continue
                del lines[index:end + 1]
                if index < len(lines) and lines[index].strip() == "":
                    del lines[index]
                elif index > 0 and lines[index - 1].strip() == "":
                    del lines[index - 1]
                changed = True
                break
        return "".join(lines)

    def _normalize_ui_drag_references(self, tail_content, ns):
        """Align UI-generated model-drag variables with this node's actual namespace.

        The UI panel is kept as an auto-appended tail block, so it can be exported
        before the drag node suffix is known. Rewriting only the small block around
        the model-drag binding marker avoids touching other drag modules in the same
        tail while making the UI read the same globals that this node declares.
        """
        text = str(tail_content or "")
        marker = "; --- MODEL DRAG BINDING BEGIN ---"
        marker_index = text.find(marker)
        if marker_index < 0:
            return text

        text = self._strip_legacy_help_mode_block(text[:marker_index]) + text[marker_index:]
        marker_index = text.find(marker)
        end_marker = "; --- MODEL DRAG BINDING END ---"
        end_index = text.find(end_marker, marker_index)
        if end_index < 0:
            end_index = len(text)
        else:
            end_index += len(end_marker)

        start = text.rfind("\n", 0, marker_index) + 1
        block = text[start:end_index]
        _drag_mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
        replacements = [
            (r"\$ssmtdrag_ui_detected_([A-Za-z0-9_]+)", ui_detected_var),
            (r"\$ssmtdrag_ui_zone_([A-Za-z0-9_]+)", ui_zone_var),
        ]
        for pattern, replacement in replacements:
            block = re.sub(pattern, lambda _m: replacement, block)
        return text[:start] + block + text[end_index:]

    @staticmethod
    def _extract_drag_present_block(present_lines):
        start = next(
            (i for i, line in enumerate(present_lines) if "; --- DRAG PRESENT BEGIN ---" in line),
            None,
        )
        if start is None:
            return None
        end = next(
            (i for i in range(start + 1, len(present_lines))
             if "; --- DRAG PRESENT END ---" in present_lines[i]),
            None,
        )
        if end is None:
            return None
        block = present_lines[start:end + 1]
        del present_lines[start:end + 1]
        return block

    def _relocate_drag_present_into_ui_tail(self, sections, tail_content):
        """Put the drag Present block in the same [Present] as the UI binding.

        The UI panel is preserved as an auto-appended tail with its own
        [Present] section. Moving the generated drag block in front of the UI
        model-drag marker removes any dependence on duplicate [Present] order
        and makes the bridge variables available on the exact binding frame.
        """
        text = str(tail_content or "")
        if "; --- MODEL DRAG BINDING BEGIN ---" not in text:
            return text

        lines = text.splitlines(keepends=True)
        tail_block = None
        begin = next(
            (i for i, line in enumerate(lines) if "; --- DRAG PRESENT BEGIN ---" in line),
            None,
        )
        if begin is not None:
            end = next(
                (i for i in range(begin + 1, len(lines)) if "; --- DRAG PRESENT END ---" in lines[i]),
                None,
            )
            if end is not None:
                tail_block = lines[begin:end + 1]
                del lines[begin:end + 1]

        block = self._extract_drag_present_block(sections.get("[Present]", []))
        if block is None:
            block = tail_block
        if block is None:
            return "".join(lines)
        marker_idx = next(
            (i for i, line in enumerate(lines) if "; --- MODEL DRAG BINDING BEGIN ---" in line),
            None,
        )
        if marker_idx is None:
            return "".join(lines)
        block_text = "\n".join(block) + "\n"
        lines[marker_idx:marker_idx] = block_text.splitlines(keepends=True)
        return "".join(lines)

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content="", ns=None):
        if ns and self._feature_panel():
            # 面板联动关闭时不把拖拽 Present 块搬进 UI tail（避免消费方
            # 读到已消失的 $ssmtdrag_ui_* 全局）；UI tail 资产本身不改不删。
            preserved_tail_content = self._relocate_drag_present_into_ui_tail(sections, preserved_tail_content)
            preserved_tail_content = self._normalize_ui_drag_references(preserved_tail_content, ns)
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            if preserved_driver_content:
                f.write(preserved_driver_content)
                if not preserved_driver_content.endswith(chr(10)):
                    f.write(chr(10))
                f.write(chr(10))
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                normalized_lines = list(lines)
                while normalized_lines and not str(normalized_lines[-1]).strip():
                    normalized_lines.pop()
                for line in normalized_lines:
                    f.write(f"{line}\n")
                f.write("\n")
            if preserved_tail_content:
                f.write("\n")
                f.write(preserved_tail_content)

    def _parse_hash_values(self, hash_str):
        hash_list = [h.strip() for h in hash_str.split(',') if h.strip()]
        ib_hashes = OrderedDict()
        for hash_value in hash_list:
            prefix_info = ObjectPrefixHelper.extract_prefix_info(hash_value)
            if prefix_info:
                prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
                draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
                if draw_ib:
                    ib_hashes[draw_ib] = True
                    continue
            normalized = str(hash_value or "").strip()
            if normalized:
                ib_hashes[normalized] = True
        return list(ib_hashes.keys())

    def _hash_to_resource_prefix(self, h):
        return h.replace('-', '_')

    def _resolve_namespace(self, ini_file_path):
        if self.mod_namespace.strip():
            normalized = re.sub(r'\W+', '_', self.mod_namespace.strip())
            return normalized or DEFAULT_MOD_NAMESPACE
        # 点击计数导出、形态键后处理和拖拽后处理必须共享同一命名空间。
        # 旧配置可能保存了空值，因此空值也回退到稳定默认值，而不是按 INI
        # 文件名分别推导出不同后缀。
        return DEFAULT_MOD_NAMESPACE

    def _find_existing_base_resource_name(self, sections, hash_filter, base_name):
        normalized_base = str(base_name or "").strip()
        normalized_hash = str(hash_filter or "").strip()
        preferred = f"Resource_{self._hash_to_resource_prefix(normalized_base)}_Position"
        legacy = f"Resource{self._hash_to_resource_prefix(normalized_hash)}Position"
        return find_base_position_resource_name(
            sections, normalized_hash, base_name=normalized_base,
            preferred_names=[preferred, legacy], fallback_name=preferred,
        )

    def _get_vertex_count(self, sections, hash_value):
        for section_name, lines in sections.items():
            if section_name.startswith(f'[TextureOverride_{hash_value}_') and '_VertexLimitRaise' in section_name:
                for line in lines:
                    if line.strip().startswith('override_vertex_count ='):
                        try:
                            return int(line.split('=', 1)[1].strip())
                        except ValueError:
                            continue
        return None

    # =======================================================================
    # execute_postprocess 主流程
    # =======================================================================

    def execute_postprocess(self, mod_export_path):
        # ---- 游戏类型路由（集中入口）----
        # EFMI → 独立分支（node_postprocess_draginteraction_efmi.py，不共用着色器/函数）；
        # zzmi 家族（ZZMI/ZZMIDX12）→ 原分支（行为不变）；
        # 其他已加载游戏类型 → 跳过并明确警告；逻辑名未加载（旧工程/测试）→ 原分支兼容。
        try:
            from ..common.global_config import GlobalConfig
            current_logic = str(getattr(GlobalConfig, "logic_name", "") or "")
        except Exception:
            current_logic = ""
        if self._cloth_enabled() and current_logic and not LogicName.is_zzmi_family(current_logic):
            raise ValueError("上下拖拽目前需要 ZZMI 合并骨架，当前游戏不生成此运行时")
        if current_logic == LogicName.EFMI:
            from .node_postprocess_draginteraction_efmi import (
                DragInteractionEFMIExporter,
                _ensure_efmi_preview_running,
            )
            DragInteractionEFMIExporter(self).execute(mod_export_path)
            # t25：EFMI 权重预览（zzmi 式语义独立实现；幂等注册，选中区域
            # 空物体才显示）
            try:
                _ensure_efmi_preview_running(self)
            except Exception:
                pass
            return
        if current_logic and not LogicName.is_zzmi_family(current_logic):
            print(
                f"[DragInteraction][WARNING] 当前游戏类型 '{current_logic}' 暂不支持拖拽交互"
                "（仅支持 EFMI 与 ZZMI 家族），已跳过"
            )
            return
        print(f"[DragInteraction] 开始执行, 输出路径: {mod_export_path}")
        if self._cloth_enabled():
            self._cloth_object_names(required=True)
            if not self._collect_enabled_zone_entries():
                raise ValueError("上下拖拽：请先配置至少一个启用的拖拽区域")
        if not NUMPY_AVAILABLE:
            print("[DragInteraction][ERROR] 需要 numpy，已跳过")
            return

        hash_values = self._parse_hash_values(self.hash_values)
        if not hash_values:
            print("[DragInteraction] 未配置目标哈希值，已跳过")
            return

        ini_files = sorted(f for f in os.listdir(mod_export_path) if f.lower().endswith('.ini'))
        if not ini_files:
            print(f"[DragInteraction] 未找到 ini 文件: {mod_export_path}")
            return

        # 复制着色器（5 个核心 + 可选手部）
        res_dir = os.path.join(mod_export_path, "res", "drag_interaction")
        os.makedirs(res_dir, exist_ok=True)
        self._copy_shaders(res_dir)
        cloth_tokens = set()

        for ini_file in ini_files:
            ini_path = os.path.join(mod_export_path, ini_file)
            sections, preserved_tail, preserved_driver = self._read_ini_to_ordered_dict(ini_path)
            if not sections:
                continue

            # 定位含目标 hash 且能到达 drawindexed 的组件
            components = self._locate_components(sections, hash_values)
            if not components:
                continue

            ns = self._resolve_namespace(ini_path)
            self._create_cumulative_backup(ini_path, mod_export_path)

            # 1) 资源烘焙（稀疏区域权重/ObjectMap/ZoneParams/PathVectors）
            # 无有效区域的组件（包含列表未命中/影响球无交集）整体跳过，不注入钩子。
            self._write_zone_resources(mod_export_path, ns)
            active_components = []
            skipped_components = []
            for comp in components:
                comp['cloth_ini_token'] = os.path.splitext(ini_file)[0]
                if self._bake_component_resources(mod_export_path, sections, comp, ns):
                    active_components.append(comp)
                else:
                    skipped_components.append(comp)
            if skipped_components:
                print(
                    "[DragInteraction] 已跳过无有效区域的组件: "
                    + ", ".join(comp["comp_name"] for comp in skipped_components)
                )
            if not active_components:
                print(
                    f"[DragInteraction][WARNING] {ini_file} 的所有目标组件均无有效拖拽区域，"
                    "未做任何注入（着色器已复制，INI 保持不变）"
                )
                continue
            components = active_components
            cloth_tokens.update(comp['cloth']['token'] for comp in components if comp.get('cloth'))

            # 2) 生成 CustomShader/CommandList/Resource 段
            self._emit_sections(sections, components, ns)

            # 3) 注入绘制钩子（ib= 之后、第一个 run=/drawindexed= 之前）
            for comp in components:
                self._inject_draw_hooks(sections, comp, ns)
                if comp.get('cloth'):
                    from .drag_cloth import publish_before_draws
                    publish_before_draws(sections, comp)
            if any(comp.get('cloth') for comp in components):
                from .drag_cloth import publish_in_tail
                preserved_tail = publish_in_tail(preserved_tail, sections, components)

            # 4) Present 块 + Constants globals
            self._emit_present_and_constants(sections, components, ns)
            if self._cloth_enabled():
                from .drag_cloth import stabilize_globals
                stabilize_globals(sections, ns)

            # 5) 变形接力终态规整（幂等，含多文件/形态键条件锚定）
            deform_chain.finalize_deform_chain(sections)

            self._write_ordered_dict_to_ini(
                sections,
                ini_path,
                preserved_tail,
                preserved_driver,
                ns,
            )
            print(f"[DragInteraction] 已注入 {len(components)} 个组件到 {ini_file}")

        from .drag_cloth import prune_assets
        prune_assets(mod_export_path, self._resolve_namespace(None), cloth_tokens)
        if self._cloth_enabled() and not cloth_tokens:
            raise ValueError("上下拖拽：衣物没有匹配到有效拖拽组件，请检查目标哈希、区域包含物体和身体碰撞体")
        print("[DragInteraction] 完成")

    # =======================================================================
    # 组件定位（含目标 hash 且能到达 drawindexed）
    # =======================================================================

    def _locate_components(self, sections, hash_values):
        components = []
        object_offset = 0
        for hash_value in hash_values:
            parts = self._collect_draw_parts(sections, hash_value)
            if not parts:
                print(f"[DragInteraction][WARNING] hash {hash_value} 未找到绘制段，跳过")
                continue
            # 组件名以 Position 资源 stem 为准（去掉 part 后缀），与 ObjectMap/Masks 文件名一致；
            # 不要用 parts[0]["base_name"]（含 A/B part 后缀，会让 comp_name 带上 part 标签）。
            base_name = self._resolve_position_stem(sections, hash_value) or parts[0]["base_name"]
            vertex_count = self._get_vertex_count(sections, hash_value)
            base_resource = self._find_existing_base_resource_name(sections, hash_value, base_name)
            vertex_bases = {
                int(part.get("vertex_base", 0) or 0)
                for part in parts
            }
            if len(vertex_bases) > 1:
                # 合并骨架下同一 DrawIB 的多个 part 天然有不同 base vertex（实测 {0,3}：
                # 头部 stub 前缀 part 的 base=0 + 承载真实几何的 part 的 base=3）⇒ 必然
                # 出现且良性，降级 INFO；勿删，逐 part 的 vertex_base 兜底正是靠这条信息。
                _level, _suffix = self._diagnostic_level(
                    sections, merged_skeleton_suffix="（合并骨架场景下必然出现且良性）"
                )
                print(
                    f"[DragInteraction][{_level}] hash {hash_value} 的绘制段使用多个 "
                    f"base vertex {sorted(vertex_bases)}；最小值 {min(vertex_bases)} 仅供诊断，"
                    f"逐顶点资源与 z26 一律按承载真实几何的 part 基准 {max(vertex_bases)} 对齐"
                    f"{_suffix}"
                )
            # 掩码基准取**承载真实几何的 part** 的 base（= max）：ZZMI 合并重定向把 3 行
            # stub 前缀放在 carrier 头部（那一 part 的 base=0），Position.buf 的第 0 行落
            # 在运行时 VB0 的 base 行上。历史实现取 min()，于是 {0,3} 时逐顶点掩码整体前
            # 移 3 行——真实几何按 index+3 读到错位权重（叶瞬光01 的 999bff94 完全无命中即
            # 此因），且基座 Detect 段的 z26 与 P0 变体自相矛盾（0 vs 3）。
            vertex_base = max(vertex_bases) if vertex_bases else 0
            # 物体显隐：按记录稳定分配全局物体编号（mesh 注释名 → id；跨组件连续），
            # 供 flag 变量注入与 TriangleObjectIDs 烘焙共用同一编号空间。
            name_to_id = {}
            for part in parts:
                for record in part.get("draw_records") or []:
                    key = self._record_object_key(record)
                    if key not in name_to_id:
                        name_to_id[key] = object_offset
                        object_offset += 1
            comp = {
                "hash": hash_value,
                "base_name": base_name,
                "comp_name": self._comp_name(base_name),
                "vertex_count": vertex_count or 0,
                "vertex_base": vertex_base,
                "base_resource": base_resource,
                "parts": parts,
                "object_id_map": name_to_id,
                "object_count": len(name_to_id),
            }
            # 掩码基准与每个「绘制真实几何」的 part 的 base 必须一致；不一致就是上面那条
            # 错位链的复发，宁可 fail loudly 也不能静默产出错位产物。
            self._assert_component_base_alignment(comp)
            components.append(comp)
        return components

    @staticmethod
    def _part_is_prefix_only(part, mask_base):
        """该 part 是否只绘制运行时 VB0 的 stub 前缀（不含真实几何）。

        ZZMI 合并重定向把 3 行 stub 前缀放在 carrier 头部，对应 part 的 base 恒为 0、
        索引数也恰好只覆盖那几个前缀顶点；这类 part 的 z26 必须保留自身 base（0），
        让检测读到掩码前缀区（本来就恒为无效值）。
        """
        base = int(part.get("vertex_base", 0) or 0)
        mask_base = int(mask_base or 0)
        if base >= mask_base:
            # base == mask_base 即承载真实几何的基准；base > mask_base 绝不可能是前缀
            # （前缀只占 [0, mask_base)）。
            return False
        index_count = int(part.get("index_count", 0) or 0)
        # 必要上界：前缀区只有 mask_base 个顶点，每条三角形最多引用其中 3 个，索引数
        # 超过 3×mask_base 时必然引用到前缀之外的顶点 ⇒ 该 part 含真实几何。
        return index_count <= 3 * mask_base

    @classmethod
    def _part_vertex_base(cls, comp, part):
        """part 的检测基准 z26：承载真实几何的 part 恒等于掩码基准，前缀 stub 用自身 base。"""
        mask_base = int(comp.get("vertex_base", 0) or 0)
        if cls._part_is_prefix_only(part, mask_base):
            return int(part.get("vertex_base", 0) or 0)
        return mask_base

    @classmethod
    def _assert_component_base_alignment(cls, comp):
        """掩码基准必须与每个绘制真实几何的 part 的 base 一致（fail loudly）。

        前缀 stub part 允许 base < mask_base（它只画前缀顶点）；只要有任何 part 在
        前缀之外仍绘制几何、而其 base 不同于掩码基准，Position.buf 烘焙出的逐顶点
        权重就会对它整体错位——此时必须中止而不是产出「产品自相矛盾」的 ini。
        """
        mask_base = int(comp.get("vertex_base", 0) or 0)
        parts = comp.get("parts") or []
        offenders = [
            part for part in parts
            if not cls._part_is_prefix_only(part, mask_base)
            and int(part.get("vertex_base", 0) or 0) != mask_base
        ]
        if not offenders:
            return
        detail = ", ".join(
            "part{} base={} index_count={}".format(
                idx,
                int(part.get("vertex_base", 0) or 0),
                int(part.get("index_count", 0) or 0),
            )
            for idx, part in enumerate(parts)
        )
        raise ValueError(
            f"[DragInteraction] {comp.get('comp_name')} 的绘制段 base vertex 与掩码基准 "
            f"{mask_base} 不一致，逐顶点掩码/权重将整体错位（{detail}）；"
            "同一条 Position.buf 只能按唯一基准烘焙，请检查该 IB 的 part 划分"
        )

    @staticmethod
    def _record_object_key(record):
        """记录对应的物体稳定键：优先 mesh 注释名，无注释回退 section+偏移。"""
        comment = record.get("hook_anchor_comment") or ""
        match = MESH_COMMENT_RE.match(str(comment))
        if match:
            return match.group("object_name")
        return "{}#{}".format(record.get("section"), record.get("draw_offset"))

    @staticmethod
    def _global_object_oids(components):
        """跨组件全局物体编号的有序并集（object_id_map 取值）。

        oid 由 _locate_components 的 object_offset 跨组件累计分配，所有按 oid
        索引的发射点（flag 声明 / post 清零）都必须使用该并集，禁止按组件内
        range 重数——否则第二个组件起的编号整体错位。
        """
        oids = set()
        for comp in components or []:
            oids.update((comp.get("object_id_map") or {}).values())
        return sorted(oids)

    def _resolve_position_stem(self, sections, hash_value):
        """从 Position 资源段的 filename 推导组件 stem（如 abc123-43191）。"""
        res = self._find_existing_base_resource_name(sections, hash_value, hash_value)
        for line in sections.get(f"[{res}]", []):
            if line.strip().startswith("filename ="):
                fname = line.split("=", 1)[1].strip().replace('\\', '/').split('/')[-1]
                # 去掉 "-Position.buf" / ".buf" 后缀得 stem
                stem = re.sub(r"-Position.*$", "", fname)
                stem = re.sub(r"\.buf$", "", stem)
                if stem:
                    return stem
        return None

    def _comp_name(self, base_name):
        # 从组件 stem（如 abc123-43191）推导资源命名前缀（连字符转下划线）
        return self._hash_to_resource_prefix(base_name)

    @classmethod
    def _collect_referenced_draw_ranges(cls, sections, command_name, visited=None):
        normalized_name = str(command_name or "").strip().casefold()
        if not normalized_name:
            return []

        visited = set() if visited is None else visited
        if normalized_name in visited:
            return []
        visited.add(normalized_name)

        target_section = None
        expected_section = f"[{normalized_name}]"
        for section_name, section_lines in sections.items():
            if section_name.casefold() == expected_section:
                target_section = section_lines
                break
        if target_section is None:
            return []

        draw_ranges = []
        for line in target_section:
            stripped = line.strip()
            normalized = stripped.casefold()
            if normalized.startswith("drawindexed ="):
                nums = [value.strip() for value in stripped.split("=", 1)[1].split(',')]
                try:
                    draw_count = int(nums[0])
                    draw_offset = int(nums[1]) if len(nums) > 1 else 0
                    vertex_base = int(nums[2]) if len(nums) > 2 else 0
                except (ValueError, IndexError):
                    continue
                if draw_count > 0 and draw_offset >= 0:
                    draw_ranges.append((draw_offset, draw_count, vertex_base))
            elif normalized.startswith("run ="):
                nested_name = stripped.split("=", 1)[1].strip()
                draw_ranges.extend(cls._collect_referenced_draw_ranges(
                    sections, nested_name, visited,
                ))
        return draw_ranges

    def _collect_draw_parts(self, sections, hash_value):
        """按目标 hash 和活动 IB 收集绘制组，并把同一 IB 的 copy 段合并为一个 part。

        材质 copy 段会重复声明同一个 IB；按 IB 归并既保留完整 index_count，又避免每个
        copy 段各注入一个重复 hook。
        """
        parts = []
        normalized_hash = hash_value.casefold()
        all_records = []
        for section_order, (section_name, lines) in enumerate(sections.items()):
            if not (section_name.startswith("[TextureOverride") and section_name.endswith("]")):
                continue

            section_hash = None
            match_first_index = None
            for line in lines:
                s = line.strip()
                normalized = s.casefold()
                if normalized.startswith("hash ="):
                    section_hash = s.split("=", 1)[1].strip()
                elif normalized.startswith("match_first_index ="):
                    try:
                        match_first_index = int(s.split("=", 1)[1].strip())
                    except ValueError:
                        pass

            active_ib_resource = None
            pending_mesh_owner = None
            pending_mesh_first_index = None
            pending_mesh_comment = None
            pending_mesh_comment_occurrence = 0
            comment_occurrences = {}
            draw_ordinal = 0
            draw_records = []
            pending_ttl_1 = None
            pending_ttl_2 = None
            pending_ttl_3 = None

            for line_idx, line in enumerate(lines):
                s = line.strip()
                taken_draw = TTL_TAKEN_DRAW_RE.match(s)
                if taken_draw:
                    s = taken_draw.group(1)
                normalized = s.casefold()
                ttl_arg1 = TTL_ARG1_RE.match(s)
                if ttl_arg1:
                    pending_ttl_1 = int(ttl_arg1.group(1))
                    continue
                ttl_arg2 = TTL_ARG2_RE.match(s)
                if ttl_arg2:
                    pending_ttl_2 = int(ttl_arg2.group(1))
                    continue
                ttl_arg3 = TTL_ARG3_RE.match(s)
                if ttl_arg3:
                    pending_ttl_3 = int(ttl_arg3.group(1))
                    continue
                if normalized.startswith("ib ="):
                    val = s.split("=", 1)[1].strip()
                    active_ib_resource = None if val.casefold() == "null" else val
                    continue

                mesh_match = MESH_COMMENT_RE.match(s)
                if mesh_match:
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = s
                    pending_mesh_comment_occurrence = comment_occurrences.get(s, 0)
                    comment_occurrences[s] = pending_mesh_comment_occurrence + 1
                    prefix_info = ObjectPrefixHelper.extract_prefix_info(mesh_match.group("object_name"))
                    if prefix_info:
                        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
                        pending_mesh_owner = str(prefix_parts.get("draw_ib", "") or "").strip() or None
                        # `int()` 只走 `coerce_index`：注释里的对象名可能带着 Blender 去重后缀
                        # （`…-10524-0.001`，复制物体或重跑导出时自动追加），裸 int() 会抛
                        # ValueError 把整段解析打断；去重后缀剥掉后仍是同一个 first_index。
                        try:
                            pending_mesh_first_index = coerce_index(
                                prefix_parts.get("first_index", ""),
                                field="first_index",
                                context=mesh_match.group("object_name"),
                            )
                        except (TypeError, ValueError):
                            pending_mesh_first_index = None
                    continue

                if TTL_DRAW_RUN_RE.match(s) and pending_mesh_comment:
                    # TTL 二次绘制站点：范围来自 $\TTL\_1(index count)/$\TTL\_2(first index)，
                    # 而非 drawindexed。跨命名空间的 CommandList\TTL\Draw 无法被
                    # _collect_referenced_draw_ranges 解析，这里显式补记录。
                    draw_owner = pending_mesh_owner or section_hash
                    if (
                        draw_owner
                        and draw_owner.casefold() == normalized_hash
                        and active_ib_resource
                        and pending_ttl_1 is not None
                        and pending_ttl_2 is not None
                        and pending_ttl_1 > 0
                        and pending_ttl_2 >= 0
                    ):
                        draw_records.append({
                            "ordinal": draw_ordinal,
                            "ib_resource": active_ib_resource,
                            "draw_offset": pending_ttl_2,
                            "draw_count": pending_ttl_1,
                            "vertex_base": pending_ttl_3 or 0,
                            "mesh_first_index": pending_mesh_first_index,
                            "hook_anchor_comment": pending_mesh_comment,
                            "hook_anchor_occurrence": pending_mesh_comment_occurrence,
                            "line_index": line_idx,
                        })
                        draw_ordinal += 1
                    pending_ttl_1 = None
                    pending_ttl_2 = None
                    pending_ttl_3 = None
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = None
                    pending_mesh_comment_occurrence = 0
                    continue

                if normalized.startswith("run =") and pending_mesh_comment:
                    command_name = s.split("=", 1)[1].strip()
                    referenced_ranges = self._collect_referenced_draw_ranges(sections, command_name)
                    if referenced_ranges:
                        draw_owner = pending_mesh_owner or section_hash
                        for draw_offset, draw_count, vertex_base in referenced_ranges:
                            if draw_owner and draw_owner.casefold() == normalized_hash and active_ib_resource:
                                draw_records.append({
                                    "ordinal": draw_ordinal,
                                    "ib_resource": active_ib_resource,
                                    "draw_offset": draw_offset,
                                    "draw_count": draw_count,
                                    "vertex_base": vertex_base,
                                    "mesh_first_index": pending_mesh_first_index,
                                    "hook_anchor_comment": pending_mesh_comment,
                                    "hook_anchor_occurrence": pending_mesh_comment_occurrence,
                                    "line_index": line_idx,
                                })
                            draw_ordinal += 1
                        pending_mesh_owner = None
                        pending_mesh_first_index = None
                        pending_mesh_comment = None
                        pending_mesh_comment_occurrence = 0
                        continue

                if normalized.startswith("drawindexed ="):
                    nums = [n.strip() for n in s.split("=", 1)[1].split(',')]
                    try:
                        draw_count = int(nums[0])
                        draw_offset = int(nums[1]) if len(nums) > 1 else 0
                        vertex_base = int(nums[2]) if len(nums) > 2 else 0
                    except (ValueError, IndexError):
                        draw_ordinal += 1
                        continue

                    draw_owner = pending_mesh_owner or section_hash
                    if (
                        active_ib_resource
                        and draw_owner
                        and draw_owner.casefold() == normalized_hash
                        and draw_count > 0
                        and draw_offset >= 0
                    ):
                        draw_records.append({
                            "ordinal": draw_ordinal,
                            "ib_resource": active_ib_resource,
                            "draw_offset": draw_offset,
                            "draw_count": draw_count,
                            "vertex_base": vertex_base,
                            "mesh_first_index": pending_mesh_first_index,
                            "hook_anchor_comment": pending_mesh_comment,
                            "hook_anchor_occurrence": pending_mesh_comment_occurrence,
                            "line_index": line_idx,
                        })
                    draw_ordinal += 1
                    pending_mesh_owner = None
                    pending_mesh_first_index = None
                    pending_mesh_comment = None
                    pending_mesh_comment_occurrence = 0

            base_name = (
                section_name[len("[TextureOverride_"):-1]
                if section_name.startswith("[TextureOverride_")
                else section_name[1:-1]
            )
            for record in draw_records:
                record["section"] = section_name
                record["section_order"] = section_order
                record["match_first_index"] = match_first_index
                record["base_name"] = base_name
                all_records.append(record)

        def _record_sort_key(record):
            is_base = 0 if record["section"].startswith("[TextureOverride_") else 1
            return (record["draw_offset"], is_base, record["section_order"])

        records_by_ib = {}
        for record in all_records:
            records_by_ib.setdefault(record["ib_resource"], []).append(record)

        for ib_resource, records in records_by_ib.items():
            ib_first_index = min(record["draw_offset"] for record in records)
            ib_index_end = max(record["draw_offset"] + record["draw_count"] for record in records)
            index_count = ib_index_end - ib_first_index

            # 保留同一 IB 内每个 draw 的索引区间与 mesh 注释名，供烘焙侧按物体过滤。
            # 同一区间因材质/换装条件重复出现时名字相同；若偶发重叠，base 段优先。
            name_ranges = []
            for record in sorted(records, key=_record_sort_key):
                comment = record.get("hook_anchor_comment") or ""
                mesh_match = MESH_COMMENT_RE.match(comment)
                if not mesh_match:
                    continue
                object_name = mesh_match.group("object_name")
                if object_name:
                    name_ranges.append((record["draw_offset"], record["draw_count"], object_name))

            representative = min(records, key=_record_sort_key)
            mesh_first_index = representative["mesh_first_index"]
            if mesh_first_index is None:
                for record in sorted(records, key=_record_sort_key):
                    if record["mesh_first_index"] is not None:
                        mesh_first_index = record["mesh_first_index"]
                        break

            match_first_index = representative["match_first_index"]
            first_index = (
                mesh_first_index
                if mesh_first_index is not None
                else match_first_index if match_first_index is not None
                else ib_first_index
            )

            parts.append({
                "section": representative["section"],
                "ib_resource": ib_resource,
                "index_count": index_count,
                "ib_first_index": ib_first_index,
                "first_index": first_index,
                "base_name": representative["base_name"],
                "hook_anchor_comment": representative["hook_anchor_comment"],
                "hook_anchor_occurrence": representative["hook_anchor_occurrence"],
                "name_ranges": name_ranges,
                "draw_records": records,
                "vertex_base": int(representative.get("vertex_base", 0) or 0),
            })
        return parts

    # =======================================================================
    # 着色器复制 + struct 1 处机械替换
    # =======================================================================

    def _copy_shaders(self, res_dir):
        """把所需着色器准备到 ``res_dir``。

        安全合并：内容与目标相同的文件**不重写**，从而不改动它的 mtime。
        3DMigoto 的自定义着色器编译缓存按 ``.hlsl`` 的 mtime 与同名
        ``<stage>_5_0.<flags>.bin`` 配对（``CompareFileTime`` 必须精确相等），
        无条件重写会让整族缓存失效、下次进游戏/按 F10 全量重编译
        （实测单次两分钟量级）。详见 ``common/safe_write.py``。
        """
        toolset = self._get_toolset_dir()
        files = list(SHADER_FILES)
        if self._collision_enabled_for_component():
            files += list(SHADER_COLLISION_FILES)
        if self._feature_skd():
            files += list(SHADER_DRIVE_FILES)
        if self._feature_var() and self._drag_drive_var_sync_bindings():
            # 只有真会发射同步段时才拷贝：否则 res/ 里留下一个没有 ini 引用、
            # 也没有编译产物的孤儿 hlsl（旧版曾在「F4 开但一个形态键都没绑区域」
            # 的工程里留下 rzm_shapekey_var_sync.hlsl 孤儿）。
            files += list(SHADER_VARSYNC_FILES)
        vertex_struct = self._get_vertex_struct_definition()
        rewritten = 0
        for fname in files:
            src = _require_toolset_file(os.path.join(toolset, fname))
            with open(src, 'r', encoding='utf-8') as f:
                content = f.read()
            # 仅 1 处机械替换：struct VertexAttributes {…} → 顶点属性定义节点的 struct
            content = re.sub(
                r"struct VertexAttributes\s*\{[^}]*\};",
                vertex_struct,
                content,
                flags=re.DOTALL,
            )
            # t33 爆炸守卫：生成器侧同源注入（幂等；已在 Toolset 侧打过补丁时为 no-op）
            if fname == "rzm_jiggle_interaction.hlsl":
                content = _inject_explosion_guard(content)
            dest = os.path.join(res_dir, fname)
            # 内容没变就不写：保持 .hlsl 的 mtime，3DMigoto 的编译缓存才继续命中
            if write_text_if_changed(dest, content):
                rewritten += 1
        # 手部着色器 + 网格/法线资产：全部字节级原样合并。手部着色器读自己
        # 的 vb0（stride 28 固定布局），不含 struct VertexAttributes，无需也
        # 不许走文本替换路径（避免行尾转换）——与原作保持字节一致。
        if self.enable_hand_cursor:
            for fname in HAND_SHADER_FILES + HAND_ASSET_FILES:
                src = _require_toolset_file(os.path.join(toolset, fname))
                if copy_file_if_changed(src, os.path.join(res_dir, fname)):
                    rewritten += 1
        # 视口探针着色器：字节级原样合并（不读角色网格，无 struct VertexAttributes）
        if self.enable_viewport_probe:
            for fname in VIEWPORT_SHADER_FILES:
                src = _require_toolset_file(os.path.join(toolset, fname))
                if copy_file_if_changed(src, os.path.join(res_dir, fname)):
                    rewritten += 1
        if rewritten:
            print(f"[DragInteraction] 着色器已更新 {rewritten} 个")

    # =======================================================================
    # 资源烘焙（稀疏区域权重 / ObjectMap / ZoneParams / PathVectors）
    # =======================================================================

    def _bake_component_resources(self, mod_export_path, sections, comp, ns):
        # 每顶点 Top-K 区域权重：高斯场烘焙。所有配置区域对本组件均无有效权重时
        # （包含物体列表未命中 / 影响球与顶点无交集）跳过该组件——不生成
        # ObjectMap/逐三角编号/掩码，调用方也不再为它注入任何拖拽钩子。
        if not self._write_jiggle_masks(mod_export_path, sections, comp, ns):
            return False
        # ObjectMap：游戏索引空间 (1+N)×16B
        self._write_object_map(mod_export_path, sections, comp)
        # 逐三角形物体编号（显隐过滤：几何映射，与权重烘焙同类）
        self._write_triangle_object_ids(mod_export_path, sections, comp)
        # 碰撞检测资源（仅模式三/拉扯模式；碰撞体未命中仅警告，不阻断拖拽导出）
        if self._collision_enabled_for_component():
            self._write_collision_resources(mod_export_path, sections, comp, ns)
        if self._cloth_enabled():
            from .drag_cloth import bake_component
            cloth_names, body_names = self._cloth_object_names()
            comp['cloth'] = bake_component(self, mod_export_path, sections, comp, ns,
                cloth_names, body_names, _blender_object_key)
        return True

    def _write_triangle_object_ids(self, mod_export_path, sections, comp):
        """逐 part 读 IB 文件，按 name_ranges 给每个三角形写入物体编号
        （0xFFFFFFFF = 未映射，不受显隐门控）。文件名 {stem}TriangleObjectIDsP{p}.buf，
        与检测着色器的 indexBase/3 三角形序一致。"""
        name_to_id = comp.get("object_id_map") or {}
        for p_idx, part in enumerate(comp["parts"]):
            res = part.get("ib_resource")
            lines = sections.get(f"[{res}]", [])
            rel = None
            for line in lines:
                if line.strip().startswith("filename ="):
                    rel = line.split("=", 1)[1].strip()
                    break
            if not rel:
                continue
            full = os.path.join(mod_export_path, rel.replace("\\", os.sep).replace("/", os.sep))
            if not os.path.exists(full):
                continue
            try:
                idx = np.fromfile(full, dtype=np.uint32)
                n_tri = len(idx) // 3
                ids = np.full(n_tri, 0xFFFFFFFF, dtype=np.uint32)
                for offset, count, name in part.get("name_ranges", []):
                    oid = name_to_id.get(name)
                    if oid is None:
                        continue
                    start = max(0, min(int(offset) // 3, n_tri))
                    end = max(0, min((int(offset) + int(count)) // 3, n_tri))
                    if start >= end:
                        continue
                    fill = np.zeros(n_tri, dtype=bool)
                    fill[start:end] = True
                    fill &= ids == 0xFFFFFFFF
                    ids[fill] = oid
                out = os.path.join(
                    mod_export_path, self._buffer_dir(sections, comp),
                    f"{comp['base_name']}TriangleObjectIDsP{p_idx}.buf",
                )
                os.makedirs(os.path.dirname(out), exist_ok=True)
                ids.tofile(out)
            except Exception:
                continue


    def _write_zone_resources(self, mod_export_path, ns):
        """写每区域物理参数和路径向量；按稳定 zone_id 直接索引。"""
        entries = self._collect_enabled_zone_entries()
        capacity = self._zone_capacity(entries)
        params = np.zeros((capacity * 2, 4), dtype=np.float32)
        paths = np.zeros((capacity, 4), dtype=np.float32)

        if not entries:
            # 与旧版“无区域时 zone0 全模型”回退一致。
            params[1] = (0.0, 1.0, 0.0, 1.0)
        for zone_id, empty in entries:
            settings = empty.ssmt_drag_zone
            params[zone_id * 2 + 0] = (
                float(settings.radius),
                float(settings.strength),
                float(settings.max_offset),
                float(settings.falloff),
            )
            params[zone_id * 2 + 1] = (
                float(settings.damping),
                1.0 if settings.grabbable else 0.0,
                0.0,  # path mode 尚未进入节点 UI，保留 ABI 槽位
                # 【用户 m01161】「法线方向拖拽」开关：1 = 拖拽轴锁定为点中面的法线，
                # 鼠标位移只决定沿法线的推进量（强度）。这个分量此前恒 1.0 且着色器
                # 从未读取，所以直接复用，不改变 ABI 形状。
                1.0 if getattr(settings, "drag_along_normal", False) else 0.0,
            )

        res_dir = os.path.join(mod_export_path, "res", "drag_interaction")
        os.makedirs(res_dir, exist_ok=True)
        params.tofile(os.path.join(res_dir, f"ZoneParams_{ns}.buf"))
        paths.tofile(os.path.join(res_dir, f"PathVectors_{ns}.buf"))
        if self._feature_skd():
            _total_slots, _zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
            stage_counts = np.array(_zone_stage_counts, dtype=np.uint32)
            stage_counts.tofile(os.path.join(res_dir, f"ZoneStageCounts_{ns}.buf"))
            sync_bindings = self._drag_drive_var_sync_bindings()
            if self._feature_var() and sync_bindings:
                # 变量同步映射表：每绑定 4×uint32 = (驱动槽位, 区域 ID, 无方向档位, 保留)；
                # 方向形态键无档位概念，nd_stage 以 0xFFFFFFFF 哨兵表示
                sync_map = np.array(
                    [
                        (slot, zone, 0xFFFFFFFF if nd_stage < 0 else nd_stage, 0)
                        for _var, slot, zone, nd_stage in sync_bindings
                    ],
                    dtype=np.uint32,
                )
                sync_map.tofile(os.path.join(res_dir, f"ShapeKeyVarSyncMap_{ns}.buf"))
        legacy_path_vectors = os.path.join(res_dir, "PathVectors.buf")
        if os.path.isfile(legacy_path_vectors):
            os.remove(legacy_path_vectors)

    def _write_object_map(self, mod_export_path, sections, comp):
        parts = comp["parts"]
        n = len(parts)
        records = [struct.pack('<ffff', float(n), 0.0, 0.0, 0.0)]
        for part in parts:
            records.append(struct.pack(
                '<ffff',
                0.0,          # firstIndex 恒 0（分区局部）：detect 着色器 indexBase = firstIndex + tri*3，
                              # 各部件 IB 是独立分区文件，全局偏移会越界读垃圾（原作 ObjectMap 全为 0）
                float(part["index_count"]),
                7.0,          # mode
                0.0,          # objectID 恒 0（原作契约）：着色器 entry.w==0 时回退 objectID=firstIndex=0，
                              # 全部件命中统一匹配碰撞档案条目 0
            ))
        data = b''.join(records)
        out = os.path.join(mod_export_path, self._buffer_dir(sections, comp), f"{comp['base_name']}ObjectMap.buf")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, 'wb') as f:
            f.write(data)
        print(f"[DragInteraction] ObjectMap: {os.path.basename(out)} ({n} parts, {len(data)}B)")

    def _buffer_dir(self, sections, comp):
        """从 ini 的 Position 资源段解析缓冲文件夹名（动态，不硬编码）。"""
        res = comp["base_resource"]
        lines = sections.get(f"[{res}]", [])
        for line in lines:
            if line.strip().startswith("filename ="):
                path = line.split("=", 1)[1].strip().replace('\\', '/')
                if '/' in path:
                    return path.split('/')[0]
        return "Meshes"

    def _write_jiggle_masks(self, mod_export_path, sections, comp, ns):
        """烘焙每顶点 Top-K 区域权重。返回 True = 掩码已写出（或回退写出）；
        False = 所有配置区域对本组件均无有效权重，调用方应整体跳过该组件。"""
        entries = self._collect_enabled_zone_entries()
        zones = [empty for _, empty in entries]
        vertex_count = comp["vertex_count"]

        # 无区域 → zone0 全 1
        if not zones or not GB_CORE_AVAILABLE:
            fallback_zone_id = entries[0][0] if entries else 0
            self._write_masks_fallback(
                mod_export_path,
                sections,
                comp,
                vertex_count,
                fallback_zone_id,
                vertex_base=int(comp.get("vertex_base", 0) or 0),
            )
            return True

        # radius 参数与球尺度失配检查（防“整块刚体动”失配，原版实测 ratio 0.3~2.2）
        self._check_zone_radius_scale(zones)

        # 读取 Position.buf 顶点坐标。VLR 可能是 ZZMI RedirectSO 的运行时容量，
        # 比基础 Position.buf 多一个 base-vertex 前缀（当前为 3 个 stub 顶点）。
        # 权重先在基础缓冲的局部索引空间烘焙，写文件前再补齐运行时前缀。
        positions = self._read_position_buf(mod_export_path, sections, comp, vertex_count)
        if positions is None:
            self._write_masks_fallback(
                mod_export_path,
                sections,
                comp,
                vertex_count,
                entries[0][0],
                vertex_base=int(comp.get("vertex_base", 0) or 0),
            )
            return True
        source_vertex_count = len(positions)
        vertex_base, vertex_count = self._runtime_vertex_layout(comp, source_vertex_count)

        # 拓扑读取：任一启用区域开了球级沿表面扩散，或任一权重球配置了包含物体列表时读一次
        # IB 三角形 → 去重边（沿表面扩散用）；同时得到逐三角形部件名供包含列表过滤。
        # 只关闭沿表面扩散但仍需要包含列表过滤时也必须读 IB，否则 allowed_mask 会退化成全允许。
        edge_verts = None
        triangles = None
        tri_part_names = None
        needs_component_topology = any(
            _zone_propagate(empty.ssmt_drag_zone, self)
            or _zone_has_included_objects(empty.ssmt_drag_zone)
            for _, empty in entries
        )
        if needs_component_topology:
            result = self._read_component_triangles(
                mod_export_path, sections, comp, source_vertex_count
            )
            if result is not None:
                triangles, tri_part_names = result
                if any(_zone_propagate(empty.ssmt_drag_zone, self) for _, empty in entries):
                    edge_verts = gb_core.edges_from_triangles(triangles)
            else:
                print(f"[DragInteraction][WARNING] {comp['comp_name']} 无法读取 IB 拓扑，沿表面传播回退体积球")

        # 烘焙参考物体世界矩阵的逆（坐标系换算）
        ref_matrix_inv = self._get_reference_matrix_inv(comp)
        export_matrix = self._get_export_space_matrix()
        # 非镜像工作流补偿：场景网格被 X 镜像过（导入时 mesh.transform 翻转、物体矩阵不变）
        # 而空物体矩阵未跟随 → 需对球矩阵施加同样的 X 镜像后才与 Position.buf（还原后
        # 朝向）同空间，否则掩码左右颠倒（用户报告）。预览不受影响（所见即所得）。
        mirror = self._get_non_mirror_mirror()

        zone_ids = np.full(
            (source_vertex_count, SPARSE_ZONE_SLOTS),
            INVALID_ZONE_ID,
            dtype=np.uint32,
        )
        zone_weights = np.zeros(
            (source_vertex_count, SPARSE_ZONE_SLOTS), dtype=np.float32
        )
        overlap_counts = np.zeros(source_vertex_count, dtype=np.uint16)
        row_indices = np.arange(source_vertex_count)
        for zone_id, empty in entries:
            settings = empty.ssmt_drag_zone
            allowed_mask = _zone_allowed_vertex_mask(
                settings, len(positions), triangles, tri_part_names
            )
            if allowed_mask is not None and not np.any(allowed_mask):
                print(
                    f"[DragInteraction][WARNING] {comp['comp_name']} 区域 {empty.name} "
                    "包含物体列表未命中任何 IB 部件，该区域对本组件被跳过；"
                    "请检查包含物体是否属于当前 IB 前缀"
                )
                continue
            field = self._evaluate_zone_field(
                positions,
                empty,
                settings,
                ref_matrix_inv,
                mirror,
                edge_verts,
                export_matrix=export_matrix,
                propagate=_zone_propagate(settings, self),
                allowed_mask=allowed_mask,
            )
            if field is None:
                continue
            field = np.asarray(field, dtype=np.float32)
            positive = field > 1e-4
            overlap_counts[positive] += 1
            weakest_slot = np.argmin(zone_weights, axis=1)
            weakest_weight = zone_weights[row_indices, weakest_slot]
            replace = field > weakest_weight
            replace_rows = row_indices[replace]
            replace_slots = weakest_slot[replace]
            zone_weights[replace_rows, replace_slots] = field[replace]
            zone_ids[replace_rows, replace_slots] = np.uint32(zone_id)

        if not np.any(zone_weights > 1e-4):
            # 所有配置区域对本组件均无有效权重（包含物体列表未命中 IB 部件，
            # 或影响球与顶点包围盒无交集）：该组件按配置即不受拖拽，跳过注入
            # 而不是中止整个导出；逐区域原因已在上方逐条警告。
            print(
                f"[DragInteraction][WARNING] {comp['comp_name']}: "
                "所有配置区域对本组件均无有效权重，已跳过该组件的拖拽注入；"
                "若该组件预期应有拖拽，请检查区域的包含物体列表与空物体摆放"
            )
            self._remove_mask_files(mod_export_path, sections, comp)
            return False

        # 从基础 Position/IB 的局部索引空间映射到运行时 VB0。RedirectSO 的
        # carrier 由 base_vertex=3 开始，因此前三行保持无效/零权重。
        if vertex_base or vertex_count != source_vertex_count:
            runtime_zone_ids = np.full(
                (vertex_count, SPARSE_ZONE_SLOTS),
                INVALID_ZONE_ID,
                dtype=np.uint32,
            )
            runtime_zone_weights = np.zeros(
                (vertex_count, SPARSE_ZONE_SLOTS), dtype=np.float32
            )
            copy_end = vertex_base + source_vertex_count
            runtime_zone_ids[vertex_base:copy_end] = zone_ids
            runtime_zone_weights[vertex_base:copy_end] = zone_weights
            zone_ids = runtime_zone_ids
            zone_weights = runtime_zone_weights

        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        os.makedirs(out_dir, exist_ok=True)
        zone_ids.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneIDs.buf"))
        zone_weights.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneWeights.buf"))
        self._remove_legacy_mask_files(out_dir, comp["base_name"])
        overflow_vertices = int(np.count_nonzero(overlap_counts > SPARSE_ZONE_SLOTS))
        if overflow_vertices:
            max_overlap = int(overlap_counts.max())
            print(
                f"[DragInteraction][WARNING] {comp['comp_name']} 有 {overflow_vertices} 个顶点同时覆盖超过 "
                f"{SPARSE_ZONE_SLOTS} 个区域（最大 {max_overlap}）；仅保留权重最高的 {SPARSE_ZONE_SLOTS} 个"
            )
        print(
            f"[DragInteraction] SparseZoneMasks: {comp['comp_name']} "
            f"({vertex_count} 顶点, {len(zones)} 区域, Top-{SPARSE_ZONE_SLOTS})"
        )
        return True

    @staticmethod
    def _runtime_vertex_layout(comp, source_vertex_count):
        """返回 ``(base_vertex, runtime_vertex_count)`` 并校验缓冲跨度。

        ``source_vertex_count`` 来自磁盘 Position.buf；``vertex_count`` 来自
        VertexLimitRaise/运行时 VB0 容量。ZZMI 合并重定向会在 carrier 前放置
        stub 前缀，所以二者不能直接混作同一个数组维度。
        """
        source_vertex_count = max(0, int(source_vertex_count or 0))
        vertex_base = max(0, int(comp.get("vertex_base", 0) or 0))
        runtime_vertex_count = max(0, int(comp.get("vertex_count", 0) or 0))
        required_count = vertex_base + source_vertex_count
        if runtime_vertex_count == 0:
            runtime_vertex_count = required_count
            comp["vertex_count"] = runtime_vertex_count
        if runtime_vertex_count < required_count:
            raise ValueError(
                f"{comp.get('comp_name', 'component')} 的运行时顶点容量 "
                f"{runtime_vertex_count} 小于 base_vertex {vertex_base} + "
                f"Position 顶点数 {source_vertex_count}"
            )
        return vertex_base, runtime_vertex_count

    def _remove_mask_files(self, mod_export_path, sections, comp):
        """组件被跳过时清掉可能残留的掩码文件（含旧命名），避免陈旧缓冲被打包。"""
        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        for suffix in ("JiggleZoneIDs.buf", "JiggleZoneWeights.buf"):
            path = os.path.join(out_dir, f"{comp['base_name']}{suffix}")
            if os.path.isfile(path):
                os.remove(path)
        self._remove_legacy_mask_files(out_dir, comp["base_name"])

    def _write_masks_fallback(
        self,
        mod_export_path,
        sections,
        comp,
        vertex_count,
        fallback_zone_id=0,
        vertex_base=0,
    ):
        zone_ids = np.full((vertex_count, SPARSE_ZONE_SLOTS), INVALID_ZONE_ID, dtype=np.uint32)
        zone_weights = np.zeros((vertex_count, SPARSE_ZONE_SLOTS), dtype=np.float32)
        vertex_base = max(0, min(int(vertex_base or 0), int(vertex_count)))
        zone_ids[vertex_base:, 0] = np.uint32(fallback_zone_id)
        zone_weights[vertex_base:, 0] = 1.0
        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        os.makedirs(out_dir, exist_ok=True)
        zone_ids.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneIDs.buf"))
        zone_weights.tofile(os.path.join(out_dir, f"{comp['base_name']}JiggleZoneWeights.buf"))
        self._remove_legacy_mask_files(out_dir, comp["base_name"])
        print(
            f"[DragInteraction] SparseZoneMasks（回退 zone{fallback_zone_id} 全 1）: "
            f"{comp['comp_name']} ({vertex_count} 顶点)"
        )

    @staticmethod
    def _remove_legacy_mask_files(out_dir, base_name):
        for index in range(3):
            legacy_path = os.path.join(out_dir, f"{base_name}JiggleMasks{index}.buf")
            if os.path.isfile(legacy_path):
                os.remove(legacy_path)

    # =======================================================================
    # 碰撞检测资源烘焙（点云 + 两级均匀网格 + 每顶点 L0）
    # =======================================================================

    @staticmethod
    def _collider_link_distance(points):
        """连通阈值 = 3 × 点云平均最近邻距（粗网格剪枝求每点最近邻，取中位数）。

        对「紧贴皮肤的连续曲面」给出约 3 倍点距（同曲面必然连通）；
        对「多块分离碰撞体」给出块内点距的 3 倍（远小于块间距 ⇒ 正确分离）。
        取中位数而非均值：少数离群点（碎面片）不会把阈值抬高。
        """
        pts = np.asarray(points, dtype=np.float64)
        n = len(pts)
        if n < 2:
            return 0.0
        ext = np.maximum(pts.max(axis=0) - pts.min(axis=0), 1e-8)
        diag = float(np.linalg.norm(ext))
        fallback = 3.0 * diag / n ** (1.0 / 3.0)
        # 粗网格：格边长 ≈ 平均点距，使每格约 1 个点，最近邻必落在 27 邻域内
        step = max(diag / n ** (1.0 / 3.0), 1e-8)
        bmin = pts.min(axis=0)
        cell = np.floor((pts - bmin) / step).astype(np.int64)
        buckets = {}
        for i in range(n):
            key = (int(cell[i, 0]), int(cell[i, 1]), int(cell[i, 2]))
            buckets.setdefault(key, []).append(i)
        best = np.full(n, np.inf, dtype=np.float64)
        for (cx, cy, cz), idxs in buckets.items():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        other = buckets.get((cx + dx, cy + dy, cz + dz))
                        if not other:
                            continue
                        idx_arr = np.asarray(idxs, dtype=np.int64)
                        sub_a = pts[idx_arr]
                        sub_b = pts[np.asarray(other, dtype=np.int64)]
                        dv = sub_a[:, None, :] - sub_b[None, :, :]
                        d = np.sqrt(np.einsum("ijk,ijk->ij", dv, dv))
                        if dx == 0 and dy == 0 and dz == 0:
                            np.fill_diagonal(d, np.inf)
                        best[idx_arr] = np.minimum(best[idx_arr], d.min(axis=1))
        finite = best[np.isfinite(best)]
        if len(finite) == 0:
            return fallback
        spacing = float(np.median(finite))
        if spacing <= 0.0:
            return fallback
        return 3.0 * spacing

    @staticmethod
    def _collider_block_ids(points, link_dist):
        """按「点间距离 < link_dist」做连通块标记（空间网格剪枝，多块分离碰撞体各自判朝向用）。

        返回 (block_id, block_count)：block_id[i] 是点 i 所属块编号（0..block_count-1）。
        单块（丝袜-大腿这种紧贴场景）返回 block_count=1，调用方走整体质心即可。

        注意：不能用「格邻接」代替距离判定 —— 格边长必须 ≥ link_dist 才能让同格点天然连通，
        而那样两块相距仅一格的分离碰撞体会被误并成一块（质心落到两块中间，离群块整块算反）。
        """
        pts = np.asarray(points, dtype=np.float64)
        n = len(pts)
        if n == 0:
            return np.zeros(0, dtype=np.int64), 0
        step = max(float(link_dist), 1e-8)
        bmin = pts.min(axis=0)
        cell = np.floor((pts - bmin) / step).astype(np.int64)
        dims = np.maximum(cell.max(axis=0) + 1, 1).astype(np.int64)
        d12 = int(dims[1] * dims[2])
        d2 = int(dims[2])
        lin = (cell[:, 0] * dims[1] + cell[:, 1]) * dims[2] + cell[:, 2]

        # 按格分组（排序法，避免逐点 Python 循环）
        order = np.argsort(lin, kind="stable")
        slin = lin[order]
        bnd = np.flatnonzero(np.diff(slin)) + 1
        starts = np.concatenate([[0], bnd])
        ends = np.concatenate([bnd, [len(slin)]])
        uniq = slin[starts]
        groups = {int(k): order[s:e] for k, s, e in zip(uniq, starts, ends)}

        # 收集候选点对（同格取上三角，邻格只取半边，避免重复）
        half = [(dx, dy, dz)
                for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
                if (dx, dy, dz) != (0, 0, 0)
                and (dx > 0 or (dx == 0 and dy > 0) or (dx == 0 and dy == 0 and dz > 0))]
        us = []
        vs = []
        for k in uniq:
            kk = int(k)
            kx = kk // d12
            rem = kk % d12
            ky = rem // d2
            kz = rem % d2
            grp = groups[kk]
            if len(grp) > 1:
                ii, jj = np.triu_indices(len(grp), k=1)
                us.append(grp[ii])
                vs.append(grp[jj])
            for dx, dy, dz in half:
                other = groups.get(int(((kx + dx) * dims[1] + (ky + dy)) * dims[2] + (kz + dz)))
                if other is None:
                    continue
                us.append(np.repeat(grp, len(other)))
                vs.append(np.tile(other, len(grp)))
        if us:
            u = np.concatenate(us)
            v = np.concatenate(vs)
            dv = pts[u] - pts[v]
            keep = np.einsum("ij,ij->i", dv, dv) < step * step
            u = u[keep]
            v = v[keep]
        else:
            u = np.zeros(0, dtype=np.int64)
            v = np.zeros(0, dtype=np.int64)

        # 向量化并查集：最小标签传播 + 路径压缩（逐对 union 走 Python 循环时
        # 紧贴大腿这种点云有 ~130 万对候选边，实测 108s，这里降到 1s 量级）
        parent = np.arange(n, dtype=np.int64)
        if len(u):
            for _ in range(256):
                nxt = parent.copy()
                np.minimum.at(nxt, u, parent[v])
                np.minimum.at(nxt, v, parent[u])
                nxt = nxt[nxt]
                if np.array_equal(nxt, parent):
                    break
                parent = nxt
            for _ in range(64):  # 兜底：确保每点都指向根
                nxt = parent[parent]
                if np.array_equal(nxt, parent):
                    break
                parent = nxt
        roots = parent
        _, block_id = np.unique(roots, return_inverse=True)
        block_id = np.asarray(block_id, dtype=np.int64).reshape(-1)
        return block_id, (int(block_id.max()) + 1) if len(block_id) else 0

    @staticmethod
    def _collider_face_normals(positions, triangles, collider_mask):
        """碰撞体三角形面法线（未归一化，模长 = 2×面积），winding 按「本连通块质心」统一朝外。

        返回 (tri_idx, face_n)：tri_idx 是「三个顶点都在碰撞体内」的三角形下标。
        多块分离碰撞体各自用本块质心判朝向 —— 用整体质心会把离群块整块算反，
        这是导出侧点云法线朝向的老问题（碰撞体可能多块分离且形状奇怪）。
        """
        p = np.asarray(positions, dtype=np.float32)
        tris_all = np.asarray(triangles, dtype=np.int64)
        empty = np.zeros((0, 3), dtype=np.float32)
        if len(tris_all) == 0 or not np.any(collider_mask):
            return np.zeros(0, dtype=np.int64), empty
        keep = (collider_mask[tris_all[:, 0]]
                & collider_mask[tris_all[:, 1]]
                & collider_mask[tris_all[:, 2]])
        tri_idx = np.flatnonzero(keep)
        tris = tris_all[keep]
        if len(tris) == 0:
            return tri_idx, empty
        p0 = p[tris[:, 0]]
        p1 = p[tris[:, 1]]
        p2 = p[tris[:, 2]]
        face_n = np.cross(p1 - p0, p2 - p0).astype(np.float32)
        tri_centroid = (p0 + p1 + p2) / 3.0

        cidx = np.flatnonzero(collider_mask)
        cpts = np.asarray(positions, dtype=np.float32)[cidx]
        block_id = np.zeros(len(cpts), dtype=np.int64)
        block_count = 1
        if len(cpts) > 1:
            # 连通阈值 = 3 × 点云平均最近邻距：块内相邻点必然连通，真正分离的多块
            # 碰撞体（间距远大于自身点距）各自成块。不能用「AABB 对角线 / n^(1/3)」——
            # 那个估计会被块间分离距离主导，把分离的两块误并成一块（质心落到两块
            # 中间，离群块整块算反，正是导出侧点云法线朝向的老问题）。
            link_dist = SSMTNode_PostProcess_DragInteraction._collider_link_distance(cpts)
            block_id, block_count = SSMTNode_PostProcess_DragInteraction._collider_block_ids(
                cpts, link_dist
            )
        if block_count <= 1:
            ref = cpts.mean(axis=0) if len(cpts) else np.zeros(3, dtype=np.float32)
            flip = np.einsum("ij,ij->i", face_n, tri_centroid - ref) < 0.0
            face_n[flip] = -face_n[flip]
            return tri_idx, face_n
        centroids = np.zeros((block_count, 3), dtype=np.float64)
        counts = np.zeros(block_count, dtype=np.int64)
        np.add.at(centroids, block_id, cpts.astype(np.float64))
        np.add.at(counts, block_id, 1)
        counts[counts == 0] = 1
        centroids /= counts[:, None]
        vert_block = np.full(len(p), -1, dtype=np.int64)
        vert_block[cidx] = block_id
        blk = vert_block[tris[:, 0]]
        blk = np.where(blk < 0, vert_block[tris[:, 1]], blk)
        blk = np.where(blk < 0, vert_block[tris[:, 2]], blk)
        blk = np.maximum(blk, 0)
        ref = centroids[blk]
        flip = np.einsum("ij,ij->i", face_n, tri_centroid - ref) < 0.0
        face_n[flip] = -face_n[flip]
        return tri_idx, face_n

    @staticmethod
    def _collider_vertex_normals(positions, triangles, collider_mask, vertex_count):
        """碰撞体顶点法线：三角面法线面积加权平均，winding 按「本连通块质心」统一朝外。

        面法线用 cross（模长 = 2×面积）即天然面积加权。
        返回 (vertex_count, 3) float32 单位法线；非碰撞体顶点为 0。
        """
        normals = np.zeros((int(vertex_count), 3), dtype=np.float32)
        if not np.any(collider_mask):
            return normals
        tri_idx, face_n = SSMTNode_PostProcess_DragInteraction._collider_face_normals(
            positions, triangles, collider_mask
        )
        if len(tri_idx) == 0:
            return normals
        tris = np.asarray(triangles, dtype=np.int64)[tri_idx]
        np.add.at(normals, tris[:, 0], face_n)
        np.add.at(normals, tris[:, 1], face_n)
        np.add.at(normals, tris[:, 2], face_n)
        norms = np.linalg.norm(normals, axis=1, keepdims=True)
        norms[norms < 1e-12] = 1.0
        return (normals / norms).astype(np.float32)

    @staticmethod
    def _sample_collider_surface(positions, triangles, collider_mask, budget,
                                 seed=20261010):
        """碰撞体三角形表面面积加权重心坐标采样，补足纯顶点采样留下的面内空洞。

        纯顶点采样在面内的最近邻距约 0.028（远大于丝袜-大腿 0.001 的真实间隙），
        单点切平面/球约束跟不上曲率。按面积分配配额、三角形内用 sqrt 变换保证
        面积均匀；采样序列由固定 seed 决定 ⇒ 同一模型每次导出结果一致。
        返回 (points (n,3) float32, tri_local (n,) int64, face_unit (n,3) float32)，
        tri_local 是采样点所属三角形在 `tri_idx` 里的局部下标。
        """
        empty_p = np.zeros((0, 3), dtype=np.float32)
        empty_i = np.zeros(0, dtype=np.int64)
        tri_idx, face_n = SSMTNode_PostProcess_DragInteraction._collider_face_normals(
            positions, triangles, collider_mask
        )
        if len(tri_idx) == 0 or budget <= 0:
            return empty_p, empty_i, empty_p
        p = np.asarray(positions, dtype=np.float32)
        tris = np.asarray(triangles, dtype=np.int64)[tri_idx]
        p0 = p[tris[:, 0]]
        p1 = p[tris[:, 1]]
        p2 = p[tris[:, 2]]
        area = 0.5 * np.linalg.norm(np.cross(p1 - p0, p2 - p0), axis=1)
        total = float(area.sum())
        if total <= 0.0:
            return empty_p, empty_i, empty_p
        quota = np.maximum(np.floor(area / total * float(budget)).astype(np.int64), 1)
        total_n = int(quota.sum())
        if total_n <= 0:
            return empty_p, empty_i, empty_p
        # 全量一次生成随机数 + np.repeat 展开三角形索引。逐三角形 Python 循环在碰撞体
        # 有 2 万面时是导出耗时主因（每面 2 次 rng 调用 + 数次数组运算 + 一次 append）。
        rng = np.random.default_rng(int(seed))
        r1 = rng.random(total_n)
        r2 = rng.random(total_n)
        su = np.sqrt(r1)
        w0 = 1.0 - su
        w1 = su * (1.0 - r2)
        w2 = su * r2
        tid = np.repeat(np.arange(len(tris), dtype=np.int64), quota)
        pts = (p0[tid] * w0[:, None]
               + p1[tid] * w1[:, None]
               + p2[tid] * w2[:, None]).astype(np.float32)
        fn = face_n[tid]
        nn = np.linalg.norm(fn, axis=1, keepdims=True)
        nn[nn < 1e-12] = 1.0
        return pts, tid, (fn / nn).astype(np.float32)

    @staticmethod
    def _decimate_collider_points(points, normals, budget):
        """体素质心保留抽稀：把点云压到 ≤ budget 点，每格保留距几何质心最近的一点。

        初始格边长按体积/预算反推；因边界效应实际格数可能略超预算，迭代放大
        格边长（最多 10 次）直到格数 ≤ budget，保证硬上界。
        """
        if len(points) <= budget or budget <= 0:
            return points, normals
        pts = np.asarray(points, dtype=np.float64)
        bmin = pts.min(axis=0)
        bmax = pts.max(axis=0)
        ext = np.maximum(bmax - bmin, 1e-8)
        vol = float(np.prod(ext))
        h = (vol / budget) ** (1.0 / 3.0)
        cell = None
        dims = None
        cid = None
        for _ in range(10):
            cell = np.floor((pts - bmin) / h).astype(np.int64)
            dims = np.floor((bmax - bmin) / h).astype(np.int64) + 1
            cid = cell[:, 0] + cell[:, 1] * dims[0] + cell[:, 2] * dims[0] * dims[1]
            if len(np.unique(cid)) <= budget:
                break
            h *= 1.1
        order = np.argsort(cid, kind="stable")
        sorted_cid = cid[order]
        boundaries = np.flatnonzero(np.diff(sorted_cid)) + 1
        starts = np.concatenate([[0], boundaries])
        ends = np.concatenate([boundaries, [len(sorted_cid)]])
        keep = []
        for s, e in zip(starts, ends):
            idx = order[s:e]
            c = pts[idx].mean(axis=0)
            d = np.einsum("ij,ij->i", pts[idx] - c, pts[idx] - c)
            keep.append(idx[int(np.argmin(d))])
        keep = np.asarray(keep, dtype=np.int64)
        return np.asarray(points, dtype=np.float32)[keep], np.asarray(normals, dtype=np.float32)[keep]

    @staticmethod
    def _build_collider_grid(points, cell_size, margin):
        """count-sort 均匀网格 + 粗层。返回 dict：
        - fine_cells: uint32 (cellN,4) 每 cell (offset,count,0,0)
        - coarse_cells: float32 (coarseN,4) 每 cell (centroid.xyz,R)
        - dims: (nx,ny,nz) 细格；cdims: 粗格；h: 细格边长；h_c: 粗格边长；bmin
        points 已按细格重排（返回重排后的顺序引用，与 fine_cells offset 对齐）。
        """
        pts = np.asarray(points, dtype=np.float64)
        bmin = pts.min(axis=0) - margin
        bmax = pts.max(axis=0) + margin
        ext = np.maximum(bmax - bmin, 1e-8)
        if cell_size > 0:
            h = float(cell_size)
        else:
            # 自适应：平均最近邻距的 2.5 倍近似
            h = 2.5 * (float(np.prod(ext)) / max(len(pts), 1)) ** (1.0 / 3.0)
        dims = np.maximum(np.floor((bmax - bmin) / h).astype(np.int64) + 1, 1)
        h_c = h * 4.0
        cdims = np.maximum(np.floor((bmax - bmin) / h_c).astype(np.int64) + 1, 1)

        cell = np.floor((pts - bmin) / h).astype(np.int64)
        cell = np.minimum(np.maximum(cell, 0), dims - 1)
        cid = cell[:, 0] + cell[:, 1] * dims[0] + cell[:, 2] * dims[0] * dims[1]
        order = np.argsort(cid, kind="stable")
        sorted_cid = cid[order]
        sorted_pts = pts[order]

        cell_n = int(dims[0] * dims[1] * dims[2])
        fine_cells = np.zeros((cell_n, 4), dtype=np.uint32)
        if len(sorted_pts):
            boundaries = np.flatnonzero(np.diff(sorted_cid)) + 1
            starts = np.concatenate([[0], boundaries])
            ends = np.concatenate([boundaries, [len(sorted_cid)]])
            for s, e, c in zip(starts, ends, sorted_cid[starts]):
                fine_cells[int(c), 0] = np.uint32(s)
                fine_cells[int(c), 1] = np.uint32(e - s)

        # 粗层
        ccell = np.floor((sorted_pts - bmin) / h_c).astype(np.int64)
        ccell = np.minimum(np.maximum(ccell, 0), cdims - 1)
        ccid = ccell[:, 0] + ccell[:, 1] * cdims[0] + ccell[:, 2] * cdims[0] * cdims[1]
        coarse_n = int(cdims[0] * cdims[1] * cdims[2])
        coarse_cells = np.zeros((coarse_n, 4), dtype=np.float32)
        if len(sorted_pts):
            corder = np.argsort(ccid, kind="stable")
            c_sorted = ccid[corder]
            c_pts = sorted_pts[corder]
            c_boundaries = np.flatnonzero(np.diff(c_sorted)) + 1
            c_starts = np.concatenate([[0], c_boundaries])
            c_ends = np.concatenate([c_boundaries, [len(c_sorted)]])
            for s, e, c in zip(c_starts, c_ends, c_sorted[c_starts]):
                gp = c_pts[s:e]
                centroid = gp.mean(axis=0)
                r = np.sqrt(np.max(np.einsum("ij,ij->i", gp - centroid, gp - centroid), initial=0.0))
                coarse_cells[int(c), 0:3] = centroid.astype(np.float32)
                coarse_cells[int(c), 3] = np.float32(r)

        return {
            "sorted_points": sorted_pts.astype(np.float32),
            "order": order,
            "fine_cells": fine_cells,
            "coarse_cells": coarse_cells,
            "dims": dims,
            "cdims": cdims,
            "h": np.float32(h),
            "h_c": np.float32(h_c),
            "bmin": bmin.astype(np.float32),
        }

    @staticmethod
    def _bake_collider_l0(positions, points, masked, normals=None):
        """每顶点 L0 接触数据（设计语义）：q0=最近碰撞点、t0=导出时刻到碰撞体表面的
        带符号距离（运行时阈值：任何位移后该顶点到表面的距离不得低于 t0）、
        n0=q0 处外向表面法线。

        布局：l0[2i]=(q0.xyz,t0)、l0[2i+1]=(n0.xyz,valid)。未遮顶点 valid=0。
        传 normals（与 points 对齐的单位法线）时 t0 取带符号表面距离、n0 取表面法线；
        不传（旧调用）回退「到最近点的无符号距离 + 指向顶点的方向」。
        分块向量化全量对拍，避免 (V×Nc×3) 一次性内存爆炸。
        """
        V = len(positions)
        l0 = np.zeros((V * 2, 4), dtype=np.float32)
        if masked is None:
            idx = np.arange(V)
        else:
            idx = np.flatnonzero(np.asarray(masked, dtype=bool))
        if len(idx) == 0 or len(points) == 0:
            return l0
        pts = np.asarray(points, dtype=np.float32)
        nrm = np.asarray(normals, dtype=np.float32) if normals is not None else None
        pos = np.asarray(positions, dtype=np.float32)
        chunk = 512
        for start in range(0, len(idx), chunk):
            sel = idx[start:start + chunk]
            q = pos[sel]  # (c,3)
            diff = q[:, None, :] - pts[None, :, :]  # (c,Nc,3)
            d2 = np.einsum("cnk,cnk->cn", diff, diff)
            j = np.argmin(d2, axis=1)
            rows = np.arange(len(sel))
            q0 = pts[j]
            if nrm is not None:
                # 设计语义：带符号表面距离（负 = 导出时已穿透）+ 表面法线
                n0 = nrm[j]
                nn = np.linalg.norm(n0, axis=1, keepdims=True)
                nn[nn < 1e-12] = 1.0
                n0 = n0 / nn
                t0 = np.einsum("ci,ci->c", q - q0, n0)
            else:
                n0 = q - q0
                nn = np.linalg.norm(n0, axis=1, keepdims=True)
                nn[nn < 1e-12] = 1.0
                n0 = n0 / nn
                t0 = np.linalg.norm(q - q0, axis=1)
            l0[2 * sel, 0:3] = q0.astype(np.float32)
            l0[2 * sel, 3] = t0.astype(np.float32)
            l0[2 * sel + 1, 0:3] = n0.astype(np.float32)
            l0[2 * sel + 1, 3] = 1.0
        return l0

    @staticmethod
    def _collider_contact_exact(positions, triangles, collider_mask, masked,
                                cell_size=0.0, chunk=512, vertex_normals=None):
        """每遮罩顶点的精确表面接触：q0 = 碰撞体三角形上的最近点、t0 = 带符号距离、n0 = 面法线。

        为什么不能用「最近采样点 + 该点法线」：采样抽稀到 budget 后点距（~0.009）远大于
        丝袜-大腿真实间隙（0.001），最近采样点与顶点的连线几乎与法线垂直 ⇒
        dot(P - q0, n0) 塌成 0.0007 量级、且约 40% 顶点符号为负，运行时阈值形同虚设。

        做法：碰撞体三角形 AABB 空间网格剪枝 → 候选三角形上做「重心坐标 clamp 投影」。
        q0 是三角形内的凸组合点 ⇒ 恒有 dot(P - q0, n0) == t0，绑定姿态下运行时约束恰好
        取等，不会因采样稀疏而漂移。网格边长自适应三角形尺度（p99 边长 × 1.5）。

        返回 (V*2, 4) float32：l0[2i]=(q0.xyz,t0)、l0[2i+1]=(n0.xyz,valid)；未遮 valid=0。
        """
        V = len(positions)
        out = np.zeros((V * 2, 4), dtype=np.float32)
        if masked is None:
            idx = np.arange(V)
        else:
            idx = np.flatnonzero(np.asarray(masked, dtype=bool))
        if len(idx) == 0 or triangles is None or len(triangles) == 0:
            return out

        p = np.asarray(positions, dtype=np.float64)
        tris_all = np.asarray(triangles, dtype=np.int64)
        cmask = np.asarray(collider_mask, dtype=bool)
        keep = cmask[tris_all[:, 0]] & cmask[tris_all[:, 1]] & cmask[tris_all[:, 2]]
        tris = tris_all[keep]
        if len(tris) == 0:
            return out
        A = p[tris[:, 0]]
        B = p[tris[:, 1]]
        C = p[tris[:, 2]]
        n_face = np.cross(B - A, C - A)
        nn = np.linalg.norm(n_face, axis=1, keepdims=True)
        nn[nn < 1e-12] = 1.0
        n_face = n_face / nn
        # 面法线按「三个顶点法线的均值」定向朝外。三角形绕序不可控，定向前的
        # n_face 有一半左右是反的 —— 而 l0n 是 shader 在网格查询落空时唯一的
        # 兜底判据（`dot(p - p0, n0) >= -tolerance`），方向反了就等于放行穿模。
        if vertex_normals is not None:
            vn_all = np.asarray(vertex_normals, dtype=np.float64)
            ref = (vn_all[tris[:, 0]] + vn_all[tris[:, 1]] + vn_all[tris[:, 2]]) / 3.0
            n_face = np.where((np.einsum("cd,cd->c", ref, n_face) < 0.0)[:, None],
                              -n_face, n_face)

        # ---- 三角形空间网格：AABB 登记，顶点所在格直接含全部覆盖它的三角形 ----
        tri_lo = np.minimum(np.minimum(A, B), C)
        tri_hi = np.maximum(np.maximum(A, B), C)
        edges = np.concatenate([
            np.linalg.norm(B - A, axis=1),
            np.linalg.norm(C - B, axis=1),
            np.linalg.norm(A - C, axis=1),
        ])
        if cell_size and cell_size > 0.0:
            h = float(cell_size)
        else:
            # p99 边长 × 1.5：绝大多数三角形的 AABB 落在单格内，格内三角形数也不至于爆炸
            h = max(float(np.percentile(edges, 99)) * 1.5, 1e-6) if len(edges) else 1e-6
        bmin = tri_lo.min(axis=0) - h
        lo = np.floor((tri_lo - bmin) / h).astype(np.int64)
        hi = np.floor((tri_hi - bmin) / h).astype(np.int64)
        dims = np.maximum(hi.max(axis=0) + 2, 1).astype(np.int64)
        span = hi - lo + 1

        buckets = {}
        for t in range(len(tris)):
            bx = int(lo[t, 0])
            by = int(lo[t, 1])
            bz = int(lo[t, 2])
            for ox in range(int(span[t, 0])):
                for oy in range(int(span[t, 1])):
                    for oz in range(int(span[t, 2])):
                        key = ((bx + ox) * dims[1] + (by + oy)) * dims[2] + (bz + oz)
                        buckets.setdefault(key, []).append(t)

        # ---- 逐格处理：同格顶点共享候选三角形集合 ----
        vcell = np.floor((p[idx] - bmin) / h).astype(np.int64)
        vkey = (vcell[:, 0] * dims[1] + vcell[:, 1]) * dims[2] + vcell[:, 2]
        order = np.argsort(vkey, kind="stable")
        sorted_key = vkey[order]
        boundaries = np.flatnonzero(np.diff(sorted_key)) + 1
        starts = np.concatenate([[0], boundaries])
        ends = np.concatenate([boundaries, [len(sorted_key)]])
        neigh = [(dx, dy, dz)
                 for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)]

        for s, e in zip(starts, ends):
            rows = order[s:e]
            cx = int(vcell[rows[0], 0])
            cy = int(vcell[rows[0], 1])
            cz = int(vcell[rows[0], 2])
            cand = []
            for dx, dy, dz in neigh:
                b = buckets.get(((cx + dx) * dims[1] + (cy + dy)) * dims[2] + (cz + dz))
                if b:
                    cand.extend(b)
            if not cand:
                continue
            cand = np.unique(np.asarray(cand, dtype=np.int64))
            a = A[cand]
            b_ = B[cand]
            c_ = C[cand]
            nf = n_face[cand]
            v0 = b_ - a
            v1 = c_ - a
            d00 = np.einsum("cd,cd->c", v0, v0)
            d01 = np.einsum("cd,cd->c", v0, v1)
            d11 = np.einsum("cd,cd->c", v1, v1)
            den = d00 * d11 - d01 * d01
            den = np.where(np.abs(den) < 1e-20, 1e-20, den)
            for r0 in range(0, len(rows), chunk):
                sub_rows = rows[r0:r0 + chunk]
                sel = idx[sub_rows]
                q = p[sel]
                v2 = q[:, None, :] - a[None, :, :]
                d20 = np.einsum("mcd,cd->mc", v2, v0)
                d21 = np.einsum("mcd,cd->mc", v2, v1)
                wb = (d11[None, :] * d20 - d01[None, :] * d21) / den[None, :]
                wc = (d00[None, :] * d21 - d01[None, :] * d20) / den[None, :]
                wa = 1.0 - wb - wc
                # 重心坐标 clamp 到三角形内：投影落在面外时退化为边上最近点（凸组合仍在面上）
                wa = np.clip(wa, 0.0, 1.0)
                wb = np.clip(wb, 0.0, 1.0)
                wc = np.clip(wc, 0.0, 1.0)
                wsum = wa + wb + wc
                wsum = np.where(wsum < 1e-12, 1.0, wsum)
                wa = wa / wsum
                wb = wb / wsum
                wc = wc / wsum
                q_on = (a[None, :, :] * wa[:, :, None]
                        + b_[None, :, :] * wb[:, :, None]
                        + c_[None, :, :] * wc[:, :, None])
                # 最近三角形必须按「欧氏距离」挑，不能按 |dot(P-q, nf)| 挑：
                # 大腿是圆柱面，远处三角形的「平面延伸」会恰好扫过丝袜顶点，使平面距离塌成 0
                # （实测按平面距离挑时 |P-q0| 中位 0.0298、t0 中位 0.000055，而真实间隙 0.001）。
                diff = q[:, None, :] - q_on
                dist2 = np.einsum("mcd,mcd->mc", diff, diff)
                j = np.argmin(dist2, axis=1)
                mi = np.arange(len(sel))
                sdist = np.einsum("mcd,mcd->mc", diff, nf[None, :, :])
                t0 = sdist[mi, j]
                q0 = q_on[mi, j]
                n0 = nf[j]
                out[2 * sel, 0:3] = q0.astype(np.float32)
                out[2 * sel, 3] = t0.astype(np.float32)
                out[2 * sel + 1, 0:3] = n0.astype(np.float32)
                out[2 * sel + 1, 3] = 1.0
        return out

    def _bake_collider_tri_grid(self, positions, triangles, collider_mask,
                                vertex_normals=None, cell_size=0.0,
                                max_per_cell=4096, reach=0.6, min_cell=0.0,
                                shell_cells=1):
        """把碰撞体三角形装进均匀网格，供运行时「动态最近距离」查询。

        为什么必须动态查询：绑定姿态下最近的 K 个三角形只覆盖顶点附近的一小片
        曲面。实测位移 0.25 时顶点早已离开那 K 个三角形，静态约束完全失效 ——
        真实数据上 235 个丝袜顶点 × 12 方向 × 4 长度 = 11280 个案例：静态 K=8
        穿模 693、最大穿深 0.175（顶点 v22697 沿 [0.71,0,0.71] 拉 0.25 被完全
        放行，实际陷进大腿 0.175）。运行时按 p 现查邻域三角形，约束才对任意
        位移都成立。

        返回 (grid_u32, tri_verts, tri_idx, meta)：
          grid_u32  —— uint32 一维数组，布局：
                        [0..2] dims.xyz       [3] tri_count
                        [4]    cell_count     [5] idx_count
                        [6]    max_per_cell   [7] 保留
                        [8 .. 8+cell_count]   CSR 每格起始（长度 cell_count+1）
                        其后                  三角形索引列表
          tri_verts —— float32 (tri_count, 4, 4)：3 个顶点 xyz + 定向后的面法线
          tri_idx   —— int64 (tri_count, 3)：每个网格三角形的三个顶点缓冲索引，
                       与 tri_verts 逐条对应（权重重映射按它反查三角形顶点的权重）
          meta      —— dict(bmin (3,) float, cell float, dims (3,) int, cell_count int)

        reach 是网格向外扩张的距离，必须 ≥ 拖拽位移上限（maxOffset，默认 0.5），
        否则顶点被拖出网格后查询落空、约束静默失效。min_cell 是格子边长下限，
        决定查询够得着多远（27 邻域覆盖 ±cell）。max_per_cell 是每格索引上限，
        默认 4096 等于不截断 —— 截断会让「最近的三角形恰好被丢掉」，查询拿到次近
        的三角形，符号判断随之出错。

        shell_cells 是「表面壳」的厚度，单位是格。只按三角形自身 AABB 登记时，
        三角形只落在紧贴自己的那几格里，整个网格只有薄薄一层有数据（实测非空格
        409/74,052 = 0.55%）；运行时查询是当前格 ±1，于是有效半径只有 ±cell
        （≈0.068），顶点离开表面 0.10 之后就开始大量查空。这里把登记范围向外
        多铺 shell_cells 格，有效半径变成 ±(1+shell_cells)×cell，壳外那几格也能
        找到最近三角形。代价是每格三角形数 ×(2·shell_cells+1)³，所以登记改成
        「全收 → 按中心距离排序 → 截断」，保证被 max_per_cell 砍掉的永远是远的。
        """
        empty_meta = {"bmin": (0.0, 0.0, 0.0), "cell": 0.0,
                      "dims": (0, 0, 0), "cell_count": 0}
        p = np.asarray(positions, dtype=np.float64)
        tris_all = np.asarray(triangles, dtype=np.int64)
        cmask = np.asarray(collider_mask, dtype=bool)
        if len(tris_all) == 0:
            return (np.zeros(8, dtype=np.uint32), np.zeros((0, 4, 4), dtype=np.float32),
                    np.zeros(0, dtype=np.int64), empty_meta)
        keep = cmask[tris_all[:, 0]] & cmask[tris_all[:, 1]] & cmask[tris_all[:, 2]]
        tris = tris_all[keep]
        if len(tris) == 0:
            return (np.zeros(8, dtype=np.uint32), np.zeros((0, 4, 4), dtype=np.float32),
                    np.zeros(0, dtype=np.int64), empty_meta)

        A = p[tris[:, 0]]
        B = p[tris[:, 1]]
        C = p[tris[:, 2]]
        n_face = np.cross(B - A, C - A)
        nn = np.linalg.norm(n_face, axis=1, keepdims=True)
        nn[nn < 1e-12] = 1.0
        n_face = n_face / nn

        # 三角形朝向：用三个顶点法线的均值定向（顶点法线朝外是可靠的）。没有顶点
        # 法线时退回「远离碰撞体 AABB 中心」——对含大腿+臀部+躯干的多连通碰撞体
        # 不可靠，只作为兜底。
        if vertex_normals is not None:
            vn = np.asarray(vertex_normals, dtype=np.float64)
            if vn.ndim == 2 and vn.shape[0] == len(p) and vn.shape[1] >= 3:
                ref = (vn[tris[:, 0]] + vn[tris[:, 1]] + vn[tris[:, 2]]) / 3.0
            else:
                ref = None
        else:
            ref = None
        if ref is None:
            body_center = 0.5 * (A.min(axis=0) + A.max(axis=0))
            ref = (A + B + C) / 3.0 - body_center
        flip = np.einsum("ij,ij->i", ref, n_face) < 0.0
        n_out = np.where(flip[:, None], -n_face, n_face)

        tri_verts = np.zeros((len(tris), 4, 4), dtype=np.float32)
        tri_verts[:, 0, 0:3] = A.astype(np.float32)
        tri_verts[:, 1, 0:3] = B.astype(np.float32)
        tri_verts[:, 2, 0:3] = C.astype(np.float32)
        tri_verts[:, 3, 0:3] = n_out.astype(np.float32)

        # 网格边长：p99 边长 × 1.5，保证绝大多数三角形落在单格内，格内三角形数
        # 不至于爆炸（实测 max 679）。格子小还有个好处：每格按「三角形中心到格
        # 中心距离」排序才准 —— 格中心离格内任何点都近。min_cell 只在需要更粗的
        # 网格时用，默认 0 表示不抬高：实测把 cell 从 0.0585 抬到 0.15 会让单格
        # 三角形数涨到几千、撞上 shader 的查询上限，穿模反而从 584 涨到 1225。
        edges = np.concatenate([
            np.linalg.norm(B - A, axis=1),
            np.linalg.norm(C - B, axis=1),
            np.linalg.norm(A - C, axis=1),
        ])
        if cell_size and cell_size > 0.0:
            h = float(cell_size)
        else:
            h = max(float(np.percentile(edges, 99)) * 1.5, 1e-6) if len(edges) else 1e-6
        h = max(h, float(min_cell))
        tri_lo = np.minimum(np.minimum(A, B), C)
        tri_hi = np.maximum(np.maximum(A, B), C)
        # 网格必须覆盖「拖拽可达范围」，不能只包住绑定姿态的碰撞体。实测顶点沿
        # +Y 拉 0.5 会直接跑出只包 AABB 的网格，查询落空 ⇒ signed 停在 +1e30
        # ⇒ 陷进大腿 0.285 却被放行。这里向外各扩 reach（默认 0.6，大于
        # maxOffset 的 0.5 默认上限）。shader 侧另有 clamp 兜底。
        bmin = tri_lo.min(axis=0) - h - reach
        # 登记范围按 shell_cells 格外扩（见 docstring 的 shell_cells 说明）。
        # lo 不能取负（bmin 已经留了 h 余量，扩完正常也在 0 以上，clamp 只是兜底）。
        pad = int(max(shell_cells, 0))
        lo = np.maximum(np.floor((tri_lo - bmin) / h).astype(np.int64) - pad, 0)
        hi = np.floor((tri_hi - bmin) / h).astype(np.int64) + pad
        span_max = hi.max(axis=0)
        dims = np.maximum(span_max + 2 + int(np.ceil(reach / h)) * 2, 1).astype(np.int64)
        cell_count = int(dims[0] * dims[1] * dims[2])
        span = hi - lo + 1

        # 每格按「三角形中心到格中心距离」升序登记，最近的在最前。shader 侧有
        # 查询上限（COLLISION_GRID_MAX_TRIS），格子放大后单格三角形数会到几千，
        # 顺序登记会让截断恰好丢掉最近的三角形 —— 而最近的三角形才是决定
        # 带符号距离正负的那个。
        tri_c = (A + B + C) / 3.0
        buckets = {}
        for t in range(len(tris)):
            bx = int(lo[t, 0])
            by = int(lo[t, 1])
            bz = int(lo[t, 2])
            for ox in range(int(span[t, 0])):
                for oy in range(int(span[t, 1])):
                    for oz in range(int(span[t, 2])):
                        # 线性下标必须与 shader 的取法一致：shader 里是
                        #   ci = x + dims.x * (y + dims.y * z)   —— x 最快变化
                        # 这里原先写的是 ((x)*dims[1] + y)*dims[2] + z（z 最快），
                        # 与 shader 相反 ⇒ 运行时查到的永远是另一个格子。实测后果：
                        # 拖拽后 235/235 个顶点查询全部落空（返回 1e30），
                        # `DistGridSigned(p1) < -tolerance` 恒为 false ⇒ 距离 CCD
                        # 从不触发 ⇒ 「拖拽手感正常但一路穿模」。
                        key = ((bz + oz) * dims[1] + (by + oy)) * dims[0] + (bx + ox)
                        cell = buckets.get(key)
                        if cell is None:
                            buckets[key] = [t]
                        else:
                            # 先全收、不在这里截断 —— 下面的排序才知道谁近谁远，
                            # 边收边砍会按三角形索引顺序丢掉近的、留下远的。
                            cell.append(t)
        mp = int(max_per_cell)
        for key, cell in buckets.items():
            if len(cell) < 2:
                continue
            kx = key % dims[0]
            ky = (key // dims[0]) % dims[1]
            kz = key // (dims[0] * dims[1])
            cc = bmin + (np.array([kx, ky, kz], dtype=np.float64) + 0.5) * h
            arr = np.asarray(cell, dtype=np.int64)
            dd = np.einsum("cd,cd->c", tri_c[arr] - cc, tri_c[arr] - cc)
            order = np.argsort(dd, kind="stable")
            if mp > 0 and len(order) > mp:
                order = order[:mp]
            buckets[key] = arr[order].tolist()

        offsets = np.zeros(cell_count + 1, dtype=np.uint32)
        flat = []
        for key in range(cell_count):
            offsets[key] = len(flat)
            cell = buckets.get(key)
            if cell:
                flat.extend(cell)
        offsets[cell_count] = len(flat)

        head = np.array([dims[0], dims[1], dims[2], len(tris),
                         cell_count, len(flat), int(max_per_cell), 0], dtype=np.uint32)
        grid = np.concatenate([
            head, offsets,
            np.asarray(flat, dtype=np.uint32) if flat else np.zeros(0, dtype=np.uint32),
        ])
        meta = {
            "bmin": (float(bmin[0]), float(bmin[1]), float(bmin[2])),
            "cell": float(h),
            "dims": (int(dims[0]), int(dims[1]), int(dims[2])),
            "cell_count": cell_count,
        }
        return grid, tri_verts, tris, meta

    def _collider_l0_mask(self, mod_export_path, sections, comp):
        """读已烘焙的稀疏权重，返回「任一 zone 权重 > 1e-4」的顶点布尔掩码（L0 只烘焙这些顶点）。"""
        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        path = os.path.join(out_dir, f"{comp['base_name']}JiggleZoneWeights.buf")
        if not os.path.isfile(path):
            return None
        try:
            w = np.fromfile(path, dtype=np.float32).reshape(-1, SPARSE_ZONE_SLOTS)
            return np.any(w > 1e-4, axis=1)
        except Exception:
            return None

    def _collider_zone_ids(self):
        """挂了碰撞体的区域 ID 集合（与 _collect_collider_objects 同一套过滤规则）。

        节点级 collision_enabled 关闭 ⇒ 空集；区域级 override==2（强制关）跳过、
        override==0/1 且 collider_objects 里确实解析出物体名才纳入。
        """
        if not getattr(self, "collision_enabled", False):
            return set()
        ids = set()
        for zone_id, empty in self._collect_enabled_zone_entries():
            settings = empty.ssmt_drag_zone
            try:
                override = int(getattr(settings, "collision_override", 0) or 0)
            except (TypeError, ValueError):
                override = 0
            if override == 2 or not _collider_allowed_names(settings):
                continue
            ids.add(int(zone_id))
        return ids

    def _collision_zone_participation(self, mod_export_path, sections, comp,
                                      source_vertex_count, runtime_vertex_count, vertex_base):
        """(source_vertex_count,) bool：顶点是否被「挂了碰撞体的区域」影响。

        _collect_collider_objects 是节点级聚合（任一区域的碰撞体 ⇒ 整组件烘一份网格），
        所以「没挂碰撞体的区域」也会被同一张网格约束。这里读已烘的稀疏区域权重
        （_write_jiggle_masks 先于本函数执行）把参与位收窄回区域级：只有自己的区域
        带碰撞体的顶点，L0 的 valid 位才为 1，运行时 CollisionSolve 才会约束它。
        权重文件缺失/长度对不上 ⇒ None（退回旧的全量参与行为，不阻断导出）。
        """
        zone_ids = self._collider_zone_ids()
        if not zone_ids:
            return np.zeros(int(source_vertex_count), dtype=bool)
        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        stem = comp["base_name"]
        ids_path = os.path.join(out_dir, f"{stem}JiggleZoneIDs.buf")
        w_path = os.path.join(out_dir, f"{stem}JiggleZoneWeights.buf")
        if not (os.path.isfile(ids_path) and os.path.isfile(w_path)):
            return None
        try:
            ids = np.fromfile(ids_path, dtype=np.uint32).reshape(-1, SPARSE_ZONE_SLOTS)
            weights = np.fromfile(w_path, dtype=np.float32).reshape(-1, SPARSE_ZONE_SLOTS)
        except Exception:
            return None
        if len(ids) == int(runtime_vertex_count):
            lo = int(vertex_base)
            hi = lo + int(source_vertex_count)
            ids = ids[lo:hi]
            weights = weights[lo:hi]
        if len(ids) != int(source_vertex_count) or len(weights) != len(ids):
            return None
        owned = np.zeros(ids.shape, dtype=bool)
        for zone_id in zone_ids:
            owned |= (ids == np.uint32(zone_id))
        return np.any(owned & (weights > 1e-4), axis=1)

    @staticmethod
    def _collider_shape(points):
        """碰撞体在 vb0 local 绑定空间的圆柱近似（PCA 主轴 + 轴向范围 + 最大径向半径）。

        用于运行时「远侧方向性约束」（rzm_jiggle_interaction.hlsl::ClampFarSideBarrier）。
        只对细长碰撞体（主轴特征值明显大于次轴，例如圆柱/大腿）返回；球/方块等
        各向同性碰撞体返回 None ⇒ 不启用方向性约束，行为退回原碰撞求解。

        返回 dict 或 None。中心 = 顶点均值；轴 = 最大特征值对应的单位特征向量；
        axis_min/axis_max = 顶点在轴上的投影范围；radius = 到轴的最大径向距离。
        """
        pts = np.asarray(points, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] < 8 or pts.shape[1] != 3:
            return None
        center = pts.mean(axis=0)
        delta = pts - center
        cov = (delta.T @ delta) / max(len(pts) - 1, 1)
        try:
            vals, vecs = np.linalg.eigh(cov)
        except np.linalg.LinAlgError:
            return None
        order = np.argsort(vals)
        major = float(vals[order[2]])
        minor = float(vals[order[1]])
        if major <= 1e-12 or major < 1.25 * max(minor, 1e-12):
            return None
        axis = np.asarray(vecs[:, order[2]], dtype=np.float64)
        n = float(np.linalg.norm(axis))
        if n <= 1e-9:
            return None
        axis = axis / n
        proj = delta @ axis
        radial = delta - proj[:, None] * axis
        return {
            "center": center,
            "axis": axis,
            "axis_min": float(proj.min()),
            "axis_max": float(proj.max()),
            "radius": float(np.linalg.norm(radial, axis=1).max()),
        }

    def _write_collision_resources(self, mod_export_path, sections, comp, ns):
        """烘焙碰撞检测资源：ColliderPoints / ColliderVertexL0。

        返回 True = 已写出；False = 无可烘焙内容（碰撞体选择为空/不在本组件）。
        """
        vertex_count = comp["vertex_count"]
        positions = self._read_position_buf(mod_export_path, sections, comp, vertex_count)
        if positions is None:
            print(f"[DragInteraction][WARNING] 碰撞烘焙：{comp['comp_name']} 无法读取 Position.buf，跳过")
            return False
        # 顶点法线只用于给碰撞体三角形法线定向：绑定姿态下丝袜与大腿几乎重合
        # （实测间隙 0.0001），靠几何自身分不出「哪一侧是外」，只能靠法线。
        vertex_normals = self._read_vertex_normals(
            mod_export_path, sections, comp
        )
        source_vertex_count = len(positions)
        vertex_base, vertex_count = self._runtime_vertex_layout(
            comp, source_vertex_count
        )

        result = self._read_component_triangles(
            mod_export_path, sections, comp, source_vertex_count
        )
        if result is None:
            print(f"[DragInteraction][WARNING] 碰撞烘焙：{comp['comp_name']} 无法读取 IB 拓扑，跳过")
            return False
        triangles, tri_part_names = result

        collider_mask = np.zeros(source_vertex_count, dtype=bool)
        collision_zones = self._collider_zone_ids()
        for zone_id, empty in self._collect_enabled_zone_entries():
            if int(zone_id) not in collision_zones:
                continue
            m = _collider_vertex_mask(
                empty.ssmt_drag_zone,
                source_vertex_count,
                triangles,
                tri_part_names,
            )
            if m is not None:
                collider_mask |= m
        if not np.any(collider_mask):
            print(
                f"[DragInteraction][WARNING] 碰撞烘焙：{comp['comp_name']} 碰撞体对象未命中本组件任何顶点，"
                "已跳过（请确认碰撞体属于当前 IB）"
            )
            return False

        # 【区域级隔离】节点级碰撞体聚合会让「没挂碰撞体的区域」也被同一份网格约束。
        # 先按区域权重算出参与位：本组件没有任何顶点被带碰撞体的区域影响 ⇒ 不做碰撞
        # （返回 False ⇒ 该组件走 x101 = 0 的 else 分支，既不绑 cs-t* 也不钉回弹）。
        participation = self._collision_zone_participation(
            mod_export_path, sections, comp,
            source_vertex_count, vertex_count, vertex_base,
        )
        if participation is not None and not np.any(participation):
            print(
                f"[DragInteraction][WARNING] 碰撞烘焙：{comp['comp_name']} 的启用区域里没有任何一个"
                "配了碰撞体，本组件不做碰撞约束（无碰撞体的区域可自由拖拽）"
            )
            return False

        normals = self._collider_vertex_normals(
            positions, triangles, collider_mask, source_vertex_count
        )
        collider_idx = np.flatnonzero(collider_mask)
        points = np.asarray(positions, dtype=np.float32)[collider_idx]
        point_normals = normals[collider_idx]
        # 远侧方向性约束用的圆柱近似：必须在面内采样/抽稀之前，用原始碰撞体顶点算。
        comp["collision_shape"] = self._collider_shape(points)

        # 面内采样：纯顶点采样在面内留下最近邻距 0.028 量级的空洞（远大于
        # 丝袜-大腿 0.001 的真实间隙），单点切平面/球约束跟不上弧面曲率。
        # 【用户 m00742】要求「把面中心的优先级提高」：预算不再对半分，全部给面内
        # 面积加权采样；顶点采样仍先加入（保住角点/边特征），随后与面内点一起按体积
        # 抽稀到 budget —— 面内点因数量占绝对多数而保留大头，等价于把面中心排到顶点
        # 之前。旧行为是 budget // 2（面内点实际只占约一半）。
        budget = int(getattr(self, "collision_point_budget", 4096) or 0)
        if budget > 0:
            face_budget = max(budget, 1)
            surf_points, _surf_tri, surf_normals = self._sample_collider_surface(
                positions, triangles, collider_mask, face_budget
            )
            if len(surf_points):
                points = np.concatenate([points, surf_points], axis=0)
                point_normals = np.concatenate([point_normals, surf_normals], axis=0)

        if budget > 0 and len(points) > budget:
            points, point_normals = self._decimate_collider_points(points, point_normals, budget)

        margin = float(getattr(self, "collision_margin", 0.002) or 0.0)
        cell_size = float(getattr(self, "collision_cell_size", 0.0) or 0.0)
        grid = self._build_collider_grid(points, cell_size, margin)

        masked = self._collider_l0_mask(mod_export_path, sections, comp)
        if masked is not None and len(masked) == vertex_count:
            masked = masked[vertex_base:vertex_base + source_vertex_count]
        elif masked is not None and len(masked) != source_vertex_count:
            print(
                f"[DragInteraction][WARNING] {comp['comp_name']} 的 JiggleZoneWeights "
                f"长度 {len(masked)} 无法与 Position {source_vertex_count} / 运行时 "
                f"{vertex_count} 对齐，碰撞 L0 将按全部基础顶点烘焙"
            )
            masked = None
        # 【区域级隔离】只把参与位为真的顶点写成有效行（l0 valid = 1）；
        # 无碰撞体区域的顶点保持 valid = 0 ⇒ CollisionSolve 的首行判据直接放行。
        if participation is not None:
            masked = participation if masked is None else (masked & participation)
        # 点云交错布局：每点 2×float4（位置 + 单位法线），法线按网格 count-sort 同一 order 重排
        reordered_normals = np.asarray(point_normals, dtype=np.float32)[grid["order"]]
        # L0 阈值必须用「到碰撞体三角形表面的真实最近点」算，不能用「到最近采样点的
        # 切平面投影」：采样抽稀到 budget 后点距（~0.009）远大于丝袜-大腿真实间隙
        # （0.001），P-q0 与 q0 处法线几乎垂直 ⇒ dot(P-q0, n0) 塌成 0.0007 量级、
        # 且 40% 顶点符号为负，运行时阈值因此形同虚设。
        l0 = self._collider_contact_exact(positions, triangles, collider_mask, masked,
                                          vertex_normals=vertex_normals)
        if vertex_base or vertex_count != source_vertex_count:
            runtime_l0 = np.zeros((vertex_count * 2, 4), dtype=np.float32)
            start = vertex_base * 2
            runtime_l0[start:start + len(l0)] = l0
            l0 = runtime_l0

        out_dir = os.path.join(mod_export_path, self._buffer_dir(sections, comp))
        os.makedirs(out_dir, exist_ok=True)
        stem = comp["base_name"]
        interleaved = np.zeros((len(grid["sorted_points"]) * 2, 4), dtype=np.float32)
        interleaved[0::2, 0:3] = grid["sorted_points"]
        interleaved[1::2, 0:3] = reordered_normals
        interleaved.tofile(os.path.join(out_dir, f"{stem}ColliderPoints.buf"))
        l0.tofile(os.path.join(out_dir, f"{stem}ColliderVertexL0.buf"))

        # 【t42】导出侧接触覆盖自检（只打印，不写任何 .buf —— 见 t42 裁定：
        #   「受控行必须有接触数据」是运行时的隐含不变量，必须显式化）。
        # 运行时把 `|t0| == 0` 当作「该行没有锚点」的唯一判据（SockHasAnchor：
        # 滑移与权重投影都会跳过），而 L0 只对 zone 权重 > 1e-4 的行烘焙 ⇒
        # 「受控却没有接触」的行会被静默冻结在绑定姿态（既不报错也不生效）。
        # 当前数据 100% 满足该不变量；一旦违反就在这里点名行号。
        l0_pairs = np.asarray(l0, dtype=np.float64).reshape(-1, 2, 4)
        has_contact = np.abs(l0_pairs[:, 0, 3]) > 0.0
        if masked is None:
            print(f"[DragInteraction] 接触覆盖: 未提供 zone 掩码 ⇒ 全量烘焙 "
                  f"{int(has_contact.sum())}/{len(has_contact)} 行")
        else:
            controlled_idx = np.flatnonzero(np.asarray(masked, dtype=bool)) + int(vertex_base)
            controlled_idx = controlled_idx[controlled_idx < len(has_contact)]
            miss = controlled_idx[~has_contact[controlled_idx]]
            print(f"[DragInteraction] 接触覆盖: 受控 {len(controlled_idx)} 行 / 有接触 "
                  f"{int(has_contact[controlled_idx].sum())} 行 / 缺接触 {len(miss)} 行")
            if len(miss):
                print(f"[DragInteraction][INFO] 受控但无接触数据 {len(miss)} 行: "
                      f"{miss[:32].tolist()}{' ...' if len(miss) > 32 else ''}"
                      "（整面碰撞仍按受控面覆盖；仅旧版绑定滑移缺少这些点锚）")

        # 三角形约束表（首选路径）：把每个受碰撞约束的顶点绑定到绑定姿态下最近的
        # K 个碰撞体三角形。运行时零搜索，约束 = dist(p, tri_j) >= base_j。
        # Keep the stored property for old blueprints, but never silently replace
        # the validated whole-face solver with point constraints when it is zero.
        tri_k = max(1, int(getattr(self, "collision_tri_count", 8) or 0))

        # 动态三角形网格（当前首选路径）：把碰撞体三角形装进均匀网格，运行时按
        # 顶点当前位置现查邻域三角形，约束 =「到表面的带符号距离不许变小」。
        # 静态 K 个三角形只覆盖绑定姿态附近一小片曲面，位移一大顶点就滑出去，
        # 约束完全失效（实测 235 顶点 × 12 方向 × 4 长度 = 11280 案例：静态 K=8
        # 穿模 693、最大穿深 0.175）。网格查询对任意位移都成立。
        grid_baked = None
        if tri_k > 0:
            grid_baked = self._bake_collider_tri_grid(
                positions, triangles, collider_mask, vertex_normals=vertex_normals,
            )
        # 注意：`_bake_collider_tri_grid` **不会**返回 None——没有完整三角形时它返回
        # 四元组空数组（见该函数 :3528-3535），所以这里不能写 `if grid_baked is None: raise`：
        # 那条守卫永远不会成立，只会让后来的人以为这条路被守住了。
        # 真正的分支是「空网格」：下面 `if leaves and len(cloth)` 不成立 ⇒ 不发
        # collision_surface（x115=0），退回旧点云求解。
        if grid_baked is not None:
            g_arr, g_tris, g_vidx, g_meta = grid_baked
            g_arr.tofile(os.path.join(out_dir, f"{stem}ColliderTriGrid.buf"))
            g_tris.tofile(os.path.join(out_dir, f"{stem}ColliderTriGridTris.buf"))
            # 网格三角形 → 顶点缓冲索引（每个三角形 3 个 uint32，与 Tris 逐条对应）：
            # 权重重映射要用它反查三角形三个顶点在 Blend 里的权重
            (g_vidx + vertex_base).astype(np.uint32).tofile(
                os.path.join(out_dir, f"{stem}ColliderTriGridVerts.buf"))
            order, leaves = bake_sweep_order(g_tris)
            # Whole-face coverage follows controlled geometry, not successful
            # bind-pose point contacts: a loose layer must still be stopped when
            # it is dragged toward the body later.
            controlled = np.ones(source_vertex_count, dtype=bool) if masked is None else np.asarray(masked, dtype=bool)
            contact_rows = np.zeros(vertex_count, dtype=bool)
            contact_rows[vertex_base:vertex_base + source_vertex_count] = controlled
            cloth, adjacency = bake_cloth_adjacency(
                np.asarray(triangles, dtype=np.int64) + vertex_base, contact_rows
            )
            if leaves and len(cloth):
                order.tofile(os.path.join(out_dir, f"{stem}ColliderSweepOrder.buf"))
                cloth.tofile(os.path.join(out_dir, f"{stem}ColliderCloth.buf"))
                adjacency.tofile(os.path.join(out_dir, f"{stem}ColliderAdjacency.buf"))
                comp["collision_surface"] = {"leaves": leaves, "face_count": len(cloth)}

        print(
            f"[DragInteraction] Collision: {comp['comp_name']} "
            f"({len(collider_idx)} 碰撞体顶点 → {len(grid['sorted_points'])} 点, "
            f"细格 {tuple(int(d) for d in grid['dims'])}, h={float(grid['h']):.4f})"
        )
        # 网格参数回写 comp，供 ini 发射（x102/x103/x104）使用
        comp["collision_grid"] = {
            "bmin": grid["bmin"],
            "h": float(grid["h"]),
            "h_c": float(grid["h_c"]),
            "dims": tuple(int(d) for d in grid["dims"]),
            "cdims": tuple(int(d) for d in grid["cdims"]),
        }
        if grid_baked is not None:
            _g_arr, _g_tris, _g_vidx, g_meta = grid_baked
            comp["collision_trigrid"] = {
                "bmin": tuple(float(x) for x in g_meta["bmin"]),
                "cell": float(g_meta["cell"]),
                "dims": tuple(int(d) for d in g_meta["dims"]),
                "cell_count": int(g_meta["cell_count"]),
            }
        if vertex_base:
            blend_dir = self._buffer_dir(sections, comp)
            blend_path = os.path.join(mod_export_path, blend_dir, f"{comp['base_name']}-Blend.buf")
            if os.path.isfile(blend_path):
                blend = np.fromfile(blend_path, dtype=np.uint8)
                if blend.size != source_vertex_count * 32:
                    raise ValueError("碰撞滑移权重需要每顶点32字节的Blend布局")
                padded = np.zeros((vertex_count, 32), dtype=np.uint8)
                padded[vertex_base:vertex_base + source_vertex_count] = blend.reshape(-1, 32)
                filename = f"{comp['base_name']}ColliderBlend.buf"
                padded.tofile(os.path.join(mod_export_path, blend_dir, filename))
                comp["collision_blend_file"] = f"{blend_dir}/{filename}"
        return True

    def _read_position_buf(self, mod_export_path, sections, comp, vertex_count):
        """从 ini 的 Position 资源段 filename 解析路径读取顶点坐标（float3×N）。"""
        res = comp["base_resource"]
        lines = sections.get(f"[{res}]", [])
        rel_path = None
        resource_stride = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("filename ="):
                rel_path = line.split("=", 1)[1].strip()
            elif stripped.startswith("stride ="):
                try:
                    resource_stride = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
        if not rel_path:
            print(f"[DragInteraction][WARNING] [{res}] 无 filename 行，无法读取 Position.buf")
            return None
        full_path = os.path.join(mod_export_path, rel_path.replace('\\', os.sep).replace('/', os.sep))
        if not os.path.exists(full_path):
            print(f"[DragInteraction][WARNING] Position.buf 不存在: {full_path}")
            return None
        try:
            data = np.fromfile(full_path, dtype=np.uint8)
            struct_stride, position_offset, position_type = self._get_position_layout()
            stride = resource_stride or struct_stride
            type_match = re.fullmatch(r'(float|half|double)([1-4]?)', position_type)
            if not type_match:
                print(f"[DragInteraction][WARNING] 不支持的 position 类型: {position_type}")
                return None
            component_count = int(type_match.group(2) or '1')
            if component_count < 3:
                print(f"[DragInteraction][WARNING] position 分量不足 3 个: {position_type}")
                return None
            scalar_type = {
                'float': np.float32,
                'half': np.float16,
                'double': np.float64,
            }[type_match.group(1)]
            attribute_size = np.dtype(scalar_type).itemsize * component_count
            if position_offset + attribute_size > stride:
                print(
                    f"[DragInteraction][WARNING] position 布局越界: "
                    f"offset={position_offset}, size={attribute_size}, stride={stride}"
                )
                return None
            n = len(data) // stride
            if vertex_count and n != vertex_count:
                # 合并骨架上 `override_vertex_count` 是合并 SO 总行数（含被吸收件的 3 行前缀），
                # 而 Position.buf 只有本件的导出行数 ⇒ 差 3 行必然出现且良性，降级 INFO。
                # 勿删：以 buf 为准的兜底与下面的补前缀逻辑都依赖这条判断。
                _level, _suffix = self._diagnostic_level(
                    sections,
                    merged_skeleton_suffix="（合并骨架场景下必然出现且良性：VLR=合并 SO 总行数）",
                )
                print(
                    f"[DragInteraction][{_level}] Position.buf 顶点数 {n} 与 VLR {vertex_count} "
                    f"不一致，以 buf 为准{_suffix}"
                )
            arr = data[:n * stride].reshape(n, stride)
            attribute_bytes = arr[:, position_offset:position_offset + attribute_size].copy()
            values = attribute_bytes.view(scalar_type).reshape(n, component_count)
            return values[:, :3].astype(np.float32, copy=False)
        except Exception as e:
            print(f"[DragInteraction][WARNING] 读取 Position.buf 失败: {e}")
            return None

    def _read_vertex_normals(self, mod_export_path, sections, comp):
        """从 ini 的 Position 资源段读取顶点法线（顶点结构里的 normal 属性）。

        与 `_read_position_buf` 读同一份 buf、同一 stride，只换属性偏移。
        碰撞约束需要它来给碰撞体三角形法线定向：丝袜与大腿在绑定姿态下几乎重合
        （实测间隙 0.0001），靠几何自身无法判断「哪一侧是外」，而顶点法线朝外是
        可靠的（实测与最近丝袜三角形面法线 dot 全为正，min 0.885）。
        返回 (n, 3) float32；结构里没有 normal 属性或读失败返回 None。
        """
        res = comp.get("base_resource")
        if not res:
            return None
        lines = sections.get(f"[{res}]", [])
        rel_path = None
        resource_stride = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("filename ="):
                rel_path = line.split("=", 1)[1].strip()
            elif stripped.startswith("stride ="):
                try:
                    resource_stride = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
        if not rel_path:
            return None
        full_path = os.path.join(mod_export_path, rel_path.replace('\\', os.sep).replace('/', os.sep))
        if not os.path.exists(full_path):
            return None
        try:
            struct_stride, normal_offset, normal_type = self._get_vertex_attribute_layout('normal')
            if normal_offset is None or normal_type is None:
                print("[DragInteraction][INFO] 顶点结构里没有 normal 属性，碰撞约束退回几何定向")
                return None
            stride = resource_stride or struct_stride
            type_match = re.fullmatch(r'(float|half|double)([1-4]?)', normal_type)
            if not type_match:
                return None
            component_count = int(type_match.group(2) or '1')
            if component_count < 3:
                return None
            scalar_type = {
                'float': np.float32,
                'half': np.float16,
                'double': np.float64,
            }[type_match.group(1)]
            attribute_size = np.dtype(scalar_type).itemsize * component_count
            if normal_offset + attribute_size > stride:
                return None
            data = np.fromfile(full_path, dtype=np.uint8)
            n = len(data) // stride
            arr = data[:n * stride].reshape(n, stride)
            attribute_bytes = arr[:, normal_offset:normal_offset + attribute_size].copy()
            values = attribute_bytes.view(scalar_type).reshape(n, component_count)
            return values[:, :3].astype(np.float32, copy=False)
        except Exception as e:
            print(f"[DragInteraction][WARNING] 读取顶点法线失败: {e}")
            return None

    def _get_reference_matrix_inv(self, comp):
        """烘焙参考物体世界矩阵的逆；留空自动解析（OffsetToVertexSpace 恒等时用单位阵）。"""
        ref = self.bake_reference_object
        if ref is not None:
            return ref.matrix_world.inverted()
        return None  # None = 单位变换

    @staticmethod
    def _get_export_space_matrix(logic_name=None):
        if logic_name is None:
            try:
                from ..common.global_config import GlobalConfig
                logic_name = GlobalConfig.logic_name
            except Exception:
                logic_name = ""
        return position_export_matrix(logic_name)

    def _get_non_mirror_mirror(self):
        """非镜像工作流补偿矩阵：检测场景网格是否带 _ssmt_non_mirror_workflow_processed 标记
        （导入时被 X 镜像处理）。命中返回 X 镜像矩阵；否则返回 None。

        优先用烘焙参考物体判定（用户显式指定时最可靠）；参考物体非网格/无标记时
        退回扫描场景全部网格。
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
        if ref is not None and ref.type == 'MESH' and _marked(ref):
            print("[DragInteraction] 非镜像工作流：区域空物体矩阵已施加 X 镜像补偿（参考物体标记）")
            return mirror
        try:
            objects = bpy.data.objects
        except Exception:
            objects = None
        if objects is not None:
            for obj in objects:
                if obj.type == 'MESH' and _marked(obj):
                    print("[DragInteraction] 非镜像工作流：区域空物体矩阵已施加 X 镜像补偿")
                    return mirror
        return None

    def _evaluate_zone_field(
        self,
        positions,
        empty,
        settings,
        ref_matrix_inv,
        mirror=None,
        edge_verts=None,
        export_matrix=None,
        propagate=None,
        allowed_mask=None,
    ):
        """对单个区域空物体求权重场（0..1），含 bbox sanity check。

        mirror: 非镜像工作流 X 镜像矩阵（(4,4) numpy）或 None。矩阵运算统一走
        numpy（mathutils.Matrix 可被 np.asarray 转换），便于无 Blender 环境测试。
        edge_verts: 网格边拓扑 (E,2)；本球沿表面扩散开启且提供时走沿表面传播
        （测地距离，权重不穿透到球体积覆盖的背面/对侧），否则回退体积球欧氏距离。
        propagate: 本球是否沿表面扩散；None = 默认开启（球级开关移除后无总开关）。
        allowed_mask: (N,) bool 可选，仅对这些顶点施加权重（包含物体列表过滤）。
        """
        try:
            if propagate is None:
                # 直接调用（测试/内部路径）未显式指定时，跟随球级沿表面扩散开关
                propagate = _zone_propagate(settings, self)
            # 空物体世界坐标 → 模组局部空间（必要时先施加非镜像工作流补偿）
            ball_world = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
            if mirror is not None:
                ball_world = np.asarray(mirror, dtype=np.float64).reshape(4, 4) @ ball_world
            if ref_matrix_inv is not None:
                ball_matrix = np.asarray(ref_matrix_inv, dtype=np.float64).reshape(4, 4) @ ball_world
            else:
                ball_matrix = ball_world
            if export_matrix is not None:
                ball_matrix = np.asarray(export_matrix, dtype=np.float64).reshape(4, 4) @ ball_matrix

            d = self._zone_distances(
                positions,
                ball_matrix,
                edge_verts,
                propagate=propagate,
                allowed_mask=allowed_mask,
            )
            if d is None:
                return None
            field = np.asarray(
                self._shape_field(d, settings.brush_strength, settings.brush_falloff_k, self.mask_plateau),
                dtype=np.float32,
            )

            # bbox sanity check：影响球与顶点包围盒需有交集
            if float(field.max(initial=0.0)) < 1e-4:
                center = ball_matrix[:3, 3]
                print(
                    f"[DragInteraction][WARNING] 区域空物体 {empty.name} 的影响球与顶点包围盒无交集 "
                    f"(中心 {tuple(round(c,3) for c in center)})，坐标系可能错位"
                )
                return None
            return field
        except Exception as e:
            print(f"[DragInteraction][WARNING] 区域 {empty.name} 求场失败: {e}")
            return None

    def _zone_distances(self, positions, ball_matrix, edge_verts, propagate=None, allowed_mask=None):
        """球局部距离数组：本球沿表面扩散开启且有拓扑时用沿表面传播距离
        （种子 = 离球心最近的表面顶点/接触点），否则用欧氏距离。矩阵不可逆返回 None。"""
        local = gb_core._to_ball_local(np.asarray(positions, dtype=np.float64), ball_matrix)
        if local is None:
            return None
        d2 = np.einsum("ij,ij->i", local, local)
        use_propagate = True if propagate is None else bool(propagate)
        if use_propagate and edge_verts is not None and len(edge_verts) > 0:
            seeds = np.zeros(local.shape[0], dtype=bool)
            if allowed_mask is None:
                seeds[int(np.argmin(d2))] = True
            else:
                allowed = np.asarray(allowed_mask, dtype=bool).reshape(-1)
                valid_d2 = np.where(allowed, d2, np.inf)
                if not np.any(np.isfinite(valid_d2)):
                    return np.full(local.shape[0], np.inf)
                seeds[int(np.argmin(valid_d2))] = True
            return gb_core.surface_distances(
                local, edge_verts, seeds, allowed_mask=allowed_mask
            )
        if allowed_mask is not None:
            d_euclid = np.sqrt(np.maximum(d2, 0.0))
            d_euclid[~np.asarray(allowed_mask, dtype=bool).reshape(-1)] = np.inf
            return d_euclid
        return np.sqrt(np.maximum(d2, 0.0))

    def _shape_field(self, d, strength, falloff_k, plateau):
        """统一衰减形状：plateau>0 平台化（d≤平台满强度、边缘平滑过渡），否则
        高斯 exp(-k·d²)；d≥1 或不可达（inf）硬截止为 0。欧氏/测地距离共用。"""
        d = np.asarray(d, dtype=np.float64)
        if plateau is not None and float(plateau) > 0.0:
            edge = float(plateau)
            t = np.clip((d - edge) / max(1.0 - edge, 1e-6), 0.0, 1.0)
            s = t * t * (3.0 - 2.0 * t)
            field = float(strength) * (1.0 - s)
        else:
            field = float(strength) * np.exp(-float(falloff_k) * d * d)
        field[d >= 1.0] = 0.0
        field[~np.isfinite(d)] = 0.0
        return field

    def _plateau_field(self, positions, ball_matrix, strength, falloff_k, plateau):
        """平台化权重场（欧氏距离版）：d ≤ plateau 保持满强度（平台），
        plateau < d < 1 平滑降到 0，d ≥ 1 为 0。plateau <= 0 时退化为原始高斯
        （向后兼容；沿表面传播路径与体积球路径共用 _shape_field 形状）。"""
        local = gb_core._to_ball_local(np.asarray(positions, dtype=np.float64), ball_matrix)
        if local is None:
            return np.zeros(len(positions), dtype=np.float64)
        d2 = np.einsum("ij,ij->i", local, local)
        d = np.sqrt(np.maximum(d2, 0.0))
        return self._shape_field(d, strength, falloff_k, plateau)

    def _read_component_triangles(self, mod_export_path, sections, comp, vertex_count):
        """从组件各 part 的 IB 资源段 filename 读 R32_UINT 索引 → (M,3) 三角形，
        并返回与三角形逐行对齐的部件 mesh 名（object 数组，无注释为 None）；
        读不到返回 None（调用方回退体积球）。"""
        tris = []
        name_arrays = []
        for part in comp["parts"]:
            res = part.get("ib_resource")
            lines = sections.get(f"[{res}]", [])
            rel = None
            for line in lines:
                if line.strip().startswith("filename ="):
                    rel = line.split("=", 1)[1].strip()
                    break
            if not rel:
                continue
            full = os.path.join(mod_export_path, rel.replace("\\", os.sep).replace("/", os.sep))
            if not os.path.exists(full):
                continue
            try:
                idx = np.fromfile(full, dtype=np.uint32)
                t_part = idx[: (len(idx) // 3) * 3].reshape(-1, 3)
                tris.append(t_part)
                # 按 draw 区间标注每个三角形所属物体；未标注的三角形按 None
                # 处理（无法判定归属，不因包含物体列表而清零）。区间重叠时先到者优先。
                names = np.full(len(t_part), None, dtype=object)
                for offset, count, name in part.get("name_ranges", []):
                    start = int(offset) // 3
                    end = (int(offset) + int(count)) // 3
                    start = max(0, min(start, len(names)))
                    end = max(0, min(end, len(names)))
                    if start >= end:
                        continue
                    fill = np.zeros(len(names), dtype=bool)
                    fill[start:end] = True
                    fill &= names == None  # noqa: E711
                    names[fill] = name
                name_arrays.append(names)
            except Exception:
                continue
        if not tris:
            return None
        t = np.concatenate(tris, axis=0)
        tri_part_names = np.concatenate(name_arrays, axis=0)
        if vertex_count:
            keep = (t < int(vertex_count)).all(axis=1)
            t = t[keep]
            tri_part_names = tri_part_names[keep]
        return (t, tri_part_names) if len(t) else None

    def _check_zone_radius_scale(self, zones):
        """影响半径与球尺度失配检查（导出时警告）。

        RubberInfluence(dist, radius) 必须在区域内显著衰减，鼠标命中点才能成为
        变形峰（“点哪拖哪”）；radius 远大于球半径时 R 在球内几乎平坦，整块近似
        刚体平移，视觉上像“抓住权重中心整块拖走”。原版 LEWDHAND 实测各区域
        radius ≈ 区域空间宽度的 0.3~2.2 倍；这里用保守阈值 2.5× 打警告。

        返回警告条数（便于单测断言）。
        """
        warned = 0
        for empty in zones:
            s = empty.ssmt_drag_zone
            zone_radius = float(s.radius) if s.radius > 0 else 0.25  # 0 = 继承回退档案
            m = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
            # 球世界半径 = 空物体缩放（gaussian_field 球局部 d<1）
            scale = float(np.mean(np.linalg.norm(m[:3, :3], axis=0)))
            if scale > 1e-6 and zone_radius > scale * 2.5:
                warned += 1
                print(
                    f"[DragInteraction][WARNING] 区域 {empty.name} 影响半径 {zone_radius:.3f} "
                    f"是球半径 {scale:.3f} 的 {zone_radius/scale:.1f} 倍 → 衰减在球内几乎平坦，"
                    f"变形会整块刚体动（看起来像抓住权重中心拖）。"
                    f"建议把该区域影响半径调到 {scale*1.0:.3f}~{scale*2.5:.3f}"
                )
        return warned

    def _collect_enabled_zone_entries(self):
        """返回 (稳定 zone_id, Empty)；为旧工程和重复 ID 自动分配空闲编号。"""
        entries = []
        used_ids = set()
        next_candidate = 0
        for item in self.zone_objects:
            obj = item.zone_object
            if obj is None:
                print("[DragInteraction][WARNING] 区域空物体已被删除，跳过")
                continue

            try:
                zone_id = int(getattr(item, "zone_id", -1))
            except (TypeError, ValueError):
                zone_id = -1
            if not (0 <= zone_id < MAX_ZONES) or zone_id in used_ids:
                while next_candidate in used_ids and next_candidate < MAX_ZONES:
                    next_candidate += 1
                if next_candidate >= MAX_ZONES:
                    print(f"[DragInteraction][WARNING] 区域超过稳定 ID 上限 {MAX_ZONES}，其余区域已跳过")
                    break
                zone_id = next_candidate
                try:
                    item.zone_id = zone_id
                except Exception:
                    pass
            used_ids.add(zone_id)
            while next_candidate in used_ids and next_candidate < MAX_ZONES:
                next_candidate += 1
            if obj.ssmt_drag_zone.enabled:
                entries.append((zone_id, obj))
        return sorted(entries, key=lambda entry: entry[0])

    def _collect_enabled_zones(self):
        """兼容预览/参数检查调用方；顺序按稳定区域 ID。"""
        return [obj for _, obj in self._collect_enabled_zone_entries()]

    @staticmethod
    def _zone_capacity(entries):
        return max(1, max((zone_id for zone_id, _ in entries), default=0) + 1)

    def _collect_collider_objects(self):
        """返回所有启用区域的碰撞体对象引用列表（考虑 zone 级 collision_override）。

        规则：节点级 collision_enabled 关闭 → 空；否则遍历启用区域，zone 级
        override==2（强制关）跳过、override==1（强制开）或 override==0（继承）均纳入，
        收集其 collider_objects 中有效且去重的 MESH 对象。
        """
        if not getattr(self, "collision_enabled", False):
            return []
        colliders = []
        seen = set()
        for _, empty in self._collect_enabled_zone_entries():
            settings = empty.ssmt_drag_zone
            try:
                override = int(getattr(settings, "collision_override", 0) or 0)
            except (TypeError, ValueError):
                override = 0
            if override == 2:
                continue
            for item in getattr(settings, "collider_objects", None) or ():
                obj = getattr(item, "object", None)
                if obj is None or getattr(obj, "type", None) != 'MESH':
                    continue
                key = getattr(obj, "name", None) or id(obj)
                if key in seen:
                    continue
                seen.add(key)
                colliders.append(obj)
        return colliders

    def _collision_enabled_for_component(self):
        """组件级碰撞烘焙/发射开关：节点级开启且至少一个区域配置了碰撞体。"""
        return len(self._collect_collider_objects()) > 0

    def _cloth_enabled(self):
        return bool(getattr(self, 'cloth_slide_enabled', False))

    def _cloth_object_names(self, required=False):
        cloth, body = set(), set()
        for _, empty in self._collect_enabled_zone_entries():
            settings = empty.ssmt_drag_zone
            included = _zone_allowed_names(settings)
            colliders = _collider_allowed_names(settings)
            if included and colliders:
                cloth.update(_blender_object_key(name) for name in included)
                body.update(_blender_object_key(name) for name in colliders)
        for attr, result in (('cloth_collection', cloth), ('cloth_collider_collection', body)):
            collection = getattr(self, attr, None)
            if collection is not None:
                result.clear()
                for obj in getattr(collection, 'all_objects', ()):
                    if getattr(obj, 'type', None) == 'MESH':
                        result.add(_blender_object_key(obj.name))
        if required and (not cloth or not body):
            raise ValueError("上下拖拽：请明确选择衣物和身体碰撞体（可用专用集合，或区域的包含物体/碰撞体列表）")
        return cloth, body

    def _collect_click_export_drivers(self):
        """扫描同主树「动画驱动蓝图」关联树中的点击计数导出节点，
        返回 [(zone_id, 循环档数, 首个受控变量名或"")]：
        供缓冲布局把导出区域与循环档数纳入容量/档位计算，并供冷启动播种读取。"""
        entries = []
        tree = getattr(self, "id_data", None)
        if tree is None:
            return entries
        for node in getattr(tree, "nodes", None) or []:
            if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_AnimDriver":
                continue
            if getattr(node, "mute", False):
                continue
            if not is_postprocess_node_on_export_chain(tree, node):
                continue
            anim_tree = bpy.data.node_groups.get(str(getattr(node, "blueprint_name", "") or ""))
            if anim_tree is None:
                continue
            for anim_node in getattr(anim_tree, "nodes", None) or []:
                if getattr(anim_node, "bl_idname", "") != "SSMTNode_AnimDriver_ClickExport":
                    continue
                if getattr(anim_node, "mute", False):
                    continue
                try:
                    zone = int(getattr(anim_node, "click_zone_id", -1))
                except Exception:
                    zone = -1
                # 有效循环档数（跨节点只读契约）：节点配置了「开关值」时列表长度即
                # 该区域的循环，否则回退循环档数属性（旧节点/测试桩无此方法）
                cycle_fn = getattr(anim_node, "effective_cycle_length", None)
                try:
                    if callable(cycle_fn):
                        cycle = int(cycle_fn())
                    else:
                        cycle = int(getattr(anim_node, "cycle_length", 0) or 0)
                except Exception:
                    cycle = 0
                if not (0 <= zone < MAX_ZONES):
                    print(
                        f"[DragInteraction][WARNING] 点击导出区域 ID {zone} 超出稳定范围 "
                        f"0-{MAX_ZONES - 1}，已跳过"
                    )
                    continue
                # 「开关值」模式下不提供冷启动播种变量：列表值 → 点击计数不可逆
                #（列表允许重复项），重启后统一回到列表首项（见点击导出节点注释）
                seq_fn = getattr(anim_node, "click_value_sequence", None)
                uses_click_values = bool(seq_fn()) if callable(seq_fn) else False
                first_var = ""
                if not uses_click_values:
                    for target in getattr(anim_node, "click_target_list", None) or []:
                        name = normalize_variable_name(getattr(target, "variable_name", "") or "")
                        if name:
                            first_var = f"${name}"
                            break
                entries.append((zone, min(64, max(0, cycle)), first_var))
        return entries

    def _click_export_seed_entries(self):
        """冷启动播种项：[(zone_id, $首个受控变量)]，同区域去重，最多 8 条。
        仅含配置了受控变量的导出节点；超限时明确报错，避免静默错误播种。"""
        seed_entries = []
        seen_zones = set()
        for zone, _cycle, first_var in self._collect_click_export_drivers():
            if not first_var or zone in seen_zones:
                continue
            seen_zones.add(zone)
            seed_entries.append((zone, first_var))
        if len(seed_entries) > 8:
            raise ValueError(
                "拖拽交互的点击导出冷启动最多支持 8 个不同区域，"
                f"当前配置了 {len(seed_entries)} 个；请合并区域或减少点击导出节点。"
            )
        return seed_entries

    def validate_export_configuration(self):
        """在写入任何导出文件前验证拖拽交互的跨节点约束。"""
        if self._cloth_enabled():
            self._cloth_object_names(required=True)
            if not self._collect_enabled_zone_entries():
                raise ValueError("上下拖拽：请先配置至少一个启用的拖拽区域")
            if self._is_efmi_mode():
                raise ValueError("上下拖拽目前需要 ZZMI 合并骨架")
        if self._feature_skd():
            self._click_export_seed_entries()
        if getattr(self, "feature_variable_link", True) and not self._feature_skd():
            # F4 ⇒ F1 硬依赖（phase2/n1 §6）：_feature_var 已强制降级，此处仅告警提示
            print(
                "[DragInteraction][WARNING] 已启用变量联动但形态键联动关闭，"
                "变量联动随之后台降级（其缓冲族依赖形态键联动）"
            )
        # F4 开、但没有任何形态键绑定区域：整族同步段/回读命令表/同步映射表都不会
        # 发射（设计上按绑定项非空门控）。这不是崩溃，但用户会看到「拖拽与形态键
        # 变量毫无联动」却查不出原因，必须显式告警。
        if self._feature_var() and not self._drag_drive_var_sync_bindings():
            print(
                "[DragInteraction][WARNING] 变量联动已开启，但没有任何形态键绑定拖拽区域 → "
                "不会生成「变量↔驱动缓冲同步」段与回读命令表（拖拽/动画驱动与形态键变量之间"
                "不会有联动，形态键只会跟随面板/驱动变量）。"
                "如需联动，请在形态键配置节点的「区域」列填写拖拽节点区域编号后重新导出。"
            )

    def _click_export_seed_variable_declared(self):
        """``$ssmtdrag_seed_pending_{ns}`` 是否会被声明（= 是否存在冷启动播种条目）。

        与 ``_emit_present_and_constants`` 的声明谓词必须保持一致：不声明却被
        点击计数导出段引用，会让该变量退化成 3DMigoto 的局部变量（跨段失效）。
        「开关值」模式的点击导出节点不提供播种变量（列表值 → 计数不可逆），
        此时导出段必须去掉门控里的 seed_pending 项。
        """
        try:
            if not self._feature_var():
                return False
            if self._is_efmi_mode():
                # EFMI 分支在 F1 下恒声明 $ssmtdrag_efmi_seed_pending_{ns}
                #（驱动段无条件绑 x157），门控里的 seed_pending 项必须保留。
                return True
            return bool(self._click_export_seed_entries())
        except Exception:
            return False

    def _click_export_trigger_vars(self, ns):
        """点击计数导出的"按住"变量（LMB / X）。

        点击计数只可能在按住期间推进（驱动 CS 只在按住时写 ClickCount），
        所以回读只需在按住（含松开沿与首帧建基线）时进行——空闲帧零 store。
        EFMI 分支的驱动门控语义不同（Candidate 跨实例赢家仲裁 + shader 内部
        mode 门），未做等价论证前不做门控 → 返回空表示"保持每帧回读"。
        """
        if self._is_efmi_mode():
            return []
        return [
            f"$ssmtdrag_lmb_down_{ns}",
            f"$ssmtdrag_x_down_{ns}",
        ]

    def _is_efmi_mode(self):
        """当前游戏类型是否为 EFMI（路由与消费方契约共用判定）。"""
        try:
            from ..common.global_config import GlobalConfig
            return str(getattr(GlobalConfig, "logic_name", "") or "") == LogicName.EFMI
        except Exception:
            return False

    def _drag_shapekey_resource_prefix(self):
        """形态键驱动资源前缀（跨节点契约，研究② §9）：EFMI 模式 → EFMI 独立前缀
        （ResourceEFMIDragShapeKey）；zzmi 家族 → 原前缀（ResourceDragShapeKey）。
        形态键节点经 _drag_shapekey_drive_resource_name 反扫推导资源名。"""
        if self._is_efmi_mode():
            return "ResourceEFMIDragShapeKey"
        return "ResourceDragShapeKey"

    def _click_export_names(self, ns):
        """点击计数导出（ClickExport）跨节点引用（研究② §3.2）：EFMI 模式 →
        EFMI 前缀资源/变量；zzmi 家族 → 原前缀。
        返回 (click_count_f_resource, booted_var, seed_pending_var)。"""
        if self._is_efmi_mode():
            return (
                f"ResourceEFMIDragShapeKeyClickCountF_{ns}",
                f"$ssmtdrag_efmi_booted_{ns}",
                f"$ssmtdrag_efmi_seed_pending_{ns}",
            )
        return (
            f"ResourceDragShapeKeyClickCountF_{ns}",
            f"$ssmtdrag_booted_{ns}",
            f"$ssmtdrag_seed_pending_{ns}",
        )

    def _drag_drive_zone_stage_counts(self):
        """按区域统计点击档位数：扫描同树所有开启拖拽驱动的形态键节点，
        每个区域取该区域无方向形态键 drag_click_stage 最大值（方向形态键忽略档位），最少 1。
        返回 {zone_id: stage_count}，只含被形态键引用的区域。
        EFMI 模式委托 EFMI 分支等价实现（独立代码，语义一致）。"""
        if self._is_efmi_mode():
            from .node_postprocess_draginteraction_efmi import DragInteractionEFMIExporter
            return DragInteractionEFMIExporter(self)._drag_drive_zone_stage_counts()
        tree = getattr(self, "id_data", None)
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
                    # 未勾选导出的形态键不生成变量与缓冲，不参与档位统计
                    continue
                try:
                    zone = int(getattr(item, "drag_zone_id", -1))
                except Exception:
                    zone = -1
                if zone < 0:
                    continue
                try:
                    dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
                except Exception:
                    dir_val = -1
                if dir_val >= 0:
                    # 方向形态键忽略档位，不参与该区域档位统计
                    continue
                try:
                    stage = int(getattr(item, "drag_click_stage", 1) or 1)
                except Exception:
                    stage = 1
                counts[zone] = max(counts.get(zone, 1), max(1, stage))
        return counts

    def _drag_drive_buffer_layout(self):
        """按区域独立档位计算驱动缓冲布局。
        返回 (total_slots, zone_bases, zone_stage_counts)：
        每个区域段 = 4 方向槽 + 该区域档位数 N 个无方向槽；
        zone_bases[z] = 区域 z 段在缓冲中的起始槽位；total_slots = 所有区域段长之和。
        EFMI 模式委托 EFMI 分支等价实现（恒双区语义，独立代码）。"""
        if self._is_efmi_mode():
            from .node_postprocess_draginteraction_efmi import DragInteractionEFMIExporter
            return DragInteractionEFMIExporter(self)._drag_drive_buffer_layout()
        capacity = self._zone_capacity(self._collect_enabled_zone_entries())
        export_entries = self._collect_click_export_drivers()
        if export_entries:
            # 点击计数导出节点的区域必须落在缓冲容量内
            capacity = max(capacity, max(zone for zone, _cycle, _var in export_entries) + 1)
        zone_counts = self._drag_drive_zone_stage_counts()
        zone_stage_counts = [max(1, int(zone_counts.get(z, 1))) for z in range(capacity)]
        for zone, cycle, _first_var in export_entries:
            if cycle >= 2:
                # 点击计数导出节点的循环档数扩展该区域点击循环（0..档数-1），与形态键档位取大
                zone_stage_counts[zone] = max(zone_stage_counts[zone], cycle - 1)
        zone_bases = []
        running = 0
        for stage_count in zone_stage_counts:
            zone_bases.append(running)
            running += 4 + stage_count
        return running, zone_bases, zone_stage_counts

    def _drag_drive_var_sync_bindings(self):
        """扫描同树开启拖拽驱动的形态键节点，收集「导出变量 → 驱动缓冲槽位」同步绑定。
        返回 [(var_name, slot_id, zone_id, nd_stage), ...]，按节点/项顺序稳定排列：
        var_name 带 $ 前缀（与形态键节点写入 IniParams 的变量一致，同一分配入口保证同名）；
        nd_stage 为无方向档位数（>=1），方向形态键为 -1（烘焙时映射为 0xFFFFFFFF）。
        槽位布局与形态键着色器 SHAPEKEY_SLOT_IDS 的 CPU 前缀和完全一致。"""
        tree = getattr(self, "id_data", None)
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
                    # 未勾选导出的形态键不会生成 INI 变量，不产生同步绑定
                    continue
                try:
                    zone = int(getattr(item, "drag_zone_id", -1))
                except Exception:
                    zone = -1
                if zone < 0 or zone >= len(zone_bases):
                    continue
                try:
                    dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
                except Exception:
                    dir_val = -1
                if 0 <= dir_val < 4:
                    slot_id = zone_bases[zone] + dir_val
                    nd_stage = -1
                else:
                    try:
                        stage = int(getattr(item, "drag_click_stage", 1) or 1)
                    except Exception:
                        stage = 1
                    nd_stage = max(1, stage)
                    slot_id = zone_bases[zone] + 4 + (nd_stage - 1)
                var_name = get_var_name(getattr(item, "shape_key_name", "") or "")
                if not var_name:
                    continue
                bindings.append((var_name, slot_id, zone, nd_stage))
        if len(bindings) > VAR_SYNC_MAX_BINDINGS:
            raise ValueError(
                f"形态键变量同步最多支持 {VAR_SYNC_MAX_BINDINGS} 个绑定，"
                f"当前为 {len(bindings)} 个"
            )
        return bindings

    # =======================================================================
    # 段生成（CustomShader / CommandList / Resource）与钩子注入、Present/Constants
    # =======================================================================
    # 段生成器（对照 Remielle_MT.ini 逐行核对；shader 原样，删减全在 ini 侧）
    # =======================================================================

    def _emit_sections(self, sections, components, ns):
        """生成 Resource / CustomShader / CommandList / Key 段（幂等：已存在跳过）。"""
        res = f"res/drag_interaction"
        sections.setdefault(DRAG_TAIL_MARKER, [])

        # ---- 全局共享资源 ----
        global_resources = {
            f"[ResourceDragDetectID_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPinnedDetectID_{ns}]": [
                "type = StructuredBuffer", "stride = 4", "array = 2",
                "bind_flags = shader_resource unordered_access",
            ],
            f"[ResourceDragPinnedDetectInfo_{ns}]": [
                "type = StructuredBuffer", "stride = 16", "array = 15",
                "bind_flags = shader_resource unordered_access",
            ],
            f"[ResourceDragZoneOut_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            f"[ResourceDragJiggleScreenState_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPathProgressState_{ns}]": [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {self._zone_capacity(self._collect_enabled_zone_entries())}",
            ],
            f"[ResourceDragPathVectors_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/PathVectors_{ns}.buf",
            ],
            f"[ResourceDragZoneParams_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res}/ZoneParams_{ns}.buf",
            ],
            f"[ResourceDragViewportFrameAPI_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 16"],
            f"[ResourceDragBakeRT_{ns}]": [
                # 与原作 ResourceLLBakeRT 完全一致：bind_flags 声明渲染目标+着色器资源，
                # Bake 的 o0 = set_viewport 需要 RT 能力，cs-t3 读需要 SRV 能力；
                # 非法/缺失 bind_flags 会导致 RT 创建失败 → 标定无效 → 检测不执行
                "type = Texture2D",
                "mode = mono",
                "width = 8",
                "height = 2",
                "mips = 1",
                "array = 1",
                "msaa = 1",
                "msaa_quality = 0",
                "format = DXGI_FORMAT_R32G32B32A32_FLOAT",
                "bind_flags = render_target shader_resource",
            ],
        }
        if self._feature_skd():
            _total_slots, _zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
            _drive_capacity = len(_zone_stage_counts)
            global_resources[f"[ResourceDragShapeKeyDrive_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {_total_slots}",
            ]
            # 方向缓冲：与驱动缓冲同构，末位 1 个上一帧按键状态槽
            #（拖拽绑定锁存独立在 ResourceDragShapeKeyDragLatch，见下）
            global_resources[f"[ResourceDragShapeKeyDir_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {_total_slots + 1}",
            ]
            # 拖拽绑定锁存（独立单槽资源）：0 = 未绑定，否则存 区域id+1。
            # +1 编码使 boot/失臂清零（0.0）即未绑定（评审 F1）；独立于方向缓冲，
            # 失臂 else 整清锁存不波及 prev-press 槽（防重新臂动产生伪按下沿）
            global_resources[f"[ResourceDragShapeKeyDragLatch_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT", "array = 1",
            ]
            # 点击计数缓冲：每区域 1 个点击档位（1..stage_count，0=未点击），供多段切换
            global_resources[f"[ResourceDragShapeKeyClickCount_{ns}]"] = [
                "type = RWBuffer", "format = R32_UINT",
                f"array = {_drive_capacity}",
            ]
            # 点击计数浮点镜像：CPU 经 store 回读仅对浮点格式有验证过的先例
            #（R32_UINT 直接 store 无格式保证），供点击计数导出使用
            global_resources[f"[ResourceDragShapeKeyClickCountF_{ns}]"] = [
                "type = RWBuffer", "format = R32_FLOAT",
                f"array = {_drive_capacity}",
            ]
            # 主导方向缓冲：每区域 1 个当前主导方向（0=上 1=右 2=下 3=左），供“任意方向”绑定读取
            global_resources[f"[ResourceDragShapeKeyActiveDir_{ns}]"] = [
                "type = RWBuffer", "format = R32_UINT",
                f"array = {_drive_capacity}",
            ]
            # 每区域档位数缓冲：供驱动着色器按区域独立循环档位
            global_resources[f"[ResourceDragShapeKeyZoneStageCounts_{ns}]"] = [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res}/ZoneStageCounts_{ns}.buf",
            ]
            # 变量→驱动缓冲同步（绑定项非空且变量联动开启时生成）：prev 值缓冲
            # 用于变更检测，映射表为导出期烘焙的静态 Buffer（每绑定 4×uint32）
            sync_bindings = self._drag_drive_var_sync_bindings()
            if self._feature_var() and sync_bindings:
                global_resources[f"[ResourceDragShapeKeyVarPrev_{ns}]"] = [
                    "type = RWBuffer", "format = R32_FLOAT",
                    f"array = {len(sync_bindings)}",
                ]
                global_resources[f"[ResourceDragShapeKeyVarSyncMap_{ns}]"] = [
                    "type = Buffer", "format = R32G32B32A32_UINT",
                    f"filename = {res}/ShapeKeyVarSyncMap_{ns}.buf",
                ]
                # 拖拽激活标志（每区域）：同步 CS 每帧镜像驱动 CS 的绑定锁存
                #（ResourceDragShapeKeyDragLatch 单槽，按住命中即绑定、移出区域
                # 不丢、松开/失臂解除），门控 CPU 回读与变量→缓冲写入，
                # 实现两写入方分时互斥。
                # CPU 经 store 直接读取（store = $var, <资源名>, <float 索引>，
                # 不能带 ref 关键字——分词器会把 ref 交给 GetTarget 报 Unknown target，
                # 整条 store 行在加载期被静默丢弃）；
                # 需要含 StoreCommand 的 ZZMI 构建——旧构建（如 8/4 前）不含该实现，
                # 此时 store 行在加载期被丢弃，本链路静默失效但其余功能不受影响）
                global_resources[f"[ResourceDragShapeKeyZoneActive_{ns}]"] = [
                    "type = RWBuffer", "format = R32_FLOAT",
                    f"array = {_drive_capacity}",
                ]
        for sec, lines in global_resources.items():
            sections.setdefault(sec, lines)

        # ---- Key 段（hold 型，置位 + post 归零；Alt 修饰键同时臂动 mode）----
        key_defs = [
            (f"[KeyDragInputManagerLMB_{ns}]", "VK_LBUTTON", [f"$ssmtdrag_lmb_down_{ns} = 1"], [f"$ssmtdrag_lmb_down_{ns} = 0"]),
            (f"[KeyDragInputManagerRMB_{ns}]", "VK_RBUTTON", [f"$ssmtdrag_rmb_down_{ns} = 1"], [f"$ssmtdrag_rmb_down_{ns} = 0"]),
            (f"[KeyDragInputManagerX_{ns}]", "X", [f"$ssmtdrag_x_down_{ns} = 1"], [f"$ssmtdrag_x_down_{ns} = 0"]),
        ]
        if self.grab_key == 'ALT':
            key_defs.append((
                f"[KeyDragInputManagerModifier_{ns}]", "VK_MENU",
                [f"$ssmtdrag_modifier_down_{ns} = 1", f"$ssmtdrag_mode_{ns} = 1"],
                [f"$ssmtdrag_modifier_down_{ns} = 0", f"$ssmtdrag_mode_{ns} = 0"],
            ))
        for sec, key, set_lines, post_lines in key_defs:
            if sec not in sections:
                lines = [f"key = {key}", "type = hold"] + set_lines + [f"post {p}" for p in post_lines]
                sections[sec] = lines

        # ---- 运行模式切换热键（type=cycle，每次按键循环 0→1→2→0）----
        mode_toggle_sec = f"[KeyDragInputManagerModeToggle_{ns}]"
        mode_toggle_key = str(getattr(self, "mode_toggle_key", "") or "").strip()
        drag_mode_var = self._runtime_variable_names(ns)[0]
        toggle_lines = [
            f"key = {mode_toggle_key}",
            "type = cycle",
            f"{drag_mode_var} = 0,1,2",
        ]
        # 快捷键可修改后重新导出：段已存在时也按当前值覆盖（保持幂等）
        if sections.get(mode_toggle_sec) != toggle_lines:
            sections[mode_toggle_sec] = toggle_lines

        # ---- 每组件资源 + Detect / Bake×8 / Jiggle / PinComponent 段 ----
        for comp in components:
            self._emit_component_resources(sections, comp, ns)
            self._emit_detect_section(sections, comp, ns)
            self._emit_bake_sections(sections, comp, ns)
            self._emit_jiggle_section(sections, comp, ns)
            self._emit_pin_component_section(sections, comp, ns)

        # ---- 全局 Pin / UpdateScreenJiggle / CommandList 段 ----
        self._emit_pin_detected_section(sections, ns)
        # 共享段恒发（手部/光标/预览消费共享屏幕状态），随后为每个组件各发一份独立段：
        # 组件级触发链（Detect→PinComponent→UpdateScreenJiggle→Jiggle）各自闭环，互不串动。
        self._emit_update_screen_jiggle_section(sections, ns)
        for comp in components:
            self._emit_update_screen_jiggle_section(sections, ns, comp)
        if self._feature_skd():
            self._emit_shapekey_drive_section(sections, ns)
            if self._feature_var():
                self._emit_shapekey_var_sync_section(sections, ns)
                self._emit_shapekey_var_readback_command_list(sections, ns)
        # ---- 物体显隐：发布 CS + 命令列表 + 缓冲资源 ----
        self._emit_vis_publish_sections(sections, components, ns)
        self._emit_command_lists(sections, components, ns)

        # ---- 手型光标（S8）：资源 + 绘制段 ----
        if self.enable_hand_cursor:
            self._emit_hand_resources(sections, ns)
            self._emit_hand_sections(sections, ns)

        # ---- 视口探针系统：资源 + 探针段 + ShaderOverride ----
        if self.enable_viewport_probe:
            self._emit_viewport_probe_sections(sections, ns)
        if self._cloth_enabled():
            from .drag_cloth import emit_components
            emit_components(self, sections, components, ns)

    def _emit_component_resources(self, sections, comp, ns):
        cn = comp["comp_name"]
        stem = comp["base_name"]
        res_dir = self._buffer_dir(sections, comp)
        # 逐顶点缓冲的行数契约：一律取该部件自己的顶点数（VLR 的 override_vertex_count）。
        # 缺省时退到与 dispatch 相同的 100000 假设：宁大勿小——偏大只是多占内存，
        # 偏小会让真实索引到的顶点永远写不进去（见下方 TempVB0 注）。
        _vc = int(comp.get("vertex_count", 0) or 0) or 100000
        comp_resources = {
            f"[ResourceDragDetect{cn}ObjectMap_{ns}]": [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res_dir}/{stem}ObjectMap.buf",
            ],
            f"[ResourceDragDebugDetect_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 23"],
            f"[ResourceDragComponentDetect_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15"],
            f"[ResourceDragPinnedComponentID_{cn}_{ns}]": [
                "type = RWStructuredBuffer", "stride = 4", "array = 2",
                "bind_flags = shader_resource unordered_access",
            ],
            f"[ResourceDragPinnedComponentInfo_{cn}_{ns}]": [
                "type = RWStructuredBuffer", "stride = 16", "array = 15",
                "bind_flags = shader_resource unordered_access",
            ],
            f"[ResourceDragComponentZoneOut_{cn}_{ns}]": ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            f"[ResourceDragJiggleState_{cn}_{ns}]": ["type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 10"],
            # u5 is a separate structured scratch copy. Copying it back must
            # clear its structured misc flag before the result can be a VB.
            f"[ResourceDragJiggleTempVB0_{cn}_{ns}]": [
                "type = Buffer",
                f"stride = {self._get_vertex_attribute_layout('position')[0]}",
                f"array = {_vc}",
                "bind_flags = vertex_buffer",
                "misc_flags = none",
            ],
        }
        comp_resources[f"[ResourceDragJiggleZoneIDs_{cn}_{ns}]"] = [
            "type = Buffer", "format = R32G32B32A32_UINT",
            f"filename = {res_dir}/{stem}JiggleZoneIDs.buf",
        ]
        comp_resources[f"[ResourceDragJiggleZoneWeights_{cn}_{ns}]"] = [
            "type = Buffer", "format = R32G32B32A32_FLOAT",
            f"filename = {res_dir}/{stem}JiggleZoneWeights.buf",
        ]
        # 逐组件屏幕状态/路径进度：共享副本（ResourceDragJiggleScreenState_{ns}）按
        # 全局命中赢家（ResourceDragPinnedDetectInfo_{ns}）写入，手部/光标/预览按设计读它；
        # 多组件若共用这两条缓冲，各组件的 Jiggle 就会读同一个赢家的状态——要么互相带动，
        # 要么第二个组件饿死（叶瞬光01 双组件实测）。故每个组件各发一份，由该组件自己的
        # PinComponent 赢家（ResourceDragPinnedComponentInfo_{cn}）喂入，Jiggle 侧回绑。
        comp_resources[f"[ResourceDragJiggleScreenState_{cn}_{ns}]"] = [
            "type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 15",
        ]
        comp_resources[f"[ResourceDragPathProgressState_{cn}_{ns}]"] = [
            "type = RWBuffer", "format = R32_FLOAT",
            f"array = {self._zone_capacity(self._collect_enabled_zone_entries())}",
        ]
        # 逐三角形物体编号（显隐过滤：检测着色器按命中三角形查 ObjectVis）
        for p_idx, _part in enumerate(comp["parts"]):
            comp_resources[f"[ResourceDragTriangleObjectIDs_{cn}P{p_idx}_{ns}]"] = [
                "type = Buffer", "format = R32_UINT",
                f"filename = {res_dir}/{stem}TriangleObjectIDsP{p_idx}.buf",
            ]
        # JiggleParams：每部件 1 条碰撞体档案（原作契约，条目同值；array = 4×部件数）
        n_parts = max(1, len(comp["parts"]))
        jp = self._jiggle_params_data(n_parts)
        comp_resources[f"[ResourceDragJiggleParams_{cn}_{ns}]"] = [
            "type = Buffer", "format = R32G32B32A32_FLOAT", f"array = {4 * n_parts}",
            f"data = {jp}",
        ]
        # 碰撞检测资源（防穿模；仅开关开启且已成功烘焙时发射）
        if comp.get("collision_grid"):
            comp_resources[f"[ResourceDragColliderPoints_{cn}_{ns}]"] = [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res_dir}/{stem}ColliderPoints.buf",
            ]
            comp_resources[f"[ResourceDragColliderVertexL0_{cn}_{ns}]"] = [
                "type = Buffer", "format = R32G32B32A32_FLOAT",
                f"filename = {res_dir}/{stem}ColliderVertexL0.buf",
            ]
            if comp.get("collision_trigrid"):
                comp_resources[f"[ResourceDragColliderTriGrid_{cn}_{ns}]"] = [
                    "type = Buffer", "format = R32_UINT",
                    f"filename = {res_dir}/{stem}ColliderTriGrid.buf",
                ]
                comp_resources[f"[ResourceDragColliderTriGridTris_{cn}_{ns}]"] = [
                    "type = Buffer", "format = R32G32B32A32_FLOAT",
                    f"filename = {res_dir}/{stem}ColliderTriGridTris.buf",
                ]
                # 网格三角形 → 顶点缓冲索引（权重重映射要用它反查三角形三个顶点的权重）
                comp_resources[f"[ResourceDragColliderTriGridVerts_{cn}_{ns}]"] = [
                    "type = Buffer", "format = R32_UINT",
                    f"filename = {res_dir}/{stem}ColliderTriGridVerts.buf",
                ]
                surface = comp.get("collision_surface")
                if surface:
                    for tag, filename in (("Order", "ColliderSweepOrder"), ("Cloth", "ColliderCloth"),
                                          ("Adjacency", "ColliderAdjacency")):
                        comp_resources[f"[ResourceDragCollision{tag}_{cn}_{ns}]"] = [
                            "type = Buffer", "format = R32_UINT", f"filename = {res_dir}/{stem}{filename}.buf",
                        ]
                    comp_resources[f"[ResourceDragCollisionBounds_{cn}_{ns}]"] = [
                        "type = RWBuffer", "format = R32G32B32A32_FLOAT", f"array = {int(surface['leaves']) * 4}",
                    ]
                    comp_resources[f"[ResourceDragCollisionFaceLimits_{cn}_{ns}]"] = [
                        "type = RWBuffer", "format = R32_FLOAT", f"array = {int(surface['face_count'])}",
                    ]
                # 沿腿轴滑动状态（逐顶点累积的轴向位移；用户 m05361 的「上下滑动控制丝袜长度」）
                # shader 按 i*2 索引（每顶点 2 个 float4：位移 + 上一帧轴向分量）
                comp_resources[f"[ResourceDragJiggleSlideState_{cn}_{ns}]"] = [
                    "type = RWBuffer", "format = R32G32B32A32_FLOAT",
                    f"array = {_vc * 2}",
                ]
                # Blend 的 typed 视图（结构化缓冲读不了权重，必须按 R32G32B32A32_FLOAT 索引）
                comp_resources[f"[ResourceDragJiggleBlendTyped_{cn}_{ns}]"] = [
                    "type = Buffer", "format = R32G32B32A32_FLOAT",
                    # 文件名必须带连字符：真实文件是 `d5e1e7ff-Blend.buf`（与 [Resourced5e1e7ffBlend]
                    # 段一致）。曾经写成 `d5e1e7ffBlend.buf` ⇒ 目录里没有该文件 ⇒ 资源加载失败、
                    # cs-t86 读不到权重，穿脱的权重投影整条链断掉（t3 §D 报的 F1）。
                    f"filename = {comp.get('collision_blend_file') or f'{res_dir}/{stem}-Blend.buf'}",
                ]
                # Persistent compute state matches RWStructuredBuffer<SockBlendRec>.
                # The draw receives a separate plain VB snapshot at stride 32.
                comp_resources[f"[ResourceDragJiggleBlendOut_{cn}_{ns}]"] = [
                    "type = RWStructuredBuffer", "stride = 32", f"array = {_vc}",
                    "bind_flags = shader_resource unordered_access",
                    f"filename = {comp.get('collision_blend_file') or f'{res_dir}/{stem}-Blend.buf'}",
                ]
                comp_resources[f"[ResourceDragJiggleBlendVB_{cn}_{ns}]"] = [
                    "type = Buffer", "stride = 32", f"array = {_vc}",
                    "bind_flags = vertex_buffer",
                    "misc_flags = none",
                ]
        for sec, lines in comp_resources.items():
            sections.setdefault(sec, lines)

    def _emit_vis_publish_sections(self, sections, components, ns):
        """物体显隐发布：CPU flag 变量 → ini params → 发布 CS → GPU 缓冲。
        复用与 rzm_shapekey_var_sync 相同的“变量→缓冲”链路；flag 槽位从
        IniParams[130] 起按 4/float4 打包（与 81/100/119 的既有用法错开）。"""
        # 编号契约（逐组件派生 + 断言，不得靠巧合）：每个组件的物体数恒等于其
        # object_id_map 的条目数，oid 由 _locate_components 跨组件累计分配；
        # ResourceDragObjectVis 的 array 必须 == Σ 每组件物体数。
        per_component_counts = [len(comp.get("object_id_map") or {}) for comp in components]
        declared_counts = [int(comp.get("object_count") or 0) for comp in components]
        if declared_counts != per_component_counts:
            raise ValueError(
                "[DragInteraction] 物体数声明与 object_id_map 不一致："
                f"object_count={declared_counts} vs len(object_id_map)={per_component_counts}"
            )
        total = sum(per_component_counts)
        if not total:
            return
        vis_sec = f"[ResourceDragObjectVis_{ns}]"
        vis_lines = ["type = RWBuffer", "format = R32_FLOAT", f"array = {total}"]
        previous = sections.get(vis_sec)
        if previous is not None and list(previous) != vis_lines:
            # 配置变化（物体增删/组件跳过）后旧声明会残留：array 与新 oid 空间错位会
            # 让发布 CS 漏写后段 flag，这里按当前配置重写而不是沿用旧值。
            print(
                f"[DragInteraction][INFO] {vis_sec} 容量 {previous} 与当前 Σ 每组件物体数 "
                f"{per_component_counts} 不一致，已重写为 array = {total}"
            )
        sections[vis_sec] = vis_lines
        oids = self._global_object_oids(components)
        if len(oids) != total:
            # 唯一性硬断言：oid 重复 = 两个组件抢同一条显隐 flag（会互相清/置位），
            # 且 array 与 oid 数不符。编号空洞（被跳过组件的 oid 被整体剔除）不在此列，
            # 只告警——不值得因此让整次导出失败。
            raise ValueError(
                "[DragInteraction] 物体编号与 ResourceDragObjectVis 容量不一致："
                f"array = {total}（Σ 每组件物体数 {per_component_counts}），实际 oid = {oids}"
            )
        if oids != list(range(total)):
            print(
                f"[DragInteraction][WARNING] 物体编号存在空洞（oid={oids}，容量 {total}）："
                "被跳过的组件仍占用了全局编号，其 flag 不会出现在 "
                f"CommandListDragVisPublish_{ns} 中"
            )
        sections.setdefault(f"[CustomShaderDragVisPublish_{ns}]", [
            f"cs = {RES_SHADER_DIR}/rzm_vis_publish.hlsl",
            f"cs-u0 = ResourceDragObjectVis_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null",
        ])
        pub_lines = []
        for oid in range(total):
            slot = 130 + oid // 4
            channel = "xyzw"[oid % 4]
            pub_lines.append(f"{channel}{slot} = $ssmtdrag_objvis_{ns}_{oid}")
        pub_lines.append(f"run = CustomShaderDragVisPublish_{ns}")
        sections.setdefault(f"[CommandListDragVisPublish_{ns}]", pub_lines)

    def _jiggle_params_data(self, collider_count=1):
        """每条 16 float：objectID radius strength falloff dragScale grabDamping grabSpring
        releaseDamping releaseSpring releaseKick maxOffset targetFollow mouseYDir mouseXDir 0 0。
        按部件数重复（原作契约：每部件一条同值档案，Body/Hair=2、Legs=1）。
        releaseKick=1.18 / targetFollow=0.12 为原作调好的标准碰撞档案——注意这与
        IniParams[71] 回退槽位（POLISH_PARAMS.y=releaseKick、.w=targetFollow，
        phys_target_follow/phys_release_kick 属性在那里恰好交叉喂到正确值）是两回事，
        缓冲档案必须用原作精确值，不能与属性混用。"""
        entry = [
            0, 0.25, 1.0, 1.5, 1.0,
            self.phys_grab_damping, self.phys_grab_spring,
            self.phys_release_damping, self.phys_release_spring,
            1.18, 0.5, 0.12,
            0, 0, 0, 0,
        ]
        vals = entry * max(1, collider_count)
        return " ".join(self._fmt(v) for v in vals)

    @staticmethod
    def _fmt(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return str(int(f)) if f == int(f) else repr(f)

    # ---- Detect{Comp}（dispatch 1,1,1）----

    def _detect_section_lines(self, comp, ns, part_index):
        cn = comp["comp_name"]
        vertex_base = int(comp.get("vertex_base", 0) or 0)
        if part_index is not None and part_index < len(comp.get("parts") or []):
            # 承载真实几何的 part 一律用掩码基准（= comp["vertex_base"]，已被
            # _assert_component_base_alignment 断言一致）；前缀 stub part 用自身 base，
            # 使 z26 与掩码行空间严格对齐（基座段与 P0 变体不再自相矛盾）。
            vertex_base = self._part_vertex_base(comp, comp["parts"][part_index])
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_object_detect.hlsl",
            "x28 = 0",
        ]
        if part_index is not None:
            # 每部件变体段只 raycast 本部件对应的 ObjectMap 条目，避免每个部件钩子
            # 都遍历整个 mesh 的所有三角形（N 倍放大）。y28/z28 为 shader 子区间。
            # t7/t8 = 逐三角形物体编号 + 物体显隐缓冲（隐式跳过隐藏物体）。
            lines.extend([
                f"y28 = {part_index}",
                "z28 = 1",
                f"cs-t7 = ResourceDragTriangleObjectIDs_{cn}P{part_index}_{ns}",
                f"cs-t8 = ResourceDragObjectVis_{ns}",
            ])
        lines.extend([
            "cs-t0 = vb0",
            "cs-t1 = ib",
            f"cs-t2 = ResourceDragDetect{cn}ObjectMap_{ns}",
            f"cs-t3 = ResourceDragBakeRT_{ns}",
            f"cs-t4 = ResourceDragJiggleZoneIDs_{cn}_{ns}",
            f"cs-t5 = ResourceDragJiggleZoneWeights_{cn}_{ns}",
            f"cs-t6 = ResourceDragViewportFrameAPI_{ns}",
            f"cs-u0 = ResourceDragDetectID_{ns}",
            f"cs-u1 = ResourceDragComponentDetect_{cn}_{ns}",
            f"cs-u2 = ResourceDragDebugDetect_{cn}_{ns}",
            "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            "x25 = $isMouseButtonDown",
            "x26 = 48", f"z26 = {vertex_base}", "w26 = 8.0",
            "x27 = $cursorX", "y27 = $cursorY", "z27 = res_width", "w27 = res_height",
            "x85 = 0", "y85 = 0", "z85 = 1", "w85 = 1",  # 视口恒等（offset=0,0 scale=1,1）
            # VIEWPORT_VALID 必须为 1：shader 检测主循环被 ValidViewportCursor 门控。
            "x86 = 1",
            "x74 = 0",  # debug dump 门控关闭
            "dispatch = 1, 1, 1",
            "cs-u0 = null", "cs-u1 = null", "cs-u2 = null",
        ])
        return lines

    def _emit_detect_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragDetect{cn}_{ns}]"
        if sec not in sections:
            sections[sec] = self._detect_section_lines(comp, ns, None)
        # 每个部件额外生成一个限定范围的变体段；基座段保留旧默认（遍历全部件）。
        for p_idx in range(len(comp["parts"])):
            part_sec = f"[CustomShaderDragDetect{cn}P{p_idx}_{ns}]"
            if part_sec not in sections:
                sections[part_sec] = self._detect_section_lines(comp, ns, p_idx)

    # ---- Bake{Comp}{Part} + Sample（R5：8P 段 → P 段 + 8 次参数化 run）----

    def _emit_bake_sections(self, sections, comp, ns):
        cn = comp["comp_name"]
        const_lines = sections.setdefault("[Constants]", [])
        for p_idx, part in enumerate(comp["parts"]):
            part_tag = f"{cn}P{p_idx}"
            bake_sec = f"[CustomShaderDragBake{part_tag}_{ns}]"
            if bake_sec in sections:
                continue
            ib_res = part["ib_resource"]
            vertex_base = int(
                part.get("vertex_base", comp.get("vertex_base", 0)) or 0
            )
            step = part["index_count"] // 8
            ib_first_index = part["ib_first_index"]
            # R5 段合并（phase2/n1 §7.2 + t6 §4.3）：8 个 Sample{i} 段合并为 1 个
            # 参数化 sample 段，bake 段内 8 次 run。三个迭代变量必须声明为
            # [Constants] global——3Dmigoto 每 [CustomShader] 段独立局部 scope，
            # 裸 $x= 不自动声明，跨段仅 global 可见；按部件 {cn}P{p}_{ns} 隔离。
            i_var = f"$ssmtdrag_bake_i_{part_tag}_{ns}"
            base_var = f"$ssmtdrag_bake_base_{part_tag}_{ns}"
            step_var = f"$ssmtdrag_bake_step_{part_tag}_{ns}"
            off_var = f"$ssmtdrag_bake_off_{part_tag}_{ns}"
            sample_target = f"CustomShaderDragBakeSample_{part_tag}_{ns}"
            for g in (
                f"global {base_var} = {ib_first_index}",
                f"global {step_var} = {step}",
                f"global {i_var} = 0",
            ):
                var = g.split("=", 1)[0].replace("global ", "").replace("persist ", "").strip()
                if not any(f"{var} " in line or f"{var}=" in line for line in const_lines):
                    const_lines.append(g)
            lines = [
                "run = BuiltInCommandListUnbindAllRenderTargets",
                f"clear = ResourceDragBakeRT_{ns} 0.0",
                f"{i_var} = 0",
            ]
            # 8 次参数化 run：i 递增后由 sample 段经 off = base + i*step 推导
            # draw 起始索引（drawindexed 第二参 == y26 == off 同步约束，t6 §4.4）
            for i in range(8):
                lines.append(f"run = {sample_target}")
                if i < 7:
                    lines.append(f"{i_var} = {i_var} + 1")
            sections[bake_sec] = lines
            sections[f"[{sample_target}]"] = [
                f"gs = {RES_SHADER_DIR}/rzm_gs_probe.hlsl",
                f"gs-t1 = {ib_res}",
                f"ps = {RES_SHADER_DIR}/rzm_gs_probe.hlsl",
                "topology = point_list",
                f"o0 = set_viewport no_view_cache ResourceDragBakeRT_{ns}",
                # [t148] 探针精确写入：透明 pass 的 alpha 混合会污染校准 RT
                "blend = ADD ONE ZERO",
                "alpha = ADD ONE ZERO",
                f"local {off_var}",
                f"{off_var} = {base_var} + {i_var} * {step_var}",
                f"x26 = {i_var}",
                f"y26 = {off_var}",
                f"drawindexed = 1, {off_var}, {vertex_base}",
            ]

    # ---- Jiggle{Comp}（TempVB0 序列 + 完整寄存器块）----

    def _emit_jiggle_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragJiggle{cn}_{ns}]"
        # 本段内容随生成器版本演进（含 R9 ShadowVB 时代），无条件重发以让重导出
        # 覆盖旧形态；内容确定性保证幂等（同输入同输出）。
        vertex_count = comp["vertex_count"] or 100000
        dispatch_n = (vertex_count + 255) // 256

        lines = [
            "local $CursorXPast", "local $CursorYPast", "local $WasMouseButtonDown",
            "local $back_x69 = x69", "local $back_y69 = y69",
            "local $back_z69 = z69", "local $back_w69 = w69",
            # 根源卫生修复：本段也写共享槽 70/72/73（TTL 合成器同名槽），一并存还。
            "local $back_x70 = x70", "local $back_y70 = y70",
            "local $back_z70 = z70", "local $back_w70 = w70",
            "local $back_x72 = x72", "local $back_y72 = y72",
            "local $back_z72 = z72", "local $back_w72 = w72",
            "local $back_x73 = x73", "local $back_y73 = y73",
            "local $back_z73 = z73", "local $back_w73 = w73",
            "",
            "if $isMouseButtonDown == 1",
            "\tif $WasMouseButtonDown == 0",
            "\t\t$CursorXPast = $cursorX", "\t\t$CursorYPast = $cursorY",
            "\tendif",
            "\t$WasMouseButtonDown = 1", "\tw67 = 1",
            "else",
            "\t$WasMouseButtonDown = 0", "\t$CursorXPast = 0", "\t$CursorYPast = 0", "\tw67 = 0",
            "endif",
            "",
            f"cs = {RES_SHADER_DIR}/rzm_jiggle_interaction.hlsl",
            "x67 = $CursorXPast", "y67 = $CursorYPast",
            "x68 = 0.25", "y68 = 1.00", "z68 = 1.50", "w68 = 1.00",  # 回退档案
            "x69 = $cursorX", "y69 = $cursorY", "z69 = $screenW", "w69 = $screenH",
            "x70 = " + self._fmt(self.phys_grab_damping),
            "y70 = " + self._fmt(self.phys_grab_spring),
            "z70 = " + self._fmt(self.phys_release_damping),
            "w70 = " + self._fmt(self.phys_release_spring),
            "x71 = 0.50", "y71 = " + self._fmt(self.phys_target_follow),
            "z71 = 1.00", "w71 = " + self._fmt(self.phys_release_kick),
            f"x72 = {max(1, len(comp['parts']))}",  # 碰撞体条数 = 部件数（原作契约）
            "y72 = " + self._fmt(self.mult_radius),
            "z72 = " + self._fmt(self.mult_strength),
            "w72 = " + self._fmt(self.mult_spring),
            "x73 = " + self._fmt(self.mult_damping), "y73 = 1.00",
            "x74 = 0",  # debug dump 关闭
            "x75 = 0",  # vertex debug 关闭
            f"x76 = $ssmtdrag_delta_time_{ns}",
            f"y76 = $ssmtdrag_sim_speed_{ns}",
            f"z76 = $ssmtdrag_max_step_{ns}",
        ]
        lines.extend([
            # 是否存在 Path Slide（IniParams[119].x = 总闸）：1 = 开放路径模式，丝袜沿大腿表面
            # 滑动 + 逐帧权重实时投影。开启只解除 shader 侧总闸，真正触发还要求同时满足
            # 区域位 ZoneParams[zone*2+1].z 与方向位 PathVectors[zone].w（三闸串联）。
            # 档位 0/1 仍逐字节不变：滑动只在档位 2 的拖拽通道内可达。
            "x119 = 1",
            f"y119 = {self._zone_capacity(self._collect_enabled_zone_entries())}",
            # 法线拖拽符号模式（IniParams[119].z）：0 = 恒定 +n（用户口径，默认，位移方向
            # 与鼠标方向无关）；1 = 随鼠标在屏幕法线上的投影翻转（旧行为，实机 A/B 回退用）。
            "z119 = 0",
            # t33 爆炸守卫的位移硬上限（IniParams[119].w）：0 = 用着色器默认
            # （0.5 = 节点侧 maxOffset 上限）。非 0 = 覆盖两者（调试用）。
            "w119 = 0",
        ])
        # 碰撞检测参数（防穿模）：仅在模式三/拉扯模式生效。115 选择整面求解，0 兼容旧资源。
        # 显式写全（含关闭时的 x101 = 0），遵守「读了必设」残留纪律。
        collision_grid = comp.get("collision_grid")
        if collision_grid:
            drag_mode_var = self._runtime_variable_names(ns)[0]
            mode = 0 if getattr(self, "collision_mode", 'SOFT') == 'SOFT' else 1
            margin = self._fmt(float(getattr(self, "collision_margin", 0.002) or 0.0))
            safety = self._fmt(0.9)
            # x104 只承载「拖拽模式变量」这一个值：粗/细网格查询已不再被 CollisionSolve
            # 使用（点云约束走绑定姿态最近的 4 个采样点）。原先的
            # `{变量} {cnx} {cny} {cnz}` 是「变量 + 字面量」混合多值，全仓无先例、
            # 无法确认 3DMigoto 的解析行为，改为纯单值。
            # 3DMigoto 的 ini 解析器**不支持** `x101 = 1 0.002 1 0.9` 这类一行多值写法：
            # 它会在第二个 token 处报 `Syntax Error: ... Unexpected identifier` 并整行丢弃
            # （实测日志：`Unrecognised entry: x101 = 1 0.0020000000949949026 1 0.9`），
            # 导致 COLLISION_PARAMS 保持未定义 ⇒ 碰撞静默失效。必须逐行拆成 x/y/z/w 单值。
            lines.extend([
                "x101 = 1",
                f"y101 = {margin}",
                f"z101 = {mode}",
                f"w101 = {safety}",
                f"x104 = {drag_mode_var}",
            ])
            # 远侧方向性约束的碰撞体形状：只在碰撞开启时发射；碰撞关不写、shader 不读。
            # 槽位 112-114 在拖拽着色器族内空闲（101-111、119 已占用）。
            shape = comp.get("collision_shape")
            if shape:
                c = np.asarray(shape["center"], dtype=np.float64).reshape(3)
                a = np.asarray(shape["axis"], dtype=np.float64).reshape(3)
                lines.extend([
                    f"x112 = {self._fmt(float(c[0]))}",
                    f"y112 = {self._fmt(float(c[1]))}",
                    f"z112 = {self._fmt(float(c[2]))}",
                    "w112 = 0",
                    f"x113 = {self._fmt(float(a[0]))}",
                    f"y113 = {self._fmt(float(a[1]))}",
                    f"z113 = {self._fmt(float(a[2]))}",
                    f"w113 = {self._fmt(float(shape['axis_min']))}",
                    f"x114 = {self._fmt(float(shape['axis_max']))}",
                    f"y114 = {self._fmt(float(shape['radius']))}",
                    "z114 = 1",
                    "w114 = 0",
                ])
            else:
                # 形状退化（球/方块/顶点太少）：显式写 0，shader 的 [114].z 门控会跳过。
                lines.extend([
                    "x112 = 0", "y112 = 0", "z112 = 0", "w112 = 0",
                    "x113 = 0", "y113 = 0", "z113 = 0", "w113 = 0",
                    "x114 = 0", "y114 = 0", "z114 = 0", "w114 = 0",
                ])
            # 槽位 107：每个顶点绑定的约束三角形数（0 = 未烘焙，走点云回退）。
            # 槽位 108/109：动态三角形网格（origin + cell / dims + cell_count）。
            # 运行时按顶点当前位置现查邻域三角形，约束 =「到表面的带符号距离不许
            # 比绑定姿态时更小」；基准在运行时现算，不需要额外缓冲。
            collision_trigrid = comp.get("collision_trigrid")
            if collision_trigrid:
                gb = collision_trigrid["bmin"]
                gd = collision_trigrid["dims"]
                lines.extend([
                    f"x108 = {self._fmt(float(gb[0]))}",
                    f"y108 = {self._fmt(float(gb[1]))}",
                    f"z108 = {self._fmt(float(gb[2]))}",
                    f"w108 = {self._fmt(float(collision_trigrid['cell']))}",
                    f"x109 = {int(gd[0])}",
                    f"y109 = {int(gd[1])}",
                    f"z109 = {int(gd[2])}",
                    f"w109 = {int(collision_trigrid['cell_count'])}",
                ])
            else:
                lines.extend(["x108 = 0", "x109 = 0"])
            # 槽位 110：沿腿轴滑动。xyz = 腿轴单位向量，w = 开关（1 轴向固化 / 2 径向固化）。
            # 轴向量由导出侧按碰撞体 PCA 主轴算出；w 恒 1。
            # 槽位 111：权重重映射开关（x = 1 时按最近碰撞体三角形的重心坐标重算权重）。
            # 用户 m05474：两者都只在碰撞开启时发射（本分支即 collision_grid 存在）。
            slide_axis = comp.get("slide_axis") or (-0.112638, -0.091816, 0.989385)
            lines.extend([
                f"x110 = {self._fmt(float(slide_axis[0]))}",
                f"y110 = {self._fmt(float(slide_axis[1]))}",
                f"z110 = {self._fmt(float(slide_axis[2]))}",
                "w110 = 1",
                "x111 = 1", "y111 = 0", "z111 = 0", "w111 = 0",
            ])
        else:
            lines.extend(["x101 = 0", "x102 = 0", "x103 = 0", "x104 = 0",
                          "x105 = 0", "x106 = 0", "x108 = 0", "x109 = 0",
                          "x110 = 0", "x111 = 0"])
        lines.extend([
            f"cs-t67 = ResourceDragPinnedComponentInfo_{cn}_{ns}",
            f"cs-t68 = ResourceDragJiggleParams_{cn}_{ns}",
            f"cs-t65 = ResourceDragJiggleZoneIDs_{cn}_{ns}",
            f"cs-t66 = ResourceDragJiggleZoneWeights_{cn}_{ns}",
            # 逐组件屏幕状态/进度：本组件只读自己的（由本组件 UpdateScreenJiggle 段写），
            # 不得读共享副本，否则拖动组件 A 的几何会跟着组件 B 的状态走（或多组件饿死）。
            f"cs-t71 = ResourceDragJiggleScreenState_{cn}_{ns}",
            f"cs-t73 = ResourceDragPathVectors_{ns}",
            f"cs-t74 = ResourceDragPathProgressState_{cn}_{ns}",
            f"cs-t75 = ResourceDragZoneParams_{ns}",
            f"cs-u6 = ResourceDragJiggleState_{cn}_{ns}",
        ])
        if collision_grid:
            lines.extend([
                f"cs-t76 = ResourceDragColliderPoints_{cn}_{ns}",
                f"cs-t79 = ResourceDragColliderVertexL0_{cn}_{ns}",
            ])
            if comp.get("collision_trigrid"):
                lines.append(f"cs-t83 = ResourceDragColliderTriGrid_{cn}_{ns}")
                lines.append(f"cs-t84 = ResourceDragColliderTriGridTris_{cn}_{ns}")
                lines.append(f"cs-t87 = ResourceDragColliderTriGridVerts_{cn}_{ns}")
                lines.append(f"cs-u3 = ResourceDragJiggleSlideState_{cn}_{ns}")
                lines.append(f"cs-t86 = ResourceDragJiggleBlendTyped_{cn}_{ns}")
                lines.append(f"cs-u2 = ResourceDragJiggleBlendOut_{cn}_{ns}")
                # 碰撞面跟随动画（t22）：碰撞三角形顶点要按「当帧骨架」蒙皮，否则碰撞面留在
                # 导出姿态，角色一摆腿顶点就穿过薄壳（实测漂移 >1 mm 即穿，gap 只有 0.37~1.74 mm）。
                # 合并骨架是 s1/s2 ping-pong：本件 deform 段按「自己的」 $zz_ms_occ_<cid> 选一份
                # 写入（occ==1 → s1、occ==2 → s2），而拖拽 CS 在渲染 draw 上执行 ⇒ 绑定必须用
                # 同一条计数器、同一套槽位口径。计数器每帧被 [Present] 清零，本件 deform 段
                # 还没跑时是 0 ⇒ 只有 2 对应 s2，0/1 都按 s1（旧口径 occ == 1 把 0 判成 s2，
                # 读到上一帧第 2 次出现留下的旧值或全零骨架）。
                # 组号/cid 一律从产物推导：猜错组号会 ref 一个不存在的资源，整个 ini 加载失败。
                # 非合并产物既没有 [ResourceZZMergedSkeleton_G<n>_s<k>] 也没有出现次计数器，
                # 那本就不该有 t88 绑定 ⇒ 静默整块跳过（不加这条门控时每个非合并部件都会
                # 走一次「推导不出」的告警，文案在非合并场景是误导）。
                if self._is_merged_skeleton_product(sections):
                    skeleton_group, skeleton_occ, skeleton_reason = self._merged_skeleton_binding(
                        sections, comp
                    )
                    if skeleton_group is None or skeleton_occ is None:
                        _warn_missing_merged_skeleton_binding(
                            comp.get("comp_name", ""), skeleton_reason
                        )
                    else:
                        lines.append(f"\tif $zz_ms_occ_{skeleton_occ} == 2")
                        lines.append(
                            f"\t\tcs-t88 = ref ResourceZZMergedSkeleton_G{skeleton_group}_s2"
                        )
                        lines.append("\telse")
                        lines.append(
                            f"\t\tcs-t88 = ref ResourceZZMergedSkeleton_G{skeleton_group}_s1"
                        )
                        lines.append("\tendif")
        surface = comp.get("collision_surface") if collision_grid else None
        lines.append(f"x115 = {1 if surface else 0}")
        lines.extend([
            "",
            f"ResourceDragJiggleTempVB0_{cn}_{ns} = vb0",
            "cs-t24 = vb0",
            f"cs-u5 = copy ResourceDragJiggleTempVB0_{cn}_{ns}",
            "",
            f"Dispatch = {dispatch_n}, 1, 1",
            "",
        ])
        if surface:
            sections.update(surface_shader_sections(f"{cn}_{ns}", int(surface["leaves"]),
                                                   int(surface["face_count"]), dispatch_n, RES_SHADER_DIR))
            lines.extend(f"run = CustomShaderDragCollision{stage}{cn}_{ns}"
                         for stage in ("Leaves", "Roots", "Surface", "Apply"))
        if comp.get("collision_trigrid"):
            lines.append(f"ResourceDragJiggleBlendVB_{cn}_{ns} = copy ResourceDragJiggleBlendOut_{cn}_{ns}")
        lines.extend([
            "vb0 = null",
            f"ResourceDragJiggleTempVB0_{cn}_{ns} = copy cs-u5",
            "cs-u5 = null", "cs-u6 = null", "cs-t71 = null",
            "cs-u3 = null", "cs-u2 = null", "cs-t88 = null",
            "",
            "post x69 = $back_x69", "post y69 = $back_y69",
            "post z69 = $back_z69", "post w69 = $back_w69",
            "post x70 = $back_x70", "post y70 = $back_y70",
            "post z70 = $back_z70", "post w70 = $back_w70",
            "post x72 = $back_x72", "post y72 = $back_y72",
            "post z72 = $back_z72", "post w72 = $back_w72",
            "post x73 = $back_x73", "post y73 = $back_y73",
            "post z73 = $back_z73", "post w73 = $back_w73",
        ])
        sections[sec] = lines

    # ---- PinComponent{Comp} ----

    def _emit_pin_component_section(self, sections, comp, ns):
        cn = comp["comp_name"]
        sec = f"[CustomShaderDragPinComponent{cn}_{ns}]"
        if sec in sections:
            return
        sections[sec] = [
            f"cs = {RES_SHADER_DIR}/rzm_pin_detected.hlsl",
            f"cs-u0 = ResourceDragComponentDetect_{cn}_{ns}",
            f"cs-u1 = ResourceDragPinnedComponentID_{cn}_{ns}",
            f"cs-u2 = ResourceDragPinnedComponentInfo_{cn}_{ns}",
            f"cs-u3 = ResourceDragComponentZoneOut_{cn}_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null", "post cs-u3 = null",
        ]

    # ---- 全局 Pin（槽 10 注入光标 x24..w24）----

    def _emit_pin_detected_section(self, sections, ns):
        sec = f"[CustomShaderDragPinDetected_{ns}]"
        if sec in sections:
            return
        sections[sec] = [
            f"cs = {RES_SHADER_DIR}/rzm_pin_detected.hlsl",
            "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            f"cs-u0 = ResourceDragDetectID_{ns}",
            f"cs-u1 = ResourceDragPinnedDetectID_{ns}",
            f"cs-u2 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-u3 = ResourceDragZoneOut_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-u2 = null", "post cs-u3 = null",
        ]

    # ---- UpdateScreenJiggle（y72=1.0 非 mult_radius，照原作不对称）----

    def _emit_update_screen_jiggle_section(self, sections, ns, comp=None):
        """发射 UpdateScreenJiggle 段。

        ``comp is None`` → 全局共享段：读共享赢家 ResourceDragPinnedDetectInfo_{ns}，
        写共享 ResourceDragJiggleScreenState_{ns}/PathProgressState_{ns}（手部、光标
        预览、PresentHand 按设计消费这两条共享缓冲，必须保留）。
        ``comp`` 给定 → 该组件的独立段：读本组件赢家 ResourceDragPinnedComponentInfo_<C>，
        写本组件独立的屏幕状态/进度缓冲，供本组件 Jiggle 的 cs-t71/cs-t74 读取。多个
        组件各自成链，拖拽其一只动其一的几何。
        """
        cn = comp["comp_name"] if comp is not None else None
        sec = (f"[CustomShaderDragUpdateScreenJiggle_{ns}]" if cn is None
               else f"[CustomShaderDragUpdateScreenJiggle_{cn}_{ns}]")
        if comp is None and sec in sections:
            return
        pinned_info = (f"ResourceDragPinnedDetectInfo_{ns}" if cn is None
                       else f"ResourceDragPinnedComponentInfo_{cn}_{ns}")
        screen_state = (f"ResourceDragJiggleScreenState_{ns}" if cn is None
                        else f"ResourceDragJiggleScreenState_{cn}_{ns}")
        path_state = (f"ResourceDragPathProgressState_{ns}" if cn is None
                      else f"ResourceDragPathProgressState_{cn}_{ns}")
        lines = [
            "local $LLScreenCursorXPast", "local $LLScreenCursorYPast", "local $LLScreenWasMouseDown",
            "",
            "if $isMouseButtonDown == 1",
            "\tif $LLScreenWasMouseDown == 0",
            "\t\t$LLScreenCursorXPast = $cursorX", "\t\t$LLScreenCursorYPast = $cursorY",
            "\tendif",
            "\t$LLScreenWasMouseDown = 1", "\tw67 = 1",
            "else",
            "\t$LLScreenWasMouseDown = 0", "\t$LLScreenCursorXPast = 0", "\t$LLScreenCursorYPast = 0", "\tw67 = 0",
            "endif",
            "",
            f"cs = {RES_SHADER_DIR}/rzm_jiggle_screen_state.hlsl",
            "x67 = $LLScreenCursorXPast", "y67 = $LLScreenCursorYPast",
            "x68 = 0.25", "y68 = 1.0", "z68 = 1.5", "w68 = 1.0",
            "x69 = $cursorX", "y69 = $cursorY", "z69 = $screenW", "w69 = $screenH",
            "x70 = " + self._fmt(self.phys_grab_damping),
            "y70 = " + self._fmt(self.phys_grab_spring),
            "z70 = " + self._fmt(self.phys_release_damping),
            "w70 = " + self._fmt(self.phys_release_spring),
            "x71 = 0.50", "y71 = " + self._fmt(self.phys_target_follow),
            "z71 = 1.00", "w71 = " + self._fmt(self.phys_release_kick),
            "x72 = 0", "y72 = 1.0",  # 照原作不对称：此处 y72=1.0 非 mult_radius
            "z72 = " + self._fmt(self.mult_strength),
            "w72 = " + self._fmt(self.mult_spring),
            "x73 = " + self._fmt(self.mult_damping),
            "y73 = 1.00", "z73 = 1.00",  # depth_pull 默认 1.0
            f"x97 = $ssmtdrag_release_boost_{ns}",
            f"y97 = $ssmtdrag_release_decay_{ns}",
            f"x76 = $ssmtdrag_delta_time_{ns}",
            f"y76 = $ssmtdrag_sim_speed_{ns}",
            f"z76 = $ssmtdrag_max_step_{ns}",
            "x84 = " + (f"$ssmtdrag_poke_sign_{ns}" if self.enable_poke else "0"),
            "y84 = " + (f"$ssmtdrag_poke_hold_mult_{ns}" if self.enable_poke else "0"),
            "z84 = " + (f"$ssmtdrag_poke_hold_frames_{ns}" if self.enable_poke else "0"),
            "w84 = 0",  # 蓄力禁用
            "x119 = 0",
            f"y119 = {self._zone_capacity(self._collect_enabled_zone_entries())}",
        ]
        lines.extend([
            f"cs-t67 = {pinned_info}",
            f"cs-t75 = ResourceDragZoneParams_{ns}",
            f"cs-u0 = {screen_state}",
            f"cs-u1 = {path_state}",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null", "post cs-u1 = null", "post cs-t67 = null",
        ])
        sections[sec] = lines

    # ---- ShapeKeyDrive：仅命中模式下按鼠标位移 / 点击档位写入驱动缓冲 ----

    def _emit_shapekey_drive_section(self, sections, ns):
        sec = f"[CustomShaderDragShapeKeyDrive_{ns}]"
        # 无条件重发（评审 D1，与 _emit_jiggle_section 同等待遇）：内容确定性幂等，
        # 重导出必须覆盖旧形态——旧段无 cs-u5 锁存绑定会与新 hlsl 错配（u5 未绑定 →
        # 锁存链死）；读入侧另按内容特征剥离旧段（_remove_existing_legacy_shapekey_sections），
        # 此处无既有段跳过守卫，保证当前形态恒胜出。
        drag_mode_var = self._runtime_variable_names(ns)[0]
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_shapekey_drive.hlsl",
            f"x76 = $ssmtdrag_delta_time_{ns}",
            f"y76 = $ssmtdrag_sim_speed_{ns}",
            f"z76 = $ssmtdrag_max_step_{ns}",
            f"z77 = {drag_mode_var}",
            f"w77 = $ssmtdrag_lmb_down_{ns}",
            f"x78 = $ssmtdrag_x_down_{ns}",
            "x79 = $ssmtdrag_shapekey_dy_" + ns,
            "y79 = $ssmtdrag_shapekey_dx_" + ns,
            "x80 = " + self._fmt(self.shapekey_drive_move_sensitivity),
        ]
        seed_entries = self._click_export_seed_entries() if self._feature_var() else []
        if seed_entries:
            # 冷启动播种（F4/ClickExport 链）：x81=条目数，从 82 起每条
            # (x=区域, y=导出变量当前值)。
            lines.append(f"y80 = $ssmtdrag_seed_pending_{ns}")
            lines.append(f"x81 = {len(seed_entries)}")
            for seed_idx, (seed_zone, seed_var) in enumerate(seed_entries):
                lines.append(f"x{82 + seed_idx} = {seed_zone}")
                lines.append(f"y{82 + seed_idx} = {seed_var}")
        else:
            # 不发射播种链时必须显式写 0：槽位 80/81 与变量同步段的取值区
            # （81..89）重叠，残留值可能让驱动 CS 走进播种分支（读到脏条目）。
            lines.append("y80 = 0")
            lines.append("x81 = 0")
        lines.extend([
            f"cs-t67 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-t68 = ResourceDragShapeKeyZoneStageCounts_{ns}",
            f"cs-u0 = ResourceDragShapeKeyDrive_{ns}",
            f"cs-u1 = ResourceDragShapeKeyDir_{ns}",
            f"cs-u2 = ResourceDragShapeKeyClickCount_{ns}",
            f"cs-u3 = ResourceDragShapeKeyActiveDir_{ns}",
            f"cs-u4 = ResourceDragShapeKeyClickCountF_{ns}",
            f"cs-u5 = ResourceDragShapeKeyDragLatch_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-t67 = null",
            "post cs-t68 = null",
            "post cs-u0 = null",
            "post cs-u1 = null",
            "post cs-u2 = null",
            "post cs-u3 = null",
            "post cs-u4 = null",
            "post cs-u5 = null",
        ])
        sections[sec] = lines

    # ---- ShapeKeyVarSync：变量变化时把当前强度实时写入 ShapeKeyDrive 缓冲 ----

    def _emit_shapekey_var_sync_section(self, sections, ns):
        bindings = self._drag_drive_var_sync_bindings()
        if not bindings:
            return
        sec = f"[CustomShaderDragShapeKeyVarSync_{ns}]"
        # 无条件重发（评审 D1，与 _emit_jiggle_section 同等待遇）：内容确定性幂等，
        # 重导出覆盖旧形态（旧段无 cs-u5 锁存绑定，读入侧已剥离，见
        # _remove_existing_legacy_shapekey_sections）
        drag_mode_var = self._runtime_variable_names(ns)[0]
        # 值区固定 81..89、握手模式区固定 90..98，各容纳 36 个绑定且不重叠。
        # 门控输入固定 IniParams[75]（低于驱动 CS 段，不与其他段冲突）
        lines = [
            f"cs = {RES_SHADER_DIR}/rzm_shapekey_var_sync.hlsl",
            f"x75 = {drag_mode_var}",
            f"y75 = $ssmtdrag_skheld_{ns}",
            f"z75 = $ssmtdrag_drawn_{ns}",
            "w75 = $inputMode",
        ]
        for i, (var_name, _slot, _zone, _nd_stage) in enumerate(bindings):
            lines.append(
                f"{'xyzw'[i % 4]}{VAR_SYNC_VALUE_BASE + i // 4} = {var_name}"
            )
        # mode: 0=空闲，1=CPU 从缓冲拉取（抑制回声），2=变量待确认（强制推送）。
        for i, (_var_name, _slot, _zone, _nd_stage) in enumerate(bindings):
            lines.append(
                f"{'xyzw'[i % 4]}{VAR_SYNC_MODE_BASE + i // 4} = "
                f"$ssmtdrag_skmode_{ns}_{i}"
            )
        lines.extend([
            f"cs-t67 = ResourceDragPinnedDetectInfo_{ns}",
            f"cs-t69 = ResourceDragShapeKeyVarSyncMap_{ns}",
            f"cs-u0 = ResourceDragShapeKeyDrive_{ns}",
            f"cs-u1 = ResourceDragShapeKeyClickCount_{ns}",
            f"cs-u2 = ResourceDragShapeKeyVarPrev_{ns}",
            f"cs-u3 = ResourceDragShapeKeyClickCountF_{ns}",
            f"cs-u4 = ResourceDragShapeKeyZoneActive_{ns}",
            # 驱动 CS 的绑定锁存（独立单槽资源）是 ZoneActive 的单一事实源
            f"cs-u5 = ResourceDragShapeKeyDragLatch_{ns}",
            "dispatch = 1, 1, 1",
            "post cs-t67 = null",
            "post cs-t69 = null",
            "post cs-u0 = null",
            "post cs-u1 = null",
            "post cs-u2 = null",
            "post cs-u3 = null",
            "post cs-u4 = null",
            "post cs-u5 = null",
        ])
        sections[sec] = lines

    # ---- ShapeKeyVarReadback：store 回读命令列表（[Present] pre run、boot 门控）----

    def _emit_shapekey_var_readback_command_list(self, sections, ns):
        bindings = self._drag_drive_var_sync_bindings()
        if not bindings:
            return
        sec = f"[CommandListDragShapeKeyVarReadback_{ns}]"
        # 无条件重发（评审 D1，与 _emit_jiggle_section 同等待遇）：内容确定性幂等，
        # 重导出覆盖旧形态（bindings 由当前节点树决定，存在旧段时也必须刷新）
        # store 放在命名命令列表内（[Present] 以 pre run 调用、boot 门控）。
        # 写法：store = $var, <资源名>, <float 索引>——不能带 ref 关键字！
        # 分词器把 ref 当独立 token 交给 GetTarget，ParseTarget 无此分支 →
        # Unknown target: ref，整条 store 行在加载期被静默丢弃（GIMI 实证有效
        # 写法为 store = $health, ps-cb0, 33，裸目标）。
        # 变量优先 + 确认握手：变量变化后 pending=1/mode=2，GPU 每帧强制推送，
        # 直到 store 回读追平 prev 才结束；仅区域实际激活且无 pending 时允许缓冲
        # 回拉变量。这样 store 的延迟旧值不会覆盖刚写入的新变量。
        #
        # ---- 回读最小化（只在"值可能变了"时回读）----
        # store 是 GPU→CPU 同步，属整条链里最贵的操作。旧实现每帧对每个绑定
        # 固定做 2 次 store（ZoneActive + Drive），即 2N 次/帧，绝大多数帧什么都没
        # 变。现在：
        #   * 区域激活不再逐绑定回读 ZoneActive（N 次），改为在"按住 / 松开沿"上
        #     回读一次**绑定锁存**（单槽，驱动 CS 当帧写、本 CL 之前刚跑过，最新），
        #     再由 CPU 推导每个绑定是否激活 → 0~1 次 store/帧；
        #   * 驱动槽只在该绑定"拖拽激活"或"等待 store 确认"时回读 → 空闲帧 0 次；
        #   * 变量只在**真的变了**时才回写（拖拽拉动缓冲 → 值变了才写变量）；
        #   * 同步 CS 的 dispatch 由 skany 门控（只有绑定处于 mode!=0 才需要跑）。
        lines = [
            # 本帧是否需要 dispatch 变量→缓冲同步 CS（有绑定 mode!=0 时置 1）
            f"$ssmtdrag_skany_{ns} = 0",
            # 锁存只在按住/松开沿回读；跳过时必须显式清 0，否则陈旧绑定会让
            # CPU 一直以为拖拽还在进行（每帧回拉一个已经不动的缓冲值）
            f"$ssmtdrag_sklatch_{ns} = 0",
            f"if $ssmtdrag_skheld_{ns} == 1 || $ssmtdrag_skheldprev_{ns} == 1",
            f"\tstore = $ssmtdrag_sklatch_{ns}, ResourceDragShapeKeyDragLatch_{ns}, 0",
            "endif",
            f"$ssmtdrag_skheldprev_{ns} = $ssmtdrag_skheld_{ns}",
        ]
        for i, (var_name, slot, zone, _nd_stage) in enumerate(bindings):
            active = f"$ssmtdrag_skact_{ns}_{i}"
            rb = f"$ssmtdrag_skrb_{ns}_{i}"
            prev = f"$ssmtdrag_skprev_{ns}_{i}"
            pending = f"$ssmtdrag_skpending_{ns}_{i}"
            mode = f"$ssmtdrag_skmode_{ns}_{i}"
            lines.extend([
                # 由锁存推导本绑定的区域激活（纯 CPU，零回读）。锁存编码 = 区域id+1，
                # 与 ZoneActive 同源，整数取值比较精确。
                f"{active} = 0",
                f"if $ssmtdrag_sklatch_{ns} == {zone + 1}",
                f"\t{active} = 1",
                "endif",
                # 只有"拖拽激活"或"等 store 确认"时才回读驱动槽
                f"if {active} >= 1 || {pending} == 1",
                f"\tstore = {rb}, ResourceDragShapeKeyDrive_{ns}, {slot}",
                "endif",
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
                # 拖拽激活：缓冲值**变了才**回写变量（不再每帧无脑覆盖）
                f"\t{mode} = 1",
                f"\tif {rb} != {prev}",
                f"\t\t{var_name} = {rb}",
                f"\t\t{prev} = {rb}",
                "\tendif",
                "else",
                f"\t{mode} = 0",
                "endif",
                # mode=1 也需要 CS 跑一趟：让它把 VarSyncPrev 追平到已拉取值，
                # 否则之后 CS 再次运行时会把这个值当"变量变化"回声写回缓冲
                f"if {mode} != 0",
                f"\t$ssmtdrag_skany_{ns} = 1",
                "endif",
            ])
        sections[sec] = lines

    # ---- CommandList：PinDetected（boot-clear + dt 钳制 + 门槛）/ Viewport / Cursor ----

    def _emit_command_lists(self, sections, components, ns):
        drag_mode_var, _ui_detected_var, _ui_zone_var = self._runtime_variable_names(ns)
        # PinDetected
        pin_sec = f"[CommandListDragPinDetected_{ns}]"
        # 无条件重发（评审 D1，与 _emit_jiggle_section 同等待遇）：内容确定性幂等，
        # 重导出必须覆盖旧形态——旧段缺 boot/失臂锁存清零（F1 假绑/F2 复活回潮）。
        # 先剔除旧段保证恒走重建路径；读入侧另按内容特征剥离
        #（_remove_existing_legacy_shapekey_sections）。
        sections.pop(pin_sec, None)
        if pin_sec not in sections:
            lines = [
                # boot-clear：RWBuffer 初始内容未定义，首帧垃圾会假命中/假位移
                f"if $ssmtdrag_booted_{ns} == 0",
                f"\tclear = ResourceDragDetectID_{ns} 0.0",
                f"\tclear = ResourceDragPinnedDetectID_{ns}",
                f"\tclear = ResourceDragPinnedDetectInfo_{ns}",
                f"\tclear = ResourceDragZoneOut_{ns} 0.0",
                f"\tclear = ResourceDragJiggleScreenState_{ns} 0.0",
                f"\tclear = ResourceDragPathProgressState_{ns} 0.0",
                f"\tclear = ResourceDragViewportFrameAPI_{ns} 0.0",
            ]
            # 逐组件屏幕状态/进度同样要清：不清则首帧残留垃圾会让某组件假命中/假位移
            for comp in components:
                lines.extend([
                    f"\tclear = ResourceDragJiggleScreenState_{comp['comp_name']}_{ns} 0.0",
                    f"\tclear = ResourceDragPathProgressState_{comp['comp_name']}_{ns} 0.0",
                ])
            if any(int(comp.get("object_count") or 0) for comp in components):
                lines.append(f"\tclear = ResourceDragObjectVis_{ns} 0.0")
            if self._feature_skd():
                lines.append(f"\tclear = ResourceDragShapeKeyDrive_{ns} 0.0")
                lines.append(f"\tclear = ResourceDragShapeKeyDir_{ns} 0.0")
                # 绑定锁存清零：编码 0=未绑定，boot 后不得假绑 zone 0（评审 F1）
                lines.append(f"\tclear = ResourceDragShapeKeyDragLatch_{ns} 0.0")
                lines.append(f"\tclear = ResourceDragShapeKeyClickCount_{ns}")
                lines.append(f"\tclear = ResourceDragShapeKeyClickCountF_{ns} 0.0")
                lines.append(f"\tclear = ResourceDragShapeKeyActiveDir_{ns}")
                if self._feature_var() and self._click_export_seed_entries():
                    # 冷启动播种：缓冲随游戏关闭销毁、persist 变量仍有上次值——
                    # boot 清零后由驱动 CS 把导出变量值写回点击计数（含镜像与档位槽）
                    lines.append(f"\t$ssmtdrag_seed_pending_{ns} = 1")
                if self._feature_var() and self._drag_drive_var_sync_bindings():
                    # prev 值缓冲/激活标志随 boot 一并清零：persist 变量非 0 时首帧即触发同步写入
                    lines.append(f"\tclear = ResourceDragShapeKeyVarPrev_{ns} 0.0")
                    lines.append(f"\tclear = ResourceDragShapeKeyZoneActive_{ns} 0.0")
                    for i in range(len(self._drag_drive_var_sync_bindings())):
                        lines.append(f"\t$ssmtdrag_skpending_{ns}_{i} = 0")
                        lines.append(f"\t$ssmtdrag_skmode_{ns}_{i} = 0")
            if self.enable_hand_cursor:
                lines.append(f"\tclear = ResourceDragJiggleCursorPreview_{ns} 0.0")
            for comp in components:
                cn = comp["comp_name"]
                lines.extend([
                    f"\tclear = ResourceDragComponentDetect_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragPinnedComponentID_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragPinnedComponentInfo_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragComponentZoneOut_{cn}_{ns} 0.0",
                    f"\tclear = ResourceDragJiggleState_{cn}_{ns} 0.0",
                ])
            lines.extend([
                f"\t$ssmtdrag_booted_{ns} = 1",
                "endif",
                "",
            ])
            if self._feature_var() and self._click_export_seed_entries():
                lines.extend([
                    # 冷启动恢复不能依赖 Alt/命中门控；boot 清零后立即从 persist 变量播种。
                    f"if $ssmtdrag_seed_pending_{ns} == 1",
                    f"\trun = CustomShaderDragShapeKeyDrive_{ns}",
                    f"\t$ssmtdrag_seed_pending_{ns} = 0",
                    "endif",
                    "",
                ])
            lines.extend([
                # t149 变量追溯审计：此处原发
                #     local $ssmtdrag_detect_next_time
                #     local $ssmtdrag_detect_interval = 0.25
                # 以及
                #     if $isMouseButtonDown == 1
                #         $ssmtdrag_detect_next_time = time
                #     endif
                # 两者在产物 ini 里**只写不读**（无任何条件/表达式/IniParams 消费；
                # 也未被任何 hlsl 读），且表达式 `0.25` / `time` 无副作用 ⇒ 删除是
                # 行为等价的（不改变任何其它命令的求值）。dt 钳制用的是
                # `$ssmtdrag_delta_time_{ns}`，与它们无关。
                # dt 钳制 [0.001, 0.100]（time 单位分钟 → 秒）
                f"if $ssmtdrag_prev_time_{ns} == 0",
                f"\t$ssmtdrag_delta_time_{ns} = 0.0166667",
                "else",
                f"\t$ssmtdrag_delta_time_{ns} = (time - $ssmtdrag_prev_time_{ns}) * 60.0",
                f"\tif $ssmtdrag_delta_time_{ns} > 0.100",
                f"\t\t$ssmtdrag_delta_time_{ns} = 0.100",
                f"\telif $ssmtdrag_delta_time_{ns} < 0.001",
                f"\t\t$ssmtdrag_delta_time_{ns} = 0.001",
                "\tendif",
                "endif",
                f"$ssmtdrag_prev_time_{ns} = time",
                "",
                f"if {drag_mode_var} >= 1 && $inputMode == 0 && $ssmtdrag_mode_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
                f"\t$ObjectDetectAllowed_{ns} = 1",
            ])
            # 根源卫生修复：UpdateScreenJiggle 等 CustomShader 会写共享 IniParams 槽
            # 69/70/72/73/97（TTL 合成器读同名槽：alpha/pass/depth/mask/slot）。
            # 这些槽位是跨 mod 共享的：一旦写入不还原，TTL 的 Push/PopIniParams
            # 会把污染值保存并原样还原，垃圾跨帧固化（曾导致 86ec 身份探针读到
            # 被污染的 SLOT_ID=1.05 而把判决写进错误 cell，整条合成链失效）。
            # 先存后还，杜绝污染外泄。
            for _slot in ("69", "70", "72", "73", "97"):
                for _comp in ("x", "y", "z", "w"):
                    lines.append(f"\t$ssmtdrag_ipbak_{_comp}{_slot} = {_comp}{_slot}")
            lines.append(f"\trun = CustomShaderDragPinDetected_{ns}")
            for comp in components:
                lines.append(f"\trun = CustomShaderDragPinComponent{comp['comp_name']}_{ns}")
            lines.append(f"\trun = CustomShaderDragUpdateScreenJiggle_{ns}")
            # 逐组件独立段紧随共享段：各组件在自己的赢家缓冲上推进屏幕状态/路径进度，
            # 后面的 Jiggle 各自读自己的那一份（共享段仍在，手部/光标继续消费）。
            for comp in components:
                lines.append(
                    f"\trun = CustomShaderDragUpdateScreenJiggle_{comp['comp_name']}_{ns}")
            if self._feature_skd():
                lines.append(f"\trun = CustomShaderDragShapeKeyDrive_{ns}")
            for _slot in ("69", "70", "72", "73", "97"):
                for _comp in ("x", "y", "z", "w"):
                    lines.append(f"\t{_comp}{_slot} = $ssmtdrag_ipbak_{_comp}{_slot}")
            lines.append("else")
            lines.append(f"\t$ObjectDetectAllowed_{ns} = 0")
            if self._feature_skd():
                # 失臂（松 Alt/undraw/输入模式切换）时驱动 CS 被内层门控跳过、
                # 锁存无人清理：在此整清锁存资源，防止下次臂动+按住时复活
                # 旧绑定（评审 F2；语义对齐面板侧「松开 Alt 结束绑定」）。
                # 注意：本 else 只在 drag_mode>=1 时才执行（PinDetected 整条由
                # 模式门控）；模式直跳 0 由 [Present] 块的终态 else 覆盖（G1）。
                # 只清独立锁存资源，不波及 prev-press 槽（防重新臂动伪按下沿）。
                lines.append(f"\tclear = ResourceDragShapeKeyDragLatch_{ns} 0.0")
            lines.extend([
                "endif",
            ])
            sections[pin_sec] = lines

        # ViewportUpdate
        vp_sec = f"[CommandListDragViewportUpdate_{ns}]"
        if vp_sec not in sections:
            sections[vp_sec] = [
                "$ssmtdrag_viewport_valid = 0",
                "$screenW = 1", "$screenH = 1",
                "if window_width > 0 && window_height > 0 && window_width <= 8192 && window_height <= 8192",
                "\t$screenW = window_width", "\t$screenH = window_height", "\t$ssmtdrag_viewport_valid = 1",
                "elif rt_width > 0 && rt_height > 0 && rt_width <= 8192 && rt_height <= 8192",
                "\t$screenW = rt_width", "\t$screenH = rt_height", "\t$ssmtdrag_viewport_valid = 1",
                "elif res_width > 0 && res_height > 0 && res_width <= 8192 && res_height <= 8192",
                "\t$screenW = res_width", "\t$screenH = res_height", "\t$ssmtdrag_viewport_valid = 1",
                "endif",
            ]

        # CursorUpdate
        cu_sec = f"[CommandListDragCursorUpdate_{ns}]"
        if cu_sec not in sections:
            sections[cu_sec] = [
                f"run = CommandListDragViewportUpdate_{ns}",
                "if $inputMode == 0 && $ssmtdrag_viewport_valid == 1",
                "\tif cursor_x > 0 && cursor_y > 0 && cursor_x < 1 && cursor_y < 1",
                "\t\t$cursorX = cursor_x", "\t\t$cursorY = 1.0 - cursor_y",
                "\telif cursor_window_x > 0 && cursor_window_y > 0 && cursor_window_x < 1 && cursor_window_y < 1",
                "\t\t$cursorX = cursor_window_x", "\t\t$cursorY = 1.0 - cursor_window_y",
                "\telif cursor_screen_x >= 0 && cursor_screen_y >= 0 && cursor_screen_x <= $screenW && cursor_screen_y <= $screenH",
                "\t\t$cursorX = cursor_screen_x / $screenW", "\t\t$cursorY = 1.0 - cursor_screen_y / $screenH",
                "\telse",
                "\t\t$cursorX = -1", "\t\t$cursorY = -1",
                "\tendif",
                "endif",
                "if $ssmtdrag_viewport_valid == 0",
                "\t$cursorX = -1", "\t$cursorY = -1",
                "endif",
                "if $inputMode == 0",
                "\t$cursorX = $cursorX * $screenW", "\t$cursorY = $cursorY * $screenH",
                "endif",
                "x24 = $cursorX", "y24 = $cursorY", "z24 = $screenW", "w24 = $screenH",
            ]

    # ===================================================================
    # 手型光标（S8）：资源声明 + 绘制段（对照原作 ResourceLLHand* /
    # CustomShaderLLUpdateJiggleCursorPreview / LLJiggleCursor / LLPresentHand×4）
    # ===================================================================

    def _emit_hand_resources(self, sections, ns):
        res = RES_SHADER_DIR
        hand_resources = {
            # 手部屏幕位置暂存（UpdateJiggleCursorPreview 每帧写入，手部 VS 读取）
            f"[ResourceDragJiggleCursorPreview_{ns}]": [
                "type = RWBuffer", "format = R32G32B32A32_FLOAT", "array = 4",
            ],
            f"[ResourceDragHandActionVB_{ns}]": [
                "type = Buffer", "stride = 28", f"filename = {res}/HandAction.buf",
            ],
            f"[ResourceDragHandActionIB_{ns}]": [
                "type = Buffer", "format = R32_UINT", f"filename = {res}/HandAction.ib",
            ],
            f"[ResourceDragHandNoActionVB_{ns}]": [
                "type = Buffer", "stride = 28", f"filename = {res}/HandNoAction.buf",
            ],
            f"[ResourceDragHandNoActionIB_{ns}]": [
                "type = Buffer", "format = R32_UINT", f"filename = {res}/HandNoAction.ib",
            ],
            # 烘焙的平滑逐顶点法线（描边外扩用；索引与位置缓冲对齐）
            f"[ResourceDragHandActionNormal_{ns}]": [
                "type = Buffer", "format = R32G32B32_FLOAT", f"filename = {res}/HandAction_Normal.buf",
            ],
            f"[ResourceDragHandNoActionNormal_{ns}]": [
                "type = Buffer", "format = R32G32B32_FLOAT", f"filename = {res}/HandNoAction_Normal.buf",
            ],
        }
        for sec, lines in hand_resources.items():
            sections.setdefault(sec, lines)

    def _hand_param_lines(self, ns, outline):
        """PresentHand 四段共享的 IniParams 寄存器行（83/89-95；描边加 98，y98 为描边标记）。"""
        lines = [
            f"x83 = $ssmtdrag_hand_surface_clip_{ns}",
            f"y83 = $ssmtdrag_hand_surface_lift_{ns}",
            f"z83 = $ssmtdrag_hand_surface_softness_{ns}",
            f"x89 = $ssmtdrag_hand_center_x_{ns}",
            f"y89 = $ssmtdrag_hand_center_y_{ns}",
            f"z89 = $ssmtdrag_hand_center_z_{ns}",
            f"x90 = $ssmtdrag_hand_scale_{ns}",
            f"y90 = $ssmtdrag_hand_opacity_{ns}",
            f"x91 = $ssmtdrag_lmb_hold_fraction_{ns} / $ssmtdrag_hand_windup_time_{ns}",
            "y91 = time",
            f"z91 = $ssmtdrag_rmb_hold_fraction_{ns} / $ssmtdrag_hand_rmb_windup_time_{ns}",
            f"x92 = $ssmtdrag_hand_tilt_min_{ns}",
            f"y92 = $ssmtdrag_hand_tilt_rmb_min_{ns}",
            f"z92 = $ssmtdrag_hand_tilt_max_deg_{ns}",
            f"w92 = $ssmtdrag_hand_tilt_rmb_max_deg_{ns}",
            f"x93 = $ssmtdrag_hand_vibrate_threshold_{ns}",
            f"y93 = $ssmtdrag_hand_vibrate_period_slow_{ns}",
            f"z93 = $ssmtdrag_hand_vibrate_period_fast_{ns}",
            f"w93 = $ssmtdrag_hand_vibrate_amplitude_{ns}",
            f"x94 = $ssmtdrag_hand_upright_max_deg_{ns}",
            f"z94 = $ssmtdrag_hand_tilt_grab_max_deg_{ns}",
            f"x95 = $ssmtdrag_hand_reference_height_{ns}",
        ]
        if outline:
            lines.extend([
                f"x98 = $ssmtdrag_hand_outline_width_{ns}",
                "y98 = 1.0",
                f"z98 = $ssmtdrag_hand_outline_opacity_{ns}",
            ])
        else:
            lines.append("y98 = 0.0")
        return lines

    def _emit_hand_sections(self, sections, ns):
        # UpdateJiggleCursorPreview：抓取点/光标 → 手部屏幕位置（每帧先于手部绘制运行）
        preview_sec = f"[CustomShaderDragUpdateJiggleCursorPreview_{ns}]"
        if preview_sec not in sections:
            sections[preview_sec] = [
                f"cs = {RES_SHADER_DIR}/rzm_jiggle_cursor_preview.hlsl",
                f"cs-t67 = ResourceDragJiggleScreenState_{ns}",
                f"cs-t68 = ResourceDragPinnedDetectInfo_{ns}",
                f"cs-u0 = ResourceDragJiggleCursorPreview_{ns}",
                "dispatch = 1, 1, 1",
                "post cs-t67 = null", "post cs-t68 = null", "post cs-u0 = null",
            ]

        # JiggleCursor：十字准星（hand_debug == 2 时额外绘制）
        cursor_sec = f"[CustomShaderDragJiggleCursor_{ns}]"
        if cursor_sec not in sections:
            sections[cursor_sec] = [
                f"vs = {RES_SHADER_DIR}/rzm_jiggle_cursor.hlsl",
                f"ps = {RES_SHADER_DIR}/rzm_jiggle_cursor.hlsl",
                "blend = ADD SRC_ALPHA INV_SRC_ALPHA",
                "cull = none",
                "topology = triangle_strip",
                "o0 = set_viewport bb",
                f"vs-t67 = ResourceDragJiggleCursorPreview_{ns}",
                "Draw = 4, 0",
                "post vs-t67 = null",
            ]

        # PresentHand ×4：NoAction/Action × 填充/描边（描边 y98=1 先画垫底，只露轮廓边）
        for action in (False, True):
            for outline in (False, True):
                tag = ("Action" if action else "") + ("Outline" if outline else "")
                sec = f"[CustomShaderDragPresentHand{tag}_{ns}]"
                if sec in sections:
                    continue
                mesh = "Action" if action else "NoAction"
                lines = [
                    f"vs = {RES_SHADER_DIR}/rzm_jiggle_hand.hlsl",
                    f"ps = {RES_SHADER_DIR}/rzm_jiggle_hand.hlsl",
                    "blend = ADD SRC_ALPHA INV_SRC_ALPHA",
                    "cull = none",
                    "topology = triangle_list",
                    "o0 = set_viewport bb",
                    f"vs-t67 = ResourceDragJiggleCursorPreview_{ns}",
                    f"vs-t68 = ResourceDragJiggleScreenState_{ns}",
                    f"vs-t69 = ResourceDragHand{mesh}Normal_{ns}",
                ]
                lines.extend(self._hand_param_lines(ns, outline))
                lines.extend([
                    f"vb0 = ResourceDragHand{mesh}VB_{ns}",
                    f"ib = ResourceDragHand{mesh}IB_{ns}",
                    # 254 quads → 508 tris → 1524 indices（与手部 IB 资产一致）
                    "DrawIndexed = 1524, 0, 0",
                    "post vs-t67 = null", "post vs-t68 = null", "post vs-t69 = null",
                    "post vb0 = null", "post ib = null",
                ])
                sections[sec] = lines

    # =======================================================================
    # 视口探针系统：抓角色渲染 RT → 分析视口矩形 → ViewportFrameAPI
    #（子区域渲染/角色查看器时校正光标映射；全屏等比时探针产出恒等，无害）
    # =======================================================================

    def _emit_viewport_probe_sections(self, sections, ns):
        # 资源：ViewportSource/LayoutT0 为动态 ref 占位（运行时由钩子快照/探针赋值）
        probe_resources = {
            f"[ResourceDragViewportSource_{ns}]": [],
            f"[ResourceDragViewportLayoutT0_{ns}]": [],
            f"[ResourceDragViewportTempoO0_{ns}]": [],
            f"[ResourceDragViewportLayoutData_{ns}]": [
                "type = Texture2D", "mode = mono", "width = 8", "height = 1",
                "mips = 1", "array = 1", "msaa = 1", "msaa_quality = 0",
                "format = DXGI_FORMAT_R32G32B32A32_FLOAT",
                "bind_flags = render_target shader_resource",
            ],
        }
        for sec, lines in probe_resources.items():
            sections.setdefault(sec, lines)

        # ShaderOverride：拦截游戏 UI 绘制帧，armed 时跑探针（hash 为 ZZZ UI 帧，照原作）
        so = f"[ShaderOverrideDragViewportLayoutProbe_{ns}]"
        if so not in sections:
            sections[so] = [
                "hash = cdc90aee00e7900d",
                "allow_duplicate_hash = true",
                f"if $ssmtdrag_viewport_probe_armed_{ns} == 1",
                f"\tpost run = CustomShaderDragViewportLayoutProbe_{ns}",
                "endif",
            ]
        # 展示页窗口 quad：与 cdc90aee 同族（o0 变换逐条一致，仅 o3 不同——
        # 探针 GS 只消费 SV_Position，同一套复刻 VS 可直接复用）。
        # 展示页的角色窗口区域由它绘制，多钩一个 hash 提高窗口矩形的捕获率。
        so2 = f"[ShaderOverrideDragViewportLayoutProbe2_{ns}]"
        if so2 not in sections:
            sections[so2] = [
                "hash = 8509ae6f43c21eb0",
                "allow_duplicate_hash = true",
                f"if $ssmtdrag_viewport_probe_armed_{ns} == 1",
                f"\tpost run = CustomShaderDragViewportLayoutProbe_{ns}",
                "endif",
            ]

        # GS 探针：读 ViewportSource（角色 RT）→ 8×1 LayoutData
        probe_sec = f"[CustomShaderDragViewportLayoutProbe_{ns}]"
        if probe_sec not in sections:
            sections[probe_sec] = [
                f"ResourceDragViewportLayoutT0_{ns} = ref ps-t0",
                # 保存当前 o0，探针结束后原样还原（对齐 TTL steal 的做法）：
                # 否则 o0/viewport 会残留 8x1 的 LayoutData，污染同帧后续绘制，
                # 并让 TTL steal 的 ResourceTempo0 = ref o0 抓到被污染的目标。
                f"ResourceDragViewportTempoO0_{ns} = ref o0",
                "run = BuiltInCommandListUnbindAllRenderTargets",
                "blend = ADD ONE ZERO",
                "alpha = ADD ONE ZERO",
                f"vs = {RES_SHADER_DIR}/rzm_viewport_layout_vs.hlsl",
                f"gs = {RES_SHADER_DIR}/rzm_viewport_layout_probe.hlsl",
                f"ps = {RES_SHADER_DIR}/rzm_viewport_layout_probe.hlsl",
                f"gs-t0 = ResourceDragViewportLayoutT0_{ns}",
                f"ps-t0 = ResourceDragViewportLayoutT0_{ns}",
                f"gs-t67 = ResourceDragViewportSource_{ns}",
                f"ps-t67 = ResourceDragViewportSource_{ns}",
                "topology = triangle_list",
                f"o0 = set_viewport no_view_cache ResourceDragViewportLayoutData_{ns}",
                f"x88 = $ssmtdrag_viewport_probe_generation_{ns}",
                "if draw_type == 2",
                "\tdrawindexed = INDEX_COUNT, FIRST_INDEX, FIRST_VERTEX",
                "elif draw_type == 4",
                "\tdrawindexedinstanced = INDEX_COUNT, INSTANCE_COUNT, FIRST_INDEX, FIRST_VERTEX, FIRST_INSTANCE",
                "else",
                "\tdrawindexed = auto",
                "endif",
                "run = BuiltInCommandListUnbindAllRenderTargets",
                f"o0 = ref ResourceDragViewportTempoO0_{ns}",
                f"ResourceDragViewportTempoO0_{ns} = null",
            ]

        # Decode CS：LayoutData → ViewportFrameAPI（检测着色器 cs-t6 读取它映射光标）
        decode_sec = f"[CustomShaderDragViewportLayoutDecode_{ns}]"
        if decode_sec not in sections:
            sections[decode_sec] = [
                f"cs = {RES_SHADER_DIR}/rzm_viewport_layout_decode.hlsl",
                f"cs-t0 = ResourceDragViewportLayoutData_{ns}",
                f"cs-u0 = ResourceDragViewportFrameAPI_{ns}",
                f"x88 = $ssmtdrag_viewport_probe_generation_{ns}",
                "dispatch = 1, 1, 1",
                "post cs-t0 = null", "post cs-u0 = null",
            ]

    # =======================================================================
    # 钩子注入（ib= 之后、第一个 run=/drawindexed= 之前；跳过 ib=null）
    # =======================================================================

    def _inject_draw_hooks(self, sections, comp, ns):
        cn = comp["comp_name"]

        # 1) 物体显隐 flag：注入到每条绘制行（drawindexed=/run=）之前、其 if 块内部。
        #    分支执行才置位——条件求值留给 ini 引擎（活条件，手改即生效，零复制）。
        #    同段内按行号倒序插入避免索引位移；幂等防护同站点方案。
        name_to_id = comp.get("object_id_map") or {}
        sites = {}
        for part in comp["parts"]:
            for record in part.get("draw_records") or []:
                oid = name_to_id.get(self._record_object_key(record))
                if oid is None:
                    continue
                sites.setdefault(record["section"], []).append((record["line_index"], oid))
        for section, items in sites.items():
            lines = sections.get(section)
            if not lines:
                continue
            for line_idx, oid in sorted(items, key=lambda item: -item[0]):
                target = ""
                if 0 <= line_idx < len(lines):
                    target = (lines[line_idx] or "").strip().casefold()
                if not (target.startswith("drawindexed =") or target.startswith("run =")):
                    continue
                window = lines[max(0, line_idx - 4):line_idx]
                if any("$ssmtdrag_objvis_{}_{}".format(ns, oid) in (line or "") for line in window):
                    continue
                lines[line_idx:line_idx] = ["\t$ssmtdrag_objvis_{}_{} = 1".format(ns, oid)]
            sections[section] = lines

        # 2) 提升式 part 钩子（探针 + Bake + Detect + jiggle + vb0）
        for p_idx, part in enumerate(comp["parts"]):
            section = part["section"]
            lines = sections.get(section)
            if not lines:
                continue
            part_tag = f"{cn}P{p_idx}"
            hook_marker = f"DRAG HOOK BEGIN {part_tag}_{ns}"
            if any(hook_marker in line for line in lines):
                continue
            hook = self._build_hook_block(comp, p_idx, ns)
            insert_at = self._find_part_hook_insert_index(lines, part)
            new_lines = lines[:insert_at] + hook + lines[insert_at:]
            sections[section] = new_lines

        # 3) 已删除：TTL 既不建 copy 段，也不再另建命令列表。TTL 调用就地替换原段里
        #    那一笔绘制，上面第 2) 步的段级 part 钩子（vb0 换绑）天然作用在它上面，
        #    当年「命令列表内层 vb0 传播不到 run = 嵌套绘制」的缺口不复存在。

    @classmethod
    def _find_part_hook_insert_index(cls, lines, part):
        anchor_comment = part.get("hook_anchor_comment")
        if anchor_comment:
            occurrence = int(part.get("hook_anchor_occurrence", 0) or 0)
            matches = [
                index for index, line in enumerate(lines)
                if line.strip() == anchor_comment
            ]
            if occurrence < len(matches):
                index = matches[occurrence]
                # The anchor mesh comment may sit inside an if/else visibility block.
                # A hook placed right before it would be gated by that condition, so
                # when the base draw is hidden but a TTL/material copy of the same IB
                # is drawn, the only hook would not run and detection would be lost.
                # Move the hook above the enclosing conditional headers.
                while index > 0:
                    previous = lines[index - 1].strip()
                    if not previous:
                        index -= 1
                        continue
                    lowered = previous.casefold()
                    if lowered.startswith("if ") or lowered == "else" or lowered.startswith("else if"):
                        index -= 1
                        continue
                    break
                return index
        return cls._find_hook_insert_index(lines)

    @staticmethod
    def _find_hook_insert_index(lines):
        """ib= 绑定行之后、第一个 run=/drawindexed= 之前。"""
        ib_idx = -1
        for i, line in enumerate(lines):
            s = line.strip().lower()
            if s.startswith("ib ="):
                ib_idx = i
        # 在 ib 之后找第一个 run= 或 drawindexed=
        for i in range(ib_idx + 1, len(lines)):
            s = line.strip().lower()
            if s.startswith("run =") or s.startswith("drawindexed ="):
                return i
        # 没有 run/drawindexed（异常）：插到 ib 后一行
        return ib_idx + 1

    def _build_hook_block(self, comp, p_idx, ns, include_detect=True):
        drag_mode_var, _ui_detected_var, _ui_zone_var = self._runtime_variable_names(ns)
        cn = comp["comp_name"]
        part_tag = f"{cn}P{p_idx}"
        temp_vb0 = f"ResourceDragJiggleTempVB0_{cn}_{ns}"
        last_dispatch = f"$ssmtdrag_last_dispatch_{cn}_{ns}"
        lines = [f"\t; --- DRAG HOOK BEGIN {part_tag}_{ns} ---"]
        if comp.get('cloth_bind'):
            lines.append('\trun = ' + comp['cloth_bind'])
        if self.enable_viewport_probe:
            # 视口探针快照：armed 时抓本帧角色渲染 RT 供探针分析视口矩形。
            # 注意：条件里不能写 ResourceDragViewportSource === null —— 该资源是
            # 空声明占位，3DMigoto 解析期会把它当静态 null 折叠整个 if 块
            # （日志 "Optimised out post"），导致捕获链从未执行。
            # 因此无条件抓取（armed 帧内最后一次为准，arm 周期先置 null，语义等价）。
            lines.extend([
                f"\tif {drag_mode_var} >= 1 && $ssmtdrag_viewport_probe_armed_{ns} == 1",
                f"\t\tResourceDragViewportSource_{ns} = copy o0 unless_null",
                "\tendif",
            ])
        lines.append(f"\t$ssmtdrag_drawn_{ns} = 1")
        if include_detect:
            lines.extend([
                # 悬停检测不要求 Alt（与实机验证过的可用配置一致）：
                # 模式开关（drag_mode_var >= 1）打开即检测，Alt 只门控实际形变（jiggle 门）。
                f"\tif {drag_mode_var} >= 1 && $ObjectDetectAllowed_{ns} == 1",
                f"\t\trun = CustomShaderDragBake{part_tag}_{ns}",
                f"\t\trun = CustomShaderDragDetect{part_tag}_{ns}",
                "\tendif",
            ])
        lines.extend([
            f"\tif {drag_mode_var} >= 2 && $ssmtdrag_mode_{ns} == 1" + (f" && $ssmtcloth_{ns}_enabled == 0" if comp.get('cloth') else ''),
            f"\t\tif time != {last_dispatch}",
            f"\t\t\trun = CustomShaderDragJiggle{cn}_{ns}",
            f"\t\t\t{last_dispatch} = time",
            "\t\tendif",
            f"\t\tvb0 = {temp_vb0}",
        ])
        # BlendOut persists the initialized compute weights. BlendVB is its
        # plain vertex-compatible snapshot, including rows untouched by slide.
        if comp.get("collision_trigrid"):
            lines.extend([
                f"\t\tif $ssmtdrag_slide_enabled_{ns} == 1",
                f"\t\t\tvb2 = ResourceDragJiggleBlendVB_{cn}_{ns}",
                "\t\telse",
                f"\t\t\tvb2 = Resource{cn}Blend",
                "\t\tendif",
            ])
        lines.extend([
            "\tendif",
            *(['\trun = ' + comp['cloth_publish'], '\tpost run = ' + comp['cloth_restore']] if comp.get('cloth_publish') else []),
            f"\t; --- DRAG HOOK END {part_tag}_{ns} ---",
        ])
        return lines

    # =======================================================================
    # Present 块 + Constants globals
    # =======================================================================

    @staticmethod
    def _place_drag_present_block(present_lines, block):
        """Position the generated Present block before the UI binding marker."""
        start = None
        end = None
        for i, line in enumerate(present_lines):
            if start is None and "; --- DRAG PRESENT BEGIN ---" in line:
                start = i
            if start is not None and "; --- DRAG PRESENT END ---" in line:
                end = i
                break
        if start is not None:
            if end is None:
                del present_lines[start:]
            else:
                del present_lines[start:end + 1]
            while start < len(present_lines) and not present_lines[start].strip():
                del present_lines[start]

        marker_idx = next(
            (i for i, line in enumerate(present_lines) if "; --- MODEL DRAG BINDING BEGIN ---" in line),
            None,
        )
        if marker_idx is not None:
            present_lines[marker_idx:marker_idx] = block
            return
        binding_idx = next(
            (i for i, line in enumerate(present_lines)
             if "if $mouse_clicked == 1" in line
             and "$is_dragging == 0" in line),
            None,
        )
        if binding_idx is not None:
            present_lines[binding_idx:binding_idx] = block
            return
        while present_lines and not present_lines[-1].strip():
            present_lines.pop()
        if present_lines:
            present_lines.append("")
        present_lines.extend(block)

    def _emit_present_and_constants(self, sections, components, ns):
        drag_mode_var, ui_detected_var, ui_zone_var = self._runtime_variable_names(ns)
        default_drag_mode = self._default_drag_system_mode()
        # ---- Constants globals ----
        const_sec = "[Constants]"
        const_lines = sections.setdefault(const_sec, [])
        globals_to_add = [
            f"global $ssmtdrag_mode_{ns} = 0",
            "global $help = 0",
            f"global persist {drag_mode_var} = {default_drag_mode}",
            f"global $ssmtdrag_drawn_{ns} = 0",
            f"global $ssmtdrag_booted_{ns} = 0",
            f"global $ssmtdrag_lmb_down_{ns} = 0",
            f"global $ssmtdrag_rmb_down_{ns} = 0",
            f"global $ssmtdrag_x_down_{ns} = 0",
            f"global $ssmtdrag_modifier_down_{ns} = 0",
            f"global $ssmtdrag_lmb_prev_{ns} = 0",
            f"global $ssmtdrag_rmb_prev_{ns} = 0",
            f"global $ssmtdrag_combo_active_{ns} = 0",
            f"global $ssmtdrag_poke_sign_{ns} = 0",
            f"global $ssmtdrag_poke_hold_mult_{ns} = 1.0",
            f"global $ssmtdrag_poke_hold_frames_{ns} = 8",
            f"global $ssmtdrag_poke_min_strength_{ns} = 0.25",
            f"global $ssmtdrag_poke_strength_{ns} = 1.0",
            f"global $ssmtdrag_release_boost_{ns} = 1.05",
            f"global $ssmtdrag_release_decay_{ns} = 0.92",
            f"global $ssmtdrag_modifier_ok_{ns} = 0",
            f"global $ssmtdrag_lmb_press_time_{ns} = 0",
            f"global $ssmtdrag_rmb_press_time_{ns} = 0",
            f"global $ssmtdrag_delta_time_{ns} = 0.0166667",
            f"global $ssmtdrag_prev_time_{ns} = 0",
            # 原作 LEWDHAND 取值：着色器步长 = clamp(dt*60*speed, 0.05, max_step)，
            # max_step < 0.05 时 clamp 直接返回 max_step —— 旧默认 1.0/0.0333 会把
            # 物理步长钳到 0.0333（比原作弱约 90 倍），表现为检测有反应但形变几乎不可见。
            f"global $ssmtdrag_sim_speed_{ns} = 3.0",
            f"global $ssmtdrag_max_step_{ns} = 3.0",
            # 丝袜穿脱（t16）的「总闸」声明。三个闸依次是：本变量（声明且为 1）/
            # ZoneParams[zone*2+1].z（区域位）/ PathVectors[zone].w（方向位）。
            # **必须在这里统一声明**：ini 里引用一个从未声明的变量，3DMigoto 会把它
            # 当 0 处理并连带让所在 if/endif 解析错位、最终弃载整个
            # [TextureOverride_VB_…_Position] 段 ⇒ 拖拽/物理/碰撞全部不执行（用户实测
            # 「落地了但毫无变化」的根因）。默认 1 = 三闸中的总闸开启（区域位与方向位仍各自
            # 独立把关，滑动只在档位 2 的拖拽通道内可达）；需要整段停用时改 0 再 F10 即可。
            f"global $ssmtdrag_slide_enabled_{ns} = 1",
            f"global $ObjectDetectAllowed_{ns} = 0",
            f"global $isMouseButtonDown = 0",
            f"global $cursorX = -1", "global $cursorY = -1",
            f"global $screenW = 1", "global $screenH = 1",
            f"global $inputMode = 0",
            # CommandListDragViewportUpdate 赋值、CursorUpdate 读取。3DMigoto 中变量跨
            # run= 命令列表边界传递必须声明为 global，否则子列表里置 1 后回调用方仍读 0 →
            # 视口恒无效 → cursor 恒 (-1,-1) → 检测永不命中且手型光标锚定屏幕外（导出模组无效果的根因）
            f"global $ssmtdrag_viewport_valid = 0",
        ]
        if self._feature_panel():
            # UI 构造器桥接：把 GPU 检测结果回读成可跨 INI 命名空间引用的只读全局量。
            # detected < 0 表示未命中；zone 仅在 detected >= 0 时有效。
            globals_to_add.extend([
                f"global {ui_detected_var} = -1",
                f"global {ui_zone_var} = -1",
            ])
        if self._feature_skd():
            # 鼠标位移量测的上一帧值全局（F1 专属：驱动 CS x79/y79 消费）——
            # 形态键联动关闭时不声明，避免残留 F1 引用
            globals_to_add.extend([
                f"global $ssmtdrag_shapekey_dy_{ns} = 0",
                f"global $ssmtdrag_shapekey_dx_{ns} = 0",
                f"global $ssmtdrag_shapekey_prev_y_{ns} = 0",
                f"global $ssmtdrag_shapekey_prev_x_{ns} = 0",
            ])
        if self.enable_viewport_probe:
            globals_to_add.extend([
                f"global $ssmtdrag_viewport_probe_enabled_{ns} = 1",
                f"global $ssmtdrag_viewport_probe_interval_{ns} = 0.50",
                f"global $ssmtdrag_viewport_probe_next_time_{ns} = 0",
                f"global $ssmtdrag_viewport_probe_armed_{ns} = 0",
                f"global $ssmtdrag_viewport_probe_generation_{ns} = 0",
            ])
        if self.enable_hand_cursor:
            # 手部参数：persist 项可在 d3dx_user.ini 调参并跨重载保留（对照原作 persist 集合）
            globals_to_add.extend([
                f"global $ssmtdrag_hand_debug_{ns} = 1",
                f"global $ssmtdrag_hand_surface_clip_{ns} = 1",
                f"global $ssmtdrag_hand_surface_lift_{ns} = 0.018",
                f"global $ssmtdrag_hand_surface_softness_{ns} = 0.020",
                f"global persist $ssmtdrag_hand_center_x_{ns} = -0.000614",
                f"global persist $ssmtdrag_hand_center_y_{ns} = 0.275962",
                f"global persist $ssmtdrag_hand_center_z_{ns} = 0.065117",
                f"global persist $ssmtdrag_hand_scale_{ns} = 0.5",
                f"global persist $ssmtdrag_hand_opacity_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_tilt_min_{ns} = 10.0",
                f"global persist $ssmtdrag_hand_windup_time_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_tilt_max_deg_{ns} = 45.0",
                f"global persist $ssmtdrag_hand_tilt_rmb_min_{ns} = 10.0",
                f"global persist $ssmtdrag_hand_rmb_windup_time_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_tilt_rmb_max_deg_{ns} = 45.0",
                f"global persist $ssmtdrag_hand_tilt_grab_max_deg_{ns} = 45.0",
                f"global persist $ssmtdrag_hand_vibrate_threshold_{ns} = 0.5",
                f"global persist $ssmtdrag_hand_vibrate_period_slow_{ns} = 0.8",
                f"global persist $ssmtdrag_hand_vibrate_period_fast_{ns} = 0.1",
                f"global persist $ssmtdrag_hand_vibrate_amplitude_{ns} = 4.0",
                f"global persist $ssmtdrag_hand_upright_max_deg_{ns} = 80.0",
                f"global persist $ssmtdrag_hand_outline_width_{ns} = 2.0",
                f"global persist $ssmtdrag_hand_outline_opacity_{ns} = 1.0",
                f"global persist $ssmtdrag_hand_reference_height_{ns} = 2160.0",
                f"global $ssmtdrag_lmb_hold_fraction_{ns} = 0",
                f"global $ssmtdrag_rmb_hold_fraction_{ns} = 0",
                f"global $ssmtdrag_rmb_lone_hold_{ns} = 0",
            ])
        # 变量↔缓冲双向同步辅助变量：激活/回读/上一值/待确认/同步模式。
        if self._feature_var():
            _sync_n = len(self._drag_drive_var_sync_bindings())
            if _sync_n:
                globals_to_add.append(f"global $ssmtdrag_skheld_{ns} = 0")
                # 回读最小化用：按住沿（决定何时回读绑定锁存）、锁存值（区域激活的
                # CPU 侧推导源）、是否需要 dispatch 同步 CS
                globals_to_add.append(f"global $ssmtdrag_skheldprev_{ns} = 0")
                globals_to_add.append(f"global $ssmtdrag_sklatch_{ns} = 0")
                globals_to_add.append(f"global $ssmtdrag_skany_{ns} = 0")
            # 冷启动播种标志：只有真的会置 1（存在 ClickExport 播种条目）时才声明，
            # 否则它是个「声明 + 被 CS 读 + 从不赋值」的死变量。未发射时驱动 CS 段
            # 显式绑 y80 = 0 / x81 = 0，不依赖本变量存在。
            if self._click_export_seed_entries():
                globals_to_add.append(f"global $ssmtdrag_seed_pending_{ns} = 0")
            for i in range(_sync_n):
                globals_to_add.extend([
                    f"global $ssmtdrag_skact_{ns}_{i} = 0",
                    f"global $ssmtdrag_skrb_{ns}_{i} = 0",
                    f"global $ssmtdrag_skprev_{ns}_{i} = 0",
                    f"global $ssmtdrag_skpending_{ns}_{i} = 0",
                    f"global $ssmtdrag_skmode_{ns}_{i} = 0",
                ])
        for comp in components:
            globals_to_add.append(f"global $ssmtdrag_last_dispatch_{comp['comp_name']}_{ns} = -1")
        # 物体显隐 flag：绘制分支内置位、Present 发布进 GPU 缓冲、post 清零。
        # oid 是跨组件连续编号（_locate_components 的 object_offset 累计），
        # 声明必须取 object_id_map 的全局 id 并集；按组件内 range 重数会导致
        # 第二个组件起全部缺失/错位（曾致 b1870eee 透明布料 flag 无 global、
        # 发布恒读 0、显隐门控误杀全部命中）。
        for oid in self._global_object_oids(components):
            globals_to_add.append(f"global $ssmtdrag_objvis_{ns}_{oid} = 0")
        # IniParams 卫生修复：存/还共享槽位的备份变量（drag 块 + 手型绘制段引用）
        for _slot in ("69", "70", "72", "73", "97"):
            for _comp in ("x", "y", "z", "w"):
                globals_to_add.append(f"global $ssmtdrag_ipbak_{_comp}{_slot} = 0")
        for _slot in ("94", "95", "98"):
            for _comp in ("x", "y", "z", "w"):
                globals_to_add.append(f"global $ssmtdrag_ipbak_{_comp}{_slot} = 0")
        for g in globals_to_add:
            var = g.split("=", 1)[0].replace("global ", "").replace("persist ", "").strip()
            # 精确匹配变量名（防 $ssmtdrag_objvis_A_3 误匹配 A_37 这类前缀串）
            if not any(f"{var} " in line or f"{var}=" in line for line in const_lines):
                const_lines.append(g)

        # ---- Present 块（手势归约 + 手部蓄力进度 + S8 手部绘制）----
        present_sec = "[Present]"
        present_lines = sections.setdefault(present_sec, [])
        ui_bridge_lines = [
            "\t; --- DRAG UI BRIDGE BEGIN ---",
            # zone 直出回读（store 无条件执行，结果有效性由 pre 阶段设置的
            # ObjectDetectAllowed 仲裁，避免 post 阶段门控变量时序不可靠）：
            #   - 命中 ID：ResourceDragPinnedDetectID 槽 0（RWStructuredBuffer<float> stride 4）
            #   - 区域 ID：ResourceDragZoneOut 槽 0（RWBuffer R32_FLOAT 标量）
            # 两者同走 R32 标量路径，与点击计数回读同型。历史版本曾从
            # stride-16 StructuredBuffer 按 float 标量索引 31 读 zone，实测不可靠
            # （zone 恒 -1），故改由 pin 着色器直出到 R32 标量槽。
            # store 读取上一份已完成的 GPU 数据，允许 UI 侧以一帧延迟稳定消费；
            # 本帧检测未运行（ObjectDetectAllowed=0）或未命中时主动失效，避免残留命中。
            f"\tstore = {ui_detected_var}, ResourceDragPinnedDetectID_{ns}, 0",
            f"\tstore = {ui_zone_var}, ResourceDragZoneOut_{ns}, 0",
            f"\tif {ui_detected_var} < 0 || $ObjectDetectAllowed_{ns} != 1",
            f"\t\t{ui_detected_var} = -1",
            f"\t\t{ui_zone_var} = -1",
            "\tendif",
            "\t; --- DRAG UI BRIDGE END ---",
        ]
        interaction_gate_lines = [
            "\t; --- DRAG INTERACTION GATE BEGIN ---",
            f"if {drag_mode_var} < 2",
            "\t$isMouseButtonDown = 0",
            f"\t$ssmtdrag_poke_sign_{ns} = 0",
            f"\t$ssmtdrag_combo_active_{ns} = 0",
            f"\tclear = ResourceDragJiggleScreenState_{ns} 0.0",
            f"\tclear = ResourceDragPathProgressState_{ns} 0.0",
        ]
        # 逐组件副本同步清零（否则模式降档后旧屏幕状态继续喂本组件 Jiggle）
        for comp in components:
            interaction_gate_lines.extend([
                f"\tclear = ResourceDragJiggleScreenState_{comp['comp_name']}_{ns} 0.0",
                f"\tclear = ResourceDragPathProgressState_{comp['comp_name']}_{ns} 0.0",
            ])
        for comp in components:
            interaction_gate_lines.append(
                f"\tclear = ResourceDragJiggleState_{comp['comp_name']}_{ns} 0.0")
        if self.enable_hand_cursor:
            interaction_gate_lines.extend([
                f"\t$ssmtdrag_lmb_hold_fraction_{ns} = 0",
                f"\t$ssmtdrag_rmb_hold_fraction_{ns} = 0",
                f"\t$ssmtdrag_rmb_lone_hold_{ns} = 0",
            ])
        interaction_gate_lines.extend([
            "endif",
        ])
        # 形态键驱动只在“仅命中”模式（1）下生效；其余模式不驱动但保持当前数值
        # （驱动 CS 内已有 mode != 1 时保持的处理，此处不再清零）
        interaction_gate_lines.extend([
            f"if {drag_mode_var} < 1",
            f"\t$ObjectDetectAllowed_{ns} = 0",
            f"\tclear = ResourceDragDetectID_{ns} 0.0",
            f"\tclear = ResourceDragPinnedDetectID_{ns}",
            f"\tclear = ResourceDragPinnedDetectInfo_{ns}",
            f"\tclear = ResourceDragZoneOut_{ns} 0.0",
        ])
        for comp in components:
            cn = comp['comp_name']
            interaction_gate_lines.extend([
                f"\tclear = ResourceDragComponentDetect_{cn}_{ns} 0.0",
                f"\tclear = ResourceDragPinnedComponentID_{cn}_{ns} 0.0",
                f"\tclear = ResourceDragPinnedComponentInfo_{cn}_{ns} 0.0",
                f"\tclear = ResourceDragComponentZoneOut_{cn}_{ns} 0.0",
            ])
        interaction_gate_lines.extend([
            "endif",
            "\t; --- DRAG INTERACTION GATE END ---",
        ])
        # 抓取手势条件（原作默认 左右键同按/X；可选 左键/右键 单键抓取）
        if self.grab_gesture == 'RMB':
            grab_cond = f"$ssmtdrag_rmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1"
        elif self.grab_gesture == 'COMBO':
            grab_cond = f"($ssmtdrag_lmb_down_{ns} == 1 && $ssmtdrag_rmb_down_{ns} == 1) || $ssmtdrag_x_down_{ns} == 1"
        else:  # LMB（默认，最直觉）
            grab_cond = f"$ssmtdrag_lmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1"
        poke_gesture = getattr(self, "poke_gesture", 'RMB')
        poke_release_lines = []
        if poke_gesture in {'LMB', 'BOTH'}:
            poke_release_lines.extend([
                f"\tif $ssmtdrag_lmb_prev_{ns} == 1 && $ssmtdrag_lmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_poke_sign_{ns} = -1",
                f"\t\t$ssmtdrag_poke_hold_mult_{ns} = time - $ssmtdrag_lmb_press_time_{ns}",
            ])
        if poke_gesture in {'RMB', 'BOTH'}:
            branch = "elif" if poke_release_lines else "if"
            poke_release_lines.extend([
                f"\t{branch} $ssmtdrag_rmb_prev_{ns} == 1 && $ssmtdrag_rmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_poke_sign_{ns} = -1",
                f"\t\t$ssmtdrag_poke_hold_mult_{ns} = time - $ssmtdrag_rmb_press_time_{ns}",
            ])
        if poke_release_lines:
            poke_release_lines.append("\tendif")
        block = [
            "\t; --- DRAG PRESENT BEGIN ---",
            "$isMouseButtonDown = 0",
            f"$ssmtdrag_poke_sign_{ns} = 0",
            (f"$ssmtdrag_mode_{ns} = $ssmtdrag_modifier_down_{ns}"
             if self.grab_key == 'ALT' else f"$ssmtdrag_mode_{ns} = 1"),
            # NONE 模式无常驻修饰键 → 常开；ALT 模式 = modifier_down
            (f"$ssmtdrag_modifier_ok_{ns} = $ssmtdrag_modifier_down_{ns}" if self.grab_key == 'ALT'
             else f"$ssmtdrag_modifier_ok_{ns} = 1"),
            f"if $ssmtdrag_modifier_ok_{ns} == 1",
            f"\tif {grab_cond}",
            "\t\t$isMouseButtonDown = 1",
            "\tendif",
            "endif",
            f"if $isMouseButtonDown == 1",
            f"\t$ssmtdrag_combo_active_{ns} = 1",
            "endif",
            # 戳脉冲
            f"if $ssmtdrag_modifier_ok_{ns} == 1 && $ssmtdrag_combo_active_{ns} == 0",
            *poke_release_lines,
            "endif",
            f"if $ssmtdrag_poke_sign_{ns} != 0",
            f"\tif $ssmtdrag_poke_hold_mult_{ns} < $ssmtdrag_poke_min_strength_{ns}",
            f"\t\t$ssmtdrag_poke_hold_mult_{ns} = $ssmtdrag_poke_min_strength_{ns}",
            f"\telif $ssmtdrag_poke_hold_mult_{ns} > 1.00",
            f"\t\t$ssmtdrag_poke_hold_mult_{ns} = 1.00",
            "\tendif",
            f"\t$ssmtdrag_poke_hold_mult_{ns} = $ssmtdrag_poke_hold_mult_{ns} * $ssmtdrag_poke_strength_{ns}",
            "else",
            f"\t$ssmtdrag_poke_hold_mult_{ns} = $ssmtdrag_poke_strength_{ns}",
            "endif",
            # press_time 记录
            f"if $ssmtdrag_lmb_down_{ns} == 1 && $ssmtdrag_lmb_prev_{ns} == 0",
            f"\t$ssmtdrag_lmb_press_time_{ns} = time",
            "endif",
            f"if $ssmtdrag_rmb_down_{ns} == 1 && $ssmtdrag_rmb_prev_{ns} == 0",
            f"\t$ssmtdrag_rmb_press_time_{ns} = time",
            "endif",
        ]
        if self.enable_hand_cursor:
            # 手部蓄力进度（对照原作 Present 的 hold-fraction 归约：独按 LMB/RMB
            # 期间持续累计并按 windup_time 封顶；组合键或非独按时归零）
            block.extend([
                f"if $ssmtdrag_lmb_down_{ns} == 1",
                f"\tif $ssmtdrag_rmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_lmb_hold_fraction_{ns} = time - $ssmtdrag_lmb_press_time_{ns}",
                f"\t\tif $ssmtdrag_lmb_hold_fraction_{ns} > $ssmtdrag_hand_windup_time_{ns}",
                f"\t\t\t$ssmtdrag_lmb_hold_fraction_{ns} = $ssmtdrag_hand_windup_time_{ns}",
                "\t\tendif",
                "\telse",
                f"\t\t$ssmtdrag_lmb_hold_fraction_{ns} = 0",
                "\tendif",
                "else",
                f"\t$ssmtdrag_lmb_hold_fraction_{ns} = 0",
                "endif",
                f"$ssmtdrag_rmb_lone_hold_{ns} = 0",
                f"if $ssmtdrag_rmb_down_{ns} == 1",
                f"\tif $ssmtdrag_lmb_down_{ns} == 0",
                f"\t\t$ssmtdrag_rmb_lone_hold_{ns} = 1",
                f"\t\t$ssmtdrag_rmb_hold_fraction_{ns} = time - $ssmtdrag_rmb_press_time_{ns}",
                f"\t\tif $ssmtdrag_rmb_hold_fraction_{ns} > $ssmtdrag_hand_rmb_windup_time_{ns}",
                f"\t\t\t$ssmtdrag_rmb_hold_fraction_{ns} = $ssmtdrag_hand_rmb_windup_time_{ns}",
                "\t\tendif",
                "\telse",
                f"\t\t$ssmtdrag_rmb_hold_fraction_{ns} = 0",
                "\tendif",
                "else",
                f"\t$ssmtdrag_rmb_hold_fraction_{ns} = 0",
                "endif",
            ])
        if self._feature_skd():
            # 鼠标位移控制：帧内计算 Y/X 位移（$cursorY 自下而上，向上移动为增），
            # 由 CustomShaderDragShapeKeyDrive 段的 x79/y79 写入 IniParams[79] 供驱动 CS 读取
            block.extend([
                f"	$ssmtdrag_shapekey_dy_{ns} = $cursorY - $ssmtdrag_shapekey_prev_y_{ns}",
                f"	$ssmtdrag_shapekey_dx_{ns} = $cursorX - $ssmtdrag_shapekey_prev_x_{ns}",
                f"	$ssmtdrag_shapekey_prev_y_{ns} = $cursorY",
                f"	$ssmtdrag_shapekey_prev_x_{ns} = $cursorX",
            ])
        block.extend([
            # prev 更新 + combo 复位
            f"$ssmtdrag_lmb_prev_{ns} = $ssmtdrag_lmb_down_{ns}",
            f"$ssmtdrag_rmb_prev_{ns} = $ssmtdrag_rmb_down_{ns}",
            f"if $ssmtdrag_lmb_down_{ns} == 0 && $ssmtdrag_rmb_down_{ns} == 0",
            f"\t$ssmtdrag_combo_active_{ns} = 0",
            "endif",
        ])
        if self.enable_viewport_probe:
            # 视口探针：armed 则解码视口矩形（供 Detect 的 FrameAPI 光标映射）；
            # 到节流间隔则清旧快照+FrameAPI 并武装下一代（低频节流，非逐帧拷贝）。
            # 条件里不能写 ResourceDragViewportSource !== null —— 空声明占位会被
            # 3DMigoto 解析期折叠（"Optimised out post"），decode 链从未执行。
            # decode CS 对空/无效 LayoutData 有完整保护（直接返回并清零），无条件运行安全。
            block.extend([
                f"if {drag_mode_var} >= 1 && $ssmtdrag_viewport_probe_armed_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
                f"\trun = CustomShaderDragViewportLayoutDecode_{ns}",
                "endif",
                f"if {drag_mode_var} >= 1 && $ssmtdrag_viewport_probe_enabled_{ns} == 1 && time >= $ssmtdrag_viewport_probe_next_time_{ns}",
                f"\tResourceDragViewportSource_{ns} = null",
                # [t138] 不再 clear：重新布防失败时保留上一份好布局（解码器失败是提前 return，不会写坏）
                # f"\tclear = ResourceDragViewportFrameAPI_{ns} 0.0",
                f"\t$ssmtdrag_viewport_probe_armed_{ns} = 1",
                f"\t$ssmtdrag_viewport_probe_generation_{ns} = $ssmtdrag_viewport_probe_generation_{ns} + 1",
                f"\t$ssmtdrag_viewport_probe_next_time_{ns} = time + $ssmtdrag_viewport_probe_interval_{ns}",
                "else",
                f"\t$ssmtdrag_viewport_probe_armed_{ns} = 0",
                "endif",
            ])
        block.extend(interaction_gate_lines)
        block.extend([
            # boot / 冷启动播种不依赖 Alt；正常检测与拖拽仍要求交互门控。
            f"if $ssmtdrag_booted_{ns} == 0",
            f"\tpre run = CommandListDragPinDetected_{ns}",
        ])
        if self._feature_var() and self._click_export_seed_entries():
            block.extend([
                f"elif $ssmtdrag_seed_pending_{ns} == 1",
                f"\tpre run = CommandListDragPinDetected_{ns}",
            ])
        block.extend([
            # 与实机可用配置一致：Pin 检测/光标更新只由模式开关门控，不要求 Alt；
            # 但必须要求本帧人物真的被绘制过（drawn==1）——否则角色不在场时每帧
            # 仍在空跑整条检测/光标链路（装几十上百个模组时纯属浪费）。
            f"elif {drag_mode_var} >= 1 && $ssmtdrag_drawn_{ns} == 1",
            f"\tpre run = CommandListDragPinDetected_{ns}",
            # 光标/视口更新只被**臂动链路**消费：检测 CS 与手型光标都在 mode==1 下才跑
            # （检测 CS 的 dispatch 条件里就有 mode==1），而检测读的本来就是上一帧的光标
            # （`pre run` 在帧首执行，晚于它的光标更新要下一帧才可见）。所以不臂动时
            # 更新光标/视口是纯空跑（本模组 ~32 条语句/帧，且"在场未臂动"是最常见状态）。
            f"\tif $ssmtdrag_mode_{ns} == 1",
            f"\t\trun = CommandListDragCursorUpdate_{ns}",
            "\tendif",
        ])
        # 未绘制（或模式 0）时 PinDetected 不执行，其内部 else 够不到：这里补上
        # 终态清理，保证「不在场」帧的检测允许位归零。
        block.append("else")
        block.append(f"\t$ObjectDetectAllowed_{ns} = 0")
        if self._feature_skd():
            # 模式 0（含 1→0 直跳，不经过模式 2 dispatch 帧）：PinDetected 整体不
            # dispatch，臂动门控内的失臂 else 够不到；在此终态 else 清锁存，
            # 防陈旧绑定跨 mode-0 滞留、回模式 1 后无命中复活（评审 G1）。
            block.append(f"\tclear = ResourceDragShapeKeyDragLatch_{ns} 0.0")
        block.append("endif")
        sync_bindings = (
            self._drag_drive_var_sync_bindings()
            if self._feature_var() else []
        )
        if sync_bindings:
            # 分时互斥：区域激活由回读命令列表从驱动 CS 的绑定锁存（单槽）推导
            #（按住命中即绑定、移出区域不丢、松开解除）；
            # 回读只在「拖拽激活」或「等 store 确认」时进行，其余时间变量完全归
            # 驱动器/用户所有——空闲帧零回读。
            block.extend([
                f"$ssmtdrag_skheld_{ns} = 0",
                f"if $ssmtdrag_mode_{ns} == 1 && ($ssmtdrag_lmb_down_{ns} == 1 || $ssmtdrag_x_down_{ns} == 1)",
                f"\t$ssmtdrag_skheld_{ns} = 1",
                "endif",
                f"if $ssmtdrag_booted_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
                f"\tpre run = CommandListDragShapeKeyVarReadback_{ns}",
                "endif",
                # 变量→驱动缓冲同步：只在「有绑定需要推送 / 刚从缓冲拉取」时 dispatch
                #（skany 由回读命令列表当帧算出），空闲帧不再空跑一次 CS
                f"if $ssmtdrag_drawn_{ns} == 1 && $ssmtdrag_skany_{ns} == 1",
                f"\trun = CustomShaderDragShapeKeyVarSync_{ns}",
                "endif",
            ])
        ui_readback_sec = f"[CommandListDragUIReadback_{ns}]"
        if self._feature_panel() and ui_readback_sec not in sections:
            sections[ui_readback_sec] = list(ui_bridge_lines)

        if self.enable_hand_cursor:
            # S8 手型光标：先更新手部屏幕位置，描边先画垫底（只露轮廓边）、填充后画；
            # 抓取中或 RMB 独按蓄力时用 Action 网格（握拳），否则 NoAction（张开）
            block.extend([
                # 门控必须含 $ssmtdrag_mode（Alt 臂动）：检测只在臂动时刷新命中数据，
                # 松开 Alt 后 PinnedDetectInfo 是陈旧值——若只按 drawn 门控，手型光标
                # 会停留在松开前的命中点上不消失（用户报告）。NONE 常开模式下
                # $ssmtdrag_mode 恒 1，行为与之前一致。
                f"if {drag_mode_var} >= 1 && $ssmtdrag_mode_{ns} == 1 && $ssmtdrag_drawn_{ns} == 1",
                f"\trun = CustomShaderDragUpdateJiggleCursorPreview_{ns}",
                f"\tif $ssmtdrag_hand_debug_{ns} == 2",
                f"\t\trun = CustomShaderDragJiggleCursor_{ns}",
                "\tendif",
                f"\tif $ssmtdrag_hand_debug_{ns} >= 1",
                # 根源卫生修复：手型绘制段写共享 IniParams 槽 83/89-95/98，
                # 其中 94/95/98 与 TTL 的 layout/mask/lift 槽冲突——先存后还。
                f"\t\t$ssmtdrag_ipbak_x94 = x94",
                f"\t\t$ssmtdrag_ipbak_y94 = y94",
                f"\t\t$ssmtdrag_ipbak_z94 = z94",
                f"\t\t$ssmtdrag_ipbak_w94 = w94",
                f"\t\t$ssmtdrag_ipbak_x95 = x95",
                f"\t\t$ssmtdrag_ipbak_y95 = y95",
                f"\t\t$ssmtdrag_ipbak_z95 = z95",
                f"\t\t$ssmtdrag_ipbak_w95 = w95",
                f"\t\t$ssmtdrag_ipbak_x98 = x98",
                f"\t\t$ssmtdrag_ipbak_y98 = y98",
                f"\t\t$ssmtdrag_ipbak_z98 = z98",
                f"\t\t$ssmtdrag_ipbak_w98 = w98",
                f"\t\tif $isMouseButtonDown == 1 || $ssmtdrag_rmb_lone_hold_{ns} == 1",
                f"\t\t\trun = CustomShaderDragPresentHandActionOutline_{ns}",
                f"\t\t\trun = CustomShaderDragPresentHandAction_{ns}",
                "\t\telse",
                f"\t\t\trun = CustomShaderDragPresentHandOutline_{ns}",
                f"\t\t\trun = CustomShaderDragPresentHand_{ns}",
                "\t\tendif",
                f"\t\tx94 = $ssmtdrag_ipbak_x94",
                f"\t\ty94 = $ssmtdrag_ipbak_y94",
                f"\t\tz94 = $ssmtdrag_ipbak_z94",
                f"\t\tw94 = $ssmtdrag_ipbak_w94",
                f"\t\tx95 = $ssmtdrag_ipbak_x95",
                f"\t\ty95 = $ssmtdrag_ipbak_y95",
                f"\t\tz95 = $ssmtdrag_ipbak_z95",
                f"\t\tw95 = $ssmtdrag_ipbak_w95",
                f"\t\tx98 = $ssmtdrag_ipbak_x98",
                f"\t\ty98 = $ssmtdrag_ipbak_y98",
                f"\t\tz98 = $ssmtdrag_ipbak_z98",
                f"\t\tw98 = $ssmtdrag_ipbak_w98",
                "\tendif",
                "endif",
            ])
        # 物体显隐发布：每帧把分支置位的 flag 经 ini params 喂给发布 CS →
        # GPU 缓冲（下一帧检测消费）；随后 post 清零供下一帧分支重算。
        total_objs = sum(int(comp.get("object_count") or 0) for comp in components)
        if total_objs:
            # 发布只在「臂动模式」下被消费：绑定该缓冲的检测 CS（cs-t8）只在
            # $ObjectDetectAllowed 为 1 时 dispatch，而它由下面这条**完全相同**的条件
            # 置位（见 CommandListDragPinDetected 的发射点）。条件不成立时整条发布
            # 命令列表（本模组 216 条语句 + 1 次 dispatch）纯属空跑。
            block.append(
                f"if {drag_mode_var} >= 1 && $inputMode == 0 && $ssmtdrag_mode_{ns} == 1"
                f" && $ssmtdrag_drawn_{ns} == 1"
            )
            block.append(f"\tpre run = CommandListDragVisPublish_{ns}")
            block.append("endif")
            # 清零必须**每绘制帧**执行：objvis 标志是逐绘制置位的（与 mode 无关，
            # 301 处都在材质分支里），攒着不清会让下次 mode==1 的发布读到陈旧可见性。
            block.append(f"if $ssmtdrag_drawn_{ns} == 1")
            # 同上：post 清零按全局 oid 并集发射，按组件 range 会产生重复且漏掉
            # 第二组件起的 oid（曾出现 0-14 重复、37-51 缺失）。
            # 清零只在「本帧真的发布过」时才有意义：角色不在场时没有任何分支把
            # objvis 置 1，逐个 post 纯属每帧开销（本模组 215 条 × 每个模组）。
            for oid in self._global_object_oids(components):
                block.append(f"\tpost $ssmtdrag_objvis_{ns}_{oid} = 0")
            block.append("endif")
        if self._feature_panel():
            block.append(f"if $ssmtdrag_drawn_{ns} == 1")
            block.append(f"\tpost run = CommandListDragUIReadback_{ns}")
            block.append("endif")
        block.extend([
            f"post $ssmtdrag_drawn_{ns} = 0",
        ])
        block.append("\t; --- DRAG PRESENT END ---")
        self._place_drag_present_block(present_lines, block)


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 权重预览（视口热力图）：仿高斯球（toolkit/gb_operators）的轻量实现。
# 单例 draw handler + 轮询 timer；节点 preview_weights 启用后，对单个目标或集合内
# 全部网格逐顶点合并各启用区域的高斯场。集合或大量区域使用快速体积距离，
# 避免交互预览反复运行逐区域 Dijkstra；导出烘焙仍使用精确的沿表面传播。
# 热力图直接叠加在模型表面（含 xray 幽灵层）。gpu/timers 均为函数内延迟导入，
# 测试环境（stub bpy，无 GPU）导入本模块零副作用。
# ---------------------------------------------------------------------------

_preview_handler = None
_preview_timer = None
_preview_batches = {}          # (tree,node) -> (target_name, batch, ghost_batch)
_preview_sig_cache = None
_preview_pending_signature = None
_preview_pending_since = None
_PREVIEW_ALPHA = 0.85
_PREVIEW_GHOST_FACTOR = 0.3
_PREVIEW_TICK = 0.2
_PREVIEW_DEBOUNCE = 0.6
def _preview_now():
    return time.monotonic()


def _collect_preview_nodes():
    """全部节点树中启用了权重预览且存在有效目标的拖拽节点。"""
    nodes = []
    for tree in bpy.data.node_groups:
        for n in tree.nodes:
            if (n.bl_idname == 'SSMTNode_PostProcess_DragInteraction'
                    and getattr(n, "preview_weights", False)
                    and _preview_targets(n)):
                nodes.append(n)
    return nodes


def _preview_targets(node):
    """集合优先；递归返回其中全部网格。未设集合时回退单物体。"""
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


# armature 姿态摘要缓存：按 (id(armature), name) 每代缓存一次，
# 同一代（同一次 tick）内所有 target 共享一次 O(B) 计算（O(T×B)→O(B)，
# 修复多子网格引用同一 armature 时每 tick 重复全量遍历的卡死根因）。
# 代号由 _preview_tick 每 tick 推进：姿态变化在下一个 tick 被检出
# （0.2s 检测延迟，与现有轮询/防抖语义一致）。
# 摘要为纯索引读取（m[i][j]）+ round(4) 滚动整数哈希：确定性、稳定、
# 不构造 16-float tuple、不触发 depsgraph 求值（tick 签名保持便宜）。
_PREVIEW_DIGEST_GENERATION = 0
_PREVIEW_DIGEST_CACHE = {}
_PREVIEW_DIGEST_CACHE_MAX = 8

# ---------------- 事件驱动失效状态（t12） ----------------
# depsgraph_update_post handler 置 dirty 后才走全量签名；空闲 tick O(1)。
# 相关键集：参与预览的数据块（目标/网格/形态键/骨架/区域空物体/集合）的
# 稳定键（as_pointer 优先，stub 回退 id()），handler 内只做 O(|updates|)
# 集合查找过滤；None = 未建（handler 保守置脏）。
_preview_dirty = True
_preview_last_selection = None
_preview_deps_registered = False
_PREVIEW_RELEVANT_IDS = None
_PREVIEW_SAFETY_TICKS = 10  # 事件驱动下每 N 个干净 tick 强制一次全量签名（兜 driver 漏报）
_preview_safety_count = 0


def _preview_armature_digest(arm, generation):
    """单 armature 姿态滚动摘要：O(B) 一次、同代内全 target 共享。
    骨骼矩阵 16 元素索引读取 + round(4) 滚动哈希；约束目标物体矩阵同法
    并入（覆盖 IK 目标物体移动但骨骼通道未变的情形）。"""
    key = (id(arm), getattr(arm, "name", ""))
    cached = _PREVIEW_DIGEST_CACHE.get(key)
    if cached is not None and cached[0] == generation:
        return cached[1]
    pose = getattr(arm, "pose", None)
    h = hash(getattr(getattr(arm, "data", None), "pose_position", None))
    if pose is not None:
        for b in getattr(pose, "bones", None) or ():
            h = (h * 31 + hash(getattr(b, "name", ""))) & 0xFFFF_FFFF
            m = getattr(b, "matrix", None)
            if m is not None:
                try:
                    for i in range(4):
                        row = m[i]
                        for j in range(4):
                            h = (h * 31 + hash(round(float(row[j]), 4))) & 0xFFFF_FFFF
                except Exception:
                    pass
            for ct in getattr(b, "constraints", None) or ():
                tgt = getattr(ct, "target", None)
                if tgt is None:
                    continue
                h = (h * 31 + hash(getattr(tgt, "name", ""))) & 0xFFFF_FFFF
                tw = getattr(tgt, "matrix_world", None)
                if tw is not None:
                    try:
                        for i in range(4):
                            row = tw[i]
                            for j in range(4):
                                h = (h * 31 + hash(round(float(row[j]), 4))) & 0xFFFF_FFFF
                    except Exception:
                        pass
    if len(_PREVIEW_DIGEST_CACHE) >= _PREVIEW_DIGEST_CACHE_MAX:
        _PREVIEW_DIGEST_CACHE.clear()
    _PREVIEW_DIGEST_CACHE[key] = (generation, h)
    return h


def _preview_deform_signature(target):
    """形变签名：形态键值 + 修改器类型指纹 + armature 姿态滚动摘要。
    同一代内同一 armature 的摘要只算一次（全 target 共享）；
    纯数据读取、不触发 depsgraph 求值，0.2s/tick 签名保持便宜。
    无形态键且无任何修改器 → None（静态，零增量）。"""
    mesh = getattr(target, "data", None)
    if mesh is None:
        return None
    mods = getattr(target, "modifiers", None) or ()
    parts = []
    sk = getattr(mesh, "shape_keys", None)
    key_blocks = getattr(sk, "key_blocks", None)
    if key_blocks is not None:
        for kb in key_blocks:
            parts.append((
                getattr(kb, "name", ""),
                round(float(getattr(kb, "value", 0.0) or 0.0), 6),
                bool(getattr(kb, "mute", False)),
            ))
    if mods:
        # 修改器类型指纹：低成本纯读；增删/类型变化即使顶点数不变也失效
        parts.append(("MOD", tuple(sorted({str(getattr(m, "type", "")) for m in mods}))))
    for mod in mods:
        if getattr(mod, "type", None) != 'ARMATURE':
            continue
        arm = getattr(mod, "object", None)
        if arm is None:
            continue
        parts.append((getattr(arm, "name", ""),
                      _preview_armature_digest(arm, _PREVIEW_DIGEST_GENERATION)))
    return tuple(parts) if parts else None


def _preview_selected_zones(node):
    """多选并集：selected_objects ∩ node.zone_objects 中 enabled 的空物体
    （identity 比较；active 无特权，只是 selected 的普通成员）。
    按 node.zone_objects 顺序输出（确定性签名）；空集 = 不显示预览。"""
    ctx = getattr(bpy, "context", None)
    if ctx is None:
        return []
    selected = getattr(ctx, "selected_objects", None) or ()
    sel_ids = {id(s) for s in selected}
    out = []
    for item in node.zone_objects:
        empty = item.zone_object
        if empty is None or id(empty) not in sel_ids:
            continue
        s = getattr(empty, "ssmt_drag_zone", None)
        if s is not None and getattr(s, "enabled", True):
            out.append((empty, s))
    return out


def _preview_node_zones(node):
    """节点预览使用的区域 = 选中并集；未选中任何区域空物体 → 空列表（不显示）。

    需求变更（t11）：推翻 t1『未选中=显示全部』——预览只回答
    『当前选中的区域权重长啥样』，无选中即无预览。"""
    return _preview_selected_zones(node)


def _preview_selection_digest(nodes):
    """选中集摘要（O(#selected + Σ#zones)），供 tick 轮询——选中变化不进
    depsgraph，必须轮询；zone 成员增删也翻转摘要（节点属性编辑不进 depsgraph
    的兜底路径）。摘要只含名字与数量，不含矩阵/骨骼。"""
    ctx = getattr(bpy, "context", None)
    sel_ids = set()
    if ctx is not None:
        selected = getattr(ctx, "selected_objects", None) or ()
        sel_ids = {id(s) for s in selected}
    digests = []
    for n in nodes:
        members = []
        hits = []
        for item in getattr(n, "zone_objects", ()):
            empty = item.zone_object
            if empty is None:
                continue
            name = getattr(empty, "name", "")
            members.append(name)
            if id(empty) in sel_ids:
                hits.append(name)
        digests.append((getattr(n, "name", ""), tuple(members), tuple(hits)))
    return tuple(digests)


def _preview_zone_signature_part(empty):
    """单个区域空物体的签名分量（矩阵/参数/包含名单）。"""
    s = empty.ssmt_drag_zone
    include_names = tuple(
        getattr(getattr(item, "object", None), "name", None)
        for item in getattr(s, "include_objects", ())
    )
    return (empty.name,
            tuple(round(v, 6) for v in np.array(empty.matrix_world, dtype=np.float64).reshape(-1)),
            s.enabled, round(s.brush_strength, 6), round(s.brush_falloff_k, 6),
            bool(getattr(s, "propagate", True)), include_names)


def _preview_signature(n):
    """矩阵/参数/选择签名：集合成员、网格变换、区域参数、选中并集与
    形变（形态键值 + 骨骼姿态）变化都会触发重算。
    未选中任何区域空物体 → 签名最小化（(树, 节点, ("sel", ()))），
    此时不渲染预览，任何目标/区域变化都不触发重建（零 churn）。"""
    collection = getattr(n, "preview_collection", None)
    zones = _preview_selected_zones(n)
    sel_names = tuple(z[0].name for z in zones)
    parts = [
        n.id_data.name,
        n.name,
        getattr(collection, "name", None),
        round(float(getattr(n, "mask_plateau", 0.0) or 0.0), 6),
        # 选中并集（有序元组）：切换选择（A→B、A→∅、多选增删）触发重建
        ("sel", sel_names),
    ]
    if not zones:
        return tuple(parts)
    for target in _preview_targets(n):
        parts.append((
            target.name,
            tuple(round(v, 6) for v in np.array(target.matrix_world, dtype=np.float64).reshape(-1)),
            len(target.data.vertices),
            _preview_deform_signature(target),
        ))
    # 只对参与预览的（选中）区域收签名：未选中区域的变化不触发重建（零churn）
    for empty, _s in zones:
        parts.append(_preview_zone_signature_part(empty))
    return tuple(parts)


def _preview_target_field(node, verts_world, tri, zones, plateau, target_name=None,
                          topo_key=None, mesh_key=None):
    """按 Blender 当前场景坐标计算单个目标网格的逐顶点合并权重（所见即所得）。
    预览直接使用网格 world 顶点与空物体 world 矩阵，不做任何导出期变换
    （非镜像 X 镜像补偿、参考物体逆、导出空间矩阵）——那些只属于导出烘焙；
    否则球会被翻到另一侧，沿表面扩散到非当前物体。
    包含物体列表过滤：球未包含当前目标网格时跳过；每球独立决定是否沿表面扩散。
    topo_key/mesh_key 由调用方传入以命中拓扑/逐球场缓存；缺省时现场计算。"""
    verts_world = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    zones = [z for z in zones if _zone_allowed_by_target(z[1], target_name)]
    if not zones:
        return np.zeros(len(verts_world), dtype=np.float64)
    if any(_zone_propagate(s, node) for _empty, s in zones):
        if topo_key is None:
            topo_key = _preview_topology_key(verts_world, tri, None)
        topo = _preview_cached_topology(verts_world, tri, weld_tol=None, key=topo_key)
        field = _preview_field_from_topology(
            node, topo, zones, plateau, topo_key=topo_key
        )
    else:
        # 全部体积球：无需拓扑，只保留顶点集。vol 键并入几何内容哈希
        # （传入的 topo_key 即 _preview_mesh_data 已算出的 _preview_topology_key；
        # 缺省时现场计算），使形变/目标矩阵变化但顶点数不变时逐球场缓存
        # 不再误命中旧几何的场。
        if topo_key is None:
            topo_key = _preview_topology_key(verts_world, tri, None)
        topo = {"world_pts": verts_world, "edge_verts": None, "adjacency": None}
        topo_key = ("vol", mesh_key, len(verts_world), topo_key)
        field = _preview_field_from_topology(
            node, topo, zones, plateau, topo_key=topo_key
        )
    return field


_PREVIEW_TOPOLOGY_CACHE = {}
_PREVIEW_TOPOLOGY_ORDER = []
_PREVIEW_TOPOLOGY_CACHE_MAX = 16

# 网格数据缓存（按目标矩阵 + 顶点/面数复用 verts_world/tri/topo_key，
# 避免每次重建都 foreach_get + calc_loop_triangles + 全量哈希）
_PREVIEW_MESH_CACHE = {}
_PREVIEW_MESH_CACHE_MAX = 32
# 集合模式合并网格拓扑缓存（按各目标矩阵/顶点/面数签名复用焊接+邻接表）
_PREVIEW_MERGED_CACHE = {}
_PREVIEW_MERGED_CACHE_MAX = 8
# 逐球权重场缓存：拓扑与球参数未变时直接复用，区域多时只重算变化的球
_PREVIEW_ZONE_FIELD_CACHE = {}
_PREVIEW_ZONE_FIELD_CACHE_MAX = 96


def _preview_topology_key(verts_world, tri, weld_tol=None):
    """网格数据签名：世界顶点 + 三角形 + 焊接容差，内容变化即失效。"""
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    h.update(np.ascontiguousarray(np.asarray(verts_world, dtype=np.float64)).tobytes())
    h.update(np.ascontiguousarray(np.asarray(tri, dtype=np.int64)).tobytes())
    h.update(str(weld_tol).encode("ascii"))
    return h.hexdigest()


def _preview_cache_get(cache, key):
    """LRU 命中：取值并把 key 移到末尾（dict 保序）。"""
    value = cache.get(key)
    if value is not None:
        cache[key] = cache.pop(key)
    return value


def _preview_cache_put(cache, key, value, max_items):
    """LRU 写入：新项放末尾，超限逐出最旧。"""
    if key in cache:
        cache.pop(key)
    cache[key] = value
    while len(cache) > max_items:
        cache.pop(next(iter(cache)))


def _preview_cached_topology(verts_world, tri, weld_tol=None, key=None):
    """焊接 + 边拓扑 + 世界坐标邻接表，按网格数据签名缓存。
    网格未变时复用（例如拖动空物体触发预览重建时不再重算
    np.unique 三维焊接与邻接表）；球只移动/缩放时各区域共享同一邻接表。
    Returns:
        dict(world_pts, edge_verts, adjacency, cluster_ids)。
    """
    verts_world = np.asarray(verts_world, dtype=np.float64).reshape(-1, 3)
    tri = np.asarray(tri, dtype=np.int64).reshape(-1, 3)
    if key is None:
        key = _preview_topology_key(verts_world, tri, weld_tol)
    cached = _PREVIEW_TOPOLOGY_CACHE.get(key)
    if cached is not None:
        return cached
    if weld_tol is not None:
        quantized = np.round(verts_world / float(weld_tol))
        _, cluster_ids = np.unique(quantized, axis=0, return_inverse=True)
        cluster_ids = cluster_ids.astype(np.int64, copy=False)
        cluster_count = int(cluster_ids.max()) + 1
        cluster_centers = np.zeros((cluster_count, 3), dtype=np.float64)
        np.add.at(cluster_centers, cluster_ids, verts_world)
        counts = np.bincount(cluster_ids, minlength=cluster_count).astype(np.float64)
        cluster_centers /= counts[:, None]
        cluster_tris = cluster_ids[tri.reshape(-1)].reshape(-1, 3)
        cluster_tris = cluster_tris[
            (cluster_tris[:, 0] != cluster_tris[:, 1])
            & (cluster_tris[:, 1] != cluster_tris[:, 2])
            & (cluster_tris[:, 0] != cluster_tris[:, 2])
        ]
        world_pts = cluster_centers
        ids_out = cluster_ids
    else:
        world_pts = verts_world
        cluster_tris = tri
        ids_out = None
    edge_verts = (
        gb_core.edges_from_triangles(cluster_tris)
        if len(cluster_tris) else np.zeros((0, 2), dtype=np.int64)
    )
    adjacency = (
        gb_core.build_surface_adjacency(world_pts, edge_verts)
        if len(edge_verts) else None
    )
    value = {
        "world_pts": world_pts,
        "edge_verts": edge_verts,
        "adjacency": adjacency,
        "cluster_ids": ids_out,
    }
    if key not in _PREVIEW_TOPOLOGY_ORDER:
        _PREVIEW_TOPOLOGY_ORDER.append(key)
    if len(_PREVIEW_TOPOLOGY_ORDER) > _PREVIEW_TOPOLOGY_CACHE_MAX:
        old = _PREVIEW_TOPOLOGY_ORDER.pop(0)
        _PREVIEW_TOPOLOGY_CACHE.pop(old, None)
    _PREVIEW_TOPOLOGY_CACHE[key] = value
    return value


def _preview_zone_distances(node, verts_world, ball_matrix, adjacency, edge_verts,
                            propagate=None, allowed_mask=None):
    """预览沿表面传播距离：均匀缩放球（旋转/平移/等比缩放）复用世界坐标
    邻接表快速路径；非均匀缩放回退逐球构建。propagate=None 默认开启（球级
    沿表面扩散开关移除后无总开关）；allowed_mask 可选，仅允许这些顶点（其余 inf）。"""
    m = np.asarray(ball_matrix, dtype=np.float64).reshape(4, 4)
    use_propagate = True if propagate is None else bool(propagate)
    allowed = None if allowed_mask is None else np.asarray(allowed_mask, dtype=bool).reshape(-1)
    linear = m[:3, :3]
    col_norms = np.linalg.norm(linear, axis=0)
    scale = float(np.mean(col_norms))
    uniform = (
        scale > 1e-9
        and np.allclose(col_norms, scale)
        and np.allclose(linear @ linear.T, scale * scale * np.eye(3), atol=1e-4)
    )
    if use_propagate and adjacency is not None and len(edge_verts) > 0:
        if uniform:
            return gb_core.surface_distances_uniform_scale(
                adjacency, verts_world, m[:3, 3], scale, allowed_mask=allowed
            )
        local = gb_core._to_ball_local(verts_world, ball_matrix)
        if local is None:
            return None
        d2 = np.einsum("ij,ij->i", local, local)
        seeds = np.zeros(local.shape[0], dtype=bool)
        if allowed is None:
            seeds[int(np.argmin(d2))] = True
        else:
            valid_d2 = np.where(allowed, d2, np.inf)
            if not np.any(np.isfinite(valid_d2)):
                return np.full(local.shape[0], np.inf)
            seeds[int(np.argmin(valid_d2))] = True
        return gb_core.surface_distances(local, edge_verts, seeds, allowed_mask=allowed)
    local = gb_core._to_ball_local(verts_world, ball_matrix)
    if local is None:
        return None
    d2 = np.einsum("ij,ij->i", local, local)
    d = np.sqrt(np.maximum(d2, 0.0))
    if allowed is not None:
        d = np.where(allowed, d, np.inf)
    return d


def _preview_zone_field(node, topo, topo_key, empty, s, plateau,
                        allowed=None, allowed_key=None):
    """单球权重场（含逐球缓存）：拓扑与球矩阵/参数未变时直接复用，
    区域很多时只重算变化的球，消除整体重建卡顿。"""
    m = np.asarray(empty.matrix_world, dtype=np.float64).reshape(4, 4)
    zone_key = (
        empty.name,
        tuple(round(v, 6) for v in m.reshape(-1)),
        bool(getattr(s, "propagate", True)),
        round(float(s.brush_strength), 6),
        round(float(s.brush_falloff_k), 6),
        round(float(plateau or 0.0), 6),
        allowed_key,
    )
    cache_key = (topo_key, zone_key)
    cached = _preview_cache_get(_PREVIEW_ZONE_FIELD_CACHE, cache_key)
    if cached is not None:
        return cached
    d = _preview_zone_distances(
        node, topo["world_pts"], m, topo["adjacency"], topo["edge_verts"],
        propagate=_zone_propagate(s, node), allowed_mask=allowed,
    )
    if d is None:
        field = np.zeros(len(topo["world_pts"]), dtype=np.float64)
    else:
        field = np.asarray(
            node._shape_field(d, s.brush_strength, s.brush_falloff_k, plateau),
            dtype=np.float64,
        )
    _preview_cache_put(_PREVIEW_ZONE_FIELD_CACHE, cache_key, field,
                       _PREVIEW_ZONE_FIELD_CACHE_MAX)
    return field


def _preview_deformed_mesh_data(target, depsgraph):
    """depsgraph evaluated mesh（形态键 + 骨骼 + 其他修改器生效后的实际网格），
    返回局部坐标顶点/三角形；失败返回 None（调用方回退基础网格）。"""
    try:
        eval_obj = target.evaluated_get(depsgraph)
        eval_mesh = eval_obj.to_mesh()
        try:
            vcount = len(eval_mesh.vertices)
            if vcount == 0:
                return None
            verts = np.empty(vcount * 3, dtype=np.float64)
            eval_mesh.vertices.foreach_get("co", verts)
            eval_mesh.calc_loop_triangles()
            tri = np.empty(len(eval_mesh.loop_triangles) * 3, dtype=np.int64)
            eval_mesh.loop_triangles.foreach_get("vertices", tri)
            return (verts.reshape(-1, 3), tri.reshape(-1, 3))
        finally:
            eval_obj.to_mesh_clear()
    except Exception:
        return None


def _preview_mesh_data(node, target, depsgraph=None):
    """读取并缓存单个目标网格的世界顶点/三角形/拓扑 key。
    目标矩阵、顶点数、面数与形变签名（形态键值 + 骨骼姿态）均未变时复用
    （跳过 foreach_get + calc_loop_triangles + 拓扑哈希——这些是大量区域下
    预览重建的主要开销）。目标带形态键/骨骼修改器且 depsgraph 可用时优先读
    evaluated mesh（形变后顶点）；获取失败回退基础网格并打印一次警告。
    网格顶点原地编辑但数量不变时预览可能滞后——含形态键几何点编辑
    （改关键点坐标但不改 value/数量时，矩阵/顶点数/面数/形变签名均不变，
    网格数据缓存不失效），关闭再开启预览或移动目标即可刷新。"""
    key = (node.id_data.name, node.name, target.name)
    mesh = target.data
    vcount = len(mesh.vertices)
    if vcount == 0:
        return None
    mw = np.array(target.matrix_world, dtype=np.float64)
    matrix_sig = tuple(round(v, 6) for v in mw.reshape(-1))
    pcount = len(mesh.polygons)
    cached = _preview_cache_get(_PREVIEW_MESH_CACHE, key)
    if cached is not None and len(cached) == 7:
        (c_matrix_sig, c_vcount, c_pcount, c_deform_sig,
         c_verts_world, c_tri, c_topo_key) = cached
        # 便宜键（矩阵/顶点数/面数）先查；形变签名走 armature 共享 digest 缓存，
        # 缓存命中路径不重复全量重算姿态摘要
        if (c_matrix_sig == matrix_sig and c_vcount == vcount
                and c_pcount == pcount
                and c_deform_sig == _preview_deform_signature(target)):
            return (c_verts_world, c_tri, c_topo_key)
    deform_sig = _preview_deform_signature(target)
    if depsgraph is not None and deform_sig is not None:
        deformed = _preview_deformed_mesh_data(target, depsgraph)
        if deformed is not None:
            verts, tri = deformed
            verts_world = verts @ mw[:3, :3].T + mw[:3, 3]
            topo_key = _preview_topology_key(verts_world, tri, None)
            item = (matrix_sig, vcount, pcount, deform_sig,
                    verts_world, tri, topo_key)
            _preview_cache_put(_PREVIEW_MESH_CACHE, key, item, _PREVIEW_MESH_CACHE_MAX)
            return (verts_world, tri, topo_key)
        print(f"[drag preview] 目标 {target.name} 形变网格获取失败，回退基础网格")
    verts = np.empty(vcount * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    verts_world = verts @ mw[:3, :3].T + mw[:3, 3]
    mesh.calc_loop_triangles()
    tri = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int64)
    mesh.loop_triangles.foreach_get("vertices", tri)
    tri = tri.reshape(-1, 3)
    topo_key = _preview_topology_key(verts_world, tri, None)
    item = (matrix_sig, vcount, pcount, deform_sig, verts_world, tri, topo_key)
    _preview_cache_put(_PREVIEW_MESH_CACHE, key, item, _PREVIEW_MESH_CACHE_MAX)
    return (verts_world, tri, topo_key)


def _preview_field_from_topology(node, topo, zones, plateau, allowed_by_zone=None,
                                 topo_key=None, allowed_keys=None):
    """对缓存拓扑的顶点集逐区域合并权重：所有区域共享同一邻接表。
    allowed_by_zone: 可选，与 zones 对齐的每球 allowed 掩码（拓扑节点级），
    用于合并预览里包含物体列表过滤。逐球走 _preview_zone_field 缓存。"""
    world_pts = topo["world_pts"]
    if topo_key is None:
        topo_key = ("topology", len(world_pts))
    field = np.zeros(len(world_pts), dtype=np.float64)
    for idx, (empty, s) in enumerate(zones):
        allowed = None if allowed_by_zone is None else allowed_by_zone[idx]
        if allowed is not None and not np.any(allowed):
            continue
        allowed_key = None if allowed_keys is None else allowed_keys[idx]
        f = _preview_zone_field(
            node, topo, topo_key, empty, s, plateau,
            allowed=allowed, allowed_key=allowed_key,
        )
        np.maximum(field, f, out=field)
    return field


def _preview_merged_mesh(node, meshes, zones, plateau, mesh_names=None, weld_tol=1e-5,
                         merged_key=None):
    """集合预览：把集合内全部网格合并为同一个连续表面再计算沿表面传播。
    共享接缝的顶点（世界坐标差 < weld_tol）会被焊接为同一拓扑节点，
    因此球命中任一部件后能沿表面连续传播到相邻部件；各自独立的部分
    仍保持不连通。包含物体列表按 mesh_names 生成每球 allowed 掩码：
    簇内所有顶点都属于列表内网格才允许（阻止沿焊接接缝进入未包含物体）。
    merged_key 由调用方按各目标签名传入，命中时复用焊接拓扑；逐球走
    _preview_zone_field 缓存。
    返回与 meshes 对齐的逐网格权重数组列表。"""
    parts = []
    vertex_offset = 0
    all_verts = []
    all_tris = []
    for verts_world, tri in meshes:
        n_verts = len(verts_world)
        parts.append((vertex_offset, n_verts))
        all_verts.append(np.asarray(verts_world, dtype=np.float64).reshape(-1, 3))
        all_tris.append(np.asarray(tri, dtype=np.int64).reshape(-1, 3) + vertex_offset)
        vertex_offset += n_verts
    if not all_verts:
        return []
    all_verts = np.concatenate(all_verts, axis=0)
    all_tris = np.concatenate(all_tris, axis=0)
    if merged_key is None:
        # 未由调用方传签名（直接调用/测试）时必须用几何哈希，避免不同网格
        # 因相同顶点/面数命中同一合并拓扑缓存
        merged_key = _preview_topology_key(all_verts, all_tris, weld_tol)
    cached_topo = _preview_cache_get(_PREVIEW_MERGED_CACHE, merged_key)
    if cached_topo is None:
        topo_key = _preview_topology_key(all_verts, all_tris, weld_tol)
        topo = _preview_cached_topology(all_verts, all_tris, weld_tol=weld_tol,
                                        key=topo_key)
        _preview_cache_put(_PREVIEW_MERGED_CACHE, merged_key, (topo, topo_key),
                           _PREVIEW_MERGED_CACHE_MAX)
    else:
        topo, topo_key = cached_topo
    allowed_by_zone, allowed_keys = _merged_zone_allowed_masks(
        zones, mesh_names, parts, topo
    )
    cluster_field = _preview_field_from_topology(
        node, topo, zones, plateau, allowed_by_zone=allowed_by_zone,
        topo_key=topo_key, allowed_keys=allowed_keys,
    )
    cluster_ids = topo.get("cluster_ids")
    if cluster_ids is None:
        cluster_ids = np.arange(len(all_verts))
    field_all = cluster_field[cluster_ids]
    return [field_all[offset:offset + n] for offset, n in parts]


def _merged_zone_allowed_masks(zones, mesh_names, parts, topo):
    """集合预览：每球生成拓扑节点级 allowed 掩码（长度 = len(topo['world_pts'])）。
    包含列表为空 → None（全部允许）；否则簇内所有顶点都属于列表内网格才允许，
    防止沿焊接接缝把权重传播进未包含物体。返回 (allowed_list, allowed_keys_list)，
    allowed_key 由包含名单与网格名决定，供逐球场缓存使用。"""
    cluster_ids = topo.get("cluster_ids")
    if cluster_ids is None:
        cluster_ids = np.arange(sum(n for _offset, n in parts))
    allowed_by_zone = []
    allowed_keys = []
    mesh_names_tuple = tuple(str(m) for m in (mesh_names or ()))
    for _empty, s in zones:
        include = getattr(s, "include_objects", None) or ()
        if not include or not mesh_names:
            allowed_by_zone.append(None)
            allowed_keys.append(("inc", (), mesh_names_tuple))
            continue
        allowed_names = _zone_allowed_names(s)
        if not allowed_names:
            allowed_by_zone.append(None)
            allowed_keys.append(("inc", (), mesh_names_tuple))
            continue
        vertex_allowed = np.zeros(cluster_ids.shape[0], dtype=bool)
        for (offset, n), mname in zip(parts, mesh_names):
            if mname and str(mname) in allowed_names:
                vertex_allowed[offset:offset + n] = True
        if not np.any(vertex_allowed):
            cluster_allowed = np.zeros(int(cluster_ids.max()) + 1, dtype=bool)
            allowed_by_zone.append(cluster_allowed)
            allowed_keys.append(("inc", tuple(sorted(allowed_names)), mesh_names_tuple))
            continue
        counts = np.bincount(cluster_ids, minlength=int(cluster_ids.max()) + 1)
        allowed_counts = np.bincount(
            cluster_ids, weights=vertex_allowed.astype(np.int64), minlength=int(cluster_ids.max()) + 1
        )
        allowed_by_zone.append(allowed_counts == counts)
        allowed_keys.append(("inc", tuple(sorted(allowed_names)), mesh_names_tuple))
    return allowed_by_zone, allowed_keys


def _rebuild_preview_batches(nodes):
    """重算全部启用节点的热力图批次。
    新批次构建成功后才整体替换（中途失败保留旧预览不黑屏）；网格数据、合并拓扑、
    逐球权重场均走缓存，拖动单个区域时只重算该球，避免大量区域下反复全量 Dijkstra。"""
    global _preview_batches
    if not GB_CORE_AVAILABLE:
        return
    import gpu
    from gpu_extras.batch import batch_for_shader
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    # 一次取 depsgraph（形态键/骨骼求值的网格源）；timer 上下文里可用，失败回退基础网格
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
    except Exception:
        depsgraph = None
    new_batches = {}
    for n in nodes:
        # 选中过滤（t11 需求变更）：只预览选中区域空物体的并集；
        # 未选中任何区域空物体 → 跳过该节点（不渲染任何预览）
        zones = _preview_node_zones(n)
        if not zones:
            continue
        targets = _preview_targets(n)
        plateau = getattr(n, "mask_plateau", 0.0)
        # 收集每个目标网格的 world 顶点与三角形（命中网格缓存时跳过
        # foreach_get / calc_loop_triangles / 拓扑哈希）
        mesh_data = []
        for target in targets:
            result = _preview_mesh_data(n, target, depsgraph)
            if result is None:
                continue
            verts_world, tri, topo_key = result
            mesh_data.append((target, verts_world, tri, topo_key))
        if not mesh_data:
            continue

        # 集合预览：所有网格合并为同一连续表面（焊接共享接缝），
        # 沿表面传播可跨部件连续扩散；单物体直接按自身拓扑计算。
        collection_mode = getattr(n, "preview_collection", None) is not None
        if collection_mode:
            merged_key = tuple(
                (target.name, topo_key, len(verts_world))
                for target, verts_world, _tri, topo_key in mesh_data
            )
            fields = _preview_merged_mesh(
                n,
                [(verts_world, tri) for _target, verts_world, tri, _tk in mesh_data],
                zones,
                plateau,
                mesh_names=[target.name for target, _verts_world, _tri, _tk in mesh_data],
                merged_key=merged_key,
            )
        else:
            fields = [
                _preview_target_field(
                    n, verts_world, tri, zones, plateau, target_name=target.name,
                    topo_key=topo_key, mesh_key=(n.id_data.name, n.name, target.name),
                )
                for target, verts_world, tri, topo_key in mesh_data
            ]

        for (target, verts_world, tri, _topo_key), field in zip(mesh_data, fields):
            field = np.asarray(field, dtype=np.float64)
            colors = gb_core.weights_to_colors(field, _PREVIEW_ALPHA).astype(np.float32)
            ghost_colors = np.array(colors, copy=True)
            ghost_colors[:, 3] *= _PREVIEW_GHOST_FACTOR
            pos = verts_world.astype(np.float32)
            indices = np.asarray(tri, dtype=np.int32)
            key = (n.id_data.name, n.name, target.name)
            new_batches[key] = (
                target.name,
                batch_for_shader(shader, 'TRIS', {"pos": pos, "color": colors}, indices=indices),
                batch_for_shader(shader, 'TRIS', {"pos": pos, "color": ghost_colors}, indices=indices),
            )
    _preview_batches = new_batches


def _drag_preview_draw():
    """draw handler：先幽灵层（透视）后主层（深度测试），与高斯球预览一致。"""
    if not _preview_batches:
        return
    try:
        import gpu
        shader = gpu.shader.from_builtin('SMOOTH_COLOR')
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        for _name, _batch, ghost in _preview_batches.values():
            try:
                ghost.draw(shader)
            except Exception:
                pass
        gpu.state.depth_test_set('LESS_EQUAL')
        for _name, batch, _ghost in _preview_batches.values():
            try:
                batch.draw(shader)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            gpu.state.blend_set('NONE')
            gpu.state.depth_test_set('NONE')
        except Exception:
            pass


def _ensure_preview_handler():
    global _preview_handler
    if _preview_handler is None:
        try:
            _preview_handler = bpy.types.SpaceView3D.draw_handler_add(
                _drag_preview_draw, (), 'WINDOW', 'POST_VIEW')
        except Exception:
            _preview_handler = None


def _remove_preview_handler():
    global _preview_handler, _preview_batches, _PREVIEW_DIGEST_GENERATION
    if _preview_handler is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_preview_handler, 'WINDOW')
        except Exception:
            pass
        _preview_handler = None
    _preview_batches = {}
    _PREVIEW_TOPOLOGY_CACHE.clear()
    _PREVIEW_TOPOLOGY_ORDER.clear()
    _PREVIEW_MESH_CACHE.clear()
    _PREVIEW_MERGED_CACHE.clear()
    _PREVIEW_ZONE_FIELD_CACHE.clear()
    _PREVIEW_DIGEST_CACHE.clear()
    _PREVIEW_DIGEST_GENERATION = 0


def _preview_rel_key(datablock):
    """depsgraph update id 的稳定键：as_pointer 优先（bpy 数据块跨访问稳定，
    Python 包装对象的 id() 不可靠），stub/无该方法时回退 id()。"""
    if datablock is None:
        return None
    try:
        return ("ptr", datablock.as_pointer())
    except Exception:
        return ("id", id(datablock))


def _preview_refresh_relevant_ids(nodes):
    """重建相关数据块键集（目标物体/网格/形态键/骨架/区域空物体/集合）。
    仅在 dirty 后的全量签名路径调用（O(Σ(targets+zones))），空闲 tick 不付。"""
    global _PREVIEW_RELEVANT_IDS
    relevant = set()
    for n in nodes:
        collection = getattr(n, "preview_collection", None)
        if collection is not None:
            relevant.add(_preview_rel_key(collection))
        for target in _preview_targets(n):
            relevant.add(_preview_rel_key(target))
            mesh = getattr(target, "data", None)
            if mesh is not None:
                relevant.add(_preview_rel_key(mesh))
                sk = getattr(mesh, "shape_keys", None)
                if sk is not None:
                    relevant.add(_preview_rel_key(sk))
            for mod in getattr(target, "modifiers", None) or ():
                if getattr(mod, "type", None) != 'ARMATURE':
                    continue
                arm = getattr(mod, "object", None)
                if arm is not None:
                    relevant.add(_preview_rel_key(arm))
                    arm_data = getattr(arm, "data", None)
                    if arm_data is not None:
                        relevant.add(_preview_rel_key(arm_data))
        for item in getattr(n, "zone_objects", ()):
            empty = getattr(item, "zone_object", None)
            if empty is not None:
                relevant.add(_preview_rel_key(empty))
    relevant.discard(None)
    _PREVIEW_RELEVANT_IDS = relevant


def _preview_depsgraph_update(depsgraph):
    """depsgraph_update_post 回调：低成本过滤（O(|updates|) 集合查找）。
    仅当本次更新涉及预览相关对象（目标网格/骨架/形态键/区域空物体/集合/节点树）
    才置 _preview_dirty；无可过滤信息（updates 缺失/相关集未建）时保守置脏。
    只读已求值图的更新清单，不触发任何再求值。"""
    global _preview_dirty
    if _preview_dirty:
        return
    relevant = _PREVIEW_RELEVANT_IDS
    if not relevant:
        _preview_dirty = True
        return
    updates = getattr(depsgraph, "updates", None)
    if updates is None:
        # 无更新清单（旧版 Blender）：无法过滤 → 保守置脏
        _preview_dirty = True
        return
    for u in updates:
        iid = getattr(u, "id", None)
        if iid is None:
            _preview_dirty = True
            return
        if _preview_rel_key(iid) in relevant:
            _preview_dirty = True
            return
        tname = type(iid).__name__
        if tname in ("Collection", "NodeGroup"):
            # 集合成员/节点列表变更的主要载体（罕见，需兜底）
            _preview_dirty = True
            return


def _ensure_preview_deps_handler():
    """注册 depsgraph_update_post 失效 handler（幂等）。
    stub bpy / 旧版 Blender 无 app.handlers → 静默降级（回退轮询）。
    与 _ensure_preview_running 的生命周期对齐（预览激活即注册）。"""
    global _preview_deps_registered
    if _preview_deps_registered:
        return
    try:
        handlers = getattr(getattr(bpy, "app", None), "handlers", None)
        deps = getattr(handlers, "depsgraph_update_post", None) if handlers is not None else None
        if deps is None:
            return
        if _preview_depsgraph_update not in deps:
            deps.append(_preview_depsgraph_update)
        _preview_deps_registered = True
    except Exception:
        _preview_deps_registered = False


def _remove_preview_deps_handler():
    """注销 depsgraph_update_post handler（幂等）；预览停用时清理并重置状态。"""
    global _preview_deps_registered, _PREVIEW_RELEVANT_IDS
    global _preview_dirty, _preview_last_selection, _preview_safety_count
    if not _preview_deps_registered:
        return
    try:
        handlers = getattr(getattr(bpy, "app", None), "handlers", None)
        deps = getattr(handlers, "depsgraph_update_post", None) if handlers is not None else None
        if deps is not None and _preview_depsgraph_update in deps:
            deps.remove(_preview_depsgraph_update)
    except Exception:
        pass
    _preview_deps_registered = False
    _PREVIEW_RELEVANT_IDS = None
    _preview_dirty = True
    _preview_last_selection = None
    _preview_safety_count = 0


def _preview_tick():
    """轮询预览，并将连续编辑产生的多次变化合并为一次批次重建。
    事件驱动模式（depsgraph handler 已注册）：空闲 tick O(1)
    （dirty 标志 + 选中集轮询），零骨骼/矩阵/depsgraph 访问；
    降级模式（无 handler 的 stub/旧版）回退 t8 全量签名轮询。"""
    global _preview_timer, _preview_sig_cache, _PREVIEW_DIGEST_GENERATION
    global _preview_pending_signature, _preview_pending_since
    global _preview_dirty, _preview_last_selection, _preview_safety_count
    # 推进 armature 姿态摘要代号：本 tick 内所有签名/重建共享同一代摘要
    # （每次 O(B) 一次），姿态变化在下一 tick 被检出（0.2s 检测延迟）。
    _PREVIEW_DIGEST_GENERATION = (_PREVIEW_DIGEST_GENERATION + 1) & 0xFFFF
    nodes = _collect_preview_nodes()
    if not nodes:
        _remove_preview_handler()
        _remove_preview_deps_handler()
        _preview_timer = None
        _preview_sig_cache = None
        _preview_pending_signature = None
        _preview_pending_since = None
        return None  # 取消 timer
    _ensure_preview_handler()
    event_driven = _preview_deps_registered
    if event_driven:
        sel = _preview_selection_digest(nodes)
        if sel != _preview_last_selection:
            _preview_last_selection = sel
            _preview_dirty = True
        if _preview_dirty or _preview_sig_cache is None:
            _preview_safety_count = 0
        elif _preview_safety_count < _PREVIEW_SAFETY_TICKS - 1:
            # ★ O(1) 空闲：零骨骼遍历/零矩阵读取/零 depsgraph 求值
            _preview_safety_count += 1
            return _PREVIEW_TICK
        else:
            # 安全阀：每 N 个干净 tick 强制一次全量签名（兜 driver 漏报）
            _preview_safety_count = 0
    try:
        sig = tuple(_preview_signature(n) for n in nodes)
        if event_driven:
            # dirty 后已付全量签名：同步刷新相关键集（区域成员增删等
            # 不翻转签名但会改变相关集，handler 过滤依赖其新鲜度）
            _preview_refresh_relevant_ids(nodes)
        now = _preview_now()
        if _preview_sig_cache is None:
            _rebuild_preview_batches(nodes)
            _preview_sig_cache = sig
            _preview_pending_signature = None
            _preview_pending_since = None
            _preview_dirty = False
        elif sig == _preview_sig_cache:
            _preview_pending_signature = None
            _preview_pending_since = None
            _preview_dirty = False        # 误报：dirty 但签名未变
        elif sig != _preview_pending_signature:
            _preview_pending_signature = sig
            _preview_pending_since = now
        elif now - _preview_pending_since >= _PREVIEW_DEBOUNCE:
            _rebuild_preview_batches(nodes)
            _preview_sig_cache = sig
            _preview_pending_signature = None
            _preview_pending_since = None
            _preview_dirty = False
    except Exception:
        # 重建失败时保留可诊断输出（控制台可见 traceback），避免静默黑屏
        import traceback
        traceback.print_exc()
    return _PREVIEW_TICK


def _ensure_preview_running():
    """幂等启动 timer（节点 draw 自愈调用；无启用节点时 tick 自行取消）。"""
    global _preview_timer
    if _preview_timer is None and _collect_preview_nodes():
        try:
            _preview_timer = bpy.app.timers.register(_preview_tick, first_interval=_PREVIEW_TICK)
        except Exception:
            _preview_timer = None
    _ensure_preview_deps_handler()


def _preview_cleanup():
    """插件注销时清理 handler 与 timer，防止重载残留。"""
    global _preview_timer, _preview_sig_cache
    global _preview_pending_signature, _preview_pending_since
    if _preview_timer is not None:
        try:
            bpy.app.timers.unregister(_preview_timer)
        except Exception:
            pass
        _preview_timer = None
    _preview_sig_cache = None
    _preview_pending_signature = None
    _preview_pending_since = None
    _remove_preview_handler()
    _remove_preview_deps_handler()


classes = (
    SSMT_DragZoneIncludeRef,
    SSMT_DragZoneColliderRef,
    SSMT_DragZoneSettings,
    SSMT_DragZoneRef,
    SSMT_OT_DragZoneAdd,
    SSMT_OT_DragZoneRemove,
    SSMT_OT_DragZonePage,
    SSMT_OT_DragZoneIncludeAdd,
    SSMT_OT_DragZoneIncludeRemove,
    SSMT_OT_DragZoneColliderAdd,
    SSMT_OT_DragZoneColliderRemove,
    SSMTNode_PostProcess_DragInteraction,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.ssmt_drag_zone = bpy.props.PointerProperty(type=SSMT_DragZoneSettings)


def unregister():
    _preview_cleanup()
    # t30：EFMI 权重预览 handler 生命周期——插件注销时一并拆卸（非 EFMI 环境
    # 该模块未导入/无 handler，guarded 幂等）
    try:
        from .node_postprocess_draginteraction_efmi import _efmi_preview_cleanup
        _efmi_preview_cleanup()
    except Exception:
        pass
    del bpy.types.Object.ssmt_drag_zone
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
