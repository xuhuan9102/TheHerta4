
'''
导入模型配置面板
'''
import os

import bpy
from bpy_extras.io_utils import ImportHelper

from ..utils.collection_utils import CollectionColor, CollectionUtils
from ..utils.json_utils import JsonUtils
from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.logic_name import LogicName
from ..common.non_mirror_workflow import NonMirrorWorkflowHelper
from ..common.object_prefix_helper import ObjectPrefixHelper
from ..common.ssmt_import_helper import SSMTImportHelper
from ..common.workspace_helper import WorkSpaceHelper
from .ntmi_modimp.ntemi_importer import (
    NTEMIImportHelper,
    NtemiDrawCallMeta,
    _discover_draw_calls,
    _load_frame_analysis_dir_map,
    _resolve_deduped_texture_dir,
    _resolve_frame_analysis_dir,
    _load_component_name_map,
    _perform_bone_merge_postprocess,
    NTEMI_PROFILE_ID,
)
from .ui_prefix_quick_ops import PrefixQuickOpsHelper


def _create_original_model_frame(tree, label="原始模型"):
    """创建一个用于容纳原始模型节点的框架"""
    frame = tree.nodes.new('NodeFrame')
    frame.label = label
    frame.use_custom_color = True
    frame.color = (0.2, 0.35, 0.2)
    return frame


def _parent_nodes_to_frame(frame, nodes):
    """将一组节点设置为指定框架的子节点"""
    for node in nodes:
        if node is None:
            continue
        node.parent = frame


def _extract_gametype_name(import_folder_path: str) -> str:
    """从导入路径中提取游戏类型名称（如 TYPE_XXX 中的 XXX）"""
    normalized = str(import_folder_path or "").replace("\\", "/")
    marker = "TYPE_"
    index = normalized.rfind(marker)
    if index == -1:
        return ""
    return normalized[index + len(marker):].strip("/").strip()


# 骨架组合集的轮换颜色（COLOR_01~08 轮换，便于在大纲视图区分不同组；
# 直接用字符串字面量而非 CollectionColor 枚举——测试环境 stub 没有完整枚举值）
_SKELETON_GROUP_COLORS = [
    "COLOR_02", "COLOR_04", "COLOR_06", "COLOR_03",
    "COLOR_07", "COLOR_08", "COLOR_05", "COLOR_01",
]


def _zzmi_collection_name_exists(collection_name: str) -> bool:
    """检查 Blender 数据库中是否已有该合集名。"""
    collections = getattr(getattr(bpy, "data", None), "collections", None)
    if collections is None:
        return False
    try:
        return collections.get(collection_name) is not None
    except (AttributeError, TypeError):
        try:
            return collection_name in collections
        except (TypeError, AttributeError):
            return False


def _zzmi_unique_collection_name(base_name: str) -> str:
    """返回 Blender 合集名；已有名称时显式生成 ``.001``、``.002``。"""
    if not _zzmi_collection_name_exists(base_name):
        return base_name
    suffix = 1
    while True:
        candidate = f"{base_name}.{suffix:03d}"
        if not _zzmi_collection_name_exists(candidate):
            return candidate
        suffix += 1


def _zzmi_get_or_create_skeleton_group_collection(
    parent_collection,
    skeleton_group: int,
    collection_cache: dict | None = None,
):
    """获取本次导入的骨架组合集，不复用其它导入遗留的同名合集。

    ``collection_cache`` 由一次完整导入独占：同一父合集和 SkeletonGroup 的多个
    Component 仍共享一个合集；下一次导入使用新的 cache，即使父合集下已有
    ``SkeletonGroup_N``，也会创建 ``SkeletonGroup_N.001``。
    """
    if collection_cache is None:
        collection_cache = {}
    try:
        parent_key = int(parent_collection.as_pointer())
    except (AttributeError, TypeError, ValueError):
        parent_key = id(parent_collection)
    cache_key = (parent_key, int(skeleton_group))
    group_collection = collection_cache.get(cache_key)
    if group_collection is not None:
        return group_collection

    base_name = f"SkeletonGroup_{int(skeleton_group)}"
    collection_name = _zzmi_unique_collection_name(base_name)
    group_collection = CollectionUtils.create_new_collection(
        collection_name=collection_name,
        color_tag=_SKELETON_GROUP_COLORS[int(skeleton_group) % len(_SKELETON_GROUP_COLORS)],
    )
    parent_collection.children.link(group_collection)
    collection_cache[cache_key] = group_collection
    return group_collection


def _zzmi_move_to_skeleton_group_collection(
    obj,
    import_key: str,
    parent_collection,
    collection_cache: dict | None = None,
) -> None:
    """ZZMI 分组版骨骼合并：把导入对象移入其骨架组合集（SkeletonGroup_<N>）。

    合集挂在该对象的父合集（LOD 合集）下；对象从原合集移出、链接进组集合。
    仅在子网格 json 已有 SkeletonGroup 字段（ensure_skeleton_data 分组版写回）时生效；
    无字段（旧缓存/未生成）保持原合集归属不变。失败不影响导入。
    """
    from ..common.zzmi_skeleton import ZZMISkeletonMergeHelper

    workspace_root = GlobalConfig.path_workspace_folder()
    json_path = ZZMISkeletonMergeHelper._resolve_submesh_json_path(workspace_root, import_key)
    if not json_path:
        return
    submesh_json = JsonUtils.LoadFromFile(json_path)
    if not isinstance(submesh_json, dict):
        return
    skeleton_group = submesh_json.get("SkeletonGroup")
    if skeleton_group is None:
        return
    try:
        skeleton_group = int(skeleton_group)
    except (TypeError, ValueError):
        return

    group_collection = _zzmi_get_or_create_skeleton_group_collection(
        parent_collection,
        skeleton_group,
        collection_cache=collection_cache,
    )

    for coll in list(obj.users_collection):
        coll.objects.unlink(obj)
    group_collection.objects.link(obj)


def _build_workspace_import_targets(workspace_collection):
    """构建工作空间中所有可导入子模型的迭代器，每个条目包含路径、显示名等信息"""
    partition_folder_paths = WorkSpaceHelper.get_workspace_partition_folderpath_list()
    target_base_paths = partition_folder_paths or [GlobalConfig.path_workspace_folder()]

    for base_path in target_base_paths:
        base_name = os.path.basename(os.path.normpath(base_path))
        base_collection = workspace_collection
        if partition_folder_paths:
            base_collection = CollectionUtils.create_new_collection(
                collection_name=base_name,
                color_tag=CollectionColor.Orange,
            )
            workspace_collection.children.link(base_collection)

        lod_submesh_dict = WorkSpaceHelper.get_lod_submesh_folderpath_dict(base_path)
        if lod_submesh_dict:
            for lod_name, submesh_folder_paths in lod_submesh_dict.items():
                lod_collection = CollectionUtils.create_new_collection(
                    collection_name=lod_name,
                    color_tag=CollectionColor.Blue,
                )
                base_collection.children.link(lod_collection)

                lod_folder_path = os.path.join(base_path, lod_name)
                drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict_for_path(base_path)
                lod_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict_for_path(lod_folder_path)
                drawib_aliasname_dict.update(lod_aliasname_dict)

                for submesh_folder_path in submesh_folder_paths:
                    submesh_folder_name = os.path.basename(submesh_folder_path)
                    parts = submesh_folder_name.split("-")
                    yield {
                        "import_key": lod_name + "." + submesh_folder_name,
                        "submesh_folder_path": submesh_folder_path,
                        "submesh_folder_name": submesh_folder_name,
                        "display_name": WorkSpaceHelper._compose_lod_name(
                            lod_name,
                            WorkSpaceHelper.get_display_submesh_name(
                                submesh_folder_name,
                                drawib_aliasname_dict=drawib_aliasname_dict,
                            ),
                        ),
                        "alias_name": WorkSpaceHelper.get_object_display_name(
                            submesh_folder_name,
                            drawib_aliasname_dict=drawib_aliasname_dict,
                        ),
                        "draw_ib": parts[0] if len(parts) >= 1 else "",
                        "component": parts[1] if len(parts) >= 2 else "1",
                        "import_collection": lod_collection,
                    }
            continue

        drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict_for_path(base_path)
        for submesh_folder_path in WorkSpaceHelper._get_submesh_folderpath_list_from(base_path):
            submesh_folder_name = os.path.basename(submesh_folder_path)
            parts = submesh_folder_name.split("-")
            yield {
                "import_key": submesh_folder_name,
                "submesh_folder_path": submesh_folder_path,
                "submesh_folder_name": submesh_folder_name,
                "display_name": WorkSpaceHelper.get_display_submesh_name(
                    submesh_folder_name,
                    drawib_aliasname_dict=drawib_aliasname_dict,
                ),
                "alias_name": WorkSpaceHelper.get_object_display_name(
                    submesh_folder_name,
                    drawib_aliasname_dict=drawib_aliasname_dict,
                ),
                "draw_ib": parts[0] if len(parts) >= 1 else "",
                "component": parts[1] if len(parts) >= 2 else "1",
                "import_collection": base_collection,
            }


def _detect_ntemi_workspace() -> bool:
    """检测当前工作空间是否为 NTEMI（异环·安魂曲）工作空间"""
    if GlobalConfig.logic_name == LogicName.NTEMI:
        return True
    workspace_root = GlobalConfig.path_workspace_folder()
    if not workspace_root or not os.path.isdir(workspace_root):
        return False
    lod0_dir = os.path.join(workspace_root, "LOD0")
    if not os.path.isdir(lod0_dir):
        return False
    import json as _json
    from pathlib import Path as _Path
    for entry in os.scandir(lod0_dir):
        if not entry.is_dir():
            continue
        type_subdirs = sorted(_Path(entry.path).glob("TYPE_*"))
        for type_dir in type_subdirs:
            for json_file in type_dir.glob("*.json"):
                try:
                    payload = _json.loads(json_file.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if str(payload.get("GamePreset", "") or "").strip().upper() == "NTEMI":
                    return True
    return False


def _parse_import_lod_prefix(import_key: str) -> str:
    """解析 import_key 的 LOD 前缀（'LOD1.xxx' -> 'LOD1'；无前缀 -> ''）。

    与 common/efmi_skeleton.EFMISkeletonMergeHelper._parse_lod_name 语义一致
    （本模块无 bpy 依赖可直接 import 的测试面，故本地实现）。
    """
    if "." in import_key and import_key.split(".", 1)[0].upper().startswith("LOD"):
        return import_key.split(".", 1)[0]
    return ""


def _projection_fail_closed_decisions(import_keys: list[str]) -> dict[str, str]:
    """classify 不可用/异常时的 fail-closed 兜底裁决（'import' | 'fail_closed'）。

    基准判定与 common/efmi_skeleton.EFMISkeletonMergeHelper.
    classify_projection_import_targets **同一口径**：reference_lod = 'LOD0'
    （请求中存在时）否则按字典序首个 LOD；无 LOD 前缀目标不受投影约束。
    **LOD1-only 工作空间**（请求中无 LOD0）→ 基准即 LOD1，全部放行（与
    classify 一致，避免兜底与主路径口径分裂）；LOD0 存在时非基准 LOD 目标
    一律 fail-closed（缺状态/裁决不可用 → 默认排除，绝不 fail-open）。
    """
    lods = sorted({_parse_import_lod_prefix(key) for key in import_keys} - {""})
    reference_lod = "LOD0" if "LOD0" in lods else (lods[0] if lods else "")
    decisions: dict[str, str] = {}
    for key in import_keys:
        lod_name = _parse_import_lod_prefix(key)
        if not lod_name or lod_name == reference_lod:
            decisions[key] = "import"
        else:
            decisions[key] = "fail_closed"
    return decisions


def _node_xy(node):
    location = getattr(node, "location", None)
    return (
        float(getattr(location, "x", 0.0) if location is not None else 0.0),
        float(getattr(location, "y", 0.0) if location is not None else 0.0),
    )


def _node_size(node):
    return (
        max(1.0, float(getattr(node, "width", 300.0) or 300.0)),
        max(1.0, float(getattr(node, "height", 200.0) or 200.0)),
    )


def _layout_efmi_auto_main_chain(
    group_node,
    rename_node=None,
    process_node=None,
    output_node=None,
    horizontal_gap=160.0,
):
    """Lay out group -> rename -> VG process -> output from the approved template."""
    template_widths = {
        "group": 200.0,
        "rename": 380.0,
        "process": 700.0,
        "output": 400.0,
    }
    # 按调用角色套模板宽度（不依赖 bl_idname）：真实节点与测试桩一致生效，
    # 布局坐标也随之使用模板后的宽度（与测试契约的坐标断言自洽）。
    for key, node in (
        ("group", group_node),
        ("rename", rename_node),
        ("process", process_node),
        ("output", output_node),
    ):
        if node is not None:
            node.width = template_widths[key]
    positions = {"group": _node_xy(group_node)}
    current = group_node
    current_x, base_y = positions["group"]
    for key, node in (
        ("rename", rename_node),
        ("process", process_node),
        ("output", output_node),
    ):
        if node is None:
            continue
        current_width, _ = _node_size(current)
        current_x += current_width + horizontal_gap
        positions[key] = (current_x, base_y)
        current = node
    return positions


def _layout_efmi_match_nodes(
    group_node,
    match_nodes,
    max_per_row=6,
    horizontal_gap=80.0,
    vertical_gap=700.0,
    offset_x=500.0,
    offset_y=1200.0,
):
    """Place matching nodes in a max-six-column grid using actual dimensions."""
    if not match_nodes:
        return []
    base_x, base_y = _node_xy(group_node)
    _, group_height = _node_size(group_node)
    start_y = base_y - group_height - offset_y
    positions = []
    row_y = start_y
    for row_start in range(0, len(match_nodes), max_per_row):
        row = match_nodes[row_start:row_start + max_per_row]
        current_x = base_x + offset_x
        row_height = max(_node_size(node)[1] for node in row)
        for node in row:
            positions.append((current_x, row_y))
            node_width, _ = _node_size(node)
            current_x += node_width + horizontal_gap
        row_y -= row_height + vertical_gap
    return positions


def _configure_efmi_auto_rename_node(rename_node):
    """Keep generated rename nodes in normal chain order before VG processing."""
    rename_node.defer_until_after_vertex_group_process = False


def _configure_efmi_auto_output_node(output_node, logic_name):
    """Apply EFMI-only defaults to an automatically generated output node."""
    if str(logic_name or "").upper() == "EFMI":
        output_node.use_rabbitfx_slot = True


def _configure_and_execute_efmi_lod_match(
    match_node,
    source_obj,
    target_obj,
    context,
):
    """配置自动匹配节点，并基于当前 Blender 物体立即生成映射文本。

    EFMILODCorrespondence 只负责把 source_obj 与 target_obj 配成一对；这里不接收
    也不消费任何 JSON/VGMap 映射，避免把导入前的骨骼账本误当成导入后物体的
    顶点组命名空间。
    """
    match_node.source_object = source_obj.name
    match_node.target_object = target_obj.name
    # 统一顶点组模式使用同一个全局名称空间；一个处理节点连接的全部匹配表
    # 必须覆盖该链上的所有导入物体。这里不能按源对象名称收窄，否则 LOD0/LOD1
    # 的统一组会被拆成两套，后续全局槽位处理反而失去覆盖。
    # t14 回退：t12 曾把 target_hash 改为专属目标前缀（针对「映射全并集交叉
    # 污染 df4b620c_copy」假设），经用户手动匹配实验 + t11 §6.6（映射键两两
    # 不相交、正确映射并集无害）+ t13-D（用户构建不含 t12）证实非本案病根，
    # 已按用户指令回退为 ""（与 t12 前一致），防修复堆积污染后续排查。
    match_node.target_hash = ""
    match_node.match_threshold = 0.06
    match_node.use_chamfer_matching = False
    match_node.use_shape_key = False
    match_node.create_debug_objects = True
    match_node.rename_format = True
    match_node.exact_hash_match = False
    return match_node.execute_match(context)


def ImprotFromWorkSpaceFull(self, context):
    """从工作空间完整导入所有子模型并构建蓝图节点树"""
    workspace_collection = WorkSpaceHelper.create_and_get_workspace_collection()
    is_ntemi = _detect_ntemi_workspace()

    if is_ntemi:
        return _import_workspace_full_ntemi(self, context, workspace_collection)

    import_targets = list(_build_workspace_import_targets(workspace_collection))
    if not import_targets:
        self.report({'ERROR'}, "当前工作空间未找到可导入的子模型目录。")
        return False

    # None = 非 EFMI/ZZMI 或未请求合并，沿用全局选项；True = 本次整批
    # 预生成完整成功；False = 任一目标失败/异常，整批必须普通导入，不能让
    # JSON 残留 VGMap 造成“部分全局组 + 部分局部组”的混合命名空间。
    merged_vgmap_ready: bool | None = None

    # t15 活性修复（佩丽卡 13:12 实况）：用户「清骨骼合并VGMap缓存 + 重新导入」
    # 时若复选框关闭，预生成 ensure 被跳过 → json 三键保持被清空（L0 段读空 →
    # 域前置全拦）。这里检测「合并元数据缺失」：无论复选框如何，缺失即修复性
    # 重生成（键完好时 missing_merged_metadata_exist=False → 门控保持原语义，
    # 幂等零行为变化；复选框开时行为不变）。
    _efmi_merged_metadata_missing = False
    if GlobalConfig.logic_name == LogicName.EFMI:
        try:
            from ..common.efmi_skeleton import EFMISkeletonMergeHelper as _EFMIMergeHelper
            _efmi_merged_metadata_missing = bool(
                _EFMIMergeHelper.missing_merged_metadata_exist(
                    GlobalConfig.path_workspace_folder(),
                    [target["import_key"] for target in import_targets],
                )
            )
        except Exception:
            _efmi_merged_metadata_missing = False

    # EFMI 骨骼合并数据预生成：导入前把 FrameAnalysis 反查的 VGMap 写回工作空间 json，
    # 使导入流程走全局骨骼索引（json 有 VGMap 且 import_merged_vgmap 开启时自动生效）。
    if (
        GlobalConfig.logic_name == LogicName.EFMI
        and (
            GlobalProterties.import_merged_vgmap()
            or _efmi_merged_metadata_missing
        )
    ):
        merged_vgmap_ready = False
        try:
            from ..common.efmi_skeleton import EFMISkeletonMergeHelper
            import_keys = [target["import_key"] for target in import_targets]
            ok, message = EFMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=GlobalConfig.path_workspace_folder(),
                unique_str_list=import_keys,
                lod_group_projection=GlobalProterties.efmi_lod_group_projection(),
                dedup_enabled=GlobalProterties.efmi_lod_group_dedup(),
            )
            print(f"[EFMI骨骼合并] {message}")
            merged_vgmap_ready = bool(ok)
            if ok:
                self.report({'INFO'}, message)
            else:
                print(f"[EFMI骨骼合并] 未生成骨骼数据：{message}")
                # 部分/全部子网格未生成合并骨架数据时会走普通导入路线，
                # 必须显式警告，避免用户误以为骨骼合并已完整生效。
                self.report({'WARNING'}, f"骨骼合并未完整生成：{message}")
        except Exception as e:
            import traceback
            print(f"[EFMI骨骼合并] 预生成失败（不阻断导入）: {e}")
            traceback.print_exc()
            self.report({'WARNING'}, f"EFMI 骨骼合并预生成异常，已回退普通导入：{e}")

        # 分组投影导入过滤（契约 C1-C5）：EFMI 多 LOD 投影模式下，非基准 LOD 目标
        # 只放行“明确匹配成功”者——GPU 有 VGMap / CPU/无顶点组有
        # EFMILODProjectionMatched；明确的投影未匹配（EFMILODProjectionSkipped）
        # 与缺状态（旧缓存/半成品/裁决缺失）一律剔除（fail-closed，C4）。
        # 过滤不依赖合并预生成**成功**（merged_vgmap_ready is True）：裁决标记
        # 由 ensure_skeleton_data 在内部任何失败点之前写入，因此合并预生成失败、
        # 回退普通导入时同样必须过滤，绝不让 CPU unknown（68 个实况）借“合并
        # 失败”路径绕过 C4 重新漏过。仅当本次导入实际执行过合并预生成
        #（merged_vgmap_ready 为 True 或 False）时生效；复选框关闭（None，未
        # 执行预生成、无裁决标记可依赖）时保留旧普通导入语义（C5 单 LOD/投影
        # 关闭/未开启合并行为不变）。
        if (
            merged_vgmap_ready is not None
            and GlobalConfig.logic_name == LogicName.EFMI
            and GlobalProterties.efmi_lod_group_projection()
        ):
            try:
                from ..common.efmi_skeleton import EFMISkeletonMergeHelper as _EFMISkeletonHelper
            except Exception as e:
                print(f"[EFMI骨骼合并] 导入投影过滤模块加载失败（不阻断导入）: {e}")
                _EFMISkeletonHelper = None

            decisions: dict[str, str] | None = None
            if _EFMISkeletonHelper is not None:
                try:
                    decisions = _EFMISkeletonHelper.classify_projection_import_targets(
                        GlobalConfig.path_workspace_folder(),
                        [target["import_key"] for target in import_targets],
                    )
                except Exception as e:
                    # classify 异常不得跳过过滤：与 decisions is None 相同，
                    # 降级到 fail-closed 兜底（非基准 LOD 目标默认排除）。
                    print(f"[EFMI骨骼合并] 投影裁决分类失败，降级 fail-closed 兜底: {e}")
            if decisions is None:
                decisions = _projection_fail_closed_decisions(
                    [target["import_key"] for target in import_targets]
                )
            dropped_keys = [
                key for key, decision in decisions.items()
                if decision != "import"
            ]
            if dropped_keys:
                before = len(import_targets)
                import_targets = [
                    target for target in import_targets
                    if decisions.get(target["import_key"], "fail_closed") == "import"
                ]
                dropped = before - len(import_targets)
                fail_closed_keys = [
                    key for key, decision in decisions.items()
                    if decision == "fail_closed"
                ]
                shown = "、".join(sorted(dropped_keys)[:5])
                suffix = "…" if len(dropped_keys) > 5 else ""
                detail = f"{shown}{suffix}"
                print(
                    f"[EFMI骨骼合并] 跨 LOD 投影未匹配/缺状态，跳过导入 "
                    f"{dropped} 个物体: {detail}"
                )
                self.report(
                    {'INFO'},
                    f"跨 LOD 投影未匹配/缺状态，跳过导入 {dropped} 个物体: {detail}",
                )
                if fail_closed_keys:
                    fc_shown = "、".join(sorted(fail_closed_keys)[:3])
                    fc_suffix = "…" if len(fail_closed_keys) > 3 else ""
                    print(
                        f"[EFMI骨骼合并] 其中 {len(fail_closed_keys)} 个缺少明确"
                        f"投影匹配状态（旧缓存/半成品）: {fc_shown}{fc_suffix}"
                    )
                    self.report(
                        {'WARNING'},
                        f"{len(fail_closed_keys)} 个目标缺少投影匹配状态，"
                        f"已按 fail-closed 排除: {fc_shown}{fc_suffix}",
                    )

    # EFMI 多 pass 布局写回（2026-09-15）：pass 槽位镜像所需的各部件 pass 布局
    # （NumViews/PS 哈希/贴图槽位，推导机制见 ui/universal/efmi_pass_mirror.py）
    # 在**导入时**一次算好存进工作空间 Config/PassLayouts.json——下次导出不再依赖
    # 提取文件还在；来源日志更新时读侧自动淘汰、回退实时扫描（生成侧
    # efmi.py::_efmi_pass_layouts 先读此缓存）。按 IB 增量合并：本批导入只更新
    # 自己这些部件，旧批次部件不丢。任何失败都不阻断导入（生成侧有兜底）。
    if GlobalConfig.logic_name == LogicName.EFMI:
        try:
            from ..common.efmi_skeleton import EFMISkeletonMergeHelper as _EFMIPassHelper
            from .universal.efmi_pass_mirror import (
                efmi_merge_pass_layouts,
                efmi_read_pass_layouts,
                efmi_scan_pass_layouts,
                efmi_write_pass_layouts,
            )
            _pm_workspace = str(GlobalConfig.path_workspace_folder() or "").strip()
            _pm_lod_map, _pm_default_dir = _EFMIPassHelper.resolve_frame_analysis_dirs_by_lod(
                _pm_workspace
            )
            _pm_log_paths = []
            for _pm_dir in list(_pm_lod_map.values()) + ([_pm_default_dir] if _pm_default_dir else []):
                _pm_log = os.path.join(str(_pm_dir), "log.txt")
                if os.path.isfile(_pm_log) and _pm_log not in _pm_log_paths:
                    _pm_log_paths.append(_pm_log)
            # import_key 形如 LOD0.d6128f13-14664-0 → IB = 去 LOD 前缀后第一段
            _pm_part_ibs = {
                str(target.get("import_key", "") or "").split(".", 1)[-1].split("-")[0].strip().lower()
                for target in import_targets
            } - {""}
            if _pm_log_paths and _pm_part_ibs and _pm_workspace:
                _pm_new = efmi_scan_pass_layouts(_pm_log_paths, _pm_part_ibs)
                if _pm_new:
                    _pm_old = efmi_read_pass_layouts(_pm_workspace)
                    _pm_written = efmi_write_pass_layouts(
                        _pm_workspace,
                        efmi_merge_pass_layouts(_pm_old, _pm_new),
                        _pm_log_paths,
                    )
                    if _pm_written:
                        print(
                            f"[EFMI多pass镜像] pass 布局已写回工作空间: {_pm_written}"
                            f"（更新 {len(_pm_new)} 个部件）"
                        )
                    else:
                        print("[EFMI多pass镜像] pass 布局写回失败（不阻断导入）")
        except Exception as _pm_exc:
            print(f"[EFMI多pass镜像] pass 布局写回异常（不阻断导入）: {_pm_exc}")

    # ZZMI 骨骼合并数据预生成（与 EFMI 同构的分支选项）：
    # 复选框（import_merged_vgmap，「使用融合统一顶点组」）关闭时完全不执行，保持旧逻辑；
    # 开启时把 FrameAnalysis 反查的 VGMap/VGOffset/VGCount 写回工作空间 json，
    # 导入流程经 create_mesh_from_json 的既有双条件路径自动走全局骨骼索引。
    is_zzmi_merged = (
        GlobalConfig.logic_name == LogicName.ZZMI
        and GlobalProterties.import_merged_vgmap()
    )
    if is_zzmi_merged:
        merged_vgmap_ready = False
        try:
            from ..common.zzmi_skeleton import ZZMISkeletonMergeHelper
            import_keys = [target["import_key"] for target in import_targets]
            ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=GlobalConfig.path_workspace_folder(),
                unique_str_list=import_keys,
            )
            print(f"[ZZMI骨骼合并] {message}")
            merged_vgmap_ready = bool(ok)
            if ok:
                self.report({'INFO'}, message)
            else:
                print(f"[ZZMI骨骼合并] 未生成骨骼数据：{message}")
                # 部分/全部子网格未生成合并骨架数据时会走普通导入路线，
                # 必须显式警告，避免用户误以为骨骼合并已完整生效。
                self.report({'WARNING'}, f"骨骼合并未完整生成：{message}")
        except Exception as e:
            import traceback
            print(f"[ZZMI骨骼合并] 预生成失败（不阻断导入）: {e}")
            traceback.print_exc()
            self.report({'WARNING'}, f"ZZMI 骨骼合并预生成异常，已回退普通导入：{e}")

    if merged_vgmap_ready is False:
        # 失败后若继续保留复选框，当前对象虽按普通组导入，后续导出器却仍会
        # 按合并骨架生成运行时段，形成导入/导出模式分裂。显式关闭选项，确保
        # 本次普通导入与随后导出保持同一契约；用户修好来源后可再次手动开启。
        GlobalProterties.set_import_merged_vgmap(False)
        self.report(
            {'WARNING'},
            "本次已整体回退普通顶点组，并关闭“使用融合统一顶点组”；"
            "修复骨骼来源后可重新开启并再次导入",
        )

    foldername_gametypename_dict = {}
    imported_objects = []
    import_records = []
    # 一次完整导入独占：同一批 Component 共享对应骨架组合集；下一次导入不得
    # 复用旧批次留下的同名合集，而应由 Blender 风格命名生成 .001/.002。
    zzmi_skeleton_group_collections = {}

    for target in import_targets:
        submesh_folder_name = target["submesh_folder_name"]
        print("Import FolderName: " + target["import_key"])

        final_import_folder_path_list = WorkSpaceHelper.get_ordered_gpu_cpu_import_folderpath_list(
            target["submesh_folder_path"],
        )
        print("Final Import Folder Path List: " + str(final_import_folder_path_list))

        for import_folder_path in final_import_folder_path_list:
            gametype_name = _extract_gametype_name(import_folder_path)
            if not gametype_name:
                self.report({'WARNING'}, f"跳过无法识别游戏类型的导入目录：{import_folder_path}")
                continue

            try:
                print("尝试导入路径: " + import_folder_path)
                json_file_path = os.path.join(import_folder_path, submesh_folder_name + ".json")
                imported_obj = SSMTImportHelper.create_mesh_from_json(
                    json_file_path=json_file_path,
                    import_collection=target["import_collection"],
                    use_merged_vgmap=merged_vgmap_ready,
                )
                if imported_obj is None:
                    continue

                display_name = target["display_name"]
                workspace_unique_str = str(imported_obj.get("3DMigoto:WorkspaceUniqueStr", "") or "").strip()
                if workspace_unique_str:
                    prefix_info = ObjectPrefixHelper.extract_prefix_info(display_name)
                    if prefix_info:
                        display_name = ObjectPrefixHelper.replace_prefix(
                            display_name,
                            workspace_unique_str,
                            ".",
                            prefix_info[0],
                            prefix_info[1],
                        )
                    else:
                        display_name = workspace_unique_str

                imported_obj.name = display_name
                imported_obj.data.name = imported_obj.name
                if is_zzmi_merged and merged_vgmap_ready is True:
                    # 分组版骨骼合并：导入对象按 json SkeletonGroup 归入对应骨架组合集，
                    # 让"同一对象空间的部件"在大纲视图里聚在一起（跨组不共享骨架）。
                    try:
                        _zzmi_move_to_skeleton_group_collection(
                            imported_obj,
                            target["import_key"],
                            target["import_collection"],
                            collection_cache=zzmi_skeleton_group_collections,
                        )
                    except Exception as e:
                        print(f"[ZZMI骨骼合并] 分组合集归组失败（不影响导入）: {e}")
                imported_objects.append(imported_obj)
                foldername_gametypename_dict[target["import_key"]] = gametype_name
                import_records.append(
                    {
                        **target,
                        "imported_obj": imported_obj,
                        "gametype_name": gametype_name,
                    }
                )
                self.report({'INFO'}, "成功导入 " + target["import_key"] + " 的数据类型: " + gametype_name)
            except Exception as e:
                print(f"导入目录失败：{import_folder_path}，错误：{e}")
                continue
            break

    if not import_records:
        self.report({'ERROR'}, "当前工作空间没有成功导入任何模型，已跳过蓝图生成。")
        return False

    save_import_json_path = os.path.join(GlobalConfig.path_workspace_folder(), "Import.json")
    JsonUtils.SaveToFile(json_dict=foldername_gametypename_dict, filepath=save_import_json_path)

    if GlobalProterties.enable_non_mirror_workflow():
        NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

    CollectionUtils.select_collection_objects(workspace_collection)
    PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

    try:
        tree_name = GlobalConfig.get_workspace_name()

        try:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
        except Exception as e:
            print(f"创建蓝图节点树失败：{e}。请检查 SSMTBlueprintTreeType 是否已正确注册。")
            self.report({'ERROR'}, "创建蓝图失败，请确认 SSMT 蓝图节点类型已正确注册。")
            return False
        tree.use_fake_user = True

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()
        original_model_frame = _create_original_model_frame(tree)

        tab_group_nodes = {}
        tab_node_lists = {}
        default_group_node = tree.nodes.new('SSMTNode_Object_Group')
        default_group_node.label = "默认分组"
        default_group_node.parent = original_model_frame
        default_node_list = []

        y_gap = 200
        tab_gap = 400

        for record in import_records:
            obj = record["imported_obj"]
            if obj.type != 'MESH':
                continue

            draw_ib = record["draw_ib"]
            tab_name = drawib_tabname_dict.get(draw_ib)

            if tab_name and tab_name not in tab_group_nodes:
                group_node = tree.nodes.new('SSMTNode_Object_Group')
                group_node.label = tab_name
                group_node.parent = original_model_frame
                tab_group_nodes[tab_name] = group_node
                tab_node_lists[tab_name] = []

            target_group = tab_group_nodes.get(tab_name, default_group_node) if tab_name else default_group_node

            node = tree.nodes.new('SSMTNode_Object_Info')
            node.object_name = obj.name
            node.object_id = str(obj.as_pointer())
            if hasattr(node, "original_object_name"):
                node.original_object_name = obj.name
            node.draw_ib = draw_ib
            node.component = record["component"] or "1"
            node.alias_name = record["alias_name"]
            node.label = obj.name
            node.parent = original_model_frame

            if target_group.inputs[-1].is_linked:
                target_group.inputs.new('SSMTSocketObject', f"Input {len(target_group.inputs) + 1}")

            tree.links.new(node.outputs[0], target_group.inputs[-1])

            if tab_name:
                tab_node_lists[tab_name].append(node)
            else:
                default_node_list.append(node)

        all_group_nodes = []
        tab_order = list(tab_group_nodes.keys())

        current_y = 0
        for tab_name in tab_order:
            nodes = tab_node_lists.get(tab_name, [])
            for node in nodes:
                node.location = (0, current_y)
                current_y -= y_gap
            current_y -= tab_gap - y_gap

        for node in default_node_list:
            node.location = (0, current_y)
            current_y -= y_gap

        has_default_links = any(inp.is_linked for inp in default_group_node.inputs)
        if has_default_links:
            all_group_nodes.append(default_group_node)
        elif not all_group_nodes:
            all_group_nodes.append(default_group_node)
        else:
            tree.nodes.remove(default_group_node)

        for tab_name in tab_order:
            all_group_nodes.append(tab_group_nodes[tab_name])

        group_x = 400
        group_current_y = 0

        for grp_node in all_group_nodes:
            grp_node.location = (group_x, group_current_y)
            group_current_y -= 300

        output_node = tree.nodes.new('SSMTNode_Result_Output')
        output_node.location = (0, 0)
        output_node.label = "生成 Mod"
        # EFMI 自动输出默认使用 RabbitFX/FX 风格。
        _configure_efmi_auto_output_node(output_node, GlobalConfig.logic_name)

        # EFMI 跨 LOD 自动处理链：账本只提供 LOD0/目标 LOD 的物体配对；
        # 顶点组映射必须在导入完成后由匹配节点读取两边实际 Blender 物体并重算。
        # 自动给相关组挂上重命名物体节点 + 顶点组处理节点：
        #   - 重命名节点（LOD0 端组）：该组全部匹配对的 LOD0 物体名 -> LOD1 对应名，
        #     插在物体组与顶点组处理节点中间（与匹配节点 LOD0→LOD1 方向一致）；
        #   - 顶点组处理节点（LOD1/目标端组）：挂该组所有部件的顶点组匹配节点，
        #     每个节点都调用 execute_match，从实际源/目标物体生成自己的映射文本。
        # 结构：物体 >>> 物体组 >>> [重命名>>顶点组处理(匹配节点)] >>> 输出。
        # 仅在 EFMI 合并路线完整成功时自动生成；无物体配对/回退普通导入时不动蓝图。
        vg_process_by_group: dict[int, object] = {}
        rename_by_group: dict[int, object] = {}
        vg_match_created = 0
        vg_match_failed = 0
        match_nodes_by_group: dict[int, list[object]] = {}
        created_auto_nodes = []
        existing_text_names = set(
            getattr(getattr(bpy, "data", None), "texts", {}).keys()
        )
        if (
            merged_vgmap_ready is True
            and GlobalConfig.logic_name == LogicName.EFMI
            and GlobalProterties.efmi_lod_group_projection()
        ):
            try:
                from ..common.efmi_skeleton import EFMISkeletonMergeHelper as _EFMISkeletonHelper
                match_pairs = _EFMISkeletonHelper.load_lod_match_pairs(
                    GlobalConfig.path_workspace_folder(),
                    [target["import_key"] for target in import_targets],
                )
                record_by_key = {
                    record["import_key"]: record for record in import_records
                }
                drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()

                def _group_node_for_key(import_key: str):
                    record = record_by_key.get(import_key)
                    if record is None:
                        return None
                    tab_name = drawib_tabname_dict.get(record["draw_ib"])
                    group_node = tab_group_nodes.get(tab_name) if tab_name else None
                    return group_node or default_group_node

                for pair in match_pairs:
                    group_node = _group_node_for_key(pair["target_key"])
                    if group_node is None:
                        continue
                    source_record = record_by_key.get(pair["reference_key"])
                    target_record = record_by_key.get(pair["target_key"])
                    if source_record is None or target_record is None:
                        print(
                            f"[EFMI自动链] 跳过配对 {pair['target_key']} -> "
                            f"{pair['reference_key']}: 未找到导入物体"
                        )
                        continue
                    source_obj = source_record["imported_obj"]
                    target_obj = target_record["imported_obj"]

                    # 重命名物体节点挂在 LOD0（源/基准）端所在组；
                    # LOD0 物体名 -> 对应 LOD1 物体名（与匹配节点方向一致）
                    rename_group = _group_node_for_key(pair["reference_key"]) or group_node
                    rename_node = rename_by_group.get(id(rename_group))
                    if rename_node is None:
                        rename_node = tree.nodes.new('SSMTNode_Object_Rename')
                        created_auto_nodes.append(rename_node)
                        rename_node.label = "LOD前缀重命名"
                        # 自动链保持普通执行顺序：先按规则重命名，再进入顶点组处理。
                        _configure_efmi_auto_rename_node(rename_node)
                        rename_node.location = (0, 0)
                        rename_by_group[id(rename_group)] = rename_node
                    rule = rename_node.rename_rules.add()
                    rule.name = f"Rule_{len(rename_node.rename_rules):03d}"
                    # 与右键「快速添加重命名规则」（node_menu.py
                    # SSMT_OT_QuickAddRenameRule）同口径：规则只写结构化前缀，
                    # 剔除点后的自定义后缀/Blender .001 冲突后缀，避免规则与
                    # 导入瞬时全名强耦合、后缀被连坐替换；提取失败时回退全名保底。
                    _src_prefix_info = ObjectPrefixHelper.extract_prefix_info(source_obj.name)
                    _tgt_prefix_info = ObjectPrefixHelper.extract_prefix_info(target_obj.name)
                    rule.search_str = (_src_prefix_info[0] if _src_prefix_info else "") or source_obj.name
                    rule.replace_str = (_tgt_prefix_info[0] if _tgt_prefix_info else "") or target_obj.name

                    # 顶点组处理节点挂在 LOD1（目标）端所在组
                    vg_proc = vg_process_by_group.get(id(group_node))
                    if vg_proc is None:
                        vg_proc = tree.nodes.new('SSMTNode_VertexGroupProcess')
                        created_auto_nodes.append(vg_proc)
                        vg_proc.location = (0, 0)
                        vg_process_by_group[id(group_node)] = vg_proc
                    pair_index = sum(
                        1 for _p in match_pairs
                        if _group_node_for_key(_p["target_key"]) is group_node
                        and _p["target_key"] <= pair["target_key"]
                    ) - 1

                    match_node = tree.nodes.new('SSMTNode_VertexGroupMatch')
                    created_auto_nodes.append(match_node)
                    match_node.label = f"LOD匹配: {target_obj.name}"
                    match_node.location = (0, 0)
                    match_node.width = 300.0
                    match_nodes_by_group.setdefault(id(group_node), []).append(match_node)
                    if vg_proc.inputs[-1].is_linked:
                        vg_proc.inputs.new(
                            'SSMTSocketObject', f"映射表 {len(vg_proc.inputs)}"
                        )
                    tree.links.new(match_node.outputs[0], vg_proc.inputs[-1])

                    rename_map, match_message = _configure_and_execute_efmi_lod_match(
                        match_node,
                        source_obj,
                        target_obj,
                        context,
                    )
                    if not rename_map:
                        vg_match_failed += 1
                        print(
                            f"[EFMI自动匹配] {source_obj.name} -> {target_obj.name} "
                            f"执行失败: {match_message}"
                        )
                    else:
                        vg_match_created += 1
                        print(
                            f"[EFMI自动匹配] {source_obj.name} -> {target_obj.name}: "
                            f"{match_message}"
                        )
            except Exception as e:
                print(f"[EFMI自动链] 自动创建处理链失败（不影响导入）: {e}")
                # 本阶段是一个事务：任何异常都撤掉本轮新增节点、自动生成的映射
                # 文本及其链接，不能把半条链留在蓝图里影响后续手工导出。
                for node in reversed(created_auto_nodes):
                    try:
                        tree.nodes.remove(node)
                    except (ReferenceError, RuntimeError, ValueError):
                        pass
                texts = getattr(getattr(bpy, "data", None), "texts", None)
                if texts is not None:
                    for text_name in set(texts.keys()) - existing_text_names:
                        try:
                            text = texts.get(text_name)
                            if text is not None:
                                texts.remove(text)
                        except (ReferenceError, RuntimeError, ValueError):
                            pass
                created_auto_nodes.clear()
                vg_process_by_group.clear()
                rename_by_group.clear()
                vg_match_created = 0
                vg_match_failed = 0
        # 所有自动节点创建完成后统一按批准模板布局，严格按绝对坐标、模板宽度
        # 和实际高度计算，确保输出始终位于顶点组处理右侧。
        for group_node in all_group_nodes:
            rename_node = rename_by_group.get(id(group_node))
            vg_proc = vg_process_by_group.get(id(group_node))
            positions = _layout_efmi_auto_main_chain(
                group_node, rename_node, vg_proc, output_node
            )
            if rename_node is not None:
                rename_node.location = positions["rename"]
            if vg_proc is not None:
                vg_proc.location = positions["process"]
            if output_node is not None:
                output_node.location = positions["output"]
            match_nodes = match_nodes_by_group.get(id(group_node), [])
            for match_node, position in zip(
                match_nodes,
                _layout_efmi_match_nodes(
                    group_node,
                    match_nodes,
                    max_per_row=6,
                    vertical_gap=700.0,
                    offset_x=500.0,
                    offset_y=1200.0,
                ),
            ):
                match_node.location = position

        if vg_match_created or vg_match_failed:
            summary = f"已自动创建 {vg_match_created} 个顶点组匹配节点"
            if vg_match_failed:
                summary += f"，{vg_match_failed} 个执行失败（可手动补执行）"
            print(f"[EFMI自动匹配] {summary}")
            self.report({'INFO'}, summary)
        if rename_by_group:
            total_rules = sum(len(node.rename_rules) for node in rename_by_group.values())
            print(f"[EFMI自动链] 已自动创建 {len(rename_by_group)} 个重命名物体节点（共 {total_rules} 条 LOD0→LOD1 前缀规则）")
            self.report({'INFO'}, f"已自动创建 {len(rename_by_group)} 个重命名物体节点（共 {total_rules} 条规则）")

        # 每组链路尾节点：组输出 -> [重命名(延迟执行)] -> [顶点组处理] -> 输出/合并。
        # 节点拓扑保留“先重命名”便于表达 LOD0→LOD1 输出意图，执行器依据
        # defer_until_after_vertex_group_process 保证实际顺序为“先 VG、后改名”。
        chain_tail_by_group: dict[int, object] = {}
        for grp_node in all_group_nodes:
            tail = grp_node
            rename_node = rename_by_group.get(id(grp_node))
            vg_proc = vg_process_by_group.get(id(grp_node))
            if rename_node is not None:
                tree.links.new(tail.outputs[0], rename_node.inputs[0])
                tail = rename_node
            if vg_proc is not None:
                tree.links.new(tail.outputs[0], vg_proc.inputs[0])
                tail = vg_proc
            chain_tail_by_group[id(grp_node)] = tail

        if len(output_node.inputs) > 0 and len(all_group_nodes) > 0:
            if len(all_group_nodes) == 1:
                grp_node = all_group_nodes[0]
                dest = chain_tail_by_group.get(id(grp_node)) or grp_node
                tree.links.new(dest.outputs[0], output_node.inputs[0])
            else:
                merge_node = tree.nodes.new('SSMTNode_Object_Group')
                merge_node.label = "合并"
                merge_node.location = (600, 0)
                merge_node.parent = original_model_frame

                for grp_node in all_group_nodes:
                    if merge_node.inputs[-1].is_linked:
                        merge_node.inputs.new('SSMTSocketObject', f"Input {len(merge_node.inputs) + 1}")
                    dest = chain_tail_by_group.get(id(grp_node)) or grp_node
                    tree.links.new(dest.outputs[0], merge_node.inputs[-1])

                tree.links.new(merge_node.outputs[0], output_node.inputs[0])

        for grp_node in all_group_nodes:
            if hasattr(grp_node, "update"):
                grp_node.update()
        if hasattr(original_model_frame, "update"):
            original_model_frame.update()

        print(f"蓝图 {tree_name} 已按工作空间标签完成导入对象分组更新。")
        return True

    except Exception as e:
        print(f"生成蓝图节点时发生错误：{e}")
        import traceback
        traceback.print_exc()
        self.report({'ERROR'}, f"生成导入蓝图失败：{e}")
        return False


def _import_workspace_full_ntemi(self, context, workspace_collection):
    """NTEMI 工作空间的完整导入流程，含骨骼合并后处理"""
    workspace_root = GlobalConfig.path_workspace_folder()
    drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict()
    draw_calls = _discover_draw_calls(workspace_root, drawib_aliasname_dict)
    if not draw_calls:
        self.report({'ERROR'}, "NTEMI工作空间未找到可导入的DrawCall数据。")
        return False

    default_frame_analysis_dir = _resolve_frame_analysis_dir(workspace_root)
    frame_analysis_dir_map = _load_frame_analysis_dir_map(workspace_root)
    lod_collection_map = {}
    deduped_texture_dir_map = {}
    component_map_by_lod = {}

    foldername_gametypename_dict = {}
    imported_objects = []
    import_records = []

    for draw_call in draw_calls:
        lod_name = str(draw_call.lod_name or "LOD0").strip() or "LOD0"
        lod_collection = lod_collection_map.get(lod_name)
        if lod_collection is None:
            lod_collection = CollectionUtils.create_new_collection(
                collection_name=lod_name,
                color_tag=CollectionColor.Blue,
            )
            workspace_collection.children.link(lod_collection)
            lod_collection_map[lod_name] = lod_collection

        if lod_name not in deduped_texture_dir_map:
            deduped_texture_dir_map[lod_name] = _resolve_deduped_texture_dir(workspace_root, lod_name)
        if lod_name not in component_map_by_lod:
            component_map_by_lod[lod_name] = _load_component_name_map(os.path.join(workspace_root, lod_name))

        frame_analysis_dir = frame_analysis_dir_map.get(
            str(draw_call.draw_ib or "").strip().lower(),
            default_frame_analysis_dir,
        )
        json_file_path = os.path.join(
            draw_call.folder_path, f"{draw_call.submesh_folder_name}.json"
        )

        workspace_unique_str = f"{lod_name}.{draw_call.submesh_folder_name}"

        try:
            imported_obj = NTEMIImportHelper.create_mesh_with_modimp_props(
                json_file_path=json_file_path,
                draw_call_meta=draw_call,
                import_collection=lod_collection,
                deduped_texture_dir=deduped_texture_dir_map.get(lod_name, ""),
                component_map=component_map_by_lod.get(lod_name, {}),
                workspace_unique_str=workspace_unique_str,
                frame_analysis_dir=frame_analysis_dir,
            )
            if imported_obj is None:
                continue

            display_name = workspace_unique_str
            if draw_call.alias_name:
                prefix_info = ObjectPrefixHelper.extract_prefix_info(draw_call.display_name)
                if prefix_info:
                    display_name = ObjectPrefixHelper.replace_prefix(
                        draw_call.display_name,
                        workspace_unique_str,
                        ".",
                        prefix_info[0],
                        prefix_info[1],
                    )
                else:
                    display_name = workspace_unique_str

            imported_obj.name = display_name
            imported_obj.data.name = display_name
            imported_objects.append(imported_obj)

            gametype_name = "GPU_P12_BI8_BW8_T8_T1-8_TA4_N4_"
            foldername_gametypename_dict[workspace_unique_str] = gametype_name
            import_records.append(
                {
                    "import_key": workspace_unique_str,
                    "submesh_folder_name": draw_call.submesh_folder_name,
                    "submesh_folder_path": draw_call.folder_path,
                    "display_name": display_name,
                    "alias_name": draw_call.alias_name,
                    "draw_ib": draw_call.draw_ib,
                    "component": draw_call.component,
                    "lod_name": lod_name,
                    "frame_analysis_dir": frame_analysis_dir,
                    "import_collection": lod_collection,
                    "imported_obj": imported_obj,
                    "gametype_name": gametype_name,
                }
            )
            self.report(
                {'INFO'},
                f"NTEMI 成功导入 {workspace_unique_str}"
            )
        except Exception as e:
            print(f"导入 NTEMI DrawCall 失败：{workspace_unique_str}，错误：{e}")
            import traceback
            traceback.print_exc()
            continue

    if not import_records:
        self.report({'ERROR'}, "NTEMI工作空间没有成功导入任何模型，已跳过蓝图生成。")
        return False

    save_import_json_path = os.path.join(workspace_root, "Import.json")
    JsonUtils.SaveToFile(json_dict=foldername_gametypename_dict, filepath=save_import_json_path)

    if GlobalProterties.enable_non_mirror_workflow():
        NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

    CollectionUtils.select_collection_objects(workspace_collection)
    PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

    imported_by_source: dict[tuple[str, str], list] = {}
    for record in import_records:
        imported_obj = record.get("imported_obj")
        draw_ib = str(record.get("draw_ib", "") or "").strip()
        frame_analysis_dir = str(record.get("frame_analysis_dir", "") or "").strip()
        if imported_obj is None or not draw_ib or not frame_analysis_dir:
            continue
        imported_by_source.setdefault((frame_analysis_dir, draw_ib), []).append(imported_obj)

    for (frame_analysis_dir, draw_ib), draw_ib_objects in imported_by_source.items():
        try:
            _perform_bone_merge_postprocess(
                objects=draw_ib_objects,
                frame_analysis_dir=frame_analysis_dir,
                draw_ib=draw_ib,
                workspace_root=workspace_root,
            )
        except Exception as e:
            print(f"NTEMI骨骼合并后处理失败：DrawIB={draw_ib}，FrameAnalysis={frame_analysis_dir}，错误：{e}")
            import traceback
            traceback.print_exc()
            self.report(
                {'WARNING'},
                f"已跳过 DrawIB {draw_ib} 的骨骼合并后处理：{os.path.basename(frame_analysis_dir)}"
            )

    try:
        tree_name = GlobalConfig.get_workspace_name()

        try:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
        except Exception as e:
            print(f"创建 NTEMI 蓝图节点树失败：{e}。请检查 SSMTBlueprintTreeType 是否已正确注册。")
            self.report({'ERROR'}, "创建 NTEMI 蓝图失败，请确认 SSMT 蓝图节点类型已正确注册。")
            return False
        tree.use_fake_user = True

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()
        original_model_frame = _create_original_model_frame(tree)

        tab_group_nodes = {}
        tab_node_lists = {}
        default_group_node = tree.nodes.new('SSMTNode_Object_Group')
        default_group_node.label = "默认分组"
        default_group_node.parent = original_model_frame
        default_node_list = []

        y_gap = 200
        tab_gap = 400

        for record in import_records:
            obj = record["imported_obj"]
            if obj.type != 'MESH':
                continue

            draw_ib = record["draw_ib"]
            tab_name = drawib_tabname_dict.get(draw_ib)

            if tab_name and tab_name not in tab_group_nodes:
                group_node = tree.nodes.new('SSMTNode_Object_Group')
                group_node.label = tab_name
                group_node.parent = original_model_frame
                tab_group_nodes[tab_name] = group_node
                tab_node_lists[tab_name] = []

            target_group = tab_group_nodes.get(tab_name, default_group_node) if tab_name else default_group_node

            node = tree.nodes.new('SSMTNode_Object_Info')
            node.object_name = obj.name
            node.object_id = str(obj.as_pointer())
            if hasattr(node, "original_object_name"):
                node.original_object_name = obj.name
            node.draw_ib = draw_ib
            node.component = record["component"] or "1"
            node.alias_name = record["alias_name"]
            node.label = obj.name
            node.parent = original_model_frame

            if target_group.inputs[-1].is_linked:
                target_group.inputs.new('SSMTSocketObject', f"Input {len(target_group.inputs) + 1}")

            tree.links.new(node.outputs[0], target_group.inputs[-1])

            if tab_name:
                tab_node_lists[tab_name].append(node)
            else:
                default_node_list.append(node)

        all_group_nodes = []
        tab_order = list(tab_group_nodes.keys())

        current_y = 0
        for tab_name in tab_order:
            nodes = tab_node_lists.get(tab_name, [])
            for node in nodes:
                node.location = (0, current_y)
                current_y -= y_gap
            current_y -= tab_gap - y_gap

        for node in default_node_list:
            node.location = (0, current_y)
            current_y -= y_gap

        has_default_links = any(inp.is_linked for inp in default_group_node.inputs)
        if has_default_links:
            all_group_nodes.append(default_group_node)
        elif not all_group_nodes:
            all_group_nodes.append(default_group_node)
        else:
            tree.nodes.remove(default_group_node)

        for tab_name in tab_order:
            all_group_nodes.append(tab_group_nodes[tab_name])

        group_x = 400
        group_current_y = 0

        for grp_node in all_group_nodes:
            grp_node.location = (group_x, group_current_y)
            group_current_y -= 300

        output_node = tree.nodes.new('SSMTNode_Result_Output_NTMIModImp')
        output_node.location = (800, 0)
        output_node.label = "NTMI ModImp 输出"

        if len(output_node.inputs) > 0 and len(all_group_nodes) > 0:
            if len(all_group_nodes) == 1:
                tree.links.new(all_group_nodes[0].outputs[0], output_node.inputs[0])
            else:
                merge_node = tree.nodes.new('SSMTNode_Object_Group')
                merge_node.label = "合并"
                merge_node.location = (600, 0)
                merge_node.parent = original_model_frame

                for grp_node in all_group_nodes:
                    if merge_node.inputs[-1].is_linked:
                        merge_node.inputs.new('SSMTSocketObject', f"Input {len(merge_node.inputs) + 1}")
                    tree.links.new(grp_node.outputs[0], merge_node.inputs[-1])

                tree.links.new(merge_node.outputs[0], output_node.inputs[0])

        for grp_node in all_group_nodes:
            if hasattr(grp_node, "update"):
                grp_node.update()
        if hasattr(original_model_frame, "update"):
            original_model_frame.update()

        print(f"已创建 NTEMI 蓝图 {tree_name}，并挂载 NTMI ModImp 输出节点。")
        return True

    except Exception as e:
        print(f"生成 NTEMI 蓝图节点时发生错误：{e}")
        import traceback
        traceback.print_exc()
        self.report({'ERROR'}, f"生成 NTEMI 导入蓝图失败：{e}")
        return False


class SSMT4ImportAllFromCurrentWorkSpaceBlueprint(bpy.types.Operator):
    """一键导入当前 SSMT 工作空间下所有内容的 Blender 算子"""
    bl_idname = "ssmt4.import_all_from_workspace"
    bl_label = TR.translate("一键导入SSMT工作空间内容")
    bl_description = "一键导入当前工作空间文件夹下所有的内容"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        success = False
        if GlobalConfig.get_workspace_name() == "":
            self.report({"ERROR"}, "请先在 SSMT 中选择工作空间后再导入。")
        elif not os.path.exists(GlobalConfig.path_workspace_folder()):
            self.report(
                {"ERROR"},
                "工作空间目录不存在，请先在 SSMT 中创建工作空间后再导入："
                + GlobalConfig.path_workspace_folder(),
            )
        else:
            TimerUtils.Start("ImportFromWorkSpaceBlueprint")
            success = bool(ImprotFromWorkSpaceFull(self, context))
            TimerUtils.End("ImportFromWorkSpaceBlueprint")

        if success:
            return {'FINISHED'}
        return {'CANCELLED'}


class SSMT4ImportRaw(bpy.types.Operator, ImportHelper):
    """导入 SSMT 格式模型文件的 Blender 算子，支持批量选择 JSON 文件"""
    bl_idname = "ssmt4.import_raw"
    bl_label = TR.translate("导入SSMT格式模型")
    bl_description = "导入SSMT格式的模型文件，只需选择.json文件即可"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: bpy.props.StringProperty(
        default='*.json',
        options={'HIDDEN'},
    )  # type: ignore

    files: bpy.props.CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
    )  # type: ignore

    def execute(self, context):
        """执行导入操作，遍历选中的 JSON 文件并创建网格对象"""
        dirname = os.path.dirname(self.filepath)

        collection_name = os.path.basename(dirname)
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
        imported_objects = []

        import_filename_list = []
        if len(self.files) == 1:
            if str(self.filepath).endswith(".json"):
                import_filename_list.append(self.filepath)
            else:
                for filename in os.listdir(self.filepath):
                    if filename.endswith(".json"):
                        import_filename_list.append(filename)
        else:
            for json_file in self.files:
                import_filename_list.append(json_file.name)

        for json_file_name in import_filename_list:
            if os.path.isabs(json_file_name):
                json_file_path = json_file_name
            else:
                json_file_path = os.path.join(dirname, json_file_name)
            imported_obj = SSMTImportHelper.create_mesh_from_json(
                json_file_path=json_file_path,
                import_collection=collection,
            )
            if imported_obj is not None:
                imported_objects.append(imported_obj)

        if GlobalProterties.enable_non_mirror_workflow():
            NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

        CollectionUtils.select_collection_objects(collection)
        PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

        if imported_objects:
            return {'FINISHED'}
        self.report({'ERROR'}, "没有成功导入任何模型，请检查所选 JSON 或目录是否有效。")
        return {'CANCELLED'}


def register():
    bpy.utils.register_class(SSMT4ImportRaw)
    bpy.utils.register_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMT4ImportRaw)
    bpy.utils.unregister_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)
