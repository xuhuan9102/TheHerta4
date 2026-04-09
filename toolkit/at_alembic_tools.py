# -*- coding: utf-8 -*-

import bpy
import bmesh
import os
import traceback
from collections import defaultdict

from .at_utils import is_alembic_object, move_object_to_collection


class ATP_OT_BakeAndImportAlembic(bpy.types.Operator):
    bl_idname = "atp.bake_and_import_alembic"
    bl_label = "执行烘焙与导入"
    bl_description = "将选中物体烘焙为Alembic文件并自动导入"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bpy.data.is_saved and context.selected_objects and context.active_object

    def execute(self, context):
        props = context.scene.atp_props
        original_selection = context.selected_objects[:]
        original_active = context.active_object

        if not original_active:
            self.report({'ERROR'}, "没有活动物体被选中，无法确定文件名。")
            return {'CANCELLED'}

        blend_file_dir = os.path.dirname(bpy.data.filepath)
        export_folder_path = os.path.join(blend_file_dir, props.abc_export_folder)

        try:
            os.makedirs(export_folder_path, exist_ok=True)
        except OSError as e:
            self.report({'ERROR'}, f"无法创建导出文件夹: {e}")
            return {'CANCELLED'}

        filename = f"{original_active.name}.abc"
        full_export_path = os.path.join(export_folder_path, filename)

        self.report({'INFO'}, f"开始将选中物体烘焙到: {full_export_path}")
        try:
            bpy.ops.wm.alembic_export(
                filepath=full_export_path,
                start=context.scene.frame_start,
                end=context.scene.frame_end,
                selected=True,
                uvs=props.abc_export_uvs,
                global_scale=props.abc_export_scale
            )
        except Exception as e:
            self.report({'ERROR'}, f"Alembic导出失败: {e}")
            traceback.print_exc()
            return {'CANCELLED'}

        if not os.path.exists(full_export_path):
            self.report({'ERROR'}, "导出文件未成功创建，导入中止")
            return {'CANCELLED'}

        self.report({'INFO'}, "烘焙成功，正在导入Alembic文件...")
        pre_import_objects = set(bpy.data.objects)

        try:
            bpy.ops.wm.alembic_import(filepath=full_export_path)
        except Exception as e:
            self.report({'ERROR'}, f"Alembic导入失败: {e}")
            return {'CANCELLED'}

        post_import_objects = set(bpy.data.objects)
        imported_objects = list(post_import_objects - pre_import_objects)

        if not imported_objects:
            self.report({'WARNING'}, "未找到新导入的物体")
            bpy.ops.object.select_all(action='DESELECT')
            for obj in original_selection: obj.select_set(True)
            context.view_layer.objects.active = original_active
            return {'FINISHED'}

        target_coll_name = props.abc_import_collection
        target_coll = bpy.data.collections.get(target_coll_name)
        if not target_coll:
            target_coll = bpy.data.collections.new(target_coll_name)
            context.scene.collection.children.link(target_coll)
            self.report({'INFO'}, f"已创建新集合: '{target_coll_name}'")

        for obj in imported_objects:
            move_object_to_collection(obj, target_coll)

        self.report({'INFO'}, f"成功将 {len(imported_objects)} 个物体放置到集合 '{target_coll.name}' 中")

        if props.abc_transfer_vertex_groups:
            if len(original_selection) == 1 and len(imported_objects) == 1:
                source_obj = original_selection[0]
                target_obj = imported_objects[0]

                if source_obj.type == 'MESH' and target_obj.type == 'MESH':
                    self.report({'INFO'}, f"开始从 '{source_obj.name}' 传递顶点组到 '{target_obj.name}'")
                    
                    pre_transfer_active = context.view_layer.objects.active
                    pre_transfer_selection = context.selected_objects[:]

                    try:
                        bpy.ops.object.select_all(action='DESELECT')
                        target_obj.select_set(True)
                        source_obj.select_set(True)
                        context.view_layer.objects.active = source_obj

                        bpy.ops.object.data_transfer(
                            data_type='VGROUP_WEIGHTS',
                            use_create=True,
                            vert_mapping='TOPOLOGY',
                            layers_select_src='ALL',
                            layers_select_dst='NAME',
                            mix_mode='REPLACE'
                        )
                        self.report({'INFO'}, "顶点组传递成功！")
                    except RuntimeError as e:
                        self.report({'ERROR'}, f"数据传递操作失败: {e}。请确保拓扑结构一致。")
                    finally:
                        bpy.ops.object.select_all(action='DESELECT')
                        for obj in pre_transfer_selection:
                            obj.select_set(True)
                        context.view_layer.objects.active = pre_transfer_active
                else:
                    self.report({'WARNING'}, "顶点组传递失败：源或目标不是网格物体。")
            elif len(original_selection) != 1:
                self.report({'WARNING'}, "顶点组传递失败：此功能仅在烘焙单个物体时可用。")

        bpy.ops.object.select_all(action='DESELECT')
        for obj in original_selection: obj.select_set(True)
        context.view_layer.objects.active = original_active

        return {'FINISHED'}


class ATP_OT_SplitAnimation(bpy.types.Operator):
    bl_idname = "atp.split_animation"
    bl_label = "执行动画拆分"
    bl_description = "将选中物体的每一帧拆分为新物体"
    bl_options = {'REGISTER', 'UNDO'}

    def copy_mesh_attributes(self, source_obj, target_obj, vertex_map):
        """在分离静态顶点时，复制UV、顶点色和权重"""
        source_mesh = source_obj.data
        target_mesh = target_obj.data

        for src_vg in source_obj.vertex_groups:
            dst_vg = target_obj.vertex_groups.new(name=src_vg.name)
            for new_idx, old_idx in vertex_map.items():
                try:
                    weight = src_vg.weight(old_idx)
                    if weight > 0:
                        dst_vg.add([new_idx], weight, 'REPLACE')
                except RuntimeError:
                    continue

        if source_mesh.uv_layers:
            for src_uv_layer in source_mesh.uv_layers:
                dst_uv_layer = target_mesh.uv_layers.new(name=src_uv_layer.name)
                for poly in target_mesh.polygons:
                    for loop_idx in poly.loop_indices:
                        new_vert_idx = target_mesh.loops[loop_idx].vertex_index
                        old_vert_idx = vertex_map[new_vert_idx]
                        for src_poly in source_mesh.polygons:
                            for src_loop_idx in src_poly.loop_indices:
                                if source_mesh.loops[src_loop_idx].vertex_index == old_vert_idx:
                                    dst_uv_layer.data[loop_idx].uv = source_mesh.uv_layers[src_uv_layer.name].data[src_loop_idx].uv
                                    break
                            else: continue
                            break

        if source_mesh.color_attributes:
            for src_col_layer in source_mesh.color_attributes:
                if src_col_layer.domain != 'CORNER': continue
                dst_col_layer = target_mesh.color_attributes.new(name=src_col_layer.name, type=src_col_layer.data_type, domain=src_col_layer.domain)
                for poly in target_mesh.polygons:
                    for loop_idx in poly.loop_indices:
                        new_vert_idx = target_mesh.loops[loop_idx].vertex_index
                        old_vert_idx = vertex_map[new_vert_idx]
                        for src_poly in source_mesh.polygons:
                            for src_loop_idx in src_poly.loop_indices:
                                if source_mesh.loops[src_loop_idx].vertex_index == old_vert_idx:
                                    dst_col_layer.data[loop_idx].color = src_col_layer.data[src_loop_idx].color
                                    break
                            else: continue
                            break

    def analyze_static_vertices(self, context, objects, props):
        """分析动画，找出在整个范围内位置不变的静态顶点"""
        self.report({'INFO'}, "正在分析静态顶点...")
        static_vertices_map = {}
        scene = context.scene
        original_frame = scene.frame_current

        try:
            for obj in objects:
                if obj.type not in {'MESH', 'CURVE', 'SURFACE'} or is_alembic_object(obj):
                    continue

                self.prepare_scene(props.anim_split_start_frame, props.anim_split_playback_type)
                depsgraph = context.evaluated_depsgraph_get()
                eval_obj = obj.evaluated_get(depsgraph)
                base_mesh = eval_obj.to_mesh()

                if not base_mesh.vertices:
                    eval_obj.to_mesh_clear()
                    continue

                base_positions = [v.co.copy() for v in base_mesh.vertices]
                eval_obj.to_mesh_clear()

                is_static = [True] * len(base_positions)

                self.prepare_scene(props.anim_split_end_frame, props.anim_split_playback_type)
                depsgraph = context.evaluated_depsgraph_get()
                eval_obj = obj.evaluated_get(depsgraph)
                current_mesh = eval_obj.to_mesh()

                for i, is_st in enumerate(is_static):
                    if is_st and i < len(current_mesh.vertices):
                        delta = (base_positions[i] - current_mesh.vertices[i].co).length
                        if delta > props.anim_split_static_tolerance:
                            is_static[i] = False

                eval_obj.to_mesh_clear()
                static_vertices_map[obj.name] = is_static
        finally:
            scene.frame_set(original_frame)

        return static_vertices_map

    def expand_dynamic_vertices(self, context, obj, static_verts_list, props):
        """修正静态顶点列表，确保任何接触到动态顶点的面上的所有顶点都被视为动态"""
        self.prepare_scene(props.anim_split_start_frame, props.anim_split_playback_type)
        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()

        try:
            if not mesh.polygons:
                return static_verts_list

            initial_dynamic_indices = {i for i, is_static in enumerate(static_verts_list) if not is_static}
            
            additional_dynamic_indices = set()
            for poly in mesh.polygons:
                is_poly_dynamic = False
                for vert_index in poly.vertices:
                    if vert_index in initial_dynamic_indices:
                        is_poly_dynamic = True
                        break
                if is_poly_dynamic:
                    for vert_index in poly.vertices:
                        additional_dynamic_indices.add(vert_index)
            
            if not additional_dynamic_indices:
                return static_verts_list

            new_static_verts_list = list(static_verts_list)
            for i in additional_dynamic_indices:
                if i < len(new_static_verts_list):
                    new_static_verts_list[i] = False
            
            return new_static_verts_list
            
        finally:
            eval_obj.to_mesh_clear()

    def create_separated_object(self, context, obj, frame, static_verts_list, collection, props, is_static_part):
        """根据静态/动态标记创建分离的物体"""
        self.prepare_scene(frame, props.anim_split_playback_type)
        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = obj.evaluated_get(depsgraph)

        source_mesh_temp = eval_obj.to_mesh()
        bm = bmesh.new()
        bm.from_mesh(source_mesh_temp)
        bm.verts.ensure_lookup_table()

        verts_to_keep_indices = {i for i, v_is_static in enumerate(static_verts_list) if
                                 (is_static_part and v_is_static) or (not is_static_part and not v_is_static)}

        if not verts_to_keep_indices:
            bm.free()
            eval_obj.to_mesh_clear()
            return None

        verts_to_delete = [v for i, v in enumerate(bm.verts) if i not in verts_to_keep_indices]

        sorted_verts_to_keep = sorted(list(verts_to_keep_indices))
        vertex_map = {new_idx: old_idx for new_idx, old_idx in enumerate(sorted_verts_to_keep)}

        bmesh.ops.delete(bm, geom=verts_to_delete, context='VERTS')
        
        part_name_suffix = "StaticBase" if is_static_part else f"{frame:03d}"
        new_mesh_name = f"{obj.name}_{part_name_suffix}"
        new_obj_name = f"{props.be_object_prefix}{obj.name}_{part_name_suffix}"

        new_mesh = bpy.data.meshes.new(new_mesh_name)
        bm.to_mesh(new_mesh)
        bm.free()

        new_mesh.transform(eval_obj.matrix_world)

        new_obj = bpy.data.objects.new(new_obj_name, new_mesh)
        for slot in obj.material_slots:
            new_obj.data.materials.append(slot.material)

        move_object_to_collection(new_obj, collection)

        self.copy_mesh_attributes(eval_obj, new_obj, vertex_map)

        eval_obj.to_mesh_clear()
        return new_obj

    def create_alembic_snapshot(self, context, obj, frame, props):
        """为Alembic物体创建当前帧的快照（应用修改器）"""
        new_obj = None
        original_active = context.view_layer.objects.active
        original_selection = context.selected_objects[:]
        was_hidden = obj.hide_get()
        if was_hidden: obj.hide_set(False)

        try:
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            bpy.ops.object.duplicate()
            new_obj = context.active_object
            new_obj.name = f"{props.be_object_prefix}{obj.name}_{frame:03d}"

            self.prepare_scene(frame, props.anim_split_playback_type)
            cache_mod_name = next((mod.name for mod in new_obj.modifiers if mod.type == 'MESH_SEQUENCE_CACHE'), None)
            if cache_mod_name:
                bpy.ops.object.modifier_apply(modifier=cache_mod_name)
            else:
                self.report({'WARNING'}, f"Alembic 物体 '{obj.name}' 未找到网格序列缓存修改器。")

        except Exception as e:
            self.report({'ERROR'}, f"处理Alembic快照时出错: {e}")
            if new_obj: bpy.data.objects.remove(new_obj, do_unlink=True)
            return None
        finally:
            if was_hidden: obj.hide_set(True)
            bpy.ops.object.select_all(action='DESELECT')
            for o in original_selection:
                if o.name in bpy.data.objects: o.select_set(True)
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = original_active

        return new_obj

    def set_linear_interpolation(self, objects):
        """将关键帧插值设为线性，防止拆分时出现过冲"""
        original_mode = bpy.context.object.mode if bpy.context.object else 'OBJECT'
        if original_mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
        try:
            for obj in objects:
                if obj.animation_data and obj.animation_data.action:
                    for fcurve in obj.animation_data.action.fcurves:
                        for keyframe in fcurve.keyframe_points:
                            keyframe.interpolation = 'LINEAR'
        finally:
            if original_mode != 'OBJECT': bpy.ops.object.mode_set(mode=original_mode)

    def prepare_scene(self, frame, playback_type):
        """跳转到指定帧"""
        if playback_type == "PRECISE":
            current = bpy.context.scene.frame_current
            if current > frame: current = max(1, frame - 5)
            for f in range(current, frame + 1):
                bpy.context.scene.frame_set(f)
        else:
            bpy.context.scene.frame_set(frame)

    def execute(self, context):
        props = context.scene.atp_props
        original_objects = context.selected_objects.copy()
        if not original_objects:
            self.report({'ERROR'}, "未选择任何对象")
            return {'CANCELLED'}

        if props.anim_split_start_frame > props.anim_split_end_frame:
            self.report({'ERROR'}, "起始帧不能大于结束帧")
            return {'CANCELLED'}

        original_frame = context.scene.frame_current

        if props.anim_split_set_linear:
            self.set_linear_interpolation(original_objects)
        
        coll_obj_map = defaultdict(list)
        for obj in original_objects:
            if not obj.users_collection:
                coll_obj_map[context.scene.collection].append(obj)
            else:
                coll_obj_map[obj.users_collection[0]].append(obj)

        static_vertices_map = {}
        has_alembic = any(is_alembic_object(obj) for obj in original_objects)
        if props.anim_split_separate_static and not has_alembic:
            static_vertices_map = self.analyze_static_vertices(context, original_objects, props)
            
            self.report({'INFO'}, "正在根据面的完整性修正动态顶点...")
            corrected_static_map = {}
            for obj in original_objects:
                if obj.name in static_vertices_map:
                    original_list = static_vertices_map[obj.name]
                    corrected_list = self.expand_dynamic_vertices(context, obj, original_list, props)
                    corrected_static_map[obj.name] = corrected_list
            static_vertices_map = corrected_static_map

        elif props.anim_split_separate_static and has_alembic:
             self.report({'WARNING'}, "检测到Alembic物体，已跳过静态分离分析。")

        try:
            for parent_coll, objects_in_coll in coll_obj_map.items():
                
                if props.anim_split_separate_static and static_vertices_map:
                    for obj in objects_in_coll:
                        if obj.name in static_vertices_map:
                            self.create_separated_object(context, obj, props.anim_split_start_frame,
                                                         static_vertices_map[obj.name], parent_coll, props,
                                                         is_static_part=True)

                for frame in range(props.anim_split_start_frame, props.anim_split_end_frame + 1):
                    sub_name = f"{parent_coll.name}_{props.anim_split_sub_prefix}{frame:03d}"
                    sub_collection = parent_coll.children.get(sub_name)

                    if not sub_collection:
                        sub_collection = bpy.data.collections.new(sub_name)
                        parent_coll.children.link(sub_collection)
                    else:
                        for obj_to_remove in list(sub_collection.objects):
                            bpy.data.objects.remove(obj_to_remove, do_unlink=True)

                    for obj in objects_in_coll:
                        if is_alembic_object(obj):
                            new_obj = self.create_alembic_snapshot(context, obj, frame, props)
                            if new_obj:
                                move_object_to_collection(new_obj, sub_collection)
                            continue

                        if props.anim_split_separate_static and obj.name in static_vertices_map:
                            self.create_separated_object(context, obj, frame,
                                                         static_vertices_map[obj.name], sub_collection, props,
                                                         is_static_part=False)
                        else:
                            self.prepare_scene(frame, props.anim_split_playback_type)
                            depsgraph = context.evaluated_depsgraph_get()
                            eval_obj = obj.evaluated_get(depsgraph)
                            mesh_data = bpy.data.meshes.new_from_object(eval_obj)
                            matrix = eval_obj.matrix_world.copy()
                            mesh_data.transform(matrix)
                            obj_name = f"{props.be_object_prefix}{obj.name}_{frame:03d}"
                            new_obj = bpy.data.objects.new(obj_name, mesh_data)
                            for slot in obj.material_slots:
                                if slot.material:
                                    new_obj.data.materials.append(slot.material)
                            move_object_to_collection(new_obj, sub_collection)
                            eval_obj.to_mesh_clear()

        finally:
            context.scene.frame_set(original_frame)

        self.report({'INFO'}, f"动画拆分完成！处理了 {len(original_objects)} 个对象。")
        return {'FINISHED'}


at_alembic_tools_list = (
    ATP_OT_BakeAndImportAlembic,
    ATP_OT_SplitAnimation,
)
