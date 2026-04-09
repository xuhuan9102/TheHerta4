import bpy


class VGAdjustListUI(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "is_selected", text="")
            row.label(text=item.name, icon='GROUP_VERTEX')
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon_value=icon)


class RefreshVGList(bpy.types.Operator):
    bl_idname = "toolkit.refresh_vg_list"
    bl_label = "刷新顶点组"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        props = context.scene.vg_props
        
        props.vg_adjust_available_groups.clear()
        props.vg_adjust_available_groups_index = 0
        
        if not obj.vertex_groups:
            self.report({'INFO'}, "物体没有顶点组")
            return {'FINISHED'}
        
        for vg in obj.vertex_groups:
            item = props.vg_adjust_available_groups.add()
            item.name = vg.name
            item.is_selected = False
        
        self.report({'INFO'}, f"已加载 {len(props.vg_adjust_available_groups)} 个顶点组")
        return {'FINISHED'}


class SelectAllVG(bpy.types.Operator):
    bl_idname = "toolkit.select_all_vg"
    bl_label = "全选"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        props = context.scene.vg_props
        return len(props.vg_adjust_available_groups) > 0

    def execute(self, context):
        props = context.scene.vg_props
        
        for item in props.vg_adjust_available_groups:
            item.is_selected = True
        
        self.report({'INFO'}, "已全选所有顶点组")
        return {'FINISHED'}


class DeselectAllVG(bpy.types.Operator):
    bl_idname = "toolkit.deselect_all_vg"
    bl_label = "取消全选"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        props = context.scene.vg_props
        return len(props.vg_adjust_available_groups) > 0

    def execute(self, context):
        props = context.scene.vg_props
        
        for item in props.vg_adjust_available_groups:
            item.is_selected = False
        
        self.report({'INFO'}, "已取消全选")
        return {'FINISHED'}


class InvertVGSelection(bpy.types.Operator):
    bl_idname = "toolkit.invert_vg_selection"
    bl_label = "反选"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        props = context.scene.vg_props
        return len(props.vg_adjust_available_groups) > 0

    def execute(self, context):
        props = context.scene.vg_props
        
        for item in props.vg_adjust_available_groups:
            item.is_selected = not item.is_selected
        
        self.report({'INFO'}, "已反选")
        return {'FINISHED'}


class AddVGToAdjustList(bpy.types.Operator):
    bl_idname = "toolkit.add_vg_to_adjust_list"
    bl_label = "添加顶点组"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        props = context.scene.vg_props
        return len(props.vg_adjust_available_groups) > 0

    def execute(self, context):
        props = context.scene.vg_props
        index = props.vg_adjust_available_groups_index
        
        if index < 0 or index >= len(props.vg_adjust_available_groups):
            return {'CANCELLED'}
        
        vg_name = props.vg_adjust_available_groups[index].name
        
        for item in props.vg_adjust_selected_groups:
            if item.name == vg_name:
                self.report({'INFO'}, f"顶点组 '{vg_name}' 已在列表中")
                return {'CANCELLED'}
        
        new_item = props.vg_adjust_selected_groups.add()
        new_item.name = vg_name
        return {'FINISHED'}


class RemoveVGFromAdjustList(bpy.types.Operator):
    bl_idname = "toolkit.remove_vg_from_adjust_list"
    bl_label = "移除顶点组"
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        props = context.scene.vg_props
        return len(props.vg_adjust_selected_groups) > 0

    def execute(self, context):
        props = context.scene.vg_props
        index = props.vg_adjust_selected_groups_index
        
        if index >= 0 and index < len(props.vg_adjust_selected_groups):
            props.vg_adjust_selected_groups.remove(index)
            props.vg_adjust_selected_groups_index = min(max(0, index - 1), len(props.vg_adjust_selected_groups) - 1)
        
        return {'FINISHED'}


class ClearVGAdjustList(bpy.types.Operator):
    bl_idname = "toolkit.clear_vg_adjust_list"
    bl_label = "清空列表"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.vg_props
        return len(props.vg_adjust_selected_groups) > 0

    def execute(self, context):
        props = context.scene.vg_props
        props.vg_adjust_selected_groups.clear()
        props.vg_adjust_selected_groups_index = 0
        return {'FINISHED'}


class AdjustVGWeights(bpy.types.Operator):
    bl_idname = "toolkit.adjust_vg_weights"
    bl_label = "调整顶点组权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        props = context.scene.vg_props
        return obj and obj.type == 'MESH' and len(props.vg_adjust_available_groups) > 0

    def execute(self, context):
        obj = context.active_object
        props = context.scene.vg_props
        
        if not obj.vertex_groups:
            self.report({'WARNING'}, "物体没有顶点组")
            return {'CANCELLED'}
        
        selected_vg_names = {item.name for item in props.vg_adjust_available_groups if item.is_selected}
        selected_vg_indices = set()
        
        for vg in obj.vertex_groups:
            if vg.name in selected_vg_names:
                selected_vg_indices.add(vg.index)
        
        if not selected_vg_indices:
            self.report({'WARNING'}, "没有勾选任何顶点组")
            return {'CANCELLED'}
        
        adjust_amount = props.vg_adjust_amount
        adjust_mode = props.vg_adjust_mode
        
        modified_count = 0
        
        for v in obj.data.vertices:
            for g in v.groups:
                if g.group in selected_vg_indices:
                    if adjust_mode == 'ADD':
                        g.weight = max(0.0, min(1.0, g.weight + adjust_amount))
                    elif adjust_mode == 'MULTIPLY':
                        g.weight = max(0.0, min(1.0, g.weight * (1.0 + adjust_amount)))
                    modified_count += 1
        
        self.report({'INFO'}, f"已调整 {modified_count} 个顶点的权重")
        return {'FINISHED'}


class NormalizeVGWeights(bpy.types.Operator):
    bl_idname = "toolkit.normalize_vg_weights"
    bl_label = "规格化顶点组权重"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        props = context.scene.vg_props
        return obj and obj.type == 'MESH' and len(props.vg_adjust_selected_groups) > 0

    def execute(self, context):
        obj = context.active_object
        props = context.scene.vg_props
        
        if not obj.vertex_groups:
            self.report({'WARNING'}, "物体没有顶点组")
            return {'CANCELLED'}
        
        selected_vg_names = {item.name for item in props.vg_adjust_selected_groups}
        selected_vg_indices = set()
        
        for vg in obj.vertex_groups:
            if vg.name in selected_vg_names:
                selected_vg_indices.add(vg.index)
        
        if not selected_vg_indices:
            self.report({'WARNING'}, "没有找到选中的顶点组")
            return {'CANCELLED'}
        
        normalize_mode = props.vg_normalize_mode
        modified_count = 0
        
        if normalize_mode == 'SELECTED':
            for v in obj.data.vertices:
                other_weight = 0.0
                selected_weights = {}
                
                for g in v.groups:
                    if g.group in selected_vg_indices:
                        selected_weights[g.group] = g.weight
                    else:
                        other_weight += g.weight
                
                if other_weight >= 1.0:
                    for g in v.groups:
                        if g.group in selected_vg_indices:
                            g.weight = 0.0
                            modified_count += 1
                else:
                    target_weight = 1.0 - other_weight
                    selected_weight = sum(selected_weights.values())
                    
                    if selected_weight > 1e-6:
                        scale = target_weight / selected_weight
                        for g in v.groups:
                            if g.group in selected_vg_indices:
                                g.weight *= scale
                                modified_count += 1
                    else:
                        if len(selected_vg_indices) > 0:
                            weight_per_vg = target_weight / len(selected_vg_indices)
                            for vg_index in selected_vg_indices:
                                obj.vertex_groups[vg_index].add([v.index], weight_per_vg, 'REPLACE')
                                modified_count += 1
        else:
            for v in obj.data.vertices:
                total_weight = 0.0
                for g in v.groups:
                    total_weight += g.weight
                
                if total_weight > 1e-6:
                    scale = 1.0 / total_weight
                    for g in v.groups:
                        g.weight *= scale
                        modified_count += 1
        
        self.report({'INFO'}, f"已规格化 {modified_count} 个顶点的权重")
        return {'FINISHED'}


vg_weight_adjust_operators = [
    VGAdjustListUI,
    RefreshVGList,
    SelectAllVG,
    DeselectAllVG,
    InvertVGSelection,
    AddVGToAdjustList,
    RemoveVGFromAdjustList,
    ClearVGAdjustList,
    AdjustVGWeights,
    NormalizeVGWeights,
]
