import bpy
import os
import time
from bpy.types import NodeTree, Node, NodeSocket

from ..common.logic_name import LogicName
from ..common.global_config import GlobalConfig
from ..common.global_properties import DEDUPLICATE_ELEMENT_TYPES, GlobalProterties
from ..common.object_prefix_helper import ObjectPrefixHelper
from .node_base import SSMTBlueprintTree, SSMTNodeBase

BLENDER_VERSION = bpy.app.version[:2]

_picking_node_name = None
_picking_tree_name = None
_is_viewing_group_objects = False
_PICK_OBJECT_TIMEOUT_SECONDS = 30.0



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
                    
                    elif node.bl_idname == 'SSMTNode_Object_Info_Pro':
                        # 物体信息 Pro：逐条目刷新（一个节点 = 多个物体信息节点）
                        pro_changed = False
                        for item in getattr(node, 'object_list', []):
                            obj_name = getattr(item, 'object_name', '')
                            if not obj_name:
                                continue
                            obj = bpy.data.objects.get(obj_name)
                            if obj:
                                new_name = obj.name
                                new_obj_id = str(obj.as_pointer())
                                if item.object_name != new_name:
                                    item.object_name = new_name
                                    updated_count += 1
                                    pro_changed = True
                                if getattr(item, 'object_id', '') != new_obj_id:
                                    item.object_id = new_obj_id
                                    updated_count += 1
                                    pro_changed = True
                            elif getattr(item, 'original_object_name', ''):
                                orig_obj = bpy.data.objects.get(item.original_object_name)
                                if orig_obj:
                                    item.object_name = item.original_object_name
                                    item.object_id = str(orig_obj.as_pointer())
                                    updated_count += 1
                                    pro_changed = True

                        # 列表为空时节点自身就是「单物体模式」的引用
                        if not getattr(node, 'object_list', []) and getattr(node, 'object_name', ''):
                            obj = bpy.data.objects.get(node.object_name)
                            if obj:
                                new_obj_id = str(obj.as_pointer())
                                if getattr(node, 'object_id', '') != new_obj_id:
                                    node.object_id = new_obj_id
                                    updated_count += 1
                            elif getattr(node, 'object_id', ''):
                                node.object_id = ""
                                updated_count += 1

                        if pro_changed:
                            for item in getattr(node, 'object_list', []):
                                refresh_object_info_pro_item(item)
                            _refresh_object_info_pro_node(node)

                    elif node.bl_idname == 'SSMTNode_MultiFile_Export':
                        for item in node.object_list:
                            obj_name = getattr(item, 'object_name', '')
                            if obj_name:
                                obj = bpy.data.objects.get(obj_name)
                                if obj:
                                    new_name = obj.name
                                    new_obj_id = str(obj.as_pointer())
                                    if item.object_name != new_name:
                                        item.object_name = new_name
                                        updated_count += 1
                                    if getattr(item, 'object_id', '') != new_obj_id:
                                        item.object_id = new_obj_id
                                        updated_count += 1
                                elif getattr(item, 'original_object_name', ''):
                                    orig_obj = bpy.data.objects.get(item.original_object_name)
                                    if orig_obj:
                                        item.object_name = item.original_object_name
                                        item.object_id = str(orig_obj.as_pointer())
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

    def _finish_picking(self, context, status, clear_globals=True):
        global _picking_node_name, _picking_tree_name

        timer = getattr(self, "_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            self._timer = None

        if clear_globals:
            _picking_node_name = None
            _picking_tree_name = None

        return status

    def _get_current_selected_object(self, context):
        active_obj = getattr(context.view_layer.objects, "active", None)
        if active_obj and active_obj in context.selected_objects:
            return active_obj

        if context.selected_objects:
            return context.selected_objects[0]

        return None

    def _try_apply_selected_object(self, context):
        global _picking_node_name, _picking_tree_name

        current_obj = self._get_current_selected_object(context)
        if current_obj is None:
            return None

        if current_obj == self._last_selected_obj and current_obj in self._initial_selected_objs:
            return None

        tree = bpy.data.node_groups.get(_picking_tree_name)
        if tree is None:
            self.report({'WARNING'}, "节点树已失效，已取消吸管选择")
            return self._finish_picking(context, {'CANCELLED'})

        node = tree.nodes.get(_picking_node_name)
        if node is None:
            self.report({'WARNING'}, "目标节点已不存在，已取消吸管选择")
            return self._finish_picking(context, {'CANCELLED'})

        node.object_name = current_obj.name
        self.report({'INFO'}, f"已选择物体: {current_obj.name}")
        return self._finish_picking(context, {'FINISHED'})
    
    def invoke(self, context, event):
        global _picking_node_name
        
        if not _picking_node_name:
            return {'CANCELLED'}
        
        self._initial_selected_objs = set(context.selected_objects)
        self._last_selected_obj = self._get_current_selected_object(context)
        self._started_at = time.monotonic()
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}
    
    def modal(self, context, event):
        global _picking_node_name, _picking_tree_name

        if not _picking_node_name or not _picking_tree_name:
            return self._finish_picking(context, {'CANCELLED'}, clear_globals=False)

        if time.monotonic() - self._started_at > _PICK_OBJECT_TIMEOUT_SECONDS:
            self.report({'WARNING'}, "吸管选择超时，已自动取消")
            return self._finish_picking(context, {'CANCELLED'})
        
        if event.type in {'ESC', 'RIGHTMOUSE'}:
            return self._finish_picking(context, {'CANCELLED'})

        if event.type == 'TIMER':
            result = self._try_apply_selected_object(context)
            if result is not None:
                return result
            return {'RUNNING_MODAL'}

        if event.type in {
            'LEFTMOUSE',
            'MIDDLEMOUSE',
            'MOUSEMOVE',
            'WHEELUPMOUSE',
            'WHEELDOWNMOUSE',
            'TRACKPADPAN',
            'TRACKPADZOOM',
        }:
            return {'PASS_THROUGH'}

        return {'RUNNING_MODAL'}


class SSMT_OT_SelectGenerateModFolder(bpy.types.Operator):
    '''选择 Generate Mod 输出目录'''
    bl_idname = "ssmt.select_generate_mod_folder"
    bl_label = "选择 Generate Mod 输出目录"
    bl_options = {'REGISTER', 'INTERNAL'}

    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def invoke(self, context, event):
        current_dir = context.scene.global_properties.generate_mod_folder_path
        if current_dir:
            self.directory = bpy.path.abspath(current_dir)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        selected_dir = bpy.path.abspath(self.directory).strip()
        if not selected_dir:
            self.report({'WARNING'}, "未选择输出目录")
            return {'CANCELLED'}

        context.scene.global_properties.generate_mod_folder_path = os.path.normpath(selected_dir)
        return {'FINISHED'}


def draw_view3d_header(self, context):
    global _picking_node_name
    if _picking_node_name:
        self.layout.label(text="请在3D视图中点击选择一个物体...", icon='EYEDROPPER')


class ObjectInfoNodeMixin:
    """物体信息节点通用逻辑（普通节点与 Pro 节点共用）。

    注意：Blender 不允许「继承已注册的 Node 子类再注册」——那样会让父类节点
    无法再创建。所以 Pro 节点与普通物体信息节点共用本 mixin，而不是继承
    SSMTNode_Object_Info。
    """

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

    def _draw_object_picker_row(self, layout):
        """物体输入框行（搜索框 + 吸管 + 选中物体按钮）。"""
        row = layout.row(align=True)

        row.prop_search(self, "object_name", bpy.data, "objects", text="", icon='OBJECT_DATA')

        op = row.operator("ssmt.start_pick_object", text="", icon='EYEDROPPER')
        op.node_name = self.name

        if self.object_name:
            obj = bpy.data.objects.get(self.object_name)
            if obj:
                if obj.type != 'MESH':
                    row.label(text="", icon='ERROR')
                    layout.label(text="错误: 仅支持网格物体", icon='ERROR')
                else:
                    op = row.operator("ssmt.select_node_object", text="", icon='RESTRICT_SELECT_OFF')
                    op.object_name = self.object_name

                    if not self.object_id:
                        self.object_id = str(obj.as_pointer())
            else:
                row.label(text="", icon='ERROR')

    def _draw_object_prefix_row(self, layout, prefix_override=None):
        """只读前缀行（普通节点显示节点前缀，Pro 可传入活动项前缀）。"""
        row = layout.row(align=True)
        row.enabled = False
        if prefix_override is None:
            row.prop(self, "object_prefix", text="前缀")
        else:
            row.label(text=f"前缀: {prefix_override}" if prefix_override else "前缀: （无）")

    def _draw_single_object_rows(self, layout):
        """单物体模式的物体行（普通节点使用）。"""
        self._draw_object_picker_row(layout)
        self._draw_object_prefix_row(layout)


class SSMTNode_Object_Info(ObjectInfoNodeMixin, SSMTNodeBase):
    """Object Info Node"""
    bl_idname = 'SSMTNode_Object_Info'
    bl_label = 'Object Info'
    bl_icon = 'OBJECT_DATAMODE'
    bl_width_min = 300

    object_name: bpy.props.StringProperty(
        name="Object Name", default="", update=ObjectInfoNodeMixin.update_object_name
    )
    object_id: bpy.props.StringProperty(name="Object ID", default="")
    original_object_name: bpy.props.StringProperty(name="Original Object Name", default="")
    object_prefix: bpy.props.StringProperty(
        name="Prefix", default="", update=ObjectInfoNodeMixin.update_object_prefix
    )
    prefix_separator: bpy.props.StringProperty(name="Prefix Separator", default=".")

    draw_ib: bpy.props.StringProperty(name="DrawIB", default="")
    index_count: bpy.props.StringProperty(name="IndexCount", default="")
    first_index: bpy.props.StringProperty(name="FirstIndex", default="")
    component: bpy.props.StringProperty(name="Component", default="")
    alias_name: bpy.props.StringProperty(name="Alias Name", default="")

    def init(self, context):
        super().init(context)

    def draw_buttons(self, context, layout):
        self._draw_single_object_rows(layout)


# ===========================================================================
# 物体信息 Pro：一个节点 = 多个物体信息节点
#
# 设计要点：
#   1. Pro 节点内部用 object_list 管理多个物体条目；
#   2. 导出入口（collect_connected_start_nodes）把 Pro 节点展开成多个
#      ObjectInfoEntryView —— 它们伪装成普通 SSMTNode_Object_Info 节点，
#      属性直通列表项。因此链式遍历与所有下游处理节点无需任何改动，
#      拿到的就是 N 个普通物体信息节点（一个 Pro 节点 = 过去的 N 个节点）；
#   3. 列表为空时回退为单物体模式，行为与普通物体信息节点完全一致。
# ===========================================================================
OBJECT_INFO_IDNAME = 'SSMTNode_Object_Info'
OBJECT_INFO_PRO_IDNAME = 'SSMTNode_Object_Info_Pro'
OBJECT_INFO_NODE_IDNAMES = (OBJECT_INFO_IDNAME, OBJECT_INFO_PRO_IDNAME)

_PRO_DEBUG = True


def _pro_log(message: str) -> None:
    """物体信息 Pro 的统一调试输出（交付保留开关）。"""
    if _PRO_DEBUG:
        print(f"[物体信息Pro] {message}")


def is_object_info_node(node) -> bool:
    """节点（或条目视图）是否应按物体信息节点读取。"""
    return getattr(node, 'bl_idname', '') in OBJECT_INFO_NODE_IDNAMES


def is_object_info_pro_node(node) -> bool:
    """真实节点对象是否就是物体信息 Pro 节点（条目视图返回 False）。"""
    return getattr(node, 'bl_idname', '') == OBJECT_INFO_PRO_IDNAME


def find_object_by_pointer(object_id: str):
    """按物体 ID（as_pointer）找回物体：物体改名后列表名字失效时的兜底解析。"""
    pointer = str(object_id or '').strip()
    if not pointer:
        return None
    for obj in bpy.data.objects:
        if obj is None:
            continue
        try:
            if str(obj.as_pointer()) == pointer:
                return obj
        except (AttributeError, ReferenceError):
            continue
    return None


def resolve_object_info_entry_object_name(entry) -> str:
    """取条目当前真实物体名：列表里的名字失效时按物体 ID 兜底。

    物体刚在 Blender 里改名、同步模块还没把新名字写回列表时，显示、收集与
    导出都用这个兜底名，避免「找不到物体」或按旧名导出失败。
    """
    name = str(getattr(entry, 'object_name', '') or '')
    if name and bpy.data.objects.get(name) is not None:
        return name
    object_id = str(getattr(entry, 'object_id', '') or '')
    if object_id:
        obj = find_object_by_pointer(object_id)
        if obj is not None:
            return obj.name
    return name


def refresh_object_info_pro_item(item) -> None:
    """按物体名刷新 Pro 列表项的前缀与派生字段（draw_ib 等）。

    ``item`` 也可以是 Pro 节点自身（回退单物体模式时复用），两者都有
    object_name / object_prefix / prefix_separator 字段。
    """
    object_name = str(getattr(item, 'object_name', '') or '')
    prefix = str(getattr(item, 'object_prefix', '') or '')
    separator = str(getattr(item, 'prefix_separator', '') or '.')

    parsed = ObjectPrefixHelper.extract_prefix_info(object_name)
    if parsed:
        prefix, separator = parsed
        item.object_prefix = ObjectPrefixHelper.normalize_prefix(prefix)
        item.prefix_separator = separator

    parts = ObjectPrefixHelper.parse_prefix_parts(prefix)
    item.draw_ib = parts.get('draw_ib', '')
    item.index_count = parts.get('index_count', '')
    item.first_index = parts.get('first_index', '')
    item.component = parts.get('component', '')
    _, _, base_name = ObjectPrefixHelper.split_name_and_prefix(object_name, prefix, separator)
    item.alias_name = base_name

    if is_object_info_pro_node(item) and hasattr(item, 'update_node_width'):
        try:
            item.update_node_width([object_name, prefix])
        except Exception as exc:
            _pro_log(f"[refresh][item] 宽度刷新失败: {exc}")
    _pro_log(
        f"[refresh][item] object='{object_name}' prefix='{prefix}' "
        f"draw_ib='{getattr(item, 'draw_ib', '')}' first_index='{getattr(item, 'first_index', '')}'"
    )


def iter_object_info_entries(node):
    """枚举物体信息节点上的物体条目（条目视图口径，与导出入口一致）。

    普通物体信息节点只有节点自身一个条目；Pro 节点列表非空时每项一个视图，
    列表为空时回退到映射节点自身的单个视图。条目都带 object_name /
    object_prefix 等属性，可直接交给 ObjectPrefixHelper 解析。
    """
    yield from build_object_info_entry_views(node)


def iter_object_info_object_names(node):
    """按出现顺序产出非空的物体名（普通节点 / Pro 节点通用）。"""
    for entry in iter_object_info_entries(node):
        object_name = str(getattr(entry, 'object_name', '') or '').strip()
        if object_name:
            yield object_name


class ObjectInfoEntryView:
    """Pro 节点单个物体条目的只读视图，伪装成普通物体信息节点。

    下游代码只做属性读取（bl_idname / object_name / object_prefix / mute /
    id_data / name 等），因此这里不需要继承 bpy 类型：``bl_idname`` 固定为
    ``SSMTNode_Object_Info``，未显式定义的属性一律回落到真实的 Pro 节点。
    ``name`` 带上条目序号，保证 ``_get_node_unique_key`` 等在展开后依然唯一。
    """

    bl_idname = OBJECT_INFO_IDNAME

    def __init__(self, node, entry, index: int):
        object.__setattr__(self, '_pro_node', node)
        object.__setattr__(self, '_pro_entry', entry)
        object.__setattr__(self, '_pro_index', index)

    # -- 节点基础属性 ---------------------------------------------------- #
    @property
    def name(self) -> str:
        base_name = str(getattr(self._pro_node, 'name', '') or '')
        if self._pro_index >= 0:
            return f"{base_name}#{self._pro_index}"
        return base_name

    @property
    def label(self) -> str:
        entry_label = str(getattr(self._pro_entry, 'object_name', '') or '')
        return entry_label or str(getattr(self._pro_node, 'label', '') or '')

    # -- 条目级属性（下游真正读取的数据） -------------------------------- #
    @property
    def object_name(self) -> str:
        # 列表里的名字失效（物体刚改名、同步未写回）时按物体 ID 兜底，
        # 保证链式遍历 / 导出永远拿到真实物体名。
        return resolve_object_info_entry_object_name(self._pro_entry)

    @property
    def object_id(self) -> str:
        return str(getattr(self._pro_entry, 'object_id', '') or '')

    @property
    def original_object_name(self) -> str:
        return str(getattr(self._pro_entry, 'original_object_name', '') or '')

    @property
    def object_prefix(self) -> str:
        return str(getattr(self._pro_entry, 'object_prefix', '') or '')

    @property
    def prefix_separator(self) -> str:
        return str(getattr(self._pro_entry, 'prefix_separator', '') or '.')

    @property
    def draw_ib(self) -> str:
        return str(getattr(self._pro_entry, 'draw_ib', '') or '')

    @property
    def index_count(self) -> str:
        return str(getattr(self._pro_entry, 'index_count', '') or '')

    @property
    def first_index(self) -> str:
        return str(getattr(self._pro_entry, 'first_index', '') or '')

    @property
    def component(self) -> str:
        return str(getattr(self._pro_entry, 'component', '') or '')

    @property
    def alias_name(self) -> str:
        return str(getattr(self._pro_entry, 'alias_name', '') or '')

    # -- 与普通物体信息节点一致的方法 ------------------------------------ #
    def get_effective_prefix(self) -> str:
        prefix_info = ObjectPrefixHelper.get_node_prefix_info(self)
        return prefix_info[0] if prefix_info else ""

    def get_effective_separator(self) -> str:
        prefix_info = ObjectPrefixHelper.get_node_prefix_info(self)
        return prefix_info[1] if prefix_info else (self.prefix_separator or ".")

    def get_virtual_object_name(self) -> str:
        return ObjectPrefixHelper.build_virtual_object_name_for_node(self)

    # -- 其它属性一律回落到真实节点 -------------------------------------- #
    def __getattr__(self, attr):
        try:
            node = object.__getattribute__(self, '_pro_node')
        except AttributeError:
            raise AttributeError(attr)
        return getattr(node, attr)

    def __repr__(self) -> str:
        return (
            f"<ObjectInfoEntryView {self.name} object='{self.object_name}' "
            f"prefix='{self.object_prefix}'>"
        )


def build_object_info_entry_views(node):
    """把物体信息节点展开成「条目视图」列表（下游零改动读取多物体）。

    普通物体信息节点原样返回（只含自身）；Pro 节点列表非空时每项一个视图，
    列表为空时回退成映射到节点自身的单个视图。所有视图的 ``bl_idname`` 都是
    ``SSMTNode_Object_Info``，因此链式遍历与下游处理节点拿到的就是普通节点。
    """
    if not is_object_info_pro_node(node):
        return [node]

    items = list(getattr(node, 'object_list', []) or [])
    if not items:
        object_name = str(getattr(node, 'object_name', '') or '').strip()
        if not object_name:
            _pro_log(f"[expand] '{getattr(node, 'name', '?')}' 空节点，无物体条目")
            return []
        _pro_log(f"[expand] '{getattr(node, 'name', '?')}' 列表为空但有单物体，回退单物体模式")
        return [ObjectInfoEntryView(node, node, -1)]

    views = [ObjectInfoEntryView(node, item, index) for index, item in enumerate(items)]
    _pro_log(
        f"[expand] '{getattr(node, 'name', '?')}' -> {len(views)} 个物体条目: "
        f"{[view.object_name for view in views]}"
    )
    return views


class ObjectInfoProItem(bpy.types.PropertyGroup):
    """物体信息 Pro 的单个物体条目（字段与物体信息节点一致）。"""
    object_name: bpy.props.StringProperty(name="物体名称", default="")  # type: ignore
    object_id: bpy.props.StringProperty(name="物体ID", default="")  # type: ignore
    original_object_name: bpy.props.StringProperty(name="原始物体名称", default="")  # type: ignore
    object_prefix: bpy.props.StringProperty(name="前缀", default="")  # type: ignore
    prefix_separator: bpy.props.StringProperty(name="前缀分隔符", default=".")  # type: ignore
    draw_ib: bpy.props.StringProperty(name="DrawIB", default="")  # type: ignore
    index_count: bpy.props.StringProperty(name="IndexCount", default="")  # type: ignore
    first_index: bpy.props.StringProperty(name="FirstIndex", default="")  # type: ignore
    component: bpy.props.StringProperty(name="Component", default="")  # type: ignore
    alias_name: bpy.props.StringProperty(name="别名", default="")  # type: ignore


class SSMT_UL_OBJECT_INFO_PRO(bpy.types.UIList):
    """物体信息 Pro 的物体列表（行内 X 按钮按 index 删除对应项）。"""

    bl_idname = "SSMT_UL_OBJECT_INFO_PRO"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        node = data
        object_name = str(getattr(item, 'object_name', '') or '')
        obj = bpy.data.objects.get(object_name) if object_name else None
        display_name = object_name
        pending_sync = False

        # 物体刚改名、同步模块还没写回列表时：按物体 ID 兜底显示真实新名，
        # 标注「同步中」而不是误报「找不到物体」。
        if object_name and obj is None:
            resolved_obj = find_object_by_pointer(getattr(item, 'object_id', ''))
            if resolved_obj is not None:
                obj = resolved_obj
                display_name = resolved_obj.name
                pending_sync = True

        if not object_name:
            item_icon = 'ERROR'
            text = f"{index + 1}. （空）"
        elif obj is None:
            item_icon = 'ERROR'
            text = f"{index + 1}. {object_name}（找不到物体）"
        elif getattr(obj, 'type', '') != 'MESH':
            item_icon = 'ERROR'
            text = f"{index + 1}. {display_name}（非网格）"
        elif pending_sync:
            item_icon = 'FILE_REFRESH'
            text = f"{index + 1}. {display_name}（同步中）"
        else:
            item_icon = 'OBJECT_DATA'
            text = f"{index + 1}. {display_name}"

        row = layout.row(align=True)
        row.label(text=text, icon=item_icon)

        op_remove = row.operator("ssmt.object_info_pro_remove", text="", icon='X')
        op_remove.node_name = str(getattr(node, 'name', '') or '')
        op_remove.index = index


def _resolve_object_info_pro_node(context, node_name):
    """从节点编辑器上下文按名称取回物体信息 Pro 节点。

    节点编辑器上下文拿不到时（面板按钮 / 其它上下文触发）回退到全局蓝图树
    按名字查找，避免算子只能在节点编辑器里用。
    """
    space_data = getattr(context, 'space_data', None)
    tree = None
    if space_data is not None:
        tree = getattr(space_data, 'edit_tree', None) or getattr(space_data, 'node_tree', None)
    node = tree.nodes.get(node_name) if tree else None
    if node is None or not is_object_info_pro_node(node):
        for node_group in bpy.data.node_groups:
            if getattr(node_group, 'bl_idname', '') != 'SSMTBlueprintTreeType':
                continue
            candidate = node_group.nodes.get(node_name)
            if candidate is not None and is_object_info_pro_node(candidate):
                node = candidate
                break
    if node is None or not is_object_info_pro_node(node):
        return None
    return node


def _collect_selected_mesh_objects(context):
    """取当前选中的网格物体（无选中时回退到活动物体），保持顺序去重。"""
    objects = [
        obj for obj in (getattr(context, 'selected_objects', None) or [])
        if getattr(obj, 'type', '') == 'MESH'
    ]
    if not objects:
        active_object = getattr(context, 'active_object', None)
        if active_object is not None and getattr(active_object, 'type', '') == 'MESH':
            objects = [active_object]
    return objects


def _refresh_object_info_pro_node(node) -> None:
    """列表变化后统一刷新节点宽度、label 与活动索引。"""
    if not is_object_info_pro_node(node):
        return
    names = [str(item.object_name or '') for item in node.object_list]
    if names:
        node.active_object_index = max(0, min(int(node.active_object_index), len(names) - 1))
        node.update_node_width(names)
    else:
        node.active_object_index = 0
        node.update_node_width([getattr(node, 'object_name', '')])
    _pro_log(
        f"[refresh][node] '{getattr(node, 'name', '?')}' 列表={len(names)} "
        f"active={int(node.active_object_index)}"
    )


def _pro_update_object_name(self, context):
    """物体信息 Pro：输入框 / 吸管选中的物体自动加入列表。

    用户要求「输入框添加的物体应该加入到列表中，而不是单独一个风格」——
    这里把单物体输入统一转成列表项；批量添加仍走「添加选中物体」按钮。
    """
    self._refresh_prefix_cache()
    if not is_object_info_pro_node(self):
        return
    object_name = str(getattr(self, 'object_name', '') or '').strip()
    if not object_name:
        return
    try:
        self._ensure_object_in_list(object_name)
    except Exception as exc:
        _pro_log(f"[picker] 输入框物体自动入列表失败: {exc}")


def _pro_update_active_index(self, context):
    """列表活动项切换时，把输入框同步成该项物体（前缀行随之切换）。"""
    if not is_object_info_pro_node(self):
        return
    if not self.object_list:
        return
    index = max(0, min(int(self.active_object_index), len(self.object_list) - 1))
    object_name = str(self.object_list[index].object_name or '')
    if object_name and str(getattr(self, 'object_name', '') or '') != object_name:
        self.object_name = object_name


class SSMT_OT_ObjectInfoPro_AddSelected(bpy.types.Operator):
    """把当前选中的网格物体批量加入物体列表"""
    bl_idname = "ssmt.object_info_pro_add_selected"
    bl_label = "添加选中物体"
    bl_description = "把当前选中的网格物体批量加入物体列表（自动去重）"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_object_info_pro_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择物体信息 Pro 节点")
            _pro_log("[add] 找不到节点")
            return {'CANCELLED'}

        objects = _collect_selected_mesh_objects(context)
        existing = {str(item.object_name or '') for item in node.object_list}

        # 列表为空时，把节点当前的单物体一起带入列表，避免添加后丢失
        migrated = ""
        if not node.object_list and getattr(node, 'object_name', ''):
            item = node.object_list.add()
            item.object_name = str(node.object_name)
            item.object_id = str(getattr(node, 'object_id', '') or '')
            item.original_object_name = str(getattr(node, 'original_object_name', '') or '')
            item.object_prefix = str(getattr(node, 'object_prefix', '') or '')
            item.prefix_separator = str(getattr(node, 'prefix_separator', '') or '.')
            refresh_object_info_pro_item(item)
            existing.add(item.object_name)
            migrated = item.object_name
            _pro_log(f"[add] 单物体迁入列表: '{migrated}'")

        added_names = []
        for obj in objects:
            name = str(getattr(obj, 'name', '') or '').strip()
            if not name or name in existing:
                continue
            item = node.object_list.add()
            item.object_name = name
            item.object_id = str(obj.as_pointer())
            refresh_object_info_pro_item(item)
            existing.add(name)
            added_names.append(name)

        if not added_names and not migrated:
            if not objects:
                self.report({'WARNING'}, "请先选中要添加的网格物体")
            else:
                self.report({'INFO'}, "选中物体都已在列表中")
            _pro_log(f"[add] 未新增（选中 {len(objects)} 个，列表 {len(node.object_list)} 个）")
            return {'CANCELLED'}

        _refresh_object_info_pro_node(node)
        _pro_log(f"[add] 新增 {len(added_names)} 个: {added_names}")
        self.report(
            {'INFO'},
            f"已添加 {len(added_names)} 个物体（当前共 {len(node.object_list)} 个）"
        )
        return {'FINISHED'}


class SSMT_OT_ObjectInfoPro_RemoveSelected(bpy.types.Operator):
    """从物体列表移除指定项"""
    bl_idname = "ssmt.object_info_pro_remove"
    bl_label = "移除物体"
    bl_description = "从物体列表移除指定项"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore
    index: bpy.props.IntProperty(default=-1)  # type: ignore

    def execute(self, context):
        node = _resolve_object_info_pro_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择物体信息 Pro 节点")
            _pro_log("[remove] 找不到节点")
            return {'CANCELLED'}

        if not (0 <= self.index < len(node.object_list)):
            self.report({'WARNING'}, "列表中没有这一项")
            _pro_log(f"[remove] 无效索引 {self.index}（列表 {len(node.object_list)} 个）")
            return {'CANCELLED'}

        removed_name = str(node.object_list[self.index].object_name or '')
        node.object_list.remove(self.index)
        if not node.object_list:
            # 回到空节点：清掉单物体模式下残留的引用，避免意外导出旧物体
            node.object_name = ""
            node.object_id = ""
            node.original_object_name = ""
            node.object_prefix = ""
            node.label = ""
            _pro_log("[remove] 列表已空，清空节点单物体引用")
        _refresh_object_info_pro_node(node)
        _pro_log(f"[remove] 移除 '{removed_name}'，剩余 {len(node.object_list)} 个")
        self.report({'INFO'}, f"已移除 '{removed_name}'")
        return {'FINISHED'}


class SSMT_OT_ObjectInfoPro_Clear(bpy.types.Operator):
    """清空物体列表"""
    bl_idname = "ssmt.object_info_pro_clear"
    bl_label = "清空"
    bl_description = "清空物体列表"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()  # type: ignore

    def execute(self, context):
        node = _resolve_object_info_pro_node(context, self.node_name)
        if node is None:
            self.report({'ERROR'}, "请选择物体信息 Pro 节点")
            _pro_log("[clear] 找不到节点")
            return {'CANCELLED'}

        removed_count = len(node.object_list)
        node.object_list.clear()
        node.object_name = ""
        node.object_id = ""
        node.original_object_name = ""
        node.object_prefix = ""
        node.label = ""
        _refresh_object_info_pro_node(node)
        _pro_log(f"[clear] 清空 {removed_count} 个")
        self.report({'INFO'}, f"已清空 {removed_count} 个物体")
        return {'FINISHED'}


class SSMTNode_Object_Info_Pro(ObjectInfoNodeMixin, SSMTNodeBase):
    """物体信息 Pro：一个节点管理多个物体，导出行为与多个物体信息节点完全一致"""
    bl_idname = OBJECT_INFO_PRO_IDNAME
    bl_label = '物体信息 Pro'
    bl_icon = 'OBJECT_DATAMODE'
    bl_width_min = 340
    bl_description = (
        "一个节点管理多个物体：导出时等价于多个物体信息节点"
        "（输入框选中的物体会自动加入列表，也可点“添加选中物体”批量添加）"
    )

    # 与普通物体信息节点一致的字段；输入框选中物体会自动加入下方列表
    object_name: bpy.props.StringProperty(
        name="Object Name", default="", update=_pro_update_object_name
    )
    object_id: bpy.props.StringProperty(name="Object ID", default="")
    original_object_name: bpy.props.StringProperty(name="Original Object Name", default="")
    object_prefix: bpy.props.StringProperty(
        name="Prefix", default="", update=ObjectInfoNodeMixin.update_object_prefix
    )
    prefix_separator: bpy.props.StringProperty(name="Prefix Separator", default=".")

    draw_ib: bpy.props.StringProperty(name="DrawIB", default="")
    index_count: bpy.props.StringProperty(name="IndexCount", default="")
    first_index: bpy.props.StringProperty(name="FirstIndex", default="")
    component: bpy.props.StringProperty(name="Component", default="")
    alias_name: bpy.props.StringProperty(name="Alias Name", default="")

    # 多物体列表
    object_list: bpy.props.CollectionProperty(type=ObjectInfoProItem)  # type: ignore
    active_object_index: bpy.props.IntProperty(
        name="活动物体", default=0, min=0, update=_pro_update_active_index
    )  # type: ignore

    #: Pro 节点固定宽度：列表增删物体时宽度不跳变（普通节点仍按文字自适应）
    PRO_NODE_WIDTH = 380.0

    def update_node_width(self, texts=None):
        """宽度固定，避免输入框/列表增删物体时节点尺寸突变。"""
        self.width = self.PRO_NODE_WIDTH

    def init(self, context):
        super().init(context)
        self.width = self.PRO_NODE_WIDTH

    def _active_entry(self):
        """当前活动列表项（无列表时返回 None）。"""
        if not self.object_list:
            return None
        index = max(0, min(int(self.active_object_index), len(self.object_list) - 1))
        return self.object_list[index]

    def _ensure_object_in_list(self, object_name: str) -> bool:
        """确保物体在列表中（输入框 / 吸管添加用）；返回是否新增。"""
        object_name = str(object_name or '').strip()
        if not object_name:
            return False
        obj = bpy.data.objects.get(object_name)
        if obj is None or getattr(obj, 'type', '') != 'MESH':
            return False

        for index, item in enumerate(self.object_list):
            if str(item.object_name or '') == object_name:
                if int(self.active_object_index) != index:
                    self.active_object_index = index
                return False

        item = self.object_list.add()
        item.object_name = object_name
        item.object_id = str(obj.as_pointer())
        refresh_object_info_pro_item(item)
        self.active_object_index = len(self.object_list) - 1
        _refresh_object_info_pro_node(self)
        _pro_log(
            f"[picker] 输入框物体已加入列表: '{object_name}'"
            f"（共 {len(self.object_list)} 个）"
        )
        return True

    def draw_buttons(self, context, layout):
        # 物体输入框始终可用：搜索框 / 吸管选中的物体会自动加入下方列表
        self._draw_object_picker_row(layout)

        active_entry = self._active_entry()
        if active_entry is not None:
            # 活动项名字可能因物体刚改名而失效：按物体 ID 兜底后再解析前缀
            active_name = resolve_object_info_entry_object_name(active_entry)
            prefix_info = (
                ObjectPrefixHelper.extract_prefix_info(active_name)
                or ObjectPrefixHelper.get_node_prefix_info(active_entry)
            )
            self._draw_object_prefix_row(
                layout, prefix_override=(prefix_info[0] if prefix_info else "")
            )
        else:
            self._draw_object_prefix_row(layout)

        box = layout.box()
        header = box.row(align=True)
        header.label(text=f"物体列表（{len(self.object_list)} 个）", icon='OBJECT_DATA')

        if self.object_list:
            # list_id 必须每个节点唯一：Blender 的 template_list 按 list_id 共享
            # 运行时状态（列表高度、滚动位置）。空 list_id 会让所有 Pro 节点的
            # 列表高度/滚动联动（拖一个全动、滚动失效），这里用节点全名区分。
            tree_name = getattr(getattr(self, 'id_data', None), 'name', '') or ''
            list_id = f"object_info_pro::{tree_name}::{self.name}"
            box.template_list(
                "SSMT_UL_OBJECT_INFO_PRO", list_id,
                self, "object_list",
                self, "active_object_index",
                rows=max(2, min(len(self.object_list), 6)),
            )
        else:
            box.label(text="列表为空：用上面的输入框选物体，或点下方按钮批量添加", icon='INFO')

        row = box.row(align=True)
        row.operator(
            "ssmt.object_info_pro_add_selected", text="添加选中物体", icon='ADD'
        ).node_name = self.name
        op_remove = row.operator("ssmt.object_info_pro_remove", text="移除", icon='REMOVE')
        op_remove.node_name = self.name
        op_remove.index = int(self.active_object_index)
        row.operator("ssmt.object_info_pro_clear", text="清空", icon='TRASH').node_name = self.name

        if active_entry is not None:
            active_name = resolve_object_info_entry_object_name(active_entry)
            active_obj = bpy.data.objects.get(active_name) if active_name else None
            if active_obj is None:
                layout.label(text="错误: 活动物体不存在", icon='ERROR')
            elif getattr(active_obj, 'type', '') != 'MESH':
                layout.label(text="错误: 仅支持网格物体", icon='ERROR')


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

        layout.prop(self.id_data, "disable_copy_modifiers_on_export")
        layout.prop(self.id_data, "use_current_pose_shading")

        layout.prop(context.scene.global_properties, 
                    "forbid_auto_texture_ini",text="禁止自动贴图流程")

        layout.prop(context.scene.global_properties,
                    "recalculate_tangent_basis",text="重新计算标准切线(TBN)")

        if GlobalConfig.logic_name != LogicName.GF2:
            layout.prop(context.scene.global_properties,
                        "recalculate_tangent",text="向量归一化法线存入TANGENT(全局)")

        if GlobalConfig.logic_name == LogicName.HIMI:
            outline_box = layout.box()
            outline_box.label(text="轮廓线修复(HI3 2.0)：COLOR 通道", icon='COLOR')
            outline_box.prop(context.scene.global_properties, "recalculate_color_mode", text="")
            outline_box.prop(context.scene.global_properties,
                            "recalculate_color_width", text="描边宽度(0.5=游戏原版, 越小越细)")

        if LogicName.is_zzmi_family(GlobalConfig.logic_name):
            layout.prop(context.scene.global_properties, "zzz_use_slot_fix")

        if GlobalConfig.logic_name == LogicName.GIMI:
            layout.prop(context.scene.global_properties, "gimi_use_orfix")

        if GlobalConfig.logic_name == LogicName.EFMI:
            layout.prop(context.scene.global_properties, "use_rabbitfx_slot")
            layout.prop(context.scene.global_properties, "use_dhash_texture")
            layout.prop(context.scene.global_properties, "efmi_pass_mirror_enabled")

        from .export_helper import BlueprintExportHelper
        has_mod_panel_node = BlueprintExportHelper.has_mod_panel_node(tree=self.id_data)
        info_box = layout.box()
        if has_mod_panel_node:
            info_box.label(text="已检测到 Mod面板 节点，导出时会生成分支 Mod 面板", icon='MENU_PANEL')
        else:
            info_box.label(text="如需生成分支 Mod 面板，请在蓝图中添加 Mod面板 节点", icon='INFO')

        layout.prop(context.scene.global_properties, "open_mod_folder_after_generate_mod",text="生成Mod后打开Mod所在文件夹")

        output_tree = self.id_data
        layout.prop(output_tree, "use_specific_generate_mod_folder_path")

        if getattr(output_tree, "use_specific_generate_mod_folder_path", False):
            box = layout.box()
            box.label(text="当前生成Mod位置文件夹:")
            box.prop(output_tree, "generate_mod_folder_path", text="")

        # 放在「指定文件夹」及其路径框之后，避免路径框跟到别的勾选项下面
        layout.prop(output_tree, "use_blueprint_name_generate_mod_folder")

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
            col.separator()
            col.prop(context.scene.global_properties, "deduplicate_ignore_vertex_id")
            if context.scene.global_properties.deduplicate_ignore_vertex_id:
                hint_box = box.box()
                hint_col = hint_box.column()
                hint_col.label(text="缝合边(Mark Seam)标记的顶点：只比位置+UV、不看顶点编号", icon='INFO')
                hint_col.label(text="没标记的顶点：照旧按勾选类型+顶点编号去重", icon='INFO')
            # 全勾＝面板默认＝历史的全字段去重键。显式改过勾选后导出就按勾选去重。
            # 属性重算在去重之前完成（见 utils/export_utils.py），不需要为它补任何类型。
            properties = context.scene.global_properties
            if not all(
                getattr(properties, f"deduplicate_{element_type}")
                for element_type in DEDUPLICATE_ELEMENT_TYPES
            ):
                hint_box = box.box()
                hint_col = hint_box.column()
                hint_col.label(text="已按勾选类型去重（全勾＝历史全字段）", icon='INFO')

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

    @staticmethod
    def _node_visit_key(node):
        tree_name = node.id_data.name if hasattr(node, "id_data") and node.id_data else ""
        node_name = getattr(node, "name", "")
        return f"{tree_name}::{node_name}"

    @staticmethod
    def _append_object_by_name(objects_to_show, obj_name):
        obj_name = str(obj_name or "").strip()
        if not obj_name:
            return
        obj = bpy.data.objects.get(obj_name)
        if obj:
            objects_to_show.add(obj)

    def _collect_group_preview_objects(self, current_node, objects_to_show, visited_nodes, visited_blueprints):
        node_key = self._node_visit_key(current_node)
        if node_key in visited_nodes:
            return
        visited_nodes.add(node_key)

        node_type = getattr(current_node, "bl_idname", "")
        if node_type == 'SSMTNode_Object_Info':
            self._append_object_by_name(objects_to_show, getattr(current_node, "object_name", ""))
        elif node_type == 'SSMTNode_Object_Info_Pro':
            for item in getattr(current_node, "object_list", []):
                self._append_object_by_name(objects_to_show, getattr(item, "object_name", ""))
        elif node_type == 'SSMTNode_MultiFile_Export':
            for item in getattr(current_node, "object_list", []):
                self._append_object_by_name(objects_to_show, getattr(item, "object_name", ""))
        elif node_type == 'SSMTNode_Blueprint_Nest':
            nested_tree_name = str(getattr(current_node, "blueprint_name", "") or "").strip()
            if nested_tree_name and nested_tree_name != 'NONE' and nested_tree_name not in visited_blueprints:
                nested_tree = bpy.data.node_groups.get(nested_tree_name)
                if nested_tree and getattr(nested_tree, "bl_idname", "") == 'SSMTBlueprintTreeType':
                    visited_blueprints.add(nested_tree_name)
                    for nested_node in nested_tree.nodes:
                        if getattr(nested_node, "bl_idname", "") == 'SSMTNode_Result_Output':
                            self._collect_group_preview_objects(
                                nested_node,
                                objects_to_show,
                                visited_nodes,
                                visited_blueprints,
                            )

        if hasattr(current_node, "inputs"):
            for inp in current_node.inputs:
                if inp.is_linked:
                    for link in inp.links:
                        self._collect_group_preview_objects(
                            link.from_node,
                            objects_to_show,
                            visited_nodes,
                            visited_blueprints,
                        )

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
        self._collect_group_preview_objects(node, objects_to_show, checked_nodes, visited_blueprints)

        view_layer_object_names = {
            obj.name
            for obj in getattr(context.view_layer, "objects", []) or []
        }
        objects_to_show = {
            obj
            for obj in objects_to_show
            if obj is not None and getattr(obj, "name", "") in view_layer_object_names
        }
        
        if not objects_to_show:
            self.report({'WARNING'}, "No selectable objects found in this group for the current View Layer")
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
    SSMT_OT_SelectGenerateModFolder,
    SSMT_OT_View_Group_Objects,
    ObjectInfoProItem,
    SSMT_UL_OBJECT_INFO_PRO,
    SSMT_OT_ObjectInfoPro_AddSelected,
    SSMT_OT_ObjectInfoPro_RemoveSelected,
    SSMT_OT_ObjectInfoPro_Clear,
    SSMTNode_Object_Info,
    SSMTNode_Object_Info_Pro,
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
