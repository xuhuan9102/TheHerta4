# -*- coding: utf-8 -*-

import bpy
import struct


class BMTP_OT_ExportAnimation(bpy.types.Operator):
    bl_idname = "bmtp.export_animation_buffer"
    bl_label = "导出动画缓冲"
    bl_description = "导出骨架动画数据为二进制缓冲文件"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.atp_props
        if not all([props.export_armature, props.export_mesh, props.export_filepath]):
            self.report({'ERROR'}, "请完整填写导出设置中的所有字段")
            return {'CANCELLED'}
        armature, mesh = props.export_armature, props.export_mesh
        output_location = bpy.path.abspath(props.export_filepath)
        S = context.scene
        result, original_frame = bytearray(), S.frame_current
        try:
            for z in range(props.export_frame_start, props.export_frame_end + 1):
                S.frame_set(z)
                frame_data = bytearray()
                pose_bones = {bone.name: bone for bone in armature.pose.bones}
                vertex_groups = [vg for vg in mesh.vertex_groups if vg.name in pose_bones]
                if not vertex_groups and z == props.export_frame_start:
                    self.report({'WARNING'}, f"网格 '{mesh.name}' 上没有找到与骨架 '{armature.name}' 匹配的顶点组。")
                    break
                for vg in vertex_groups:
                    bone = pose_bones[vg.name]
                    for i in range(3):
                        for j in range(4):
                            frame_data += struct.pack("f", bone.matrix_channel[i][j])
                result += frame_data
        finally:
            S.frame_set(original_frame)
        if not result:
             self.report({'WARNING'}, "没有导出任何数据。")
             return {'CANCELLED'}
        try:
            with open(output_location, "wb") as f:
                f.write(result)
            self.report({'INFO'}, f"动画已成功导出到: {output_location}")
        except Exception as e:
            self.report({'ERROR'}, f"无法写入文件: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}


at_animation_export_list = (
    BMTP_OT_ExportAnimation,
)
