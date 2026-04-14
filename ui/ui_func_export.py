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
        LOG.info("📍 第一阶段：前处理")
        LOG.info("-" * 40)

        TimerUtils.start_stage("物体收集")
        object_names = self._collect_object_names_from_tree(tree)
        TimerUtils.end_stage("物体收集")

        TimerUtils.start_stage("副本创建")
        original_to_copy_map = PreProcessHelper.execute_preprocess(object_names)

        if original_to_copy_map:
            nested_trees = self._collect_nested_trees(tree)
            PreProcessHelper.update_blueprint_node_references(tree, nested_trees)
        TimerUtils.end_stage("副本创建")

        if has_shapekey_export:
            BlueprintExportHelper.collect_shapekey_objects(tree)

        export_success = False
        error_message = None
        buffer_sizes = []
        blueprint_model = None

        try:
            current_export_round = 0

            if has_shapekey_export:
                LOG.info("")
                LOG.info("📍 第二阶段：形态键导出")
                LOG.info("-" * 40)

                for slot_index in range(max_shapekey_slot + 1):
                    current_export_round += 1
                    
                    if slot_index == 0:
                        buffer_folder_name = "Meshes0000"
                        BlueprintExportHelper.set_all_shapekey_values(0)
                        LOG.info("")
                        LOG.info(f"🎭 第 {current_export_round} 轮导出: 基态 (所有形态键=0)")
                    else:
                        buffer_folder_name = f"Meshes1{slot_index:03d}"
                        BlueprintExportHelper.set_all_shapekey_values(0, slot_index)
                        LOG.info("")
                        LOG.info(f"🎭 第 {current_export_round} 轮导出: 槽位 {slot_index}")
                    
                    LOG.info(f"   Buffer 文件夹: {buffer_folder_name}")
                    
                    BlueprintExportHelper.set_current_buffer_folder_name(buffer_folder_name)
                    BlueprintExportHelper.set_current_export_index(1)

                    blueprint_model = self._do_single_export(
                        context, tree, buffer_folder_name, 
                        current_export_round, 
                        generate_ini=(current_export_round == 1)
                    )
                    
                    if blueprint_model is None:
                        error_message = "蓝图解析失败"
                        break

                    if current_export_round == 1:
                        BlueprintExportHelper.generate_shapekey_classification_report(blueprint_model)

                    mod_export_path = GlobalConfig.path_generate_mod_folder()
                    buffer_path = os.path.join(mod_export_path, buffer_folder_name)
                    
                    if os.path.exists(buffer_path):
                        buffer_size = self._get_folder_size(buffer_path)
                        buffer_sizes.append(buffer_size)
                        LOG.info(f"   Buffer 文件夹大小: {buffer_size} 字节")
                        
                        if len(buffer_sizes) > 1:
                            if buffer_size != buffer_sizes[0]:
                                error_message = f"形态键导出第 {current_export_round} 轮的 Buffer 文件夹大小 ({buffer_size} 字节) 与第一轮 ({buffer_sizes[0]} 字节) 不一致！"
                                LOG.error(f"❌ {error_message}")
                                break

                if error_message:
                    pass
                elif has_multi_file_export:
                    LOG.info("")
                    LOG.info("📍 第三阶段：多文件导出")
                    LOG.info("-" * 40)

                    BlueprintExportHelper.set_all_shapekey_values(0)

                    for multi_index in range(1, max_multi_file_count + 1):
                        current_export_round += 1
                        
                        buffer_folder_name = f"Meshes{multi_index:02d}"
                        LOG.info("")
                        LOG.info(f"📦 第 {current_export_round} 轮导出: 多文件索引 {multi_index}")
                        LOG.info(f"   Buffer 文件夹: {buffer_folder_name}")
                        
                        BlueprintExportHelper.set_current_buffer_folder_name(buffer_folder_name)
                        BlueprintExportHelper.set_current_export_index(multi_index)
                        
                        obj_info = BlueprintExportHelper.get_multi_file_export_object_info(multi_index - 1)
                        for node_name, info in obj_info.items():
                            LOG.info(f"   节点 '{node_name}' → 物体: {info.get('object_name', 'N/A')}")

                        is_final_export = (multi_index == max_multi_file_count)
                        
                        blueprint_model = self._do_single_export(
                            context, tree, buffer_folder_name,
                            current_export_round,
                            generate_ini=False
                        )
                        
                        if blueprint_model is None:
                            error_message = "蓝图解析失败"
                            break

                        mod_export_path = GlobalConfig.path_generate_mod_folder()
                        buffer_path = os.path.join(mod_export_path, buffer_folder_name)
                        
                        if os.path.exists(buffer_path):
                            buffer_size = self._get_folder_size(buffer_path)
                            buffer_sizes.append(buffer_size)
                            LOG.info(f"   Buffer 文件夹大小: {buffer_size} 字节")
                            
                            if len(buffer_sizes) > 1:
                                if buffer_size != buffer_sizes[0]:
                                    error_message = f"多文件导出第 {current_export_round} 轮的 Buffer 文件夹大小 ({buffer_size} 字节) 与第一轮 ({buffer_sizes[0]} 字节) 不一致！"
                                    LOG.error(f"❌ {error_message}")
                                    break

                if not error_message:
                    LOG.info("")
                    LOG.info("📍 第四阶段：后处理")
                    LOG.info("-" * 40)
                    LOG.info(f"   所有导出完成，开始执行后处理")

                    TimerUtils.start_stage("后处理节点")
                    mod_export_path = GlobalConfig.path_generate_mod_folder()
                    blueprint_model.execute_postprocess_nodes(mod_export_path)
                    TimerUtils.end_stage("后处理节点")
                    export_success = True

                else:
                    LOG.info("")
                    LOG.info("📍 第三阶段：最终导出")
                    LOG.info("-" * 40)
                    
                    buffer_folder_name = "Meshes"
                    LOG.info(f"   Buffer 文件夹: {buffer_folder_name}")
                    
                    BlueprintExportHelper.set_current_buffer_folder_name(buffer_folder_name)
                    BlueprintExportHelper.set_current_export_index(1)
                    BlueprintExportHelper.set_all_shapekey_values(0)

                    blueprint_model = self._do_single_export(
                        context, tree, buffer_folder_name,
                        current_export_round + 1,
                        generate_ini=False
                    )
                    
                    if blueprint_model is None:
                        error_message = "蓝图解析失败"
                    else:
                        mod_export_path = GlobalConfig.path_generate_mod_folder()
                        buffer_path = os.path.join(mod_export_path, buffer_folder_name)
                        
                        if os.path.exists(buffer_path):
                            buffer_size = self._get_folder_size(buffer_path)
                            buffer_sizes.append(buffer_size)
                            LOG.info(f"   Buffer 文件夹大小: {buffer_size} 字节")
                            
                            if len(buffer_sizes) > 1:
                                if buffer_size != buffer_sizes[0]:
                                    error_message = f"最终导出的 Buffer 文件夹大小 ({buffer_size} 字节) 与第一轮 ({buffer_sizes[0]} 字节) 不一致！"
                                    LOG.error(f"❌ {error_message}")
                        
                        if not error_message:
                            LOG.info("")
                            LOG.info("📍 第四阶段：后处理")
                            LOG.info("-" * 40)
                            LOG.info(f"   所有导出完成，开始执行后处理")

                            TimerUtils.start_stage("后处理节点")
                            mod_export_path = GlobalConfig.path_generate_mod_folder()
                            blueprint_model.execute_postprocess_nodes(mod_export_path)
                            TimerUtils.end_stage("后处理节点")
                            export_success = True

            elif has_multi_file_export:
                LOG.info("")
                LOG.info("📍 第二阶段：多文件导出")
                LOG.info("-" * 40)

                for multi_index in range(1, max_multi_file_count + 1):
                    current_export_round += 1
                    
                    buffer_folder_name = f"Meshes{multi_index:02d}"
                    LOG.info("")
                    LOG.info(f"📦 第 {current_export_round} 轮导出")
                    LOG.info(f"   Buffer 文件夹: {buffer_folder_name}")
                    
                    BlueprintExportHelper.set_current_buffer_folder_name(buffer_folder_name)
                    BlueprintExportHelper.set_current_export_index(multi_index)
                    
                    obj_info = BlueprintExportHelper.get_multi_file_export_object_info(multi_index - 1)
                    for node_name, info in obj_info.items():
                        LOG.info(f"   节点 '{node_name}' → 物体: {info.get('object_name', 'N/A')}")

                    is_first_export = (multi_index == 1)
                    
                    blueprint_model = self._do_single_export(
                        context, tree, buffer_folder_name,
                        current_export_round,
                        generate_ini=is_first_export
                    )
                    
                    if blueprint_model is None:
                        error_message = "蓝图解析失败"
                        break

                    mod_export_path = GlobalConfig.path_generate_mod_folder()
                    buffer_path = os.path.join(mod_export_path, buffer_folder_name)
                    
                    if os.path.exists(buffer_path):
                        buffer_size = self._get_folder_size(buffer_path)
                        buffer_sizes.append(buffer_size)
                        LOG.info(f"   Buffer 文件夹大小: {buffer_size} 字节")
                        
                        if len(buffer_sizes) > 1:
                            if buffer_size != buffer_sizes[0]:
                                error_message = f"第 {current_export_round} 轮导出的 Buffer 文件夹大小 ({buffer_size} 字节) 与第一轮 ({buffer_sizes[0]} 字节) 不一致！"
                                LOG.error(f"❌ {error_message}")
                                break

                if not error_message:
                    LOG.info("")
                    LOG.info("📍 第三阶段：后处理")
                    LOG.info("-" * 40)
                    LOG.info(f"   所有 {max_multi_file_count} 轮导出完成，开始执行后处理")

                    TimerUtils.start_stage("后处理节点")
                    mod_export_path = GlobalConfig.path_generate_mod_folder()
                    blueprint_model.execute_postprocess_nodes(mod_export_path)
                    TimerUtils.end_stage("后处理节点")
                    export_success = True

            else:
                LOG.info("")
                LOG.info("📍 第二阶段：单次导出")
                LOG.info("-" * 40)

                buffer_folder_name = "Meshes"
                BlueprintExportHelper.set_current_buffer_folder_name(buffer_folder_name)
                BlueprintExportHelper.set_current_export_index(1)

                blueprint_model = self._do_single_export(
                    context, tree, buffer_folder_name,
                    1,
                    generate_ini=True
                )
                
                if blueprint_model is None:
                    error_message = "蓝图解析失败"
                else:
                    LOG.info("")
                    LOG.info("📍 第三阶段：后处理")
                    LOG.info("-" * 40)

                    TimerUtils.start_stage("后处理节点")
                    mod_export_path = GlobalConfig.path_generate_mod_folder()
                    blueprint_model.execute_postprocess_nodes(mod_export_path)
                    TimerUtils.end_stage("后处理节点")
                    export_success = True

        except Exception as e:
            LOG.error(f"❌ 导出过程中发生错误: {e}")
            error_message = f"导出失败: {str(e)}"
            import traceback
            traceback.print_exc()

        LOG.info("")
        LOG.info("📍 清理阶段")
        LOG.info("-" * 40)

        if has_shapekey_export:
            BlueprintExportHelper.set_all_shapekey_values(0)
            LOG.info("   所有形态键已归零")

        TimerUtils.start_stage("资源清理")
        PreProcessHelper.cleanup_copies()
        TimerUtils.end_stage("资源清理")

        LOG.info("")
        LOG.info("=" * 60)
        if export_success:
            if has_shapekey_export and has_multi_file_export:
                total_rounds = max_shapekey_slot + 1 + max_multi_file_count
                LOG.info(f"✅ 形态键+多文件导出完成! 共导出 {total_rounds} 轮")
            elif has_shapekey_export:
                total_rounds = max_shapekey_slot + 2
                LOG.info(f"✅ 形态键导出完成! 共导出 {total_rounds} 轮")
            elif has_multi_file_export:
                LOG.info(f"✅ 多文件导出完成! 共导出 {max_multi_file_count} 轮")
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

        return {'FINISHED'}

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
