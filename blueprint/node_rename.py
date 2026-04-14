
import bpy
from dataclasses import dataclass

from .node_base import SSMTNodeBase


@dataclass
class RenameRule:
    """单条重命名规则"""

    search_str: str = ""
    replace_str: str = ""

    def get_description(self) -> str:
        return f"'{self.search_str}' → '{self.replace_str}'"


class RenameRuleProperty(bpy.types.PropertyGroup):
    """单条重命名规则的属性定义"""

    search_str: bpy.props.StringProperty(
        name="Search",
        default="",
        description="要搜索的字符串"
    )

    replace_str: bpy.props.StringProperty(
        name="Replace",
        default="",
        description="替换成的字符串"
    )


class SSMT_UL_RenameRules(bpy.types.UIList):
    """重命名规则列表 UIList"""

    bl_idname = "SSMT_UL_RENAME_RULES"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)

            s_icon = 'VIEWZOOM' if item.search_str else 'ERROR'
            r_icon = 'FILE_REFRESH' if item.replace_str else 'ERROR'

            search_col = row.column(align=True)
            search_col.prop(item, "search_str", text="", icon=s_icon)

            arrow_col = row.column(align=True)
            arrow_col.alignment = 'CENTER'
            arrow_col.label(text="→", icon='RIGHTARROW')

            replace_col = row.column(align=True)
            replace_col.prop(item, "replace_str", text="", icon=r_icon)

        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text="", icon='FILE_TEXT')


class SSMT_OT_AddRenameRule(bpy.types.Operator):
    '''添加一条重命名规则'''
    bl_idname = "ssmt.add_rename_rule"
    bl_label = "Add Rule"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_node and context.active_node.bl_idname == 'SSMTNode_Object_Rename'

    def execute(self, context):
        node = context.active_node
        rule = node.rename_rules.add()
        rule.name = f"Rule_{len(node.rename_rules):03d}"
        node.active_rule_index = len(node.rename_rules) - 1
        return {'FINISHED'}


class SSMT_OT_RemoveRenameRule(bpy.types.Operator):
    '''删除选中的重命名规则'''
    bl_idname = "ssmt.remove_rename_rule"
    bl_label = "Remove Rule"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = getattr(context, 'active_node', None)
        return node and node.bl_idname == 'SSMTNode_Object_Rename' and len(node.rename_rules) > 0

    def execute(self, context):
        node = context.active_node
        idx = node.active_rule_index
        if 0 <= idx < len(node.rename_rules):
            node.rename_rules.remove(idx)
            if node.active_rule_index >= len(node.rename_rules):
                node.active_rule_index = max(0, len(node.rename_rules) - 1)
        return {'FINISHED'}


class SSMT_OT_MoveRenameRuleUp(bpy.types.Operator):
    '''将选中的规则向上移动'''
    bl_idname = "ssmt.move_rename_rule_up"
    bl_label = "Move Up"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = getattr(context, 'active_node', None)
        return node and node.bl_idname == 'SSMTNode_Object_Rename' and node.active_rule_index > 0

    def execute(self, context):
        node = context.active_node
        idx = node.active_rule_index
        if idx > 0:
            node.rename_rules.move(idx, idx - 1)
            node.active_rule_index = idx - 1
        return {'FINISHED'}


class SSMT_OT_MoveRenameRuleDown(bpy.types.Operator):
    '''将选中的规则向下移动'''
    bl_idname = "ssmt.move_rename_rule_down"
    bl_label = "Move Down"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = getattr(context, 'active_node', None)
        return node and node.bl_idname == 'SSMTNode_Object_Rename' and node.active_rule_index < len(node.rename_rules) - 1

    def execute(self, context):
        node = context.active_node
        idx = node.active_rule_index
        if idx < len(node.rename_rules) - 1:
            node.rename_rules.move(idx, idx + 1)
            node.active_rule_index = idx + 1
        return {'FINISHED'}


class SSMTNode_Object_Rename(SSMTNodeBase):
    '''Object Rename Node - 多规则物体名称修改器'''
    bl_idname = 'SSMTNode_Object_Rename'
    bl_label = 'Rename Object'
    bl_icon = 'OUTLINER'

    rename_rules: bpy.props.CollectionProperty(type=RenameRuleProperty)
    active_rule_index: bpy.props.IntProperty(name="Active Rule Index", default=0)
    reverse_mapping: bpy.props.BoolProperty(
        name="Reverse Mapping",
        default=False,
        description="全局反转映射：所有规则执行完后，按反向顺序再执行一遍（search ↔ replace 互换）"
    )

    def init(self, context):
        self.inputs.new('SSMTSocketObject', "Input")
        self.outputs.new('SSMTSocketObject', "Output")
        self.width = 380

    def draw_buttons(self, context, layout):
        col = layout.column(align=True)

        header = col.row(align=True)
        header.label(text="📝 重命名规则:", icon='OUTLINER')
        header.label(text=f"[{len(self.rename_rules)}]")

        list_row = col.row(align=True)

        rows = max(3, min(len(self.rename_rules), 6))
        list_row.template_list(
            "SSMT_UL_RENAME_RULES", "",
            self, "rename_rules",
            self, "active_rule_index",
            rows=rows, type='DEFAULT'
        )

        op_col = list_row.column(align=True)
        op_col.operator("ssmt.add_rename_rule", text="", icon='ADD')
        op_col.operator("ssmt.remove_rename_rule", text="", icon='REMOVE')
        op_col.separator(factor=0.5)
        op_col.operator("ssmt.move_rename_rule_up", text="", icon='TRIA_UP')
        op_col.operator("ssmt.move_rename_rule_down", text="", icon='TRIA_DOWN')

        layout.separator(factor=0.4)

        rev = layout.row(align=True)
        rev.prop(self, "reverse_mapping", text="↔️ 全局反转映射", toggle=True, icon='LOOP_BACK')

    @staticmethod
    def apply_to_object_name(object_name: str, node=None) -> tuple:
        """
        对物体名称应用此节点的所有规则（供蓝图解析引擎调用）

        这是节点对外暴露的核心接口，将处理逻辑封装在节点内部，
        保持 model.py (蓝图解析文件) 的简洁性。

        Args:
            object_name: 原始物体名称
            node: SSMTNode_Object_Rename 节点实例

        Returns:
            tuple: (new_name, was_modified, history_list, signature_str)
                   - new_name: 最终名称
                   - was_modified: 是否被修改过
                   - history_list: 每步的修改记录列表
                   - signature_str: 参数签名（用于哈希计算）
        """
        if not object_name or not node:
            return (object_name, False, [], "Rename[]")

        rules_list = []
        if hasattr(node, 'rename_rules'):
            for rule in node.rename_rules:
                rule_data = {
                    'search_str': getattr(rule, 'search_str', ''),
                    'replace_str': getattr(rule, 'replace_str', '')
                }
                rules_list.append(rule_data)

        reverse_mapping = getattr(node, 'reverse_mapping', False)

        new_name, was_modified, history = SSMTNode_Object_Rename.apply_rename_rules(
            object_name,
            rules_list,
            reverse_mapping=reverse_mapping
        )

        signature = SSMTNode_Object_Rename.generate_signature(rules_list, reverse_mapping)

        return (new_name, was_modified, history, signature)

    @staticmethod
    def apply_rename_rules(object_name: str, rename_rules: list, reverse_mapping: bool = False) -> tuple:
        """
        按顺序应用多条重命名规则（内部实现）

        Args:
            object_name: 原始物体名称
            rename_rules: 重命名规则列表，每项为 dict:
                - search_str: 搜索字符串
                - replace_str: 替换字符串
            reverse_mapping: 是否全局反转映射

        Returns:
            tuple: (new_name, was_modified, history_list)
        """
        import sys
        from ..utils.log_utils import LOG as _LOG

        if not object_name or not rename_rules:
            return (object_name, False, [])

        current_name = object_name
        total_modified = False
        history = []

        for i, rule in enumerate(rename_rules):
            old_name_before_rule = current_name

            search_str = rule.get('search_str', '')
            replace_str = rule.get('replace_str', '')

            if not search_str:
                continue

            new_name = current_name.replace(search_str, replace_str)

            was_modified_this_step = (new_name != old_name_before_rule)

            if was_modified_this_step:
                total_modified = True

                record = {
                    'rule_index': i + 1,
                    'old_name': old_name_before_rule,
                    'new_name': new_name,
                    'search': search_str,
                    'replace': replace_str,
                    'is_reversed': False
                }

                history.append(record)
                current_name = new_name
            else:
                pass

        if reverse_mapping and total_modified:
            _LOG.debug(f"   ↩️ 开始全局反转映射...")

            reversed_history = []

            for i in range(len(rename_rules) - 1, -1, -1):
                rule = rename_rules[i]
                old_name_before_reverse = current_name

                search_str = rule.get('search_str', '')
                replace_str = rule.get('replace_str', '')

                if not search_str:
                    continue

                new_name = current_name.replace(replace_str, search_str)

                was_reversed = (new_name != old_name_before_reverse)

                if was_reversed:
                    record = {
                        'rule_index': i + 1,
                        'old_name': old_name_before_reverse,
                        'new_name': new_name,
                        'search': replace_str,
                        'replace': search_str,
                        'is_reversed': True
                    }

                    reversed_history.append(record)
                    current_name = new_name

                    _LOG.debug(f"      [反转规则{i+1}] '{old_name_before_reverse}' → '{new_name}' (反向)")

            history.extend(reversed_history)

            _LOG.debug(f"   ↪️ 反转完成，共 {len(reversed_history)} 条反向操作")

        return (current_name, total_modified, history)

    @staticmethod
    def generate_signature(rename_rules: list, reverse_mapping: bool = False) -> str:
        """
        生成节点的参数签名（用于处理链哈希计算）

        Args:
            rename_rules: 规则数据列表
            reverse_mapping: 是否开启全局反转

        Returns:
            str: 签名字符串
        """
        params = []

        rule_count = len(rename_rules)
        if rule_count > 0:
            params.append(f"rules={rule_count}")

            rule_signatures = []
            for i, rule in enumerate(rename_rules):
                search_str = rule.get('search_str', '')
                replace_str = rule.get('replace_str', '')

                sig_parts = []
                if search_str:
                    sig_parts.append(f"'{search_str}'")
                if replace_str:
                    sig_parts.append(f"→'{replace_str}'")

                rule_sig = "|".join(sig_parts) if sig_parts else f"Rule{i+1}"
                rule_signatures.append(rule_sig)

            if len(rule_signatures) <= 3:
                params.extend(rule_signatures)
            else:
                params.append(f"[{rule_signatures[0]},{rule_signatures[1]},...,{rule_signatures[-1]}]")

        if reverse_mapping:
            params.append("rev=global")

        return f"Rename[{','.join(params)}]" if params else "Rename[]"

    @staticmethod
    def generate_debug_summary(processing_chains: list) -> str:
        """
        生成全局名称修改统计摘要（供蓝图解析引擎调用）

        Args:
            processing_chains: 所有处理链列表

        Returns:
            str: 统计摘要文本行
        """
        total_renames = sum(len(c.rename_history) for c in processing_chains)
        renamed_objects = sum(1 for c in processing_chains if c.rename_history)
        return f"名称修改操作: {total_renames} 次 (影响 {renamed_objects} 个物体)"

    @staticmethod
    def generate_debug_detail(rename_history: list) -> list:
        """
        生成单个物体的名称修改详情（供蓝图解析引擎调用）

        Args:
            rename_history: 单条处理链的 rename_history 列表

        Returns:
            list[str]: 调试文本行列表
        """
        lines = []
        forward_ops = [h for h in rename_history if not h.get('is_reversed')]
        reversed_ops = [h for h in rename_history if h.get('is_reversed')]

        lines.append(f"\n✏️ 名称修改历史 ({len(rename_history)}次操作, {len(forward_ops)}正向 + {len(reversed_ops)}反转):")
        for j, rename_op in enumerate(rename_history, 1):
            is_reversed = rename_op.get('is_reversed', False)
            rev_mark = " [↔️反转]" if is_reversed else ""

            lines.append(f"  [{j}] '{rename_op['old_name']}' → '{rename_op['new_name']}'{rev_mark}")
            lines.append(f"      规则 #{rename_op.get('rule_index', '?')}: 搜索'{rename_op.get('search', '')}', 替换为'{rename_op.get('replace', '')}'")

        return lines

    @staticmethod
    def execute_object_rename(original_obj_name: str, new_name: str, scene_objects=None) -> tuple:
        """
        实际执行物体重命名操作，处理同名冲突
        
        Args:
            original_obj_name: 原始物体名称
            new_name: 目标新名称
            scene_objects: 场景中的物体集合（用于检测冲突），如果为 None 则使用 bpy.data.objects
            
        Returns:
            tuple: (final_name, was_renamed, conflict_resolved)
                - final_name: 最终的物体名称
                - was_renamed: 是否执行了重命名
                - conflict_resolved: 是否解决了同名冲突
        """
        if scene_objects is None:
            scene_objects = bpy.data.objects
        
        obj = scene_objects.get(original_obj_name)
        if not obj:
            return (original_obj_name, False, False)
        
        if original_obj_name == new_name:
            return (original_obj_name, False, False)
        
        existing_obj = scene_objects.get(new_name)
        
        if existing_obj is None or existing_obj == obj:
            try:
                obj.name = new_name
                return (obj.name, True, False)
            except Exception:
                return (original_obj_name, False, False)
        
        final_name = SSMTNode_Object_Rename._resolve_name_conflict(new_name, scene_objects, obj)
        
        try:
            obj.name = final_name
            return (obj.name, True, True)
        except Exception:
            return (original_obj_name, False, False)

    @staticmethod
    def _resolve_name_conflict(target_name: str, scene_objects, exclude_obj=None) -> str:
        """
        解决物体名称冲突，在点后面插入数字序号
        
        例如：
        - 'd1a31f0b-7146-0.自定义名称_copy' → 'd1a31f0b-7146-0.001.自定义名称_copy'
        - 'd1a31f0b-7146-0.自定义名称' → 'd1a31f0b-7146-0.001.自定义名称'
        
        Args:
            target_name: 目标名称
            scene_objects: 场景物体集合
            exclude_obj: 要排除的物体（避免与自身比较）
            
        Returns:
            str: 解决冲突后的名称
        """
        dot_pos = target_name.find('.')
        
        if dot_pos == -1:
            prefix = target_name
            suffix = ""
        else:
            prefix = target_name[:dot_pos]
            suffix = target_name[dot_pos:]
        
        counter = 1
        while True:
            new_name = f"{prefix}.{counter:03d}{suffix}"
            
            existing = scene_objects.get(new_name)
            if existing is None or existing == exclude_obj:
                return new_name
            
            counter += 1
            
            if counter > 9999:
                import uuid
                return f"{prefix}.{uuid.uuid4().hex[:8]}{suffix}"

    @staticmethod
    def execute_batch_rename(rename_chains: list) -> dict:
        """
        批量执行物体重命名操作
        
        Args:
            rename_chains: 包含 rename_history 和 original_object_name 的处理链列表
            
        Returns:
            dict: {'total_renamed': int, 'total_conflicts': int}
        """
        from ..utils.log_utils import LOG
        
        rule_match_counts = {}
        total_renamed = 0
        total_conflicts = 0
        renamed_objects = {}
        
        for chain in rename_chains:
            original_name = chain.original_object_name
            new_name = chain.object_name
            
            if original_name == new_name:
                continue
            
            lookup_name = renamed_objects.get(original_name, original_name)
            
            for record in chain.rename_history:
                search = record.get('search', '')
                replace = record.get('replace', '')
                rule_key = f"'{search}' → '{replace}'"
                
                if rule_key not in rule_match_counts:
                    rule_match_counts[rule_key] = 0
            
            final_name, was_renamed, conflict_resolved = SSMTNode_Object_Rename.execute_object_rename(
                lookup_name,
                new_name
            )
            
            if was_renamed:
                total_renamed += 1
                chain.object_name = final_name
                renamed_objects[original_name] = final_name
                
                for record in chain.rename_history:
                    search = record.get('search', '')
                    replace = record.get('replace', '')
                    rule_key = f"'{search}' → '{replace}'"
                    rule_match_counts[rule_key] += 1
                
                if conflict_resolved:
                    total_conflicts += 1
            else:
                if original_name in renamed_objects:
                    chain.object_name = renamed_objects[original_name]
        
        if total_renamed == 0:
            return {'total_renamed': 0, 'total_conflicts': 0, 'renamed_objects': renamed_objects}
        
        LOG.info("🔧 重命名节点开始执行")
        
        for rule_key, count in rule_match_counts.items():
            if count > 0:
                LOG.info(f"   规则 {rule_key}: {count} 个物体匹配成功")
        
        conflict_str = f", 解决 {total_conflicts} 个冲突" if total_conflicts > 0 else ""
        LOG.info(f"   ✅ 重命名节点执行完成: {total_renamed} 个物体{conflict_str}")
        
        return {'total_renamed': total_renamed, 'total_conflicts': total_conflicts, 'renamed_objects': renamed_objects}

    @staticmethod
    def log_rename_summary(rename_chains: list):
        from ..utils.log_utils import LOG

        rule_match_counts = {}
        total_count = 0

        for chain in rename_chains:
            total_count += 1
            for record in chain.rename_history:
                search = record.get('search', '')
                replace = record.get('replace', '')
                rule_key = f"'{search}' → '{replace}'"

                if rule_key not in rule_match_counts:
                    rule_match_counts[rule_key] = 0
                rule_match_counts[rule_key] += 1

        if total_count == 0:
            return

        LOG.info("🔧 重命名节点（虚拟映射）")
        for rule_key, count in rule_match_counts.items():
            if count > 0:
                LOG.info(f"   规则 {rule_key}: {count} 个物体匹配")
        LOG.info(f"   ✅ 共 {total_count} 个物体将使用重命名映射")

    def validate_configuration(self) -> list:
        """验证节点配置"""
        issues = []

        if len(self.rename_rules) == 0:
            issues.append(("WARNING", "未添加任何重命名规则"))

        for i, rule in enumerate(self.rename_rules):
            if not rule.search_str:
                issues.append(("WARNING", f"规则 {i+1}: 未设置搜索字符串"))

        return issues

    def get_preview_result(self, sample_name: str = "4c11c155-288-7068.自定义名称") -> dict:
        """预览多规则修改效果"""
        rules_data = [
            {
                'search_str': r.search_str,
                'replace_str': r.replace_str
            }
            for r in self.rename_rules
        ]

        final_name, modified, history = SSMTNode_Object_Rename.apply_rename_rules(
            sample_name,
            rules_data,
            reverse_mapping=self.reverse_mapping
        )

        return {
            'original': sample_name,
            'final': final_name,
            'modified': modified,
            'rule_count': len(self.rename_rules),
            'reverse_mapping': self.reverse_mapping,
            'history': history
        }


classes = (
    RenameRuleProperty,
    SSMT_UL_RenameRules,
    SSMT_OT_AddRenameRule,
    SSMT_OT_RemoveRenameRule,
    SSMT_OT_MoveRenameRuleUp,
    SSMT_OT_MoveRenameRuleDown,
    SSMTNode_Object_Rename,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
