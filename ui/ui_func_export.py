import bpy
import os

from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..utils.command_utils import CommandUtils
from ..utils.log_utils import LOG

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName

from .universal.efmi import ExportEFMI
from .universal.gimi import ExportGIMI
from .universal.himi import ExportHIMI
from .universal.identityv import ExportIdentityV
from .universal.snowbreak import ExportSnowBreak
from .universal.srmi import ExportSRMI
from .universal.unity import ExportUnity
from .wwmi.wwmi_export import ExportWWMI
from .universal.yysls import ExportYYSLS
from .universal.zzmi import ExportZZMI

from ..blueprint.model import BluePrintModel
from ..blueprint.export_helper import BlueprintExportHelper
from ..blueprint.preprocess import PreProcessHelper
from ..blueprint.preprocess_parallel import ParallelPreprocessCoordinator
from ..blueprint.export_parallel import ExportRoundExecutor, ParallelExportCoordinator, ParallelExportError
from ..common.global_properties import GlobalProterties


class SSMTGenerateModBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.generate_mod_blueprint"
    bl_label = TR.translate("生成Mod")
    bl_description = "根据当前工作空间对应的蓝图架构生成对应的Mod文件"
    bl_options = {'REGISTER','UNDO'}

    _POSTPROCESS_NODE_PREFIX = 'SSMTNode_PostProcess_'

    def invoke(self, context, event):
        tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not tree:
            return self.execute(context)

        has_postprocess = self._has_postprocess_nodes(tree)

        if has_postprocess:
            mod_export_path = GlobalConfig.path_generate_mod_folder()
            if mod_export_path and os.path.exists(mod_export_path):
                if os.listdir(mod_export_path):
                    self._export_path = mod_export_path
                    return context.window_manager.invoke_props_dialog(self, width=400)

        return self.execute(context)

    def draw(self, context):
        layout = self.layout
        layout.label(text="⚠️ 导出目录不为空！", icon='ERROR')
        layout.separator()
        layout.label(text=f"路径: {getattr(self, '_export_path', '未知')}")
        layout.label(text="检测到后处理节点，继续导出可能会覆盖现有文件。")
        layout.separator()
        layout.label(text="是否继续导出？")

    def _has_postprocess_nodes(self, tree) -> bool:
        for node in tree.nodes:
            if node.bl_idname.startswith(self._POSTPROCESS_NODE_PREFIX) and not node.mute:
                return True
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                blueprint_name = getattr(node, 'blueprint_name', '')
                if blueprint_name and blueprint_name != 'NONE':
                    nested_tree = bpy.data.node_groups.get(blueprint_name)
                    if nested_tree and self._has_postprocess_nodes(nested_tree):
                        return True
        return False

    def execute(self, context):
        LOG.start_collecting()
        
        TimerUtils.start_session("Mod导出")

        TimerUtils.start_stage("蓝图验证")
        tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图，请在蓝图编辑器中点击 Generate Mod")
            return {'CANCELLED'}

        BlueprintExportHelper.set_runtime_blueprint_tree(tree)
        TimerUtils.end_stage("蓝图验证")

        BluePrintModel.clear_object_name_mapping()

        LOG.info("=" * 60)
        LOG.info("🚀 开始生成Mod")
        LOG.info("=" * 60)

        has_shapekey_export = BlueprintExportHelper.has_shapekey_postprocess_node(tree)
        max_shapekey_slot = 0
        if has_shapekey_export:
            max_shapekey_slot = BlueprintExportHelper.calculate_max_shapekey_slot_count(tree)
            if max_shapekey_slot == 0:
                has_shapekey_export = False
                LOG.info("   未检测到形态键，跳过形态键导出")

        max_multi_file_count = BlueprintExportHelper.calculate_max_export_count(tree)
        has_multi_file_export = BlueprintExportHelper.has_multi_file_export_nodes()

        if has_shapekey_export:
            LOG.info("")
            LOG.info("🎭 形态键导出模式")
            LOG.info("-" * 40)
            LOG.info(f"   检测到形态键配置后处理节点")
            LOG.info(f"   最大形态键槽位: {max_shapekey_slot}")
            LOG.info(f"   形态键导出次数: {max_shapekey_slot + 1} 次 (基态 + {max_shapekey_slot} 个槽位)")

        if has_multi_file_export:
            LOG.info("")
            LOG.info("📦 多文件导出模式")
            LOG.info("-" * 40)
            LOG.info(f"   检测到 {len(BlueprintExportHelper.multi_file_export_nodes)} 个多文件导出节点")
            LOG.info(f"   多文件导出次数: {max_multi_file_count} 次")
            for node in BlueprintExportHelper.multi_file_export_nodes:
                obj_list = getattr(node, 'object_list', [])
                LOG.info(f"   - 节点 '{node.name}': {len(obj_list)} 个物体")

        LOG.info("")
        LOG.info("📍 第一阶段：检测与准备")
        LOG.info("-" * 40)

        export_success = False
        error_message = None
        buffer_sizes = []
        blueprint_model = None
        export_plan = []

        try:
            export_plan = self._build_export_round_plan(
                has_shapekey_export=has_shapekey_export,
                max_shapekey_slot=max_shapekey_slot,
                has_multi_file_export=has_multi_file_export,
                max_multi_file_count=max_multi_file_count,
            )
            self._log_export_plan(export_plan)

            if has_shapekey_export:
                BlueprintExportHelper.collect_shapekey_objects(tree)
                LOG.info(f"   收集到 {len(BlueprintExportHelper.shapekey_objects)} 个形态键物体")

            if len(export_plan) == 1:
                LOG.info("")
                LOG.info("📍 第二阶段：单次导出")
                LOG.info("-" * 40)

                round_result = self._execute_main_round(tree, export_plan[0])
                blueprint_model = round_result["blueprint_model"]
                reference_size = round_result["buffer_size"]
                buffer_sizes.append(reference_size)

            else:
                first_round = export_plan[0]
                middle_rounds = export_plan[1:-1]
                last_round = export_plan[-1]

                LOG.info("")
                LOG.info("📍 第二阶段：主线程首轮导出")
                LOG.info("-" * 40)

                first_result = self._execute_main_round(tree, first_round)
                blueprint_model = first_result["blueprint_model"]
                reference_size = first_result["buffer_size"]
                buffer_sizes.append(reference_size)

                if middle_rounds:
                    if GlobalProterties.enable_parallel_export_rounds():
                        LOG.info("")
                        LOG.info("📍 第三阶段：并行中间轮次导出")
                        LOG.info("-" * 40)
                        middle_results = ParallelExportCoordinator.execute_middle_rounds(tree, middle_rounds)
                        middle_results.sort(key=lambda item: item.get("round_index", 0))
                        for result in middle_results:
                            self._validate_buffer_size(reference_size, result["buffer_size"], result["round_index"], result["buffer_folder_name"])
                            buffer_sizes.append(result["buffer_size"])
                    else:
                        LOG.info("")
                        LOG.info("📍 第三阶段：串行中间轮次导出")
                        LOG.info("-" * 40)
                        for round_plan in middle_rounds:
                            round_result = self._execute_main_round(tree, round_plan)
                            self._validate_buffer_size(reference_size, round_result["buffer_size"], round_plan["round_index"], round_plan["buffer_folder_name"])
                            buffer_sizes.append(round_result["buffer_size"])

                LOG.info("")
                LOG.info("📍 第四阶段：主线程尾轮导出")
                LOG.info("-" * 40)
                last_result = self._execute_main_round(tree, last_round)
                blueprint_model = last_result["blueprint_model"]
                self._validate_buffer_size(reference_size, last_result["buffer_size"], last_round["round_index"], last_round["buffer_folder_name"])
                buffer_sizes.append(last_result["buffer_size"])

            LOG.info("")
            LOG.info("📍 第五阶段：后处理")
            LOG.info("-" * 40)
            TimerUtils.start_stage("后处理节点")
            mod_export_path = GlobalConfig.path_generate_mod_folder()
            blueprint_model.execute_postprocess_nodes(mod_export_path)
            TimerUtils.end_stage("后处理节点")
            export_success = True

        except Exception as e:
            error_message = f"导出失败: {str(e)}"
            print(f"❌ 导出过程中发生错误: {e}")
            import traceback
            traceback.print_exc()

            PreProcessHelper.cleanup_copies()

        LOG.info("")
        LOG.info("📍 清理阶段")
        LOG.info("-" * 40)

        if has_shapekey_export:
            BlueprintExportHelper.set_all_shapekey_values(0)
            LOG.info("   所有形态键已归零")

        LOG.info("")
        LOG.info("=" * 60)
        if export_success:
            total_rounds = len(export_plan) if export_plan else 1
            if has_shapekey_export and has_multi_file_export:
                LOG.info(f"✅ 形态键+多文件导出完成! 共导出 {total_rounds} 轮")
            elif has_shapekey_export:
                LOG.info(f"✅ 形态键导出完成! 共导出 {total_rounds} 轮")
            elif has_multi_file_export:
                LOG.info(f"✅ 多文件导出完成! 共导出 {total_rounds} 轮")
            else:
                LOG.info("✅ Mod生成完成!")
            LOG.info("=" * 60)
            self.report({'INFO'},TR.translate("Generate Mod Success!"))
            CommandUtils.OpenGeneratedModFolder()
        else:
            LOG.info(f"❌ Mod生成失败: {error_message}")
            LOG.info("=" * 60)
            self.report({'ERROR'}, error_message)

        TimerUtils.print_summary()

        LOG.stop_collecting()
        log_name = LOG.save_to_text_editor()
        if log_name:
            print(f"📄 导出日志已保存至文本编辑器: {log_name}")

        return {'FINISHED'}

    def _build_export_round_plan(self, has_shapekey_export: bool, max_shapekey_slot: int,
                                 has_multi_file_export: bool, max_multi_file_count: int) -> list:
        export_plan = []
        round_index = 0

        if has_shapekey_export:
            for slot_index in range(max_shapekey_slot + 1):
                round_index += 1
                if slot_index == 0:
                    export_plan.append({
                        "round_index": round_index,
                        "phase": "shapekey",
                        "description": "形态键基态",
                        "buffer_folder_name": "Meshes0000",
                        "export_index": 1,
                        "generate_ini": True,
                        "generate_classification_report": True,
                        "shapekey_mode": "all_zero",
                        "shapekey_slot_index": None,
                    })
                else:
                    export_plan.append({
                        "round_index": round_index,
                        "phase": "shapekey",
                        "description": f"形态键槽位 {slot_index}",
                        "buffer_folder_name": f"Meshes1{slot_index:03d}",
                        "export_index": 1,
                        "generate_ini": False,
                        "generate_classification_report": False,
                        "shapekey_mode": "slot",
                        "shapekey_slot_index": slot_index,
                    })

        if has_multi_file_export:
            for multi_index in range(1, max_multi_file_count + 1):
                round_index += 1
                export_plan.append({
                    "round_index": round_index,
                    "phase": "multifile",
                    "description": f"多文件索引 {multi_index}",
                    "buffer_folder_name": f"Meshes{multi_index:02d}",
                    "export_index": multi_index,
                    "generate_ini": (not has_shapekey_export and multi_index == 1),
                    "generate_classification_report": False,
                    "shapekey_mode": "all_zero" if has_shapekey_export else "unchanged",
                    "shapekey_slot_index": None,
                })

        if not export_plan:
            export_plan.append({
                "round_index": 1,
                "phase": "single",
                "description": "单次导出",
                "buffer_folder_name": "Meshes",
                "export_index": 1,
                "generate_ini": True,
                "generate_classification_report": False,
                "shapekey_mode": "unchanged",
                "shapekey_slot_index": None,
            })

        return export_plan

    def _log_export_plan(self, export_plan: list):
        LOG.info("")
        LOG.info("🗂️ 导出轮次计划")
        LOG.info("-" * 40)
        for round_plan in export_plan:
            flags = []
            if round_plan.get("generate_ini"):
                flags.append("生成INI")
            if round_plan.get("generate_classification_report"):
                flags.append("分类报告")
            if not flags:
                flags.append("仅Buffer")
            LOG.info(
                f"   第 {round_plan['round_index']} 轮: {round_plan['description']} -> {round_plan['buffer_folder_name']} ({' / '.join(flags)})"
            )

    def _execute_main_round(self, tree, round_plan: dict) -> dict:
        LOG.info("")
        LOG.info(f"🔄 第 {round_plan['round_index']} 轮导出: {round_plan['description']}")
        LOG.info(f"   Buffer 文件夹: {round_plan['buffer_folder_name']}")

        result = ExportRoundExecutor.execute_round(
            tree=tree,
            round_plan=round_plan,
            allow_parallel_preprocess=True,
        )

        LOG.info(f"   Buffer 文件夹大小: {result['buffer_size']} 字节")
        return result

    def _validate_buffer_size(self, reference_size: int, current_size: int, round_index: int, buffer_folder_name: str):
        if reference_size != current_size:
            raise ParallelExportError(
                f"第 {round_index} 轮导出的 Buffer 文件夹 {buffer_folder_name} 大小 ({current_size} 字节) 与首轮 ({reference_size} 字节) 不一致"
            )

    def _do_single_export(self, context, tree, buffer_folder_name, export_round, generate_ini=True):
        LOG.info(f"   正在解析蓝图...")

        TimerUtils.start_stage(f"蓝图解析_{export_round}")
        try:
            blueprint_model = BluePrintModel(tree=tree, context=context)
        except ValueError as error:
            LOG.error(f"   蓝图解析失败: {error}")
            return None
        TimerUtils.end_stage(f"蓝图解析_{export_round}")

        TimerUtils.start_stage(f"引用验证_{export_round}")
        self._validate_copy_references(blueprint_model)
        TimerUtils.end_stage(f"引用验证_{export_round}")

        LOG.info(f"📋 待导出物体: {len(blueprint_model.ordered_draw_obj_data_model_list)} 个")

        if generate_ini:
            TimerUtils.start_stage(f"数据导出_{export_round}")
            self._export_with_ini(blueprint_model)
            TimerUtils.end_stage(f"数据导出_{export_round}")
        else:
            TimerUtils.start_stage(f"缓冲文件生成_{export_round}")
            self._export_buffers_only(blueprint_model)
            TimerUtils.end_stage(f"缓冲文件生成_{export_round}")

        return blueprint_model

    def _export_with_ini(self, blueprint_model):
        if GlobalConfig.logic_name == LogicName.EFMI:
            ExportEFMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.GIMI:
            ExportGIMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.HIMI:
            ExportHIMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.IdentityV:
            ExportIdentityV(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.SRMI:
            ExportSRMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.ZZMI:
            ExportZZMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.WWMI:
            ExportWWMI(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            ExportSnowBreak(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name == LogicName.YYSLS:
            ExportYYSLS(blueprint_model=blueprint_model).export()
        elif GlobalConfig.logic_name in (LogicName.Naraka, LogicName.NarakaM, LogicName.GF2, LogicName.AILIMIT):
            ExportUnity(blueprint_model=blueprint_model).export()

    def _export_buffers_only(self, blueprint_model):
        if GlobalConfig.logic_name == LogicName.EFMI:
            ExportEFMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.GIMI:
            ExportGIMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.HIMI:
            ExportHIMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.IdentityV:
            ExportIdentityV(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.SRMI:
            ExportSRMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.ZZMI:
            ExportZZMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.WWMI:
            ExportWWMI(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            ExportSnowBreak(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name == LogicName.YYSLS:
            ExportYYSLS(blueprint_model=blueprint_model).export_buffers_only()
        elif GlobalConfig.logic_name in (LogicName.Naraka, LogicName.NarakaM, LogicName.GF2, LogicName.AILIMIT):
            ExportUnity(blueprint_model=blueprint_model).export_buffers_only()

    def _get_folder_size(self, folder_path: str) -> int:
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(folder_path):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.isfile(filepath):
                        total_size += os.path.getsize(filepath)
        except Exception as e:
            LOG.warning(f"计算文件夹大小失败: {e}")
        return total_size

    def _collect_object_names_from_tree(self, tree) -> list:
        object_names = []

        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Object_Info' and not node.mute:
                obj_name = getattr(node, 'object_name', '')
                if obj_name:
                    object_names.append(obj_name)

        nested_count = 0
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                nested_names = self._collect_nested_object_names(node)
                object_names.extend(nested_names)
                nested_count += len(nested_names)

        main_count = len(object_names) - nested_count
        LOG.info(f"📋 物体收集: {tree.name}(主蓝图) {main_count} 个, 嵌套蓝图 {nested_count} 个, 共 {len(object_names)} 个")
        return object_names

    def _collect_nested_object_names(self, nest_node) -> list:
        blueprint_name = getattr(nest_node, 'blueprint_name', '')
        if not blueprint_name or blueprint_name == 'NONE':
            return []

        nested_tree = bpy.data.node_groups.get(blueprint_name)
        if not nested_tree:
            return []

        object_names = []
        for node in nested_tree.nodes:
            if node.bl_idname == 'SSMTNode_Object_Info' and not node.mute:
                obj_name = getattr(node, 'object_name', '')
                if obj_name:
                    object_names.append(obj_name)

        return object_names

    def _collect_nested_trees(self, tree) -> list:
        nested_trees = []
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Blueprint_Nest' and not node.mute:
                blueprint_name = getattr(node, 'blueprint_name', '')
                if blueprint_name and blueprint_name != 'NONE':
                    nested_tree = bpy.data.node_groups.get(blueprint_name)
                    if nested_tree:
                        nested_trees.append(nested_tree)
        return nested_trees

    def _validate_copy_references(self, blueprint_model: BluePrintModel):
        if not PreProcessHelper.has_copies():
            return

        LOG.info(f"📋 验证物体引用是否为副本...")

        invalid_objects = []
        for chain in blueprint_model.processing_chains:
            if chain.is_valid:
                obj_name = chain.object_name
                if not PreProcessHelper.validate_copy_suffix(obj_name):
                    invalid_objects.append(obj_name)

        if invalid_objects:
            LOG.error(f"   ❌ 前处理错误：以下物体引用未更新为副本:")
            for obj_name in invalid_objects:
                LOG.error(f"      - {obj_name}")
            raise ValueError(f"前处理错误：物体引用未正确更新为副本，请检查前处理流程")

        LOG.info(f"   ✅ 所有物体引用已正确更新为副本")


def register():
    bpy.utils.register_class(SSMTGenerateModBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTGenerateModBlueprint)
