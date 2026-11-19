import os
import shutil
import hashlib
import json
from pathlib import Path

import bpy

from ..common.global_config import GlobalConfig
from ..common.global_key_count_helper import GlobalKeyCountHelper
from ..common.global_properties import GlobalProterties
try:  # 纯文件装载的测试宿主可能没有贴图计划模块：退化为旧「先落盘再删除」流程
    from ..common.texture_export_plan import TextureExportPlan
except ImportError:  # pragma: no cover
    TextureExportPlan = None
from ..utils.log_utils import LOG
from ..utils.timer_utils import TimerUtils
from . import export_progress
from .direct_export_multifile import DirectMultiFileGenerator
from .direct_export_shapekey import DirectShapeKeyGenerator
from .export_helper import BlueprintExportHelper
from .export_parallel import ExportRoundExecutor
from .export_step_plan import build_step_plan
from .model import BluePrintModel
from .preprocess import PreProcessHelper
from .preprocess_parallel import ParallelPreprocessCoordinator
from .sync import refresh_blueprint_sync_state
from ..ui.universal.efmi import ExportEFMI
from ..ui.universal.gimi import ExportGIMI
from ..ui.universal.himi import ExportHIMI
from ..ui.universal.identityv import ExportIdentityV
from ..ui.universal.snowbreak import ExportSnowBreak
from ..ui.universal.srmi import ExportSRMI
from ..ui.universal.unity import ExportUnity
from ..ui.wwmi.wwmi_export import ExportWWMI
from ..ui.universal.yysls import ExportYYSLS
from ..ui.universal.zzmi import ExportZZMI
from ..common.logic_name import LogicName


_SYNC_GUARD = False


class DirectExportError(RuntimeError):
    pass


def _call_runtime_recorder(method_name: str, *args) -> None:
    """调用 helper 上的导出期登记方法；helper 缺该方法或实现抛错时静默跳过。

    登记只是给后处理节点用的附加信息：helper 可能来自局部升级的旧文件、测试桩或裁剪环境，
    缺方法或抛异常都不得让本轮导出失败。本函数刻意留在本模块内 —— tests 会把
    blueprint.export_helper 整体替换成桩对象，跨模块导入这个兜底函数会直接 ImportError。
    """
    recorder = getattr(BlueprintExportHelper, method_name, None)
    if not callable(recorder):
        return
    try:
        recorder(*args)
    except Exception:
        pass


def _raise_for_unknown_logic_name() -> None:
    raise DirectExportError(
        f"当前 logic_name='{GlobalConfig.logic_name}' 未加载或不受支持，无法执行直出。"
    )


def _get_sync_root_tree(node, context=None):
    root_tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)
    if root_tree:
        return root_tree
    return getattr(node, "id_data", None)


#: 节点进度键的分隔符：``树名/节点名``。导出会递归展开嵌套蓝图，不同蓝图
#: 树里的同名节点必须能区分开，所以 worker 上报一律带所属树名。
_NODE_KEY_SEPARATOR = "/"
#: 嵌套蓝图节点与 SSMT 蓝图树类型：与导出流程 ``_collect_postprocess_nodes`` /
#: ``model.py::_resolve_nested_blueprint_collect`` 同一套递归口径。
_NODE_TYPE_BLUEPRINT_NEST = "SSMTNode_Blueprint_Nest"
_BLUEPRINT_TREE_IDNAME = "SSMTBlueprintTreeType"
_BLUEPRINT_NONE_NAME = "NONE"


def _node_progress_key(tree, node) -> str:
    """节点进度键：``树名/节点名``；没有树名时退回纯节点名（旧格式兼容）。"""
    node_name = str(getattr(node, "name", "") or "").strip()
    if not node_name:
        return ""
    tree_name = str(getattr(tree, "name", "") or "").strip()
    if not tree_name:
        return node_name
    return f"{tree_name}{_NODE_KEY_SEPARATOR}{node_name}"


def _iter_nested_blueprint_trees(tree):
    """某个蓝图树里 ``SSMTNode_Blueprint_Nest`` 引用到的嵌套蓝图树。

    跳过静音节点、``NONE`` / 空名、不存在的树、非 SSMT 蓝图树（与导出流程
    ``model.py::_resolve_nested_blueprint_collect`` 同口径）；异常静默。
    """
    for node in getattr(tree, "nodes", None) or ():
        try:
            if str(getattr(node, "bl_idname", "") or "") != _NODE_TYPE_BLUEPRINT_NEST:
                continue
            if bool(getattr(node, "mute", False)):
                continue
            blueprint_name = str(getattr(node, "blueprint_name", "") or "").strip()
            if not blueprint_name or blueprint_name == _BLUEPRINT_NONE_NAME:
                continue
            nested_tree = bpy.data.node_groups.get(blueprint_name)
            if nested_tree is None:
                continue
            if str(getattr(nested_tree, "bl_idname", "") or "") != _BLUEPRINT_TREE_IDNAME:
                continue
            yield nested_tree
        except Exception:
            continue


def _iter_blueprint_trees(tree):
    """主蓝图树 + 递归展开的嵌套蓝图树（先主树，再按引用顺序广度优先）。

    ``visited`` 按对象 id 记已展开的树，循环引用（A 嵌 B、B 又嵌 A）不会死循环。
    """
    if tree is None:
        return

    visited = set()
    pending = [tree]
    while pending:
        current = pending.pop(0)
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        yield current
        for nested_tree in _iter_nested_blueprint_trees(current):
            if id(nested_tree) not in visited:
                pending.append(nested_tree)


def _report_postprocess_node(node, status: str) -> None:
    """后台导出时上报后处理节点进度；同步导出（没有进度文件）时静默。

    后处理节点可能来自嵌套蓝图，键用 ``node.id_data``（节点所属蓝图树）拿树名。
    """
    try:
        key = _node_progress_key(getattr(node, "id_data", None), node)
        if key:
            export_progress.report_node(key, status)
    except Exception:
        pass


#: 直出流程里参与节点高亮、但没有自己的后处理上报入口的阶段节点。
#: 这些节点不进入主进程预计算的步骤预算，只用 ``count_step=False`` 更新
#: ``stage.json`` 的 ``nodes`` 表，让 ``_sync_node_highlight`` 按阶段逐个上色。
#: 收集物体 / 前处理阶段真正读取的节点（前处理本身没有专属节点，保持它 running）。
_OBJECT_INFO_NODE_IDNAMES = ("SSMTNode_Object_Info", "SSMTNode_Object_Info_Pro")
#: 解析蓝图 / 构建导出器时读取的节点：顶点组处理（model.py 里批量执行）、
#: 物体组（处理链上的分组节点）、蓝图嵌套（展开嵌套蓝图）。
_BLUEPRINT_PARSE_NODE_IDNAMES = (
    "SSMTNode_VertexGroupProcess",
    "SSMTNode_Object_Group",
    "SSMTNode_Blueprint_Nest",
)
#: 基础导出（Generate Mod）使用的输出节点；NTMI ModImp 是另一种输出节点类型。
_RESULT_OUTPUT_NODE_IDNAMES = (
    "SSMTNode_Result_Output",
    "SSMTNode_Result_Output_NTMIModImp",
)


def _iter_stage_node_names(tree, bl_idnames):
    """按 ``bl_idname`` 从主树 + 递归嵌套树里取节点进度键。

    键格式 ``树名/节点名``；跳过静音节点与空名，去重。
    """
    if tree is None:
        return []
    desired = {str(value or "") for value in (bl_idnames or ()) if str(value or "")}
    if not desired:
        return []

    keys = []
    seen_keys = set()
    for current_tree in _iter_blueprint_trees(tree):
        for node in getattr(current_tree, "nodes", None) or ():
            try:
                if bool(getattr(node, "mute", False)):
                    continue
                if str(getattr(node, "bl_idname", "") or "") not in desired:
                    continue
                key = _node_progress_key(current_tree, node)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                keys.append(key)
            except Exception:
                continue
    return keys


def _collect_participant_node_names(tree):
    """收集主进程会补全状态的非后处理节点键（与 ``ui_func_export`` 同口径）。

    与导出流程一致地递归遍历主树 + 嵌套蓝图树，键格式 ``树名/节点名``。
    主进程的 ``_participant_phase_status`` 在 ``stage.json`` 里出现任何节点后
    会把**没上报**的参与节点一律猜成 done。worker 先把它们登记为 pending，
    才能让主进程不再"整片"上色；状态由下面的阶段上报逐个推进。
    """
    if tree is None:
        return []

    try:
        checker = getattr(BlueprintExportHelper, "_is_node_connected_to_output", None)
    except Exception:
        checker = None

    keys = []
    seen_keys = set()
    for current_tree in _iter_blueprint_trees(tree):
        for node in getattr(current_tree, "nodes", None) or ():
            try:
                if bool(getattr(node, "mute", False)):
                    continue
                bl_idname = str(getattr(node, "bl_idname", "") or "")
                if bl_idname.startswith("SSMTNode_PostProcess_"):
                    continue
                if bl_idname.startswith("SSMTNode_Result_"):
                    continue
                if bl_idname in ("NodeReroute", "NodeFrame"):
                    continue
                if callable(checker):
                    try:
                        if not checker(current_tree, node):
                            continue
                    except Exception:
                        continue
                key = _node_progress_key(current_tree, node)
                if not key or key in seen_keys:
                    continue
                seen_keys.add(key)
                keys.append(key)
            except Exception:
                continue
    return keys


def _report_participant_nodes(tree, status: str) -> None:
    """批量上报主进程关注的参与节点（pending 批量写，done 只驱动高亮）。"""
    try:
        names = _collect_participant_node_names(tree)
        if not names:
            return
        if str(status or "") == export_progress.STATE_PENDING:
            export_progress.report_nodes(names)
            return
        for name in names:
            export_progress.report_node(name, status, count_step=False)
    except Exception:
        pass


def _report_stage_nodes(tree, bl_idnames, status: str) -> None:
    """把某阶段真正使用的蓝图节点按 ``bl_idname`` 批量写进 stage.json。

    - ``pending`` 用 ``report_nodes`` 批量登记，导出开始时一次上蓝灰色；
    - ``running`` / ``done`` 用 ``report_node(..., count_step=False)``，
      只驱动节点高亮，不占用主进程预算的步骤数；
    - 任何异常静默，进度上报绝不能拖垮导出。
    """
    try:
        names = _iter_stage_node_names(tree, bl_idnames)
        if not names:
            return
        if str(status or "") == export_progress.STATE_PENDING:
            export_progress.report_nodes(names)
            return
        for name in names:
            export_progress.report_node(name, status, count_step=False)
    except Exception:
        pass


def _prepare_postprocess_nodes(postprocess_nodes, name_mapping: dict[str, str] | None = None):
    # 直出会复用标准后处理节点，这里先清缓存，再把运行时副本名回填到节点。
    for postprocess_node in postprocess_nodes or []:
        node_class = type(postprocess_node)
        clear_cache = getattr(node_class, "clear_cache", None)
        if callable(clear_cache):
            try:
                clear_cache()
            except Exception:
                pass

    if not name_mapping:
        return

    LOG.info(f"Direct export: applying name mapping to {len(name_mapping)} postprocess nodes")
    for postprocess_node in postprocess_nodes or []:
        apply_name_mapping = getattr(postprocess_node, "apply_name_mapping", None)
        if callable(apply_name_mapping):
            try:
                apply_name_mapping(name_mapping)
            except Exception as exc:
                LOG.warning(f"   后处理节点 '{postprocess_node.name}' 应用名称映射失败: {exc}")


def collect_direct_shapekey_nodes(tree) -> list[bpy.types.Node]:
    return [
        node
        for node in BlueprintExportHelper.collect_shapekey_postprocess_nodes(tree)
        if getattr(node, "direct_export_mode", False)
    ]


def collect_direct_multifile_nodes(tree) -> list[bpy.types.Node]:
    return [
        node
        for node in BlueprintExportHelper.collect_multi_file_export_nodes(tree)
        if getattr(node, "direct_export_mode", False)
    ]


def has_direct_export_mode(tree) -> bool:
    if not tree:
        return False

    return bool(collect_direct_shapekey_nodes(tree) or collect_direct_multifile_nodes(tree))


def sync_shapekey_direct_mode(node, context=None):
    global _SYNC_GUARD
    if _SYNC_GUARD:
        return

    tree = _get_sync_root_tree(node, context=context)
    if not tree:
        return

    try:
        _SYNC_GUARD = True
        for linked_node in BlueprintExportHelper.collect_shapekey_postprocess_nodes(tree):
            if linked_node is node:
                continue
            if getattr(linked_node, "direct_export_mode", False) != bool(node.direct_export_mode):
                linked_node.direct_export_mode = bool(node.direct_export_mode)
    finally:
        _SYNC_GUARD = False

    if not bool(getattr(node, "direct_export_mode", False)):
        BlueprintExportHelper.reset_direct_export_runtime_state(clear_postprocess_caches=True)


def sync_multifile_direct_mode(node, context=None):
    global _SYNC_GUARD
    if _SYNC_GUARD:
        return

    tree = _get_sync_root_tree(node, context=context)
    if not tree:
        return

    try:
        _SYNC_GUARD = True
        for linked_node in BlueprintExportHelper.collect_multi_file_export_nodes(tree):
            if linked_node is node:
                continue
            if getattr(linked_node, "direct_export_mode", False) != bool(node.direct_export_mode):
                linked_node.direct_export_mode = bool(node.direct_export_mode)
    finally:
        _SYNC_GUARD = False

    if not bool(getattr(node, "direct_export_mode", False)):
        BlueprintExportHelper.reset_direct_export_runtime_state(clear_postprocess_caches=True)


def _build_exporter(blueprint_model):
    if GlobalConfig.logic_name in (LogicName.EFMI, LogicName.HTMI):
        return ExportEFMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "GIMI":
        return ExportGIMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "HIMI":
        return ExportHIMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "IdentityV":
        return ExportIdentityV(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "SRMI":
        return ExportSRMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "ZZMIDX12":
        from ..ui.universal.zzmidx12 import ExportZZMIDX12
        return ExportZZMIDX12(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "ZZMI":
        return ExportZZMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "WWMI":
        return ExportWWMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "SnowBreak":
        return ExportSnowBreak(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "YYSLS":
        return ExportYYSLS(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name in ("Naraka", "NarakaM", "GF2", "AILIMIT"):
        return ExportUnity(blueprint_model=blueprint_model)

    _raise_for_unknown_logic_name()


def _reset_buffer_folder(folder_name: str):
    base_path = GlobalConfig.path_generate_mod_folder()
    folder_path = os.path.join(base_path, folder_name)
    if os.path.isdir(folder_path):
        shutil.rmtree(folder_path, ignore_errors=True)
    os.makedirs(folder_path, exist_ok=True)
    return folder_path


def _build_object_name_list(tree, has_shapekey: bool, has_multifile: bool) -> list[str]:
    return BlueprintExportHelper.collect_connected_preprocess_object_names(tree)


class DirectExportSession:
    """直出导出会话管理器，管理从前处理到后处理的完整直出流程"""

    def __init__(self, context, tree):
        self.context = context
        self.tree = tree
        self.base_blueprint_model = None
        self.base_exporter = None
        self.shapekey_source_blueprint_model = None
        self.shapekey_source_exporter = None
        self.direct_shapekey_nodes = []
        self.direct_multifile_nodes = []
        self.ordered_postprocess_nodes = []
        self.pose_shading = {}

    def _collect_direct_nodes(self):
        BlueprintExportHelper.set_runtime_blueprint_tree(self.tree)
        self.direct_shapekey_nodes = collect_direct_shapekey_nodes(self.tree)
        self.direct_multifile_nodes = collect_direct_multifile_nodes(self.tree)
        self.ordered_postprocess_nodes = BlueprintExportHelper._collect_postprocess_nodes(self.tree)

        if not self.direct_shapekey_nodes and not self.direct_multifile_nodes:
            raise DirectExportError("当前蓝图中没有启用直出模式的形态键配置或多文件节点。")

    def _setup_base_state(self):
        if self.direct_shapekey_nodes:
            BlueprintExportHelper.collect_shapekey_objects(self.tree)
            # 清零前先记录场景值：收尾要还原，且「烘焙未勾选形态键」只能从这里拿到真实当前值
            BlueprintExportHelper.capture_shapekey_scene_state()
            BlueprintExportHelper.set_all_shapekey_values(0)

        BlueprintExportHelper.set_current_export_index(1)

    def _collect_objects(self):
        return PreProcessHelper.collect_target_object_names_strict(
            _build_object_name_list(
                self.tree,
                has_shapekey=bool(self.direct_shapekey_nodes),
                has_multifile=bool(self.direct_multifile_nodes),
            )
        )

    def _run_preprocess(
        self,
        object_names: list[str],
        preserve_shape_keys: bool = False,
        capture_shape_keys: bool = False,
    ):
        if not object_names:
            raise DirectExportError("未收集到可处理的物体。")

        if capture_shape_keys:
            BlueprintExportHelper.clear_direct_shapekey_position_records()
            if GlobalProterties.enable_parallel_preprocess():
                original_to_copy_map = ParallelPreprocessCoordinator.execute_preprocess_capture_shape_keys(object_names)
            else:
                # 串行直出现在也复用前处理缓存，并把 ShapeKey 采样记录作为缓存侧车一起管理。
                original_to_copy_map = PreProcessHelper.execute_preprocess_capture_shape_keys(object_names)
        elif preserve_shape_keys:
            if GlobalProterties.enable_parallel_preprocess():
                original_to_copy_map = ParallelPreprocessCoordinator.execute_preprocess_preserve_shape_keys(object_names)
            else:
                original_to_copy_map = PreProcessHelper.execute_preprocess_preserve_shape_keys(object_names)
        elif GlobalProterties.enable_parallel_preprocess():
            original_to_copy_map = ParallelPreprocessCoordinator.execute_preprocess(object_names)
        else:
            original_to_copy_map = PreProcessHelper.execute_preprocess(object_names)

        nested_trees = []
        if original_to_copy_map:
            nested_trees = ExportRoundExecutor.collect_nested_trees(self.tree)
            PreProcessHelper.update_blueprint_node_references(self.tree, nested_trees)

        return original_to_copy_map, nested_trees

    def _build_blueprint_model(self):
        BluePrintModel.clear_object_name_mapping()
        model = BluePrintModel(tree=self.tree, context=self.context)
        # 导出期缓存：后处理节点在导出结束之后才执行，需要据此反查 DrawIB（只写不清）。
        _call_runtime_recorder("set_current_blueprint_model", model)
        return model

    def _build_exporter(self, blueprint_model, preserve_shape_key_mix: bool | None = None):
        if preserve_shape_key_mix is None:
            preserve_shape_key_mix = bool(self.direct_shapekey_nodes)
        BlueprintExportHelper.set_preserve_current_shapekey_mix_for_export(preserve_shape_key_mix)
        try:
            return _build_exporter(blueprint_model)
        finally:
            BlueprintExportHelper.set_preserve_current_shapekey_mix_for_export(False)

    def _prepare_shapekey_buffer_names(self, blueprint_model=None):
        report_blueprint_model = blueprint_model or self.base_blueprint_model
        if not self.direct_shapekey_nodes or report_blueprint_model is None:
            BlueprintExportHelper.clear_runtime_shapekey_buffer_names()
            return

        report_generated = BlueprintExportHelper.generate_shapekey_classification_report(report_blueprint_model)
        if not report_generated:
            BlueprintExportHelper.clear_runtime_shapekey_buffer_names()
            return

        classification_text_obj = next(
            (text for text in bpy.data.texts if "Shape_Key_Classification" in text.name),
            None,
        )
        if classification_text_obj is None:
            BlueprintExportHelper.clear_runtime_shapekey_buffer_names()
            return

        slot_to_name_to_objects, _unique_hashes, _hash_to_objects, _all_objects = (
            self.direct_shapekey_nodes[0]._parse_classification_text_final(
                classification_text_obj.as_string()
            )
        )
        shapekey_name_map = {}
        for names_data in slot_to_name_to_objects.values():
            for shapekey_name, objects in names_data.items():
                for obj_name in objects:
                    # Shape key names are opaque labels: keep exact names from the
                    # classification report, including "Key 1" or non-ASCII names.
                    obj_hash = self.direct_shapekey_nodes[0]._extract_hash_from_name(obj_name)
                    obj_prefix = self.direct_shapekey_nodes[0]._extract_hash_prefix(obj_hash) if obj_hash else None
                    for key in (obj_hash, obj_prefix):
                        if not key:
                            continue
                        shapekey_name_map.setdefault(key, []).append(shapekey_name)
        BlueprintExportHelper.set_runtime_shapekey_buffer_name_map(shapekey_name_map)
        BlueprintExportHelper.set_runtime_shapekey_buffer_names([])
        BlueprintExportHelper.runtime_shapekey_sequences = ()
        for node in self.direct_shapekey_nodes:
            if not (getattr(node, "use_frame_table", False) and getattr(node, "store_all_vertex_channels", False)):
                continue
            names = dict.fromkeys(
                name for names_data in slot_to_name_to_objects.values() for name in names_data
            )
            names_to_vars = {name: node.get_shape_key_export_variable_name(name) for name in names}
            vars_to_names = {var.lstrip("$"): name for name, var in names_to_vars.items()}
            generator = DirectShapeKeyGenerator(node, "", blueprint_model=None, exporter=None)
            groups, _totals = generator._read_sequence_groups_from_ext_node(names_to_vars)
            BlueprintExportHelper.runtime_shapekey_sequences = tuple(
                tuple(vars_to_names[var] for var in members)
                for members in groups.values()
            )
            return slot_to_name_to_objects, names_to_vars, groups, _totals

    def _frame_table_cache(self, frame_settings):
        from .frame_table_cache import FrameTableCache
        from .preprocess_cache import PreProcessCache

        cache_dir = PreProcessCache.get_cache_dir()
        if not cache_dir or not frame_settings:
            return None
        slots, variable_names, sequence_groups, totals = frame_settings
        node = self.direct_shapekey_nodes[0]
        if not sequence_groups:
            return None
        draw_calls = self.base_blueprint_model.ordered_draw_obj_data_model_list
        relative_keys = {}
        for name in dict.fromkeys(draw.get_blender_obj_name() for draw in draw_calls):
            obj = bpy.data.objects.get(name)
            shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
            if shape_keys is not None:
                relative_keys[name] = [
                    (key.name, key.relative_key.name if key.relative_key else None)
                    for key in shape_keys.key_blocks
                ]
        context = [
            bpy.app.version_string,
            GlobalConfig.logic_name,
            # Draw conditions are emitted from the current INI. Geometry and
            # vertex ordering are validated separately by sample/base digests.
            sorted((draw.obj_name, draw.get_blender_obj_name()) for draw in draw_calls),
            slots,
            variable_names,
            sequence_groups,
            totals,
            sorted(relative_keys.items()),
            node.use_packed_Meshess,
            node.use_optimized_lookup,
            node.merge_slot_files,
            node.use_sparse_vertex_index,
            node.drag_drive_enabled,
        ]
        digest = hashlib.sha256(json.dumps(context, ensure_ascii=False).encode("utf-8"))
        root = Path(__file__).resolve().parents[1]
        sample_version = hashlib.sha256(b"full-channel-pose-groups-v2")
        for source in (
            "utils/export_utils.py", "utils/shapekey_pose_worker.py",
            "common/obj_buffer_helper.py", "common/shapekey_native.py",
        ):
            sample_version.update((root / source).read_bytes())
        digest.update(sample_version.digest())
        for source in (
            "blueprint/direct_export_shapekey.py", "blueprint/direct_export_shapekey_output_mixin.py",
            "blueprint/frame_table_cache.py", "common/drawib_model.py",
            "Toolset/shapekey_anim_frame_table.hlsl", "Toolset/shapekey_weight_sync.hlsl",
        ):
            digest.update((root / source).read_bytes())
        blueprint_id = hashlib.sha256(self.tree.name.encode("utf-8")).hexdigest()[:24]
        bundle = Path(cache_dir) / "frame_tables" / f"{blueprint_id}.zip"
        return FrameTableCache(bundle, digest.hexdigest(), sample_version.hexdigest())

    def _export_full(self, exporter, folder_name: str):
        BlueprintExportHelper.set_current_buffer_folder_name(folder_name)
        _reset_buffer_folder(folder_name)
        exporter.export()

    def _export_buffers_only(self, exporter, folder_name: str):
        BlueprintExportHelper.set_current_buffer_folder_name(folder_name)
        _reset_buffer_folder(folder_name)
        exporter.export_buffers_only()

    def _run_standard_shapekey_rounds(self):
        max_slot = BlueprintExportHelper.calculate_max_shapekey_slot_count(self.tree)
        if max_slot <= 0:
            return

        # The base Meshes0000 export has already been produced. Generate the
        # slot rounds with the same executor used by the standard export path so
        # Meshes1001+ stay byte-aligned with the non-direct baseline.
        PreProcessHelper.cleanup_copies(silent=True)

        reference_size = 0
        base_folder = os.path.join(GlobalConfig.path_generate_mod_folder(), "Meshes0000")
        try:
            for dirpath, _dirnames, filenames in os.walk(base_folder):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.isfile(filepath):
                        reference_size += os.path.getsize(filepath)
        except Exception:
            reference_size = 0

        for slot_index in range(1, max_slot + 1):
            round_plan = {
                "round_index": slot_index + 1,
                "phase": "shapekey",
                "description": f"Direct ShapeKey Slot {slot_index}",
                "buffer_folder_name": f"Meshes1{slot_index:03d}",
                "export_index": 1,
                "generate_ini": False,
                "generate_classification_report": False,
                "shapekey_mode": "slot",
                "shapekey_slot_index": slot_index,
            }
            round_result = ExportRoundExecutor.execute_round(
                tree=self.tree,
                round_plan=round_plan,
                allow_parallel_preprocess=True,
                **({"pose_shading": self.pose_shading} if self.pose_shading else {}),
            )
            if reference_size and round_result.get("buffer_size", 0) != reference_size:
                LOG.warning(
                    "直出形态键附加轮次的 Buffer 大小与 Meshes0000 不一致: "
                    f"{round_plan['buffer_folder_name']}={round_result.get('buffer_size', 0)}, "
                    f"Meshes0000={reference_size}"
                )

    def _needs_standard_shapekey_rounds(self) -> bool:
        for postprocess_node in self.ordered_postprocess_nodes:
            if postprocess_node.bl_idname != "SSMTNode_PostProcess_ShapeKey":
                continue
            if not getattr(postprocess_node, "direct_export_mode", False):
                return True
        return False

    def run(self):
        # 步骤清单在导出开始前一次算好：后台导出已由 ``_run_worker`` 用同一个
        # 计划函数建好（ensure_plan 不会覆盖）；同步直出（没有后台会话清单）时
        # 在这里自建，保证 total 从第一步起就是最终值、全程不变。
        try:
            export_progress.ensure_plan(
                build_step_plan(self.tree, direct_export=True)
            )
        except Exception:
            pass

        TimerUtils.start_stage("Direct-CollectNodes")
        # 先把主进程关注的参与节点统一登记为 pending；主进程对"没上报"的
        # 参与节点会整体猜成 done，逐个上报前必须先占住这些名字。
        _report_participant_nodes(self.tree, export_progress.STATE_PENDING)
        _report_stage_nodes(self.tree, _RESULT_OUTPUT_NODE_IDNAMES, export_progress.STATE_PENDING)
        self._collect_direct_nodes()
        TimerUtils.end_stage("Direct-CollectNodes")

        has_shapekey = bool(self.direct_shapekey_nodes)
        has_multifile = bool(self.direct_multifile_nodes)
        frame_cache = None
        frame_table_all_channels = any(
            getattr(node, "use_frame_table", False)
            and getattr(node, "store_all_vertex_channels", False)
            for node in self.direct_shapekey_nodes
        )

        object_names = None
        TimerUtils.start_stage("Direct-SetupState")
        if getattr(self.tree, "use_current_pose_shading", False):
            from ..utils.current_pose_shading import capture

            object_names = self._collect_objects()
            self.pose_shading = capture(self.tree, object_names)
        self._setup_base_state()
        TimerUtils.end_stage("Direct-SetupState")

        TimerUtils.start_stage("Direct-CollectObjects")
        _report_stage_nodes(self.tree, _OBJECT_INFO_NODE_IDNAMES, export_progress.STATE_RUNNING)
        if object_names is None:
            object_names = self._collect_objects()
        TimerUtils.end_stage("Direct-CollectObjects")

        TimerUtils.start_stage("Direct-Preprocess")
        if frame_table_all_channels:
            # Keep the preprocessed keys so the exporter can build per-key
            # vertex buffers in the same index space as the base buffer.
            base_copy_map, _nested_trees = self._run_preprocess(object_names, preserve_shape_keys=True)
        elif has_shapekey:
            BlueprintExportHelper.clear_direct_shapekey_position_records()
            base_copy_map, _nested_trees = self._run_preprocess(object_names, capture_shape_keys=True)
        else:
            base_copy_map, _nested_trees = self._run_preprocess(object_names, preserve_shape_keys=False)
        TimerUtils.end_stage("Direct-Preprocess")
        # 前处理没有专属节点：收集物体时点亮的物体信息节点一直保持 running，
        # 直到前处理真正结束才置为 done。
        _report_stage_nodes(self.tree, _OBJECT_INFO_NODE_IDNAMES, export_progress.STATE_DONE)

        try:
            if frame_table_all_channels:
                TimerUtils.start_stage("Preprocess-BakeDisabledShapeKeys")
                PreProcessHelper._bake_disabled_shape_keys(list(base_copy_map.values()))
                TimerUtils.end_stage("Preprocess-BakeDisabledShapeKeys")
            if self.pose_shading:
                from ..utils.current_pose_shading import apply_to_copies

                apply_to_copies(base_copy_map, self.pose_shading)
            if has_shapekey:
                TimerUtils.start_stage("Direct-BlueprintModel")
                _report_stage_nodes(
                    self.tree,
                    _BLUEPRINT_PARSE_NODE_IDNAMES,
                    export_progress.STATE_RUNNING,
                )
                self.base_blueprint_model = self._build_blueprint_model()
                TimerUtils.end_stage("Direct-BlueprintModel")

                frame_settings = self._prepare_shapekey_buffer_names(self.base_blueprint_model)
                if (
                    frame_table_all_channels and len(self.direct_shapekey_nodes) == 1
                    and not has_multifile and not self._needs_standard_shapekey_rounds()
                    and GlobalProterties.enable_export_cache()
                ):
                    frame_cache = self._frame_table_cache(frame_settings)
                    BlueprintExportHelper.runtime_frame_table_cache = frame_cache

                TimerUtils.start_stage("Direct-BuildExporter")
                self.base_exporter = self._build_exporter(
                    self.base_blueprint_model,
                    preserve_shape_key_mix=frame_table_all_channels,
                )
                TimerUtils.end_stage("Direct-BuildExporter")
                _report_stage_nodes(
                    self.tree,
                    _BLUEPRINT_PARSE_NODE_IDNAMES,
                    export_progress.STATE_DONE,
                )
                self.shapekey_source_blueprint_model = self.base_blueprint_model
                self.shapekey_source_exporter = self.base_exporter
                base_name_mapping = dict(BluePrintModel._object_name_mapping)
                mod_export_path = GlobalConfig.path_generate_mod_folder()

                BlueprintExportHelper.set_suppress_shapekey_resource_export(True)
                try:
                    TimerUtils.start_stage("Direct-BaseExport")
                    _report_stage_nodes(
                        self.tree,
                        _RESULT_OUTPUT_NODE_IDNAMES,
                        export_progress.STATE_RUNNING,
                    )
                    self._export_full(self.base_exporter, "Meshes0000")
                    if frame_cache is not None:
                        if frame_cache.finish_probe(mod_export_path):
                            LOG.info("全属性帧表缓存命中：已跳过形态键采样和帧表重建")
                        elif frame_cache.skipped_samples:
                            LOG.info("全属性帧表无法整体复用，检查分组缓存并补齐采样")
                            frame_cache.reset_probe()
                            BlueprintExportHelper.set_suppress_shapekey_resource_export(False)
                            self.base_exporter = self._build_exporter(
                                self.base_blueprint_model, preserve_shape_key_mix=True
                            )
                            self.shapekey_source_exporter = self.base_exporter
                            base_name_mapping = dict(BluePrintModel._object_name_mapping)
                            BlueprintExportHelper.set_suppress_shapekey_resource_export(True)
                            self._export_full(self.base_exporter, "Meshes0000")
                            frame_cache.finish_probe(mod_export_path)
                    TimerUtils.end_stage("Direct-BaseExport")
                    _report_stage_nodes(
                        self.tree,
                        _RESULT_OUTPUT_NODE_IDNAMES,
                        export_progress.STATE_DONE,
                    )
                finally:
                    BlueprintExportHelper.set_suppress_shapekey_resource_export(False)

            if not has_shapekey:
                TimerUtils.start_stage("Direct-BlueprintModel")
                _report_stage_nodes(
                    self.tree,
                    _BLUEPRINT_PARSE_NODE_IDNAMES,
                    export_progress.STATE_RUNNING,
                )
                self.base_blueprint_model = self._build_blueprint_model()
                TimerUtils.end_stage("Direct-BlueprintModel")

                self._prepare_shapekey_buffer_names(self.base_blueprint_model)

                BlueprintExportHelper.set_suppress_shapekey_resource_export(False)
                TimerUtils.start_stage("Direct-BuildExporter")
                self.base_exporter = self._build_exporter(
                    self.base_blueprint_model,
                    preserve_shape_key_mix=False,
                )
                TimerUtils.end_stage("Direct-BuildExporter")
                _report_stage_nodes(
                    self.tree,
                    _BLUEPRINT_PARSE_NODE_IDNAMES,
                    export_progress.STATE_DONE,
                )
                base_name_mapping = dict(BluePrintModel._object_name_mapping)
                mod_export_path = GlobalConfig.path_generate_mod_folder()
                TimerUtils.start_stage("Direct-BaseExport")
                _report_stage_nodes(
                    self.tree,
                    _RESULT_OUTPUT_NODE_IDNAMES,
                    export_progress.STATE_RUNNING,
                )
                self._export_full(self.base_exporter, "Meshes0000")
                TimerUtils.end_stage("Direct-BaseExport")
                _report_stage_nodes(
                    self.tree,
                    _RESULT_OUTPUT_NODE_IDNAMES,
                    export_progress.STATE_DONE,
                )


            # 基础导出结束后，所有前处理 / 蓝图解析 / 导出轮次节点都已执行完；
            # 没在上面的阶段映射里逐个上报的参与节点在这里统一置为 done
            # （不推进一步，只补高亮状态）。
            _report_participant_nodes(self.tree, export_progress.STATE_DONE)

            _prepare_postprocess_nodes(
                self.ordered_postprocess_nodes,
                base_name_mapping,
            )
            if GlobalConfig.logic_name == LogicName.NTEMI:
                from ..ui.ntmi_modimp.texture_slot_refresh import refresh_texture_slots_for_objects
                refresh_texture_slots_for_objects(
                    [
                        obj
                        for draw_call_model in getattr(self.base_blueprint_model, "ordered_draw_obj_data_model_list", []) or []
                        for obj in [bpy.data.objects.get(draw_call_model.get_blender_obj_name())]
                        if obj is not None and obj.type == "MESH"
                    ]
                )

            if has_shapekey and self._needs_standard_shapekey_rounds():
                # 只有存在未启用直出的 ShapeKey 后处理节点时，才补跑传统附加轮次。
                TimerUtils.start_stage("直出-形态键附加轮次")
                self._run_standard_shapekey_rounds()
                TimerUtils.end_stage("直出-形态键附加轮次")
            elif has_shapekey:
                LOG.info("Direct ShapeKey: skipped standard slot export; generating direct resources from Meshes0000 and sampled vertex data")

            # 附加形态键轮次（_run_standard_shapekey_rounds → execute_round）会把运行时缓存里的
            # 蓝图模型覆盖成轮次模型；后处理阶段要的是基态模型，这里再登记一次。
            _call_runtime_recorder(
                "set_current_blueprint_model",
                self.shapekey_source_blueprint_model or self.base_blueprint_model,
            )

            try:
                keys = [
                    _node_progress_key(getattr(node, "id_data", None), node)
                    for node in self.ordered_postprocess_nodes
                ]
                export_progress.report_nodes([key for key in keys if key])
            except Exception:
                pass

            for postprocess_node in self.ordered_postprocess_nodes:
                _report_postprocess_node(postprocess_node, export_progress.STATE_RUNNING)
                if postprocess_node.bl_idname == "SSMTNode_PostProcess_ShapeKey":
                    if getattr(postprocess_node, "direct_export_mode", False):
                        # 直出模式下直接从 Meshes0000 和前处理记录构造 ShapeKey 资源。
                        TimerUtils.start_stage("直出-形态键生成")
                        DirectShapeKeyGenerator(
                            node=postprocess_node,
                            mod_export_path=mod_export_path,
                            blueprint_model=self.shapekey_source_blueprint_model or self.base_blueprint_model,
                            exporter=self.shapekey_source_exporter or self.base_exporter,
                        ).generate(frame_cache=frame_cache)
                        TimerUtils.end_stage("直出-形态键生成")
                    else:
                        execute_postprocess = getattr(postprocess_node, "execute_postprocess", None)
                        if callable(execute_postprocess):
                            execute_postprocess(mod_export_path)
                    _report_postprocess_node(postprocess_node, export_progress.STATE_DONE)
                    continue

                if postprocess_node.bl_idname == "SSMTNode_PostProcess_MultiFile":
                    if has_multifile:
                        TimerUtils.start_stage("Direct-MultiFileGenerate")
                        DirectMultiFileGenerator(
                            config_node=postprocess_node,
                            multi_file_nodes=self.direct_multifile_nodes,
                            mod_export_path=mod_export_path,
                            exporter=self.base_exporter,
                        ).generate()
                        TimerUtils.end_stage("Direct-MultiFileGenerate")
                    else:
                        execute_postprocess = getattr(postprocess_node, "execute_postprocess", None)
                        if callable(execute_postprocess):
                            execute_postprocess(mod_export_path)
                    _report_postprocess_node(postprocess_node, export_progress.STATE_DONE)
                    continue

                execute_postprocess = getattr(postprocess_node, "execute_postprocess", None)
                if callable(execute_postprocess):
                    execute_postprocess(mod_export_path)
                _report_postprocess_node(postprocess_node, export_progress.STATE_DONE)

            # 贴图计划兜底落盘：资源合并节点已在自身开头落盘，这里幂等；
            # 蓝图没有资源合并节点（或它被禁用）时也能把贴图盘出来。
            if TextureExportPlan is not None:
                try:
                    TextureExportPlan.finalize(GlobalConfig.path_generate_mod_folder())
                except Exception as plan_error:
                    LOG.warning(f"贴图计划落盘失败（不影响导出）: {plan_error}")

        finally:
            try:
                PreProcessHelper.cleanup_copies()
            finally:
                BlueprintExportHelper.clear_runtime_shapekey_buffer_names()
                BlueprintExportHelper.clear_direct_shapekey_position_records()
                BlueprintExportHelper.set_suppress_shapekey_resource_export(False)
                if has_shapekey:
                    BlueprintExportHelper.set_all_shapekey_values(0)
                # 收尾把导出前的场景形态键值写回原物体：导出过程中为了基态采样会临时清零，
                # 不能让它变成对用户场景的破坏（历史行为会在导出后留下全 0 的滑杆）。
                BlueprintExportHelper.restore_shapekey_scene_state()
                BlueprintExportHelper.set_current_export_index(1)


class SSMT_OT_GenerateDirectModBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.generate_mod_direct_blueprint"
    bl_label = "Generate Mod Direct"
    bl_description = "Generate direct export resources for ShapeKey and MultiFile nodes"
    bl_options = {'REGISTER', 'UNDO'}

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        default="",
        options={'SKIP_SAVE'},
    )  # type: ignore

    def _resolve_target_tree(self, context):
        requested_tree_name = str(getattr(self, "blueprint_name", "") or "").strip()
        if requested_tree_name:
            tree = BlueprintExportHelper.get_selected_blueprint_tree(
                selected_name=requested_tree_name,
                context=context,
            )
        else:
            tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)
        return tree

    def execute(self, context):
        tree = self._resolve_target_tree(context)
        if tree is None:
            self.report({'ERROR'}, "未找到当前蓝图树，请先在蓝图编辑器中选中目标蓝图。")
            return {'CANCELLED'}
        # 走统一后台入口，但声明只要直出流程：不能让它落进 NTMI ModImp 分支。
        result = bpy.ops.ssmt.generate_mod_blueprint(
            'INVOKE_DEFAULT',
            blueprint_name=tree.name,
            direct_export_only=True,
        )
        return {'FINISHED'} if result == {'RUNNING_MODAL'} else result


def execute_direct_export(context, tree):
    previous_runtime_tree_name = BlueprintExportHelper.runtime_blueprint_tree_name
    BlueprintExportHelper.set_runtime_blueprint_tree(tree)
    GlobalKeyCountHelper.initialize()
    # 贴图计划：直出流程同样按「资源合并」节点配置初始化（跨轮次累积，收尾统一落盘）。
    if TextureExportPlan is not None:
        try:
            TextureExportPlan.configure_from_tree(tree, GlobalConfig.path_generate_mod_folder())
        except Exception as plan_error:
            LOG.warning(f"贴图计划初始化失败（按旧流程导出）: {plan_error}")
            TextureExportPlan.reset()
    # 直出运行时开关：后处理在 session.run() 内部执行，所以整个会话期间都置真，
    # 结束后（异常也）复位，让普通 / 后台 / 并行导出看到的一直是「不是直出」。
    BlueprintExportHelper.set_direct_export_active(True)
    try:
        refresh_blueprint_sync_state(tree=tree, context=context, include_all_blueprints=True)
        BlueprintExportHelper.validate_object_info_references(tree)
        session = DirectExportSession(context=context, tree=tree)
        session.run()
    finally:
        BlueprintExportHelper.set_direct_export_active(False)
        BlueprintExportHelper.runtime_blueprint_tree_name = previous_runtime_tree_name


classes = (
    SSMT_OT_GenerateDirectModBlueprint,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
