import bpy
import os
import glob
import re
import shutil
import struct
import tempfile
from collections import OrderedDict, Counter, defaultdict

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

from .direct_export import sync_shapekey_direct_mode
try:
    from . import deform_chain
except ImportError:  # 测试 stub 包无 __path__ 时退化为绝对导入
    from blueprint import deform_chain
from .node_postprocess_base import SSMTNode_PostProcess_Base
from .variable_registry import allocate_shape_key_variable_name, mark_variable_name_used, normalize_variable_name
from ..common.mod_path_compat import collect_base_position_resource_map
from ..common.mod_path_compat import derive_shapekey_base_resource_name
from ..common.mod_path_compat import derive_shapekey_freq_resource_name
from ..common.mod_path_compat import derive_shapekey_merged_data_resource_name
from ..common.mod_path_compat import derive_shapekey_merged_map_resource_name
from ..common.mod_path_compat import derive_shapekey_slot_map_resource_name
from ..common.mod_path_compat import derive_shapekey_slot_resource_name
from ..common.mod_path_compat import ensure_resource_alias_section
from ..common.mod_path_compat import resolve_hash_buffer_candidate
from ..common.object_prefix_helper import ObjectPrefixHelper


class ShapeKeyVariableItem(bpy.types.PropertyGroup):
    shape_key_name: bpy.props.StringProperty(name="Shape Key Name", default="") # type: ignore
    assigned_variable_name: bpy.props.StringProperty(name="Assigned Variable Name", default="") # type: ignore
    export_enabled: bpy.props.BoolProperty(
        name="导出该形态键",
        description="勾选后该形态键才会被导出；未勾选的形态键会被直接无视（不占用槽位、不生成缓冲区与INI变量）",
        default=True,
    ) # type: ignore

    def update_custom_variable_name(self, context):
        normalized = normalize_variable_name(self.custom_variable_name)
        if normalized != self.custom_variable_name:
            self.custom_variable_name = normalized
            return
        if normalized:
            mark_variable_name_used(normalized, context=context)

    custom_variable_name: bpy.props.StringProperty(
        name="Custom Variable Name",
        default="",
        update=update_custom_variable_name,
    ) # type: ignore
    drag_zone_id: bpy.props.IntProperty(
        name="拖拽区域 ID",
        description="绑定拖拽交互节点的区域 ID（与该节点区域空物体显示的编号一致）；-1 = 不绑定，沿用强度变量",
        default=-1,
        min=-1,
        max=255,
    ) # type: ignore
    drag_click_stage: bpy.props.IntProperty(
        name="点击档位",
        description="同区域内点击第 N 次时激活该形态键（1=点击一次，2=点击两次…）；拖拽节点的点击档位数按同树各形态键的最大档位自动推导",
        default=1,
        min=1,
        max=16,
    ) # type: ignore
    drag_dir_id: bpy.props.EnumProperty(
        name="驱动方向",
        description="无方向=点击时按档位直接 0/1 开关，不随鼠标位移；否则由对应方向鼠标位移驱动强度",
        items=[
            ('-1', "无方向", "点击时按档位直接 0/1 开关，不随鼠标位移"),
            ('0', "向上", "鼠标向上移动时驱动"),
            ('1', "向右", "鼠标向右移动时驱动"),
            ('2', "向下", "鼠标向下移动时驱动"),
            ('3', "向左", "鼠标向左移动时驱动"),
        ],
        default='-1',
    ) # type: ignore


class SSMT_UL_ShapeKeyVariableMappings(bpy.types.UIList):
    bl_idname = "SSMT_UL_SHAPEKEY_VARIABLE_MAPPINGS"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        del context, icon, active_data, active_propname, index
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            row = layout.row(align=True)
            row.prop(item, "export_enabled", text="")
            row.label(text=getattr(item, "shape_key_name", "") or "<未命名>", icon='SHAPEKEY_DATA')
            value_col = row.column(align=True)
            value_col.prop(item, "custom_variable_name", text="导出变量")
            assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
            value_col.label(text=f"预分配: ${assigned_name}" if assigned_name else "预分配: 未分配", icon='INFO')
            if getattr(data, "drag_drive_enabled", False):
                zone_row = row.row(align=True)
                zone_row.prop(item, "drag_zone_id", text="区域")
                zone_row.prop(item, "drag_click_stage", text="档位")
                zone_row.prop(item, "drag_dir_id", text="方向")

_name_mapping_cache = {}


def clear_name_mapping_cache():
    global _name_mapping_cache
    _name_mapping_cache.clear()
    print("[ShapeKey] 已清除名称映射缓存")


def _resolve_workspace_category_stride(unique_str, category):
    """延迟导入工作空间格式解析（避免测试桩包触发模块链导入）。"""
    from ..common.submesh_metadata import resolve_workspace_category_stride as _impl
    return _impl(unique_str, category)


def _resolve_workspace_game_type_by_prefix(prefix):
    from ..common.submesh_metadata import resolve_workspace_game_type_by_prefix as _impl
    return _impl(prefix)


def _resolve_workspace_category_elements(unique_str, category):
    from ..common.submesh_metadata import resolve_workspace_category_elements as _impl
    return _impl(unique_str, category)


# HLSL uint 系占位类型（按字节宽度占位，不参与任何读写）
_HLSL_PAD_TYPE_BY_SIZE = {4: "uint", 8: "uint2", 12: "uint3", 16: "uint4"}

# 与着色器模板一致的默认顶点布局（40 字节：pos+normal+tangent）
_DEFAULT_VERTEX_STRUCT_DEFINITION = "struct VertexAttributes {\n    float3 position;\n    float3 normal;\n    float4 tangent;\n};"


class SSMTNode_PostProcess_ShapeKey(SSMTNode_PostProcess_Base):
    INI_PREAMBLE_KEY = "__SSMT_INI_PREAMBLE__"
    bl_idname = 'SSMTNode_PostProcess_ShapeKey'
    bl_label = '形态键配置'
    bl_description = '读取分类文本，生成支持多形态叠加混合的INI配置'

    INTENSITY_START_INDEX = 100
    VERTEX_RANGE_START_INDEX = 200
    # 形态键着色器中 ShapeKeyDrive 缓冲的 SRV 寄存器（同一 dispatch 内避开
    # t50-t54 / t75+ / t99，由 INI 的 cs-t100 绑定）
    DRAG_DRIVE_REGISTER = 100
    # 点击计数缓冲的 SRV 寄存器（由 INI 的 cs-t101 绑定）
    DRAG_CLICK_COUNT_REGISTER = 101

    shapekey_variable_items: bpy.props.CollectionProperty(type=ShapeKeyVariableItem) # type: ignore
    shapekey_variable_index: bpy.props.IntProperty(default=0) # type: ignore

    @staticmethod
    def clear_cache():
        global _name_mapping_cache
        _name_mapping_cache.clear()

    use_packed_Meshess: bpy.props.BoolProperty(
        name="使用紧凑缓冲区",
        description="仅存储变化的顶点数据，大幅减小体积。需要 'numpy' 库。",
        default=True
    )
    store_deltas: bpy.props.BoolProperty(
        name="存储顶点增量",
        description="不存储完整的顶点坐标，而是存储与基础模型的差值，进一步减小体积。需要 'numpy' 库。",
        default=True
    )
    use_optimized_lookup: bpy.props.BoolProperty(
        name="优化查找性能",
        description="使用顶点FREQ索引缓冲区替代大量条件分支，显著提升GPU性能。需要 'numpy' 库。",
        default=True
    )
    merge_slot_files: bpy.props.BoolProperty(
        name="合并槽位文件",
        description="将各槽位生成的紧凑缓冲区与索引缓冲区合并为单文件，减少着色器 T 资源位占用。当前主要在紧凑模式下生效。",
        default=False
    )
    # 直出开关和同蓝图中的其他 ShapeKey 后处理节点同步，避免槽位资源生成策略不一致。
    direct_export_mode: bpy.props.BoolProperty(
        name="直出模式",
        description="启用后该节点参与直出导出，并与同类节点同步",
        default=False,
        update=sync_shapekey_direct_mode,
    )
    drag_drive_enabled: bpy.props.BoolProperty(
        name="拖拽驱动形态键",
        description="从拖拽交互节点的 ShapeKeyDrive 缓冲区读取强度（仅命中模式下命中区域并按住左键或 X，强度在 0↔1 间切换）",
        default=False,
    )

    def apply_name_mapping(self, mapping):
        global _name_mapping_cache
        _name_mapping_cache[self.name] = mapping.copy()
        print(f"[ShapeKey] 已接收名称映射: {mapping}")

    def _get_name_mapping(self):
        global _name_mapping_cache
        return _name_mapping_cache.get(self.name, {})

    def _apply_name_mapping_to_object(self, obj_name):
        mapping = self._get_name_mapping()
        if not mapping:
            return obj_name

        for old_part, new_part in mapping.items():
            if old_part in obj_name:
                obj_name = obj_name.replace(old_part, new_part)

        return obj_name

    def _iter_connected_source_object_names(self):
        from .export_helper import BlueprintExportHelper

        seen_names = set()

        def add_name(name):
            clean_name = str(name or "").strip()
            if not clean_name or clean_name in seen_names:
                return
            seen_names.add(clean_name)
            yield clean_name

        get_blueprint_model = getattr(BlueprintExportHelper, "get_current_blueprint_model", None)
        blueprint_model = get_blueprint_model() if callable(get_blueprint_model) else None
        processing_chains = getattr(blueprint_model, "processing_chains", []) if blueprint_model is not None else []
        if processing_chains:
            for chain in processing_chains:
                if not getattr(chain, "is_valid", False) or not getattr(chain, "reached_output", False):
                    continue
                for candidate_name in (
                    getattr(chain, "original_object_name", "") or "",
                    getattr(chain, "object_name", "") or "",
                    getattr(chain, "virtual_object_name", "") or "",
                    getattr(chain, "export_object_name_override", "") or "",
                ):
                    try:
                        resolved_name = ObjectPrefixHelper.resolve_source_object_name(candidate_name)
                    except Exception:
                        resolved_name = ""
                    for item in add_name(resolved_name or candidate_name):
                        yield item
                get_export_object_name = getattr(chain, "get_export_object_name", None)
                if callable(get_export_object_name):
                    try:
                        export_name = get_export_object_name() or ""
                    except Exception:
                        export_name = ""
                    try:
                        resolved_export_name = ObjectPrefixHelper.resolve_source_object_name(export_name)
                    except Exception:
                        resolved_export_name = ""
                    for item in add_name(resolved_export_name or export_name):
                        yield item
                for rename_record in getattr(chain, "rename_history", []) or []:
                    for candidate_name in (
                        rename_record.get("old_name", "") or "",
                        rename_record.get("new_name", "") or "",
                    ):
                        try:
                            resolved_name = ObjectPrefixHelper.resolve_source_object_name(candidate_name)
                        except Exception:
                            resolved_name = ""
                        for item in add_name(resolved_name or candidate_name):
                            yield item

        for node in BlueprintExportHelper.collect_connected_start_nodes(self.id_data):
            if node.bl_idname == "SSMTNode_Object_Info":
                for item in add_name(getattr(node, "object_name", "")):
                    yield item
            elif node.bl_idname == "SSMTNode_MultiFile_Export":
                for obj_item in getattr(node, "object_list", []):
                    for name in add_name(getattr(obj_item, "object_name", "")):
                        yield name

    def _ensure_shapekey_variable_item(self, shape_key_name: str):
        for item in self.shapekey_variable_items:
            if item.shape_key_name == shape_key_name:
                return item

        item = self.shapekey_variable_items.add()
        item.shape_key_name = shape_key_name
        item.assigned_variable_name = allocate_shape_key_variable_name(shape_key_name)
        item.custom_variable_name = normalize_variable_name(item.assigned_variable_name)
        return item

    def _backfill_shape_key_variable_input(self, item):
        assigned_name = normalize_variable_name(getattr(item, "assigned_variable_name", "") or "")
        custom_name = normalize_variable_name(getattr(item, "custom_variable_name", "") or "")
        if assigned_name and not custom_name:
            item.custom_variable_name = assigned_name
            return True
        return False

    def ensure_shape_key_variable_map(self, shape_key_names):
        normalized_names = sorted({str(name or "").strip() for name in shape_key_names if str(name or "").strip()})
        existing_by_name = {
            item.shape_key_name: item
            for item in self.shapekey_variable_items
            if str(getattr(item, "shape_key_name", "") or "").strip()
        }
        created_count = 0
        backfilled_count = 0

        rebuilt_items = []
        for shape_key_name in normalized_names:
            existing = existing_by_name.get(shape_key_name)
            if existing is None:
                item = self.shapekey_variable_items.add()
                item.shape_key_name = shape_key_name
                item.assigned_variable_name = allocate_shape_key_variable_name(shape_key_name)
                item.custom_variable_name = normalize_variable_name(item.assigned_variable_name)
                created_count += 1
                rebuilt_items.append(item)
            else:
                owned_names = (
                    getattr(existing, "assigned_variable_name", ""),
                    getattr(existing, "custom_variable_name", ""),
                )
                if not existing.assigned_variable_name:
                    existing.assigned_variable_name = allocate_shape_key_variable_name(
                        shape_key_name,
                        preferred=existing.custom_variable_name,
                        owned_names=owned_names,
                    )
                elif self._backfill_shape_key_variable_input(existing):
                    backfilled_count += 1
                rebuilt_items.append(existing)

        # 形态键集合未变化：即使顺序不同也不重建，避免清空区域/档位/方向等拖拽设置
        existing_names = {
            str(getattr(item, "shape_key_name", "") or "").strip()
            for item in self.shapekey_variable_items
        }
        if existing_names == set(normalized_names):
            return created_count, backfilled_count

        serialized_items = [
            {
                "shape_key_name": getattr(item, "shape_key_name", ""),
                "assigned_variable_name": getattr(item, "assigned_variable_name", ""),
                "custom_variable_name": getattr(item, "custom_variable_name", ""),
                "export_enabled": getattr(item, "export_enabled", True),
                "drag_zone_id": getattr(item, "drag_zone_id", -1),
                "drag_click_stage": getattr(item, "drag_click_stage", 1),
                "drag_dir_id": getattr(item, "drag_dir_id", "-1"),
            }
            for item in rebuilt_items
        ]

        while len(self.shapekey_variable_items) > 0:
            self.shapekey_variable_items.remove(len(self.shapekey_variable_items) - 1)

        for serialized in serialized_items:
            item = self.shapekey_variable_items.add()
            item.shape_key_name = serialized["shape_key_name"]
            item.assigned_variable_name = serialized["assigned_variable_name"]
            item.custom_variable_name = serialized["custom_variable_name"]
            item.export_enabled = serialized["export_enabled"]
            item.drag_zone_id = serialized["drag_zone_id"]
            item.drag_click_stage = serialized["drag_click_stage"]
            item.drag_dir_id = serialized["drag_dir_id"]

        return created_count, backfilled_count

    def _is_shape_key_export_enabled(self, shape_key_name) -> bool:
        """形态键未在映射列表中时默认视为勾选；仅显式取消勾选的条目才不导出。"""
        name = str(shape_key_name or "").strip()
        if not name:
            return False
        for item in getattr(self, "shapekey_variable_items", None) or []:
            if str(getattr(item, "shape_key_name", "") or "").strip() == name:
                return bool(getattr(item, "export_enabled", True))
        return True

    def get_shape_key_export_variable_name(self, shape_key_name: str) -> str:
        shape_key_name = str(shape_key_name or "").strip()
        item = self._ensure_shapekey_variable_item(shape_key_name)
        owned_names = (
            getattr(item, "assigned_variable_name", ""),
            getattr(item, "custom_variable_name", ""),
        )
        custom_name = normalize_variable_name(item.custom_variable_name)
        if custom_name:
            return f"${custom_name}"

        assigned_name = normalize_variable_name(item.assigned_variable_name)
        if not assigned_name:
            assigned_name = allocate_shape_key_variable_name(shape_key_name, owned_names=owned_names)
            item.assigned_variable_name = assigned_name
        return f"${assigned_name}"

    def _find_drag_drive_node(self):
        """在同一节点树中查找已开启形态键驱动输出的拖拽交互节点。

        四开关迁移后改读拖拽节点的 _feature_skd()（形态键联动总开关，含旧值迁移
        与消费方约束）；旧版本/测试桩无该方法时回退 enable_shapekey_drive。
        若谓词不同步，F1 关闭后形态键节点仍会绑定指向未发射资源的 t100/t101
        （3Dmigoto 加载报错/绑定失败），见 phase2/n1 §5.2 跨节点读取链。"""
        tree = getattr(self, "id_data", None)
        if tree is None:
            return None
        for node in tree.nodes:
            if (
                getattr(node, "bl_idname", "") == "SSMTNode_PostProcess_DragInteraction"
                and self._drag_node_skd_enabled(node)
            ):
                return node
        return None

    @staticmethod
    def _drag_node_skd_enabled(node):
        """读拖拽节点的形态键联动有效值；无新方法的旧节点/测试桩走旧开关。"""
        feature_skd = getattr(node, "_feature_skd", None)
        if feature_skd is not None:
            return bool(feature_skd())
        return bool(getattr(node, "enable_shapekey_drive", False))

    def _drag_shapekey_drive_resource_name(self, ini_path=None):
        """自动从同一节点树中的拖拽交互节点推导 ShapeKeyDrive 资源名。"""
        node = self._find_drag_drive_node()
        if node is None:
            return None
        try:
            ns = node._resolve_namespace(ini_path or "")
        except Exception:
            ns = ""
        return f"ResourceDragShapeKeyDrive_{ns}"

    def _drag_shapekey_click_count_resource_name(self, ini_path=None):
        """自动从同一节点树中的拖拽交互节点推导点击计数资源名。"""
        node = self._find_drag_drive_node()
        if node is None:
            return None
        try:
            ns = node._resolve_namespace(ini_path or "")
        except Exception:
            ns = ""
        return f"ResourceDragShapeKeyClickCount_{ns}"

    def _drag_drive_stage_count(self):
        """兼容入口：返回各区域档位数之和对应的段槽数中的最大档位（已废弃，仅测试/回退用）。
        找不到拖拽节点时回退 1=单档。"""
        node = self._find_drag_drive_node()
        if node is None:
            return 1
        try:
            _total, _bases, counts = node._drag_drive_buffer_layout()
            return max(1, max(counts or [1]))
        except Exception:
            return 1

    def _drag_drive_buffer_layout(self):
        """委托同树拖拽节点计算按区域独立档位的缓冲布局。
        返回 (total_slots, zone_bases, zone_stage_counts)；找不到时回退单区域单档。"""
        node = self._find_drag_drive_node()
        if node is None:
            return 0, [0], [1]
        try:
            total, bases, counts = node._drag_drive_buffer_layout()
            return total, list(bases), list(counts)
        except Exception:
            return 0, [0], [1]

    def _drag_drive_zone_ids(self, unique_names):
        """返回与 unique_names（FREQ 索引顺序）对齐的区域 ID 列表；-1 表示不绑定。"""
        zone_ids = []
        for name in unique_names:
            item = None
            for candidate in self.shapekey_variable_items:
                if candidate.shape_key_name == name:
                    item = candidate
                    break
            if item is None:
                item = self._ensure_shapekey_variable_item(name)
            zone_ids.append(int(getattr(item, "drag_zone_id", -1)))
        return zone_ids

    def _drag_drive_click_stages(self, unique_names):
        """返回与 unique_names（FREQ 索引顺序）对齐的点击档位列表（未绑定区域时用 0xFFFFFFFF 表示禁用）。"""
        stages = []
        for name in unique_names:
            item = None
            for candidate in self.shapekey_variable_items:
                if candidate.shape_key_name == name:
                    item = candidate
                    break
            if item is None:
                item = self._ensure_shapekey_variable_item(name)
            zone_id = int(getattr(item, "drag_zone_id", -1))
            try:
                stage = int(getattr(item, "drag_click_stage", 1) or 1)
            except Exception:
                stage = 1
            stages.append(stage if zone_id >= 0 else -1)
        return stages

    def _drag_drive_dirs(self, unique_names):
        """返回与 unique_names（FREQ 索引顺序）对齐的驱动方向列表。
        无方向映射为 4（每档 5 槽：0-3 方向 + 4 无方向）；未绑定区域用 -1（0xFFFFFFFF 禁用）。"""
        dirs = []
        for name in unique_names:
            item = None
            for candidate in self.shapekey_variable_items:
                if candidate.shape_key_name == name:
                    item = candidate
                    break
            if item is None:
                item = self._ensure_shapekey_variable_item(name)
            zone_id = int(getattr(item, "drag_zone_id", -1))
            try:
                dir_val = int(getattr(item, "drag_dir_id", "-1") or "-1")
            except Exception:
                dir_val = -1
            if zone_id < 0:
                dirs.append(-1)
            elif dir_val < 0:
                dirs.append(4)  # 无方向槽
            else:
                dirs.append(dir_val)
        return dirs

    def collect_blueprint_shape_key_names(self):
        from .export_helper import BlueprintExportHelper
        result = set()
        for obj_name in self._iter_connected_source_object_names():
            obj = bpy.data.objects.get(obj_name)
            if not obj:
                obj = BlueprintExportHelper._resolve_shapekey_object_in_scene(obj_name)
            if not obj or obj.type != "MESH" or not getattr(obj, "data", None):
                continue
            shape_keys = getattr(getattr(obj.data, "shape_keys", None), "key_blocks", None)
            if not shape_keys:
                continue
            for index, key_block in enumerate(shape_keys):
                if index == 0:
                    continue
                key_name = str(getattr(key_block, "name", "") or "").strip()
                if key_name:
                    result.add(key_name)
        return sorted(result)

    def draw_buttons(self, context, layout):
        layout.operator("ssmt.scan_shapekey_variables", text="预分配蓝图形态键变量", icon='FILE_REFRESH').node_name = self.name
        if self.shapekey_variable_items:
            enabled_count = sum(
                1 for item in self.shapekey_variable_items
                if getattr(item, "export_enabled", True)
            )
            box = layout.box()
            box.label(
                text=f"形态键变量映射 (导出 {enabled_count}/{len(self.shapekey_variable_items)})",
                icon='SHAPEKEY_DATA',
            )
            box.template_list(
                "SSMT_UL_SHAPEKEY_VARIABLE_MAPPINGS", "",
                self, "shapekey_variable_items",
                self, "shapekey_variable_index",
                rows=max(4, min(len(self.shapekey_variable_items), 12)),
            )

        layout.prop(self, "use_packed_Meshess")
        layout.prop(self, "store_deltas")
        layout.prop(self, "use_optimized_lookup")
        layout.prop(self, "merge_slot_files")
        layout.prop(self, "direct_export_mode")

        drive_box = layout.box()
        drive_box.label(text="拖拽驱动形态键", icon='DRIVER')
        drive_box.prop(self, "drag_drive_enabled")
        if self.drag_drive_enabled:
            drive_box.label(text="自动识别同树拖拽节点：为各形态键设置 区域 + 点击档位 + 驱动方向", icon='INFO')

        if not NUMPY_AVAILABLE:
            layout.label(text="警告: 未安装numpy库，优化功能不可用", icon='ERROR')

    def _create_safe_var_name(self, text, prefix="", existing_names=None):
        if not text:
            text = "unnamed"

        safe_text = re.sub(r'\s+', '_', text)
        safe_text = re.sub(r'[^a-zA-Z0-9_]', '', safe_text)

        if safe_text and safe_text[0].isdigit():
            safe_text = "_" + safe_text

        if not safe_text:
            safe_text = "var"

        result = f"{prefix}{safe_text}"

        if existing_names is not None:
            original_result = result
            counter = 1
            while result in existing_names:
                result = f"{original_result}_{counter}"
                counter += 1
            existing_names.add(result)

        return result

    @staticmethod
    def _normalize_ini_section_lookup_key(section_name):
        normalized_name = str(section_name or "").strip()
        if normalized_name.startswith('[') and normalized_name.endswith(']'):
            normalized_name = normalized_name[1:-1].strip()
        return normalized_name.casefold()

    def _build_ini_section_lookup(self, sections):
        section_lookup = {}
        for section_name in sections.keys():
            normalized_name = self._normalize_ini_section_lookup_key(section_name)
            if normalized_name and normalized_name not in section_lookup:
                section_lookup[normalized_name] = section_name
        return section_lookup

    @staticmethod
    def _extract_ini_assignment_value(line, key_name):
        stripped_line = str(line or "").strip()
        if '=' not in stripped_line:
            return ""
        assignment_name, value = stripped_line.split('=', 1)
        if assignment_name.strip().casefold() != str(key_name or "").strip().casefold():
            return ""
        return value.strip()

    @staticmethod
    def _extract_run_target_name(line):
        target_name = SSMTNode_PostProcess_ShapeKey._extract_ini_assignment_value(line, "run")
        if target_name.startswith('[') and target_name.endswith(']'):
            target_name = target_name[1:-1].strip()
        return target_name

    def _resolve_run_section_name(self, section_lookup, run_target_name):
        if not run_target_name:
            return ""
        return section_lookup.get(self._normalize_ini_section_lookup_key(run_target_name), "")

    @staticmethod
    def _parse_draw_command_line(stripped_line):
        stripped_line = str(stripped_line or "").strip()
        if '=' not in stripped_line:
            return None
        command_name, raw_params = stripped_line.split('=', 1)
        command_name = command_name.strip().casefold()
        if command_name not in {'drawindexed', 'drawindexedinstanced'}:
            return None

        def safe_int_parse(value):
            try:
                value = str(value or "").strip()
                if value.lstrip('-').isdigit():
                    return int(value)
            except Exception:
                pass
            return None

        parts = [part.strip() for part in raw_params.strip().split(',')]
        if command_name == 'drawindexedinstanced':
            if len(parts) < 5:
                return None
            draw_params = (
                safe_int_parse(parts[0]),
                safe_int_parse(parts[2]),
                safe_int_parse(parts[3]),
            )
        else:
            if len(parts) != 3:
                return None
            draw_params = (
                safe_int_parse(parts[0]),
                safe_int_parse(parts[1]),
                safe_int_parse(parts[2]),
            )
        return draw_params if all(value is not None for value in draw_params) else None

    @staticmethod
    def _resolve_ib_resource_path(resource_map, ib_resource_ref):
        resource_ref = str(ib_resource_ref or "").strip()
        if not resource_ref:
            return None

        if resource_ref.lower().startswith('ref '):
            resource_ref = resource_ref[4:].strip()
        if resource_ref.startswith('[') and resource_ref.endswith(']'):
            resource_ref = resource_ref[1:-1].strip()
        return resource_map.get(resource_ref.casefold())

    def _resolve_draw_command_from_section(self, sections, section_lookup, section_name, run_path=None, visited_sections=None):
        if not section_name:
            return None

        if visited_sections is None:
            visited_sections = set()
        if section_name in visited_sections:
            return None

        visited_sections = set(visited_sections)
        visited_sections.add(section_name)
        lines = sections.get(section_name, []) or []
        current_run_path = list(run_path or [])

        for line_index, line in enumerate(lines):
            draw_params = self._parse_draw_command_line(line.strip())
            if draw_params is not None:
                return {
                    "draw_params": draw_params,
                    "draw_section_name": section_name,
                    "draw_line_index": line_index,
                    "run_path": current_run_path,
                }

            run_target_name = self._extract_run_target_name(line)
            if not run_target_name:
                continue

            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue

            result = self._resolve_draw_command_from_section(
                sections,
                section_lookup,
                target_section_name,
                run_path=current_run_path + [(section_name, line_index)],
                visited_sections=visited_sections,
            )
            if result is not None:
                return result

        return None

    def _resolve_draw_command_from_mesh_block(self, sections, section_lookup, section_name, lines, mesh_line_index, block_end_index):
        for line_index in range(mesh_line_index + 1, block_end_index):
            draw_params = self._parse_draw_command_line(lines[line_index].strip())
            if draw_params is not None:
                return {
                    "draw_params": draw_params,
                    "draw_section_name": section_name,
                    "draw_line_index": line_index,
                    "run_path": [],
                }

            run_target_name = self._extract_run_target_name(lines[line_index])
            if not run_target_name:
                continue

            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue

            result = self._resolve_draw_command_from_section(
                sections,
                section_lookup,
                target_section_name,
                run_path=[(section_name, line_index)],
                visited_sections={section_name},
            )
            if result is not None:
                return result

        return None

    def _resolve_ib_path_from_section(self, sections, section_lookup, resource_map, section_name, visited_sections=None):
        if not section_name:
            return None

        if visited_sections is None:
            visited_sections = set()
        if section_name in visited_sections:
            return None

        visited_sections = set(visited_sections)
        visited_sections.add(section_name)
        lines = sections.get(section_name, []) or []

        for line in lines:
            ib_resource_ref = self._extract_ini_assignment_value(line, "ib")
            ib_path = self._resolve_ib_resource_path(resource_map, ib_resource_ref)
            if ib_path:
                return ib_path

        for line in lines:
            run_target_name = self._extract_run_target_name(line)
            if not run_target_name:
                continue

            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue

            ib_path = self._resolve_ib_path_from_section(
                sections,
                section_lookup,
                resource_map,
                target_section_name,
                visited_sections=visited_sections,
            )
            if ib_path:
                return ib_path

        return None

    def _resolve_ib_path_from_anchor(self, sections, section_lookup, resource_map, section_name, anchor_line_index):
        lines = sections.get(section_name, []) or []
        anchor_line_index = min(max(int(anchor_line_index or 0), 0), len(lines))

        for line_index in range(anchor_line_index - 1, -1, -1):
            stripped_line = lines[line_index].strip()
            if stripped_line.startswith('if ') or stripped_line == 'endif':
                continue

            ib_resource_ref = self._extract_ini_assignment_value(stripped_line, "ib")
            ib_path = self._resolve_ib_resource_path(resource_map, ib_resource_ref)
            if ib_path:
                return ib_path

            run_target_name = self._extract_run_target_name(stripped_line)
            if not run_target_name:
                continue

            target_section_name = self._resolve_run_section_name(section_lookup, run_target_name)
            if not target_section_name:
                continue

            ib_path = self._resolve_ib_path_from_section(
                sections,
                section_lookup,
                resource_map,
                target_section_name,
                visited_sections={section_name},
            )
            if ib_path:
                return ib_path

        return None

    def _parse_ini_for_draw_info(self, sections, base_path):
        draw_info, resource_map = {}, {}
        section_lookup = self._build_ini_section_lookup(sections)
        for section_name, lines in sections.items():
            if section_name.lower().startswith('[resource'):
                filename = next(
                    (
                        value
                        for value in (
                            self._extract_ini_assignment_value(line, "filename")
                            for line in lines
                        )
                        if value
                    ),
                    None,
                )
                if filename:
                    resource_name = self._normalize_ini_section_lookup_key(section_name)
                    resource_map[resource_name] = os.path.join(base_path, filename.replace('/', os.sep))

        print(f"  [DEBUG] resource_map keys: {list(resource_map.keys())}")

        for section_name, lines in sections.items():
            if not section_name.lower().startswith('[textureoverride'):
                continue

            print(f"  [DEBUG] processing section: {section_name}")
            for mesh_line_index, line in enumerate(lines):
                stripped_line = line.strip()
                mesh_match = re.search(r'\[mesh:([^\]]+)\]', stripped_line)
                if not mesh_match:
                    continue

                current_mesh_name = mesh_match.group(1).strip()
                print(f"    [DEBUG] found mesh comment: '{stripped_line}' -> name: '{current_mesh_name}'")

                block_end_index = len(lines)
                for next_index in range(mesh_line_index + 1, len(lines)):
                    if re.search(r'\[mesh:([^\]]+)\]', lines[next_index].strip()):
                        block_end_index = next_index
                        break

                resolved_draw = self._resolve_draw_command_from_mesh_block(
                    sections,
                    section_lookup,
                    section_name,
                    lines,
                    mesh_line_index,
                    block_end_index,
                )
                if resolved_draw is None:
                    print(f"    [WARNING] draw command not found, skip mesh: '{current_mesh_name}'")
                    continue

                draw_params = resolved_draw.get("draw_params")
                draw_section_name = resolved_draw.get("draw_section_name", "")
                draw_line_index = resolved_draw.get("draw_line_index", -1)
                run_path = list(resolved_draw.get("run_path", []) or [])

                ib_path = None
                if run_path:
                    ib_path = self._resolve_ib_path_from_anchor(
                        sections,
                        section_lookup,
                        resource_map,
                        draw_section_name,
                        draw_line_index,
                    )
                    if not ib_path:
                        for anchor_section_name, anchor_line_index in reversed(run_path):
                            ib_path = self._resolve_ib_path_from_anchor(
                                sections,
                                section_lookup,
                                resource_map,
                                anchor_section_name,
                                anchor_line_index,
                            )
                            if ib_path:
                                break
                else:
                    ib_path = self._resolve_ib_path_from_anchor(
                        sections,
                        section_lookup,
                        resource_map,
                        draw_section_name,
                        draw_line_index,
                    )

                if not ib_path:
                    print(f"    [WARNING] IB path not found, skip mesh: '{current_mesh_name}'")
                    continue

                print(f"      [DEBUG] resolved IB path: '{ib_path}'")
                print(
                    f"    [DEBUG] draw parsed: mesh={current_mesh_name}, "
                    f"index_count={draw_params[0]}, start_index_location={draw_params[1]}, "
                    f"base_vertex_location={draw_params[2]}"
                )
                draw_info.setdefault(current_mesh_name, []).append({
                    'draw_params': draw_params,
                    'ib_path': ib_path,
                })

        return draw_info

    def _calculate_vertex_range(self, ib_path, draw_params):
        index_count, start_index_location, base_vertex_location = draw_params
        print(f"    [DEBUG] _calculate_vertex_range: ib_path={ib_path}, index_count={index_count}, start_index_location={start_index_location}, base_vertex_location={base_vertex_location}")
        
        if not os.path.isfile(ib_path):
            print(f"    [WARNING] IB 文件不存在: {ib_path}")
            return None, None
        try:
            file_size = os.path.getsize(ib_path)
            print(f"    [DEBUG] IB 文件大小: {file_size} 字节")
            
            with open(ib_path, 'rb') as f:
                seek_pos = start_index_location * 4
                read_size = index_count * 4
                print(f"    [DEBUG] seek 位置: {seek_pos}, 读取大小: {read_size}")
                
                if seek_pos >= file_size:
                    print(f"    [WARNING] seek 位置 {seek_pos} 超出文件大小 {file_size}")
                    return None, None
                
                f.seek(seek_pos)
                data = f.read(read_size)
                if len(data) < read_size:
                    print(f"    [WARNING] 读取数据不足: 期望 {read_size}, 实际 {len(data)}")
                    return None, None
                # 烘焙表（freq_indices/merged_map 等）按**静态 VB 行**索引，
                # 而 drawindexed 的 base_vertex 是运行时 SO 缓冲的前缀偏移
                # （合并骨骼重定向生成的 IB 已预移 base，静态行号 = 裸索引值）。
                # 若把 base_vertex 加进来，每个物体的烘焙范围会整体平移 base 行，
                # 跨物体边界互相盖章（base=3 时后一个物体头部 3 行被前一个物体
                # 的动画槽位覆盖 → 运行时那 3 个顶点跟随错误动画漂移）。
                indices = list(struct.unpack(f'<{index_count}I', data))
                
                min_idx = min(indices) if indices else None
                max_idx = max(indices) if indices else None
                print(f"    [DEBUG] 计算结果: min_index={min_idx}, max_index={max_idx}")
                
                return (min_idx, max_idx) if indices else (None, None)
        except Exception as e:
            print(f"    [ERROR] 计算顶点范围时出错: {e}")
            import traceback
            traceback.print_exc()
            return None, None

    def _extract_hash_from_name(self, obj_name):
        prefix_info = ObjectPrefixHelper.extract_prefix_info(obj_name)
        if prefix_info:
            prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])
            bare_unique_str = str(prefix_parts.get("bare_unique_str", "") or "").strip()
            if bare_unique_str:
                return bare_unique_str

        match = re.match(r'^([a-f0-9]{8}-[a-f0-9]+(?:-[a-f0-9]+)?)', obj_name)
        if match:
            return match.group(1)
        match = re.match(r'^([a-f0-9]{8})', obj_name)
        if match:
            return match.group(1)
        return None

    def _extract_alias_from_name(self, obj_name):
        prefix_info = ObjectPrefixHelper.extract_prefix_info(obj_name)
        if prefix_info:
            _prefix, _separator, base_name = ObjectPrefixHelper.split_name_and_prefix(
                obj_name,
                prefix_info[0],
                prefix_info[1],
            )
            return base_name if base_name else obj_name

        obj_hash = self._extract_hash_from_name(obj_name)
        if obj_hash:
            remainder = obj_name[len(obj_hash):]
            alias = remainder.lstrip('.').lstrip('_')
            return alias if alias else obj_name
        return obj_name

    @staticmethod
    def _strip_runtime_copy_suffix(name):
        stripped = re.sub(r'(_(?:chain|dup|copy|BPE)\d*)+$', '', name, flags=re.IGNORECASE)
        return stripped

    @staticmethod
    def _strip_object_suffix(name):
        # Only remove explicitly-defined runtime suffixes.
        # Do not strip Blender numeric suffixes such as ".001" or any other custom suffix.
        stripped = re.sub(r'(_(?:chain|dup|copy|BPE)\d*)+$', '', name, flags=re.IGNORECASE)
        return stripped

    def _get_merge_identity_alias(self, obj_name):
        alias = self._extract_alias_from_name(obj_name)
        return self._strip_runtime_copy_suffix(alias)

    def _merge_identical_ranges(self, calculated_ranges):
        range_to_objects = OrderedDict()
        for obj_name, (start_v, end_v, ib_path) in calculated_ranges.items():
            if start_v is None:
                continue
            merge_identity = self._get_merge_identity_alias(obj_name)
            key = (start_v, end_v, ib_path, merge_identity)
            if key not in range_to_objects:
                range_to_objects[key] = []
            range_to_objects[key].append(obj_name)

        merged_ranges = OrderedDict()
        old_to_new = {}

        for (start_v, end_v, ib_path, merge_identity), obj_names in range_to_objects.items():
            if len(obj_names) == 1:
                merged_ranges[obj_names[0]] = (start_v, end_v)
                old_to_new[obj_names[0]] = obj_names[0]
            else:
                merge_aliases = []
                hash_prefix_for_group = None
                for n in obj_names:
                    obj_hash = self._extract_hash_from_name(n)
                    if obj_hash and hash_prefix_for_group is None:
                        hash_prefix_for_group = obj_hash
                    merge_aliases.append(self._get_merge_identity_alias(n))
                counter = Counter(merge_aliases)
                base_name = counter.most_common(1)[0][0]
                if hash_prefix_for_group:
                    merged_name = f"{hash_prefix_for_group}.{base_name}_x{len(obj_names)}"
                else:
                    merged_name = f"{base_name}_x{len(obj_names)}"
                merged_ranges[merged_name] = (start_v, end_v)
                for n in obj_names:
                    old_to_new[n] = merged_name
                print(
                    f"  [MERGE] 合并 {len(obj_names)} 个相同范围 ({start_v}-{end_v})、相同 IB、"
                    f"相同运行时对象 '{merge_identity}' 的物体 -> '{merged_name}'"
                )
                print(f"    IB: {os.path.basename(ib_path)}")
                for n in obj_names:
                    print(f"    - {n}")

        return merged_ranges, old_to_new

    def _extract_hash_prefix(self, hash_val):
        if hash_val:
            prefix_parts = ObjectPrefixHelper.parse_prefix_parts(hash_val)
            draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
            if draw_ib:
                return draw_ib
            normalized_hash_val = str(hash_val or "").strip()
            if normalized_hash_val.upper().startswith("LOD") and "." in normalized_hash_val:
                normalized_hash_val = normalized_hash_val.split(".", 1)[1]
            return normalized_hash_val.split('-')[0]
        return None

    def _hash_to_resource_prefix(self, h):
        bare_unique_str = str(ObjectPrefixHelper.parse_prefix_parts(h).get("bare_unique_str", "") or h or "").strip()
        return bare_unique_str.replace('.', '_').replace('-', '_')

    def _should_merge_slot_files(self, use_packed=None):
        if use_packed is None:
            use_packed = self.use_packed_Meshess
        return bool(getattr(self, "merge_slot_files", False) and use_packed)

    def _resolve_hash_buffer_path(self, mod_export_path, folder_name, hash_val, file_suffix, preferred_hashes=None):
        h_prefix = self._extract_hash_prefix(hash_val)
        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(hash_val)
        unique_str_candidate = prefix_parts.get("unique_str", "")
        candidate_hashes = []
        for candidate in list(preferred_hashes or []) + [hash_val, unique_str_candidate, h_prefix]:
            normalized_candidate = str(candidate or "").strip()
            if normalized_candidate and normalized_candidate not in candidate_hashes:
                candidate_hashes.append(normalized_candidate)

        folder_path = os.path.join(mod_export_path, folder_name)
        return resolve_hash_buffer_candidate(
            folder_path,
            hash_val,
            file_suffix,
            preferred_hashes=candidate_hashes,
        )

    def _resolve_position_buffer_path(self, mod_export_path, folder_name, hash_val, preferred_hashes=None):
        return self._resolve_hash_buffer_path(
            mod_export_path,
            folder_name,
            hash_val,
            "-Position.buf",
            preferred_hashes=preferred_hashes,
        )

    def _resolve_map_buffer_path(self, mod_export_path, folder_name, hash_val, preferred_hashes=None):
        return self._resolve_hash_buffer_path(
            mod_export_path,
            folder_name,
            hash_val,
            "-Position_map.buf",
            preferred_hashes=preferred_hashes,
        )

    def _get_merged_data_file_suffix(self, use_delta):
        return "_merged_packed_pos_delta" if use_delta else "_merged_packed"

    def _get_merged_data_resource_name(self, hash_val, use_delta):
        resource_suffix = "_Merged_PackedPosDelta" if use_delta else "_Merged_Packed"
        return f"Resource_{self._hash_to_resource_prefix(hash_val)}_Position{resource_suffix}"

    def _get_merged_map_resource_name(self, hash_val):
        return f"Resource_{self._hash_to_resource_prefix(hash_val)}_Position_Merged_Map"

    @staticmethod
    def _compute_dispatch_group_count(vertex_count, threads_per_group=16):
        vertex_count = int(vertex_count or 0)
        threads_per_group = max(1, int(threads_per_group or 1))
        return max(1, (vertex_count + threads_per_group - 1) // threads_per_group)

    def _parse_classification_text_final(self, text_content):
        slot_to_name_to_objects, hash_to_objects, all_objects = OrderedDict(), OrderedDict(), []
        current_slot, current_shapekey_name = None, None
        for line in text_content.splitlines():
            line = line.strip()
            if not line or line.startswith('#'): continue
            slot_match = re.search(r'槽位\s*(\d+):', line)
            if slot_match:
                current_slot = int(slot_match.group(1))
                if current_slot not in slot_to_name_to_objects: slot_to_name_to_objects[current_slot] = OrderedDict()
                current_shapekey_name = None; continue
            name_match = re.search(r'名称:\s*(.+)', line)
            if name_match and current_slot is not None:
                candidate_name = name_match.group(1).strip()
                if not self._is_shape_key_export_enabled(candidate_name):
                    # 未勾选导出的形态键直接无视：不登记名称，其后的物体行也一并跳过
                    current_shapekey_name = None
                    continue
                current_shapekey_name = candidate_name
                if current_shapekey_name not in slot_to_name_to_objects[current_slot]: slot_to_name_to_objects[current_slot][current_shapekey_name] = []
                continue
            obj_match = re.search(r'物体:\s*(.+)', line)
            if obj_match and current_slot is not None and current_shapekey_name is not None:
                obj_name = obj_match.group(1).strip()
                if obj_name not in slot_to_name_to_objects[current_slot][current_shapekey_name]:
                    slot_to_name_to_objects[current_slot][current_shapekey_name].append(obj_name)
                if obj_name not in all_objects: all_objects.append(obj_name)
                obj_hash = self._extract_hash_from_name(obj_name)
                if obj_hash:
                    if obj_hash not in hash_to_objects: hash_to_objects[obj_hash] = []
                    if obj_name not in hash_to_objects[obj_hash]: hash_to_objects[obj_hash].append(obj_name)
        unique_hashes = list(OrderedDict.fromkeys(h for obj in all_objects if (h := self._extract_hash_from_name(obj))))
        return slot_to_name_to_objects, unique_hashes, hash_to_objects, all_objects

    @staticmethod
    def parse_vertex_struct(struct_definition):
        if not struct_definition or not struct_definition.strip():
            return None

        TYPE_SIZES = {
            'float': 4,
            'float2': 8,
            'float3': 12,
            'float4': 16,
            'int': 4,
            'int2': 8,
            'int3': 12,
            'int4': 16,
            'uint': 4,
            'uint2': 8,
            'uint3': 12,
            'uint4': 16,
            'half': 2,
            'half2': 4,
            'half3': 6,
            'half4': 8,
            'double': 8,
            'double2': 16,
            'double3': 24,
            'double4': 32,
        }

        total_bytes = 0
        total_floats = 0
        attributes = []
        unrecognized_types = set()

        lines = struct_definition.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('//') or line.startswith('/*') or line.startswith('*'):
                continue

            line = line.rstrip(';').strip()

            parts = line.split()
            if len(parts) >= 2:
                type_name = parts[0]
                var_name = parts[1].rstrip(';')

                if type_name in TYPE_SIZES:
                    byte_size = TYPE_SIZES[type_name]
                    total_bytes += byte_size
                    total_floats += byte_size // 4
                    attributes.append({'type': type_name, 'name': var_name, 'size': byte_size})
                elif type_name.lower() != 'struct' and not line.endswith('{') and not line.endswith('}'):
                    unrecognized_types.add(type_name)

        if unrecognized_types:
            print(f"警告: 发现未识别的顶点属性类型: {', '.join(unrecognized_types)}")

        if total_bytes == 0:
            print(f"警告: 无法解析顶点结构体定义，total_bytes为0")
            return None

        if not attributes:
            print(f"警告: 未找到有效的顶点属性")
            return None

        return (total_bytes, total_floats, attributes)


    def _get_workspace_position_stride(self, hash_val):
        """按哈希/IB 从工作空间解析 Position 类别字节步长（每个 IB 独立，支持并存）。

        工作空间 SubmeshJson 的 CategoryBufferList 记录了每个 IB 的真实布局
        （如 9 个 IB 的 Position=16 字节、1 个 IB 的 Position=40 字节），
        这里优先以它为准，替代用户手填的单一「顶点属性定义」。
        """
        cache_key = str(hash_val or "").strip()
        if not cache_key:
            return 0
        cache = getattr(self, "_workspace_stride_cache", None)
        if cache is None:
            cache = {}
            try:
                self._workspace_stride_cache = cache
            except Exception:
                pass
        if cache_key in cache:
            return cache[cache_key]
        stride = 0
        try:
            stride = _resolve_workspace_category_stride(cache_key, "Position")
            if stride <= 0:
                game_type = _resolve_workspace_game_type_by_prefix(self._extract_hash_prefix(cache_key))
                if game_type is not None:
                    stride = int((getattr(game_type, "CategoryStrideDict", {}) or {}).get("Position", 0) or 0)
        except Exception:
            stride = 0
        cache[cache_key] = int(stride)
        return cache[cache_key]

    def _detect_vertex_format(self, base_bytes, shapekey_bytes, struct_definition=None, preferred_stride=None, hash_val=None):
        # 1) 显式步长（直出路径 runtime_infos 的 position_stride 已按工作空间解析）
        if preferred_stride and int(preferred_stride) > 0 and int(preferred_stride) % 4 == 0 and len(base_bytes) % int(preferred_stride) == 0:
            VERTEX_STRIDE = int(preferred_stride)
            NUM_FLOATS_PER_VERTEX = VERTEX_STRIDE // 4
            num_vertices = len(base_bytes) // VERTEX_STRIDE
            print(f"使用工作空间步长: {VERTEX_STRIDE}字节, 每顶点{NUM_FLOATS_PER_VERTEX}个float, 顶点数={num_vertices}")
            return (VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX, num_vertices)

        # 2) 按 hash 从工作空间解析（每个 IB 独立的真实格式，支持 16/40 等异构并存；
        #    不再被用户手填的顶点属性定义统一覆盖）
        if hash_val:
            workspace_stride = self._get_workspace_position_stride(hash_val)
            if workspace_stride > 0 and workspace_stride % 4 == 0 and len(base_bytes) % workspace_stride == 0:
                VERTEX_STRIDE = int(workspace_stride)
                NUM_FLOATS_PER_VERTEX = VERTEX_STRIDE // 4
                num_vertices = len(base_bytes) // VERTEX_STRIDE
                print(f"从工作空间解析哈希 {hash_val} 的 Position 步长: {VERTEX_STRIDE}字节, 每顶点{NUM_FLOATS_PER_VERTEX}个float, 顶点数={num_vertices}")
                return (VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX, num_vertices)
            if workspace_stride > 0:
                print(f"警告: 哈希 {hash_val} 工作空间 Position 步长 {workspace_stride} 与缓冲区大小 {len(base_bytes)} 不整除，回退到结构体定义")

        # 3) 结构体定义（顶点属性定义节点 / 默认 40B）作为回退
        if struct_definition and struct_definition.strip():
            parsed = self.parse_vertex_struct(struct_definition)
            if parsed:
                VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX, attributes = parsed
                if len(base_bytes) % VERTEX_STRIDE == 0:
                    num_vertices = len(base_bytes) // VERTEX_STRIDE
                    print(f"使用结构体定义: 步长={VERTEX_STRIDE}字节, 每顶点{NUM_FLOATS_PER_VERTEX}个float, 顶点数={num_vertices}")
                    return (VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX, num_vertices)
                print(f"警告: 结构体定义步长 {VERTEX_STRIDE}与缓冲区大小 {len(base_bytes)} 不整除，使用默认值")
            else:
                print(f"警告: 结构体定义解析失败，使用默认值")

        # 4) 默认 40 字节（与原逻辑一致），带整除性回退，避免静默错位
        VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX = 40, 10
        if len(base_bytes) % VERTEX_STRIDE != 0:
            for candidate_stride in (16, 12, 8):
                if len(base_bytes) % candidate_stride == 0:
                    VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX = candidate_stride, candidate_stride // 4
                    break
        num_vertices = len(base_bytes) // VERTEX_STRIDE
        print(f"使用默认值: 步长={VERTEX_STRIDE}字节, 每顶点{NUM_FLOATS_PER_VERTEX}个float, 顶点数={num_vertices}")
        return (VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX, num_vertices)

    def _process_shapekey_Meshess(self, mod_export_path, slot_to_name_to_objects, hash_to_stride):
        use_packed = self.use_packed_Meshess
        use_delta = self.store_deltas
        merge_slot_files = self._should_merge_slot_files(use_packed)

        if not NUMPY_AVAILABLE:
            print("Numpy库未找到，无法执行缓冲区优化。")
            return False, {}

        if getattr(self, "merge_slot_files", False) and not use_packed:
            print("文件合并模式当前仅在紧凑缓冲区模式下生效，将回退到旧版逐槽位文件模式。")

        print(
            f"开始处理缓冲区 (紧凑:{'是' if use_packed else '否'}, "
            f"增量(仅位置):{'是' if use_delta else '否'}, 文件合并:{'是' if merge_slot_files else '否'})..."
        )

        if merge_slot_files:
            return self._process_merged_shapekey_Meshess(mod_export_path, slot_to_name_to_objects, hash_to_stride)

        hash_to_actual_file_hash = {}
        Meshess_to_process = set()
        for slot, names_data in slot_to_name_to_objects.items():
            for obj in [o for name, objs in names_data.items() for o in objs]:
                h = self._extract_hash_from_name(obj)
                if h: Meshess_to_process.add((h, slot))

        print(f"  [DEBUG] 需要处理 {len(Meshess_to_process)} 个缓冲区组合")

        for h, slot in sorted(list(Meshess_to_process)):
            h_prefix = self._extract_hash_prefix(h)
            preferred_actual_hash = hash_to_actual_file_hash.get(h)
            base_path, resolved_actual_hash = self._resolve_position_buffer_path(
                mod_export_path,
                "Meshes0000",
                h,
                preferred_hashes=[preferred_actual_hash] if preferred_actual_hash else None,
            )
            actual_hash = preferred_actual_hash or resolved_actual_hash

            print(f"  [DEBUG] 尝试查找基础文件: {base_path}")
            if os.path.exists(base_path):
                print(f"    找到基础文件: {os.path.basename(base_path)}")
            else:
                print(f"    [WARNING] 找不到基础文件: {base_path}")

            folder_name = f"Meshes1{slot:03d}"
            shapekey_path, shapekey_actual_hash = self._resolve_position_buffer_path(
                mod_export_path,
                folder_name,
                h,
                preferred_hashes=[actual_hash, h],
            )

            print(f"  [DEBUG] 尝试查找形态键文件: {shapekey_path}")
            if os.path.exists(shapekey_path):
                print(f"    找到形态键文件: {os.path.basename(shapekey_path)}")
                if shapekey_actual_hash != actual_hash:
                    print(
                        f"    [WARNING] 槽位 {slot} 使用的形态键文件哈希与基础解析哈希不一致: "
                        f"{shapekey_actual_hash} != {actual_hash}，输出仍将统一写为 {actual_hash}"
                    )
            else:
                print(f"    [WARNING] 找不到形态键文件: {shapekey_path}")

            output_dir = os.path.join(mod_export_path, folder_name)

            print(f"  处理槽位 {slot} (哈希: {h}, 实际文件哈希: {actual_hash}, 前缀: {h_prefix})...")
            if not all(os.path.exists(p) for p in [base_path, shapekey_path]):
                print(f"    -> 跳过：找不到基础或形态键文件 for hash {h}, slot {slot}")
                print(f"       基础路径: {base_path} (存在: {os.path.exists(base_path)})")
                print(f"       形态键路径: {shapekey_path} (存在: {os.path.exists(shapekey_path)})")
                continue

            hash_to_actual_file_hash[h] = actual_hash

            os.makedirs(output_dir, exist_ok=True)

            try:
                with open(base_path, 'rb') as f: base_bytes = f.read()
                with open(shapekey_path, 'rb') as f: shapekey_bytes = f.read()
                if len(base_bytes) != len(shapekey_bytes):
                    print(f"    -> 跳过：文件大小不匹配 for hash {h}, slot {slot}")
                    continue

                struct_definition = self._get_vertex_struct_definition()
                VERTEX_STRIDE, NUM_FLOATS_PER_VERTEX, num_vertices = self._detect_vertex_format(
                    base_bytes,
                    shapekey_bytes,
                    struct_definition,
                    hash_val=h,
                )
                print(f"    -> 检测到格式: 步长={VERTEX_STRIDE}字节, 每顶点{NUM_FLOATS_PER_VERTEX}个float, 顶点数={num_vertices}")

                if h_prefix not in hash_to_stride:
                    hash_to_stride[h_prefix] = VERTEX_STRIDE

                base_data = np.frombuffer(base_bytes, dtype='f').reshape((num_vertices, NUM_FLOATS_PER_VERTEX))
                shapekey_data = np.frombuffer(shapekey_bytes, dtype='f').reshape((num_vertices, NUM_FLOATS_PER_VERTEX))

                output_prefix = os.path.join(output_dir, f"{actual_hash}-Position")

                if use_delta:
                    data_to_write = shapekey_data[:, :3] - base_data[:, :3]
                    filename_suffix = "_pos_delta"
                    if use_packed: filename_suffix = "_packed_pos_delta"

                    pos_diff_mask = ~np.isclose(base_data[:, :3], shapekey_data[:, :3], atol=1e-6).all(axis=1)
                    num_active_vertices = np.sum(pos_diff_mask)

                    if num_active_vertices == 0:
                        print(f"    -> 无位置差异，生成空文件。")
                        if use_packed:
                            open(f"{output_prefix}{filename_suffix}.buf", 'wb').close()
                            open(f"{output_prefix}_map.buf", 'wb').close()
                        else:
                            open(f"{output_prefix}{filename_suffix}.buf", 'wb').close()
                        continue

                    if use_packed:
                        packed_data = data_to_write[pos_diff_mask]
                        data_path = f"{output_prefix}{filename_suffix}.buf"
                        with open(data_path, 'wb') as f: f.write(packed_data.tobytes())

                        index_map = np.full(num_vertices, -1, dtype=np.int32)
                        index_map[pos_diff_mask] = np.arange(num_active_vertices, dtype=np.int32)
                        map_path = f"{output_prefix}_map.buf"
                        with open(map_path, 'wb') as f: f.write(index_map.tobytes())
                        print(f"    -> 成功生成: {os.path.basename(data_path)} 和 {os.path.basename(map_path)}")
                    else:
                        data_path = f"{output_prefix}{filename_suffix}.buf"
                        with open(data_path, 'wb') as f: f.write(data_to_write.tobytes())
                        print(f"    -> 成功生成: {os.path.basename(data_path)}")

                elif use_packed:
                    filename_suffix = "_packed"
                    diff_mask = ~np.isclose(base_data, shapekey_data, atol=1e-6).all(axis=1)
                    num_active_vertices = np.sum(diff_mask)

                    if num_active_vertices == 0:
                        print(f"    -> 无差异，生成空文件。")
                        open(f"{output_prefix}{filename_suffix}.buf", 'wb').close()
                        open(f"{output_prefix}_map.buf", 'wb').close()
                        continue

                    packed_data = shapekey_data[diff_mask]
                    data_path = f"{output_prefix}{filename_suffix}.buf"
                    with open(data_path, 'wb') as f: f.write(packed_data.tobytes())

                    index_map = np.full(num_vertices, -1, dtype=np.int32)
                    index_map[diff_mask] = np.arange(num_active_vertices, dtype=np.int32)
                    map_path = f"{output_prefix}_map.buf"
                    with open(map_path, 'wb') as f: f.write(index_map.tobytes())
                    print(f"    -> 成功生成: {os.path.basename(data_path)} 和 {os.path.basename(map_path)}")
                else:
                    print(f"    -> 标准模式，使用原始形态键文件。")
                    pass
            except Exception as e:
                print(f"    -> 处理时出错: {e}")
                return False, {}

        print("缓冲区处理完成。")
        return True, hash_to_actual_file_hash

    def _process_merged_shapekey_Meshess(self, mod_export_path, slot_to_name_to_objects, hash_to_stride):
        use_delta = self.store_deltas
        hash_to_actual_file_hash = {}
        hash_to_slots = OrderedDict()

        for slot, names_data in slot_to_name_to_objects.items():
            for obj in [o for name, objs in names_data.items() for o in objs]:
                h = self._extract_hash_from_name(obj)
                if not h:
                    continue
                if h not in hash_to_slots:
                    hash_to_slots[h] = set()
                hash_to_slots[h].add(slot)

        print(f"  [DEBUG] 需要处理 {len(hash_to_slots)} 个哈希的合并缓冲区")

        struct_definition = self._get_vertex_struct_definition()
        output_dir = os.path.join(mod_export_path, "Meshes0000")
        os.makedirs(output_dir, exist_ok=True)

        for h, slots in hash_to_slots.items():
            sorted_slots = sorted(slots)
            h_prefix = self._extract_hash_prefix(h)
            base_path, base_actual_hash = self._resolve_position_buffer_path(mod_export_path, "Meshes0000", h)

            print(f"  [DEBUG] 合并处理哈希 {h}，槽位: {sorted_slots}")
            if not os.path.exists(base_path):
                print(f"    -> 跳过：找不到基础文件 {base_path}")
                continue

            try:
                with open(base_path, 'rb') as f:
                    base_bytes = f.read()
            except Exception as e:
                print(f"    -> 读取基础文件失败: {e}")
                return False, {}

            actual_hash = base_actual_hash
            base_data = None
            num_vertices = None
            num_floats_per_vertex = None
            vertex_stride = None
            num_slots = max(sorted_slots) if sorted_slots else 0
            merged_index_map = None
            merged_data_parts = []
            next_global_index = 0
            processed_slot_count = 0

            for slot in sorted_slots:
                folder_name = f"Meshes1{slot:03d}"
                shapekey_path, slot_actual_hash = self._resolve_position_buffer_path(
                    mod_export_path,
                    folder_name,
                    h,
                    preferred_hashes=[actual_hash, h],
                )
                print(f"    [DEBUG] 槽位 {slot} 文件: {shapekey_path}")

                if not os.path.exists(shapekey_path):
                    print(f"      -> 跳过：找不到形态键文件 {shapekey_path}")
                    continue

                try:
                    with open(shapekey_path, 'rb') as f:
                        shapekey_bytes = f.read()
                except Exception as e:
                    print(f"      -> 读取形态键文件失败: {e}")
                    return False, {}

                if len(base_bytes) != len(shapekey_bytes):
                    print(f"      -> 跳过：文件大小不匹配 (base={len(base_bytes)}, shapekey={len(shapekey_bytes)})")
                    continue

                if base_data is None:
                    vertex_stride, num_floats_per_vertex, num_vertices = self._detect_vertex_format(
                        base_bytes,
                        shapekey_bytes,
                        struct_definition,
                        hash_val=h,
                    )
                    print(
                        f"      -> 检测到格式: 步长={vertex_stride}字节, "
                        f"每顶点{num_floats_per_vertex}个float, 顶点数={num_vertices}"
                    )

                    if h_prefix not in hash_to_stride:
                        hash_to_stride[h_prefix] = vertex_stride

                    base_data = np.frombuffer(base_bytes, dtype='f').reshape((num_vertices, num_floats_per_vertex))
                    merged_index_map = np.full((num_vertices, num_slots), -1, dtype=np.int32)

                shapekey_data = np.frombuffer(shapekey_bytes, dtype='f').reshape((num_vertices, num_floats_per_vertex))

                if use_delta:
                    data_to_write = shapekey_data[:, :3] - base_data[:, :3]
                    diff_mask = ~np.isclose(base_data[:, :3], shapekey_data[:, :3], atol=1e-6).all(axis=1)
                else:
                    data_to_write = shapekey_data
                    diff_mask = ~np.isclose(base_data, shapekey_data, atol=1e-6).all(axis=1)

                active_count = int(np.sum(diff_mask))
                if active_count > 0:
                    packed_data = data_to_write[diff_mask]
                    merged_data_parts.append(packed_data)

                    slot_index_map = np.full(num_vertices, -1, dtype=np.int32)
                    slot_index_map[diff_mask] = np.arange(next_global_index, next_global_index + active_count, dtype=np.int32)
                    merged_index_map[:, slot - 1] = slot_index_map
                    next_global_index += active_count
                else:
                    print(f"      -> 槽位 {slot} 无有效差异，将写入空映射列。")

                if slot_actual_hash and slot_actual_hash != actual_hash:
                    print(
                        f"      [WARNING] 槽位 {slot} 使用的形态键文件哈希与基础解析哈希不一致: "
                        f"{slot_actual_hash} != {actual_hash}，输出仍将统一写为 {actual_hash}"
                    )
                processed_slot_count += 1

            if base_data is None or merged_index_map is None:
                print(f"    -> 跳过：哈希 {h} 没有可用的槽位文件")
                continue

            data_suffix = self._get_merged_data_file_suffix(use_delta)
            data_path = os.path.join(output_dir, f"{actual_hash}-Position{data_suffix}.buf")
            map_path = os.path.join(output_dir, f"{actual_hash}-Position_merged_map.buf")

            if merged_data_parts:
                merged_data = np.concatenate(merged_data_parts, axis=0)
            else:
                empty_width = 3 if use_delta else num_floats_per_vertex
                merged_data = np.empty((0, empty_width), dtype=np.float32)

            with open(data_path, 'wb') as f:
                f.write(merged_data.astype(np.float32, copy=False).tobytes())

            with open(map_path, 'wb') as f:
                f.write(merged_index_map.reshape(-1).tobytes())

            hash_to_actual_file_hash[h] = actual_hash
            print(
                f"    -> 合并完成: {os.path.basename(data_path)} + {os.path.basename(map_path)} "
                f"(槽位数={num_slots}, 实际处理槽位={processed_slot_count}, 全局索引数={next_global_index})"
            )

        print("合并缓冲区处理完成。")
        return True, hash_to_actual_file_hash

    def _read_ini_to_ordered_dict(self, ini_file_path):
        sections = OrderedDict([(self.INI_PREAMBLE_KEY, [])])
        current_section = None
        preserved_tail_content = ""
        preserved_driver_content = ""

        try:
            with open(ini_file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            preserved_driver_content, content = self.split_anim_driver_block_content(content)
            content, preserved_tail_content = self.split_auto_appended_tail_content(content)
            if preserved_tail_content:
                print("[ShapeKey] 检测到自动追加尾块，将保留")

            for line in content.splitlines():
                stripped_line = line.strip()
                if stripped_line.startswith('[') and stripped_line.endswith(']'):
                    current_section = stripped_line
                    if current_section not in sections:
                        sections[current_section] = []
                    continue
                if current_section is None:
                    sections[self.INI_PREAMBLE_KEY].append(line)
                    continue
                if not stripped_line:
                    continue
                if current_section:
                    sections[current_section].append(line)
        except Exception as e:
            raise RuntimeError(f"读取INI文件失败: {e}") from e
        if not sections[self.INI_PREAMBLE_KEY]:
            del sections[self.INI_PREAMBLE_KEY]
        return sections, preserved_tail_content, preserved_driver_content

    def _write_ordered_dict_to_ini(self, sections, ini_file_path, preserved_tail_content="", preserved_driver_content=""):
        temp_path = None
        file_descriptor = None
        try:
            target_path = os.path.abspath(ini_file_path)
            target_stat = os.stat(target_path)
            file_descriptor, temp_path = tempfile.mkstemp(
                dir=os.path.dirname(target_path),
                prefix=f".{os.path.basename(target_path)}.",
                suffix=".tmp",
            )
            file_object = os.fdopen(file_descriptor, 'w', encoding='utf-8', newline='\n')
            file_descriptor = None
            with file_object as f:
                if preserved_driver_content:
                    f.write(preserved_driver_content)
                    if not preserved_driver_content.endswith(chr(10)):
                        f.write(chr(10))
                    f.write(chr(10))
                for section_name, lines in sections.items():
                    if section_name == self.INI_PREAMBLE_KEY:
                        for line in lines:
                            f.write(line + '\n')
                        if lines and str(lines[-1]).strip():
                            f.write('\n')
                        continue
                    if section_name.startswith(';;'):
                        f.write(section_name + '\n')
                    else:
                        f.write(section_name + '\n')
                    for line in lines:
                        f.write(line + '\n')
                    f.write('\n')

                if preserved_tail_content:
                    f.write('\n')
                    f.write(preserved_tail_content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_path, target_stat.st_mode)
            os.replace(temp_path, target_path)
            temp_path = None
        except Exception as e:
            raise RuntimeError(f"写入INI文件失败: {e}") from e
        finally:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _get_vertex_count(self, sections, hash_value):
        for section_name, lines in sections.items():
            if f"override_vertex_count" in section_name:
                for line in lines:
                    if line.strip().startswith("override_vertex_count"):
                        try:
                            return int(line.split('=')[1].strip())
                        except (ValueError, IndexError):
                            pass
        return None

    def _get_vertex_attrs_node(self):
        # 「顶点属性定义」节点已下线：工作空间按 IB 动态提供格式。此处仅兼容旧
        # 蓝图文件——残留节点可能已是未定义类型，必须探测方法存在再使用。
        def _as_vertex_attrs(candidate):
            if (
                candidate is not None
                and getattr(candidate, "bl_idname", "") == 'SSMTNode_PostProcess_VertexAttrs'
                and callable(getattr(candidate, "get_vertex_struct_definition", None))
            ):
                return candidate
            return None

        if not self.inputs[0].is_linked:
            return None

        source_node = self.inputs[0].links[0].from_node
        matched = _as_vertex_attrs(source_node)
        if matched is not None:
            return matched

        if source_node.inputs[0].is_linked:
            prev_node = source_node.inputs[0].links[0].from_node
            return _as_vertex_attrs(prev_node)

        return None

    def _get_shader_template_name(self):
        use_packed = self.use_packed_Meshess
        use_delta = self.store_deltas
        use_optimized = self.use_optimized_lookup
        merge_slot_files = self._should_merge_slot_files(use_packed)

        if merge_slot_files:
            if use_delta:
                return "shapekey_anim_packed_delta_v5_merged.hlsl"
            return "shapekey_anim_packed_v5_merged.hlsl"

        if use_optimized and use_delta and use_packed:
            return "shapekey_anim_packed_delta_v4_optimized.hlsl"
        elif use_delta and use_packed:
            return "shapekey_anim_packed_delta_v3.hlsl"
        elif use_delta:
            return "shapekey_anim_standard_delta_v3.hlsl"
        elif use_packed:
            return "shapekey_anim_packed.hlsl"
        else:
            return "shapekey_anim_standard.hlsl"

    def _get_shader_source_path(self):
        try:
            addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            asset_source_dir = os.path.join(addon_dir, "Toolset")
            shader_template_name = self._get_shader_template_name()
            shader_source_path = os.path.join(asset_source_dir, shader_template_name)
            return shader_source_path
        except Exception as e:
            print(f"获取着色器模板路径时出错: {e}")
            return None

    def _get_workspace_vertex_struct_definition(self, hash_val):
        """按工作空间游戏类型为指定哈希合成 VertexAttributes struct。

        形态键着色器模板按固定字段名读写：position（float3 算术）、normal（float3
        算术，仅非增量模板）、tangent（.xyz，仅非增量模板）。StructuredBuffer 的
        行宽又必须与实际 -Position.buf 一致——同一工作空间内 16/40 字节 IB 并存，
        因此 struct 必须逐 IB 从工作空间合成，而非用单一手填定义。

        合成规则（保持模板字段算术可编译）：
        - POSITION 12B -> float3 position；16B -> float3 position + float position_w
        - NORMAL  12B -> float3 normal；16B -> float3 normal + float normal_w
        - TANGENT 16B -> float4 tangent；12B -> float3 tangent
        - 其它元素（如 4B 压缩法线）-> uint 系占位（delta 模板不触碰这些字段；
          standard/packed 非增量模板需要 float3 normal / float4 tangent，布局
          不满足时放弃合成并告警）

        Returns:
            合成出的 struct 文本；不可合成时返回 None。
        """
        workspace_stride = self._get_workspace_position_stride(hash_val)
        if workspace_stride <= 0:
            return None

        elements = _resolve_workspace_category_elements(str(hash_val or "").strip(), "Position")
        if not elements:
            game_type = _resolve_workspace_game_type_by_prefix(self._extract_hash_prefix(hash_val))
            if game_type is None:
                return None
            elements = [
                element
                for element in getattr(game_type, "D3D11ElementList", []) or []
                if str(getattr(element, "Category", "") or "").upper() == "POSITION"
            ]
        if not elements:
            return None

        lines = []
        total_bytes = 0
        pad_index = 0
        has_float3_normal = False
        has_float4_tangent = False
        for element in elements:
            semantic = str(getattr(element, "SemanticName", "") or "").upper()
            byte_width = int(getattr(element, "ByteWidth", 0) or 0)
            if byte_width <= 0:
                return None
            if semantic == "POSITION" and byte_width == 12:
                lines.append("    float3 position;")
            elif semantic == "POSITION" and byte_width == 16:
                lines.append("    float3 position;")
                lines.append("    float position_w;")
            elif semantic == "NORMAL" and byte_width == 12:
                lines.append("    float3 normal;")
                has_float3_normal = True
            elif semantic == "NORMAL" and byte_width == 16:
                lines.append("    float3 normal;")
                lines.append("    float normal_w;")
                has_float3_normal = True
            elif semantic == "TANGENT" and byte_width == 16:
                lines.append("    float4 tangent;")
                has_float4_tangent = True
            elif semantic == "TANGENT" and byte_width == 12:
                lines.append("    float3 tangent;")
            else:
                field_type = _HLSL_PAD_TYPE_BY_SIZE.get(byte_width)
                if field_type is None:
                    return None
                lines.append(f"    {field_type} _pad{pad_index};")
                pad_index += 1
            total_bytes += byte_width

        if total_bytes != int(workspace_stride):
            return None
        if not any(line.strip() == "float3 position;" for line in lines):
            return None

        if not (has_float3_normal and has_float4_tangent):
            print(
                f"[ShapeKey] 哈希 {hash_val} 的工作空间布局（步长 {workspace_stride}）不含 "
                f"float3 normal / float4 tangent，合成 struct 仅适用于增量(delta)模板；"
                f"standard/packed 非增量模板仍需要完整 40B 布局。"
            )

        return "struct VertexAttributes {\n" + "\n".join(lines) + "\n};"

    def _get_vertex_struct_definition(self, hash_val=None):
        # 1) 工作空间合成（推荐）：与实际 -Position.buf 行宽逐 IB 一致
        workspace_struct = None
        workspace_stride = 0
        if hash_val:
            try:
                workspace_struct = self._get_workspace_vertex_struct_definition(hash_val)
                workspace_stride = self._get_workspace_position_stride(hash_val)
            except Exception:
                workspace_struct = None
        if workspace_struct:
            return workspace_struct

        # 2) 旧蓝图兼容：历史上连接的顶点属性定义节点（节点已下线，仅兼容旧文件）
        vertex_attrs_node = self._get_vertex_attrs_node()
        if vertex_attrs_node:
            struct_definition = vertex_attrs_node.get_vertex_struct_definition()
            # 着色器 struct 的字节数必须与实际 -Position.buf 行宽一致；
            # 当用户手填的顶点属性定义与工作空间真实步长不一致时（如定义 16 字节、
            # 实际 40 字节），继续用它替换 struct 会让 shader 读位错乱（游戏内顶点
            # 爆炸）。此时改用与模板一致的默认布局并显式告警。
            if hash_val and workspace_stride > 0:
                try:
                    parsed = self.parse_vertex_struct(struct_definition)
                    if parsed is not None and int(parsed[0]) != int(workspace_stride):
                        print(
                            f"[ShapeKey][警告] 顶点属性定义步长 {int(parsed[0])} 与工作空间实际步长 "
                            f"{int(workspace_stride)} 不一致（哈希 {hash_val}），着色器将改用工作空间默认布局。"
                            f"建议直接移除顶点属性定义节点，让格式完全跟随工作空间。"
                        )
                        return _DEFAULT_VERTEX_STRUCT_DEFINITION
                except Exception:
                    pass
            return struct_definition

        return _DEFAULT_VERTEX_STRUCT_DEFINITION

    def _update_shader_file(self, shader_path, hash_slot_data, use_packed, use_delta, unique_names, unique_objects, use_optimized=False, merge_slot_files=False, drag_drive_enabled=False, drag_zone_ids=None, drag_click_stages=None, drag_stage_count=1, drag_dirs=None, hash_val=None):
        try:
            with open(shader_path, 'r', encoding='utf-8') as f:
                content = f.read()

            vertex_struct = self._get_vertex_struct_definition(hash_val=hash_val)
            if vertex_struct:
                content = re.sub(r"struct VertexAttributes\s*\{[^}]*\};", vertex_struct, content, flags=re.DOTALL)

            name_to_freq_def = {name: f"FREQ{i+1}" for i, name in enumerate(unique_names)}
            obj_to_range_defs = {obj: (f"START{i+1}", f"END{i+1}") for i, obj in enumerate(unique_objects)}

            zone_ids = list(drag_zone_ids or []) if drag_drive_enabled else []
            click_stages = list(drag_click_stages or []) if drag_drive_enabled else []
            drag_dirs = list(drag_dirs or []) if drag_drive_enabled else []
            # 按区域独立段布局：每区域 4 方向槽 + 该区域档位数 N 个无方向槽；
            # 每个绑定项直接生成绝对槽位（CPU 前缀和），读取时无需再算全局 stage 索引。
            _total_slots, _zone_bases, _zone_stage_counts = self._drag_drive_buffer_layout()
            zone_bases = list(_zone_bases)
            define_lines = [f"// --- Shared Animation Intensity (per Shape Key Name) ---\n// From index {self.INTENSITY_START_INDEX} onwards"]
            if drag_drive_enabled:
                define_lines.append(f"Buffer<float> ShapeKeyDrive : register(t{self.DRAG_DRIVE_REGISTER});")
                define_lines.append(f"Buffer<uint> ShapeKeyClickCount : register(t{self.DRAG_CLICK_COUNT_REGISTER});")
                if any(zone >= 0 for zone in zone_ids):
                    ids_text = ", ".join(str(zone) if zone >= 0 else "0xFFFFFFFFu" for zone in zone_ids)
                    define_lines.append(f"static const uint SHAPEKEY_ZONE_IDS[{len(zone_ids)}] = {{ {ids_text} }};")
                    stage_list = list(click_stages) if len(click_stages) == len(zone_ids) else [1] * len(zone_ids)
                    dir_list = list(drag_dirs) if len(drag_dirs) == len(zone_ids) else [4] * len(zone_ids)
                    nd_stage_ids = []
                    slot_ids = []
                    for idx, zone in enumerate(zone_ids):
                        if zone < 0 or zone >= len(zone_bases):
                            nd_stage_ids.append(0xFFFFFFFF)
                            slot_ids.append(0xFFFFFFFF)
                            continue
                        d = dir_list[idx] if idx < len(dir_list) else 4
                        if d >= 0 and d < 4:
                            nd_stage_ids.append(0xFFFFFFFF)
                            slot_ids.append(zone_bases[zone] + d)
                        else:
                            stage = max(1, stage_list[idx] if idx < len(stage_list) else 1)
                            nd_stage_ids.append(stage)
                            slot_ids.append(zone_bases[zone] + 4 + (stage - 1))
                    nd_text = ", ".join(str(v) if v >= 0 else "0xFFFFFFFFu" for v in nd_stage_ids)
                    define_lines.append(f"static const uint SHAPEKEY_ND_STAGE_IDS[{len(zone_ids)}] = {{ {nd_text} }};")
                    slot_text = ", ".join(str(v) if v >= 0 else "0xFFFFFFFFu" for v in slot_ids)
                    define_lines.append(f"static const uint SHAPEKEY_SLOT_IDS[{len(zone_ids)}] = {{ {slot_text} }};")
                else:
                    define_lines.append("static const uint SHAPEKEY_ZONE_IDS[1] = { 0xFFFFFFFFu };")
                    define_lines.append("static const uint SHAPEKEY_ND_STAGE_IDS[1] = { 0xFFFFFFFFu };")
                    define_lines.append("static const uint SHAPEKEY_SLOT_IDS[1] = { 0xFFFFFFFFu };")
            for i, name in enumerate(unique_names):
                zone = zone_ids[i] if i < len(zone_ids) else -1
                if drag_drive_enabled and zone >= 0:
                    nd_stage = nd_stage_ids[i] if i < len(nd_stage_ids) else 0xFFFFFFFF
                    slot_id = slot_ids[i] if i < len(slot_ids) else 0xFFFFFFFF
                    define_lines.append(
                        f"#define FREQ{i+1} (SHAPEKEY_ND_STAGE_IDS[{i}] == 0xFFFFFFFFu || ShapeKeyClickCount[SHAPEKEY_ZONE_IDS[{i}]] == SHAPEKEY_ND_STAGE_IDS[{i}]"
                        f" ? ShapeKeyDrive[SHAPEKEY_SLOT_IDS[{i}]] : 0.0) // {name} (zone {zone}, slot {slot_id})"
                    )
                else:
                    define_lines.append(f"#define FREQ{i+1} IniParams[{self.INTENSITY_START_INDEX + i}].x // {name}")

            if not use_optimized:
                define_lines.extend([f"\n// --- Per-Object Vertex Ranges ---\n// From index {self.VERTEX_RANGE_START_INDEX} onwards"])
                for i, obj_name in enumerate(unique_objects):
                    start_idx = self.VERTEX_RANGE_START_INDEX + i * 2
                    define_lines.append(f"#define START{i+1} (uint)IniParams[{start_idx}].x // {obj_name}")
                    define_lines.append(f"#define END{i+1}   (uint)IniParams[{start_idx + 1}].x")

            logic_lines = []

            if merge_slot_files:
                logic_lines.append("    // V5 merged mode: all slot buffers are packed into a single data buffer and a single index buffer")
                logic_lines.append(f"    uint num_slots = {max(hash_slot_data.keys()) if hash_slot_data else 0};")

                for slot_num, names_data in sorted(hash_slot_data.items()):
                    slot_index = slot_num - 1
                    logic_lines.append(f"    // --- Slot {slot_index} (merged) ---")
                    logic_lines.append(f"    uint packed_idx_slot{slot_index} = i * num_slots + {slot_index};")

                    if use_optimized:
                        logic_lines.append(f"    uint freq_idx_slot{slot_index} = vertex_freq_indices[packed_idx_slot{slot_index}];")
                        logic_lines.append(f"    if (freq_idx_slot{slot_index} != 255)")
                        logic_lines.append("    {")
                        if drag_drive_enabled:
                            logic_lines.append(f"        float anim_weight_slot{slot_index} = IniParams[{self.INTENSITY_START_INDEX} + freq_idx_slot{slot_index}].x;")
                            logic_lines.append(f"        uint sk_zone_slot{slot_index} = SHAPEKEY_ZONE_IDS[freq_idx_slot{slot_index}];")
                            logic_lines.append(f"        uint sk_nd_stage_slot{slot_index} = SHAPEKEY_ND_STAGE_IDS[freq_idx_slot{slot_index}];")
                            logic_lines.append(f"        uint sk_slot_slot{slot_index} = SHAPEKEY_SLOT_IDS[freq_idx_slot{slot_index}];")
                            logic_lines.append(f"        if (sk_zone_slot{slot_index} != 0xFFFFFFFFu && (sk_nd_stage_slot{slot_index} == 0xFFFFFFFFu || ShapeKeyClickCount[sk_zone_slot{slot_index}] == sk_nd_stage_slot{slot_index}))")
                            logic_lines.append("        {")
                            logic_lines.append(f"            anim_weight_slot{slot_index} = ShapeKeyDrive[sk_slot_slot{slot_index}];")
                            logic_lines.append("        }")
                        else:
                            logic_lines.append(
                                f"        float anim_weight_slot{slot_index} = IniParams[{self.INTENSITY_START_INDEX} + freq_idx_slot{slot_index}].x;"
                            )
                    else:
                        is_first_if = True
                        logic_lines.append(f"    float anim_weight_slot{slot_index} = 0.0;")
                        for name, objects in names_data.items():
                            for obj in objects:
                                start_def, end_def = obj_to_range_defs[obj]
                                if_cmd = "if" if is_first_if else "else if"
                                logic_lines.append(
                                    f"    {if_cmd} (i >= {start_def} && i <= {end_def}) {{ anim_weight_slot{slot_index} = {name_to_freq_def[name]}; }} // Name: {name}"
                                )
                                is_first_if = False
                        logic_lines.append(f"    if (anim_weight_slot{slot_index} > 1e-5)")
                        logic_lines.append("    {")

                    logic_lines.append(f"        int packed_index = merged_shapekey_indices[packed_idx_slot{slot_index}];")
                    logic_lines.append("        if (packed_index != -1)")
                    logic_lines.append("        {")

                    if use_delta:
                        calc_line = f"total_diff_position += merged_shapekey_pos_deltas[packed_index] * anim_weight_slot{slot_index};"
                    else:
                        calc_line = f"total_diff_position += (merged_shapekeys[packed_index].position - base[i].position) * anim_weight_slot{slot_index};"

                    logic_lines.append("            " + calc_line)
                    logic_lines.append("        }")
                    logic_lines.append("    }\n")
            elif use_optimized:
                logic_lines.append("    // Optimized: Direct FREQ index lookup instead of hundreds of if-else branches")
                logic_lines.append(f"    uint num_slots = {max(hash_slot_data.keys()) if hash_slot_data else 0};")
                for slot_num, names_data in sorted(hash_slot_data.items()):
                    slot_index = slot_num - 1
                    logic_lines.extend([f"    // --- Slot {slot_index} (t{51+slot_index}) ---"])
                    logic_lines.append(f"    uint packed_idx_slot{slot_index} = i * num_slots + {slot_index};")
                    logic_lines.append(f"    uint freq_idx_slot{slot_index} = vertex_freq_indices[packed_idx_slot{slot_index}];")
                    logic_lines.append(f"    if (freq_idx_slot{slot_index} != 255)")
                    logic_lines.append("    {")
                    if drag_drive_enabled:
                        logic_lines.append(f"        float anim_weight_slot{slot_index} = IniParams[{self.INTENSITY_START_INDEX} + freq_idx_slot{slot_index}].x;")
                        logic_lines.append(f"        uint sk_zone_slot{slot_index} = SHAPEKEY_ZONE_IDS[freq_idx_slot{slot_index}];")
                        logic_lines.append(f"        uint sk_nd_stage_slot{slot_index} = SHAPEKEY_ND_STAGE_IDS[freq_idx_slot{slot_index}];")
                        logic_lines.append(f"        uint sk_slot_slot{slot_index} = SHAPEKEY_SLOT_IDS[freq_idx_slot{slot_index}];")
                        logic_lines.append(f"        if (sk_zone_slot{slot_index} != 0xFFFFFFFFu && (sk_nd_stage_slot{slot_index} == 0xFFFFFFFFu || ShapeKeyClickCount[sk_zone_slot{slot_index}] == sk_nd_stage_slot{slot_index}))")
                        logic_lines.append("        {")
                        logic_lines.append(f"            anim_weight_slot{slot_index} = ShapeKeyDrive[sk_slot_slot{slot_index}];")
                        logic_lines.append("        }")
                    else:
                        logic_lines.append(f"        float anim_weight_slot{slot_index} = IniParams[{self.INTENSITY_START_INDEX} + freq_idx_slot{slot_index}].x;")

                    if use_packed:
                        logic_lines.extend([f"        int packed_index = shapekey_maps[{slot_index}][i];", "        if (packed_index != -1)", "        {"])
                        logic_lines.append(f"            total_diff_position += shapekey_pos_deltas[{slot_index}][packed_index] * anim_weight_slot{slot_index};")
                        logic_lines.append("        }")
                    else:
                        logic_lines.append(f"        total_diff_position += shapekey_pos_deltas[{slot_index}][i] * anim_weight_slot{slot_index};")

                    logic_lines.append("    }")
            else:
                for slot_num, names_data in sorted(hash_slot_data.items()):
                    slot_index = slot_num - 1
                    is_first_if = True

                    logic_lines.extend([f"    // --- Slot {slot_index} (t{51+slot_index}) ---", f"    float anim_weight_slot{slot_index} = 0.0;"])
                    for name, objects in names_data.items():
                        for obj in objects:
                            start_def, end_def = obj_to_range_defs[obj]
                            if_cmd = "if" if is_first_if else "else if"
                            logic_lines.append(f"    {if_cmd} (i >= {start_def} && i <= {end_def}) {{ anim_weight_slot{slot_index} = {name_to_freq_def[name]}; }} // Name: {name}")
                            is_first_if = False

                    logic_lines.extend([f"    if (anim_weight_slot{slot_index} > 1e-5)", "    {"])

                    indent = "        "
                    read_idx = "i"
                    if use_packed:
                        logic_lines.extend([f"        int packed_index = shapekey_maps[{slot_index}][i];", "        if (packed_index != -1)", "        {"])
                        read_idx = "packed_index"
                        indent = "            "

                    if use_delta:
                        calc_line = f"total_diff_position += shapekey_pos_deltas[{slot_index}][{read_idx}] * anim_weight_slot{slot_index};"
                    else:
                        calc_line = f"total_diff_position += (shapekeys[{slot_index}][{read_idx}].position - base[i].position) * anim_weight_slot{slot_index};"

                    logic_lines.append(indent + calc_line)

                    if use_packed: logic_lines.extend(["        }", "    }\n"])
                    else: logic_lines.extend(["    }\n"])

            content = re.sub(r"// --- \[PYTHON-MANAGED BLOCK START\] ---.*?// --- \[PYTHON-MANAGED BLOCK END\] ---",
                             f"// --- [PYTHON-MANAGED BLOCK START] ---\n{chr(10).join(define_lines)}\n// --- [PYTHON-MANAGED BLOCK END] ---",
                             content, flags=re.DOTALL)
            content = re.sub(r"// --- \[PYTHON-MANAGED LOGIC START\] ---.*?// --- \[PYTHON-MANAGED LOGIC END\] ---",
                             f"// --- [PYTHON-MANAGED LOGIC START] ---\n{chr(10).join(logic_lines)}    // --- [PYTHON-MANAGED LOGIC END] ---",
                             content, flags=re.DOTALL)

            with open(shader_path, 'w', encoding='utf-8') as f:
                f.write(content)

            mode_str = (
                f"紧凑:{'是' if use_packed else '否'}, "
                f"增量(仅位置):{'是' if use_delta else '否'}, "
                f"优化查找:{'是' if use_optimized else '否'}, "
                f"文件合并:{'是' if merge_slot_files else '否'}"
            )
            print(f"成功更新着色器 ({mode_str})，支持 {len(hash_slot_data)} 个槽位。")
            return True
        except Exception as e:
            print(f"更新着色器文件失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _generate_vertex_freq_index_Meshess(self, mod_export_path, hash_val, hash_slot_data, unique_names, vertex_count, calculated_ranges):
        if not NUMPY_AVAILABLE:
            print("Numpy库未找到，无法生成FREQ索引缓冲区")
            return False

        name_to_freq_index = {name: i for i, name in enumerate(unique_names)}
        print(f"    [DEBUG] 形态键到FREQ索引映射: {name_to_freq_index}")
        print(f"    [DEBUG] calculated_ranges 键: {list(calculated_ranges.keys())}")

        num_slots = max(hash_slot_data.keys()) if hash_slot_data else 0

        if num_slots <= 0 or vertex_count <= 0:
            print("    [DEBUG] 没有可用槽位或顶点数为 0，跳过 FREQ 索引生成")
            return 0

        freq_indices = np.full((vertex_count, num_slots), 255, dtype=np.uint32)
        merge_slot_files = self._should_merge_slot_files()

        merged_index_map = None
        if merge_slot_files:
            merged_map_path = os.path.join(mod_export_path, "Meshes0000", f"{hash_val}-Position_merged_map.buf")
            if os.path.exists(merged_map_path):
                try:
                    with open(merged_map_path, 'rb') as f:
                        merged_map_flat = np.frombuffer(f.read(), dtype=np.int32)

                    expected_size = vertex_count * num_slots
                    if merged_map_flat.size >= expected_size:
                        merged_index_map = merged_map_flat[:expected_size].reshape((vertex_count, num_slots))
                        print(f"    [DEBUG] 加载合并映射文件: {os.path.basename(merged_map_path)}")
                    else:
                        print(
                            f"    [WARNING] 合并映射文件长度不足: {merged_map_flat.size} < {expected_size}，"
                            "将回退到范围写入。"
                        )
                except Exception as e:
                    print(f"    [WARNING] 读取合并映射文件失败: {e}")

        for slot_num, names_data in hash_slot_data.items():
            slot_index = slot_num - 1
            slot_index_map = None

            if not merge_slot_files:
                folder_name = f"Meshes1{slot_num:03d}"
                map_path, resolved_map_hash = self._resolve_map_buffer_path(
                    mod_export_path,
                    folder_name,
                    hash_val,
                    preferred_hashes=[hash_val],
                )
                if os.path.exists(map_path):
                    try:
                        with open(map_path, 'rb') as f:
                            slot_index_map = np.frombuffer(f.read(), dtype=np.int32)
                        print(f"    [DEBUG] 加载映射文件: {os.path.basename(map_path)} (hash={resolved_map_hash})")
                    except Exception as e:
                        print(f"    [WARNING] 读取映射文件失败: {e}")

            print(
                f"    [DEBUG] Slot {slot_num}: 处理 {len(names_data)} 个形态键, "
                f"{'使用合并映射' if merged_index_map is not None else ('找到 1 个映射文件' if slot_index_map is not None else '未找到映射文件')}"
            )
            for name, objects in names_data.items():
                freq_idx = name_to_freq_index.get(name, 255)
                print(f"      [DEBUG] 形态键 '{name}' -> FREQ索引 {freq_idx}, 物体: {objects}")
                if freq_idx == 255:
                    continue

                for obj_name in objects:
                    obj_hash = self._extract_hash_from_name(obj_name)
                    obj_prefix = self._extract_hash_prefix(obj_hash) if obj_hash else None
                    hash_prefix = self._extract_hash_prefix(hash_val)
                    if obj_prefix != hash_prefix:
                        print(f"        [DEBUG] 跳过物体 '{obj_name}' (前缀不匹配: {obj_prefix} != {hash_prefix})")
                        continue

                    if obj_name not in calculated_ranges:
                        print(f"        [DEBUG] 物体 '{obj_name}' 不在 calculated_ranges 中")
                        continue

                    start_v, end_v = calculated_ranges[obj_name]
                    if start_v is None or end_v is None:
                        print(f"        [DEBUG] 物体 '{obj_name}' 范围无效: {start_v}-{end_v}")
                        continue

                    start_v = max(0, min(start_v, vertex_count - 1))
                    end_v = max(0, min(end_v, vertex_count - 1))
                    print(f"        [DEBUG] 物体 '{obj_name}' 设置顶点 {start_v}-{end_v} 为 FREQ索引 {freq_idx}")

                    if merged_index_map is not None:
                        index_map = merged_index_map[:, slot_index]
                    else:
                        index_map = slot_index_map

                    if index_map is not None:
                        valid_local = np.flatnonzero(index_map[start_v:end_v + 1] >= 0)
                        if valid_local.size > 0:
                            valid_vertices = valid_local + start_v
                            freq_indices[valid_vertices, slot_index] = freq_idx
                    else:
                        freq_indices[start_v:end_v + 1, slot_index] = freq_idx
                        print(f"        [DEBUG] 物体 '{obj_name}' 没有映射文件，直接设置所有顶点")

        output_dir = os.path.join(mod_export_path, "Meshes0000")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{hash_val}-Position_freq_indices.buf")

        with open(output_path, 'wb') as f:
            f.write(freq_indices.reshape(-1).tobytes())

        print(f"    生成FREQ索引缓冲区: {os.path.basename(output_path)} (顶点数: {vertex_count}, 槽位数: {num_slots})")
        print(f"    [DEBUG] FREQ索引值分布: {dict(zip(*np.unique(freq_indices, return_counts=True)))}")

        return num_slots

    def execute_postprocess(self, mod_export_path):
        print(f"形态键配置后处理节点开始执行，Mod导出路径: {mod_export_path}")

        classification_text_obj = next((t for t in bpy.data.texts if "Shape_Key_Classification" in t.name), None)
        if not classification_text_obj:
            print("未找到 'Shape_Key_Classification' 文本")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            print("路径中未找到任何.ini文件")
            return

        target_ini_file = ini_files[0]
        use_packed = self.use_packed_Meshess
        use_delta = self.store_deltas
        use_optimized = self.use_optimized_lookup
        merge_slot_files = self._should_merge_slot_files(use_packed)

        if (use_packed or use_delta or use_optimized) and not NUMPY_AVAILABLE:
            print("Numpy库未找到，无法使用优化功能")
            return

        shader_source_path = self._get_shader_source_path()
        if not shader_source_path or not os.path.exists(shader_source_path):
            print(f"着色器模板文件未找到: {shader_source_path}")
            return

        print(f"使用着色器模板: {self._get_shader_template_name()}")

        self._create_cumulative_backup(target_ini_file, mod_export_path)

        try:
            sections, preserved_tail_content, preserved_driver_content = self._read_ini_to_ordered_dict(target_ini_file)
            slot_to_name_to_objects, unique_hashes, hash_to_objects, all_objects = self._parse_classification_text_final(classification_text_obj.as_string())

            if not slot_to_name_to_objects:
                print("分类文本解析失败或为空")
                return

            hash_to_stride = {}
            success, hash_to_actual_file_hash = self._process_shapekey_Meshess(mod_export_path, slot_to_name_to_objects, hash_to_stride)
            if not success:
                print("缓冲区处理失败")
                return

            print(f"  [DEBUG] 哈希映射表: {hash_to_actual_file_hash}")

            all_unique_names = list(OrderedDict.fromkeys(name for slot_data in slot_to_name_to_objects.values() for name in slot_data.keys()))
            all_unique_objects = list(OrderedDict.fromkeys(obj for slot_data in slot_to_name_to_objects.values() for name_data in slot_data.values() for obj in name_data))

            hash_to_base_resources = {}
            resource_pattern = re.compile(r'\[(Resource_?([a-f0-9]{8}(?:[_-][a-f0-9]+)*)_?Position(\d*))\]')
            for section_name in sections.keys():
                match = resource_pattern.match(section_name)
                if match:
                    full_name, hash_val, number = match.groups()
                    # 这里只登记基础 Position Resource，带编号的派生资源会在后续生成逻辑里单独处理。
                    if number:
                        continue
                    hash_val_normalized = hash_val.replace('_', '-')
                    hash_prefix = self._extract_hash_prefix(hash_val_normalized)
                    if hash_prefix and hash_prefix not in hash_to_base_resources:
                        hash_to_base_resources[hash_prefix] = []
                    if hash_prefix:
                        hash_to_base_resources[hash_prefix].append((int(number) if number else 1, full_name))
            for hash_val in hash_to_base_resources:
                hash_to_base_resources[hash_val].sort()
                hash_to_base_resources[hash_val] = [name for key, name in hash_to_base_resources[hash_val]]

            print("开始自动计算顶点索引范围...")
            draw_info_map = self._parse_ini_for_draw_info(sections, mod_export_path)
            print(f"  [DEBUG] draw_info_map 键: {list(draw_info_map.keys())}")
            print(f"  [DEBUG] all_objects: {all_objects}")
            
            mesh_name_to_range_and_ib = {}
            mesh_candidates_by_prefix = defaultdict(list)
            hash_to_base_resources = collect_base_position_resource_map(
                sections,
                self._extract_hash_prefix,
            )

            mesh_runtime_alias_map = defaultdict(list)
            mesh_base_alias_map = defaultdict(list)
            for mesh_name, info_list in draw_info_map.items():
                all_ranges = []
                ib_path = None
                for info in info_list:
                    start_v, end_v = self._calculate_vertex_range(info['ib_path'], info['draw_params'])
                    if start_v is not None and end_v is not None:
                        all_ranges.append((start_v, end_v))
                        if ib_path is None:
                            ib_path = info['ib_path']
                
                if all_ranges:
                    min_start = min(r[0] for r in all_ranges)
                    max_end = max(r[1] for r in all_ranges)
                    mesh_name_to_range_and_ib[mesh_name] = (min_start, max_end, ib_path)

                    mesh_hash = self._extract_hash_from_name(mesh_name)
                    mesh_prefix = self._extract_hash_prefix(mesh_hash) if mesh_hash else None
                    if mesh_prefix:
                        mesh_alias = self._extract_alias_from_name(mesh_name)
                        runtime_alias = self._strip_runtime_copy_suffix(mesh_alias).casefold()
                        base_alias = self._strip_object_suffix(mesh_alias).casefold()
                        candidate = (mesh_name, min_start, max_end, ib_path)
                        mesh_candidates_by_prefix[mesh_prefix].append(candidate)
                        mesh_runtime_alias_map[(mesh_prefix, runtime_alias)].append(candidate)
                        mesh_base_alias_map[(mesh_prefix, base_alias)].append(candidate)
            
            calculated_ranges = {}
            for obj_name in all_objects:
                if obj_name in mesh_name_to_range_and_ib:
                    start_v, end_v, ib_path = mesh_name_to_range_and_ib[obj_name]
                    calculated_ranges[obj_name] = (start_v, end_v, ib_path)
                    print(f"  [DEBUG] 映射物体 '{obj_name}' -> 范围 ({start_v}, {end_v}), IB: {os.path.basename(ib_path)}")
                else:
                    obj_hash = self._extract_hash_from_name(obj_name)
                    if obj_hash:
                        obj_prefix = self._extract_hash_prefix(obj_hash)
                        obj_alias = self._extract_alias_from_name(obj_name)
                        obj_runtime_alias = self._strip_runtime_copy_suffix(obj_alias).casefold()
                        obj_base_alias = self._strip_object_suffix(obj_alias).casefold()

                        runtime_matches = mesh_runtime_alias_map.get((obj_prefix, obj_runtime_alias), [])
                        if len(runtime_matches) == 1:
                            mesh_name, start_v, end_v, ib_path = runtime_matches[0]
                            calculated_ranges[obj_name] = (start_v, end_v, ib_path)
                            print(f"  [DEBUG] 映射物体 '{obj_name}' -> 通过运行时别名匹配 '{mesh_name}' -> 范围 ({start_v}, {end_v}), IB: {os.path.basename(ib_path)}")
                            continue
                        if len(runtime_matches) > 1:
                            matched_names = ", ".join(item[0] for item in runtime_matches)
                            print(f"  [WARNING] 物体 '{obj_name}' 的运行时别名匹配到多个候选: {matched_names}，跳过自动映射")
                            continue

                        base_matches = mesh_base_alias_map.get((obj_prefix, obj_base_alias), [])
                        if len(base_matches) == 1:
                            mesh_name, start_v, end_v, ib_path = base_matches[0]
                            calculated_ranges[obj_name] = (start_v, end_v, ib_path)
                            print(f"  [DEBUG] 映射物体 '{obj_name}' -> 通过基础别名匹配 '{mesh_name}' -> 范围 ({start_v}, {end_v}), IB: {os.path.basename(ib_path)}")
                            continue
                        if len(base_matches) > 1:
                            matched_names = ", ".join(item[0] for item in base_matches)
                            print(f"  [WARNING] 物体 '{obj_name}' 的基础别名匹配到多个候选: {matched_names}，跳过自动映射")
                            continue

                        prefix_candidates = mesh_candidates_by_prefix.get(obj_prefix, [])
                        if len(prefix_candidates) == 1:
                            mesh_name, start_v, end_v, ib_path = prefix_candidates[0]
                            calculated_ranges[obj_name] = (start_v, end_v, ib_path)
                            print(f"  [DEBUG] 映射物体 '{obj_name}' -> 通过唯一前缀候选匹配 '{mesh_name}' -> 范围 ({start_v}, {end_v}), IB: {os.path.basename(ib_path)}")
                        elif len(prefix_candidates) > 1:
                            candidate_names = ", ".join(item[0] for item in prefix_candidates[:8])
                            print(
                                f"  [WARNING] 物体 '{obj_name}' 存在 {len(prefix_candidates)} 个同前缀候选但别名未匹配，"
                                f"已跳过自动映射。候选示例: {candidate_names}"
                            )
            
            print(f"  [DEBUG] calculated_ranges (合并前): {len(calculated_ranges)} 个条目")
            
            calculated_ranges, range_name_mapping = self._merge_identical_ranges(calculated_ranges)
            print(f"  [DEBUG] calculated_ranges (合并后): {len(calculated_ranges)} 个条目")
            
            if range_name_mapping:
                new_all_objects = []
                seen_merged = set()
                for obj_name in all_objects:
                    new_name = range_name_mapping.get(obj_name, obj_name)
                    if new_name not in seen_merged:
                        new_all_objects.append(new_name)
                        seen_merged.add(new_name)
                all_objects[:] = new_all_objects
                
                for slot in slot_to_name_to_objects:
                    for name in slot_to_name_to_objects[slot]:
                        new_objs = []
                        seen = set()
                        for obj in slot_to_name_to_objects[slot][name]:
                            mapped = range_name_mapping.get(obj, obj)
                            if mapped not in seen:
                                new_objs.append(mapped)
                                seen.add(mapped)
                        slot_to_name_to_objects[slot][name] = new_objs
                
                for h in hash_to_objects:
                    new_objs = []
                    seen = set()
                    for obj in hash_to_objects[h]:
                        mapped = range_name_mapping.get(obj, obj)
                        if mapped not in seen:
                            new_objs.append(mapped)
                            seen.add(mapped)
                    hash_to_objects[h] = new_objs
                
                all_unique_objects = list(OrderedDict.fromkeys(
                    range_name_mapping.get(obj, obj) for obj in all_unique_objects
                ))
                all_unique_names = list(OrderedDict.fromkeys(all_unique_names))

            vertex_counts = {}
            for s, ls in sections.items():
                m = re.match(r'\[TextureOverride_([a-f0-9]{8}(?:[_-][a-f0-9]+)*)_[^_]*_VertexLimitRaise\]', s)
                if m:
                    for l in ls:
                        if l.strip().startswith('override_vertex_count'):
                            try:
                                hash_val = m.group(1).replace('_', '-')
                                hash_prefix = self._extract_hash_prefix(hash_val)
                                if hash_prefix:
                                    vertex_counts[hash_prefix] = int(l.split('=')[1].strip())
                                    print(f"  [DEBUG] 从INI读取顶点数: section={s}, hash_prefix={hash_prefix}, count={vertex_counts[hash_prefix]}")
                            except (ValueError, IndexError):
                                pass

            print(f"  [DEBUG] vertex_counts 字典: {vertex_counts}")

            for h in unique_hashes:
                h_prefix = self._extract_hash_prefix(h)
                if h_prefix not in vertex_counts:
                    resolved_hash = hash_to_actual_file_hash.get(h, h)
                    resolved_base_path, resolved_base_hash = self._resolve_position_buffer_path(
                        mod_export_path,
                        "Meshes0000",
                        h,
                        preferred_hashes=[resolved_hash],
                    )
                    if os.path.exists(resolved_base_path):
                        try:
                            file_size = os.path.getsize(resolved_base_path)
                            stride = hash_to_stride.get(h_prefix, 40)
                            inferred_count = file_size // stride
                            vertex_counts[h_prefix] = inferred_count
                            print(
                                f"  [DEBUG] 从文件大小推断顶点数: hash_prefix={h_prefix}, "
                                f"file={os.path.basename(resolved_base_path)}, resolved_hash={resolved_base_hash}, "
                                f"size={file_size}, stride={stride}, count={inferred_count}"
                            )
                        except Exception as e:
                            print(f"  [WARNING] 无法推断顶点数: {e}")

            dest_res_dir = os.path.join(mod_export_path, "res")
            os.makedirs(dest_res_dir, exist_ok=True)

            drag_drive_enabled = bool(getattr(self, "drag_drive_enabled", False))
            drag_drive_resource = None
            if drag_drive_enabled:
                drag_drive_resource = self._drag_shapekey_drive_resource_name(target_ini_file)
                if not drag_drive_resource:
                    print("[ShapeKey] 已开启拖拽驱动但未找到启用了驱动输出的拖拽节点，回退到强度变量")
                    drag_drive_enabled = False

            hash_to_shader_paths = {}
            for hash_val in unique_hashes:
                shader_dest_path = os.path.join(dest_res_dir, f"shapekey_anim_{hash_val}.hlsl")
                shutil.copy2(shader_source_path, shader_dest_path)
                hash_to_shader_paths[hash_val] = shader_dest_path
                print(f"已创建独立着色器文件: shapekey_anim_{hash_val}.hlsl")

            for hash_val in unique_hashes:
                hash_objects = hash_to_objects.get(hash_val, [])
                hash_slot_data = {}
                for slot, name_data in slot_to_name_to_objects.items():
                    for name, objects in name_data.items():
                        if any(obj in hash_objects for obj in objects):
                            if slot not in hash_slot_data:
                                hash_slot_data[slot] = {}
                            hash_slot_data[slot][name] = [obj for obj in objects if obj in hash_objects]

                if hash_slot_data:
                    hash_unique_names = list(OrderedDict.fromkeys(name for slot_data in hash_slot_data.values() for name in slot_data.keys()))
                    hash_unique_objects = list(OrderedDict.fromkeys(obj for slot_data in hash_slot_data.values() for name_data in slot_data.values() for obj in name_data))

                    if use_optimized:
                        hash_prefix = self._extract_hash_prefix(hash_val)
                        vertex_count = vertex_counts.get(hash_prefix, 10000)
                        actual_file_hash = hash_to_actual_file_hash.get(hash_val, hash_val)
                        self._generate_vertex_freq_index_Meshess(mod_export_path, actual_file_hash, hash_slot_data, hash_unique_names, vertex_count, calculated_ranges)

                    if not self._update_shader_file(
                        hash_to_shader_paths[hash_val],
                        hash_slot_data,
                        use_packed,
                        use_delta,
                        hash_unique_names,
                        hash_unique_objects,
                        use_optimized,
                        merge_slot_files,
                        drag_drive_enabled=drag_drive_enabled,
                        drag_zone_ids=self._drag_drive_zone_ids(hash_unique_names) if drag_drive_enabled else None,
                        drag_click_stages=self._drag_drive_click_stages(hash_unique_names) if drag_drive_enabled else None,
                        drag_stage_count=self._drag_drive_stage_count(),
                        drag_dirs=self._drag_drive_dirs(hash_unique_names) if drag_drive_enabled else None,
                        hash_val=hash_val,
                    ):
                        print(f"更新哈希 {hash_val} 的着色器文件失败")

            if '[Constants]' not in sections:
                sections['[Constants]'] = []
            constants_lines = sections['[Constants]']
            constants_content = "".join(constants_lines)
            vars_to_define = set()

            shapekey_freq_params = {}
            for name in all_unique_names:
                shapekey_freq_params[name] = self.get_shape_key_export_variable_name(name)

            constants_lines.append("\n; --- Auto-generated Shape Key Intensity Controls (Additive Blending) ---")
            for name, param in shapekey_freq_params.items():
                if param not in constants_content:
                    constants_lines.append(f"; 控制形态键 '{name}' 的强度")
                    constants_lines.append(f"global persist {param} = 0.0")

            vertex_range_vars = {}
            if not use_optimized:
                constants_lines.append("\n; --- Auto-generated Vertex Ranges for Shape Keys ---")
                existing_vertex_range_names = set()
                for obj_name, (start_v, end_v) in calculated_ranges.items():
                    if start_v is None:
                        continue
                    safe_name = self._create_safe_var_name(obj_name.replace("-", "_"), existing_names=existing_vertex_range_names)
                    start_var, end_var = f"$SV_{safe_name}", f"$EV_{safe_name}"
                    vertex_range_vars[obj_name] = (start_var, end_var)
                    if start_var not in constants_content:
                        constants_lines.append(f"global {start_var} = {start_v}")
                    if end_var not in constants_content:
                        constants_lines.append(f"global {end_var} = {end_v}")

            for h in unique_hashes:
                h_prefix = self._extract_hash_prefix(h)
                base_resources = hash_to_base_resources.get(h_prefix, [])
                res_to_post = base_resources if base_resources else [f"Resource_{self._hash_to_resource_prefix(h)}_Position"]
                for res_name in res_to_post:
                    ensure_resource_alias_section(
                        sections,
                        res_name,
                        "_0",
                        source_candidates=[res_name],
                    )
                    if f"post {res_name} = copy_desc" not in constants_content:
                        constants_lines.append(f"post {res_name} = copy_desc {res_name}_0")
                if len(base_resources) > 1:
                    # ShapeKey 的内部基础网格选择器必须与 MultiFile 对用户公开的
                    # animation_swapkey（历史默认 $swapkey100）隔离。
                    vars_to_define.add("$ssmt_sk_base_mesh")
                if f"post run = CustomShader_{h}_Anim" not in constants_content:
                    constants_lines.append(f"post run = CustomShader_{h}_Anim")

            if vars_to_define:
                constants_lines.append("\n; --- Auto-generated Base Mesh Switch Key ---")
                for var in sorted(list(vars_to_define)):
                    if f"global persist {var}" not in constants_content and f"global {var}" not in constants_content:
                        constants_lines.append(f"global persist {var} = 1")

            if '[Present]' not in sections:
                sections['[Present]'] = []
            present_lines = sections['[Present]']
            for h in unique_hashes:
                run_line = f"    run = CustomShader_{h}_Anim"
                if run_line not in present_lines:
                    present_lines.append(run_line)

            compute_blocks_to_add = OrderedDict()
            for h in unique_hashes:
                block_name = f"[CustomShader_{h}_Anim]"

                hash_objects = hash_to_objects.get(h, [])
                hash_slot_data = {}
                for slot, name_data in slot_to_name_to_objects.items():
                    for name, objects in name_data.items():
                        matching_objs = [obj for obj in objects if obj in hash_objects]
                        if matching_objs:
                            if slot not in hash_slot_data:
                                hash_slot_data[slot] = {}
                            hash_slot_data[slot][name] = matching_objs

                if hash_slot_data:
                    hash_unique_names = list(OrderedDict.fromkeys(name for slot_data in hash_slot_data.values() for name in slot_data.keys()))
                    hash_unique_objects = list(OrderedDict.fromkeys(obj for slot_data in hash_slot_data.values() for name_data in slot_data.values() for obj in name_data))

                    block_lines = ["\n    ; --- Shared Intensity Controls (per Shape Key Name) ---"]
                    for i, name in enumerate(hash_unique_names):
                        if shapekey_freq_params.get(name):
                            block_lines.append(f"    x{self.INTENSITY_START_INDEX + i} = {shapekey_freq_params.get(name)} \n; {name}")
                    if not use_optimized:
                        block_lines.append("\n    ; --- Per-Object Vertex Range Controls ---")
                        for i, obj_name in enumerate(hash_unique_objects):
                            if obj_name in calculated_ranges and calculated_ranges[obj_name][0] is not None:
                                start_var, end_var = vertex_range_vars.get(obj_name, (f"$SV_unknown", f"$EV_unknown"))
                                block_lines.append(f"    x{self.VERTEX_RANGE_START_INDEX + i*2} = {start_var} \n; {obj_name} Start")
                                block_lines.append(f"    x{self.VERTEX_RANGE_START_INDEX + i*2 + 1} = {end_var} \n; {obj_name} End")

                    t_registers_to_null = []
                    slots_for_hash = sorted(hash_slot_data.keys())
                    base_resources = hash_to_base_resources.get(h_prefix, [])
                    primary_base_resource = base_resources[0] if base_resources else f"Resource_{self._hash_to_resource_prefix(h)}_Position"

                    if not use_delta:
                        block_lines.append(f"\n    cs-t50 = copy {derive_shapekey_base_resource_name(primary_base_resource)}")
                        t_registers_to_null.append("cs-t50")

                    res_suffix = "_packed_pos_delta" if use_packed and use_delta else \
                                 "_pos_delta" if use_delta else \
                                 "_packed" if use_packed else ""

                    mode_str = (
                        f"紧凑:{'是' if use_packed else '否'}, "
                        f"增量(仅位置):{'是' if use_delta else '否'}, "
                        f"优化查找:{'是' if use_optimized else '否'}, "
                        f"文件合并:{'是' if merge_slot_files else '否'}"
                    )
                    block_lines.append(f"\n    ; --- Binding Shape Key Meshess (Mode: {mode_str}) ---")
                    if merge_slot_files:
                        block_lines.append(f"    cs-t51 = copy {derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}")
                        block_lines.append(f"    cs-t52 = copy {derive_shapekey_merged_map_resource_name(primary_base_resource)}")
                        t_registers_to_null.extend(["cs-t51", "cs-t52"])

                        if use_optimized:
                            block_lines.append(f"    cs-t53 = copy {derive_shapekey_freq_resource_name(primary_base_resource)}")
                            t_registers_to_null.append("cs-t53")
                    else:
                        for slot in slots_for_hash:
                            res_name = derive_shapekey_slot_resource_name(primary_base_resource, slot, res_suffix if (use_packed or use_delta) else "")

                            t_reg = 51 + slot - 1
                            block_lines.append(f"    cs-t{t_reg} = copy {res_name}")
                            t_registers_to_null.append(f"cs-t{t_reg}")
                            if use_packed:
                                map_reg = 75 + slot - 1
                                block_lines.append(f"    cs-t{map_reg} = copy {derive_shapekey_slot_map_resource_name(primary_base_resource, slot)}")
                                t_registers_to_null.append(f"cs-t{map_reg}")

                        if use_optimized:
                            block_lines.append(f"    cs-t99 = copy {derive_shapekey_freq_resource_name(primary_base_resource)}")
                            t_registers_to_null.append("cs-t99")

                    if drag_drive_resource:
                        block_lines.append("\n    ; --- Drag ShapeKey Drive ---")
                        block_lines.append(f"    cs-t{self.DRAG_DRIVE_REGISTER} = {drag_drive_resource}")
                        click_resource = self._drag_shapekey_click_count_resource_name(target_ini_file)
                        if click_resource:
                            block_lines.append(f"    cs-t{self.DRAG_CLICK_COUNT_REGISTER} = {click_resource}")
                        t_registers_to_null.append(f"cs-t{self.DRAG_DRIVE_REGISTER}")
                        t_registers_to_null.append(f"cs-t{self.DRAG_CLICK_COUNT_REGISTER}")

                    block_lines.append(f"    cs = ./res/shapekey_anim_{h}.hlsl")

                    h_prefix = self._extract_hash_prefix(h)
                    res_to_bind = base_resources if base_resources else [f"Resource_{self._hash_to_resource_prefix(h)}_Position"]
                    if len(res_to_bind) > 1:
                        block_lines.append(f"\n    ; --- Base Mesh Switching ---")
                        for i, res_name in enumerate(res_to_bind, 1):
                            ensure_resource_alias_section(
                                sections,
                                res_name,
                                "_0",
                                source_candidates=[res_name],
                            )
                            block_lines.extend([f"    if $ssmt_sk_base_mesh == {i}", f"        cs-u5 = copy {res_name}_0", f"        {res_name} = ref cs-u5", "    endif"])
                    else:
                        res_name = res_to_bind[0]
                        ensure_resource_alias_section(
                            sections,
                            res_name,
                            "_0",
                            source_candidates=[res_name],
                        )
                        block_lines.extend([f"    cs-u5 = copy {res_name}_0", f"    {res_name} = ref cs-u5"])

                    dispatch_count = self._compute_dispatch_group_count(vertex_counts.get(h_prefix, 0), threads_per_group=16)
                    block_lines.extend([f"    Dispatch = {dispatch_count}, 1, 1", "    cs-u5 = null", *[f"    {reg} = null" for reg in sorted(list(set(t_registers_to_null)))]])
                    compute_blocks_to_add[block_name] = block_lines

            new_resource_lines = []
            generated_section_names = set()

            for h in unique_hashes:
                h_prefix = self._extract_hash_prefix(h)
                actual_file_hash = hash_to_actual_file_hash.get(h, h)
                base_resources = hash_to_base_resources.get(h_prefix, [])
                primary_base_resource = base_resources[0] if base_resources else f"Resource_{self._hash_to_resource_prefix(h)}_Position"
                section_name = f"[{derive_shapekey_base_resource_name(primary_base_resource)}]"
                if section_name not in sections and section_name not in generated_section_names:
                    stride = hash_to_stride.get(h_prefix, 40)
                    new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = Meshes0000/{actual_file_hash}-Position.buf", ""])
                    generated_section_names.add(section_name)

            if merge_slot_files:
                for h in unique_hashes:
                    h_prefix = self._extract_hash_prefix(h)
                    if not h_prefix:
                        continue

                    actual_file_hash = hash_to_actual_file_hash.get(h, h)
                    base_stride = hash_to_stride.get(h_prefix, 40)
                    data_stride = 12 if use_delta else base_stride
                    base_resources = hash_to_base_resources.get(h_prefix, [])
                    primary_base_resource = base_resources[0] if base_resources else f"Resource_{self._hash_to_resource_prefix(h)}_Position"
                    data_section = f"[{derive_shapekey_merged_data_resource_name(primary_base_resource, use_delta)}]"
                    data_filename = f"Meshes0000/{actual_file_hash}-Position{self._get_merged_data_file_suffix(use_delta)}.buf"
                    if data_section not in sections and data_section not in generated_section_names:
                        new_resource_lines.extend([data_section, "type = Buffer", f"stride = {data_stride}", f"filename = {data_filename}", ""])
                        generated_section_names.add(data_section)

                    map_section = f"[{derive_shapekey_merged_map_resource_name(primary_base_resource)}]"
                    map_filename = f"Meshes0000/{actual_file_hash}-Position_merged_map.buf"
                    if map_section not in sections and map_section not in generated_section_names:
                        new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = {map_filename}", ""])
                        generated_section_names.add(map_section)
            else:
                for slot, names_data in slot_to_name_to_objects.items():
                    for obj in [o for name, objs in names_data.items() for o in objs]:
                        h = self._extract_hash_from_name(obj)
                        h_prefix = self._extract_hash_prefix(h) if h else None
                        if h_prefix:
                            actual_file_hash = hash_to_actual_file_hash.get(h, h)
                            base_stride = hash_to_stride.get(h_prefix, 40)
                            base_resources = hash_to_base_resources.get(h_prefix, [])
                            primary_base_resource = base_resources[0] if base_resources else f"Resource_{self._hash_to_resource_prefix(h)}_Position"
                            stride, filename, section_name = 0, "", ""
                            if use_delta:
                                res_suffix = "_packed_pos_delta" if use_packed else "_pos_delta"
                                stride = 12
                            elif use_packed:
                                res_suffix = "_packed"
                                stride = base_stride
                            else:
                                res_suffix = ""
                                stride = base_stride

                            if use_delta or use_packed:
                                section_name = f"[{derive_shapekey_slot_resource_name(primary_base_resource, slot, res_suffix)}]"
                                folder_name = f"Meshes1{slot:03d}"
                                filename = f"{folder_name}/{actual_file_hash}-Position{res_suffix}.buf"
                                if section_name not in sections and section_name not in generated_section_names:
                                    new_resource_lines.extend([section_name, "type = Buffer", f"stride = {stride}", f"filename = {filename}", ""])
                                    generated_section_names.add(section_name)

                            if use_packed:
                                map_section = f"[{derive_shapekey_slot_map_resource_name(primary_base_resource, slot)}]"
                                folder_name = f"Meshes1{slot:03d}"
                                if map_section not in sections and map_section not in generated_section_names:
                                    new_resource_lines.extend([map_section, "type = Buffer", "stride = 4", f"filename = {folder_name}/{actual_file_hash}-Position_map.buf", ""])
                                    generated_section_names.add(map_section)

            if use_optimized:
                for h in unique_hashes:
                    actual_file_hash = hash_to_actual_file_hash.get(h, h)
                    h_prefix = self._extract_hash_prefix(h)
                    base_resources = hash_to_base_resources.get(h_prefix, [])
                    primary_base_resource = base_resources[0] if base_resources else f"Resource_{self._hash_to_resource_prefix(h)}_Position"
                    freq_idx_section = f"[{derive_shapekey_freq_resource_name(primary_base_resource)}]"
                    if freq_idx_section not in sections and freq_idx_section not in generated_section_names:
                        new_resource_lines.extend([freq_idx_section, "type = Buffer", "stride = 4", f"filename = Meshes0000/{actual_file_hash}-Position_freq_indices.buf", ""])
                        generated_section_names.add(freq_idx_section)

            if new_resource_lines:
                sections[";; --- Generated Shape Key Meshess ---"] = new_resource_lines

            for h in unique_hashes:
                h_prefix = self._extract_hash_prefix(h)
                for res_name in hash_to_base_resources.get(h_prefix, [f"Resource_{self._hash_to_resource_prefix(h)}_Position"]):
                    ensure_resource_alias_section(
                        sections,
                        res_name,
                        "_0",
                        source_candidates=[res_name],
                    )

            sections.update(compute_blocks_to_add)

            # 终态规整（幂等）：检测到 rank10 多文件段时改为条件锚定、
            # 接力块 rank 排序、复位行去重、_mf 声明、post run 移除（接力协议 v3）
            deform_chain.finalize_deform_chain(sections)

            self._write_ordered_dict_to_ini(sections, target_ini_file, preserved_tail_content, preserved_driver_content)

            mode_str = (
                f"紧凑:{'是' if use_packed else '否'}, "
                f"增量(仅位置):{'是' if use_delta else '否'}, "
                f"优化查找:{'是' if use_optimized else '否'}, "
                f"文件合并:{'是' if merge_slot_files else '否'}"
            )
            print(f"形态键配置({mode_str})已生成到 {os.path.basename(target_ini_file)}")

        except Exception as e:
            print(f"生成形态键配置时发生未知错误: {e}")
            import traceback
            traceback.print_exc()
            raise

        print("形态键配置后处理节点执行完成")


class SSMT_OT_ScanShapeKeyVariables(bpy.types.Operator):
    bl_idname = "ssmt.scan_shapekey_variables"
    bl_label = "预分配蓝图形态键变量"
    bl_options = {'REGISTER', 'UNDO'}

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        tree = context.space_data.edit_tree
        node = tree.nodes.get(self.node_name) if tree else None
        if node is None or node.bl_idname != 'SSMTNode_PostProcess_ShapeKey':
            self.report({'WARNING'}, "未找到形态键配置节点")
            return {'CANCELLED'}

        shape_key_names = node.collect_blueprint_shape_key_names()
        created_count, backfilled_count = node.ensure_shape_key_variable_map(shape_key_names)
        self.report(
            {'INFO'},
            f"已扫描 {len(shape_key_names)} 个形态键，新增预分配 {created_count} 个变量，回填变量框 {backfilled_count} 项"
        )
        return {'FINISHED'}


classes = (
    ShapeKeyVariableItem,
    SSMT_UL_ShapeKeyVariableMappings,
    SSMTNode_PostProcess_ShapeKey,
    SSMT_OT_ScanShapeKeyVariables,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
