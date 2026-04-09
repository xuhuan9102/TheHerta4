# -*- coding: utf-8 -*-

import bpy
from pathlib import Path


class BMTP_OT_MergeBuffers(bpy.types.Operator):
    """根据命名规则批量合并顶点缓冲文件"""
    bl_idname = "bmtp.merge_vertex_buffers"
    bl_label = "合并顶点缓冲"
    bl_description = "自动合并文件夹内所有'-Position.buf'和'-Texcoord.buf'文件"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.atp_props
        path_str = props.merge_buf_path
        
        if not path_str:
            self.report({'ERROR'}, "请先选择一个包含缓冲文件的目标路径")
            return {'CANCELLED'}

        target_dir = Path(bpy.path.abspath(path_str))
        
        if not target_dir.is_dir():
            self.report({'ERROR'}, f"指定的路径不是一个有效的文件夹: {target_dir}")
            return {'CANCELLED'}

        merged_count = 0
        
        for pos_file_path in target_dir.glob('*-Position.buf'):
            base_name = pos_file_path.name.removesuffix('-Position.buf')
            
            tex_file_path = target_dir / f"{base_name}-Texcoord.buf"
            output_file_path = target_dir / f"{base_name}.buf"
            
            if not tex_file_path.exists():
                print(f"跳过: 未找到对应的Texcoord文件 '{tex_file_path.name}'")
                continue

            try:
                with open(pos_file_path, 'rb') as pos_file, \
                     open(tex_file_path, 'rb') as tex_file, \
                     open(output_file_path, 'wb') as merge_file:
                    
                    while True:
                        pos_chunk = pos_file.read(16)
                        if not pos_chunk:
                            break
                        
                        tex_chunk = tex_file.read(8)
                        if not tex_chunk:
                            break
                        
                        merge_file.write(pos_chunk)
                        merge_file.write(tex_chunk)
                
                merged_count += 1
                print(f"成功合并: {base_name}")
                
            except Exception as e:
                self.report({'WARNING'}, f"合并文件 '{base_name}' 时出错: {str(e)}")
                continue

        if merged_count > 0:
            self.report({'INFO'}, f"成功合并了 {merged_count} 个顶点缓冲文件")
        else:
            self.report({'WARNING'}, "没有找到可合并的文件对")
        
        return {'FINISHED'}


at_buffer_merge_list = (
    BMTP_OT_MergeBuffers,
)
