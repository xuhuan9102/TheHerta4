"""导出步骤计划：**导出开始时一次算好**的固定步骤清单。

进度条的百分比 = ``done / total``，其中 ``total`` 必须从一开始就是最终值，
不能在导出过程中随新阶段出现而增长（否则分母一直在变，百分比不准）。
所以主进程（``ui_func_export`` 的预估、``export_background`` 的会话）与
worker（``export_background._run_worker`` / ``direct_export``）都调用这里**同一个**
``build_step_plan`` 拿清单，``total = len(清单)`` 全程不变。

清单内容按导出模式固定：

- 直出模式：直出顶层阶段（7 个）+ 前处理子阶段（按 ``has_shapekey_export`` 给
  固定集合）+ 基础导出内部子阶段（8 个）+ 每个后处理节点 1 步 + 直出形态键
  生成 / 多文件生成 / 传统形态键附加轮次（按配置）+ 收尾 1 步；
- 普通模式：每个轮次 1 步 + 每个后处理节点 1 步 + 收尾 1 步；
- 快速局部导出：单轮 1 步 + 后处理节点（临时树通常无节点）+ 收尾 1 步。

计步规则（见 ``export_progress``）：清单内、且整次导出里没计过的名字才
``done += 1``；同名子阶段重复出现只计一次；清单之外的阶段只更新状态栏文本。
前处理缓存开/关、是否存在形态键等会让实际阶段数略有出入，这里按**固定集合 /
宁可多算 1~2 步**处理，收尾 ``finish_steps()`` 会把 ``done`` 补满到 ``total``。

阶段名必须与 ``utils/timer_utils.py::TimerUtils._stage_display_name_map`` 的
**中文显示名**一致 —— ``advance_stage`` 收到的就是显示名。唯一例外是
``Preprocess-BakeDisabledShapeKeys``：显示名映射里没有它，按原英文名计步。
"""

#: 直出流程固定执行的顶层阶段（TimerUtils 显示名）
DIRECT_BASE_STAGE_STEPS = (
    "直出-收集节点",
    "直出-初始化状态",
    "直出-收集物体",
    "直出-执行前处理",
    "直出-解析蓝图模型",
    "直出-构建导出器",
    "直出-基础导出",
)

#: 前处理子阶段（``preprocess.py`` 里串行前处理会走到的固定集合）
PREPROCESS_STAGE_STEPS = (
    "前处理-计算缓存哈希",
    "前处理-加载缓存",
    "前处理-创建副本",
    "前处理-清空形态键",
    "前处理-恢复形态键",
    "前处理-应用形态键",
    "前处理-重命名UV",
    "前处理-恢复非镜像",
)

#: 直出形态键采样额外走的前处理子阶段
PREPROCESS_SHAPEKEY_STEPS = (
    # TimerUtils 的显示名映射里没有这个阶段，advance_stage 收到的是原英文名
    "Preprocess-BakeDisabledShapeKeys",
    "前处理-采样直出形态键",
)

#: 基础导出内部子阶段（同名子阶段会反复切换，计步只计一次）
BASE_EXPORT_STEPS = (
    "数据哈希预计算",
    "对象处理与合并",
    "Element数据解析",
    "Element数据打包",
    "索引去重与Buffer构建",
    "ShapeKey计算",
    "缓冲文件生成",
    "INI配置生成",
)

#: 直出形态键资源生成（形态键后处理节点内部的独立阶段）
DIRECT_SHAPEKEY_STEP = "直出-形态键生成"
#: 直出多文件资源生成
DIRECT_MULTIFILE_STEP = "直出-生成多文件资源"
#: 未启用直出的形态键节点会补跑传统附加轮次（整段只算 1 步）
DIRECT_EXTRA_ROUND_STEP = "直出-形态键附加轮次"
#: 快速局部导出的单轮
QUICK_EXPORT_STEP = "快速局部导出"
#: 收尾（``finish_steps`` 补满）
FINAL_STEP = "收尾"

#: 形态键 / 多文件后处理节点的 bl_idname（与 ``direct_export`` 同口径）
SHAPEKEY_POSTPROCESS_IDNAME = "SSMTNode_PostProcess_ShapeKey"
MULTIFILE_POSTPROCESS_IDNAME = "SSMTNode_PostProcess_MultiFile"


def _dedupe(keys) -> list:
    """保持顺序去重；空名丢弃。"""
    plan = []
    seen = set()
    for key in keys or []:
        text = str(key or "")
        if not text or text in seen:
            continue
        seen.add(text)
        plan.append(text)
    return plan


def _postprocess_nodes(tree, postprocess_nodes=None) -> list:
    """取导出链路上的后处理节点（与直出 / 普通导出同一个收集函数）。"""
    if postprocess_nodes is not None:
        return list(postprocess_nodes or [])
    if tree is None:
        return []
    try:
        from .export_helper import BlueprintExportHelper

        return list(BlueprintExportHelper._collect_postprocess_nodes(tree) or [])
    except Exception:
        return []


def _node_progress_key(node) -> str:
    """节点进度键：``树名/节点名``（与 ``direct_export._node_progress_key`` 一致）。"""
    name = str(getattr(node, "name", "") or "").strip()
    if not name:
        return ""
    tree_name = str(getattr(getattr(node, "id_data", None), "name", "") or "").strip()
    if not tree_name:
        return name
    return f"{tree_name}/{name}"


def _node_step_keys(nodes, *, direct_export) -> list:
    """后处理节点各自的步骤键。

    直出模式 worker 上报的是 ``树名/节点名``；普通模式进度桥上报的是纯节点名。
    """
    keys = []
    for node in nodes or []:
        key = _node_progress_key(node) if direct_export else str(getattr(node, "name", "") or "")
        if key:
            keys.append(key)
    return _dedupe(keys)


def _split_shapekey_nodes(nodes):
    """把后处理节点拆成（全部形态键节点, 启用直出的形态键节点）。"""
    shapekey_nodes = []
    direct_shapekey_nodes = []
    for node in nodes or []:
        if str(getattr(node, "bl_idname", "") or "") != SHAPEKEY_POSTPROCESS_IDNAME:
            continue
        shapekey_nodes.append(node)
        if bool(getattr(node, "direct_export_mode", False)):
            direct_shapekey_nodes.append(node)
    return shapekey_nodes, direct_shapekey_nodes


def _has_direct_multifile_nodes(nodes) -> bool:
    for node in nodes or []:
        if str(getattr(node, "bl_idname", "") or "") != MULTIFILE_POSTPROCESS_IDNAME:
            continue
        if bool(getattr(node, "direct_export_mode", False)):
            return True
    return False


def build_step_plan(
    tree,
    *,
    direct_export: bool,
    has_shapekey_export: bool = False,
    max_shapekey_slot: int = 0,
    has_multi_file_export: bool = False,
    round_plans=None,
    quick_export: bool = False,
    postprocess_nodes=None,
) -> list:
    """返回本次导出**固定不变**的步骤键清单（有序、无重复）。

    ``total = len(返回清单)`` 在导出开始时一次确定，之后不再增长。参数与
    manifest 字段 / ``ui_func_export._create_background_session`` 的取值一致，
    保证主进程「加载工程」阶段显示的分母就是最终分母。
    """
    rounds = list(round_plans or [])
    nodes = _postprocess_nodes(tree, postprocess_nodes)

    if quick_export:
        plan = [QUICK_EXPORT_STEP]
        # 快速局部导出走普通后处理桥（上报纯节点名）
        plan += _node_step_keys(nodes, direct_export=False)
        plan.append(FINAL_STEP)
        return _dedupe(plan)

    if not direct_export:
        plan = [f"第 {index} 轮" for index in range(1, len(rounds) + 1)]
        plan += _node_step_keys(nodes, direct_export=False)
        plan.append(FINAL_STEP)
        return _dedupe(plan)

    shapekey_nodes, direct_shapekey_nodes = _split_shapekey_nodes(nodes)
    has_shapekey = bool(has_shapekey_export) or bool(shapekey_nodes) or int(max_shapekey_slot or 0) > 0
    # 节点列表拿不到时（收集失败）按「有形态键节点就认为走直出生成」保守处理
    direct_shapekey = bool(direct_shapekey_nodes) or (has_shapekey and not shapekey_nodes)
    needs_standard_rounds = any(
        not bool(getattr(node, "direct_export_mode", False)) for node in shapekey_nodes
    )
    has_multifile = _has_direct_multifile_nodes(nodes) or bool(has_multi_file_export)
    frame_table_all_channels = any(
        getattr(node, "use_frame_table", False)
        and getattr(node, "store_all_vertex_channels", False)
        for node in direct_shapekey_nodes
    )

    plan = list(DIRECT_BASE_STAGE_STEPS)
    if frame_table_all_channels:
        plan += [
            "前处理-计算缓存哈希",
            "前处理-加载缓存",
            "前处理-创建副本",
            "前处理-清空形态键",
            "前处理-应用约束",
            "前处理-应用修改器",
            "前处理-三角化",
            "前处理-应用变换",
            "前处理-恢复形态键",
            "前处理-重命名UV",
            "前处理-恢复非镜像",
            "Preprocess-BakeDisabledShapeKeys",
        ]
    else:
        plan += list(PREPROCESS_STAGE_STEPS)
    if has_shapekey and not frame_table_all_channels:
        plan += list(PREPROCESS_SHAPEKEY_STEPS)
    plan += list(BASE_EXPORT_STEPS)
    plan += _node_step_keys(nodes, direct_export=True)
    if direct_shapekey:
        plan.append(DIRECT_SHAPEKEY_STEP)
    if has_multifile:
        plan.append(DIRECT_MULTIFILE_STEP)
    if has_shapekey and needs_standard_rounds:
        plan.append(DIRECT_EXTRA_ROUND_STEP)
    plan.append(FINAL_STEP)
    return _dedupe(plan)
