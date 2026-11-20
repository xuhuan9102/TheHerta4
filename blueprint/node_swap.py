"""
Object swap node support.
"""

from dataclasses import dataclass

import bpy

from ..common.object_prefix_helper import ObjectPrefixHelper
from .node_base import SSMTNodeBase
from .variable_registry import ensure_object_swap_variable_name, mark_variable_name_used, normalize_variable_name

_swap_preview_hidden_state = None
_swap_preview_session_owner = None
_is_button_context_menu_hooked = False


@dataclass
class SwapKeyConfig:
    node_id: str = ""
    index: int = 0
    hotkey: str = ""
    swap_type: str = "cycle"
    option_count: int = 2
    comment: str = ""
    custom_var_name: str = ""
    assigned_variable_name: str = ""

    def get_swap_key_name(self) -> str:
        if self.custom_var_name:
            return f"${self.custom_var_name}"
        if self.assigned_variable_name:
            return f"${self.assigned_variable_name}"
        return f"$swapkey{self.index}"

    def get_key_swap_section_name(self) -> str:
        return f"KeySwap_{self.index}"

    def get_active_param_name(self) -> str:
        return f"$active{self.index}"

    def get_condition_value(self, option_index: int) -> str:
        return str(option_index)

    def get_condition_str(self, value: int = 1) -> str:
        return f"{self.get_swap_key_name()} == {value}"


class SSMTNode_ObjectSwap(SSMTNodeBase):
    bl_idname = "SSMTNode_ObjectSwap"
    bl_label = "物体切换"
    bl_icon = "SHADERFX"

    def _ensure_initial_visible_variable_name(self, context=None):
        if getattr(self, "custom_var_initialized", False):
            return False

        assigned_name = ensure_object_swap_variable_name(self, context=context)
        if not assigned_name:
            return False

        if str(getattr(self, "custom_var_name", "") or "").strip():
            self.custom_var_initialized = True
            return False

        self.custom_var_initialized = True
        self.custom_var_name = assigned_name
        return True

    def update_all_properties(self, context):
        if self._ensure_initial_visible_variable_name(context=context):
            return

        normalized = normalize_variable_name(self.custom_var_name)
        if normalized != str(self.custom_var_name or "").strip().lstrip("$"):
            self.custom_var_name = normalized
            return
        if normalized:
            mark_variable_name_used(normalized, context=context)
            self.custom_var_initialized = True
        ensure_object_swap_variable_name(self, context=context)
        self.update_node_width([self.comment, self.hotkey, self.custom_var_name, self.assigned_variable_name])

    comment: bpy.props.StringProperty(
        name="备注",
        description="生成到 KeySwap 段落中的注释。",
        default="",
        update=update_all_properties,
    )

    custom_var_name: bpy.props.StringProperty(
        name="变量名",
        description="节点创建时会自动填入预分配变量名，你可以直接复制或手动修改它。",
        default="",
        update=update_all_properties,
    )

    assigned_variable_name: bpy.props.StringProperty(
        name="Assigned Variable Name",
        description="Preallocated global variable name for this node.",
        default="",
        options={"HIDDEN"},
    )

    custom_var_initialized: bpy.props.BoolProperty(
        name="Custom Variable Initialized",
        default=False,
        options={"HIDDEN"},
    )

    hotkey: bpy.props.StringProperty(
        name="快捷键",
        description="按键格式: Modifier KeyName；留空时仅通过变量或切换面板控制。",
        default="",
        update=update_all_properties,
    )

    swap_type: bpy.props.EnumProperty(
        name="切换类型",
        description="切换模式。",
        items=[
            ("cycle", "循环切换", "循环切换所有选项"),
            ("toggle", "开关切换", "在两个选项间切换"),
            ("hold", "按住生效", "按住时激活"),
        ],
        default="cycle",
        update=update_all_properties,
    )

    def update_input_slot_count(self, context):
        self._update_input_sockets()
        self._sanitize_preview_option_index()

    input_slot_count: bpy.props.IntProperty(
        name="输入口数量",
        description="每个输入口对应一个切换选项。",
        min=1,
        max=1024,
        default=2,
        update=update_input_slot_count,
    )

    condition_operator: bpy.props.EnumProperty(
        name="条件运算符",
        description="多个切换条件之间的逻辑运算符。",
        items=[
            ("&&", "AND (&&)", "所有条件都满足时执行"),
            ("||", "OR (||)", "任一条件满足时执行"),
        ],
        default="&&",
        update=update_all_properties,
    )

    description_expanded: bpy.props.BoolProperty(
        name="展开说明",
        description="展开节点说明菜单。",
        default=False,
    )

    preview_option_index: bpy.props.IntProperty(
        name="Preview Option Index",
        default=-1,
        options={"HIDDEN"},
    )

    preview_session_active: bpy.props.BoolProperty(
        name="Preview Session Active",
        default=False,
        options={"HIDDEN"},
    )

    preview_empty_mode: bpy.props.BoolProperty(
        name="Preview Empty Mode",
        default=False,
        options={"HIDDEN"},
    )

    def init(self, context):
        self._ensure_initial_visible_variable_name(context=context)
        self.outputs.new("SSMTSocketObject", "Output")
        self._update_input_sockets()
        self.width = 320

    def copy(self, node):
        self.assigned_variable_name = ""
        self.custom_var_initialized = False
        self.custom_var_name = ""
        self.preview_option_index = -1
        self.preview_session_active = False
        self.preview_empty_mode = False
        self._ensure_initial_visible_variable_name()

    def _update_input_sockets(self):
        current_count = len(self.inputs)
        target_count = self.input_slot_count

        while current_count > target_count:
            self.inputs.remove(self.inputs[-1])
            current_count -= 1

        while current_count < target_count:
            self.inputs.new("SSMTSocketObject", f"选项_{current_count}")
            current_count += 1

        for idx, inp in enumerate(self.inputs):
            inp.name = f"选项_{idx}"

    def update(self):
        self._ensure_initial_visible_variable_name()
        ensure_object_swap_variable_name(self)
        self._update_input_sockets()
        self._sanitize_preview_option_index()

    def _sanitize_preview_option_index(self) -> int:
        option_count = max(1, int(getattr(self, "input_slot_count", 1) or 1))
        current_index = int(getattr(self, "preview_option_index", -1))
        if current_index < -1 or current_index >= option_count:
            self.preview_option_index = -1
            return -1
        return current_index

    def _get_preview_button_text(self, current_preview_index: int) -> str:
        if self.preview_session_active and current_preview_index == self.input_slot_count - 1:
            return f"退出切换预览 [当前 {current_preview_index}]"
        if 0 <= current_preview_index < self.input_slot_count:
            return f"预览切换物体 [当前 {current_preview_index}]"
        return "预览切换物体 [从 0 开始]"

    def draw_buttons(self, context, layout):
        current_preview_index = self._sanitize_preview_option_index()
        layout.operator(
            "ssmt.cycle_swap_preview",
            text=self._get_preview_button_text(current_preview_index),
            icon="HIDE_OFF",
        ).node_name = self.name
        layout.separator()

        layout.prop(self, "comment", text="备注")
        layout.prop(self, "custom_var_name", text="变量名")
        if not str(self.custom_var_name or "").strip() and str(self.assigned_variable_name or "").strip():
            layout.label(text=f"预分配变量: ${self.assigned_variable_name}", icon="INFO")
        row = layout.row(align=True)
        row.prop(self, "hotkey", text="按键")
        op = row.operator("ssmt.key_picker", text="选键", icon='KEYINGSET')
        op.node_name = self.name
        op.node_tree_name = self.id_data.name
        op.target_prop = "hotkey"

        layout.separator()
        layout.label(text="选项数量:")
        row = layout.row(align=True)
        row.prop(self, "input_slot_count", text="")
        if self.input_slot_count >= 2:
            row.operator("ssmt.add_swap_option", text="", icon="ADD").node_name = self.name
        if self.input_slot_count > 1:
            row.operator("ssmt.remove_swap_option", text="", icon="REMOVE").node_name = self.name

        layout.separator()
        row = layout.row()
        icon = "TRIA_DOWN" if self.description_expanded else "TRIA_RIGHT"
        row.prop(self, "description_expanded", text="节点说明", icon=icon, emboss=True)

        if self.description_expanded:
            col = layout.column()
            col.scale_y = 0.8
            col.prop(self, "swap_type", text="类型")
            col.prop(self, "condition_operator", text="运算符")
            col.separator()
            self._draw_node_description(col)

    def _draw_node_description(self, layout):
        var_display = f"${self.custom_var_name}" if self.custom_var_name else f"${self.assigned_variable_name}"
        option_seq = ", ".join(str(i) for i in range(self.input_slot_count))

        layout.label(text="本节点会追加以下配置段落:", icon="INFO")
        layout.label(text="  [KeySwap_*]", icon="NONE")
        layout.label(text="  [Constants]", icon="NONE")
        layout.label(text="  [Present]", icon="NONE")
        layout.label(text="  [TextureOverride_*]", icon="NONE")

        layout.separator()
        layout.label(text="当前节点参数:", icon="FILE_TEXT")
        layout.label(text=f"  备注: {self.comment if self.comment else '(未设置)'}", icon="NONE")
        layout.label(text=f"  变量: {var_display}", icon="NONE")
        layout.label(text=f"  按键: {self.hotkey}", icon="NONE")
        layout.label(text=f"  类型: {self.swap_type}", icon="NONE")
        layout.label(text=f"  运算符: {self.condition_operator}", icon="NONE")

        layout.separator()
        layout.label(text="选项配置:", icon="TRACKING")
        layout.label(text=f"  选项值: {option_seq}", icon="NONE")
        layout.label(text=f"  条件格式: {var_display} == 选项值", icon="NONE")

        layout.separator()
        layout.label(text="生成示例:", icon="INFO")
        layout.label(text="  [KeySwap_*]", icon="NONE")
        if self.comment:
            layout.label(text=f"  ; {self.comment}", icon="NONE")
        layout.label(text="  condition = $active0 == 1", icon="NONE")
        layout.label(text=f"  key = {self.hotkey}", icon="NONE")
        layout.label(text=f"  type = {self.swap_type}", icon="NONE")
        layout.label(text=f"  {var_display} = {option_seq},", icon="NONE")


class SSMT_OT_AddSwapOption(bpy.types.Operator):
    bl_idname = "ssmt.add_swap_option"
    bl_label = "添加选项"
    bl_options = {"REGISTER", "UNDO"}

    node_name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.active_node is not None

    def execute(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == "SSMTNode_ObjectSwap" and node.input_slot_count < 1024:
            node.input_slot_count += 1
        return {"FINISHED"}


class SSMT_OT_RemoveSwapOption(bpy.types.Operator):
    bl_idname = "ssmt.remove_swap_option"
    bl_label = "移除选项"
    bl_options = {"REGISTER", "UNDO"}

    node_name: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return context.active_node is not None

    def execute(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == "SSMTNode_ObjectSwap" and node.input_slot_count > 1:
            node.input_slot_count -= 1
        return {"FINISHED"}


class SSMT_OT_CycleSwapPreview(bpy.types.Operator):
    bl_idname = "ssmt.cycle_swap_preview"
    bl_label = "预览切换物体"
    bl_description = "按 0 -> 1 -> 2 的顺序预览当前物体切换节点的选项，只显示对应分支的物体"
    bl_options = {"REGISTER"}

    _RESULT_NODE_TYPES = {
        "SSMTNode_Result_Output",
        "SSMTNode_Result_Output_NTMIModImp",
    }

    node_name: bpy.props.StringProperty()

    @staticmethod
    def _node_visit_key(node) -> str:
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

    @staticmethod
    def _lookup_node_by_key(node_key):
        tree_name, _, node_name = str(node_key or "").partition("::")
        if not tree_name or not node_name:
            return None
        tree = bpy.data.node_groups.get(tree_name)
        if tree is None:
            return None
        return tree.nodes.get(node_name)

    @staticmethod
    def _find_view3d_area(context):
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return window, area, region
        return None, None, None

    @staticmethod
    def _is_local_view_active(area) -> bool:
        for space in area.spaces:
            if space.type == "VIEW_3D" and space.local_view:
                return True
        return False

    @staticmethod
    def _build_view3d_override(window, area, region):
        override = {"window": window, "area": area}
        if region is not None:
            override["region"] = region
        return override

    @staticmethod
    def _set_node_preview_state(node, *, active, empty_mode, option_index):
        node.preview_session_active = active
        node.preview_empty_mode = empty_mode
        node.preview_option_index = option_index

    def _exit_local_view_if_needed(self, context, view_3d_window, view_3d_area, region):
        if not self._is_local_view_active(view_3d_area):
            return
        override = self._build_view3d_override(view_3d_window, view_3d_area, region)
        with context.temp_override(**override):
            bpy.ops.view3d.localview()

    def _capture_hidden_state(self, context, owner_key):
        global _swap_preview_hidden_state

        state = _swap_preview_hidden_state
        if state and state.get("owner_key") == owner_key:
            return

        hidden_states = {}
        for obj in context.view_layer.objects:
            try:
                hidden_states[obj.name] = bool(obj.hide_get())
            except Exception:
                hidden_states[obj.name] = False

        _swap_preview_hidden_state = {
            "owner_key": owner_key,
            "hidden_states": hidden_states,
        }

    def _restore_hidden_state(self, owner_key=None):
        global _swap_preview_hidden_state

        state = _swap_preview_hidden_state
        if not state:
            return
        if owner_key is not None and state.get("owner_key") != owner_key:
            return

        for obj_name, hidden in state.get("hidden_states", {}).items():
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            try:
                obj.hide_set(hidden)
            except Exception:
                continue

        _swap_preview_hidden_state = None

    def _clear_foreign_preview_session(self, context, current_owner_key, view_3d_window, view_3d_area, region):
        global _swap_preview_session_owner

        owner_key = _swap_preview_session_owner
        if not owner_key or owner_key == current_owner_key:
            return

        owner_node = self._lookup_node_by_key(owner_key)
        if owner_node is not None:
            self._set_node_preview_state(owner_node, active=False, empty_mode=False, option_index=-1)

        try:
            self._exit_local_view_if_needed(context, view_3d_window, view_3d_area, region)
        except Exception:
            pass

        self._restore_hidden_state(owner_key=owner_key)
        _swap_preview_session_owner = None

    def _enter_empty_preview(self, context, owner_key, node, target_option_index, view_3d_window, view_3d_area, region):
        global _swap_preview_session_owner

        self._capture_hidden_state(context, owner_key)
        self._exit_local_view_if_needed(context, view_3d_window, view_3d_area, region)

        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        for selected_obj in context.selected_objects:
            selected_obj.select_set(False)

        for obj in context.view_layer.objects:
            try:
                obj.hide_set(True)
            except Exception:
                continue

        self._set_node_preview_state(node, active=True, empty_mode=True, option_index=target_option_index)
        _swap_preview_session_owner = owner_key
        self.report({"INFO"}, f"正在预览选项 {target_option_index}（空）")
        return {"FINISHED"}

    def _enter_object_preview(self, context, owner_key, node, target_option_index, objects_to_show, view_3d_window, view_3d_area, region):
        global _swap_preview_session_owner

        if bool(getattr(node, "preview_empty_mode", False)):
            self._restore_hidden_state(owner_key=owner_key)
        self._capture_hidden_state(context, owner_key)

        self._exit_local_view_if_needed(context, view_3d_window, view_3d_area, region)

        if context.mode != "OBJECT":
            try:
                bpy.ops.object.mode_set(mode="OBJECT")
            except Exception:
                pass

        for selected_obj in context.selected_objects:
            selected_obj.select_set(False)

        active_obj = None
        for obj in objects_to_show:
            try:
                obj.hide_set(False)
            except Exception:
                pass
            obj.select_set(True)
            if active_obj is None:
                active_obj = obj

        if active_obj is not None:
            try:
                context.view_layer.objects.active = active_obj
            except Exception:
                pass

        override = self._build_view3d_override(view_3d_window, view_3d_area, region)
        with context.temp_override(**override):
            bpy.ops.view3d.localview()
            bpy.ops.view3d.view_axis(type="FRONT")
            bpy.ops.view3d.view_selected()
            if view_3d_area.spaces.active:
                view_3d_area.spaces.active.shading.type = "SOLID"

        self._set_node_preview_state(node, active=True, empty_mode=False, option_index=target_option_index)
        _swap_preview_session_owner = owner_key
        self.report({"INFO"}, f"正在预览选项 {target_option_index}")
        return {"FINISHED"}

    def _finish_preview_session(self, context, owner_key, node, view_3d_window, view_3d_area, region):
        global _swap_preview_session_owner

        self._exit_local_view_if_needed(context, view_3d_window, view_3d_area, region)
        self._restore_hidden_state(owner_key=owner_key)
        self._set_node_preview_state(node, active=False, empty_mode=False, option_index=-1)
        _swap_preview_session_owner = None
        self.report({"INFO"}, "已退出切换预览")
        return {"FINISHED"}

    def _resolve_nested_swap_option_index(self, node) -> int:
        option_count = max(1, int(getattr(node, "input_slot_count", 1) or 1))
        preview_index = int(getattr(node, "preview_option_index", -1))
        if 0 <= preview_index < option_count:
            return preview_index
        return 0

    @staticmethod
    def _get_preview_effective_object_name(node) -> str:
        try:
            return ObjectPrefixHelper.build_virtual_object_name_for_node(node, strict=True)
        except Exception:
            return str(getattr(node, "object_name", "") or "")

    @staticmethod
    def _passes_rename_filters(candidate_name: str, rename_nodes_on_path) -> bool:
        current_name = str(candidate_name or "")
        if not current_name or not rename_nodes_on_path:
            return bool(current_name)

        try:
            from .node_rename import SSMTNode_Object_Rename
        except ImportError:
            return True

        for rename_node in reversed(tuple(rename_nodes_on_path)):
            new_name, was_modified, _history, _signature = SSMTNode_Object_Rename.apply_to_object_name(
                current_name,
                rename_node,
            )
            if getattr(rename_node, "filter_objects", False) and not was_modified:
                return False
            if was_modified:
                current_name = new_name

        return True

    def _append_object_info_preview_object(self, node, objects_to_show, rename_nodes_on_path):
        # 物体信息 Pro 通过条目视图展开成多个普通物体信息节点条目；
        # 普通节点只有一个条目（即节点自身），行为不变。
        from .node_obj import iter_object_info_entries

        for entry in iter_object_info_entries(node):
            effective_name = self._get_preview_effective_object_name(entry)
            if not self._passes_rename_filters(effective_name, rename_nodes_on_path):
                continue
            self._append_object_by_name(objects_to_show, getattr(entry, "object_name", ""))

    def _append_multifile_preview_objects(self, node, objects_to_show, rename_nodes_on_path):
        for item in getattr(node, "object_list", []):
            object_name = str(getattr(item, "object_name", "") or "")
            if not self._passes_rename_filters(object_name, rename_nodes_on_path):
                continue
            self._append_object_by_name(objects_to_show, object_name)

    def _collect_objects_for_swap_option(
        self,
        swap_node,
        option_index,
        objects_to_show,
        visited_nodes,
        visited_blueprints,
        rename_nodes_on_path=(),
    ):
        if option_index < 0 or option_index >= len(getattr(swap_node, "inputs", [])):
            return

        option_input = swap_node.inputs[option_index]
        if not option_input.is_linked:
            return

        for link in option_input.links:
            from_node = getattr(link, "from_node", None)
            if from_node is not None:
                self._collect_preview_objects(
                    from_node,
                    objects_to_show,
                    visited_nodes,
                    visited_blueprints,
                    rename_nodes_on_path=rename_nodes_on_path,
                )

    def _collect_preview_objects(
        self,
        current_node,
        objects_to_show,
        visited_nodes,
        visited_blueprints,
        rename_nodes_on_path=(),
    ):
        node_key = self._node_visit_key(current_node)
        if node_key in visited_nodes:
            return
        visited_nodes.add(node_key)

        node_type = getattr(current_node, "bl_idname", "")
        if node_type in ("SSMTNode_Object_Info", "SSMTNode_Object_Info_Pro"):
            self._append_object_info_preview_object(current_node, objects_to_show, rename_nodes_on_path)
        elif node_type == "SSMTNode_MultiFile_Export":
            self._append_multifile_preview_objects(current_node, objects_to_show, rename_nodes_on_path)
        elif node_type == "SSMTNode_Object_Rename":
            rename_nodes_on_path = tuple(rename_nodes_on_path) + (current_node,)
        elif node_type == "SSMTNode_Blueprint_Nest":
            nested_tree_name = str(getattr(current_node, "blueprint_name", "") or "").strip()
            if nested_tree_name and nested_tree_name != "NONE" and nested_tree_name not in visited_blueprints:
                nested_tree = bpy.data.node_groups.get(nested_tree_name)
                if nested_tree and getattr(nested_tree, "bl_idname", "") == "SSMTBlueprintTreeType":
                    visited_blueprints.add(nested_tree_name)
                    for nested_node in nested_tree.nodes:
                        if getattr(nested_node, "bl_idname", "") in self._RESULT_NODE_TYPES:
                            self._collect_preview_objects(
                                nested_node,
                                objects_to_show,
                                visited_nodes,
                                visited_blueprints,
                                rename_nodes_on_path=rename_nodes_on_path,
                            )
        elif node_type == "SSMTNode_ObjectSwap":
            nested_option_index = self._resolve_nested_swap_option_index(current_node)
            self._collect_objects_for_swap_option(
                current_node,
                nested_option_index,
                objects_to_show,
                visited_nodes,
                visited_blueprints,
                rename_nodes_on_path=rename_nodes_on_path,
            )
            return

        if hasattr(current_node, "inputs"):
            for input_socket in current_node.inputs:
                if not input_socket.is_linked:
                    continue
                for link in input_socket.links:
                    from_node = getattr(link, "from_node", None)
                    if from_node is not None:
                        self._collect_preview_objects(
                            from_node,
                            objects_to_show,
                            visited_nodes,
                            visited_blueprints,
                            rename_nodes_on_path=rename_nodes_on_path,
                        )

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {"CANCELLED"}

        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != "SSMTNode_ObjectSwap":
            return {"CANCELLED"}

        view_3d_window, view_3d_area, region = self._find_view3d_area(context)
        if not view_3d_area:
            self.report({"WARNING"}, "No 3D View found")
            return {"CANCELLED"}

        owner_key = self._node_visit_key(node)
        self._clear_foreign_preview_session(context, owner_key, view_3d_window, view_3d_area, region)

        option_count = max(1, int(getattr(node, "input_slot_count", len(node.inputs) or 1) or 1))
        session_active = bool(getattr(node, "preview_session_active", False))
        current_preview_index = int(getattr(node, "preview_option_index", -1))
        if current_preview_index < -1 or current_preview_index >= option_count:
            current_preview_index = -1

        if session_active and current_preview_index >= option_count - 1:
            return self._finish_preview_session(
                context,
                owner_key,
                node,
                view_3d_window,
                view_3d_area,
                region,
            )

        target_option_index = 0 if not session_active else current_preview_index + 1

        objects_to_show = set()
        visited_nodes = set()
        visited_blueprints = set()
        self._collect_objects_for_swap_option(
            node,
            target_option_index,
            objects_to_show,
            visited_nodes,
            visited_blueprints,
        )

        if not objects_to_show:
            try:
                return self._enter_empty_preview(
                    context,
                    owner_key,
                    node,
                    target_option_index,
                    view_3d_window,
                    view_3d_area,
                    region,
                )
            except Exception as exc:
                self.report({"WARNING"}, f"空选项预览失败: {exc}")
                return {"CANCELLED"}

        try:
            return self._enter_object_preview(
                context,
                owner_key,
                node,
                target_option_index,
                objects_to_show,
                view_3d_window,
                view_3d_area,
                region,
            )
        except Exception as exc:
            self.report({"WARNING"}, f"预览视图设置失败: {exc}")
            return {"CANCELLED"}


class ObjectSwapDebugger:
    @staticmethod
    def _get_node_unique_key(node) -> str:
        tree_name = node.id_data.name if hasattr(node, "id_data") and node.id_data else ""
        return f"{tree_name}::{node.name}"

    @staticmethod
    def generate_chain_detail(chain, registry=None) -> list[str]:
        if not chain.swap_node_option_values:
            return []

        lines = []
        for swap_key, option_val in chain.swap_node_option_values.items():
            swap_node = None
            for node in chain.node_path:
                if ObjectSwapDebugger._get_node_unique_key(node) == swap_key:
                    swap_node = node
                    break

            if swap_node is not None:
                swap_key_index = 0
                if registry and hasattr(registry, "node_swapkey_map"):
                    swap_key_index = registry.node_swapkey_map.get(swap_key, 0)
                node_index = 0
                for idx, path_node in enumerate(chain.node_path):
                    if ObjectSwapDebugger._get_node_unique_key(path_node) == swap_key:
                        node_index = idx
                        break
                lines.extend(ObjectSwapDebugger.generate_debug_detail(swap_node, node_index, swap_key_index))
            else:
                lines.append(f"物体切换: {swap_key} -> 选项 {option_val + 1} (索引 {option_val})")

        return lines

    @staticmethod
    def generate_debug_detail(swap_node: bpy.types.Node, node_index: int, swap_key_index: int) -> list[str]:
        resolved_name = normalize_variable_name(getattr(swap_node, "custom_var_name", "") or "")
        if not resolved_name:
            resolved_name = normalize_variable_name(getattr(swap_node, "assigned_variable_name", "") or "")
        var_name = f"${resolved_name}" if resolved_name else f"$swapkey{swap_key_index}"
        config = SwapKeyConfig(
            index=swap_key_index,
            comment=getattr(swap_node, "comment", ""),
            custom_var_name=normalize_variable_name(getattr(swap_node, "custom_var_name", "") or ""),
            assigned_variable_name=normalize_variable_name(getattr(swap_node, "assigned_variable_name", "") or ""),
        )

        lines = [
            "",
            f"物体切换节点 #{node_index + 1}",
            f"   备注: {getattr(swap_node, 'comment', '未设置')}",
            f"   变量: {var_name}",
            f"   快捷键: {getattr(swap_node, 'hotkey', 'N/A')}",
            f"   切换类型: {getattr(swap_node, 'swap_type', 'N/A')}",
            f"   选项数量: {getattr(swap_node, 'input_slot_count', 1)}",
            "",
            "   配置段落:",
            f"   [{config.get_key_swap_section_name()}]",
        ]

        if config.comment:
            lines.append(f"   ; {config.comment}")
        lines.extend(
            [
                "   condition = $active0 == 1",
                f"   key = {getattr(swap_node, 'hotkey', 'N/A')}",
                f"   type = {getattr(swap_node, 'swap_type', 'N/A')}",
                f"   {var_name} = {','.join(str(i) for i in range(getattr(swap_node, 'input_slot_count', 1)))},",
                "",
                "   常量声明:",
                f"   {var_name} = 0",
                "",
                "   初始化参数:",
                "   post $active0 = 0",
            ]
        )
        return lines


def _iter_blueprint_swap_nodes():
    for tree in getattr(bpy.data, "node_groups", []) or []:
        if getattr(tree, "bl_idname", "") != "SSMTBlueprintTreeType":
            continue
        nodes = getattr(tree, "nodes", []) or []
        if isinstance(nodes, dict):
            node_iterable = nodes.values()
        else:
            node_iterable = nodes
        for node in node_iterable:
            if getattr(node, "bl_idname", "") == "SSMTNode_ObjectSwap":
                yield node


def _get_blueprint_tree_by_name(tree_name: str):
    clean_name = str(tree_name or "").strip()
    if not clean_name:
        return None

    node_groups = getattr(bpy.data, "node_groups", None)
    if node_groups is None:
        return None

    getter = getattr(node_groups, "get", None)
    if callable(getter):
        return getter(clean_name)

    for tree in node_groups:
        if getattr(tree, "name", "") == clean_name:
            return tree
    return None


def _resolve_button_context_swap_property(context):
    pointer = getattr(context, "button_pointer", None)
    button_prop = getattr(context, "button_prop", None)
    if pointer is None or button_prop is None:
        return None, None
    if getattr(pointer, "bl_idname", "") != "SSMTNode_ObjectSwap":
        return None, None

    property_name = str(getattr(button_prop, "identifier", "") or "").strip()
    allowed_properties = {
        "comment",
        "custom_var_name",
        "hotkey",
        "swap_type",
        "input_slot_count",
        "condition_operator",
    }
    if property_name not in allowed_properties:
        return None, None
    return pointer, property_name


class SSMT_OT_CopySwapPropertyToSameHotkeyNodes(bpy.types.Operator):
    bl_idname = "ssmt.copy_swap_property_to_same_hotkey_nodes"
    bl_label = "将内容复制到相同快捷键的节点"
    bl_description = "在所有蓝图中查找填写了相同快捷键的物体切换节点，并同步当前输入框内容"
    bl_options = {"REGISTER", "UNDO"}

    source_tree_name: bpy.props.StringProperty(options={"SKIP_SAVE"})  # type: ignore
    source_node_name: bpy.props.StringProperty(options={"SKIP_SAVE"})  # type: ignore
    property_name: bpy.props.StringProperty(options={"SKIP_SAVE"})  # type: ignore

    def execute(self, context):
        tree = _get_blueprint_tree_by_name(str(getattr(self, "source_tree_name", "") or "").strip())
        source_node = tree.nodes.get(self.source_node_name) if tree else None
        property_name = str(getattr(self, "property_name", "") or "").strip()
        if source_node is None or getattr(source_node, "bl_idname", "") != "SSMTNode_ObjectSwap" or not property_name:
            self.report({"WARNING"}, "未找到源物体切换节点")
            return {"CANCELLED"}

        source_hotkey = str(getattr(source_node, "hotkey", "") or "").strip()
        if not source_hotkey:
            self.report({"WARNING"}, "当前物体切换节点未填写快捷键")
            return {"CANCELLED"}

        value = getattr(source_node, property_name)
        updated_count = 0
        for node in _iter_blueprint_swap_nodes():
            if node == source_node:
                continue
            if str(getattr(node, "hotkey", "") or "").strip() != source_hotkey:
                continue
            try:
                setattr(node, property_name, value)
                if property_name == "input_slot_count":
                    update_fn = getattr(node, "update_input_slot_count", None)
                    if callable(update_fn):
                        update_fn(context)
                else:
                    update_fn = getattr(node, "update_all_properties", None)
                    if callable(update_fn):
                        update_fn(context)
                updated_count += 1
            except Exception:
                continue

        self.report({"INFO"}, f"已同步 {updated_count} 个相同快捷键节点")
        return {"FINISHED"}


def draw_swap_button_context_menu(self, context):
    node, property_name = _resolve_button_context_swap_property(context)
    if node is None or not property_name:
        return

    layout = self.layout
    layout.separator()
    op = layout.operator(
        "ssmt.copy_swap_property_to_same_hotkey_nodes",
        text="将内容复制到相同快捷键的节点",
        icon="PASTEDOWN",
    )
    op.source_tree_name = node.id_data.name if getattr(node, "id_data", None) else ""
    op.source_node_name = node.name
    op.property_name = property_name


classes = (
    SSMTNode_ObjectSwap,
    SSMT_OT_AddSwapOption,
    SSMT_OT_RemoveSwapOption,
    SSMT_OT_CycleSwapPreview,
    SSMT_OT_CopySwapPropertyToSameHotkeyNodes,
)


def register():
    global _is_button_context_menu_hooked
    for cls in classes:
        bpy.utils.register_class(cls)
    if not _is_button_context_menu_hooked:
        bpy.types.UI_MT_button_context_menu.append(draw_swap_button_context_menu)
        _is_button_context_menu_hooked = True


def unregister():
    global _is_button_context_menu_hooked
    if _is_button_context_menu_hooked:
        try:
            bpy.types.UI_MT_button_context_menu.remove(draw_swap_button_context_menu)
        except Exception:
            pass
        _is_button_context_menu_hooked = False
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
