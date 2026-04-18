import bpy
from bpy.types import NodeTree, Node, NodeSocket

from ..common.logic_name import LogicName
from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.object_prefix_helper import ObjectPrefixHelper
from .node_base import SSMTBlueprintTree, SSMTNodeBase

BLENDER_VERSION = bpy.app.version[:2]

_picking_node_name = None
_picking_tree_name = None
_is_viewing_group_objects = False



class SSMT_OT_RefreshNodeObjectIDs(bpy.types.Operator):
    '''刷新节点树中所有节点的物体ID关联'''
    bl_idname = "ssmt.refresh_node_object_ids"
    bl_label = "刷新物体ID关联"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        updated_count = 0
        
        for tree in bpy.data.node_groups:
            if tree.bl_idname == 'SSMTBlueprintTreeType':
                for node in tree.nodes:
                    if node.bl_idname == 'SSMTNode_Object_Info':
                        obj_name = getattr(node, 'object_name', '')
                        obj_id = getattr(node, 'object_id', '')
                        
                        if obj_name:
                            obj = bpy.data.objects.get(obj_name)
                            if obj:
                                new_obj_id = str(obj.as_pointer())
                                if node.object_id != new_obj_id:
                                    node.object_id = new_obj_id
                                    updated_count += 1
                            elif obj_id:
                                node.object_id = ""
                                updated_count += 1
                    
                    elif node.bl_idname == 'SSMTNode_MultiFile_Export':
                        for item in node.object_list:
                            obj_name = getattr(item, 'object_name', '')
                            if obj_name:
                                obj = bpy.data.objects.get(obj_name)
                                if obj:
                                    new_name = obj.name
                                    if item.object_name != new_name:
                                        item.object_name = new_name
                                        updated_count += 1
                                elif getattr(item, 'original_object_name', ''):
                                    orig_obj = bpy.data.objects.get(item.original_object_name)
                                    if orig_obj:
                                        item.object_name = item.original_object_name
                                        updated_count += 1
        
        if updated_count > 0:
            self.report({'INFO'}, f"已更新 {updated_count} 个节点的物体引用")
           
        else:
            self.report({'INFO'}, "所有节点都已建立物体引用关联")
        
        return {'FINISHED'}


class SSMT_OT_SelectNodeObject(bpy.types.Operator):
    '''Select this object in 3D View'''
    bl_idname = "ssmt.select_node_object"
    bl_label = "Select Object"
    
    object_name: bpy.props.StringProperty()

    def execute(self, context):
        obj_name = self.object_name
        if not obj_name:
            return {'CANCELLED'}
        
        obj = bpy.data.objects.get(obj_name)
        if obj:
            try:
                bpy.ops.object.select_all(action='DESELECT')
            except:
                pass
                
            obj.select_set(True)
            context.view_layer.objects.active = obj
            self.report({'INFO'}, f"Selected: {obj_name}")
        else:
            self.report({'WARNING'}, f"Object '{obj_name}' not found")
        
        return {'FINISHED'}


class SSMT_OT_StartPickObject(bpy.types.Operator):
    '''Start picking an object from 3D View'''
    bl_idname = "ssmt.start_pick_object"
    bl_label = "Pick Object"
    bl_description = "点击后在3D视图中选择一个物体"
    
    node_name: bpy.props.StringProperty()
    
    def execute(self, context):
        global _picking_node_name, _picking_tree_name
        
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        
        if not tree:
            self.report({'WARNING'}, "无法获取节点树上下文")
            return {'CANCELLED'}
        
        _picking_node_name = self.node_name
        _picking_tree_name = tree.name
        self.report({'INFO'}, "请在3D视图中点击选择一个物体")
        
        bpy.ops.ssmt.pick_object_modal('INVOKE_DEFAULT')
        
        return {'FINISHED'}


class SSMT_OT_PickObjectModal(bpy.types.Operator):
    '''Modal operator for picking objects in 3D View'''
    bl_idname = "ssmt.pick_object_modal"
    bl_label = "Pick Object"
    bl_options = {'REGISTER', 'INTERNAL'}
    
    def invoke(self, context, event):
        global _picking_node_name
        
        if not _picking_node_name:
            return {'CANCELLED'}
        
        self._initial_selected_objs = set(context.selected_objects)
        if context.selected_objects:
            self._last_selected_obj = context.selected_objects[0]
        else:
            self._last_selected_obj = None
        
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        global _picking_node_name, _picking_tree_name
        
        if event.type == 'ESC':
            _picking_node_name = None
            _picking_tree_name = None
            return {'CANCELLED'}
        
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            for area in context.screen.areas:
                if area.type == 'VIEW_3D':
                    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                    if region and area.x <= event.mouse_x <= area.x + area.width and area.y <= event.mouse_y <= area.y + area.height:
                        return {'PASS_THROUGH'}
        
        if event.type == 'MOUSEMOVE':
            current_selected = context.selected_objects
            if current_selected:
                current_obj = current_selected[0]
                if current_obj != self._last_selected_obj and current_obj not in self._initial_selected_objs:
                    tree = bpy.data.node_groups.get(_picking_tree_name)
                    if tree:
                        node = tree.nodes.get(_picking_node_name)
                        if node:
                            node.object_name = current_obj.name
                            self.report({'INFO'}, f"已选择物体: {current_obj.name}")
                    
                    _picking_node_name = None
                    _picking_tree_name = None
                    return {'FINISHED'}
        
        return {'PASS_THROUGH'}


def draw_view3d_header(self, context):
    global _picking_node_name
    if _picking_node_name:
        self.layout.label(text="请在3D视图中点击选择一个物体...", icon='EYEDROPPER')


class SSMTNode_Object_Info(SSMTNodeBase):
    '''Object Info Node'''
    bl_idname = 'SSMTNode_Object_Info'
    bl_label = 'Object Info'
    bl_icon = 'OBJECT_DATAMODE'
    bl_width_min = 300

    def _set_cached_prefix_data(self, prefix: str, separator: str):
        self["object_prefix"] = ObjectPrefixHelper.normalize_prefix(prefix)
        self["prefix_separator"] = separator or "."

    def _refresh_prefix_cache(self, from_prefix_update: bool = False):
        self.label = self.object_name if self.object_name else "Object Info"

        prefix_info = ObjectPrefixHelper.get_node_prefix_info(self)
        prefix = ""
        separator = self.prefix_separator or "."
        if prefix_info:
            prefix, separator = prefix_info

        if prefix:
            self._set_cached_prefix_data(prefix, separator)
            if not from_prefix_update:
                normalized = ObjectPrefixHelper.normalize_prefix(prefix)
                if self.object_prefix != normalized:
                    self.object_prefix = normalized
        elif not self.object_prefix:
            self["prefix_separator"] = separator

        parts = ObjectPrefixHelper.parse_prefix_parts(self.object_prefix)
        self.draw_ib = parts["draw_ib"]
        self.index_count = parts["index_count"]
        self.first_index = parts["first_index"]
        self.component = parts["component"]

        _, _, base_name = ObjectPrefixHelper.split_name_and_prefix(self.object_name, self.object_prefix, self.prefix_separator)
        self.alias_name = base_name

        obj = bpy.data.objects.get(self.object_name)
        if obj:
            self.object_id = str(obj.as_pointer())
        elif not self.object_name:
            self.object_id = ""

        self.update_node_width([self.object_name, self.object_prefix])

    def update_object_prefix(self, context):
        self._set_cached_prefix_data(self.object_prefix, self.prefix_separator)
        self._refresh_prefix_cache(from_prefix_update=True)
    
    def update_object_name(self, context):
        self._refresh_prefix_cache()
    object_name: bpy.props.StringProperty(name="Object Name", default="", update=update_object_name)
    object_id: bpy.props.StringProperty(name="Object ID", default="")
    original_object_name: bpy.props.StringProperty(name="Original Object Name", default="")
    object_prefix: bpy.props.StringProperty(name="Prefix", default="", update=update_object_prefix)
    prefix_separator: bpy.props.StringProperty(name="Prefix Separator", default=".")


    draw_ib: bpy.props.StringProperty(name="DrawIB", default="")
    index_count: bpy.props.StringProperty(name="IndexCount", default="")
    first_index: bpy.props.StringProperty(name="FirstIndex", default="")
    component: bpy.props.StringProperty(name="Component", default="")
    alias_name: bpy.props.StringProperty(name="Alias Name", default="")

    def get_effective_prefix(self) -> str:
        prefix_info = ObjectPrefixHelper.get_node_prefix_info(self)
        return prefix_info[0] if prefix_info else ""

    def get_effective_separator(self) -> str:
        prefix_info = ObjectPrefixHelper.get_node_prefix_info(self)
        return prefix_info[1] if prefix_info else (self.prefix_separator or ".")

    def get_virtual_object_name(self) -> str:
        return ObjectPrefixHelper.build_virtual_object_name_for_node(self)

    def init(self, context):
        self.outputs.new('SSMTSocketObject', "Object")

    def draw_buttons(self, context, layout):
        row = layout.row(align=True)

        row.prop_search(self, "object_name", bpy.data, "objects", text="", icon='OBJECT_DATA')
        
        op = row.operator("ssmt.start_pick_object", text="", icon='EYEDROPPER')
        op.node_name = self.name

        if self.object_name:
            obj = bpy.data.objects.get(self.object_name)
            if obj:
                op = row.operator("ssmt.select_node_object", text="", icon='RESTRICT_SELECT_OFF')
                op.object_name = self.object_name
                
                if not self.object_id:
                    self.object_id = str(obj.as_pointer())
            else:
                row.label(text="", icon='ERROR')

        layout.prop(self, "object_prefix", text="前缀")


class SSMTNode_Object_Group(SSMTNodeBase):
    '''单纯用于分组的节点，可以接受任何节点作为输入，放在一个组里'''
    bl_idname = 'SSMTNode_Object_Group'
    bl_label = 'Group'
    bl_icon = 'GROUP'

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 200

    def draw_buttons(self, context, layout):
        layout.operator("ssmt.view_group_objects", text="查看递归解析预览", icon='HIDE_OFF').node_name = self.name

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Input {len(self.inputs) + 1}")
        
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
             self.inputs.remove(self.inputs[-1])


class SSMTNode_Result_Output(SSMTNodeBase):
    '''Result Output Node'''
    bl_idname = 'SSMTNode_Result_Output'
    bl_label = 'Generate Mod'
    bl_icon = 'EXPORT'

    show_vertex_deduplication_panel: bpy.props.BoolProperty(
        name="顶点去重精度控制",
        default=False,
    ) # type: ignore

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Group 1")
        self.outputs.new('SSMTSocketPostProcess', "Post Process")
        self.width = 400

    def draw_buttons(self, context, layout):
        layout.operator("ssmt.generate_mod_blueprint", text="Generate Mod", icon='EXPORT')
        
        if GlobalConfig.logic_name == LogicName.WWMI:
            layout.prop(context.scene.global_properties, "ignore_muted_shape_keys")
            layout.prop(context.scene.global_properties, "apply_all_modifiers")
            layout.prop(context.scene.global_properties, "export_add_missing_vertex_groups")

        layout.prop(context.scene.global_properties, 
                    "forbid_auto_texture_ini",text="禁止自动贴图流程")

        if GlobalConfig.logic_name != LogicName.GF2:
            layout.prop(context.scene.global_properties,
                        "recalculate_tangent",text="向量归一化法线存入TANGENT(全局)")

        if GlobalConfig.logic_name == LogicName.HIMI:
            layout.prop(context.scene.global_properties,
                        "recalculate_color",text="算术平均归一化法线存入COLOR(全局)")

        if GlobalConfig.logic_name == LogicName.ZZMI:
            layout.prop(context.scene.global_properties, "zzz_use_slot_fix")

        if GlobalConfig.logic_name == LogicName.GIMI:
            layout.prop(context.scene.global_properties, "gimi_use_orfix")

        if GlobalConfig.logic_name == LogicName.EFMI:
            layout.prop(context.scene.global_properties, "use_rabbitfx_slot")

        layout.prop(context.scene.global_properties, "generate_branch_mod_gui",text="生成分支架构Mod面板(测试中)")

        layout.prop(context.scene.global_properties, "open_mod_folder_after_generate_mod",text="生成Mod后打开Mod所在文件夹")

        layout.prop(context.scene.global_properties, "use_specific_generate_mod_folder_path")

        if GlobalProterties.use_specific_generate_mod_folder_path():
            box = layout.box()
            box.label(text="当前生成Mod位置文件夹:")
            box.label(text=context.scene.global_properties.generate_mod_folder_path)

            layout.operator("ssmt.select_generate_mod_folder", icon='FILE_FOLDER')

        row = layout.row()
        row.prop(self, "show_vertex_deduplication_panel", 
                 icon='TRIA_DOWN' if self.show_vertex_deduplication_panel else 'TRIA_RIGHT',
                 icon_only=True, emboss=False)
        row.label(text="顶点去重精度控制")

        if self.show_vertex_deduplication_panel:
            box = layout.box()
            col = box.column()
            col.label(text="参与去重的数据类型:")
            col.prop(context.scene.global_properties, "deduplicate_POSITION")
            col.prop(context.scene.global_properties, "deduplicate_NORMAL")
            col.prop(context.scene.global_properties, "deduplicate_TANGENT")
            col.prop(context.scene.global_properties, "deduplicate_BINORMAL")
            col.prop(context.scene.global_properties, "deduplicate_TEXCOORD")
            col.prop(context.scene.global_properties, "deduplicate_COLOR")
            col.prop(context.scene.global_properties, "deduplicate_BLENDWEIGHT")
            col.prop(context.scene.global_properties, "deduplicate_BLENDINDICES")

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Group {len(self.inputs) + 1}")
        
        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
             self.inputs.remove(self.inputs[-1])


class SSMT_OT_View_Group_Objects(bpy.types.Operator):
    '''递归解析当前组下面所有的物体并在当前3D视图中展示，点击切换局部视图，注意组节点最好不要包含按键切换，否则会同时展示所有切换分支内容'''
    bl_idname = "ssmt.view_group_objects"
    bl_label = "View Group Objects"
    
    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
             return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node:
             return {'CANCELLED'}

        view_3d_area = None
        view_3d_window = None
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    view_3d_area = area
                    view_3d_window = window
                    break
            if view_3d_area:
                break
        
        if not view_3d_area:
            self.report({'WARNING'}, "No 3D View found")
            return {'CANCELLED'}

        in_local_view = False
        for space in view_3d_area.spaces:
            if space.type == 'VIEW_3D' and space.local_view:
                in_local_view = True
                break
        
        if in_local_view:
            try:
                with context.temp_override(window=view_3d_window, area=view_3d_area):
                    bpy.ops.view3d.localview()
                self.report({'INFO'}, "Exited local view")
            except Exception as e:
                self.report({'WARNING'}, f"Could not exit local view: {e}")
            return {'FINISHED'}

        objects_to_show = set()
        checked_nodes = set()
        visited_blueprints = set()

        def collect_objects(current_node):
            if current_node in checked_nodes: 
                return
            checked_nodes.add(current_node)

            if getattr(current_node, "bl_idname", "") == 'SSMTNode_Object_Info':
                obj_name = getattr(current_node, "object_name", "")
                if obj_name:
                    obj = bpy.data.objects.get(obj_name)
                    if obj:
                        objects_to_show.add(obj)


            if hasattr(current_node, "inputs"):
                for inp in current_node.inputs:
                    if inp.is_linked:
                        for link in inp.links:
                            collect_objects(link.from_node)

        collect_objects(node)
        
        if not objects_to_show:
            self.report({'WARNING'}, "No objects found in this group")
            return {'CANCELLED'}

        def deselect_all_safe():
            for o in bpy.context.selected_objects:
                o.select_set(False)

        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        deselect_all_safe()
        for obj in objects_to_show:
            obj.select_set(True)

        region = next((r for r in view_3d_area.regions if r.type == 'WINDOW'), None)
        if region:
            try:
                with context.temp_override(window=view_3d_window, area=view_3d_area, region=region):
                    bpy.ops.view3d.localview()
                    bpy.ops.view3d.view_axis(type='FRONT')
                    bpy.ops.view3d.view_selected()
                    if view_3d_area.spaces.active:
                        view_3d_area.spaces.active.shading.type = 'SOLID'
            except Exception as e:
                print(f"View setup warning: {e}")

        self.report({'INFO'}, f"Showing {len(objects_to_show)} objects in local view")
        return {'FINISHED'}


classes = (
    SSMT_OT_RefreshNodeObjectIDs,
    SSMT_OT_SelectNodeObject,
    SSMT_OT_StartPickObject,
    SSMT_OT_PickObjectModal,
    SSMT_OT_View_Group_Objects,
    SSMTNode_Object_Info,
    SSMTNode_Object_Group,
    SSMTNode_Result_Output,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(draw_view3d_header)


def unregister():
    bpy.types.VIEW3D_HT_header.remove(draw_view3d_header)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
