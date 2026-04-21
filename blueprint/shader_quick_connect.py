import bpy
from bpy.types import Operator


class SSMT_OT_ShaderQuickTransparent(Operator):
    """快速创建透明材质节点连接
    
    从选中的纹理节点创建以下连接：
    - 纹理节点.颜色 -> 混合着色器.着色器2
    - 纹理节点.Alpha -> 混合着色器.系数
    - 透明BSDF -> 混合着色器.着色器1
    - 混合着色器 -> 材质输出
    """
    bl_idname = "ssmt.shader_quick_transparent"
    bl_label = "快速创建透明材质连接"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            self.report({'ERROR'}, "请在着色器编辑器中使用此功能")
            return {'CANCELLED'}

        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree:
            self.report({'ERROR'}, "未找到节点树")
            return {'CANCELLED'}

        if node_tree.bl_idname != 'ShaderNodeTree':
            self.report({'ERROR'}, "此功能仅适用于着色器编辑器")
            return {'CANCELLED'}

        selected_nodes = [node for node in node_tree.nodes if node.select]

        if len(selected_nodes) == 0:
            self.report({'WARNING'}, "请先选择一个纹理节点")
            return {'CANCELLED'}

        texture_node = None
        for node in selected_nodes:
            if node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT', 'TEX_NOISE', 'TEX_WAVE',
                            'TEX_VORONOI', 'TEX_MUSGRAVE', 'TEX_GRADIENT', 'TEX_MAGIC',
                            'TEX_CHECKER', 'TEX_BRICK', 'TEX_POINTDENSITY', 'TEX_COORDINATE',
                            'TEX_SKY', 'TEX_IES', 'TEX_WHITE_NOISE'}:
                texture_node = node
                break

        if not texture_node:
            texture_node = selected_nodes[0]

        links = node_tree.links
        nodes = node_tree.nodes

        base_x = texture_node.location.x + texture_node.width + 50
        base_y = texture_node.location.y

        mix_shader_node = nodes.new('ShaderNodeMixShader')
        mix_shader_node.name = "Mix Shader (Transparent)"
        mix_shader_node.label = "Mix Shader (Transparent)"
        mix_shader_node.location = (base_x + 200, base_y)

        transparent_node = nodes.new('ShaderNodeBsdfTransparent')
        transparent_node.name = "Transparent BSDF"
        transparent_node.label = "Transparent BSDF"
        transparent_node.location = (base_x, base_y + 100)

        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL' and node.is_active_output:
                output_node = node
                break

        if not output_node:
            for node in nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    output_node = node
                    break

        if not output_node:
            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.location = (base_x + 450, base_y)
            output_node.is_active_output = True

        color_output = None
        alpha_output = None

        for output in texture_node.outputs:
            if output.name == 'Color':
                color_output = output
            elif output.name == 'Alpha':
                alpha_output = output

        if color_output:
            links.new(color_output, mix_shader_node.inputs[2])

        if alpha_output:
            links.new(alpha_output, mix_shader_node.inputs[0])

        links.new(transparent_node.outputs[0], mix_shader_node.inputs[1])

        links.new(mix_shader_node.outputs[0], output_node.inputs[0])

        for node in selected_nodes:
            node.select = False

        mix_shader_node.select = True
        transparent_node.select = True

        self.report({'INFO'}, f"已创建透明材质连接: {texture_node.name} -> Mix Shader -> Material Output")

        return {'FINISHED'}


class SSMT_OT_ShaderQuickEmission(Operator):
    """快速创建发光材质节点连接
    
    从选中的纹理节点创建以下连接：
    - 纹理节点.颜色 -> 发光着色器.颜色
    - 发光着色器 -> 材质输出
    """
    bl_idname = "ssmt.shader_quick_emission"
    bl_label = "快速创建发光材质连接"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            self.report({'ERROR'}, "请在着色器编辑器中使用此功能")
            return {'CANCELLED'}

        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree:
            self.report({'ERROR'}, "未找到节点树")
            return {'CANCELLED'}

        if node_tree.bl_idname != 'ShaderNodeTree':
            self.report({'ERROR'}, "此功能仅适用于着色器编辑器")
            return {'CANCELLED'}

        selected_nodes = [node for node in node_tree.nodes if node.select]

        if len(selected_nodes) == 0:
            self.report({'WARNING'}, "请先选择一个纹理节点")
            return {'CANCELLED'}

        texture_node = None
        for node in selected_nodes:
            if node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT', 'TEX_NOISE', 'TEX_WAVE',
                            'TEX_VORONOI', 'TEX_MUSGRAVE', 'TEX_GRADIENT', 'TEX_MAGIC',
                            'TEX_CHECKER', 'TEX_BRICK', 'TEX_POINTDENSITY', 'TEX_COORDINATE',
                            'TEX_SKY', 'TEX_IES', 'TEX_WHITE_NOISE'}:
                texture_node = node
                break

        if not texture_node:
            texture_node = selected_nodes[0]

        links = node_tree.links
        nodes = node_tree.nodes

        base_x = texture_node.location.x + texture_node.width + 50
        base_y = texture_node.location.y

        emission_node = nodes.new('ShaderNodeEmission')
        emission_node.name = "Emission"
        emission_node.label = "Emission"
        emission_node.location = (base_x + 200, base_y)

        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL' and node.is_active_output:
                output_node = node
                break

        if not output_node:
            for node in nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    output_node = node
                    break

        if not output_node:
            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.location = (base_x + 450, base_y)
            output_node.is_active_output = True

        color_output = None
        for output in texture_node.outputs:
            if output.name == 'Color':
                color_output = output
                break

        if color_output:
            links.new(color_output, emission_node.inputs[0])

        links.new(emission_node.outputs[0], output_node.inputs[0])

        for node in selected_nodes:
            node.select = False

        emission_node.select = True

        self.report({'INFO'}, f"已创建发光材质连接: {texture_node.name} -> Emission -> Material Output")

        return {'FINISHED'}


class SSMT_OT_ShaderQuickDiffuse(Operator):
    """快速创建漫反射材质节点连接
    
    从选中的纹理节点创建以下连接：
    - 纹理节点.颜色 -> 漫反射BSDF.颜色
    - 漫反射BSDF -> 材质输出
    """
    bl_idname = "ssmt.shader_quick_diffuse"
    bl_label = "快速创建漫反射材质连接"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            self.report({'ERROR'}, "请在着色器编辑器中使用此功能")
            return {'CANCELLED'}

        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree:
            self.report({'ERROR'}, "未找到节点树")
            return {'CANCELLED'}

        if node_tree.bl_idname != 'ShaderNodeTree':
            self.report({'ERROR'}, "此功能仅适用于着色器编辑器")
            return {'CANCELLED'}

        selected_nodes = [node for node in node_tree.nodes if node.select]

        if len(selected_nodes) == 0:
            self.report({'WARNING'}, "请先选择一个纹理节点")
            return {'CANCELLED'}

        texture_node = None
        for node in selected_nodes:
            if node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT', 'TEX_NOISE', 'TEX_WAVE',
                            'TEX_VORONOI', 'TEX_MUSGRAVE', 'TEX_GRADIENT', 'TEX_MAGIC',
                            'TEX_CHECKER', 'TEX_BRICK', 'TEX_POINTDENSITY', 'TEX_COORDINATE',
                            'TEX_SKY', 'TEX_IES', 'TEX_WHITE_NOISE'}:
                texture_node = node
                break

        if not texture_node:
            texture_node = selected_nodes[0]

        links = node_tree.links
        nodes = node_tree.nodes

        base_x = texture_node.location.x + texture_node.width + 50
        base_y = texture_node.location.y

        diffuse_node = nodes.new('ShaderNodeBsdfDiffuse')
        diffuse_node.name = "Diffuse BSDF"
        diffuse_node.label = "Diffuse BSDF"
        diffuse_node.location = (base_x + 200, base_y)

        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL' and node.is_active_output:
                output_node = node
                break

        if not output_node:
            for node in nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    output_node = node
                    break

        if not output_node:
            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.location = (base_x + 450, base_y)
            output_node.is_active_output = True

        color_output = None
        for output in texture_node.outputs:
            if output.name == 'Color':
                color_output = output
                break

        if color_output:
            links.new(color_output, diffuse_node.inputs[0])

        links.new(diffuse_node.outputs[0], output_node.inputs[0])

        for node in selected_nodes:
            node.select = False

        diffuse_node.select = True

        self.report({'INFO'}, f"已创建漫反射材质连接: {texture_node.name} -> Diffuse BSDF -> Material Output")

        return {'FINISHED'}


class SSMT_OT_ShaderQuickPrincipled(Operator):
    """快速创建原理化BSDF材质节点连接
    
    从选中的纹理节点创建以下连接：
    - 纹理节点.颜色 -> 原理化BSDF.基础颜色
    - 纹理节点.Alpha -> 原理化BSDF.透明度（如果存在）
    - 原理化BSDF -> 材质输出
    """
    bl_idname = "ssmt.shader_quick_principled"
    bl_label = "快速创建原理化材质连接"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            self.report({'ERROR'}, "请在着色器编辑器中使用此功能")
            return {'CANCELLED'}

        node_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not node_tree:
            self.report({'ERROR'}, "未找到节点树")
            return {'CANCELLED'}

        if node_tree.bl_idname != 'ShaderNodeTree':
            self.report({'ERROR'}, "此功能仅适用于着色器编辑器")
            return {'CANCELLED'}

        selected_nodes = [node for node in node_tree.nodes if node.select]

        if len(selected_nodes) == 0:
            self.report({'WARNING'}, "请先选择一个纹理节点")
            return {'CANCELLED'}

        texture_node = None
        for node in selected_nodes:
            if node.type in {'TEX_IMAGE', 'TEX_ENVIRONMENT', 'TEX_NOISE', 'TEX_WAVE',
                            'TEX_VORONOI', 'TEX_MUSGRAVE', 'TEX_GRADIENT', 'TEX_MAGIC',
                            'TEX_CHECKER', 'TEX_BRICK', 'TEX_POINTDENSITY', 'TEX_COORDINATE',
                            'TEX_SKY', 'TEX_IES', 'TEX_WHITE_NOISE'}:
                texture_node = node
                break

        if not texture_node:
            texture_node = selected_nodes[0]

        links = node_tree.links
        nodes = node_tree.nodes

        base_x = texture_node.location.x + texture_node.width + 50
        base_y = texture_node.location.y

        principled_node = nodes.new('ShaderNodeBsdfPrincipled')
        principled_node.name = "Principled BSDF"
        principled_node.label = "Principled BSDF"
        principled_node.location = (base_x + 200, base_y)

        output_node = None
        for node in nodes:
            if node.type == 'OUTPUT_MATERIAL' and node.is_active_output:
                output_node = node
                break

        if not output_node:
            for node in nodes:
                if node.type == 'OUTPUT_MATERIAL':
                    output_node = node
                    break

        if not output_node:
            output_node = nodes.new('ShaderNodeOutputMaterial')
            output_node.location = (base_x + 450, base_y)
            output_node.is_active_output = True

        color_output = None
        alpha_output = None

        for output in texture_node.outputs:
            if output.name == 'Color':
                color_output = output
            elif output.name == 'Alpha':
                alpha_output = output

        if color_output:
            links.new(color_output, principled_node.inputs[0])

        if alpha_output:
            for inp in principled_node.inputs:
                if inp.name in {'Alpha', '透明度'}:
                    links.new(alpha_output, inp)
                    break

        links.new(principled_node.outputs[0], output_node.inputs[0])

        for node in selected_nodes:
            node.select = False

        principled_node.select = True

        self.report({'INFO'}, f"已创建原理化材质连接: {texture_node.name} -> Principled BSDF -> Material Output")

        return {'FINISHED'}


def draw_shader_context_menu(self, context):
    if not isinstance(context.space_data, bpy.types.SpaceNodeEditor):
        return

    if context.space_data.tree_type != 'ShaderNodeTree':
        return

    selected_nodes = [node for node in context.space_data.node_tree.nodes if node.select] if context.space_data.node_tree else []

    if len(selected_nodes) == 0:
        return

    layout = self.layout
    layout.separator()
    layout.label(text="材质快速连接", icon='NODETREE')

    layout.operator("ssmt.shader_quick_transparent", text="透明材质连接", icon='NODE')
    layout.operator("ssmt.shader_quick_emission", text="发光材质连接", icon='NODE')
    layout.operator("ssmt.shader_quick_diffuse", text="漫反射材质连接", icon='NODE')
    layout.operator("ssmt.shader_quick_principled", text="原理化材质连接", icon='NODE')


classes = (
    SSMT_OT_ShaderQuickTransparent,
    SSMT_OT_ShaderQuickEmission,
    SSMT_OT_ShaderQuickDiffuse,
    SSMT_OT_ShaderQuickPrincipled,
)

_is_menu_hooked = False


def register():
    global _is_menu_hooked
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _is_menu_hooked:
        bpy.types.NODE_MT_context_menu.append(draw_shader_context_menu)
        _is_menu_hooked = True


def unregister():
    global _is_menu_hooked
    if _is_menu_hooked:
        bpy.types.NODE_MT_context_menu.remove(draw_shader_context_menu)
        _is_menu_hooked = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
