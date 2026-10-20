# -*- coding: utf-8 -*-
"""自定义材质指定：仅对用户在节点中指定的部件生成材质/贴图资源引用。

该节点复用“材质转资源”的完整生成逻辑，但处理前会先按目标物体白名单过滤：
未在列表中指定的 TextureOverride 段（即其它部件）不会被触碰，保持默认配置。
"""
import bpy
import glob
import json
import os
import re
import time
from collections import OrderedDict

from .node_postprocess_material import (
    MATERIAL_DETECT_PRESETS,
    SSMTNode_PostProcess_MaterialBase,
)


NODE_IDNAME = "SSMTNode_PostProcess_CustomMaterialAssign"
PICK_TIMEOUT_SECONDS = 30.0
RESTORE_MARKER_START = "; --- CustomMaterialAssign Restore Default ---"
RESTORE_MARKER_END = "; --- End CustomMaterialAssign Restore Default ---"

# 吸管拾取状态（与现有 node_obj.py 的 3D 视图吸管风格一致）。
_pick_context = {
    "node_name": "",
    "tree_name": "",
    "item_index": -1,
}

_switch_sync_guard = False

#: 角色激活标志候选。口径与 ``anim_driver_base._get_activation_flag``（NTEMI 用
#: ``$ntmi_active0``）、``node_swap_ini`` 与 ``m_ini_helper.add_branch_key_sections``
#: 的 ``$active0`` 一致。
ACTIVATION_FLAG_CANDIDATES = ("$active0", "$ntmi_active0")

#: 贴图槽位绑定行（``ps-tN = Resource_xxx`` / ``Resource\ZZMI\Xxx = ref Resource-yyy``）。
#: 与 ``node_postprocess_object_texture`` 的口径一致：只认这三种参数名。
_TEXTURE_BINDING_RE = re.compile(
    r"^\s*(?P<param>Resource\\[A-Za-z0-9_]+\\[A-Za-z0-9_]+|this|ps-t\d+)\s*=\s*"
    r"(?:ref\s+)?(?P<resource>[A-Za-z0-9_\-.]+)\s*(?:;.*)?$",
    re.IGNORECASE,
)

#: ``[Resource-...]``（基础导出 slot 风格）与 ``[Resource_...]``（材质转资源）定义段。
_RESOURCE_SECTION_RE = re.compile(r"^\[Resource[-_]", re.IGNORECASE)

#: ini 中的部件锚点注释 ``; [mesh:物体名]``（可带 ``[vertex_count:N]``）。
_MESH_COMMENT_RE = re.compile(r"\[mesh:([^\]]+)\]", re.IGNORECASE)

#: IB 键（``8位hex-数字-数字``，如 ``6ef3d666-888-0``）。
#: 导出 ini 的 mesh 注释常只带 IB 键（``LOD0.6ef3d666-888-0``），而 Blender
#: 物体名后面还挂着部件名（``LOD0.6ef3d666-888-0.武器翅膀花纹贴片``），精确
#: 名查找对不上，只能靠 IB 键把两者关联起来。前后边界避免把更长的 hex 串
#: 当成 IB 前缀截断匹配。
_IB_KEY_RE = re.compile(
    r"(?<![0-9a-fA-F])([0-9a-fA-F]{8})-(\d+)-(\d+)(?!\d)"
)

#: 探测「该部件读不读得到材质」时依次尝试的贴图类型。
#: 与材质转资源的前缀体系一致；只要有一种命中就认为材质可用。
_MATERIAL_PROBE_TYPES = (
    "DiffuseMap",
    "NormalMap",
    "LightMap",
    "MaterialMap",
    "Glowmap",
    "GlowMap",
    "FXMap",
    "TTLMap",
    "WengineFx",
)

#: 回退原理备忘（2026-10-06 实测结论，勿再引入 ``ps-tN = null``）：
#:
#: * 3DMigoto 的 TextureOverride 只在游戏 draw 之前**插入**指令。mod 不写
#:   ``ps-tN`` 赋值时，游戏自己绑定的那张纹理仍在槽位里 —— 这就是原生贴图。
#:   一旦 mod 写了 ``ps-tN = null`` 反而**显式解绑**，该部件会直接变成纯黑。
#: * 因此让“其他部件”用原生贴图的正确做法是：把它们的贴图引用置
#:   ``Resource\<ns>\Xxx = null``（ZZMI 的 SlotFix 用 ``unless_null`` 绑定，
#:   源为 null 时不会覆盖槽位），并且**保证它们的绘制点排在段内所有 MOD
#:   覆盖之前** —— 定义之前游戏原版贴图自然就在槽位上。
#: * 同 hash 段里的绘制顺序才是关键：非目标部件在前、目标部件在后。
#: * 由此得出**按参数类型分流**的回退规则（实现见 ``_is_slot_style_param``）：
#:   ``Resource\<ns>\Xxx`` 这类别名可以写 ``= null``（SlotFix 会放行）；
#:   而 ``ps-tN`` / ``this`` 直接指向槽位，只能**整行删除** —— 不写赋值就等于
#:   保留游戏原生贴图，写 ``= null`` 反而是显式解绑。

#: 资源定义段里的 ``filename = ...`` 行。
_FILENAME_LINE_RE = re.compile(
    r"^[ \t]*filename[ \t]*=[ \t]*(?P<value>.+?)[ \t]*$",
    re.IGNORECASE,
)

#: 只认这两种形态（行首 ``;`` 的注释行不算）：
#:   ``global $active0`` / ``global persist $active0 = 0`` —— 门控标志的声明；
#:   ``$active0 = 1``                                      —— 部件 TextureOverride 的置位。
#: 两者任一在位即认为导出器发射了整套门控机制（三件套同进同出，见 unity.py:66 /
#: zzmi.py:4099 / wwmi_export.py:444）。
#: **必须两种都认**：段字典按段名去重，实测产物（克拉蕾.ini）有 12 个 ``[Constants]``，
#: ``global $active0`` 所在的段会被后面的 ``[Constants]`` 顶掉 —— 只看声明会漏判，
#: 而置位行在唯一的 ``[TextureOverride_VB_*]`` 段里，字典里一定还在。
_ACTIVATION_FLAG_DECL_RE = re.compile(
    r"^[ \t]*global(?:[ \t]+persist)?[ \t]+(\$[A-Za-z_]\w*)",
    re.IGNORECASE,
)
_ACTIVATION_FLAG_SET_RE = re.compile(r"^[ \t]*(\$[A-Za-z_]\w*)[ \t]*=[ \t]*1[ \t]*$")


def _is_slot_style_param(param) -> bool:
    r"""绑定行的参数是不是「直接指向游戏槽位」的形态（``ps-tN`` / ``this``）。

    这类参数回退时**只能整行删除**：写 ``ps-tN = null`` / ``this = null`` 等于
    显式解绑，会让该部件直接变纯黑（见上方回退原理备忘）。``Resource\<ns>\Xxx``
    这类别名则写 ``= null``，是否覆盖槽位交给 ZZMI 的 SlotFix（``unless_null``）。
    """
    text = str(param or "").strip().lower()
    return text == "this" or text.startswith("ps-t")


def _collect_conditional_block_indexes(lines) -> set:
    r"""返回落在 ``if`` … ``endif`` 条件块内的行号集合。

    条件块里的赋值是**运行期按条件择一**的，最典型的就是材质切换组：同一个参数
    （如 ``Resource\RabbitFX\Glowmap``）在 ``if $swapkey15151 == 0`` / ``== 1``
    各写一条。这类行绝不能按「该网格绘制点之前最后一次赋值生效」判成孤儿 ——
    一判就会把除末档以外的每一档都改写成 ``= null``，表现为切换档位「空白」。

    2026-10-12 实测（``SSMT BluePrint`` 产物）：档 0 的
    ``Resource\RabbitFX\Glowmap = ref Resource_GlowMap_Gmsktwcnsg`` 被置成
    ``= null``，只剩档 1 的 ``Resource_GlowMap_Gmsktwcnsg.001`` 生效。
    """
    inside = set()
    depth = 0
    for index, line in enumerate(lines):
        stripped = str(line).strip().lower()
        if stripped == "if" or stripped.startswith("if ") or stripped.startswith("if("):
            depth += 1
            continue
        if stripped.startswith("endif"):
            depth = max(0, depth - 1)
            continue
        if depth > 0:
            inside.add(index)
    return inside


def _iter_ini_lines(sections):
    """把 ``{段名: [行...]}`` 摊平成行序列（容忍非行列表的簿记键）。"""
    values = sections.values() if hasattr(sections, "values") else (sections or ())
    for lines in values:
        if isinstance(lines, str):
            yield lines
            continue
        for line in list(lines or []):
            yield line


def find_activation_flag_in_sections(sections):
    """返回待写回 ini 段里可用的角色激活标志，没有则返回空串。

    判据取「门控机制在位」而不是「猜游戏支不支持」：整套机制（``[Constants]
    global $active0`` + 各部件 ``$active0 = 1`` + 主 ``[Present] post $active0 = 0``）
    由导出器在「蓝图存在切换按键」时**整体**发射。没有这套机制时给热键加
    ``condition = $active0 == 1`` 会恒假（未声明变量按 0 读）——按键直接失效，
    比不加门控更糟，所以这里宁可不加（并告警）。
    """
    declared, assigned = set(), set()
    for line in _iter_ini_lines(sections):
        text = str(line or "")
        match = _ACTIVATION_FLAG_DECL_RE.match(text)
        if match and match.group(1) in ACTIVATION_FLAG_CANDIDATES:
            declared.add(match.group(1))
            continue
        match = _ACTIVATION_FLAG_SET_RE.match(text)
        if match and match.group(1) in ACTIVATION_FLAG_CANDIDATES:
            assigned.add(match.group(1))
    for candidate in ACTIVATION_FLAG_CANDIDATES:
        if candidate in declared or candidate in assigned:
            return candidate
    return ""


def _log_missing_activation_flag(section_name):
    """缺角色激活标志时告警（否则用户只会看到"热键对别的角色也生效"）。"""
    message = (
        f"[材质转资源] {section_name} 未做角色门控：本 ini 没有角色激活标志"
        "（global $active0 / $ntmi_active0），切换热键会同时作用于其它角色。"
        "要让该键只在角色被绘制时生效，请给本模组任意一个物体切换 / 形态键按键节点后"
        "重新导出（导出器会随之发射 $active0 门控三件套）。"
    )
    try:
        from ..utils.log_utils import LOG as _LOG

        _LOG.warning(message)
    except Exception:
        print(message)


def _sync_switch_variable_fields(group, context):
    """同一切换变量的启用状态、备注与按键保持完全一致（限定在所属蓝图内）。

    一个切换变量在 UI 上只有一套控件、在 INI 里也只有一段 ``[KeySwap_Diffuse_*]``，
    所以这三项必须同变量同值；否则生成侧会被"最后一个组"的值覆盖。
    """
    global _switch_sync_guard
    if _switch_sync_guard:
        return
    variable = str(getattr(group, "switch_variable", "") or "").strip()
    if not variable:
        return
    # 只同步触发变量所属的蓝图树，避免不同蓝图同名变量互相干扰。
    try:
        owner_tree = group.id_data.id_data
    except Exception:
        owner_tree = None
    _switch_sync_guard = True
    try:
        enabled = bool(getattr(group, "enabled", True))
        comment = str(getattr(group, "comment", "") or "")
        hotkey = str(getattr(group, "key", "") or "")
        trees = [owner_tree] if owner_tree is not None else list(bpy.data.node_groups)
        for tree in trees:
            for node in getattr(tree, "nodes", []) or []:
                if getattr(node, "bl_idname", "") != NODE_IDNAME:
                    continue
                collections = [getattr(node, "global_switch_groups", [])]
                collections.extend(
                    getattr(item, "switch_groups", [])
                    for item in getattr(node, "target_items", [])
                )
                for collection in collections:
                    for candidate in collection:
                        if str(getattr(candidate, "switch_variable", "") or "").strip() == variable:
                            if candidate is group:
                                continue
                            candidate.enabled = enabled
                            candidate.comment = comment
                            candidate.key = hotkey
    finally:
        _switch_sync_guard = False


def _pick_variable_control_group(groups):
    """同一变量下的"控制组"：第一个填了按键的组，都没填则取第一个组。

    与 UI 控件组（``_draw_global_switch_panel`` 取 ``entries[0]``）和贴图切换
    面板取其第一个非空 hotkey 的既有口径一致；用户在一个共享框里填的键必须
    落到整个变量上。
    """
    fallback = None
    for group in groups:
        if fallback is None:
            fallback = group
        if str(getattr(group, "key", "") or "").strip():
            return group
    return fallback


def _normalize_switch_variable_metadata(collections):
    """把同一变量的 备注/按键/启用 归一为该变量控制组的值。

    扫描重建后每个部件组各自带着旧值，同伴可能停在默认 ``N``；归一后
    "看到的控件 = 实际生成的 KeySwap 段"，也不会在下一次导出时回退。
    返回每个变量的控制组，供调用方做一次显式跨节点同步。
    """
    ordered = []
    for collection in collections:
        ordered.extend(collection)
    by_variable = OrderedDict()
    for group in ordered:
        variable = str(getattr(group, "switch_variable", "") or "").strip()
        if variable:
            by_variable.setdefault(variable, []).append(group)
    controls = []
    for groups in by_variable.values():
        control = _pick_variable_control_group(groups)
        if control is None:
            continue
        key = str(getattr(control, "key", "") or "")
        comment = str(getattr(control, "comment", "") or "")
        enabled = bool(getattr(control, "enabled", True))
        for group in groups:
            group.key = key
            group.comment = comment
            group.enabled = enabled
        controls.append(control)
    return controls


def _mesh_object_poll(self, obj):
    return bool(getattr(obj, "type", "") == "MESH")


class SSMT_CustomMaterialAssignSwitchGroup(bpy.types.PropertyGroup):
    """某个目标部件的贴图切换控制组（由全局扫描创建）。"""

    object_name: bpy.props.StringProperty(
        name="部件",
        description="属于哪个部件的贴图切换组（全局模式使用）",
        default="",
        options={"HIDDEN"},
    )
    switch_variable: bpy.props.StringProperty(
        name="材质切换变量",
        description="生成到 INI 的控制变量，默认沿用材质转资源的 $swapkey 命名",
        default="",
    )
    key: bpy.props.StringProperty(
        name="切换按键",
        description="控制该部件多套贴图切换的按键；同一切换变量下的部件共享此按键",
        default="N",
        update=_sync_switch_variable_fields,
    )
    state_count: bpy.props.IntProperty(
        name="切换档数",
        default=2,
        min=2,
        max=64,
    )
    enabled: bpy.props.BoolProperty(
        name="启用切换",
        description="关闭后只使用第一套贴图，不写入按键切换",
        default=True,
        update=_sync_switch_variable_fields,
    )
    comment: bpy.props.StringProperty(
        name="备注",
        description="同一切换变量下的部件共享此备注，并写入 KeySwap 段",
        default="",
        update=_sync_switch_variable_fields,
    )
    bindings: bpy.props.StringProperty(
        name="材质绑定",
        description="内部使用：JSON 形式记录参与同一切换的各前缀材质组",
        default="",
        options={"HIDDEN"},
    )
    merge_group_id: bpy.props.StringProperty(
        name="合并组标识",
        default="",
        options={"HIDDEN"},
    )
    preview_state: bpy.props.IntProperty(
        name="预览档位",
        description="内部使用：该分组当前预览到第几套贴图（-1 = 未预览）",
        default=-1,
        options={"HIDDEN"},
    )


class SSMT_CustomMaterialAssignTargetItem(bpy.types.PropertyGroup):
    """目标部件：一个可拖入/选择的大纲视图网格物体。"""

    target_object: bpy.props.PointerProperty(
        name="目标部件",
        description="从大纲视图拖入，或点击右侧吸管在视口/大纲中选择",
        type=bpy.types.Object,
        poll=_mesh_object_poll,
    )
    switch_groups: bpy.props.CollectionProperty(
        type=SSMT_CustomMaterialAssignSwitchGroup
    )


def _find_node(context, node_name):
    tree = getattr(context.space_data, "edit_tree", None) or getattr(
        context.space_data, "node_tree", None
    )
    if not tree:
        return None
    node = tree.nodes.get(node_name)
    if node and node.bl_idname == NODE_IDNAME:
        return node
    return None


def _find_material_assign_node_from_blueprint(context):
    trees = []
    window = getattr(context, "window", None)
    screens = [getattr(window, "screen", None)] if window else []
    for candidate_window in getattr(getattr(context, "window_manager", None), "windows", []) or []:
        screen = getattr(candidate_window, "screen", None)
        if screen and screen not in screens:
            screens.append(screen)
    for screen in screens:
        for area in getattr(screen, "areas", []) or []:
            if area.type != "NODE_EDITOR":
                continue
            for space in getattr(area, "spaces", []) or []:
                tree = getattr(space, "edit_tree", None) or getattr(space, "node_tree", None)
                if getattr(tree, "bl_idname", "") == "SSMTBlueprintTreeType" and tree not in trees:
                    trees.append(tree)
    for tree in bpy.data.node_groups:
        if getattr(tree, "bl_idname", "") == "SSMTBlueprintTreeType" and tree not in trees:
            trees.append(tree)
    for tree in trees:
        for node in getattr(tree, "nodes", []) or []:
            if getattr(node, "bl_idname", "") == NODE_IDNAME:
                return node
    return None


class SSMT_OT_CustomMaterialAssignAddSelected(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_add_selected"
    bl_label = "将选中物体加入材质转资源"
    bl_description = "将当前选中的所有网格物体加入材质转资源 Pro 节点"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    def execute(self, context):
        node = _find_material_assign_node_from_blueprint(context)
        if node is None:
            self.report({"WARNING"}, "没有找到材质转资源节点")
            return {"CANCELLED"}
        if bool(getattr(node, "use_global_assign", False)):
            self.report({"WARNING"}, "全局指定模式下不支持添加")
            return {"CANCELLED"}
        selected = [obj for obj in (getattr(context, "selected_objects", None) or []) if getattr(obj, "type", "") == "MESH"]
        existing = {item.target_object for item in node.target_items if item.target_object}
        added = 0
        for obj in selected:
            if obj in existing:
                continue
            item = next((candidate for candidate in node.target_items if getattr(candidate, "target_object", None) is None), None)
            if item is None:
                item = node.target_items.add()
            item.target_object = obj
            existing.add(obj)
            added += 1
        if added:
            node.active_target_index = len(node.target_items) - 1
        self.report({"INFO"}, f"已加入 {added} 个部件" if added else "选中物体已在材质转资源中")
        return {"FINISHED"}


def _find_custom_node(node):
    """只接受材质转资源pro 节点（独立检测 operator 使用）。"""
    return bool(node is not None and getattr(node, "bl_idname", "") == NODE_IDNAME)


class SSMT_OT_CustomMaterialDetectAddPrefix(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_add_prefix"
    bl_label = "添加前缀"
    bl_description = "按预设顺序添加下一个材质检测前缀（材质转资源pro 独立）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        for preset in MATERIAL_DETECT_PRESETS:
            if preset not in existing:
                new_item = node.material_detect_prefixes.add()
                new_item.prefix = preset
                return {'FINISHED'}

        self.report({'WARNING'}, "所有预设前缀已添加")
        return {'CANCELLED'}


class SSMT_OT_CustomMaterialDetectRemovePrefix(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_remove_prefix"
    bl_label = "移除前缀"
    bl_description = "移除指定索引的材质检测前缀（材质转资源pro 独立）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node:
            if 0 <= self.item_index < len(node.material_detect_prefixes):
                node.material_detect_prefixes.remove(self.item_index)
        return {'FINISHED'}


class SSMT_OT_CustomMaterialDetectAddCustomPrefix(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_add_custom_prefix"
    bl_label = "添加自定义前缀"
    bl_description = "添加手动输入的材质检测前缀（材质转资源pro 独立）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}

        custom = node.temp_prefix_input.strip()
        if not custom:
            self.report({'WARNING'}, "请输入前缀")
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        if custom in existing:
            self.report({'WARNING'}, f"前缀 '{custom}' 已存在")
            return {'CANCELLED'}

        new_item = node.material_detect_prefixes.add()
        new_item.prefix = custom
        node.temp_prefix_input = ""
        return {'FINISHED'}


class SSMT_OT_CustomMaterialDetect(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect"
    bl_label = "Material Detect (Pro)"
    bl_description = "按材质转资源pro 的指定范围检测缺失前缀材质（含嵌套蓝图）"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def _collect_target_object_names(self, node):
        """返回本次检测的部件名列表：非全局=白名单部件，全局=蓝图链路部件。"""
        names = []
        seen = set()
        if bool(getattr(node, "use_global_assign", False)):
            tree = getattr(node, "id_data", None)
            for obj_name in _connected_blueprint_object_names(tree) if tree is not None else []:
                if obj_name in seen:
                    continue
                seen.add(obj_name)
                names.append(obj_name)
        else:
            for item in node.target_items:
                obj = getattr(item, "target_object", None)
                if obj is None or getattr(obj, "type", "") != "MESH":
                    continue
                if obj.name in seen:
                    continue
                seen.add(obj.name)
                names.append(obj.name)
        return names

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {'CANCELLED'}

        prefixes = [item.prefix.strip() for item in node.material_detect_prefixes if item.prefix.strip()]
        if not prefixes:
            self.report({'WARNING'}, 'Add at least one material prefix first')
            return {'CANCELLED'}

        unique_object_names = self._collect_target_object_names(node)
        if not unique_object_names:
            self.report({'WARNING'}, '未找到需要检测的部件（非全局模式请先在材质转资源pro节点中添加目标部件）')
            return {'CANCELLED'}

        node.detected_materials.clear()

        missing_count = 0
        for obj_name in unique_object_names:
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                continue

            for prefix in prefixes:
                has_prefix_material = False
                for material_slot in obj.material_slots:
                    material = material_slot.material
                    if material and material.name.startswith(prefix):
                        has_prefix_material = True
                        break

                if has_prefix_material:
                    continue

                item = node.detected_materials.add()
                item.object_name = obj_name
                item.missing_prefix = prefix
                missing_count += 1

        node.detect_all_ok = (missing_count == 0)

        if missing_count > 0:
            self.report({'WARNING'}, f'Detection finished: {missing_count} missing entries across {len(unique_object_names)} objects')
        else:
            self.report({'INFO'}, f'Detection finished: all prefixes found across {len(unique_object_names)} objects')
        return {'FINISHED'}


class SSMT_OT_CustomMaterialDetectClear(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_detect_clear"
    bl_label = "清除结果"
    bl_description = "清除材质转资源pro 的检测结果"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node:
            node.detected_materials.clear()
            node.detect_all_ok = False
        return {'FINISHED'}


#: 结果输出节点的全部 idname（各导出入口各一个），扫描时作为链路起点识别。
_RESULT_OUTPUT_IDNAMES = {
    "SSMTNode_Result_Output",
    "SSMTNode_Result_Output_NTMIModImp",
    "SSMTNode_VeloExportBridge",
}


def _node_visit_key(node):
    tree_name = getattr(getattr(node, "id_data", None), "name", "")
    return f"{tree_name}::{getattr(node, 'name', '')}"


def _find_connected_result_output(start_node):
    """从后处理节点沿输入链向上找结果输出节点；找不到返回 None。"""
    visited = set()

    def walk(node):
        if node is None:
            return None
        key = _node_visit_key(node)
        if key in visited:
            return None
        visited.add(key)

        if getattr(node, "bl_idname", "") in _RESULT_OUTPUT_IDNAMES:
            return node

        for socket in getattr(node, "inputs", []) or []:
            if getattr(socket, "bl_idname", "") != "SSMTSocketPostProcess" or not socket.is_linked:
                continue
            for link in socket.links:
                found = walk(link.from_node)
                if found is not None:
                    return found
        return None

    return walk(start_node)


def _find_tree_result_output(tree):
    """在当前蓝图树里找结果输出节点（节点还没接进链路时用）。"""
    for candidate in getattr(tree, "nodes", []) or []:
        if getattr(candidate, "bl_idname", "") in _RESULT_OUTPUT_IDNAMES:
            return candidate
    return None


def _collect_connected_object_names(start_node):
    """沿已连接的节点链收集物体信息节点引用的物体名（含嵌套蓝图）。

    从结果输出节点反向遍历输入链：物体信息节点取 ``object_name``，多文件导出
    节点取 ``object_list``；遇到嵌套蓝图则进入它的结果输出节点继续遍历。
    被静音（``mute``）的节点在 Blender 里等同于停用、不参与导出，因此不收集
    （与同文件的 ``_connected_blueprint_object_names`` 同口径）。
    返回按出现顺序去重的物体名列表。
    """
    visited_nodes = set()
    visited_trees = set()
    names = []
    seen_names = set()

    def add_name(raw_name):
        obj_name = str(raw_name or "").strip()
        if obj_name and obj_name not in seen_names:
            seen_names.add(obj_name)
            names.append(obj_name)

    def walk(node):
        if node is None:
            return
        key = _node_visit_key(node)
        if key in visited_nodes:
            return
        visited_nodes.add(key)

        node_type = getattr(node, "bl_idname", "")
        # 静音节点不收集名字（静音 = 停用、不参与导出）；但仍继续向上遍历，
        # 因为 Blender 里静音节点的连线依然存在，链路不会因此断开。
        if not getattr(node, "mute", False):
            if node_type == "SSMTNode_Object_Info":
                add_name(getattr(node, "object_name", ""))
            elif node_type == "SSMTNode_MultiFile_Export":
                for item in getattr(node, "object_list", []) or []:
                    add_name(getattr(item, "object_name", ""))
            elif node_type == "SSMTNode_Blueprint_Nest":
                tree_name = str(getattr(node, "blueprint_name", "") or "").strip()
                if tree_name and tree_name != "NONE" and tree_name not in visited_trees:
                    visited_trees.add(tree_name)
                    nested_tree = bpy.data.node_groups.get(tree_name)
                    if nested_tree and getattr(nested_tree, "bl_idname", "") == "SSMTBlueprintTreeType":
                        for nested_node in nested_tree.nodes:
                            if getattr(nested_node, "bl_idname", "") in _RESULT_OUTPUT_IDNAMES:
                                walk(nested_node)

        for socket in getattr(node, "inputs", []) or []:
            if not socket.is_linked:
                continue
            for link in socket.links:
                walk(link.from_node)

    walk(start_node)
    return names


class SSMT_OT_CustomMaterialAssignScanObjects(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_scan_objects"
    bl_label = "扫描蓝图物体"
    bl_description = (
        "扫描当前蓝图节点树上已连接的物体信息节点（含嵌套蓝图），"
        "把它们引用的物体自动加入下方部件输入框；已在列表中的不会重复添加"
    )
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node is None:
            self.report({"WARNING"}, "没有找到材质转资源pro 节点")
            return {"CANCELLED"}
        if bool(getattr(node, "use_global_assign", False)):
            self.report({"WARNING"}, "全局指定模式下不需要部件输入框")
            return {"CANCELLED"}

        result_output = _find_connected_result_output(node)
        if result_output is None:
            # 节点还没接进链路时（刚拖进来），退化为在当前蓝图树里找结果输出节点
            result_output = _find_tree_result_output(getattr(node, "id_data", None))
        if result_output is None:
            self.report({"WARNING"}, "当前蓝图里没有结果输出节点，无法扫描")
            return {"CANCELLED"}

        object_names = _collect_connected_object_names(result_output)
        if not object_names:
            self.report({"WARNING"}, "蓝图里没有已连接的物体信息节点")
            return {"CANCELLED"}

        existing = {item.target_object for item in node.target_items if item.target_object}
        added = 0
        missing = 0
        not_mesh = 0
        for obj_name in object_names:
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                missing += 1
                continue
            if getattr(obj, "type", "") != "MESH":
                not_mesh += 1
                continue
            if obj in existing:
                continue
            item = next(
                (candidate for candidate in node.target_items
                 if getattr(candidate, "target_object", None) is None),
                None,
            )
            if item is None:
                item = node.target_items.add()
            item.target_object = obj
            existing.add(obj)
            added += 1

        if added:
            node.active_target_index = len(node.target_items) - 1

        message = f"扫描到 {len(object_names)} 个物体，新增 {added} 个部件"
        skipped = []
        if missing:
            skipped.append(f"{missing} 个已不在场景中")
        if not_mesh:
            skipped.append(f"{not_mesh} 个不是网格")
        if skipped:
            message += "（" + "，".join(skipped) + "，跳过）"
        self.report({"INFO"} if added else {"WARNING"}, message)
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignAddTarget(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_add_target"
    bl_label = "添加目标部件"
    bl_description = "新增一个部件指定输入框"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        item = node.target_items.add()
        node.active_target_index = len(node.target_items) - 1
        # 新输入框尽量默认为当前活动物体，减少手动操作。
        active = getattr(getattr(context, "view_layer", None), "objects", None)
        active_obj = getattr(active, "active", None)
        if active_obj and active_obj.type == "MESH":
            item.target_object = active_obj
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignClearTargets(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_clear_targets"
    bl_label = "清空部件"
    bl_description = "清空部件输入框，只保留 1 个空项"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        removed = len(node.target_items)
        # 部件马上要被清掉，预览过的材质必须先还原：分组条目没了就没人再能写回原材质。
        _restore_all_preview(node)
        _restore_preview_material_shading(node, context)
        node.target_items.clear()
        node.target_items.add()
        node.active_target_index = 0
        self.report({"INFO"}, f"已清空部件输入框（原有 {removed} 项，保留 1 个空项）")
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignRemoveTarget(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_remove_target"
    bl_label = "移除目标部件"
    bl_description = "移除当前目标部件输入框（至少保留一个）"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        if len(node.target_items) <= 1:
            self.report({"WARNING"}, "至少保留一个目标部件输入框")
            return {"CANCELLED"}
        index = self.item_index
        if not 0 <= index < len(node.target_items):
            return {"CANCELLED"}
        # 共享组合并组内第一次点击叉号只解除合并，保留为独立部件窗口。
        item = node.target_items[index]
        if item.switch_groups:
            for group in list(item.switch_groups):
                variable = str(getattr(group, "switch_variable", "") or "").strip()
                merge_id = str(getattr(group, "merge_group_id", "") or "").strip()
                if not merge_id:
                    continue
                peers = []
                for other in node.target_items:
                    if other is item:
                        continue
                    peers.extend(
                        candidate for candidate in other.switch_groups
                        if str(getattr(candidate, "switch_variable", "") or "").strip() == variable
                        and str(getattr(candidate, "merge_group_id", "") or "").strip() == merge_id
                    )
                if peers:
                    group.enabled = peers[0].enabled
                    group.comment = peers[0].comment
                    group.key = peers[0].key
                    group.merge_group_id = ""
                    return {"FINISHED"}
        node.target_items.remove(index)
        node.active_target_index = min(
            max(0, self.item_index - 1),
            max(0, len(node.target_items) - 1),
        )
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignPickTarget(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_pick_target"
    bl_label = "拾取目标部件"
    bl_description = "点击后选择要指定材质的网格部件（可在 3D 视口或大纲中点击）"
    bl_options = {"REGISTER", "INTERNAL"}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty(min=0)

    def execute(self, context):
        global _pick_context
        tree = getattr(context.space_data, "edit_tree", None) or getattr(
            context.space_data, "node_tree", None
        )
        if not tree:
            self.report({"WARNING"}, "无法获取节点树上下文")
            return {"CANCELLED"}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != NODE_IDNAME:
            self.report({"WARNING"}, "目标节点不存在")
            return {"CANCELLED"}
        if not 0 <= self.item_index < len(node.target_items):
            return {"CANCELLED"}

        _pick_context["node_name"] = self.node_name
        _pick_context["tree_name"] = tree.name
        _pick_context["item_index"] = self.item_index
        self.report({"INFO"}, "请选择要指定材质的部件（网格物体）")
        bpy.ops.ssmt.custom_material_assign_pick_modal("INVOKE_DEFAULT")
        return {"FINISHED"}


class SSMT_OT_CustomMaterialAssignPickTargetModal(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_assign_pick_modal"
    bl_label = "拾取目标部件"
    bl_options = {"REGISTER", "INTERNAL"}

    def _clear(self, context, status, clear_globals=True):
        global _pick_context
        timer = getattr(self, "_timer", None)
        if timer is not None:
            context.window_manager.event_timer_remove(timer)
            self._timer = None
        if clear_globals:
            _pick_context["node_name"] = ""
            _pick_context["tree_name"] = ""
            _pick_context["item_index"] = -1
        return status

    def _current_object(self, context):
        active = getattr(context.view_layer.objects, "active", None)
        if active and active in context.selected_objects:
            return active
        if context.selected_objects:
            return context.selected_objects[0]
        return None

    def _try_apply(self, context):
        global _pick_context
        obj = self._current_object(context)
        if obj is None:
            return None
        if obj == self._last_selected and obj in self._initial_selected:
            return None

        tree = bpy.data.node_groups.get(_pick_context["tree_name"])
        if tree is None:
            self.report({"WARNING"}, "节点树已失效，已取消吸管选择")
            return self._clear(context, {"CANCELLED"})
        node = tree.nodes.get(_pick_context["node_name"])
        if node is None or node.bl_idname != NODE_IDNAME:
            self.report({"WARNING"}, "目标节点已失效，已取消吸管选择")
            return self._clear(context, {"CANCELLED"})
        index = _pick_context["item_index"]
        if not 0 <= index < len(node.target_items):
            return self._clear(context, {"CANCELLED"})

        if obj.type != "MESH":
            if not getattr(self, "_non_mesh_warned", False):
                self._non_mesh_warned = True
                self.report({"WARNING"}, "只能指定网格物体作为部件")
            return None
        node.target_items[index].target_object = obj
        self.report({"INFO"}, f"已指定部件: {obj.name}")
        return self._clear(context, {"FINISHED"})

    def invoke(self, context, event):
        global _pick_context
        if not _pick_context["node_name"]:
            return {"CANCELLED"}
        self._initial_selected = set(context.selected_objects)
        self._last_selected = self._current_object(context)
        self._non_mesh_warned = False
        self._started_at = time.monotonic()
        self._timer = context.window_manager.event_timer_add(0.1, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        global _pick_context
        if not _pick_context["node_name"]:
            return self._clear(context, {"CANCELLED"}, clear_globals=False)

        if time.monotonic() - self._started_at > PICK_TIMEOUT_SECONDS:
            self.report({"WARNING"}, "拾取超时，已自动取消")
            return self._clear(context, {"CANCELLED"})

        if event.type in {"ESC", "RIGHTMOUSE"}:
            return self._clear(context, {"CANCELLED"})

        if event.type == "TIMER":
            result = self._try_apply(context)
            if result is not None:
                return result
            return {"RUNNING_MODAL"}

        if event.type in {
            "LEFTMOUSE",
            "MIDDLEMOUSE",
            "MOUSEMOVE",
            "WHEELUPMOUSE",
            "WHEELDOWNMOUSE",
            "TRACKPADPAN",
            "TRACKPADZOOM",
        }:
            return {"PASS_THROUGH"}
        return {"RUNNING_MODAL"}


def _draw_picking_header(self, context):
    if _pick_context["node_name"]:
        self.layout.label(
            text="请选择要指定材质的部件（网格物体）...",
            icon="EYEDROPPER",
        )


#: 明确不参与贴图切换的材质前缀。
#:
#: 渲染/预览用的自造材质首段不是贴图类型名（``IMGPV_<网格名>``，见
#: ``common/marked_texture_material.py`` 的 RENDER_MATERIAL_PREFIX），必须排除。
#:
#: 除此之外**不再限制类型**：原实现用 ``MATERIAL_DETECT_PRESETS`` 加几个硬编码
#: 类型做白名单，把 BodyMaskMap 这类真实贴图类型挡在外面 —— 它们即使在同一部件上
#: 有多套材质也成不了切换组。生成侧本来就不限类型
#: （``extract_texture_type_from_resource`` / ``find_matching_materials`` 都只是按
#: 前缀字符串匹配），所以白名单只会漏掉本该支持的贴图。
_SWITCH_PREFIX_EXCLUDE = {
    "imgpv",
}


def _parse_switch_var_base(node):
    value = str(getattr(node, "material_switch_var", "") or "").strip()
    if not value:
        value = "$swapkey150"
    match = re.match(r"^(\$\w+?)(\d+)$", value)
    if match:
        return match.group(1), int(match.group(2))
    return "$swapkey", 0


def _collect_switch_prefix_groups(obj):
    """按材质前缀收集同一部件的多套材质。

    不限贴图类型（只排除 ``_SWITCH_PREFIX_EXCLUDE`` 里的自造材质）：材质转资源的
    生成侧按前缀字符串匹配，任何前缀的多套材质都能生成切换行，所以这里也照此口径。
    """
    groups = OrderedDict()
    if obj is None or getattr(obj, "type", "") != "MESH":
        return groups
    for material_slot in getattr(obj, "material_slots", []) or []:
        material = material_slot.material
        if not material:
            continue
        name = str(getattr(material, "name", "") or "")
        parts = name.split("_", 1)
        if len(parts) < 2:
            continue
        prefix = parts[0].strip()
        if not prefix or prefix.casefold() in _SWITCH_PREFIX_EXCLUDE:
            continue
        names = groups.setdefault(prefix.casefold(), [])
        if name not in names:
            names.append(name)
    return OrderedDict(
        (prefix, names)
        for prefix, names in groups.items()
        if len(names) > 1
    )


def _connected_blueprint_object_names(tree):
    """仿照物体切换面板：只返回链接到 Mod 输出节点的 Object Info / MultiFile。"""
    OUTPUT_IDS = {
        "SSMTNode_Result_Output",
        "SSMTNode_Result_Output_NTMIModImp", "SSMTNode_VeloExportBridge",
    }
    SOURCE_IDS = {
        "SSMTNode_Object_Info",
        "SSMTNode_MultiFile_Export",
    }

    result_names = []
    seen_trees = set()
    seen_nodes = set()

    def collect_tree(current_tree):
        if current_tree is None or current_tree.name in seen_trees:
            return
        seen_trees.add(current_tree.name)

        output_node = None
        for candidate in current_tree.nodes:
            if candidate.bl_idname in OUTPUT_IDS:
                output_node = candidate
                break
        if output_node is None:
            return

        def is_connected(node):
            if node is None:
                return False
            check_visited = set()

            def check_reverse(current):
                if current is None:
                    return False
                current_key = (
                    getattr(current, "id_data", None).name if getattr(current, "id_data", None) else "",
                    current.name,
                )
                if current_key in check_visited:
                    return False
                check_visited.add(current_key)
                if current == node:
                    return True
                for input_socket in getattr(current, "inputs", []) or []:
                    if not input_socket.is_linked:
                        continue
                    for link in input_socket.links:
                        if check_reverse(link.from_node):
                            return True
                return False

            return check_reverse(output_node)

        for node in current_tree.nodes:
            if node.bl_idname not in SOURCE_IDS or node.mute:
                continue
            node_key = (current_tree.name, node.name)
            if node_key in seen_nodes:
                continue
            if not is_connected(node):
                continue
            seen_nodes.add(node_key)
            if node.bl_idname == "SSMTNode_Object_Info":
                object_name = str(getattr(node, "object_name", "") or "")
                if object_name:
                    result_names.append(object_name)
            elif node.bl_idname == "SSMTNode_MultiFile_Export":
                for item in getattr(node, "object_list", []) or []:
                    object_name = str(getattr(item, "object_name", "") or "")
                    if object_name:
                        result_names.append(object_name)

        for node in current_tree.nodes:
            if node.bl_idname == "SSMTNode_Blueprint_Nest" and not node.mute:
                blueprint_name = str(getattr(node, "blueprint_name", "") or "")
                if blueprint_name and blueprint_name != "NONE":
                    nested = bpy.data.node_groups.get(blueprint_name)
                    if nested and getattr(nested, "bl_idname", "") == "SSMTBlueprintTreeType":
                        collect_tree(nested)

    collect_tree(tree)
    # 去重并保持顺序
    ordered = []
    seen = set()
    for name in result_names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


class SSMT_OT_CustomMaterialScanSwitches(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_scan_switches"
    bl_label = "全局扫描贴图切换"
    bl_description = "扫描所有指定部件的同名前缀材质，为可切换部件创建贴图切换控制组"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    @staticmethod
    def _binding_signature(binding_lists):
        """切换身份 = 这一组材质名清单本身（与顺序无关）。

        对齐原版「材质转资源」的判定：它用 ``tuple(sorted(mat.name ...))`` 作
        ``material_group_to_swapkey`` 的 key，只有**同一套材质**才复用同一个
        ``$swapkeyN``，材质不同就各领新号。套数相同但贴图不同（例如脸部 2 套、
        身体 2 套）必须各自成组 —— 18ab9ba 曾把身份换成"套数形状"
        （``_group_shape``），导致不同贴图因档数相同被并成一组。
        """
        return tuple(sorted(tuple(sorted(names)) for names in binding_lists))

    @classmethod
    def _group_signature(cls, group):
        try:
            bindings = json.loads(str(getattr(group, "bindings", "") or "[]"))
        except Exception:
            bindings = []
        if not isinstance(bindings, list):
            bindings = []
        return cls._binding_signature(
            [names for names in bindings if isinstance(names, (list, tuple))]
        )

    @classmethod
    def _material_group_signature(cls, groups):
        return cls._binding_signature(list(groups.values()))

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}

        # 预览改的是网格面材质索引；重新扫描会重建分组，先还原预览，
        # 否则"上一次预览停在第 N 套"的部件会被带进新分组。
        _restore_all_preview(node)
        _restore_preview_material_shading(node, context)

        prefix, next_number = _parse_switch_var_base(node)
        total_groups = 0
        total_parts = 0

        global_mode = bool(getattr(node, "use_global_assign", False))
        scan_objects = []
        if global_mode:
            try:
                current_tree = getattr(node, "id_data", None)
                scan_object_names = (
                    _connected_blueprint_object_names(current_tree)
                    if current_tree is not None
                    else []
                )
                seen_object_names = set()
                for obj_name in scan_object_names:
                    if obj_name in seen_object_names:
                        continue
                    seen_object_names.add(obj_name)
                    obj = bpy.data.objects.get(obj_name)
                    if obj is not None:
                        scan_objects.append(obj)
            except Exception:
                scan_objects = []
            if not scan_objects:
                self.report(
                    {"WARNING"},
                    "未在链接到 Mod 输出的蓝图链路中找到任何部件，已忽略未连接部件",
                )
                return {"CANCELLED"}
            # 必须先快照纯数据、再清空集合：PropertyGroup 项从集合里移除后，残留的
            # Python 引用不会抛 ReferenceError，而是静默读回属性默认值
            # （state_count→2、bindings/comment/object_name→""、key→"N"、
            # enabled→True；若随后 add 了新项，旧引用还会附身到新项内存上）。
            # 旧实现先 clear 再读字段，导致第二次扫描时每个旧组身份退化成空：
            # 同材质合并与旧变量沿用全部失效（分组结果与首次不同），用户设置的
            # 备注/按键/停用状态也被抹成默认值。
            old_global = {}
            for group in node.global_switch_groups:
                variable = str(getattr(group, "switch_variable", "") or "").strip()
                if not variable:
                    continue
                object_name = str(getattr(group, "object_name", "") or "")
                old_global[(object_name, variable)] = {
                    "variable": variable,
                    "signature": self._group_signature(group),
                    "comment": str(group.comment),
                    "key": str(group.key),
                    "enabled": bool(group.enabled),
                }
            node.global_switch_groups.clear()
        else:
            scan_objects = [
                item.target_object
                for item in node.target_items
            ]

        # 汇总旧组纯数据（全局分支已在清空前取好，非全局分支在上面的循环里取）。
        old_by_object = {}
        old_by_variable = {}
        if global_mode:
            for (object_name, variable), snapshot in old_global.items():
                old_by_object[object_name] = snapshot
                old_by_variable.setdefault(variable, snapshot)
        else:
            for item in node.target_items:
                for old_group in item.switch_groups:
                    if old_group.switch_variable:
                        snapshot = {
                            "variable": str(old_group.switch_variable), "signature": self._group_signature(old_group),
                            "comment": str(old_group.comment), "key": str(old_group.key), "enabled": bool(old_group.enabled),
                        }
                        old_by_object[item.target_object.name if item.target_object else ""] = snapshot
                        old_by_variable.setdefault(snapshot["variable"], snapshot)
            # 重新扫描后按当前材质套件重建，避免已不符合条件的部件残留在旧合并组。
            for item in node.target_items:
                item.switch_groups.clear()

        # signature -> variable：同材质集合的部件共用一个切换变量
        # variable -> signature：一个变量只归属一种材质集合（拆分历史误合并）
        signature_variables = {}
        variable_signatures = {}
        for variable, data in old_by_variable.items():
            signature_variables.setdefault(data["signature"], variable)

        # 旧变量会被沿用，新变量必须从“已占用的最大号 + 1”开始分配：否则链路里
        # 新增 / 改名 / 换贴图套数的部件会领到旧组已占用的号，被错并进同一个切换
        # 变量（KeySwap 段只有一份档数，两个部件会一起切且档数不符）。
        _used_numbers = [
            int(match.group(2))
            for match in (
                re.match(r"^\$?(\w+?)(\d+)$", str(variable))
                for variable in old_by_variable
            )
            if match and f"${match.group(1)}" == prefix
        ]
        if _used_numbers:
            next_number = max(next_number, max(_used_numbers) + 1)

        # 重建期间关掉同变量同步回调：批量重建时每个部件组都要先按自己的旧值落位，
        # 否则同伴默认值（N / 空备注）会在写入瞬间通过 update 回调把用户的键顶掉。
        # 重建完成后由下面的归一 + 一次显式同步统一该变量。
        global _switch_sync_guard
        previous_guard = _switch_sync_guard
        _switch_sync_guard = True
        control_groups = []
        try:
            for obj in scan_objects:
                if obj is None or getattr(obj, "type", "") != "MESH":
                    continue
                groups = _collect_switch_prefix_groups(obj)
                if not groups:
                    continue

                signature = self._material_group_signature(groups)
                old_data = old_by_object.get(obj.name)
                variable = ""
                # 沿用旧变量：材质没变才沿用；且该变量不能被别的材质集合占用
                # （历史版本按"档数形状"合并过，会出现脸部和身体共用 $swapkey150）。
                if old_data is not None and old_data["signature"] == signature:
                    candidate = old_data["variable"]
                    if variable_signatures.get(candidate, signature) == signature:
                        variable = candidate
                if not variable:
                    variable = signature_variables.get(signature, "")
                if not variable:
                    variable = f"{prefix}{next_number}"
                    next_number += 1
                signature_variables.setdefault(signature, variable)
                variable_signatures.setdefault(variable, signature)

                if global_mode:
                    container = node.global_switch_groups
                else:
                    item = next(
                        (candidate for candidate in node.target_items if candidate.target_object == obj),
                        None,
                    )
                    if item is None:
                        continue
                    container = item.switch_groups

                total_parts += 1
                group = container.add()
                group.object_name = obj.name
                group.switch_variable = variable
                group.merge_group_id = variable
                group.state_count = max(len(names) for names in groups.values())
                group.bindings = json.dumps(
                    [sorted(names) for names in groups.values()],
                    ensure_ascii=False,
                )
                metadata = old_data or old_by_variable.get(variable)
                if metadata is not None:
                    group.comment = metadata["comment"]
                    group.key = metadata["key"]
                    group.enabled = metadata["enabled"]
                else:
                    group.comment = ""
                    group.key = "N"
                    group.enabled = True
                total_groups += 1

                counts = {len(names) for names in groups.values()}
                if len(counts) > 1:
                    self.report(
                        {"WARNING"},
                        f"{obj.name} 各前缀贴图档数不一致，按键可能无法完整切换所有贴图",
                    )

            # 同一变量的 备注/按键/启用 归一：一个变量在 UI 上只有一套控件、在 INI 里
            # 只有一段 KeySwap，同伴若停在默认 N，导出时会把用户填的键覆盖掉。
            if global_mode:
                control_groups = _normalize_switch_variable_metadata(
                    [node.global_switch_groups]
                )
            else:
                control_groups = _normalize_switch_variable_metadata(
                    [item.switch_groups for item in node.target_items]
                )
        finally:
            _switch_sync_guard = previous_guard

        # 归一结果同步给同变量的其它节点（本节点各组已直接写好）。
        for control in control_groups:
            _sync_switch_variable_fields(control, None)

        if total_groups:
            self.report(
                {"INFO"},
                f"扫描完成：{total_parts} 个部件需要切换，共 {total_groups} 个切换组",
            )
        else:
            self.report({"INFO"}, "扫描完成：未发现可切换的多套同前缀贴图")
        return {"FINISHED"}


class SSMT_OT_CustomMaterialClearSwitches(bpy.types.Operator):
    bl_idname = "ssmt.custom_material_clear_switches"
    bl_label = "清除贴图切换扫描"
    bl_description = "删除所有已创建的贴图切换控制组"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if not node:
            return {"CANCELLED"}
        # 分组马上要被清掉，预览的部件必须先还原（否则没人再能写回原材质）。
        _restore_all_preview(node)
        _restore_preview_material_shading(node, context)
        cleared = 0
        for item in node.target_items:
            cleared += len(item.switch_groups)
            item.switch_groups.clear()
        cleared += len(node.global_switch_groups)
        node.global_switch_groups.clear()
        self.report({"INFO"}, f"已清除 {cleared} 个贴图切换控制组")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 贴图切换预览：点分组里的档位按钮，直接把对应部件的网格面切到那一套材质，
# 在视口里实时看到效果；「恢复」把面材质索引写回预览前的备份。
#
# 档位口径必须与生成侧一致：``generate_material_lines`` 写出
# ``if $swapkey == index`` 的 index 就是 ``find_matching_materials()`` 的序号，
# 而后者按**材质槽顺序**收集同前缀材质（并按材质签名去重）。所以预览也按材质槽
# 顺序取第 index 个材质，**不能按名称排序** —— 槽顺序是 [B, A] 时运行时的档位 0
# 是 B 而不是 A（扫描写下的 bindings 是排序后的名字，只用于标识分组身份）。
#
# 预览只改 ``polygon.material_index``（视口显示），不动材质槽、不动贴图、不参与
# 导出：材质转资源的 INI/资源生成读的是物体材质槽，与面材质索引无关。
# ---------------------------------------------------------------------------
PREVIEW_STATE_NONE = -1
PREVIEW_STATE_CYCLE = -2
PREVIEW_MAX_DIRECT_BUTTONS = 6
_MATERIAL_SHADING_TYPES = {"MATERIAL", "RENDERED"}

# 预览前的视口着色方式：{节点归属键: {视口键: 着色类型}}（会话级，不写进 .blend）
_preview_shading_backup = {}


def _group_preview_state(group):
    """该切换组当前预览到第几档（``PREVIEW_STATE_NONE`` = 未预览）。"""
    try:
        return int(getattr(group, "preview_state", PREVIEW_STATE_NONE))
    except Exception:
        return PREVIEW_STATE_NONE


def _set_group_preview_state(group, state):
    try:
        group.preview_state = int(state)
    except Exception:
        pass


def _has_active_preview(node):
    return any(
        _group_preview_state(group) != PREVIEW_STATE_NONE
        for group, _obj, _item in _iter_node_switch_groups(node)
    )


def _preview_prefix_set(group):
    """切换组覆盖的材质前缀（来自扫描写下的 bindings）。"""
    prefixes = set()
    try:
        bindings = json.loads(str(getattr(group, "bindings", "") or "") or "[]")
    except Exception:
        bindings = []
    if not isinstance(bindings, list):
        return prefixes
    for names in bindings:
        if not isinstance(names, (list, tuple)):
            continue
        for name in names:
            prefix = str(name or "").split("_", 1)[0].strip().casefold()
            if prefix:
                prefixes.add(prefix)
    return prefixes


def _material_signature_provider(node):
    """生成侧的去重口径 ``_build_material_signature``；取不到时退化为不去重。"""
    provider = getattr(node, "_build_material_signature", None)
    return provider if callable(provider) else None


def _slot_order_prefix_materials(obj, prefix, signature_of=None):
    """按材质槽顺序返回该前缀的全部材质名（同签名去重，与生成侧同序）。"""
    names = []
    seen = set()
    for slot in getattr(obj, "material_slots", []) or []:
        material = getattr(slot, "material", None)
        name = str(getattr(material, "name", "") or "")
        if not name or name.split("_", 1)[0].strip().casefold() != prefix:
            continue
        if signature_of is not None:
            try:
                signature = signature_of(material)
            except Exception:
                signature = ("__name__", name)
            if signature in seen:
                continue
            seen.add(signature)
        names.append(name)
    return names


def _read_material_indices(mesh):
    indices = [0] * len(mesh.polygons)
    mesh.polygons.foreach_get("material_index", indices)
    return indices


def _write_material_indices(mesh, indices):
    mesh.polygons.foreach_set("material_index", indices)
    mesh.update()


def _material_runs(indices):
    """材质索引 → 游程编码 ``[[档位, 连续面数], ...]``（备份要整串存进 RNA）。"""
    runs = []
    for value in indices:
        value = int(value)
        if runs and runs[-1][0] == value:
            runs[-1][1] += 1
        else:
            runs.append([value, 1])
    return runs


def _runs_to_indices(runs, expected_count):
    """游程解码；面数与备份不符（物体被改过）时返回 None，绝不写坏数据。"""
    if not isinstance(runs, (list, tuple)):
        return None
    indices = []
    for run in runs:
        if not isinstance(run, (list, tuple)) or len(run) != 2:
            return None
        try:
            slot = int(run[0])
            count = int(run[1])
        except Exception:
            return None
        if count <= 0:
            return None
        indices.extend([slot] * count)
    if len(indices) != int(expected_count):
        return None
    return indices


def _load_preview_backup(node):
    try:
        data = json.loads(str(getattr(node, "switch_preview_backup", "") or "") or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _store_preview_backup(node, backup):
    try:
        node.switch_preview_backup = (
            json.dumps(backup, ensure_ascii=False) if backup else ""
        )
    except Exception:
        pass


def _resolve_group_object(group, fallback_object=None):
    """切换组的目标物体：优先按扫描时写下的物体名找（重命名后自动失效）。"""
    name = str(getattr(group, "object_name", "") or "")
    if name:
        obj = bpy.data.objects.get(name)
        if obj is not None:
            return obj
    return fallback_object


def _iter_node_switch_groups(node):
    """遍历节点内全部切换组：``(group, 目标物体, 所属 target_item)``。"""
    for group in getattr(node, "global_switch_groups", []) or []:
        yield group, _resolve_group_object(group), None
    for item in getattr(node, "target_items", []) or []:
        fallback = getattr(item, "target_object", None)
        for group in getattr(item, "switch_groups", []) or []:
            yield group, _resolve_group_object(group, fallback), item


def _iter_variable_groups(node, variable):
    """同一材质切换变量的全部部件组（运行时的 KeySwap 也是整变量一起切）。"""
    normalized = str(variable or "").strip()
    if not normalized:
        return []
    return [
        entry
        for entry in _iter_node_switch_groups(node)
        if str(getattr(entry[0], "switch_variable", "") or "").strip() == normalized
    ]


def _preview_object_name(group, obj):
    name = str(getattr(obj, "name", "") or "")
    return name or str(getattr(group, "object_name", "") or "")


def _object_hidden_in_viewport(obj):
    for attr in ("hide_get", "hide_viewport"):
        try:
            value = getattr(obj, attr)
            value = value() if callable(value) else value
        except Exception:
            continue
        if bool(value):
            return True
    return False


def _apply_preview_state(obj, prefixes, state, signature_of=None):
    """把物体上属于这些前缀的面切到第 state 档材质；返回是否有改动。"""
    mesh = getattr(obj, "data", None)
    if mesh is None or not prefixes or not hasattr(mesh, "polygons"):
        return False
    prefix_materials = {}
    for prefix in prefixes:
        names = _slot_order_prefix_materials(obj, prefix, signature_of)
        if names:
            prefix_materials[prefix] = names
    if not prefix_materials:
        return False

    slot_index_by_name = {}
    slot_prefix = {}
    for index, slot in enumerate(getattr(obj, "material_slots", []) or []):
        material = getattr(slot, "material", None)
        name = str(getattr(material, "name", "") or "")
        if not name:
            continue
        slot_index_by_name.setdefault(name, index)
        prefix = name.split("_", 1)[0].strip().casefold()
        if prefix in prefix_materials:
            slot_prefix[index] = prefix
    if not slot_prefix:
        return False

    indices = _read_material_indices(mesh)
    changed = False
    for face, current in enumerate(indices):
        prefix = slot_prefix.get(current)
        if prefix is None:
            continue
        names = prefix_materials[prefix]
        target = state if 0 <= state < len(names) else len(names) - 1
        target_slot = slot_index_by_name.get(names[target])
        if target_slot is None or target_slot == current:
            continue
        indices[face] = target_slot
        changed = True
    if changed:
        _write_material_indices(mesh, indices)
    return changed


def _preview_switch_variable(node, variable, state, signature_of=None):
    """把某个切换变量下的全部部件切到第 state 档。

    首次预览某个物体前先记下它的原始面材质索引（游程编码存回节点），
    所以「恢复」写回的一定是**预览前**的样子，而不是上一档。
    返回 ``{"state", "changed", "missing", "hidden"}``；变量不存在时返回 None。
    """
    entries = _iter_variable_groups(node, variable)
    if not entries:
        return None
    backup = _load_preview_backup(node)
    backup_dirty = False
    changed = []
    missing = []
    hidden = []
    for group, obj, _item in entries:
        name = _preview_object_name(group, obj)
        if obj is None or getattr(obj, "type", "") != "MESH":
            missing.append(name)
            continue
        prefixes = _preview_prefix_set(group)
        if not prefixes:
            missing.append(name)
            continue
        if name and name not in backup:
            try:
                backup[name] = _material_runs(_read_material_indices(obj.data))
                backup_dirty = True
            except Exception:
                pass
        if _apply_preview_state(obj, prefixes, state, signature_of):
            changed.append(name)
        if _object_hidden_in_viewport(obj):
            hidden.append(name)
        _set_group_preview_state(group, state)
    if backup_dirty:
        _store_preview_backup(node, backup)
    return {
        "state": state,
        "changed": changed,
        "missing": missing,
        "hidden": hidden,
    }


def _restore_preview_objects(node, object_names):
    """把备份里的原始面材质索引写回这些物体；返回真正恢复的部件数。"""
    backup = _load_preview_backup(node)
    if not backup:
        return 0
    restored = 0
    for object_name in object_names:
        if object_name not in backup:
            continue
        runs = backup.pop(object_name)
        obj = bpy.data.objects.get(object_name)
        mesh = getattr(obj, "data", None) if obj is not None else None
        if mesh is None or not hasattr(mesh, "polygons"):
            continue
        indices = _runs_to_indices(runs, len(mesh.polygons))
        if indices is None:
            continue
        _write_material_indices(mesh, indices)
        restored += 1
    _store_preview_backup(node, backup)
    return restored


def _reset_preview_states(node):
    for group, _obj, _item in _iter_node_switch_groups(node):
        _set_group_preview_state(group, PREVIEW_STATE_NONE)


def _restore_preview_variable(node, variable):
    """退出某个分组的预览：还原它的部件并清掉该变量的预览档位。"""
    entries = _iter_variable_groups(node, variable)
    names = []
    for group, obj, _item in entries:
        name = _preview_object_name(group, obj)
        if name and name not in names:
            names.append(name)
        _set_group_preview_state(group, PREVIEW_STATE_NONE)
    return _restore_preview_objects(node, names)


def _restore_all_preview(node):
    """还原节点里全部预览过的部件（扫描/清除前也走这里收尾）。"""
    _reset_preview_states(node)
    backup = _load_preview_backup(node)
    return _restore_preview_objects(node, list(backup.keys()))


def _preview_owner_key(node):
    tree = getattr(node, "id_data", None)
    tree_name = str(getattr(tree, "name", "") or "")
    return f"{tree_name}::{str(getattr(node, 'name', '') or '')}"


def _iter_view3d_shading_spaces(context):
    """``((屏幕|区域|序号), 着色设置)``；无窗口/无 3D 视图时为空。"""
    window_manager = getattr(context, "window_manager", None)
    for window in getattr(window_manager, "windows", []) or []:
        screen = getattr(window, "screen", None)
        screen_name = str(getattr(screen, "name", "") or "")
        for area in getattr(screen, "areas", []) or []:
            if getattr(area, "type", "") != "VIEW_3D":
                continue
            area_name = str(getattr(area, "name", "") or "")
            for index, space in enumerate(getattr(area, "spaces", []) or []):
                shading = getattr(space, "shading", None)
                if shading is None:
                    continue
                yield f"{screen_name}|{area_name}|{index}", shading


def _ensure_preview_material_shading(node, context):
    """没有视口处于材质/渲染着色时临时切到材质预览，否则预览看不见贴图变化。

    已有材质预览视口就完全不动用户的着色设置；切换过的视口在退出预览时还原。
    """
    if not bool(getattr(node, "preview_material_shading", True)):
        return
    spaces = list(_iter_view3d_shading_spaces(context))
    if not spaces:
        return
    if any(
        str(getattr(shading, "type", "")) in _MATERIAL_SHADING_TYPES
        for _key, shading in spaces
    ):
        return
    backup = _preview_shading_backup.setdefault(_preview_owner_key(node), {})
    for key, shading in spaces:
        current = str(getattr(shading, "type", "") or "")
        if current in _MATERIAL_SHADING_TYPES:
            continue
        backup.setdefault(key, current)
        try:
            shading.type = "MATERIAL"
        except Exception:
            pass


def _restore_preview_material_shading(node, context):
    backup = _preview_shading_backup.pop(_preview_owner_key(node), None)
    if not backup:
        return
    spaces = dict(_iter_view3d_shading_spaces(context))
    for key, shading_type in backup.items():
        shading = spaces.get(key)
        if shading is None:
            continue
        try:
            shading.type = shading_type
        except Exception:
            pass


def _tag_redraw(context):
    area = getattr(context, "area", None)
    if area is None:
        return
    try:
        area.tag_redraw()
    except Exception:
        pass


class SSMT_OT_CustomMaterialPreviewSwitch(bpy.types.Operator):
    """把某个贴图切换分组的部件切到指定档位（视口实时预览）。"""

    bl_idname = "ssmt.custom_material_preview_switch"
    bl_label = "预览贴图切换"
    bl_description = (
        "把该分组对应部件的网格面切到指定档位的材质，直接在视口里看到贴图切换效果；"
        "点 ↺ 还原原始材质。只影响视口显示，不影响导出的 INI 与资源"
    )
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty(default="")
    switch_variable: bpy.props.StringProperty(default="")
    state: bpy.props.IntProperty(default=0)

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node is None:
            self.report({"WARNING"}, "未找到材质转资源pro 节点")
            return {"CANCELLED"}
        variable = str(self.switch_variable or "").strip()
        if not variable:
            self.report({"WARNING"}, "该切换组没有材质切换变量，请重新扫描")
            return {"CANCELLED"}

        if int(self.state) == PREVIEW_STATE_NONE:
            restored = _restore_preview_variable(node, variable)
            if not _has_active_preview(node):
                _restore_preview_material_shading(node, context)
            _tag_redraw(context)
            self.report(
                {"INFO"},
                f"已恢复 {restored} 个部件的原始材质"
                if restored
                else "该分组当前没有预览中的部件",
            )
            return {"FINISHED"}

        specs = _iter_variable_groups(node, variable)
        if not specs:
            self.report({"WARNING"}, "该贴图切换分组已失效，请重新扫描")
            return {"CANCELLED"}
        state_count = max(
            2,
            max(int(getattr(group, "state_count", 2) or 2) for group, _obj, _item in specs),
        )
        target = int(self.state)
        if target == PREVIEW_STATE_CYCLE:
            current = _group_preview_state(specs[0][0])
            # 第一下先跳开默认档（档 0），让用户马上看到"换过"的差别；
            # 之后循环 1 → 2 → … → 0 回到默认。
            target = (current + 1) % state_count if current >= 0 else (1 % state_count)
        target = max(0, min(target, state_count - 1))

        result = _preview_switch_variable(
            node, variable, target, _material_signature_provider(node)
        )
        if result is None:
            self.report({"WARNING"}, "该贴图切换分组已失效，请重新扫描")
            return {"CANCELLED"}
        _ensure_preview_material_shading(node, context)
        _tag_redraw(context)

        message = (
            f"贴图预览 {target + 1}/{state_count}：{len(result['changed'])} 个部件已切换"
            if result["changed"]
            else f"贴图预览 {target + 1}/{state_count}：材质已是该档"
        )
        if result["missing"]:
            message += f"，{len(result['missing'])} 个部件未找到"
        if result["hidden"]:
            message += "（有部件在视口隐藏，看不到效果）"
        self.report({"INFO"}, message)
        return {"FINISHED"}


class SSMT_OT_CustomMaterialPreviewRestoreAll(bpy.types.Operator):
    """退出本节点全部贴图预览并还原原始材质。"""

    bl_idname = "ssmt.custom_material_preview_restore_all"
    bl_label = "退出贴图预览"
    bl_description = "把本节点预览过的全部部件材质还原为预览前的样子"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    node_name: bpy.props.StringProperty(default="")

    def execute(self, context):
        node = _find_node(context, self.node_name)
        if node is None:
            self.report({"WARNING"}, "未找到材质转资源pro 节点")
            return {"CANCELLED"}
        restored = _restore_all_preview(node)
        _restore_preview_material_shading(node, context)
        _tag_redraw(context)
        self.report(
            {"INFO"},
            f"已还原 {restored} 个部件的原始材质" if restored else "没有需要还原的预览",
        )
        return {"FINISHED"}


class SSMTNode_PostProcess_CustomMaterialAssign(SSMTNode_PostProcess_MaterialBase):
    bl_idname = NODE_IDNAME
    bl_label = "材质转资源pro"
    bl_description = (
        "仅对指定部件按材质名称生成贴图资源引用；"
        "未指定部件保持默认配置，不受影响"
    )
    bl_icon = "OBJECT_DATA"

    target_items: bpy.props.CollectionProperty(
        type=SSMT_CustomMaterialAssignTargetItem
    )
    active_target_index: bpy.props.IntProperty(name="当前部件", default=0, min=0)
    restore_default_textures_after_draw: bpy.props.BoolProperty(
        name="自动恢复后续部件默认贴图",
        description=(
            "目标部件之后若还有未指定部件，会在这些部件的 mesh 绘制前恢复"
            "该段开头的默认贴图，避免后续部件串用这张自定义贴图"
        ),
        default=True,
    )
    clear_non_target_textures: bpy.props.BoolProperty(
        name="清除未指定部件的游戏贴图",
        description=(
            "导出时让目标部件列表之外的其他部件回退游戏原生贴图："
            "把它们的 Resource 别名置空（不写 ps-t 解绑，否则会变纯黑），"
            "并回收不再被引用的 Resource 定义段与 Textures 下对应贴图文件。"
            "依赖同 hash 段内的绘制顺序——非目标部件必须排在目标部件之前，"
            "排在之后的会被保持原样并计数上报。"
            "目标部件里读不到材质的（未设材质/材质节点不正确）同样按非目标处理"
            "并逐个提示。仅对能按 ; [mesh:...] 注释判定归属的段生效"
        ),
        default=False,
    )
    export_textures_as_dds: bpy.props.BoolProperty(
        name="导出时把贴图转成DDS",
        description=(
            "导出 Mod 时，材质引用的贴图若还不是 .dds（例如编辑期用的无损 PNG），"
            "先用 texconv 按现有 DDS 转换规则转成 DDS 放进 Mod 的 Textures 目录，"
            "INI 里的 filename 也指向转出来的 DDS。源贴图文件保留不动。"
            "关闭时按原样复制源文件"
        ),
        default=True,
    )
    use_global_assign: bpy.props.BoolProperty(
        name="使用全局指定",
        description=(
            "开启后忽略下方指定部件列表，按材质转资源的原始逻辑扫描并"
            "单独处理所有部件"
        ),
        default=False,
    )
    global_switch_groups: bpy.props.CollectionProperty(
        type=SSMT_CustomMaterialAssignSwitchGroup
    )
    switch_preview_backup: bpy.props.StringProperty(
        name="预览备份",
        description=(
            "内部使用：预览贴图切换前的网格面材质索引备份（游程编码），"
            "「恢复」按钮用它把部件材质写回原样"
        ),
        default="",
        options={"HIDDEN"},
    )
    preview_material_shading: bpy.props.BoolProperty(
        name="预览时切换到材质预览着色",
        description=(
            "点预览按钮时，若没有任何视口处于材质预览/渲染着色，自动切到材质预览"
            "（否则视口里看不到贴图变化），退出预览时还原原着色方式"
        ),
        default=True,
    )

    def init(self, context):
        super().init(context)
        self.width = 440
        if len(self.target_items) == 0:
            self.target_items.add()

    def _target_object_set(self):
        names = set()
        for item in self.target_items:
            obj = item.target_object
            if obj is not None and getattr(obj, "type", "") == "MESH":
                names.add(obj.name)
        return names

    @staticmethod
    def _ib_key_from_name(name):
        """从名字里提取 IB 键（``8位hex-数字-数字``，小写）；提取不到返回 ``None``。

        允许名字带 ``LOD0.`` 前缀、``.部件名`` 后缀与 ``_copy`` 之类的尾巴：
        导出 ini 的 mesh 注释通常只有 IB 键，而 Blender 物体名还带着部件名，
        两边要靠这个键对齐。匹配不到时返回 ``None``（调用方按归属不明处理）。
        """
        match = _IB_KEY_RE.search(str(name or ""))
        if not match:
            return None
        return "{}-{}-{}".format(
            match.group(1), match.group(2), match.group(3)
        ).lower()

    def _target_ib_set(self):
        """目标部件物体名里的 IB 键集合（小写）。

        同一个 IB 可能带多个部件（只指定其中一部分），所以集合里出现的 IB
        在名字查不到物体时按「可能含目标部件」保守处理。
        """
        keys = set()
        for name in self._target_object_set():
            key = self._ib_key_from_name(name)
            if key:
                keys.add(key)
        return keys

    def _is_primary_switch_group(self, group, collection):
        variable = str(getattr(group, "switch_variable", "") or "").strip()
        if not variable:
            return True
        for candidate in collection:
            if candidate == group:
                return True
            if str(getattr(candidate, "switch_variable", "") or "").strip() == variable:
                return False
        return True

    def _is_custom_target(self, obj):
        return obj is not None and obj.name in self._target_object_set()

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        """让段可以进入，但逐 mesh 时只允许本节点指定的部件匹配。

        材质转资源在一个 TextureOverride 段里会先取“第一条 mesh 注释”作为段对象；
        如果在这里直接按白名单过滤，像 testttt 这种排在 rei/jack 后面的目标部件，
        会因为第一条 mesh 不是目标而把整段跳过。因此无 object_filter 的“段入口
        查找”不限制，后面带材质过滤器的逐 mesh 查找才应用白名单。
        """
        if bool(getattr(self, "use_global_assign", False)):
            return super().find_object_by_mesh_name(mesh_name, object_filter)
        if object_filter is None:
            return super().find_object_by_mesh_name(mesh_name, None)

        if not self._target_object_set():
            return None

        original_filter = object_filter

        def combined_filter(candidate):
            if not self._is_custom_target(candidate):
                return False
            return bool(original_filter(candidate))

        object_filter = combined_filter
        return super().find_object_by_mesh_name(mesh_name, object_filter)

    def find_matching_materials(self, obj, texture_type):
        """TTL/FX 等按 mesh 扫描的入口也只允许指定的部件。"""
        if bool(getattr(self, "use_global_assign", False)):
            materials = super().find_matching_materials(obj, texture_type)
            return self._limit_disabled_switch_materials(materials)
        if obj is None or not self._is_custom_target(obj):
            return []
        materials = super().find_matching_materials(obj, texture_type)
        return self._limit_disabled_switch_materials(materials)

    def _disabled_switch_variables(self):
        """返回被关闭的切换变量；同变量任一组关闭即整体关闭。"""
        disabled = set()
        for spec in self._iter_switch_group_defs():
            if not spec["enabled"]:
                disabled.add(spec["variable"])
        return disabled

    def _limit_disabled_switch_materials(self, materials):
        """禁用切换时只保留材质槽顺序中的第一套材质。

        生成阶段某些纹理类型可能只返回完整切换绑定的一个子集（例如
        DiffuseMap 的三套材质中当前段只命中前两套）。旧逻辑要求名称集合
        与绑定集合完全相等，导致备用材质绕过过滤并被写入 INI。只要当前
        命中的多个材质全部属于同一个已禁用绑定，就应视为该切换组并禁用。
        """
        if not materials or len(materials) < 2:
            return materials
        names = tuple(sorted(str(getattr(material, "name", "") or "") for material in materials))
        name_set = set(names)
        disabled = self._disabled_switch_variables()
        if not disabled:
            return materials
        for spec in self._iter_switch_group_defs():
            if spec["variable"] in disabled and any(
                len(name_set) >= 2
                and name_set.issubset(set(str(name) for name in binding))
                for binding in spec["bindings"]
            ):
                return materials[:1]
        return materials

    def generate_material_lines(self, matching_materials, param_name, texture_type, obj,
                                texture_folder, all_sections,
                                object_to_diffuse_swapkey, material_group_to_swapkey,
                                swap_key_prefix, next_swap_key_num, used_swap_keys,
                                resource_name_provider=None):
        """最终生成入口的防线：禁用切换组时绝不生成备用资源/INI 条件块。"""
        matching_materials = self._limit_disabled_switch_materials(matching_materials)
        return super().generate_material_lines(
            matching_materials, param_name, texture_type, obj,
            texture_folder, all_sections, object_to_diffuse_swapkey,
            material_group_to_swapkey, swap_key_prefix, next_swap_key_num,
            used_swap_keys, resource_name_provider=resource_name_provider,
        )

    def _find_workspace_slot_materials(self, obj, slot_info):
        materials = super()._find_workspace_slot_materials(obj, slot_info)
        return self._limit_disabled_switch_materials(materials)

    def _collect_ps_texture_slot_materials(self, obj):
        slots = super()._collect_ps_texture_slot_materials(obj)
        disabled = self._disabled_switch_variables()
        if not disabled:
            return slots
        for slot_info in slots.values():
            materials = list(slot_info.get("materials", {}).values())
            limited = self._limit_disabled_switch_materials(materials)
            if len(limited) < len(materials):
                slot_info["materials"] = OrderedDict(
                    (self._build_material_signature(material), material)
                    for material in limited
                )
        return slots

    def _iter_switch_group_defs(self):
        global_mode = bool(getattr(self, "use_global_assign", False))
        if global_mode:
            collections = [self.global_switch_groups]
        else:
            collections = [item.switch_groups for item in self.target_items]
        for collection in collections:
            for group in collection:
                variable = str(getattr(group, "switch_variable", "") or "").strip()
                if not variable:
                    continue
                try:
                    bindings = json.loads(
                        str(getattr(group, "bindings", "") or "") or "[]"
                    )
                except Exception:
                    bindings = []
                if not isinstance(bindings, list):
                    bindings = []
                yield {
                    "item": None,
                    "group": group,
                    "enabled": bool(getattr(group, "enabled", True)),
                    "variable": variable,
                    "comment": str(getattr(group, "comment", "") or "").strip(),
                    "key": str(getattr(group, "key", "") or "").strip(),
                    "state_count": max(
                        2,
                        int(getattr(group, "state_count", 2) or 2),
                    ),
                    "bindings": [
                        tuple(names)
                        for names in bindings
                        if isinstance(names, (list, tuple))
                    ],
                }

    def _prepare_material_group_switch_map(self, material_group_to_swapkey):
        for spec in self._iter_switch_group_defs():
            for binding in spec["bindings"]:
                if binding:
                    material_group_to_swapkey.setdefault(
                        tuple(binding),
                        spec["variable"],
                    )

    def _max_ui_switch_number(self):
        prefix, _ = _parse_switch_var_base(self)
        max_number = -1
        for spec in self._iter_switch_group_defs():
            match = re.match(r"^\$?(\w+?)(\d+)$", spec["variable"])
            if not match:
                continue
            if f"${match.group(1)}" != prefix:
                continue
            max_number = max(max_number, int(match.group(2)))
        return max_number

    def _write_keyswap_section(self, sections, spec):
        variable = spec["variable"].lstrip("$")
        if not variable:
            return
        section_name = f"[KeySwap_Diffuse_{variable}]"
        state_count = spec["state_count"]
        values = ",".join(str(index) for index in range(state_count))
        condition = f"${variable} == 0 || ${variable} < {state_count}"
        # 角色门控：与同 ini 的 [KeySwap_*] 物体切换 / 动画驱动 KeyToggle 同口径 ——
        # 只有本部件当帧被绘制过（$active0 = 1）才响应热键。否则同一个键会把别的
        # 角色的贴图一起切走（实机产物 克拉蕾.ini：39 段 KeySwap 里只有
        # KeySwap_Diffuse_* 这两段没有门控）。
        activation_flag = find_activation_flag_in_sections(sections)
        if activation_flag:
            condition = f"{activation_flag} == 1 && ({condition})"
        else:
            _log_missing_activation_flag(section_name)
        lines = []
        if spec["comment"]:
            lines.append(f"; {spec['comment']}")
        lines.extend(
            [
                f"condition = {condition}",
                f"key = {spec['key']}",
                "type = cycle",
                f"${variable} = {values}",
            ]
        )
        sections[section_name] = lines

    def _strip_stale_keyswap_blocks(self, sections):
        """清理旧版本误写入的裸 KeySwap_Diffuse 段头及其内容。"""
        bare_header_re = re.compile(
            r"^KeySwap_Diffuse_[A-Za-z0-9_]+$",
            re.IGNORECASE,
        )
        continuation_re = re.compile(
            r"^(?:condition\s*=|key\s*=|type\s*=|\$[A-Za-z0-9_]+\s*=|\s*;)",
            re.IGNORECASE,
        )
        for section_key, section_lines in list(sections.items()):
            cleaned = []
            index = 0
            while index < len(section_lines):
                line = section_lines[index]
                stripped = str(line or "").strip()
                if bare_header_re.match(stripped):
                    index += 1
                    consumed = 0
                    while (
                        index < len(section_lines)
                        and consumed < 8
                    ):
                        candidate = str(section_lines[index] or "").strip()
                        if not candidate or candidate.startswith(";MARK:"):
                            break
                        if not continuation_re.match(candidate):
                            break
                        index += 1
                        consumed += 1
                    continue
                cleaned.append(line)
                index += 1
            sections[section_key] = cleaned

    def define_swapkeys_in_sections(self, sections, keys_to_define):
        """先声明全局变量，再为已扫描出的切换组写入 KeySwap 按键代码。"""
        self._strip_stale_keyswap_blocks(sections)
        super().define_swapkeys_in_sections(sections, keys_to_define)
        used_keys = set(keys_to_define or [])
        # 一个变量只写一段 [KeySwap_Diffuse_<var>]：同变量的每个部件组都会写出
        # 同名 section，后写覆盖先写，同伴停在默认 N 时会把用户的按键顶掉。
        # 取该变量里第一个填过按键的组为准（与 UI 控件组/贴图面板口径一致）。
        chosen = OrderedDict()
        for spec in self._iter_switch_group_defs():
            if not spec["enabled"]:
                continue
            if spec["variable"] not in used_keys:
                continue
            if not spec["key"]:
                continue
            chosen.setdefault(spec["variable"], spec)
        for spec in chosen.values():
            self._write_keyswap_section(sections, spec)

    def _section_custom_mesh_names(self, lines):
        """返回段内属于本节点指定部件的 mesh 注释名称。"""
        names = []
        seen = set()
        for line in lines:
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            obj = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if obj is not None and mesh_name not in seen:
                seen.add(mesh_name)
                names.append(mesh_name)
        return names

    def _capture_section_default_resources(self, lines):
        """捕获第一条 mesh 注释之前作为默认值的 Resource/ps-t 赋值与 run。"""
        defaults = OrderedDict()
        run_lines = []
        for line in lines:
            if self.extract_mesh_name(line):
                break
            stripped = str(line or "").strip()
            if not stripped:
                continue
            # 只保留后续“重建时会被材质转资源再次识别并清理”的 run，
            # 否则像 CommandListSkinTexture 这类行会残留在 mesh 块中重复累积。
            if stripped.startswith("run = "):
                run_lines.append(stripped)
                continue
            match = re.match(
                r"^(Resource\\[^=\s]+|ps-t\d+)\s*=\s*(.+)$",
                stripped,
                re.IGNORECASE,
            )
            if match:
                defaults.setdefault(match.group(1).strip().casefold(), stripped)
        return defaults, run_lines

    def _insert_restore_blocks(self, lines):
        """在目标 mesh 后面的第一个非指定 mesh 绘制前恢复默认贴图资源。

        不在目标 draw 之后做恢复，是因为当前游戏纹理状态下“draw 后恢复”并不可靠；
        改为在后续默认部件自己的 mesh 注释后插入默认赋值，和材质转资源
        “mesh 前先赋值再 draw”的既有写法一致，也更可靠。
        """
        if not bool(self.restore_default_textures_after_draw):
            return
        defaults, run_lines = self._capture_section_default_resources(lines)
        if not defaults:
            return

        default_block = [
            RESTORE_MARKER_START,
            *list(defaults.values()),
            *run_lines,
            RESTORE_MARKER_END,
        ]
        seen_custom_target = False
        restore_inserted = False
        mesh_index = 0
        while mesh_index < len(lines):
            line = lines[mesh_index]
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                mesh_index += 1
                continue
            target = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if target is not None:
                seen_custom_target = True
            elif seen_custom_target:
                lines[mesh_index + 1:mesh_index + 1] = default_block
                mesh_index += len(default_block) + 1
                restore_inserted = True
                continue
            mesh_index += 1
        # 指定部件位于段尾（例如被移动到段尾）时，段内没有后续默认部件可以
        # “顺路”恢复默认贴图，需要在段尾补一次恢复。
        # 仅对原神 GIMI ORFix 写法生效（ORFix 会在描边/夜魂通道按
        # ResourcePST1/PST2 抓取并写回槽位，残留的自定义贴图会污染未指定部件），
        # 其它游戏(ZZZ/崩铁/NTEMI…)保持原有行为，输出完全不变。
        from .node_postprocess_material import _load_gimi_orfix_adapter
        _orfix_adapter = _load_gimi_orfix_adapter()
        if (seen_custom_target and not restore_inserted
                and _orfix_adapter is not None
                and _orfix_adapter.uses_orfix(lines)):
            insert_at = len(lines)
            while insert_at > 0:
                stripped = str(lines[insert_at - 1]).strip()
                if not stripped or stripped.upper().startswith(";MARK:"):
                    insert_at -= 1
                    continue
                break
            lines[insert_at:insert_at] = default_block

    def _strip_generated_material_lines(self, lines, preserved_ps_slots=None):
        """先移除上一轮写入的恢复块，再交给材质转资源清理常规生成行。"""
        cleaned = []
        skipping_restore_block = False
        for line in lines:
            stripped = str(line or "").strip()
            if stripped == RESTORE_MARKER_START:
                skipping_restore_block = True
                continue
            if stripped == RESTORE_MARKER_END:
                skipping_restore_block = False
                continue
            if not skipping_restore_block:
                cleaned.append(line)
        return super()._strip_generated_material_lines(
            cleaned,
            preserved_ps_slots=preserved_ps_slots,
        )

    def _move_top_level_targets_to_end(self, lines):
        """把顶层（不被 if 包裹）的目标 mesh 绘制块移动到本段末尾。

        目标部件最后绘制时，后面不再有默认部件，就不会把自定义贴图状态带到其它
        部件。若目标位于 if/endif 内部，为保证条件块完整暂不移动，仍走默认恢复。
        """
        if_depth = 0
        top_mesh_indices = []
        marker_indices = []

        for index, line in enumerate(lines):
            stripped = str(line or "").strip()
            if not stripped:
                continue
            if re.match(r"^if\s+", stripped, re.IGNORECASE):
                if_depth += 1
                continue
            if stripped.casefold() == "endif":
                if_depth = max(0, if_depth - 1)
                continue
            if if_depth == 0:
                if self.extract_mesh_name(line):
                    top_mesh_indices.append(index)
                elif stripped.startswith(";MARK:"):
                    marker_indices.append(index)

        target_ranges = []
        draw_re = re.compile(r"^\s*drawindexed(?:instanced)?\s*=", re.IGNORECASE)
        for mesh_index in top_mesh_indices:
            line = lines[mesh_index]
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            target = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if target is None:
                continue

            # 目标 mesh 后的材质切换 if 会出现在其 draw 之前，应一起移动；
            # 只有看到目标自己的 draw 之后遇到的下一个顶层 mesh/if/MARK 才是边界。
            next_boundary = len(lines)
            nested_depth = 0
            seen_draw = False
            for scan_index in range(mesh_index + 1, len(lines)):
                scan_line = lines[scan_index]
                stripped = str(scan_line or "").strip()
                if not stripped:
                    continue
                if re.match(r"^if\s+", stripped, re.IGNORECASE):
                    if nested_depth == 0 and seen_draw:
                        next_boundary = scan_index
                        break
                    nested_depth += 1
                    continue
                if stripped.casefold() == "endif":
                    nested_depth = max(0, nested_depth - 1)
                    continue
                if nested_depth == 0:
                    if self.extract_mesh_name(scan_line):
                        next_boundary = scan_index
                        break
                    if stripped.startswith(";MARK:"):
                        next_boundary = scan_index
                        break
                    if draw_re.match(stripped):
                        seen_draw = True

            segment = lines[mesh_index:next_boundary]
            if not any(draw_re.match(str(item or "")) for item in segment):
                continue
            if not any(
                self._is_generated_material_line(str(item or "").strip())
                for item in segment
            ):
                continue
            target_ranges.append((mesh_index, next_boundary))

        if not target_ranges:
            return

        moved_segments = []
        for start, end in reversed(target_ranges):
            moved_segments.insert(0, lines[start:end])
            del lines[start:end]

        insert_at = len(lines)
        for index, line in enumerate(lines):
            stripped = str(line or "").strip()
            if stripped.startswith(";MARK:"):
                insert_at = index
                break
        for segment in moved_segments:
            lines[insert_at:insert_at] = segment
            insert_at += len(segment)

    def _find_flat_region_target_ranges(self, lines, region_start, region_end):
        """查找一段平铺 if 内容中的目标 mesh 范围（支持材质切换 if）。"""
        mesh_indices = []
        for index in range(region_start, region_end):
            if self.extract_mesh_name(lines[index]):
                mesh_indices.append(index)

        ranges = []
        draw_re = re.compile(r"^\s*drawindexed(?:instanced)?\s*=", re.IGNORECASE)
        for mesh_index in mesh_indices:
            line = lines[mesh_index]
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            target = self.find_object_by_mesh_name(
                mesh_name,
                object_filter=self._is_custom_target,
            )
            if target is None:
                continue

            segment_end = region_end
            nested_depth = 0
            seen_draw = False
            for scan_index in range(mesh_index + 1, region_end):
                scan_line = lines[scan_index]
                stripped = str(scan_line or "").strip()
                if not stripped:
                    continue
                if re.match(r"^if\s+", stripped, re.IGNORECASE):
                    nested_depth += 1
                    continue
                if stripped.casefold() == "endif":
                    if nested_depth == 0 and seen_draw:
                        segment_end = scan_index
                        break
                    if nested_depth > 0:
                        nested_depth -= 1
                    continue
                if nested_depth == 0:
                    if self.extract_mesh_name(scan_line):
                        segment_end = scan_index
                        break
                    if stripped.startswith(";MARK:"):
                        segment_end = scan_index
                        break
                    if draw_re.match(stripped):
                        seen_draw = True

            segment = lines[mesh_index:segment_end]
            if not any(draw_re.match(str(item or "")) for item in segment):
                continue
            if not any(
                self._is_generated_material_line(str(item or "").strip())
                for item in segment
            ):
                continue
            ranges.append((mesh_index, segment_end))
        return ranges

    def _move_targets_to_end_of_top_level_if_blocks(self, lines):
        """在每个顶层 if/endif 分支内，把目标 mesh 绘制块移到该分支 endif 前。"""
        top_level_blocks = []
        if_depth = 0
        block_start = -1
        for index, line in enumerate(lines):
            stripped = str(line or "").strip()
            if not stripped:
                continue
            if re.match(r"^if\s+", stripped, re.IGNORECASE):
                if if_depth == 0:
                    block_start = index
                if_depth += 1
            elif stripped.casefold() == "endif":
                if_depth -= 1
                if if_depth == 0 and block_start >= 0:
                    top_level_blocks.append((block_start, index))
                    block_start = -1

        for block_start, block_end in top_level_blocks:
            # 嵌套游戏 if 内部的 mesh 不能安全按平铺块重排，跳过由默认保护处理。
            nested_mesh_found = False
            scan_depth = 0
            for index in range(block_start + 1, block_end):
                stripped = str(lines[index] or "").strip()
                if re.match(r"^if\s+", stripped, re.IGNORECASE):
                    scan_depth += 1
                elif stripped.casefold() == "endif":
                    scan_depth = max(0, scan_depth - 1)
                elif scan_depth > 0 and self.extract_mesh_name(lines[index]):
                    nested_mesh_found = True
            if nested_mesh_found:
                continue

            ranges = self._find_flat_region_target_ranges(
                lines,
                block_start + 1,
                block_end,
            )
            if not ranges:
                continue

            moved_segments = []
            removed_count = 0
            for start, end in reversed(ranges):
                moved_segments.insert(0, lines[start:end])
                del lines[start:end]
                removed_count += end - start
            insert_at = block_end - removed_count
            for segment in moved_segments:
                lines[insert_at:insert_at] = segment
                insert_at += len(segment)

    def process_texture_override_section(
        self,
        section_name,
        all_sections,
        material_group_to_swapkey,
        swap_key_prefix=None,
        next_swap_key_num=None,
        used_swap_keys=None,
        transparency_sections_to_add=None,
    ):
        """只进入包含指定部件的段，并在其绘制后恢复默认贴图。"""
        if used_swap_keys is None or used_swap_keys is Ellipsis:
            used_swap_keys = set()
        if transparency_sections_to_add is None or transparency_sections_to_add is Ellipsis:
            transparency_sections_to_add = OrderedDict()
        if material_group_to_swapkey is None or material_group_to_swapkey is Ellipsis:
            material_group_to_swapkey = {}
        if swap_key_prefix is None or swap_key_prefix is Ellipsis:
            swap_key_prefix = None
        if next_swap_key_num is None or next_swap_key_num is Ellipsis:
            next_swap_key_num = 0

        if bool(getattr(self, "use_global_assign", False)):
            self._prepare_material_group_switch_map(material_group_to_swapkey)
            max_ui_number = self._max_ui_switch_number()
            if max_ui_number >= 0 and next_swap_key_num <= max_ui_number:
                next_swap_key_num = max_ui_number + 1
            return super().process_texture_override_section(
                section_name,
                all_sections,
                material_group_to_swapkey,
                swap_key_prefix=swap_key_prefix,
                next_swap_key_num=next_swap_key_num,
                used_swap_keys=used_swap_keys,
                transparency_sections_to_add=transparency_sections_to_add,
            )

        lines = all_sections.get(section_name, [])
        if not self._section_custom_mesh_names(lines):
            return next_swap_key_num

        self._prepare_material_group_switch_map(material_group_to_swapkey)
        max_ui_number = self._max_ui_switch_number()
        if max_ui_number >= 0 and next_swap_key_num <= max_ui_number:
            next_swap_key_num = max_ui_number + 1

        result = super().process_texture_override_section(
            section_name,
            all_sections,
            material_group_to_swapkey,
            swap_key_prefix=swap_key_prefix,
            next_swap_key_num=next_swap_key_num,
            used_swap_keys=used_swap_keys,
            transparency_sections_to_add=transparency_sections_to_add,
        )
        self._move_targets_to_end_of_top_level_if_blocks(lines)
        self._move_top_level_targets_to_end(lines)
        self._insert_restore_blocks(lines)
        return result

    def draw_material_detection_panel(self, context, layout):
        """材质检测窗口（可折叠），检测范围跟随“使用全局指定”开关。"""
        layout.separator()
        header_row = layout.row(align=True)
        header_row.prop(
            self,
            "show_detect_panel",
            icon="TRIA_DOWN" if self.show_detect_panel else "TRIA_RIGHT",
            text="材质检测",
            emboss=False,
        )
        if not self.show_detect_panel:
            return

        box = layout.box()
        prefix_row = box.row(align=True)
        prefix_row.label(text="检测前缀:", icon="FILTER")
        op = prefix_row.operator("ssmt.custom_material_detect_add_prefix", text="", icon="ADD")
        op.node_name = self.name

        for index, item in enumerate(self.material_detect_prefixes):
            row = box.row(align=True)
            row.label(text=item.prefix, icon="MATERIAL")
            remove = row.operator(
                "ssmt.custom_material_detect_remove_prefix",
                text="",
                icon="X",
            )
            remove.node_name = self.name
            remove.item_index = index

        input_row = box.row(align=True)
        input_row.prop(self, "temp_prefix_input", text="", icon="CONSOLE")
        add_custom = input_row.operator(
            "ssmt.custom_material_detect_add_custom_prefix",
            text="",
            icon="ADD",
        )
        add_custom.node_name = self.name

        btn_row = box.row(align=True)
        detect = btn_row.operator("ssmt.custom_material_detect", text="检测材质", icon="VIEWZOOM")
        detect.node_name = self.name
        clear = btn_row.operator("ssmt.custom_material_detect_clear", text="清除", icon="X")
        clear.node_name = self.name

        if self.detected_materials:
            result_box = box.box()
            result_box.label(
                text=f"缺失材质 ({len(self.detected_materials)} 个)",
                icon="ERROR",
            )
            for item in self.detected_materials:
                row = result_box.row(align=True)
                row.label(text=item.object_name, icon="OBJECT_DATA")
                row.label(text=f"缺少: {item.missing_prefix}", icon="ERROR")
        elif self.detect_all_ok:
            box.label(text="全部正确", icon="CHECKMARK")

    def _draw_switch_preview_row(self, layout, group):
        """一行贴图预览档位按钮：点一下就把该分组的部件切到那一档。

        档数少时每个档一个按钮（按下态 = 当前预览档），档数多时用一个循环按钮；
        预览中额外给一个「恢复」按钮（LOOP_BACK），点它写回预览前的材质。
        """
        variable = str(getattr(group, "switch_variable", "") or "").strip()
        if not variable:
            return
        state_count = max(2, int(getattr(group, "state_count", 2) or 2))
        current = _group_preview_state(group)

        row = layout.row(align=True)
        row.label(text="贴图预览", icon="HIDE_OFF")
        if state_count <= PREVIEW_MAX_DIRECT_BUTTONS:
            for index in range(state_count):
                button = row.operator(
                    "ssmt.custom_material_preview_switch",
                    text=str(index + 1),
                    depress=current == index,
                )
                button.node_name = self.name
                button.switch_variable = variable
                button.state = index
        else:
            button = row.operator(
                "ssmt.custom_material_preview_switch",
                text=f"{current + 1 if current >= 0 else 1}/{state_count}",
                depress=current >= 0,
            )
            button.node_name = self.name
            button.switch_variable = variable
            button.state = PREVIEW_STATE_CYCLE

        if current >= 0:
            restore = row.operator(
                "ssmt.custom_material_preview_switch", text="", icon="LOOP_BACK"
            )
            restore.node_name = self.name
            restore.switch_variable = variable
            restore.state = PREVIEW_STATE_NONE
            layout.label(
                text=(
                    f"预览中（第 {current + 1}/{state_count} 套贴图）"
                    "——只改视口显示，导出不受影响"
                ),
                icon="INFO",
            )

    def _draw_target_input_panel(self, context, layout):
        """非全局模式：显示目标部件输入框。"""
        title = layout.box()
        title.label(text="目标部件（仅这些部件会按材质生成）", icon="OBJECT_DATA")
        title.label(
            text="未在此列表中的部件保持默认配置，不会修改",
            icon="INFO",
        )

        rows = layout.box()
        header = rows.row(align=True)
        header.label(text=f"部件输入框（{len(self.target_items)} 个）")
        scan = header.operator(
            "ssmt.custom_material_assign_scan_objects", text="扫描蓝图物体", icon="VIEWZOOM"
        )
        scan.node_name = self.name
        add = header.operator(
            "ssmt.custom_material_assign_add_target", text="添加", icon="ADD"
        )
        add.node_name = self.name
        clear_targets = header.operator(
            "ssmt.custom_material_assign_clear_targets", text="清空", icon="TRASH"
        )
        clear_targets.node_name = self.name

        grouped = OrderedDict()
        for index, item in enumerate(self.target_items):
            for group in item.switch_groups:
                variable = str(getattr(group, "merge_group_id", "") or "").strip() or f"__single_{index}"
                if variable:
                    grouped.setdefault(variable, []).append((index, item, group))
        merged_groups = {variable: entries for variable, entries in grouped.items() if len(entries) > 1}
        primary_indices = {entries[0][0] for entries in merged_groups.values()}
        merged_member_indices = {entry[0] for entries in merged_groups.values() for entry in entries}

        def draw_target_row(parent, index, item, label):
            row = parent.row(align=True)
            row.prop(item, "target_object", text=label)
            pick = row.operator("ssmt.custom_material_assign_pick_target", text="", icon="EYEDROPPER")
            pick.node_name = self.name
            pick.item_index = index
            remove_row = row.row(align=True)
            remove_row.enabled = len(self.target_items) > 1
            remove = remove_row.operator("ssmt.custom_material_assign_remove_target", text="", icon="X")
            remove.node_name = self.name
            remove.item_index = index
            target = item.target_object
            if target is None:
                parent.label(text="未指定（可拖入大纲物体，或使用吸管）", icon="INFO")
            elif target.type != "MESH":
                parent.label(text="仅支持网格物体", icon="ERROR")
            else:
                parent.label(text=f"物体: {target.name}", icon="MESH_DATA")

        def draw_switch_controls(parent, group, group_index=1):
            header = parent.row(align=True)
            header.prop(
                group,
                "enabled",
                text="",
                icon="CHECKBOX_HLT" if group.enabled else "CHECKBOX_DEHLT",
            )
            header.label(text=f"贴图切换 {group_index}（{group.state_count} 档）", icon="KEYFRAME_HLT")
            parent.prop(group, "switch_variable", text="材质切换变量")
            parent.prop(group, "comment", text="备注")
            parent.prop(group, "key", text="切换按键")
            self._draw_switch_preview_row(parent, group)

        for index, item in enumerate(self.target_items):
            if index not in primary_indices:
                continue
            # 共享组主窗口：多个部件框在同一窗口内，控件只保留一套。
            matching = [entries for entries in merged_groups.values() if entries[0][0] == index]
            if matching:
                for entries in matching:
                    first_group = entries[0][2]
                    group_box = rows.box()
                    # 组头：明确写出「这一组有几个部件共用同一个切换变量」。
                    # 以前这里只写「部件指定组（N 档）」，成员行又和独立部件长得一样，
                    # 滚到中段就完全看不出它们是一组（数据其实早已合并成同一个 $swapkey）。
                    group_box.label(
                        text=f"贴图切换组（{len(entries)} 个部件共用同一个切换）",
                        icon="LINKED",
                    )
                    draw_switch_controls(group_box, first_group)
                    members_box = group_box.box()
                    members_box.label(
                        text=f"组内共用此切换的部件（{len(entries)} 个）",
                        icon="GROUP",
                    )
                    for member_index, member_item, _group in entries:
                        draw_target_row(members_box, member_index, member_item, f"部件 {member_index + 1}")
                continue
            box = rows.box()
            draw_target_row(box, index, item, f"部件 {index + 1}")
            for group_index, group in enumerate(item.switch_groups, 1):
                draw_switch_controls(box, group, group_index)

        # 未指定或未形成共享组的条目仍保持原有独立窗口。
        for index, item in enumerate(self.target_items):
            if index in merged_member_indices:
                continue
            box = rows.box()
            draw_target_row(box, index, item, f"部件 {index + 1}")
            for group_index, group in enumerate(item.switch_groups, 1):
                draw_switch_controls(box, group, group_index)

    def _draw_global_switch_panel(self, context, layout):
        """全局模式：显示全局贴图切换框。"""
        box = layout.box()
        header = box.row(align=True)
        header.label(
            text=f"贴图切换框（{len(self.global_switch_groups)} 个切换组）",
            icon="KEYFRAME_HLT",
        )
        if not self.global_switch_groups:
            box.label(
                text="尚未扫描：在全局模式下点击上方「全局扫描贴图切换」",
                icon="INFO",
            )

        grouped = OrderedDict()
        for group in self.global_switch_groups:
            variable = str(getattr(group, "merge_group_id", "") or "").strip() or f"__single_{getattr(group, 'object_name', '')}"
            if variable:
                grouped.setdefault(variable, []).append(group)
        for variable, entries in grouped.items():
            group_box = box.box()
            first_group = entries[0]
            if len(entries) > 1:
                group_box.label(text=f"共享切换组（{first_group.state_count} 档）", icon="LINKED")
            else:
                group_box.label(text=f"贴图切换（{first_group.state_count} 档）", icon="KEYFRAME_HLT")
            parts_box = group_box.column(align=True)
            for group in entries:
                row = parts_box.row(align=True)
                row.label(text=str(getattr(group, "object_name", "") or "(未命名部件)"), icon="OBJECT_DATA")
            header = group_box.row(align=True)
            header.prop(first_group, "enabled", text="", icon="CHECKBOX_HLT" if first_group.enabled else "CHECKBOX_DEHLT")
            header.label(text=f"贴图切换 1（{first_group.state_count} 档）", icon="KEYFRAME_HLT")
            group_box.prop(first_group, "switch_variable", text="材质切换变量")
            group_box.prop(first_group, "comment", text="备注")
            group_box.prop(first_group, "key", text="切换按键")
            self._draw_switch_preview_row(group_box, first_group)

    def draw_buttons(self, context, layout):
        layout.prop(self, "use_global_assign", text="使用全局指定")
        if self.use_global_assign:
            layout.label(
                text="已开启：将忽略下方目标列表，按材质转资源逻辑处理所有部件",
                icon="INFO",
            )
        else:
            layout.label(
                text="仅处理下方列表中指定的部件",
                icon="INFO",
            )

        scan_row = layout.row(align=True)
        scan = scan_row.operator(
            "ssmt.custom_material_scan_switches",
            text="全局扫描贴图切换",
            icon="FILE_REFRESH",
        )
        scan.node_name = self.name
        clear = scan_row.operator(
            "ssmt.custom_material_clear_switches",
            text="清除",
            icon="X",
        )
        clear.node_name = self.name
        if _has_active_preview(self):
            restore_all = scan_row.operator(
                "ssmt.custom_material_preview_restore_all",
                text="退出预览",
                icon="LOOP_BACK",
            )
            restore_all.node_name = self.name
        layout.prop(self, "preview_material_shading")

        if self.use_global_assign:
            self._draw_global_switch_panel(context, layout)
        else:
            self._draw_target_input_panel(context, layout)

        options = layout.box()
        options.label(text="材质转资源选项", icon="MATERIAL")
        options.prop(self, "material_to_resource_override")
        options.prop(self, "restore_default_textures_after_draw")
        if not self.use_global_assign:
            options.prop(self, "clear_non_target_textures")
        options.prop(self, "debug_disable_fx_ttl")
        options.prop(self, "export_textures_as_dds")

        layout.label(
            text="材质命名前缀沿用材质转资源规则（如 DiffuseMap_xxx）",
            icon="INFO",
        )
        if self.use_global_assign:
            layout.label(
                text="全局指定已开启：未处理单独目标移动/默认恢复逻辑",
                icon="INFO",
            )
        else:
            layout.label(
                text="顶层指定部件会自动移到本段末尾绘制，避免污染后续部件",
                icon="SORT_ASC",
            )
            layout.label(
                text="导出 Mod 时自动执行；未指定部件对应的 INI 段不会被修改",
                icon="TIME",
            )
        self.draw_material_detection_panel(context, layout)

    # ------------------------------------------------------------------ #
    # 清除未指定部件的游戏贴图（勾选 clear_non_target_textures 时启用）
    # ------------------------------------------------------------------ #

    def _mesh_name_verdict(self, mesh_name, mesh_verdict_cache, target_ibs=None):
        """``[mesh:...]`` 名 -> ``target`` / ``non_target`` / ``unknown``。

        精确名找得到物体时按目标列表判定，和原来一致。**找不到物体时**改用
        IB 键回退：导出 ini 的 mesh 注释常只有 IB 键（``LOD0.6ef3d666-888-0``），
        而物体名后面带部件名（``LOD0.6ef3d666-888-0.武器翅膀花纹贴片``），精确
        查找必然落空——若不做回退，整段都会被当成「归属不明」保护起来，未指定
        部件的贴图永远清不掉。

        * IB 键在目标集合里 -> ``target``：同一个 IB 可能还有别的目标部件
          （名字没在这条注释里出现），保守按目标保留。
        * IB 键存在但不在集合里 -> ``non_target``：本段确实没有目标部件。
        * 连 IB 键都提取不到 -> ``unknown``：保持原来的保守语义，原样保留。

        ``target_ibs`` 可由调用方预算一次传进来（``_clear_non_target_textures``
        会传）；不传时内部用 ``_target_ib_set()`` 现算，方便测试直接调用。
        """
        verdict = mesh_verdict_cache.get(mesh_name)
        if verdict is None:
            obj = self.find_object_by_mesh_name(mesh_name, object_filter=None)
            if obj is not None:
                if self._is_custom_target(obj):
                    verdict = "target"
                else:
                    verdict = "non_target"
            else:
                if target_ibs is None:
                    target_ibs = self._target_ib_set()
                ib_key = self._ib_key_from_name(mesh_name)
                if ib_key is None:
                    verdict = "unknown"
                elif ib_key in target_ibs:
                    verdict = "target"
                else:
                    verdict = "non_target"
            mesh_verdict_cache[mesh_name] = verdict
        return verdict

    def _target_has_readable_material(self, obj, probe_cache):
        """目标部件是否读得到材质（没材质/材质节点不对都算读不到）。

        判据与材质转资源生成绑定用的是同一套 ``find_matching_materials``：
        它读不到材质时就不会给该部件写资源引用，该部件随后会继承同段里别的
        绑定——所以这里必须把这类部件按非目标处理，否则贴图是错的。
        """
        key = str(getattr(obj, "name", "") or "")
        cached = probe_cache.get(key)
        if cached is not None:
            return cached
        readable = False
        for texture_type in _MATERIAL_PROBE_TYPES:
            try:
                if self.find_matching_materials(obj, texture_type):
                    readable = True
                    break
            except Exception:  # noqa: BLE001 - 材质异常一律按“读不到”处理
                continue
        probe_cache[key] = readable
        return readable

    @staticmethod
    def _collect_mesh_binding_lines(lines):
        """按「绘制点」把贴图绑定行归给网格。

        返回 ``[(网格名, {参数: 行号}, 绘制行号)]``。3DMigoto 里槽位赋值对
        之后的绘制持续生效，所以一个网格实际用到的绑定 = 它这条
        ``drawindexed`` 之前最后一次设置的那批值。基础导出把绑定写在
        ``; [mesh:...]`` 之前，材质转资源重建后写在注释之后，按绘制点取可以让
        两种排布都归到正确的网格。
        """
        positions = []
        for index, line in enumerate(lines):
            mesh_name = _MESH_COMMENT_RE.search(str(line))
            if mesh_name:
                positions.append((index, mesh_name.group(1)))
        if not positions:
            return []

        collected = []
        latest = {}
        cursor = 0
        for order, (pos, name) in enumerate(positions):
            next_pos = (
                positions[order + 1][0] if order + 1 < len(positions) else len(lines)
            )
            draw_pos = next_pos
            for index in range(pos, next_pos):
                stripped = str(lines[index]).strip().lower()
                if stripped.startswith("drawindexed") or stripped.startswith("draw "):
                    draw_pos = index
                    break
            for index in range(cursor, draw_pos):
                match = _TEXTURE_BINDING_RE.match(str(lines[index]))
                if match:
                    latest[match.group("param")] = index
            collected.append((name, dict(latest), draw_pos))
            cursor = draw_pos
        return collected

    @staticmethod
    def _collect_filename_references(ini_files):
        """收集所有 INI 中 ``filename = ...`` 指向的相对路径（小写正斜杠）。"""
        referenced = set()
        for ini_file in ini_files:
            try:
                with open(ini_file, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except OSError:
                continue
            for line in content.splitlines():
                match = _FILENAME_LINE_RE.match(line)
                if not match:
                    continue
                value = match.group("value").strip().replace("\\", "/")
                if value:
                    referenced.add(value.lower())
        return referenced

    @staticmethod
    def _resolve_mod_relative_path(mod_export_path, relative):
        """把 mod 根目录下的相对路径解析为绝对路径；越界返回 ``None``。"""
        root_abs = os.path.realpath(os.path.abspath(mod_export_path))
        candidate = os.path.realpath(
            os.path.abspath(os.path.join(root_abs, str(relative).replace("\\", os.sep).replace("/", os.sep)))
        )
        try:
            if os.path.commonpath((os.path.normcase(root_abs), os.path.normcase(candidate))) != os.path.normcase(root_abs):
                return None
        except ValueError:
            return None
        return candidate

    @staticmethod
    def _resource_still_referenced(sections, resource_name):
        """段集合中是否还有对 ``resource_name`` 的引用（段头行不算引用）。"""
        pattern = re.compile(
            r"(?<![\w\-.])" + re.escape(resource_name) + r"(?![\w\-])"
        )
        for lines in sections.values():
            for line in lines:
                if pattern.search(str(line)):
                    return True
        return False

    def _clear_non_target_textures(self, mod_export_path):
        """让目标部件列表之外的其他部件回退游戏原生贴图。

        做法是**利用同 hash 段内的绘制顺序**：依次把绑定行按 ``; [mesh:...]``
        注释与绘制点归到具体部件；只要某个网格排在段内所有 MOD 覆盖（也就是
        目标部件/归属不明部件的绘制点）**之前**，它绘制时槽位里还是游戏自己
        绑定的纹理，此时把它的 ``Resource\\ZZMI\\Xxx = ref ...`` 置成 ``= null``
        （ZZMI 的 SlotFix 用 ``unless_null``，源为 null 就不会覆盖槽位），
        它便停留在游戏原生贴图上。

        **绝不写 ``ps-tN = null``**：那是显式解绑，会让该部件直接变纯黑
        （2026-10-06 实测）。按参数类型分流（``_is_slot_style_param``）：
        ``Resource\\<ns>\\Xxx`` 这类别名置 ``= null``；``ps-tN`` / ``this``
        这类直接指向槽位的绑定**整行删除**（不写赋值 = 游戏原生贴图还在槽位里）。
        排在 MOD 覆盖之后的非目标网格不做处理并计数上报，因为那时槽位已被前面
        的块占住，回退同样拿不到原生贴图。

        随后回收因此失去全部引用的 ``[Resource-...]`` / ``[Resource_...]``
        定义段，最后删除这些定义段指向的、不再被任何 INI 引用的贴图文件。
        段内没有 ``; [mesh:...]`` 注释时无从判定归属，整段保持原样。

        另外，**目标部件列表里读不到材质的部件一律视作非目标**：没设材质或材质
        节点配置不正确时，材质转资源不会给它写资源引用，它只会继承同段别的
        绑定而显示错误贴图。这类部件同样会被置空回退原生，并在结束时逐个点名
        提示，方便回去补材质。
        """
        target_names = self._target_object_set()
        if not target_names:
            return 0
        # 名字查不到物体时的 IB 回退集合（每个文件/段共用一份，避免重复计算）。
        target_ibs = self._target_ib_set()

        ini_files = sorted(glob.glob(os.path.join(mod_export_path, "*.ini")))
        if not ini_files:
            return 0

        removed_bindings = 0
        removed_slot_lines = 0
        removed_sections = 0
        skipped_sections = 0
        skipped_meshes = 0
        no_material_meshes = []
        freed_filenames = set()
        mesh_verdict_cache = {}
        material_probe_cache = {}

        for ini_file in ini_files:
            try:
                with open(ini_file, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except OSError as exc:
                print(f"[材质转资源pro] 清除未指定部件贴图失败 {ini_file}: {exc}")
                continue

            # 与材质转资源保持同一口径：驱动块与自动追加尾段整体剥出后原样拼回。
            preserved_driver_content, content = self.split_anim_driver_block_content(content)
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)

            preamble_lines, sections = self._parse_ini_content(content)
            if not sections:
                continue

            freed_resources = set()
            changed = False
            for section_name in list(sections.keys()):
                if not str(section_name).startswith("[TextureOverride_"):
                    continue
                lines = sections[section_name]
                mesh_bindings = self._collect_mesh_binding_lines(lines)
                if not mesh_bindings:
                    if any(_TEXTURE_BINDING_RE.match(str(line)) for line in lines):
                        skipped_sections += 1
                    continue

                protected_lines = set()
                mesh_verdicts = []
                boundary = None
                for mesh_name, bindings, draw_pos in mesh_bindings:
                    verdict = self._mesh_name_verdict(
                        mesh_name, mesh_verdict_cache, target_ibs
                    )
                    if verdict == "target":
                        target_obj = self.find_object_by_mesh_name(mesh_name, object_filter=None)
                        if target_obj is not None and not self._target_has_readable_material(
                            target_obj, material_probe_cache
                        ):
                            # 目标部件但读不到材质：材质转资源根本不会给它写引用，
                            # 它只会继承同段里别的绑定（贴图是错的）。按非目标处理。
                            verdict = "missing_material"
                            if mesh_name not in no_material_meshes:
                                no_material_meshes.append(mesh_name)
                    mesh_verdicts.append((verdict, bindings, draw_pos))
                    if verdict not in ("non_target", "missing_material"):
                        # target：目标部件正在使用；unknown：名字对不上任何导出
                        # 物体、也提取不到 IB 键，归属不明。两者都必须原样保留，
                        # 并构成 MOD 覆盖的边界——只有排在它们之前的绘制才拿得到
                        # 游戏原生贴图。
                        protected_lines.update(bindings.values())
                        if boundary is None or draw_pos < boundary:
                            boundary = draw_pos

                foreign_bindings = {}
                for verdict, bindings, draw_pos in mesh_verdicts:
                    if verdict not in ("non_target", "missing_material"):
                        continue
                    if boundary is not None and draw_pos > boundary:
                        # 排在 MOD 覆盖之后，槽位已被前面的块占住，置 null 也
                        # 只是「不覆盖」，拿不到原生贴图。保持原样并上报。
                        skipped_meshes += 1
                        continue
                    for param, index in bindings.items():
                        foreign_bindings.setdefault(index, param)

                # 孤儿绑定行：被后续赋值覆盖、任何绘制点都用不到（典型是基础
                # 导出给第一个子网格写、而该段每个网格又都有自己的绑定时的那
                # 一份段级默认值）。置空它们不影响任何绘制，却能解除对
                # Resource 定义段的引用，让贴图真正可回收。
                # 例外：``if`` … ``endif`` 条件块内的赋值按运行期条件择一
                # （材质切换组同一参数在多个分支各写一条），不是孤儿，必须原样
                # 保留 —— 否则除末档以外的档位会被整批置成 ``= null``。
                used_indexes = {
                    index for _, bindings, _ in mesh_verdicts for index in bindings.values()
                }
                conditional_indexes = _collect_conditional_block_indexes(lines)
                for index, line in enumerate(lines):
                    if index in used_indexes or index in conditional_indexes:
                        continue
                    match = _TEXTURE_BINDING_RE.match(str(line))
                    if match:
                        foreign_bindings.setdefault(index, match.group("param"))

                if not foreign_bindings:
                    continue

                section_changed = False
                new_lines = []
                for index, line in enumerate(lines):
                    if index in foreign_bindings and index not in protected_lines:
                        match = _TEXTURE_BINDING_RE.match(str(line))
                        if match:
                            freed_resources.add(match.group("resource"))
                            removed_bindings += 1
                            if _is_slot_style_param(match.group("param")):
                                # ps-tN / this 直接指向游戏槽位：写 ``= null`` 是
                                # 显式解绑（部件纯黑），只能整行删除——不写赋值，
                                # 游戏自己绑定的那张纹理就还在槽位里。
                                removed_slot_lines += 1
                                section_changed = True
                                continue
                            new_lines.append(f"{foreign_bindings[index]} = null")
                            section_changed = True
                            continue
                    new_lines.append(line)
                if section_changed:
                    sections[section_name] = new_lines
                    changed = True

            if not changed:
                continue

            remaining_tokens = set()
            for lines in sections.values():
                for line in lines:
                    match = _TEXTURE_BINDING_RE.match(str(line))
                    if match:
                        remaining_tokens.add(match.group("resource"))

            for section_name in list(sections.keys()):
                if not _RESOURCE_SECTION_RE.match(str(section_name)):
                    continue
                resource_name = str(section_name).strip().strip("[]").strip()
                if resource_name not in freed_resources:
                    continue
                if resource_name in remaining_tokens:
                    continue
                if self._resource_still_referenced(sections, resource_name):
                    continue
                for line in sections[section_name]:
                    file_match = _FILENAME_LINE_RE.match(str(line))
                    if file_match:
                        freed_filenames.add(file_match.group("value").strip().replace("\\", "/"))
                del sections[section_name]
                removed_sections += 1

            new_content = self._serialize_ini_content(
                preamble_lines,
                sections,
                preserved_tail_content=preserved_tail_content,
                preserved_driver_content=preserved_driver_content,
            )
            with open(ini_file, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(new_content)

        removed_files = 0
        if freed_filenames:
            still_referenced = self._collect_filename_references(ini_files)
            for relative in sorted(freed_filenames):
                if relative.lower() in still_referenced:
                    continue
                target_path = self._resolve_mod_relative_path(mod_export_path, relative)
                if target_path is None or not os.path.isfile(target_path):
                    continue
                try:
                    os.remove(target_path)
                    removed_files += 1
                except OSError as exc:
                    print(f"[材质转资源pro] 删除贴图失败 {target_path}: {exc}")

        if removed_bindings or removed_sections or removed_files:
            print(
                f"[材质转资源pro] 已清除未指定部件贴图：回退绑定行 {removed_bindings}"
                f"（删除槽位绑定 {removed_slot_lines} 行、别名置空 "
                f"{removed_bindings - removed_slot_lines} 行）、"
                f"资源定义段 {removed_sections}、贴图文件 {removed_files}"
            )
        if skipped_sections:
            print(
                f"[材质转资源pro] 有 {skipped_sections} 个段含无法归属的网格，已保持原样"
            )
        if skipped_meshes:
            print(
                f"[材质转资源pro] 有 {skipped_meshes} 个非目标网格排在 MOD 覆盖之后"
                f"（同 hash 段内），置 null 拿不到原生贴图，已保持原样"
            )
        if no_material_meshes:
            # 提示语只用 ASCII 标点：Windows 上 stdout 可能是 GBK，emoji/特殊符号
            # 会直接抛 UnicodeEncodeError 把导出打断。
            print(
                f"[材质转资源pro] 警告：以下 {len(no_material_meshes)} 个目标部件读不到材质"
                "（未设置材质或材质节点配置不正确），已按非目标部件处理"
                "并回退游戏原生贴图："
            )
            for mesh_name in no_material_meshes:
                print(f"    - {mesh_name}")
        return removed_bindings + removed_sections + removed_files

    def execute_postprocess(self, mod_export_path, exporter=None):
        if not bool(getattr(self, "use_global_assign", False)) and not self._target_object_set():
            print("[材质转资源pro] 未指定任何目标部件，跳过")
            return
        result = super().execute_postprocess(mod_export_path, exporter=exporter)
        if bool(getattr(self, "clear_non_target_textures", False)) and not bool(
            getattr(self, "use_global_assign", False)
        ):
            self._clear_non_target_textures(mod_export_path)
        return result


LEGACY_MATERIAL_NODE_ID = 'SSMTNode_PostProcess_Material'


def _migrate_legacy_material_nodes():
    """把旧版「材质转资源」节点原位迁移为「材质转资源pro」（幂等）。

    旧类仅保留为注册壳（见 node_postprocess_material），目的是让旧蓝图文件
    能够正常加载、避免未知节点类型被 Blender 静默丢弃；迁移在此之后执行：
    复制全部共享属性、切换为全局指定模式（等价原版全场景行为），并重连
    同名 socket 的连线。迁移完成的壳节点随即删除。
    """
    migrated = 0
    for tree in bpy.data.node_groups:
        if getattr(tree, 'bl_idname', '') != 'SSMTBlueprintTreeType':
            continue
        for node in list(tree.nodes):
            if getattr(node, 'bl_idname', '') != LEGACY_MATERIAL_NODE_ID:
                continue
            try:
                _migrate_legacy_material_node(tree, node)
                migrated += 1
            except Exception as exc:
                print(f"[TheHerta4] 材质转资源节点迁移失败 {node.name}: {exc}")
    if migrated:
        print(f"[TheHerta4] 材质转资源节点迁移完成：{migrated} 个节点已转换为「材质转资源pro」。")
    return migrated


def _migrate_legacy_material_node(tree, node):
    new_node = tree.nodes.new(NODE_IDNAME)
    new_node.location = node.location
    if node.label:
        new_node.label = node.label
    # 原版按场景全部物体处理，等价于 pro 的「使用全局指定」模式。
    new_node.use_global_assign = True
    # 复制共享属性（属性均声明在未注册的 SSMTNode_PostProcess_MaterialBase）。
    for attr in ('material_to_resource_override', 'debug_disable_fx_ttl',
                 'material_switch_var', 'temp_prefix_input',
                 'show_detect_panel', 'detect_all_ok'):
        try:
            setattr(new_node, attr, getattr(node, attr))
        except Exception:
            pass
    for item in node.material_detect_prefixes:
        target = new_node.material_detect_prefixes.add()
        target.prefix = item.prefix
    for item in node.detected_materials:
        target = new_node.detected_materials.add()
        target.object_name = item.object_name
        target.missing_prefix = item.missing_prefix
    # 重连输入/输出 socket（两者的 socket 布局一致：PostProcess Input/Output）。
    for index, output in enumerate(node.outputs):
        for link in list(output.links):
            tree.links.new(new_node.outputs[index], link.to_socket)
    for index, socket_input in enumerate(node.inputs):
        for link in list(socket_input.links):
            tree.links.new(link.from_socket, new_node.inputs[index])
    tree.nodes.remove(node)


def _migrate_legacy_material_nodes_handler(*args):
    # Blender 调用 load_post 处理器时会传入参数（本机 5.2 实测为 2 个），
    # 而 register() 里也手动调用它（0 个参数）。所以必须收 *args，
    # 否则 handler 一触发就 TypeError——而且这个错误发生在进入函数体之前，下面的
    # try/except 兜不住，会在控制台刷 "Error in bpy.app.handlers.load_post[N]"。
    try:
        _migrate_legacy_material_nodes()
    except Exception as exc:
        print(f"[TheHerta4] 材质转资源节点迁移失败: {exc}")


def _run_initial_migration():
    _migrate_legacy_material_nodes_handler()
    return None  # 返回 None => 定时器只执行一次


def _schedule_initial_migration():
    """覆盖「addon 启用时文件已经打开」的场景。

    插件注册发生在 bpy.data 就绪之前，此时它是 _RestrictData，任何集合访问都会抛
    AttributeError（旧代码因此每次都打印迁移失败）。所以推迟到主循环第一帧再跑；
    headless 下没有事件循环，但 bpy.data 此时已就绪，直接同步执行。
    """
    if hasattr(bpy.data, "materials"):
        _run_initial_migration()
        return
    try:
        bpy.app.timers.register(_run_initial_migration, first_interval=0.0)
    except Exception:
        pass


classes = (
    SSMT_CustomMaterialAssignSwitchGroup,
    SSMT_CustomMaterialAssignTargetItem,
    SSMT_OT_CustomMaterialDetectAddPrefix,
    SSMT_OT_CustomMaterialDetectRemovePrefix,
    SSMT_OT_CustomMaterialDetectAddCustomPrefix,
    SSMT_OT_CustomMaterialDetect,
    SSMT_OT_CustomMaterialDetectClear,
    SSMT_OT_CustomMaterialAssignAddTarget,
    SSMT_OT_CustomMaterialAssignClearTargets,
    SSMT_OT_CustomMaterialAssignAddSelected,
    SSMT_OT_CustomMaterialAssignScanObjects,
    SSMT_OT_CustomMaterialAssignRemoveTarget,
    SSMT_OT_CustomMaterialAssignPickTarget,
    SSMT_OT_CustomMaterialAssignPickTargetModal,
    SSMT_OT_CustomMaterialScanSwitches,
    SSMT_OT_CustomMaterialClearSwitches,
    SSMT_OT_CustomMaterialPreviewSwitch,
    SSMT_OT_CustomMaterialPreviewRestoreAll,
    SSMTNode_PostProcess_CustomMaterialAssign,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.VIEW3D_HT_header.append(_draw_picking_header)
    if _migrate_legacy_material_nodes_handler not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_migrate_legacy_material_nodes_handler)
    # 立即迁移一次：覆盖「addon 启用时文件已打开」的场景。
    _schedule_initial_migration()


def unregister():
    bpy.types.VIEW3D_HT_header.remove(_draw_picking_header)
    if _migrate_legacy_material_nodes_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_migrate_legacy_material_nodes_handler)
    try:
        bpy.app.timers.unregister(_run_initial_migration)
    except Exception:
        pass
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
