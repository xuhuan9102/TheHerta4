import bpy
import os

from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR
from ..utils.command_utils import CommandUtils
from ..utils.log_utils import LOG

from ..common.global_config import GlobalConfig

from ..blueprint.model import BluePrintModel
from ..blueprint.direct_export import execute_direct_export, has_direct_export_mode
from ..blueprint.export_helper import BlueprintExportHelper
from ..blueprint.preprocess import PreProcessHelper
from ..blueprint.export_parallel import ExportRoundExecutor, ParallelExportCoordinator, ParallelExportError
from ..common.global_properties import GlobalProterties


def _raise_for_unknown_logic_name() -> None:
    raise ValueError(
        "当前游戏预设未加载或不受支持，未执行任何主导出逻辑。"
        f" 当前 logic_name='{GlobalConfig.logic_name}'，请先确认全局设置中的游戏预设已正确加载。"
    )


class SSMTGenerateModBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.generate_mod_blueprint"
    bl_label = TR.translate("生成Mod")
    bl_description = "根据当前工作空间对应的蓝图架构生成对应的Mod文件"
    bl_options = {'REGISTER','UNDO'}

    _POSTPROCESS_NODE_PREFIX = 'SSMTNode_PostProcess_'

    blueprint_name: bpy.props.StringProperty(
        name="Blueprint Name",
        default="",
        options={'SKIP_SAVE'},
    ) # type: ignore

    def _resolve_target_tree(self, context):
        requested_tree_name = str(getattr(self, "blueprint_name", "") or "").strip()
        if requested_tree_name:
            tree = BlueprintExportHelper.get_selected_blueprint_tree(
                selected_name=requested_tree_name,
                context=context,
            )
        else:
            tree = BlueprintExportHelper.get_current_blueprint_tree(context=context)

        global_properties = getattr(getattr(context, "scene", None), "global_properties", None)
        if tree and global_properties and getattr(global_properties, "selected_blueprint_name", "") != tree.name:
            global_properties.selected_blueprint_name = tree.name

        return tree

    def invoke(self, context, event):
        tree = self._resolve_target_tree(context)
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
        GlobalConfig.read_from_main_json_ssmt4()
        
        TimerUtils.start_session("Mod导出")

        TimerUtils.start_stage("蓝图验证")
        tree = self._resolve_target_tree(context)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图，请在蓝图编辑器中点击 Generate Mod")
            return {'CANCELLED'}

        BlueprintExportHelper.set_runtime_blueprint_tree(tree)
        TimerUtils.end_stage("蓝图验证")

        BluePrintModel.clear_object_name_mapping()

        LOG.info("=" * 60)
        LOG.info("🚀 开始生成Mod")
        LOG.info("=" * 60)

        # 直出模式复用原有按钮入口，避免用户在标准导出和直出之间来回切换。
        if has_direct_export_mode(tree):
            LOG.info("⚡ 检测到直出模式，切换到直出导出流程")

            export_success = False
            error_message = None

            try:
                execute_direct_export(context=context, tree=tree)
                export_success = True
            except Exception as e:
                error_message = f"直出失败: {str(e)}"
                print(f"❌ 直出过程中发生错误: {e}")
                import traceback
                traceback.print_exc()
                PreProcessHelper.cleanup_copies(silent=True)

            LOG.info("")
            LOG.info("📍 清理阶段")
            LOG.info("-" * 40)
            PreProcessHelper.cleanup_copies()

            LOG.info("")
            LOG.info("=" * 60)
            if export_success:
                LOG.info("✅ 直出完成!")
                LOG.info("=" * 60)
                self.report({'INFO'}, TR.translate("Generate Mod Success!"))
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

            if export_success:
                return {'FINISHED'}
            return {'CANCELLED'}

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

            PreProcessHelper.cleanup_copies(silent=True)

        LOG.info("")
        LOG.info("📍 清理阶段")
        LOG.info("-" * 40)

        PreProcessHelper.cleanup_copies()

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

class SSMTQuickExportSelected(bpy.types.Operator):
    bl_idname = "ssmt.quick_export_selected"
    bl_label = TR.translate("快速局部导出")
    bl_description = "无视蓝图，直接导出当前选中的网格物体"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        selected_meshes = [obj for obj in context.selected_objects if obj.type == 'MESH']
        if not selected_meshes:
            self.report({'WARNING'}, "请先选择至少一个网格物体")
            return {'CANCELLED'}

        GlobalConfig.read_from_main_json_ssmt4()

        temp_tree = None
        previous_runtime_tree_name = BlueprintExportHelper.runtime_blueprint_tree_name
        previous_export_index = BlueprintExportHelper.current_export_index
        previous_buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        export_success = False

        try:
            LOG.start_collecting()
            TimerUtils.start_session("快速局部导出")

            LOG.info("=" * 60)
            LOG.info("🚀 开始快速局部导出")
            LOG.info(f"📋 当前选择网格物体: {len(selected_meshes)} 个")
            for obj in selected_meshes:
                LOG.info(f"   - {obj.name}")

            temp_tree = self._build_temp_blueprint_tree(selected_meshes)

            round_plan = {
                "round_index": 1,
                "phase": "single",
                "description": "快速局部导出",
                "buffer_folder_name": "Meshes",
                "export_index": 1,
                "generate_ini": True,
                "generate_classification_report": False,
                "shapekey_mode": "unchanged",
                "shapekey_slot_index": None,
            }

            result = ExportRoundExecutor.execute_round(
                tree=temp_tree,
                round_plan=round_plan,
                allow_parallel_preprocess=True,
            )

            export_success = True
            self.report({'INFO'}, f"快速局部导出完成，共导出 {result['object_count']} 个物体")
            CommandUtils.OpenGeneratedModFolder()
            return {'FINISHED'}
        except Exception as e:
            print(f"❌ 快速局部导出失败: {e}")
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, f"快速局部导出失败: {e}")
            return {'CANCELLED'}
        finally:
            BlueprintExportHelper.runtime_blueprint_tree_name = previous_runtime_tree_name
            BlueprintExportHelper.set_current_export_index(previous_export_index)
            BlueprintExportHelper.set_current_buffer_folder_name(previous_buffer_folder_name)

            if temp_tree and bpy.data.node_groups.get(temp_tree.name):
                bpy.data.node_groups.remove(temp_tree, do_unlink=True)

            TimerUtils.print_summary()

            LOG.stop_collecting()
            if export_success:
                log_name = LOG.save_to_text_editor()
                if log_name:
                    print(f"📄 快速局部导出日志已保存至文本编辑器: {log_name}")

    def _build_temp_blueprint_tree(self, objects):
        tree_name = f"SSMT_QuickExport_{len(bpy.data.node_groups) + 1}"
        tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')

        group_node = tree.nodes.new('SSMTNode_Object_Group')
        group_node.label = "Quick Export"
        group_node.location = (300, 0)

        output_node = tree.nodes.new('SSMTNode_Result_Output')
        output_node.label = "Quick Export"
        output_node.location = (650, 0)
        tree.links.new(group_node.outputs[0], output_node.inputs[0])

        current_y = 0
        for obj in objects:
            node = tree.nodes.new('SSMTNode_Object_Info')
            node.object_name = obj.name
            node.object_id = str(obj.as_pointer())
            node.label = obj.name
            node.location = (0, current_y)
            current_y -= 180

            if group_node.inputs[-1].is_linked:
                group_node.inputs.new('SSMTSocketObject', f"Input {len(group_node.inputs) + 1}")

            tree.links.new(node.outputs[0], group_node.inputs[-1])

        group_node.location = (300, max(0, (len(objects) - 1) * -90))
        output_node.location = (650, group_node.location.y)
        return tree


def register():
    bpy.utils.register_class(SSMTQuickExportSelected)
    bpy.utils.register_class(SSMTGenerateModBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMTQuickExportSelected)
    bpy.utils.unregister_class(SSMTGenerateModBlueprint)
