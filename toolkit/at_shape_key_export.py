# -*- coding: utf-8 -*-

import bpy
import os
import shutil
import time
import traceback
from collections import defaultdict


class SKE_AutomationPipeline:
    """形态键批量导出自动化流程控制器"""

    def __init__(self, props, context):
        self.props = props
        self.context = context
        self.scene = self.context.scene
        self.target_objects = []
        self.max_shape_key_slots = 0

    def _wait_for_step_delay(self):
        """步骤间暂停"""
        if self.props.auto_step_delay_seconds > 0:
            time.sleep(self.props.auto_step_delay_seconds)

    def _collect_objects_from_node_tree(self, node_tree_name, depth=0):
        """从节点树中收集所有物体（支持嵌套蓝图）"""
        tree = bpy.data.node_groups.get(node_tree_name)
        if not tree:
            raise RuntimeError(f"找不到节点树 '{node_tree_name}'")

        checked_nodes = set()

        def collect_objects_from_node(node, current_tree_name):
            if node.name in checked_nodes:
                return
            checked_nodes.add(node.name)

            if node.bl_idname == 'SSMTNode_Object_Info':
                obj_name = getattr(node, 'object_name', '')
                if obj_name:
                    obj = bpy.data.objects.get(obj_name)
                    if obj and obj.type == 'MESH' and obj.data and obj not in self.target_objects:
                        self.target_objects.append(obj)
            
            elif node.bl_idname == 'SSMTNode_Blueprint_Nest':
                nested_tree_name = getattr(node, 'blueprint_name', '')
                if nested_tree_name and nested_tree_name != 'NONE':
                    indent = "  " * (depth + 1)
                    print(f"{indent}[Nest] 发现嵌套蓝图: '{nested_tree_name}'")
                    self._collect_objects_from_node_tree(nested_tree_name, depth + 1)

            if hasattr(node, "inputs"):
                for inp in node.inputs:
                    if inp.is_linked:
                        for link in inp.links:
                            collect_objects_from_node(link.from_node, current_tree_name)

        output_node = None
        for node in tree.nodes:
            if node.bl_idname == 'SSMTNode_Result_Output':
                output_node = node
                break

        if not output_node:
            raise RuntimeError(f"在节点树 '{node_tree_name}' 中未找到输出节点 (SSMTNode_Result_Output)")

        collect_objects_from_node(output_node, node_tree_name)

        if depth == 0 and not self.target_objects:
            raise RuntimeError(f"在节点树 '{node_tree_name}' 中没有找到任何网格物体。")

        if depth == 0:
            print(f"  [Init] 从节点树 '{node_tree_name}' 中找到 {len(self.target_objects)} 个物体。")

    def select_objects_from_node_tree(self, node_tree_name):
        """选中节点树中的所有物体"""
        try:
            bpy.ops.object.select_all(action='DESELECT')

            if not self.target_objects:
                print(f"  [Select] 警告: 未找到可选择的物体。")
                return

            valid_objects = [obj for obj in self.target_objects if obj.name in self.context.view_layer.objects]
            if not valid_objects:
                print(f"  [Select] 警告: 目标物体在当前视图层不可用。")
                return

            for obj in valid_objects:
                obj.select_set(True)
            self.context.view_layer.objects.active = valid_objects[0]

            print(f"  [Select] 已选中 {len(valid_objects)} 个物体。")

        except Exception as e:
            print(f"  [Select] 错误: 选择物体时出错: {e}")

    def cleanup_export_files(self, new_buffer_name):
        """
        [修改版] 仅重命名导出的 Buffer 文件夹到目标名称。
        不再删除 .ini 文件，也不清理 Buffer 内部文件。
        """
        print(f"  -- [File Ops] 重命名导出文件夹为 ({new_buffer_name}) --")
        export_path = bpy.path.abspath(self.props.auto_export_base_path)
        if not os.path.isdir(export_path):
            print(f"    [Error] 导出目录 '{export_path}' 不存在！")
            return

        original_buffer_path = os.path.join(export_path, "Buffer")
        
        if os.path.isdir(original_buffer_path):
            new_buffer_path = os.path.join(export_path, new_buffer_name)
            
            if os.path.exists(new_buffer_path):
                print(f"    [Warning] 目标文件夹 '{new_buffer_name}' 已存在，正在覆盖。")
                shutil.rmtree(new_buffer_path)
            
            try:
                os.rename(original_buffer_path, new_buffer_path)
                print(f"    [File Ops] 成功重命名: 'Buffer' -> '{new_buffer_name}'")
            except OSError as e:
                print(f"    [Error] 重命名文件夹失败: {e}")
        else:
            print(f"    [Warning] 未在 '{export_path}' 中找到 'Buffer' 文件夹。")

    def set_all_shape_keys(self, value):
        """将所有目标物体上的所有形态键值设为指定值"""
        for obj in self.target_objects:
            if obj.data and obj.data.shape_keys:
                for kb in obj.data.shape_keys.key_blocks:
                    if kb != obj.data.shape_keys.reference_key:
                        kb.value = value
    
    def _safe_save_mainfile(self):
        """安全保存工程文件，自动处理损坏的形态键等无效数据"""
        try:
            bpy.ops.wm.save_mainfile()
            print("  [Save] 工程文件已保存。")
            return True
        except RuntimeError as e:
            err_msg = str(e)
            if "无效的" in err_msg or "invalid" in err_msg.lower() or "指针" in err_msg:
                print(f"  [Save] 检测到损坏数据，正在自动清理...")
                print(f"  [Save] 错误详情: {err_msg}")

                for obj in list(bpy.data.objects):
                    if not obj.data:
                        continue
                    sk = getattr(obj.data, 'shape_keys', None)
                    if not sk:
                        continue
                    keys_to_remove = []
                    for kb in sk.key_blocks:
                        try:
                            _test = kb.name
                            _test = kb.value
                            _test = kb.mute
                            _test = kb.slider_min
                            _test = kb.slider_max
                        except Exception:
                            keys_to_remove.append(kb)

                    for kb in keys_to_remove:
                        try:
                            obj.shape_key_remove(kb)
                            print(f"    [Clean] 已移除损坏形态键 '{kb.name}' (物体: {obj.name})")
                        except Exception as clean_err:
                            print(f"    [Clean] 移除失败 '{kb.name}': {clean_err}")

                try:
                    bpy.ops.outliner.orphans_purge(do_recursive=True)
                    print("  [Clean] 已清理孤立数据。")
                except Exception:
                    pass

                try:
                    bpy.ops.wm.save_mainfile()
                    print("  [Save] 清理后工程文件已保存。")
                    return True
                except RuntimeError as retry_err:
                    print(f"  [Save] 清理后仍然无法保存: {retry_err}")
                    raise
            else:
                raise

    def _classify_and_write_shape_keys(self):
        """分类所有物体的形态键，并写入到文本编辑器"""
        classification_data = defaultdict(lambda: defaultdict(list))

        for obj in self.target_objects:
            if obj.hide_viewport or obj.hide_render or obj.hide_get():
                continue
            
            if obj.data and obj.data.shape_keys:
                for i, kb in enumerate(obj.data.shape_keys.key_blocks):
                    if i == 0: continue
                    slot_index = i
                    shape_key_name = kb.name
                    classification_data[slot_index][shape_key_name].append(obj.name)
        
        if not classification_data:
            print("  [Classify] 未在任何物体上找到可分类的形态键。")
            return

        output_lines = ["# 自动化形态键导出 - 分类报告", time.ctime(), "="*40, ""]
        sorted_slots = sorted(classification_data.keys())

        for slot in sorted_slots:
            output_lines.append(f"槽位 {slot}:")
            sk_data = classification_data[slot]
            sorted_sk_names = sorted(sk_data.keys())
            
            for sk_name in sorted_sk_names:
                output_lines.append(f"  - 名称: {sk_name}")
                object_list = sk_data[sk_name]
                for obj_name in sorted(object_list):
                    output_lines.append(f"    - 物体: {obj_name}")
            output_lines.append("")

        final_text = "\n".join(output_lines)
        text_block_name = "Shape_Key_Classification"
        if text_block_name in bpy.data.texts:
            txt = bpy.data.texts[text_block_name]
            txt.clear()
        else:
            txt = bpy.data.texts.new(name=text_block_name)
        txt.write(final_text)
        print(f"  [Classify] 形态键分类报告已写入文本编辑器: '{text_block_name}'")

    def run(self):
        """执行主流程"""
        print("\n" + "=" * 40)
        print("   ATP 形态键导出自动化流程启动   ")
        print("=" * 40)

        start_time = time.time()

        try:
            print("--- [步骤 1] 初始化并准备物体 ---")
            
            self.target_objects.clear()
            self._collect_objects_from_node_tree(self.props.ske_node_tree_name)
            
            for obj in self.target_objects:
                if obj.data and obj.data.shape_keys:
                    num_keys = len(obj.data.shape_keys.key_blocks)
                    if num_keys > 1:
                        self.max_shape_key_slots = max(self.max_shape_key_slots, num_keys - 1)

            print(f"  [Init] 检测到最多 {self.max_shape_key_slots} 个形态键槽位。")
            self._wait_for_step_delay()

            print("\n--- [新增步骤] 分类形态键并输出报告 ---")
            self._classify_and_write_shape_keys()
            self._wait_for_step_delay()

            print("\n--- [步骤 2] 导出基础形态 (Buffer0000) ---")
            self.set_all_shape_keys(0)
            self.select_objects_from_node_tree(self.props.ske_node_tree_name)
            self._wait_for_step_delay()

            print("  [Save] 保存工程文件...")
            self._safe_save_mainfile()
            self._wait_for_step_delay()

            print("  [Export] 调用 SSMT 导出...")
            bpy.ops.ssmt.generate_mod_blueprint(node_tree_name=self.props.ske_node_tree_name)
            print("  [Export] 导出命令执行完毕。")
            self._wait_for_step_delay()

            self.cleanup_export_files("Buffer0000")
            self._wait_for_step_delay()

            print(f"\n--- [步骤 3] 循环导出 {self.max_shape_key_slots} 个变形形态 ---")
            if self.max_shape_key_slots == 0:
                print("  [Info] 未找到任何可导出的形态键，流程提前结束。")
            
            for i in range(self.max_shape_key_slots):
                slot_index = i + 1
                loop_start_time = time.time()
                print(f"\n>>> 开始处理槽位 {slot_index} <<<")

                self.set_all_shape_keys(0)

                print(f"  [SK] 激活槽位 {slot_index} 的形态键...")
                for obj in self.target_objects:
                    if obj.data and obj.data.shape_keys:
                        if len(obj.data.shape_keys.key_blocks) > slot_index:
                            key_block = obj.data.shape_keys.key_blocks[slot_index]
                            key_block.value = 1.0

                self.select_objects_from_node_tree(self.props.ske_node_tree_name)
                self._wait_for_step_delay()

                print("  [Save] 保存工程文件...")
                self._safe_save_mainfile()
                self._wait_for_step_delay()

                print("  [Export] 调用 SSMT 导出...")
                bpy.ops.ssmt.generate_mod_blueprint(node_tree_name=self.props.ske_node_tree_name)
                print("  [Export] 导出命令执行完毕。")
                self._wait_for_step_delay()

                buffer_name = f"Buffer1{slot_index:03d}"
                self.cleanup_export_files(buffer_name)

                print(f"<<< 槽位 {slot_index} 处理完成 (耗时: {time.time() - loop_start_time:.2f}s) >>>")
                self._wait_for_step_delay()

            print(f"\n{'=' * 40}")
            print(f"   形态键导出流程全部完成! 总耗时: {time.time() - start_time:.2f}s")
            print(f"{'=' * 40}")

        except Exception as e:
            print(f"\n!!! 自动化流程发生严重错误 !!!")
            traceback.print_exc()
            raise e 
        finally:
            print("\n--- [Final Cleanup] 执行最终清理 ---")
            if self.target_objects:
                self.set_all_shape_keys(0)
                print("  [Cleanup] 所有形态键已归零。")


class ATP_OT_ShapeKeyExport(bpy.types.Operator):
    bl_idname = "atp.shape_key_export"
    bl_label = "执行形态键导出"
    bl_description = "执行自动化形态键导出流程"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.atp_props
        try:
            pipeline = SKE_AutomationPipeline(props, context)
            pipeline.run()
        except Exception as e:
            self.report({'ERROR'}, f"形态键导出流程失败: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


at_shape_key_export_list = (
    ATP_OT_ShapeKeyExport,
)
