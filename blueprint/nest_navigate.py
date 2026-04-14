import bpy
from bpy.types import Operator
from bpy.props import StringProperty


class SSMT_OT_BlueprintNestNavigate(Operator):
    '''Navigate through nested blueprints - Tab键导航嵌套蓝图'''
    bl_idname = "ssmt.blueprint_nest_navigate"
    bl_label = "蓝图嵌套导航"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            return {'CANCELLED'}

        selected_nodes = [node for node in context.selected_nodes if hasattr(node, 'bl_idname')]

        if len(selected_nodes) == 1:
            selected_node = selected_nodes[0]
            if selected_node.bl_idname == 'SSMTNode_Blueprint_Nest':
                self.enter_nested_blueprint(context, selected_node)
                return {'FINISHED'}

        self.return_to_parent_blueprint(context)
        return {'FINISHED'}

    def enter_nested_blueprint(self, context, nest_node):
        blueprint_name = getattr(nest_node, 'blueprint_name', '')
        if not blueprint_name or blueprint_name == 'NONE':
            self.report({'WARNING'}, "该嵌套蓝图节点未指定蓝图")
            return

        nested_tree = bpy.data.node_groups.get(blueprint_name)
        if not nested_tree or nested_tree.bl_idname != 'SSMTBlueprintTreeType':
            self.report({'WARNING'}, f"蓝图 '{blueprint_name}' 不存在或不是SSMT蓝图")
            return

        space_data = getattr(context, "space_data", None)
        space_data.node_tree = nested_tree

        for node in nested_tree.nodes:
            node.select = False

        print(f"[Blueprint Nest] 进入嵌套蓝图: {blueprint_name}")

    def return_to_parent_blueprint(self, context):
        space_data = getattr(context, "space_data", None)
        current_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)

        if not current_tree:
            return

        current_blueprint_name = current_tree.name

        parent_blueprints = self.find_parent_blueprints(current_blueprint_name)

        if not parent_blueprints:
            print(f"[Blueprint Nest] 未找到引用蓝图 '{current_blueprint_name}' 的父蓝图")
            return

        parent_tree, nest_node = parent_blueprints[0]

        if len(parent_blueprints) > 1:
            print(f"[Blueprint Nest] 警告: 找到 {len(parent_blueprints)} 个父蓝图，选择第一个: {parent_tree.name}")
            for tree, node in parent_blueprints:
                print(f"  - {tree.name} (节点: {node.name})")

        space_data.node_tree = parent_tree

        if nest_node:
            for node in parent_tree.nodes:
                node.select = False
            nest_node.select = True

        print(f"[Blueprint Nest] 返回父蓝图: {parent_tree.name}")

    def find_parent_blueprints(self, blueprint_name):
        parent_blueprints = []

        for node_group in bpy.data.node_groups:
            if node_group.bl_idname != 'SSMTBlueprintTreeType':
                continue

            if node_group.name == blueprint_name:
                continue

            for node in node_group.nodes:
                if node.bl_idname == 'SSMTNode_Blueprint_Nest':
                    if getattr(node, 'blueprint_name', '') == blueprint_name:
                        parent_blueprints.append((node_group, node))

        return parent_blueprints

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        if not space_data or space_data.type != 'NODE_EDITOR':
            return False

        edit_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
        if not edit_tree:
            return False

        if edit_tree.bl_idname != 'SSMTBlueprintTreeType':
            return False

        return True


class SSMT_OT_CreateBlueprintFromNest(Operator):
    '''Create a new blueprint from nest node'''
    bl_idname = "ssmt.create_blueprint_from_nest"
    bl_label = "创建新蓝图"
    bl_options = {'REGISTER', 'INTERNAL'}

    blueprint_name: StringProperty(
        name="蓝图名称",
        description="输入新蓝图的名称",
        default=""
    )

    node_name: StringProperty(
        name="节点名称",
        default=""
    )

    def invoke(self, context, event):
        if self.node_name:
            space_data = getattr(context, "space_data", None)
            if space_data and space_data.type == 'NODE_EDITOR':
                tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
                if tree:
                    node = tree.nodes.get(self.node_name)
                    if node and node.bl_idname == 'SSMTNode_Blueprint_Nest':
                        if not self.blueprint_name:
                            self.blueprint_name = f"SSMT_Blueprint_{len(bpy.data.node_groups) + 1}"
                        return context.window_manager.invoke_props_dialog(self)

        return {'CANCELLED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "blueprint_name")

        if self.blueprint_name and bpy.data.node_groups.get(self.blueprint_name):
            layout.label(text="警告: 该名称已存在，将使用现有蓝图", icon='ERROR')

    def execute(self, context):
        if not self.blueprint_name:
            self.report({'WARNING'}, "请输入蓝图名称")
            return {'CANCELLED'}

        existing_tree = bpy.data.node_groups.get(self.blueprint_name)

        if existing_tree and existing_tree.bl_idname == 'SSMTBlueprintTreeType':
            tree = existing_tree
            self.report({'INFO'}, f"使用现有蓝图: {self.blueprint_name}")
        else:
            tree = bpy.data.node_groups.new(self.blueprint_name, 'SSMTBlueprintTreeType')
            tree.use_fake_user = True
            self.report({'INFO'}, f"创建新蓝图: {self.blueprint_name}")

            output_node = tree.nodes.new('SSMTNode_Result_Output')
            output_node.location = (400, 0)

        space_data = getattr(context, "space_data", None)
        if space_data and space_data.type == 'NODE_EDITOR':
            current_tree = getattr(space_data, "edit_tree", None) or getattr(space_data, "node_tree", None)
            if current_tree:
                node = current_tree.nodes.get(self.node_name)
                if node and node.bl_idname == 'SSMTNode_Blueprint_Nest':
                    node.blueprint_name = self.blueprint_name

        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        space_data = getattr(context, "space_data", None)
        return space_data and space_data.type == 'NODE_EDITOR'


classes = (
    SSMT_OT_BlueprintNestNavigate,
    SSMT_OT_CreateBlueprintFromNest,
)


_keymaps = []


def register_keymaps():
    wm = bpy.context.window_manager
    if not wm.keyconfigs.addon:
        return

    kc = wm.keyconfigs.addon

    km = kc.keymaps.new(name='Node Editor', space_type='NODE_EDITOR')

    kmi = km.keymap_items.new(
        'ssmt.blueprint_nest_navigate',
        type='TAB',
        value='PRESS'
    )
    _keymaps.append((km, kmi))

    print("[Blueprint Nest] 已注册 Tab 键导航")


def unregister_keymaps():
    for km, kmi in _keymaps:
        km.keymap_items.remove(kmi)
    _keymaps.clear()

    print("[Blueprint Nest] 已注销 Tab 键导航")


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    register_keymaps()


def unregister():
    unregister_keymaps()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
