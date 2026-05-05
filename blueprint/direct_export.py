import os
import shutil

import bpy

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..utils.log_utils import LOG
from ..utils.timer_utils import TimerUtils
from .direct_export_multifile import DirectMultiFileGenerator
from .direct_export_shapekey import DirectShapeKeyGenerator
from .export_helper import BlueprintExportHelper
from .export_parallel import ExportRoundExecutor
from .model import BluePrintModel
from .preprocess import PreProcessHelper
from .preprocess_parallel import ParallelPreprocessCoordinator
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


_SYNC_GUARD = False


class DirectExportError(RuntimeError):
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


def _build_exporter(blueprint_model):
    if GlobalConfig.logic_name == "EFMI":
        return ExportEFMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "GIMI":
        return ExportGIMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "HIMI":
        return ExportHIMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "IdentityV":
        return ExportIdentityV(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "SRMI":
        return ExportSRMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name == "ZZMI":
        return ExportZZMI(blueprint_model=blueprint_model)
    if GlobalConfig.logic_name in ("WWMI", "NTEMI"):
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
    return BlueprintExportHelper.collect_connected_object_names(tree)


class DirectExportSession:
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
        return BluePrintModel(tree=self.tree, context=self.context)

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
        TimerUtils.start_stage("Direct-CollectNodes")
        self._collect_direct_nodes()
        TimerUtils.end_stage("Direct-CollectNodes")

        has_shapekey = bool(self.direct_shapekey_nodes)
        has_multifile = bool(self.direct_multifile_nodes)

        TimerUtils.start_stage("Direct-SetupState")
        self._setup_base_state()
        TimerUtils.end_stage("Direct-SetupState")

        TimerUtils.start_stage("Direct-CollectObjects")
        object_names = self._collect_objects()
        TimerUtils.end_stage("Direct-CollectObjects")

        TimerUtils.start_stage("Direct-Preprocess")
        if has_shapekey:
            BlueprintExportHelper.clear_direct_shapekey_position_records()
            base_copy_map, _nested_trees = self._run_preprocess(object_names, capture_shape_keys=True)
        else:
            base_copy_map, _nested_trees = self._run_preprocess(object_names, preserve_shape_keys=False)
        TimerUtils.end_stage("Direct-Preprocess")

        try:
            if has_shapekey:
                TimerUtils.start_stage("Direct-BlueprintModel")
                self.base_blueprint_model = self._build_blueprint_model()
                TimerUtils.end_stage("Direct-BlueprintModel")

                self._prepare_shapekey_buffer_names(self.base_blueprint_model)

                TimerUtils.start_stage("Direct-BuildExporter")
                self.base_exporter = self._build_exporter(
                    self.base_blueprint_model,
                    preserve_shape_key_mix=False,
                )
                TimerUtils.end_stage("Direct-BuildExporter")
                self.shapekey_source_blueprint_model = self.base_blueprint_model
                self.shapekey_source_exporter = self.base_exporter
                base_name_mapping = dict(BluePrintModel._object_name_mapping)
                mod_export_path = GlobalConfig.path_generate_mod_folder()

                BlueprintExportHelper.set_suppress_shapekey_resource_export(True)
                try:
                    TimerUtils.start_stage("Direct-BaseExport")
                    self._export_full(self.base_exporter, "Meshes0000")
                    TimerUtils.end_stage("Direct-BaseExport")
                finally:
                    BlueprintExportHelper.set_suppress_shapekey_resource_export(False)

            if not has_shapekey:
                TimerUtils.start_stage("Direct-BlueprintModel")
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
                base_name_mapping = dict(BluePrintModel._object_name_mapping)
                mod_export_path = GlobalConfig.path_generate_mod_folder()
                TimerUtils.start_stage("Direct-BaseExport")
                self._export_full(self.base_exporter, "Meshes0000")
                TimerUtils.end_stage("Direct-BaseExport")


            _prepare_postprocess_nodes(
                self.ordered_postprocess_nodes,
                base_name_mapping,
            )

            if has_shapekey and self._needs_standard_shapekey_rounds():
                # 只有存在未启用直出的 ShapeKey 后处理节点时，才补跑传统附加轮次。
                TimerUtils.start_stage("直出-形态键附加轮次")
                self._run_standard_shapekey_rounds()
                TimerUtils.end_stage("直出-形态键附加轮次")
            elif has_shapekey:
                LOG.info("Direct ShapeKey: skipped standard slot export; generating direct resources from Meshes0000 and preprocess records")

            for postprocess_node in self.ordered_postprocess_nodes:
                if postprocess_node.bl_idname == "SSMTNode_PostProcess_ShapeKey":
                    if getattr(postprocess_node, "direct_export_mode", False):
                        # 直出模式下直接从 Meshes0000 和前处理记录构造 ShapeKey 资源。
                        TimerUtils.start_stage("直出-形态键生成")
                        DirectShapeKeyGenerator(
                            node=postprocess_node,
                            mod_export_path=mod_export_path,
                            blueprint_model=self.shapekey_source_blueprint_model or self.base_blueprint_model,
                            exporter=self.shapekey_source_exporter or self.base_exporter,
                        ).generate()
                        TimerUtils.end_stage("直出-形态键生成")
                    else:
                        execute_postprocess = getattr(postprocess_node, "execute_postprocess", None)
                        if callable(execute_postprocess):
                            execute_postprocess(mod_export_path)
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
                    continue

                execute_postprocess = getattr(postprocess_node, "execute_postprocess", None)
                if callable(execute_postprocess):
                    execute_postprocess(mod_export_path)

        finally:
            try:
                PreProcessHelper.cleanup_copies()
            finally:
                BlueprintExportHelper.clear_runtime_shapekey_buffer_names()
                BlueprintExportHelper.clear_direct_shapekey_position_records()
                BlueprintExportHelper.set_suppress_shapekey_resource_export(False)
                if has_shapekey:
                    BlueprintExportHelper.set_all_shapekey_values(0)
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
        LOG.start_collecting()
        GlobalConfig.read_from_main_json_ssmt4()

        TimerUtils.start_session("Mod直出")

        try:
            tree = self._resolve_target_tree(context)
            if not tree:
                self.report({'ERROR'}, "No current blueprint tree found")
                return {'CANCELLED'}

            BlueprintExportHelper.set_runtime_blueprint_tree(tree)

            session = DirectExportSession(context=context, tree=tree)
            session.run()

            self.report({'INFO'}, "Generate Mod Direct Success!")
            return {'FINISHED'}
        except Exception as exc:
            LOG.error(f"直出失败: {exc}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            TimerUtils.print_summary()
            LOG.stop_collecting()


def execute_direct_export(context, tree):
    BlueprintExportHelper.set_runtime_blueprint_tree(tree)
    session = DirectExportSession(context=context, tree=tree)
    session.run()


classes = (
    SSMT_OT_GenerateDirectModBlueprint,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
