import bpy
import os
import shutil
from bpy.types import Node, PropertyGroup
from bpy.props import StringProperty, CollectionProperty, BoolProperty, EnumProperty

from .node_base import SSMTNodeBase, SSMTSocketObject
from ..common.global_config import GlobalConfig


class CrossIBMatchMode:
    INDEX_COUNT = 'INDEX_COUNT'
    INDEX_COUNT_LABEL = '通过 IndexCount 识别'

    IB_HASH = 'IB_HASH'
    IB_HASH_LABEL = '通过 IB Hash 识别 (ZZMI)'

    @classmethod
    def get_items(cls):
        return [
            (cls.INDEX_COUNT, cls.INDEX_COUNT_LABEL, "通过 match_index_count 参数进行匹配"),
            (cls.IB_HASH, cls.IB_HASH_LABEL, "通过 IB Hash 和 FirstIndex/Component 识别 (ZZMI 格式)"),
        ]

    @classmethod
    def get_items_for_logic_name(cls, logic_name):
        if logic_name == 'EFMI':
            return [(cls.INDEX_COUNT, cls.INDEX_COUNT_LABEL, "通过 match_index_count 参数进行匹配")]
        elif logic_name == 'ZZMI':
            return [(cls.IB_HASH, cls.IB_HASH_LABEL, "通过 IB Hash 和 FirstIndex/Component 识别 (ZZMI 格式)")]
        return [(cls.INDEX_COUNT, cls.INDEX_COUNT_LABEL, "通过 match_index_count 参数进行匹配")]


class CrossIBItem(PropertyGroup):
    source_ib: StringProperty(
        name="源IB",
        description="源IB前缀，例如: 9f387166 或 9f387166-0（SSMT4格式，-后面是first_index）",
        default=""
    )
    target_ib: StringProperty(
        name="目标IB",
        description="目标IB前缀，例如: a55afe59 或 a55afe59-0（SSMT4格式，-后面是first_index）",
        default=""
    )
    source_index_count: StringProperty(
        name="源IndexCount",
        description="源 IndexCount 值，用于通过 IndexCount 识别",
        default=""
    )
    target_index_count: StringProperty(
        name="目标IndexCount",
        description="目标 IndexCount 值，用于通过 IndexCount 识别",
        default=""
    )

    @classmethod
    def parse_ib_with_component(cls, ib_str):
        if not ib_str:
            return "", 1

        parts = ib_str.split("-")
        ib_hash = parts[0]

        if len(parts) > 1 and parts[1].isdigit():
            component_index = int(parts[1])
        else:
            component_index = 1

        return ib_hash, component_index

    @classmethod
    def parse_ib_with_first_index(cls, ib_str):
        if not ib_str:
            return "", 0

        parts = ib_str.split("-")
        ib_hash = parts[0]

        if len(parts) > 1 and parts[1].isdigit():
            first_index = int(parts[1])
        else:
            first_index = 0

        return ib_hash, first_index


class CrossIBMethodEnum:
    END_FIELD = 'END_FIELD'
    END_FIELD_LABEL = '终末地跨 IB'
    END_FIELD_LOGIC_NAME = 'EFMI'

    VB_COPY = 'VB_COPY'
    VB_COPY_LABEL = 'VB 复制'
    VB_COPY_LOGIC_NAME = 'ZZMI'

    @classmethod
    def get_items(cls):
        return [
            (cls.END_FIELD, cls.END_FIELD_LABEL, "终末地跨 IB 方式 (仅 EFMI)"),
            (cls.VB_COPY, cls.VB_COPY_LABEL, "VB 复制方式 (仅 ZZMI)"),
        ]

    @classmethod
    def get_items_for_logic_name(cls, logic_name):
        items = []
        for item in cls.get_items():
            method_id = item[0]
            method_logic_name = getattr(cls, f"{method_id}_LOGIC_NAME", None)
            if method_logic_name and method_logic_name == logic_name:
                items.append(item)
        return items

    @classmethod
    def get_available_methods(cls, logic_name):
        available_methods = []
        for item in cls.get_items():
            method_id = item[0]
            method_logic_name = getattr(cls, f"{method_id}_LOGIC_NAME", None)
            if method_logic_name and method_logic_name == logic_name:
                available_methods.append(method_id)
        return available_methods


class SSMT_OT_CrossIB_AddItem(bpy.types.Operator):
    bl_idname = "ssmt.cross_ib_add_item"
    bl_label = "添加跨IB映射"
    bl_description = "添加一个新的跨IB映射项"

    node_name: StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name)
        if node:
            new_item = node.cross_ib_list.add()
            new_item.source_ib = ""
            new_item.target_ib = ""

        return {'FINISHED'}


class SSMT_OT_CrossIB_RemoveItem(bpy.types.Operator):
    bl_idname = "ssmt.cross_ib_remove_item"
    bl_label = "删除跨IB映射"
    bl_description = "删除选中的跨IB映射项"

    node_name: StringProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name)
        if node and self.item_index >= 0 and self.item_index < len(node.cross_ib_list):
            node.cross_ib_list.remove(self.item_index)

        return {'FINISHED'}


class SSMTNode_CrossIB(SSMTNodeBase):
    bl_idname = 'SSMTNode_CrossIB'
    bl_label = 'Cross IB'
    bl_icon = 'ARROW_LEFTRIGHT'
    bl_width_min = 350

    cross_ib_list: CollectionProperty(type=CrossIBItem)

    original_cross_ib_data: bpy.props.StringProperty(
        name="原始跨IB数据",
        description="保存原始的跨IB参数（JSON格式）",
        default="",
        options={'HIDDEN'}
    )

    cross_ib_method: EnumProperty(
        name="跨 IB 方式",
        description="选择跨 IB 的实现方式",
        items=CrossIBMethodEnum.get_items(),
        default=CrossIBMethodEnum.END_FIELD,
        update=lambda self, context: self._update_cross_ib_method()
    )

    match_mode: EnumProperty(
        name="识别模式",
        description="选择跨 IB 的识别模式",
        items=CrossIBMatchMode.get_items(),
        default=CrossIBMatchMode.INDEX_COUNT,
    )

    vb_slot_200: BoolProperty(
        name="200",
        description="源块 VS 槽位 200",
        default=True
    )

    vb_slot_201: BoolProperty(
        name="201",
        description="源块 VS 槽位 201",
        default=True
    )

    vb_slot_202: BoolProperty(
        name="202",
        description="目标块 VS 槽位 202",
        default=True
    )

    vb_slot_203: BoolProperty(
        name="203",
        description="目标块 VS 槽位 203",
        default=True
    )

    vb_slot_204: BoolProperty(
        name="204",
        description="源块 VS 槽位 204",
        default=True
    )

    current_logic_name: StringProperty(
        name="当前运行模式",
        description="当前游戏的运行模式",
        default="",
        get=lambda self: self._get_current_logic_name()
    )

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input 1")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 350
        self.use_custom_color = True
        self.color = (0.6, 0.3, 0.6)

        self._update_cross_ib_method()

    def _get_current_logic_name(self):
        from ..common.global_config import GlobalConfig
        return GlobalConfig.logic_name or "未知"

    def _update_cross_ib_method(self):
        from ..common.global_config import GlobalConfig
        logic_name = GlobalConfig.logic_name

        available_methods = CrossIBMethodEnum.get_available_methods(logic_name)

        if available_methods:
            if self.cross_ib_method not in available_methods:
                self.cross_ib_method = available_methods[0]
        else:
            self.cross_ib_method = ''

        if logic_name == 'ZZMI':
            self.match_mode = CrossIBMatchMode.IB_HASH
        elif logic_name == 'EFMI':
            self.match_mode = CrossIBMatchMode.INDEX_COUNT

    def draw_buttons(self, context, layout):
        logic_name = self._get_current_logic_name()

        row = layout.row()
        row.label(text=f"当前运行模式: {logic_name}", icon='INFO')

        available_methods = CrossIBMethodEnum.get_available_methods(logic_name)

        if available_methods:
            if len(available_methods) == 1:
                row = layout.row()
                row.label(text=f"跨 IB 方式: {CrossIBMethodEnum.__dict__[available_methods[0] + '_LABEL']}", icon='CHECKMARK')
            else:
                row = layout.row()
                row.prop(self, "cross_ib_method", expand=True)
        else:
            row = layout.row()
            row.label(text="当前运行模式不支持跨 IB", icon='ERROR')

        if logic_name == "EFMI":
            row = layout.row()
            row.prop(self, "match_mode", text="识别模式")

            box_vb = layout.box()
            box_vb.label(text="VS 槽位选项 (用于条件判断)", icon='CHECKBOX_HLT')

            row = box_vb.row()
            row.label(text="源块:")
            row.prop(self, "vb_slot_200", text="200")
            row.prop(self, "vb_slot_201", text="201")
            row.prop(self, "vb_slot_204", text="204")

            row = box_vb.row()
            row.label(text="目标块:")
            row.prop(self, "vb_slot_202", text="202")
            row.prop(self, "vb_slot_203", text="203")

        box = layout.box()

        if logic_name == "ZZMI":
            box.label(text="跨IB映射列表 (源IB >> 目标IB)", icon='ARROW_LEFTRIGHT')
            box.label(text="格式: IBHash-FirstIndex (SSMT4) 或 IBHash-Component (SSMT3)", icon='INFO')

            for i, item in enumerate(self.cross_ib_list):
                row = box.row(align=True)
                row.prop(item, "source_ib", text="源")
                row.label(text=">>")
                row.prop(item, "target_ib", text="目标")

                op = row.operator("ssmt.cross_ib_remove_item", text="", icon='X')
                op.node_name = self.name
                op.item_index = i
        else:
            box.label(text="跨IB映射列表 (源IndexCount >> 目标IndexCount)", icon='ARROW_LEFTRIGHT')

            for i, item in enumerate(self.cross_ib_list):
                row = box.row(align=True)
                row.prop(item, "source_index_count", text="源")
                row.label(text=">>")
                row.prop(item, "target_index_count", text="目标")

                op = row.operator("ssmt.cross_ib_remove_item", text="", icon='X')
                op.node_name = self.name
                op.item_index = i

        row = layout.row()
        op = row.operator("ssmt.cross_ib_add_item", text="添加映射", icon='ADD')
        op.node_name = self.name

    def update(self):
        if self.inputs and self.inputs[-1].is_linked:
            self.inputs.new('SSMTSocketObject', f"Input {len(self.inputs) + 1}")

        if len(self.inputs) > 1 and not self.inputs[-1].is_linked and not self.inputs[-2].is_linked:
            self.inputs.remove(self.inputs[-1])

    def get_cross_ib_mappings(self):
        mappings = []
        for item in self.cross_ib_list:
            if item.source_index_count and item.target_index_count:
                mappings.append({
                    'source_index_count': item.source_index_count,
                    'target_index_count': item.target_index_count,
                    'match_mode': self.match_mode
                })
        return mappings

    def get_vb_condition_source(self) -> str:
        vb_slots = []
        if self.vb_slot_200:
            vb_slots.append("200")
        if self.vb_slot_201:
            vb_slots.append("201")
        if self.vb_slot_204:
            vb_slots.append("204")

        if not vb_slots:
            return ""

        if len(vb_slots) == 1:
            return f"if vs == {vb_slots[0]}"

        conditions = [f"vs == {slot}" for slot in vb_slots]
        return "if " + " || ".join(conditions)

    def get_vb_condition_target(self) -> str:
        vb_slots = []
        if self.vb_slot_202:
            vb_slots.append("202")
        if self.vb_slot_203:
            vb_slots.append("203")

        if not vb_slots:
            return ""

        if len(vb_slots) == 1:
            return f"if vs == {vb_slots[0]}"

        conditions = [f"vs == {slot}" for slot in vb_slots]
        return "if " + " || ".join(conditions)

    def get_source_ib_list(self):
        source_list = []
        for item in self.cross_ib_list:
            if item.source_index_count:
                source_list.append(item.source_index_count)
        return list(set(source_list))

    def get_target_ib_list(self):
        target_list = []
        for item in self.cross_ib_list:
            if item.target_index_count:
                target_list.append(item.target_index_count)
        return list(set(target_list))

    def get_ib_mapping_dict(self):
        ib_mapping = {}
        match_mode = self.match_mode

        for item in self.cross_ib_list:
            if match_mode == CrossIBMatchMode.IB_HASH:
                if item.source_ib and item.target_ib:
                    source_ib_hash, source_first_index = CrossIBItem.parse_ib_with_first_index(item.source_ib)
                    target_ib_hash, target_first_index = CrossIBItem.parse_ib_with_first_index(item.target_ib)

                    source_key = f"{source_ib_hash}_{source_first_index}"
                    if source_key not in ib_mapping:
                        ib_mapping[source_key] = []

                    target_key = f"{target_ib_hash}_{target_first_index}"
                    if target_key not in ib_mapping[source_key]:
                        ib_mapping[source_key].append(target_key)
            else:
                has_index_count = item.source_index_count and item.target_index_count
                has_ib_hash = item.source_ib and item.target_ib

                if has_index_count:
                    source_key = f"indexcount_{item.source_index_count}"
                    if source_key not in ib_mapping:
                        ib_mapping[source_key] = []

                    target_key = f"indexcount_{item.target_index_count}"
                    if target_key not in ib_mapping[source_key]:
                        ib_mapping[source_key].append(target_key)
                elif has_ib_hash:
                    source_ib_hash, source_first_index = CrossIBItem.parse_ib_with_first_index(item.source_ib)
                    target_ib_hash, target_first_index = CrossIBItem.parse_ib_with_first_index(item.target_ib)

                    source_key = f"{source_ib_hash}_{source_first_index}"
                    if source_key not in ib_mapping:
                        ib_mapping[source_key] = []

                    target_key = f"{target_ib_hash}_{target_first_index}"
                    if target_key not in ib_mapping[source_key]:
                        ib_mapping[source_key].append(target_key)

        return ib_mapping

    def get_match_mode(self):
        return self.match_mode

    def save_original_params(self):
        import json

        original_data = []
        for item in self.cross_ib_list:
            original_data.append({
                'source_ib': item.source_ib,
                'target_ib': item.target_ib,
                'source_index_count': item.source_index_count,
                'target_index_count': item.target_index_count,
            })

        self.original_cross_ib_data = json.dumps(original_data)
        print(f"[CrossIB] 已保存节点 {self.name} 的原始参数，共 {len(original_data)} 条")

    def restore_original_params(self):
        import json

        if not self.original_cross_ib_data:
            return

        try:
            original_data = json.loads(self.original_cross_ib_data)

            self.cross_ib_list.clear()
            for item_data in original_data:
                new_item = self.cross_ib_list.add()
                new_item.source_ib = item_data.get('source_ib', '')
                new_item.target_ib = item_data.get('target_ib', '')
                new_item.source_index_count = item_data.get('source_index_count', '')
                new_item.target_index_count = item_data.get('target_index_count', '')

            print(f"[CrossIB] 已恢复节点 {self.name} 的原始参数，共 {len(original_data)} 条")
        except Exception as e:
            print(f"[CrossIB] 恢复节点 {self.name} 的原始参数失败: {e}")

    def _get_base_mapping_data(self):
        import json

        if self.original_cross_ib_data:
            try:
                return json.loads(self.original_cross_ib_data)
            except Exception:
                pass

        base_data = []
        for item in self.cross_ib_list:
            base_data.append({
                'source_ib': item.source_ib,
                'target_ib': item.target_ib,
                'source_index_count': item.source_index_count,
                'target_index_count': item.target_index_count,
            })
        return base_data

    def _has_indexcount_pair(self, source_index_count, target_index_count):
        for item in self.cross_ib_list:
            if item.source_index_count == source_index_count and item.target_index_count == target_index_count:
                return True
        return False

    def _has_ibhash_pair(self, source_ib, target_ib):
        for item in self.cross_ib_list:
            if item.source_ib == source_ib and item.target_ib == target_ib:
                return True
        return False

    def apply_indexcount_mapping(self, indexcount_mapping):
        if not indexcount_mapping:
            return

        print(f"[CrossIB] 开始应用IndexCount映射，共 {len(indexcount_mapping)} 条规则")

        added_count = 0
        for item_data in self._get_base_mapping_data():
            original_source = item_data.get('source_index_count', '')
            original_target = item_data.get('target_index_count', '')
            if not original_source or not original_target:
                continue

            if original_source not in indexcount_mapping or original_target not in indexcount_mapping:
                continue

            new_source = indexcount_mapping[original_source]
            new_target = indexcount_mapping[original_target]

            if new_source == original_source and new_target == original_target:
                continue

            if self._has_indexcount_pair(new_source, new_target):
                continue

            new_item = self.cross_ib_list.add()
            new_item.source_index_count = new_source
            new_item.target_index_count = new_target
            added_count += 1
            print(f"[CrossIB] 追加IndexCount映射: {original_source}->{original_target} => {new_source}->{new_target}")

        print(f"[CrossIB] IndexCount映射应用完成，新增了 {added_count} 条映射")

    def apply_ibhash_mapping(self, ibhash_mapping):
        if not ibhash_mapping:
            return

        print(f"[CrossIB] 开始应用IBHash映射，共 {len(ibhash_mapping)} 条规则")

        added_count = 0
        for item_data in self._get_base_mapping_data():
            original_source = item_data.get('source_ib', '')
            original_target = item_data.get('target_ib', '')
            if not original_source or not original_target:
                continue

            if original_source not in ibhash_mapping or original_target not in ibhash_mapping:
                continue

            new_source = ibhash_mapping[original_source]
            new_target = ibhash_mapping[original_target]

            if new_source == original_source and new_target == original_target:
                continue

            if self._has_ibhash_pair(new_source, new_target):
                continue

            new_item = self.cross_ib_list.add()
            new_item.source_ib = new_source
            new_item.target_ib = new_target
            added_count += 1
            print(f"[CrossIB] 追加IBHash映射: {original_source}->{original_target} => {new_source}->{new_target}")

        print(f"[CrossIB] IBHash映射应用完成，新增了 {added_count} 条映射")


class SSMTNode_PostProcess_CrossIB(SSMTNodeBase):
    bl_idname = 'SSMTNode_PostProcess_CrossIB'
    bl_label = 'Cross IB PostProcess'
    bl_icon = 'FILE_REFRESH'
    bl_width_min = 300

    def init(self, context):
        self.inputs.new('SSMTSocketPostProcess', "Input")
        self.outputs.new('SSMTSocketPostProcess', "Output")
        self.width = 300

    def draw_buttons(self, context, layout):
        layout.label(text="跨IB后处理节点", icon='FILE_REFRESH')
        layout.label(text="自动复制HLSL文件到res目录")

    def execute_postprocess(self, mod_export_path):
        self._copy_hlsl_files(mod_export_path)

    def _copy_hlsl_files(self, mod_export_path):
        source_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "Toolset", "old")

        if not os.path.exists(source_dir):
            print(f"[CrossIB] 警告: Toolset目录不存在: {source_dir}")
            return

        hlsl_files = [
            'extract_cb1_ps.hlsl',
            'extract_cb1_vs.hlsl',
            'record_bones_cs.hlsl',
            'redirect_cb1_cs.hlsl'
        ]

        res_dir = os.path.join(mod_export_path, "res")
        os.makedirs(res_dir, exist_ok=True)

        copied_count = 0
        for hlsl_file in hlsl_files:
            source_file = os.path.join(source_dir, hlsl_file)
            target_file = os.path.join(res_dir, hlsl_file)

            if os.path.exists(source_file):
                if not os.path.exists(target_file):
                    shutil.copy2(source_file, target_file)
                    print(f"[CrossIB] 已复制: {hlsl_file}")
                    copied_count += 1
                else:
                    print(f"[CrossIB] 文件已存在，跳过: {hlsl_file}")
            else:
                print(f"[CrossIB] 警告: 源文件不存在: {source_file}")

        print(f"[CrossIB] 共复制 {copied_count} 个HLSL文件到 {res_dir}")


classes = (
    CrossIBItem,
    SSMT_OT_CrossIB_AddItem,
    SSMT_OT_CrossIB_RemoveItem,
    SSMTNode_CrossIB,
    SSMTNode_PostProcess_CrossIB,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
