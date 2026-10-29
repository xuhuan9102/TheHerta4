import bpy
from contextvars import ContextVar
from functools import wraps

_material_lookup_cache = ContextVar('herta_material_lookup_cache', default=None)

def _material_lookup_scope(method):
    @wraps(method)
    def scoped(self, *args, **kwargs):
        token = _material_lookup_cache.set({})
        try:
            return method(self, *args, **kwargs)
        finally:
            _material_lookup_cache.reset(token)
    return scoped
import os
import glob
import hashlib
import re
from collections import OrderedDict
import shutil

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
try:  # 纯文件装载的测试宿主 / 裁剪环境可能没有 common 包：退化为旧「立即复制」路径
    from ..common.texture_export_plan import TextureExportPlan
except ImportError:  # pragma: no cover
    TextureExportPlan = None
from ..toolkit.tt_dds_conversion import convert_texture_to_dds_cached, is_dds_file
from . import fx_namespace
from .node_postprocess_base import SSMTNode_PostProcess_Base

_name_mapping_cache = {}
_reverse_name_mapping_cache = {}
# 资源缓存按“材质内容签名”去重，避免同一套贴图被重复复制/重复生成 Resource。
_material_resource_cache = {}
# 旧版把 TTL 写成段尾一整组，靠这两个标记在重导出时整组摘掉。现在是就地替换，
# 标记只剩兼容用途：老工程文件里残留的组仍由 _strip_ttl_groups 摘走。
_TTL_GROUP_MARKER = "; [SSMT-TTL] {}"
_TTL_GROUP_END = "; [/SSMT-TTL]"
# 旧版注释掉的原绘制行：块里没有别的绘制行时，这一行的数字就是唯一来源。
_TTL_DRAW_TAKEN_PREFIX = "; [SSMT TTL 已接管] "
_TTL_DRAW_TAKEN_RE = re.compile(r"^;\s*\[SSMT TTL 已接管\]", re.IGNORECASE)
# 就地替换后块里出现的调用行：见到它就知道这个块已经换过，重导出别再来一遍。
_TTL_CALL_LINE = "run = CommandList{}TTL{}Draw".format(chr(92), chr(92))
# TTL 合成亮度默认值（TTLib 的 $\TTL\V，即 HSV 的 Value）。
# 必须写在 if 条件块之外：TTLib 的 CommandListFrameReset 每帧把 $V 重置为 1.0，
# 只有绘制前重新赋值才生效；不需要提亮的段落手动删掉这一行即可。
_TTL_DEFAULT_BRIGHTNESS = "1.5"
# ── EFMI 半透明（EFMITTL）──────────────────────────────────────────────
# 末地 EFMI 没有 ZZMI/TTLib 那种「把这一笔改写成第二遍绘制」的 TTL；它的半透明由
# 一个共享库接管**这一次**绘制：调用方只交出场参数（索引数 / 起始索引，可选 alpha
# 与遮罩贴图），然后 run 库的命令列表，库按绘制通道（阴影 / 前向 / G-Buffer / 深度
# 预通道 / 描边）自己决定画、捕获还是跳过。
# 库自带启动声明：库文件是 Mods/EFMITTL/translucency.ini，命名空间 EFMITTL，
# 命令列表 CommandList\EFMITTL\Draw，公开的变量与资源别名就是下面这几条。
# 库在 Draw 结尾会把 alpha / mask_channel / mask_invert / Mask / count / first 全部
# 复位，所以本仓只负责调用前赋值，不写复位行。
_EFMITTL_KEY = "EFMITTL"
_EFMITTL_LIBRARY_FILE = os.path.join(_EFMITTL_KEY, "translucency.ini")
_EFMITTL_MASK_PARAM = "Resource\\EFMITTL\\Mask"
_EFMITTL_DRAW_LINE = "run = CommandList\\EFMITTL\\Draw"
# 遮罩默认读透明通道（3 = A）：库自己的默认是 0（R），我们不跟它，每次显式写 3。
# 提取工具的透明通道贴图四个通道同值，所以 3 读到的就是那张遮罩本身；方向与 ZZMI
# 的 TTL 一致：mask_invert = 1。
_EFMITTL_MASK_CHANNEL_LINE = "$\\EFMITTL\\mask_channel = 3"
_EFMITTL_MASK_INVERT_LINE = "$\\EFMITTL\\mask_invert = 1"
_EFMITTL_DRAW_RE = re.compile(
    r'^(?P<indent>\s*)drawindexed(?:instanced)?\s*=\s*(?P<args>[^;]+?)\s*$',
    re.IGNORECASE,
)
# 库用 ``drawindexed = $count, $first, 0`` 出图：只画一个实例。EFMI 把实例绘制拆成
# 「每次一个实例」的回调（$draw_call_instance_id），所以 mod 里的实例数就是这两者之一。
_EFMITTL_SINGLE_INSTANCE_TOKENS = {"1", "instance_count"}
# 拖拽物体显隐 flag 行（注入在绘制分支内；TTL 块重建必须原样保留，否则隐藏判定失效）
_DRAG_OBJVIS_LINE_RE = re.compile(r'^\s*\$ssmtdrag_objvis_[\w]*\s*=\s*1\s*$')

# 各 FX 命名空间的资源前缀（小写，用于识别 ini 里已有的发光/裁切绑定）。
# 识别一律按「所有已知命名空间」做，而不是只看当前逻辑：作者可能在自己的配置表里
# 手写另一套命名空间的绑定，材质转资源应该照样把它指向新复制出来的贴图资源。
_FX_RESOURCE_PREFIXES = tuple(
    profile.resource_prefix for profile in fx_namespace.ALL_PROFILES
)
_FX_PROFILE_BY_PREFIX = {
    profile.resource_prefix.lower(): profile for profile in fx_namespace.ALL_PROFILES
}
# 绘制后复位块的引用行 / 亮度参数行（跨命名空间识别，避免透明度段重建时把复位行
# 当成用户内容搬走）。
_FX_RESET_REF_LINES = tuple(
    f"{profile.glow_ref} = ref null" for profile in fx_namespace.ALL_PROFILES
) + tuple(
    f"{profile.fxmap_ref} = ref null" for profile in fx_namespace.ALL_PROFILES
) + tuple(
    # TTLMap 只有声明了它的命名空间才有（HI3FX）；空字符串要跳过，否则会多出一条
    # 谁也匹配不上的 " = ref null"。
    f"{profile.ttlmap_ref} = ref null"
    for profile in fx_namespace.ALL_PROFILES
    if profile.ttlmap_ref
)
_FX_RESET_PARAM_LINES = tuple(
    f"{profile.param('brightness')} = 0" for profile in fx_namespace.ALL_PROFILES
)
_FX_RUN_LINES = tuple(profile.run_line for profile in fx_namespace.ALL_PROFILES)

#: 同一次导出里多个切换组填同一个按键时的顺延候选。
#: 实机现象：衣服（$swapkey150）与身体（$swapkey151）都填 ``N``，按一次 N 两个组
#: 同时 cycle，衣服和身体一起变、档位互相错位。导出期把后写入的组顺延到这里。
KEYSWAP_FALLBACK_KEYS = ("M", "B", "V", "K", "L", "J", "H", "G", "T", "Y")


def used_keyswap_keys(sections, skip_section=""):
    """配置表里已被其它 KeySwap 段占用的按键（小写，不含 ``skip_section`` 自己）。"""
    used = set()
    for name, lines in (sections or {}).items():
        if skip_section and str(name) == str(skip_section):
            continue
        if not str(name).startswith("[KeySwap"):
            continue
        for line in lines or ():
            match = re.match(r"^\s*key\s*=\s*(.+?)\s*$", str(line), re.IGNORECASE)
            if match:
                used.add(match.group(1).strip().casefold())
                break
    return used


def resolve_keyswap_key(sections, section_name, requested_key, log_prefix=""):
    """多个切换组共用同一个键时顺延到候选键（不改用户节点里的按键配置）。"""
    key = str(requested_key or "").strip()
    if not key:
        return key
    used = used_keyswap_keys(sections, skip_section=section_name)
    if key.casefold() not in used:
        return key
    for candidate in KEYSWAP_FALLBACK_KEYS:
        if candidate.casefold() not in used:
            print(
                f"[{log_prefix or 'KeySwap'}] 切换按键冲突：{section_name} 的 key={key} "
                f"已被同表其它切换组占用，本次导出改用 {candidate}"
            )
            return candidate
    return key


def _fx_profile_for_param(param_name):
    """参数名属于哪个 FX 命名空间；不属于任何一套时返回 None。"""
    lowered = str(param_name or "").lower()
    for prefix, profile in _FX_PROFILE_BY_PREFIX.items():
        if lowered.startswith(prefix):
            return profile
    return None


def _starts_with_fx_resource_prefix(param_name):
    return _fx_profile_for_param(param_name) is not None


def _load_gimi_orfix_adapter():
    """加载原神 ORFix 适配模块；模块缺失时返回 None，不影响其它游戏导出。"""
    try:
        from . import gimi_orfix_adapter
    except ImportError:
        return None
    return gimi_orfix_adapter


def clear_name_mapping_cache():
    global _name_mapping_cache, _reverse_name_mapping_cache, _material_resource_cache
    _name_mapping_cache.clear()
    _reverse_name_mapping_cache.clear()
    _material_resource_cache.clear()


class MaterialPrefixItem(bpy.types.PropertyGroup):
    prefix: bpy.props.StringProperty(
        name="前缀",
        description="材质名称前缀，用于筛选检测的材质",
        default=""
    )


class DetectedMaterialItem(bpy.types.PropertyGroup):
    object_name: bpy.props.StringProperty(name="物体名称", default="")
    missing_prefix: bpy.props.StringProperty(name="缺失前缀", default="")


MATERIAL_DETECT_PRESETS = [
    "DiffuseMap",
    "NormalMap",
    "LightMap",
    "MaterialMap",
    "RampMap",
    "HighLightMap",
    "WengineFX",
    "WengineFx",
    "ZglowMap",
]


class SSMT_OT_MaterialDetectAddPrefix(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_add_prefix"
    bl_label = "添加前缀"
    bl_description = "按预设顺序添加下一个材质检测前缀"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
            return {'CANCELLED'}

        existing = {item.prefix for item in node.material_detect_prefixes}
        for preset in MATERIAL_DETECT_PRESETS:
            if preset not in existing:
                new_item = node.material_detect_prefixes.add()
                new_item.prefix = preset
                return {'FINISHED'}

        self.report({'WARNING'}, "所有预设前缀已添加")
        return {'CANCELLED'}


class SSMT_OT_MaterialDetectRemovePrefix(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_remove_prefix"
    bl_label = "移除前缀"
    bl_description = "移除指定索引的材质检测前缀"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()
    item_index: bpy.props.IntProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == 'SSMTNode_PostProcess_Material':
            if 0 <= self.item_index < len(node.material_detect_prefixes):
                node.material_detect_prefixes.remove(self.item_index)
        return {'FINISHED'}


class SSMT_OT_MaterialDetectAddCustomPrefix(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_add_custom_prefix"
    bl_label = "添加自定义前缀"
    bl_description = "添加手动输入的材质检测前缀"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
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


class SSMT_OT_MaterialDetect(bpy.types.Operator):
    bl_idname = "ssmt.material_detect"
    bl_label = "Material Detect"
    bl_description = "Detect missing prefixed materials from connected objects, including nested blueprints"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    @staticmethod
    def _is_result_output_node(node) -> bool:
        return getattr(node, "bl_idname", "") in {
            "SSMTNode_Result_Output",
            "SSMTNode_Result_Output_NTMIModImp", "SSMTNode_VeloExportBridge",
        }

    def _find_result_output(self, node, visited=None):
        if visited is None:
            visited = set()
        tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
        node_key = f"{tree_name}::{node.name}"
        if node_key in visited:
            return None
        visited.add(node_key)

        if self._is_result_output_node(node):
            return node

        for input_socket in node.inputs:
            if input_socket.bl_idname == 'SSMTSocketPostProcess' and input_socket.is_linked:
                for link in input_socket.links:
                    result = self._find_result_output(link.from_node, visited)
                    if result:
                        return result
        return None

    def _collect_object_info_nodes(self, node, visited=None, visited_trees=None):
        if visited is None:
            visited = set()
        if visited_trees is None:
            visited_trees = set()

        tree_name = node.id_data.name if hasattr(node, 'id_data') and node.id_data else ""
        node_key = f"{tree_name}::{node.name}"
        if node_key in visited:
            return []
        visited.add(node_key)

        detect_nodes = []
        if node.bl_idname in {'SSMTNode_Object_Info', 'SSMTNode_Object_Info_Pro', 'SSMTNode_MultiFile_Export'}:
            detect_nodes.append(node)

        if node.bl_idname == 'SSMTNode_Blueprint_Nest':
            nested_tree_name = str(getattr(node, 'blueprint_name', '') or '').strip()
            if nested_tree_name and nested_tree_name != 'NONE' and nested_tree_name not in visited_trees:
                nested_tree = bpy.data.node_groups.get(nested_tree_name)
                if nested_tree and getattr(nested_tree, 'bl_idname', '') == 'SSMTBlueprintTreeType':
                    visited_trees.add(nested_tree_name)
                    for nested_node in nested_tree.nodes:
                        if self._is_result_output_node(nested_node):
                            detect_nodes.extend(
                                self._collect_object_info_nodes(
                                    nested_node,
                                    visited=visited,
                                    visited_trees=visited_trees,
                                )
                            )

        for input_socket in node.inputs:
            if input_socket.is_linked:
                for link in input_socket.links:
                    detect_nodes.extend(
                        self._collect_object_info_nodes(
                            link.from_node,
                            visited=visited,
                            visited_trees=visited_trees,
                        )
                    )
        return detect_nodes

    def _iter_detect_object_names(self, detect_nodes):
        for detect_node in detect_nodes:
            node_type = getattr(detect_node, 'bl_idname', '')
            if node_type == 'SSMTNode_Object_Info':
                obj_name = getattr(detect_node, 'object_name', '')
                if obj_name:
                    yield obj_name
            elif node_type == 'SSMTNode_Object_Info_Pro':
                for item in getattr(detect_node, 'object_list', []):
                    obj_name = getattr(item, 'object_name', '')
                    if obj_name:
                        yield obj_name
            elif node_type == 'SSMTNode_MultiFile_Export':
                for item in getattr(detect_node, 'object_list', []):
                    obj_name = getattr(item, 'object_name', '')
                    if obj_name:
                        yield obj_name

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            self.report({'WARNING'}, 'No active blueprint tree')
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name)
        if not node or node.bl_idname != 'SSMTNode_PostProcess_Material':
            return {'CANCELLED'}

        prefixes = [item.prefix.strip() for item in node.material_detect_prefixes if item.prefix.strip()]
        if not prefixes:
            self.report({'WARNING'}, 'Add at least one material prefix first')
            return {'CANCELLED'}

        result_output = self._find_result_output(node)
        if not result_output:
            self.report({'WARNING'}, 'No connected Result_Output node found')
            return {'CANCELLED'}

        detect_nodes = self._collect_object_info_nodes(result_output)
        if not detect_nodes:
            self.report({'WARNING'}, 'No connected object nodes found')
            return {'CANCELLED'}

        node.detected_materials.clear()

        unique_object_names = []
        seen_object_names = set()
        for obj_name in self._iter_detect_object_names(detect_nodes):
            if obj_name in seen_object_names:
                continue
            seen_object_names.add(obj_name)
            unique_object_names.append(obj_name)

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


class SSMT_OT_MaterialDetectClear(bpy.types.Operator):
    bl_idname = "ssmt.material_detect_clear"
    bl_label = "清除结果"
    bl_description = "清除检测结果"
    bl_options = {'INTERNAL'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = getattr(context.space_data, "edit_tree", None) or getattr(context.space_data, "node_tree", None)
        if not tree:
            return {'CANCELLED'}
        node = tree.nodes.get(self.node_name)
        if node and node.bl_idname == 'SSMTNode_PostProcess_Material':
            node.detected_materials.clear()
            node.detect_all_ok = False
        return {'FINISHED'}


class SSMTNode_PostProcess_MaterialBase(SSMTNode_PostProcess_Base):
    """材质转资源节点的共享实现基类（未注册的纯 Python 基类）。

    注意：Blender 不允许注册「继承自已注册自定义节点」的子类——注册子类会
    破坏父节点类型（复现：注册 SSMTNode_PostProcess_CustomMaterialAssign 后，
    SSMTNode_PostProcess_Material 无法再创建，并报 unable to get Python class
    for RNA struct 警告）。因此具体节点类都继承本基类、各自独立注册。
    本基类由「材质转资源pro」（完整功能）与「材质转资源」弃用壳（旧文件兼容）
    共用；原版功能已由 pro 完全覆盖（逐方法对比见 docs/PR7审查修复记录.md）。
    """
    TRANSPARENCY_SECTION_MARKER = ";MARK:CustomShaderTransparency----------------------------------------------------------"

    @staticmethod
    def _parse_ini_content(content):
        preamble_lines = []
        sections = OrderedDict()
        current_section = None
        for line in str(content or "").splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                current_section = stripped
                sections.setdefault(current_section, [])
            elif current_section is None:
                preamble_lines.append(line)
            elif stripped:
                sections[current_section].append(line)
        return preamble_lines, sections

    @classmethod
    def _serialize_ini_content(
        cls,
        preamble_lines,
        sections,
        transparency_sections=None,
        preserved_tail_content="",
        preserved_driver_content="",
    ):
        new_content = []
        if preserved_driver_content:
            new_content.append(preserved_driver_content.rstrip())
            new_content.append('')
        new_content.extend(preamble_lines or [])
        if new_content and sections and new_content[-1].strip():
            new_content.append('')

        for section_name, lines in sections.items():
            new_content.append(section_name)
            new_content.extend(lines)
            new_content.append('')

        if transparency_sections:
            new_content.append(f'\n{cls.TRANSPARENCY_SECTION_MARKER}')
            for shader_name, lines in transparency_sections.items():
                new_content.append(f"[{shader_name}]")
                new_content.extend(lines)
                new_content.append('')

        if preserved_tail_content:
            new_content.append('')
            new_content.append(preserved_tail_content)

        return "\n".join(new_content)

    @classmethod
    def _strip_previous_transparency_sections(cls, content):
        content = str(content or "")
        marker_index = content.find(cls.TRANSPARENCY_SECTION_MARKER)
        if marker_index < 0:
            return content

        preserved_tail_index = None
        for marker in cls.AUTO_APPENDED_SECTION_MARKERS:
            candidate_index = content.find(marker, marker_index + len(cls.TRANSPARENCY_SECTION_MARKER))
            if candidate_index < 0:
                continue
            if preserved_tail_index is None or candidate_index < preserved_tail_index:
                preserved_tail_index = candidate_index

        generated_tail_end = preserved_tail_index if preserved_tail_index is not None else len(content)
        generated_tail = content[
            marker_index + len(cls.TRANSPARENCY_SECTION_MARKER):generated_tail_end
        ]
        transparency_bodies = {}
        current_section_name = ""
        current_section_lines = []

        def store_current_section():
            if not current_section_name.startswith("CustomShaderTransparencyCloth"):
                return
            body_start = next(
                (
                    index + 1
                    for index, line in enumerate(current_section_lines)
                    if line.strip() == "; --- Start of Overridden Mesh Content ---"
                ),
                None,
            )
            if body_start is not None:
                transparency_bodies[current_section_name.casefold()] = current_section_lines[body_start:]

        for line in generated_tail.splitlines():
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']'):
                store_current_section()
                current_section_name = stripped[1:-1].strip()
                current_section_lines = []
            elif current_section_name:
                current_section_lines.append(line)
        store_current_section()

        restored_prefix_lines = []
        for line in content[:marker_index].rstrip().splitlines():
            run_match = re.match(r'^\s*run\s*=\s*([^;]+?)\s*$', line, re.IGNORECASE)
            run_target = run_match.group(1).strip().casefold() if run_match else ""
            body_lines = transparency_bodies.get(run_target)
            if body_lines is None:
                restored_prefix_lines.append(line)
            else:
                restored_prefix_lines.extend(body_lines)

        prefix = "\n".join(restored_prefix_lines).rstrip()
        if preserved_tail_index is None:
            return prefix
        return prefix + "\n\n" + content[preserved_tail_index:]

    @staticmethod
    def clear_cache():
        clear_name_mapping_cache()

    material_to_resource_override: bpy.props.BoolProperty(
        name="覆盖现有资源",
        description=(
            "给之前导出过旧版本 Mod 的情况用：允许本次导出的贴图覆盖 Mod 目录里上次留下的同名文件。"
            "开启「资源合并 → 落盘前联合去重」时按内容智能判定：与旧文件内容重复"
            "（MD5 / 像素级 / 相似度任一命中）就不覆盖、也不重新转 DDS；内容不一样才覆盖。"
            "未开启联合去重时保持旧行为：无条件覆盖并强制重新转换"
        ),
        default=False
    )
    debug_disable_fx_ttl: bpy.props.BoolProperty(
        name="禁用 FX/TTL (调试)",
        description="调试用：开启后暂时不生成 FX（FXMap→RabbitFX/NTEMIFX）与 TTL（TTLMap）段落",
        default=False
    )
    material_switch_var: bpy.props.StringProperty(
        name="材质切换变量",
        description="用于材质切换的起始变量名(会自动递增)",
        default="$swapkey150",
        update=lambda self, context: self.update_node_width([self.material_switch_var])
    )
    material_detect_prefixes: bpy.props.CollectionProperty(type=MaterialPrefixItem)
    temp_prefix_input: bpy.props.StringProperty(name="自定义前缀", default="")
    detected_materials: bpy.props.CollectionProperty(type=DetectedMaterialItem)
    detect_all_ok: bpy.props.BoolProperty(name="全部正确", default=False)
    show_detect_panel: bpy.props.BoolProperty(
        name="材质检测",
        description="展开/收起材质检测面板",
        default=False
    )

    _ntmi_modimp_extra_ps_t2_diffuse_map = False

    def apply_name_mapping(self, mapping):
        global _name_mapping_cache, _reverse_name_mapping_cache

        node_key = self.name
        _name_mapping_cache[node_key] = mapping.copy()
        _reverse_name_mapping_cache[node_key] = {}

        for original_name, new_name in mapping.items():
            _reverse_name_mapping_cache[node_key][new_name] = original_name

        print(f"[MaterialToResource] 已应用名称映射: {len(mapping)} 条规则")
        for original_name, new_name in mapping.items():
            print(f"  映射: '{original_name}' -> '{new_name}'")

    def _get_name_mapping(self):
        global _name_mapping_cache
        return _name_mapping_cache.get(self.name, {})

    def _get_reverse_name_mapping(self):
        global _reverse_name_mapping_cache
        return _reverse_name_mapping_cache.get(self.name, {})

    def draw_buttons(self, context, layout):
        layout.prop(self, "material_to_resource_override")
        layout.prop(self, "debug_disable_fx_ttl")
        layout.prop(self, "material_switch_var")

        name_mapping = self._get_name_mapping()
        if name_mapping:
            box = layout.box()
            box.label(text=f"已应用 {len(name_mapping)} 条名称映射", icon='INFO')

        layout.separator()
        header_row = layout.row(align=True)
        header_row.prop(self, "show_detect_panel", icon='TRIA_DOWN' if self.show_detect_panel else 'TRIA_RIGHT', text="材质检测", emboss=False)

        if self.show_detect_panel:
            box = layout.box()

            prefix_row = box.row(align=True)
            prefix_row.label(text="检测前缀:", icon='FILTER')
            op = prefix_row.operator("ssmt.material_detect_add_prefix", text="", icon='ADD')
            op.node_name = self.name

            for i, item in enumerate(self.material_detect_prefixes):
                row = box.row(align=True)
                row.label(text=item.prefix, icon='MATERIAL')
                op = row.operator("ssmt.material_detect_remove_prefix", text="", icon='X')
                op.node_name = self.name
                op.item_index = i

            input_row = box.row(align=True)
            input_row.prop(self, "temp_prefix_input", text="", icon='CONSOLE')
            op = input_row.operator("ssmt.material_detect_add_custom_prefix", text="", icon='ADD')
            op.node_name = self.name

            btn_row = box.row(align=True)
            op = btn_row.operator("ssmt.material_detect", text="检测材质", icon='VIEWZOOM')
            op.node_name = self.name
            op = btn_row.operator("ssmt.material_detect_clear", text="清除", icon='X')
            op.node_name = self.name

            if self.detected_materials:
                result_box = box.box()
                result_box.label(text=f"缺失材质 ({len(self.detected_materials)} 个)", icon='ERROR')
                for item in self.detected_materials:
                    row = result_box.row(align=True)
                    row.label(text=item.object_name, icon='OBJECT_DATA')
                    row.label(text=f"缺少: {item.missing_prefix}", icon='ERROR')
            elif self.detect_all_ok:
                result_box = box.box()
                result_box.label(text="全部正确", icon='CHECKMARK')

    def extract_mesh_name(self, line):
        match = re.search(r'\[mesh:([^\]]+)\]', line)
        return match.group(1) if match else None

    def _strip_all_suffixes(self, name):
        stripped_names = []
        current = name
        max_iterations = 20
        iteration = 0
        
        suffix_patterns = [
            r'_copy$',
            r'_chain\d+$',
            r'_dup\d+$',
        ]
        
        while iteration < max_iterations:
            changed = False
            for pattern in suffix_patterns:
                new_name = re.sub(pattern, '', current)
                if new_name != current:
                    if new_name not in stripped_names:
                        stripped_names.append(new_name)
                    current = new_name
                    changed = True
                    break
            if not changed:
                break
            iteration += 1
        
        return stripped_names

    def find_object_by_mesh_name(self, mesh_name, object_filter=None):
        # Names/mappings and scene objects stay fixed during this INI pass.
        # Cache ordered candidates, not a filtered result: per-section material
        # filters and Pro's target whitelist still run on every call.
        cache = _material_lookup_cache.get()
        if cache is None:
            return self._find_object_by_mesh_name_uncached(mesh_name, object_filter)
        key = (id(self), mesh_name)
        if key not in cache:
            candidates = []
            def collect(candidate):
                candidates.append(candidate)
                return False
            self._find_object_by_mesh_name_uncached(mesh_name, collect)
            cache[key] = candidates
        return next((obj for obj in cache[key] if object_filter is None or object_filter(obj)), None)

    def _find_object_by_mesh_name_uncached(self, mesh_name, object_filter=None):
        from ..utils.log_utils import LOG as _LOG
        _LOG.debug(f"[find_object_by_mesh_name] 输入 mesh_name: '{mesh_name}'")
        
        reverse_mapping = self._get_reverse_name_mapping()
        name_mapping = self._get_name_mapping()
        _LOG.debug(f"  reverse_mapping 条目数: {len(reverse_mapping)}")
        _LOG.debug(f"  name_mapping 条目数: {len(name_mapping)}")

        potential_names = []

        all_stripped = self._strip_all_suffixes(mesh_name)
        for stripped in all_stripped:
            if stripped not in potential_names:
                potential_names.append(stripped)
                _LOG.debug(f"  移除后缀生成: '{stripped}'")

        potential_names.append(mesh_name)
        _LOG.debug(f"  初始 potential_names: {potential_names}")

        if reverse_mapping:
            for new_name, original_name in reverse_mapping.items():
                if new_name == mesh_name:
                    if original_name not in potential_names:
                        potential_names.append(original_name)
                        _LOG.debug(f"  反向映射精确匹配: '{new_name}' -> '{original_name}'")
                    for stripped in self._strip_all_suffixes(original_name):
                        if stripped not in potential_names:
                            potential_names.append(stripped)
                            _LOG.debug(f"  反向映射+后缀: '{original_name}' -> '{stripped}'")
                elif new_name in mesh_name:
                    original_mesh_name = mesh_name.replace(new_name, original_name)
                    if original_mesh_name not in potential_names:
                        potential_names.append(original_mesh_name)
                        _LOG.debug(f"  反向映射部分匹配: '{new_name}' in '{mesh_name}' -> '{original_mesh_name}'")
                    for stripped in self._strip_all_suffixes(original_mesh_name):
                        if stripped not in potential_names:
                            potential_names.append(stripped)
                            _LOG.debug(f"  反向映射部分+后缀: '{original_mesh_name}' -> '{stripped}'")

        if name_mapping:
            for original_name, new_name in name_mapping.items():
                if original_name in mesh_name:
                    renamed_mesh_name = mesh_name.replace(original_name, new_name)
                    if renamed_mesh_name not in potential_names:
                        potential_names.append(renamed_mesh_name)
                        _LOG.debug(f"  正向映射: '{original_name}' -> '{new_name}' in '{mesh_name}' -> '{renamed_mesh_name}'")

        _LOG.debug(f"  第一阶段 potential_names: {potential_names}")
        for name in potential_names:
            obj = bpy.data.objects.get(name)
            if obj:
                if object_filter is not None and not object_filter(obj):
                    _LOG.debug(f"  ⚠️ 找到物体但材质不匹配，继续查找: '{name}'")
                    continue
                _LOG.debug(f"  ✅ 找到物体: '{name}'")
                return obj
            else:
                _LOG.debug(f"  ❌ 未找到物体: '{name}'")

        clean_name = re.sub(r'^[a-f0-9]+-[\d]+-', '', mesh_name)
        _LOG.debug(f"  清理前缀后的名称: '{clean_name}' (原: '{mesh_name}')")
        if clean_name != mesh_name:
            potential_clean_names = []

            for stripped in self._strip_all_suffixes(clean_name):
                if stripped not in potential_clean_names:
                    potential_clean_names.append(stripped)
                    _LOG.debug(f"    清理后移除后缀生成: '{stripped}'")

            potential_clean_names.append(clean_name)
            _LOG.debug(f"    初始 potential_clean_names: {potential_clean_names}")

            if name_mapping:
                for original_name, new_name in name_mapping.items():
                    if original_name in clean_name:
                        renamed_clean_name = clean_name.replace(original_name, new_name)
                        if renamed_clean_name not in potential_clean_names:
                            potential_clean_names.append(renamed_clean_name)
                            _LOG.debug(f"    清理后正向映射: '{original_name}' -> '{new_name}' -> '{renamed_clean_name}'")
                        for stripped in self._strip_all_suffixes(renamed_clean_name):
                            if stripped not in potential_clean_names:
                                potential_clean_names.append(stripped)
                                _LOG.debug(f"    清理后正向映射+后缀: '{renamed_clean_name}' -> '{stripped}'")

            if reverse_mapping:
                for new_name, original_name in reverse_mapping.items():
                    original_clean = re.sub(r'^[a-f0-9]+-[\d]+-', '', original_name)
                    if original_clean and original_clean not in potential_clean_names:
                        potential_clean_names.append(original_clean)
                        _LOG.debug(f"    反向映射原始名清理前缀: '{original_name}' -> '{original_clean}'")
                        for stripped in self._strip_all_suffixes(original_clean):
                            if stripped not in potential_clean_names:
                                potential_clean_names.append(stripped)
                                _LOG.debug(f"    反向映射原始名清理+后缀: '{original_clean}' -> '{stripped}'")
                    if new_name in clean_name:
                        original_clean_name = clean_name.replace(new_name, original_name)
                        if original_clean_name not in potential_clean_names:
                            potential_clean_names.append(original_clean_name)
                            _LOG.debug(f"    清理后反向映射部分: '{new_name}' -> '{original_clean_name}'")
                    elif new_name == clean_name:
                        if original_name not in potential_clean_names:
                            potential_clean_names.append(original_name)
                            _LOG.debug(f"    清理后反向映射精确: '{new_name}' -> '{original_name}'")

            _LOG.debug(f"  第二阶段 potential_clean_names: {potential_clean_names}")
            for name in potential_clean_names:
                obj = bpy.data.objects.get(name)
                if obj:
                    if object_filter is not None and not object_filter(obj):
                        _LOG.debug(f"  ⚠️ 找到物体但材质不匹配，继续查找: '{name}'")
                        continue
                    _LOG.debug(f"  ✅ 找到物体: '{name}'")
                    return obj
                else:
                    _LOG.debug(f"  ❌ 未找到物体: '{name}'")

        _LOG.debug(f"  ⚠️ 最终未找到匹配物体: '{mesh_name}'")
        return None

    def extract_transparency_info_from_mesh_name(self, mesh_name):
        match = re.search(r'(.+)_透明(\d+(\.\d+)?)', mesh_name)
        if match:
            base_name = match.group(1)
            transparency_value = match.group(2)
            shader_name = f"CustomShaderTransparencyCloth{base_name.replace('-', '_').replace('.', '_')}_透明{transparency_value}"
            shader_name = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(shader_name)
            return shader_name, transparency_value
        return None, None

    def extract_texture_type_from_resource(self, resource_name):
        match = re.search(r'_Slot_([^_]+)$', resource_name)
        if match:
            return match.group(1)

        match = re.search(r'ResourceTexture_[^=]+_(T\d+)(?:_\d+)?$', resource_name, re.IGNORECASE)
        if match:
            return match.group(1)

        match = re.search(r'Resource-.*-([^_-]+)$', resource_name)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _parse_ps_texture_material_slot(material_name):
        clean_name = str(material_name or "").strip()
        if not clean_name:
            return None
        match = re.match(r'^(?:ps[-_ ]*)?t(\d+)(?:[_\-. ].*)?$', clean_name, re.IGNORECASE)
        if not match:
            return None

        slot_number = str(int(match.group(1)))
        return f"ps-t{slot_number}", f"T{slot_number}"

    @staticmethod
    def _resource_token(value):
        token = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip())
        token = token.strip("_")
        return token or "part"

    @staticmethod
    def _resource_name_token(value):
        token = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "").strip())
        token = token.strip("_.-")
        return token or "Texture"

    @staticmethod
    def _latin_token_for_text(text):
        # 非 ASCII 文本（如中文）映射为确定性的“随机”英文串：由内容哈希派生，
        # 同一文本永远得到同一串字母，引用行与资源定义段才能始终对应。
        digest = hashlib.md5(str(text).encode("utf-8")).digest()
        value = int.from_bytes(digest[:8], "big")
        letters = []
        for _ in range(10):
            letters.append(chr(ord("a") + (value % 26)))
            value //= 26
        return "".join(letters).capitalize()

    @staticmethod
    def _replace_non_ascii_runs(text):
        return re.sub(
            r"[^\x00-\x7f]+",
            lambda match: SSMTNode_PostProcess_MaterialBase._latin_token_for_text(match.group(0)),
            str(text or ""),
        )

    @staticmethod
    def _material_resource_stem(material):
        material_name = str(getattr(material, "name", "") or "").strip()
        if not material_name:
            material_name = "Texture"
        stem = re.sub(r'[\r\n\[\]=<>:"/\\|?*\s]+', "_", material_name).strip()
        stem = SSMTNode_PostProcess_MaterialBase._replace_non_ascii_runs(stem)
        return stem.strip("._") or "Texture"

    @staticmethod
    def _normalize_ps_texture_slot_label(value):
        clean_value = str(value or "").strip()
        if not clean_value:
            return None
        match = re.match(r'^(?:ps[-_ ]*)?t(\d+)$', clean_value, re.IGNORECASE)
        if not match:
            return None
        slot_number = str(int(match.group(1)))
        return f"ps-t{slot_number}", f"T{slot_number}"

    @classmethod
    def _infer_ps_texture_slot_label(cls, *values):
        for value in values:
            normalized_slot = cls._normalize_ps_texture_slot_label(value)
            if normalized_slot:
                return normalized_slot

            basename = os.path.basename(str(value or ""))
            match = re.search(r'(?:^|[-_])t(\d+)(?:[-_.]|$)', basename, re.IGNORECASE)
            if match:
                slot_number = str(int(match.group(1)))
                return f"ps-t{slot_number}", f"T{slot_number}"

        return None

    def _object_texture_resource_identity(self, obj):
        try:
            from ..common.object_prefix_helper import ObjectPrefixHelper
        except Exception:
            ObjectPrefixHelper = None

        candidate_names = []

        def append_candidate_name(name):
            name = str(name or "").strip()
            if name and name not in candidate_names:
                candidate_names.append(name)

        if obj:
            append_candidate_name(getattr(obj, "name", ""))
            append_candidate_name(getattr(obj, "original_object_name", ""))
            for stripped_name in self._strip_all_suffixes(getattr(obj, "name", "")):
                append_candidate_name(stripped_name)

            if ObjectPrefixHelper is not None:
                try:
                    append_candidate_name(ObjectPrefixHelper.resolve_source_object_name(obj.name))
                except Exception:
                    pass

        if ObjectPrefixHelper is not None:
            for name in candidate_names:
                try:
                    prefix_info = ObjectPrefixHelper.extract_prefix_info(name)
                except Exception:
                    prefix_info = None
                if not prefix_info:
                    continue
                prefix, _ = prefix_info
                parts = ObjectPrefixHelper.parse_prefix_parts(prefix)
                draw_ib = str(parts.get("draw_ib", "") or "").strip()
                index_count = str(parts.get("index_count", "") or "").strip()
                first_index = str(parts.get("first_index", "") or "").strip()
                if draw_ib and index_count and first_index:
                    return f"{draw_ib}-{index_count}-{first_index}"
                if draw_ib:
                    return draw_ib

        for name in candidate_names:
            clean_name = str(name or "").strip()
            if not clean_name:
                continue
            clean_name = re.sub(r"^LOD\d+\.", "", clean_name, flags=re.IGNORECASE)
            prefix_candidate = clean_name.split(".", 1)[0]
            match = re.match(r'^([A-Za-z0-9]{6,})[-_](\d+)[-_](\d+)', prefix_candidate)
            if match:
                return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
            match = re.match(r'^([A-Za-z0-9]{6,})', prefix_candidate)
            if match:
                return match.group(1)

        return self._resource_name_token(getattr(obj, "name", "") if obj else "")

    def _object_texture_resource_token(self, obj):
        return self._resource_token(self._object_texture_resource_identity(obj))

    def _ps_texture_resource_name(self, obj, slot_label):
        slot_token = str(slot_label or "").replace("ps-", "").replace("-", "_").upper()
        return f"ResourceTexture_{self._object_texture_resource_token(obj)}_{slot_token}"

    @staticmethod
    def _ps_texture_material_resource_name(material):
        return f"ResourceTexture_{SSMTNode_PostProcess_MaterialBase._material_resource_stem(material)}"

    def _collect_ntmi_cached_texture_slots_raw(self, obj):
        if obj is None:
            return ""
        try:
            from ..ui.ntmi_modimp.prefix_property_cache import get_prefix_record_props, has_prefix_record
        except Exception:
            return ""

        try:
            cached_props = get_prefix_record_props(getattr(obj, "name", ""))
            has_cached_record = has_prefix_record(getattr(obj, "name", ""))
        except Exception:
            return ""
        if not has_cached_record:
            return None
        if not isinstance(cached_props, dict):
            return ""

        workspace_unique_str = str(cached_props.get("modimp_workspace_unique_str", "") or "").strip()
        profile_id = str(cached_props.get("modimp_profile_id", "") or "").strip().lower()
        if not workspace_unique_str and profile_id not in {"yihuan", "ntemi"}:
            return ""

        return str(cached_props.get("modimp_texture_slots", "") or "").strip()

    def _collect_modimp_texture_slots(self, obj):
        import json as _json

        result = OrderedDict()
        raw = self._collect_ntmi_cached_texture_slots_raw(obj)
        if raw is None:
            raw = str(obj.get("modimp_texture_slots", "") or "").strip()
        if not raw:
            return result
        try:
            slots = _json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return result
        if not isinstance(slots, dict):
            return result

        for slot_label, binding in slots.items():
            if not isinstance(binding, dict):
                continue
            mark_name = str(binding.get("mark_name", "") or "").strip()
            source_path = str(binding.get("source_path", "") or "").strip()
            normalized_slot = self._infer_ps_texture_slot_label(
                slot_label,
                binding.get("mark_slot", ""),
                binding.get("slot", ""),
                source_path,
                binding.get("mark_filename", ""),
            )
            if not normalized_slot:
                continue
            if not mark_name:
                continue
            param_name, slot_token = normalized_slot
            slot_info = dict(binding)
            slot_info["param_name"] = param_name
            slot_info["slot_token"] = slot_token
            slot_info["mark_name"] = mark_name
            slot_info["source_path"] = source_path
            result[param_name] = slot_info
        return result

    def _workspace_material_output_filename(self, material, source_path=None):
        source_path = str(source_path or "").strip()
        source_extension = os.path.splitext(source_path)[1] or ".dds"
        return f"{self._material_resource_stem(material)}{source_extension}"

    def _workspace_material_resource_name(self, material):
        return f"Resource-{self._material_resource_stem(material)}"

    @staticmethod
    def _clone_generated_param_lines(lines, source_param, target_param):
        if not lines:
            return []

        source_param = str(source_param or "").strip()
        target_param = str(target_param or "").strip()
        if not source_param or not target_param or source_param == target_param:
            return []

        assignment_re = re.compile(
            rf"^(?P<indent>\s*){re.escape(source_param)}\s*=\s*(?:(?P<ref>ref)\s+)?(?P<resource>.+?)\s*$",
            re.IGNORECASE,
        )
        alias_lines = []
        target_is_resource_ref = target_param.lower().startswith("resource\\")

        for line in lines:
            match = assignment_re.match(str(line or ""))
            if not match:
                continue

            indent = match.group("indent") or ""
            resource_name = str(match.group("resource") or "").strip()
            if target_is_resource_ref:
                alias_lines.append(f"{indent}{target_param} = ref {resource_name}")
            else:
                alias_lines.append(f"{indent}{target_param} = {resource_name}")

        return alias_lines

    @staticmethod
    def _find_first_slot_by_mark_name(slot_map, mark_name):
        target_mark_name = str(mark_name or "").strip().lower()
        if not target_mark_name:
            return ""

        for param_name, slot_info in (slot_map or {}).items():
            current_mark_name = str(getattr(slot_info, "get", lambda *_args, **_kwargs: "")("mark_name", "") or "").strip().lower()
            if current_mark_name == target_mark_name:
                return str(param_name or "").strip().lower()
        return ""

    @staticmethod
    def _ensure_alias_assignment_in_lines(lines, source_param, target_param):
        source_param = str(source_param or "").strip()
        target_param = str(target_param or "").strip()
        if not source_param or not target_param or source_param == target_param:
            return False

        source_re = re.compile(
            rf"^(?P<indent>\s*){re.escape(source_param)}\s*=\s*(?:(?P<ref>ref)\s+)?(?P<resource>.+?)\s*$",
            re.IGNORECASE,
        )
        target_re = re.compile(
            rf"^\s*{re.escape(target_param)}\s*=",
            re.IGNORECASE,
        )

        if any(target_re.match(str(line or "")) for line in lines):
            return False

        for index, line in enumerate(lines):
            match = source_re.match(str(line or ""))
            if not match:
                continue
            indent = match.group("indent") or ""
            resource_name = str(match.group("resource") or "").strip()
            lines.insert(index, f"{indent}{target_param} = {resource_name}")
            return True

        return False

    def _find_workspace_slot_materials(self, obj, slot_info):
        mark_name = str(slot_info.get("mark_name", "") or "").strip()
        if not mark_name:
            return []

        texture_type_lower = mark_name.lower()
        
        def find_materials_in_object(target_obj):
            matching_materials = OrderedDict()
            for material_slot in getattr(target_obj, "material_slots", []):
                material = material_slot.material
                if not material:
                    continue
                material_name = str(getattr(material, "name", "") or "")
                if texture_type_lower == "fxmap" and not material_name.lower().startswith("fxmap_"):
                    continue
                if not material_name.lower().startswith(texture_type_lower):
                    continue
                material_first_word = material_name.split('_')[0].lower()
                if material_first_word != texture_type_lower:
                    continue
                signature = self._build_material_signature(material)
                if signature not in matching_materials:
                    matching_materials[signature] = material
            return list(matching_materials.values())
        
        if obj:
            obj_materials = find_materials_in_object(obj)
            if obj_materials:
                return obj_materials

        return []

    def _create_workspace_texture_resource_entry(self, obj, slot_info, texture_folder, all_sections, texture_ini_folder="Textures"):
        material = next(iter(self._find_workspace_slot_materials(obj, slot_info)), None)
        if not material:
            return None, None
        texture_image = self.get_texture_from_material(material)
        if not texture_image:
            return None, None
        source_path = bpy.path.abspath(getattr(texture_image, "filepath", "") or "")
        if not source_path or not os.path.exists(source_path):
            return None, None

        os.makedirs(texture_folder, exist_ok=True)
        output_filename = self._workspace_material_output_filename(material, source_path=source_path)
        target_path = os.path.join(texture_folder, output_filename)

        # 贴图计划（落盘前联合去重）：这条路径不转 DDS（spec=copy），与基础导出同规格，
        # 内容重复时复用代表文件；未启用时保持原来的立即复制。
        if TextureExportPlan is not None and TextureExportPlan.enabled():
            planned_filename = TextureExportPlan.register(
                source_path,
                texture_folder,
                output_filename,
                owner=TextureExportPlan.OWNER_MATERIAL,
                owner_label=str(getattr(self, "name", "") or ""),
                spec=("copy",),
                overwrite_policy=(
                    "overwrite" if self.material_to_resource_override else "skip"
                ),
            )
            if not planned_filename:
                return None, None
            output_filename = planned_filename
        else:
            try:
                source_abs = os.path.abspath(source_path)
                target_abs = os.path.abspath(target_path)
                if source_abs != target_abs:
                    if self.material_to_resource_override or not os.path.exists(target_path):
                        shutil.copy2(source_path, target_path)
            except Exception as e:
                print(f"复制工作空间贴图失败: {e}")
                return None, None

        resource_name = self._workspace_material_resource_name(material)
        resource_section_name = f"[{resource_name}]"
        texture_ini_folder = str(texture_ini_folder or "Textures").strip().strip("\\/")
        desired_line = f"filename = {texture_ini_folder}/{output_filename}".replace("\\", "/")
        existing_lines = all_sections.get(resource_section_name, [])
        existing_normalized = {
            line.strip().replace("\\", "/")
            for line in existing_lines
        }
        if self.material_to_resource_override or desired_line not in existing_normalized:
            all_sections[resource_section_name] = [desired_line]

        param_name = slot_info.get("param_name", "")
        return f"{param_name} = {resource_name}", resource_name

    def _has_strict_fxmap_material(self, obj):
        if not obj:
            return False
        for material_slot in getattr(obj, "material_slots", []):
            material = material_slot.material
            material_name = str(getattr(material, "name", "") or "")
            if material_name.lower().startswith("fxmap_"):
                return True
        return False

    def _collect_ntemifx_texture_slots(self, obj, workspace_resource_by_slot=None) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self._has_strict_fxmap_material(obj):
            return result

        if workspace_resource_by_slot:
            for slot_label, slot_info in workspace_resource_by_slot.items():
                mark_name = str(slot_info.get("mark_name", "") or "").strip()
                resource_name = str(slot_info.get("resource_name", "") or "").strip()
                if mark_name == "FXMap" and resource_name:
                    result[slot_label] = resource_name
            return result

        for slot_label, slot_info in self._collect_modimp_texture_slots(obj).items():
            mark_name = str(slot_info.get("mark_name", "") or "").strip()
            if mark_name != "FXMap":
                continue
            material = next(iter(self._find_workspace_slot_materials(obj, slot_info)), None)
            if material:
                result[slot_label] = self._workspace_material_resource_name(material)
        return result

    def _collect_ps_texture_slot_materials(self, obj):
        slot_to_materials = OrderedDict()
        if not obj:
            return slot_to_materials

        def collect_from_object(target_obj):
            result = OrderedDict()
            for material_slot in getattr(target_obj, "material_slots", []):
                material = material_slot.material
                if not material:
                    continue
                parsed_slot = self._parse_ps_texture_material_slot(material.name)
                if not parsed_slot:
                    continue
                param_name, texture_type = parsed_slot
                slot_info = result.setdefault(
                    param_name,
                    {
                        "texture_type": texture_type,
                        "materials": OrderedDict(),
                    },
                )
                signature = self._build_material_signature(material)
                if signature not in slot_info["materials"]:
                    slot_info["materials"][signature] = material
            return result
        
        obj_slots = collect_from_object(obj)
        if obj_slots:
            return obj_slots

        return slot_to_materials

    def build_mapping_for_section(self, lines):
        mapping = OrderedDict()
        line_pattern = re.compile(r'^(ps-t\d+|Resource\\[^\s=]+)\s*=\s*(?:ref\s+)?(.*)$')
        for line in lines:
            resource_match = line_pattern.match(line.strip())
            if resource_match:
                param_name = resource_match.group(1).strip()
                resource_name = resource_match.group(2).strip()
                texture_type = self.extract_texture_type_from_resource(resource_name)
                if texture_type:
                    mapping[param_name] = texture_type
        return mapping

    def find_matching_materials(self, obj, texture_type):
        texture_type_lower = texture_type.lower()

        def find_in_object(target_obj):
            result = []
            for material_slot in getattr(target_obj, "material_slots", []):
                material = material_slot.material
                if not material:
                    continue
                if texture_type_lower == "fxmap" and not material.name.lower().startswith("fxmap_"):
                    continue
                if not material.name.lower().startswith(texture_type_lower):
                    continue
                material_first_word = material.name.split('_')[0].lower()
                if material_first_word == texture_type_lower:
                    # 多档切换里每个材质占一档，不能按"材质签名"去重：去重会让某个槽位
                    # 的档数少于其它槽位（实机：同一个身体 Diffuse 5 档 / Normal 4 档 /
                    # Light 3 档），切到高档位时缺失的槽位沿用上一档贴图 → 贴图错乱。
                    # 同图材质由 ``create_resource_entry`` 的签名缓存复用同一资源，
                    # 不会重复复制，也不会多出重复的 Resource 段。
                    result.append(material)
            return result
        
        if obj:
            obj_materials = find_in_object(obj)
            if obj_materials:
                return obj_materials

        return []

    def _object_has_matching_materials(self, obj, ini_mapping):
        for slot_info in self._collect_modimp_texture_slots(obj).values():
            texture_type = str(slot_info.get("mark_name", "") or "").strip()
            if texture_type and texture_type != "FXMap" and self._find_workspace_slot_materials(obj, slot_info):
                return True

        for param_name, texture_type in ini_mapping.items():
            param_lower = str(param_name or "").lower()
            is_supported_param = (
                param_lower.startswith("ps-t")
                or param_lower.startswith("resource\\zzmi\\")
                or _starts_with_fx_resource_prefix(param_lower)
            )
            if not is_supported_param:
                continue
            if texture_type == "FXMap" and param_lower.startswith("ps-t"):
                continue
            if self.find_matching_materials(obj, texture_type):
                return True

        if self._collect_ps_texture_slot_materials(obj):
            return True

        # 贴图类型 → 材质前缀兜底。TTLMap 对所有运行时都算：ZZMI 拆两遍绘制、EFMI 交给
        # EFMITTL 库，两种协议都靠 TTLMap_ 材质认物。把它只留给 HI3FX，会让「没有贴图
        # 标记、段内也没有 ps-t」的物体在这里被拦掉，后面的 FX/TTL 生成跟着一起冻结。
        fallback_texture_types = ["Glowmap", "FXMap", "TTLMap"]
        return any(
            self.find_matching_materials(obj, texture_type)
            for texture_type in fallback_texture_types
        )

    @staticmethod
    def _build_material_signature(material):
        if not material:
            return ("",)

        material_name = str(getattr(material, "name", "") or "")
        type_prefix = material_name.split('_')[0].lower()
        
        main_texture_path = None
        if getattr(material, "use_nodes", False) and getattr(material, "node_tree", None):
            try:
                output_node = next(n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
                surface_input = output_node.inputs.get('Surface')
                if surface_input and surface_input.is_linked:
                    def find_texture_node_recursively(node):
                        if node.type == 'TEX_IMAGE' and node.image:
                            image = node.image
                            return bpy.path.abspath(getattr(image, "filepath", "") or "") or image.name
                        for input_socket in node.inputs:
                            if input_socket.is_linked:
                                found = find_texture_node_recursively(input_socket.links[0].from_node)
                                if found: return found
                        return None
                    main_texture_path = find_texture_node_recursively(surface_input.links[0].from_node)
            except (StopIteration, AttributeError):
                pass
            
            if not main_texture_path:
                for node in material.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        image = node.image
                        main_texture_path = bpy.path.abspath(getattr(image, "filepath", "") or "") or image.name
                        break

        if main_texture_path:
            return (type_prefix, main_texture_path)
        return (type_prefix, material_name)

    def get_texture_from_material(self, material):
        if not material or not material.use_nodes:
            return None
        try:
            output_node = next(n for n in material.node_tree.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
            surface_input = output_node.inputs.get('Surface')
            if surface_input and surface_input.is_linked:
                def find_texture_node_recursively(node):
                    if node.type == 'TEX_IMAGE' and node.image:
                        return node.image
                    for input_socket in node.inputs:
                        if input_socket.is_linked:
                            found_image = find_texture_node_recursively(input_socket.links[0].from_node)
                            if found_image: return found_image
                    return None
                linked_image = find_texture_node_recursively(surface_input.links[0].from_node)
                if linked_image: return linked_image
        except (StopIteration, AttributeError):
            pass
        for node in material.node_tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                return node.image
        return None

    # -- 后台导出细分进度：贴图复制 / 转 DDS --------------------------------
    #
    # 材质转资源pro 的 texconv 转换是耗时大头（实测 26 张 ≈13~20s），期间原来
    # 状态栏停在"材质转资源pro"一动不动，用户会以为卡死。这里按"每张已处理贴图"
    # 上报一次细粒度文本。只影响**显示**：``export_progress`` 没有 stage 文件时
    # （同步导出 / 主线程）静默返回，不改导出流程与产物。

    def _reset_texture_progress(self) -> None:
        """一次导出开始时清零贴图计数。"""
        self._tex_progress_done = 0
        self._tex_progress_total = 0

    def _plan_texture_progress(self, count) -> None:
        """登记本批次预计处理的贴图数（只累加；用于显示 ``i/total`` 的 total）。"""
        try:
            planned = int(getattr(self, "_tex_progress_total", 0) or 0)
            self._tex_progress_total = planned + max(0, int(count or 0))
        except (TypeError, ValueError):
            pass

    def _report_texture_progress(self) -> None:
        """每张进入复制的贴图前上报一次；任何异常都吞掉，绝不影响导出。

        ``export_progress`` 按需导入：轻量 fake 宿主（单测）没有这个子模块，
        同步导出 / 主线程也没有 stage 文件 —— 两种情况都保持静默。
        """
        try:
            from . import export_progress

            done = int(getattr(self, "_tex_progress_done", 0) or 0) + 1
            self._tex_progress_done = done
            total = max(int(getattr(self, "_tex_progress_total", 0) or 0), done)
            self._tex_progress_total = total
            # 前缀用节点名（默认就叫「材质转资源pro」）：节点完成时进度中台会按
            # "<节点名>：" 前缀清掉这段细分文本，避免漏到下一个节点。
            label = str(getattr(self, "name", "") or "").strip() or "材质转资源pro"
            export_progress.report_stage(f"{label}：转换贴图 {done}/{total}")
        except Exception:
            pass

    def _register_texture_plan_entry(self, source_path, target_folder, stem, file_extension,
                                     material, forced_filename=None, workspace_source_path=None,
                                     needs_convert=False, dedup_group=None):
        """把材质贴图登记进「贴图落盘计划」并返回 ini 应写的文件名。

        与 ``copy_texture_file`` 的旧逻辑一一对应：
        - 需要转 DDS：首选名 ``stem.dds``，转换参数交给计划器（落盘时只转一次）；
        - 不需要转换：沿用 ``forced_filename`` / 源扩展名；
        - 落盘规格（spec）只按贴图类型区分：同一类型内按资源合并节点设置的模式
          （MD5 / 像素级 / 相似度）判定为重复就合并，**不看源文件名 / MD5 / 格式**；
          不同类型绝不合并（``("dds", "LightMap")`` 与 ``("dds", "MaterialMap")`` 分开，
          线性贴图与 sRGB 贴图内容相同也不能共用一个 DDS）。
        内容与已登记项重复时计划器直接返回代表文件名，本张不再复制也不再转换。
        """
        is_overwrite = bool(getattr(self, "material_to_resource_override", False))
        if TextureExportPlan is None:
            return None
        texture_type = str(getattr(material, "name", "") or "").strip().split("_")[0]

        if needs_convert:
            preferred_filename = f"{stem}.dds"
            # 规格只按「贴图类型」区分：同类型内重复判定（MD5/像素/相似度，按节点设置）
            # 命中即合并；跨类型（如 LightMap 线性 vs MaterialMap sRGB）永不合并。
            spec = ("dds", texture_type)
            convert_kwargs = {
                "props": bpy.context.scene.texture_tools_props,
                "force": is_overwrite,
                "match_name": stem,
                "texture_type": texture_type,
                "name_hints": (str(getattr(material, "name", "") or ""), stem),
                "workspace_source_path": workspace_source_path or "",
            }
        else:
            if (
                forced_filename
                and os.path.splitext(forced_filename)[1].lower() == file_extension.lower()
            ):
                preferred_filename = forced_filename
            else:
                preferred_filename = f"{stem}{file_extension}"
            spec = ("copy",)
            convert_kwargs = None

        return TextureExportPlan.register(
            source_path,
            target_folder,
            preferred_filename,
            owner=TextureExportPlan.OWNER_MATERIAL,
            owner_label=str(getattr(self, "name", "") or ""),
            spec=spec,
            convert_kwargs=convert_kwargs,
            overwrite_policy="overwrite" if is_overwrite else "skip",
            dedup_group=dedup_group,
        )

    def copy_texture_file(self, texture_image, target_folder, material, forced_filename=None,
                          workspace_source_path=None, dedup_group=None):
        if not texture_image or not texture_image.filepath:
            return None
        try:
            source_path = bpy.path.abspath(texture_image.filepath)
            if not os.path.exists(source_path): return None
            os.makedirs(target_folder, exist_ok=True)

            stem = SSMTNode_PostProcess_MaterialBase._material_resource_stem(material)
            _, file_extension = os.path.splitext(os.path.basename(source_path))

            # 贴图计划（落盘前联合去重）：只登记 + 决策，复制 / 转 DDS 由
            # TextureExportPlan.finalize() 统一执行；内容重复时直接复用代表文件名。
            if TextureExportPlan is not None and TextureExportPlan.enabled():
                source_is_dds_for_plan = is_dds_file(source_path)
                return self._register_texture_plan_entry(
                    source_path=source_path,
                    target_folder=target_folder,
                    stem=stem,
                    file_extension=file_extension,
                    material=material,
                    forced_filename=forced_filename,
                    workspace_source_path=workspace_source_path,
                    dedup_group=dedup_group,
                    needs_convert=(
                        (file_extension.lower() != ".dds" or not source_is_dds_for_plan)
                        and getattr(self, "export_textures_as_dds", False)
                    ),
                )

            # 导出 DDS 优先：编辑期贴图常是无损 PNG（避免反复存 DDS 时被过度压缩），但游戏
            # 只认 DDS，所以导出时按现有 DDS 规则转一份放进 Mod，INI 的 filename 靠本函数
            # 的返回值生成，自然跟着指向 .dds。开关在「材质转资源pro」节点上；旧版弃用壳没有
            # 该属性，getattr 兜底为 False，行为与改动前一致。
            #
            # 判断按**文件内容**而不是扩展名：工作空间里的贴图常是「改后缀的 PNG」
            # （F:\library\...\贴图\10.dds 实际是 PNG 字节）。只按扩展名判断会跳过转换、
            # 把 PNG 字节原样复制成 .dds，3DMigoto 按 DDS 读取失败 → 游戏里贴图没效果。
            source_is_dds = is_dds_file(source_path)
            if (file_extension.lower() != ".dds" or not source_is_dds) and getattr(
                self, "export_textures_as_dds", False
            ):
                converted_name = f"{stem}.dds"
                converted_path = os.path.join(target_folder, converted_name)
                # 格式来源优先级：工作空间里这张材质的原贴图 DDS 头 → 材质类型规则 → 源文件名。
                # 引擎读的就是原贴图那份字节（BC5 是双通道格式），照抄它的格式才不会读错。
                texture_type = str(getattr(material, "name", "") or "").strip().split("_")[0]
                ok, reason = convert_texture_to_dds_cached(
                    source_path,
                    converted_path,
                    props=bpy.context.scene.texture_tools_props,
                    force=bool(self.material_to_resource_override),
                    match_name=stem,
                    texture_type=texture_type,
                    name_hints=(getattr(material, "name", ""), stem),
                    workspace_source_path=workspace_source_path or "",
                )
                if ok:
                    return converted_name
                # 转换失败不中断导出：退回原样复制。文件名也必须用回源扩展名 ——
                # forced_filename 可能来自上一轮成功缓存的 .dds，此刻已无对应文件。
                print(f"贴图转 DDS 失败，回退为原样复制: {source_path} - {reason}")

            if forced_filename and os.path.splitext(forced_filename)[1].lower() == file_extension.lower():
                new_filename = forced_filename
            else:
                # 缓存名与本轮源扩展名不一致时不能沿用：forced_filename 可能是上一轮
                # 成功转出的 .dds，此刻已无对应文件（例如本次转换失败、或编辑期贴图刚被
                # 重链成 PNG），照抄会把 PNG 字节写成 .dds，游戏读不出来。
                new_filename = f"{stem}{file_extension}"

            target_path = os.path.join(target_folder, new_filename)
            if (
                os.path.exists(target_path)
                and not self.material_to_resource_override
                and is_dds_file(target_path)
            ):
                return new_filename
            shutil.copy2(source_path, target_path)
            return new_filename
        except Exception as e:
            print(f"复制纹理文件失败: {e}")
            return None

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict()
        current_section = None
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped_line = line.strip()
                    if stripped_line.startswith('[') and stripped_line.endswith(']'):
                        current_section = stripped_line
                        sections[current_section] = []
                    elif not stripped_line:
                        continue
                    elif current_section is not None:
                        sections[current_section].append(line.rstrip())
        except FileNotFoundError:
            return None
        return sections

    def _write_ordered_dict_to_ini(self, sections, ini_file_path):
        with open(ini_file_path, 'w', encoding='utf-8') as f:
            for section_name, lines in sections.items():
                f.write(f"{section_name}\n")
                for line in lines:
                    f.write(f"{line}\n")
                f.write("\n")

    def define_swapkeys_in_sections(self, sections, keys_to_define):
        if not keys_to_define: return
        if '[Constants]' not in sections:
            new_sections = OrderedDict([('[Constants]', [])])
            new_sections.update(sections)
            sections.clear()
            sections.update(new_sections)
        constants_lines = sections['[Constants]']
        existing_definitions = "".join(constants_lines)
        for key in sorted(list(keys_to_define)):
            if key not in existing_definitions:
                definition = f"global persist {key} = 0"
                constants_lines.append(definition)

    def _workspace_source_paths_for_materials(self, obj):
        """材质名 → 工作空间里该材质原贴图的路径。

        导出格式必须照抄原贴图的 DDS 头（引擎按原通道读，BC5 就是双通道），这条映射就是
        在导出期找到那份原贴图的唯一桥梁：节点从 modimp_texture_slots 里拿到 source_path。
        拿不到（节点不是从工作空间导入、或槽位缺 source_path）就返回空表，格式回落到规则表。
        """
        mapping = {}
        if obj is None:
            return mapping
        try:
            slots = self._collect_modimp_texture_slots(obj)
        except Exception:
            return mapping
        for slot_info in slots.values():
            source_path = str(slot_info.get("source_path", "") or "").strip()
            if not source_path:
                continue
            for material in self._find_workspace_slot_materials(obj, slot_info):
                mapping.setdefault(str(getattr(material, "name", "") or ""), source_path)
        return mapping


    def _order_materials_by_switch_group(self, materials, group_key):
        """按切换组扫描时的档位顺序排列材质（与面板/热键看到的档位一致）。

        导出时 ``find_matching_materials`` 按材质槽顺序返回，而扫描生成的切换组
        ``bindings`` 是排序后的材质名列表；两者顺序不同会让「第 N 档」对不上用户配置
        的第 N 档（切档显示另一档的贴图）。拿不到顺序时保持原样。
        """
        order_map = getattr(self, "_material_group_order", None) or {}
        order = order_map.get(group_key)
        if not order:
            return list(materials)
        by_name = {
            str(getattr(material, "name", "") or ""): material for material in materials
        }
        ordered = [by_name[name] for name in order if name in by_name]
        seen = set(order)
        ordered.extend(
            material
            for material in materials
            if str(getattr(material, "name", "") or "") not in seen
        )
        return ordered

    @staticmethod
    def _best_matching_switch_group(mat_names_tuple, material_group_to_swapkey):
        """材质集合与扫描时 bindings 不完全一致时，找交集最大的已有切换组。"""
        wanted = set(mat_names_tuple)
        best_key = None
        best_hit = 0
        for key in material_group_to_swapkey:
            hit = len(wanted & set(key))
            if hit > best_hit:
                best_key = key
                best_hit = hit
        return best_key

    @staticmethod
    def _parse_switch_key_parts(value):
        """``$swapkey150`` -> ``("$swapkey", 150)``（非贪婪，前缀不能吃掉数字）。"""
        match = re.match(r"^(\$\w+?)(\d+)$", str(value or "").strip())
        if not match:
            return str(value or "").strip() or "$swapkey", 0
        return match.group(1), int(match.group(2))

    def _next_free_switch_key(self, swap_key_prefix, next_swap_key_num):
        """新建切换变量时避开本节点与同蓝图其它切换节点已占用的编号。"""
        used = set()
        try:
            tree = getattr(self, "id_data", None)
            if tree is not None:
                for node in getattr(tree, "nodes", []) or []:
                    containers = []
                    for item in getattr(node, "target_items", []) or []:
                        containers.extend(list(getattr(item, "switch_groups", []) or []))
                    containers.extend(list(getattr(node, "global_switch_groups", []) or []))
                    for group in containers:
                        _p, number = self._parse_switch_key_parts(
                            getattr(group, "switch_variable", "")
                        )
                        if number > 0:
                            used.add(number)
        except Exception:
            pass
        number = int(next_swap_key_num or 0)
        while number in used:
            number += 1
        return swap_key_prefix, number

    def generate_material_lines(self, matching_materials, param_name, texture_type, obj,
                                texture_folder, all_sections,
                                object_to_diffuse_swapkey, material_group_to_swapkey,
                                swap_key_prefix, next_swap_key_num, used_swap_keys,
                                resource_name_provider=None):
        generated_lines = []
        # 细分进度：本批次要处理 len(matching_materials) 张贴图（下面每张都会进
        # ``create_resource_entry`` 上报一次），先累加进显示用的 total。
        self._plan_texture_progress(len(matching_materials or []))
        # 发光亮度写在当前逻辑对应的 FX 命名空间变量上：
        # RabbitFX/NTEMIFX 是大写 $…\brightness 同名字段，HI3FX 同样叫 brightness
        # （但它是 IniParams row230.w，语义一致）。
        brightness_param_name = fx_namespace.current_profile().param("brightness")
        # 材质名 → 工作空间里该材质原贴图的路径：导出格式照抄这份原贴图的 DDS 头。
        workspace_source_by_material = self._workspace_source_paths_for_materials(obj)

        if len(matching_materials) == 1:
            material = matching_materials[0]
            if texture_type == 'Glowmap':
                match = re.search(r'^Glowmap_(\d+)_', material.name, re.IGNORECASE)
                if match:
                    generated_lines.append(f"{brightness_param_name} = {match.group(1)}")

            resource_name = resource_name_provider(material, 0) if resource_name_provider else None
            new_line = self.create_resource_entry(
                material,
                param_name,
                texture_folder,
                all_sections,
                resource_name_override=resource_name,
                workspace_source_path=workspace_source_by_material.get(str(getattr(material, "name", "") or "")),
                dedup_group=f"{getattr(obj, 'name', '') or ''}|{texture_type}|single",
            )
            if new_line:
                generated_lines.append(new_line)

        elif len(matching_materials) > 1:
            current_swap_variable = None
            mat_names_tuple = tuple(sorted([mat.name for mat in matching_materials]))
            ordered_materials = self._order_materials_by_switch_group(
                matching_materials, mat_names_tuple
            )
            if texture_type != 'DiffuseMap' and obj.name in object_to_diffuse_swapkey:
                current_swap_variable = object_to_diffuse_swapkey[obj.name]
            else:
                if mat_names_tuple not in material_group_to_swapkey:
                    # 用户改过材质槽时，材质集合与扫描时的 bindings 不再完全一致：
                    # 按「材质名交集最大」复用已有切换组，避免新建一个变量把同一组
                    # 拆成两个（实机：身体出现 $swapkey15152，切换时该槽位不跟组走）。
                    matched_key = self._best_matching_switch_group(
                        mat_names_tuple, material_group_to_swapkey
                    )
                    if matched_key is None:
                        swap_key_prefix, next_swap_key_num = self._next_free_switch_key(
                            swap_key_prefix, next_swap_key_num
                        )
                        new_swap_key = f"{swap_key_prefix}{next_swap_key_num}"
                        material_group_to_swapkey[mat_names_tuple] = new_swap_key
                        next_swap_key_num += 1
                    else:
                        new_swap_key = material_group_to_swapkey[matched_key]
                else:
                    new_swap_key = material_group_to_swapkey[mat_names_tuple]
                current_swap_variable = new_swap_key

                if texture_type == 'DiffuseMap':
                    object_to_diffuse_swapkey[obj.name] = current_swap_variable

            used_swap_keys.add(current_swap_variable)

            generated_lines.append(f"; {texture_type} 材质切换 (组: {current_swap_variable})")
            for index, material in enumerate(ordered_materials):
                resource_name = resource_name_provider(material, index) if resource_name_provider else None
                new_line = self.create_resource_entry(
                    material,
                    param_name,
                    texture_folder,
                    all_sections,
                    resource_name_override=resource_name,
                    workspace_source_path=workspace_source_by_material.get(str(getattr(material, "name", "") or "")),
                    # 同一切换组的不同档位是刻意变体（脸妆/服装微调）：联合去重不得把它们
                    # 合并成同一张图，否则切档看起来"没反应"。
                    dedup_group=f"{getattr(obj, 'name', '') or ''}|{texture_type}|{current_swap_variable}",
                )
                if new_line:
                    generated_lines.append(f"if {current_swap_variable} == {index}")
                    if texture_type == 'Glowmap':
                        match = re.search(r'^Glowmap_(\d+)_', material.name, re.IGNORECASE)
                        if match:
                            generated_lines.append(f"    {brightness_param_name} = {match.group(1)}")
                    generated_lines.append(f"    {new_line}")
                    generated_lines.append("endif")
            generated_lines.append("")

        return generated_lines, next_swap_key_num

    def create_resource_entry(self, material, param_name, texture_folder, all_sections,
                              resource_name_override=None, workspace_source_path=None,
                              dedup_group=None):
        global _material_resource_cache

        # 细分进度：每张材质贴图开始复制/转 DDS 前先刷新状态栏文本。
        # 联合去重模式下这里只登记、不复制/不转换，进度改由落盘阶段统一上报
        # （「贴图落盘 i/total」），否则状态栏会显示并不存在的"转换中"。
        if TextureExportPlan is None or not TextureExportPlan.enabled():
            self._report_texture_progress()

        texture_image = self.get_texture_from_material(material)
        if not texture_image: return None

        # 同内容材质复用同一个 Resource 名称，避免材质后处理把同图资源反复写出。
        material_signature = self._build_material_signature(material)
        resource_name_override = str(resource_name_override or "").strip()
        cache_key = material_signature if not resource_name_override else (material_signature, resource_name_override)
        cached_entry = _material_resource_cache.get(cache_key)

        if cached_entry:
            resource_name = cached_entry.get(
                "resource_name",
                resource_name_override or f"Resource_{self._material_resource_stem(material)}",
            )
            copied_filename = self.copy_texture_file(
                texture_image,
                texture_folder,
                material,
                forced_filename=cached_entry.get("filename"),
                workspace_source_path=workspace_source_path,
                dedup_group=dedup_group,
            )
        else:
            copied_filename = self.copy_texture_file(
                texture_image, texture_folder, material,
                workspace_source_path=workspace_source_path,
                dedup_group=dedup_group,
            )
            resource_name = resource_name_override or f"Resource_{self._material_resource_stem(material)}"
            if copied_filename:
                _material_resource_cache[cache_key] = {
                    "resource_name": resource_name,
                    "filename": copied_filename,
                }

        if not copied_filename: return None
        resource_section_name = f"[{resource_name}]"
        desired_line = f"filename = Textures/{copied_filename}".replace("\\", "/")
        existing_lines = all_sections.get(resource_section_name, [])
        existing_normalized = {
            line.strip().replace("\\", "/")
            for line in existing_lines
        }
        if self.material_to_resource_override or desired_line not in existing_normalized:
            all_sections[resource_section_name] = [desired_line]
        return "{} = {}".format(param_name, resource_name) if not param_name.lower().startswith("resource\\") else "{} = ref {}".format(param_name, resource_name)

    @staticmethod
    def _is_generated_material_line(line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False

        generated_prefixes = (
            "Resource\\RabbitFX\\",
            "Resource\\RabbitFx\\",
            "Resource\\ZZMI\\",
            "Resource\\NTEMIFX\\",
            "Resource\\HI3FX\\",
            "ps-t",
            "$\\RabbitFX\\brightness",
            "$\\HI3FX\\brightness",
        )
        generated_exact = {
            "run = CommandList\\RabbitFX\\SetTextures",
            "run = CommandList\\RabbitFx\\SetTextures",
            "run = CommandList\\ZZMI\\SetTextures",
            "run = CommandList\\RabbitFX\\Run",
            "run = CommandList\\NTEMIFX\\Run",
            "run = CommandList\\HI3FX\\Run",
            "run = CommandList\\HI3FX\\Reset",
        }
        return (
            stripped in generated_exact
            or stripped.startswith(generated_prefixes)
            or "材质切换" in stripped
        )

    def _is_material_switch_if_line(self, line: str) -> bool:
        base_var = str(getattr(self, "material_switch_var", "") or "").strip()
        if not base_var:
            return False

        match = re.match(r"^if\s+(\$\w+?)(\d+)\s*==", line.strip())
        base_match = re.match(r"^(\$\w+?)(\d+)$", base_var)
        if match and base_match:
            var_prefix, var_index = match.groups()
            base_prefix, base_index = base_match.groups()
            return var_prefix == base_prefix and int(var_index) >= int(base_index)

        return line.strip().startswith(f"if {base_var} ")

    @staticmethod
    def _should_emit_fx_reset(fx_kinds, next_mesh_fx_kinds) -> bool:
        """当前 mesh 是否需要发射 FX 复位行。

        **恒为 True（2026-09 用户口径）**：每一次绘制都必须自带 FX 复位，因为作者会
        自行调整绘制顺序；"下一个绘制必然重设同名 FX"这个前提在重排后不再成立，
        一旦依赖它就会让上一个 mesh 的 FX 绑定泄漏到不该有的部件上。

        历史上这里做过"下一个 mesh 会重设同名 FX 就跳过复位"的优化（省下每模组
        ~1380 条/帧的 `CommandList\\…\\Run`），已按上述理由撤销；`next_mesh_fx_kinds`
        参数保留以兼容调用点与既有测试。
        """
        return True

    def _find_mesh_block_reset_insert_index(self, lines, mesh_start_index: int) -> int:
        search_end_idx = len(lines)
        for i in range(mesh_start_index + 1, len(lines)):
            if '[mesh:' in lines[i]:
                search_end_idx = i
                break

        draw_idx = -1
        for i in range(mesh_start_index + 1, search_end_idx):
            if 'drawindexed' in lines[i]:
                draw_idx = i
                break

        # 着色器替换模式下 drawindexed 被移到了 run = 引用的段里，
        # 此时用 run = 行作为锚点来确定插入位置。
        if draw_idx == -1:
            run_idx = -1
            for i in range(mesh_start_index + 1, search_end_idx):
                stripped = lines[i].strip()
                if stripped.startswith("run = ") or stripped.startswith("run="):
                    run_idx = i
            if run_idx == -1:
                return -1
            draw_idx = run_idx

        block_depth = 0
        last_endif_idx = -1
        for i in range(mesh_start_index + 1, search_end_idx):
            stripped = lines[i].strip()
            if stripped.startswith("if "):
                block_depth += 1
            elif stripped == "endif":
                if block_depth > 0:
                    block_depth -= 1
                last_endif_idx = i

        if last_endif_idx > draw_idx:
            return last_endif_idx + 1
        return draw_idx + 1

    def _strip_generated_material_lines(self, lines, preserved_ps_slots=None):
        cleaned_lines = []
        inside_mesh_block = False
        skipping_material_switch_block = False
        preserved_ps_slots = {
            str(slot or "").strip().lower()
            for slot in (preserved_ps_slots or [])
            if str(slot or "").strip()
        }

        for line in lines:
            if self.extract_mesh_name(line):
                inside_mesh_block = True
                skipping_material_switch_block = False
                cleaned_lines.append(line)
                continue

            stripped = line.strip()
            if inside_mesh_block and self._is_material_switch_if_line(stripped):
                skipping_material_switch_block = True
                continue

            if skipping_material_switch_block:
                if stripped == "endif":
                    skipping_material_switch_block = False
                continue

            if inside_mesh_block and self._is_generated_material_line(stripped):
                ps_match = re.match(r"^(ps-t\d+)\s*=", stripped, re.IGNORECASE)
                if ps_match and ps_match.group(1).strip().lower() in preserved_ps_slots:
                    cleaned_lines.append(line)
                    continue
                continue

            cleaned_lines.append(line)

        return cleaned_lines

    @staticmethod
    def _extract_transparency_value_from_mesh_name(mesh_name):
        match = re.search(r'_透明(\d+(\.\d+)?)', str(mesh_name or ""))
        return match.group(1) if match else None

    @staticmethod
    def _ttl_parse_drawindexed(line):
        # 原生绘制行，以及被本节点注释掉（TTL 已接管）的那一行，都要认：
        # 后者是重导出时唯一还留着的参数来源——块本身不搬走，只把绘制注释掉。
        match = re.match(
            r'^\s*;?\s*(?:\[SSMT TTL 已接管\]\s*)?drawindexed(?:instanced)?\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)',
            str(line or "").strip(),
            re.IGNORECASE,
        )
        if match:
            # TTLib 的调用约定与 drawindexed 一致：
            # _1=index count，_2=first index，_3=first vertex/base vertex。
            # ZZMI RedirectSO 会在正文前保留 3 个 stub 顶点，因此第三项
            # 不能丢弃，否则 TTL 二次绘制会从 base_vertex=0 错读 SO。
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        return None

    @staticmethod
    def _ttl_group_spans(lines):
        """返回各 TTL 组占用的行号闭区间，供 phase2 跳过组内锚点。"""
        spans = []
        start = None
        marker_prefix = _TTL_GROUP_MARKER.split("{}")[0].strip()
        for index, line in enumerate(lines):
            stripped = str(line).strip()
            if start is None and stripped.startswith(marker_prefix):
                start = index
            elif start is not None and stripped == _TTL_GROUP_END:
                spans.append((start, index))
                start = None
        if start is not None:
            spans.append((start, len(lines) - 1))
        return spans

    @staticmethod
    def _strip_ttl_groups(lines):
        """摘掉上一次导出留在段尾的 TTL 组。

        TTL 组现在跟普通块同处一个段，材质节点每次导出都会重扫这个段；不先摘掉，
        组里的 `; [mesh:]` 锚点就会被当成普通块重写一遍，参数行随之叠加。
        """
        kept = []
        skipping = False
        for line in lines:
            stripped = str(line).strip()
            if stripped.startswith(_TTL_GROUP_MARKER.split("{}")[0].strip()):
                skipping = True
                continue
            if skipping:
                if stripped == _TTL_GROUP_END:
                    skipping = False
                continue
            kept.append(line)
        lines[:] = kept

    def _ttl_inline_draws(self, block_lines, param_lines):
        """把块里的绘制行就地换成「参数 + TTL 调用」，其余行原样保留。

        TTLib 的捕获走的是 CustomShader，而 3Dmigoto 在执行 CustomShader 前后会把
        ps/vs/混合/深度/光栅/RT 整套备份再还原（CommandList.cpp 的
        RunCustomShaderCommand::run），所以调用留在这一笔绘制原来的位置是安全的：
        同一段里后面的绘制不会被上一次捕获的状态污染，也就不必整组搬到段尾。
        """
        inline = []
        for line in block_lines:
            raw = str(line)
            stripped = raw.strip()
            draw = self._ttl_parse_drawindexed(stripped)
            if draw is None:
                inline.append(line)
                continue
            indent = raw[:len(raw) - len(raw.lstrip())]
            for param in param_lines:
                inline.append(indent + str(param))
            inline.append("{}${}TTL{}_1 = {}".format(indent, chr(92), chr(92), draw[0]))
            inline.append("{}${}TTL{}_2 = {}".format(indent, chr(92), chr(92), draw[1]))
            inline.append("{}${}TTL{}_3 = {}".format(indent, chr(92), chr(92), draw[2]))
            inline.append("{}run = CommandList{}TTL{}Draw".format(indent, chr(92), chr(92)))
        return inline

    def _ttl_material_candidate_filter(self):
        """TTL 链路取物体用的过滤器：只认身上真有 TTLMap 材质的候选。

        不带过滤的解析是「先到先得」：链路分叉出来的空壳副本（没有材质）只要名字排在
        源物体前面就会被选中，TTL / EFMITTL 随即静默跳过。交给过滤器后，解析在过滤
        不过时会继续试下一个候选——也就是反向映射出来的源物体。
        """
        return lambda candidate: bool(self.find_matching_materials(candidate, "TTLMap"))

    def _process_ttl_sections(self, section_name, lines, all_sections, material_group_to_swapkey,
                              swap_key_prefix, next_swap_key_num, used_swap_keys, texture_folder):
        from ..utils.log_utils import LOG as _LOG
        # 旧版把 TTL 排在段尾；重导出时先摘掉那几组，免得和就地替换叠成两份绘制。
        self._strip_ttl_groups(lines)
        mesh_lines_info = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]
        if not mesh_lines_info:
            return next_swap_key_num

        if_stack = []
        if_endif_map = {}
        mesh_cond = {}
        for i, line in enumerate(lines):
            stripped = str(line).strip()
            if re.match(r'^if\s+', stripped, re.IGNORECASE):
                if_stack.append((i, line))
            elif stripped.casefold() == 'endif' and if_stack:
                if_idx, _if_line = if_stack.pop()
                if_endif_map[if_idx] = (i, line)
            mesh_name = self.extract_mesh_name(line)
            if mesh_name:
                mesh_cond[i] = [if_idx for if_idx, _ in if_stack]

        for mesh_index, mesh_name in reversed(mesh_lines_info):
            transparency_value = self._extract_transparency_value_from_mesh_name(mesh_name)
            obj = self.find_object_by_mesh_name(mesh_name, object_filter=self._ttl_material_candidate_filter())
            fx_materials = self.find_matching_materials(obj, "FXMap") if obj is not None else []
            ttl_materials = self.find_matching_materials(obj, "TTLMap") if obj is not None else []
            if fx_materials and ttl_materials:
                _LOG.warning(f"      TTL: 跳过 '{mesh_name}'（同一物体不能同时启用 FX 与 TTL）")
                continue
            if not ttl_materials:
                continue

            # 块的范围：本 mesh 注释到下一个 mesh 注释之间；若这个块落在 if 分支里，
            # 到所属 if 的 endif 为止（不然会把段级的条件结构切碎）。
            cond_if_indexes = mesh_cond.get(mesh_index, [])
            innermost_endif = None
            if cond_if_indexes:
                innermost_endif = if_endif_map.get(cond_if_indexes[-1], (None, None))[0]
            block_start = mesh_index
            block_end = len(lines)
            for i in range(mesh_index + 1, len(lines)):
                if self.extract_mesh_name(lines[i]):
                    block_end = i
                    break
            if innermost_endif is not None and innermost_endif < block_end:
                block_end = innermost_endif
            block_lines = lines[block_start:block_end]

            # 已经换过就原样留着：参数行就在调用上面，重导出不该再叠一份。
            if any(str(bl).strip().casefold() == _TTL_CALL_LINE.casefold() for bl in block_lines):
                continue
            if not any(self._ttl_parse_drawindexed(str(bl).strip()) is not None for bl in block_lines):
                _LOG.info(f"      TTL: 跳过 '{mesh_name}'（块内无 drawindexed）")
                continue

            # 参数紧贴调用写、落在同一个分支里：TTLib 每笔 Draw 结尾会把 alpha /
            # mask_channel / mask_invert / 遮罩 / _1.._3 全部复位，所以下一个块必须
            # 自己重新交一遍，参数也就不能提到块外。
            param_lines = []
            ttl_ref_lines, next_swap_key_num = self.generate_material_lines(
                ttl_materials, r"Resource\TTL\TransparencyTex", "TTLMap", obj, texture_folder, all_sections,
                {}, material_group_to_swapkey, swap_key_prefix, next_swap_key_num, used_swap_keys)
            param_lines.extend(ttl_ref_lines)
            param_lines.append("${}TTL{}mask_channel = 3".format(chr(92), chr(92)))
            param_lines.append("${}TTL{}mask_invert = 1".format(chr(92), chr(92)))
            alpha_value = transparency_value if transparency_value else "1.0"
            alpha_var = self._ttl_ensure_alpha_variable(alpha_value, all_sections)
            param_lines.append("${}TTL{}alpha = {}".format(chr(92), chr(92), alpha_var))
            param_lines.append("${}TTL{}V = {}".format(chr(92), chr(92), _TTL_DEFAULT_BRIGHTNESS))

            # 就地替换：块位置、mesh 注释、贴图绑定、if 结构都不动，只把这一笔绘制
            # 换成库调用。重导出时块照旧被这张节点认领；拖拽节点的 vb0 换绑也天然
            # 作用在同一段上（命令列表内部的换绑不会传给嵌套的 run）。
            lines[block_start:block_end] = self._ttl_inline_draws(block_lines, param_lines)

        return next_swap_key_num

    @staticmethod
    def _ttl_normalize_alpha(alpha_value):
        try:
            return str(float(str(alpha_value)))
        except (TypeError, ValueError):
            return str(alpha_value)

    @classmethod
    def _ttl_alpha_var_name(cls, alpha_value):
        normalized = cls._ttl_normalize_alpha(alpha_value)
        token = normalized.replace(".", "_")
        return f"$TTLAlpha{token}", normalized

    def _ttl_ensure_alpha_variable(self, alpha_value, all_sections):
        var_name, normalized = self._ttl_alpha_var_name(alpha_value)
        constants_key = '[Constants]'
        if constants_key not in all_sections:
            all_sections[constants_key] = []
        definition = f"global {var_name} = {normalized}"
        existing = {str(line).strip() for line in all_sections[constants_key]}
        if definition not in existing:
            all_sections[constants_key].append(definition)
        return var_name

    @staticmethod
    def _efmi_ttl_library_present():
        """EFMITTL 库是否躺在当前游戏的 Mods 目录里。

        没装时**不能**改写绘制：``run = CommandList\\EFMITTL\\Draw`` 找不到命令列表，
        这一笔就什么都不画，部件直接消失——比「继续按原来不透明画」糟得多。
        所以这里只做「装没装」的判断，判断为没装就原样保留绘制并在日志里点名要装什么。
        """
        try:
            mods_folder = GlobalConfig.path_mods_folder()
        except Exception:
            return False
        mods_folder = str(mods_folder or "").strip()
        if not mods_folder:
            return False
        return os.path.isfile(os.path.join(mods_folder, _EFMITTL_LIBRARY_FILE))

    @classmethod
    def _efmi_ttl_draw_args(cls, draw_args):
        """把 ``drawindexed(instanced)`` 的参数拆成 EFMITTL 要的 ``(count, first)``。

        库自己写死了 ``drawindexed = $count, $first, 0``，所以交接有三个前提，任何
        一条不满足就返回 None（宁可保持原来的不透明绘制，也不画错）：

        - 第三个数（base vertex）必须是 0，库没有 base vertex 的位置；
        - 实例数必须是 1：库只画一个实例，而 EFMI 的实例绘制会被拆成每次一个实例
          的回调，mod 里那句 ``INSTANCE_COUNT`` 就是这种形态；
        - count / first 必须是数字。
        """
        fields = [field.strip() for field in str(draw_args or "").split(",")]
        if len(fields) not in (3, 4, 5):
            return None
        if len(fields) == 3:
            # drawindexed = count, first_index, base_vertex：实例数按 1 处理。
            count, first, base_vertex = fields
            instances = "1"
        else:
            # drawindexedinstanced = count, instances, first_index, base_vertex[, first_instance]
            count, instances, first, base_vertex = fields[:4]
        if not count or not first:
            return None
        if base_vertex not in ("0", ""):
            return None
        if instances.casefold() not in _EFMITTL_SINGLE_INSTANCE_TOKENS:
            return None
        if not count.isdigit() or not first.isdigit():
            return None
        return count, first

    def _process_efmi_ttl_sections(self, section_name, lines, all_sections,
                                   material_group_to_swapkey, swap_key_prefix,
                                   next_swap_key_num, used_swap_keys, texture_folder):
        """把带 TTLMap 材质的 mesh 的绘制交给 EFMITTL 库（EFMI 专用）。

        与 ``_process_ttl_sections``（ZZMI）的根本区别：这里不新建段、也不做第二遍
        绘制，只把这一段里那一行 ``drawindexed(instanced)`` 就地换成库的调用约定：

        .. code-block:: ini

            Resource\\EFMITTL\\Mask = ref ResourceTTLMap_Body
            $\\EFMITTL\\mask_channel = 3
            $\\EFMITTL\\mask_invert = 1
            $\\EFMITTL\\count = 59679
            $\\EFMITTL\\first = 0
            run = CommandList\\EFMITTL\\Draw

        所以索引缓冲、顶点缓冲、贴图槽都还是这一笔自己的绑定，不需要搬。
        """
        from ..utils.log_utils import LOG as _LOG
        if any(_EFMITTL_DRAW_LINE.casefold() in str(line).casefold() for line in lines):
            # 这个段已经交给库了：重复导出时别把参数行叠第二遍。
            return next_swap_key_num
        if not self._efmi_ttl_library_present():
            _LOG.warning(
                "      EFMI TTL: 未找到 EFMITTL 库（"
                + os.path.join("Mods", _EFMITTL_LIBRARY_FILE)
                + "），本次保留原绘制；装好库再导出即可生成 TTL"
            )
            return next_swap_key_num

        mesh_lines_info = [(i, self.extract_mesh_name(line))
                           for i, line in enumerate(lines) if self.extract_mesh_name(line)]
        for mesh_index, mesh_name in reversed(mesh_lines_info):
            obj = self.find_object_by_mesh_name(mesh_name, object_filter=self._ttl_material_candidate_filter())
            if obj is None:
                continue
            ttl_materials = self.find_matching_materials(obj, "TTLMap")
            if not ttl_materials:
                continue
            if self.find_matching_materials(obj, "FXMap"):
                # 与 ZZMI 同一条规矩：一个物体不能同时走 FX 与半透明。
                _LOG.warning(f"      EFMI TTL: 跳过 '{mesh_name}'（同一物体不能同时启用 FX 与 TTL）")
                continue

            block_end = len(lines)
            for i in range(mesh_index + 1, len(lines)):
                if self.extract_mesh_name(lines[i]):
                    block_end = i
                    break
            draw_index = -1
            draw_match = None
            for i in range(mesh_index + 1, block_end):
                match = _EFMITTL_DRAW_RE.match(str(lines[i]))
                if match:
                    draw_index, draw_match = i, match
                    break
            if draw_index < 0:
                _LOG.info(f"      EFMI TTL: 跳过 '{mesh_name}'（块内无 drawindexed）")
                continue

            draw_args = self._efmi_ttl_draw_args(draw_match.group("args"))
            if draw_args is None:
                _LOG.info(
                    f"      EFMI TTL: 跳过 '{mesh_name}'"
                    f"（{draw_match.group('args').strip()} 不是可交接的单实例绘制）"
                )
                continue
            count, first = draw_args

            mask_lines, next_swap_key_num = self.generate_material_lines(
                ttl_materials, _EFMITTL_MASK_PARAM, "TTLMap", obj, texture_folder, all_sections,
                {}, material_group_to_swapkey, swap_key_prefix, next_swap_key_num, used_swap_keys)
            # 材质没有贴图时 generate_material_lines 只回一行注释（多材质还会回一对
            # if/endif），所以「有没有遮罩」按是否真的写出了 = ref 行来判断。
            has_mask = any("= ref " in str(line) for line in mask_lines)
            alpha_value = self._extract_transparency_value_from_mesh_name(mesh_name)
            if not has_mask and not alpha_value:
                _LOG.info(
                    f"      EFMI TTL: 跳过 '{mesh_name}'（TTLMap 材质没有贴图，也没有 _透明 数值）"
                )
                continue

            ttl_lines = []
            if has_mask:
                ttl_lines.extend(mask_lines)
                ttl_lines.append(_EFMITTL_MASK_CHANNEL_LINE)
                ttl_lines.append(_EFMITTL_MASK_INVERT_LINE)
            if alpha_value:
                # 不写 alpha 时库按「有遮罩 ⇒ 1，让遮罩决定」处理；写了就是整体乘一层。
                ttl_lines.append("$\\EFMITTL\\alpha = {}".format(self._ttl_normalize_alpha(alpha_value)))
            ttl_lines.append("$\\EFMITTL\\count = {}".format(count))
            ttl_lines.append("$\\EFMITTL\\first = {}".format(first))
            ttl_lines.append(_EFMITTL_DRAW_LINE)

            # 原绘制在 if 分支里就要跟着缩进，参数行不能跑到条件块外面去。
            indent = draw_match.group("indent") or ""
            lines[draw_index:draw_index + 1] = [
                indent + str(line) if str(line).strip() else line for line in ttl_lines
            ]
            _LOG.info(
                f"      找到 '{mesh_name}'，EFMI TTL: count={count} first={first}"
                + (f"，遮罩材质: {', '.join(str(m.name) for m in ttl_materials)}" if has_mask else "")
            )
        return next_swap_key_num

    def process_texture_override_section(self, section_name, all_sections,
                                          material_group_to_swapkey, swap_key_prefix=None, next_swap_key_num=None,
                                          used_swap_keys=None, transparency_sections_to_add=None):
        from ..utils.log_utils import LOG as _LOG
        debug_disable_fx_ttl = bool(getattr(self, "debug_disable_fx_ttl", False))
        # 本次导出用哪一套 FX 命名空间：HIMI → HI3FX，NTEMI → NTEMIFX，其余 → RabbitFX。
        fx_profile = fx_namespace.profile_for_logic(getattr(GlobalConfig, "logic_name", ""))
        if material_group_to_swapkey is None or material_group_to_swapkey is Ellipsis:
            material_group_to_swapkey = {}
        if used_swap_keys is None or used_swap_keys is Ellipsis:
            used_swap_keys = set()
        if transparency_sections_to_add is None or transparency_sections_to_add is Ellipsis:
            transparency_sections_to_add = OrderedDict()
        if swap_key_prefix is Ellipsis:
            swap_key_prefix = None
        if next_swap_key_num is Ellipsis:
            next_swap_key_num = None
        if swap_key_prefix is None or next_swap_key_num is None:
            base_swap_var = getattr(self, "material_switch_var", "") or "$swapkey0"
            match = re.match(r'(\$\w+?)(\d+)', str(base_swap_var))
            swap_key_prefix = match.group(1) if match else str(base_swap_var)
            next_swap_key_num = int(match.group(2)) if match else 0

        lines = all_sections[section_name]
        # 以前这里是段级早退：只要段里出现 TTL 的绘制就整段跳过。TTL 不再独立成段，
        # 早退会把同段其它网格的材质一起冻结，所以改成只摘掉 TTL 组、其余照常处理。
        self._strip_ttl_groups(lines)
        mesh_line = next((line for line in lines if self.extract_mesh_name(line)), "")
        section_mesh_name = self.extract_mesh_name(mesh_line) or ""
        section_obj = self.find_object_by_mesh_name(section_mesh_name) if section_mesh_name else None

        if section_mesh_name and section_obj is None:
            return next_swap_key_num

        ini_mapping = self.build_mapping_for_section(lines)
        material_candidate_filter = lambda candidate: self._object_has_matching_materials(candidate, ini_mapping)
        preserved_ps_slots = set()
        workspace_slots = OrderedDict()
        if getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False):
            if section_obj is not None:
                workspace_slots = self._collect_modimp_texture_slots(section_obj)
        diffuse_workspace_slot = self._find_first_slot_by_mark_name(workspace_slots, "DiffuseMap")
        if getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False) and diffuse_workspace_slot and diffuse_workspace_slot != "ps-t2":
            _self_alias_inserted = self._ensure_alias_assignment_in_lines(lines, diffuse_workspace_slot, "ps-t2")
            if _self_alias_inserted:
                ini_mapping["ps-t2"] = ini_mapping.get(diffuse_workspace_slot, "DiffuseMap")
                preserved_ps_slots.add("ps-t2")
        lines[:] = self._strip_generated_material_lines(lines, preserved_ps_slots=preserved_ps_slots)
        config_path = os.path.normpath(all_sections.get('_config_path', ''))
        workspace_texture_ini_folder = "Textures"
        workspace_texture_folder = os.path.join(config_path, workspace_texture_ini_folder)
        texture_folder = os.path.join(config_path, "Textures")
        object_to_diffuse_swapkey = {}

        mesh_lines_info_phase1 = [(i, self.extract_mesh_name(line)) for i, line in enumerate(lines) if self.extract_mesh_name(line)]

        for insert_index, mesh_name in reversed(mesh_lines_info_phase1):
            obj = self.find_object_by_mesh_name(mesh_name, object_filter=material_candidate_filter)
            if not obj:
                _LOG.info(f"      找到 '{mesh_name}', 所有候选均未匹配到材质")
                continue
            if not obj.material_slots:
                continue
            
            matched_types = []
            
            new_lines_for_this_mesh = []
            generated_zzmi_style, generated_glowmap, generated_fxmap = False, False, False
            generated_ttlmap = False
            generated_ps_slots = set()

            workspace_resource_by_slot = OrderedDict()
            diffuse_workspace_slot = ""
            for param_name, slot_info in self._collect_modimp_texture_slots(obj).items():
                texture_type = str(slot_info.get("mark_name", "") or "").strip()
                if not texture_type or texture_type == "FXMap":
                    continue
                if not diffuse_workspace_slot and texture_type == "DiffuseMap":
                    diffuse_workspace_slot = str(param_name or "").strip().lower()
                matching_materials = self._find_workspace_slot_materials(obj, slot_info)
                if not matching_materials:
                    continue
                matched_types.append(texture_type)
                generated_lines, next_swap_key_num = self.generate_material_lines(
                    matching_materials, param_name, texture_type, obj, workspace_texture_folder, all_sections,
                    object_to_diffuse_swapkey, material_group_to_swapkey,
                    swap_key_prefix, next_swap_key_num, used_swap_keys)
                if generated_lines:
                    new_lines_for_this_mesh.extend(generated_lines)
                    generated_ps_slots.add(param_name.lower())
                    workspace_resource_by_slot[param_name] = {
                        **dict(slot_info),
                        "resource_name": self._workspace_material_resource_name(matching_materials[0]),
                    }

            if (
                getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False)
                and diffuse_workspace_slot
                and diffuse_workspace_slot in generated_ps_slots
                and "ps-t2" not in generated_ps_slots
            ):
                diffuse_alias_lines = self._clone_generated_param_lines(
                    new_lines_for_this_mesh,
                    diffuse_workspace_slot,
                    "ps-t2",
                )
                if diffuse_alias_lines:
                    new_lines_for_this_mesh.extend(diffuse_alias_lines)
                    generated_ps_slots.add("ps-t2")
                    matched_types.append("DiffuseMap->ps-t2")

            is_pst_style = any(k.lower().startswith("ps-t") for k in ini_mapping.keys())
            is_zzmi_style = any(k.lower().startswith("resource\\zzmi\\") for k in ini_mapping.keys())
            is_fx_style = any(_starts_with_fx_resource_prefix(k) for k in ini_mapping.keys())
            # 段里真正出现过哪几套 FX 命名空间（决定要不要补 SetTextures，
            # 以及补哪一套的——各运行时的 SetTextures 不是同一个命令列表）。
            fx_profiles_in_section = []
            if is_pst_style or is_zzmi_style or is_fx_style:
                for param_name, texture_type in ini_mapping.items():
                    is_zzmi_param = param_name.lower().startswith("resource\\zzmi\\")
                    fx_param_profile = _fx_profile_for_param(param_name)
                    is_fx_param = fx_param_profile is not None

                    if not is_zzmi_param and not is_fx_param and not param_name.lower().startswith("ps-t"):
                        continue
                    if texture_type == "FXMap" and param_name.lower().startswith("ps-t"):
                        continue
                    if param_name.lower() in generated_ps_slots:
                        continue
                    matching_materials = self.find_matching_materials(obj, texture_type)
                    if matching_materials:
                        matched_types.append(texture_type)
                        if is_zzmi_param:
                            generated_zzmi_style = True
                        elif is_fx_param:
                            if fx_param_profile not in fx_profiles_in_section:
                                fx_profiles_in_section.append(fx_param_profile)
                        resource_name_provider = None
                        if param_name.lower().startswith("ps-t"):
                            resource_name_provider = lambda material, index: self._ps_texture_material_resource_name(material)
                        generated_lines, next_swap_key_num = self.generate_material_lines(
                            matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                            object_to_diffuse_swapkey, material_group_to_swapkey,
                            swap_key_prefix, next_swap_key_num, used_swap_keys,
                            resource_name_provider=resource_name_provider)
                        new_lines_for_this_mesh.extend(generated_lines)
                        if param_name.lower().startswith("ps-t"):
                            generated_ps_slots.add(param_name.lower())

            slot_materials = self._collect_ps_texture_slot_materials(obj)
            for param_name, slot_info in slot_materials.items():
                if param_name.lower() in generated_ps_slots:
                    continue
                texture_type = slot_info["texture_type"]
                matching_materials = list(slot_info["materials"].values())
                if not matching_materials:
                    continue
                matched_types.append(texture_type)
                resource_name_provider = lambda material, index: self._ps_texture_material_resource_name(material)
                generated_lines, next_swap_key_num = self.generate_material_lines(
                    matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                    object_to_diffuse_swapkey, material_group_to_swapkey,
                    swap_key_prefix, next_swap_key_num, used_swap_keys,
                    resource_name_provider=resource_name_provider)
                new_lines_for_this_mesh.extend(generated_lines)

            if (
                getattr(self, "_ntmi_modimp_extra_ps_t2_diffuse_map", False)
                and diffuse_workspace_slot
                and "ps-t2" not in generated_ps_slots
            ):
                diffuse_alias_lines = self._clone_generated_param_lines(
                    new_lines_for_this_mesh,
                    diffuse_workspace_slot,
                    "ps-t2",
                )
                if diffuse_alias_lines:
                    new_lines_for_this_mesh.extend(diffuse_alias_lines)
                    generated_ps_slots.add("ps-t2")
                    matched_types.append("DiffuseMap->ps-t2")

            fxmap_lines = []
            # 贴图类型 → 资源别名。TTLMap 是「抖动半透明」专用的第二个遮罩通道，
            # 只有当前命名空间的档案声明了它（目前只有 HI3FX）才会产出：其它运行时
            # 没有这个概念，产出这条引用只会跑一个不存在的语义。
            fxmap_texture_types = [
                ('Glowmap', fx_profile.glow_ref),
                ('FXMap', fx_profile.fxmap_ref),
            ]
            if fx_profile.ttlmap_ref:
                fxmap_texture_types.append(('TTLMap', fx_profile.ttlmap_ref))
            for texture_type, param_name in fxmap_texture_types:
                if debug_disable_fx_ttl and texture_type in ('FXMap', 'TTLMap'):
                    continue
                matching_materials = self.find_matching_materials(obj, texture_type)
                if matching_materials:
                    matched_types.append(texture_type)
                    # 命名空间按当前执行逻辑取档案：RabbitFX / NTEMIFX / HI3FX。
                    # HIMI（崩坏 3）走 HI3FX：资源别名是 Resource\HI3FX\GlowMap /
                    # FXMap / TTLMap，变量名与 Run 命令列表都跟着换
                    # （见 blueprint/fx_namespace.py）。
                    if texture_type == 'Glowmap': generated_glowmap = True
                    if texture_type == 'FXMap': generated_fxmap = True
                    if texture_type == 'TTLMap': generated_ttlmap = True
                    generated_lines, next_swap_key_num = self.generate_material_lines(
                        matching_materials, param_name, texture_type, obj, texture_folder, all_sections,
                        object_to_diffuse_swapkey, material_group_to_swapkey,
                        swap_key_prefix, next_swap_key_num, used_swap_keys)
                    fxmap_lines.extend(generated_lines)
                    # Run 行对三种贴图类型是同一条命令列表：调用/使用方法不变，
                    # 只是引用行多了一种贴图类型。
                    fxmap_lines.append(fx_profile.run_line)

            ntemifx_lines = []
            ntemifx_texture_slots = {} if debug_disable_fx_ttl else self._collect_ntemifx_texture_slots(obj, workspace_resource_by_slot)
            for slot_label, resource_name in ntemifx_texture_slots.items():
                ntemifx_lines.append(f"Resource\\NTEMIFX\\FXMap = ref {resource_name}")
                ntemifx_lines.append("run = CommandList\\NTEMIFX\\Run")
                matched_types.append(f"NTEMIFX/{slot_label}")
            if ntemifx_texture_slots:
                reset_after_draw = []
                reset_after_draw.append("Resource\\NTEMIFX\\FXMap = ref null")
                reset_after_draw.append("run = CommandList\\NTEMIFX\\Run")
                reset_insert_idx = self._find_mesh_block_reset_insert_index(lines, insert_index)
                if reset_insert_idx != -1:
                    lines[reset_insert_idx:reset_insert_idx] = reset_after_draw
            
            if matched_types:
                _LOG.info(f"      找到 '{mesh_name}', 匹配材质: {', '.join(matched_types)}")
            else:
                _LOG.info(f"      找到 '{mesh_name}', 未匹配到材质")
            
            if generated_zzmi_style: new_lines_for_this_mesh.append("run = CommandList\\ZZMI\\SetTextures")
            # SetTextures 是「语义贴图重映射」，各运行时未必都提供（HI3FX 把它放在默认
            # 不加载的 HI3FX.Remap.ini 里），所以只补段里实际出现过、且档案声明了它的
            # 那一套命名空间。
            for fx_param_profile in fx_profiles_in_section:
                if fx_param_profile.set_textures_line:
                    new_lines_for_this_mesh.append(fx_param_profile.set_textures_line)
            new_lines_for_this_mesh.extend(fxmap_lines)
            new_lines_for_this_mesh.extend(ntemifx_lines)
            lines[insert_index + 1:insert_index + 1] = new_lines_for_this_mesh
            reset_lines = []
            if generated_glowmap:
                reset_lines.append(f"{fx_profile.glow_ref} = ref null")
                if fx_profile.reset_neutralises_variables:
                    # RabbitFX/NTEMIFX 的 Run 结尾会把 $h/$s/$v/$brightness 复位，
                    # 所以「写 0 + 再 Run 一次」就是复位。
                    reset_lines.append(f"{fx_profile.param('brightness')} = 0")
            if generated_fxmap:
                reset_lines.append(f"{fx_profile.fxmap_ref} = ref null")
            if generated_ttlmap:
                reset_lines.append(f"{fx_profile.ttlmap_ref} = ref null")
            if reset_lines:
                # HI3FX 的 Run 不复位变量，必须用 Reset（它自己会 null 别名 + Clean），
                # 否则 $brightness 会一直停在 0，后续绘制全黑。
                reset_lines.append(fx_profile.reset_line or fx_profile.run_line)
                reset_insert_idx = self._find_mesh_block_reset_insert_index(lines, insert_index)
                if reset_insert_idx != -1:
                    lines[reset_insert_idx:reset_insert_idx] = reset_lines
        # 半透明生成有两套互不相干的协议，按游戏分派：
        # - ZZMI：TTLib 的「把这一笔拆成第二遍绘制」，_process_ttl_sections 就地换掉绘制行；
        # - EFMI：EFMITTL 共享库接管这一笔绘制，_process_efmi_ttl_sections 就地换掉
        #   drawindexed 并交给库（同样不新建段、不改索引/顶点缓冲绑定）。
        # 别的逻辑只做普通材质转资源与 FX/Glowmap，不得因为某种 drawindexed 参数恰好
        # 能被正则解析就误入半透明链路。
        # 别和 HI3FX 的 TTLMap 混淆：那是**第二个遮罩通道**（抖动半透明），属于普通
        # 材质转资源，由上面的 FX 段落产出，不涉及这里的绘制接管。
        logic_name = GlobalConfig.logic_name
        if debug_disable_fx_ttl:
            _LOG.info(f"      调试开关已开启，跳过 {section_name} 的 FX/TTL 生成")
        elif logic_name == LogicName.ZZMI:
            next_swap_key_num = self._process_ttl_sections(
                section_name, lines, all_sections, material_group_to_swapkey,
                swap_key_prefix, next_swap_key_num, used_swap_keys, texture_folder)
        elif logic_name == LogicName.EFMI:
            next_swap_key_num = self._process_efmi_ttl_sections(
                section_name, lines, all_sections, material_group_to_swapkey,
                swap_key_prefix, next_swap_key_num, used_swap_keys, texture_folder)

        # 旧版把 TTL 组排在本段段尾、组内自带 `; [mesh:]` 锚点。现在改成就地替换，
        # 新产物里不会有组；这里仍跳过旧工程残留的组，免得 phase2 把组内锚点当成
        # 第二个块、连组一起搬进透明段。
        ttl_group_spans = self._ttl_group_spans(lines)
        mesh_lines_info_phase2 = [
            (i, self.extract_mesh_name(line))
            for i, line in enumerate(lines)
            if self.extract_mesh_name(line)
            and not any(start <= i <= end for start, end in ttl_group_spans)
        ]

        # 段体内容 -> 段名：同一个 mesh 在一个段里会随物体切换分支出现多次，
        # 内容（材质 + drawindexed）完全一致时复用同一个透明段，不再生成
        # _2/_3… 这样内容逐字节重复的段。
        transparency_body_section_names: dict = {}
        for existing_section_name, existing_section_body in transparency_sections_to_add.items():
            transparency_body_section_names.setdefault(tuple(existing_section_body), str(existing_section_name))
        for mesh_index, mesh_name in reversed(mesh_lines_info_phase2):
            transparency_shader_name, transparency_value = self.extract_transparency_info_from_mesh_name(mesh_name)
            if transparency_shader_name:
                # TTL 接管过的块不再搬：混合已经由 TTL 库的绘制自己做，搬进透明段
                # 等于把这一笔又画一遍。就地替换后块里留着的是库调用本身。
                taken_over = False
                for i in range(mesh_index + 1, len(lines)):
                    probe = lines[i].strip()
                    if (
                        probe.startswith("; [SSMT-TTL]")
                        or probe.startswith(_TTL_DRAW_TAKEN_PREFIX.strip())
                        or probe.casefold() == _TTL_CALL_LINE.casefold()
                    ):
                        taken_over = True
                        break
                    if self.extract_mesh_name(lines[i]) or (
                        probe.startswith('[') and not probe.startswith('[mesh:')
                    ):
                        break
                if taken_over:
                    continue
                start_move_idx = mesh_index + 1
                end_move_idx = len(lines)
                conditional_depth = 0
                draw_seen = False
                for i in range(start_move_idx, len(lines)):
                    stripped = lines[i].strip()
                    if stripped.startswith("; [SSMT-TTL]"):
                        end_move_idx = i
                        break
                    if self.extract_mesh_name(lines[i]) or (
                        stripped.startswith('[') and not stripped.startswith('[mesh:')
                    ):
                        end_move_idx = i
                        break
                    if re.match(r'^if\s+', stripped, re.IGNORECASE):
                        conditional_depth += 1
                    elif stripped.casefold() == 'endif':
                        conditional_depth = max(0, conditional_depth - 1)

                    if re.match(r'^drawindexed(?:instanced)?\s*=', stripped, re.IGNORECASE):
                        draw_seen = True

                    if draw_seen and conditional_depth == 0:
                        end_move_idx = i + 1
                        break
                final_block = []
                if start_move_idx < end_move_idx:
                    block_to_move = lines[start_move_idx:end_move_idx]
                    reset_keywords = _FX_RESET_REF_LINES + _FX_RESET_PARAM_LINES
                    filtered_block_to_move = [
                        line for line in block_to_move
                        if not any(keyword in line for keyword in reset_keywords)
                    ]
                    for line in filtered_block_to_move:
                        if any(run_line in line for run_line in _FX_RUN_LINES):
                            # Run 只有在前面已经出现过资源别名时才搬过去：整段没有
                            # 别名绑定（只剩一条孤立的 Run）时它是空转，丢掉。
                            has_resource_before = any(
                                prefix in prev_line
                                for prev_line in final_block
                                for prefix in _FX_RESOURCE_PREFIXES
                            )
                            if has_resource_before:
                                final_block.append(line)
                        else:
                            final_block.append(line)
                body_lines = [
                    "blend = ADD BLEND_FACTOR INV_BLEND_FACTOR",
                    f"blend_factor[0] = {transparency_value}", f"blend_factor[1] = {transparency_value}",
                    f"blend_factor[2] = {transparency_value}", "blend_factor[3] = 1",
                    "handling = skip",
                    "; --- Start of Overridden Mesh Content ---"
                ] + final_block
                reused_section_name = transparency_body_section_names.get(tuple(body_lines))
                if reused_section_name is not None:
                    transparency_shader_name = reused_section_name
                else:
                    base_shader_name = transparency_shader_name
                    suffix = 2
                    existing_section_names = {
                        str(existing_name or "").strip().strip('[]').casefold()
                        for existing_name in all_sections
                    }
                    generated_section_names = {
                        str(existing_name or "").casefold()
                        for existing_name in transparency_sections_to_add
                    }
                    while (
                        transparency_shader_name.casefold() in generated_section_names
                        or transparency_shader_name.casefold() in existing_section_names
                    ):
                        transparency_shader_name = f"{base_shader_name}_{suffix}"
                        suffix += 1
                    transparency_sections_to_add[transparency_shader_name] = body_lines
                    transparency_body_section_names[tuple(body_lines)] = transparency_shader_name
                lines.insert(mesh_index + 1, f"run = {transparency_shader_name}")
                del lines[mesh_index + 2:end_move_idx + 1]
        return next_swap_key_num

    @staticmethod
    def _material_target_section_names(sections):
        """返回需要执行材质转换的 INI 段，包含 EFMI 合并骨骼绘制回调。

        普通导出把 ``[mesh:*]`` 与 draw 放在 ``TextureOverride`` 段内；EFMI
        合并骨骼则只在 EntryPoint 中挂载 ``Callback_Component_DrawCustom``，实际
        内容位于被引用的 ``CommandList_Draw_*``。这里只跟随该显式引用，避免把
        用户自定义或其它后处理生成的同名前缀 CommandList 误当成绘制段。
        """
        target_names = []
        seen_names = set()

        texture_override_names = [
            section_name
            for section_name in sections
            if section_name.startswith('[TextureOverride_')
        ]
        for section_name in texture_override_names:
            seen_names.add(section_name)
            target_names.append(section_name)

        section_lookup = {
            str(section_name).strip().strip('[]').casefold(): section_name
            for section_name in sections
            if str(section_name).strip().startswith('[')
            and str(section_name).strip().endswith(']')
        }
        callback_pattern = re.compile(
            r'^\s*CommandList\\EFMIv1\\Callback_Component_DrawCustom\s*=\s*'
            r'ref\s+([^;\s]+)',
            re.IGNORECASE,
        )
        for entrypoint_name in texture_override_names:
            for line in sections.get(entrypoint_name, []):
                callback_match = callback_pattern.match(str(line))
                if not callback_match:
                    continue
                referenced_name = callback_match.group(1).strip().strip('[]')
                section_name = section_lookup.get(referenced_name.casefold())
                if section_name is None or section_name in seen_names:
                    continue
                seen_names.add(section_name)
                target_names.append(section_name)

        return target_names

    def _collect_section_zzmi_slot_alias_map(self, lines):
        """汇总一个 TextureOverride 段内各部件槽位对应的 ZZMI 别名。

        SSMT/工作空间的槽位标记（mark_name）才是发光语义的权威来源：
        WengineFx / WengineFX 表示该 ps-t 槽应通过
        Resource\\ZZMI\\WengineFx 别名绑定；GlowMap 表示应通过
        Resource\\ZZMI\\GlowMap 绑定；ZglowMap 表示该贴图专用于 ZZMI 发光，
        同样通过 Resource\\ZZMI\\GlowMap 绑定，且不应再被当作 RabbitFX 发光
        重复处理。这里按 mesh 行找到对应物体并读取其工作空间槽位元数据，
        返回 {ps-tN(小写): 别名} 映射。
        """
        if GlobalConfig.logic_name != LogicName.ZZMI:
            return {}
        result = {}
        seen_params = set()
        for line in lines:
            mesh_name = self.extract_mesh_name(line)
            if not mesh_name:
                continue
            obj = self.find_object_by_mesh_name(mesh_name)
            if obj is None:
                continue
            try:
                slots = self._collect_modimp_texture_slots(obj)
            except Exception:
                slots = {}
            for param_name, slot_info in (slots or {}).items():
                key = str(param_name or "").strip().lower()
                if not key.startswith("ps-t") or key in seen_params:
                    continue
                mark_name = str(slot_info.get("mark_name", "") or "").strip().lower()
                if mark_name == "wenginefx":
                    result[key] = r"Resource\ZZMI\WengineFx"
                    seen_params.add(key)
                elif mark_name in ("glowmap", "glow", "zglowmap", "zglow"):
                    result[key] = r"Resource\ZZMI\GlowMap"
                    seen_params.add(key)
        return result

    def _normalize_zzmi_slot_lines(self, lines, slot_alias_map=None):
        """ZZMI 专用：把直接绑定 ps-tN 的发光槽位改写为资源别名。

        直接写 ps-tN = Resource... 会绕过 SlotFix 按 shader 的槽位分配。
        最终写盘前按 SSMT 标记决定别名：标记为 WengineFx / WengineFX 的槽位
        写成 Resource\\ZZMI\\WengineFx，标记为 GlowMap / ZglowMap（或资源名含
        GlowMap / ZglowMap）的槽位写成 Resource\\ZZMI\\GlowMap。
        """
        if GlobalConfig.logic_name != LogicName.ZZMI:
            return lines
        slot_alias_map = dict(slot_alias_map or {})
        converted = []
        for line in lines:
            stripped = str(line or "")
            match = re.match(
                r"^(\s*)ps-t\d+\s*=\s*(?:ref\s+)?(Resource[^\s=;]+?)\s*$",
                stripped,
                re.IGNORECASE,
            )
            if match:
                indent = match.group(1)
                resource_name = match.group(2)
                param_key = stripped.strip().split("=", 1)[0].strip().lower()
                alias = slot_alias_map.get(param_key)
                resource_lower = resource_name.lower()
                if alias is None:
                    if "wenginefx" in resource_lower:
                        alias = r"Resource\ZZMI\WengineFx"
                    elif "glowmap" in resource_lower:
                        alias = r"Resource\ZZMI\GlowMap"
                if alias is not None:
                    line = f"{indent}{alias} = ref {resource_name}"
            converted.append(line)
        return converted

    def _prewarm_material_sources(self, sections):
        if TextureExportPlan is None or not TextureExportPlan.enabled():
            return
        paths = set()
        objects = {}
        for name in self._material_target_section_names(sections):
            for line in sections[name]:
                mesh_name = self.extract_mesh_name(line)
                if not mesh_name:
                    continue
                obj = self.find_object_by_mesh_name(mesh_name)
                if obj is not None:
                    objects[obj.name] = obj
                cache = _material_lookup_cache.get() or {}
                for candidate in cache.get((id(self), mesh_name), ()):
                    objects[candidate.name] = candidate
        for obj in objects.values():
            if hasattr(self, '_is_custom_target') and not getattr(self, 'use_global_assign', False):
                if not self._is_custom_target(obj):
                    continue
            for slot in obj.material_slots:
                image = self.get_texture_from_material(slot.material)
                if image is not None and image.filepath:
                    paths.add(bpy.path.abspath(image.filepath, library=image.library))
        TextureExportPlan.prewarm_sources(sorted(paths))

    @_material_lookup_scope
    def execute_postprocess(self, mod_export_path, exporter=None):
        from ..utils.log_utils import LOG as _LOG

        self._ntmi_modimp_extra_ps_t2_diffuse_map = bool(
            getattr(exporter, "extra_ps_t2_diffuse_map", False)
        )

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            return

        _LOG.info("🔧 材质转资源节点开始执行")

        # 每次导出重新计贴图进度（多个材质转资源节点各自从 1 开始，文本自带节点前缀）
        self._reset_texture_progress()

        total_found = 0
        total_matched = 0

        for ini_file in ini_files:
            self._create_cumulative_backup(ini_file, mod_export_path)

            with open(ini_file, 'r', encoding='utf-8') as f:
                content = f.read()

            preserved_driver_content, content = self.split_anim_driver_block_content(content)
            content = self._strip_previous_transparency_sections(content)
            preserved_tail_content = ""
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)

            preamble_lines, sections = self._parse_ini_content(content)

            sections['_config_path'] = mod_export_path
            self._prewarm_material_sources(sections)

            transparency_sections_to_add = OrderedDict()
            used_swap_keys = set()
            material_group_to_swapkey = {}
            base_swap_var = self.material_switch_var
            match = re.match(r'(\$\w+?)(\d+)', base_swap_var)
            swap_key_prefix = match.group(1) if match else base_swap_var
            next_swap_key_num = int(match.group(2)) if match else 0

            for section_name in self._material_target_section_names(sections):
                next_swap_key_num = self.process_texture_override_section(
                    section_name, sections,
                    material_group_to_swapkey, swap_key_prefix, next_swap_key_num,
                    used_swap_keys, transparency_sections_to_add
                )

            del sections['_config_path']

            # 原神 GIMI ORFix 适配：只有检测到 ORFix 写法的段才会被重排，
            # 绝区零(ZZZ)/崩铁/NTEMI 等其它游戏的段落原样通过。
            _orfix_adapter = _load_gimi_orfix_adapter()
            if _orfix_adapter is not None:
                for _section_name in list(sections.keys()):
                    if not str(_section_name or "").lower().startswith("[textureoverride"):
                        continue
                    _section_lines = sections[_section_name]
                    if _orfix_adapter.uses_orfix(_section_lines):
                        sections[_section_name] = _orfix_adapter.place_orfix_runs(
                            _section_lines
                        )

            for _section_name in list(sections.keys()):
                if not str(_section_name or "").lower().startswith("[textureoverride"):
                    continue
                _section_lines = sections[_section_name]
                _slot_alias_map = self._collect_section_zzmi_slot_alias_map(_section_lines)
                sections[_section_name] = self._normalize_zzmi_slot_lines(
                    _section_lines,
                    slot_alias_map=_slot_alias_map,
                )

            self.define_swapkeys_in_sections(sections, used_swap_keys)

            new_content = self._serialize_ini_content(
                preamble_lines,
                sections,
                transparency_sections=transparency_sections_to_add,
                preserved_tail_content=preserved_tail_content,
                preserved_driver_content=preserved_driver_content,
            )

            with open(ini_file, 'w', encoding='utf-8', newline='\n') as f:
                f.write(new_content)

        _LOG.info(f"   ✅ 材质转资源节点执行完成")


class SSMTNode_PostProcess_Material(SSMTNode_PostProcess_MaterialBase):
    """材质转资源（弃用壳）。

    仅保留 bl_idname 用于加载旧版蓝图文件，避免未知节点类型导致节点被
    Blender 静默丢弃。功能已完全被「材质转资源pro」覆盖；addon 注册/文件
    加载后会自动迁移为 pro 节点（见 node_postprocess_custom_material_assign
    的 _migrate_legacy_material_nodes）。
    """
    bl_idname = 'SSMTNode_PostProcess_Material'
    bl_label = '材质转资源（已弃用）'
    bl_description = '已弃用：功能已被「材质转资源pro」完全覆盖，打开/保存后会自动迁移为 pro 节点。'

    def draw_buttons(self, context, layout):
        layout.label(
            text='已弃用：请改用「材质转资源pro」（本节点将自动迁移并转换）',
            icon='ERROR',
        )


classes = (
    MaterialPrefixItem,
    DetectedMaterialItem,
    SSMT_OT_MaterialDetectAddPrefix,
    SSMT_OT_MaterialDetectAddCustomPrefix,
    SSMT_OT_MaterialDetectRemovePrefix,
    SSMT_OT_MaterialDetect,
    SSMT_OT_MaterialDetectClear,
    SSMTNode_PostProcess_Material,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
